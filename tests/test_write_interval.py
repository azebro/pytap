"""Tests for the configurable write interval in PyTapDataUpdateCoordinator."""

from datetime import datetime
import time
from unittest.mock import MagicMock

import pytest

from homeassistant.const import CONF_HOST, CONF_PORT
from homeassistant.core import HomeAssistant

from custom_components.pytap.const import (
    CONF_MODULE_BARCODE,
    CONF_MODULE_NAME,
    CONF_MODULE_PEAK_POWER,
    CONF_MODULE_STRING,
    CONF_MODULES,
    CONF_WRITE_INTERVAL,
    DEFAULT_PORT,
    DEFAULT_WRITE_INTERVAL,
)
from custom_components.pytap.coordinator import PyTapDataUpdateCoordinator, _AVERAGED_FIELDS
from custom_components.pytap.pytap.core.events import InfrastructureEvent, PowerReportEvent


MOCK_MODULES = [
    {
        CONF_MODULE_STRING: "A",
        CONF_MODULE_NAME: "Panel_01",
        CONF_MODULE_BARCODE: "A-1234567B",
        CONF_MODULE_PEAK_POWER: 455,
    },
]


def _make_entry(hass, write_interval=None):
    """Create a mock ConfigEntry with optional write_interval."""
    entry = MagicMock()
    data = {
        CONF_HOST: "192.168.1.100",
        CONF_PORT: DEFAULT_PORT,
        CONF_MODULES: MOCK_MODULES,
    }
    if write_interval is not None:
        data[CONF_WRITE_INTERVAL] = write_interval
    entry.data = data
    entry.entry_id = "test_write_interval_entry"
    entry.options = {}
    return entry


def _make_infra_event():
    """Build an InfrastructureEvent that maps node 1 to barcode A-1234567B."""
    return InfrastructureEvent(
        gateways={1: {"address": "aa:bb", "version": "1.0"}},
        nodes={1: {"address": "11:22:33:44", "barcode": "A-1234567B"}},
        timestamp=datetime.now(),
    )


def _make_power_event(power=100.0):
    """Build a minimal PowerReportEvent for barcode A-1234567B."""
    voltage_in = 30.0
    current_in = round(power / voltage_in, 4)
    return PowerReportEvent(
        gateway_id=1,
        node_id=1,
        barcode="A-1234567B",
        voltage_in=voltage_in,
        voltage_out=voltage_in,
        current_in=current_in,
        temperature=25.0,
        dc_dc_duty_cycle=0.5,
        rssi=-60,
        timestamp=datetime.now(),
    )


class TestWriteIntervalInit:
    """Test that write_interval is read from entry data correctly."""

    def test_default_write_interval(self, hass: HomeAssistant) -> None:
        """Coordinator uses DEFAULT_WRITE_INTERVAL when not configured."""
        entry = _make_entry(hass)
        coordinator = PyTapDataUpdateCoordinator(hass, entry)
        assert coordinator._write_interval == DEFAULT_WRITE_INTERVAL

    def test_custom_write_interval(self, hass: HomeAssistant) -> None:
        """Coordinator honours a custom write_interval from entry data."""
        entry = _make_entry(hass, write_interval=10)
        coordinator = PyTapDataUpdateCoordinator(hass, entry)
        assert coordinator._write_interval == 10.0

    def test_throttle_state_initialised(self, hass: HomeAssistant) -> None:
        """Throttle bookkeeping fields start at their zero values."""
        entry = _make_entry(hass)
        coordinator = PyTapDataUpdateCoordinator(hass, entry)
        assert coordinator._last_ha_update == 0.0
        assert coordinator._ha_update_pending is False


class TestWriteIntervalThrottling:
    """Test that HA updates are throttled to at most once per write_interval."""

    def _run_batch(self, coordinator, event):
        """Simulate one iteration of the inner read-loop for a single event."""
        if coordinator._process_event(event):
            coordinator._ha_update_pending = True

        now = time.monotonic()
        pushed = False
        if coordinator._ha_update_pending and (
            now - coordinator._last_ha_update >= coordinator._write_interval
        ):
            pushed = True
            coordinator._last_ha_update = now
            coordinator._ha_update_pending = False
        return pushed

    def test_first_event_triggers_push(self, hass: HomeAssistant) -> None:
        """First data event always triggers an immediate HA push (last_ha_update=0)."""
        entry = _make_entry(hass, write_interval=60)
        coordinator = PyTapDataUpdateCoordinator(hass, entry)
        coordinator._schedule_save = MagicMock()
        coordinator._handle_infrastructure(_make_infra_event())

        pushed = self._run_batch(coordinator, _make_power_event())

        assert pushed is True

    def test_second_event_within_interval_suppressed(self, hass: HomeAssistant) -> None:
        """A second event arriving before the interval elapses is NOT pushed immediately."""
        entry = _make_entry(hass, write_interval=60)
        coordinator = PyTapDataUpdateCoordinator(hass, entry)
        coordinator._schedule_save = MagicMock()
        coordinator._handle_infrastructure(_make_infra_event())

        # First batch — should push (last_ha_update starts at 0)
        pushed1 = self._run_batch(coordinator, _make_power_event(power=100.0))
        assert pushed1 is True

        # Second batch immediately after — interval (60 s) has not elapsed
        pushed2 = self._run_batch(coordinator, _make_power_event(power=110.0))
        assert pushed2 is False, "Second push should be suppressed within the interval"
        assert coordinator._ha_update_pending is True, "Pending flag should remain set"

    def test_pending_flag_cleared_after_push(self, hass: HomeAssistant) -> None:
        """_ha_update_pending is cleared to False after a successful push."""
        entry = _make_entry(hass, write_interval=0)  # interval=0 → always push
        coordinator = PyTapDataUpdateCoordinator(hass, entry)
        coordinator._schedule_save = MagicMock()
        coordinator._handle_infrastructure(_make_infra_event())

        self._run_batch(coordinator, _make_power_event())

        assert coordinator._ha_update_pending is False


def _run_averaging(node: dict, readings: list[dict]) -> dict:
    """Replicate the per-interval averaging block from coordinator._listen.

    Returns an averaged copy of *node* — the same snapshot that would be
    passed to async_set_updated_data when the write interval fires.
    """
    avg_node = dict(node)
    for field in _AVERAGED_FIELDS:
        values = [r[field] for r in readings if r[field] is not None]
        avg_node[field] = round(sum(values) / len(values), 3) if values else None
    if avg_node["power"] is not None:
        avg_node["performance"] = round(
            (max(avg_node["power"], 0.0) / avg_node["peak_power"]) * 100.0, 2
        )
    else:
        avg_node["performance"] = None
    return avg_node


def _numeric_reading(**overrides) -> dict:
    """Build a minimal numeric-fields dict as stored in _reading_buffers."""
    defaults = {
        "voltage_in": 30.0,
        "voltage_out": 30.0,
        "current_in": 3.333,
        "current_out": 3.333,
        "power": 100.0,
        "temperature": 25.0,
        "dc_dc_duty_cycle": 0.5,
        "rssi": -60,
    }
    return {**defaults, **overrides}


class TestAveragingMath:
    """Pure unit tests for the per-interval averaging computation.

    These tests exercise _run_averaging directly — no coordinator or hass
    fixture needed, so they cannot hang on HA event-loop setup.
    """

    PEAK_POWER = 455

    def _node(self, **fields) -> dict:
        return {"peak_power": self.PEAK_POWER, **_numeric_reading(**fields)}

    def test_single_reading_passthrough(self) -> None:
        """A single buffered reading is returned unchanged."""
        node = self._node(power=100.0)
        result = _run_averaging(node, [_numeric_reading(power=100.0)])
        assert result["power"] == pytest.approx(100.0, rel=1e-3)

    def test_two_readings_averaged(self) -> None:
        """Two readings with different power values produce the correct mean."""
        node = self._node(power=200.0)
        readings = [_numeric_reading(power=100.0), _numeric_reading(power=200.0)]
        result = _run_averaging(node, readings)
        assert result["power"] == pytest.approx(150.0, rel=1e-3)

    def test_performance_recomputed_from_averaged_power(self) -> None:
        """Performance is derived from the averaged power, not the last raw reading."""
        node = self._node(power=self.PEAK_POWER)
        readings = [
            _numeric_reading(power=0.0),
            _numeric_reading(power=self.PEAK_POWER),
        ]
        result = _run_averaging(node, readings)
        expected = round((self.PEAK_POWER / 2 / self.PEAK_POWER) * 100.0, 2)
        assert result["performance"] == pytest.approx(expected, rel=1e-3)

    def test_none_values_excluded_from_average(self) -> None:
        """None entries for a field are ignored; the mean is over valid readings only."""
        node = self._node(power=100.0)
        readings = [
            _numeric_reading(power=100.0),
            {field: None for field in _AVERAGED_FIELDS},
        ]
        result = _run_averaging(node, readings)
        assert result["power"] == pytest.approx(100.0, rel=1e-3)

    def test_all_none_values_produce_none(self) -> None:
        """If every reading for a field is None the averaged field is also None."""
        node = {"peak_power": self.PEAK_POWER, **{f: None for f in _AVERAGED_FIELDS}}
        result = _run_averaging(node, [{f: None for f in _AVERAGED_FIELDS}])
        assert result["power"] is None
        assert result["performance"] is None


class TestBufferPopulation:
    """Test that _handle_power_report populates _reading_buffers correctly.

    These tests verify the wiring between event processing and the buffer,
    without exercising the write-interval flush path.
    """

    def test_buffer_populated_on_power_reports(self, hass: HomeAssistant) -> None:
        """Each processed power report appends one entry to the barcode's buffer."""
        entry = _make_entry(hass)
        coordinator = PyTapDataUpdateCoordinator(hass, entry)
        coordinator._schedule_save = MagicMock()
        coordinator._handle_infrastructure(_make_infra_event())

        coordinator._process_event(_make_power_event(power=100.0))
        coordinator._process_event(_make_power_event(power=200.0))

        assert "A-1234567B" in coordinator._reading_buffers
        assert len(coordinator._reading_buffers["A-1234567B"]) == 2

    def test_buffer_entry_contains_all_averaged_fields(self, hass: HomeAssistant) -> None:
        """Each buffer entry has exactly the fields listed in _AVERAGED_FIELDS."""
        entry = _make_entry(hass)
        coordinator = PyTapDataUpdateCoordinator(hass, entry)
        coordinator._schedule_save = MagicMock()
        coordinator._handle_infrastructure(_make_infra_event())

        coordinator._process_event(_make_power_event(power=100.0))

        reading = coordinator._reading_buffers["A-1234567B"][0]
        assert set(reading.keys()) == set(_AVERAGED_FIELDS)

    def test_data_nodes_retains_latest_raw_value(self, hass: HomeAssistant) -> None:
        """self.data['nodes'] always reflects the most-recent raw reading."""
        entry = _make_entry(hass)
        coordinator = PyTapDataUpdateCoordinator(hass, entry)
        coordinator._schedule_save = MagicMock()
        coordinator._handle_infrastructure(_make_infra_event())

        coordinator._process_event(_make_power_event(power=100.0))
        coordinator._process_event(_make_power_event(power=200.0))

        # power ≈ 200 (computed from current_out * voltage_out inside the event)
        assert coordinator.data["nodes"]["A-1234567B"]["power"] == pytest.approx(
            200.0, rel=0.01
        )

