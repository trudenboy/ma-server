"""Tests for provider/crossfade.py — TailBuffer, collect_crossfade_head, apply_crossfade."""

from __future__ import annotations

import logging
from collections.abc import AsyncGenerator
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from music_assistant.providers.yandex_ynison.crossfade import (
    CrossfadeNoiseError,
    TailBuffer,
    apply_crossfade,
    collect_crossfade_head,
    crossfade_bytes_for,
)
from music_assistant.providers.yandex_ynison.prebuffer import PreBuffer


def _make_pcm_format(sample_rate: int = 48000, bit_depth: int = 24, channels: int = 2) -> MagicMock:
    """Create a mock AudioFormat with pcm_sample_size computed correctly."""
    fmt = MagicMock()
    fmt.sample_rate = sample_rate
    fmt.bit_depth = bit_depth
    fmt.channels = channels
    fmt.pcm_sample_size = sample_rate * (bit_depth // 8) * channels
    fmt.content_type = MagicMock()
    fmt.content_type.name = "PCM_S24LE"
    fmt.content_type.value = "s24le"
    return fmt


class TestTailBuffer:
    """Unit tests for TailBuffer."""

    def test_zero_capacity_raises(self) -> None:
        """TailBuffer raises ValueError on zero or negative capacity."""
        with pytest.raises(ValueError, match="must be > 0"):
            TailBuffer(0)
        with pytest.raises(ValueError, match="must be > 0"):
            TailBuffer(-1)

    def test_push_within_capacity(self) -> None:
        """push() returns empty bytes when buffer has room."""
        tb = TailBuffer(10)
        overflow = tb.push(b"hello")
        assert overflow == b""
        assert len(tb) == 5

    def test_push_overflow(self) -> None:
        """push() returns overflow bytes when buffer exceeds capacity."""
        tb = TailBuffer(6)
        overflow = tb.push(b"abcdefghij")  # 10 bytes, capacity 6
        assert overflow == b"abcd"  # first 4 bytes overflow
        assert len(tb) == 6

    def test_push_multiple_chunks(self) -> None:
        """Multiple pushes accumulate correctly with overflow."""
        tb = TailBuffer(5)
        o1 = tb.push(b"abc")
        assert o1 == b""
        o2 = tb.push(b"defgh")  # total 8 bytes, capacity 5
        assert o2 == b"abc"
        assert len(tb) == 5

    def test_flush_returns_tail(self) -> None:
        """flush() returns the buffered tail and resets."""
        tb = TailBuffer(10)
        tb.push(b"hello")
        tb.push(b"world")
        tail = tb.flush()
        assert tail == b"helloworld"
        assert len(tb) == 0

    def test_flush_empty(self) -> None:
        """flush() returns empty bytes on fresh buffer."""
        tb = TailBuffer(10)
        assert tb.flush() == b""

    def test_push_exact_capacity(self) -> None:
        """push() with exactly capacity bytes yields no overflow."""
        tb = TailBuffer(5)
        overflow = tb.push(b"abcde")
        assert overflow == b""
        assert len(tb) == 5

    def test_sequential_overflow(self) -> None:
        """Sequential pushes each produce correct overflow."""
        tb = TailBuffer(3)
        assert tb.push(b"abc") == b""
        assert tb.push(b"d") == b"a"
        assert tb.push(b"ef") == b"bc"
        assert tb.flush() == b"def"

    def test_capacity_property(self) -> None:
        """Capacity property returns the configured capacity."""
        tb = TailBuffer(42)
        assert tb.capacity == 42


class TestCrossfadeBytesFor:
    """Tests for crossfade_bytes_for()."""

    def test_basic_calculation(self) -> None:
        """5 seconds at 48kHz/24bit/stereo = 1,440,000 bytes."""
        fmt = _make_pcm_format()
        result = crossfade_bytes_for(5.0, fmt)
        frame_size = 3 * 2  # 24bit=3bytes * 2channels
        expected = int(fmt.pcm_sample_size * 5.0)
        expected = (expected // frame_size) * frame_size
        assert result == expected
        assert result == 1_440_000

    def test_frame_aligned(self) -> None:
        """Result is always frame-aligned."""
        fmt = _make_pcm_format()
        frame_size = (fmt.bit_depth // 8) * fmt.channels
        for dur in [0.1, 1.0, 3.7, 10.0]:
            result = crossfade_bytes_for(dur, fmt)
            assert result % frame_size == 0

    def test_zero_duration(self) -> None:
        """Zero duration returns zero bytes."""
        fmt = _make_pcm_format()
        assert crossfade_bytes_for(0.0, fmt) == 0


class TestCollectCrossfadeHead:
    """Tests for collect_crossfade_head()."""

    async def test_collects_target_bytes(self) -> None:
        """Collects exactly target_bytes from prebuffer queue."""
        pb = PreBuffer(track_id="t:1", seek_ms=0, output_format=MagicMock())
        pb.ready.set()
        await pb.queue.put(b"aaaa")  # 4 bytes
        await pb.queue.put(b"bbbb")  # 4 bytes
        await pb.queue.put(b"cccc")  # 4 bytes
        await pb.queue.put(None)

        data, _eof = await collect_crossfade_head(pb, target_bytes=8)
        assert len(data) >= 8
        assert data[:8] == b"aaaabbbb"

    async def test_eof_before_target(self) -> None:
        """Returns partial data and eof=True if stream ends early."""
        pb = PreBuffer(track_id="t:1", seek_ms=0, output_format=MagicMock())
        await pb.queue.put(b"short")
        await pb.queue.put(None)

        data, eof = await collect_crossfade_head(pb, target_bytes=100)
        assert data == b"short"
        assert eof is True

    async def test_timeout(self) -> None:
        """Returns partial data if timeout fires before enough bytes."""
        pb = PreBuffer(track_id="t:1", seek_ms=0, output_format=MagicMock())
        await pb.queue.put(b"partial")
        # No EOF, no more data — will timeout

        data, eof = await collect_crossfade_head(pb, target_bytes=100, timeout=0.1)
        assert data == b"partial"
        assert eof is False

    async def test_empty_prebuffer(self) -> None:
        """Returns empty bytes and eof if prebuffer has only EOF."""
        pb = PreBuffer(track_id="t:1", seek_ms=0, output_format=MagicMock())
        await pb.queue.put(None)

        data, eof = await collect_crossfade_head(pb, target_bytes=100)
        assert data == b""
        assert eof is True


class TestApplyCrossfade:
    """Tests for apply_crossfade()."""

    async def test_delegates_to_standard_crossfade(self) -> None:
        """apply_crossfade calls StandardCrossFade.apply with processed inputs."""
        fmt = _make_pcm_format()
        logger = logging.getLogger("test")

        fade_out = b"\x00" * 1000
        fade_in = b"\xff" * 1000

        mock_apply_results = [b"mixed1", b"mixed2"]

        async def _mock_apply(_self: Any, _fo: bytes, _fi: Any, _pf: Any) -> Any:
            for chunk in mock_apply_results:
                yield chunk

        with (
            patch(
                "music_assistant.providers.yandex_ynison.crossfade.strip_silence",
                new_callable=AsyncMock,
                return_value=fade_out,
            ),
            patch(
                "music_assistant.providers.yandex_ynison.crossfade.align_audio_to_frame_boundary",
                return_value=fade_out,
            ),
            patch(
                "music_assistant.controllers.streams.smart_fades.fades.StandardCrossFade.apply",
                _mock_apply,
            ),
            patch(
                "music_assistant.providers.yandex_ynison.crossfade.compute_rms_pct",
                return_value=0.0,
            ),
        ):
            collected: list[bytes] = []
            async for chunk in apply_crossfade(fade_out, fade_in, fmt, 5.0, logger):
                collected.append(chunk)

        assert collected == [b"mixed1", b"mixed2"]

    async def test_empty_fade_out_after_strip(self) -> None:
        """If fade_out is empty after strip_silence, yields fade_in as-is."""
        fmt = _make_pcm_format()
        logger = logging.getLogger("test")

        with (
            patch(
                "music_assistant.providers.yandex_ynison.crossfade.strip_silence",
                new_callable=AsyncMock,
                return_value=b"",
            ),
            patch(
                "music_assistant.providers.yandex_ynison.crossfade.align_audio_to_frame_boundary",
                return_value=b"",
            ),
        ):
            collected: list[bytes] = []
            async for chunk in apply_crossfade(b"tail", b"head_data", fmt, 5.0, logger):
                collected.append(chunk)

        assert collected == [b"head_data"]

    async def test_empty_fade_out_with_async_gen_fade_in(self) -> None:
        """If fade_out empty, async generator fade_in is consumed correctly."""
        fmt = _make_pcm_format()
        logger = logging.getLogger("test")

        async def _fade_in_gen() -> AsyncGenerator[bytes, None]:
            yield b"part1"
            yield b"part2"

        with (
            patch(
                "music_assistant.providers.yandex_ynison.crossfade.strip_silence",
                new_callable=AsyncMock,
                return_value=b"",
            ),
            patch(
                "music_assistant.providers.yandex_ynison.crossfade.align_audio_to_frame_boundary",
                return_value=b"",
            ),
        ):
            collected: list[bytes] = []
            async for chunk in apply_crossfade(b"tail", _fade_in_gen(), fmt, 5.0, logger):
                collected.append(chunk)

        assert collected == [b"part1", b"part2"]

    async def test_strip_silence_and_align_called(self) -> None:
        """apply_crossfade preprocesses fade_out with strip_silence + align."""
        fmt = _make_pcm_format()
        logger = logging.getLogger("test")

        mock_strip = AsyncMock(return_value=b"stripped")
        mock_align = MagicMock(return_value=b"aligned")

        async def _mock_apply(_self: Any, fade_out: bytes, _fi: Any, _pf: Any) -> Any:
            assert fade_out == b"aligned", "fade_out should be aligned"
            yield b"out"

        with (
            patch("music_assistant.providers.yandex_ynison.crossfade.strip_silence", mock_strip),
            patch(
                "music_assistant.providers.yandex_ynison.crossfade.align_audio_to_frame_boundary",
                mock_align,
            ),
            patch(
                "music_assistant.controllers.streams.smart_fades.fades.StandardCrossFade.apply",
                _mock_apply,
            ),
            patch(
                "music_assistant.providers.yandex_ynison.crossfade.compute_rms_pct",
                return_value=0.0,
            ),
        ):
            collected: list[bytes] = []
            async for chunk in apply_crossfade(b"raw_tail", b"head", fmt, 5.0, logger):
                collected.append(chunk)

        mock_strip.assert_awaited_once_with(b"raw_tail", pcm_format=fmt, reverse=True)
        mock_align.assert_called_once_with(b"stripped", fmt)
        assert collected == [b"out"]

    async def test_noise_detection_aborts_crossfade(self) -> None:
        """If crossfade output has high RMS (noise), CrossfadeNoiseError is raised."""
        fmt = _make_pcm_format()
        logger = logging.getLogger("test")

        fade_out = b"\x00" * 100
        fade_in = b"\x00" * 100

        async def _mock_apply(_self: Any, _fo: bytes, _fi: Any, _pf: Any) -> Any:
            yield b"noisy_chunk"
            yield b"more_noise"

        with (
            patch(
                "music_assistant.providers.yandex_ynison.crossfade.strip_silence",
                new_callable=AsyncMock,
                return_value=fade_out,
            ),
            patch(
                "music_assistant.providers.yandex_ynison.crossfade.align_audio_to_frame_boundary",
                return_value=fade_out,
            ),
            patch(
                "music_assistant.controllers.streams.smart_fades.fades.StandardCrossFade.apply",
                _mock_apply,
            ),
            patch(
                "music_assistant.providers.yandex_ynison.crossfade.compute_rms_pct",
                return_value=60.0,
            ),
            pytest.raises(CrossfadeNoiseError, match=r"RMS=60\.0%"),
        ):
            async for _ in apply_crossfade(fade_out, fade_in, fmt, 5.0, logger):
                pass
