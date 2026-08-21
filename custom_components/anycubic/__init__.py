"""AnyCubic 3D Printer (local) integration setup."""
from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from homeassistant.helpers.device_registry import DeviceEntry

from .anycubic_local.exceptions import CloudModeError, HandshakeError
from .anycubic_local.handshake import do_handshake
from .const import DOMAIN, PLATFORMS, ace_suffix
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
