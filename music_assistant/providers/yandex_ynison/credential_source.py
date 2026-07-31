"""Read linked Yandex Music credentials from Music Assistant setup data."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from music_assistant_models.enums import ProviderType
from music_assistant_models.errors import LoginFailed, ResourceTemporarilyUnavailable
from ya_passport_auth import SecretStr

from .constants import YANDEX_MUSIC_CONF_TOKEN, YANDEX_MUSIC_CONF_X_TOKEN

if TYPE_CHECKING:
    from music_assistant.mass import MusicAssistant


class YandexMusicCredentialSource:
    """Read credentials owned by one linked Yandex Music provider instance."""

    def __init__(self, mass: MusicAssistant, instance_id: str) -> None:
        """
        Initialize the credential source.

        :param mass: Music Assistant instance used to resolve the provider.
        :param instance_id: Linked Yandex Music provider instance id.
        """
        self._mass = mass
        self._instance_id = instance_id

    def read_tokens(self) -> tuple[SecretStr | None, SecretStr | None]:
        """Return the linked provider's music token and x-token."""
        owner = self._mass.get_provider(self._instance_id)
        if owner is None:
            raise ResourceTemporarilyUnavailable(
                f"Linked Yandex Music provider '{self._instance_id}' is not loaded yet"
            )
        if owner.domain != "yandex_music" or owner.type != ProviderType.MUSIC:
            raise LoginFailed(
                f"Linked provider '{self._instance_id}' is not a Yandex Music provider. "
                "Reconfigure this Ynison instance and select a Yandex Music provider."
            )
        get_setup_value = getattr(owner, "get_setup_value", None)
        if not callable(get_setup_value):
            raise LoginFailed(
                f"Linked Yandex Music provider '{self._instance_id}' cannot expose setup "
                "credentials. Upgrade Music Assistant or the Yandex Music provider."
            )
        return (
            self._to_secret(get_setup_value(YANDEX_MUSIC_CONF_TOKEN)),
            self._to_secret(get_setup_value(YANDEX_MUSIC_CONF_X_TOKEN)),
        )

    @staticmethod
    def _to_secret(value: Any) -> SecretStr | None:
        """Normalize a setup-data credential to ``SecretStr``."""
        if isinstance(value, SecretStr):
            return value
        if isinstance(value, str) and value:
            return SecretStr(value)
        return None
