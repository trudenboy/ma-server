"""Self-resuming Device Flow + dialog-skill creation orchestrator.

Music Assistant config-actions are synchronous: each click of the auto-create
button is one HTTP request, and the form re-renders with whatever entries we
return. Yandex Passport's Device Flow needs the user to walk away and confirm
on a different page (~30s-several minutes), so we can't drive it inside a
single action call.

Solution: layer a *local* state machine on top of ``SkillCreationArtifacts``
that survives across clicks via JSON in the provider config. Each click
advances by exactly one external-IO step:

- ``IDLE`` (no session, no token) → start Device Flow → ``DEVICE_FLOW_STARTED``
- ``DEVICE_FLOW_STARTED`` → one-shot poll with short window → if confirmed,
  refresh cookies and capture ``x_token`` → ``AUTHENTICATED``
- ``AUTHENTICATED`` (or any post-auth artifact state) → run
  ``auto_create_skill(channel="aliceSkill", oauth_*=None)`` end-to-end →
  ``DONE`` / ``FAILED``

The full pipeline after auth is a single ya-dialogs-api call: all four steps
(``create_app → upload_logo → update_draft → request_deploy``) are fast HTTP
to ``dialogs.yandex.ru``. ``progress_cb`` checkpoints each step so a mid-call
failure resumes from the last completed state on the next click.
"""

from __future__ import annotations

import dataclasses
import json
import logging
import time
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from ya_dialogs_api import (
    SkillCreationArtifacts,
    SkillCreationState,
    auto_create_skill,
)
from ya_passport_auth import DeviceCodeSession, SecretStr
from ya_passport_auth.exceptions import (
    DeviceCodeTimeoutError,
    InvalidCredentialsError,
)

from .auth_session import make_cached_authenticator, passport_client_session
from .constants import DIALOG_CHANNEL

_LOGGER = logging.getLogger(__name__)

__all__ = [
    "AutoCreateOutcome",
    "LocalAutoCreateStage",
    "deserialize_device_session",
    "run_auto_create_step",
    "serialize_device_session",
]


class LocalAutoCreateStage(StrEnum):
    """High-level UX stage derived from artifacts + pending Device Flow.

    Not persisted directly: rendered on every click from
    ``(SkillCreationArtifacts, has_pending_device_session, has_cached_x_token)``.
    Drives the button label flip ("Create" / "Confirm and continue" /
    "Resume" / "Re-create" / "Retry") and the visibility of the
    Cancel button.
    """

    IDLE = "idle"
    DEVICE_FLOW_STARTED = "device_flow_started"
    PIPELINE_RUNNING = "pipeline_running"
    DONE = "done"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class AutoCreateOutcome:
    """Result of one click on the auto-create button.

    The dispatcher in :mod:`provider.__init__` writes ``artifacts``,
    ``device_session_blob``, and ``x_token`` into the form ``values`` so MA
    persists them on the next form save. ``user_message`` and the
    ``user_code`` / ``verification_url`` pair feed the status LABEL the user
    sees while the flow is in progress.
    """

    artifacts: SkillCreationArtifacts
    """Latest snapshot — overwrites CONF_DIALOG_AUTO_CREATE_ARTIFACTS."""

    device_session_blob: str | None
    """Serialised pending session, ``None`` to drop, ``""`` (empty) to keep as-is."""

    x_token: str | None
    """Newly minted x_token, ``""`` to clear cache, ``None`` to leave existing."""

    user_code: str | None
    """Code to display to the user (``DEVICE_FLOW_STARTED`` only)."""

    verification_url: str | None
    """URL the user must open (``DEVICE_FLOW_STARTED`` only)."""

    user_message: str
    """Human-readable status for the LABEL entry."""

    stage: LocalAutoCreateStage
    """High-level UX stage — drives button label + cancel visibility."""


# ---------------------------------------------------------------------------
# Device session JSON round-trip
# ---------------------------------------------------------------------------


def serialize_device_session(
    session: DeviceCodeSession | None, expires_at_epoch: float
) -> str | None:
    """Serialise a ``DeviceCodeSession`` to JSON for config storage.

    ``device_code`` is unwrapped from :class:`SecretStr` because it must
    survive a config round-trip — the SECURE_STRING entry encrypts it at
    rest. ``expires_at_epoch`` is the absolute wall-clock deadline we
    recompute once at first start and check on every resume so a long-idle
    user doesn't see a "still waiting" loop on a code Yandex already killed.
    """
    if session is None:
        return None
    return json.dumps(
        {
            "device_code": session.device_code.get_secret(),
            "user_code": session.user_code,
            "verification_url": session.verification_url,
            "expires_in": session.expires_in,
            "interval": session.interval,
            "expires_at_epoch": expires_at_epoch,
        },
        ensure_ascii=False,
    )


def deserialize_device_session(
    raw: str | None,
) -> tuple[DeviceCodeSession, float] | None:
    """Inverse of :func:`serialize_device_session`. Returns ``None`` on any failure.

    Returns ``(session, expires_at_epoch)`` so the caller can decide whether
    the underlying user_code is still alive on Yandex's side.
    """
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        return None
    if not isinstance(data, dict):
        return None
    try:
        device_code = SecretStr(str(data["device_code"]))
        session = DeviceCodeSession(
            device_code=device_code,
            user_code=str(data["user_code"]),
            verification_url=str(data["verification_url"]),
            expires_in=int(data["expires_in"]),
            interval=int(data["interval"]),
        )
        expires_at_epoch = float(data.get("expires_at_epoch", 0.0))
    except (KeyError, ValueError, TypeError):
        return None
    return session, expires_at_epoch


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------


async def run_auto_create_step(
    *,
    skill_name: str,
    backend_uri: str,
    description: str,
    structured_examples: list[dict[str, Any]] | None,
    activation_phrases: list[str] | None,
    cached_x_token: str | None,
    pending_device_session_blob: str | None,
    artifacts: SkillCreationArtifacts,
    poll_window: float = 8.0,
) -> AutoCreateOutcome:
    """Advance the state machine by exactly one external-IO step.

    Branches by current state:

    1. ``artifacts.state == DONE`` → no-op outcome (caller resets artifacts
       on a "Re-create" click before invoking us).
    2. Pending Device Flow session → one-shot poll. If confirmed, capture
       x_token and proceed to pipeline in *the same click* (cheap step).
       If still pending and underlying code alive, keep stage. If underlying
       expired or Yandex rejected, return FAILED with a clear last_error.
    3. Have ``cached_x_token`` (post-auth) → run the full
       ``auto_create_skill`` pipeline.
    4. None of the above → start Device Flow.
    """
    if artifacts.state == SkillCreationState.DONE:
        return AutoCreateOutcome(
            artifacts=artifacts,
            device_session_blob=None,
            x_token=None,
            user_code=None,
            verification_url=None,
            user_message="Skill created. Click 'Save' to apply the settings.",
            stage=LocalAutoCreateStage.DONE,
        )

    pending = deserialize_device_session(pending_device_session_blob)

    if pending is not None:
        return await _resume_device_flow(
            pending_session=pending[0],
            expires_at_epoch=pending[1],
            poll_window=poll_window,
            skill_name=skill_name,
            backend_uri=backend_uri,
            description=description,
            structured_examples=structured_examples,
            activation_phrases=activation_phrases,
            artifacts=artifacts,
        )

    if cached_x_token:
        return await _run_pipeline(
            cached_x_token=cached_x_token,
            skill_name=skill_name,
            backend_uri=backend_uri,
            description=description,
            structured_examples=structured_examples,
            activation_phrases=activation_phrases,
            artifacts=artifacts,
        )

    return await _start_device_flow(artifacts=artifacts)


# ---------------------------------------------------------------------------
# Stage handlers
# ---------------------------------------------------------------------------


async def _start_device_flow(*, artifacts: SkillCreationArtifacts) -> AutoCreateOutcome:
    """Request a fresh user_code from Yandex Passport and persist the session."""
    try:
        async with passport_client_session() as client:
            session = await client.start_device_login(
                device_name="Music Assistant",
            )
    except Exception as exc:
        _LOGGER.exception("auto-create: start_device_login failed")
        failed = dataclasses.replace(
            artifacts,
            state=SkillCreationState.FAILED,
            last_error=f"Failed to request a device code from Yandex Passport: {exc!r}",
        )
        return AutoCreateOutcome(
            artifacts=failed,
            device_session_blob=None,
            x_token=None,
            user_code=None,
            verification_url=None,
            user_message=str(failed.last_error),
            stage=LocalAutoCreateStage.FAILED,
        )

    expires_at = time.time() + session.expires_in
    blob = serialize_device_session(session, expires_at)
    user_message = (
        f"Open {session.verification_url} and enter code {session.user_code}. "
        "Click the button again once you've confirmed."
    )
    return AutoCreateOutcome(
        artifacts=artifacts,
        device_session_blob=blob,
        x_token=None,
        user_code=session.user_code,
        verification_url=session.verification_url,
        user_message=user_message,
        stage=LocalAutoCreateStage.DEVICE_FLOW_STARTED,
    )


async def _resume_device_flow(
    *,
    pending_session: DeviceCodeSession,
    expires_at_epoch: float,
    poll_window: float,
    skill_name: str,
    backend_uri: str,
    description: str,
    structured_examples: list[dict[str, Any]] | None,
    activation_phrases: list[str] | None,
    artifacts: SkillCreationArtifacts,
) -> AutoCreateOutcome:
    """Poll the Device Flow once with a short window; chain into pipeline on success."""
    remaining = expires_at_epoch - time.time()
    if remaining <= 0:
        failed = dataclasses.replace(
            artifacts,
            state=SkillCreationState.FAILED,
            last_error="Device code expired. Click 'Create' to request a new one.",
        )
        return AutoCreateOutcome(
            artifacts=failed,
            device_session_blob=None,
            x_token=None,
            user_code=None,
            verification_url=None,
            user_message=str(failed.last_error),
            stage=LocalAutoCreateStage.FAILED,
        )

    window = min(poll_window, remaining)
    try:
        async with passport_client_session() as client:
            credentials = await client.poll_device_until_confirmed(
                pending_session,
                total_timeout=window,
            )
            x_token_secret = credentials.x_token
            await client.refresh_passport_cookies(x_token_secret)
    except DeviceCodeTimeoutError:
        if window < remaining:
            blob = serialize_device_session(pending_session, expires_at_epoch)
            return AutoCreateOutcome(
                artifacts=artifacts,
                device_session_blob=blob,
                x_token=None,
                user_code=pending_session.user_code,
                verification_url=pending_session.verification_url,
                user_message=(
                    f"Still waiting for confirmation. Code {pending_session.user_code} "
                    f"at {pending_session.verification_url}. Click the button again."
                ),
                stage=LocalAutoCreateStage.DEVICE_FLOW_STARTED,
            )
        failed = dataclasses.replace(
            artifacts,
            state=SkillCreationState.FAILED,
            last_error="Device code expired. Click 'Create' to request a new one.",
        )
        return AutoCreateOutcome(
            artifacts=failed,
            device_session_blob=None,
            x_token=None,
            user_code=None,
            verification_url=None,
            user_message=str(failed.last_error),
            stage=LocalAutoCreateStage.FAILED,
        )
    except InvalidCredentialsError as exc:
        _LOGGER.warning("auto-create: device flow rejected: %s", exc)
        failed = dataclasses.replace(
            artifacts,
            state=SkillCreationState.FAILED,
            last_error=f"Yandex Passport rejected the sign-in: {exc}",
        )
        return AutoCreateOutcome(
            artifacts=failed,
            device_session_blob=None,
            x_token=None,
            user_code=None,
            verification_url=None,
            user_message=str(failed.last_error),
            stage=LocalAutoCreateStage.FAILED,
        )
    except Exception as exc:
        _LOGGER.exception("auto-create: device flow unexpected error")
        failed = dataclasses.replace(
            artifacts,
            state=SkillCreationState.FAILED,
            last_error=f"Unexpected authentication error: {exc!r}",
        )
        return AutoCreateOutcome(
            artifacts=failed,
            device_session_blob=None,
            x_token=None,
            user_code=None,
            verification_url=None,
            user_message=str(failed.last_error),
            stage=LocalAutoCreateStage.FAILED,
        )

    x_token = x_token_secret.get_secret()
    pipeline_outcome = await _run_pipeline(
        cached_x_token=x_token,
        skill_name=skill_name,
        backend_uri=backend_uri,
        description=description,
        structured_examples=structured_examples,
        activation_phrases=activation_phrases,
        artifacts=artifacts,
    )
    return dataclasses.replace(
        pipeline_outcome,
        device_session_blob=None,
        x_token=x_token,
    )


async def _run_pipeline(
    *,
    cached_x_token: str,
    skill_name: str,
    backend_uri: str,
    description: str,
    structured_examples: list[dict[str, Any]] | None,
    activation_phrases: list[str] | None,
    artifacts: SkillCreationArtifacts,
) -> AutoCreateOutcome:
    """Run the OAuth-free aliceSkill pipeline end-to-end on cached cookies.

    Error handling has two layers:

    1. ``ya_dialogs_api.auto_create_skill`` itself catches every
       :class:`DialogsApiError` subclass internally and returns artifacts
       with ``state=FAILED`` and a populated ``last_error`` (see
       library docstring). We don't need to catch those explicitly — they
       arrive as a regular return value and flow through
       :func:`_outcome_from_failed_pipeline`.

    2. :class:`InvalidCredentialsError` from
       ``ya_passport_auth.refresh_passport_cookies`` is the one exception
       that escapes the library because cookie refresh happens inside the
       authenticator context manager (before the library's pipeline
       starts). We translate it into ``outcome.x_token=""`` so the
       dispatcher clears the cache and the next click can re-auth
       cleanly via Device Flow.
    """
    authenticator = make_cached_authenticator(cached_x_token)

    try:
        result = await auto_create_skill(
            authenticator=authenticator,
            skill_name=skill_name,
            artifacts=artifacts,
            backend_uri=backend_uri,
            channel=DIALOG_CHANNEL,
            description=description,
            structured_examples=structured_examples,
            activation_phrases=activation_phrases,
        )
    except InvalidCredentialsError as exc:
        _LOGGER.warning("auto-create: cached x_token rejected by Passport: %s", exc)
        failed = dataclasses.replace(
            artifacts,
            state=SkillCreationState.FAILED,
            last_error="Cached auth has expired. Click the button again to re-authenticate.",
        )
        return AutoCreateOutcome(
            artifacts=failed,
            device_session_blob=None,
            x_token="",
            user_code=None,
            verification_url=None,
            user_message=str(failed.last_error),
            stage=LocalAutoCreateStage.FAILED,
        )

    if result.state == SkillCreationState.DONE:
        skill_url = (
            f"https://dialogs.yandex.ru/developer/skills/{result.skill_id}"
            if result.skill_id
            else "https://dialogs.yandex.ru/developer"
        )
        message = (
            f"Skill created (skill_id={result.skill_id}). "
            f"Yandex moderation queue: 5-15 minutes. "
            f"Check on-air status: {skill_url}"
        )
        return AutoCreateOutcome(
            artifacts=result,
            device_session_blob=None,
            x_token=None,
            user_code=None,
            verification_url=None,
            user_message=message,
            stage=LocalAutoCreateStage.DONE,
        )

    return _outcome_from_failed_pipeline(result)


def _outcome_from_failed_pipeline(result: SkillCreationArtifacts) -> AutoCreateOutcome:
    """Translate a FAILED ``SkillCreationArtifacts`` into a UX outcome.

    Reads ``last_error`` (already populated by ya-dialogs-api). For typed
    sub-errors that the library reports through the artifact, the wire
    representation is ``last_error: str`` — we keep it as-is. The caller
    (dispatcher) layers domain-aware advice (e.g. duplicate-name → suggest
    rename) by inspecting the message before rendering.
    """
    msg = result.last_error or "Pipeline failed without a description."
    return AutoCreateOutcome(
        artifacts=result,
        device_session_blob=None,
        x_token=None,
        user_code=None,
        verification_url=None,
        user_message=msg,
        stage=LocalAutoCreateStage.FAILED,
    )
