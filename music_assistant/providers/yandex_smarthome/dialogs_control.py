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
    "mute",
    "unmute",
    "list_players",
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


def _try_match(cleaned: str, player_hint: str | None) -> ParsedControl | None:
    """Match `cleaned` against control patterns; return ParsedControl or None."""
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
    for pattern, action in _CONTROL_PATTERNS:
        if pattern.match(cleaned):
            return ParsedControl(action=action, player_hint=player_hint)
    return None


_NA_BOUNDARY_RE = re.compile(r"\s+на\s+", re.IGNORECASE)


def parse_control(text: str) -> ParsedControl | None:
    """Classify a voice utterance as a control command, or None to fall through.

    Tries each `на`-boundary in the cleaned text as a possible
    "на <player>" suffix, starting from the rightmost. First yields
    (cleaned, None) for the whole-phrase case so that "поставь на
    паузу" still matches `pause` with no hint, even when the phrase
    contains "на" inside the action keywords.
    """
    if not text:
        return None
    cleaned = _PUNCT_RE.sub(" ", text)
    cleaned = _SPACE_RE.sub(" ", cleaned).strip()
    cleaned = re.sub(r"^алиса[,\s]+", "", cleaned, flags=re.IGNORECASE)
    if not cleaned:
        return None

    # Whole-phrase first (no hint).
    if direct := _try_match(cleaned, player_hint=None):
        return direct

    # Then try each "на " split from right to left, so e.g.
    # "поставь на паузу на кухне" splits at the *last* "на".
    matches = list(_NA_BOUNDARY_RE.finditer(cleaned))
    for m in reversed(matches):
        rest = cleaned[: m.start()].strip()
        hint = cleaned[m.end() :].strip().lower()
        if not rest or not hint:
            continue
        if matched := _try_match(rest, player_hint=hint):
            return matched
    return None


# ---------------------------------------------------------------------------
# Executor + confirmation
# ---------------------------------------------------------------------------


def _plural_ru(n: int, forms: tuple[str, str, str]) -> str:
    """Pick the correct Russian quantitative form for `n`.

    Args:
        n: The number.
        forms: ``(form_for_1, form_for_2_to_4, form_for_5_plus)``.

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


def control_confirmation(control: ParsedControl) -> str:
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
    if action == "mute":
        return "Звук выключен."
    if action == "unmute":
        return "Звук включен."
    # list_players (the only remaining action; Literal is exhaustive)
    return "Готово."  # placeholder; handler computes the real text


async def execute_control(
    mass: MusicAssistant,
    control: ParsedControl,
    player: Any,
) -> None:
    """Dispatch a ParsedControl to the matching MA command.

    Errors are logged and swallowed — Alice has already been told the
    action was accepted; we don't have a channel to surface failures
    back into the same conversation.
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
        elif action == "mute":
            await mass.players.cmd_volume_mute(pid, True)
        elif action == "unmute":
            await mass.players.cmd_volume_mute(pid, False)
    except asyncio.CancelledError:
        raise
    except Exception:
        _LOGGER.exception("execute_control(%s) failed for player %s", action, pid)
