"""Pure rendering of auto-create UI entries from state.

Decoupled from the dispatcher (``provider.__init__`` uses these helpers
verbatim) so the form-shape can be unit-tested without exercising the
actual orchestrator. Returns ``ConfigEntry`` tuples.
"""

from __future__ import annotations

from music_assistant_models.config_entries import ConfigEntry
from music_assistant_models.enums import ConfigEntryType
from ya_dialogs_api import SkillCreationArtifacts, SkillCreationState

from .auto_create import AutoCreateOutcome, LocalAutoCreateStage
from .constants import (
    CONF_ACTION_AUTO_CREATE_DIALOG,
    CONF_ACTION_CANCEL_DIALOG_SKILL_FLOW,
)

__all__ = ["build_auto_create_entries"]


def _create_button_label(stage: LocalAutoCreateStage) -> str:
    """Button label flips per stage so users see what the click will do."""
    return {
        LocalAutoCreateStage.IDLE: "Create skill",
        LocalAutoCreateStage.DEVICE_FLOW_STARTED: "Confirm and continue",
        LocalAutoCreateStage.PIPELINE_RUNNING: "Resume",
        LocalAutoCreateStage.DONE: "Re-create",
        LocalAutoCreateStage.FAILED: "Retry",
    }[stage]


def _derive_stage(
    *,
    artifacts: SkillCreationArtifacts,
    pending_session_present: bool,
    cached_x_token_present: bool,
) -> LocalAutoCreateStage:
    """Derive the UX stage from persistent state.

    Decision order (most specific first):

    1. Pending Device Flow session → ``DEVICE_FLOW_STARTED``.
    2. Artifacts ``DONE`` → ``DONE`` regardless of token presence.
    3. Artifacts ``FAILED`` → ``FAILED`` (Retry button).
    4. Artifacts in any post-create state with cached token →
       ``PIPELINE_RUNNING`` ("Resume" — next click hits the pipeline).
    5. Same intermediate state but **no** cached token → ``IDLE``: next
       click will start a fresh Device Flow first, so the button label
       must say "Create skill", not "Resume", to match the actual next step.
    6. Otherwise → ``IDLE``.
    """
    if pending_session_present:
        return LocalAutoCreateStage.DEVICE_FLOW_STARTED
    if artifacts.state == SkillCreationState.DONE:
        return LocalAutoCreateStage.DONE
    if artifacts.state == SkillCreationState.FAILED:
        return LocalAutoCreateStage.FAILED
    if cached_x_token_present and artifacts.state in (
        SkillCreationState.APP_CREATED,
        SkillCreationState.DRAFT_UPDATED,
        SkillCreationState.OAUTH_CREATED,
        SkillCreationState.OAUTH_ATTACHED,
        SkillCreationState.DEPLOY_REQUESTED,
    ):
        return LocalAutoCreateStage.PIPELINE_RUNNING
    return LocalAutoCreateStage.IDLE


def _status_label_text(
    *,
    stage: LocalAutoCreateStage,
    artifacts: SkillCreationArtifacts,
    action_outcome: AutoCreateOutcome | None,
    pending_user_code: str | None,
    pending_verification_url: str | None,
) -> str:
    """Compose the status message shown above the auto-create button.

    Priority: a fresh action outcome wins (the user just clicked); otherwise
    we fall back to a state-derived static hint so the form still has
    context after a re-open. ``pending_user_code`` /
    ``pending_verification_url`` are populated when a Device Flow session is
    persisted in config — they let us re-show the original code/URL after a
    form reload, instead of leaving the LABEL blank with no instructions.
    """
    if action_outcome is not None:
        return action_outcome.user_message
    if stage == LocalAutoCreateStage.DEVICE_FLOW_STARTED:
        if pending_user_code and pending_verification_url:
            return (
                f"Device Flow in progress. Open {pending_verification_url} and "
                f"enter code {pending_user_code}, then click 'Confirm and continue'."
            )
        return (
            "Device Flow in progress. Click 'Confirm and continue' to check "
            "for confirmation, or 'Cancel' to abort."
        )
    if stage == LocalAutoCreateStage.DONE and artifacts.skill_id:
        return f"Skill created (skill_id={artifacts.skill_id})."
    if stage == LocalAutoCreateStage.FAILED and artifacts.last_error:
        return f"Error: {artifacts.last_error}"
    if stage == LocalAutoCreateStage.PIPELINE_RUNNING:
        return (
            "Skill creation was interrupted. Click 'Resume' to continue "
            f"from step {artifacts.state.value}."
        )
    if stage == LocalAutoCreateStage.IDLE:
        return (
            "Click 'Create skill' — Music Assistant will sign in to Yandex Passport "
            "(Device Flow) and register the skill at dialogs.yandex.ru."
        )
    return ""


def build_auto_create_entries(
    *,
    artifacts: SkillCreationArtifacts,
    pending_session_present: bool,
    cached_x_token_present: bool,
    action_outcome: AutoCreateOutcome | None,
    pending_user_code: str | None = None,
    pending_verification_url: str | None = None,
) -> tuple[ConfigEntry, ...]:
    """Render the auto-create cluster: status LABEL + ACTION + Cancel.

    The Cancel button is visible only when in DEVICE_FLOW_STARTED or FAILED —
    these are the states where the user might want to abandon a partial
    flow without waiting for the underlying user_code to expire.

    ``pending_user_code`` / ``pending_verification_url`` come from the
    deserialised Device Flow session: passing them in lets the LABEL
    remain self-explanatory after a form reload mid-Device-Flow (otherwise
    the user has nowhere to read the code they need to confirm).
    """
    stage = _derive_stage(
        artifacts=artifacts,
        pending_session_present=pending_session_present,
        cached_x_token_present=cached_x_token_present,
    )

    status_text = _status_label_text(
        stage=stage,
        artifacts=artifacts,
        action_outcome=action_outcome,
        pending_user_code=pending_user_code,
        pending_verification_url=pending_verification_url,
    )

    entries: list[ConfigEntry] = []

    if status_text:
        entries.append(
            ConfigEntry(
                key="label_auto_create_status",
                type=ConfigEntryType.LABEL,
                label=status_text,
            )
        )

    entries.append(
        ConfigEntry(
            key=CONF_ACTION_AUTO_CREATE_DIALOG,
            type=ConfigEntryType.ACTION,
            label="Auto-register skill",
            description=(
                "One click creates a skill at https://dialogs.yandex.ru/developer "
                "via the Yandex Passport Device Flow. The button can be clicked "
                "repeatedly — the process resumes from the last completed step."
            ),
            action=CONF_ACTION_AUTO_CREATE_DIALOG,
            action_label=_create_button_label(stage),
            required=False,
            default_value="",
        )
    )

    if stage in (LocalAutoCreateStage.DEVICE_FLOW_STARTED, LocalAutoCreateStage.FAILED):
        entries.append(
            ConfigEntry(
                key=CONF_ACTION_CANCEL_DIALOG_SKILL_FLOW,
                type=ConfigEntryType.ACTION,
                label="Cancel",
                description=(
                    "Aborts the current authentication / creation process. "
                    "The cached x_token is preserved."
                ),
                action=CONF_ACTION_CANCEL_DIALOG_SKILL_FLOW,
                action_label="Cancel",
                required=False,
                default_value="",
            )
        )

    return tuple(entries)
