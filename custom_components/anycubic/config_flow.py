"""Config flow: collect the printer IP, validate via the LAN handshake."""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from homeassistant.const import CONF_HOST

from .anycubic_local.exceptions import HandshakeError
from .anycubic_local.handshake import HandshakeResult, do_handshake
from .const import DOMAIN, MODEL_NAMES

# Characters that are never part of a bare address and that would change what the handshake
# URL means: the address is interpolated into "http://{host}:18910/info" exactly as typed.
_NOT_IN_AN_ADDRESS = frozenset("/:@?#")


def _clean_host(typed: str) -> str | None:
    """The address as typed, trimmed; None if it cannot be a bare IP address or hostname.

    "a.b/c", "user@a.b", "a.b:81" and "a.b?x" would each send the handshake somewhere, or
    ask it for something, other than the address the user believes they entered. Those are
    refused before any request is made and before anything is stored. Whitespace round the
    outside is only ever a paste artefact, so it is trimmed rather than refused; anywhere
    else, like any other unprintable character, it cannot be part of an address.
    """
    host = typed.strip()
    if not host or any(c in _NOT_IN_AN_ADDRESS or not c.isprintable() or c.isspace()
                       for c in host):
        return None
    return host


class AnycubicConfigFlow(ConfigFlow, domain=DOMAIN):
    VERSION = 1

    async def _async_identify(self, typed: str) -> tuple[str, HandshakeResult] | None:
        """Check an address and ask the printer there who it is; None if it cannot be used.

        One path for every step that takes an address, so adding a printer and moving one
        run the same checks and the same handshake. A printer that gives no serial counts
        as unusable here: with nothing to tell it apart by, it can neither become an entry
        nor be matched to one, and every caller relies on the serial being real.
        """
        host = _clean_host(typed)
        if host is None:
            return None
        try:
            hs = await self.hass.async_add_executor_job(do_handshake, host)
        except (HandshakeError, OSError):
            return None
        return (host, hs) if hs.serial else None

    async def async_step_user(self, user_input=None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            found = await self._async_identify(user_input[CONF_HOST])
            if found is None:
                errors["base"] = "cannot_connect"
            else:
                host, hs = found
                await self.async_set_unique_id(hs.serial)
                # Adding a printer that is already here, at a different address, is what
                # people try when the printer has moved: so move the entry, rather than
                # answer "already configured" and leave it pointing at the dead address.
                # Safe only because _async_identify never returns an empty serial, which
                # would otherwise match any entry that has no unique id of its own.
                self._abort_if_unique_id_configured(updates={CONF_HOST: host})
                title = MODEL_NAMES.get(hs.model_id, "Anycubic printer")
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

    async def async_step_reconfigure(
            self, entry_data: Mapping[str, Any] | None = None) -> ConfigFlowResult:
        """Change the printer's address without deleting the entry.

        A printer given a new address by DHCP left its entry in setup-retry for good: reauth
        only re-checks the address it already has. Having this step is also what makes Home
        Assistant offer "Reconfigure" on the entry.

        It only hands over to the form. The frontend starts this flow with no data, but
        Home Assistant 2024.9 and 2024.10 could also start it with a copy of the entry's
        data, and that must not be taken for something the user typed.
        """
        return await self.async_step_reconfigure_confirm()

    async def async_step_reconfigure_confirm(self, user_input=None) -> ConfigFlowResult:
        # Everything here is spelled the 2024.9 way, like reauth above: _get_reconfigure_entry,
        # _abort_if_unique_id_mismatch, data_updates= and the automatic abort reason all
        # arrived in HA 2024.11.
        entry = self.hass.config_entries.async_get_entry(self.context["entry_id"])
        errors: dict[str, str] = {}
        if user_input is not None:
            found = await self._async_identify(user_input[CONF_HOST])
            # The entry's unique id is the only thing that says which printer this is. The
            # entry is normally NOT loaded when someone needs this step, so there is no
            # coordinator to ask. With no unique id nothing can be confirmed, so nothing
            # is accepted.
            if found is None or not entry.unique_id:
                errors["base"] = "cannot_connect"
            elif found[1].serial != entry.unique_id:
                # Deliberately not _abort_if_unique_id_configured(updates=...): if the printer
                # that answered is configured as well, that helper would re-point ITS entry
                # at the address typed here.
                errors["base"] = "wrong_printer"
            else:
                return self.async_update_reload_and_abort(
                    entry, data={**entry.data, CONF_HOST: found[0]},
                    reason="reconfigure_successful")
        # Pre-filled with the current address, or with what was just typed so a typo can be
        # corrected rather than retyped.
        shown = entry.data[CONF_HOST] if user_input is None else user_input[CONF_HOST]
        return self.async_show_form(
            step_id="reconfigure_confirm",
            data_schema=vol.Schema({vol.Required(CONF_HOST, default=shown): str}), errors=errors)
