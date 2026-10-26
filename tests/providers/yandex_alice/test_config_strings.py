"""
Every user-facing text of the options page and the setup flow resolves from ``strings.json``.

Options entries resolve ``config_entries.<key>.<field>``; setup-flow steps
resolve ``setup_flow.<step_id>``; form errors resolve ``errors.<slug>``; flow
aborts resolve ``setup_flow.abort.<reason>``; action results resolve
``config_actions.<slug>``. A missing text renders empty in the UI.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest
from ya_passport_auth.ma import BORROW_SOURCE_OWN

from music_assistant.providers.yandex_alice import setup_flow as flow
from music_assistant.providers.yandex_alice.constants import (
    CONF_AUTH_X_TOKEN,
    CONF_DIALOG_SKILL_ID,
    CONF_DIALOG_SKILL_NAME,
    CONF_EXTERNAL_BASE_URL,
    CONF_YM_INSTANCE,
)

from .localization import authored_texts, load_strings
from .test_plugin import SETUP, _plugin
from .test_setup_flow import (  # noqa: F401 -- fixtures used by the flow tests below
    BASE_URL,
    EXISTING_DATA,
    _duplicate,
    _FakeSession,
    api,
    client,
)

_PLACEHOLDER = re.compile(r"\{(\d+)\}")

_OPTION_STATES: dict[str, dict[str, Any]] = {
    "signed_in": SETUP,
    "signed_out": {**SETUP, CONF_AUTH_X_TOKEN: None},
    "no_skill": {**SETUP, CONF_DIALOG_SKILL_ID: ""},
    "empty": {},
}


@pytest.mark.asyncio
@pytest.mark.parametrize("state", _OPTION_STATES)
async def test_options_entries_resolve_text(state: str, tmp_path: Path) -> None:
    """Visible options entries have a label (in code or authored) and enough params."""
    plugin, _mass = _plugin(tmp_path, setup_data=dict(_OPTION_STATES[state]))
    for entry in await plugin.get_config_entries():
        if entry.hidden:
            continue
        texts = authored_texts(entry)
        assert entry.label or "label" in texts, f"{state}: '{entry.key}' has no label"
        if entry.type.value == "action":
            assert "action_label" in texts, f"{state}: '{entry.key}' has no action_label"
        if entry.label:
            assert "label" not in texts, f"{state}: '{entry.key}' runtime label is shadowed"
        params = entry.translation_params or []
        for field in ("label", "description"):
            placeholders = [int(m) for m in _PLACEHOLDER.findall(texts.get(field, ""))]
            if placeholders:
                assert len(params) > max(placeholders), f"{state}: '{entry.key}.{field}'"


def test_action_labels_carry_no_placeholders() -> None:
    """MA resolves action_label without params — placeholders would render literally."""
    for key, texts in load_strings()["config_entries"].items():
        assert not _PLACEHOLDER.search(texts.get("action_label", "")), key


def test_action_results_and_hard_to_reach_entries_are_authored() -> None:
    """Action outcome texts and rarely rendered labels exist."""
    strings = load_strings()
    actions = strings["config_actions"]
    for slug in (
        "status_refreshed",
        "status_unavailable",
        "skill_deleted",
        "manifest_export_success",
        "manifest_import_success",
        "manifest_import_invalid",
        "manifest_reset_success",
        "manifest_validate_success",
    ):
        assert slug in actions, slug
    for key in ("label_diagnostics_inactive", "label_skill_missing", "label_auth_signed_out"):
        assert "label" in strings["config_entries"][key], key


def _flow_scenarios() -> list[list[tuple[str, dict[str, Any]]]]:
    own = ("user", {CONF_YM_INSTANCE: BORROW_SOURCE_OWN})
    skill = (
        "skill",
        {
            flow.CONF_SKILL_MODE: flow.SKILL_MODE_CREATE,
            CONF_DIALOG_SKILL_NAME: "Music Assistant",
            CONF_EXTERNAL_BASE_URL: BASE_URL,
        },
    )
    existing = (
        "skill",
        {
            flow.CONF_SKILL_MODE: flow.SKILL_MODE_EXISTING,
            CONF_DIALOG_SKILL_NAME: "Music Assistant",
            CONF_EXTERNAL_BASE_URL: BASE_URL,
            CONF_DIALOG_SKILL_ID: "sk-1",
        },
    )
    return [
        [
            own,
            ("method", {flow.CONF_METHOD: flow.METHOD_COOKIES}),
            ("cookies", {flow.CONF_COOKIES: "a=b"}),
            skill,
        ],
        [own, ("method", {flow.CONF_METHOD: flow.METHOD_DEVICE}), existing, ("webhook_info", {})],
    ]


@pytest.mark.asyncio
async def test_setup_flow_steps_and_fields_are_authored(api: Any) -> None:  # noqa: F811
    """Every step the flow shows has a title, and every field a label and option titles."""
    strings = load_strings()
    steps: set[str] = set()
    entries: list[Any] = []
    api.create.return_value = _duplicate()
    scenarios = _flow_scenarios()
    scenarios[0].append(("duplicate", {flow.CONF_DUPLICATE_CHOICE: flow.DUPLICATE_ADOPT}))
    for responses in scenarios:
        session = _FakeSession(responses)
        await flow.run_setup(session)  # type: ignore[arg-type]
        steps.update(session.steps)
        for step_entries in session.form_entries.values():
            entries.extend(step_entries)
    api.update.return_value = _updated_outcome()
    for second_step in (
        ("edit_skill", {CONF_DIALOG_SKILL_NAME: "Music Assistant"}),
        ("webhook", {CONF_EXTERNAL_BASE_URL: BASE_URL}),
    ):
        action = (
            flow.RECONFIGURE_EDIT if second_step[0] == "edit_skill" else flow.RECONFIGURE_WEBHOOK
        )
        session = _FakeSession(
            [("reconfigure", {flow.CONF_RECONFIGURE: action}), second_step],
            kind="reconfigure",
            setup_data=dict(EXISTING_DATA),
        )
        await flow.run_setup(session)  # type: ignore[arg-type]
        steps.update(session.steps)
        for step_entries in session.form_entries.values():
            entries.extend(step_entries)
    for step in steps:
        assert "title" in strings["setup_flow"].get(step, {}), f"setup_flow.{step}.title"
    for entry in entries:
        texts = strings["config_entries"].get(entry.key, {})
        assert "label" in texts, f"config_entries.{entry.key}.label"
        for option in entry.options or []:
            if option.title is None:
                assert option.value in texts.get("options", {}), (
                    f"config_entries.{entry.key}.options.{option.value}"
                )


def test_flow_progress_texts_errors_and_aborts_are_authored() -> None:
    """Progress texts, form error slugs and abort reasons used by the flow exist."""
    strings = load_strings()
    for step in ("device_login", "scan_qr", "create_skill", "update_skill"):
        assert strings["setup_flow"][step].get("progress_text") or step == "device_login", step
    for slug in ("invalid_base_url", "skill_id_required"):
        assert slug in strings["errors"], slug
    for reason in ("login_denied", "update_failed"):
        assert reason in strings["setup_flow"]["abort"], reason


def _updated_outcome() -> Any:
    from ya_dialogs_api import SkillCreationArtifacts, SkillCreationState  # noqa: PLC0415

    from music_assistant.providers.yandex_alice.auto_update import (  # noqa: PLC0415
        AutoUpdateOutcome,
    )

    return AutoUpdateOutcome(
        artifacts=SkillCreationArtifacts(state=SkillCreationState.DONE, skill_id="sk-1"),
        x_token=None,
        user_message="ok",
    )
