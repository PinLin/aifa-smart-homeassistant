"""Long-lived TLS socket manager for AIFA classic hubs.

Each AIFA i-Ctrl hub speaks a private TLS protocol on api.aifaremote.com:8751
in addition to its REST surface. The hub pushes:

- Observed AC status (mode + target_temp; helper flags are not reliably shadowed)
- Sensor packets (temperature / humidity)
- Wake-up frames (need a stock reply to keep the connection alive)
- Notify frames (signal that something else has just changed)

This module owns the lifecycle of those connections — one task per hub, with
exponential-backoff reconnect, four streaming parser buffers (capped to bound
long-lived memory growth), and a 3 s self-query loop that drives observed
state when the hub doesn't push spontaneously. State mutations are delegated
back to the coordinator via callbacks so this module stays free of any
runtime-state coupling.
"""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

from homeassistant.const import EVENT_HOMEASSISTANT_STARTED
from homeassistant.core import CALLBACK_TYPE, Event, HomeAssistant, callback
from homeassistant.helpers.event import async_track_time_interval

from .ac import (
    CLASSIC_WAKE_REPLY_PACKET,
    AifaAcStatus,
    decode_observed_status_packet,
    is_supported_ac_sub_device,
)
from .api import (
    AifaClassicSensorState,
    AifaDevice,
    AifaSmartApiClient,
    AifaSubDevice,
    build_classic_ac_query_steps,
    decode_classic_sensor_packet,
    extract_classic_notify_packets,
    extract_classic_sensor_packets,
    extract_classic_status_packets,
    extract_classic_wake_packets,
)
from .const import DOMAIN

if TYPE_CHECKING:
    pass

_LOGGER = logging.getLogger(__name__)

# Cap each streaming buffer in the classic-socket listener. Without a cap,
# any byte stream that no extractor signature ever consumes (e.g. an
# unterminated SENSOR{ prefix) can accumulate indefinitely on a long-lived
# TLS connection.
_CLASSIC_SOCKET_BUFFER_MAX = 65536
_CLASSIC_SOCKET_BUFFER_TRIM = 32768


def _ac_socket_sub_type(sub_device: AifaSubDevice) -> int:
    """Resolve the classic controller AC subtype used by the private socket."""
    for candidate in (sub_device.type, sub_device.sub_type):
        try:
            value = int(str(candidate))
        except (TypeError, ValueError):
            continue
        if value in {0, 1}:
            return value
    return 0


def _trim_socket_buffer(buffer: bytes) -> bytes:
    """Cap a streaming socket buffer to bound long-lived memory growth."""
    if len(buffer) > _CLASSIC_SOCKET_BUFFER_MAX:
        return buffer[-_CLASSIC_SOCKET_BUFFER_TRIM:]
    return buffer


@dataclass(frozen=True, slots=True)
class AifaClassicSocketTarget:
    """One supported AC target served by a classic socket listener."""

    sub_device_id: str
    sub_type: int
    device_code: str | None


@dataclass(frozen=True, slots=True)
class AifaClassicSocketListenerSpec:
    """Listener configuration for one classic AIFA hub."""

    device_id: str
    mac: str
    targets: tuple[AifaClassicSocketTarget, ...]


# Callback signatures. Manager calls back into the coordinator for any state
# mutation so this module owns I/O and lifecycle only.
ApplyObservedStatusFn = Callable[
    [AifaClassicSocketListenerSpec, int, AifaAcStatus], bool
]
ApplySensorStateFn = Callable[[str, AifaClassicSensorState], bool]
NotifyDataChangedFn = Callable[[], None]
NotifyFullRefreshFn = Callable[[], Awaitable[None]]


class ClassicSocketManager:
    """Owns the long-lived TLS connections to AIFA classic hubs."""

    def __init__(
        self,
        *,
        hass: HomeAssistant,
        client: AifaSmartApiClient,
        on_observed_status: ApplyObservedStatusFn,
        on_sensor_state: ApplySensorStateFn,
        on_data_changed: NotifyDataChangedFn,
        on_notify_full_refresh: NotifyFullRefreshFn,
    ) -> None:
        self._hass = hass
        self._client = client
        self._on_observed_status = on_observed_status
        self._on_sensor_state = on_sensor_state
        self._on_data_changed = on_data_changed
        self._on_notify_full_refresh = on_notify_full_refresh
        self._specs: dict[str, AifaClassicSocketListenerSpec] = {}
        self._listener_tasks: dict[str, asyncio.Task[None]] = {}
        self._notify_probe_tasks: dict[str, asyncio.Task[None]] = {}
        self._sync_unsub: CALLBACK_TYPE | None = None
        self._start_unsub: CALLBACK_TYPE | None = None
        self._bootstrap_task: asyncio.Task[None] | None = None
        self._sync_lock = asyncio.Lock()
        # Per-device TLS writers + per-device write locks. The reader loop in
        # `_async_consume` registers its writer here so external callers can
        # send command packets through the same long-lived TLS connection that
        # AIFA app itself uses (handshake + raw bytes, no HTTPS overhead).
        self._socket_writers: dict[str, asyncio.StreamWriter] = {}
        self._socket_write_locks: dict[str, asyncio.Lock] = {}

    # ----- Public API -----

    @property
    def specs(self) -> dict[str, AifaClassicSocketListenerSpec]:
        """Currently active listener specs (read-only view of internal dict)."""
        return self._specs

    def has_active_listener(self, device_id: str) -> bool:
        """Return True iff a long-lived listener task is running for `device_id`."""
        task = self._listener_tasks.get(device_id)
        return device_id in self._specs and task is not None and not task.done()

    def has_active_writer(self, device_id: str) -> bool:
        """Return True iff a connected TLS writer is registered for `device_id`."""
        writer = self._socket_writers.get(device_id)
        return writer is not None and not writer.is_closing()

    async def async_send_packet(self, device_id: str, packet_hex: str) -> bool:
        """Send a raw IR command through the long-lived TLS socket for `device_id`.

        Returns True on successful write. Returns False when no live writer is
        available — the caller is expected to surface this as an error so the
        user retries once the listener has reconnected. AIFA app itself does
        not fall back to a REST path for AC commands either.

        Raises on TLS write errors (`OSError` / similar) so the caller can
        translate them into HA service errors.

        This is the same wire path AIFA app itself uses (TLS to
        aifaremote.com:8751 with the `a1fad7c0` + JSON{mac,token} handshake);
        latency is lower and the bytes ride the same connection that already
        streams observed broadcasts.
        """
        writer = self._socket_writers.get(device_id)
        lock = self._socket_write_locks.get(device_id)
        if writer is None or lock is None or writer.is_closing():
            return False

        try:
            data = bytes.fromhex(packet_hex.strip().lower())
        except ValueError:
            return False
        if not data:
            return False

        async with lock:
            writer.write(data)
            await writer.drain()
        return True

    def sync_specs(self, devices: dict[str, AifaDevice]) -> None:
        """Refresh the desired set of hubs covered by background classic polling."""
        previous_specs = self._specs
        self._specs = self._build_specs(devices)
        for device_id, previous in previous_specs.items():
            current = self._specs.get(device_id)
            if current != previous:
                self._cancel_listener_task(device_id)
        if self._specs:
            self._ensure_background_sync()
            self._ensure_listener_tasks()
        else:
            self._stop_background_sync()

    async def shutdown(self) -> None:
        """Cancel background sync, listener tasks, and notify-probe tasks."""
        self._stop_background_sync()
        self._specs.clear()
        tasks: list[asyncio.Task[None]] = []
        tasks.extend(
            task
            for task in list(self._listener_tasks.values())
            if task is not None and not task.done()
        )
        self._listener_tasks.clear()
        tasks.extend(
            task
            for task in list(self._notify_probe_tasks.values())
            if task is not None and not task.done()
        )
        self._notify_probe_tasks.clear()
        if self._bootstrap_task is not None and not self._bootstrap_task.done():
            tasks.append(self._bootstrap_task)
        self._bootstrap_task = None
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    # ----- Spec building / lifecycle -----

    def _build_specs(
        self, devices: dict[str, AifaDevice]
    ) -> dict[str, AifaClassicSocketListenerSpec]:
        """Describe which hubs should keep a long-lived classic socket open."""
        specs: dict[str, AifaClassicSocketListenerSpec] = {}
        for device in devices.values():
            if not device.online or not device.mac:
                continue
            targets = tuple(
                sorted(
                    (
                        AifaClassicSocketTarget(
                            sub_device_id=sub_device.id,
                            sub_type=_ac_socket_sub_type(sub_device),
                            device_code=sub_device.device_code,
                        )
                        for sub_device in device.sub_devices
                        if is_supported_ac_sub_device(sub_device.type, sub_device.device_code)
                    ),
                    key=lambda item: (item.sub_type, item.sub_device_id),
                )
            )
            if not targets:
                continue
            specs[device.id] = AifaClassicSocketListenerSpec(
                device_id=device.id,
                mac=device.mac,
                targets=targets,
            )
        return specs

    def _ensure_background_sync(self) -> None:
        """Start the background classic poller once Home Assistant is fully running."""
        if self._sync_unsub is not None or self._start_unsub is not None:
            return
        if self._hass.is_running:
            self._start_background_sync()
            return
        self._start_unsub = self._hass.bus.async_listen_once(
            EVENT_HOMEASSISTANT_STARTED,
            self._async_handle_home_assistant_started,
        )

    @callback
    def _start_background_sync(self) -> None:
        """Register periodic classic polling once HA startup has completed."""
        if self._sync_unsub is not None or not self._specs:
            return
        self._sync_unsub = async_track_time_interval(
            self._hass,
            self._async_periodic_sync,
            timedelta(seconds=3),
        )
        self._bootstrap_task = self._hass.async_create_background_task(
            self._async_periodic_sync(None),
            name=f"{DOMAIN}_classic_socket_bootstrap",
        )
        self._ensure_listener_tasks()

    @callback
    def _stop_background_sync(self) -> None:
        """Stop periodic classic polling and clear startup hooks."""
        if self._start_unsub is not None:
            self._start_unsub()
            self._start_unsub = None
        if self._sync_unsub is not None:
            self._sync_unsub()
            self._sync_unsub = None
        task = self._bootstrap_task
        self._bootstrap_task = None
        if task is not None and not task.done():
            task.cancel()
        for device_id in tuple(self._listener_tasks):
            self._cancel_listener_task(device_id)
        for device_id in tuple(self._notify_probe_tasks):
            probe_task = self._notify_probe_tasks.pop(device_id)
            if not probe_task.done():
                probe_task.cancel()

    async def _async_handle_home_assistant_started(self, _: Event) -> None:
        """Start periodic classic polling after HA has fully finished startup."""
        self._start_unsub = None
        self._start_background_sync()

    async def _async_periodic_sync(self, _: datetime | None) -> None:
        """Poll the private classic AC query path for external state changes."""
        if not self._specs:
            return
        if self._sync_lock.locked():
            return

        async with self._sync_lock:
            updated = False
            for spec in tuple(self._specs.values()):
                try:
                    observed_by_sub_type = await self._client.async_query_classic_ac_statuses(
                        spec.mac,
                        sub_types={target.sub_type for target in spec.targets},
                        timeout=3.0,
                    )
                except Exception as err:  # pragma: no cover - runtime network guard
                    _LOGGER.debug(
                        "Classic socket periodic sync failed for %s: %s",
                        spec.device_id,
                        err,
                    )
                    continue
                for sub_type, observed in observed_by_sub_type.items():
                    updated = self._on_observed_status(spec, sub_type, observed) or updated

            if updated:
                self._on_data_changed()

    def _ensure_listener_tasks(self) -> None:
        """Ensure one long-lived classic socket listener exists per supported hub."""
        if not self._hass.is_running:
            return
        for device_id in tuple(self._listener_tasks):
            if device_id not in self._specs:
                self._cancel_listener_task(device_id)

        for device_id in self._specs:
            task = self._listener_tasks.get(device_id)
            if task is not None and not task.done():
                continue
            self._listener_tasks[device_id] = self._hass.async_create_background_task(
                self._async_run_listener(device_id),
                name=f"{DOMAIN}_classic_socket_listener_{device_id}",
            )

    def _cancel_listener_task(self, device_id: str) -> None:
        """Cancel one long-lived classic socket listener task."""
        task = self._listener_tasks.pop(device_id, None)
        if task is not None and not task.done():
            task.cancel()

    # ----- The actual I/O loop -----

    async def _async_run_listener(self, device_id: str) -> None:
        """Maintain a long-lived classic socket and apply live state packets."""
        backoff_seconds = 1.0
        try:
            while device_id in self._specs:
                spec = self._specs[device_id]
                try:
                    await self._async_consume(spec)
                    backoff_seconds = 1.0
                except asyncio.CancelledError:
                    raise
                except Exception as err:  # pragma: no cover - runtime network guard
                    _LOGGER.debug(
                        "Classic socket listener failed for %s: %s",
                        device_id,
                        err,
                    )
                await asyncio.sleep(backoff_seconds)
                backoff_seconds = min(backoff_seconds * 2, 15.0)
        finally:
            self._listener_tasks.pop(device_id, None)

    async def _async_consume(self, spec: AifaClassicSocketListenerSpec) -> None:
        """Read one long-lived classic socket until it disconnects."""
        socket_session = await self._client.async_open_classic_socket(spec.mac, timeout=6.0)
        reader = socket_session.reader
        writer = socket_session.writer
        # Register writer + per-device lock so external callers (coordinator)
        # can send command packets through this same TLS connection. The lock
        # serializes writes between the reader loop's own self-query / wake-
        # reply traffic and externally-initiated control commands.
        write_lock = asyncio.Lock()
        self._socket_writers[spec.device_id] = writer
        self._socket_write_locks[spec.device_id] = write_lock
        status_buffer = b""
        wake_buffer = b""
        notify_buffer = b""
        sensor_buffer = b""
        try:
            if socket_session.initial_bytes:
                updated = await self._async_process_chunk(
                    spec,
                    socket_session.initial_bytes,
                    writer,
                    status_buffer=status_buffer,
                    wake_buffer=wake_buffer,
                    notify_buffer=notify_buffer,
                    sensor_buffer=sensor_buffer,
                )
                status_buffer = updated[0]
                wake_buffer = updated[1]
                notify_buffer = updated[2]
                sensor_buffer = updated[3]
                if updated[4]:
                    self._on_data_changed()

            await self._async_send_query(spec, writer)
            loop = asyncio.get_running_loop()
            next_query_at = loop.time() + 3.0

            while spec.device_id in self._specs:
                timeout = max(0.1, next_query_at - loop.time())
                try:
                    chunk = await asyncio.wait_for(reader.read(4096), timeout=timeout)
                except asyncio.TimeoutError:
                    await self._async_send_query(spec, writer)
                    next_query_at = loop.time() + 3.0
                    continue
                if not chunk:
                    break
                updated = await self._async_process_chunk(
                    spec,
                    chunk,
                    writer,
                    status_buffer=status_buffer,
                    wake_buffer=wake_buffer,
                    notify_buffer=notify_buffer,
                    sensor_buffer=sensor_buffer,
                )
                status_buffer = updated[0]
                wake_buffer = updated[1]
                notify_buffer = updated[2]
                sensor_buffer = updated[3]
                if updated[4]:
                    self._on_data_changed()
        finally:
            # Drop registration before closing so external send attempts
            # racing with shutdown don't try to write into a closing socket.
            current_writer = self._socket_writers.get(spec.device_id)
            if current_writer is writer:
                self._socket_writers.pop(spec.device_id, None)
                self._socket_write_locks.pop(spec.device_id, None)
            writer.close()
            try:
                await writer.wait_closed()
            except OSError:
                pass

    async def _async_send_query(
        self,
        spec: AifaClassicSocketListenerSpec,
        writer: asyncio.StreamWriter,
    ) -> None:
        """Send the app-like AC query sequence over an existing listener socket."""
        lock = self._socket_write_locks.get(spec.device_id)
        if lock is None:
            for packet, _ in build_classic_ac_query_steps(
                {target.sub_type for target in spec.targets}
            ):
                writer.write(bytes.fromhex(packet))
            await writer.drain()
            return
        async with lock:
            for packet, _ in build_classic_ac_query_steps(
                {target.sub_type for target in spec.targets}
            ):
                writer.write(bytes.fromhex(packet))
            await writer.drain()

    async def _async_process_chunk(
        self,
        spec: AifaClassicSocketListenerSpec,
        chunk: bytes,
        writer: asyncio.StreamWriter,
        *,
        status_buffer: bytes,
        wake_buffer: bytes,
        notify_buffer: bytes,
        sensor_buffer: bytes,
    ) -> tuple[bytes, bytes, bytes, bytes, bool]:
        """Parse one classic socket chunk and apply any decoded updates."""
        status_buffer += chunk
        wake_buffer += chunk
        notify_buffer += chunk
        sensor_buffer += chunk
        updated = False

        wake_packets, wake_buffer = extract_classic_wake_packets(wake_buffer)
        if wake_packets:
            lock = self._socket_write_locks.get(spec.device_id)
            if lock is None:
                writer.write(bytes.fromhex(CLASSIC_WAKE_REPLY_PACKET))
                await writer.drain()
            else:
                async with lock:
                    writer.write(bytes.fromhex(CLASSIC_WAKE_REPLY_PACKET))
                    await writer.drain()

        status_packets, status_buffer = extract_classic_status_packets(status_buffer)
        for raw_packet in status_packets:
            decoded = decode_observed_status_packet(raw_packet)
            if decoded is None:
                continue
            sub_type, observed = decoded
            updated = self._on_observed_status(spec, sub_type, observed) or updated

        sensor_packets, sensor_buffer = extract_classic_sensor_packets(sensor_buffer)
        for packet in sensor_packets:
            decoded_sensor = decode_classic_sensor_packet(packet)
            if decoded_sensor is None:
                continue
            updated = self._on_sensor_state(spec.device_id, decoded_sensor) or updated

        notify_packets, notify_buffer = extract_classic_notify_packets(notify_buffer)
        if notify_packets:
            self._schedule_notify_probe(spec.device_id)

        # Cap each buffer so a malformed/uninterpretable byte stream cannot
        # accumulate forever on a long-lived connection. Trimming from the
        # front loses an in-progress packet at the boundary, but the parser
        # naturally re-syncs on the next prefix match.
        status_buffer = _trim_socket_buffer(status_buffer)
        wake_buffer = _trim_socket_buffer(wake_buffer)
        notify_buffer = _trim_socket_buffer(notify_buffer)
        sensor_buffer = _trim_socket_buffer(sensor_buffer)

        return status_buffer, wake_buffer, notify_buffer, sensor_buffer, updated

    # ----- Notify probe -----

    def _schedule_notify_probe(self, device_id: str) -> None:
        """Debounce a one-off classic query after a short notify-only frame."""
        task = self._notify_probe_tasks.get(device_id)
        if task is not None and not task.done():
            return
        self._notify_probe_tasks[device_id] = self._hass.async_create_background_task(
            self._async_run_notify_probe(device_id),
            name=f"{DOMAIN}_classic_socket_notify_probe_{device_id}",
        )

    async def _async_run_notify_probe(self, device_id: str) -> None:
        """Query observed AC state shortly after a notify-only frame.

        Also schedules a full coordinator refresh: a notify frame can mean
        macros/functions/devices were changed externally (e.g. AIFA app),
        and the AC-state probe alone won't pick those up. Without this,
        external changes only surface after the next 120s polling tick.
        """
        try:
            spec = self._specs.get(device_id)
            if spec is None:
                return
            self._hass.async_create_background_task(
                self._on_notify_full_refresh(),
                name=f"{DOMAIN}_notify_full_refresh_{device_id}",
            )
            updated = False
            for delay in (0.2, 0.75, 1.5, 3.0):
                await asyncio.sleep(delay)
                observed_by_sub_type = await self._client.async_query_classic_ac_statuses(
                    spec.mac,
                    sub_types={target.sub_type for target in spec.targets},
                    timeout=2.5,
                )
                for sub_type, observed in observed_by_sub_type.items():
                    updated = self._on_observed_status(spec, sub_type, observed) or updated
                if updated:
                    self._on_data_changed()
        except asyncio.CancelledError:
            raise
        except Exception as err:  # pragma: no cover - runtime network guard
            _LOGGER.debug(
                "Classic socket notify probe failed for %s: %s",
                device_id,
                err,
            )
        finally:
            self._notify_probe_tasks.pop(device_id, None)
