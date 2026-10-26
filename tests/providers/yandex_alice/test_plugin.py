"""Lifecycle, options page and config actions of the Yandex Alice plugin provider."""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from music_assistant_models.errors import ActionUnavailable, LoginFailed
from ya_passport_auth.ma import BORROW_SOURCE_OWN

from music_assistant.providers.yandex_alice import plugin as plugin_module
from music_assistant.providers.yandex_alice import setup
from music_assistant.providers.yandex_alice.constants import (
    CONF_ACTION_DELETE_SKILL,
    CONF_ACTION_IMPORT_MANIFEST,
    CONF_ACTION_OPEN_DEV_CONSOLE,
    CONF_ACTION_REFRESH_STATUS,
    CONF_ACTION_TEST_WEBHOOK,
    CONF_AUTH_USER_NAME,
    CONF_AUTH_X_TOKEN,
    CONF_DIALOG_PUBLICATION_STATUS,
    CONF_DIALOG_SKILL_ID,
    CONF_DIALOG_SKILL_NAME,
    CONF_DIALOG_SKILL_OVERRIDE_PASTE,
    CONF_DIALOG_VOICE_CONTINUATION,
    CONF_DIALOG_WEBHOOK_SECRET,
    CONF_EXPOSED_PLAYERS,
    CONF_EXTERNAL_BASE_URL,
    CONF_INSTANCE_NAME,
    CONF_YM_INSTANCE,
)
from music_assistant.providers.yandex_alice.plugin import YandexAlicePlugin
from music_assistant.providers.yandex_alice.skill_manifest_provider import (
    ManifestActionResult,
    SkillManifestProvider,
)

INSTANCE = "yandex_alice"
SETUP: dict[str, Any] = {
    CONF_YM_INSTANCE: BORROW_SOURCE_OWN,
    CONF_AUTH_X_TOKEN: "x-own",
    CONF_AUTH_USER_NAME: "alice.user",
    CONF_DIALOG_SKILL_ID: "sk-1",
    CONF_DIALOG_SKILL_NAME: "Music Assistant",
    CONF_DIALOG_WEBHOOK_SECRET: "secret-1",
    CONF_EXTERNAL_BASE_URL: "https://ma.example.org",
    CONF_DIALOG_PUBLICATION_STATUS: "on_air",
}


class _FakeConfigController:
    """Path-addressed storage with the encryption helpers MA's config controller has."""

    def __init__(self, values: dict[str, Any], setup_data: dict[str, Any]) -> None:
        """Store raw provider values and setup data the way MA persists them."""
        self.data: dict[str, Any] = {
            "providers": {
                INSTANCE: {
                    "values": {
                        key: self.encrypt_string(value) if key == CONF_AUTH_X_TOKEN else value
                        for key, value in values.items()
                    },
                    "setup_data": {
                        key: self.encrypt_string(value) if isinstance(value, str) else value
                        for key, value in setup_data.items()
                    },
                }
            }
        }

    def get(self, path: str, default: Any = None) -> Any:
        """Read a value by slash-separated path."""
        node: Any = self.data
        for part in path.split("/"):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node

    def set(self, path: str, value: Any, immediate: bool = False) -> None:
        """Write a value by slash-separated path."""
        *parents, leaf = path.split("/")
        node = self.data
        for part in parents:
            node = node.setdefault(part, {})
        node[leaf] = value

    def get_raw_provider_config_value(self, instance: str, key: str, default: Any = None) -> Any:
        """Return a stored provider config value."""
        return self.get(f"providers/{instance}/values/{key}", default)

    def set_raw_provider_config_value(
        self, instance: str, key: str, value: Any, encrypted: bool = False, immediate: bool = False
    ) -> None:
        """Store a provider config value."""
        self.set(f"providers/{instance}/values/{key}", value)

    @staticmethod
    def encrypt_string(value: str) -> str:
        """Mark a value as encrypted."""
        return f"enc:{value}"

    @staticmethod
    def decrypt_string(value: str) -> str:
        """Strip the encryption marker (plain values pass through)."""
        return value.removeprefix("enc:")


def _mass_and_config(
    tmp_path: Path, values: dict[str, Any], setup_data: dict[str, Any]
) -> tuple[MagicMock, MagicMock, MagicMock]:
    mass = MagicMock()
    mass.storage_path = str(tmp_path)
    mass.webserver.register_dynamic_route = MagicMock(return_value=MagicMock())
    mass.players.all_players.return_value = []
    raw_values = {"log_level": "GLOBAL", **values}
    mass.config = _FakeConfigController(raw_values, setup_data)
    config = MagicMock()
    config.instance_id = INSTANCE
    config.name = None
    config.values = {}
    config.setup_data = mass.config.data["providers"][INSTANCE]["setup_data"]
    config.get_value.side_effect = lambda key, default=None: raw_values.get(key, default)
    manifest = MagicMock()
    manifest.domain = "yandex_alice"
    return mass, config, manifest


def _plugin(
    tmp_path: Path,
    *,
    values: dict[str, Any] | None = None,
    setup_data: dict[str, Any] | None = None,
) -> tuple[YandexAlicePlugin, MagicMock]:
    mass, config, manifest = _mass_and_config(
        tmp_path, values or {}, dict(SETUP if setup_data is None else setup_data)
    )
    return YandexAlicePlugin(mass, manifest, config, set()), mass


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_runtime_configuration_comes_from_setup_data(tmp_path: Path) -> None:
    """Skill id and webhook secret are read from setup data, options from config."""
    plugin, _mass = _plugin(
        tmp_path,
        values={
            CONF_INSTANCE_NAME: "Alice Room",
            CONF_EXPOSED_PLAYERS: ["p1", 2],
            CONF_DIALOG_VOICE_CONTINUATION: True,
        },
    )
    await plugin.handle_async_init()
    assert plugin._instance_name == "Alice Room"
    assert plugin._dialog_skill_id == "sk-1"
    assert plugin._dialog_webhook_secret == "secret-1"
    assert plugin._exposed_player_ids == {"p1", "2"}
    assert plugin._voice_continuation is True


@pytest.mark.asyncio
async def test_setup_migrates_legacy_config_values(tmp_path: Path) -> None:
    """A pre-2.10 install has its auth and skill keys copied into setup data once."""
    mass, config, manifest = _mass_and_config(
        tmp_path,
        values={
            CONF_AUTH_X_TOKEN: "x-legacy",
            CONF_DIALOG_SKILL_ID: "sk-legacy",
            CONF_DIALOG_WEBHOOK_SECRET: "secret-legacy",
        },
        setup_data={CONF_DIALOG_SKILL_NAME: "Kept name"},
    )
    provider = await setup(mass, manifest, config)
    assert isinstance(provider, YandexAlicePlugin)
    assert provider.get_setup_value(CONF_AUTH_X_TOKEN) == "x-legacy"
    assert provider.get_setup_value(CONF_DIALOG_SKILL_ID) == "sk-legacy"
    assert provider.get_setup_value(CONF_DIALOG_WEBHOOK_SECRET) == "secret-legacy"
    assert provider.get_setup_value(CONF_DIALOG_SKILL_NAME) == "Kept name"
    stored = mass.config.get(f"providers/{INSTANCE}/setup_data/{CONF_AUTH_X_TOKEN}")
    assert stored == "enc:x-legacy"


@pytest.mark.asyncio
async def test_migration_keeps_existing_setup_data(tmp_path: Path) -> None:
    """Values already in setup data win over stale legacy config values."""
    mass, config, manifest = _mass_and_config(
        tmp_path, values={CONF_DIALOG_SKILL_ID: "sk-stale"}, setup_data=dict(SETUP)
    )
    provider = await setup(mass, manifest, config)
    assert provider.get_setup_value(CONF_DIALOG_SKILL_ID) == "sk-1"  # type: ignore[union-attr]


@pytest.mark.asyncio
async def test_loaded_and_unload_register_exactly_one_route(tmp_path: Path) -> None:
    """The provider lifecycle registers the webhook, then tears it down."""
    plugin, mass = _plugin(tmp_path)
    unregister = MagicMock()
    mass.webserver.register_dynamic_route.return_value = unregister
    await plugin.handle_async_init()
    await plugin.loaded_in_mass()
    assert plugin._dialogs_handler is not None
    assert mass.webserver.register_dynamic_route.call_args.args[0] == (
        "/api/yandex_dialogs/webhook/secret-1"
    )
    await plugin.unload()
    unregister.assert_called_once()
    assert plugin._dialogs_handler is None


@pytest.mark.asyncio
async def test_loaded_without_skill_does_not_register_route(tmp_path: Path) -> None:
    """Without a skill id and secret the webhook stays unregistered."""
    plugin, mass = _plugin(tmp_path, setup_data={})
    await plugin.handle_async_init()
    await plugin.loaded_in_mass()
    assert plugin._dialogs_handler is None
    mass.webserver.register_dynamic_route.assert_not_called()


@pytest.mark.asyncio
async def test_replace_manifest_provider_updates_running_handler(tmp_path: Path) -> None:
    """A successful manifest action activates its snapshot immediately."""
    plugin, _mass = _plugin(tmp_path)
    await plugin.handle_async_init()
    await plugin.loaded_in_mass()
    replacement = SkillManifestProvider(plugin.mass)
    plugin.replace_manifest_provider(replacement)
    assert plugin._dialogs_handler is not None
    assert plugin._dialogs_handler._manifest_provider is replacement


# ---------------------------------------------------------------------------
# Options page
# ---------------------------------------------------------------------------


async def _keys(plugin: YandexAlicePlugin) -> list[str]:
    return [entry.key for entry in await plugin.get_config_entries()]


@pytest.mark.asyncio
async def test_options_page_for_signed_in_owner(tmp_path: Path) -> None:
    """A signed-in owner sees the account, the skill card and every skill action."""
    plugin, _mass = _plugin(tmp_path)
    keys = await _keys(plugin)
    for key in (
        "label_auth_status",
        "label_skill_registered",
        "label_webhook_url",
        CONF_ACTION_TEST_WEBHOOK,
        CONF_ACTION_OPEN_DEV_CONSOLE,
        CONF_ACTION_REFRESH_STATUS,
        CONF_ACTION_DELETE_SKILL,
        CONF_EXPOSED_PLAYERS,
        CONF_DIALOG_SKILL_OVERRIDE_PASTE,
    ):
        assert key in keys


@pytest.mark.asyncio
async def test_options_page_without_token_hides_account_actions(tmp_path: Path) -> None:
    """Without an x_token the page asks to sign in and hides the actions that need it."""
    plugin, _mass = _plugin(tmp_path, setup_data={**SETUP, CONF_AUTH_X_TOKEN: None})
    keys = await _keys(plugin)
    assert "label_auth_signed_out" in keys
    assert CONF_ACTION_REFRESH_STATUS not in keys
    assert CONF_ACTION_DELETE_SKILL not in keys
    assert CONF_ACTION_TEST_WEBHOOK in keys


@pytest.mark.asyncio
async def test_options_page_without_skill(tmp_path: Path) -> None:
    """Without a skill the page points to Reconfigure instead of the skill card."""
    plugin, _mass = _plugin(tmp_path, setup_data={**SETUP, CONF_DIALOG_SKILL_ID: ""})
    keys = await _keys(plugin)
    assert "label_skill_missing" in keys
    assert "label_skill_registered" not in keys


@pytest.mark.asyncio
async def test_options_page_when_borrowing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Borrowing names the linked Yandex Music instance."""
    monkeypatch.setattr(
        plugin_module, "list_yandex_music_instances", lambda _mass: [("ym-1", "Main")]
    )
    monkeypatch.setattr(plugin_module, "borrowed_x_token", lambda _mass, _inst: "x-borrowed")
    plugin, _mass = _plugin(
        tmp_path, setup_data={**SETUP, CONF_YM_INSTANCE: "ym-1", CONF_AUTH_X_TOKEN: None}
    )
    entries = await plugin.get_config_entries()
    borrowed = next(entry for entry in entries if entry.key == "label_auth_borrowed")
    assert borrowed.translation_params == ["Main"]
    assert CONF_ACTION_REFRESH_STATUS in [entry.key for entry in entries]


# ---------------------------------------------------------------------------
# Config actions
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_test_webhook_reports_probe_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Test webhook probes the stored base URL and secret."""
    probe = AsyncMock(return_value=(True, "Webhook reachable."))
    monkeypatch.setattr(plugin_module, "probe_webhook_reachability", probe)
    plugin, _mass = _plugin(tmp_path)
    result = await plugin.handle_config_action(CONF_ACTION_TEST_WEBHOOK)
    probe.assert_awaited_once_with("https://ma.example.org", "secret-1")
    assert result.message == "✅ Webhook reachable."  # type: ignore[union-attr]


@pytest.mark.asyncio
async def test_open_dev_console_opens_skill_page(tmp_path: Path) -> None:
    """Open developer console returns the skill's console URL."""
    plugin, _mass = _plugin(tmp_path)
    result = await plugin.handle_config_action(CONF_ACTION_OPEN_DEV_CONSOLE)
    assert result.open_url == "https://dialogs.yandex.ru/developer/skills/sk-1"  # type: ignore[union-attr]


@pytest.mark.asyncio
async def test_refresh_status_stores_new_status(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Refresh status fetches with the stored token and saves the result."""
    fetch = AsyncMock(return_value="in_moderation")
    monkeypatch.setattr(plugin_module, "fetch_skill_publication_status", fetch)
    plugin, _mass = _plugin(tmp_path)
    await plugin.handle_config_action(CONF_ACTION_REFRESH_STATUS)
    fetch.assert_awaited_once_with("x-own", "sk-1")
    assert plugin.get_setup_value(CONF_DIALOG_PUBLICATION_STATUS) == "in_moderation"


@pytest.mark.asyncio
async def test_delete_skill_clears_skill_data(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Delete skill removes it in Yandex and forgets the skill id."""
    delete = AsyncMock()
    monkeypatch.setattr(plugin_module, "delete_skill", delete)
    plugin, _mass = _plugin(tmp_path)
    await plugin.handle_config_action(CONF_ACTION_DELETE_SKILL)
    delete.assert_awaited_once_with("x-own", "sk-1")
    assert plugin.get_setup_value(CONF_DIALOG_SKILL_ID) == ""


@pytest.mark.asyncio
async def test_delete_skill_stops_the_webhook(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """After deleting the skill its webhook route no longer answers."""
    monkeypatch.setattr(plugin_module, "delete_skill", AsyncMock())
    plugin, mass = _plugin(tmp_path)
    unregister = MagicMock()
    mass.webserver.register_dynamic_route.return_value = unregister
    await plugin.handle_async_init()
    await plugin.loaded_in_mass()
    await plugin.handle_config_action(CONF_ACTION_DELETE_SKILL)
    unregister.assert_called_once()
    assert plugin._dialogs_handler is None


@pytest.mark.asyncio
async def test_action_without_token_raises_login_failed(tmp_path: Path) -> None:
    """An action that needs the account fails with LoginFailed when there is no token."""
    plugin, _mass = _plugin(tmp_path, setup_data={**SETUP, CONF_AUTH_X_TOKEN: None})
    with pytest.raises(LoginFailed):
        await plugin.handle_config_action(CONF_ACTION_DELETE_SKILL)


@pytest.mark.asyncio
async def test_import_manifest_uses_saved_paste_and_clears_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Import reads the saved paste field and empties it after a successful import."""
    manifest = MagicMock()
    manifest.import_from_paste.return_value = ManifestActionResult(
        "manifest_import_success", ("/x/skill.toml",), success=True
    )
    manifest.last_import_success = True
    monkeypatch.setattr(plugin_module, "SkillManifestProvider", lambda _mass: manifest)
    plugin, mass = _plugin(tmp_path, values={CONF_DIALOG_SKILL_OVERRIDE_PASTE: "[skill]"})
    result = await plugin.handle_config_action(CONF_ACTION_IMPORT_MANIFEST)
    manifest.import_from_paste.assert_called_once_with("[skill]")
    assert result.translation_key == "manifest_import_success"  # type: ignore[union-attr]
    stored = mass.config.get_raw_provider_config_value(INSTANCE, CONF_DIALOG_SKILL_OVERRIDE_PASTE)
    assert stored == ""


@pytest.mark.asyncio
async def test_unknown_action_is_unavailable(tmp_path: Path) -> None:
    """Actions the page no longer offers are rejected."""
    plugin, _mass = _plugin(tmp_path)
    with pytest.raises(ActionUnavailable):
        await plugin.handle_config_action("sign_in")
