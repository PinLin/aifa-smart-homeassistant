"""LAN status queries and half-open link detection against a fake hub.

The replies mirror a v5/210623 hub: after `ff f3 f2 f0` the hub
sends `fea0 0115 0000 20 11 … f0` for slot 0 and `fea1 0179 0000 … f0` for
the empty slot 1.
"""
from __future__ import annotations

import asyncio
import unittest

import pytest

from custom_components.aifa_smart.ac import decode_observed_status_packet
from custom_components.aifa_smart.classic_listener import build_hub_specs
from custom_components.aifa_smart.coordinator import (
    AifaAccountData,
    AifaSmartCoordinator,
)
from custom_components.aifa_smart.lan import (
    LAN_IDLE_QUERY_SECONDS,
    LAN_RECENT_RX_SECONDS,
    LAN_STATUS_QUERY_PACKET,
    LanTransportManager,
)
from tests.test_lan_fake_hub import (
    _HUB_MAC,
    _LOCALHOST,
    FakeHub,
    _device,
    _fake_hass,
    _wait_until,
)

pytestmark = pytest.mark.usefixtures("socket_enabled")

_QUERY = bytes.fromhex(LAN_STATUS_QUERY_PACKET)
_SLOT0_COOL = bytes.fromhex("fea00115000020110000000000f0")
_SLOT1_EMPTY = bytes.fromhex("fea10179000000000000000000f0")


class QueryingFakeHub(FakeHub):
    """Answers status queries the way the real hub did, unless muted."""

    def __init__(self) -> None:
        super().__init__(_HUB_MAC)
        self.answer_queries = True
        self.queries = 0

    async def _connect_back(self, addr: tuple[str, int]) -> None:
        try:
            reader, writer = await asyncio.open_connection(addr[0], addr[1])
        except OSError:
            return
        self.connect_ports.append(addr[1])
        self.writers.append(writer)
        pending = b""
        while True:
            data = await reader.read(4096)
            if not data:
                self.eof_count += 1
                return
            self.received.extend(data)
            pending += data
            while _QUERY in pending:
                pending = pending.split(_QUERY, 1)[1]
                self.queries += 1
                if self.answer_queries:
                    await asyncio.sleep(0.02)
                    writer.write(_SLOT0_COOL)
                    await writer.drain()
                    await asyncio.sleep(0.02)
                    writer.write(_SLOT1_EMPTY)
                    await writer.drain()


class _Harness:
    def __init__(self, hub: FakeHub, *, on_status=None, **kwargs) -> None:
        self.observed: list[tuple[int, object]] = []
        self._forward = on_status
        self.manager = LanTransportManager(
            hass=_fake_hass(),  # type: ignore[arg-type]
            on_observed_status=self._on_status,
            on_data_changed=lambda: None,
            broadcast_provider=self._broadcast,
            target_port=hub.port,
            search_source_port=0,
            bind_host=_LOCALHOST,
            backoff_schedule=(0.05,),
            connect_attempts=2,
            connect_interval=0.3,
            search_timeout=0.2,
            **kwargs,
        )

    async def _broadcast(self) -> list[str]:
        return [_LOCALHOST]

    def _on_status(self, spec, sub_type, observed) -> bool:
        self.observed.append((sub_type, observed))
        if self._forward is not None:
            return self._forward(spec, sub_type, observed)
        return True


class LanStatusQueryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.hub = await QueryingFakeHub().start()

    async def asyncTearDown(self) -> None:
        await self.harness.manager.shutdown()
        await self.hub.stop()

    async def _connect(self, **kwargs) -> LanTransportManager:
        self.harness = _Harness(self.hub, **kwargs)
        manager = self.harness.manager
        manager.sync_specs({"7001": _device()})
        await _wait_until(lambda: manager.is_connected("7001"))
        return manager

    async def test_query_is_answered_with_both_slots(self) -> None:
        manager = await self._connect()
        self.assertTrue(await manager.async_request_status("7001"))
        await _wait_until(lambda: len(self.harness.observed) == 2)
        self.assertEqual(bytes(self.hub.received), _QUERY)
        (slot0, status0), (slot1, status1) = self.harness.observed
        self.assertEqual((slot0, status0.mode), (0, "cool"))  # type: ignore[attr-defined]
        self.assertEqual((slot1, status1.mode), (1, "off"))  # type: ignore[attr-defined]
        self.assertIsNotNone(manager.links["7001"].last_status_at)

    async def test_query_without_link_reports_false(self) -> None:
        manager = await self._connect()
        self.assertFalse(await manager.async_request_status("other"))

    async def test_idle_link_is_queried_and_stays_up(self) -> None:
        manager = await self._connect(idle_query_after=0.2, query_reply_timeout=1.0)
        await _wait_until(lambda: self.hub.queries >= 2)
        self.assertTrue(manager.is_connected("7001"))
        self.assertEqual(len(self.hub.connect_ports), 1)
        self.assertIsNone(manager.links["7001"].last_error)

    async def test_unanswered_query_drops_and_reconnects(self) -> None:
        self.hub.answer_queries = False
        manager = await self._connect(idle_query_after=0.2, query_reply_timeout=0.2)
        await _wait_until(lambda: self.hub.eof_count >= 1)
        self.assertEqual(manager.links["7001"].last_error, "idle_timeout")
        await _wait_until(lambda: len(self.hub.connect_ports) == 2)
        await _wait_until(lambda: manager.is_connected("7001"))
        self.assertEqual(manager.links["7001"].reconnect_count, 1)

    async def test_unanswered_idle_query_waits_while_the_hub_still_sends(self) -> None:
        self.hub.answer_queries = False
        manager = await self._connect(idle_query_after=0.2, query_reply_timeout=0.2)
        await self.hub.push(bytes.fromhex("f1aaa0f0"))
        # A second query proves the first miss was put off, not fatal.
        await _wait_until(lambda: self.hub.queries >= 2)
        self.assertTrue(manager.is_connected("7001"))
        self.assertEqual(len(self.hub.connect_ports), 1)
        self.assertEqual(self.hub.eof_count, 0)
        self.assertIsNone(manager.links["7001"].last_error)

    async def test_unanswered_idle_query_drops_once_the_last_bytes_age_out(self) -> None:
        self.hub.answer_queries = False
        manager = await self._connect(
            idle_query_after=0.2, query_reply_timeout=0.2, recent_rx_window=1.0
        )
        await self.hub.push(bytes.fromhex("f1aaa0f0"))
        await _wait_until(lambda: self.hub.eof_count >= 1)
        self.assertEqual(manager.links["7001"].last_error, "idle_timeout")
        # Put off once while the push was recent, then dropped.
        self.assertGreaterEqual(self.hub.queries, 2)
        self.hub.answer_queries = True
        await _wait_until(lambda: len(self.hub.connect_ports) == 2)

    async def test_default_ratios_never_put_off_an_unanswered_idle_query(self) -> None:
        # 60 / 5 / 30 scaled down: the query only goes out after the window
        # has passed, so a byte before it cannot hold the link up.
        scale = 100.0
        self.hub.answer_queries = False
        manager = await self._connect(
            idle_query_after=LAN_IDLE_QUERY_SECONDS / scale,
            query_reply_timeout=5.0 / scale,
            recent_rx_window=LAN_RECENT_RX_SECONDS / scale,
            connect_write_delay=0.0,
        )
        await self.hub.push(bytes.fromhex("f1aaa0f0"))
        await _wait_until(lambda: self.hub.eof_count >= 1)
        self.assertEqual(self.hub.queries, 1)
        self.assertEqual(manager.links["7001"].last_error, "idle_timeout")

    async def test_link_with_no_bytes_at_all_drops_without_waiting(self) -> None:
        self.hub.answer_queries = False
        manager = await self._connect(
            idle_query_after=0.2, query_reply_timeout=0.2, recent_rx_window=30.0
        )
        loop = asyncio.get_running_loop()
        started = loop.time()
        await _wait_until(lambda: self.hub.eof_count >= 1)
        self.assertLess(loop.time() - started, 5.0)
        self.assertEqual(self.hub.queries, 1)
        self.assertEqual(manager.links["7001"].last_error, "idle_timeout")

    async def test_any_inbound_byte_resets_the_idle_timer(self) -> None:
        self.hub.answer_queries = False
        manager = await self._connect(idle_query_after=0.4, query_reply_timeout=0.2)
        for _ in range(4):
            await asyncio.sleep(0.2)
            await self.hub.push(bytes.fromhex("f1aaa0f0"))
        self.assertEqual(self.hub.queries, 0)
        self.assertTrue(manager.is_connected("7001"))


def test_defaults_match_the_hub_protocol() -> None:
    assert LAN_IDLE_QUERY_SECONDS == 60.0
    assert LAN_RECENT_RX_SECONDS == 30.0
    assert LAN_STATUS_QUERY_PACKET == "fff3f2f0"


class SlotIsolationTests(unittest.IsolatedAsyncioTestCase):
    """Slot 1 packets must not land on the slot 0 air conditioner."""

    async def asyncSetUp(self) -> None:
        self.hub = await QueryingFakeHub().start()
        coordinator = AifaSmartCoordinator.__new__(AifaSmartCoordinator)
        coordinator.data = AifaAccountData(  # type: ignore[attr-defined]
            devices={"7001": _device()}, macros=[], fetched_at=None,  # type: ignore[arg-type]
        )
        coordinator._ac_runtime_states = {}  # type: ignore[attr-defined]
        self.coordinator = coordinator
        self.harness = _Harness(
            self.hub, on_status=coordinator._apply_classic_observed_status  # noqa: SLF001
        )

    async def asyncTearDown(self) -> None:
        await self.harness.manager.shutdown()
        await self.hub.stop()

    async def test_slot1_reply_leaves_slot0_state_alone(self) -> None:
        manager = self.harness.manager
        manager.sync_specs(self.coordinator.data.devices)
        await _wait_until(lambda: manager.is_connected("7001"))
        await manager.async_request_status("7001")
        await _wait_until(lambda: len(self.harness.observed) == 2)
        state = self.coordinator.get_ac_runtime_state("7001", "ac-1")
        self.assertIsNotNone(state.observed_status)
        self.assertEqual(state.observed_status.mode, "cool")  # type: ignore[union-attr]
        self.assertEqual(state.observed_revision, 1)
        self.assertEqual(
            set(self.coordinator._ac_runtime_states), {("7001", "ac-1")}  # noqa: SLF001
        )

    async def test_slot1_alone_is_ignored(self) -> None:
        spec = build_hub_specs(self.coordinator.data.devices, require_online=False)["7001"]
        sub_type, observed = decode_observed_status_packet(_SLOT1_EMPTY)  # type: ignore[misc]
        self.assertEqual(sub_type, 1)
        updated = self.coordinator._apply_classic_observed_status(  # noqa: SLF001
            spec, sub_type, observed
        )
        self.assertFalse(updated)
        state = self.coordinator.get_ac_runtime_state("7001", "ac-1")
        self.assertIsNone(state.observed_status)


if __name__ == "__main__":
    unittest.main()
