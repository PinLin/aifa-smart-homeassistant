"""Focused tests for the coordinator."""
from __future__ import annotations

import asyncio
import types
import unittest
import unittest.mock
from dataclasses import replace

from custom_components.aifa_smart.api import AifaClassicSensorState, AifaDevice
from custom_components.aifa_smart.ac import AifaAcStatus
from custom_components.aifa_smart.coordinator import AifaSmartCoordinator
from tests.hub_reports import confirming_sender, report_soon


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
        coordinator._hub_report_waiters = {}  # type: ignore[attr-defined]
        coordinator._ac_apply_locks = {}  # type: ignore[attr-defined]

        async def fake_send_packet(device_id, packet_hex):
            # sub_device_id isn't visible here so we capture None and rely on
            # responses for the full triple. Every packet is recorded: a
            # macro step draws no status query.
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


async def _send_all(coordinator, commands) -> list[dict]:
    """Send commands one after another like macro steps."""
    return [await coordinator._async_send_unconfirmed(command) for command in commands]  # noqa: SLF001


class CoordinatorTlsDispatchTests(unittest.TestCase):
    """Unconfirmed sends go through the long-lived classic TLS socket.

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
        coordinator._ac_apply_locks = {}  # type: ignore[attr-defined]
        coordinator._tls_sent = tls_sent  # type: ignore[attr-defined]
        return coordinator

    def test_dispatch_uses_tls_when_classic_socket_is_live(self) -> None:
        """A device with an active classic socket sends via TLS."""
        from custom_components.aifa_smart.api import AifaFunctionCommand

        coordinator = self._make_coordinator(classic_send_results=True)
        commands = [
            AifaFunctionCommand(id="1", device_id="d1", sub_device_id="s1", command="ffa0a1f0"),
        ]
        responses = asyncio.run(_send_all(coordinator, commands))

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
            asyncio.run(_send_all(coordinator, commands))

        # The send was attempted before raising
        self.assertEqual(coordinator._tls_sent, [("d1", "ffa0a2f0")])

    def test_classic_temperature_key_sends_one_press_over_tls(self) -> None:
        from custom_components.aifa_smart.api import AifaSubDevice

        coordinator = self._make_coordinator(classic_send_results=True)
        coordinator._ac_apply_locks = {}  # type: ignore[attr-defined]
        coordinator._ac_last_send_at = {}  # type: ignore[attr-defined]
        coordinator._hub_report_waiters = {}  # type: ignore[attr-defined]
        send_packet = coordinator._classic_listener.async_send_packet  # type: ignore[attr-defined]

        async def send_and_report(device_id, packet_hex):
            sent = await send_packet(device_id, packet_hex)
            report_soon(coordinator, device_id, "cloud")
            return sent

        coordinator._classic_listener.async_send_packet = send_and_report  # type: ignore[attr-defined]
        sub_device = AifaSubDevice(
            id="s1", device_id="d1", name="AC", type="0", sub_type="0", device_code="115"
        )
        coordinator.get_sub_device = lambda device_id, sub_device_id: sub_device

        asyncio.run(
            coordinator.async_press_classic_temperature_key("d1", "s1", up=True)
        )

        self.assertEqual(coordinator._tls_sent, [("d1", "ffa006f0")])

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
            asyncio.run(_send_all(coordinator, commands))

        # d1 sent (ok), d2 attempted (failed) — third command never reached
        self.assertEqual(
            coordinator._tls_sent,
            [("d1", "aaaa"), ("d2", "bbbb")],
        )

    def test_dispatch_handles_empty_command_list(self) -> None:
        """No commands → no transport touched, returns empty list."""
        coordinator = self._make_coordinator()
        responses = asyncio.run(_send_all(coordinator, []))
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
            asyncio.run(_send_all(coordinator, commands))


class _AcApplyHarness:
    _STATUS_PACKET_HEX_LENGTH = 30

    def _make_coordinator(self, *, version=5, subversion=210623, sub_type="0"):
        from custom_components.aifa_smart.api import AifaSubDevice
        from custom_components.aifa_smart.coordinator import AifaAccountData

        sub_device = AifaSubDevice(
            id="s1", device_id="d1", name="冷氣", type="0", sub_type=sub_type,
            device_code="136",
        )
        if sub_type == "1":
            sub_device.type = "1"
        hub = AifaDevice(
            id="d1", name="Hub", mac=None, device_type=None, online=True,
            temperature=None, humidity=None, firmware=None,
            sub_devices=[sub_device], version=version, subversion=subversion,
        )
        coordinator = AifaSmartCoordinator.__new__(AifaSmartCoordinator)
        coordinator.data = AifaAccountData(  # type: ignore[attr-defined]
            devices={"d1": hub}, macros=[], fetched_at=None,  # type: ignore[arg-type]
        )
        coordinator._ac_runtime_states = {}  # type: ignore[attr-defined]
        coordinator._ac_apply_locks = {}  # type: ignore[attr-defined]
        coordinator._ac_last_send_at = {}  # type: ignore[attr-defined]
        coordinator._ac_pending_release_tasks = {}  # type: ignore[attr-defined]
        events: list[str] = []

        async def fake_throttle(key):
            events.append("throttle")

        coordinator._async_send_confirmed = confirming_sender(coordinator, events)  # type: ignore[attr-defined]
        coordinator._async_throttle_ac_send = fake_throttle  # type: ignore[attr-defined]
        coordinator.async_update_listeners = lambda: None  # type: ignore[attr-defined]
        coordinator._schedule_ac_pending_release = lambda *a, **k: None  # type: ignore[attr-defined]
        coordinator._events = events  # type: ignore[attr-defined]
        state = coordinator.get_ac_runtime_state("d1", "s1", device_code="136")
        state.requested_target_temperature = 26
        state.last_active_hvac_mode = "cool"
        return coordinator, state

    def _sent(self, coordinator) -> list[str]:
        return [event for event in coordinator._events if event != "throttle"]


class CoordinatorModeKeyTests(_AcApplyHarness, unittest.TestCase):
    """Hubs at version 5 / subversion 210623 take HVAC modes as single key presses."""

    def test_mode_change_sends_only_the_mode_key(self) -> None:
        coordinator, state = self._make_coordinator()
        state.apply_observed_status(_report_status("cool", 26))

        returned = asyncio.run(
            coordinator.async_apply_ac_state("d1", "s1", hvac_mode="heat", device_code="136")
        )

        self.assertEqual(self._sent(coordinator), ["ffa017f0"])
        self.assertEqual(returned, "ffa017f0")
        self.assertEqual(state.requested_hvac_mode, "heat")
        self.assertEqual(state.exposed_hvac_mode, "heat")
        self.assertEqual(state.last_status_command, "ffa017f0")
        self.assertIsNone(state.last_power_command)
        assert state.command_status is not None
        self.assertIsNone(state.command_status.target_temperature)

    def test_every_mode_maps_to_its_key(self) -> None:
        expected = {
            "auto": "ffa015f0",
            "cool": "ffa016f0",
            "heat": "ffa017f0",
            "dry": "ffa018f0",
            "fan": "ffa019f0",
        }
        for mode, command in expected.items():
            with self.subTest(mode=mode):
                coordinator, state = self._make_coordinator()
                state.apply_observed_status(_report_status("cool", 26))
                asyncio.run(
                    coordinator.async_apply_ac_state("d1", "s1", hvac_mode=mode, device_code="136")
                )
                self.assertEqual(self._sent(coordinator), [command])

    def test_reselecting_current_mode_still_sends_the_key(self) -> None:
        coordinator, state = self._make_coordinator()
        state.apply_observed_status(_report_status("cool", 26))

        asyncio.run(
            coordinator.async_apply_ac_state("d1", "s1", hvac_mode="cool", device_code="136")
        )

        self.assertEqual(self._sent(coordinator), ["ffa016f0"])

    def test_turn_on_from_off_sends_power_then_mode_key_with_throttle(self) -> None:
        coordinator, state = self._make_coordinator()
        state.requested_hvac_mode = "off"

        asyncio.run(
            coordinator.async_apply_ac_state("d1", "s1", hvac_mode="cool", device_code="136")
        )

        self.assertEqual(
            coordinator._events, ["throttle", "ffa0a1f0", "throttle", "ffa016f0"]
        )
        self.assertEqual(state.last_power_command, "ffa0a1f0")
        self.assertEqual(state.exposed_hvac_mode, "cool")

    def test_sub_type_1_mode_key_uses_a1(self) -> None:
        coordinator, state = self._make_coordinator(sub_type="1")
        state.apply_observed_status(_report_status("cool", 26))

        asyncio.run(
            coordinator.async_apply_ac_state("d1", "s1", hvac_mode="cool", device_code="136")
        )

        self.assertEqual(self._sent(coordinator), ["ffa116f0"])

    def test_setpoint_goes_out_as_the_apps_15_byte_packet(self) -> None:
        """The slider's 4-byte branch is dead code in the app; it always sends toBytes."""
        coordinator, state = self._make_coordinator()
        state.apply_observed_status(_report_status("cool", 26))

        asyncio.run(
            coordinator.async_apply_ac_state(
                "d1", "s1", target_temperature=24, device_code="136"
            )
        )

        self.assertEqual(self._sent(coordinator), ["ffa0070136240020400800000000f0"])

    def test_other_hub_versions_keep_the_status_packet(self) -> None:
        for version, subversion in ((5, 210624), (4, 210623), (None, None)):
            with self.subTest(version=version, subversion=subversion):
                coordinator, state = self._make_coordinator(
                    version=version, subversion=subversion
                )
                state.requested_hvac_mode = "off"
                asyncio.run(
                    coordinator.async_apply_ac_state(
                        "d1", "s1", hvac_mode="heat", device_code="136"
                    )
                )
                sent = self._sent(coordinator)
                # From off the app's On key goes first; its mode buttons only
                # work while on.
                self.assertEqual(len(sent), 2)
                self.assertEqual(sent[0], "ffa0a1f0")
                self.assertEqual(len(sent[1]), self._STATUS_PACKET_HEX_LENGTH)
                self.assertEqual(sent[1][:6], "ffa008")


def _report_status(mode: str, temperature: int | None, **fields) -> AifaAcStatus:
    values = dict(
        device_code="136",
        mode=mode,
        target_temperature=temperature,
        timer_ir_value=0,
        fan_mode="medium",
        swing_mode="fixed_2",
        turbo=False,
        sleep=False,
        power_saving=False,
        packet_byte_12=0,
        packet_byte_13=0,
    )
    values.update(fields)
    return AifaAcStatus(**values)


class CoordinatorAppStatusPacketTests(_AcApplyHarness, unittest.TestCase):
    """Hubs without mode keys get the app's 15-byte packet, built from its last report."""

    def _hub(self):
        return self._make_coordinator(version=4, subversion=1)

    def test_mode_change_copies_every_other_field_from_the_last_report(self) -> None:
        coordinator, state = self._hub()
        state.apply_observed_status(
            _report_status("cool", 26, timer_ir_value=12, sleep=True, turbo=True)
        )

        asyncio.run(
            coordinator.async_apply_ac_state("d1", "s1", hvac_mode="heat", device_code="136")
        )

        # keyPress 8, temp 26, timer 12, heat, medium, fixed_2, turbo before sleep.
        self.assertEqual(self._sent(coordinator), ["ffa0080136261208400804000000f0"])
        self.assertEqual(state.exposed_hvac_mode, "heat")

    def test_power_saving_wins_the_single_flag_byte(self) -> None:
        coordinator, state = self._hub()
        state.apply_observed_status(
            _report_status("cool", 26, power_saving=True, turbo=True, sleep=True)
        )

        asyncio.run(
            coordinator.async_apply_ac_state("d1", "s1", hvac_mode="dry", device_code="136")
        )

        self.assertEqual(self._sent(coordinator)[0][20:22], "01")

    def test_report_without_a_setpoint_is_repeated_as_zero(self) -> None:
        coordinator, state = self._hub()
        state.apply_observed_status(_report_status("cool", None))

        asyncio.run(
            coordinator.async_apply_ac_state("d1", "s1", hvac_mode="fan", device_code="136")
        )

        self.assertEqual(self._sent(coordinator), ["ffa0080136000080400800000000f0"])

    def test_setpoint_up_and_down_use_key_press_6_and_7(self) -> None:
        for target, key_press in ((27, "06"), (26, "07"), (25, "07")):
            with self.subTest(target=target):
                coordinator, state = self._hub()
                state.apply_observed_status(_report_status("cool", 26))
                asyncio.run(
                    coordinator.async_apply_ac_state(
                        "d1", "s1", target_temperature=target, device_code="136"
                    )
                )
                sent = self._sent(coordinator)
                self.assertEqual(len(sent), 1)
                self.assertEqual(sent[0][4:6], key_press)
                self.assertEqual(sent[0][10:12], str(target))

    def test_setpoint_is_refused_where_the_app_disables_the_slider(self) -> None:
        from homeassistant.exceptions import ServiceValidationError

        for mode, temperature in (("auto", 26), ("fan_only", 26), ("dry", None), ("off", None)):
            with self.subTest(mode=mode):
                coordinator, state = self._hub()
                state.apply_observed_status(_report_status(mode, temperature))
                with self.assertRaises(ServiceValidationError) as ctx:
                    asyncio.run(
                        coordinator.async_apply_ac_state(
                            "d1", "s1", target_temperature=24, device_code="136"
                        )
                    )
                self.assertEqual(ctx.exception.translation_key, "ac_temperature_unavailable")
                self.assertEqual(self._sent(coordinator), [])

    def test_without_any_report_the_packet_falls_back_to_auto_fan_and_no_swing(self) -> None:
        coordinator, state = self._hub()
        # On from an earlier command the hub never reported.
        state.apply_sent_command_status(_report_status("cool", 26))

        asyncio.run(
            coordinator.async_apply_ac_state("d1", "s1", hvac_mode="heat", device_code="136")
        )

        self.assertEqual(self._sent(coordinator), ["ffa0080136260008010000000000f0"])

    def test_turn_on_without_a_report_lets_the_next_report_decide(self) -> None:
        coordinator, state = self._hub()
        state.requested_hvac_mode = "off"
        probes: list[str | None] = []

        async def send_without_report(command, confirmation):
            coordinator._events.append(command.command)  # noqa: SLF001
            return "lan", None

        coordinator._async_send_confirmed = send_without_report  # type: ignore[attr-defined]
        coordinator._schedule_ac_pending_release = (  # type: ignore[attr-defined]
            lambda device_id, sub_device_id, **kwargs: probes.append(kwargs["expected_mode"])
        )

        asyncio.run(
            coordinator.async_apply_ac_state("d1", "s1", hvac_mode="on", device_code="136")
        )

        self.assertEqual(self._sent(coordinator), ["ffa0a1f0"])
        self.assertEqual(state.exposed_hvac_mode, "cool")
        self.assertFalse(state.command_status_pending)
        self.assertEqual(probes, [])
        # Nothing pending, so a later off report is not held back.
        state.apply_observed_status(_report_status("off", None))
        self.assertEqual(state.exposed_hvac_mode, "off")

    def test_mode_from_off_builds_the_packet_from_the_report_after_power_on(self) -> None:
        coordinator, state = self._hub()
        state.apply_observed_status(_report_status("off", None))
        on_report = _report_status("cool", 25, fan_mode="high", swing_mode="fixed_4")

        async def send(command, confirmation):
            coordinator._events.append(command.command)  # noqa: SLF001
            if command.command == "ffa0a1f0":
                state.apply_observed_status(on_report)
                return "lan", on_report
            observed = replace(on_report, mode="heat")
            state.apply_observed_status(observed)
            return "lan", observed

        coordinator._async_send_confirmed = send  # type: ignore[attr-defined]

        asyncio.run(
            coordinator.async_apply_ac_state("d1", "s1", hvac_mode="heat", device_code="136")
        )

        self.assertEqual(
            self._sent(coordinator), ["ffa0a1f0", "ffa0080136250008804000000000f0"]
        )

    def test_turn_on_sends_only_the_power_key(self) -> None:
        for version, subversion in ((4, 1), (5, 210623)):
            with self.subTest(version=version):
                coordinator, state = self._make_coordinator(version=version, subversion=subversion)
                state.requested_hvac_mode = "off"
                asyncio.run(
                    coordinator.async_apply_ac_state("d1", "s1", hvac_mode="on", device_code="136")
                )
                self.assertEqual(self._sent(coordinator), ["ffa0a1f0"])
                self.assertEqual(state.last_power_command, "ffa0a1f0")
                self.assertEqual(state.exposed_hvac_mode, "cool")


class ApplyClassicSensorStateTests(unittest.TestCase):
    """Cloud readings win over the hub's raw sensor packet."""

    @staticmethod
    def _apply(device: AifaDevice, decoded: AifaClassicSensorState) -> bool:
        coordinator = AifaSmartCoordinator.__new__(AifaSmartCoordinator)
        coordinator.get_device = lambda device_id: device
        return coordinator._apply_classic_sensor_state(device.id, decoded)

    @staticmethod
    def _device(temperature: float | None, humidity: float | None) -> AifaDevice:
        return AifaDevice(
            id="7001",
            name="hub",
            mac=None,
            device_type="4",
            online=True,
            temperature=temperature,
            humidity=humidity,
            firmware=None,
        )

    def test_cloud_values_are_not_overwritten(self) -> None:
        device = self._device(27.8, 67.5)
        updated = self._apply(device, AifaClassicSensorState(28.3, 57.3))
        self.assertFalse(updated)
        self.assertEqual(device.temperature, 27.8)
        self.assertEqual(device.humidity, 67.5)
        self.assertEqual(device.sensor_samples, {})

    def test_packet_fills_readings_missing_from_cloud(self) -> None:
        device = self._device(27.8, None)
        updated = self._apply(device, AifaClassicSensorState(28.3, 57.3))
        self.assertTrue(updated)
        self.assertEqual(device.temperature, 27.8)
        self.assertEqual(device.humidity, 57.3)
        self.assertEqual(device.sensor_samples["humidity"].value, 57.3)
        self.assertNotIn("temperature", device.sensor_samples)


if __name__ == "__main__":
    unittest.main()
