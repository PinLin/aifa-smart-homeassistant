"""Config flow for AIFA Smart."""
from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.core import callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import (
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
)

from .api import AifaSmartApiClient, AifaSmartAuthError, AifaSmartConnectionError, AifaTokens
from .classic_listener import build_hub_specs
from .const import (
    CONF_ACCESS_TOKEN,
    CONF_EMAIL,
    CONF_PASSWORD,
    CONF_REFRESH_TOKEN,
    CONF_TOKEN_EXPIRES_AT,
    CONF_TOKEN_TYPE,
    CONF_TRANSPORT,
    DOMAIN,
    TRANSPORT_CLOUD,
    TRANSPORTS,
    transport_from_options,
)
from .lan import async_default_broadcast_addresses, async_search_hubs, normalize_mac

_LOGGER = logging.getLogger(__name__)

# Newer HA cores reload the entry after an options change and reject entries
# that also carry an update listener; older cores need the listener.
OPTIONS_FLOW_RELOADS_ENTRY = hasattr(config_entries, "OptionsFlowWithReload")
_OptionsFlowBase: type[config_entries.OptionsFlow] = getattr(
    config_entries, "OptionsFlowWithReload", config_entries.OptionsFlow
)


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


def _options_schema(transport: str) -> vol.Schema:
    """Build the options schema with the current transport preselected."""
    return vol.Schema(
        {
            vol.Required(CONF_TRANSPORT, default=transport): SelectSelector(
                SelectSelectorConfig(
                    options=list(TRANSPORTS),
                    translation_key=CONF_TRANSPORT,
                    mode=SelectSelectorMode.LIST,
                )
            ),
        }
    )


class AifaSmartOptionsFlow(_OptionsFlowBase):
    """Let the user pick how commands reach the hub."""

    _warned_transport: str | None = None

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.FlowResult:
        """Show or store the transport option.

        LAN and auto first look for the hubs with a c0 search. When one is
        missing the form comes back with a warning, and submitting the same
        choice again saves it anyway.
        """
        transport = transport_from_options(self.config_entry.options)
        errors: dict[str, str] = {}
        placeholders: dict[str, str] = {}
        if user_input is not None:
            transport = transport_from_options(user_input)
            if transport == TRANSPORT_CLOUD or transport == self._warned_transport:
                return self._async_save(transport)
            missing = await self._async_missing_hubs()
            if missing is None:
                return self._async_save(transport)
            self._warned_transport = transport
            if missing:
                errors["base"] = "hub_not_found"
                placeholders["hubs"] = ", ".join(missing)
            else:
                errors["base"] = "no_hub_found"
        return self.async_show_form(
            step_id="init",
            data_schema=_options_schema(transport),
            errors=errors,
            description_placeholders=placeholders,
        )

    def _async_save(self, transport: str) -> config_entries.FlowResult:
        return self.async_create_entry(
            data={**self.config_entry.options, CONF_TRANSPORT: transport}
        )

    async def _async_missing_hubs(self) -> list[str] | None:
        """Return None when every hub answered, else the names that did not.

        An empty list means no hub list was available and nothing answered.
        """
        coordinator = getattr(self.config_entry, "runtime_data", None)
        data = getattr(coordinator, "data", None)
        known: dict[str, str] = {}
        if data is not None:
            for device_id, spec in build_hub_specs(data.devices, require_online=False).items():
                known[normalize_mac(spec.mac)] = data.devices[device_id].name
        try:
            replies = await async_search_hubs(
                await async_default_broadcast_addresses(self.hass)
            )
        except OSError as err:
            _LOGGER.debug("AIFA LAN search from the options flow failed: %s", err)
            replies = {}
        found = {normalize_mac(mac) for mac in replies}
        if known:
            missing = [name for mac, name in known.items() if mac not in found]
            return missing or None
        return None if found else []


class AifaSmartConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for AIFA Smart."""

    VERSION = 1

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: config_entries.ConfigEntry,
    ) -> AifaSmartOptionsFlow:
        """Return the options flow handler."""
        return AifaSmartOptionsFlow()

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

