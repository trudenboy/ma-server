"""PCM normalization helpers for Yandex Music audio streams.

Contains format profiles, the AudioFormat factory, and first-chunk
diagnostics used by both the direct streaming path and pre-buffering.
"""

from __future__ import annotations

import struct
from typing import Any

from music_assistant_models.enums import ContentType
from music_assistant_models.media_items import AudioFormat

# PCM normalization profiles by YM quality tier.
# Ensures MA's single ffmpeg receives a consistent format between tracks.
# NOTE: AudioFormat is a *mutable* dataclass — MA's FFMpeg._log_reader_task
# mutates input_format.codec_type in-place.  We MUST create a fresh copy for
# every place that stores a reference (PluginSource.audio_format, PreBuffer,
# ffmpeg output_format) so that mutation of one doesn't corrupt the others.
PCM_LOSSLESS_PARAMS: dict[str, Any] = {
    "content_type": ContentType.PCM_S24LE,
    "sample_rate": 48000,
    "bit_depth": 24,
    "channels": 2,
}
PCM_LOSSY_PARAMS: dict[str, Any] = {
    "content_type": ContentType.PCM_S16LE,
    "sample_rate": 44100,
    "bit_depth": 16,
    "channels": 2,
}

_SIGNED_24BIT_MAX = 0x800000
_SIGNED_24BIT_RANGE = 0x1000000


def make_pcm_format(params: dict[str, Any]) -> AudioFormat:
    """Create a fresh AudioFormat from stored params (safe from mutation)."""
    return AudioFormat(**params)


def log_first_chunk(logger: Any, chunk: bytes, fmt: AudioFormat) -> None:
    """Log diagnostic info about the first chunk of a track stream.

    Computes RMS amplitude of the first 1024 samples to help detect garbage
    data (which appears as near-maximum amplitude white noise / hissing).
    """
    if not chunk:
        return
    sample_width = fmt.bit_depth // 8
    if sample_width == 2:
        pack_fmt = "<h"
    elif sample_width == 3:
        pack_fmt = None  # 24-bit needs manual unpacking
    else:
        logger.debug(
            "First chunk: %d bytes (unsupported bit_depth=%d for RMS)",
            len(chunk),
            fmt.bit_depth,
        )
        return

    n_samples = min(1024, len(chunk) // sample_width)
    if n_samples == 0:
        logger.debug("First chunk: %d bytes (too small for RMS)", len(chunk))
        return

    sum_sq = 0.0
    for i in range(n_samples):
        offset = i * sample_width
        if pack_fmt:
            (sample,) = struct.unpack_from(pack_fmt, chunk, offset)
        else:
            # 24-bit little-endian signed
            b = chunk[offset : offset + 3]
            val = int.from_bytes(b, "little", signed=False)
            if val >= _SIGNED_24BIT_MAX:
                val -= _SIGNED_24BIT_RANGE
            sample = val
        sum_sq += sample * sample

    rms = (sum_sq / n_samples) ** 0.5
    max_val = (1 << (fmt.bit_depth - 1)) - 1
    rms_pct = (rms / max_val) * 100 if max_val else 0

    # RMS > 70% of max for raw PCM almost certainly indicates garbage data
    level = "WARNING" if rms_pct > 70 else "DEBUG"
    log_fn = logger.warning if level == "WARNING" else logger.debug
    log_fn(
        "First chunk: %d bytes, RMS=%.0f (%.1f%% of max %d), fmt=%s/%dHz/%dbit",
        len(chunk),
        rms,
        rms_pct,
        max_val,
        fmt.content_type.value,
        fmt.sample_rate,
        fmt.bit_depth,
    )
