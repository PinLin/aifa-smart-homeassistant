"""Runtime state helpers for AIFA Smart entities."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime

from .ac import (
    AIFA_AC_DEFAULT_TARGET_TEMP,
    AifaAcStatus,
    clamp_target_temperature,
    get_default_mode,
)


def _same_report(left: AifaAcStatus, right: AifaAcStatus) -> bool:
    return (
        left.mode,
        left.target_temperature,
        left.timer_ir_value,
        left.fan_mode,
        left.swing_mode,
        left.turbo,
        left.sleep,
        left.power_saving,
    ) == (
        right.mode,
        right.target_temperature,
        right.timer_ir_value,
        right.fan_mode,
        right.swing_mode,
        right.turbo,
        right.sleep,
        right.power_saving,
    )


@dataclass(slots=True)
class AifaAcRuntimeState:
    """Shared requested and observed state for a single AC sub-device."""

    device_code: str | None
    requested_hvac_mode: str
    requested_target_temperature: int
    last_active_hvac_mode: str | None = None
    observed_status: AifaAcStatus | None = None
    observed_revision: int = 0
    # Survives a sent command clearing observed_status, because the hub does
    # not report again after every command and the app keeps showing b7.
    last_reported_fan_mode: str | None = None
    command_status: AifaAcStatus | None = None
    command_status_pending: bool = False
    state_source: str = "requested_fallback"
    last_status_command: str | None = None
    last_power_command: str | None = None
    # Set after a power off the hub never reports: the AC stays off while
    # reports repeat power_off_report; any other report means it changed.
    # Without a report yet, the next one becomes power_off_report.
    power_off_held: bool = False
    power_off_report: AifaAcStatus | None = None

    @classmethod
    def create(cls, device_code: str | None) -> AifaAcRuntimeState:
        """Build a runtime state with sane defaults for a device code."""
        return cls(
            device_code=device_code,
            # i-Ctrl Pro shows Off until the hub's first status report.
            requested_hvac_mode="off",
            requested_target_temperature=clamp_target_temperature(
                AIFA_AC_DEFAULT_TARGET_TEMP,
                device_code=device_code,
            ),
            last_active_hvac_mode=get_default_mode(device_code),
        )

    @staticmethod
    def _main_fields_match(left: AifaAcStatus, right: AifaAcStatus) -> bool:
        """Return True when two statuses agree on mode and setpoint.

        Those are the only fields a mode or setpoint command sets; every
        other field of a 15-byte packet repeats the hub's own report.
        """
        if left.mode != right.mode:
            return False
        if (
            left.target_temperature is not None
            and right.target_temperature is not None
            and left.target_temperature != right.target_temperature
        ):
            return False
        return True

    @property
    def command_status_matches_observed(self) -> bool:
        """Return True when the last sent-command state matches the latest observed state."""
        if self.command_status is None or self.observed_status is None:
            return False
        return self._main_fields_match(self.command_status, self.observed_status)

    @property
    def effective_state_source(self) -> str:
        """Return the best available source for the exposed main AC state."""
        if (
            self.command_status is not None
            and (
                self.observed_status is None
                or (self.command_status_pending and not self.command_status_matches_observed)
            )
        ):
            return "sent_command"
        if self.observed_status is not None:
            return "aifa_socket"
        return self.state_source

    def apply_sent_command_status(self, status: AifaAcStatus) -> None:
        """Record the command-derived AC state after a local send."""
        preserve_observed_main_state = (
            self.observed_status is not None and self._main_fields_match(self.observed_status, status)
        )
        self.command_status = status
        self.command_status_pending = not preserve_observed_main_state
        if preserve_observed_main_state:
            self.state_source = "aifa_socket"
        else:
            self.state_source = "sent_command"
            self.observed_status = None
        if status.mode != "off":
            self.last_active_hvac_mode = status.mode

    def apply_confirmed_command_status(self, status: AifaAcStatus) -> None:
        """Record a command whose result the hub has already reported.

        The hub report is already in observed_status and stays the state.
        """
        self.command_status = status
        self.command_status_pending = False
        if self.observed_status is not None:
            self.state_source = "aifa_socket"
        if status.mode != "off":
            self.last_active_hvac_mode = status.mode

    def hold_power_off(self, report: AifaAcStatus | None) -> None:
        """Keep showing off until a report other than `report` arrives."""
        self.power_off_held = True
        self.power_off_report = report

    def release_power_off(self) -> None:
        self.power_off_held = False
        self.power_off_report = None

    def clear_pending_command_status(self) -> None:
        """Stop preferring the command-derived state over observed socket state."""
        self.command_status_pending = False

    def apply_observed_status(self, observed: AifaAcStatus) -> None:
        """Merge a newly observed AIFA socket state into the runtime model."""
        if observed.reported_fan_mode is not None:
            self.last_reported_fan_mode = observed.reported_fan_mode
        if self.power_off_held:
            if self.power_off_report is None:
                self.power_off_report = observed
            if _same_report(self.power_off_report, observed):
                self.observed_status = observed
                self.observed_revision += 1
                return
            self.release_power_off()
        previous_command_status = self.command_status
        previous_command_matches = (
            previous_command_status is not None and self._main_fields_match(previous_command_status, observed)
        )

        # Stale-observed guard: if a command is in flight (command_status_pending)
        # and the incoming observed packet contradicts that command's mode/temp,
        # the hub almost certainly has not propagated our IR yet — observed is
        # the *previous* state. Bumping observed_revision still lets the wait
        # loop see fresh data, but we don't let stale "off" clobber a freshly
        # sent "cool" (which would silently revert command_status, and so
        # the shown mode, to off). The next pushed report unwinds pending, or
        # the coordinator's pending release does once its window ends.
        if (
            self.command_status_pending
            and previous_command_status is not None
            and not previous_command_matches
        ):
            self.observed_status = observed
            self.observed_revision += 1
            return

        self.observed_status = observed
        self.observed_revision += 1
        self.state_source = "aifa_socket"
        self.requested_hvac_mode = observed.mode
        if observed.target_temperature is not None:
            self.requested_target_temperature = clamp_target_temperature(
                observed.target_temperature,
                device_code=self.device_code,
            )
        if observed.mode != "off":
            self.last_active_hvac_mode = observed.mode

        if observed.mode == "off":
            self.command_status = AifaAcStatus(
                device_code=observed.device_code or self.device_code,
                mode="off",
                target_temperature=None,
                timer_ir_value=0,
                fan_mode=None,
                swing_mode=None,
                turbo=False,
                sleep=False,
                power_saving=False,
                packet_byte_12=observed.packet_byte_12,
                packet_byte_13=observed.packet_byte_13,
            )
            self.command_status_pending = False
            return

        if previous_command_status is not None and not previous_command_matches:
            self.command_status = None
            self.command_status_pending = False

        if self.command_status is not None and self.command_status_matches_observed:
            self.command_status_pending = False

    @property
    def exposed_hvac_mode(self) -> str:
        """Return the state that should be shown to Home Assistant."""
        if self.power_off_held:
            return "off"
        if (
            self.command_status is not None
            and (
                self.observed_status is None
                or (self.command_status_pending and not self.command_status_matches_observed)
            )
        ):
            return self.command_status.mode
        if self.observed_status is not None:
            return self.observed_status.mode
        if self.command_status is not None:
            return self.command_status.mode
        # A requested mode restored from before a restart is not a report;
        # like i-Ctrl Pro, show Off until the hub sends one.
        return "off"

    @property
    def exposed_target_temperature(self) -> int:
        """Return the best available target temperature."""
        if self.command_status is not None and (
            self.observed_status is None
            or (self.command_status_pending and not self.command_status_matches_observed)
        ):
            if self.command_status.target_temperature is not None:
                return self.command_status.target_temperature
        if self.observed_status is not None and self.observed_status.target_temperature is not None:
            return self.observed_status.target_temperature
        if self.command_status is not None and self.command_status.target_temperature is not None:
            return self.command_status.target_temperature
        return self.requested_target_temperature

    @property
    def is_assumed(self) -> bool:
        """Return True when Home Assistant is still using requested fallback state."""
        return self.effective_state_source != "aifa_socket"
