"""Yandex Alice (Dialogs custom skill) plugin provider for Music Assistant.

Exposes Music Assistant playback to a Yandex Dialogs custom skill — a Russian
NLU voice control surface invoked via *«Алиса, попроси Music Assistant …»*.

Setup is **manual** in this release:
1. User creates a custom dialog skill in https://dialogs.yandex.ru/developer
2. User points the skill's webhook URL at MA's
   ``/api/yandex_dialogs/webhook/<your-secret>`` endpoint.
3. User pastes the skill ID, skill token, and webhook secret into the
   provider's config form.

A future release will add an auto-create flow that uses the
``ya-dialogs-api`` library to register the skill programmatically; for now
manual setup keeps the v1.0 surface small and reliable.
"""

from __future__ import annotations

import logging
import secrets
from typing import TYPE_CHECKING

from music_assistant_models.config_entries import ConfigEntry, ConfigValueOption
from music_assistant_models.enums import ConfigEntryType, ProviderFeature

from .constants import (
    CONF_DIALOG_SKILL_ENABLED,
    CONF_DIALOG_SKILL_ID,
    CONF_DIALOG_SKILL_NAME,
    CONF_DIALOG_SKILL_TOKEN,
    CONF_DIALOG_WEBHOOK_SECRET,
    CONF_EXPOSED_PLAYERS,
    CONF_EXPOSED_PLAYLISTS,
    CONF_EXTERNAL_BASE_URL,
    CONF_INSTANCE_NAME,
    DIALOG_DEFAULT_NAME,
    DIALOG_NAME_MAX_LEN,
    DIALOG_NAME_MIN_LEN,
    DIALOG_WEBHOOK_BASE_PATH,
    YANDEX_DIALOGS_DEVELOPER_URL,
)
from .playlists import fetch_playlist_options
from .plugin import YandexAlicePlugin

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ConfigValueType, ProviderConfig
    from music_assistant_models.provider import ProviderManifest

    from music_assistant.mass import MusicAssistant
    from music_assistant.models import ProviderInstanceType


_LOGGER = logging.getLogger(__name__)

SUPPORTED_FEATURES: set[ProviderFeature] = set()


async def setup(
    mass: MusicAssistant,
    manifest: ProviderManifest,
    config: ProviderConfig,
) -> ProviderInstanceType:
    """Initialise the provider instance with the given configuration."""
    return YandexAlicePlugin(mass, manifest, config, SUPPORTED_FEATURES)


def _generate_webhook_secret() -> str:
    """Return a fresh URL-safe random secret for the webhook path."""
    return secrets.token_urlsafe(24)


async def _list_player_options(mass: MusicAssistant) -> list[ConfigValueOption]:
    """List MA players the user can expose to voice control."""
    options: list[ConfigValueOption] = []
    try:
        for player in mass.players.all_players():
            options.append(
                ConfigValueOption(
                    title=player.display_name or player.name or player.player_id,
                    value=player.player_id,
                )
            )
    except Exception as exc:
        _LOGGER.debug("could not enumerate players: %s", exc)
    return options


async def get_config_entries(
    mass: MusicAssistant,
    instance_id: str | None = None,
    action: str | None = None,
    values: dict[str, ConfigValueType] | None = None,
) -> tuple[ConfigEntry, ...]:
    """Build the provider config-form entries.

    Args:
        mass: MusicAssistant runtime, used to enumerate players/playlists.
        instance_id: Stable provider instance ID (unused — single-instance).
        action: Action key when the user clicked a button. Currently unused
            (auto-create / rename actions ship in a later release).
        values: Current form values (echoed back across submissions).
    """
    _ = instance_id, action
    values = values or {}

    # Generate a webhook secret on first open if the user hasn't set one yet.
    existing_secret = str(values.get(CONF_DIALOG_WEBHOOK_SECRET) or "").strip()
    default_secret = existing_secret or _generate_webhook_secret()

    instance_name = str(values.get(CONF_INSTANCE_NAME) or DIALOG_DEFAULT_NAME)

    # Player options used both by the exposed-players selector and the
    # exposed-playlists selector below.
    player_options = await _list_player_options(mass)
    try:
        playlist_options = await fetch_playlist_options(mass)
    except Exception as exc:
        _LOGGER.debug("could not enumerate playlists: %s", exc)
        playlist_options = []

    base_url_hint = (
        "Public HTTPS URL of this Music Assistant instance, "
        "as Yandex will see it (e.g. https://ma.example.com). "
        "Leave empty to use MA's global Base URL setting."
    )

    return (
        ConfigEntry(
            key="label_intro",
            type=ConfigEntryType.LABEL,
            label=(
                "🎙️ Yandex Alice voice control. Create a custom dialog "
                f"skill at {YANDEX_DIALOGS_DEVELOPER_URL}, point its "
                "webhook at the URL shown below, and paste the skill ID + "
                "token from the dev console."
            ),
        ),
        ConfigEntry(
            key=CONF_INSTANCE_NAME,
            type=ConfigEntryType.STRING,
            label="Instance name",
            description=(
                "Display name shown to users. Pick something they will say "
                'to invoke the skill, e.g. "Music Assistant" → '
                "«Алиса, попроси Music Assistant …»"
            ),
            required=False,
            default_value=DIALOG_DEFAULT_NAME,
        ),
        ConfigEntry(
            key=CONF_EXTERNAL_BASE_URL,
            type=ConfigEntryType.STRING,
            label="External base URL (optional)",
            description=base_url_hint,
            required=False,
            default_value="",
        ),
        ConfigEntry(
            key=CONF_DIALOG_SKILL_ENABLED,
            type=ConfigEntryType.BOOLEAN,
            label="Enable dialog skill",
            description=(
                "Turn this on once you have created the custom skill in "
                "the Yandex dev console and pasted the credentials below."
            ),
            required=False,
            default_value=False,
        ),
        ConfigEntry(
            key=CONF_DIALOG_SKILL_NAME,
            type=ConfigEntryType.STRING,
            label="Skill name (informational)",
            description=(
                f"Used in UI labels only. Min {DIALOG_NAME_MIN_LEN}, "
                f"max {DIALOG_NAME_MAX_LEN} characters."
            ),
            required=False,
            default_value=instance_name,
        ),
        ConfigEntry(
            key=CONF_DIALOG_SKILL_ID,
            type=ConfigEntryType.STRING,
            label="Skill ID",
            description=(
                "UUID from the dev console URL (https://dialogs.yandex.ru/developer/skills/<this>)."
            ),
            required=False,
            default_value="",
        ),
        ConfigEntry(
            key=CONF_DIALOG_SKILL_TOKEN,
            type=ConfigEntryType.SECURE_STRING,
            label="Skill OAuth token",
            description=(
                "OAuth token from "
                "https://oauth.yandex.ru/authorize?response_type=token"
                "&client_id=c473ca268cd749d3a8371351a8f2bcbd. "
                "Used to push state callbacks to Yandex (future feature; "
                "stored encrypted)."
            ),
            required=False,
            default_value="",
        ),
        ConfigEntry(
            key=CONF_DIALOG_WEBHOOK_SECRET,
            type=ConfigEntryType.SECURE_STRING,
            label="Webhook URL secret",
            description=(
                "Random secret embedded in the webhook URL. The full URL to "
                "paste into the dev console's webhook field is "
                f"<external_base_url>{DIALOG_WEBHOOK_BASE_PATH}/<this-secret>. "
                "Pre-filled with a fresh value; click 'Save' to commit."
            ),
            required=False,
            default_value=default_secret,
        ),
        ConfigEntry(
            key=CONF_EXPOSED_PLAYERS,
            type=ConfigEntryType.STRING,
            label="Voice-controllable players",
            description=(
                "Players the skill is allowed to control. Leave empty to "
                "expose all players known to MA."
            ),
            multi_value=True,
            options=player_options,
            required=False,
            default_value=[],
        ),
        ConfigEntry(
            key=CONF_EXPOSED_PLAYLISTS,
            type=ConfigEntryType.STRING,
            label="Voice-addressable playlists",
            description=(
                "Optional curated list of playlists the user can ask for by "
                "name. Leave empty for full library search."
            ),
            multi_value=True,
            options=playlist_options,
            required=False,
            default_value=[],
        ),
    )
