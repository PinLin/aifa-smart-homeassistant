"""Config flow for AIFA Smart."""
from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import AifaSmartApiClient, AifaSmartAuthError, AifaSmartConnectionError, AifaTokens
from .const import (
    CONF_ACCESS_TOKEN,
    CONF_EMAIL,
    CONF_PASSWORD,
    CONF_REFRESH_TOKEN,
    CONF_TOKEN_EXPIRES_AT,
    CONF_TOKEN_TYPE,
    DOMAIN,
)

_LOGGER = logging.getLogger(__name__)


def _token_data_fields(tokens: AifaTokens | None) -> dict[str, Any]:
    """Return the token fields to merge into a config-entry data payload."""
    if tokens is None:
        return {}
    return {
        CONF_ACCESS_TOKEN: tokens.access_token,
        CONF_REFRESH_TOKEN: tokens.refresh_token,
        CONF_TOKEN_TYPE: tokens.token_type,
        CONF_TOKEN_EXPIRES_AT: tokens.expires_at.isoformat()
        if tokens.expires_at is not None
        else None,
    }


def _user_schema(defaults: dict[str, Any] | None = None) -> vol.Schema:
    """Build the main config schema."""
    defaults = defaults or {}
    return vol.Schema(
        {
            vol.Required(CONF_EMAIL, default=defaults.get(CONF_EMAIL, "")): str,
            vol.Required(CONF_PASSWORD, default=defaults.get(CONF_PASSWORD, "")): str,
        }
    )


class AifaSmartConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for AIFA Smart."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.FlowResult:
        """Handle the initial step."""
        errors: dict[str, str] = {}

        if user_input is not None:
            email = user_input[CONF_EMAIL].strip()
            password = user_input[CONF_PASSWORD]
            await self.async_set_unique_id(email.lower())
            self._abort_if_unique_id_configured()

            session = async_get_clientsession(self.hass)
            client = AifaSmartApiClient(session, email=email, password=password)
            try:
                await client.async_validate_credentials()
                await client.async_get_devices()
            except AifaSmartAuthError:
                errors["base"] = "invalid_auth"
            except AifaSmartConnectionError:
                errors["base"] = "cannot_connect"
            except Exception:
                _LOGGER.exception("Unexpected exception while validating AIFA Smart credentials")
                errors["base"] = "unknown"
            else:
                data: dict[str, Any] = {CONF_EMAIL: email}
                data.update(_token_data_fields(client.tokens))
                return self.async_create_entry(
                    title=email,
                    data=data,
                )

        return self.async_show_form(
            step_id="user",
            data_schema=_user_schema(user_input),
            errors=errors,
        )

    def _reauth_email(self) -> str:
        """Return the email from the reauth entry, or an empty string."""
        entry_id = self.context.get("entry_id") if self.context else None
        if not entry_id:
            return ""
        entry = self.hass.config_entries.async_get_entry(entry_id)
        if entry is None:
            return ""
        return str(entry.data.get(CONF_EMAIL) or "")

    async def async_step_reauth(
        self, entry_data: dict[str, Any]
    ) -> config_entries.FlowResult:
        """Trigger reauth when the stored tokens can no longer authenticate."""
        return await self.async_step_reauth_confirm()

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.FlowResult:
        """Handle user-initiated reconfigure by reusing the reauth prompt."""
        return await self.async_step_reauth_confirm(user_input)

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.FlowResult:
        """Prompt the user to re-enter their password."""
        errors: dict[str, str] = {}
        email = self._reauth_email()

        if user_input is not None:
            password = user_input[CONF_PASSWORD]
            session = async_get_clientsession(self.hass)
            client = AifaSmartApiClient(
                session, email=email, password=password
            )
            try:
                await client.async_validate_credentials()
            except AifaSmartAuthError:
                errors["base"] = "invalid_auth"
            except AifaSmartConnectionError:
                errors["base"] = "cannot_connect"
            except Exception:
                _LOGGER.exception("Unexpected exception during AIFA Smart reauth")
                errors["base"] = "unknown"
            else:
                entry_id = self.context.get("entry_id") if self.context else None
                entry = (
                    self.hass.config_entries.async_get_entry(entry_id)
                    if entry_id
                    else None
                )
                if entry is None:
                    return self.async_abort(reason="reauth_successful")

                new_data = {**entry.data, CONF_EMAIL: email}
                new_data.pop(CONF_PASSWORD, None)
                new_data.update(_token_data_fields(client.tokens))
                self.hass.config_entries.async_update_entry(entry, data=new_data)
                await self.hass.config_entries.async_reload(entry.entry_id)
                return self.async_abort(reason="reauth_successful")

        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=vol.Schema({vol.Required(CONF_PASSWORD): str}),
            description_placeholders={"email": email},
            errors=errors,
        )

