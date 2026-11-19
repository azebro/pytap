"""DataUpdateCoordinator for the PyTap integration.

Bridges the blocking pytap parser library into Home Assistant's async event loop.
Uses a background executor thread for streaming data from the Tigo gateway,
and filters events by user-configured barcodes.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
import logging
from pathlib import Path
import threading
import time
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import __version__ as HA_VERSION
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.helpers.event import async_track_time_change, async_track_time_interval
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from homeassistant.util import dt as dt_util

from .capture_services import capture_job_running, track_capture_job
from .connection import normalize_connection, open_source
from .const import (
    CONF_AUTO_DISCOVERY,
    CONF_BAUD_RATE,
    CONF_CONNECTION_TYPE,
    CONF_EXCLUDED_BARCODES,
    CONF_LIVE_REPORTING,
    CONF_MODULE_BARCODE,
    CONF_MODULE_NAME,
    CONF_MODULE_PEAK_POWER,
    CONF_MODULE_STRING,
    CONF_MODULES,
    CONF_STALE_ACTION,
    CONF_STALE_TIMEOUT,
    CONF_WRITE_INTERVAL,
    DEFAULT_DISCOVERED_STRING,
    DEFAULT_LIVE_REPORTING,
    DEFAULT_PEAK_POWER,
    DEFAULT_STALE_ACTION,
    DEFAULT_STALE_TIMEOUT,
    DEFAULT_WRITE_INTERVAL,
    DOMAIN,
    ENERGY_GAP_THRESHOLD_SECONDS,
    ENERGY_LOW_POWER_THRESHOLD_W,
    FRESHNESS_CHECK_INTERVAL_SECONDS,
    INTEGRATION_VERSION,
    LISTENER_STOP_TIMEOUT,
    MAX_STALE_TIMEOUT,
    MIN_STALE_TIMEOUT,
    RECONNECT_DELAY,
    RECONNECT_RETRIES,
    RECONNECT_TIMEOUT,
    STALE_ACTIONS,
)
from .energy import EnergyAccumulator, accumulate_energy
from .pytap.core.capture import CaptureWriter, capture_directory, export_archive, list_captures, read_manifest
from .pytap.core.events import (
    Event,
    InfrastructureEvent,
    PowerReportEvent,
    StringEvent,
    TopologyEvent,
)
from .pytap.core.state import PersistentState

_LOGGER = logging.getLogger(__name__)

type PyTapConfigEntry = ConfigEntry[PyTapDataUpdateCoordinator]

STORE_VERSION = 2
SAVE_DELAY_SECONDS = 10

# Numeric node fields that are averaged over the write interval before being
# pushed to Home Assistant.  Averaging prevents the HA database from receiving
# every raw reading while still giving a representative value per interval.
_AVERAGED_FIELDS = (
    "voltage_in",
    "voltage_out",
    "current_in",
    "current_out",
    "power",
    "temperature",
    "dc_dc_duty_cycle",
    "rssi",
)

# Persisted per-node fields, restored on startup (so "hold" can show them
# after a restart). Report timing (report_monotonic) is session-only.
_SNAPSHOT_FIELDS = (
    "gateway_id",
    "node_id",
    "voltage_in",
    "voltage_out",
    "current_in",
    "current_out",
    "power",
    "performance",
    "temperature",
    "dc_dc_duty_cycle",
    "rssi",
    "daily_energy_wh",
    "total_energy_wh",
    "readings_today",
    "daily_reset_date",
    "last_update",
    "topology",
)


def _daily_reset_fields(today: str) -> dict[str, Any]:
    return {"daily_energy_wh": 0.0, "readings_today": 0, "daily_reset_date": today}


class BarcodeClaims:
    """Domain-wide optimizer ownership, so two entries never track one barcode.

    Entity unique IDs are barcode-only; claims are atomic across worker threads.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._owners: dict[str, str] = {}

    def claim(self, barcode: str, entry_id: str) -> bool:
        """Claim ``barcode`` for ``entry_id``; True if it now (or already) owns it."""
        with self._lock:
            return self._owners.setdefault(barcode, entry_id) == entry_id

    def release_entry(self, entry_id: str) -> None:
        """Release every barcode claimed by ``entry_id``."""
        with self._lock:
            self._owners = {b: owner for b, owner in self._owners.items() if owner != entry_id}


BARCODE_CLAIMS_KEY = f"{DOMAIN}_barcode_claims"


def barcode_claims(hass: HomeAssistant) -> BarcodeClaims:
    """The shared claim map; create it on the event loop before any worker starts."""
    return hass.data.setdefault(BARCODE_CLAIMS_KEY, BarcodeClaims())


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


def coordinator_store(hass: HomeAssistant, entry_id: str) -> Store:
    """The single persisted state file for one config entry."""
    return _MigratingStore(hass, STORE_VERSION, f"pytap_{entry_id}_coordinator")


def parked_coordinators(hass: HomeAssistant) -> dict[str, PyTapDataUpdateCoordinator]:
    """Coordinators whose listener outlived its stop, by entry ID (never live ones).

    Their worker may still hold the port until it exits.
    """
    return hass.data.setdefault(DOMAIN, {})


class PyTapDataUpdateCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Manage streaming data from the Tigo gateway via pytap parser.

    Runs a background listener thread that reads from the TCP/serial source,
    feeds bytes into the parser, and dispatches parsed events to the HA event loop.
    Stores user-selected optimizers and, when enabled, newly discovered ones.
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
        self._connection = normalize_connection(entry.data)
        self._connected = False
        self._retries = 0
        self._modules: list[dict[str, Any]] = [dict(m) for m in entry.data.get(CONF_MODULES, [])]
        self._auto_discovery: bool = entry.data.get(CONF_AUTO_DISCOVERY, False)
        self._excluded_barcodes: set[str] = set(entry.data.get(CONF_EXCLUDED_BARCODES, []))
        self._auto_modules: dict[str, dict[str, Any]] = {}
        self._live_reporting: bool = entry.data.get(
            CONF_LIVE_REPORTING, DEFAULT_LIVE_REPORTING
        )
        raw_write_interval = entry.data.get(CONF_WRITE_INTERVAL, DEFAULT_WRITE_INTERVAL)
        try:
            self._write_interval: float = float(raw_write_interval)
        except (TypeError, ValueError):
            self._write_interval = float(DEFAULT_WRITE_INTERVAL)
        self._write_interval = max(0.0, self._write_interval)
        stale_action = entry.data.get(CONF_STALE_ACTION, DEFAULT_STALE_ACTION)
        self._stale_action: str = stale_action if stale_action in STALE_ACTIONS else DEFAULT_STALE_ACTION
        try:
            stale_timeout = int(entry.data.get(CONF_STALE_TIMEOUT, DEFAULT_STALE_TIMEOUT))
        except (TypeError, ValueError):
            stale_timeout = DEFAULT_STALE_TIMEOUT
        self._stale_timeout: int = max(MIN_STALE_TIMEOUT, min(MAX_STALE_TIMEOUT, stale_timeout))

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
        # Barcodes skipped because another PyTap entry owns them (logged once).
        self._foreign_barcodes: set[str] = set()
        # Created here, on the loop, so workers only ever claim atomically.
        self._claims = barcode_claims(hass)
        for barcode in sorted(self._configured_barcodes):
            if not self._claims.claim(barcode, entry.entry_id):
                _LOGGER.warning("Optimizer %s is configured here but another PyTap entry tracks it", barcode)

        # Counter for power reports dropped because barcode could not be
        # resolved.  Used to emit periodic INFO-level messages so operators
        # know data IS flowing but resolution is blocked.
        self._pending_power_reports: int = 0

        # Listener task handle
        self._listener_task: asyncio.Task | None = None
        self._worker_future: asyncio.Future | None = None
        self._stop_lock = asyncio.Lock()
        self._stop_event = threading.Event()

        # Write-interval throttle state (accessed only from the listener thread)
        # Only used when live_reporting is False.
        self._last_ha_update: float = 0.0
        self._ha_update_pending: bool = False
        # Per-barcode buffer of timestamped numeric readings accumulated
        # between HA writes.  Each entry is (monotonic_ts, field_dict).
        # Flushed (time-weighted averaged) each time the write interval fires.
        self._reading_buffers: dict[str, list[tuple[float, dict[str, Any]]]] = {}

        # Midnight reset subscription
        self._midnight_reset_unsub: CALLBACK_TYPE | None = None

        # Periodic check that marks panels unavailable once reports stop.
        self._freshness_unsub: CALLBACK_TYPE | None = None
        self._reporting_barcodes: frozenset[str] = frozenset()
        # Per-barcode entity callbacks re-rendered when that panel's freshness
        # changes, keyed by each registration's unsubscribe function.
        self._freshness_listeners: dict[str, dict[CALLBACK_TYPE, CALLBACK_TYPE]] = {}
        # Per-barcode entity callbacks re-rendered when that panel's data changes.
        self._data_listeners: dict[str, dict[CALLBACK_TYPE, CALLBACK_TYPE]] = {}
        # Worker side (under ``_state_lock``): panels changed since the last
        # publish, or a global change that needs every listener.
        self._changed_barcodes: set[str] = set()
        self._full_update_pending: bool = False
        # Loop side: changes of snapshots dropped as older than a midnight reset.
        self._missed_barcodes: set[str] = set()
        self._missed_full_update: bool = False
        # Snapshot sequence (taken under ``_state_lock``) and the last applied one.
        self._snapshot_sequence: int = 0
        self._applied_sequence: int = 0
        # Parser whose counters the disconnect flush publishes.
        self._counter_source: Any = None

        # Active source handle; the worker owns open/read/close.
        self._source: Any = None
        self._source_lock = threading.Lock()
        self._connection_generation = 0
        self._capture: CaptureWriter | None = None
        self._capture_lock = asyncio.Lock()
        self._capture_start_future: asyncio.Future | None = None
        self._capture_export_future: asyncio.Future | None = None
        self._capture_root = Path(hass.config.path("pytap_captures", entry.entry_id))

        # --- Persistence (single HA Store for all state) ---
        self._store = coordinator_store(hass, entry.entry_id)
        self._unsaved_changes: bool = False
        self._save_task: asyncio.TimerHandle | None = None
        # Worker-side: a save is already requested from the loop this cycle.
        self._save_requested: bool = False

        # Parser infrastructure state — shared between coordinator and parser
        self._persistent_state: PersistentState = PersistentState()

        # Per-module energy accumulation state (persisted)
        self._energy_state: dict[str, EnergyAccumulator] = {}
        self._store_loaded = False
        # The Store held saved state (not a first run or a renamed corrupt file).
        self._store_restored = False
        # Never write after a failed load (that would replace saved totals with
        # empty state) or once the entry is being removed.
        self._save_blocked = False

        # The listener thread owns ``_state``; Home Assistant only ever sees
        # copies published to ``data`` on its event loop. ``_state_lock``
        # guards ``_state``/``_energy_state`` against loop-side readers.
        self._state_lock = threading.RLock()
        self._state: dict[str, Any] = {
            "gateways": {},
            "nodes": {},
            "counters": {},
            "discovered_barcodes": [],
        }
        self.data: dict[str, Any] = self._snapshot()

    def _snapshot(self) -> dict[str, Any]:
        """Copy the worker state for publication; node dicts are never shared.

        Each snapshot carries a sequence so one taken before a midnight reset
        can't overwrite the reset once it reaches the loop.
        """
        with self._state_lock:
            self._snapshot_sequence += 1
            return {
                **self._state,
                "gateways": dict(self._state["gateways"]),
                "nodes": {barcode: dict(node) for barcode, node in self._state["nodes"].items()},
                "counters": dict(self._state["counters"]),
                "discovered_barcodes": list(self._state["discovered_barcodes"]),
                "sequence": self._snapshot_sequence,
            }

    def _mark_changed(self, barcode: str) -> None:
        """Record a panel whose entities must re-render (``_state_lock`` held)."""
        self._changed_barcodes.add(barcode)

    def _mark_full_update(self) -> None:
        """Record a global change that re-renders every listener (``_state_lock`` held)."""
        self._full_update_pending = True

    def _publish(self, snapshot: dict[str, Any]) -> None:
        """Hand a snapshot and the panels changed since the last one to the loop.

        Only the worker publishes, so the changes recorded so far are exactly
        those contained in ``snapshot``.
        """
        with self._state_lock:
            changed = None if self._full_update_pending else frozenset(self._changed_barcodes)
            self._changed_barcodes.clear()
            self._full_update_pending = False
        self.hass.loop.call_soon_threadsafe(self.async_set_updated_data, snapshot, changed)

    async def _async_update_data(self) -> dict[str, Any]:
        """Return current data (push-based, no polling needed)."""
        return self.data

    @property
    def modules(self) -> list[dict[str, Any]]:
        """A snapshot of manual and discovered module metadata for HA's loop."""
        return [dict(module) for module in list(self._modules)]

    @property
    def auto_discovery(self) -> bool:
        """Whether new optimizer barcodes are admitted automatically."""
        return self._auto_discovery

    @property
    def module_count(self) -> int:
        """Cheap catalogue size so platforms can skip unchanged discoveries."""
        return len(self._configured_barcodes)

    def module_peak_power(self, barcode: str) -> int:
        """Read the immutable catalogue rating without copying every module."""
        value = self._module_lookup.get(barcode, {}).get(CONF_MODULE_PEAK_POWER, DEFAULT_PEAK_POWER)
        try:
            return max(0, int(value))
        except (TypeError, ValueError):
            return 0

    @property
    def store_loaded(self) -> bool:
        """The Store was read without error (it may have been empty)."""
        return self._store_loaded

    @property
    def store_restored(self) -> bool:
        """Saved state, including discovered modules, was actually restored."""
        return self._store_restored

    @property
    def connection(self) -> dict[str, Any]:
        """Normalised settings of the endpoint this coordinator's worker opens."""
        return dict(self._connection)

    @property
    def stale_action(self) -> str:
        """What live readings do once a panel stops reporting."""
        return self._stale_action

    @property
    def stale_timeout(self) -> int:
        """Seconds without a valid report before a panel counts as stale."""
        return self._stale_timeout

    def seconds_since_report(self, barcode: str) -> float | None:
        """Seconds since the published data's last valid report, if any this session."""
        node = self.data.get("nodes", {}).get(barcode)
        reported = node.get("report_monotonic") if node else None
        return None if reported is None else time.monotonic() - reported

    def is_reporting(self, barcode: str) -> bool:
        """Whether live readings are current (a valid report within the timeout).

        Averaged mode publishes at most once per interval, so the interval is
        added to the allowance; otherwise panels would flap between flushes.
        """
        age = self.seconds_since_report(barcode)
        allowance = self._stale_timeout + (0 if self._live_reporting else self._write_interval)
        return age is not None and age < allowance

    def _current_reporting(self) -> frozenset[str]:
        return frozenset(
            barcode for barcode in list(self.data.get("nodes", {})) if self.is_reporting(barcode)
        )

    @callback
    def async_set_updated_data(self, data: dict[str, Any], changed: frozenset[str] | None = None) -> None:
        """Publish data and re-render the entities it affects.

        ``changed`` lists the panels whose data changed; only their entities
        and the aggregates containing them are notified. ``None`` is a global
        change (e.g. new modules for the platforms to add): every listener runs.
        Snapshots older than the last applied one (a midnight reset) are dropped.
        """
        sequence = data.get("sequence")
        if isinstance(sequence, int):
            if sequence < self._applied_sequence:
                # The next snapshot contains these changes; notify them then.
                if changed is None:
                    self._missed_full_update = True
                else:
                    self._missed_barcodes |= changed
                return
            self._applied_sequence = sequence
        if changed is not None and self._missed_full_update:
            changed = None
        self._missed_full_update = False
        if changed is None:
            self._missed_barcodes.clear()
            super().async_set_updated_data(data)
            self._reporting_barcodes = self._current_reporting()
            return
        if self._missed_barcodes:
            changed = changed | self._missed_barcodes
            self._missed_barcodes.clear()
        self.data = data
        self.last_update_success = True
        if not changed:
            return
        # Time-based transitions of other panels are left to the freshness check.
        self._reporting_barcodes = (self._reporting_barcodes - changed) | {
            barcode for barcode in changed if self.is_reporting(barcode)
        }
        self._notify_barcode_listeners(self._data_listeners, changed)

    @staticmethod
    def _add_barcode_listener(
        registry: dict[str, dict[CALLBACK_TYPE, CALLBACK_TYPE]],
        barcodes: list[str],
        update_callback: CALLBACK_TYPE,
    ) -> CALLBACK_TYPE:
        watched = frozenset(barcodes)

        @callback
        def remove_listener() -> None:
            for barcode in watched:
                if (listeners := registry.get(barcode)) is not None:
                    listeners.pop(remove_listener, None)
                    if not listeners:
                        del registry[barcode]

        for barcode in watched:
            registry.setdefault(barcode, {})[remove_listener] = update_callback
        return remove_listener

    def _notify_barcode_listeners(
        self, registry: dict[str, dict[CALLBACK_TYPE, CALLBACK_TYPE]], barcodes: frozenset[str]
    ) -> None:
        # One call per registration, even if several of its panels changed.
        affected: dict[CALLBACK_TYPE, CALLBACK_TYPE] = {}
        for barcode in barcodes:
            affected.update(registry.get(barcode, {}))
        for update_callback in affected.values():
            try:
                update_callback()
            except Exception:
                _LOGGER.exception("Error updating PyTap entity")

    @callback
    def async_add_freshness_listener(
        self, barcodes: list[str], update_callback: CALLBACK_TYPE
    ) -> CALLBACK_TYPE:
        """Call ``update_callback`` when any of ``barcodes`` starts or stops reporting."""
        return self._add_barcode_listener(self._freshness_listeners, barcodes, update_callback)

    @callback
    def async_add_data_listener(self, barcodes: list[str], update_callback: CALLBACK_TYPE) -> CALLBACK_TYPE:
        """Call ``update_callback`` when published data of any of ``barcodes`` changes.

        Global updates still reach entities through their coordinator listener.
        """
        return self._add_barcode_listener(self._data_listeners, barcodes, update_callback)

    @callback
    def _async_check_freshness(self, _now: datetime | None = None) -> None:
        """Re-render only entities of panels that started or stopped reporting."""
        reporting = self._current_reporting()
        changed = reporting ^ self._reporting_barcodes
        if not changed:
            return
        self._reporting_barcodes = reporting
        self._notify_barcode_listeners(self._freshness_listeners, changed)

    def _claim_barcode(self, barcode: str) -> bool:
        """Atomically claim an optimizer unless another PyTap entry tracks it."""
        return self._claims.claim(barcode, self.config_entry.entry_id)

    def release_barcode_claims(self) -> None:
        """Let other entries track this entry's optimizers (on unload or removal)."""
        self._claims.release_entry(self.config_entry.entry_id)

    def _add_discovered_module(
        self, barcode: str, node_id: int | None = None, restored: dict[str, Any] | None = None
    ) -> bool:
        """Admit one optimizer without config-entry writes or listener reloads.

        Called by the sole parser worker, or during Store loading before it
        starts. Known auto modules remain tracked if discovery is later disabled.
        Explicit configured metadata always wins over saved discovery defaults.
        """
        if barcode in self._configured_barcodes or barcode in self._excluded_barcodes:
            return False
        if not self._auto_discovery and restored is None:
            return False
        if not self._claim_barcode(barcode):
            if barcode not in self._foreign_barcodes:
                self._foreign_barcodes.add(barcode)
                _LOGGER.info("Not tracking %s: another PyTap entry already tracks it", barcode)
            return False
        module = {
            CONF_MODULE_BARCODE: barcode,
            CONF_MODULE_NAME: f"Optimizer {barcode}",
            CONF_MODULE_STRING: DEFAULT_DISCOVERED_STRING,
            # Rated panel power is not broadcast by the optimizer. Zero means
            # unknown; performance is unavailable until the user supplies Wp.
            CONF_MODULE_PEAK_POWER: 0,
        }
        if restored:
            for field in (CONF_MODULE_NAME, CONF_MODULE_STRING):
                value = restored.get(field)
                if isinstance(value, str) and value.strip():
                    module[field] = value
            rating = restored.get(CONF_MODULE_PEAK_POWER)
            if type(rating) is int and 0 <= rating <= 1000:
                module[CONF_MODULE_PEAK_POWER] = rating
        self._auto_modules[barcode] = module
        self._modules.append(module)
        self._module_lookup[barcode] = module
        self._configured_barcodes.add(barcode)
        payload = self._build_node_payload(barcode, module, node_id)
        if acc := self._energy_state.get(barcode):
            self._merge_energy_into_node(payload, acc)
        self._state["nodes"].setdefault(barcode, payload)
        # Platforms add the new entities from their coordinator listeners.
        self._mark_full_update()
        self._schedule_save()
        _LOGGER.info("Automatically tracking Tigo optimizer %s", barcode)
        return True

    def get_diagnostics_data(self) -> dict[str, Any]:
        """Return coordinator diagnostics snapshot."""
        return {
            "packet_capture": self.capture_status,
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
                **self._connection,
                "connected": self._connected,
                "retries": self._retries,
            },
            "module_discovery": {
                "enabled": self._auto_discovery,
                "module_count": len(self._modules),
                "excluded_barcodes": sorted(self._excluded_barcodes),
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
                for barcode, acc in dict(self._energy_state).items()
            },
        }

    @property
    def capture_status(self) -> dict[str, Any]:
        """Diagnostics-safe counters: no local paths and never captured packet data."""
        return self._capture.status() if self._capture is not None else {"state": "idle"}

    async def async_start_capture(
        self, duration_minutes: int = 1440, max_storage_mib: int = 1024
    ) -> dict[str, Any]:
        """Record the active source without opening another port or bus writes."""
        from .pytap.core.capture import CaptureError

        async with self._capture_lock:
            if self._stop_event.is_set():
                raise CaptureError("listener_not_running", "Listener is not running")
            if self._capture_writer_running() or self._capture_job_running():
                raise CaptureError("capture_busy", "A capture or export is already running or stopping")
            if type(duration_minutes) is not int or not 1 <= duration_minutes <= 10080:
                raise CaptureError(
                    "invalid_duration", "Duration must be 1–10080 minutes", min="1", max="10080"
                )
            if type(max_storage_mib) is not int or not 16 <= max_storage_mib <= 4096:
                raise CaptureError("invalid_storage", "Storage must be 16–4096 MiB", min="16", max="4096")
            source = {CONF_CONNECTION_TYPE: self._connection[CONF_CONNECTION_TYPE]}
            if CONF_BAUD_RATE in self._connection:
                source.update(
                    baud_rate=self._connection[CONF_BAUD_RATE], data_bits=8, parity="none", stop_bits=1
                )
            writer = CaptureWriter(
                self._capture_root,
                {
                    "integration_version": INTEGRATION_VERSION,
                    "home_assistant_version": HA_VERSION,
                    "source": source,
                    "parser_state": {},
                    "parser_state_at_capture_boundary": False,
                    "started_mid_connection": self._connected,
                    "timestamps": "UTC/monotonic observation times at source.read return; not UART airtime",
                },
                duration_seconds=duration_minutes * 60,
                max_storage_bytes=max_storage_mib * 1024 * 1024,
            )
            self._capture = writer  # Publish before enabling the producer.
            self._capture_start_future = self.hass.async_add_executor_job(writer.start)
            try:
                await asyncio.shield(self._capture_start_future)
            except asyncio.CancelledError:
                writer.request_stop("start_cancelled")
                raise
            writer.mark("capture_started", self._connection_generation, connected=self._connected)
            return writer.status(include_directory=True)

    async def async_stop_capture(self, reason: str = "user") -> dict[str, Any]:
        """Drain/flush in the executor, preserving evidence on stop and reload."""
        async with self._capture_lock:
            writer = self._capture
            if writer is None:
                return {"state": "idle"}
            writer.request_stop(reason)
            try:
                async with asyncio.timeout(LISTENER_STOP_TIMEOUT):
                    if self._capture_start_future is not None:
                        try:
                            await asyncio.shield(self._capture_start_future)
                        except Exception:
                            pass  # Failed startup is already reflected in status.
                    await self.hass.async_add_executor_job(writer.join, 10)
            except TimeoutError:
                _LOGGER.error("Capture writer is still stopping")
            return writer.status(include_directory=True)

    async def async_list_captures(self) -> list[dict[str, Any]]:
        return await self.hass.async_add_executor_job(list_captures, self._capture_root)

    async def async_capture_info(self, capture_id: str | None = None) -> dict[str, Any]:
        if capture_id is None or (self._capture is not None and capture_id == self._capture.capture_id):
            return self._capture_service_status()

        def info() -> dict[str, Any]:
            directory = capture_directory(self._capture_root, capture_id)
            manifest = read_manifest(directory, allow_incomplete=True)
            hidden = ("metadata", "segments", "events", "directory")
            return {
                **{key: value for key, value in manifest.items() if key not in hidden},
                "directory": str(directory),
            }

        return await self.hass.async_add_executor_job(info)

    async def async_export_capture(
        self, capture_id: str | None = None, max_storage_mib: int | None = None
    ) -> dict[str, Any]:
        """Archive off the loop; the job future, not the lock, marks the export busy."""
        from .pytap.core.capture import CaptureError

        async with self._capture_lock:
            if capture_id is None and self._capture is not None:
                capture_id = self._capture.capture_id  # An empty ID is rejected, not defaulted.
            if capture_id is None:
                raise CaptureError("capture_id_required", "Select a stopped capture ID")
            if self._capture_writer_running():
                raise CaptureError("stop_before_export", "Stop capture before exporting")
            if self._capture_job_running():
                raise CaptureError("capture_busy", "A capture or export is already running or stopping")
            if max_storage_mib is None:
                budget = None  # The capture's own budget, from its manifest.
            elif type(max_storage_mib) is not int or not 16 <= max_storage_mib <= 4096:
                raise CaptureError("invalid_storage", "Storage must be 16–4096 MiB", min="16", max="4096")
            else:
                budget = max_storage_mib * 1024 * 1024
            root = self._capture_root

            def export() -> str:
                directory = capture_directory(root, capture_id)
                limit = budget
                if limit is None:
                    limit = read_manifest(directory, allow_incomplete=True)["max_storage_bytes"]
                return str(export_archive(directory, allow_interrupted=True, max_storage_bytes=limit))

            future = self._capture_export_future = self.hass.async_add_executor_job(export)
            track_capture_job(self.hass, root, future)
        # Released so stop/unload never wait for a long export; start/delete see the busy future.
        archive = await asyncio.shield(future)
        return {
            "capture_id": capture_id,
            "archive": archive,
            "note": "Private local file; not exposed over HTTP",
        }

    async def async_delete_capture(self, capture_id: str) -> dict[str, Any]:
        """Delete one stopped capture and its exports; never the active session."""
        from .pytap.core.capture import CaptureError, delete_capture

        async with self._capture_lock:
            if self._capture is not None and capture_id == self._capture.capture_id:
                if self._capture_writer_running():
                    raise CaptureError("capture_active", "Stop the active capture before deleting it")
            if self._capture_job_running():
                raise CaptureError("capture_busy", "A capture or export is already running or stopping")
            future = self._capture_export_future = self.hass.async_add_executor_job(
                delete_capture, self._capture_root, capture_id
            )
            track_capture_job(self.hass, self._capture_root, future)
        result = await asyncio.shield(future)
        if self._capture is not None and capture_id == self._capture.capture_id:
            self._capture = None
        return result

    def _capture_service_status(self) -> dict[str, Any]:
        """Admin action responses also include the private local directory."""
        if self._capture is None:
            return {"state": "idle"}
        return self._capture.status(include_directory=True)

    def _capture_job_running(self) -> bool:
        """This coordinator's job, or one left running by an unloaded coordinator."""
        own = self._capture_export_future is not None and not self._capture_export_future.done()
        return own or capture_job_running(self.hass, self._capture_root)

    def _capture_writer_running(self) -> bool:
        return (self._capture is not None and self._capture.busy) or (
            self._capture_start_future is not None and not self._capture_start_future.done()
        )

    async def async_start_listener(self) -> None:
        """Start the background listener task."""
        if self._worker_future is not None and not self._worker_future.done():
            raise RuntimeError("Previous PyTap listener has not stopped")
        if self._listener_task is not None and not self._listener_task.done():
            raise RuntimeError("PyTap listener is already starting")
        await self._async_load_coordinator_state()
        self._stop_event.clear()
        self._listener_task = self.config_entry.async_create_background_task(
            self.hass,
            self._async_listen(),
            name="pytap_listener",
        )
        self._schedule_midnight_reset()
        self._freshness_unsub = async_track_time_interval(
            self.hass,
            self._async_check_freshness,
            timedelta(seconds=FRESHNESS_CHECK_INTERVAL_SECONDS),
            cancel_on_shutdown=True,
        )

    async def _async_listen(self) -> None:
        """Async wrapper to run the blocking listener in an executor."""
        self._worker_future = self.hass.async_add_executor_job(self._listen)
        try:
            # HA may cancel background tasks during shutdown. Cancelling the
            # waiter must not lose the handle tracking actual worker completion.
            await asyncio.shield(self._worker_future)
        finally:
            self._stop_event.set()
            if self._capture is not None:
                # Nothing more can be recorded; let the writer finalise its files.
                self._capture.request_stop("listener_stopped")

    def _interrupt_source(self) -> None:
        """Wake a read blocked on an idle TCP socket so the worker sees the stop."""
        with self._source_lock:
            source = self._source
        if (interrupt := getattr(source, "interrupt", None)) is not None:
            try:
                interrupt()
            except Exception as err:
                _LOGGER.debug("Source interrupt failed (%s)", type(err).__name__)

    @callback
    def _cancel_lifecycle_timers(self) -> None:
        """Cancel timers and the pending save; none of them owns the port."""
        if self._midnight_reset_unsub is not None:
            self._midnight_reset_unsub()
            self._midnight_reset_unsub = None
        if self._freshness_unsub is not None:
            self._freshness_unsub()
            self._freshness_unsub = None
        # Unsaved changes are written by the final save once the worker exits.
        self._cancel_scheduled_save()

    @callback
    def async_on_listener_exit(self, exit_callback: CALLBACK_TYPE) -> None:
        """Call ``exit_callback`` on the loop once the listener and its worker have exited."""
        for pending in (self._listener_task, self._worker_future):
            if pending is not None and not pending.done():
                pending.add_done_callback(lambda _done: self.async_on_listener_exit(exit_callback))
                return
        self.hass.loop.call_soon(exit_callback)

    async def async_stop_listener(self, *, discard_state: bool = False) -> bool:
        """Bound the entire shutdown, including both controller lock waits.

        Timers and pending saves are cancelled even if the worker can't be
        joined in time. ``discard_state`` (entry removal) also blocks every
        later Store write, so a late final save can't recreate a deleted Store.
        """
        if discard_state:
            self._save_blocked = True
        self._stop_event.set()
        if self._capture is not None:
            # Disarm a preparing writer even when startup/export holds the lock,
            # and before the interrupted read could be recorded as an error.
            self._capture.request_stop("unload")
        self._interrupt_source()
        self._cancel_lifecycle_timers()
        try:
            async with asyncio.timeout(LISTENER_STOP_TIMEOUT):
                return await self._async_stop_listener_locked()
        except TimeoutError:
            _LOGGER.error("PyTap shutdown timed out; worker handles retained and reload blocked")
            return False

    async def _async_stop_listener_locked(self) -> bool:
        """Await the worker and its port cleanup, without blocking HA's loop.

        The worker owns close; reads/open attempts are bounded. Never race a
        serial read with close, or cancel an executor future as a substitute for
        joining its worker. A failed stop prevents reload from taking the port.
        """
        async with self._stop_lock:
            self._stop_event.set()
            try:
                # Leave part of the caller's bound for capture stop and final save.
                async with asyncio.timeout(LISTENER_STOP_TIMEOUT * 0.75):
                    # Include the wrapper even if it hasn't scheduled its worker.
                    if self._listener_task is not None:
                        try:
                            await asyncio.shield(self._listener_task)
                        except asyncio.CancelledError:
                            # HA cancels the wrapper on shutdown. Still join the
                            # worker, but don't swallow cancellation of this stop.
                            if asyncio.current_task().cancelling():
                                raise
                    if self._worker_future is not None:
                        await asyncio.shield(self._worker_future)
            except TimeoutError:
                _LOGGER.error("PyTap listener did not stop; reload blocked")
                return False
            except Exception as err:
                # The worker has exited (and run its finally cleanup). A parser
                # failure must not prevent timer/state cleanup during unload.
                _LOGGER.error("PyTap listener exited with %s", type(err).__name__)
            await self.async_stop_capture("unload")
            # A running export doesn't own the port: let it finish in the
            # executor instead of failing (and blocking) unload or reload.
            if (self._capture is not None and self._capture.busy) or (
                self._capture_start_future is not None and not self._capture_start_future.done()
            ):
                return False
            self._listener_task = None
            self._worker_future = None
            self._cancel_lifecycle_timers()
            if self._unsaved_changes:
                # Shielded: the caller's timeout or HA shutdown must not cut the
                # final write short once the worker has released the state.
                await asyncio.shield(
                    self.hass.async_create_task(
                        self._async_save_coordinator_state(), "pytap final state save"
                    )
                )
            return True

    def _schedule_midnight_reset(self) -> None:
        """Reset daily accumulators at every local midnight (DST-safe)."""
        self._midnight_reset_unsub = async_track_time_change(
            self.hass, self._perform_midnight_reset, hour=0, minute=0, second=0
        )

    @callback
    def _perform_midnight_reset(self, _now: datetime | None = None) -> None:
        """Reset all daily accumulators and push updated data to sensors."""
        today = dt_util.now().date().isoformat()
        _LOGGER.info(
            "Midnight daily reset — resetting daily accumulators for %s", today
        )

        reset: set[str] = set()
        with self._state_lock:
            for barcode, acc in dict(self._energy_state).items():
                if acc.daily_reset_date == today:
                    continue
                acc.daily_energy_wh = 0.0
                acc.readings_today = 0
                acc.daily_reset_date = today
                if (node := self._state["nodes"].get(barcode)) is not None:
                    self._state["nodes"][barcode] = {**node, **_daily_reset_fields(today)}
                    reset.add(barcode)
            if reset:
                # Snapshots the worker took before the reset must not undo it.
                self._snapshot_sequence += 1
                sequence = self._snapshot_sequence

        if reset:
            self._schedule_save()
            # Reset the *published* values only, so averaged mode never shows
            # raw mid-interval readings because of the reset.
            nodes = {
                barcode: {**node, **_daily_reset_fields(today)} if barcode in reset else node
                for barcode, node in self.data.get("nodes", {}).items()
            }
            self.async_set_updated_data({**self.data, "nodes": nodes, "sequence": sequence}, frozenset(reset))

    def _listen(self) -> None:
        """Blocking listener loop (runs in executor thread).

        Connects to the Tigo gateway, reads bytes, feeds them into the
        parser, and dispatches events back to the HA event loop.
        """
        from .pytap.api import create_parser

        retries = 0
        # Announce an outage once. Only data actually received ends it, so a
        # link that connects and then drops immediately doesn't warn every retry.
        outage_announced = False
        connected_once = False

        while not self._stop_event.is_set():
            source = None
            parser = create_parser(persistent_state=self._persistent_state)
            self._init_mappings_from_parser(parser)
            self._infra_received = False
            with self._source_lock:
                self._source = None

            try:
                source = open_source(self.hass.loop, self._connection)
                with self._source_lock:
                    if self._stop_event.is_set():
                        return
                    self._source = source
                self._connected = True
                self._connection_generation += 1
                if self._capture is not None:
                    self._capture.mark("connection_open", self._connection_generation)
                if not connected_once:
                    _LOGGER.info("Connected to Tigo byte stream")
                    connected_once = True
                else:
                    _LOGGER.debug("Reopened Tigo byte stream (attempt %d)", retries)
                last_data_time = time.monotonic()

                while not self._stop_event.is_set():
                    data = source.read(4096)
                    if data:
                        last_data_time = time.monotonic()
                        if retries:
                            if outage_announced:
                                _LOGGER.info("Tigo byte stream recovered after %d attempt(s)", retries)
                            outage_announced = False
                            retries = 0
                            self._retries = 0
                        if self._capture is not None:
                            self._capture.record(
                                data, self._connection_generation, self._persistent_state.to_dict
                            )
                        events = parser.feed(data)
                        with self._state_lock:
                            for event in events:
                                if self._process_event(event):
                                    self._ha_update_pending = True

                        if self._live_reporting:
                            # Live mode — push every change immediately
                            self._push_live_update(parser)
                        else:
                            # Interval mode — buffer readings and push
                            # time-weighted averages once per write_interval.
                            self._maybe_flush_interval(parser, time.monotonic())
                    else:
                        # No data this read cycle (socket read timeout).
                        now = time.monotonic()
                        if not self._live_reporting:
                            # Flush buffered readings on schedule so sensors
                            # keep updating during production lulls.
                            self._maybe_flush_interval(parser, now)
                        if (
                            RECONNECT_TIMEOUT > 0
                            and (now - last_data_time) > RECONNECT_TIMEOUT
                        ):
                            if outage_announced:
                                _LOGGER.debug("Still no data from gateway, reconnecting")
                            else:
                                _LOGGER.warning(
                                    "No data from gateway for %ds, reconnecting",
                                    RECONNECT_TIMEOUT,
                                )
                                outage_announced = True
                            break

            except Exception as err:
                if self._stop_event.is_set():
                    # An interrupted read on stop is not a connection error.
                    return
                if self._capture is not None:
                    self._capture.mark(
                        "connection_error", self._connection_generation, error=type(err).__name__
                    )
                # Endpoints stay out of logs; errno is safe and useful. Log the
                # loss once, then keep retrying quietly until it recovers.
                errno = getattr(err, "errno", None)
                detail = f"{type(err).__name__}" + (f", errno {errno}" if errno is not None else "")
                if not outage_announced:
                    _LOGGER.warning("Tigo byte stream unavailable (%s); retrying", detail)
                    outage_announced = True
                else:
                    _LOGGER.debug("Connection attempt failed (%s)", detail)
            finally:
                self._connected = False
                try:
                    if self._capture is not None:
                        self._capture.mark("connection_closed", self._connection_generation)
                    self._flush_pending_on_disconnect()
                finally:
                    # A failed snapshot must never skip releasing the port.
                    if source is not None:
                        try:
                            source.close()
                        except Exception as err:
                            _LOGGER.warning("Source close error (%s)", type(err).__name__)
                    with self._source_lock:
                        self._source = None

            if self._stop_event.is_set():
                break

            retries += 1
            self._retries = retries
            if RECONNECT_RETRIES > 0 and retries > RECONNECT_RETRIES:
                _LOGGER.error("Max retries (%d) exceeded", RECONNECT_RETRIES)
                return

            _LOGGER.debug(
                "Reconnecting in %ds (attempt %d/%s)...",
                RECONNECT_DELAY,
                retries,
                str(RECONNECT_RETRIES) if RECONNECT_RETRIES else "∞",
            )
            # Wake immediately on unload, including while a proxy is offline.
            if self._stop_event.wait(RECONNECT_DELAY):
                return

    def _flush_pending_on_disconnect(self, parser: Any = None) -> None:
        """Publish data held back by the reporting interval before disconnect."""
        if not self._ha_update_pending:
            return
        parser = parser if parser is not None else self._counter_source
        with self._state_lock:
            if parser is not None:
                self._state["counters"] = parser.counters
            if self._live_reporting:
                snapshot = self._snapshot()
            else:
                # Averaged mode never publishes raw values, even with empty buffers.
                snapshot = self._build_averaged_snapshot(time.monotonic())
                self._reading_buffers.clear()
        self._publish(snapshot)
        self._ha_update_pending = False

    def _push_live_update(self, parser: Any) -> None:
        """Push an immediate snapshot to HA (live reporting mode).

        Publishes a copy of every node so the listener thread never mutates
        data Home Assistant is reading. Runs in the executor thread.
        """
        self._counter_source = parser
        if not self._ha_update_pending:
            return
        with self._state_lock:
            self._state["counters"] = parser.counters
            snapshot = self._snapshot()
        self._publish(snapshot)
        self._ha_update_pending = False

    def _maybe_flush_interval(self, parser: Any, now: float) -> None:
        """Flush buffered readings as a time-weighted average (interval mode).

        Pushes a snapshot once the write interval has elapsed since the last
        update.  Called both when data arrives and during idle read cycles so
        buffered readings are not stranded when the stream goes quiet.  Runs
        in the executor thread.
        """
        self._counter_source = parser
        if not self._ha_update_pending or (
            now - self._last_ha_update < self._write_interval
        ):
            return
        with self._state_lock:
            self._state["counters"] = parser.counters
            snapshot = self._build_averaged_snapshot(now)
        per_node_counts = {
            barcode: len(readings)
            for barcode, readings in self._reading_buffers.items()
            if readings and barcode in snapshot["nodes"]
        }
        _LOGGER.debug(
            "HA update: %d node(s) — %s",
            len(per_node_counts),
            ", ".join(
                f"{snapshot['nodes'][b].get('name', b)}: {n} reading(s)"
                for b, n in per_node_counts.items()
            ),
        )
        self._reading_buffers.clear()
        self._publish(snapshot)
        self._last_ha_update = now
        self._ha_update_pending = False

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
        with self._state_lock:
            return self._process_event_locked(event)

    def _process_event_locked(self, event: Event) -> bool:
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
            discovered_changed = False
            if barcode not in self._discovered_barcodes:
                self._discovered_barcodes.add(barcode)
                self._state["discovered_barcodes"] = sorted(self._discovered_barcodes)
                self._schedule_save()
                discovered_changed = True
                _LOGGER.info(
                    "Discovered unconfigured Tigo optimizer barcode: %s (gateway=%d, node=%d).",
                    barcode,
                    event.gateway_id,
                    event.node_id,
                )
            if not self._add_discovered_module(barcode, event.node_id):
                return discovered_changed
            # Process this first reading immediately, rather than dropping it
            # while HA creates the newly discovered sensor entities.

        # Get module metadata
        module_meta = self._module_lookup.get(barcode, {})
        previous = self._state["nodes"].get(barcode)
        peak_power = self.module_peak_power(barcode)
        performance: float | None = None
        if event.power is not None and peak_power > 0:
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

        self._state["nodes"][barcode] = {
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
            "report_monotonic": time.monotonic(),
        }
        if previous is not None and "topology" in previous:
            self._state["nodes"][barcode]["topology"] = previous["topology"]
        # Only buffer readings in interval/averaged mode.  In live mode the
        # raw values are pushed immediately, so buffering would grow without
        # bound and never be consumed.
        if not self._live_reporting:
            self._reading_buffers.setdefault(barcode, []).append(
                (
                    time.monotonic(),
                    {field: self._state["nodes"][barcode][field] for field in _AVERAGED_FIELDS},
                )
            )
        self._mark_changed(barcode)
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
        if event.gateways != self._state["gateways"]:
            self._mark_full_update()
        self._state["gateways"] = event.gateways

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
                            "Discovered unconfigured Tigo optimizer barcode: %s (node=%d).",
                            barcode,
                            node_id,
                        )
                    self._add_discovered_module(barcode, node_id)

        if discovered_changed:
            self._state["discovered_barcodes"] = sorted(self._discovered_barcodes)

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
            self._mark_full_update()
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
            _LOGGER.info(
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
            _LOGGER.info(
                "Barcode mappings updated — %d/%d configured barcodes now "
                "matched in node table.",
                len(configured_matched),
                len(self._configured_barcodes),
            )
            if configured_missing:
                _LOGGER.info(
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
            node_data = self._state["nodes"].get(barcode)
            if node_data:
                # Replace rather than mutate: saves may be copying this dict.
                self._state["nodes"][barcode] = {**node_data, "topology": event.to_dict()}
                self._mark_changed(barcode)
                return True
        return False

    # -------------------------------------------------------------------
    #  Persistence helpers
    # -------------------------------------------------------------------

    def _build_averaged_snapshot(self, flush_ts: float) -> dict[str, Any]:
        """Return the data snapshot to push to Home Assistant.

        For each barcode in ``_reading_buffers``, computes a **time-weighted
        average** of numeric readings and writes the result into a copy of
        the node dict.  ``self._state["nodes"]`` is **not** mutated — the raw
        latest values are preserved there for persistence; only the returned
        snapshot carries averaged values.

        Time-weighted averaging weights each reading by the duration it was
        "active" (time until the next reading, or until ``flush_ts`` for the
        last one).  This gives a physically representative average when
        readings arrive at uneven intervals.

        ``flush_ts`` should be ``time.monotonic()`` at the moment of flush.

        Must be called before ``_reading_buffers`` is cleared.
        """
        base = self._snapshot()
        snapshot_nodes = base["nodes"]
        published = self.data.get("nodes", {})
        for barcode, node in snapshot_nodes.items():
            # No new reading this interval: keep the previously published
            # averages, but take energy fields from the worker.
            if not self._reading_buffers.get(barcode) and (old := published.get(barcode)) is not None:
                for field in (*_AVERAGED_FIELDS, "performance"):
                    if field in old:
                        node[field] = old[field]
        for barcode, readings in self._reading_buffers.items():
            node = snapshot_nodes.get(barcode)
            if node is None or not readings:
                continue

            if len(readings) == 1:
                # Single reading — no averaging needed
                _, fields = readings[0]
                avg_node = dict(node)
                for field in _AVERAGED_FIELDS:
                    avg_node[field] = fields[field]
            else:
                avg_node = dict(node)
                for field in _AVERAGED_FIELDS:
                    weighted_sum = 0.0
                    total_weight = 0.0
                    for i, (ts, fields) in enumerate(readings):
                        value = fields[field]
                        if value is None:
                            continue
                        # Weight = duration until next reading (or flush)
                        if i + 1 < len(readings):
                            weight = readings[i + 1][0] - ts
                        else:
                            weight = flush_ts - ts
                        if weight <= 0:
                            weight = 1e-6  # avoid zero-weight edge case
                        weighted_sum += value * weight
                        total_weight += weight
                    avg_node[field] = (
                        round(weighted_sum / total_weight, 3)
                        if total_weight > 0
                        else None
                    )

            peak = avg_node.get("peak_power", DEFAULT_PEAK_POWER)
            if avg_node.get("power") is not None and peak > 0:
                avg_node["performance"] = round(
                    (max(avg_node["power"], 0.0) / peak) * 100.0,
                    2,
                )
            else:
                avg_node["performance"] = None
            snapshot_nodes[barcode] = avg_node
        return {**base, "nodes": snapshot_nodes}

    def _build_node_payload(
        self,
        barcode: str,
        module_meta: dict[str, Any],
        node_id: int | None,
    ) -> dict[str, Any]:
        """Build a baseline node payload for restored/startup state."""
        return {
            "gateway_id": None,
            "node_id": node_id,
            "barcode": barcode,
            "name": module_meta.get(CONF_MODULE_NAME, barcode),
            "string": module_meta.get(CONF_MODULE_STRING, ""),
            "peak_power": module_meta.get(CONF_MODULE_PEAK_POWER, DEFAULT_PEAK_POWER),
            "voltage_in": None,
            "voltage_out": None,
            "current_in": None,
            "current_out": None,
            "power": None,
            "performance": None,
            "temperature": None,
            "dc_dc_duty_cycle": None,
            "rssi": None,
            "daily_energy_wh": 0.0,
            "total_energy_wh": 0.0,
            "readings_today": 0,
            "daily_reset_date": "",
            "last_update": None,
        }

    def _merge_snapshot_into_node(
        self,
        node_payload: dict[str, Any],
        snapshot: dict[str, Any],
    ) -> None:
        """Merge persisted node snapshot fields into a baseline node payload."""
        for key in _SNAPSHOT_FIELDS:
            if key in snapshot:
                node_payload[key] = snapshot[key]

    def _merge_energy_into_node(
        self,
        node_payload: dict[str, Any],
        acc: EnergyAccumulator,
    ) -> None:
        """Merge persisted accumulator values into a node payload."""
        node_payload["daily_energy_wh"] = round(acc.daily_energy_wh, 2)
        node_payload["total_energy_wh"] = round(acc.total_energy_wh, 2)
        node_payload["readings_today"] = acc.readings_today
        node_payload["daily_reset_date"] = acc.daily_reset_date

    async def _async_load_coordinator_state(self) -> None:
        """Load persisted state, then publish it before any entity is created.

        If the saved state can't be read, nothing is written back this session
        and setup skips orphan cleanup, rather than replacing energy totals and
        discovered modules with empty state.
        """
        try:
            stored = await self._store.async_load()
            if stored is not None:
                with self._state_lock:
                    self._apply_stored_state(stored)
        except Exception as err:
            self._save_blocked = True
            _LOGGER.warning(
                "Could not load saved PyTap state (%s); it won't be overwritten until the next reload",
                type(err).__name__,
            )
        else:
            self._store_loaded = True
            self._store_restored = stored is not None
        self.data = self._snapshot()

    def _apply_stored_state(self, stored: dict[str, Any]) -> None:
        """Restore mappings, discoveries, parser state, energy and node snapshots."""

        # Restore discovery metadata before node snapshots. The config entry's
        # explicit list overrides it, including user-edited names/groupings/Wp.
        for module in stored.get("auto_modules", []):
            if (
                isinstance(module, dict)
                and isinstance(barcode := module.get(CONF_MODULE_BARCODE), str)
                and barcode
            ):
                self._add_discovered_module(barcode, restored=module)

        # Restore barcode ↔ node_id mappings
        barcode_to_node = stored.get("barcode_to_node", {})
        for barcode, node_id in barcode_to_node.items():
            self._barcode_to_node[barcode] = int(node_id)
            self._node_to_barcode[int(node_id)] = barcode

        # Restore discovered barcodes
        discovered = stored.get("discovered_barcodes", [])
        self._discovered_barcodes = set(discovered)
        self._state["discovered_barcodes"] = sorted(self._discovered_barcodes)
        # Turning discovery on can admit barcodes learned in previous manual
        # sessions, without waiting for another node-table broadcast.
        for barcode in self._discovered_barcodes:
            self._add_discovered_module(barcode, self._barcode_to_node.get(barcode))

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

        # Restore last known node snapshots for configured barcodes, and
        # merge energy accumulator values where available.
        node_snapshots = stored.get("node_snapshots", {})
        for barcode in self._configured_barcodes:
            snapshot = node_snapshots.get(barcode)
            acc = self._energy_state.get(barcode)
            if not isinstance(snapshot, dict) and acc is None:
                continue

            module_meta = self._module_lookup.get(barcode, {})
            node_id = self._barcode_to_node.get(barcode)
            node_payload = self._build_node_payload(barcode, module_meta, node_id)

            if isinstance(snapshot, dict):
                self._merge_snapshot_into_node(node_payload, snapshot)

            if acc is not None:
                self._merge_energy_into_node(node_payload, acc)

            # A user may have changed/cleared the rating since the last reading.
            power = node_payload.get("power")
            peak = node_payload.get(CONF_MODULE_PEAK_POWER, DEFAULT_PEAK_POWER)
            node_payload["performance"] = (
                round(max(power, 0) / peak * 100, 2) if power is not None and peak > 0 else None
            )

            self._state["nodes"][barcode] = node_payload

        _LOGGER.info(
            "Restored coordinator state: %d barcode mappings, %d discovered barcodes, "
            "%d gateway identities, %d energy states, %d node snapshots",
            len(barcode_to_node),
            len(discovered),
            len(self._persistent_state.gateway_identities),
            len(self._energy_state),
            len(node_snapshots),
        )

    async def _async_save_coordinator_state(self) -> None:
        """Save all state (barcode mappings, discovered barcodes, parser state) to HA Store."""
        if self._save_blocked:
            return
        self._unsaved_changes = False
        # Serialise under the state lock so the worker can't change nodes or
        # accumulators mid-copy; the Store write itself happens outside it.
        with self._state_lock:
            configured_barcodes = self._configured_barcodes.copy()
            data = {
                "auto_modules": [dict(module) for module in self._auto_modules.values()],
                "barcode_to_node": dict(self._barcode_to_node),
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
                            acc.last_reading_ts.isoformat() if acc.last_reading_ts is not None else None
                        ),
                    }
                    for barcode, acc in self._energy_state.items()
                },
                "node_snapshots": {
                    barcode: {key: value for key, value in node.items() if key in _SNAPSHOT_FIELDS}
                    for barcode, node in self._state["nodes"].items()
                    if barcode in configured_barcodes and isinstance(node, dict)
            },
        }
        try:
            await self._store.async_save(data)
        except Exception:
            _LOGGER.warning("Failed to save coordinator state")

    def _schedule_save(self) -> None:
        """Schedule a throttled save of coordinator state.

        Uses a throttle pattern: if a save is already scheduled, let it fire
        rather than cancelling and restarting. This guarantees the store is
        written within SAVE_DELAY_SECONDS of the first change, even under
        continuous high-frequency updates.

        Safe to call from the executor thread — dispatches to the HA event loop
        only for the first change of each save cycle.
        """
        self._unsaved_changes = True
        if self._save_requested:
            return
        self._save_requested = True

        def _do_schedule() -> None:
            if self._stop_event.is_set():
                self._save_requested = False
                return
            if self._save_task is not None:
                return  # Save already pending — let it fire on time
            self._save_task = self.hass.loop.call_later(
                SAVE_DELAY_SECONDS,
                lambda: self.hass.async_create_task(self._do_save()),
            )

        self.hass.loop.call_soon_threadsafe(_do_schedule)

    def _cancel_scheduled_save(self) -> None:
        """Drop a pending save timer (loop only); the next change starts a new cycle."""
        if self._save_task is not None:
            self._save_task.cancel()
            self._save_task = None
        self._save_requested = False

    async def _do_save(self) -> None:
        """Execute the scheduled save and clear the timer handle."""
        self._save_task = None
        # Cleared before copying state, so later changes request another save.
        self._save_requested = False
        await self._async_save_coordinator_state()
