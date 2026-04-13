"""Tests for provider/prebuffer.py — PreBuffer, run_fill, yield_from_prebuffer."""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

from music_assistant.providers.yandex_ynison.prebuffer import (
    PreBuffer,
    make_prebuffer,
    run_fill,
    yield_from_prebuffer,
)


class TestPreBuffer:
    """Unit tests for the PreBuffer dataclass."""

    def test_make_prebuffer_defaults(self) -> None:
        """make_prebuffer creates PreBuffer with correct defaults."""
        fmt = MagicMock()
        pb = make_prebuffer(track_id="t:1", seek_ms=100, output_format=fmt)
        assert pb.track_id == "t:1"
        assert pb.seek_ms == 100
        assert pb.output_format is fmt
        assert pb.stream_details is None
        assert pb.error is None
        assert pb.task is None
        assert pb.chunks_queued == 0
        assert pb.queue.maxsize == 64

    def test_make_prebuffer_custom_maxsize(self) -> None:
        """make_prebuffer respects custom maxsize."""
        pb = make_prebuffer(track_id="t:2", seek_ms=0, output_format=MagicMock(), maxsize=8)
        assert pb.queue.maxsize == 8

    async def test_cancel_drains_and_sends_eof(self) -> None:
        """cancel() drains queue and puts EOF sentinel."""
        pb = PreBuffer(track_id="t:1", seek_ms=0, output_format=MagicMock())
        # Fill some data
        await pb.queue.put(b"chunk1")
        await pb.queue.put(b"chunk2")

        await pb.cancel()

        # Queue should contain only EOF
        item = pb.queue.get_nowait()
        assert item is None

    async def test_cancel_with_running_task(self) -> None:
        """cancel() cancels a running task before draining."""
        pb = PreBuffer(track_id="t:1", seek_ms=0, output_format=MagicMock())

        async def _slow_fill() -> None:
            await asyncio.sleep(100)

        pb.task = asyncio.get_event_loop().create_task(_slow_fill())
        await pb.cancel()
        assert pb.task.cancelled() or pb.task.done()

    def test_ready_event_initially_unset(self) -> None:
        """PreBuffer.ready is unset on creation."""
        pb = make_prebuffer(track_id="t:1", seek_ms=0, output_format=MagicMock())
        assert not pb.ready.is_set()

    def test_ready_threshold_default(self) -> None:
        """Default ready_threshold is 8."""
        pb = make_prebuffer(track_id="t:1", seek_ms=0, output_format=MagicMock())
        assert pb.ready_threshold == 8

    def test_ready_threshold_custom(self) -> None:
        """make_prebuffer respects custom ready_threshold."""
        pb = make_prebuffer(track_id="t:1", seek_ms=0, output_format=MagicMock(), ready_threshold=3)
        assert pb.ready_threshold == 3


class TestRunFill:
    """Unit tests for the run_fill() function."""

    async def test_happy_path(self) -> None:
        """run_fill populates queue with chunks and EOF sentinel."""
        fmt = MagicMock()
        pb = make_prebuffer(track_id="t:1", seek_ms=0, output_format=fmt)

        sd = MagicMock()
        sd.audio_format = MagicMock()

        get_stream_details = AsyncMock(return_value=sd)

        async def _audio_stream(_sd: object) -> Any:
            yield b"raw"

        pcm_chunks = [b"pcm-a", b"pcm-b"]

        async def _fake_ffmpeg(**_kwargs: object) -> Any:
            for c in pcm_chunks:
                yield c

        logger = MagicMock()

        with patch(
            "music_assistant.providers.yandex_ynison.prebuffer.get_ffmpeg_stream",
            side_effect=_fake_ffmpeg,
        ):
            await run_fill(
                prebuffer=pb,
                get_stream_details=get_stream_details,
                get_audio_stream=_audio_stream,
                output_format=fmt,
                logger=logger,
            )

        assert pb.chunks_queued == 2
        assert pb.stream_details is sd
        assert pb.error is None

        # Queue should have chunks + EOF
        collected = []
        while True:
            item = pb.queue.get_nowait()
            if item is None:
                break
            collected.append(item)
        assert collected == pcm_chunks

    async def test_on_stream_details_callback(self) -> None:
        """on_stream_details callback is invoked with (sd, seek_ms)."""
        fmt = MagicMock()
        pb = make_prebuffer(track_id="t:1", seek_ms=500, output_format=fmt)

        sd = MagicMock()
        sd.audio_format = MagicMock()
        get_stream_details = AsyncMock(return_value=sd)

        async def _audio_stream(_sd: object) -> Any:
            yield b"raw"

        async def _fake_ffmpeg(**_kwargs: object) -> Any:
            yield b"pcm"

        callback = AsyncMock()
        logger = MagicMock()

        with patch(
            "music_assistant.providers.yandex_ynison.prebuffer.get_ffmpeg_stream",
            side_effect=_fake_ffmpeg,
        ):
            await run_fill(
                prebuffer=pb,
                get_stream_details=get_stream_details,
                get_audio_stream=_audio_stream,
                output_format=fmt,
                logger=logger,
                on_stream_details=callback,
            )

        callback.assert_awaited_once_with(sd, 500)

    async def test_seek_adds_ss_arg(self) -> None:
        """run_fill passes -ss argument when seek_ms > 0."""
        fmt = MagicMock()
        pb = make_prebuffer(track_id="t:1", seek_ms=5000, output_format=fmt)

        sd = MagicMock()
        sd.audio_format = MagicMock()
        get_stream_details = AsyncMock(return_value=sd)

        async def _audio_stream(_sd: object) -> Any:
            yield b"raw"

        captured_kwargs: dict[str, Any] = {}

        async def _fake_ffmpeg(**kwargs: object) -> Any:
            captured_kwargs.update(kwargs)
            yield b"pcm"

        logger = MagicMock()

        with patch(
            "music_assistant.providers.yandex_ynison.prebuffer.get_ffmpeg_stream",
            side_effect=_fake_ffmpeg,
        ):
            await run_fill(
                prebuffer=pb,
                get_stream_details=get_stream_details,
                get_audio_stream=_audio_stream,
                output_format=fmt,
                logger=logger,
            )

        extra_args = captured_kwargs.get("extra_input_args", [])
        assert "-ss" in extra_args
        assert "5.000" in extra_args

    async def test_error_sets_prebuffer_error(self) -> None:
        """run_fill captures exceptions in prebuffer.error."""
        fmt = MagicMock()
        pb = make_prebuffer(track_id="t:1", seek_ms=0, output_format=fmt)

        get_stream_details = AsyncMock(side_effect=RuntimeError("boom"))

        async def _audio_stream(_sd: object) -> Any:
            yield b"raw"

        logger = MagicMock()

        await run_fill(
            prebuffer=pb,
            get_stream_details=get_stream_details,
            get_audio_stream=_audio_stream,
            output_format=fmt,
            logger=logger,
        )

        assert isinstance(pb.error, RuntimeError)
        assert "boom" in str(pb.error)
        # EOF sentinel still delivered
        assert pb.queue.get_nowait() is None

    async def test_put_timeout_aborts_fill(self) -> None:
        """run_fill aborts when queue.put times out."""
        fmt = MagicMock()
        # Tiny queue that fills instantly
        pb = make_prebuffer(track_id="t:1", seek_ms=0, output_format=fmt, maxsize=1)

        sd = MagicMock()
        sd.audio_format = MagicMock()
        get_stream_details = AsyncMock(return_value=sd)

        async def _audio_stream(_sd: object) -> Any:
            yield b"raw"

        # Produce many chunks — second one should timeout
        async def _fake_ffmpeg(**_kwargs: object) -> Any:
            for i in range(100):
                yield f"chunk-{i}".encode()

        logger = MagicMock()

        # Pre-fill the queue so it's full
        await pb.queue.put(b"blocking")

        with (
            patch(
                "music_assistant.providers.yandex_ynison.prebuffer.get_ffmpeg_stream",
                side_effect=_fake_ffmpeg,
            ),
            patch("music_assistant.providers.yandex_ynison.prebuffer._QUEUE_PUT_TIMEOUT", 0.05),
        ):
            await run_fill(
                prebuffer=pb,
                get_stream_details=get_stream_details,
                get_audio_stream=_audio_stream,
                output_format=fmt,
                logger=logger,
            )

        assert isinstance(pb.error, TimeoutError)

    async def test_ready_event_fires_after_threshold(self) -> None:
        """run_fill sets ready after ready_threshold chunks are queued."""
        fmt = MagicMock()
        pb = make_prebuffer(track_id="t:1", seek_ms=0, output_format=fmt, ready_threshold=3)

        sd = MagicMock()
        sd.audio_format = MagicMock()
        get_stream_details = AsyncMock(return_value=sd)

        async def _audio_stream(_sd: object) -> Any:
            yield b"raw"

        chunks = [b"c1", b"c2", b"c3", b"c4", b"c5"]

        async def _fake_ffmpeg(**_kwargs: object) -> Any:
            for c in chunks:
                yield c

        logger = MagicMock()

        with patch(
            "music_assistant.providers.yandex_ynison.prebuffer.get_ffmpeg_stream",
            side_effect=_fake_ffmpeg,
        ):
            await run_fill(
                prebuffer=pb,
                get_stream_details=get_stream_details,
                get_audio_stream=_audio_stream,
                output_format=fmt,
                logger=logger,
            )

        assert pb.ready.is_set()
        assert pb.chunks_queued == 5

    async def test_ready_event_fires_on_short_track(self) -> None:
        """run_fill sets ready even if fewer chunks than threshold (via finally)."""
        fmt = MagicMock()
        pb = make_prebuffer(track_id="t:1", seek_ms=0, output_format=fmt, ready_threshold=10)

        sd = MagicMock()
        sd.audio_format = MagicMock()
        get_stream_details = AsyncMock(return_value=sd)

        async def _audio_stream(_sd: object) -> Any:
            yield b"raw"

        async def _fake_ffmpeg(**_kwargs: object) -> Any:
            yield b"c1"
            yield b"c2"

        logger = MagicMock()

        with patch(
            "music_assistant.providers.yandex_ynison.prebuffer.get_ffmpeg_stream",
            side_effect=_fake_ffmpeg,
        ):
            await run_fill(
                prebuffer=pb,
                get_stream_details=get_stream_details,
                get_audio_stream=_audio_stream,
                output_format=fmt,
                logger=logger,
            )

        # Only 2 chunks < threshold of 10, but ready should still be set
        assert pb.ready.is_set()
        assert pb.chunks_queued == 2

    async def test_ready_event_fires_on_error(self) -> None:
        """run_fill sets ready even when an error occurs (prevents deadlock)."""
        fmt = MagicMock()
        pb = make_prebuffer(track_id="t:1", seek_ms=0, output_format=fmt, ready_threshold=10)

        get_stream_details = AsyncMock(side_effect=RuntimeError("fail"))

        async def _audio_stream(_sd: object) -> Any:
            yield b"raw"

        logger = MagicMock()

        await run_fill(
            prebuffer=pb,
            get_stream_details=get_stream_details,
            get_audio_stream=_audio_stream,
            output_format=fmt,
            logger=logger,
        )

        assert pb.ready.is_set()
        assert pb.error is not None


class TestYieldFromPrebuffer:
    """Unit tests for yield_from_prebuffer()."""

    async def test_yields_until_eof(self) -> None:
        """yield_from_prebuffer yields chunks until None sentinel."""
        pb = PreBuffer(track_id="t:1", seek_ms=0, output_format=MagicMock())
        pb.ready.set()
        await pb.queue.put(b"a")
        await pb.queue.put(b"b")
        await pb.queue.put(None)

        collected = []
        async for chunk in yield_from_prebuffer(pb):
            collected.append(chunk)

        assert collected == [b"a", b"b"]

    async def test_empty_stream(self) -> None:
        """yield_from_prebuffer handles immediate EOF."""
        pb = PreBuffer(track_id="t:1", seek_ms=0, output_format=MagicMock())
        pb.ready.set()
        await pb.queue.put(None)

        collected = []
        async for chunk in yield_from_prebuffer(pb):
            collected.append(chunk)

        assert collected == []

    async def test_waits_for_ready_event(self) -> None:
        """yield_from_prebuffer blocks until ready event is set."""
        pb = PreBuffer(track_id="t:1", seek_ms=0, output_format=MagicMock())
        await pb.queue.put(b"data")
        await pb.queue.put(None)

        collected: list[bytes] = []
        started = asyncio.Event()

        async def _consume() -> None:
            started.set()
            async for chunk in yield_from_prebuffer(pb):
                collected.append(chunk)

        task = asyncio.create_task(_consume())
        await started.wait()
        # Give consumer a chance to run — should be blocked on ready
        await asyncio.sleep(0.05)
        assert collected == [], "Should not yield before ready"

        pb.ready.set()
        await task
        assert collected == [b"data"]
