"""Entity helpers for AIFA Smart."""
from __future__ import annotations

from typing import TYPE_CHECKING, Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import callback
from homeassistant.helpers.device_registry import (
    CONNECTION_NETWORK_MAC,
    DeviceInfo,
    format_mac,
)
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .api import AifaDevice, AifaSubDevice
from .const import DOMAIN, MANUFACTURER
from .device_meta import device_model_label, sub_device_model_label

if TYPE_CHECKING:
    from .coordinator import AifaAccountData, AifaSmartCoordinator


def account_identifier(entry_id: str) -> str:
    """Build a stable identifier for the virtual account device."""
    return f"account:{entry_id}"


def build_account_device_info(entry: ConfigEntry) -> DeviceInfo:
    """Build Home Assistant device metadata for account-level diagnostics."""
    return DeviceInfo(
        identifiers={(DOMAIN, account_identifier(entry.entry_id))},
        manufacturer=MANUFACTURER,
        name="AIFA Smart Account",
        model="AIFA Cloud Account",
    )


def build_device_info(device: AifaDevice) -> DeviceInfo:
    """Build Home Assistant device metadata."""
    info = DeviceInfo(
        identifiers={(DOMAIN, device.id)},
        manufacturer=MANUFACTURER,
        name=device.name,
        model=device_model_label(device),
        sw_version=device.firmware,
    )
    if device.mac:
        info["connections"] = {(CONNECTION_NETWORK_MAC, format_mac(device.mac))}
    return info


def build_sub_device_info(device: AifaDevice, sub_device: AifaSubDevice) -> DeviceInfo:
    """Build Home Assistant device metadata for a single sub-device.

    Use the bare sub-device name; the parent hub linkage is conveyed by
    `via_device` so the HA UI shows "via i-Ctrl AC-XXXX" without us having
    to repeat the parent name in every sub-device label.
    """
    return DeviceInfo(
        identifiers={(DOMAIN, f"{device.id}:{sub_device.id}")},
        manufacturer=MANUFACTURER,
        name=sub_device.name,
        model=sub_device_model_label(sub_device),
        sw_version=device.firmware,
        via_device=(DOMAIN, device.id),
    )


def entity_unique_id(device_id: str, attribute: str) -> str:
    """Build a stable entity unique ID."""
    return f"{DOMAIN}_{device_id}_{attribute}"


def sub_device_entity_unique_id(device_id: str, sub_device_id: str, attribute: str) -> str:
    """Build a stable unique ID for a sub-device entity."""
    return entity_unique_id(f"{device_id}_{sub_device_id}", attribute)


def device_entity_id(platform: str, device_id: str, attribute: str) -> str:
    """Build a stable entity_id for a device-scoped entity.

    # Why: HA's slugify pinyin-transliterates Chinese device names — using
    # device.id keeps entity_ids deterministic and ASCII regardless of locale.
    """
    return f"{platform}.{DOMAIN}_{device_id}_{attribute}"


def sub_device_entity_id(
    platform: str, device_id: str, sub_device_id: str, attribute: str
) -> str:
    """Build a stable entity_id for a sub-device-scoped entity."""
    return f"{platform}.{DOMAIN}_{device_id}_{sub_device_id}_{attribute}"


def account_entity_id(platform: str, entry: ConfigEntry, attribute: str) -> str:
    """Build a stable entity_id for an account-level entity."""
    return f"{platform}.{DOMAIN}_account_{entry.entry_id}_{attribute}"


def common_attributes(device: AifaDevice) -> dict[str, Any]:
    """Shared extra attributes for all device entities."""
    return {
        "device_id": device.id,
        "device_type": device.device_type,
        "sub_device_count": len(device.sub_devices),
        "function_count": len(device.functions),
        "last_update": device.updated_at.isoformat() if device.updated_at else None,
    }


def common_account_attributes(entry: ConfigEntry, data: AifaAccountData) -> dict[str, Any]:
    """Shared extra attributes for all account-level entities."""
    return {
        "entry_id": entry.entry_id,
        "device_count": len(data.devices),
        "macro_count": len(data.macros),
        "last_update": data.fetched_at.isoformat(),
    }


class StateBroadcastDedupMixin:
    """Skip async_write_ha_state when tracked properties haven't changed.

    Each coordinator refresh fans out to every entity's
    ``_handle_coordinator_update``. With dozens of sensors / switches /
    buttons per AIFA account, that turns into a lot of HA state writes
    for a payload that is mostly identical between cycles. Subclasses
    declare which properties drive their visible state via
    ``_state_attrs`` and inherit this mixin alongside ``CoordinatorEntity``;
    an empty tuple keeps the default broadcast-on-every-refresh behaviour.
    """

    _state_attrs: tuple[str, ...] = ()
    _last_broadcast_state: tuple[Any, ...] | None = None

    async def async_added_to_hass(self) -> None:
        """Seed the last-broadcast snapshot so the first real update is honest."""
        await super().async_added_to_hass()  # type: ignore[misc]
        self._refresh_last_broadcast()

    def _refresh_last_broadcast(self) -> bool:
        """Recompute the snapshot. Return True if it differs from the prior one."""
        if not self._state_attrs:
            return True
        snapshot = tuple(getattr(self, attr, None) for attr in self._state_attrs)
        if snapshot != self._last_broadcast_state:
            self._last_broadcast_state = snapshot
            return True
        return False

    @callback
    def _handle_coordinator_update(self) -> None:
        """Only broadcast when the entity's tracked state actually changed."""
        if self._refresh_last_broadcast():
            self.async_write_ha_state()  # type: ignore[attr-defined]


class AifaAccountEntity(StateBroadcastDedupMixin, CoordinatorEntity["AifaSmartCoordinator"]):
    """Base class for entities attached to the virtual AIFA account device."""

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: "AifaSmartCoordinator",
        entry: ConfigEntry,
        attribute: str,
        *,
        platform: str,
    ) -> None:
        super().__init__(coordinator)
        self._entry = entry
        self._attribute = attribute
        self._attr_unique_id = entity_unique_id(f"account_{entry.entry_id}", attribute)
        self.entity_id = account_entity_id(platform, entry, attribute)

    @property
    def available(self) -> bool:
        return (
            self.coordinator.last_update_success and self.coordinator.data is not None
        )

    @property
    def device_info(self) -> DeviceInfo:
        return build_account_device_info(self._entry)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        data = self.coordinator.data
        return {} if data is None else common_account_attributes(self._entry, data)


class AifaDeviceEntity(StateBroadcastDedupMixin, CoordinatorEntity["AifaSmartCoordinator"]):
    """Base class for entities attached to a top-level AIFA device."""

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: "AifaSmartCoordinator",
        device_id: str,
        attribute: str,
        *,
        platform: str,
    ) -> None:
        super().__init__(coordinator)
        self._device_id = device_id
        self._attribute = attribute
        self._attr_unique_id = entity_unique_id(device_id, attribute)
        self.entity_id = device_entity_id(platform, device_id, attribute)

    @property
    def device(self) -> AifaDevice | None:
        return self.coordinator.get_device(self._device_id)

    @property
    def available(self) -> bool:
        return self.coordinator.last_update_success and self.device is not None

    @property
    def device_info(self) -> DeviceInfo | None:
        device = self.device
        return build_device_info(device) if device is not None else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        device = self.device
        return common_attributes(device) if device is not None else {}


class AifaSubDeviceEntity(StateBroadcastDedupMixin, CoordinatorEntity["AifaSmartCoordinator"]):
    """Base class for entities attached to one AIFA sub-device."""

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: "AifaSmartCoordinator",
        device_id: str,
        sub_device_id: str,
        attribute: str,
        *,
        platform: str,
    ) -> None:
        super().__init__(coordinator)
        self._device_id = device_id
        self._sub_device_id = sub_device_id
        self._attribute = attribute
        self._attr_unique_id = sub_device_entity_unique_id(
            device_id, sub_device_id, attribute
        )
        self.entity_id = sub_device_entity_id(
            platform, device_id, sub_device_id, attribute
        )

    @property
    def device(self) -> AifaDevice | None:
        return self.coordinator.get_device(self._device_id)

    @property
    def sub_device(self) -> AifaSubDevice | None:
        return self.coordinator.get_sub_device(self._device_id, self._sub_device_id)

    @property
    def available(self) -> bool:
        return (
            self.coordinator.last_update_success
            and self.device is not None
            and self.sub_device is not None
        )

    @property
    def device_info(self) -> DeviceInfo | None:
        device = self.device
        sub_device = self.sub_device
        if device is None or sub_device is None:
            return None
        return build_sub_device_info(device, sub_device)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        device = self.device
        sub_device = self.sub_device
        if device is None or sub_device is None:
            return {}
        return common_sub_device_attributes(device, sub_device)


def common_sub_device_attributes(device: AifaDevice, sub_device: AifaSubDevice) -> dict[str, Any]:
    """Shared extra attributes for all sub-device entities."""
    attributes = common_attributes(device)
    attributes.update(
        {
            "sub_device_id": sub_device.id,
            "sub_device_name": sub_device.name,
            "sub_device_type": sub_device.type,
            "sub_device_sub_type": sub_device.sub_type,
            "sub_device_code": sub_device.device_code,
            "sub_device_brand_id": sub_device.device_code_brand_id,
            "sub_device_brand_name": sub_device.device_code_brand_name,
            "sub_device_brand_localized_name": sub_device.device_code_brand_localized_name,
            "sub_device_code_country": sub_device.device_code_country,
            "sub_device_code_version": sub_device.device_code_version,
            "sub_device_code_subversion": sub_device.device_code_subversion,
            "sub_device_code_type": sub_device.device_code_type,
            "sub_device_code_remote": sub_device.device_code_remote,
            "sub_device_code_popular": sub_device.device_code_popular,
            "sub_device_code_model_name": sub_device.device_code_model_name,
            "sub_device_ac_available_modes": sub_device.ac_available_modes,
            "sub_device_ac_classic_temp_control": sub_device.ac_classic_temp_control,
            "sub_device_ac_extra_windspeed": sub_device.ac_extra_windspeed,
            "sub_device_ac_show_display_mold": sub_device.ac_show_display_mold,
            "sub_device_ac_single_mode": sub_device.ac_single_mode,
            "sub_device_ac_dehumidifier": sub_device.ac_dehumidifier,
            "sub_device_ac_supports_sleep": sub_device.ac_supports_sleep,
            "sub_device_ac_supports_power_saving": sub_device.ac_supports_power_saving,
            "sub_device_ac_supports_turbo": sub_device.ac_supports_turbo,
            "sub_device_estimated_capabilities": sub_device.estimated_capabilities,
            "sub_device_available_brand_count": sub_device.available_brand_count,
            "sub_device_available_device_code_count": sub_device.available_device_code_count,
            "sub_device_function_count": len(sub_device.functions),
        }
    )
    return attributes
