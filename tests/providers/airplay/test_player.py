"""Unit tests for AirPlay player."""

import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from music_assistant_models.constants import PLAYER_CONTROL_NATIVE

from music_assistant.providers.airplay.constants import (
    CONF_IGNORE_VOLUME,
    CONF_STORED_VOLUME,
    CONF_STREAMING_MODE,
    STREAMING_MODE_AP2_COMPAT,
    STREAMING_MODE_AP2_NTP,
    STREAMING_MODE_AUTO,
    STREAMING_MODE_RAOP,
    StreamingProtocol,
)
from music_assistant.providers.airplay.player import AirPlayPlayer
from music_assistant.providers.airplay.provider import AirPlayProvider
from music_assistant.providers.airplay.stream_session import AirPlayStreamSession

# _airplay._tcp features bitmask with the AirPlay 2 feature bits set (bit 38/48).
AP2_FEATURES = "0x4A7FDFD5,0x3C177FDE"
# audioFormat bits as advertised in a receiver's /info format tables.
ALAC_44100_16 = 1 << 18
ALAC_44100_24 = 1 << 19
ALAC_48000_24 = 1 << 21


def _stub_raw_config(provider: MagicMock, stored: dict[str, object] | None = None) -> None:
    """Serve raw player config values from a dict instead of an (always truthy) mock."""
    values = stored if stored is not None else {}
    provider.mass.config.get_raw_player_config_value.side_effect = (
        lambda _player_id, key, default=None: values.get(key, default)
    )
    provider.mass.config.set_raw_player_config_value.side_effect = lambda _player_id, key, value: (
        values.__setitem__(key, value)
    )


def _stub_volume_scaling(provider: MagicMock, min_volume: int = 0, max_volume: int = 100) -> None:
    """Apply the controller's real min/max volume scaling instead of a mock."""
    identity = (min_volume, max_volume) == (0, 100)
    provider.mass.players.scale_volume_to_device.side_effect = lambda _player_id, logical: (
        logical if identity else min_volume + (logical * (max_volume - min_volume)) // 100
    )
    provider.mass.players.scale_volume_from_device.side_effect = lambda _player_id, device: (
        device if identity else ((device - min_volume) * 100) // (max_volume - min_volume)
    )


@pytest.fixture
def airplay_player() -> AirPlayPlayer:
    """Create a basic AirPlayPlayer with mock defaults."""
    provider = MagicMock()
    _stub_raw_config(provider)
    _stub_volume_scaling(provider)
    return AirPlayPlayer(
        provider=provider,
        player_id="test_player",
        display_name="Test Player",
        address="127.0.0.1",
        manufacturer="Test Manufacturer",
        model="Test Model",
        raop_discovery_info=None,
        airplay_discovery_info=None,
    )


@pytest.mark.parametrize(
    ("manufacturer", "model", "expected"),
    [
        ("Apple", "MacBookPro18,3", False),
        ("Apple Inc.", "MacBook Air (MacBookAir10,1)", False),
        ("Apple", "iMac (iMac21,1)", False),
        ("Apple", "Mac mini (Mac16,11)", False),
        ("Apple", "Mac Pro (MacPro7,1)", False),
        ("Apple", "Mac Studio (Mac14,13)", False),
        ("Apple", "HomePod Mini", True),
        ("Apple", "Apple TV 4K", True),
        ("Acme", "Mac-compatible receiver", True),
    ],
)
def test_macos_devices_are_disabled_by_default(
    manufacturer: str, model: str, expected: bool
) -> None:
    """Macs are disabled by default without affecting other AirPlay receivers."""
    provider = MagicMock()
    player = AirPlayPlayer(
        provider=provider,
        player_id="test_player",
        display_name="Test Player",
        address="127.0.0.1",
        manufacturer=manufacturer,
        model=model,
        raop_discovery_info=None,
        airplay_discovery_info=None,
    )

    assert player.enabled_by_default is expected
    assert provider.mass.config.create_default_player_config.call_args.args[-1] is expected


@pytest.mark.parametrize(
    ("aiplay_properties", "raop_properties", "expected"),
    [
        ({b"flags": b"0x200"}, None, True),
        ({b"sf": b"0x201"}, None, True),
        ({b"flags": b"0x4"}, None, False),
        ({b"sf": b"0x8"}, None, True),
        ({b"flags": b"0x9"}, None, True),
        (None, {b"flags": "0x200"}, True),
        (None, {b"sf": b"0x201"}, True),
        (None, {b"flags": b"0x4"}, False),
        (None, {b"sf": b"0x8"}, True),
        (None, {b"flags": b"0x9"}, True),
        # Combined flags across discovery records should be OR-ed.
        ({b"sf": b"0x8"}, {b"sf": b"0x200"}, True),
        ({b"sf": b"0x200"}, {b"sf": b"0x8"}, True),
        ({b"flags": b"0x4"}, {b"flags": b"0x0"}, False),
        ({}, {}, False),
    ],
)
def test_requires_pin_pairing(
    airplay_player: AirPlayPlayer,
    aiplay_properties: dict[bytes, bytes] | None,
    raop_properties: dict[bytes, bytes] | None,
    expected: bool,
) -> None:
    """Test the _requires_pairing method of AirPlayPlayer."""
    if aiplay_properties is not None:
        aiplay_discovery_info = MagicMock()
        aiplay_discovery_info.properties = aiplay_properties
        airplay_player.airplay_discovery_info = aiplay_discovery_info
    if raop_properties is not None:
        raop_discovery_info = MagicMock()
        raop_discovery_info.properties = raop_properties
        airplay_player.raop_discovery_info = raop_discovery_info
    assert airplay_player._requires_pin_pairing() == expected


@pytest.mark.parametrize(
    ("aiplay_properties", "raop_properties", "expected"),
    [
        ({b"flags": b"0x80"}, None, True),
        ({b"sf": b"0x81"}, None, True),
        ({b"flags": b"0x4"}, None, False),
        ({b"sf": b"0x80"}, None, True),
        ({b"flags": b"0x90"}, None, True),
        ({b"flags": b"0x1000"}, None, False),
        (None, {b"flags": "0x80"}, True),
        (None, {b"sf": b"0x81"}, True),
        (None, {b"flags": b"0x4"}, False),
        (None, {b"sf": b"0x80"}, True),
        (None, {b"flags": b"0x90"}, True),
        ({}, {}, False),
    ],
)
def test_password_required(
    airplay_player: AirPlayPlayer,
    aiplay_properties: dict[bytes, bytes] | None,
    raop_properties: dict[bytes, bytes] | None,
    expected: bool,
) -> None:
    """Test the flags-based password announcements."""
    if aiplay_properties is not None:
        aiplay_discovery_info = MagicMock()
        aiplay_discovery_info.properties = aiplay_properties
        aiplay_discovery_info.decoded_properties = {}
        airplay_player.airplay_discovery_info = aiplay_discovery_info
    if raop_properties is not None:
        raop_discovery_info = MagicMock()
        raop_discovery_info.properties = raop_properties
        raop_discovery_info.decoded_properties = {}
        airplay_player.raop_discovery_info = raop_discovery_info
    assert airplay_player.password_required == expected


def test_build_streaming_pairing_uses_discovered_ipv4_address() -> None:
    """HAP pairing falls back to a discovered IPv4 address when playback uses IPv6."""
    provider = MagicMock()
    provider.dacp_id = "test_dacp"
    airplay_info = MagicMock()
    airplay_info.properties = {b"flags": b"0x80"}
    airplay_info.port = 7000
    player = AirPlayPlayer(
        provider=provider,
        player_id="test_player",
        display_name="Test Player",
        address="2001:db8::10",
        manufacturer="Apple",
        model="AppleTV",
        raop_discovery_info=None,
        airplay_discovery_info=airplay_info,
    )
    pairing_instance = MagicMock()

    with (
        patch(
            "music_assistant.providers.airplay.player.get_primary_ip_address_from_zeroconf",
            return_value="192.168.1.50",
        ),
        patch(
            "music_assistant.providers.airplay.pairing.AirPlayPairing",
            return_value=pairing_instance,
        ) as pairing_cls,
    ):
        result = player._build_streaming_pairing(StreamingProtocol.AIRPLAY2)

    assert result is pairing_instance
    assert pairing_cls.call_args.kwargs["address"] == "192.168.1.50"


def test_build_streaming_pairing_fails_without_ipv4_address() -> None:
    """HAP pairing reports an actionable error when discovery has no IPv4 address."""
    provider = MagicMock()
    provider.dacp_id = "test_dacp"
    airplay_info = MagicMock()
    airplay_info.properties = {b"flags": b"0x80"}
    airplay_info.port = 7000
    player = AirPlayPlayer(
        provider=provider,
        player_id="test_player",
        display_name="Test Player",
        address="2001:db8::10",
        manufacturer="Apple",
        model="AppleTV",
        raop_discovery_info=None,
        airplay_discovery_info=airplay_info,
    )

    with (
        patch(
            "music_assistant.providers.airplay.player.get_primary_ip_address_from_zeroconf",
            return_value="2001:db8::20",
        ),
        pytest.raises(PlayerCommandFailed, match="requires an IPv4"),
    ):
        player._build_streaming_pairing(StreamingProtocol.AIRPLAY2)


@pytest.mark.asyncio
async def test_config_entries_include_ignore_volume(airplay_player: AirPlayPlayer) -> None:
    """The ignore_volume setting must be offered in the player config entries."""
    entries = await airplay_player.get_config_entries()
    assert any(entry.key == CONF_IGNORE_VOLUME for entry in entries)


@pytest.mark.asyncio
async def test_config_entries_preserve_raop_encryption_setting(
    airplay_player: AirPlayPlayer,
) -> None:
    """RAOP keeps its advanced encryption toggle with the secure default enabled."""
    entries = await airplay_player.get_config_entries()
    entry = next(entry for entry in entries if entry.key == CONF_ENCRYPTION)

    assert entry.default_value is True
    assert entry.hidden is False
    assert entry.advanced is True


@pytest.mark.asyncio
async def test_config_entries_sync_adjust_is_non_advanced(airplay_player: AirPlayPlayer) -> None:
    """AirPlay offers sync_adjust as a discoverable (non-advanced) setting."""
    entries = await airplay_player.get_config_entries()
    entry = next((entry for entry in entries if entry.key == CONF_SYNC_ADJUST), None)
    assert entry is not None
    # non-advanced so users can find it: it is the primary control for compensating
    # a device wired to a TV / AV receiver / amplifier that adds its own audio delay
    assert entry.advanced is False


def _set_discovery_info(
    player: AirPlayPlayer,
    *,
    raop: bool,
    airplay: bool,
    airplay_features: str | None = None,
) -> None:
    """
    Attach discovery mocks so the device advertises the given protocols.

    :param airplay_features: When set, the _airplay service advertises this
        ``features`` bitmask (e.g. to mark the device AirPlay 2 capable).
    """
    if raop:
        raop_info = MagicMock()
        raop_info.properties = {}
        raop_info.decoded_properties = {}
        player.raop_discovery_info = raop_info
    else:
        player.raop_discovery_info = None
    if airplay:
        airplay_info = MagicMock()
        airplay_info.properties = {}
        airplay_info.decoded_properties = {"features": airplay_features} if airplay_features else {}
        player.airplay_discovery_info = airplay_info
    else:
        player.airplay_discovery_info = None


def _make_apple_player() -> AirPlayPlayer:
    """Create an AirPlayPlayer that identifies as a genuine Apple device."""
    return AirPlayPlayer(
        provider=MagicMock(),
        player_id="test_player",
        display_name="Test Apple TV",
        address="127.0.0.1",
        manufacturer="Apple",
        model="Apple TV 4K",
        raop_discovery_info=None,
        airplay_discovery_info=None,
    )


# --- Streaming-mode escape hatch: entry visibility ---


@pytest.mark.asyncio
async def test_streaming_mode_offered_for_non_apple_airplay2(
    airplay_player: AirPlayPlayer,
) -> None:
    """A non-Apple AirPlay 2 device gets the streaming-mode pin with its own lanes."""
    _set_discovery_info(airplay_player, raop=True, airplay=True, airplay_features=AP2_FEATURES)
    entries = await airplay_player.get_config_entries()
    entry = next((entry for entry in entries if entry.key == CONF_STREAMING_MODE), None)
    assert entry is not None
    assert entry.default_value == STREAMING_MODE_AUTO
    # advanced-only: it is a workaround, not a routine protocol choice
    assert entry.advanced is True
    values = [option.value for option in entry.options]
    # this device advertises RAOP too, so the legacy lane is on offer
    assert STREAMING_MODE_RAOP in values
    assert STREAMING_MODE_AP2_NTP in values


@pytest.mark.asyncio
async def test_streaming_mode_on_apple_offers_no_ntp_lane() -> None:
    """
    Apple devices get the entry as an escape hatch, minus the NTP lane.

    An Apple receiver renders silence on an NTP-timed realtime stream
    (hardware-measured), so that lane is never offered; the compatibility
    flow and legacy RAOP remain available as the escapes for networks where
    the PTP ports are blocked, and pinning PTP stays possible as an explicit
    choice of the normal lane.
    """
    player = _make_apple_player()
    _set_discovery_info(player, raop=True, airplay=True, airplay_features=AP2_FEATURES)
    entries = await player.get_config_entries()
    entry = next((entry for entry in entries if entry.key == CONF_STREAMING_MODE), None)
    assert entry is not None
    values = [option.value for option in entry.options]
    assert STREAMING_MODE_AP2_NTP not in values
    assert STREAMING_MODE_RAOP in values


@pytest.mark.asyncio
async def test_streaming_mode_hidden_for_raop_only(airplay_player: AirPlayPlayer) -> None:
    """A RAOP-only device has no alternative lane to pin, so no entry is offered."""
    _set_discovery_info(airplay_player, raop=True, airplay=False)
    entries = await airplay_player.get_config_entries()
    assert all(entry.key != CONF_STREAMING_MODE for entry in entries)


@pytest.mark.asyncio
async def test_streaming_mode_lanes_for_airplay2_only(airplay_player: AirPlayPlayer) -> None:
    """
    An AirPlay-2-only device offers the AirPlay 2 lanes but no RAOP.

    This is the class the entry exists for: video-class TVs with no _raop
    service and a PTP advertisement their stack never honors need the NTP
    lane as their only escape.
    """
    _set_discovery_info(airplay_player, raop=False, airplay=True, airplay_features=AP2_FEATURES)
    entries = await airplay_player.get_config_entries()
    entry = next((entry for entry in entries if entry.key == CONF_STREAMING_MODE), None)
    assert entry is not None
    values = [option.value for option in entry.options]
    assert STREAMING_MODE_AP2_NTP in values
    assert STREAMING_MODE_RAOP not in values


# --- Protocol resolution ---


@pytest.mark.parametrize(
    ("airplay_props", "raop_props", "expected"),
    [
        # devices advertising the AirPlay 2 feature bits get AirPlay 2
        ({"features": AP2_FEATURES}, {}, StreamingProtocol.AIRPLAY2),
        # the _raop ft field is used as fallback when _airplay lacks features
        ({}, {"ft": "0x445F8A00,0x1C340"}, StreamingProtocol.AIRPLAY2),
        # legacy receivers without the AirPlay 2 feature bits stay on RAOP
        ({"features": "0x5A7FFFF7"}, {}, StreamingProtocol.RAOP),
        # no features advertised at all: RAOP (safe legacy default)
        ({}, {}, StreamingProtocol.RAOP),
    ],
)
def test_protocol_resolution_follows_capability(
    airplay_props: dict[str, str], raop_props: dict[str, str], expected: StreamingProtocol
) -> None:
    """Without the force toggle, protocol resolution follows the advertised AirPlay 2 bits."""
    raop_info = MagicMock()
    raop_info.decoded_properties = raop_props
    airplay_info = MagicMock()
    airplay_info.decoded_properties = airplay_props
    player = AirPlayPlayer(
        provider=MagicMock(),
        player_id="test_player",
        display_name="Test Player",
        address="127.0.0.1",
        manufacturer="Test Manufacturer",
        model="Test Model",
        raop_discovery_info=raop_info,
        airplay_discovery_info=airplay_info,
    )
    _configure_player(player, {CONF_STREAMING_MODE: STREAMING_MODE_AUTO})
    assert player.protocol == expected


def test_protocol_resolution_airplay_service_only() -> None:
    """A device advertising only the _airplay service is AirPlay 2 even without features."""
    airplay_info = MagicMock()
    airplay_info.decoded_properties = {}
    player = AirPlayPlayer(
        provider=MagicMock(),
        player_id="test_player",
        display_name="Test Player",
        address="127.0.0.1",
        manufacturer="Test Manufacturer",
        model="Test Model",
        raop_discovery_info=None,
        airplay_discovery_info=airplay_info,
    )
    _configure_player(player, {CONF_STREAMING_MODE: STREAMING_MODE_AUTO})
    assert player.protocol == StreamingProtocol.AIRPLAY2


def test_raop_mode_resolves_to_raop_on_non_apple_airplay2(airplay_player: AirPlayPlayer) -> None:
    """The RAOP mode on an eligible device forces RAOP for both resolution and stream args."""
    _set_discovery_info(airplay_player, raop=True, airplay=True, airplay_features=AP2_FEATURES)
    _configure_player(airplay_player, {CONF_STREAMING_MODE: STREAMING_MODE_RAOP})
    assert airplay_player.protocol == StreamingProtocol.RAOP
    assert airplay_player.protocol_override == StreamingProtocol.RAOP


def test_raop_mode_applies_on_apple_with_raop_service() -> None:
    """The RAOP escape hatch works on an Apple device that advertises _raop."""
    player = _make_apple_player()
    _set_discovery_info(player, raop=True, airplay=True, airplay_features=AP2_FEATURES)
    _configure_player(player, {CONF_STREAMING_MODE: STREAMING_MODE_RAOP})
    assert player.protocol == StreamingProtocol.RAOP
    assert player.protocol_override == StreamingProtocol.RAOP


def test_ntp_mode_ignored_on_apple_airplay2() -> None:
    """A stray persisted NTP mode is ignored on Apple devices (the lane is never offered)."""
    player = _make_apple_player()
    _set_discovery_info(player, raop=True, airplay=True, airplay_features=AP2_FEATURES)
    _configure_player(player, {CONF_STREAMING_MODE: STREAMING_MODE_AP2_NTP})
    assert player.streaming_mode == STREAMING_MODE_AUTO


@pytest.mark.parametrize(
    ("stored_config", "expected"),
    [
        # no credentials at all: pairing is required before the player is usable
        ({}, True),
        # a legacy RAOP pairing keeps the player usable after the device
        # resolves to AirPlay 2 (the binary streams RAOP-compat with the secret)
        ({CONF_RAOP_CREDENTIALS: "clientid:secret"}, False),
        # AirPlay 2 credentials obviously suffice as well
        ({CONF_AIRPLAY_CREDENTIALS: "a" * 192}, False),
    ],
)
def test_needs_setup_accepts_credentials_for_either_protocol(
    airplay_player: AirPlayPlayer, stored_config: dict[str, str], expected: bool
) -> None:
    """A PIN-pairing device needs setup only when no credentials are stored at all."""
    # PIN-required device that resolves to AirPlay 2 (Apple TV-like)
    airplay_info = MagicMock()
    airplay_info.properties = {b"flags": b"0x8"}
    airplay_info.decoded_properties = {"features": "0x4A7FDFD5,0x3C177FDE"}
    airplay_player.airplay_discovery_info = airplay_info
    # credentials now live in the player's setup_data, read via get_setup_value
    airplay_player.get_setup_value = (  # type: ignore[method-assign]
        lambda key, default=None: stored_config.get(key, default)
    )
    assert airplay_player.needs_setup is expected


# --- Hi-res playback tests ---


def _configure_player(player: AirPlayPlayer, values: dict[str, object]) -> None:
    """Stub the player config to return the given values."""
    player.config.get_value.side_effect = (  # type: ignore[attr-defined]
        lambda key, default=None: values.get(key, default)
    )


@pytest.mark.parametrize(
    ("advertised_audio_formats", "streaming_mode", "airplay2_capable", "expected"),
    [
        # 24-bit advertised on the realtime stream
        (ALAC_44100_24, STREAMING_MODE_AUTO, True, [(44100, 24), (48000, 24)]),
        # the Apple TV advertises 24-bit for its buffered stream only
        (ALAC_48000_24, STREAMING_MODE_AUTO, True, [(44100, 24), (48000, 24)]),
        # the RAOP mode cannot do 24-bit: falls back to the 16-bit base
        (ALAC_44100_24, STREAMING_MODE_RAOP, True, [(44100, 16)]),
        # the compatibility mode streams through the 16-bit RAOP flow
        (ALAC_44100_24, STREAMING_MODE_AP2_COMPAT, True, [(44100, 16)]),
        # a receiver that streams RAOP never gets 24-bit, whatever it advertises
        (ALAC_44100_24, STREAMING_MODE_AUTO, False, [(44100, 16)]),
        # only 16-bit advertised: the 16-bit default
        (ALAC_44100_16, STREAMING_MODE_AUTO, True, [(44100, 16)]),
        # nothing advertised (unreachable device or no format tables)
        (0, STREAMING_MODE_AUTO, True, [(44100, 16)]),
    ],
)
def test_hires_supported_sample_rates(
    airplay_player: AirPlayPlayer,
    advertised_audio_formats: int,
    streaming_mode: str,
    airplay2_capable: bool,
    expected: list[tuple[int, int]],
) -> None:
    """The formats the device advertises drive the advertised sample rates."""
    _set_discovery_info(
        airplay_player,
        raop=True,
        airplay=True,
        airplay_features=AP2_FEATURES if airplay2_capable else None,
    )
    airplay_player.advertised_audio_formats = advertised_audio_formats
    _configure_player(airplay_player, {CONF_STREAMING_MODE: streaming_mode})
    assert airplay_player.supported_sample_rates == expected


def test_hires_disabled_in_compatibility_mode(airplay_player: AirPlayPlayer) -> None:
    """A hi-res device pinned to compatibility mode drops back to the 16-bit base."""
    _set_discovery_info(airplay_player, raop=True, airplay=True, airplay_features=AP2_FEATURES)
    airplay_player.advertised_audio_formats = ALAC_44100_24
    _configure_player(airplay_player, {CONF_STREAMING_MODE: STREAMING_MODE_AP2_COMPAT})

    # the compat lanes keep reporting AirPlay 2, so the protocol alone cannot gate hi-res
    assert airplay_player.protocol == StreamingProtocol.AIRPLAY2
    assert airplay_player.hires_playback_enabled is False
    assert airplay_player.supported_sample_rates == [(44100, 16)]

    session_format = AudioFormat(
        content_type=ContentType.PCM_F32LE, sample_rate=48000, bit_depth=32
    )
    assert airplay_player.get_stream_pcm_format(session_format) == AIRPLAY_PCM_FORMAT


def test_get_stream_pcm_format_hires(airplay_player: AirPlayPlayer) -> None:
    """For a 24-bit capable device the stream format is 24-bit in a s32le container."""
    _set_discovery_info(airplay_player, raop=True, airplay=True, airplay_features=AP2_FEATURES)
    airplay_player.advertised_audio_formats = ALAC_44100_24
    _configure_player(airplay_player, {CONF_STREAMING_MODE: STREAMING_MODE_AUTO})

    session_format = AudioFormat(
        content_type=ContentType.PCM_F32LE, sample_rate=48000, bit_depth=32
    )
    stream_format = airplay_player.get_stream_pcm_format(session_format)
    # the binary expects raw s32le on stdin for --bitdepth 24
    assert stream_format.content_type == ContentType.PCM_S32LE
    assert stream_format.sample_rate == 48000
    assert stream_format.bit_depth == 24

    # an unsupported session rate falls back to the 44.1 kHz base
    session_format = AudioFormat(
        content_type=ContentType.PCM_F32LE, sample_rate=96000, bit_depth=32
    )
    stream_format = airplay_player.get_stream_pcm_format(session_format)
    assert stream_format.sample_rate == 44100
    assert stream_format.bit_depth == 24


def test_get_stream_pcm_format_default(airplay_player: AirPlayPlayer) -> None:
    """Without a 24-bit capable device the stream format is the 44.1/16 default."""
    _set_discovery_info(airplay_player, raop=True, airplay=True)
    _configure_player(airplay_player, {CONF_STREAMING_MODE: STREAMING_MODE_AUTO})
    session_format = AudioFormat(
        content_type=ContentType.PCM_F32LE, sample_rate=48000, bit_depth=32
    )
    assert airplay_player.get_stream_pcm_format(session_format) == AIRPLAY_PCM_FORMAT


@pytest.mark.asyncio
async def test_session_pcm_format_selection(airplay_player: AirPlayPlayer) -> None:
    """AirPlay delegates the complete session format decision to the shared selector."""
    selected_format = AudioFormat(
        content_type=ContentType.PCM_S24LE,
        sample_rate=48000,
        bit_depth=24,
    )
    streams_audio = cast("MagicMock", airplay_player.mass.streams.audio)
    streams_audio.select_flow_pcm_format = AsyncMock(return_value=selected_format)
    cast("MagicMock", airplay_player.mass.player_queues.get).return_value = None
    media = MagicMock()
    media.source_id = "queue1"
    media.queue_item_id = "item1"
    queue_item = MagicMock()
    queue_item.streamdetails.audio_format.sample_rate = 48000
    airplay_player.mass.player_queues.get_item.return_value = queue_item  # type: ignore[attr-defined]
    sync_clients = [airplay_player, airplay_player]

    fmt = await airplay_player._get_session_pcm_format(sync_clients, media)

    assert fmt is selected_format
    streams_audio.select_flow_pcm_format.assert_awaited_once_with(
        airplay_player,
        start_streamdetails=queue_item.streamdetails,
        crossfade_enabled=False,
        overlay_active=False,
        fallback_sample_rate=AIRPLAY_PCM_FORMAT.sample_rate,
        output_players=sync_clients,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("normalization_mode", "expected_content_type", "expected_bit_depth"),
    [
        (VolumeNormalizationMode.DISABLED, ContentType.PCM_S24LE, 24),
        (VolumeNormalizationMode.MEASUREMENT_ONLY, ContentType.PCM_F32LE, 32),
    ],
)
async def test_session_pcm_format_selects_processing_depth(
    airplay_player: AirPlayPlayer,
    normalization_mode: VolumeNormalizationMode,
    expected_content_type: ContentType,
    expected_bit_depth: int,
) -> None:
    """An AirPlay session only uses float PCM when processing needs headroom."""
    _set_discovery_info(airplay_player, raop=True, airplay=True, airplay_features=AP2_FEATURES)
    airplay_player.advertised_audio_formats = ALAC_48000_24
    _configure_player(airplay_player, {CONF_STREAMING_MODE: STREAMING_MODE_AUTO})
    airplay_player.mass.streams.audio = StreamsAudio(airplay_player.mass)
    cast("MagicMock", airplay_player.mass.config.get_player_dsp_config).return_value = MagicMock(
        enabled=False
    )
    cast(
        "MagicMock", airplay_player.mass.streams.get_crossfade_mode
    ).return_value = CrossfadeMode.DISABLED

    streamdetails = MagicMock()
    streamdetails.audio_format = AudioFormat(
        content_type=ContentType.FLAC,
        sample_rate=48000,
        bit_depth=24,
    )
    streamdetails.media_type = MediaType.TRACK
    streamdetails.volume_normalization_mode = normalization_mode
    queue_item = MagicMock(streamdetails=streamdetails)
    queue = MagicMock(crossfade_enabled=False, overlay_enabled=False, overlay_source=None)
    cast("MagicMock", airplay_player.mass.player_queues.get).return_value = queue
    cast("MagicMock", airplay_player.mass.player_queues.get_item).return_value = queue_item
    media = MagicMock(source_id="queue1", queue_item_id="item1", media_type=MediaType.TRACK)

    fmt = await airplay_player._get_session_pcm_format([airplay_player], media)

    assert fmt.content_type == expected_content_type
    assert fmt.sample_rate == 48000
    assert fmt.bit_depth == expected_bit_depth


@pytest.mark.asyncio
async def test_config_entries_include_ignore_volume(airplay_player: AirPlayPlayer) -> None:
    """The ignore_volume setting must be offered in the player config entries."""
    entries = await airplay_player.get_config_entries()
    assert any(entry.key == CONF_IGNORE_VOLUME for entry in entries)


@pytest.mark.parametrize(
    ("manufacturer", "model", "expected"),
    [
        # AirPlay 2 preferred models get AirPlay 2, even when RAOP is advertised
        ("AirPlay", "JBL BAR 1300", StreamingProtocol.AIRPLAY2),
        ("AirPlay", "JBL Charge 5 Wi-Fi", StreamingProtocol.AIRPLAY2),
        # other devices advertising both protocols default to RAOP
        ("Test Manufacturer", "Test Model", StreamingProtocol.RAOP),
    ],
)
def test_auto_protocol_selection(
    manufacturer: str, model: str, expected: StreamingProtocol
) -> None:
    """Automatic protocol selection prefers AirPlay 2 for known AP2-preferred models."""
    player = AirPlayPlayer(
        provider=MagicMock(),
        player_id="test_player",
        display_name="Test Player",
        address="127.0.0.1",
        manufacturer=manufacturer,
        model=model,
        raop_discovery_info=MagicMock(),
        airplay_discovery_info=MagicMock(),
    )
    assert player._get_protocol_for_config_value(0) == expected


# --- Volume and Mute tests ---


def _setup_running_stream(player: AirPlayPlayer) -> AsyncMock:
    """Attach a mock running stream to the player and return the send_cli_command mock."""
    stream = MagicMock()
    stream.running = True
    # every streaming player has a session; this one is playing, not parked
    stream.session = MagicMock(parked=False)
    send_cmd = AsyncMock()
    stream.send_cli_command = send_cmd
    player.stream = stream
    return send_cmd


@pytest.mark.asyncio
async def test_volume_mute_sends_zero(airplay_player: AirPlayPlayer) -> None:
    """Muting with a running stream should send VOLUME=0."""
    send_cmd = _setup_running_stream(airplay_player)
    airplay_player._attr_volume_level = 75

    await airplay_player.volume_mute(True)

    send_cmd.assert_called_once_with("VOLUME=0")
    assert airplay_player._attr_volume_muted is True


@pytest.mark.asyncio
async def test_volume_set_skipped_while_muted(airplay_player: AirPlayPlayer) -> None:
    """Volume changes while muted should NOT send a CLI command."""
    send_cmd = _setup_running_stream(airplay_player)
    airplay_player._attr_volume_muted = True

    await airplay_player.volume_set(60)

    send_cmd.assert_not_called()
    assert airplay_player._attr_volume_level == 60


@pytest.mark.asyncio
async def test_volume_set_records_level_before_sending(airplay_player: AirPlayPlayer) -> None:
    """A resync reading the level mid-send must observe the new volume, not the old one."""
    send_cmd = _setup_running_stream(airplay_player)
    airplay_player._attr_volume_level = 20
    observed: list[int | None] = []

    async def read_level_while_sending(_command: str) -> bool:
        # stands in for the connect-time volume resync, which reads the player's
        # level while this send is still suspended
        await asyncio.sleep(0)
        observed.append(airplay_player.volume_level)
        return True

    send_cmd.side_effect = read_level_while_sending

    await airplay_player.volume_set(80)

    assert observed == [80]
    assert airplay_player.volume_level == 80


@pytest.mark.asyncio
async def test_volume_set_records_level_when_the_send_fails(
    airplay_player: AirPlayPlayer,
) -> None:
    """A dropped command must not lose the requested level; the resync repairs the device."""
    send_cmd = _setup_running_stream(airplay_player)
    send_cmd.return_value = False
    airplay_player._attr_volume_level = 20

    await airplay_player.volume_set(80)

    send_cmd.assert_awaited_once_with("VOLUME=80")
    assert airplay_player.volume_level == 80


@pytest.mark.asyncio
async def test_volume_unmute_restores_volume(airplay_player: AirPlayPlayer) -> None:
    """Unmuting with a running stream should send VOLUME={current_volume}."""
    send_cmd = _setup_running_stream(airplay_player)
    airplay_player._attr_volume_level = 42
    airplay_player._attr_volume_muted = True

    await airplay_player.volume_mute(False)

    send_cmd.assert_called_once_with("VOLUME=42")
    assert airplay_player._attr_volume_muted is False


@pytest.mark.asyncio
async def test_volume_mute_no_stream(airplay_player: AirPlayPlayer) -> None:
    """Muting without a running stream should update state without CLI commands."""
    airplay_player.stream = None

    with patch.object(AirPlayPlayer, "update_state") as mock_update:
        await airplay_player.volume_mute(True)

        assert airplay_player._attr_volume_muted is True
        mock_update.assert_called_once()


def test_owns_volume_true_without_protocol_parent(airplay_player: AirPlayPlayer) -> None:
    """A standalone AirPlay player always owns its own volume."""
    assert airplay_player.owns_volume is True


def test_owns_volume_true_when_parent_unresolvable(airplay_player: AirPlayPlayer) -> None:
    """A protocol parent that no longer resolves cannot own the volume either."""
    airplay_player.mass.players.get_player.return_value = None  # type: ignore[attr-defined]
    airplay_player.set_protocol_parent_id("parent")

    assert airplay_player.owns_volume is True


def test_owns_volume_true_when_parent_control_is_self(airplay_player: AirPlayPlayer) -> None:
    """This output owns the volume when the parent's control resolves to it directly."""
    parent = MagicMock()
    parent.volume_control_for_output.return_value = "test_player"
    airplay_player.mass.players.get_player.return_value = parent  # type: ignore[attr-defined]
    airplay_player.set_protocol_parent_id("parent")

    assert airplay_player.owns_volume is True
    # the control must be resolved against this player as the rendering output
    parent.volume_control_for_output.assert_called_once_with(airplay_player.player_id)


def test_owns_volume_true_when_parent_control_is_bridge_on_self(
    airplay_player: AirPlayPlayer,
) -> None:
    """This output owns the volume when the control is a bridge riding on it."""
    parent = MagicMock()
    parent.volume_control_for_output.return_value = "sendspin_bridge"
    bridge = MagicMock()
    bridge.underlying_player_id = "test_player"
    airplay_player.mass.players.get_player.side_effect = {  # type: ignore[attr-defined]
        "parent": parent,
        "sendspin_bridge": bridge,
    }.get
    airplay_player.set_protocol_parent_id("parent")

    assert airplay_player.owns_volume is True


@pytest.mark.parametrize("control", ["dlna_player", PLAYER_CONTROL_NATIVE])
def test_owns_volume_false_when_another_control_owns_it(
    airplay_player: AirPlayPlayer, control: str
) -> None:
    """Another control (a sibling interface, or the receiver's own native control) owns it."""
    parent = MagicMock()
    parent.volume_control_for_output.return_value = control
    airplay_player.mass.players.get_player.side_effect = {  # type: ignore[attr-defined]
        "parent": parent,
    }.get
    airplay_player.set_protocol_parent_id("parent")

    assert airplay_player.owns_volume is False


def test_update_volume_from_device_keeps_native_parent_feedback(
    airplay_player: AirPlayPlayer,
) -> None:
    """Use DACP feedback to keep the child AirPlay volume current."""
    parent = MagicMock()
    parent.state.volume_level = 42
    parent.volume_control = PLAYER_CONTROL_NATIVE
    airplay_player.mass.players.get_player.return_value = parent  # type: ignore[attr-defined]
    airplay_player.config.get_value.return_value = False  # type: ignore[attr-defined]
    airplay_player.set_protocol_parent_id("parent")
    airplay_player._attr_volume_level = 57
    airplay_player.last_command_sent = time.time()

    with patch.object(AirPlayPlayer, "update_state") as mock_update:
        airplay_player.update_volume_from_device(57)

    assert airplay_player._attr_volume_level == 57
    airplay_player.mass.config.set_raw_player_config_value.assert_called_once_with(  # type: ignore[attr-defined]
        airplay_player.player_id, CONF_STORED_VOLUME, 57
    )
    mock_update.assert_called_once()


def test_sync_volume_level_uses_parent_volume_without_native_parent(
    airplay_player: AirPlayPlayer,
) -> None:
    """Keep existing behavior for protocol parents without native volume control."""
    parent = MagicMock()
    parent.state.volume_level = 42
    parent.volume_control = None
    airplay_player.mass.players.get_player.return_value = parent  # type: ignore[attr-defined]
    airplay_player.set_protocol_parent_id("parent")
    airplay_player._attr_volume_level = 48

    with patch.object(AirPlayPlayer, "update_state") as mock_update:
        airplay_player.sync_volume_level()

    assert airplay_player._attr_volume_level == 42
    airplay_player.mass.config.set_raw_player_config_value.assert_called_once_with(  # type: ignore[attr-defined]
        airplay_player.player_id,
        CONF_STORED_VOLUME,
        42,
    )
    mock_update.assert_called_once()


def test_sync_volume_level_ignores_parent_volume_zero(
    airplay_player: AirPlayPlayer,
) -> None:
    """
    Clear our mute when it is owned by a control that doesn't render this stream.

    The mute is a latch that only an explicit unmute clears, so a mute applied while
    a sibling interface owned the parent would otherwise start this stream silent and
    swallow every volume command after it.
    """
    parent = MagicMock()
    parent.state.volume_level = 0
    parent.volume_control = None
    airplay_player.mass.players.get_player.return_value = parent  # type: ignore[attr-defined]
    airplay_player.set_protocol_parent_id("parent")
    airplay_player._attr_volume_muted = True

    with patch.object(AirPlayPlayer, "update_state") as mock_update:
        airplay_player.release_foreign_mute_latch()

    assert airplay_player._attr_volume_muted is False
    mock_update.assert_called_once()
    # the control must be resolved against this player as the rendering output
    parent.mute_control_for_output.assert_called_once_with(airplay_player.player_id)


def test_release_foreign_mute_latch_keeps_mute_owned_by_this_output(
    airplay_player: AirPlayPlayer,
) -> None:
    """Keep our own mute when this player owns the parent's mute."""
    parent = MagicMock()
    parent.mute_control_for_output.return_value = "test_player"
    airplay_player.mass.players.get_player.return_value = parent  # type: ignore[attr-defined]
    airplay_player.set_protocol_parent_id("parent")
    airplay_player._attr_volume_muted = True

    with patch.object(AirPlayPlayer, "update_state") as mock_update:
        airplay_player.release_foreign_mute_latch()

    assert airplay_player._attr_volume_muted is True
    mock_update.assert_not_called()


def test_release_foreign_mute_latch_does_nothing_when_not_muted(
    airplay_player: AirPlayPlayer,
) -> None:
    """Never having latched a mute is not something to act on."""
    parent = MagicMock()
    airplay_player.mass.players.get_player.return_value = parent  # type: ignore[attr-defined]
    airplay_player.set_protocol_parent_id("parent")
    airplay_player._attr_volume_muted = False

    with patch.object(AirPlayPlayer, "update_state") as mock_update:
        airplay_player.release_foreign_mute_latch()

    assert airplay_player._attr_volume_muted is False
    parent.mute_control_for_output.assert_not_called()
    mock_update.assert_not_called()
