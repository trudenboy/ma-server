"""Pure helpers for assembling Yandex Dialogs skill metadata.

Side-effect free: no MA / aiohttp / network access. Lives in its own module
so the orchestrator (auto_create.py / auto_update.py) can be tested without
threading these strings through every fixture.
"""

from __future__ import annotations

from typing import Any

from .constants import DIALOG_WEBHOOK_BASE_PATH


def build_backend_uri(base_url: str, webhook_secret: str) -> str:
    """Compose the public webhook URL Yandex must call.

    Yandex requires HTTPS — the dev-console rejects plain http:// at draft-update
    time, but we surface the rejection up-front so the user sees a clear error
    before the Device Flow even starts.

    Raises:
        ValueError: ``base_url`` is empty / not HTTPS, or ``webhook_secret`` is empty.
    """
    base = (base_url or "").strip().rstrip("/")
    if not base:
        msg = "External base URL is empty — set a public HTTPS URL for Yandex first"
        raise ValueError(msg)
    if not base.lower().startswith("https://"):
        msg = f"External base URL must use HTTPS (got: {base!r})"
        raise ValueError(msg)
    secret = (webhook_secret or "").strip()
    if not secret:
        msg = "Webhook secret is empty — open the form once to auto-generate one"
        raise ValueError(msg)
    return f"{base}{DIALOG_WEBHOOK_BASE_PATH}/{secret}"


def build_skill_description(skill_name: str) -> str:
    """Default Russian description shown in the Alice catalog and to moderators.

    Yandex rejects empty descriptions for ``aliceSkill`` skills, so we always
    return a non-empty string. Embeds the skill name so the catalog listing
    is self-contained.
    """
    name = (skill_name or "").strip() or "Music Assistant"
    return (
        f"Голосовое управление Music Assistant через навык «{name}». "
        "Включение треков, управление воспроизведением и громкостью, "
        "перемещение очереди между колонками."
    )


def build_activation_phrases(skill_name: str) -> list[str]:
    """Default activation phrase list — single entry: the skill name itself."""
    name = (skill_name or "").strip() or "Music Assistant"
    return [name]


def build_structured_examples(skill_name: str) -> list[dict[str, Any]]:
    """Default structured examples shown to moderators.

    Shape captured from a successful PATCH issued by the dev console after a
    manual form fill (see ya_dialogs_api.api_client.build_dialog_draft_payload
    docstring). Three concrete commands so reviewers can see playback,
    transport, and multi-room intents without us guessing what passes review.
    """
    name = (skill_name or "").strip() or "Music Assistant"
    return [
        {
            "marker": "попроси",
            "activationPhrase": name,
            "request": "включи джаз",
            "is_valid": True,
        },
        {
            "marker": "попроси",
            "activationPhrase": name,
            "request": "поставь на паузу",
            "is_valid": True,
        },
        {
            "marker": "попроси",
            "activationPhrase": name,
            "request": "переведи музыку на кухню",
            "is_valid": True,
        },
    ]
