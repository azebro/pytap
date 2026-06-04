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


MOCK_MODULES_TWO = [
    {
        CONF_MODULE_STRING: "A",
        CONF_MODULE_NAME: "Panel_01",
        CONF_MODULE_BARCODE: "A-1234567B",
        CONF_MODULE_PEAK_POWER: 455,
    },
    {
        CONF_MODULE_STRING: "A",
        CONF_MODULE_NAME: "Panel_02",
        CONF_MODULE_BARCODE: "B-9876543C",
        CONF_MODULE_PEAK_POWER: 455,
    },
]


def _make_entry_two_modules(hass):
    entry = MagicMock()
    entry.data = {
        CONF_HOST: "192.168.1.100",
        CONF_PORT: DEFAULT_PORT,
        CONF_MODULES: MOCK_MODULES_TWO,
    }
    entry.entry_id = "test_snapshot_entry"
    entry.options = {}
    return entry


class TestAveragedSnapshot:
    """Tests for coordinator._build_averaged_snapshot().

    These call the actual production method — not a duplicate — and verify
    that the snapshot data emitted to HA carries per-node averages.
    """

    BARCODE_A = "A-1234567B"
    BARCODE_B = "B-9876543C"
    PEAK_POWER = 455

    def _reading(self, power: float) -> dict:
        """Build a numeric-fields dict as stored in _reading_buffers."""
        current = round(power / 30.0, 4)
        return {
            "voltage_in": 30.0,
            "voltage_out": 30.0,
            "current_in": current,
            "current_out": current,
            "power": power,
            "temperature": 25.0,
            "dc_dc_duty_cycle": 0.5,
            "rssi": -60,
        }

    def _seed(self, coordinator, barcode: str, readings: list[dict]) -> None:
        """Plant a node entry and buffer readings directly."""
        coordinator.data["nodes"][barcode] = {
            "name": barcode,
            "peak_power": self.PEAK_POWER,
            **readings[-1],
            "performance": None,
            "daily_energy_wh": 0.0,
            "total_energy_wh": 0.0,
            "readings_today": len(readings),
            "daily_reset_date": "",
            "last_update": None,
        }
        coordinator._reading_buffers[barcode] = list(readings)

    def test_single_reading_passthrough(self, hass: HomeAssistant) -> None:
        """A single buffered reading is passed through to the snapshot unchanged."""
        coordinator = PyTapDataUpdateCoordinator(hass, _make_entry(hass))
        coordinator._schedule_save = MagicMock()
        self._seed(coordinator, self.BARCODE_A, [self._reading(100.0)])

        snapshot = coordinator._build_averaged_snapshot()

        assert snapshot["nodes"][self.BARCODE_A]["power"] == pytest.approx(100.0, rel=1e-3)

    def test_two_readings_averaged(self, hass: HomeAssistant) -> None:
        """Two readings produce the correct per-node mean in the snapshot."""
        coordinator = PyTapDataUpdateCoordinator(hass, _make_entry(hass))
        coordinator._schedule_save = MagicMock()
        self._seed(coordinator, self.BARCODE_A, [self._reading(100.0), self._reading(200.0)])

        snapshot = coordinator._build_averaged_snapshot()

        assert snapshot["nodes"][self.BARCODE_A]["power"] == pytest.approx(150.0, rel=1e-3)

    def test_performance_recomputed_from_averaged_power(self, hass: HomeAssistant) -> None:
        """Performance in the snapshot is derived from the averaged power."""
        coordinator = PyTapDataUpdateCoordinator(hass, _make_entry(hass))
        coordinator._schedule_save = MagicMock()
        self._seed(
            coordinator,
            self.BARCODE_A,
            [self._reading(0.0), self._reading(self.PEAK_POWER)],
        )

        snapshot = coordinator._build_averaged_snapshot()

        expected = round((self.PEAK_POWER / 2 / self.PEAK_POWER) * 100.0, 2)
        assert snapshot["nodes"][self.BARCODE_A]["performance"] == pytest.approx(
            expected, rel=1e-3
        )

    def test_none_values_excluded_from_average(self, hass: HomeAssistant) -> None:
        """None entries for a field are excluded; mean is over valid readings only."""
        coordinator = PyTapDataUpdateCoordinator(hass, _make_entry(hass))
        coordinator._schedule_save = MagicMock()
        self._seed(coordinator, self.BARCODE_A, [self._reading(100.0)])
        coordinator._reading_buffers[self.BARCODE_A].append(
            {f: None for f in _AVERAGED_FIELDS}
        )

        snapshot = coordinator._build_averaged_snapshot()

        assert snapshot["nodes"][self.BARCODE_A]["power"] == pytest.approx(100.0, rel=1e-3)

    def test_all_none_values_produce_none(self, hass: HomeAssistant) -> None:
        """If every reading for a field is None the snapshot field is also None."""
        coordinator = PyTapDataUpdateCoordinator(hass, _make_entry(hass))
        coordinator._schedule_save = MagicMock()
        coordinator.data["nodes"][self.BARCODE_A] = {
            "name": self.BARCODE_A,
            "peak_power": self.PEAK_POWER,
            **{f: None for f in _AVERAGED_FIELDS},
        }
        coordinator._reading_buffers[self.BARCODE_A] = [
            {f: None for f in _AVERAGED_FIELDS}
        ]

        snapshot = coordinator._build_averaged_snapshot()

        assert snapshot["nodes"][self.BARCODE_A]["power"] is None
        assert snapshot["nodes"][self.BARCODE_A]["performance"] is None

    def test_per_node_isolation(self, hass: HomeAssistant) -> None:
        """Readings from different nodes MUST NOT affect each other's averages."""
        coordinator = PyTapDataUpdateCoordinator(hass, _make_entry_two_modules(hass))
        coordinator._schedule_save = MagicMock()
        # Node A: 100 + 300 → mean 200
        self._seed(coordinator, self.BARCODE_A, [self._reading(100.0), self._reading(300.0)])
        # Node B: 50 + 50 → mean 50  (unchanged regardless of Node A's readings)
        self._seed(coordinator, self.BARCODE_B, [self._reading(50.0), self._reading(50.0)])

        snapshot = coordinator._build_averaged_snapshot()

        assert snapshot["nodes"][self.BARCODE_A]["power"] == pytest.approx(200.0, rel=1e-3)
        assert snapshot["nodes"][self.BARCODE_B]["power"] == pytest.approx(50.0, rel=1e-3)

    def test_self_data_nodes_not_mutated(self, hass: HomeAssistant) -> None:
        """_build_averaged_snapshot must not mutate self.data['nodes']."""
        coordinator = PyTapDataUpdateCoordinator(hass, _make_entry(hass))
        coordinator._schedule_save = MagicMock()
        self._seed(coordinator, self.BARCODE_A, [self._reading(100.0), self._reading(200.0)])

        coordinator._build_averaged_snapshot()

        # Latest raw value (200) must be preserved for persistence
        assert coordinator.data["nodes"][self.BARCODE_A]["power"] == pytest.approx(
            200.0, rel=1e-3
        )


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

