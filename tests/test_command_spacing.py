"""Writes keep i-Ctrl Pro's queue spacing, on the LAN and on the cloud socket.

ConnectionRepository sends one packet, then holds the next for that
packet's delay: 600 ms after a key, 300 ms after a query, a 15-byte packet
or SetCode. A hub busy answering a query has been seen to drop a key that
arrives at the same time, and every report, including the query's answer,
confirms a +/- key; so the key that brings a link up must not ride along
with the connect query.
"""
from __future__ import annotations

import asyncio
import types
import unittest

import pytest
from homeassistant.core import HomeAssistant

from custom_components.aifa_smart.api import AifaMacro, AifaMacroCommand
from custom_components.aifa_smart.classic_listener import (
    KEY_QUEUE_DELAY,
    QUERY_QUEUE_DELAY,
    queue_delay,
)
from custom_components.aifa_smart.lan import LanSendResult, LanTransportManager
from tests.test_command_confirmation import (
    _QUERY,
    ConfirmingFakeHub,
    _build_coordinator,
    _cancel_pending_releases,
    _fast_timings,
    _hub_device,
    _NoCloud,
)
from tests.test_connect_write_delay import _timed_cloud
from tests.test_lan_fake_hub import _LOCALHOST, _wait_until

pytestmark = pytest.mark.usefixtures("socket_enabled")

_KEY = bytes.fromhex("ffa006f0")
_STATUS_15 = bytes.fromhex("ffa0011500002011000000000000f0")
# Sleeps never end early, so the gaps have no upper bound to race; this
# only absorbs the clock resolution.
_SLACK = 0.005


def test_delays_follow_the_app() -> None:
    assert KEY_QUEUE_DELAY == 0.6
    assert QUERY_QUEUE_DELAY == 0.3
    assert queue_delay(_KEY) == KEY_QUEUE_DELAY
    assert queue_delay(_QUERY) == QUERY_QUEUE_DELAY
    assert queue_delay(_STATUS_15) == QUERY_QUEUE_DELAY
    assert queue_delay(bytes.fromhex("fff5a00033f0")) == QUERY_QUEUE_DELAY
    # Anti mold's 6-byte packet is not in the app's delay table; wait like a key.
    assert queue_delay(bytes.fromhex("a1faac0000f0")) == KEY_QUEUE_DELAY
    # Macro steps carry learned IR codes; they wait like a key.
    assert queue_delay(bytes.fromhex("aabbccddeeff00")) == KEY_QUEUE_DELAY


class _TimedHub(ConfirmingFakeHub):
    """Records each chunk read from the connection with its arrival time."""

    def __init__(self) -> None:
        super().__init__()
        self.chunks: list[tuple[float, bytes]] = []

    def _heard(self, data: bytes, connected_at: float) -> bytes:
        self.chunks.append((asyncio.get_running_loop().time(), bytes(data)))
        return data

    def arrival(self, packet: bytes) -> float:
        for at, data in self.chunks:
            if packet in data:
                return at
        raise AssertionError(f"{packet.hex()} never arrived: {self.chunks}")


def _hass(*, eager: bool) -> types.SimpleNamespace:
    def create_task(coro, name=None, eager_start=True):
        loop = asyncio.get_running_loop()
        if eager:
            # Home Assistant starts background tasks eagerly by default.
            return asyncio.Task(coro, loop=loop, name=name, eager_start=True)
        return loop.create_task(coro, name=name)

    return types.SimpleNamespace(
        async_create_background_task=create_task, async_create_task=create_task
    )


class LanSpacingTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.hub = await _TimedHub().start()
        self.timings = _fast_timings()
        self.timings.__enter__()
        self.coordinator = None

    async def asyncTearDown(self) -> None:
        self.timings.__exit__(None, None, None)
        if self.coordinator is not None:
            await _cancel_pending_releases(self.coordinator)
        await self.manager.shutdown()
        await self.hub.stop()

    async def _broadcast(self) -> list[str]:
        return [_LOCALHOST]

    def _manager(self, hass) -> LanTransportManager:
        self.manager = LanTransportManager(
            hass=hass,
            on_observed_status=lambda *args: False,
            on_data_changed=lambda: None,
            broadcast_provider=self._broadcast,
            target_port=self.hub.port,
            search_source_port=0,
            bind_host=_LOCALHOST,
            backoff_schedule=(30.0,),
            connect_attempts=1,
            connect_interval=0.3,
            search_timeout=0.2,
        )
        return self.manager

    async def _press_key_that_wakes_the_link(self, *, eager: bool) -> None:
        hass = _hass(eager=eager)
        manager = self._manager(hass)
        self.coordinator = _build_coordinator(
            "lan",
            device=_hub_device(version=5, subversion=210623, device_code="115"),
            lan=manager,
            listener=_NoCloud(),
        )
        self.coordinator.hass = hass
        manager._on_observed_status = self.coordinator._apply_lan_observed_status  # noqa: SLF001
        manager._on_link_state_changed = self.coordinator._on_lan_link_state_changed  # noqa: SLF001
        self.hub.accept_connect = False
        manager.sync_specs(self.coordinator.data.devices)
        await _wait_until(lambda: manager.links["7001"].state == "backoff", timeout=5)
        self.hub.accept_connect = True
        await self.coordinator.async_press_classic_temperature_key("7001", "ac-1", up=True)
        await _wait_until(lambda: self.hub.queries >= 1, timeout=3)

    def _assert_not_in_one_write(self, *packets: bytes) -> None:
        for _, data in self.hub.chunks:
            self.assertFalse(
                all(packet in data for packet in packets),
                f"{[p.hex() for p in packets]} arrived together: {data.hex()}",
            )

    async def test_key_waking_the_link_follows_the_connect_query(self) -> None:
        await self._press_key_that_wakes_the_link(eager=True)
        self._assert_not_in_one_write(_QUERY, _KEY)
        gap = self.hub.arrival(_KEY) - self.hub.arrival(_QUERY)
        self.assertGreaterEqual(gap, QUERY_QUEUE_DELAY - _SLACK)

    async def test_connect_query_scheduled_after_the_key_still_waits(self) -> None:
        # Without eager tasks the key takes the write lock first.
        await self._press_key_that_wakes_the_link(eager=False)
        self._assert_not_in_one_write(_QUERY, _KEY)
        gap = self.hub.arrival(_QUERY) - self.hub.arrival(_KEY)
        self.assertGreaterEqual(gap, KEY_QUEUE_DELAY - _SLACK)

    async def test_each_write_waits_for_the_previous_ones_delay(self) -> None:
        manager = self._manager(_hass(eager=True))
        manager.sync_specs(
            _build_coordinator("lan", device=_hub_device(device_code="115")).data.devices
        )
        await _wait_until(lambda: manager.is_connected("7001"), timeout=5)
        for packet in (_KEY, _STATUS_15, _KEY):
            result = await manager.async_send_packet("7001", packet.hex(), connect_timeout=1.0)
            self.assertIs(result, LanSendResult.SENT)
        self.assertTrue(await manager.async_request_status("7001"))
        await _wait_until(lambda: self.hub.queries >= 1, timeout=3)
        writes = [data for _, data in self.hub.chunks]
        self.assertEqual(writes[:4], [_KEY, _STATUS_15, _KEY, _QUERY])
        times = [at for at, _ in self.hub.chunks[:4]]
        self.assertGreaterEqual(times[1] - times[0], KEY_QUEUE_DELAY - _SLACK)
        self.assertGreaterEqual(times[2] - times[1], QUERY_QUEUE_DELAY - _SLACK)
        self.assertGreaterEqual(times[3] - times[2], KEY_QUEUE_DELAY - _SLACK)

    async def test_macro_steps_keep_the_queue_spacing_and_their_own_delay(self) -> None:
        hass = _hass(eager=True)
        manager = self._manager(hass)
        self.coordinator = _build_coordinator(
            "lan", device=_hub_device(device_code="115"), lan=manager, listener=_NoCloud()
        )
        self.coordinator.hass = hass
        steps = ("aabbccdd", "aabbccee", "aabbccff")
        self.coordinator.data.macros.append(
            AifaMacro(
                id="m",
                name="Macro",
                commands=[
                    AifaMacroCommand(
                        id=str(index), macro_id="m", device_id="7001", sub_device_id=None,
                        command=packet, order_no=index, delay=delay,
                    )
                    for index, (packet, delay) in enumerate(zip(steps, (0, 1000, 0)))
                ],
            )
        )
        manager.sync_specs(self.coordinator.data.devices)
        await _wait_until(lambda: manager.is_connected("7001"), timeout=5)
        # The hub answers nothing these steps; none of them waits for it.
        self.hub.silence()
        await self.coordinator.async_execute_macro("m")
        await _wait_until(lambda: len(self.hub.chunks) >= len(steps), timeout=3)
        times = [self.hub.arrival(bytes.fromhex(packet)) for packet in steps]
        self.assertGreaterEqual(times[1] - times[0], KEY_QUEUE_DELAY - _SLACK)
        self.assertGreaterEqual(times[2] - times[1], 1.0 - _SLACK)
        self.assertEqual(self.hub.queries, 0)


async def test_cloud_key_after_the_connect_query_waits(hass: HomeAssistant) -> None:
    manager, client, timed = _timed_cloud(hass)
    try:
        manager.sync_specs(
            _build_coordinator("cloud", device=_hub_device(device_code="115")).data.devices
        )
        await _wait_until(lambda: client.writer.writes == [_QUERY], timeout=2.0)
        assert await manager.async_send_packet("7001", _KEY.hex())
        assert await manager.async_send_packet("7001", _STATUS_15.hex())
        assert client.writer.writes == [_QUERY, _KEY, _STATUS_15]
        offsets = timed.write_offsets
        assert offsets[1] - offsets[0] >= QUERY_QUEUE_DELAY - _SLACK
        assert offsets[2] - offsets[1] >= KEY_QUEUE_DELAY - _SLACK
    finally:
        await manager.shutdown()
