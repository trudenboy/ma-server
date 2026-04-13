"""Tests for the YandexYnisonProvider."""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from music_assistant_models.enums import (
    ContentType,
    PlaybackState,
    ProviderFeature,
    ProviderType,
    StreamType,
)
from music_assistant_models.errors import LoginFailed
from ya_passport_auth import SecretStr

from music_assistant.providers.yandex_ynison.config_helpers import find_sibling_token
from music_assistant.providers.yandex_ynison.constants import (
    CONF_ALLOW_PLAYER_SWITCH,
    CONF_DEVICE_ID,
    CONF_DISPLAY_NAME,
    CONF_PLAYER,
    CONF_TOKEN,
    CONF_X_TOKEN,
    DEFAULT_DISPLAY_NAME,
    PLAYER_ID_AUTO,
)
from music_assistant.providers.yandex_ynison.provider import (
    _PCM_LOSSLESS_PARAMS,
    _PCM_LOSSY_PARAMS,
    YandexYnisonProvider,
    _make_pcm_format,
)
from music_assistant.providers.yandex_ynison.ynison_client import YnisonState


def _make_mock_config(values: dict[str, Any] | None = None) -> MagicMock:
    """Create a mock ProviderConfig."""
    defaults: dict[str, Any] = {
        CONF_TOKEN: "test-music-token",
        CONF_X_TOKEN: None,
        CONF_PLAYER: PLAYER_ID_AUTO,
        CONF_ALLOW_PLAYER_SWITCH: True,
        CONF_DISPLAY_NAME: DEFAULT_DISPLAY_NAME,
        CONF_DEVICE_ID: "test-device-uuid",
        "log_level": "GLOBAL",
    }
    if values:
        defaults.update(values)
    config = MagicMock()
    config.get_value.side_effect = defaults.get
    return config


def _make_mock_mass() -> MagicMock:
    """Create a mock MusicAssistant instance."""
    mass = MagicMock()
    mass.cache_path = "/var/cache/test-cache"

    def _create_task(coro: object) -> MagicMock:
        if asyncio.iscoroutine(coro):
            coro.close()  # prevent RuntimeWarning for unawaited coroutine
        return MagicMock()

    mass.create_task = MagicMock(side_effect=_create_task)
    mass.subscribe = MagicMock(return_value=MagicMock())
    mass.get_providers = MagicMock(return_value=[])
    mass.config.set_raw_provider_config_value = MagicMock()

    # Players
    mass.players.all_players = MagicMock(return_value=[])
    mass.players.get_player = MagicMock(return_value=None)
    mass.players.select_source = AsyncMock()
    mass.players.cmd_stop = AsyncMock()
    mass.players.cmd_volume_set = AsyncMock()
    mass.players.trigger_player_update = MagicMock()

    return mass


def _make_mock_manifest() -> MagicMock:
    """Create a mock ProviderManifest."""
    manifest = MagicMock()
    manifest.domain = "yandex_ynison"
    return manifest


def _make_provider(player_id: str = PLAYER_ID_AUTO) -> YandexYnisonProvider:
    """Create a YandexYnisonProvider with mock dependencies."""
    mass = _make_mock_mass()
    config = _make_mock_config({CONF_PLAYER: player_id})
    manifest = _make_mock_manifest()
    return YandexYnisonProvider(mass, manifest, config, {ProviderFeature.AUDIO_SOURCE})


# ------------------------------------------------------------------
# Provider init
# ------------------------------------------------------------------


class TestProviderInit:
    """Tests for provider initialization."""

    def test_source_details(self) -> None:
        """PluginSource should be configured correctly."""
        provider = _make_provider()

        source = provider.get_source()
        assert source.stream_type == StreamType.CUSTOM
        assert source.audio_format.content_type == ContentType.PCM_S16LE
        assert source.audio_format.sample_rate == 44100
        assert source.audio_format.bit_depth == 16
        assert source.audio_format.channels == 2
        assert source.can_play_pause is False
        assert source.can_seek is False
        assert source.can_next_previous is False
        assert source.on_select is not None

    def test_device_id_persisted(self) -> None:
        """When no device_id in config, should generate and persist."""
        mass = _make_mock_mass()
        config = _make_mock_config({CONF_DEVICE_ID: None})
        manifest = _make_mock_manifest()

        provider = YandexYnisonProvider(mass, manifest, config, {ProviderFeature.AUDIO_SOURCE})

        # Should have generated a device ID and saved it
        mass.config.set_raw_provider_config_value.assert_called()
        assert provider._device_id  # non-empty

    def test_existing_device_id_used(self) -> None:
        """When device_id exists in config, should use it."""
        mass = _make_mock_mass()
        config = _make_mock_config({CONF_DEVICE_ID: "existing-uuid"})
        manifest = _make_mock_manifest()

        provider = YandexYnisonProvider(mass, manifest, config, {ProviderFeature.AUDIO_SOURCE})

        assert provider._device_id == "existing-uuid"


# ------------------------------------------------------------------
# Player selection
# ------------------------------------------------------------------


class TestPlayerSelection:
    """Tests for _get_target_player_id."""

    def test_auto_no_players(self) -> None:
        """Auto mode returns None when no players available."""
        provider = _make_provider()
        assert provider._get_target_player_id() is None

    def test_auto_with_playing_player(self) -> None:
        """Auto mode selects the currently playing player."""
        provider = _make_provider()

        player1 = MagicMock()
        player1.player_id = "player1"
        player1.display_name = "Player 1"
        player1.state.playback_state = PlaybackState.IDLE

        player2 = MagicMock()
        player2.player_id = "player2"
        player2.display_name = "Player 2"
        player2.state.playback_state = PlaybackState.PLAYING

        provider.mass.players.all_players.return_value = [player1, player2]  # type: ignore[attr-defined]

        assert provider._get_target_player_id() == "player2"

    def test_specific_player_exists(self) -> None:
        """Returns configured player when it exists."""
        provider = _make_provider("my-player")
        provider.mass.players.get_player.return_value = MagicMock()  # type: ignore[attr-defined]

        assert provider._get_target_player_id() == "my-player"

    def test_specific_player_missing(self) -> None:
        """Returns None when configured player no longer exists."""
        provider = _make_provider("gone-player")
        provider.mass.players.get_player.return_value = None  # type: ignore[attr-defined]

        assert provider._get_target_player_id() is None

    def test_active_player_takes_priority(self) -> None:
        """Active player takes priority over auto selection."""
        provider = _make_provider()
        provider._active_player_id = "active-one"
        provider.mass.players.get_player.return_value = MagicMock()  # type: ignore[attr-defined]

        assert provider._get_target_player_id() == "active-one"


# ------------------------------------------------------------------
# Source selection
# ------------------------------------------------------------------


class TestSourceSelection:
    """Tests for _on_source_selected."""

    async def test_on_source_selected_sets_active(self) -> None:
        """Selecting source sets the active player."""
        provider = _make_provider()

        provider._source_details.in_use_by = "new-player"
        await provider._on_source_selected()
        assert provider._active_player_id == "new-player"

    async def test_on_source_selected_switching_disabled(self) -> None:
        """Rejects source selection when player switching is disabled."""
        mass = _make_mock_mass()
        config = _make_mock_config({CONF_ALLOW_PLAYER_SWITCH: False})
        manifest = _make_mock_manifest()
        provider = YandexYnisonProvider(mass, manifest, config, {ProviderFeature.AUDIO_SOURCE})

        # Set default player
        provider._default_player_id = "default-player"
        mass.players.get_player.return_value = MagicMock()

        provider._source_details.in_use_by = "other-player"
        with pytest.raises(RuntimeError, match="Player switching is disabled"):
            await provider._on_source_selected()

        # Should have rejected the switch and restored in_use_by
        assert provider._active_player_id is None
        assert provider._source_details.in_use_by == "default-player"


# ------------------------------------------------------------------
# Clear active player
# ------------------------------------------------------------------


class TestClearActivePlayer:
    """Tests for _clear_active_player."""

    def test_clears_state(self) -> None:
        """Clearing active player resets state and triggers update."""
        provider = _make_provider()

        provider._active_player_id = "some-player"
        provider._source_details.in_use_by = "some-player"

        provider._clear_active_player()

        assert provider._active_player_id is None
        assert provider._source_details.in_use_by is None  # type: ignore[unreachable]
        provider.mass.players.trigger_player_update.assert_called_with("some-player")


# ------------------------------------------------------------------
# Provider matching
# ------------------------------------------------------------------


class TestProviderMatching:
    """Tests for _check_yandex_provider_match."""

    async def test_finds_yandex_music_provider(self) -> None:
        """Links to Yandex Music provider and enables playback control."""
        provider = _make_provider()

        mock_ym = MagicMock()
        mock_ym.domain = "yandex_music"
        mock_ym.type = ProviderType.MUSIC
        provider.mass.get_providers.return_value = [mock_ym]  # type: ignore[attr-defined]

        await provider._check_yandex_provider_match()

        assert provider._yandex_provider is mock_ym
        assert provider._source_details.can_play_pause is True
        assert provider._source_details.on_play is not None

    async def test_no_matching_provider(self) -> None:
        """No linked provider disables playback control."""
        provider = _make_provider()

        provider.mass.get_providers.return_value = []  # type: ignore[attr-defined]
        await provider._check_yandex_provider_match()

        assert provider._yandex_provider is None
        assert provider._source_details.can_play_pause is False


# ------------------------------------------------------------------
# Ynison state handling
# ------------------------------------------------------------------


class TestYnisonStateHandling:
    """Tests for _handle_ynison_state."""

    async def test_activates_on_our_device(self) -> None:
        """Activates playback when Ynison reports our device as active."""
        provider = _make_provider()

        # Setup a target player
        player = MagicMock()
        player.player_id = "player1"
        player.display_name = "Player 1"
        provider.mass.players.all_players.return_value = [player]  # type: ignore[attr-defined]
        provider.mass.players.get_player.return_value = player  # type: ignore[attr-defined]

        state = YnisonState(
            active_device_id=provider._device_id,
            player_state={
                "status": {"paused": False, "progress_ms": 5000, "duration_ms": 200000},
                "player_queue": {
                    "current_playable_index": 0,
                    "playable_list": [{"playable_id": "track1"}],
                },
            },
        )

        await provider._handle_ynison_state(state)

        assert provider._active_player_id == "player1"

    async def test_clears_on_device_switch(self) -> None:
        """Clears active player when device switches away."""
        provider = _make_provider()

        provider._active_player_id = "player1"
        provider._source_details.in_use_by = "player1"

        state = YnisonState(active_device_id="other-device-id")
        await provider._handle_ynison_state(state)

        assert provider._active_player_id is None
        assert provider._source_details.in_use_by is None  # type: ignore[unreachable]

    async def test_seek_detected_from_ynison(self) -> None:
        """Detects seek from Yandex app via progress drift."""
        provider = _make_provider()

        player = MagicMock()
        player.player_id = "player1"
        provider.mass.players.all_players.return_value = [player]  # type: ignore[attr-defined]
        provider.mass.players.get_player.return_value = player  # type: ignore[attr-defined]

        def _make_state(progress_ms: int) -> YnisonState:
            return YnisonState(
                active_device_id=provider._device_id,
                player_state={
                    "status": {
                        "paused": False,
                        "progress_ms": progress_ms,
                        "duration_ms": 200000,
                    },
                    "player_queue": {
                        "current_playable_index": 0,
                        "playable_list": [{"playable_id": "track1"}],
                    },
                },
            )

        # First state — track starts at 0ms
        await provider._handle_ynison_state(_make_state(0))
        assert provider._current_streaming_track_id == "track1"  # set eagerly on detection

        # Expire the grace period so the seek detection isn't suppressed
        provider._seek_grace_until = 0.0

        # Second state — seek to 60s (drift 60000ms > 2000ms)
        await provider._handle_ynison_state(_make_state(60000))
        assert provider._seek_position_ms == 60000
        assert provider._track_changed_event.is_set()

        # Verify force_update=True was used so the server sends a full
        # PLAYER_UPDATED event (not just a lightweight elapsed-time one)
        provider.mass.players.trigger_player_update.assert_called_with("player1", force_update=True)  # type: ignore[attr-defined]

    async def test_seek_grace_period_after_track_change(self) -> None:
        """Seek detection is suppressed during grace period after track change."""
        provider = _make_provider()

        player = MagicMock()
        player.player_id = "player1"
        provider.mass.players.all_players.return_value = [player]  # type: ignore[attr-defined]
        provider.mass.players.get_player.return_value = player  # type: ignore[attr-defined]

        def _make_state(progress_ms: int) -> YnisonState:
            return YnisonState(
                active_device_id=provider._device_id,
                player_state={
                    "status": {
                        "paused": False,
                        "progress_ms": progress_ms,
                        "duration_ms": 200000,
                    },
                    "player_queue": {
                        "current_playable_index": 0,
                        "playable_list": [{"playable_id": "track1"}],
                    },
                },
            )

        # Track starts — sets grace period
        await provider._handle_ynison_state(_make_state(0))
        assert provider._seek_grace_until > 0

        # Echo with progress=0 arrives during grace period — should NOT
        # trigger seek even though drift calculation would exceed threshold
        provider._track_changed_event.clear()
        await provider._handle_ynison_state(_make_state(0))
        assert provider._seek_position_ms == 0  # unchanged
        assert not provider._track_changed_event.is_set()  # no false seek

    async def test_progress_throttled_update(self) -> None:
        """Regular progress updates trigger player update with throttling."""
        provider = _make_provider()

        player = MagicMock()
        player.player_id = "player1"
        provider.mass.players.all_players.return_value = [player]  # type: ignore[attr-defined]
        provider.mass.players.get_player.return_value = player  # type: ignore[attr-defined]

        state = YnisonState(
            active_device_id=provider._device_id,
            player_state={
                "status": {
                    "paused": False,
                    "progress_ms": 5000,
                    "duration_ms": 200000,
                },
                "player_queue": {
                    "current_playable_index": 0,
                    "playable_list": [{"playable_id": "track1"}],
                },
            },
        )

        # First call — significant (new track) → always triggers
        await provider._handle_ynison_state(state)
        call_count_1 = provider.mass.players.trigger_player_update.call_count  # type: ignore[attr-defined]

        # Simulate same track still playing (no seek, no track change)
        provider._last_sent_to_ynison_ms = 5000

        state2 = YnisonState(
            active_device_id=provider._device_id,
            player_state={
                "status": {
                    "paused": False,
                    "progress_ms": 6000,
                    "duration_ms": 200000,
                },
                "player_queue": {
                    "current_playable_index": 0,
                    "playable_list": [{"playable_id": "track1"}],
                },
            },
        )

        # Second call shortly after — throttled, no trigger
        await provider._handle_ynison_state(state2)
        call_count_2 = provider.mass.players.trigger_player_update.call_count  # type: ignore[attr-defined]

        # Force the throttle to expire
        provider._last_player_update_time = 0.0
        await provider._handle_ynison_state(state2)
        call_count_3 = provider.mass.players.trigger_player_update.call_count  # type: ignore[attr-defined]

        # First call triggered, second was throttled, third triggered
        assert call_count_1 >= 1
        assert call_count_2 == call_count_1
        assert call_count_3 > call_count_2

        # Regular (non-seek) updates should NOT use force_update
        provider.mass.players.trigger_player_update.assert_called_with(  # type: ignore[attr-defined]
            "player1", force_update=False
        )

    async def test_duration_updated_from_stream_details(self) -> None:
        """Duration is updated from stream_details and pushed to Ynison."""
        provider = _make_provider()
        provider._source_details.in_use_by = "player1"
        mock_ynison = MagicMock()
        mock_ynison.update_playing_status = AsyncMock()
        mock_ynison.state.is_paused = False
        provider._ynison = mock_ynison

        stream_details = MagicMock()
        stream_details.duration = 185  # seconds

        await provider._update_metadata_from_stream(stream_details, seek_ms=30000)

        meta = provider._source_details.metadata
        assert meta is not None
        assert meta.duration == 185
        assert meta.elapsed_time == 30  # 30000ms → 30s
        assert provider._actual_duration_ms == 185000
        provider.mass.players.trigger_player_update.assert_called_once_with(  # type: ignore[attr-defined]
            "player1", force_update=True
        )
        # Real duration pushed to Ynison
        mock_ynison.update_playing_status.assert_awaited_once_with(
            progress_ms=30000, duration_ms=185000, paused=False
        )

    async def test_signal_track_completion_advances_index(self) -> None:
        """Track completion advances index and reports status."""
        provider = _make_provider()
        mock_ynison = MagicMock()
        mock_ynison.state = YnisonState(
            active_device_id=provider._device_id,
            player_state={
                "status": {"paused": False, "progress_ms": 180000, "duration_ms": 200000},
                "player_queue": {
                    "current_playable_index": 0,
                    "playable_list": [{"playable_id": "t1"}, {"playable_id": "t2"}],
                    "entity_id": "playlist:123",
                    "entity_type": "PLAYLIST",
                },
            },
        )
        mock_ynison.update_playing_status = AsyncMock()
        mock_ynison.update_player_state = AsyncMock()
        provider._ynison = mock_ynison

        await provider._signal_track_completion()

        # 1. Reports progress=duration
        mock_ynison.update_playing_status.assert_awaited_once_with(
            progress_ms=200000, duration_ms=200000, paused=False
        )
        # 2. Advances current_playable_index by 1
        call_args = mock_ynison.update_player_state.call_args
        sent_state = call_args.kwargs["player_state"]
        assert sent_state["player_queue"]["current_playable_index"] == 1
        assert sent_state["status"]["progress_ms"] == 0
        assert sent_state["status"]["paused"] is False
        # Resets actual duration for next track
        assert provider._actual_duration_ms == 0

    async def test_signal_track_completion_no_send_full_state(self) -> None:
        """Track completion never sends full state reset."""
        provider = _make_provider()
        mock_ynison = MagicMock()
        mock_ynison.state = YnisonState(
            active_device_id=provider._device_id,
            player_state={
                "status": {"paused": False, "progress_ms": 180000, "duration_ms": 200000},
                "player_queue": {
                    "current_playable_index": 0,
                    "playable_list": [{"playable_id": "t1"}, {"playable_id": "t2"}],
                    "entity_id": "playlist:123",
                },
            },
        )
        mock_ynison.update_playing_status = AsyncMock()
        mock_ynison.update_player_state = AsyncMock()
        mock_ynison.send_full_state = AsyncMock()
        provider._ynison = mock_ynison

        await provider._signal_track_completion()

        # Must NOT send full state reset
        mock_ynison.send_full_state.assert_not_called()

    async def test_signal_track_completion_uses_actual_duration(self) -> None:
        """Track completion prefers _actual_duration_ms over stale state.duration_ms."""
        provider = _make_provider()
        provider._actual_duration_ms = 300000
        mock_ynison = MagicMock()
        mock_ynison.state = YnisonState(
            active_device_id=provider._device_id,
            player_state={
                "status": {"paused": False, "progress_ms": 180000, "duration_ms": 200000},
                "player_queue": {
                    "current_playable_index": 0,
                    "playable_list": [{"playable_id": "t1"}, {"playable_id": "t2"}],
                },
            },
        )
        mock_ynison.update_playing_status = AsyncMock()
        mock_ynison.update_player_state = AsyncMock()
        provider._ynison = mock_ynison

        await provider._signal_track_completion()

        mock_ynison.update_playing_status.assert_awaited_once_with(
            progress_ms=300000, duration_ms=300000, paused=False
        )

    async def test_signal_track_completion_radio_replenishes_queue(self) -> None:
        """At end of RADIO queue, fetches more tracks via YM API and advances."""
        provider = _make_provider()
        mock_ynison = MagicMock()
        mock_ynison.state = YnisonState(
            active_device_id=provider._device_id,
            player_state={
                "status": {"paused": False, "progress_ms": 200000, "duration_ms": 215000},
                "player_queue": {
                    "current_playable_index": 1,
                    "playable_list": [
                        {"playable_id": "t1", "from": "radio-src"},
                        {"playable_id": "t2", "from": "radio-src"},
                    ],
                    "entity_id": "user:onyourwave",
                    "entity_type": "RADIO",
                },
            },
        )
        mock_ynison.update_playing_status = AsyncMock()
        mock_ynison.update_player_state = AsyncMock()
        provider._ynison = mock_ynison

        # Mock YM provider returning new tracks
        mock_track = MagicMock()
        mock_track.id = "t3"
        mock_track.title = "New Track"
        mock_track.albums = [MagicMock(id="a3")]
        mock_track.cover_uri = "cover3.jpg"

        mock_client = MagicMock()
        mock_client.get_rotor_station_tracks = AsyncMock(return_value=([mock_track], "batch-123"))
        mock_ym_provider = MagicMock()
        mock_ym_provider.client = mock_client
        provider._yandex_provider = mock_ym_provider

        await provider._signal_track_completion()

        # Fetched tracks from station
        mock_client.get_rotor_station_tracks.assert_awaited_once_with("user:onyourwave", queue="t2")
        # Advanced index to 2 with expanded playable_list
        call_args = mock_ynison.update_player_state.call_args
        sent_state = call_args.kwargs["player_state"]
        assert sent_state["player_queue"]["current_playable_index"] == 2
        expanded = sent_state["player_queue"]["playable_list"]
        assert len(expanded) == 3
        assert expanded[2]["playable_id"] == "t3"
        assert expanded[2]["title"] == "New Track"
        assert expanded[2]["from"] == "radio-src"

    async def test_signal_track_completion_radio_no_provider(self) -> None:
        """At end of queue without YM provider, does not crash."""
        provider = _make_provider()
        mock_ynison = MagicMock()
        mock_ynison.state = YnisonState(
            active_device_id=provider._device_id,
            player_state={
                "status": {"paused": False, "progress_ms": 200000, "duration_ms": 215000},
                "player_queue": {
                    "current_playable_index": 1,
                    "playable_list": [
                        {"playable_id": "t1"},
                        {"playable_id": "t2"},
                    ],
                    "entity_id": "user:onyourwave",
                    "entity_type": "RADIO",
                },
            },
        )
        mock_ynison.update_playing_status = AsyncMock()
        mock_ynison.update_player_state = AsyncMock()
        provider._ynison = mock_ynison
        provider._yandex_provider = None

        await provider._signal_track_completion()

        # Status reported
        mock_ynison.update_playing_status.assert_awaited_once()
        # Cannot advance — no provider to fetch tracks
        mock_ynison.update_player_state.assert_not_called()

    async def test_prefetch_on_second_to_last_track(self) -> None:
        """Pre-fetches tracks when playing second-to-last item in queue."""
        provider = _make_provider()
        mock_ynison = MagicMock()
        mock_ynison.connected = True
        mock_ynison.update_player_state = AsyncMock()
        # 4 tracks, currently at index 2 (second-to-last)
        mock_ynison.state = YnisonState(
            active_device_id=provider._device_id,
            player_state={
                "status": {"paused": False, "progress_ms": 10000, "duration_ms": 200000},
                "player_queue": {
                    "current_playable_index": 2,
                    "playable_list": [
                        {"playable_id": "t1", "from": "src"},
                        {"playable_id": "t2", "from": "src"},
                        {"playable_id": "t3", "from": "src"},
                        {"playable_id": "t4", "from": "src"},
                    ],
                    "entity_id": "user:onyourwave",
                    "entity_type": "RADIO",
                },
            },
        )
        provider._ynison = mock_ynison

        mock_track = MagicMock()
        mock_track.id = "t5"
        mock_track.title = "Prefetched"
        mock_track.albums = [MagicMock(id="a5")]
        mock_track.cover_uri = "cover5.jpg"

        mock_client = MagicMock()
        mock_client.get_rotor_station_tracks = AsyncMock(return_value=([mock_track], "batch-pfx"))
        mock_ym_provider = MagicMock()
        mock_ym_provider.client = mock_client
        provider._yandex_provider = mock_ym_provider

        # Trigger prefetch
        provider._maybe_prefetch(
            2,
            mock_ynison.state.player_state["player_queue"]["playable_list"],
            "user:onyourwave",
            "RADIO",
        )
        assert provider._prefetch_task is not None
        await provider._prefetch_task

        # Prefetched list should contain old + new
        assert provider._prefetched_list is not None
        assert len(provider._prefetched_list) == 5
        assert provider._prefetched_list[4]["playable_id"] == "t5"

    async def test_signal_completion_uses_prefetched(self) -> None:
        """Track completion uses pre-fetched data instead of making API call."""
        provider = _make_provider()
        mock_ynison = MagicMock()
        mock_ynison.state = YnisonState(
            active_device_id=provider._device_id,
            player_state={
                "status": {"paused": False, "progress_ms": 200000, "duration_ms": 215000},
                "player_queue": {
                    "current_playable_index": 3,
                    "playable_list": [
                        {"playable_id": "t1"},
                        {"playable_id": "t2"},
                        {"playable_id": "t3"},
                        {"playable_id": "t4"},
                    ],
                    "entity_id": "user:onyourwave",
                    "entity_type": "RADIO",
                },
            },
        )
        mock_ynison.update_playing_status = AsyncMock()
        mock_ynison.update_player_state = AsyncMock()
        provider._ynison = mock_ynison

        # Simulate pre-fetched data
        prefetched = [
            {"playable_id": "t1"},
            {"playable_id": "t2"},
            {"playable_id": "t3"},
            {"playable_id": "t4"},
            {"playable_id": "t5"},
        ]
        provider._prefetched_list = prefetched

        mock_client = MagicMock()
        mock_client.get_rotor_station_tracks = AsyncMock()
        mock_ym_provider = MagicMock()
        mock_ym_provider.client = mock_client
        provider._yandex_provider = mock_ym_provider

        await provider._signal_track_completion()

        # Should NOT have called API — used prefetched
        mock_client.get_rotor_station_tracks.assert_not_awaited()
        # Advanced with prefetched list
        call_args = mock_ynison.update_player_state.call_args
        sent_state = call_args.kwargs["player_state"]
        assert sent_state["player_queue"]["current_playable_index"] == 4
        assert len(sent_state["player_queue"]["playable_list"]) == 5
        # Prefetch consumed
        assert provider._prefetched_list is None

    async def test_best_duration_prefers_actual(self) -> None:
        """_best_duration_ms prefers _actual_duration_ms over state.duration_ms."""
        provider = _make_provider()
        mock_ynison = MagicMock()
        mock_ynison.state = YnisonState(
            active_device_id=provider._device_id,
            player_state={
                "status": {"duration_ms": 200000},
            },
        )
        provider._ynison = mock_ynison

        # Fallback to state when actual is 0
        assert provider._best_duration_ms() == 200000

        # Prefer actual when set
        provider._actual_duration_ms = 300000
        assert provider._best_duration_ms() == 300000

        # Without ynison, only actual
        provider._ynison = None
        assert provider._best_duration_ms() == 300000
        provider._actual_duration_ms = 0
        assert provider._best_duration_ms() == 0

    async def test_wait_for_track_change_ignores_echo(self) -> None:
        """_wait_for_track_change should ignore echoes and wait for actual change."""
        provider = _make_provider()
        mock_ynison = MagicMock()
        mock_ynison.state = YnisonState(
            active_device_id=provider._device_id,
            player_state={
                "status": {"progress_ms": 248000, "duration_ms": 248000},
                "player_queue": {
                    "current_playable_index": 5,
                    "playable_list": [{"playable_id": "old_track"}],
                    "entity_id": "user:onyourwave",
                    "entity_type": "RADIO",
                },
            },
        )
        provider._ynison = mock_ynison

        async def simulate_echo_then_change() -> None:
            await asyncio.sleep(0.01)
            # First event: echo with same track (should be ignored)
            provider._track_changed_event.set()
            await asyncio.sleep(0.01)
            # Second event: actual track change
            mock_ynison.state = YnisonState(
                active_device_id=provider._device_id,
                player_state={
                    "status": {"progress_ms": 0, "duration_ms": 0},
                    "player_queue": {
                        "current_playable_index": 6,
                        "playable_list": [{"playable_id": "new_track"}],
                        "entity_id": "user:onyourwave",
                        "entity_type": "RADIO",
                    },
                },
            )
            provider._track_changed_event.set()

        asyncio.create_task(simulate_echo_then_change())
        result = await provider._wait_for_track_change("old_track", timeout=5.0)
        assert result is True

    async def test_wait_for_track_change_timeout(self) -> None:
        """_wait_for_track_change returns False on timeout."""
        provider = _make_provider()
        mock_ynison = MagicMock()
        mock_ynison.state = YnisonState(
            active_device_id=provider._device_id,
            player_state={
                "status": {"progress_ms": 248000},
                "player_queue": {
                    "current_playable_index": 5,
                    "playable_list": [{"playable_id": "old_track"}],
                    "entity_id": "user:onyourwave",
                    "entity_type": "RADIO",
                },
            },
        )
        provider._ynison = mock_ynison

        result = await provider._wait_for_track_change("old_track", timeout=0.1)
        assert result is False


# ------------------------------------------------------------------
# ------------------------------------------------------------------
# PCM normalization (per-track ffmpeg → adaptive PCM)
# ------------------------------------------------------------------


class TestPCMNormalization:
    """Tests for per-track ffmpeg normalization to PCM."""

    async def test_stream_track_always_uses_ffmpeg(self) -> None:
        """_stream_track always normalizes through ffmpeg, even without seek."""
        provider = _make_provider()
        provider._source_details.in_use_by = "player1"

        mock_yandex = MagicMock()
        sd = MagicMock()
        sd.duration = 200
        sd.audio_format = MagicMock()
        mock_yandex.get_stream_details = AsyncMock(return_value=sd)

        async def _fake_audio_stream(_details: object) -> Any:
            yield b"raw-cdn-data"

        mock_yandex.get_audio_stream = _fake_audio_stream
        provider._yandex_provider = mock_yandex

        mock_ynison = MagicMock()
        mock_ynison.update_playing_status = AsyncMock()
        mock_ynison.state.is_paused = False
        provider._ynison = mock_ynison

        async def _fake_ffmpeg(**_kwargs: object) -> Any:
            yield b"pcm-normalized"

        with patch(
            "music_assistant.providers.yandex_ynison.provider.get_ffmpeg_stream",
            side_effect=_fake_ffmpeg,
        ) as mock_ffmpeg:
            collected: list[bytes] = []
            async for chunk in provider._stream_track("track:123"):
                collected.append(chunk)

        assert collected == [b"pcm-normalized"]
        mock_ffmpeg.assert_called_once()
        call_kwargs = mock_ffmpeg.call_args
        # Default (no YM provider linked) → lossy profile
        assert call_kwargs.kwargs["output_format"] == provider._normalized_format
        assert call_kwargs.kwargs["output_format"].content_type == ContentType.PCM_S16LE
        # No seek args when seek_ms=0, but readrate is always present
        args = call_kwargs.kwargs.get("extra_input_args", [])
        assert "-readrate" in args
        assert "-ss" not in args

    async def test_stream_track_seek_adds_ss_arg(self) -> None:
        """With seek > 0, _stream_track adds -ss to ffmpeg args."""
        provider = _make_provider()
        provider._source_details.in_use_by = "player1"

        mock_yandex = MagicMock()
        sd = MagicMock()
        sd.duration = 200
        sd.audio_format = MagicMock()
        mock_yandex.get_stream_details = AsyncMock(return_value=sd)

        async def _fake_audio_stream(_details: object) -> Any:
            yield b"raw-data"

        mock_yandex.get_audio_stream = _fake_audio_stream
        provider._yandex_provider = mock_yandex

        mock_ynison = MagicMock()
        mock_ynison.update_playing_status = AsyncMock()
        mock_ynison.state.is_paused = False
        provider._ynison = mock_ynison

        async def _fake_ffmpeg(**_kwargs: object) -> Any:
            yield b"pcm-seeked"

        with patch(
            "music_assistant.providers.yandex_ynison.provider.get_ffmpeg_stream",
            side_effect=_fake_ffmpeg,
        ) as mock_ffmpeg:
            collected: list[bytes] = []
            async for chunk in provider._stream_track("track:123", seek_ms=5000):
                collected.append(chunk)

        assert collected == [b"pcm-seeked"]
        mock_ffmpeg.assert_called_once()
        call_kwargs = mock_ffmpeg.call_args
        args = call_kwargs.kwargs.get("extra_input_args", [])
        assert "-ss" in args
        assert "-readrate" in args

    async def test_default_format_is_pcm_s16le(self) -> None:
        """Default PluginSource audio_format is PCM s16le (lossy profile)."""
        provider = _make_provider()
        source = provider.get_source()
        assert source.audio_format.content_type == ContentType.PCM_S16LE
        assert source.audio_format.sample_rate == 44100
        assert source.audio_format.bit_depth == 16
        assert source.audio_format.channels == 2

    async def test_superb_quality_uses_lossless_profile(self) -> None:
        """When YM quality=superb, format switches to PCM s24le/48kHz."""
        provider = _make_provider()

        mock_yandex = MagicMock()
        mock_yandex.domain = "yandex_music"
        mock_yandex.type = ProviderType.MUSIC
        mock_yandex.config.get_value.side_effect = lambda k: "superb" if k == "quality" else None
        provider._yandex_provider = mock_yandex
        provider._update_normalized_format()

        assert provider._normalized_format.content_type == ContentType.PCM_S24LE
        assert provider._normalized_format.sample_rate == 48000
        assert provider._normalized_format.bit_depth == 24
        assert provider._source_details.audio_format == provider._normalized_format

    async def test_balanced_quality_uses_lossy_profile(self) -> None:
        """When YM quality=balanced, format stays PCM s16le/44.1kHz."""
        provider = _make_provider()

        mock_yandex = MagicMock()
        mock_yandex.domain = "yandex_music"
        mock_yandex.type = ProviderType.MUSIC
        mock_yandex.config.get_value.side_effect = lambda k: "balanced" if k == "quality" else None
        provider._yandex_provider = mock_yandex
        provider._update_normalized_format()

        assert provider._normalized_format.content_type == ContentType.PCM_S16LE
        assert provider._normalized_format.sample_rate == 44100
        assert provider._normalized_format.bit_depth == 16

    async def test_audio_format_not_modified_by_stream(self) -> None:
        """PluginSource audio_format stays fixed (not updated from stream)."""
        provider = _make_provider()
        provider._source_details.in_use_by = "player1"

        mock_yandex = MagicMock()
        sd = MagicMock()
        sd.duration = 200
        sd.audio_format = MagicMock()  # different format
        mock_yandex.get_stream_details = AsyncMock(return_value=sd)

        async def _fake_audio_stream(_details: object) -> Any:
            yield b"data"

        mock_yandex.get_audio_stream = _fake_audio_stream
        provider._yandex_provider = mock_yandex

        mock_ynison = MagicMock()
        mock_ynison.update_playing_status = AsyncMock()
        mock_ynison.state.is_paused = False
        provider._ynison = mock_ynison

        async def _fake_ffmpeg(**_kwargs: object) -> Any:
            yield b"pcm"

        original_format = provider._normalized_format

        with patch(
            "music_assistant.providers.yandex_ynison.provider.get_ffmpeg_stream",
            side_effect=_fake_ffmpeg,
        ):
            async for _ in provider._stream_track("track:123"):
                pass

        # audio_format should still be _normalized_format, not sd.audio_format
        assert provider._source_details.audio_format is original_format

    async def test_stream_track_api_error_returns_empty(self) -> None:
        """If get_stream_details fails, _stream_track yields nothing."""
        provider = _make_provider()
        mock_yandex = MagicMock()
        mock_yandex.get_stream_details = AsyncMock(side_effect=Exception("API error"))
        provider._yandex_provider = mock_yandex

        collected: list[bytes] = []
        async for chunk in provider._stream_track("track:bad"):
            collected.append(chunk)

        assert collected == []


# ------------------------------------------------------------------
# Pre-buffer
# ------------------------------------------------------------------


class TestPreBuffer:
    """Tests for pre-buffer system."""

    async def test_prebuffer_happy_path(self) -> None:
        """Prebuffer fills queue via ffmpeg normalization."""
        provider = _make_provider()
        provider._source_details.in_use_by = "player1"

        mock_yandex = MagicMock()
        sd = MagicMock()
        sd.duration = 200
        sd.audio_format = MagicMock()
        mock_yandex.get_stream_details = AsyncMock(return_value=sd)

        async def _fake_audio_stream(_details: object) -> Any:
            yield b"raw-cdn"

        mock_yandex.get_audio_stream = _fake_audio_stream
        provider._yandex_provider = mock_yandex

        mock_ynison = MagicMock()
        mock_ynison.update_playing_status = AsyncMock()
        mock_ynison.state.is_paused = False
        provider._ynison = mock_ynison

        pcm_chunks = [b"pcm-1", b"pcm-2", b"pcm-3"]

        async def _fake_ffmpeg(**_kwargs: object) -> Any:
            for c in pcm_chunks:
                yield c

        # Use real create_task
        provider.mass.create_task = lambda coro: asyncio.get_event_loop().create_task(coro)  # type: ignore[method-assign, assignment, misc]

        with patch(
            "music_assistant.providers.yandex_ynison.provider.get_ffmpeg_stream",
            side_effect=_fake_ffmpeg,
        ):
            await provider._start_prebuffer("track:1")
            assert provider._prebuffer is not None
            assert provider._prebuffer.track_id == "track:1"

            # Wait for fill task to finish
            assert provider._prebuffer.task is not None
            await provider._prebuffer.task

        # Yield from prebuffer
        collected: list[bytes] = []
        async for chunk in provider._yield_from_prebuffer():
            collected.append(chunk)

        assert collected == pcm_chunks

    async def test_prebuffer_cancel(self) -> None:
        """Cancelling prebuffer stops the fill task."""
        provider = _make_provider()

        mock_yandex = MagicMock()

        async def _slow_stream(_details: object) -> Any:
            for i in range(100):
                await asyncio.sleep(0.1)
                yield f"chunk-{i}".encode()

        sd = MagicMock()
        sd.duration = 200
        sd.audio_format = MagicMock()
        mock_yandex.get_stream_details = AsyncMock(return_value=sd)
        mock_yandex.get_audio_stream = _slow_stream
        provider._yandex_provider = mock_yandex

        mock_ynison = MagicMock()
        mock_ynison.update_playing_status = AsyncMock()
        mock_ynison.state.is_paused = False
        provider._ynison = mock_ynison

        async def _slow_ffmpeg(**_kwargs: object) -> Any:
            for i in range(100):
                await asyncio.sleep(0.1)
                yield f"pcm-{i}".encode()

        provider.mass.create_task = lambda coro: asyncio.get_event_loop().create_task(coro)  # type: ignore[method-assign, assignment, misc]

        with patch(
            "music_assistant.providers.yandex_ynison.provider.get_ffmpeg_stream",
            side_effect=_slow_ffmpeg,
        ):
            await provider._start_prebuffer("track:1")
            assert provider._prebuffer is not None
            # Let it start filling
            await asyncio.sleep(0.05)
            await provider._prebuffer.cancel()
            assert provider._prebuffer.task is not None
            assert provider._prebuffer.task.done()

    async def test_prebuffer_error_fallback(self) -> None:
        """When prebuffer errors, get_audio_stream falls back to _stream_track."""
        provider = _make_provider()

        mock_yandex = MagicMock()
        mock_yandex.get_stream_details = AsyncMock(side_effect=Exception("API failed"))
        provider._yandex_provider = mock_yandex

        mock_ynison = MagicMock()
        mock_ynison.update_playing_status = AsyncMock()
        mock_ynison.state.is_paused = False
        provider._ynison = mock_ynison

        provider.mass.create_task = lambda coro: asyncio.get_event_loop().create_task(coro)  # type: ignore[method-assign, assignment, misc]

        await provider._start_prebuffer("track:err")
        assert provider._prebuffer is not None
        assert provider._prebuffer.task is not None
        await provider._prebuffer.task

        # Should have error set
        assert provider._prebuffer.error is not None

    async def test_prebuffer_replaced_on_new_track(self) -> None:
        """Starting a new prebuffer cancels the old one."""
        provider = _make_provider()

        mock_yandex = MagicMock()
        sd = MagicMock()
        sd.duration = 200
        sd.audio_format = MagicMock()
        mock_yandex.get_stream_details = AsyncMock(return_value=sd)

        async def _fake_audio_stream(_details: object) -> Any:
            yield b"data"

        mock_yandex.get_audio_stream = _fake_audio_stream
        provider._yandex_provider = mock_yandex

        mock_ynison = MagicMock()
        mock_ynison.update_playing_status = AsyncMock()
        mock_ynison.state.is_paused = False
        provider._ynison = mock_ynison

        async def _fake_ffmpeg(**_kwargs: object) -> Any:
            yield b"pcm"

        provider.mass.create_task = lambda coro: asyncio.get_event_loop().create_task(coro)  # type: ignore[method-assign, assignment, misc]

        with patch(
            "music_assistant.providers.yandex_ynison.provider.get_ffmpeg_stream",
            side_effect=_fake_ffmpeg,
        ):
            await provider._start_prebuffer("track:1")
            first_prebuffer = provider._prebuffer
            assert first_prebuffer is not None

            await provider._start_prebuffer("track:2")
            assert provider._prebuffer is not None
            assert provider._prebuffer.track_id == "track:2"
            # First task should be cancelled
            assert first_prebuffer.task is not None
            assert first_prebuffer.task.done()

    async def test_clear_active_player_cancels_prebuffer(self) -> None:
        """_clear_active_player should cancel and clear prebuffer."""
        provider = _make_provider()
        provider._source_details.in_use_by = "player1"

        mock_yandex = MagicMock()
        sd = MagicMock()
        sd.duration = 200
        sd.audio_format = MagicMock()
        mock_yandex.get_stream_details = AsyncMock(return_value=sd)

        async def _fake_audio_stream(_details: object) -> Any:
            yield b"data"

        mock_yandex.get_audio_stream = _fake_audio_stream
        provider._yandex_provider = mock_yandex

        mock_ynison = MagicMock()
        mock_ynison.update_playing_status = AsyncMock()
        mock_ynison.state.is_paused = False
        provider._ynison = mock_ynison

        async def _fake_ffmpeg(**_kwargs: object) -> Any:
            yield b"pcm"

        provider.mass.create_task = lambda coro: asyncio.get_event_loop().create_task(coro)  # type: ignore[method-assign, assignment, misc]

        with patch(
            "music_assistant.providers.yandex_ynison.provider.get_ffmpeg_stream",
            side_effect=_fake_ffmpeg,
        ):
            await provider._start_prebuffer("track:1")
            assert provider._prebuffer is not None

        provider._clear_active_player()
        assert provider._prebuffer is None


class TestResolveToken:
    """Tests for _resolve_token self-healing logic."""

    async def test_prefers_x_token_refresh(self) -> None:
        """When x_token is available, refreshes music token instead of using stored one."""
        provider = _make_provider()
        provider.config = _make_mock_config(
            {CONF_TOKEN: "old-stored-token", CONF_X_TOKEN: "my-x-token"}
        )

        with patch(
            "music_assistant.providers.yandex_ynison.provider.refresh_music_token",
            new_callable=AsyncMock,
            return_value=SecretStr("fresh-music-token"),
        ) as mock_refresh:
            result = await provider._resolve_token()

        assert result.get_secret() == "fresh-music-token"
        mock_refresh.assert_awaited_once()

    async def test_falls_back_to_stored_on_refresh_failure(self) -> None:
        """Falls back to stored token when x_token refresh fails with non-auth error."""
        provider = _make_provider()
        provider.config = _make_mock_config(
            {CONF_TOKEN: "old-stored-token", CONF_X_TOKEN: "my-x-token"}
        )

        with patch(
            "music_assistant.providers.yandex_ynison.provider.refresh_music_token",
            new_callable=AsyncMock,
            side_effect=TimeoutError("network"),
        ):
            result = await provider._resolve_token()

        assert result.get_secret() == "old-stored-token"

    async def test_uses_stored_token_when_no_x_token(self) -> None:
        """Returns stored music token when x_token is not configured."""
        provider = _make_provider()
        provider.config = _make_mock_config({CONF_TOKEN: "stored", CONF_X_TOKEN: None})

        result = await provider._resolve_token()

        assert result.get_secret() == "stored"

    async def test_raises_when_no_tokens(self) -> None:
        """Raises LoginFailed when neither token is configured."""
        provider = _make_provider()
        provider.config = _make_mock_config({CONF_TOKEN: None, CONF_X_TOKEN: None})

        with pytest.raises(LoginFailed, match="No Yandex Music token"):
            await provider._resolve_token()


# ------------------------------------------------------------------
# Instance name postfix
# ------------------------------------------------------------------


class TestInstanceNamePostfix:
    """Tests for instance_name_postfix property."""

    def test_returns_custom_display_name(self) -> None:
        """Returns display_name when it differs from the default."""
        config = _make_mock_config({CONF_DISPLAY_NAME: "Living Room"})
        mass = _make_mock_mass()
        manifest = _make_mock_manifest()
        provider = YandexYnisonProvider(mass, manifest, config, {ProviderFeature.AUDIO_SOURCE})
        assert provider.instance_name_postfix == "Living Room"

    def test_returns_none_for_default_name(self) -> None:
        """Returns None when display_name is the default (falls back to index)."""
        provider = _make_provider()
        assert provider.instance_name_postfix is None


# ------------------------------------------------------------------
# Sibling token detection
# ------------------------------------------------------------------


class TestSiblingTokenDetection:
    """Tests for find_sibling_token."""

    def test_finds_sibling_token(self) -> None:
        """Detects token from an existing sibling ynison instance."""
        mass = _make_mock_mass()
        mass.config.get = MagicMock(
            return_value={
                "inst1": {
                    "domain": "yandex_ynison",
                    "instance_id": "inst1",
                    "values": {CONF_TOKEN: "sibling-token", CONF_X_TOKEN: "sibling-x-token"},
                }
            }
        )
        token, x_token = find_sibling_token(mass, instance_id="inst2")
        assert token == "sibling-token"
        assert x_token == "sibling-x-token"

    def test_skips_own_instance(self) -> None:
        """Does not reuse its own token."""
        mass = _make_mock_mass()
        mass.config.get = MagicMock(
            return_value={
                "inst1": {
                    "domain": "yandex_ynison",
                    "instance_id": "inst1",
                    "values": {CONF_TOKEN: "my-token"},
                }
            }
        )
        token, x_token = find_sibling_token(mass, instance_id="inst1")
        assert token is None
        assert x_token is None

    def test_returns_none_when_no_siblings(self) -> None:
        """Returns None when no sibling instances exist."""
        mass = _make_mock_mass()
        mass.config.get = MagicMock(return_value={})
        token, x_token = find_sibling_token(mass, instance_id=None)
        assert token is None
        assert x_token is None

    def test_skips_other_domains(self) -> None:
        """Ignores instances from other provider domains."""
        mass = _make_mock_mass()
        mass.config.get = MagicMock(
            return_value={
                "inst1": {
                    "domain": "yandex_music",
                    "instance_id": "inst1",
                    "values": {CONF_TOKEN: "other-token"},
                }
            }
        )
        token, x_token = find_sibling_token(mass, instance_id=None)
        assert token is None
        assert x_token is None


class TestPCMFrameAlignment:
    """Tests for PCM frame alignment padding in get_audio_stream."""

    async def test_frame_alignment_padding_s24le(self) -> None:
        """Verify padding math for s24le stereo (frame_size=6)."""
        provider = _make_provider()
        provider._normalized_format = _make_pcm_format(_PCM_LOSSLESS_PARAMS)
        fmt = provider._normalized_format
        frame_size = (fmt.bit_depth // 8) * fmt.channels
        assert frame_size == 6  # 3 bytes x 2 channels

        # 4096 bytes yielded: 4096 % 6 = 4, need 2 bytes padding
        bytes_yielded = 4096
        remainder = bytes_yielded % frame_size
        assert remainder == 4
        pad = frame_size - remainder
        assert pad == 2

    async def test_frame_alignment_padding_s16le(self) -> None:
        """Verify padding math for s16le stereo (frame_size=4)."""
        provider = _make_provider()
        provider._normalized_format = _make_pcm_format(_PCM_LOSSY_PARAMS)
        fmt = provider._normalized_format
        frame_size = (fmt.bit_depth // 8) * fmt.channels
        assert frame_size == 4  # 2 bytes x 2 channels

        # 4096 is already aligned to 4
        assert 4096 % frame_size == 0

        # 4097 needs 3 bytes padding
        assert 4097 % frame_size == 1
        assert frame_size - (4097 % frame_size) == 3

    async def test_no_padding_when_aligned(self) -> None:
        """No padding needed when bytes_yielded is already frame-aligned."""
        fmt = _make_pcm_format(_PCM_LOSSLESS_PARAMS)
        frame_size = (fmt.bit_depth // 8) * fmt.channels
        # 6000 bytes = 1000 frames of s24le stereo
        assert 6000 % frame_size == 0
