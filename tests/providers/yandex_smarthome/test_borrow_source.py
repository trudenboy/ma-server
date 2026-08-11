"""Borrowed Yandex Music credentials in the native setup flow."""

from __future__ import annotations

import dataclasses
from types import SimpleNamespace
from typing import Any
from unittest import mock

import pytest
from music_assistant_models.enums import ProviderType
from ya_dialogs_api import SkillCreationState, load_artifacts
from ya_passport_auth.ma import BORROW_SOURCE_OWN

from music_assistant.models.setup_flow import SetupFlowError
from music_assistant.providers.yandex_smarthome.constants import (
    CONF_AUTH_X_TOKEN,
    CONF_CLOUD_INSTANCE_ID,
    CONF_CONNECTION_TYPE,
    CONF_YM_INSTANCE,
)
from music_assistant.providers.yandex_smarthome.setup_flow import (
    _collect_user,
    _provision_skill,
    _user_entries,
)

_SETUP_FLOW = "music_assistant.providers.yandex_smarthome.setup_flow"


def _make_mass(instances: dict[str, str] | None = None) -> mock.MagicMock:
    """Build a Music Assistant stand-in with optional Yandex Music instances."""
    mass = mock.MagicMock()
    mass.config.get.return_value = {
        instance_id: {"domain": "yandex_music", "name": name}
        for instance_id, name in (instances or {}).items()
    }
    owner = mock.MagicMock()
    owner.domain = "yandex_music"
    owner.type = ProviderType.MUSIC
    owner.config.get_value = lambda key: {"x_token": "test-x-ym"}.get(key)
    mass.get_provider.return_value = owner
    mass.webserver.base_url = "http://ma.local:8095"
    return mass


def _fake_session(mass: mock.MagicMock, *, setup_data: dict[str, Any] | None = None) -> Any:
    """Build a setup-session stand-in whose progress helper awaits inline."""
    session = mock.MagicMock()
    session.mass = mass
    session.flow_id = "flow-id"
    session.context = SimpleNamespace(setup_data=dict(setup_data or {}))

    async def _progress_until(awaitable: Any, **_: Any) -> Any:
        return await awaitable

    session.progress_until = mock.AsyncMock(side_effect=_progress_until)
    session.progress = mock.MagicMock()
    return session


def _done_artifacts(skill_id: str = "skill-xyz") -> Any:
    """Create completed skill artifacts for provisioning tests."""
    return dataclasses.replace(
        load_artifacts(None), state=SkillCreationState.DONE, skill_id=skill_id
    )


class TestAccountSourceDropdown:
    """Account-source choices on the first setup form."""

    def test_dropdown_lists_instances_and_own(self) -> None:
        """List every Yandex Music instance plus the own-account choice."""
        entries = _user_entries(
            "cloud", BORROW_SOURCE_OWN, "", [("ym-a", "Main"), ("ym-b", "Second")]
        )
        source = {entry.key: entry for entry in entries}[CONF_YM_INSTANCE]

        assert [option.value for option in source.options] == [
            "ym-a",
            "ym-b",
            BORROW_SOURCE_OWN,
        ]

    async def test_stale_selection_normalizes_to_own(self) -> None:
        """A removed linked instance resets to this provider's own account."""
        mass = _make_mass({"ym-a": "Main"})
        session = _fake_session(mass, setup_data={CONF_YM_INSTANCE: "removed"})
        captured: dict[str, Any] = {}

        async def _form(entries: Any, **_: Any) -> dict[str, Any]:
            captured["entries"] = entries
            return {
                CONF_CONNECTION_TYPE: "cloud",
                CONF_YM_INSTANCE: BORROW_SOURCE_OWN,
            }

        session.form = mock.AsyncMock(side_effect=_form)
        await _collect_user(session, dict(session.context.setup_data))

        source = {entry.key: entry for entry in captured["entries"]}[CONF_YM_INSTANCE]
        assert source.value == BORROW_SOURCE_OWN


class TestProvisionSkillBorrow:
    """Skill provisioning with credentials owned by Yandex Music."""

    async def test_borrow_uses_ym_token_without_persistence(self) -> None:
        """Use the borrowed token without copying it into setup data."""
        mass = _make_mass({"ym-a": "Main"})
        session = _fake_session(mass)
        collected: dict[str, Any] = {CONF_CLOUD_INSTANCE_ID: "ci-1"}
        with (
            mock.patch(f"{_SETUP_FLOW}.make_authenticator") as make_auth,
            mock.patch(f"{_SETUP_FLOW}.auto_create_skill", new_callable=mock.AsyncMock) as create,
            mock.patch(f"{_SETUP_FLOW}.load_default_logo_bytes", return_value=b"logo"),
        ):
            create.return_value = _done_artifacts()
            skill_id = await _provision_skill(
                session,
                collected,
                connection_type="cloud_plus",
                skill_name="Test",
                ym_instance="ym-a",
            )

        assert skill_id == "skill-xyz"
        assert make_auth.call_args.kwargs == {"cached_x_token": "test-x-ym"}
        assert CONF_AUTH_X_TOKEN not in collected

    async def test_borrow_source_error_raises_setup_flow_error(self) -> None:
        """A missing linked instance fails without starting another login."""
        mass = _make_mass({"ym-a": "Main"})
        mass.get_provider.return_value = None
        session = _fake_session(mass)
        with (
            mock.patch(f"{_SETUP_FLOW}.make_authenticator") as make_auth,
            mock.patch(f"{_SETUP_FLOW}._device_login") as device_login,
            pytest.raises(SetupFlowError, match="not loaded"),
        ):
            await _provision_skill(
                session,
                {CONF_CLOUD_INSTANCE_ID: "ci-1"},
                connection_type="cloud_plus",
                skill_name="Test",
                ym_instance="ym-a",
            )
        make_auth.assert_not_called()
        device_login.assert_not_called()

    async def test_failed_pipeline_surfaces_last_error(self) -> None:
        """A provisioning pipeline failure becomes an actionable setup error."""
        mass = _make_mass({"ym-a": "Main"})
        session = _fake_session(mass)
        failed = dataclasses.replace(
            load_artifacts(None),
            state=SkillCreationState.FAILED,
            last_error="dialogs.yandex.ru rejected the request",
        )
        with (
            mock.patch(f"{_SETUP_FLOW}.make_authenticator"),
            mock.patch(f"{_SETUP_FLOW}.auto_create_skill", new_callable=mock.AsyncMock) as create,
            mock.patch(f"{_SETUP_FLOW}.load_default_logo_bytes", return_value=b"logo"),
        ):
            create.return_value = failed
            with pytest.raises(SetupFlowError, match="rejected the request"):
                await _provision_skill(
                    session,
                    {CONF_CLOUD_INSTANCE_ID: "ci-1"},
                    connection_type="cloud_plus",
                    skill_name="Test",
                    ym_instance="ym-a",
                )
