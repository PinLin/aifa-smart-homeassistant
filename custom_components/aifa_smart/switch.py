"""Switch entities for AIFA Smart AC helper flags."""
from __future__ import annotations

from dataclasses import dataclass

from homeassistant.components.switch import SwitchEntity, SwitchEntityDescription
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .ac import get_default_mode, is_supported_ac_sub_device
from .coordinator import AifaSmartCoordinator
from .entity import AifaSubDeviceEntity


@dataclass(frozen=True, kw_only=True, slots=True)
class AifaAcFlagEntityDescription(SwitchEntityDescription):
    """Description for one AC helper switch."""

    attr_name: str


_AC_FLAG_SWITCHES: tuple[AifaAcFlagEntityDescription, ...] = (
    # Byte 10 feature flag bit positions (per ac.py build_status_command):
    #   sleep         → 0x08   VERIFIED 2026-04-27 from AVD AIFA app capture
    #   power_saving  → 0x02   INFERRED — AVD AIFA app's Power Saving button
    #                          did not toggle a bit in any captured packet.
    #   turbo         → 0x04   INFERRED — bit position not verified against a
    #                          Turbo-capable AC. Selected by elimination from
    #                          the byte-10 bit budget.
    AifaAcFlagEntityDescription(
        key="turbo",
        translation_key="turbo",
        attr_name="turbo",
    ),
    AifaAcFlagEntityDescription(
        key="sleep",
        translation_key="sleep",
        attr_name="sleep",
    ),
    AifaAcFlagEntityDescription(
        key="power_saving",
        translation_key="power_saving",
        attr_name="power_saving",
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up AIFA Smart switch entities."""
    coordinator: AifaSmartCoordinator = entry.runtime_data

    entities: list[SwitchEntity] = []
    for device in coordinator.data.devices.values():
        for sub_device in device.sub_devices:
            if not is_supported_ac_sub_device(sub_device.type, sub_device.device_code):
                continue
            if sub_device.ac_supports_sleep:
                entities.append(
                    AifaAcFlagSwitch(
                        coordinator,
                        device.id,
                        sub_device.id,
                        _AC_FLAG_SWITCHES[1],
                    )
                )
            if sub_device.ac_supports_power_saving:
                entities.append(
                    AifaAcFlagSwitch(
                        coordinator,
                        device.id,
                        sub_device.id,
                        _AC_FLAG_SWITCHES[2],
                    )
                )
            if sub_device.ac_supports_turbo:
                entities.append(
                    AifaAcFlagSwitch(
                        coordinator,
                        device.id,
                        sub_device.id,
                        _AC_FLAG_SWITCHES[0],
                    )
                )

    async_add_entities(entities)


class AifaAcFlagSwitch(AifaSubDeviceEntity, SwitchEntity):
    """Switch entity for a single AIFA AC boolean helper flag."""

    _attr_entity_category = None
    _state_attrs = ("is_on", "assumed_state", "extra_state_attributes", "available")

    def __init__(
        self,
        coordinator: AifaSmartCoordinator,
        device_id: str,
        sub_device_id: str,
        description: AifaAcFlagEntityDescription,
    ) -> None:
        super().__init__(
            coordinator,
            device_id,
            sub_device_id,
            description.key,
            platform="switch",
        )
        self.entity_description = description
        self._attr_translation_key = description.translation_key

    @property
    def runtime(self):
        """Return the shared runtime state."""
        return self.coordinator.get_ac_runtime_state(
            self._device_id,
            self._sub_device_id,
            device_code=None if self.sub_device is None else self.sub_device.device_code,
        )

    @property
    def assumed_state(self) -> bool:
        """Return True while the integration is still using requested fallback state."""
        return self.runtime.helper_flags_are_assumed

    @property
    def is_on(self) -> bool:
        """Return the current helper-flag state."""
        runtime = self.runtime
        if self.entity_description.attr_name == "turbo":
            return runtime.exposed_turbo
        if self.entity_description.attr_name == "sleep":
            return runtime.exposed_sleep
        return runtime.exposed_power_saving

    @property
    def extra_state_attributes(self) -> dict[str, object]:
        """Return helper diagnostics."""
        attrs = dict(super().extra_state_attributes)
        if not attrs:
            return attrs
        attrs["aifa_state_source"] = self.runtime.effective_state_source
        attrs["aifa_helper_flags_state_source"] = self.runtime.effective_helper_flags_state_source
        attrs["aifa_observed_state_available"] = self.runtime.observed_status is not None
        attrs["aifa_command_state_available"] = self.runtime.command_status is not None
        attrs["aifa_command_state_pending"] = self.runtime.command_status_pending
        attrs["aifa_command_state_matches_observed"] = self.runtime.command_status_matches_observed
        return attrs

    async def async_turn_on(self, **kwargs) -> None:
        """Enable the helper flag."""
        await self._async_apply(True)

    async def async_turn_off(self, **kwargs) -> None:
        """Disable the helper flag."""
        await self._async_apply(False)

    async def _async_apply(self, value: bool) -> None:
        """Send the updated helper flag through the shared AC packet path."""
        runtime = self.runtime
        hvac_mode = runtime.requested_hvac_mode
        if hvac_mode == "off":
            hvac_mode = runtime.last_active_hvac_mode or get_default_mode(
                None if self.sub_device is None else self.sub_device.device_code
            )

        if self.entity_description.attr_name == "turbo":
            kwargs = {"turbo": value}
        elif self.entity_description.attr_name == "sleep":
            kwargs = {"sleep": value}
        else:
            kwargs = {"power_saving": value}
        await self.coordinator.async_apply_ac_state(
            self._device_id,
            self._sub_device_id,
            hvac_mode=hvac_mode,
            device_code=None if self.sub_device is None else self.sub_device.device_code,
            **kwargs,
        )
