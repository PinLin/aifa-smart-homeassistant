"""The integration asks the hub only when i-Ctrl Pro would.

i-Ctrl Pro sends `ff f3 f2 f0` on connect and when the AC page opens, and
otherwise relies on what the hub pushes. A hub busy answering extra queries
has been seen to drop a key press that arrives at the same time.
"""
from __future__ import annotations

import asyncio
import socket
from datetime import timedelta
from unittest.mock import patch

import pytest
from homeassistant.const import EVENT_HOMEASSISTANT_STARTED
from homeassistant.core import CoreState, HomeAssistant
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import async_fire_time_changed

from custom_components.aifa_smart import classic_listener as listener_module
from custom_components.aifa_smart import coordinator as coordinator_module
from custom_components.aifa_smart.ac import CLASSIC_AC_QUERY_PACKET
from custom_components.aifa_smart.api import AifaClassicSocketSession
from custom_components.aifa_smart.classic_listener import (
    ClassicSocketManager,
    build_hub_specs,
)
from custom_components.aifa_smart.lan import LAN_STATUS_QUERY_PACKET, LanSendResult
from tests.test_command_confirmation import (
    _build_coordinator,
    _CloudListener,
    _first_cloud_report,
    status,
)
from tests.test_lan import _STATUS_COOL_26
from tests.test_lan_fake_hub import _device

_QUERY = bytes.fromhex("fff3f2f0")
_WAKE = bytes.fromhex("f1eea0f0")
_NOTIFY = bytes.fromhex("faf5010203f0")


class _Transport:
    def abort(self) -> None:
        return None


class _Socket:
    def __init__(self) -> None:
        self.options: list[tuple[int, int, int]] = []

    def setsockopt(self, level: int, option: int, value: int) -> None:
        self.options.append((level, option, value))


class _Writer:
    def __init__(self) -> None:
        self.writes: list[bytes] = []
        self.transport = _Transport()
        self.socket = _Socket()
        self._closing = False

    def get_extra_info(self, name: str, default=None):
        return self.socket if name == "socket" else default

    def write(self, data: bytes) -> None:
        self.writes.append(bytes(data))

    async def drain(self) -> None:
        return None

    def is_closing(self) -> bool:
        return self._closing

    def close(self) -> None:
        self._closing = True

    async def wait_closed(self) -> None:
        return None


class _Client:
    """Hands out one socket and records any other client call."""

    def __init__(self) -> None:
        self.reader = asyncio.StreamReader()
        self.writer = _Writer()
        self.opens = 0
        self.unexpected: list[str] = []

    async def async_open_classic_socket(self, mac: str, *, timeout: float = 6.0):
        self.opens += 1
        return AifaClassicSocketSession(
            reader=self.reader, writer=self.writer, initial_bytes=b""
        )

    def __getattr__(self, name: str):
        unexpected = self.__dict__.setdefault("unexpected", [])

        async def record(*args, **kwargs):
            unexpected.append(name)
            return {}

        return record


async def _until(predicate, timeout: float = 2.0) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not predicate():
        if loop.time() > deadline:
            raise AssertionError("condition not reached")
        await asyncio.sleep(0.01)


async def _let_time_pass(hass: HomeAssistant, *, hours: int) -> None:
    """Fire every pending timer, an hour of wall clock at a time."""
    now = dt_util.utcnow()
    for hour in range(1, hours + 1):
        async_fire_time_changed(hass, now + timedelta(hours=hour), fire_all=True)
        await hass.async_block_till_done()
        await asyncio.sleep(0)


class _Harness:
    def __init__(self, hass: HomeAssistant, **manager_options) -> None:
        self.client = _Client()
        self.observed: list[tuple[int, str]] = []
        self.refreshes = 0

        async def refresh() -> None:
            self.refreshes += 1

        self.manager = ClassicSocketManager(
            hass=hass,
            client=self.client,  # type: ignore[arg-type]
            on_observed_status=lambda spec, sub_type, observed: self.observed.append(
                (sub_type, observed.mode)
            )
            or True,
            on_sensor_state=lambda *args: False,
            on_data_changed=lambda: None,
            on_notify_full_refresh=refresh,
            **manager_options,
        )

    @property
    def writes(self) -> list[bytes]:
        return self.client.writer.writes

    async def connect(self) -> None:
        self.manager.sync_specs({"7001": _device()})
        await _until(lambda: self.manager.has_active_writer("7001") and self.writes)


@pytest.fixture
async def harness(hass: HomeAssistant):
    item = _Harness(hass)
    await item.connect()
    yield item
    await item.manager.shutdown()


async def test_cloud_listener_queries_once_on_connect(harness: _Harness) -> None:
    assert CLASSIC_AC_QUERY_PACKET == "fff3f2f0"
    assert harness.writes == [_QUERY]
    assert harness.client.opens == 1


async def test_cloud_socket_has_keepalive(harness: _Harness) -> None:
    """Keepalive is what notices a half-open link once nothing is written."""
    assert (socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1) in harness.client.writer.socket.options


async def test_listener_waits_for_home_assistant_to_start(hass: HomeAssistant) -> None:
    hass.set_state(CoreState.not_running)
    item = _Harness(hass)
    try:
        item.manager.sync_specs({"7001": _device()})
        await asyncio.sleep(0.05)
        assert item.client.opens == 0
        hass.set_state(CoreState.running)
        hass.bus.async_fire(EVENT_HOMEASSISTANT_STARTED)
        await _until(lambda: item.writes == [_QUERY])
        assert item.client.opens == 1
    finally:
        await item.manager.shutdown()


async def test_quick_drops_keep_growing_the_backoff(hass: HomeAssistant) -> None:
    """A relay that closes right after the handshake must not draw a query a second."""
    # Only the backoff is under test; the post-ack write delay would spend
    # real time on every one of the quick reconnects.
    item = _Harness(hass, connect_write_delay=0)
    real_open = item.client.async_open_classic_socket

    async def open_and_close(mac, *, timeout=6.0):
        session = await real_open(mac, timeout=timeout)
        session.reader.feed_eof()
        item.client.reader = asyncio.StreamReader()
        return session

    item.client.async_open_classic_socket = open_and_close  # type: ignore[method-assign]
    backoffs: list[float] = []
    real_sleep = asyncio.sleep

    async def fake_sleep(delay, *args, **kwargs):
        if delay >= 1:
            backoffs.append(delay)
            delay = 0
        return await real_sleep(delay, *args, **kwargs)

    try:
        with patch.object(listener_module.asyncio, "sleep", fake_sleep):
            item.manager.sync_specs({"7001": _device()})
            await _until(lambda: len(backoffs) >= 4)
    finally:
        await item.manager.shutdown()
    assert backoffs[:4] == [1.0, 2.0, 4.0, 8.0]


async def test_cloud_listener_sends_nothing_while_time_passes(
    hass: HomeAssistant, harness: _Harness
) -> None:
    await _let_time_pass(hass, hours=6)
    assert harness.writes == [_QUERY]
    assert harness.client.opens == 1
    # No short-lived query sockets either.
    assert harness.client.unexpected == []


async def test_notify_reloads_the_account_without_asking_the_hub(
    hass: HomeAssistant, harness: _Harness
) -> None:
    harness.client.reader.feed_data(_NOTIFY)
    await _until(lambda: harness.refreshes == 1)
    await _let_time_pass(hass, hours=1)
    assert harness.writes == [_QUERY]
    assert harness.client.unexpected == []
    assert harness.refreshes == 1


async def test_pushes_are_still_applied_and_wake_is_answered(
    hass: HomeAssistant, harness: _Harness
) -> None:
    harness.client.reader.feed_data(_WAKE + _STATUS_COOL_26)
    await _until(lambda: harness.observed == [(0, "cool")])
    await _until(lambda: len(harness.writes) == 2)
    assert harness.writes == [_QUERY, bytes.fromhex("ccdd")]


async def test_cloud_confirmation_queries_once_after_the_push_wait() -> None:
    """With no push, `ff f3 f2 f0` goes out once, AC_CONFIRM_PUSH_TIMEOUT later."""
    assert coordinator_module.AC_CONFIRM_PUSH_TIMEOUT == 1.5
    listener = _CloudListener()
    listener.push = False
    coordinator = _build_coordinator("cloud", listener=listener)
    listener.coordinator = coordinator
    _first_cloud_report(coordinator, "cool")
    loop = asyncio.get_running_loop()
    sent_at: list[tuple[str, float]] = []
    send = listener.async_send_packet

    async def timed_send(device_id: str, packet_hex: str) -> bool:
        sent_at.append((packet_hex, loop.time()))
        return await send(device_id, packet_hex)

    listener.async_send_packet = timed_send  # type: ignore[method-assign]
    with patch.object(coordinator_module, "_AC_SEND_MIN_INTERVAL_SECONDS", 0.0):
        await coordinator.async_apply_ac_state(
            "7001", "ac-1", device_code="136", hvac_mode="heat"
        )
    queries = [at for packet, at in sent_at if packet == "fff3f2f0"]
    commands = [at for packet, at in sent_at if packet != "fff3f2f0"]
    assert len(commands) == 1
    assert len(queries) == 1
    assert 1.4 <= queries[0] - commands[0] < 2.0


class _LanStub:
    """LAN link whose hub answers only the status query."""

    def __init__(self) -> None:
        self.coordinator = None
        self.events: list[tuple[str, float]] = []

    def is_connected(self, device_id: str) -> bool:
        return True

    async def async_send_packet(self, device_id, packet_hex, *, connect_timeout):
        self.events.append((packet_hex, asyncio.get_running_loop().time()))
        return LanSendResult.SENT

    async def async_request_status(self, device_id: str) -> bool:
        self.events.append((LAN_STATUS_QUERY_PACKET, asyncio.get_running_loop().time()))
        spec = build_hub_specs(self.coordinator.data.devices, require_online=False)["7001"]
        asyncio.get_running_loop().call_later(
            0.05,
            self.coordinator._apply_lan_observed_status,  # noqa: SLF001
            spec,
            0,
            status("heat"),
        )
        return True


async def test_lan_confirmation_queries_once_after_the_push_wait() -> None:
    assert LAN_STATUS_QUERY_PACKET == "fff3f2f0"
    lan = _LanStub()
    coordinator = _build_coordinator("lan", lan=lan)
    lan.coordinator = coordinator
    spec = build_hub_specs(coordinator.data.devices, require_online=False)["7001"]
    coordinator._apply_lan_observed_status(spec, 0, status("cool"))  # noqa: SLF001
    with patch.object(coordinator_module, "_AC_SEND_MIN_INTERVAL_SECONDS", 0.0):
        await coordinator.async_apply_ac_state(
            "7001", "ac-1", device_code="136", hvac_mode="heat"
        )
    queries = [at for packet, at in lan.events if packet == "fff3f2f0"]
    commands = [at for packet, at in lan.events if packet != "fff3f2f0"]
    assert len(commands) == 1
    assert len(queries) == 1
    assert 1.4 <= queries[0] - commands[0] < 2.0


async def test_pending_release_clears_without_writing_to_the_hub() -> None:
    listener = _CloudListener()
    coordinator = _build_coordinator("cloud", listener=listener)
    listener.coordinator = coordinator
    state = coordinator.get_ac_runtime_state("7001", "ac-1", device_code="136")
    state.command_status_pending = True
    with patch.object(coordinator_module, "AC_PENDING_RELEASE_SECONDS", 0.2):
        coordinator._schedule_ac_pending_release(  # noqa: SLF001
            "7001", "ac-1", expected_mode="cool"
        )
        await asyncio.sleep(0.1)
        assert state.command_status_pending
        await asyncio.sleep(0.2)
    assert not state.command_status_pending
    assert coordinator._ac_pending_release_tasks == {}  # noqa: SLF001
    assert listener.sent == []
