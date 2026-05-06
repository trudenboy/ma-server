"""Yandex Alice (Dialogs custom skill) plugin provider for Music Assistant.

Bridges a Yandex Dialogs custom-skill webhook to MA player commands. The
provider exposes a single HTTP route (``/api/yandex_dialogs/webhook/<secret>``)
that Yandex calls on every user utterance; the handler performs Russian NLU,
resolves the addressed player, and dispatches the parsed intent to
``mass.player_queues`` / ``mass.players``.

Unlike the Smart Home device-bridge in ma-provider-yandex-smarthome, this
provider does not register MA players as Yandex IoT devices — it operates
entirely through the Dialogs custom-skill protocol, which gives it access
to richer commands (search-and-play, transfer queue, shuffle/repeat,
seek, now-playing) at the cost of requiring the user to invoke the skill
explicitly (*«Алиса, попроси Music Assistant …»*).
"""

from __future__ import annotations

from typing import Any

from music_assistant.models.plugin import PluginProvider

from .constants import (
    CONF_DIALOG_SKILL_ENABLED,
    CONF_DIALOG_SKILL_ID,
    CONF_DIALOG_WEBHOOK_SECRET,
    CONF_EXPOSED_PLAYERS,
    CONF_INSTANCE_NAME,
)
from .dialogs import DialogsWebhookHandler


class YandexAlicePlugin(PluginProvider):
    """Plugin provider that wires a Yandex Dialogs custom-skill webhook."""

    _dialogs_handler: DialogsWebhookHandler | None = None

    async def handle_async_init(self) -> None:
        """Read config values and stash them on the instance."""
        self._instance_name = str(self.config.get_value(CONF_INSTANCE_NAME) or "Music Assistant")
        self._dialog_skill_enabled = bool(self.config.get_value(CONF_DIALOG_SKILL_ENABLED))
        self._dialog_skill_id = str(self.config.get_value(CONF_DIALOG_SKILL_ID) or "")
        self._dialog_webhook_secret = str(self.config.get_value(CONF_DIALOG_WEBHOOK_SECRET) or "")
        exposed_raw = self.config.get_value(CONF_EXPOSED_PLAYERS)
        if isinstance(exposed_raw, list) and exposed_raw:
            self._exposed_player_ids: set[str] | None = {str(item) for item in exposed_raw}
        else:
            self._exposed_player_ids = None

    async def loaded_in_mass(self) -> None:
        """Register the Dialogs webhook route once the webserver is up."""
        if not (
            self._dialog_skill_enabled and self._dialog_skill_id and self._dialog_webhook_secret
        ):
            return
        self._dialogs_handler = DialogsWebhookHandler(
            self.mass,
            skill_id=self._dialog_skill_id,
            webhook_secret=self._dialog_webhook_secret,
            exposed_player_ids=self._exposed_player_ids,
        )
        self._dialogs_handler.register_routes()

    async def unload(self, is_removed: bool = False) -> None:
        """Tear down the webhook route (idempotent)."""
        _ = is_removed
        if self._dialogs_handler is not None:
            self._dialogs_handler.unregister_routes()
            self._dialogs_handler = None

    # MA may call into the provider for diagnostics; keep a noop attribute hook.
    def get_diagnostics(self) -> dict[str, Any]:
        """Expose a tiny status snapshot for MA diagnostics."""
        return {
            "instance_name": self._instance_name,
            "dialog_skill_enabled": self._dialog_skill_enabled,
            "dialog_skill_id_present": bool(self._dialog_skill_id),
            "dialog_webhook_secret_present": bool(self._dialog_webhook_secret),
            "exposed_player_count": (
                len(self._exposed_player_ids) if self._exposed_player_ids else 0
            ),
            "handler_active": self._dialogs_handler is not None,
        }
