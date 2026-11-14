"""Saving an air conditioner's remote code, like the AIFA Smart app's check mark after its pairing.

The end-to-end cases run the options flow against a fake hub on 127.0.0.1
that behaves like the v5/210623 hub: it echoes SetCode as
`fa f5 <slot> <hi> <lo> f0` and reports the stored code in b2-b3 of the
14-byte status packet it sends for `ff f3 f2 f0`.
"""
from __future__ import annotations

import asyncio
import types
from contextlib import ExitStack
from dataclasses import replace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import issue_registry as ir
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.aifa_smart import coordinator as coordinator_module
from custom_components.aifa_smart.ac import (
    _bcd_encode_device_code,
    build_set_code_command,
    parse_set_code_ack,
    set_code_slot,
)
from custom_components.aifa_smart.api import (
    AifaDeviceCode,
    AifaDeviceCodeCatalog,
    AifaSmartApiClient,
    AifaSmartApiError,
    AifaSmartConnectionError,
    _parse_sub_device,
    extract_set_code_ack_packets,
)
from custom_components.aifa_smart.classic_listener import (
    AifaClassicSocketTarget,
    ClassicStreamBuffers,
    process_classic_stream_chunk,
)
from custom_components.aifa_smart.const import CONF_ACCESS_TOKEN, CONF_EMAIL, DOMAIN
from custom_components.aifa_smart.coordinator import (
    ISSUE_DEVICE_CODE_MISMATCH,
    SET_CODE_SETTLE_SECONDS,
    DeviceCodeChangeError,
)
from custom_components.aifa_smart.lan import LanTransportManager
from tests.hub_reports import status
from tests.test_command_confirmation import _build_coordinator, _NoCloud
from tests.test_lan_fake_hub import _HUB_MAC, _LOCALHOST, FakeHub, _device, _fake_hass, _wait_until

pytestmark = pytest.mark.usefixtures("socket_enabled")

_QUERY = bytes.fromhex("fff3f2f0")


# ----- Encoding -----


@pytest.mark.parametrize(
    ("code", "expected"),
    [
        (115, (0x01, 0x15)),
        (999, (0x09, 0x99)),
        (1000, (0x10, 0x00)),
        (9999, (0x99, 0x99)),
        # "19866": int("19", 16) = 0x19, int("866", 16) = 0x866, low 8 bits 0x66.
        (19866, (0x19, 0x66)),
        # "10000": 0x10 and int("000", 16) = 0, the same bytes as 1000.
        (10000, (0x10, 0x00)),
    ],
)
def test_device_code_bytes_follow_the_app(code: int, expected: tuple[int, int]) -> None:
    assert _bcd_encode_device_code(code) == expected


def test_set_code_packet_matches_the_verified_capture() -> None:
    assert build_set_code_command(0xA0, 33) == "fff5a00033f0"
    assert build_set_code_command(0xA0, 115) == "fff5a00115f0"
    assert build_set_code_command(0xA1, 1234) == "fff5a11234f0"


@pytest.mark.parametrize("code", [10000, 19866, -1])
def test_set_code_refuses_codes_above_9999(code: int) -> None:
    with pytest.raises(ValueError):
        build_set_code_command(0xA0, code)


@pytest.mark.parametrize(
    ("sub_type", "slot"),
    [("0", 0xA0), ("1", 0xA1), (1, 0xA1), ("3", 0xA0), (None, 0xA0), ("x", 0xA0)],
)
def test_set_code_slot_is_a1_only_for_sub_type_1(sub_type, slot: int) -> None:
    assert set_code_slot(sub_type) == slot


def test_set_code_ack_is_parsed_across_chunks() -> None:
    packets, rest = extract_set_code_ack_packets(bytes.fromhex("00faf5a000"))
    assert packets == []
    packets, rest = extract_set_code_ack_packets(rest + bytes.fromhex("33f0fea0"))
    assert packets == [bytes.fromhex("faf5a00033f0")]
    assert parse_set_code_ack(packets[0]) == (0xA0, 0x00, 0x33)
    assert parse_set_code_ack(bytes.fromhex("faf5a00033f1")) is None


async def test_stream_parser_reports_set_code_ack_next_to_status() -> None:
    acks: list[tuple[int, int, int]] = []
    statuses: list[int] = []
    buffers = ClassicStreamBuffers()

    async def wake() -> None:
        return None

    for chunk in (bytes.fromhex("faf5a000"), bytes.fromhex("33f0") + _report(0x00, 0x33)):
        await process_classic_stream_chunk(
            chunk,
            buffers,
            device_type=None,
            reply_wake=wake,
            on_status=lambda sub_type, observed: statuses.append(sub_type) or False,
            on_sensor=lambda sensor: False,
            on_notify=lambda: None,
            on_set_code_ack=lambda *ack: acks.append(ack),
        )
    assert acks == [(0xA0, 0x00, 0x33)]
    assert statuses == [0]


# ----- Cloud data -----


def test_learned_keys_belong_to_the_sub_device() -> None:
    sub = _parse_sub_device(
        {
            "id": 77,
            "type": 0,
            "subType": 0,
            "deviceCode": 115,
            "learnedKeys": [{"subDeviceId": 77, "key": 1}, {"subDeviceId": 78, "key": 2}],
        },
        7001,
    )
    assert sub is not None and sub.learned_key_count == 1
    bare = _parse_sub_device({"id": 77, "deviceCode": 115}, 7001)
    assert bare is not None and bare.learned_key_count == 0
    no_owner = _parse_sub_device({"id": 77, "learnedKeys": [{"key": 1}]}, 7001)
    assert no_owner is not None and no_owner.learned_key_count == 1


async def test_patch_body_carries_only_the_device_code() -> None:
    client = AifaSmartApiClient(MagicMock(), email="a@example.com", password="x")
    with patch.object(client, "_authorized_request", AsyncMock(return_value={})) as request:
        await client.async_update_sub_device_code("ac-1", 33)
    request.assert_awaited_once_with("PATCH", "/sub-devices/ac-1", json={"deviceCode": 33})


# ----- Fake hub -----


def _report(high: int, low: int, slot: int = 0) -> bytes:
    return bytes([0xFE, 0xA0 | slot, high, low, 0x00, 0x00, 0x20, 0x11]) + bytes(5) + b"\xf0"


class SetCodeFakeHub(FakeHub):
    """Fake hub that stores one code per slot.

    silent_codes:   SetCodes for these codes get no echo (the hub still stores them)
    ignored_codes:  SetCodes for these codes are echoed but not stored
    answer_queries: whether `ff f3 f2 f0` draws a status report
    """

    def __init__(self, code: int = 115) -> None:
        super().__init__(_HUB_MAC)
        self.codes = {0xA0: _bcd_encode_device_code(code)}
        self.silent_codes: set[int] = set()
        self.ignored_codes: set[int] = set()
        self.answer_queries = True
        self.packets: list[str] = []

    @property
    def code(self) -> tuple[int, int]:
        return self.codes[0xA0]

    async def _send(self, writer: asyncio.StreamWriter, data: bytes) -> None:
        writer.write(data)
        await writer.drain()

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
                    pending = pending[4:]
                    self.packets.append(_QUERY.hex())
                    if self.answer_queries:
                        await self._send(writer, _report(*self.code))
                elif pending[:2] == b"\xff\xf5" and len(pending) >= 6:
                    packet, pending = pending[:6], pending[6:]
                    self.packets.append(packet.hex())
                    code = int(packet[3:5].hex())
                    if code not in self.ignored_codes:
                        self.codes[packet[2]] = (packet[3], packet[4])
                    if code not in self.silent_codes:
                        await asyncio.sleep(0.05)
                        await self._send(writer, b"\xfa" + packet[1:])
                elif pending[0] == 0xFF and len(pending) < 6:
                    break
                else:
                    pending = pending[1:]


def _rows() -> list[AifaDeviceCode]:
    def row(row_id: str, code: str, brand: str, *, remote: bool = False) -> AifaDeviceCode:
        return AifaDeviceCode(
            id=row_id,
            code=code,
            brand_id=brand,
            brand_name=f"Brand {brand}",
            brand_localized_name=None,
            country=1,
            version=5,
            subversion=210623,
            type=0,
            remote=remote,
            popular=None,
        )

    return [
        row("1", "115", "b1"),
        row("2", "33", "b1"),
        row("3", "18", "b1", remote=True),
        row("4", "12345", "b1"),
        row("5", "25", "b2"),
    ]


class FakeClient:
    def __init__(self) -> None:
        self.patches: list[tuple[str, int]] = []
        self.patch_errors: list[Exception] = []

    async def async_get_sub_device_device_codes(self, sub_device_id: str) -> AifaDeviceCodeCatalog:
        return AifaDeviceCodeCatalog(brands={}, device_codes=_rows())

    async def async_update_sub_device_code(self, sub_device_id: str, device_code: int) -> None:
        self.patches.append((sub_device_id, device_code))
        if self.patch_errors:
            raise self.patch_errors.pop(0)


def _fast_set_code() -> ExitStack:
    stack = ExitStack()
    stack.enter_context(patch.object(coordinator_module, "SET_CODE_ACK_TIMEOUT", 0.5))
    stack.enter_context(patch.object(coordinator_module, "SET_CODE_SETTLE_SECONDS", 0.05))
    stack.enter_context(patch.object(coordinator_module, "SET_CODE_QUERY_TIMEOUT", 0.5))
    return stack


def test_settle_time_matches_the_app() -> None:
    assert SET_CODE_SETTLE_SECONDS == 2.0


@pytest.fixture(autouse=True)
def bypass_setup_entry():
    with patch("custom_components.aifa_smart.async_setup_entry", return_value=True):
        yield




class Rig:
    def __init__(self, hass: HomeAssistant, hub: SetCodeFakeHub, learned: int = 0) -> None:
        self.hass = hass
        self.hub = hub
        self.client = FakeClient()
        self.manager = LanTransportManager(
            hass=_fake_hass(),  # type: ignore[arg-type]
            on_observed_status=lambda *args: False,
            on_data_changed=lambda: None,
            broadcast_provider=self._broadcast,
            target_port=hub.port,
            search_source_port=0,
            bind_host=_LOCALHOST,
            backoff_schedule=(0.05,),
            connect_attempts=2,
            connect_interval=0.3,
            search_timeout=0.2,
            connect_write_delay=0.0,
        )
        device = _device()
        device.sub_devices[0].device_code = "115"
        device.sub_devices[0].learned_key_count = learned
        self.coordinator = _build_coordinator("lan", device=device, lan=self.manager, listener=_NoCloud())
        self.coordinator.client = self.client  # type: ignore[attr-defined]
        self.coordinator._device_code_catalogs = {}  # noqa: SLF001
        self.coordinator.hass = hass  # type: ignore[assignment]
        self.coordinator._device_code_checked = set()  # noqa: SLF001
        # Same wiring as AifaSmartCoordinator.__init__.
        self.manager._on_observed_status = self.coordinator._apply_lan_observed_status  # noqa: SLF001
        self.manager._on_set_code_ack = self.coordinator._apply_lan_set_code_ack  # noqa: SLF001
        self.entry = MockConfigEntry(
            domain=DOMAIN,
            unique_id="user@example.com",
            data={CONF_EMAIL: "user@example.com", CONF_ACCESS_TOKEN: "access"},
        )
        self.entry.add_to_hass(hass)
        self.entry.runtime_data = self.coordinator
        self.coordinator.entry = self.entry  # type: ignore[attr-defined]

    async def _broadcast(self) -> list[str]:
        return [_LOCALHOST]

    async def connect(self) -> None:
        self.manager.sync_specs(self.coordinator.data.devices)
        await _wait_until(lambda: self.manager.is_connected("7001"))

    async def close(self) -> None:
        await self.manager.shutdown()
        await self.hub.stop()

    @property
    def sub_device(self):
        return self.coordinator.get_sub_device("7001", "ac-1")

    async def open_code_form(self):
        flow = await self.hass.config_entries.options.async_init(self.entry.entry_id)
        assert flow["type"] is FlowResultType.MENU
        result = await self.hass.config_entries.options.async_configure(
            flow["flow_id"], {"next_step_id": "device_code"}
        )
        assert result["step_id"] == "device_code"
        result = await self.hass.config_entries.options.async_configure(
            flow["flow_id"], {"sub_device": "ac-1"}
        )
        assert result["step_id"] == "brand"
        result = await self.hass.config_entries.options.async_configure(
            flow["flow_id"], {"brand": "b1"}
        )
        assert result["step_id"] == "code"
        return result

    async def choose(self, code: str):
        form = await self.open_code_form()
        with patch.object(self.hass.config_entries, "async_schedule_reload") as reload:
            result = await self.hass.config_entries.options.async_configure(
                form["flow_id"], {"code": code, "action": "save"}
            )
        self.reload = reload
        return result

    def set_codes_sent(self) -> list[str]:
        return [packet for packet in self.hub.packets if packet.startswith("fff5")]


@pytest.fixture
async def rig(hass: HomeAssistant):
    hub = await SetCodeFakeHub().start()
    built = Rig(hass, hub)
    with _fast_set_code():
        await built.connect()
        yield built
    await built.close()


# ----- Options flow, end to end -----


@pytest.mark.usefixtures("enable_custom_integrations")
async def test_code_form_lists_only_settable_codes_and_marks_the_current_one(rig: Rig) -> None:
    form = await rig.open_code_form()
    options = form["data_schema"].schema["code"].config["options"]
    assert [option["value"] for option in options] == ["33", "115"]
    assert form["data_schema"]({})["code"] == "115"
    assert form["description_placeholders"]["current_code"] == "115"


@pytest.mark.usefixtures("enable_custom_integrations")
async def test_change_succeeds_after_echo_and_report(rig: Rig) -> None:
    result = await rig.choose("33")

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "device_code_changed"
    assert rig.hub.packets == ["fff5a00033f0", "fff3f2f0"]
    assert rig.hub.code == (0x00, 0x33)
    assert rig.client.patches == [("ac-1", 33)]
    assert rig.sub_device.device_code == "33"
    assert rig.coordinator.get_ac_runtime_state("7001", "ac-1").device_code == "33"
    rig.reload.assert_called_once_with(rig.entry.entry_id)


@pytest.mark.usefixtures("enable_custom_integrations")
async def test_same_code_sends_nothing(rig: Rig) -> None:
    result = await rig.choose("115")

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "device_code_unchanged"
    await asyncio.sleep(0.1)
    assert rig.hub.received == bytearray()
    assert rig.client.patches == []
    rig.reload.assert_not_called()


@pytest.mark.usefixtures("enable_custom_integrations")
async def test_missing_echo_restores_without_patch(rig: Rig) -> None:
    rig.hub.silent_codes = {33}

    result = await rig.choose("33")

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "set_code_unconfirmed"}
    assert rig.set_codes_sent() == ["fff5a00033f0", "fff5a00115f0"]
    assert rig.hub.code == (0x01, 0x15)
    assert rig.client.patches == []
    assert rig.sub_device.device_code == "115"
    rig.reload.assert_not_called()


@pytest.mark.usefixtures("enable_custom_integrations")
async def test_reported_code_mismatch_restores_without_patch(rig: Rig) -> None:
    rig.hub.ignored_codes = {33}

    result = await rig.choose("33")

    assert result["errors"] == {"base": "set_code_mismatch"}
    assert rig.hub.packets == ["fff5a00033f0", "fff3f2f0", "fff5a00115f0", "fff3f2f0"]
    assert rig.hub.code == (0x01, 0x15)
    assert rig.client.patches == []
    assert rig.sub_device.device_code == "115"


@pytest.mark.usefixtures("enable_custom_integrations")
async def test_patch_rejected_restores_the_hub(rig: Rig) -> None:
    rig.client.patch_errors = [AifaSmartApiError("bad", status=400)]

    result = await rig.choose("33")

    assert result["errors"] == {"base": "cloud_update_failed"}
    assert rig.set_codes_sent() == ["fff5a00033f0", "fff5a00115f0"]
    assert rig.hub.code == (0x01, 0x15)
    # A 400 cannot have stored the code, so the cloud is left alone.
    assert rig.client.patches == [("ac-1", 33)]
    assert rig.sub_device.device_code == "115"
    rig.reload.assert_not_called()


@pytest.mark.usefixtures("enable_custom_integrations")
async def test_patch_timeout_restores_the_hub_and_the_cloud(rig: Rig) -> None:
    rig.client.patch_errors = [AifaSmartConnectionError("timeout")]

    result = await rig.choose("33")

    assert result["errors"] == {"base": "cloud_update_failed"}
    assert rig.hub.code == (0x01, 0x15)
    assert rig.client.patches == [("ac-1", 33), ("ac-1", 115)]


@pytest.mark.usefixtures("enable_custom_integrations")
async def test_patch_timeout_and_failed_cloud_restore_say_the_cloud_is_unknown(rig: Rig) -> None:
    rig.client.patch_errors = [AifaSmartConnectionError("timeout"), AifaSmartConnectionError("timeout")]

    result = await rig.choose("33")

    assert result["errors"] == {"base": "cloud_update_unknown"}
    assert rig.hub.code == (0x01, 0x15)
    assert rig.client.patches == [("ac-1", 33), ("ac-1", 115)]


@pytest.mark.usefixtures("enable_custom_integrations")
async def test_unconfirmed_restore_says_the_hub_code_is_unknown(rig: Rig) -> None:
    rig.hub.silent_codes = {115, 33}

    result = await rig.choose("33")

    assert result["errors"] == {"base": "restore_unconfirmed"}
    assert result["description_placeholders"]["current_code"] == "115"
    assert result["description_placeholders"]["new_code"] == "33"
    assert rig.set_codes_sent() == ["fff5a00033f0", "fff5a00115f0"]
    assert rig.client.patches == []


@pytest.mark.usefixtures("enable_custom_integrations")
async def test_patch_failure_with_unconfirmed_restore(rig: Rig) -> None:
    rig.client.patch_errors = [AifaSmartApiError("bad", status=400)]
    rig.hub.silent_codes = {115}

    result = await rig.choose("33")

    assert result["errors"] == {"base": "restore_unconfirmed"}
    assert rig.client.patches == [("ac-1", 33)]


@pytest.mark.usefixtures("enable_custom_integrations")
async def test_silent_hub_reports_nothing_and_nothing_is_patched(rig: Rig) -> None:
    rig.hub.silent_codes = {115, 33}
    rig.hub.answer_queries = False

    result = await rig.choose("33")

    assert result["errors"] == {"base": "restore_unconfirmed"}
    assert rig.client.patches == []


async def test_unreachable_hub_sends_nothing_and_does_not_restore(hass: HomeAssistant) -> None:
    hub = await SetCodeFakeHub().start()
    built = Rig(hass, hub)
    built.coordinator._lan_manager = None  # noqa: SLF001
    try:
        with pytest.raises(DeviceCodeChangeError) as err:
            await built.coordinator.async_change_device_code("7001", "ac-1", _rows()[1])
    finally:
        await built.close()
    assert err.value.reason == "set_code_not_sent"
    assert built.client.patches == []


# ----- Guards -----


@pytest.mark.usefixtures("enable_custom_integrations")
async def test_learned_keys_block_the_change(hass: HomeAssistant) -> None:
    hub = await SetCodeFakeHub().start()
    built = Rig(hass, hub, learned=2)
    try:
        flow = await hass.config_entries.options.async_init(built.entry.entry_id)
        await hass.config_entries.options.async_configure(
            flow["flow_id"], {"next_step_id": "device_code"}
        )
        result = await hass.config_entries.options.async_configure(
            flow["flow_id"], {"sub_device": "ac-1"}
        )
        assert result["step_id"] == "device_code"
        assert result["errors"] == {"base": "learned_keys"}
        with pytest.raises(DeviceCodeChangeError) as err:
            await built.coordinator.async_change_device_code("7001", "ac-1", _rows()[1])
        assert err.value.reason == "learned_keys"
    finally:
        await built.close()
    assert hub.received == bytearray()


@pytest.mark.parametrize("row_index", [2, 3], ids=["remote", "above_9999"])
async def test_remote_and_long_codes_are_refused(hass: HomeAssistant, row_index: int) -> None:
    hub = await SetCodeFakeHub().start()
    built = Rig(hass, hub)
    try:
        with pytest.raises(DeviceCodeChangeError) as err:
            await built.coordinator.async_change_device_code("7001", "ac-1", _rows()[row_index])
    finally:
        await built.close()
    assert err.value.reason == "code_unsupported"
    assert hub.received == bytearray()
    assert built.client.patches == []


# ----- Startup consistency check -----


def _checking_coordinator(hass: HomeAssistant, device_code: str = "115"):
    device = _device()
    device.sub_devices[0].device_code = device_code
    coordinator = _build_coordinator("lan", device=device)
    coordinator.hass = hass  # type: ignore[assignment]
    coordinator._device_code_checked = set()  # noqa: SLF001
    return coordinator


def _deliver(coordinator, code: str, device_code: str = "115") -> None:
    spec = types.SimpleNamespace(
        device_id="7001",
        targets=(AifaClassicSocketTarget("ac-1", 0, device_code),),
    )
    observed = replace(status("cool"), device_code=code)
    coordinator._apply_classic_observed_status(spec, 0, observed, source="lan")  # noqa: SLF001


async def test_first_report_with_another_code_raises_a_repair_issue(hass: HomeAssistant) -> None:
    coordinator = _checking_coordinator(hass)

    _deliver(coordinator, "33")

    issue = ir.async_get(hass).async_get_issue(DOMAIN, f"{ISSUE_DEVICE_CODE_MISMATCH}_ac-1")
    assert issue is not None
    assert issue.translation_key == ISSUE_DEVICE_CODE_MISMATCH
    assert issue.translation_placeholders == {"name": "AC", "hub_code": "33", "cloud_code": "115"}
    # Only the first report is compared, and nothing is corrected.
    ir.async_delete_issue(hass, DOMAIN, f"{ISSUE_DEVICE_CODE_MISMATCH}_ac-1")
    _deliver(coordinator, "33")
    assert ir.async_get(hass).async_get_issue(DOMAIN, f"{ISSUE_DEVICE_CODE_MISMATCH}_ac-1") is None
    assert coordinator.get_sub_device("7001", "ac-1").device_code == "115"


async def test_matching_first_report_raises_nothing(hass: HomeAssistant) -> None:
    coordinator = _checking_coordinator(hass)

    _deliver(coordinator, "115")

    assert ir.async_get(hass).async_get_issue(DOMAIN, f"{ISSUE_DEVICE_CODE_MISMATCH}_ac-1") is None


async def test_five_digit_cloud_codes_are_not_compared(hass: HomeAssistant) -> None:
    coordinator = _checking_coordinator(hass, device_code="19866")

    _deliver(coordinator, "1966", device_code="19866")

    assert ir.async_get(hass).async_get_issue(DOMAIN, f"{ISSUE_DEVICE_CODE_MISMATCH}_ac-1") is None


# ----- Review fixes -----


@pytest.mark.usefixtures("enable_custom_integrations")
async def test_closing_the_dialog_mid_change_still_finishes_it(rig: Rig) -> None:
    """The flow step is cancelled when the browser goes away; the change is not."""
    form = await rig.open_code_form()
    with patch.object(rig.hass.config_entries, "async_schedule_reload") as reload:
        step = asyncio.ensure_future(
            rig.hass.config_entries.options.async_configure(
                form["flow_id"], {"code": "33", "action": "save"}
            )
        )
        await _wait_until(lambda: rig.hub.code == (0x00, 0x33))
        step.cancel()
        with pytest.raises(asyncio.CancelledError):
            await step
        await _wait_until(lambda: rig.client.patches == [("ac-1", 33)])
        await _wait_until(lambda: reload.called)
    assert rig.sub_device.device_code == "33"
    assert rig.hub.packets == ["fff5a00033f0", "fff3f2f0"]


@pytest.mark.usefixtures("enable_custom_integrations")
async def test_reports_during_a_failed_change_raise_no_issue(rig: Rig) -> None:
    rig.client.patch_errors = [AifaSmartApiError("bad", status=400)]

    result = await rig.choose("33")

    assert result["errors"] == {"base": "cloud_update_failed"}
    issue_id = f"{ISSUE_DEVICE_CODE_MISMATCH}_ac-1"
    assert ir.async_get(rig.hass).async_get_issue(DOMAIN, issue_id) is None
    # The check still runs on the next report outside the change.
    await FakeHub.push(rig.hub, _report(0x00, 0x18))
    await _wait_until(lambda: ir.async_get(rig.hass).async_get_issue(DOMAIN, issue_id) is not None)


@pytest.mark.usefixtures("enable_custom_integrations")
async def test_sub_type_1_air_conditioners_are_refused(hass: HomeAssistant) -> None:
    hub = await SetCodeFakeHub().start()
    built = Rig(hass, hub)
    built.sub_device.sub_type = "1"
    try:
        flow = await hass.config_entries.options.async_init(built.entry.entry_id)
        await hass.config_entries.options.async_configure(
            flow["flow_id"], {"next_step_id": "device_code"}
        )
        result = await hass.config_entries.options.async_configure(
            flow["flow_id"], {"sub_device": "ac-1"}
        )
        assert result["errors"] == {"base": "sub_device_unsupported"}
        with pytest.raises(DeviceCodeChangeError) as err:
            await built.coordinator.async_change_device_code("7001", "ac-1", _rows()[1])
        assert err.value.reason == "sub_device_unsupported"
    finally:
        await built.close()
    assert hub.received == bytearray()


async def test_command_queued_behind_a_change_is_not_sent_with_the_old_code(
    hass: HomeAssistant,
) -> None:
    from homeassistant.exceptions import HomeAssistantError

    hub = await SetCodeFakeHub().start()
    built = Rig(hass, hub)
    built.sub_device.device_code = "33"
    try:
        with pytest.raises(HomeAssistantError) as err:
            await built.coordinator.async_apply_ac_state(
                "7001", "ac-1", hvac_mode="cool", device_code="115"
            )
    finally:
        await built.close()
    assert err.value.translation_key == "ac_device_code_changed"
    assert hub.received == bytearray()


@pytest.mark.usefixtures("enable_custom_integrations")
async def test_unloaded_entry_mid_flow_aborts(hass: HomeAssistant) -> None:
    hub = await SetCodeFakeHub().start()
    built = Rig(hass, hub)
    try:
        flow = await hass.config_entries.options.async_init(built.entry.entry_id)
        await hass.config_entries.options.async_configure(
            flow["flow_id"], {"next_step_id": "device_code"}
        )
        built.entry.runtime_data = None
        result = await hass.config_entries.options.async_configure(
            flow["flow_id"], {"sub_device": "ac-1"}
        )
    finally:
        await built.close()
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "not_loaded"


@pytest.mark.usefixtures("enable_custom_integrations")
async def test_command_sent_during_a_change_waits_for_it(rig: Rig) -> None:
    """A command must not reach the hub between SetCode and its check."""
    from homeassistant.exceptions import HomeAssistantError

    change = asyncio.ensure_future(
        rig.coordinator.async_change_device_code("7001", "ac-1", _rows()[1])
    )
    await _wait_until(lambda: rig.set_codes_sent() != [])
    with patch.object(rig.hass.config_entries, "async_schedule_reload"):
        with pytest.raises(HomeAssistantError) as err:
            await rig.coordinator.async_apply_ac_state(
                "7001", "ac-1", hvac_mode="on", device_code="115"
            )
        assert await change is True
    assert err.value.translation_key == "ac_device_code_changed"
    assert rig.hub.packets == ["fff5a00033f0", "fff3f2f0"]
