"""NetEase Cloud Music provider support for Music Assistant."""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

from music_assistant_models.config_entries import ConfigEntry, ConfigValueOption, ConfigValueType
from music_assistant_models.enums import ConfigEntryType, ProviderFeature
from music_assistant_models.errors import InvalidDataError

from .constants import (
    CONF_ACTION_AUTH_QR,
    CONF_ACTION_CLEAR_AUTH,
    CONF_BASE_URL,
    CONF_LIKED_TRACKS_MAX_TRACKS,
    CONF_QUALITY,
    CONF_REMEMBER_SESSION,
    CONF_TOKEN,
    CONF_USER_ID,
    DEFAULT_BASE_URL,
    QUALITY_HIGH,
    QUALITY_LOSSLESS,
    QUALITY_STANDARD,
)
from .provider import NetEaseCloudMusicProvider
from .ncloud_auth import perform_qr_auth

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ProviderConfig
    from music_assistant_models.provider import ProviderManifest

    from music_assistant.mass import MusicAssistant
    from music_assistant.models import ProviderInstanceType

SUPPORTED_FEATURES = {
    ProviderFeature.LIBRARY_ARTISTS,
    ProviderFeature.LIBRARY_ALBUMS,
    ProviderFeature.LIBRARY_TRACKS,
    ProviderFeature.LIBRARY_PLAYLISTS,
    ProviderFeature.ARTIST_ALBUMS,
    ProviderFeature.ARTIST_TOPTRACKS,
    ProviderFeature.SEARCH,
    ProviderFeature.LIBRARY_ARTISTS_EDIT,
    ProviderFeature.LIBRARY_ALBUMS_EDIT,
    ProviderFeature.LIBRARY_TRACKS_EDIT,
    ProviderFeature.LIBRARY_PLAYLISTS_EDIT,
    ProviderFeature.BROWSE,
    ProviderFeature.SIMILAR_TRACKS,
    ProviderFeature.RECOMMENDATIONS,
    ProviderFeature.LYRICS,
}


async def setup(
    mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
) -> ProviderInstanceType:
    """Initialize provider(instance) with given configuration."""
    return NetEaseCloudMusicProvider(mass, manifest, config, SUPPORTED_FEATURES)


async def get_config_entries(
    mass: MusicAssistant,
    instance_id: str | None = None,  # noqa: ARG001
    action: str | None = None,
    values: dict[str, ConfigValueType] | None = None,
) -> tuple[ConfigEntry, ...]:
    """Return Config entries to setup this provider."""
    if values is None:
        values = {}

    # Handle QR auth action
    if action == CONF_ACTION_AUTH_QR:
        session_id = values.get("session_id")
        if not session_id:
            raise InvalidDataError("Missing session_id for QR authentication")
        user_id, token = await perform_qr_auth(mass, str(session_id))
        values[CONF_TOKEN] = token
        if values.get(CONF_REMEMBER_SESSION, True):
            values[CONF_USER_ID] = user_id
        else:
            values[CONF_USER_ID] = None

    # Handle Clear auth action
    if action == CONF_ACTION_CLEAR_AUTH:
        values[CONF_TOKEN] = None
        values[CONF_USER_ID] = None

    # Check if user is authenticated
    is_authenticated = bool(values.get(CONF_TOKEN))

    # Dynamic label text
    if not is_authenticated:
        label_text = (
            "Scan a QR code with the NetEase Cloud Music app on your phone to authenticate.\n\n"
            "Alternatively, you can enter a cookie/token manually in the advanced settings."
        )
    elif action == CONF_ACTION_AUTH_QR:
        label_text = (
            "Authenticated to NetEase Cloud Music. Don't forget to save to complete setup."
        )
    else:
        label_text = "Authenticated to NetEase Cloud Music."

    return (
        # Status label
        ConfigEntry(
            key="label_text",
            type=ConfigEntryType.LABEL,
            label=label_text,
        ),
        # QR authentication (primary)
        ConfigEntry(
            key=CONF_ACTION_AUTH_QR,
            type=ConfigEntryType.ACTION,
            label="Login with QR code",
            description="Opens a QR code page — scan it with the NetEase Cloud Music app.",
            action=CONF_ACTION_AUTH_QR,
            action_label="Login with QR code",
            hidden=is_authenticated,
        ),
        # Remember session toggle
        ConfigEntry(
            key=CONF_REMEMBER_SESSION,
            type=ConfigEntryType.BOOLEAN,
            label="Remember session (auto-refresh token)",
            description="When enabled, stores a long-lived session to automatically "
            "refresh your token when it expires. When disabled, you must "
            "re-authenticate manually when the token expires.",
            default_value=True,
            hidden=is_authenticated,
        ),
        # Clear auth
        ConfigEntry(
            key=CONF_ACTION_CLEAR_AUTH,
            type=ConfigEntryType.ACTION,
            label="Reset authentication",
            description="Clear the current authentication details.",
            action=CONF_ACTION_CLEAR_AUTH,
            action_label="Reset authentication",
            hidden=not is_authenticated,
        ),
        # Token storage (populated by QR action or manual entry)
        ConfigEntry(
            key=CONF_TOKEN,
            type=ConfigEntryType.SECURE_STRING,
            label="NetEase Cloud Music Token",
            description="Auth token — populated automatically by QR login, "
            "or enter manually (cookie string). See the documentation for how to obtain it.",
            required=True,
            hidden=is_authenticated,
            value=cast("str", values.get(CONF_TOKEN)) if values else None,
        ),
        # User ID (internal storage, always hidden)
        ConfigEntry(
            key=CONF_USER_ID,
            type=ConfigEntryType.SECURE_STRING,
            label="User ID",
            hidden=True,
            required=False,
            value=cast("str", values.get(CONF_USER_ID)) if values else None,
        ),
        # Quality
        ConfigEntry(
            key=CONF_QUALITY,
            type=ConfigEntryType.STRING,
            label="Audio quality",
            description="Select preferred audio quality.",
            options=[
                ConfigValueOption("Standard (MP3 ~128kbps)", QUALITY_STANDARD),
                ConfigValueOption("High (MP3 ~320kbps)", QUALITY_HIGH),
                ConfigValueOption("Lossless (FLAC)", QUALITY_LOSSLESS),
            ],
            default_value=QUALITY_HIGH,
        ),
        # Liked Tracks maximum tracks (advanced)
        ConfigEntry(
            key=CONF_LIKED_TRACKS_MAX_TRACKS,
            type=ConfigEntryType.INTEGER,
            label="Liked Tracks maximum tracks",
            description="Maximum number of tracks to show in Liked Tracks virtual playlist. "
            "Higher values may significantly increase load time. "
            "Lower values load faster. Default: 500.",
            range=(50, 2000),
            default_value=500,
            required=False,
            advanced=True,
        ),
        # API Base URL (advanced)
        ConfigEntry(
            key=CONF_BASE_URL,
            type=ConfigEntryType.STRING,
            label="API Base URL",
            description="API endpoint base URL. "
            "Only change if you're running a local instance of NeteaseCloudMusicApi. "
            f"Default: {DEFAULT_BASE_URL}",
            default_value=DEFAULT_BASE_URL,
            required=False,
            advanced=True,
        ),
    )
