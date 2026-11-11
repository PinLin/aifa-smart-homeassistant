"""The brand shown for a code several brands share is the one chosen in the options flow.

The cloud keeps only `deviceCode` for a sub-device, and AIFA Smart app 1.2.9
PATCHes only `{"deviceCode": N}`, so a code picked under a second brand
would otherwise show as the first catalog brand with that code.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType

from custom_components.aifa_smart.api import (
    AifaDeviceCode,
    AifaDeviceCodeCatalog,
    AifaSmartApiClient,
)
from custom_components.aifa_smart.const import (
    CONF_SELECTED_BRANDS,
    CONF_TRANSPORT,
    TRANSPORT_CLOUD,
)
from custom_components.aifa_smart.entity import common_sub_device_attributes
from custom_components.aifa_smart.sensor import AifaSubDeviceBrandSensor
from tests.test_device_code_change import FakeClient, Rig, _fast_set_code
from tests.test_device_cache import _entry
from tests.test_set_code_reject import RejectingFakeHub
from tests.test_unload_lifecycle import _Cloud, _patches

pytestmark = pytest.mark.usefixtures("socket_enabled")

_FIRST, _CHOSEN = "b1", "b2"


def _row(row_id: str, code: str, brand: str) -> AifaDeviceCode:
    names = {_FIRST: ("Brand One", "品牌一"), _CHOSEN: ("Brand Two", "品牌二")}
    return AifaDeviceCode(
        id=row_id,
        code=code,
        brand_id=brand,
        brand_name=names[brand][0],
        brand_localized_name=names[brand][1],
        country=1,
        version=5,
        subversion=210623,
        type=0,
        remote=False,
        popular=None,
    )


def _catalog() -> AifaDeviceCodeCatalog:
    return AifaDeviceCodeCatalog(
        brands={},
        device_codes=[
            _row("1", "115", _FIRST),
            _row("2", "33", _FIRST),
            _row("3", "115", _CHOSEN),
            _row("4", "33", _CHOSEN),
        ],
    )


def _shown(coordinator) -> tuple[str | None, str | None]:
    """(brand sensor value, brand id attribute) for the AC."""
    sensor = AifaSubDeviceBrandSensor(coordinator, "7001", "ac-1")
    device = coordinator.get_device("7001")
    sub_device = coordinator.get_sub_device("7001", "ac-1")
    return sensor.native_value, common_sub_device_attributes(device, sub_device)["sub_device_brand_id"]


# ----- Setup and reload -----


@pytest.fixture
def catalog_calls():
    cloud = _Cloud()
    patches = _patches(cloud, stub_lan=True)
    catalog = AsyncMock(side_effect=lambda *args: _catalog())
    patches.append(
        patch.object(AifaSmartApiClient, "async_get_sub_device_device_codes", catalog)
    )
    for item in patches:
        item.start()
    yield catalog
    for item in patches:
        item.stop()


async def _setup(hass: HomeAssistant, options: dict | None = None):
    entry = _entry(hass, TRANSPORT_CLOUD)
    if options is not None:
        hass.config_entries.async_update_entry(entry, options={**entry.options, **options})
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED
    return entry


@pytest.mark.usefixtures("enable_custom_integrations")
async def test_without_a_choice_the_first_brand_with_the_code_is_shown(
    hass: HomeAssistant, catalog_calls
) -> None:
    entry = await _setup(hass)
    assert _shown(entry.runtime_data) == ("品牌一", _FIRST)
    await hass.config_entries.async_unload(entry.entry_id)


@pytest.mark.usefixtures("enable_custom_integrations")
async def test_chosen_brand_is_shown_at_once_and_after_a_reload(
    hass: HomeAssistant, catalog_calls
) -> None:
    entry = await _setup(hass)
    coordinator = entry.runtime_data
    calls = catalog_calls.await_count

    await coordinator.async_select_brand("7001", "ac-1", _row("3", "115", _CHOSEN))

    assert _shown(coordinator) == ("品牌二", _CHOSEN)
    assert catalog_calls.await_count == calls
    assert entry.options[CONF_SELECTED_BRANDS] == {
        "ac-1": {"brand_id": _CHOSEN, "device_code": "115"}
    }
    assert entry.options[CONF_TRANSPORT] == TRANSPORT_CLOUD

    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.runtime_data is not coordinator
    assert _shown(entry.runtime_data) == ("品牌二", _CHOSEN)
    await hass.config_entries.async_unload(entry.entry_id)


@pytest.mark.usefixtures("enable_custom_integrations")
async def test_choice_for_another_code_falls_back_to_the_catalog_lookup(
    hass: HomeAssistant, catalog_calls
) -> None:
    # Chosen with 33; the cloud now says 115, e.g. after a change in the AIFA Smart app.
    entry = await _setup(
        hass, {CONF_SELECTED_BRANDS: {"ac-1": {"brand_id": _CHOSEN, "device_code": "33"}}}
    )
    assert _shown(entry.runtime_data) == ("品牌一", _FIRST)
    await hass.config_entries.async_unload(entry.entry_id)


@pytest.mark.usefixtures("enable_custom_integrations")
async def test_choice_matching_the_code_is_used_at_setup(
    hass: HomeAssistant, catalog_calls
) -> None:
    entry = await _setup(
        hass, {CONF_SELECTED_BRANDS: {"ac-1": {"brand_id": _CHOSEN, "device_code": "115"}}}
    )
    assert _shown(entry.runtime_data) == ("品牌二", _CHOSEN)
    await hass.config_entries.async_unload(entry.entry_id)


@pytest.mark.usefixtures("enable_custom_integrations")
async def test_choice_of_a_brand_without_the_code_falls_back(
    hass: HomeAssistant, catalog_calls
) -> None:
    entry = await _setup(
        hass, {CONF_SELECTED_BRANDS: {"ac-1": {"brand_id": "b9", "device_code": "115"}}}
    )
    assert _shown(entry.runtime_data) == ("品牌一", _FIRST)
    await hass.config_entries.async_unload(entry.entry_id)


# ----- Options flow -----


@pytest.fixture(autouse=True)
def bypass_setup_entry(request):
    if "catalog_calls" in request.fixturenames:
        yield
        return
    with patch("custom_components.aifa_smart.async_setup_entry", return_value=True):
        yield


class _SharedCodeClient(FakeClient):
    async def async_get_sub_device_device_codes(self, sub_device_id: str) -> AifaDeviceCodeCatalog:
        return _catalog()


@pytest.fixture
async def rig(hass: HomeAssistant):
    hub = await RejectingFakeHub().start()
    built = Rig(hass, hub)
    built.client = _SharedCodeClient()
    built.coordinator.client = built.client  # type: ignore[attr-defined]
    built.manager._on_set_code_reject = built.coordinator._apply_lan_set_code_reject  # noqa: SLF001
    with _fast_set_code():
        await built.connect()
        yield built
    await built.close()


async def _brand_form(rig: Rig):
    flow = await rig.hass.config_entries.options.async_init(rig.entry.entry_id)
    await rig.hass.config_entries.options.async_configure(
        flow["flow_id"], {"next_step_id": "device_code"}
    )
    result = await rig.hass.config_entries.options.async_configure(
        flow["flow_id"], {"sub_device": "ac-1"}
    )
    assert result["step_id"] == "brand"
    return result


async def _choose(rig: Rig, brand: str, code: str):
    form = await _brand_form(rig)
    result = await rig.hass.config_entries.options.async_configure(
        form["flow_id"], {"brand": brand}
    )
    assert result["step_id"] == "code"
    with patch.object(rig.hass.config_entries, "async_schedule_reload") as reload:
        result = await rig.hass.config_entries.options.async_configure(
            result["flow_id"], {"code": code, "action": "save"}
        )
    rig.reload = reload
    return result


def _records(rig: Rig) -> dict:
    return rig.entry.options.get(CONF_SELECTED_BRANDS, {})


@pytest.mark.usefixtures("enable_custom_integrations")
async def test_brand_step_defaults_to_the_catalog_lookup_without_a_choice(rig: Rig) -> None:
    form = await _brand_form(rig)
    assert form["data_schema"]({})["brand"] == _FIRST


@pytest.mark.usefixtures("enable_custom_integrations")
async def test_choosing_a_brand_for_the_code_in_use_records_it(rig: Rig) -> None:
    result = await _choose(rig, _CHOSEN, "115")

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "device_code_unchanged"
    assert rig.hub.received == bytearray()
    assert rig.client.patches == []
    assert _records(rig) == {"ac-1": {"brand_id": _CHOSEN, "device_code": "115"}}
    form = await _brand_form(rig)
    assert form["data_schema"]({})["brand"] == _CHOSEN


@pytest.mark.usefixtures("enable_custom_integrations")
async def test_changed_code_records_the_brand_it_was_chosen_under(rig: Rig) -> None:
    result = await _choose(rig, _CHOSEN, "33")

    assert result["reason"] == "device_code_changed"
    assert _records(rig) == {"ac-1": {"brand_id": _CHOSEN, "device_code": "33"}}
    assert rig.coordinator.selected_brand_id(rig.sub_device) == _CHOSEN


@pytest.mark.usefixtures("enable_custom_integrations")
@pytest.mark.parametrize("failure", ["unconfirmed", "rejected"])
async def test_failed_change_keeps_the_earlier_choice(rig: Rig, failure: str) -> None:
    await _choose(rig, _CHOSEN, "115")
    if failure == "rejected":
        rig.hub.rejected_codes = {33}
        expected = "set_code_rejected"
    else:
        rig.hub.silent_codes = {33}
        expected = "set_code_unconfirmed"

    result = await _choose(rig, _FIRST, "33")

    assert result["errors"] == {"base": expected}
    assert rig.sub_device.device_code == "115"
    assert _records(rig) == {"ac-1": {"brand_id": _CHOSEN, "device_code": "115"}}
    assert rig.coordinator.selected_brand_id(rig.sub_device) == _CHOSEN
