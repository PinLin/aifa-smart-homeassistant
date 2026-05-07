"""Tests for AC runtime state fallback rules."""
from __future__ import annotations

import unittest

from custom_components.aifa_smart.ac import AifaAcStatus
from custom_components.aifa_smart.runtime import AifaAcRuntimeState


class AifaAcRuntimeStateTests(unittest.TestCase):
    """Validate the requested-vs-observed runtime exposure rules."""

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
        self.assertEqual(state.exposed_fan_mode, "medium")
        self.assertEqual(state.exposed_swing_mode, "auto")

    def test_unverified_observed_helper_flags_do_not_override_requested(self) -> None:
        """The AIFA hub doesn't shadow helper flags via /commands/raw, so when we
        have no positive signal that observed is trustworthy, the user's draft wins."""
        state = AifaAcRuntimeState.create("136")
        state.requested_turbo = True
        state.requested_sleep = True
        state.requested_power_saving = True
        state.observed_status = AifaAcStatus(
            device_code="136",
            mode="cool",
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
        state.state_source = "aifa_socket"
        state.helper_flags_state_source = "requested_fallback"

        self.assertEqual(state.effective_helper_flags_state_source, "aifa_socket")
        # Requested draft preserved — observed's all-zero is presumed stale.
        self.assertTrue(state.exposed_turbo)
        self.assertTrue(state.exposed_sleep)
        self.assertTrue(state.exposed_power_saving)
        # With observed_status present (mode != off) and no command_status,
        # the switches are not "assumed" in HA's sense — we have a socket
        # connection; we just don't trust its specific values for flags.
        self.assertFalse(state.helper_flags_are_assumed)

    def test_observed_state_backfills_requested_base_and_keeps_matching_command_flags(self) -> None:
        """Observed AIFA state should become the new control baseline without erasing matched flags."""
        state = AifaAcRuntimeState.create("136")
        state.requested_hvac_mode = "heat"
        state.requested_target_temperature = 28
        state.requested_fan_mode = "high"
        state.requested_swing_mode = "fixed_5"
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
        state.helper_flags_state_source = "sent_command"

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
        # Hub doesn't shadow fan/swing, so observed values no longer backfill
        # the user's draft. Pre-test values stay put.
        self.assertEqual(state.requested_fan_mode, "high")
        self.assertEqual(state.requested_swing_mode, "fixed_5")
        self.assertEqual(state.effective_state_source, "aifa_socket")
        # Command_status wins for helper flags because hub doesn't echo them.
        self.assertEqual(state.effective_helper_flags_state_source, "sent_command")
        self.assertTrue(state.exposed_turbo)
        self.assertFalse(state.helper_flags_are_assumed)

    def test_observed_state_clears_stale_command_flags_when_main_shape_drifts(self) -> None:
        """External state changes should stop reusing old helper flags from a stale command."""
        state = AifaAcRuntimeState.create("136")
        state.requested_turbo = True
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
        state.helper_flags_state_source = "sent_command"

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
        # External shape drift clears command_status but keeps the user's
        # helper draft — hub doesn't echo flags so observed=False is presumed
        # stale rather than authoritative.
        self.assertTrue(state.exposed_turbo)
        self.assertEqual(state.effective_helper_flags_state_source, "aifa_socket")

    def test_observed_off_state_clears_helper_flags_when_not_pending(self) -> None:
        """An observed off packet should reset helper flags when no command is in flight."""
        state = AifaAcRuntimeState.create("136")
        state.requested_turbo = True
        state.requested_sleep = True
        state.requested_power_saving = True

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
        self.assertEqual(state.effective_helper_flags_state_source, "aifa_socket")
        self.assertFalse(state.exposed_turbo)
        self.assertFalse(state.exposed_sleep)
        self.assertFalse(state.exposed_power_saving)
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

    def test_helper_flags_use_observed_state_once_verified(self) -> None:
        """Verified helper flags should come from the observed packet."""
        state = AifaAcRuntimeState.create("136")
        state.requested_turbo = False
        state.requested_sleep = False
        state.requested_power_saving = False
        state.observed_status = AifaAcStatus(
            device_code="136",
            mode="cool",
            target_temperature=24,
            timer_ir_value=0,
            fan_mode="high",
            swing_mode="off",
            turbo=True,
            sleep=True,
            power_saving=True,
            packet_byte_12=0,
            packet_byte_13=0,
        )
        state.state_source = "aifa_socket"
        state.helper_flags_state_source = "aifa_socket"

        self.assertTrue(state.exposed_turbo)
        self.assertTrue(state.exposed_sleep)
        self.assertTrue(state.exposed_power_saving)

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
        # Command_status now wins over observed for helper flags because the
        # hub doesn't shadow them accurately via /commands/raw.
        self.assertEqual(state.effective_helper_flags_state_source, "sent_command")
        self.assertTrue(state.exposed_turbo)


if __name__ == "__main__":
    unittest.main()
