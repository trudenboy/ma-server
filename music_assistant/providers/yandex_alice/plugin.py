"""
Yandex Alice (Dialogs custom skill) plugin provider for Music Assistant.

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

The account and the skill are set up by the setup flow and stored as setup
data; the options page shows them and offers the actions that need no input.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

from music_assistant_models.config_entries import ConfigActionResult, ConfigValueOption
from music_assistant_models.errors import LoginFailed
from ya_dialogs_api import SkillCreationArtifacts, dump_artifacts
from ya_passport_auth.exceptions import InvalidCredentialsError
from ya_passport_auth.ma import list_yandex_music_instances

from music_assistant.helpers.plugin_engines import (
    create_ai_engine_config_entries,
    select_ai_engine,
)
from music_assistant.models.plugin import PluginProvider

from .constants import (
    CATEGORY_SETTINGS,
    CONF_ACTION_DELETE_SKILL,
    CONF_ACTION_EXPORT_MANIFEST,
    CONF_ACTION_IMPORT_MANIFEST,
    CONF_ACTION_OPEN_DEV_CONSOLE,
    CONF_ACTION_REFRESH_STATUS,
    CONF_ACTION_RESET_MANIFEST,
    CONF_ACTION_TEST_WEBHOOK,
    CONF_ACTION_VALIDATE_MANIFEST,
    CONF_AI_ENGINE,
    CONF_AI_FALLBACK,
    CONF_AUTH_USER_NAME,
    CONF_AUTH_X_TOKEN,
    CONF_DIALOG_AUTO_CREATE_ARTIFACTS,
    CONF_DIALOG_PUBLICATION_STATUS,
    CONF_DIALOG_SKILL_ID,
    CONF_DIALOG_SKILL_NAME,
    CONF_DIALOG_SKILL_OVERRIDE_PASTE,
    CONF_DIALOG_VOICE_CONTINUATION,
    CONF_DIALOG_WEBHOOK_SECRET,
    CONF_EXPOSED_PLAYERS,
    CONF_EXTERNAL_BASE_URL,
    CONF_INSTANCE_NAME,
    CONF_USE_DIFFERENT_INSTANCE_NAME,
    CONF_YM_INSTANCE,
    DIALOG_DEFAULT_NAME,
)
from .dialog_skill_meta import build_backend_uri
from .dialogs import DialogsWebhookHandler
from .publication_status import fetch_skill_publication_status
from .setup_view import AccountStatus, SkillStatus, build_options_entries, diagnostics_entries
from .skill_manifest_provider import SkillManifestProvider
from .skill_ops import SETUP_DATA_KEYS, borrowed_x_token, delete_skill, is_borrowing
from .webhook_probe import probe_webhook_reachability

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ConfigEntry

    from .skill_manifest_provider import ManifestActionResult

_DEV_CONSOLE_URL = "https://dialogs.yandex.ru/developer/skills"


class YandexAlicePlugin(PluginProvider):
    """Plugin provider that wires a Yandex Dialogs custom-skill webhook."""

    _dialogs_handler: DialogsWebhookHandler | None = None

    async def handle_async_init(self) -> None:
        """Read config values and setup data and stash them on the instance."""
        self._instance_name = str(self.config.get_value(CONF_INSTANCE_NAME) or "Music Assistant")
        self._dialog_skill_id = str(self.get_setup_value(CONF_DIALOG_SKILL_ID) or "")
        self._dialog_webhook_secret = str(self.get_setup_value(CONF_DIALOG_WEBHOOK_SECRET) or "")
        exposed_raw = self.config.get_value(CONF_EXPOSED_PLAYERS)
        if isinstance(exposed_raw, list) and exposed_raw:
            self._exposed_player_ids: set[str] | None = {str(item) for item in exposed_raw}
        else:
            self._exposed_player_ids = None
        # Voice continuation (P1.4) — power-user toggle, no UI surface yet.
        # Read directly from the config bag; absent → False (today's behaviour).
        self._voice_continuation = bool(
            self.config.get_value(CONF_DIALOG_VOICE_CONTINUATION) or False
        )
        self._ai_engine_uid: str | None = None
        if self.config.get_value(CONF_AI_FALLBACK):
            # An engine whose plugin is not loaded yet resolves to None here but
            # stays configured; the webhook resolves it again on every question.
            engine = await select_ai_engine(self, CONF_AI_ENGINE)
            configured = str(self.config.get_value(CONF_AI_ENGINE) or "")
            self._ai_engine_uid = engine.uid if engine is not None else configured or None

    async def loaded_in_mass(self) -> None:
        """Register the Dialogs webhook route once the webserver is up."""
        if not (self._dialog_skill_id and self._dialog_webhook_secret):
            return
        # Manifest file access happens once at provider startup, off the event
        # loop. Runtime webhook dispatch uses this immutable in-memory snapshot.
        manifest_provider = await asyncio.to_thread(SkillManifestProvider, self.mass)
        self._dialogs_handler = DialogsWebhookHandler(
            self.mass,
            skill_id=self._dialog_skill_id,
            webhook_secret=self._dialog_webhook_secret,
            exposed_player_ids=self._exposed_player_ids,
            voice_continuation=self._voice_continuation,
            manifest_provider=manifest_provider,
            ai_engine_uid=self._ai_engine_uid,
        )
        self._dialogs_handler.register_routes()

    async def unload(self, is_removed: bool = False) -> None:
        """Tear down the webhook route (idempotent)."""
        _ = is_removed
        if self._dialogs_handler is not None:
            self._dialogs_handler.unregister_routes()
            self._dialogs_handler = None

    def replace_manifest_provider(self, manifest_provider: SkillManifestProvider) -> None:
        """Activate a config-loaded manifest snapshot in the running webhook."""
        if self._dialogs_handler is not None:
            self._dialogs_handler.replace_manifest_provider(manifest_provider)

    def migrate_legacy_config(self) -> None:
        """Copy auth and skill values saved as plain config (before MA 2.10) into setup data."""
        for key in SETUP_DATA_KEYS:
            if key in self.config.setup_data:
                continue
            raw = self.mass.config.get_raw_provider_config_value(self.instance_id, key)
            if raw is None or raw == "":
                continue
            value = self.mass.config.decrypt_string(raw) if isinstance(raw, str) else raw
            self._update_setup_data(key, value)

    def diagnostics_snapshot(self) -> dict[str, Any]:
        """Expose a tiny status snapshot for the config UI (synchronous)."""
        handler = self._dialogs_handler
        return {
            "instance_name": self._instance_name,
            "dialog_skill_id_present": bool(self._dialog_skill_id),
            "dialog_webhook_secret_present": bool(self._dialog_webhook_secret),
            "exposed_player_count": (
                len(self._exposed_player_ids) if self._exposed_player_ids else 0
            ),
            "handler_active": handler is not None,
            "webhook_calls_total": handler.webhook_call_count if handler else 0,
            "authenticated_calls_total": handler.authenticated_call_count if handler else 0,
            "last_webhook_ts": handler.last_webhook_ts if handler else None,
        }

    async def get_diagnostics(self) -> dict[str, Any] | None:
        """Return diagnostics info for MA diagnostics reports."""
        return self.diagnostics_snapshot()

    async def get_config_entries(self) -> tuple[ConfigEntry, ...]:
        """Return the options page: account, skill status, settings and manifest tools."""
        skill_name = str(self.get_setup_value(CONF_DIALOG_SKILL_NAME) or DIALOG_DEFAULT_NAME)
        manifest = await asyncio.to_thread(SkillManifestProvider, self.mass)
        return build_options_entries(
            account=self._account_status(),
            skill=SkillStatus(
                name=skill_name,
                skill_id=str(self.get_setup_value(CONF_DIALOG_SKILL_ID) or ""),
                publication_status=str(self.get_setup_value(CONF_DIALOG_PUBLICATION_STATUS) or ""),
                webhook_url=self._webhook_url(),
            ),
            player_options=self._player_options(),
            instance_name=str(self.config.get_value(CONF_INSTANCE_NAME) or skill_name),
            use_different_instance_name=bool(
                self.config.get_value(CONF_USE_DIFFERENT_INSTANCE_NAME) or False
            ),
            manifest_status=manifest.status(),
            manifest_paste=str(self.config.get_value(CONF_DIALOG_SKILL_OVERRIDE_PASTE) or ""),
            diagnostics=diagnostics_entries(
                self.diagnostics_snapshot() if self._dialogs_handler is not None else None
            ),
            ai_engine_entries=await create_ai_engine_config_entries(
                self.mass, CONF_AI_ENGINE, depends_on=CONF_AI_FALLBACK, category=CATEGORY_SETTINGS
            ),
        )

    async def handle_config_action(self, action: str) -> ConfigActionResult | None:
        """
        Run an options-page button.

        :param action: The pressed button's action id.
        :raises LoginFailed: The action needs the Yandex account and it is not usable.
        """
        skill_id = str(self.get_setup_value(CONF_DIALOG_SKILL_ID) or "")
        if action == CONF_ACTION_TEST_WEBHOOK:
            reachable, message = await probe_webhook_reachability(
                str(self.get_setup_value(CONF_EXTERNAL_BASE_URL) or ""),
                str(self.get_setup_value(CONF_DIALOG_WEBHOOK_SECRET) or ""),
            )
            return ConfigActionResult(message=("✅ " if reachable else "❌ ") + message)
        if action == CONF_ACTION_OPEN_DEV_CONSOLE:
            return ConfigActionResult(
                open_url=f"{_DEV_CONSOLE_URL}/{skill_id}" if skill_id else _DEV_CONSOLE_URL
            )
        if action == CONF_ACTION_REFRESH_STATUS:
            status = await fetch_skill_publication_status(self._x_token(), skill_id)
            if status is None:
                return ConfigActionResult(
                    message="Could not fetch the publication status from Yandex Dialogs.",
                    translation_key="status_unavailable",
                )
            self._update_setup_data(CONF_DIALOG_PUBLICATION_STATUS, status)
            return ConfigActionResult(
                message="Publication status refreshed.", translation_key="status_refreshed"
            )
        if action == CONF_ACTION_DELETE_SKILL:
            x_token = self._x_token()
            try:
                await delete_skill(x_token, skill_id)
            except InvalidCredentialsError as err:
                raise LoginFailed(str(err)) from err
            await self.unload()
            self._update_setup_data(CONF_DIALOG_SKILL_ID, "")
            self._update_setup_data(CONF_DIALOG_PUBLICATION_STATUS, "")
            self._update_setup_data(
                CONF_DIALOG_AUTO_CREATE_ARTIFACTS, dump_artifacts(SkillCreationArtifacts())
            )
            return ConfigActionResult(
                message="Skill deleted. Reconfigure the provider to create a new one.",
                translation_key="skill_deleted",
            )
        if action in (
            CONF_ACTION_EXPORT_MANIFEST,
            CONF_ACTION_IMPORT_MANIFEST,
            CONF_ACTION_RESET_MANIFEST,
            CONF_ACTION_VALIDATE_MANIFEST,
        ):
            return await self._manifest_action(action)
        return await super().handle_config_action(action)  # type: ignore[return-value]

    async def _manifest_action(self, action: str) -> ConfigActionResult:
        """Run a skill-manifest button and activate a successful result in the webhook."""
        manifest = await asyncio.to_thread(SkillManifestProvider, self.mass)
        result: ManifestActionResult
        if action == CONF_ACTION_EXPORT_MANIFEST:
            result = await asyncio.to_thread(manifest.export_to_override)
        elif action == CONF_ACTION_IMPORT_MANIFEST:
            paste = str(self.config.get_value(CONF_DIALOG_SKILL_OVERRIDE_PASTE) or "")
            result = await asyncio.to_thread(manifest.import_from_paste, paste)
            if manifest.last_import_success:
                self._update_config_value(CONF_DIALOG_SKILL_OVERRIDE_PASTE, "")
        elif action == CONF_ACTION_RESET_MANIFEST:
            result = await asyncio.to_thread(manifest.reset_override)
        else:
            result = await asyncio.to_thread(manifest.validate_override_message)
        if result.success:
            self.replace_manifest_provider(manifest)
        return ConfigActionResult(
            translation_key=result.translation_key,
            translation_args=list(result.translation_params),
        )

    def _x_token(self) -> str:
        """Return the x_token the provider acts with (own or borrowed)."""
        source = self.get_setup_value(CONF_YM_INSTANCE)
        if is_borrowing(source):
            return borrowed_x_token(self.mass, str(source))
        if x_token := str(self.get_setup_value(CONF_AUTH_X_TOKEN) or ""):
            return x_token
        msg = "Not signed in to Yandex. Reconfigure the provider to sign in."
        raise LoginFailed(msg)

    def _account_status(self) -> AccountStatus:
        """Describe the Yandex account for the options page."""
        source = self.get_setup_value(CONF_YM_INSTANCE)
        try:
            self._x_token()
        except LoginFailed:
            signed_in = False
        else:
            signed_in = True
        borrowed_from: str | None = None
        if is_borrowing(source):
            names = dict(list_yandex_music_instances(self.mass))
            borrowed_from = names.get(str(source), str(source))
        return AccountStatus(
            borrowed_from=borrowed_from,
            user_name=str(self.get_setup_value(CONF_AUTH_USER_NAME) or ""),
            signed_in=signed_in,
        )

    def _webhook_url(self) -> str:
        """Return the public webhook URL registered with Yandex ("" when not set up)."""
        try:
            return build_backend_uri(
                str(self.get_setup_value(CONF_EXTERNAL_BASE_URL) or ""),
                str(self.get_setup_value(CONF_DIALOG_WEBHOOK_SECRET) or ""),
            )
        except ValueError:
            return ""

    def _player_options(self) -> list[ConfigValueOption]:
        """List MA players the user can expose to voice control."""
        try:
            return [
                ConfigValueOption(
                    title=player.display_name or player.name or player.player_id,
                    value=player.player_id,
                )
                for player in self.mass.players.all_players()
            ]
        except Exception as err:
            self.logger.debug("could not enumerate players: %s", err)
            return []
