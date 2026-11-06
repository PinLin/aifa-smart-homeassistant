"""Sensor entities for AIFA Smart."""
from __future__ import annotations

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import PERCENTAGE, UnitOfTemperature
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .ac import AC_KEYS, FAN_SPEED_OPTIONS, is_supported_ac_sub_device, visible_ac_keys
from .coordinator import AifaSmartCoordinator
from .entity import (
    AifaAccountEntity,
    AifaDeviceEntity,
    AifaSubDeviceEntity,
    async_remove_stale_sub_device_entities,
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up AIFA Smart sensors from a config entry."""
    coordinator: AifaSmartCoordinator = entry.runtime_data

    entities: list[SensorEntity] = [
        AifaMacroCountSensor(entry, coordinator),
    ]
    for device in coordinator.data.devices.values():
        entities.extend(
            [
                AifaTemperatureSensor(coordinator, device.id),
                AifaHumiditySensor(coordinator, device.id),
                AifaFirmwareSensor(coordinator, device.id),
            ]
        )
        for sub_device in device.sub_devices:
            entities.extend(
                [
                    AifaSubDeviceCodeSensor(coordinator, device.id, sub_device.id),
                    AifaSubDeviceCodeSourceSensor(coordinator, device.id, sub_device.id),
                    AifaSubDeviceBrandSensor(coordinator, device.id, sub_device.id),
                    AifaSubDeviceFunctionCountSensor(coordinator, device.id, sub_device.id),
                ]
            )
            # Shown with the fan key, the only way to change the speed.
            if is_supported_ac_sub_device(
                sub_device.type, sub_device.device_code
            ) and AC_KEYS["fan_speed"] in visible_ac_keys(sub_device.device_code):
                entities.append(AifaAcFanSpeedSensor(coordinator, device.id, sub_device.id))
    async_remove_stale_sub_device_entities(
        hass,
        entry,
        coordinator.data.devices,
        "sensor",
        ("fan_speed",),
        {entity.unique_id for entity in entities if entity.unique_id is not None},
    )
    async_add_entities(entities)


class _AifaAccountBaseSensor(AifaAccountEntity, SensorEntity):
    """Shared base sensor for account-level diagnostics."""

    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _state_attrs = ("native_value", "extra_state_attributes", "available")

    def __init__(
        self,
        entry: ConfigEntry,
        coordinator: AifaSmartCoordinator,
        attribute: str,
    ) -> None:
        super().__init__(coordinator, entry, attribute, platform="sensor")


class _AifaBaseSensor(AifaDeviceEntity, SensorEntity):
    """Shared base sensor for a single AIFA device."""

    _state_attrs = ("native_value", "extra_state_attributes", "available")

    def __init__(
        self, coordinator: AifaSmartCoordinator, device_id: str, attribute: str
    ) -> None:
        super().__init__(coordinator, device_id, attribute, platform="sensor")


class AifaMacroCountSensor(_AifaAccountBaseSensor):
    """Diagnostic sensor for the number of saved macros."""

    _attr_translation_key = "macro_count"

    def __init__(self, entry: ConfigEntry, coordinator: AifaSmartCoordinator) -> None:
        super().__init__(entry, coordinator, "macro_count")

    @property
    def native_value(self) -> int | None:
        data = self.coordinator.data
        return None if data is None else len(data.macros)

    @property
    def extra_state_attributes(self):
        """Return account diagnostics plus a readable macro summary."""
        attributes = super().extra_state_attributes
        data = self.coordinator.data
        if data is None:
            return attributes
        attributes["macros"] = [
            {
                "id": macro.id,
                "name": macro.name,
                "command_count": len(macro.commands),
            }
            for macro in data.macros
        ]
        return attributes


class AifaTemperatureSensor(_AifaBaseSensor):
    """Device temperature sensor."""

    _attr_translation_key = "temperature"
    _attr_device_class = SensorDeviceClass.TEMPERATURE
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS
    _attr_suggested_display_precision = 1

    def __init__(self, coordinator: AifaSmartCoordinator, device_id: str) -> None:
        super().__init__(coordinator, device_id, "temperature")

    @property
    def native_value(self) -> float | None:
        device = self.device
        return None if device is None else device.temperature


class AifaHumiditySensor(_AifaBaseSensor):
    """Device humidity sensor."""

    _attr_translation_key = "humidity"
    _attr_device_class = SensorDeviceClass.HUMIDITY
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = PERCENTAGE
    _attr_suggested_display_precision = 1

    def __init__(self, coordinator: AifaSmartCoordinator, device_id: str) -> None:
        super().__init__(coordinator, device_id, "humidity")

    @property
    def native_value(self) -> float | None:
        device = self.device
        return None if device is None else device.humidity


class AifaFirmwareSensor(_AifaBaseSensor):
    """Device firmware sensor."""

    _attr_translation_key = "firmware"
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: AifaSmartCoordinator, device_id: str) -> None:
        super().__init__(coordinator, device_id, "firmware")

    @property
    def native_value(self) -> str | None:
        device = self.device
        return None if device is None else device.firmware


class _AifaBaseSubDeviceSensor(AifaSubDeviceEntity, SensorEntity):
    """Shared base sensor for a single sub-device."""

    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _state_attrs = ("native_value", "extra_state_attributes", "available")

    def __init__(
        self,
        coordinator: AifaSmartCoordinator,
        device_id: str,
        sub_device_id: str,
        attribute: str,
    ) -> None:
        super().__init__(
            coordinator, device_id, sub_device_id, attribute, platform="sensor"
        )


class AifaSubDeviceCodeSensor(_AifaBaseSubDeviceSensor):
    """Diagnostic sensor for the learned control code of a sub-device."""

    _attr_translation_key = "sub_device_code"

    def __init__(
        self, coordinator: AifaSmartCoordinator, device_id: str, sub_device_id: str
    ) -> None:
        super().__init__(coordinator, device_id, sub_device_id, "device_code")

    @property
    def native_value(self) -> str | None:
        sub_device = self.sub_device
        return None if sub_device is None else sub_device.device_code


class AifaSubDeviceCodeSourceSensor(_AifaBaseSubDeviceSensor):
    """Diagnostic sensor for whether the selected code comes from the online batch."""

    _attr_translation_key = "sub_device_code_source"
    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = ["catalog", "cloud"]

    def __init__(
        self, coordinator: AifaSmartCoordinator, device_id: str, sub_device_id: str
    ) -> None:
        super().__init__(coordinator, device_id, sub_device_id, "code_source")

    @property
    def native_value(self) -> str | None:
        sub_device = self.sub_device
        if sub_device is None or sub_device.device_code_remote is None:
            return None
        return "cloud" if sub_device.device_code_remote else "catalog"

    @property
    def extra_state_attributes(self):
        """Return common diagnostic attributes plus a direct cloud-code flag."""
        attributes = super().extra_state_attributes
        sub_device = self.sub_device
        if sub_device is None:
            return attributes
        attributes["is_cloud_code"] = sub_device.device_code_remote
        return attributes


class AifaSubDeviceBrandSensor(_AifaBaseSubDeviceSensor):
    """Diagnostic sensor for the selected AIFA catalog brand."""

    _attr_translation_key = "sub_device_brand"

    def __init__(
        self, coordinator: AifaSmartCoordinator, device_id: str, sub_device_id: str
    ) -> None:
        super().__init__(coordinator, device_id, sub_device_id, "brand")

    @property
    def native_value(self) -> str | None:
        sub_device = self.sub_device
        if sub_device is None:
            return None
        return sub_device.device_code_brand_localized_name or sub_device.device_code_brand_name


class AifaSubDeviceFunctionCountSensor(_AifaBaseSubDeviceSensor):
    """Diagnostic sensor for the number of saved functions on a sub-device."""

    _attr_translation_key = "sub_device_function_count"
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(
        self, coordinator: AifaSmartCoordinator, device_id: str, sub_device_id: str
    ) -> None:
        super().__init__(coordinator, device_id, sub_device_id, "function_count")

    @property
    def native_value(self) -> int | None:
        sub_device = self.sub_device
        return None if sub_device is None else len(sub_device.functions)


class AifaAcFanSpeedSensor(AifaSubDeviceEntity, SensorEntity):
    """Fan speed from the hub's last status report (b7)."""

    # Reports arrive over the command transport, so follow its availability.
    _control_entity = True
    _attr_translation_key = "ac_fan_speed"
    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = list(FAN_SPEED_OPTIONS)
    _state_attrs = ("native_value", "available")

    def __init__(
        self, coordinator: AifaSmartCoordinator, device_id: str, sub_device_id: str
    ) -> None:
        super().__init__(
            coordinator, device_id, sub_device_id, "fan_speed", platform="sensor"
        )

    @property
    def native_value(self) -> str | None:
        sub_device = self.sub_device
        return self.coordinator.get_ac_runtime_state(
            self._device_id,
            self._sub_device_id,
            device_code=None if sub_device is None else sub_device.device_code,
        ).last_reported_fan_mode
