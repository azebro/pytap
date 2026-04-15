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
from custom_components.pytap.coordinator import PyTapDataUpdateCoordinator
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
    return PowerReportEvent(
        gateway_id=1,
        node_id=1,
        barcode="A-1234567B",
        voltage_in=30.0,
        voltage_out=30.0,
        current_in=3.0,
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

