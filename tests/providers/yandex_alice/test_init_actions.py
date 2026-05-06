# ruff: noqa: PLC0415
"""Integration tests for provider/__init__.get_config_entries — action dispatcher.

Mocks the orchestrator entry points (``run_auto_create_step``,
``run_auto_update``) so we test the dispatcher's ``values`` rehydration,
re-create / cancel reset semantics, and the entries it places into the
returned tuple — not the orchestrator internals (those are tested elsewhere).
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from ya_dialogs_api import (
    SkillCreationArtifacts,
    SkillCreationState,
    dump_artifacts,
)

from music_assistant.providers import yandex_alice
from music_assistant.providers.yandex_alice import get_config_entries
from music_assistant.providers.yandex_alice.auto_create import (
    AutoCreateOutcome,
    LocalAutoCreateStage,
)
from music_assistant.providers.yandex_alice.auto_update import AutoUpdateOutcome
from music_assistant.providers.yandex_alice.constants import (
    CONF_ACTION_AUTO_CREATE_DIALOG,
    CONF_ACTION_CANCEL_DIALOG_SKILL_FLOW,
    CONF_ACTION_RENAME_DIALOG_SKILL,
    CONF_AUTH_X_TOKEN,
    CONF_DIALOG_AUTO_CREATE_ARTIFACTS,
    CONF_DIALOG_AUTO_CREATE_DEVICE_SESSION,
    CONF_DIALOG_SKILL_ID,
    CONF_DIALOG_SKILL_NAME,
    CONF_EXTERNAL_BASE_URL,
    CONF_INSTANCE_NAME,
)


def _make_mass() -> MagicMock:
    """Build a MagicMock MA with empty player + playlist enumeration."""
    mass = MagicMock()
    mass.players.all_players = MagicMock(return_value=[])
    return mass


@pytest.fixture(autouse=True)
def _stub_playlists(monkeypatch: pytest.MonkeyPatch) -> None:
    """Empty playlist options for all tests in this module."""
    monkeypatch.setattr(yandex_alice, "fetch_playlist_options", AsyncMock(return_value=[]))


def _entries_by_key(entries: tuple[Any, ...]) -> dict[str, Any]:
    """Index entries by their ``key`` for easy lookup."""
    return {e.key: e for e in entries}


# ---------------------------------------------------------------------------
# action=None: default form
# ---------------------------------------------------------------------------


class TestDefaultForm:
    """No action: form has both auto-create button and (conditionally) rename."""

    @pytest.mark.asyncio
    async def test_no_action_renders_auto_create_button(self) -> None:
        """Auto-create ACTION entry is always present."""
        entries = await get_config_entries(_make_mass(), values={})
        keys = _entries_by_key(entries)
        assert CONF_ACTION_AUTO_CREATE_DIALOG in keys

    @pytest.mark.asyncio
    async def test_rename_hidden_without_skill_id_or_token(self) -> None:
        """Rename ACTION is suppressed when skill_id or x_token is missing."""
        entries = await get_config_entries(_make_mass(), values={})
        keys = _entries_by_key(entries)
        assert CONF_ACTION_RENAME_DIALOG_SKILL not in keys

    @pytest.mark.asyncio
    async def test_rename_visible_when_skill_id_and_token_present(self) -> None:
        """Both skill_id and cached x_token populate the form → rename shows up."""
        artifacts = SkillCreationArtifacts(
            state=SkillCreationState.DONE,
            skill_id="sk-1",
            last_known_name="My Skill",
        )
        values: dict[str, Any] = {
            CONF_AUTH_X_TOKEN: "tok",
            CONF_DIALOG_AUTO_CREATE_ARTIFACTS: dump_artifacts(artifacts),
        }
        entries = await get_config_entries(_make_mass(), values=values)
        keys = _entries_by_key(entries)
        assert CONF_ACTION_RENAME_DIALOG_SKILL in keys


# ---------------------------------------------------------------------------
# action = CONF_ACTION_AUTO_CREATE_DIALOG
# ---------------------------------------------------------------------------


class TestAutoCreateAction:
    """auto-create dispatch: invokes run_auto_create_step with derived inputs."""

    @pytest.mark.asyncio
    async def test_invokes_run_auto_create_step(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Click → run_auto_create_step is awaited once with skill_name + backend_uri."""
        outcome = AutoCreateOutcome(
            artifacts=SkillCreationArtifacts(),
            device_session_blob='{"user_code": "X"}',
            x_token=None,
            user_code="X",
            verification_url="https://ya.ru/device",
            user_message="started",
            stage=LocalAutoCreateStage.DEVICE_FLOW_STARTED,
        )
        step_mock = AsyncMock(return_value=outcome)
        monkeypatch.setattr(yandex_alice, "run_auto_create_step", step_mock)

        values: dict[str, Any] = {
            CONF_INSTANCE_NAME: "Music Assistant",
            CONF_DIALOG_SKILL_NAME: "MA Test",
            CONF_EXTERNAL_BASE_URL: "https://ma.example.com",
        }
        await get_config_entries(
            _make_mass(),
            action=CONF_ACTION_AUTO_CREATE_DIALOG,
            values=values,
        )

        step_mock.assert_awaited_once()
        assert step_mock.await_args is not None
        kwargs = step_mock.await_args.kwargs
        assert kwargs["skill_name"] == "MA Test"
        assert kwargs["backend_uri"].startswith(
            "https://ma.example.com/api/yandex_dialogs/webhook/"
        )

    @pytest.mark.asyncio
    async def test_https_required_short_circuits(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """http:// base URL → FAILED before run_auto_create_step is called."""
        step_mock = AsyncMock()
        monkeypatch.setattr(yandex_alice, "run_auto_create_step", step_mock)

        values: dict[str, Any] = {
            CONF_INSTANCE_NAME: "MA",
            CONF_EXTERNAL_BASE_URL: "http://insecure.example.com",
        }
        await get_config_entries(
            _make_mass(),
            action=CONF_ACTION_AUTO_CREATE_DIALOG,
            values=values,
        )

        step_mock.assert_not_awaited()
        # The dispatcher writes a FAILED artifacts blob into values
        from ya_dialogs_api import load_artifacts

        artifacts = load_artifacts(str(values.get(CONF_DIALOG_AUTO_CREATE_ARTIFACTS) or "") or None)
        assert artifacts.state == SkillCreationState.FAILED
        assert "HTTPS" in (artifacts.last_error or "")

    @pytest.mark.asyncio
    async def test_re_click_on_done_resets_artifacts(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """If artifacts.state was DONE, dispatcher resets to NONE before stepping."""
        captured_artifacts: list[SkillCreationArtifacts] = []

        async def _capture(**kwargs: Any) -> AutoCreateOutcome:
            captured_artifacts.append(kwargs["artifacts"])
            return AutoCreateOutcome(
                artifacts=SkillCreationArtifacts(),
                device_session_blob=None,
                x_token=None,
                user_code=None,
                verification_url=None,
                user_message="restart",
                stage=LocalAutoCreateStage.IDLE,
            )

        monkeypatch.setattr(yandex_alice, "run_auto_create_step", _capture)

        done = SkillCreationArtifacts(
            state=SkillCreationState.DONE,
            skill_id="sk-old",
            last_known_name="Old",
        )
        values: dict[str, Any] = {
            CONF_DIALOG_AUTO_CREATE_ARTIFACTS: dump_artifacts(done),
            CONF_EXTERNAL_BASE_URL: "https://ma.example.com",
        }
        await get_config_entries(
            _make_mass(),
            action=CONF_ACTION_AUTO_CREATE_DIALOG,
            values=values,
        )
        assert len(captured_artifacts) == 1
        # The dispatcher reset before stepping — old skill_id is gone
        assert captured_artifacts[0].state == SkillCreationState.NONE
        assert captured_artifacts[0].skill_id is None

    @pytest.mark.asyncio
    async def test_writes_skill_id_on_done(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Successful pipeline → CONF_DIALOG_SKILL_ID auto-populated in values."""
        outcome = AutoCreateOutcome(
            artifacts=SkillCreationArtifacts(
                state=SkillCreationState.DONE,
                skill_id="sk-new-uuid",
                last_known_name="MA",
            ),
            device_session_blob=None,
            x_token="fresh",
            user_code=None,
            verification_url=None,
            user_message="✅",
            stage=LocalAutoCreateStage.DONE,
        )
        monkeypatch.setattr(yandex_alice, "run_auto_create_step", AsyncMock(return_value=outcome))

        values: dict[str, Any] = {CONF_EXTERNAL_BASE_URL: "https://ma.example.com"}
        await get_config_entries(
            _make_mass(),
            action=CONF_ACTION_AUTO_CREATE_DIALOG,
            values=values,
        )
        assert values[CONF_DIALOG_SKILL_ID] == "sk-new-uuid"
        assert values[CONF_AUTH_X_TOKEN] == "fresh"

    @pytest.mark.asyncio
    async def test_backup_restore_pre_sets_app_created(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """skill_id in values + artifacts NONE → pre-set to APP_CREATED to skip create_app."""
        captured_artifacts: list[SkillCreationArtifacts] = []

        async def _capture(**kwargs: Any) -> AutoCreateOutcome:
            captured_artifacts.append(kwargs["artifacts"])
            return AutoCreateOutcome(
                artifacts=kwargs["artifacts"],
                device_session_blob=None,
                x_token=None,
                user_code=None,
                verification_url=None,
                user_message="stub",
                stage=LocalAutoCreateStage.PIPELINE_RUNNING,
            )

        monkeypatch.setattr(yandex_alice, "run_auto_create_step", _capture)

        # Empty artifacts but skill_id present (config restored from backup)
        values: dict[str, Any] = {
            CONF_DIALOG_SKILL_ID: "sk-existing-uuid",
            CONF_EXTERNAL_BASE_URL: "https://ma.example.com",
            CONF_AUTH_X_TOKEN: "tok",
        }
        await get_config_entries(
            _make_mass(),
            action=CONF_ACTION_AUTO_CREATE_DIALOG,
            values=values,
        )

        assert captured_artifacts[0].state == SkillCreationState.APP_CREATED
        assert captured_artifacts[0].skill_id == "sk-existing-uuid"


# ---------------------------------------------------------------------------
# action = CONF_ACTION_RENAME_DIALOG_SKILL
# ---------------------------------------------------------------------------


class TestRenameAction:
    """Rename dispatch: invokes run_auto_update with skill_name + cached token."""

    @pytest.mark.asyncio
    async def test_invokes_run_auto_update(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Click → run_auto_update awaited with skill_name + backend_uri + cached_x_token."""
        result = AutoUpdateOutcome(
            artifacts=SkillCreationArtifacts(
                state=SkillCreationState.DONE,
                skill_id="sk-1",
                last_known_name="New Name",
            ),
            x_token=None,
            user_message="✅ обновлён",
        )
        update_mock = AsyncMock(return_value=result)
        monkeypatch.setattr(yandex_alice, "run_auto_update", update_mock)

        values: dict[str, Any] = {
            CONF_DIALOG_SKILL_NAME: "New Name",
            CONF_EXTERNAL_BASE_URL: "https://ma.example.com",
            CONF_AUTH_X_TOKEN: "tok",
            CONF_DIALOG_AUTO_CREATE_ARTIFACTS: dump_artifacts(
                SkillCreationArtifacts(
                    state=SkillCreationState.DONE,
                    skill_id="sk-1",
                    last_known_name="Old Name",
                )
            ),
        }
        await get_config_entries(
            _make_mass(),
            action=CONF_ACTION_RENAME_DIALOG_SKILL,
            values=values,
        )

        update_mock.assert_awaited_once()
        assert update_mock.await_args is not None
        kwargs = update_mock.await_args.kwargs
        assert kwargs["skill_name"] == "New Name"
        assert kwargs["cached_x_token"] == "tok"

    @pytest.mark.asyncio
    async def test_token_cleared_on_auth_failure(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """run_auto_update returns x_token='' → values clears CONF_AUTH_X_TOKEN."""
        result = AutoUpdateOutcome(
            artifacts=SkillCreationArtifacts(
                state=SkillCreationState.FAILED,
                skill_id="sk-1",
                last_error="истёк",
            ),
            x_token="",
            user_message="auth expired",
        )
        monkeypatch.setattr(yandex_alice, "run_auto_update", AsyncMock(return_value=result))

        values: dict[str, Any] = {
            CONF_AUTH_X_TOKEN: "stale",
            CONF_EXTERNAL_BASE_URL: "https://ma.example.com",
            CONF_DIALOG_AUTO_CREATE_ARTIFACTS: dump_artifacts(
                SkillCreationArtifacts(state=SkillCreationState.DONE, skill_id="sk-1")
            ),
        }
        await get_config_entries(
            _make_mass(),
            action=CONF_ACTION_RENAME_DIALOG_SKILL,
            values=values,
        )
        assert values[CONF_AUTH_X_TOKEN] == ""


# ---------------------------------------------------------------------------
# action = CONF_ACTION_CANCEL_DIALOG_SKILL_FLOW
# ---------------------------------------------------------------------------


class TestCancelAction:
    """Cancel: drop pending session + reset artifacts; keep cached x_token."""

    @pytest.mark.asyncio
    async def test_resets_artifacts_and_session(self) -> None:
        """Cancel clears CONF_DIALOG_AUTO_CREATE_DEVICE_SESSION + resets artifacts."""
        values: dict[str, Any] = {
            CONF_DIALOG_AUTO_CREATE_DEVICE_SESSION: '{"user_code": "X"}',
            CONF_DIALOG_AUTO_CREATE_ARTIFACTS: dump_artifacts(
                SkillCreationArtifacts(
                    state=SkillCreationState.APP_CREATED,
                    skill_id="sk-orphan",
                )
            ),
            CONF_AUTH_X_TOKEN: "preserve-me",
            CONF_EXTERNAL_BASE_URL: "https://ma.example.com",
        }
        await get_config_entries(
            _make_mass(),
            action=CONF_ACTION_CANCEL_DIALOG_SKILL_FLOW,
            values=values,
        )

        from ya_dialogs_api import load_artifacts

        # Artifacts reset to NONE
        rehydrated = load_artifacts(str(values[CONF_DIALOG_AUTO_CREATE_ARTIFACTS]))
        assert rehydrated.state == SkillCreationState.NONE
        assert rehydrated.skill_id is None
        # Session dropped
        assert values[CONF_DIALOG_AUTO_CREATE_DEVICE_SESSION] == ""
        # Token preserved
        assert values[CONF_AUTH_X_TOKEN] == "preserve-me"


# ---------------------------------------------------------------------------
# Code-review fixes — targeted regression coverage
# ---------------------------------------------------------------------------


class TestStableWebhookSecret:
    """Webhook secret must NOT regenerate between action clicks.

    Otherwise auto-create would register a webhook URL containing a secret
    that the next render replaces with a different one — orphaning the
    Yandex-side webhook against MA's eventual saved secret.
    """

    @pytest.mark.asyncio
    async def test_secret_reused_across_action_clicks(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Two consecutive clicks see the same backend_uri/secret (no regen)."""
        captured_uris: list[str] = []

        async def _capture(**kwargs: Any) -> AutoCreateOutcome:
            captured_uris.append(kwargs["backend_uri"])
            return AutoCreateOutcome(
                artifacts=SkillCreationArtifacts(),
                device_session_blob=None,
                x_token=None,
                user_code=None,
                verification_url=None,
                user_message="ok",
                stage=LocalAutoCreateStage.IDLE,
            )

        monkeypatch.setattr(yandex_alice, "run_auto_create_step", _capture)

        # First click: no secret in values → dispatcher generates + writes back.
        values: dict[str, Any] = {CONF_EXTERNAL_BASE_URL: "https://ma.example.com"}
        await get_config_entries(
            _make_mass(),
            action=CONF_ACTION_AUTO_CREATE_DIALOG,
            values=values,
        )

        # The dispatcher must have stabilised the secret in values
        # so subsequent renders see the same one.
        first_secret = str(values.get("dialog_webhook_secret") or "")
        assert first_secret

        # Second click — must reuse the same secret in backend_uri.
        await get_config_entries(
            _make_mass(),
            action=CONF_ACTION_AUTO_CREATE_DIALOG,
            values=values,
        )

        assert len(captured_uris) == 2
        assert captured_uris[0] == captured_uris[1]
        assert first_secret in captured_uris[0]


class TestDeriveStageRespectsCachedToken:
    """Intermediate artifact state without cached x_token → IDLE, not Resume.

    Otherwise the button label says "Resume" but the next click actually
    starts a fresh Device Flow — confusing UX.
    """

    @pytest.mark.asyncio
    async def test_intermediate_state_without_token_renders_create_label(self) -> None:
        """artifacts=APP_CREATED + no x_token → auto-create button says 'Create skill'."""
        artifacts = SkillCreationArtifacts(
            state=SkillCreationState.APP_CREATED,
            skill_id="sk-partial",
        )
        values: dict[str, Any] = {
            CONF_DIALOG_AUTO_CREATE_ARTIFACTS: dump_artifacts(artifacts),
            # No CONF_AUTH_X_TOKEN → next click will hit Device Flow
        }
        entries = await get_config_entries(_make_mass(), values=values)
        keys = _entries_by_key(entries)
        action_entry = keys[CONF_ACTION_AUTO_CREATE_DIALOG]
        assert action_entry.action_label == "Create skill"

    @pytest.mark.asyncio
    async def test_intermediate_state_with_token_renders_resume_label(self) -> None:
        """artifacts=APP_CREATED + cached x_token → button says 'Resume'."""
        artifacts = SkillCreationArtifacts(
            state=SkillCreationState.APP_CREATED,
            skill_id="sk-partial",
        )
        values: dict[str, Any] = {
            CONF_DIALOG_AUTO_CREATE_ARTIFACTS: dump_artifacts(artifacts),
            CONF_AUTH_X_TOKEN: "tok",
        }
        entries = await get_config_entries(_make_mass(), values=values)
        keys = _entries_by_key(entries)
        assert keys[CONF_ACTION_AUTO_CREATE_DIALOG].action_label == "Resume"


class TestDeviceFlowStartedHintOnReload:
    """LABEL re-shows user_code + URL after a form reload mid-Device-Flow."""

    @pytest.mark.asyncio
    async def test_label_renders_user_code_from_persisted_session(self) -> None:
        """device_session_blob in values → status LABEL shows the code + URL."""
        import json

        device_session = json.dumps(
            {
                "device_code": "secret",
                "user_code": "WXYZ-1234",
                "verification_url": "https://ya.ru/device",
                "expires_in": 600,
                "interval": 5,
                "expires_at_epoch": 9999999999.0,
            }
        )
        values: dict[str, Any] = {CONF_DIALOG_AUTO_CREATE_DEVICE_SESSION: device_session}
        entries = await get_config_entries(_make_mass(), values=values)
        keys = _entries_by_key(entries)

        # Status LABEL is rendered with the code + URL inline.
        assert "label_auto_create_status" in keys
        status_label = keys["label_auto_create_status"].label
        assert "WXYZ-1234" in status_label
        assert "ya.ru/device" in status_label
