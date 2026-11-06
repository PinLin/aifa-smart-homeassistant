"""Offline start-up from the device cache (Q3)."""
from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import STATE_UNAVAILABLE
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.aifa_smart import async_first_refresh_or_cache
from custom_components.aifa_smart.api import (
    AifaDevice,
    AifaDeviceCodeCatalog,
    AifaMacro,
    AifaMacroCommand,
    AifaSmartApiClient,
    AifaSmartAuthError,
    AifaSmartConnectionError,
    AifaSubDevice,
)
from custom_components.aifa_smart.const import (
    CONF_ACCESS_TOKEN,
    CONF_EMAIL,
    CONF_REFRESH_TOKEN,
    CONF_TOKEN_EXPIRES_AT,
    CONF_TOKEN_TYPE,
    CONF_TRANSPORT,
    DOMAIN,
    TRANSPORT_AUTO,
    TRANSPORT_CLOUD,
    TRANSPORT_LAN,
)
from custom_components.aifa_smart.device_cache import (
    CACHE_VERSION,
    cache_store_key,
    deserialize_account,
    serialize_account,
)

_SECRET_ACCESS = "SECRET-ACCESS-TOKEN-1234"
_SECRET_REFRESH = "SECRET-REFRESH-TOKEN-5678"
_SECRET_RAW = "SECRET-RAW-TOKEN-9999"
_FORBIDDEN_KEYS = {
    "raw",
    "token",
    "access_token",
    "refresh_token",
    "password",
    "email",
    "functions",
    "temperature",
    "humidity",
    "sensor_samples",
}


def _device(*, online: bool | None = True) -> AifaDevice:
    sub_device = AifaSubDevice(
        id="ac-1",
        device_id="7001",
        name="Living room AC",
        type="0",
        sub_type="0",
        device_code="115",
        ac_available_modes=["cool", "fan_only"],
        estimated_capabilities=["mode"],
        raw={"token": _SECRET_RAW, "accessToken": _SECRET_ACCESS},
    )
    return AifaDevice(
        id="7001",
        name="Hub",
        mac="A0B1C2D3E4F5",
        device_type="2",
        online=online,
        temperature=27.5,
        humidity=61.0,
        firmware="5.0",
        version=5,
        subversion=210623,
        sub_devices=[sub_device],
        raw={"token": _SECRET_RAW, "refresh_token": _SECRET_REFRESH},
    )


def _macros() -> list[AifaMacro]:
    return [
        AifaMacro(
            id="m1",
            name="Cool down",
            commands=[
                AifaMacroCommand(
                    id="mc1",
                    macro_id="m1",
                    device_id="7001",
                    sub_device_id="ac-1",
                    command="ffa016f0",
                    delay=500,
                    order_no=1,
                    raw={"token": _SECRET_RAW},
                )
            ],
            raw={"token": _SECRET_RAW},
        )
    ]


def _all_keys(value: object) -> set[str]:
    if isinstance(value, dict):
        keys = set(value)
        for child in value.values():
            keys |= _all_keys(child)
        return keys
    if isinstance(value, list):
        keys: set[str] = set()
        for child in value:
            keys |= _all_keys(child)
        return keys
    return set()


def test_serialized_cache_has_no_credentials_or_raw_payloads() -> None:
    body = serialize_account({"7001": _device()}, _macros())
    text = json.dumps(body)
    for secret in (_SECRET_ACCESS, _SECRET_REFRESH, _SECRET_RAW):
        assert secret not in text
    assert not (_all_keys(body) & _FORBIDDEN_KEYS)


def test_round_trip_keeps_what_entities_and_lan_need() -> None:
    devices, macros = deserialize_account(
        json.loads(json.dumps(serialize_account({"7001": _device()}, _macros())))
    )
    device = devices["7001"]
    assert (device.mac, device.version, device.subversion) == ("A0B1C2D3E4F5", 5, 210623)
    assert device.online is None
    assert device.temperature is None and device.humidity is None
    sub_device = device.sub_devices[0]
    assert (sub_device.type, sub_device.device_code) == ("0", "115")
    assert sub_device.ac_available_modes == ["cool", "fan_only"]
    assert sub_device.raw == {} and sub_device.functions == []
    assert macros[0].commands[0].command == "ffa016f0"
    assert macros[0].commands[0].delay == 500


@pytest.mark.parametrize(
    "body",
    [
        None,
        [],
        {"devices": [{"name": "no id"}]},
        {"devices": [{"id": "7001", "sub_devices": [{"id": ""}]}]},
        {"devices": "nope"},
        {"devices": [], "macros": [{"id": "m1", "commands": [42]}]},
    ],
)
def test_malformed_bodies_raise_value_error(body) -> None:
    with pytest.raises((TypeError, ValueError)):
        deserialize_account(body)


class _FakeCoordinator:
    def __init__(self, transport: str, error: Exception | None, cache_hit: bool) -> None:
        self.transport = transport
        self.error = error
        self.cache_hit = cache_hit
        self.cache_calls = 0

    async def async_config_entry_first_refresh(self) -> None:
        if self.error is not None:
            raise self.error

    async def async_start_from_cache(self) -> bool:
        self.cache_calls += 1
        return self.cache_hit


def _not_ready() -> ConfigEntryNotReady:
    err = ConfigEntryNotReady()
    err.__cause__ = AifaSmartConnectionError("cloud down")
    return err


@pytest.mark.asyncio
@pytest.mark.parametrize("transport", [TRANSPORT_LAN, TRANSPORT_AUTO])
async def test_lan_modes_fall_back_to_cache(transport: str) -> None:
    coordinator = _FakeCoordinator(transport, _not_ready(), cache_hit=True)
    await async_first_refresh_or_cache(coordinator)  # type: ignore[arg-type]
    assert coordinator.cache_calls == 1


@pytest.mark.asyncio
async def test_lan_mode_without_cache_is_not_ready() -> None:
    coordinator = _FakeCoordinator(TRANSPORT_LAN, _not_ready(), cache_hit=False)
    with pytest.raises(ConfigEntryNotReady):
        await async_first_refresh_or_cache(coordinator)  # type: ignore[arg-type]


@pytest.mark.asyncio
@pytest.mark.parametrize("transport", [TRANSPORT_CLOUD, TRANSPORT_LAN, TRANSPORT_AUTO])
async def test_auth_failure_never_uses_cache(transport: str) -> None:
    coordinator = _FakeCoordinator(transport, ConfigEntryAuthFailed("revoked"), cache_hit=True)
    with pytest.raises(ConfigEntryAuthFailed):
        await async_first_refresh_or_cache(coordinator)  # type: ignore[arg-type]
    assert coordinator.cache_calls == 0


@pytest.mark.asyncio
async def test_cloud_mode_never_uses_cache() -> None:
    coordinator = _FakeCoordinator(TRANSPORT_CLOUD, _not_ready(), cache_hit=True)
    with pytest.raises(ConfigEntryNotReady):
        await async_first_refresh_or_cache(coordinator)  # type: ignore[arg-type]
    assert coordinator.cache_calls == 0


# ----- Full setup against a real hass; the LAN manager is a stub -----


class _StubLan:
    def __init__(self, **kwargs) -> None:
        self.synced: list[dict] = []
        self.links: dict = {}
        self.broadcast_addresses: list[str] = []

    def sync_specs(self, devices) -> None:
        self.synced.append(dict(devices))

    def is_available(self, device_id: str) -> bool:
        return True

    def is_connected(self, device_id: str) -> bool:
        return True

    def snapshot(self) -> dict:
        return {}

    async def async_request_status(self, device_id: str) -> bool:
        return True

    async def shutdown(self) -> None:
        return None


@pytest.fixture
def verify_cleanup():
    """HA-owned threads linger after full setups, as in the flow tests."""
    yield


def _entry(hass: HomeAssistant, transport: str) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="user@example.com",
        data={
            CONF_EMAIL: "user@example.com",
            CONF_ACCESS_TOKEN: _SECRET_ACCESS,
            CONF_REFRESH_TOKEN: _SECRET_REFRESH,
            CONF_TOKEN_TYPE: "Bearer",
            CONF_TOKEN_EXPIRES_AT: "2099-01-01T00:00:00+00:00",
        },
        options={CONF_TRANSPORT: transport},
    )
    entry.add_to_hass(hass)
    return entry


def _seed_cache(hass_storage: dict, entry_id: str, body: object) -> str:
    key = cache_store_key(entry_id)
    hass_storage[key] = {"version": CACHE_VERSION, "minor_version": 1, "key": key, "data": body}
    return key


def _patches(*, devices_error: Exception | None = None):
    async def get_devices(self):
        if devices_error is not None:
            raise devices_error
        return [_device()]

    return [
        patch("custom_components.aifa_smart.coordinator.LanTransportManager", _StubLan),
        patch(
            "custom_components.aifa_smart.catalog.async_initialize",
            AsyncMock(return_value=lambda: None),
        ),
        patch.object(AifaSmartApiClient, "async_get_devices", get_devices),
        patch.object(AifaSmartApiClient, "async_get_macros", AsyncMock(return_value=_macros())),
        patch.object(AifaSmartApiClient, "async_get_functions", AsyncMock(return_value=[])),
        patch.object(
            AifaSmartApiClient,
            "async_get_sub_device_device_codes",
            AsyncMock(return_value=AifaDeviceCodeCatalog()),
        ),
        patch.object(
            AifaSmartApiClient, "async_query_classic_ac_statuses", AsyncMock(return_value={})
        ),
        patch(
            "custom_components.aifa_smart.classic_listener.ClassicSocketManager.sync_specs",
            lambda self, devices: None,
        ),
    ]


async def _setup(hass: HomeAssistant, entry: MockConfigEntry, **kwargs) -> None:
    patches = _patches(**kwargs)
    for item in patches:
        item.start()
    try:
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    finally:
        for item in patches:
            item.stop()


def _entity_ids(hass: HomeAssistant, entry: MockConfigEntry, domain: str) -> list[str]:
    registry = er.async_get(hass)
    return [
        item.entity_id
        for item in er.async_entries_for_config_entry(registry, entry.entry_id)
        if item.domain == domain
    ]


@pytest.mark.asyncio
@pytest.mark.usefixtures("enable_custom_integrations")
async def test_lan_setup_starts_from_cache_when_cloud_is_down(
    hass: HomeAssistant, hass_storage: dict
) -> None:
    entry = _entry(hass, TRANSPORT_LAN)
    _seed_cache(hass_storage, entry.entry_id, serialize_account({"7001": _device()}, _macros()))

    await _setup(hass, entry, devices_error=AifaSmartConnectionError("cloud down"))

    assert entry.state is ConfigEntryState.LOADED
    coordinator = entry.runtime_data
    assert coordinator.running_on_cache is True
    assert coordinator.last_update_success is False
    assert list(coordinator.lan_manager.synced[-1]) == ["7001"]
    climates = _entity_ids(hass, entry, "climate")
    assert climates
    assert hass.states.get(climates[0]).state != STATE_UNAVAILABLE
    readings = [
        entity_id
        for entity_id in _entity_ids(hass, entry, "sensor")
        if "temperature" in entity_id or "humidity" in entity_id
    ]
    assert readings
    for entity_id in readings:
        assert hass.states.get(entity_id).state == STATE_UNAVAILABLE
    assert await hass.config_entries.async_unload(entry.entry_id)
    # Unload keeps the cache for the next (possibly offline) reload.
    assert cache_store_key(entry.entry_id) in hass_storage


@pytest.mark.asyncio
@pytest.mark.usefixtures("enable_custom_integrations")
async def test_auth_failure_goes_to_reauth_even_with_cache(
    hass: HomeAssistant, hass_storage: dict
) -> None:
    entry = _entry(hass, TRANSPORT_LAN)
    _seed_cache(hass_storage, entry.entry_id, serialize_account({"7001": _device()}, _macros()))

    await _setup(hass, entry, devices_error=AifaSmartAuthError("revoked"))

    assert entry.state is ConfigEntryState.SETUP_ERROR
    flows = hass.config_entries.flow.async_progress_by_handler(DOMAIN)
    assert any(flow["context"]["source"] == "reauth" for flow in flows)


@pytest.mark.asyncio
@pytest.mark.usefixtures("enable_custom_integrations")
async def test_cloud_setup_ignores_cache(hass: HomeAssistant, hass_storage: dict) -> None:
    entry = _entry(hass, TRANSPORT_CLOUD)
    _seed_cache(hass_storage, entry.entry_id, serialize_account({"7001": _device()}, _macros()))

    await _setup(hass, entry, devices_error=AifaSmartConnectionError("cloud down"))

    assert entry.state is ConfigEntryState.SETUP_RETRY


@pytest.mark.asyncio
@pytest.mark.usefixtures("enable_custom_integrations")
async def test_corrupt_cache_is_discarded_and_setup_retries(
    hass: HomeAssistant, hass_storage: dict
) -> None:
    entry = _entry(hass, TRANSPORT_LAN)
    key = _seed_cache(hass_storage, entry.entry_id, {"devices": [{"name": "no id"}]})

    await _setup(hass, entry, devices_error=AifaSmartConnectionError("cloud down"))

    assert entry.state is ConfigEntryState.SETUP_RETRY
    assert key not in hass_storage


@pytest.mark.asyncio
@pytest.mark.usefixtures("enable_custom_integrations")
async def test_successful_refresh_writes_cache_without_tokens(
    hass: HomeAssistant, hass_storage: dict
) -> None:
    entry = _entry(hass, TRANSPORT_LAN)

    await _setup(hass, entry)

    assert entry.state is ConfigEntryState.LOADED
    stored = hass_storage[cache_store_key(entry.entry_id)]
    text = json.dumps(stored)
    for secret in (_SECRET_ACCESS, _SECRET_REFRESH, _SECRET_RAW, "user@example.com"):
        assert secret not in text
    assert not (_all_keys(stored["data"]) & _FORBIDDEN_KEYS)
    assert stored["data"]["devices"][0]["mac"] == "A0B1C2D3E4F5"
    assert await hass.config_entries.async_unload(entry.entry_id)


@pytest.mark.asyncio
@pytest.mark.usefixtures("enable_custom_integrations")
async def test_cloud_mode_writes_no_cache(hass: HomeAssistant, hass_storage: dict) -> None:
    entry = _entry(hass, TRANSPORT_CLOUD)

    await _setup(hass, entry)

    assert entry.state is ConfigEntryState.LOADED
    assert cache_store_key(entry.entry_id) not in hass_storage
    assert await hass.config_entries.async_unload(entry.entry_id)


@pytest.mark.asyncio
@pytest.mark.usefixtures("enable_custom_integrations")
async def test_removing_entry_deletes_cache(hass: HomeAssistant, hass_storage: dict) -> None:
    entry = _entry(hass, TRANSPORT_LAN)
    key = _seed_cache(hass_storage, entry.entry_id, serialize_account({"7001": _device()}, []))

    await hass.config_entries.async_remove(entry.entry_id)
    await hass.async_block_till_done()

    assert key not in hass_storage
