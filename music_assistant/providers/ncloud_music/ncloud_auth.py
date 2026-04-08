"""NetEase Cloud Music authentication module (placeholder)."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from music_assistant.mass import MusicAssistant


async def perform_qr_auth(
    mass: MusicAssistant, session_id: str
) -> tuple[str, str]:
    """Perform QR code authentication.

    :param mass: Music Assistant instance
    :param session_id: Unique session identifier for QR polling
    :return: Tuple of (user_id, token)

    TODO: Implement QR authentication flow
    """
    raise NotImplementedError("QR authentication not yet implemented")
