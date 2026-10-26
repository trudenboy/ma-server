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

# Cached Yandex Passport x_token from the first successful Device Flow.
# Reused on subsequent auto-create / rename runs so the user doesn't have
# to re-confirm the device code every time. Long-lived (months);
# automatically refreshed on use. Cleared if Yandex returns 401 on refresh.
CONF_AUTH_X_TOKEN = "auth_x_token"
# Display name of the signed-in Yandex account (login or display name).
# Surfaced as a "Authorized as <name>" banner once auth is complete; not
# used for any API call.
CONF_AUTH_USER_NAME = "auth_user_name"

# Dialog skill (Yandex Dialogs custom skill — voice playback)
CONF_DIALOG_SKILL_NAME = "dialog_skill_name"
CONF_DIALOG_SKILL_ID = "dialog_skill_id"
CONF_DIALOG_WEBHOOK_SECRET = "dialog_webhook_secret"
CONF_DIALOG_AUTO_CREATE_ARTIFACTS = "dialog_auto_create_artifacts"
# v1.2.0 — Yandex skill publication status, classified into one of:
# ``on_air`` / ``in_moderation`` / ``draft`` / ``rejected`` / ``unknown``.
# Refreshed once after every successful Create / Update / Adopt /
# Recreate / Refresh-status action. Read by Step 3 to render the
# moderation banner without making an HTTP call on every render.
CONF_DIALOG_PUBLICATION_STATUS = "dialog_publication_status"

# Yandex account source: instance_id of a linked yandex_music provider to
# borrow the x_token from, or the shared "__own__" sentinel for this
# plugin's own sign-in (key name matches the other yandex providers).
CONF_YM_INSTANCE = "ym_instance"

# ---------------------------------------------------------------------------
# Options-page actions
# ---------------------------------------------------------------------------
CONF_ACTION_DELETE_SKILL = "delete_skill"
# Options-page button: fetch the live publication status from Yandex Dialogs
# and store it for the status banner.
CONF_ACTION_REFRESH_STATUS = "refresh_status"
# Test webhook reachability — outgoing POST to verify DNS + TLS + reverse proxy.
CONF_ACTION_TEST_WEBHOOK = "test_webhook_reachability"

# Options-page button: open the skill in the Yandex Dialogs dev console.
CONF_ACTION_OPEN_DEV_CONSOLE = "open_dev_console"


# v1.6.0 — file-based skill manifest override + UI actions.
# Override lives at ``<storage>/yandex_alice/skill.toml``. UI exposes
# four actions (export / import / reset / validate) plus a paste
# field for browser-based import; banner reflects bundled vs override.
CONF_ACTION_EXPORT_MANIFEST = "export_manifest"
CONF_ACTION_IMPORT_MANIFEST = "import_manifest"
CONF_ACTION_RESET_MANIFEST = "reset_manifest"
CONF_ACTION_VALIDATE_MANIFEST = "validate_manifest"
CONF_DIALOG_SKILL_OVERRIDE_PASTE = "dialog_skill_override_paste"

# Voice + activation phrases, edited in the reconfigure flow (otherwise auto-derived).
# Yandex Dialogs allows up to **three** alternative activation phrases
# in addition to the skill name itself (which is the first phrase).
# Each must be at least 2 words just like the skill name; empty slots
# are skipped when assembling the payload sent to Yandex.
CONF_DIALOG_SKILL_VOICE = "dialog_skill_voice"
CONF_DIALOG_ACTIVATION_PHRASE_2 = "dialog_activation_phrase_2"
CONF_DIALOG_ACTIVATION_PHRASE_3 = "dialog_activation_phrase_3"
CONF_DIALOG_ACTIVATION_PHRASE_4 = "dialog_activation_phrase_4"

# Toggle: split-personality between MA "Instance name" (internal) and Yandex
# "Skill name" (user-facing voice trigger). Default merged — both come from
# CONF_DIALOG_SKILL_NAME. Power users can flip this to expose a separate
# CONF_INSTANCE_NAME field.
CONF_USE_DIFFERENT_INSTANCE_NAME = "use_different_instance_name"

# Toggle: keep the conversation open after a play / control success (P1.4).
# Default OFF — historical voice-UX where the skill ends the session and
# the user re-says "Алиса, попроси <name>" for the next command. ON keeps
# `end_session=false` after success so follow-ups skip the activation
# preamble at the cost of a "skill is listening" indicator on screened
# surfaces. Explicit "стоп / останови / выключи / выключи музыку" still
# end the session via the existing `stop` control intent (matched by
# `parse_control` patterns in `dialogs_control.py`).
CONF_DIALOG_VOICE_CONTINUATION = "dialog_voice_continuation"

# Yandex Dialogs catalog voice options (TTS), passed to draft payload.
# Wire values + display names extracted live from the dev console
# (https://dialogs.yandex.ru/developer → skill → Голос dropdown) on
# 2026-05-07; other strings will be rejected by the draft PATCH.
# Voice selection rarely matters for voice-control skills (the user
# hears Alice, not the skill's TTS), but we expose it for completeness.
DIALOG_VOICE_OPTIONS: tuple[tuple[str, str], ...] = (
    ("good_oksana", "Oksana (default)"),
    ("jane", "Jane"),
    ("zahar", "Zakhar"),
    ("ermil", "Yermil"),
    ("erkanyavas", "Erkan Yavas"),
    ("shitova.us", "Alisa"),
    ("kostya.gpu", "Kostya"),
    ("valtz.gpu", "Filipp"),
    ("tatyana_abramova.gpu", "Anya"),
)
DIALOG_VOICE_DEFAULT = "good_oksana"

# ---------------------------------------------------------------------------
# Form categories (progressive disclosure)
# ---------------------------------------------------------------------------
CATEGORY_AUTHORIZATION = "Authorization"
CATEGORY_SKILL = "Skill"
# Frontend renders ``settings.category.{slug}`` and falls back to the
# raw slug when no translation exists — using TitleCase slugs gives
# us readable section headers ("Authorization" / "Skill" / "Settings")
# without shipping i18n. Avoid the bare slug ``"settings"`` because
# the frontend reserves that for the top-level Settings page.
CATEGORY_SETTINGS = "Settings"
CATEGORY_ADVANCED = "advanced"

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

# ---------------------------------------------------------------------------
# AI answers
# ---------------------------------------------------------------------------
CONF_AI_FALLBACK = "ai_fallback"
CONF_AI_ENGINE = "ai_engine"
# How long the webhook waits for the AI engine before replying "думаю" and
# delivering the answer on the next turn; stays inside Yandex's 3 s cap.
AI_INLINE_TIMEOUT = 2.3
AI_QUERY_TIMEOUT = 30
AI_MAX_POLLS = 2
