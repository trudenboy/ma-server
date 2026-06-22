r"""Provider settings form — clean rewrite with three top-level categories.

The form is grouped by *what the entry semantically belongs to*, not
by linear setup steps:

* **Authorization** — Yandex Passport sign-in / sign-out. Always
  visible. Primary CTA when no token is cached, signed-in banner +
  secondary "Sign out" otherwise.
* **Skill** — everything that touches the Yandex Dialogs skill
  itself: skill name, the public webhook URL, Create / Edit /
  Update / Delete buttons, the duplicate-name resolution prompt,
  the dev-console link, and (under *Show advanced*) the manual
  fields (skill_id, OAuth token, webhook secret) plus diagnostics.
  Visible whenever auth is done OR a manual ``skill_id`` is set.
* **Settings** — provider-wide tuning that does not relate to the
  skill object: voice-controllable players + the (advanced)
  "different MA-side instance name" toggle.

Each block is a small builder. The top-level
:func:`build_form_entries` composes the three of them, stamps every
entry with its category, and returns the flat tuple MA's frontend
expects.

Frontend sections render directly off ``ConfigEntry.category``: any
slug that is not ``generic`` / ``advanced`` / ``protocol_general``
becomes its own visual section, with the header text taken from
``settings.category.{slug}`` (with the slug itself as fallback). We
use TitleCase slugs so the fallback already reads as a proper
section heading without shipping a translation file.
"""

# ruff: noqa: RUF001, RUF002

from __future__ import annotations

import contextlib
import dataclasses
from typing import TYPE_CHECKING

from music_assistant_models.config_entries import ConfigEntry, ConfigValueOption
from music_assistant_models.enums import ConfigEntryType
from ya_dialogs_api import SkillCreationArtifacts, SkillCreationState

from .auto_create import AutoCreateOutcome, LocalAutoCreateStage
from .constants import (
    CATEGORY_AUTHORIZATION,
    CATEGORY_SETTINGS,
    CATEGORY_SKILL,
    CONF_ACTION_ADOPT_EXISTING,
    CONF_ACTION_AUTO_CREATE_DIALOG,
    CONF_ACTION_CANCEL_DIALOG_SKILL_FLOW,
    CONF_ACTION_CANCEL_EDIT,
    CONF_ACTION_CLEAR_AUTH,
    CONF_ACTION_DELETE_SKILL,
    CONF_ACTION_EDIT_SKILL,
    CONF_ACTION_EXPORT_MANIFEST,
    CONF_ACTION_IMPORT_MANIFEST,
    CONF_ACTION_RECREATE_DUPLICATE,
    CONF_ACTION_REFRESH_STATUS,
    CONF_ACTION_REGENERATE_WEBHOOK_SECRET,
    CONF_ACTION_RESET_MANIFEST,
    CONF_ACTION_SIGN_IN,
    CONF_ACTION_TEST_WEBHOOK,
    CONF_ACTION_UPDATE_SKILL,
    CONF_ACTION_VALIDATE_MANIFEST,
    CONF_DIALOG_ACTIVATION_PHRASE_2,
    CONF_DIALOG_ACTIVATION_PHRASE_3,
    CONF_DIALOG_ACTIVATION_PHRASE_4,
    CONF_DIALOG_SKILL_ID,
    CONF_DIALOG_SKILL_NAME,
    CONF_DIALOG_SKILL_OVERRIDE_PASTE,
    CONF_DIALOG_SKILL_TOKEN,
    CONF_DIALOG_SKILL_VOICE,
    CONF_DIALOG_WEBHOOK_SECRET,
    CONF_EXPOSED_PLAYERS,
    CONF_EXTERNAL_BASE_URL,
    CONF_INSTANCE_NAME,
    CONF_USE_DIFFERENT_INSTANCE_NAME,
    DIALOG_DEFAULT_NAME,
    DIALOG_VOICE_DEFAULT,
    DIALOG_VOICE_OPTIONS,
    DIALOG_WEBHOOK_BASE_PATH,
)
from .dialog_skill_meta import validate_activation_phrase, validate_skill_name
from .publication_status import (
    STATUS_DRAFT,
    STATUS_IN_MODERATION,
    STATUS_ON_AIR,
    STATUS_REJECTED,
)
from .skill_manifest_provider import ManifestStatus
from .url_helpers import validate_external_base_url


def _publication_status_banner(
    status: str | None,
) -> tuple[str, ConfigEntryType]:
    """Map a classified publication status to a Step 3 banner.

    Returns ``(label_text, entry_type)``. Negative / actionable states
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
    * ℹ️ unknown / not yet fetched — LABEL
    """
    if status == STATUS_ON_AIR:
        return (
            "✅ Yandex moderation passed — your skill is on air. "
            "Voice commands routed via Alice will now reach this skill on "
            "your Yandex Station.",
            ConfigEntryType.LABEL,
        )
    if status == STATUS_IN_MODERATION:
        return (
            "⏳ Yandex moderation in progress (typically 5-15 min). "
            "Click Refresh status below to re-check.",
            ConfigEntryType.ALERT,
        )
    if status == STATUS_REJECTED:
        return (
            "❌ Yandex moderation rejected the latest deploy. "
            "Open the dev console to see the rejection reason and edit the "
            "skill, then click Update skill to re-submit.",
            ConfigEntryType.ALERT,
        )
    if status == STATUS_DRAFT:
        return (
            "⚠️ Skill is registered but has never been published. "
            "Click Update skill (or re-deploy via Recreate) to submit it "
            "to Yandex moderation.",
            ConfigEntryType.ALERT,
        )
    return (
        "ℹ️ Publication status not yet fetched — click Refresh status to query Yandex Dialogs.",
        ConfigEntryType.LABEL,
    )


if TYPE_CHECKING:
    from collections.abc import Iterable

__all__ = ["build_form_entries"]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _stamp(entries: Iterable[ConfigEntry], category: str) -> tuple[ConfigEntry, ...]:
    """Set ``category`` on every entry that hasn't picked one explicitly.

    Default for ``ConfigEntry.category`` is ``"generic"``; treat that
    as "needs a category" and stamp it with *category*. Also sets
    ``category_translation_key`` so MA frontend renders the slug as
    the section header (``u(translation_key, slug)`` — fallback on
    slug when the key isn't translated).
    """
    out: list[ConfigEntry] = []
    for e in entries:
        current = getattr(e, "category", "") or ""
        if current and current != "generic":
            out.append(e)
            continue
        try:
            out.append(
                dataclasses.replace(
                    e,
                    category=category,
                    category_translation_key=f"yandex_alice.category.{category}",
                )
            )
        except TypeError, ValueError:
            with contextlib.suppress(Exception):
                e.category = category
                e.category_translation_key = f"yandex_alice.category.{category}"
            out.append(e)
    return tuple(out)


# ---------------------------------------------------------------------------
# Authorization
# ---------------------------------------------------------------------------


def _authorization_block(
    *, signed_in: bool, user_name: str, last_error: str | None
) -> tuple[ConfigEntry, ...]:
    """Sign-in CTA or signed-in banner + Sign out."""
    if signed_in:
        return (
            ConfigEntry(
                key="label_auth_status",
                type=ConfigEntryType.LABEL,
                label=f"✅ Signed in to Yandex as «{user_name.strip() or 'Yandex account'}».",
            ),
            ConfigEntry(
                key=CONF_ACTION_CLEAR_AUTH,
                type=ConfigEntryType.ACTION,
                label="Sign out of Yandex",
                description=(
                    "Clears the cached Yandex Passport x_token. The "
                    "registered skill stays in Yandex Dialogs; sign in "
                    "again to manage it."
                ),
                action=CONF_ACTION_CLEAR_AUTH,
                action_label="Sign out",
                required=False,
                default_value="",
            ),
        )

    entries: list[ConfigEntry] = []
    if last_error:
        entries.append(
            ConfigEntry(
                key="label_auth_last_error",
                type=ConfigEntryType.ALERT,
                label=f"❌ {last_error}",
            )
        )
    entries.append(
        ConfigEntry(
            key="label_auth_intro",
            type=ConfigEntryType.LABEL,
            label=(
                "Sign in to Yandex to register a dialog skill on your "
                "behalf. We never see your password — Yandex hands us "
                "a long-lived x_token only after you confirm a "
                "verification code in the popup window."
            ),
        )
    )
    entries.append(
        ConfigEntry(
            key=CONF_ACTION_SIGN_IN,
            type=ConfigEntryType.ACTION,
            label="Sign in to Yandex Passport",
            description=(
                "Starts the Yandex Passport Device Flow in a popup "
                "window. After confirming the code, the form refreshes."
            ),
            action=CONF_ACTION_SIGN_IN,
            action_label="Sign in to Yandex Passport",
            required=False,
            default_value="",
        )
    )
    return tuple(entries)


# ---------------------------------------------------------------------------
# Skill
# ---------------------------------------------------------------------------


def _skill_create_subblock(
    *,
    artifacts: SkillCreationArtifacts,
    skill_name: str,
    external_base_url: str,
    base_url_description: str,
    base_url_valid: bool,
    update_message: str | None,
    stage: LocalAutoCreateStage,
) -> tuple[ConfigEntry, ...]:
    """Pre-DONE state: Skill name + URL + Create / Continue / Try-again."""
    entries: list[ConfigEntry] = [
        ConfigEntry(
            key=CONF_DIALOG_SKILL_NAME,
            type=ConfigEntryType.STRING,
            label="Skill name",
            description=(
                "At least 2 words. Globally unique across all Yandex "
                "skills. Examples: 'Music Assistant', 'My Music', "
                "'Home Audio'."
            ),
            required=False,
            value=skill_name,
            default_value="",
            validate=validate_skill_name,
        ),
        ConfigEntry(
            key=CONF_EXTERNAL_BASE_URL,
            type=ConfigEntryType.STRING,
            label="External base URL (HTTPS, required)",
            description=base_url_description,
            required=False,
            value=external_base_url,
            default_value="",
            validate=validate_external_base_url,
        ),
    ]
    if base_url_valid:
        entries.append(
            ConfigEntry(
                key=CONF_ACTION_TEST_WEBHOOK,
                type=ConfigEntryType.ACTION,
                label="Test webhook reachability",
                description=(
                    "Sends a sentinel POST to the webhook URL and "
                    "reports the result. Catches DNS / TLS / "
                    "reverse-proxy issues *before* you spend a "
                    "Yandex moderation cycle on a broken setup."
                ),
                action=CONF_ACTION_TEST_WEBHOOK,
                action_label="Test webhook",
                required=False,
                default_value="",
            )
        )

    if update_message and stage not in (
        LocalAutoCreateStage.FAILED,
        LocalAutoCreateStage.DUPLICATE_DETECTED,
    ):
        entries.append(
            ConfigEntry(
                key="label_skill_msg",
                type=ConfigEntryType.LABEL,
                label=update_message,
            )
        )

    if stage == LocalAutoCreateStage.PIPELINE_RUNNING:
        entries.append(
            ConfigEntry(
                key="label_skill_resume",
                type=ConfigEntryType.LABEL,
                label=(
                    "⏸ Setup was interrupted. Click 'Continue setup' "
                    f"to resume from step {artifacts.state.value}."
                ),
            )
        )
        entries.append(
            ConfigEntry(
                key=CONF_ACTION_AUTO_CREATE_DIALOG,
                type=ConfigEntryType.ACTION,
                label="Continue setup",
                description="Resumes the skill creation pipeline from the last completed step.",
                action=CONF_ACTION_AUTO_CREATE_DIALOG,
                action_label="Continue setup",
                required=False,
                default_value="",
            )
        )
        return tuple(entries)

    if stage == LocalAutoCreateStage.FAILED:
        err = (artifacts.last_error or "Unknown error.").strip()
        entries.append(
            ConfigEntry(
                key="label_skill_failed",
                type=ConfigEntryType.ALERT,
                label=f"❌ {err}",
            )
        )
        if external_base_url:
            entries.append(
                ConfigEntry(
                    key="label_skill_failed_manual",
                    type=ConfigEntryType.LABEL,
                    label=(
                        "Manual fallback: open https://dialogs.yandex.ru/"
                        "developer in your browser and create the skill "
                        f"yourself with webhook URL "
                        f"{external_base_url.rstrip('/')}"
                        f"{DIALOG_WEBHOOK_BASE_PATH}/<webhook secret>. "
                        "Once the skill is created, paste its Skill ID "
                        "into the Advanced section below."
                    ),
                )
            )
        entries.append(
            ConfigEntry(
                key=CONF_ACTION_AUTO_CREATE_DIALOG,
                type=ConfigEntryType.ACTION,
                label="Try again",
                description="Retries the skill creation pipeline from the last completed step.",
                action=CONF_ACTION_AUTO_CREATE_DIALOG,
                action_label="Try again",
                required=False,
                default_value="",
            )
        )
        entries.append(
            ConfigEntry(
                key=CONF_ACTION_CANCEL_DIALOG_SKILL_FLOW,
                type=ConfigEntryType.ACTION,
                label="Reset and start over",
                description=(
                    "Drops partial setup state and clears the failed "
                    "flag. Cached Passport sign-in is kept."
                ),
                action=CONF_ACTION_CANCEL_DIALOG_SKILL_FLOW,
                action_label="Reset (start over)",
                required=False,
                default_value="",
            )
        )
        return tuple(entries)

    # IDLE — ready to create.
    entries.append(
        ConfigEntry(
            key="label_skill_intro",
            type=ConfigEntryType.LABEL,
            label=(
                "Click 'Create skill' to register a new dialog skill "
                "in your Yandex Dialogs account. The external base URL "
                "above must be a public HTTPS URL."
            ),
        )
    )
    entries.append(
        ConfigEntry(
            key=CONF_ACTION_AUTO_CREATE_DIALOG,
            type=ConfigEntryType.ACTION,
            label="Create skill",
            description=(
                "Pre-checks Yandex for an existing skill with the same "
                "name; if found, prompts to Recreate or Adopt. "
                "Otherwise registers a fresh skill."
            ),
            action=CONF_ACTION_AUTO_CREATE_DIALOG,
            action_label="Create skill",
            required=False,
            default_value="",
        )
    )
    return tuple(entries)


def _skill_duplicate_subblock(
    *,
    duplicate_skill_name: str,
    duplicate_skill_id: str,
    action_outcome: AutoCreateOutcome | None,
) -> tuple[ConfigEntry, ...]:
    """Same-name conflict: Recreate / Adopt resolution prompt."""
    entries: list[ConfigEntry] = []
    if action_outcome and action_outcome.user_message:
        entries.append(
            ConfigEntry(
                key="label_skill_outcome",
                type=ConfigEntryType.LABEL,
                label=action_outcome.user_message,
            )
        )
    entries.append(
        ConfigEntry(
            key="label_skill_dup_intro",
            type=ConfigEntryType.LABEL,
            label=(
                f"⚠ A skill named «{duplicate_skill_name}» already exists "
                "in your Yandex Dialogs account. Choose how to proceed:"
            ),
        )
    )
    entries.append(
        ConfigEntry(
            key="label_skill_dup_recreate_hint",
            type=ConfigEntryType.LABEL,
            label=(
                "  • Recreate — delete the existing skill and register "
                "a fresh one with the same name."
            ),
        )
    )
    entries.append(
        ConfigEntry(
            key="label_skill_dup_adopt_hint",
            type=ConfigEntryType.LABEL,
            label=(
                "  • Adopt — keep the existing skill and re-deploy it "
                "against this Music Assistant's webhook URL."
            ),
        )
    )
    if duplicate_skill_id:
        url = f"https://dialogs.yandex.ru/developer/skills/{duplicate_skill_id}"
        entries.append(
            ConfigEntry(
                key="label_skill_dup_console",
                type=ConfigEntryType.LABEL,
                label=f"Open existing skill: {url}",
                help_link=url,
            )
        )
    entries.append(
        ConfigEntry(
            key=CONF_ACTION_RECREATE_DUPLICATE,
            type=ConfigEntryType.ACTION,
            label="Recreate (delete + create fresh)",
            description="Deletes the existing skill in Yandex and registers a fresh one.",
            action=CONF_ACTION_RECREATE_DUPLICATE,
            action_label="Recreate skill",
            required=False,
            default_value="",
        )
    )
    entries.append(
        ConfigEntry(
            key=CONF_ACTION_ADOPT_EXISTING,
            type=ConfigEntryType.ACTION,
            label="Adopt existing skill",
            description="Keeps the existing skill and re-deploys it against this MA's webhook.",
            action=CONF_ACTION_ADOPT_EXISTING,
            action_label="Adopt existing",
            required=False,
            default_value="",
        )
    )
    return tuple(entries)


def _skill_registered_subblock(
    *,
    artifacts: SkillCreationArtifacts,
    edit_mode: bool,
    skill_name: str,
    activation_phrase_2: str,
    activation_phrase_3: str,
    activation_phrase_4: str,
    voice: str,
    update_message: str | None,
    publication_status: str | None,
) -> tuple[ConfigEntry, ...]:
    """DONE state: identity card + status banner + Edit / Refresh / Delete."""
    name = artifacts.last_known_name or skill_name or "Music Assistant"
    skill_id = artifacts.skill_id or ""
    dev_console_url = f"https://dialogs.yandex.ru/developer/skills/{skill_id}" if skill_id else ""

    status_text, status_entry_type = _publication_status_banner(publication_status)
    entries: list[ConfigEntry] = [
        ConfigEntry(
            key="label_skill_registered",
            type=ConfigEntryType.LABEL,
            label=f"✅ Skill «{name}» is registered. Skill ID: {skill_id}",
        ),
        ConfigEntry(
            key="label_skill_publication_status",
            type=status_entry_type,
            label=status_text,
        ),
    ]
    if dev_console_url:
        entries.append(
            ConfigEntry(
                key="label_skill_dev_console_url",
                type=ConfigEntryType.STRING,
                label="Yandex Dialogs dev console",
                description="Copy this URL and open it in your browser.",
                required=False,
                value=dev_console_url,
                default_value="",
            )
        )
    if update_message:
        entries.append(
            ConfigEntry(
                key="label_skill_update_msg",
                type=ConfigEntryType.LABEL,
                label=update_message,
            )
        )
    if not edit_mode:
        entries.append(
            ConfigEntry(
                key=CONF_ACTION_EDIT_SKILL,
                type=ConfigEntryType.ACTION,
                label="Edit skill",
                description=(
                    "Reveals editable fields for the skill name, "
                    "alternative activation phrases, and TTS voice."
                ),
                action=CONF_ACTION_EDIT_SKILL,
                action_label="Edit skill",
                required=False,
                default_value="",
            )
        )
        entries.append(
            ConfigEntry(
                key=CONF_ACTION_REFRESH_STATUS,
                type=ConfigEntryType.ACTION,
                label="Refresh publication status",
                description=(
                    "Re-fetches the skill's status from Yandex Dialogs (one "
                    "HTTP call). Useful while moderation is in progress to "
                    "see when the skill flips to on-air."
                ),
                action=CONF_ACTION_REFRESH_STATUS,
                action_label="Refresh status",
                required=False,
                default_value="",
            )
        )
        return tuple(entries)

    # Edit mode
    entries.append(
        ConfigEntry(
            key="label_skill_edit_intro",
            type=ConfigEntryType.LABEL,
            label="Edit the fields below, then click 'Update skill'.",
        )
    )
    entries.append(
        ConfigEntry(
            key=CONF_DIALOG_SKILL_NAME,
            type=ConfigEntryType.STRING,
            label="Skill name (activation phrase #1)",
            description=(
                "At least 2 words. This is the activation phrase users "
                "say to Alice to invoke your skill."
            ),
            required=False,
            value=skill_name,
            default_value="",
            validate=validate_skill_name,
        )
    )
    for idx, (key, phrase) in enumerate(
        (
            (CONF_DIALOG_ACTIVATION_PHRASE_2, activation_phrase_2),
            (CONF_DIALOG_ACTIVATION_PHRASE_3, activation_phrase_3),
            (CONF_DIALOG_ACTIVATION_PHRASE_4, activation_phrase_4),
        ),
        start=2,
    ):
        entries.append(
            ConfigEntry(
                key=key,
                type=ConfigEntryType.STRING,
                label=f"Alternative activation phrase #{idx} (optional)",
                description="Optional. At least 2 words, or leave empty to skip.",
                required=False,
                value=phrase,
                default_value="",
                validate=validate_activation_phrase,
            )
        )
    voice_options = [
        ConfigValueOption(title=label, value=value) for value, label in DIALOG_VOICE_OPTIONS
    ]
    entries.append(
        ConfigEntry(
            key=CONF_DIALOG_SKILL_VOICE,
            type=ConfigEntryType.STRING,
            label="TTS voice",
            description="Voice used for the skill's text-to-speech replies.",
            required=False,
            value=voice or DIALOG_VOICE_DEFAULT,
            default_value=DIALOG_VOICE_DEFAULT,
            options=voice_options,
        )
    )
    entries.append(
        ConfigEntry(
            key=CONF_ACTION_UPDATE_SKILL,
            type=ConfigEntryType.ACTION,
            label="Update skill",
            description=(
                "Pushes edits to Yandex via PATCH draft + re-deploy. Yandex moderation: 5-15 min."
            ),
            action=CONF_ACTION_UPDATE_SKILL,
            action_label="Update skill",
            required=False,
            default_value="",
        )
    )
    entries.append(
        ConfigEntry(
            key=CONF_ACTION_DELETE_SKILL,
            type=ConfigEntryType.ACTION,
            label="Delete skill",
            description=(
                "Permanently deletes the skill from Yandex Dialogs. "
                "Cached Passport sign-in is kept."
            ),
            action=CONF_ACTION_DELETE_SKILL,
            action_label="Delete skill",
            required=False,
            default_value="",
        )
    )
    entries.append(
        ConfigEntry(
            key=CONF_ACTION_CANCEL_EDIT,
            type=ConfigEntryType.ACTION,
            label="Cancel edit",
            description="Discards your edits and exits edit mode.",
            action=CONF_ACTION_CANCEL_EDIT,
            action_label="Cancel edit",
            required=False,
            default_value="",
        )
    )
    return tuple(entries)


def _skill_advanced_subblock(  # noqa: PLR0913
    *,
    is_configured: bool,
    skill_id_value: str,
    skill_token_value: str,
    webhook_secret: str,
    activation_phrase_2: str,
    activation_phrase_3: str,
    activation_phrase_4: str,
    voice: str,
    suppress_phrase_voice_mirror: bool,
    suppress_skill_name_mirror: bool,
    suppress_external_base_url_mirror: bool,
    skill_name: str,
    external_base_url: str,
    diagnostics: tuple[ConfigEntry, ...],
) -> tuple[ConfigEntry, ...]:
    """Skill-related Advanced fields (visible behind ``Show advanced`` toggle).

    The various ``suppress_*_mirror`` flags avoid duplicate-key
    collisions: when a key is already rendered *visible* by
    ``_skill_create_subblock`` / ``_skill_registered_subblock`` in
    edit mode, it must NOT also appear as a hidden Advanced mirror.
    The dispatcher passes ``True`` for whichever keys are visible in
    the current state and ``False`` for the rest; the latter need a
    mirror so their values still round-trip on form Save (FE only
    persists visible field values).
    """
    entries: list[ConfigEntry] = []
    if not suppress_skill_name_mirror:
        entries.append(
            ConfigEntry(
                key=CONF_DIALOG_SKILL_NAME,
                type=ConfigEntryType.STRING,
                label="Skill name",
                description="The skill's display name in Yandex Dialogs.",
                required=False,
                value=skill_name,
                default_value="",
                advanced=True,
            )
        )
    if not suppress_external_base_url_mirror:
        entries.append(
            ConfigEntry(
                key=CONF_EXTERNAL_BASE_URL,
                type=ConfigEntryType.STRING,
                label="External base URL (HTTPS)",
                description=(
                    "Public HTTPS URL Yandex calls for webhooks. "
                    "Editable here even after the skill is registered."
                ),
                required=False,
                value=external_base_url,
                default_value="",
                advanced=True,
                validate=validate_external_base_url,
            )
        )
    entries.append(
        ConfigEntry(
            key=CONF_DIALOG_SKILL_ID,
            type=ConfigEntryType.STRING,
            label="Skill ID",
            description=(
                "UUID of the skill — populated automatically after a "
                "successful auto-create, or paste manually if you set "
                "up the skill yourself."
            ),
            required=False,
            value=skill_id_value,
            default_value="",
            read_only=is_configured,
            advanced=True,
        )
    )
    entries.append(
        ConfigEntry(
            key=CONF_DIALOG_SKILL_TOKEN,
            type=ConfigEntryType.SECURE_STRING,
            label="Skill OAuth token (manual setup only)",
            description=(
                "Optional OAuth token from "
                "https://oauth.yandex.ru/authorize?response_type=token"
                "&client_id=c473ca268cd749d3a8371351a8f2bcbd. "
                "Used to push state callbacks to Yandex (future "
                "feature; stored encrypted)."
            ),
            help_link=(
                "https://oauth.yandex.ru/authorize?response_type=token"
                "&client_id=c473ca268cd749d3a8371351a8f2bcbd"
            ),
            required=False,
            value=skill_token_value,
            advanced=True,
        )
    )
    entries.append(
        ConfigEntry(
            key=CONF_DIALOG_WEBHOOK_SECRET,
            type=ConfigEntryType.SECURE_STRING,
            label="Webhook URL secret",
            description=(
                "Random secret embedded in the webhook URL. The full "
                f"URL is <external_base_url>{DIALOG_WEBHOOK_BASE_PATH}"
                "/<this-secret>. Editable: changes here invalidate "
                "Yandex's existing registration."
            ),
            required=False,
            value=webhook_secret,
            advanced=True,
        )
    )
    if is_configured:
        entries.append(
            ConfigEntry(
                key=CONF_ACTION_REGENERATE_WEBHOOK_SECRET,
                type=ConfigEntryType.ACTION,
                label="Regenerate webhook secret",
                description=(
                    "Generates a fresh secret + resets the auto-create "
                    "state. The current Yandex skill registration "
                    "becomes stale — you'll need to re-create the skill."
                ),
                action=CONF_ACTION_REGENERATE_WEBHOOK_SECRET,
                action_label="Regenerate (forces re-create)",
                required=False,
                default_value="",
                advanced=True,
            )
        )
    if not suppress_phrase_voice_mirror:
        for idx, (key, value) in enumerate(
            (
                (CONF_DIALOG_ACTIVATION_PHRASE_2, activation_phrase_2),
                (CONF_DIALOG_ACTIVATION_PHRASE_3, activation_phrase_3),
                (CONF_DIALOG_ACTIVATION_PHRASE_4, activation_phrase_4),
            ),
            start=2,
        ):
            entries.append(
                ConfigEntry(
                    key=key,
                    type=ConfigEntryType.STRING,
                    label=f"Alternative activation phrase #{idx}",
                    description="Optional. At least 2 words, or empty to skip.",
                    required=False,
                    value=value,
                    default_value="",
                    advanced=True,
                )
            )
        entries.append(
            ConfigEntry(
                key=CONF_DIALOG_SKILL_VOICE,
                type=ConfigEntryType.STRING,
                label="TTS voice",
                description="Yandex TTS voice id.",
                required=False,
                value=voice or DIALOG_VOICE_DEFAULT,
                default_value=DIALOG_VOICE_DEFAULT,
                advanced=True,
            )
        )
    entries.extend(diagnostics)
    return tuple(entries)


def _skill_block(  # noqa: PLR0913
    *,
    artifacts: SkillCreationArtifacts,
    cached_x_token_present: bool,
    skill_id_value: str,
    skill_token_value: str,
    webhook_secret: str,
    edit_mode: bool,
    skill_name: str,
    activation_phrase_2: str,
    activation_phrase_3: str,
    activation_phrase_4: str,
    voice: str,
    update_message: str | None,
    external_base_url: str,
    base_url_description: str,
    base_url_valid: bool,
    duplicate_skill_id: str | None,
    duplicate_skill_name: str | None,
    action_outcome: AutoCreateOutcome | None,
    publication_status: str | None,
    diagnostics: tuple[ConfigEntry, ...],
) -> tuple[ConfigEntry, ...]:
    """Skill section visible when authed OR a manual skill_id is set."""
    is_done = artifacts.state == SkillCreationState.DONE
    skill_known = is_done or bool(skill_id_value)
    visible = cached_x_token_present or skill_known
    if not visible:
        return ()

    duplicate_pending = bool(duplicate_skill_id)
    if duplicate_pending:
        stage = LocalAutoCreateStage.DUPLICATE_DETECTED
    elif is_done:
        stage = LocalAutoCreateStage.DONE
    elif artifacts.state == SkillCreationState.FAILED:
        stage = LocalAutoCreateStage.FAILED
    elif cached_x_token_present and artifacts.state in (
        SkillCreationState.APP_CREATED,
        SkillCreationState.DRAFT_UPDATED,
        SkillCreationState.OAUTH_CREATED,
        SkillCreationState.OAUTH_ATTACHED,
        SkillCreationState.DEPLOY_REQUESTED,
    ):
        stage = LocalAutoCreateStage.PIPELINE_RUNNING
    else:
        stage = LocalAutoCreateStage.IDLE

    visible_entries: tuple[ConfigEntry, ...]
    suppress_external_base_url_mirror = False
    if duplicate_pending:
        visible_entries = _skill_duplicate_subblock(
            duplicate_skill_name=duplicate_skill_name or "",
            duplicate_skill_id=duplicate_skill_id or "",
            action_outcome=action_outcome,
        )
        suppress_phrase_voice_mirror = False
        suppress_skill_name_mirror = False
    elif is_done:
        visible_entries = _skill_registered_subblock(
            artifacts=artifacts,
            edit_mode=edit_mode,
            skill_name=skill_name,
            activation_phrase_2=activation_phrase_2,
            activation_phrase_3=activation_phrase_3,
            activation_phrase_4=activation_phrase_4,
            voice=voice,
            update_message=update_message,
            publication_status=publication_status,
        )
        suppress_phrase_voice_mirror = edit_mode
        suppress_skill_name_mirror = edit_mode
        # external_base_url is NOT visible in DONE / edit-mode → keep
        # the Advanced mirror so its value still round-trips on Save.
    else:
        visible_entries = _skill_create_subblock(
            artifacts=artifacts,
            skill_name=skill_name,
            external_base_url=external_base_url,
            base_url_description=base_url_description,
            base_url_valid=base_url_valid,
            update_message=update_message,
            stage=stage,
        )
        suppress_phrase_voice_mirror = False
        suppress_skill_name_mirror = True  # rendered visible above
        suppress_external_base_url_mirror = True  # rendered visible above

    advanced_entries = _skill_advanced_subblock(
        is_configured=is_done and bool(artifacts.skill_id),
        skill_id_value=skill_id_value,
        skill_token_value=skill_token_value,
        webhook_secret=webhook_secret,
        activation_phrase_2=activation_phrase_2,
        activation_phrase_3=activation_phrase_3,
        activation_phrase_4=activation_phrase_4,
        voice=voice,
        suppress_phrase_voice_mirror=suppress_phrase_voice_mirror,
        suppress_skill_name_mirror=suppress_skill_name_mirror,
        suppress_external_base_url_mirror=suppress_external_base_url_mirror,
        skill_name=skill_name,
        external_base_url=external_base_url,
        diagnostics=diagnostics,
    )
    return (*visible_entries, *advanced_entries)


# ---------------------------------------------------------------------------
# Settings (provider-wide)
# ---------------------------------------------------------------------------


def _manifest_block(
    *,
    status: ManifestStatus,
    paste_value: str,
    update_message: str | None,
) -> tuple[ConfigEntry, ...]:
    """Skill manifest banner + Export / Import / Reset / Validate actions.

    The manifest controls intents + entities deployed to Yandex (skill
    grammar) and the runtime mapping that turns NLU matches into
    player actions. Bundled by default; users override by writing
    ``<storage>/yandex_alice/skill.toml`` (manually or via Import).
    """
    if status.source == "bundled":
        banner = (
            f"Skill manifest: bundled default "
            f"({status.intent_count} intents, {status.entity_count} entities). "
            f"Use Export to copy it to {status.override_path} for editing."
        )
    elif status.source == "override_valid":
        banner = (
            f"Skill manifest: override active at {status.override_path} "
            f"({status.intent_count} intents, {status.entity_count} entities)."
        )
    else:  # override_invalid
        banner = (
            f"⚠ Skill manifest: override at {status.override_path} is invalid — "
            f"falling back to bundled default ({status.intent_count} intents, "
            f"{status.entity_count} entities). "
            f"Error: {status.error or 'unknown parse error'}"
        )

    entries: list[ConfigEntry] = [
        ConfigEntry(
            key="label_manifest_banner",
            type=ConfigEntryType.LABEL,
            label=banner,
        ),
    ]
    if update_message:
        entries.append(
            ConfigEntry(
                key="label_manifest_message",
                type=ConfigEntryType.LABEL,
                label=update_message,
            )
        )
    entries.extend(
        (
            ConfigEntry(
                key=CONF_ACTION_EXPORT_MANIFEST,
                type=ConfigEntryType.ACTION,
                label="Export manifest",
                description=(
                    "Write the current bundled manifest to "
                    f"{status.override_path}. Will not overwrite an existing "
                    "override; use Reset first if you want to start over."
                ),
                action=CONF_ACTION_EXPORT_MANIFEST,
                action_label="Export to file",
                required=False,
                default_value="",
            ),
            ConfigEntry(
                key=CONF_DIALOG_SKILL_OVERRIDE_PASTE,
                type=ConfigEntryType.STRING,
                label="Manifest TOML to import",
                description=(
                    "Paste the full TOML contents and click Import. If your "
                    "browser strips line breaks, paste base64 with prefix "
                    "data:base64,<base64-encoded-TOML> — base64 -i skill.toml "
                    "will produce one. Cleared on a successful import."
                ),
                required=False,
                value=paste_value,
                default_value="",
            ),
            ConfigEntry(
                key=CONF_ACTION_IMPORT_MANIFEST,
                type=ConfigEntryType.ACTION,
                label="Import manifest",
                description=(
                    "Validate the pasted TOML and write it to "
                    f"{status.override_path}. Existing override is replaced "
                    "atomically. The paste field is cleared on success."
                ),
                action=CONF_ACTION_IMPORT_MANIFEST,
                action_label="Import from paste",
                required=False,
                default_value="",
            ),
            ConfigEntry(
                key=CONF_ACTION_VALIDATE_MANIFEST,
                type=ConfigEntryType.ACTION,
                label="Validate manifest",
                description=(
                    "Re-parse the override file and report parse / schema "
                    "errors without re-deploying to Yandex."
                ),
                action=CONF_ACTION_VALIDATE_MANIFEST,
                action_label="Validate override",
                required=False,
                default_value="",
            ),
            ConfigEntry(
                key=CONF_ACTION_RESET_MANIFEST,
                type=ConfigEntryType.ACTION,
                label="Reset manifest",
                description=(
                    "Delete the override file and revert to the bundled "
                    "default. Idempotent — safe to click when no override "
                    "exists."
                ),
                action=CONF_ACTION_RESET_MANIFEST,
                action_label="Reset to bundled default",
                required=False,
                default_value="",
            ),
        )
    )
    return tuple(entries)


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
            label="Voice-controllable players",
            description=(
                "Players Alice is allowed to control. Leave empty to "
                "expose all players known to MA — Alice will then "
                "accept voice commands for any player by name."
            ),
            multi_value=True,
            options=player_options,
            required=False,
            default_value=[],
        ),
        ConfigEntry(
            key=CONF_USE_DIFFERENT_INSTANCE_NAME,
            type=ConfigEntryType.BOOLEAN,
            label="Use a different MA-side instance name",
            description=(
                "By default the Skill name is also used as the "
                "instance name shown in MA's provider list. Turn "
                "this on to specify a different MA-side name."
            ),
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
                label="Instance name (MA-side display)",
                description=(
                    "Display name shown in Music Assistant's provider "
                    "list. Independent from the Skill name."
                ),
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
                label="Instance name (auto)",
                required=False,
                default_value=skill_name or DIALOG_DEFAULT_NAME,
                hidden=True,
            )
        )
    return tuple(entries)


# ---------------------------------------------------------------------------
# Top-level composer
# ---------------------------------------------------------------------------


def build_form_entries(  # noqa: PLR0913
    *,
    artifacts: SkillCreationArtifacts,
    cached_x_token_present: bool,
    user_name: str,
    skill_id_value: str,
    skill_token_value: str,
    webhook_secret: str,
    last_error: str | None,
    action_outcome: AutoCreateOutcome | None,
    duplicate_skill_id: str | None,
    duplicate_skill_name: str | None,
    edit_mode: bool,
    skill_name: str,
    activation_phrase_2: str,
    activation_phrase_3: str,
    activation_phrase_4: str,
    voice: str,
    update_message: str | None,
    external_base_url: str,
    base_url_description: str,
    base_url_valid: bool,
    player_options: list[ConfigValueOption],
    instance_name: str,
    use_different_instance_name: bool,
    publication_status: str | None,
    diagnostics: tuple[ConfigEntry, ...],
    manifest_status: ManifestStatus,
    manifest_paste: str,
    manifest_message: str | None,
    hidden_state: tuple[ConfigEntry, ...],
) -> tuple[ConfigEntry, ...]:
    """Compose Authorization + Skill + Settings + hidden state."""
    auth = _stamp(
        _authorization_block(
            signed_in=cached_x_token_present,
            user_name=user_name,
            last_error=last_error,
        ),
        CATEGORY_AUTHORIZATION,
    )
    skill = _stamp(
        _skill_block(
            artifacts=artifacts,
            cached_x_token_present=cached_x_token_present,
            skill_id_value=skill_id_value,
            skill_token_value=skill_token_value,
            webhook_secret=webhook_secret,
            edit_mode=edit_mode,
            skill_name=skill_name,
            activation_phrase_2=activation_phrase_2,
            activation_phrase_3=activation_phrase_3,
            activation_phrase_4=activation_phrase_4,
            voice=voice,
            update_message=update_message,
            external_base_url=external_base_url,
            base_url_description=base_url_description,
            base_url_valid=base_url_valid,
            duplicate_skill_id=duplicate_skill_id,
            duplicate_skill_name=duplicate_skill_name,
            action_outcome=action_outcome,
            publication_status=publication_status,
            diagnostics=diagnostics,
        ),
        CATEGORY_SKILL,
    )
    settings = _stamp(
        (
            *_settings_block(
                player_options=player_options,
                instance_name=instance_name,
                skill_name=skill_name,
                use_different_instance_name=use_different_instance_name,
            ),
            *_manifest_block(
                status=manifest_status,
                paste_value=manifest_paste,
                update_message=manifest_message,
            ),
        ),
        CATEGORY_SETTINGS,
    )
    # Hidden state-carriers stay in the default "generic" category —
    # they're hidden=True so they never render as a section anyway.
    return (*auth, *skill, *settings, *hidden_state)
