"""Tests for provider/auto_create.py — self-resuming Device Flow + skill pipeline.

We mock two external dependencies:

- ``provider.auth_session.passport_client_session`` — the async context manager
  that yields a PassportClient. Tests inject a fake PassportClient with the
  exact ``start_device_login`` / ``poll_device_until_confirmed`` /
  ``refresh_passport_cookies`` shape needed for the case under test.
- ``provider.auto_create.auto_create_skill`` — the ya-dialogs-api orchestrator.
  Tests inject the desired ``SkillCreationArtifacts`` outcome.
"""

from __future__ import annotations

import time
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from ya_dialogs_api import SkillCreationArtifacts, SkillCreationState
from ya_passport_auth import DeviceCodeSession, SecretStr
from ya_passport_auth.exceptions import (
    DeviceCodeTimeoutError,
    InvalidCredentialsError,
)

from music_assistant.providers.yandex_alice import auto_create
from music_assistant.providers.yandex_alice.auto_create import (
    LocalAutoCreateStage,
    deserialize_device_session,
    run_auto_create_step,
    serialize_device_session,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------


def _make_device_code_session(
    *,
    user_code: str = "ABCD-1234",
    device_code: str = "device-code-secret",
    expires_in: int = 600,
    interval: int = 5,
) -> DeviceCodeSession:
    """Build a DeviceCodeSession with sensible test defaults."""
    return DeviceCodeSession(
        device_code=SecretStr(device_code),
        user_code=user_code,
        verification_url="https://ya.ru/device",
        expires_in=expires_in,
        interval=interval,
    )


def _patch_passport_client(
    monkeypatch: pytest.MonkeyPatch,
    *,
    start_session: DeviceCodeSession | Exception | None = None,
    poll_outcome: Any = None,
) -> MagicMock:
    """Install a fake passport_client_session() yielding a configured client.

    *start_session*: result (or Exception) of ``start_device_login``.
    *poll_outcome*: tuple ``(credentials, refresh_side_effect)`` or Exception.
        ``credentials`` becomes the return of ``poll_device_until_confirmed``;
        ``refresh_side_effect`` is set on ``refresh_passport_cookies``.
    Returns the fake client (a MagicMock) so tests can assert call_args.
    """
    client = MagicMock()
    if start_session is not None:
        if isinstance(start_session, Exception):
            client.start_device_login = AsyncMock(side_effect=start_session)
        else:
            client.start_device_login = AsyncMock(return_value=start_session)
    else:
        client.start_device_login = AsyncMock()

    if poll_outcome is not None:
        if isinstance(poll_outcome, Exception):
            client.poll_device_until_confirmed = AsyncMock(side_effect=poll_outcome)
            client.refresh_passport_cookies = AsyncMock()
        else:
            credentials, refresh_side_effect = poll_outcome
            client.poll_device_until_confirmed = AsyncMock(return_value=credentials)
            client.refresh_passport_cookies = AsyncMock(side_effect=refresh_side_effect)
    else:
        client.poll_device_until_confirmed = AsyncMock()
        client.refresh_passport_cookies = AsyncMock()

    @asynccontextmanager
    async def _fake_cm() -> AsyncIterator[MagicMock]:
        yield client

    monkeypatch.setattr(auto_create, "passport_client_session", _fake_cm)
    return client


def _patch_auto_create_skill(
    monkeypatch: pytest.MonkeyPatch,
    *,
    return_artifacts: SkillCreationArtifacts,
    side_effect: Exception | None = None,
) -> AsyncMock:
    """Replace auto_create.auto_create_skill with an AsyncMock returning the desired result."""
    mock = AsyncMock(return_value=return_artifacts, side_effect=side_effect)
    monkeypatch.setattr(auto_create, "auto_create_skill", mock)
    return mock


def _make_credentials() -> MagicMock:
    """Build a mock Credentials with x_token returning 'fresh-x-token'."""
    creds = MagicMock()
    creds.x_token = SecretStr("fresh-x-token")
    return creds


# ---------------------------------------------------------------------------
# DeviceCodeSession serialisation
# ---------------------------------------------------------------------------


class TestSerializeDeviceSession:
    """JSON round-trip preserves all fields and excludes plaintext from repr."""

    def test_round_trip(self) -> None:
        """Serialise → deserialise yields equal session + epoch."""
        session = _make_device_code_session()
        epoch = 1_746_537_600.0
        blob = serialize_device_session(session, epoch)
        assert blob is not None
        rehydrated = deserialize_device_session(blob)
        assert rehydrated is not None
        s2, e2 = rehydrated
        assert s2.user_code == session.user_code
        assert s2.verification_url == session.verification_url
        assert s2.expires_in == session.expires_in
        assert s2.interval == session.interval
        assert s2.device_code.get_secret() == "device-code-secret"
        assert e2 == epoch

    def test_serialise_none(self) -> None:
        """``None`` session → ``None`` blob."""
        assert serialize_device_session(None, 0.0) is None

    def test_deserialise_empty(self) -> None:
        """Empty / None raw → None outcome."""
        assert deserialize_device_session(None) is None
        assert deserialize_device_session("") is None

    def test_deserialise_garbage(self) -> None:
        """Non-JSON or non-dict input returns None instead of raising."""
        assert deserialize_device_session("not json {{{") is None
        assert deserialize_device_session('"a string"') is None

    def test_deserialise_missing_fields(self) -> None:
        """Missing required keys → None (corrupt blob is silently dropped)."""
        assert deserialize_device_session('{"user_code": "X"}') is None


# ---------------------------------------------------------------------------
# Stage 1: Start Device Flow (IDLE click)
# ---------------------------------------------------------------------------


class TestStartDeviceFlow:
    """First click on IDLE: request user_code, persist session blob."""

    @pytest.mark.asyncio
    async def test_starts_device_flow_returns_user_code(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Outcome contains user_code + verification_url + serialised session blob."""
        session = _make_device_code_session(user_code="WXYZ-9876")
        _patch_passport_client(monkeypatch, start_session=session)

        outcome = await run_auto_create_step(
            skill_name="Test",
            backend_uri="https://example.test/api/yandex_dialogs/webhook/sec",
            description="Test description.",
            structured_examples=None,
            activation_phrases=None,
            cached_x_token=None,
            pending_device_session_blob=None,
            artifacts=SkillCreationArtifacts(),
        )

        assert outcome.stage == LocalAutoCreateStage.DEVICE_FLOW_STARTED
        assert outcome.user_code == "WXYZ-9876"
        assert outcome.verification_url == "https://ya.ru/device"
        assert outcome.device_session_blob is not None
        # The serialised blob round-trips
        rehydrated = deserialize_device_session(outcome.device_session_blob)
        assert rehydrated is not None
        assert rehydrated[0].user_code == "WXYZ-9876"
        assert outcome.x_token is None
        assert "WXYZ-9876" in outcome.user_message

    @pytest.mark.asyncio
    async def test_start_failure_returns_failed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Network error during start_device_login → FAILED outcome with message."""
        _patch_passport_client(monkeypatch, start_session=RuntimeError("network down"))

        outcome = await run_auto_create_step(
            skill_name="Test",
            backend_uri="https://example.test/x",
            description="d",
            structured_examples=None,
            activation_phrases=None,
            cached_x_token=None,
            pending_device_session_blob=None,
            artifacts=SkillCreationArtifacts(),
        )

        assert outcome.stage == LocalAutoCreateStage.FAILED
        assert outcome.device_session_blob is None
        assert "network down" in (outcome.user_message or "")


# ---------------------------------------------------------------------------
# Stage 2: Resume Device Flow (subsequent clicks while DEVICE_FLOW_STARTED)
# ---------------------------------------------------------------------------


class TestResumeDeviceFlow:
    """Second-click polling: confirm → run pipeline; or keep waiting."""

    @pytest.mark.asyncio
    async def test_confirmed_proceeds_to_pipeline(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Successful poll captures x_token and immediately runs the pipeline."""
        session = _make_device_code_session()
        blob = serialize_device_session(session, time.time() + 600)

        _patch_passport_client(
            monkeypatch,
            poll_outcome=(_make_credentials(), None),
        )
        done = SkillCreationArtifacts(
            state=SkillCreationState.DONE,
            skill_id="skill-uuid-1",
            last_known_name="Test",
        )
        skill_mock = _patch_auto_create_skill(monkeypatch, return_artifacts=done)

        outcome = await run_auto_create_step(
            skill_name="Test",
            backend_uri="https://example.test/x",
            description="d",
            structured_examples=None,
            activation_phrases=None,
            cached_x_token=None,
            pending_device_session_blob=blob,
            artifacts=SkillCreationArtifacts(),
        )

        assert outcome.stage == LocalAutoCreateStage.DONE
        assert outcome.x_token == "fresh-x-token"
        # Device session is dropped after successful auth.
        assert outcome.device_session_blob is None
        assert outcome.artifacts.skill_id == "skill-uuid-1"
        skill_mock.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_still_waiting_keeps_session(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """DeviceCodeTimeoutError within the local poll window → keep waiting."""
        # expires_at_epoch ~10 minutes in the future, but our window is 8s.
        session = _make_device_code_session()
        epoch = time.time() + 600
        blob = serialize_device_session(session, epoch)

        _patch_passport_client(
            monkeypatch,
            poll_outcome=DeviceCodeTimeoutError("local poll window elapsed"),
        )

        outcome = await run_auto_create_step(
            skill_name="Test",
            backend_uri="https://example.test/x",
            description="d",
            structured_examples=None,
            activation_phrases=None,
            cached_x_token=None,
            pending_device_session_blob=blob,
            artifacts=SkillCreationArtifacts(),
        )

        assert outcome.stage == LocalAutoCreateStage.DEVICE_FLOW_STARTED
        assert outcome.user_code == "ABCD-1234"
        # Session blob is preserved for next click
        assert outcome.device_session_blob is not None

    @pytest.mark.asyncio
    async def test_underlying_expiry_returns_failed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """If user_code's actual expiry has passed → FAILED, drop session."""
        session = _make_device_code_session()
        # expires_at_epoch in the past
        blob = serialize_device_session(session, time.time() - 1.0)
        _patch_passport_client(monkeypatch)

        outcome = await run_auto_create_step(
            skill_name="Test",
            backend_uri="https://example.test/x",
            description="d",
            structured_examples=None,
            activation_phrases=None,
            cached_x_token=None,
            pending_device_session_blob=blob,
            artifacts=SkillCreationArtifacts(),
        )

        assert outcome.stage == LocalAutoCreateStage.FAILED
        assert outcome.device_session_blob is None
        assert "expired" in (outcome.user_message or "")

    @pytest.mark.asyncio
    async def test_invalid_credentials_returns_failed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """InvalidCredentialsError during poll → FAILED + drop session."""
        session = _make_device_code_session()
        blob = serialize_device_session(session, time.time() + 600)
        _patch_passport_client(
            monkeypatch,
            poll_outcome=InvalidCredentialsError("auth cancelled"),
        )

        outcome = await run_auto_create_step(
            skill_name="Test",
            backend_uri="https://example.test/x",
            description="d",
            structured_examples=None,
            activation_phrases=None,
            cached_x_token=None,
            pending_device_session_blob=blob,
            artifacts=SkillCreationArtifacts(),
        )

        assert outcome.stage == LocalAutoCreateStage.FAILED
        assert outcome.device_session_blob is None
        msg = (outcome.user_message or "").lower()
        assert "rejected" in msg or "cancelled" in msg


# ---------------------------------------------------------------------------
# Stage 3: Run pipeline with cached x_token
# ---------------------------------------------------------------------------


class TestRunPipeline:
    """Post-auth click runs the pipeline end-to-end on cached cookies."""

    @pytest.mark.asyncio
    async def test_happy_path(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """auto_create_skill returns DONE → outcome stage=DONE with skill_id link."""
        done = SkillCreationArtifacts(
            state=SkillCreationState.DONE,
            skill_id="skill-uuid-2",
            last_known_name="Test",
        )
        _patch_auto_create_skill(monkeypatch, return_artifacts=done)

        outcome = await run_auto_create_step(
            skill_name="Test",
            backend_uri="https://example.test/x",
            description="d",
            structured_examples=None,
            activation_phrases=None,
            cached_x_token="cached-token",
            pending_device_session_blob=None,
            artifacts=SkillCreationArtifacts(),
        )

        assert outcome.stage == LocalAutoCreateStage.DONE
        assert outcome.artifacts.skill_id == "skill-uuid-2"
        assert "skill-uuid-2" in outcome.user_message

    @pytest.mark.asyncio
    async def test_failed_artifacts_become_failed_outcome(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """auto_create_skill returns FAILED → outcome stage=FAILED, last_error rendered."""
        failed = SkillCreationArtifacts(
            state=SkillCreationState.FAILED,
            last_error="Skill name is already taken — pick another",
        )
        _patch_auto_create_skill(monkeypatch, return_artifacts=failed)

        outcome = await run_auto_create_step(
            skill_name="Test",
            backend_uri="https://example.test/x",
            description="d",
            structured_examples=None,
            activation_phrases=None,
            cached_x_token="cached-token",
            pending_device_session_blob=None,
            artifacts=SkillCreationArtifacts(),
        )

        assert outcome.stage == LocalAutoCreateStage.FAILED
        assert "already taken" in outcome.user_message

    @pytest.mark.asyncio
    async def test_passport_invalid_credentials_clears_token(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """InvalidCredentialsError from cached refresh → outcome.x_token='' to clear cache."""
        _patch_auto_create_skill(
            monkeypatch,
            return_artifacts=SkillCreationArtifacts(),  # not used
            side_effect=InvalidCredentialsError("session expired"),
        )

        outcome = await run_auto_create_step(
            skill_name="Test",
            backend_uri="https://example.test/x",
            description="d",
            structured_examples=None,
            activation_phrases=None,
            cached_x_token="stale-token",
            pending_device_session_blob=None,
            artifacts=SkillCreationArtifacts(),
        )

        assert outcome.stage == LocalAutoCreateStage.FAILED
        assert outcome.x_token == ""  # Signal to dispatcher: clear the cache
        assert "expired" in (outcome.user_message or "")


# ---------------------------------------------------------------------------
# Top-level dispatch decisions
# ---------------------------------------------------------------------------


class TestRunAutoCreateStepDispatch:
    """run_auto_create_step branches by (artifacts.state, x_token, pending session)."""

    @pytest.mark.asyncio
    async def test_done_state_is_no_op(self) -> None:
        """artifacts.state=DONE → outcome stage=DONE, no network calls."""
        artifacts = SkillCreationArtifacts(
            state=SkillCreationState.DONE,
            skill_id="existing",
            last_known_name="X",
        )
        outcome = await run_auto_create_step(
            skill_name="X",
            backend_uri="https://example.test/x",
            description="d",
            structured_examples=None,
            activation_phrases=None,
            cached_x_token="t",
            pending_device_session_blob=None,
            artifacts=artifacts,
        )
        assert outcome.stage == LocalAutoCreateStage.DONE
        # Artifacts pass through unchanged
        assert outcome.artifacts.skill_id == "existing"

    @pytest.mark.asyncio
    async def test_pending_session_takes_precedence_over_token(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Even with cached x_token, a pending session means we must finish auth first."""
        session = _make_device_code_session()
        blob = serialize_device_session(session, time.time() + 600)
        _patch_passport_client(
            monkeypatch,
            poll_outcome=DeviceCodeTimeoutError("waiting"),
        )

        outcome = await run_auto_create_step(
            skill_name="X",
            backend_uri="https://example.test/x",
            description="d",
            structured_examples=None,
            activation_phrases=None,
            cached_x_token="ignored-while-session-pending",
            pending_device_session_blob=blob,
            artifacts=SkillCreationArtifacts(),
        )

        # We polled the pending session, not jumped to the pipeline.
        assert outcome.stage == LocalAutoCreateStage.DEVICE_FLOW_STARTED
