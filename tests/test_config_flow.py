"""Tests for the AIFA Smart config and options flow."""
from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.config_entries import SOURCE_REAUTH, SOURCE_USER
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.aifa_smart import _tokens_from_entry, config_flow
from custom_components.aifa_smart.api import AifaTokens
from custom_components.aifa_smart.const import (
    CONF_ACCESS_TOKEN,
    CONF_EMAIL,
    CONF_PASSWORD,
    CONF_REFRESH_TOKEN,
    CONF_TOKEN_EXPIRES_AT,
    CONF_TOKEN_TYPE,
    DOMAIN,
)


@pytest.fixture(autouse=True)
def bypass_setup_entry():
    """Skip the full integration setup; we only test the flow itself."""
    with patch(
        "custom_components.aifa_smart.async_setup_entry",
        return_value=True,
    ):
        yield


@pytest.fixture(autouse=True)
def verify_cleanup():
    """Override PHCC's verify_cleanup for this file.

    A successful CREATE_ENTRY result causes HA to spin up entity/device
    registries and an aiohttp resolver thread under the hood. Those are
    framework-owned (not from our code), but PHCC's lingering-thread
    assertion still catches them. Suppressing the check keeps these flow
    tests focused on behaviour we control.
    """
    yield


def _make_client(*, tokens: AifaTokens | None = None) -> AsyncMock:
    """Build a fake AifaSmartApiClient that records the rotated tokens."""
    client = AsyncMock()
    client.async_validate_credentials = AsyncMock(return_value=None)
    client.async_get_devices = AsyncMock(return_value=[])
    client.tokens = tokens
    return client


def test_user_schema_only_collects_credentials() -> None:
    """Initial setup form should only ask for the email and password."""
    schema = config_flow._user_schema()
    parsed = schema({CONF_EMAIL: "user@example.com", CONF_PASSWORD: "secret"})

    assert parsed[CONF_EMAIL] == "user@example.com"
    assert parsed[CONF_PASSWORD] == "secret"
    assert "update_interval" not in parsed


@pytest.mark.asyncio
@pytest.mark.usefixtures("enable_custom_integrations")
async def test_user_step_persists_tokens_from_validated_client(
    hass: HomeAssistant,
) -> None:
    """A successful login should stash the client's OAuth tokens in entry.data."""
    client = _make_client(
        tokens=AifaTokens(
            access_token="access-123",
            refresh_token="refresh-456",
            token_type="Bearer",
            expires_at=None,
        )
    )

    with patch(
        "custom_components.aifa_smart.config_flow.AifaSmartApiClient",
        return_value=client,
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": SOURCE_USER}
        )
        assert result["type"] is FlowResultType.FORM
        assert result["step_id"] == "user"

        result2 = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {CONF_EMAIL: "user@example.com", CONF_PASSWORD: "secret"},
        )

    assert result2["type"] is FlowResultType.CREATE_ENTRY
    assert result2["data"][CONF_EMAIL] == "user@example.com"
    assert result2["data"][CONF_ACCESS_TOKEN] == "access-123"
    assert result2["data"][CONF_REFRESH_TOKEN] == "refresh-456"
    assert result2["data"][CONF_TOKEN_TYPE] == "Bearer"
    assert result2["data"][CONF_TOKEN_EXPIRES_AT] is None
    # The password must NOT be persisted in the config entry.
    assert CONF_PASSWORD not in result2["data"]


@pytest.mark.asyncio
@pytest.mark.usefixtures("enable_custom_integrations")
async def test_reauth_flow_stores_new_tokens_and_drops_password(
    hass: HomeAssistant,
) -> None:
    """Reauth should swap in new tokens and scrub any legacy password field."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="user@example.com",
        data={
            CONF_EMAIL: "user@example.com",
            # Legacy password lingering from pre-token installations.
            CONF_PASSWORD: "legacy-password",
            CONF_ACCESS_TOKEN: "old-access",
            CONF_REFRESH_TOKEN: "revoked-refresh",
        },
    )
    entry.add_to_hass(hass)

    client = _make_client(
        tokens=AifaTokens(
            access_token="new-access",
            refresh_token="new-refresh",
            token_type="Bearer",
            expires_at=None,
        )
    )

    with patch(
        "custom_components.aifa_smart.config_flow.AifaSmartApiClient",
        return_value=client,
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN,
            context={
                "source": SOURCE_REAUTH,
                "entry_id": entry.entry_id,
                "unique_id": entry.unique_id,
            },
            data=entry.data,
        )
        assert result["type"] is FlowResultType.FORM
        assert result["step_id"] == "reauth_confirm"

        final = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_PASSWORD: "new-password"}
        )

    assert final["type"] is FlowResultType.ABORT
    assert final["reason"] == "reauth_successful"
    assert entry.data[CONF_EMAIL] == "user@example.com"
    assert entry.data[CONF_ACCESS_TOKEN] == "new-access"
    assert entry.data[CONF_REFRESH_TOKEN] == "new-refresh"
    assert CONF_PASSWORD not in entry.data


def test_tokens_from_entry_rebuilds_token_dataclass() -> None:
    """Stored token fields should round-trip back into AifaTokens."""
    expires = datetime(2026, 4, 24, 12, 0, 0, tzinfo=UTC)
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_EMAIL: "user@example.com",
            CONF_PASSWORD: "secret",
            CONF_ACCESS_TOKEN: "access-123",
            CONF_REFRESH_TOKEN: "refresh-456",
            CONF_TOKEN_TYPE: "Bearer",
            CONF_TOKEN_EXPIRES_AT: expires.isoformat(),
        },
    )

    tokens = _tokens_from_entry(entry)

    assert tokens is not None
    assert tokens.access_token == "access-123"
    assert tokens.refresh_token == "refresh-456"
    assert tokens.token_type == "Bearer"
    assert tokens.expires_at == expires


def test_tokens_from_entry_tolerates_bad_expires_at() -> None:
    """A malformed expires_at string should not break startup."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_ACCESS_TOKEN: "access-123",
            CONF_TOKEN_EXPIRES_AT: "not-a-timestamp",
        },
    )

    tokens = _tokens_from_entry(entry)

    assert tokens is not None
    assert tokens.access_token == "access-123"
    assert tokens.expires_at is None
