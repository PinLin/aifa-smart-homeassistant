"""LAN transport for AIFA i-Ctrl hubs.

The hub has no listening port of its own. HA opens a TCP listener, then
broadcasts a 54-byte UDP request to port 11123 whose source port equals that
TCP port; the hub connects back with plain TCP and speaks the same byte
stream as the cloud TLS socket (status packets, `f1 ee ?? f0` wake frames).
"""
from __future__ import annotations

import asyncio
import errno
import ipaddress
import logging
import time
from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from homeassistant.util import dt as dt_util

from .ac import CLASSIC_WAKE_REPLY_PACKET, AifaAcStatus
from .api import (
    AifaClassicSensorState,
    AifaDevice,
    AifaSmartConnectionError,
    enable_tcp_keepalive,
)
from .classic_listener import (
    AifaClassicSocketListenerSpec,
    ClassicStreamBuffers,
    build_hub_specs,
    process_classic_stream_chunk,
    queue_delay,
)
from .const import DOMAIN, LAN_UNAVAILABLE_GRACE_SECONDS

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)

LAN_HUB_PORT = 11123
LAN_SEARCH_SOURCE_PORT = 11124
LAN_PACKET_LENGTH = 54

_REQUEST_HEADER = b"\x54\x01\x00"
_SEARCH_REPLY_HEADER = b"\x55\x01\xa4"
_CMD_SEARCH = 0xC0
_CMD_CONNECT = 0xC2
_MAC_SLICE = slice(3, 15)
_CMD_INDEX = 16
_MAC_SEPARATORS = str.maketrans("", "", ":-. \t")

LAN_BACKOFF_SCHEDULE: tuple[float, ...] = (1, 2, 4, 8, 16, 32, 60)
# A link that drops sooner than this counts as a failure for the backoff.
_MIN_HEALTHY_SECONDS = 10.0
_SEARCH_TIMEOUT = 1.5
_SEARCH_MIN_INTERVAL = 60.0
# Two misses in a row usually means the hub got a new DHCP lease.
_HUB_IP_FORGET_AFTER_FAILURES = 2

# DevicePacketAcQuery as sent by the AIFA app; the hub answers it for every
# slot without sending anything to the AC.
LAN_STATUS_QUERY_PACKET = "fff3f2f0"
# A link with no inbound bytes for this long gets a status query, and is
# treated as half-open when that query draws no reply. Keepalive alone
# takes ~90 s and misses links whose writes are still being buffered.
LAN_IDLE_QUERY_SECONDS = 60.0
LAN_QUERY_REPLY_TIMEOUT = 5.0
# An unanswered idle query does not condemn a link that carried bytes this
# recently, since the hub can ignore queries for 8 s or more. With the
# defaults the idle query only goes out after 60 s of silence, so this only
# matters if the idle and reply times ever add up to less than it.
LAN_RECENT_RX_SECONDS = 30.0
# The hub drops whatever arrives in the first moments of a connection; the
# AIFA app reports the link connected, and starts writing, 0.5 s in.
LAN_CONNECT_WRITE_DELAY = 0.5


def wire_mac(mac: str) -> str:
    """Strip separators but keep the case the hub or cloud reported."""
    return str(mac or "").strip().translate(_MAC_SEPARATORS)


def normalize_mac(mac: str | None) -> str:
    """Return a case-insensitive comparison key for a hub MAC."""
    return wire_mac(mac or "").upper()


def _build_request(cmd: int, mac_bytes: bytes = b"") -> bytes:
    packet = bytearray(LAN_PACKET_LENGTH)
    packet[0:3] = _REQUEST_HEADER
    if mac_bytes:
        packet[_MAC_SLICE] = mac_bytes
    packet[_CMD_INDEX] = cmd
    return bytes(packet)


def build_search_packet() -> bytes:
    """Build the c0 discovery request answered by every hub on the segment."""
    return _build_request(_CMD_SEARCH)


def build_connect_packet(mac: str) -> bytes:
    """Build the c2 request asking the hub with `mac` to connect back."""
    raw = wire_mac(mac).encode("ascii", errors="strict")
    if len(raw) != 12:
        raise ValueError(f"AIFA hub MAC must be 12 characters, got {len(raw)}")
    return _build_request(_CMD_CONNECT, raw)


def parse_search_reply(data: bytes) -> str | None:
    """Return the MAC string from a c0 reply, or None for anything else."""
    if len(data) != LAN_PACKET_LENGTH or data[:3] != _SEARCH_REPLY_HEADER:
        return None
    try:
        return data[_MAC_SLICE].decode("ascii")
    except UnicodeDecodeError:
        return None


def compute_broadcast_addresses(adapters: Iterable[Mapping[str, Any]]) -> list[str]:
    """Return subnet broadcast addresses for every usable IPv4 adapter address.

    The `enabled` flag is ignored on purpose: HA's adapter list is cached and
    with default settings only the default adapter is marked enabled, while
    the hub may sit on another NIC. 255.255.255.255 is never used because it
    leaves through the default route only.
    """
    result: set[str] = set()
    for adapter in adapters:
        for entry in adapter.get("ipv4") or ():
            try:
                interface = ipaddress.IPv4Interface(
                    f"{entry['address']}/{int(entry['network_prefix'])}"
                )
            except (KeyError, TypeError, ValueError):
                continue
            if interface.network.prefixlen >= 31:
                continue
            if interface.ip.is_loopback or interface.ip.is_link_local:
                continue
            result.add(str(interface.network.broadcast_address))
    return sorted(result, key=ipaddress.IPv4Address)


class LanConnectTimeout(AifaSmartConnectionError):
    """The hub did not connect back after the c2 broadcasts."""


class LanSendResult(StrEnum):
    """Outcome of one LAN write; WRITE_FAILED means bytes may have left."""

    SENT = "sent"
    NOT_CONNECTED = "not_connected"
    WRITE_FAILED = "write_failed"


# A broadcast the OS refuses, e.g. ENOKEY on a VPN interface, fails again on
# every connect attempt, so each address and errno is logged once.
_reported_send_errors: set[tuple[str, int | None]] = set()


class _TargetedSender(asyncio.DatagramProtocol):
    """Ties an immediate sendto error to the address it was sent to."""

    def __init__(self) -> None:
        self.target: str | None = None
        self.failed: set[str] = set()

    def error_received(self, exc: Exception) -> None:
        if self.target is None:
            _LOGGER.debug("AIFA LAN broadcast error: %s", exc)
            return
        self.failed.add(self.target)
        key = (self.target, getattr(exc, "errno", None))
        if key in _reported_send_errors:
            return
        _reported_send_errors.add(key)
        _LOGGER.debug(
            "AIFA LAN broadcast to %s failed: %s; skipping it for this attempt",
            self.target,
            exc,
        )


def _send_to_each(
    transport: asyncio.DatagramTransport,
    sender: _TargetedSender,
    packet: bytes,
    targets: Iterable[str],
    port: int,
) -> None:
    for address in targets:
        if address in sender.failed:
            continue
        # The selector transport reports an immediate failure from inside
        # sendto, which is the only point where the address is known.
        sender.target = address
        try:
            transport.sendto(packet, (address, port))
        finally:
            sender.target = None


async def async_request_lan_connection(
    mac: str,
    broadcast_addrs: Iterable[str],
    *,
    attempts: int = 3,
    interval: float = 1.0,
    port_retries: int = 10,
    target_port: int = LAN_HUB_PORT,
    expected_peer: str | None = None,
    bind_host: str = "0.0.0.0",
) -> tuple[asyncio.StreamReader, asyncio.StreamWriter, int]:
    """Ask the hub to connect back and return (reader, writer, local_port).

    The hub connects to the UDP source port of the c2 request, so the UDP
    socket must be bound to the same port number as the TCP listener.
    """
    packet = build_connect_packet(mac)
    targets = list(broadcast_addrs)
    if not targets:
        raise LanConnectTimeout("No IPv4 broadcast address available")

    loop = asyncio.get_running_loop()
    accepted: asyncio.Future[tuple[asyncio.StreamReader, asyncio.StreamWriter]] = (
        loop.create_future()
    )

    def on_connect(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        peer = writer.get_extra_info("peername")
        peer_ip = peer[0] if peer else None
        if accepted.done() or (expected_peer is not None and peer_ip != expected_peer):
            writer.close()
            return
        accepted.set_result((reader, writer))

    server: asyncio.Server | None = None
    transport: asyncio.DatagramTransport | None = None
    sender: _TargetedSender | None = None
    returned = False
    try:
        for _ in range(max(1, port_retries)):
            server = await asyncio.start_server(on_connect, host=bind_host, port=0)
            port = server.sockets[0].getsockname()[1]
            try:
                transport, sender = await loop.create_datagram_endpoint(
                    _TargetedSender,
                    local_addr=(bind_host, port),
                    allow_broadcast=True,
                )
                break
            except OSError as err:
                if err.errno != errno.EADDRINUSE:
                    raise
                _LOGGER.debug("AIFA LAN UDP port %s busy, picking another", port)
                server.close()
                server = None
        if server is None or transport is None or sender is None:
            raise LanConnectTimeout("No free TCP/UDP port pair for the LAN request")

        for _ in range(max(1, attempts)):
            _send_to_each(transport, sender, packet, targets, target_port)
            try:
                reader, writer = await asyncio.wait_for(
                    asyncio.shield(accepted), timeout=interval
                )
            except asyncio.TimeoutError:
                continue
            enable_tcp_keepalive(writer.get_extra_info("socket"))
            returned = True
            return reader, writer, port
        raise LanConnectTimeout("AIFA hub did not connect back")
    finally:
        if not returned and accepted.done() and not accepted.cancelled():
            accepted.result()[1].close()
        if transport is not None:
            transport.close()
        if server is not None:
            # Never await server.wait_closed(): since Python 3.12.1 it also
            # waits for the accepted hub connection to close.
            server.close()
        if not accepted.done():
            accepted.cancel()


class _SearchProtocol(_TargetedSender):
    def __init__(self) -> None:
        super().__init__()
        self.replies: dict[str, str] = {}

    def datagram_received(self, data: bytes, addr: tuple[str, int]) -> None:
        mac = parse_search_reply(data)
        if mac is not None:
            self.replies[mac] = addr[0]


async def async_search_hubs(
    broadcast_addrs: Iterable[str],
    *,
    timeout: float = _SEARCH_TIMEOUT,
    target_port: int = LAN_HUB_PORT,
    source_port: int = LAN_SEARCH_SOURCE_PORT,
    bind_host: str = "0.0.0.0",
) -> dict[str, str]:
    """Broadcast a c0 search and return {mac as reported by the hub: ip}."""
    loop = asyncio.get_running_loop()
    try:
        transport, protocol = await loop.create_datagram_endpoint(
            _SearchProtocol, local_addr=(bind_host, source_port), allow_broadcast=True
        )
    except OSError:
        transport, protocol = await loop.create_datagram_endpoint(
            _SearchProtocol, local_addr=(bind_host, 0), allow_broadcast=True
        )
    try:
        packet = build_search_packet()
        _send_to_each(transport, protocol, packet, broadcast_addrs, target_port)
        await asyncio.sleep(timeout)
        return dict(protocol.replies)
    finally:
        transport.close()


async def async_default_broadcast_addresses(hass: HomeAssistant) -> list[str]:
    """Return broadcast addresses for every IPv4 adapter HA knows about."""
    from homeassistant.components import network

    return compute_broadcast_addresses(await network.async_get_adapters(hass))


@dataclass(slots=True)
class LanHubLink:
    """Live state of the LAN connection to one hub."""

    spec: AifaClassicSocketListenerSpec
    state: str = "idle"
    created_at: float = field(default_factory=time.monotonic)
    hub_ip: str | None = None
    local_port: int | None = None
    connected_at: datetime | None = None
    disconnected_at: float | None = None
    last_rx_at: datetime | None = None
    last_rx_monotonic: float | None = None
    last_status_at: datetime | None = None
    last_tx_at: datetime | None = None
    last_connect_ms: int | None = None
    writable_at: float | None = None
    consecutive_failures: int = 0
    reconnect_count: int = 0
    last_error: str | None = None
    writer: asyncio.StreamWriter | None = None
    write_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    connected_event: asyncio.Event = field(default_factory=asyncio.Event)
    wake_event: asyncio.Event = field(default_factory=asyncio.Event)
    on_demand_failed_at: float | None = None
    grace_timer: asyncio.TimerHandle | None = None

    @property
    def device_id(self) -> str:
        return self.spec.device_id

    @property
    def connected(self) -> bool:
        return self.writer is not None and not self.writer.is_closing()


ApplyLanObservedStatusFn = Callable[
    [AifaClassicSocketListenerSpec, int, AifaAcStatus], bool
]
ApplyLanSetCodeAckFn = Callable[[AifaClassicSocketListenerSpec, int, int, int], bool]
ApplyLanSetCodeRejectFn = Callable[[AifaClassicSocketListenerSpec], None]
ApplyLanCodeMatchFn = Callable[[AifaClassicSocketListenerSpec, bytes], bool]
BroadcastProviderFn = Callable[[], Awaitable[list[str]]]


class LanTransportManager:
    """Keeps one LAN connection per hub and sends raw packets over it."""

    def __init__(
        self,
        *,
        hass: HomeAssistant,
        on_observed_status: ApplyLanObservedStatusFn,
        on_data_changed: Callable[[], None],
        broadcast_provider: BroadcastProviderFn | None = None,
        target_port: int = LAN_HUB_PORT,
        search_source_port: int = LAN_SEARCH_SOURCE_PORT,
        bind_host: str = "0.0.0.0",
        backoff_schedule: tuple[float, ...] = LAN_BACKOFF_SCHEDULE,
        connect_attempts: int = 3,
        connect_interval: float = 1.0,
        search_timeout: float = _SEARCH_TIMEOUT,
        idle_query_after: float = LAN_IDLE_QUERY_SECONDS,
        query_reply_timeout: float = LAN_QUERY_REPLY_TIMEOUT,
        recent_rx_window: float = LAN_RECENT_RX_SECONDS,
        on_link_state_changed: Callable[[], None] | None = None,
        unavailable_grace: float = LAN_UNAVAILABLE_GRACE_SECONDS,
        connect_write_delay: float = LAN_CONNECT_WRITE_DELAY,
        on_set_code_ack: ApplyLanSetCodeAckFn | None = None,
        on_set_code_reject: ApplyLanSetCodeRejectFn | None = None,
        on_code_match: ApplyLanCodeMatchFn | None = None,
    ) -> None:
        self._hass = hass
        self._on_set_code_ack = on_set_code_ack
        self._on_set_code_reject = on_set_code_reject
        self._on_code_match = on_code_match
        self._on_observed_status = on_observed_status
        self._on_data_changed = on_data_changed
        self._broadcast_provider = broadcast_provider or (
            lambda: async_default_broadcast_addresses(hass)
        )
        self._target_port = target_port
        self._search_source_port = search_source_port
        self._bind_host = bind_host
        self._backoff_schedule = backoff_schedule
        self._connect_attempts = connect_attempts
        self._connect_interval = connect_interval
        self._search_timeout = search_timeout
        self._idle_query_after = idle_query_after
        self._query_reply_timeout = query_reply_timeout
        self._recent_rx_window = recent_rx_window
        self._on_link_state_changed = on_link_state_changed
        self._unavailable_grace = unavailable_grace
        self._connect_write_delay = connect_write_delay
        self._links: dict[str, LanHubLink] = {}
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._discovered_macs: dict[str, str] = {}
        self._last_search_at: float | None = None
        self._search_lock = asyncio.Lock()
        self._closed = False
        self.broadcast_addresses: list[str] = []

    # ----- Public API -----

    @property
    def links(self) -> dict[str, LanHubLink]:
        return self._links

    def is_connected(self, device_id: str) -> bool:
        link = self._links.get(device_id)
        return link is not None and link.connected

    def is_available(self, device_id: str) -> bool:
        """Connected, or down for less than the grace period (never-up counts from start)."""
        link = self._links.get(device_id)
        if link is None:
            return False
        if link.connected:
            return True
        down_since = (
            link.disconnected_at if link.disconnected_at is not None else link.created_at
        )
        return time.monotonic() - down_since < self._unavailable_grace

    def sync_specs(self, devices: dict[str, AifaDevice]) -> None:
        """Track hubs with supported AC targets; the cloud online flag is ignored."""
        if self._closed:
            # A refresh still in flight during unload must not restart links.
            return
        specs = build_hub_specs(devices, require_online=False)
        for device_id in tuple(self._links):
            if specs.get(device_id) != self._links[device_id].spec:
                self._stop_link(device_id)
        for device_id, spec in specs.items():
            if device_id in self._links:
                continue
            link = LanHubLink(spec=spec)
            self._links[device_id] = link
            self._arm_grace_timer(link)
            self._tasks[device_id] = self._hass.async_create_background_task(
                self._async_run_link(link),
                name=f"{DOMAIN}_lan_{device_id}",
            )

    async def async_ensure_connected(self, device_id: str, *, timeout: float) -> bool:
        """Cut any backoff short and wait up to `timeout` for a live link."""
        link = self._links.get(device_id)
        if link is None:
            return False
        if link.connected:
            return True
        link.wake_event.set()
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        # The event can still be set for a link that just dropped, before its
        # task clears it; keep waiting for the next connection in that case.
        while not link.connected:
            remaining = deadline - loop.time()
            if remaining <= 0:
                return False
            try:
                await asyncio.wait_for(link.connected_event.wait(), timeout=remaining)
            except asyncio.TimeoutError:
                return False
            if not link.connected:
                await asyncio.sleep(min(0.05, max(remaining, 0)))
        return True

    async def async_send_packet(
        self, device_id: str, packet_hex: str, *, connect_timeout: float
    ) -> LanSendResult:
        """Write one raw packet to the hub over the LAN link."""
        link = self._links.get(device_id)
        if link is None:
            return LanSendResult.NOT_CONNECTED
        try:
            data = bytes.fromhex(packet_hex.strip().lower())
        except ValueError:
            return LanSendResult.NOT_CONNECTED
        if not data:
            return LanSendResult.NOT_CONNECTED
        if not link.connected and not await self.async_ensure_connected(
            device_id, timeout=connect_timeout
        ):
            link.on_demand_failed_at = time.monotonic()
            return LanSendResult.NOT_CONNECTED

        writer = link.writer
        if writer is None:
            return LanSendResult.NOT_CONNECTED
        async with link.write_lock:
            if not await self._async_wait_writable(link, writer):
                return LanSendResult.NOT_CONNECTED
            try:
                writer.write(data)
                link.writable_at = time.monotonic() + queue_delay(data)
                await writer.drain()
            except (OSError, RuntimeError) as err:
                link.last_error = f"write_failed: {err!r}"
                # Closing ends the read loop, which reconnects; a socket that
                # failed a write is not trusted with the next one.
                writer.close()
                return LanSendResult.WRITE_FAILED
        link.last_tx_at = dt_util.utcnow()
        return LanSendResult.SENT

    async def async_request_status(self, device_id: str) -> bool:
        """Ask a connected hub to push its AC status; replies arrive as packets.

        An unanswered query leaves the link up: the hub ignores queries for
        a while after some keys, and a half-open link is caught by
        TCP_USER_TIMEOUT on the unacknowledged write (Linux only) or by the
        idle check.
        """
        link = self._links.get(device_id)
        if link is None or link.writer is None or link.writer.is_closing():
            return False
        return await self._async_write_query(link, link.writer) is not None

    def snapshot(self) -> dict[str, dict[str, Any]]:
        """Diagnostics-friendly view of every link."""

        def iso(value: datetime | None) -> str | None:
            return value.isoformat() if value is not None else None

        return {
            device_id: {
                "state": link.state,
                "connected": link.connected,
                "hub_ip": link.hub_ip,
                "local_port": link.local_port,
                "connected_at": iso(link.connected_at),
                "last_rx_at": iso(link.last_rx_at),
                "last_status_at": iso(link.last_status_at),
                "last_tx_at": iso(link.last_tx_at),
                "reconnect_count": link.reconnect_count,
                "consecutive_failures": link.consecutive_failures,
                "last_error": link.last_error,
                "last_connect_ms": link.last_connect_ms,
            }
            for device_id, link in self._links.items()
        }

    async def shutdown(self) -> None:
        """Stop every link and close its connection."""
        self._closed = True
        tasks = [task for task in self._tasks.values() if not task.done()]
        for device_id in tuple(self._links):
            self._stop_link(device_id)
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    # ----- Lifecycle -----

    def _notify_link_state(self) -> None:
        if self._on_link_state_changed is not None:
            self._on_link_state_changed()

    def _arm_grace_timer(self, link: LanHubLink) -> None:
        """Re-evaluate availability when the grace period of a down link runs out."""
        if self._on_link_state_changed is None:
            return
        self._cancel_grace_timer(link)
        link.grace_timer = asyncio.get_running_loop().call_later(
            self._unavailable_grace + 0.1, self._notify_link_state
        )

    @staticmethod
    def _cancel_grace_timer(link: LanHubLink) -> None:
        if link.grace_timer is not None:
            link.grace_timer.cancel()
            link.grace_timer = None

    def _stop_link(self, device_id: str) -> None:
        link = self._links.pop(device_id, None)
        if link is not None:
            link.state = "stopped"
            self._cancel_grace_timer(link)
            if link.writer is not None:
                link.writer.close()
        task = self._tasks.pop(device_id, None)
        if task is not None and not task.done():
            task.cancel()

    def _backoff_delay(self, failures: int) -> float:
        index = min(max(failures - 1, 0), len(self._backoff_schedule) - 1)
        return self._backoff_schedule[index]

    async def _async_backoff(self, link: LanHubLink) -> None:
        link.state = "backoff"
        try:
            await asyncio.wait_for(
                link.wake_event.wait(),
                timeout=self._backoff_delay(link.consecutive_failures),
            )
        except asyncio.TimeoutError:
            pass
        link.wake_event.clear()

    async def _async_resolve_mac(self, link: LanHubLink, broadcast: list[str]) -> str:
        """Prefer the MAC string exactly as the hub reports it in a c0 reply."""
        key = normalize_mac(link.spec.mac)
        if key not in self._discovered_macs:
            async with self._search_lock:
                now = time.monotonic()
                if key not in self._discovered_macs and (
                    self._last_search_at is None
                    or now - self._last_search_at >= _SEARCH_MIN_INTERVAL
                ):
                    self._last_search_at = now
                    try:
                        replies = await async_search_hubs(
                            broadcast,
                            timeout=self._search_timeout,
                            target_port=self._target_port,
                            source_port=self._search_source_port,
                            bind_host=self._bind_host,
                        )
                    except OSError as err:
                        _LOGGER.debug("AIFA LAN search failed: %s", err)
                        replies = {}
                    for raw_mac in replies:
                        self._discovered_macs[normalize_mac(raw_mac)] = raw_mac
        return self._discovered_macs.get(key) or wire_mac(link.spec.mac)

    async def _async_run_link(self, link: LanHubLink) -> None:
        try:
            while self._links.get(link.device_id) is link:
                link.state = "connecting"
                started = time.monotonic()
                try:
                    broadcast = await self._broadcast_provider()
                    self.broadcast_addresses = broadcast
                    mac = await self._async_resolve_mac(link, broadcast)
                    reader, writer, port = await async_request_lan_connection(
                        mac,
                        broadcast,
                        attempts=self._connect_attempts,
                        interval=self._connect_interval,
                        target_port=self._target_port,
                        expected_peer=link.hub_ip,
                        bind_host=self._bind_host,
                    )
                except asyncio.CancelledError:
                    raise
                except Exception as err:  # noqa: BLE001 - keep the link loop alive
                    link.consecutive_failures += 1
                    link.last_error = (
                        "connect_timeout"
                        if isinstance(err, LanConnectTimeout)
                        else f"connect_failed: {err!r}"
                    )
                    if link.consecutive_failures >= _HUB_IP_FORGET_AFTER_FAILURES:
                        link.hub_ip = None
                    _LOGGER.debug(
                        "AIFA LAN connect to %s failed: %s", link.device_id, err
                    )
                    await self._async_backoff(link)
                    continue

                peer = writer.get_extra_info("peername")
                if link.connected_at is not None:
                    link.reconnect_count += 1
                link.hub_ip = peer[0] if peer else link.hub_ip
                link.local_port = port
                link.connected_at = dt_util.utcnow()
                link.last_connect_ms = int((time.monotonic() - started) * 1000)
                failures_before = link.consecutive_failures
                connected_since = time.monotonic()
                link.writable_at = connected_since + self._connect_write_delay
                link.consecutive_failures = 0
                link.last_error = None
                link.writer = writer
                link.state = "connected"
                link.disconnected_at = None
                link.connected_event.set()
                self._cancel_grace_timer(link)
                self._notify_link_state()
                try:
                    await self._async_consume(link, reader, writer)
                except asyncio.CancelledError:
                    raise
                except Exception as err:  # noqa: BLE001 - keep the link loop alive
                    link.last_error = f"read_failed: {err!r}"
                    _LOGGER.debug("AIFA LAN link %s failed: %s", link.device_id, err)
                finally:
                    link.connected_event.clear()
                    link.writer = None
                    link.writable_at = None
                    link.disconnected_at = time.monotonic()
                    writer.close()
                if self._links.get(link.device_id) is not link:
                    break
                self._arm_grace_timer(link)
                self._notify_link_state()
                if link.last_error is None:
                    link.last_error = "disconnected"
                if time.monotonic() - connected_since < _MIN_HEALTHY_SECONDS:
                    link.consecutive_failures = failures_before + 1
                await self._async_backoff(link)
        finally:
            if link.writer is not None:
                link.writer.close()
                link.writer = None
            link.connected_event.clear()

    @staticmethod
    async def _async_wait_writable(
        link: LanHubLink, writer: asyncio.StreamWriter
    ) -> bool:
        """Hold a write until the hub listens and the queue delay of the
        previous write has passed; False if the link went away.

        Callers wait while holding the write lock so writes keep their order.
        """
        if link.writable_at is not None:
            delay = link.writable_at - time.monotonic()
            if delay > 0:
                await asyncio.sleep(delay)
        return link.writer is writer and not writer.is_closing()

    async def _async_write_query(
        self, link: LanHubLink, writer: asyncio.StreamWriter
    ) -> float | None:
        """Send a status query; returns the monotonic time it went out."""
        async with link.write_lock:
            if not await self._async_wait_writable(link, writer):
                return None
            # Taken before the write: a reply may be read while drain waits.
            sent_at = time.monotonic()
            query = bytes.fromhex(LAN_STATUS_QUERY_PACKET)
            try:
                writer.write(query)
                link.writable_at = sent_at + queue_delay(query)
                await writer.drain()
            except (OSError, RuntimeError) as err:
                link.last_error = f"query_write_failed: {err!r}"
                writer.close()
                return None
        link.last_tx_at = dt_util.utcnow()
        return sent_at

    async def _async_consume(
        self,
        link: LanHubLink,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        """Read the hub stream until EOF or until an idle query goes unanswered."""
        buffers = ClassicStreamBuffers()
        spec = link.spec
        loop = asyncio.get_running_loop()
        idle_from = loop.time()
        # Bytes from an earlier connection say nothing about this one.
        heard_at: float | None = None
        query_sent_at: float | None = None

        async def reply_wake() -> None:
            async with link.write_lock:
                if not await self._async_wait_writable(link, writer):
                    return
                writer.write(bytes.fromhex(CLASSIC_WAKE_REPLY_PACKET))
                await writer.drain()

        def on_status(sub_type: int, observed: AifaAcStatus) -> bool:
            link.last_status_at = dt_util.utcnow()
            return self._on_observed_status(spec, sub_type, observed)

        def on_set_code_ack(slot: int, high: int, low: int) -> bool:
            if self._on_set_code_ack is None:
                return False
            return self._on_set_code_ack(spec, slot, high, low)

        def on_set_code_reject() -> None:
            if self._on_set_code_reject is not None:
                self._on_set_code_reject(spec)

        def on_code_match(packet: bytes) -> bool:
            if self._on_code_match is None:
                return False
            return self._on_code_match(spec, packet)

        def on_sensor(sensor: AifaClassicSensorState) -> bool:
            _LOGGER.debug("AIFA LAN sensor packet from %s: %s", spec.device_id, sensor)
            return False

        while self._links.get(spec.device_id) is link:
            if query_sent_at is None:
                deadline = idle_from + self._idle_query_after
            else:
                deadline = query_sent_at + self._query_reply_timeout
            try:
                chunk = await asyncio.wait_for(
                    reader.read(4096), timeout=max(0.0, deadline - loop.time())
                )
            except asyncio.TimeoutError:
                if query_sent_at is not None:
                    now = loop.time()
                    if heard_at is not None and now - heard_at < self._recent_rx_window:
                        # Ask again later; heard_at only moves on new bytes,
                        # so a silent link still drops once it ages out.
                        query_sent_at = None
                        idle_from = now
                        continue
                    link.last_error = "idle_timeout"
                    _LOGGER.debug(
                        "AIFA LAN link %s did not answer a status query; reconnecting",
                        spec.device_id,
                    )
                    return
                if await self._async_write_query(link, writer) is None:
                    return
                query_sent_at = loop.time()
                continue
            if not chunk:
                return
            heard_at = idle_from = loop.time()
            query_sent_at = None
            link.last_rx_at = dt_util.utcnow()
            link.last_rx_monotonic = time.monotonic()
            if await process_classic_stream_chunk(
                chunk,
                buffers,
                device_type=spec.device_type,
                reply_wake=reply_wake,
                on_status=on_status,
                on_sensor=on_sensor,
                on_notify=lambda: None,
                on_set_code_ack=on_set_code_ack,
                on_set_code_reject=on_set_code_reject,
                on_code_match=on_code_match,
            ):
                self._on_data_changed()
