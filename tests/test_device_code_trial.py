"""Trying a remote code before saving it, like the AIFA Smart app's pairing button.

The AIFA Smart app sends SetCode, waits 2 s, presses `ff a0 a0 f0` and lets the user
watch the air conditioner. Only the check mark stores the code in the cloud;
going back puts the old code on the hub. The cases run the options flow
against a fake hub on 127.0.0.1.
"""
from __future__ import annotations

import asyncio
import time
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.core import HassJob, HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.update_coordinator import TimestampDataUpdateCoordinator

from custom_components.aifa_smart.ac import build_pairing_key_command
from custom_components.aifa_smart.api import AifaDeviceCode, AifaDeviceCodeCatalog, AifaSmartApiError
from custom_components.aifa_smart.const import DOMAIN
from custom_components.aifa_smart.coordinator import ISSUE_DEVICE_CODE_MISMATCH
from tests.test_device_code_change import (
    FakeClient,
    Rig,
    SetCodeFakeHub,
    _fast_set_code,
    _report,
    _rows,
)
from tests.test_lan_fake_hub import FakeHub, _wait_until

pytestmark = pytest.mark.usefixtures("socket_enabled", "enable_custom_integrations")

_QUERY = bytes.fromhex("fff3f2f0")
_KEY = "ffa0a0f0"


@pytest.fixture(autouse=True)
def bypass_setup_entry():
    with patch("custom_components.aifa_smart.async_setup_entry", return_value=True):
        yield


class TrialFakeHub(SetCodeFakeHub):
    """SetCodeFakeHub that also takes 4-byte key presses and refuses codes.

    rejected_codes: SetCodes answered with `fb f5`, code kept
    sent_at:        (packet, monotonic time) for every packet taken
    """

    def __init__(self) -> None:
        super().__init__()
        self.rejected_codes: set[int] = set()
        self.sent_at: list[tuple[str, float]] = []

    def _take(self, packet: bytes) -> None:
        self.packets.append(packet.hex())
        self.sent_at.append((packet.hex(), time.monotonic()))

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
                elif pending[:2] == b"\xff\xf5" and len(pending) >= 6:
                    packet, pending = pending[:6], pending[6:]
                    self._take(packet)
                    code = int(packet[3:5].hex())
                    if code in self.rejected_codes:
                        await asyncio.sleep(0.05)
                        await self._send(writer, b"\xfb\xf5")
                        continue
                    if code not in self.ignored_codes:
                        self.codes[packet[2]] = (packet[3], packet[4])
                    if code not in self.silent_codes:
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


class TrialClient(FakeClient):
    """Brand b1 also lists code 116, so a second code can be tried."""

    async def async_get_sub_device_device_codes(self, sub_device_id: str) -> AifaDeviceCodeCatalog:
        rows = _rows()
        extra = AifaDeviceCode(
            id="6",
            code="116",
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
        return AifaDeviceCodeCatalog(brands={}, device_codes=[*rows, extra])


@pytest.fixture
async def rig(hass: HomeAssistant):
    hub = await TrialFakeHub().start()
    built = Rig(hass, hub)
    built.client = TrialClient()
    built.coordinator.client = built.client  # type: ignore[attr-defined]
    built.coordinator._code_trials = {}  # noqa: SLF001
    # Same wiring as AifaSmartCoordinator.__init__.
    built.manager._on_set_code_reject = built.coordinator._apply_lan_set_code_reject  # noqa: SLF001
    with _fast_set_code():
        await built.connect()
        yield built
    await built.close()


async def _submit(rig: Rig, flow_id: str, code: str, action: str):
    with patch.object(rig.hass.config_entries, "async_schedule_reload") as reload:
        result = await rig.hass.config_entries.options.async_configure(
            flow_id, {"code": code, "action": action}
        )
    rig.reload = reload
    if action == "try" and not result["errors"]:
        # The key is written before the step returns but read by the hub later.
        await _wait_until(lambda: rig.hub.packets[-1:] == [_KEY])
    return result


def test_pairing_key_is_the_apps_generic_power_key() -> None:
    assert build_pairing_key_command(0xA0) == _KEY
    assert build_pairing_key_command(0xA1) == "ffa1a0f0"


async def test_code_form_defaults_to_try(rig: Rig) -> None:
    form = await rig.open_code_form()
    defaults = form["data_schema"]({})
    assert defaults == {"code": "115", "action": "try"}
    assert form["description_placeholders"]["hub_code"] == "115"


async def test_try_sends_set_code_then_the_power_key_and_stores_nothing(rig: Rig) -> None:
    form = await rig.open_code_form()

    result = await _submit(rig, form["flow_id"], "33", "try")

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "code"
    assert result["errors"] == {}
    assert result["description_placeholders"]["hub_code"] == "33"
    assert result["description_placeholders"]["current_code"] == "115"
    # No query between: the hub drops a key while it answers one.
    assert rig.hub.packets == ["fff5a00033f0", _KEY]
    (_, set_code_at), (_, key_at) = rig.hub.sent_at
    assert key_at - set_code_at >= 0.05
    assert rig.hub.code == (0x00, 0x33)
    assert rig.client.patches == []
    assert rig.sub_device.device_code == "115"
    rig.reload.assert_not_called()
    trial = rig.coordinator.code_trial("7001")
    assert trial is not None and trial.hub_code == 33 and trial.original_code == 115


async def test_refused_try_shows_the_error_and_keeps_the_code(rig: Rig) -> None:
    rig.hub.rejected_codes = {33}
    form = await rig.open_code_form()

    result = await _submit(rig, form["flow_id"], "33", "try")

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "code"
    assert result["errors"] == {"base": "set_code_rejected"}
    assert result["description_placeholders"]["hub_code"] == "115"
    assert result["description_placeholders"]["new_code"] == "33"
    # The query only checks the kept code; no key and no restore follow.
    assert rig.hub.packets == ["fff5a00033f0", "fff3f2f0"]
    assert rig.hub.code == (0x01, 0x15)
    assert rig.client.patches == []
    assert rig.coordinator.code_trial("7001") is None


async def test_refused_try_after_a_try_keeps_the_tried_code(rig: Rig) -> None:
    rig.hub.rejected_codes = {116}
    form = await rig.open_code_form()
    await _submit(rig, form["flow_id"], "33", "try")

    result = await _submit(rig, form["flow_id"], "116", "try")

    assert result["errors"] == {"base": "set_code_rejected"}
    assert result["description_placeholders"]["hub_code"] == "33"
    assert rig.hub.packets == ["fff5a00033f0", _KEY, "fff5a00116f0", "fff3f2f0"]
    assert rig.hub.code == (0x00, 0x33)
    assert rig.coordinator.code_trial("7001").hub_code == 33


async def test_save_after_a_try_only_checks_the_code_then_patches(rig: Rig) -> None:
    form = await rig.open_code_form()
    await _submit(rig, form["flow_id"], "33", "try")

    result = await _submit(rig, form["flow_id"], "33", "save")

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "device_code_changed"
    assert rig.hub.packets == ["fff5a00033f0", _KEY, "fff3f2f0"]
    assert rig.client.patches == [("ac-1", 33)]
    assert rig.sub_device.device_code == "33"
    rig.reload.assert_called_once_with(rig.entry.entry_id)
    assert rig.coordinator.code_trial("7001") is None
    # The finished flow is removed; nothing is put back.
    await asyncio.sleep(0.2)
    assert rig.hub.code == (0x00, 0x33)
    assert rig.hub.packets == ["fff5a00033f0", _KEY, "fff3f2f0"]


async def test_save_after_a_try_sends_set_code_again_when_the_check_fails(rig: Rig) -> None:
    form = await rig.open_code_form()
    await _submit(rig, form["flow_id"], "33", "try")
    # Someone moved the hub off the tried code, e.g. the AIFA Smart app.
    rig.hub.codes[0xA0] = (0x01, 0x15)

    result = await _submit(rig, form["flow_id"], "33", "save")

    assert result["reason"] == "device_code_changed"
    assert rig.hub.packets == ["fff5a00033f0", _KEY, "fff3f2f0", "fff5a00033f0", "fff3f2f0"]
    assert rig.client.patches == [("ac-1", 33)]


async def test_try_another_code_switches_directly(rig: Rig) -> None:
    form = await rig.open_code_form()
    await _submit(rig, form["flow_id"], "33", "try")

    result = await _submit(rig, form["flow_id"], "116", "try")

    assert result["errors"] == {}
    assert result["description_placeholders"]["hub_code"] == "116"
    assert rig.hub.packets == ["fff5a00033f0", _KEY, "fff5a00116f0", _KEY]
    assert rig.hub.code == (0x01, 0x16)
    assert rig.client.patches == []


async def test_save_another_code_after_a_try_runs_the_full_change(rig: Rig) -> None:
    form = await rig.open_code_form()
    await _submit(rig, form["flow_id"], "33", "try")

    result = await _submit(rig, form["flow_id"], "116", "save")

    assert result["reason"] == "device_code_changed"
    assert rig.hub.packets == ["fff5a00033f0", _KEY, "fff5a00116f0", "fff3f2f0"]
    assert rig.hub.code == (0x01, 0x16)
    assert rig.client.patches == [("ac-1", 116)]
    assert rig.coordinator.code_trial("7001") is None
    await asyncio.sleep(0.2)
    assert rig.hub.code == (0x01, 0x16)


async def test_saving_the_current_code_after_a_try_puts_it_back(rig: Rig) -> None:
    form = await rig.open_code_form()
    await _submit(rig, form["flow_id"], "33", "try")

    result = await _submit(rig, form["flow_id"], "115", "save")

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "device_code_unchanged"
    assert rig.hub.packets == ["fff5a00033f0", _KEY, "fff5a00115f0", "fff3f2f0"]
    assert rig.hub.code == (0x01, 0x15)
    assert rig.client.patches == []
    assert rig.coordinator.code_trial("7001") is None


async def test_failed_patch_after_a_try_restores_the_cloud_code(rig: Rig) -> None:
    form = await rig.open_code_form()
    await _submit(rig, form["flow_id"], "33", "try")
    rig.client.patch_errors = [AifaSmartApiError("bad", status=400)]

    result = await _submit(rig, form["flow_id"], "33", "save")

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "cloud_update_failed"}
    assert result["description_placeholders"]["hub_code"] == "115"
    assert rig.hub.packets == ["fff5a00033f0", _KEY, "fff3f2f0", "fff5a00115f0", "fff3f2f0"]
    assert rig.hub.code == (0x01, 0x15)
    assert rig.coordinator.code_trial("7001") is None
    rig.reload.assert_not_called()


async def test_closing_the_dialog_after_a_try_restores_the_cloud_code(rig: Rig) -> None:
    form = await rig.open_code_form()
    await _submit(rig, form["flow_id"], "33", "try")

    rig.hass.config_entries.options.async_abort(form["flow_id"])

    await _wait_until(lambda: rig.coordinator.code_trial("7001") is None)
    assert rig.hub.packets == ["fff5a00033f0", _KEY, "fff5a00115f0", "fff3f2f0"]
    assert rig.hub.code == (0x01, 0x15)
    assert rig.client.patches == []


async def test_closing_the_dialog_mid_try_still_restores(rig: Rig) -> None:
    form = await rig.open_code_form()
    step = asyncio.ensure_future(
        rig.hass.config_entries.options.async_configure(
            form["flow_id"], {"code": "33", "action": "try"}
        )
    )
    await _wait_until(lambda: rig.hub.packets == ["fff5a00033f0"])
    step.cancel()
    with pytest.raises(asyncio.CancelledError):
        await step
    rig.hass.config_entries.options.async_abort(form["flow_id"])

    await _wait_until(lambda: rig.coordinator.code_trial("7001") is None)
    # The press finishes first, then the hub goes back.
    assert rig.hub.packets == ["fff5a00033f0", _KEY, "fff5a00115f0", "fff3f2f0"]
    assert rig.hub.code == (0x01, 0x15)


async def test_unconfirmed_restore_still_ends_the_trial(rig: Rig) -> None:
    form = await rig.open_code_form()
    await _submit(rig, form["flow_id"], "33", "try")
    rig.hub.silent_codes = {115}

    rig.hass.config_entries.options.async_abort(form["flow_id"])

    await _wait_until(lambda: rig.coordinator.code_trial("7001") is None)
    assert rig.hub.packets[-1] == "fff5a00115f0"
    # Commands work again, and the next report is compared with the cloud.
    assert ("7001", "ac-1") not in rig.coordinator._device_code_checked  # noqa: SLF001


async def test_home_assistant_stop_restores_the_cloud_code(rig: Rig) -> None:
    form = await rig.open_code_form()
    await _submit(rig, form["flow_id"], "33", "try")
    jobs = list(rig.hass._shutdown_jobs)  # noqa: SLF001
    assert len(jobs) == 1

    # What HA's stage 1 runs before it cancels background tasks.
    await rig.hass.async_run_hass_job(jobs[0].job, *jobs[0].args)

    assert rig.hub.packets == ["fff5a00033f0", _KEY, "fff5a00115f0", "fff3f2f0"]
    assert rig.hub.code == (0x01, 0x15)
    assert rig.coordinator.code_trial("7001") is None
    rig.hass.config_entries.options.async_abort(form["flow_id"])
    await asyncio.sleep(0.2)
    assert rig.hub.packets.count("fff5a00115f0") == 1


async def test_no_shutdown_job_is_left_after_a_save(rig: Rig) -> None:
    form = await rig.open_code_form()
    await _submit(rig, form["flow_id"], "33", "try")
    await _submit(rig, form["flow_id"], "33", "save")
    assert rig.hass._shutdown_jobs == []  # noqa: SLF001


async def test_unload_restores_the_cloud_code_before_the_links_close(rig: Rig) -> None:
    form = await rig.open_code_form()
    await _submit(rig, form["flow_id"], "33", "try")
    closed_at: list[list[str]] = []
    rig.coordinator._classic_listener.shutdown = AsyncMock()  # noqa: SLF001

    async def lan_shutdown() -> None:
        closed_at.append(list(rig.hub.packets))

    with (
        patch.object(TimestampDataUpdateCoordinator, "async_shutdown", AsyncMock()),
        patch.object(rig.manager, "shutdown", lan_shutdown),
    ):
        await rig.coordinator.async_shutdown()

    assert closed_at == [["fff5a00033f0", _KEY, "fff5a00115f0", "fff3f2f0"]]
    assert rig.hub.code == (0x01, 0x15)
    assert rig.coordinator.code_trial("7001") is None


async def test_ac_commands_are_refused_while_the_hub_is_on_a_tried_code(rig: Rig) -> None:
    form = await rig.open_code_form()
    await _submit(rig, form["flow_id"], "33", "try")
    packets = list(rig.hub.packets)

    with pytest.raises(HomeAssistantError) as raised:
        await rig.coordinator.async_apply_ac_state("7001", "ac-1", hvac_mode="cool")
    assert raised.value.translation_key == "ac_device_code_trial"
    await asyncio.sleep(0.1)
    assert rig.hub.packets == packets

    rig.hass.config_entries.options.async_abort(form["flow_id"])
    await _wait_until(lambda: rig.coordinator.code_trial("7001") is None)
    rig.coordinator._raise_if_code_trial("7001", "ac-1")  # noqa: SLF001


async def test_a_command_queued_behind_a_try_is_refused(rig: Rig) -> None:
    form = await rig.open_code_form()
    step = asyncio.ensure_future(_submit(rig, form["flow_id"], "33", "try"))
    await _wait_until(lambda: rig.hub.packets == ["fff5a00033f0"])

    with pytest.raises(HomeAssistantError) as raised:
        await rig.coordinator.async_apply_ac_state("7001", "ac-1", hvac_mode="cool")

    assert raised.value.translation_key == "ac_device_code_trial"
    result = await step
    assert result["errors"] == {}
    # It waited for the press instead of going out in between.
    await asyncio.sleep(0.1)
    assert rig.hub.packets == ["fff5a00033f0", _KEY]


async def test_a_second_flow_cannot_change_the_code_during_a_try(rig: Rig) -> None:
    form = await rig.open_code_form()
    await _submit(rig, form["flow_id"], "33", "try")

    other = await rig.hass.config_entries.options.async_init(rig.entry.entry_id)
    await rig.hass.config_entries.options.async_configure(
        other["flow_id"], {"next_step_id": "device_code"}
    )
    result = await rig.hass.config_entries.options.async_configure(
        other["flow_id"], {"sub_device": "ac-1"}
    )

    assert result["step_id"] == "device_code"
    assert result["errors"] == {"base": "code_change_busy"}
    # Closing the other dialog does not undo this flow's trial.
    rig.hass.config_entries.options.async_abort(other["flow_id"])
    await asyncio.sleep(0.2)
    assert rig.coordinator.code_trial("7001") is not None
    assert rig.hub.code == (0x00, 0x33)


async def test_reports_with_the_tried_code_raise_no_issue(rig: Rig) -> None:
    form = await rig.open_code_form()
    await _submit(rig, form["flow_id"], "33", "try")
    issue_id = f"{ISSUE_DEVICE_CODE_MISMATCH}_ac-1"

    await FakeHub.push(rig.hub, _report(0x00, 0x33))
    await asyncio.sleep(0.2)

    assert ir.async_get(rig.hass).async_get_issue(DOMAIN, issue_id) is None


async def test_a_restore_failing_at_once_on_stop_does_not_skip_the_next_shutdown_job(
    rig: Rig,
) -> None:
    form = await rig.open_code_form()
    await _submit(rig, form["flow_id"], "33", "try")
    ran: list[bool] = []

    async def other_integration() -> None:
        ran.append(True)

    rig.hass.async_add_shutdown_job(HassJob(other_integration))
    rig.coordinator._async_send_raw_command = AsyncMock(  # noqa: SLF001
        side_effect=HomeAssistantError("not connected")
    )
    # HA's stage 1 loop: eager tasks, run while iterating the list.
    tasks = [
        rig.hass.async_run_hass_job(job.job, *job.args)
        for job in rig.hass._shutdown_jobs  # noqa: SLF001
    ]
    await asyncio.gather(*[task for task in tasks if task is not None])

    assert ran == [True]
    assert rig.coordinator.code_trial("7001") is None
    rig.hass.config_entries.options.async_abort(form["flow_id"])


async def test_saving_the_current_code_retries_an_unconfirmed_restore(rig: Rig) -> None:
    form = await rig.open_code_form()
    await _submit(rig, form["flow_id"], "33", "try")
    rig.hub.ignored_codes = {115}

    result = await _submit(rig, form["flow_id"], "115", "save")

    assert result["errors"] == {"base": "trial_restore_unconfirmed"}
    assert result["description_placeholders"]["hub_code"] == "?"
    assert rig.coordinator.code_trial("7001").hub_code is None
    rig.hub.ignored_codes = set()

    result = await _submit(rig, form["flow_id"], "115", "save")

    assert result["reason"] == "device_code_unchanged"
    assert rig.set_codes_sent() == ["fff5a00033f0", "fff5a00115f0", "fff5a00115f0"]
    assert rig.hub.code == (0x01, 0x15)
    assert rig.coordinator.code_trial("7001") is None


async def _second_code_form(rig: Rig):
    other = await rig.hass.config_entries.options.async_init(rig.entry.entry_id)
    await rig.hass.config_entries.options.async_configure(
        other["flow_id"], {"next_step_id": "device_code"}
    )
    await rig.hass.config_entries.options.async_configure(
        other["flow_id"], {"sub_device": "ac-1"}
    )
    result = await rig.hass.config_entries.options.async_configure(
        other["flow_id"], {"brand": "b1"}
    )
    assert result["step_id"] == "code"
    return result


async def test_saving_the_current_code_during_another_dialogs_try_is_refused(rig: Rig) -> None:
    form = await rig.open_code_form()
    other = await _second_code_form(rig)
    await _submit(rig, other["flow_id"], "33", "try")

    result = await _submit(rig, form["flow_id"], "115", "save")

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "code_change_busy"}
    assert result["description_placeholders"]["hub_code"] == "?"
    assert rig.hub.code == (0x00, 0x33)
    assert rig.coordinator.code_trial("7001").owner == other["flow_id"]
    rig.hass.config_entries.options.async_abort(other["flow_id"])
    await _wait_until(lambda: rig.coordinator.code_trial("7001") is None)


async def test_a_try_during_another_dialogs_save_is_refused(rig: Rig) -> None:
    form = await rig.open_code_form()
    other = await _second_code_form(rig)
    save = asyncio.ensure_future(_submit(rig, form["flow_id"], "33", "save"))
    await _wait_until(lambda: rig.hub.packets == ["fff5a00033f0"])

    result = await _submit(rig, other["flow_id"], "116", "try")

    assert result["errors"] == {"base": "code_change_busy"}
    assert (await save)["reason"] == "device_code_changed"
    assert rig.coordinator.code_trial("7001") is None
    assert rig.hub.packets == ["fff5a00033f0", "fff3f2f0"]


async def test_no_try_starts_once_unload_began(rig: Rig) -> None:
    form = await rig.open_code_form()
    rig.coordinator._closing = True  # noqa: SLF001

    result = await _submit(rig, form["flow_id"], "33", "try")

    assert result["errors"] == {"base": "code_change_busy"}
    assert rig.hub.packets == []
