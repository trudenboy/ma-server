"""Tests for provider/streaming.py — PCM helpers and first-chunk diagnostics."""

from __future__ import annotations

import logging
import struct
from unittest.mock import MagicMock

import pytest
from music_assistant_models.enums import ContentType
from music_assistant_models.media_items import AudioFormat

from music_assistant.providers.yandex_ynison.streaming import (
    _SIGNED_24BIT_MAX,
    _SIGNED_24BIT_RANGE,
    PCM_LOSSLESS_PARAMS,
    PCM_LOSSY_PARAMS,
    log_first_chunk,
    make_pcm_format,
)

# ---------------------------------------------------------------
# make_pcm_format
# ---------------------------------------------------------------


class TestMakePcmFormat:
    """Tests for the AudioFormat factory."""

    def test_lossless_format(self) -> None:
        """Lossless params produce s24le/48kHz/24bit/stereo."""
        fmt = make_pcm_format(PCM_LOSSLESS_PARAMS)
        assert isinstance(fmt, AudioFormat)
        assert fmt.content_type == ContentType.PCM_S24LE
        assert fmt.sample_rate == 48000
        assert fmt.bit_depth == 24
        assert fmt.channels == 2

    def test_lossy_format(self) -> None:
        """Lossy params produce s16le/44.1kHz/16bit/stereo."""
        fmt = make_pcm_format(PCM_LOSSY_PARAMS)
        assert isinstance(fmt, AudioFormat)
        assert fmt.content_type == ContentType.PCM_S16LE
        assert fmt.sample_rate == 44100
        assert fmt.bit_depth == 16
        assert fmt.channels == 2

    def test_returns_fresh_instances(self) -> None:
        """Each call must return a NEW AudioFormat to prevent mutation leaks."""
        fmt1 = make_pcm_format(PCM_LOSSY_PARAMS)
        fmt2 = make_pcm_format(PCM_LOSSY_PARAMS)
        assert fmt1 is not fmt2

    def test_custom_params(self) -> None:
        """Custom params (22050Hz, mono) create matching format."""
        params = {
            "content_type": ContentType.PCM_S16LE,
            "sample_rate": 22050,
            "bit_depth": 16,
            "channels": 1,
        }
        fmt = make_pcm_format(params)
        assert fmt.sample_rate == 22050
        assert fmt.channels == 1


# ---------------------------------------------------------------
# log_first_chunk — 16-bit
# ---------------------------------------------------------------


class TestLogFirstChunk16Bit:
    """Tests for log_first_chunk with 16-bit audio."""

    @pytest.fixture
    def logger(self) -> MagicMock:
        """Create a mock logger."""
        return MagicMock(spec=logging.Logger)

    @pytest.fixture
    def fmt_16(self) -> AudioFormat:
        """Create a 16-bit PCM format."""
        return make_pcm_format(PCM_LOSSY_PARAMS)

    def test_empty_chunk_returns_early(self, logger: MagicMock, fmt_16: AudioFormat) -> None:
        """Empty chunk logs debug about unsupported/too small."""
        log_first_chunk(logger, b"", fmt_16)
        logger.debug.assert_called_once()
        assert "unsupported or too small" in logger.debug.call_args[0][0]
        logger.warning.assert_not_called()

    def test_small_chunk_debug_message(self, logger: MagicMock, fmt_16: AudioFormat) -> None:
        """Chunk smaller than one sample → logged as too small."""
        log_first_chunk(logger, b"\x01", fmt_16)
        logger.debug.assert_called_once()
        assert "unsupported or too small" in logger.debug.call_args[0][0]

    def test_silent_16bit(self, logger: MagicMock, fmt_16: AudioFormat) -> None:
        """All-zero samples → RMS 0, debug level."""
        chunk = b"\x00\x00" * 1024
        log_first_chunk(logger, chunk, fmt_16)
        logger.debug.assert_called_once()
        assert "RMS=0" in logger.debug.call_args[0][0] % logger.debug.call_args[0][1:]

    def test_loud_16bit_triggers_warning(self, logger: MagicMock, fmt_16: AudioFormat) -> None:
        """Near-max samples → RMS > 70% → warning level."""
        max_val = 32767
        chunk = struct.pack("<h", max_val) * 1024
        log_first_chunk(logger, chunk, fmt_16)
        logger.warning.assert_called_once()

    def test_moderate_16bit_debug(self, logger: MagicMock, fmt_16: AudioFormat) -> None:
        """~25% level samples → below 70% threshold → debug level."""
        mid_val = 8000
        chunk = struct.pack("<h", mid_val) * 1024
        log_first_chunk(logger, chunk, fmt_16)
        logger.debug.assert_called_once()
        logger.warning.assert_not_called()


# ---------------------------------------------------------------
# log_first_chunk — 24-bit
# ---------------------------------------------------------------


class TestLogFirstChunk24Bit:
    """Tests for log_first_chunk with 24-bit audio."""

    @pytest.fixture
    def logger(self) -> MagicMock:
        """Create a mock logger."""
        return MagicMock(spec=logging.Logger)

    @pytest.fixture
    def fmt_24(self) -> AudioFormat:
        """Create a 24-bit PCM format."""
        return make_pcm_format(PCM_LOSSLESS_PARAMS)

    def test_silent_24bit(self, logger: MagicMock, fmt_24: AudioFormat) -> None:
        """All-zero 24-bit samples → RMS 0, debug level."""
        chunk = b"\x00\x00\x00" * 1024
        log_first_chunk(logger, chunk, fmt_24)
        logger.debug.assert_called_once()
        assert "RMS=0" in logger.debug.call_args[0][0] % logger.debug.call_args[0][1:]

    def test_loud_24bit_triggers_warning(self, logger: MagicMock, fmt_24: AudioFormat) -> None:
        """Near-max 24-bit samples → warning."""
        max_val = _SIGNED_24BIT_MAX - 1  # 0x7FFFFF
        b = max_val.to_bytes(3, "little", signed=False)
        chunk = b * 1024
        log_first_chunk(logger, chunk, fmt_24)
        logger.warning.assert_called_once()

    def test_negative_24bit_samples(self, logger: MagicMock, fmt_24: AudioFormat) -> None:
        """Negative 24-bit samples (val >= 0x800000) are correctly sign-extended."""
        # -1 in 24-bit signed = 0xFFFFFF
        neg_one = (0xFFFFFF).to_bytes(3, "little", signed=False)
        chunk = neg_one * 1024
        log_first_chunk(logger, chunk, fmt_24)
        # RMS of -1 repeated = 1, which is ~0.00001% — way below warning
        logger.debug.assert_called_once()
        logger.warning.assert_not_called()


# ---------------------------------------------------------------
# log_first_chunk — unsupported bit depth
# ---------------------------------------------------------------


class TestLogFirstChunkUnsupported:
    """Tests for log_first_chunk with unsupported bit depths."""

    @pytest.fixture
    def logger(self) -> MagicMock:
        """Create a mock logger."""
        return MagicMock(spec=logging.Logger)

    def test_8bit_unsupported(self, logger: MagicMock) -> None:
        """8-bit audio logs unsupported message."""
        fmt = AudioFormat(content_type=ContentType.PCM_S16LE, bit_depth=8, sample_rate=44100)
        log_first_chunk(logger, b"\x42" * 100, fmt)
        logger.debug.assert_called_once()
        msg = logger.debug.call_args[0][0] % logger.debug.call_args[0][1:]
        assert "bit_depth=8" in msg

    def test_32bit_unsupported(self, logger: MagicMock) -> None:
        """32-bit audio logs unsupported message."""
        fmt = AudioFormat(
            content_type=ContentType.PCM_S16LE,
            bit_depth=32,
            sample_rate=44100,
        )
        log_first_chunk(logger, b"\x42" * 100, fmt)
        logger.debug.assert_called_once()
        msg = logger.debug.call_args[0][0] % logger.debug.call_args[0][1:]
        assert "bit_depth=32" in msg


# ---------------------------------------------------------------
# Constants
# ---------------------------------------------------------------


class TestConstants:
    """Verify PCM param dicts and 24-bit constants."""

    def test_pcm_lossless_keys(self) -> None:
        """Lossless dict has all required keys."""
        assert set(PCM_LOSSLESS_PARAMS.keys()) == {
            "content_type",
            "sample_rate",
            "bit_depth",
            "channels",
        }

    def test_pcm_lossy_keys(self) -> None:
        """Lossy dict has all required keys."""
        assert set(PCM_LOSSY_PARAMS.keys()) == {
            "content_type",
            "sample_rate",
            "bit_depth",
            "channels",
        }

    def test_24bit_constants(self) -> None:
        """24-bit boundary constants are correct."""
        assert _SIGNED_24BIT_MAX == 2**23
        assert _SIGNED_24BIT_RANGE == 2**24
