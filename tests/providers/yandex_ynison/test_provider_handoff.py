"""Tests for handoff playback mode."""

from __future__ import annotations

import asyncio
import contextlib
import time
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from music_assistant_models.enums import (
    PlaybackState,
    ProviderFeature,
)

from music_assistant.providers.yandex_ynison import _features_for_mode
from music_assistant.providers.yandex_ynison.constants import (
    CONF_ALLOW_PLAYER_SWITCH,
    CONF_DEVICE_ID,
    CONF_MASS_PLAYER_ID,
    CONF_PLAYBACK_MODE,
    CONF_PUBLISH_NAME,
    CONF_TOKEN,
    CONF_YM_INSTANCE,
    DEFAULT_DISPLAY_NAME,
    PLAYBACK_MODE_HANDOFF,
    PLAYBACK_MODE_STREAM,
    YM_INSTANCE_OWN,
)
from music_assistant.providers.yandex_ynison.provider import YandexYnisonProvider
from music_assistant.providers.yandex_ynison.ynison_client import YnisonState


def _make_mock_config(values: dict[str, Any] | None = None) -> MagicMock:
    """Mock ProviderConfig — handoff mode by default."""
    defaults: dict[str, Any] = {
        CONF_TOKEN: "test-music-token",
        CONF_YM_INSTANCE: YM_INSTANCE_OWN,
        CONF_MASS_PLAYER_ID: "player-A",
        CONF_ALLOW_PLAYER_SWITCH: True,
        CONF_PUBLISH_NAME: DEFAULT_DISPLAY_NAME,
        CONF_DEVICE_ID: "test-device-uuid",
        CONF_PLAYBACK_MODE: PLAYBACK_MODE_HANDOFF,
        "log_level": "GLOBAL",
    }
    if values:
        defaults.update(values)
    config = MagicMock()
    config.get_value.side_effect = defaults.get
    return config


def _make_mock_mass() -> MagicMock:
    """Mock MA with player_queues APIs needed for handoff."""
    mass = MagicMock()
    mass.cache_path = "/var/cache/test-cache"

    def _create_task(coro: object) -> MagicMock:
        if asyncio.iscoroutine(coro):
            coro.close()
        return MagicMock()

    mass.create_task = MagicMock(side_effect=_create_task)
    mass.subscribe = MagicMock(return_value=MagicMock())
    mass.get_providers = MagicMock(return_value=[])
    mass.config.set_raw_provider_config_value = MagicMock()
    mass.cache.get = AsyncMock(return_value=None)
    mass.cache.set = AsyncMock()
    mass.cache.delete = AsyncMock()

    # players
    fake_player = MagicMock()
    fake_player.player_id = "player-A"
    fake_player.display_name = "Player A"
    fake_player.state.playback_state = PlaybackState.IDLE
    mass.players.all_players = MagicMock(return_value=[fake_player])
    mass.players.get_player = MagicMock(return_value=fake_player)
    mass.players.select_source = AsyncMock()
    mass.players.cmd_stop = AsyncMock()
    mass.players.trigger_player_update = MagicMock()

    # player_queues — the handoff target
    mass.player_queues.play_media = AsyncMock()
    mass.player_queues.pause = AsyncMock()
    mass.player_queues.play = AsyncMock()
    mass.player_queues.seek = AsyncMock()
    mass.player_queues.next = AsyncMock()
    mass.player_queues.previous = AsyncMock()

    # default queue snapshot — IDLE, no elapsed time
    queue = MagicMock()
    queue.state = PlaybackState.IDLE
    queue.corrected_elapsed_time = 0.0
    queue.current_item = None
    mass.player_queues.get = MagicMock(return_value=queue)

    return mass


def _make_mock_manifest() -> MagicMock:
    manifest = MagicMock()
    manifest.domain = "yandex_ynison"
    return manifest


def _make_handoff_provider() -> YandexYnisonProvider:
    """Build a provider configured in handoff mode (no AUDIO_SOURCE feature)."""
    mass = _make_mock_mass()
    config = _make_mock_config()
    manifest = _make_mock_manifest()
    return YandexYnisonProvider(mass, manifest, config, set())


def _make_state(track_id: str, *, paused: bool = False, progress_ms: int = 0) -> MagicMock:
    """Build a minimal YnisonState-like mock.

    YnisonState exposes its scalars through @property without setters, so we
    use MagicMock here rather than instantiating the real class.
    """
    state = MagicMock(spec=YnisonState)
    state.current_track_id = track_id
    state.active_device_id = "test-device-uuid"
    state.is_paused = paused
    state.progress_ms = progress_ms
    state.last_update_is_echo = False
    state.duration_ms = 200000
    state.player_state = {
        "player_queue": {
            "current_playable_index": 0,
            "playable_list": [{"playable_id": track_id, "title": "Track"}],
            "entity_type": "",
            "entity_id": "",
        },
        "status": {},
    }
    return state


# ------------------------------------------------------------------
# _features_for_mode
# ------------------------------------------------------------------


class TestFeaturesForMode:
    """The setup() helper that picks SUPPORTED_FEATURES from config."""

    def test_stream_mode_advertises_audio_source(self) -> None:
        """Stream mode keeps AUDIO_SOURCE so the plugin appears as a source."""
        assert _features_for_mode(PLAYBACK_MODE_STREAM) == {ProviderFeature.AUDIO_SOURCE}

    def test_handoff_mode_has_no_features(self) -> None:
        """Handoff mode drops AUDIO_SOURCE — playback flows through MA queue."""
        assert _features_for_mode(PLAYBACK_MODE_HANDOFF) == set()

    def test_unknown_mode_falls_back_to_stream(self) -> None:
        """Unknown mode value falls back to stream behaviour, not crash."""
        assert _features_for_mode("nonsense") == {ProviderFeature.AUDIO_SOURCE}


# ------------------------------------------------------------------
# Provider init flags
# ------------------------------------------------------------------


@pytest.mark.asyncio
class TestHandoffInit:
    """Provider state when configured for handoff."""

    async def test_handoff_flag_set(self) -> None:
        """A handoff-config provider exposes _is_handoff=True and the mode constant."""
        provider = _make_handoff_provider()
        assert provider._is_handoff is True
        assert provider._playback_mode == PLAYBACK_MODE_HANDOFF


# ------------------------------------------------------------------
# _handoff_activate
# ------------------------------------------------------------------


@pytest.mark.asyncio
class TestHandoffActivate:
    """Translate Ynison state into player_queue commands."""

    async def test_new_track_calls_play_media(self) -> None:
        """A new Ynison track id triggers player_queues.play_media with the right URI."""
        provider = _make_handoff_provider()
        # Without a linked yandex_music provider the URI falls back to the
        # bare domain — that's what we assert here.
        provider._yandex_provider = None

        state = _make_state("track-1")
        await provider._handoff_activate(state, "player-A")

        provider.mass.player_queues.play_media.assert_awaited_once()
        call_args = provider.mass.player_queues.play_media.call_args
        assert call_args.args[0] == "player-A"
        assert call_args.args[1] == "yandex_music://track/track-1"
        assert provider._handoff_current_track_id == "track-1"
        assert provider._active_player_id == "player-A"

    async def test_same_track_no_redundant_play_media(self) -> None:
        """Re-receiving the active track without drift skips play_media and seek."""
        provider = _make_handoff_provider()
        provider._handoff_current_track_id = "track-1"
        # MA queue keeps reporting the same track at ~50s while Ynison
        # echoes ~50s — no drift, no commands.
        queue = provider.mass.player_queues.get.return_value
        queue.state = PlaybackState.PLAYING
        queue.corrected_elapsed_time = 50.0

        state = _make_state("track-1", progress_ms=50_000)
        await provider._handoff_activate(state, "player-A")

        provider.mass.player_queues.play_media.assert_not_awaited()
        provider.mass.player_queues.seek.assert_not_awaited()

    async def test_drift_triggers_seek(self) -> None:
        """Drift > 3000 ms between Ynison and MA queue triggers a seek call."""
        provider = _make_handoff_provider()
        provider._handoff_current_track_id = "track-1"
        queue = provider.mass.player_queues.get.return_value
        queue.state = PlaybackState.PLAYING
        queue.corrected_elapsed_time = 10.0  # MA at 10s

        # Ynison reports 60s — 50s drift, well over the 3s threshold
        state = _make_state("track-1", progress_ms=60_000)
        await provider._handoff_activate(state, "player-A")

        provider.mass.player_queues.seek.assert_awaited_once_with("player-A", 60)

    async def test_echo_does_not_trigger_seek(self) -> None:
        """An echo update from Ynison must not bounce back as a seek command."""
        provider = _make_handoff_provider()
        provider._handoff_current_track_id = "track-1"
        queue = provider.mass.player_queues.get.return_value
        queue.state = PlaybackState.PLAYING
        queue.corrected_elapsed_time = 10.0

        state = _make_state("track-1", progress_ms=60_000)
        state.last_update_is_echo = True
        await provider._handoff_activate(state, "player-A")

        provider.mass.player_queues.seek.assert_not_awaited()

    async def test_paused_queue_resumes_when_ynison_says_playing(self) -> None:
        """If MA queue is paused while Ynison says playing, resume the queue."""
        provider = _make_handoff_provider()
        provider._handoff_current_track_id = "track-1"
        queue = provider.mass.player_queues.get.return_value
        queue.state = PlaybackState.PAUSED
        queue.corrected_elapsed_time = 10.0

        state = _make_state("track-1", progress_ms=10_000)
        await provider._handoff_activate(state, "player-A")

        provider.mass.player_queues.play.assert_awaited_once_with("player-A")


@pytest.mark.asyncio
class TestHandoffPause:
    """Pause translation."""

    async def test_handoff_pause_calls_queue_pause(self) -> None:
        """Ynison pause translates to player_queues.pause on the active player."""
        provider = _make_handoff_provider()
        await provider._handoff_pause("player-A")
        provider.mass.player_queues.pause.assert_awaited_once_with("player-A")

    async def test_handoff_pause_swallows_errors(self) -> None:
        """Failures inside player_queues.pause must not propagate to Ynison handler."""
        provider = _make_handoff_provider()
        provider.mass.player_queues.pause = AsyncMock(side_effect=Exception("boom"))
        # Must not raise
        await provider._handoff_pause("player-A")

    async def test_paused_state_skips_pause_when_no_active_player(self) -> None:
        """Without _active_player_id set, paused-state path must NOT pause anyone.

        Copilot review N1: the previous code fell back to
        _get_target_player_id(), which auto-selects an unrelated playing
        player after startup/cleanup — that would silently pause the wrong
        MA queue.
        """
        provider = _make_handoff_provider()
        provider._active_player_id = None  # not yet activated

        # Build a state that says "our device, paused".
        state = MagicMock(spec=YnisonState)
        state.current_track_id = "track-1"
        state.active_device_id = "test-device-uuid"
        state.is_paused = True
        state.progress_ms = 0
        state.last_update_is_echo = False
        state.duration_ms = 0
        state.player_state = {
            "player_queue": {
                "current_playable_index": 0,
                "playable_list": [{"playable_id": "track-1"}],
                "entity_type": "",
                "entity_id": "",
            },
            "status": {},
        }

        await provider._handle_ynison_state(state)

        provider.mass.player_queues.pause.assert_not_awaited()

    async def test_paused_state_uses_active_player_id(self) -> None:
        """When _active_player_id is set, pause is routed to that player."""
        provider = _make_handoff_provider()
        provider._active_player_id = "player-A"

        state = MagicMock(spec=YnisonState)
        state.current_track_id = "track-1"
        state.active_device_id = "test-device-uuid"
        state.is_paused = True
        state.progress_ms = 0
        state.last_update_is_echo = False
        state.duration_ms = 0
        state.player_state = {
            "player_queue": {
                "current_playable_index": 0,
                "playable_list": [{"playable_id": "track-1"}],
                "entity_type": "",
                "entity_id": "",
            },
            "status": {},
        }

        await provider._handle_ynison_state(state)

        provider.mass.player_queues.pause.assert_awaited_once_with("player-A")


@pytest.mark.asyncio
class TestClearActivePlayerHandoffBookkeeping:
    """`_clear_active_player()` must reset every handoff watermark.

    Copilot review N2: a stale `_handoff_last_progress_sync_mono` would
    suppress the first progress / heartbeat update of the next session.
    """

    async def test_clear_resets_handoff_watermarks(self) -> None:
        """All handoff state — including the throttle watermark — is cleared."""
        provider = _make_handoff_provider()
        provider._active_player_id = "player-A"
        provider._handoff_current_track_id = "track-1"
        provider._handoff_completion_signaled_for = "track-1"
        provider._handoff_grace_until = 9999.0
        provider._handoff_last_seen_state = PlaybackState.PLAYING
        provider._handoff_last_progress_sync_mono = 9999.0

        provider._clear_active_player()

        assert provider._active_player_id is None
        assert provider._handoff_current_track_id is None  # type: ignore[unreachable]
        assert provider._handoff_completion_signaled_for is None
        assert provider._handoff_grace_until == 0.0
        assert provider._handoff_last_seen_state is None
        assert provider._handoff_last_progress_sync_mono == 0.0


# ------------------------------------------------------------------
# _on_ma_player_event
# ------------------------------------------------------------------


@pytest.mark.asyncio
class TestHandoffActivateExtended:
    """Coverage for grace, dedup, replay, instance-id URI."""

    async def test_uri_uses_yandex_provider_instance_id(self) -> None:
        """When a yandex_music provider is linked, its instance_id is in the URI."""
        provider = _make_handoff_provider()
        ym = MagicMock()
        ym.instance_id = "ym-borrow-A"
        provider._yandex_provider = ym
        provider._get_stream_details_with_retry = AsyncMock(  # type: ignore[method-assign]
            side_effect=Exception("ignored")
        )

        await provider._handoff_activate(_make_state("track-X"), "player-A")

        provider.mass.player_queues.play_media.assert_awaited_once()
        assert (
            provider.mass.player_queues.play_media.call_args.args[1]
            == "ym-borrow-A://track/track-X"
        )

    async def test_uri_falls_back_to_domain_without_provider(self) -> None:
        """Missing yandex_music link → URI uses bare `yandex_music` domain."""
        provider = _make_handoff_provider()
        provider._yandex_provider = None

        await provider._handoff_activate(_make_state("track-Y"), "player-A")

        assert (
            provider.mass.player_queues.play_media.call_args.args[1]
            == "yandex_music://track/track-Y"
        )

    async def test_dedup_skips_play_media_when_queue_already_playing_same_uri(self) -> None:
        """If MA queue already plays the expected URI, no fresh play_media."""
        provider = _make_handoff_provider()
        provider._yandex_provider = None
        queue = provider.mass.player_queues.get.return_value
        queue.state = PlaybackState.PLAYING
        queue.current_item = MagicMock()
        queue.current_item.uri = "yandex_music://track/track-Z"

        await provider._handoff_activate(_make_state("track-Z"), "player-A")

        provider.mass.player_queues.play_media.assert_not_awaited()
        # bookkeeping is still updated
        assert provider._handoff_current_track_id == "track-Z"

    async def test_resume_via_play_when_queue_paused_with_same_uri(self) -> None:
        """Same URI but PAUSED → call play(), not play_media()."""
        provider = _make_handoff_provider()
        provider._yandex_provider = None
        queue = provider.mass.player_queues.get.return_value
        queue.state = PlaybackState.PAUSED
        queue.current_item = MagicMock()
        queue.current_item.uri = "yandex_music://track/track-Z"

        await provider._handoff_activate(_make_state("track-Z"), "player-A")

        provider.mass.player_queues.play_media.assert_not_awaited()
        provider.mass.player_queues.play.assert_awaited_once_with("player-A")

    async def test_grace_blocks_drift_seek_after_play_media(self) -> None:
        """Drift seek is suppressed inside the grace window after play_media."""
        provider = _make_handoff_provider()
        provider._yandex_provider = None
        # Trigger play_media once → grace window opens.
        await provider._handoff_activate(_make_state("track-1"), "player-A")
        assert provider._handoff_grace_until > time.monotonic()

        # Same track, big drift, queue still resolving (PLAYING but elapsed=0,
        # which is the typical state in the first second after play_media).
        queue = provider.mass.player_queues.get.return_value
        queue.state = PlaybackState.PLAYING
        queue.corrected_elapsed_time = 0.0

        await provider._handoff_activate(_make_state("track-1", progress_ms=60_000), "player-A")
        provider.mass.player_queues.seek.assert_not_awaited()

    async def test_grace_overridden_by_playing_state_with_elapsed(self) -> None:
        """A real seek inside grace must still go through if queue progressed."""
        provider = _make_handoff_provider()
        provider._yandex_provider = None
        await provider._handoff_activate(_make_state("track-1"), "player-A")

        queue = provider.mass.player_queues.get.return_value
        queue.state = PlaybackState.PLAYING
        queue.corrected_elapsed_time = 5.0  # > 1s elapsed → override

        await provider._handoff_activate(_make_state("track-1", progress_ms=60_000), "player-A")
        provider.mass.player_queues.seek.assert_awaited_once_with("player-A", 60)

    async def test_replay_resets_completion_marker(self) -> None:
        """Same track with progress<1s clears the completion-once marker."""
        provider = _make_handoff_provider()
        provider._yandex_provider = None
        provider._handoff_current_track_id = "track-1"
        provider._handoff_completion_signaled_for = "track-1"

        await provider._handoff_activate(_make_state("track-1", progress_ms=500), "player-A")
        assert provider._handoff_completion_signaled_for is None

    async def test_play_media_failure_does_not_commit_track_id(self) -> None:
        """If play_media throws, _handoff_current_track_id stays unchanged.

        Otherwise the next Ynison update for the same track id would fall
        through the same-track branch and never retry play_media, leaving
        MA stuck out of sync (Copilot review C3).
        """
        provider = _make_handoff_provider()
        provider._yandex_provider = None
        provider._handoff_current_track_id = "old-track"
        provider.mass.player_queues.play_media = AsyncMock(side_effect=Exception("boom"))

        await provider._handoff_activate(_make_state("new-track"), "player-A")

        # Track id NOT advanced — next state-update will retry play_media.
        assert provider._handoff_current_track_id == "old-track"
        # Grace was NOT opened — we don't want to suppress drift seeks for a
        # play_media that never actually started.
        assert provider._handoff_grace_until == 0.0

    async def test_play_media_failure_does_not_start_heartbeat(self) -> None:
        """Heartbeat must not run after a failed play_media (Copilot review N3).

        Otherwise it would keep streaming the previous/idle queue's progress
        to Ynison and prevent rebalancing away from a non-working device.
        """
        provider = _make_handoff_provider()
        provider._yandex_provider = None
        provider.mass.player_queues.play_media = AsyncMock(side_effect=Exception("boom"))
        ensure_calls: list[None] = []
        provider._ensure_handoff_heartbeat = (  # type: ignore[method-assign]
            lambda: ensure_calls.append(None)
        )

        await provider._handoff_activate(_make_state("new-track"), "player-A")

        assert ensure_calls == []  # heartbeat never started

    async def test_dedup_skip_starts_heartbeat(self) -> None:
        """Dedup branch (queue already plays the URI) must still arm heartbeat.

        The activation succeeded — Ynison expects regular progress reports.
        """
        provider = _make_handoff_provider()
        provider._yandex_provider = None
        queue = provider.mass.player_queues.get.return_value
        queue.state = PlaybackState.PLAYING
        queue.current_item = MagicMock()
        queue.current_item.uri = "yandex_music://track/track-Z"
        ensure_calls: list[None] = []
        provider._ensure_handoff_heartbeat = (  # type: ignore[method-assign]
            lambda: ensure_calls.append(None)
        )

        await provider._handoff_activate(_make_state("track-Z"), "player-A")

        assert ensure_calls == [None]


@pytest.mark.asyncio
class TestHandoffHeartbeat:
    """Independent progress heartbeat (P1)."""

    async def test_heartbeat_loop_sends_progress_when_active(self) -> None:
        """One heartbeat tick fires _send_progress_to_ynison."""
        provider = _make_handoff_provider()
        provider._handoff_heartbeat_interval = 0.05
        provider._active_player_id = "player-A"
        ynison = MagicMock()
        ynison.connected = True
        ynison.state.duration_ms = 200_000
        provider._ynison = ynison
        provider._actual_duration_ms = 200_000
        queue = provider.mass.player_queues.get.return_value
        queue.state = PlaybackState.PLAYING
        queue.corrected_elapsed_time = 30.0
        send_mock = AsyncMock()
        provider._send_progress_to_ynison = send_mock  # type: ignore[method-assign]

        task = asyncio.create_task(provider._handoff_heartbeat_loop())
        await asyncio.sleep(0.12)
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

        assert send_mock.await_count >= 1
        call = send_mock.await_args_list[0]
        assert call.kwargs["progress_ms"] == 30000
        assert call.kwargs["paused"] is False

    async def test_heartbeat_skips_without_active_player(self) -> None:
        """No active_player_id → heartbeat skips its tick."""
        provider = _make_handoff_provider()
        provider._handoff_heartbeat_interval = 0.05
        provider._active_player_id = None
        ynison = MagicMock()
        ynison.connected = True
        provider._ynison = ynison
        send_mock = AsyncMock()
        provider._send_progress_to_ynison = send_mock  # type: ignore[method-assign]

        task = asyncio.create_task(provider._handoff_heartbeat_loop())
        await asyncio.sleep(0.12)
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

        send_mock.assert_not_awaited()

    async def test_heartbeat_clears_player_when_queue_disappears(self) -> None:
        """Queue gone → loop clears active player and exits."""
        provider = _make_handoff_provider()
        provider._handoff_heartbeat_interval = 0.05
        provider._active_player_id = "player-A"
        ynison = MagicMock()
        ynison.connected = True
        provider._ynison = ynison
        provider.mass.player_queues.get = MagicMock(return_value=None)

        task = asyncio.create_task(provider._handoff_heartbeat_loop())
        await asyncio.sleep(0.12)
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

        assert provider._active_player_id is None


class TestForceProgressOnStateChange:
    """P10: state transitions bypass the 2s throttle for low-latency forwarding."""

    def _setup(self) -> YandexYnisonProvider:
        """Build a handoff provider hooked up to a connected Ynison mock."""
        provider = _make_handoff_provider()
        provider._active_player_id = "player-A"
        ynison = MagicMock()
        ynison.connected = True
        ynison.state.duration_ms = 200_000
        provider._ynison = ynison
        provider._actual_duration_ms = 200_000
        return provider

    def test_state_change_triggers_send_within_throttle_window(self) -> None:
        """A PLAYING→PAUSED transition forwards progress even mid-throttle."""
        provider = self._setup()
        # Simulate: throttle taken recently
        provider._handoff_last_progress_sync_mono = time.monotonic()
        # Make sure we're using the same `now` reference baseline
        queue = provider.mass.player_queues.get.return_value
        queue.state = PlaybackState.PLAYING

        event = MagicMock()
        event.object_id = "player-A"

        # First tick: state goes from None → PLAYING (state_changed=True)
        provider._on_ma_player_event(event)
        first_calls = provider.mass.create_task.call_count
        assert first_calls >= 1

        # Second tick: same PLAYING state inside throttle window → no send
        provider._on_ma_player_event(event)
        assert provider.mass.create_task.call_count == first_calls

        # Third tick: state transitions to PAUSED → bypasses throttle
        queue.state = PlaybackState.PAUSED
        provider._on_ma_player_event(event)
        assert provider.mass.create_task.call_count > first_calls


class TestOnMaPlayerEvent:
    """MA → Ynison sync via subscription (synchronous handler)."""

    def _setup(self) -> YandexYnisonProvider:
        """Build a handoff provider wired up to a connected Ynison mock."""
        provider = _make_handoff_provider()
        provider._active_player_id = "player-A"
        ynison = MagicMock()
        ynison.connected = True
        ynison.state.duration_ms = 200000
        provider._ynison = ynison
        # Bypass real best-duration logic so the path doesn't hit MagicMock arithmetic
        provider._actual_duration_ms = 200000
        return provider

    def test_ignores_events_for_other_players(self) -> None:
        """Events on a different player must not result in any work."""
        provider = self._setup()
        event = MagicMock()
        event.object_id = "player-B"  # different player
        provider._on_ma_player_event(event)
        provider.mass.create_task.assert_not_called()

    def test_progress_throttle(self) -> None:
        """Two events fired within the throttle window result in only one push."""
        provider = self._setup()

        event = MagicMock()
        event.object_id = "player-A"

        # First event passes the throttle
        provider._on_ma_player_event(event)
        first_call_count = provider.mass.create_task.call_count
        assert first_call_count >= 1

        # Immediate second event blocked by 2s throttle
        provider._on_ma_player_event(event)
        assert provider.mass.create_task.call_count == first_call_count

    def test_idle_queue_signals_completion_once(self) -> None:
        """Queue going IDLE near track end signals completion once per track id."""
        provider = self._setup()
        provider._handoff_current_track_id = "track-1"
        # Queue went IDLE near the end of the track (elapsed >= duration - 5s)
        queue = provider.mass.player_queues.get.return_value
        queue.state = PlaybackState.IDLE
        queue.current_item = MagicMock()
        queue.current_item.duration = 200  # seconds
        queue.corrected_elapsed_time = 199.0  # 1s before end

        event = MagicMock()
        event.object_id = "player-A"
        provider._on_ma_player_event(event)
        assert provider._handoff_completion_signaled_for == "track-1"

        # Second event with same track and bypassed throttle must NOT re-signal
        provider._handoff_last_progress_sync_mono = 0.0
        before = provider.mass.create_task.call_count
        provider._on_ma_player_event(event)
        # Marker remains the same — completion is one-shot per track.
        assert provider._handoff_completion_signaled_for == "track-1"
        # At least one new task may fire for the throttle-bypassed progress
        # update, but completion should not double-fire.
        assert provider.mass.create_task.call_count >= before

    def test_idle_queue_at_pause_does_not_signal_completion(self) -> None:
        """IDLE mid-track (e.g. pause on single-track queue) must NOT advance.

        Reproduces the bug where pausing a single-track queue makes MA's
        queue runner report IDLE — without the near-end guard we'd misread
        that as natural end-of-track and cascade through the RADIO tail.
        """
        provider = self._setup()
        provider._handoff_current_track_id = "track-1"
        queue = provider.mass.player_queues.get.return_value
        queue.state = PlaybackState.IDLE
        queue.current_item = MagicMock()
        queue.current_item.duration = 200
        queue.corrected_elapsed_time = 30.0  # nowhere near the end

        event = MagicMock()
        event.object_id = "player-A"
        provider._on_ma_player_event(event)

        assert provider._handoff_completion_signaled_for is None

    def test_idle_queue_with_unknown_duration_does_not_signal(self) -> None:
        """Without a duration we can't tell pause from end — be conservative."""
        provider = self._setup()
        provider._handoff_current_track_id = "track-1"
        queue = provider.mass.player_queues.get.return_value
        queue.state = PlaybackState.IDLE
        queue.current_item = MagicMock()
        queue.current_item.duration = 0  # unknown
        queue.corrected_elapsed_time = 50.0

        event = MagicMock()
        event.object_id = "player-A"
        provider._on_ma_player_event(event)

        assert provider._handoff_completion_signaled_for is None

    def test_idle_queue_without_current_item_does_not_signal(self) -> None:
        """current_item=None (cleared queue) is not a completion signal."""
        provider = self._setup()
        provider._handoff_current_track_id = "track-1"
        queue = provider.mass.player_queues.get.return_value
        queue.state = PlaybackState.IDLE
        queue.current_item = None
        queue.corrected_elapsed_time = 50.0

        event = MagicMock()
        event.object_id = "player-A"
        provider._on_ma_player_event(event)

        assert provider._handoff_completion_signaled_for is None

    def test_idle_short_track_is_treated_as_near_end(self) -> None:
        """Tracks shorter than the 5s margin always treat IDLE as end-of-track."""
        provider = self._setup()
        provider._handoff_current_track_id = "track-1"
        queue = provider.mass.player_queues.get.return_value
        queue.state = PlaybackState.IDLE
        queue.current_item = MagicMock()
        queue.current_item.duration = 3  # 3-second track
        queue.corrected_elapsed_time = 0.0  # margin makes threshold 0

        event = MagicMock()
        event.object_id = "player-A"
        provider._on_ma_player_event(event)

        # 3s track: threshold = max(0, 3-5) = 0 → any elapsed counts as end.
        assert provider._handoff_completion_signaled_for == "track-1"
