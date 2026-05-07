"""Binary sensor entities for AIFA Smart."""
from __future__ import annotations

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .coordinator import AifaSmartCoordinator
from .entity import AifaDeviceEntity


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up AIFA Smart binary sensors."""
    coordinator: AifaSmartCoordinator = entry.runtime_data
    async_add_entities(
        [AifaOnlineBinarySensor(coordinator, device.id) for device in coordinator.data.devices.values()]
    )


class AifaOnlineBinarySensor(AifaDeviceEntity, BinarySensorEntity):
    """Connectivity state for a single AIFA device."""

    _attr_translation_key = "online"
    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _state_attrs = ("is_on", "available")

    def __init__(self, coordinator: AifaSmartCoordinator, device_id: str) -> None:
        super().__init__(coordinator, device_id, "online", platform="binary_sensor")

    @property
    def is_on(self) -> bool | None:
        """Return current connectivity state."""
        device = self.device
        return None if device is None else device.online
