"""Yandex Ynison plugin provider for Music Assistant."""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncGenerator, Callable
from contextlib import suppress
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


class YandexYnisonProvider(PluginProvider):
    """Implementation of the Yandex Music Connect (Ynison) Plugin."""

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

        # PluginSource
        self._source_details = PluginSource(
            id=self.instance_id,
            name=self.name,
            passive=not self._allow_player_switch,
            can_play_pause=False,
            can_seek=False,
            can_next_previous=False,
            audio_format=AudioFormat(
                content_type=ContentType.PCM_S16LE,
                codec_type=ContentType.PCM_S16LE,
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

            # Clear event before reading state to avoid missing updates
            # that arrive between the read and clear
            track_id = self._ynison.state.current_track_id
            self._track_changed_event.clear()
            self._current_streaming_track_id = track_id

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

            # Stream the current track (with seek offset if any)
            seek_ms = self._seek_position_ms
            self._seek_position_ms = 0
            async for chunk in self._stream_track(track_id, seek_ms=seek_ms):
                yield chunk
                # Check if track changed, stopped, or source deselected
                if (
                    self._track_changed_event.is_set()
                    or self._stream_stop_event.is_set()
                    or self._source_details.in_use_by != player_id
                ):
                    break

            self._current_streaming_track_id = None

            if self._stream_stop_event.is_set():
                break

            # Track finished naturally — advance queue in Ynison
            if not self._track_changed_event.is_set() and self._ynison:
                self.logger.info("Track %s finished, advancing to next", track_id)
                await self._advance_queue()
                # Wait for Ynison to confirm the track change
                try:
                    await asyncio.wait_for(self._track_changed_event.wait(), timeout=10.0)
                except TimeoutError:
                    self.logger.debug("No track change after advance, stopping stream")
                    break

    async def _stream_track(self, track_id: str, seek_ms: int = 0) -> AsyncGenerator[bytes, None]:
        """Stream a single track by ID, yielding PCM chunks.

        Converts source audio to PCM via ffmpeg since MA reads audio_format
        before our generator starts — dynamic format updates are too late.
        """
        try:
            stream_details = await self._yandex_provider.get_stream_details(
                track_id, MediaType.TRACK
            )
        except Exception:
            self.logger.exception("Failed to get stream details for track %s", track_id)
            return

        # Update metadata from stream details (authoritative source for duration)
        self._update_metadata_from_stream(stream_details, seek_ms)

        # Determine audio input based on stream type
        audio_input: AsyncGenerator[bytes, None] | str
        if stream_details.stream_type == StreamType.CUSTOM:
            audio_input = self._yandex_provider.get_audio_stream(stream_details)
            self.logger.info(
                "Streaming track %s (custom): format=%s", track_id, stream_details.audio_format
            )
        elif stream_details.stream_type == StreamType.HTTP and stream_details.path:
            audio_input = stream_details.path
            self.logger.info(
                "Streaming track %s (http): format=%s", track_id, stream_details.audio_format
            )
        else:
            self.logger.warning(
                "Unsupported stream type %s for track %s",
                stream_details.stream_type,
                track_id,
            )
            return

        # Build extra input args for seek
        extra_input_args: list[str] | None = None
        if seek_ms > 0:
            seek_sec = seek_ms / 1000.0
            extra_input_args = ["-ss", f"{seek_sec:.3f}"]
            self.logger.info("Seeking to %.1fs in track %s", seek_sec, track_id)

        async for chunk in get_ffmpeg_stream(
            audio_input=audio_input,
            input_format=stream_details.audio_format,
            output_format=self._source_details.audio_format,
            extra_input_args=extra_input_args,
        ):
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
            except LoginFailed:
                raise
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

        if is_our_device and not state.is_paused:
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

        # Select source on the target player if not already active or resuming
        if self._source_details.in_use_by != target_player_id or needs_reselect:
            self._active_player_id = target_player_id
            self._source_details.in_use_by = target_player_id
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

        # Update duration and elapsed time from player state
        if state.duration_ms:
            meta.duration = state.duration_ms // 1000
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

    def _update_metadata_from_stream(self, stream_details: StreamDetails, seek_ms: int = 0) -> None:
        """Update PluginSource metadata from stream details (authoritative for duration)."""
        if self._source_details.metadata is None:
            self._source_details.metadata = StreamMetadata(
                title=f"Yandex Music Connect | {self._display_name}",
            )
        meta = self._source_details.metadata
        if stream_details.duration:
            meta.duration = stream_details.duration
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
                self.mass.players.trigger_player_update(new_player_id)
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

    async def _on_play(self) -> None:
        """Handle play command — send resume to Ynison."""
        if not self._ynison:
            raise UnsupportedFeaturedException("Not connected to Ynison")
        state = self._ynison.state
        await self._ynison.update_playing_status(
            progress_ms=state.progress_ms,
            duration_ms=state.duration_ms,
            paused=False,
        )

    async def _on_pause(self) -> None:
        """Handle pause command — send pause to Ynison."""
        if not self._ynison:
            raise UnsupportedFeaturedException("Not connected to Ynison")
        state = self._ynison.state
        await self._ynison.update_playing_status(
            progress_ms=state.progress_ms,
            duration_ms=state.duration_ms,
            paused=True,
        )

    async def _advance_queue(self) -> None:
        """Advance to the next track in the Ynison queue."""
        if not self._ynison:
            return
        state = self._ynison.state
        queue = state.player_state.get("player_queue", {})
        current_index = queue.get("current_playable_index", 0)
        playable_list = queue.get("playable_list", [])
        if current_index + 1 < len(playable_list):
            new_state = dict(state.player_state)
            new_state["player_queue"] = dict(queue)
            new_state["player_queue"]["current_playable_index"] = current_index + 1
            new_state["status"] = dict(new_state.get("status", {}))
            new_state["status"]["progress_ms"] = 0
            new_state["status"]["paused"] = False
            await self._ynison.send_full_state(player_state=new_state)
        else:
            self.logger.info("Queue exhausted, no next track")
            self._stream_stop_event.set()

    async def _on_next(self) -> None:
        """Handle next track command — update queue index in Ynison."""
        if not self._ynison:
            raise UnsupportedFeaturedException("Not connected to Ynison")
        await self._advance_queue()

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
            new_state["status"]["paused"] = False
            await self._ynison.send_full_state(player_state=new_state)

    async def _on_seek(self, position: int) -> None:
        """Handle seek command — send position update to Ynison.

        :param position: Position in seconds from Music Assistant.
        """
        if not self._ynison:
            raise UnsupportedFeaturedException("Not connected to Ynison")
        state = self._ynison.state
        await self._ynison.update_playing_status(
            progress_ms=position * 1000,
            duration_ms=state.duration_ms,
            paused=state.is_paused,
        )
