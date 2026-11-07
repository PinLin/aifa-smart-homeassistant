"""Coordinator for AIFA Smart."""
from __future__ import annotations

import asyncio
import logging
import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import (
    ConfigEntryAuthFailed,
    HomeAssistantError,
    ServiceValidationError,
)
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.update_coordinator import (
    TimestampDataUpdateCoordinator,
    UpdateFailed,
)
from homeassistant.util import dt as dt_util

from .ac import (
    CLASSIC_AC_QUERY_PACKET,
    FAN_SPEED_KEY,
    STATUS_KEY_PRESS_MODE,
    STATUS_KEY_PRESS_TEMPERATURE_DOWN,
    STATUS_KEY_PRESS_TEMPERATURE_UP,
    AifaAcKey,
    AifaAcStatus,
    ac_key_requires_power_on,
    build_ac_key_command,
    build_app_status_command,
    build_classic_temperature_key_command,
    build_estimated_capabilities,
    build_mode_key_command,
    build_power_off_command,
    build_power_on_command,
    clamp_target_temperature,
    get_ac_capabilities,
    get_default_mode,
    hub_reports_ac_state,
    hub_requires_mode_keys,
    is_anti_mold_key,
    is_supported_ac_sub_device,
    normalize_ac_mode,
    normalize_catalog_mode,
)
from .api import (
    AifaClassicSensorState,
    AifaDevice,
    AifaDeviceCodeCatalog,
    AifaFunction,
    AifaFunctionCommand,
    AifaMacro,
    AifaSensorSample,
    AifaSmartApiClient,
    AifaSmartAuthError,
    AifaSmartConnectionError,
    AifaSmartError,
    AifaSubDevice,
)
from .classic_listener import (
    AifaClassicSocketListenerSpec,
    ClassicSocketManager,
    _ac_socket_sub_type,
)
from .const import (
    DOMAIN,
    LAN_AUTO_SUPPRESS_SECONDS,
    LAN_CONNECT_TIMEOUT,
    SCAN_INTERVAL,
    TRANSPORT_AUTO,
    TRANSPORT_CLOUD,
    TRANSPORT_LAN,
    transport_from_options,
)
from .device_cache import AifaDeviceCache
from .lan import LanSendResult, LanTransportManager
from .runtime import AifaAcRuntimeState

_LOGGER = logging.getLogger(__name__)

# Minimum spacing between two raw-IR sends to the same target. The AIFA hub
# needs roughly this long to dispatch + reconcile one command before the next
# one races with its observed packet. Empirical floor; tightening below this
# reproduces the dropped-command symptom from the manual sync test plan.
_AC_SEND_MIN_INTERVAL_SECONDS = 1.0

# Threshold for surfacing a repair issue. With the default 60s SCAN_INTERVAL
# this is ~10 minutes of sustained failure — long enough that a real outage
# is happening rather than a transient blip. Distinct from the catalog
# refresh repair issue (catalog.py); this one fires on cloud poll failures
# (UpdateFailed / ConfigEntryAuthFailed) regardless of catalog state.
_FAILURES_BEFORE_ISSUE = 10
ISSUE_POLLING_FAILING = "polling_failing"
# The hub reports its status about 0.4 s after a command and answers a query
# within 0.8 s on the LAN; 1.5 s leaves room for a busy hub or the cloud relay.
AC_CONFIRM_PUSH_TIMEOUT = 1.5
AC_CONFIRM_QUERY_TIMEOUT = 1.5
# Whether the hub reports the AC as off depends on the code: some codes
# report b4 = b6 = 0 after the power-off key, others keep reporting
# b6 = 0x20 and never show off. Only "the hub answered" works for every
# code, so a power off is confirmed by any report. A power on is confirmed
# strictly: a report that still shows off means the hub dropped the key,
# and for a code that never shows off every report shows on.
AC_POWER_OFF_STRICT_CONFIRM = False
# How long a sent but unreported state is preferred over the hub's last
# report before the hub's own report is shown again.
AC_PENDING_RELEASE_SECONDS = 14.0
AC_PENDING_RELEASE_OFF_SECONDS = 26.0


@dataclass(frozen=True, slots=True)
class AcConfirmation:
    """What a hub status report must show for a command to count as delivered."""

    # None accepts a report for any slot (a slot 1 AC's reports are unverified).
    sub_type: int | None
    mode: str | None = None
    target_temperature: int | None = None
    # The fan key cycles the speed, so only a b7 different from this proves
    # the hub took it.
    fan_speed_changed_from: str | None = None
    # Any report that does not show off; the hub reports no setpoint or
    # mode a power key could be checked against.
    power_on: bool = False

    def covers(self, sub_type: int) -> bool:
        return self.sub_type is None or sub_type == self.sub_type

    def matches(self, observed: AifaAcStatus) -> bool:
        if self.fan_speed_changed_from is not None:
            return observed.mode != "off" and observed.fan_mode != self.fan_speed_changed_from
        if self.power_on:
            return observed.mode != "off"
        if self.mode is None:
            return True
        if observed.mode != self.mode:
            return False
        # The hub report has no setpoint (0x00) on the firmware seen so far.
        return (
            self.target_temperature is None
            or observed.target_temperature is None
            or observed.target_temperature == self.target_temperature
        )

    def describe(self) -> str:
        if self.power_on:
            return "on"
        return self.mode or "any"


@dataclass(slots=True)
class _HubReportWaiter:
    """Status reports a hub sent while one command waits for confirmation."""

    reports: list[tuple[str, int, AifaAcStatus]] = field(default_factory=list)
    event: asyncio.Event = field(default_factory=asyncio.Event)


def _confirmation_slot(sub_type: int) -> int | None:
    """Slot whose reports can confirm a command; None accepts any slot.

    Which slot the hub reports after a slot 1 command is not verified.
    """
    return sub_type if sub_type == 0 else None


def _report_source(transport: str) -> str:
    return "lan" if transport == "lan" else "cloud"


async def _async_wait_until(
    waiter: _HubReportWaiter, predicate: Callable[[], bool], timeout: float
) -> bool:
    """Wait until `predicate` holds for what the waiter collected."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while True:
        if predicate():
            return True
        remaining = deadline - loop.time()
        if remaining <= 0:
            return False
        waiter.event.clear()
        try:
            await asyncio.wait_for(waiter.event.wait(), timeout=remaining)
        except asyncio.TimeoutError:
            pass


async def _async_wait_for_hub_report(
    waiter: _HubReportWaiter,
    source: str,
    confirmation: AcConfirmation,
    start: int,
    timeout: float,
) -> tuple[AifaAcStatus | None, bool, AifaAcStatus | None]:
    """Return (matching report, any covered report seen, last covered report).

    A report that does not match keeps the wait going: the hub's own echo
    may still follow an older report or one caused by a remote.
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    index = start
    replied = False
    last: AifaAcStatus | None = None
    while True:
        while index < len(waiter.reports):
            report_source, sub_type, observed = waiter.reports[index]
            index += 1
            if report_source != source or not confirmation.covers(sub_type):
                continue
            replied = True
            last = observed
            if confirmation.matches(observed):
                return observed, True, observed
        remaining = deadline - loop.time()
        if remaining <= 0:
            return None, replied, last
        waiter.event.clear()
        try:
            await asyncio.wait_for(waiter.event.wait(), timeout=remaining)
        except asyncio.TimeoutError:
            pass


def _off_status(device_code: str | None) -> AifaAcStatus:
    return AifaAcStatus(
        device_code=device_code,
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


def _app_status_base(state: AifaAcRuntimeState, device_code: str | None) -> AifaAcStatus:
    """The status i-Ctrl Pro copies into a 15-byte packet: its last On report.

    The app has no packet to copy before its first report (its mode buttons
    and slider are disabled then); this falls back to the setpoint HA last
    asked for with fan auto, swing off and no flags.
    """
    observed = state.observed_status
    if observed is not None and observed.mode != "off":
        return observed
    return AifaAcStatus(
        device_code=device_code,
        mode=state.last_active_hvac_mode or get_default_mode(device_code),
        target_temperature=state.requested_target_temperature,
        timer_ir_value=0,
        fan_mode="auto",
        swing_mode="off",
        turbo=False,
        sleep=False,
        power_saving=False,
        packet_byte_12=0,
        packet_byte_13=0,
    )


@dataclass(slots=True)
class AifaAccountData:
    """Snapshot of AIFA Smart account data."""

    devices: dict[str, AifaDevice]
    macros: list[AifaMacro]
    fetched_at: datetime


class AifaSmartCoordinator(TimestampDataUpdateCoordinator[AifaAccountData]):
    """Coordinate AIFA Smart data refreshes."""

    _transport: str = TRANSPORT_CLOUD
    _lan_manager: LanTransportManager | None = None
    _device_cache: AifaDeviceCache | None = None
    # True from an offline start until the first successful cloud refresh.
    running_on_cache: bool = False
    # Set on unload; a refresh that is not an entry task may still finish.
    _closed: bool = False

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        client: AifaSmartApiClient,
    ) -> None:
        self.entry = entry
        self.client = client
        self._device_code_catalogs: dict[str, AifaDeviceCodeCatalog] = {}
        self._ac_runtime_states: dict[tuple[str, str], AifaAcRuntimeState] = {}
        self._ac_pending_release_tasks: dict[tuple[str, str], asyncio.Task[None]] = {}
        # Per-hub lock + per-(device, sub_device) last-send timestamp to
        # serialize commands and enforce a min interval between IR sends.
        # Without these, two HA service calls within ~1s could both send IR
        # before the hub reconciles the first command, racing on runtime state
        # and producing the "dropped command" symptom (see runtime.py
        # apply_observed_status stale-observed guard for the other half of
        # the fix).
        self._ac_apply_locks: dict[str, asyncio.Lock] = {}
        self._ac_last_send_at: dict[tuple[str, str | None], float] = {}
        self._hub_report_waiters: dict[str, list[_HubReportWaiter]] = {}
        self._lan_queried_at: dict[str, datetime | None] = {}
        self._consecutive_failures = 0
        # The base class registers async_shutdown on the entry's unload.
        super().__init__(
            hass,
            logger=_LOGGER,
            config_entry=entry,
            name=f"{DOMAIN}_{entry.entry_id}",
            update_interval=timedelta(seconds=SCAN_INTERVAL),
        )

        self._classic_listener = ClassicSocketManager(
            hass=hass,
            client=client,
            on_observed_status=self._apply_cloud_stream_status,
            on_sensor_state=self._apply_classic_sensor_state,
            on_data_changed=self.async_update_listeners,
            on_notify_full_refresh=self.async_request_refresh,
        )
        self._transport = transport_from_options(entry.options)
        self._lan_failed_at: dict[str, float] = {}
        self._transport_counters: dict[str, int | str | None] = {
            "sent_lan": 0,
            "sent_cloud": 0,
            "auto_fallbacks": 0,
            "lan_write_failures": 0,
            "last_transport": None,
        }
        if self._transport != TRANSPORT_CLOUD:
            self._lan_manager = LanTransportManager(
                hass=hass,
                on_observed_status=self._apply_lan_observed_status,
                on_data_changed=self.async_update_listeners,
                on_link_state_changed=self._on_lan_link_state_changed,
            )
            self._device_cache = AifaDeviceCache(hass, entry.entry_id)

    @property
    def transport(self) -> str:
        """Transport selected in the entry options."""
        return self._transport

    @property
    def lan_manager(self) -> LanTransportManager | None:
        """LAN transport manager, present only for the lan and auto modes."""
        return self._lan_manager

    def transport_diagnostics(self) -> dict[str, object]:
        """Transport state for diagnostics; carries no MAC or credential."""
        lan = self._lan_manager
        listener = self._classic_listener
        return {
            "mode": self._transport,
            "running_on_cache": self.running_on_cache,
            "broadcast_addresses": list(lan.broadcast_addresses) if lan is not None else [],
            "lan": lan.snapshot() if lan is not None else {},
            "cloud_listener": {
                device_id: {
                    "listening": listener.has_active_listener(device_id),
                    "writer": listener.has_active_writer(device_id),
                }
                for device_id in listener.specs
            },
            "counters": dict(self._transport_counters),
        }

    def is_control_available(self, device_id: str) -> bool:
        """Availability of entities that send commands to the hub.

        Cloud mode keeps the poll-based rule. LAN mode follows the link with a
        grace period so a reconnect does not flap entities. Auto mode is usable
        while either path is.
        """
        if self._transport == TRANSPORT_CLOUD:
            return self.last_update_success
        lan_available = self._lan_manager is not None and self._lan_manager.is_available(
            device_id
        )
        if self._transport == TRANSPORT_LAN:
            return lan_available
        return lan_available or self.last_update_success

    def _on_lan_link_state_changed(self) -> None:
        self.async_update_listeners()
        lan_manager = self._lan_manager
        account = self.data
        if lan_manager is None or account is None:
            return
        for device in account.devices.values():
            link = lan_manager.links.get(device.id)
            if link is None or not link.connected:
                continue
            # Link-state callbacks also fire on drops and grace expiry; the
            # connect timestamp tells a new connection from those.
            if self._lan_queried_at.get(device.id) == link.connected_at:
                continue
            self._lan_queried_at[device.id] = link.connected_at
            if any(
                is_supported_ac_sub_device(sub.type, sub.device_code)
                for sub in device.sub_devices
            ):
                # Like i-Ctrl Pro on every connect: state changed while the
                # link was down is never pushed, and a LAN-only start has no
                # cloud poll to ask for it.
                self.hass.async_create_background_task(
                    lan_manager.async_request_status(device.id),
                    name=f"{DOMAIN}_lan_connect_query_{device.id}",
                )

    async def async_shutdown(self) -> None:
        """Cancel background classic-socket and pending-release tasks."""
        self._closed = True
        await super().async_shutdown()
        await self._classic_listener.shutdown()
        if self._lan_manager is not None:
            await self._lan_manager.shutdown()
        tasks = [
            task
            for task in list(self._ac_pending_release_tasks.values())
            if task is not None and not task.done()
        ]
        self._ac_pending_release_tasks.clear()
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    def async_sync_background_tasks(self) -> None:
        """Sync long-lived listener tasks against the latest coordinator snapshot."""
        if self.data is None:
            return
        self._classic_listener.sync_specs(self.data.devices)
        if self._lan_manager is not None:
            self._lan_manager.sync_specs(self.data.devices)

    def get_device(self, device_id: str) -> AifaDevice | None:
        """Return a single device by ID."""
        if self.data is None:
            return None
        return self.data.devices.get(device_id)

    def get_sub_device(self, device_id: str, sub_device_id: str) -> AifaSubDevice | None:
        """Return a single sub-device by parent device and id."""
        device = self.get_device(device_id)
        if device is None:
            return None
        return device.get_sub_device(sub_device_id)

    async def _async_send_unconfirmed(self, command: AifaFunctionCommand) -> dict:
        """Write one packet like i-Ctrl Pro sends a macro step: no report awaited.

        A macro step may be any learned IR code that draws no report. Raises
        only when nothing could be written or a write failed, never resends;
        the cloud path has no REST fallback, as in the app.
        """
        # A report during an AC command's wait must not come from this packet.
        async with self._ac_apply_lock(command.device_id):
            transport = await self._async_send_raw_command(command)
        return self._send_response(command, transport)

    @staticmethod
    def _send_response(command: AifaFunctionCommand, transport: str) -> dict:
        response: dict = {
            "deviceId": command.device_id,
            "status": "success",
            "transport": transport,
        }
        if command.sub_device_id is not None:
            response["subDeviceId"] = command.sub_device_id
        return response

    async def _async_send_raw_command(self, command: AifaFunctionCommand) -> str:
        """Send one raw packet over the configured transport and name it."""
        if self._transport == TRANSPORT_LAN:
            return await self._async_send_raw_command_lan(command)
        if self._transport == TRANSPORT_AUTO and self._lan_manager is not None:
            return await self._async_send_raw_command_auto(command, self._lan_manager)
        return await self._async_send_raw_command_cloud(command)

    async def _async_send_confirmed(
        self,
        command: AifaFunctionCommand,
        confirmation: AcConfirmation,
        *,
        on_written: Callable[[], None] | None = None,
    ) -> tuple[str, AifaAcStatus]:
        """Send one packet and return once a hub status report confirms it.

        Callers hold the hub's apply lock, so a report belongs either to this
        command or to an outside source such as a remote; the content check
        tells those apart wherever the report carries the changed field.
        Only reports read from the connection that carried the command
        count, because a half-open connection still accepts writes.
        A report that does not match raises without a resend, like i-Ctrl
        Pro, which never repeats a key; the entity shows that report.
        `on_written` runs once the packet is out, before the reports it draws.
        """
        device_id = command.device_id
        waiter = _HubReportWaiter()
        waiters = self._hub_report_waiters.setdefault(device_id, [])
        waiters.append(waiter)
        try:
            transport = await self._async_send_raw_command(command)
            if on_written is not None:
                on_written()
            # Counted from after the write: a report that arrived while a
            # LAN link was still connecting predates the command.
            start = len(waiter.reports)
            source = _report_source(transport)
            matched, replied, last_report = await _async_wait_for_hub_report(
                waiter,
                source,
                confirmation,
                start,
                AC_CONFIRM_PUSH_TIMEOUT,
            )
            if matched is None and await self._async_query_hub_status(
                device_id, transport
            ):
                matched, replied, last_report = await _async_wait_for_hub_report(
                    waiter, source, confirmation, start, AC_CONFIRM_QUERY_TIMEOUT
                )
            if matched is not None:
                return transport, matched
            if not replied:
                # Silence is not a dead link: after some keys the hub ignores
                # queries for a while yet still takes keys, so the link is
                # left to keepalive, write failures and the LAN idle check.
                _LOGGER.warning(
                    "AIFA hub %s sent no status after a command over %s",
                    device_id,
                    transport,
                )
                raise HomeAssistantError(
                    translation_domain=DOMAIN,
                    translation_key="hub_no_response",
                    translation_placeholders={"device_id": device_id},
                )
            if confirmation.fan_speed_changed_from is not None:
                raise HomeAssistantError(
                    translation_domain=DOMAIN,
                    translation_key="ac_fan_speed_unchanged",
                    translation_placeholders={"device_id": device_id},
                )
            if confirmation.power_on:
                raise HomeAssistantError(
                    translation_domain=DOMAIN,
                    translation_key="ac_power_on_not_reported",
                    translation_placeholders={"device_id": device_id},
                )
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="ac_state_mismatch",
                translation_placeholders={
                    "device_id": device_id,
                    "expected": confirmation.describe(),
                    "reported": last_report.mode if last_report is not None else "",
                },
            )
        finally:
            waiters.remove(waiter)
            if not waiters and self._hub_report_waiters.get(device_id) is waiters:
                del self._hub_report_waiters[device_id]

    async def _async_query_hub_status(self, device_id: str, transport: str) -> bool:
        """Ask the hub for its status on the connection that carried the command."""
        try:
            if transport == "lan":
                lan_manager = self._lan_manager
                return lan_manager is not None and await lan_manager.async_request_status(
                    device_id
                )
            return await self._classic_listener.async_send_packet(
                device_id, CLASSIC_AC_QUERY_PACKET
            )
        except (OSError, RuntimeError) as err:
            _LOGGER.debug("AIFA status query to %s failed: %s", device_id, err)
            return False

    async def _async_send_raw_command_cloud(self, command: AifaFunctionCommand) -> str:
        if not await self._async_write_cloud(command.device_id, command.command):
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="cloud_unavailable",
                translation_placeholders={"device_id": command.device_id},
            )
        return "classic_tls"

    async def _async_write_cloud(self, device_id: str, packet: str) -> bool:
        """False when nothing was written; a failed write may have left bytes."""
        try:
            return await self._classic_listener.async_send_packet(device_id, packet)
        except (OSError, RuntimeError) as err:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="cloud_write_failed",
                translation_placeholders={"device_id": device_id},
            ) from err

    async def _async_send_over_lan(self, command: AifaFunctionCommand) -> LanSendResult:
        if self._lan_manager is None:
            return LanSendResult.NOT_CONNECTED
        result = await self._lan_manager.async_send_packet(
            command.device_id, command.command, connect_timeout=LAN_CONNECT_TIMEOUT
        )
        if result is LanSendResult.SENT:
            self._count_transport("sent_lan", last="lan")
        elif result is LanSendResult.WRITE_FAILED:
            self._count_transport("lan_write_failures")
        return result

    async def _async_send_raw_command_lan(self, command: AifaFunctionCommand) -> str:
        """LAN only: never touch the cloud, even when the hub is unreachable."""
        result = await self._async_send_over_lan(command)
        if result is LanSendResult.SENT:
            return "lan"
        if result is LanSendResult.WRITE_FAILED:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="lan_write_failed",
                translation_placeholders={"device_id": command.device_id},
            )
        raise HomeAssistantError(
            translation_domain=DOMAIN,
            translation_key="lan_unavailable",
            translation_placeholders={"device_id": command.device_id},
        )

    async def _async_send_raw_command_auto(
        self, command: AifaFunctionCommand, lan_manager: LanTransportManager
    ) -> str:
        """Prefer LAN; use the cloud only when nothing was written locally.

        A failed write may already have reached the hub, and relative keys
        (temperature +/-, fan cycling) would step twice if resent, so
        WRITE_FAILED never falls back.
        """
        device_id = command.device_id
        failed_at = self._lan_failed_at.get(device_id)
        suppressed = (
            failed_at is not None
            and time.monotonic() - failed_at < LAN_AUTO_SUPPRESS_SECONDS
            and not lan_manager.is_connected(device_id)
        )
        if not suppressed:
            result = await self._async_send_over_lan(command)
            if result is LanSendResult.SENT:
                self._lan_failed_at.pop(device_id, None)
                return "lan"
            self._lan_failed_at[device_id] = time.monotonic()
            if result is LanSendResult.WRITE_FAILED:
                raise HomeAssistantError(
                    translation_domain=DOMAIN,
                    translation_key="lan_write_failed",
                    translation_placeholders={"device_id": device_id},
                )
            self._count_transport("auto_fallbacks")

        if not await self._async_write_cloud(device_id, command.command):
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="no_transport",
                translation_placeholders={"device_id": device_id},
            )
        self._count_transport("sent_cloud", last="classic_tls")
        return "classic_tls"

    def _count_transport(self, key: str, *, last: str | None = None) -> None:
        counters = self._transport_counters
        counters[key] = int(counters[key] or 0) + 1
        if last is not None:
            counters["last_transport"] = last

    async def _async_refresh_after_send(self) -> None:
        """Refresh after a command without making LAN control wait on the cloud.

        The debouncer runs the first refresh inline, so awaiting it would hold
        the apply lock for the whole cloud round trip (30 s when unreachable).
        """
        if self._transport == TRANSPORT_CLOUD:
            await self.async_request_refresh()
            return
        self.hass.async_create_background_task(
            self.async_request_refresh(),
            name=f"{DOMAIN}_refresh_after_send",
        )

    def get_macro(self, macro_id: str) -> AifaMacro | None:
        """Look up a saved macro by id from the latest account snapshot."""
        account = self.data
        if account is None:
            return None
        for macro in account.macros:
            if macro.id == macro_id:
                return macro
        return None

    async def async_execute_macro(self, macro_id: str) -> list[dict]:
        """Run all commands of a saved macro in order, honoring per-step delays.

        A step goes out once both the transports' queue spacing and the
        macro's own delay have passed, without waiting for the hub to report.
        """
        macro = self.get_macro(macro_id)
        if macro is None:
            raise AifaSmartError(f"Macro {macro_id} not found")
        if not macro.commands:
            return []

        ordered = sorted(
            macro.commands, key=lambda command: command.order_no or 0
        )
        responses: list[dict] = []
        last_index = len(ordered) - 1
        for index, command in enumerate(ordered):
            responses.append(
                await self._async_send_unconfirmed(
                    AifaFunctionCommand(
                        id=f"macro-{macro.id}-{index}",
                        device_id=str(command.device_id),
                        sub_device_id=command.sub_device_id,
                        command=command.command,
                    )
                )
            )
            if index < last_index and command.delay and command.delay > 0:
                await asyncio.sleep(command.delay / 1000)

        await self._async_refresh_after_send()
        return responses

    def _prune_stale_runtime_state(self, devices: dict[str, AifaDevice]) -> None:
        """Drop runtime/cache entries for sub-devices no longer in the snapshot.

        Without this, removing a sub-device in the AIFA app leaves stale keys
        in `_ac_runtime_states`, `_ac_apply_locks`, `_ac_last_send_at`,
        `_device_code_catalogs`, and orphan tasks in
        `_ac_pending_release_tasks` for the lifetime of the integration.
        """
        live_pairs: set[tuple[str, str]] = set()
        live_sub_device_ids: set[str] = set()
        for device in devices.values():
            for sub_device in device.sub_devices:
                live_pairs.add((device.id, sub_device.id))
                live_sub_device_ids.add(sub_device.id)

        for key in list(self._ac_runtime_states.keys()):
            if key not in live_pairs:
                self._ac_runtime_states.pop(key, None)
        for device_id, lock in list(self._ac_apply_locks.items()):
            # A hub missing from one snapshot may still have a command in flight.
            if device_id not in devices and not lock.locked():
                self._ac_apply_locks.pop(device_id, None)
        for key in list(self._ac_last_send_at.keys()):
            device_id, sub_device_id = key
            if sub_device_id is None:
                if device_id not in devices:
                    self._ac_last_send_at.pop(key, None)
            elif (device_id, sub_device_id) not in live_pairs:
                self._ac_last_send_at.pop(key, None)
        for sub_device_id in list(self._device_code_catalogs.keys()):
            if sub_device_id not in live_sub_device_ids:
                self._device_code_catalogs.pop(sub_device_id, None)
        for key in list(self._ac_pending_release_tasks.keys()):
            if key in live_pairs:
                continue
            task = self._ac_pending_release_tasks.pop(key, None)
            if task is not None and not task.done():
                task.cancel()

    def get_ac_runtime_state(
        self,
        device_id: str,
        sub_device_id: str,
        *,
        device_code: str | None = None,
    ) -> AifaAcRuntimeState:
        """Return shared requested/observed state for one AC sub-device."""
        key = (device_id, sub_device_id)
        state = self._ac_runtime_states.get(key)
        if state is None:
            state = AifaAcRuntimeState.create(device_code)
            self._ac_runtime_states[key] = state
        return state

    async def async_apply_ac_state(
        self,
        device_id: str,
        sub_device_id: str,
        *,
        hvac_mode: str | None = None,
        target_temperature: int | None = None,
        device_code: str | None = None,
    ) -> str:
        """Send one AC state change and return once the hub has confirmed it.

        `hvac_mode="on"` sends only the power key, like the app's On button;
        a target temperature is set in the mode the AC is showing.
        Raises HomeAssistantError when the hub does not answer or reports a
        different state; the command is not resent and the entity shows the
        hub's last report.
        """
        lock_key = (device_id, sub_device_id)
        async with self._ac_apply_lock(device_id):
            await self._async_throttle_ac_send(lock_key)
            state = self.get_ac_runtime_state(
                device_id, sub_device_id, device_code=device_code
            )
            # Read before pending is cleared: a pending power off must still
            # count as off when deciding whether the power key is needed.
            shown_hvac_mode = state.exposed_hvac_mode

            def supersede() -> None:
                # A written command supersedes any state still pending
                # release; reports during the wait must apply as they come.
                # One that never went out leaves the shown state alone.
                self._cancel_ac_pending_release(lock_key)
                state.clear_pending_command_status()

            try:
                return await self._async_apply_ac_state_locked(
                    device_id,
                    sub_device_id,
                    hvac_mode=hvac_mode,
                    target_temperature=target_temperature,
                    device_code=device_code,
                    shown_hvac_mode=shown_hvac_mode,
                    on_first_write=supersede,
                )
            except Exception:
                self.async_update_listeners()
                raise

    async def async_press_classic_temperature_key(
        self, device_id: str, sub_device_id: str, *, up: bool
    ) -> None:
        """Send one +/- temperature key press to a classic-temperature AC."""
        sub_device = self.get_sub_device(device_id, sub_device_id)
        if sub_device is None:
            raise AifaSmartError(f"Sub-device {sub_device_id} not found")
        sub_type = _ac_socket_sub_type(sub_device)
        # The status report carries no setpoint, so any report from the
        # slot confirms the press.
        await self._async_send_ac_key(
            device_id,
            sub_device_id,
            "classic-temperature-key",
            build_classic_temperature_key_command(sub_type, up=up),
            lambda: AcConfirmation(sub_type=_confirmation_slot(sub_type)),
        )

    async def async_press_ac_key(
        self, device_id: str, sub_device_id: str, key: AifaAcKey
    ) -> None:
        """Send one i-Ctrl Pro AC page key."""
        sub_device = self.get_sub_device(device_id, sub_device_id)
        if sub_device is None:
            raise AifaSmartError(f"Sub-device {sub_device_id} not found")
        state = self.get_ac_runtime_state(
            device_id, sub_device_id, device_code=sub_device.device_code
        )
        shown_mode = state.exposed_hvac_mode
        if shown_mode == "off" and ac_key_requires_power_on(key, sub_device.device_code):
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="ac_key_requires_power_on",
            )
        # The app swaps the anti-mold button for a notice in a mode the
        # Remote Config list does not allow.
        if is_anti_mold_key(key) and normalize_catalog_mode(shown_mode) not in (
            get_ac_capabilities(sub_device.device_code).anti_mold_modes
        ):
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="ac_anti_mold_mode_unsupported",
            )
        sub_type = _ac_socket_sub_type(sub_device)
        reports_state = sub_type == 0 and hub_reports_ac_state(sub_device.device_code)

        def written() -> None:
            if state.power_off_held:
                # A key the app allows while off may change the report; that
                # does not mean the AC turned on.
                state.hold_power_off(None)

        def confirmation() -> AcConfirmation:
            # Read under the hub lock so the baseline is the report right
            # before this press. Without a report there is nothing to compare.
            observed = state.observed_status
            if (
                key.code == FAN_SPEED_KEY
                and reports_state
                and observed is not None
                and observed.fan_mode is not None
            ):
                return AcConfirmation(
                    sub_type=sub_type, fan_speed_changed_from=observed.fan_mode
                )
            return AcConfirmation(sub_type=_confirmation_slot(sub_type))

        await self._async_send_ac_key(
            device_id,
            sub_device_id,
            f"ac-key-{key.key}",
            build_ac_key_command(key, sub_type),
            confirmation,
            on_written=written,
        )

    async def _async_send_ac_key(
        self,
        device_id: str,
        sub_device_id: str,
        command_id: str,
        packet: str,
        confirmation: Callable[[], AcConfirmation],
        *,
        on_written: Callable[[], None] | None = None,
    ) -> None:
        lock_key = (device_id, sub_device_id)
        async with self._ac_apply_lock(device_id):
            await self._async_throttle_ac_send(lock_key)
            await self._async_send_confirmed(
                AifaFunctionCommand(
                    id=command_id,
                    device_id=device_id,
                    sub_device_id=sub_device_id,
                    command=packet,
                ),
                confirmation(),
                on_written=on_written,
            )

    def _ac_apply_lock(self, device_id: str) -> asyncio.Lock:
        """One lock per hub: a report cannot tell which of two commands it answers."""
        lock = self._ac_apply_locks.get(device_id)
        if lock is None:
            lock = asyncio.Lock()
            self._ac_apply_locks[device_id] = lock
        return lock

    async def _async_throttle_ac_send(
        self, key: tuple[str, str | None]
    ) -> None:
        """Sleep until at least _AC_SEND_MIN_INTERVAL_SECONDS since last send for key."""
        loop = asyncio.get_running_loop()
        last = self._ac_last_send_at.get(key)
        if last is not None:
            elapsed = loop.time() - last
            remaining = _AC_SEND_MIN_INTERVAL_SECONDS - elapsed
            if remaining > 0:
                await asyncio.sleep(remaining)
        self._ac_last_send_at[key] = loop.time()

    async def _async_apply_ac_state_locked(
        self,
        device_id: str,
        sub_device_id: str,
        *,
        hvac_mode: str | None = None,
        target_temperature: int | None = None,
        device_code: str | None = None,
        shown_hvac_mode: str | None = None,
        on_first_write: Callable[[], None] | None = None,
    ) -> str:
        state = self.get_ac_runtime_state(
            device_id,
            sub_device_id,
            device_code=device_code,
        )
        lock_key = (device_id, sub_device_id)
        hub = self.get_device(device_id)
        sub_device = self.get_sub_device(device_id, sub_device_id)
        sub_type = 0 if sub_device is None else _ac_socket_sub_type(sub_device)
        confirm_slot = _confirmation_slot(sub_type)
        device_code = device_code or state.device_code
        # Only slot 0 echoes are verified; the power and status packets carry
        # no slot bit, so a slot 1 AC's report slot is unknown.
        reports_state = sub_type == 0 and hub_reports_ac_state(device_code)
        current_mode = shown_hvac_mode or state.exposed_hvac_mode
        if (
            hvac_mode not in ("on", "off")
            and reports_state
            and state.observed_status is None
            and state.restored_hvac_mode is not None
        ):
            # The mode restored after a restart is not a report: a 15-byte
            # packet built without one would reset fan, swing and flags.
            await self._async_fetch_first_report(device_id, sub_type)
            current_mode = state.exposed_hvac_mode

        if hvac_mode == "on":
            return await self._async_power_on_ac_locked(
                device_id,
                sub_device_id,
                state,
                device_code,
                confirm_slot,
                strict=reports_state,
                on_written=on_first_write,
            )

        resolved_mode = (
            current_mode
            if hvac_mode is None
            else normalize_ac_mode(hvac_mode.lower())
        )
        if resolved_mode == "off" and target_temperature is None:
            command = build_power_off_command(device_code=device_code)
            strict = AC_POWER_OFF_STRICT_CONFIRM and reports_state
            _, report = await self._async_send_confirmed(
                AifaFunctionCommand(
                    id="manual-off",
                    device_id=device_id,
                    sub_device_id=sub_device_id,
                    command=command,
                ),
                AcConfirmation(
                    sub_type=confirm_slot,
                    mode="off" if strict else None,
                ),
                on_written=on_first_write,
            )
            state.requested_hvac_mode = "off"
            state.last_power_command = command
            state.last_status_command = None
            state.apply_sent_command_status(_off_status(device_code))
            state.release_power_off()
            if not strict and report.mode != "off":
                # The hub keeps repeating this report after a power off it
                # never reports, so it cannot settle the state; only a
                # different report can. A report confirming a slot 1 command
                # may come from slot 0, so that slot waits for its own.
                state.hold_power_off(report if confirm_slot is not None else None)
                state.command_status_pending = False
            self.async_update_listeners()
            if state.command_status_pending:
                self._schedule_ac_pending_release(
                    device_id,
                    sub_device_id,
                    expected_mode="off",
                )
            return command

        def confirmation(*, mode: str, temperature: int | None = None) -> AcConfirmation:
            if not reports_state:
                return AcConfirmation(sub_type=confirm_slot)
            return AcConfirmation(
                sub_type=sub_type,
                mode=mode,
                target_temperature=temperature,
            )

        power_on_command: str | None = None
        if target_temperature is None:
            if current_mode == "off":
                # The app's mode buttons only work while on, so press its On
                # button first and build on the report that follows.
                power_on_command = await self._async_send_power_on(
                    device_id,
                    sub_device_id,
                    state,
                    device_code,
                    confirm_slot,
                    strict=reports_state,
                    on_written=on_first_write,
                )
                on_first_write = None
                await self._async_throttle_ac_send(lock_key)
            base = _app_status_base(state, device_code)
            if hub is not None and hub_requires_mode_keys(hub.version, hub.subversion):
                command_id = "manual-mode-key"
                packet = build_mode_key_command(resolved_mode, sub_type)
                # A mode key leaves the AC's own setpoint alone.
                sent_status = replace(base, mode=resolved_mode, target_temperature=None)
            else:
                command_id = "manual-status"
                sent_status = replace(base, mode=resolved_mode)
                packet = build_app_status_command(
                    sent_status,
                    key_press=STATUS_KEY_PRESS_MODE,
                    sub_type=sub_type,
                    device_code=device_code,
                )
            packet_confirmation = confirmation(mode=resolved_mode)
        else:
            base = _app_status_base(state, device_code)
            # The app's slider is disabled in these states.
            if (
                resolved_mode in ("off", "auto", "fan_only")
                or (resolved_mode == "dry" and not base.target_temperature)
            ):
                raise ServiceValidationError(
                    translation_domain=DOMAIN,
                    translation_key="ac_temperature_unavailable",
                )
            temperature = clamp_target_temperature(target_temperature, device_code=device_code)
            key_press = (
                STATUS_KEY_PRESS_TEMPERATURE_UP
                if temperature > (base.target_temperature or 0)
                else STATUS_KEY_PRESS_TEMPERATURE_DOWN
            )
            command_id = "manual-status"
            sent_status = replace(base, mode=resolved_mode, target_temperature=temperature)
            packet = build_app_status_command(
                sent_status,
                key_press=key_press,
                sub_type=sub_type,
                device_code=device_code,
            )
            packet_confirmation = confirmation(mode=resolved_mode, temperature=temperature)

        await self._async_send_confirmed(
            AifaFunctionCommand(
                id=command_id,
                device_id=device_id,
                sub_device_id=sub_device_id,
                command=packet,
            ),
            packet_confirmation,
            on_written=on_first_write,
        )

        state.requested_hvac_mode = resolved_mode
        if sent_status.target_temperature is not None:
            state.requested_target_temperature = clamp_target_temperature(
                sent_status.target_temperature, device_code=device_code
            )
        state.last_active_hvac_mode = resolved_mode
        state.last_status_command = packet
        state.last_power_command = power_on_command
        if reports_state:
            state.apply_confirmed_command_status(sent_status)
        else:
            state.apply_sent_command_status(sent_status)
        self.async_update_listeners()
        if state.command_status_pending:
            self._schedule_ac_pending_release(
                device_id,
                sub_device_id,
                expected_mode=resolved_mode,
            )
        return packet

    async def _async_fetch_first_report(self, device_id: str, sub_type: int) -> None:
        """Ask the hub for the report a restored state lacks; raise without one."""
        waiter = _HubReportWaiter()
        waiters = self._hub_report_waiters.setdefault(device_id, [])
        waiters.append(waiter)
        try:
            transport = await self._async_send_raw_command(
                AifaFunctionCommand(
                    id="status-query",
                    device_id=device_id,
                    sub_device_id=None,
                    command=CLASSIC_AC_QUERY_PACKET,
                )
            )
            source = _report_source(transport)
            if await _async_wait_until(
                waiter,
                lambda: any(
                    report_source == source and slot == sub_type
                    for report_source, slot, _ in waiter.reports
                ),
                AC_CONFIRM_QUERY_TIMEOUT,
            ):
                return
        finally:
            waiters.remove(waiter)
            if not waiters and self._hub_report_waiters.get(device_id) is waiters:
                del self._hub_report_waiters[device_id]
        raise HomeAssistantError(
            translation_domain=DOMAIN,
            translation_key="ac_state_unknown",
            translation_placeholders={"device_id": device_id},
        )

    async def _async_send_power_on(
        self,
        device_id: str,
        sub_device_id: str,
        state: AifaAcRuntimeState,
        device_code: str | None,
        confirm_slot: int | None,
        *,
        strict: bool,
        on_written: Callable[[], None] | None = None,
    ) -> str:
        """Send the app's On key; the report it draws must apply as it comes.

        With `strict`, a hub that still reports off raises
        ac_power_on_not_reported, the key is not resent, and the AC stays
        off, including a held power off.
        """
        command = build_power_on_command(device_code=device_code)
        held = state.power_off_held

        def written() -> None:
            # Only a key that went out can have turned the AC on; until then
            # the stale report the hub repeats must keep showing off.
            state.release_power_off()
            if on_written is not None:
                on_written()

        try:
            await self._async_send_confirmed(
                AifaFunctionCommand(
                    id="manual-power-on",
                    device_id=device_id,
                    sub_device_id=sub_device_id,
                    command=command,
                ),
                AcConfirmation(sub_type=confirm_slot, power_on=strict),
                on_written=written,
            )
        except HomeAssistantError as err:
            if (
                getattr(err, "translation_key", None) == "ac_power_on_not_reported"
                and held
            ):
                # Held against the off report just seen: the hub repeating it
                # keeps the AC off, and any other report, such as a power on
                # from a remote, is shown. The report the hold was set
                # against before could be the one a real power on brings.
                state.hold_power_off(state.observed_status)
            raise
        return command

    async def _async_power_on_ac_locked(
        self,
        device_id: str,
        sub_device_id: str,
        state: AifaAcRuntimeState,
        device_code: str | None,
        confirm_slot: int | None,
        *,
        strict: bool,
        on_written: Callable[[], None] | None = None,
    ) -> str:
        command = await self._async_send_power_on(
            device_id,
            sub_device_id,
            state,
            device_code,
            confirm_slot,
            strict=strict,
            on_written=on_written,
        )
        state.last_power_command = command
        observed = state.observed_status
        if observed is None or observed.mode == "off":
            # Nothing reported the mode the AC came back in; show the last one.
            # Not pending, so the next report, whatever it says, replaces it.
            mode = state.last_active_hvac_mode or get_default_mode(device_code)
            state.requested_hvac_mode = mode
            state.apply_sent_command_status(
                replace(_app_status_base(state, device_code), mode=mode)
            )
            state.command_status_pending = False
        else:
            state.requested_hvac_mode = observed.mode
            state.command_status = None
            state.state_source = "aifa_socket"
        self.async_update_listeners()
        return command

    async def _async_update_data(self) -> AifaAccountData:
        """Fetch device data from AIFA Smart."""
        try:
            result = await self._async_do_update()
        except (UpdateFailed, ConfigEntryAuthFailed):
            self._consecutive_failures += 1
            if self._consecutive_failures >= _FAILURES_BEFORE_ISSUE:
                self._raise_polling_issue()
            raise
        self._consecutive_failures = 0
        self._clear_polling_issue()
        self.running_on_cache = False
        if not self._closed:
            # The next entry owns the cache once this one has unloaded.
            await self._async_save_device_cache(result)
        return result

    async def _async_save_device_cache(self, result: AifaAccountData) -> None:
        if self._device_cache is None:
            return
        try:
            await self._device_cache.async_save(result.devices, result.macros)
        except Exception as err:  # noqa: BLE001 - a disk problem must not fail the poll
            _LOGGER.warning("Unable to write the AIFA device cache: %s", err)

    async def async_start_from_cache(self) -> bool:
        """Seed data from the device cache after a failed first refresh.

        last_update_success stays False, so cloud-bound entities remain
        unavailable while LAN control works; polling keeps retrying the cloud.
        """
        if self._device_cache is None:
            return False
        cached = await self._device_cache.async_load()
        if cached is None:
            return False
        devices, macros, saved_at = cached
        for device in devices.values():
            for sub_device in device.sub_devices:
                self._async_refresh_runtime_state(device.id, sub_device)
        self.data = AifaAccountData(
            devices=devices,
            macros=macros,
            fetched_at=saved_at or dt_util.utcnow(),
        )
        self.running_on_cache = True
        return True

    def _raise_polling_issue(self) -> None:
        """Surface a Repairs entry once sustained polling failure crosses the threshold."""
        ir.async_create_issue(
            self.hass,
            DOMAIN,
            f"{ISSUE_POLLING_FAILING}_{self.entry.entry_id}",
            is_fixable=False,
            severity=ir.IssueSeverity.ERROR,
            translation_key=ISSUE_POLLING_FAILING,
            translation_placeholders={"entry_id": self.entry.entry_id},
        )

    def _clear_polling_issue(self) -> None:
        """Drop the Repairs entry once the next refresh succeeds."""
        ir.async_delete_issue(
            self.hass, DOMAIN, f"{ISSUE_POLLING_FAILING}_{self.entry.entry_id}"
        )

    async def _async_do_update(self) -> AifaAccountData:
        """Fetch device data from AIFA Smart."""
        try:
            devices = await self.client.async_get_devices()
            macros = await self.client.async_get_macros()
            enriched: dict[str, AifaDevice] = {}
            for device in devices:
                functions_by_id: OrderedDict[str, AifaFunction] = OrderedDict()
                for sub_device in device.sub_devices:
                    functions = await self.client.async_get_functions(sub_device.id)
                    sub_device.functions = functions
                    await self._async_enrich_sub_device(sub_device)
                    self._async_refresh_runtime_state(device.id, sub_device)
                    for function in functions:
                        functions_by_id[function.id] = function
                device.functions = list(functions_by_id.values())

                enriched[device.id] = device

            if not self._closed:
                self._classic_listener.sync_specs(enriched)
                if self._lan_manager is not None:
                    self._lan_manager.sync_specs(enriched)
            self._prune_stale_runtime_state(enriched)
            return AifaAccountData(
                devices=enriched,
                macros=macros,
                fetched_at=dt_util.utcnow(),
            )
        except AifaSmartAuthError as err:
            raise ConfigEntryAuthFailed(f"Authentication failed: {err}") from err
        except AifaSmartConnectionError as err:
            raise UpdateFailed(f"Connection failed: {err}") from err
        except AifaSmartError as err:
            raise UpdateFailed(str(err)) from err

    async def _async_enrich_sub_device(self, sub_device: AifaSubDevice) -> None:
        """Attach selected device-code metadata and AC capability hints."""
        catalog = self._device_code_catalogs.get(sub_device.id)
        if catalog is None:
            catalog = await self.client.async_get_sub_device_device_codes(sub_device.id)
            self._device_code_catalogs[sub_device.id] = catalog

        sub_device.available_brand_count = len(catalog.brands)
        sub_device.available_device_code_count = len(catalog.device_codes)

        selected = catalog.find_by_code(sub_device.device_code)
        if selected is not None:
            sub_device.device_code_brand_id = selected.brand_id
            sub_device.device_code_brand_name = selected.brand_name
            sub_device.device_code_brand_localized_name = selected.brand_localized_name
            sub_device.device_code_country = selected.country
            sub_device.device_code_version = selected.version
            sub_device.device_code_subversion = selected.subversion
            sub_device.device_code_type = selected.type
            sub_device.device_code_remote = selected.remote
            sub_device.device_code_popular = selected.popular

        if (sub_device.type or "").strip() == "0" and sub_device.device_code:
            capabilities = get_ac_capabilities(sub_device.device_code)
            sub_device.ac_available_modes = list(capabilities.available_modes)
            sub_device.ac_classic_temp_control = capabilities.classic_temp_control
            sub_device.ac_extra_windspeed = capabilities.extra_windspeed
            sub_device.ac_show_display_mold = capabilities.show_display_mold
            sub_device.ac_single_mode = capabilities.single_mode
            sub_device.ac_dehumidifier = capabilities.dehumidifier
            sub_device.ac_supports_sleep = capabilities.supports_sleep
            sub_device.ac_supports_power_saving = capabilities.supports_power_saving
            sub_device.ac_supports_turbo = capabilities.supports_turbo

        if is_supported_ac_sub_device(sub_device.type, sub_device.device_code):
            sub_device.estimated_capabilities = build_estimated_capabilities(sub_device.device_code)

    def _async_refresh_runtime_state(self, device_id: str, sub_device: AifaSubDevice) -> None:
        """Refresh runtime fallback metadata from the latest AIFA data snapshot."""
        if not is_supported_ac_sub_device(sub_device.type, sub_device.device_code):
            return

        state = self.get_ac_runtime_state(
            device_id,
            sub_device.id,
            device_code=sub_device.device_code,
        )
        if state.device_code != sub_device.device_code:
            self._ac_runtime_states[(device_id, sub_device.id)] = AifaAcRuntimeState.create(
                sub_device.device_code
            )
            state = self._ac_runtime_states[(device_id, sub_device.id)]

        # Keep requested defaults within the newly discovered capability range.
        state.requested_target_temperature = clamp_target_temperature(
            state.requested_target_temperature,
            device_code=sub_device.device_code,
        )

        if state.observed_status is not None and state.observed_status.device_code is None:
            state.observed_status = AifaAcStatus(
                device_code=sub_device.device_code,
                mode=state.observed_status.mode,
                target_temperature=state.observed_status.target_temperature,
                timer_ir_value=state.observed_status.timer_ir_value,
                fan_mode=state.observed_status.fan_mode,
                swing_mode=state.observed_status.swing_mode,
                turbo=state.observed_status.turbo,
                sleep=state.observed_status.sleep,
                power_saving=state.observed_status.power_saving,
                packet_byte_12=state.observed_status.packet_byte_12,
                packet_byte_13=state.observed_status.packet_byte_13,
                reported_fan_mode=state.observed_status.reported_fan_mode,
            )

    def _cancel_ac_pending_release(self, key: tuple[str, str]) -> None:
        task = self._ac_pending_release_tasks.pop(key, None)
        if task is not None and not task.done():
            task.cancel()

    def _schedule_ac_pending_release(
        self,
        device_id: str,
        sub_device_id: str,
        *,
        expected_mode: str | None,
    ) -> None:
        """Stop preferring the sent state once the hub had time to push its own.

        Like i-Ctrl Pro, nothing is asked after a command: a matching report
        clears the pending state on arrival, and otherwise the hub's last
        report is shown once the window ends.
        """
        key = (device_id, sub_device_id)
        self._cancel_ac_pending_release(key)
        window = (
            AC_PENDING_RELEASE_OFF_SECONDS
            if expected_mode == "off"
            else AC_PENDING_RELEASE_SECONDS
        )
        self._ac_pending_release_tasks[key] = self.hass.async_create_task(
            self._async_release_ac_pending(key, window)
        )

    async def _async_release_ac_pending(
        self, key: tuple[str, str], window: float
    ) -> None:
        try:
            await asyncio.sleep(window)
        finally:
            if self._ac_pending_release_tasks.get(key) is asyncio.current_task():
                self._ac_pending_release_tasks.pop(key, None)
                state = self.get_ac_runtime_state(*key)
                state.clear_pending_command_status()
                self.async_update_listeners()

    def _apply_classic_sensor_state(
        self,
        device_id: str,
        decoded: AifaClassicSensorState,
    ) -> bool:
        """Merge a live classic sensor payload into the device snapshot."""
        device = self.get_device(device_id)
        if device is None:
            return False

        now = dt_util.utcnow()
        updated = False

        # The cloud value is what i-Ctrl Pro displays; the hub's raw packet
        # converts differently, so it only fills readings the cloud lacks.
        for key, value in (
            ("temperature", decoded.temperature),
            ("humidity", decoded.humidity),
        ):
            if value is None or getattr(device, key) is not None:
                continue
            setattr(device, key, value)
            device.sensor_samples[key] = AifaSensorSample(
                sensor_type=key,
                value=value,
                updated_at=now,
                raw=dict(decoded.raw),
            )
            updated = True

        if updated:
            device.updated_at = now
        return updated

    def _apply_lan_observed_status(
        self,
        spec: AifaClassicSocketListenerSpec,
        sub_type: int,
        observed: AifaAcStatus,
    ) -> bool:
        return self._apply_classic_observed_status(spec, sub_type, observed, source="lan")

    def _apply_cloud_stream_status(
        self,
        spec: AifaClassicSocketListenerSpec,
        sub_type: int,
        observed: AifaAcStatus,
    ) -> bool:
        return self._apply_classic_observed_status(spec, sub_type, observed, source="cloud")

    def _apply_classic_observed_status(
        self,
        spec: AifaClassicSocketListenerSpec,
        sub_type: int,
        observed: AifaAcStatus,
        *,
        source: str | None = None,
    ) -> bool:
        """Apply one observed classic AC status to all matching sub-devices on the hub.

        `source` names the long-lived connection a report was read from; only
        those reports can confirm a command sent on that connection.
        """
        updated = False
        for target in spec.targets:
            if target.sub_type != sub_type:
                continue
            status = observed
            if observed.device_code is None and target.device_code is not None:
                status = AifaAcStatus(
                    device_code=target.device_code,
                    mode=observed.mode,
                    target_temperature=observed.target_temperature,
                    timer_ir_value=observed.timer_ir_value,
                    fan_mode=observed.fan_mode,
                    swing_mode=observed.swing_mode,
                    turbo=observed.turbo,
                    sleep=observed.sleep,
                    power_saving=observed.power_saving,
                    packet_byte_12=observed.packet_byte_12,
                    packet_byte_13=observed.packet_byte_13,
                    reported_fan_mode=observed.reported_fan_mode,
                )
            state = self.get_ac_runtime_state(
                spec.device_id,
                target.sub_device_id,
                device_code=target.device_code,
            )
            previous_revision = state.observed_revision
            state.apply_observed_status(status)
            updated = updated or state.observed_revision != previous_revision
        if source is not None:
            for waiter in self._hub_report_waiters.get(spec.device_id, ()):
                waiter.reports.append((source, sub_type, observed))
                waiter.event.set()
        return updated
