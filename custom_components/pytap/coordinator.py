"""DataUpdateCoordinator for the PyTap integration.

Bridges the blocking pytap parser library into Home Assistant's async event loop.
Uses a background executor thread for streaming data from the Tigo gateway,
and filters events by user-configured barcodes.
"""

from __future__ import annotations

import asyncio
from datetime import datetime
import logging
import threading
import time
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST, CONF_PORT
from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from homeassistant.util import dt as dt_util

from .const import (
    CONF_MODULE_BARCODE,
    CONF_MODULE_NAME,
    CONF_MODULE_PEAK_POWER,
    CONF_MODULE_STRING,
    CONF_MODULES,
    DEFAULT_PEAK_POWER,
    DEFAULT_PORT,
    DOMAIN,
    ENERGY_GAP_THRESHOLD_SECONDS,
    ENERGY_LOW_POWER_THRESHOLD_W,
    RECONNECT_DELAY,
    RECONNECT_RETRIES,
    RECONNECT_TIMEOUT,
)
from .energy import EnergyAccumulator, accumulate_energy
from .pytap.core.events import (
    Event,
    InfrastructureEvent,
    PowerReportEvent,
    StringEvent,
    TopologyEvent,
)
from .pytap.core.state import PersistentState

_LOGGER = logging.getLogger(__name__)

STORE_VERSION = 2
SAVE_DELAY_SECONDS = 10


class _MigratingStore(Store):
    """Store subclass with explicit migration support.

    The default Store raises NotImplementedError on major-version
    mismatches, which silently drops all persisted state.  This subclass
    returns old data as-is because all format changes between v1 and v2
    are backward-compatible (the ``energy_data`` key was added and the
    load code already handles its absence via ``.get()``).
    """

    async def _async_migrate_func(
        self,
        old_major_version: int,
        old_minor_version: int,
        old_data: dict,
    ) -> dict:
        """Migrate v1 → v2: return data as-is (format is backward-compatible)."""
        return old_data


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
        self._modules: list[dict[str, Any]] = entry.data.get(CONF_MODULES, [])

        # Build barcode allowlist from configured modules
        self._configured_barcodes: set[str] = {
            m[CONF_MODULE_BARCODE] for m in self._modules if m.get(CONF_MODULE_BARCODE)
        }
        # Module lookup by barcode for name/string metadata
        self._module_lookup: dict[str, dict[str, Any]] = {
            m[CONF_MODULE_BARCODE]: m
            for m in self._modules
            if m.get(CONF_MODULE_BARCODE)
        }

        # Barcode ↔ node_id mapping learned from InfrastructureEvents
        self._barcode_to_node: dict[str, int] = {}
        self._node_to_barcode: dict[int, str] = {}

        # Whether the current session has received an InfrastructureEvent
        # that includes a non-empty node table.  Until a node table arrives,
        # stale mappings from the previous session may be incorrect (node-ID
        # reassignment overnight) so fallback resolution is suppressed to
        # avoid mis-routing power reports.
        self._infra_received: bool = False

        # Track barcodes seen on the bus but not in user config
        self._discovered_barcodes: set[str] = set()

        # Counter for power reports dropped because barcode could not be
        # resolved.  Used to emit periodic INFO-level messages so operators
        # know data IS flowing but resolution is blocked.
        self._pending_power_reports: int = 0

        # Listener task handle
        self._listener_task: asyncio.Task | None = None
        self._stop_event = threading.Event()

        # Source handle for cancellation — accessed from both threads
        self._source: Any = None
        self._source_lock = threading.Lock()

        # --- Persistence (single HA Store for all state) ---
        self._store = _MigratingStore(
            hass, STORE_VERSION, f"pytap_{entry.entry_id}_coordinator"
        )
        self._unsaved_changes: bool = False
        self._save_task: asyncio.TimerHandle | None = None

        # Parser infrastructure state — shared between coordinator and parser
        self._persistent_state: PersistentState = PersistentState()

        # Per-module energy accumulation state (persisted)
        self._energy_state: dict[str, EnergyAccumulator] = {}

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

    def get_diagnostics_data(self) -> dict[str, Any]:
        """Return coordinator diagnostics snapshot."""
        return {
            "node_mappings": {
                "barcode_to_node": dict(self._barcode_to_node),
                "node_to_barcode": {
                    str(node_id): barcode
                    for node_id, barcode in self._node_to_barcode.items()
                },
            },
            "connection_state": {
                "infra_received": self._infra_received,
                "pending_power_reports": self._pending_power_reports,
                "host": self._host,
                "port": self._port,
            },
            "energy_state": {
                barcode: {
                    "daily_energy_wh": round(acc.daily_energy_wh, 2),
                    "total_energy_wh": round(acc.total_energy_wh, 2),
                    "daily_reset_date": acc.daily_reset_date,
                    "last_power_w": acc.last_power_w,
                    "last_reading_ts": (
                        acc.last_reading_ts.isoformat()
                        if acc.last_reading_ts is not None
                        else None
                    ),
                    "readings_today": acc.readings_today,
                }
                for barcode, acc in self._energy_state.items()
            },
        }

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
            parser = create_parser(persistent_state=self._persistent_state)
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
        # Only use the fallback after the *current* session has received an
        # InfrastructureEvent with a non-empty node table — before that,
        # the mapping may contain stale entries from a previous session
        # where node-IDs differed.
        if (
            not barcode
            and self._infra_received
            and event.node_id in self._node_to_barcode
        ):
            barcode = self._node_to_barcode[event.node_id]

        if not barcode:
            self._pending_power_reports += 1
            if not self._infra_received:
                # Log at INFO every 50 reports so operator sees data is flowing
                if self._pending_power_reports == 1:
                    _LOGGER.info(
                        "Power report for node %d — waiting for node table "
                        "before barcode resolution (gateway=%d)",
                        event.node_id,
                        event.gateway_id,
                    )
                elif self._pending_power_reports % 50 == 0:
                    _LOGGER.info(
                        "%d power reports received but barcode resolution "
                        "still pending — waiting for gateway to send "
                        "the full node table",
                        self._pending_power_reports,
                    )
                else:
                    _LOGGER.debug(
                        "Power report for node %d deferred — waiting for "
                        "node table (gateway=%d) [%d pending]",
                        event.node_id,
                        event.gateway_id,
                        self._pending_power_reports,
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
        peak_power_raw = module_meta.get(CONF_MODULE_PEAK_POWER, DEFAULT_PEAK_POWER)
        try:
            peak_power = int(peak_power_raw)
        except (TypeError, ValueError):
            peak_power = DEFAULT_PEAK_POWER
        if peak_power <= 0:
            peak_power = DEFAULT_PEAK_POWER

        performance: float | None = None
        if event.power is not None:
            performance = (max(event.power, 0.0) / peak_power) * 100.0
        now = dt_util.now()

        acc = self._energy_state.setdefault(
            barcode,
            EnergyAccumulator(daily_reset_date=now.date().isoformat()),
        )
        update_result = accumulate_energy(
            acc,
            power=event.power,
            now=now,
            gap_threshold=ENERGY_GAP_THRESHOLD_SECONDS,
            low_power_threshold=ENERGY_LOW_POWER_THRESHOLD_W,
        )
        if update_result.discarded_gap_during_production:
            _LOGGER.debug(
                "Discarded energy trapezoid for %s due to long gap during production",
                barcode,
            )

        self.data["nodes"][barcode] = {
            "gateway_id": event.gateway_id,
            "node_id": event.node_id,
            "barcode": barcode,
            "name": module_meta.get(CONF_MODULE_NAME, barcode),
            "string": module_meta.get(CONF_MODULE_STRING, ""),
            "peak_power": peak_power,
            "voltage_in": event.voltage_in,
            "voltage_out": event.voltage_out,
            "current_in": event.current_in,
            "current_out": event.current_out,
            "power": event.power,
            "performance": round(performance, 2) if performance is not None else None,
            "temperature": event.temperature,
            "dc_dc_duty_cycle": event.dc_dc_duty_cycle,
            "rssi": event.rssi,
            "daily_energy_wh": round(acc.daily_energy_wh, 2),
            "total_energy_wh": round(acc.total_energy_wh, 2),
            "readings_today": acc.readings_today,
            "daily_reset_date": acc.daily_reset_date,
            "last_update": now.isoformat(),
        }
        self._schedule_save()
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
        has_nodes = bool(event.nodes)
        _LOGGER.info(
            "Infrastructure event received: %d gateways, %d nodes (barcodes: %s)",
            len(event.gateways),
            len(event.nodes),
            ", ".join(event_barcodes) or "none",
        )

        # Only treat _infra_received as True once we have an event that
        # actually contains a node table.  Gateway-only events (0 nodes)
        # arrive early in the session before the gateway has sent the
        # full node table, and should not be treated as authoritative.
        first_infra_with_nodes = not self._infra_received and has_nodes
        if has_nodes:
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

        # When the event has no nodes, preserve existing coordinator
        # mappings — they may have been loaded from saved state and are
        # better than nothing until a real node table replaces them.
        if not has_nodes:
            _LOGGER.info(
                "Infrastructure event has no node table — keeping %d "
                "existing barcode mappings as fallback",
                len(self._barcode_to_node),
            )
            # Still schedule a save for gateway data changes
            if event.gateways:
                self._schedule_save()
            return True

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

        # Reset pending counter now that resolution is possible
        if self._pending_power_reports > 0:
            _LOGGER.info(
                "Node table received — %d power reports were pending "
                "barcode resolution",
                self._pending_power_reports,
            )
            self._pending_power_reports = 0

        configured_matched = set(new_barcode_to_node) & self._configured_barcodes
        configured_missing = self._configured_barcodes - set(new_barcode_to_node)

        if first_infra_with_nodes:
            _LOGGER.warning(
                "First node table this session — barcode "
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
        """Load all persisted state (barcode mappings, discovered barcodes, parser state) from HA Store."""
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

        # Restore parser infrastructure state
        parser_state_data = stored.get("parser_state")
        if parser_state_data:
            try:
                self._persistent_state = PersistentState.from_dict(parser_state_data)
            except Exception:
                _LOGGER.warning(
                    "Failed to restore parser state from store, starting fresh"
                )
                self._persistent_state = PersistentState()

        # Restore energy accumulation state
        today = dt_util.now().date().isoformat()
        for barcode, energy_data in stored.get("energy_data", {}).items():
            last_reading_ts = None
            if raw_ts := energy_data.get("last_reading_ts"):
                try:
                    last_reading_ts = datetime.fromisoformat(raw_ts)
                except (TypeError, ValueError):
                    last_reading_ts = None

            daily_reset_date = str(energy_data.get("daily_reset_date", ""))
            daily_energy_wh = float(energy_data.get("daily_energy_wh", 0.0))
            readings_today = int(energy_data.get("readings_today", 0))
            if daily_reset_date != today:
                daily_reset_date = today
                daily_energy_wh = 0.0
                readings_today = 0

            self._energy_state[barcode] = EnergyAccumulator(
                daily_energy_wh=daily_energy_wh,
                total_energy_wh=float(energy_data.get("total_energy_wh", 0.0)),
                daily_reset_date=daily_reset_date,
                last_power_w=float(energy_data.get("last_power_w", 0.0)),
                last_reading_ts=last_reading_ts,
                readings_today=readings_today,
            )

        # Pre-populate coordinator.data["nodes"] for configured barcodes
        # that have persisted energy state.  This makes energy values
        # available to sensors immediately on startup instead of waiting
        # for the first live power report (which would otherwise cause a
        # visible drop while RestoreSensor is the only fallback).
        for barcode in self._configured_barcodes:
            acc = self._energy_state.get(barcode)
            if acc is None:
                continue
            module_meta = self._module_lookup.get(barcode, {})
            node_id = self._barcode_to_node.get(barcode)
            self.data["nodes"][barcode] = {
                "gateway_id": None,
                "node_id": node_id,
                "barcode": barcode,
                "name": module_meta.get(CONF_MODULE_NAME, barcode),
                "string": module_meta.get(CONF_MODULE_STRING, ""),
                "peak_power": module_meta.get(
                    CONF_MODULE_PEAK_POWER, DEFAULT_PEAK_POWER
                ),
                "voltage_in": None,
                "voltage_out": None,
                "current_in": None,
                "current_out": None,
                "power": None,
                "performance": None,
                "temperature": None,
                "dc_dc_duty_cycle": None,
                "rssi": None,
                "daily_energy_wh": round(acc.daily_energy_wh, 2),
                "total_energy_wh": round(acc.total_energy_wh, 2),
                "readings_today": acc.readings_today,
                "daily_reset_date": acc.daily_reset_date,
                "last_update": None,
            }

        _LOGGER.info(
            "Restored coordinator state: %d barcode mappings, %d discovered barcodes, "
            "%d gateway identities, %d energy states",
            len(barcode_to_node),
            len(discovered),
            len(self._persistent_state.gateway_identities),
            len(self._energy_state),
        )

    async def _async_save_coordinator_state(self) -> None:
        """Save all state (barcode mappings, discovered barcodes, parser state) to HA Store."""
        self._unsaved_changes = False
        data = {
            "barcode_to_node": {
                barcode: node_id for barcode, node_id in self._barcode_to_node.items()
            },
            "discovered_barcodes": sorted(self._discovered_barcodes),
            "parser_state": self._persistent_state.to_dict(),
            "energy_data": {
                barcode: {
                    "daily_energy_wh": acc.daily_energy_wh,
                    "daily_reset_date": acc.daily_reset_date,
                    "total_energy_wh": acc.total_energy_wh,
                    "readings_today": acc.readings_today,
                    "last_power_w": acc.last_power_w,
                    "last_reading_ts": (
                        acc.last_reading_ts.isoformat()
                        if acc.last_reading_ts is not None
                        else None
                    ),
                }
                for barcode, acc in self._energy_state.items()
            },
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

    def reload_modules(self, modules: list[dict[str, Any]]) -> None:
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
                now = dt_util.now()
                acc = self._energy_state.setdefault(
                    barcode,
                    EnergyAccumulator(daily_reset_date=now.date().isoformat()),
                )
                self.data["nodes"][barcode] = {
                    "gateway_id": None,
                    "node_id": self._barcode_to_node[barcode],
                    "barcode": barcode,
                    "name": module_meta.get(CONF_MODULE_NAME, barcode),
                    "string": module_meta.get(CONF_MODULE_STRING, ""),
                    "peak_power": module_meta.get(
                        CONF_MODULE_PEAK_POWER, DEFAULT_PEAK_POWER
                    ),
                    "voltage_in": None,
                    "voltage_out": None,
                    "current_in": None,
                    "current_out": None,
                    "power": None,
                    "performance": None,
                    "temperature": None,
                    "dc_dc_duty_cycle": None,
                    "rssi": None,
                    "daily_energy_wh": round(acc.daily_energy_wh, 2),
                    "total_energy_wh": round(acc.total_energy_wh, 2),
                    "readings_today": acc.readings_today,
                    "daily_reset_date": acc.daily_reset_date,
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
