"""Availability of command entities per transport (Q1) and of readings (Q2)."""
from __future__ import annotations

import asyncio
import types
import unittest
from unittest.mock import patch

import pytest

from custom_components.aifa_smart import lan as lan_module
from custom_components.aifa_smart.binary_sensor import AifaOnlineBinarySensor
from custom_components.aifa_smart.button import AifaAcKeyButton, AifaClassicTemperatureKeyButton
from custom_components.aifa_smart.classic_listener import build_hub_specs
from custom_components.aifa_smart.climate import AifaAirConditionerClimate
from custom_components.aifa_smart.const import (
    LAN_UNAVAILABLE_GRACE_SECONDS,
    TRANSPORT_AUTO,
    TRANSPORT_CLOUD,
    TRANSPORT_LAN,
)
from custom_components.aifa_smart.coordinator import AifaSmartCoordinator
from custom_components.aifa_smart.entity import AifaSubDeviceEntity
from custom_components.aifa_smart.lan import LanHubLink, LanTransportManager
from custom_components.aifa_smart.sensor import (
    AifaAcFanSpeedSensor,
    AifaHumiditySensor,
    AifaSubDeviceCodeSensor,
    AifaTemperatureSensor,
)
from tests.test_lan_fake_hub import (
    _LOCALHOST,
    FakeHub,
    _device,
    _fake_hass,
    _wait_until,
)


class _Lan:
    def __init__(self, available: bool) -> None:
        self.available = available

    def is_available(self, device_id: str) -> bool:
        return self.available


def _coordinator(transport: str, *, lan_available: bool | None, cloud_ok: bool):
    coordinator = AifaSmartCoordinator.__new__(AifaSmartCoordinator)
    coordinator._code_trials = {}  # type: ignore[attr-defined]
    coordinator._code_matches = {}  # type: ignore[attr-defined]
    coordinator._code_match_tasks = set()  # type: ignore[attr-defined]
    coordinator._transport = transport  # type: ignore[attr-defined]
    coordinator._lan_manager = (  # type: ignore[attr-defined]
        None if lan_available is None else _Lan(lan_available)
    )
    coordinator.last_update_success = cloud_ok
    return coordinator


class CoordinatorTruthTableTests(unittest.TestCase):
    def test_truth_table(self) -> None:
        # (transport, LAN link usable, cloud poll OK) -> control available
        table = [
            (TRANSPORT_CLOUD, None, True, True),
            (TRANSPORT_CLOUD, None, False, False),
            (TRANSPORT_LAN, True, True, True),
            (TRANSPORT_LAN, True, False, True),
            (TRANSPORT_LAN, False, True, False),
            (TRANSPORT_LAN, False, False, False),
            (TRANSPORT_LAN, None, True, False),
            (TRANSPORT_AUTO, True, True, True),
            (TRANSPORT_AUTO, True, False, True),
            (TRANSPORT_AUTO, False, True, True),
            (TRANSPORT_AUTO, False, False, False),
        ]
        for transport, lan_ok, cloud_ok, expected in table:
            with self.subTest(transport=transport, lan=lan_ok, cloud=cloud_ok):
                coordinator = _coordinator(transport, lan_available=lan_ok, cloud_ok=cloud_ok)
                self.assertIs(coordinator.is_control_available("7001"), expected)

    def test_cloud_mode_ignores_a_lan_manager(self) -> None:
        coordinator = _coordinator(TRANSPORT_CLOUD, lan_available=True, cloud_ok=False)
        self.assertFalse(coordinator.is_control_available("7001"))


class _ConnectedWriter:
    def is_closing(self) -> bool:
        return False


class LinkGraceTests(unittest.TestCase):
    def _manager_with_link(self) -> tuple[LanTransportManager, LanHubLink]:
        manager = LanTransportManager(
            hass=_fake_hass(),  # type: ignore[arg-type]
            on_observed_status=lambda *args: False,
            on_data_changed=lambda: None,
        )
        spec = build_hub_specs({"7001": _device()}, require_online=False)["7001"]
        link = LanHubLink(spec=spec, created_at=1000.0)
        manager.links["7001"] = link
        return manager, link

    def _at(self, manager: LanTransportManager, now: float) -> bool:
        with patch.object(lan_module.time, "monotonic", lambda: now):
            return manager.is_available("7001")

    def test_connected_link_is_available(self) -> None:
        manager, link = self._manager_with_link()
        link.writer = _ConnectedWriter()  # type: ignore[assignment]
        self.assertTrue(self._at(manager, 1_000_000.0))

    def test_grace_after_disconnect(self) -> None:
        manager, link = self._manager_with_link()
        link.disconnected_at = 2000.0
        self.assertTrue(self._at(manager, 2000.0 + LAN_UNAVAILABLE_GRACE_SECONDS - 0.5))
        self.assertFalse(self._at(manager, 2000.0 + LAN_UNAVAILABLE_GRACE_SECONDS))

    def test_never_connected_link_counts_from_creation(self) -> None:
        manager, _link = self._manager_with_link()
        self.assertTrue(self._at(manager, 1000.0 + LAN_UNAVAILABLE_GRACE_SECONDS - 0.5))
        self.assertFalse(self._at(manager, 1000.0 + LAN_UNAVAILABLE_GRACE_SECONDS))

    def test_unknown_hub_is_unavailable(self) -> None:
        manager, _link = self._manager_with_link()
        with patch.object(lan_module.time, "monotonic", lambda: 1000.0):
            self.assertFalse(manager.is_available("other"))


@pytest.mark.usefixtures("socket_enabled")
class LinkStateNotificationTests(unittest.IsolatedAsyncioTestCase):
    """Entities must hear about connect, drop and the end of the grace period."""

    async def asyncSetUp(self) -> None:
        self.hub = await FakeHub().start()
        self.notifications = 0

        async def broadcast() -> list[str]:
            return [_LOCALHOST]

        def notify() -> None:
            self.notifications += 1

        self.manager = LanTransportManager(
            hass=_fake_hass(),  # type: ignore[arg-type]
            on_observed_status=lambda *args: False,
            on_data_changed=lambda: None,
            broadcast_provider=broadcast,
            target_port=self.hub.port,
            search_source_port=0,
            bind_host=_LOCALHOST,
            backoff_schedule=(0.05,),
            connect_attempts=1,
            connect_interval=0.1,
            search_timeout=0.1,
            on_link_state_changed=notify,
            unavailable_grace=0.3,
        )

    async def asyncTearDown(self) -> None:
        await self.manager.shutdown()
        await self.hub.stop()

    async def test_drop_then_grace_expiry_notifies(self) -> None:
        self.manager.sync_specs({"7001": _device()})
        await _wait_until(lambda: self.manager.is_connected("7001"))
        self.assertEqual(self.notifications, 1)
        self.hub.accept_connect = False
        self.hub.drop()
        await _wait_until(lambda: self.notifications == 2)
        self.assertTrue(self.manager.is_available("7001"))
        await _wait_until(lambda: self.notifications == 3, timeout=2.0)
        self.assertFalse(self.manager.is_available("7001"))

    async def test_shutdown_cancels_pending_grace_timer(self) -> None:
        self.hub.accept_connect = False
        self.manager.sync_specs({"7001": _device()})
        await self.manager.shutdown()
        await asyncio.sleep(0.5)
        self.assertEqual(self.notifications, 0)


class EntityWiringTests(unittest.TestCase):
    def test_only_command_entities_follow_the_transport(self) -> None:
        for cls in (
            AifaAirConditionerClimate,
            AifaAcKeyButton,
            AifaClassicTemperatureKeyButton,
            AifaAcFanSpeedSensor,
        ):
            self.assertTrue(cls._control_entity, cls)  # noqa: SLF001
        self.assertFalse(AifaSubDeviceCodeSensor._control_entity)  # noqa: SLF001

    def _available(self, *, control: bool, lan_ok: bool, cloud_ok: bool) -> bool:
        coordinator = _coordinator(TRANSPORT_LAN, lan_available=lan_ok, cloud_ok=cloud_ok)
        entity = types.SimpleNamespace(
            _control_entity=control,
            _device_id="7001",
            coordinator=coordinator,
            device=object(),
            sub_device=object(),
        )
        return AifaSubDeviceEntity.available.fget(entity)  # type: ignore[attr-defined]

    def test_lan_mode_control_entity_survives_cloud_outage(self) -> None:
        self.assertTrue(self._available(control=True, lan_ok=True, cloud_ok=False))
        self.assertFalse(self._available(control=True, lan_ok=False, cloud_ok=True))

    def test_reading_entities_still_follow_the_cloud(self) -> None:
        self.assertFalse(self._available(control=False, lan_ok=True, cloud_ok=False))
        self.assertTrue(self._available(control=False, lan_ok=False, cloud_ok=True))

    def test_temperature_and_humidity_sensors_stay_cloud_bound(self) -> None:
        coordinator = _coordinator(TRANSPORT_LAN, lan_available=True, cloud_ok=False)
        for cls in (AifaTemperatureSensor, AifaHumiditySensor, AifaOnlineBinarySensor):
            entity = types.SimpleNamespace(coordinator=coordinator, device=object())
            with self.subTest(cls=cls.__name__):
                self.assertFalse(cls.available.fget(entity))  # type: ignore[attr-defined]


if __name__ == "__main__":
    unittest.main()
