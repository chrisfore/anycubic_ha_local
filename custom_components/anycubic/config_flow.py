"""Config flow: collect the printer IP, validate via the LAN handshake."""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from homeassistant.const import CONF_HOST

from .anycubic_local.exceptions import HandshakeError
from .anycubic_local.handshake import do_handshake
from .const import DOMAIN, MODEL_NAMES


class AnycubicConfigFlow(ConfigFlow, domain=DOMAIN):
    VERSION = 1

    async def async_step_user(self, user_input=None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            host = user_input[CONF_HOST]
            try:
                hs = await self.hass.async_add_executor_job(do_handshake, host)
            except (HandshakeError, OSError):
                errors["base"] = "cannot_connect"
            else:
                if not hs.serial:
                    errors["base"] = "cannot_connect"
                else:
                    await self.async_set_unique_id(hs.serial)
                    self._abort_if_unique_id_configured()
                    title = MODEL_NAMES.get(hs.model_id, "AnyCubic printer")
                    return self.async_create_entry(title=title, data={CONF_HOST: host})
        return self.async_show_form(
            step_id="user", data_schema=vol.Schema({vol.Required(CONF_HOST): str}), errors=errors)

    async def async_step_reauth(self, entry_data: Mapping[str, Any]) -> ConfigFlowResult:
        """Triggered when LAN Mode was turned off on the printer (handshake hit cloud mode)."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(self, user_input=None) -> ConfigFlowResult:
        # async_get_entry by context entry_id (not _get_reauth_entry, which only exists since
        # HA 2024.11) keeps the reauth flow working down to our declared 2024.9 minimum.
        entry = self.hass.config_entries.async_get_entry(self.context["entry_id"])
        errors: dict[str, str] = {}
        if user_input is not None:
            try:
                await self.hass.async_add_executor_job(do_handshake, entry.data[CONF_HOST])
            except (HandshakeError, OSError):
                errors["base"] = "cannot_connect"
            else:
                return self.async_update_reload_and_abort(entry, data=entry.data)
        return self.async_show_form(
            step_id="reauth_confirm", errors=errors,
            description_placeholders={"host": entry.data[CONF_HOST]})
