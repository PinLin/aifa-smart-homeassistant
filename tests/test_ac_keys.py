"""AC page keys follow i-Ctrl Pro 1.2.9: which ones exist, what they send, how they confirm."""
from __future__ import annotations

import types
import unittest
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.components.climate import ClimateEntityFeature, HVACMode
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import entity_registry as er

from custom_components.aifa_smart.ac import (
    AC_KEYS,
    build_ac_key_command,
    decode_fan_speed_byte,
    decode_observed_status_packet,
    shows_classic_temperature_keys,
    visible_ac_keys,
)
from custom_components.aifa_smart.climate import AifaAirConditionerClimate
from custom_components.aifa_smart.const import DOMAIN, TRANSPORT_LAN
from custom_components.aifa_smart.device_cache import serialize_account
from custom_components.aifa_smart.entity import AifaSubDeviceEntity
from custom_components.aifa_smart.lan import LanTransportManager
from custom_components.aifa_smart.sensor import AifaAcFanSpeedSensor
from tests.hub_reports import status
from tests.test_command_confirmation import (
    ConfirmingFakeHub,
    FakeHub,
    _build_coordinator,
    _cancel_pending_releases,
    _fast_timings,
    _has_report,
    _hub_device,
    _report,
)
from tests.test_device_cache import _device, _entry, _macros, _seed_cache, _setup
from tests.test_lan_fake_hub import _LOCALHOST, _fake_hass, _wait_until

def _keys(device_code: str) -> list[str]:
    return [key.key for key in visible_ac_keys(device_code)]


class VisibleKeysTests(unittest.TestCase):
    def test_code_16_shows_the_six_bottom_sheet_keys(self) -> None:
        self.assertEqual(
            _keys("115"),
            ["timer", "swing", "fan_speed", "power_saving", "sleep", "turbo"],
        )

    def test_extra_windspeed_code_adds_quiet_and_extra_wind_speed(self) -> None:
        self.assertIn("quiet", _keys("19888"))
        self.assertIn("extra_wind_speed", _keys("19888"))
        self.assertNotIn("quiet", _keys("115"))

    def test_display_mold_code_adds_display_and_mold(self) -> None:
        self.assertIn("display", _keys("134"))
        self.assertIn("mold", _keys("134"))

    def test_wind_speed_plus_minus_only_for_19866(self) -> None:
        self.assertIn("wind_speed_high", _keys("19866"))
        self.assertIn("wind_speed_low", _keys("19866"))
        self.assertNotIn("wind_speed_high", _keys("134"))

    def test_single_mode_shows_the_mode_key_instead_of_the_bottom_sheet(self) -> None:
        self.assertEqual(_keys("281"), ["power_saving", "display", "mode"])
        self.assertNotIn("mode", _keys("115"))

    def test_anti_mold_keys_only_on_proplus_and_acplus_hubs(self) -> None:
        for hub_type in ("6", "8"):
            with self.subTest(hub_type=hub_type):
                keys = [key.key for key in visible_ac_keys("115", hub_type)]
                self.assertIn("anti_mold_on", keys)
                self.assertIn("anti_mold_off", keys)
        for hub_type in (None, "2", "4"):
            with self.subTest(hub_type=hub_type):
                keys = [key.key for key in visible_ac_keys("115", hub_type)]
                self.assertNotIn("anti_mold_on", keys)

    def test_key_bytes_match_the_app(self) -> None:
        expected = {
            "timer": "ffa010f0",
            "swing": "ffa002f0",
            "fan_speed": "ffa005f0",
            "power_saving": "ffa00ef0",
            "sleep": "ffa023f0",
            "turbo": "ffa022f0",
            "quiet": "ffa02af0",
            "extra_wind_speed": "ffa028f0",
            "display": "ffa027f0",
            "mold": "ffa026f0",
            "wind_speed_high": "ffa038f0",
            "wind_speed_low": "ffa036f0",
            "mode": "ffa008f0",
            "anti_mold_on": "a1faaca001f0",
            "anti_mold_off": "a1faaca000f0",
        }
        self.assertEqual(
            {name: build_ac_key_command(key, 0) for name, key in AC_KEYS.items()}, expected
        )
        self.assertEqual(build_ac_key_command(AC_KEYS["fan_speed"], 1), "ffa105f0")
        self.assertEqual(build_ac_key_command(AC_KEYS["anti_mold_on"], 1), "a1faaca101f0")

    def test_single_mode_bottom_sheet_stays_open_while_off(self) -> None:
        from custom_components.aifa_smart.ac import ac_key_requires_power_on

        self.assertFalse(ac_key_requires_power_on(AC_KEYS["power_saving"], "281"))
        self.assertFalse(ac_key_requires_power_on(AC_KEYS["display"], "281"))
        self.assertFalse(ac_key_requires_power_on(AC_KEYS["mode"], "281"))
        self.assertTrue(ac_key_requires_power_on(AC_KEYS["power_saving"], "115"))
        self.assertTrue(ac_key_requires_power_on(AC_KEYS["display"], "134"))

    def test_enabled_only_while_on_matches_the_app(self) -> None:
        on_only = {name for name, key in AC_KEYS.items() if key.requires_on}
        self.assertEqual(
            on_only,
            {"timer", "swing", "fan_speed", "sleep", "turbo", "anti_mold_on", "anti_mold_off"},
        )


class ClassicTemperatureKeysTests(unittest.TestCase):
    def test_versions_0_5_10_with_a_classic_code(self) -> None:
        for version in (0, 5, 10):
            with self.subTest(version=version):
                self.assertTrue(shows_classic_temperature_keys(version, "115"))

    def test_other_or_unknown_versions_use_the_slider(self) -> None:
        for version in (None, 2, 4, 6):
            with self.subTest(version=version):
                self.assertFalse(shows_classic_temperature_keys(version, "115"))

    def test_non_classic_code_uses_the_slider(self) -> None:
        self.assertFalse(shows_classic_temperature_keys(5, "136"))


def _packet(b4: int, b5: int, b6: int, b7: int, b8: int = 0, b9: int = 0) -> bytes:
    return bytes([0xFE, 0xA0, 0x01, 0x15, b4, b5, b6, b7, b8, b9]) + bytes(3) + b"\xf0"


class StatusDecoderTests(unittest.TestCase):
    def test_fan_byte_values_seen_on_hardware(self) -> None:
        # Four 0x05 presses on code 115: 11 -> 90 -> 50 -> 30 -> 11.
        self.assertEqual(
            [decode_fan_speed_byte(value) for value in (0x11, 0x90, 0x50, 0x30)],
            ["auto", "high", "medium", "low"],
        )

    def test_fan_byte_without_a_known_bit_is_auto(self) -> None:
        self.assertEqual(decode_fan_speed_byte(0x10), "auto")
        self.assertEqual(decode_fan_speed_byte(0x00), "auto")

    def test_mode_byte_without_a_known_bit_is_auto(self) -> None:
        _, decoded = decode_observed_status_packet(_packet(0x24, 0, 0x00, 0x11))
        self.assertEqual(decoded.mode, "auto")

    def test_zero_setpoint_and_mode_is_off(self) -> None:
        _, decoded = decode_observed_status_packet(_packet(0x00, 0x05, 0x00, 0x11))
        self.assertEqual(decoded.mode, "off")
        self.assertEqual(decoded.timer_ir_value, 5)

    def test_flags_use_the_app_bits(self) -> None:
        _, decoded = decode_observed_status_packet(_packet(0, 0, 0x20, 0x11, b9=0x01))
        self.assertTrue(decoded.power_saving)
        self.assertFalse(decoded.turbo)
        _, decoded = decode_observed_status_packet(_packet(0, 0, 0x20, 0x11, b9=0x0C))
        self.assertEqual((decoded.sleep, decoded.turbo, decoded.power_saving), (True, True, False))

    def test_swing_byte_without_a_known_bit_is_off(self) -> None:
        _, decoded = decode_observed_status_packet(_packet(0, 0, 0x20, 0x11, b8=0x02))
        self.assertEqual(decoded.swing_mode, "off")

    def test_non_decimal_setpoint_or_timer_drops_the_packet(self) -> None:
        self.assertIsNone(decode_observed_status_packet(_packet(0x1A, 0, 0x20, 0x11)))
        self.assertIsNone(decode_observed_status_packet(_packet(0x24, 0x0B, 0x20, 0x11)))


@pytest.mark.usefixtures("socket_enabled")
class AcKeyPressTests(unittest.IsolatedAsyncioTestCase):
    """Key presses over a real LAN socket to the fake v5/210623 hub."""

    async def asyncSetUp(self) -> None:
        self.hub = await ConfirmingFakeHub().start()
        self.timings = _fast_timings()
        self.timings.__enter__()
        self.manager = LanTransportManager(
            hass=_fake_hass(),  # type: ignore[arg-type]
            on_observed_status=lambda *args: False,
            on_data_changed=lambda: None,
            broadcast_provider=self._broadcast,
            target_port=self.hub.port,
            search_source_port=0,
            bind_host=_LOCALHOST,
            backoff_schedule=(0.05,),
            connect_attempts=2,
            connect_interval=0.3,
            search_timeout=0.2,
        )
        self.coordinator = _build_coordinator(
            "lan",
            device=_hub_device(version=5, subversion=210623, device_code="115"),
            lan=self.manager,
        )
        self.manager._on_observed_status = self.coordinator._apply_lan_observed_status  # noqa: SLF001
        self.manager.sync_specs(self.coordinator.data.devices)
        await _wait_until(lambda: self.manager.is_connected("7001"))
        # A first report so the AC shows as on, as after the app's query.
        await FakeHub.push(self.hub, _report(0, self.hub.mode_byte, self.hub.fan_byte))
        await _wait_until(_has_report(self.coordinator))

    async def asyncTearDown(self) -> None:
        self.timings.__exit__(None, None, None)
        await _cancel_pending_releases(self.coordinator)
        await self.manager.shutdown()
        await self.hub.stop()

    async def _broadcast(self) -> list[str]:
        return [_LOCALHOST]

    def _state(self):
        return self.coordinator.get_ac_runtime_state("7001", "ac-1")

    async def _press(self, name: str) -> None:
        await self.coordinator.async_press_ac_key("7001", "ac-1", AC_KEYS[name])

    async def test_fan_key_confirms_on_a_new_b7_and_the_sensor_follows(self) -> None:
        self.assertEqual(self._state().observed_status.fan_mode, "auto")
        for expected in ("high", "medium", "low", "auto"):
            await self._press("fan_speed")
            self.assertEqual(self._state().observed_status.fan_mode, expected)
        self.assertEqual(self.hub.commands, ["ffa005f0"] * 4)

    async def test_fan_key_with_unchanged_b7_raises_and_is_not_resent(self) -> None:
        self.hub.cycles_fan = False
        queries = self.hub.queries
        with self.assertRaises(HomeAssistantError) as ctx:
            await self._press("fan_speed")
        self.assertEqual(ctx.exception.translation_key, "ac_fan_speed_unchanged")
        self.assertEqual(self.hub.commands, ["ffa005f0"])
        self.assertEqual(self.hub.queries, queries + 1)

    async def test_fan_key_ignores_a_remote_report_with_the_old_b7(self) -> None:
        self.hub.remote_push = _report(0, 0x20, 0x11)
        self.hub.echo_delay = 0.1
        await self._press("fan_speed")
        self.assertEqual(self.hub.commands, ["ffa005f0"])
        self.assertEqual(self._state().observed_status.fan_mode, "high")

    async def test_other_keys_take_any_report_and_are_not_resent(self) -> None:
        self.hub.forced_mode = 0x80
        for name in ("swing", "turbo", "sleep", "timer", "power_saving"):
            await self._press(name)
        self.assertEqual(
            self.hub.commands, ["ffa002f0", "ffa022f0", "ffa023f0", "ffa010f0", "ffa00ef0"]
        )

    async def test_key_without_reply_raises_without_resend(self) -> None:
        self.hub.silence()
        with self.assertRaises(HomeAssistantError) as ctx:
            await self._press("sleep")
        self.assertEqual(ctx.exception.translation_key, "hub_no_response")
        self.assertEqual(self.hub.commands, ["ffa023f0"])

    async def test_on_only_keys_are_refused_while_off(self) -> None:
        await self.coordinator.async_apply_ac_state(
            "7001", "ac-1", hvac_mode="off", device_code="115"
        )
        self.hub.commands.clear()
        # The "^" button that opens the bottom sheet is greyed out while off,
        # so even keys without their own check cannot be reached.
        for name in (
            "timer", "swing", "fan_speed", "sleep", "turbo", "power_saving",
            "quiet", "display", "mold", "wind_speed_high", "anti_mold_on",
        ):
            with self.subTest(key=name):
                with self.assertRaises(ServiceValidationError) as ctx:
                    await self._press(name)
                self.assertEqual(ctx.exception.translation_key, "ac_key_requires_power_on")
        self.assertEqual(self.hub.commands, [])

    async def test_on_only_keys_are_refused_before_the_first_report(self) -> None:
        self.coordinator._ac_runtime_states.clear()  # noqa: SLF001
        self.assertEqual(self._state().exposed_hvac_mode, "off")
        for name in ("swing", "fan_speed", "power_saving"):
            with self.subTest(key=name):
                with self.assertRaises(ServiceValidationError) as ctx:
                    await self._press(name)
                self.assertEqual(ctx.exception.translation_key, "ac_key_requires_power_on")
        self.assertEqual(self.hub.commands, [])

    async def test_key_allowed_while_off_does_not_end_the_held_off(self) -> None:
        """A single-mode AC may press power saving while off; a changed report stays off."""
        self.hub.off_reports_zero = False
        await self.coordinator.async_apply_ac_state(
            "7001", "ac-1", hvac_mode="off", device_code="115"
        )
        self.coordinator.get_sub_device("7001", "ac-1").device_code = "281"
        # The hub reports a new b7 with the mode key's echo.
        self.hub.fan_byte = 0x90
        await self._press("mode")
        self.assertEqual(self._state().exposed_hvac_mode, "off")
        await FakeHub.push(self.hub, _report(0, 0x20, 0x90))
        self.assertEqual(self._state().exposed_hvac_mode, "off")

    async def test_anti_mold_needs_a_mode_from_ac_available_modes(self) -> None:
        # Code 115 has an empty ac_available_modes list.
        with self.assertRaises(ServiceValidationError) as ctx:
            await self._press("anti_mold_on")
        self.assertEqual(ctx.exception.translation_key, "ac_anti_mold_mode_unsupported")
        self.assertEqual(self.hub.commands, [])


class ClimateFollowsTheAppTests(unittest.IsolatedAsyncioTestCase):
    def _climate(self, *, version=5, device_code="115"):
        coordinator = _build_coordinator(
            "lan", device=_hub_device(version=version, subversion=210623, device_code=device_code)
        )
        calls: list[dict] = []

        async def apply(device_id, sub_device_id, **kwargs):
            calls.append(kwargs)
            return ""

        coordinator.async_apply_ac_state = apply  # type: ignore[method-assign]
        return AifaAirConditionerClimate(coordinator, "7001", "ac-1"), calls

    def test_no_fan_or_swing_selection(self) -> None:
        climate, _ = self._climate()
        self.assertFalse(climate.supported_features & ClimateEntityFeature.FAN_MODE)
        self.assertFalse(climate.supported_features & ClimateEntityFeature.SWING_MODE)

    def test_setpoint_only_where_the_app_shows_the_slider(self) -> None:
        cases = ((5, "115", False), (0, "115", False), (10, "115", False), (4, "115", True), (5, "136", True))
        for version, code, has_setpoint in cases:
            with self.subTest(version=version, code=code):
                climate, _ = self._climate(version=version, device_code=code)
                self.assertEqual(
                    bool(climate.supported_features & ClimateEntityFeature.TARGET_TEMPERATURE),
                    has_setpoint,
                )

    def test_dehumidifier_code_offers_only_dry_and_fan(self) -> None:
        climate, _ = self._climate(device_code="19966")
        self.assertEqual(
            climate.hvac_modes, [HVACMode.OFF, HVACMode.FAN_ONLY, HVACMode.DRY]
        )

    def test_single_mode_lists_only_off_and_the_reported_mode(self) -> None:
        climate, _ = self._climate(device_code="281")
        climate.runtime.apply_observed_status(status("dry"))
        self.assertEqual(climate.hvac_modes, [HVACMode.OFF, HVACMode.DRY])

    async def test_restart_keeps_a_held_power_off(self) -> None:
        climate, _ = self._climate()
        climate.runtime.hold_power_off(None)
        attributes = climate.extra_state_attributes
        self.assertTrue(attributes["aifa_power_off_held"])

        restored, _ = self._climate()

        async def last_state():
            return types.SimpleNamespace(state="off", attributes=attributes)

        async def no_extra_data():
            return None

        # A state saved by a version without extra restore data.
        restored.async_get_last_state = last_state  # type: ignore[method-assign]
        restored.async_get_last_extra_data = no_extra_data  # type: ignore[method-assign]
        restored.hass = None
        with patch.object(AifaSubDeviceEntity, "async_added_to_hass", AsyncMock()):
            await restored.async_added_to_hass()
        restored.runtime.apply_observed_status(status("cool"))
        self.assertEqual(restored.hvac_mode, HVACMode.OFF)

    def test_shows_off_until_the_first_report(self) -> None:
        climate, _ = self._climate(device_code="136")
        self.assertEqual(climate.hvac_mode, HVACMode.OFF)
        climate.runtime.apply_observed_status(status("heat"))
        self.assertEqual(climate.hvac_mode, HVACMode.HEAT)

    async def test_restart_shows_the_last_state_until_the_first_report(self) -> None:
        climate, _ = self._climate(device_code="136")
        climate.runtime.apply_observed_status(status("heat"))
        extra = climate.extra_restore_state_data

        restored, _ = self._climate(device_code="136")

        async def last_extra_data():
            return extra

        restored.async_get_last_extra_data = last_extra_data  # type: ignore[method-assign]
        restored.hass = None
        with patch.object(AifaSubDeviceEntity, "async_added_to_hass", AsyncMock()):
            await restored.async_added_to_hass()
        self.assertEqual(restored.hvac_mode, HVACMode.HEAT)
        self.assertEqual(restored.runtime.last_active_hvac_mode, "heat")
        restored.runtime.apply_observed_status(status("dry"))
        self.assertEqual(restored.hvac_mode, HVACMode.DRY)

    async def test_single_mode_selecting_the_shown_mode_is_a_no_op(self) -> None:
        climate, calls = self._climate(device_code="281")
        climate.runtime.apply_observed_status(status("dry"))
        await climate.async_set_hvac_mode(HVACMode.DRY)
        self.assertEqual(calls, [])

    async def test_single_mode_code_refuses_mode_selection(self) -> None:
        climate, calls = self._climate(device_code="281")
        with self.assertRaises(ServiceValidationError) as ctx:
            await climate.async_set_hvac_mode(HVACMode.HEAT)
        self.assertEqual(ctx.exception.translation_key, "ac_mode_not_offered")
        await climate.async_set_hvac_mode(HVACMode.OFF)
        self.assertEqual(calls, [{"hvac_mode": "off", "device_code": "281"}])

    async def test_turn_on_is_the_power_key(self) -> None:
        climate, calls = self._climate()
        await climate.async_turn_on()
        self.assertEqual(calls, [{"hvac_mode": "on", "device_code": "115"}])

    async def test_set_temperature_with_a_new_mode_sets_the_mode_first(self) -> None:
        climate, calls = self._climate(version=4, device_code="136")
        climate.runtime.apply_observed_status(status("cool"))
        await climate.async_set_temperature(temperature=25, hvac_mode=HVACMode.HEAT)
        self.assertEqual(
            calls,
            [
                {"hvac_mode": "heat", "device_code": "136"},
                {"target_temperature": 25, "device_code": "136"},
            ],
        )


class FanSpeedSensorTests(unittest.TestCase):
    def _sensor(self, transport: str):
        coordinator = _build_coordinator(transport, device=_hub_device(device_code="115"))
        sensor = AifaAcFanSpeedSensor(coordinator, "7001", "ac-1")
        return sensor, coordinator.get_ac_runtime_state("7001", "ac-1")

    def test_unknown_until_the_hub_has_reported(self) -> None:
        for transport in ("cloud", "lan"):
            with self.subTest(transport=transport):
                sensor, state = self._sensor(transport)
                self.assertIsNone(sensor.native_value)
                state.apply_sent_command_status(status("cool"))
                self.assertIsNone(sensor.native_value)

    def test_follows_b7_including_while_the_hub_reports_off(self) -> None:
        sensor, state = self._sensor("lan")
        _, observed = decode_observed_status_packet(_packet(0, 0, 0x20, 0x90))
        state.apply_observed_status(observed)
        self.assertEqual(sensor.native_value, "high")
        _, observed = decode_observed_status_packet(_packet(0, 0, 0x00, 0x50))
        state.apply_observed_status(observed)
        self.assertEqual(observed.mode, "off")
        self.assertEqual(sensor.native_value, "medium")

    def test_keeps_the_last_report_after_power_off_without_a_new_report(self) -> None:
        for transport in ("cloud", "lan"):
            with self.subTest(transport=transport):
                sensor, state = self._sensor(transport)
                _, observed = decode_observed_status_packet(_packet(0x24, 0, 0x20, 0x90))
                state.apply_observed_status(observed)
                state.apply_sent_command_status(status("off"))
                state.hold_power_off(observed)
                self.assertEqual(state.effective_state_source, "sent_command")
                self.assertTrue(state.power_off_held)
                self.assertIsNone(state.observed_status)
                self.assertEqual(sensor.native_value, "high")


# ----- Full setup against a real hass -----


@pytest.fixture
def verify_cleanup():
    """HA-owned threads linger after full setups, as in test_device_cache."""
    yield

_OLD_SWITCH_KEYS = ("turbo", "sleep", "power_saving")


@pytest.mark.asyncio
@pytest.mark.usefixtures("enable_custom_integrations")
async def test_setup_replaces_flag_switches_with_key_buttons(
    hass: HomeAssistant, hass_storage: dict
) -> None:
    entry = _entry(hass, TRANSPORT_LAN)
    _seed_cache(hass_storage, entry.entry_id, serialize_account({"7001": _device()}, _macros()))
    registry = er.async_get(hass)
    for key in _OLD_SWITCH_KEYS:
        registry.async_get_or_create(
            "switch",
            DOMAIN,
            f"{DOMAIN}_7001_ac-1_{key}",
            config_entry=entry,
            suggested_object_id=f"{DOMAIN}_7001_ac-1_{key}",
        )
    # Another integration's switch with the same suffix must stay.
    registry.async_get_or_create("switch", "other", "x_turbo", config_entry=None)
    # Keys the app does not show for code 115, and one for a sub-device that
    # is not in this snapshot.
    for unique_id in (f"{DOMAIN}_7001_ac-1_quiet_key", f"{DOMAIN}_9999_ac-9_quiet_key"):
        registry.async_get_or_create(
            "button", DOMAIN, unique_id, config_entry=entry, suggested_object_id=unique_id
        )

    await _setup(hass, entry)

    assert entry.state is ConfigEntryState.LOADED
    entries = er.async_entries_for_config_entry(registry, entry.entry_id)
    assert [item for item in entries if item.domain == "switch"] == []
    assert registry.async_get_entity_id("switch", "other", "x_turbo") is not None
    buttons = {item.unique_id for item in entries if item.domain == "button"}
    for key in ("timer", "swing", "fan_speed", "power_saving", "sleep", "turbo"):
        assert f"{DOMAIN}_7001_ac-1_{key}_key" in buttons
    for key in ("quiet", "extra_wind_speed", "display", "mold", "wind_speed_high"):
        assert f"{DOMAIN}_7001_ac-1_{key}_key" not in buttons
    assert f"{DOMAIN}_9999_ac-9_quiet_key" in buttons
    # v5 hub + classic code 115: +/- keys, no setpoint on the climate entity.
    assert f"{DOMAIN}_7001_ac-1_temperature_up" in buttons
    sensors = {item.unique_id for item in entries if item.domain == "sensor"}
    assert f"{DOMAIN}_7001_ac-1_fan_speed" in sensors
    climate_id = next(item.entity_id for item in entries if item.domain == "climate")
    attributes = hass.states.get(climate_id).attributes
    assert "fan_modes" not in attributes
    assert "swing_modes" not in attributes
    assert "temperature" not in attributes
    assert await hass.config_entries.async_unload(entry.entry_id)

