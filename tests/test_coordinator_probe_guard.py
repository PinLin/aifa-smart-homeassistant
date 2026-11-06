"""Post-send probe behaviour per transport: the cloud must not block LAN sends."""
from __future__ import annotations

import asyncio
import types
import unittest

from custom_components.aifa_smart.api import (
    AifaDevice,
    AifaSmartConnectionError,
    AifaSmartError,
    AifaSubDevice,
)
from custom_components.aifa_smart.const import (
    TRANSPORT_AUTO,
    TRANSPORT_CLOUD,
    TRANSPORT_LAN,
)
from custom_components.aifa_smart.coordinator import (
    AifaAccountData,
    AifaSmartCoordinator,
)


def _device(*, online: bool = True) -> AifaDevice:
    return AifaDevice(
        id="7001",
        name="Hub",
        mac="A0B1C2D3E4F5",
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
    )


class _Listener:
    def __init__(self, *, listening: bool = False, writer: bool = False) -> None:
        self.listening = listening
        self.writer = writer

    def has_active_listener(self, device_id: str) -> bool:
        return self.listening

    def has_active_writer(self, device_id: str) -> bool:
        return self.writer


class _Lan:
    def __init__(self, connected: bool) -> None:
        self.connected = connected
        self.queries: list[str] = []

    def is_connected(self, device_id: str) -> bool:
        return self.connected

    async def async_request_status(self, device_id: str) -> bool:
        self.queries.append(device_id)
        return True


class _Client:
    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.calls = 0

    async def async_query_classic_ac_statuses(self, mac, sub_types=None, timeout=6.0):
        self.calls += 1
        if self.error is not None:
            raise self.error
        return {}


def _coordinator(
    transport: str,
    *,
    listener: _Listener,
    client: _Client,
    lan: _Lan | None = None,
    online: bool = True,
) -> AifaSmartCoordinator:
    coordinator = AifaSmartCoordinator.__new__(AifaSmartCoordinator)
    coordinator.data = AifaAccountData(  # type: ignore[attr-defined]
        devices={"7001": _device(online=online)}, macros=[], fetched_at=None,  # type: ignore[arg-type]
    )
    coordinator.client = client  # type: ignore[assignment]
    coordinator._classic_listener = listener  # type: ignore[attr-defined]
    coordinator._transport = transport  # type: ignore[attr-defined]
    coordinator._lan_manager = lan  # type: ignore[attr-defined]
    coordinator._ac_runtime_states = {}  # type: ignore[attr-defined]
    coordinator.async_update_listeners = lambda: None  # type: ignore[method-assign]
    return coordinator


def _probe(coordinator: AifaSmartCoordinator) -> bool:
    return asyncio.run(
        coordinator._async_probe_ac_observed_state(  # noqa: SLF001
            "7001", "ac-1", expected_mode="cool"
        )
    )


class ProbeGuardTests(unittest.TestCase):
    def test_lan_mode_skips_cloud_query_without_cloud_writer(self) -> None:
        client = _Client()
        coordinator = _coordinator(
            TRANSPORT_LAN, listener=_Listener(), client=client, lan=_Lan(False)
        )
        self.assertFalse(_probe(coordinator))
        self.assertEqual(client.calls, 0)

    def test_auto_mode_skips_cloud_query_without_cloud_writer(self) -> None:
        client = _Client()
        coordinator = _coordinator(
            TRANSPORT_AUTO, listener=_Listener(), client=client, lan=_Lan(False)
        )
        self.assertFalse(_probe(coordinator))
        self.assertEqual(client.calls, 0)

    def test_lan_mode_swallows_cloud_query_errors(self) -> None:
        for error in (
            AifaSmartConnectionError("token refresh failed"),
            AifaSmartError("bad payload"),
            OSError("unreachable"),
            TimeoutError(),
        ):
            with self.subTest(error=type(error).__name__):
                client = _Client(error)
                coordinator = _coordinator(
                    TRANSPORT_LAN,
                    listener=_Listener(writer=True),
                    client=client,
                    lan=_Lan(False),
                )
                self.assertFalse(_probe(coordinator))
                self.assertEqual(client.calls, 1)

    def test_cloud_mode_still_queries_without_writer_and_propagates(self) -> None:
        client = _Client()
        coordinator = _coordinator(TRANSPORT_CLOUD, listener=_Listener(), client=client)
        self.assertFalse(_probe(coordinator))
        self.assertEqual(client.calls, 1)

        failing = _Client(AifaSmartConnectionError("down"))
        coordinator = _coordinator(TRANSPORT_CLOUD, listener=_Listener(), client=failing)
        with self.assertRaises(AifaSmartConnectionError):
            _probe(coordinator)

    def test_cloud_mode_offline_hub_is_not_probed(self) -> None:
        client = _Client()
        coordinator = _coordinator(
            TRANSPORT_CLOUD, listener=_Listener(), client=client, online=False
        )
        self.assertFalse(_probe(coordinator))
        self.assertEqual(client.calls, 0)

    def test_lan_link_lets_an_offline_hub_be_probed(self) -> None:
        client = _Client()
        lan = _Lan(True)
        coordinator = _coordinator(
            TRANSPORT_LAN,
            listener=_Listener(),
            client=client,
            lan=lan,
            online=False,
        )
        waited: list[str] = []

        async def fake_wait(device_id, sub_device_id, **kwargs):
            waited.append(device_id)
            return True

        coordinator._async_wait_for_classic_socket_update = fake_wait  # type: ignore[method-assign]
        self.assertTrue(_probe(coordinator))
        self.assertEqual(waited, ["7001"])
        self.assertEqual(lan.queries, ["7001"])
        self.assertEqual(client.calls, 0)

    def test_disconnected_lan_link_is_not_queried(self) -> None:
        lan = _Lan(False)
        coordinator = _coordinator(
            TRANSPORT_AUTO, listener=_Listener(), client=_Client(), lan=lan
        )
        _probe(coordinator)
        self.assertEqual(lan.queries, [])


class StatusStreamTests(unittest.TestCase):
    def test_truth_table(self) -> None:
        cases = [
            (False, None, False),
            (True, None, True),
            (False, False, False),
            (False, True, True),
            (True, False, True),
        ]
        for listening, lan_connected, expected in cases:
            with self.subTest(listening=listening, lan=lan_connected):
                coordinator = _coordinator(
                    TRANSPORT_LAN,
                    listener=_Listener(listening=listening),
                    client=_Client(),
                    lan=None if lan_connected is None else _Lan(lan_connected),
                )
                self.assertIs(coordinator._has_status_stream("7001"), expected)  # noqa: SLF001

    def test_wait_returns_on_lan_push_without_cloud_listener(self) -> None:
        coordinator = _coordinator(
            TRANSPORT_LAN, listener=_Listener(), client=_Client(), lan=_Lan(True)
        )
        state = coordinator.get_ac_runtime_state("7001", "ac-1", device_code="136")

        async def run() -> bool:
            async def push() -> None:
                await asyncio.sleep(0.05)
                state.observed_revision += 1
                state.observed_status = types.SimpleNamespace(mode="cool")  # type: ignore[assignment]

            task = asyncio.create_task(push())
            try:
                return await coordinator._async_wait_for_classic_socket_update(  # noqa: SLF001
                    "7001", "ac-1", minimum_observed_revision=0, timeout=0.5
                )
            finally:
                await task

        self.assertTrue(asyncio.run(run()))


if __name__ == "__main__":
    unittest.main()
