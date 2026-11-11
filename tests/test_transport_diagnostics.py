"""Transport diagnostics and the options-flow hub search warning."""
from __future__ import annotations

import json
import types
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.aifa_smart import config_flow
from custom_components.aifa_smart.const import (
    CONF_ACCESS_TOKEN,
    CONF_EMAIL,
    CONF_TRANSPORT,
    DOMAIN,
    TRANSPORT_AUTO,
    TRANSPORT_CLOUD,
    TRANSPORT_LAN,
)
from custom_components.aifa_smart.coordinator import (
    AifaAccountData,
    AifaSmartCoordinator,
)
from custom_components.aifa_smart.diagnostics import async_get_config_entry_diagnostics
from custom_components.aifa_smart.lan import LanHubLink, LanTransportManager, build_hub_specs
from tests.test_lan_fake_hub import _HUB_MAC, _device, _fake_hass

_T0 = datetime(2000, 1, 2, 6, 0, tzinfo=timezone.utc)


class _OpenWriter:
    def is_closing(self) -> bool:
        return False


def _diagnostics_coordinator() -> AifaSmartCoordinator:
    manager = LanTransportManager(
        hass=_fake_hass(),  # type: ignore[arg-type]
        on_observed_status=lambda *args: False,
        on_data_changed=lambda: None,
    )
    spec = build_hub_specs({"7001": _device()}, require_online=False)["7001"]
    link = LanHubLink(
        spec=spec,
        state="connected",
        hub_ip="192.168.30.136",
        local_port=40001,
        connected_at=_T0,
        last_rx_at=_T0,
        last_status_at=_T0,
        last_tx_at=_T0,
        last_connect_ms=420,
        reconnect_count=2,
    )
    link.writer = _OpenWriter()  # type: ignore[assignment]
    manager.links["7001"] = link
    manager.broadcast_addresses = ["192.168.30.255"]

    coordinator = AifaSmartCoordinator.__new__(AifaSmartCoordinator)
    coordinator._code_trials = {}  # type: ignore[attr-defined]
    coordinator._code_matches = {}  # type: ignore[attr-defined]
    coordinator._code_match_tasks = set()  # type: ignore[attr-defined]
    coordinator.data = AifaAccountData(  # type: ignore[attr-defined]
        devices={"7001": _device()}, macros=[], fetched_at=_T0,
    )
    coordinator.last_update_success = False
    coordinator.update_interval = None
    coordinator._transport = TRANSPORT_LAN  # type: ignore[attr-defined]
    coordinator._lan_manager = manager  # type: ignore[attr-defined]
    coordinator.running_on_cache = True
    coordinator._classic_listener = types.SimpleNamespace(  # type: ignore[attr-defined]
        specs={"7001": object()},
        has_active_listener=lambda device_id: True,
        has_active_writer=lambda device_id: False,
    )
    coordinator._transport_counters = {  # type: ignore[attr-defined]
        "sent_lan": 3,
        "sent_cloud": 0,
        "auto_fallbacks": 0,
        "lan_write_failures": 1,
        "last_transport": "lan",
    }
    return coordinator



async def _open_transport(hass: HomeAssistant, entry):
    """The options flow opens on a menu; transport is one of its two pages."""
    menu = await hass.config_entries.options.async_init(entry.entry_id)
    assert menu["type"] is FlowResultType.MENU
    assert menu["menu_options"] == ["transport", "device_code"]
    return await hass.config_entries.options.async_configure(
        menu["flow_id"], {"next_step_id": "transport"}
    )

@pytest.mark.asyncio
async def test_transport_block_snapshot() -> None:
    coordinator = _diagnostics_coordinator()
    entry = types.SimpleNamespace(
        title="user@example.com",
        version=1,
        data={CONF_EMAIL: "user@example.com", CONF_ACCESS_TOKEN: "SECRET"},
        options={CONF_TRANSPORT: TRANSPORT_LAN},
        runtime_data=coordinator,
    )

    result = await async_get_config_entry_diagnostics(None, entry)  # type: ignore[arg-type]

    assert result["transport"] == {
        "mode": "lan",
        "running_on_cache": True,
        "broadcast_addresses": ["192.168.30.255"],
        "lan": {
            "7001": {
                "state": "connected",
                "connected": True,
                "hub_ip": "192.168.30.136",
                "local_port": 40001,
                "connected_at": "2000-01-02T06:00:00+00:00",
                "last_rx_at": "2000-01-02T06:00:00+00:00",
                "last_status_at": "2000-01-02T06:00:00+00:00",
                "last_tx_at": "2000-01-02T06:00:00+00:00",
                "reconnect_count": 2,
                "consecutive_failures": 0,
                "last_error": None,
                "last_connect_ms": 420,
            }
        },
        "cloud_listener": {"7001": {"listening": True, "writer": False}},
        "counters": {
            "sent_lan": 3,
            "sent_cloud": 0,
            "auto_fallbacks": 0,
            "lan_write_failures": 1,
            "last_transport": "lan",
        },
    }
    text = json.dumps(result, default=str)
    assert _HUB_MAC not in text
    assert "SECRET" not in text


@pytest.mark.asyncio
async def test_cloud_mode_transport_block_has_no_lan() -> None:
    coordinator = _diagnostics_coordinator()
    coordinator._transport = TRANSPORT_CLOUD  # type: ignore[attr-defined]
    coordinator._lan_manager = None  # type: ignore[attr-defined]
    coordinator.running_on_cache = False
    block = coordinator.transport_diagnostics()
    assert block["mode"] == "cloud"
    assert block["lan"] == {}
    assert block["broadcast_addresses"] == []


# ----- Options flow: c0 search and two-step save -----


@pytest.fixture(autouse=True)
def bypass_setup_entry():
    with patch("custom_components.aifa_smart.async_setup_entry", return_value=True):
        yield




def _entry(hass: HomeAssistant, *, with_hub_list: bool = True) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="user@example.com",
        data={CONF_EMAIL: "user@example.com", CONF_ACCESS_TOKEN: "access"},
        options={},
    )
    entry.add_to_hass(hass)
    if with_hub_list:
        entry.runtime_data = types.SimpleNamespace(
            data=AifaAccountData(devices={"7001": _device()}, macros=[], fetched_at=_T0)
        )
    return entry


def _search(replies: dict[str, str] | Exception):
    mock = AsyncMock()
    if isinstance(replies, Exception):
        mock.side_effect = replies
    else:
        mock.return_value = replies
    return (
        patch.object(
            config_flow,
            "async_default_broadcast_addresses",
            AsyncMock(return_value=["192.0.2.255"]),
        ),
        patch.object(config_flow, "async_search_hubs", mock),
        mock,
    )


async def _submit(hass: HomeAssistant, flow_id: str, transport: str):
    return await hass.config_entries.options.async_configure(
        flow_id, {CONF_TRANSPORT: transport}
    )


@pytest.mark.asyncio
@pytest.mark.usefixtures("enable_custom_integrations")
async def test_found_hub_saves_on_first_submit(hass: HomeAssistant) -> None:
    entry = _entry(hass)
    addr_patch, search_patch, search = _search({"a0:b1:c2:d3:e4:f5": "192.0.2.10"})
    with addr_patch, search_patch:
        result = await _open_transport(hass, entry)
        result = await _submit(hass, result["flow_id"], TRANSPORT_LAN)
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options == {CONF_TRANSPORT: TRANSPORT_LAN}
    assert search.await_count == 1


@pytest.mark.asyncio
@pytest.mark.usefixtures("enable_custom_integrations")
async def test_missing_hub_warns_then_second_submit_saves(hass: HomeAssistant) -> None:
    entry = _entry(hass)
    addr_patch, search_patch, search = _search({})
    with addr_patch, search_patch:
        result = await _open_transport(hass, entry)
        result = await _submit(hass, result["flow_id"], TRANSPORT_AUTO)
        assert result["type"] is FlowResultType.FORM
        assert result["errors"] == {"base": "hub_not_found"}
        assert result["description_placeholders"] == {"hubs": "Hub"}
        assert result["data_schema"]({})[CONF_TRANSPORT] == TRANSPORT_AUTO
        assert entry.options == {}

        result = await _submit(hass, result["flow_id"], TRANSPORT_AUTO)
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options == {CONF_TRANSPORT: TRANSPORT_AUTO}
    assert search.await_count == 1


@pytest.mark.asyncio
@pytest.mark.usefixtures("enable_custom_integrations")
async def test_switching_choice_after_warning_searches_again(hass: HomeAssistant) -> None:
    entry = _entry(hass)
    addr_patch, search_patch, search = _search({})
    with addr_patch, search_patch:
        result = await _open_transport(hass, entry)
        result = await _submit(hass, result["flow_id"], TRANSPORT_LAN)
        result = await _submit(hass, result["flow_id"], TRANSPORT_AUTO)
        assert result["type"] is FlowResultType.FORM
        assert search.await_count == 2


@pytest.mark.asyncio
@pytest.mark.usefixtures("enable_custom_integrations")
async def test_cloud_choice_never_searches(hass: HomeAssistant) -> None:
    entry = _entry(hass)
    addr_patch, search_patch, search = _search({})
    with addr_patch, search_patch:
        result = await _open_transport(hass, entry)
        result = await _submit(hass, result["flow_id"], TRANSPORT_CLOUD)
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert search.await_count == 0


@pytest.mark.asyncio
@pytest.mark.usefixtures("enable_custom_integrations")
async def test_unloaded_entry_warns_only_when_nothing_answers(hass: HomeAssistant) -> None:
    entry = _entry(hass, with_hub_list=False)
    addr_patch, search_patch, _search_mock = _search({})
    with addr_patch, search_patch:
        result = await _open_transport(hass, entry)
        result = await _submit(hass, result["flow_id"], TRANSPORT_LAN)
    assert result["errors"] == {"base": "no_hub_found"}


@pytest.mark.asyncio
@pytest.mark.usefixtures("enable_custom_integrations")
async def test_search_socket_error_counts_as_not_found(hass: HomeAssistant) -> None:
    entry = _entry(hass)
    addr_patch, search_patch, _search_mock = _search(OSError("address in use"))
    with addr_patch, search_patch:
        result = await _open_transport(hass, entry)
        result = await _submit(hass, result["flow_id"], TRANSPORT_LAN)
    assert result["errors"] == {"base": "hub_not_found"}


DEVICE_CODE_CHANGE_ERRORS = {
    "learned_keys",
    "current_code_unsupported",
    "code_unsupported",
    "sub_device_unsupported",
    "set_code_not_sent",
    "set_code_mismatch",
    "set_code_rejected",
    "set_code_rejected_unverified",
    "set_code_unconfirmed",
    "cloud_update_failed",
    "cloud_update_unknown",
    "restore_unconfirmed",
    "restore_unconfirmed_cloud_unknown",
    "code_change_busy",
    "code_trial_running",
    "trial_key_not_sent",
    "trial_restore_unconfirmed",
}

CODE_MATCH_ERRORS = {
    "code_match_failed",
    "code_match_timeout",
    "code_match_none",
    "code_match_not_sent",
    "code_match_running",
    "code_match_unsupported",
}


def test_warning_strings_exist_in_every_language() -> None:
    from pathlib import Path

    component = Path(__file__).parent.parent / "custom_components" / "aifa_smart"
    for name in ("strings.json", "translations/en.json", "translations/zh-Hant.json"):
        errors = json.loads((component / name).read_text())["options"]["error"]
        assert set(errors) == {
            "hub_not_found",
            "no_hub_found",
            *DEVICE_CODE_CHANGE_ERRORS,
            *CODE_MATCH_ERRORS,
        }, name
        assert "{hubs}" in errors["hub_not_found"], name
