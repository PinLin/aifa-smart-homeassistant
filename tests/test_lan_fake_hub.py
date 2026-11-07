"""LAN transport against a fake hub on 127.0.0.1 using real sockets.

The fake hub mimics what was observed on the real i-Ctrl: it answers c0
searches with its MAC string, and on a c2 request carrying exactly that MAC
it opens a plain TCP connection back to the UDP source port of the request.
"""
from __future__ import annotations

import asyncio
import errno
import socket
import types
import unittest
from unittest.mock import patch

import pytest

from custom_components.aifa_smart import lan
from custom_components.aifa_smart.api import AifaDevice, AifaSubDevice
from custom_components.aifa_smart.lan import (
    LanConnectTimeout,
    LanSendResult,
    LanTransportManager,
    async_request_lan_connection,
)

# PHCC blocks every socket call, including UDP to 127.0.0.1; everything here
# binds to 127.0.0.1 only.
pytestmark = pytest.mark.usefixtures("socket_enabled")

_HUB_MAC = "A0B1C2D3E4F5"
_STATUS_COOL_26 = bytes.fromhex("fea00136260024110000000000f0")
_LOCALHOST = "127.0.0.1"


class FakeHub(asyncio.DatagramProtocol):
    """UDP side of the fake hub; TCP connect-backs are tracked per instance."""

    def __init__(self, mac: str = _HUB_MAC, *, initial: bytes = b"") -> None:
        self.mac = mac
        self.initial = initial
        self.accept_connect = True
        self.transport: asyncio.DatagramTransport | None = None
        self.port = 0
        self.c2_requests: list[tuple[str, int]] = []
        self.connect_ports: list[int] = []
        self.received = bytearray()
        self.writers: list[asyncio.StreamWriter] = []
        self.eof_count = 0
        self._tasks: set[asyncio.Task[None]] = set()

    async def start(self) -> FakeHub:
        loop = asyncio.get_running_loop()
        self.transport, _ = await loop.create_datagram_endpoint(
            lambda: self, local_addr=(_LOCALHOST, 0)
        )
        self.port = self.transport.get_extra_info("sockname")[1]
        return self

    async def stop(self) -> None:
        if self.transport is not None:
            self.transport.close()
        for writer in self.writers:
            writer.close()
        for task in list(self._tasks):
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)

    def datagram_received(self, data: bytes, addr: tuple[str, int]) -> None:
        if len(data) != 54 or data[:3] != b"\x54\x01\x00":
            return
        if data[16] == 0xC0:
            reply = bytearray(54)
            reply[:3] = b"\x55\x01\xa4"
            reply[3:15] = self.mac.encode("ascii")
            reply[15] = 0x04
            reply[16] = 0xC1
            assert self.transport is not None
            self.transport.sendto(bytes(reply), addr)
            return
        if data[16] != 0xC2:
            return
        self.c2_requests.append(addr)
        if self.accept_connect and data[3:15] == self.mac.encode("ascii"):
            self._spawn(self._connect_back(addr))

    def _spawn(self, coro) -> None:
        task = asyncio.get_running_loop().create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _connect_back(self, addr: tuple[str, int]) -> None:
        try:
            reader, writer = await asyncio.open_connection(addr[0], addr[1])
        except OSError:
            return
        self.connect_ports.append(addr[1])
        self.writers.append(writer)
        if self.initial:
            writer.write(self.initial)
        while True:
            data = await reader.read(4096)
            if not data:
                self.eof_count += 1
                return
            self.received.extend(data)

    async def push(self, data: bytes) -> None:
        writer = self.writers[-1]
        writer.write(data)
        await writer.drain()

    def drop(self) -> None:
        self.writers[-1].close()


async def _wait_until(predicate, timeout: float = 3.0) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not predicate():
        if loop.time() > deadline:
            raise AssertionError("condition not met in time")
        await asyncio.sleep(0.01)


def _device(mac: str = _HUB_MAC, *, online: bool = True) -> AifaDevice:
    return AifaDevice(
        id="7001",
        name="Hub",
        mac=mac,
        device_type=None,
        online=online,
        temperature=None,
        humidity=None,
        firmware=None,
        sub_devices=[
            AifaSubDevice(
                id="ac-1",
                device_id="7001",
                name="AC",
                type="0",
                sub_type="0",
                device_code="136",
            )
        ],
        updated_at=None,
        raw={},
    )


def _fake_hass() -> types.SimpleNamespace:
    def create_task(coro, name=None, eager_start=True):
        return asyncio.get_running_loop().create_task(coro, name=name)

    return types.SimpleNamespace(async_create_background_task=create_task)


class _ManagerHarness:
    def __init__(self, hub: FakeHub) -> None:
        self.observed: list[tuple[object, int, object]] = []
        self.data_changed = 0
        self.manager = LanTransportManager(
            hass=_fake_hass(),  # type: ignore[arg-type]
            on_observed_status=self._on_status,
            on_data_changed=self._on_changed,
            broadcast_provider=self._broadcast,
            target_port=hub.port,
            search_source_port=0,
            bind_host=_LOCALHOST,
            backoff_schedule=(0.05,),
            connect_attempts=2,
            connect_interval=0.3,
            search_timeout=0.2,
        )

    async def _broadcast(self) -> list[str]:
        return [_LOCALHOST]

    def _on_status(self, spec, sub_type, observed) -> bool:
        self.observed.append((spec, sub_type, observed))
        return True

    def _on_changed(self) -> None:
        self.data_changed += 1


class RequestConnectionTests(unittest.IsolatedAsyncioTestCase):
    """The c2 request / connect-back handshake."""

    async def asyncSetUp(self) -> None:
        self.hub = await FakeHub().start()

    async def asyncTearDown(self) -> None:
        await self.hub.stop()

    async def test_hub_connects_back_to_udp_source_port(self) -> None:
        reader, writer, port = await async_request_lan_connection(
            _HUB_MAC,
            [_LOCALHOST],
            target_port=self.hub.port,
            bind_host=_LOCALHOST,
            interval=0.5,
        )
        try:
            self.assertEqual(self.hub.c2_requests[0][1], port)
            await _wait_until(lambda: self.hub.connect_ports == [port])
            self.assertEqual(writer.get_extra_info("sockname")[1], port)
        finally:
            writer.close()

    async def test_mac_mismatch_times_out(self) -> None:
        with self.assertRaises(LanConnectTimeout):
            await async_request_lan_connection(
                "FFFFFFFFFFFF",
                [_LOCALHOST],
                attempts=2,
                interval=0.2,
                target_port=self.hub.port,
                bind_host=_LOCALHOST,
            )
        self.assertEqual(len(self.hub.c2_requests), 2)
        self.assertEqual(self.hub.connect_ports, [])

    async def test_retries_with_new_port_when_udp_port_is_taken(self) -> None:
        busy = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        busy.bind((_LOCALHOST, 0))
        busy_port = busy.getsockname()[1]
        real_start_server = asyncio.start_server
        ports_tried: list[int] = []

        async def start_server_first_on_busy_port(*args, **kwargs):
            if not ports_tried:
                kwargs["port"] = busy_port
            server = await real_start_server(*args, **kwargs)
            ports_tried.append(server.sockets[0].getsockname()[1])
            return server

        try:
            with patch.object(lan.asyncio, "start_server", start_server_first_on_busy_port):
                _reader, writer, port = await async_request_lan_connection(
                    _HUB_MAC,
                    [_LOCALHOST],
                    target_port=self.hub.port,
                    bind_host=_LOCALHOST,
                    interval=0.5,
                )
            writer.close()
        finally:
            busy.close()
        self.assertEqual(ports_tried[0], busy_port)
        self.assertEqual(len(ports_tried), 2)
        self.assertNotEqual(port, busy_port)
        self.assertEqual(port, ports_tried[1])
        await _wait_until(lambda: self.hub.connect_ports == [port])

    async def test_other_bind_errors_are_not_retried(self) -> None:
        calls = 0

        async def failing_endpoint(*args, **kwargs):
            nonlocal calls
            calls += 1
            raise OSError(errno.EACCES, "denied")

        loop = asyncio.get_running_loop()
        with patch.object(loop, "create_datagram_endpoint", failing_endpoint):
            with self.assertRaises(OSError):
                await async_request_lan_connection(
                    _HUB_MAC, [_LOCALHOST], target_port=self.hub.port, bind_host=_LOCALHOST
                )
        self.assertEqual(calls, 1)


class LanManagerFakeHubTests(unittest.IsolatedAsyncioTestCase):
    """LanTransportManager end to end against the fake hub."""

    async def asyncSetUp(self) -> None:
        self.hub = await FakeHub().start()
        self.harness = _ManagerHarness(self.hub)
        self.manager = self.harness.manager

    async def asyncTearDown(self) -> None:
        await self.manager.shutdown()
        await self.hub.stop()

    async def _connect(self, device: AifaDevice | None = None) -> None:
        self.manager.sync_specs({"7001": device or _device()})
        await _wait_until(lambda: self.manager.is_connected("7001"))

    async def test_connects_even_when_cloud_reports_offline(self) -> None:
        await self._connect(_device(online=False))
        link = self.manager.links["7001"]
        self.assertEqual(link.hub_ip, _LOCALHOST)
        self.assertEqual(link.state, "connected")
        self.assertEqual(self.hub.connect_ports, [link.local_port])

    async def test_wake_gets_ccdd_and_f1aa_gets_nothing(self) -> None:
        await self._connect()
        await self.hub.push(bytes.fromhex("f1aaa0f0"))
        await self.hub.push(bytes.fromhex("f1eea0f0"))
        await _wait_until(lambda: bytes(self.hub.received) == b"\xcc\xdd")
        await self.hub.push(_STATUS_COOL_26)
        await _wait_until(lambda: self.harness.observed)
        self.assertEqual(bytes(self.hub.received), b"\xcc\xdd")

    async def test_status_packet_is_decoded(self) -> None:
        await self._connect()
        await self.hub.push(_STATUS_COOL_26[:6])
        await self.hub.push(_STATUS_COOL_26[6:])
        await _wait_until(lambda: self.harness.observed)
        spec, sub_type, observed = self.harness.observed[0]
        self.assertEqual(spec.device_id, "7001")  # type: ignore[attr-defined]
        self.assertEqual(sub_type, 0)
        self.assertEqual(observed.mode, "cool")  # type: ignore[attr-defined]
        self.assertEqual(observed.target_temperature, 26)  # type: ignore[attr-defined]
        self.assertEqual(self.harness.data_changed, 1)
        self.assertIsNotNone(self.manager.links["7001"].last_status_at)

    async def test_sent_bytes_match_exactly(self) -> None:
        await self._connect()
        result = await self.manager.async_send_packet(
            "7001", " FFA016F0 ", connect_timeout=1.0
        )
        self.assertIs(result, LanSendResult.SENT)
        await _wait_until(lambda: bytes(self.hub.received) == bytes.fromhex("ffa016f0"))
        self.assertIsNotNone(self.manager.links["7001"].last_tx_at)

    async def test_reconnects_after_hub_drops_connection(self) -> None:
        await self._connect()
        first_port = self.manager.links["7001"].local_port
        self.hub.drop()
        await _wait_until(lambda: len(self.hub.connect_ports) == 2)
        await _wait_until(lambda: self.manager.is_connected("7001"))
        link = self.manager.links["7001"]
        self.assertEqual(link.reconnect_count, 1)
        self.assertEqual(self.hub.connect_ports, [first_port, link.local_port])
        result = await self.manager.async_send_packet("7001", "ffa015f0", connect_timeout=1.0)
        self.assertIs(result, LanSendResult.SENT)
        await _wait_until(lambda: bytes(self.hub.received) == bytes.fromhex("ffa015f0"))

    async def test_send_connects_on_demand_after_wake(self) -> None:
        self.hub.accept_connect = False
        self.manager.sync_specs({"7001": _device()})
        await _wait_until(lambda: self.manager.links["7001"].consecutive_failures >= 1)
        self.hub.accept_connect = True
        result = await self.manager.async_send_packet("7001", "ffa016f0", connect_timeout=2.0)
        self.assertIs(result, LanSendResult.SENT)

    async def test_send_not_connected_when_hub_never_answers(self) -> None:
        self.hub.accept_connect = False
        self.manager.sync_specs({"7001": _device()})
        result = await self.manager.async_send_packet("7001", "ffa016f0", connect_timeout=0.2)
        self.assertIs(result, LanSendResult.NOT_CONNECTED)
        self.assertIsNotNone(self.manager.links["7001"].on_demand_failed_at)
        self.assertEqual(bytes(self.hub.received), b"")

    async def test_unknown_device_is_not_connected(self) -> None:
        result = await self.manager.async_send_packet("nope", "ffa016f0", connect_timeout=0.1)
        self.assertIs(result, LanSendResult.NOT_CONNECTED)

    async def test_uses_mac_string_from_search_reply(self) -> None:
        # The hub only accepts its own spelling; the cloud reports lower case.
        await self._connect(_device(mac="a0:b1:c2:d3:e4:f5"))
        self.assertEqual(len(self.hub.connect_ports), 1)

    async def test_mac_mismatch_never_connects(self) -> None:
        self.manager.sync_specs({"7001": _device(mac="FFFFFFFFFFFF")})
        await _wait_until(lambda: self.manager.links["7001"].consecutive_failures >= 2)
        link = self.manager.links["7001"]
        self.assertEqual(link.last_error, "connect_timeout")
        self.assertFalse(self.manager.is_connected("7001"))
        self.assertEqual(self.hub.connect_ports, [])

    async def test_shutdown_closes_link(self) -> None:
        await self._connect()
        await self.manager.shutdown()
        await _wait_until(lambda: self.hub.eof_count == 1)
        self.assertEqual(self.manager.links, {})

    async def test_sync_after_shutdown_does_not_restart_links(self) -> None:
        await self._connect()
        await self.manager.shutdown()
        self.manager.sync_specs({"7001": _device()})
        self.assertEqual(self.manager.links, {})
        self.assertEqual(self.manager._tasks, {})  # noqa: SLF001

    async def test_short_lived_links_back_off(self) -> None:
        await self._connect()
        self.hub.drop()
        await _wait_until(lambda: len(self.hub.connect_ports) == 2)
        await _wait_until(lambda: self.manager.is_connected("7001"))
        self.hub.drop()
        await _wait_until(lambda: not self.manager.is_connected("7001"))
        self.assertEqual(self.manager.links["7001"].consecutive_failures, 2)

    async def test_removed_hub_stops_its_link(self) -> None:
        await self._connect()
        self.manager.sync_specs({})
        await _wait_until(lambda: self.hub.eof_count == 1)
        self.assertNotIn("7001", self.manager.links)


class CoordinatorOverFakeHubTests(unittest.IsolatedAsyncioTestCase):
    """LAN mode in the coordinator reaches the fake hub and never the cloud."""

    async def asyncSetUp(self) -> None:
        from custom_components.aifa_smart.coordinator import (
            AifaAccountData,
            AifaSmartCoordinator,
        )

        self.hub = await FakeHub().start()
        self.harness = _ManagerHarness(self.hub)
        self.cloud_sent: list[tuple[str, str]] = []

        async def cloud_send(device_id, packet_hex):
            self.cloud_sent.append((device_id, packet_hex))
            return True

        coordinator = AifaSmartCoordinator.__new__(AifaSmartCoordinator)
        coordinator.data = AifaAccountData(  # type: ignore[attr-defined]
            devices={"7001": _device()}, macros=[], fetched_at=None,  # type: ignore[arg-type]
        )
        coordinator._classic_listener = types.SimpleNamespace(  # type: ignore[attr-defined]
            async_send_packet=cloud_send
        )
        coordinator._transport = "lan"  # type: ignore[attr-defined]
        coordinator._ac_apply_locks = {}  # type: ignore[attr-defined]
        coordinator._lan_manager = self.harness.manager  # type: ignore[attr-defined]
        coordinator._lan_failed_at = {}  # type: ignore[attr-defined]
        coordinator._transport_counters = {  # type: ignore[attr-defined]
            "sent_lan": 0,
            "sent_cloud": 0,
            "auto_fallbacks": 0,
            "lan_write_failures": 0,
            "last_transport": None,
        }
        self.coordinator = coordinator

    async def asyncTearDown(self) -> None:
        await self.harness.manager.shutdown()
        await self.hub.stop()

    def _command(self):
        from custom_components.aifa_smart.api import AifaFunctionCommand

        return AifaFunctionCommand(
            id="1", device_id="7001", sub_device_id="ac-1", command="ffa019f0"
        )

    async def test_lan_mode_sends_exact_bytes_to_hub(self) -> None:
        self.harness.manager.sync_specs(self.coordinator.data.devices)
        response = await self.coordinator._async_send_unconfirmed(self._command())  # noqa: SLF001
        self.assertEqual(response["transport"], "lan")
        await _wait_until(lambda: bytes(self.hub.received) == bytes.fromhex("ffa019f0"))
        self.assertEqual(self.cloud_sent, [])

    async def test_lan_mode_unreachable_hub_raises_without_cloud(self) -> None:
        from homeassistant.exceptions import HomeAssistantError

        self.hub.accept_connect = False
        self.harness.manager.sync_specs(self.coordinator.data.devices)
        with patch("custom_components.aifa_smart.coordinator.LAN_CONNECT_TIMEOUT", 0.3):
            with self.assertRaises(HomeAssistantError) as ctx:
                await self.coordinator._async_send_unconfirmed(self._command())  # noqa: SLF001
        self.assertEqual(ctx.exception.translation_key, "lan_unavailable")
        self.assertEqual(self.cloud_sent, [])
        self.assertEqual(bytes(self.hub.received), b"")


class _BrokenWriter:
    def __init__(self) -> None:
        self.closed = False

    def write(self, data: bytes) -> None:
        return None

    async def drain(self) -> None:
        raise ConnectionResetError("peer reset")

    def is_closing(self) -> bool:
        return self.closed

    def close(self) -> None:
        self.closed = True


class LanManagerUnitTests(unittest.IsolatedAsyncioTestCase):
    """Paths that are awkward to provoke with a real socket."""

    def _manager(self) -> LanTransportManager:
        return LanTransportManager(
            hass=_fake_hass(),  # type: ignore[arg-type]
            on_observed_status=lambda *args: False,
            on_data_changed=lambda: None,
        )

    async def test_write_failure_is_reported_separately(self) -> None:
        manager = self._manager()
        spec = lan.build_hub_specs({"7001": _device()}, require_online=False)["7001"]
        link = lan.LanHubLink(spec=spec)
        writer = _BrokenWriter()
        link.writer = writer  # type: ignore[assignment]
        manager.links["7001"] = link
        result = await manager.async_send_packet("7001", "ffa016f0", connect_timeout=0.1)
        self.assertIs(result, LanSendResult.WRITE_FAILED)
        self.assertIn("write_failed", link.last_error or "")
        # Closed so the read loop ends and the link reconnects.
        self.assertTrue(writer.closed)

    async def test_backoff_schedule(self) -> None:
        manager = self._manager()
        self.assertEqual(
            [manager._backoff_delay(n) for n in range(0, 10)],  # noqa: SLF001
            [1, 1, 2, 4, 8, 16, 32, 60, 60, 60],
        )


if __name__ == "__main__":
    unittest.main()
