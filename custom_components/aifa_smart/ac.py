"""AIFA Smart air-conditioner packet helpers."""
from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import StrEnum
from typing import Final

_LOGGER = logging.getLogger(__name__)

# Track which cloud-code device IDs we've already warned about to avoid log spam.
_CLOUD_CODE_WARNED: set[int] = set()


from . import catalog

AIFA_AC_ESTIMATED_CAPABILITIES: Final[tuple[str, ...]] = (
    "power",
    "temperature",
    "cool",
    "dry",
    "fan_only",
    "heat",
    "auto",
    "direction",
    "speed",
    "turbo",
    "power_saving",
    "sleep",
)
# Range of the app's setpoint slider.
AIFA_AC_MIN_TEMP = 16
AIFA_AC_MAX_TEMP = 30
AIFA_AC_DEFAULT_TARGET_TEMP = 24
AIFA_AC_DEFAULT_FAN_MODE = "high"
AIFA_AC_DEFAULT_SWING_MODE = "fixed_5"
CLASSIC_AC_QUERY_PACKET = "fff3f2f0"
CLASSIC_WAKE_REPLY_PACKET = "ccdd"

POWER_ON_COMMAND = "ffa0a1f0"
# Verified from the saved cloud function named "你好" on sub-device 31867.
POWER_OFF_COMMAND = "ffa0a2f0"

_STATUS_PACKET_PREFIX = 0x06
_OBSERVED_STATUS_PACKET_LENGTH = 14
_SUPPORTED_SUB_DEVICE_TYPE = "0"
_DEFAULT_AVAILABLE_MODES: Final[tuple[str, ...]] = ("auto", "cool", "heat", "fan", "dry")
_DEFAULT_FAN_MODES: Final[tuple[str, ...]] = ("auto", "low", "medium", "high")
_DEFAULT_SWING_MODES: Final[tuple[str, ...]] = (
    "off",
    "auto",
    "fixed_1",
    "fixed_2",
    "fixed_3",
    "fixed_4",
    "fixed_5",
)

# Byte tables of the AIFA Smart app's AirConditionerStatusOn.toBytes / fromBytes.
_MODE_BYTE_BY_NAME: Final[dict[str, int]] = {
    "auto": 0x10,
    "cool": 0x20,
    "dry": 0x40,
    "fan_only": 0x80,
    "heat": 0x08,
}
# App speed 0..3.
_FAN_BYTE_BY_NAME: Final[dict[str, int]] = {
    "auto": 0x01,
    "low": 0x20,
    "medium": 0x40,
    "high": 0x80,
}
# App direction off, auto, one..five.
_SWING_BYTE_BY_NAME: Final[dict[str, int]] = {
    "off": 0x00,
    "auto": 0x01,
    "fixed_1": 0x04,
    "fixed_2": 0x08,
    "fixed_3": 0x80,
    "fixed_4": 0x40,
    "fixed_5": 0x20,
}

# i-Ctrl hubs on this firmware mis-read the mode byte of the 15-byte status
# packet, so the AIFA Smart app sends a single mode key instead. The match is on
# the hub's own version/subversion, not on the AC device code.
MODE_KEY_HUB_VERSION: Final = 5
MODE_KEY_HUB_SUBVERSION: Final = 210623
# Mode key values as the app sends them.
_MODE_KEY_BY_NAME: Final[dict[str, int]] = {
    "auto": 0x15,
    "cool": 0x16,
    "heat": 0x17,
    "dry": 0x18,
    "fan_only": 0x19,
}
_TEMPERATURE_UP_KEY = 0x06
_TEMPERATURE_DOWN_KEY = 0x07
# The AIFA Smart app shows the +/- keys instead of the setpoint slider only on these
# hub versions (subversion is not checked).
CLASSIC_TEMP_KEY_HUB_VERSIONS: Final[frozenset[int]] = frozenset({0, 5, 10})
# Hard-coded in the AIFA Smart app rather than read from Remote Config.
WIND_SPEED_PLUS_MINUS_CODES: Final[frozenset[str]] = frozenset({"19866"})
# Hub DTO types (ProPlus, AcPlus) whose page carries the anti-mold block.
ANTI_MOLD_HUB_TYPES: Final[frozenset[str]] = frozenset({"6", "8"})

FAN_SPEED_KEY = 0x05
# The AIFA Smart app's key callback turns these two into its anti-mold packet.
_ANTI_MOLD_ON_KEY = 0x99
_ANTI_MOLD_OFF_KEY = 0x98
# keyPress byte of the 15-byte packet the app sends from its mode buttons and
# setpoint slider.
STATUS_KEY_PRESS_MODE = 0x08
STATUS_KEY_PRESS_TEMPERATURE_UP = 0x06
STATUS_KEY_PRESS_TEMPERATURE_DOWN = 0x07


@dataclass(frozen=True, slots=True)
class AifaAcKey:
    """One key of the AIFA Smart app AC page that sends `ff a0|slot KK f0`."""

    key: str
    code: int
    # The app greys the key itself out while the AC status is off.
    requires_on: bool
    # Keys in the bottom sheet, whose "^" button is greyed out while the AC
    # is off unless the code is single-mode.
    in_bottom_sheet: bool = True


AC_KEYS: Final[dict[str, AifaAcKey]] = {
    key.key: key
    for key in (
        AifaAcKey("timer", 0x10, requires_on=True),
        AifaAcKey("swing", 0x02, requires_on=True),
        AifaAcKey("fan_speed", FAN_SPEED_KEY, requires_on=True),
        AifaAcKey("power_saving", 0x0E, requires_on=False),
        AifaAcKey("sleep", 0x23, requires_on=True),
        AifaAcKey("turbo", 0x22, requires_on=True),
        AifaAcKey("quiet", 0x2A, requires_on=False),
        AifaAcKey("extra_wind_speed", 0x28, requires_on=False),
        AifaAcKey("display", 0x27, requires_on=False),
        AifaAcKey("mold", 0x26, requires_on=False),
        AifaAcKey("wind_speed_high", 0x38, requires_on=False),
        AifaAcKey("wind_speed_low", 0x36, requires_on=False),
        # The single-mode remote has one mode key that cycles through modes.
        AifaAcKey("mode", 0x08, requires_on=False, in_bottom_sheet=False),
        AifaAcKey("anti_mold_on", _ANTI_MOLD_ON_KEY, requires_on=True, in_bottom_sheet=False),
        AifaAcKey("anti_mold_off", _ANTI_MOLD_OFF_KEY, requires_on=True, in_bottom_sheet=False),
    )
}
@dataclass(frozen=True, slots=True)
class AifaAcCapabilities:
    """Capability matrix for a single AIFA air-conditioner device code."""

    device_code: str | None
    available_modes: tuple[str, ...]
    anti_mold_modes: tuple[str, ...]
    fan_modes: tuple[str, ...]
    swing_modes: tuple[str, ...]
    classic_temp_control: bool
    extra_windspeed: bool
    show_display_mold: bool
    single_mode: bool
    dehumidifier: bool
    supports_sleep: bool
    supports_power_saving: bool
    supports_turbo: bool


@dataclass(frozen=True, slots=True)
class AifaAcStatus:
    """Decoded status packet for a supported air-conditioner profile."""

    device_code: str | None
    mode: str
    target_temperature: int | None
    timer_ir_value: int
    fan_mode: str | None
    swing_mode: str | None
    turbo: bool
    sleep: bool
    power_saving: bool
    packet_byte_12: int
    packet_byte_13: int
    # b7 as the app shows it even when mode is off, where fan_mode stays None.
    reported_fan_mode: str | None = None


@dataclass(frozen=True, slots=True)
class AifaAcProfile:
    """Packet profile for the shared AIFA AC status family."""

    min_temp: int
    max_temp: int
    default_target_temp: int
    default_fan_mode: str
    default_swing_mode: str
    power_on_command: str
    power_off_command: str
    packet_prefix: int
    mode_byte_by_name: dict[str, int]
    fan_byte_by_name: dict[str, int]
    swing_byte_by_name: dict[str, int]


SHARED_AIFA_AC_PROFILE: Final[AifaAcProfile] = AifaAcProfile(
    min_temp=AIFA_AC_MIN_TEMP,
    max_temp=AIFA_AC_MAX_TEMP,
    default_target_temp=AIFA_AC_DEFAULT_TARGET_TEMP,
    default_fan_mode=AIFA_AC_DEFAULT_FAN_MODE,
    default_swing_mode=AIFA_AC_DEFAULT_SWING_MODE,
    power_on_command=POWER_ON_COMMAND,
    power_off_command=POWER_OFF_COMMAND,
    packet_prefix=_STATUS_PACKET_PREFIX,
    mode_byte_by_name=dict(_MODE_BYTE_BY_NAME),
    fan_byte_by_name=dict(_FAN_BYTE_BY_NAME),
    swing_byte_by_name=dict(_SWING_BYTE_BY_NAME),
)


def _normalize_device_code(device_code: str | None) -> str | None:
    """Normalize device-code values into non-empty strings."""
    if device_code is None:
        return None
    value = str(device_code).strip()
    return value or None


def normalize_ac_mode(mode: str) -> str:
    """Normalize a user-facing HVAC mode into the packet profile key."""
    normalized = mode.strip().lower()
    if normalized == "fan":
        return "fan_only"
    return normalized


def normalize_catalog_mode(mode: str) -> str:
    """Normalize catalog mode names to the runtime convention."""
    normalized = mode.strip().lower()
    if normalized == "fan_only":
        return "fan"
    return normalized


def get_ac_capabilities(device_code: str | None) -> AifaAcCapabilities:
    """Return remote-config derived capabilities for a device code."""
    cat = catalog.get_active()
    normalized_code = _normalize_device_code(device_code)
    # Remote config's ac_available_modes lists the modes anti-mold may run in,
    # not the AC's modes: the app always offers all five mode buttons.
    anti_mold_modes = cat.available_modes_by_device_code.get(normalized_code or "", ())

    supports_extra_windspeed = normalized_code in (
        cat.extra_windspeed_codes | cat.observed_extra_windspeed_codes
    )
    # Diagnostics only: which keys exist follows visible_ac_keys, as in the app.
    supports_turbo = True

    return AifaAcCapabilities(
        device_code=normalized_code,
        available_modes=tuple(
            normalize_catalog_mode(mode) for mode in _DEFAULT_AVAILABLE_MODES
        ),
        anti_mold_modes=tuple(normalize_catalog_mode(mode) for mode in anti_mold_modes),
        fan_modes=_DEFAULT_FAN_MODES,
        swing_modes=_DEFAULT_SWING_MODES,
        classic_temp_control=normalized_code in cat.classic_temp_control_codes,
        extra_windspeed=supports_extra_windspeed,
        show_display_mold=normalized_code in cat.show_display_mold_codes,
        single_mode=normalized_code in cat.single_mode_codes,
        dehumidifier=normalized_code in cat.dehumidifier_codes,
        # These fields are present in the shared AC editor and status model.
        supports_sleep=True,
        supports_power_saving=True,
        supports_turbo=supports_turbo,
    )


def build_estimated_capabilities(device_code: str | None) -> list[str]:
    """Build a deduplicated capability list for diagnostics."""
    capabilities = get_ac_capabilities(device_code)
    ordered: list[str] = ["power", "temperature"]
    ordered.extend(
        "fan_only" if mode == "fan" else mode for mode in capabilities.available_modes
    )
    ordered.extend(["direction", "speed"])
    if capabilities.supports_turbo:
        ordered.append("turbo")
    if capabilities.supports_sleep:
        ordered.append("sleep")
    if capabilities.supports_power_saving:
        ordered.append("power_saving")
    if capabilities.extra_windspeed:
        ordered.append("extra_windspeed")
    if capabilities.show_display_mold:
        ordered.append("anti_mold")
    if capabilities.classic_temp_control:
        ordered.append("classic_temp_control")
    if capabilities.single_mode:
        ordered.append("single_mode")
    if capabilities.dehumidifier:
        ordered.append("dehumidifier")

    deduplicated: list[str] = []
    seen: set[str] = set()
    for capability in ordered:
        if capability in seen:
            continue
        seen.add(capability)
        deduplicated.append(capability)
    return deduplicated


def get_ac_profile(device_code: str | None) -> AifaAcProfile | None:
    """Return the shared packet family when the device-code is supported."""
    return SHARED_AIFA_AC_PROFILE if is_supported_ac_device_code(device_code) else None


def hub_reports_ac_state(device_code: str | None) -> bool:
    """Return False for 5-digit cloud codes, whose state the hub never echoes."""
    try:
        return int(str(device_code).strip()) < 10000
    except (TypeError, ValueError):
        return True


def is_supported_ac_device_code(device_code: str | None) -> bool:
    """Return True when a device-code can use the shared AC packet family."""
    return _normalize_device_code(device_code) is not None


def is_supported_ac_sub_device(sub_device_type: str | None, device_code: str | None) -> bool:
    """Return True when a sub-device looks like a supported AIFA air conditioner."""
    return (sub_device_type or "").strip() == _SUPPORTED_SUB_DEVICE_TYPE and is_supported_ac_device_code(
        device_code
    )


def app_mode_buttons(device_code: str | None) -> tuple[str, ...]:
    """Modes the app offers as buttons; single-mode remotes only have the mode key."""
    capabilities = get_ac_capabilities(device_code)
    if capabilities.single_mode:
        return ()
    if capabilities.dehumidifier:
        return tuple(
            mode for mode in capabilities.available_modes if mode not in ("cool", "heat", "auto")
        )
    return capabilities.available_modes


def get_supported_ac_modes(device_code: str | None) -> tuple[str, ...]:
    """Return normalized catalog modes for a device-code."""
    return get_ac_capabilities(device_code).available_modes


def get_default_mode(device_code: str | None) -> str:
    """Return the preferred active mode when turning an AC on."""
    available_modes = get_supported_ac_modes(device_code)
    for candidate in ("cool", "auto", "heat", "dry", "fan"):
        if candidate in available_modes:
            return candidate
    return "cool"


def clamp_target_temperature(
    value: float | int | None,
    *,
    device_code: str | None = None,
) -> int:
    """Clamp a target temperature into the supported range."""
    profile = get_ac_profile(device_code) or SHARED_AIFA_AC_PROFILE
    temperature = profile.default_target_temp if value is None else round(float(value))
    return max(profile.min_temp, min(profile.max_temp, int(temperature)))


def _bcd_encode_device_code(device_code: int) -> tuple[int, int]:
    """Encode a device code into its two packet bytes like AIFA Smart app 1.2.9.

    `deviceCodeToBytes` pads the decimal string to 4 characters, parses
    characters [0:2] and [2:] as hex, and a Uint8List keeps only the low
    8 bits. Codes up to 9999 come out as plain BCD; a 5-digit code keeps
    its last two digits, so different codes can share bytes.

    Examples:
        device 25    → "0025"  → (0x00, 0x25)
        device 136   → "0136"  → (0x01, 0x36)
        device 9999  → "9999"  → (0x99, 0x99)
        device 10000 → "10000" → (0x10, 0x00), the same bytes as 1000
        device 19844 → "19844" → (0x19, 0x44)
        device 19866 → "19866" → (0x19, 0x66)
    """
    if not 0 <= device_code <= 99999:
        raise ValueError(
            f"device_code must be 0..99999; got {device_code}"
        )
    if device_code >= 10000 and device_code not in _CLOUD_CODE_WARNED:
        _CLOUD_CODE_WARNED.add(device_code)
        _LOGGER.warning(
            "device_code %s is a 5-digit cloud code — Plus-protocol support "
            "is EXPERIMENTAL. Three caveats: (1) the IR data must first be "
            "downloaded to the hub via the official AIFA app (Plus's TLS "
            "DevicePacketPlusDownload* protocol is not implemented here); "
            "(2) the hub broadcasts no acCommand state echo in Plus mode, so "
            "the climate entity stays in assumed-state mode (HA UI shows '?' "
            "badge) — there is no real status feedback; (3) bytes 3-4 keep "
            "only the first two and the last two digits, so codes such as "
            "19844 and 19944 share bytes on the wire — IR firing depends on "
            "which specific 5-digit code the AIFA app pushed to the hub.",
            device_code,
        )
    padded = str(device_code).rjust(4, "0")
    return (int(padded[0:2], 16) & 0xFF, int(padded[2:], 16) & 0xFF)


# SetCode bytes for a 5-digit code alias other codes, and the AIFA Smart app only
# sends SetCode for catalog rows with remote == false.
SET_CODE_MAX_DEVICE_CODE = 9999
SET_CODE_ACK_LENGTH = 6


def set_code_slot(sub_type: str | int | None) -> int:
    """Air-conditioner slot for SetCode: 0xA1 only when subType is 1."""
    try:
        return 0xA1 if int(str(sub_type).strip()) == 1 else 0xA0
    except (TypeError, ValueError):
        return 0xA0


def build_set_code_command(slot: int, device_code: int) -> str:
    """Build SetCode `ff f5 <slot> <hi> <lo> f0` for a code up to 9999."""
    if not 0 <= device_code <= SET_CODE_MAX_DEVICE_CODE:
        raise ValueError(f"SetCode needs a code from 0 to 9999; got {device_code}")
    high, low = _bcd_encode_device_code(device_code)
    return bytes([0xFF, 0xF5, slot & 0xFF, high, low, 0xF0]).hex()


# The AIFA Smart app's pairing test press uses the generic power key of every
# sub-device type, not the AC page's on key 0xA1.
PAIRING_TEST_KEY = 0xA0


def build_pairing_key_command(slot: int) -> str:
    """Build the pairing test press `ff <slot> a0 f0`."""
    return bytes([0xFF, slot & 0xFF, PAIRING_TEST_KEY, 0xF0]).hex()


def parse_set_code_ack(packet: bytes) -> tuple[int, int, int] | None:
    """Return (slot, hi, lo) from the hub's `fa f5 <slot> <hi> <lo> f0` echo."""
    if (
        len(packet) != SET_CODE_ACK_LENGTH
        or packet[0] != 0xFA
        or packet[1] != 0xF5
        or packet[-1] != 0xF0
        # `fa f5 <slot> ff ff f0` ends a code match; no code is spelled ff.
        or 0xFF in packet[3:5]
    ):
        return None
    return packet[2], packet[3], packet[4]


def build_code_match_start_command(slot: int) -> str:
    """Build the AIFA Smart app's StartMatch `ff f5 f4 f1 <slot> f0`."""
    return bytes([0xFF, 0xF5, 0xF4, 0xF1, slot & 0xFF, 0xF0]).hex()


def build_code_match_next_command(slot: int) -> str:
    """Build NextMatch `ff f5 f4 f3 <slot> f0`, which asks for the next candidate."""
    return bytes([0xFF, 0xF5, 0xF4, 0xF3, slot & 0xFF, 0xF0]).hex()


class CodeMatchReply(StrEnum):
    """What one hub reply during a code match means."""

    CANDIDATE = "candidate"
    END = "end"
    FAILED = "failed"


def parse_code_match_packet(packet: bytes) -> tuple[CodeMatchReply, int, int | None] | None:
    """Return (kind, slot, candidate code) like the AIFA Smart app reads a match reply.

    `fb f5 <slot> …` fails the match; `fa f5 <slot> xx` (4 bytes) or a frame
    with ff after b2 ends it; any other `fa f5 <slot> …` is a candidate whose
    bytes after b2, f0 dropped, read as decimal. The code is None when those
    bytes are not decimal digits.
    """
    if len(packet) < 3 or packet[1] != 0xF5 or packet[0] not in (0xFA, 0xFB):
        return None
    slot = packet[2]
    if packet[0] == 0xFB:
        return CodeMatchReply.FAILED, slot, None
    if len(packet) == 4:
        return CodeMatchReply.END, slot, None
    digits = [byte for byte in packet[3:] if byte != 0xF0]
    if 0xFF in digits:
        return CodeMatchReply.END, slot, None
    try:
        code = int("".join(f"{byte:02x}" for byte in digits))
    except ValueError:
        code = None
    return CodeMatchReply.CANDIDATE, slot, code


def device_code_wire_bytes(device_code: str | int | None) -> tuple[int, int] | None:
    """Bytes a code occupies in packets, or None when it cannot be encoded."""
    try:
        return _bcd_encode_device_code(int(str(device_code).strip()))
    except (TypeError, ValueError):
        return None


def build_power_on_command(*, device_code: str | None = None) -> str:
    """Return the short raw packet that turns an AC on."""
    profile = get_ac_profile(device_code) or SHARED_AIFA_AC_PROFILE
    return profile.power_on_command


def build_classic_temperature_key_command(sub_type: int, *, up: bool) -> str:
    """Build one +/- key press for ACs whose remote has no absolute setpoint.

    Each packet is a single key press; many such ACs only show the current
    setpoint on the first press and change it from the second.
    """
    return _build_single_key_command(
        sub_type, _TEMPERATURE_UP_KEY if up else _TEMPERATURE_DOWN_KEY
    )


def build_ac_key_command(key: AifaAcKey, sub_type: int) -> str:
    """Build one AIFA Smart app AC page key press."""
    if key.code in (_ANTI_MOLD_ON_KEY, _ANTI_MOLD_OFF_KEY):
        return bytes(
            (
                0xA1,
                0xFA,
                0xAC,
                0xA0 | (sub_type & 0x01),
                0x01 if key.code == _ANTI_MOLD_ON_KEY else 0x00,
                0xF0,
            )
        ).hex()
    return _build_single_key_command(sub_type, key.code)


def ac_key_requires_power_on(key: AifaAcKey, device_code: str | None) -> bool:
    """Return True when the app cannot press this key while the AC is off."""
    return key.requires_on or (
        key.in_bottom_sheet and not get_ac_capabilities(device_code).single_mode
    )


def is_anti_mold_key(key: AifaAcKey) -> bool:
    return key.code in (_ANTI_MOLD_ON_KEY, _ANTI_MOLD_OFF_KEY)


def shows_classic_temperature_keys(
    hub_version: int | None, device_code: str | None
) -> bool:
    """Return True when the app shows +/- keys instead of the setpoint slider."""
    return (
        hub_version in CLASSIC_TEMP_KEY_HUB_VERSIONS
        and get_ac_capabilities(device_code).classic_temp_control
    )


def visible_ac_keys(
    device_code: str | None, hub_type: str | None = None
) -> tuple[AifaAcKey, ...]:
    """Return the AC page keys the AIFA Smart app shows for a device code and hub type."""
    capabilities = get_ac_capabilities(device_code)
    single = capabilities.single_mode
    # Remote Config only: the app has no equivalent of the observed-codes list.
    extra_windspeed = capabilities.device_code in catalog.get_active().extra_windspeed_codes
    visible = {
        "timer": not single,
        "swing": not single,
        "fan_speed": not single,
        "power_saving": True,
        "sleep": not single,
        "turbo": not single,
        "mode": single,
        "anti_mold_on": hub_type in ANTI_MOLD_HUB_TYPES,
        "anti_mold_off": hub_type in ANTI_MOLD_HUB_TYPES,
        "quiet": extra_windspeed,
        "extra_wind_speed": extra_windspeed,
        "display": capabilities.show_display_mold or single,
        "mold": capabilities.show_display_mold,
        "wind_speed_high": capabilities.device_code in WIND_SPEED_PLUS_MINUS_CODES,
        "wind_speed_low": capabilities.device_code in WIND_SPEED_PLUS_MINUS_CODES,
    }
    return tuple(key for name, key in AC_KEYS.items() if visible[name])


def hub_requires_mode_keys(version: int | None, subversion: int | None) -> bool:
    """Return True when the hub must receive HVAC modes as single key presses."""
    return version == MODE_KEY_HUB_VERSION and subversion == MODE_KEY_HUB_SUBVERSION


def build_mode_key_command(mode: str, sub_type: int) -> str:
    """Build one HVAC mode key press for hubs that need single mode keys."""
    normalized_mode = normalize_ac_mode(mode)
    key = _MODE_KEY_BY_NAME.get(normalized_mode)
    if key is None:
        raise ValueError(f"Unsupported AIFA AC mode key: {mode}")
    return _build_single_key_command(sub_type, key)


def _build_single_key_command(sub_type: int, key: int) -> str:
    return bytes((0xFF, 0xA0 | (sub_type & 0x01), key, 0xF0)).hex()


def build_power_off_command(*, device_code: str | None = None) -> str:
    """Return the verified short raw packet that turns an AC off."""
    profile = get_ac_profile(device_code) or SHARED_AIFA_AC_PROFILE
    return profile.power_off_command


_FLAG_BYTE_POWER_SAVING = 0x01
_FLAG_BYTE_TURBO = 0x04
_FLAG_BYTE_SLEEP = 0x08


def _to_bcd(value: int) -> int:
    """Encode like the app: decimal digits parsed back as a hex byte."""
    return int(str(value), 16) & 0xFF


def build_app_status_command(
    status: AifaAcStatus,
    *,
    key_press: int,
    sub_type: int,
    device_code: str | None,
) -> str:
    """Build the 15-byte packet the AIFA Smart app's `AirConditionerStatusOn.toBytes` sends.

    The app copies its last reported On status and changes only the pressed
    field, so every other byte repeats what the hub reported.
    """
    if status.mode == "off":
        raise ValueError("The app never sends a 15-byte packet for an off status")
    if device_code is None:
        raise ValueError("device_code is required to build a status command")
    try:
        bcd_high, bcd_low = _bcd_encode_device_code(int(device_code))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"device_code={device_code!r} cannot be BCD-encoded: {exc}") from exc
    if status.power_saving:
        flags = _FLAG_BYTE_POWER_SAVING
    elif status.turbo:
        flags = _FLAG_BYTE_TURBO
    elif status.sleep:
        flags = _FLAG_BYTE_SLEEP
    else:
        flags = 0x00
    return bytes(
        [
            0xFF,
            0xA0 | (sub_type & 0x01),
            key_press,
            bcd_high,
            bcd_low,
            _to_bcd(status.target_temperature or 0),
            _to_bcd(status.timer_ir_value),
            _MODE_BYTE_BY_NAME[normalize_ac_mode(status.mode)],
            _FAN_BYTE_BY_NAME.get(status.fan_mode or "", 0x00),
            _SWING_BYTE_BY_NAME.get(status.swing_mode or "", 0x00),
            flags,
            0x00,
            0x00,
            0x00,
            0xF0,
        ]
    ).hex()


def _decode_bcd_byte(value: int) -> int | None:
    """Decode a decimal BCD byte used by the observed AC state packets."""
    text = f"{value:02x}"
    if any(char < "0" or char > "9" for char in text):
        return None
    return int(text)


FAN_SPEED_OPTIONS: Final[tuple[str, ...]] = ("auto", "low", "medium", "high")


def decode_fan_speed_byte(value: int) -> str:
    """Decode b7 the way the AIFA Smart app's `_speedFromByte` does.

    Bits are tested in this order and 0x10 is never read, so the hub's
    reports 0x11 / 0x90 / 0x50 / 0x30 are auto / 3 / 2 / 1.
    """
    if value & 0x01:
        return "auto"
    if value & 0x20:
        return "low"
    if value & 0x40:
        return "medium"
    if value & 0x80:
        return "high"
    return "auto"


def decode_observed_status_packet(
    packet: bytes | str,
    *,
    device_code: str | None = None,
) -> tuple[int, AifaAcStatus] | None:
    """Decode a controller-side observed AC state packet when possible."""
    if isinstance(packet, str):
        try:
            packet_bytes = bytes.fromhex(packet)
        except ValueError:
            return None
    else:
        packet_bytes = packet

    if (
        len(packet_bytes) != _OBSERVED_STATUS_PACKET_LENGTH
        or packet_bytes[0] != 0xFE
        or packet_bytes[1] not in (0xA0, 0xA1)
        or packet_bytes[-1] != 0xF0
    ):
        return None

    sub_type = 0 if packet_bytes[1] == 0xA0 else 1
    decoded_device_code = _decode_bcd_byte(packet_bytes[2])  # type: ignore[arg-type]
    if decoded_device_code is None:
        observed_device_code = _normalize_device_code(device_code)
    else:
        observed_device_code = str((decoded_device_code * 100) + (_decode_bcd_byte(packet_bytes[3]) or 0))

    # The AIFA Smart app parses b4/b5 with int.parse, so a non-decimal nibble makes
    # it drop the whole packet.
    timer_ir_value = _decode_bcd_byte(packet_bytes[5])
    if timer_ir_value is None:
        return None

    observed_fan_mode = decode_fan_speed_byte(packet_bytes[7])

    if packet_bytes[4] == 0x00 and packet_bytes[6] == 0x00:
        return (
            sub_type,
            AifaAcStatus(
                device_code=observed_device_code,
                mode="off",
                target_temperature=None,
                timer_ir_value=timer_ir_value,
                fan_mode=None,
                swing_mode=None,
                turbo=False,
                sleep=False,
                power_saving=False,
                packet_byte_12=packet_bytes[12],
                packet_byte_13=0,
                reported_fan_mode=observed_fan_mode,
            ),
        )

    mode_byte = packet_bytes[6]
    if mode_byte & 0x08:
        mode = "heat"
    elif mode_byte & 0x10:
        mode = "auto"
    elif mode_byte & 0x20:
        mode = "cool"
    elif mode_byte & 0x40:
        mode = "dry"
    elif mode_byte & 0x80:
        mode = "fan_only"
    else:
        mode = "auto"

    target_temperature = _decode_bcd_byte(packet_bytes[4])
    if target_temperature is None:
        return None
    # The hub reports 0x00 when it has no setpoint for the mode, including cool.
    if not AIFA_AC_MIN_TEMP <= target_temperature <= AIFA_AC_MAX_TEMP:
        target_temperature = None

    swing_byte = packet_bytes[8]
    if swing_byte == 0x00:
        observed_swing_mode = "off"
    elif swing_byte & 0x01:
        observed_swing_mode = "auto"
    elif swing_byte & 0x04:
        observed_swing_mode = "fixed_1"
    elif swing_byte & 0x08:
        observed_swing_mode = "fixed_2"
    elif swing_byte & 0x80:
        observed_swing_mode = "fixed_3"
    elif swing_byte & 0x40:
        observed_swing_mode = "fixed_4"
    elif swing_byte & 0x20:
        observed_swing_mode = "fixed_5"
    else:
        observed_swing_mode = "off"

    return (
        sub_type,
        AifaAcStatus(
            device_code=observed_device_code,
            mode=mode,
            target_temperature=target_temperature,
            timer_ir_value=timer_ir_value,
            fan_mode=observed_fan_mode,
            swing_mode=observed_swing_mode,
            turbo=bool(packet_bytes[9] & 0x04),
            sleep=bool(packet_bytes[9] & 0x08),
            power_saving=bool(packet_bytes[9] & 0x01),
            packet_byte_12=packet_bytes[10],
            packet_byte_13=packet_bytes[11],
            reported_fan_mode=observed_fan_mode,
        ),
    )
