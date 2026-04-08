"""NetEase Cloud Music API client (placeholder)."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import aiohttp


class NetEaseCloudMusicClient:
    """Client for NetEase Cloud Music API.

    TODO: Implement API client using NeteaseCloudMusicApi
    or direct HTTP calls to music.163.com
    """

    def __init__(self, session: aiohttp.ClientSession, token: str) -> None:
        """Initialize the API client.

        :param session: aiohttp client session
        :param token: Authentication token
        """
        self._session = session
        self._token = token

    async def get_user_playlist(self, user_id: str) -> list[dict[str, Any]]:
        """Get user's playlists."""
        raise NotImplementedError("Not yet implemented")
