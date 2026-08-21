"""
Controller to stream audio to players.

The streams controller hosts a basic, unprotected HTTP-only webserver
purely to stream audio packets to players.
"""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import AsyncGenerator
from contextlib import aclosing
from math import ceil
from typing import TYPE_CHECKING, cast

from aiofiles.os import wrap
from aiohttp import web
from music_assistant_models.audio_processing import AudioQueueProcessing
from music_assistant_models.config_entries import ConfigEntry, ConfigValueOption
from music_assistant_models.enums import (
    ConfigEntryType,
    ContentType,
    CrossfadeMode,
    MediaType,
    PlayerFeature,
    StreamType,
    VolumeNormalizationMode,
)
from music_assistant_models.errors import AudioError, InvalidDataError, ProviderUnavailableError
from music_assistant_models.media_items import AudioFormat

from music_assistant.constants import (
    ANNOUNCE_ALERT_FILE,
    CONF_BIND_IP,
    CONF_BIND_PORT,
    CONF_CROSSFADE_DURATION,
    CONF_CROSSFADE_MODE,
    CONF_ENTRY_ENABLE_ICY_METADATA,
    CONF_ENTRY_LOG_LEVEL,
    CONF_ENTRY_VOLUME_NORMALIZATION_TARGET,
    CONF_HTTP_PROFILE,
    CONF_OUTPUT_CODEC,
    CONF_PLAYER_QUEUES,
    CONF_PREFER_WAV_FOR_LIVE_SOURCES,
    CONF_PUBLISH_IP,
    CONF_VALUE_AUTO,
    CONF_VOLUME_NORMALIZATION_FIXED_GAIN_RADIO,
    CONF_VOLUME_NORMALIZATION_FIXED_GAIN_TRACKS,
    CONF_VOLUME_NORMALIZATION_RADIO,
    CONF_VOLUME_NORMALIZATION_TRACKS,
    DEFAULT_STREAM_HEADERS,
    ICY_HEADERS,
    SILENCE_FILE,
    VERBOSE_LOG_LEVEL,
    WILDCARD_BIND_IPS,
)
from music_assistant.controllers.players.helpers import AnnounceData
from music_assistant.controllers.streams.audio import StreamsAudio
from music_assistant.controllers.streams.constants import (
    CONF_ALLOW_CROSSFADE_SAME_ALBUM,
    CONF_BUFFER_SIZE,
    CONF_BUFFER_SIZE_DEFAULT,
    CONF_SMART_FADES_LOG_LEVEL,
    DEFAULT_PORT,
    get_available_buffer_sizes,
)
from music_assistant.controllers.streams.smart_fades.analyzer import SmartFadesAnalyzer
from music_assistant.helpers.audio import (
    calculate_content_length,
    create_streaming_wave_header,
    get_content_length,
    get_mime_type,
    store_content_length_in_cache,
)
from music_assistant.helpers.buffered_generator import buffered
from music_assistant.helpers.ffmpeg import (
    CACHE_ATTR_FFMPEG_VERSION,
    CACHE_ATTR_LIBSOXR_PRESENT,
    check_ffmpeg_version,
    get_ffmpeg_stream,
)
from music_assistant.helpers.ffmpeg import LOGGER as FFMPEG_LOGGER
from music_assistant.helpers.util import (
    format_ip_for_url,
    get_ip_addresses,
    get_publish_ip_candidates,
    get_source_ip_for_target,
    sanitize_http_header_value,
)
from music_assistant.helpers.webserver import Webserver, redact_sensitive_headers
from music_assistant.models.core_controller import CoreController
from music_assistant.models.music_provider import MusicProvider
from music_assistant.models.plugin import PluginProvider, PluginSource
from music_assistant.models.smart_fades import SmartFadesMode
from music_assistant.providers.universal_group.constants import UGP_PREFIX
from music_assistant.providers.universal_group.player import UniversalGroupPlayer

if TYPE_CHECKING:
    from music_assistant_models.config_entries import CoreConfig
    from music_assistant_models.player import PlayerMedia

    from music_assistant.helpers.json import SerializableType
    from music_assistant.mass import MusicAssistant


isfile = wrap(os.path.isfile)


class StreamsController(CoreController):
    """Controller to stream audio to players."""

    domain: str = "streams"

    def __init__(self, mass: MusicAssistant) -> None:
        """Initialize instance."""
        super().__init__(mass)
        self._server = Webserver(self.logger, enable_dynamic_routes=True)
        self.register_dynamic_route = self._server.register_dynamic_route
        self.unregister_dynamic_route = self._server.unregister_dynamic_route
        self.manifest.name = "Streamserver"
        self.manifest.description = (
            "Music Assistant's core controller that is responsible for "
            "streaming audio to players on the local network."
        )
        self.manifest.icon = "cast-audio"
        self.announcement_renderer = AnnouncementRenderer()
        self.live_announcements = LiveAnnouncementManager(mass, self.logger)
        self._bind_ip: str = "0.0.0.0"
        self._base_url: str = ""
        self._configured_publish_ip: str | None = None
        # every address players may reach this host on, best candidate first; publish_ip is
        # the first of them and the network fingerprint watches the whole list for changes
        self._publish_addresses: list[str] = []
        # the network as it was at the previous setup, to spot a runtime change
        self._network_fingerprint: tuple[str, str, int, tuple[str, ...]] | None = None
        self.audio = StreamsAudio(mass)
        self._smart_fades_analyzer = SmartFadesAnalyzer(self)

    def output_stream_active(self) -> bool:
        """Return whether a queue stream (single item or flow) is actively serving a player."""
        return self._active_output_streams > 0

    async def get_diagnostics(self) -> dict[str, Any]:
        """Return diagnostics info for this controller to include in diagnostics reports."""
        return {
            "active_output_streams": self._active_output_streams,
            "active_announcements": len(self.announcements),
        }

    def output_stream_active(self) -> bool:
        """Return whether a queue stream (single item or flow) is actively serving a player."""
        return self._active_output_streams > 0

    async def get_diagnostics(self) -> dict[str, SerializableType]:
        """Return diagnostics info for this controller to include in diagnostics reports."""
        return {
            "ffmpeg_version": get_global_cache_value(CACHE_ATTR_FFMPEG_VERSION),
            "libsoxr_support": get_global_cache_value(CACHE_ATTR_LIBSOXR_PRESENT),
            "active_output_streams": self._active_output_streams,
            "active_announcements": self.announcement_renderer.active_announcements,
            "active_announcement_renders": self.announcement_renderer.active_renders,
            "active_live_announcements": self.live_announcements.active_sessions,
            "publish_ip_configured": self._configured_publish_ip is not None,
        }

    @property
    def base_url(self) -> str:
        """Return the base_url for the streamserver."""
        return self._base_url

    @property
    def bind_ip(self) -> str:
        """Return the IP address this streamserver is bound to."""
        return self._bind_ip

    @property
    def smart_fades_analyzer(self) -> SmartFadesAnalyzer:
        """Return the SmartFadesAnalyzer instance."""
        return self._smart_fades_analyzer

    async def get_config_entries(
        self, action: str | None = None, values: dict[str, ConfigValueType] | None = None
    ) -> tuple[ConfigEntry, ...]:
        """Return all Config Entries for this core module (if any)."""
        ip_addresses = await get_ip_addresses(include_ipv6=True)
        return (
            ConfigEntry(
                key=CONF_BUFFER_SIZE,
                type=ConfigEntryType.STRING,
                default_value=CONF_BUFFER_SIZE_DEFAULT,
                label="Audio buffer size",
                description="Controls how much audio is buffered in memory. "
                "A larger buffer improves playback stability and seeking "
                "but uses more memory.\n\n"
                "- **Minimal**: Small buffer, "
                "recommended for memory-constrained devices.\n"
                "- **Balanced**: Moderate buffer, "
                "good balance for most systems.\n"
                "- **Maximum**: Large buffer, "
                "best performance for systems with plenty of memory.",
                # Only offer presets the host's RAM can sustain (Balanced >= 4GB,
                # Maximum >= 7GB); see get_available_buffer_sizes.
                options=[
                    ConfigValueOption(size.value.title(), size.value)
                    for size in get_available_buffer_sizes()
                ],
                required=False,
                category="playback",
            ),
            ConfigEntry(
                key=CONF_VOLUME_NORMALIZATION_RADIO,
                type=ConfigEntryType.STRING,
                default_value=VolumeNormalizationMode.FALLBACK_DYNAMIC,
                options=[
                    ConfigValueOption(x.value, title=x.value.replace("_", " ").title())
                    for x in VolumeNormalizationMode
                ],
                category="playback",
            ),
            ConfigEntry(
                key=CONF_VOLUME_NORMALIZATION_TRACKS,
                type=ConfigEntryType.STRING,
                default_value=VolumeNormalizationMode.FALLBACK_DYNAMIC,
                options=[
                    ConfigValueOption(x.value, title=x.value.replace("_", " ").title())
                    for x in VolumeNormalizationMode
                ],
                category="playback",
            ),
            ConfigEntry(
                key=CONF_VOLUME_NORMALIZATION_FIXED_GAIN_RADIO,
                type=ConfigEntryType.FLOAT,
                range=(-20, 10),
                default_value=-6,
                category="playback",
            ),
            ConfigEntry(
                key=CONF_VOLUME_NORMALIZATION_FIXED_GAIN_TRACKS,
                type=ConfigEntryType.FLOAT,
                range=(-20, 10),
                default_value=-6,
                category="playback",
            ),
            CONF_ENTRY_VOLUME_NORMALIZATION_TARGET,
            ConfigEntry(
                key=CONF_ALLOW_CROSSFADE_SAME_ALBUM,
                type=ConfigEntryType.BOOLEAN,
                default_value=False,
                category="playback",
            ),
            ConfigEntry(
                key=CONF_PUBLISH_IP,
                type=ConfigEntryType.STRING,
                default_value=CONF_VALUE_AUTO,
                required=False,
                category="generic",
                advanced=True,
                requires_reload=True,
            ),
            ConfigEntry(
                key=CONF_BIND_PORT,
                type=ConfigEntryType.INTEGER,
                default_value=DEFAULT_PORT,
                category="generic",
                advanced=True,
                requires_reload=True,
            ),
            ConfigEntry(
                key=CONF_BIND_IP,
                type=ConfigEntryType.STRING,
                default_value="0.0.0.0",
                options=[ConfigValueOption(x, x) for x in {"0.0.0.0", "::", *ip_addresses}],
                label="Bind to IP/interface",
                description="Start the stream server on this specific interface. \n"
                "Use 0.0.0.0 or :: to bind to all interfaces, which is the default. \n"
                "This is an advanced setting that should normally "
                "not be adjusted in regular setups.",
                category="generic",
                advanced=True,
                required=False,
                requires_reload=True,
            ),
            ConfigEntry(
                key=CONF_SMART_FADES_LOG_LEVEL,
                type=ConfigEntryType.STRING,
                options=CONF_ENTRY_LOG_LEVEL.options,
                default_value="GLOBAL",
                category="generic",
                advanced=True,
            ),
        )

    async def setup(self, config: CoreConfig) -> None:
        """Async initialize of module."""
        # initialize the audio sub-controller (needs mass.streams to be set)
        self.audio.setup()
        # copy log level to audio/ffmpeg loggers
        self.audio.logger.setLevel(self.logger.level)
        FFMPEG_LOGGER.setLevel(self.logger.level)
        self._setup_smart_fades_logger(config)
        # perform check for ffmpeg version
        await check_ffmpeg_version()
        # start the webserver
        self.publish_port = config.get_value(CONF_BIND_PORT, DEFAULT_PORT)
        self.publish_ip = config.get_value(CONF_PUBLISH_IP)
        self._bind_ip = bind_ip = str(config.get_value(CONF_BIND_IP))
        # print a big fat message in the log where the streamserver is running
        # because this is a common source of issues for people with more complex setups
        self.logger.log(
            logging.INFO if self.mass.config.onboard_done else logging.WARNING,
            "\n\n################################################################################\n"
            "Starting streamserver on  %s:%s\n"
            "This is the IP address that is communicated to players.\n"
            "If this is incorrect, audio will not play!\n"
            "See the documentation how to configure the publish IP for the Streamserver\n"
            "in Settings --> Core modules --> Streamserver\n"
            "################################################################################\n",
            self.publish_ip,
            self.publish_port,
        )
        publish_candidates = await get_publish_ip_candidates(include_ipv6=True)
        bind_ip = str(config.get_value(CONF_BIND_IP))
        self._resolve_publish_state(bind_ip, publish_candidates)
        await self._server.setup(
            bind_ip=bind_ip,
            bind_port=cast("int", self.publish_port),
            static_routes=[
                (
                    "*",
                    "/flow/{session_id}/{queue_id}/{queue_item_id}/{player_id}.{fmt}",
                    self.serve_queue_flow_stream,
                ),
                (
                    "*",
                    "/single/{session_id}/{queue_id}/{queue_item_id}/{player_id}.{fmt}",
                    self.serve_queue_item_stream,
                ),
                (
                    "*",
                    "/command/{session_id}/{queue_id}/{command}.mp3",
                    self.serve_command_request,
                ),
                ("*", "/announcement/{player_id}.{fmt}", self.serve_announcement_stream),
                (
                    "*",
                    "/pluginsource/{plugin_source}/{player_id}.{fmt}",
                    self.serve_plugin_source_stream,
                ),
            ],
        )
        # adopt what the server actually bound to: a configured port of 0 is only resolved
        # by the OS at bind time and an unavailable bind IP falls back to all interfaces
        self.publish_port = cast("int", self._server.port)
        self._resolve_publish_state(self._server.bind_ip or DEFAULT_HOST, publish_candidates)
        # print a big fat message in the log where the streamserver is running
        # because this is a common source of issues for people with more complex setups
        self.logger.log(
            logging.INFO if self.mass.config.onboard_done else logging.WARNING,
            "\n\n################################################################################\n"
            "Started streamserver on %s:%s\n"
            "This is the IP address that is communicated to players.\n"
            "If this is incorrect, audio will not play!\n"
            "See the documentation for how to configure the publish IP for the Streamserver\n"
            "in Settings --> System --> Streams\n"
            "################################################################################\n",
            self.publish_ip,
            self.publish_port,
        )
        await self._reload_network_dependent_providers()

    async def post_setup(self) -> None:
        """Handle logic after all core controllers have been set up."""
        # the inbound half of a live announcement rides on the webserver: it is the only
        # one of the two servers that authenticates (and that browsers reach over https)
        self.live_announcements.setup()

    async def close(self) -> None:
        """Cleanup on exit."""
        await self._server.close()

    async def resolve_stream_url(self, player_id: str, media: PlayerMedia) -> str:
        """
        Resolve the stream URL for the given PlayerMedia.

        :param player_id: The (protocol) player ID requesting the stream.
        :param media: The PlayerMedia object for which to resolve the stream URL.
        :return: The resolved stream URL as a string.
        """
        if media.media_type == MediaType.ANNOUNCEMENT:
            return media.uri
        if media.media_type == MediaType.PLUGIN_SOURCE:
            if media.custom_data and (source_id := media.custom_data.get("source_id")):
                plugin_source = self.mass.players.get_plugin_source(source_id)
                if plugin_source:
                    return await self.get_plugin_source_url(plugin_source, player_id)
            return media.uri
        protocol_player = self.mass.players.get_player(player_id)
        conf_output_codec = cast(
            "str",
            protocol_player.config.get_value(CONF_OUTPUT_CODEC, default="flac")
            if protocol_player
            else "flac",
        )
        output_codec = ContentType.try_parse(conf_output_codec)
        fmt = output_codec.value
        # handle raw pcm without exact format specifiers
        if output_codec.is_pcm() and ";" not in fmt:
            fmt += f";codec=pcm;rate={44100};bitrate={16};channels={2}"
        session_id = media.queue_session_id
        queue_item_id = media.queue_item_id
        if not session_id or not queue_item_id:
            raise InvalidDataError("Can not resolve stream URL: Invalid PlayerMedia data")
        queue_id = media.source_id
        queue = self.mass.player_queues.get(queue_id) if queue_id else None
        crossfade_needs_flow_mode = (
            # if the player(queue) has crossfade enabled but the player(protocol) does not support
            # gapless playback, we need to enforce flow mode
            queue_id
            and (queue_player := self.mass.players.get_player(queue_id))
            and queue_player.config.get_value(CONF_SMART_FADES_MODE) != SmartFadesMode.DISABLED
            and protocol_player
            and not protocol_player.supports_gapless
        )
        # the audio overlay is mixed into the queue's continuous (flow) stream;
        # per-item requests would restart the overlay at every track boundary
        overlay_needs_flow_mode = queue is not None and overlay_active(queue)
        # Determine flow_mode based on the actual player's capabilities.
        # This is done here (just-in-time) because the player's protocol determines this
        flow_mode = (
            protocol_player is not None
            and (protocol_player.flow_mode or crossfade_needs_flow_mode)
            and media.media_type not in (MediaType.RADIO, MediaType.PLUGIN_SOURCE)
        )
        base_path = "flow" if flow_mode else "single"
        return f"{self._server.base_url}/{base_path}/{session_id}/{queue_id}/{queue_item_id}/{player_id}.{fmt}"  # noqa: E501

    async def get_plugin_source_url(self, plugin_source: PluginSource, player_id: str) -> str:
        """Get the url for the Plugin Source stream/proxy."""
        if plugin_source.audio_format.content_type.is_pcm():
            fmt = ContentType.WAV.value
        else:
            fmt = plugin_source.audio_format.content_type.value
        return f"{self._server.base_url}/pluginsource/{plugin_source.id}/{player_id}.{fmt}"

    async def serve_queue_item_stream(self, request: web.Request) -> web.StreamResponse:  # noqa: PLR0915
        """Stream single queueitem audio to a player."""
        self._log_request(request)
        queue_id = request.match_info["queue_id"]
        player_id = request.match_info["player_id"]
        if not (queue := self.mass.player_queues.get(queue_id)):
            raise web.HTTPNotFound(reason=f"Unknown Queue: {queue_id}")
        session_id = request.match_info["session_id"]
        pq_data = self.mass.player_queues.queue_data(queue.queue_id)
        if pq_data.session_id is None or session_id != pq_data.session_id:
            raise web.HTTPNotFound(reason=f"Unknown (or invalid) session: {session_id}")
        if not (player := self.mass.players.get_player(player_id)):
            raise web.HTTPNotFound(reason=f"Unknown Player: {player_id}")
        queue_item_id = request.match_info["queue_item_id"]
        queue_item = self.mass.player_queues.get_item(queue_id, queue_item_id)
        if not queue_item:
            raise web.HTTPNotFound(reason=f"Unknown Queue item: {queue_item_id}")
        if not queue_item.streamdetails:
            try:
                queue_item.streamdetails = await self.audio.get_stream_details(
                    queue_item=queue_item
                )
            except Exception as e:
                self.logger.error(
                    "Failed to get streamdetails for QueueItem %s: %s", queue_item_id, e
                )
                queue_item.available = False
                raise web.HTTPNotFound(reason=f"No streamdetails for Queue item: {queue_item_id}")

        # pick output format based on the streamdetails and player capabilities
        pcm_format = await self.audio.select_pcm_format(
            player=player, streamdetails=queue_item.streamdetails, smartfades_enabled=True
        )
        output_format = await self.audio.get_output_format(
            output_format_str=request.match_info["fmt"],
            player=player,
            content_sample_rate=pcm_format.sample_rate,
            content_bit_depth=pcm_format.bit_depth,
        )

        # prepare request, add some DLNA/UPNP compatible headers
        # icy-name is sanitized to avoid a "Potential header injection attack" exception by aiohttp
        # see https://github.com/music-assistant/support/issues/4913
        headers = {
            **DEFAULT_STREAM_HEADERS,
            "icy-name": queue_item.name.replace("\n", " ").replace("\r", " ").replace("\t", " "),
            "contentFeatures.dlna.org": "DLNA.ORG_OP=01;DLNA.ORG_FLAGS=01500000000000000000000000000000",  # noqa: E501
            "Accept-Ranges": "none",
            "Content-Type": get_mime_type(output_format.output_format_str),
        }

        resp = web.StreamResponse(status=200, reason="OK", headers=headers)
        resp.content_type = get_mime_type(output_format.output_format_str)
        http_profile = await self.mass.config.get_player_config_value(
            player_id, CONF_HTTP_PROFILE, default="default", return_type=str
        )
        if http_profile == "forced_content_length" and not queue_item.duration:
            # just set an insane high content length to make sure the player keeps playing
            resp.content_length = calculate_content_length(output_format, 12 * 3600)
        elif http_profile == "forced_content_length" and queue_item.duration:
            # estimate content length based on effective duration
            # account for seek position (e.g., crossfade from previous track)
            seek_pos = queue_item.streamdetails.seek_position if queue_item.streamdetails else 0
            effective_duration = max(queue_item.duration - seek_pos, 1)
            # use cached actual bytes-per-second if available (from a previous stream)
            resp.content_length = await get_content_length(
                self.mass, queue_item.uri, output_format, effective_duration
            )
        elif http_profile == "chunked":
            resp.enable_chunked_encoding()

        await resp.prepare(request)

        # return early if this is not a GET request
        if request.method != "GET":
            return resp

        if queue_item.media_type != MediaType.TRACK:
            # no crossfade on non-tracks
            smart_fades_mode = SmartFadesMode.DISABLED
        else:
            smart_fades_mode = await self.mass.config.get_player_config_value(
                queue.queue_id, CONF_SMART_FADES_MODE, return_type=SmartFadesMode
            )
            standard_crossfade_duration = self.mass.config.get_raw_player_config_value(
                queue.queue_id, CONF_CROSSFADE_DURATION, 10
            )
        if (
            smart_fades_mode != SmartFadesMode.DISABLED
            and PlayerFeature.GAPLESS_PLAYBACK not in player.state.supported_features
        ):
            self.logger.warning(
                "Crossfade disabled: Player %s does not support gapless playback, "
                "consider enabling flow mode to enable crossfade on this player.",
                player.state.name if player else "Unknown Player",
            )
            smart_fades_mode = SmartFadesMode.DISABLED

        if smart_fades_mode != SmartFadesMode.DISABLED:
            # crossfade is enabled, use special crossfaded single item stream
            # where the crossfade of the next track is present in the stream of
            # a single track. This only works if the player supports gapless playback!
            audio_input = self.audio.get_queue_item_stream_with_smartfade(
                player=player,
                queue_item=queue_item,
                pcm_format=pcm_format,
                smart_fades_mode=smart_fades_mode,
                standard_crossfade_duration=standard_crossfade_duration,
            )
        else:
            # no crossfade, just a regular single item stream
            audio_input = self.audio.get_queue_item_stream(
                queue_item=queue_item,
                pcm_format=pcm_format,
                seek_position=queue_item.streamdetails.seek_position,
                playback_speed=cast(
                    "float", queue_item.extra_attributes.get("playback_speed", 1.0)
                ),
            )
        # stream the audio
        # this final ffmpeg process in the chain will convert the raw, lossless PCM audio into
        # the desired output format for the player including any player specific filter params
        # such as channels mixing, DSP, resampling and, only if needed, encoding to lossy formats
        first_chunk_received = False
        bytes_sent = 0
        async for chunk in get_ffmpeg_stream(
            audio_input=audio_input,
            input_format=pcm_format,
            output_format=output_format,
            filter_params=self.audio.get_player_filter_params(
                player_id=player.player_id, input_format=pcm_format, output_format=output_format
            ),
        ):
            try:
                await resp.write(chunk)
                bytes_sent += len(chunk)
                if not first_chunk_received:
                    first_chunk_received = True
                    # inform the queue that the track is now loaded in the buffer
                    # so for example the next track can be enqueued
                    self.mass.player_queues.track_loaded_in_buffer(
                        queue_item.queue_id, queue_item.queue_item_id
                    )
            except (BrokenPipeError, ConnectionResetError, ConnectionError) as err:
                if (
                    first_chunk_received
                    and not player.stop_called
                    and queue_item.streamdetails.duration  # ignore for radio streams
                ):
                    # Player disconnected (unexpected) after receiving at least some data
                    # This could indicate buffering issues, network problems,
                    # or player-specific issues.
                    self.logger.warning(
                        "Player %s disconnected prematurely from stream for %s (%s) - "
                        "error: %s, sent %d bytes, content_length=%s",
                        queue.display_name,
                        queue_item.name,
                        queue_item.uri,
                        err.__class__.__name__,
                        bytes_sent,
                        resp.content_length,
                    )
                break
        if queue_item.streamdetails.stream_error:
            self.logger.error(
                "Error streaming QueueItem %s (%s) to %s",
                queue_item.name,
                queue_item.uri,
                queue.display_name,
            )
        elif (
            bytes_sent > 0
            and queue_item.streamdetails
            and queue_item.streamdetails.seconds_streamed
        ):
            # cache the actual encoded bytes-per-second for this URI + output format
            # so future content_length estimates are near-exact
            self.mass.create_task(
                store_content_length_in_cache(
                    self.mass,
                    queue_item.uri,
                    output_format,
                    bytes_sent,
                    queue_item.streamdetails.seconds_streamed,
                )
            )
        return resp

    async def serve_queue_flow_stream(self, request: web.Request) -> web.StreamResponse:  # noqa: PLR0915
        """Stream Queue Flow audio to player."""
        self._log_request(request)
        queue_id = request.match_info["queue_id"]
        player_id = request.match_info["player_id"]
        if not (queue := self.mass.player_queues.get(queue_id)):
            raise web.HTTPNotFound(reason=f"Unknown Queue: {queue_id}")
        session_id = request.match_info["session_id"]
        queue_data = self.mass.player_queues.queue_data(queue_id)
        if queue_data.session_id is None or session_id != queue_data.session_id:
            raise web.HTTPNotFound(reason=f"Unknown (or invalid) session: {session_id}")
        if not (player := self.mass.players.get_player(player_id)):
            raise web.HTTPNotFound(reason=f"Unknown Player: {player_id}")
        start_queue_item_id = request.match_info["queue_item_id"]
        start_queue_item = self.mass.player_queues.get_item(queue_id, start_queue_item_id)
        if not start_queue_item:
            raise web.HTTPNotFound(reason=f"Unknown Queue item: {start_queue_item_id}")

        # select the highest possible PCM settings for this player
        flow_pcm_format = await self.audio.select_flow_format(player)

        # work out output format/details
        output_format = await self.audio.get_output_format(
            output_format_str=request.match_info["fmt"],
            player=player,
            content_sample_rate=flow_pcm_format.sample_rate,
            content_bit_depth=flow_pcm_format.bit_depth,
        )
        # work out ICY metadata support
        icy_preference = self.mass.config.get_raw_player_config_value(
            player_id,
            CONF_ENTRY_ENABLE_ICY_METADATA.key,
            CONF_ENTRY_ENABLE_ICY_METADATA.default_value,
        )
        enable_icy = request.headers.get("Icy-MetaData", "") == "1" and icy_preference != "disabled"
        icy_meta_interval = 256000 if icy_preference == "full" else 16384

        # prepare request, add some DLNA/UPNP compatible headers.
        # icy-name (in DEFAULT_STREAM_HEADERS) is always present so players have a
        # readable stream name; the rest of the ICY/shoutcast metadata headers are
        # only advertised when the client actually requested ICY metadata, rather
        # than on every flow response.
        headers = {
            **DEFAULT_STREAM_HEADERS,
            **ICY_HEADERS,
            "contentFeatures.dlna.org": "DLNA.ORG_OP=01;DLNA.ORG_FLAGS=01700000000000000000000000000000",  # noqa: E501
            "Accept-Ranges": "none",
            "Content-Type": get_mime_type(output_format.output_format_str),
        }
        if enable_icy:
            headers["icy-metaint"] = str(icy_meta_interval)

        resp = web.StreamResponse(status=200, reason="OK", headers=headers)
        http_profile = player.get_config_value(CONF_HTTP_PROFILE, "default")
        if http_profile == "forced_content_length":
            # just set an insane high content length to make sure the player keeps playing
            resp.content_length = calculate_content_length(output_format, 12 * 3600)
        elif http_profile == "chunked":
            resp.enable_chunked_encoding()

        await resp.prepare(request)

        # return early if this is not a GET request
        if request.method != "GET":
            return resp

        self._update_audio_processing_context(
            queue=queue,
            queue_item=start_queue_item,
            pcm_format=flow_pcm_format,
            crossfade_mode=crossfade_mode,
            overlay_enabled=overlay_active(queue),
            session_id=session_id,
        )
        output_plan = self.audio.get_player_output_plan(
            player.player_id,
            flow_pcm_format,
            output_format,
            shared_player_ids=player.state.group_members,
            queue_id=queue_id,
            session_id=session_id,
        )

        # all checks passed, start streaming!
        # this final ffmpeg process in the chain will convert the raw, lossless PCM audio into
        # the desired output format for the player including any player specific filter params
        # such as channels mixing, DSP, resampling and, only if needed, encoding to lossy formats
        self.logger.debug("Start serving Queue flow audio stream for %s", queue.display_name)

        # Mark this player as actively streaming so audio analysis yields CPU to playback
        # for the duration of the flow stream (see audio_analysis.playback_active).
        self._active_output_streams += 1
        flow_stream = self.audio.get_queue_flow_stream(
            queue=queue,
            start_queue_item=start_queue_item,
            pcm_format=flow_pcm_format,
            session_id=session_id,
            protocol_player=player,
        )
        if overlay_active(queue):
            flow_stream = self.audio.get_overlay_mixed_stream(queue, flow_stream, flow_pcm_format)
        audio_bytes = get_ffmpeg_stream(
            audio_input=flow_stream,
            input_format=flow_pcm_format,
            output_format=output_format,
            filter_params=output_plan.filter_params,
            # we need to slowly feed the music to avoid the player stopping and later
            # restarting (or completely failing) the audio stream by keeping the buffer short.
            # this is reported to be an issue especially with Chromecast players.
            # see for example: https://github.com/music-assistant/support/issues/3717
            # allow buffer ahead of 6 seconds and read rest in realtime
            extra_input_args=["-readrate", "1.0", "-readrate_initial_burst", "6"],
            chunk_size=icy_meta_interval if enable_icy else calculate_content_length(output_format),
        ):
            try:
                await resp.write(chunk)
            except BrokenPipeError, ConnectionResetError, ConnectionError:
                # race condition
                break

                    if not enable_icy:
                        continue

            # if icy metadata is enabled, send the icy metadata after the chunk
            if (
                # use current item here and not buffered item, otherwise
                # the icy metadata will be too much ahead
                (current_item := queue.current_item)
                and current_item.streamdetails
                and current_item.streamdetails.stream_title
            ):
                try:
                    await resp.write(chunk)
                except BrokenPipeError, ConnectionResetError, ConnectionError:
                    # race condition
                    break

                if not enable_icy:
                    continue

                # if icy metadata is enabled, send the icy metadata after the chunk
                if (
                    # use current item here and not buffered item, otherwise
                    # the icy metadata will be too much ahead
                    (current_item := queue.current_item)
                    and current_item.streamdetails
                    and current_item.streamdetails.stream_title
                ):
                    title = current_item.streamdetails.stream_title
                elif queue and current_item and current_item.name:
                    title = current_item.name
                else:
                    title = "Music Assistant"
                metadata = f"StreamTitle='{title}';".encode()
                if icy_preference == "full" and current_item and current_item.image:
                    metadata += f"StreamURL='{current_item.image.path}'".encode()
                while len(metadata) % 16 != 0:
                    metadata += b"\x00"
                length = len(metadata)
                length_b = chr(int(length / 16)).encode()
                await resp.write(length_b + metadata)
        finally:
            self._active_output_streams -= 1

        return resp

    async def serve_command_request(self, request: web.Request) -> web.FileResponse:
        """Handle special 'command' request for a player."""
        self._log_request(request)
        queue_id = request.match_info["queue_id"]
        session_id = request.match_info["session_id"]
        queue_data = self.mass.player_queues.queue_data_or_none(queue_id)
        if queue_data is None or queue_data.session_id != session_id:
            raise web.HTTPNotFound(reason=f"Unknown (or invalid) session: {session_id}")
        command = request.match_info["command"]
        if command == "next":
            self.mass.create_task(self.mass.player_queues.next(queue_id))
        return web.FileResponse(SILENCE_FILE, headers={"icy-name": "Music Assistant"})

    async def serve_announcement_stream(self, request: web.Request) -> web.StreamResponse:
        """Stream announcement audio to a player."""
        self._log_request(request)
        player_id = request.match_info["player_id"]
        if not (player := self.mass.players.get_player(player_id)):
            raise web.HTTPNotFound(reason=f"Unknown Player: {player_id}")
        if not (announce_data := self.announcement_renderer.get_for_player(player_id)):
            raise web.HTTPNotFound(reason=f"No pending announcements for Player: {player_id}")

        # work out output format/details
        fmt = request.match_info["fmt"]
        audio_format = AudioFormat(content_type=ContentType.try_parse(fmt))

        http_profile = self._get_announcement_http_profile(player_id, announce_data)

        # return early if this is not a GET request:
        # players often probe the url with a HEAD request before fetching it and
        # rendering the announcement for such a probe would run the entire (costly)
        # TTS/ffmpeg chain twice for a single announcement.
        if request.method != "GET":
            resp = web.StreamResponse(status=200, reason="OK", headers=DEFAULT_STREAM_HEADERS)
            resp.content_type = get_mime_type(audio_format.output_format_str)
            if http_profile == "chunked":
                resp.enable_chunked_encoding()
            await resp.prepare(request)
            return resp

        if http_profile == "forced_content_length":
            # given the fact that an announcement is just a short audio clip,
            # just send it over completely at once so we have a fixed content length
            data = bytearray()
            announcement_stream = self.get_announcement_stream(announce_data, audio_format)
            # aclosing guarantees the stream (and thus the ffmpeg process chain behind
            # it) is torn down immediately when the request is cancelled, instead of
            # lingering until garbage collection finalizes the abandoned generator.
            async with aclosing(announcement_stream):
                async for chunk in announcement_stream:
                    data += chunk
            return web.Response(
                body=bytes(data),
                content_type=get_mime_type(audio_format.output_format_str),
                headers=DEFAULT_STREAM_HEADERS,
            )

        resp = web.StreamResponse(status=200, reason="OK", headers=DEFAULT_STREAM_HEADERS)
        resp.content_type = get_mime_type(audio_format.output_format_str)
        if http_profile == "chunked":
            resp.enable_chunked_encoding()

        await resp.prepare(request)

        # all checks passed, start streaming!
        self.logger.debug(
            "Start serving audio stream for Announcement %s to %s",
            announce_data["announcement_url"],
            player.display_name,
        )
        async for chunk in self.get_announcement_stream(
            announcement_url=announce_data["announcement_url"],
            output_format=audio_format,
            pre_announce=announce_data["pre_announce"],
            pre_announce_url=announce_data["pre_announce_url"],
        ):
            try:
                await resp.write(chunk)
            except BrokenPipeError, ConnectionResetError:
                break

        self.logger.debug(
            "Finished serving audio stream for Announcement %s to %s",
            announce_data["announcement_url"],
            player.display_name,
        )

        return resp

    async def serve_plugin_source_stream(self, request: web.Request) -> web.StreamResponse:
        """Stream PluginSource audio to a player."""
        self._log_request(request)
        plugin_source_id = request.match_info["plugin_source"]
        provider = cast("PluginProvider", self.mass.get_provider(plugin_source_id))
        if not provider:
            raise ProviderUnavailableError(f"Unknown PluginSource: {plugin_source_id}")
        # work out output format/details
        player_id = request.match_info["player_id"]
        player = self.mass.players.get_player(player_id)
        if not player:
            raise web.HTTPNotFound(reason=f"Unknown Player: {player_id}")
        plugin_source = provider.get_source()
        output_format = await self.audio.get_output_format(
            output_format_str=request.match_info["fmt"],
            player=player,
            content_sample_rate=plugin_source.audio_format.sample_rate,
            content_bit_depth=plugin_source.audio_format.bit_depth,
        )
        headers = {
            **DEFAULT_STREAM_HEADERS,
            "contentFeatures.dlna.org": "DLNA.ORG_OP=01;DLNA.ORG_FLAGS=01700000000000000000000000000000",  # noqa: E501
            "icy-name": plugin_source.name,
            "Accept-Ranges": "none",
            "Content-Type": get_mime_type(output_format.output_format_str),
        }

        resp = web.StreamResponse(status=200, reason="OK", headers=headers)
        resp.content_type = get_mime_type(output_format.output_format_str)
        http_profile = await self.mass.config.get_player_config_value(
            player_id, CONF_HTTP_PROFILE, default="default", return_type=str
        )
        if http_profile == "forced_content_length":
            # just set an insanely high content length to make sure the player keeps playing
            resp.content_length = calculate_content_length(output_format, 12 * 3600)
        elif http_profile == "chunked":
            resp.enable_chunked_encoding()

        await resp.prepare(request)

        # return early if this is not a GET request
        if request.method != "GET":
            return resp

        # all checks passed, start streaming!
        if not plugin_source.audio_format:
            raise InvalidDataError(f"No audio format for plugin source {plugin_source_id}")
        async for chunk in self.get_plugin_source_stream(
            plugin_source_id=plugin_source_id,
            output_format=output_format,
            player_id=player_id,
            player_filter_params=self.audio.get_player_filter_params(
                player_id, plugin_source.audio_format, output_format
            ),
        ):
            try:
                await resp.write(chunk)
            except (BrokenPipeError, ConnectionResetError, ConnectionError):
                break
        return resp

    def get_command_url(self, player_or_queue_id: str, command: str) -> str:
        """Get the url for the special command stream."""
        return f"{self.base_url}/command/{player_or_queue_id}/{command}.mp3"

    def get_announcement_url(
        self,
        player_id: str,
        content_type: ContentType = ContentType.MP3,
    ) -> str:
        """
        Get the url that serves the announcement registered for the given player.

        :param player_id: The player the announcement is played on.
        :param content_type: The format to serve the announcement in.
        """
        # use stream server to host announcement on local network
        # this ensures playback on all players, including ones that do not
        # like https hosts and it also offers the pre-announce 'bell'
        return f"{self.base_url}/announcement/{player_id}.{content_type.value}"

    def get_stream(
        self,
        media: PlayerMedia,
        pcm_format: AudioFormat,
        player_id: str | None = None,
        force_flow_mode: bool = False,
        use_flow_stream_buffering: bool = False,
    ) -> AsyncGenerator[bytes]:
        """
        Get a stream of the given media as raw PCM audio.

        This is used as helper for player providers that can consume the raw PCM
        audio stream directly (e.g. AirPlay) and not rely on HTTP transport.

        :param media: The PlayerMedia to stream.
        :param pcm_format: The desired output PCM format.
        :param player_id: The player ID requesting the stream. Used to determine
            if flow mode should be used based on the player's capabilities.
        :param force_flow_mode: Force flow mode regardless of player capabilities.
            Used for multi-client streaming scenarios that require continuous streams.
        :param use_flow_stream_buffering: Buffer the flow stream to provide headroom
            during smart fades transitions. Use for consumers that read directly
            (e.g. AirPlay, Snapcast) and can't tolerate stalls.
        """
        # select audio source
        if media.media_type == MediaType.ANNOUNCEMENT:
            # special case: stream announcement
            assert media.custom_data
            return self.get_announcement_stream(
                media.custom_data["announcement_url"],
                output_format=pcm_format,
                pre_announce=media.custom_data["pre_announce"],
                pre_announce_url=media.custom_data["pre_announce_url"],
            )
        if media.media_type == MediaType.PLUGIN_SOURCE:
            # special case: plugin source stream
            assert media.custom_data
            return self.get_plugin_source_stream(
                plugin_source_id=media.custom_data["source_id"],
                output_format=pcm_format,
                # need to pass player_id from the PlayerMedia object
                # because this could have been a group
                player_id=media.custom_data["player_id"],
            )
        if (
            media.source_id
            and media.source_id.startswith(UGP_PREFIX)
            and media.uri
            and "/ugp/" in media.uri
        ):
            # special case: member player accessing UGP stream
            # Check URI to distinguish from the UGP accessing its own stream
            ugp_player = cast("UniversalGroupPlayer", self.mass.players.get_player(media.source_id))
            ugp_stream = ugp_player.stream
            assert ugp_stream is not None  # for type checker
            if ugp_stream.base_pcm_format == pcm_format:
                # no conversion needed
                return ugp_stream.subscribe_raw()
            return ugp_stream.get_stream(output_format=pcm_format)
        if media.source_id and media.queue_item_id:
            # Queue stream request - determine flow_mode based on player capabilities
            # or force it if explicitly requested (e.g., for multi-client streaming)
            protocol_player = self.mass.players.get_player(player_id) if player_id else None
            queue_id = media.source_id
            queue = self.mass.player_queues.get(queue_id)
            queue_session_id = media.queue_session_id
            crossfade_needs_flow_mode = (
                # if the player(queue) has crossfade enabled but the player(protocol)
                # does not support gapless playback, we need to enforce flow mode
                queue_id
                and (queue_player := self.mass.players.get_player(queue_id))
                and queue_player.config.get_value(CONF_SMART_FADES_MODE) != SmartFadesMode.DISABLED
                and protocol_player
                and not protocol_player.supports_gapless
            )
            # the audio overlay is mixed into the queue's continuous (flow) stream;
            # per-item requests would restart the overlay at every track boundary
            overlay_needs_flow_mode = queue is not None and overlay_active(queue)
            flow_mode = (
                force_flow_mode
                or (protocol_player is not None and protocol_player.flow_mode)
                or crossfade_needs_flow_mode
                or overlay_needs_flow_mode
            )
            if media.media_type == MediaType.RADIO:
                # flow_mode for radio is pointless
                flow_mode = False
            if flow_mode:
                # flow stream request
                assert queue
                start_queue_item = self.mass.player_queues.get_item(
                    media.source_id, media.queue_item_id
                )
                assert start_queue_item
                flow_stream = self.audio.get_queue_flow_stream(
                    queue=queue,
                    start_queue_item=start_queue_item,
                    pcm_format=pcm_format,
                    protocol_player=protocol_player,
                )
                self._update_audio_processing_context(
                    queue=queue,
                    queue_item=start_queue_item,
                    pcm_format=pcm_format,
                    crossfade_mode=crossfade_mode,
                    overlay_enabled=overlay_active(queue),
                    session_id=queue_session_id,
                )
                flow_stream = self.audio.get_queue_flow_stream(
                    queue=queue,
                    start_queue_item=start_queue_item,
                    pcm_format=pcm_format,
                    session_id=queue_session_id,
                    protocol_player=protocol_player,
                )
                if overlay_active(queue):
                    flow_stream = self.audio.get_overlay_mixed_stream(
                        queue, flow_stream, pcm_format
                    )
                if use_flow_stream_buffering:
                    return buffered(flow_stream, buffer_size=30, min_buffer_before_yield=1)
                return flow_stream
            # single item stream (e.g. radio or non-flow mode)
            queue_item = self.mass.player_queues.get_item(media.source_id, media.queue_item_id)
            assert queue_item
            return self.audio.get_queue_item_stream(
                queue_item=queue_item,
                pcm_format=pcm_format,
                seek_position=(
                    int(queue_item.streamdetails.seek_position) if queue_item.streamdetails else 0
                ),
                playback_speed=cast(
                    "float", queue_item.extra_attributes.get("playback_speed", 1.0)
                ),
                session_id=queue_session_id,
            )
        # assume url or some other direct path
        # NOTE: this will fail if its an uri not playable by ffmpeg
        return get_ffmpeg_stream(
            audio_input=media.uri,
            input_format=AudioFormat(content_type=ContentType.try_parse(media.uri)),
            output_format=pcm_format,
        )

    async def get_preview_stream(
        self,
        provider_instance_id_or_domain: str,
        item_id: str,
        media_type: MediaType = MediaType.TRACK,
    ) -> AsyncGenerator[bytes]:
        """Create a 30 seconds preview audioclip for the given media item."""
        if not (music_prov := self.mass.get_provider(provider_instance_id_or_domain)):
            raise ProviderUnavailableError
        if TYPE_CHECKING:
            assert isinstance(music_prov, MusicProvider)

        if not await music_prov.get_item(media_type, item_id):
            msg = f"Item {item_id} not found in provider {provider_instance_id_or_domain}"
            raise InvalidDataError(msg)

        streamdetails = await music_prov.get_stream_details(item_id, media_type)
        pcm_format = AudioFormat(
            content_type=ContentType.from_bit_depth(streamdetails.audio_format.bit_depth),
            sample_rate=streamdetails.audio_format.sample_rate,
            bit_depth=streamdetails.audio_format.bit_depth,
            channels=streamdetails.audio_format.channels,
        )
        async for chunk in get_ffmpeg_stream(
            audio_input=self.audio.get_media_stream(
                streamdetails=streamdetails, pcm_format=pcm_format
            ),
            input_format=pcm_format,
            output_format=AudioFormat(content_type=ContentType.AAC),
            extra_input_args=["-t", "30"],
        ):
            yield chunk

    async def get_announcement_stream(
        self, announce_data: AnnounceData, output_format: AudioFormat
    ) -> AsyncGenerator[bytes]:
        """
        Get the audio of an announcement (pre-announce chime + announcement).

        Any number of consumers may stream the same announcement at once; its source is
        fetched and decoded only once. The audio stays available while the stream is
        held open.

        async def fetch_announcement() -> None:
            fmt = announcement_url.rsplit(".")[-1]
            try:
                async for chunk in get_ffmpeg_stream(
                    audio_input=announcement_url,
                    input_format=AudioFormat(content_type=ContentType.try_parse(fmt)),
                    output_format=pcm_format,
                    chunk_size=calculate_content_length(pcm_format, 1),
                ):
                    await announcement_data.put(chunk)
            except AudioError as err:
                self.logger.warning(
                    "Failed to fetch announcement audio from %s: %s", announcement_url, err
                )
            finally:
                await announcement_data.put(None)  # always signal end of stream

        self.mass.create_task(fetch_announcement())

        async def _announcement_stream() -> AsyncGenerator[bytes]:
            """Generate the PCM audio stream for the announcement + optional pre-announce."""
            if pre_announce:
                async for chunk in get_ffmpeg_stream(
                    audio_input=pre_announce_url,
                    input_format=AudioFormat(content_type=ContentType.try_parse(pre_announce_url)),
                    output_format=pcm_format,
                    chunk_size=calculate_content_length(pcm_format, 1),
                ):
                    yield chunk
        finally:
            await self.announcement_renderer.release(render)

    async def get_announcement_duration(
        self, announcement: PlayerMedia, timeout: float = DEFAULT_RENDER_TIMEOUT
    ) -> int | None:
        """
        Get the exact duration (in seconds) of an announcement, once it finished rendering.

        Waits for the audio to be rendered in full, so call this while the announcement
        plays rather than before handing it to a player. Returns None when the length can
        not be determined, e.g. the announcement is no longer playing or its source did
        not deliver in time.

        :param announcement: The announcement to return the duration for.
        :param timeout: Maximum time to wait for the audio to finish rendering.
        """
        if announcement.duration:
            return announcement.duration
        if not announcement.custom_data:
            return None
        render = self.announcement_renderer.get(cast("AnnounceData", announcement.custom_data))
        if render is None:
            return None
        duration = await render.wait_finished(timeout)
        return ceil(duration) if duration else None

    async def get_plugin_source_stream(
        self,
        plugin_source_id: str,
        output_format: AudioFormat,
        player_id: str,
        player_filter_params: list[str] | None = None,
    ) -> AsyncGenerator[bytes, None]:
        """Get the special plugin source stream."""
        plugin_prov = cast("PluginProvider", self.mass.get_provider(plugin_source_id))
        if not plugin_prov:
            raise ProviderUnavailableError(f"Unknown PluginSource: {plugin_source_id}")

        plugin_source = plugin_prov.get_source()
        self.logger.debug(
            "Start streaming PluginSource %s to %s using output format %s",
            plugin_source_id,
            player_id,
            output_format,
        )
        # this should already be set by the player controller, but just to be sure
        plugin_source.in_use_by = player_id

        try:
            async for chunk in get_ffmpeg_stream(
                audio_input=cast(
                    "str | AsyncGenerator[bytes, None]",
                    plugin_prov.get_audio_stream(player_id)
                    if plugin_source.stream_type == StreamType.CUSTOM
                    else plugin_source.path,
                ),
                input_format=plugin_source.audio_format,
                output_format=output_format,
                filter_params=player_filter_params,
                extra_input_args=["-y", "-re"],
            ):
                if plugin_source.in_use_by != player_id:
                    # another player took over or the stream ended, stop streaming
                    break
                yield chunk
        finally:
            self.logger.debug(
                "Finished streaming PluginSource %s to %s", plugin_source_id, player_id
            )
            await asyncio.sleep(1)  # prevent race conditions when selecting source
            if plugin_source.in_use_by == player_id:
                # release control
                plugin_source.in_use_by = None

    def _update_audio_processing_context(
        self,
        queue: PlayerQueue,
        queue_item: QueueItem,
        pcm_format: AudioFormat,
        crossfade_mode: CrossfadeMode,
        overlay_enabled: bool,
        session_id: str | None = None,
    ) -> None:
        """
        Store the shared processing context selected for a queue item.

        :param queue: Active player queue.
        :param queue_item: Queue item being prepared.
        :param pcm_format: Shared PCM format leaving queue processing.
        :param crossfade_mode: Effective crossfade mode for the item.
        :param overlay_enabled: Whether an overlay is mixed into this stream.
        :param session_id: Queue session that owns processing-detail updates.
        """
        if queue_item.streamdetails is None:
            return
        queue_data = self.mass.player_queues.queue_data_or_none(queue.queue_id)
        if (
            queue_data is None
            or (processing_session_id := session_id or queue_data.session_id) is None
            or queue_data.session_id != processing_session_id
        ):
            return
        self.audio_processing.start_session(queue.queue_id, processing_session_id)
        self.audio_processing.update_item_context(
            queue_id=queue.queue_id,
            session_id=processing_session_id,
            queue_item_id=queue_item.queue_item_id,
            queue_processing=AudioQueueProcessing(
                pcm_format=pcm_format,
                playback_speed=cast(
                    "float",
                    queue_item.extra_attributes.get("playback_speed", 1.0),
                ),
                crossfade_mode=crossfade_mode,
                overlay_active=overlay_enabled,
            ),
            alters_audio=queue_item.streamdetails.fade_in,
        )

    def _get_announcement_http_profile(self, player_id: str, announce_data: AnnounceData) -> str:
        """
        Resolve the http profile for serving an announcement stream.

        Announcement urls are registered under the visible player's id, but the
        stream may be fetched by a linked protocol player; the profile must come
        from the player that actually performs the fetch.
        """
        announce_player = None
        if announce_player_id := announce_data.get("announce_player_id"):
            announce_player = self.mass.players.get_player(announce_player_id)
        if announce_player is None:
            announce_player = self.mass.players.get_player(player_id)
        if announce_player is None:
            return "default"
        return announce_player.get_output_config_value(CONF_HTTP_PROFILE, "default")

    async def _finish_flow_stream(
        self, resp: web.StreamResponse, queue_id: str, session_id: str
    ) -> None:
        """
        Close a fully served flow stream, giving the player time to drain when it ends the queue.

        :param resp: The flow stream response, already fully written.
        :param queue_id: Id of the queue the flow stream belongs to.
        :param session_id: Stream session this response was opened for.
        """
        if self.mass.player_queues.flow_queue_exhausted(queue_id, session_id):
            # the player is still holding a few seconds of audio it has not rendered yet
            # and drops that as soon as the stream ends, so let it play out first.
            # a flow that ends to be restarted right away gets no such grace: there the
            # player should go idle as soon as possible so the next stream can start.
            self.logger.debug(
                "Flow stream for queue %s reached the end of the queue - holding the "
                "connection open for %ss so the player can play out its buffer",
                queue_id,
                FLOW_STREAM_LEAD_OUT_SECONDS,
            )
            await asyncio.sleep(FLOW_STREAM_LEAD_OUT_SECONDS)
        # aiohttp derives keep-alive from the request, so the 'Connection: close' we
        # advertise is relayed to the player but never applied to the response itself.
        # Without this the player is left waiting on a stream that already ended.
        resp.force_close()

    def _log_request(self, request: web.Request) -> None:
        """Log request."""
        if self.logger.isEnabledFor(VERBOSE_LOG_LEVEL):
            self.logger.log(
                VERBOSE_LOG_LEVEL,
                "Got %s request to %s from %s\nheaders: %s\n",
                request.method,
                request.path,
                request.remote,
                redact_sensitive_headers(request.headers),
            )
        else:
            self.logger.debug(
                "Got %s request to %s from %s (HTTP/%s.%s, connection: %s)",
                request.method,
                request.path,
                request.remote,
                request.version.major,
                request.version.minor,
                request.headers.get("Connection", "-"),
            )

    async def _reload_network_dependent_providers(self) -> None:
        """Reload the providers that captured the streamserver network, if it changed."""
        previous = self._network_fingerprint
        current = (
            self._bind_ip,
            str(self.publish_ip),
            cast("int", self.publish_port),
            tuple(self._publish_addresses),
        )
        if previous is None or previous == current:
            self._network_fingerprint = current
            return
        # these providers bind or advertise the network while they load, so a plain
        # reload is what moves them over - they share no lighter rebind path
        instance_ids = [
            prov.instance_id
            for prov in self.mass.providers
            if prov.reload_on_streams_network_change
        ]
        for instance_id in instance_ids:
            try:
                config = await self.mass.config.get_provider_config(instance_id)
                self.logger.info(
                    "Streamserver network changed, reloading provider %s",
                    config.name or config.domain,
                )
                await self.mass.load_provider_config(config)
            except Exception as err:
                self.logger.warning(
                    "Error reloading provider %s: %s",
                    instance_id,
                    str(err) or err.__class__.__name__,
                    exc_info=err,
                )
        # only mark the new network as applied once the loop completed, so a run cut short
        # by a second config change runs again on the next reload
        self._network_fingerprint = current

    def _setup_smart_fades_logger(self, config: CoreConfig) -> None:
        """Set up smart fades logger level."""
        log_level = str(config.get_value(CONF_SMART_FADES_LOG_LEVEL))
        if log_level == "GLOBAL":
            self.smart_fades_analyzer.logger.setLevel(self.logger.level)
            self.audio.smart_fades_mixer.logger.setLevel(self.logger.level)
        else:
            self.smart_fades_analyzer.logger.setLevel(log_level)
            self.audio.smart_fades_mixer.logger.setLevel(log_level)

    def _resolve_publish_state(self, bind_ip: str, publish_candidates: tuple[str, ...]) -> None:
        """
        Resolve the addresses and base URL to advertise for the given bind address.

        Reads ``self.publish_port``, so set that first.

        :param bind_ip: Address the streamserver binds to (a wildcard means all interfaces).
        :param publish_candidates: Host addresses reachable from the local network, ranked.
        """
        self._bind_ip = bind_ip
        self._publish_addresses = _get_publish_addresses(
            bind_ip, self._configured_publish_ip, publish_candidates
        )
        # the single address players are handed, taken from the top of the ranked list
        self.publish_ip = self._publish_addresses[0]
        self._base_url = f"http://{format_ip_for_url(self.publish_ip)}:{self.publish_port}"


def _same_ip_family(ip: str, other_ip: str) -> bool:
    """Return whether two addresses belong to the same IP family."""
    return (":" in ip) == (":" in other_ip)
