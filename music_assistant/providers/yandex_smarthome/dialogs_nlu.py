"""Server-side NLU parser for Yandex Dialogs custom-skill webhook commands.

The plugin's Dialogs skill registers in the Yandex Dialogs UI without
declared intents/slots — Yandex passes the raw user phrase as
``request.command``. We classify it ourselves: kind (track/artist/album/
playlist/my_wave/genre/search), search query, optional player hint.

Pure-Python; no MA-API dependency for the parser itself, only for
``resolve_player`` which iterates ``mass.players.all_players()``.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:
    from music_assistant.mass import MusicAssistant


_LOGGER = logging.getLogger(__name__)

CommandKind = Literal["track", "artist", "album", "playlist", "my_wave", "genre", "search"]


@dataclass(frozen=True, slots=True)
class ParsedCommand:
    """Result of classifying a Yandex Dialogs voice command."""

    kind: CommandKind
    query: str
    player_hint: str | None = None
    radio_mode: bool = False


# ---------------------------------------------------------------------------
# Command parser
# ---------------------------------------------------------------------------

# Punctuation we strip up-front. Keep apostrophes (e.g. "rock'n'roll") and
# hyphens (e.g. "rock-n-roll") inside words.
_PUNCT_RE = re.compile(r"[!?.,;:«»\"„“]")
_SPACE_RE = re.compile(r"\s+")

# Trailing "<player>" hint suffix introduced by the Russian preposition "на".
# The hint can be multi-word (e.g. a phrase like "in the kitchen speaker").
# Anchored to a word boundary so a name beginning with the same letters
# (e.g. "Natalie" in Russian) isn't mis-split mid-token.
_PLAYER_SUFFIX_RE = re.compile(r"\s+на\s+(?P<hint>.+?)\s*$", re.IGNORECASE)

# Verb regex covering Russian imperative + infinitive forms used to
# start playback ("turn on", "play", "launch"). Yandex's voice-to-text
# sometimes returns the infinitive ("включить") even if the user spoke
# the imperative ("включи"); we accept both. Also covers the plural
# imperatives (-те), informal aspect variants (включай/сыграй),
# and the listening verbs (послушай/послушать).
_VERB_RE = re.compile(
    r"^(?:алиса[, ]+)?(?:"
    r"включи(?:те)?|включай(?:те)?|включить|"
    r"поставь(?:те)?|поставить|"
    r"запусти(?:те)?|запустить|"
    r"сыграй(?:те)?|сыграть|"
    r"играй(?:те)?|"
    r"послушай(?:те)?|послушать|"
    r"найди(?:те)?|найти|"
    r"открой(?:те)?|открыть|"
    r"покажи(?:те)?|показать"
    r")(?:\s+|$)",
    re.IGNORECASE,
)

# Type prefixes inside the intent part. Order matters: longer keywords first.
_KIND_RULES: tuple[tuple[re.Pattern[str], CommandKind, bool], ...] = (
    # my_wave / personal radio wave — no query, the verb is everything
    (re.compile(r"^(?:мою|свою|нашу)\s+волну\b", re.IGNORECASE), "my_wave", True),
    (re.compile(r"^мо[её]\s+радио\b", re.IGNORECASE), "my_wave", True),
    # playlist
    (re.compile(r"^(?:плейлист|подборку|подборка)\s+(.+)$", re.IGNORECASE), "playlist", False),
    # album
    (re.compile(r"^(?:альбом|пластинку|пластинка)\s+(.+)$", re.IGNORECASE), "album", False),
    # artist (radio mode)
    (
        re.compile(r"^(?:исполнителя|артиста|группу|группа)\s+(.+)$", re.IGNORECASE),
        "artist",
        True,
    ),
    # explicit track marker
    (
        re.compile(r"^(?:песню|трек|композицию|композиция)\s+(.+)$", re.IGNORECASE),
        "track",
        False,
    ),
    # explicit radio
    (re.compile(r"^радио\s+(.+)$", re.IGNORECASE), "genre", True),
    # genre marker
    (re.compile(r"^жанр\s+(.+)$", re.IGNORECASE), "genre", True),
)


def parse_command(text: str) -> ParsedCommand:
    """Parse a raw voice command into a structured ParsedCommand.

    Examples:
      "включи Metallica на кухне"            → kind=search, query=metallica, hint=кухне
      "включи песню Yesterday"                → kind=track, query=yesterday
      "включи альбом Black Album на спальне"  → kind=album, query=black album, hint=спальне
      "включи исполнителя Metallica"           → kind=artist, query=metallica, radio_mode=True
      "включи мою волну"                      → kind=my_wave, query=, radio_mode=True
      "включи джаз"                           → kind=search, query=джаз
      "включи жанр джаз"                      → kind=genre, query=джаз, radio_mode=True
    """
    if not text:
        return ParsedCommand(kind="search", query="")

    cleaned = _PUNCT_RE.sub(" ", text)
    cleaned = _SPACE_RE.sub(" ", cleaned).strip()

    # Strip the "Alice, ..." vocative prefix if present
    # (Yandex usually strips it on its side, but defensively).
    cleaned = re.sub(r"^алиса[,\s]+", "", cleaned, flags=re.IGNORECASE)

    # Split off the trailing "<player>" hint suffix.
    player_hint: str | None = None
    if match := _PLAYER_SUFFIX_RE.search(cleaned):
        player_hint = match.group("hint").strip().lower()
        cleaned = cleaned[: match.start()].strip()

    # Strip the imperative verb at the start (e.g. "play this", "turn on that").
    intent_part = _VERB_RE.sub("", cleaned).strip()

    if not intent_part:
        return ParsedCommand(kind="search", query="", player_hint=player_hint)

    # Try kind rules in order.
    for pattern, kind, radio in _KIND_RULES:
        if rule_match := pattern.match(intent_part):
            query = rule_match.group(1).strip() if rule_match.groups() else ""
            return ParsedCommand(
                kind=kind,
                query=query.lower(),
                player_hint=player_hint,
                radio_mode=radio,
            )

    # Fallback: unstructured search — let mass.music.search figure out
    # the type. Force radio_mode=True so when the result is an artist or
    # a single track, MA starts a radio based on it instead of playing
    # one item and stopping (matches the typical user expectation
    # "включи <X>" → "play <X> music").
    return ParsedCommand(
        kind="search",
        query=intent_part.lower(),
        player_hint=player_hint,
        radio_mode=True,
    )


# ---------------------------------------------------------------------------
# Player resolver
# ---------------------------------------------------------------------------

# Common Russian inflection suffixes we strip for fuzzy player-name matching.
# Not a full lemmatizer — picks up the most frequent endings for short names.
# Order: longest first so multi-letter suffixes match before single-letter ones.
_INFLECTION_SUFFIXES = (
    "ого",
    "ому",
    "ыми",
    "ая",  # feminine adjective nominative — "большая", "маленькая"
    "ой",
    "ом",
    "ым",
    "ы",
    "е",
    "у",
    "а",
    "и",
    "й",
    "ь",
    "я",  # feminine noun nominative — "Кухня", "Спальня"
)


# Generic Russian words for "speaker / player" — fall through to the
# default/only-exposed player if the user said one of these instead of
# a specific player name. Stored as already-normalised stems so we can
# compare against the same normalisation we run on `hint`.
_GENERIC_PLAYER_STEMS = frozenset(
    {
        "колонк",  # колонка / на колонке / колонку
        "плеер",  # плеер / на плеере / плеера
        "пле",  # short for "плеер" after stripping the trailing -ер suffix
        "проигрыватель",  # full word survives stem (no matching suffix)
        "проигрывател",  # stripped «-ь»
        "динамик",  # динамик / на динамике
        "акустик",  # акустика / на акустике
        "устройств",  # устройство / на устройстве
    }
)


def _normalize_player_token(name: str) -> str:
    """Lowercase + strip common Russian inflection suffix.

    Applied to both haystack (player.name) and needle (hint) so they
    match each other after the same shaping.
    """
    norm = name.lower().strip()
    norm = _PUNCT_RE.sub(" ", norm)
    norm = _SPACE_RE.sub(" ", norm).strip()
    # Strip a trailing inflection suffix from each whitespace-separated token.
    parts: list[str] = []
    for token in norm.split():
        stemmed = token
        for suffix in _INFLECTION_SUFFIXES:
            if len(stemmed) > len(suffix) + 2 and stemmed.endswith(suffix):
                stemmed = stemmed[: -len(suffix)]
                break
        parts.append(stemmed)
    return " ".join(parts)


def list_exposed_players(
    mass: MusicAssistant,
    *,
    exposed_ids: set[str] | None = None,
) -> list[Any]:
    """Return all available, enabled, non-synced players (filtered by exposure).

    Same filter as ``resolve_player_candidates`` uses for its candidate set,
    extracted so the dialog handler can answer "what speakers do you see?"
    queries (P0.6 ``list_players`` action) without re-implementing it.
    """
    out: list[Any] = []
    for player in mass.players.all_players():
        if not player.available or not player.enabled:
            continue
        if getattr(player, "synced_to", None):
            continue
        if exposed_ids and player.player_id not in exposed_ids:
            continue
        out.append(player)
    return out


def resolve_player_candidates(
    mass: MusicAssistant,
    hint: str | None,
    *,
    default_id: str | None = None,
    exposed_ids: set[str] | None = None,
) -> list[Any]:
    """Return the best-matching tier of players for ``hint``.

    Filters: only players that are available, enabled, and not synced to
    a leader. Optional ``exposed_ids`` further restricts to the user's
    exposed-players list. Tier priority: exact → startswith → contains →
    generic-word fallback. The caller decides what to do with multiple
    matches (typically: ask the user to disambiguate).

    Logs a single DEBUG-level summary on every call describing the
    decision: chosen tier, candidate count, and the names of the
    candidates returned.

    Returns:
        A list with all players in the best non-empty tier. ``[]`` if
        nothing matched. ``[player]`` for an unambiguous resolution.
    """
    candidates = list_exposed_players(mass, exposed_ids=exposed_ids)

    def _label(p: Any) -> str:
        return str(getattr(p, "name", None) or p.player_id)

    def _result(result: list[Any], reason: str) -> list[Any]:
        _LOGGER.debug(
            "resolve_player: hint=%r default=%s exposed=%d -> %d candidate(s) %s [%s]",
            hint,
            default_id,
            len(candidates),
            len(result),
            [_label(p) for p in result],
            reason,
        )
        return result

    if not candidates:
        return _result([], "no exposed players")

    # Single-player install or no hint → default / only candidate.
    if not hint:
        if default_id:
            for p in candidates:
                if p.player_id == default_id:
                    return _result([p], "no hint, matched default_id")
        if len(candidates) == 1:
            return _result(candidates[:], "no hint, single exposed player")
        return _result([], "no hint, ambiguous")

    needle = _normalize_player_token(hint)
    if not needle:
        return _result([], "hint normalised to empty string")

    exact: list[Any] = []
    startswith: list[Any] = []
    contains: list[Any] = []
    haystacks: list[tuple[str, str]] = []  # (raw, normalised) for debug
    for p in candidates:
        haystack = _normalize_player_token(p.name or p.player_id)
        haystacks.append((p.name or p.player_id, haystack))
        if not haystack:
            continue
        if haystack == needle:
            exact.append(p)
        elif haystack.startswith(needle) or needle.startswith(haystack):
            startswith.append(p)
        elif needle in haystack or haystack in needle:
            contains.append(p)

    _LOGGER.debug(
        "resolve_player tiers: hint=%r needle=%r candidates=%s "
        "matches: exact=%d startswith=%d contains=%d",
        hint,
        needle,
        haystacks,
        len(exact),
        len(startswith),
        len(contains),
    )

    for tier_name, tier in (
        ("exact", exact),
        ("startswith", startswith),
        ("contains", contains),
    ):
        if tier:
            tier.sort(key=lambda p: (p.name or p.player_id).lower())
            return _result(tier, f"tier={tier_name}")

    # Generic-word fallback: "на колонке" / "на проигрывателе" / "на динамике"
    # mean "any speaker" — resolve unambiguously only when the choice is
    # forced (default_id set, or single exposed player).
    if any(stem in needle for stem in _GENERIC_PLAYER_STEMS):
        if default_id:
            for p in candidates:
                if p.player_id == default_id:
                    _LOGGER.info(
                        "Generic player hint %r → resolved to default player %r",
                        hint,
                        p.name,
                    )
                    return _result([p], "generic word, matched default_id")
        if len(candidates) == 1:
            _LOGGER.info(
                "Generic player hint %r → resolved to the only exposed player %r",
                hint,
                candidates[0].name,
            )
            return _result(candidates[:], "generic word, single exposed player")
        _LOGGER.warning(
            "Generic player hint %r matches no specific player and there are "
            "%d exposed players — caller will ask for clarification",
            hint,
            len(candidates),
        )
        return _result([], "generic word, multiple players, no default")

    return _result([], "no tier matched")


def resolve_player(
    mass: MusicAssistant,
    hint: str | None,
    *,
    default_id: str | None = None,
    exposed_ids: set[str] | None = None,
) -> Any:
    """Find an unambiguously-matching MA player for ``hint``, or None.

    Thin wrapper over ``resolve_player_candidates`` — returns the single
    candidate when exactly one matches, ``None`` otherwise (zero matches
    or ambiguous). Use ``resolve_player_candidates`` directly when you
    want to surface the ambiguity to the user.
    """
    candidates = resolve_player_candidates(
        mass, hint, default_id=default_id, exposed_ids=exposed_ids
    )
    return candidates[0] if len(candidates) == 1 else None
