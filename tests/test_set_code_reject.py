"""The hub's `fb f5` refusal of a SetCode.

The hub answers SetCode for codes it does not support with the two bytes `fb f5`, with
no slot, code or end byte, and keeps its code. It echoes a code it takes as
`fa f5 <slot> <hi> <lo> f0`.
"""
from __future__ import annotations

import asyncio
import time
import types
from unittest.mock import patch

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType

from custom_components.aifa_smart import coordinator as coordinator_module
from custom_components.aifa_smart.api import extract_set_code_reject_packets
from custom_components.aifa_smart.classic_listener import (
    ClassicSocketManager,
    ClassicStreamBuffers,
    build_hub_specs,
    process_classic_stream_chunk,
)
from custom_components.aifa_smart.coordinator import (
    DeviceCodeChangeError,
    _HubReportWaiter,
)
from tests.test_command_confirmation import _build_coordinator
from tests.test_device_code_change import (
    FakeClient,
    Rig,
    SetCodeFakeHub,
    _fast_set_code,
    _report,
    _rows,
)
from tests.test_lan_fake_hub import _device

pytestmark = pytest.mark.usefixtures("socket_enabled")

_REJECT = b"\xfb\xf5"


@pytest.fixture(autouse=True)
def bypass_setup_entry():
    with patch("custom_components.aifa_smart.async_setup_entry", return_value=True):
        yield


# ----- Parsing -----


def test_reject_is_two_bytes() -> None:
    assert extract_set_code_reject_packets(_REJECT) == ([_REJECT], b"")


def test_reject_split_across_chunks() -> None:
    packets, rest = extract_set_code_reject_packets(b"\x00\xfb")
    assert packets == []
    assert rest == b"\xfb"
    packets, rest = extract_set_code_reject_packets(rest + b"\xf5" + _report(0x01, 0x15))
    assert packets == [_REJECT]
    assert rest == b""


def test_fb_f5_inside_other_frames_is_not_a_reject() -> None:
    status_with_fb_f5 = bytes([0xFE, 0xA0, 0x01, 0x28, 0x25, 0x00, 0x24, 0x11, 0x00, 0x00, 0xFB, 0xF5, 0x00, 0xF0])
    notify_with_fb_f5 = bytes([0xFA, 0xF5, 0x00, 0xFB, 0xF5, 0xF0])
    wake_with_fb = bytes([0xF1, 0xEE, 0xFB, 0xF0])
    assert extract_set_code_reject_packets(status_with_fb_f5 + notify_with_fb_f5 + wake_with_fb) == ([], b"")


def test_frame_cut_off_with_fb_f5_inside_waits_for_the_rest() -> None:
    status = bytes([0xFE, 0xA0, 0x01, 0x28, 0x25, 0x00, 0x24, 0x11, 0x00, 0x00, 0xFB, 0xF5, 0x00, 0xF0])
    packets, rest = extract_set_code_reject_packets(status[:12])
    assert packets == []
    assert rest == status[:12]
    packets, rest = extract_set_code_reject_packets(rest + status[12:] + _REJECT)
    assert packets == [_REJECT]
    assert rest == b""


async def _feed(chunks: list[bytes]) -> tuple[int, list[int], int, int]:
    """Return (rejects, status slots, wake replies, acks) for a run of chunks."""
    rejects: list[None] = []
    statuses: list[int] = []
    wakes: list[None] = []
    acks: list[tuple[int, int, int]] = []
    buffers = ClassicStreamBuffers()

    async def wake() -> None:
        wakes.append(None)

    for chunk in chunks:
        await process_classic_stream_chunk(
            chunk,
            buffers,
            device_type=None,
            reply_wake=wake,
            on_status=lambda sub_type, observed: statuses.append(sub_type) or False,
            on_sensor=lambda sensor: False,
            on_notify=lambda: None,
            on_set_code_ack=lambda *ack: acks.append(ack) or False,
            on_set_code_reject=lambda: rejects.append(None),
        )
    return len(rejects), statuses, len(wakes), len(acks)


async def test_stream_parser_finds_a_split_reject_next_to_a_status_and_a_wake() -> None:
    wake = bytes.fromhex("f1ee00f0")
    result = await _feed([b"\xfb", b"\xf5" + _report(0x01, 0x15) + wake])
    assert result == (1, [0], 1, 0)


async def test_stream_parser_keeps_status_and_echo_with_fb_f5_bytes_intact() -> None:
    status = bytes([0xFE, 0xA0, 0x01, 0x15, 0x25, 0x00, 0x24, 0x11, 0x00, 0x00, 0xFB, 0xF5, 0x00, 0xF0])
    echo = bytes.fromhex("faf5a00033f0")
    result = await _feed([status[:11], status[11:] + echo])
    assert result == (0, [0], 0, 1)


# ----- Claim rule -----


def test_only_a_code_change_takes_a_reject() -> None:
    coordinator = _build_coordinator("lan")
    command_waiter = _HubReportWaiter()
    change_waiter = _HubReportWaiter(changing_code=True)
    coordinator._hub_report_waiters["7001"] = [command_waiter, change_waiter]  # noqa: SLF001
    coordinator._record_set_code_reject("7001", "lan")  # noqa: SLF001
    assert command_waiter.rejects == []
    assert not command_waiter.event.is_set()
    assert change_waiter.rejects == ["lan"]


def test_reject_with_nobody_waiting_is_ignored() -> None:
    coordinator = _build_coordinator("lan")
    coordinator._record_set_code_reject("7001", "lan")  # noqa: SLF001
    assert coordinator._hub_report_waiters == {}  # noqa: SLF001


# ----- Fake hub, LAN end to end -----


class RejectingFakeHub(SetCodeFakeHub):
    """SetCodeFakeHub that answers `fb f5` for `rejected_codes`.

    split_reject:      send `fb` and `f5` in two writes
    status_after:      a status report right behind `f5`, in the same write
    stores_rejected:   store the code anyway (a hub that lies)
    reject_delay:      seconds before `fb f5` goes out; longer than the client's
                       write pacing after a SetCode, so its query follows at once
    deaf_after_reject: seconds after `fb f5` during which received packets are dropped
    """

    def __init__(self) -> None:
        super().__init__()
        self.rejected_codes: set[int] = set()
        self.split_reject = False
        self.status_after = False
        self.stores_rejected = False
        self.reject_delay = 0.4
        self.deaf_after_reject = 0.2

    async def _connect_back(self, addr: tuple[str, int]) -> None:
        try:
            reader, writer = await asyncio.open_connection(addr[0], addr[1])
        except OSError:
            return
        self.connect_ports.append(addr[1])
        self.writers.append(writer)
        pending = b""
        deaf_until = 0.0
        while True:
            data = await reader.read(4096)
            if not data:
                self.eof_count += 1
                return
            self.received.extend(data)
            if time.monotonic() < deaf_until:
                continue
            pending += data
            while pending:
                if pending.startswith(bytes.fromhex("fff3f2f0")):
                    pending = pending[4:]
                    self.packets.append("fff3f2f0")
                    if self.answer_queries:
                        await self._send(writer, _report(*self.code))
                elif pending[:2] == b"\xff\xf5" and len(pending) >= 6:
                    packet, pending = pending[:6], pending[6:]
                    self.packets.append(packet.hex())
                    code = int(packet[3:5].hex())
                    if code in self.rejected_codes:
                        if self.stores_rejected:
                            self.codes[packet[2]] = (packet[3], packet[4])
                        await asyncio.sleep(self.reject_delay)
                        deaf_until = time.monotonic() + self.deaf_after_reject
                        tail = _report(*self.code) if self.status_after else b""
                        if self.split_reject:
                            await self._send(writer, b"\xfb")
                            await asyncio.sleep(0.05)
                            await self._send(writer, b"\xf5" + tail)
                        else:
                            await self._send(writer, _REJECT + tail)
                        continue
                    if code not in self.ignored_codes:
                        self.codes[packet[2]] = (packet[3], packet[4])
                    if code not in self.silent_codes:
                        await asyncio.sleep(0.05)
                        await self._send(writer, b"\xfa" + packet[1:])
                elif pending[0] == 0xFF and len(pending) < 6:
                    break
                else:
                    pending = pending[1:]


@pytest.fixture
async def rig(hass: HomeAssistant):
    hub = await RejectingFakeHub().start()
    built = Rig(hass, hub)
    # Same wiring as AifaSmartCoordinator.__init__.
    built.manager._on_set_code_reject = built.coordinator._apply_lan_set_code_reject  # noqa: SLF001
    with _fast_set_code():
        await built.connect()
        yield built
    await built.close()


async def _choose_timed(rig: Rig, code: str):
    started = time.monotonic()
    # The real ack timeout, so only the refusal can end the wait early.
    with patch.object(coordinator_module, "SET_CODE_ACK_TIMEOUT", 3.0):
        result = await rig.choose(code)
    return result, time.monotonic() - started


@pytest.mark.usefixtures("enable_custom_integrations")
@pytest.mark.parametrize(("split", "status_after"), [(False, False), (True, True), (False, True)])
async def test_refused_code_ends_at_once_without_patch_or_restore(
    rig: Rig, split: bool, status_after: bool
) -> None:
    rig.hub.rejected_codes = {33}
    rig.hub.split_reject = split
    rig.hub.status_after = status_after

    result, elapsed = await _choose_timed(rig, "33")

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "set_code_rejected"}
    assert result["description_placeholders"]["new_code"] == "33"
    assert result["description_placeholders"]["current_code"] == "115"
    # The reject settle (0.5 s) plus the exchange; a wait that ran to the
    # 3 s ack timeout would be well above this.
    assert elapsed < 1.5
    # One check of the old code; no SetCode to restore it.
    assert rig.hub.packets == ["fff5a00033f0", "fff3f2f0"]
    assert rig.hub.code == (0x01, 0x15)
    assert rig.client.patches == []
    assert rig.sub_device.device_code == "115"
    rig.reload.assert_not_called()


@pytest.mark.usefixtures("enable_custom_integrations")
async def test_query_right_after_the_reject_is_lost_without_the_settle(rig: Rig) -> None:
    # Positive control: the hub drops what arrives just after `fb f5`.
    rig.hub.rejected_codes = {33}
    with patch.object(coordinator_module, "SET_CODE_REJECT_SETTLE", 0):
        result = await rig.choose("33")

    assert result["errors"] == {"base": "set_code_rejected_unverified"}


@pytest.mark.usefixtures("enable_custom_integrations")
async def test_refusal_with_the_code_changed_anyway_restores(rig: Rig) -> None:
    rig.hub.rejected_codes = {33}
    rig.hub.stores_rejected = True

    result = await rig.choose("33")

    assert result["errors"] == {"base": "set_code_rejected_unverified"}
    assert rig.hub.packets == ["fff5a00033f0", "fff3f2f0", "fff5a00115f0", "fff3f2f0"]
    assert rig.hub.code == (0x01, 0x15)
    assert rig.client.patches == []
    assert rig.sub_device.device_code == "115"


@pytest.mark.usefixtures("enable_custom_integrations")
async def test_refusal_without_a_report_restores(rig: Rig) -> None:
    rig.hub.rejected_codes = {33}
    rig.hub.answer_queries = False

    result = await rig.choose("33")

    # Nothing showed the hub kept 115, so it is set back and that fails too.
    assert result["errors"] == {"base": "restore_unconfirmed"}
    assert rig.set_codes_sent() == ["fff5a00033f0", "fff5a00115f0"]
    assert rig.client.patches == []


@pytest.mark.usefixtures("enable_custom_integrations")
async def test_refused_restore_counts_when_the_query_shows_the_old_code(rig: Rig) -> None:
    # 33 goes unechoed, so 115 is set back; that SetCode draws fb f5, as a
    # late refusal would, yet the hub reports 115.
    rig.hub.silent_codes = {33}
    rig.hub.rejected_codes = {115}
    rig.hub.stores_rejected = True

    result = await rig.choose("33")

    assert result["errors"] == {"base": "set_code_unconfirmed"}
    assert rig.hub.packets == ["fff5a00033f0", "fff5a00115f0", "fff3f2f0"]
    assert rig.hub.code == (0x01, 0x15)
    assert rig.client.patches == []


@pytest.mark.usefixtures("enable_custom_integrations")
async def test_reject_outside_a_code_change_changes_nothing(rig: Rig) -> None:
    for writer in rig.hub.writers:
        writer.write(_REJECT)
        await writer.drain()
    await asyncio.sleep(0.1)
    assert rig.coordinator._hub_report_waiters == {}  # noqa: SLF001

    result = await rig.choose("33")

    assert result["reason"] == "device_code_changed"
    assert rig.hub.packets == ["fff5a00033f0", "fff3f2f0"]
    assert rig.client.patches == [("ac-1", 33)]


# ----- Cloud end to end -----


class _RejectingCloud:
    """Cloud relay that feeds the hub's bytes through ClassicSocketManager's parser."""

    def __init__(self, coordinator) -> None:
        self.sent: list[str] = []
        self.code = (0x01, 0x15)
        self.rejected_codes: set[int] = set()
        manager = ClassicSocketManager.__new__(ClassicSocketManager)
        # Same wiring as AifaSmartCoordinator.__init__.
        manager._on_observed_status = coordinator._apply_cloud_stream_status  # noqa: SLF001
        manager._on_sensor_state = lambda *args: False  # noqa: SLF001
        manager._on_set_code_ack = coordinator._apply_cloud_set_code_ack  # noqa: SLF001
        manager._on_set_code_reject = coordinator._apply_cloud_set_code_reject  # noqa: SLF001
        manager._on_code_match = coordinator._apply_cloud_code_match  # noqa: SLF001
        manager._hass = None  # noqa: SLF001
        self.manager = manager
        self.buffers = ClassicStreamBuffers()
        self.spec = build_hub_specs(coordinator.data.devices, require_online=False)["7001"]

    def has_active_listener(self, device_id: str) -> bool:
        return True

    def has_active_writer(self, device_id: str) -> bool:
        return True

    async def async_send_packet(self, device_id: str, packet_hex: str) -> bool:
        self.sent.append(packet_hex)
        packet = bytes.fromhex(packet_hex)
        if packet_hex == "fff3f2f0":
            self._later(_report(*self.code))
        elif packet[:2] == b"\xff\xf5":
            if int(packet[3:5].hex()) in self.rejected_codes:
                self._later(b"\xfb", b"\xf5" + _report(*self.code))
            else:
                self.code = (packet[3], packet[4])
                self._later(b"\xfa" + packet[1:])
        return True

    def _later(self, *chunks: bytes) -> None:
        async def deliver() -> None:
            for chunk in chunks:
                await asyncio.sleep(0.02)
                await self.manager._async_process_chunk(  # noqa: SLF001
                    self.spec, chunk, None, self.buffers  # type: ignore[arg-type]
                )

        asyncio.get_running_loop().create_task(deliver())


async def test_cloud_refusal_ends_at_once_without_patch_or_restore() -> None:
    device = _device()
    device.sub_devices[0].device_code = "115"
    coordinator = _build_coordinator("cloud", device=device)
    cloud = _RejectingCloud(coordinator)
    cloud.rejected_codes = {33}
    coordinator._classic_listener = cloud  # noqa: SLF001
    client = FakeClient()
    coordinator.client = client  # type: ignore[attr-defined]
    coordinator._device_code_checked = set()  # noqa: SLF001
    row = next(row for row in _rows() if row.code == "33")

    started = time.monotonic()
    with _fast_set_code(), patch.object(coordinator_module, "SET_CODE_ACK_TIMEOUT", 3.0):
        with pytest.raises(DeviceCodeChangeError) as err:
            await coordinator._async_change_device_code("7001", "ac-1", row)  # noqa: SLF001
    assert err.value.reason == "set_code_rejected"
    assert time.monotonic() - started < 1.5
    assert cloud.sent == ["fff5a00033f0", "fff3f2f0"]
    assert client.patches == []
    assert coordinator.get_sub_device("7001", "ac-1").device_code == "115"


async def test_cloud_accepted_code_is_unaffected() -> None:
    device = _device()
    device.sub_devices[0].device_code = "115"
    coordinator = _build_coordinator("cloud", device=device)
    cloud = _RejectingCloud(coordinator)
    coordinator._classic_listener = cloud  # noqa: SLF001
    client = FakeClient()
    coordinator.client = client  # type: ignore[attr-defined]
    coordinator._device_code_checked = set()  # noqa: SLF001
    coordinator._device_code_catalogs = {}  # noqa: SLF001
    coordinator._device_cache = None  # noqa: SLF001
    entry = types.SimpleNamespace(entry_id="e", options={})
    coordinator.entry = entry  # type: ignore[attr-defined]
    reloads: list[str] = []

    def update_entry(target, *, options) -> None:
        target.options = options

    coordinator.hass.config_entries = types.SimpleNamespace(  # type: ignore[attr-defined]
        async_schedule_reload=reloads.append, async_update_entry=update_entry
    )
    row = next(row for row in _rows() if row.code == "33")

    with _fast_set_code(), patch.object(coordinator_module.ir, "async_delete_issue"):
        assert await coordinator._async_change_device_code("7001", "ac-1", row)  # noqa: SLF001
    assert cloud.sent == ["fff5a00033f0", "fff3f2f0"]
    assert client.patches == [("ac-1", 33)]
    assert reloads == ["e"]

