"""Tests for the linked-only Ynison setup flow."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from unittest.mock import MagicMock

import pytest
from music_assistant_models.enums import FlowStepType
from ya_passport_auth import Credentials, QrSession, SecretStr

from music_assistant.models.setup_flow import AbortFlow, SetupFlowContext, SetupSession
from music_assistant.providers.yandex_ynison import setup_flow as yn_flow
from music_assistant.providers.yandex_ynison.constants import (
    CONF_MASS_PLAYER_ID,
    CONF_REMEMBER_SESSION,
    CONF_TOKEN,
    CONF_X_TOKEN,
    CONF_YM_INSTANCE,
    DEFAULT_DISPLAY_NAME,
    PLAYER_ID_AUTO,
)
from music_assistant.providers.yandex_ynison.setup_flow import run_setup

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ConfigEntry, ConfigValueType


class _SetupSession(SetupSession):
    """Small setup-session fake retaining the form and persisted result."""

    def __init__(
        self,
        providers: dict[str, dict[str, Any]],
        submitted: dict[str, ConfigValueType],
        *,
        values: dict[str, ConfigValueType] | None = None,
        setup_data: dict[str, ConfigValueType] | None = None,
    ) -> None:
        self._mass_mock = MagicMock()
        self.mass = self._mass_mock
        self._mass_mock.config.get.return_value = providers
        self._mass_mock.players.all_players.return_value = []
        self.context = SetupFlowContext(
            kind="setup",
            reason="user",
            domain="yandex_ynison",
            values=values or {},
            setup_data=setup_data or {},
        )
        self._submitted = submitted
        self.entries: list[ConfigEntry] = []
        self.form_kwargs: dict[str, Any] = {}
        self.shown_errors: list[dict[str, str] | None] = []
        self.finished_values: dict[str, ConfigValueType] | None = None

    async def form(
        self,
        entries: list[ConfigEntry],
        step_id: str = "user",
        errors: dict[str, str] | None = None,
        last_step: bool | None = None,
        expires_in: float | None = None,
        translation_params: list[str] | None = None,
    ) -> dict[str, ConfigValueType]:
        """Capture the presented form and return the configured submission."""
        self.entries = entries
        self.form_kwargs = {
            "step_id": step_id,
            "errors": errors,
            "last_step": last_step,
            "expires_in": expires_in,
            "translation_params": translation_params,
        }
        self.shown_errors.append(errors)
        return self._submitted

    async def finish(self, values: dict[str, ConfigValueType]) -> dict[str, str]:
        """Capture setup data as Music Assistant would persist it."""
        self.finished_values = values
        return {"instance_id": "ynison-test"}

    def set_players(self, players: list[Any]) -> None:
        """Configure the players returned by the Music Assistant fake."""
        self._mass_mock.players.all_players.return_value = players


def _entry(session: _SetupSession, key: str) -> Any:
    return next(entry for entry in session.entries if entry.key == key)


async def test_single_yandex_music_instance_is_preselected_and_persisted() -> None:
    """Removing the single-account default must not make simple setup ambiguous."""
    session = _SetupSession(
        {"ym-main": {"domain": "yandex_music", "name": "Primary"}},
        {
            CONF_YM_INSTANCE: "ym-main",
            CONF_MASS_PLAYER_ID: PLAYER_ID_AUTO,
            CONF_PUBLISH_NAME: DEFAULT_DISPLAY_NAME,
        },
    )

    await run_setup(session)

    source = _entry(session, CONF_YM_INSTANCE)
    assert source.default_value == "ym-main"
    assert source.value == "ym-main"
    assert [option.value for option in source.options] == ["ym-main"]
    assert session.form_kwargs["last_step"] is True
    assert session.finished_values == {
        CONF_YM_INSTANCE: "ym-main",
        CONF_MASS_PLAYER_ID: PLAYER_ID_AUTO,
        CONF_PUBLISH_NAME: DEFAULT_DISPLAY_NAME,
    }


async def test_multiple_accounts_without_valid_prefill_require_explicit_selection() -> None:
    """Defaulting to the first account must not silently switch a user's identity."""
    session = _SetupSession(
        {
            "ym-a": {"domain": "yandex_music", "name": "A"},
            "ym-b": {"domain": "yandex_music", "name": "B"},
        },
        {
            CONF_YM_INSTANCE: "ym-b",
            CONF_MASS_PLAYER_ID: PLAYER_ID_AUTO,
            CONF_PUBLISH_NAME: DEFAULT_DISPLAY_NAME,
        },
        setup_data={CONF_YM_INSTANCE: "__own__"},
    )

    await run_setup(session)

    source = _entry(session, CONF_YM_INSTANCE)
    assert source.default_value is None
    assert source.value is None
    assert [option.value for option in source.options] == ["ym-a", "ym-b"]
    assert session.finished_values is not None
    assert session.finished_values[CONF_YM_INSTANCE] == "ym-b"


async def test_reconfigure_preserves_valid_identity_and_nulls_legacy_secrets() -> None:
    """Leaving legacy values intact must not preserve a second credential owner."""
    setup_data: dict[str, ConfigValueType] = {
        CONF_YM_INSTANCE: "ym-main",
        CONF_MASS_PLAYER_ID: "living-room",
        CONF_PUBLISH_NAME: "Living room",
        "token": "old-music-token",
        "x_token": "old-x-token",
        "account_login": "alice",
        "remember_session": True,
    }
    session = _SetupSession(
        {"ym-main": {"domain": "yandex_music", "name": "Primary"}},
        {
            CONF_YM_INSTANCE: "ym-main",
            CONF_MASS_PLAYER_ID: "living-room",
            CONF_PUBLISH_NAME: "Living room",
        },
        setup_data=setup_data,
    )
    player = MagicMock()
    player.player_id = "living-room"
    player.display_name = "Living room"
    session.set_players([player])

    await run_setup(session)

    assert _entry(session, CONF_YM_INSTANCE).value == "ym-main"
    assert _entry(session, CONF_MASS_PLAYER_ID).value == "living-room"
    assert _entry(session, CONF_PUBLISH_NAME).value == "Living room"
    assert session.finished_values is not None
    for key in ("token", "x_token", "account_login", "remember_session"):
        assert session.finished_values[key] is None


async def test_new_setup_does_not_persist_legacy_auth_keys() -> None:
    """Always writing legacy nulls must not pollute setup data for new instances."""
    session = _SetupSession(
        {"ym-main": {"domain": "yandex_music", "name": "Primary"}},
        {
            CONF_YM_INSTANCE: "ym-main",
            CONF_MASS_PLAYER_ID: PLAYER_ID_AUTO,
            CONF_PUBLISH_NAME: DEFAULT_DISPLAY_NAME,
        },
    )

    await run_setup(session)

    assert session.finished_values is not None
    assert (
        not {
            "token",
            "x_token",
            "account_login",
            "remember_session",
        }
        & session.finished_values.keys()
    )


async def test_no_yandex_music_instance_aborts_as_missing_dependency() -> None:
    """Rendering an empty account picker must not create an unusable Ynison instance."""
    session = _SetupSession(
        {},
        {
            CONF_YM_INSTANCE: "unused",
            CONF_MASS_PLAYER_ID: PLAYER_ID_AUTO,
            CONF_PUBLISH_NAME: DEFAULT_DISPLAY_NAME,
        },
    )

    with pytest.raises(AbortFlow) as err:
        await run_setup(session)

    assert err.value.reason == "missing_dependency"


async def test_disabled_yandex_music_instances_are_not_linkable() -> None:
    """A disabled credential owner cannot satisfy the runtime dependency."""
    session = _SetupSession(
        {
            "ym-disabled": {
                "domain": "yandex_music",
                "name": "Disabled",
                "enabled": False,
            },
            "ym-enabled": {
                "domain": "yandex_music",
                "name": "Enabled",
                "enabled": True,
            },
        },
        {
            CONF_YM_INSTANCE: "ym-enabled",
            CONF_MASS_PLAYER_ID: PLAYER_ID_AUTO,
            CONF_PUBLISH_NAME: DEFAULT_DISPLAY_NAME,
        },
    )

    await run_setup(session)

    source = _entry(session, CONF_YM_INSTANCE)
    assert [option.value for option in source.options] == ["ym-enabled"]
    assert source.value == "ym-enabled"


async def test_only_disabled_yandex_music_instances_abort_setup() -> None:
    """Setup must stop when every possible credential owner is disabled."""
    session = _SetupSession(
        {"ym-disabled": {"domain": "yandex_music", "enabled": False}},
        {},
    )

    with pytest.raises(AbortFlow) as err:
        await run_setup(session)

    assert err.value.reason == "missing_dependency"


async def test_finish_error_reopens_form_with_preserved_values() -> None:
    """Letting a load failure escape must not discard the user's linked-account choice."""

    class RetrySession(_SetupSession):
        attempts = 0

        async def finish(self, values: dict[str, ConfigValueType]) -> dict[str, str]:
            self.attempts += 1
            if self.attempts == 1:
                raise SetupFlowError("provider rejected setup", translation_key="invalid_auth")
            return await super().finish(values)

    session = RetrySession(
        {"ym-main": {"domain": "yandex_music", "name": "Primary"}},
        {
            CONF_YM_INSTANCE: "ym-main",
            CONF_MASS_PLAYER_ID: PLAYER_ID_AUTO,
            CONF_PUBLISH_NAME: "Kitchen",
        },
    )

    await run_setup(session)

    assert session.attempts == 2
    assert session.shown_errors == [None, {"base": "invalid_auth"}]
    assert session.finished_values is not None
    assert session.finished_values[CONF_PUBLISH_NAME] == "Kitchen"


async def test_legacy_player_and_display_name_are_preserved() -> None:
    """Removing the old aliases must not reset non-auth identity during reconfigure."""
    session = _SetupSession(
        {"ym-main": {"domain": "yandex_music", "name": "Primary"}},
        {
            CONF_YM_INSTANCE: "ym-main",
            CONF_MASS_PLAYER_ID: "kitchen",
            CONF_PUBLISH_NAME: "Old kitchen",
        },
        values={"player": "kitchen", "display_name": "Old kitchen"},
    )
    player = MagicMock()
    player.player_id = "kitchen"
    player.display_name = "Kitchen"
    session.set_players([player])

    await run_setup(session)

def _published_steps(mass: mock.Mock) -> list[Any]:
    """Return the flow steps pushed through mass.signal_event, in order."""
    return [call.kwargs["data"] for call in mass.signal_event.call_args_list]


async def _wait_for(predicate: Any, timeout: float = 5.0) -> Any:
    """Wait until the predicate returns truthy (or fail the test)."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if result := predicate():
            return result
        await asyncio.sleep(0.01)
    raise AssertionError("condition not met within timeout")


async def _await_user_form(session: SetupSession) -> None:
    """Wait until the user form is presented."""
    await _wait_for(lambda: session.current_step and session.current_step.type == FlowStepType.FORM)


async def test_borrow_mode_finishes_with_instance_only() -> None:
    """Selecting a linked Yandex Music instance persists only that instance id."""
    collected: dict[str, Any] = {}

    async def finish(_s: SetupSession, values: dict[str, Any]) -> dict[str, str]:
        collected.update(values)
        return {"instance_id": "yandex_ynison--1"}

    session, _mass = _make_session(
        finish, providers={"ym-a": {"domain": "yandex_music", "name": "Main"}}
    )
    task = asyncio.create_task(yn_flow.run_setup(session))
    await _await_user_form(session)
    session.handle_submit(
        {
            CONF_YM_INSTANCE: "ym-a",
            CONF_REMEMBER_SESSION: True,
            CONF_MASS_PLAYER_ID: "kitchen",
        }
    )
    await _wait_for(lambda: session.finished)
    await task

    assert collected == {
        CONF_YM_INSTANCE: "ym-a",
        CONF_MASS_PLAYER_ID: "kitchen",
    }


async def test_own_mode_qr_persists_tokens_and_login() -> None:
    """Own-mode QR login persists music token, x_token (remember on) and display login."""
    creds = Credentials(x_token=SecretStr("XT"), music_token=SecretStr("MT"), display_login="alice")
    collected: dict[str, Any] = {}

    async def finish(_s: SetupSession, values: dict[str, Any]) -> dict[str, str]:
        collected.update(values)
        return {"instance_id": "yandex_ynison--1"}

    session, mass = _make_session(finish)
    client = _FakeClient(creds)
    with mock.patch.object(yn_flow, "PassportClient") as pc:
        pc.create.return_value = _async_cm(client)
        task = asyncio.create_task(yn_flow.run_setup(session))
        await _await_user_form(session)
        session.handle_submit(
            {
                CONF_YM_INSTANCE: YM_INSTANCE_OWN,
                CONF_REMEMBER_SESSION: True,
                CONF_MASS_PLAYER_ID: "kitchen",
            }
        )
        await _wait_for(lambda: session.finished)
        await task

    assert collected == {
        CONF_YM_INSTANCE: YM_INSTANCE_OWN,
        CONF_TOKEN: "MT",
        CONF_X_TOKEN: "XT",
        CONF_ACCOUNT_LOGIN: "alice",
        CONF_MASS_PLAYER_ID: "kitchen",
    }
    scan_steps = [s for s in _published_steps(mass) if s.step_id == "scan_qr"]
    assert scan_steps
    assert all(s.image and s.image.startswith("data:image/svg+xml") for s in scan_steps)


async def test_own_mode_without_remember_clears_x_token() -> None:
    """Own-mode QR login with remember off stores the music token but no x_token."""
    creds = Credentials(x_token=SecretStr("XT"), music_token=SecretStr("MT"), display_login="bob")
    collected: dict[str, Any] = {}

    async def finish(_s: SetupSession, values: dict[str, Any]) -> dict[str, str]:
        collected.update(values)
        return {"instance_id": "yandex_ynison--1"}

    session, _mass = _make_session(finish)
    client = _FakeClient(creds)
    with mock.patch.object(yn_flow, "PassportClient") as pc:
        pc.create.return_value = _async_cm(client)
        task = asyncio.create_task(yn_flow.run_setup(session))
        await _await_user_form(session)
        session.handle_submit(
            {
                CONF_YM_INSTANCE: YM_INSTANCE_OWN,
                CONF_REMEMBER_SESSION: False,
                CONF_MASS_PLAYER_ID: "kitchen",
            }
        )
        await _wait_for(lambda: session.finished)
        await task

    assert collected[CONF_TOKEN] == "MT"
    assert collected[CONF_X_TOKEN] is None


async def test_aborts_without_players() -> None:
    """With no players registered the flow aborts with the no_players reason."""

    async def finish(_s: SetupSession, _values: dict[str, Any]) -> dict[str, str]:
        raise AssertionError("finish must not be reached")

    session, mass = _make_session(finish)
    mass.players.all_players.return_value = []

    with pytest.raises(AbortFlow) as excinfo:
        await yn_flow.run_setup(session)
    assert excinfo.value.reason == "no_players"
