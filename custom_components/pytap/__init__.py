"""The PyTap integration.

Lifecycle management for the PyTap Home Assistant custom component.
Creates the push-based streaming coordinator, starts the background listener,
and wires up platform forwarding and teardown.
"""

from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant

from .const import DOMAIN
from .coordinator import PyTapDataUpdateCoordinator

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [Platform.SENSOR]


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up PyTap from a config entry.

    1. Create the streaming coordinator.
    2. Run an initial refresh to validate connectivity.
    3. Start the background listener task.
    4. Store the coordinator and forward platform setup.
    """
    hass.data.setdefault(DOMAIN, {})

    coordinator = PyTapDataUpdateCoordinator(hass, entry)

    # Validates that the coordinator can be initialised; does not block on data
    await coordinator.async_config_entry_first_refresh()

    # Start the background streaming listener
    await coordinator.async_start_listener()

    hass.data[DOMAIN][entry.entry_id] = coordinator

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    # Listen for options updates to reload module configuration
    entry.async_on_unload(entry.add_update_listener(_async_update_options))

    return True


async def _async_update_options(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Handle options update — reload the integration."""
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry.

    1. Unload platforms.
    2. Stop the coordinator's background listener.
    3. Remove coordinator from hass.data.
    """
    if unload_ok := await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        coordinator: PyTapDataUpdateCoordinator = hass.data[DOMAIN].pop(entry.entry_id)
        await coordinator.async_stop_listener()

    return unload_ok
