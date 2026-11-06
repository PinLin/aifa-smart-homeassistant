"""Long-lived TLS socket manager for AIFA classic hubs.

Each AIFA i-Ctrl hub speaks a private TLS protocol on api.aifaremote.com:8751
in addition to its REST surface. The hub pushes:

- Observed AC status (mode + target_temp; helper flags are not reliably shadowed)
- Sensor packets (temperature / humidity)
- Wake-up frames (need a stock reply to keep the connection alive)
- Notify frames (signal that something else has just changed)

This module owns the lifecycle of those connections — one task per hub, with
exponential-backoff reconnect and four streaming parser buffers (capped to
bound long-lived memory growth). Like i-Ctrl Pro, it queries the hub once per
connection and otherwise relies on what the hub pushes: a hub busy answering
queries has been seen to drop a key press that arrives at the same time.
State mutations are delegated back to the coordinator via callbacks so this
module stays free of any runtime-state coupling.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

from homeassistant.const import EVENT_HOMEASSISTANT_STARTED
from homeassistant.core import CALLBACK_TYPE, Event, HomeAssistant, callback

from .ac import (
    CLASSIC_AC_QUERY_PACKET,
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
    decode_classic_sensor_packet,
    enable_tcp_keepalive,
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
# The AIFA app waits this long after the relay's handshake ack before it
# reports the link connected and starts writing (shared with its LAN path).
CLASSIC_CONNECT_WRITE_DELAY = 0.5
# A link that drops sooner than this keeps growing the backoff, so a relay
# that closes right after the handshake cannot draw a query every second.
_MIN_HEALTHY_SECONDS = 10.0
# i-Ctrl Pro's command queue holds the next write for the previous packet's
# delay: 600 ms after a key, 300 ms after a query, a 15-byte packet or
# SetCode. A hub answering a query can drop a key arriving with it.
KEY_QUEUE_DELAY = 0.6
QUERY_QUEUE_DELAY = 0.3
_QUERY_LIKE_LENGTHS = frozenset({6, 15})


def queue_delay(packet: bytes) -> float:
    """How long the next write waits after `packet`; other codes wait like a key."""
    if packet == bytes.fromhex(CLASSIC_AC_QUERY_PACKET) or len(packet) in _QUERY_LIKE_LENGTHS:
        return QUERY_QUEUE_DELAY
    return KEY_QUEUE_DELAY


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
    device_type: str | None = None


def build_hub_specs(
    devices: dict[str, AifaDevice], *, require_online: bool
) -> dict[str, AifaClassicSocketListenerSpec]:
    """Describe the hubs with at least one supported AC target."""
    specs: dict[str, AifaClassicSocketListenerSpec] = {}
    for device in devices.values():
        if not device.mac or (require_online and not device.online):
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
            device_type=device.device_type,
        )
    return specs


@dataclass(slots=True)
class ClassicStreamBuffers:
    """Per-connection parser buffers for the classic hub byte stream."""

    status: bytes = b""
    wake: bytes = b""
    notify: bytes = b""
    sensor: bytes = b""


async def process_classic_stream_chunk(
    chunk: bytes,
    buffers: ClassicStreamBuffers,
    *,
    device_type: str | None,
    reply_wake: Callable[[], Awaitable[None]],
    on_status: Callable[[int, AifaAcStatus], bool],
    on_sensor: Callable[[AifaClassicSensorState], bool],
    on_notify: Callable[[], None],
) -> bool:
    """Parse one chunk of the hub byte stream shared by the cloud and LAN links.

    Mutates `buffers` in place and returns True when any callback reported a
    state change.
    """
    buffers.status += chunk
    buffers.wake += chunk
    buffers.notify += chunk
    buffers.sensor += chunk
    updated = False

    wake_packets, buffers.wake = extract_classic_wake_packets(buffers.wake)
    if wake_packets:
        await reply_wake()

    status_packets, buffers.status = extract_classic_status_packets(buffers.status)
    for raw_packet in status_packets:
        decoded = decode_observed_status_packet(raw_packet)
        if decoded is None:
            continue
        sub_type, observed = decoded
        updated = on_status(sub_type, observed) or updated

    sensor_packets, buffers.sensor = extract_classic_sensor_packets(buffers.sensor)
    for packet in sensor_packets:
        decoded_sensor = decode_classic_sensor_packet(packet, device_type=device_type)
        if decoded_sensor is None:
            continue
        updated = on_sensor(decoded_sensor) or updated

    notify_packets, buffers.notify = extract_classic_notify_packets(buffers.notify)
    if notify_packets:
        on_notify()

    # Trimming from the front can lose an in-progress packet at the boundary,
    # but each extractor re-syncs on the next prefix match.
    buffers.status = _trim_socket_buffer(buffers.status)
    buffers.wake = _trim_socket_buffer(buffers.wake)
    buffers.notify = _trim_socket_buffer(buffers.notify)
    buffers.sensor = _trim_socket_buffer(buffers.sensor)
    return updated


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
        connect_write_delay: float = CLASSIC_CONNECT_WRITE_DELAY,
    ) -> None:
        self._hass = hass
        self._client = client
        self._on_observed_status = on_observed_status
        self._on_sensor_state = on_sensor_state
        self._on_data_changed = on_data_changed
        self._on_notify_full_refresh = on_notify_full_refresh
        self._connect_write_delay = connect_write_delay
        self._specs: dict[str, AifaClassicSocketListenerSpec] = {}
        self._listener_tasks: dict[str, asyncio.Task[None]] = {}
        self._start_unsub: CALLBACK_TYPE | None = None
        # Per-device TLS writers + per-device write locks. The reader loop in
        # `_async_consume` registers its writer here so external callers can
        # send command packets through the same long-lived TLS connection that
        # AIFA app itself uses (handshake + raw bytes, no HTTPS overhead).
        self._socket_writers: dict[str, asyncio.StreamWriter] = {}
        self._socket_write_locks: dict[str, asyncio.Lock] = {}
        self._socket_writable_at: dict[str, float] = {}

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
            await self._async_wait_writable(device_id)
            if self._socket_writers.get(device_id) is not writer or writer.is_closing():
                return False
            writer.write(data)
            self._hold_next_write(device_id, data)
            await writer.drain()
        return True

    def _hold_next_write(self, device_id: str, data: bytes) -> None:
        self._socket_writable_at[device_id] = (
            asyncio.get_running_loop().time() + queue_delay(data)
        )

    async def _async_wait_writable(self, device_id: str) -> None:
        """Hold a write until the hub listens to the current socket and the
        queue delay of the previous write has passed.

        Callers wait while holding the write lock so writes keep their order.
        """
        writable_at = self._socket_writable_at.get(device_id)
        if writable_at is not None:
            delay = writable_at - asyncio.get_running_loop().time()
            if delay > 0:
                await asyncio.sleep(delay)

    def drop_connection(self, device_id: str) -> None:
        """Abort the long-lived socket of a hub so the listener reconnects.

        TCP keepalive notices a half-open socket only after a few minutes;
        a command that drew no reply is reason enough to reconnect now.
        """
        writer = self._socket_writers.pop(device_id, None)
        self._socket_write_locks.pop(device_id, None)
        if writer is not None and not writer.is_closing():
            writer.transport.abort()

    def sync_specs(self, devices: dict[str, AifaDevice]) -> None:
        """Refresh the desired set of hubs that keep a classic socket open."""
        previous_specs = self._specs
        self._specs = self._build_specs(devices)
        for device_id, previous in previous_specs.items():
            current = self._specs.get(device_id)
            if current != previous:
                self._cancel_listener_task(device_id)
        if self._specs:
            self._ensure_listener_tasks()
        else:
            self._stop_listeners()

    async def shutdown(self) -> None:
        """Cancel the startup hook and every listener task."""
        self._stop_listeners()
        self._specs.clear()
        tasks = [
            task
            for task in list(self._listener_tasks.values())
            if task is not None and not task.done()
        ]
        self._listener_tasks.clear()
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    # ----- Spec building / lifecycle -----

    def _build_specs(
        self, devices: dict[str, AifaDevice]
    ) -> dict[str, AifaClassicSocketListenerSpec]:
        """Describe which hubs should keep a long-lived classic socket open."""
        return build_hub_specs(devices, require_online=True)

    @callback
    def _stop_listeners(self) -> None:
        """Cancel the startup hook and every listener task."""
        if self._start_unsub is not None:
            self._start_unsub()
            self._start_unsub = None
        for device_id in tuple(self._listener_tasks):
            self._cancel_listener_task(device_id)

    async def _async_handle_home_assistant_started(self, _: Event) -> None:
        """Open the listeners once Home Assistant has finished starting."""
        self._start_unsub = None
        self._ensure_listener_tasks()

    def _ensure_listener_tasks(self) -> None:
        """Ensure one long-lived classic socket listener exists per supported hub."""
        if not self._hass.is_running:
            if self._start_unsub is None:
                self._start_unsub = self._hass.bus.async_listen_once(
                    EVENT_HOMEASSISTANT_STARTED,
                    self._async_handle_home_assistant_started,
                )
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
                started = asyncio.get_running_loop().time()
                try:
                    await self._async_consume(spec)
                except asyncio.CancelledError:
                    raise
                except Exception as err:  # pragma: no cover - runtime network guard
                    _LOGGER.debug(
                        "Classic socket listener failed for %s: %s",
                        device_id,
                        err,
                    )
                if asyncio.get_running_loop().time() - started >= _MIN_HEALTHY_SECONDS:
                    backoff_seconds = 1.0
                await asyncio.sleep(backoff_seconds)
                backoff_seconds = min(backoff_seconds * 2, 15.0)
        finally:
            self._listener_tasks.pop(device_id, None)

    async def _async_consume(self, spec: AifaClassicSocketListenerSpec) -> None:
        """Read one long-lived classic socket until it disconnects."""
        socket_session = await self._client.async_open_classic_socket(spec.mac, timeout=6.0)
        reader = socket_session.reader
        writer = socket_session.writer
        # Nothing is written after the connect query, so only keepalive can
        # tell a half-open link (e.g. a WAN drop without RST) from a quiet one.
        enable_tcp_keepalive(writer.get_extra_info("socket"))
        # Register writer + per-device lock so external callers (coordinator)
        # can send command packets through this same TLS connection. The lock
        # serializes writes between the reader loop's own connect query / wake
        # reply traffic and externally-initiated control commands.
        write_lock = asyncio.Lock()
        self._socket_writable_at[spec.device_id] = (
            asyncio.get_running_loop().time() + self._connect_write_delay
        )
        self._socket_writers[spec.device_id] = writer
        self._socket_write_locks[spec.device_id] = write_lock
        buffers = ClassicStreamBuffers()
        try:
            if socket_session.initial_bytes:
                if await self._async_process_chunk(
                    spec, socket_session.initial_bytes, writer, buffers
                ):
                    self._on_data_changed()

            # i-Ctrl Pro asks once per connection; every later report is pushed.
            await self._async_write(
                spec.device_id, writer, CLASSIC_AC_QUERY_PACKET, queued=True
            )

            while spec.device_id in self._specs:
                chunk = await reader.read(4096)
                if not chunk:
                    break
                if await self._async_process_chunk(spec, chunk, writer, buffers):
                    self._on_data_changed()
        finally:
            # Drop registration before closing so external send attempts
            # racing with shutdown don't try to write into a closing socket.
            current_writer = self._socket_writers.get(spec.device_id)
            if current_writer is writer:
                self._socket_writers.pop(spec.device_id, None)
                self._socket_write_locks.pop(spec.device_id, None)
                self._socket_writable_at.pop(spec.device_id, None)
            writer.close()
            try:
                await writer.wait_closed()
            except OSError:
                pass

    async def _async_write(
        self,
        device_id: str,
        writer: asyncio.StreamWriter,
        packet_hex: str,
        *,
        queued: bool = False,
    ) -> None:
        """Write one packet the listener itself owes the hub.

        A wake reply answers the hub and does not hold the queue; the
        connect query does, like any packet i-Ctrl Pro queues.
        """
        lock = self._socket_write_locks.get(device_id)
        data = bytes.fromhex(packet_hex)
        async with lock if lock is not None else contextlib.nullcontext():
            await self._async_wait_writable(device_id)
            writer.write(data)
            if queued:
                self._hold_next_write(device_id, data)
            await writer.drain()

    async def _async_process_chunk(
        self,
        spec: AifaClassicSocketListenerSpec,
        chunk: bytes,
        writer: asyncio.StreamWriter,
        buffers: ClassicStreamBuffers,
    ) -> bool:
        """Parse one classic socket chunk and apply any decoded updates."""

        async def reply_wake() -> None:
            await self._async_write(spec.device_id, writer, CLASSIC_WAKE_REPLY_PACKET)

        return await process_classic_stream_chunk(
            chunk,
            buffers,
            device_type=spec.device_type,
            reply_wake=reply_wake,
            on_status=lambda sub_type, observed: self._on_observed_status(
                spec, sub_type, observed
            ),
            on_sensor=lambda sensor: self._on_sensor_state(spec.device_id, sensor),
            on_notify=lambda: self._schedule_notify_refresh(spec.device_id),
        )

    def _schedule_notify_refresh(self, device_id: str) -> None:
        """Reload the account after a notify frame; the hub itself is not asked.

        A notify frame can mean macros, functions or devices were changed
        elsewhere (e.g. in the AIFA app), which the pushed AC status does not
        carry. The coordinator's request debouncer coalesces bursts.
        """
        self._hass.async_create_background_task(
            self._on_notify_full_refresh(),
            name=f"{DOMAIN}_notify_full_refresh_{device_id}",
        )
