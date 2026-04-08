"""Tests for the Ynison WebSocket client."""

from __future__ import annotations

import asyncio
import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from provider.constants import (
    DEFAULT_APP_NAME,
    DEVICE_TYPE_WEB,
    YNISON_ORIGIN,
)
from provider.ynison_client import (
    YnisonClient,
    YnisonDeviceInfo,
    YnisonState,
    generate_device_id,
)


@pytest.fixture
def device_info() -> YnisonDeviceInfo:
    """Create test device info."""
    return YnisonDeviceInfo(
        device_id="test-device-id",
        title="Test Device",
    )


@pytest.fixture
def mock_callbacks() -> tuple[AsyncMock, AsyncMock]:
    """Create mock callbacks for state update and disconnect."""
    return AsyncMock(), AsyncMock()


@pytest.fixture
def client(
    device_info: YnisonDeviceInfo,
    mock_callbacks: tuple[AsyncMock, AsyncMock],
) -> YnisonClient:
    """Create a YnisonClient instance for testing."""
    on_state_update, on_disconnect = mock_callbacks
    return YnisonClient(
        token="test-token",
        device_info=device_info,
        on_state_update=on_state_update,
        on_disconnect=on_disconnect,
        logger=MagicMock(),
    )


# ------------------------------------------------------------------
# YnisonDeviceInfo
# ------------------------------------------------------------------


class TestYnisonDeviceInfo:
    """Tests for YnisonDeviceInfo dataclass."""

    def test_defaults(self) -> None:
        info = YnisonDeviceInfo(device_id="abc", title="My Speaker")
        assert info.type == DEVICE_TYPE_WEB
        assert info.app_name == DEFAULT_APP_NAME

    def test_custom_values(self) -> None:
        info = YnisonDeviceInfo(
            device_id="xyz",
            title="Custom",
            type="TV",
            app_name="CustomApp",
            app_version="2.0",
        )
        assert info.type == "TV"
        assert info.app_version == "2.0"


# ------------------------------------------------------------------
# YnisonState
# ------------------------------------------------------------------


class TestYnisonState:
    """Tests for YnisonState dataclass."""

    def test_empty_state(self) -> None:
        state = YnisonState()
        assert state.current_track_id is None
        assert state.is_paused is True
        assert state.progress_ms == 0
        assert state.duration_ms == 0

    def test_current_track_id(self) -> None:
        state = YnisonState(
            player_state={
                "player_queue": {
                    "current_playable_index": 1,
                    "playable_list": [
                        {"playable_id": "track1"},
                        {"playable_id": "track2"},
                        {"playable_id": "track3"},
                    ],
                }
            }
        )
        assert state.current_track_id == "track2"

    def test_current_track_id_out_of_bounds(self) -> None:
        state = YnisonState(
            player_state={
                "player_queue": {
                    "current_playable_index": 10,
                    "playable_list": [{"playable_id": "track1"}],
                }
            }
        )
        assert state.current_track_id is None

    def test_is_paused(self) -> None:
        state = YnisonState(player_state={"status": {"paused": False}})
        assert state.is_paused is False

    def test_progress_and_duration(self) -> None:
        state = YnisonState(
            player_state={
                "status": {
                    "progress_ms": 30000,
                    "duration_ms": 180000,
                }
            }
        )
        assert state.progress_ms == 30000
        assert state.duration_ms == 180000


# ------------------------------------------------------------------
# YnisonClient internals
# ------------------------------------------------------------------


class TestYnisonClientBuildMethods:
    """Tests for YnisonClient helper/build methods."""

    def test_build_headers(self, client: YnisonClient) -> None:
        headers = client._build_headers()
        assert headers["Authorization"] == "OAuth test-token"
        assert headers["Origin"] == YNISON_ORIGIN
        assert "Sec-WebSocket-Protocol" in headers

    def test_build_headers_with_ticket(self, client: YnisonClient) -> None:
        headers = client._build_headers(redirect_ticket="ticket123", session_id=42)
        proto = headers["Sec-WebSocket-Protocol"]
        assert "Ynison-Redirect-Ticket" in proto
        assert "ticket123" in proto
        assert "42" in proto

    def test_build_ws_protocol_header(self, client: YnisonClient) -> None:
        proto = client._build_ws_protocol_header()
        assert proto.startswith("Bearer, v2, ")
        data = json.loads(proto[len("Bearer, v2, ") :])
        assert data["Ynison-Device-Id"] == "test-device-id"
        device_info = json.loads(data["Ynison-Device-Info"])
        assert device_info["app_name"] == DEFAULT_APP_NAME

    def test_build_device_dict(self, client: YnisonClient) -> None:
        device = client._build_device_dict()
        assert device["info"]["device_id"] == "test-device-id"
        assert device["capabilities"]["can_be_player"] is True
        assert device["capabilities"]["can_be_remote_controller"] is False

    def test_build_initial_state(self, client: YnisonClient) -> None:
        state = client._build_initial_state()
        assert state["status"]["paused"] is True
        assert state["player_queue"]["playable_list"] == []


# ------------------------------------------------------------------
# YnisonClient parse state
# ------------------------------------------------------------------


class TestYnisonClientParseState:
    """Tests for state parsing."""

    def test_parse_state(self, client: YnisonClient) -> None:
        data: dict[str, Any] = {
            "player_state": {
                "status": {"paused": False, "progress_ms": 5000, "duration_ms": 200000},
                "player_queue": {
                    "current_playable_index": 0,
                    "playable_list": [{"playable_id": "track42"}],
                },
            },
            "active_device_id_optional": "test-device-id",
            "devices": [{"info": {"device_id": "test-device-id"}}],
        }
        client._parse_state(data)
        assert client.state.current_track_id == "track42"
        assert client.state.active_device_id == "test-device-id"
        assert client.state.is_paused is False

    def test_parse_state_partial(self, client: YnisonClient) -> None:
        """Partial updates should preserve existing state."""
        client.state.active_device_id = "old-device"
        client._parse_state({"player_state": {"status": {"paused": True}}})
        assert client.state.active_device_id == "old-device"


# ------------------------------------------------------------------
# YnisonClient send methods
# ------------------------------------------------------------------


class TestYnisonClientSend:
    """Tests for send methods."""

    @pytest.fixture(autouse=True)
    def _setup_ws(self, client: YnisonClient) -> None:
        """Set up a mock WebSocket."""
        self.mock_ws = AsyncMock()
        self.mock_ws.closed = False
        client._ws = self.mock_ws
        client._connected = True

    async def test_update_playing_status(self, client: YnisonClient) -> None:
        await client.update_playing_status(1000, 5000, paused=False)
        call_args = self.mock_ws.send_str.call_args[0][0]
        msg = json.loads(call_args)
        status = msg["update_playing_status"]["playing_status"]
        assert status["progress_ms"] == 1000
        assert status["duration_ms"] == 5000
        assert status["paused"] is False

    async def test_update_volume(self, client: YnisonClient) -> None:
        await client.update_volume(0.75)
        msg = json.loads(self.mock_ws.send_str.call_args[0][0])
        assert msg["update_volume_info"]["volume_info"]["volume"] == 0.75

    async def test_update_volume_clamp(self, client: YnisonClient) -> None:
        await client.update_volume(1.5)
        msg = json.loads(self.mock_ws.send_str.call_args[0][0])
        assert msg["update_volume_info"]["volume_info"]["volume"] == 1.0

    async def test_update_active_device(self, client: YnisonClient) -> None:
        await client.update_active_device("device-123")
        msg = json.loads(self.mock_ws.send_str.call_args[0][0])
        assert msg["update_active_device"]["device_id_optional"] == "device-123"

    async def test_send_not_connected(self, client: YnisonClient) -> None:
        """Should silently skip when not connected."""
        client._ws = None
        await client.update_volume(0.5)
        # No exception raised


# ------------------------------------------------------------------
# generate_device_id
# ------------------------------------------------------------------


class TestGenerateDeviceId:
    """Tests for generate_device_id."""

    def test_format(self) -> None:
        device_id = generate_device_id()
        assert len(device_id) == 16
        assert device_id.isalnum()
        assert device_id.islower() or device_id.isdigit()

    def test_uniqueness(self) -> None:
        ids = {generate_device_id() for _ in range(10)}
        assert len(ids) == 10


# ------------------------------------------------------------------
# YnisonClient disconnect
# ------------------------------------------------------------------


class TestYnisonClientDisconnect:
    """Tests for disconnect handling."""

    async def test_disconnect_closes_ws(self, client: YnisonClient) -> None:
        mock_ws = AsyncMock()
        mock_ws.closed = False
        client._ws = mock_ws
        client._connected = True

        await client.disconnect()

        mock_ws.close.assert_called_once()
        assert client._connected is False
        assert client._ws is None

    async def test_disconnect_cancels_tasks(self, client: YnisonClient) -> None:
        # Create a real task that we can cancel
        async def _forever() -> None:
            await asyncio.Event().wait()

        task = asyncio.ensure_future(_forever())
        client._message_task = task

        await client.disconnect()

        assert task.cancelled()

    async def test_disconnect_when_not_connected(self, client: YnisonClient) -> None:
        """Should not raise when already disconnected."""
        await client.disconnect()
