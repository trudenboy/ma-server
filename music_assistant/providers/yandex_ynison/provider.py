"""Yandex Ynison plugin provider for Music Assistant."""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncGenerator, Callable
from contextlib import suppress
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, cast

from music_assistant_models.enums import (
    ContentType,
    EventType,
    MediaType,
    PlaybackState,
    ProviderFeature,
    ProviderType,
    StreamType,
)
from music_assistant_models.errors import LoginFailed, UnsupportedFeaturedException
from music_assistant_models.media_items import AudioFormat
from music_assistant_models.streamdetails import StreamDetails, StreamMetadata
from ya_passport_auth import SecretStr

from music_assistant.helpers.ffmpeg import get_ffmpeg_stream
from music_assistant.models.plugin import PluginProvider, PluginSource

from .constants import (
    CONF_ALLOW_PLAYER_SWITCH,
    CONF_DEVICE_ID,
    CONF_DISPLAY_NAME,
    CONF_PLAYER,
    CONF_TOKEN,
    CONF_X_TOKEN,
    DEFAULT_DISPLAY_NAME,
    PLAYER_ID_AUTO,
)
from .yandex_auth import refresh_music_token
from .ynison_client import YnisonClient, YnisonDeviceInfo, YnisonState, generate_device_id

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ProviderConfig
    from music_assistant_models.event import MassEvent
    from music_assistant_models.provider import ProviderManifest

    from music_assistant.mass import MusicAssistant


@dataclass
class PreBuffer:
    """Holds pre-buffered audio data for an upcoming track."""

    track_id: str
    seek_ms: int
    queue: asyncio.Queue[bytes | None] = field(default_factory=lambda: asyncio.Queue(maxsize=64))
    stream_details: StreamDetails | None = None
    error: Exception | None = None
    task: asyncio.Task[None] | None = None

    async def cancel(self) -> None:
        """Cancel the prebuffer task and drain the queue."""
        if self.task and not self.task.done():
            self.task.cancel()
            with suppress(asyncio.CancelledError):
                await self.task


class YandexYnisonProvider(PluginProvider):
    """Implementation of the Yandex Music Connect (Ynison) Plugin."""

    @property
    def instance_name_postfix(self) -> str | None:
        """Return display name as instance postfix for multi-instance setups."""
        name = self._display_name
        return name if name != DEFAULT_DISPLAY_NAME else None

    def __init__(
        self,
        mass: MusicAssistant,
        manifest: ProviderManifest,
        config: ProviderConfig,
        supported_features: set[ProviderFeature],
    ) -> None:
        """Initialize the Ynison plugin provider."""
        super().__init__(mass, manifest, config, supported_features)

        # Config values
        self._default_player_id: str = (
            cast("str", self.config.get_value(CONF_PLAYER)) or PLAYER_ID_AUTO
        )
        allow_switch_value = self.config.get_value(CONF_ALLOW_PLAYER_SWITCH)
        self._allow_player_switch: bool = (
            cast("bool", allow_switch_value) if allow_switch_value is not None else True
        )
        self._display_name: str = (
            cast("str", self.config.get_value(CONF_DISPLAY_NAME)) or DEFAULT_DISPLAY_NAME
        )

        # Device ID — persist in config so re-registration uses the same ID
        device_id = cast("str | None", self.config.get_value(CONF_DEVICE_ID))
        if not device_id:
            device_id = generate_device_id()
            self._update_config_value(CONF_DEVICE_ID, device_id)
        self._device_id: str = device_id

        # Runtime state
        self._active_player_id: str | None = None
        self._ynison: YnisonClient | None = None
        self._runner_task: asyncio.Task[None] | None = None
        self._on_unload_callbacks: list[Callable[..., None]] = []
        self._yandex_provider: Any = None
        self._current_streaming_track_id: str | None = None
        self._track_changed_event = asyncio.Event()
        self._stream_stop_event = asyncio.Event()
        self._seek_position_ms: int = 0
        self._last_progress_ms: int = 0
        self._last_progress_time: float = 0.0
        self._last_player_update_time: float = 0.0
        self._actual_duration_ms: int = 0
        self._prebuffer: PreBuffer | None = None
        self._prefetched_list: list[dict[str, Any]] | None = None
        self._prefetch_task: asyncio.Task[Any] | None = None

        # PluginSource
        self._source_details = PluginSource(
            id=self.instance_id,
            name=self.name,
            passive=not self._allow_player_switch,
            can_play_pause=False,
            can_seek=False,
            can_next_previous=False,
            audio_format=AudioFormat(
                content_type=ContentType.FLAC,
                sample_rate=44100,
                bit_depth=16,
                channels=2,
            ),
            metadata=StreamMetadata(
                title=f"Yandex Music Connect | {self._display_name}",
            ),
            stream_type=StreamType.CUSTOM,
        )
        self._source_details.on_select = self._on_source_selected

    # ------------------------------------------------------------------
    # Provider lifecycle
    # ------------------------------------------------------------------

    async def handle_async_init(self) -> None:
        """Handle async initialization of the provider."""
        token = await self._resolve_token()

        device_info = YnisonDeviceInfo(
            device_id=self._device_id,
            title=self._display_name,
        )

        self._ynison = YnisonClient(
            token=token,
            device_info=device_info,
            on_state_update=self._handle_ynison_state,
            on_disconnect=self._handle_ynison_disconnect,
            logger=self.logger,
        )

        self._runner_task = self.mass.create_task(self._ynison.connect())

        # Subscribe to provider events to detect linked yandex_music provider
        self._on_unload_callbacks.append(
            self.mass.subscribe(
                self._on_provider_event,
                EventType.PROVIDERS_UPDATED,
            )
        )
        # Initial check for matching provider
        self.mass.create_task(self._check_yandex_provider_match())

    async def unload(self, is_removed: bool = False) -> None:
        """Handle close/cleanup of the provider."""
        if self._prebuffer:
            await self._prebuffer.cancel()
            self._prebuffer = None

        if self._prefetch_task and not self._prefetch_task.done():
            self._prefetch_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._prefetch_task

        if self._ynison:
            await self._ynison.disconnect()

        if self._runner_task and not self._runner_task.done():
            self._runner_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._runner_task

        for callback in self._on_unload_callbacks:
            callback()

    def get_source(self) -> PluginSource:
        """Get (audio)source details for this plugin."""
        return self._source_details

    async def get_audio_stream(self, player_id: str) -> AsyncGenerator[bytes, None]:
        """Return continuous audio stream following Ynison track changes.

        Streams the current track, then waits for track changes and streams
        the next track automatically. Runs until the source is deselected.
        """
        self._stream_stop_event.clear()

        while not self._stream_stop_event.is_set() and self._source_details.in_use_by == player_id:
            if not self._ynison or not self._ynison.state.current_track_id:
                # Wait for a track to appear
                self._track_changed_event.clear()
                try:
                    await asyncio.wait_for(self._track_changed_event.wait(), timeout=30.0)
                except TimeoutError:
                    continue
                continue

            # Clear event before reading state so any subsequent update
            # re-sets the event instead of being silently cleared.
            self._track_changed_event.clear()
            track_id = self._ynison.state.current_track_id
            self._current_streaming_track_id = track_id

            # Don't start streaming if Ynison reports paused — wait for resume
            if self._ynison.state.is_paused:
                continue

            if not self._yandex_provider:
                self.logger.warning(
                    "No linked Yandex Music provider — cannot stream track %s", track_id
                )
                self._current_streaming_track_id = None
                self._stream_stop_event.set()
                if self._source_details.in_use_by == player_id:
                    self._source_details.in_use_by = None
                    await self.mass.players.cmd_stop(player_id)
                return

            # Stream the current track — use prebuffer if available
            seek_ms = self._seek_position_ms
            self._seek_position_ms = 0

            if (
                self._prebuffer
                and self._prebuffer.track_id == track_id
                and self._prebuffer.seek_ms == seek_ms
                and not self._prebuffer.error
            ):
                # Prebuffer hit — data is already loading/ready
                self.logger.debug("Using prebuffer for track %s", track_id)
                async for chunk in self._yield_from_prebuffer():
                    yield chunk
                    if (
                        self._track_changed_event.is_set()
                        or self._stream_stop_event.is_set()
                        or self._source_details.in_use_by != player_id
                    ):
                        break
            else:
                # Prebuffer miss — stream directly (fallback)
                if self._prebuffer and self._prebuffer.track_id != track_id:
                    self.logger.debug(
                        "Prebuffer miss: have %s, need %s",
                        self._prebuffer.track_id,
                        track_id,
                    )
                async for chunk in self._stream_track(track_id, seek_ms=seek_ms):
                    yield chunk
                    if (
                        self._track_changed_event.is_set()
                        or self._stream_stop_event.is_set()
                        or self._source_details.in_use_by != player_id
                    ):
                        break

            # Don't clear _current_streaming_track_id yet — keep it set
            # during advance/wait so Ynison echo of the same track doesn't
            # trigger a false track-change detection in _activate_playback.

            if self._stream_stop_event.is_set():
                self._current_streaming_track_id = None
                break

            # Track finished naturally — signal completion to Ynison.
            # Yandex controls the queue; we just wait for the next track.
            if not self._track_changed_event.is_set() and self._ynison:
                self.logger.info("Track %s finished, advancing to next", track_id)
                await self._signal_track_completion()
                if not await self._wait_for_track_change(track_id):
                    self._stream_stop_event.set()
                    self._current_streaming_track_id = None
                    break

            # Clear before next iteration — the new track ID will be set at
            # the top of the loop from the latest Ynison state.
            self._current_streaming_track_id = None

    async def _wait_for_track_change(self, old_track_id: str, timeout: float = 30.0) -> bool:
        """Wait for Ynison to report a different track, ignoring echoes.

        After _signal_track_completion sends update_playing_status, Ynison
        echoes back the same track with updated progress.  Only return True
        once current_track_id actually differs from old_track_id.
        """
        deadline = time.monotonic() + timeout
        while not self._stream_stop_event.is_set():
            self._track_changed_event.clear()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                await asyncio.wait_for(self._track_changed_event.wait(), timeout=remaining)
            except TimeoutError:
                break
            if self._ynison and self._ynison.state.current_track_id != old_track_id:
                return True
        self.logger.info("No new track from Ynison after completion, stopping stream")
        return False

    async def _stream_track(self, track_id: str, seek_ms: int = 0) -> AsyncGenerator[bytes, None]:
        """Stream a single track, yielding raw audio bytes (no transcoding).

        For normal playback: raw passthrough (FLAC/MP3/AAC bytes as-is).
        For seek: ffmpeg fallback (server-side seek, output FLAC).

        get_audio_stream from yandex_music provider handles both raw and
        encrypted transports via windowed Range requests with CDN reconnect.
        """
        try:
            stream_details = await self._yandex_provider.get_stream_details(
                track_id, MediaType.TRACK
            )
        except Exception:
            self.logger.exception("Failed to get stream details for track %s", track_id)
            return

        await self._update_metadata_from_stream(stream_details, seek_ms)

        if seek_ms > 0:
            # Seek requires ffmpeg (provider doesn't support byte-level seek)
            seek_sec = seek_ms / 1000.0
            self.logger.info("Seeking to %.1fs in track %s via ffmpeg", seek_sec, track_id)
            async for chunk in get_ffmpeg_stream(
                audio_input=self._yandex_provider.get_audio_stream(stream_details),
                input_format=stream_details.audio_format,
                output_format=self._source_details.audio_format,
                extra_input_args=["-ss", f"{seek_sec:.3f}"],
            ):
                yield chunk
            return

        # No seek → raw passthrough (FLAC/MP3/AAC bytes forwarded as-is)
        self.logger.info(
            "Streaming track %s (passthrough): format=%s",
            track_id,
            stream_details.audio_format,
        )
        async for chunk in self._yandex_provider.get_audio_stream(stream_details):
            yield chunk

    # ------------------------------------------------------------------
    # Pre-buffer
    # ------------------------------------------------------------------

    async def _start_prebuffer(self, track_id: str, seek_ms: int = 0) -> None:
        """Start pre-buffering a track into an asyncio.Queue.

        Called immediately on track change from Ynison — before the player
        HTTP GET arrives. When get_audio_stream runs, it checks if a matching
        prebuffer exists and yields from the queue instead of calling the API.
        """
        if self._prebuffer:
            await self._prebuffer.cancel()

        prebuffer = PreBuffer(track_id=track_id, seek_ms=seek_ms)
        self._prebuffer = prebuffer

        async def _fill() -> None:
            try:
                sd = await self._yandex_provider.get_stream_details(track_id, MediaType.TRACK)
                prebuffer.stream_details = sd
                await self._update_metadata_from_stream(sd, seek_ms)

                if seek_ms > 0:
                    # Seek requires ffmpeg — fill queue with ffmpeg output
                    seek_sec = seek_ms / 1000.0
                    async for chunk in get_ffmpeg_stream(
                        audio_input=self._yandex_provider.get_audio_stream(sd),
                        input_format=sd.audio_format,
                        output_format=self._source_details.audio_format,
                        extra_input_args=["-ss", f"{seek_sec:.3f}"],
                    ):
                        await prebuffer.queue.put(chunk)
                else:
                    async for chunk in self._yandex_provider.get_audio_stream(sd):
                        await prebuffer.queue.put(chunk)
            except asyncio.CancelledError:
                raise
            except Exception as err:
                prebuffer.error = err
                self.logger.warning("Prebuffer failed for %s: %s", track_id, err)
            finally:
                await prebuffer.queue.put(None)  # EOF sentinel

        prebuffer.task = self.mass.create_task(_fill())

    async def _yield_from_prebuffer(self) -> AsyncGenerator[bytes, None]:
        """Yield chunks from the active prebuffer queue until EOF sentinel."""
        assert self._prebuffer is not None
        while True:
            chunk = await self._prebuffer.queue.get()
            if chunk is None:
                break
            yield chunk

    # ------------------------------------------------------------------
    # Token handling
    # ------------------------------------------------------------------

    async def _resolve_token(self) -> SecretStr:
        """Resolve the Yandex Music token, refreshing from x_token if needed.

        Prefers refreshing from x_token when available so that an expired
        music token does not permanently break the plugin (self-healing).
        Falls back to the stored music token if the refresh attempt fails.
        """
        token = cast("str | None", self.config.get_value(CONF_TOKEN))
        x_token = cast("str | None", self.config.get_value(CONF_X_TOKEN))

        if x_token:
            try:
                self.logger.debug("Refreshing music token from x_token")
                new_token = await refresh_music_token(SecretStr(x_token))
                self._update_config_value(CONF_TOKEN, new_token.get_secret(), encrypted=True)
                return new_token
            except Exception as err:
                if token:
                    self.logger.warning("Token refresh failed, using stored token: %s", err)
                    return SecretStr(token)
                raise LoginFailed("Failed to refresh Yandex music token from x_token") from err

        if token:
            return SecretStr(token)

        raise LoginFailed("No Yandex Music token configured")

    # ------------------------------------------------------------------
    # Ynison state handling
    # ------------------------------------------------------------------

    async def _handle_ynison_state(self, state: YnisonState) -> None:
        """Handle state update from Ynison."""
        is_our_device = state.active_device_id == self._device_id

        # Detailed queue logging for diagnostics
        queue = state.player_state.get("player_queue", {})
        playable_list = queue.get("playable_list", [])
        current_index = queue.get("current_playable_index", -1)
        entity_type = queue.get("entity_type", "")
        entity_id = queue.get("entity_id", "")
        track_id = state.current_track_id
        self.logger.debug(
            "Ynison state: active_device=%s (ours=%s) track=%s "
            "index=%d/%d entity=%s type=%s paused=%s progress=%dms",
            state.active_device_id,
            is_our_device,
            track_id,
            current_index,
            len(playable_list),
            entity_id[:40] if entity_id else "<none>",
            entity_type,
            state.is_paused,
            state.progress_ms,
        )

        if is_our_device and not state.is_paused:
            # Pre-fetch next batch when playing second-to-last track
            self._maybe_prefetch(current_index, playable_list, entity_id, entity_type)
            await self._activate_playback(state)
        elif is_our_device and state.is_paused:
            # Our device but paused — stop player, keep association
            await self._pause_playback()
        elif self._source_details.in_use_by:
            # Active device switched away — fully release player
            self._clear_active_player()

    async def _activate_playback(self, state: YnisonState) -> None:
        """Activate playback on the target MA player."""
        target_player_id = self._get_target_player_id()
        if not target_player_id:
            self.logger.warning("Ynison active on our device but no MA player available")
            return

        # Detect resume after pause: stream was stopped but player still associated
        needs_reselect = self._stream_stop_event.is_set()
        self._stream_stop_event.clear()

        # Select source on the target player if not already active or resuming.
        # Do not pre-set in_use_by: the player controller relies on it to detect
        # and stop any previous player during handover.
        if self._source_details.in_use_by != target_player_id or needs_reselect:
            self._active_player_id = target_player_id
            self.mass.create_task(
                self.mass.players.select_source(target_player_id, self.instance_id)
            )

        # Signal track change if track_id changed
        significant_change = False
        new_track = state.current_track_id
        if new_track and new_track != self._current_streaming_track_id:
            self.logger.info("Track changed: %s -> %s", self._current_streaming_track_id, new_track)
            self._seek_position_ms = state.progress_ms
            self._track_changed_event.set()
            significant_change = True
            # Start prebuffering immediately — before the player HTTP GET
            if self._yandex_provider:
                self.mass.create_task(self._start_prebuffer(new_track, state.progress_ms))
        elif new_track and new_track == self._current_streaming_track_id:
            # Detect seek: compare reported progress with expected progress
            # Expected = last known progress + elapsed wall-clock time
            now = time.monotonic()
            elapsed_ms = (now - self._last_progress_time) * 1000 if self._last_progress_time else 0
            expected_ms = self._last_progress_ms + elapsed_ms
            drift_ms = abs(state.progress_ms - expected_ms)
            if drift_ms > 2000:
                self.logger.info(
                    "Seek detected on track %s: expected ~%dms, got %dms (drift %dms)",
                    new_track,
                    int(expected_ms),
                    state.progress_ms,
                    int(drift_ms),
                )
                self._seek_position_ms = state.progress_ms
                self._track_changed_event.set()
                significant_change = True
        self._last_progress_ms = state.progress_ms
        self._last_progress_time = time.monotonic()

        # Update metadata from state
        self._update_metadata(state)

        # Always trigger player update on significant changes;
        # throttle regular updates to avoid UI churn (every 5 seconds).
        # Use force_update on seek/track change so the server broadcasts a full
        # PLAYER_UPDATED event instead of a lightweight elapsed-time-only one
        # that the frontend may not handle for PluginSource players.
        now_mono = time.monotonic()
        if significant_change or needs_reselect or now_mono - self._last_player_update_time >= 5.0:
            self.mass.players.trigger_player_update(
                target_player_id, force_update=significant_change
            )
            self._last_player_update_time = now_mono

    def _update_metadata(self, state: YnisonState) -> None:
        """Update PluginSource metadata from Ynison state."""
        if self._source_details.metadata is None:
            self._source_details.metadata = StreamMetadata(
                title=f"Yandex Music Connect | {self._display_name}",
            )

        meta = self._source_details.metadata

        # Update duration (prefer actual from stream_details) and elapsed time
        best_duration = self._best_duration_ms()
        if best_duration:
            meta.duration = best_duration // 1000
        if state.progress_ms is not None:
            meta.elapsed_time = state.progress_ms // 1000
            meta.elapsed_time_last_updated = time.time()

        # Extract track info from player state if available
        queue = state.player_state.get("player_queue", {})
        playable_list = queue.get("playable_list", [])
        index = queue.get("current_playable_index", 0)
        if playable_list and 0 <= index < len(playable_list):
            playable = playable_list[index]
            title = playable.get("title")
            if title:
                meta.title = title
            cover = playable.get("cover_url_optional")
            if cover and not cover.startswith("http"):
                cover = f"https://{cover}"
            if cover:
                # Replace %% placeholder with size
                cover = cover.replace("%%", "400x400")
            meta.image_url = cover

    async def _update_metadata_from_stream(
        self, stream_details: StreamDetails, seek_ms: int = 0
    ) -> None:
        """Update PluginSource metadata from stream details (authoritative for duration)."""
        if self._source_details.metadata is None:
            self._source_details.metadata = StreamMetadata(
                title=f"Yandex Music Connect | {self._display_name}",
            )
        meta = self._source_details.metadata
        if stream_details.duration:
            meta.duration = stream_details.duration
            self._actual_duration_ms = stream_details.duration * 1000
            # Push the real duration to Ynison so the YM app shows
            # the correct value (we send duration_ms=0 on advance to
            # prevent stale propagation, so this corrects it).
            if self._ynison:
                await self._ynison.update_playing_status(
                    progress_ms=seek_ms,
                    duration_ms=self._actual_duration_ms,
                    paused=self._ynison.state.is_paused,
                )
        meta.elapsed_time = seek_ms // 1000 if seek_ms else 0
        meta.elapsed_time_last_updated = time.time()
        if self._source_details.in_use_by:
            self.mass.players.trigger_player_update(
                self._source_details.in_use_by, force_update=True
            )

    async def _pause_playback(self) -> None:
        """Handle pause — stop streaming but keep player association for resume."""
        self._stream_stop_event.set()
        self._last_progress_time = 0.0  # reset so resume doesn't trigger false seek
        player_id = self._source_details.in_use_by
        if player_id:
            try:
                await self.mass.players.cmd_stop(player_id)
            except Exception:
                self.logger.debug("Failed to stop player %s on pause", player_id)

    async def _handle_ynison_disconnect(self) -> None:
        """Handle permanent disconnect from Ynison."""
        self.logger.error("Ynison connection permanently lost")
        self._clear_active_player()

    # ------------------------------------------------------------------
    # Player selection
    # ------------------------------------------------------------------

    def _get_target_player_id(self) -> str | None:
        """Determine the target player ID for playback."""
        # If there's an active player, validate it still exists
        if self._active_player_id:
            if self.mass.players.get_player(self._active_player_id):
                return self._active_player_id
            self._active_player_id = None

        # Auto selection
        if self._default_player_id == PLAYER_ID_AUTO:
            all_players = list(self.mass.players.all_players(False, False))
            # Prefer currently playing player
            for player in all_players:
                if player.state.playback_state == PlaybackState.PLAYING:
                    self.logger.debug("Auto-selecting playing player: %s", player.display_name)
                    return player.player_id
            # Fallback to first available
            if all_players:
                return all_players[0].player_id
            return None

        # Specific configured player
        if self.mass.players.get_player(self._default_player_id):
            return self._default_player_id

        self.logger.warning(
            "Configured default player '%s' no longer exists",
            self._default_player_id,
        )
        return None

    async def _on_source_selected(self) -> None:
        """Handle callback when this source is selected on a player."""
        new_player_id = self._source_details.in_use_by
        if not new_player_id:
            return

        # Check if manual player switching is allowed
        if not self._allow_player_switch:
            current_target = self._get_target_player_id()
            if new_player_id != current_target:
                self.logger.debug(
                    "Player switching disabled, rejecting selection on %s",
                    new_player_id,
                )
                self._source_details.in_use_by = current_target
                # Revert the rejected player's active_source back to its MA queue
                # (the controller already set it to our plugin before calling on_select)
                try:
                    await self.mass.players.select_source(new_player_id, new_player_id)
                except Exception:
                    self.logger.debug("Could not revert active_source for %s", new_player_id)
                if current_target:
                    self.mass.players.trigger_player_update(current_target)
                msg = (
                    "Player switching is disabled; source must remain on "
                    f"{current_target or 'the configured target player'}"
                )
                raise RuntimeError(msg)

        # Stop previous player if switching
        if self._active_player_id and self._active_player_id != new_player_id:
            self.logger.info(
                "Source selected on %s, stopping %s",
                new_player_id,
                self._active_player_id,
            )
            try:
                await self.mass.players.cmd_stop(self._active_player_id)
            except Exception as err:
                self.logger.debug(
                    "Failed to stop previous player %s: %s",
                    self._active_player_id,
                    err,
                )

        self._active_player_id = new_player_id
        self.logger.debug("Active player set to: %s", new_player_id)

    def _clear_active_player(self) -> None:
        """Clear the active player and reset plugin state."""
        prev_player_id = self._active_player_id
        was_in_use = self._source_details.in_use_by == prev_player_id
        self._active_player_id = None
        self._source_details.in_use_by = None
        self._stream_stop_event.set()
        self._prefetched_list = None
        if self._prefetch_task and not self._prefetch_task.done():
            self._prefetch_task.cancel()
        if self._prebuffer:
            self.mass.create_task(self._prebuffer.cancel())
            self._prebuffer = None

        if prev_player_id:
            self.logger.debug(
                "Playback ended on player %s, clearing active player",
                prev_player_id,
            )
            if was_in_use:
                self.mass.create_task(self.mass.players.cmd_stop(prev_player_id))
            self.mass.players.trigger_player_update(prev_player_id)

    # ------------------------------------------------------------------
    # Yandex Music provider matching
    # ------------------------------------------------------------------

    def _on_provider_event(self, event: MassEvent) -> None:
        """Handle provider added/removed events."""
        self.mass.create_task(self._check_yandex_provider_match())

    async def _check_yandex_provider_match(self) -> None:
        """Check if a Yandex Music provider is available for audio streaming."""
        for provider in self.mass.get_providers():
            if provider.domain == "yandex_music" and provider.type == ProviderType.MUSIC:
                self.logger.debug("Found Yandex Music provider — enabling playback control")
                self._yandex_provider = provider
                self._update_source_capabilities()
                return

        if self._yandex_provider is not None:
            self.logger.debug(
                "Yandex Music provider no longer available — disabling playback control"
            )
            self._yandex_provider = None
            self._update_source_capabilities()

    def _update_source_capabilities(self) -> None:
        """Update source capabilities based on linked provider availability."""
        has_provider = self._yandex_provider is not None
        self._source_details.can_play_pause = has_provider
        self._source_details.can_seek = has_provider
        self._source_details.can_next_previous = has_provider

        if has_provider:
            self._source_details.on_play = self._on_play
            self._source_details.on_pause = self._on_pause
            self._source_details.on_next = self._on_next
            self._source_details.on_previous = self._on_previous
            self._source_details.on_seek = self._on_seek
        else:
            self._source_details.on_play = None
            self._source_details.on_pause = None
            self._source_details.on_next = None
            self._source_details.on_previous = None
            self._source_details.on_seek = None

        if self._source_details.in_use_by:
            self.mass.players.trigger_player_update(self._source_details.in_use_by)

    # ------------------------------------------------------------------
    # Playback control callbacks
    # ------------------------------------------------------------------

    def _best_duration_ms(self) -> int:
        """Return the best known duration: actual from stream, or Ynison state as fallback."""
        if self._actual_duration_ms > 0:
            return self._actual_duration_ms
        if self._ynison:
            return self._ynison.state.duration_ms
        return 0

    async def _on_play(self) -> None:
        """Handle play command — send resume to Ynison."""
        if not self._ynison:
            raise UnsupportedFeaturedException("Not connected to Ynison")
        state = self._ynison.state
        await self._ynison.update_playing_status(
            progress_ms=state.progress_ms,
            duration_ms=self._best_duration_ms(),
            paused=False,
        )

    async def _on_pause(self) -> None:
        """Handle pause command — send pause to Ynison."""
        if not self._ynison:
            raise UnsupportedFeaturedException("Not connected to Ynison")
        state = self._ynison.state
        await self._ynison.update_playing_status(
            progress_ms=state.progress_ms,
            duration_ms=self._best_duration_ms(),
            paused=True,
        )

    _RADIO_ENTITY_TYPES = {"RADIO"}

    def _maybe_prefetch(
        self,
        current_index: int,
        playable_list: list[dict[str, Any]],
        entity_id: str,
        entity_type: str,
    ) -> None:
        """Kick off background prefetch when playing the second-to-last track."""
        if entity_type not in self._RADIO_ENTITY_TYPES:
            return
        if not self._yandex_provider or not playable_list:
            return
        # second-to-last = index == len - 2
        if current_index != len(playable_list) - 2:
            return
        # Already prefetched or prefetch in progress
        if self._prefetched_list is not None:
            return
        if self._prefetch_task and not self._prefetch_task.done():
            return

        self.logger.info(
            "Pre-fetching tracks (at index %d/%d, entity=%s)",
            current_index,
            len(playable_list),
            entity_id[:40] if entity_id else "<none>",
        )

        async def _do_prefetch() -> None:
            result = await self._replenish_radio_queue(entity_id, entity_type, playable_list)
            if result:
                self._prefetched_list = result

        self._prefetch_task = asyncio.create_task(_do_prefetch())

    async def _signal_track_completion(self) -> None:
        """Signal that the current track finished playing.

        Ynison is a state-sync protocol — the active device must advance
        current_playable_index itself.

        If the next index is within the playable list, we advance immediately.
        If we're at the end (typical for RADIO/wave with short queues),
        we fetch more tracks via the Yandex Music API, append them to the
        playable_list, and then advance.
        """
        if not self._ynison:
            return
        state = self._ynison.state
        duration = self._best_duration_ms()
        queue = state.player_state.get("player_queue", {})
        current_index = queue.get("current_playable_index", 0)
        playable_list = queue.get("playable_list", [])
        entity_type = queue.get("entity_type", "")
        entity_id = queue.get("entity_id", "")
        next_index = current_index + 1

        self.logger.info(
            "Track finished at index %d/%d (entity=%s type=%s), "
            "advancing to index %d (duration=%dms)",
            current_index,
            len(playable_list),
            entity_id[:40] if entity_id else "<none>",
            entity_type,
            next_index,
            duration,
        )
        self._actual_duration_ms = 0

        # 1. Report that playback reached the end.
        # Update progress tracking so the Ynison echo of this message
        # doesn't trigger false seek detection in _activate_playback.
        self._last_progress_ms = duration
        self._last_progress_time = time.monotonic()
        await self._ynison.update_playing_status(
            progress_ms=duration, duration_ms=duration, paused=False
        )

        if next_index < len(playable_list):
            # 2a. Queue has room — advance immediately
            await self._advance_queue_index(next_index)
        elif entity_type in self._RADIO_ENTITY_TYPES:
            # 2b. At end of RADIO queue — use prefetched data or fetch now
            expanded: list[dict[str, Any]] | None = None
            if self._prefetched_list:
                self.logger.info("Using pre-fetched queue (%d items)", len(self._prefetched_list))
                expanded = self._prefetched_list
                self._prefetched_list = None
            elif self._prefetch_task and not self._prefetch_task.done():
                self.logger.info("Waiting for in-flight prefetch...")
                await self._prefetch_task
                expanded = self._prefetched_list
                self._prefetched_list = None
            else:
                expanded = await self._replenish_radio_queue(entity_id, entity_type, playable_list)
            if expanded:
                await self._advance_queue_index(next_index, expanded_list=expanded)
            else:
                self.logger.warning(
                    "Could not replenish queue (entity=%s type=%s), cannot advance",
                    entity_id,
                    entity_type,
                )
        else:
            self.logger.info(
                "End of non-radio queue (entity=%s type=%s), playback complete",
                entity_id[:40] if entity_id else "<none>",
                entity_type,
            )

    async def _replenish_radio_queue(
        self,
        entity_id: str,
        entity_type: str,
        playable_list: list[dict[str, Any]],
    ) -> list[dict[str, Any]] | None:
        """Fetch more tracks from Yandex Music API and return expanded playable_list.

        The active device is responsible for replenishing RADIO/wave queues.
        Ynison only syncs state — it does NOT generate new tracks.
        """
        if not self._yandex_provider:
            self.logger.warning("No yandex_music provider available for radio replenishment")
            return None

        # Determine the last track ID for pagination
        last_track_id: str | None = None
        if playable_list:
            last_track_id = playable_list[-1].get("playable_id")

        self.logger.info(
            "Fetching more tracks for %s station %s (queue=%s)",
            entity_type,
            entity_id,
            last_track_id,
        )

        try:
            client = self._yandex_provider.client
            tracks, batch_id = await client.get_rotor_station_tracks(entity_id, queue=last_track_id)
        except Exception:
            self.logger.exception("Failed to fetch radio tracks for %s", entity_id)
            return None

        if not tracks:
            self.logger.warning("No tracks returned for station %s", entity_id)
            return None

        # Determine the 'from' field from existing items
        from_field = ""
        if playable_list:
            from_field = playable_list[0].get("from", "")

        # Convert tracks to Ynison playable_list format
        new_items: list[dict[str, Any]] = []
        for track in tracks:
            album_id = ""
            if hasattr(track, "albums") and track.albums:
                album_id = str(track.albums[0].id) if track.albums[0].id else ""
            cover = ""
            if hasattr(track, "cover_uri") and track.cover_uri:
                cover = track.cover_uri
            new_items.append(
                {
                    "playable_id": str(track.id),
                    "album_id_optional": album_id,
                    "playable_type": "TRACK",
                    "from": from_field,
                    "title": track.title or "",
                    "cover_url_optional": cover,
                }
            )

        self.logger.info(
            "Fetched %d new tracks for station %s (batch=%s)",
            len(new_items),
            entity_id,
            batch_id,
        )

        return list(playable_list) + new_items

    async def _advance_queue_index(
        self,
        next_index: int,
        *,
        expanded_list: list[dict[str, Any]] | None = None,
    ) -> None:
        """Send update_player_state to advance the queue to next_index.

        If expanded_list is provided, it replaces the playable_list
        (used after radio queue replenishment).
        """
        if not self._ynison:
            return
        state = self._ynison.state
        queue = state.player_state.get("player_queue", {})
        new_state = dict(state.player_state)
        new_state["player_queue"] = dict(queue)
        new_state["player_queue"]["current_playable_index"] = next_index
        if expanded_list is not None:
            new_state["player_queue"]["playable_list"] = expanded_list
        new_state["status"] = dict(new_state.get("status", {}))
        new_state["status"]["progress_ms"] = 0
        new_state["status"]["duration_ms"] = 0
        new_state["status"]["paused"] = False
        await self._ynison.update_player_state(player_state=new_state)

    async def _on_next(self) -> None:
        """Handle next track command — signal track end so Yandex advances."""
        if not self._ynison:
            raise UnsupportedFeaturedException("Not connected to Ynison")
        await self._signal_track_completion()

    async def _on_previous(self) -> None:
        """Handle previous track command — update queue index in Ynison."""
        if not self._ynison:
            raise UnsupportedFeaturedException("Not connected to Ynison")
        state = self._ynison.state
        queue = state.player_state.get("player_queue", {})
        current_index = queue.get("current_playable_index", 0)
        if current_index > 0:
            new_state = dict(state.player_state)
            new_state["player_queue"] = dict(queue)
            new_state["player_queue"]["current_playable_index"] = current_index - 1
            new_state["status"] = dict(new_state.get("status", {}))
            new_state["status"]["progress_ms"] = 0
            new_state["status"]["duration_ms"] = 0
            new_state["status"]["paused"] = False
            self._actual_duration_ms = 0
            await self._ynison.update_player_state(player_state=new_state)

    async def _on_seek(self, position: int) -> None:
        """Handle seek command — send position update to Ynison.

        :param position: Position in seconds from Music Assistant.
        """
        if not self._ynison:
            raise UnsupportedFeaturedException("Not connected to Ynison")
        seek_ms = position * 1000
        state = self._ynison.state
        await self._ynison.update_playing_status(
            progress_ms=seek_ms,
            duration_ms=self._best_duration_ms(),
            paused=state.is_paused,
        )
        # Also trigger local stream restart so seek takes effect
        # immediately without waiting for the Ynison echo.
        self._seek_position_ms = seek_ms
        self._track_changed_event.set()
        # Cancel prebuffer — seek changes the stream position
        if self._prebuffer:
            self.mass.create_task(self._prebuffer.cancel())
            self._prebuffer = None
