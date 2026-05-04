"""Tests for provider/dialogs.py — webhook handler."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp.test_utils import make_mocked_request

from music_assistant.providers.yandex_smarthome.dialogs import DialogsWebhookHandler

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
        artists: list = field(default_factory=list)
        albums: list = field(default_factory=list)
        tracks: list = field(default_factory=list)
        playlists: list = field(default_factory=list)

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


def _build_request(body: dict, secret: str = _TEST_SECRET) -> web.Request:
    """Build a mocked aiohttp Request that returns the given JSON body."""
    req = make_mocked_request(
        "POST",
        f"/api/yandex_dialogs/webhook/{secret}",
        match_info={"secret": secret},
    )
    req.json = AsyncMock(return_value=body)  # type: ignore[method-assign]
    return req


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

    async def test_session_remembers_player_for_followups(self) -> None:
        """Second command in same session uses the player from the first command."""
        track = MagicMock(uri="library://track/123", spec_set=["uri"])
        mass = _make_mass([MockPlayer(player_id="p1", name="Кухня")], search_track=track)
        handler = self._make_handler(mass)
        # First command — explicit player hint
        await handler._handle_webhook(
            _build_request(
                {
                    "session": {"skill_id": "skill-uuid-1", "session_id": "s99", "new": False},
                    "request": {"command": "включи Metallica на кухне"},
                }
            )
        )
        await asyncio.sleep(0)
        # Second command — no hint, should pick the same player from session memory
        mass.player_queues.play_media.reset_mock()
        await handler._handle_webhook(
            _build_request(
                {
                    "session": {"skill_id": "skill-uuid-1", "session_id": "s99", "new": False},
                    "request": {"command": "включи Beatles"},
                }
            )
        )
        await asyncio.sleep(0)
        mass.player_queues.play_media.assert_awaited_once()
        assert mass.player_queues.play_media.call_args.kwargs["queue_id"] == "p1"
