"""DataUpdateCoordinator for the PyTap integration.

Bridges the blocking pytap parser library into Home Assistant's async event loop.
Uses a background executor thread for streaming data from the Tigo gateway,
and filters events by user-configured barcodes.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST, CONF_PORT
from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import (
    CONF_MODULE_BARCODE,
    CONF_MODULE_NAME,
    CONF_MODULE_STRING,
    CONF_MODULES,
    DEFAULT_PORT,
    DOMAIN,
    RECONNECT_DELAY,
    RECONNECT_RETRIES,
    RECONNECT_TIMEOUT,
)
from .pytap.core.events import (
    Event,
    InfrastructureEvent,
    PowerReportEvent,
    StringEvent,
    TopologyEvent,
)

_LOGGER = logging.getLogger(__name__)

STORE_VERSION = 1
SAVE_DELAY_SECONDS = 10


class PyTapDataUpdateCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Manage streaming data from the Tigo gateway via pytap parser.

    Runs a background listener thread that reads from the TCP/serial source,
    feeds bytes into the parser, and dispatches parsed events to the HA event loop.
    Only events matching user-configured barcodes are stored in coordinator data.
    """

    config_entry: ConfigEntry

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        """Initialize the coordinator."""
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            config_entry=entry,
        )
        self._host: str = entry.data[CONF_HOST]
        self._port: int = entry.data.get(CONF_PORT, DEFAULT_PORT)
        self._modules: list[dict[str, str]] = entry.data.get(CONF_MODULES, [])

        # Build barcode allowlist from configured modules
        self._configured_barcodes: set[str] = {
            m[CONF_MODULE_BARCODE] for m in self._modules if m.get(CONF_MODULE_BARCODE)
        }
        # Module lookup by barcode for name/string metadata
        self._module_lookup: dict[str, dict[str, str]] = {
            m[CONF_MODULE_BARCODE]: m
            for m in self._modules
            if m.get(CONF_MODULE_BARCODE)
        }

        # Barcode ↔ node_id mapping learned from InfrastructureEvents
        self._barcode_to_node: dict[str, int] = {}
        self._node_to_barcode: dict[int, str] = {}

        # Whether the current session has received its first InfrastructureEvent.
        # Until it arrives, stale mappings from the previous session may be
        # incorrect (node-ID reassignment overnight) so fallback resolution
        # is suppressed to avoid mis-routing power reports.
        self._infra_received: bool = False

        # Track barcodes seen on the bus but not in user config
        self._discovered_barcodes: set[str] = set()

        # Listener task handle
        self._listener_task: asyncio.Task | None = None
        self._stop_event = threading.Event()

        # Source handle for cancellation — accessed from both threads
        self._source: Any = None
        self._source_lock = threading.Lock()

        # --- Persistence ---
        # Parser-level state file (gateway identities, versions, node tables)
        self._state_file_path: Path = Path(
            hass.config.path(f".storage/pytap_{entry.entry_id}_parser_state.json")
        )
        # Coordinator-level HA Store (barcode mappings, discovered barcodes)
        self._store: Store = Store(
            hass, STORE_VERSION, f"pytap_{entry.entry_id}_coordinator"
        )
        self._unsaved_changes: bool = False
        self._save_task: asyncio.TimerHandle | None = None

        # Initialize data structure
        self.data: dict[str, Any] = {
            "gateways": {},
            "nodes": {},
            "counters": {},
            "discovered_barcodes": [],
        }

    async def _async_update_data(self) -> dict[str, Any]:
        """Return current data (push-based, no polling needed)."""
        return self.data

    async def async_start_listener(self) -> None:
        """Start the background listener task."""
        await self._async_load_coordinator_state()
        self._stop_event.clear()
        self._listener_task = self.config_entry.async_create_background_task(
            self.hass,
            self._async_listen(),
            name="pytap_listener",
        )

    async def _async_listen(self) -> None:
        """Async wrapper to run the blocking listener in an executor."""
        await self.hass.async_add_executor_job(self._listen)

    async def async_stop_listener(self) -> None:
        """Stop the background listener task."""
        self._stop_event.set()
        # Flush any pending state save
        if self._save_task is not None:
            self._save_task.cancel()
            self._save_task = None
        if self._unsaved_changes:
            await self._async_save_coordinator_state()
        # Close source to unblock the read()/connect() call in the executor
        with self._source_lock:
            if self._source is not None:
                try:
                    self._source.close()
                except Exception:
                    pass
        if self._listener_task is not None:
            self._listener_task.cancel()
            try:
                async with asyncio.timeout(5):
                    await self._listener_task
            except (asyncio.CancelledError, TimeoutError, Exception):
                pass
            self._listener_task = None

    def _listen(self) -> None:
        """Blocking listener loop (runs in executor thread).

        Connects to the Tigo gateway, reads bytes, feeds them into the
        parser, and dispatches events back to the HA event loop.
        """
        from .pytap.api import connect, create_parser

        retries = 0

        while not self._stop_event.is_set():
            parser = create_parser(state_file=str(self._state_file_path))
            self._init_mappings_from_parser(parser)
            self._infra_received = False
            with self._source_lock:
                self._source = None

            try:
                source_config = {"tcp": self._host, "port": self._port}
                source = connect(source_config)
                with self._source_lock:
                    if self._stop_event.is_set():
                        source.close()
                        return
                    self._source = source
                _LOGGER.info(
                    "Connected to Tigo gateway at %s:%s", self._host, self._port
                )
                retries = 0
                last_data_time = time.monotonic()

                while not self._stop_event.is_set():
                    data = self._source.read(4096)
                    if data:
                        last_data_time = time.monotonic()
                        events = parser.feed(data)
                        for event in events:
                            data_changed = self._process_event(event)
                            if data_changed:
                                # Push each event individually to HA
                                self.data["counters"] = parser.counters
                                self.hass.loop.call_soon_threadsafe(
                                    self.async_set_updated_data,
                                    dict(self.data),
                                )
                    elif (
                        RECONNECT_TIMEOUT > 0
                        and (time.monotonic() - last_data_time) > RECONNECT_TIMEOUT
                    ):
                        _LOGGER.warning(
                            "No data from gateway for %ds, reconnecting",
                            RECONNECT_TIMEOUT,
                        )
                        break

            except Exception as err:
                if self._stop_event.is_set():
                    return
                _LOGGER.error("Connection error: %s", err)
            finally:
                with self._source_lock:
                    if self._source is not None:
                        try:
                            self._source.close()
                        except Exception:
                            pass
                        self._source = None

            if self._stop_event.is_set():
                break

            retries += 1
            if RECONNECT_RETRIES > 0 and retries > RECONNECT_RETRIES:
                _LOGGER.error("Max retries (%d) exceeded", RECONNECT_RETRIES)
                return

            _LOGGER.info(
                "Reconnecting in %ds (attempt %d/%s)...",
                RECONNECT_DELAY,
                retries,
                str(RECONNECT_RETRIES) if RECONNECT_RETRIES else "∞",
            )
            # Sleep in small increments so we can respond to stop quickly
            for _ in range(RECONNECT_DELAY * 10):
                if self._stop_event.is_set():
                    return
                time.sleep(0.1)

    def _init_mappings_from_parser(self, parser: Any) -> None:
        """Pre-populate barcode/node mappings from the parser's persistent state.

        Parser state is the freshest source of truth for barcode ↔ node
        relationships.  When the parser has a non-empty node table, its
        mappings fully replace the coordinator's saved ones.  When the
        parser state is empty (e.g. state file was lost or first run),
        the coordinator's saved mappings are preserved so that barcodes
        discovered in earlier sessions survive and can be matched
        immediately when the user adds them as modules.
        """
        try:
            infra = parser.infrastructure
            parser_barcode_to_node: dict[str, int] = {}
            parser_node_to_barcode: dict[int, str] = {}
            for node_id, node_info in infra.get("nodes", {}).items():
                barcode = node_info.get("barcode")
                if barcode:
                    parser_barcode_to_node[barcode] = node_id
                    parser_node_to_barcode[node_id] = barcode

            if parser_barcode_to_node:
                # Parser has data — use it as ground truth
                purged = set(self._barcode_to_node) - set(parser_barcode_to_node)
                self._barcode_to_node = parser_barcode_to_node
                self._node_to_barcode = parser_node_to_barcode
                _LOGGER.info(
                    "Restored %d barcode↔node mappings from parser state%s",
                    len(parser_barcode_to_node),
                    f" (purged {len(purged)} stale)" if purged else "",
                )
            else:
                # Parser state is empty — keep coordinator-saved mappings
                _LOGGER.info(
                    "Parser state has no node table; keeping %d "
                    "coordinator-saved barcode mappings as fallback",
                    len(self._barcode_to_node),
                )
        except Exception:
            _LOGGER.debug("Could not read parser infrastructure for pre-population")

    def _process_event(self, event: Event) -> bool:
        """Process a parsed event, filtering by configured barcodes.

        Returns True if coordinator data was modified (triggers HA update).
        """
        if isinstance(event, PowerReportEvent):
            return self._handle_power_report(event)
        if isinstance(event, InfrastructureEvent):
            return self._handle_infrastructure(event)
        if isinstance(event, TopologyEvent):
            return self._handle_topology(event)
        if isinstance(event, StringEvent):
            _LOGGER.debug(
                "String event (gw=%d, node=%d, %s): %s",
                event.gateway_id,
                event.node_id,
                event.direction,
                event.content,
            )
        return False

    def _handle_power_report(self, event: PowerReportEvent) -> bool:
        """Handle a power report event. Returns True if data was modified."""
        barcode = event.barcode

        # Try to resolve barcode from node_id via the coordinator mapping.
        # Only use the fallback after the *current* session has received its
        # first InfrastructureEvent — before that, the mapping may contain
        # stale entries from a previous session where node-IDs differed.
        if (
            not barcode
            and self._infra_received
            and event.node_id in self._node_to_barcode
        ):
            barcode = self._node_to_barcode[event.node_id]

        if not barcode:
            if not self._infra_received:
                _LOGGER.debug(
                    "Power report for node %d deferred — waiting for first "
                    "infrastructure event this session (gateway=%d)",
                    event.node_id,
                    event.gateway_id,
                )
            else:
                _LOGGER.debug(
                    "Power report for node %d with no barcode yet (gateway=%d)",
                    event.node_id,
                    event.gateway_id,
                )
            return False

        # Check if this barcode is in our configured allowlist
        if barcode not in self._configured_barcodes:
            if barcode not in self._discovered_barcodes:
                self._discovered_barcodes.add(barcode)
                self.data["discovered_barcodes"] = sorted(self._discovered_barcodes)
                self._schedule_save()
                _LOGGER.info(
                    "Discovered unconfigured Tigo optimizer barcode: %s "
                    "(gateway=%d, node=%d). Add it to your PyTap module "
                    "list to start tracking.",
                    barcode,
                    event.gateway_id,
                    event.node_id,
                )
                return True
            return False

        # Get module metadata
        module_meta = self._module_lookup.get(barcode, {})

        self.data["nodes"][barcode] = {
            "gateway_id": event.gateway_id,
            "node_id": event.node_id,
            "barcode": barcode,
            "name": module_meta.get(CONF_MODULE_NAME, barcode),
            "string": module_meta.get(CONF_MODULE_STRING, ""),
            "voltage_in": event.voltage_in,
            "voltage_out": event.voltage_out,
            "current_in": event.current_in,
            "current_out": event.current_out,
            "power": event.power,
            "temperature": event.temperature,
            "dc_dc_duty_cycle": event.dc_dc_duty_cycle,
            "rssi": event.rssi,
            "last_update": datetime.now().isoformat(),
        }
        return True

    def _handle_infrastructure(self, event: InfrastructureEvent) -> bool:
        """Handle an infrastructure event — rebuild gateway/node mappings.

        Each InfrastructureEvent carries the *complete* current node table,
        so mappings are rebuilt from scratch rather than incrementally
        appended.  This purges stale entries that accumulate after overnight
        reconnections, gateway re-enumerations, or node-ID reassignments
        and would otherwise cause incorrect barcode resolution.

        Returns True (always modifies gateway data).
        """
        event_barcodes = [
            n.get("barcode", "?") for n in event.nodes.values() if n.get("barcode")
        ]
        _LOGGER.info(
            "Infrastructure event received: %d gateways, %d nodes (barcodes: %s)",
            len(event.gateways),
            len(event.nodes),
            ", ".join(event_barcodes) or "none",
        )

        first_infra = not self._infra_received
        self._infra_received = True

        # Update gateways
        self.data["gateways"] = event.gateways

        # Rebuild barcode ↔ node_id mappings from scratch
        new_barcode_to_node: dict[str, int] = {}
        new_node_to_barcode: dict[int, str] = {}
        discovered_changed = False
        for node_id, node_info in event.nodes.items():
            barcode = node_info.get("barcode")
            if barcode:
                new_barcode_to_node[barcode] = node_id
                new_node_to_barcode[node_id] = barcode

                # Log discovery of unconfigured barcodes
                if barcode not in self._configured_barcodes:
                    if barcode not in self._discovered_barcodes:
                        self._discovered_barcodes.add(barcode)
                        discovered_changed = True
                        _LOGGER.info(
                            "Discovered unconfigured Tigo optimizer barcode: %s "
                            "(node=%d). Add it to your PyTap module list to "
                            "start tracking.",
                            barcode,
                            node_id,
                        )

        if discovered_changed:
            self.data["discovered_barcodes"] = sorted(self._discovered_barcodes)

        mappings_changed = (
            new_barcode_to_node != self._barcode_to_node
            or new_node_to_barcode != self._node_to_barcode
        )

        if mappings_changed:
            purged_barcodes = set(self._barcode_to_node) - set(new_barcode_to_node)
            if purged_barcodes:
                _LOGGER.info(
                    "Purged %d stale barcode mappings: %s",
                    len(purged_barcodes),
                    purged_barcodes,
                )

        self._barcode_to_node = new_barcode_to_node
        self._node_to_barcode = new_node_to_barcode

        configured_matched = set(new_barcode_to_node) & self._configured_barcodes
        configured_missing = self._configured_barcodes - set(new_barcode_to_node)

        if first_infra:
            if not event_barcodes:
                _LOGGER.info(
                    "First infrastructure event this session — node table not "
                    "yet received. Barcode resolution will activate once the "
                    "gateway sends the full node table. Configured barcodes: %s",
                    ", ".join(sorted(self._configured_barcodes)) or "none",
                )
            else:
                _LOGGER.warning(
                    "First infrastructure event this session — barcode "
                    "resolution now active. %d/%d configured barcodes matched "
                    "in node table.",
                    len(configured_matched),
                    len(self._configured_barcodes),
                )
                if configured_missing:
                    _LOGGER.warning(
                        "Configured barcodes NOT found in node table: %s. "
                        "Check that these barcodes are correct.",
                        ", ".join(sorted(configured_missing)),
                    )
        elif mappings_changed and new_barcode_to_node:
            _LOGGER.warning(
                "Barcode mappings updated — %d/%d configured barcodes now "
                "matched in node table.",
                len(configured_matched),
                len(self._configured_barcodes),
            )
            if configured_missing:
                _LOGGER.warning(
                    "Configured barcodes still NOT found in node table: %s",
                    ", ".join(sorted(configured_missing)),
                )

        if mappings_changed or discovered_changed:
            self._schedule_save()

        return True

    def _handle_topology(self, event: TopologyEvent) -> bool:
        """Handle a topology event for matched nodes.

        Returns True if node data was updated.
        """
        barcode = self._node_to_barcode.get(event.node_id)
        if barcode and barcode in self._configured_barcodes:
            node_data = self.data["nodes"].get(barcode)
            if node_data:
                node_data["topology"] = event.to_dict()
                return True
        return False

    # -------------------------------------------------------------------
    #  Persistence helpers
    # -------------------------------------------------------------------

    async def _async_load_coordinator_state(self) -> None:
        """Load coordinator-level state (barcode mappings, discovered barcodes) from HA Store."""
        try:
            stored = await self._store.async_load()
        except Exception:
            _LOGGER.debug("No stored coordinator state found")
            stored = None

        if stored is None:
            return

        # Restore barcode ↔ node_id mappings
        barcode_to_node = stored.get("barcode_to_node", {})
        for barcode, node_id in barcode_to_node.items():
            self._barcode_to_node[barcode] = int(node_id)
            self._node_to_barcode[int(node_id)] = barcode

        # Restore discovered barcodes
        discovered = stored.get("discovered_barcodes", [])
        self._discovered_barcodes = set(discovered)
        self.data["discovered_barcodes"] = sorted(self._discovered_barcodes)

        _LOGGER.info(
            "Restored coordinator state: %d barcode mappings, %d discovered barcodes",
            len(barcode_to_node),
            len(discovered),
        )

    async def _async_save_coordinator_state(self) -> None:
        """Save coordinator-level state to HA Store."""
        self._unsaved_changes = False
        data = {
            "barcode_to_node": {
                barcode: node_id for barcode, node_id in self._barcode_to_node.items()
            },
            "discovered_barcodes": sorted(self._discovered_barcodes),
        }
        try:
            await self._store.async_save(data)
        except Exception:
            _LOGGER.warning("Failed to save coordinator state")

    def _schedule_save(self) -> None:
        """Schedule a debounced save of coordinator state.

        Safe to call from the executor thread — dispatches to the HA event loop.
        """
        self._unsaved_changes = True

        def _do_schedule() -> None:
            if self._save_task is not None:
                self._save_task.cancel()
            self._save_task = self.hass.loop.call_later(
                SAVE_DELAY_SECONDS,
                lambda: self.hass.async_create_task(
                    self._async_save_coordinator_state()
                ),
            )

        self.hass.loop.call_soon_threadsafe(_do_schedule)

    def reload_modules(self, modules: list[dict[str, str]]) -> None:
        """Reload the module configuration (called from options flow).

        After updating the allowlist, checks whether any newly-configured
        barcodes already have a known node mapping from a previous
        infrastructure event.  If so, a placeholder entry is created in
        coordinator data so sensor entities can bind immediately instead
        of waiting for the next power report.
        """
        old_configured = set(self._configured_barcodes)
        self._modules = modules
        self._configured_barcodes = {
            m[CONF_MODULE_BARCODE] for m in modules if m.get(CONF_MODULE_BARCODE)
        }
        self._module_lookup = {
            m[CONF_MODULE_BARCODE]: m for m in modules if m.get(CONF_MODULE_BARCODE)
        }

        newly_added = self._configured_barcodes - old_configured
        already_resolved = newly_added & set(self._barcode_to_node)
        not_yet_resolved = newly_added - set(self._barcode_to_node)

        # Pre-populate node data for newly added barcodes that already
        # have a known node mapping so sensors can start immediately.
        for barcode in already_resolved:
            if barcode not in self.data["nodes"]:
                module_meta = self._module_lookup.get(barcode, {})
                self.data["nodes"][barcode] = {
                    "gateway_id": None,
                    "node_id": self._barcode_to_node[barcode],
                    "barcode": barcode,
                    "name": module_meta.get(CONF_MODULE_NAME, barcode),
                    "string": module_meta.get(CONF_MODULE_STRING, ""),
                    "voltage_in": None,
                    "voltage_out": None,
                    "current_in": None,
                    "current_out": None,
                    "power": None,
                    "temperature": None,
                    "dc_dc_duty_cycle": None,
                    "rssi": None,
                    "last_update": None,
                }

        _LOGGER.info(
            "Reloaded module config: tracking %d barcodes",
            len(self._configured_barcodes),
        )
        if already_resolved:
            _LOGGER.info(
                "Newly added barcodes already resolved from saved mappings: %s",
                ", ".join(sorted(already_resolved)),
            )
        if not_yet_resolved:
            _LOGGER.info(
                "Newly added barcodes not yet in node table (will resolve "
                "on next infrastructure event): %s",
                ", ".join(sorted(not_yet_resolved)),
            )
