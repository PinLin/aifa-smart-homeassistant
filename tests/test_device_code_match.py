"""Finding a remote code with the hub's IR receiver, like the AIFA Smart app's one-touch match.

A v5/210623 hub answers StartMatch `ff f5 f4 f1 a0 f0` with
`cc dd`, sends the first candidate `fa f5 a0 00 33 f0` about 2 s after the
remote's power key, one more for every NextMatch `ff f5 f4 f3 a0 f0`, and
ends with `fa f5 a0 ff ff f0`. The cases run the options flow against a
fake hub on 127.0.0.1 that does the same.
"""
from __future__ import annotations

import asyncio
import time
import types
from contextlib import ExitStack
from dataclasses import replace
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.exceptions import HomeAssistantError

from custom_components.aifa_smart import coordinator as coordinator_module
from custom_components.aifa_smart.ac import (
    CodeMatchReply,
    build_code_match_next_command,
    build_code_match_start_command,
    parse_code_match_packet,
    parse_set_code_ack,
)
from custom_components.aifa_smart.api import (
    AifaDeviceCode,
    AifaFunctionCommand,
    AifaDeviceCodeCatalog,
    extract_classic_status_packets,
    extract_code_match_packets,
    extract_set_code_ack_packets,
    extract_set_code_reject_packets,
)
from custom_components.aifa_smart.classic_listener import (
    ClassicSocketManager,
    ClassicStreamBuffers,
    build_hub_specs,
    process_classic_stream_chunk,
)
from custom_components.aifa_smart.config_flow import _candidate_rows
from custom_components.aifa_smart.const import CONF_SELECTED_BRANDS
from custom_components.aifa_smart.coordinator import CodeTrial, DeviceCodeChangeError, _HubReportWaiter
from tests.test_command_confirmation import _build_coordinator
from tests.test_device_code_change import Rig, _fast_set_code, _report
from tests.test_device_code_trial import TrialClient, TrialFakeHub
from tests.test_lan_fake_hub import _device, _wait_until

pytestmark = pytest.mark.usefixtures("socket_enabled", "enable_custom_integrations")

_START = "fff5f4f1a0f0"
_NEXT = "fff5f4f3a0f0"
_KEY = "ffa0a0f0"
_QUERY = bytes.fromhex("fff3f2f0")
_END = bytes.fromhex("faf5a0fffff0")
_END4 = bytes.fromhex("faf5a000")
_FAIL = bytes.fromhex("fbf5a0f0")


def _candidate(code: int, slot: int = 0xA0) -> bytes:
    return bytes([0xFA, 0xF5, slot]) + bytes.fromhex(f"{code:04d}") + b"\xf0"


@pytest.fixture(autouse=True)
def bypass_setup_entry():
    with patch("custom_components.aifa_smart.async_setup_entry", return_value=True):
        yield


class MatchFakeHub(TrialFakeHub):
    """TrialFakeHub that also runs a code match.

    replies:     what the hub sends after the remote's key, then one per NextMatch
    press_after: seconds between StartMatch and the first reply (the user's press)
    log:         ("start" | "next" | "reply", hex) in order
    """

    def __init__(self) -> None:
        super().__init__()
        self.replies: list[bytes] = []
        self.press_after = 0.05
        self.log: list[tuple[str, str]] = []
        self._next_reply = 0
        self._reply_tasks: set[asyncio.Task[None]] = set()

    def _schedule_reply(self, writer: asyncio.StreamWriter, delay: float) -> None:
        task = asyncio.get_running_loop().create_task(self._reply(writer, delay))
        self._reply_tasks.add(task)
        task.add_done_callback(self._reply_tasks.discard)

    async def stop(self) -> None:
        for task in list(self._reply_tasks):
            task.cancel()
        await asyncio.gather(*self._reply_tasks, return_exceptions=True)
        await super().stop()

    async def _reply(self, writer: asyncio.StreamWriter, delay: float) -> None:
        if self._next_reply >= len(self.replies):
            return
        reply = self.replies[self._next_reply]
        self._next_reply += 1
        await asyncio.sleep(delay)
        # A tuple is one reply written in pieces, as TCP may deliver it.
        chunks = reply if isinstance(reply, tuple) else (reply,)
        self.log.append(("reply", b"".join(chunks).hex()))
        for index, chunk in enumerate(chunks):
            if index:
                await asyncio.sleep(0.05)
            await self._send(writer, chunk)

    async def _connect_back(self, addr: tuple[str, int]) -> None:
        try:
            reader, writer = await asyncio.open_connection(addr[0], addr[1])
        except OSError:
            return
        self.connect_ports.append(addr[1])
        self.writers.append(writer)
        pending = b""
        while True:
            data = await reader.read(4096)
            if not data:
                self.eof_count += 1
                return
            self.received.extend(data)
            pending += data
            while pending:
                if pending.startswith(_QUERY):
                    self._take(pending[:4])
                    pending = pending[4:]
                    if self.answer_queries:
                        await self._send(writer, _report(*self.code))
                elif pending[:3] == b"\xff\xf5\xf4" and len(pending) >= 6:
                    packet, pending = pending[:6], pending[6:]
                    self._take(packet)
                    if packet[3] == 0xF1:
                        self.log.append(("start", packet.hex()))
                        self._next_reply = 0
                        await self._send(writer, b"\xcc\xdd")
                        self._schedule_reply(writer, self.press_after)
                    else:
                        self.log.append(("next", packet.hex()))
                        self._schedule_reply(writer, 0.02)
                elif pending[:2] == b"\xff\xf5" and len(pending) >= 6:
                    packet, pending = pending[:6], pending[6:]
                    self._take(packet)
                    self.codes[packet[2]] = (packet[3], packet[4])
                    await asyncio.sleep(0.05)
                    await self._send(writer, b"\xfa" + packet[1:])
                elif (
                    pending[0] == 0xFF
                    and len(pending) >= 4
                    and pending[1] != 0xF5
                    and pending[3] == 0xF0
                ):
                    self._take(pending[:4])
                    pending = pending[4:]
                elif pending[0] == 0xFF and len(pending) < 6:
                    break
                else:
                    pending = pending[1:]


class MatchClient(TrialClient):
    """Codes 115 and 25 are listed by brands b1 and b2, so their brand is not known."""

    async def async_get_sub_device_device_codes(self, sub_device_id: str) -> AifaDeviceCodeCatalog:
        catalog = await super().async_get_sub_device_device_codes(sub_device_id)
        shared = AifaDeviceCode(
            id="7",
            code="25",
            brand_id="b1",
            brand_name="Brand b1",
            brand_localized_name=None,
            country=1,
            version=5,
            subversion=210623,
            type=0,
            remote=False,
            popular=None,
        )
        shared_current = replace(shared, id="8", code="115", brand_id="b2", brand_name="Brand b2")
        return AifaDeviceCodeCatalog(
            brands={}, device_codes=[*catalog.device_codes, shared, shared_current]
        )


def _fast_match(timeout: float = 3.0) -> ExitStack:
    stack = _fast_set_code()
    stack.enter_context(patch.object(coordinator_module, "CODE_MATCH_TIMEOUT", timeout))
    return stack


@pytest.fixture
async def rig(hass: HomeAssistant):
    hub = await MatchFakeHub().start()
    built = Rig(hass, hub)
    built.client = MatchClient()
    built.coordinator.client = built.client  # type: ignore[attr-defined]
    # Same wiring as AifaSmartCoordinator.__init__.
    built.manager._on_set_code_reject = built.coordinator._apply_lan_set_code_reject  # noqa: SLF001
    built.manager._on_code_match = built.coordinator._apply_lan_code_match  # noqa: SLF001
    with _fast_match():
        await built.connect()
        yield built
    await built.close()


async def _open_match_form(rig: Rig):
    options = rig.hass.config_entries.options
    flow = await options.async_init(rig.entry.entry_id)
    await options.async_configure(flow["flow_id"], {"next_step_id": "device_code"})
    result = await options.async_configure(
        flow["flow_id"], {"sub_device": "ac-1", "method": "match"}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "match"
    assert result["errors"] == {}
    return result


async def _run_match(rig: Rig, flow_id: str):
    options = rig.hass.config_entries.options
    progress = await options.async_configure(flow_id, {})
    assert progress["type"] is FlowResultType.SHOW_PROGRESS
    assert progress["step_id"] == "match"
    assert progress["progress_action"] == "match"
    await rig.hass.async_block_till_done()
    return await options.async_configure(flow_id)


async def _submit(rig: Rig, flow_id: str, code: str, action: str):
    with patch.object(rig.hass.config_entries, "async_schedule_reload") as reload:
        result = await rig.hass.config_entries.options.async_configure(
            flow_id, {"code": code, "action": action}
        )
    rig.reload = reload
    if action == "try" and not result["errors"]:
        await _wait_until(lambda: rig.hub.packets[-1:] == [_KEY])
    return result


def _assert_idle(rig: Rig) -> None:
    assert rig.coordinator._code_matches == {}  # noqa: SLF001
    assert not rig.coordinator._ac_apply_lock("7001").locked()  # noqa: SLF001


# ----- Packets -----


def test_match_packets_match_the_app() -> None:
    assert build_code_match_start_command(0xA0) == _START
    assert build_code_match_next_command(0xA0) == _NEXT
    assert build_code_match_start_command(0xA1) == "fff5f4f1a1f0"


@pytest.mark.parametrize(
    ("packet", "expected"),
    [
        ("faf5a00033f0", (CodeMatchReply.CANDIDATE, 0xA0, 33)),
        ("faf5a00128f0", (CodeMatchReply.CANDIDATE, 0xA0, 128)),
        ("faf5a1 1234f0", (CodeMatchReply.CANDIDATE, 0xA1, 1234)),
        ("faf5a0fffff0", (CodeMatchReply.END, 0xA0, None)),
        ("faf5a000", (CodeMatchReply.END, 0xA0, None)),
        ("faf5a0f0", (CodeMatchReply.END, 0xA0, None)),
        ("fbf5a0", (CodeMatchReply.FAILED, 0xA0, None)),
        ("fbf5a00000f0", (CodeMatchReply.FAILED, 0xA0, None)),
        ("faf5a01a00f0", (CodeMatchReply.CANDIDATE, 0xA0, None)),
        ("fbf5", None),
        ("fef5a0", None),
    ],
)
def test_match_replies_are_read_like_the_app(packet: str, expected) -> None:
    assert parse_code_match_packet(bytes.fromhex(packet.replace(" ", ""))) == expected


def test_match_end_is_not_a_set_code_echo() -> None:
    assert parse_set_code_ack(_END) is None
    assert parse_set_code_ack(_candidate(33)) == (0xA0, 0x00, 0x33)


def test_extractor_drops_cc_dd_and_keeps_a_split_candidate() -> None:
    packets, rest = extract_code_match_packets(bytes.fromhex("ccdd") + _candidate(33)[:3])
    assert packets == []
    packets, rest = extract_code_match_packets(rest + _candidate(33)[3:])
    assert packets == [_candidate(33)]
    assert rest == b""


def test_extractor_waits_on_five_bytes_then_takes_a_candidate_or_the_short_end() -> None:
    packets, rest = extract_code_match_packets(_candidate(33)[:5])
    assert (packets, rest) == ([], _candidate(33)[:5])
    packets, rest = extract_code_match_packets(rest + b"\xf0")
    assert packets == [_candidate(33)]
    # A 4-byte end, then one byte of a status report in the same chunk.
    report = _report(0x01, 0x15)
    packets, rest = extract_code_match_packets(_END4 + report[:1])
    assert packets == []
    packets, rest = extract_code_match_packets(rest + report[1:])
    assert packets == [_END4]
    assert rest == b""


def test_short_end_at_the_end_of_a_chunk_or_before_other_frames() -> None:
    # 00 could open a BCD code: the bytes stay in case a candidate was cut.
    assert extract_code_match_packets(_END4) == ([_END4], _END4)
    assert extract_code_match_packets(bytes.fromhex("faf5a0ff")) == (
        [bytes.fromhex("faf5a0ff")],
        b"",
    )
    report = _report(0x01, 0x15)
    assert extract_code_match_packets(_END4 + report) == ([_END4], b"")
    assert extract_code_match_packets(_END4 + _candidate(33)) == ([_END4, _candidate(33)], b"")


def test_candidate_cut_after_four_bytes_still_comes_out_whole() -> None:
    packets, rest = extract_code_match_packets(_candidate(33)[:4])
    assert packets == [_candidate(33)[:4]]
    packets, rest = extract_code_match_packets(rest + _candidate(33)[4:])
    assert packets == [_candidate(33)]
    assert rest == b""


async def test_a_short_end_kept_for_a_cut_candidate_comes_out_once() -> None:
    taken: list[bytes] = []
    await _process(
        [_END4, b"\xcc\xdd", _candidate(33)],
        on_code_match=lambda packet: taken.append(packet) or True,
    )
    # The cc dd of the next StartMatch does not bring the old end back.
    assert taken == [_END4, _candidate(33)]


def test_extractor_steps_over_set_code_refusals_and_other_frames() -> None:
    # A 2-byte refusal of a SetCode is not a failed match.
    report = _report(0x01, 0x15)
    assert extract_code_match_packets(b"\xfb\xf5" + report) == ([], b"")
    assert extract_code_match_packets(b"\xfb\xf5") == ([], b"\xfb\xf5")
    assert extract_code_match_packets(_FAIL) == ([_FAIL[:3]], b"")
    # Status and heartbeat frames whose bytes look like a reply.
    tricky = bytes([0xFE, 0xA0, 0x01, 0x15, 0xFA, 0xF5, 0xA0, 0x00]) + bytes(5) + b"\xf0"
    assert extract_code_match_packets(tricky + bytes.fromhex("f1eea0f0")) == ([], b"")


def test_other_extractors_keep_their_frames_around_match_replies() -> None:
    report = _report(0x01, 0x15)
    stream = _END4 + report + b"\xfb\xf5" + _END
    assert extract_set_code_ack_packets(stream)[0] == [_END]
    assert parse_set_code_ack(_END) is None
    assert extract_set_code_reject_packets(stream)[0] == [b"\xfb\xf5"]
    assert extract_classic_status_packets(stream)[0] == [report]


async def _process(chunks: list[bytes], *, on_code_match) -> tuple[list, int]:
    acks: list[tuple[int, int, int]] = []
    notifies: list[None] = []
    buffers = ClassicStreamBuffers()

    async def wake() -> None:
        return None

    for chunk in chunks:
        await process_classic_stream_chunk(
            chunk,
            buffers,
            device_type=None,
            reply_wake=wake,
            on_status=lambda sub_type, observed: False,
            on_sensor=lambda sensor: False,
            on_notify=lambda: notifies.append(None),
            on_set_code_ack=lambda *ack: acks.append(ack) or False,
            on_code_match=on_code_match,
        )
    return acks, len(notifies)


async def test_claimed_candidates_are_neither_echoes_nor_notifies() -> None:
    taken: list[bytes] = []
    acks, notifies = await _process(
        [b"\xcc\xdd", _candidate(33), _END4],
        on_code_match=lambda packet: taken.append(packet) or True,
    )
    assert taken == [_candidate(33), _END4]
    assert acks == []
    assert notifies == 0


async def test_unclaimed_candidates_stay_echoes() -> None:
    acks, notifies = await _process([_candidate(33)], on_code_match=lambda packet: False)
    assert acks == [(0xA0, 0x00, 0x33)]
    assert notifies == 1


def test_candidate_rows_keep_the_hub_order_and_a_single_brand() -> None:
    def row(row_id: str, code: str, brand: str) -> AifaDeviceCode:
        return AifaDeviceCode(
            id=row_id, code=code, brand_id=brand, brand_name=brand, brand_localized_name=None,
            country=1, version=5, subversion=210623, type=0, remote=False, popular=None,
        )

    listed = [row("1", "33", "b1"), row("2", "25", "b1"), row("3", "25", "b2")]
    rows = _candidate_rows([128, 33, 25, 0], listed)
    assert [r.code for r in rows] == ["128", "33", "25"]
    assert [r.brand_id for r in rows] == [None, "b1", None]
    assert rows[0].remote is False


# ----- Options flow, end to end -----


async def test_match_lists_candidates_then_tries_and_saves_one(rig: Rig) -> None:
    rig.hub.replies = [_candidate(33), _candidate(128), _candidate(25), _candidate(34), _END]
    form = await _open_match_form(rig)
    flow_id = form["flow_id"]

    result = await _run_match(rig, flow_id)

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "match_result"
    assert result["description_placeholders"]["count"] == "4"
    assert result["description_placeholders"]["codes"] == "33, 128, 25, 34"
    # Every reply after the first waited for its NextMatch; none followed the end.
    assert [kind for kind, _ in rig.hub.log] == [
        "start", "reply", "next", "reply", "next", "reply", "next", "reply", "next", "reply",
    ]
    assert rig.hub.packets == [_START, _NEXT, _NEXT, _NEXT, _NEXT]
    assert rig.coordinator._hub_report_waiters == {}  # noqa: SLF001
    _assert_idle(rig)

    code_form = await rig.hass.config_entries.options.async_configure(flow_id, {})
    assert code_form["step_id"] == "code"
    options = code_form["data_schema"].schema["code"].config["options"]
    assert [option["value"] for option in options] == ["33", "128", "25", "34"]
    assert code_form["data_schema"]({})["code"] == "33"

    tried = await _submit(rig, flow_id, "128", "try")
    assert tried["errors"] == {}
    assert tried["description_placeholders"]["hub_code"] == "128"
    assert rig.hub.packets[5:] == ["fff5a00128f0", _KEY]
    assert rig.client.patches == []

    saved = await _submit(rig, flow_id, "128", "save")
    assert saved["type"] is FlowResultType.ABORT
    assert saved["reason"] == "device_code_changed"
    assert rig.client.patches == [("ac-1", 128)]
    assert rig.sub_device.device_code == "128"
    # 128 is not in the catalog: no brand is recorded.
    assert "ac-1" not in rig.entry.options.get(CONF_SELECTED_BRANDS, {})


async def test_saved_candidate_keeps_the_brand_only_one_brand_lists(rig: Rig) -> None:
    rig.hub.replies = [_candidate(33), _END]
    form = await _open_match_form(rig)
    await _run_match(rig, form["flow_id"])
    await rig.hass.config_entries.options.async_configure(form["flow_id"], {})

    saved = await _submit(rig, form["flow_id"], "33", "save")

    assert saved["reason"] == "device_code_changed"
    assert rig.entry.options[CONF_SELECTED_BRANDS]["ac-1"] == {"brand_id": "b1", "device_code": "33"}


async def test_candidate_cut_after_four_bytes_is_not_an_end(rig: Rig) -> None:
    cut = _candidate(128)
    rig.hub.replies = [_candidate(33), (cut[:4], cut[4:]), _END]
    form = await _open_match_form(rig)

    result = await _run_match(rig, form["flow_id"])

    assert result["step_id"] == "match_result"
    assert result["description_placeholders"]["codes"] == "33, 128"
    assert rig.hub.packets == [_START, _NEXT, _NEXT]


async def test_short_end_frame_ends_the_match(rig: Rig) -> None:
    rig.hub.replies = [_candidate(33), _candidate(128), _END4]
    form = await _open_match_form(rig)

    result = await _run_match(rig, form["flow_id"])

    assert result["step_id"] == "match_result"
    assert result["description_placeholders"]["codes"] == "33, 128"
    assert rig.hub.packets == [_START, _NEXT, _NEXT]


@pytest.mark.parametrize(
    ("replies", "error"),
    [
        ([_candidate(33), _FAIL], "code_match_failed"),
        ([_END], "code_match_none"),
        ([_candidate(33)], "code_match_timeout"),
        ([], "code_match_timeout"),
        ([_candidate(0), _END], "code_match_unsupported"),
    ],
)
async def test_failed_match_stays_on_the_match_page(rig: Rig, replies, error: str) -> None:
    rig.hub.replies = replies
    form = await _open_match_form(rig)

    with patch.object(coordinator_module, "CODE_MATCH_TIMEOUT", 0.4):
        result = await _run_match(rig, form["flow_id"])

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "match"
    assert result["errors"] == {"base": error}
    assert not any(packet.startswith("fff5a0") for packet in rig.hub.packets)
    _assert_idle(rig)

    # A retry starts a new match.
    rig.hub.replies = [_candidate(128), _END]
    retry = await _run_match(rig, form["flow_id"])
    assert retry["step_id"] == "match_result"
    assert retry["description_placeholders"]["codes"] == "128"


async def test_candidates_do_not_restart_the_timer(rig: Rig) -> None:
    # The hub keeps sending candidates and never an end frame.
    rig.hub.replies = [_candidate(code) for code in range(1, 200)]
    form = await _open_match_form(rig)

    started = time.monotonic()
    with patch.object(coordinator_module, "CODE_MATCH_TIMEOUT", 1.2):
        result = await _run_match(rig, form["flow_id"])

    assert result["errors"] == {"base": "code_match_timeout"}
    assert time.monotonic() - started < 1.7
    # The LAN queue spaces 6-byte ff f5 packets 0.3 s apart, as for SetCode.
    assert rig.hub.packets.count(_NEXT) >= 3


async def test_ac_commands_and_code_trials_are_refused_during_a_match(rig: Rig) -> None:
    rig.hub.replies = [_candidate(33), _END]
    rig.hub.press_after = 0.6
    form = await _open_match_form(rig)
    progress = await rig.hass.config_entries.options.async_configure(form["flow_id"], {})
    assert progress["type"] is FlowResultType.SHOW_PROGRESS
    await _wait_until(lambda: rig.hub.packets == [_START])

    started = time.monotonic()
    with pytest.raises(HomeAssistantError) as err:
        await rig.coordinator.async_apply_ac_state("7001", "ac-1", hvac_mode="cool")
    assert err.value.translation_key == "ac_device_code_match"
    # Refused at once, not after the match lets go of the hub lock.
    assert time.monotonic() - started < 0.3
    catalog = await rig.client.async_get_sub_device_device_codes("ac-1")
    row = next(r for r in catalog.device_codes if r.code == "33")
    for start in (
        rig.coordinator.async_try_device_code("7001", "ac-1", row, owner="other"),
        rig.coordinator.async_change_device_code("7001", "ac-1", row, owner="other"),
    ):
        with pytest.raises(DeviceCodeChangeError) as change_err:
            await start
        assert change_err.value.reason == "code_match_running"
    with pytest.raises(DeviceCodeChangeError) as match_err:
        await rig.coordinator.async_match_device_code("7001", "ac-1")
    assert match_err.value.reason == "code_match_running"

    await rig.hass.async_block_till_done()
    result = await rig.hass.config_entries.options.async_configure(form["flow_id"])
    assert result["step_id"] == "match_result"
    assert rig.hub.packets == [_START, _NEXT]
    _assert_idle(rig)


@pytest.mark.parametrize("busy", ["change", "trial", "set_code"])
async def test_match_does_not_start_while_a_code_may_change(rig: Rig, busy: str) -> None:
    coordinator = rig.coordinator
    if busy == "change":
        coordinator._code_changes_running.add("7001")  # noqa: SLF001
    elif busy == "trial":
        coordinator._code_trials["7001"] = CodeTrial(  # noqa: SLF001
            owner="other", sub_device_id="ac-1", slot=0xA0, original_code=115, hub_code=115
        )
    else:
        coordinator._hub_report_waiters["7001"] = [_HubReportWaiter(changing_code=True)]  # noqa: SLF001

    with pytest.raises(DeviceCodeChangeError) as err:
        await coordinator.async_match_device_code("7001", "ac-1")

    assert err.value.reason == "code_change_busy"
    await asyncio.sleep(0.1)
    assert rig.hub.packets == []
    assert coordinator._code_matches == {}  # noqa: SLF001


async def test_busy_match_shows_the_error_on_the_match_page(rig: Rig) -> None:
    form = await _open_match_form(rig)
    rig.coordinator._code_changes_running.add("7001")  # noqa: SLF001

    result = await rig.hass.config_entries.options.async_configure(form["flow_id"], {})

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "match"
    assert result["errors"] == {"base": "code_change_busy"}
    assert rig.hub.packets == []


async def test_closing_the_dialog_lets_the_match_reach_its_end(rig: Rig) -> None:
    rig.hub.replies = [_candidate(33), _END]
    rig.hub.press_after = 0.4
    form = await _open_match_form(rig)
    await rig.hass.config_entries.options.async_configure(form["flow_id"], {})
    await _wait_until(lambda: rig.hub.packets == [_START])

    rig.hass.config_entries.options.async_abort(form["flow_id"])
    await asyncio.sleep(0)

    # The hub has no StopMatch and would wait for NextMatch after the
    # candidate, so the match goes on and keeps refusing AC commands.
    assert rig.coordinator._code_matches  # noqa: SLF001
    with pytest.raises(HomeAssistantError):
        await rig.coordinator.async_apply_ac_state("7001", "ac-1", hvac_mode="cool")
    await _wait_until(lambda: not rig.coordinator._code_matches)  # noqa: SLF001
    assert rig.hub.packets == [_START, _NEXT]
    _assert_idle(rig)
    rig.hub.replies = [_candidate(128), _END]
    assert await rig.coordinator.async_match_device_code("7001", "ac-1") == [128]


async def test_replies_before_start_match_are_not_candidates(rig: Rig) -> None:
    rig.hub.replies = [_candidate(33), _END]
    lock = rig.coordinator._ac_apply_lock("7001")  # noqa: SLF001
    await lock.acquire()
    task = asyncio.get_running_loop().create_task(
        rig.coordinator.async_match_device_code("7001", "ac-1")
    )
    await _wait_until(lambda: "7001" in rig.coordinator._code_matches)  # noqa: SLF001
    # A late candidate of an earlier match, while this one waits for the lock.
    assert rig.coordinator._record_code_match("7001", "lan", _candidate(99))  # noqa: SLF001
    lock.release()

    assert await task == [33]
    assert rig.hub.packets == [_START, _NEXT]


async def test_other_packets_are_refused_during_a_match(rig: Rig) -> None:
    rig.hub.replies = [_candidate(33), _END]
    rig.hub.press_after = 0.4
    task = asyncio.get_running_loop().create_task(
        rig.coordinator.async_match_device_code("7001", "ac-1")
    )
    await _wait_until(lambda: rig.hub.packets == [_START])

    with pytest.raises(HomeAssistantError) as err:
        await rig.coordinator._async_send_unconfirmed(  # noqa: SLF001
            AifaFunctionCommand(id="tv", device_id="7001", sub_device_id=None, command="ff0102f0")
        )

    assert err.value.translation_key == "ac_device_code_match"
    assert await task == [33]
    assert rig.hub.packets == [_START, _NEXT]


async def test_saving_the_current_code_keeps_the_recorded_brand(rig: Rig) -> None:
    rig.hass.config_entries.async_update_entry(
        rig.entry, options={CONF_SELECTED_BRANDS: {"ac-1": {"brand_id": "b2", "device_code": "115"}}}
    )
    rig.hub.replies = [_candidate(115), _END]
    form = await _open_match_form(rig)
    await _run_match(rig, form["flow_id"])
    await rig.hass.config_entries.options.async_configure(form["flow_id"], {})

    saved = await _submit(rig, form["flow_id"], "115", "save")

    assert saved["reason"] == "device_code_unchanged"
    assert rig.entry.options[CONF_SELECTED_BRANDS]["ac-1"] == {"brand_id": "b2", "device_code": "115"}


async def test_a_short_end_does_not_end_the_next_match(rig: Rig) -> None:
    rig.hub.replies = [_END4]
    form = await _open_match_form(rig)
    first = await _run_match(rig, form["flow_id"])
    assert first["errors"] == {"base": "code_match_none"}

    rig.hub.replies = [_candidate(33), _END]
    second = await _run_match(rig, form["flow_id"])

    assert second["step_id"] == "match_result"
    assert second["description_placeholders"]["codes"] == "33"


async def test_unload_cancels_a_running_match(rig: Rig) -> None:
    rig.hub.replies = [_candidate(33), _END]
    rig.hub.press_after = 0.5
    task = asyncio.get_running_loop().create_task(
        rig.coordinator.async_match_device_code("7001", "ac-1")
    )
    await _wait_until(lambda: rig.hub.packets == [_START])

    with patch.object(
        coordinator_module.TimestampDataUpdateCoordinator, "async_shutdown", AsyncMock()
    ), patch.object(rig.coordinator._classic_listener, "shutdown", AsyncMock(), create=True):  # noqa: SLF001
        rig.coordinator._lan_manager = None  # noqa: SLF001
        await rig.coordinator.async_shutdown()

    assert rig.coordinator._code_matches == {}  # noqa: SLF001
    assert rig.coordinator._code_match_tasks == set()  # noqa: SLF001
    with pytest.raises(asyncio.CancelledError):
        await task


async def test_match_waiting_for_the_lock_does_not_start_after_unload_began(rig: Rig) -> None:
    lock = rig.coordinator._ac_apply_lock("7001")  # noqa: SLF001
    await lock.acquire()
    task = asyncio.get_running_loop().create_task(
        rig.coordinator.async_match_device_code("7001", "ac-1")
    )
    await _wait_until(lambda: "7001" in rig.coordinator._code_matches)  # noqa: SLF001
    rig.coordinator._closing = True  # noqa: SLF001
    lock.release()

    with pytest.raises(DeviceCodeChangeError) as err:
        await task
    assert err.value.reason == "code_change_busy"
    assert rig.hub.packets == []


# ----- Cloud -----


class _MatchingCloud:
    """Cloud relay that feeds a hub's match replies through ClassicSocketManager's parser."""

    def __init__(self, coordinator, replies: list[bytes]) -> None:
        self.sent: list[str] = []
        self.replies = list(replies)
        self.notifies = 0
        manager = ClassicSocketManager.__new__(ClassicSocketManager)
        # Same wiring as AifaSmartCoordinator.__init__.
        manager._on_observed_status = coordinator._apply_cloud_stream_status  # noqa: SLF001
        manager._on_sensor_state = lambda *args: False  # noqa: SLF001
        manager._on_set_code_ack = coordinator._apply_cloud_set_code_ack  # noqa: SLF001
        manager._on_set_code_reject = coordinator._apply_cloud_set_code_reject  # noqa: SLF001
        manager._on_code_match = coordinator._apply_cloud_code_match  # noqa: SLF001
        manager._hass = None  # noqa: SLF001
        manager._schedule_notify_refresh = self._notify  # type: ignore[method-assign]
        self.manager = manager
        self.buffers = ClassicStreamBuffers()
        self.spec = build_hub_specs(coordinator.data.devices, require_online=False)["7001"]

    def _notify(self, device_id: str) -> None:
        self.notifies += 1

    def has_active_listener(self, device_id: str) -> bool:
        return True

    def has_active_writer(self, device_id: str) -> bool:
        return True

    async def async_send_packet(self, device_id: str, packet_hex: str) -> bool:
        self.sent.append(packet_hex)
        if packet_hex == _START:
            self._later(b"\xcc\xdd", self.replies.pop(0))
        elif packet_hex == _NEXT and self.replies:
            self._later(self.replies.pop(0))
        return True

    def _later(self, *chunks: bytes) -> None:
        async def deliver() -> None:
            for chunk in chunks:
                await asyncio.sleep(0.02)
                await self.manager._async_process_chunk(  # noqa: SLF001
                    self.spec, chunk, None, self.buffers  # type: ignore[arg-type]
                )

        asyncio.get_running_loop().create_task(deliver())


async def test_cloud_link_carries_the_match() -> None:
    device = _device()
    device.sub_devices[0].device_code = "115"
    coordinator = _build_coordinator("cloud", device=device)
    cloud = _MatchingCloud(coordinator, [_candidate(33), _candidate(128), _END])
    coordinator._classic_listener = cloud  # noqa: SLF001

    with patch.object(coordinator_module, "CODE_MATCH_TIMEOUT", 1.5):
        codes = await coordinator.async_match_device_code("7001", "ac-1")

    assert codes == [33, 128]
    assert cloud.sent == [_START, _NEXT, _NEXT]
    # Claimed replies do not look like notifies that reload the account.
    assert cloud.notifies == 0
    assert coordinator._code_matches == {}  # noqa: SLF001


async def test_cloud_failure_drops_the_candidates() -> None:
    device = _device()
    device.sub_devices[0].device_code = "115"
    coordinator = _build_coordinator("cloud", device=device)
    cloud = _MatchingCloud(coordinator, [_candidate(33), _FAIL])
    coordinator._classic_listener = cloud  # noqa: SLF001

    with patch.object(coordinator_module, "CODE_MATCH_TIMEOUT", 1.5):
        with pytest.raises(DeviceCodeChangeError) as err:
            await coordinator.async_match_device_code("7001", "ac-1")

    assert err.value.reason == "code_match_failed"
    assert cloud.sent == [_START, _NEXT]


async def test_unsent_start_reports_not_sent() -> None:
    device = _device()
    device.sub_devices[0].device_code = "115"
    coordinator = _build_coordinator("cloud", device=device)
    coordinator._classic_listener = types.SimpleNamespace(  # noqa: SLF001
        async_send_packet=lambda *args: asyncio.sleep(0, result=False)
    )

    with pytest.raises(DeviceCodeChangeError) as err:
        await coordinator.async_match_device_code("7001", "ac-1")

    assert err.value.reason == "code_match_not_sent"
    assert coordinator._code_matches == {}  # noqa: SLF001
