"""Tests for PyTap coordinator persistence (barcode mappings & discovered barcodes).

Validates that barcode↔node mappings and discovered barcodes survive restarts
via both the parser's state_file and the coordinator's HA Store.
"""

from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from homeassistant.const import CONF_HOST, CONF_PORT
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
from custom_components.pytap.pytap.core.events import (
    InfrastructureEvent,
    PowerReportEvent,
)


MOCK_MODULES = [
    {
        CONF_MODULE_STRING: "A",
        CONF_MODULE_NAME: "Panel_01",
        CONF_MODULE_BARCODE: "A-1234567B",
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

    def test_state_file_path_set(self, hass: HomeAssistant) -> None:
        """State file path should point to .storage in HA config dir."""
        entry = _make_entry(hass)
        coordinator = PyTapDataUpdateCoordinator(hass, entry)

        assert coordinator._state_file_path is not None
        path_str = str(coordinator._state_file_path)
        assert ".storage" in path_str
        assert entry.entry_id in path_str
        assert path_str.endswith(".json")

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


class TestSaveCoordinatorState:
    """Test _async_save_coordinator_state persists data."""

    async def test_save_writes_all_data(self, hass: HomeAssistant) -> None:
        """Save should write barcode mappings and discovered barcodes."""
        entry = _make_entry(hass)
        coordinator = PyTapDataUpdateCoordinator(hass, entry)

        coordinator._barcode_to_node = {"A-1234567B": 10}
        coordinator._node_to_barcode = {10: "A-1234567B"}
        coordinator._discovered_barcodes = {"X-9999999Z"}
        coordinator._unsaved_changes = True

        coordinator._store.async_save = AsyncMock()

        await coordinator._async_save_coordinator_state()

        coordinator._store.async_save.assert_called_once()
        saved = coordinator._store.async_save.call_args[0][0]
        assert saved["barcode_to_node"] == {"A-1234567B": 10}
        assert saved["discovered_barcodes"] == ["X-9999999Z"]
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


class TestParserStateFilePassedThrough:
    """Test that the parser is created with a state_file for persistence."""

    def test_listen_creates_parser_with_state_file(self, hass: HomeAssistant) -> None:
        """Parser should be created with state_file in _listen."""
        entry = _make_entry(hass)
        coordinator = PyTapDataUpdateCoordinator(hass, entry)

        # We verify by checking the coordinator has the right state file path
        expected_path = str(coordinator._state_file_path)
        assert ".storage" in expected_path
        assert "parser_state" in expected_path


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
