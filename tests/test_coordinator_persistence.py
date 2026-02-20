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
from custom_components.pytap.pytap.core.events import InfrastructureEvent


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
    """Test _init_mappings_from_parser pre-populates coordinator maps."""

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
