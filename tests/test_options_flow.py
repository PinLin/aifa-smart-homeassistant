"""Tests for the AIFA Smart transport options flow and its reload hook."""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
import voluptuous as vol
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.aifa_smart import async_setup_options_reload, config_flow
from custom_components.aifa_smart.const import (
    CONF_ACCESS_TOKEN,
    CONF_EMAIL,
    CONF_TRANSPORT,
    DEFAULT_TRANSPORT,
    DOMAIN,
    TRANSPORT_AUTO,
    TRANSPORT_CLOUD,
    TRANSPORT_LAN,
    TRANSPORTS,
    transport_from_options,
)

_COMPONENT_DIR = Path(__file__).parent.parent / "custom_components" / "aifa_smart"


@pytest.fixture(autouse=True)
def bypass_setup_entry():
    """Skip the full integration setup; we only test the flow itself."""
    with patch(
        "custom_components.aifa_smart.async_setup_entry",
        return_value=True,
    ):
        yield


@pytest.fixture(autouse=True)
def lan_search_finds_a_hub():
    """Choosing lan/auto runs a c0 search; never let it reach the real network."""
    with (
        patch.object(
            config_flow,
            "async_default_broadcast_addresses",
            AsyncMock(return_value=["192.0.2.255"]),
        ),
        patch.object(
            config_flow,
            "async_search_hubs",
            AsyncMock(return_value={"A0B1C2D3E4F5": "192.0.2.10"}),
        ),
    ):
        yield


@pytest.fixture(autouse=True)
def verify_cleanup():
    """Same override as test_config_flow: HA-owned threads linger after flows."""
    yield


def _entry(hass: HomeAssistant, options: dict | None = None) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="user@example.com",
        data={CONF_EMAIL: "user@example.com", CONF_ACCESS_TOKEN: "access"},
        options=options or {},
    )
    entry.add_to_hass(hass)
    return entry


def test_default_transport_is_cloud() -> None:
    assert DEFAULT_TRANSPORT == TRANSPORT_CLOUD
    assert transport_from_options({}) == TRANSPORT_CLOUD
    assert transport_from_options({CONF_TRANSPORT: "bogus"}) == TRANSPORT_CLOUD
    assert transport_from_options({CONF_TRANSPORT: TRANSPORT_AUTO}) == TRANSPORT_AUTO


@pytest.mark.asyncio
@pytest.mark.usefixtures("enable_custom_integrations")
async def test_options_form_defaults_to_cloud(hass: HomeAssistant) -> None:
    entry = _entry(hass)

    result = await hass.config_entries.options.async_init(entry.entry_id)

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "init"
    assert result["data_schema"]({})[CONF_TRANSPORT] == TRANSPORT_CLOUD


@pytest.mark.asyncio
@pytest.mark.usefixtures("enable_custom_integrations")
async def test_options_form_preselects_current_transport(hass: HomeAssistant) -> None:
    entry = _entry(hass, {CONF_TRANSPORT: TRANSPORT_LAN})

    result = await hass.config_entries.options.async_init(entry.entry_id)

    assert result["data_schema"]({})[CONF_TRANSPORT] == TRANSPORT_LAN


@pytest.mark.asyncio
@pytest.mark.usefixtures("enable_custom_integrations")
async def test_options_flow_writes_transport(hass: HomeAssistant) -> None:
    entry = _entry(hass, {"unrelated": 1})

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result2 = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_TRANSPORT: TRANSPORT_AUTO}
    )

    assert result2["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options == {"unrelated": 1, CONF_TRANSPORT: TRANSPORT_AUTO}


@pytest.mark.asyncio
@pytest.mark.usefixtures("enable_custom_integrations")
async def test_options_flow_rejects_unknown_transport(hass: HomeAssistant) -> None:
    entry = _entry(hass)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    with pytest.raises(vol.Invalid):
        await hass.config_entries.options.async_configure(
            result["flow_id"], {CONF_TRANSPORT: "bluetooth"}
        )


@pytest.mark.asyncio
@pytest.mark.usefixtures("enable_custom_integrations")
async def test_transport_change_reloads_entry(hass: HomeAssistant) -> None:
    entry = _entry(hass)
    reload = AsyncMock(return_value=True)

    with (
        patch.object(config_flow, "OPTIONS_FLOW_RELOADS_ENTRY", False),
        patch.object(hass.config_entries, "async_reload", reload),
    ):
        async_setup_options_reload(hass, entry)
        result = await hass.config_entries.options.async_init(entry.entry_id)
        await hass.config_entries.options.async_configure(
            result["flow_id"], {CONF_TRANSPORT: TRANSPORT_LAN}
        )
        await hass.async_block_till_done()

    reload.assert_awaited_once_with(entry.entry_id)


@pytest.mark.asyncio
@pytest.mark.usefixtures("enable_custom_integrations")
async def test_token_rotation_and_same_transport_do_not_reload(
    hass: HomeAssistant,
) -> None:
    entry = _entry(hass, {CONF_TRANSPORT: TRANSPORT_LAN})
    reload = AsyncMock(return_value=True)

    with (
        patch.object(config_flow, "OPTIONS_FLOW_RELOADS_ENTRY", False),
        patch.object(hass.config_entries, "async_reload", reload),
    ):
        async_setup_options_reload(hass, entry)
        hass.config_entries.async_update_entry(
            entry, data={**entry.data, CONF_ACCESS_TOKEN: "rotated"}
        )
        hass.config_entries.async_update_entry(
            entry, options={CONF_TRANSPORT: TRANSPORT_LAN, "unrelated": 2}
        )
        await hass.async_block_till_done()

    reload.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.usefixtures("enable_custom_integrations")
async def test_no_update_listener_when_core_reloads_options(
    hass: HomeAssistant,
) -> None:
    """Cores with OptionsFlowWithReload refuse entries that also have listeners."""
    entry = _entry(hass)

    with patch.object(config_flow, "OPTIONS_FLOW_RELOADS_ENTRY", True):
        async_setup_options_reload(hass, entry)

    assert entry.update_listeners == []


def test_options_flow_base_matches_core_capability() -> None:
    from homeassistant import config_entries

    expected = getattr(
        config_entries, "OptionsFlowWithReload", config_entries.OptionsFlow
    )
    assert issubclass(config_flow.AifaSmartOptionsFlow, expected)
    assert config_flow.OPTIONS_FLOW_RELOADS_ENTRY is hasattr(
        config_entries, "OptionsFlowWithReload"
    )


def _key_tree(value: object, prefix: str = "") -> set[str]:
    if not isinstance(value, dict):
        return {prefix}
    keys: set[str] = set()
    for key, child in value.items():
        keys |= _key_tree(child, f"{prefix}/{key}")
    return keys


def test_translations_cover_transport_options() -> None:
    strings = json.loads((_COMPONENT_DIR / "strings.json").read_text())
    english = json.loads((_COMPONENT_DIR / "translations" / "en.json").read_text())
    chinese = json.loads((_COMPONENT_DIR / "translations" / "zh-Hant.json").read_text())

    assert english == strings
    assert _key_tree(chinese) == _key_tree(strings)
    assert set(strings["selector"]["transport"]["options"]) == set(TRANSPORTS)
    assert "transport" in strings["options"]["step"]["init"]["data"]


def test_manifest_depends_on_network() -> None:
    manifest = json.loads((_COMPONENT_DIR / "manifest.json").read_text())
    assert "network" in manifest["dependencies"]
