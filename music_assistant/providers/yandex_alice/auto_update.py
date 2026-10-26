"""
Auto-update wrapper around ``ya_dialogs_api.auto_update_skill``.

Used by the reconfigure flow to edit the skill or re-register its webhook
address. ``auto_update_skill`` patches
the full draft (so a rename also re-applies description / activation
phrases / structured examples) and re-deploys; Yandex moderation queue
rules then apply (~5-15 min for ``aliceSkill``).

Cache-only-by-design: refuses to fall back to interactive Device Flow.
If the x_token is missing or rejected by Passport, the outcome signals
the setup flow to ask the user to sign in again.
"""

from __future__ import annotations

import dataclasses
import logging
from dataclasses import dataclass
from typing import Any

from ya_dialogs_api import (
    EntityDraft,
    IntentDraft,
    SkillCreationArtifacts,
    SkillCreationState,
    auto_update_skill,
)
from ya_passport_auth.exceptions import InvalidCredentialsError

from .auth_session import make_cached_authenticator
from .constants import DIALOG_CHANNEL

_LOGGER = logging.getLogger(__name__)

__all__ = ["AutoUpdateOutcome", "run_auto_update"]


@dataclass(frozen=True, slots=True)
class AutoUpdateOutcome:
    """Result of one skill update attempt."""

    artifacts: SkillCreationArtifacts
    """Latest snapshot — overwrites CONF_DIALOG_AUTO_CREATE_ARTIFACTS."""

    x_token: str | None
    """``""`` to clear cached x_token (auth rejected); ``None`` to leave as-is."""

    user_message: str
    """Status / error string shown to the user on failure."""


async def run_auto_update(
    *,
    cached_x_token: str | None,
    skill_name: str,
    backend_uri: str,
    description: str,
    structured_examples: list[dict[str, Any]] | None,
    activation_phrases: list[str] | None,
    intents: list[IntentDraft],
    entities: list[EntityDraft],
    artifacts: SkillCreationArtifacts,
    voice: str | None = None,
) -> AutoUpdateOutcome:
    """
    Patch the existing skill draft + re-deploy. No Device Flow fallback.

    Pre-conditions checked synchronously before any network call:

    - ``cached_x_token`` is non-empty — otherwise return FAILED.
    - ``artifacts.skill_id`` is set — otherwise FAILED (no skill to update).

    The library itself surfaces ya-dialogs-api errors as ``state=FAILED`` /
    ``last_error`` rather than raising. We translate
    :class:`InvalidCredentialsError` from Passport into a token-clear
    signal so the setup flow can ask the user to sign in again.
    """
    if not cached_x_token:
        failed = dataclasses.replace(
            artifacts,
            state=SkillCreationState.FAILED,
            last_error="No cached authentication — reconfigure the provider and sign in first.",
        )
        return AutoUpdateOutcome(
            artifacts=failed,
            x_token=None,
            user_message=str(failed.last_error),
        )

    if not artifacts.skill_id:
        failed = dataclasses.replace(
            artifacts,
            state=SkillCreationState.FAILED,
            last_error="skill_id is missing — create the skill first (reconfigure the provider).",
        )
        return AutoUpdateOutcome(
            artifacts=failed,
            x_token=None,
            user_message=str(failed.last_error),
        )

    authenticator = make_cached_authenticator(cached_x_token)

    try:
        result = await auto_update_skill(
            authenticator=authenticator,
            artifacts=artifacts,
            skill_name=skill_name,
            backend_uri=backend_uri,
            channel=DIALOG_CHANNEL,
            description=description,
            structured_examples=structured_examples,
            activation_phrases=activation_phrases,
            intents=intents,
            entities=entities,
            voice=voice,
        )
    except InvalidCredentialsError as exc:
        _LOGGER.warning("auto-update: cached x_token rejected: %s", exc)
        failed = dataclasses.replace(
            artifacts,
            state=SkillCreationState.FAILED,
            last_error=("The Yandex sign-in has expired. Sign in again to continue."),
        )
        return AutoUpdateOutcome(
            artifacts=failed,
            x_token="",
            user_message=str(failed.last_error),
        )

    if result.state == SkillCreationState.DONE:
        message = (
            f"Skill updated (name: {skill_name!r}). "
            "Yandex moderation queue: 5-15 minutes before the update is published."
        )
        return AutoUpdateOutcome(
            artifacts=result,
            x_token=None,
            user_message=message,
        )

    msg = result.last_error or "Failed to update the skill."
    return AutoUpdateOutcome(
        artifacts=result,
        x_token=None,
        user_message=msg,
    )
