"""Transport selection in the coordinator: cloud, LAN only, and automatic."""
from __future__ import annotations

import asyncio
import json
import types
import unittest
from pathlib import Path
from unittest.mock import patch

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.aifa_smart import coordinator as coordinator_module
from custom_components.aifa_smart.api import AifaFunctionCommand
from custom_components.aifa_smart.const import (
    CONF_TRANSPORT,
    DOMAIN,
    LAN_AUTO_SUPPRESS_SECONDS,
    LAN_CONNECT_TIMEOUT,
    TRANSPORT_AUTO,
    TRANSPORT_CLOUD,
    TRANSPORT_LAN,
)
from custom_components.aifa_smart.coordinator import (
    AifaAccountData,
    AifaSmartCoordinator,
)
from custom_components.aifa_smart.lan import LanSendResult, LanTransportManager

_COMPONENT_DIR = Path(__file__).parent.parent / "custom_components" / "aifa_smart"


class _FakeLan:
    def __init__(self, results: list[LanSendResult], *, connected: bool = False) -> None:
        self.results = list(results)
        self.connected = connected
        self.sent: list[tuple[str, str, float]] = []
        self.synced: list[dict] = []
        self.shutdown_calls = 0

    async def async_send_packet(self, device_id, packet_hex, *, connect_timeout):
        self.sent.append((device_id, packet_hex, connect_timeout))
        return self.results.pop(0)

    def is_connected(self, device_id: str) -> bool:
        return self.connected

    def sync_specs(self, devices) -> None:
        self.synced.append(devices)

    async def shutdown(self) -> None:
        self.shutdown_calls += 1


def _make_coordinator(
    transport: str,
    *,
    lan_results: list[LanSendResult] | None = None,
    cloud_ok: bool = True,
    with_lan: bool = True,
):
    coordinator = AifaSmartCoordinator.__new__(AifaSmartCoordinator)
    coordinator.data = AifaAccountData(  # type: ignore[attr-defined]
        devices={}, macros=[], fetched_at=None,  # type: ignore[arg-type]
    )
    cloud_sent: list[tuple[str, str]] = []

    async def fake_cloud_send(device_id, packet_hex):
        cloud_sent.append((device_id, packet_hex))
        return cloud_ok

    coordinator._classic_listener = types.SimpleNamespace(  # type: ignore[attr-defined]
        async_send_packet=fake_cloud_send,
        sync_specs=lambda devices: None,
        shutdown=lambda: asyncio.sleep(0),
    )
    coordinator._transport = transport  # type: ignore[attr-defined]
    coordinator._lan_failed_at = {}  # type: ignore[attr-defined]
    coordinator._transport_counters = {  # type: ignore[attr-defined]
        "sent_lan": 0,
        "sent_cloud": 0,
        "auto_fallbacks": 0,
        "lan_write_failures": 0,
        "last_transport": None,
    }
    lan = _FakeLan(lan_results or [])
    coordinator._lan_manager = lan if with_lan else None  # type: ignore[attr-defined]
    coordinator._ac_follow_up_probe_tasks = {}  # type: ignore[attr-defined]
    return coordinator, lan, cloud_sent


def _command(device_id: str = "d1", packet: str = "ffa016f0") -> AifaFunctionCommand:
    return AifaFunctionCommand(id="1", device_id=device_id, sub_device_id="s1", command=packet)


def _dispatch(coordinator, *commands: AifaFunctionCommand) -> list[dict]:
    return asyncio.run(coordinator._async_dispatch_raw_commands(list(commands)))  # noqa: SLF001


class LanOnlyDispatchTests(unittest.TestCase):
    """LAN mode never touches the cloud socket."""

    def test_sent_over_lan(self) -> None:
        coordinator, lan, cloud_sent = _make_coordinator(
            TRANSPORT_LAN, lan_results=[LanSendResult.SENT]
        )
        responses = _dispatch(coordinator, _command())
        self.assertEqual(responses[0]["transport"], "lan")
        self.assertEqual(responses[0]["subDeviceId"], "s1")
        self.assertEqual(lan.sent, [("d1", "ffa016f0", LAN_CONNECT_TIMEOUT)])
        self.assertEqual(cloud_sent, [])
        self.assertEqual(coordinator._transport_counters["sent_lan"], 1)  # noqa: SLF001

    def test_not_connected_raises_without_cloud(self) -> None:
        coordinator, _lan, cloud_sent = _make_coordinator(
            TRANSPORT_LAN, lan_results=[LanSendResult.NOT_CONNECTED]
        )
        with self.assertRaises(HomeAssistantError) as ctx:
            _dispatch(coordinator, _command())
        self.assertEqual(ctx.exception.translation_key, "lan_unavailable")
        self.assertEqual(ctx.exception.translation_domain, DOMAIN)
        self.assertEqual(cloud_sent, [])

    def test_write_failed_raises_without_cloud(self) -> None:
        coordinator, _lan, cloud_sent = _make_coordinator(
            TRANSPORT_LAN, lan_results=[LanSendResult.WRITE_FAILED]
        )
        with self.assertRaises(HomeAssistantError) as ctx:
            _dispatch(coordinator, _command())
        self.assertEqual(ctx.exception.translation_key, "lan_write_failed")
        self.assertEqual(cloud_sent, [])
        self.assertEqual(coordinator._transport_counters["lan_write_failures"], 1)  # noqa: SLF001

    def test_missing_manager_raises_without_cloud(self) -> None:
        coordinator, _lan, cloud_sent = _make_coordinator(TRANSPORT_LAN, with_lan=False)
        with self.assertRaises(HomeAssistantError) as ctx:
            _dispatch(coordinator, _command())
        self.assertEqual(ctx.exception.translation_key, "lan_unavailable")
        self.assertEqual(cloud_sent, [])

    def test_batch_stops_at_first_failure(self) -> None:
        coordinator, lan, cloud_sent = _make_coordinator(
            TRANSPORT_LAN,
            lan_results=[LanSendResult.SENT, LanSendResult.NOT_CONNECTED],
        )
        with self.assertRaises(HomeAssistantError):
            _dispatch(coordinator, _command(packet="aa"), _command(packet="bb"), _command(packet="cc"))
        self.assertEqual([packet for _, packet, _ in lan.sent], ["aa", "bb"])
        self.assertEqual(cloud_sent, [])


class AutoDispatchTests(unittest.TestCase):
    """Auto mode: LAN first, cloud only when nothing was written locally."""

    def test_sent_over_lan(self) -> None:
        coordinator, _lan, cloud_sent = _make_coordinator(
            TRANSPORT_AUTO, lan_results=[LanSendResult.SENT]
        )
        self.assertEqual(_dispatch(coordinator, _command())[0]["transport"], "lan")
        self.assertEqual(cloud_sent, [])

    def test_not_connected_falls_back_to_cloud(self) -> None:
        coordinator, lan, cloud_sent = _make_coordinator(
            TRANSPORT_AUTO, lan_results=[LanSendResult.NOT_CONNECTED]
        )
        responses = _dispatch(coordinator, _command())
        self.assertEqual(responses[0]["transport"], "classic_tls")
        self.assertEqual(len(lan.sent), 1)
        self.assertEqual(cloud_sent, [("d1", "ffa016f0")])
        counters = coordinator._transport_counters  # noqa: SLF001
        self.assertEqual(counters["auto_fallbacks"], 1)
        self.assertEqual(counters["sent_cloud"], 1)
        self.assertEqual(counters["last_transport"], "classic_tls")

    def test_write_failed_does_not_fall_back(self) -> None:
        coordinator, _lan, cloud_sent = _make_coordinator(
            TRANSPORT_AUTO, lan_results=[LanSendResult.WRITE_FAILED]
        )
        with self.assertRaises(HomeAssistantError) as ctx:
            _dispatch(coordinator, _command())
        self.assertEqual(ctx.exception.translation_key, "lan_write_failed")
        self.assertEqual(cloud_sent, [])

    def test_both_failing_raises_no_transport(self) -> None:
        coordinator, _lan, cloud_sent = _make_coordinator(
            TRANSPORT_AUTO, lan_results=[LanSendResult.NOT_CONNECTED], cloud_ok=False
        )
        with self.assertRaises(HomeAssistantError) as ctx:
            _dispatch(coordinator, _command())
        self.assertEqual(ctx.exception.translation_key, "no_transport")
        self.assertEqual(len(cloud_sent), 1)

    def test_lan_is_skipped_for_60_seconds_after_a_failure(self) -> None:
        coordinator, lan, cloud_sent = _make_coordinator(
            TRANSPORT_AUTO,
            lan_results=[LanSendResult.NOT_CONNECTED, LanSendResult.SENT],
        )
        clock = [1000.0]
        with patch.object(coordinator_module.time, "monotonic", lambda: clock[0]):
            _dispatch(coordinator, _command())
            clock[0] += LAN_AUTO_SUPPRESS_SECONDS - 1
            self.assertEqual(_dispatch(coordinator, _command())[0]["transport"], "classic_tls")
            self.assertEqual(len(lan.sent), 1)
            self.assertEqual(len(cloud_sent), 2)
            clock[0] += 1
            self.assertEqual(_dispatch(coordinator, _command())[0]["transport"], "lan")
        self.assertEqual(len(lan.sent), 2)
        self.assertEqual(len(cloud_sent), 2)
        self.assertNotIn("d1", coordinator._lan_failed_at)  # noqa: SLF001

    def test_suppression_is_per_hub(self) -> None:
        coordinator, lan, cloud_sent = _make_coordinator(
            TRANSPORT_AUTO,
            lan_results=[LanSendResult.NOT_CONNECTED, LanSendResult.SENT],
        )
        _dispatch(coordinator, _command("d1"))
        self.assertEqual(_dispatch(coordinator, _command("d2"))[0]["transport"], "lan")
        self.assertEqual([device for device, _, _ in lan.sent], ["d1", "d2"])

    def test_live_link_is_used_inside_the_suppression_window(self) -> None:
        coordinator, lan, cloud_sent = _make_coordinator(
            TRANSPORT_AUTO,
            lan_results=[LanSendResult.NOT_CONNECTED, LanSendResult.SENT],
        )
        _dispatch(coordinator, _command())
        lan.connected = True
        self.assertEqual(_dispatch(coordinator, _command())[0]["transport"], "lan")
        self.assertEqual(len(cloud_sent), 1)


class CloudDispatchUnchangedTests(unittest.TestCase):
    """Cloud mode ignores a LAN manager entirely."""

    def test_cloud_mode_never_uses_lan(self) -> None:
        coordinator, lan, cloud_sent = _make_coordinator(TRANSPORT_CLOUD)
        self.assertEqual(_dispatch(coordinator, _command())[0]["transport"], "classic_tls")
        self.assertEqual(lan.sent, [])
        self.assertEqual(cloud_sent, [("d1", "ffa016f0")])

    def test_bare_coordinator_defaults_to_cloud(self) -> None:
        coordinator = AifaSmartCoordinator.__new__(AifaSmartCoordinator)
        self.assertEqual(coordinator.transport, TRANSPORT_CLOUD)
        self.assertIsNone(coordinator.lan_manager)


class LanManagerLifecycleTests(unittest.TestCase):
    def test_sync_and_shutdown_reach_the_lan_manager(self) -> None:
        coordinator, lan, _ = _make_coordinator(TRANSPORT_LAN)
        coordinator.async_sync_background_tasks()
        self.assertEqual(lan.synced, [{}])
        asyncio.run(coordinator.async_shutdown())
        self.assertEqual(lan.shutdown_calls, 1)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("options", "expected_transport", "has_lan"),
    [
        ({}, TRANSPORT_CLOUD, False),
        ({CONF_TRANSPORT: TRANSPORT_LAN}, TRANSPORT_LAN, True),
        ({CONF_TRANSPORT: TRANSPORT_AUTO}, TRANSPORT_AUTO, True),
    ],
)
async def test_coordinator_creates_lan_manager_only_when_needed(
    hass: HomeAssistant, options, expected_transport, has_lan
) -> None:
    entry = MockConfigEntry(domain=DOMAIN, data={}, options=options)
    entry.add_to_hass(hass)
    coordinator = AifaSmartCoordinator(hass, entry, client=object())  # type: ignore[arg-type]
    try:
        assert coordinator.transport == expected_transport
        assert isinstance(coordinator.lan_manager, LanTransportManager) is has_lan
    finally:
        await coordinator.async_shutdown()


def test_exception_messages_are_translated() -> None:
    keys = {"lan_unavailable", "lan_write_failed", "no_transport"}
    for path in (
        _COMPONENT_DIR / "strings.json",
        _COMPONENT_DIR / "translations" / "en.json",
        _COMPONENT_DIR / "translations" / "zh-Hant.json",
    ):
        exceptions = json.loads(path.read_text())["exceptions"]
        assert keys <= set(exceptions), path
        for key in keys:
            assert "{device_id}" in exceptions[key]["message"], (path, key)


if __name__ == "__main__":
    unittest.main()
