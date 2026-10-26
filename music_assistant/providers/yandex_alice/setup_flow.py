"""
Setup flow for the Yandex Alice plugin provider.

Adding the provider walks the user through the one-time input: pick the Yandex
account (borrow a linked Yandex Music instance or sign in with own credentials
by device code, QR code or browser cookies), then either create the Yandex
Dialogs skill automatically or point the flow at a skill made by hand.

Reconfiguring opens a menu: sign in again, edit the skill, change the webhook
address, or create the skill again. A reconfigure that MA starts because of an
authentication error goes straight to sign-in.

Everything collected is stored as the provider's setup data.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

import segno
from music_assistant_models.config_entries import ConfigEntry, ConfigValueOption
from music_assistant_models.enums import ConfigEntryType
from music_assistant_models.errors import (
    InvalidDataError,
    LoginFailed,
    ResourceTemporarilyUnavailable,
)
from ya_dialogs_api import (
    SkillCreationArtifacts,
    SkillCreationState,
    dump_artifacts,
    load_artifacts,
)
from ya_passport_auth import ClientConfig, PassportClient
from ya_passport_auth.exceptions import (
    DeviceCodeTimeoutError,
    InvalidCredentialsError,
    QRTimeoutError,
    YaPassportError,
)
from ya_passport_auth.ma import BORROW_SOURCE_OWN, list_yandex_music_instances, login_with_cookies

from music_assistant.models.setup_flow import AbortFlow, StepExpiredError

from .auto_create import (
    AutoCreateOutcome,
    LocalAutoCreateStage,
    adopt_existing_skill,
    delete_existing_skill_then_recreate,
    run_create_skill,
)
from .auto_update import run_auto_update
from .constants import (
    CONF_AUTH_USER_NAME,
    CONF_AUTH_X_TOKEN,
    CONF_DIALOG_ACTIVATION_PHRASE_2,
    CONF_DIALOG_ACTIVATION_PHRASE_3,
    CONF_DIALOG_ACTIVATION_PHRASE_4,
    CONF_DIALOG_AUTO_CREATE_ARTIFACTS,
    CONF_DIALOG_PUBLICATION_STATUS,
    CONF_DIALOG_SKILL_ID,
    CONF_DIALOG_SKILL_NAME,
    CONF_DIALOG_SKILL_VOICE,
    CONF_DIALOG_WEBHOOK_SECRET,
    CONF_EXTERNAL_BASE_URL,
    CONF_YM_INSTANCE,
    DIALOG_DEFAULT_NAME,
    DIALOG_VOICE_DEFAULT,
    DIALOG_VOICE_OPTIONS,
)
from .dialog_skill_meta import (
    build_backend_uri,
    validate_activation_phrase,
    validate_skill_name,
)
from .publication_status import fetch_skill_publication_status
from .skill_manifest_provider import SkillManifestProvider
from .skill_ops import borrowed_x_token, deploy_args, generate_webhook_secret, is_borrowing
from .url_helpers import is_public_https_url, try_detect_public_https_url

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ConfigValueType
    from ya_passport_auth import Credentials

    from music_assistant.models.setup_flow import SetupSession

CONF_METHOD = "method"
METHOD_DEVICE = "device"
METHOD_QR = "qr"
METHOD_COOKIES = "cookies"
CONF_COOKIES = "cookies"

CONF_SKILL_MODE = "skill_mode"
SKILL_MODE_CREATE = "create"
SKILL_MODE_EXISTING = "existing"

CONF_DUPLICATE_CHOICE = "duplicate_choice"
DUPLICATE_ADOPT = "adopt"
DUPLICATE_RECREATE = "recreate"
DUPLICATE_RENAME = "rename"

CONF_RECONFIGURE = "reconfigure_action"
RECONFIGURE_SIGN_IN = "sign_in"
RECONFIGURE_EDIT = "edit_skill"
RECONFIGURE_WEBHOOK = "webhook"
RECONFIGURE_RECREATE = "recreate"

CONF_NEW_SECRET = "new_webhook_secret"  # noqa: S105 - config key, not a secret
CONF_WEBHOOK_URL = "webhook_url"

# device name shown on the Yandex confirmation screen during the login flows
_DEVICE_NAME = "Music Assistant"

_PHRASE_KEYS = (
    CONF_DIALOG_ACTIVATION_PHRASE_2,
    CONF_DIALOG_ACTIVATION_PHRASE_3,
    CONF_DIALOG_ACTIVATION_PHRASE_4,
)


async def run_setup(session: SetupSession) -> None:
    """
    Run the Yandex Alice setup or reconfigure flow.

    :param session: The setup session driving the flow.
    """
    data: dict[str, ConfigValueType] = dict(session.context.setup_data)
    data.setdefault(CONF_DIALOG_WEBHOOK_SECRET, generate_webhook_secret())
    if session.context.kind != "reconfigure":
        x_token = await _account(session, data)
        await _skill(session, data, x_token)
    elif session.context.reason == "auth":
        await _account(session, data)
    else:
        await _reconfigure(session, data)
    await session.finish(data)


async def _reconfigure(session: SetupSession, data: dict[str, ConfigValueType]) -> None:
    """Show the reconfigure menu and run the chosen part."""
    values = await session.form(
        [
            ConfigEntry(
                key=CONF_RECONFIGURE,
                type=ConfigEntryType.STRING,
                required=True,
                default_value=RECONFIGURE_EDIT
                if data.get(CONF_DIALOG_SKILL_ID)
                else RECONFIGURE_RECREATE,
                options=[
                    ConfigValueOption(value=RECONFIGURE_SIGN_IN),
                    ConfigValueOption(value=RECONFIGURE_EDIT),
                    ConfigValueOption(value=RECONFIGURE_WEBHOOK),
                    ConfigValueOption(value=RECONFIGURE_RECREATE),
                ],
            )
        ],
        step_id="reconfigure",
    )
    choice = str(values[CONF_RECONFIGURE])
    if choice == RECONFIGURE_SIGN_IN:
        await _account(session, data)
    elif choice == RECONFIGURE_EDIT:
        await _edit_skill(session, data, await _stored_x_token(session, data))
    elif choice == RECONFIGURE_WEBHOOK:
        await _webhook(session, data)
    else:
        data[CONF_DIALOG_AUTO_CREATE_ARTIFACTS] = dump_artifacts(SkillCreationArtifacts())
        await _skill(session, data, await _stored_x_token(session, data))


# ---------------------------------------------------------------------------
# Account
# ---------------------------------------------------------------------------


async def _account(session: SetupSession, data: dict[str, ConfigValueType]) -> str:
    """Pick the account source, sign in if needed, and return a usable x_token."""
    instances = list_yandex_music_instances(session.mass)
    valid = {inst_id for inst_id, _ in instances}
    default = str(data.get(CONF_YM_INSTANCE) or BORROW_SOURCE_OWN)
    if default != BORROW_SOURCE_OWN and default not in valid:
        default = BORROW_SOURCE_OWN
    errors: dict[str, str] | None = None
    while True:
        values = await session.form(
            [
                ConfigEntry(
                    key=CONF_YM_INSTANCE,
                    type=ConfigEntryType.STRING,
                    required=True,
                    default_value=default,
                    options=[
                        *(
                            ConfigValueOption(value=inst_id, title=f"Yandex Music: {name}")
                            for inst_id, name in instances
                        ),
                        ConfigValueOption(value=BORROW_SOURCE_OWN),
                    ],
                )
            ],
            step_id="user",
            errors=errors,
        )
        source = str(values[CONF_YM_INSTANCE])
        if source == BORROW_SOURCE_OWN:
            break
        try:
            x_token = borrowed_x_token(session.mass, source)
        except LoginFailed as err:
            errors = {"base": str(err)}
            default = source
            continue
        data.update({CONF_YM_INSTANCE: source, CONF_AUTH_X_TOKEN: None, CONF_AUTH_USER_NAME: None})
        return x_token

    creds = await _sign_in(session)
    x_token = creds.x_token.get_secret()
    data.update(
        {
            CONF_YM_INSTANCE: BORROW_SOURCE_OWN,
            CONF_AUTH_X_TOKEN: x_token,
            CONF_AUTH_USER_NAME: creds.display_login or "",
        }
    )
    return x_token


async def _stored_x_token(session: SetupSession, data: dict[str, ConfigValueType]) -> str:
    """Return the stored account's x_token, signing in again when it is missing."""
    source = data.get(CONF_YM_INSTANCE)
    if is_borrowing(source):
        try:
            return borrowed_x_token(session.mass, str(source))
        except LoginFailed:
            return await _account(session, data)
    if x_token := str(data.get(CONF_AUTH_X_TOKEN) or ""):
        return x_token
    return await _account(session, data)


async def _sign_in(session: SetupSession) -> Credentials:
    """Collect a login method and sign in to Yandex Passport."""
    errors: dict[str, str] | None = None
    while True:
        values = await session.form(
            [
                ConfigEntry(
                    key=CONF_METHOD,
                    type=ConfigEntryType.STRING,
                    required=True,
                    default_value=METHOD_DEVICE,
                    options=[
                        ConfigValueOption(value=METHOD_DEVICE),
                        ConfigValueOption(value=METHOD_QR),
                        ConfigValueOption(value=METHOD_COOKIES),
                    ],
                )
            ],
            step_id="method",
            errors=errors,
        )
        method = str(values[CONF_METHOD])
        try:
            if method == METHOD_COOKIES:
                return await _cookie_login(session)
            if method == METHOD_QR:
                return await _qr_login(session)
            return await _device_login(session)
        except YaPassportError as err:
            errors = {"base": str(err)}


async def _cookie_login(session: SetupSession) -> Credentials:
    """Collect pasted browser cookies and exchange them for credentials."""
    errors: dict[str, str] | None = None
    while True:
        values = await session.form(
            [ConfigEntry(key=CONF_COOKIES, type=ConfigEntryType.SECURE_STRING, required=True)],
            step_id="cookies",
            errors=errors,
        )
        try:
            return await login_with_cookies(str(values[CONF_COOKIES]))
        except (InvalidDataError, LoginFailed, ResourceTemporarilyUnavailable) as err:
            errors = {"base": str(err)}


async def _device_login(session: SetupSession) -> Credentials:
    """Run the Yandex Passport device-code login, minting a fresh code on expiry."""
    async with PassportClient.create(config=ClientConfig()) as client:
        while True:
            device = await client.start_device_login(device_name=_DEVICE_NAME)
            try:
                return await session.external_until(
                    # the poll outlives the step so the step's countdown (StepExpiredError,
                    # then a fresh code) always wins the race
                    client.poll_device_until_confirmed(
                        device, total_timeout=float(device.expires_in) + 60
                    ),
                    url=device.verification_url,
                    step_id="device_login",
                    expires_in=float(device.expires_in),
                    translation_params=[device.user_code],
                )
            except StepExpiredError, DeviceCodeTimeoutError:
                continue
            except InvalidCredentialsError as err:
                raise AbortFlow("login_denied") from err


async def _qr_login(session: SetupSession) -> Credentials:
    """Run the Yandex Passport QR login, minting a fresh code on expiry."""
    ttl = ClientConfig().qr_poll_total_timeout_seconds
    async with PassportClient.create(config=ClientConfig()) as client:
        while True:
            qr = await client.start_qr_login()
            try:
                return await session.progress_until(
                    client.poll_qr_until_confirmed(qr, total_timeout=ttl),
                    step_id="scan_qr",
                    text="scan_qr",
                    image=_qr_image(qr.qr_url),
                    expires_in=int(ttl),
                )
            except StepExpiredError, QRTimeoutError:
                continue
            except InvalidCredentialsError as err:
                raise AbortFlow("login_denied") from err


# ---------------------------------------------------------------------------
# Skill
# ---------------------------------------------------------------------------


async def _skill(session: SetupSession, data: dict[str, ConfigValueType], x_token: str) -> None:
    """Create the skill automatically or register a hand-made one."""
    manifest = await asyncio.to_thread(SkillManifestProvider, session.mass)
    artifacts = load_artifacts(str(data.get(CONF_DIALOG_AUTO_CREATE_ARTIFACTS) or "") or None)
    default_url = str(data.get(CONF_EXTERNAL_BASE_URL) or "") or (
        try_detect_public_https_url(session.mass) or ""
    )
    defaults: dict[str, ConfigValueType] = {
        CONF_SKILL_MODE: SKILL_MODE_CREATE,
        CONF_DIALOG_SKILL_NAME: str(data.get(CONF_DIALOG_SKILL_NAME) or DIALOG_DEFAULT_NAME),
        CONF_EXTERNAL_BASE_URL: default_url,
        CONF_DIALOG_SKILL_ID: "",
    }
    errors: dict[str, str] | None = None
    while True:
        values = await session.form(_skill_entries(defaults), step_id="skill", errors=errors)
        defaults = {**defaults, **values}
        name = str(values.get(CONF_DIALOG_SKILL_NAME) or "").strip() or DIALOG_DEFAULT_NAME
        base_url = str(values.get(CONF_EXTERNAL_BASE_URL) or "").strip().rstrip("/")
        if not is_public_https_url(base_url):
            errors = {CONF_EXTERNAL_BASE_URL: "invalid_base_url"}
            continue
        backend_uri = build_backend_uri(base_url, str(data[CONF_DIALOG_WEBHOOK_SECRET]))
        data.update({CONF_DIALOG_SKILL_NAME: name, CONF_EXTERNAL_BASE_URL: base_url})

        if values.get(CONF_SKILL_MODE) == SKILL_MODE_EXISTING:
            skill_id = str(values.get(CONF_DIALOG_SKILL_ID) or "").strip()
            if not skill_id:
                errors = {CONF_DIALOG_SKILL_ID: "skill_id_required"}
                continue
            data[CONF_DIALOG_SKILL_ID] = skill_id
            data[CONF_DIALOG_PUBLICATION_STATUS] = ""
            await session.form(
                [
                    ConfigEntry(
                        key=CONF_WEBHOOK_URL,
                        type=ConfigEntryType.STRING,
                        required=False,
                        default_value=backend_uri,
                        read_only=True,
                    )
                ],
                step_id="webhook_info",
                last_step=True,
                translation_params=[backend_uri],
            )
            return

        args = deploy_args(manifest, skill_name=name, backend_uri=backend_uri)
        outcome = await session.progress_until(
            run_create_skill(cached_x_token=x_token, artifacts=artifacts, **args),
            step_id="create_skill",
            text="create_skill",
        )
        if outcome.stage == LocalAutoCreateStage.DUPLICATE_DETECTED:
            resolved = await _resolve_duplicate(session, outcome, x_token, args)
            if resolved is None:
                errors = None
                continue
            outcome = resolved
        artifacts = outcome.artifacts
        if outcome.stage == LocalAutoCreateStage.DONE and artifacts.skill_id:
            await _store_created(data, outcome, x_token)
            return
        if outcome.x_token == "":
            # Yandex rejected the token: sign in again, then retry the same step
            x_token = await _account(session, data)
            errors = None
            continue
        errors = {"base": outcome.user_message}


async def _resolve_duplicate(
    session: SetupSession,
    outcome: AutoCreateOutcome,
    x_token: str,
    args: dict[str, Any],
) -> AutoCreateOutcome | None:
    """Ask how to handle an existing same-name skill; None means rename."""
    existing_id = outcome.pending_duplicate_skill_id or ""
    values = await session.form(
        [
            ConfigEntry(
                key=CONF_DUPLICATE_CHOICE,
                type=ConfigEntryType.STRING,
                required=True,
                default_value=DUPLICATE_ADOPT,
                options=[
                    ConfigValueOption(value=DUPLICATE_ADOPT),
                    ConfigValueOption(value=DUPLICATE_RECREATE),
                    ConfigValueOption(value=DUPLICATE_RENAME),
                ],
            )
        ],
        step_id="duplicate",
        translation_params=[outcome.pending_duplicate_skill_name or "", existing_id],
    )
    choice = values[CONF_DUPLICATE_CHOICE]
    if choice == DUPLICATE_RENAME:
        return None
    resolver = (
        adopt_existing_skill if choice == DUPLICATE_ADOPT else delete_existing_skill_then_recreate
    )
    return await session.progress_until(
        resolver(cached_x_token=x_token, existing_skill_id=existing_id, **args),
        step_id="create_skill",
        text="create_skill",
    )


async def _store_created(
    data: dict[str, ConfigValueType], outcome: AutoCreateOutcome, x_token: str
) -> None:
    """Persist a successfully created skill and its first publication status."""
    skill_id = outcome.artifacts.skill_id or ""
    data[CONF_DIALOG_SKILL_ID] = skill_id
    data[CONF_DIALOG_AUTO_CREATE_ARTIFACTS] = dump_artifacts(outcome.artifacts)
    data[CONF_DIALOG_PUBLICATION_STATUS] = (
        await fetch_skill_publication_status(x_token, skill_id) or ""
    )


async def _edit_skill(
    session: SetupSession, data: dict[str, ConfigValueType], x_token: str
) -> None:
    """Edit the skill name, extra activation phrases and voice, and push them to Yandex."""
    manifest = await asyncio.to_thread(SkillManifestProvider, session.mass)
    artifacts = load_artifacts(str(data.get(CONF_DIALOG_AUTO_CREATE_ARTIFACTS) or "") or None)
    if not artifacts.skill_id and (skill_id := str(data.get(CONF_DIALOG_SKILL_ID) or "")):
        artifacts = SkillCreationArtifacts(state=SkillCreationState.DONE, skill_id=skill_id)
    if not is_public_https_url(str(data.get(CONF_EXTERNAL_BASE_URL) or "")):
        await _ask_base_url(session, data, offer_new_secret=False)
    backend_uri = build_backend_uri(
        str(data.get(CONF_EXTERNAL_BASE_URL) or ""), str(data[CONF_DIALOG_WEBHOOK_SECRET])
    )
    errors: dict[str, str] | None = None
    while True:
        values = await session.form(_edit_entries(data), step_id="edit_skill", errors=errors)
        name = str(values.get(CONF_DIALOG_SKILL_NAME) or "").strip() or DIALOG_DEFAULT_NAME
        extra = [str(values.get(key) or "").strip() for key in _PHRASE_KEYS]
        voice = str(values.get(CONF_DIALOG_SKILL_VOICE) or "") or DIALOG_VOICE_DEFAULT
        outcome = await session.progress_until(
            run_auto_update(
                cached_x_token=x_token,
                artifacts=artifacts,
                voice=voice,
                **deploy_args(
                    manifest,
                    skill_name=name,
                    backend_uri=backend_uri,
                    activation_phrases=[name, *(phrase for phrase in extra if phrase)],
                ),
            ),
            step_id="update_skill",
            text="update_skill",
        )
        if outcome.artifacts.state == SkillCreationState.DONE:
            data.update(
                {
                    CONF_DIALOG_SKILL_NAME: name,
                    CONF_DIALOG_SKILL_VOICE: voice,
                    CONF_DIALOG_AUTO_CREATE_ARTIFACTS: dump_artifacts(outcome.artifacts),
                    **dict(zip(_PHRASE_KEYS, extra, strict=True)),
                }
            )
            return
        if outcome.x_token == "":
            x_token = await _account(session, data)
            errors = None
            continue
        errors = {"base": outcome.user_message}


async def _webhook(session: SetupSession, data: dict[str, ConfigValueType]) -> None:
    """Change the public base URL or the webhook secret and re-register the backend."""
    await _ask_base_url(session, data, offer_new_secret=True)
    if not data.get(CONF_DIALOG_SKILL_ID):
        return
    x_token = await _stored_x_token(session, data)
    manifest = await asyncio.to_thread(SkillManifestProvider, session.mass)
    artifacts = load_artifacts(str(data.get(CONF_DIALOG_AUTO_CREATE_ARTIFACTS) or "") or None)
    if not artifacts.skill_id:
        artifacts = SkillCreationArtifacts(
            state=SkillCreationState.DONE, skill_id=str(data[CONF_DIALOG_SKILL_ID])
        )
    name = str(data.get(CONF_DIALOG_SKILL_NAME) or DIALOG_DEFAULT_NAME)
    phrases = [name, *(str(data.get(key) or "") for key in _PHRASE_KEYS if data.get(key))]
    backend_uri = build_backend_uri(
        str(data[CONF_EXTERNAL_BASE_URL]), str(data[CONF_DIALOG_WEBHOOK_SECRET])
    )
    while True:
        outcome = await session.progress_until(
            run_auto_update(
                cached_x_token=x_token,
                artifacts=artifacts,
                voice=str(data.get(CONF_DIALOG_SKILL_VOICE) or "") or None,
                **deploy_args(
                    manifest,
                    skill_name=name,
                    backend_uri=backend_uri,
                    activation_phrases=phrases,
                ),
            ),
            step_id="update_skill",
            text="update_skill",
        )
        if outcome.artifacts.state == SkillCreationState.DONE:
            data[CONF_DIALOG_AUTO_CREATE_ARTIFACTS] = dump_artifacts(outcome.artifacts)
            return
        if outcome.x_token != "":
            raise AbortFlow("update_failed")
        x_token = await _account(session, data)


async def _ask_base_url(
    session: SetupSession, data: dict[str, ConfigValueType], *, offer_new_secret: bool
) -> None:
    """Ask for the public HTTPS base URL (and optionally a new webhook secret)."""
    entries = [
        ConfigEntry(
            key=CONF_EXTERNAL_BASE_URL,
            type=ConfigEntryType.STRING,
            required=True,
            default_value=str(data.get(CONF_EXTERNAL_BASE_URL) or "")
            or (try_detect_public_https_url(session.mass) or ""),
        )
    ]
    if offer_new_secret:
        entries.append(
            ConfigEntry(
                key=CONF_NEW_SECRET,
                type=ConfigEntryType.BOOLEAN,
                required=False,
                default_value=False,
            )
        )
    errors: dict[str, str] | None = None
    while True:
        values = await session.form(entries, step_id="webhook", errors=errors)
        base_url = str(values.get(CONF_EXTERNAL_BASE_URL) or "").strip().rstrip("/")
        if is_public_https_url(base_url):
            break
        errors = {CONF_EXTERNAL_BASE_URL: "invalid_base_url"}
    data[CONF_EXTERNAL_BASE_URL] = base_url
    if values.get(CONF_NEW_SECRET):
        data[CONF_DIALOG_WEBHOOK_SECRET] = generate_webhook_secret()


# ---------------------------------------------------------------------------
# Entries and images
# ---------------------------------------------------------------------------


def _skill_entries(defaults: dict[str, ConfigValueType]) -> list[ConfigEntry]:
    """Build the skill step: create vs existing, name, base URL and (existing) skill id."""
    return [
        ConfigEntry(
            key=CONF_SKILL_MODE,
            type=ConfigEntryType.STRING,
            required=True,
            default_value=defaults[CONF_SKILL_MODE],
            options=[
                ConfigValueOption(value=SKILL_MODE_CREATE),
                ConfigValueOption(value=SKILL_MODE_EXISTING),
            ],
        ),
        ConfigEntry(
            key=CONF_DIALOG_SKILL_NAME,
            type=ConfigEntryType.STRING,
            required=True,
            default_value=defaults[CONF_DIALOG_SKILL_NAME],
            validate=validate_skill_name,
        ),
        ConfigEntry(
            key=CONF_EXTERNAL_BASE_URL,
            type=ConfigEntryType.STRING,
            required=True,
            default_value=defaults[CONF_EXTERNAL_BASE_URL],
        ),
        ConfigEntry(
            key=CONF_DIALOG_SKILL_ID,
            type=ConfigEntryType.STRING,
            required=False,
            default_value=defaults[CONF_DIALOG_SKILL_ID],
            depends_on=CONF_SKILL_MODE,
            depends_on_value=SKILL_MODE_EXISTING,
        ),
    ]


def _edit_entries(data: dict[str, ConfigValueType]) -> list[ConfigEntry]:
    """Build the edit step from the stored skill settings."""
    return [
        ConfigEntry(
            key=CONF_DIALOG_SKILL_NAME,
            type=ConfigEntryType.STRING,
            required=True,
            default_value=str(data.get(CONF_DIALOG_SKILL_NAME) or DIALOG_DEFAULT_NAME),
            validate=validate_skill_name,
        ),
        *(
            ConfigEntry(
                key=key,
                type=ConfigEntryType.STRING,
                required=False,
                default_value=str(data.get(key) or ""),
                validate=validate_activation_phrase,
            )
            for key in _PHRASE_KEYS
        ),
        ConfigEntry(
            key=CONF_DIALOG_SKILL_VOICE,
            type=ConfigEntryType.STRING,
            required=False,
            default_value=str(data.get(CONF_DIALOG_SKILL_VOICE) or DIALOG_VOICE_DEFAULT),
            options=[
                ConfigValueOption(title=label, value=value) for value, label in DIALOG_VOICE_OPTIONS
            ],
        ),
    ]


def _qr_image(qr_url: str) -> str:
    """Render a QR-login URL as an SVG data URI."""
    return str(segno.make(qr_url, error="m").svg_data_uri(scale=4))
