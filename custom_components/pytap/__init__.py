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
from homeassistant.helpers import entity_registry as er

from .const import CONF_MODULE_BARCODE, CONF_MODULES, DOMAIN
from .coordinator import PyTapDataUpdateCoordinator

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [Platform.SENSOR]

# Bumped from 1 → 2 when voltage/current were split into in/out variants.
CONFIG_ENTRY_VERSION = 2

# Old unique IDs that were replaced by the _in/_out split.
_LEGACY_UNIQUE_ID_RENAMES: dict[str, str] = {
    "voltage": "voltage_in",
    "current": "current_in",
}
# Old unique IDs that no longer have a corresponding entity at all.
_LEGACY_UNIQUE_IDS_TO_REMOVE: set[str] = {"voltage", "current"}


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up PyTap from a config entry.

    1. Clean up legacy entity unique IDs from pre-v0.2.0.
    2. Create the streaming coordinator.
    3. Run an initial refresh to validate connectivity.
    4. Start the background listener task.
    5. Store the coordinator and forward platform setup.
    """
    hass.data.setdefault(DOMAIN, {})

    # --- Legacy entity migration (voltage/current → voltage_in/out, current_in/out) ---
    await _async_cleanup_legacy_entities(hass, entry)

    coordinator = PyTapDataUpdateCoordinator(hass, entry)

    # Validates that the coordinator can be initialised; does not block on data
    await coordinator.async_config_entry_first_refresh()

    # Start the background streaming listener
    await coordinator.async_start_listener()

    # Ensure listener is stopped on entry unload (covers HA shutdown path)
    entry.async_on_unload(coordinator.async_stop_listener)

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


async def async_migrate_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Migrate a config entry to the current version.

    v1 → v2: voltage/current sensors split into _in/_out variants.
    """
    if entry.version < CONFIG_ENTRY_VERSION:
        _LOGGER.info(
            "Migrating PyTap config entry %s from version %d to %d",
            entry.entry_id,
            entry.version,
            CONFIG_ENTRY_VERSION,
        )
        hass.config_entries.async_update_entry(entry, version=CONFIG_ENTRY_VERSION)
    return True


async def _async_cleanup_legacy_entities(
    hass: HomeAssistant, entry: ConfigEntry
) -> None:
    """Remove legacy entity registry entries left over from the voltage/current rename.

    Before v0.2.0, sensors used unique IDs like ``pytap_BARCODE_voltage`` and
    ``pytap_BARCODE_current``. These were replaced by ``_voltage_in``,
    ``_voltage_out``, ``_current_in``, and ``_current_out``. The old entries
    linger in the entity registry as orphaned/unavailable entities.
    """
    ent_reg = er.async_get(hass)
    modules: list[dict[str, str]] = entry.data.get(CONF_MODULES, [])
    removed = 0

    for module in modules:
        barcode = module.get(CONF_MODULE_BARCODE, "")
        if not barcode:
            continue
        for old_suffix in _LEGACY_UNIQUE_IDS_TO_REMOVE:
            old_unique_id = f"{DOMAIN}_{barcode}_{old_suffix}"
            entity_id = ent_reg.async_get_entity_id("sensor", DOMAIN, old_unique_id)
            if entity_id is not None:
                _LOGGER.info(
                    "Removing legacy entity %s (unique_id=%s)",
                    entity_id,
                    old_unique_id,
                )
                ent_reg.async_remove(entity_id)
                removed += 1

    if removed:
        _LOGGER.info("Cleaned up %d legacy sensor entities", removed)
