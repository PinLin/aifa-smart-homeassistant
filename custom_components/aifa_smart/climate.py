"""Climate entities for AIFA Smart."""
from __future__ import annotations

from typing import Any, Final

from homeassistant.components.climate import ClimateEntity
from homeassistant.components.climate.const import (
    HVACAction,
    HVACMode,
    ClimateEntityFeature,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import ATTR_TEMPERATURE, UnitOfTemperature
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity

from .ac import (
    AIFA_AC_MAX_TEMP,
    AIFA_AC_MIN_TEMP,
    get_ac_capabilities,
    get_default_mode,
    get_supported_fan_modes,
    get_supported_swing_modes,
    is_supported_ac_sub_device,
)
from .coordinator import AifaSmartCoordinator
from .entity import AifaSubDeviceEntity

_ATTR_FAN_MODE = "fan_mode"
_ATTR_HVAC_MODE = "hvac_mode"
_ATTR_SWING_MODE = "swing_mode"
_ATTR_LAST_ACTIVE_HVAC_MODE = "aifa_last_active_hvac_mode"
_ATTR_REQUESTED_HVAC_MODE = "aifa_requested_hvac_mode"
_ATTR_REQUESTED_TARGET_TEMPERATURE = "aifa_requested_target_temperature"
_ATTR_REQUESTED_FAN_MODE = "aifa_requested_fan_mode"
_ATTR_REQUESTED_SWING_MODE = "aifa_requested_swing_mode"
_ATTR_REQUESTED_TURBO = "aifa_requested_turbo"
_ATTR_REQUESTED_SLEEP = "aifa_requested_sleep"
_ATTR_REQUESTED_POWER_SAVING = "aifa_requested_power_saving"
_CATALOG_TO_HVAC: Final[dict[str, HVACMode]] = {
    "auto": HVACMode.AUTO,
    "cool": HVACMode.COOL,
    "dry": HVACMode.DRY,
    "fan": HVACMode.FAN_ONLY,
    "fan_only": HVACMode.FAN_ONLY,
    "heat": HVACMode.HEAT,
}
_HVAC_TO_PACKET_MODE: Final[dict[HVACMode, str]] = {
    HVACMode.AUTO: "auto",
    HVACMode.COOL: "cool",
    HVACMode.DRY: "dry",
    HVACMode.FAN_ONLY: "fan",
    HVACMode.HEAT: "heat",
}
_HVAC_ACTION_BY_MODE: Final[dict[HVACMode, HVACAction]] = {
    HVACMode.AUTO: HVACAction.IDLE,
    HVACMode.COOL: HVACAction.COOLING,
    HVACMode.DRY: HVACAction.DRYING,
    HVACMode.FAN_ONLY: HVACAction.FAN,
    HVACMode.HEAT: HVACAction.HEATING,
}


def _supported_hvac_modes(device_code: str | None) -> list[HVACMode]:
    """Convert catalog mode names into Home Assistant HVAC modes."""
    hvac_modes: list[HVACMode] = [HVACMode.OFF]
    for mode in get_ac_capabilities(device_code).available_modes:
        hvac_mode = _CATALOG_TO_HVAC.get(mode)
        if hvac_mode is not None:
            hvac_modes.append(hvac_mode)
    return hvac_modes


def _default_active_hvac_mode(device_code: str | None) -> HVACMode:
    """Pick the preferred mode when the user turns the AC on."""
    return _CATALOG_TO_HVAC.get(get_default_mode(device_code), HVACMode.COOL)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up AIFA Smart climate entities."""
    coordinator: AifaSmartCoordinator = entry.runtime_data

    entities: list[ClimateEntity] = []
    for device in coordinator.data.devices.values():
        for sub_device in device.sub_devices:
            if is_supported_ac_sub_device(sub_device.type, sub_device.device_code):
                entities.append(AifaAirConditionerClimate(coordinator, device.id, sub_device.id))

    async_add_entities(entities)


class AifaAirConditionerClimate(AifaSubDeviceEntity, RestoreEntity, ClimateEntity):
    """Climate control surface backed by AIFA raw packets and best-available state."""

    _attr_translation_key = "climate"
    _attr_temperature_unit = UnitOfTemperature.CELSIUS
    _attr_target_temperature_step = 1
    _attr_min_temp = AIFA_AC_MIN_TEMP
    _attr_max_temp = AIFA_AC_MAX_TEMP

    def __init__(
        self,
        coordinator: AifaSmartCoordinator,
        device_id: str,
        sub_device_id: str,
    ) -> None:
        super().__init__(
            coordinator, device_id, sub_device_id, "climate", platform="climate"
        )

        sub_device = coordinator.get_sub_device(device_id, sub_device_id)
        device_code = None if sub_device is None else sub_device.device_code
        self._capabilities = get_ac_capabilities(device_code)
        self._active_hvac_modes = [
            mode for mode in _supported_hvac_modes(device_code) if mode != HVACMode.OFF
        ]
        self._attr_hvac_modes = _supported_hvac_modes(device_code)
        self._attr_fan_modes = list(get_supported_fan_modes(device_code))
        self._attr_swing_modes = list(get_supported_swing_modes(device_code))

        supported_features = (
            ClimateEntityFeature.TARGET_TEMPERATURE
            | ClimateEntityFeature.TURN_ON
            | ClimateEntityFeature.TURN_OFF
        )
        if self._attr_fan_modes:
            supported_features |= ClimateEntityFeature.FAN_MODE
        if self._attr_swing_modes:
            supported_features |= ClimateEntityFeature.SWING_MODE
        self._attr_supported_features = supported_features

        self.coordinator.get_ac_runtime_state(
            self._device_id,
            self._sub_device_id,
            device_code=self.device_code,
        )

    @property
    def runtime(self):
        """Return the shared runtime state for this AC."""
        return self.coordinator.get_ac_runtime_state(
            self._device_id,
            self._sub_device_id,
            device_code=self.device_code,
        )

    @property
    def assumed_state(self) -> bool:
        """Return True while Home Assistant is still showing requested fallback state.

        For 5-digit cloud codes (Plus protocol) the hub never echoes
        `acCommand` broadcasts, so any observed_status this entity exposes
        is stale Classic-mode residue from before the user switched. Force
        assumed=True in that case so HA UI consistently shows the '?' badge
        and users don't trust stale temp/mode readings.
        """
        if self.runtime.is_assumed:
            return True
        code = self.device_code
        if code is not None:
            try:
                if int(code) >= 10000:
                    return True
            except (TypeError, ValueError):
                pass
        return False

    @property
    def device_code(self) -> str | None:
        """Return the sub-device packet profile key."""
        sub_device = self.sub_device
        return None if sub_device is None else sub_device.device_code

    @property
    def current_temperature(self) -> float | None:
        """Return the latest sensed room temperature."""
        device = self.device
        return None if device is None else device.temperature

    @property
    def target_temperature(self) -> float:
        """Return the best available AC setpoint."""
        return float(self.runtime.exposed_target_temperature)

    @property
    def hvac_mode(self) -> HVACMode:
        """Return the best available HVAC mode."""
        requested = self.runtime.exposed_hvac_mode
        if requested == "off":
            return HVACMode.OFF
        return _CATALOG_TO_HVAC.get(requested, _default_active_hvac_mode(self.device_code))

    @property
    def hvac_action(self) -> HVACAction:
        """Return the current HVAC action."""
        mode = self.hvac_mode
        if mode == HVACMode.OFF:
            return HVACAction.OFF
        return _HVAC_ACTION_BY_MODE.get(mode, HVACAction.IDLE)

    @property
    def fan_mode(self) -> str | None:
        """Return the best available fan mode."""
        return self.runtime.exposed_fan_mode

    @property
    def swing_mode(self) -> str | None:
        """Return the best available swing mode."""
        return self.runtime.exposed_swing_mode

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return diagnostic metadata for the current packet profile."""
        attributes = dict(super().extra_state_attributes)
        sub_device = self.sub_device
        if not attributes or sub_device is None:
            return attributes

        runtime = self.runtime
        observed_fields = ["hvac_mode"]
        if runtime.observed_status is not None and runtime.observed_status.target_temperature is not None:
            observed_fields.append("target_temperature")
        if runtime.observed_status is not None and runtime.observed_status.fan_mode is not None:
            observed_fields.append("fan_mode")
        if runtime.observed_status is not None and runtime.observed_status.swing_mode is not None:
            observed_fields.append("swing_mode")
        if runtime.observed_status is not None and runtime.observed_status.turbo:
            observed_fields.append("turbo")
        if runtime.observed_status is not None and runtime.observed_status.sleep:
            observed_fields.append("sleep")
        if runtime.observed_status is not None and runtime.observed_status.power_saving:
            observed_fields.append("power_saving")
        attributes.update(
            {
                "assumed_state": runtime.is_assumed,
                "aifa_state_source": runtime.effective_state_source,
                "aifa_helper_flags_state_source": runtime.effective_helper_flags_state_source,
                "aifa_observed_state_available": runtime.observed_status is not None,
                "aifa_command_state_available": runtime.command_status is not None,
                "aifa_command_state_pending": runtime.command_status_pending,
                "aifa_command_state_matches_observed": runtime.command_status_matches_observed,
                "aifa_observed_fields": observed_fields,
                "last_status_command": runtime.last_status_command,
                "last_power_command": runtime.last_power_command,
                "turbo_enabled": runtime.exposed_turbo,
                "sleep_enabled": runtime.exposed_sleep,
                "power_saving_enabled": runtime.exposed_power_saving,
                "aifa_available_modes": list(self._capabilities.available_modes),
                "aifa_supports_sleep": self._capabilities.supports_sleep,
                "aifa_supports_power_saving": self._capabilities.supports_power_saving,
                "aifa_supports_turbo": self._capabilities.supports_turbo,
                _ATTR_REQUESTED_HVAC_MODE: runtime.requested_hvac_mode,
                _ATTR_REQUESTED_TARGET_TEMPERATURE: runtime.requested_target_temperature,
                _ATTR_REQUESTED_FAN_MODE: runtime.requested_fan_mode,
                _ATTR_REQUESTED_SWING_MODE: runtime.requested_swing_mode,
                _ATTR_REQUESTED_TURBO: runtime.requested_turbo,
                _ATTR_REQUESTED_SLEEP: runtime.requested_sleep,
                _ATTR_REQUESTED_POWER_SAVING: runtime.requested_power_saving,
                _ATTR_LAST_ACTIVE_HVAC_MODE: runtime.last_active_hvac_mode,
            }
        )
        return attributes

    async def async_added_to_hass(self) -> None:
        """Restore the last requested state on restart."""
        await super().async_added_to_hass()

        last_state = await self.async_get_last_state()
        if last_state is None:
            return

        runtime = self.runtime
        requested_mode = last_state.attributes.get(_ATTR_REQUESTED_HVAC_MODE)
        supported_mode_values = {mode.value for mode in self._attr_hvac_modes}
        if requested_mode in supported_mode_values:
            runtime.requested_hvac_mode = requested_mode

        requested_target = last_state.attributes.get(_ATTR_REQUESTED_TARGET_TEMPERATURE)
        if requested_target is not None:
            runtime.requested_target_temperature = int(float(requested_target))

        requested_fan_mode = last_state.attributes.get(_ATTR_REQUESTED_FAN_MODE)
        if requested_fan_mode in self._attr_fan_modes:
            runtime.requested_fan_mode = requested_fan_mode

        requested_swing_mode = last_state.attributes.get(_ATTR_REQUESTED_SWING_MODE)
        if requested_swing_mode in self._attr_swing_modes:
            runtime.requested_swing_mode = requested_swing_mode

        requested_turbo = last_state.attributes.get(_ATTR_REQUESTED_TURBO)
        if isinstance(requested_turbo, bool):
            runtime.requested_turbo = requested_turbo

        requested_sleep = last_state.attributes.get(_ATTR_REQUESTED_SLEEP)
        if isinstance(requested_sleep, bool):
            runtime.requested_sleep = requested_sleep

        requested_power_saving = last_state.attributes.get(_ATTR_REQUESTED_POWER_SAVING)
        if isinstance(requested_power_saving, bool):
            runtime.requested_power_saving = requested_power_saving

        restored_last_active = last_state.attributes.get(_ATTR_LAST_ACTIVE_HVAC_MODE)
        if restored_last_active in supported_mode_values and restored_last_active != HVACMode.OFF.value:
            runtime.last_active_hvac_mode = restored_last_active

    async def async_turn_on(self) -> None:
        """Turn the AC on using the last requested target state."""
        runtime = self.runtime
        requested_mode = runtime.requested_hvac_mode
        mode = requested_mode if requested_mode != "off" else runtime.last_active_hvac_mode
        if mode is None or mode == "off":
            mode = get_default_mode(self.device_code)
        await self.coordinator.async_apply_ac_state(
            self._device_id,
            self._sub_device_id,
            hvac_mode=mode,
            device_code=self.device_code,
        )

    async def async_turn_off(self) -> None:
        """Turn the AC off using the verified short power-off packet."""
        await self.coordinator.async_apply_ac_state(
            self._device_id,
            self._sub_device_id,
            hvac_mode="off",
            device_code=self.device_code,
        )

    async def async_set_hvac_mode(self, hvac_mode: HVACMode) -> None:
        """Set the HVAC mode."""
        if hvac_mode == HVACMode.OFF:
            await self.async_turn_off()
            return
        if hvac_mode not in self._active_hvac_modes:
            raise HomeAssistantError(f"Unsupported AIFA HVAC mode: {hvac_mode}")
        await self.coordinator.async_apply_ac_state(
            self._device_id,
            self._sub_device_id,
            hvac_mode=_HVAC_TO_PACKET_MODE[hvac_mode],
            device_code=self.device_code,
        )

    async def async_set_temperature(self, **kwargs: Any) -> None:
        """Set a target temperature."""
        if ATTR_TEMPERATURE not in kwargs:
            raise HomeAssistantError("AIFA climate requires target temperature")

        requested_mode = kwargs.get(_ATTR_HVAC_MODE)
        if isinstance(requested_mode, HVACMode):
            target_mode = requested_mode
        elif isinstance(requested_mode, str):
            target_mode = HVACMode(requested_mode)
        else:
            current_mode = self.hvac_mode
            target_mode = current_mode if current_mode != HVACMode.OFF else _default_active_hvac_mode(
                self.device_code
            )

        if target_mode == HVACMode.OFF:
            raise HomeAssistantError("Set a non-off HVAC mode before sending temperature")

        await self.coordinator.async_apply_ac_state(
            self._device_id,
            self._sub_device_id,
            hvac_mode=_HVAC_TO_PACKET_MODE[target_mode],
            target_temperature=int(float(kwargs[ATTR_TEMPERATURE])),
            device_code=self.device_code,
        )

    async def async_set_fan_mode(self, fan_mode: str) -> None:
        """Set the AC fan mode."""
        if fan_mode not in self._attr_fan_modes:
            raise HomeAssistantError(f"Unsupported AIFA fan mode: {fan_mode}")

        await self.coordinator.async_apply_ac_state(
            self._device_id,
            self._sub_device_id,
            hvac_mode=self.runtime.requested_hvac_mode
            if self.runtime.requested_hvac_mode != "off"
            else get_default_mode(self.device_code),
            fan_mode=fan_mode,
            device_code=self.device_code,
        )

    async def async_set_swing_mode(self, swing_mode: str) -> None:
        """Set the AC swing mode."""
        if swing_mode not in self._attr_swing_modes:
            raise HomeAssistantError(f"Unsupported AIFA swing mode: {swing_mode}")

        await self.coordinator.async_apply_ac_state(
            self._device_id,
            self._sub_device_id,
            hvac_mode=self.runtime.requested_hvac_mode
            if self.runtime.requested_hvac_mode != "off"
            else get_default_mode(self.device_code),
            swing_mode=swing_mode,
            device_code=self.device_code,
        )
