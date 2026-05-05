# ruff: noqa: RUF001, RUF002
"""Tests for provider/dialogs.py — webhook handler."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp.test_utils import make_mocked_request

from music_assistant.providers.yandex_smarthome.dialogs import DialogsWebhookHandler, _tts_for

if TYPE_CHECKING:
    from aiohttp import web


@dataclass
class MockPlayer:
    """Minimal player stub for webhook handler tests."""

    player_id: str = "p1"
    name: str = "Кухня"
    available: bool = True
    enabled: bool = True
    synced_to: str | None = None
    supported_features: set[str] = field(default_factory=set)
    powered: bool = True


class _MockPlayers:
    def __init__(self, players: list[MockPlayer]) -> None:
        """Initialise with a fixed player list."""
        self._players = players
        self.cmd_power = AsyncMock()

    def all_players(self) -> list[MockPlayer]:
        """Return all players."""
        return list(self._players)

    def get_player(self, player_id: str) -> MockPlayer | None:
        """Return player by id or None."""
        return next((p for p in self._players if p.player_id == player_id), None)


def _make_mass(players: list[MockPlayer], search_track: object = None) -> MagicMock:
    mass = MagicMock()
    mass.players = _MockPlayers(players)
    mass.music = MagicMock()

    @dataclass
    class _SearchResults:
        artists: list[object] = field(default_factory=list)
        albums: list[object] = field(default_factory=list)
        tracks: list[object] = field(default_factory=list)
        playlists: list[object] = field(default_factory=list)

    if search_track is not None:
        mass.music.search = AsyncMock(return_value=_SearchResults(tracks=[search_track]))
    else:
        mass.music.search = AsyncMock(return_value=_SearchResults())

    mass.music_providers = []
    mass.providers = []
    mass.player_queues = MagicMock()
    mass.player_queues.play_media = AsyncMock()
    mass.webserver = MagicMock()
    mass.webserver.register_dynamic_route = MagicMock(return_value=lambda: None)
    # mass.create_task must actually schedule the coroutine so fire-and-forget
    # tasks run when the test awaits asyncio.sleep(0).
    mass.create_task = lambda coro, **_kw: asyncio.ensure_future(coro)
    return mass


_TEST_SECRET = "topsecret"


def _build_request(body: dict[str, Any], secret: str = _TEST_SECRET) -> web.Request:
    """Build a mocked aiohttp Request that returns the given JSON body."""
    req = make_mocked_request(
        "POST",
        f"/api/yandex_dialogs/webhook/{secret}",
        match_info={"secret": secret},
    )
    req.json = AsyncMock(return_value=body)  # type: ignore[method-assign]
    return req


def _response_body(resp: web.Response) -> dict[str, Any]:
    """Decode a web.json_response body into a dict for assertions."""
    decoded: dict[str, Any] = json.loads(resp.body)  # type: ignore[arg-type]
    return decoded


@pytest.mark.asyncio
class TestDialogsWebhookHandler:
    """End-to-end tests for the webhook entry point."""

    def _make_handler(self, mass: MagicMock, **kwargs: object) -> DialogsWebhookHandler:
        """Build a handler with sensible test defaults."""
        return DialogsWebhookHandler(
            mass,
            skill_id=str(kwargs.get("skill_id", "skill-uuid-1")),
            webhook_secret=str(kwargs.get("webhook_secret", "topsecret")),
            exposed_player_ids=kwargs.get("exposed_player_ids"),  # type: ignore[arg-type]
        )

    async def test_register_routes_calls_mass_webserver(self) -> None:
        """register_routes calls register_dynamic_route with the correct URL."""
        mass = _make_mass([])
        handler = self._make_handler(mass)
        handler.register_routes()
        mass.webserver.register_dynamic_route.assert_called_once()
        path_arg = mass.webserver.register_dynamic_route.call_args[0][0]
        assert path_arg == "/api/yandex_dialogs/webhook/topsecret"

    async def test_unregister_routes(self) -> None:
        """unregister_routes calls the unregister callback returned by register_dynamic_route."""
        mass = _make_mass([])
        unregister = MagicMock()
        mass.webserver.register_dynamic_route = MagicMock(return_value=unregister)
        handler = self._make_handler(mass)
        handler.register_routes()
        handler.unregister_routes()
        unregister.assert_called_once()

    async def test_secret_mismatch_returns_404(self) -> None:
        """Webhook request with wrong URL secret is rejected with 404."""
        mass = _make_mass([])
        handler = self._make_handler(mass)
        body = {
            "session": {"skill_id": "skill-uuid-1", "session_id": "s1"},
            "request": {"command": "включи Metallica"},
        }
        req = make_mocked_request(
            "POST",
            "/api/yandex_dialogs/webhook/wrong",
            match_info={"secret": "wrong"},
        )
        req.json = AsyncMock(return_value=body)  # type: ignore[method-assign]
        resp = await handler._handle_webhook(req)
        assert resp.status == 404

    async def test_secret_parsed_from_path_when_no_match_info(self) -> None:
        """Cover the production secret-from-path fallback in `_handle_webhook`.

        Production registers an exact route (no `{secret}` variable), so
        `request.match_info` is empty and the handler parses the secret
        from `request.path`. This test passes `match_info={}` to exercise
        that branch.
        """
        track = MagicMock(uri="library://track/123", spec_set=["uri"])
        mass = _make_mass([MockPlayer(player_id="p1", name="Кухня")], search_track=track)
        handler = self._make_handler(mass)
        body = {
            "session": {"skill_id": "skill-uuid-1", "session_id": "s1", "new": False},
            "request": {"command": "включи Metallica на кухне"},
        }
        req = make_mocked_request(
            "POST",
            f"/api/yandex_dialogs/webhook/{_TEST_SECRET}",
            match_info={},
        )
        req.json = AsyncMock(return_value=body)  # type: ignore[method-assign]
        resp = await handler._handle_webhook(req)
        # If path parsing works, secret matches and we reach the play branch (200).
        assert resp.status == 200

    async def test_skill_id_mismatch_returns_401(self) -> None:
        """Payload with wrong skill_id is rejected with 401."""
        mass = _make_mass([])
        handler = self._make_handler(mass)
        body = {
            "session": {"skill_id": "different-skill", "session_id": "s1"},
            "request": {"command": "включи Metallica"},
        }
        resp = await handler._handle_webhook(_build_request(body))
        assert resp.status == 401

    async def test_session_new_empty_command_greets(self) -> None:
        """New session with empty command returns 200 greeting without playing."""
        mass = _make_mass([])
        handler = self._make_handler(mass)
        body = {
            "session": {"skill_id": "skill-uuid-1", "session_id": "s1", "new": True},
            "request": {"command": ""},
        }
        resp = await handler._handle_webhook(_build_request(body))
        assert resp.status == 200
        mass.player_queues.play_media.assert_not_awaited()

    async def test_unknown_player_asks_for_clarification(self) -> None:
        """Command mentioning an unknown player returns 200 without playing."""
        mass = _make_mass([MockPlayer(player_id="p1", name="Спальня")])
        handler = self._make_handler(mass)
        body = {
            "session": {"skill_id": "skill-uuid-1", "session_id": "s1", "new": False},
            "request": {"command": "включи Metallica на Кухне"},
        }
        resp = await handler._handle_webhook(_build_request(body))
        assert resp.status == 200
        mass.player_queues.play_media.assert_not_awaited()

    async def test_no_results_says_not_found(self) -> None:
        """No search results returns 200 without playing."""
        mass = _make_mass([MockPlayer(player_id="p1", name="Кухня")])
        handler = self._make_handler(mass)
        body = {
            "session": {"skill_id": "skill-uuid-1", "session_id": "s1", "new": False},
            "request": {"command": "включи nonexistent на кухне"},
        }
        resp = await handler._handle_webhook(_build_request(body))
        assert resp.status == 200
        mass.player_queues.play_media.assert_not_awaited()

    async def test_full_happy_path_starts_play_media(self) -> None:
        """Resolved track triggers play_media on the correct player."""
        track = MagicMock(uri="library://track/123", spec_set=["uri"])
        mass = _make_mass([MockPlayer(player_id="p1", name="Кухня")], search_track=track)
        handler = self._make_handler(mass)
        body = {
            "session": {"skill_id": "skill-uuid-1", "session_id": "s1", "new": False},
            "request": {"command": "включи Metallica на кухне"},
        }
        resp = await handler._handle_webhook(_build_request(body))
        assert resp.status == 200
        # Allow the fire-and-forget task to run.
        await asyncio.sleep(0)
        mass.player_queues.play_media.assert_awaited_once()
        call_kwargs = mass.player_queues.play_media.call_args.kwargs
        assert call_kwargs["queue_id"] == "p1"
        assert call_kwargs["media"] is track


# ---------------------------------------------------------------------------
# Yandex state envelope (P0.1) + tts split (P0.2)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestStatePersistence:
    """Tests that the handler reads/writes Yandex state envelope correctly."""

    def _make_handler(self, mass: MagicMock) -> DialogsWebhookHandler:
        return DialogsWebhookHandler(mass, skill_id="skill-uuid-1", webhook_secret=_TEST_SECRET)

    async def test_resolved_player_persisted_in_session_and_application_state(self) -> None:
        """Successful play writes last_player_id to session_state and application_state."""
        track = MagicMock(uri="library://track/1", spec_set=["uri"])
        mass = _make_mass([MockPlayer(player_id="p1", name="Кухня")], search_track=track)
        handler = self._make_handler(mass)
        body = {
            "session": {"skill_id": "skill-uuid-1", "session_id": "s1", "new": False},
            "request": {"command": "включи Metallica на кухне"},
        }
        resp = await handler._handle_webhook(_build_request(body))
        await asyncio.sleep(0)
        body_out = _response_body(resp)
        assert body_out["session_state"]["last_player_id"] == "p1"
        assert body_out["application_state"]["last_player_id"] == "p1"
        # No user identity in the request → no user_state_update.
        assert "user_state_update" not in body_out

    async def test_user_state_written_when_user_id_present(self) -> None:
        """When session.user.user_id is set, response merges preferred_player_id."""
        track = MagicMock(uri="library://track/1", spec_set=["uri"])
        mass = _make_mass([MockPlayer(player_id="p1", name="Кухня")], search_track=track)
        handler = self._make_handler(mass)
        body = {
            "session": {
                "skill_id": "skill-uuid-1",
                "session_id": "s1",
                "new": False,
                "user": {"user_id": "yandex-user-1"},
            },
            "request": {"command": "включи Metallica на кухне"},
        }
        resp = await handler._handle_webhook(_build_request(body))
        await asyncio.sleep(0)
        body_out = _response_body(resp)
        assert body_out["user_state_update"] == {"preferred_player_id": "p1"}

    async def test_default_player_priority_session_over_application(self) -> None:
        """When command has no player hint, session.last_player_id wins over application's."""
        track = MagicMock(uri="library://track/1", spec_set=["uri"])
        mass = _make_mass(
            [MockPlayer(player_id="p1", name="Кухня"), MockPlayer(player_id="p2", name="Спальня")],
            search_track=track,
        )
        handler = self._make_handler(mass)
        body = {
            "session": {"skill_id": "skill-uuid-1", "session_id": "s1", "new": False},
            "request": {"command": "включи Beatles"},
            "state": {
                "session": {"last_player_id": "p1"},
                "application": {"last_player_id": "p2"},
            },
        }
        await handler._handle_webhook(_build_request(body))
        await asyncio.sleep(0)
        assert mass.player_queues.play_media.call_args.kwargs["queue_id"] == "p1"

    async def test_default_player_falls_through_to_application(self) -> None:
        """No session.last_player_id — application_state wins."""
        track = MagicMock(uri="library://track/1", spec_set=["uri"])
        mass = _make_mass(
            [MockPlayer(player_id="p1", name="Кухня"), MockPlayer(player_id="p2", name="Спальня")],
            search_track=track,
        )
        handler = self._make_handler(mass)
        body = {
            "session": {"skill_id": "skill-uuid-1", "session_id": "s1", "new": False},
            "request": {"command": "включи Beatles"},
            "state": {"application": {"last_player_id": "p2"}},
        }
        await handler._handle_webhook(_build_request(body))
        await asyncio.sleep(0)
        assert mass.player_queues.play_media.call_args.kwargs["queue_id"] == "p2"

    async def test_default_player_falls_through_to_user(self) -> None:
        """Both session and application empty — user.preferred_player_id wins."""
        track = MagicMock(uri="library://track/1", spec_set=["uri"])
        mass = _make_mass(
            [MockPlayer(player_id="p1", name="Кухня"), MockPlayer(player_id="p2", name="Спальня")],
            search_track=track,
        )
        handler = self._make_handler(mass)
        body = {
            "session": {
                "skill_id": "skill-uuid-1",
                "session_id": "s1",
                "new": False,
                "user": {"user_id": "yandex-user-1"},
            },
            "request": {"command": "включи Beatles"},
            "state": {"user": {"preferred_player_id": "p2"}},
        }
        await handler._handle_webhook(_build_request(body))
        await asyncio.sleep(0)
        assert mass.player_queues.play_media.call_args.kwargs["queue_id"] == "p2"

    async def test_user_id_echo_falls_back_to_nested(self) -> None:
        """When root session.user_id is missing, echo the nested session.user.user_id."""
        track = MagicMock(uri="library://track/1", spec_set=["uri"])
        mass = _make_mass([MockPlayer(player_id="p1", name="Кухня")], search_track=track)
        handler = self._make_handler(mass)
        body = {
            "session": {
                "skill_id": "skill-uuid-1",
                "session_id": "s1",
                "new": False,
                # No root "user_id"; only the nested one.
                "user": {"user_id": "yandex-user-42"},
            },
            "request": {"command": "включи Metallica на кухне"},
        }
        resp = await handler._handle_webhook(_build_request(body))
        body_out = _response_body(resp)
        assert body_out["session"]["user_id"] == "yandex-user-42"

    async def test_session_state_preserved_on_player_not_found(self) -> None:
        """Even on error, existing session_state is echoed back so other keys aren't lost."""
        mass = _make_mass([MockPlayer(player_id="p1", name="Спальня")])
        handler = self._make_handler(mass)
        body = {
            "session": {"skill_id": "skill-uuid-1", "session_id": "s1", "new": False},
            "request": {"command": "включи Metallica на Кухне"},
            "state": {"session": {"foo": "bar"}},
        }
        resp = await handler._handle_webhook(_build_request(body))
        body_out = _response_body(resp)
        assert body_out["session_state"] == {"foo": "bar"}


class TestTtsHelper:
    """Tests for _tts_for stress-mark substitution."""

    def test_known_word_gets_stress_mark(self) -> None:
        """A known word from the dict has `+` injected before the stressed vowel."""
        assert _tts_for("Включаю Metallica") == "Включ+аю Metallica"

    def test_unknown_word_passes_through(self) -> None:
        """A word not in the dict is unchanged."""
        assert _tts_for("Привет мир") == "Привет мир"

    def test_empty_input(self) -> None:
        """Empty input is returned as-is."""
        assert _tts_for("") == ""

    def test_capitalisation_preserved(self) -> None:
        """Original capitalisation of the first letter is preserved."""
        # All-lowercase original.
        assert _tts_for("включаю джаз") == "включ+аю джаз"
        # Capitalised original.
        assert _tts_for("Включаю джаз") == "Включ+аю джаз"


@pytest.mark.asyncio
class TestTtsResponseField:
    """Test that the handler emits separate text + tts in the response envelope."""

    async def test_response_tts_differs_from_text_when_known_word_used(self) -> None:
        """Happy path response has different `tts` from `text` when stress-mark fires."""
        track = MagicMock(uri="library://track/1", spec_set=["uri"])
        mass = _make_mass([MockPlayer(player_id="p1", name="Кухня")], search_track=track)
        handler = DialogsWebhookHandler(mass, skill_id="skill-uuid-1", webhook_secret=_TEST_SECRET)
        body = {
            "session": {"skill_id": "skill-uuid-1", "session_id": "s1", "new": False},
            "request": {"command": "включи Metallica на кухне"},
        }
        resp = await handler._handle_webhook(_build_request(body))
        body_out = _response_body(resp)
        text = body_out["response"]["text"]
        tts = body_out["response"]["tts"]
        assert text != tts
        assert "включ+аю" in tts.lower()


# ---------------------------------------------------------------------------
# Control commands integration (P0.6)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestControlCommandsIntegration:
    """Integration tests for the control branch in _handle_webhook."""

    def _setup_mass_with_control_methods(self, players: list[MockPlayer]) -> MagicMock:
        mass = _make_mass(players)
        mass.player_queues.pause = AsyncMock()
        mass.player_queues.resume = AsyncMock()
        mass.player_queues.stop = AsyncMock()
        mass.player_queues.next = AsyncMock()
        mass.player_queues.previous = AsyncMock()
        mass.players.cmd_volume_up = AsyncMock()
        mass.players.cmd_volume_down = AsyncMock()
        mass.players.cmd_volume_set = AsyncMock()
        mass.players.cmd_volume_mute = AsyncMock()
        return mass

    async def test_pause_command_calls_player_queues_pause(self) -> None:
        """'пауза на кухне' → mass.player_queues.pause(p1) and confirms in response."""
        mass = self._setup_mass_with_control_methods([MockPlayer(player_id="p1", name="Кухня")])
        handler = DialogsWebhookHandler(mass, skill_id="skill-uuid-1", webhook_secret=_TEST_SECRET)
        body = {
            "session": {"skill_id": "skill-uuid-1", "session_id": "s1", "new": False},
            "request": {"command": "пауза на кухне"},
        }
        resp = await handler._handle_webhook(_build_request(body))
        await asyncio.sleep(0)
        assert resp.status == 200
        mass.player_queues.pause.assert_awaited_once_with("p1")
        body_out = _response_body(resp)
        assert body_out["response"]["text"] == "Пауза."
        # State persisted as in play branch.
        assert body_out["session_state"]["last_player_id"] == "p1"
        assert body_out["application_state"]["last_player_id"] == "p1"
        # play_media should NOT be called for control commands.
        mass.player_queues.play_media.assert_not_awaited()

    async def test_volume_set_command(self) -> None:
        """'громкость 50 на кухне' → cmd_volume_set(p1, 50)."""
        mass = self._setup_mass_with_control_methods([MockPlayer(player_id="p1", name="Кухня")])
        handler = DialogsWebhookHandler(mass, skill_id="skill-uuid-1", webhook_secret=_TEST_SECRET)
        body = {
            "session": {"skill_id": "skill-uuid-1", "session_id": "s1", "new": False},
            "request": {"command": "громкость 50 на кухне"},
        }
        resp = await handler._handle_webhook(_build_request(body))
        await asyncio.sleep(0)
        assert resp.status == 200
        mass.players.cmd_volume_set.assert_awaited_once_with("p1", 50)

    async def test_control_uses_default_player_from_state(self) -> None:
        """A control phrase without explicit hint uses state.session.last_player_id."""
        mass = self._setup_mass_with_control_methods(
            [MockPlayer(player_id="p1", name="Кухня"), MockPlayer(player_id="p2", name="Спальня")]
        )
        handler = DialogsWebhookHandler(mass, skill_id="skill-uuid-1", webhook_secret=_TEST_SECRET)
        body = {
            "session": {"skill_id": "skill-uuid-1", "session_id": "s1", "new": False},
            "request": {"command": "пауза"},
            "state": {"session": {"last_player_id": "p2"}},
        }
        resp = await handler._handle_webhook(_build_request(body))
        await asyncio.sleep(0)
        assert resp.status == 200
        mass.player_queues.pause.assert_awaited_once_with("p2")

    async def test_control_unknown_player_asks_for_clarification(self) -> None:
        """Control command with an unknown player hint returns a clarification."""
        mass = self._setup_mass_with_control_methods([MockPlayer(player_id="p1", name="Спальня")])
        handler = DialogsWebhookHandler(mass, skill_id="skill-uuid-1", webhook_secret=_TEST_SECRET)
        body = {
            "session": {"skill_id": "skill-uuid-1", "session_id": "s1", "new": False},
            "request": {"command": "пауза на гостиной"},
        }
        resp = await handler._handle_webhook(_build_request(body))
        assert resp.status == 200
        mass.player_queues.pause.assert_not_awaited()
        body_out = _response_body(resp)
        assert "Не нашёл колонку «гостиной»" in body_out["response"]["text"]

    async def test_list_players_returns_player_names(self) -> None:
        """'сколько колонок видишь' → response with the count and names of exposed players."""
        mass = self._setup_mass_with_control_methods(
            [
                MockPlayer(player_id="p1", name="Кухня"),
                MockPlayer(player_id="p2", name="Спальня"),
                MockPlayer(player_id="p3", name="Гостиная"),
            ]
        )
        handler = DialogsWebhookHandler(mass, skill_id="skill-uuid-1", webhook_secret=_TEST_SECRET)
        body = {
            "session": {"skill_id": "skill-uuid-1", "session_id": "s1", "new": False},
            "request": {"command": "сколько колонок видишь"},
        }
        resp = await handler._handle_webhook(_build_request(body))
        assert resp.status == 200
        body_out = _response_body(resp)
        text = body_out["response"]["text"]
        assert "Вижу 3 колонки" in text
        assert "Кухня" in text
        assert "Спальня" in text
        assert "Гостиная" in text
        # Informational query — keep the mic open for follow-ups.
        assert body_out["response"]["end_session"] is False
        # No playback or control was dispatched.
        mass.player_queues.pause.assert_not_awaited()
        mass.player_queues.play_media.assert_not_awaited()

    async def test_list_players_skips_unavailable(self) -> None:
        """Only available + enabled + non-synced players are counted."""
        mass = self._setup_mass_with_control_methods(
            [
                MockPlayer(player_id="p1", name="Кухня"),
                MockPlayer(player_id="p2", name="Disabled", enabled=False),
                MockPlayer(player_id="p3", name="Unavailable", available=False),
                MockPlayer(player_id="p4", name="Synced", synced_to="leader"),
            ]
        )
        handler = DialogsWebhookHandler(mass, skill_id="skill-uuid-1", webhook_secret=_TEST_SECRET)
        body = {
            "session": {"skill_id": "skill-uuid-1", "session_id": "s1", "new": False},
            "request": {"command": "какие колонки"},
        }
        resp = await handler._handle_webhook(_build_request(body))
        body_out = _response_body(resp)
        text = body_out["response"]["text"]
        assert "Вижу одну колонку: Кухня" in text
        assert "Disabled" not in text
        assert "Unavailable" not in text
        assert "Synced" not in text

    async def test_control_no_hint_no_default_asks_for_player(self) -> None:
        """Control with no hint + no default + multi-player → ask for the player.

        Previously responded with the misleading "Не нашёл колонку «(не указано)»";
        now the message tells the user to specify the player.
        """
        mass = self._setup_mass_with_control_methods(
            [
                MockPlayer(player_id="p1", name="Кухня"),
                MockPlayer(player_id="p2", name="Спальня"),
            ]
        )
        handler = DialogsWebhookHandler(mass, skill_id="skill-uuid-1", webhook_secret=_TEST_SECRET)
        body = {
            "session": {"skill_id": "skill-uuid-1", "session_id": "s1", "new": False},
            "request": {"command": "пауза"},
        }
        resp = await handler._handle_webhook(_build_request(body))
        assert resp.status == 200
        mass.player_queues.pause.assert_not_awaited()
        body_out = _response_body(resp)
        text = body_out["response"]["text"]
        assert "(не указано)" not in text
        assert "на какой колонке" in text.lower()


# ---------------------------------------------------------------------------
# Disambiguation (P0.3)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestDisambiguation:
    """End-to-end tests for the disambiguation prompt + pending-command replay."""

    async def test_multiple_matches_returns_disambiguation_prompt(self) -> None:
        """Two candidates → response carries buttons + pending_command, end_session=False."""
        track = MagicMock(uri="library://track/1", spec_set=["uri"])
        mass = _make_mass(
            [
                MockPlayer(player_id="p1", name="Кухня большая"),
                MockPlayer(player_id="p2", name="Кухня маленькая"),
            ],
            search_track=track,
        )
        handler = DialogsWebhookHandler(mass, skill_id="skill-uuid-1", webhook_secret=_TEST_SECRET)
        body = {
            "session": {"skill_id": "skill-uuid-1", "session_id": "s1", "new": False},
            "request": {"command": "включи Metallica на кухне"},
        }
        resp = await handler._handle_webhook(_build_request(body))
        assert resp.status == 200
        body_out = _response_body(resp)
        assert body_out["response"]["end_session"] is False
        assert "buttons" in body_out["response"]
        button_titles = {b["title"] for b in body_out["response"]["buttons"]}
        assert button_titles == {"Кухня большая", "Кухня маленькая"}
        # pending_command is saved with the original play intent + the
        # ordered candidate IDs for voice ordinal resolution.
        pending = body_out["session_state"]["pending_command"]
        assert pending["kind"] == "search"
        assert pending["query"] == "metallica"
        assert pending["radio_mode"] is True
        assert pending["candidate_ids"] == ["p1", "p2"]
        # Nothing is played yet.
        mass.player_queues.play_media.assert_not_awaited()

    async def test_button_press_resolves_pending(self) -> None:
        """ButtonPressed payload.player_id triggers a play of the saved pending_command."""
        track = MagicMock(uri="library://track/1", spec_set=["uri"])
        mass = _make_mass(
            [
                MockPlayer(player_id="p1", name="Кухня большая"),
                MockPlayer(player_id="p2", name="Кухня маленькая"),
            ],
            search_track=track,
        )
        handler = DialogsWebhookHandler(mass, skill_id="skill-uuid-1", webhook_secret=_TEST_SECRET)
        body = {
            "session": {"skill_id": "skill-uuid-1", "session_id": "s1", "new": False},
            "request": {
                "type": "ButtonPressed",
                "command": "Кухня большая",
                "payload": {"player_id": "p1"},
            },
            "state": {
                "session": {
                    "pending_command": {"kind": "search", "query": "metallica", "radio_mode": True},
                },
            },
        }
        resp = await handler._handle_webhook(_build_request(body))
        await asyncio.sleep(0)
        assert resp.status == 200
        mass.player_queues.play_media.assert_awaited_once()
        assert mass.player_queues.play_media.call_args.kwargs["queue_id"] == "p1"
        # pending_command is cleared from the response state.
        body_out = _response_body(resp)
        assert "pending_command" not in body_out["session_state"]
        assert body_out["session_state"]["last_player_id"] == "p1"

    async def test_slot_elicit_when_query_empty(self) -> None:
        """Bare verb (empty query) → 'Что включить?' + awaiting_query=True."""
        mass = _make_mass([MockPlayer(player_id="p1", name="Кухня")])
        handler = DialogsWebhookHandler(mass, skill_id="skill-uuid-1", webhook_secret=_TEST_SECRET)
        body = {
            "session": {"skill_id": "skill-uuid-1", "session_id": "s1", "new": False},
            "request": {"command": "включи"},
        }
        resp = await handler._handle_webhook(_build_request(body))
        assert resp.status == 200
        body_out = _response_body(resp)
        assert body_out["response"]["end_session"] is False
        assert "Что включить" in body_out["response"]["text"]
        assert body_out["session_state"]["awaiting_query"] is True
        # Nothing played.
        mass.player_queues.play_media.assert_not_awaited()

    async def test_followup_with_awaiting_query_resolves(self) -> None:
        """Next utterance after slot-elicit is treated as the play query."""
        track = MagicMock(uri="library://track/1", spec_set=["uri"])
        mass = _make_mass([MockPlayer(player_id="p1", name="Кухня")], search_track=track)
        handler = DialogsWebhookHandler(mass, skill_id="skill-uuid-1", webhook_secret=_TEST_SECRET)
        body = {
            "session": {"skill_id": "skill-uuid-1", "session_id": "s1", "new": False},
            "request": {"command": "Metallica"},
            "state": {"session": {"awaiting_query": True}},
        }
        resp = await handler._handle_webhook(_build_request(body))
        await asyncio.sleep(0)
        assert resp.status == 200
        mass.player_queues.play_media.assert_awaited_once()
        body_out = _response_body(resp)
        # awaiting_query is cleared on success.
        assert "awaiting_query" not in body_out["session_state"]

    async def test_control_during_awaiting_query_dispatches_control(self) -> None:
        """Slot-elicit was active, but the user pivots to a control phrase.

        "Включи." → "Что включить?" (awaiting_query=True). Then the user
        says "пауза на кухне" — this must dispatch a control command, not
        get prefixed with "включи " and turned into a search query.
        """
        mass = _make_mass([MockPlayer(player_id="p1", name="Кухня")])
        mass.player_queues.pause = AsyncMock()
        handler = DialogsWebhookHandler(mass, skill_id="skill-uuid-1", webhook_secret=_TEST_SECRET)
        body = {
            "session": {"skill_id": "skill-uuid-1", "session_id": "s1", "new": False},
            "request": {"command": "пауза на кухне"},
            "state": {"session": {"awaiting_query": True}},
        }
        resp = await handler._handle_webhook(_build_request(body))
        await asyncio.sleep(0)
        assert resp.status == 200
        mass.player_queues.pause.assert_awaited_once_with("p1")
        # awaiting_query must be cleared on successful control dispatch.
        body_out = _response_body(resp)
        assert "awaiting_query" not in body_out["session_state"]
        # play_media not called — this was a control, not a play.
        mass.player_queues.play_media.assert_not_awaited()

    async def test_followup_full_play_command_does_not_double_prefix(self) -> None:
        """Follow-up like 'включи Yesterday' is parsed as-is, not double-prefixed."""
        track = MagicMock(uri="library://track/1", spec_set=["uri"])
        mass = _make_mass([MockPlayer(player_id="p1", name="Кухня")], search_track=track)
        handler = DialogsWebhookHandler(mass, skill_id="skill-uuid-1", webhook_secret=_TEST_SECRET)
        body = {
            "session": {"skill_id": "skill-uuid-1", "session_id": "s1", "new": False},
            "request": {"command": "включи Yesterday"},
            "state": {"session": {"awaiting_query": True}},
        }
        resp = await handler._handle_webhook(_build_request(body))
        await asyncio.sleep(0)
        assert resp.status == 200
        mass.player_queues.play_media.assert_awaited_once()
        # The search call must use "yesterday" (after parser strips "включи"),
        # not "включи yesterday".
        search_query = mass.music.search.call_args.kwargs["search_query"]
        assert search_query == "yesterday"

    async def test_play_no_hint_no_default_offers_disambiguation(self) -> None:
        """Play branch: no hint + no default + 2+ players → disambiguation prompt.

        Without this, the user would see "Не нашёл колонку «(не указано)»".
        """
        track = MagicMock(uri="library://track/1", spec_set=["uri"])
        mass = _make_mass(
            [
                MockPlayer(player_id="p1", name="Кухня"),
                MockPlayer(player_id="p2", name="Спальня"),
            ],
            search_track=track,
        )
        handler = DialogsWebhookHandler(mass, skill_id="skill-uuid-1", webhook_secret=_TEST_SECRET)
        body = {
            "session": {"skill_id": "skill-uuid-1", "session_id": "s1", "new": False},
            "request": {"command": "включи Metallica"},
        }
        resp = await handler._handle_webhook(_build_request(body))
        body_out = _response_body(resp)
        assert body_out["response"]["end_session"] is False
        assert "buttons" in body_out["response"]
        button_titles = {b["title"] for b in body_out["response"]["buttons"]}
        assert button_titles == {"Кухня", "Спальня"}
        # pending_command saved with the original play intent + candidate_ids.
        # Order is significant — used as the index space for voice ordinal
        # resolution ("первая" → candidate_ids[0]).
        pending = body_out["session_state"]["pending_command"]
        assert pending["kind"] == "search"
        assert pending["query"] == "metallica"
        assert pending["radio_mode"] is True
        assert pending["candidate_ids"] == ["p1", "p2"]
        mass.player_queues.play_media.assert_not_awaited()

    async def test_button_payload_validated_against_exposed_set(self) -> None:
        """ButtonPressed with a payload targeting a non-exposed player is rejected.

        Defence-in-depth: even though Yandex echoes our own payload back,
        we never trust the player_id without re-checking it's currently
        exposed/enabled/available.
        """
        track = MagicMock(uri="library://track/1", spec_set=["uri"])
        mass = _make_mass(
            [
                MockPlayer(player_id="p1", name="Кухня"),
                MockPlayer(player_id="p2", name="Спальня"),
            ],
            search_track=track,
        )
        handler = DialogsWebhookHandler(mass, skill_id="skill-uuid-1", webhook_secret=_TEST_SECRET)
        body = {
            "session": {"skill_id": "skill-uuid-1", "session_id": "s1", "new": False},
            "request": {
                "type": "ButtonPressed",
                "command": "Гостиная",
                "payload": {"player_id": "p99-not-in-set"},
            },
            "state": {
                "session": {
                    "pending_command": {"kind": "search", "query": "metallica", "radio_mode": True},
                },
            },
        }
        resp = await handler._handle_webhook(_build_request(body))
        await asyncio.sleep(0)
        # play_media must NOT be awaited — invalid payload should not play.
        mass.player_queues.play_media.assert_not_awaited()
        # Status is still 200; the handler falls through, but no playback.
        assert resp.status == 200

    async def test_disambiguation_clears_awaiting_query(self) -> None:
        """Slot-elicit → multi-match → disambiguation prompt drops awaiting_query.

        Without this, the next user utterance ("Кухня маленькая") would get
        auto-prefixed with "включи " by the awaiting-query branch and miss
        the pending-command resolver.
        """
        track = MagicMock(uri="library://track/1", spec_set=["uri"])
        mass = _make_mass(
            [
                MockPlayer(player_id="p1", name="Кухня большая"),
                MockPlayer(player_id="p2", name="Кухня маленькая"),
            ],
            search_track=track,
        )
        handler = DialogsWebhookHandler(mass, skill_id="skill-uuid-1", webhook_secret=_TEST_SECRET)
        # Simulate the awaiting_query → ambiguous-resolution turn.
        body = {
            "session": {"skill_id": "skill-uuid-1", "session_id": "s1", "new": False},
            "request": {"command": "Metallica на кухне"},
            "state": {"session": {"awaiting_query": True}},
        }
        resp = await handler._handle_webhook(_build_request(body))
        body_out = _response_body(resp)
        # Disambiguation prompt is returned (multi-match).
        assert body_out["response"]["end_session"] is False
        assert "buttons" in body_out["response"]
        # And the response carries pending_command but NOT awaiting_query.
        assert "pending_command" in body_out["session_state"]
        assert "awaiting_query" not in body_out["session_state"]

    async def test_voice_ordinal_resolves_pending(self) -> None:
        """User answers disambiguation with 'первая' → first candidate is picked."""
        track = MagicMock(uri="library://track/1", spec_set=["uri"])
        mass = _make_mass(
            [
                MockPlayer(player_id="p1", name="Кухня"),
                MockPlayer(player_id="p2", name="Спальня"),
            ],
            search_track=track,
        )
        handler = DialogsWebhookHandler(mass, skill_id="skill-uuid-1", webhook_secret=_TEST_SECRET)
        body = {
            "session": {"skill_id": "skill-uuid-1", "session_id": "s1", "new": False},
            "request": {"command": "первая"},
            "state": {
                "session": {
                    "pending_command": {
                        "kind": "search",
                        "query": "metallica",
                        "radio_mode": True,
                        "candidate_ids": ["p1", "p2"],
                    },
                },
            },
        }
        resp = await handler._handle_webhook(_build_request(body))
        await asyncio.sleep(0)
        assert resp.status == 200
        mass.player_queues.play_media.assert_awaited_once()
        assert mass.player_queues.play_media.call_args.kwargs["queue_id"] == "p1"

    async def test_voice_ordinal_second_candidate(self) -> None:
        """'вторая' picks the second candidate from candidate_ids."""
        track = MagicMock(uri="library://track/1", spec_set=["uri"])
        mass = _make_mass(
            [
                MockPlayer(player_id="p1", name="Кухня"),
                MockPlayer(player_id="p2", name="Спальня"),
            ],
            search_track=track,
        )
        handler = DialogsWebhookHandler(mass, skill_id="skill-uuid-1", webhook_secret=_TEST_SECRET)
        body = {
            "session": {"skill_id": "skill-uuid-1", "session_id": "s1", "new": False},
            "request": {"command": "вторая"},
            "state": {
                "session": {
                    "pending_command": {
                        "kind": "search",
                        "query": "metallica",
                        "radio_mode": True,
                        "candidate_ids": ["p1", "p2"],
                    },
                },
            },
        }
        await handler._handle_webhook(_build_request(body))
        await asyncio.sleep(0)
        assert mass.player_queues.play_media.call_args.kwargs["queue_id"] == "p2"

    async def test_ordinal_out_of_range_reasks_does_not_fall_through(self) -> None:
        """User says 'третья' when only 2 candidates → re-ask, don't search for 'третья'.

        Without this, the ordinal would be parsed but skip the lookup,
        the free-text path would parse the utterance as a search query,
        and a default-player resolution might play "третья" on some
        random player.
        """
        track = MagicMock(uri="library://track/1", spec_set=["uri"])
        mass = _make_mass(
            [
                MockPlayer(player_id="p1", name="Кухня"),
                MockPlayer(player_id="p2", name="Спальня"),
            ],
            search_track=track,
        )
        handler = DialogsWebhookHandler(mass, skill_id="skill-uuid-1", webhook_secret=_TEST_SECRET)
        body = {
            "session": {"skill_id": "skill-uuid-1", "session_id": "s1", "new": False},
            "request": {"command": "третья"},
            "state": {
                "session": {
                    "pending_command": {
                        "kind": "search",
                        "query": "metallica",
                        "radio_mode": True,
                        "candidate_ids": ["p1", "p2"],
                    },
                },
            },
        }
        resp = await handler._handle_webhook(_build_request(body))
        await asyncio.sleep(0)
        assert resp.status == 200
        body_out = _response_body(resp)
        # Disambiguation re-asked, not played.
        assert body_out["response"]["end_session"] is False
        assert "buttons" in body_out["response"]
        # pending_command still set (with same candidate set).
        assert body_out["session_state"]["pending_command"]["candidate_ids"] == ["p1", "p2"]
        mass.player_queues.play_media.assert_not_awaited()

    async def test_ordinal_targets_unexposed_player_reasks(self) -> None:
        """User picks a valid ordinal but the indexed player has been removed → re-ask."""
        track = MagicMock(uri="library://track/1", spec_set=["uri"])
        # Only p1 exposed now — p2 is gone since the buttons were sent.
        mass = _make_mass(
            [MockPlayer(player_id="p1", name="Кухня")],
            search_track=track,
        )
        handler = DialogsWebhookHandler(mass, skill_id="skill-uuid-1", webhook_secret=_TEST_SECRET)
        body = {
            "session": {"skill_id": "skill-uuid-1", "session_id": "s1", "new": False},
            "request": {"command": "вторая"},
            "state": {
                "session": {
                    "pending_command": {
                        "kind": "search",
                        "query": "metallica",
                        "radio_mode": True,
                        "candidate_ids": ["p1", "p2"],
                    },
                },
            },
        }
        resp = await handler._handle_webhook(_build_request(body))
        await asyncio.sleep(0)
        body_out = _response_body(resp)
        # Re-asked with the remaining exposed candidate (p1).
        assert body_out["response"]["end_session"] is False
        assert body_out["session_state"]["pending_command"]["candidate_ids"] == ["p1"]
        mass.player_queues.play_media.assert_not_awaited()

    async def test_voice_ordinal_with_filler(self) -> None:
        """Filler-padded ordinal answers ('выбираю первую', 'хочу вторую') resolve.

        On smart speakers users naturally pad voice replies with filler;
        the strict-anchor regex from v1.8.2 missed these.
        """
        track = MagicMock(uri="library://track/1", spec_set=["uri"])
        mass = _make_mass(
            [
                MockPlayer(player_id="p1", name="Кухня"),
                MockPlayer(player_id="p2", name="Спальня"),
            ],
            search_track=track,
        )
        handler = DialogsWebhookHandler(mass, skill_id="skill-uuid-1", webhook_secret=_TEST_SECRET)
        body = {
            "session": {"skill_id": "skill-uuid-1", "session_id": "s1", "new": False},
            "request": {"command": "выбираю первую"},
            "state": {
                "session": {
                    "pending_command": {
                        "kind": "search",
                        "query": "metallica",
                        "radio_mode": True,
                        "candidate_ids": ["p1", "p2"],
                    },
                },
            },
        }
        await handler._handle_webhook(_build_request(body))
        await asyncio.sleep(0)
        assert mass.player_queues.play_media.call_args.kwargs["queue_id"] == "p1"

    async def test_voice_accusative_adjective(self) -> None:
        """Accusative-case answer 'большую' resolves to 'Кухня большая'.

        Caught by the new `ую` suffix in `_INFLECTION_SUFFIXES`.
        """
        track = MagicMock(uri="library://track/1", spec_set=["uri"])
        mass = _make_mass(
            [
                MockPlayer(player_id="p1", name="Кухня большая"),
                MockPlayer(player_id="p2", name="Кухня маленькая"),
            ],
            search_track=track,
        )
        handler = DialogsWebhookHandler(mass, skill_id="skill-uuid-1", webhook_secret=_TEST_SECRET)
        body = {
            "session": {"skill_id": "skill-uuid-1", "session_id": "s1", "new": False},
            "request": {"command": "большую"},
            "state": {
                "session": {
                    "pending_command": {
                        "kind": "search",
                        "query": "metallica",
                        "radio_mode": True,
                        "candidate_ids": ["p1", "p2"],
                    },
                },
            },
        }
        await handler._handle_webhook(_build_request(body))
        await asyncio.sleep(0)
        assert mass.player_queues.play_media.call_args.kwargs["queue_id"] == "p1"

    async def test_voice_accusative_noun(self) -> None:
        """Accusative noun 'Кухню' resolves to 'Кухня' via the new `ю` suffix."""
        track = MagicMock(uri="library://track/1", spec_set=["uri"])
        mass = _make_mass(
            [
                MockPlayer(player_id="p1", name="Кухня"),
                MockPlayer(player_id="p2", name="Спальня"),
            ],
            search_track=track,
        )
        handler = DialogsWebhookHandler(mass, skill_id="skill-uuid-1", webhook_secret=_TEST_SECRET)
        body = {
            "session": {"skill_id": "skill-uuid-1", "session_id": "s1", "new": False},
            "request": {"command": "Кухню"},
            "state": {
                "session": {
                    "pending_command": {
                        "kind": "search",
                        "query": "metallica",
                        "radio_mode": True,
                        "candidate_ids": ["p1", "p2"],
                    },
                },
            },
        }
        await handler._handle_webhook(_build_request(body))
        await asyncio.sleep(0)
        assert mass.player_queues.play_media.call_args.kwargs["queue_id"] == "p1"

    async def test_voice_ordinal_digit(self) -> None:
        """A bare digit ('2') also works as an ordinal."""
        track = MagicMock(uri="library://track/1", spec_set=["uri"])
        mass = _make_mass(
            [
                MockPlayer(player_id="p1", name="Кухня"),
                MockPlayer(player_id="p2", name="Спальня"),
            ],
            search_track=track,
        )
        handler = DialogsWebhookHandler(mass, skill_id="skill-uuid-1", webhook_secret=_TEST_SECRET)
        body = {
            "session": {"skill_id": "skill-uuid-1", "session_id": "s1", "new": False},
            "request": {"command": "2"},
            "state": {
                "session": {
                    "pending_command": {
                        "kind": "search",
                        "query": "metallica",
                        "radio_mode": True,
                        "candidate_ids": ["p1", "p2"],
                    },
                },
            },
        }
        await handler._handle_webhook(_build_request(body))
        await asyncio.sleep(0)
        assert mass.player_queues.play_media.call_args.kwargs["queue_id"] == "p2"

    async def test_freetext_narrows_to_candidate_set(self) -> None:
        """Free-text answer is matched only against the saved candidate IDs.

        With 3 exposed players (Кухня большая, Кухня маленькая, Гостиная)
        and a saved candidate set covering only the two kitchens, saying
        'большая' must pick "Кухня большая" — even though 'большая'
        could ambiguously refer to several players in a larger set.
        """
        track = MagicMock(uri="library://track/1", spec_set=["uri"])
        mass = _make_mass(
            [
                MockPlayer(player_id="p1", name="Кухня большая"),
                MockPlayer(player_id="p2", name="Кухня маленькая"),
                MockPlayer(player_id="p3", name="Гостиная большая"),
            ],
            search_track=track,
        )
        handler = DialogsWebhookHandler(mass, skill_id="skill-uuid-1", webhook_secret=_TEST_SECRET)
        body = {
            "session": {"skill_id": "skill-uuid-1", "session_id": "s1", "new": False},
            "request": {"command": "большая"},
            "state": {
                "session": {
                    "pending_command": {
                        "kind": "search",
                        "query": "metallica",
                        "radio_mode": True,
                        "candidate_ids": ["p1", "p2"],
                    },
                },
            },
        }
        resp = await handler._handle_webhook(_build_request(body))
        await asyncio.sleep(0)
        assert resp.status == 200
        # Must pick p1 (Кухня большая, in candidate set) — not p3
        # (also matches "большая" but excluded from candidate_ids).
        assert mass.player_queues.play_media.call_args.kwargs["queue_id"] == "p1"

    async def test_freetext_followup_resolves_pending(self) -> None:
        """User says 'на кухне маленькой' after the disambiguation question — plays on p2."""
        track = MagicMock(uri="library://track/1", spec_set=["uri"])
        mass = _make_mass(
            [
                MockPlayer(player_id="p1", name="Кухня большая"),
                MockPlayer(player_id="p2", name="Кухня маленькая"),
            ],
            search_track=track,
        )
        handler = DialogsWebhookHandler(mass, skill_id="skill-uuid-1", webhook_secret=_TEST_SECRET)
        body = {
            "session": {"skill_id": "skill-uuid-1", "session_id": "s1", "new": False},
            "request": {"command": "на кухне маленькой"},
            "state": {
                "session": {
                    "pending_command": {"kind": "search", "query": "metallica", "radio_mode": True},
                },
            },
        }
        resp = await handler._handle_webhook(_build_request(body))
        await asyncio.sleep(0)
        assert resp.status == 200
        mass.player_queues.play_media.assert_awaited_once()
        assert mass.player_queues.play_media.call_args.kwargs["queue_id"] == "p2"
