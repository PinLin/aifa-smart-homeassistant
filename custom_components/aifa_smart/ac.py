"""AIFA Smart air-conditioner packet helpers."""
from __future__ import annotations

import logging
from dataclasses import dataclass
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
AIFA_AC_MIN_TEMP = 17
AIFA_AC_MAX_TEMP = 30
AIFA_AC_DEFAULT_TARGET_TEMP = 24
AIFA_AC_DEFAULT_TIMER_IR_VALUE = 0
AIFA_AC_DEFAULT_FAN_MODE = "high"
AIFA_AC_DEFAULT_SWING_MODE = "fixed_5"
CLASSIC_AC_QUERY_PACKET = "fff3f2f0"
CLASSIC_WAKE_REPLY_PACKET = "ccdd"

POWER_ON_COMMAND = "ffa0a1f0"
# Verified from the saved cloud function named "你好" on sub-device 31867.
POWER_OFF_COMMAND = "ffa0a2f0"

_STATUS_PACKET_PREFIX = 0x06
# Byte 2 (mode + temperature region), all values verified 2026-04-27 from
# AVD AIFA app v1.2.5 → /functions captures. Bit positions are device-code
# agnostic per Dart-side reverse engineering of AirConditionerStatusOn.toBytes:
#   cool >= 24 → 0x06   (verified: cool 24 default, cool 28)
#   cool <  24 → 0x07   (verified: cool 19, cool 20)
#   heat       → 0x08   (verified: heat 24 default)
#   fan_only   → 0x08   (verified: fanonlysample function)
#   auto       → 0x08   (verified: probe1 cmd 644455 RESPONSE)
#   dry        → 0x08   (verified: probe1 cmd 644474)
_VALID_STATUS_PACKET_PREFIXES: Final[frozenset[int]] = frozenset({0x06, 0x07, 0x08})
_STATUS_PACKET_LENGTH = 15
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
_DEFAULT_PACKET_BYTE_12 = 0x00
_DEFAULT_PACKET_BYTE_13 = 0x00

_MODE_BYTE_BY_NAME: Final[dict[str, int]] = {
    # All values verified 2026-04-27 from AVD AIFA app /functions captures.
    "auto": 0x10,      # verified: probe1 cmd 644455 ("Mode: Auto")
    "cool": 0x20,      # verified: cool 19/20/24/28
    "dry": 0x40,       # verified: probe1 cmd 644474 ("Mode: Dry")
    "fan_only": 0x80,  # verified: fanonlysample function
    "heat": 0x08,      # verified: heat 24 default
}
# Fan-byte mapping — partial verification 2026-04-27 from AVD AIFA app captures:
#   0x01 = auto    (AIFA metadata "speed: 0") VERIFIED via "by by", probe1
#   0x40 = medium  (AIFA metadata "speed: 2") VERIFIED via probe1 cmd 644454
#   0x80 = high    (AIFA metadata "speed: 3") VERIFIED via probe1 default
#   0x20 = low     (AIFA metadata "speed: 1") INFERRED — could not capture
#                  via AVD because the app's Speed control needs a working
#                  hub TCP connection (TCP 8751) which the AVD can't reach
#                  due to QEMU NAT. The 0x20 slot is the only remaining
#                  single-bit value consistent with the verified 0x01/0x40/0x80
#                  pattern, and matches what the integration has used in
#                  production. Re-verify by physical-phone capture if reports
#                  of incorrect "low" behavior surface.
_FAN_BYTE_BY_NAME: Final[dict[str, int]] = {
    "auto": 0x01,
    "low": 0x20,
    "medium": 0x40,
    "high": 0x80,
}
# AC_EXTRA_WINDSPEED_CODES (e.g. 19888) reportedly support 5 fan speeds
# (auto/low/mid_low/medium/mid_high/high) but the byte values for mid_low
# and mid_high are unknown — neither the reverse-engineered Dart enum nor
# the AIFA app's local SQLite samples expose them. Until we get a real
# 19888 sample, capabilities.extra_windspeed remains diagnostic-only and
# fan_modes stays at the 4-speed default.
# Swing-byte mapping — Direction-button cycle order VERIFIED 2026-04-27 by
# tapping the AVD AIFA app's Direction control 1..5 times and observing the
# resulting bytes:
#   default state → 0x01
#   after  1 tap  → 0x04
#   after  2 taps → 0x08
#   after  3 taps → 0x80
#   after  4 taps → 0x40
#   after  5 taps → 0x20
# Cycle ORDER is verified. Whether 0x01 is best labelled "auto" vs "default
# fixed pose" is INFERRED from the AIFA app naming convention (the integration
# uses "auto"). 0x00 ("off") was NEVER observed in any captured sample —
# kept here for HA UI completeness only and likely never emitted by the
# real device. Re-verify if any user reports unexpected swing behavior.
_SWING_BYTE_BY_NAME: Final[dict[str, int]] = {
    "off": 0x00,
    "auto": 0x01,
    "fixed_1": 0x04,
    "fixed_2": 0x08,
    "fixed_3": 0x80,
    "fixed_4": 0x40,
    "fixed_5": 0x20,
}
_MODE_NAME_BY_BYTE: Final[dict[int, str]] = {value: key for key, value in _MODE_BYTE_BY_NAME.items()}
_FAN_NAME_BY_BYTE: Final[dict[int, str]] = {value: key for key, value in _FAN_BYTE_BY_NAME.items()}
_SWING_NAME_BY_BYTE: Final[dict[int, str]] = {
    value: key for key, value in _SWING_BYTE_BY_NAME.items()
}


@dataclass(frozen=True, slots=True)
class AifaAcCapabilities:
    """Capability matrix for a single AIFA air-conditioner device code."""

    device_code: str | None
    available_modes: tuple[str, ...]
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
    configured_modes = cat.available_modes_by_device_code.get(
        normalized_code or "", None
    )

    if configured_modes is None:
        available_modes = _DEFAULT_AVAILABLE_MODES
    else:
        available_modes = configured_modes

    supports_extra_windspeed = normalized_code in (
        cat.extra_windspeed_codes | cat.observed_extra_windspeed_codes
    )
    # Turbo is exposed for every AC sub-device. The per-device IR encoder
    # is now BCD-device-aware (byte 4 = device_code BCD low byte), so the
    # earlier "we only know 136 supports turbo" heuristic is obsolete —
    # the byte-10 0x04 feature bit is a uniform on-the-wire signal to the
    # hub, and per-device IR translation is the hub's job. The switch
    # entity remains entity_registry_enabled_default=False so users opt-in
    # after confirming their AC actually responds to turbo.
    supports_turbo = True

    return AifaAcCapabilities(
        device_code=normalized_code,
        available_modes=tuple(normalize_catalog_mode(mode) for mode in available_modes),
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


def is_supported_ac_device_code(device_code: str | None) -> bool:
    """Return True when a device-code can use the shared AC packet family."""
    normalized_code = _normalize_device_code(device_code)
    if normalized_code is None:
        return False
    capabilities = get_ac_capabilities(normalized_code)
    return len(capabilities.available_modes) > 0


def is_supported_ac_sub_device(sub_device_type: str | None, device_code: str | None) -> bool:
    """Return True when a sub-device looks like a supported AIFA air conditioner."""
    return (sub_device_type or "").strip() == _SUPPORTED_SUB_DEVICE_TYPE and is_supported_ac_device_code(
        device_code
    )


def get_supported_ac_modes(device_code: str | None) -> tuple[str, ...]:
    """Return normalized catalog modes for a device-code."""
    return get_ac_capabilities(device_code).available_modes


def get_supported_fan_modes(device_code: str | None) -> tuple[str, ...]:
    """Return supported fan modes for a device-code."""
    return get_ac_capabilities(device_code).fan_modes


def get_supported_swing_modes(device_code: str | None) -> tuple[str, ...]:
    """Return supported swing modes for a device-code."""
    return get_ac_capabilities(device_code).swing_modes


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


def encode_temperature_bcd(target_temperature: int, *, device_code: str | None = None) -> int:
    """Encode a Celsius temperature using the BCD byte used by AIFA packets."""
    tens, ones = divmod(
        clamp_target_temperature(target_temperature, device_code=device_code), 10
    )
    return (tens << 4) | ones


def _is_bcd_byte(byte: int) -> bool:
    """True iff each nibble of `byte` is 0..9 (i.e., a valid BCD digit pair)."""
    return ((byte >> 4) & 0xF) <= 9 and (byte & 0xF) <= 9


def _bcd_encode_device_code(device_code: int) -> tuple[int, int]:
    """Encode a device code into the (byte_3, byte_4) status-packet pair.

    Mirrors the Dart-side `deviceCodeToBytes` reverse-engineered from blutter
    output of `air_conditioner_status.dart` v1.2.5:
    `code.toString().padLeft(4, "0")`, then interpret chars [0:2] and [2:4]
    as two hex bytes (`int.parse(.., radix:16)`).

    For 4-digit codes (offline catalog 1..414, plus our pre-cloud-aware
    legacy range 0..9999) this is mathematically equivalent to BCD-encoding
    `(code // 100, code % 100)` because all chars are 0..9.

    For 5-digit codes (cloud catalog `remote=true` rows 19844..19999) the
    5th character is silently dropped (substring(2, 4) discards index 4).
    This causes intentional aliasing — e.g. 19844..19848 all map to
    (0x19, 0x84). The AIFA hub disambiguates using its `set-code`-loaded
    value, treating bytes 3-4 only as a "family hint". So 5-digit support
    requires the hub to be re-synced via PATCH+set-code BEFORE the encoded
    packet is sent (otherwise the hub will translate to the wrong family).

    Examples:
        device 0     → "0000"  → (0x00, 0x00)
        device 11    → "0011"  → (0x00, 0x11)
        device 136   → "0136"  → (0x01, 0x36)
        device 9999  → "9999"  → (0x99, 0x99)
        device 19844 → "19844" → (0x19, 0x84)  ← collides with 19845..19848
        device 19999 → "19999" → (0x19, 0x99)  ← collides with 19990..19998
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
            "badge) — there is no real status feedback; (3) BCD bytes 3-4 "
            "drop the 5th digit, so codes within the same hundreds (e.g. "
            "19844..19848) collide on the wire — IR firing depends on which "
            "specific 5-digit code the AIFA app pushed to the hub.",
            device_code,
        )
    padded = str(device_code).rjust(4, "0")
    high_str = padded[0:2]
    low_str = padded[2:4]
    if not (high_str.isdigit() and low_str.isdigit()):
        raise ValueError(
            f"device_code chars must all be digits; got {device_code!r}"
        )
    return (int(high_str, 16), int(low_str, 16))


def _resolve_packet_prefix(normalized_mode: str, temperature: int) -> int:
    """Select byte 2 (mode + temperature region) per verified app samples."""
    if normalized_mode == "cool":
        return 0x06 if temperature >= 24 else 0x07
    # All non-cool modes verified to use 0x08 (heat / fan_only / auto / dry).
    return 0x08


def clamp_timer_ir_value(value: int | None) -> int:
    """Clamp a timer IR byte into the packet family's known BCD-safe range."""
    if value is None:
        return AIFA_AC_DEFAULT_TIMER_IR_VALUE
    return max(0, min(99, int(value)))


def encode_timer_ir_bcd(value: int | None) -> int:
    """Encode the timer IR field using the AC packet's decimal BCD layout."""
    clamped_value = clamp_timer_ir_value(value)
    tens, ones = divmod(clamped_value, 10)
    return (tens << 4) | ones


def build_power_on_command(*, device_code: str | None = None) -> str:
    """Return the short raw packet that turns an AC on."""
    profile = get_ac_profile(device_code) or SHARED_AIFA_AC_PROFILE
    return profile.power_on_command


def build_power_off_command(*, device_code: str | None = None) -> str:
    """Return the verified short raw packet that turns an AC off."""
    profile = get_ac_profile(device_code) or SHARED_AIFA_AC_PROFILE
    return profile.power_off_command


def build_ac_query_packet() -> str:
    """Return the classic controller packet that queries active AC state."""
    return CLASSIC_AC_QUERY_PACKET


def build_ac_query_single_type_packet(ac_sub_type: int) -> str:
    """Return the classic controller packet that queries one AC subtype."""
    normalized_sub_type = int(ac_sub_type)
    if normalized_sub_type not in {0, 1}:
        raise ValueError(f"Unsupported AIFA AC subtype: {ac_sub_type}")
    return f"fff3f2a{normalized_sub_type:x}f0"


def build_query_anti_mold_packet(ac_sub_type: int) -> str:
    """Return the classic controller packet that queries anti-mold status."""
    normalized_sub_type = int(ac_sub_type)
    if normalized_sub_type not in {0, 1}:
        raise ValueError(f"Unsupported AIFA AC subtype: {ac_sub_type}")
    return f"a1faaca{normalized_sub_type:x}f0"


def build_status_command(
    mode: str,
    target_temperature: int,
    *,
    device_code: str | None = None,
    timer_ir_value: int | None = AIFA_AC_DEFAULT_TIMER_IR_VALUE,
    fan_mode: str = AIFA_AC_DEFAULT_FAN_MODE,
    swing_mode: str = AIFA_AC_DEFAULT_SWING_MODE,
    turbo: bool = False,
    sleep: bool = False,
    power_saving: bool = False,
    packet_byte_12: int = _DEFAULT_PACKET_BYTE_12,
    packet_byte_13: int = _DEFAULT_PACKET_BYTE_13,
) -> str:
    """Build a raw AC status command for the shared packet profile."""
    profile = get_ac_profile(device_code) or SHARED_AIFA_AC_PROFILE
    normalized_mode = normalize_ac_mode(mode)
    normalized_fan_mode = fan_mode.strip().lower().replace("full_speed", "high")
    normalized_swing_mode = swing_mode.strip().lower()
    temperature = clamp_target_temperature(target_temperature, device_code=device_code)

    # Verified from the app-saved cloud function "fanonlysample" on sub-device 31867:
    # fan_only function-editor default swing renders as byte 9 = 0x01 (auto in
    # the integration's swing map), not the shared fixed_5 default (0x20).
    if (
        normalized_mode == "fan_only"
        and normalized_swing_mode == profile.default_swing_mode
    ):
        normalized_swing_mode = "auto"

    if normalized_mode not in profile.mode_byte_by_name:
        raise ValueError(f"Unsupported AIFA AC mode: {mode}")
    if normalized_fan_mode not in profile.fan_byte_by_name:
        raise ValueError(f"Unsupported AIFA AC fan mode: {fan_mode}")
    if normalized_swing_mode not in profile.swing_byte_by_name:
        raise ValueError(f"Unsupported AIFA AC swing mode: {swing_mode}")

    # Byte 10 feature flags:
    #   sleep         → 0x08  VERIFIED 2026-04-27 via AVD AIFA app Sleep button
    #                          (probe1 cmd 644452: ffa0060136240020010108000000f0)
    #   power_saving  → 0x02  VERIFIED INEFFECTIVE for Daikin code 11 (ARC466A12)
    #                          on 2026-04-29. AC ack'd packet (beep) but no ECO
    #                          indicator and no fan/temp change. Daikin typically
    #                          encodes ECON as a separate one-shot IR code, not in
    #                          the main status packet. Bit position retained for
    #                          other AC families that may use byte 10 differently.
    #   turbo         → 0x04  VERIFIED INEFFECTIVE for Daikin code 11 (ARC466A12)
    #                          on 2026-04-29. Same pattern as PS — AC ack'd but
    #                          no POWERFUL indicator and no fan change. Daikin
    #                          typically encodes POWERFUL as a separate one-shot
    #                          IR code. Bit position retained for other families.
    #                          Users opt-in to the turbo entity from the entity
    #                          registry and validate against their physical AC.
    feature_byte = 0x00
    if power_saving:
        feature_byte = 0x02
    elif turbo:
        feature_byte = 0x04
    elif sleep:
        feature_byte = 0x08

    packet_prefix = _resolve_packet_prefix(normalized_mode, temperature)
    # Bytes 3-4 are BCD of device_code padded to 4 digits — mirrors the
    # Dart-side AirConditionerStatusOn.toBytes() encoder. We refuse to
    # silently default to a hardcoded device family — a None / unparseable
    # device_code is a real bug the caller must fix, not something the
    # encoder should paper over.
    if device_code is None:
        raise ValueError(
            "device_code is required to build a status command; "
            "got None"
        )
    try:
        bcd_high, bcd_low = _bcd_encode_device_code(int(device_code))
    except (TypeError, ValueError) as exc:
        raise ValueError(
            f"device_code={device_code!r} cannot be BCD-encoded: {exc}"
        ) from exc

    packet = bytes(
        [
            0xFF,
            0xA0,
            packet_prefix,
            bcd_high,
            bcd_low,
            encode_temperature_bcd(temperature, device_code=device_code),
            encode_timer_ir_bcd(timer_ir_value),
            profile.mode_byte_by_name[normalized_mode],
            profile.fan_byte_by_name[normalized_fan_mode],
            profile.swing_byte_by_name[normalized_swing_mode],
            feature_byte,
            packet_byte_12 & 0xFF,
            packet_byte_13 & 0xFF,
            0x00,
            0xF0,
        ]
    )
    return packet.hex()


def _decode_bcd_byte(value: int) -> int | None:
    """Decode a decimal BCD byte used by the observed AC state packets."""
    text = f"{value:02x}"
    if any(char < "0" or char > "9" for char in text):
        return None
    return int(text)


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

    if packet_bytes[4] == 0x00 and packet_bytes[6] == 0x00:
        return (
            sub_type,
            AifaAcStatus(
                device_code=observed_device_code,
                mode="off",
                target_temperature=None,
                timer_ir_value=0,
                fan_mode=None,
                swing_mode=None,
                turbo=False,
                sleep=False,
                power_saving=False,
                packet_byte_12=packet_bytes[12],
                packet_byte_13=0,
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
        mode = None

    target_temperature = _decode_bcd_byte(packet_bytes[4])
    if mode in {"auto", "fan_only", "dry"} and packet_bytes[4] == 0x00:
        target_temperature = None

    if mode is None:
        return None

    # Fan / swing bit positions mirror _FAN_BYTE_BY_NAME / _SWING_BYTE_BY_NAME.
    # See those constants for verification status.
    fan_byte = packet_bytes[7]
    if fan_byte & 0x01:
        observed_fan_mode = "auto"
    elif fan_byte & 0x20:
        observed_fan_mode = "low"
    elif fan_byte & 0x40:
        observed_fan_mode = "medium"
    elif fan_byte & 0x80:
        observed_fan_mode = "high"
    else:
        observed_fan_mode = None

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
        observed_swing_mode = None

    return (
        sub_type,
        AifaAcStatus(
            device_code=observed_device_code,
            mode=mode,
            target_temperature=target_temperature,
            timer_ir_value=0,
            fan_mode=observed_fan_mode,
            swing_mode=observed_swing_mode,
            # Bit positions match the command-packet encoder per Tier 1
            # verified-byte work: sleep=0x08, PS=0x02, turbo=0x04. Earlier
            # this decoder used stale masks (0x10/0x01) which is why hub-side
            # state changes (e.g., user toggling Sleep in the AIFA app) never
            # propagated back to HA's entity state.
            turbo=bool(packet_bytes[9] & 0x04),
            sleep=bool(packet_bytes[9] & 0x08),
            power_saving=bool(packet_bytes[9] & 0x02),
            packet_byte_12=packet_bytes[10],
            packet_byte_13=packet_bytes[11],
        ),
    )


def decode_status_command(command: str, *, device_code: str | None = None) -> AifaAcStatus | None:
    """Decode a shared AIFA AC status packet when possible."""
    try:
        packet = bytes.fromhex(command)
    except ValueError:
        return None

    if (
        len(packet) != _STATUS_PACKET_LENGTH
        or packet[0] != 0xFF
        or packet[1] != 0xA0
        or packet[2] not in _VALID_STATUS_PACKET_PREFIXES
        or not _is_bcd_byte(packet[3])
        or not _is_bcd_byte(packet[4])
        or packet[-1] != 0xF0
    ):
        return None

    temp_byte = packet[5]
    target_temperature = ((temp_byte >> 4) * 10) + (temp_byte & 0x0F)
    if target_temperature < AIFA_AC_MIN_TEMP or target_temperature > AIFA_AC_MAX_TEMP:
        return None
    timer_ir_value = _decode_bcd_byte(packet[6])
    if timer_ir_value is None:
        return None

    mode = _MODE_NAME_BY_BYTE.get(packet[7])
    fan_mode = _FAN_NAME_BY_BYTE.get(packet[8])
    swing_mode = _SWING_NAME_BY_BYTE.get(packet[9])
    if mode is None or fan_mode is None or swing_mode is None:
        return None

    feature_byte = packet[10]
    return AifaAcStatus(
        device_code=_normalize_device_code(device_code),
        mode=mode,
        target_temperature=target_temperature,
        timer_ir_value=timer_ir_value,
        fan_mode=fan_mode,
        swing_mode=swing_mode,
        # Bit positions kept in lockstep with build_status_command:
        #   sleep=0x08 verified, power_saving=0x02 inferred, turbo=0x04 inferred.
        turbo=bool(feature_byte & 0x04),
        sleep=bool(feature_byte & 0x08),
        power_saving=bool(feature_byte & 0x02),
        packet_byte_12=packet[11],
        packet_byte_13=packet[12],
    )
