"""Writes wait until the hub listens on a new connection.

On the real hub a status query written right after the LAN connection
opened drew nothing, while the same query 0.5 s later was answered.
The AIFA Smart app waits 500 ms after connecting (LAN) or after the relay's
handshake ack (cloud) before it reports the link connected and writes.
"""
from __future__ import annotations

import asyncio
import unittest

import pytest
from homeassistant.core import HomeAssistant

from custom_components.aifa_smart import lan
from custom_components.aifa_smart.classic_listener import (
    CLASSIC_CONNECT_WRITE_DELAY,
    ClassicSocketManager,
)
from custom_components.aifa_smart.lan import LAN_CONNECT_WRITE_DELAY, LanTransportManager
from tests.test_app_query_parity import _Client
from tests.test_command_confirmation import (
    _HEAT,
    _QUERY,
    ConfirmingFakeHub,
    _build_coordinator,
    _cancel_pending_releases,
    _fast_timings,
    _has_report,
    _hub_device,
    _NoCloud,
)
from tests.test_lan_fake_hub import _LOCALHOST, _device, _fake_hass, _wait_until

pytestmark = pytest.mark.usefixtures("socket_enabled")

_DEAF_SECONDS = 0.5


class DeafFakeHub(ConfirmingFakeHub):
    """Drops every byte that arrives in the first 0.5 s of a connection."""

    def __init__(self) -> None:
        super().__init__()
        self.dropped = bytearray()

    def _heard(self, data: bytes, connected_at: float) -> bytes:
        elapsed = asyncio.get_running_loop().time() - connected_at
        if elapsed < _DEAF_SECONDS:
            self.dropped.extend(data)
            return b""
        return data


class LanConnectWriteDelayTests(unittest.IsolatedAsyncioTestCase):
    """Real sockets to a fake hub that ignores the start of each connection."""

    write_delay = LAN_CONNECT_WRITE_DELAY

    async def asyncSetUp(self) -> None:
        self.hub = await DeafFakeHub().start()
        self.hub.mode_byte = _HEAT
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

    async def _connect(self, *, query_on_connect: bool) -> None:
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
            connect_write_delay=self.write_delay,
        )
        self.coordinator = _build_coordinator(
            "lan", device=_hub_device(), lan=self.manager, listener=_NoCloud()
        )
        self.manager._on_observed_status = self.coordinator._apply_lan_observed_status  # noqa: SLF001
        if query_on_connect:
            self.manager._on_link_state_changed = self.coordinator._on_lan_link_state_changed  # noqa: SLF001
        self.manager.sync_specs(self.coordinator.data.devices)
        await _wait_until(lambda: self.manager.is_connected("7001"))

    def _state(self):
        return self.coordinator.get_ac_runtime_state("7001", "ac-1")

    async def test_connect_query_waits_and_draws_the_report(self) -> None:
        await self._connect(query_on_connect=True)
        await _wait_until(_has_report(self.coordinator), timeout=2.0)
        self.assertEqual(bytes(self.hub.dropped), b"")
        self.assertEqual(self.hub.queries, 1)
        self.assertEqual(self._state().exposed_hvac_mode, "heat")

    async def test_command_right_after_connect_is_held_and_confirmed(self) -> None:
        await self._connect(query_on_connect=False)
        # The confirmation timeouts are 0.3 s here, shorter than the write
        # delay: the wait must start once the command is actually written.
        await self.coordinator.async_apply_ac_state(
            "7001", "ac-1", device_code="136", hvac_mode="on"
        )
        self.assertEqual(bytes(self.hub.dropped), b"")
        self.assertEqual(self.hub.commands, ["ffa0a1f0"])
        self.assertEqual(self._state().exposed_hvac_mode, "heat")

    async def test_command_racing_the_connect_query_is_not_dropped(self) -> None:
        await self._connect(query_on_connect=True)
        await self.coordinator.async_apply_ac_state(
            "7001", "ac-1", device_code="136", hvac_mode="on"
        )
        self.assertEqual(bytes(self.hub.dropped), b"")
        self.assertEqual(self.hub.commands, ["ffa0a1f0"])
        # Whichever took the write lock second waits out the first's queue
        # delay, so the query may still be on its way.
        await _wait_until(lambda: self.hub.queries >= 1, timeout=2.0)
        await asyncio.sleep(0.2)
        self.assertEqual(self.hub.queries, 1)


class LanWithoutWriteDelayTests(unittest.IsolatedAsyncioTestCase):
    """Control: with no delay the fake hub does drop the connect query."""

    async def test_connect_query_is_lost_without_the_delay(self) -> None:
        hub = await DeafFakeHub().start()
        manager = LanTransportManager(
            hass=_fake_hass(),  # type: ignore[arg-type]
            on_observed_status=lambda *args: False,
            on_data_changed=lambda: None,
            broadcast_provider=lambda: asyncio.sleep(0, [_LOCALHOST]),
            target_port=hub.port,
            search_source_port=0,
            bind_host=_LOCALHOST,
            backoff_schedule=(0.05,),
            connect_attempts=2,
            connect_interval=0.3,
            search_timeout=0.2,
            connect_write_delay=0,
        )
        try:
            manager.sync_specs({"7001": _device()})
            await _wait_until(lambda: manager.is_connected("7001"))
            self.assertTrue(await manager.async_request_status("7001"))
            await asyncio.sleep(_DEAF_SECONDS + 0.2)
            self.assertEqual(bytes(hub.dropped), _QUERY)
            self.assertEqual(hub.queries, 0)
            self.assertIsNone(manager.links["7001"].last_rx_at)
        finally:
            await manager.shutdown()
            await hub.stop()


class _TimedWriter:
    """Records when each write happened, relative to the socket opening."""

    def __init__(self, writer, opened_at: list[float]) -> None:
        self._writer = writer
        self._opened_at = opened_at
        self.write_offsets: list[float] = []

    def write(self, data: bytes) -> None:
        loop_time = asyncio.get_running_loop().time()
        self.write_offsets.append(loop_time - self._opened_at[-1])
        self._writer.write(data)

    def __getattr__(self, name: str):
        return getattr(self._writer, name)


def _timed_cloud(hass: HomeAssistant) -> tuple[ClassicSocketManager, _Client, _TimedWriter]:
    client = _Client()
    opened_at: list[float] = []
    timed = _TimedWriter(client.writer, opened_at)
    real_open = client.async_open_classic_socket

    async def open_after_ack(mac, *, timeout=6.0):
        session = await real_open(mac, timeout=timeout)
        opened_at.append(asyncio.get_running_loop().time())
        session.writer = timed
        return session

    client.async_open_classic_socket = open_after_ack  # type: ignore[method-assign]
    manager = ClassicSocketManager(
        hass=hass,
        client=client,  # type: ignore[arg-type]
        on_observed_status=lambda *args: False,
        on_sensor_state=lambda *args: False,
        on_data_changed=lambda: None,
        on_notify_full_refresh=lambda: asyncio.sleep(0),
    )
    return manager, client, timed


async def test_cloud_connect_query_waits_after_the_ack(hass: HomeAssistant) -> None:
    assert CLASSIC_CONNECT_WRITE_DELAY == 0.5
    manager, client, timed = _timed_cloud(hass)
    try:
        manager.sync_specs({"7001": _device()})
        await _wait_until(lambda: client.writer.writes == [_QUERY], timeout=2.0)
        assert timed.write_offsets[0] >= CLASSIC_CONNECT_WRITE_DELAY
    finally:
        await manager.shutdown()


async def test_cloud_command_right_after_the_ack_is_held(hass: HomeAssistant) -> None:
    manager, client, timed = _timed_cloud(hass)
    try:
        manager.sync_specs({"7001": _device()})
        await _wait_until(lambda: manager.has_active_writer("7001"))
        assert client.writer.writes == []
        assert await manager.async_send_packet("7001", "ffa0a1f0")
        assert bytes.fromhex("ffa0a1f0") in client.writer.writes
        assert min(timed.write_offsets) >= CLASSIC_CONNECT_WRITE_DELAY
    finally:
        await manager.shutdown()


def test_lan_delay_matches_the_app() -> None:
    # ConnectionRepository._onConnected awaits Future.delayed(500000 us).
    assert lan.LAN_CONNECT_WRITE_DELAY == 0.5
