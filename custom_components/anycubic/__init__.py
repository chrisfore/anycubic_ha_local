"""AnyCubic 3D Printer (local) integration setup."""
from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady

from .anycubic_local.exceptions import CloudModeError, HandshakeError
from .anycubic_local.handshake import do_handshake
from .const import PLATFORMS
from .coordinator import AnycubicCoordinator

type AnycubicConfigEntry = ConfigEntry[AnycubicCoordinator]


async def async_setup_entry(hass: HomeAssistant, entry: AnycubicConfigEntry) -> bool:
    try:
        hs = await hass.async_add_executor_job(do_handshake, entry.data[CONF_HOST])
    except CloudModeError as err:
        # LAN Mode was turned off on the printer — guide the user to re-enable it via reauth.
        raise ConfigEntryAuthFailed(str(err)) from err
    except (HandshakeError, OSError) as err:
        raise ConfigEntryNotReady(str(err)) from err
    coordinator = AnycubicCoordinator(hass, hs, host=entry.data[CONF_HOST])
    await coordinator.async_start()
    entry.runtime_data = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: AnycubicConfigEntry) -> bool:
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
