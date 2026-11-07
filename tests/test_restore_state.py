"""After a restart or reload the AC shows what it showed before, until the hub reports.

This departs from i-Ctrl Pro on purpose (it shows Off until the first
report): flipping a running AC to off and back on in HA would trigger
automations on every restart. With nothing to restore it shows Off.

The state is kept as RestoreEntity extra data, which HA stores even while
the entity is unavailable; state attributes are not written then, so a
restart during a network outage used to lose a held power off.
"""
from __future__ import annotations

import pytest
from homeassistant.components.climate import HVACMode
from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import HomeAssistant, State
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import restore_state
from pytest_homeassistant_custom_component.common import (
    mock_restore_cache,
    mock_restore_cache_with_extra_data,
)

from custom_components.aifa_smart.ac import AC_KEYS
from custom_components.aifa_smart.classic_listener import build_hub_specs
from custom_components.aifa_smart.const import DOMAIN, TRANSPORT_CLOUD
from custom_components.aifa_smart.entity import sub_device_entity_unique_id
from custom_components.aifa_smart.runtime import AifaAcRestoreData, AifaAcRuntimeState
from tests.hub_reports import status
from tests.test_device_cache import _entry
from tests.test_unload_lifecycle import _Cloud, _patches

# Registered up front so the restore cache can be seeded under a known id.
_CLIMATE = "climate.aifa_ac"
_FAN = "sensor.aifa_ac_fan_speed"


def _extra(**kwargs) -> dict:
    return AifaAcRestoreData(**kwargs).as_dict()


@pytest.fixture
def entry(hass: HomeAssistant):
    entry = _entry(hass, TRANSPORT_CLOUD)
    registry = er.async_get(hass)
    for entity_id, attribute in ((_CLIMATE, "climate"), (_FAN, "fan_speed")):
        domain, object_id = entity_id.split(".")
        registry.async_get_or_create(
            domain,
            DOMAIN,
            sub_device_entity_unique_id("7001", "ac-1", attribute),
            config_entry=entry,
            suggested_object_id=object_id,
        )
    return entry


@pytest.fixture
def cloud():
    cloud = _Cloud()
    patches = _patches(cloud, stub_lan=True)
    for item in patches:
        item.start()
    yield cloud
    for item in patches:
        item.stop()


async def _setup(hass: HomeAssistant, entry) -> tuple[object, dict[str, list[str]]]:
    """Set up the entry and record every state each AC entity was given."""
    written: dict[str, list[str]] = {_CLIMATE: [], _FAN: []}

    def record(event) -> None:
        new_state = event.data["new_state"]
        if event.data["entity_id"] in written and new_state is not None:
            written[event.data["entity_id"]].append(new_state.state)

    hass.bus.async_listen("state_changed", record)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry, written


def _report(entry, mode: str) -> None:
    coordinator = entry.runtime_data
    spec = build_hub_specs(coordinator.data.devices, require_online=False)["7001"]
    coordinator._apply_cloud_stream_status(spec, 0, status(mode))  # noqa: SLF001
    coordinator.async_update_listeners()


async def _unload(hass: HomeAssistant, entry) -> None:
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


# ----- M3: the last state until the first report -----


@pytest.mark.asyncio
@pytest.mark.usefixtures("enable_custom_integrations")
async def test_restart_shows_the_last_mode_without_passing_through_off(
    hass: HomeAssistant, cloud: _Cloud, entry
) -> None:
    mock_restore_cache_with_extra_data(
        hass,
        [(State(_CLIMATE, "cool"), _extra(hvac_mode="cool", last_active_hvac_mode="cool"))],
    )
    entry, written = await _setup(hass, entry)
    assert written[_CLIMATE] == ["cool"]

    _report(entry, "fan_only")
    await hass.async_block_till_done()
    assert hass.states.get(_CLIMATE).state == HVACMode.FAN_ONLY
    assert HVACMode.OFF not in written[_CLIMATE]
    await _unload(hass, entry)


@pytest.mark.asyncio
@pytest.mark.usefixtures("enable_custom_integrations")
async def test_first_report_saying_off_still_turns_it_off(
    hass: HomeAssistant, cloud: _Cloud, entry
) -> None:
    mock_restore_cache_with_extra_data(hass, [(State(_CLIMATE, "cool"), _extra(hvac_mode="cool"))])
    entry, _ = await _setup(hass, entry)
    _report(entry, "off")
    await hass.async_block_till_done()
    assert hass.states.get(_CLIMATE).state == HVACMode.OFF
    await _unload(hass, entry)


@pytest.mark.asyncio
@pytest.mark.usefixtures("enable_custom_integrations")
async def test_fresh_install_shows_off_until_the_first_report(
    hass: HomeAssistant, cloud: _Cloud, entry
) -> None:
    entry, written = await _setup(hass, entry)
    assert written[_CLIMATE] == ["off"]
    assert written[_FAN] == [STATE_UNKNOWN]
    _report(entry, "cool")
    await hass.async_block_till_done()
    assert hass.states.get(_CLIMATE).state == HVACMode.COOL
    await _unload(hass, entry)


@pytest.mark.asyncio
@pytest.mark.usefixtures("enable_custom_integrations")
async def test_fan_speed_sensor_restores_its_last_value(
    hass: HomeAssistant, cloud: _Cloud, entry
) -> None:
    mock_restore_cache_with_extra_data(
        hass, [(State(_FAN, "high"), _extra(hvac_mode="cool", fan_mode="high"))]
    )
    entry, written = await _setup(hass, entry)
    assert written[_FAN] == ["high"]
    # Same runtime state as the climate entity's; the two store the same data.
    runtime = entry.runtime_data.get_ac_runtime_state("7001", "ac-1")
    assert runtime.last_reported_fan_mode == "high"
    await _unload(hass, entry)


@pytest.mark.asyncio
@pytest.mark.usefixtures("enable_custom_integrations")
async def test_keys_that_need_power_follow_the_restored_state(
    hass: HomeAssistant, cloud: _Cloud, entry
) -> None:
    mock_restore_cache_with_extra_data(hass, [(State(_CLIMATE, "cool"), _extra(hvac_mode="cool"))])
    entry, _ = await _setup(hass, entry)
    coordinator = entry.runtime_data
    # Shown on, so the key is allowed; it fails only for want of a socket.
    with pytest.raises(HomeAssistantError) as raised:
        await coordinator.async_press_ac_key("7001", "ac-1", AC_KEYS["swing"])
    assert raised.value.translation_key == "cloud_unavailable"
    await _unload(hass, entry)


# ----- M2: a held power off survives a restart while unavailable -----


@pytest.mark.asyncio
@pytest.mark.usefixtures("enable_custom_integrations")
async def test_held_power_off_is_stored_while_unavailable(
    hass: HomeAssistant, cloud: _Cloud, entry
) -> None:
    entry, _ = await _setup(hass, entry)
    coordinator = entry.runtime_data
    _report(entry, "cool")
    coordinator.get_ac_runtime_state("7001", "ac-1").hold_power_off(None)
    coordinator.last_update_success = False
    coordinator.async_update_listeners()
    await hass.async_block_till_done()
    state = hass.states.get(_CLIMATE)
    assert state.state == STATE_UNAVAILABLE
    assert "aifa_power_off_held" not in state.attributes

    stored = {
        item.state.entity_id: item
        for item in restore_state.async_get(hass).async_get_stored_states()
    }
    extra = stored[_CLIMATE].extra_data.as_dict()
    assert extra["power_off_held"] is True
    assert extra["hvac_mode"] == "off"
    assert extra["last_active_hvac_mode"] == "cool"
    await _unload(hass, entry)


@pytest.mark.asyncio
@pytest.mark.usefixtures("enable_custom_integrations")
async def test_restart_while_unavailable_keeps_the_held_power_off(
    hass: HomeAssistant, cloud: _Cloud, entry
) -> None:
    mock_restore_cache_with_extra_data(
        hass,
        [
            (
                State(_CLIMATE, STATE_UNAVAILABLE),
                _extra(hvac_mode="off", power_off_held=True, last_active_hvac_mode="cool"),
            )
        ],
    )
    entry, written = await _setup(hass, entry)
    assert written[_CLIMATE] == ["off"]
    # The hub's first report repeats the mode it had before the power off.
    _report(entry, "cool")
    await hass.async_block_till_done()
    assert hass.states.get(_CLIMATE).state == HVACMode.OFF
    coordinator = entry.runtime_data
    with pytest.raises(ServiceValidationError) as raised:
        await coordinator.async_press_ac_key("7001", "ac-1", AC_KEYS["swing"])
    assert raised.value.translation_key == "ac_key_requires_power_on"
    # A different report, e.g. from a remote, means the AC changed.
    _report(entry, "fan_only")
    await hass.async_block_till_done()
    assert hass.states.get(_CLIMATE).state == HVACMode.FAN_ONLY
    await _unload(hass, entry)


@pytest.mark.asyncio
@pytest.mark.usefixtures("enable_custom_integrations")
async def test_state_from_a_version_without_extra_data_is_still_read(
    hass: HomeAssistant, cloud: _Cloud, entry
) -> None:
    mock_restore_cache(
        hass,
        [
            State(
                _CLIMATE,
                "off",
                {
                    "aifa_power_off_held": True,
                    "aifa_last_active_hvac_mode": "cool",
                    "aifa_requested_target_temperature": 25,
                },
            )
        ],
    )
    entry, _ = await _setup(hass, entry)
    _report(entry, "cool")
    await hass.async_block_till_done()
    assert hass.states.get(_CLIMATE).state == HVACMode.OFF
    runtime = entry.runtime_data.get_ac_runtime_state("7001", "ac-1")
    assert runtime.last_active_hvac_mode == "cool"
    assert runtime.requested_target_temperature == 25
    await _unload(hass, entry)


@pytest.mark.asyncio
@pytest.mark.usefixtures("enable_custom_integrations")
async def test_old_mode_without_extra_data_is_shown_until_the_first_report(
    hass: HomeAssistant, cloud: _Cloud, entry
) -> None:
    mock_restore_cache(hass, [State(_CLIMATE, "cool", {"aifa_power_off_held": False})])
    _, written = await _setup(hass, entry)
    assert written[_CLIMATE] == ["cool"]


@pytest.mark.asyncio
@pytest.mark.usefixtures("enable_custom_integrations")
async def test_old_unavailable_state_without_extra_data_shows_off(
    hass: HomeAssistant, cloud: _Cloud, entry
) -> None:
    mock_restore_cache(hass, [State(_CLIMATE, STATE_UNAVAILABLE)])
    _, written = await _setup(hass, entry)
    assert written[_CLIMATE] == ["off"]


# ----- The runtime side of the same mechanism -----


def test_only_the_first_restore_counts() -> None:
    state = AifaAcRuntimeState.create("115")
    state.restore(AifaAcRestoreData(hvac_mode="cool", fan_mode="low"))
    state.restore(AifaAcRestoreData(hvac_mode="off", power_off_held=True, fan_mode="high"))
    assert state.exposed_hvac_mode == "cool"
    assert state.power_off_held is False
    assert state.last_reported_fan_mode == "low"


def test_mode_the_code_does_not_offer_is_not_restored() -> None:
    state = AifaAcRuntimeState.create("115")
    state.restore(AifaAcRestoreData(hvac_mode="bogus", last_active_hvac_mode="bogus"))
    assert state.exposed_hvac_mode == "off"
    assert state.last_active_hvac_mode != "bogus"


def test_restored_hold_takes_a_report_that_came_first_as_its_baseline() -> None:
    state = AifaAcRuntimeState.create("115")
    state.apply_observed_status(status("cool"))
    state.restore(AifaAcRestoreData(hvac_mode="off", power_off_held=True))
    assert state.exposed_hvac_mode == "off"
    state.apply_observed_status(status("cool"))
    assert state.exposed_hvac_mode == "off"
    state.apply_observed_status(status("fan_only"))
    assert state.exposed_hvac_mode == "fan_only"


def test_a_command_replaces_the_restored_mode() -> None:
    state = AifaAcRuntimeState.create("115")
    state.restore(AifaAcRestoreData(hvac_mode="cool"))
    state.apply_sent_command_status(status("off"))
    assert state.restored_hvac_mode is None
    assert state.exposed_hvac_mode == "off"


def test_restore_data_round_trips_and_rejects_garbage() -> None:
    data = AifaAcRestoreData(
        hvac_mode="cool",
        power_off_held=True,
        last_active_hvac_mode="cool",
        target_temperature=24,
        fan_mode="medium",
    )
    assert AifaAcRestoreData.from_dict(data.as_dict()) == data
    assert AifaAcRestoreData.from_dict({}) is None
    assert AifaAcRestoreData.from_dict(
        {"hvac_mode": 3, "power_off_held": "yes", "target_temperature": "x"}
    ) == AifaAcRestoreData()


# ----- A restored mode is not a report to build a 15-byte packet on -----

_COOL_FAN_HIGH = bytes([0xFE, 0xA0, 0x01, 0x36, 0x24, 0x00, 0x20, 0x90]) + bytes(5) + b"\xf0"


class _QueryLan:
    """LAN stub: the hub answers only status queries, with `report`."""

    def __init__(self) -> None:
        self.coordinator = None
        self.sent: list[str] = []
        self.links: dict = {}
        self.report: bytes | None = _COOL_FAN_HIGH

    def is_connected(self, device_id: str) -> bool:
        return True

    def is_available(self, device_id: str) -> bool:
        return True

    async def async_send_packet(self, device_id, packet_hex, *, connect_timeout):
        import asyncio

        from custom_components.aifa_smart.ac import decode_observed_status_packet
        from custom_components.aifa_smart.lan import LanSendResult

        self.sent.append(packet_hex)
        if packet_hex == "fff3f2f0" and self.report is not None:
            sub_type, observed = decode_observed_status_packet(self.report)
            spec = build_hub_specs(self.coordinator.data.devices, require_online=False)["7001"]
            asyncio.get_running_loop().call_soon(
                self.coordinator._apply_lan_observed_status, spec, sub_type, observed  # noqa: SLF001
            )
        elif packet_hex != "fff3f2f0":
            # Echo the setpoint so the command confirms.
            echo = _COOL_FAN_HIGH[:4] + bytes([0x25]) + _COOL_FAN_HIGH[5:]
            sub_type, observed = decode_observed_status_packet(echo)
            spec = build_hub_specs(self.coordinator.data.devices, require_online=False)["7001"]
            asyncio.get_running_loop().call_soon(
                self.coordinator._apply_lan_observed_status, spec, sub_type, observed  # noqa: SLF001
            )
        return LanSendResult.SENT

    async def async_request_status(self, device_id: str) -> bool:
        return False


def _restored_coordinator():
    from tests.test_command_confirmation import _build_coordinator, _hub_device

    lan = _QueryLan()
    coordinator = _build_coordinator("lan", device=_hub_device(device_code="136"), lan=lan)
    lan.coordinator = coordinator
    state = coordinator.get_ac_runtime_state("7001", "ac-1", device_code="136")
    state.restore(AifaAcRestoreData(hvac_mode="cool", fan_mode="high"))
    return coordinator, lan, state


@pytest.mark.asyncio
async def test_setpoint_after_restore_is_built_on_a_queried_report() -> None:
    from unittest.mock import patch

    from custom_components.aifa_smart import coordinator as coordinator_module

    coordinator, lan, state = _restored_coordinator()
    with patch.object(coordinator_module, "_AC_SEND_MIN_INTERVAL_SECONDS", 0.0):
        await coordinator.async_apply_ac_state(
            "7001", "ac-1", target_temperature=25, device_code="136"
        )
    assert lan.sent[0] == "fff3f2f0"
    packet = bytes.fromhex(lan.sent[1])
    assert len(packet) == 15
    # Fan from the hub's report (high, 0x80), not the fallback (auto, 0x01).
    assert packet[8] == 0x80
    for task in coordinator._ac_pending_release_tasks.values():  # noqa: SLF001
        task.cancel()


@pytest.mark.asyncio
async def test_setpoint_after_restore_without_a_report_is_not_sent() -> None:
    from unittest.mock import patch

    from custom_components.aifa_smart import coordinator as coordinator_module

    coordinator, lan, state = _restored_coordinator()
    lan.report = None
    with (
        patch.object(coordinator_module, "_AC_SEND_MIN_INTERVAL_SECONDS", 0.0),
        patch.object(coordinator_module, "AC_CONFIRM_QUERY_TIMEOUT", 0.2),
        pytest.raises(HomeAssistantError) as raised,
    ):
        await coordinator.async_apply_ac_state(
            "7001", "ac-1", target_temperature=25, device_code="136"
        )
    assert raised.value.translation_key == "ac_state_unknown"
    assert lan.sent == ["fff3f2f0"]
    assert state.exposed_hvac_mode == "cool"
