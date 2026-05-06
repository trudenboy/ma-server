"""Yandex Alice (Dialogs custom skill) plugin provider for Music Assistant.

Exposes Music Assistant playback to a Yandex Dialogs custom skill — a Russian
NLU voice control surface invoked via *«Алиса, попроси Music Assistant …»*.

Setup paths:

1. **Auto** (since v1.1.0): the *Create skill* button kicks off a Yandex
   Passport Device Flow login and registers the skill in
   ``https://dialogs.yandex.ru/developer`` programmatically via
   ``ya-dialogs-api``. The skill ID is auto-populated on success.
2. **Manual** (still supported): create the skill yourself in the dev console,
   point its webhook URL at ``/api/yandex_dialogs/webhook/<your-secret>``,
   and paste the skill ID + token into the form.
"""

from __future__ import annotations

import dataclasses
import logging
import secrets
from typing import TYPE_CHECKING

from music_assistant_models.config_entries import ConfigEntry, ConfigValueOption
from music_assistant_models.enums import ConfigEntryType, ProviderFeature
from ya_dialogs_api import (
    SkillCreationArtifacts,
    SkillCreationState,
    dump_artifacts,
    load_artifacts,
)

from .auto_create import (
    AutoCreateOutcome,
    LocalAutoCreateStage,
    deserialize_device_session,
    run_auto_create_step,
)
from .auto_create_view import build_auto_create_entries
from .auto_update import run_auto_update
from .constants import (
    CONF_ACTION_AUTO_CREATE_DIALOG,
    CONF_ACTION_CANCEL_DIALOG_SKILL_FLOW,
    CONF_ACTION_RENAME_DIALOG_SKILL,
    CONF_AUTH_X_TOKEN,
    CONF_DIALOG_AUTO_CREATE_ARTIFACTS,
    CONF_DIALOG_AUTO_CREATE_DEVICE_SESSION,
    CONF_DIALOG_SKILL_ENABLED,
    CONF_DIALOG_SKILL_ID,
    CONF_DIALOG_SKILL_NAME,
    CONF_DIALOG_SKILL_TOKEN,
    CONF_DIALOG_WEBHOOK_SECRET,
    CONF_EXPOSED_PLAYERS,
    CONF_EXPOSED_PLAYLISTS,
    CONF_EXTERNAL_BASE_URL,
    CONF_INSTANCE_NAME,
    DIALOG_DEFAULT_NAME,
    DIALOG_NAME_MAX_LEN,
    DIALOG_NAME_MIN_LEN,
    DIALOG_WEBHOOK_BASE_PATH,
    YANDEX_DIALOGS_DEVELOPER_URL,
)
from .dialog_skill_meta import (
    build_activation_phrases,
    build_backend_uri,
    build_skill_description,
    build_structured_examples,
)
from .playlists import fetch_playlist_options
from .plugin import YandexAlicePlugin

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ConfigValueType, ProviderConfig
    from music_assistant_models.provider import ProviderManifest

    from music_assistant.mass import MusicAssistant
    from music_assistant.models import ProviderInstanceType


_LOGGER = logging.getLogger(__name__)

SUPPORTED_FEATURES: set[ProviderFeature] = set()


async def setup(
    mass: MusicAssistant,
    manifest: ProviderManifest,
    config: ProviderConfig,
) -> ProviderInstanceType:
    """Initialise the provider instance with the given configuration."""
    return YandexAlicePlugin(mass, manifest, config, SUPPORTED_FEATURES)


def _generate_webhook_secret() -> str:
    """Return a fresh URL-safe random secret for the webhook path."""
    return secrets.token_urlsafe(24)


async def _list_player_options(mass: MusicAssistant) -> list[ConfigValueOption]:
    """List MA players the user can expose to voice control."""
    options: list[ConfigValueOption] = []
    try:
        for player in mass.players.all_players():
            options.append(
                ConfigValueOption(
                    title=player.display_name or player.name or player.player_id,
                    value=player.player_id,
                )
            )
    except Exception as exc:
        _LOGGER.debug("could not enumerate players: %s", exc)
    return options


def _name_drifted(artifacts: SkillCreationArtifacts, skill_name: str) -> bool:
    """Detect divergence between MA-side `skill_name` and Yandex `last_known_name`."""
    return bool(
        artifacts.last_known_name and artifacts.last_known_name.strip() != skill_name.strip()
    )


def _resolve_saved_value(
    values: dict[str, ConfigValueType],
    key: str,
) -> str:
    """Read a config value from form ``values`` (string-coerced).

    Earlier versions also fell through to ``mass.config.get_provider_config``
    for keys the frontend may not echo back. That call deadlocks against the
    config controller's own lock when MA opens the provider settings page —
    `get_config_entries` is invoked by MA *while* it holds the config lock,
    and the recursive read blocks indefinitely. So we now rely solely on
    ``values``, and stabilise critical SECURE_STRING fields by writing the
    derived value back into ``values`` early in the dispatcher (so subsequent
    action clicks within the same form session see the same value).
    """
    return str(values.get(key) or "")


async def get_config_entries(  # noqa: PLR0915
    mass: MusicAssistant,
    instance_id: str | None = None,
    action: str | None = None,
    values: dict[str, ConfigValueType] | None = None,
) -> tuple[ConfigEntry, ...]:
    """Build the provider config-form entries with auto-create / rename actions.

    Action handling:

    - ``CONF_ACTION_AUTO_CREATE_DIALOG`` — advance the Device Flow + skill
      creation state machine by one external-IO step. Re-click drives further
      stages (see :mod:`provider.auto_create`).
    - ``CONF_ACTION_RENAME_DIALOG_SKILL`` — patch the existing skill draft
      via cached x_token; no Device Flow.
    - ``CONF_ACTION_CANCEL_DIALOG_SKILL_FLOW`` — drop pending session +
      reset artifacts; preserve cached x_token.

    Auto-create / rename state lives in three hidden config entries
    (``CONF_AUTH_X_TOKEN``, ``CONF_DIALOG_AUTO_CREATE_ARTIFACTS``,
    ``CONF_DIALOG_AUTO_CREATE_DEVICE_SESSION``) that round-trip through the
    form on every save.
    """
    values = values or {}

    # Generate a webhook secret on first open if the user hasn't set one yet.
    # Read through saved provider config too: the frontend may not echo
    # SECURE_STRING fields between action clicks, and regenerating the
    # secret per call would orphan webhooks already registered with Yandex
    # against an earlier (now-discarded) secret.
    _ = instance_id  # reserved for future per-instance config lookups
    existing_secret = _resolve_saved_value(values, CONF_DIALOG_WEBHOOK_SECRET).strip()
    default_secret = existing_secret or _generate_webhook_secret()
    # Stabilise inside this dispatch: any backend_uri assembled below uses
    # the same secret as the form will save on user click.
    values[CONF_DIALOG_WEBHOOK_SECRET] = default_secret

    instance_name = str(values.get(CONF_INSTANCE_NAME) or DIALOG_DEFAULT_NAME)

    # ---- Pull persistent auto-create / auth state ----
    artifacts = load_artifacts(
        _resolve_saved_value(values, CONF_DIALOG_AUTO_CREATE_ARTIFACTS) or None
    )
    cached_x_token = _resolve_saved_value(values, CONF_AUTH_X_TOKEN)
    device_session_blob = _resolve_saved_value(values, CONF_DIALOG_AUTO_CREATE_DEVICE_SESSION)

    # Skill name priority: explicit dialog skill name → instance name → default.
    skill_name = (
        str(values.get(CONF_DIALOG_SKILL_NAME) or "").strip()
        or str(values.get(CONF_INSTANCE_NAME) or "").strip()
        or DIALOG_DEFAULT_NAME
    )

    external_base_url = str(values.get(CONF_EXTERNAL_BASE_URL) or "").strip().rstrip("/")
    webhook_secret = default_secret

    action_outcome: AutoCreateOutcome | None = None
    update_message: str | None = None

    # ---- Action dispatcher ----
    if action == CONF_ACTION_AUTO_CREATE_DIALOG:
        # Treat re-click on DONE as "Re-create" → reset artifacts before stepping.
        if artifacts.state == SkillCreationState.DONE:
            artifacts = SkillCreationArtifacts()
            device_session_blob = ""

        # Backup-restore safety: skill_id is set in config but artifacts are
        # NONE → pre-position to APP_CREATED so the library skips create_app
        # and patches the existing skill rather than creating a duplicate.
        saved_skill_id = str(values.get(CONF_DIALOG_SKILL_ID) or "").strip()
        if saved_skill_id and artifacts.state == SkillCreationState.NONE and not artifacts.skill_id:
            artifacts = dataclasses.replace(
                artifacts,
                state=SkillCreationState.APP_CREATED,
                skill_id=saved_skill_id,
            )

        try:
            backend_uri = build_backend_uri(external_base_url, webhook_secret)
        except ValueError as exc:
            action_outcome = AutoCreateOutcome(
                artifacts=dataclasses.replace(
                    artifacts,
                    state=SkillCreationState.FAILED,
                    last_error=str(exc),
                ),
                device_session_blob=None,
                x_token=None,
                user_code=None,
                verification_url=None,
                user_message=str(exc),
                stage=LocalAutoCreateStage.FAILED,
            )
        else:
            action_outcome = await run_auto_create_step(
                skill_name=skill_name,
                backend_uri=backend_uri,
                description=build_skill_description(skill_name),
                structured_examples=build_structured_examples(skill_name),
                activation_phrases=build_activation_phrases(skill_name),
                cached_x_token=cached_x_token or None,
                pending_device_session_blob=device_session_blob or None,
                artifacts=artifacts,
            )

    elif action == CONF_ACTION_RENAME_DIALOG_SKILL:
        try:
            backend_uri = build_backend_uri(external_base_url, webhook_secret)
        except ValueError as exc:
            update_message = str(exc)
            artifacts = dataclasses.replace(
                artifacts,
                state=SkillCreationState.FAILED,
                last_error=str(exc),
            )
        else:
            update_outcome = await run_auto_update(
                cached_x_token=cached_x_token or None,
                skill_name=skill_name,
                backend_uri=backend_uri,
                description=build_skill_description(skill_name),
                structured_examples=build_structured_examples(skill_name),
                activation_phrases=build_activation_phrases(skill_name),
                artifacts=artifacts,
            )
            artifacts = update_outcome.artifacts
            update_message = update_outcome.user_message
            if update_outcome.x_token == "":
                cached_x_token = ""

    elif action == CONF_ACTION_CANCEL_DIALOG_SKILL_FLOW:
        # Drop pending session + reset artifacts; keep cached x_token.
        artifacts = SkillCreationArtifacts()
        device_session_blob = ""

    # ---- Reflect outcome into values so the next form save persists state ----
    if action_outcome is not None:
        artifacts = action_outcome.artifacts
        if action_outcome.device_session_blob is not None:
            device_session_blob = action_outcome.device_session_blob
        elif action_outcome.stage in (
            LocalAutoCreateStage.DONE,
            LocalAutoCreateStage.FAILED,
        ):
            device_session_blob = ""
        if action_outcome.x_token is not None:
            cached_x_token = action_outcome.x_token

    values[CONF_DIALOG_AUTO_CREATE_ARTIFACTS] = dump_artifacts(artifacts)
    values[CONF_AUTH_X_TOKEN] = cached_x_token
    values[CONF_DIALOG_AUTO_CREATE_DEVICE_SESSION] = device_session_blob
    if artifacts.state == SkillCreationState.DONE and artifacts.skill_id:
        values[CONF_DIALOG_SKILL_ID] = artifacts.skill_id

    # ---- Player / playlist options ----
    player_options = await _list_player_options(mass)
    try:
        playlist_options = await fetch_playlist_options(mass)
    except Exception as exc:
        _LOGGER.debug("could not enumerate playlists: %s", exc)
        playlist_options = []

    base_url_hint = (
        "Public HTTPS URL of this Music Assistant instance, "
        "as Yandex will see it (e.g. https://ma.example.com). "
        "Leave empty to use MA's global Base URL setting."
    )

    # ---- Auto-create cluster: status LABEL + auto-create ACTION + Cancel ----
    # Surface the pending session details so the LABEL can re-show the
    # user_code + verification URL after a form reload mid-Device-Flow.
    pending_user_code: str | None = None
    pending_verification_url: str | None = None
    if device_session_blob:
        decoded = deserialize_device_session(device_session_blob)
        if decoded is not None:
            pending_user_code = decoded[0].user_code
            pending_verification_url = decoded[0].verification_url

    auto_create_entries = build_auto_create_entries(
        artifacts=artifacts,
        pending_session_present=bool(device_session_blob),
        cached_x_token_present=bool(cached_x_token),
        action_outcome=action_outcome,
        pending_user_code=pending_user_code,
        pending_verification_url=pending_verification_url,
    )

    # ---- Rename cluster: drift LABEL (conditional) + Rename ACTION ----
    rename_entries: tuple[ConfigEntry, ...] = ()
    rename_visible = bool(artifacts.skill_id and cached_x_token)
    if rename_visible:
        drift_text = ""
        if update_message:
            drift_text = update_message
        elif _name_drifted(artifacts, skill_name):
            drift_text = (
                f"Name in Yandex ('{artifacts.last_known_name}') differs from "
                f"the current 'Skill name' ({skill_name!r}). Click 'Rename'."
            )
        rename_entries = (
            *(
                (
                    ConfigEntry(
                        key="label_rename_status",
                        type=ConfigEntryType.LABEL,
                        label=drift_text,
                    ),
                )
                if drift_text
                else ()
            ),
            ConfigEntry(
                key=CONF_ACTION_RENAME_DIALOG_SKILL,
                type=ConfigEntryType.ACTION,
                label="Rename skill in Yandex",
                description=(
                    "Apply the current 'Skill name' value to the existing "
                    "skill in Yandex Dialogs (PATCH draft + re-deploy). "
                    "Uses the cached x_token — no re-authentication required."
                ),
                action=CONF_ACTION_RENAME_DIALOG_SKILL,
                action_label="Rename",
                required=False,
                default_value="",
            ),
        )

    # ---- Hidden state-carrier entries (round-trip persistence) ----
    hidden_state_entries = (
        ConfigEntry(
            key=CONF_AUTH_X_TOKEN,
            type=ConfigEntryType.SECURE_STRING,
            label="Yandex Passport x_token (cached)",
            description="Cached after first successful Device Flow.",
            required=False,
            default_value=cached_x_token,
            hidden=True,
        ),
        ConfigEntry(
            key=CONF_DIALOG_AUTO_CREATE_ARTIFACTS,
            type=ConfigEntryType.STRING,
            label="Auto-create artifacts (JSON)",
            description="State machine snapshot — persisted between clicks.",
            required=False,
            default_value=dump_artifacts(artifacts),
            hidden=True,
        ),
        ConfigEntry(
            key=CONF_DIALOG_AUTO_CREATE_DEVICE_SESSION,
            type=ConfigEntryType.SECURE_STRING,
            label="Pending Device Flow session (JSON)",
            description="Persisted during DEVICE_FLOW_STARTED stage.",
            required=False,
            default_value=device_session_blob,
            hidden=True,
        ),
    )

    return (
        ConfigEntry(
            key="label_intro",
            type=ConfigEntryType.LABEL,
            label=(
                "Yandex Alice voice control. Use 'Create skill' below "
                "for one-click registration via Yandex Passport, or set up "
                f"manually at {YANDEX_DIALOGS_DEVELOPER_URL}."
            ),
        ),
        ConfigEntry(
            key=CONF_INSTANCE_NAME,
            type=ConfigEntryType.STRING,
            label="Instance name",
            description=(
                "Display name shown to users. Pick something they will say "
                'to invoke the skill, e.g. "Music Assistant" → '
                '"Alice, ask Music Assistant ..."'
            ),
            required=False,
            default_value=DIALOG_DEFAULT_NAME,
        ),
        ConfigEntry(
            key=CONF_EXTERNAL_BASE_URL,
            type=ConfigEntryType.STRING,
            label="External base URL (HTTPS, required for auto-create)",
            description=base_url_hint,
            required=False,
            default_value="",
        ),
        ConfigEntry(
            key=CONF_DIALOG_SKILL_ENABLED,
            type=ConfigEntryType.BOOLEAN,
            label="Enable dialog skill",
            description=(
                "Turn this on once the skill is created (auto or manual) "
                "and the credentials below are populated."
            ),
            required=False,
            default_value=False,
        ),
        ConfigEntry(
            key=CONF_DIALOG_SKILL_NAME,
            type=ConfigEntryType.STRING,
            label="Skill name",
            description=(
                "Display name pushed to Yandex Dialogs on auto-create / "
                "rename. Min "
                f"{DIALOG_NAME_MIN_LEN}, max {DIALOG_NAME_MAX_LEN} characters."
            ),
            required=False,
            default_value=instance_name,
        ),
        *auto_create_entries,
        *rename_entries,
        ConfigEntry(
            key=CONF_DIALOG_SKILL_ID,
            type=ConfigEntryType.STRING,
            label="Skill ID",
            description=(
                "UUID of the skill — populated automatically after a "
                "successful auto-create, or paste manually if you set up "
                "the skill yourself."
            ),
            required=False,
            default_value="",
        ),
        ConfigEntry(
            key=CONF_DIALOG_SKILL_TOKEN,
            type=ConfigEntryType.SECURE_STRING,
            label="Skill OAuth token (manual setup only)",
            description=(
                "Optional OAuth token from "
                "https://oauth.yandex.ru/authorize?response_type=token"
                "&client_id=c473ca268cd749d3a8371351a8f2bcbd. "
                "Used to push state callbacks to Yandex (future feature; "
                "stored encrypted)."
            ),
            required=False,
            default_value="",
        ),
        ConfigEntry(
            key=CONF_DIALOG_WEBHOOK_SECRET,
            type=ConfigEntryType.SECURE_STRING,
            label="Webhook URL secret",
            description=(
                "Random secret embedded in the webhook URL. The full URL is "
                f"<external_base_url>{DIALOG_WEBHOOK_BASE_PATH}/<this-secret>. "
                "Pre-filled with a fresh value; click 'Save' to commit."
            ),
            required=False,
            default_value=default_secret,
        ),
        ConfigEntry(
            key=CONF_EXPOSED_PLAYERS,
            type=ConfigEntryType.STRING,
            label="Voice-controllable players",
            description=(
                "Players the skill is allowed to control. Leave empty to "
                "expose all players known to MA."
            ),
            multi_value=True,
            options=player_options,
            required=False,
            default_value=[],
        ),
        ConfigEntry(
            key=CONF_EXPOSED_PLAYLISTS,
            type=ConfigEntryType.STRING,
            label="Voice-addressable playlists",
            description=(
                "Optional curated list of playlists the user can ask for by "
                "name. Leave empty for full library search."
            ),
            multi_value=True,
            options=playlist_options,
            required=False,
            default_value=[],
        ),
        *hidden_state_entries,
    )
