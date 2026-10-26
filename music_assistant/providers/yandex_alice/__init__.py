"""
Yandex Alice (Dialogs custom skill) plugin provider for Music Assistant.

Exposes Music Assistant playback to a Yandex Dialogs custom skill — a Russian
NLU voice control surface invoked via *«Алиса, попроси Music Assistant …»*.

Adding the provider runs the setup flow (``setup_flow.py``): sign in to
Yandex (or borrow a linked Yandex Music account), then create the skill
automatically or register a skill made by hand in the Yandex Dialogs console.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from .plugin import YandexAlicePlugin

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ProviderConfig
    from music_assistant_models.enums import ProviderFeature
    from music_assistant_models.provider import ProviderManifest

    from music_assistant.mass import MusicAssistant
    from music_assistant.models import ProviderInstanceType

SUPPORTED_FEATURES: set[ProviderFeature] = set()


async def setup(
    mass: MusicAssistant,
    manifest: ProviderManifest,
    config: ProviderConfig,
) -> ProviderInstanceType:
    """Initialise the provider instance with the given configuration."""
    provider = YandexAlicePlugin(mass, manifest, config, SUPPORTED_FEATURES)
    provider.migrate_legacy_config()
    return provider
