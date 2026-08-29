"""AirPlay Player implementations."""

from __future__ import annotations

import asyncio
import contextlib
import ipaddress
import time
from typing import TYPE_CHECKING, cast

from music_assistant_models.config_entries import ConfigEntry, ConfigValueOption, ConfigValueType
from music_assistant_models.enums import ConfigEntryType, PlaybackState, PlayerFeature, PlayerType

from music_assistant.constants import (
    CONF_ENTRY_DEPRECATED_EQ_BASS,
    CONF_ENTRY_DEPRECATED_EQ_MID,
    CONF_ENTRY_DEPRECATED_EQ_TREBLE,
    CONF_ENTRY_FLOW_MODE_ENFORCED,
    CONF_ENTRY_OUTPUT_CODEC_HIDDEN,
    CONF_ENTRY_SYNC_ADJUST,
    create_sample_rates_config_entry,
)
from music_assistant_models.errors import PlayerCommandFailed
from music_assistant_models.media_items import AudioFormat

from music_assistant.constants import CONF_ENTRY_SYNC_ADJUST, create_sample_rates_config_entry
from music_assistant.helpers.util import get_primary_ip_address_from_zeroconf, is_valid_mac_address
from music_assistant.models.player import DeviceInfo, Player, PlayerMedia
from music_assistant.models.setup_flow import AbortFlow

from . import announce
from .constants import (
    AIRPLAY_DISCOVERY_TYPE,
    AIRPLAY_HIRES_AUDIO_FORMATS,
    AIRPLAY_HIRES_SAMPLE_RATES,
    AIRPLAY_PCM_FORMAT,
    AIRPLAY_REJOIN_ATTEMPT_DELAYS,
    AIRPLAY_VOLUME_ECHO_GRACE_S,
    BASE_PLAYER_FEATURES,
    CONF_AIRPLAY_CREDENTIALS,
    CONF_AIRPLAY_PROTOCOL,
    CONF_ALAC_ENCODE,
    CONF_ENCRYPTION,
    CONF_ENTRY_SYNC_ADJUST_AIRPLAY,
    CONF_IGNORE_VOLUME,
    CONF_PAIR_NOW,
    CONF_PAIRING_PASSWORD,
    CONF_PAIRING_PIN,
    CONF_PASSWORD,
    CONF_PASSWORD_INVALID,
    CONF_RAOP_CREDENTIALS,
    FALLBACK_VOLUME,
    LEGACY_PAIRING_BIT,
    PAIRING_PIN_FORMAT,
    PASSWORD_BIT,
    PIN_REQUIRED,
    RAOP_DISCOVERY_TYPE,
    STREAMING_MODE_AP2_COMPAT,
    STREAMING_MODE_AP2_NTP,
    STREAMING_MODE_AP2_PTP,
    STREAMING_MODE_AUTO,
    STREAMING_MODE_RAOP,
    StreamingProtocol,
)
from .helpers import (
    default_buffer_depth,
    default_hires_enabled,
    get_decoded_property,
    is_apple_device,
    is_macos_device,
    parse_airplay_features,
    player_id_to_mac_address,
    supports_airplay2,
)
from .stream_session import AirPlayStreamSession

if TYPE_CHECKING:
    from zeroconf.asyncio import AsyncServiceInfo

    from music_assistant.models.setup_flow import SetupSession

    from .pairing import AirPlayPairing
    from .provider import AirPlayProvider
    from .stream import AirPlayStream

# Docker bridge subnet, sometimes wrongly advertised via mDNS by containerized devices.
_DOCKER_SUBNET = ipaddress.ip_network("172.16.0.0/12")


class AirPlayPlayer(Player):
    """Base implementation shared by all AirPlay players."""

    def __init__(
        self,
        provider: AirPlayProvider,
        player_id: str,
        raop_discovery_info: AsyncServiceInfo | None,
        airplay_discovery_info: AsyncServiceInfo | None,
        address: str,
        display_name: str,
        manufacturer: str,
        model: str,
        initial_volume: int = FALLBACK_VOLUME,
    ) -> None:
        """Initialize AirPlayPlayer."""
        self.raop_discovery_info = raop_discovery_info
        self.airplay_discovery_info = airplay_discovery_info
        # Audio formats the receiver advertises, learned from its /info response;
        # zero until that lands (or when the device publishes no format tables).
        self.advertised_audio_formats = 0
        self._attr_enabled_by_default = not is_macos_device(manufacturer, model)
        super().__init__(provider, player_id)
        self.address = address
        self.stream: AirPlayStream | None = None
        # Serializes the two paths that can put a cliairplay process on this
        # receiver (the native stream session and the Sendspin bridge), from the
        # moment either decides to displace what is published until it publishes
        # its own stream. Two processes on one receiver reset each other's RTSP
        # channel and both sessions die. Always taken INSIDE self._lock, never
        # around it: play_media holds self._lock across the whole session start,
        # which takes this lock for every member.
        self.stream_spawn_lock = asyncio.Lock()
        self.last_command_sent = 0.0
        self._volume_reports_ignored_until = 0.0
        self._lock = asyncio.Lock()
        self._transitioning = False  # Set during stream replacement to ignore stale DACP messages
        self._rejoin_task: asyncio.Task[None] | None = None
        # Set (static) player attributes
        self._attr_name = display_name
        self._attr_available = True
        mac_address = player_id_to_mac_address(player_id)
        self._attr_device_info = DeviceInfo(
            model=model,
            manufacturer=manufacturer,
            ip_address=address,
            mac_address=player_id_to_mac_address(player_id),
        )
        # Only add MAC address if it's valid (not 00:00:00:00:00:00)
        if is_valid_mac_address(mac_address):
            self._attr_device_info.add_identifier(IdentifierType.MAC_ADDRESS, mac_address)
        self._attr_device_info.add_identifier(IdentifierType.IP_ADDRESS, address)
        self._attr_device_info.add_identifier(IdentifierType.AIRPLAY_ID, player_id)
        self._attr_volume_level = initial_volume
        self._attr_can_group_with = {provider.instance_id}
        self._attr_enabled_by_default = not is_broken_airplay_model(manufacturer, model)
        self._attr_supported_sample_rates = [
            (AIRPLAY_PCM_FORMAT.sample_rate, AIRPLAY_PCM_FORMAT.bit_depth)
        ]

    @property
    def protocol(self) -> StreamingProtocol:
        """Get the streaming protocol to use/prefer for this player."""
        preferred_option = cast("int", self.config.get_value(CONF_AIRPLAY_PROTOCOL))
        return self._get_protocol_for_config_value(preferred_option)

    @property
    def available(self) -> bool:
        """Return if the player is currently available."""
        if self._requires_pairing():
            # check if we have credentials stored for the current protocol
            creds_key = self._get_credentials_key(self.protocol)
            if not self.config.get_value(creds_key):
                return False
        return super().available

    @property
    def corrected_elapsed_time(self) -> float:
        """Return the corrected elapsed time accounting for stream session restarts."""
        if not self.stream or not self.stream.session:
            return super().corrected_elapsed_time or 0.0
        session = self.stream.session
        elapsed = time.time() - session.start_time - session.total_pause_time
        if session.last_paused is not None:
            current_pause = time.time() - session.last_paused
            elapsed -= current_pause
        return max(0.0, elapsed)

    async def get_config_entries(
        self,
        action: str | None = None,
        values: dict[str, ConfigValueType] | None = None,
    ) -> list[ConfigEntry]:
        """Return all (provider/player specific) Config Entries for the given player (if any)."""
        # Pairing/credentials are no longer config entries: they are collected by the
        # interactive setup flow (run_setup_flow) and stored in the player's setup_data.
        base_entries: list[ConfigEntry] = []

        # Effective RAOP state from the current (stored) streaming mode, so the
        # RAOP-only entries show/hide consistently with it.
        is_raop = self.protocol == StreamingProtocol.RAOP

        # Streaming-mode escape hatch: a per-device pin of the protocol/timing
        # lane for receivers whose automatic route misbehaves. Only offered
        # when the device actually has a lane to choose (Apple receivers are
        # always native AirPlay 2 with PTP and get no entry).
        mode_options = self.streaming_mode_options
        if len(mode_options) > 1:
            base_entries.append(
                ConfigEntry(
                    key=CONF_STREAMING_MODE,
                    type=ConfigEntryType.STRING,
                    options=mode_options,
                    default_value=STREAMING_MODE_AUTO,
                    category="protocol_generic",
                    advanced=True,
                )
            )

        # 24-bit toggle, shown only when the device advertises 24-bit support
        # (per-device default: see default_hires_enabled). Hidden rather than
        # omitted when it does not: the formats are probed async after
        # registration, and an entry absent from the registration-time config
        # parse would drop the user's stored value until the next config save.
        base_entries.append(
            ConfigEntry(
                key=CONF_ENABLE_HIRES,
                type=ConfigEntryType.BOOLEAN,
                default_value=self._hires_default_enabled,
                hidden=not self.advertised_audio_formats & AIRPLAY_HIRES_AUDIO_FORMATS,
                category="protocol_generic",
                requires_reload=True,
            )
        )

        # Regular AirPlay config entries
        base_entries += [
            ConfigEntry(
                key=CONF_AIRPLAY_PROTOCOL,
                type=ConfigEntryType.INTEGER,
                required=False,
                label="AirPlay protocol version to use for streaming",
                description="AirPlay version 1 protocol uses RAOP.\n"
                "AirPlay version 2 is an extension of RAOP.\n"
                "Some newer devices do not fully support RAOP and "
                "will only work with AirPlay version 2, "
                "while older devices may only support RAOP.\n\n"
                "In most cases the default automatic selection will work fine.",
                category="airplay",
                options=[
                    ConfigValueOption("Automatically select", 0),
                    ConfigValueOption("Prefer AirPlay 1 (RAOP)", StreamingProtocol.RAOP.value),
                    ConfigValueOption("Prefer AirPlay 2", StreamingProtocol.AIRPLAY2.value),
                ],
                default_value=0,
            ),
            ConfigEntry(
                key=CONF_ENCRYPTION,
                type=ConfigEntryType.BOOLEAN,
                default_value=True,
                label="Enable encryption",
                description="Enable encrypted communication with the player, "
                "some (3rd party) players require this to be disabled.",
                depends_on=CONF_AIRPLAY_PROTOCOL,
                depends_on_value=StreamingProtocol.RAOP.value,
                hidden=self.protocol != StreamingProtocol.RAOP,
            ),
            ConfigEntry(
                key=CONF_ALAC_ENCODE,
                type=ConfigEntryType.BOOLEAN,
                default_value=True,
                label="Enable compression",
                description="Save some network bandwidth by sending the audio as "
                "(lossless) ALAC at the cost of a bit of CPU.",
                depends_on=CONF_AIRPLAY_PROTOCOL,
                depends_on_value=StreamingProtocol.RAOP.value,
                hidden=self.protocol != StreamingProtocol.RAOP,
            ),
            CONF_ENTRY_SYNC_ADJUST,
            ConfigEntry(
                key=CONF_PASSWORD,
                type=ConfigEntryType.SECURE_STRING,
                default_value=None,
                required=False,
                # Storage (and encryption) vehicle only: the device password is
                # entered through the setup flow, which is also what a wrong
                # password sends the user back to. A hidden entry keeps its stored
                # value across config saves (the frontend never submits it).
                hidden=True,
                category="protocol_generic",
                advanced=True,
            ),
            ConfigEntry(
                key=CONF_IGNORE_VOLUME,
                type=ConfigEntryType.BOOLEAN,
                default_value=False,
                label="Ignore volume reports sent by the device itself",
                description=(
                    "The AirPlay protocol allows devices to report their own volume "
                    "level. \n"
                    "For some devices this is not reliable and can cause unexpected "
                    "volume changes. \n"
                    "Enable this option to ignore these reports."
                ),
                category="protocol_generic",
                advanced=True,
            ),
            ConfigEntry(
                key=CONF_RAOP_LATENCY,
                type=ConfigEntryType.INTEGER,
                default_value=AIRPLAY_OUTPUT_BUFFER_DEFAULT_DURATION_MS,
                range=(
                    RAOP_OUTPUT_BUFFER_MIN_DURATION_MS,
                    RAOP_OUTPUT_BUFFER_MAX_DURATION_MS,
                ),
                label="Milliseconds of data to buffer",
                description=(
                    "The number of milliseconds of data to buffer\n"
                    "NOTE: This adds to the latency experienced for commencement "
                    "of playback. \n"
                    "Try increasing value if playback is unreliable."
                ),
                depends_on=CONF_AIRPLAY_PROTOCOL,
                depends_on_value=StreamingProtocol.RAOP.value,
                hidden=not is_raop,
                category="protocol_generic",
                advanced=True,
            ),
            # Receiver-queue depth presets. The range reaches past the standard
            # 2 s receiver buffer because that figure is only what the binary
            # assumes for a device that reports no window of its own, and the
            # deepest starving devices ask for more than the assumption. The
            # default comes from the device-family table, and Automatic resolves
            # through that same table at stream time, so selecting it never
            # downgrades an affected device.
            ConfigEntry(
                key=CONF_BUFFER_DEPTH,
                type=ConfigEntryType.INTEGER,
                default_value=AIRPLAY_SESSION_ESTABLISHMENT_LATENCY_DEFAULT_MS,
                range=(
                    AIRPLAY_OUTPUT_BUFFER_MIN_DURATION_MS,
                    AIRPLAY_OUTPUT_BUFFER_MAX_DURATION_MS,
                ),
                category="airplay",
                # TODO: remove depends_on when DACP support is added for AirPlay2
                depends_on=CONF_AIRPLAY_PROTOCOL,
                depends_on_value=StreamingProtocol.RAOP.value,
                hidden=self.protocol != StreamingProtocol.RAOP,
            ),
        ]

        if is_broken_airplay_model(self.device_info.manufacturer, self.device_info.model):
            base_entries.insert(-1, BROKEN_AIRPLAY_WARN)

        if effective_protocol == StreamingProtocol.AIRPLAY2:
            # Insert the warning right after the protocol choice entry
            for i, entry in enumerate(base_entries):
                if entry.key == CONF_AIRPLAY_PROTOCOL:
                    base_entries.insert(
                        i + 1,
                        ConfigEntry(
                            key="AIRPLAY2_SYNC_WARN",
                            type=ConfigEntryType.ALERT,
                            default_value=None,
                            required=False,
                            label="AirPlay 2 currently does not support audio synchronization. "
                            "Grouping/syncing with other players is not available. "
                            "Switch to AirPlay 1 (RAOP) if you need multi-room sync.",
                        ),
                    )
                    break

        return base_entries

    async def run_setup_flow(self, session: SetupSession) -> None:
        """
        Run the interactive setup flow for this AirPlay player (streaming pairing).

        :param session: The setup flow session used to interact with the user.
        """
        return bool(self._get_flags() & PASSWORD_BIT)

    def _get_credentials_key(self, protocol: StreamingProtocol) -> str:
        """Get the config key for credentials for given protocol."""
        if protocol == StreamingProtocol.RAOP:
            return CONF_RAOP_CREDENTIALS
        return CONF_AIRPLAY_CREDENTIALS

    def _get_protocol_for_config_value(self, config_option: int) -> StreamingProtocol:
        if config_option == StreamingProtocol.AIRPLAY2:
            return StreamingProtocol.AIRPLAY2
        if config_option == StreamingProtocol.RAOP:
            return StreamingProtocol.RAOP
        # automatic selection
        if self.airplay_discovery_info and is_airplay2_preferred_model(
            self.device_info.manufacturer, self.device_info.model
        ):
            return StreamingProtocol.AIRPLAY2
        # Fall back to AirPlay 2 if RAOP service was not discovered
        if not self.raop_discovery_info and self.airplay_discovery_info:
            return StreamingProtocol.AIRPLAY2
        return StreamingProtocol.RAOP

    def _get_credentials_key(self, protocol: StreamingProtocol) -> str:
        """Get the config key for credentials for given protocol."""
        if protocol == StreamingProtocol.RAOP:
            return CONF_RAOP_CREDENTIALS
        return CONF_AIRPLAY_CREDENTIALS

    def _get_protocol_for_config_value(self, config_option: int) -> StreamingProtocol:
        if config_option == StreamingProtocol.AIRPLAY2 and self.airplay_discovery_info:
            return StreamingProtocol.AIRPLAY2
        if config_option == StreamingProtocol.RAOP and self.raop_discovery_info:
            return StreamingProtocol.RAOP
        # automatic selection
        if self.airplay_discovery_info and is_airplay2_preferred_model(
            self.device_info.manufacturer, self.device_info.model
        ):
            return StreamingProtocol.AIRPLAY2
        return StreamingProtocol.RAOP

    def _get_pairing_config_entries(
        self, values: dict[str, ConfigValueType] | None
    ) -> list[ConfigEntry]:
        """
        Return pairing config entries for Apple TV and macOS devices.

        Uses native pairing for both AirPlay 2 (HAP) and RAOP protocols.
        """
        Return pairing config entries for Apple TV and macOS devices.

        Uses native pairing for both AirPlay 2 (HAP) and RAOP protocols.
        """
        self.logger.debug(f"_get_pairing_config_entries with values: {values}")
        entries: list[ConfigEntry] = []

        # Determine protocol name for UI
        conf_protocol: int = 0
        if values and (val := values.get(CONF_AIRPLAY_PROTOCOL)):
            conf_protocol = cast("int", val)
        else:
            conf_protocol = cast("int", self.config.get_value(CONF_AIRPLAY_PROTOCOL, 0) or 0)
        protocol = self._get_protocol_for_config_value(conf_protocol)
        protocol_name = "RAOP" if protocol == StreamingProtocol.RAOP else "AirPlay"
        protocol_key = (
            CONF_RAOP_CREDENTIALS
            if protocol == StreamingProtocol.RAOP
            else CONF_AIRPLAY_CREDENTIALS
        )
        has_creds_for_current_protocol = (
            values.get(protocol_key) if values else self.config.get_value(protocol_key)
        )

        if not has_creds_for_current_protocol:
            # If pairing was started, show PIN entry
            if self._active_pairing and self._active_pairing.is_pairing:
                entries.append(
                    ConfigEntry(
                        key=CONF_PAIRING_PIN,
                        type=ConfigEntryType.STRING,
                        label="Enter the 4-digit PIN shown on the device",
                        required=True,
                    )
                )
                entries.append(
                    ConfigEntry(
                        key=CONF_ACTION_FINISH_PAIRING,
                        type=ConfigEntryType.ACTION,
                        label=f"Complete {protocol_name} pairing with the PIN",
                        action=CONF_ACTION_FINISH_PAIRING,
                    )
            else:
                # Show pairing instructions and start button
                entries.append(
                    ConfigEntry(
                        key="pairing_instructions",
                        type=ConfigEntryType.LABEL,
                        label=(
                            f"This device requires {protocol_name} pairing before it can be used. "
                            "Click the button below to start the pairing process."
                        ),
                        category="protocol_generic",
                    )
                )
                entries.append(
                    ConfigEntry(
                        key=CONF_ACTION_START_PAIRING,
                        type=ConfigEntryType.ACTION,
                        label=f"Start {protocol_name} pairing",
                        action=CONF_ACTION_START_PAIRING,
                        category="protocol_generic",
                    )
                )
        else:
            self.logger.debug(f"Device is already paired for {protocol_name}, showing reset option")
            # Show paired status
            entries.append(
                ConfigEntry(
                    key="pairing_status",
                    type=ConfigEntryType.LABEL,
                    label=f"Device is paired ({protocol_name}) and ready to use.",
                )
            )
            # Add reset pairing button
            entries.append(
                ConfigEntry(
                    key=CONF_ACTION_RESET_PAIRING,
                    type=ConfigEntryType.ACTION,
                    label=f"Reset {protocol_name} pairing",
                    action=CONF_ACTION_RESET_PAIRING,
                )
            )

        # Store credentials (hidden from UI)
        for protocol in (StreamingProtocol.RAOP, StreamingProtocol.AIRPLAY2):
            conf_key = self._get_credentials_key(protocol)
            entries.append(
                ConfigEntry(
                    key=conf_key,
                    type=ConfigEntryType.SECURE_STRING,
                    label=conf_key,
                    default_value=None,
                    value=values.get(conf_key) if values else None,
                    required=False,
                    hidden=True,
                )
            )
        return entries

    async def _handle_pairing_action(
        self, action: str, values: dict[str, ConfigValueType] | None
    ) -> None:
        """
        Handle pairing actions.

        Uses native pairing for both AirPlay 2 (HAP) and RAOP protocols.
        Both produce credentials compatible with cliap2/cliraop respectively.
        """
        conf_protocol: int = 0
        if values and (val := values.get(CONF_AIRPLAY_PROTOCOL)):
            conf_protocol = cast("int", val)
        else:
            conf_protocol = cast("int", self.config.get_value(CONF_AIRPLAY_PROTOCOL, 0) or 0)
        protocol = self._get_protocol_for_config_value(conf_protocol)
        protocol_name = "RAOP" if protocol == StreamingProtocol.RAOP else "AirPlay"

        if action == CONF_ACTION_START_PAIRING:
            if self._active_pairing and self._active_pairing.is_pairing:
                self.logger.warning("Pairing process already in progress for %s", self.display_name)
                return

            self.logger.info("Starting %s pairing for %s", protocol_name, self.display_name)

            from .pairing import AirPlayPairing  # noqa: PLC0415

            # Determine port based on protocol
            # Note: For Apple devices, pairing always happens on the AirPlay port (7000)
            # even when streaming will use RAOP. The RAOP port (5000) is only for streaming.
            port: int | None = None
            if self.airplay_discovery_info:
                port = self.airplay_discovery_info.port or 7000
            elif self.raop_discovery_info:
                # Fallback for devices without AirPlay service
                port = self.raop_discovery_info.port or 5000
            # Get the DACP ID from the provider - must match what cliap2 uses
            provider = cast("AirPlayProvider", self.provider)
            device_id = provider.dacp_id

            self._active_pairing = AirPlayPairing(
                address=self.address,
                name=self.display_name,
                protocol=protocol,
                logger=self.logger,
                port=port,
                device_id=device_id,
            )
            await self._active_pairing.start_pairing()

        elif action == CONF_ACTION_FINISH_PAIRING:
            if not values:
                return

    async def _start_pairing(self, protocol: StreamingProtocol, protocol_name: str) -> None:
        """Begin a new pairing session for the given protocol."""
        self.logger.debug(f"_start_pairing for protocol: {protocol_name}")
        if self._active_pairing and self._active_pairing.is_pairing:
            self.logger.warning("Pairing process already in progress for %s", self.display_name)
            return

        self.logger.info("Starting %s pairing for %s", protocol_name, self.display_name)

        from .pairing import AirPlayPairing  # noqa: PLC0415

        # Determine port based on protocol
        # Note: For Apple devices, pairing always happens on the AirPlay port (7000)
        # even when streaming will use RAOP. The RAOP port (5000) is only for streaming.
        port: int | None = None
        if self.airplay_discovery_info:
            port = self.airplay_discovery_info.port or 7000
        elif self.raop_discovery_info:
            # Fallback for devices without AirPlay service
            port = self.raop_discovery_info.port or 5000
        # Get the DACP ID from the provider - must match what cliap2 uses
        provider = cast("AirPlayProvider", self.provider)
        device_id = provider.dacp_id

        self._active_pairing = AirPlayPairing(
            address=self.address,
            name=self.display_name,
            protocol=protocol,
            logger=self.logger,
            port=port,
            device_id=device_id,
        )
        await self._active_pairing.start_pairing_session()

        if self._requires_pin_pairing():
            await self._active_pairing.start_pin_pairing()

    async def _finish_pairing(
        self,
        values: dict[str, ConfigValueType] | None,
        protocol: StreamingProtocol,
        protocol_name: str,
    ) -> None:
        """Complete an in-progress pairing session.

        ``values`` may contain a PIN or a password supplied by the user when required.
        """
        self.logger.debug(f"_finish_pairing for protocol: {protocol_name} with values: {values}")
        if not values:
            return
        pin = None
        if self._requires_pin_pairing():
            pin = values.get(CONF_PAIRING_PIN)
            if not pin:
                self.logger.warning("No PIN provided for pairing")
                return

            if not self._active_pairing:
                self.logger.warning("No active pairing session for %s", self.display_name)
                return

            credentials = await self._active_pairing.finish_pairing(pin=str(pin))
            self._active_pairing = None

            # Store credentials with the protocol-specific key
            cred_key = self._get_credentials_key(protocol)
            values[cred_key] = credentials

            self.logger.info("Finished %s pairing for %s", protocol_name, self.display_name)

        elif action == CONF_ACTION_RESET_PAIRING:
            cred_key = self._get_credentials_key(protocol)
            self.logger.info("Resetting %s pairing for %s", protocol_name, self.display_name)
            if values is not None:
                values[cred_key] = None

    async def stop(self) -> None:
        """Send STOP command to player."""
        # an explicit stop (including power-off routed as stop) is user intent:
        # drop any pending automatic re-join
        self.cancel_group_rejoin()
        async with self._lock:
            if self.stream and self.stream.session:
                # forward stop to the entire stream session
                await self.stream.session.stop()
            elif cast("AirPlayProvider", self.provider).bridge_manager.stop_streaming(
                self.player_id
            ):
                # Sendspin bridge active: it tears the transport down straight
                # away and takes the player out of the Sendspin session
                pass
            elif self.stream and self.stream.running:
                # Fallback: stop protocol directly
                await self.stream.stop(force=True)
                self.stream = None
            self._attr_current_media = None
            self.update_state()

    async def play(self) -> None:
        """Handle PLAY (unpause) command on the player."""
        session = self.stream.session if self.stream and self.stream.running else None
        if self.group_members or self.synced_to or (session and session.parked):
            # Grouped pause parks the whole session (standby); unpausing one
            # member cannot restart the group in sync, and a parked member is
            # held with nothing being fed until a re-anchor - which ACTION=PLAY
            # does not carry, so it would report playback over silence. The park
            # outlives the group, so a player left alone by an ungroup is keyed
            # on the park itself, not on its membership. Resume via the queue
            # instead: play_media flushes and re-anchors every parked member at
            # one shared instant. The queue can belong to a linked native parent
            # (for example Sonos), so resolve it instead of using the AirPlay ID.
            active_queue = self.mass.players.get_active_queue(self)
            if active_queue is None:
                raise PlayerCommandFailed(
                    f"Cannot resume AirPlay player {self.display_name} without an active queue"
                )
            await self.mass.player_queues.resume(active_queue.queue_id, fade_in=False)
            return
        async with self._lock:
            if self.stream and self.stream.running:
                if await self.stream.send_cli_command("ACTION=PLAY"):
                    # Resuming re-anchors playout; the binary zeroes its own
                    # re-anchor total on resume, so drop the tracked shift to
                    # keep the server and binary baselines aligned.
                    self.stream.reset_reanchor_shift()

    async def pause(self) -> None:
        """Send PAUSE command to player."""
        if self.group_members or self.synced_to:
            # A broadcast pause cannot keep independent member processes
            # sample-aligned on resume. Instead the session is parked: every
            # member stalls but keeps its connection (and remote control), and
            # the queue's resume flushes and re-anchors over the live
            # connections — the same coordinated warm restart as seek/next.
            if (
                self.stream
                and self.stream.running
                and self.stream.session
                and await self.stream.session.standby()
            ):
                return
            # Some member no longer has a live connection: full stop and let
            # the queue controller resume from the saved position.
            self.logger.debug("Sync group cannot be parked, using STOP instead of PAUSE")
            await self.stop()
            return

        async with self._lock:
            if not self.stream or not self.stream.running:
                return
            await self.stream.send_cli_command("ACTION=PAUSE")

    async def play_media(self, media: PlayerMedia) -> None:
        """Handle PLAY MEDIA on given player."""
        # the player is being (re)purposed on purpose: drop any pending
        # automatic re-join left over from an unexpected stream loss
        self.cancel_group_rejoin()
        async with self._lock:
            if self.synced_to:
                # this should not happen, but guard anyways
                raise RuntimeError("Player is synced")
            self._attr_current_media = media

        # Always stop any existing stream
        if self.stream and self.stream.running and self.stream.session:
            # Set transitioning flag to ignore stale DACP messages (like prevent-playback)
            self._transitioning = True
            # Force stop the session (to speed up stopping)
            await self.stream.session.stop(force=True)
            self.stream = None

        # select audio source
        audio_source = self.mass.streams.get_stream(media, AIRPLAY_FLOW_PCM_FORMAT)

        # setup StreamSession for player (and its sync childs if any)
        sync_clients = self._get_sync_clients()
        provider = cast("AirPlayProvider", self.provider)
        stream_session = AirPlayStreamSession(provider, sync_clients, AIRPLAY_FLOW_PCM_FORMAT)
        await stream_session.start(audio_source)
        self._transitioning = False

    async def play_announcement(
        self, announcement: PlayerMedia, volume_level: int | None = None
    ) -> None:
        """
        Play an announcement natively, mixed over the audio the player is rendering.

        :param announcement: Details of the announcement that needs to be played.
        :param volume_level: Optional volume level for the announcement.
        """
        # The lock windows live inside the orchestration: the dispatch decision and
        # the arming hold self._lock like play_media does, while the multi-second
        # clip waits run outside it (see announce.py).
        await announce.play_announcement(self, announcement, volume_level)

    async def volume_set(self, volume_level: int) -> None:
        """Send VOLUME_SET command to given player."""
        if self.stream and self.stream.running:
            await self.stream.send_cli_command(f"VOLUME={volume_level}")
        self.update_state()
        # store last state in playerconfig
        self.mass.config.set_raw_player_config_value(
            self.player_id, CONF_STORED_VOLUME, volume_level
        )

    async def volume_mute(self, muted: bool) -> None:
        """Handle VOLUME_MUTE command on the player."""
        self._attr_volume_muted = muted
        if self.stream and self.stream.running:
            volume = 0 if muted else (self.volume_level or 0)
            await self.stream.send_cli_command(f"VOLUME={volume}")
        self.update_state()

    async def set_members(
        self,
        player_ids_to_add: list[str] | None = None,
        player_ids_to_remove: list[str] | None = None,
    ) -> None:
        """Handle SET_MEMBERS command on the player."""
        async with self._lock:
            if self.synced_to:
                # this should not happen, but guard anyways
                raise RuntimeError("Player is synced, cannot set members")
            if not player_ids_to_add and not player_ids_to_remove:
                # nothing to do
                return

            stream_session = (
                self.stream.session
                if self.stream and self.stream.running and self.stream.session
                else None
            )
            # handle removals first
            if player_ids_to_remove:
                if self.player_id in player_ids_to_remove:
                    # Callers only ask for this leader alone or for the whole group at once.
                    # A partial self+subset removal would need the other requested members
                    # released here as well, instead of returning right after the leader.
                    remaining_members = [
                        member_id
                        for member_id in self._attr_group_members
                        if member_id != self.player_id and member_id not in player_ids_to_remove
                    ]
                    if stream_session and remaining_members:
                        # Members stay behind: remove only this leader client,
                        # the session continues for the remaining players
                        await stream_session.remove_client(self, reason="leader removed from group")
                    elif stream_session:
                        # The whole group is being removed, tear the session down
                        await stream_session.stop()
                    self._attr_group_members = []
                    self.update_state()
                    return

                for child_player in self._get_sync_clients():
                    if child_player.player_id in player_ids_to_remove:
                        # update group_members first to prevent race conditions
                        # where a concurrent play_media could re-include this player
                        if child_player.player_id in self._attr_group_members:
                            self._attr_group_members.remove(child_player.player_id)
                        if stream_session:
                            await stream_session.remove_client(
                                child_player, reason="child removed from group"
                            )
                        elif child_player.stream and child_player.stream.running:
                            # leader's stream is no longer running but child still has
                            # an active stream - stop it directly
                            await child_player.stream.stop(force=True)

                # If group leader is left alone after removals, clear the group_members list
                if (
                    self._attr_group_members
                    and len(self._attr_group_members) == 1
                    and self.player_id in self._attr_group_members
                ):
                    self._attr_group_members = []

            # handle additions
            for player_id in player_ids_to_add or []:
                if player_id == self.player_id or player_id in self.group_members:
                    # nothing to do: player is already part of the group
                    continue
                child_player_to_add: AirPlayPlayer | None = cast(
                    "AirPlayPlayer | None", self.mass.players.get_player(player_id)
                )
                if not child_player_to_add:
                    # should not happen, but guard against it
                    continue

                # ensure the child does not have an existing stream session active
                if child_player_to_add := cast(
                    "AirPlayPlayer | None", self.mass.players.get_player(player_id)
                ):
                    if (
                        child_player_to_add.playback_state == PlaybackState.PAUSED
                        and child_player_to_add.stream
                    ):
                        # Stop the paused stream to avoid a deadlock situation
                        await child_player_to_add.stream.stop()
                    if (
                        child_player_to_add.stream
                        and child_player_to_add.stream.running
                        and child_player_to_add.stream.session
                        and child_player_to_add.stream.session != stream_session
                    ):
                        await child_player_to_add.stream.session.remove_client(
                            child_player_to_add, reason="moving to different session"
                        )

                # add new child to the existing stream (RAOP or AirPlay2) session (if any)
                self._attr_group_members.append(player_id)
                if stream_session and child_player_to_add is not None:
                    # Skip add_client if the player is already streaming in this session
                    # (e.g. after a dynamic leader switch where the stream continues)
                    if child_player_to_add not in stream_session.sync_clients:
                        await stream_session.add_client(child_player_to_add)
                elif self.active_output_protocol not in (None, "native"):
                    # Members can only be attached to this player's own stream session, which
                    # does not exist while it renders through one of its output protocols.
                    self.logger.warning(
                        "%s joined the group of %s while that player renders through another "
                        "output protocol: there is no stream session to join, so it stays silent",
                        child_player_to_add.display_name if child_player_to_add else player_id,
                        self.display_name,
                    )

            # Ensure group leader includes itself in group_members when it has members
            # This is required for the synced_to property to work correctly
            if self._attr_group_members and self.player_id not in self._attr_group_members:
                self._attr_group_members.insert(0, self.player_id)

            # always update the state after modifying group members
            self.update_state()

    @property
    def ignore_volume_reports(self) -> bool:
        """Return True if the device's own volume reports must not be acted on."""
        if self._volume_reports_ignored_until > time.time():
            # a level we sent ourselves is still echoing back
            return True
        return bool(
            self.config.get_value(CONF_IGNORE_VOLUME)
            or self.device_info.manufacturer.lower() == "apple"
        )

    def suppress_volume_reports(self, seconds: float = AIRPLAY_VOLUME_ECHO_GRACE_S) -> None:
        """
        Ignore the device's own volume reports for the given time.

        :param seconds: How long from now the reports are ignored; a window that is
            already open is only ever extended.
        """
        self._volume_reports_ignored_until = max(
            self._volume_reports_ignored_until, time.time() + seconds
        )

    def update_volume_from_device(self, volume: int) -> None:
        """Update volume from device feedback."""
        if self.ignore_volume_reports:
            return

        cur_volume = self.volume_level or 0
        if abs(cur_volume - volume) > 1 or (time.time() - self.last_command_sent) > 3:
            self.mass.create_task(self._adopt_device_volume(volume))
        else:
            self._attr_volume_level = volume
            self.mass.config.set_raw_player_config_value(self.player_id, CONF_STORED_VOLUME, volume)
            self.update_state()

    def set_discovery_info(self, discovery_info: AsyncServiceInfo, display_name: str) -> None:
        """Set/update the discovery info for the player."""
        self._attr_name = display_name
        if discovery_info.type == AIRPLAY_DISCOVERY_TYPE:
            self.airplay_discovery_info = discovery_info
        elif discovery_info.type == RAOP_DISCOVERY_TYPE:
            self.raop_discovery_info = discovery_info
        else:  # guard
            return
        cur_address = self.address
        prefer_ipv6 = ":" in str(self.mass.streams.publish_ip)
        new_address = get_primary_ip_address_from_zeroconf(discovery_info, prefer_ipv6=prefer_ipv6)
        if new_address is None:
            # should always be set, but guard against None
            return
        if cur_address != new_address:
            # Ignore mDNS updates that replace a routable address with a Docker bridge one.
            try:
                if (
                    cur_address
                    and ipaddress.ip_address(new_address) in _DOCKER_SUBNET
                    and ipaddress.ip_address(cur_address) not in _DOCKER_SUBNET
                ):
                    self.logger.warning(
                        "Ignoring mDNS update from %s to Docker address %s",
                        cur_address,
                        new_address,
                    )
                    self.update_state()
                    return
            except ValueError:
                pass
            self.logger.debug("Address updated from %s to %s", cur_address, new_address)
            self.address = new_address
            self._attr_device_info.ip_address = new_address
        self.update_state()

    def set_state_from_stream(
        self,
        state: PlaybackState | None = None,
        elapsed_time: float | None = None,
        stream: AirPlayStream | None = None,
    ) -> None:
        """
        Set the playback state from stream (RAOP or AirPlay2).

        :param state: New playback state (or None to keep current).
        :param elapsed_time: New elapsed time (or None to keep current).
        :param stream: The stream instance sending this update (for validation).
        """
        # Ignore state updates from old/stale streams
        if stream is not None and stream != self.stream:
            return

        if state is not None:
            prev_state = self._attr_playback_state
            self._attr_playback_state = state
            if self.stream and self.stream.session:
                if prev_state == PlaybackState.PLAYING and state != PlaybackState.PLAYING:
                    self.stream.session.last_paused = time.time()
                elif prev_state != PlaybackState.PLAYING and state == PlaybackState.PLAYING:
                    if self.stream.session.last_paused is not None:
                        pause_duration = time.time() - self.stream.session.last_paused
                        self.stream.session.total_pause_time += pause_duration
                        self.stream.session.last_paused = None
        if elapsed_time is not None:
            self._attr_elapsed_time = elapsed_time
            self._attr_elapsed_time_last_updated = time.time()
        self.update_state()

    def get_stream_pcm_format(self, session_pcm_format: AudioFormat) -> AudioFormat:
        """
        Return the PCM format to feed this player's cliairplay process.

        :param session_pcm_format: The PCM format of the (shared) stream session.
        """
        if not self.hires_playback_enabled:
            return AIRPLAY_PCM_FORMAT
        # 24-bit: the binary expects raw s32le input on stdin (--bitdepth 24)
        # and truncates to 24-bit ALAC internally.
        supported_rates = {sample_rate for sample_rate, _ in self.supported_sample_rates}
        sample_rate = (
            session_pcm_format.sample_rate
            if session_pcm_format.sample_rate in supported_rates
            else AIRPLAY_PCM_FORMAT.sample_rate
        )
        return AudioFormat(
            content_type=ContentType.PCM_S32LE,
            sample_rate=sample_rate,
            bit_depth=24,
        )

    @property
    def owns_volume(self) -> bool:
        """
        Return True if this output is the resolved owner of its own volume.

        AirPlay volume is the receiver's own volume: setting it writes through to the
        device and persists there after the session ends. It may therefore only be set
        when no other control owns the volume of this output.
        """
        if (
            self.protocol_parent_id
            and (parent_player := self.mass.players.get_player(self.protocol_parent_id))
            and parent_player.state.volume_level is not None
        ):
            if self._has_native_protocol_parent:
                # Native parent volume is on the receiver/amplifier scale.
                # Keep the AirPlay child volume learned from DACP feedback instead.
                return
            if parent_player.state.volume_level == 0:
                # A parent volume of 0 usually means the (idle) sibling interface
                # feeding the parent doesn't know the real device volume, e.g. the
                # cast side of the same device reports 0 while in standby. Adopting
                # it would start the stream hard muted, so keep our own last known
                # volume instead.
                return
            if self._attr_volume_level == parent_player.state.volume_level:
                return
            self._attr_volume_level = parent_player.state.volume_level
            self.mass.config.set_raw_player_config_value(
                self.player_id, CONF_STORED_VOLUME, self._attr_volume_level
            )
            self.update_state()

    async def on_config_updated(self) -> None:
        """Handle logic when the player config is updated."""
        await super().on_config_updated()
        prov = cast("AirPlayProvider", self.provider)
        bridge_manager = prov.bridge_manager
        has_bridge = bridge_manager.get_bridge(self.player_id) is not None
        if self.protocol == StreamingProtocol.AIRPLAY2 and has_bridge:
            # AP2 doesn't support sync — tear down the Sendspin bridge
            await bridge_manager.remove_bridge(self.player_id)
        elif self.protocol != StreamingProtocol.AIRPLAY2 and not has_bridge:
            # Switched back to RAOP — set up the Sendspin bridge
            await bridge_manager.setup_bridge(self)

    async def on_unload(self) -> None:
        """Handle logic when the player is unloaded from the Player controller."""
        await super().on_unload()
        self.cancel_group_rejoin()
        if self.stream:
            # remove this player from the stream session if it is running
            if self.stream.running and self.stream.session:
                await self.stream.session.remove_client(self, reason="player unloaded")
            self.stream = None

    def schedule_group_rejoin(self, candidate_ids: list[str]) -> None:
        """
        Schedule a bounded automatic re-join of this player to its still-active group.

        Used when this player's stream process died unexpectedly while it was part
        of a playing sync group (e.g. the device rode out a network blackout): the
        player is re-added to the group's live session through the regular
        late-join path after a short backoff. Any user action on the player (or it
        joining a session by other means) cancels the re-join; when the group is
        no longer playing, its membership was changed meanwhile or the device is
        offline, the re-join is abandoned and the player simply stays idle.

        :param candidate_ids: Player ids that led or shared the group at the
            moment the stream was lost, used to resolve the re-join target (the
            leadership may transfer while the backoff runs).
        """
        self.cancel_group_rejoin()
        self.logger.info(
            "Scheduling automatic re-join of %s to its group after unexpected stream loss",
            self.display_name,
        )
        self._rejoin_task = self.mass.create_task(self._group_rejoin_attempts(candidate_ids))

    def cancel_group_rejoin(self) -> None:
        """Cancel any pending automatic group re-join attempts for this player."""
        rejoin_task = self._rejoin_task
        # never self-cancel: the re-join attempt itself flows through the same
        # session (re)start paths that call this to clear stale schedules. The
        # handle also survives such a call, so a later user action can still
        # cancel the retry loop between attempts.
        if rejoin_task is None or rejoin_task is asyncio.current_task():
            return
        self._rejoin_task = None
        if not rejoin_task.done():
            rejoin_task.cancel()

    def on_player_media_updated(self) -> None:
        """Handle callback when the current media of the player is updated."""
        if not self.stream or not self.stream.running:
            return
        metadata = self.state.current_media
        if not metadata:
            return
        progress = int(metadata.corrected_elapsed_time or 0)
        self.mass.create_task(self.stream.send_metadata(progress, metadata))

    async def _adopt_device_volume(self, volume: int) -> None:
        """
        Take over a level the device set itself.

        :param volume: The level the device reported.
        """
        ignored_until = self._volume_reports_ignored_until
        await self.volume_set(volume)
        # Writing the level back is a volume command like any other and opens the echo
        # window, but this one only hands the device its own level: leaving the window
        # open would swallow the rest of a volume the user is still turning up. A longer
        # window opened while this was in flight (an announcement) still stands.
        if self._volume_reports_ignored_until <= time.time() + AIRPLAY_VOLUME_ECHO_GRACE_S:
            self._volume_reports_ignored_until = ignored_until

    def _control_routes_to_self(self, control: str) -> bool:
        """Return True if the given (resolved) control routes to this player."""
        if control == self.player_id:
            return True
        # bridge players riding on this player (e.g. Sendspin-over-AirPlay) forward to us
        if control_player := self.mass.players.get_player(control):
            return control_player.underlying_player_id == self.player_id
        return False

    def _get_flags(self) -> int:
        # Flags are either present via "sf" or "flags". Taken from pyatv.protocols.airplay.utils.
        # We combine flags from both RAOP and AirPlay discovery services because
        # LEGACY_PAIRING_BIT (0x200) is typically only in the RAOP service sf field
        # (e.g. Apple TV HD), while PIN_REQUIRED (0x8) may only appear in the AirPlay
        # service sf/flags field. Using only one source misses the pairing requirement.
        flags = 0
        for discovery_info in filter(None, [self.raop_discovery_info, self.airplay_discovery_info]):
            raw = (
                discovery_info.properties.get(b"sf")
                or discovery_info.properties.get(b"flags")
                or b"0x0"
            )
            with contextlib.suppress(ValueError, TypeError):
                flags |= int(raw, 16)
        return flags

    def _requires_pin_pairing(self) -> bool:
        """
        Check if this device requires pairing.

        Adapted from pyatv.protocols.airplay.utils.get_pairing_requirement.
        """
        return bool(self._get_flags() & (LEGACY_PAIRING_BIT | PIN_REQUIRED))

    def _get_credentials_key(self, protocol: StreamingProtocol) -> str:
        """Get the config key for credentials for given protocol."""
        if protocol == StreamingProtocol.RAOP:
            return CONF_RAOP_CREDENTIALS
        return CONF_AIRPLAY_CREDENTIALS

    @property
    def _advertised_features(self) -> str | None:
        """Return the AirPlay features bitmask the device advertises via mDNS."""
        # Prefer the _airplay service's ``features``, falling back to the _raop
        # service's ``ft`` when the former is absent (some devices only populate one).
        features: str | None = None
        if self.airplay_discovery_info:
            features = self.airplay_discovery_info.decoded_properties.get(
                "features"
            ) or self.airplay_discovery_info.decoded_properties.get("ft")
        if not features and self.raop_discovery_info:
            features = self.raop_discovery_info.decoded_properties.get("ft")
        return features

    @property
    def _is_airplay2_capable(self) -> bool:
        """
        Return whether this device can stream over AirPlay 2.

        Mirrors the feature-bit test the cliairplay binary uses for its own route
        selection: a device is AirPlay 2 capable when it exposes the _airplay
        service and either advertises the AirPlay 2 feature bits or offers no RAOP
        fallback at all (i.e. it is a pure AirPlay 2 receiver).
        """
        if not self.airplay_discovery_info:
            return False
        return supports_airplay2(self._advertised_features) or not self.raop_discovery_info

    async def _run_streaming_pairing(
        self, session: SetupSession, collected: dict[str, ConfigValueType]
    ) -> None:
        """
        Pair the streaming protocol (RAOP or AirPlay 2) and collect the device password.

        The two are evaluated independently: a device that is already paired can
        still be missing its password (or have had it rejected), which is exactly
        the state a receiver ends up in when it gains password protection after
        it was set up.

        :param session: The setup flow session used to interact with the user.
        :param collected: The values collected so far; updated in place.
        """
        password_collected = await self._run_protocol_pairing(session, collected)
        if not password_collected and self.needs_password_setup:
            await self._ask_device_password(session)

    async def _run_protocol_pairing(
        self, session: SetupSession, collected: dict[str, ConfigValueType]
    ) -> bool:
        """
        Pair the streaming protocol (RAOP or AirPlay 2), unless already paired.

        When the device requires pairing this runs it, re-offering it as a skippable
        step when credentials are already stored (so a re-launched flow can replace a
        stale pairing). When the device requires no pairing, any leftover credentials
        are cleared: they would keep forcing the pair-verify route, which some
        receivers (e.g. HomePods after their password was removed) accept while
        refusing to actually output audio. The obtained credentials are added to
        ``collected`` under the protocol-specific key.

        :param session: The setup flow session used to interact with the user.
        :param collected: The values collected so far; updated in place.
        :return: Whether the device password was collected as part of the pairing.
        """
        pin_pairing = self._requires_pin_pairing()
        # a password only replaces PIN pairing on the native AirPlay 2 flow
        password_pairing = self.password_required and self.protocol == StreamingProtocol.AIRPLAY2
        if not (pin_pairing or password_pairing):
            for cred_key in (CONF_AIRPLAY_CREDENTIALS, CONF_RAOP_CREDENTIALS):
                if self.get_setup_value(cred_key) is not None:
                    collected[cred_key] = None
            return False
        already_paired = bool(
            self.get_setup_value(CONF_AIRPLAY_CREDENTIALS)
            or self.get_setup_value(CONF_RAOP_CREDENTIALS)
        )
        if already_paired and not await self._offer_optional_pairing(
            session, "streaming_repair_offer"
        ):
            return False

        protocol = self.protocol
        cred_key = self._get_credentials_key(protocol)
        if pin_pairing:
            step_id, field_key, field_type, field_format = (
                "pair_pin",
                CONF_PAIRING_PIN,
                ConfigEntryType.PAIRING_CODE,
                PAIRING_PIN_FORMAT,
            )
        else:
            step_id, field_key, field_type, field_format = (
                "pair_password",
                CONF_PAIRING_PASSWORD,
                ConfigEntryType.SECURE_STRING,
                None,
            )

        errors: dict[str, str] | None = None
        while True:
            # Each attempt uses a fresh session: finish_pairing() closes the live
            # subprocess/session on completion, so a rejected PIN needs a new one
            # (and the device re-shows its PIN).
            pairing = await self._prepare_streaming_pairing(protocol, pin_pairing=pin_pairing)
            try:
                values = await session.form(
                    [
                        ConfigEntry(
                            key=field_key,
                            type=field_type,
                            required=True,
                            category="protocol_generic",
                            format=field_format,
                        )
                    ],
                    step_id=step_id,
                    errors=errors,
                )
                entered_value = str(values[field_key])
                credentials = await pairing.finish_pairing(pin=entered_value)
            except PlayerCommandFailed as err:
                # leave a default-level trace: the flow swallows the error into
                # the re-served form, which support logs otherwise never show
                self.logger.warning("Pairing with %s failed: %s", self.display_name, err)
                errors = {"base": err.translation_key or str(err)}
                continue
            finally:
                # tears down the subprocess on retry, success and abort (cancellation)
                await pairing.close()
            collected[cred_key] = credentials
            if password_pairing:
                # The device password authenticates every later stream too (the
                # binary's transient leg), so keep it next to the credentials
                # instead of discarding it with the setup form.
                self._store_device_password(entered_value)
            return password_pairing

    async def _ask_device_password(self, session: SetupSession) -> None:
        """
        Ask for the device password and store it, without attempting any pairing.

        Covers the devices that have no pairing to do: a legacy RAOP receiver, and
        an already paired device whose password is missing or was rejected. There
        is no live session to validate the entry against, so a wrong password only
        surfaces on the next connect - which marks the player as needing setup again.

        :param session: The setup flow session used to interact with the user.
        """
        values = await session.form(
            [
                ConfigEntry(
                    key=CONF_PAIRING_PASSWORD,
                    type=ConfigEntryType.SECURE_STRING,
                    required=True,
                    category="protocol_generic",
                )
            ],
            step_id="pair_password",
        )
        self._store_device_password(str(values[CONF_PAIRING_PASSWORD]))

    async def _offer_optional_pairing(self, session: SetupSession, step_id: str) -> bool:
        """
        Ask whether to run the offered (optional) pairing now.

        :param session: The setup flow session used to interact with the user.
        :param step_id: The (i18n) step id describing the offered pairing.
        """
        values = await session.form(
            [
                ConfigEntry(
                    key=CONF_PAIR_NOW,
                    type=ConfigEntryType.BOOLEAN,
                    default_value=False,
                    category="protocol_generic",
                )
            ],
            step_id=step_id,
        )
        return bool(values[CONF_PAIR_NOW])

    async def _prepare_streaming_pairing(
        self, protocol: StreamingProtocol, *, pin_pairing: bool
    ) -> AirPlayPairing:
        """
        Build and start a streaming pairing session (the device shows its PIN).

        A failure here cannot be recovered by re-prompting the user, so it aborts the
        flow; a partially started session is torn down first.

        :param protocol: The streaming protocol to pair (RAOP or AirPlay 2).
        :param pin_pairing: Whether the device shows a PIN the user must enter.
        """
        pairing: AirPlayPairing | None = None
        started = False
        try:
            pairing = self._build_streaming_pairing(protocol)
            await pairing.start_pairing_session()
            if pin_pairing:
                await pairing.start_pin_pairing()
            started = True
        except Exception as err:
            # a failure starting the session (device unreachable, binary/system
            # issue, ...) cannot be fixed by re-prompting, so abort with a clear
            # reason instead of letting it surface as a generic internal error
            self.logger.warning("Could not start AirPlay pairing session: %s", err)
            raise AbortFlow("pairing_failed") from err
        finally:
            if not started and pairing is not None:
                await pairing.close()
        assert pairing is not None  # reached only when started, i.e. a live session
        return pairing

    def _build_streaming_pairing(self, protocol: StreamingProtocol) -> AirPlayPairing:
        """
        Build an AirPlayPairing for the given streaming protocol.

        :param protocol: The streaming protocol to pair (RAOP or AirPlay 2).
        """
        from .pairing import AirPlayPairing  # noqa: PLC0415

        # For Apple devices pairing always happens on the AirPlay port (7000) even
        # when streaming will use RAOP; the RAOP port (5000) is only for streaming.
        port: int | None = None
        if self.airplay_discovery_info:
            port = self.airplay_discovery_info.port or 7000
        elif self.raop_discovery_info:
            port = self.raop_discovery_info.port or 5000
        provider = cast("AirPlayProvider", self.provider)
        device_id = provider.dacp_id
        pairing_address = self.address
        if protocol == StreamingProtocol.AIRPLAY2 and not isinstance(
            ipaddress.ip_address(pairing_address), ipaddress.IPv4Address
        ):
            if self.airplay_discovery_info:
                discovered_address = get_primary_ip_address_from_zeroconf(
                    self.airplay_discovery_info
                )
                if discovered_address and isinstance(
                    ipaddress.ip_address(discovered_address), ipaddress.IPv4Address
                ):
                    pairing_address = discovered_address
            if not isinstance(ipaddress.ip_address(pairing_address), ipaddress.IPv4Address):
                raise PlayerCommandFailed("AirPlay pairing requires an IPv4 device address")
        return AirPlayPairing(
            address=pairing_address,
            name=self.display_name,
            protocol=protocol,
            logger=self.logger,
            port=port,
            device_id=device_id,
        )

    async def _get_session_pcm_format(
        self, sync_clients: list[AirPlayPlayer], media: PlayerMedia
    ) -> AudioFormat:
        """
        Select the shared PCM format for a new stream session.

        :param sync_clients: All players that will take part in the session.
        :param media: The media that is about to be played.
        """
        queue = self.mass.player_queues.get(media.source_id) if media.source_id else None
        queue_item = (
            self.mass.player_queues.get_item(media.source_id, media.queue_item_id)
            if media.source_id and media.queue_item_id
            else None
        )
        streamdetails = queue_item.streamdetails if queue_item else None
        crossfade_enabled = bool(
            queue
            and media.media_type == MediaType.TRACK
            and self.mass.streams.get_crossfade_mode(queue) != CrossfadeMode.DISABLED
        )
        return await self.mass.streams.audio.select_flow_pcm_format(
            self,
            start_streamdetails=streamdetails,
            crossfade_enabled=crossfade_enabled,
            overlay_active=bool(queue and overlay_active(queue)),
            fallback_sample_rate=AIRPLAY_PCM_FORMAT.sample_rate,
            output_players=sync_clients,
        )

    @property
    def _has_native_protocol_parent(self) -> bool:
        """Return True if this AirPlay protocol player is linked to a native parent."""
        if not self.protocol_parent_id:
            return False
        parent_player = self.mass.players.get_player(self.protocol_parent_id)
        return bool(parent_player and parent_player.volume_control == PLAYER_CONTROL_NATIVE)

    def _get_sync_clients(self) -> list[AirPlayPlayer]:
        """Get all sync clients for a player."""
        sync_clients: list[AirPlayPlayer] = []
        # we need to return the player itself too
        group_child_ids = {self.player_id}
        group_child_ids.update(self.group_members)
        for child_id in group_child_ids:
            if client := cast("AirPlayPlayer | None", self.mass.players.get_player(child_id)):
                sync_clients.append(client)
        return sync_clients

    async def _group_rejoin_attempts(self, candidate_ids: list[str]) -> None:
        """Re-join this player to its group's live session after a bounded backoff."""
        max_attempts = len(AIRPLAY_REJOIN_ATTEMPT_DELAYS)
        for attempt, delay in enumerate(AIRPLAY_REJOIN_ATTEMPT_DELAYS, start=1):
            await asyncio.sleep(delay)
            if (
                self.group_members
                or (self.stream and self.stream.running)
                or self.playback_state != PlaybackState.IDLE
                # synced into a group outside the original one = deliberate regroup.
                # Still pointing at an original candidate is fine: a static group
                # keeps the sync membership while only the session lost this player.
                or (self.synced_to and self.synced_to not in candidate_ids)
            ):
                # the player was grouped or repurposed by other means meanwhile
                self.logger.debug(
                    "Automatic group re-join for %s cancelled: player is active again",
                    self.display_name,
                )
                return
            if not self.available:
                # the device is offline: an attempt cannot succeed and the user
                # may well have switched it off on purpose
                self.logger.debug(
                    "Automatic group re-join for %s cancelled: player is unavailable",
                    self.display_name,
                )
                return
            target = self._resolve_rejoin_target(candidate_ids)
            if target is None:
                # the group may be between sessions (e.g. a track change); keep
                # trying until the attempts run out
                self.logger.debug(
                    "Automatic group re-join attempt %d/%d for %s: no playing group found",
                    attempt,
                    max_attempts,
                    self.display_name,
                )
                continue
            # When the sync membership survived the stream loss (a static group,
            # where membership is configuration), only the running session needs
            # healing; a group command would no-op on the existing membership.
            heal_session = (
                target.stream.session
                if self.player_id in target.group_members and target.stream is not None
                else None
            )
            try:
                if heal_session is not None:
                    await heal_session.add_client(self)
                else:
                    # Join through the target's own set_members: both ends are
                    # players of this provider, so the join never needs the
                    # visible-player translations of the controller's grouping
                    # pipeline - and that pipeline's capability gate reflects
                    # grouping state that is in flux right after a stream loss,
                    # so it may silently refuse an internal re-join.
                    await target.set_members(player_ids_to_add=[self.player_id])
            except Exception as err:
                self.logger.warning(
                    "Automatic re-join of %s to group of %s failed (attempt %d/%d): %s",
                    self.display_name,
                    target.display_name,
                    attempt,
                    max_attempts,
                    err,
                )
                continue
            # A late-join can also fail without raising (the player then holds
            # group membership without a live stream), so verify the session
            # actually carries this player before declaring success.
            if (
                self.stream
                and self.stream.running
                and self.stream.session
                and self in self.stream.session.sync_clients
            ):
                self.logger.info(
                    "Automatically re-joined %s to the group of %s after stream loss",
                    self.display_name,
                    target.display_name,
                )
                return
            self.logger.warning(
                "Automatic re-join of %s did not produce a running stream (attempt %d/%d)",
                self.display_name,
                attempt,
                max_attempts,
            )
            if heal_session is None:
                # undo the group membership this attempt created so a retry (or
                # a manual regroup) starts from a clean join
                try:
                    await target.set_members(player_ids_to_remove=[self.player_id])
                except Exception as err:
                    # a failed undo leaves the membership for the next attempt,
                    # which then heals the session instead of joining anew
                    self.logger.debug(
                        "Undo of failed re-join attempt for %s failed: %s",
                        self.display_name,
                        err,
                    )
        self.logger.warning(
            "Giving up on automatic group re-join for %s after %d attempt(s); "
            "the player stays idle",
            self.display_name,
            max_attempts,
        )

    def _resolve_rejoin_target(self, candidate_ids: list[str]) -> AirPlayPlayer | None:
        """Resolve which player now carries the group's actively playing session."""
        for candidate_id in candidate_ids:
            candidate = self.mass.players.get_player(candidate_id)
            if candidate is None or candidate is self:
                continue
            if not isinstance(candidate, AirPlayPlayer):
                continue
            if candidate.synced_to:
                # the candidate was absorbed into another group since the loss
                # (user intent): never follow the old group's players elsewhere.
                # A leadership transfer inside the original group is still found:
                # the promoted member is itself one of the candidates.
                continue
            if not candidate.available:
                continue
            # only a PLAYING session can absorb a late joiner: a parked (paused)
            # session has no live timeline to anchor against
            if candidate.playback_state != PlaybackState.PLAYING:
                continue
            if not (candidate.stream and candidate.stream.running and candidate.stream.session):
                continue
            return candidate
        return None

    def _store_device_password(self, password: str) -> None:
        """
        Persist a device password so every later stream can authenticate with it.

        :param password: The plaintext password entered by the user.
        """
        self.mass.config.set_raw_player_config_value(
            self.player_id, CONF_PASSWORD, self.mass.config.encrypt_string(password)
        )
        # a freshly entered password deserves a clean slate: the reject marker
        # would otherwise keep the player in "needs setup" until the next connect
        self.set_password_invalid(False)

    @property
    def _hires_default_enabled(self) -> bool:
        """Return the per-device default for the 24-bit toggle."""
        return default_hires_enabled(
            self.device_info.manufacturer or "", self.device_info.model or ""
        )


class GenericAirPlayPlayer(AirPlayPlayer):
    """AirPlay protocol endpoint without independent device control."""

    _attr_type = PlayerType.PROTOCOL
