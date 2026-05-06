"""Constants for the Yandex Alice (Dialogs custom skill) plugin provider."""

from __future__ import annotations

import logging
import os
from typing import cast

from ya_dialogs_api import DIALOG_CHANNEL as _LIB_DIALOG_CHANNEL
from ya_dialogs_api import Channel

_LOGGER = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Config entry keys (user-facing)
# ---------------------------------------------------------------------------
CONF_INSTANCE_NAME = "instance_name"
# Override for MA's webserver Base URL — used when generating callback /
# webhook URLs for Yandex. Lets users keep MA's global Base URL unset (so
# HA Ingress / local access keep working) while still exposing a public
# HTTPS URL only to Yandex via a reverse proxy.
CONF_EXTERNAL_BASE_URL = "external_base_url"
CONF_EXPOSED_PLAYERS = "exposed_players"
CONF_EXPOSED_PLAYLISTS = "exposed_playlists"

# Cached Yandex Passport x_token from the first successful Device Flow.
# Reused on subsequent auto-create / rename runs so the user doesn't have
# to re-confirm the device code every time. Long-lived (months);
# automatically refreshed on use. Cleared if Yandex returns 401 on refresh.
CONF_AUTH_X_TOKEN = "auth_x_token"

# Dialog skill (Yandex Dialogs custom skill — voice playback)
CONF_DIALOG_SKILL_ENABLED = "dialog_skill_enabled"
CONF_DIALOG_SKILL_NAME = "dialog_skill_name"
CONF_DIALOG_SKILL_ID = "dialog_skill_id"
CONF_DIALOG_SKILL_TOKEN = "dialog_skill_token"
CONF_DIALOG_WEBHOOK_SECRET = "dialog_webhook_secret"
CONF_DIALOG_AUTO_CREATE_ARTIFACTS = "dialog_auto_create_artifacts"
CONF_DIALOG_AUTO_CREATE_SESSION_ID = "dialog_auto_create_session_id"
# Persisted DeviceCodeSession (JSON) so the auto-create button can advance
# the Device Flow state machine across multiple clicks. Cleared after a
# successful poll, on expiry, or on Cancel.
CONF_DIALOG_AUTO_CREATE_DEVICE_SESSION = "dialog_auto_create_device_session"

# ---------------------------------------------------------------------------
# Config actions (config-flow buttons)
# ---------------------------------------------------------------------------
CONF_ACTION_AUTO_CREATE_DIALOG = "auto_create_dialog_skill"
CONF_ACTION_RENAME_DIALOG_SKILL = "rename_dialog_skill"
# Cancel an in-flight Device Flow / drop partial artifacts. Visible only when
# DEVICE_FLOW_STARTED or FAILED. Cached x_token is preserved across cancel.
CONF_ACTION_CANCEL_DIALOG_SKILL_FLOW = "cancel_dialog_skill_flow"

# ---------------------------------------------------------------------------
# Webhook routing
# ---------------------------------------------------------------------------
DIALOG_WEBHOOK_BASE_PATH = "/api/yandex_dialogs/webhook"
# Maximum time the dialogs webhook handler may spend resolving / dispatching
# before it must return a response. Yandex's Alice Dialogs protocol enforces
# a 3-second hard cap; we leave 0.5s of headroom.
DIALOG_RESOLVE_TIMEOUT = 2.5

# ---------------------------------------------------------------------------
# Dialog skill metadata defaults
# ---------------------------------------------------------------------------
DIALOG_DEFAULT_NAME = "Music Assistant"
# Yandex Dialogs app-store-api channel string for the custom dialog skill.
# Captured from dev console DevTools (POST /apps): channel="aliceSkill".
# Override via MA_YANDEX_DIALOG_CHANNEL env var if Yandex changes the contract.
# Validated against ya_dialogs_api.Channel — invalid values fall back to the
# library default with a warning rather than producing a silent type lie.
_dialog_channel_raw = os.environ.get("MA_YANDEX_DIALOG_CHANNEL", _LIB_DIALOG_CHANNEL)
if _dialog_channel_raw not in ("smartHome", "aliceSkill"):
    _LOGGER.warning(
        "MA_YANDEX_DIALOG_CHANNEL=%r is not a recognised Yandex Channel "
        "wire value; falling back to %r",
        _dialog_channel_raw,
        _LIB_DIALOG_CHANNEL,
    )
    _dialog_channel_raw = _LIB_DIALOG_CHANNEL
DIALOG_CHANNEL: Channel = cast("Channel", _dialog_channel_raw)
DIALOG_NAME_MIN_LEN = 2
DIALOG_NAME_MAX_LEN = 64

# ---------------------------------------------------------------------------
# Yandex Passport / Dialogs reference URLs
# ---------------------------------------------------------------------------
YANDEX_DIALOGS_DEVELOPER_URL = "https://dialogs.yandex.ru/developer"
YANDEX_OAUTH_URL = (
    "https://oauth.yandex.ru/authorize?response_type=token"
    "&client_id=c473ca268cd749d3a8371351a8f2bcbd"
)
