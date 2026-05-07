"""Coordinator for AIFA Smart."""
from __future__ import annotations

import asyncio
import logging
from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime, timedelta

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.update_coordinator import (
    TimestampDataUpdateCoordinator,
    UpdateFailed,
)
from homeassistant.util import dt as dt_util

from .ac import (
    AifaAcStatus,
    build_estimated_capabilities,
    build_power_off_command,
    build_status_command,
    clamp_target_temperature,
    decode_status_command,
    get_ac_capabilities,
    get_default_mode,
    is_supported_ac_sub_device,
    normalize_ac_mode,
)
from .api import (
    AifaClassicSensorState,
    AifaDevice,
    AifaDeviceCodeCatalog,
    AifaFunction,
    AifaFunctionCommand,
    AifaMacro,
    AifaSensorSample,
    AifaSubDevice,
    AifaSmartApiClient,
    AifaSmartAuthError,
    AifaSmartConnectionError,
    AifaSmartError,
)
from .classic_listener import (
    AifaClassicSocketListenerSpec,
    ClassicSocketManager,
    _ac_socket_sub_type,
)
from .const import DOMAIN, SCAN_INTERVAL
from .runtime import AifaAcRuntimeState, resolve_helper_flag_mutex

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


@dataclass(slots=True)
class AifaAccountData:
    """Snapshot of AIFA Smart account data."""

    devices: dict[str, AifaDevice]
    macros: list[AifaMacro]
    fetched_at: datetime


class AifaSmartCoordinator(TimestampDataUpdateCoordinator[AifaAccountData]):
    """Coordinate AIFA Smart data refreshes."""

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
        self._ac_follow_up_probe_tasks: dict[tuple[str, str], asyncio.Task[None]] = {}
        # Per-(device, sub_device) lock + last-send timestamp to serialize
        # async_apply_ac_state and enforce a min interval between IR sends.
        # Without these, two HA service calls within ~1s could both send IR
        # before the hub reconciles the first command, racing on runtime state
        # and producing the "dropped command" symptom (see runtime.py
        # apply_observed_status stale-observed guard for the other half of
        # the fix).
        self._ac_apply_locks: dict[tuple[str, str], asyncio.Lock] = {}
        self._ac_last_send_at: dict[tuple[str, str | None], float] = {}
        self._consecutive_failures = 0
        super().__init__(
            hass,
            logger=_LOGGER,
            name=f"{DOMAIN}_{entry.entry_id}",
            update_interval=timedelta(seconds=SCAN_INTERVAL),
        )

        self._classic_listener = ClassicSocketManager(
            hass=hass,
            client=client,
            on_observed_status=self._apply_classic_observed_status,
            on_sensor_state=self._apply_classic_sensor_state,
            on_data_changed=self.async_update_listeners,
            on_notify_full_refresh=self.async_request_refresh,
        )

    async def async_shutdown(self) -> None:
        """Cancel background classic-socket and follow-up probe tasks."""
        await self._classic_listener.shutdown()
        tasks = [
            task
            for task in list(self._ac_follow_up_probe_tasks.values())
            if task is not None and not task.done()
        ]
        self._ac_follow_up_probe_tasks.clear()
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    def async_sync_background_tasks(self) -> None:
        """Sync long-lived listener tasks against the latest coordinator snapshot."""
        if self.data is None:
            return
        self._classic_listener.sync_specs(self.data.devices)

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

    async def _async_dispatch_raw_commands(
        self, commands: list[AifaFunctionCommand]
    ) -> list[dict]:
        """Send raw commands through the long-lived classic TLS socket.

        Mirrors AIFA app's behaviour: AC control commands ride the same long-
        lived TLS socket to ``aifaremote.com:8751`` that streams hub
        broadcasts. AIFA app itself does not fall back to REST when the
        socket is unavailable — it queues until the socket comes back. We
        match that contract by raising on missing socket / write error so
        the caller can surface the failure rather than silently routing
        through a REST path that AIFA app reserves for geofence automation.
        """
        if not commands:
            return []

        responses: list[dict] = []
        for command in commands:
            sent_via_tls = await self._classic_listener.async_send_packet(
                command.device_id, command.command
            )
            if not sent_via_tls:
                raise AifaSmartError(
                    f"No active TLS connection for device {command.device_id}; "
                    "cannot send command"
                )

            response: dict = {
                "deviceId": command.device_id,
                "status": "success",
                "transport": "classic_tls",
            }
            if command.sub_device_id is not None:
                response["subDeviceId"] = command.sub_device_id
            responses.append(response)
        return responses

    async def async_send_function_commands(
        self, commands: list[AifaFunctionCommand]
    ) -> list[dict]:
        """Send a prepared command sequence and refresh coordinator data."""
        responses = await self._async_dispatch_raw_commands(commands)
        await self.async_request_refresh()
        return responses

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
        """Run all commands of a saved macro in order, honoring per-step delays."""
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
            responses.extend(
                await self._async_dispatch_raw_commands(
                    [
                        AifaFunctionCommand(
                            id=f"macro-{macro.id}-{index}",
                            device_id=str(command.device_id),
                            sub_device_id=command.sub_device_id,
                            command=command.command,
                        )
                    ]
                )
            )
            if index < last_index and command.delay and command.delay > 0:
                await asyncio.sleep(command.delay / 1000)

        await self.async_request_refresh()
        return responses

    def _prune_stale_runtime_state(self, devices: dict[str, AifaDevice]) -> None:
        """Drop runtime/cache entries for sub-devices no longer in the snapshot.

        Without this, removing a sub-device in the AIFA app leaves stale keys
        in `_ac_runtime_states`, `_ac_apply_locks`, `_ac_last_send_at`,
        `_device_code_catalogs`, and orphan tasks in
        `_ac_follow_up_probe_tasks` for the lifetime of the integration.
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
        for key in list(self._ac_apply_locks.keys()):
            if key not in live_pairs:
                self._ac_apply_locks.pop(key, None)
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
        for key in list(self._ac_follow_up_probe_tasks.keys()):
            if key in live_pairs:
                continue
            task = self._ac_follow_up_probe_tasks.pop(key, None)
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
        fan_mode: str | None = None,
        swing_mode: str | None = None,
        turbo: bool | None = None,
        sleep: bool | None = None,
        power_saving: bool | None = None,
        device_code: str | None = None,
    ) -> str:
        """Send one AC power or status command and update requested fallback state."""
        # Serialize concurrent applies for the same AC and enforce a minimum
        # gap between sends. The hub needs ~1s to reconcile each command;
        # without this, a second send racing the first observed packet
        # produces the "dropped command" symptom.
        lock_key = (device_id, sub_device_id)
        lock = self._ac_apply_locks.get(lock_key)
        if lock is None:
            lock = asyncio.Lock()
            self._ac_apply_locks[lock_key] = lock
        async with lock:
            await self._async_throttle_ac_send(lock_key)
            return await self._async_apply_ac_state_locked(
                device_id,
                sub_device_id,
                hvac_mode=hvac_mode,
                target_temperature=target_temperature,
                fan_mode=fan_mode,
                swing_mode=swing_mode,
                turbo=turbo,
                sleep=sleep,
                power_saving=power_saving,
                device_code=device_code,
            )

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
        fan_mode: str | None = None,
        swing_mode: str | None = None,
        turbo: bool | None = None,
        sleep: bool | None = None,
        power_saving: bool | None = None,
        device_code: str | None = None,
    ) -> str:
        state = self.get_ac_runtime_state(
            device_id,
            sub_device_id,
            device_code=device_code,
        )
        baseline_observed_revision = state.observed_revision
        resolved_mode = normalize_ac_mode(
            (hvac_mode or state.requested_hvac_mode or get_default_mode(device_code)).lower()
        )
        if resolved_mode == "off":
            command = build_power_off_command(device_code=device_code)
            await self.async_send_function_commands(
                [
                    AifaFunctionCommand(
                        id="manual-off",
                        device_id=device_id,
                        sub_device_id=sub_device_id,
                        command=command,
                    )
                ]
            )
            state.requested_hvac_mode = "off"
            state.requested_turbo = False
            state.requested_sleep = False
            state.requested_power_saving = False
            state.last_power_command = command
            state.last_status_command = None
            state.apply_sent_command_status(
                AifaAcStatus(
                    device_code=device_code or state.device_code,
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
            self.async_update_listeners()
            await self._async_probe_ac_observed_state(
                device_id,
                sub_device_id,
                minimum_observed_revision=baseline_observed_revision,
                expected_mode="off",
            )
            self._schedule_ac_follow_up_probe(
                device_id,
                sub_device_id,
                expected_mode="off",
            )
            return command

        resolved_target_temperature = clamp_target_temperature(
            target_temperature if target_temperature is not None else state.requested_target_temperature,
            device_code=device_code,
        )
        resolved_fan_mode = fan_mode or state.requested_fan_mode
        resolved_swing_mode = swing_mode or state.requested_swing_mode
        resolved_power_saving, resolved_turbo, resolved_sleep = resolve_helper_flag_mutex(
            runtime_power_saving=state.requested_power_saving,
            runtime_turbo=state.requested_turbo,
            runtime_sleep=state.requested_sleep,
            caller_power_saving=power_saving,
            caller_turbo=turbo,
            caller_sleep=sleep,
        )

        command = build_status_command(
            resolved_mode,
            resolved_target_temperature,
            device_code=device_code,
            fan_mode=resolved_fan_mode,
            swing_mode=resolved_swing_mode,
            turbo=resolved_turbo,
            sleep=resolved_sleep,
            power_saving=resolved_power_saving,
        )
        await self.async_send_function_commands(
            [
                AifaFunctionCommand(
                    id="manual-status",
                    device_id=device_id,
                    sub_device_id=sub_device_id,
                    command=command,
                )
            ]
        )

        state.requested_hvac_mode = resolved_mode
        state.requested_target_temperature = resolved_target_temperature
        state.requested_fan_mode = resolved_fan_mode
        state.requested_swing_mode = resolved_swing_mode
        state.requested_turbo = resolved_turbo
        state.requested_sleep = resolved_sleep
        state.requested_power_saving = resolved_power_saving
        state.last_active_hvac_mode = resolved_mode
        state.last_status_command = command
        state.last_power_command = None
        decoded_command = decode_status_command(command, device_code=device_code)
        if decoded_command is not None:
            state.apply_sent_command_status(decoded_command)
        else:
            state.command_status = None
            state.command_status_pending = False
            state.observed_status = None
            state.state_source = "requested_fallback"
            state.helper_flags_state_source = "requested_fallback"
        self.async_update_listeners()
        await self._async_probe_ac_observed_state(
            device_id,
            sub_device_id,
            minimum_observed_revision=baseline_observed_revision,
            expected_mode=resolved_mode,
            expected_target_temperature=resolved_target_temperature,
            expected_fan_mode=resolved_fan_mode,
            expected_swing_mode=resolved_swing_mode,
        )
        self._schedule_ac_follow_up_probe(
            device_id,
            sub_device_id,
            expected_mode=resolved_mode,
            expected_target_temperature=resolved_target_temperature,
            expected_fan_mode=resolved_fan_mode,
            expected_swing_mode=resolved_swing_mode,
        )
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
        return result

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

                await self._async_refresh_device_observed_states(device)
                enriched[device.id] = device

            self._classic_listener.sync_specs(enriched)
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
        if state.requested_fan_mode not in get_ac_capabilities(sub_device.device_code).fan_modes:
            state.requested_fan_mode = get_ac_capabilities(sub_device.device_code).fan_modes[0]
        if state.requested_swing_mode not in get_ac_capabilities(sub_device.device_code).swing_modes:
            state.requested_swing_mode = get_ac_capabilities(sub_device.device_code).swing_modes[-1]

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
            )

    async def _async_refresh_device_observed_states(self, device: AifaDevice) -> None:
        """Refresh observed AC runtime state for all supported sub-devices on one hub."""
        if not device.online or not device.mac:
            return

        ac_sub_devices = [
            sub_device
            for sub_device in device.sub_devices
            if is_supported_ac_sub_device(sub_device.type, sub_device.device_code)
        ]
        if not ac_sub_devices:
            return

        observed_by_sub_type = await self.client.async_query_classic_ac_statuses(
            device.mac,
            sub_types={_ac_socket_sub_type(sub_device) for sub_device in ac_sub_devices},
        )
        if not observed_by_sub_type:
            return

        for sub_device in ac_sub_devices:
            state = self.get_ac_runtime_state(
                device.id,
                sub_device.id,
                device_code=sub_device.device_code,
            )
            observed = observed_by_sub_type.get(_ac_socket_sub_type(sub_device))
            if observed is None:
                continue
            state.apply_observed_status(observed)

    async def _async_probe_ac_observed_state(
        self,
        device_id: str,
        sub_device_id: str,
        *,
        minimum_observed_revision: int | None = None,
        expected_mode: str | None = None,
        expected_target_temperature: int | None = None,
        expected_fan_mode: str | None = None,
        expected_swing_mode: str | None = None,
    ) -> bool:
        """Probe one AC sub-device through the private classic socket."""
        device = self.get_device(device_id)
        sub_device = self.get_sub_device(device_id, sub_device_id)
        if (
            device is None
            or sub_device is None
            or not device.online
            or not device.mac
            or not is_supported_ac_sub_device(sub_device.type, sub_device.device_code)
        ):
            return False

        if self._classic_listener.has_active_listener(device_id) and await self._async_wait_for_classic_socket_update(
            device_id,
            sub_device_id,
            minimum_observed_revision=minimum_observed_revision,
            expected_mode=expected_mode,
            expected_target_temperature=expected_target_temperature,
            expected_fan_mode=expected_fan_mode,
            expected_swing_mode=expected_swing_mode,
        ):
            return True

        observed_by_sub_type = await self.client.async_query_classic_ac_statuses(
            device.mac,
            sub_types={_ac_socket_sub_type(sub_device)},
        )
        observed = observed_by_sub_type.get(_ac_socket_sub_type(sub_device))
        if observed is None:
            return False

        state = self.get_ac_runtime_state(
            device_id,
            sub_device_id,
            device_code=sub_device.device_code,
        )
        state.apply_observed_status(observed)
        self.async_update_listeners()
        return True

    async def _async_wait_for_classic_socket_update(
        self,
        device_id: str,
        sub_device_id: str,
        *,
        minimum_observed_revision: int | None = None,
        expected_mode: str | None = None,
        expected_target_temperature: int | None = None,
        expected_fan_mode: str | None = None,
        expected_swing_mode: str | None = None,
        timeout: float = 2.5,
    ) -> bool:
        """Wait briefly for the long-lived classic socket listener to push a fresh state."""
        if not self._classic_listener.has_active_listener(device_id):
            return False

        state = self.get_ac_runtime_state(device_id, sub_device_id)
        baseline_revision = (
            state.observed_revision if minimum_observed_revision is None else minimum_observed_revision
        )
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while loop.time() < deadline:
            if state.observed_status is not None and state.observed_revision > baseline_revision:
                if expected_mode is None:
                    return True
                if self._ac_state_matches_expectation(
                    device_id,
                    sub_device_id,
                    expected_mode=expected_mode,
                    expected_target_temperature=expected_target_temperature,
                    expected_fan_mode=expected_fan_mode,
                    expected_swing_mode=expected_swing_mode,
                ):
                    return True
            await asyncio.sleep(0.1)
        return False

    def _schedule_ac_follow_up_probe(
        self,
        device_id: str,
        sub_device_id: str,
        *,
        expected_mode: str | None,
        expected_target_temperature: int | None = None,
        expected_fan_mode: str | None = None,
        expected_swing_mode: str | None = None,
    ) -> None:
        """Schedule background socket re-probes until the observed state settles."""
        key = (device_id, sub_device_id)
        task = self._ac_follow_up_probe_tasks.pop(key, None)
        if task is not None and not task.done():
            task.cancel()

        self._ac_follow_up_probe_tasks[key] = self.hass.async_create_task(
            self._async_follow_up_ac_probe(
                device_id,
                sub_device_id,
                expected_mode=expected_mode,
                expected_target_temperature=expected_target_temperature,
                expected_fan_mode=expected_fan_mode,
                expected_swing_mode=expected_swing_mode,
            )
        )

    async def _async_follow_up_ac_probe(
        self,
        device_id: str,
        sub_device_id: str,
        *,
        expected_mode: str | None,
        expected_target_temperature: int | None = None,
        expected_fan_mode: str | None = None,
        expected_swing_mode: str | None = None,
    ) -> None:
        """Poll the private socket in the background until the AC state settles."""
        key = (device_id, sub_device_id)
        delays = (2.0, 4.0, 8.0, 12.0) if expected_mode == "off" else (2.0, 4.0, 8.0)
        try:
            for delay in delays:
                await asyncio.sleep(delay)
                if not await self._async_probe_ac_observed_state(
                    device_id,
                    sub_device_id,
                    expected_mode=expected_mode,
                    expected_target_temperature=expected_target_temperature,
                    expected_fan_mode=expected_fan_mode,
                    expected_swing_mode=expected_swing_mode,
                ):
                    continue
                if self._ac_state_matches_expectation(
                    device_id,
                    sub_device_id,
                    expected_mode=expected_mode,
                    expected_target_temperature=expected_target_temperature,
                    expected_fan_mode=expected_fan_mode,
                    expected_swing_mode=expected_swing_mode,
                ):
                    break
        except asyncio.CancelledError:
            raise
        finally:
            current = self._ac_follow_up_probe_tasks.get(key)
            if current is asyncio.current_task():
                state = self.get_ac_runtime_state(device_id, sub_device_id)
                state.clear_pending_command_status()
                self.async_update_listeners()
                self._ac_follow_up_probe_tasks.pop(key, None)

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
        """Return True when the latest observed AC state matches the requested shape."""
        state = self.get_ac_runtime_state(device_id, sub_device_id)
        observed = state.observed_status
        if observed is None:
            return False
        if expected_mode is None:
            return True
        if observed.mode != expected_mode:
            return False
        if expected_target_temperature is not None and observed.target_temperature not in (
            None,
            expected_target_temperature,
        ):
            return False
        if expected_fan_mode is not None and observed.fan_mode not in (None, expected_fan_mode):
            return False
        if expected_swing_mode is not None and observed.swing_mode not in (None, expected_swing_mode):
            return False
        return True

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

        if decoded.temperature is not None and device.temperature != decoded.temperature:
            device.temperature = decoded.temperature
            updated = True
        if decoded.humidity is not None and device.humidity != decoded.humidity:
            device.humidity = decoded.humidity
            updated = True

        if decoded.temperature is not None:
            current = device.sensor_samples.get("temperature")
            if current is None or current.value != decoded.temperature or current.raw != decoded.raw:
                device.sensor_samples["temperature"] = AifaSensorSample(
                    sensor_type="temperature",
                    value=decoded.temperature,
                    updated_at=now,
                    raw=dict(decoded.raw),
                )
                updated = True
        if decoded.humidity is not None:
            current = device.sensor_samples.get("humidity")
            if current is None or current.value != decoded.humidity or current.raw != decoded.raw:
                device.sensor_samples["humidity"] = AifaSensorSample(
                    sensor_type="humidity",
                    value=decoded.humidity,
                    updated_at=now,
                    raw=dict(decoded.raw),
                )
                updated = True

        if updated:
            device.updated_at = now
        return updated

    def _apply_classic_observed_status(
        self,
        spec: AifaClassicSocketListenerSpec,
        sub_type: int,
        observed: AifaAcStatus,
    ) -> bool:
        """Apply one observed classic AC status to all matching sub-devices on the hub."""
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
                )
            state = self.get_ac_runtime_state(
                spec.device_id,
                target.sub_device_id,
                device_code=target.device_code,
            )
            previous_revision = state.observed_revision
            state.apply_observed_status(status)
            updated = updated or state.observed_revision != previous_revision
        return updated
