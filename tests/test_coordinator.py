"""Focused tests for classic AC probe coordination."""
from __future__ import annotations

import asyncio
import types
import unittest
import unittest.mock

from custom_components.aifa_smart.coordinator import AifaSmartCoordinator
from custom_components.aifa_smart.runtime import AifaAcRuntimeState


class _FakeClassicListener:
    def has_active_listener(self, device_id: str) -> bool:
        return True


class _ProbeHarness:
    """Minimal self object for exercising coordinator wait logic."""

    def __init__(self, state: AifaAcRuntimeState, matches_expectation: bool) -> None:
        self.state = state
        self.matches_expectation = matches_expectation
        self._classic_listener = _FakeClassicListener()

    def get_ac_runtime_state(self, device_id: str, sub_device_id: str):
        return self.state

    def _ac_state_matches_expectation(
        self,
        device_id: str,
        sub_device_id: str,
        *,
        expected_mode: str | None,
        expected_target_temperature: int | None = None,
        expected_fan_mode: str | None = None,
        expected_swing_mode: str | None = None,
    ) -> bool:
        return self.matches_expectation


class CoordinatorProbeTests(unittest.TestCase):
    """Coverage for the listener-vs-query wait gate."""

    def test_listener_revision_requires_expected_state_match(self) -> None:
        """A stale listener update should not short-circuit the fresh query path."""
        state = AifaAcRuntimeState.create("136")
        harness = _ProbeHarness(state, matches_expectation=False)

        async def _mutate() -> None:
            await asyncio.sleep(0.05)
            state.observed_revision = 1
            state.observed_status = types.SimpleNamespace(mode="auto")

        async def _run() -> bool:
            task = asyncio.create_task(_mutate())
            try:
                return await AifaSmartCoordinator._async_wait_for_classic_socket_update(
                    harness,
                    "8014",
                    "31867",
                    minimum_observed_revision=0,
                    expected_mode="off",
                    timeout=0.2,
                )
            finally:
                await task

        self.assertFalse(asyncio.run(_run()))

    def test_listener_revision_can_short_circuit_when_expected_state_matches(self) -> None:
        """A matching listener update should still satisfy the fast path."""
        state = AifaAcRuntimeState.create("136")
        harness = _ProbeHarness(state, matches_expectation=True)

        async def _mutate() -> None:
            await asyncio.sleep(0.05)
            state.observed_revision = 1
            state.observed_status = types.SimpleNamespace(mode="off")

        async def _run() -> bool:
            task = asyncio.create_task(_mutate())
            try:
                return await AifaSmartCoordinator._async_wait_for_classic_socket_update(
                    harness,
                    "8014",
                    "31867",
                    minimum_observed_revision=0,
                    expected_mode="off",
                    timeout=0.2,
                )
            finally:
                await task

        self.assertTrue(asyncio.run(_run()))


class CoordinatorRuntimeStateIsolationTests(unittest.TestCase):
    """Multi-AC: runtime state must not cross-contaminate across sub-devices."""

    def _make_minimal_coordinator(self) -> AifaSmartCoordinator:
        coordinator = AifaSmartCoordinator.__new__(AifaSmartCoordinator)
        coordinator._ac_runtime_states = {}  # type: ignore[attr-defined]
        return coordinator

    def test_distinct_sub_devices_get_distinct_runtime_states(self) -> None:
        """Two ACs on the same hub must each get their own runtime state object."""
        coordinator = self._make_minimal_coordinator()
        state_a = coordinator.get_ac_runtime_state("hub1", "ac_a", device_code="136")
        state_b = coordinator.get_ac_runtime_state("hub1", "ac_b", device_code="136")
        self.assertIsNot(state_a, state_b)

    def test_same_sub_device_id_across_hubs_stays_isolated(self) -> None:
        """Sub-device IDs collide across hubs; the (device_id, sub_device_id) key keeps them apart."""
        coordinator = self._make_minimal_coordinator()
        state_h1 = coordinator.get_ac_runtime_state("hub1", "ac_x", device_code="136")
        state_h2 = coordinator.get_ac_runtime_state("hub2", "ac_x", device_code="136")
        self.assertIsNot(state_h1, state_h2)

    def test_repeated_lookup_returns_same_state(self) -> None:
        """Repeated lookups for the same key must return the same instance (singleton per key)."""
        coordinator = self._make_minimal_coordinator()
        state1 = coordinator.get_ac_runtime_state("hub1", "ac_a", device_code="136")
        state1.requested_target_temperature = 25
        state2 = coordinator.get_ac_runtime_state("hub1", "ac_a", device_code="136")
        self.assertIs(state1, state2)
        self.assertEqual(state2.requested_target_temperature, 25)


class CoordinatorMacroExecutionTests(unittest.TestCase):
    """async_execute_macro must run all commands in order with per-step delays."""

    def _make_coordinator_with_macro(self, macro):
        from custom_components.aifa_smart.coordinator import AifaAccountData

        coordinator = AifaSmartCoordinator.__new__(AifaSmartCoordinator)
        coordinator.data = AifaAccountData(  # type: ignore[attr-defined]
            devices={},
            macros=[macro] if macro is not None else [],
            fetched_at=None,  # type: ignore[arg-type]
        )

        sent: list[tuple[str, str, str | None]] = []

        async def fake_send_packet(device_id, packet_hex):
            # Reconstruct what _async_dispatch_raw_commands would observe;
            # sub_device_id isn't visible here so we capture None and rely on
            # responses for the full triple.
            sent.append((device_id, packet_hex, None))
            return True

        coordinator._classic_listener = types.SimpleNamespace(  # type: ignore[attr-defined]
            async_send_packet=fake_send_packet,
        )
        coordinator._sent = sent  # type: ignore[attr-defined]

        async def fake_refresh():
            return None

        coordinator.async_request_refresh = fake_refresh  # type: ignore[attr-defined]
        return coordinator

    def test_get_macro_returns_match_or_none(self) -> None:
        from custom_components.aifa_smart.api import AifaMacro, AifaMacroCommand

        macro = AifaMacro(
            id="42",
            name="Movie Time",
            commands=[AifaMacroCommand(id="1", macro_id="42", device_id="d1", sub_device_id=None, command="ffaa")],
        )
        coordinator = self._make_coordinator_with_macro(macro)
        self.assertIs(coordinator.get_macro("42"), macro)
        self.assertIsNone(coordinator.get_macro("99"))

    def test_execute_macro_sends_commands_in_order_no(self) -> None:
        """Commands must run sorted by order_no, not list order."""
        from custom_components.aifa_smart.api import AifaMacro, AifaMacroCommand

        macro = AifaMacro(
            id="42",
            name="Movie Time",
            commands=[
                AifaMacroCommand(id="c2", macro_id="42", device_id="d1", sub_device_id="s1", command="bb", order_no=2),
                AifaMacroCommand(id="c0", macro_id="42", device_id="d2", sub_device_id=None, command="aa", order_no=0),
                AifaMacroCommand(id="c1", macro_id="42", device_id="d1", sub_device_id="s1", command="cc", order_no=1),
            ],
        )
        coordinator = self._make_coordinator_with_macro(macro)
        asyncio.run(coordinator.async_execute_macro("42"))
        # sub_device_id isn't visible to the TLS send_packet boundary; the
        # dispatcher attaches it to the response. The ordering assertion
        # focuses on the order_no contract: order_no=0 fires first, then 1,
        # then 2.
        self.assertEqual(
            coordinator._sent,
            [("d2", "aa", None), ("d1", "cc", None), ("d1", "bb", None)],
        )

    def test_execute_macro_honors_per_step_delay_skipping_final(self) -> None:
        """Sleep should fire between steps but not after the last command."""
        from custom_components.aifa_smart.api import AifaMacro, AifaMacroCommand

        macro = AifaMacro(
            id="7",
            name="Quick",
            commands=[
                AifaMacroCommand(id="a", macro_id="7", device_id="d1", sub_device_id=None, command="aa", delay=200, order_no=0),
                AifaMacroCommand(id="b", macro_id="7", device_id="d1", sub_device_id=None, command="bb", delay=300, order_no=1),
            ],
        )
        coordinator = self._make_coordinator_with_macro(macro)
        slept: list[float] = []

        original_sleep = asyncio.sleep

        async def fake_sleep(seconds):
            slept.append(seconds)
            await original_sleep(0)

        with unittest.mock.patch.object(asyncio, "sleep", fake_sleep):
            asyncio.run(coordinator.async_execute_macro("7"))
        # delay=200 between step 0 and 1; delay=300 after final step is skipped
        self.assertEqual(slept, [0.2])

    def test_execute_unknown_macro_raises(self) -> None:
        from custom_components.aifa_smart.api import AifaSmartError

        coordinator = self._make_coordinator_with_macro(None)
        with self.assertRaises(AifaSmartError):
            asyncio.run(coordinator.async_execute_macro("does-not-exist"))

    def test_execute_macro_with_empty_commands_returns_no_responses(self) -> None:
        from custom_components.aifa_smart.api import AifaMacro

        coordinator = self._make_coordinator_with_macro(
            AifaMacro(id="empty", name="Empty", commands=[])
        )
        result = asyncio.run(coordinator.async_execute_macro("empty"))
        self.assertEqual(result, [])
        self.assertEqual(coordinator._sent, [])


class CoordinatorTlsDispatchTests(unittest.TestCase):
    """_async_dispatch_raw_commands sends through the long-lived classic TLS socket.

    The integration mirrors AIFA app's own contract: AC commands ride the
    long-lived TLS socket to aifaremote.com:8751 only. AIFA app does not
    fall back to a REST path for AC commands either — when the socket is
    not available, the dispatcher raises so the caller can surface the
    failure to the user.
    """

    def _make_coordinator(self, *, classic_send_results: dict[str, bool] | bool = True):
        """Build a coordinator with the classic listener stubbed.

        `classic_send_results` controls what async_send_packet returns: a single
        bool applies to every device; a dict keys per device_id.
        """
        from custom_components.aifa_smart.coordinator import AifaAccountData

        coordinator = AifaSmartCoordinator.__new__(AifaSmartCoordinator)
        coordinator.data = AifaAccountData(  # type: ignore[attr-defined]
            devices={},
            macros=[],
            fetched_at=None,  # type: ignore[arg-type]
        )

        tls_sent: list[tuple[str, str]] = []

        async def fake_send_packet(device_id, packet_hex):
            tls_sent.append((device_id, packet_hex))
            if isinstance(classic_send_results, dict):
                return classic_send_results.get(device_id, False)
            return classic_send_results

        coordinator._classic_listener = types.SimpleNamespace(  # type: ignore[attr-defined]
            async_send_packet=fake_send_packet,
        )
        coordinator._tls_sent = tls_sent  # type: ignore[attr-defined]
        return coordinator

    def test_dispatch_uses_tls_when_classic_socket_is_live(self) -> None:
        """A device with an active classic socket sends via TLS."""
        from custom_components.aifa_smart.api import AifaFunctionCommand

        coordinator = self._make_coordinator(classic_send_results=True)
        commands = [
            AifaFunctionCommand(id="1", device_id="d1", sub_device_id="s1", command="ffa0a1f0"),
        ]
        responses = asyncio.run(coordinator._async_dispatch_raw_commands(commands))

        self.assertEqual(coordinator._tls_sent, [("d1", "ffa0a1f0")])
        self.assertEqual(len(responses), 1)
        self.assertEqual(responses[0]["transport"], "classic_tls")
        self.assertEqual(responses[0]["status"], "success")
        self.assertEqual(responses[0]["subDeviceId"], "s1")

    def test_dispatch_raises_when_no_classic_socket(self) -> None:
        """Without an active TLS writer, dispatch must surface the failure."""
        from custom_components.aifa_smart.api import AifaFunctionCommand, AifaSmartError

        coordinator = self._make_coordinator(classic_send_results=False)
        commands = [
            AifaFunctionCommand(id="1", device_id="d1", sub_device_id=None, command="ffa0a2f0"),
        ]
        with self.assertRaises(AifaSmartError):
            asyncio.run(coordinator._async_dispatch_raw_commands(commands))

        # The send was attempted before raising
        self.assertEqual(coordinator._tls_sent, [("d1", "ffa0a2f0")])

    def test_dispatch_raises_on_first_no_tls_in_a_batch(self) -> None:
        """In a multi-device batch, the first device without TLS aborts the whole batch."""
        from custom_components.aifa_smart.api import AifaFunctionCommand, AifaSmartError

        coordinator = self._make_coordinator(
            classic_send_results={"d1": True, "d2": False}
        )
        commands = [
            AifaFunctionCommand(id="1", device_id="d1", sub_device_id="s1", command="aaaa"),
            AifaFunctionCommand(id="2", device_id="d2", sub_device_id="s2", command="bbbb"),
            AifaFunctionCommand(id="3", device_id="d1", sub_device_id="s1", command="cccc"),
        ]
        with self.assertRaises(AifaSmartError):
            asyncio.run(coordinator._async_dispatch_raw_commands(commands))

        # d1 sent (ok), d2 attempted (failed) — third command never reached
        self.assertEqual(
            coordinator._tls_sent,
            [("d1", "aaaa"), ("d2", "bbbb")],
        )

    def test_dispatch_handles_empty_command_list(self) -> None:
        """No commands → no transport touched, returns empty list."""
        coordinator = self._make_coordinator()
        responses = asyncio.run(coordinator._async_dispatch_raw_commands([]))
        self.assertEqual(responses, [])
        self.assertEqual(coordinator._tls_sent, [])

    def test_dispatch_propagates_oserror_from_tls_write(self) -> None:
        """A TLS write that raises OSError propagates to the caller."""
        from custom_components.aifa_smart.api import AifaFunctionCommand

        coordinator = self._make_coordinator()

        async def raising_send_packet(device_id, packet_hex):
            raise OSError("simulated socket dropped")

        coordinator._classic_listener.async_send_packet = raising_send_packet  # type: ignore[attr-defined]

        commands = [
            AifaFunctionCommand(id="1", device_id="d1", sub_device_id=None, command="ffa0a1f0"),
        ]
        with self.assertRaises(OSError):
            asyncio.run(coordinator._async_dispatch_raw_commands(commands))


class CoordinatorHelperFlagMutexTests(unittest.TestCase):
    """Toggling one helper flag must clear the others, regardless of prior state."""

    def _make_coordinator_for_apply(self):
        from custom_components.aifa_smart.api import AifaSubDevice

        coordinator = AifaSmartCoordinator.__new__(AifaSmartCoordinator)
        coordinator._ac_runtime_states = {}  # type: ignore[attr-defined]
        coordinator._ac_apply_locks = {}  # type: ignore[attr-defined]
        coordinator._ac_last_send_at = {}  # type: ignore[attr-defined]
        sent: list[str] = []

        async def fake_send_function_commands(commands):
            sent.extend(c.command for c in commands)
            return [{"ok": True}]

        async def fake_refresh():
            return None

        async def fake_probe(*args, **kwargs):
            return None

        coordinator.async_send_function_commands = fake_send_function_commands  # type: ignore[attr-defined]
        coordinator.async_request_refresh = fake_refresh  # type: ignore[attr-defined]
        coordinator.async_update_listeners = lambda: None  # type: ignore[attr-defined]
        coordinator._async_probe_ac_observed_state = fake_probe  # type: ignore[attr-defined]
        coordinator._sent = sent  # type: ignore[attr-defined]

        sub_device = AifaSubDevice(
            id="s1", device_id="d1", name="冷氣", type="0", sub_type="0",
            device_code="136",
        )
        coordinator.get_sub_device = lambda d, s: sub_device  # type: ignore[attr-defined]
        coordinator._schedule_ac_follow_up_probe = lambda *a, **k: None  # type: ignore[attr-defined]
        return coordinator

    def test_turn_on_sleep_clears_existing_power_saving(self) -> None:
        """Regression: with power_saving=True in state, turn ON sleep must succeed."""
        coordinator = self._make_coordinator_for_apply()
        state = coordinator.get_ac_runtime_state("d1", "s1", device_code="136")
        state.requested_hvac_mode = "cool"
        state.requested_target_temperature = 26
        state.requested_fan_mode = "high"
        state.requested_swing_mode = "fixed_5"
        state.requested_power_saving = True
        state.last_active_hvac_mode = "cool"

        asyncio.run(
            coordinator.async_apply_ac_state(
                "d1", "s1", hvac_mode="cool", device_code="136", sleep=True,
            )
        )

        self.assertTrue(state.requested_sleep)
        self.assertFalse(state.requested_power_saving)
        self.assertFalse(state.requested_turbo)

    def test_turn_on_turbo_clears_existing_sleep(self) -> None:
        coordinator = self._make_coordinator_for_apply()
        state = coordinator.get_ac_runtime_state("d1", "s1", device_code="136")
        state.requested_hvac_mode = "cool"
        state.requested_target_temperature = 24
        state.requested_fan_mode = "auto"
        state.requested_swing_mode = "off"
        state.requested_sleep = True
        state.last_active_hvac_mode = "cool"

        asyncio.run(
            coordinator.async_apply_ac_state(
                "d1", "s1", hvac_mode="cool", device_code="136", turbo=True,
            )
        )

        self.assertTrue(state.requested_turbo)
        self.assertFalse(state.requested_sleep)
        self.assertFalse(state.requested_power_saving)

    def test_turn_on_power_saving_clears_existing_turbo(self) -> None:
        coordinator = self._make_coordinator_for_apply()
        state = coordinator.get_ac_runtime_state("d1", "s1", device_code="136")
        state.requested_hvac_mode = "cool"
        state.requested_target_temperature = 24
        state.requested_fan_mode = "auto"
        state.requested_swing_mode = "off"
        state.requested_turbo = True
        state.last_active_hvac_mode = "cool"

        asyncio.run(
            coordinator.async_apply_ac_state(
                "d1", "s1", hvac_mode="cool", device_code="136", power_saving=True,
            )
        )

        self.assertTrue(state.requested_power_saving)
        self.assertFalse(state.requested_turbo)
        self.assertFalse(state.requested_sleep)

    def test_turn_off_sleep_keeps_other_helpers(self) -> None:
        """Turning OFF a helper must not clobber the other helpers' state."""
        coordinator = self._make_coordinator_for_apply()
        state = coordinator.get_ac_runtime_state("d1", "s1", device_code="136")
        state.requested_hvac_mode = "cool"
        state.requested_target_temperature = 24
        state.requested_fan_mode = "auto"
        state.requested_swing_mode = "off"
        state.requested_power_saving = True
        state.last_active_hvac_mode = "cool"

        asyncio.run(
            coordinator.async_apply_ac_state(
                "d1", "s1", hvac_mode="cool", device_code="136", sleep=False,
            )
        )

        self.assertFalse(state.requested_sleep)
        self.assertTrue(state.requested_power_saving)
        self.assertFalse(state.requested_turbo)


class ResolveHelperFlagMutexTests(unittest.TestCase):
    """Direct tests for the extracted helper-flag mutex contract."""

    def setUp(self) -> None:
        from custom_components.aifa_smart.runtime import resolve_helper_flag_mutex
        self._resolve = resolve_helper_flag_mutex

    def _call(self, **overrides):
        defaults = dict(
            runtime_power_saving=False, runtime_turbo=False, runtime_sleep=False,
            caller_power_saving=None, caller_turbo=None, caller_sleep=None,
        )
        defaults.update(overrides)
        return self._resolve(**defaults)

    def test_caller_sleep_true_clears_runtime_power_saving(self) -> None:
        ps, turbo, sleep = self._call(runtime_power_saving=True, caller_sleep=True)
        self.assertEqual((ps, turbo, sleep), (False, False, True))

    def test_caller_turbo_true_clears_runtime_sleep(self) -> None:
        ps, turbo, sleep = self._call(runtime_sleep=True, caller_turbo=True)
        self.assertEqual((ps, turbo, sleep), (False, True, False))

    def test_caller_power_saving_true_clears_runtime_turbo(self) -> None:
        ps, turbo, sleep = self._call(runtime_turbo=True, caller_power_saving=True)
        self.assertEqual((ps, turbo, sleep), (True, False, False))

    def test_caller_false_does_not_revive_other_flags(self) -> None:
        ps, turbo, sleep = self._call(runtime_power_saving=True, caller_sleep=False)
        self.assertEqual((ps, turbo, sleep), (True, False, False))

    def test_runtime_only_power_saving_wins_over_other_runtime_flags(self) -> None:
        ps, turbo, sleep = self._call(
            runtime_power_saving=True, runtime_turbo=True, runtime_sleep=True,
        )
        self.assertEqual((ps, turbo, sleep), (True, False, False))

    def test_all_none_and_clear_returns_clear(self) -> None:
        self.assertEqual(self._call(), (False, False, False))


if __name__ == "__main__":
    unittest.main()
