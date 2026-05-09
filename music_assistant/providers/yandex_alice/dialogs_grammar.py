# ruff: noqa: RUF001
"""Yandex Dialogs custom-intent grammars + entities for the Music Assistant skill.

Intents and entities here are delivered to Yandex via
``ya_dialogs_api.IntentDraft``/``EntityDraft`` (with their ``set_intents`` /
``set_entities`` setters) during skill provisioning. At runtime, when Yandex
matches a user phrase against one of these grammars, it pre-classifies the
intent and surfaces it in ``request.nlu.intents.<form_name>`` (with extracted
slot values) — the webhook handler reads that block first and only falls back
to the in-house regex parsers when the platform produced no match.

Design notes:

* **Maximal coverage of slot- and verb-based commands.** As of v1.4.0 the
  module covers the full control surface that fits a static, declarative
  grammar: pause/resume/stop, next/previous, volume up/down/set/relative,
  mute/unmute, shuffle on/off, repeat one/all/off, seek forward/back/start,
  list/forget players, now-playing, and the My-Wave play intent. What
  stays on regex (`provider/dialogs_control.py`) is what static grammars
  fundamentally can't express: the player-hint suffix ("на кухне", "в
  спальне" — dynamic per-user enum) and the free-text query play domain
  (track/artist/album/playlist/genre).
* **Slot-bearing intents** (``volume_set``, ``volume_increase`` /
  ``volume_decrease``, ``seek_forward`` / ``seek_back``) declare slots
  programmatically via :class:`SlotDeclaration`. The library composes the
  matching ``slots:`` DSL block into ``sourceText`` automatically — see
  ``ya_dialogs_api.IntentDraft.rendered_source_text``.
* **Custom entities** (currently just ``time_unit``) live in
  :func:`build_entities`. Pass that list through
  ``auto_update_skill(entities=...)`` to keep the server-side
  ``customEntities`` source in sync; the wrapper schedules entity sync
  before intent sync so intent grammars referencing entity types pass
  Granet validation.
* **`%lemma` directive** matches all morphological forms of the lemma
  (e.g. ``%lemma включить`` covers «включи / включите / включай /
  включить / включим»). Applied conservatively to verbs that have
  multiple commonly-used forms.
* **Grammar source is server-validated synchronously.** Bad syntax
  surfaces as ``DialogsIntentValidationError`` from ``set_intents()``
  (or ``DialogsEntitiesValidationError`` from ``set_entities()`` for
  the entity DSL).
* **Positive tests** double as documentation and a self-check —
  Yandex's "Протестировать" button in the dev console can run them
  individually for visual regression.

Adding a new intent: append a new entry, regenerate the skill via the
plugin's "Apply skill changes" form action, observe the moderation
cycle complete (minutes to hours for private skills), then exercise the
phrase against a live device.
"""

from __future__ import annotations

import re
from typing import Any

from ya_dialogs_api import EntityDraft, EntityValue, IntentDraft, SlotDeclaration

from .dialogs_control import ControlAction, ParsedControl
from .dialogs_nlu import ParsedCommand

# ---------------------------------------------------------------------------
# Custom entities — referenced from slot-bearing intents
# ---------------------------------------------------------------------------

TIME_UNIT_ENTITY = EntityDraft(
    name="time_unit",
    values=(
        EntityValue(name="seconds", phrases=("секунда", "секунды", "секунд", "сек")),
        EntityValue(name="minutes", phrases=("минута", "минуты", "минут", "мин")),
    ),
)


# ---------------------------------------------------------------------------
# Grammar fragments — control intents (no slots, originals)
# ---------------------------------------------------------------------------

_PAUSE_GRAMMAR = """\
root:
    %lemma
    пауза | поставь на паузу | останови музыку | на паузу
"""

_RESUME_GRAMMAR = """\
root:
    %lemma
    продолжить | возобновить | включи снова
"""

_NEXT_GRAMMAR = """\
root:
    %lemma
    следующая | следующий трек | дальше | переключи
"""

_PREVIOUS_GRAMMAR = """\
root:
    %lemma
    предыдущая | предыдущий трек | назад | вернись
"""

_STOP_GRAMMAR = """\
root:
    %lemma
    стоп | останови | выключи | выключи музыку
"""

_VOLUME_UP_GRAMMAR = """\
root:
    %lemma
    громче | сделай громче | прибавь
"""

_VOLUME_DOWN_GRAMMAR = """\
root:
    %lemma
    тише | сделай тише | убавь
"""

_SHUFFLE_ON_GRAMMAR = """\
root:
    %lemma
    перемешай | включи перемешивание | случайный порядок | в случайном порядке
"""

_SHUFFLE_OFF_GRAMMAR = """\
root:
    выключи перемешивание | не перемешивай | по порядку
"""

_NOW_PLAYING_GRAMMAR = """\
root:
    что играет | что сейчас играет | что мы слушаем | что за песня | что за трек
"""


# ---------------------------------------------------------------------------
# Grammar fragments — control intents (no slots, NEW in v1.4.0)
# ---------------------------------------------------------------------------

_MUTE_GRAMMAR = """\
root:
    %lemma
    приглуши | выключи звук | беззвучно | без звука
"""

_UNMUTE_GRAMMAR = """\
root:
    %lemma
    включи звук | сделай звук | верни звук
"""

_SEEK_START_GRAMMAR = """\
root:
    %lemma
    к началу | в начало | начни заново | начни трек заново | перемотай к началу
"""

_REPEAT_ONE_GRAMMAR = """\
root:
    %lemma
    повтори песню | повтори трек | повтори эту песню | повтори эту | повтори композицию
"""

_REPEAT_ALL_GRAMMAR = """\
root:
    %lemma
    повтори всё | повтори все | повтори плейлист | повтори очередь | повторяй всё
"""

_REPEAT_OFF_GRAMMAR = """\
root:
    %lemma
    выключи повтор | не повторяй | отмени повтор
"""

# NB: Granet rejects multi-line alternations where ``|`` opens a
# continuation line (it sees an empty operand on the prior line). Keep
# all `|`-separated alternatives on a single root line — long lines are
# preferred to that error.
_LIST_PLAYERS_GRAMMAR = """\
root:
    сколько колонок | какие колонки | какие у тебя колонки | перечисли колонки | список колонок | покажи колонки | назови колонки
"""

_FORGET_PLAYER_GRAMMAR = """\
root:
    %lemma
    забудь колонку | сбрось колонку | забудь плеер | забудь выбор | сбрось выбор | поменяй колонку | сменить колонку | выбери колонку заново
"""


# ---------------------------------------------------------------------------
# Grammar fragments — slot-bearing intents (NEW in v1.4.0)
# ---------------------------------------------------------------------------

# YANDEX.NUMBER slot — volume level 0..100 (clamped post-extract).
# NB: Granet rejects ``%lemma`` directives placed inside ``[...]``
# optional blocks ("Некорректный символ"). Express the optional verb
# via top-level alternation instead (with-verb branch / without-verb
# branch).
_VOLUME_SET_GRAMMAR = """\
root:
    громкость [на] $Level [процент | процентов | процента] | %lemma сделать громкость [на] $Level [процент | процентов | процента]
$Level: $YANDEX.NUMBER
"""

# Two separate intents for increase / decrease — Yandex doesn't expose a
# signed-number slot type, so we encode direction in the form_name and
# pick up the magnitude via YANDEX.NUMBER. Alternations stay on one root
# line (Granet rejects ``|`` at the start of a continuation line).
_VOLUME_INCREASE_GRAMMAR = """\
root:
    %lemma прибавить [на] $Delta [процент | процентов | процента] | %lemma сделать громче на $Delta [процент | процентов | процента] | на $Delta [процент | процентов | процента] громче
$Delta: $YANDEX.NUMBER
"""

_VOLUME_DECREASE_GRAMMAR = """\
root:
    %lemma убавить [на] $Delta [процент | процентов | процента] | %lemma сделать тише на $Delta [процент | процентов | процента] | на $Delta [процент | процентов | процента] тише
$Delta: $YANDEX.NUMBER
"""

# Seek with custom-entity unit (seconds / minutes). The runtime mapper
# multiplies by 60 when unit=="minutes" and defaults to seconds when the
# unit slot is absent — the grammar marks $Unit optional so phrases like
# "перемотай вперёд на 30" (no unit-word) still match. Yandex stores
# entity-typed slot values as the entity-value name, not the surface
# phrase.
#
# ``%lemma`` directives can't sit inside ``[...]`` optional blocks
# ("Некорректный символ" from Granet). Express the optional verb via
# top-level alternation: bare-direction branch / verb-led branch.
_SEEK_FORWARD_GRAMMAR = """\
root:
    вперёд [на] $Amount [$Unit] | %lemma перемотать вперёд [на] $Amount [$Unit]
$Amount: $YANDEX.NUMBER
$Unit: $time_unit
"""

_SEEK_BACK_GRAMMAR = """\
root:
    назад [на] $Amount [$Unit] | %lemma перемотать назад [на] $Amount [$Unit]
$Amount: $YANDEX.NUMBER
$Unit: $time_unit
"""


# ---------------------------------------------------------------------------
# Grammar fragments — play intents
# ---------------------------------------------------------------------------

_MY_WAVE_GRAMMAR = """\
root:
    %lemma
    включи мою волну | включи моё радио | поставь мою волну | моя волна
"""


# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------


def build_entities() -> list[EntityDraft]:
    """Return the list of custom entities referenced by intent grammars."""
    return [TIME_UNIT_ENTITY]


def build_grammar() -> list[IntentDraft]:
    """Return the full list of custom intents to declare on the skill."""
    return [
        # ----- Originals (no slots) -------------------------------------
        IntentDraft(
            form_name="control.pause",
            human_readable_name="Пауза",
            source_text=_PAUSE_GRAMMAR,
            positive_tests="пауза\nпоставь на паузу\nостанови музыку\nна паузу",
            negative_tests="включи\nследующая",
        ),
        IntentDraft(
            form_name="control.resume",
            human_readable_name="Продолжить",
            source_text=_RESUME_GRAMMAR,
            positive_tests="продолжи\nпродолжить\nвозобнови\nвключи снова",
        ),
        IntentDraft(
            form_name="control.next",
            human_readable_name="Следующий трек",
            source_text=_NEXT_GRAMMAR,
            positive_tests="следующая\nследующий трек\nдальше\nпереключи",
        ),
        IntentDraft(
            form_name="control.previous",
            human_readable_name="Предыдущий трек",
            source_text=_PREVIOUS_GRAMMAR,
            positive_tests="предыдущая\nпредыдущий трек\nназад\nвернись",
        ),
        IntentDraft(
            form_name="control.stop",
            human_readable_name="Стоп",
            source_text=_STOP_GRAMMAR,
            positive_tests="стоп\nостанови\nвыключи\nвыключи музыку",
        ),
        IntentDraft(
            form_name="control.volume_up",
            human_readable_name="Громче",
            source_text=_VOLUME_UP_GRAMMAR,
            positive_tests="громче\nсделай громче\nприбавь",
        ),
        IntentDraft(
            form_name="control.volume_down",
            human_readable_name="Тише",
            source_text=_VOLUME_DOWN_GRAMMAR,
            positive_tests="тише\nсделай тише\nубавь",
        ),
        IntentDraft(
            form_name="control.shuffle_on",
            human_readable_name="Включить перемешивание",
            source_text=_SHUFFLE_ON_GRAMMAR,
            positive_tests="перемешай\nвключи перемешивание\nслучайный порядок",
        ),
        IntentDraft(
            form_name="control.shuffle_off",
            human_readable_name="Выключить перемешивание",
            source_text=_SHUFFLE_OFF_GRAMMAR,
            positive_tests="выключи перемешивание\nне перемешивай\nпо порядку",
        ),
        IntentDraft(
            form_name="control.now_playing",
            human_readable_name="Что играет",
            source_text=_NOW_PLAYING_GRAMMAR,
            positive_tests="что играет\nчто сейчас играет\nчто мы слушаем",
        ),
        # ----- New no-slot intents (v1.4.0) ----------------------------
        IntentDraft(
            form_name="control.mute",
            human_readable_name="Без звука",
            source_text=_MUTE_GRAMMAR,
            positive_tests="приглуши\nвыключи звук\nбеззвучно",
        ),
        IntentDraft(
            form_name="control.unmute",
            human_readable_name="Вернуть звук",
            source_text=_UNMUTE_GRAMMAR,
            positive_tests="включи звук\nсделай звук\nверни звук",
        ),
        IntentDraft(
            form_name="control.seek_start",
            human_readable_name="К началу",
            source_text=_SEEK_START_GRAMMAR,
            positive_tests="к началу\nв начало\nначни заново",
        ),
        IntentDraft(
            form_name="control.repeat_one",
            human_readable_name="Повтор одной песни",
            source_text=_REPEAT_ONE_GRAMMAR,
            positive_tests="повтори песню\nповтори трек\nповтори эту песню",
        ),
        IntentDraft(
            form_name="control.repeat_all",
            human_readable_name="Повтор всего",
            source_text=_REPEAT_ALL_GRAMMAR,
            positive_tests="повтори всё\nповтори плейлист\nповтори очередь",
        ),
        IntentDraft(
            form_name="control.repeat_off",
            human_readable_name="Выключить повтор",
            source_text=_REPEAT_OFF_GRAMMAR,
            positive_tests="выключи повтор\nне повторяй\nотмени повтор",
        ),
        IntentDraft(
            form_name="control.list_players",
            human_readable_name="Какие колонки",
            source_text=_LIST_PLAYERS_GRAMMAR,
            positive_tests="сколько колонок\nкакие колонки\nперечисли колонки",
        ),
        IntentDraft(
            form_name="control.forget_player",
            human_readable_name="Забыть колонку",
            source_text=_FORGET_PLAYER_GRAMMAR,
            positive_tests="забудь колонку\nсбрось выбор\nпоменяй колонку",
        ),
        # ----- New slot-bearing intents (v1.4.0) -----------------------
        IntentDraft(
            form_name="control.volume_set",
            human_readable_name="Громкость на N процентов",
            source_text=_VOLUME_SET_GRAMMAR,
            positive_tests="громкость 50\nгромкость на 30 процентов\nсделай громкость 75",
            slots=(SlotDeclaration(name="level", type="YANDEX.NUMBER", source="$Level"),),
        ),
        IntentDraft(
            form_name="control.volume_increase",
            human_readable_name="Прибавить громкость на N",
            source_text=_VOLUME_INCREASE_GRAMMAR,
            positive_tests="прибавь на 20\nприбавь на 15 процентов\nна 10 громче",
            slots=(SlotDeclaration(name="delta", type="YANDEX.NUMBER", source="$Delta"),),
        ),
        IntentDraft(
            form_name="control.volume_decrease",
            human_readable_name="Убавить громкость на N",
            source_text=_VOLUME_DECREASE_GRAMMAR,
            positive_tests="убавь на 20\nубавь на 25 процентов\nна 15 тише",
            slots=(SlotDeclaration(name="delta", type="YANDEX.NUMBER", source="$Delta"),),
        ),
        IntentDraft(
            form_name="control.seek_forward",
            human_readable_name="Перемотать вперёд",
            source_text=_SEEK_FORWARD_GRAMMAR,
            positive_tests=(
                "перемотай вперёд на 30 секунд\nперемотай вперёд на 2 минуты\nвперёд 15 секунд"
            ),
            negative_tests="назад 30 секунд",
            slots=(
                SlotDeclaration(name="amount", type="YANDEX.NUMBER", source="$Amount"),
                SlotDeclaration(name="unit", type="time_unit", source="$Unit"),
            ),
        ),
        IntentDraft(
            form_name="control.seek_back",
            human_readable_name="Перемотать назад",
            source_text=_SEEK_BACK_GRAMMAR,
            positive_tests=(
                "перемотай назад на 30 секунд\nперемотай назад на 1 минуту\nназад 15 секунд"
            ),
            negative_tests="вперёд 30 секунд",
            slots=(
                SlotDeclaration(name="amount", type="YANDEX.NUMBER", source="$Amount"),
                SlotDeclaration(name="unit", type="time_unit", source="$Unit"),
            ),
        ),
        # ----- Play intents -------------------------------------------
        IntentDraft(
            form_name="play.my_wave",
            human_readable_name="Моя волна",
            source_text=_MY_WAVE_GRAMMAR,
            positive_tests="включи мою волну\nпоставь мою волну\nвключи моё радио",
        ),
    ]


# ---------------------------------------------------------------------------
# Runtime: map platform-classified intents back to dispatcher dataclasses
# ---------------------------------------------------------------------------

# No-slot intents map directly to a ControlAction literal. Slot-bearing
# intents are handled inline in `parse_platform_intent` because they need
# slot extraction. Keep this map in lockstep with the no-slot grammars
# above — adding a new no-slot intent here without a matching
# IntentDraft (or vice versa) would silently misclassify utterances.
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


def _slot_int(slots: dict[str, Any] | None, name: str) -> int | None:
    """Pull a YANDEX.NUMBER slot out of an ``intent.slots`` block.

    Yandex sends ``value`` as int or float depending on the spoken
    number (e.g. integer 30 vs fractional 3.5). Both are coerced to
    int. Booleans (which are technically ``isinstance(..., int)`` in
    Python) are rejected to guard against the edge case where a
    different slot type accidentally occupies the same key.
    """
    if not isinstance(slots, dict):
        return None
    slot = slots.get(name)
    if not isinstance(slot, dict):
        return None
    value = slot.get("value")
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        return int(value)
    return None


def _slot_str(slots: dict[str, Any] | None, name: str) -> str | None:
    """Pull a string-typed slot value (custom entity or YANDEX.STRING)."""
    if not isinstance(slots, dict):
        return None
    slot = slots.get(name)
    if not isinstance(slot, dict):
        return None
    value = slot.get("value")
    return value if isinstance(value, str) else None


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
#   that themselves use "на <noun>" (currently only "паузу" from
#   ``_PAUSE_GRAMMAR`` — "поставь на паузу" / "на паузу"). When a new
#   intent grammar introduces another such token, add it here.
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
    if not isinstance(nlu_intents, dict) or not nlu_intents:
        return None
    for form_name, intent in nlu_intents.items():
        slots = intent.get("slots") if isinstance(intent, dict) else None

        # No-slot control intents — map directly.
        action = _CONTROL_INTENT_MAP.get(form_name)
        if action is not None:
            return ParsedControl(action=action)

        # Slot-bearing control intents — extract slot values.
        if form_name == "control.volume_set":
            level = _slot_int(slots, "level")
            if level is not None:
                return ParsedControl(action="volume_set", value=max(0, min(100, level)))
            continue
        if form_name in ("control.volume_increase", "control.volume_decrease"):
            delta = _slot_int(slots, "delta")
            magnitude = abs(delta) if delta is not None else _DEFAULT_VOLUME_DELTA
            magnitude = max(0, min(100, magnitude))
            sign = +1 if form_name.endswith("increase") else -1
            return ParsedControl(action="volume_relative", value=sign * magnitude)
        if form_name in ("control.seek_forward", "control.seek_back"):
            amount = _slot_int(slots, "amount")
            if amount is None or amount <= 0:
                continue
            unit = _slot_str(slots, "unit") or "seconds"
            seconds = amount * (60 if unit == "minutes" else 1)
            if seconds > _MAX_SEEK_SECONDS:
                # Out-of-range — treat as a misclassification rather than
                # dispatching a multi-hour skip.
                continue
            seek_action: ControlAction = (
                "seek_forward" if form_name.endswith("forward") else "seek_back"
            )
            return ParsedControl(action=seek_action, value=seconds)

        # Play intents.
        if form_name == "play.my_wave":
            return ParsedCommand(kind="my_wave", query="", radio_mode=True)
    return None
