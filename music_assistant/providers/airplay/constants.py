"""Constants for the AirPlay provider."""

from __future__ import annotations

from dataclasses import replace
from enum import IntEnum, StrEnum
from typing import Final

from music_assistant_models.enums import ContentType, PlayerFeature
from music_assistant_models.media_items import AudioFormat

from music_assistant.constants import CONF_ENTRY_SYNC_ADJUST
from music_assistant.controllers.streams.constants import SEEK_WAIT_THRESHOLD

DOMAIN = "airplay"


class StreamingProtocol(IntEnum):
    """AirPlay streaming protocol versions."""

    RAOP = 1  # AirPlay 1 (RAOP)
    AIRPLAY2 = 2  # AirPlay 2


class AirPlayRemoteCommand(StrEnum):
    """Transport commands received from an AirPlay receiver."""

    PLAY = "play"
    PAUSE = "pause"
    PLAY_PAUSE = "play_pause"
    NEXT = "next"
    PREVIOUS = "previous"


class ClockReadiness(StrEnum):
    """
    How a receiver's clock readiness resolved for an anchor decision.

    Only PROJECTED carries an instant; the rest all mean "anchor on the lead
    alone", but for very different reasons - one is a device that will not play
    at all, and treating them alike hides it.
    """

    # The binary projected when the receiver's clock becomes usable.
    PROJECTED = "projected"
    # NTP timing: there is no receiver clock to wait for.
    NOT_APPLICABLE = "not_applicable"
    # The receiver never answered our PTP clock and will render silence.
    STALLED = "stalled"
    # Nothing arrived within the wait: a slow device (retryable) or a receiver
    # whose readiness went unreported.
    UNREPORTED = "unreported"


CONF_PASSWORD: Final[str] = "password"
# Storage-only marker (no config entry) set when the device rejected the stored
# password, so the player keeps asking for setup across restarts until a working
# password is entered.
CONF_PASSWORD_INVALID: Final[str] = "password_invalid"
# Provider marker that the stored password verdicts were reviewed once. Releases
# that could not tell a password challenge apart from a flat refusal wrote the
# key above for both, so what they left behind is no evidence about a password
# and is dropped a single time; a device that really challenges marks itself
# again on its next connect.
CONF_PASSWORD_MARKERS_REVIEWED: Final[str] = "password_markers_reviewed"
CONF_IGNORE_VOLUME: Final[str] = "ignore_volume"
CONF_ENCRYPTION: Final[str] = "encryption"
# Advanced per-device streaming mode: pins the protocol/timing lane for
# receivers whose automatic route misbehaves. Options are offered per device
# capability; Automatic is the default and the setting is only ever written by
# the user — a failing automatic route is reported, never switched away from.
CONF_STREAMING_MODE: Final[str] = "streaming_mode"
# Per-device 24-bit toggle, only offered for devices that advertise 24-bit
# support. Defaults per device family (see default_hires_enabled).
CONF_ENABLE_HIRES: Final[str] = "enable_hires"
# Provider marker that the compatibility-mode pins were reset once. Earlier
# releases switched a player here themselves when its native control channel
# failed (usually a network dropout), pinning it to a lane many devices reject
# outright, so those machine-written values are returned to Automatic a single
# time; a deliberate choice can simply be made again.
CONF_COMPAT_PINS_REVIEWED: Final[str] = "compat_pins_reviewed"
STREAMING_MODE_AUTO: Final[str] = "auto"
STREAMING_MODE_AP2_PTP: Final[str] = "ap2_ptp"
STREAMING_MODE_AP2_NTP: Final[str] = "ap2_ntp"
STREAMING_MODE_AP2_COMPAT: Final[str] = "ap2_compat"
STREAMING_MODE_RAOP: Final[str] = "raop"
CONF_STORED_VOLUME: Final[str] = "stored_volume"
CONF_COMPANION_CREDENTIALS: Final[str] = "companion_credentials"
CONF_MRP_CREDENTIALS: Final[str] = "mrp_credentials"
CONF_NATIVE_MRP_CREDENTIALS: Final[str] = "native_mrp_credentials"

# Bundle id of the Music Assistant tvOS dashboard app, launched over Companion on
# eligible Apple TVs (see tvos/docs/launch-contract.md).
TVOS_APP_BUNDLE_ID: Final[str] = "io.music-assistant.tvos"

AIRPLAY_DISCOVERY_TYPE: Final[str] = "_airplay._tcp.local."
COMPANION_DISCOVERY_TYPE: Final[str] = "_companion-link._tcp.local."
MRP_DISCOVERY_TYPE: Final[str] = "_mediaremotetv._tcp.local."
RAOP_DISCOVERY_TYPE: Final[str] = "_raop._tcp.local."
DACP_DISCOVERY_TYPE: Final[str] = "_dacp._tcp.local."

AIRPLAY_OUTPUT_BUFFER_DURATION_MS: Final[int] = (
    2000  # Read ahead buffer for cliraop. Output buffer duration for cliap2.
)
AIRPLAY2_MIN_LOG_LEVEL: Final[int] = 3  # Min loglevel to ensure stderr output contains what we need
AIRPLAY2_CONNECT_TIME_MS: Final[int] = 2500  # Time in ms to allow AirPlay2 device to connect
RAOP_CONNECT_TIME_MS: Final[int] = 1000  # Time in ms to allow RAOP device to connect

# Per-protocol credential storage keys
CONF_RAOP_CREDENTIALS: Final[str] = "raop_credentials"
CONF_AIRPLAY_CREDENTIALS: Final[str] = "airplay_credentials"

# Legacy credential key (for migration)
CONF_AP_CREDENTIALS: Final[str] = "ap_credentials"

# Pairing action keys
CONF_ACTION_START_PAIRING: Final[str] = "start_pairing"
CONF_ACTION_FINISH_PAIRING: Final[str] = "finish_pairing"
CONF_ACTION_RESET_PAIRING: Final[str] = "reset_pairing"
CONF_PAIRING_PIN: Final[str] = "pairing_pin"
CONF_ENABLE_LATE_JOIN: Final[str] = "enable_late_join"

BACKOFF_TIME_LOWER_LIMIT: Final[int] = 15  # seconds
BACKOFF_TIME_UPPER_LIMIT: Final[int] = 300  # Five minutes

FALLBACK_VOLUME: Final[int] = 20
AIRPLAY_VOLUME_MUTE: Final[float] = -144.0
# How long a volume we sent ourselves keeps the device's own volume reports from
# being acted on. A receiver echoes every level it is given back over DACP, and
# an echo that arrives after the next level was already sent would otherwise be
# read as the user turning the knob and written straight back to the device.
AIRPLAY_VOLUME_ECHO_GRACE_S: Final[float] = 2.0

AIRPLAY_PCM_FORMAT = AudioFormat(
    content_type=ContentType.from_bit_depth(16), sample_rate=44100, bit_depth=16
)
# Sample rates advertised for a receiver that supports 24-bit (AirPlay 2 flow
# only). At 24-bit the cliairplay binary expects raw s32le on stdin and truncates
# to 24-bit ALAC internally.
AIRPLAY_HIRES_SAMPLE_RATES: Final[list[tuple[int, int]]] = [(44100, 24), (48000, 24)]

BROKEN_AIRPLAY_MODELS = (
    # Samsung has been repeatedly being reported as having issues with AirPlay (raop and AP2)
    # Samsung will work with AirPlay2 once PTP timing is implemented for the MA build
    ("Samsung", "*"),
)

AIRPLAY_2_DEFAULT_MODELS = (
    # Models that are known to work better with AirPlay 2 protocol instead of RAOP
    # These use the translated/friendly model names from get_model_info()
    # Both fields support fnmatch-style wildcards and match case-insensitively.
    ("Ubiquiti Inc.", "*"),
)

BROKEN_AIRPLAY_WARN = ConfigEntry(
    key="BROKEN_AIRPLAY",
    type=ConfigEntryType.ALERT,
    default_value=None,
    required=False,
    label="This player is known to have broken AirPlay support. "
    "Playback may fail or simply be silent. "
    "There is no workaround for this issue at the moment. \n"
    "If you already enforced AirPlay 2 on the player and it remains silent, "
    "this is one of the known broken models. Only remedy is to nag the manufacturer for a fix.",
)

BASE_PLAYER_FEATURES: Final[set[PlayerFeature]] = {
    PlayerFeature.PLAY_MEDIA,
    PlayerFeature.PLAY_ANNOUNCEMENT,
    PlayerFeature.SET_MEMBERS,
    PlayerFeature.MULTI_DEVICE_DSP,
    PlayerFeature.VOLUME_SET,
    PlayerFeature.VOLUME_MUTE,
}


PIN_REQUIRED = 0x8
PASSWORD_BIT = 0x80
LEGACY_PAIRING_BIT = 0x200

# Provider setting: opt-in for the shared PTP daemon's per-packet timing trace
# (Announce/Sync/Follow_Up) when verbose logging is active. Off by default —
# the trace floods the log and only matters for clock-sync debugging.
CONF_VERBOSE_PTP_LOGGING: Final[str] = "verbose_ptp_logging"

# The cliairplay binary tags no log levels on its output, so a genuine problem is
# recognised by keyword and promoted to a warning that stays visible at normal levels.
CLI_PROBLEM_MARKERS: Final[tuple[str, ...]] = ("error", "cannot", "failed", "unable")
# Bound on how many of those promoted lines the shared PTP daemon may produce per
# window before the rest are counted instead of logged. The markers above are
# deliberately broad, and "error" is ordinary vocabulary in clock telemetry
# (offset error, path delay error), so with the daemon's per-packet trace running
# at ~10 lines/s a single matching line would otherwise fill the log at WARNING.
# A burst still gets through, which is what a real one-shot daemon failure is.
PTP_DAEMON_WARN_BURST: Final[int] = 5
PTP_DAEMON_WARN_WINDOW: Final[float] = 60.0
