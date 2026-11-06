"""Regression tests for the review of the LAN transport batches."""
from __future__ import annotations

import asyncio
import types
import unittest

import pytest

from custom_components.aifa_smart.api import AifaFunctionCommand
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
from tests.test_coordinator_probe_guard import _Client, _Lan, _Listener
from tests.test_coordinator_probe_guard import _coordinator as _probe_coordinator
from tests.test_lan_fake_hub import _device, _wait_until
from tests.test_lan_status_query import QueryingFakeHub, _Harness

_CLOUD_HANG = 3.0


class _SentLan:
    def is_connected(self, device_id: str) -> bool:
        return True

    async def async_send_packet(self, device_id, packet_hex, *, connect_timeout):
        return LanSendResult.SENT


def _send_coordinator(transport: str):
    coordinator = AifaSmartCoordinator.__new__(AifaSmartCoordinator)
    coordinator._transport = transport  # type: ignore[attr-defined]
    coordinator._lan_manager = _SentLan()  # type: ignore[attr-defined]
    coordinator._lan_failed_at = {}  # type: ignore[attr-defined]
    coordinator._transport_counters = {  # type: ignore[attr-defined]
        "sent_lan": 0, "sent_cloud": 0, "auto_fallbacks": 0,
        "lan_write_failures": 0, "last_transport": None,
    }

    async def cloud_send(device_id, packet_hex):
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


def _command() -> AifaFunctionCommand:
    return AifaFunctionCommand(id="1", device_id="7001", sub_device_id="ac-1", command="ffa016f0")


class RefreshAfterSendTests(unittest.IsolatedAsyncioTestCase):
    async def test_lan_command_does_not_wait_for_the_cloud_refresh(self) -> None:
        coordinator, background = _send_coordinator(TRANSPORT_LAN)
        loop = asyncio.get_running_loop()
        started = loop.time()
        responses = await coordinator.async_send_function_commands([_command()])
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
        await coordinator.async_send_function_commands([_command()])
        self.assertGreaterEqual(loop.time() - started, 0.2)
        self.assertEqual(background, [])


class CloudFallbackTimeoutTests(unittest.TestCase):
    def test_lan_mode_caps_the_cloud_query(self) -> None:
        seen: list[float] = []

        class _TimedClient(_Client):
            async def async_query_classic_ac_statuses(self, mac, sub_types=None, timeout=6.0):
                seen.append(timeout)
                return {}

        coordinator = _probe_coordinator(
            TRANSPORT_LAN, listener=_Listener(writer=True), client=_TimedClient(), lan=_Lan(False)
        )
        asyncio.run(coordinator._async_probe_ac_observed_state("7001", "ac-1"))  # noqa: SLF001
        self.assertEqual(seen, [2.0])

    def test_cloud_mode_keeps_the_default_timeout(self) -> None:
        seen: list[float] = []

        class _TimedClient(_Client):
            async def async_query_classic_ac_statuses(self, mac, sub_types=None, timeout=6.0):
                seen.append(timeout)
                return {}

        coordinator = _probe_coordinator(
            TRANSPORT_CLOUD, listener=_Listener(), client=_TimedClient()
        )
        asyncio.run(coordinator._async_probe_ac_observed_state("7001", "ac-1"))  # noqa: SLF001
        self.assertEqual(seen, [6.0])


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

    async def test_unanswered_probe_query_closes_the_link(self) -> None:
        self.hub.answer_queries = False
        self.assertTrue(await self.manager.async_request_status("7001"))
        await _wait_until(lambda: self.hub.eof_count >= 1)
        self.assertEqual(self.manager.links["7001"].last_error, "query_timeout")
        await _wait_until(lambda: len(self.hub.connect_ports) == 2)

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
