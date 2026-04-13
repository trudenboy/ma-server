"""Crossfade support for gapless track transitions via MA's StandardCrossFade.

Provides a TailBuffer for accumulating the end of a track, a helper to
collect the head of the next track from a PreBuffer, and a thin wrapper
around MA's ``StandardCrossFade`` that adds silence stripping and frame
alignment before the crossfade.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncGenerator
from typing import TYPE_CHECKING

from music_assistant.controllers.streams.smart_fades.fades import StandardCrossFade
from music_assistant.helpers.audio import (
    align_audio_to_frame_boundary,
    iter_pcm_slices,
    strip_silence,
)

if TYPE_CHECKING:
    from music_assistant_models.media_items import AudioFormat

    from provider.prebuffer import PreBuffer

_HEAD_COLLECT_TIMEOUT = 10.0


class TailBuffer:
    """Rolling buffer that keeps the last *capacity* bytes of PCM audio.

    As audio chunks are pushed, bytes exceeding the capacity are returned
    as "overflow" to be yielded downstream immediately.  When the track
    ends, ``flush()`` returns the accumulated tail for crossfading.
    """

    __slots__ = ("_buf", "_capacity")

    def __init__(self, capacity: int) -> None:
        """Initialize with *capacity* bytes."""
        if capacity <= 0:
            raise ValueError(f"TailBuffer capacity must be > 0, got {capacity}")
        self._buf = bytearray()
        self._capacity = capacity

    @property
    def capacity(self) -> int:
        """Maximum number of bytes held in the buffer."""
        return self._capacity

    def push(self, chunk: bytes) -> bytes:
        """Append *chunk*, return overflow bytes that no longer fit.

        The returned bytes should be yielded to the consumer immediately.
        """
        self._buf.extend(chunk)
        if len(self._buf) <= self._capacity:
            return b""
        overflow = bytes(self._buf[: len(self._buf) - self._capacity])
        del self._buf[: len(self._buf) - self._capacity]
        return overflow

    def flush(self) -> bytes:
        """Return accumulated tail and reset the buffer."""
        tail = bytes(self._buf)
        self._buf.clear()
        return tail

    def __len__(self) -> int:
        """Return current number of bytes in the buffer."""
        return len(self._buf)


def crossfade_bytes_for(duration_s: float, pcm_format: AudioFormat) -> int:
    """Return the number of bytes needed for *duration_s* of PCM audio.

    The result is aligned to a whole number of PCM frames.
    """
    frame_size = (pcm_format.bit_depth // 8) * pcm_format.channels
    raw = int(pcm_format.pcm_sample_size * duration_s)
    return (raw // frame_size) * frame_size


async def collect_crossfade_head(
    prebuffer: PreBuffer,
    target_bytes: int,
    timeout: float = _HEAD_COLLECT_TIMEOUT,
) -> tuple[bytes, bool]:
    """Collect the first *target_bytes* of audio from *prebuffer*.

    Returns ``(data, eof_reached)``.  Stops early if the prebuffer's
    queue yields ``None`` (EOF) or if *timeout* seconds elapse.
    """
    buf = bytearray()
    eof = False
    try:
        async with asyncio.timeout(timeout):
            while len(buf) < target_bytes:
                chunk = await prebuffer.queue.get()
                if chunk is None:
                    eof = True
                    break
                buf.extend(chunk)
    except TimeoutError:
        pass
    return bytes(buf), eof


async def apply_crossfade(
    fade_out: bytes,
    fade_in: bytes | AsyncGenerator[bytes, None],
    pcm_format: AudioFormat,
    duration_s: float,
    logger: logging.Logger,
) -> AsyncGenerator[bytes, None]:
    """Apply crossfade between outgoing tail and incoming head.

    Wraps MA's :class:`StandardCrossFade` with silence stripping and
    frame alignment on the fade-out part (same preprocessing as
    ``SmartFadesMixer.mix`` uses for ``STANDARD_CROSSFADE`` mode).

    :param fade_out: Raw PCM bytes for the outgoing track's tail.
    :param fade_in: Raw PCM bytes or async generator for the incoming track.
    :param pcm_format: Audio format of both inputs and the output.
    :param duration_s: Crossfade duration in seconds.
    :param logger: Logger instance.
    """
    fade_out = await strip_silence(fade_out, pcm_format=pcm_format, reverse=True)
    fade_out = align_audio_to_frame_boundary(fade_out, pcm_format)

    if len(fade_out) == 0:
        # Nothing left after stripping — yield fade_in in frame-aligned slices
        if isinstance(fade_in, bytes):
            for sl in iter_pcm_slices(fade_in, pcm_format):
                yield sl
        else:
            async for chunk in fade_in:
                for sl in iter_pcm_slices(chunk, pcm_format):
                    yield sl
        return

    crossfader = StandardCrossFade(logger=logger, crossfade_duration=duration_s)
    async for chunk in crossfader.apply(fade_out, fade_in, pcm_format):
        for sl in iter_pcm_slices(chunk, pcm_format):
            yield sl
