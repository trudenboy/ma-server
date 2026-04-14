"""Pre-buffer management for gapless track transitions.

Handles pre-buffering of audio data into asyncio queues so that when a player
requests audio, data is already available — reducing latency on track changes.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncGenerator, Awaitable, Callable
from contextlib import suppress
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from music_assistant_models.enums import MediaType
from music_assistant_models.streamdetails import StreamDetails

from music_assistant.helpers.ffmpeg import get_ffmpeg_stream
from music_assistant.helpers.util import close_async_generator

from .constants import PACING_READRATE
from .streaming import PROBE_ARGS, pacing_args

if TYPE_CHECKING:
    from music_assistant_models.media_items import AudioFormat

_QUEUE_PUT_TIMEOUT = 30.0
_EOF_PUT_TIMEOUT = 5.0


@dataclass
class PreBuffer:
    """Holds pre-buffered audio data for an upcoming track."""

    track_id: str
    seek_ms: int
    output_format: AudioFormat
    queue: asyncio.Queue[bytes | None] = field(default_factory=lambda: asyncio.Queue(maxsize=64))
    stream_details: StreamDetails | None = None
    error: Exception | None = None
    task: asyncio.Task[None] | None = None
    chunks_queued: int = 0
    ready: asyncio.Event = field(default_factory=asyncio.Event)
    ready_threshold: int = 8
    _audio_gen: AsyncGenerator[bytes, None] | None = field(default=None, repr=False)

    async def cancel(self) -> None:
        """Cancel the prebuffer task, close generators, drain queue."""
        if self.task and not self.task.done():
            self.task.cancel()
            with suppress(asyncio.CancelledError):
                await self.task
        # Close the ffmpeg generator after the task exits to avoid
        # "generator is already running" errors.
        if self._audio_gen is not None:
            await close_async_generator(self._audio_gen)
            self._audio_gen = None
        while True:
            try:
                self.queue.get_nowait()
            except asyncio.QueueEmpty:
                break
        with suppress(asyncio.QueueFull):
            self.queue.put_nowait(None)


# Type alias for the callback invoked after stream details are fetched
OnStreamDetails = Callable[[StreamDetails, int], Awaitable[None]]


async def run_fill(
    prebuffer: PreBuffer,
    get_stream_details: Callable[[str, MediaType], Awaitable[StreamDetails]],
    get_audio_stream: Callable[[StreamDetails], AsyncGenerator[bytes, None]],
    output_format: AudioFormat,
    logger: logging.Logger,
    on_stream_details: OnStreamDetails | None = None,
    pacing_mode: str = PACING_READRATE,
) -> None:
    """Fill a PreBuffer queue from a Yandex Music stream via ffmpeg.

    This is the shared implementation used by both current-track and
    next-track prebuffering.  The caller provides callbacks for fetching
    stream details and audio data.

    :param prebuffer: The PreBuffer to fill.
    :param get_stream_details: Async callable to resolve stream details.
    :param get_audio_stream: Async generator factory for raw audio data.
    :param output_format: Target PCM format for ffmpeg transcoding.
    :param logger: Logger instance.
    :param on_stream_details: Optional callback after stream details are fetched
                              (e.g. to update metadata).  Receives (sd, seek_ms).
    :param pacing_mode: FFmpeg pacing mode (readrate / realtime / unlimited).
    """
    try:
        sd = await get_stream_details(prebuffer.track_id, MediaType.TRACK)
        prebuffer.stream_details = sd

        if on_stream_details is not None:
            await on_stream_details(sd, prebuffer.seek_ms)

        extra_input_args = PROBE_ARGS + pacing_args(pacing_mode)
        if prebuffer.seek_ms > 0:
            extra_input_args += ["-ss", f"{prebuffer.seek_ms / 1000.0:.3f}"]

        audio_gen = get_ffmpeg_stream(
            audio_input=get_audio_stream(sd),
            input_format=sd.audio_format,
            output_format=output_format,
            extra_input_args=extra_input_args,
        )
        prebuffer._audio_gen = audio_gen

        async for chunk in audio_gen:
            prebuffer.chunks_queued += 1
            if (
                not prebuffer.ready.is_set()
                and prebuffer.chunks_queued >= prebuffer.ready_threshold
            ):
                prebuffer.ready.set()
            try:
                await asyncio.wait_for(prebuffer.queue.put(chunk), timeout=_QUEUE_PUT_TIMEOUT)
            except TimeoutError:
                prebuffer.error = TimeoutError("Queue put timeout — consumer stalled")
                logger.warning(
                    "Prebuffer queue full for %.0fs, aborting for %s",
                    _QUEUE_PUT_TIMEOUT,
                    prebuffer.track_id,
                )
                await close_async_generator(audio_gen)
                prebuffer._audio_gen = None
                return
    except asyncio.CancelledError:
        raise
    except Exception as err:
        prebuffer.error = err
        logger.warning("Prebuffer failed for %s: %s", prebuffer.track_id, err)
    finally:
        prebuffer._audio_gen = None
        prebuffer.ready.set()
        logger.debug(
            "Prebuffer fill done for %s: %d chunks queued, error=%s",
            prebuffer.track_id,
            prebuffer.chunks_queued,
            prebuffer.error,
        )
        try:
            await asyncio.wait_for(prebuffer.queue.put(None), timeout=_EOF_PUT_TIMEOUT)
        except (TimeoutError, asyncio.CancelledError):
            with suppress(asyncio.QueueEmpty):
                prebuffer.queue.get_nowait()
            with suppress(asyncio.QueueFull):
                prebuffer.queue.put_nowait(None)


async def yield_from_prebuffer(prebuffer: PreBuffer) -> AsyncGenerator[bytes, None]:
    """Yield chunks from a prebuffer queue until EOF sentinel (None).

    Waits for the ``ready`` event before yielding the first chunk, so downstream
    consumers don't start until a minimum amount of audio has been buffered.
    """
    await prebuffer.ready.wait()
    while True:
        chunk = await prebuffer.queue.get()
        if chunk is None:
            break
        yield chunk


def make_prebuffer(
    track_id: str,
    seek_ms: int,
    output_format: AudioFormat,
    *,
    maxsize: int = 64,
    ready_threshold: int = 8,
) -> PreBuffer:
    """Create a new PreBuffer with the given parameters."""
    return PreBuffer(
        track_id=track_id,
        seek_ms=seek_ms,
        output_format=output_format,
        queue=asyncio.Queue(maxsize=maxsize),
        ready_threshold=ready_threshold,
    )
