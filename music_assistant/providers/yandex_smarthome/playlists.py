"""Helpers for exposing MA library playlists as Yandex input_source modes.

Wraps the few MA APIs used by the playlist-source feature so the rest of
the provider stays decoupled from `mass.music`/`player_queues` internals
and so tests can stub a single seam.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from music_assistant_models.config_entries import ConfigValueOption

if TYPE_CHECKING:
    from music_assistant.mass import MusicAssistant


_LOGGER = logging.getLogger(__name__)

_LIBRARY_LIMIT = 500


async def fetch_playlist_options(mass: MusicAssistant) -> list[ConfigValueOption]:
    """Build ConfigValueOption list of all library playlists for the config form.

    Used at config-render time only. Fail-soft: returns [] if mass.music or
    the playlists controller is not yet available (e.g. provider load order).
    """
    try:
        playlists = await mass.music.playlists.library_items(limit=_LIBRARY_LIMIT)
    except Exception as exc:
        # Fail-soft: this runs on every config-form render and races with
        # provider/database startup. Don't spam stack traces — debug-level
        # is enough for diagnostics, normal renders stay quiet.
        _LOGGER.debug("Library playlists not available yet: %s", exc)
        return []

    options: list[ConfigValueOption] = []
    for playlist in playlists:
        if not playlist.uri:
            continue
        provider_label = playlist.provider or ""
        title = f"{playlist.name} ({provider_label})" if provider_label else playlist.name
        options.append(ConfigValueOption(title=title, value=playlist.uri))
    return options


async def play_playlist(mass: MusicAssistant, player_id: str, uri: str) -> None:
    """Start playback of a playlist URI on the given player's queue.

    `play_media` accepts a URI string directly and resolves the playlist's
    tracks into the queue. queue_id == player_id for the player's own queue.
    """
    await mass.player_queues.play_media(queue_id=player_id, media=uri)
