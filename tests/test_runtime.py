"""Tests for AC runtime state fallback rules."""
from __future__ import annotations

import unittest

from custom_components.aifa_smart.ac import AifaAcStatus
from custom_components.aifa_smart.runtime import AifaAcRuntimeState


class AifaAcRuntimeStateTests(unittest.TestCase):
    """Validate the requested-vs-observed runtime exposure rules."""

    def test_shows_off_until_the_first_report(self) -> None:
        state = AifaAcRuntimeState.create("136")
        self.assertEqual(state.exposed_hvac_mode, "off")
        self.assertEqual(state.requested_hvac_mode, "off")
        # Turning on without a report falls back to this mode.
        self.assertEqual(state.last_active_hvac_mode, "cool")
        # A mode restored from before a restart is not a report either.
        state.requested_hvac_mode = "heat"
        self.assertEqual(state.exposed_hvac_mode, "off")

    def test_command_status_drives_exposed_fields_while_socket_state_is_stale(self) -> None:
        """A freshly sent command should win until the socket catches up."""
        state = AifaAcRuntimeState.create("136")
        state.command_status = AifaAcStatus(
            device_code="136",
            mode="cool",
            target_temperature=26,
            timer_ir_value=0,
            fan_mode="medium",
            swing_mode="auto",
            turbo=False,
            sleep=False,
            power_saving=False,
            packet_byte_12=0,
            packet_byte_13=0,
        )
        state.command_status_pending = True
        state.state_source = "sent_command"
        state.observed_status = AifaAcStatus(
            device_code="136",
            mode="heat",
            target_temperature=24,
            timer_ir_value=0,
            fan_mode="high",
            swing_mode="off",
            turbo=False,
            sleep=False,
            power_saving=False,
            packet_byte_12=0,
            packet_byte_13=0,
        )

        self.assertEqual(state.effective_state_source, "sent_command")
        self.assertEqual(state.exposed_hvac_mode, "cool")
        self.assertEqual(state.exposed_target_temperature, 26)

    def test_observed_state_backfills_requested_base(self) -> None:
        """Observed AIFA state should become the new control baseline."""
        state = AifaAcRuntimeState.create("136")
        state.requested_hvac_mode = "heat"
        state.requested_target_temperature = 28
        state.command_status = AifaAcStatus(
            device_code="136",
            mode="cool",
            target_temperature=24,
            timer_ir_value=0,
            fan_mode="medium",
            swing_mode="auto",
            turbo=True,
            sleep=False,
            power_saving=False,
            packet_byte_12=0,
            packet_byte_13=0,
        )

        state.apply_observed_status(
            AifaAcStatus(
                device_code="136",
                mode="cool",
                target_temperature=24,
                timer_ir_value=0,
                fan_mode="medium",
                swing_mode="auto",
                turbo=False,
                sleep=False,
                power_saving=False,
                packet_byte_12=0,
                packet_byte_13=0,
            )
        )

        self.assertEqual(state.requested_hvac_mode, "cool")
        self.assertEqual(state.requested_target_temperature, 24)
        self.assertEqual(state.effective_state_source, "aifa_socket")

    def test_observed_state_clears_stale_command_when_main_shape_drifts(self) -> None:
        """External state changes should stop reusing a stale command."""
        state = AifaAcRuntimeState.create("136")
        state.command_status = AifaAcStatus(
            device_code="136",
            mode="cool",
            target_temperature=24,
            timer_ir_value=0,
            fan_mode="high",
            swing_mode="off",
            turbo=True,
            sleep=False,
            power_saving=False,
            packet_byte_12=0,
            packet_byte_13=0,
        )

        state.apply_observed_status(
            AifaAcStatus(
                device_code="136",
                mode="heat",
                target_temperature=25,
                timer_ir_value=0,
                fan_mode="medium",
                swing_mode="auto",
                turbo=False,
                sleep=False,
                power_saving=False,
                packet_byte_12=0,
                packet_byte_13=0,
            )
        )

        self.assertIsNone(state.command_status)
        self.assertEqual(state.requested_hvac_mode, "heat")
        self.assertEqual(state.requested_target_temperature, 25)

    def test_observed_off_state_settles_when_not_pending(self) -> None:
        """An observed off packet becomes the state when no command is in flight."""
        state = AifaAcRuntimeState.create("136")

        state.apply_observed_status(
            AifaAcStatus(
                device_code="136",
                mode="off",
                target_temperature=None,
                timer_ir_value=0,
                fan_mode=None,
                swing_mode=None,
                turbo=False,
                sleep=False,
                power_saving=False,
                packet_byte_12=0,
                packet_byte_13=0,
            )
        )

        self.assertEqual(state.effective_state_source, "aifa_socket")
        self.assertEqual(state.exposed_hvac_mode, "off")
        self.assertEqual(state.command_status.mode, "off")
        self.assertFalse(state.command_status_pending)

    def test_stale_observed_does_not_clobber_pending_command(self) -> None:
        """Stale observed off packet must not silently revert a freshly sent cool command.

        Reproduces the A3 dropped-command bug: turn_off → set_hvac_mode(cool)
        within ~1s. The hub's pre-cool 'off' observation arrives after the
        cool IR was sent. Without the stale-observed guard, that observation
        clobbers requested_hvac_mode and command_status back to off, and the
        cool intent is lost from runtime state.
        """
        state = AifaAcRuntimeState.create("136")
        state.requested_hvac_mode = "cool"
        state.apply_sent_command_status(
            AifaAcStatus(
                device_code="136",
                mode="cool",
                target_temperature=24,
                timer_ir_value=0,
                fan_mode="medium",
                swing_mode="fixed_3",
                turbo=False,
                sleep=False,
                power_saving=False,
                packet_byte_12=0,
                packet_byte_13=0,
            )
        )
        baseline_revision = state.observed_revision
        self.assertTrue(state.command_status_pending)

        state.apply_observed_status(
            AifaAcStatus(
                device_code="136",
                mode="off",
                target_temperature=None,
                timer_ir_value=0,
                fan_mode=None,
                swing_mode=None,
                turbo=False,
                sleep=False,
                power_saving=False,
                packet_byte_12=0,
                packet_byte_13=0,
            )
        )

        self.assertEqual(state.requested_hvac_mode, "cool")
        self.assertIsNotNone(state.command_status)
        self.assertEqual(state.command_status.mode, "cool")
        self.assertTrue(state.command_status_pending)
        self.assertEqual(state.observed_revision, baseline_revision + 1)

    def test_matching_observed_clears_pending(self) -> None:
        """When observed catches up to the sent command, pending should clear."""
        state = AifaAcRuntimeState.create("136")
        state.apply_sent_command_status(
            AifaAcStatus(
                device_code="136",
                mode="cool",
                target_temperature=24,
                timer_ir_value=0,
                fan_mode="medium",
                swing_mode="fixed_3",
                turbo=False,
                sleep=False,
                power_saving=False,
                packet_byte_12=0,
                packet_byte_13=0,
            )
        )

        state.apply_observed_status(
            AifaAcStatus(
                device_code="136",
                mode="cool",
                target_temperature=24,
                timer_ir_value=0,
                fan_mode="medium",
                swing_mode="fixed_3",
                turbo=False,
                sleep=False,
                power_saving=False,
                packet_byte_12=0,
                packet_byte_13=0,
            )
        )

        self.assertFalse(state.command_status_pending)
        self.assertEqual(state.requested_hvac_mode, "cool")

    def test_sent_command_keeps_matching_observed_main_state(self) -> None:
        """A fresh local command should not erase an already observed matching socket state."""
        state = AifaAcRuntimeState.create("136")
        state.apply_observed_status(
            AifaAcStatus(
                device_code="136",
                mode="cool",
                target_temperature=26,
                timer_ir_value=0,
                fan_mode="medium",
                swing_mode="auto",
                turbo=False,
                sleep=False,
                power_saving=False,
                packet_byte_12=0,
                packet_byte_13=0,
            )
        )

        state.apply_sent_command_status(
            AifaAcStatus(
                device_code="136",
                mode="cool",
                target_temperature=26,
                timer_ir_value=0,
                fan_mode="medium",
                swing_mode="auto",
                turbo=True,
                sleep=False,
                power_saving=False,
                packet_byte_12=0,
                packet_byte_13=0,
            )
        )

        self.assertIsNotNone(state.observed_status)
        self.assertEqual(state.effective_state_source, "aifa_socket")
        self.assertFalse(state.command_status_pending)


    def test_held_power_off_takes_the_next_report_when_it_has_none(self) -> None:
        state = AifaAcRuntimeState.create("136")
        state.hold_power_off(None)
        cool = AifaAcStatus(
            device_code="136", mode="cool", target_temperature=None, timer_ir_value=0,
            fan_mode="auto", swing_mode="off", turbo=False, sleep=False,
            power_saving=False, packet_byte_12=0, packet_byte_13=0,
        )
        state.apply_observed_status(cool)
        self.assertEqual(state.exposed_hvac_mode, "off")
        state.apply_observed_status(cool)
        self.assertEqual(state.exposed_hvac_mode, "off")
        state.apply_observed_status(
            AifaAcStatus(
                device_code="136", mode="heat", target_temperature=None, timer_ir_value=0,
                fan_mode="auto", swing_mode="off", turbo=False, sleep=False,
                power_saving=False, packet_byte_12=0, packet_byte_13=0,
            )
        )
        self.assertFalse(state.power_off_held)
        self.assertEqual(state.exposed_hvac_mode, "heat")

if __name__ == "__main__":
    unittest.main()
