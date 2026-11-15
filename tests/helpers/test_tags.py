"""Tests for parsing audio file tags (ID3, MP4/AAC, Vorbis, APEv2, etc.)."""

import asyncio
import json
import pathlib
import shutil
import subprocess
import sys
from contextlib import suppress
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import mutagen
import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer
from music_assistant_models.errors import InvalidDataError
from mutagen.id3 import ID3, UFID
from PIL import Image

# mutagen 1.47 does not re-export UFID explicitly (fixed in 1.48, which dev pins)
from mutagen.id3 import ID3, UFID  # type: ignore[attr-defined]

from music_assistant.constants import UNKNOWN_ARTIST
from music_assistant.helpers import tags
from music_assistant.helpers.tags import split_artists

RESOURCES_DIR = pathlib.Path(__file__).parent.parent.resolve().joinpath("fixtures")

FILE_MP3 = str(RESOURCES_DIR.joinpath("MyArtist - MyTitle.mp3"))
FILE_MP3_ID3V24_MULTIVALUE = str(RESOURCES_DIR.joinpath("MultiArtist-ID3v24-NullSeparated.mp3"))
FILE_M4A = str(RESOURCES_DIR.joinpath("MyArtist - MyTitle.m4a"))
FILE_FLAC = str(RESOURCES_DIR.joinpath("MultipleArtists.flac"))
FILE_FLAC_SEMICOLON = str(RESOURCES_DIR.joinpath("ArtistWithSemicolon.flac"))
FILE_WV = str(RESOURCES_DIR.joinpath("MyArtist - MyTitle.wv"))


@pytest.fixture
def probe_process(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    """Provide a probe whose output and exit status can be controlled."""
    process = MagicMock(returncode=0)
    process.communicate = AsyncMock(return_value=(b'{"format": {}, "streams": []}', b""))
    process.__aenter__ = AsyncMock(return_value=process)
    process.__aexit__ = AsyncMock(return_value=None)
    factory = MagicMock(return_value=process)
    monkeypatch.setattr(tags, "AsyncProcess", factory)
    return factory


@pytest.mark.parametrize(
    ("stderr", "expected_detail"),
    [
        (
            b"[Vorbis parser @ 0x123] Invalid Setup header\n"
            b"[ogg @ 0x456] Header processing failed: Unknown error occurred\n",
            "Invalid Setup header",
        ),
        (b"broken.ogg: Unknown error occurred\n", "Invalid or unsupported media file"),
    ],
)
async def test_parse_tags_reports_actionable_ffprobe_error(
    probe_process: MagicMock, stderr: bytes, expected_detail: str
) -> None:
    """Test that tag parsing reports a useful FFprobe failure."""
    process = probe_process.return_value
    process.returncode = 1
    process.communicate.return_value = (
        b'{"error":{"code":-1,"string":"Unknown error occurred"}}',
        stderr,
    )

    with pytest.raises(InvalidDataError) as err:
        await tags.async_parse_tags("broken.ogg")

    assert str(err.value) == f"Unable to retrieve info for broken.ogg ({expected_detail})"
    assert probe_process.call_args.kwargs["stderr"] is True
    args = probe_process.call_args.args[0]
    assert args[args.index("-loglevel") + 1] == "error"


async def test_parse_tags_gives_up_on_a_hanging_probe(probe_process: MagicMock) -> None:
    """A probe that never finishes is bounded by a timeout and reported as invalid data."""
    process = probe_process.return_value
    process.communicate.side_effect = TimeoutError

    with pytest.raises(InvalidDataError) as err:
        await tags.async_parse_tags("http://radio.example.com/stream")

    assert "no result within" in str(err.value)
    process.communicate.assert_awaited_once_with(timeout=tags.FFPROBE_TIMEOUT)


async def test_parse_tags_bounds_network_reads_on_urls(probe_process: MagicMock) -> None:
    """Tag parsing of a URL sets a network read timeout, of a local file none."""
    with pytest.raises(InvalidDataError):
        await tags.async_parse_tags("https://example.com/a.mp3")
    args = probe_process.call_args.args[0]
    assert args[args.index("-rw_timeout") + 1] == "10000000"
    assert args.index("-rw_timeout") < args.index("-i")

    with pytest.raises(InvalidDataError):
        await tags.async_parse_tags("/music/a.mp3")
    assert "-rw_timeout" not in probe_process.call_args.args[0]


async def test_async_parse_tags_decodes_missing_duration(monkeypatch: pytest.MonkeyPatch) -> None:
    """A missing duration can be obtained from a cancellable full decode."""
    raw = {
        "format": {"filename": "https://example.com/audio", "format_name": "mp3"},
        "streams": [{"codec_type": "audio", "channels": 2, "sample_rate": "44100"}],
    }
    processes = []
    for output in (json.dumps(raw).encode(), b"time=00:03:21.25 bitrate="):
        process = MagicMock(returncode=0)
        process.communicate = AsyncMock(return_value=(output, b""))
        process.__aenter__ = AsyncMock(return_value=process)
        process.__aexit__ = AsyncMock(return_value=None)
        processes.append(process)
    factory = MagicMock(side_effect=processes)
    monkeypatch.setattr(tags, "AsyncProcess", factory)

    result = await tags.async_parse_tags("https://example.com/audio", require_duration=True)

    assert result.duration == 201.25
    decoder_args = factory.call_args.args[0]
    assert decoder_args[0] == "ffmpeg"
    assert decoder_args[decoder_args.index("-rw_timeout") + 1] == "10000000"
    assert decoder_args.index("-rw_timeout") < decoder_args.index("-i")
    processes[1].communicate.assert_awaited_once_with(timeout=tags.FFMPEG_DURATION_TIMEOUT)


@pytest.mark.parametrize("command", ["ffprobe", "ffmpeg"])
@pytest.mark.parametrize("cancel", [True, False])
async def test_async_parse_tags_reaps_hanging_process(
    monkeypatch: pytest.MonkeyPatch, command: str, cancel: bool
) -> None:
    """Cancellation and deadlines reap both probe and duration children."""
    started = asyncio.Event()
    children: list[asyncio.subprocess.Process] = []
    create_subprocess = asyncio.create_subprocess_exec
    raw = {
        "format": {"filename": FILE_MP3, "format_name": "mp3"},
        "streams": [{"codec_type": "audio", "channels": 2, "sample_rate": "44100"}],
    }

    async def start_process(*args: str, **kwargs: Any) -> asyncio.subprocess.Process:
        hanging = args[0] == command
        code = "import time; time.sleep(60)" if hanging else f"print({json.dumps(raw)!r})"
        child = await create_subprocess(sys.executable, "-c", code, **kwargs)
        children.append(child)
        if hanging:
            started.set()
        return child

    monkeypatch.setattr(asyncio, "create_subprocess_exec", start_process)
    if not cancel:
        timeout_key = "FFPROBE_TIMEOUT" if command == "ffprobe" else "FFMPEG_DURATION_TIMEOUT"
        monkeypatch.setattr(tags, timeout_key, 0.1)
    task = asyncio.create_task(tags.async_parse_tags("provider-file.mp3", require_duration=True))
    try:
        await asyncio.wait_for(started.wait(), 5)
        if cancel:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            with pytest.raises(
                InvalidDataError, match=r"no result within|Unable to retrieve duration"
            ):
                await task
        assert children
        assert all(child.returncode is not None for child in children)
    finally:
        if not task.done():
            task.cancel()
            with suppress(asyncio.CancelledError, InvalidDataError):
                await task


def test_parse_rejects_file_without_audio_channels() -> None:
    """A file for which ffprobe reports zero channels is corrupt and must be skipped."""
    raw = {
        "format": {"filename": "corrupt.mp3", "format_name": "mp3", "duration": "0"},
        "streams": [{"codec_type": "audio", "channels": 0, "sample_rate": "44100"}],
    }
    with pytest.raises(InvalidDataError, match="No audio channels found"):
        tags.AudioTags.parse(raw)


async def test_parse_tags_reads_a_probe_that_finished_before_it_was_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A probe that is done before its output is collected still yields the tags."""
    start = AsyncProcess.start

    async def start_and_finish(self: AsyncProcess) -> None:
        """Start the process and let it run to its end, as a busy event loop does."""
        await start(self)
        await self.wait()

    monkeypatch.setattr(AsyncProcess, "start", start_and_finish)

    _tags = await tags.async_parse_tags(FILE_MP3)

    assert _tags.title == "MyTitle"


async def test_parse_metadata_from_id3tags() -> None:
    """Test parsing of parsing metadata from ID3 tags."""
    filename = str(RESOURCES_DIR.joinpath("MyArtist - MyTitle.mp3"))
    _tags = await tags.async_parse_tags(filename)
    assert _tags.album == "MyAlbum"
    assert _tags.title == "MyTitle"
    assert _tags.duration == 1.032
    assert _tags.album_artists == ("MyArtist",)
    assert _tags.artists == ("MyArtist", "MyArtist2")
    assert _tags.genres == ("Genre1", "Genre2")
    assert _tags.musicbrainz_albumartistids == ("abcdefg",)
    assert _tags.musicbrainz_artistids == ("abcdefg",)
    assert _tags.musicbrainz_releasegroupid == "abcdefg"
    assert _tags.musicbrainz_recordingid == "abcdefg"
    assert _tags.synchronized_lyrics == [
        ("My synchronized lyrics start here", 0),
        ("continue on this line", 671110),
        ("and end here.", 5999999),
    ]
    # test parsing disc/track number
    _tags.tags["disc"] = ""
    assert _tags.disc is None
    _tags.tags["disc"] = "1"
    assert _tags.disc == 1
    _tags.tags["disc"] = "1/1"  # type: ignore[unreachable]
    assert _tags.disc == 1
    # test parsing album year
    _tags.tags["date"] = "blah"
    assert _tags.year is None
    _tags.tags.pop("date", None)
    assert _tags.year is None
    _tags.tags["date"] = "2022"
    assert _tags.year == 2022
    _tags.tags["date"] = "2022-05-05"
    assert _tags.year == 2022
    _tags.tags["date"] = ""
    assert _tags.year is None


async def test_parse_id3v24_null_separated_artists() -> None:
    """Test parsing ID3v2.4 tags with null-separated multi-value TPE1/TPE2."""
    _tags = await tags.async_parse_tags(FILE_MP3_ID3V24_MULTIVALUE)
    # Null-separated artists in TPE1 should be parsed as multiple artists
    assert _tags.artists == ("Artist One", "Artist Two", "Artist Three")
    # Null-separated album artists in TPE2 should be parsed as multiple album artists
    assert _tags.album_artists == ("Album Artist A", "Album Artist B")
    # MB IDs should match
    assert _tags.musicbrainz_artistids == ("mb-artist-1", "mb-artist-2", "mb-artist-3")
    assert _tags.musicbrainz_albumartistids == ("mb-albumartist-1", "mb-albumartist-2")


async def test_parse_metadata_from_mp4tags() -> None:
    """Test parsing of metadata from MP4/AAC tags."""
    filename = FILE_M4A
    _tags = await tags.async_parse_tags(filename)
    assert _tags.album == "MyAlbum"
    assert _tags.title == "MyTitle"
    assert _tags.album_artists == ("MyArtist",)
    assert _tags.artists == ("MyArtist", "MyArtist2")
    assert _tags.genres == ("Genre1", "Genre2")
    assert _tags.musicbrainz_albumartistids == ("abcdefg",)
    assert _tags.musicbrainz_artistids == ("abcdefg",)
    assert _tags.musicbrainz_releasegroupid == "abcdefg"
    assert _tags.musicbrainz_recordingid == "abcdefg"
    # test track/disc from MP4 tuples
    assert _tags.track == 5
    assert _tags.disc == 1
    # test total track/disc
    assert _tags.tags.get("tracktotal") == "12"
    assert _tags.tags.get("disctotal") == "2"
    # test year
    assert _tags.year == 2022
    # test sort tags (artistsort/albumartistsort returned as lists to match ID3 behavior)
    assert _tags.tags.get("titlesort") == "MyTitle Sort"
    assert _tags.tags.get("artistsort") == ["MyArtist Sort"]
    assert _tags.tags.get("albumsort") == "MyAlbum Sort"
    assert _tags.tags.get("albumartistsort") == ["MyAlbumArtist Sort"]


def test_parse_metadata_from_apev2tags() -> None:
    """
    Test parsing of metadata from APEv2 tags (WavPack).

    Uses parse_tags_mutagen directly since the minimal WavPack fixture
    does not contain valid audio data for ffprobe to parse.
    """
    result = parse_tags_mutagen(FILE_WV)
    assert result.get("album") == "MyAlbum"
    assert result.get("title") == "MyTitle"
    assert result.get("albumartist") == "MyArtist"
    assert result.get("artist") == "MyArtist"
    assert result.get("artists") == ["MyArtist", "MyArtist2"]
    assert result.get("genre") == ["Genre1", "Genre2"]
    assert result.get("musicbrainzalbumartistid") == ["abcdefg"]
    assert result.get("musicbrainzartistid") == ["abcdefg"]
    assert result.get("musicbrainzreleasegroupid") == "abcdefg"
    assert result.get("musicbrainzrecordingid") == "abcdefg"
    # test track/disc (APEv2 uses "5/12" format like ID3)
    assert result.get("track") == "5/12"
    assert result.get("disc") == "1/2"
    # test year
    assert result.get("date") == "2022"
    # test sort tags (artistsort/albumartistsort returned as lists to match ID3 behavior)
    assert result.get("titlesort") == "MyTitle Sort"
    assert result.get("artistsort") == ["MyArtist Sort"]
    assert result.get("albumsort") == "MyAlbum Sort"
    assert result.get("albumartistsort") == ["MyAlbumArtist Sort"]


def test_id3_musicbrainz_ufid_strips_trailing_null() -> None:
    """
    Strip a trailing NUL terminator from the MusicBrainz UFID frame data.

    Some taggers (e.g. Picard) append a NUL to the UFID data; without stripping
    it the recording MBID is malformed and breaks import and MusicBrainz
    lookups.

    See https://github.com/music-assistant/support/issues/5906
    """
    ufid = UFID(  # type: ignore[no-untyped-call]
        owner="http://musicbrainz.org",
        data=b"1e74cd4c-cfa7-4bdb-99da-41869f5f1171\x00",
    )

    mock_tags = MagicMock()
    mock_tags.get = lambda key: ufid if key == "UFID:http://musicbrainz.org" else None
    result = _parse_id3_tags(mock_tags)

    assert result["musicbrainzrecordingid"] == "1e74cd4c-cfa7-4bdb-99da-41869f5f1171"


async def test_parse_metadata_from_flac_with_multiple_artist_fields() -> None:
    """Test parsing of FLAC file with multiple ARTIST fields (per Vorbis spec)."""
    _tags = await tags.async_parse_tags(FILE_FLAC)
    assert _tags.album == "Test Album"
    assert _tags.title == "Test Track"
    # Multiple ARTIST fields should be treated as authoritative list
    assert _tags.artists == ("Artist One", "Artist Two", "Artist Three")
    # Multiple ALBUMARTIST fields should be treated as authoritative list
    assert _tags.album_artists == ("Album Artist 1", "Album Artist 2")
    assert _tags.genres == ("Rock", "Pop")
    assert _tags.year == 2024
    # MusicBrainz IDs
    assert _tags.musicbrainz_artistids == ("mb-artist-id-1", "mb-artist-id-2", "mb-artist-id-3")
    assert _tags.musicbrainz_albumartistids == ("mb-albumartist-id-1", "mb-albumartist-id-2")
    assert _tags.musicbrainz_recordingid == "mb-track-id"
    # Track/disc from Vorbis comments
    assert _tags.track == 5
    assert _tags.disc == 1


async def test_parse_metadata_from_filename() -> None:
    """Test parsing of parsing metadata from filename."""
    filename = str(RESOURCES_DIR.joinpath("MyArtist - MyTitle without Tags.mp3"))
    _tags = await tags.async_parse_tags(filename)
    assert _tags.album is None
    assert _tags.title == "MyTitle without Tags"
    assert _tags.duration == 1.032
    assert _tags.album_artists == ()
    assert _tags.artists == ("MyArtist",)
    assert _tags.genres == ()
    assert _tags.musicbrainz_albumartistids == ()
    assert _tags.musicbrainz_artistids == ()
    assert _tags.musicbrainz_releasegroupid is None
    assert _tags.musicbrainz_recordingid is None


async def test_parse_metadata_from_invalid_filename() -> None:
    """Test parsing of parsing metadata from (invalid) filename."""
    filename = str(RESOURCES_DIR.joinpath("test.mp3"))
    _tags = await tags.async_parse_tags(filename)
    assert _tags.album is None
    assert _tags.title == "test"
    assert _tags.duration == 1.032
    assert _tags.album_artists == ()
    assert _tags.artists == (UNKNOWN_ARTIST,)
    assert _tags.genres == ()
    assert _tags.musicbrainz_albumartistids == ()
    assert _tags.musicbrainz_artistids == ()
    assert _tags.musicbrainz_releasegroupid is None
    assert _tags.musicbrainz_recordingid is None


def test_split_artists_with_expected_count() -> None:
    """Test splitting artists guided by expected count (from MB IDs)."""
    # With expected_count=3, should split on extra splitters to reach target
    result = split_artists("Shabson, Krgovich & Harris", expected_count=3)
    assert result == ("Shabson", "Krgovich", "Harris")

    # With expected_count=3, ampersands should split
    result = split_artists("Shabson & Krgovich & Harris", expected_count=3)
    assert result == ("Shabson", "Krgovich", "Harris")

    # With expected_count=3, commas should split
    result = split_artists("Shabson, Krgovich, Harris", expected_count=3)
    assert result == ("Shabson", "Krgovich", "Harris")

    # With expected_count=1, should NOT split at all
    result = split_artists("Shabson & Krgovich", expected_count=1)
    assert result == ("Shabson & Krgovich",)

    # With expected_count=None (no MB IDs), should NOT split on extra splitters
    result = split_artists("Shabson & Krgovich", expected_count=None)
    assert result == ("Shabson & Krgovich",)

    # With expected_count=0 (no MB IDs), should NOT split on extra splitters
    result = split_artists("Shabson & Krgovich", expected_count=0)
    assert result == ("Shabson & Krgovich",)


def test_split_artists_featuring() -> None:
    """Test that featuring splitters always work regardless of expected_count."""
    # "feat." should always split, even with no expected_count
    result = split_artists("John Lennon feat. Yoko Ono", expected_count=None)
    assert result == ("John Lennon", "Yoko Ono")

    # "feat." should split even with expected_count=1 (featuring overrides)
    # Actually, expected_count=1 means single artist, so we return as-is
    result = split_artists("John Lennon feat. Yoko Ono", expected_count=1)
    assert result == ("John Lennon feat. Yoko Ono",)

    # "featuring" should work
    result = split_artists("Artist A featuring Artist B", expected_count=None)
    assert result == ("Artist A", "Artist B")

    # "ft." should work
    result = split_artists("Artist A ft. Artist B", expected_count=None)
    assert result == ("Artist A", "Artist B")

    # " presents " should split without disturbing an ampersand inside an artist name
    result = split_artists("Above & Beyond presents OceanLab", expected_count=None)
    assert result == ("Above & Beyond", "OceanLab")


def test_split_artists_no_oversplit() -> None:
    """Test that split_artists stops at expected_count and doesn't over-split."""
    # Hall & Oates is a duo, with 2 MB IDs we should split on feat. first
    # and get exactly 2 artists
    result = split_artists("Hall & Oates feat. David Ruffin", expected_count=2)
    assert result == ("Hall & Oates", "David Ruffin")

    # With 3 MB IDs, we should split further
    result = split_artists("Hall & Oates feat. David Ruffin", expected_count=3)
    assert result == ("Hall", "Oates", "David Ruffin")

    # Simon & Garfunkel with 1 MB ID (the duo) should stay as one
    result = split_artists("Simon & Garfunkel", expected_count=1)
    assert result == ("Simon & Garfunkel",)

    # Simon & Garfunkel with 2 MB IDs (Paul + Art) should split
    result = split_artists("Simon & Garfunkel", expected_count=2)
    assert result == ("Simon", "Garfunkel")


def test_split_artists_with_not_split() -> None:
    """Test that 'with' is only split when we have MB ID evidence."""
    # "with" should NOT split without expected_count (could be artist name)
    result = split_artists("Jerk With a Bomb", expected_count=None)
    assert result == ("Jerk With a Bomb",)

    # "with" should NOT split with expected_count=1
    result = split_artists("Jerk With a Bomb", expected_count=1)
    assert result == ("Jerk With a Bomb",)

    # "with" SHOULD split when expected_count=2 indicates multiple artists
    result = split_artists("Artist A with Artist B", expected_count=2)
    assert result == ("Artist A", "Artist B")
