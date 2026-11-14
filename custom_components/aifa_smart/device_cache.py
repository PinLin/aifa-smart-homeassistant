"""Device-list cache that lets LAN and auto modes start while the cloud is down.

Only whitelisted structural fields are written. Cloud `raw` payloads,
functions, sensor readings and anything token-like never reach the file.
"""
from __future__ import annotations

import logging
from collections.abc import Iterable, Mapping
from datetime import datetime
from typing import TYPE_CHECKING, Any

from homeassistant.util import dt as dt_util

from .api import AifaDevice, AifaMacro, AifaMacroCommand, AifaSubDevice
from .const import DOMAIN

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)

CACHE_VERSION = 1

_DEVICE_FIELDS = ("id", "name", "mac", "device_type", "firmware", "version", "subversion")
_SUB_DEVICE_FIELDS = (
    "id",
    "device_id",
    "name",
    "type",
    "sub_type",
    "device_code",
    "device_code_brand_id",
    "device_code_brand_name",
    "device_code_brand_localized_name",
    "device_code_country",
    "device_code_version",
    "device_code_subversion",
    "device_code_type",
    "device_code_remote",
    "device_code_popular",
    "device_code_model_name",
    "available_brand_count",
    "available_device_code_count",
    "ac_available_modes",
    "ac_classic_temp_control",
    "ac_extra_windspeed",
    "ac_show_display_mold",
    "ac_single_mode",
    "ac_dehumidifier",
    "ac_supports_sleep",
    "ac_supports_power_saving",
    "ac_supports_turbo",
    "estimated_capabilities",
    "learned_key_count",
)
_MACRO_COMMAND_FIELDS = (
    "id",
    "macro_id",
    "device_id",
    "sub_device_id",
    "command",
    "delay",
    "order_no",
)
_REQUIRED_STR = ("id",)


def cache_store_key(entry_id: str) -> str:
    return f"{DOMAIN}.{entry_id}.devices"


def _pick_fields(obj: object, fields: Iterable[str]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for name in fields:
        value = getattr(obj, name)
        result[name] = list(value) if isinstance(value, list) else value
    return result


def serialize_account(devices: Mapping[str, AifaDevice], macros: Iterable[AifaMacro]) -> dict[str, Any]:
    """Return the JSON-ready cache body for a device snapshot."""
    return {
        "devices": [
            {
                **_pick_fields(device, _DEVICE_FIELDS),
                "sub_devices": [
                    _pick_fields(sub_device, _SUB_DEVICE_FIELDS)
                    for sub_device in device.sub_devices
                ],
            }
            for device in devices.values()
        ],
        "macros": [
            {
                "id": macro.id,
                "name": macro.name,
                "commands": [
                    _pick_fields(command, _MACRO_COMMAND_FIELDS) for command in macro.commands
                ],
            }
            for macro in macros
        ],
    }


def _check_row(row: Any, fields: Iterable[str]) -> Mapping[str, Any]:
    if not isinstance(row, Mapping):
        raise ValueError("cache row is not an object")
    for name in _REQUIRED_STR:
        if not isinstance(row.get(name), str) or not row[name]:
            raise ValueError(f"cache row has no {name}")
    return {name: row.get(name) for name in fields}


def deserialize_account(
    body: Any,
) -> tuple[dict[str, AifaDevice], list[AifaMacro]]:
    """Rebuild devices and macros; raise ValueError for anything malformed."""
    if not isinstance(body, Mapping):
        raise ValueError("cache body is not an object")
    devices: dict[str, AifaDevice] = {}
    for raw_device in body.get("devices") or ():
        values = dict(_check_row(raw_device, _DEVICE_FIELDS))
        for int_field in ("version", "subversion"):
            if values[int_field] is not None and not isinstance(values[int_field], int):
                raise ValueError(f"cache field {int_field} is not an integer")
        sub_devices = []
        for raw_sub in raw_device.get("sub_devices") or ():
            sub_values = dict(_check_row(raw_sub, _SUB_DEVICE_FIELDS))
            for list_field in ("ac_available_modes", "estimated_capabilities"):
                sub_values[list_field] = list(sub_values.get(list_field) or [])
            learned = sub_values.get("learned_key_count")
            sub_values["learned_key_count"] = learned if isinstance(learned, int) else 0
            sub_devices.append(AifaSubDevice(**sub_values))
        devices[values["id"]] = AifaDevice(
            **values,
            # Cloud-only facts are unknown offline; readings stay unavailable.
            online=None,
            temperature=None,
            humidity=None,
            sub_devices=sub_devices,
        )
    macros: list[AifaMacro] = []
    for raw_macro in body.get("macros") or ():
        macro_values = _check_row(raw_macro, ("id", "name"))
        commands = [
            AifaMacroCommand(**_check_row(raw_command, _MACRO_COMMAND_FIELDS))
            for raw_command in raw_macro.get("commands") or ()
        ]
        macros.append(
            AifaMacro(
                id=macro_values["id"],
                name=str(macro_values.get("name") or ""),
                commands=commands,
            )
        )
    return devices, macros


class AifaDeviceCache:
    """Per config entry cache in `.storage`, written only when it changes."""

    def __init__(self, hass: HomeAssistant, entry_id: str) -> None:
        from homeassistant.helpers.storage import Store

        self._store: Store[dict[str, Any]] = Store(
            hass, CACHE_VERSION, cache_store_key(entry_id), private=True
        )
        self._last_body: dict[str, Any] | None = None

    async def async_save(
        self, devices: Mapping[str, AifaDevice], macros: Iterable[AifaMacro]
    ) -> None:
        body = serialize_account(devices, macros)
        if body == self._last_body:
            return
        await self._store.async_save({**body, "saved_at": dt_util.utcnow().isoformat()})
        self._last_body = body

    async def async_load(
        self,
    ) -> tuple[dict[str, AifaDevice], list[AifaMacro], datetime | None] | None:
        """Return the cached snapshot, or None when missing or unreadable."""
        try:
            stored = await self._store.async_load()
        except Exception as err:  # noqa: BLE001 - a bad cache must not block setup
            _LOGGER.warning("AIFA device cache unreadable, discarding it: %s", err)
            await self.async_remove()
            return None
        if stored is None:
            return None
        try:
            devices, macros = deserialize_account(stored)
        except (TypeError, ValueError) as err:
            _LOGGER.warning("AIFA device cache malformed, discarding it: %s", err)
            await self.async_remove()
            return None
        try:
            saved_at = dt_util.parse_datetime(str(stored.get("saved_at") or ""))
        except ValueError:
            saved_at = None
        return devices, macros, saved_at

    async def async_remove(self) -> None:
        self._last_body = None
        try:
            await self._store.async_remove()
        except OSError as err:
            _LOGGER.debug("Unable to remove AIFA device cache: %s", err)
