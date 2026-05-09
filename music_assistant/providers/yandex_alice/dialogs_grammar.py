"""Yandex Dialogs runtime mapper for the Music Assistant skill.

The declarative source of truth for what the auto-create / auto-update
pipeline ships to dialogs.yandex.ru lives in
``provider/data/skill.toml``. The TOML format and its parser live in
``ya_dialogs_api.manifest`` so format changes (or new Yandex shape
quirks) absorb at the library level without touching every consumer.

This module is the **provider-specific layer** — the runtime mapper
that turns a Yandex-classified intent (form_name + slots) into the
internal :class:`ParsedControl` / :class:`ParsedCommand` that the
webhook dispatcher consumes.

Authoring workflow for grammar / entities tweaks:

* Edit ``provider/data/skill.toml`` — each ``grammar = \"\"\"…\"\"\"`` block
  is the Granet ``sourceText`` byte-for-byte, copy-paste-compatible
  with the dev-console editor at
  ``https://dialogs.yandex.ru/developer/skills/<id>/draft/settings/intents``.
* The runtime mapper (``parse_platform_intent`` and
  ``_CONTROL_INTENT_MAP``) below stays in this file because it owns
  application semantics (which form_name maps to which Music Assistant
  command). Phase B will move that mapping into the manifest as well.

Granet shape rules (empirically pinned through v1.4.x):

* Single-line alternation only (``|`` at start of continuation rejected).
* ``%lemma`` directive standalone above alternation; never inside
  ``[...]`` optional blocks; never after ``|`` in alternation.
* ``slots:`` sub-block lives inside ``sourceText`` — the
  ya-dialogs-api 2.2.0+ client recognises the existing block and
  skips auto-composing structured slots.
"""

from __future__ import annotations

import importlib.resources
import re
from typing import Any

from ya_dialogs_api import (
    EntityDraft,
    IntentDraft,
    SkillManifest,
    iter_intent_matches,
    parse_manifest_text,
)

from .dialogs_control import ControlAction, ParsedControl
from .dialogs_nlu import ParsedCommand

# ---------------------------------------------------------------------------
# Manifest loading — package-bundled default
# ---------------------------------------------------------------------------


def _load_default_manifest_text() -> str:
    """Read ``provider/data/skill.toml`` from the package."""
    ref = importlib.resources.files("provider.data").joinpath("skill.toml")
    return ref.read_text(encoding="utf-8")


def _default_manifest() -> SkillManifest:
    """Parse the package-bundled default skill manifest."""
    return parse_manifest_text(_load_default_manifest_text())


# ---------------------------------------------------------------------------
# Builders — manifest-backed
# ---------------------------------------------------------------------------


def build_entities() -> list[EntityDraft]:
    """Return the custom entities for the skill, parsed from the manifest."""
    return _default_manifest().to_entity_drafts()


def build_grammar() -> list[IntentDraft]:
    """Return the full list of custom intents to declare on the skill."""
    return _default_manifest().to_intent_drafts()


# ---------------------------------------------------------------------------
# Runtime: map platform-classified intents back to dispatcher dataclasses
# ---------------------------------------------------------------------------

# No-slot intents map directly to a ControlAction literal. Slot-bearing
# intents are handled inline in `parse_platform_intent` because they need
# slot extraction. Keep this map in lockstep with the no-slot grammars
# in the manifest — adding a new no-slot intent here without a matching
# manifest entry (or vice versa) would silently misclassify utterances.
_CONTROL_INTENT_MAP: dict[str, ControlAction] = {
    # Originals
    "control.pause": "pause",
    "control.resume": "resume",
    "control.next": "next",
    "control.previous": "previous",
    "control.stop": "stop",
    "control.volume_up": "volume_up",
    "control.volume_down": "volume_down",
    "control.shuffle_on": "shuffle_on",
    "control.shuffle_off": "shuffle_off",
    "control.now_playing": "now_playing",
    # New (v1.4.0)
    "control.mute": "mute",
    "control.unmute": "unmute",
    "control.seek_start": "seek_start",
    "control.repeat_one": "repeat_one",
    "control.repeat_all": "repeat_all",
    "control.repeat_off": "repeat_off",
    "control.list_players": "list_players",
    "control.forget_player": "forget_player",
}


# Default volume delta when Yandex matched a *_increase / *_decrease
# intent without picking up a number — mirrors the historical regex
# fallthrough where bare «прибавь» / «убавь» bumped by ten units.
_DEFAULT_VOLUME_DELTA = 10

# Upper bound on seek-slot values (seconds). Yandex's YANDEX.NUMBER has
# no inherent cap, so a misheard "перемотай на тридцать тысяч секунд"
# would otherwise dispatch ``skip(seconds=30000)`` (~8 h). Anything
# beyond a day clearly didn't mean what it says — return ``None`` and
# let the caller surface a graceful "не понял" instead of skipping
# something nonsensical.
_MAX_SEEK_SECONDS = 24 * 3600


# "на" boundary used to peel the trailing player-hint suffix off a
# platform-parsed command text. Yandex's static custom intents can't
# enumerate the per-user list of player names, so the suffix is
# recovered from the raw command text and attached to the ParsedControl
# alongside.
_NA_BOUNDARY_RE = re.compile(r"\s+на\s+", re.IGNORECASE)

# Hint candidates that are clearly not a player name. Phrases containing
# multiple "на" tokens (e.g. "громкость на 50 на кухне", "перемотай на 30
# секунд", "поставь на паузу на кухне") would otherwise misroute the
# slot-side "на N <unit>" or the action-side "на <noun>" as a hint.
#
# - ``_HINT_UNIT_WORDS`` covers unit nouns that follow numeric slots
#   ("на 30 секунд", "на 50 процентов").
# - ``_HINT_ACTION_WORDS`` covers action-content nouns from grammars
#   that themselves use "на <noun>" (currently only "паузу" from the
#   ``control.pause`` intent in skill.toml — "поставь на паузу" / "на
#   паузу"). When a new intent grammar introduces another such token,
#   add it here.
_HINT_UNIT_WORDS: frozenset[str] = frozenset(
    {
        "секунда",
        "секунды",
        "секунд",
        "сек",
        "минута",
        "минуты",
        "минут",
        "мин",
        "процент",
        "процента",
        "процентов",
    }
)
_HINT_ACTION_WORDS: frozenset[str] = frozenset(
    {
        "паузу",
    }
)


def extract_trailing_player_hint(text: str) -> str | None:
    """Return the lower-cased trailing "на <player>" suffix, or None.

    Examples:
        ``"пауза на кухне"`` → ``"кухне"``
        ``"поставь на паузу на кухне"`` → ``"кухне"`` (only the rightmost
        "на " is taken)
        ``"перемотай вперёд на 30 секунд"`` → ``None`` (the suffix is a
        slot value, not a player name)
        ``"громкость на 50"`` → ``None``

    The suffix is rejected when its first token starts with a digit or
    is one of the unit words ("секунд", "минут", "процентов" and their
    morphological variants) — those follow "на" as part of a numeric
    slot, not as a destination player.
    """
    if not text:
        return None
    parts = _NA_BOUNDARY_RE.split(text)
    if len(parts) < 2:
        return None
    hint = parts[-1].strip().lower()
    if not hint:
        return None
    first_token = hint.split(maxsplit=1)[0]
    if first_token[0].isdigit():
        return None
    if first_token in _HINT_UNIT_WORDS:
        return None
    # Single-token hint matching an action-content noun ("паузу") is
    # part of the action phrase, not a destination player.
    if " " not in hint and hint in _HINT_ACTION_WORDS:
        return None
    return hint


def parse_platform_intent(
    nlu_intents: dict[str, Any] | None,
) -> ParsedControl | ParsedCommand | None:
    """Map a ``request.nlu.intents`` block to the dispatcher's dataclass.

    Slot extraction (``slot_int`` / ``slot_str``) is delegated to
    ``ya_dialogs_api.IntentMatch`` so any future change in Yandex's
    NLU payload shape absorbs at the library level rather than here.

    Returns ``None`` when:
    * The block is missing / empty (no grammar declared, or no match).
    * The matched intent name isn't one we ship a runtime handler for.
    * A slot-bearing intent fired but its slot value is missing /
      malformed (lets the regex fallback pick the phrase up).

    Returns the FIRST recognised intent in iteration order. Yandex doesn't
    guarantee a single match — when grammars overlap the platform may
    surface several — but the grammar set is engineered so each phrase
    pattern lives in exactly one intent.
    """
    for match in iter_intent_matches(nlu_intents):
        # No-slot control intents — map directly.
        action = _CONTROL_INTENT_MAP.get(match.form_name)
        if action is not None:
            return ParsedControl(action=action)

        # Slot-bearing control intents — extract slot values.
        if match.form_name == "control.volume_set":
            level = match.slot_int("level")
            if level is not None:
                return ParsedControl(action="volume_set", value=max(0, min(100, level)))
            continue
        if match.form_name in ("control.volume_increase", "control.volume_decrease"):
            delta = match.slot_int("delta")
            magnitude = abs(delta) if delta is not None else _DEFAULT_VOLUME_DELTA
            magnitude = max(0, min(100, magnitude))
            sign = +1 if match.form_name.endswith("increase") else -1
            return ParsedControl(action="volume_relative", value=sign * magnitude)
        if match.form_name in ("control.seek_forward", "control.seek_back"):
            amount = match.slot_int("amount")
            if amount is None or amount <= 0:
                continue
            unit = match.slot_str("unit") or "seconds"
            seconds = amount * (60 if unit == "minutes" else 1)
            if seconds > _MAX_SEEK_SECONDS:
                # Out-of-range — treat as a misclassification rather than
                # dispatching a multi-hour skip.
                continue
            seek_action: ControlAction = (
                "seek_forward" if match.form_name.endswith("forward") else "seek_back"
            )
            return ParsedControl(action=seek_action, value=seconds)

        # Play intents.
        if match.form_name == "play.my_wave":
            return ParsedCommand(kind="my_wave", query="", radio_mode=True)
    return None
