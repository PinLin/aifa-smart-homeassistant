"""Helpers that feed hub status reports into a coordinator built with __new__."""
from __future__ import annotations

import asyncio
import types

from custom_components.aifa_smart.ac import AifaAcStatus


def status(mode: str) -> AifaAcStatus:
    return AifaAcStatus(
        device_code=None,
        mode=mode,
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


def report(coordinator, device_id: str, source: str, *, mode: str = "cool", sub_type: int = 0) -> None:
    """Deliver one report the way a long-lived connection would, without AC targets."""
    spec = types.SimpleNamespace(device_id=device_id, targets=())
    coordinator._apply_classic_observed_status(  # noqa: SLF001
        spec, sub_type, status(mode), source=source
    )


def confirming_sender(coordinator, sent: list[str]):
    """Stub for _async_send_confirmed that records the packet and reports success.

    The report lands in the runtime state as the hub would deliver it, so the
    apply path sees the same observed state as with a real hub.
    """

    async def send(command, confirmation, *, on_written=None):
        sent.append(command.command)
        if on_written is not None:
            on_written()
        if confirmation.mode is not None:
            mode = confirmation.mode
        elif command.command[4:6] == "a1":
            # After the power key the hub reports a mode, never off.
            mode = "cool"
        else:
            mode = "off"
        state = coordinator.get_ac_runtime_state(command.device_id, command.sub_device_id)
        observed = status(mode)
        state.apply_observed_status(observed)
        return "lan", observed

    return send


def report_soon(coordinator, device_id: str, source: str, **kwargs) -> None:
    """Report right after the current write returns, as a real hub would."""
    asyncio.get_running_loop().call_soon(
        lambda: report(coordinator, device_id, source, **kwargs)
    )
