"""The power key counts as delivered only when the hub stops reporting off.

With some codes the hub reports all zeros (off) after a power key it
dropped, and the AC's state shortly after one it took. With others it keeps
reporting the last mode whatever the AC does.
"""
from __future__ import annotations

import unittest

import pytest
from homeassistant.exceptions import HomeAssistantError

from tests import test_command_confirmation
from tests.test_command_confirmation import (
    _COOL,
    ConfirmingFakeHub,
    _build_coordinator,
    _cancel_pending_releases,
    _CloudListener,
    _fast_timings,
    _first_cloud_report,
    _hub_device,
    _report,
)
from tests.test_lan_fake_hub import FakeHub, _wait_until

pytestmark = pytest.mark.usefixtures("socket_enabled")

_ON_KEY = "ffa0a1f0"


# Referenced through the module so pytest does not collect its tests here too.
_Lan = test_command_confirmation.LanConfirmationTests


class LanPowerOnTests(unittest.IsolatedAsyncioTestCase):
    transport = "lan"
    asyncSetUp = _Lan.asyncSetUp
    asyncTearDown = _Lan.asyncTearDown
    _connect = _Lan._connect
    _broadcast = _Lan._broadcast
    _state = _Lan._state
    _apply = _Lan._apply

    hub: ConfirmingFakeHub

    def _drop_keys_and_report_off(self) -> None:
        # The power key reaches the hub but the AC never hears it.
        self.hub.ignore_commands = True
        self.hub.push = False
        self.hub.mode_byte = 0x00

    async def test_off_report_after_the_power_key_raises_without_resend(self) -> None:
        self.hub.mode_byte = 0x00
        await self._connect()
        self._drop_keys_and_report_off()
        with self.assertRaises(HomeAssistantError) as ctx:
            await self._apply(hvac_mode="on")
        self.assertEqual(ctx.exception.translation_key, "ac_power_on_not_reported")
        self.assertEqual(self.hub.commands, [_ON_KEY])
        self.assertEqual(self.hub.queries, 1)
        state = self._state()
        self.assertEqual(state.exposed_hvac_mode, "off")
        self.assertFalse(state.command_status_pending)
        self.assertEqual(self.coordinator._ac_pending_release_tasks, {})  # noqa: SLF001
        self.assertEqual(self.cloud.sent, [])

    async def test_failed_power_on_keeps_a_held_power_off(self) -> None:
        await self._connect()
        # Power off held against the cool report the hub keeps repeating.
        self.hub.off_reports_zero = False
        await self._apply(hvac_mode="off")
        self.assertTrue(self._state().power_off_held)
        self._drop_keys_and_report_off()
        with self.assertRaises(HomeAssistantError) as ctx:
            await self._apply(hvac_mode="on")
        self.assertEqual(ctx.exception.translation_key, "ac_power_on_not_reported")
        state = self._state()
        self.assertTrue(state.power_off_held)
        self.assertEqual(state.exposed_hvac_mode, "off")
        # The hub repeating its off report keeps the AC off.
        revision = state.observed_revision
        await FakeHub.push(self.hub, _report(0, 0x00))
        await _wait_until(lambda: self._state().observed_revision > revision)
        self.assertEqual(self._state().exposed_hvac_mode, "off")
        self.assertEqual(self.hub.commands, ["ffa0a2f0", _ON_KEY])
        # A real power on, e.g. from a remote, reports the same cool the
        # hold was first set against and must show.
        await FakeHub.push(self.hub, _report(0, _COOL))
        await _wait_until(lambda: self._state().exposed_hvac_mode == "cool")
        self.assertFalse(self._state().power_off_held)

    async def test_failed_power_on_after_a_restored_hold_shows_a_later_power_on(self) -> None:
        self.hub.mode_byte = 0x00
        await self._connect(first_report=False)
        # A restart restored a held power off before any report.
        self._state().hold_power_off(None)
        self._drop_keys_and_report_off()
        with self.assertRaises(HomeAssistantError) as ctx:
            await self._apply(hvac_mode="on")
        self.assertEqual(ctx.exception.translation_key, "ac_power_on_not_reported")
        self.assertTrue(self._state().power_off_held)
        await FakeHub.push(self.hub, _report(0, _COOL))
        await _wait_until(lambda: self._state().exposed_hvac_mode == "cool")

    async def test_on_report_after_the_power_key_confirms(self) -> None:
        await self._connect()
        await self._apply(hvac_mode="off")
        self.assertEqual(self._state().exposed_hvac_mode, "off")
        await self._apply(hvac_mode="on")
        self.assertEqual(self.hub.commands, ["ffa0a2f0", _ON_KEY])
        self.assertEqual(self.hub.queries, 0)
        self.assertEqual(self._state().exposed_hvac_mode, "cool")

    async def test_off_then_on_report_still_confirms(self) -> None:
        # A stale off report may come first; only the window's end decides.
        await self._connect()
        await self._apply(hvac_mode="off")
        self.hub.remote_push = _report(0, 0x00)
        self.hub.echo_delay = 0.1
        await self._apply(hvac_mode="on")
        self.assertEqual(self.hub.commands, ["ffa0a2f0", _ON_KEY])
        self.assertEqual(self._state().exposed_hvac_mode, "cool")

    async def test_code_16_hub_that_never_reports_off_is_unchanged(self) -> None:
        await self._connect(_hub_device(device_code="115"))
        self.hub.off_reports_zero = False
        await self.coordinator.async_apply_ac_state(
            "7001", "ac-1", hvac_mode="off", device_code="115"
        )
        self.hub.ignore_commands = True
        self.hub.forced_mode = _COOL
        await self.coordinator.async_apply_ac_state(
            "7001", "ac-1", hvac_mode="on", device_code="115"
        )
        self.assertEqual(self.hub.commands, ["ffa0a2f0", _ON_KEY])
        state = self._state()
        self.assertFalse(state.power_off_held)
        self.assertEqual(state.exposed_hvac_mode, "cool")

    async def test_mode_from_off_stops_when_the_power_key_did_not_take(self) -> None:
        self.hub.mode_byte = 0x00
        await self._connect()
        self._drop_keys_and_report_off()
        with self.assertRaises(HomeAssistantError) as ctx:
            await self._apply(hvac_mode="heat")
        self.assertEqual(ctx.exception.translation_key, "ac_power_on_not_reported")
        self.assertEqual(self.hub.commands, [_ON_KEY])
        state = self._state()
        self.assertEqual(state.exposed_hvac_mode, "off")
        self.assertIsNone(state.last_status_command)

    async def test_mode_key_hub_from_off_stops_when_the_power_key_did_not_take(self) -> None:
        self.hub.mode_byte = 0x00
        await self._connect(_hub_device(version=5, subversion=210623))
        self._drop_keys_and_report_off()
        with self.assertRaises(HomeAssistantError) as ctx:
            await self._apply(hvac_mode="heat")
        self.assertEqual(ctx.exception.translation_key, "ac_power_on_not_reported")
        self.assertEqual(self.hub.commands, [_ON_KEY])
        self.assertEqual(self._state().exposed_hvac_mode, "off")


del _Lan


class _PowerKeyCloudListener(_CloudListener):
    """Cloud relay whose hub turns on with the power key unless it drops keys."""

    def __init__(self) -> None:
        super().__init__()
        self.drops_keys = False

    async def async_send_packet(self, device_id: str, packet_hex: str) -> bool:
        if packet_hex == _ON_KEY and not self.drops_keys:
            self.mode = "cool"
        return await super().async_send_packet(device_id, packet_hex)


class CloudPowerOnTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.timings = _fast_timings()
        self.timings.__enter__()

    async def asyncTearDown(self) -> None:
        self.timings.__exit__(None, None, None)
        await _cancel_pending_releases(self.coordinator)

    def _setup(self, device_code: str = "136") -> _PowerKeyCloudListener:
        self.listener = _PowerKeyCloudListener()
        self.listener.mode = "off"
        self.device_code = device_code
        self.coordinator = _build_coordinator(
            "cloud", device=_hub_device(device_code=device_code), listener=self.listener
        )
        self.listener.coordinator = self.coordinator
        _first_cloud_report(self.coordinator, "off")
        return self.listener

    def _state(self):
        return self.coordinator.get_ac_runtime_state("7001", "ac-1")

    async def _apply(self, **kwargs) -> str:
        return await self.coordinator.async_apply_ac_state(
            "7001", "ac-1", device_code=self.device_code, **kwargs
        )

    def _commands(self) -> list[str]:
        return [packet for packet in self.listener.sent if packet != "fff3f2f0"]

    async def test_off_report_after_the_power_key_raises_without_resend(self) -> None:
        self._setup().drops_keys = True
        self.listener.push = False
        with self.assertRaises(HomeAssistantError) as ctx:
            await self._apply(hvac_mode="on")
        self.assertEqual(ctx.exception.translation_key, "ac_power_on_not_reported")
        self.assertEqual(self._commands(), [_ON_KEY])
        self.assertEqual(self.listener.sent[-1], "fff3f2f0")
        self.assertEqual(self._state().exposed_hvac_mode, "off")
        self.assertEqual(self.listener.dropped, [])

    async def test_on_report_after_the_power_key_confirms(self) -> None:
        self._setup()
        await self._apply(hvac_mode="on")
        self.assertEqual(self._commands(), [_ON_KEY])
        self.assertEqual(self._state().exposed_hvac_mode, "cool")

    async def test_mode_from_off_stops_when_the_power_key_did_not_take(self) -> None:
        self._setup().drops_keys = True
        with self.assertRaises(HomeAssistantError) as ctx:
            await self._apply(hvac_mode="heat")
        self.assertEqual(ctx.exception.translation_key, "ac_power_on_not_reported")
        self.assertEqual(self._commands(), [_ON_KEY])
        self.assertEqual(self._state().exposed_hvac_mode, "off")

    async def test_mode_from_off_continues_when_the_power_key_took(self) -> None:
        self._setup()
        await self._apply(hvac_mode="heat")
        commands = self._commands()
        self.assertEqual(commands[0], _ON_KEY)
        self.assertEqual(len(commands), 2)
        self.assertEqual(self._state().exposed_hvac_mode, "heat")

    async def test_five_digit_code_without_state_echo_stays_lenient(self) -> None:
        # The hub never echoes these codes' state, so its off report says nothing.
        self._setup("19844").drops_keys = True
        await self._apply(hvac_mode="on")
        self.assertEqual(self._commands(), [_ON_KEY])
