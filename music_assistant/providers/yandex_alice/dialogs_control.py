# ruff: noqa: RUF001
"""Playback-control NLU + executor for the Yandex Dialogs custom skill.

Handles utterances that don't carry a music query — pause/resume/next/
previous/volume up-down-set/mute/unmute. Runs *before* the play-command
parser in the webhook flow; if `parse_control` returns None the handler
falls through to the existing music-search path.
"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

from music_assistant_models.enums import RepeatMode

from .dialogs_nlu import _PUNCT_RE, _SPACE_RE

if TYPE_CHECKING:
    from music_assistant.mass import MusicAssistant


_LOGGER = logging.getLogger(__name__)

ControlAction = Literal[
    "pause",
    "resume",
    "stop",
    "next",
    "previous",
    "volume_up",
    "volume_down",
    "volume_set",
    "volume_relative",  # value = signed delta (+20, -5); executor reads current vol + clamps
    "mute",
    "unmute",
    "list_players",
    "forget_player",
    # v1.9.0 — six new actions
    "now_playing",  # info — handler reads queue.current_item.name
    "shuffle_on",
    "shuffle_off",
    "repeat_off",
    "repeat_one",
    "repeat_all",
    "seek_forward",  # value = positive seconds
    "seek_back",  # value = positive seconds; negated when dispatched
    "seek_start",  # absolute seek to 0
    "transfer",  # player_hint = TARGET player; SOURCE is the saved default
]


@dataclass(frozen=True, slots=True)
class ParsedControl:
    """Result of classifying a Yandex Dialogs voice command as a control action."""

    action: ControlAction
    value: int | None = None
    player_hint: str | None = None


# Pattern catalogue. Order matters within each tier — first match wins.
# All patterns are anchored (^...$) to require a whole-phrase match.
_CONTROL_PATTERNS: tuple[tuple[re.Pattern[str], ControlAction], ...] = (
    # list_players — informational query "what speakers do you see?".
    # Matched before the play-verb-strip can interpret "покажи колонки"
    # as a play kind=search query="колонки".
    (
        re.compile(
            r"^сколько\s+колонок(?:\s+(?:ты\s+)?(?:видишь|знаешь))?$",
            re.IGNORECASE,
        ),
        "list_players",
    ),
    (
        re.compile(
            r"^какие\s+колонки(?:\s+(?:ты\s+)?(?:видишь|знаешь|есть))?$",
            re.IGNORECASE,
        ),
        "list_players",
    ),
    (re.compile(r"^какие\s+у\s+тебя\s+колонки$", re.IGNORECASE), "list_players"),
    (re.compile(r"^перечисли\s+колонки$", re.IGNORECASE), "list_players"),
    (re.compile(r"^список\s+колонок$", re.IGNORECASE), "list_players"),
    (re.compile(r"^покажи\s+колонки$", re.IGNORECASE), "list_players"),
    (re.compile(r"^назови\s+колонки$", re.IGNORECASE), "list_players"),
    # forget_player — clears the saved "default player" so the next
    # play command without an explicit hint asks again. Useful when
    # the user previously picked a player and now wants to change
    # without re-stating the name on every turn.
    (re.compile(r"^забудь\s+колонку$", re.IGNORECASE), "forget_player"),
    (re.compile(r"^сбрось\s+колонку$", re.IGNORECASE), "forget_player"),
    (re.compile(r"^забудь\s+плеер$", re.IGNORECASE), "forget_player"),
    (re.compile(r"^забудь\s+выбор$", re.IGNORECASE), "forget_player"),
    (re.compile(r"^сбрось\s+выбор$", re.IGNORECASE), "forget_player"),
    (re.compile(r"^выбери\s+колонку\s+заново$", re.IGNORECASE), "forget_player"),
    (re.compile(r"^поменяй\s+колонку$", re.IGNORECASE), "forget_player"),
    (re.compile(r"^сменить\s+колонку$", re.IGNORECASE), "forget_player"),
    # now_playing — info query about the current track (no MA mutation)
    (re.compile(r"^что\s+(?:сейчас\s+)?играет$", re.IGNORECASE), "now_playing"),
    (re.compile(r"^что\s+(?:мы\s+)?слушаем$", re.IGNORECASE), "now_playing"),
    (re.compile(r"^что\s+за\s+(?:песня|трек|композиция)$", re.IGNORECASE), "now_playing"),
    (re.compile(r"^какой\s+(?:сейчас\s+)?(?:трек|играет)$", re.IGNORECASE), "now_playing"),
    # shuffle_on / shuffle_off
    (re.compile(r"^перемешай$", re.IGNORECASE), "shuffle_on"),
    (re.compile(r"^включи\s+перемешивание$", re.IGNORECASE), "shuffle_on"),
    (re.compile(r"^случайный\s+порядок$", re.IGNORECASE), "shuffle_on"),
    (re.compile(r"^в\s+случайном\s+порядке$", re.IGNORECASE), "shuffle_on"),
    (re.compile(r"^выключи\s+перемешивание$", re.IGNORECASE), "shuffle_off"),
    (re.compile(r"^не\s+перемешивай$", re.IGNORECASE), "shuffle_off"),
    (re.compile(r"^по\s+порядку$", re.IGNORECASE), "shuffle_off"),
    # repeat — order matters: more-specific (with object) first, then bare verbs
    (
        re.compile(
            r"^повтор(?:и)?\s+(?:песн[июя]|трек(?:а)?|композицию|композиция|эту|эту\s+песню)$",
            re.IGNORECASE,
        ),
        "repeat_one",
    ),
    (
        re.compile(
            r"^повтор(?:и)?\s+(?:всё|все|очередь|плейлист|список)$",
            re.IGNORECASE,
        ),
        "repeat_all",
    ),
    (re.compile(r"^повторяй$", re.IGNORECASE), "repeat_all"),
    (re.compile(r"^включи\s+повтор$", re.IGNORECASE), "repeat_all"),
    (re.compile(r"^выключи\s+повтор$", re.IGNORECASE), "repeat_off"),
    (re.compile(r"^не\s+повторяй$", re.IGNORECASE), "repeat_off"),
    # seek_start — absolute seek to position 0 (start of current track)
    (re.compile(r"^(?:перемотай\s+)?к\s+началу$", re.IGNORECASE), "seek_start"),
    (re.compile(r"^(?:перемотай\s+)?в\s+начало$", re.IGNORECASE), "seek_start"),
    (re.compile(r"^начни\s+(?:трек\s+)?заново$", re.IGNORECASE), "seek_start"),
    # mute / unmute — explicit "звук" disambiguates from play-verb "включи"
    (re.compile(r"^включи\s+звук$", re.IGNORECASE), "unmute"),
    (re.compile(r"^сделай\s+звук$", re.IGNORECASE), "unmute"),
    (re.compile(r"^приглуши$", re.IGNORECASE), "mute"),
    (re.compile(r"^выключи\s+звук$", re.IGNORECASE), "mute"),
    (re.compile(r"^беззвучно$", re.IGNORECASE), "mute"),
    # resume — must come before "включи" play-verb stripping; we run before
    # parse_command anyway, but match anchored phrases here for clarity
    (re.compile(r"^продолжи(?:ть)?$", re.IGNORECASE), "resume"),
    (re.compile(r"^включи\s+снова$", re.IGNORECASE), "resume"),
    (re.compile(r"^возобнови(?:ть)?$", re.IGNORECASE), "resume"),
    # pause
    (re.compile(r"^пауза$", re.IGNORECASE), "pause"),
    (re.compile(r"^на\s+паузу$", re.IGNORECASE), "pause"),
    (re.compile(r"^поставь\s+на\s+паузу$", re.IGNORECASE), "pause"),
    (re.compile(r"^останови\s+музыку$", re.IGNORECASE), "pause"),
    # stop — bare "выключи" maps to stop (safer than power-off)
    (re.compile(r"^стоп$", re.IGNORECASE), "stop"),
    (re.compile(r"^останови$", re.IGNORECASE), "stop"),
    (re.compile(r"^выключи$", re.IGNORECASE), "stop"),
    (re.compile(r"^выключи\s+музыку$", re.IGNORECASE), "stop"),
    # next track
    (re.compile(r"^следующ(?:ая|ий|ее)?(?:\s+трек)?$", re.IGNORECASE), "next"),
    (re.compile(r"^дальше$", re.IGNORECASE), "next"),
    (re.compile(r"^переключи$", re.IGNORECASE), "next"),
    # previous track
    (re.compile(r"^предыдущ(?:ая|ий|ее)?(?:\s+трек)?$", re.IGNORECASE), "previous"),
    (re.compile(r"^назад$", re.IGNORECASE), "previous"),
    (re.compile(r"^верни(?:сь)?$", re.IGNORECASE), "previous"),
    # volume up
    (re.compile(r"^громче$", re.IGNORECASE), "volume_up"),
    (re.compile(r"^сделай\s+громче$", re.IGNORECASE), "volume_up"),
    (re.compile(r"^прибавь(?:\s+громкость)?$", re.IGNORECASE), "volume_up"),
    # volume down
    (re.compile(r"^тише$", re.IGNORECASE), "volume_down"),
    (re.compile(r"^сделай\s+тише$", re.IGNORECASE), "volume_down"),
    (re.compile(r"^убавь(?:\s+громкость)?$", re.IGNORECASE), "volume_down"),
)

# Volume-set with explicit number, e.g. "громкость 50", "громкость на 30 процентов".
_VOLUME_SET_RE = re.compile(
    r"^(?:сделай\s+)?громкост(?:ь|и)\s+(?:на\s+)?(?P<n>\d{1,3})(?:\s+процентов)?$",
    re.IGNORECASE,
)

# Relative-volume phrasings without the keyword "громкость". The verb is
# matched even when no digit is captured — the digit slot is filled from
# the regex group OR from `request.nlu.entities[YANDEX.NUMBER]` (passed
# in via `parse_control(text, entities=...)`). When neither yields a
# number, these patterns intentionally fall through to the bare
# "прибавь"/"убавь" → volume_up/volume_down rules in `_CONTROL_PATTERNS`.
# Yandex normalises spelled-out numbers in `request.command` (тридцать → 30)
# so the regex covers most phrasings; the entity is the defensive fallback.
_VOLUME_INC_RE = re.compile(
    r"^(?:сделай\s+)?(?:прибавь(?:те)?|прибавить)"
    r"(?:\s+(?:на\s+)?(?P<n>\d{1,3})(?:\s+процентов)?)?$",
    re.IGNORECASE,
)
_VOLUME_DEC_RE = re.compile(
    r"^(?:сделай\s+)?(?:убавь(?:те)?|убавить)"
    r"(?:\s+(?:на\s+)?(?P<n>\d{1,3})(?:\s+процентов)?)?$",
    re.IGNORECASE,
)
_VOLUME_NUM_INC_RE = re.compile(
    r"^на\s+(?P<n>\d{1,3})\s+(?:громче|погромче)$",
    re.IGNORECASE,
)
_VOLUME_NUM_DEC_RE = re.compile(
    r"^на\s+(?P<n>\d{1,3})\s+(?:тише|потише)$",
    re.IGNORECASE,
)


def _yandex_number(entities: list[Any] | None) -> int | None:
    """Return the first integer YANDEX.NUMBER value from request entities, or None.

    Yandex's normalised `request.command` already converts most spelled-out
    Russian numbers to digits, but the entity is the authoritative fallback
    for phrasings the regex didn't anchor on a digit position. The list
    type is `list[Any]` because the values come from network JSON and we
    defend against mixed-type elements.
    """
    if not entities:
        return None
    for ent in entities:
        if not isinstance(ent, dict):
            continue
        if ent.get("type") != "YANDEX.NUMBER":
            continue
        value = ent.get("value")
        if isinstance(value, bool):  # bool is a subclass of int — exclude it
            continue
        if isinstance(value, (int, float)):
            return int(value)
    return None


# Seek forward / backward with numeric amount + optional unit. Unit defaults
# to seconds when missing. "Минут[уы]" multiplies by 60.
_SEEK_FORWARD_RE = re.compile(
    r"^(?:перемотай\s+|перемотать\s+|промотай\s+)?"
    r"(?:вперёд|вперед)\s+(?:на\s+)?(?P<n>\d{1,4})"
    r"(?:\s+(?P<unit>сек(?:унд[уы]?)?|мин(?:ут[уы]?)?))?$",
    re.IGNORECASE,
)
_SEEK_BACK_RE = re.compile(
    r"^(?:перемотай\s+|перемотать\s+|промотай\s+)?"
    r"назад\s+(?:на\s+)?(?P<n>\d{1,4})"
    r"(?:\s+(?P<unit>сек(?:унд[уы]?)?|мин(?:ут[уы]?)?))?$",
    re.IGNORECASE,
)

# Transfer playback to a target player. The target name is captured into
# `player_hint`; SOURCE comes from the caller's `default_id`.
_TRANSFER_RE = re.compile(
    r"^(?:переведи|перенеси|продолжи)\s+(?:музыку\s+)?(?:на|в)\s+(?P<target>.+)$",
    re.IGNORECASE,
)


def _seek_seconds(match: re.Match[str]) -> int | None:
    """Parse the digit + optional unit out of a seek-pattern match."""
    try:
        n = int(match.group("n"))
    except (TypeError, ValueError):
        return None
    unit = (match.group("unit") or "").lower()
    if unit.startswith("мин"):
        n *= 60
    return n


def _try_match(
    cleaned: str,
    player_hint: str | None,
    entities: list[Any] | None = None,
) -> ParsedControl | None:
    """Match `cleaned` against control patterns; return ParsedControl or None.

    `entities` is `request.nlu.entities` from the Yandex envelope. When a
    relative-volume verb matches without a captured digit, we fall back to
    `YANDEX.NUMBER` from there before deciding to surface `volume_relative`.
    """
    if not cleaned:
        return None
    if vmatch := _VOLUME_SET_RE.match(cleaned):
        try:
            value = int(vmatch.group("n"))
        except (TypeError, ValueError):
            return None
        return ParsedControl(
            action="volume_set",
            value=max(0, min(100, value)),
            player_hint=player_hint,
        )
    # Relative-volume — try INCREASE forms ("прибавь N", "на N громче"),
    # then DECREASE ("убавь N", "на N тише"). When the verb matches but
    # the digit slot is empty, fall back to YANDEX.NUMBER from the
    # request envelope. If neither yields a number, return None so the
    # bare-verb fallthrough in `_CONTROL_PATTERNS` handles "прибавь" /
    # "убавь" as volume_up / volume_down.
    for pattern, sign in (
        (_VOLUME_INC_RE, +1),
        (_VOLUME_NUM_INC_RE, +1),
        (_VOLUME_DEC_RE, -1),
        (_VOLUME_NUM_DEC_RE, -1),
    ):
        if rel_match := pattern.match(cleaned):
            n: int | None = None
            try:
                raw = rel_match.group("n")
                if raw is not None:
                    n = int(raw)
            except (IndexError, TypeError, ValueError):
                n = None
            if n is None:
                n = _yandex_number(entities)
            if n is not None:
                # Clamp the magnitude so an absurd "прибавь на 999" doesn't
                # underflow/overflow downstream arithmetic. ``0`` stays
                # ``0`` — "прибавь на 0" is a valid (if pointless) no-op
                # rather than the user's spoken zero being silently
                # promoted to one.
                magnitude = max(0, min(100, abs(n)))
                return ParsedControl(
                    action="volume_relative",
                    value=sign * magnitude,
                    player_hint=player_hint,
                )
    if smatch := _SEEK_FORWARD_RE.match(cleaned):
        seconds = _seek_seconds(smatch)
        if seconds is not None:
            return ParsedControl(action="seek_forward", value=seconds, player_hint=player_hint)
    if smatch := _SEEK_BACK_RE.match(cleaned):
        seconds = _seek_seconds(smatch)
        if seconds is not None:
            return ParsedControl(action="seek_back", value=seconds, player_hint=player_hint)
    if tmatch := _TRANSFER_RE.match(cleaned):
        # For transfer, the captured group goes into `player_hint` —
        # it's the TARGET. The handler resolves it; SOURCE is `default_id`.
        # `player_hint` from the caller's "на <X>" suffix split is
        # ignored here (transfer phrases already include the target).
        return ParsedControl(
            action="transfer",
            player_hint=tmatch.group("target").strip().lower(),
        )
    for pattern, action in _CONTROL_PATTERNS:
        if pattern.match(cleaned):
            return ParsedControl(action=action, player_hint=player_hint)
    return None


_NA_BOUNDARY_RE = re.compile(r"\s+на\s+", re.IGNORECASE)


def parse_control(
    text: str,
    entities: list[Any] | None = None,
) -> ParsedControl | None:
    """Classify a voice utterance as a control command, or None to fall through.

    Tries each `на`-boundary in the cleaned text as a possible
    "на <player>" suffix, starting from the rightmost. First yields
    (cleaned, None) for the whole-phrase case so that "поставь на
    паузу" still matches `pause` with no hint, even when the phrase
    contains "на" inside the action keywords.

    `entities` is the (optional) `request.nlu.entities` array from the
    Yandex Dialogs envelope, used as a fallback source for `YANDEX.NUMBER`
    when a relative-volume verb matched without a captured digit.
    """
    if not text:
        return None
    cleaned = _PUNCT_RE.sub(" ", text)
    cleaned = _SPACE_RE.sub(" ", cleaned).strip()
    cleaned = re.sub(r"^алиса[,\s]+", "", cleaned, flags=re.IGNORECASE)
    if not cleaned:
        return None

    # Whole-phrase first (no hint).
    if direct := _try_match(cleaned, player_hint=None, entities=entities):
        return direct

    # Then try each "на " split from right to left, so e.g.
    # "поставь на паузу на кухне" splits at the *last* "на".
    matches = list(_NA_BOUNDARY_RE.finditer(cleaned))
    for m in reversed(matches):
        rest = cleaned[: m.start()].strip()
        hint = cleaned[m.end() :].strip().lower()
        if not rest or not hint:
            continue
        if matched := _try_match(rest, player_hint=hint, entities=entities):
            return matched
    return None


# ---------------------------------------------------------------------------
# Executor + confirmation
# ---------------------------------------------------------------------------


def _plural_ru(n: int, forms: tuple[str, str, str]) -> str:
    """Pick the correct Russian quantitative form for `n`.

    :param n: The number.
    :param forms: ``(form_for_1, form_for_2_to_4, form_for_5_plus)``.

    Russian quantitative agreement:
      1, 21, 31, … → form_for_1 (e.g. "колонку")
      2-4, 22-24, … → form_for_2_to_4 ("колонки")
      0, 5-20, 25-30, … → form_for_5_plus ("колонок")
    """
    n_abs = abs(n)
    if n_abs % 10 == 1 and n_abs % 100 != 11:
        return forms[0]
    if 2 <= n_abs % 10 <= 4 and not 12 <= n_abs % 100 <= 14:
        return forms[1]
    return forms[2]


def format_list_players(players: list[Any]) -> str:
    """Build the spoken response listing exposed players for `list_players` action."""
    n = len(players)
    if n == 0:
        return "Не вижу ни одной колонки."
    names = ", ".join(getattr(p, "name", None) or p.player_id for p in players)
    if n == 1:
        return f"Вижу одну колонку: {names}."
    word = _plural_ru(n, ("колонку", "колонки", "колонок"))
    return f"Вижу {n} {word}: {names}."


def control_confirmation(control: ParsedControl) -> str:  # noqa: PLR0911
    """User-facing confirmation text for a control action.

    Caveat: ``list_players`` is **not** confirmed here — the handler builds
    the response text from the live player list via ``format_list_players``.
    """
    action = control.action
    if action == "pause":
        return "Пауза."
    if action == "resume":
        return "Продолжаю."
    if action == "stop":
        return "Остановил."
    if action == "next":
        return "Следующая."
    if action == "previous":
        return "Предыдущая."
    if action == "volume_up":
        return "Громче."
    if action == "volume_down":
        return "Тише."
    if action == "volume_set":
        return f"Громкость {control.value}."
    if action == "volume_relative":
        delta = control.value or 0
        if delta > 0:
            return f"Громче на {delta}."
        if delta < 0:
            return f"Тише на {-delta}."
        return "Готово."
    if action == "mute":
        return "Звук выключен."
    if action == "unmute":
        return "Звук включен."
    if action == "forget_player":
        return "Хорошо, забыл колонку. В следующий раз спрошу."
    if action == "shuffle_on":
        return "Включил перемешивание."
    if action == "shuffle_off":
        return "Выключил перемешивание."
    if action == "repeat_off":
        return "Выключил повтор."
    if action == "repeat_one":
        return "Повтор песни."
    if action == "repeat_all":
        return "Повтор очереди."
    if action == "seek_forward":
        return f"Перемотал на {control.value} секунд вперёд."
    if action == "seek_back":
        return f"Перемотал на {control.value} секунд назад."
    if action == "seek_start":
        return "Перемотал к началу."
    # list_players / now_playing / transfer — handler computes the real
    # text (live data) and never calls this. Placeholder for safety.
    return "Готово."


async def execute_control(  # noqa: PLR0915
    mass: MusicAssistant,
    control: ParsedControl,
    player: Any,
) -> None:
    """Dispatch a ParsedControl to the matching MA command.

    Errors are logged and swallowed — Alice has already been told the
    action was accepted; we don't have a channel to surface failures
    back into the same conversation.

    Note: ``list_players`` is a member of ``ControlAction`` for typing
    convenience, but it's an *informational* query handled inline by
    ``DialogsWebhookHandler._handle_control`` (which never calls this
    function for it). The explicit branch below makes that contract
    safe — a stray call won't silently no-op, it logs and returns.
    """
    pid = player.player_id
    action = control.action
    try:
        if action == "pause":
            await mass.player_queues.pause(pid)
        elif action == "resume":
            await mass.player_queues.resume(pid)
        elif action == "stop":
            await mass.player_queues.stop(pid)
        elif action == "next":
            await mass.player_queues.next(pid)
        elif action == "previous":
            await mass.player_queues.previous(pid)
        elif action == "volume_up":
            await mass.players.cmd_volume_up(pid)
        elif action == "volume_down":
            await mass.players.cmd_volume_down(pid)
        elif action == "volume_set":
            value = max(0, min(100, control.value or 0))
            await mass.players.cmd_volume_set(pid, value)
        elif action == "volume_relative":
            # Read current volume, apply signed delta, clamp [0, 100].
            # Falls back to 50 if the player exposes no volume_level
            # (some virtual players do); the user feedback ("Громче на 20")
            # then becomes a no-op rather than mis-targeting.
            delta = control.value or 0
            current = getattr(player, "volume_level", None)
            if not isinstance(current, (int, float)):
                current = 50
            new_value = max(0, min(100, int(current) + int(delta)))
            await mass.players.cmd_volume_set(pid, new_value)
        elif action == "mute":
            await mass.players.cmd_volume_mute(pid, True)
        elif action == "unmute":
            await mass.players.cmd_volume_mute(pid, False)
        elif action == "list_players":
            # Informational query — the handler builds the response
            # text from a live `list_exposed_players(...)` call and
            # never dispatches here. If we somehow got called for
            # this action it's a caller bug, not something to silently
            # ignore.
            _LOGGER.warning(
                "execute_control called with action='list_players'; "
                "this is informational and should be handled by the "
                "webhook handler, not dispatched here. Skipping.",
            )
        elif action == "forget_player":
            # State-management query — the handler clears the cached
            # default-player from session/application/cache state and
            # never dispatches here. Defensive branch.
            _LOGGER.warning(
                "execute_control called with action='forget_player'; "
                "this is a state-management op handled by the webhook "
                "handler, not dispatched here. Skipping.",
            )
        elif action == "shuffle_on":
            await mass.player_queues.set_shuffle(pid, shuffle_enabled=True)
        elif action == "shuffle_off":
            await mass.player_queues.set_shuffle(pid, shuffle_enabled=False)
        elif action == "repeat_off":
            # NB: set_repeat is sync, not async — do NOT await.
            mass.player_queues.set_repeat(pid, RepeatMode.OFF)
        elif action == "repeat_one":
            mass.player_queues.set_repeat(pid, RepeatMode.ONE)
        elif action == "repeat_all":
            mass.player_queues.set_repeat(pid, RepeatMode.ALL)
        elif action == "seek_forward":
            await mass.player_queues.skip(pid, seconds=control.value or 0)
        elif action == "seek_back":
            await mass.player_queues.skip(pid, seconds=-(control.value or 0))
        elif action == "seek_start":
            await mass.player_queues.seek(pid, position=0)
        elif action in ("now_playing", "transfer"):
            # Live-data / multi-player actions — the handler builds the
            # response from queue.current_item / transfer_queue and
            # never dispatches here. Defensive branch.
            _LOGGER.warning(
                "execute_control called with action=%r — handled by webhook "
                "handler, not dispatched here. Skipping.",
                action,
            )
    except asyncio.CancelledError:
        raise
    except Exception:
        _LOGGER.exception("execute_control(%s) failed for player %s", action, pid)
