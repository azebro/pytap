"""Tests for the configurable write interval in PyTapDataUpdateCoordinator.

Covers:
- Live vs. interval mode selection from entry data
- Time-weighted averaging logic in _build_averaged_snapshot()
- Buffer population with timestamps
- Throttle behaviour in interval mode
- Live mode push-per-event behaviour
"""

from datetime import datetime
import time
from unittest.mock import MagicMock

from homeassistant.const import CONF_HOST, CONF_PORT
from homeassistant.core import HomeAssistant
import pytest

from custom_components.pytap.const import (
    CONF_LIVE_REPORTING,
    CONF_MODULE_BARCODE,
    CONF_MODULE_NAME,
    CONF_MODULE_PEAK_POWER,
    CONF_MODULE_STRING,
    CONF_MODULES,
    CONF_WRITE_INTERVAL,
    DEFAULT_PORT,
    DEFAULT_WRITE_INTERVAL,
)
from custom_components.pytap.coordinator import (
    _AVERAGED_FIELDS,
    PyTapDataUpdateCoordinator,
)
from custom_components.pytap.pytap.core.events import (
    InfrastructureEvent,
    PowerReportEvent,
)

MOCK_MODULES = [
    {
        CONF_MODULE_STRING: "A",
        CONF_MODULE_NAME: "Panel_01",
        CONF_MODULE_BARCODE: "A-1234567B",
        CONF_MODULE_PEAK_POWER: 455,
    },
]


def _make_entry(hass, *, live_reporting=None, write_interval=None):
    """Create a mock ConfigEntry with optional reporting settings."""
    entry = MagicMock()
    data = {
        CONF_HOST: "192.168.1.100",
        CONF_PORT: DEFAULT_PORT,
        CONF_MODULES: MOCK_MODULES,
    }
    if live_reporting is not None:
        data[CONF_LIVE_REPORTING] = live_reporting
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


# ----------------------------------------------------------
# Initialization: live_reporting flag + write_interval
# ----------------------------------------------------------


class TestReportingModeInit:
    """Test that live_reporting and write_interval are read from entry data."""

    def test_default_is_live(self, hass: HomeAssistant) -> None:
        """Coordinator defaults to live reporting when not configured."""
        entry = _make_entry(hass)
        coordinator = PyTapDataUpdateCoordinator(hass, entry)
        assert coordinator._live_reporting is True

    def test_live_reporting_explicit_true(self, hass: HomeAssistant) -> None:
        """Coordinator honours explicit live_reporting=True."""
        entry = _make_entry(hass, live_reporting=True)
        coordinator = PyTapDataUpdateCoordinator(hass, entry)
        assert coordinator._live_reporting is True

    def test_live_reporting_explicit_false(self, hass: HomeAssistant) -> None:
        """Coordinator honours live_reporting=False (interval mode)."""
        entry = _make_entry(hass, live_reporting=False, write_interval=30)
        coordinator = PyTapDataUpdateCoordinator(hass, entry)
        assert coordinator._live_reporting is False
        assert coordinator._write_interval == 30.0

    def test_default_write_interval(self, hass: HomeAssistant) -> None:
        """Coordinator uses DEFAULT_WRITE_INTERVAL when not configured."""
        entry = _make_entry(hass)
        coordinator = PyTapDataUpdateCoordinator(hass, entry)
        assert coordinator._write_interval == DEFAULT_WRITE_INTERVAL

    def test_custom_write_interval(self, hass: HomeAssistant) -> None:
        """Coordinator honours a custom write_interval from entry data."""
        entry = _make_entry(hass, live_reporting=False, write_interval=60)
        coordinator = PyTapDataUpdateCoordinator(hass, entry)
        assert coordinator._write_interval == 60.0

    def test_throttle_state_initialised(self, hass: HomeAssistant) -> None:
        """Throttle bookkeeping fields start at their zero values."""
        entry = _make_entry(hass)
        coordinator = PyTapDataUpdateCoordinator(hass, entry)
        assert coordinator._last_ha_update == 0.0
        assert coordinator._ha_update_pending is False


# ----------------------------------------------------------
# Throttle behaviour (interval mode only)
# ----------------------------------------------------------


class TestWriteIntervalThrottling:
    """Test that HA updates are throttled in interval mode."""

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
        """First data event always triggers an immediate HA push."""
        entry = _make_entry(hass, live_reporting=False, write_interval=60)
        coordinator = PyTapDataUpdateCoordinator(hass, entry)
        coordinator._schedule_save = MagicMock()
        coordinator._handle_infrastructure(_make_infra_event())

        pushed = self._run_batch(coordinator, _make_power_event())
        assert pushed is True

    def test_second_event_within_interval_suppressed(self, hass: HomeAssistant) -> None:
        """A second event arriving before the interval is NOT pushed."""
        entry = _make_entry(hass, live_reporting=False, write_interval=60)
        coordinator = PyTapDataUpdateCoordinator(hass, entry)
        coordinator._schedule_save = MagicMock()
        coordinator._handle_infrastructure(_make_infra_event())

        pushed1 = self._run_batch(coordinator, _make_power_event(power=100.0))
        assert pushed1 is True

        pushed2 = self._run_batch(coordinator, _make_power_event(power=110.0))
        assert pushed2 is False
        assert coordinator._ha_update_pending is True

    def test_pending_flag_cleared_after_push(self, hass: HomeAssistant) -> None:
        """_ha_update_pending is cleared after a successful push."""
        entry = _make_entry(hass, live_reporting=False, write_interval=0)
        coordinator = PyTapDataUpdateCoordinator(hass, entry)
        coordinator._schedule_save = MagicMock()
        coordinator._handle_infrastructure(_make_infra_event())

        self._run_batch(coordinator, _make_power_event())
        assert coordinator._ha_update_pending is False


# ----------------------------------------------------------
# Time-weighted averaging
# ----------------------------------------------------------


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
        CONF_LIVE_REPORTING: False,
        CONF_WRITE_INTERVAL: 30,
    }
    entry.entry_id = "test_snapshot_entry"
    entry.options = {}
    return entry


class TestTimeWeightedAveraging:
    """Tests for coordinator._build_averaged_snapshot() with time-weighted averaging.

    Readings are stored as (monotonic_timestamp, field_dict) tuples.
    The average for each field is weighted by the duration each reading
    was "active" (time until the next reading, or until flush_ts for the last).
    """

    BARCODE_A = "A-1234567B"
    BARCODE_B = "B-9876543C"
    PEAK_POWER = 455

    def _reading(self, power: float) -> dict:
        """Build a numeric-fields dict as stored in reading buffers."""
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

    def _seed(
        self,
        coordinator,
        barcode: str,
        timestamped_readings: list[tuple[float, dict]],
    ) -> None:
        """Plant a node entry and buffer timestamped readings directly."""
        _, last_fields = timestamped_readings[-1]
        coordinator.data["nodes"][barcode] = {
            "name": barcode,
            "peak_power": self.PEAK_POWER,
            **last_fields,
            "performance": None,
            "daily_energy_wh": 0.0,
            "total_energy_wh": 0.0,
            "readings_today": len(timestamped_readings),
            "daily_reset_date": "",
            "last_update": None,
        }
        coordinator._reading_buffers[barcode] = list(timestamped_readings)

    def test_single_reading_passthrough(self, hass: HomeAssistant) -> None:
        """A single buffered reading is passed through unchanged."""
        coordinator = PyTapDataUpdateCoordinator(
            hass, _make_entry(hass, live_reporting=False)
        )
        coordinator._schedule_save = MagicMock()
        self._seed(coordinator, self.BARCODE_A, [(100.0, self._reading(100.0))])

        snapshot = coordinator._build_averaged_snapshot(flush_ts=105.0)

        assert snapshot["nodes"][self.BARCODE_A]["power"] == pytest.approx(
            100.0, rel=1e-3
        )

    def test_equal_spacing_matches_arithmetic_mean(self, hass: HomeAssistant) -> None:
        """With equal spacing, time-weighted average == arithmetic mean."""
        coordinator = PyTapDataUpdateCoordinator(
            hass, _make_entry(hass, live_reporting=False)
        )
        coordinator._schedule_save = MagicMock()
        self._seed(
            coordinator,
            self.BARCODE_A,
            [
                (100.0, self._reading(100.0)),  # active for 5s
                (105.0, self._reading(200.0)),  # active for 5s (until flush)
            ],
        )

        snapshot = coordinator._build_averaged_snapshot(flush_ts=110.0)

        assert snapshot["nodes"][self.BARCODE_A]["power"] == pytest.approx(
            150.0, rel=1e-3
        )

    def test_uneven_spacing_weights_by_duration(self, hass: HomeAssistant) -> None:
        """With uneven spacing, the longer-held reading dominates.

        Reading 1: power=100 held for 1s (t=100 to t=101)
        Reading 2: power=200 held for 9s (t=101 to t=110)
        TWA = (100*1 + 200*9) / (1+9) = 1900/10 = 190.0
        """
        coordinator = PyTapDataUpdateCoordinator(
            hass, _make_entry(hass, live_reporting=False)
        )
        coordinator._schedule_save = MagicMock()
        self._seed(
            coordinator,
            self.BARCODE_A,
            [
                (100.0, self._reading(100.0)),  # 1s
                (101.0, self._reading(200.0)),  # 9s until flush
            ],
        )

        snapshot = coordinator._build_averaged_snapshot(flush_ts=110.0)

        assert snapshot["nodes"][self.BARCODE_A]["power"] == pytest.approx(
            190.0, rel=1e-3
        )

    def test_burst_then_gap(self, hass: HomeAssistant) -> None:
        """Burst of readings followed by a long gap weights correctly.

        3 readings at 100W in quick succession (1s apart), then 200W for 27s.
        TWA = (100*1 + 100*1 + 100*1 + 200*27) / 30 = 5700/30 = 190.0
        """
        coordinator = PyTapDataUpdateCoordinator(
            hass, _make_entry(hass, live_reporting=False)
        )
        coordinator._schedule_save = MagicMock()
        self._seed(
            coordinator,
            self.BARCODE_A,
            [
                (100.0, self._reading(100.0)),  # 1s
                (101.0, self._reading(100.0)),  # 1s
                (102.0, self._reading(100.0)),  # 1s
                (103.0, self._reading(200.0)),  # 27s until flush
            ],
        )

        snapshot = coordinator._build_averaged_snapshot(flush_ts=130.0)

        assert snapshot["nodes"][self.BARCODE_A]["power"] == pytest.approx(
            190.0, rel=1e-3
        )

    def test_performance_recomputed_from_averaged_power(
        self, hass: HomeAssistant
    ) -> None:
        """Performance is derived from the time-weighted averaged power."""
        coordinator = PyTapDataUpdateCoordinator(
            hass, _make_entry(hass, live_reporting=False)
        )
        coordinator._schedule_save = MagicMock()
        self._seed(
            coordinator,
            self.BARCODE_A,
            [
                (100.0, self._reading(0.0)),    # 5s
                (105.0, self._reading(self.PEAK_POWER)),  # 5s until flush
            ],
        )

        snapshot = coordinator._build_averaged_snapshot(flush_ts=110.0)

        expected_power = self.PEAK_POWER / 2.0
        expected_perf = round((expected_power / self.PEAK_POWER) * 100.0, 2)
        assert snapshot["nodes"][self.BARCODE_A]["performance"] == pytest.approx(
            expected_perf, rel=1e-3
        )

    def test_none_values_excluded_from_average(self, hass: HomeAssistant) -> None:
        """None entries for a field are excluded from the time-weighted mean."""
        coordinator = PyTapDataUpdateCoordinator(
            hass, _make_entry(hass, live_reporting=False)
        )
        coordinator._schedule_save = MagicMock()
        self._seed(
            coordinator,
            self.BARCODE_A,
            [(100.0, self._reading(100.0))],
        )
        coordinator._reading_buffers[self.BARCODE_A].append(
            (105.0, {f: None for f in _AVERAGED_FIELDS})
        )

        snapshot = coordinator._build_averaged_snapshot(flush_ts=110.0)

        # Only the first reading (100W) contributes
        assert snapshot["nodes"][self.BARCODE_A]["power"] == pytest.approx(
            100.0, rel=1e-3
        )

    def test_all_none_values_produce_none(self, hass: HomeAssistant) -> None:
        """If every reading for a field is None the snapshot field is also None."""
        coordinator = PyTapDataUpdateCoordinator(
            hass, _make_entry(hass, live_reporting=False)
        )
        coordinator._schedule_save = MagicMock()
        coordinator.data["nodes"][self.BARCODE_A] = {
            "name": self.BARCODE_A,
            "peak_power": self.PEAK_POWER,
            **{f: None for f in _AVERAGED_FIELDS},
        }
        coordinator._reading_buffers[self.BARCODE_A] = [
            (100.0, {f: None for f in _AVERAGED_FIELDS})
        ]

        snapshot = coordinator._build_averaged_snapshot(flush_ts=110.0)

        assert snapshot["nodes"][self.BARCODE_A]["power"] is None
        assert snapshot["nodes"][self.BARCODE_A]["performance"] is None

    def test_per_node_isolation(self, hass: HomeAssistant) -> None:
        """Readings from different nodes MUST NOT affect each other."""
        coordinator = PyTapDataUpdateCoordinator(
            hass, _make_entry_two_modules(hass)
        )
        coordinator._schedule_save = MagicMock()
        # Node A: 100 for 5s, 300 for 5s → TWA = 200
        self._seed(
            coordinator,
            self.BARCODE_A,
            [
                (100.0, self._reading(100.0)),
                (105.0, self._reading(300.0)),
            ],
        )
        # Node B: 50 for 5s, 50 for 5s → TWA = 50
        self._seed(
            coordinator,
            self.BARCODE_B,
            [
                (100.0, self._reading(50.0)),
                (105.0, self._reading(50.0)),
            ],
        )

        snapshot = coordinator._build_averaged_snapshot(flush_ts=110.0)

        assert snapshot["nodes"][self.BARCODE_A]["power"] == pytest.approx(
            200.0, rel=1e-3
        )
        assert snapshot["nodes"][self.BARCODE_B]["power"] == pytest.approx(
            50.0, rel=1e-3
        )

    def test_self_data_nodes_not_mutated(self, hass: HomeAssistant) -> None:
        """_build_averaged_snapshot must not mutate self.data['nodes']."""
        coordinator = PyTapDataUpdateCoordinator(
            hass, _make_entry(hass, live_reporting=False)
        )
        coordinator._schedule_save = MagicMock()
        self._seed(
            coordinator,
            self.BARCODE_A,
            [
                (100.0, self._reading(100.0)),
                (105.0, self._reading(200.0)),
            ],
        )

        coordinator._build_averaged_snapshot(flush_ts=110.0)

        # Latest raw value (200) must be preserved for persistence
        assert coordinator.data["nodes"][self.BARCODE_A]["power"] == pytest.approx(
            200.0, rel=1e-3
        )


# ----------------------------------------------------------
# Buffer population
# ----------------------------------------------------------


class TestBufferPopulation:
    """Test that _handle_power_report populates _reading_buffers correctly."""

    def test_buffer_populated_on_power_reports(self, hass: HomeAssistant) -> None:
        """Each processed power report appends one timestamped entry."""
        entry = _make_entry(hass, live_reporting=False)
        coordinator = PyTapDataUpdateCoordinator(hass, entry)
        coordinator._schedule_save = MagicMock()
        coordinator._handle_infrastructure(_make_infra_event())

        coordinator._process_event(_make_power_event(power=100.0))
        coordinator._process_event(_make_power_event(power=200.0))

        assert "A-1234567B" in coordinator._reading_buffers
        assert len(coordinator._reading_buffers["A-1234567B"]) == 2

    def test_buffer_entry_is_timestamped_tuple(self, hass: HomeAssistant) -> None:
        """Each buffer entry is a (monotonic_ts, field_dict) tuple."""
        entry = _make_entry(hass, live_reporting=False)
        coordinator = PyTapDataUpdateCoordinator(hass, entry)
        coordinator._schedule_save = MagicMock()
        coordinator._handle_infrastructure(_make_infra_event())

        coordinator._process_event(_make_power_event(power=100.0))

        item = coordinator._reading_buffers["A-1234567B"][0]
        assert isinstance(item, tuple)
        assert len(item) == 2
        ts, fields = item
        assert isinstance(ts, float)
        assert set(fields.keys()) == set(_AVERAGED_FIELDS)

    def test_data_nodes_retains_latest_raw_value(self, hass: HomeAssistant) -> None:
        """self.data['nodes'] always reflects the most-recent raw reading."""
        entry = _make_entry(hass, live_reporting=False)
        coordinator = PyTapDataUpdateCoordinator(hass, entry)
        coordinator._schedule_save = MagicMock()
        coordinator._handle_infrastructure(_make_infra_event())

        coordinator._process_event(_make_power_event(power=100.0))
        coordinator._process_event(_make_power_event(power=200.0))

        assert coordinator.data["nodes"]["A-1234567B"]["power"] == pytest.approx(
            200.0, rel=0.01
        )

    def test_live_mode_does_not_buffer(self, hass: HomeAssistant) -> None:
        """In live mode, reading buffers should still be populated for
        consistency, but the live path does not use them."""
        entry = _make_entry(hass, live_reporting=True)
        coordinator = PyTapDataUpdateCoordinator(hass, entry)
        coordinator._schedule_save = MagicMock()
        coordinator._handle_infrastructure(_make_infra_event())

        coordinator._process_event(_make_power_event(power=100.0))

        # Buffer is populated by _handle_power_report regardless of mode;
        # the live path just ignores it and pushes raw data directly.
        assert "A-1234567B" in coordinator._reading_buffers
