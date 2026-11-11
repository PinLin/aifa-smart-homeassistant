"""Config flow for AIFA Smart."""
from __future__ import annotations

import asyncio
import logging
from dataclasses import replace
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

from .ac import is_supported_ac_sub_device, set_code_slot
from .api import (
    AifaDeviceCode,
    AifaSmartApiClient,
    AifaSmartAuthError,
    AifaSmartConnectionError,
    AifaSmartError,
    AifaSubDevice,
    AifaTokens,
)
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
from .coordinator import DeviceCodeChangeError, settable_device_code
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


_STEP_TRANSPORT = "transport"
_STEP_DEVICE_CODE = "device_code"
_STEP_MATCH = "match"
_STEP_MATCH_RESULT = "match_result"
CONF_SUB_DEVICE = "sub_device"
CONF_METHOD = "method"
METHOD_BRAND = "brand"
METHOD_MATCH = "match"
CONF_BRAND = "brand"
CONF_CODE = "code"
CONF_ACTION = "action"
ACTION_TRY = "try"
ACTION_SAVE = "save"
_NO_BRAND = "-"


def _select(options: list[dict[str, str]]) -> SelectSelector:
    return SelectSelector(
        SelectSelectorConfig(options=options, mode=SelectSelectorMode.DROPDOWN)
    )


def _settable_rows(rows: list[AifaDeviceCode]) -> list[AifaDeviceCode]:
    """Rows the AIFA Smart app sets with SetCode: remote == false, and 4 digits at most."""
    return [
        row
        for row in rows
        if row.remote is False and settable_device_code(row.code) is not None
    ]


def _candidate_rows(
    codes: list[int], rows: list[AifaDeviceCode]
) -> list[AifaDeviceCode]:
    """One row per settable candidate, in the hub's order.

    The brand is kept only when a single brand lists the code. The AIFA Smart app
    sets any candidate, so a code missing from the catalog still gets a row.
    """
    candidates: list[AifaDeviceCode] = []
    for code in codes:
        if settable_device_code(str(code)) is None:
            continue
        listed = [row for row in rows if settable_device_code(row.code) == code]
        if listed and len({row.brand_id for row in listed}) == 1:
            candidates.append(listed[0])
        elif listed:
            candidates.append(
                replace(listed[0], brand_id=None, brand_name=None, brand_localized_name=None)
            )
        else:
            candidates.append(
                AifaDeviceCode(
                    id="",
                    code=str(code),
                    brand_id=None,
                    brand_name=None,
                    brand_localized_name=None,
                    country=None,
                    version=None,
                    subversion=None,
                    type=None,
                    remote=False,
                    popular=None,
                )
            )
    return candidates


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
    """Pick the transport, or switch an air conditioner to another remote code."""

    _warned_transport: str | None = None
    _device_id: str | None = None
    _sub_device: AifaSubDevice | None = None
    _rows: list[AifaDeviceCode] | None = None
    _brand: str | None = None
    # The coordinator holding this flow's code trial; a reload replaces it.
    _trial_coordinator: Any = None
    _match_task: asyncio.Task[tuple[list[int], list[AifaDeviceCode]] | str] | None = None
    _match_error: str | None = None
    _match_codes: list[int] | None = None
    # Set once a match found codes; the code page then lists only these.
    _match_rows: list[AifaDeviceCode] | None = None

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.FlowResult:
        """Offer the two option pages."""
        return self.async_show_menu(
            step_id="init", menu_options=[_STEP_TRANSPORT, _STEP_DEVICE_CODE]
        )

    async def async_step_transport(
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
            step_id=_STEP_TRANSPORT,
            data_schema=_options_schema(transport),
            errors=errors,
            description_placeholders=placeholders,
        )

    def _coordinator(self) -> Any:
        coordinator = getattr(self.config_entry, "runtime_data", None)
        if coordinator is None or getattr(coordinator, "data", None) is None:
            return None
        return coordinator

    async def async_step_device_code(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.FlowResult:
        """Choose the air conditioner whose code changes."""
        coordinator = self._coordinator()
        if coordinator is None:
            return self.async_abort(reason="not_loaded")
        candidates: dict[str, tuple[str, str, AifaSubDevice]] = {}
        for device in coordinator.data.devices.values():
            for sub_device in device.sub_devices:
                if is_supported_ac_sub_device(sub_device.type, sub_device.device_code):
                    candidates[sub_device.id] = (device.id, device.name, sub_device)
        if not candidates:
            return self.async_abort(reason="no_sub_devices")

        errors: dict[str, str] = {}
        if user_input is not None:
            device_id, _, sub_device = candidates[user_input[CONF_SUB_DEVICE]]
            trial = coordinator.code_trial(device_id)
            if trial is not None and trial.owner != self.flow_id:
                errors["base"] = "code_change_busy"
            elif sub_device.learned_key_count:
                errors["base"] = "learned_keys"
            elif set_code_slot(sub_device.sub_type) != 0xA0:
                errors["base"] = "sub_device_unsupported"
            elif settable_device_code(sub_device.device_code) is None:
                errors["base"] = "current_code_unsupported"
            else:
                self._device_id = device_id
                self._sub_device = sub_device
                if user_input.get(CONF_METHOD) == METHOD_MATCH:
                    return await self.async_step_match()
                return await self.async_step_brand()

        options = [
            {
                "value": sub_id,
                "label": f"{hub_name} / {sub_device.name} ({sub_device.device_code})",
            }
            for sub_id, (_, hub_name, sub_device) in candidates.items()
        ]
        return self.async_show_form(
            step_id=_STEP_DEVICE_CODE,
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_SUB_DEVICE,
                        default=(user_input or {}).get(CONF_SUB_DEVICE, options[0]["value"]),
                    ): _select(options),
                    vol.Required(
                        CONF_METHOD,
                        default=(user_input or {}).get(CONF_METHOD, METHOD_BRAND),
                    ): SelectSelector(
                        SelectSelectorConfig(
                            options=[METHOD_BRAND, METHOD_MATCH],
                            translation_key=CONF_METHOD,
                            mode=SelectSelectorMode.LIST,
                        )
                    ),
                }
            ),
            errors=errors,
        )

    async def async_step_match(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.FlowResult:
        """Let the hub read the original remote, like the AIFA Smart app's one-touch match.

        The match takes up to 15 s, longer than a flow step may wait, so it
        runs as the progress task; a closed dialog cancels it.
        """
        sub_device = self._sub_device
        assert sub_device is not None
        task = self._match_task
        if task is None and user_input is not None:
            coordinator = self._coordinator()
            if coordinator is None:
                return self.async_abort(reason="not_loaded")
            task = self._match_task = self.hass.async_create_task(
                self._async_match_codes(coordinator),
                name=f"aifa_smart_match_device_code_{sub_device.id}",
            )
            # A refusal ends the task at once; a progress step cannot show
            # a form, so its error is shown here.
            if task.done():
                self._match_task = None
                if not self._take_match_result(task):
                    return self._match_form()
                return await self.async_step_match_result()
        if task is not None:
            if not task.done():
                return self.async_show_progress(
                    step_id=_STEP_MATCH, progress_action=_STEP_MATCH, progress_task=task
                )
            self._match_task = None
            return self.async_show_progress_done(
                next_step_id=_STEP_MATCH_RESULT if self._take_match_result(task) else _STEP_MATCH
            )
        return self._match_form()

    def _match_form(self) -> config_entries.FlowResult:
        assert self._sub_device is not None
        errors: dict[str, str] = {}
        if self._match_error is not None:
            errors["base"] = self._match_error
            self._match_error = None
        return self.async_show_form(
            step_id=_STEP_MATCH,
            data_schema=vol.Schema({}),
            errors=errors,
            description_placeholders={"name": self._sub_device.name},
        )

    def _take_match_result(
        self, task: asyncio.Task[tuple[list[int], list[AifaDeviceCode]] | str]
    ) -> bool:
        """Keep a finished match's codes; False with `_match_error` set when it failed."""
        result = "code_match_failed" if task.cancelled() else task.result()
        if isinstance(result, str):
            self._match_error = result
            return False
        self._match_codes, self._match_rows = result
        return True

    async def _async_match_codes(
        self, coordinator: Any
    ) -> tuple[list[int], list[AifaDeviceCode]] | str:
        """Return (hub candidates, rows to try), or the error key."""
        sub_device = self._sub_device
        assert sub_device is not None
        try:
            codes = await coordinator.async_match_device_code(self._device_id, sub_device.id)
        except DeviceCodeChangeError as err:
            return err.reason
        except Exception:
            # A raising progress task would leave the dialog spinning.
            _LOGGER.exception("AIFA code match failed")
            return "code_match_failed"
        try:
            catalog = await coordinator.client.async_get_sub_device_device_codes(
                sub_device.id
            )
        except AifaSmartError as err:
            # Brands only label the codes; the hub's candidates still work.
            _LOGGER.warning("Fetching AIFA device codes failed: %s", err)
            listed: list[AifaDeviceCode] = []
        else:
            listed = _settable_rows(catalog.device_codes)
        rows = _candidate_rows(codes, listed)
        if not rows:
            return "code_match_unsupported"
        return codes, rows

    async def async_step_match_result(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.FlowResult:
        """List the hub's candidates before they are tried one by one."""
        sub_device = self._sub_device
        assert sub_device is not None and self._match_codes is not None
        if user_input is not None:
            return await self.async_step_code()
        return self.async_show_form(
            step_id=_STEP_MATCH_RESULT,
            data_schema=vol.Schema({}),
            description_placeholders={
                "name": sub_device.name,
                "count": str(len(self._match_codes)),
                "codes": ", ".join(str(code) for code in self._match_codes),
            },
        )

    async def async_step_brand(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.FlowResult:
        """Choose the brand; the list comes from the sub-device's code catalog."""
        sub_device = self._sub_device
        assert sub_device is not None
        coordinator = self._coordinator()
        if coordinator is None:
            return self.async_abort(reason="not_loaded")
        if self._rows is None:
            try:
                catalog = await coordinator.client.async_get_sub_device_device_codes(
                    sub_device.id
                )
            except AifaSmartError as err:
                _LOGGER.warning("Fetching AIFA device codes failed: %s", err)
                return self.async_abort(reason="cannot_connect")
            self._rows = _settable_rows(catalog.device_codes)
            if not self._rows:
                return self.async_abort(reason="no_device_codes")

        if user_input is not None:
            self._brand = user_input[CONF_BRAND]
            return await self.async_step_code()

        labels: dict[str, str] = {}
        for row in self._rows:
            key = row.brand_id or _NO_BRAND
            labels.setdefault(
                key, row.brand_localized_name or row.brand_name or key
            )
        default = coordinator.selected_brand_id(sub_device)
        if default not in labels:
            current = next(
                (row for row in self._rows if row.code == sub_device.device_code), None
            )
            default = (current.brand_id or _NO_BRAND) if current is not None else None
        options = [
            {"value": key, "label": label}
            for key, label in sorted(labels.items(), key=lambda item: item[1].casefold())
        ]
        return self.async_show_form(
            step_id=CONF_BRAND,
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_BRAND, default=default or options[0]["value"]
                    ): _select(options)
                }
            ),
            description_placeholders={
                "name": sub_device.name,
                "current_code": str(sub_device.device_code),
            },
        )

    async def async_step_code(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.FlowResult:
        """Try codes on the hub, like the AIFA Smart app's pairing, then save one.

        A try leaves the hub on the code without touching the cloud; the
        hub goes back to the cloud's code when the flow ends without a save.
        """
        sub_device = self._sub_device
        assert sub_device is not None
        rows = self._code_rows()
        old_code = str(settable_device_code(sub_device.device_code))
        placeholders = {
            "name": sub_device.name,
            "current_code": old_code,
            "new_code": "",
        }
        errors: dict[str, str] = {}
        coordinator = self._coordinator()
        if user_input is not None and user_input[CONF_CODE] in rows:
            new_code = user_input[CONF_CODE]
            if coordinator is None:
                return self.async_abort(reason="not_loaded")
            placeholders["new_code"] = new_code
            row = rows[new_code]
            if user_input.get(CONF_ACTION, ACTION_TRY) == ACTION_TRY:
                self._trial_coordinator = coordinator
                try:
                    await coordinator.async_try_device_code(
                        self._device_id, sub_device.id, row, owner=self.flow_id
                    )
                except DeviceCodeChangeError as err:
                    errors["base"] = err.reason
            elif new_code == old_code:
                try:
                    restored = await coordinator.async_end_code_trial(
                        self._device_id, self.flow_id
                    )
                except DeviceCodeChangeError as err:
                    errors["base"] = err.reason
                    restored = None
                if restored is False:
                    errors["base"] = "trial_restore_unconfirmed"
                elif restored:
                    # Picking a brand for the code in use is how its brand is
                    # set when several brands share the code. A match that
                    # could not tell the brand keeps the one recorded.
                    if self._match_rows is None or row.brand_id is not None:
                        await coordinator.async_select_brand(
                            self._device_id, sub_device.id, row
                        )
                    return self.async_abort(
                        reason="device_code_unchanged",
                        description_placeholders=placeholders,
                    )
            else:
                try:
                    await coordinator.async_change_device_code(
                        self._device_id, sub_device.id, row, owner=self.flow_id
                    )
                except DeviceCodeChangeError as err:
                    errors["base"] = err.reason
                else:
                    return self.async_abort(
                        reason="device_code_changed",
                        description_placeholders=placeholders,
                    )

        placeholders["hub_code"] = self._hub_code(coordinator, old_code)
        options = [{"value": code, "label": code} for code in rows]
        default = (user_input or {}).get(CONF_CODE) or (
            old_code
            if old_code in rows and self._match_rows is None
            else options[0]["value"]
        )
        return self.async_show_form(
            step_id=CONF_CODE,
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_CODE, default=default): _select(options),
                    vol.Required(CONF_ACTION, default=ACTION_TRY): SelectSelector(
                        SelectSelectorConfig(
                            options=[ACTION_TRY, ACTION_SAVE],
                            translation_key=CONF_ACTION,
                            mode=SelectSelectorMode.LIST,
                        )
                    ),
                }
            ),
            errors=errors,
            description_placeholders=placeholders,
        )

    def _code_rows(self) -> dict[str, AifaDeviceCode]:
        """The codes the code page offers, in the order it lists them.

        A match keeps the hub's order: its first candidate is the likeliest.
        """
        if self._match_rows is not None:
            return {str(settable_device_code(row.code)): row for row in self._match_rows}
        assert self._rows is not None
        rows: dict[str, AifaDeviceCode] = {}
        for row in self._rows:
            if (row.brand_id or _NO_BRAND) == self._brand:
                rows.setdefault(str(settable_device_code(row.code)), row)
        return dict(sorted(rows.items(), key=lambda item: int(item[0])))

    def _hub_code(self, coordinator: Any, cloud_code: str) -> str:
        """The code the hub uses as far as this flow knows, for the form text."""
        trial = None if coordinator is None else coordinator.code_trial(self._device_id)
        if trial is None:
            return cloud_code
        # Another dialog's trial: this one cannot tell what the hub uses.
        if trial.owner != self.flow_id or trial.hub_code is None:
            return "?"
        return str(trial.hub_code)

    @callback
    def async_remove(self) -> None:
        """Put the hub back on the cloud's code when the dialog goes away unsaved.

        HA calls this for a closed dialog, an abort and a finished flow; a
        saved code has already ended the trial, so nothing is sent then.
        """
        # HA cancels the progress task first; a match has nothing to undo.
        if self._match_task is not None:
            self._match_task.cancel()
        coordinator = self._trial_coordinator
        if coordinator is not None and self._device_id is not None:
            coordinator.async_release_code_trial(self._device_id, self.flow_id)

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

