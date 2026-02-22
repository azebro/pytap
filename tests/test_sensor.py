"""Tests for the PyTap sensor platform."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from homeassistant.components.sensor import SensorStateClass
from homeassistant.const import CONF_HOST, CONF_PORT, UnitOfEnergy
from homeassistant.core import HomeAssistant

from custom_components.pytap.const import (
    CONF_MODULE_BARCODE,
    CONF_MODULE_NAME,
    CONF_MODULE_STRING,
    CONF_MODULES,
    DEFAULT_PORT,
    DOMAIN,
)
from custom_components.pytap.coordinator import PyTapDataUpdateCoordinator
from custom_components.pytap.sensor import SENSOR_DESCRIPTIONS, async_setup_entry


MOCK_MODULES = [
    {
        CONF_MODULE_STRING: "A",
        CONF_MODULE_NAME: "Panel_01",
        CONF_MODULE_BARCODE: "A-1234567B",
    },
    {
        CONF_MODULE_STRING: "B",
        CONF_MODULE_NAME: "Panel_02",
        CONF_MODULE_BARCODE: "C-2345678D",
    },
]

MOCK_NODE_DATA = {
    "A-1234567B": {
        "gateway_id": 1,
        "node_id": 10,
        "barcode": "A-1234567B",
        "name": "Panel_01",
        "string": "A",
        "voltage_in": 35.2,
        "voltage_out": 34.8,
        "current_in": 8.5,
        "current_out": 8.5977,
        "power": 299.2,
        "temperature": 42.0,
        "dc_dc_duty_cycle": 0.95,
        "rssi": -65,
        "daily_energy_wh": 123.45,
        "total_energy_wh": 4567.89,
        "daily_reset_date": "2025-01-01",
        "last_update": "2025-01-01T12:00:00",
    },
}

MOCK_NODE_DATA_TWO = {
    "A-1234567B": {
        **MOCK_NODE_DATA["A-1234567B"],
        "power": 300.0,
        "daily_energy_wh": 120.0,
        "total_energy_wh": 4500.0,
    },
    "C-2345678D": {
        "gateway_id": 1,
        "node_id": 11,
        "barcode": "C-2345678D",
        "name": "Panel_02",
        "string": "B",
        "voltage_in": 34.9,
        "voltage_out": 34.6,
        "current_in": 7.8,
        "current_out": 7.6,
        "power": 250.0,
        "temperature": 40.0,
        "dc_dc_duty_cycle": 0.9,
        "rssi": -66,
        "daily_energy_wh": 100.0,
        "total_energy_wh": 4100.0,
        "daily_reset_date": "2025-01-01",
        "last_update": "2025-01-01T12:00:30",
    },
}


def _make_mock_config_entry(hass, modules=None):
    """Create a mock config entry for testing."""
    entry = MagicMock()
    entry.data = {
        CONF_HOST: "192.168.1.100",
        CONF_PORT: DEFAULT_PORT,
        CONF_MODULES: MOCK_MODULES if modules is None else modules,
    }
    entry.entry_id = "test_entry_id"
    entry.options = {}
    return entry


def _make_mock_coordinator(hass, entry, node_data=None):
    """Create a mock coordinator with test data."""
    coordinator = MagicMock(spec=PyTapDataUpdateCoordinator)
    coordinator.data = {
        "gateways": {},
        "nodes": node_data or {},
        "counters": {},
        "discovered_barcodes": [],
    }
    coordinator.hass = hass
    coordinator.config_entry = entry
    coordinator.last_update_success = True
    coordinator.async_request_refresh = AsyncMock()
    return coordinator


async def test_sensor_entities_created(hass: HomeAssistant) -> None:
    """Test that sensor entities are created for each module/sensor combination."""
    entry = _make_mock_config_entry(hass)
    coordinator = _make_mock_coordinator(hass, entry)

    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN][entry.entry_id] = coordinator

    entities = []

    def capture_entities(new_entities):
        entities.extend(new_entities)

    await async_setup_entry(hass, entry, capture_entities)

    # 2 modules × 10 + 2 strings × 3 + installation 3 = 29 entities
    assert len(entities) == 29


async def test_sensor_unique_ids(hass: HomeAssistant) -> None:
    """Test that sensor unique IDs follow the expected pattern."""
    entry = _make_mock_config_entry(hass)
    coordinator = _make_mock_coordinator(hass, entry)

    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN][entry.entry_id] = coordinator

    entities = []
    await async_setup_entry(hass, entry, lambda e: entities.extend(e))

    unique_ids = {e.unique_id for e in entities}
    # Check a few expected unique IDs
    assert f"{DOMAIN}_A-1234567B_power" in unique_ids
    assert f"{DOMAIN}_C-2345678D_rssi" in unique_ids
    assert f"{DOMAIN}_A-1234567B_temperature" in unique_ids
    assert f"{DOMAIN}_{entry.entry_id}_string_A_power" in unique_ids
    assert f"{DOMAIN}_{entry.entry_id}_installation_total_energy" in unique_ids


async def test_sensor_available_with_data(hass: HomeAssistant) -> None:
    """Test that sensors report available when node data is present."""
    entry = _make_mock_config_entry(hass)
    coordinator = _make_mock_coordinator(hass, entry, node_data=MOCK_NODE_DATA)

    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN][entry.entry_id] = coordinator

    entities = []
    await async_setup_entry(hass, entry, lambda e: entities.extend(e))

    # Find the power sensor for S-1234567A (has data)
    power_sensor = next(
        e for e in entities if e.unique_id == f"{DOMAIN}_A-1234567B_power"
    )
    assert power_sensor.available is True

    # Find a sensor for C-2345678D (no data yet)
    power_sensor_b = next(
        e for e in entities if e.unique_id == f"{DOMAIN}_C-2345678D_power"
    )
    assert power_sensor_b.available is False


async def test_sensor_unavailable_without_data(hass: HomeAssistant) -> None:
    """Test that sensors report unavailable when no node data exists."""
    entry = _make_mock_config_entry(hass)
    coordinator = _make_mock_coordinator(hass, entry, node_data={})

    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN][entry.entry_id] = coordinator

    entities = []
    await async_setup_entry(hass, entry, lambda e: entities.extend(e))

    for entity in entities:
        assert entity.available is False


async def test_sensor_skips_modules_without_barcode(hass: HomeAssistant) -> None:
    """Test that modules without a barcode are skipped."""
    entry = _make_mock_config_entry(hass)
    # Add a module without a barcode
    entry.data = {
        **entry.data,
        CONF_MODULES: [
            *MOCK_MODULES,
            {
                CONF_MODULE_STRING: "C",
                CONF_MODULE_NAME: "Unknown",
                CONF_MODULE_BARCODE: "",
            },
        ],
    }
    coordinator = _make_mock_coordinator(hass, entry)

    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN][entry.entry_id] = coordinator

    entities = []
    await async_setup_entry(hass, entry, lambda e: entities.extend(e))

    # Only the 2 valid modules should contribute to entities: 2*10 + 2*3 + 3 = 29
    assert len(entities) == 29


async def test_sensor_device_info(hass: HomeAssistant) -> None:
    """Test that sensor entities have correct device info."""
    entry = _make_mock_config_entry(hass)
    coordinator = _make_mock_coordinator(hass, entry)

    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN][entry.entry_id] = coordinator

    entities = []
    await async_setup_entry(hass, entry, lambda e: entities.extend(e))

    power_sensor = next(
        e for e in entities if e.unique_id == f"{DOMAIN}_A-1234567B_power"
    )
    device_info = power_sensor.device_info
    assert (DOMAIN, "A-1234567B") in device_info["identifiers"]
    assert device_info["manufacturer"] == "Tigo Energy"
    assert device_info["model"] == "TS4"
    assert device_info["serial_number"] == "A-1234567B"


async def test_sensor_descriptions_count() -> None:
    """Test that we have the expected number of sensor descriptions."""
    assert len(SENSOR_DESCRIPTIONS) == 10


async def test_energy_sensor_descriptions() -> None:
    """Test energy sensor description metadata."""
    description_map = {
        description.key: description for description in SENSOR_DESCRIPTIONS
    }

    daily_energy = description_map["daily_energy"]
    assert daily_energy.native_unit_of_measurement == UnitOfEnergy.WATT_HOUR
    assert daily_energy.state_class == SensorStateClass.TOTAL

    total_energy = description_map["total_energy"]
    assert total_energy.native_unit_of_measurement == UnitOfEnergy.WATT_HOUR
    assert total_energy.state_class == SensorStateClass.TOTAL_INCREASING


async def test_daily_energy_last_reset(hass: HomeAssistant) -> None:
    """Test that daily energy exposes a last_reset timestamp."""
    entry = _make_mock_config_entry(hass)
    coordinator = _make_mock_coordinator(hass, entry, node_data=MOCK_NODE_DATA)

    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN][entry.entry_id] = coordinator

    entities = []
    await async_setup_entry(hass, entry, lambda e: entities.extend(e))

    daily_energy_sensor = next(
        e for e in entities if e.unique_id == f"{DOMAIN}_A-1234567B_daily_energy"
    )
    assert daily_energy_sensor.last_reset is not None
    assert daily_energy_sensor.last_reset.isoformat().startswith("2025-01-01T00:00:00")


async def test_aggregate_entity_count_single_string(hass: HomeAssistant) -> None:
    """Two modules on one string produce 26 entities."""
    modules = [
        {
            CONF_MODULE_STRING: "A",
            CONF_MODULE_NAME: "Panel_01",
            CONF_MODULE_BARCODE: "A-1234567B",
        },
        {
            CONF_MODULE_STRING: "A",
            CONF_MODULE_NAME: "Panel_02",
            CONF_MODULE_BARCODE: "C-2345678D",
        },
    ]
    entry = _make_mock_config_entry(hass, modules=modules)
    coordinator = _make_mock_coordinator(hass, entry)

    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN][entry.entry_id] = coordinator

    entities = []
    await async_setup_entry(hass, entry, lambda e: entities.extend(e))

    assert len(entities) == 26


async def test_string_power_sums_constituents(hass: HomeAssistant) -> None:
    """String aggregate power should sum all members of the string."""
    modules = [
        {
            CONF_MODULE_STRING: "A",
            CONF_MODULE_NAME: "Panel_01",
            CONF_MODULE_BARCODE: "A-1234567B",
        },
        {
            CONF_MODULE_STRING: "A",
            CONF_MODULE_NAME: "Panel_02",
            CONF_MODULE_BARCODE: "C-2345678D",
        },
    ]
    entry = _make_mock_config_entry(hass, modules=modules)
    coordinator = _make_mock_coordinator(hass, entry, node_data=MOCK_NODE_DATA_TWO)

    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN][entry.entry_id] = coordinator

    entities = []
    await async_setup_entry(hass, entry, lambda e: entities.extend(e))

    sensor = next(
        e
        for e in entities
        if e.unique_id == f"{DOMAIN}_{entry.entry_id}_string_A_power"
    )
    sensor.async_write_ha_state = lambda: None
    sensor._handle_coordinator_update()
    assert sensor.native_value == 550.0


async def test_installation_power_sums_all(hass: HomeAssistant) -> None:
    """Installation aggregate power should sum all configured modules."""
    entry = _make_mock_config_entry(hass)
    coordinator = _make_mock_coordinator(hass, entry, node_data=MOCK_NODE_DATA_TWO)

    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN][entry.entry_id] = coordinator

    entities = []
    await async_setup_entry(hass, entry, lambda e: entities.extend(e))

    sensor = next(
        e
        for e in entities
        if e.unique_id == f"{DOMAIN}_{entry.entry_id}_installation_power"
    )
    sensor.async_write_ha_state = lambda: None
    sensor._handle_coordinator_update()
    assert sensor.native_value == 550.0


async def test_string_daily_energy_sums(hass: HomeAssistant) -> None:
    """String aggregate daily energy should sum all members of the string."""
    modules = [
        {
            CONF_MODULE_STRING: "A",
            CONF_MODULE_NAME: "Panel_01",
            CONF_MODULE_BARCODE: "A-1234567B",
        },
        {
            CONF_MODULE_STRING: "A",
            CONF_MODULE_NAME: "Panel_02",
            CONF_MODULE_BARCODE: "C-2345678D",
        },
    ]
    entry = _make_mock_config_entry(hass, modules=modules)
    coordinator = _make_mock_coordinator(hass, entry, node_data=MOCK_NODE_DATA_TWO)

    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN][entry.entry_id] = coordinator

    entities = []
    await async_setup_entry(hass, entry, lambda e: entities.extend(e))

    sensor = next(
        e
        for e in entities
        if e.unique_id == f"{DOMAIN}_{entry.entry_id}_string_A_daily_energy"
    )
    sensor.async_write_ha_state = lambda: None
    sensor._handle_coordinator_update()
    assert sensor.native_value == 220.0


async def test_string_total_energy_sums(hass: HomeAssistant) -> None:
    """String aggregate total energy should sum all members of the string."""
    modules = [
        {
            CONF_MODULE_STRING: "A",
            CONF_MODULE_NAME: "Panel_01",
            CONF_MODULE_BARCODE: "A-1234567B",
        },
        {
            CONF_MODULE_STRING: "A",
            CONF_MODULE_NAME: "Panel_02",
            CONF_MODULE_BARCODE: "C-2345678D",
        },
    ]
    entry = _make_mock_config_entry(hass, modules=modules)
    coordinator = _make_mock_coordinator(hass, entry, node_data=MOCK_NODE_DATA_TWO)

    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN][entry.entry_id] = coordinator

    entities = []
    await async_setup_entry(hass, entry, lambda e: entities.extend(e))

    sensor = next(
        e
        for e in entities
        if e.unique_id == f"{DOMAIN}_{entry.entry_id}_string_A_total_energy"
    )
    sensor.async_write_ha_state = lambda: None
    sensor._handle_coordinator_update()
    assert sensor.native_value == 8600.0


async def test_installation_daily_energy_sums_all(hass: HomeAssistant) -> None:
    """Installation daily energy should sum all configured modules."""
    entry = _make_mock_config_entry(hass)
    coordinator = _make_mock_coordinator(hass, entry, node_data=MOCK_NODE_DATA_TWO)

    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN][entry.entry_id] = coordinator

    entities = []
    await async_setup_entry(hass, entry, lambda e: entities.extend(e))

    sensor = next(
        e
        for e in entities
        if e.unique_id == f"{DOMAIN}_{entry.entry_id}_installation_daily_energy"
    )
    sensor.async_write_ha_state = lambda: None
    sensor._handle_coordinator_update()
    assert sensor.native_value == 220.0


async def test_installation_total_energy_sums_all(hass: HomeAssistant) -> None:
    """Installation total energy should sum all configured modules."""
    entry = _make_mock_config_entry(hass)
    coordinator = _make_mock_coordinator(hass, entry, node_data=MOCK_NODE_DATA_TWO)

    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN][entry.entry_id] = coordinator

    entities = []
    await async_setup_entry(hass, entry, lambda e: entities.extend(e))

    sensor = next(
        e
        for e in entities
        if e.unique_id == f"{DOMAIN}_{entry.entry_id}_installation_total_energy"
    )
    sensor.async_write_ha_state = lambda: None
    sensor._handle_coordinator_update()
    assert sensor.native_value == 8600.0


async def test_aggregate_available_partial_data(hass: HomeAssistant) -> None:
    """Aggregate should be available if at least one source node reports."""
    entry = _make_mock_config_entry(hass)
    coordinator = _make_mock_coordinator(hass, entry, node_data=MOCK_NODE_DATA)

    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN][entry.entry_id] = coordinator

    entities = []
    await async_setup_entry(hass, entry, lambda e: entities.extend(e))

    installation = next(
        e
        for e in entities
        if e.unique_id == f"{DOMAIN}_{entry.entry_id}_installation_power"
    )
    assert installation.available is True


async def test_aggregate_excludes_none_values(hass: HomeAssistant) -> None:
    """Aggregate sum should ignore nodes where value_key is None."""
    entry = _make_mock_config_entry(hass)
    node_data = {
        **MOCK_NODE_DATA_TWO,
        "C-2345678D": {**MOCK_NODE_DATA_TWO["C-2345678D"], "power": None},
    }
    coordinator = _make_mock_coordinator(hass, entry, node_data=node_data)

    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN][entry.entry_id] = coordinator

    entities = []
    await async_setup_entry(hass, entry, lambda e: entities.extend(e))

    installation = next(
        e
        for e in entities
        if e.unique_id == f"{DOMAIN}_{entry.entry_id}_installation_power"
    )
    installation.async_write_ha_state = lambda: None
    installation._handle_coordinator_update()
    assert installation.native_value == 300.0


async def test_aggregate_extra_attributes(hass: HomeAssistant) -> None:
    """Aggregate extra attrs should expose optimizer and reporting counts."""
    entry = _make_mock_config_entry(hass)
    coordinator = _make_mock_coordinator(hass, entry, node_data=MOCK_NODE_DATA)

    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN][entry.entry_id] = coordinator

    entities = []
    await async_setup_entry(hass, entry, lambda e: entities.extend(e))

    installation = next(
        e
        for e in entities
        if e.unique_id == f"{DOMAIN}_{entry.entry_id}_installation_power"
    )
    assert installation.extra_state_attributes == {
        "optimizer_count": 2,
        "reporting_count": 1,
    }


async def test_string_aggregate_device_info(hass: HomeAssistant) -> None:
    """String aggregate should be attached to a virtual string device."""
    entry = _make_mock_config_entry(hass)
    coordinator = _make_mock_coordinator(hass, entry, node_data=MOCK_NODE_DATA_TWO)

    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN][entry.entry_id] = coordinator

    entities = []
    await async_setup_entry(hass, entry, lambda e: entities.extend(e))

    sensor = next(
        e
        for e in entities
        if e.unique_id == f"{DOMAIN}_{entry.entry_id}_string_A_power"
    )
    device_info = sensor.device_info
    assert (DOMAIN, f"{entry.entry_id}_string_A") in device_info["identifiers"]
    assert device_info["name"] == "Tigo String A"
    assert device_info["manufacturer"] == "Tigo Energy"
    assert device_info["model"] == "String Aggregate"


async def test_installation_aggregate_device_info(hass: HomeAssistant) -> None:
    """Installation aggregate should be attached to installation virtual device."""
    entry = _make_mock_config_entry(hass)
    coordinator = _make_mock_coordinator(hass, entry, node_data=MOCK_NODE_DATA_TWO)

    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN][entry.entry_id] = coordinator

    entities = []
    await async_setup_entry(hass, entry, lambda e: entities.extend(e))

    sensor = next(
        e
        for e in entities
        if e.unique_id == f"{DOMAIN}_{entry.entry_id}_installation_power"
    )
    device_info = sensor.device_info
    assert (DOMAIN, f"{entry.entry_id}_installation") in device_info["identifiers"]
    assert device_info["name"] == "Tigo Installation"
    assert device_info["manufacturer"] == "Tigo Energy"
    assert device_info["model"] == "Installation Aggregate"


async def test_aggregate_daily_energy_last_reset(hass: HomeAssistant) -> None:
    """Aggregate daily energy should have a local-midnight last_reset."""
    entry = _make_mock_config_entry(hass)
    coordinator = _make_mock_coordinator(hass, entry, node_data=MOCK_NODE_DATA_TWO)

    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN][entry.entry_id] = coordinator

    entities = []
    await async_setup_entry(hass, entry, lambda e: entities.extend(e))

    sensor = next(
        e
        for e in entities
        if e.unique_id == f"{DOMAIN}_{entry.entry_id}_installation_daily_energy"
    )
    assert sensor.last_reset is not None
    assert sensor.last_reset.hour == 0
    assert sensor.last_reset.minute == 0
    assert sensor.last_reset.second == 0


async def test_aggregate_total_energy_no_last_reset(hass: HomeAssistant) -> None:
    """Aggregate total energy should not expose last_reset."""
    entry = _make_mock_config_entry(hass)
    coordinator = _make_mock_coordinator(hass, entry, node_data=MOCK_NODE_DATA_TWO)

    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN][entry.entry_id] = coordinator

    entities = []
    await async_setup_entry(hass, entry, lambda e: entities.extend(e))

    sensor = next(
        e
        for e in entities
        if e.unique_id == f"{DOMAIN}_{entry.entry_id}_installation_total_energy"
    )
    assert sensor.last_reset is None


async def test_no_string_aggregates_when_no_modules(hass: HomeAssistant) -> None:
    """No entities should be created when there are no configured modules."""
    entry = _make_mock_config_entry(hass, modules=[])
    coordinator = _make_mock_coordinator(hass, entry)

    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN][entry.entry_id] = coordinator

    entities = []
    await async_setup_entry(hass, entry, lambda e: entities.extend(e))

    assert entities == []
