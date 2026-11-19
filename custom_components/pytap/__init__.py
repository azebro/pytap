"""The PyTap integration.

Lifecycle management for the PyTap Home Assistant custom component.
Creates the push-based streaming coordinator, starts the background listener,
and wires up platform forwarding and teardown.
"""

from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.const import EVENT_HOMEASSISTANT_STOP, Platform
from homeassistant.core import CALLBACK_TYPE, Event, HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryError, ConfigEntryNotReady
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.typing import ConfigType

from .capture_services import async_remove_captures, register_capture_services
from .connection import InvalidConnectionSettings
from .const import (
    CONF_AUTO_DISCOVERY,
    CONF_BAUD_RATE,
    CONF_CONNECTION_TYPE,
    CONF_EXCLUDED_BARCODES,
    CONF_HOST,
    CONF_LIVE_REPORTING,
    CONF_MODULE_BARCODE,
    CONF_MODULE_PEAK_POWER,
    CONF_MODULE_STRING,
    CONF_MODULES,
    CONF_PORT,
    CONF_SERIAL_PORT,
    CONF_WRITE_INTERVAL,
    CONNECTION_TCP,
    DEFAULT_LIVE_REPORTING,
    DEFAULT_PEAK_POWER,
    DEFAULT_STRING_NAME,
    DEFAULT_WRITE_INTERVAL,
    DOMAIN,
    MAX_WRITE_INTERVAL,
    MIN_WRITE_INTERVAL,
)
from .coordinator import (
    PyTapConfigEntry,
    PyTapDataUpdateCoordinator,
    coordinator_store,
    parked_coordinators,
)

_LOGGER = logging.getLogger(__name__)
CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)

PLATFORMS: list[Platform] = [Platform.BINARY_SENSOR, Platform.SENSOR]

# Readable names for the setting named in an invalid_connection_settings error.
_SETTING_NAMES: dict[str, str] = {
    "base": "connection type",
    CONF_HOST: "host",
    CONF_PORT: "port",
    CONF_SERIAL_PORT: "serial port",
    CONF_BAUD_RATE: "baud rate",
}

# Bumped from 1 → 2 when voltage/current were split into in/out variants.
# Bumped from 2 → 3 when module string labels became mandatory.
# Bumped from 3 → 4 when module peak_power became mandatory in config.
# Bumped from 4 → 5 when live_reporting toggle was added.
# Bumped from 5 → 6 when serial/proxy connections were added.
# Bumped from 6 → 7 when automatic module discovery became configurable.
CONFIG_ENTRY_VERSION = 7

# Old unique IDs that were replaced by the _in/_out split.
_LEGACY_UNIQUE_ID_RENAMES: dict[str, str] = {
    "voltage": "voltage_in",
    "current": "current_in",
}
# Old unique IDs that no longer have a corresponding entity at all.
_LEGACY_UNIQUE_IDS_TO_REMOVE: set[str] = {"voltage", "current"}


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Register actions once, independent of an individual config entry."""
    register_capture_services(hass)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: PyTapConfigEntry) -> bool:
    """Set up PyTap from a config entry.

    1. Wait for any previous listener for this entry to release its port.
    2. Clean up legacy entity unique IDs from pre-v0.2.0.
    3. Create the coordinator and start the background listener (no
       connectivity check: an offline gateway is retried in the background).
    4. Remove devices for modules that are no longer configured.
    5. Store the coordinator and forward platform setup.
    """
    # A timed-out stop must not allow a new coordinator to claim the same port.
    # Raising NotReady makes Home Assistant retry setup until it has stopped.
    parked = parked_coordinators(hass)
    if previous := parked.get(entry.entry_id):
        if not await previous.async_stop_listener():
            raise ConfigEntryNotReady(translation_domain=DOMAIN, translation_key="previous_listener_stopping")
        if parked.get(entry.entry_id) is previous:
            del parked[entry.entry_id]
        previous.release_barcode_claims()

    # --- Legacy entity migration (voltage/current → voltage_in/out, current_in/out) ---
    await _async_cleanup_legacy_entities(hass, entry)

    try:
        coordinator = PyTapDataUpdateCoordinator(hass, entry)
    except InvalidConnectionSettings as err:
        # Older versions could save e.g. an out-of-range port. Ask the user
        # to fix it in the options flow rather than retrying forever.
        raise ConfigEntryError(
            translation_domain=DOMAIN,
            translation_key="invalid_connection_settings",
            translation_placeholders={"field": _SETTING_NAMES.get(err.field, err.field)},
        ) from err

    try:
        await coordinator.async_config_entry_first_refresh()
        await coordinator.async_start_listener()
        entry.runtime_data = coordinator
        _async_stop_on_shutdown(hass, entry, coordinator)

        # Skipped if saved state couldn't be read: discovered modules are unknown.
        if coordinator.store_loaded:
            _async_remove_orphaned_devices(hass, entry, coordinator)

        await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

        # Listen for options updates to reload module configuration
        entry.async_on_unload(entry.add_update_listener(_async_update_options))
    except BaseException:
        # Don't leak timers or barcode claims, or leave a running worker
        # unguarded: a retry must not open the port before it's released.
        await _async_stop_or_park(hass, entry.entry_id, coordinator)
        raise

    return True


@callback
def _async_stop_on_shutdown(
    hass: HomeAssistant, entry: ConfigEntry, coordinator: PyTapDataUpdateCoordinator
) -> None:
    """Stop the listener when Home Assistant stops.

    Home Assistant doesn't unload entries on shutdown: stop the listener and
    finalise any packet capture explicitly so its files stay verifiable.
    """
    remove_listener: CALLBACK_TYPE | None = None

    @callback
    def _async_on_stop(_event: Event) -> None:
        nonlocal remove_listener
        # A one-time listener has already removed itself.
        remove_listener = None
        hass.async_create_task(coordinator.async_stop_listener(), "pytap stop on shutdown")

    @callback
    def _async_remove_listener() -> None:
        nonlocal remove_listener
        if remove_listener is not None:
            remove_listener()
            remove_listener = None

    remove_listener = hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, _async_on_stop)
    entry.async_on_unload(_async_remove_listener)


async def _async_update_options(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Handle options update — reload the integration."""
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: PyTapConfigEntry) -> bool:
    """Unload a config entry.

    A listener that doesn't stop in time parks its coordinator in the stopping
    registry so the next setup waits (``ConfigEntryNotReady``) instead of
    the entry getting stuck as failed-unload until Home Assistant restarts.
    """
    coordinator = entry.runtime_data
    if not await _async_stop_or_park(hass, entry.entry_id, coordinator):
        _LOGGER.warning("PyTap listener is still stopping; setup will wait for it")
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def _async_stop_or_park(
    hass: HomeAssistant, entry_id: str, coordinator: PyTapDataUpdateCoordinator
) -> bool:
    """Stop the listener and release its optimizers, or park it if it won't stop."""
    stopped = False
    try:
        stopped = await coordinator.async_stop_listener()
    finally:
        if stopped:
            coordinator.release_barcode_claims()
        else:
            _async_park(hass, entry_id, coordinator)
    return stopped


@callback
def _async_park(hass: HomeAssistant, entry_id: str, coordinator: PyTapDataUpdateCoordinator) -> None:
    """Track a coordinator whose stop timed out until its worker really exits.

    Setup of the same entry waits for it and config flows treat its endpoint
    as in use. Once the worker exits, it finishes its own cleanup, so it
    doesn't leak when no setup comes to wait for it (e.g. a disabled entry).
    """
    parked = parked_coordinators(hass)
    parked[entry_id] = coordinator

    async def _async_finish_stop() -> None:
        if parked.get(entry_id) is coordinator and await coordinator.async_stop_listener():
            if parked.get(entry_id) is coordinator:
                del parked[entry_id]
                coordinator.release_barcode_claims()

    @callback
    def _async_listener_exited() -> None:
        hass.async_create_background_task(_async_finish_stop(), "pytap parked listener cleanup")

    coordinator.async_on_listener_exit(_async_listener_exited)


async def async_remove_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Delete this entry's private packet captures and saved state with the entry.

    A listener that is still stopping is given a bounded wait first and can no
    longer save, so it can't recreate the deleted Store.
    """
    parked = parked_coordinators(hass)
    if (coordinator := parked.get(entry.entry_id)) is not None:
        if await coordinator.async_stop_listener(discard_state=True):
            if parked.get(entry.entry_id) is coordinator:
                del parked[entry.entry_id]
        else:
            # It stays parked, so its port counts as in use until the worker exits.
            _LOGGER.warning("PyTap listener is still stopping; deleting its captures and saved state anyway")
        coordinator.release_barcode_claims()
    await async_remove_captures(hass, entry.entry_id)
    await coordinator_store(hass, entry.entry_id).async_remove()


async def async_remove_config_entry_device(
    hass: HomeAssistant, entry: PyTapConfigEntry, device: dr.DeviceEntry
) -> bool:
    """Let users delete optimizer and empty string devices from the UI.

    Deleting an optimizer excludes its barcode (so discovery doesn't re-add it)
    and reloads the entry. Every identifier that isn't one of this entry's own
    (installation or string) is a barcode, so discovered optimizers are excluded
    even while the entry isn't loaded. The installation device and strings that
    still contain modules can't be deleted.
    """
    prefix = f"{entry.entry_id}_"
    identifiers = {value for domain, value in device.identifiers if domain == DOMAIN}
    modules = list(entry.data.get(CONF_MODULES, []))
    if removed := {value for value in identifiers if not value.startswith(prefix)}:
        excluded = set(entry.data.get(CONF_EXCLUDED_BARCODES, [])) | removed
        hass.config_entries.async_update_entry(
            entry,
            data={
                **entry.data,
                CONF_MODULES: [m for m in modules if m.get(CONF_MODULE_BARCODE) not in removed],
                CONF_EXCLUDED_BARCODES: sorted(excluded),
            },
        )
        return True
    known = entry.runtime_data.modules if entry.state is ConfigEntryState.LOADED else modules
    used_strings = {f"{prefix}string_{m.get(CONF_MODULE_STRING)}" for m in known}
    return not identifiers & ({f"{prefix}installation"} | used_strings)


def _async_remove_orphaned_devices(
    hass: HomeAssistant, entry: ConfigEntry, coordinator: PyTapDataUpdateCoordinator
) -> None:
    """Remove devices (and their entities) of excluded optimizers and empty strings.

    Other unknown optimizer devices are kept: they may belong to discovered
    optimizers whose saved state was lost, which come back once rediscovered
    (users can still delete them). For the same reason, empty strings are kept
    while discovery is on but no saved state was restored.
    """
    prefix = f"{entry.entry_id}_"
    modules = coordinator.modules
    tracked = {module.get(CONF_MODULE_BARCODE, "") for module in modules}
    excluded = set(entry.data.get(CONF_EXCLUDED_BARCODES, [])) - tracked
    keep = tracked | {f"{prefix}installation"}
    keep.update(f"{prefix}string_{module.get(CONF_MODULE_STRING, '')}" for module in modules)
    strings_known = coordinator.store_restored or not coordinator.auto_discovery
    registry = dr.async_get(hass)
    for device in dr.async_entries_for_config_entry(registry, entry.entry_id):
        identifiers = {value for domain, value in device.identifiers if domain == DOMAIN}
        if not identifiers or identifiers & keep:
            continue
        barcodes = {value for value in identifiers if not value.startswith(prefix)}
        if (barcodes and barcodes <= excluded) or (
            not barcodes
            and strings_known
            and all(value.startswith(f"{prefix}string_") for value in identifiers)
        ):
            _LOGGER.info("Removing PyTap device %s: no longer configured", device.name)
            registry.async_remove_device(device.id)


async def async_migrate_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Migrate a config entry to the current version.

    v1 → v2: voltage/current sensors split into _in/_out variants.
    v2 → v3: module string labels are mandatory.
    v3 → v4: module peak power is added with default value.
    v4 → v5: live_reporting toggle added (default True).
    v5 → v6: existing connections explicitly marked as TCP.
    v6 → v7: preserve manual selection on existing entries.
    """
    if entry.version > CONFIG_ENTRY_VERSION:
        return False
    if entry.version == 1:
        _LOGGER.info(
            "Migrating PyTap config entry %s from version 1 to 2",
            entry.entry_id,
        )
        hass.config_entries.async_update_entry(entry, version=2)

    if entry.version == 2:
        _LOGGER.info(
            "Migrating PyTap config entry %s from version 2 to 3",
            entry.entry_id,
        )
        modules = list(entry.data.get(CONF_MODULES, []))
        updated_modules: list[dict[str, str]] = []
        updated = False

        for module in modules:
            normalized = dict(module)
            if not normalized.get(CONF_MODULE_STRING):
                normalized[CONF_MODULE_STRING] = DEFAULT_STRING_NAME
                updated = True
            updated_modules.append(normalized)

        if updated:
            new_data = {**entry.data, CONF_MODULES: updated_modules}
            hass.config_entries.async_update_entry(entry, data=new_data, version=3)
        else:
            hass.config_entries.async_update_entry(entry, version=3)

    if entry.version == 3:
        _LOGGER.info(
            "Migrating PyTap config entry %s from version 3 to 4",
            entry.entry_id,
        )
        modules = list(entry.data.get(CONF_MODULES, []))
        updated_modules: list[dict[str, str | int]] = []

        for module in modules:
            normalized = dict(module)
            if CONF_MODULE_PEAK_POWER not in normalized:
                normalized[CONF_MODULE_PEAK_POWER] = DEFAULT_PEAK_POWER
            updated_modules.append(normalized)

        new_data = {**entry.data, CONF_MODULES: updated_modules}
        hass.config_entries.async_update_entry(entry, data=new_data, version=4)

    if entry.version == 4:
        _LOGGER.info(
            "Migrating PyTap config entry %s from version 4 to 5",
            entry.entry_id,
        )
        new_data = {**entry.data}
        if CONF_LIVE_REPORTING not in new_data:
            new_data[CONF_LIVE_REPORTING] = DEFAULT_LIVE_REPORTING
        # The v5 reporting form constrains write_interval to
        # [MIN_WRITE_INTERVAL, MAX_WRITE_INTERVAL].  Earlier versions allowed
        # smaller values, so clamp any stale value to keep the options form
        # submittable after upgrade.
        if CONF_WRITE_INTERVAL in new_data:
            try:
                interval = int(new_data[CONF_WRITE_INTERVAL])
            except (TypeError, ValueError):
                interval = DEFAULT_WRITE_INTERVAL
            new_data[CONF_WRITE_INTERVAL] = max(
                MIN_WRITE_INTERVAL, min(MAX_WRITE_INTERVAL, interval)
            )
        hass.config_entries.async_update_entry(entry, data=new_data, version=5)

    if entry.version == 5:
        hass.config_entries.async_update_entry(
            entry,
            data={**entry.data, CONF_CONNECTION_TYPE: CONNECTION_TCP},
            version=6,
        )

    if entry.version == 6:
        hass.config_entries.async_update_entry(
            entry,
            data={
                **entry.data,
                CONF_AUTO_DISCOVERY: entry.data.get(CONF_AUTO_DISCOVERY, False),
                CONF_EXCLUDED_BARCODES: entry.data.get(CONF_EXCLUDED_BARCODES, []),
            },
            version=7,
        )

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
