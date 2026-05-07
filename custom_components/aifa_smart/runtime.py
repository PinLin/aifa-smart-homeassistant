"""Runtime state helpers for AIFA Smart entities."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime

from .ac import (
    AIFA_AC_DEFAULT_FAN_MODE,
    AIFA_AC_DEFAULT_TARGET_TEMP,
    AIFA_AC_DEFAULT_SWING_MODE,
    AifaAcStatus,
    clamp_target_temperature,
    get_default_mode,
    get_supported_fan_modes,
    get_supported_swing_modes,
)


def resolve_helper_flag_mutex(
    *,
    runtime_power_saving: bool,
    runtime_turbo: bool,
    runtime_sleep: bool,
    caller_power_saving: bool | None,
    caller_turbo: bool | None,
    caller_sleep: bool | None,
) -> tuple[bool, bool, bool]:
    """Apply the AC helper-flag mutual-exclusion contract.

    The hub packet encodes these helpers in a single feature byte
    (0x01=power_saving, 0x04=turbo, 0x10=sleep), so at most one can be
    active at a time. ``None`` for a caller arg means "leave alone";
    ``True``/``False`` is an explicit toggle from a switch turn_on/turn_off.

    Caller intent wins over runtime state: explicitly turning one ON clears
    the other two even if a different one was already on. Otherwise the
    runtime state's existing flag wins (priority: power_saving > turbo >
    sleep) so we don't drop helpers the user previously enabled.

    Returns ``(power_saving, turbo, sleep)`` for the outgoing packet.
    """
    resolved_power_saving = (
        runtime_power_saving if caller_power_saving is None else caller_power_saving
    )
    resolved_turbo = runtime_turbo if caller_turbo is None else caller_turbo
    resolved_sleep = runtime_sleep if caller_sleep is None else caller_sleep

    if caller_power_saving is True:
        resolved_turbo = False
        resolved_sleep = False
    elif caller_turbo is True:
        resolved_sleep = False
        resolved_power_saving = False
    elif caller_sleep is True:
        resolved_turbo = False
        resolved_power_saving = False
    elif resolved_power_saving:
        resolved_turbo = False
        resolved_sleep = False
    elif resolved_turbo:
        resolved_sleep = False
    elif resolved_sleep:
        resolved_turbo = False

    return resolved_power_saving, resolved_turbo, resolved_sleep


@dataclass(slots=True)
class AifaAcRuntimeState:
    """Shared requested and observed state for a single AC sub-device."""

    device_code: str | None
    requested_hvac_mode: str
    requested_target_temperature: int
    requested_fan_mode: str
    requested_swing_mode: str
    requested_turbo: bool = False
    requested_sleep: bool = False
    requested_power_saving: bool = False
    last_active_hvac_mode: str | None = None
    observed_status: AifaAcStatus | None = None
    observed_revision: int = 0
    command_status: AifaAcStatus | None = None
    command_status_pending: bool = False
    state_source: str = "requested_fallback"
    helper_flags_state_source: str = "requested_fallback"
    last_status_command: str | None = None
    last_power_command: str | None = None

    @classmethod
    def create(cls, device_code: str | None) -> AifaAcRuntimeState:
        """Build a runtime state with sane defaults for a device code."""
        fan_modes = get_supported_fan_modes(device_code)
        swing_modes = get_supported_swing_modes(device_code)
        default_mode = get_default_mode(device_code)
        return cls(
            device_code=device_code,
            requested_hvac_mode=default_mode,
            requested_target_temperature=clamp_target_temperature(
                AIFA_AC_DEFAULT_TARGET_TEMP,
                device_code=device_code,
            ),
            requested_fan_mode=fan_modes[0] if fan_modes else AIFA_AC_DEFAULT_FAN_MODE,
            requested_swing_mode=swing_modes[-1] if swing_modes else AIFA_AC_DEFAULT_SWING_MODE,
            last_active_hvac_mode=default_mode,
        )

    @staticmethod
    def _main_fields_match(left: AifaAcStatus, right: AifaAcStatus) -> bool:
        """Return True when two statuses agree on the hub-shadowed fields.

        Empirical (verified 2026-04-25 against AIFA i-Ctrl hub firmware):
        the AIFA hub only shadows mode and target_temperature in the classic
        socket observed packet. fan_mode, swing_mode, and helper flags stay at
        the hub's last-known default regardless of what the IR payload sent
        via the classic TLS socket specified. Comparing those fields here produces
        false 'mismatch' signals after every HA-driven send and causes the
        runtime to clear command_status and revert the user's draft. So we
        only check the two reliably shadowed fields.
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

    @property
    def effective_helper_flags_state_source(self) -> str:
        """Return the best available source for helper-flag exposure.

        The AIFA hub does not shadow turbo/sleep/power_saving from any
        command path (verified 2026-04-25: sending sleep=True over both
        the classic TLS socket and the cloud's `/commands/raw` REST
        endpoint left the observed feature byte at 0x00). An observed mode=off
        packet implicitly means all flags are off (reliable), but for any
        active mode we prefer command_status over observed.
        """
        if self.observed_status is not None and self.observed_status.mode == "off":
            return "aifa_socket"
        if self.command_status is not None:
            return "sent_command"
        if self.observed_status is not None:
            return "aifa_socket"
        return self.helper_flags_state_source

    def apply_sent_command_status(self, status: AifaAcStatus) -> None:
        """Record the command-derived AC state after a local send."""
        preserve_observed_main_state = (
            self.observed_status is not None and self._main_fields_match(self.observed_status, status)
        )
        self.command_status = status
        self.command_status_pending = not preserve_observed_main_state
        self.helper_flags_state_source = "sent_command"
        if preserve_observed_main_state:
            self.state_source = "aifa_socket"
        else:
            self.state_source = "sent_command"
            self.observed_status = None
        if status.mode != "off":
            self.last_active_hvac_mode = status.mode

    def clear_pending_command_status(self) -> None:
        """Stop preferring the command-derived state over observed socket state."""
        self.command_status_pending = False

    def apply_observed_status(self, observed: AifaAcStatus) -> None:
        """Merge a newly observed AIFA socket state into the runtime model."""
        previous_command_status = self.command_status
        previous_command_matches = (
            previous_command_status is not None and self._main_fields_match(previous_command_status, observed)
        )

        # Stale-observed guard: if a command is in flight (command_status_pending)
        # and the incoming observed packet contradicts that command's mode/temp,
        # the hub almost certainly has not propagated our IR yet — observed is
        # the *previous* state. Bumping observed_revision still lets the wait
        # loop see fresh data, but we don't let stale "off" clobber a freshly
        # sent "cool" (which would silently revert requested_hvac_mode and
        # command_status to off). The follow-up probe / next observed packet
        # will eventually deliver the matching state and unwind pending then.
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
        # Deliberately do NOT backfill requested_fan_mode / requested_swing_mode
        # from observed: the hub does not shadow those accurately (see
        # _main_fields_match for the verification note). Keep the user's draft.

        if observed.mode != "off":
            self.last_active_hvac_mode = observed.mode

        if observed.mode == "off":
            self.requested_turbo = False
            self.requested_sleep = False
            self.requested_power_saving = False
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
            self.helper_flags_state_source = "aifa_socket"
            return

        if previous_command_status is not None and not previous_command_matches:
            self.command_status = None
            self.command_status_pending = False
            # Don't backfill requested helper flags from observed — hub doesn't
            # shadow them accurately, so observed is often stale. Keep draft.

        if observed.turbo or observed.sleep or observed.power_saving:
            self.requested_turbo = observed.turbo
            self.requested_sleep = observed.sleep
            self.requested_power_saving = observed.power_saving
            self.helper_flags_state_source = "aifa_socket"
        elif self.command_status is not None and self.command_status_matches_observed:
            self.helper_flags_state_source = "sent_command"
        else:
            self.helper_flags_state_source = "requested_fallback"

        if self.command_status is not None and self.command_status_matches_observed:
            self.command_status_pending = False

    @property
    def exposed_hvac_mode(self) -> str:
        """Return the state that should be shown to Home Assistant."""
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
        return self.requested_hvac_mode

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
    def exposed_fan_mode(self) -> str:
        """Return the best available fan mode.

        Hub-shadow doesn't echo fan_mode reliably — command_status wins,
        then the user's last draft (requested_fan_mode). observed is only
        a last-resort fallback when we have neither.
        """
        if self.command_status is not None and self.command_status.fan_mode is not None:
            return self.command_status.fan_mode
        if self.requested_fan_mode:
            return self.requested_fan_mode
        if self.observed_status is not None and self.observed_status.fan_mode is not None:
            return self.observed_status.fan_mode
        return self.requested_fan_mode

    @property
    def exposed_swing_mode(self) -> str:
        """Return the best available swing mode. Same rationale as exposed_fan_mode."""
        if self.command_status is not None and self.command_status.swing_mode is not None:
            return self.command_status.swing_mode
        if self.requested_swing_mode:
            return self.requested_swing_mode
        if self.observed_status is not None and self.observed_status.swing_mode is not None:
            return self.observed_status.swing_mode
        return self.requested_swing_mode

    @property
    def exposed_turbo(self) -> bool:
        """Return the best available turbo flag.

        Hub doesn't shadow turbo via any command path. Priority:
        1. observed mode=off → False (reliable)
        2. command_status → user just sent this
        3. helper_flags_state_source=="aifa_socket" → a prior observed packet
           showed a positive flag, proving the hub can track flags right now
        4. requested draft → last thing the user set
        """
        if self.observed_status is not None and self.observed_status.mode == "off":
            return False
        if self.command_status is not None:
            return self.command_status.turbo
        if (
            self.observed_status is not None
            and self.helper_flags_state_source == "aifa_socket"
        ):
            return self.observed_status.turbo
        return self.requested_turbo

    @property
    def exposed_sleep(self) -> bool:
        """Return the best available sleep flag. See exposed_turbo."""
        if self.observed_status is not None and self.observed_status.mode == "off":
            return False
        if self.command_status is not None:
            return self.command_status.sleep
        if (
            self.observed_status is not None
            and self.helper_flags_state_source == "aifa_socket"
        ):
            return self.observed_status.sleep
        return self.requested_sleep

    @property
    def exposed_power_saving(self) -> bool:
        """Return the best available power-saving flag. See exposed_turbo."""
        if self.observed_status is not None and self.observed_status.mode == "off":
            return False
        if self.command_status is not None:
            return self.command_status.power_saving
        if (
            self.observed_status is not None
            and self.helper_flags_state_source == "aifa_socket"
        ):
            return self.observed_status.power_saving
        return self.requested_power_saving

    @property
    def is_assumed(self) -> bool:
        """Return True when Home Assistant is still using requested fallback state."""
        return self.effective_state_source != "aifa_socket"

    @property
    def helper_flags_are_assumed(self) -> bool:
        """Return True when helper flags are not yet backed by a trustworthy signal.

        Unlike the main climate state, the AIFA hub does not echo helper
        flags via observed packets, so we can't use observed to confirm them.
        A successful send produces a command_status which we treat as the
        source of truth. Only state with neither command nor observed
        counts as assumed.
        """
        if self.observed_status is not None and self.observed_status.mode == "off":
            return False
        if self.command_status is not None:
            return False
        return self.effective_helper_flags_state_source == "requested_fallback"


