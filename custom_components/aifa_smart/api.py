"""API client for AIFA Smart."""
from __future__ import annotations

import asyncio
import json
import logging
import ssl
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Callable, Mapping

import aiohttp

from .ac import (
    AifaAcStatus,
    CLASSIC_WAKE_REPLY_PACKET,
    build_ac_query_packet,
    build_ac_query_single_type_packet,
    build_query_anti_mold_packet,
    decode_observed_status_packet,
)
from .const import (
    API_BASE_URL,
    CLASSIC_SOCKET_HOST,
    CLASSIC_SOCKET_PORT,
    DEVICE_TRANSFER_PATH,
    DEVICES_PATH,
    FUNCTIONS_PATH,
    MACROS_PATH,
    OAUTH_CLIENT_ID,
    OAUTH_TOKEN_PATH,
    SUB_DEVICE_DEVICE_CODES_PATH,
    SUB_DEVICE_SET_CODE_PATH,
)

_LOGGER = logging.getLogger(__name__)
_CLASSIC_SOCKET_HANDSHAKE_PREFIX = bytes.fromhex("a1fad7c0")
_CLASSIC_WAKE_UP_PREFIX = bytes.fromhex("f1ee")
_CLASSIC_NOTIFY_PREFIX = bytes.fromhex("faf5")
_CLASSIC_SENSOR_PREFIX = b"SENSOR"
_CLASSIC_PACKET_END = 0xF0
_CLASSIC_SOCKET_SSL_CONTEXT = ssl.create_default_context()


class AifaSmartError(Exception):
    """Base error for the integration."""


class AifaSmartConnectionError(AifaSmartError):
    """Raised when the API cannot be reached."""


class AifaSmartAuthError(AifaSmartError):
    """Raised when authentication fails."""


class AifaSmartApiError(AifaSmartError):
    """Raised when the API returns an unexpected response."""

    def __init__(
        self,
        message: str,
        *,
        status: int | None = None,
        code: int | None = None,
        name: str | None = None,
        payload: Any = None,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.name = name
        self.payload = payload


@dataclass(slots=True)
class AifaTokens:
    """OAuth token set."""

    access_token: str
    refresh_token: str | None = None
    token_type: str = "Bearer"
    expires_at: datetime | None = None

    def expiring_soon(self) -> bool:
        """Return True when the access token should be refreshed."""
        return self.expires_at is None or datetime.now(UTC) >= self.expires_at - timedelta(
            seconds=60
        )


@dataclass(slots=True)
class AifaClassicSocketSession:
    """Open classic-socket connection plus any post-handshake payload."""

    reader: asyncio.StreamReader
    writer: asyncio.StreamWriter
    initial_bytes: bytes = b""


@dataclass(slots=True)
class AifaSensorSample:
    """Latest known sensor sample for a device."""

    sensor_type: str
    value: float | None
    updated_at: datetime | None
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class AifaClassicSensorState:
    """Decoded live sensor payload from the classic socket."""

    temperature: float | None
    humidity: float | None
    raw: dict[str, str] = field(default_factory=dict)


@dataclass(slots=True)
class AifaFunctionCommand:
    """Single raw command associated with a cloud function."""

    id: str
    device_id: str
    sub_device_id: str | None
    command: str
    order_no: int | None = None
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class AifaFunction:
    """Saved cloud function / automation entry."""

    id: str
    device_id: str
    name: str
    category: str | None
    active: bool | None
    days: int | None
    notification: int | None
    start_time: int | None
    time_interval: int | None
    local: bool | None
    commands: list[AifaFunctionCommand] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def sub_device_ids(self) -> list[str]:
        """Return distinct sub-device ids referenced by the function commands."""
        seen: set[str] = set()
        ordered: list[str] = []
        for command in self.commands:
            if command.sub_device_id is None or command.sub_device_id in seen:
                continue
            seen.add(command.sub_device_id)
            ordered.append(command.sub_device_id)
        return ordered


@dataclass(slots=True)
class AifaMacroCommand:
    """Single raw command associated with a saved macro."""

    id: str
    macro_id: str
    device_id: str
    sub_device_id: str | None
    command: str
    delay: int | None = None
    order_no: int | None = None
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class AifaMacro:
    """Saved multi-step macro."""

    id: str
    name: str
    commands: list[AifaMacroCommand] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class AifaSubDevice:
    """Normalized sub-device / control model."""

    id: str
    device_id: str
    name: str
    type: str | None
    sub_type: str | None
    device_code: str | None
    device_code_brand_id: str | None = None
    device_code_brand_name: str | None = None
    device_code_brand_localized_name: str | None = None
    device_code_country: int | None = None
    device_code_version: int | None = None
    device_code_subversion: int | None = None
    device_code_type: int | None = None
    device_code_remote: bool | None = None
    device_code_popular: bool | None = None
    device_code_model_name: str | None = None
    available_brand_count: int | None = None
    available_device_code_count: int | None = None
    ac_available_modes: list[str] = field(default_factory=list)
    ac_classic_temp_control: bool | None = None
    ac_extra_windspeed: bool | None = None
    ac_show_display_mold: bool | None = None
    ac_single_mode: bool | None = None
    ac_dehumidifier: bool | None = None
    ac_supports_sleep: bool | None = None
    ac_supports_power_saving: bool | None = None
    ac_supports_turbo: bool | None = None
    estimated_capabilities: list[str] = field(default_factory=list)
    functions: list[AifaFunction] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class AifaDevice:
    """Normalized device model."""

    id: str
    name: str
    mac: str | None
    device_type: str | None
    online: bool | None
    temperature: float | None
    humidity: float | None
    firmware: str | None
    sub_devices: list[AifaSubDevice] = field(default_factory=list)
    functions: list[AifaFunction] = field(default_factory=list)
    updated_at: datetime | None = None
    sensor_samples: dict[str, AifaSensorSample] = field(default_factory=dict)
    raw: dict[str, Any] = field(default_factory=dict)

    def get_sub_device(self, sub_device_id: str) -> AifaSubDevice | None:
        """Return a single sub-device by ID."""
        for sub_device in self.sub_devices:
            if sub_device.id == sub_device_id:
                return sub_device
        return None


@dataclass(slots=True)
class AifaBrandLocalization:
    """Localized device-brand label."""

    country_code: str | None
    language_code: str | None
    name: str
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class AifaBrand:
    """Brand metadata returned by the device-code catalog endpoint."""

    id: str
    name: str
    localized_name: str | None
    localizations: list[AifaBrandLocalization] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class AifaDeviceCode:
    """Single device-code row from the AIFA cloud catalog."""

    id: str
    code: str | None
    brand_id: str | None
    brand_name: str | None
    brand_localized_name: str | None
    country: int | None
    version: int | None
    subversion: int | None
    type: int | None
    remote: bool | None
    popular: bool | None
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class AifaDeviceCodeCatalog:
    """All known device-code rows for a single sub-device category."""

    brands: dict[str, AifaBrand] = field(default_factory=dict)
    device_codes: list[AifaDeviceCode] = field(default_factory=list)

    def get_device_code(self, device_code_id: str | None) -> AifaDeviceCode | None:
        """Return the catalog row whose internal `id` matches `device_code_id`."""
        if device_code_id is None:
            return None
        lookup = str(device_code_id)
        for device_code in self.device_codes:
            if device_code.id == lookup:
                return device_code
        return None

    def find_by_code(self, code: str | None) -> AifaDeviceCode | None:
        """Return the first catalog row whose `code` matches `code`.

        AIFA cloud's `sub_device.deviceCode` field stores the IR codec
        family number (e.g. 11 for Daikin's Classic AC code 11), NOT the
        catalog row id (which is e.g. 4 for that same Daikin entry). For
        diagnostic enrichment from a cloud value we look up by `code`.

        Returns the first match — for codes shared across multiple brands
        (e.g. code 25 has 13 brand variants in the AIFA catalog) we cannot
        recover which specific brand the user originally selected because
        cloud only persists the bare `deviceCode` integer. Callers that
        need brand-accuracy must rely on a separate signal.
        """
        if code is None:
            return None
        lookup = str(code)
        for device_code in self.device_codes:
            if device_code.code == lookup:
                return device_code
        return None


def _maybe_mapping(value: Any) -> Mapping[str, Any] | None:
    """Return a mapping if the value is one."""
    return value if isinstance(value, Mapping) else None


def _pick(mapping: Mapping[str, Any], *keys: str) -> Any:
    """Return the first present key from a mapping."""
    for key in keys:
        if key in mapping and mapping[key] is not None:
            return mapping[key]
    return None


def _coerce_bool(value: Any) -> bool | None:
    """Convert common API values into bool."""
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        text = value.strip().lower()
        if text in {"1", "true", "yes", "y", "on", "online"}:
            return True
        if text in {"0", "false", "no", "n", "off", "offline"}:
            return False
    return None


def _coerce_float(value: Any) -> float | None:
    """Convert API values to float."""
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _coerce_datetime(value: Any) -> datetime | None:
    """Best-effort timestamp parser."""
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    if isinstance(value, (int, float)):
        timestamp = float(value)
        if timestamp > 1_000_000_000_000:
            timestamp /= 1000.0
        return datetime.fromtimestamp(timestamp, tz=UTC)
    if not isinstance(value, str):
        return None

    text = value.strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"

    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        parsed = None

    if parsed is None:
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y/%m/%d %H:%M:%S", "%Y%m%d%H%M%S"):
            try:
                parsed = datetime.strptime(text, fmt)
                break
            except ValueError:
                continue

    if parsed is None:
        return None

    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _coerce_str(value: Any) -> str | None:
    """Convert a value to a trimmed string."""
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _pick_brand_localized_name(
    localizations: list[AifaBrandLocalization],
) -> str | None:
    """Pick a stable localized brand label for diagnostics."""
    preferred = next(
        (
            localization.name
            for localization in localizations
            if localization.country_code == "tw" and localization.language_code == "zh"
        ),
        None,
    )
    if preferred:
        return preferred
    return localizations[0].name if localizations else None


def _extract_list(payload: Any, *keys: str) -> list[Any]:
    """Extract a list from variable response shapes."""
    if isinstance(payload, list):
        return payload
    if isinstance(payload, Mapping):
        for key in keys:
            value = payload.get(key)
            if isinstance(value, list):
                return value
    return []


def _oauth_payload(grant_type: str, **fields: Any) -> dict[str, Any]:
    """Build the JSON body expected by the AIFA OAuth endpoint."""
    payload: dict[str, Any] = {
        "client_id": OAUTH_CLIENT_ID,
        "grant_type": grant_type,
    }
    payload.update(fields)
    return payload


def _transfer_payload(
    mac: str,
    *,
    command: str | None = None,
    packet: str | None = None,
    device_id: str | int | None = None,
    sub_device_id: str | int | None = None,
    extra: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build the verified shape for the classic i-Ctrl transfer endpoint."""
    payload: dict[str, Any] = {"mac": mac}
    if command is not None:
        payload["command"] = command
    if packet is not None:
        payload["packet"] = packet
    if device_id is not None:
        payload["deviceId"] = _coerce_int(device_id) or device_id
    if sub_device_id is not None:
        payload["subDeviceId"] = _coerce_int(sub_device_id) or sub_device_id
    if extra:
        payload.update(dict(extra))
    return payload


def extract_classic_status_packets(buffer: bytes) -> tuple[list[bytes], bytes]:
    """Extract observed classic-controller AC status packets from a socket buffer."""
    packets: list[bytes] = []
    scan_index = 0
    buffer_length = len(buffer)

    while scan_index <= buffer_length - 14:
        start = buffer.find(b"\xfe", scan_index)
        if start < 0:
            break
        if start + 14 > buffer_length:
            break
        candidate = buffer[start : start + 14]
        if candidate[1] in (0xA0, 0xA1) and candidate[-1] == 0xF0:
            packets.append(candidate)
            scan_index = start + 14
            continue
        scan_index = start + 1

    incomplete_start = buffer.rfind(b"\xfe", scan_index)
    if incomplete_start >= 0 and buffer_length - incomplete_start < 14:
        return packets, buffer[incomplete_start:]
    return packets, b""


def extract_classic_wake_packets(buffer: bytes) -> tuple[list[bytes], bytes]:
    """Extract wake-up packets that require the classic ``ccdd`` reply."""
    packets: list[bytes] = []
    scan_index = 0
    buffer_length = len(buffer)

    while scan_index <= buffer_length - 4:
        start = buffer.find(_CLASSIC_WAKE_UP_PREFIX, scan_index)
        if start < 0:
            break
        if start + 4 > buffer_length:
            break
        candidate = buffer[start : start + 4]
        if candidate[-1] == _CLASSIC_PACKET_END:
            packets.append(candidate)
            scan_index = start + 4
            continue
        scan_index = start + 1

    last_start = buffer.rfind(b"\xf1", scan_index)
    if last_start >= 0:
        tail = buffer[last_start:]
        if len(tail) == 1:
            return packets, tail
        if tail[:2] == _CLASSIC_WAKE_UP_PREFIX and len(tail) < 4:
            return packets, tail
    return packets, b""


def extract_classic_notify_packets(buffer: bytes) -> tuple[list[bytes], bytes]:
    """Extract short classic notify packets that hint an AC state change occurred."""
    packets: list[bytes] = []
    scan_index = 0
    buffer_length = len(buffer)

    while scan_index <= buffer_length - 6:
        start = buffer.find(_CLASSIC_NOTIFY_PREFIX, scan_index)
        if start < 0:
            break
        if start + 6 > buffer_length:
            break
        candidate = buffer[start : start + 6]
        if candidate[-1] == _CLASSIC_PACKET_END:
            packets.append(candidate)
            scan_index = start + 6
            continue
        scan_index = start + 1

    last_start = buffer.rfind(b"\xfa", scan_index)
    if last_start >= 0:
        tail = buffer[last_start:]
        if len(tail) == 1:
            return packets, tail
        if tail[:2] == _CLASSIC_NOTIFY_PREFIX and len(tail) < 6:
            return packets, tail
    return packets, b""


def extract_classic_sensor_packets(buffer: bytes) -> tuple[list[bytes], bytes]:
    """Extract ``SENSOR{...}`` payloads from a classic socket buffer."""
    packets: list[bytes] = []
    scan_index = 0

    while True:
        start = buffer.find(_CLASSIC_SENSOR_PREFIX, scan_index)
        if start < 0:
            break
        end = buffer.find(b"}", start + len(_CLASSIC_SENSOR_PREFIX))
        if end < 0:
            return packets, buffer[start:]
        packets.append(buffer[start : end + 1])
        scan_index = end + 1

    return packets, b""


def decode_classic_sensor_packet(
    packet: bytes,
    *,
    temperature_adjustment_mode: int | None = None,
) -> AifaClassicSensorState | None:
    """Decode one live classic-socket sensor packet into temperature and humidity."""
    if not packet.startswith(_CLASSIC_SENSOR_PREFIX):
        return None

    try:
        payload = json.loads(packet[len(_CLASSIC_SENSOR_PREFIX) :].decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(payload, Mapping):
        return None

    raw_values = {str(key): str(value) for key, value in payload.items()}

    humidity: float | None = None
    humidity_raw = raw_values.get("hum")
    if humidity_raw is not None:
        try:
            humidity = int(humidity_raw) / 65536 * 100
        except ValueError:
            humidity = None

    temperature: float | None = None
    temperature_raw = raw_values.get("temp")
    if temperature_raw is not None:
        try:
            raw_temperature = int(temperature_raw) / 65536 * 175 - 45
        except ValueError:
            raw_temperature = None
        if raw_temperature is not None:
            temperature = (
                (-0.00322 * raw_temperature * raw_temperature)
                + (1.05 * raw_temperature)
                + 0.235
            )
            if temperature_adjustment_mode == 4:
                temperature -= 1.0

    return AifaClassicSensorState(
        temperature=temperature,
        humidity=humidity,
        raw=raw_values,
    )


# Backwards-compatible aliases for tests and older local tooling.
_extract_classic_status_packets = extract_classic_status_packets
_extract_classic_wake_packets = extract_classic_wake_packets
_extract_classic_notify_packets = extract_classic_notify_packets
_extract_classic_sensor_packets = extract_classic_sensor_packets


def build_classic_ac_query_steps(sub_types: set[int] | None) -> list[tuple[str, float]]:
    """Build the app-like classic AC query sequence for one or more sub-types."""
    normalized_sub_types: list[int] = []
    for item in sub_types or set():
        value = int(item)
        if value in {0, 1} and value not in normalized_sub_types:
            normalized_sub_types.append(value)

    query_steps: list[tuple[str, float]] = []
    if normalized_sub_types:
        for item in normalized_sub_types:
            query_steps.append((build_ac_query_single_type_packet(item), 0.30))
            query_steps.append((build_query_anti_mold_packet(item), 0.60))
    else:
        query_steps.append((build_ac_query_packet(), 0.30))
    return query_steps


def _parse_device(raw: Mapping[str, Any]) -> AifaDevice | None:
    """Normalize a device row."""
    sensor_block = _maybe_mapping(
        _pick(raw, "sensors", "sensor", "sensorData", "sensor_data")
    ) or {}
    device_id = _pick(raw, "id", "deviceId", "device_id")
    if device_id is None:
        return None

    sub_devices = _pick(raw, "subDevices", "sub_devices") or []
    if not isinstance(sub_devices, list):
        sub_devices = []

    parsed_sub_devices: list[AifaSubDevice] = []
    for row in sub_devices:
        if not isinstance(row, Mapping):
            continue
        sub_device = _parse_sub_device(row, device_id)
        if sub_device is not None:
            parsed_sub_devices.append(sub_device)

    device = AifaDevice(
        id=str(device_id),
        name=str(_pick(raw, "deviceName", "name") or f"AIFA Device {device_id}"),
        mac=_coerce_str(_pick(raw, "mac")),
        device_type=str(_pick(raw, "deviceType", "type", "device_type") or "").strip() or None,
        online=_coerce_bool(_pick(raw, "online", "isOnline", "connected")),
        temperature=_coerce_float(
            _pick(raw, "temperature", "temp") or _pick(sensor_block, "temperature", "temp")
        ),
        humidity=_coerce_float(
            _pick(raw, "humidity") or _pick(sensor_block, "humidity", "humid")
        ),
        firmware=str(_pick(raw, "firmware", "version", "currentVersion") or "").strip() or None,
        sub_devices=parsed_sub_devices,
        updated_at=_coerce_datetime(
            _pick(raw, "updatedAt", "updated_at", "timestamp", "timeStamp", "time")
        ),
        raw=dict(raw),
    )
    return device


def _parse_sub_device(raw: Mapping[str, Any], device_id: Any) -> AifaSubDevice | None:
    """Normalize a sub-device row."""
    sub_device_id = _pick(raw, "id", "subDeviceId", "sub_device_id")
    if sub_device_id is None:
        return None

    return AifaSubDevice(
        id=str(sub_device_id),
        device_id=str(device_id),
        name=str(_pick(raw, "name", "deviceName") or f"Sub-device {sub_device_id}"),
        type=_coerce_str(_pick(raw, "type")),
        sub_type=_coerce_str(_pick(raw, "subType", "sub_type")),
        device_code=_coerce_str(_pick(raw, "deviceCode", "device_code")),
        raw=dict(raw),
    )


def _parse_function_command(raw: Mapping[str, Any], fallback_order: int) -> AifaFunctionCommand | None:
    """Normalize a single function command row."""
    command = _coerce_str(_pick(raw, "command"))
    device_id = _pick(raw, "deviceId", "device_id")
    command_id = _pick(raw, "id", "commandId", "command_id", fallback_order)
    if command is None or device_id is None or command_id is None:
        return None

    return AifaFunctionCommand(
        id=str(command_id),
        device_id=str(device_id),
        sub_device_id=_coerce_str(_pick(raw, "subDeviceId", "sub_device_id")),
        command=command,
        order_no=_coerce_int(_pick(raw, "orderNo", "order_no", "order", fallback_order)),
        raw=dict(raw),
    )


def _parse_function(raw: Mapping[str, Any]) -> AifaFunction | None:
    """Normalize a saved function row."""
    function_id = _pick(raw, "id", "functionId", "function_id")
    device_id = _pick(raw, "deviceId", "device_id")
    if function_id is None or device_id is None:
        return None

    commands_raw = _extract_list(raw, "commands", "functionCommands", "function_commands")
    commands: list[AifaFunctionCommand] = []
    for index, row in enumerate(commands_raw):
        if not isinstance(row, Mapping):
            continue
        command = _parse_function_command(row, index)
        if command is not None:
            commands.append(command)

    commands.sort(key=lambda item: item.order_no if item.order_no is not None else 0)

    time_block = _maybe_mapping(_pick(raw, "time")) or {}
    start_time = _coerce_int(_pick(time_block, "start"))
    if start_time is None:
        start_time = _coerce_int(_pick(raw, "startTime", "start_time"))
    time_interval = _coerce_int(_pick(time_block, "length"))
    if time_interval is None:
        time_interval = _coerce_int(_pick(raw, "timeInterval", "time_interval"))

    return AifaFunction(
        id=str(function_id),
        device_id=str(device_id),
        name=str(_pick(raw, "name", "functionName") or f"Function {function_id}"),
        category=_coerce_str(_pick(raw, "category")),
        active=_coerce_bool(_pick(raw, "active", "enabled")),
        days=_coerce_int(_pick(raw, "days")),
        notification=_coerce_int(_pick(raw, "notification")),
        start_time=start_time,
        time_interval=time_interval,
        local=_coerce_bool(_pick(raw, "local")),
        commands=commands,
        raw=dict(raw),
    )


def _parse_brand_localization(raw: Mapping[str, Any]) -> AifaBrandLocalization | None:
    """Normalize a single brand-localization row."""
    name = _coerce_str(_pick(raw, "name"))
    if name is None:
        return None

    return AifaBrandLocalization(
        country_code=_coerce_str(_pick(raw, "countryCode", "country_code")),
        language_code=_coerce_str(_pick(raw, "languageCode", "language_code")),
        name=name,
        raw=dict(raw),
    )


def _parse_brand(raw: Mapping[str, Any]) -> AifaBrand | None:
    """Normalize a device-brand row."""
    brand_id = _pick(raw, "id", "brandId", "brand_id")
    name = _coerce_str(_pick(raw, "name"))
    if brand_id is None or name is None:
        return None

    localizations_raw = _extract_list(raw, "localizations")
    localizations: list[AifaBrandLocalization] = []
    for row in localizations_raw:
        if not isinstance(row, Mapping):
            continue
        localization = _parse_brand_localization(row)
        if localization is not None:
            localizations.append(localization)

    return AifaBrand(
        id=str(brand_id),
        name=name,
        localized_name=_pick_brand_localized_name(localizations),
        localizations=localizations,
        raw=dict(raw),
    )


def _parse_device_code(
    raw: Mapping[str, Any], brands: dict[str, AifaBrand]
) -> AifaDeviceCode | None:
    """Normalize a single device-code row."""
    device_code_id = _pick(raw, "id", "deviceCodeId", "device_code_id")
    if device_code_id is None:
        return None

    brand_id = _coerce_str(_pick(raw, "brandId", "brand_id"))
    brand = None if brand_id is None else brands.get(brand_id)

    return AifaDeviceCode(
        id=str(device_code_id),
        code=_coerce_str(_pick(raw, "code")),
        brand_id=brand_id,
        brand_name=None if brand is None else brand.name,
        brand_localized_name=None if brand is None else brand.localized_name,
        country=_coerce_int(_pick(raw, "country")),
        version=_coerce_int(_pick(raw, "version")),
        subversion=_coerce_int(_pick(raw, "subversion")),
        type=_coerce_int(_pick(raw, "type")),
        remote=_coerce_bool(_pick(raw, "remote")),
        popular=_coerce_bool(_pick(raw, "popular")),
        raw=dict(raw),
    )


def _parse_device_code_catalog(payload: Mapping[str, Any]) -> AifaDeviceCodeCatalog:
    """Normalize a full device-code catalog payload."""
    brands: dict[str, AifaBrand] = {}
    for row in _extract_list(payload, "brands"):
        if not isinstance(row, Mapping):
            continue
        brand = _parse_brand(row)
        if brand is not None:
            brands[brand.id] = brand

    device_codes: list[AifaDeviceCode] = []
    for row in _extract_list(payload, "deviceCodes", "device_codes"):
        if not isinstance(row, Mapping):
            continue
        device_code = _parse_device_code(row, brands)
        if device_code is not None:
            device_codes.append(device_code)

    return AifaDeviceCodeCatalog(
        brands=brands,
        device_codes=device_codes,
    )


def _parse_macro_command(
    raw: Mapping[str, Any],
    *,
    macro_id: str,
    fallback_order: int,
) -> AifaMacroCommand | None:
    """Normalize a single macro command row."""
    command = _coerce_str(_pick(raw, "command"))
    device_id = _pick(raw, "deviceId", "device_id")
    command_id = _pick(raw, "id", "commandId", "command_id", fallback_order)
    if command is None or device_id is None or command_id is None:
        return None

    return AifaMacroCommand(
        id=str(command_id),
        macro_id=macro_id,
        device_id=str(device_id),
        sub_device_id=_coerce_str(_pick(raw, "subDeviceId", "sub_device_id")),
        command=command,
        delay=_coerce_int(_pick(raw, "delay", "delayMs", "delay_ms")),
        order_no=_coerce_int(_pick(raw, "orderNo", "order_no", "order", fallback_order)),
        raw=dict(raw),
    )


def _parse_macro(raw: Mapping[str, Any]) -> AifaMacro | None:
    """Normalize a saved macro row."""
    macro_id = _pick(raw, "id", "macroId", "macro_id")
    if macro_id is None:
        return None

    commands_raw = _extract_list(raw, "commands", "macroCommands", "macro_commands")
    commands: list[AifaMacroCommand] = []
    for index, row in enumerate(commands_raw):
        if not isinstance(row, Mapping):
            continue
        command = _parse_macro_command(row, macro_id=str(macro_id), fallback_order=index)
        if command is not None:
            commands.append(command)

    commands.sort(key=lambda item: item.order_no if item.order_no is not None else 0)

    return AifaMacro(
        id=str(macro_id),
        name=str(_pick(raw, "name", "macroName", "macro_name") or f"Macro {macro_id}"),
        commands=commands,
        raw=dict(raw),
    )


class AifaSmartApiClient:
    """Client for the AIFA Smart cloud API."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        *,
        email: str | None = None,
        password: str | None = None,
        tokens: AifaTokens | None = None,
        on_tokens_updated: Callable[[AifaTokens], None] | None = None,
    ) -> None:
        self._session = session
        self._email = email
        self._password = password
        self._tokens = tokens
        self._token_lock = asyncio.Lock()
        self._on_tokens_updated = on_tokens_updated

    @property
    def tokens(self) -> AifaTokens | None:
        """Return the current in-memory tokens."""
        return self._tokens

    def _store_tokens(self, tokens: AifaTokens) -> None:
        """Store a fresh token set and notify any persistence listener."""
        self._tokens = tokens
        if self._on_tokens_updated is not None:
            self._on_tokens_updated(tokens)

    async def async_validate_credentials(self) -> None:
        """Validate that configured credentials can get a token."""
        await self._ensure_access_token(force_login=True)

    async def async_get_devices(self) -> list[AifaDevice]:
        """Fetch devices from the cloud API."""
        payload = await self._authorized_request("GET", DEVICES_PATH)
        rows = _extract_list(payload, "devices", "data", "items")
        devices: list[AifaDevice] = []
        for row in rows:
            if not isinstance(row, Mapping):
                continue
            device = _parse_device(row)
            if device is not None:
                devices.append(device)
        return devices

    async def async_get_functions(self, sub_device_id: str) -> list[AifaFunction]:
        """Fetch saved functions for a sub-device."""
        try:
            payload = await self._authorized_request(
                "GET",
                FUNCTIONS_PATH,
                params={"subDeviceId": sub_device_id},
            )
        except AifaSmartApiError as err:
            if err.status in {400, 404, 500}:
                _LOGGER.debug("Ignoring functions endpoint error for %s: %s", sub_device_id, err)
                return []
            raise
        rows = _extract_list(payload, "functions", "data", "items")
        functions: list[AifaFunction] = []
        for row in rows:
            if not isinstance(row, Mapping):
                continue
            function = _parse_function(row)
            if function is not None:
                functions.append(function)
        return functions

    async def async_get_macros(self) -> list[AifaMacro]:
        """Fetch saved macros for the current account."""
        try:
            payload = await self._authorized_request("GET", MACROS_PATH)
        except AifaSmartApiError as err:
            if err.status in {400, 404, 500}:
                _LOGGER.debug("Ignoring macros error: %s", err)
                return []
            raise

        rows = _extract_list(payload, "macros", "data", "items")
        macros: list[AifaMacro] = []
        for row in rows:
            if not isinstance(row, Mapping):
                continue
            macro = _parse_macro(row)
            if macro is not None:
                macros.append(macro)
        macros.sort(key=lambda item: item.name.casefold())
        return macros

    async def async_get_sub_device_device_codes(
        self, sub_device_id: str
    ) -> AifaDeviceCodeCatalog:
        """Fetch the device-code catalog for a sub-device category."""
        try:
            payload = await self._authorized_request(
                "GET",
                SUB_DEVICE_DEVICE_CODES_PATH.format(sub_device_id=sub_device_id),
            )
        except AifaSmartApiError as err:
            if err.status in {400, 404, 500}:
                _LOGGER.debug(
                    "Ignoring device-code catalog error for %s: %s", sub_device_id, err
                )
                return AifaDeviceCodeCatalog()
            raise

        if not isinstance(payload, Mapping):
            return AifaDeviceCodeCatalog()
        return _parse_device_code_catalog(payload)

    async def async_push_sub_device_code(
        self, *, sub_device_id: int, device_code_id: int
    ) -> None:
        """Tell the hub to load a Classic device-code IR profile.

        POST /sub-devices/{id}/set-code with `{"deviceCodeId": N}`. Cloud
        relays the request via TLS 8751; on 204 the hub writes the code
        into NAND-persistent storage and resumes broadcasting its BCD in
        the idle frame `fea00<HHLL>...f0`.

        Verified empirically (2026-04-29 against firmware v4):
        - Hub state survives full power cycle (NAND-backed).
        - Cloud rejects with 404 "Device not connected" when the hub is
          offline — set-code is NOT queued for later delivery.
        - 5-digit (Plus) codes return 202 "No device code set or device
          code is downloaded"; the IR data must first be pushed via the
          TLS DevicePacket.plusDownload* protocol, not implemented here.
        """
        path = SUB_DEVICE_SET_CODE_PATH.format(sub_device_id=sub_device_id)
        await self._authorized_request(
            "POST", path, json={"deviceCodeId": device_code_id}
        )

    async def async_transfer_packet(
        self,
        mac: str,
        *,
        command: str | None = None,
        packet: str | None = None,
        device_id: str | int | None = None,
        sub_device_id: str | int | None = None,
        extra: Mapping[str, Any] | None = None,
    ) -> bool:
        """Send a packet through the classic i-Ctrl transfer endpoint.

        Verified behavior today:
        - POST /devices/transfer requires ``mac`` for classic i-Ctrl hubs.
        - The endpoint currently returns ``201`` or ``202`` with an empty body.
        - It does not yet give us a usable observed AC state payload.
        """
        payload = _transfer_payload(
            mac,
            command=command,
            packet=packet,
            device_id=device_id,
            sub_device_id=sub_device_id,
            extra=extra,
        )
        try:
            await self._authorized_request(
                "POST",
                DEVICE_TRANSFER_PATH,
                json=payload,
            )
        except AifaSmartApiError as err:
            if err.status in {201, 202}:
                return True
            raise
        return True

    async def async_open_classic_socket(
        self,
        mac: str,
        *,
        timeout: float = 6.0,
    ) -> AifaClassicSocketSession:
        """Open the private classic socket and return any bytes received after the handshake."""
        token = await self._ensure_access_token()
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(
                CLASSIC_SOCKET_HOST,
                CLASSIC_SOCKET_PORT,
                ssl=_CLASSIC_SOCKET_SSL_CONTEXT,
                server_hostname=CLASSIC_SOCKET_HOST,
            ),
            timeout=timeout,
        )

        try:
            handshake = _CLASSIC_SOCKET_HANDSHAKE_PREFIX + json.dumps(
                {"mac": mac, "token": token.access_token},
                separators=(",", ":"),
            ).encode()
            writer.write(handshake)
            await writer.drain()

            ack = await asyncio.wait_for(reader.read(32), timeout=min(3.0, timeout))
            if ack and ack[:1] != b"\x00":
                _LOGGER.debug(
                    "Unexpected classic-socket handshake response for %s: %s",
                    mac,
                    ack.hex(),
                )
            return AifaClassicSocketSession(
                reader=reader,
                writer=writer,
                initial_bytes=ack[1:] if len(ack) > 1 else b"",
            )
        except Exception:
            writer.close()
            try:
                await writer.wait_closed()
            except OSError:
                pass
            raise

    async def async_query_classic_ac_statuses(
        self,
        mac: str,
        *,
        sub_types: set[int] | None = None,
        timeout: float = 6.0,
    ) -> dict[int, AifaAcStatus]:
        """Query observed AC controller state through the private classic socket.

        Current behavior mirrors the app more closely than the earlier generic probe:
        - for known AC sub-types we send ``acQuerySingleType(subType)``
        - then, after a short gap, ``queryAntiMold(subType)``
        - if the controller sends a wake-up frame, we reply with ``ccdd``
        """
        query_steps = build_classic_ac_query_steps(sub_types)
        normalized_sub_types = {
            int(item) for item in (sub_types or set()) if int(item) in {0, 1}
        }

        observed: dict[int, AifaAcStatus] = {}
        writer: asyncio.StreamWriter | None = None
        status_buffer = b""
        wake_buffer = b""

        try:
            socket_session = await self.async_open_classic_socket(mac, timeout=timeout)
            reader = socket_session.reader
            writer = socket_session.writer
            status_buffer += socket_session.initial_bytes
            wake_buffer += socket_session.initial_bytes

            loop = asyncio.get_running_loop()
            per_packet_window = max(1.0, min(2.0, timeout / max(1, len(query_steps) + 1)))

            async def _drain_for(window_seconds: float) -> None:
                nonlocal status_buffer, wake_buffer
                deadline = loop.time() + window_seconds
                while loop.time() < deadline:
                    try:
                        chunk = await asyncio.wait_for(
                            reader.read(4096),
                            timeout=min(0.45, max(0.05, deadline - loop.time())),
                        )
                    except asyncio.TimeoutError:
                        continue
                    if not chunk:
                        break

                    status_buffer += chunk
                    wake_buffer += chunk

                    wake_packets, wake_buffer = extract_classic_wake_packets(wake_buffer)
                    if wake_packets:
                        _LOGGER.debug(
                            "Classic socket wake-up packet(s) received for %s: %s",
                            mac,
                            [packet.hex() for packet in wake_packets],
                        )
                        writer.write(bytes.fromhex(CLASSIC_WAKE_REPLY_PACKET))
                        await writer.drain()

                    packets, status_buffer = extract_classic_status_packets(status_buffer)
                    for raw_packet in packets:
                        decoded = decode_observed_status_packet(raw_packet)
                        if decoded is None:
                            continue
                        sub_type, status = decoded
                        observed[sub_type] = status

                    if normalized_sub_types and all(
                        sub_type in observed for sub_type in normalized_sub_types
                    ):
                        break

            for packet, settle_seconds in query_steps:
                writer.write(bytes.fromhex(packet))
                await writer.drain()
                await _drain_for(min(per_packet_window, settle_seconds))
                if normalized_sub_types and all(
                    sub_type in observed for sub_type in normalized_sub_types
                ):
                    return observed

            final_deadline = loop.time() + 1.0
            while loop.time() < final_deadline:
                try:
                    chunk = await asyncio.wait_for(
                        reader.read(4096),
                        timeout=min(0.5, max(0.05, final_deadline - loop.time())),
                    )
                except asyncio.TimeoutError:
                    continue
                if not chunk:
                    break

                status_buffer += chunk
                wake_buffer += chunk

                wake_packets, wake_buffer = extract_classic_wake_packets(wake_buffer)
                if wake_packets:
                    _LOGGER.debug(
                        "Classic socket wake-up packet(s) received for %s: %s",
                        mac,
                        [packet.hex() for packet in wake_packets],
                    )
                    writer.write(bytes.fromhex(CLASSIC_WAKE_REPLY_PACKET))
                    await writer.drain()

                packets, status_buffer = extract_classic_status_packets(status_buffer)
                for raw_packet in packets:
                    decoded = decode_observed_status_packet(raw_packet)
                    if decoded is None:
                        continue
                    sub_type, status = decoded
                    observed[sub_type] = status

            return observed
        except (asyncio.TimeoutError, OSError) as err:
            _LOGGER.debug("Classic socket query failed for %s: %s", mac, err)
            return {}
        finally:
            if writer is not None:
                writer.close()
                try:
                    await writer.wait_closed()
                except OSError:
                    pass

    async def _authorized_request(
        self, method: str, path: str, **kwargs: Any
    ) -> Any:
        """Run an authenticated request and retry once after refreshing a token."""
        token = await self._ensure_access_token()
        headers = dict(kwargs.pop("headers", {}))
        headers.setdefault("Accept", "application/json")
        headers["Authorization"] = f"{token.token_type} {token.access_token}".strip()

        try:
            return await self._request_json(method, path, headers=headers, **kwargs)
        except AifaSmartAuthError:
            token = await self._ensure_access_token(force_refresh=True)
            headers["Authorization"] = f"{token.token_type} {token.access_token}".strip()
            return await self._request_json(method, path, headers=headers, **kwargs)

    async def _ensure_access_token(
        self, *, force_login: bool = False, force_refresh: bool = False
    ) -> AifaTokens:
        """Make sure an access token is available."""
        async with self._token_lock:
            if (
                not force_login
                and not force_refresh
                and self._tokens is not None
                and not self._tokens.expiring_soon()
            ):
                return self._tokens

            if (
                not force_login
                and self._tokens is not None
                and self._tokens.refresh_token
                and (force_refresh or self._tokens.expiring_soon())
            ):
                # A revoked refresh token surfaces as AifaSmartAuthError, which
                # the coordinator translates into ConfigEntryAuthFailed so HA
                # starts the reauth flow.
                self._store_tokens(
                    await self._async_refresh_token(self._tokens.refresh_token)
                )
                return self._tokens  # type: ignore[return-value]

            if not self._email or not self._password:
                raise AifaSmartAuthError("Missing AIFA Smart credentials")

            self._store_tokens(
                await self._async_login_password(self._email, self._password)
            )
            return self._tokens  # type: ignore[return-value]

    async def _async_login_password(self, email: str, password: str) -> AifaTokens:
        """Exchange account credentials for an access token."""
        payload = _oauth_payload("password", email=email, password=password)
        response = await self._request_json(
            "POST",
            OAUTH_TOKEN_PATH,
            json=payload,
            auth_request=True,
        )
        return self._parse_tokens(response)

    async def _async_refresh_token(self, refresh_token: str) -> AifaTokens:
        """Refresh the current access token."""
        payload = _oauth_payload("refresh_token", refresh_token=refresh_token)
        response = await self._request_json(
            "POST",
            OAUTH_TOKEN_PATH,
            json=payload,
            auth_request=True,
        )
        if "refresh_token" not in response and "refreshToken" not in response:
            response = dict(response)
            response["refresh_token"] = refresh_token
        return self._parse_tokens(response)

    def _parse_tokens(self, payload: Mapping[str, Any]) -> AifaTokens:
        """Normalize an OAuth token response."""
        access_token = _pick(payload, "access_token", "accessToken")
        if not access_token:
            raise AifaSmartAuthError("AIFA Smart token response did not include access_token")

        refresh_token = _pick(payload, "refresh_token", "refreshToken")
        expires_in = _pick(payload, "expires_in", "expiresIn")
        expires_at = None
        if expires_in is not None:
            try:
                expires_at = datetime.now(UTC) + timedelta(seconds=int(expires_in))
            except (TypeError, ValueError):
                expires_at = None

        return AifaTokens(
            access_token=str(access_token),
            refresh_token=str(refresh_token) if refresh_token else None,
            token_type=str(_pick(payload, "token_type", "tokenType") or "Bearer"),
            expires_at=expires_at,
        )

    async def _request_json(
        self,
        method: str,
        path: str,
        *,
        auth_request: bool = False,
        **kwargs: Any,
    ) -> Any:
        """Run an HTTP request and decode JSON responses."""
        url = f"{API_BASE_URL}{path}"
        headers = dict(kwargs.pop("headers", {}))
        headers.setdefault("Accept", "application/json")
        if "json" in kwargs:
            headers.setdefault("Content-Type", "application/json")

        try:
            async with self._session.request(
                method,
                url,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=30),
                **kwargs,
            ) as response:
                text = await response.text()
        except asyncio.TimeoutError as err:
            raise AifaSmartConnectionError("AIFA Smart API timed out") from err
        except aiohttp.ClientError as err:
            raise AifaSmartConnectionError("Unable to reach AIFA Smart API") from err

        try:
            payload = json.loads(text) if text else {}
        except json.JSONDecodeError:
            payload = {"raw": text} if text else {}

        if response.status in {401, 403}:
            raise AifaSmartAuthError(
                self._error_message(payload) or "Authentication failed"
            )

        if response.status >= 400:
            if auth_request and response.status == 400:
                raise AifaSmartAuthError(self._error_message(payload) or "Authentication failed")
            raise AifaSmartApiError(
                self._error_message(payload) or f"Unexpected AIFA Smart API error: {response.status}",
                status=response.status,
                code=_coerce_int(_pick(payload, "code")),
                name=str(_pick(payload, "name") or "") or None,
                payload=payload,
            )

        return payload

    def _error_message(self, payload: Any) -> str | None:
        """Extract the best available error message from a response payload."""
        if not isinstance(payload, Mapping):
            return None
        return str(_pick(payload, "message", "error", "name") or "").strip() or None


def _coerce_int(value: Any) -> int | None:
    """Convert a value to int."""
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
