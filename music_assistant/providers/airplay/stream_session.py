"""Unified AirPlay/RAOP stream session logic for AirPlay devices."""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncGenerator
from contextlib import suppress
from typing import TYPE_CHECKING

from music_assistant_models.errors import PlayerCommandFailed

from music_assistant.constants import CONF_SYNC_ADJUST
from music_assistant.helpers.ffmpeg import FFMpeg

from .constants import (
    AIRPLAY2_CONNECT_TIME_MS,
    CONF_ENABLE_LATE_JOIN,
    ENABLE_LATE_JOIN_DEFAULT,
    RAOP_CONNECT_TIME_MS,
    StreamingProtocol,
)
from .protocols.airplay2 import AirPlay2Stream
from .protocols.raop import RaopStream

if TYPE_CHECKING:
    from music_assistant_models.media_items import AudioFormat

    from .player import AirPlayPlayer
    from .provider import AirPlayProvider


class AirPlayStreamSession:
    """Stream session (RAOP or AirPlay2) to one or more players."""

    def __init__(
        self,
        airplay_provider: AirPlayProvider,
        sync_clients: list[AirPlayPlayer],
        pcm_format: AudioFormat,
    ) -> None:
        """Initialize AirPlayStreamSession.

        :param airplay_provider: The AirPlay provider instance.
        :param sync_clients: List of AirPlay players to stream to.
        :param pcm_format: PCM format of the input stream.
        """
        assert sync_clients
        self.prov = airplay_provider
        self.mass = airplay_provider.mass
        self.pcm_format = pcm_format
        self.sync_clients = sync_clients
        self._audio_source_task: asyncio.Task[None] | None = None
        self._player_ffmpeg: dict[str, FFMpeg] = {}
        self._lock = asyncio.Lock()
        self.start_ntp: int = 0
        self.start_time: float = 0.0
        self.wait_start: float = 0.0
        self.seconds_streamed: float = 0
        self.total_pause_time: float = 0.0
        self.last_paused: float | None = None
        self._first_chunk_received = asyncio.Event()

    async def start(self, audio_source: AsyncGenerator[bytes]) -> None:
        """Initialize stream session for all players."""
        cur_time = time.time()
        has_airplay2_client = any(
            p.protocol == StreamingProtocol.AIRPLAY2 for p in self.sync_clients
        )
        wait_start = AIRPLAY2_CONNECT_TIME_MS if has_airplay2_client else RAOP_CONNECT_TIME_MS
        wait_start_seconds = wait_start / 1000
        self.wait_start = wait_start_seconds
        self.start_time = cur_time + wait_start_seconds
        self.start_ntp = unix_time_to_ntp(self.start_time)
        await asyncio.gather(*[self._start_client(p, self.start_ntp) for p in self.sync_clients])
        self._audio_source_task = asyncio.create_task(self._audio_streamer(audio_source))
        try:
            await asyncio.gather(
                *[p.stream.wait_for_connection() for p in self.sync_clients if p.stream]
            )
        except Exception:
            # playback failed to start, cleanup
            await self.stop()
            raise PlayerCommandFailed("Playback failed to start")

    async def stop(self, force: bool = False) -> None:
        """Stop playback and cleanup."""
        if self._audio_source_task and not self._audio_source_task.done():
            self._audio_source_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._audio_source_task
        if force:
            await asyncio.gather(
                *[self.stop_client(x, force=True) for x in self.sync_clients],
            )
            self.sync_clients = []
        else:
            await asyncio.gather(
                *[self.remove_client(x) for x in self.sync_clients],
            )

    async def remove_client(
        self, airplay_player: AirPlayPlayer, reason: str = "client removed"
    ) -> None:
        """
        Remove a sync client from the session.

        :param airplay_player: The player to remove from the session.
        :param reason: Short human-readable reason for the removal, used in teardown logs.
        """
        async with self._lock:
            if airplay_player not in self.sync_clients:
                return
            self.sync_clients.remove(airplay_player)
        await self._cleanup_after_removal(airplay_player, reason=reason)

    async def _cleanup_after_removal(
        self, airplay_player: AirPlayPlayer, reason: str = "client removed"
    ) -> None:
        """
        Clean up processes and state after a client has been removed from sync_clients.

        :param airplay_player: The player whose processes should be stopped.
        :param reason: Short human-readable reason, forwarded to stop_client for logging.
        """
        stream = airplay_player.stream
        if stream is not None and stream.session != self:
            stream = None
        await self.stop_client(airplay_player)
        # If this was the last client, stop the session
        if not self.sync_clients:
            await self.stop()

    async def stop_client(self, airplay_player: AirPlayPlayer, force: bool = False) -> None:
        """
        Stop a client's stream and ffmpeg.

        :param airplay_player: The player to stop.
        :param reason: Short human-readable reason for the teardown, used in debug logs.
        """
        self.prov.logger.debug(
            "AirPlay session teardown: session=%s client=%s reason=%s",
            id(self),
            airplay_player.player_id,
            reason,
        )
        ffmpeg = self._player_ffmpeg.pop(airplay_player.player_id, None)
        if force:
            if ffmpeg and not ffmpeg.closed:
                await ffmpeg.kill()
            if airplay_player.stream and airplay_player.stream.session == self:
                await airplay_player.stream.stop(force=True)
        else:
            if ffmpeg and not ffmpeg.closed:
                await ffmpeg.close()
            if airplay_player.stream and airplay_player.stream.session == self:
                await airplay_player.stream.stop()

    async def add_client(self, airplay_player: AirPlayPlayer) -> None:
        """Add a sync client to the session as a late joiner.

        Uses the PCM ring buffer to prime the late joiner's pipeline so it
        starts playing quickly. All work happens under the lock to ensure
        ``seconds_streamed`` and the buffer are consistent.

        Devices generally cannot honour an NTP start anchor that is in the
        past — they just play whatever the pipe gives them, trailing the
        group by the deficit. To stay in sync we therefore push the late
        joiner's ``start_at`` into the future (at least ``wait_start`` ahead
        of now) and trim the corresponding amount from the head of the
        buffered PCM, so the first sample we send maps to the correct future
        stream position.

        1. Snapshot the ring buffer and calculate how many seconds it holds.
        2. Map the buffer's first byte to its stream position; if the
           resulting NTP start is in the past, shift it forward to
           ``now + min_headroom`` and trim that many seconds from the buffer
           head so timing stays aligned with the rest of the group.
        3. Start ffmpeg+CLI, write the (possibly trimmed) buffer into ffmpeg
           to prime the pipe while cliraop is still connecting, then add to
           sync_clients so the audio streamer continues seamlessly.
        """
        if not self.sync_clients:
            return
        first_client = self.sync_clients[0]
        if not first_client.stream or not first_client.stream.running:
            return

        allow_late_join = self.prov.config.get_value(
            CONF_ENABLE_LATE_JOIN, ENABLE_LATE_JOIN_DEFAULT
        )
        if not allow_late_join:
            await self.stop()
            if sync_leader.current_media:
                self.mass.call_later(
                    0.5,
                    self.mass.players.cmd_resume(sync_leader.player_id),
                    task_id=f"resync_session_{sync_leader.player_id}",
                )
            return

        async with self._lock:
            skip_seconds = self.seconds_streamed
            start_at = self.start_time + skip_seconds
            start_ntp = unix_time_to_ntp(start_at)
            if airplay_player not in self.sync_clients:
                self.sync_clients.append(airplay_player)

            await self._start_client(airplay_player, start_ntp)

    async def _audio_streamer(self, audio_source: AsyncGenerator[bytes]) -> None:
        """Stream audio to all players."""
        pcm_sample_size = self.pcm_format.pcm_sample_size
        watchdog_task = asyncio.create_task(self._silence_watchdog(pcm_sample_size))
        try:
            async for chunk in audio_source:
                if not self.sync_clients:
                    break

                await self._write_chunk_to_all_players(chunk)
                self.seconds_streamed += len(chunk) / pcm_sample_size
        finally:
            if not watchdog_task.done():
                watchdog_task.cancel()
                with suppress(asyncio.CancelledError):
                    await watchdog_task
        async with self._lock:
            await asyncio.gather(
                *[
                    self._write_eof_to_player(x)
                    for x in self.sync_clients
                    if x.stream and x.stream.running
                ],
                return_exceptions=True,
            )

    async def _silence_watchdog(self, pcm_sample_size: int) -> None:
        """Insert silence if audio source is slow to deliver first chunk."""
        grace_period = 0.2
        max_silence_padding = 5.0
        silence_inserted = 0.0

        await asyncio.sleep(grace_period)
        while not self._first_chunk_received.is_set() and silence_inserted < max_silence_padding:
            silence_duration = 0.1
            silence_bytes = int(pcm_sample_size * silence_duration)
            silence_chunk = bytes(silence_bytes)
            await self._write_chunk_to_all_players(silence_chunk)
            self.seconds_streamed += silence_duration
            silence_inserted += silence_duration
            await asyncio.sleep(0.05)

        if silence_inserted > 0:
            self.prov.logger.warning(
                "Inserted %.1fs silence padding while waiting for audio source",
                silence_inserted,
            )

    async def _write_chunk_to_all_players(self, chunk: bytes) -> None:
        """Write a chunk to all connected players."""
        async with self._lock:
            sync_clients = [x for x in self.sync_clients if x.stream and x.stream.running]
            if not sync_clients:
                return

            # Write chunk to all players
            write_tasks = [self._write_chunk_to_player(x, chunk) for x in sync_clients if x.stream]
            results = await asyncio.gather(*write_tasks, return_exceptions=True)

            # Check for write errors or timeouts
            players_to_remove: list[tuple[AirPlayPlayer, str]] = []
            for i, result in enumerate(results):
                if i >= len(sync_clients):
                    continue
                player = sync_clients[i]

                if isinstance(result, TimeoutError):
                    self.prov.logger.warning(
                        "Removing player %s from session: stopped reading data (write timeout)",
                        player.player_id,
                    )
                    players_to_remove.append((player, "audio write timeout"))
                elif isinstance(result, Exception):
                    self.prov.logger.warning(
                        "Removing player %s from session due to write error: %s",
                        player.player_id,
                        result,
                    )
                    players_to_remove.append((player, f"audio write error: {result}"))

            # Remove failed players from sync_clients immediately under the lock
            # so they are excluded from future write cycles. Only defer process
            # cleanup (_cleanup_after_removal) — this prevents fire-and-forget
            # remove_client calls from racing with a subsequent add_client when
            # a player is being moved between groups.
            for player, removal_reason in players_to_remove:
                if player in self.sync_clients:
                    self.sync_clients.remove(player)
                self.mass.create_task(self._cleanup_after_removal(player, reason=removal_reason))

    async def _write_chunk_to_player(self, airplay_player: AirPlayPlayer, chunk: bytes) -> None:
        """Write audio chunk to a player's ffmpeg process."""
        player_id = airplay_player.player_id
        if ffmpeg := self._player_ffmpeg.get(player_id):
            if ffmpeg.closed:
                return
            await asyncio.wait_for(ffmpeg.write(chunk), timeout=35.0)

    async def _write_eof_to_player(self, airplay_player: AirPlayPlayer) -> None:
        """Write EOF to a specific player."""
        if ffmpeg := self._player_ffmpeg.pop(airplay_player.player_id, None):
            await ffmpeg.write_eof()
            await ffmpeg.wait_with_timeout(30)
            if airplay_player.stream and airplay_player.stream._cli_proc:
                await airplay_player.stream._cli_proc.write_eof()

    async def _start_client(self, airplay_player: AirPlayPlayer, start_ntp: int) -> None:
        """Start CLI process and ffmpeg for a single client."""
        if airplay_player.stream and airplay_player.stream.running:
            await airplay_player.stream.stop()
        if airplay_player.protocol == StreamingProtocol.AIRPLAY2:
            airplay_player.stream = AirPlay2Stream(airplay_player)
        else:
            airplay_player.stream = RaopStream(airplay_player)
        airplay_player.stream.session = self
        sync_adjust = airplay_player.config.get_value(CONF_SYNC_ADJUST, 0)
        assert isinstance(sync_adjust, int)
        if sync_adjust != 0:
            start_ntp = unix_time_to_ntp(ntp_to_unix_time(start_ntp) + (sync_adjust / 1000))
        await airplay_player.stream.start(start_ntp)
        # Start ffmpeg to feed audio to CLI stdin
        if ffmpeg := self._player_ffmpeg.pop(airplay_player.player_id, None):
            await ffmpeg.close()
        filter_params = self.mass.streams.audio.get_player_filter_params(
            airplay_player.player_id,
            input_format=self.pcm_format,
            output_format=get_final_output_format(airplay_player.stream.pcm_format, airplay_player),
        )
        cli_proc = airplay_player.stream._cli_proc
        assert cli_proc
        assert cli_proc.proc
        assert cli_proc.proc.stdin
        stdin_transport = cli_proc.proc.stdin.transport
        audio_output: str | int = stdin_transport.get_extra_info("pipe").fileno()
        ffmpeg = FFMpeg(
            audio_input="-",
            input_format=self.pcm_format,
            output_format=airplay_player.stream.pcm_format,
            filter_params=filter_params,
            audio_output=audio_output,
        )
        await ffmpeg.start()
        self._player_ffmpeg[airplay_player.player_id] = ffmpeg
