"""Constants for NetEase Cloud Music provider."""

from __future__ import annotations

# Authentication
CONF_TOKEN = "token"
CONF_USER_ID = "user_id"
CONF_REMEMBER_SESSION = "remember_session"
CONF_ACTION_AUTH_QR = "auth_qr"
CONF_ACTION_CLEAR_AUTH = "clear_auth"

# Audio quality
CONF_QUALITY = "quality"
QUALITY_STANDARD = "standard"  # MP3 ~128kbps
QUALITY_HIGH = "high"  # MP3 ~320kbps
QUALITY_LOSSLESS = "lossless"  # FLAC

# API configuration
CONF_BASE_URL = "base_url"
DEFAULT_BASE_URL = "https://music.163.com/api"
DEFAULT_API_SERVER_URL = "http://localhost:3000"  # For local NeteaseCloudMusicApi instance

# Library limits
CONF_LIKED_TRACKS_MAX_TRACKS = "liked_tracks_max_tracks"
DEFAULT_LIKED_TRACKS_MAX = 500

# Browse section IDs
BROWSE_DAILY_RECOMMEND_ID = "daily_recommend"
BROWSE_PERSONALIZED_SONGS_ID = "personalized_songs"
BROWSE_PERSONALIZED_PLAYLISTS_ID = "personalized_playlists"
BROWSE_NEW_ALBUMS_ID = "new_albums"
BROWSE_TOP_CHARTS_ID = "top_charts"
BROWSE_LIKED_SONGS_ID = "liked_songs"
BROWSE_MY_PLAYLISTS_ID = "my_playlists"

# Pagination
PAGE_SIZE = 50
RECOMMEND_BATCH_SIZE = 30

# Image sizes
IMAGE_SIZE_SMALL = "150y150"
IMAGE_SIZE_MEDIUM = "300y300"
IMAGE_SIZE_LARGE = "1024y1024"

# Cache durations
CACHE_DURATION_RECOMMENDATIONS = 3600  # 1 hour
CACHE_DURATION_CHARTS = 7200  # 2 hours
CACHE_DURATION_ARTIST = 86400  # 24 hours
CACHE_DURATION_ALBUM = 86400  # 24 hours

# Logging
LOGGER_PREFIX = "[NetEaseCloudMusic]"
