"""
Home Assistant Plugin for Music Assistant.

The plugin is the core of all communication to/from Home Assistant and
responsible for maintaining the WebSocket API connection to HA.
Also, the Music Assistant integration within HA will relay its own api
communication over the HA api for more flexibility as well as security.
"""

from __future__ import annotations

import asyncio
import logging
import os
from functools import partial
from itertools import batched
from sys import intern
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, NamedTuple, TypedDict, cast

from hass_client import HomeAssistantClient
from hass_client.exceptions import BaseHassClientError
from hass_client.utils import get_websocket_url
from music_assistant_models.auth import Scope
from music_assistant_models.config_entries import ConfigEntry
from music_assistant_models.enums import (
    ConfigEntryType,
    ContentType,
    EventType,
    MediaType,
    ProviderFeature,
    StreamType,
)
from music_assistant_models.errors import (
    MusicAssistantError,
    SetupFailedError,
    UnsupportedFeaturedException,
)
from music_assistant_models.media_items.audio_format import AudioFormat
from music_assistant_models.player_control import PlayerControl
from music_assistant_models.streamdetails import StreamDetails

from music_assistant.constants import VERBOSE_LOG_LEVEL
from music_assistant.controllers.cache import use_cache
from music_assistant.helpers.datetime import iso_from_utc_timestamp
from music_assistant.helpers.json import SerializableType
from music_assistant.helpers.util import lock, try_parse_int
from music_assistant.models.plugin import AIEngine, PluginProvider, TTSEngine

from .constants import (
    CONF_MUTE_CONTROLS,
    CONF_POWER_CONTROLS,
    CONF_VOLUME_CONTROLS,
    OFF_STATES,
    MediaPlayerEntityFeature,
    parse_supported_features,
)
from .control_entities import (
    SEARCH_CONTROL_ENTITIES_LIMIT,
    ControlEntitySearch,
    HassControlEntitySearchResult,
)
from .helpers import ControlCapabilities, get_control_name, is_entity_id

if TYPE_CHECKING:
    from collections.abc import Callable, Collection, Mapping

    from aiohttp import ClientResponse, ClientSession
    from hass_client.models import (
        Area,
        CompressedState,
        Context,
        Device,
        Entity,
        EntityStateEvent,
        Event,
        State,
    )
    from music_assistant_models.config_entries import ProviderConfig
    from music_assistant_models.player import PlayerMedia
    from music_assistant_models.provider import ProviderManifest

    from music_assistant.mass import MusicAssistant
    from music_assistant.models import ProviderInstanceType

DOMAIN = "hass"
CONF_URL = "url"
CONF_AUTH_TOKEN = "token"
CONF_VERIFY_SSL = "verify_ssl"
FEATURE_DISCOVERY_TIMEOUT = 30
STATE_FETCH_TIMEOUT = 30
STATE_FETCH_BATCH_SIZE = 500
# window to collect entity registry updates in, so an integration registering a
# batch of entities results in a single rebuild of the engine lists
ENGINE_REFRESH_DEBOUNCE = 2
# window in which repeated device lookups reuse one listing, so a burst of players
# connecting does not fetch the (unfilterable) device registry once per player
DEVICE_REGISTRY_CACHE_TTL = 60
# areas are renamed even less often than devices, and only ever supply a label
AREA_REGISTRY_CACHE_TTL = 60

SEARCH_CONTROL_ENTITIES_COMMAND = f"{DOMAIN}/search_control_entities"

# Home Assistant entity domains that back the TTS and AI Task features.
FEATURE_DOMAINS = ("tts", "ai_task")
FEATURE_DOMAIN_PREFIXES = tuple(f"{domain}." for domain in FEATURE_DOMAINS)

# Entity registry fields a change to which can alter the mirrored registry. Beyond the
# mirrored fields themselves, disabled_by decides whether an entity is listed at all, and
# config_entry_id joins them because Home Assistant can clear disabled_by while reporting
# only the move to the other config entry.
REGISTRY_FIELDS_AFFECTING_MIRROR = frozenset(
    {"entity_id", "platform", "device_id", "area_id", "disabled_by", "config_entry_id"}
)


class DeviceMediaPlayerInfo(TypedDict):
    """Home Assistant correlation info for a device that is natively connected elsewhere."""

    # user-facing device name in HA (name_by_user or name)
    name: str | None
    # first enabled media_player entity of the device that supports announcements
    announce_entity_id: str | None


class HassRegistryEntity(NamedTuple):
    """
    Home Assistant entity registry entry, limited to the fields Music Assistant uses.

    The entity ID is not a field: entries are always keyed by it.
    """

    platform: str
    device_id: str | None
    # the area the entity is assigned to directly, overriding the one of its device
    area_id: str | None


async def setup(
    mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
) -> ProviderInstanceType:
    """Initialize provider(instance) with given configuration."""
    return HomeAssistantProvider(mass, manifest, config, set())


def _control_config_entries() -> tuple[ConfigEntry, ...]:
    """Return the config entries holding the entities selected as player controls."""
    return tuple(
        ConfigEntry(
            key=conf_key,
            type=ConfigEntryType.STRING,
            multi_value=True,
            required=True,
            default_value=[],
            category="player_controls",
        )
        for conf_key in (CONF_POWER_CONTROLS, CONF_VOLUME_CONTROLS, CONF_MUTE_CONTROLS)
    )


class HomeAssistantProvider(PluginProvider):
    """Home Assistant Plugin for Music Assistant."""

    hass: HomeAssistantClient
    _listen_task: asyncio.Task[None] | None = None
    _player_controls: dict[str, PlayerControl] | None = None
    _unsubscribe_controls: Callable[[], None] | None = None
    _unsubscribe_entity_registry: Callable[[], None] | None = None
    _engine_refresh_task: asyncio.Task[None] | None = None
    _ai_engines: list[AIEngine]
    _tts_engines: list[TTSEngine]
    _startup_complete: bool = False
    _entity_registry: Mapping[str, HassRegistryEntity] | None = None
    _entity_registry_generation: int = 0
    _entity_registry_lock: asyncio.Lock
    _wanted_controls: dict[str, ControlCapabilities] | None = None
    _control_reconcile_lock: asyncio.Lock
    _control_entity_search: ControlEntitySearch
    _unregister_search_command: Callable[[], None] | None = None

    @property
    def url(self) -> str | None:
        """Return the configured Home Assistant URL, or None if not configured."""
        url = self.get_setup_value(CONF_URL)
        if isinstance(url, str) and url:
            return url
        return None

    @property
    def entity_registry_generation(self) -> int:
        """Return a counter that changes whenever the mirrored entity registry is dropped."""
        return self._entity_registry_generation

    async def get_config_entries(self) -> tuple[ConfigEntry, ...]:
        """
        Return the (options) config entries for the Home Assistant provider.

        The connection URL and authentication token are collected by the setup flow (see
        setup_flow.py) unless running as a Home Assistant add-on, where they are fixed; only
        the player-control and feature options are configurable here.
        """
        base_entries: tuple[ConfigEntry, ...]
        if self.mass.running_as_hass_addon:
            # on supervisor, we use the internal url
            # token set to None for auto retrieval
            base_entries = (
                ConfigEntry(
                    key=CONF_URL,
                    type=ConfigEntryType.STRING,
                    label=CONF_URL,
                    required=True,
                    default_value="http://supervisor/core/api",
                    value="http://supervisor/core/api",
                    hidden=True,
                ),
                ConfigEntry(
                    key=CONF_AUTH_TOKEN,
                    type=ConfigEntryType.STRING,
                    label=CONF_AUTH_TOKEN,
                    required=False,
                    default_value=None,
                    value=None,
                    hidden=True,
                ),
                ConfigEntry(
                    key=CONF_VERIFY_SSL,
                    type=ConfigEntryType.BOOLEAN,
                    label=CONF_VERIFY_SSL,
                    required=False,
                    default_value=False,
                    hidden=True,
                ),
            )
        else:
            # url/token/verify_ssl are collected by the setup flow instead (see setup_flow.py)
            base_entries = ()

        return (*base_entries, *_control_config_entries())

    async def handle_async_init(self) -> None:
        """Handle async initialization of the plugin."""
        if self._listen_task and not self._listen_task.done():
            msg = "Home Assistant listener is already running"
            raise SetupFailedError(msg)
        self._startup_complete = False
        self._player_controls = {}
        self._wanted_controls = None
        self._control_reconcile_lock = asyncio.Lock()
        self._ai_engines = []
        self._tts_engines = []
        url = get_websocket_url(cast("str", self.get_setup_value(CONF_URL)))
        token = self.get_setup_value(CONF_AUTH_TOKEN)
        logging.getLogger("hass_client").setLevel(self.logger.level + 10)
        ssl = bool(self.get_setup_value(CONF_VERIFY_SSL, True))
        http_session = self.mass.http_session if ssl else self.mass.http_session_no_ssl
        self.hass = HomeAssistantClient(url, token, http_session)
        self._entity_registry = None
        self._entity_registry_lock = asyncio.Lock()
        self._control_entity_search = ControlEntitySearch(self)
        # registering here rather than in loaded_in_mass pairs the command with the teardown
        # in _disconnect_hass, so a reload can never leave it registered twice
        self._unregister_search_command = self.mass.register_api_command(
            SEARCH_CONTROL_ENTITIES_COMMAND,
            self.search_control_entities,
            required_scope=Scope.CONFIG_PROVIDERS_READ,
        )
        try:
            await self.hass.connect()
        except BaseHassClientError as err:
            await self._cleanup_failed_init()
            err_msg = str(err) or err.__class__.__name__
            raise SetupFailedError(err_msg) from err
        self._listen_task = self.mass.create_task(self._hass_listener())
        try:
            # the registry subscription must be live before the first registry read, so no
            # registry change can slip through unnoticed; _disconnect_hass tears the
            # subscription down again on the failure paths below
            await self._subscribe_entity_registry()
            await self._resolve_startup_features()
        except asyncio.CancelledError:
            await self._cleanup_failed_init()
            raise
        except BaseHassClientError as err:
            await self._cleanup_failed_init()
            err_msg = str(err) or err.__class__.__name__
            raise SetupFailedError(err_msg) from err
        except Exception:
            await self._cleanup_failed_init()
            raise

    async def loaded_in_mass(self) -> None:
        """Call after the provider has been loaded."""
        await self._register_player_controls()

    async def unload(self, is_removed: bool = False) -> None:
        """
        Handle unload/close of the provider.

        Called when provider is deregistered (e.g. MA exiting or config reloading).
        """
        # unregister all player controls
        if self._player_controls:
            for entity_id in self._player_controls:
                self.mass.players.remove_player_control(entity_id)
        self._startup_complete = False
        await self._disconnect_hass()

    async def update_config(self, config: ProviderConfig, changed_keys: set[str]) -> None:
        """
        Handle logic when the config is updated.

        A change limited to the player control selection is applied in place, so adding or
        removing a control does not drop and re-establish the Home Assistant connection.
        Any other change reloads the provider as usual.

        Raises when the in place update fails (because Home Assistant is unreachable, for
        example); the controls are then left as they were and a later update retries.

        :param config: The updated provider config.
        :param changed_keys: The keys that changed in the given config.
        """
        control_keys = {
            f"values/{conf_key}"
            for conf_key in (CONF_POWER_CONTROLS, CONF_VOLUME_CONTROLS, CONF_MUTE_CONTROLS)
        }
        if not changed_keys or not changed_keys <= control_keys:
            await super().update_config(config, changed_keys)
            return
        # store the new config before reconciling: the control lists are read back from it
        self.config = config
        await self._register_player_controls()

    async def get_diagnostics(self) -> dict[str, SerializableType]:
        """Return diagnostics info for this provider to include in diagnostics reports."""
        return {
            "connected": self.hass.connected,
            "ha_version": self.hass.version,
            "listener_active": self._listen_task is not None and not self._listen_task.done(),
            "player_controls": len(self._player_controls) if self._player_controls else 0,
        }

    async def get_entity_registry(self) -> Mapping[str, HassRegistryEntity]:
        """
        Return the Home Assistant entity registry, keyed by entity ID.

        Entities that are disabled in Home Assistant are absent from the result, and so are
        entities without a unique ID: those are not part of Home Assistant's registry at all.

        The result is shared between all callers and is read-only: both the mapping and
        its entries reject writes.
        """
        if (registry := self._entity_registry) is not None:
            return registry
        async with self._entity_registry_lock:
            if (registry := self._entity_registry) is None:
                generation = self._entity_registry_generation
                registry = await self._fetch_entity_registry()
                # a registry change while the fetch was in flight leaves the listing stale
                # on arrival, so serve it to this caller but keep it out of the cache
                if generation == self._entity_registry_generation:
                    self._entity_registry = registry
            return registry

    async def get_entity_registry_entries(self, entity_ids: Collection[str]) -> dict[str, Entity]:
        """
        Return the full Home Assistant entity registry entries of the given entities.

        :param entity_ids: The entity IDs to look up.
        :return: The registry entries keyed by entity ID; entities unknown to
            Home Assistant are absent from the result.
        """
        if not entity_ids:
            return {}
        result = cast(
            "dict[str, Entity | None]",
            await self.hass.send_command(
                "config/entity_registry/get_entries", entity_ids=list(entity_ids)
            ),
        )
        return {entity_id: entry for entity_id, entry in result.items() if entry is not None}

    async def get_device_registry(self) -> dict[str, Device]:
        """
        Return the Home Assistant device registry, keyed by device ID.

        Home Assistant offers no abbreviated variant of the device registry listing, so the
        entries carry all of their fields. The listing is reused for a short while, so a
        device change may take up to DEVICE_REGISTRY_CACHE_TTL seconds to be reflected.
        """
        return await self._fetch_device_registry()

    async def get_area_registry(self) -> dict[str, Area]:
        """
        Return the Home Assistant area registry, keyed by area ID.

        The listing is reused for a short while, so an area change may take up to
        AREA_REGISTRY_CACHE_TTL seconds to be reflected.
        """
        return await self._fetch_area_registry()

    async def search_control_entities(
        self,
        search: str | None = None,
        control_type: str | None = None,
        limit: int = SEARCH_CONTROL_ENTITIES_LIMIT,
    ) -> HassControlEntitySearchResult:
        """
        Search the Home Assistant entities that can be used as a player control.

        Music Assistant's own players are never part of the result. Consecutive searches are
        served from a short lived cache that an entity registry change drops right away, so a
        newly added or removed entity shows up immediately, while a device or area rename can
        lag by up to a minute.

        :param search: Text to match, case insensitively, against the entity ID, the entity
            name, its device name and its area name. Every whitespace separated word must
            match one of those fields, though not necessarily the same one. All eligible
            entities match when omitted.
        :param control_type: Restrict the result to entities that can serve this control role,
            given as one of the provider's control config keys (``power_controls``,
            ``volume_controls`` or ``mute_controls``). All roles are returned when omitted.
        :param limit: Maximum number of entities (not groups) to return, itself capped at
            ``SEARCH_CONTROL_ENTITIES_MAX_LIMIT``.
        :return: The matching entities grouped by the device and area they belong to, ordered
            by area, device and entity name, plus a flag telling whether matches were left out
            to honor the limit.
        """
        return await self._control_entity_search.search(search, control_type, limit)

    async def get_media_player_device_infos(
        self,
        mac_addresses: Collection[str],
        platform: str,
    ) -> dict[str, DeviceMediaPlayerInfo]:
        """
        Correlate devices (by MAC address) to their HA name and media_player entity.

        Used for devices that are natively connected to Music Assistant but also
        present in Home Assistant, to pick up their HA device name and their
        (announcement-capable) media_player entity.

        :param mac_addresses: Device MAC addresses to look up (case-insensitive).
        :param platform: The HA integration domain the media_player entities must belong to.
        :return: Correlation info keyed by lowercased MAC address; devices unknown
            to Home Assistant are absent from the result.
        """
        wanted_macs = {mac.lower() for mac in mac_addresses}
        if not wanted_macs:
            return {}
        device_registry = await self.get_device_registry()
        device_by_mac: dict[str, Device] = {
            connection[1].lower(): device
            for device in device_registry.values()
            for connection in device.get("connections", [])
            if len(connection) == 2
            and connection[0] == "mac"
            and connection[1].lower() in wanted_macs
        }
        if not device_by_mac:
            return {}
        media_players_by_device: dict[str, list[str]] = {}
        for entity_id, entry in (await self.get_entity_registry()).items():
            if (
                entry.platform == platform
                and entity_id.startswith("media_player.")
                and (device_id := entry.device_id)
            ):
                media_players_by_device.setdefault(device_id, []).append(entity_id)
        candidates_by_mac = {
            mac: media_players_by_device.get(device["id"], [])
            for mac, device in device_by_mac.items()
        }
        states = {
            state["entity_id"]: state
            for state in await self.get_states(
                entity_ids=[
                    entity_id
                    for entity_ids in candidates_by_mac.values()
                    for entity_id in entity_ids
                ]
            )
        }

        def _supports_announce(entity_id: str) -> bool:
            if (state := states.get(entity_id)) is None:
                return False
            supported_features = parse_supported_features(
                state["attributes"].get("supported_features"), entity_id, self.logger
            )
            return MediaPlayerEntityFeature.MEDIA_ANNOUNCE in supported_features

        return {
            mac: DeviceMediaPlayerInfo(
                name=device["name_by_user"] or device["name"],
                announce_entity_id=next(
                    (
                        entity_id
                        for entity_id in candidates_by_mac[mac]
                        if _supports_announce(entity_id)
                    ),
                    None,
                ),
            )
            for mac, device in device_by_mac.items()
        }

    async def get_user_details(self, ha_user_id: str) -> tuple[str | None, str | None, str | None]:
        """
        Get user username, display name and avatar URL from Home Assistant.

        Looks up the user in config/auth/list for username, and the person entity
        for display name and picture URL.

        :param ha_user_id: Home Assistant user ID.
        :return: Tuple of (username, display_name, avatar_url) or all None if not found.
        """
        try:
            username: str | None = None
            display_name: str | None = None
            avatar_url: str | None = None

            # Get username from config/auth/list (admin endpoint, we have admin access)
            try:
                users = await self.hass.send_command("config/auth/list")
                for user in users or []:
                    if user.get("id") == ha_user_id:
                        username = user.get("username")
                        # Also get name as fallback display name
                        if not display_name:
                            display_name = user.get("name")
                        break
            except Exception as err:
                self.logger.log(VERBOSE_LOG_LEVEL, "Failed to get HA user list: %s", err)

            # Get external URL for building avatar URL
            ha_url: str | None = None
            try:
                network_urls = await self.hass.send_command("network/url")
                if network_urls:
                    ha_url = network_urls.get("external") or network_urls.get("internal")
            except Exception as err:
                self.logger.log(VERBOSE_LOG_LEVEL, "Failed to get HA network URLs: %s", err)

            # Find person linked to this HA user ID for display name and avatar
            try:
                persons = await self.hass.send_command("person/list")
                # person/list returns {storage: [...], config: [...]}
                all_persons = (persons.get("storage") or []) + (persons.get("config") or [])
                for person in all_persons:
                    if person.get("user_id") == ha_user_id:
                        # Person name takes priority for display name
                        if person_name := person.get("name"):
                            display_name = person_name
                        if (person_picture := person.get("picture")) and ha_url:
                            avatar_url = f"{ha_url.rstrip('/')}{person_picture}"
                        break
            except Exception as err:
                self.logger.log(VERBOSE_LOG_LEVEL, "Failed to get HA person details: %s", err)

            self.logger.log(
                VERBOSE_LOG_LEVEL,
                "get_user_details for %s: username=%s, display_name=%s, avatar_url=%s",
                ha_user_id,
                username,
                display_name,
                avatar_url,
            )
            return username, display_name, avatar_url
        except Exception as err:
            self.logger.warning("Failed to get HA user details: %s", err)
            return None, None, None
