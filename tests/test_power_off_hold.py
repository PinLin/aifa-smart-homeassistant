"""A power off the hub never reports stays held until a key actually goes out.

After HA turns the AC off the hub keeps reporting the last mode, so the
entity holds Off until a different report arrives. A power key that was
never written must not end that hold: the reconnect query would then bring
back the stale report and show the AC as on while it is still off.
"""
from __future__ import annotations

import asyncio
import unittest
from unittest.mock import patch

from homeassistant.exceptions import HomeAssistantError

from custom_components.aifa_smart import coordinator as coordinator_module
from custom_components.aifa_smart.ac import AC_KEYS, decode_observed_status_packet
from custom_components.aifa_smart.classic_listener import build_hub_specs
from custom_components.aifa_smart.lan import LanSendResult
from tests.test_command_confirmation import _build_coordinator, _hub_device, _NoCloud

_COOL = bytes([0xFE, 0xA0, 0x01, 0x15, 0x00, 0x00, 0x20, 0x11]) + bytes(5) + b"\xf0"
_HEAT = bytes([0xFE, 0xA0, 0x01, 0x15, 0x00, 0x00, 0x08, 0x11]) + bytes(5) + b"\xf0"


class _Lan:
    """LAN manager whose link can drop; a write draws the hub's usual report."""

    def __init__(self) -> None:
        self.coordinator = None
        self.up = True
        self.sent: list[str] = []
        self.links: dict = {}
        self.report = _COOL

    def is_connected(self, device_id: str) -> bool:
        return self.up

    def is_available(self, device_id: str) -> bool:
        return self.up

    def push(self, raw: bytes | None = None) -> None:
        sub_type, observed = decode_observed_status_packet(raw or self.report)
        spec = build_hub_specs(self.coordinator.data.devices, require_online=False)["7001"]
        self.coordinator._apply_lan_observed_status(spec, sub_type, observed)  # noqa: SLF001

    async def async_send_packet(self, device_id, packet_hex, *, connect_timeout):
        if not self.up:
            return LanSendResult.NOT_CONNECTED
        self.sent.append(packet_hex)
        asyncio.get_running_loop().call_soon(self.push)
        return LanSendResult.SENT

    async def async_request_status(self, device_id: str) -> bool:
        if not self.up:
            return False
        asyncio.get_running_loop().call_soon(self.push)
        return True


class PowerOffHoldTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.patcher = patch.object(coordinator_module, "_AC_SEND_MIN_INTERVAL_SECONDS", 0.0)
        self.patcher.start()
        self.lan = _Lan()
        self.coordinator = _build_coordinator(
            "lan",
            device=_hub_device(version=5, subversion=210623, device_code="115"),
            lan=self.lan,
        )
        self.lan.coordinator = self.coordinator
        self.lan.push()
        await self.coordinator.async_apply_ac_state(
            "7001", "ac-1", hvac_mode="off", device_code="115"
        )
        self.state = self.coordinator.get_ac_runtime_state("7001", "ac-1")
        self.assertTrue(self.state.power_off_held)
        self.assertEqual(self.state.exposed_hvac_mode, "off")
        self.lan.sent.clear()

    async def asyncTearDown(self) -> None:
        self.patcher.stop()
        tasks = list(self.coordinator._ac_pending_release_tasks.values())  # noqa: SLF001
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    def _assert_still_held_after_reconnect(self) -> None:
        self.assertTrue(self.state.power_off_held)
        self.assertEqual(self.state.exposed_hvac_mode, "off")
        # Link back up: the connect query draws the same stale report.
        self.lan.up = True
        self.lan.push()
        self.assertTrue(self.state.power_off_held)
        self.assertEqual(self.state.exposed_hvac_mode, "off")

    async def test_turn_on_that_was_not_written_keeps_the_hold(self) -> None:
        self.lan.up = False
        with self.assertRaises(HomeAssistantError) as raised:
            await self.coordinator.async_apply_ac_state(
                "7001", "ac-1", hvac_mode="on", device_code="115"
            )
        self.assertEqual(raised.exception.translation_key, "lan_unavailable")
        self.assertEqual(self.lan.sent, [])
        self._assert_still_held_after_reconnect()

    async def test_mode_from_off_that_was_not_written_keeps_the_hold(self) -> None:
        self.lan.up = False
        with self.assertRaises(HomeAssistantError):
            await self.coordinator.async_apply_ac_state(
                "7001", "ac-1", hvac_mode="heat", device_code="115"
            )
        self.assertEqual(self.lan.sent, [])
        self._assert_still_held_after_reconnect()

    async def test_cloud_turn_on_without_a_socket_keeps_the_hold(self) -> None:
        self.coordinator._transport = "cloud"  # noqa: SLF001
        self.coordinator._classic_listener = _NoCloud()  # noqa: SLF001
        with self.assertRaises(HomeAssistantError) as raised:
            await self.coordinator.async_apply_ac_state(
                "7001", "ac-1", hvac_mode="on", device_code="115"
            )
        self.assertEqual(raised.exception.translation_key, "cloud_unavailable")
        self.assertTrue(self.state.power_off_held)
        self.lan.push()
        self.assertEqual(self.state.exposed_hvac_mode, "off")

    async def test_key_allowed_while_off_that_was_not_written_keeps_the_baseline(self) -> None:
        self.lan.push()
        baseline = self.state.power_off_report
        self.assertIsNotNone(baseline)
        self.lan.up = False
        with self.assertRaises(HomeAssistantError):
            await self.coordinator.async_press_ac_key("7001", "ac-1", AC_KEYS["mode"])
        self.assertIs(self.state.power_off_report, baseline)
        # A remote turning the AC on still ends the hold.
        self.lan.up = True
        self.lan.push(_HEAT)
        self.assertFalse(self.state.power_off_held)
        self.assertEqual(self.state.exposed_hvac_mode, "heat")

    async def test_turn_on_that_was_written_ends_the_hold(self) -> None:
        await self.coordinator.async_apply_ac_state(
            "7001", "ac-1", hvac_mode="on", device_code="115"
        )
        self.assertEqual(self.lan.sent, ["ffa0a1f0"])
        self.assertFalse(self.state.power_off_held)
        self.assertEqual(self.state.exposed_hvac_mode, "cool")
