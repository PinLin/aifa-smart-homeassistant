"""Regression tests for the review of the LAN transport batches."""
from __future__ import annotations

import asyncio
import types
import unittest

import pytest

from custom_components.aifa_smart.api import AifaMacro, AifaMacroCommand
from custom_components.aifa_smart.const import TRANSPORT_CLOUD, TRANSPORT_LAN
from custom_components.aifa_smart.coordinator import (
    AifaAccountData,
    AifaSmartCoordinator,
)
from custom_components.aifa_smart.device_cache import (
    AifaDeviceCache,
    deserialize_account,
    serialize_account,
)
from custom_components.aifa_smart.lan import LanSendResult
from tests.hub_reports import report_soon
from tests.test_lan_fake_hub import _device, _wait_until
from tests.test_lan_status_query import QueryingFakeHub, _Harness

_CLOUD_HANG = 3.0


class _SentLan:
    def __init__(self, coordinator) -> None:
        self.coordinator = coordinator

    def is_connected(self, device_id: str) -> bool:
        return True

    async def async_send_packet(self, device_id, packet_hex, *, connect_timeout):
        report_soon(self.coordinator, device_id, "lan")
        return LanSendResult.SENT

    async def async_request_status(self, device_id):
        return True


def _send_coordinator(transport: str):
    coordinator = AifaSmartCoordinator.__new__(AifaSmartCoordinator)
    coordinator.data = AifaAccountData(  # type: ignore[attr-defined]
        devices={},
        macros=[
            AifaMacro(
                id="m",
                name="Macro",
                commands=[
                    AifaMacroCommand(
                        id="1", macro_id="m", device_id="7001", sub_device_id="ac-1",
                        command="ffa016f0",
                    )
                ],
            )
        ],
        fetched_at=None,  # type: ignore[arg-type]
    )
    coordinator._transport = transport  # type: ignore[attr-defined]
    coordinator._lan_manager = _SentLan(coordinator)  # type: ignore[attr-defined]
    coordinator._lan_failed_at = {}  # type: ignore[attr-defined]
    coordinator._hub_report_waiters = {}  # type: ignore[attr-defined]
    coordinator._ac_apply_locks = {}  # type: ignore[attr-defined]
    coordinator._transport_counters = {  # type: ignore[attr-defined]
        "sent_lan": 0, "sent_cloud": 0, "auto_fallbacks": 0,
        "lan_write_failures": 0, "last_transport": None,
    }

    async def cloud_send(device_id, packet_hex):
        report_soon(coordinator, device_id, "cloud")
        return True

    coordinator._classic_listener = types.SimpleNamespace(  # type: ignore[attr-defined]
        async_send_packet=cloud_send
    )
    background: list[asyncio.Task] = []

    def create_background_task(coro, name=None, eager_start=True):
        task = asyncio.get_running_loop().create_task(coro)
        background.append(task)
        return task

    coordinator.hass = types.SimpleNamespace(  # type: ignore[assignment]
        async_create_background_task=create_background_task
    )

    async def hanging_refresh() -> None:
        await asyncio.sleep(_CLOUD_HANG)

    coordinator.async_request_refresh = hanging_refresh  # type: ignore[method-assign]
    return coordinator, background


class RefreshAfterSendTests(unittest.IsolatedAsyncioTestCase):
    async def test_lan_command_does_not_wait_for_the_cloud_refresh(self) -> None:
        coordinator, background = _send_coordinator(TRANSPORT_LAN)
        loop = asyncio.get_running_loop()
        started = loop.time()
        responses = await coordinator.async_execute_macro("m")
        self.assertLess(loop.time() - started, 0.5)
        self.assertEqual(responses[0]["transport"], "lan")
        self.assertEqual(len(background), 1)
        background[0].cancel()

    async def test_cloud_command_still_awaits_the_refresh(self) -> None:
        coordinator, background = _send_coordinator(TRANSPORT_CLOUD)

        async def quick_refresh() -> None:
            await asyncio.sleep(0.2)

        coordinator.async_request_refresh = quick_refresh  # type: ignore[method-assign]
        loop = asyncio.get_running_loop()
        started = loop.time()
        await coordinator.async_execute_macro("m")
        self.assertGreaterEqual(loop.time() - started, 0.2)
        self.assertEqual(background, [])


@pytest.mark.usefixtures("socket_enabled")
class ProbeQueryWatchdogTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.hub = await QueryingFakeHub().start()
        self.harness = _Harness(self.hub, idle_query_after=60.0, query_reply_timeout=0.2)
        self.manager = self.harness.manager
        self.manager.sync_specs({"7001": _device()})
        await _wait_until(lambda: self.manager.is_connected("7001"))

    async def asyncTearDown(self) -> None:
        await self.manager.shutdown()
        await self.hub.stop()

    async def test_unanswered_probe_query_keeps_the_link(self) -> None:
        self.hub.answer_queries = False
        self.assertTrue(await self.manager.async_request_status("7001"))
        await _wait_until(lambda: self.hub.queries == 1)
        await asyncio.sleep(0.6)
        self.assertTrue(self.manager.is_connected("7001"))
        self.assertEqual(self.hub.eof_count, 0)
        self.assertEqual(len(self.hub.connect_ports), 1)
        self.assertIsNone(self.manager.links["7001"].last_error)

    async def test_answered_probe_query_keeps_the_link(self) -> None:
        self.assertTrue(await self.manager.async_request_status("7001"))
        await _wait_until(lambda: len(self.harness.observed) == 2)
        await asyncio.sleep(0.4)
        self.assertTrue(self.manager.is_connected("7001"))
        self.assertEqual(self.hub.eof_count, 0)


def test_non_integer_version_is_rejected() -> None:
    body = serialize_account({"7001": _device()}, [])
    body["devices"][0]["version"] = "5"
    with pytest.raises(ValueError):
        deserialize_account(body)


@pytest.mark.asyncio
async def test_bad_saved_at_does_not_break_loading(hass, hass_storage) -> None:
    cache = AifaDeviceCache(hass, "entry1")
    hass_storage["aifa_smart.entry1.devices"] = {
        "version": 1,
        "minor_version": 1,
        "key": "aifa_smart.entry1.devices",
        "data": {**serialize_account({"7001": _device()}, []), "saved_at": "2000-13-45T00:00:00"},
    }
    loaded = await cache.async_load()
    assert loaded is not None
    devices, _macros, saved_at = loaded
    assert list(devices) == ["7001"]
    assert saved_at is None
