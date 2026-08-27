"""Setup flow for linking Ynison to one configured Yandex Music account."""

from __future__ import annotations

from typing import TYPE_CHECKING

from music_assistant_models.config_entries import ConfigEntry, ConfigValueOption
from music_assistant_models.enums import ConfigEntryType

from music_assistant.helpers.config_entries import create_player_selector
from music_assistant.models.setup_flow import AbortFlow, SetupFlowError, StepExpiredError

from .config_helpers import list_yandex_music_instances
from .constants import (
    CONF_MASS_PLAYER_ID,
    CONF_REMEMBER_SESSION,
    CONF_TOKEN,
    CONF_X_TOKEN,
    CONF_YM_INSTANCE,
    YM_INSTANCE_OWN,
)

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ConfigValueType

    from music_assistant.models.setup_flow import SetupSession


async def run_setup(session: SetupSession) -> None:
    """
    Run the Ynison setup flow: pick the account source, then borrow or QR-log in.

    :param session: The setup session driving the flow.
    """
    if not session.mass.players.all_players(False, False):
        raise AbortFlow("no_players")
    ym_instances = list_yandex_music_instances(session.mass)
    valid_sources = {inst_id for inst_id, _ in ym_instances}
    setup_data = dict(session.context.setup_data)
    prefill: dict[str, ConfigValueType] = {**session.context.values, **setup_data}
    default_source = str(prefill.get(CONF_YM_INSTANCE) or YM_INSTANCE_OWN)
    if default_source != YM_INSTANCE_OWN and default_source not in valid_sources:
        default_source = YM_INSTANCE_OWN
    default_player = prefill.get(CONF_MASS_PLAYER_ID)

    errors: dict[str, str] | None = None
    while True:
        submitted = await session.form(
            [
                _source_entry(selected_source, ym_instances),
                create_player_selector(
                    session.mass,
                    CONF_MASS_PLAYER_ID,
                    default_player,
                ),
            ],
            step_id="user",
            errors=errors,
            last_step=True,
        )
        source = str(values[CONF_YM_INSTANCE])
        remember = bool(values[CONF_REMEMBER_SESSION])
        default_player = values[CONF_MASS_PLAYER_ID]
        identity: dict[str, ConfigValueType] = {
            CONF_MASS_PLAYER_ID: default_player,
        }
        if source != YM_INSTANCE_OWN:
            # borrow mode: the linked Yandex Music instance owns authentication
            try:
                await session.finish({CONF_YM_INSTANCE: source, **identity})
                return
            except SetupFlowError as err:
                errors = {"base": err.translation_key or str(err)}
                default_source = source
                continue
        # own credentials: QR login
        try:
            creds = await _qr_login(session)
        except YaPassportError as err:
            errors = {"base": str(err)}
            continue
        if creds.music_token is None:
            errors = {"base": "no_music_token"}
            continue
        collected: dict[str, ConfigValueType] = {
            CONF_YM_INSTANCE: YM_INSTANCE_OWN,
            CONF_TOKEN: creds.music_token.get_secret(),
            CONF_X_TOKEN: creds.x_token.get_secret() if remember else None,
            CONF_ACCOUNT_LOGIN: creds.display_login,
            **identity,
        }
        try:
            await session.finish(collected)
            return
        except SetupFlowError as err:
            errors = {"base": err.translation_key or str(err)}
            setup_data = collected


def _source_entry(
    selected_source: str | None,
    ym_instances: list[tuple[str, str]],
) -> ConfigEntry:
    """Build the required linked Yandex Music provider selector."""
    return ConfigEntry(
        key=CONF_YM_INSTANCE,
        type=ConfigEntryType.STRING,
        required=True,
        default_value=selected_source,
        value=selected_source,
        options=[
            ConfigValueOption(value=instance_id, title=f"Yandex Music: {name}")
            for instance_id, name in ym_instances
        ],
    )
