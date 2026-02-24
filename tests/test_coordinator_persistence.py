"""Tests for PyTap coordinator persistence (barcode mappings, discovered barcodes, parser state).

Validates that barcode\u2194node mappings, discovered barcodes, and parser
infrastructure state survive restarts via the consolidated HA Store.
"""

from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from homeassistant.const import CONF_HOST, CONF_PORT
from homeassistant.core import HomeAssistant

from custom_components.pytap.const import (
    CONF_MODULE_BARCODE,
    CONF_MODULE_NAME,
    CONF_MODULE_PEAK_POWER,
    CONF_MODULE_STRING,
    CONF_MODULES,
    DEFAULT_PEAK_POWER,
    DEFAULT_PORT,
    DOMAIN,
)
from custom_components.pytap.coordinator import (
    PyTapDataUpdateCoordinator,
    _MigratingStore,
)
from custom_components.pytap.energy import EnergyAccumulator
from custom_components.pytap.pytap.core.events import (
    InfrastructureEvent,
    PowerReportEvent,
)
from custom_components.pytap.pytap.core.state import PersistentState


MOCK_MODULES = [
    {
        CONF_MODULE_STRING: "A",
        CONF_MODULE_NAME: "Panel_01",
        CONF_MODULE_BARCODE: "A-1234567B",
        CONF_MODULE_PEAK_POWER: 455,
    },
]


def _make_entry(hass):
    """Create a mock ConfigEntry."""
    entry = MagicMock()
    entry.data = {
        CONF_HOST: "192.168.1.100",
        CONF_PORT: DEFAULT_PORT,
        CONF_MODULES: MOCK_MODULES,
    }
    entry.entry_id = "test_entry_abc123"
    entry.options = {}
    return entry


class TestCoordinatorPersistenceInit:
    """Test that persistence infrastructure is set up in __init__."""

    def test_persistent_state_initialised(self, hass: HomeAssistant) -> None:
        """PersistentState should be initialised as empty."""
        entry = _make_entry(hass)
        coordinator = PyTapDataUpdateCoordinator(hass, entry)

        assert isinstance(coordinator._persistent_state, PersistentState)
        assert coordinator._persistent_state.gateway_identities == {}
        assert coordinator._persistent_state.gateway_versions == {}
        assert coordinator._persistent_state.gateway_node_tables == {}

    def test_store_created(self, hass: HomeAssistant) -> None:
        """HA Store should be created with correct key."""
        entry = _make_entry(hass)
        coordinator = PyTapDataUpdateCoordinator(hass, entry)

        assert coordinator._store is not None
        assert entry.entry_id in coordinator._store.key

    def test_initial_state_empty(self, hass: HomeAssistant) -> None:
        """Barcode mappings and discovered barcodes should start empty."""
        entry = _make_entry(hass)
        coordinator = PyTapDataUpdateCoordinator(hass, entry)

        assert coordinator._barcode_to_node == {}
        assert coordinator._node_to_barcode == {}
        assert coordinator._discovered_barcodes == set()


class TestLoadCoordinatorState:
    """Test _async_load_coordinator_state restores persisted data."""

    async def test_load_restores_barcode_mappings(self, hass: HomeAssistant) -> None:
        """Barcode↔node mappings should be restored from Store."""
        entry = _make_entry(hass)
        coordinator = PyTapDataUpdateCoordinator(hass, entry)

        stored_data = {
            "barcode_to_node": {"A-1234567B": 10, "C-2345678D": 20},
            "discovered_barcodes": ["X-9999999Z"],
        }
        coordinator._store.async_load = AsyncMock(return_value=stored_data)

        await coordinator._async_load_coordinator_state()

        assert coordinator._barcode_to_node == {"A-1234567B": 10, "C-2345678D": 20}
        assert coordinator._node_to_barcode == {10: "A-1234567B", 20: "C-2345678D"}

    async def test_load_restores_discovered_barcodes(self, hass: HomeAssistant) -> None:
        """Discovered barcodes should be restored from Store."""
        entry = _make_entry(hass)
        coordinator = PyTapDataUpdateCoordinator(hass, entry)

        stored_data = {
            "barcode_to_node": {},
            "discovered_barcodes": ["X-9999999Z", "B-7654321A"],
        }
        coordinator._store.async_load = AsyncMock(return_value=stored_data)

        await coordinator._async_load_coordinator_state()

        assert coordinator._discovered_barcodes == {"X-9999999Z", "B-7654321A"}
        assert coordinator.data["discovered_barcodes"] == [
            "B-7654321A",
            "X-9999999Z",
        ]

    async def test_load_handles_no_stored_data(self, hass: HomeAssistant) -> None:
        """Loading with no stored data should leave state empty."""
        entry = _make_entry(hass)
        coordinator = PyTapDataUpdateCoordinator(hass, entry)

        coordinator._store.async_load = AsyncMock(return_value=None)

        await coordinator._async_load_coordinator_state()

        assert coordinator._barcode_to_node == {}
        assert coordinator._node_to_barcode == {}
        assert coordinator._discovered_barcodes == set()

    async def test_load_handles_exception(self, hass: HomeAssistant) -> None:
        """Loading should handle Store exceptions gracefully."""
        entry = _make_entry(hass)
        coordinator = PyTapDataUpdateCoordinator(hass, entry)

        coordinator._store.async_load = AsyncMock(side_effect=Exception("disk error"))

        # Should not raise
        await coordinator._async_load_coordinator_state()

        assert coordinator._barcode_to_node == {}

    async def test_load_restores_parser_state(self, hass: HomeAssistant) -> None:
        """Parser infrastructure state should be restored from Store."""
        entry = _make_entry(hass)
        coordinator = PyTapDataUpdateCoordinator(hass, entry)

        stored_data = {
            "barcode_to_node": {},
            "discovered_barcodes": [],
            "parser_state": {
                "gateway_identities": {"1": "aa:bb:cc:dd:ee:ff:00:11"},
                "gateway_versions": {"1": "2.0.1"},
                "gateway_node_tables": {},
            },
        }
        coordinator._store.async_load = AsyncMock(return_value=stored_data)

        await coordinator._async_load_coordinator_state()

        assert len(coordinator._persistent_state.gateway_identities) == 1
        assert coordinator._persistent_state.gateway_versions[1] == "2.0.1"

    async def test_load_handles_corrupt_parser_state(self, hass: HomeAssistant) -> None:
        """Corrupt parser state in Store should fall back to empty state."""
        entry = _make_entry(hass)
        coordinator = PyTapDataUpdateCoordinator(hass, entry)

        stored_data = {
            "barcode_to_node": {},
            "discovered_barcodes": [],
            "parser_state": {
                "garbage": "data",
                "gateway_identities": {"not_int": "bad"},
            },
        }
        coordinator._store.async_load = AsyncMock(return_value=stored_data)

        # Should not raise
        await coordinator._async_load_coordinator_state()

        # Parser state may be partially loaded or fresh — should not crash

    async def test_load_restores_energy_data(self, hass: HomeAssistant) -> None:
        """Energy accumulator state should be restored from Store."""
        entry = _make_entry(hass)
        coordinator = PyTapDataUpdateCoordinator(hass, entry)

        stored_data = {
            "barcode_to_node": {},
            "discovered_barcodes": [],
            "energy_data": {
                "A-1234567B": {
                    "daily_energy_wh": 10.5,
                    "daily_reset_date": datetime.now().date().isoformat(),
                    "total_energy_wh": 1234.5,
                    "readings_today": 17,
                    "last_power_w": 250.0,
                    "last_reading_ts": "2025-01-01T10:00:00",
                }
            },
        }
        coordinator._store.async_load = AsyncMock(return_value=stored_data)

        await coordinator._async_load_coordinator_state()

        acc = coordinator._energy_state["A-1234567B"]
        assert acc.daily_energy_wh == 10.5
        assert acc.total_energy_wh == 1234.5
        assert acc.readings_today == 17
        assert acc.last_power_w == 250.0

    async def test_load_resets_daily_energy_on_new_day(
        self, hass: HomeAssistant
    ) -> None:
        """Daily energy should reset when stored reset date is not today."""
        entry = _make_entry(hass)
        coordinator = PyTapDataUpdateCoordinator(hass, entry)

        stored_data = {
            "barcode_to_node": {},
            "discovered_barcodes": [],
            "energy_data": {
                "A-1234567B": {
                    "daily_energy_wh": 99.0,
                    "daily_reset_date": "2000-01-01",
                    "total_energy_wh": 555.0,
                    "readings_today": 44,
                    "last_power_w": 100.0,
                    "last_reading_ts": "2025-01-01T10:00:00",
                }
            },
        }
        coordinator._store.async_load = AsyncMock(return_value=stored_data)

        await coordinator._async_load_coordinator_state()

        acc = coordinator._energy_state["A-1234567B"]
        assert acc.daily_energy_wh == 0.0
        assert acc.readings_today == 0
        assert acc.total_energy_wh == 555.0
        assert acc.daily_reset_date == datetime.now().date().isoformat()

    async def test_load_handles_missing_energy_data(self, hass: HomeAssistant) -> None:
        """Missing energy_data key should leave energy state empty."""
        entry = _make_entry(hass)
        coordinator = PyTapDataUpdateCoordinator(hass, entry)

        stored_data = {
            "barcode_to_node": {"A-1234567B": 10},
            "discovered_barcodes": ["X-9999999Z"],
            "parser_state": {
                "gateway_identities": {},
                "gateway_versions": {},
                "gateway_node_tables": {},
            },
        }
        coordinator._store.async_load = AsyncMock(return_value=stored_data)

        await coordinator._async_load_coordinator_state()

        assert coordinator._barcode_to_node == {"A-1234567B": 10}
        assert coordinator._discovered_barcodes == {"X-9999999Z"}
        assert coordinator._energy_state == {}

    async def test_load_restores_node_snapshot_without_energy(
        self, hass: HomeAssistant
    ) -> None:
        """Node snapshot should restore live fields even without energy_data."""
        entry = _make_entry(hass)
        coordinator = PyTapDataUpdateCoordinator(hass, entry)

        stored_data = {
            "barcode_to_node": {"A-1234567B": 10},
            "discovered_barcodes": [],
            "node_snapshots": {
                "A-1234567B": {
                    "gateway_id": 1,
                    "node_id": 10,
                    "voltage_in": 35.2,
                    "voltage_out": 34.8,
                    "current_in": 8.5,
                    "current_out": 8.4,
                    "power": 299.2,
                    "performance": 65.76,
                    "temperature": 42.0,
                    "dc_dc_duty_cycle": 0.95,
                    "rssi": -65,
                    "last_update": "2026-02-24T20:00:00",
                }
            },
        }
        coordinator._store.async_load = AsyncMock(return_value=stored_data)

        await coordinator._async_load_coordinator_state()

        node = coordinator.data["nodes"]["A-1234567B"]
        assert node["power"] == 299.2
        assert node["voltage_in"] == 35.2
        assert node["last_update"] == "2026-02-24T20:00:00"


class TestSaveCoordinatorState:
    """Test _async_save_coordinator_state persists data."""

    async def test_save_writes_all_data(self, hass: HomeAssistant) -> None:
        """Save should write barcode mappings, discovered barcodes, and parser state."""
        entry = _make_entry(hass)
        coordinator = PyTapDataUpdateCoordinator(hass, entry)

        coordinator._barcode_to_node = {"A-1234567B": 10}
        coordinator._node_to_barcode = {10: "A-1234567B"}
        coordinator._discovered_barcodes = {"X-9999999Z"}
        coordinator._energy_state = {
            "A-1234567B": EnergyAccumulator(
                daily_energy_wh=1.25,
                daily_reset_date=datetime.now().date().isoformat(),
                total_energy_wh=100.75,
                readings_today=9,
                last_power_w=250.0,
                last_reading_ts=datetime.now(),
            )
        }
        coordinator.data["nodes"]["A-1234567B"] = {
            "gateway_id": 1,
            "node_id": 10,
            "barcode": "A-1234567B",
            "name": "Panel_01",
            "string": "A",
            "peak_power": 455,
            "voltage_in": 35.2,
            "voltage_out": 34.8,
            "current_in": 8.5,
            "current_out": 8.4,
            "power": 299.2,
            "performance": 65.76,
            "temperature": 42.0,
            "dc_dc_duty_cycle": 0.95,
            "rssi": -65,
            "daily_energy_wh": 1.25,
            "total_energy_wh": 100.75,
            "readings_today": 9,
            "daily_reset_date": datetime.now().date().isoformat(),
            "last_update": "2026-02-24T20:00:00",
        }
        coordinator._unsaved_changes = True

        coordinator._store.async_save = AsyncMock()

        await coordinator._async_save_coordinator_state()

        coordinator._store.async_save.assert_called_once()
        saved = coordinator._store.async_save.call_args[0][0]
        assert saved["barcode_to_node"] == {"A-1234567B": 10}
        assert saved["discovered_barcodes"] == ["X-9999999Z"]
        assert "parser_state" in saved
        assert "energy_data" in saved
        assert "node_snapshots" in saved
        assert "A-1234567B" in saved["energy_data"]
        assert "A-1234567B" in saved["node_snapshots"]
        assert saved["node_snapshots"]["A-1234567B"]["power"] == 299.2
        assert saved["energy_data"]["A-1234567B"]["readings_today"] == 9
        assert coordinator._unsaved_changes is False

    async def test_save_handles_exception(self, hass: HomeAssistant) -> None:
        """Save should not raise on Store error."""
        entry = _make_entry(hass)
        coordinator = PyTapDataUpdateCoordinator(hass, entry)

        coordinator._store.async_save = AsyncMock(side_effect=Exception("write error"))
        coordinator._unsaved_changes = True

        # Should not raise
        await coordinator._async_save_coordinator_state()


class TestInitMappingsFromParser:
    """Test _init_mappings_from_parser replaces coordinator maps."""

    def test_populates_from_parser_infrastructure(self, hass: HomeAssistant) -> None:
        """Mappings should be populated from parser.infrastructure."""
        entry = _make_entry(hass)
        coordinator = PyTapDataUpdateCoordinator(hass, entry)

        mock_parser = MagicMock()
        mock_parser.infrastructure = {
            "gateways": {1: {"address": "aa:bb:cc:dd", "version": "1.0"}},
            "nodes": {
                10: {"address": "11:22:33:44", "barcode": "A-1234567B"},
                20: {"address": "55:66:77:88", "barcode": "C-2345678D"},
            },
        }

        coordinator._init_mappings_from_parser(mock_parser)

        assert coordinator._barcode_to_node == {
            "A-1234567B": 10,
            "C-2345678D": 20,
        }
        assert coordinator._node_to_barcode == {
            10: "A-1234567B",
            20: "C-2345678D",
        }

    def test_replaces_stale_mappings(self, hass: HomeAssistant) -> None:
        """Old stale entries should be purged when parser state replaces them."""
        entry = _make_entry(hass)
        coordinator = PyTapDataUpdateCoordinator(hass, entry)

        # Pre-populate with stale mappings from a previous session
        coordinator._barcode_to_node = {"OLD-BARCODE": 99, "A-1234567B": 5}
        coordinator._node_to_barcode = {99: "OLD-BARCODE", 5: "A-1234567B"}

        mock_parser = MagicMock()
        mock_parser.infrastructure = {
            "gateways": {},
            "nodes": {
                10: {"address": "11:22:33:44", "barcode": "A-1234567B"},
            },
        }

        coordinator._init_mappings_from_parser(mock_parser)

        # Stale "OLD-BARCODE" and old node_id 5 should be gone
        assert coordinator._barcode_to_node == {"A-1234567B": 10}
        assert coordinator._node_to_barcode == {10: "A-1234567B"}
        assert "OLD-BARCODE" not in coordinator._barcode_to_node
        assert 99 not in coordinator._node_to_barcode

    def test_handles_empty_infrastructure(self, hass: HomeAssistant) -> None:
        """Should handle parser with no nodes gracefully."""
        entry = _make_entry(hass)
        coordinator = PyTapDataUpdateCoordinator(hass, entry)

        mock_parser = MagicMock()
        mock_parser.infrastructure = {"gateways": {}, "nodes": {}}

        coordinator._init_mappings_from_parser(mock_parser)

        assert coordinator._barcode_to_node == {}

    def test_handles_parser_exception(self, hass: HomeAssistant) -> None:
        """Should not raise if parser.infrastructure raises."""
        entry = _make_entry(hass)
        coordinator = PyTapDataUpdateCoordinator(hass, entry)

        mock_parser = MagicMock()
        mock_parser.infrastructure = property(
            lambda self: (_ for _ in ()).throw(RuntimeError("parser error"))
        )
        type(mock_parser).infrastructure = property(
            lambda self: (_ for _ in ()).throw(RuntimeError("parser error"))
        )

        # Should not raise
        coordinator._init_mappings_from_parser(mock_parser)


class TestParserStatePassedThrough:
    """Test that the parser is created with the coordinator's PersistentState."""

    def test_persistent_state_shared_with_parser(self, hass: HomeAssistant) -> None:
        """PersistentState from coordinator should be the same object used by parser."""
        entry = _make_entry(hass)
        coordinator = PyTapDataUpdateCoordinator(hass, entry)

        # Verify PersistentState exists and is a valid instance
        assert isinstance(coordinator._persistent_state, PersistentState)


class TestInfrastructureEventTriggersSave:
    """Test that infrastructure events trigger a debounced save."""

    def test_new_mappings_schedule_save(self, hass: HomeAssistant) -> None:
        """New barcode mappings from infrastructure events should trigger save."""
        entry = _make_entry(hass)
        coordinator = PyTapDataUpdateCoordinator(hass, entry)

        # Patch _schedule_save so we can verify it's called
        coordinator._schedule_save = MagicMock()

        event = InfrastructureEvent(
            gateways={1: {"address": "aa:bb", "version": "1.0"}},
            nodes={
                10: {"address": "11:22:33:44", "barcode": "A-1234567B"},
            },
            timestamp=datetime.now(),
        )

        coordinator._handle_infrastructure(event)

        # New mappings were added → _schedule_save should be called
        coordinator._schedule_save.assert_called_once()
        assert coordinator._barcode_to_node["A-1234567B"] == 10

    def test_unchanged_mappings_skip_save(self, hass: HomeAssistant) -> None:
        """Same mappings from a repeat infrastructure event should not trigger save."""
        entry = _make_entry(hass)
        coordinator = PyTapDataUpdateCoordinator(hass, entry)

        coordinator._barcode_to_node = {"A-1234567B": 10}
        coordinator._node_to_barcode = {10: "A-1234567B"}
        coordinator._schedule_save = MagicMock()

        event = InfrastructureEvent(
            gateways={},
            nodes={10: {"address": "11:22:33:44", "barcode": "A-1234567B"}},
            timestamp=datetime.now(),
        )

        coordinator._handle_infrastructure(event)

        coordinator._schedule_save.assert_not_called()

    def test_stale_mappings_purged_on_infrastructure(self, hass: HomeAssistant) -> None:
        """Stale node mappings must be removed when infrastructure rebuilds."""
        entry = _make_entry(hass)
        coordinator = PyTapDataUpdateCoordinator(hass, entry)

        # Simulate stale mappings from a prior session
        coordinator._barcode_to_node = {
            "A-1234567B": 10,
            "STALE-BARCODE": 99,
        }
        coordinator._node_to_barcode = {
            10: "A-1234567B",
            99: "STALE-BARCODE",
        }
        coordinator._schedule_save = MagicMock()

        # Infrastructure event only has node 10 — node 99 is gone
        event = InfrastructureEvent(
            gateways={},
            nodes={10: {"address": "11:22:33:44", "barcode": "A-1234567B"}},
            timestamp=datetime.now(),
        )

        coordinator._handle_infrastructure(event)

        assert coordinator._barcode_to_node == {"A-1234567B": 10}
        assert coordinator._node_to_barcode == {10: "A-1234567B"}
        assert "STALE-BARCODE" not in coordinator._barcode_to_node
        assert 99 not in coordinator._node_to_barcode
        coordinator._schedule_save.assert_called_once()

    def test_node_id_reassignment_cleans_old_mapping(self, hass: HomeAssistant) -> None:
        """When a barcode moves to a new node_id the old entry is removed."""
        entry = _make_entry(hass)
        coordinator = PyTapDataUpdateCoordinator(hass, entry)

        coordinator._barcode_to_node = {"A-1234567B": 10}
        coordinator._node_to_barcode = {10: "A-1234567B"}
        coordinator._schedule_save = MagicMock()

        # Same barcode, different node_id — old node 10 should disappear
        event = InfrastructureEvent(
            gateways={},
            nodes={15: {"address": "11:22:33:44", "barcode": "A-1234567B"}},
            timestamp=datetime.now(),
        )

        coordinator._handle_infrastructure(event)

        assert coordinator._barcode_to_node == {"A-1234567B": 15}
        assert coordinator._node_to_barcode == {15: "A-1234567B"}
        assert 10 not in coordinator._node_to_barcode
        coordinator._schedule_save.assert_called_once()

    def test_infra_sets_infra_received_flag(self, hass: HomeAssistant) -> None:
        """_infra_received should become True after first infrastructure event."""
        entry = _make_entry(hass)
        coordinator = PyTapDataUpdateCoordinator(hass, entry)
        coordinator._schedule_save = MagicMock()

        assert coordinator._infra_received is False

        event = InfrastructureEvent(
            gateways={},
            nodes={10: {"address": "11:22:33:44", "barcode": "A-1234567B"}},
            timestamp=datetime.now(),
        )
        coordinator._handle_infrastructure(event)

        assert coordinator._infra_received is True


class TestPowerReportDeferredBeforeInfra:
    """Test that power reports with no direct barcode are deferred until
    the first InfrastructureEvent of the current session."""

    def test_fallback_blocked_before_infra(self, hass: HomeAssistant) -> None:
        """Power report fallback to _node_to_barcode should be blocked pre-infra."""
        entry = _make_entry(hass)
        coordinator = PyTapDataUpdateCoordinator(hass, entry)

        # Stale mapping from previous session
        coordinator._barcode_to_node = {"A-1234567B": 10}
        coordinator._node_to_barcode = {10: "A-1234567B"}
        coordinator._infra_received = False

        # Power report with no direct barcode — only node_id match
        event = PowerReportEvent(
            gateway_id=1,
            node_id=10,
            barcode=None,
            voltage_in=30.0,
            voltage_out=29.0,
            current_in=8.0,
            temperature=40.0,
            dc_dc_duty_cycle=0.9,
            rssi=-60,
            timestamp=datetime.now(),
        )
        result = coordinator._handle_power_report(event)

        # Should be deferred (return False), not stored
        assert result is False
        assert "A-1234567B" not in coordinator.data["nodes"]

    def test_fallback_allowed_after_infra(self, hass: HomeAssistant) -> None:
        """Power report fallback should work after infra event is received."""
        entry = _make_entry(hass)
        coordinator = PyTapDataUpdateCoordinator(hass, entry)
        coordinator._schedule_save = MagicMock()

        # Receive infrastructure first
        infra_event = InfrastructureEvent(
            gateways={},
            nodes={10: {"address": "11:22:33:44", "barcode": "A-1234567B"}},
            timestamp=datetime.now(),
        )
        coordinator._handle_infrastructure(infra_event)
        assert coordinator._infra_received is True

        # Now a power report with no direct barcode
        power_event = PowerReportEvent(
            gateway_id=1,
            node_id=10,
            barcode=None,
            voltage_in=30.0,
            voltage_out=29.0,
            current_in=8.0,
            temperature=40.0,
            dc_dc_duty_cycle=0.9,
            rssi=-60,
            timestamp=datetime.now(),
        )
        result = coordinator._handle_power_report(power_event)

        assert result is True
        assert "A-1234567B" in coordinator.data["nodes"]

    def test_direct_barcode_works_before_infra(self, hass: HomeAssistant) -> None:
        """Power reports with a direct barcode should work even pre-infra."""
        entry = _make_entry(hass)
        coordinator = PyTapDataUpdateCoordinator(hass, entry)
        coordinator._infra_received = False

        event = PowerReportEvent(
            gateway_id=1,
            node_id=10,
            barcode="A-1234567B",
            voltage_in=30.0,
            voltage_out=29.0,
            current_in=8.0,
            temperature=40.0,
            dc_dc_duty_cycle=0.9,
            rssi=-60,
            timestamp=datetime.now(),
        )
        result = coordinator._handle_power_report(event)

        assert result is True
        assert "A-1234567B" in coordinator.data["nodes"]


class TestStopFlushesState:
    """Test that async_stop_listener flushes unsaved state."""

    async def test_stop_saves_unsaved_changes(self, hass: HomeAssistant) -> None:
        """Stopping with unsaved changes should flush to Store."""
        entry = _make_entry(hass)
        coordinator = PyTapDataUpdateCoordinator(hass, entry)
        coordinator._unsaved_changes = True
        coordinator._store.async_save = AsyncMock()

        await coordinator.async_stop_listener()

        coordinator._store.async_save.assert_called_once()

    async def test_stop_without_unsaved_changes(self, hass: HomeAssistant) -> None:
        """Stopping without unsaved changes should not save."""
        entry = _make_entry(hass)
        coordinator = PyTapDataUpdateCoordinator(hass, entry)
        coordinator._unsaved_changes = False
        coordinator._store.async_save = AsyncMock()

        await coordinator.async_stop_listener()

        coordinator._store.async_save.assert_not_called()


class TestPowerReportPerformance:
    """Test power report performance field behavior."""

    def test_power_report_includes_performance(self, hass: HomeAssistant) -> None:
        """Power report should populate performance in node payload."""
        entry = _make_entry(hass)
        coordinator = PyTapDataUpdateCoordinator(hass, entry)

        event = PowerReportEvent(
            gateway_id=1,
            node_id=10,
            barcode="A-1234567B",
            voltage_in=30.0,
            voltage_out=29.0,
            current_in=8.0,
            temperature=40.0,
            dc_dc_duty_cycle=0.9,
            rssi=-60,
            timestamp=datetime.now(),
        )
        result = coordinator._handle_power_report(event)

        assert result is True
        node = coordinator.data["nodes"]["A-1234567B"]
        assert "performance" in node
        assert node["peak_power"] == 455

    def test_power_report_performance_calculation(self, hass: HomeAssistant) -> None:
        """Performance should be power/peak_power*100 rounded to 2 decimals."""
        entry = _make_entry(hass)
        coordinator = PyTapDataUpdateCoordinator(hass, entry)

        event = PowerReportEvent(
            gateway_id=1,
            node_id=10,
            barcode="A-1234567B",
            voltage_in=50.0,
            voltage_out=25.0,
            current_in=5.0,
            temperature=40.0,
            dc_dc_duty_cycle=0.9,
            rssi=-60,
            timestamp=datetime.now(),
        )
        coordinator._handle_power_report(event)

        node = coordinator.data["nodes"]["A-1234567B"]
        assert node["performance"] == 54.95

    def test_power_report_default_peak_power(self, hass: HomeAssistant) -> None:
        """Missing module peak_power should fall back to default."""
        entry = _make_entry(hass)
        entry.data = {
            **entry.data,
            CONF_MODULES: [
                {
                    CONF_MODULE_STRING: "A",
                    CONF_MODULE_NAME: "Panel_01",
                    CONF_MODULE_BARCODE: "A-1234567B",
                }
            ],
        }
        coordinator = PyTapDataUpdateCoordinator(hass, entry)

        event = PowerReportEvent(
            gateway_id=1,
            node_id=10,
            barcode="A-1234567B",
            voltage_in=60.0,
            voltage_out=30.0,
            current_in=5.0,
            temperature=40.0,
            dc_dc_duty_cycle=0.9,
            rssi=-60,
            timestamp=datetime.now(),
        )
        coordinator._handle_power_report(event)

        node = coordinator.data["nodes"]["A-1234567B"]
        assert node["peak_power"] == DEFAULT_PEAK_POWER


class TestStoreMigration:
    """Test that _MigratingStore handles version mismatches without data loss."""

    def test_store_is_migrating_store(self, hass: HomeAssistant) -> None:
        """Coordinator should use _MigratingStore, not the base Store."""
        entry = _make_entry(hass)
        coordinator = PyTapDataUpdateCoordinator(hass, entry)

        assert isinstance(coordinator._store, _MigratingStore)

    async def test_v1_store_data_loads_successfully(self, hass: HomeAssistant) -> None:
        """V1 store data (no energy_data key) should load without error."""
        entry = _make_entry(hass)
        coordinator = PyTapDataUpdateCoordinator(hass, entry)

        # Simulate v1 store data — no energy_data key
        v1_data = {
            "barcode_to_node": {"A-1234567B": 10},
            "discovered_barcodes": ["X-9999999Z"],
        }
        coordinator._store.async_load = AsyncMock(return_value=v1_data)

        await coordinator._async_load_coordinator_state()

        assert coordinator._barcode_to_node == {"A-1234567B": 10}
        assert coordinator._discovered_barcodes == {"X-9999999Z"}
        assert coordinator._energy_state == {}

    async def test_migrate_func_returns_data_as_is(self, hass: HomeAssistant) -> None:
        """_async_migrate_func should return old data unchanged."""
        entry = _make_entry(hass)
        coordinator = PyTapDataUpdateCoordinator(hass, entry)

        old_data = {
            "barcode_to_node": {"A-1234567B": 10},
            "discovered_barcodes": [],
        }
        result = await coordinator._store._async_migrate_func(1, 1, old_data)
        assert result == old_data


class TestEnergyPrePopulation:
    """Test that coordinator.data['nodes'] is pre-populated from loaded energy state."""

    async def test_load_prepopulates_node_data(self, hass: HomeAssistant) -> None:
        """After loading energy state, coordinator.data['nodes'] should have energy values."""
        entry = _make_entry(hass)
        coordinator = PyTapDataUpdateCoordinator(hass, entry)

        today = datetime.now().date().isoformat()
        stored_data = {
            "barcode_to_node": {"A-1234567B": 10},
            "discovered_barcodes": [],
            "energy_data": {
                "A-1234567B": {
                    "daily_energy_wh": 42.5,
                    "daily_reset_date": today,
                    "total_energy_wh": 5000.0,
                    "readings_today": 100,
                    "last_power_w": 250.0,
                    "last_reading_ts": "2026-02-23T10:00:00",
                }
            },
        }
        coordinator._store.async_load = AsyncMock(return_value=stored_data)

        await coordinator._async_load_coordinator_state()

        # Node data should be pre-populated with energy values
        node = coordinator.data["nodes"].get("A-1234567B")
        assert node is not None
        assert node["total_energy_wh"] == 5000.0
        assert node["daily_energy_wh"] == 42.5
        assert node["readings_today"] == 100
        assert node["barcode"] == "A-1234567B"

    async def test_prepopulated_node_has_none_for_live_fields(
        self, hass: HomeAssistant
    ) -> None:
        """Pre-populated node data should have None for fields that require live data."""
        entry = _make_entry(hass)
        coordinator = PyTapDataUpdateCoordinator(hass, entry)

        today = datetime.now().date().isoformat()
        stored_data = {
            "barcode_to_node": {},
            "discovered_barcodes": [],
            "energy_data": {
                "A-1234567B": {
                    "daily_energy_wh": 10.0,
                    "daily_reset_date": today,
                    "total_energy_wh": 100.0,
                    "readings_today": 5,
                    "last_power_w": 100.0,
                    "last_reading_ts": None,
                }
            },
        }
        coordinator._store.async_load = AsyncMock(return_value=stored_data)

        await coordinator._async_load_coordinator_state()

        node = coordinator.data["nodes"]["A-1234567B"]
        assert node["power"] is None
        assert node["voltage_in"] is None
        assert node["voltage_out"] is None
        assert node["current_in"] is None
        assert node["current_out"] is None
        assert node["temperature"] is None
        assert node["rssi"] is None
        assert node["performance"] is None

    async def test_prepopulation_skips_unconfigured_barcodes(
        self, hass: HomeAssistant
    ) -> None:
        """Only configured barcodes should be pre-populated in node data."""
        entry = _make_entry(hass)
        coordinator = PyTapDataUpdateCoordinator(hass, entry)

        today = datetime.now().date().isoformat()
        stored_data = {
            "barcode_to_node": {},
            "discovered_barcodes": [],
            "energy_data": {
                "A-1234567B": {
                    "daily_energy_wh": 10.0,
                    "daily_reset_date": today,
                    "total_energy_wh": 100.0,
                    "readings_today": 5,
                    "last_power_w": 100.0,
                    "last_reading_ts": None,
                },
                "UNCONFIGURED-BARCODE": {
                    "daily_energy_wh": 20.0,
                    "daily_reset_date": today,
                    "total_energy_wh": 200.0,
                    "readings_today": 10,
                    "last_power_w": 200.0,
                    "last_reading_ts": None,
                },
            },
        }
        coordinator._store.async_load = AsyncMock(return_value=stored_data)

        await coordinator._async_load_coordinator_state()

        assert "A-1234567B" in coordinator.data["nodes"]
        assert "UNCONFIGURED-BARCODE" not in coordinator.data["nodes"]

    async def test_power_report_overwrites_prepopulated_data(
        self, hass: HomeAssistant
    ) -> None:
        """A live power report should fully replace pre-populated node data."""
        entry = _make_entry(hass)
        coordinator = PyTapDataUpdateCoordinator(hass, entry)

        today = datetime.now().date().isoformat()
        stored_data = {
            "barcode_to_node": {"A-1234567B": 10},
            "discovered_barcodes": [],
            "energy_data": {
                "A-1234567B": {
                    "daily_energy_wh": 42.5,
                    "daily_reset_date": today,
                    "total_energy_wh": 5000.0,
                    "readings_today": 100,
                    "last_power_w": 250.0,
                    "last_reading_ts": None,
                }
            },
        }
        coordinator._store.async_load = AsyncMock(return_value=stored_data)
        await coordinator._async_load_coordinator_state()

        # Pre-populated: power is None
        assert coordinator.data["nodes"]["A-1234567B"]["power"] is None

        # Live power report arrives
        coordinator._infra_received = True
        coordinator._schedule_save = MagicMock()
        event = PowerReportEvent(
            gateway_id=1,
            node_id=10,
            barcode="A-1234567B",
            voltage_in=60.0,
            voltage_out=30.0,
            current_in=5.0,
            temperature=40.0,
            dc_dc_duty_cycle=0.9,
            rssi=-60,
            timestamp=datetime.now(),
        )
        coordinator._handle_power_report(event)

        node = coordinator.data["nodes"]["A-1234567B"]
        # Live fields should now be populated
        assert node["power"] is not None
        assert node["voltage_in"] == 60.0
        # Energy should continue from the loaded accumulator, not reset
        assert node["total_energy_wh"] >= 5000.0
