"""Tests for provider/dialogs_nlu.py — voice command parser + player resolver."""

from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from music_assistant.providers.yandex_smarthome.dialogs_nlu import (
    ParsedCommand,
    parse_command,
    resolve_player,
)

# ---------------------------------------------------------------------------
# parse_command — table-driven
# ---------------------------------------------------------------------------


class TestParseCommand:
    """Tests for parse_command across kinds + player suffix."""

    @pytest.mark.parametrize(
        ("phrase", "expected_kind", "expected_query", "expected_hint", "expected_radio"),
        [
            # Bare/default search
            ("включи Metallica", "search", "metallica", None, False),
            ("включи джаз", "search", "джаз", None, False),
            # Track explicit
            ("включи песню Yesterday", "track", "yesterday", None, False),
            ("включи трек Imagine", "track", "imagine", None, False),
            # Album explicit
            ("включи альбом Black Album", "album", "black album", None, False),
            ("включи пластинку Дыхание", "album", "дыхание", None, False),
            # Artist explicit (radio)
            ("включи исполнителя Metallica", "artist", "metallica", None, True),
            ("включи группу Beatles", "artist", "beatles", None, True),
            # Playlist explicit
            ("включи плейлист утренний джаз", "playlist", "утренний джаз", None, False),
            ("включи подборку рок", "playlist", "рок", None, False),
            # My wave
            ("включи мою волну", "my_wave", "", None, True),
            ("включи свою волну", "my_wave", "", None, True),
            # Genre / radio
            ("включи жанр джаз", "genre", "джаз", None, True),
            ("включи радио рок", "genre", "рок", None, True),
            # With player suffix
            ("включи Metallica на кухне", "search", "metallica", "кухне", False),
            ("включи песню Yesterday на спальне", "track", "yesterday", "спальне", False),
            ("включи мою волну на кухне", "my_wave", "", "кухне", True),
            ("включи альбом Black Album на колонке", "album", "black album", "колонке", False),
            # Punctuation, casing, alice prefix
            ("Алиса, включи Metallica.", "search", "metallica", None, False),
            ("ВКЛЮЧИ ПЕСНЮ Hey Jude!", "track", "hey jude", None, False),
            # Different verbs (including включай which was previously unmatched)
            ("поставь Metallica", "search", "metallica", None, False),
            ("запусти джаз на кухне", "search", "джаз", "кухне", False),
            ("включай Metallica", "search", "metallica", None, False),
            ("включайте джаз на кухне", "search", "джаз", "кухне", False),
        ],
    )
    def test_parse(
        self,
        phrase: str,
        expected_kind: str,
        expected_query: str,
        expected_hint: str | None,
        expected_radio: bool,
    ) -> None:
        """Each parametrized phrase maps to the expected ParsedCommand fields."""
        result = parse_command(phrase)
        assert result.kind == expected_kind, f"phrase={phrase!r}"
        assert result.query == expected_query, f"phrase={phrase!r}"
        assert result.player_hint == expected_hint, f"phrase={phrase!r}"
        assert result.radio_mode == expected_radio, f"phrase={phrase!r}"

    def test_empty(self) -> None:
        """Empty input returns a search ParsedCommand with empty query."""
        assert parse_command("") == ParsedCommand(kind="search", query="")

    def test_just_alice(self) -> None:
        """Bare 'алиса' without a verb keeps the full word as query."""
        assert parse_command("алиса").query == "алиса"


# ---------------------------------------------------------------------------
# resolve_player — fixtures + cases
# ---------------------------------------------------------------------------


@dataclass
class MockPlayer:
    """Minimal player stub for resolver tests."""

    player_id: str = "p1"
    name: str = "Player"
    available: bool = True
    enabled: bool = True
    synced_to: str | None = None
    supported_features: set[str] = field(default_factory=set)


class MockPlayerController:
    """Minimal player controller stub."""

    def __init__(self, players: list[MockPlayer]) -> None:
        """Initialise with a fixed player list."""
        self._players = players

    def all_players(self) -> list[MockPlayer]:
        """Return all players."""
        return list(self._players)


@dataclass
class MockMass:
    """Minimal mass stub for NLU resolver tests."""

    players: MockPlayerController


def _mass(players: list[MockPlayer]) -> MockMass:
    return MockMass(players=MockPlayerController(players))


class TestResolvePlayer:
    """Tests for resolve_player — fuzzy name matching with Russian inflections."""

    def test_exact_lowercase_match(self) -> None:
        """Exact lowercase hint matches the player with that name."""
        players = [
            MockPlayer(player_id="p1", name="Кухня"),
            MockPlayer(player_id="p2", name="Спальня"),
        ]
        result = resolve_player(_mass(players), "кухня")
        assert result is not None
        assert result.player_id == "p1"

    def test_inflected_match(self) -> None:
        """Locative-case hint 'кухне' matches the player named 'Кухня'."""
        players = [
            MockPlayer(player_id="p1", name="Кухня"),
            MockPlayer(player_id="p2", name="Спальня"),
        ]
        result = resolve_player(_mass(players), "кухне")
        assert result is not None
        assert result.player_id == "p1"

    def test_substring_match(self) -> None:
        """Short hint matches a player whose name contains it as substring."""
        players = [
            MockPlayer(player_id="p1", name="Sendspin BT Group"),
            MockPlayer(player_id="p2", name="Lenco LS-500"),
        ]
        result = resolve_player(_mass(players), "lenco")
        assert result is not None
        assert result.player_id == "p2"

    def test_no_match_returns_none(self) -> None:
        """Unrecognised hint returns None."""
        players = [MockPlayer(player_id="p1", name="Кухня")]
        assert resolve_player(_mass(players), "гостиная") is None

    def test_skips_disabled(self) -> None:
        """Disabled players are excluded from candidates."""
        players = [MockPlayer(player_id="p1", name="Кухня", enabled=False)]
        assert resolve_player(_mass(players), "кухня") is None

    def test_skips_unavailable(self) -> None:
        """Unavailable players are excluded from candidates."""
        players = [MockPlayer(player_id="p1", name="Кухня", available=False)]
        assert resolve_player(_mass(players), "кухня") is None

    def test_skips_synced(self) -> None:
        """Players synced to another player are excluded from candidates."""
        players = [MockPlayer(player_id="p1", name="Кухня", synced_to="leader")]
        assert resolve_player(_mass(players), "кухня") is None

    def test_default_id_used_when_no_hint(self) -> None:
        """No hint falls back to default_id."""
        players = [
            MockPlayer(player_id="p1", name="Кухня"),
            MockPlayer(player_id="p2", name="Спальня"),
        ]
        result = resolve_player(_mass(players), None, default_id="p2")
        assert result is not None
        assert result.player_id == "p2"

    def test_single_player_no_hint_picked(self) -> None:
        """No hint with a single available player returns that player."""
        players = [MockPlayer(player_id="p1", name="Кухня")]
        result = resolve_player(_mass(players), None)
        assert result is not None
        assert result.player_id == "p1"

    def test_exposed_ids_filter(self) -> None:
        """Hint matching a player outside exposed_ids returns None."""
        players = [
            MockPlayer(player_id="p1", name="Кухня"),
            MockPlayer(player_id="p2", name="Спальня"),
        ]
        result = resolve_player(_mass(players), "кухня", exposed_ids={"p2"})
        assert result is None
