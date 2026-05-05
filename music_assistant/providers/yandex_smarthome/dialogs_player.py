"""Music + player resolvers for the Yandex Dialogs custom-skill webhook.

`resolve_query` turns a `ParsedCommand` into a concrete URI/MediaItem ready
to feed to `mass.player_queues.play_media`. `play_for_alice` wraps the
power-on + queue-play sequence.

Bound to MA APIs: `mass.music.search`, `mass.music.get_item_by_uri`,
`mass.player_queues.play_media`, `mass.players.cmd_power`.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

from music_assistant_models.enums import MediaType

if TYPE_CHECKING:
    from music_assistant_models.media_items import MediaItemType

    from music_assistant.mass import MusicAssistant

    from .dialogs_nlu import ParsedCommand


_LOGGER = logging.getLogger(__name__)

_SEARCH_LIMIT_DEFAULT = 5


def _has_feature(player: Any, feature_name: str) -> bool:
    """Mirror provider.device._has_feature so we don't import device.py from here."""
    features = getattr(player, "supported_features", None)
    if not features:
        return False
    return any(
        str(f) == feature_name or getattr(f, "value", None) == feature_name for f in features
    )


def _first(items: Any) -> Any:
    """Return the first item of a Sequence, or None if empty/not-a-sequence."""
    try:
        return next(iter(items))
    except (StopIteration, TypeError):
        return None


# ---------------------------------------------------------------------------
# Content resolver
# ---------------------------------------------------------------------------


async def resolve_query(mass: MusicAssistant, parsed: ParsedCommand) -> MediaItemType | str | None:
    """Pick the best media item for the parsed voice command.

    Returns either a MediaItem or a URI string (both accepted by
    play_media); None means we couldn't resolve and the webhook handler
    should respond with a "not found" message to the user.
    """
    if parsed.kind == "my_wave":
        return await _resolve_my_wave(mass)
    if parsed.kind == "genre":
        return await _resolve_genre(mass, parsed.query)

    if not parsed.query:
        return None

    # Map kind → search MediaTypes, prefer-library bias on first try.
    media_types_by_kind: dict[str, list[MediaType]] = {
        "track": [MediaType.TRACK],
        "artist": [MediaType.ARTIST],
        "album": [MediaType.ALBUM],
        "playlist": [MediaType.PLAYLIST],
        "search": [MediaType.PLAYLIST, MediaType.ALBUM, MediaType.ARTIST, MediaType.TRACK],
    }
    media_types = media_types_by_kind.get(parsed.kind, [MediaType.TRACK])

    try:
        results = await mass.music.search(
            search_query=parsed.query,
            media_types=media_types,
            limit=_SEARCH_LIMIT_DEFAULT,
        )
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        _LOGGER.warning("mass.music.search failed for %r: %s", parsed.query, exc)
        return None

    return _pick_from_results(results, parsed.kind)


def _pick_from_results(results: object, kind: str) -> MediaItemType | None:
    """Pick best MediaItem from SearchResults given the parsed kind."""
    # SearchResults has .artists, .albums, .tracks, .playlists, plus .library_*.
    # For "search" (kind=catch-all) prefer playlist > album > artist > track.
    order: list[str]
    if kind == "search":
        order = ["playlists", "albums", "artists", "tracks"]
    elif kind == "track":
        order = ["tracks"]
    elif kind == "artist":
        order = ["artists"]
    elif kind == "album":
        order = ["albums"]
    elif kind == "playlist":
        order = ["playlists"]
    else:
        order = ["tracks"]

    for attr in order:
        bucket = getattr(results, attr, None)
        if not bucket:
            continue
        item = _first(bucket)
        if item is not None:
            return item  # type: ignore[no-any-return]
    return None


# ---------------------------------------------------------------------------
# Yandex-specific specials
# ---------------------------------------------------------------------------


def _find_yandex_music_provider(mass: MusicAssistant) -> Any:
    """Locate the first available yandex_music music provider instance."""
    for attr in ("music_providers", "providers"):
        try:
            for prov in getattr(mass, attr, ()):
                if getattr(prov, "domain", None) == "yandex_music" and getattr(
                    prov, "available", True
                ):
                    return prov
        except Exception:  # noqa: S110
            pass
    return None


async def _resolve_my_wave(mass: MusicAssistant) -> str | None:
    """Resolve "My Wave" radio — yandex_music rotor station user:onyourwave.

    Returns a track URI from the rotor batch; play_media in radio mode
    will keep pulling next tracks via the standard queue radio loop. If
    yandex_music isn't installed/available, returns None.
    """
    provider = _find_yandex_music_provider(mass)
    if provider is None:
        _LOGGER.info("My Wave requested but yandex_music provider is not available")
        return None
    try:
        client = getattr(provider, "client", None)
        if client is None:
            return None
        tracks, _batch_id = await client.get_rotor_station_tracks("user:onyourwave")
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        _LOGGER.warning("My Wave rotor fetch failed: %s", exc)
        return None
    if not tracks:
        return None
    first_track = tracks[0]
    track_id = getattr(first_track, "id", None) or getattr(first_track, "track_id", None)
    if not track_id:
        return None
    instance_id = getattr(provider, "instance_id", "yandex_music")
    return f"{instance_id}://track/{track_id}"


async def _resolve_genre(mass: MusicAssistant, query: str) -> MediaItemType | str | None:
    """Resolve genre-based radio.

    Best-effort: try yandex_music genre rotor;
    fall back to plain artist search with radio_mode upstream.
    """
    if not query:
        return None
    provider = _find_yandex_music_provider(mass)
    if provider is not None:
        try:
            client = getattr(provider, "client", None)
            if client is not None:
                station_id = f"genre:{query}"
                tracks, _ = await client.get_rotor_station_tracks(station_id)
                if tracks:
                    first_track = tracks[0]
                    track_id = getattr(first_track, "id", None) or getattr(
                        first_track, "track_id", None
                    )
                    if track_id:
                        instance_id = getattr(provider, "instance_id", "yandex_music")
                        return f"{instance_id}://track/{track_id}"
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            _LOGGER.debug("Genre rotor fallback for %r: %s", query, exc)

    # Generic fallback: search across artists+tracks; caller will use radio_mode.
    try:
        results = await mass.music.search(
            search_query=query,
            media_types=[MediaType.ARTIST, MediaType.TRACK],
            limit=_SEARCH_LIMIT_DEFAULT,
        )
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        _LOGGER.warning("Genre fallback search failed for %r: %s", query, exc)
        return None
    return _first(getattr(results, "artists", None) or []) or _first(  # type: ignore[no-any-return]
        getattr(results, "tracks", None) or []
    )


# ---------------------------------------------------------------------------
# Playback
# ---------------------------------------------------------------------------


async def play_for_alice(
    mass: MusicAssistant,
    player_id: str,
    media: MediaItemType | str,
    *,
    radio_mode: bool = False,
) -> None:
    """Power the player on if needed, then start playback via player_queues."""
    player = mass.players.get_player(player_id)
    if player is not None and _has_feature(player, "power"):
        powered = getattr(player, "powered", None)
        if powered is False:
            try:
                await mass.players.cmd_power(player_id, True)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                _LOGGER.warning("cmd_power(True) on %s failed: %s", player_id, exc)

    await mass.player_queues.play_media(queue_id=player_id, media=media, radio_mode=radio_mode)
