"""AIFA Smart integration."""
from __future__ import annotations

import asyncio
from datetime import datetime
from typing import TYPE_CHECKING


from .const import (
    CONF_ACCESS_TOKEN,
    CONF_EMAIL,
    CONF_REFRESH_TOKEN,
    CONF_TOKEN_EXPIRES_AT,
    CONF_TOKEN_TYPE,
    DOMAIN,
    MANUFACTURER,
)
from .device_meta import device_model_label, sub_device_model_label

if TYPE_CHECKING:
    from .api import AifaTokens
    from .coordinator import AifaSmartCoordinator
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant

    AifaConfigEntry = ConfigEntry[AifaSmartCoordinator]

PLATFORMS = [
    "sensor",
    "binary_sensor",
    "climate",
    "switch",
    "button",
]


def _tokens_from_entry(entry: ConfigEntry) -> AifaTokens | None:
    """Rebuild a token dataclass from persisted config entry data."""
    from .api import AifaTokens

    access_token = entry.data.get(CONF_ACCESS_TOKEN)
    if not access_token:
        return None

    expires_raw = entry.data.get(CONF_TOKEN_EXPIRES_AT)
    expires_at: datetime | None = None
    if expires_raw:
        try:
            expires_at = datetime.fromisoformat(str(expires_raw))
        except ValueError:
            expires_at = None

    return AifaTokens(
        access_token=str(access_token),
        refresh_token=entry.data.get(CONF_REFRESH_TOKEN) or None,
        token_type=str(entry.data.get(CONF_TOKEN_TYPE) or "Bearer"),
        expires_at=expires_at,
    )


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up AIFA Smart from a config entry."""
    from homeassistant.helpers.aiohttp_client import async_get_clientsession

    from .api import AifaSmartApiClient, AifaTokens
    from .coordinator import AifaSmartCoordinator

    session = async_get_clientsession(hass)

    def _persist_tokens(tokens: AifaTokens) -> None:
        """Write refreshed tokens back into the config entry."""
        hass.config_entries.async_update_entry(
            entry,
            data={
                **entry.data,
                CONF_ACCESS_TOKEN: tokens.access_token,
                CONF_REFRESH_TOKEN: tokens.refresh_token,
                CONF_TOKEN_TYPE: tokens.token_type,
                CONF_TOKEN_EXPIRES_AT: tokens.expires_at.isoformat()
                if tokens.expires_at is not None
                else None,
            },
        )

    client = AifaSmartApiClient(
        session,
        email=entry.data.get(CONF_EMAIL),
        tokens=_tokens_from_entry(entry),
        on_tokens_updated=_persist_tokens,
    )
    coordinator = AifaSmartCoordinator(hass, entry, client)

    # Catalog must initialize before first capability lookup so climate
    # entities pick up cached/live data rather than bundled defaults.
    from . import catalog
    catalog_unsub = await catalog.async_initialize(hass)
    entry.async_on_unload(catalog_unsub)

    await coordinator.async_config_entry_first_refresh()

    entry.runtime_data = coordinator
    entry.async_on_unload(coordinator.async_shutdown)
    coordinator.async_sync_background_tasks()
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    await _async_remove_obsolete_automation_entities(hass, entry.entry_id)
    await _async_clear_legacy_disabled_helper_flags(hass, entry.entry_id)
    await _async_sync_device_registry(hass, coordinator)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


_OBSOLETE_TIMER_IR_KEYS: tuple[str, ...] = (
    "timer_ir_value",
    "timer_ir_preset",
    "apply_timer_ir",
    "clear_timer_ir",
    "increase_timer_ir",
    "decrease_timer_ir",
)


def _is_obsolete_automation_entity(entity) -> bool:
    """Return True when an entity belongs to a removed AIFA feature set."""
    if entity.platform != DOMAIN:
        return False

    unique_id = entity.unique_id or ""
    if not unique_id.startswith(f"{DOMAIN}_"):
        return False

    # Legacy AIFA automation entities (removed in v0.x):
    if "_function_" in unique_id:
        entity_id = entity.entity_id or ""
        if entity_id.startswith("button."):
            return True
        if entity_id.startswith("sensor.") and unique_id.endswith("_schedule"):
            return True
        if entity_id.startswith("binary_sensor.") and unique_id.endswith("_active"):
            return True
        return False

    # Legacy timer IR entities (removed in v0.1.x — see AIFA_TIMER_IR_RESEARCH.md):
    if any(unique_id.endswith("_" + key) for key in _OBSOLETE_TIMER_IR_KEYS):
        return True

    return False


async def _async_remove_obsolete_automation_entities(
    hass: HomeAssistant, entry_id: str
) -> None:
    """Remove registry entities that belonged to removed AIFA feature sets."""
    from homeassistant.helpers import entity_registry as er

    entity_registry = er.async_get(hass)
    for entity in er.async_entries_for_config_entry(entity_registry, entry_id):
        if _is_obsolete_automation_entity(entity):
            entity_registry.async_remove(entity.entity_id)


async def _async_clear_legacy_disabled_helper_flags(
    hass: HomeAssistant, entry_id: str
) -> None:
    """Re-enable sleep/turbo helpers that were shipped disabled in older releases.

    Earlier versions set ``entity_registry_enabled_default=False`` on the
    sleep and turbo switches while the IR encoding was being verified. The
    flags now ship enabled by default, but HA leaves the existing
    ``disabled_by="integration"`` flag on already-registered entities — so
    upgrades alone do not re-enable them. Clear that flag here, but only when
    the integration itself was the disabler, so users who manually disabled
    the entity keep their preference.
    """
    from homeassistant.helpers import entity_registry as er

    entity_registry = er.async_get(hass)
    for entity in er.async_entries_for_config_entry(entity_registry, entry_id):
        if entity.disabled_by != er.RegistryEntryDisabler.INTEGRATION:
            continue
        unique_id = entity.unique_id or ""
        if unique_id.endswith("_sleep") or unique_id.endswith("_turbo"):
            entity_registry.async_update_entity(
                entity.entity_id, disabled_by=None
            )


async def _async_sync_device_registry(
    hass: HomeAssistant, coordinator: AifaSmartCoordinator
) -> None:
    """Update existing device-registry rows with the latest human-readable metadata."""
    from homeassistant.helpers import device_registry as dr

    device_registry = dr.async_get(hass)
    pending_parent_ids = {device.id for device in coordinator.data.devices.values()}
    pending_sub_ids = {
        f"{device.id}:{sub_device.id}"
        for device in coordinator.data.devices.values()
        for sub_device in device.sub_devices
    }

    # Platform setup creates device-registry rows asynchronously. Retry briefly so
    # updated human-readable labels win over whatever was first registered.
    for _ in range(10):
        for device in coordinator.data.devices.values():
            parent_identifier = (DOMAIN, device.id)
            if device.id in pending_parent_ids:
                parent_entry = device_registry.async_get_device(identifiers={parent_identifier})
                if parent_entry is not None:
                    device_registry.async_update_device(
                        parent_entry.id,
                        manufacturer=MANUFACTURER,
                        model=device_model_label(device),
                        name=device.name,
                        sw_version=device.firmware,
                    )
                    pending_parent_ids.discard(device.id)

            for sub_device in device.sub_devices:
                composite_id = f"{device.id}:{sub_device.id}"
                if composite_id not in pending_sub_ids:
                    continue
                sub_entry = device_registry.async_get_device(
                    identifiers={(DOMAIN, composite_id)}
                )
                if sub_entry is None:
                    continue
                device_registry.async_update_device(
                    sub_entry.id,
                    manufacturer=MANUFACTURER,
                    model=sub_device_model_label(sub_device),
                    name=sub_device.name,
                    sw_version=device.firmware,
                )
                pending_sub_ids.discard(composite_id)

        if not pending_parent_ids and not pending_sub_ids:
            break
        await asyncio.sleep(0.5)
