"""
Provider options page.

The one-time input (account, sign-in, skill creation) lives in the setup
flow; this page shows what was set up and keeps the things a user changes or
triggers later:

* **Authorization** — which Yandex account the provider acts as.
* **Skill** — the registered skill, its publication status, the webhook URL,
  and the buttons that need no input (refresh status, test webhook, open the
  developer console, delete the skill).
* **Settings** — voice-controllable players, the instance name override and
  the skill manifest tools.
* **Advanced** — webhook diagnostics.

All user-facing entry texts live in ``strings.json`` under
``config_entries.<key>.<field>``; dynamic fragments flow through
``translation_params`` (``{0}``/``{1}`` placeholders).
"""

from __future__ import annotations

import dataclasses
import time
from typing import TYPE_CHECKING, Any

from music_assistant_models.config_entries import ConfigEntry, ConfigValueOption
from music_assistant_models.enums import ConfigEntryType

from .constants import (
    CATEGORY_AUTHORIZATION,
    CATEGORY_SETTINGS,
    CATEGORY_SKILL,
    CONF_ACTION_DELETE_SKILL,
    CONF_ACTION_EXPORT_MANIFEST,
    CONF_ACTION_IMPORT_MANIFEST,
    CONF_ACTION_OPEN_DEV_CONSOLE,
    CONF_ACTION_REFRESH_STATUS,
    CONF_ACTION_RESET_MANIFEST,
    CONF_ACTION_TEST_WEBHOOK,
    CONF_ACTION_VALIDATE_MANIFEST,
    CONF_AI_FALLBACK,
    CONF_DIALOG_SKILL_OVERRIDE_PASTE,
    CONF_EXPOSED_PLAYERS,
    CONF_INSTANCE_NAME,
    CONF_USE_DIFFERENT_INSTANCE_NAME,
    DIALOG_DEFAULT_NAME,
)
from .publication_status import (
    STATUS_DRAFT,
    STATUS_IN_MODERATION,
    STATUS_ON_AIR,
    STATUS_REJECTED,
)

if TYPE_CHECKING:
    from collections.abc import Iterable

    from .skill_manifest_provider import ManifestStatus

__all__ = ["AccountStatus", "SkillStatus", "build_options_entries", "diagnostics_entries"]


@dataclasses.dataclass(frozen=True, slots=True)
class AccountStatus:
    """Which Yandex account the provider acts as."""

    borrowed_from: str | None
    """Display name of the linked Yandex Music instance, when borrowing."""

    user_name: str
    """Display login of the provider's own account ("" when unknown)."""

    signed_in: bool
    """True when a usable x_token is available (own or borrowed)."""


@dataclasses.dataclass(frozen=True, slots=True)
class SkillStatus:
    """The registered Yandex Dialogs skill as stored in setup data."""

    name: str
    skill_id: str
    publication_status: str
    webhook_url: str


def _publication_status_banner(
    status: str | None,
) -> tuple[str, ConfigEntryType]:
    """
    Map a classified publication status to a Step 3 banner.

    Returns ``(translation_key, entry_type)`` — the key selects the
    banner text from ``strings.json``. Negative / actionable states
    use :attr:`ConfigEntryType.ALERT` — the MA frontend renders these
    as a tonal amber ``v-alert`` to draw the eye. Neutral / success
    states use :attr:`ConfigEntryType.LABEL` (plain text) so the form
    doesn't scream at a user whose skill is fine.

    Color emoji prefixes carry the semantic differentiation since
    ALERT's color is hard-coded amber by the frontend:

    * ✅ on-air (success) — LABEL
    * ⏳ in-moderation (waiting) — ALERT
    * ❌ rejected (error) — ALERT
    * ⚠️ draft (needs user action) — ALERT
    * info icon: unknown / not yet fetched — LABEL
    """
    if status == STATUS_ON_AIR:
        return ("pub_status_on_air", ConfigEntryType.LABEL)
    if status == STATUS_IN_MODERATION:
        return ("pub_status_in_moderation", ConfigEntryType.ALERT)
    if status == STATUS_REJECTED:
        return ("pub_status_rejected", ConfigEntryType.ALERT)
    if status == STATUS_DRAFT:
        return ("pub_status_draft", ConfigEntryType.ALERT)
    return ("pub_status_unknown", ConfigEntryType.LABEL)


def _stamp(entries: Iterable[ConfigEntry], category: str) -> tuple[ConfigEntry, ...]:
    """Set ``category`` on every entry that hasn't picked one explicitly."""
    return tuple(
        entry
        if entry.category and entry.category != "generic"
        else dataclasses.replace(entry, category=category)
        for entry in entries
    )


def _account_block(account: AccountStatus) -> tuple[ConfigEntry, ...]:
    """Show the account the provider acts as, or how to sign in."""
    if account.borrowed_from is not None:
        return (
            ConfigEntry(
                key="label_auth_borrowed",
                type=ConfigEntryType.LABEL,
                translation_params=[account.borrowed_from],
            ),
        )
    if account.signed_in:
        if account.user_name:
            return (
                ConfigEntry(
                    key="label_auth_status",
                    type=ConfigEntryType.LABEL,
                    translation_params=[account.user_name],
                ),
            )
        return (ConfigEntry(key="label_auth_status_anonymous", type=ConfigEntryType.LABEL),)
    return (ConfigEntry(key="label_auth_signed_out", type=ConfigEntryType.ALERT),)


def _action(key: str, *, advanced: bool = False) -> ConfigEntry:
    return ConfigEntry(
        key=key,
        type=ConfigEntryType.ACTION,
        action=key,
        required=False,
        default_value="",
        advanced=advanced,
    )


def _skill_block(skill: SkillStatus, *, can_manage: bool) -> tuple[ConfigEntry, ...]:
    """Build the registered-skill card: status, webhook URL and the input-free actions."""
    if not skill.skill_id:
        return (ConfigEntry(key="label_skill_missing", type=ConfigEntryType.ALERT),)
    status_key, status_type = _publication_status_banner(skill.publication_status)
    entries: list[ConfigEntry] = [
        ConfigEntry(
            key="label_skill_registered",
            type=ConfigEntryType.LABEL,
            translation_params=[skill.name or DIALOG_DEFAULT_NAME, skill.skill_id],
        ),
        ConfigEntry(
            key="label_skill_publication_status",
            type=status_type,
            translation_key=status_key,
        ),
        ConfigEntry(
            key="label_webhook_url",
            type=ConfigEntryType.LABEL,
            translation_params=[skill.webhook_url],
        ),
        _action(CONF_ACTION_TEST_WEBHOOK),
        _action(CONF_ACTION_OPEN_DEV_CONSOLE),
    ]
    if can_manage:
        entries.append(_action(CONF_ACTION_REFRESH_STATUS))
        entries.append(_action(CONF_ACTION_DELETE_SKILL, advanced=True))
    return tuple(entries)


def diagnostics_entries(stats: dict[str, Any] | None) -> tuple[ConfigEntry, ...]:
    """
    Render webhook counters from the running provider.

    :param stats: The provider's diagnostics snapshot, or None before it loaded.
    """
    if not stats or not stats.get("handler_active"):
        return (
            ConfigEntry(
                key="label_diagnostics_inactive",
                type=ConfigEntryType.LABEL,
                advanced=True,
            ),
        )
    webhook_calls = int(stats.get("webhook_calls_total") or 0)
    authenticated_calls = int(stats.get("authenticated_calls_total") or 0)
    last_ts = stats.get("last_webhook_ts")
    if isinstance(last_ts, (int, float)) and last_ts > 0:
        delta = max(0, int(time.time() - last_ts))
        if delta < 60:
            last_ago = f"{delta} sec ago"
        elif delta < 3600:
            last_ago = f"{delta // 60} min ago"
        else:
            last_ago = f"{delta // 3600} h ago"
    else:
        last_ago = "never"
    return (
        ConfigEntry(
            key="label_diagnostics_summary",
            type=ConfigEntryType.LABEL,
            translation_params=[str(webhook_calls), str(authenticated_calls), last_ago],
            advanced=True,
        ),
    )


def _manifest_block(
    *,
    status: ManifestStatus,
    paste_value: str,
) -> tuple[ConfigEntry, ...]:
    """
    Skill manifest banner + Export / Import / Reset / Validate actions.

    The manifest controls intents + entities deployed to Yandex (skill
    grammar) and the runtime mapping that turns NLU matches into
    player actions. Bundled by default; users override by writing
    ``<storage>/yandex_alice/skill.toml`` (manually or via Import).
    """
    counts = [str(status.intent_count), str(status.entity_count)]
    if status.source == "bundled":
        banner_key = "manifest_banner_bundled"
        banner_params = [*counts, str(status.override_path)]
    elif status.source == "override_valid":
        banner_key = "manifest_banner_override"
        banner_params = [str(status.override_path), *counts]
    else:  # override_invalid
        banner_key = "manifest_banner_invalid"
        banner_params = [str(status.override_path), *counts, status.error or "unknown parse error"]

    entries: list[ConfigEntry] = [
        ConfigEntry(
            key="label_manifest_banner",
            type=ConfigEntryType.LABEL,
            translation_key=banner_key,
            translation_params=banner_params,
        ),
    ]
    entries.extend(
        (
            ConfigEntry(
                key=CONF_ACTION_EXPORT_MANIFEST,
                type=ConfigEntryType.ACTION,
                translation_params=[str(status.override_path)],
                action=CONF_ACTION_EXPORT_MANIFEST,
                required=False,
                default_value="",
            ),
            ConfigEntry(
                key=CONF_DIALOG_SKILL_OVERRIDE_PASTE,
                type=ConfigEntryType.STRING,
                required=False,
                value=paste_value,
                default_value="",
            ),
            ConfigEntry(
                key=CONF_ACTION_IMPORT_MANIFEST,
                type=ConfigEntryType.ACTION,
                translation_params=[str(status.override_path)],
                action=CONF_ACTION_IMPORT_MANIFEST,
                required=False,
                default_value="",
            ),
            ConfigEntry(
                key=CONF_ACTION_VALIDATE_MANIFEST,
                type=ConfigEntryType.ACTION,
                action=CONF_ACTION_VALIDATE_MANIFEST,
                required=False,
                default_value="",
            ),
            ConfigEntry(
                key=CONF_ACTION_RESET_MANIFEST,
                type=ConfigEntryType.ACTION,
                action=CONF_ACTION_RESET_MANIFEST,
                required=False,
                default_value="",
            ),
        )
    )
    return tuple(entries)


def _ai_block(engine_entries: tuple[ConfigEntry, ...]) -> tuple[ConfigEntry, ...]:
    """AI answers toggle followed by MA's engine picker (shown while the toggle is on)."""
    return (
        ConfigEntry(
            key=CONF_AI_FALLBACK,
            type=ConfigEntryType.BOOLEAN,
            required=False,
            default_value=False,
        ),
        *engine_entries,
    )


def _settings_block(
    *,
    player_options: list[ConfigValueOption],
    instance_name: str,
    skill_name: str,
    use_different_instance_name: bool,
) -> tuple[ConfigEntry, ...]:
    """Voice-controllable players + (advanced) MA-side instance name override."""
    entries: list[ConfigEntry] = [
        ConfigEntry(
            key=CONF_EXPOSED_PLAYERS,
            type=ConfigEntryType.STRING,
            multi_value=True,
            options=player_options,
            required=False,
            default_value=[],
        ),
        ConfigEntry(
            key=CONF_USE_DIFFERENT_INSTANCE_NAME,
            type=ConfigEntryType.BOOLEAN,
            required=False,
            default_value=False,
            advanced=True,
        ),
    ]
    if use_different_instance_name:
        entries.append(
            ConfigEntry(
                key=CONF_INSTANCE_NAME,
                type=ConfigEntryType.STRING,
                required=False,
                default_value=instance_name or DIALOG_DEFAULT_NAME,
                advanced=True,
                depends_on=CONF_USE_DIFFERENT_INSTANCE_NAME,
                depends_on_value=True,
            )
        )
    else:
        # Hidden tracker: keep instance_name in sync with skill_name
        # while the toggle is off, so MA's display label looks right.
        entries.append(
            ConfigEntry(
                key=CONF_INSTANCE_NAME,
                type=ConfigEntryType.STRING,
                required=False,
                default_value=skill_name or DIALOG_DEFAULT_NAME,
                hidden=True,
            )
        )
    return tuple(entries)


def build_options_entries(
    *,
    account: AccountStatus,
    skill: SkillStatus,
    player_options: list[ConfigValueOption],
    instance_name: str,
    use_different_instance_name: bool,
    manifest_status: ManifestStatus,
    manifest_paste: str,
    diagnostics: tuple[ConfigEntry, ...],
    ai_engine_entries: tuple[ConfigEntry, ...] = (),
) -> tuple[ConfigEntry, ...]:
    """
    Compose the options page.

    :param account: Which Yandex account the provider acts as.
    :param skill: The registered skill.
    :param player_options: Players the user can expose to voice control.
    :param instance_name: Current MA-side instance name.
    :param use_different_instance_name: Whether the instance name differs from the skill name.
    :param manifest_status: Bundled / override state of the skill manifest.
    :param manifest_paste: Saved manifest paste field value.
    :param diagnostics: Webhook diagnostics entries.
    :param ai_engine_entries: MA's AI engine picker, shown while AI answers are on.
    """
    return (
        *_stamp(_account_block(account), CATEGORY_AUTHORIZATION),
        *_stamp(_skill_block(skill, can_manage=account.signed_in), CATEGORY_SKILL),
        *_stamp(
            (
                *_settings_block(
                    player_options=player_options,
                    instance_name=instance_name,
                    skill_name=skill.name,
                    use_different_instance_name=use_different_instance_name,
                ),
                *_ai_block(ai_engine_entries),
                *_manifest_block(status=manifest_status, paste_value=manifest_paste),
            ),
            CATEGORY_SETTINGS,
        ),
        *diagnostics,
    )
