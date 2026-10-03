"""
Yandex Smart Home Plugin Provider for Music Assistant.

Exposes Music Assistant players as Yandex Smart Home devices so Alice can
control playback (play / pause / volume / mute / source) via natural-language
commands. Authentication and provisioning are handled by ``setup_flow.py``;
only playback options remain configurable after setup.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from .plugin import YandexSmartHomePlugin

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ProviderConfig
    from music_assistant_models.enums import ProviderFeature
    from music_assistant_models.provider import ProviderManifest

    from music_assistant.mass import MusicAssistant
    from music_assistant.models import ProviderInstanceType


SUPPORTED_FEATURES: set[ProviderFeature] = set()


async def setup(
    mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
) -> ProviderInstanceType:
    """Initialize provider(instance) with given configuration."""
    return YandexSmartHomePlugin(mass, manifest, config, SUPPORTED_FEATURES)
