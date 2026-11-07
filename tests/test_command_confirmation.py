"""Commands return only after the hub's status report confirms them.

The LAN cases run against a fake hub on 127.0.0.1 that tracks the mode byte
like the real i-Ctrl: after a command it pushes `fe a0 01 15 00 00 XX 11 … f0`
and it answers `ff f3 f2 f0` with the slot 0 then the slot 1 report.
"""
from __future__ import annotations

import asyncio
import types
import unittest
from datetime import datetime, timezone
from contextlib import ExitStack
from unittest.mock import patch

import pytest
from homeassistant.exceptions import HomeAssistantError

from custom_components.aifa_smart import coordinator as coordinator_module
from custom_components.aifa_smart.api import AifaMacro, AifaMacroCommand
from custom_components.aifa_smart.classic_listener import build_hub_specs
from custom_components.aifa_smart.coordinator import (
    AifaAccountData,
    AifaSmartCoordinator,
)
from custom_components.aifa_smart.lan import LanTransportManager
from tests.hub_reports import status
from tests.test_lan_fake_hub import (
    _HUB_MAC,
    _LOCALHOST,
    FakeHub,
    _device,
    _fake_hass,
    _wait_until,
)

pytestmark = pytest.mark.usefixtures("socket_enabled")

_QUERY = bytes.fromhex("fff3f2f0")
_MODE_KEYS = {0x15: 0x10, 0x16: 0x20, 0x17: 0x08, 0x18: 0x40, 0x19: 0x80}
_COOL, _HEAT, _FAN = 0x20, 0x08, 0x80
_TIMEOUT = 0.3


_FAN_KEY_CYCLE = (0x11, 0x90, 0x50, 0x30)


def _report(slot: int, mode_byte: int, fan_byte: int = 0x11) -> bytes:
    return bytes([0xFE, 0xA0 | slot, 0x01, 0x15, 0x00, 0x00, mode_byte, fan_byte]) + bytes(5) + b"\xf0"


class ConfirmingFakeHub(FakeHub):
    """Fake hub that changes its recorded mode on commands and reports it.

    push:          report right after each command (the normal case)
    query_only:    no report after commands, but answer status queries
    silent:        neither, like a half-open link
    forced_mode:   report this mode byte instead of the real one ("pushes a wrong value")
    wrong_reports: report `forced_mode` only this many times, then the truth
    ignore_commands: commands are lost; the recorded mode does not change
    reply_slot:    slot used in every report
    remote_push:   report pushed as soon as a command arrives, before the echo
    echo_delay:    delay before the echo
    fan_byte:      reported b7; the fan key steps it like the v5/210623 hub
                   (11 -> 90 -> 50 -> 30 -> 11) unless cycles_fan is False
    """

    def __init__(self) -> None:
        super().__init__(_HUB_MAC)
        self.mode_byte = _COOL
        self.last_on_mode = _COOL
        self.push = True
        self.answer_queries = True
        self.forced_mode: int | None = None
        self.wrong_reports: int | None = None
        self.ignore_commands = False
        self.off_reports_zero = True
        self.reply_slot = 0
        self.remote_push: bytes | None = None
        self.echo_delay = 0.0
        self.fan_byte = _FAN_KEY_CYCLE[0]
        self.cycles_fan = True
        self.commands: list[str] = []
        self.queries = 0

    def _heard(self, data: bytes, connected_at: float) -> bytes:
        """Bytes of `data` the hub acts on; subclasses model a deaf hub."""
        return data

    def silence(self) -> None:
        self.push = False
        self.answer_queries = False

    def _reported_mode(self) -> int:
        if self.forced_mode is None:
            return self.mode_byte
        if self.wrong_reports is None:
            return self.forced_mode
        if self.wrong_reports > 0:
            self.wrong_reports -= 1
            return self.forced_mode
        return self.mode_byte

    async def _send(self, writer: asyncio.StreamWriter, data: bytes) -> None:
        writer.write(data)
        await writer.drain()

    async def _apply(self, packet: bytes, writer: asyncio.StreamWriter) -> None:
        self.commands.append(packet.hex())
        if not self.ignore_commands:
            if len(packet) == 15:
                self.mode_byte = packet[7]
            elif packet[2] in _MODE_KEYS:
                self.mode_byte = _MODE_KEYS[packet[2]]
            elif len(packet) == 4 and packet[2] == 0x05 and self.cycles_fan:
                index = _FAN_KEY_CYCLE.index(self.fan_byte)
                self.fan_byte = _FAN_KEY_CYCLE[(index + 1) % len(_FAN_KEY_CYCLE)]
            elif packet[2] == 0xA1 and self.mode_byte == 0:
                self.mode_byte = self.last_on_mode
            elif packet[2] == 0xA2 and self.off_reports_zero:
                self.last_on_mode = self.mode_byte or self.last_on_mode
                self.mode_byte = 0x00
        if self.remote_push is not None:
            await self._send(writer, self.remote_push)
        if self.push:
            if self.echo_delay:
                await asyncio.sleep(self.echo_delay)
            await self._send(writer, _report(self.reply_slot, self._reported_mode(), self.fan_byte))

    async def _connect_back(self, addr: tuple[str, int]) -> None:
        try:
            reader, writer = await asyncio.open_connection(addr[0], addr[1])
        except OSError:
            return
        self.connect_ports.append(addr[1])
        self.writers.append(writer)
        connected_at = asyncio.get_running_loop().time()
        pending = b""
        while True:
            data = await reader.read(4096)
            if not data:
                self.eof_count += 1
                return
            self.received.extend(data)
            pending += self._heard(data, connected_at)
            while pending:
                if pending.startswith(_QUERY):
                    pending = pending[4:]
                    self.queries += 1
                    if self.answer_queries:
                        await self._send(writer, _report(self.reply_slot, self._reported_mode(), self.fan_byte))
                        await self._send(writer, _report(1, 0x00))
                elif pending[0] == 0xFF and len(pending) >= 4 and pending[3] == 0xF0:
                    packet, pending = pending[:4], pending[4:]
                    await self._apply(packet, writer)
                elif pending[0] == 0xFF and len(pending) >= 15:
                    packet, pending = pending[:15], pending[15:]
                    await self._apply(packet, writer)
                elif pending[0] == 0xFF:
                    break
                else:
                    pending = pending[1:]


class _NoCloud:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []

    async def async_send_packet(self, device_id: str, packet_hex: str) -> bool:
        self.sent.append((device_id, packet_hex))
        return False

    def has_active_listener(self, device_id: str) -> bool:
        return False

    def has_active_writer(self, device_id: str) -> bool:
        return False


def _hub_device(*, version=None, subversion=None, device_code="136"):
    device = _device()
    device.version = version
    device.subversion = subversion
    device.sub_devices[0].device_code = device_code
    return device


def _build_coordinator(transport: str, *, device=None, lan=None, listener=None):
    coordinator = AifaSmartCoordinator.__new__(AifaSmartCoordinator)
    coordinator.data = AifaAccountData(  # type: ignore[attr-defined]
        devices={"7001": device or _hub_device()}, macros=[], fetched_at=None,  # type: ignore[arg-type]
    )
    coordinator._transport = transport  # type: ignore[attr-defined]
    coordinator._lan_manager = lan  # type: ignore[attr-defined]
    coordinator._classic_listener = listener or _NoCloud()  # type: ignore[attr-defined]
    coordinator._lan_failed_at = {}  # type: ignore[attr-defined]
    coordinator._transport_counters = {  # type: ignore[attr-defined]
        "sent_lan": 0, "sent_cloud": 0, "auto_fallbacks": 0,
        "lan_write_failures": 0, "last_transport": None,
    }
    coordinator._ac_runtime_states = {}  # type: ignore[attr-defined]
    coordinator._ac_apply_locks = {}  # type: ignore[attr-defined]
    coordinator._ac_last_send_at = {}  # type: ignore[attr-defined]
    coordinator._ac_pending_release_tasks = {}  # type: ignore[attr-defined]
    coordinator._hub_report_waiters = {}  # type: ignore[attr-defined]
    coordinator._lan_queried_at = {}  # type: ignore[attr-defined]
    coordinator.async_update_listeners = lambda: None  # type: ignore[method-assign]
    coordinator.async_request_refresh = lambda: asyncio.sleep(0)  # type: ignore[method-assign]

    def create_task(coro, name=None, eager_start=True):
        return asyncio.get_running_loop().create_task(coro)

    coordinator.hass = types.SimpleNamespace(  # type: ignore[assignment]
        async_create_task=create_task, async_create_background_task=create_task
    )
    return coordinator


def _fast_timings() -> ExitStack:
    stack = ExitStack()
    stack.enter_context(patch.object(coordinator_module, "AC_CONFIRM_PUSH_TIMEOUT", _TIMEOUT))
    stack.enter_context(patch.object(coordinator_module, "AC_CONFIRM_QUERY_TIMEOUT", _TIMEOUT))
    stack.enter_context(patch.object(coordinator_module, "_AC_SEND_MIN_INTERVAL_SECONDS", 0.0))
    return stack


async def _cancel_pending_releases(coordinator) -> None:
    tasks = list(coordinator._ac_pending_release_tasks.values())  # noqa: SLF001
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)


def _has_report(coordinator):
    def check() -> bool:
        state = coordinator._ac_runtime_states.get(("7001", "ac-1"))  # noqa: SLF001
        return state is not None and state.observed_status is not None

    return check


def _first_cloud_report(coordinator, mode: str) -> None:
    spec = build_hub_specs(coordinator.data.devices, require_online=False)["7001"]
    coordinator._apply_cloud_stream_status(spec, 0, status(mode))  # noqa: SLF001


class LanConfirmationTests(unittest.IsolatedAsyncioTestCase):
    """End to end over a real LAN socket to the fake hub."""

    transport = "lan"

    async def asyncSetUp(self) -> None:
        self.hub = await ConfirmingFakeHub().start()
        self.timings = _fast_timings()
        self.timings.__enter__()

    async def asyncTearDown(self) -> None:
        self.timings.__exit__(None, None, None)
        await _cancel_pending_releases(self.coordinator)
        await self.manager.shutdown()
        await self.hub.stop()

    async def _connect(
        self, device=None, *, first_report: bool = True, query_on_connect: bool = False
    ) -> AifaSmartCoordinator:
        self.cloud = _NoCloud()
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
            self.transport, device=device, lan=self.manager, listener=self.cloud
        )
        # Same wiring as AifaSmartCoordinator.__init__.
        self.manager._on_observed_status = self.coordinator._apply_lan_observed_status  # noqa: SLF001
        if query_on_connect:
            self.manager._on_link_state_changed = self.coordinator._on_lan_link_state_changed  # noqa: SLF001
        self.manager.sync_specs(self.coordinator.data.devices)
        await _wait_until(lambda: self.manager.is_connected("7001"))
        if first_report:
            # The integration's first poll queries the hub; until a report
            # arrives the AC shows off, like i-Ctrl Pro.
            await FakeHub.push(self.hub, _report(0, self.hub.mode_byte, self.hub.fan_byte))
            await _wait_until(_has_report(self.coordinator))
        return self.coordinator

    async def _broadcast(self) -> list[str]:
        return [_LOCALHOST]

    def _state(self):
        return self.coordinator.get_ac_runtime_state("7001", "ac-1")

    async def _apply(self, **kwargs) -> str:
        return await self.coordinator.async_apply_ac_state(
            "7001", "ac-1", device_code="136", **kwargs
        )

    async def test_connect_without_a_report_queries_the_hub(self) -> None:
        self.hub.mode_byte = _HEAT
        await self._connect(first_report=False, query_on_connect=True)
        await _wait_until(_has_report(self.coordinator))
        self.assertEqual(self.hub.queries, 1)
        self.assertEqual(self.hub.commands, [])
        self.assertEqual(self._state().exposed_hvac_mode, "heat")

    async def test_before_any_report_a_mode_change_presses_on_first(self) -> None:
        await self._connect(first_report=False)
        self.assertEqual(self._state().exposed_hvac_mode, "off")
        await self._apply(hvac_mode="heat")
        self.assertEqual(len(self.hub.commands), 2)
        self.assertEqual(self.hub.commands[0], "ffa0a1f0")
        self.assertEqual(len(self.hub.commands[1]), 30)
        self.assertEqual(self._state().exposed_hvac_mode, "heat")

    async def test_before_any_report_a_mode_key_hub_presses_on_first(self) -> None:
        await self._connect(_hub_device(version=5, subversion=210623), first_report=False)
        await self._apply(hvac_mode="heat")
        self.assertEqual(self.hub.commands, ["ffa0a1f0", "ffa017f0"])
        self.assertEqual(self._state().exposed_hvac_mode, "heat")

    async def test_before_any_report_the_on_key_shows_the_report_it_draws(self) -> None:
        await self._connect(first_report=False)
        self.hub.mode_byte = _HEAT
        await self._apply(hvac_mode="on")
        self.assertEqual(self.hub.commands, ["ffa0a1f0"])
        self.assertEqual(self._state().exposed_hvac_mode, "heat")

    async def test_before_any_report_the_temperature_keys_still_work(self) -> None:
        await self._connect(_hub_device(device_code="115"), first_report=False)
        await self.coordinator.async_press_classic_temperature_key("7001", "ac-1", up=True)
        self.assertEqual(self.hub.commands, ["ffa006f0"])

    async def test_push_confirms_and_state_is_observed(self) -> None:
        await self._connect()
        await self._apply(hvac_mode="heat")
        self.assertEqual(len(self.hub.commands), 1)
        self.assertEqual(self.hub.queries, 0)
        state = self._state()
        self.assertEqual(state.exposed_hvac_mode, "heat")
        self.assertEqual(state.effective_state_source, "aifa_socket")
        self.assertFalse(state.command_status_pending)
        self.assertEqual(self.coordinator._ac_pending_release_tasks, {})  # noqa: SLF001
        self.assertEqual(self.cloud.sent, [])

    async def test_query_confirms_when_the_push_is_missing(self) -> None:
        await self._connect()
        self.hub.push = False
        await self._apply(hvac_mode="heat")
        self.assertEqual(len(self.hub.commands), 1)
        self.assertEqual(self.hub.queries, 1)
        self.assertEqual(self._state().exposed_hvac_mode, "heat")

    async def test_mismatch_is_not_resent_even_when_a_retry_would_match(self) -> None:
        await self._connect()
        self.hub.forced_mode = _FAN
        # Wrong in both the push and the query reply of the only try.
        self.hub.wrong_reports = 2
        with self.assertRaises(HomeAssistantError) as ctx:
            await self._apply(hvac_mode="heat")
        self.assertEqual(ctx.exception.translation_key, "ac_state_mismatch")
        self.assertEqual(len(self.hub.commands), 1)
        self.assertEqual(self._state().exposed_hvac_mode, "fan_only")

    async def test_mismatch_raises_without_resend_and_shows_the_hub_report(self) -> None:
        await self._connect()
        self.hub.forced_mode = _FAN
        with self.assertRaises(HomeAssistantError) as ctx:
            await self._apply(hvac_mode="heat")
        self.assertEqual(ctx.exception.translation_key, "ac_state_mismatch")
        self.assertEqual(ctx.exception.translation_placeholders["reported"], "fan_only")
        self.assertEqual(ctx.exception.translation_placeholders["expected"], "heat")
        self.assertEqual(len(self.hub.commands), 1)
        state = self._state()
        self.assertFalse(state.command_status_pending)
        self.assertEqual(state.exposed_hvac_mode, "fan_only")
        self.assertEqual(state.requested_hvac_mode, "fan_only")

    async def test_no_reply_raises_and_keeps_the_link(self) -> None:
        await self._connect()
        # Short enough that the old query watchdog would have fired below.
        self.manager._query_reply_timeout = 0.2  # noqa: SLF001
        self.hub.silence()
        with self.assertRaises(HomeAssistantError) as ctx:
            await self._apply(hvac_mode="heat")
        self.assertEqual(ctx.exception.translation_key, "hub_no_response")
        self.assertEqual(len(self.hub.commands), 1)
        self.assertEqual(self.hub.queries, 1)
        await asyncio.sleep(0.5)
        self.assertTrue(self.manager.is_connected("7001"))
        self.assertEqual(len(self.hub.connect_ports), 1)
        self.assertEqual(self.hub.eof_count, 0)
        self.assertIsNone(self.manager.links["7001"].last_error)
        self.assertEqual(self.manager.links["7001"].reconnect_count, 0)
        self.assertFalse(self._state().command_status_pending)
        self.assertEqual(self.cloud.sent, [])

    async def test_next_command_after_no_reply_goes_out_on_the_same_link(self) -> None:
        # Like the hub after a fan key in dry mode: deaf to queries for a
        # while, then the next key is reported as usual.
        await self._connect()
        port = self.hub.connect_ports[0]
        self.hub.silence()
        with self.assertRaises(HomeAssistantError) as ctx:
            await self._apply(hvac_mode="heat")
        self.assertEqual(ctx.exception.translation_key, "hub_no_response")
        self.hub.push = True
        self.hub.answer_queries = True
        await self._apply(hvac_mode="cool")
        self.assertEqual(len(self.hub.commands), 2)
        self.assertEqual(self._state().exposed_hvac_mode, "cool")
        self.assertEqual(self.hub.connect_ports, [port])
        self.assertEqual(self.hub.eof_count, 0)
        self.assertEqual(self.cloud.sent, [])

    async def test_relative_key_is_never_resent(self) -> None:
        await self._connect(_hub_device(device_code="115"))
        self.hub.forced_mode = _FAN
        await self.coordinator.async_press_classic_temperature_key("7001", "ac-1", up=True)
        self.assertEqual(self.hub.commands, ["ffa006f0"])

    async def test_relative_key_without_reply_raises_without_resend(self) -> None:
        await self._connect(_hub_device(device_code="115"))
        self.hub.silence()
        with self.assertRaises(HomeAssistantError) as ctx:
            await self.coordinator.async_press_classic_temperature_key("7001", "ac-1", up=False)
        self.assertEqual(ctx.exception.translation_key, "hub_no_response")
        self.assertEqual(self.hub.commands, ["ffa007f0"])

    async def test_power_off_is_confirmed_by_any_report(self) -> None:
        await self._connect()
        self.hub.off_reports_zero = False
        await self._apply(hvac_mode="off")
        self.assertEqual(self.hub.commands, ["ffa0a2f0"])
        state = self._state()
        self.assertEqual(state.exposed_hvac_mode, "off")
        # A probe would only fetch the same report again, which cannot settle it.
        self.assertEqual(self.coordinator._ac_pending_release_tasks, {})  # noqa: SLF001

    async def test_power_off_holds_while_the_hub_repeats_its_report(self) -> None:
        """The v5/210623 hub keeps reporting the last mode after power off."""
        await self._connect()
        self.hub.off_reports_zero = False
        await self._apply(hvac_mode="off")
        revision = self._state().observed_revision
        await FakeHub.push(self.hub, _report(0, _COOL))
        await _wait_until(lambda: self._state().observed_revision > revision)
        self.assertEqual(self._state().exposed_hvac_mode, "off")
        # A different report (the app changed the mode) means the AC is on.
        await FakeHub.push(self.hub, _report(0, _HEAT))
        await _wait_until(lambda: self._state().exposed_hvac_mode == "heat")

    async def test_failed_second_power_off_keeps_the_held_off(self) -> None:
        await self._connect()
        self.hub.off_reports_zero = False
        await self._apply(hvac_mode="off")
        self.hub.silence()
        with self.assertRaises(HomeAssistantError):
            await self._apply(hvac_mode="off")
        self.assertEqual(self._state().exposed_hvac_mode, "off")

    async def test_power_off_reported_as_off_needs_no_follow_up(self) -> None:
        await self._connect()
        await self._apply(hvac_mode="off")
        state = self._state()
        self.assertEqual(state.exposed_hvac_mode, "off")
        self.assertFalse(state.command_status_pending)
        self.assertEqual(self.coordinator._ac_pending_release_tasks, {})  # noqa: SLF001

    async def test_strict_power_off_switch_requires_the_off_report(self) -> None:
        await self._connect()
        self.hub.off_reports_zero = False
        with patch.object(coordinator_module, "AC_POWER_OFF_STRICT_CONFIRM", True):
            with self.assertRaises(HomeAssistantError) as ctx:
                await self._apply(hvac_mode="off")
        self.assertEqual(ctx.exception.translation_key, "ac_state_mismatch")
        self.assertEqual(self.hub.commands, ["ffa0a2f0"])

    async def test_remote_report_does_not_end_the_wait(self) -> None:
        await self._connect()
        self.hub.remote_push = _report(0, _FAN)
        self.hub.echo_delay = 0.1
        await self._apply(hvac_mode="heat")
        self.assertEqual(len(self.hub.commands), 1)
        self.assertEqual(self._state().exposed_hvac_mode, "heat")

    async def test_remote_report_is_not_a_confirmation_of_a_lost_command(self) -> None:
        await self._connect()
        self.hub.ignore_commands = True
        self.hub.push = False
        self.hub.remote_push = _report(0, _FAN)
        with self.assertRaises(HomeAssistantError) as ctx:
            await self._apply(hvac_mode="heat")
        self.assertEqual(ctx.exception.translation_key, "ac_state_mismatch")
        self.assertEqual(len(self.hub.commands), 1)

    async def test_slot1_report_does_not_confirm_a_slot0_command(self) -> None:
        await self._connect()
        self.hub.reply_slot = 1
        self.hub.answer_queries = False
        with self.assertRaises(HomeAssistantError) as ctx:
            await self._apply(hvac_mode="heat")
        self.assertEqual(ctx.exception.translation_key, "hub_no_response")

    async def test_mode_key_hub_confirms_power_then_mode(self) -> None:
        await self._connect(_hub_device(version=5, subversion=210623))
        await self._apply(hvac_mode="off")
        await self._apply(hvac_mode="heat")
        self.assertEqual(self.hub.commands, ["ffa0a2f0", "ffa0a1f0", "ffa017f0"])
        self.assertEqual(self._state().exposed_hvac_mode, "heat")

    async def test_pending_power_off_still_sends_the_power_key(self) -> None:
        await self._connect(_hub_device(version=5, subversion=210623))
        self.hub.off_reports_zero = False
        await self._apply(hvac_mode="off")
        # A report while the off is pending keeps showing off (stale guard).
        await FakeHub.push(self.hub, _report(0, _COOL))
        await _wait_until(lambda: self._state().observed_status is not None)
        self.assertEqual(self._state().exposed_hvac_mode, "off")
        await self._apply(hvac_mode="heat")
        self.assertEqual(self.hub.commands, ["ffa0a2f0", "ffa0a1f0", "ffa017f0"])

    async def test_commands_on_one_hub_run_one_at_a_time(self) -> None:
        await self._connect()
        self.hub.echo_delay = 0.1
        received_at_report: list[int] = []
        original = self.coordinator._apply_lan_observed_status  # noqa: SLF001

        def record(spec, sub_type, observed):
            received_at_report.append(len(self.hub.commands))
            return original(spec, sub_type, observed)

        self.manager._on_observed_status = record  # noqa: SLF001
        await asyncio.gather(
            self._apply(hvac_mode="heat"),
            self._apply(hvac_mode="dry"),
        )
        # The second command reaches the hub only after the first one's report.
        self.assertEqual(received_at_report, [1, 2])
        self.assertEqual(self._state().exposed_hvac_mode, "dry")


class AutoConfirmationTests(LanConfirmationTests):
    """Auto mode: a LAN write that goes unconfirmed never falls back to the cloud."""

    transport = "auto"


class _CloudListener:
    """Cloud listener whose socket delivers reports through the coordinator."""

    def __init__(self) -> None:
        self.coordinator: AifaSmartCoordinator | None = None
        self.sent: list[str] = []
        self.dropped: list[str] = []
        self.mode = "cool"
        self.push = True
        self.answer_queries = True
        self.forced_mode: str | None = None
        self.delay = 0.05

    def has_active_listener(self, device_id: str) -> bool:
        return True

    def has_active_writer(self, device_id: str) -> bool:
        return True

    def drop_connection(self, device_id: str) -> None:
        self.dropped.append(device_id)

    async def async_send_packet(self, device_id: str, packet_hex: str) -> bool:
        self.sent.append(packet_hex)
        if packet_hex == "fff3f2f0":
            if self.answer_queries:
                self._report_later()
            return True
        packet = bytes.fromhex(packet_hex)
        if len(packet) == 15:
            self.mode = {0x20: "cool", 0x08: "heat", 0x80: "fan_only", 0x40: "dry", 0x10: "auto"}[packet[7]]
        if self.push:
            self._report_later()
        return True

    def _report_later(self) -> None:
        assert self.coordinator is not None
        spec = build_hub_specs(self.coordinator.data.devices, require_online=False)["7001"]
        mode = self.forced_mode or self.mode
        asyncio.get_running_loop().call_later(
            self.delay, self.coordinator._apply_cloud_stream_status, spec, 0, status(mode)  # noqa: SLF001
        )


class CloudConfirmationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.timings = _fast_timings()
        self.timings.__enter__()
        self.listener = _CloudListener()
        self.coordinator = _build_coordinator("cloud", listener=self.listener)
        self.listener.coordinator = self.coordinator
        _first_cloud_report(self.coordinator, "cool")

    async def asyncTearDown(self) -> None:
        self.timings.__exit__(None, None, None)
        await _cancel_pending_releases(self.coordinator)

    def _state(self):
        return self.coordinator.get_ac_runtime_state("7001", "ac-1")

    async def _apply(self, **kwargs) -> str:
        return await self.coordinator.async_apply_ac_state(
            "7001", "ac-1", device_code="136", **kwargs
        )

    def _commands(self) -> list[str]:
        return [packet for packet in self.listener.sent if packet != "fff3f2f0"]

    async def test_push_confirms_and_state_is_observed(self) -> None:
        await self._apply(hvac_mode="heat")
        self.assertEqual(len(self._commands()), 1)
        self.assertNotIn("fff3f2f0", self.listener.sent)
        state = self._state()
        self.assertEqual(state.exposed_hvac_mode, "heat")
        self.assertEqual(state.effective_state_source, "aifa_socket")
        self.assertFalse(state.command_status_pending)

    async def test_query_on_the_cloud_socket_confirms_when_the_push_is_missing(self) -> None:
        self.listener.push = False
        await self._apply(hvac_mode="heat")
        self.assertEqual(self.listener.sent[-1], "fff3f2f0")
        self.assertEqual(len(self._commands()), 1)
        self.assertEqual(self._state().exposed_hvac_mode, "heat")

    async def test_mismatch_raises_without_resend(self) -> None:
        self.listener.forced_mode = "fan_only"
        with self.assertRaises(HomeAssistantError) as ctx:
            await self._apply(hvac_mode="heat")
        self.assertEqual(ctx.exception.translation_key, "ac_state_mismatch")
        self.assertEqual(len(self._commands()), 1)
        self.assertEqual(self._state().exposed_hvac_mode, "fan_only")
        self.assertFalse(self._state().command_status_pending)

    async def test_no_reply_raises_and_keeps_the_cloud_socket(self) -> None:
        self.listener.push = False
        self.listener.answer_queries = False
        with self.assertRaises(HomeAssistantError) as ctx:
            await self._apply(hvac_mode="heat")
        self.assertEqual(ctx.exception.translation_key, "hub_no_response")
        self.assertEqual(self.listener.dropped, [])
        self.assertEqual(len(self._commands()), 1)
        self.listener.push = True
        self.listener.answer_queries = True
        await self._apply(hvac_mode="dry")
        self.assertEqual(len(self._commands()), 2)
        self.assertEqual(self._state().exposed_hvac_mode, "dry")
        self.assertEqual(self.listener.dropped, [])

    async def test_report_from_no_connection_does_not_confirm(self) -> None:
        """A report not read from the command's connection proves nothing about it."""
        self.listener.push = False
        self.listener.answer_queries = False
        spec = build_hub_specs(self.coordinator.data.devices, require_online=False)["7001"]
        asyncio.get_running_loop().call_later(
            0.05,
            lambda: self.coordinator._apply_classic_observed_status(  # noqa: SLF001
                spec, 0, status("heat")
            ),
        )
        with self.assertRaises(HomeAssistantError) as ctx:
            await self._apply(hvac_mode="heat")
        self.assertEqual(ctx.exception.translation_key, "hub_no_response")

    async def test_relative_key_takes_any_report_and_is_not_resent(self) -> None:
        self.coordinator.data.devices["7001"].sub_devices[0].device_code = "115"
        self.listener.forced_mode = "fan_only"
        await self.coordinator.async_press_classic_temperature_key("7001", "ac-1", up=True)
        self.assertEqual(self._commands(), ["ffa006f0"])

    async def test_power_off_is_confirmed_by_any_report(self) -> None:
        self.listener.forced_mode = "cool"
        await self._apply(hvac_mode="off")
        self.assertEqual(self._commands(), ["ffa0a2f0"])
        self.assertEqual(self._state().exposed_hvac_mode, "off")

    async def test_commands_on_one_hub_run_one_at_a_time(self) -> None:
        events: list[str] = []
        send = self.listener.async_send_packet
        report = self.coordinator._apply_cloud_stream_status  # noqa: SLF001

        async def logged_send(device_id, packet_hex):
            events.append("send")
            return await send(device_id, packet_hex)

        def logged_report(spec, sub_type, observed):
            events.append("report")
            return report(spec, sub_type, observed)

        self.listener.async_send_packet = logged_send  # type: ignore[method-assign]
        self.coordinator._apply_cloud_stream_status = logged_report  # type: ignore[method-assign]
        await asyncio.gather(self._apply(hvac_mode="heat"), self._apply(hvac_mode="dry"))
        self.assertEqual(events, ["send", "report", "send", "report"])

    def _add_macro(self, *commands: AifaMacroCommand) -> None:
        self.coordinator.data.macros.append(AifaMacro(id="m", name="Macro", commands=list(commands)))

    async def test_macro_step_is_not_confirmed(self) -> None:
        # Like i-Ctrl Pro: a learned IR code may draw no report at all.
        self._add_macro(
            AifaMacroCommand(id="1", macro_id="m", device_id="7001", sub_device_id=None, command="aabb"),
            AifaMacroCommand(id="2", macro_id="m", device_id="7001", sub_device_id=None, command="ccdd"),
        )
        self.listener.push = False
        self.listener.answer_queries = False
        with patch.object(coordinator_module, "AC_CONFIRM_PUSH_TIMEOUT", 5.0):
            responses = await self.coordinator.async_execute_macro("m")
        self.assertEqual([r["transport"] for r in responses], ["classic_tls", "classic_tls"])
        # No status query after a step, and no reconnect for want of a report.
        self.assertEqual(self.listener.sent, ["aabb", "ccdd"])
        self.assertEqual(self.listener.dropped, [])

    async def test_macro_step_that_cannot_be_written_raises(self) -> None:
        self._add_macro(
            AifaMacroCommand(id="1", macro_id="m", device_id="7001", sub_device_id=None, command="aabb"),
            AifaMacroCommand(id="2", macro_id="m", device_id="7001", sub_device_id=None, command="ccdd"),
        )

        async def no_socket(device_id: str, packet_hex: str) -> bool:
            self.listener.sent.append(packet_hex)
            return False

        self.listener.async_send_packet = no_socket  # type: ignore[method-assign]
        with self.assertRaises(HomeAssistantError) as ctx:
            await self.coordinator.async_execute_macro("m")
        self.assertEqual(ctx.exception.translation_key, "cloud_unavailable")
        # The macro stops at the step that did not go out.
        self.assertEqual(self.listener.sent, ["aabb"])

    async def test_ac_commands_are_still_confirmed(self) -> None:
        self.listener.push = False
        self.listener.answer_queries = False
        with self.assertRaises(HomeAssistantError) as ctx:
            await self._apply(hvac_mode="heat")
        self.assertEqual(ctx.exception.translation_key, "hub_no_response")


class LockPruneTests(unittest.IsolatedAsyncioTestCase):
    async def test_held_hub_lock_survives_a_snapshot_without_the_hub(self) -> None:
        coordinator = _build_coordinator("lan")
        coordinator._device_code_catalogs = {}  # noqa: SLF001
        held = coordinator._ac_apply_lock("7001")  # noqa: SLF001
        idle = coordinator._ac_apply_lock("9999")  # noqa: SLF001
        async with held:
            coordinator._prune_stale_runtime_state({})  # noqa: SLF001
        self.assertIs(coordinator._ac_apply_locks.get("7001"), held)  # noqa: SLF001
        self.assertNotIn("9999", coordinator._ac_apply_locks)  # noqa: SLF001
        self.assertFalse(idle.locked())


class CloudCodeTests(unittest.IsolatedAsyncioTestCase):
    """5-digit cloud codes get no state echo, so any report confirms delivery."""

    async def test_cloud_code_accepts_any_report_without_resend(self) -> None:
        listener = _CloudListener()
        coordinator = _build_coordinator(
            "cloud", device=_hub_device(device_code="19844"), listener=listener
        )
        listener.coordinator = coordinator
        listener.forced_mode = "cool"
        _first_cloud_report(coordinator, "cool")
        with _fast_timings():
            await coordinator.async_apply_ac_state(
                "7001", "ac-1", hvac_mode="heat", device_code="19844"
            )
        self.assertEqual(len([p for p in listener.sent if p != "fff3f2f0"]), 1)
        await _cancel_pending_releases(coordinator)


if __name__ == "__main__":
    unittest.main()


class _StubLink:
    def __init__(self) -> None:
        self.connected = False
        self.connected_at = None


class _StubLan:
    def __init__(self) -> None:
        self.links = {"7001": _StubLink()}
        self.queries: list[str] = []

    async def async_request_status(self, device_id: str) -> None:
        self.queries.append(device_id)


class LanConnectQueryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.lan = _StubLan()
        self.coordinator = _build_coordinator("lan", lan=self.lan)
        self.link = self.lan.links["7001"]
        self._stamp = 0

    async def _settle(self) -> None:
        await asyncio.sleep(0)
        await asyncio.sleep(0)

    async def _connect(self) -> None:
        self._stamp += 1
        self.link.connected = True
        self.link.connected_at = datetime(2000, 1, 1, 0, 0, self._stamp, tzinfo=timezone.utc)
        self.coordinator._on_lan_link_state_changed()
        await self._settle()

    async def _drop(self) -> None:
        self.link.connected = False
        self.coordinator._on_lan_link_state_changed()
        await self._settle()

    async def test_first_connection_queries_the_hub(self) -> None:
        await self._connect()
        self.assertEqual(self.lan.queries, ["7001"])

    async def test_reconnect_queries_even_with_observed_status(self) -> None:
        await self._connect()
        self.coordinator.get_ac_runtime_state("7001", "ac-1", device_code="136").observed_status = (
            object()
        )
        await self._drop()
        await self._connect()
        self.assertEqual(self.lan.queries, ["7001", "7001"])

    async def test_drop_and_grace_expiry_do_not_query(self) -> None:
        await self._connect()
        await self._drop()
        self.coordinator._on_lan_link_state_changed()  # grace timer expiry
        await self._settle()
        self.assertEqual(self.lan.queries, ["7001"])

    async def test_same_connection_is_queried_once(self) -> None:
        await self._connect()
        self.coordinator._on_lan_link_state_changed()
        self.coordinator._on_lan_link_state_changed()
        await self._settle()
        self.assertEqual(self.lan.queries, ["7001"])
