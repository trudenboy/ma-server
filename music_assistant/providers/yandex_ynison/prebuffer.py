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
from .streaming import PROBE_ARGS, compute_rms_pct, pacing_args

if TYPE_CHECKING:
    from music_assistant_models.media_items import AudioFormat

_EOF_PUT_TIMEOUT = 5.0
_GARBAGE_RMS_PCT = 55.0  # RMS > this → likely garbage (white noise ≈ 57.7%)
_GARBAGE_RETRY_DELAY = 0.5  # seconds to wait before retry


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
        self.ready.set()
        with suppress(asyncio.QueueFull):
            self.queue.put_nowait(None)


# Type alias for the callback invoked after stream details are fetched
OnStreamDetails = Callable[[StreamDetails, int], Awaitable[None]]


class _GarbageDetected(Exception):
    """Raised internally when the first PCM chunk looks like garbage."""


async def _feed_chunks(
    prebuffer: PreBuffer,
    audio_gen: AsyncGenerator[bytes, None],
    output_format: AudioFormat,
    *,
    check_garbage: bool,
) -> None:
    """Read chunks from *audio_gen* into the prebuffer queue.

    Raises _GarbageDetected on the first chunk if *check_garbage* is True and
    the RMS exceeds the threshold.  The caller is responsible for tearing down
    the generator and retrying.
    """
    async for chunk in audio_gen:
        if check_garbage and prebuffer.chunks_queued == 0:
            rms_pct = compute_rms_pct(chunk, output_format)
            if rms_pct > _GARBAGE_RMS_PCT:
                raise _GarbageDetected(f"RMS={rms_pct:.1f}% for {prebuffer.track_id}")

        prebuffer.chunks_queued += 1
        if not prebuffer.ready.is_set() and prebuffer.chunks_queued >= prebuffer.ready_threshold:
            prebuffer.ready.set()
        # Blocking put — backpressure pauses the producer until the consumer
        # drains the queue.  No timeout: matches MA server's AudioBuffer
        # behaviour and avoids the data-loss bug where a 30 s timeout expired
        # before the consumer started reading the next-track prebuffer.
        await prebuffer.queue.put(chunk)


async def run_fill(
    prebuffer: PreBuffer,
    get_stream_details: Callable[[str, MediaType], Awaitable[StreamDetails]],
    get_audio_stream: Callable[[StreamDetails], AsyncGenerator[bytes, None]],
    output_format: AudioFormat,
    logger: logging.Logger,
    on_stream_details: OnStreamDetails | None = None,
    pacing_mode: str = PACING_READRATE,
    invalidate_cache: Callable[[str], Awaitable[None]] | None = None,
) -> None:
    """Fill a PreBuffer queue from a Yandex Music stream via ffmpeg.

    This is the shared implementation used by both current-track and
    next-track prebuffering.  The caller provides callbacks for fetching
    stream details and audio data.

    If the first PCM chunk looks like garbage (RMS > 55 %), the ffmpeg
    process is torn down and one retry is attempted with fresh stream
    details (cache invalidated).  This guards against intermittent
    container-detection failures in ffmpeg.
    """
    try:
        for attempt in range(2):
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

            try:
                await _feed_chunks(
                    prebuffer,
                    audio_gen,
                    output_format,
                    check_garbage=(attempt == 0),
                )
                break  # success or natural end
            except _GarbageDetected as gd:
                logger.warning("Garbage detected (%s), retrying with fresh stream", gd)
                await close_async_generator(audio_gen)
                prebuffer._audio_gen = None
                if invalidate_cache:
                    await invalidate_cache(prebuffer.track_id)
                await asyncio.sleep(_GARBAGE_RETRY_DELAY)
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
