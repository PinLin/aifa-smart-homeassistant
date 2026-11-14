"""Unloading the entry stops every connection, including ones a late refresh would open.

A refresh that is not an entry task (notify frame, refresh after a command,
the Refresh button) can still be running when the entry unloads for an
options change or a code change.
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.aifa_smart.api import AifaDeviceCodeCatalog, AifaSmartApiClient
from custom_components.aifa_smart.classic_listener import (
    ClassicStreamBuffers,
    process_classic_stream_chunk,
)
from custom_components.aifa_smart.const import TRANSPORT_CLOUD, TRANSPORT_LAN
from custom_components.aifa_smart.coordinator import _HubReportWaiter
from custom_components.aifa_smart.device_cache import cache_store_key
from tests.test_device_cache import _StubLan, _device, _entry, _macros


class _Cloud:
    """Cloud side of the client: a device list that can be held, and TLS opens."""

    def __init__(self) -> None:
        self.opens = 0
        self.closed = 0
        self.hold: asyncio.Event | None = None
        self.fetching = asyncio.Event()

    async def get_devices(self, client) -> list:
        self.fetching.set()
        if self.hold is not None:
            await self.hold.wait()
        return [_device()]

    async def open_socket(self, client, mac: str, *, timeout: float = 6.0):
        self.opens += 1
        try:
            await asyncio.Event().wait()
        finally:
            self.closed += 1


def _patches(cloud: _Cloud, *, stub_lan: bool) -> list:
    patches = [
        patch(
            "custom_components.aifa_smart.catalog.async_initialize",
            AsyncMock(return_value=lambda: None),
        ),
        patch.object(
            AifaSmartApiClient,
            "async_get_devices",
            lambda self: cloud.get_devices(self),
        ),
        patch.object(AifaSmartApiClient, "async_get_macros", AsyncMock(return_value=_macros())),
        patch.object(AifaSmartApiClient, "async_get_functions", AsyncMock(return_value=[])),
        patch.object(
            AifaSmartApiClient,
            "async_get_sub_device_device_codes",
            AsyncMock(return_value=AifaDeviceCodeCatalog()),
        ),
        patch.object(
            AifaSmartApiClient,
            "async_open_classic_socket",
            lambda self, mac, timeout=6.0: cloud.open_socket(self, mac, timeout=timeout),
        ),
    ]
    if stub_lan:
        patches.append(
            patch("custom_components.aifa_smart.coordinator.LanTransportManager", _StubLan)
        )
    return patches


async def _setup(hass: HomeAssistant, transport: str, cloud: _Cloud) -> MockConfigEntry:
    entry = _entry(hass, transport)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED
    return entry


async def _wait_for(predicate, timeout: float = 5.0) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not predicate():
        if loop.time() > deadline:
            raise AssertionError("condition not met in time")
        await asyncio.sleep(0.01)


@pytest.fixture
def cloud():
    cloud = _Cloud()
    patches = _patches(cloud, stub_lan=True)
    for item in patches:
        item.start()
    yield cloud
    for item in patches:
        item.stop()


@pytest.mark.asyncio
@pytest.mark.usefixtures("enable_custom_integrations")
async def test_unload_closes_the_cloud_listener(hass: HomeAssistant, cloud: _Cloud) -> None:
    entry = await _setup(hass, TRANSPORT_CLOUD, cloud)
    coordinator = entry.runtime_data
    await _wait_for(lambda: cloud.opens == 1)
    listener = coordinator._classic_listener  # noqa: SLF001

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()

    assert cloud.closed == 1
    assert not listener.has_active_listener("7001")
    assert listener._listener_tasks == {}  # noqa: SLF001
    # The base class stops the debouncer and ignores refreshes from now on.
    assert coordinator._shutdown_requested is True  # noqa: SLF001


@pytest.mark.asyncio
@pytest.mark.usefixtures("enable_custom_integrations")
async def test_refresh_finishing_after_unload_opens_no_cloud_connection(
    hass: HomeAssistant, cloud: _Cloud
) -> None:
    entry = await _setup(hass, TRANSPORT_CLOUD, cloud)
    coordinator = entry.runtime_data
    await _wait_for(lambda: cloud.opens == 1)

    cloud.hold = asyncio.Event()
    cloud.fetching.clear()
    # Same as a notify frame: a hass task, not an entry task.
    refresh = hass.async_create_background_task(
        coordinator.async_request_refresh(), name="late refresh"
    )
    await asyncio.wait_for(cloud.fetching.wait(), timeout=5)
    assert await hass.config_entries.async_unload(entry.entry_id)
    cloud.hold.set()
    await refresh
    await hass.async_block_till_done()

    assert cloud.opens == 1
    assert coordinator._classic_listener._listener_tasks == {}  # noqa: SLF001


@pytest.mark.asyncio
@pytest.mark.usefixtures("enable_custom_integrations")
async def test_refresh_finishing_after_unload_leaves_lan_and_cache_alone(
    hass: HomeAssistant, hass_storage: dict, cloud: _Cloud
) -> None:
    entry = await _setup(hass, TRANSPORT_LAN, cloud)
    coordinator = entry.runtime_data
    lan = coordinator.lan_manager
    synced = len(lan.synced)

    cloud.hold = asyncio.Event()
    cloud.fetching.clear()
    refresh = hass.async_create_background_task(
        coordinator._async_refresh(), name="late refresh"  # noqa: SLF001
    )
    await asyncio.wait_for(cloud.fetching.wait(), timeout=5)
    assert await hass.config_entries.async_unload(entry.entry_id)
    # The next entry writes its own cache; the old one must not overwrite it.
    hass_storage.pop(cache_store_key(entry.entry_id), None)
    cloud.hold.set()
    await refresh
    await hass.async_block_till_done()

    assert len(lan.synced) == synced
    assert cache_store_key(entry.entry_id) not in hass_storage
    assert coordinator._classic_listener._listener_tasks == {}  # noqa: SLF001


# ----- A SetCode echo is not a notify frame -----

_ECHO = bytes.fromhex("faf5a00033f0")


async def _parse(chunk: bytes, ack_consumed: bool) -> tuple[int, list]:
    notified = 0
    acks: list = []

    def on_notify() -> None:
        nonlocal notified
        notified += 1

    def on_ack(slot: int, high: int, low: int) -> bool:
        acks.append((slot, high, low))
        return ack_consumed and (slot, high, low) == (0xA0, 0x00, 0x33)

    async def reply_wake() -> None:
        return None

    await process_classic_stream_chunk(
        chunk,
        ClassicStreamBuffers(),
        device_type=None,
        reply_wake=reply_wake,
        on_status=lambda sub_type, observed: False,
        on_sensor=lambda sensor: False,
        on_notify=on_notify,
        on_set_code_ack=on_ack,
    )
    return notified, acks


@pytest.mark.asyncio
async def test_echo_of_our_own_set_code_draws_no_refresh() -> None:
    notified, acks = await _parse(_ECHO, ack_consumed=True)
    assert acks == [(0xA0, 0x00, 0x33)]
    assert notified == 0


@pytest.mark.asyncio
async def test_frame_nobody_waits_for_still_refreshes() -> None:
    # A code changed in the AIFA Smart app echoes the same way and should reload the account.
    notified, acks = await _parse(_ECHO, ack_consumed=False)
    assert acks == [(0xA0, 0x00, 0x33)]
    assert notified == 1


@pytest.mark.asyncio
async def test_other_frame_in_the_same_chunk_still_refreshes() -> None:
    notified, _ = await _parse(_ECHO + bytes.fromhex("faf5010203f0"), ack_consumed=True)
    assert notified == 1


def test_coordinator_claims_an_echo_only_during_its_own_code_change() -> None:
    from tests.test_command_confirmation import _build_coordinator

    coordinator = _build_coordinator("cloud")
    assert coordinator._record_set_code_ack("7001", "cloud", 0xA0, 0x00, 0x33) is False  # noqa: SLF001
    coordinator._hub_report_waiters["7001"] = [_HubReportWaiter()]  # noqa: SLF001
    assert coordinator._record_set_code_ack("7001", "cloud", 0xA0, 0x00, 0x33) is False  # noqa: SLF001
    coordinator._hub_report_waiters["7001"].append(_HubReportWaiter(changing_code=True))  # noqa: SLF001
    assert coordinator._record_set_code_ack("7001", "cloud", 0xA0, 0x00, 0x33) is True  # noqa: SLF001
    # Not a SetCode slot, so not an answer to ours.
    assert coordinator._record_set_code_ack("7001", "cloud", 0x01, 0x02, 0x03) is False  # noqa: SLF001
