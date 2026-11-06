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
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity

from .ac import (
    AIFA_AC_MAX_TEMP,
    AIFA_AC_MIN_TEMP,
    app_mode_buttons,
    get_ac_capabilities,
    get_default_mode,
    is_supported_ac_sub_device,
    shows_classic_temperature_keys,
)
from .const import DOMAIN
from .coordinator import AifaSmartCoordinator
from .entity import AifaSubDeviceEntity

_ATTR_HVAC_MODE = "hvac_mode"
_ATTR_LAST_ACTIVE_HVAC_MODE = "aifa_last_active_hvac_mode"
_ATTR_REQUESTED_HVAC_MODE = "aifa_requested_hvac_mode"
_ATTR_REQUESTED_TARGET_TEMPERATURE = "aifa_requested_target_temperature"
_ATTR_POWER_OFF_HELD = "aifa_power_off_held"
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
    # A single-mode AC has no mode buttons but still reports one of these.
    modes = app_mode_buttons(device_code) or get_ac_capabilities(device_code).available_modes
    for mode in modes:
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

    _control_entity = True
    # A hub report only proves the hub fired the IR code, not that the AC got it.
    _attr_assumed_state = True
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
            _CATALOG_TO_HVAC[mode] for mode in app_mode_buttons(device_code)
        ]
        self._attr_hvac_modes = _supported_hvac_modes(device_code)

        # i-Ctrl Pro has no fan or swing selection, only cycle keys (button
        # entities), so the climate entity offers neither.
        supported_features = (
            ClimateEntityFeature.TURN_ON | ClimateEntityFeature.TURN_OFF
        )
        hub = coordinator.get_device(device_id)
        # Where the app shows +/- keys instead of the slider, the AC takes only
        # relative key presses and never reports a setpoint.
        self._classic_temperature_keys = shows_classic_temperature_keys(
            None if hub is None else hub.version, device_code
        )
        if not self._classic_temperature_keys:
            supported_features |= ClimateEntityFeature.TARGET_TEMPERATURE
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
    def target_temperature(self) -> float | None:
        """Return the best available AC setpoint."""
        if self._classic_temperature_keys:
            return None
        return float(self.runtime.exposed_target_temperature)

    @property
    def hvac_modes(self) -> list[HVACMode]:
        """List only modes that can be selected, plus the one being shown."""
        if self._active_hvac_modes:
            return self._attr_hvac_modes
        # Single-mode: no mode buttons, so only off and the reported mode.
        mode = self.hvac_mode
        return [HVACMode.OFF] if mode == HVACMode.OFF else [HVACMode.OFF, mode]

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
        attributes.update(
            {
                # HA's own assumed_state attribute is always true here; this
                # says whether the shown state came from the hub's report.
                "aifa_state_assumed": runtime.is_assumed,
                "aifa_state_source": runtime.effective_state_source,
                "aifa_observed_state_available": runtime.observed_status is not None,
                "aifa_command_state_available": runtime.command_status is not None,
                "aifa_command_state_pending": runtime.command_status_pending,
                "aifa_command_state_matches_observed": runtime.command_status_matches_observed,
                "aifa_observed_fields": observed_fields,
                "last_status_command": runtime.last_status_command,
                "last_power_command": runtime.last_power_command,
                "aifa_available_modes": list(self._capabilities.available_modes),
                "aifa_anti_mold_modes": list(self._capabilities.anti_mold_modes),
                "aifa_supports_sleep": self._capabilities.supports_sleep,
                "aifa_supports_power_saving": self._capabilities.supports_power_saving,
                "aifa_supports_turbo": self._capabilities.supports_turbo,
                _ATTR_REQUESTED_HVAC_MODE: runtime.requested_hvac_mode,
                _ATTR_REQUESTED_TARGET_TEMPERATURE: runtime.requested_target_temperature,
                _ATTR_POWER_OFF_HELD: runtime.power_off_held,
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
        supported_mode_values = {mode.value for mode in self._attr_hvac_modes}

        requested_target = last_state.attributes.get(_ATTR_REQUESTED_TARGET_TEMPERATURE)
        if requested_target is not None:
            runtime.requested_target_temperature = int(float(requested_target))

        # The hub still reports the mode it had before the power off, so the
        # first report after a restart must not turn the AC back on.
        if last_state.attributes.get(_ATTR_POWER_OFF_HELD) is True:
            runtime.hold_power_off(None)

        restored_last_active = last_state.attributes.get(_ATTR_LAST_ACTIVE_HVAC_MODE)
        if restored_last_active in supported_mode_values and restored_last_active != HVACMode.OFF.value:
            runtime.last_active_hvac_mode = restored_last_active

    async def async_turn_on(self) -> None:
        """Send the power key only, like the app's On button."""
        await self.coordinator.async_apply_ac_state(
            self._device_id,
            self._sub_device_id,
            hvac_mode="on",
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
        if not self._active_hvac_modes and hvac_mode == self.hvac_mode:
            return
        if hvac_mode not in self._active_hvac_modes:
            # Single-mode ACs only have the cycling mode key (a button entity).
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="ac_mode_not_offered",
                translation_placeholders={"mode": hvac_mode},
            )
        await self.coordinator.async_apply_ac_state(
            self._device_id,
            self._sub_device_id,
            hvac_mode=_HVAC_TO_PACKET_MODE[hvac_mode],
            device_code=self.device_code,
        )

    async def async_set_temperature(self, **kwargs: Any) -> None:
        """Set a target temperature in the mode the AC is showing."""
        if ATTR_TEMPERATURE not in kwargs:
            raise HomeAssistantError("AIFA climate requires target temperature")

        requested_mode = kwargs.get(_ATTR_HVAC_MODE)
        if requested_mode is not None and HVACMode(requested_mode) != self.hvac_mode:
            await self.async_set_hvac_mode(HVACMode(requested_mode))
        if requested_mode == HVACMode.OFF:
            return

        await self.coordinator.async_apply_ac_state(
            self._device_id,
            self._sub_device_id,
            target_temperature=int(float(kwargs[ATTR_TEMPERATURE])),
            device_code=self.device_code,
        )
