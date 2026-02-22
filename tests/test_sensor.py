"""Tests for the PyTap sensor platform."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from homeassistant.components.sensor import SensorStateClass
from homeassistant.const import CONF_HOST, CONF_PORT, UnitOfEnergy
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

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


def _make_mock_config_entry(hass):
    """Create a mock config entry for testing."""
    entry = MagicMock()
    entry.data = {
        CONF_HOST: "192.168.1.100",
        CONF_PORT: DEFAULT_PORT,
        CONF_MODULES: MOCK_MODULES,
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

    # 2 modules × 10 sensors = 20 entities
    assert len(entities) == 20


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

    # Only the 2 valid modules should create entities: 2 × 10 = 20
    assert len(entities) == 20


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
