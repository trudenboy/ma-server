"""Test configuration and fixtures."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock

import pytest

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ProviderConfig
    from music_assistant_models.provider import ProviderManifest


@pytest.fixture
def mock_provider_config() -> ProviderConfig:
    """Create a mock provider configuration."""
    config = MagicMock(spec=ProviderConfig)
    config.values = {
        "token": "test_token",
        "user_id": "test_user_id",
        "quality": "high",
        "base_url": "http://localhost:3000",
    }
    return config


@pytest.fixture
def mock_provider_manifest() -> ProviderManifest:
    """Create a mock provider manifest."""
    manifest = MagicMock(spec=ProviderManifest)
    manifest.domain = "ncloud_music"
    manifest.name = "NetEase Cloud Music"
    return manifest


@pytest.fixture
def sample_user_account() -> dict:
    """Load sample user account fixture."""
    return {
        "account": {
            "id": 12345678,
            "userName": "testuser",
            "nickname": "Test User",
            "avatarUrl": "https://p1.music.126.net/avatar.jpg",
        },
        "profile": {
            "userId": 12345678,
            "nickname": "Test User",
            "avatarUrl": "https://p1.music.126.net/avatar.jpg",
            "followeds": 100,
            "follows": 200,
            "playlistCount": 10,
        },
    }


@pytest.fixture
def sample_playlist() -> dict:
    """Load sample playlist fixture."""
    return {
        "id": 123456789,
        "name": "Test Playlist",
        "coverImgUrl": "https://p1.music.126.net/cover.jpg",
        "creator": {
            "userId": 12345678,
            "nickname": "Test User",
        },
        "trackCount": 50,
        "playCount": 1000,
        "trackIds": [{"id": 1001}, {"id": 1002}, {"id": 1003}],
    }


@pytest.fixture
def sample_track() -> dict:
    """Load sample track fixture."""
    return {
        "id": 1001,
        "name": "Test Track",
        "duration": 240000,  # milliseconds
        "ar": [
            {
                "id": 2001,
                "name": "Test Artist",
            }
        ],
        "al": {
            "id": 3001,
            "name": "Test Album",
            "picUrl": "https://p1.music.126.net/album_cover.jpg",
        },
        "dt": 240000,
        "fee": 0,  # 0 = free, 1 = VIP
    }


@pytest.fixture
def sample_artist() -> dict:
    """Load sample artist fixture."""
    return {
        "id": 2001,
        "name": "Test Artist",
        "picUrl": "https://p1.music.126.net/artist.jpg",
        "albumSize": 10,
        "musicSize": 100,
        "followed": False,
    }


@pytest.fixture
def sample_album() -> dict:
    """Load sample album fixture."""
    return {
        "id": 3001,
        "name": "Test Album",
        "picUrl": "https://p1.music.126.net/album_cover.jpg",
        "publishTime": 1609459200000,
        "artist": {
            "id": 2001,
            "name": "Test Artist",
        },
        "size": 12,
    }


@pytest.fixture
def sample_lyrics() -> dict:
    """Load sample lyrics fixture."""
    return {
        "lrc": {
            "lyric": "[00:00.00]Test Track\n[00:05.00]Line 1\n[00:10.00]Line 2\n[00:15.00]Line 3",
        },
        "tlyric": {
            "lyric": "[00:05.00]翻译行 1\n[00:10.00]翻译行 2",
        },
    }


@pytest.fixture
def sample_song_url() -> dict:
    """Load sample song URL fixture."""
    return {
        "data": [
            {
                "id": 1001,
                "url": "https://music.163.com/song/media/outer/url/test.mp3",
                "br": 320000,
                "size": 10000000,
                "type": "mp3",
            }
        ]
    }


@pytest.fixture
def sample_search_results() -> dict:
    """Load sample search results fixture."""
    return {
        "result": {
            "songs": [
                {
                    "id": 1001,
                    "name": "Test Track",
                    "duration": 240000,
                    "ar": [{"id": 2001, "name": "Test Artist"}],
                    "al": {"id": 3001, "name": "Test Album"},
                }
            ],
            "artists": [
                {
                    "id": 2001,
                    "name": "Test Artist",
                    "picUrl": "https://p1.music.126.net/artist.jpg",
                }
            ],
            "albums": [
                {
                    "id": 3001,
                    "name": "Test Album",
                    "picUrl": "https://p1.music.126.net/album_cover.jpg",
                    "artist": {"id": 2001, "name": "Test Artist"},
                }
            ],
            "playlists": [
                {
                    "id": 123456789,
                    "name": "Test Playlist",
                    "coverImgUrl": "https://p1.music.126.net/cover.jpg",
                    "trackCount": 50,
                }
            ],
        }
    }
