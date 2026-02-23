"""Tests for PyTap diagnostics endpoint."""

from datetime import datetime
from unittest.mock import MagicMock

from homeassistant.const import CONF_HOST, CONF_PORT
from homeassistant.core import HomeAssistant

from custom_components.pytap.const import (
    CONF_MODULES,
    DEFAULT_PORT,
    DOMAIN,
)
from custom_components.pytap.coordinator import PyTapDataUpdateCoordinator
from custom_components.pytap.diagnostics import async_get_config_entry_diagnostics
from custom_components.pytap.energy import EnergyAccumulator


def _make_entry() -> MagicMock:
    """Create a mock config entry for diagnostics tests."""
    entry = MagicMock()
    entry.entry_id = "diag_entry"
    entry.data = {
        CONF_HOST: "192.168.1.100",
        CONF_PORT: DEFAULT_PORT,
        CONF_MODULES: [],
    }
    entry.options = {}
    entry.as_dict = MagicMock(
        return_value={
            "data": entry.data,
            "entry_id": entry.entry_id,
            "options": entry.options,
            "domain": DOMAIN,
        }
    )
    return entry


async def test_config_entry_diagnostics_redacts_host(hass: HomeAssistant) -> None:
    """Diagnostics should redact host while preserving troubleshooting fields."""
    entry = _make_entry()
    coordinator = PyTapDataUpdateCoordinator(hass, entry)

    coordinator.data["counters"] = {
        "frames_received": 100,
        "crc_errors": 2,
        "noise_bytes": 11,
        "runts": 0,
        "giants": 0,
    }
    coordinator.data["gateways"] = {1: {"address": "aa:bb", "version": "1.0.0"}}
    coordinator.data["discovered_barcodes"] = ["X-9999999Z"]
    coordinator.data["nodes"] = {
        "A-1234567B": {
            "gateway_id": 1,
            "node_id": 10,
            "last_update": "2026-02-23T10:00:00",
            "daily_energy_wh": 12.5,
            "total_energy_wh": 100.0,
            "readings_today": 4,
        }
    }
    coordinator._barcode_to_node = {"A-1234567B": 10}
    coordinator._node_to_barcode = {10: "A-1234567B"}
    coordinator._infra_received = True
    coordinator._pending_power_reports = 0
    coordinator._energy_state = {
        "A-1234567B": EnergyAccumulator(
            daily_energy_wh=12.5,
            total_energy_wh=100.0,
            daily_reset_date="2026-02-23",
            last_power_w=200.0,
            last_reading_ts=datetime(2026, 2, 23, 10, 0, 0),
            readings_today=4,
        )
    }

    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN][entry.entry_id] = coordinator

    diagnostics = await async_get_config_entry_diagnostics(hass, entry)

    assert diagnostics["config_entry"]["data"][CONF_HOST] == "**REDACTED**"
    assert diagnostics["connection_state"]["host"] == "**REDACTED**"
    assert diagnostics["counters"]["frames_received"] == 100
    assert diagnostics["node_mappings"]["barcode_to_node"] == {"A-1234567B": 10}
    assert diagnostics["connection_state"]["infra_received"] is True
    assert diagnostics["nodes"]["A-1234567B"]["readings_today"] == 4


async def test_config_entry_diagnostics_includes_unredacted_barcodes(
    hass: HomeAssistant,
) -> None:
    """Barcodes should remain visible for module-level troubleshooting."""
    entry = _make_entry()
    coordinator = PyTapDataUpdateCoordinator(hass, entry)
    coordinator._barcode_to_node = {"A-1234567B": 10}

    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN][entry.entry_id] = coordinator

    diagnostics = await async_get_config_entry_diagnostics(hass, entry)

    assert "A-1234567B" in diagnostics["node_mappings"]["barcode_to_node"]


async def test_config_entry_diagnostics_fresh_install(hass: HomeAssistant) -> None:
    """Diagnostics should not raise on a fresh coordinator with no data."""
    entry = _make_entry()
    coordinator = PyTapDataUpdateCoordinator(hass, entry)

    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN][entry.entry_id] = coordinator

    diagnostics = await async_get_config_entry_diagnostics(hass, entry)

    assert diagnostics["counters"] == {}
    assert diagnostics["gateways"] == {}
    assert diagnostics["nodes"] == {}
    assert diagnostics["discovered_barcodes"] == []
    assert diagnostics["node_mappings"]["barcode_to_node"] == {}
    assert diagnostics["connection_state"]["infra_received"] is False
    assert diagnostics["connection_state"]["pending_power_reports"] == 0
    assert diagnostics["energy_state"] == {}


async def test_config_entry_diagnostics_all_keys_present(
    hass: HomeAssistant,
) -> None:
    """Diagnostics output should contain all expected top-level keys."""
    entry = _make_entry()
    coordinator = PyTapDataUpdateCoordinator(hass, entry)

    coordinator.data["counters"] = {"frames_received": 5}
    coordinator.data["gateways"] = {1: {"address": "aa:bb"}}
    coordinator.data["discovered_barcodes"] = ["Z-0000000A"]
    coordinator._energy_state = {
        "A-1234567B": EnergyAccumulator(
            daily_energy_wh=1.0,
            total_energy_wh=50.0,
            daily_reset_date="2026-02-23",
            readings_today=2,
        )
    }

    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN][entry.entry_id] = coordinator

    diagnostics = await async_get_config_entry_diagnostics(hass, entry)

    expected_keys = {
        "config_entry",
        "counters",
        "gateways",
        "discovered_barcodes",
        "nodes",
        "node_mappings",
        "connection_state",
        "energy_state",
    }
    assert expected_keys.issubset(diagnostics.keys())

    # Port should not be redacted
    assert diagnostics["connection_state"]["port"] == DEFAULT_PORT

    # Energy state should contain per-barcode data
    assert "A-1234567B" in diagnostics["energy_state"]
    assert diagnostics["energy_state"]["A-1234567B"]["readings_today"] == 2

    # Discovered barcodes pass through
    assert diagnostics["discovered_barcodes"] == ["Z-0000000A"]
