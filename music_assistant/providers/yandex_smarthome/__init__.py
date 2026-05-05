"""
Yandex Smart Home Plugin Provider for Music Assistant.

Exposes Music Assistant players to Yandex Alice via the Yandex Smart Home API.
Allows voice control of MA players through Alice commands like
"Алиса, включи музыку на [имя плеера]".

Architecture:
  Alice voice command → Yandex Cloud → Smart Home API callback → this plugin → MA Player

The plugin registers MA players as media_device in Yandex Smart Home,
mapping capabilities (on_off, volume, pause) to MA player controls.

Reference: https://github.com/dext0r/yandex_smart_home
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import logging
import uuid
from typing import TYPE_CHECKING, cast

import aiohttp
from music_assistant_models.config_entries import ConfigEntry, ConfigValueOption
from music_assistant_models.enums import ConfigEntryType, ProviderFeature

from ._compat import SecretStr
from .auto_skill import (
    auto_create_skill,
    auto_rename_dialog_skill,
    load_default_logo_bytes,
)
from .auto_skill_state import (
    SkillCreationState,
    dump_artifacts,
    load_artifacts,
)
from .auto_skill_ui import build_cloud_plus_entries, build_direct_entries
from .cloud import get_cloud_otp, register_cloud_instance
from .constants import (
    CONF_ACTION_AUTO_CREATE,
    CONF_ACTION_AUTO_CREATE_DIALOG,
    CONF_ACTION_GET_OTP,
    CONF_ACTION_REGISTER,
    CONF_ACTION_RENAME_DIALOG_SKILL,
    CONF_AUTO_CREATE_ARTIFACTS,
    CONF_AUTO_CREATE_SESSION_ID,
    CONF_CLOUD_CONNECTION_TOKEN,
    CONF_CLOUD_INSTANCE_ID,
    CONF_CLOUD_INSTANCE_PASSWORD,
    CONF_CONNECTION_TYPE,
    CONF_DIALOG_AUTO_CREATE_ARTIFACTS,
    CONF_DIALOG_AUTO_CREATE_SESSION_ID,
    CONF_DIALOG_SKILL_ENABLED,
    CONF_DIALOG_SKILL_ID,
    CONF_DIALOG_SKILL_NAME,
    CONF_DIALOG_SKILL_TOKEN,
    CONF_DIALOG_WEBHOOK_SECRET,
    CONF_DIRECT_ACCESS_TOKEN,
    CONF_DIRECT_CLIENT_SECRET,
    CONF_EXPOSED_PLAYERS,
    CONF_EXPOSED_PLAYLISTS,
    CONF_EXTERNAL_BASE_URL,
    CONF_INSTANCE_NAME,
    CONF_SKILL_ID,
    CONF_SKILL_TOKEN,
    CONNECTION_TYPE_CLOUD,
    CONNECTION_TYPE_CLOUD_PLUS,
    CONNECTION_TYPE_DIRECT,
    DIALOG_DEFAULT_NAME,
    DIALOG_WEBHOOK_BASE_PATH,
    MAX_INPUT_SOURCES,
)
from .playlists import fetch_playlist_options
from .plugin import YandexSmartHomePlugin

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ConfigValueType, ProviderConfig
    from music_assistant_models.provider import ProviderManifest

    from music_assistant.mass import MusicAssistant
    from music_assistant.models import ProviderInstanceType

_LOGGER = logging.getLogger(__name__)

SUPPORTED_FEATURES: set[ProviderFeature] = set()


def _build_status_label(otp_code: str | None, is_cloud_plus: bool, is_registered: bool) -> str:
    """Build the status label text based on registration state."""
    if otp_code and is_cloud_plus:
        return (
            "✅ Cloud instance registered! "
            "Open Yandex app → Devices → Add device → Smart Home → "
            "find your private skill → enter OTP code below → "
            "then click Save to complete setup."
        )
    if otp_code:
        return (
            "✅ Cloud instance registered! "
            "Open Yandex app → Devices → Add device → Smart Home → "
            "find 'Yaha Cloud' skill → enter OTP code below → "
            "then click Save to complete setup."
        )
    if is_registered:
        return (
            "✅ Cloud instance is configured. "
            "Use 'Get OTP code' if you need to re-link with Yandex."
        )
    return (
        "Register a cloud instance to connect with Yandex Alice. "
        "This is free and uses the yaha-cloud.ru relay service (no public URL needed)."
    )


async def setup(
    mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
) -> ProviderInstanceType:
    """Initialize provider(instance) with given configuration."""
    return YandexSmartHomePlugin(mass, manifest, config, SUPPORTED_FEATURES)


def _resolve_direct_client_secret(
    mass: MusicAssistant,
    instance_id: str | None,
    values: dict[str, ConfigValueType],
) -> str:
    """Return the direct-mode OAuth client secret for the current install.

    `CONF_DIRECT_CLIENT_SECRET` is a SECURE_STRING: MA's frontend does
    not echo saved secrets back into ``values`` on re-open, so reading
    from ``values`` alone returns an empty string for existing instances.
    Prefer the persisted value from saved config and fall back to
    ``values`` only for first-time setup before any save.
    """
    if instance_id:
        prov = mass.get_provider(instance_id)
        if prov and prov.config:
            saved = prov.config.get_value(CONF_DIRECT_CLIENT_SECRET)
            if saved:
                return str(saved)
    return str(values.get(CONF_DIRECT_CLIENT_SECRET) or "")


def _resolve_external_base_url(
    mass: MusicAssistant,
    values: dict[str, ConfigValueType] | None = None,
) -> str:
    """Return the public-facing Base URL to use for Yandex callbacks/webhooks.

    Priority:
      1. ``CONF_EXTERNAL_BASE_URL`` from values (user-set plugin override)
      2. ``mass.webserver.base_url`` (MA's global setting)

    The override exists so users can keep MA's global Base URL pointing at
    the local address (so HA Ingress / local UI keep working) while
    exposing a public HTTPS URL only to Yandex via a reverse proxy.
    Trailing slashes are stripped.
    """
    override = ""
    if values is not None:
        override = str(values.get(CONF_EXTERNAL_BASE_URL) or "").strip()
    if override:
        return override.rstrip("/")
    fallback = ""
    with contextlib.suppress(Exception):
        fallback = str(mass.webserver.base_url)
    return fallback.strip().rstrip("/")


def _resolve_dialog_webhook_secret(
    mass: MusicAssistant,
    instance_id: str | None,
    values: dict[str, ConfigValueType],
) -> str:
    """Return the dialog webhook path-secret for the current install.

    Like ``_resolve_direct_client_secret``, prefers the persisted
    SECURE_STRING from saved config since the frontend does not echo
    secrets back into ``values`` on re-open.
    """
    if instance_id:
        prov = mass.get_provider(instance_id)
        if prov and prov.config:
            saved = prov.config.get_value(CONF_DIALOG_WEBHOOK_SECRET)
            if saved:
                return str(saved)
    return str(values.get(CONF_DIALOG_WEBHOOK_SECRET) or "")


async def _handle_config_actions(
    mass: MusicAssistant,
    action: str | None,
    values: dict[str, ConfigValueType],
    instance_id: str | None,
    is_cloud_plus: bool,
    connection_type: str,
) -> str | None:
    """Execute config-flow actions and return OTP code if obtained."""
    saved_config = None
    if instance_id:
        prov = mass.get_provider(instance_id)
        if prov:
            saved_config = prov.config

    if action == CONF_ACTION_REGISTER:
        try:
            platform = "yandex" if is_cloud_plus else None
            async with aiohttp.ClientSession() as session:
                data = await register_cloud_instance(session, platform=platform)
            values[CONF_CLOUD_INSTANCE_ID] = data["id"]
            values[CONF_CLOUD_INSTANCE_PASSWORD] = data["password"]
            values[CONF_CLOUD_CONNECTION_TOKEN] = data["connection_token"]
            _LOGGER.info("Auto-registered cloud instance: %s", data["id"])
        except Exception:
            _LOGGER.exception("Failed to register cloud instance")

    otp_code: str | None = None
    if action == CONF_ACTION_GET_OTP:
        cloud_id = str(values.get(CONF_CLOUD_INSTANCE_ID, ""))
        cloud_token = ""
        if saved_config:
            cloud_token = str(saved_config.get_value(CONF_CLOUD_CONNECTION_TOKEN) or "")
        if not cloud_token:
            cloud_token = str(values.get(CONF_CLOUD_CONNECTION_TOKEN, ""))
        if cloud_id and cloud_token:
            try:
                async with aiohttp.ClientSession() as session:
                    otp_code = await get_cloud_otp(session, cloud_id, SecretStr(cloud_token))
            except Exception:
                _LOGGER.exception("Failed to get OTP code")

    # NOTE: the old flow used to auto-fetch OTP right after Register so
    # the user saw the code immediately. In the 3-step cloud_plus flow
    # (Register → Create skill → Get OTP), that leaks the OTP into Step 1.
    # OTP is now fetched only when the user explicitly presses Get OTP
    # in Step 3.

    if action == CONF_ACTION_AUTO_CREATE:
        await _run_auto_create_action(mass, values, connection_type, instance_id)

    if action == CONF_ACTION_AUTO_CREATE_DIALOG:
        await _run_auto_create_dialog_action(mass, values, connection_type, instance_id)

    if action == CONF_ACTION_RENAME_DIALOG_SKILL:
        await _run_rename_dialog_action(mass, values, instance_id)

    return otp_code


async def _run_auto_create_action(
    mass: MusicAssistant,
    values: dict[str, ConfigValueType],
    connection_type: str,
    instance_id: str | None,
) -> None:
    """Execute the experimental auto-create-skill action.

    Never re-raises: all errors are persisted into the artifacts blob so
    the UI can show a FAILED state on the next render rather than
    crashing the config form.
    """
    # MA's frontend supplies ``values["session_id"]`` when it triggers an
    # action — AuthenticationHelper listens on that exact id to open
    # and later close the popup. If we roll our own id nothing listens
    # and the popup never appears. Fall back to a local uuid only if the
    # frontend happened not to pass one (shouldn't happen in practice).
    session_id = str(values.get("session_id") or uuid.uuid4().hex)
    values[CONF_AUTO_CREATE_SESSION_ID] = session_id
    artifacts_raw = values.get(CONF_AUTO_CREATE_ARTIFACTS)
    artifacts = load_artifacts(str(artifacts_raw) if artifacts_raw else None)

    try:
        new_artifacts = await auto_create_skill(
            mass=mass,
            connection_type=connection_type,
            skill_name=str(values.get(CONF_INSTANCE_NAME) or "Music Assistant"),
            artifacts=artifacts,
            cloud_instance_id=str(values.get(CONF_CLOUD_INSTANCE_ID, "")),
            direct_client_secret=_resolve_direct_client_secret(mass, instance_id, values),
            logo_bytes=load_default_logo_bytes(),
            session_id=session_id,
            base_url_override=str(values.get(CONF_EXTERNAL_BASE_URL) or "") or None,
        )
    except asyncio.CancelledError:
        # Preserve cooperative cancellation so config-flow shutdown
        # doesn't get converted into a FAILED artifact.
        raise
    except ValueError as exc:
        # Precondition failures come back here — surface as FAILED.
        new_artifacts = dataclasses.replace(
            artifacts,
            state=SkillCreationState.FAILED,
            last_error=str(exc),
        )
        _LOGGER.warning("auto-create precondition failed: %s", exc)
    except Exception as exc:  # defensive — never crash the config form
        new_artifacts = dataclasses.replace(
            artifacts,
            state=SkillCreationState.FAILED,
            last_error=repr(exc),
        )
        _LOGGER.exception("auto-create hit unexpected error")

    values[CONF_AUTO_CREATE_ARTIFACTS] = dump_artifacts(new_artifacts)
    if new_artifacts.state == SkillCreationState.DONE and new_artifacts.skill_id:
        # Only set CONF_SKILL_ID on full success so the runtime doesn't
        # try to use a half-built skill mid-pipeline.
        values[CONF_SKILL_ID] = new_artifacts.skill_id


def _build_dialog_backend_uri(base_url: str, webhook_secret: str) -> str:
    return f"{base_url.rstrip('/')}{DIALOG_WEBHOOK_BASE_PATH}/{webhook_secret}"


async def _run_auto_create_dialog_action(
    mass: MusicAssistant,
    values: dict[str, ConfigValueType],
    connection_type: str,
    instance_id: str | None,
) -> None:
    """Execute the experimental dialog skill auto-create action.

    Mirrors ``_run_auto_create_action`` but targets the dialog channel
    and persists under separate config keys so the two pipelines are
    independent.
    """
    session_id = str(values.get("session_id") or uuid.uuid4().hex)
    values[CONF_DIALOG_AUTO_CREATE_SESSION_ID] = session_id

    artifacts_raw = values.get(CONF_DIALOG_AUTO_CREATE_ARTIFACTS)
    artifacts = load_artifacts(str(artifacts_raw) if artifacts_raw else None)

    webhook_secret = _resolve_dialog_webhook_secret(mass, instance_id, values)
    if not webhook_secret:
        webhook_secret = uuid.uuid4().hex
        values[CONF_DIALOG_WEBHOOK_SECRET] = webhook_secret

    ma_base_url = _resolve_external_base_url(mass, values)
    dialog_backend_uri = _build_dialog_backend_uri(ma_base_url, webhook_secret)

    skill_name = str(values.get(CONF_DIALOG_SKILL_NAME) or DIALOG_DEFAULT_NAME)

    try:
        new_artifacts = await auto_create_skill(
            mass=mass,
            connection_type=connection_type,
            skill_name=skill_name,
            artifacts=artifacts,
            cloud_instance_id="",
            direct_client_secret=_resolve_direct_client_secret(mass, instance_id, values),
            logo_bytes=load_default_logo_bytes(),
            session_id=session_id,
            skill_type="dialog",
            dialog_backend_uri=dialog_backend_uri,
            base_url_override=str(values.get(CONF_EXTERNAL_BASE_URL) or "") or None,
        )
    except asyncio.CancelledError:
        raise
    except ValueError as exc:
        new_artifacts = dataclasses.replace(
            artifacts,
            state=SkillCreationState.FAILED,
            last_error=str(exc),
        )
        _LOGGER.warning("dialog auto-create precondition failed: %s", exc)
    except Exception as exc:
        new_artifacts = dataclasses.replace(
            artifacts,
            state=SkillCreationState.FAILED,
            last_error=repr(exc),
        )
        _LOGGER.exception("dialog auto-create hit unexpected error")

    values[CONF_DIALOG_AUTO_CREATE_ARTIFACTS] = dump_artifacts(new_artifacts)
    if new_artifacts.state == SkillCreationState.DONE and new_artifacts.skill_id:
        values[CONF_DIALOG_SKILL_ID] = new_artifacts.skill_id


async def _run_rename_dialog_action(
    mass: MusicAssistant,
    values: dict[str, ConfigValueType],
    instance_id: str | None,
) -> None:
    """Execute the dialog skill rename + re-deploy action."""
    artifacts_raw = values.get(CONF_DIALOG_AUTO_CREATE_ARTIFACTS)
    artifacts = load_artifacts(str(artifacts_raw) if artifacts_raw else None)

    webhook_secret = _resolve_dialog_webhook_secret(mass, instance_id, values)
    ma_base_url = _resolve_external_base_url(mass, values)
    dialog_backend_uri = _build_dialog_backend_uri(ma_base_url, webhook_secret)

    skill_name = str(values.get(CONF_DIALOG_SKILL_NAME) or DIALOG_DEFAULT_NAME)
    session_id = str(values.get("session_id") or uuid.uuid4().hex)

    new_artifacts = await auto_rename_dialog_skill(
        mass=mass,
        artifacts=artifacts,
        new_name=skill_name,
        dialog_backend_uri=dialog_backend_uri,
        session_id=session_id,
    )
    values[CONF_DIALOG_AUTO_CREATE_ARTIFACTS] = dump_artifacts(new_artifacts)


async def get_config_entries(  # noqa: PLR0915
    mass: MusicAssistant,
    instance_id: str | None = None,
    action: str | None = None,
    values: dict[str, ConfigValueType] | None = None,
) -> tuple[ConfigEntry, ...]:
    """Return Config entries to setup this provider."""
    if values is None:
        values = {}

    connection_type = str(values.get(CONF_CONNECTION_TYPE, CONNECTION_TYPE_CLOUD))
    is_cloud = connection_type == CONNECTION_TYPE_CLOUD
    is_cloud_plus = connection_type == CONNECTION_TYPE_CLOUD_PLUS
    is_direct = connection_type == CONNECTION_TYPE_DIRECT

    otp_code = await _handle_config_actions(
        mass, action, values, instance_id, is_cloud_plus, connection_type
    )

    # Auto-create-skill state — loaded once and threaded through the
    # per-mode builders below.
    artifacts_raw = values.get(CONF_AUTO_CREATE_ARTIFACTS)
    artifacts_str = str(artifacts_raw) if artifacts_raw else None
    artifacts = load_artifacts(artifacts_str)
    session_id_val = values.get(CONF_AUTO_CREATE_SESSION_ID)
    session_id_str = str(session_id_val) if session_id_val else None
    ma_base_url_for_ui = _resolve_external_base_url(mass, values)

    is_registered = bool(values.get(CONF_CLOUD_INSTANCE_ID)) and bool(
        values.get(CONF_CLOUD_CONNECTION_TOKEN)
    )
    cloud_instance_id = str(values.get(CONF_CLOUD_INSTANCE_ID, ""))

    label_text = _build_status_label(otp_code, is_cloud_plus, is_registered)

    # Build player options for exposed players filter
    player_options: list[ConfigValueOption] = []
    try:
        for player in mass.players.all_players():
            state = player.state
            player_options.append(
                ConfigValueOption(title=state.name or state.player_id, value=state.player_id)
            )
    except Exception:  # noqa: S110
        pass

    # Build playlist options from MA library (any music provider). Fail-soft: empty
    # list if music controller isn't ready (e.g. provider load order at first run).
    # CancelledError must propagate so config-flow cancellation/shutdown work.
    playlist_options: list[ConfigValueOption] = []
    try:
        playlist_options = await fetch_playlist_options(mass)
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: S110
        pass

    entries: list[ConfigEntry] = [
        # Instance name
        ConfigEntry(
            key=CONF_INSTANCE_NAME,
            type=ConfigEntryType.STRING,
            label="Instance Name",
            description=(
                "Name of this MA instance as it will appear in Yandex Smart Home. "
                "Alice will use this name for voice commands, e.g. "
                '"Алиса, включи музыку на [имя]".'
            ),
            required=False,
            default_value="Music Assistant",
        ),
        # Save-and-reopen notice — the form doesn't re-render on
        # dropdown change, so the user has to Save + reopen to see
        # the next mode's fields.
        ConfigEntry(
            key="label_connection_type_notice",
            type=ConfigEntryType.LABEL,
            label=(
                "💡 After changing Connection Type below, click Save and "
                "reopen this settings page to see the fields for the new mode."
            ),
        ),
        # Connection type selector
        ConfigEntry(
            key=CONF_CONNECTION_TYPE,
            type=ConfigEntryType.STRING,
            label="Connection Type",
            description=(
                '"cloud" — public Yaha Cloud skill (simple setup). '
                '"cloud_plus" — private skill via cloud relay (for multi-platform setups). '
                '"direct" — Yandex calls your MA server directly (requires public HTTPS URL).'
            ),
            required=False,
            default_value=CONNECTION_TYPE_CLOUD,
            options=[
                ConfigValueOption(title="Cloud (public Yaha Cloud skill)", value="cloud"),
                ConfigValueOption(title="Cloud Plus (private skill)", value="cloud_plus"),
                ConfigValueOption(title="Direct (no relay, requires public URL)", value="direct"),
            ],
            # NOTE: immediate_apply produced glitchy mixed-mode renders
            # (entries from old mode stayed on screen next to new ones),
            # so users need Save + reopen after changing Connection
            # Type. Kept here to stop someone re-adding it.
        ),
    ]

    # -- Per-mode sections (each builder returns only the fields for its mode)
    if is_cloud:
        entries.extend(_cloud_mode_entries(label_text, otp_code, is_registered))
    elif is_cloud_plus:
        entries.extend(
            build_cloud_plus_entries(
                otp_code=otp_code,
                is_registered=is_registered,
                cloud_instance_id=cloud_instance_id,
                artifacts=artifacts,
                session_id=session_id_str,
                user_code=None,  # popup URL carries the code
                verification_url=None,
                existing_artifacts_raw=artifacts_str,
                base_url=ma_base_url_for_ui,
                skill_id=str(values.get(CONF_SKILL_ID) or ""),
                skill_token_set=bool(values.get(CONF_SKILL_TOKEN)),
            )
        )
    elif is_direct:
        # Pre-generate the per-install direct client secret once so it
        # survives round-trips (auto-skill pipeline reads it later).
        # SECURE_STRING is not echoed back into ``values`` on re-open,
        # so prefer the persisted value from saved config first and
        # only mint a fresh UUID on true first-time setup.
        direct_secret = _resolve_direct_client_secret(mass, instance_id, values)
        if not direct_secret:
            direct_secret = uuid.uuid4().hex
            values[CONF_DIRECT_CLIENT_SECRET] = direct_secret

        # Pre-generate the dialog webhook secret similarly.
        dialog_webhook_secret = _resolve_dialog_webhook_secret(mass, instance_id, values)
        if not dialog_webhook_secret:
            dialog_webhook_secret = uuid.uuid4().hex
            values[CONF_DIALOG_WEBHOOK_SECRET] = dialog_webhook_secret

        # Dialog skill state
        dialog_artifacts_raw = values.get(CONF_DIALOG_AUTO_CREATE_ARTIFACTS)
        dialog_artifacts_str = str(dialog_artifacts_raw) if dialog_artifacts_raw else None
        dialog_artifacts = load_artifacts(dialog_artifacts_str)
        dialog_session_id_val = values.get(CONF_DIALOG_AUTO_CREATE_SESSION_ID)
        dialog_session_id_str = str(dialog_session_id_val) if dialog_session_id_val else None

        # Plugin-local override of MA's webserver Base URL — published to
        # Yandex but doesn't touch MA's global setting. Lets users keep the
        # global Base URL pointing at the local IP (so HA Ingress / local
        # frontend keep working) while still exposing a public HTTPS URL
        # to Yandex via a reverse proxy.
        ma_global_base_url = ""
        with contextlib.suppress(Exception):
            ma_global_base_url = str(mass.webserver.base_url)
        external_url_description = (
            "Public HTTPS URL of this MA instance, used only for Yandex "
            "callbacks and webhooks (Yandex requires HTTPS). Set this if "
            "you don't want to change MA's global Base URL — e.g. you reach "
            "MA via Home Assistant Ingress and exposing a public URL "
            "globally would break local access. Leave empty to use MA's "
            f"Base URL ({ma_global_base_url or '<unset>'})."
        )
        entries.append(
            ConfigEntry(
                key=CONF_EXTERNAL_BASE_URL,
                type=ConfigEntryType.STRING,
                label="External Base URL (HTTPS, optional override)",
                description=external_url_description,
                required=False,
                default_value="",
                value=str(values.get(CONF_EXTERNAL_BASE_URL) or ""),
                depends_on=CONF_CONNECTION_TYPE,
                depends_on_value=CONNECTION_TYPE_DIRECT,
            )
        )
        entries.extend(
            build_direct_entries(
                artifacts=artifacts,
                session_id=session_id_str,
                user_code=None,
                verification_url=None,
                existing_artifacts_raw=artifacts_str,
                base_url=ma_base_url_for_ui,
                direct_client_secret=direct_secret,
                skill_id=str(values.get(CONF_SKILL_ID) or ""),
                skill_token_set=bool(values.get(CONF_SKILL_TOKEN)),
                dialog_skill_enabled=bool(values.get(CONF_DIALOG_SKILL_ENABLED)),
                dialog_skill_name=str(values.get(CONF_DIALOG_SKILL_NAME) or DIALOG_DEFAULT_NAME),
                dialog_artifacts=dialog_artifacts,
                dialog_skill_id=str(values.get(CONF_DIALOG_SKILL_ID) or ""),
                dialog_session_id=dialog_session_id_str,
                dialog_existing_artifacts_raw=dialog_artifacts_str,
                dialog_user_code=None,
                dialog_verification_url=None,
            )
        )
        # NB: CONF_DIRECT_CLIENT_SECRET is now emitted by the manual
        # fallback block (advanced/hidden per state), so we don't add a
        # duplicate hidden round-trip entry here.

        # Round-trip the dialog webhook secret (SECURE_STRING, never echoed).
        entries.append(
            ConfigEntry(
                key=CONF_DIALOG_WEBHOOK_SECRET,
                type=ConfigEntryType.SECURE_STRING,
                label="Dialog webhook secret (internal)",
                hidden=True,
                required=False,
                value=dialog_webhook_secret,
            )
        )
        # Round-trip the dialog skill token (set externally if needed).
        entries.append(
            ConfigEntry(
                key=CONF_DIALOG_SKILL_TOKEN,
                type=ConfigEntryType.SECURE_STRING,
                label="Dialog skill token (internal)",
                hidden=True,
                required=False,
                value=cast("str", values.get(CONF_DIALOG_SKILL_TOKEN)) if values else None,
            )
        )

    # -- Tail: player filter + hidden round-trip fields (all modes) --
    entries.extend(_common_tail_entries(player_options, playlist_options, values))
    return tuple(entries)


def _cloud_mode_entries(
    label_text: str, otp_code: str | None, is_registered: bool
) -> list[ConfigEntry]:
    """Public-cloud mode: simple register + get-OTP flow."""
    return [
        # Advisory — the public Yaha Cloud skill can only be linked to one
        # instance per Yandex account, so users who already set up Yaha
        # Cloud in Home Assistant (or another MA install) need Cloud Plus.
        # There's no pre-flight API to detect this, so the warning is
        # static — cheaper than a failed OTP attempt.
        ConfigEntry(
            key="label_cloud_conflict_warning",
            type=ConfigEntryType.LABEL,
            label=(
                "⚠️ If this Yandex account already uses the Yaha Cloud skill "
                "via Home Assistant or another Music Assistant install, "
                "pick 'Cloud Plus' above instead — the public skill can "
                "only be linked to one instance per account."
            ),
            depends_on=CONF_CONNECTION_TYPE,
            depends_on_value=CONNECTION_TYPE_CLOUD,
        ),
        ConfigEntry(
            key="label_status",
            type=ConfigEntryType.LABEL,
            label=label_text,
            depends_on=CONF_CONNECTION_TYPE,
            depends_on_value=CONNECTION_TYPE_CLOUD,
        ),
        ConfigEntry(
            key="otp_code",
            type=ConfigEntryType.STRING,
            label="OTP Code",
            description="Copy this code and enter it in the Yandex app.",
            required=False,
            value=otp_code,
            hidden=not otp_code,
            depends_on=CONF_CONNECTION_TYPE,
            depends_on_value=CONNECTION_TYPE_CLOUD,
        ),
        ConfigEntry(
            key=CONF_ACTION_REGISTER,
            type=ConfigEntryType.ACTION,
            label="Register cloud instance",
            description="Register a new instance on yaha-cloud.ru relay service.",
            action=CONF_ACTION_REGISTER,
            action_label="Register with cloud",
            hidden=is_registered,
            # No depends_on — MA disables actions with an unsaved
            # dependency value until the user clicks Save, which breaks
            # the flow right after picking a connection type.
        ),
        ConfigEntry(
            key=CONF_ACTION_GET_OTP,
            type=ConfigEntryType.ACTION,
            label="Get OTP code",
            description="Get a fresh one-time password to link with Yandex Smart Home app.",
            action=CONF_ACTION_GET_OTP,
            action_label="Get OTP code",
            hidden=not is_registered,
        ),
    ]


def _common_tail_entries(
    player_options: list[ConfigValueOption],
    playlist_options: list[ConfigValueOption],
    values: dict[str, ConfigValueType],
) -> list[ConfigEntry]:
    """Player filter + hidden round-trip fields shared by every mode."""
    return [
        ConfigEntry(
            key=CONF_EXPOSED_PLAYERS,
            type=ConfigEntryType.STRING,
            label="Exposed Players",
            description=(
                "Select which MA players to expose to Yandex Smart Home. "
                "Leave empty to expose all players."
            ),
            required=False,
            multi_value=True,
            default_value=[],
            options=list(player_options) if player_options else [],
        ),
        ConfigEntry(
            key=CONF_EXPOSED_PLAYLISTS,
            type=ConfigEntryType.STRING,
            label=f"Exposed Playlists (max {MAX_INPUT_SOURCES})",
            description=(
                "Pick up to "
                f"{MAX_INPUT_SOURCES} playlists from your MA library — they appear "
                "as input_source mode slots one..ten on every exposed player, in the "
                "order you select them. Alice triggers them by ordinal only "
                "(«Alice, switch <player> source to five»); the Yandex Smart Home "
                "API does not allow naming mode values, so remember the order you "
                "picked. If the list is empty, save the form and reopen it once "
                "your music providers have finished loading their library."
            ),
            required=False,
            multi_value=True,
            default_value=[],
            options=list(playlist_options) if playlist_options else [],
        ),
        ConfigEntry(
            key=CONF_CLOUD_INSTANCE_ID,
            type=ConfigEntryType.STRING,
            label="Cloud Instance ID",
            hidden=True,
            required=False,
            value=cast("str", values.get(CONF_CLOUD_INSTANCE_ID)) if values else None,
        ),
        ConfigEntry(
            key=CONF_CLOUD_INSTANCE_PASSWORD,
            type=ConfigEntryType.SECURE_STRING,
            label="Cloud Instance Password",
            hidden=True,
            required=False,
            value=(cast("str", values.get(CONF_CLOUD_INSTANCE_PASSWORD)) if values else None),
        ),
        ConfigEntry(
            key=CONF_CLOUD_CONNECTION_TOKEN,
            type=ConfigEntryType.SECURE_STRING,
            label="Cloud Connection Token",
            hidden=True,
            required=False,
            value=(cast("str", values.get(CONF_CLOUD_CONNECTION_TOKEN)) if values else None),
        ),
        ConfigEntry(
            key=CONF_DIRECT_ACCESS_TOKEN,
            type=ConfigEntryType.SECURE_STRING,
            label="Direct Access Token",
            hidden=True,
            required=False,
            value=(cast("str", values.get(CONF_DIRECT_ACCESS_TOKEN)) if values else None),
        ),
    ]
