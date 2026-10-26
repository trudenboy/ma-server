"""
Yandex Dialogs skill operations shared by the setup flow and the options page.

The setup flow creates / edits the skill and signs in; the options page
deletes it, refreshes its status and tests the webhook. Both need the same
deploy payload and the same view of which Yandex account to act as.
"""

from __future__ import annotations

import secrets
from typing import TYPE_CHECKING, Any

from music_assistant_models.errors import LoginFailed
from ya_dialogs_api import DialogsSkillCreator
from ya_passport_auth.ma import BORROW_SOURCE_OWN, BorrowedCredentialSource

from .auth_session import cached_authenticated_session
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
    DIALOG_CHANNEL,
)
from .dialog_skill_meta import (
    build_activation_phrases,
    build_skill_description,
    build_structured_examples,
)

if TYPE_CHECKING:
    from music_assistant.mass import MusicAssistant

    from .skill_manifest_provider import SkillManifestProvider

# Keys collected by the setup flow. Before MA 2.10 they were plain config
# values; on load they are copied into setup data once (see the plugin).
SETUP_DATA_KEYS: tuple[str, ...] = (
    CONF_YM_INSTANCE,
    CONF_AUTH_X_TOKEN,
    CONF_AUTH_USER_NAME,
    CONF_DIALOG_SKILL_ID,
    CONF_DIALOG_SKILL_NAME,
    CONF_DIALOG_WEBHOOK_SECRET,
    CONF_EXTERNAL_BASE_URL,
    CONF_DIALOG_ACTIVATION_PHRASE_2,
    CONF_DIALOG_ACTIVATION_PHRASE_3,
    CONF_DIALOG_ACTIVATION_PHRASE_4,
    CONF_DIALOG_SKILL_VOICE,
    CONF_DIALOG_AUTO_CREATE_ARTIFACTS,
    CONF_DIALOG_PUBLICATION_STATUS,
)


def generate_webhook_secret() -> str:
    """Return a fresh URL-safe random secret for the webhook path."""
    return secrets.token_urlsafe(24)


def is_borrowing(ym_instance: object) -> bool:
    """Return True when the account is borrowed from a linked Yandex Music instance."""
    return bool(ym_instance) and ym_instance != BORROW_SOURCE_OWN


def borrowed_x_token(mass: MusicAssistant, ym_instance: str) -> str:
    """
    Return the x_token of a linked Yandex Music instance (read-only).

    :param mass: The Music Assistant instance.
    :param ym_instance: Instance id of the linked Yandex Music provider.
    :raises LoginFailed: The instance is missing or holds no session token.
    """
    try:
        _, x_token = BorrowedCredentialSource(mass, ym_instance).read_tokens()
    except Exception as err:
        raise LoginFailed(str(err)) from err
    if x_token is None:
        msg = (
            "The linked Yandex Music instance has no session token. "
            "Authenticate it (with Remember session enabled) first."
        )
        raise LoginFailed(msg)
    return x_token.get_secret()


def deploy_args(
    manifest_provider: SkillManifestProvider,
    *,
    skill_name: str,
    backend_uri: str,
    activation_phrases: list[str] | None = None,
) -> dict[str, Any]:
    """
    Build the keyword arguments shared by the skill create / adopt / update calls.

    :param manifest_provider: Snapshot supplying the skill grammar.
    :param skill_name: Skill name registered in Yandex Dialogs.
    :param backend_uri: Public webhook URL Yandex calls.
    :param activation_phrases: Phrases to register; derived from the name when None.
    """
    return {
        "skill_name": skill_name,
        "backend_uri": backend_uri,
        "description": build_skill_description(skill_name),
        "structured_examples": build_structured_examples(skill_name),
        "activation_phrases": activation_phrases or build_activation_phrases(skill_name),
        "intents": manifest_provider.grammar(),
        "entities": manifest_provider.entities(),
    }


async def delete_skill(x_token: str, skill_id: str) -> None:
    """
    Hard-delete a skill from the user's Yandex Dialogs account.

    :param x_token: Yandex Passport x_token of the skill owner.
    :param skill_id: Skill to delete.
    """
    async with cached_authenticated_session(x_token) as session:
        creator = DialogsSkillCreator(session, channel=DIALOG_CHANNEL)
        csrf = await creator.fetch_csrf()
        await creator.delete_skill(csrf, skill_id)
