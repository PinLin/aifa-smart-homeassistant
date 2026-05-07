"""Button entities for forcing refresh and triggering saved AIFA macros."""
from __future__ import annotations

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import AifaSmartCoordinator
from .entity import build_account_device_info


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the force-refresh button and dynamic macro buttons."""
    coordinator: AifaSmartCoordinator = entry.runtime_data
    entities: list[ButtonEntity] = [AifaForceRefreshButton(coordinator, entry)]
    async_add_entities(entities)

    known_macro_ids: set[str] = set()

    @callback
    def _async_add_new_macros() -> None:
        if coordinator.data is None:
            return
        new_entities: list[ButtonEntity] = []
        for macro in coordinator.data.macros:
            if macro.id in known_macro_ids:
                continue
            known_macro_ids.add(macro.id)
            new_entities.append(AifaMacroButton(coordinator, entry, macro.id))
        if new_entities:
            async_add_entities(new_entities)

    _async_add_new_macros()
    entry.async_on_unload(coordinator.async_add_listener(_async_add_new_macros))


class AifaForceRefreshButton(CoordinatorEntity[AifaSmartCoordinator], ButtonEntity):
    """Manual coordinator refresh — bypasses the 60s polling cadence.

    Provides a UI-discoverable trigger for `coordinator.async_request_refresh()`
    so users don't need to call `homeassistant.update_entity` from Developer Tools.
    """

    _attr_has_entity_name = True
    _attr_translation_key = "force_refresh"
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(
        self,
        coordinator: AifaSmartCoordinator,
        entry: ConfigEntry,
    ) -> None:
        super().__init__(coordinator)
        self._entry = entry
        self._attr_unique_id = f"aifa_smart_force_refresh_{entry.entry_id}"
        self.entity_id = f"button.{DOMAIN}_force_refresh_{entry.entry_id[:6]}"

    @property
    def device_info(self):
        return build_account_device_info(self._entry)

    async def async_press(self) -> None:
        await self.coordinator.async_request_refresh()


class AifaMacroButton(CoordinatorEntity[AifaSmartCoordinator], ButtonEntity):
    """Button that triggers a saved AIFA macro."""

    _attr_has_entity_name = False
    _attr_icon = "mdi:script-text-play"

    def __init__(
        self,
        coordinator: AifaSmartCoordinator,
        entry: ConfigEntry,
        macro_id: str,
    ) -> None:
        super().__init__(coordinator)
        self._macro_id = macro_id
        self._entry = entry
        self._attr_unique_id = f"aifa_smart_macro_{macro_id}"
        self.entity_id = f"button.{DOMAIN}_macro_{macro_id}"

    @property
    def macro(self):
        """Return the latest snapshot of the underlying macro."""
        return self.coordinator.get_macro(self._macro_id)

    @property
    def name(self) -> str | None:
        """Track the macro's current name; AIFA app rename should follow through."""
        macro = self.macro
        return macro.name if macro is not None else None

    @property
    def available(self) -> bool:
        """Hide the button if the macro got deleted server-side."""
        return self.coordinator.last_update_success and self.macro is not None

    @property
    def device_info(self):
        """Group all macros under the integration's account device."""
        return build_account_device_info(self._entry)

    @property
    def extra_state_attributes(self) -> dict[str, object]:
        """Surface the macro's command count for diagnostics."""
        macro = self.macro
        if macro is None:
            return {}
        delay_total = sum(
            (cmd.delay or 0) for cmd in macro.commands[:-1]
        ) if macro.commands else 0
        return {
            "macro_id": macro.id,
            "command_count": len(macro.commands),
            "total_delay_ms": delay_total,
        }

    async def async_press(self) -> None:
        """Run the saved macro through the coordinator."""
        await self.coordinator.async_execute_macro(self._macro_id)
