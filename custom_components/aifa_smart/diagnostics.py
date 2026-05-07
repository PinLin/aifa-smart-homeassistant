"""Diagnostics support for AIFA Smart."""
from __future__ import annotations

from dataclasses import asdict, is_dataclass
from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.device_registry import DeviceEntry

from . import catalog
from .const import DOMAIN
from .coordinator import AifaSmartCoordinator

# Sensitive fields that must never reach a downloaded diagnostics report.
# Covers OAuth tokens, raw credentials, and any cloud identifiers that could
# pivot back to the user's account or hardware (mac, serial, device_id).
TO_REDACT: set[str] = {
    "access_token",
    "refresh_token",
    "token",
    "password",
    "email",
    "mac",
    "serial",
    "serial_number",
    "device_id",
    "sub_device_id",
}


def _to_dict(value: Any) -> Any:
    """Best-effort serialise dataclasses / mappings / iterables for JSON output."""
    if is_dataclass(value) and not isinstance(value, type):
        return {key: _to_dict(item) for key, item in asdict(value).items()}
    if isinstance(value, dict):
        return {str(key): _to_dict(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_to_dict(item) for item in value]
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return value


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: ConfigEntry
) -> dict[str, Any]:
    """Return a redacted snapshot for support reports."""
    coordinator: AifaSmartCoordinator = entry.runtime_data
    data = coordinator.data

    snapshot: dict[str, Any] = {
        "entry": {
            "title": entry.title,
            "version": entry.version,
            "data": dict(entry.data),
            "options": dict(entry.options),
        },
        "coordinator": {
            "last_update_success": coordinator.last_update_success,
            "update_interval": (
                coordinator.update_interval.total_seconds()
                if coordinator.update_interval is not None
                else None
            ),
            "fetched_at": data.fetched_at.isoformat() if data is not None else None,
            "device_count": len(data.devices) if data is not None else 0,
            "macro_count": len(data.macros) if data is not None else 0,
        },
        "devices": (
            [_to_dict(device) for device in data.devices.values()]
            if data is not None
            else []
        ),
        "catalog": catalog.get_status(),
    }
    return async_redact_data(snapshot, TO_REDACT)


async def async_get_device_diagnostics(
    hass: HomeAssistant,
    entry: ConfigEntry,
    device: DeviceEntry,
) -> dict[str, Any]:
    """Return diagnostics scoped to a single AIFA device.

    Includes the AifaDevice payload the coordinator stores plus a
    roster of every entity registered for the device with its current
    state and attributes — enough to triage "this entity shows wrong
    value" bug reports without screenshots. Distinct from the
    config-entry dump, which covers the entire account at once.
    """
    coordinator: AifaSmartCoordinator = entry.runtime_data
    data = coordinator.data

    # Resolve the device id from identifiers. AIFA uses a couple of
    # identifier shapes today: top-level devices use {(DOMAIN, device.id)},
    # the virtual account device uses {(DOMAIN, "account:<entry_id>")}.
    device_id: str | None = None
    is_account_device = False
    for ident_domain, identifier in device.identifiers:
        if ident_domain != DOMAIN:
            continue
        if identifier.startswith("account:"):
            is_account_device = True
        else:
            device_id = identifier
        break

    matched_device = None
    if device_id and data is not None:
        matched_device = data.devices.get(device_id)

    ent_reg = er.async_get(hass)
    entities: list[dict[str, Any]] = []
    for ent in er.async_entries_for_device(
        ent_reg, device.id, include_disabled_entities=True
    ):
        state = hass.states.get(ent.entity_id)
        entities.append(
            {
                "entity_id": ent.entity_id,
                "unique_id": ent.unique_id,
                "platform": ent.platform,
                "domain": ent.domain,
                "translation_key": ent.translation_key,
                "device_class": ent.device_class or ent.original_device_class,
                "disabled_by": ent.disabled_by,
                "state": state.state if state else None,
                "attributes": dict(state.attributes) if state else None,
            }
        )

    snapshot = {
        "device": {
            "id": device.id,
            "name": device.name,
            "name_by_user": device.name_by_user,
            "manufacturer": device.manufacturer,
            "model": device.model,
            "identifiers": [list(i) for i in device.identifiers],
            "is_account_device": is_account_device,
        },
        "aifa_device_id": device_id,
        "aifa_device": _to_dict(matched_device) if matched_device is not None else None,
        "entities": entities,
    }
    return async_redact_data(snapshot, TO_REDACT)
