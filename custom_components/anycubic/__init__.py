"""Anycubic 3D Printer (local) integration setup."""
from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from homeassistant.helpers.device_registry import DeviceEntry

from .anycubic_local.const import redacted_error
from .anycubic_local.exceptions import CloudModeError, HandshakeError
from .anycubic_local.handshake import do_handshake
from .const import DOMAIN, PLATFORMS, ace_suffix
from .coordinator import AnycubicCoordinator

type AnycubicConfigEntry = ConfigEntry[AnycubicCoordinator]


async def async_setup_entry(hass: HomeAssistant, entry: AnycubicConfigEntry) -> bool:
    # Home Assistant logs both of these messages at its normal level, so the exception's text
    # goes through the redactor first. No handshake has succeeded here, which leaves the
    # entered address as the one thing known that it could name.
    try:
        hs = await hass.async_add_executor_job(do_handshake, entry.data[CONF_HOST])
    except CloudModeError as err:
        # LAN Mode was turned off on the printer — guide the user to re-enable it via reauth.
        raise ConfigEntryAuthFailed(redacted_error(err, (entry.data[CONF_HOST],))) from err
    except (HandshakeError, OSError) as err:
        raise ConfigEntryNotReady(redacted_error(err, (entry.data[CONF_HOST],))) from err
    coordinator = AnycubicCoordinator(hass, hs, host=entry.data[CONF_HOST])
    try:
        await coordinator.async_start()
    except OSError as err:
        # The handshake is HTTP and the session is MQTT, and a printer that has just been
        # switched on answers the first before its broker accepts the second — Home
        # Assistant and the printer starting together is enough. Every way the connect
        # fails then is an OSError (refused, timed out, a TLS handshake cut short, a name
        # that does not resolve), and all of them pass with waiting, so setup is retried.
        # Outside this, it was a setup error that stayed until a reload by hand. The
        # handshake has succeeded here, so the redactor is given everything it reported
        # as well as the entered address.
        raise ConfigEntryNotReady(
            "the printer's broker is not accepting connections yet: "
            + redacted_error(err, (*coordinator.redaction_identifiers, entry.data[CONF_HOST]))
        ) from err
    entry.runtime_data = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    # The options (the camera source) are read when the platforms are set up, so a change
    # takes effect by setting the entry up again. Only a change to the options: this
    # listener also hears the address being changed, and that flow reloads the entry
    # itself — reloading here as well would do it twice.
    loaded_with = dict(entry.options)

    async def _async_reload_on_new_options(hass: HomeAssistant, entry: ConfigEntry) -> None:
        if dict(entry.options) != loaded_with:
            await hass.config_entries.async_reload(entry.entry_id)

    entry.async_on_unload(entry.add_update_listener(_async_reload_on_new_options))
    return True


async def async_unload_entry(hass: HomeAssistant, entry: AnycubicConfigEntry) -> bool:
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def async_remove_config_entry_device(
    hass: HomeAssistant, entry: AnycubicConfigEntry, device: DeviceEntry
) -> bool:
    """Allow deleting a multi-material box the printer no longer reports.

    Boxes come and go (a second ACE unplugged, or the phantom box-0 device an older
    install left on printers with a built-in changer — issue #8). Without this hook Home
    Assistant offers no Delete button at all, so those devices are stuck forever. The
    printer itself and every currently reported box stay protected.
    """
    coordinator = entry.runtime_data
    serial = coordinator.hs.serial
    live = {(DOMAIN, serial)} | {
        (DOMAIN, f"{serial}_{ace_suffix(box.id)}") for box in coordinator.data.ace
    }
    return not (device.identifiers & live)
