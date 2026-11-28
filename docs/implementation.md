# PyTap — Implementation Document

> Version 1.4.0 · Last updated: November 2026

This document captures the current implementation state of the PyTap Home Assistant custom component. It describes what has been built, how each module works, the design decisions made during development, and the test coverage in place.

For the high-level architecture and design rationale, see [architecture.md](architecture.md).

---

## Table of Contents

1. [Implementation Summary](#implementation-summary)
2. [File Inventory](#file-inventory)
3. [Module Details](#module-details)
   - [const.py — Constants](#constpy--constants)
   - [manifest.json — Integration Metadata](#manifestjson--integration-metadata)
   - [config_flow.py — Configuration Flow](#config_flowpy--configuration-flow)
   - [connection.py — Transports](#connectionpy--transports)
   - [coordinator.py — Data Coordinator](#coordinatorpy--data-coordinator)
   - [energy.py — Energy Accumulation](#energypy--energy-accumulation)
   - [sensor.py — Sensor Platform](#sensorpy--sensor-platform)
   - [binary_sensor.py — Binary Sensor Platform](#binary_sensorpy--binary-sensor-platform)
   - [capture_services.py and pytap/core/capture.py — Packet Capture](#capture_servicespy-and-pytapcorecapturepy--packet-capture)
   - [pytap/core/replay.py and scripts/replay_capture.py — Offline Replay](#pytapcorereplaypy-and-scriptsreplay_capturepy--offline-replay)
   - [diagnostics.py — Diagnostics Platform](#diagnosticspy--diagnostics-platform)
   - [\_\_init\_\_.py — Integration Lifecycle](#__init__py--integration-lifecycle)
   - [strings.json / translations — UI Strings](#stringsjson--translations--ui-strings)
4. [Config Flow UX Design](#config-flow-ux-design)
5. [Data Flow](#data-flow)
6. [Testing](#testing)
7. [Design Decisions & Trade-offs](#design-decisions--trade-offs)
8. [Known Deviations from Architecture](#known-deviations-from-architecture)
9. [Development History](#development-history)
10. [Future Work](#future-work)

---

## Implementation Summary

PyTap is a Home Assistant custom component that passively monitors Tigo TAP solar energy systems. It receives the RS-485 bus byte stream over TCP, a local serial adapter, or an ESPHome serial proxy, parses the protocol in real time using an embedded Python parser library, and exposes per-optimizer entities in Home Assistant.

**Key characteristics of the current implementation:**

| Aspect | Implementation |
| --- | --- |
| Integration type | Hub (`integration_type: "hub"`) |
| Data delivery | Push-based streaming (`iot_class: "local_push"`); selectable live or time-weighted averaged interval reporting |
| Entity creation | Configured modules at setup; passively discovered modules added at runtime (discovery on by default for new setups, manual mode available) |
| Config flow | Transport menu (TCP / serial), then menu-driven module list with discovery setting |
| Threading model | Blocking parser in executor thread with private state; copies published to the async event loop |
| Stale readings | Configurable action (`hold` default for all entries; opt-in `unavailable`, `zero`) after a 60–600 s timeout (default 120 s) |
| Restart behavior | Store restores mappings, discovered modules, energy and node snapshots; live readings are shown before the first new report only with `hold` |
| External dependencies | `serialx` (serial/proxy); parser library embedded |
| Entity types | Per optimizer: 13 sensors + 3 binary sensors; 4 aggregate sensors per string and per installation; discovery-status sensor when discovery is on |
| Packet capture | Opt-in admin actions; offline replay script |

---

## File Inventory

```
custom_components/pytap/
├── __init__.py          # Lifecycle (setup, teardown, migration v1→v7, device removal, capture cleanup)
├── binary_sensor.py     # High output mode, low voltage flag, reading fault
├── capture_services.py  # Admin actions: start/stop/status/list/export/delete capture
├── config_flow.py       # Config & options flows
├── connection.py        # Connection validation, serial/proxy source, endpoint comparison
├── const.py             # Domain, config keys, defaults, reporting/stale/energy/plausibility constants
├── coordinator.py       # Push-based DataUpdateCoordinator
├── diagnostics.py       # Diagnostics download (config entry diagnostics)
├── energy.py            # Pure trapezoidal energy accumulation helpers
├── entity.py            # Shared optimizer DeviceInfo
├── manifest.json        # HA integration metadata
├── sensor.py            # Per-optimizer, aggregate and discovery-status sensors
├── services.yaml        # Action descriptions
├── strings.json         # UI strings (source of truth)
├── translations/
│   └── en.json          # English translations (mirrors strings.json)
└── pytap/               # Embedded protocol parser library (no HA imports)
    ├── api.py           # Public API: connect(), create_parser(), parse_bytes()
    └── core/
        ├── parser.py    # Protocol parser: bytes → events
        ├── types.py     # Protocol constants & frame types
        ├── events.py    # Event dataclasses (PowerReportEvent, etc.)
        ├── state.py     # SlotClock, NodeTableBuilder, PersistentState (to_dict/from_dict)
        ├── source.py    # TcpSource (with interrupt()), SerialSource
        ├── capture.py   # CaptureWriter, manifest validation, iter_capture, export, delete
        ├── replay.py    # FrameScanner, decode_wire_frame, replay_events
        ├── crc.py       # CRC-16-CCITT
        └── barcode.py   # Tigo barcode encode/decode

scripts/
└── replay_capture.py    # Offline capture validation, frame inspection and replay

tests/                   # See Testing
docs/
├── architecture.md      # Architecture & design document
└── implementation.md    # This file
```

---

## Module Details

### `const.py` — Constants

Defines all integration-wide constants in a single location, including:

```python
DOMAIN = "pytap"
INTEGRATION_VERSION = "1.4.0"

# Connection
DEFAULT_PORT = 502
DEFAULT_BAUD_RATE = 38400
CONNECTION_TIMEOUT = 10           # Open/connect bound (s)
SERIAL_READ_TIMEOUT = 1           # Idle serial read bound (s)
LISTENER_STOP_TIMEOUT = 20        # Bound for stopping the listener (s)

# Modules
DEFAULT_PEAK_POWER = 455          # Wp for manually added modules
DEFAULT_DISCOVERED_STRING = "Discovered"

# Reporting mode
DEFAULT_LIVE_REPORTING = True
DEFAULT_WRITE_INTERVAL = 30       # 5–300 s

# Stale readings
CONF_STALE_ACTION = "stale_action"     # "hold" | "unavailable" | "zero"
CONF_STALE_TIMEOUT = "stale_timeout"
DEFAULT_STALE_ACTION = "hold"
DEFAULT_STALE_TIMEOUT = 120            # 60–600 s
FRESHNESS_CHECK_INTERVAL_SECONDS = 5

# Plausibility (power reports outside these are faults)
MAX_PLAUSIBLE_VOLTAGE = 100.0
MAX_OUTPUT_ABOVE_INPUT_VOLTS = 2.0
MIN_PLAUSIBLE_TEMPERATURE = -40.0
MAX_PLAUSIBLE_TEMPERATURE = 125.0

# Reconnection / energy
RECONNECT_TIMEOUT = 60
RECONNECT_DELAY = 5
RECONNECT_RETRIES = 0             # 0 = infinite
ENERGY_GAP_THRESHOLD_SECONDS = 120
ENERGY_LOW_POWER_THRESHOLD_W = 1.0
```

---

### `manifest.json` — Integration Metadata

```json
{
  "domain": "pytap",
  "name": "PyTap",
  "after_dependencies": ["esphome"],
  "codeowners": ["@azebro"],
  "config_flow": true,
  "dependencies": ["usb"],
  "documentation": "https://github.com/azebro/pytap",
  "integration_type": "hub",
  "iot_class": "local_push",
  "issue_tracker": "https://github.com/azebro/pytap/issues",
  "requirements": ["serialx>=1.11.0"],
  "version": "1.4.0"
}
```

`hacs.json` requires Home Assistant 2026.10.4. Each Core's official constraints
choose its compatible `serialx` version (1.11.0 on the minimum, 1.15.1 on
2026.11.2); neither dependency metadata nor Core internals are overridden.

---

### `config_flow.py` — Configuration Flow

#### Config Flow Steps

```
┌─────────────┐   ┌──────────────┐   ┌──────────────────┐   ┌──────────────┐
│ Step: user  │──►│ tcp / serial │──►│ Step: modules_   │──►│ add_module / │
│ (menu)      │   │ (form)       │   │ menu (menu)      │◄──│ discovery    │
└─────────────┘   └──────────────┘   │  ► Add module    │   └──────────────┘
                                     │  ► Discovery     │
                                     │  ► Finish setup  │── CREATE_ENTRY
                                     └──────────────────┘
```

1. **`async_step_user`** — Menu: TCP gateway or Serial / Home Assistant proxy.
2. **`async_step_tcp` / `async_step_serial`** — `normalize_connection()` validates the form; `connection_errors()` then rejects a serial port used by another PyTap entry (`serial_port_in_use`, symlinks resolved) or a serial port / TCP host and port still held by a parked coordinator (`serial_port_in_use` / `connection_in_use`), and probes the endpoint in the executor (a path that doesn't resolve to a character device under `/dev/` gives `invalid_serial_port`). A failed probe shows `cannot_connect`; the same settings submitted again are accepted. Sets a random UUID as unique ID.
3. **`async_step_modules_menu`** — Add a module, Module discovery settings, Finish setup; shows the module list and discovery mode.
4. **`async_step_discovery`** — `auto_discovery` toggle (default on).
5. **`async_step_add_module`** — `string`, `name`, `barcode` (required), `peak_power` (1–1000, default 455). Errors: `missing_string`, `missing_name`, `missing_barcode`, `invalid_barcode` (`^[0-9A-Fa-f]-[0-9A-Fa-f]{1,7}[A-Za-z]$`), `duplicate_barcode`.
6. **`async_step_finish`** — In manual mode with no modules, shows the add-module form with `no_modules`. Re-checks the serial claim, then creates the entry with connection settings, `modules`, `auto_discovery` and `excluded_barcodes: []`.

#### Options Flow Steps

- **`async_step_init`** — Menu: change connection, change reporting, change stale reading behaviour, change discovery, add, edit, remove, Save and close. The module list is read from the running coordinator (including discovered modules).
- **`async_step_change_connection`** → **`change_tcp` / `change_serial`** — Same validation as setup; the currently used serial port is not probed. Saved immediately (title updated), which reloads the entry; an entry in setup error/retry (e.g. after invalid saved settings) is reloaded explicitly, since its update listener was never registered.
- **`async_step_change_reporting`** — Live reporting checkbox. On: clears `write_interval`, saves. Off: continues to **`async_step_set_interval`** (5–300 s).
- **`async_step_change_stale`** — `stale_action` dropdown (translated selector) and `stale_timeout` (60–600 s). Saved immediately.
- **`async_step_change_discovery`** — Toggle, applied on Save and close.
- **`async_step_add_module`** — Same as setup.
- **`async_step_edit_module`** → **`async_step_module_details`** — Name, string, `peak_power` (0–1000; 0 = unknown). The barcode can't change.
- **`async_step_remove_module`** — Dropdown; adds the barcode to `excluded_barcodes`.
- **`async_step_done`** — Merges modules discovered while the flow was open (unless removed/excluded), validates manual mode isn't empty, and writes `modules`, `auto_discovery`, `excluded_barcodes`.

#### Error Classes

`InvalidModuleFormat`, `InvalidBarcodeFormat` (`HomeAssistantError` subclasses). Connection errors use `connection.InvalidConnectionSettings(field, error)`.

---

### `connection.py` — Transports

- **`normalize_connection(data)`** — TCP: non-empty host, port 1–65535. Serial: no control characters; local endpoints are normalised (`posixpath.normpath`, which is what gets saved) and must be under `/dev/` but not `/dev/fd`, `/dev/std*`, `/dev/shm` or `/dev/mqueue` (`is_device_path()`); URLs must be `esphome-hass://esphome/<port>?<filters>` using only known identity filters (no credentials or protocol overrides); baud 1–4000000.
- **`check_device_node(endpoint)`** — Blocking; called by `open_source()` before opening a local path. Its `realpath` must pass `is_device_path()` and be a character device.
- **`serial_endpoint_key()` / `resolved_endpoint_key()`** — Normalise endpoints for comparison; the latter resolves symlinks (blocking, run in the executor).
- **`SerialProxySource`** — Wraps `serialx.async_serial_for_url` (8N1, no flow control, DTR/RTS left undefined). Its blocking `connect()/read()/close()` submit bounded coroutines to HA's loop; an idle read returns `b""` after `SERIAL_READ_TIMEOUT`, EOF raises `ConnectionResetError`.
- **`open_source(loop, data)`** — Opens a `SerialProxySource` or `TcpSource`; closes it if `connect()` fails.
- **`connection_title()`** — `PyTap (<host>)`, `PyTap (Home Assistant serial proxy)` or `PyTap (Serial)`.

HA 2026.10.4 accepts supported proxy URLs with `port_name`, except decoded
`port_udev_id` values ending in `-port<number>` (P9). P9 is checked before a
missing `port_name` (P1), with distinct flow/setup errors that advise selecting
the port again or upgrading to HA 2026.11+. HA 2026.11.2 accepts both forms.
Other supported identity filters are retained, and malformed P2–P8 URLs are
rejected on both versions. Availability or identity-mismatch failures retain
the existing background-retry and flow resubmission rules; no additional
matcher limitation is introduced.

---

### `coordinator.py` — Data Coordinator

#### Class: `PyTapDataUpdateCoordinator`

Inherits from `DataUpdateCoordinator[dict[str, Any]]`. This is a **push-based** integration — `_async_update_data()` just returns the current data.

#### Initialization

Reads from `ConfigEntry.data`: connection settings (`normalize_connection`, may raise `InvalidConnectionSettings`), `modules`, `auto_discovery` (default `False` if missing), `excluded_barcodes`, `live_reporting`, `write_interval`, `stale_action` (missing or invalid values fall back to `hold`) and `stale_timeout` (clamped to 60–600). Builds the barcode allowlist and module lookup, and initialises:

```python
self._state = {                # owned by the listener thread
    "gateways": {},
    "nodes": {},               # barcode → node dict
    "counters": {},            # parser counters
    "discovered_barcodes": [],
}
self.data = self._snapshot()   # what Home Assistant reads
```

#### Worker State and Publishing

`_state` and `_energy_state` are guarded by `_state_lock` (`threading.RLock`). `_snapshot()` copies every node dict and adds a `sequence` number; `_publish(snapshot)` schedules `async_set_updated_data(snapshot, changed)` with `call_soon_threadsafe`. `changed` holds the barcodes marked by `_mark_changed()` since the last publish, or `None` after `_mark_full_update()` (module admitted, gateways or node mappings changed). With a set, only the callbacks registered via `async_add_data_listener(barcodes, callback)` for those barcodes run; with `None`, every coordinator listener runs (the platforms add new entities from theirs). A snapshot older than the last applied one (taken before a midnight reset) is dropped and its changes are notified with the next one. HA-side code (entities, diagnostics, midnight reset) only reads or replaces `self.data`.

#### Listener Lifecycle

```
async_start_listener()
    ├── _async_load_coordinator_state()     (Store → _state, then self.data = snapshot)
    ├── background task → _async_listen()  └── executor job → _listen()
    ├── _schedule_midnight_reset()          (async_track_time_change 00:00:00)
    └── freshness timer                     (async_track_time_interval, 5 s)

async_stop_listener(*, discard_state=False) → bool   (bounded by LISTENER_STOP_TIMEOUT)
    ├── set _stop_event, capture.request_stop("unload"), _interrupt_source()
    ├── cancel midnight, freshness and save timers (even if the stop then times out)
    └── _async_stop_listener_locked()
          ├── await listener task and worker future (shielded)
          ├── async_stop_capture("unload")
          └── flush unsaved Store changes (shielded; skipped once saves are blocked)
```

- **`_async_listen()`** — Owns the completion future returned by `hass.loop.run_in_executor(None, self._listen)` and awaits it through `asyncio.shield`. Unlike an executor future created by `hass.async_add_executor_job` from this background task, it is not directly cancelled by HA before the stop event. The existing shutdown handler can therefore join the actual worker and complete the final Store save.
- **`_listen()`** — For each connection: new parser via `create_parser(persistent_state=...)`, `open_source()`, then read 4096-byte chunks. Each chunk is recorded by an active capture, fed to the parser, and the resulting events processed under `_state_lock`. Live mode publishes at once (`_push_live_update`); averaged mode calls `_maybe_flush_interval` on data and on idle reads. After `RECONNECT_TIMEOUT` of silence it reconnects. Errors and silence reconnects are logged once per outage (type + errno, at WARNING), later attempts at DEBUG; the outage ends only when data is received again. A read interrupted by stop isn't recorded as a connection error. The `finally` block marks the capture, flushes pending data, and closes the source. The reconnect delay uses `_stop_event.wait()`.
- **`_interrupt_source()`** — Calls the source's `interrupt()` if present (`TcpSource` shuts the socket down to wake `recv`).
- A stop that times out returns `False`; `__init__.py` parks the coordinator so the next setup waits for it, and `async_on_listener_exit()` lets it finish its own stop once the worker exits. `discard_state=True` (entry removal) blocks every later Store write. An export running in the executor does not block stopping.

#### Event Processing

`_process_event()` dispatches `PowerReportEvent`, `InfrastructureEvent`, `TopologyEvent` (attached to known nodes) and `StringEvent` (DEBUG log) and returns whether data changed.

**`_handle_power_report(event) → bool`:**
1. Resolves the barcode from the event, or from `_node_to_barcode` once this session has received a node table; otherwise counts a pending report and returns `False`.
2. Unconfigured barcode: recorded in `discovered_barcodes` (logged once) and passed to `_add_discovered_module()`; if not admitted, returns.
3. `reading_faults(event)` — non-empty means impossible: keep the previous node (or a baseline payload), set `reading_fault=True`, `fault_reasons`, `last_fault_at`, log a WARNING when the fault changes, save, return `True`. No energy, no `report_monotonic` update.
4. Valid report: performance (0 W floor, `None` while `peak_power` is 0), `accumulate_energy()`, then replace the node:

```python
{
    "gateway_id": int, "node_id": int, "barcode": str,
    "name": str, "string": str, "peak_power": int,
    "voltage_in": float, "voltage_out": float,
    "current_in": float, "current_out": float,
    "power": float, "performance": float | None,
    "temperature": float, "dc_dc_duty_cycle": float,  # 0.0–1.0
    "rssi": int,
    "high_output_mode": bool, "status_code": int, "low_voltage_flag": bool,
    "reading_fault": False, "fault_reasons": [], "last_fault_at": str | None,
    "daily_energy_wh": float, "total_energy_wh": float,
    "readings_today": int, "daily_reset_date": str,
    "last_update": str,          # ISO 8601
    "report_monotonic": float,   # session-only
    # "topology": {...}          # kept from the previous node if present
}
```

In averaged mode the `_AVERAGED_FIELDS` are appended to `_reading_buffers[barcode]`.

**`_handle_infrastructure(event) → bool`:** replaces `gateways`; with a node table, rebuilds `barcode ↔ node_id` from scratch, logs/admits unconfigured barcodes, resets the pending counter, logs configured barcodes missing from the table, and saves on change. Gateway-only events keep existing mappings.

#### Module Discovery

`_add_discovered_module(barcode, node_id, restored)` returns `False` for configured or excluded barcodes, when discovery is off (unless restoring), or when `BarcodeClaims.claim()` finds another owner (logged once). Claims live in `hass.data` and are atomic across workers. A separate configured-ownership snapshot, refreshed on the event loop by `SIGNAL_CONFIG_ENTRY_CHANGED`, reserves unloaded entries' barcodes too. `release_barcode_claims()` releases active claims after worker shutdown, not configured reservations. A manual duplicate is removed from only the conflicting coordinator's active module set; healthy modules remain. Otherwise discovery adds `{"name": "Optimizer <barcode>", "string": "Discovered", "peak_power": 0}` (restored metadata overrides), a baseline node, marks a full update and schedules a save. Entities are created by the platform listeners on the next update.

Options Save and close reconciles its initial module snapshot with current
entry/runtime data. Only fields edited in that form overwrite current values;
external deletions, exclusions, unrelated edits and discovery additions are
preserved. An explicit re-add can clear an exclusion.

#### Reporting Modes

- **Live (`live_reporting = True`)** — `_push_live_update()` publishes a snapshot whenever events changed data.
- **Averaged (`live_reporting = False`)** — `_build_averaged_snapshot(flush_ts)` computes a time-weighted average per field from the buffer, recomputes `performance`, and keeps previously published averages for nodes without new readings (taking energy/status/fault fields from the worker). The buffer is cleared after each flush.

#### Stale Readings

- `seconds_since_report(barcode)` — from the published node's `report_monotonic`.
- `is_reporting(barcode)` — `age < stale_timeout` (+ `write_interval` in averaged mode).
- `_async_check_freshness()` — every 5 s; computes the barcodes whose reporting state changed since the last publish or check and calls only the callbacks registered for them via `async_add_freshness_listener(barcodes, callback)` (each registration at most once). Live per-optimizer sensors/binary sensors register their own barcode; aggregates register their members and re-register in `set_barcodes()` when membership changes. Energy-only per-optimizer sensors don't register. (Data listeners, see above, are registered by every per-optimizer entity and aggregate.)
- `stale_action` / `stale_timeout` properties are read by the entity platforms.

#### Midnight Reset

`_perform_midnight_reset()` zeroes `daily_energy_wh` / `readings_today` in the accumulators and worker nodes (skipping those already reset today), saves, and publishes a copy of `self.data` with only those daily fields reset, so averaged values aren't replaced by raw ones.

#### Persistence

One HA Store (`pytap_<entry_id>_coordinator`, version 2; `_MigratingStore` accepts v1 data as-is) containing `auto_modules`, `barcode_to_node`, `discovered_barcodes`, `parser_state`, `energy_data` and `node_snapshots` (`_SNAPSHOT_FIELDS`: measurements, status flags, energy, `last_update`, `topology`, `reading_fault`, `fault_reasons`, `last_fault_at`). On load, discovered modules are restored before snapshots; energy accumulators from a previous day are reset; performance is recomputed from the current rating. `_schedule_save()` (thread-safe; posts to the loop only for the first change of each save cycle) writes at most once per 10 s; stop flushes pending changes. Serialisation happens under `_state_lock`. If `async_load()` raises, `_save_blocked` is set (WARNING), so nothing is written for the coordinator's lifetime and setup skips orphan cleanup; `store_loaded` is then `False`. `store_restored` is `True` only when saved data was actually present (a corrupt file renamed by HA loads as `None`).

`_init_mappings_from_parser()` pre-populates mappings from the parser's node tables on each (re)connect; saved coordinator mappings are kept when the parser has none.

#### Packet Capture

`async_start_capture`, `async_stop_capture`, `async_capture_info`, `async_list_captures`, `async_export_capture`, `async_delete_capture` operate on `<config>/pytap_captures/<entry_id>` under `_capture_lock`. The trusted boundary is `hass.config.config_dir`: symlinks below it are rejected for creation, listing, resolution, export, deletion and entry removal; symlinked ancestors above it are allowed. Refused external paths are never followed, deleted or migrated. Start validates 1–10080 minutes and 16–4096 MiB (default 1024), and refuses while the listener is stopped or another capture/export/delete runs (including one left running by an unloaded coordinator: jobs are registered per capture root with `track_capture_job()`). Export refuses while recording, defaults its budget to the capture's own (`max_storage_bytes` from its manifest), and runs as a shielded executor job that does not block unload. Delete refuses the active session. `capture_status` (for diagnostics) never includes the directory; action responses do.

---

### `energy.py` — Energy Accumulation

Pure-logic module implementing trapezoidal energy integration, HA-independent.

**`EnergyAccumulator`** — `daily_energy_wh`, `total_energy_wh`, `daily_reset_date`, `last_power_w`, `last_reading_ts`, `readings_today`.

**`accumulate_energy(acc, power, now, …) → EnergyUpdateResult`:**
1. Clamps power to non-negative.
2. Resets daily values on date change.
3. Integrates `((prev + power) / 2) × (Δt / 3600)` when `0 < Δt ≤ gap_threshold`.
4. Flags longer gaps during production as discarded.
5. Increments `readings_today` and updates `last_power_w` / `last_reading_ts`.

The coordinator calls it only for plausible reports. Daily values are also reset proactively at local midnight (see above).

---

### `sensor.py` — Sensor Platform

#### Sensor Descriptions

`PyTapSensorEntityDescription` adds `value_key`, `live` (follows the stale action; `False` for energy and readings today) and `write_on_change` (status code). Keys: `performance`, `power`, `voltage_in`, `voltage_out`, `current_in`, `current_out`, `temperature`, `dc_dc_duty_cycle`, `rssi`, `daily_energy` (`TOTAL`), `total_energy` (`TOTAL_INCREASING`), `readings_today` (diagnostic, `TOTAL`), `status_code` (diagnostic). Aggregate descriptions: `performance`, `power`, `daily_energy`, `total_energy` for strings and the installation.

#### Entity Creation

`async_setup_entry()` registers `add_discovered_entities` as a coordinator listener and calls it once. It adds the 13 sensors for each barcode not yet known, creates string aggregates for new strings and installation aggregates once any barcode exists, and updates aggregate membership (`set_barcodes`) on every change. `PyTapDiscoverySensor` (`module_discovery`) is added when `auto_discovery` is on. For two modules on two strings: 26 optimizer sensors + 8 string + 4 installation sensors (+1 discovery sensor), plus 6 binary sensors.

#### `PyTapSensor`

- `unique_id`: `pytap_<barcode>_<key>`; device from `entity.module_device_info()`.
- **Restore:** coordinator value if present, else `RestoreSensor` data or last state; performance stays `None` when the rating is 0.
- **Availability:** see the table in [architecture.md](architecture.md#entity-availability). `ZERO_WHEN_STALE` = power, voltage in/out, current in/out, performance.
- **Values:** duty cycle × 100; status code formatted `0x%02X`; zero substituted when stale in `zero` mode.
- **Attributes:** `string_group`, `last_update` (not for the status code), `gateway_id`.
- **`last_reset`:** daily energy and readings today use `daily_reset_date`.

#### `PyTapAggregateSensor`

- Live values (power, performance) use `_members()`: all members with data in `hold`, or only reporting ones otherwise; `zero` mode reports 0 when none report. In `zero` mode performance divides by the rating of every member with data, counting stale members as 0 W. Performance is `None` if any member is unrated.
- Energy values accumulate production against per-member `baselines`, stored with the total and `cycle_date` as `_AggregateEnergyData`. For `v >= b`, credit `v - b` and advance `b`. For `0.5 * b <= v < b`, including the boundary, credit nothing and preserve `b` as a held baseline across restores; recovery above `b` credits only the excess. For `v < 0.5 * b`, credit `v` and rebase as a genuine reset. New/re-added members initially rebase without credit. Daily updates require a matching `daily_reset_date`; a stale tracked member retains zero as its baseline, while an uninitialized member stays uninitialized. Restore advances stale cycles without waiting for node data and prunes removed members. Temporarily omitted current members retain their baselines. A daily rollover zeroes held baselines. Unattached optimizer/aggregate sensors use the coordinator's HA timezone.
- Coordinator counters and aggregate baselines persist separately. Residual 50%-rule risks are lost held skew, near-zero-baseline or large-loss misclassification as resets (overcounting by the post-loss value), and genuine resets of at least half the baseline being held. Exact #39 rollback from a held baseline can re-credit at most the held gap and never a full member value.
- Attributes: `optimizer_count` is the configured member count; `reporting_count` counts present members reporting inside the stale timeout plus the averaged interval, where enabled. It can be zero overnight or after restart, including in hold mode. Daily `last_reset` is the HA-local start of `cycle_date`.

---

### `binary_sensor.py` — Binary Sensor Platform

Same dynamic creation as the sensor platform. Descriptions: `high_output_mode`, `low_voltage_flag` (live) and `reading_fault` (`PROBLEM`, not live). All are diagnostic, keyed `pytap_<barcode>_<key>`, and write state only when `(available, is_on, attributes)` changes. Live flags are available while stale only with `hold`. `reading_fault` exposes `fault_reasons` and `last_fault_at`; it is unavailable while the panel has no node and `None` (unknown) while its node has no fault result yet (e.g. a newly discovered panel).

---

### `capture_services.py` and `pytap/core/capture.py` — Packet Capture

**Actions** (registered once in `async_setup`, admin-only, `SupportsResponse.OPTIONAL`): `start_capture` (`duration_minutes` 1–10080, default 1440; `max_storage_mib` 16–4096, default 1024), `stop_capture`, `capture_status` / `export_capture` (optional `capture_id`; export also optional `max_storage_mib`), `list_captures`, `delete_capture` (required `capture_id`). `CaptureError` becomes a translated `ServiceValidationError`, as do other `ValueError`s from invalid evidence (`capture_invalid`); `CaptureRuntimeError` (e.g. `export_insufficient_space`) and `OSError`/`KeyError` (`capture_failed`, with only the exception type) become `HomeAssistantError`. `async_remove_captures()` deletes the entry's capture root on entry removal; with an export/delete job still running it waits up to 30 s (`async_wait_for_capture_jobs()`) and otherwise defers deletion until the job ends.

**`CaptureWriter`:**

- `start()` (executor) creates private directories/files (0700/0600), computes the session budget from `max_storage_bytes` minus existing usage (captures and ZIPs) and free disk space, writes the first manifest, then starts the writer thread.
- `record()` (worker thread) never blocks: it copies the read into a bounded queue, or stops with `queue_overflow` and counts the loss. The first record stores a `PersistentState` snapshot in the metadata.
- `mark()` queues connection events (`events.jsonl`).
- The writer thread writes `bus-NNNNNN.bin` / `chunks-NNNNNN.bin` (`<QQQII` index), rotates at 16 MiB, stops on `duration_limit`, `storage_limit`, `disk_space_low` or `disk_error`, and rewrites `manifest.json` only when it changed (at most every 10 s, plus rotation/stop) after fsyncing the data files and before fsyncing the directory. SHA-256 checksums cover every segment, index and `events.jsonl`.

**Reading and export:** `read_manifest()` validates structure (format 1 or 2; format 2 requires the `events` block; a too deeply nested manifest raises `ValueError`) and refuses incomplete captures unless allowed. `iter_capture()` verifies sizes/checksums (strict for complete captures; a format 1 event log only gets a warning, as it has no checksum) and streams `CapturedChunk`s; for incomplete evidence it warns, stops at an invalid index row, and never replays unindexed tails. `export_archive()` enforces the trusted boundary, validates, and checks the estimated ZIP size against the budget left by everything else under the root (the exported capture excluded; `export_budget_exceeded`) and against free disk space minus 64 MiB (`CaptureRuntimeError`, `export_insufficient_space`); both limits are re-checked while writing. The source plus its archive may therefore use approximately twice the selected budget; this is not a combined hard cap. Inputs are opened with `O_NOFOLLOW` (relative to the session directory where supported) and must be regular files. It writes `<capture_id>-<hex>.zip` next to the session; incomplete captures also get unlisted crash segments, and archives that are incomplete or have warnings/unverified files (e.g. a format 1 event log) get `recovery.json` (`verified`, `format_version`, `validation_warnings`, `unverified_files`). `delete_capture()` validates every entry before deleting the session and its ZIPs, and also deletes ZIPs whose session directory is gone. `list_captures()` returns the latest 20. No automatic pruning or HA backup-policy changes are introduced.

---

### `pytap/core/replay.py` and `scripts/replay_capture.py` — Offline Replay

- **`FrameScanner`** splits chunks into `frame`, `noise`, `incomplete` and `oversized_fragment` records without crossing connections or gaps. Frames are decoded by `decode_wire_frame()` (escapes, CRC, receive/command headers, power-report measurements including raw unknown/extra bytes) and flagged `giant` above the live parser's size limit (also when an invalid escape follows). A mirror of the live framing state machine sets `live_parser_would_drop` and `live_parser_drop_reason` (`framing_state`, `unknown_escape`, `giant`, `runt`, `crc_mismatch`). With `started_mid_connection` (from the manifest metadata), or after lost bytes within one connection, it tracks every possible starting state until they converge; frames completed before then get `live_parser_would_drop: None` with reason `unknown_at_capture_start` / `unknown_after_capture_gap`.
- **`replay_events(chunks, state)`** runs `Parser(persistent_state=state, clock=…)` with each chunk's recorded UTC time as the clock, starting a new parser on connection changes or gaps.
- **`scripts/replay_capture.py <session> --output <new-dir> [--allow-incomplete]`** validates the capture first, then writes `chunks.jsonl`, `frames.jsonl`, `events.jsonl` (bootstrapped from the manifest's parser state) and `summary.json` (including `format_version`, `frames_live_parser_would_drop`, `frames_live_parser_verdict_unknown`, and `integrity_verified`, true only for complete format 2 captures). It never modifies the source or overwrites output.

---

### `diagnostics.py` — Diagnostics Platform

`async_get_config_entry_diagnostics()` returns:

- `config_entry` — with `host`, `serial_port` and `title` redacted.
- `modules`, `counters` (parser counters, including `invalid_slots`, `invalid_slot_frames_kept`, `invalid_slot_frames_dropped` and `no_slot_clock_drops`), `gateways`, `discovered_barcodes`.
- `nodes` — per barcode: `last_update`, `gateway_id`, `node_id`, `reporting`, `seconds_since_report`, `status_code`, `reading_fault`, `fault_reasons`, `last_fault_at`, daily/total energy, `readings_today`.
- From `get_diagnostics_data()` (also redacted): `packet_capture` (status without directory), `node_mappings`, `connection_state` (incl. transport type, baud rate, `connected`, `retries`), `module_discovery`, `energy_state`.

---

### `__init__.py` — Integration Lifecycle

#### Config Entry Version

`CONFIG_ENTRY_VERSION = 7` (matches `PyTapConfigFlow.VERSION`):

- `v1 → v2`: voltage/current split to `_in`/`_out`
- `v2 → v3`: module string labels mandatory (missing → `"Default"`)
- `v3 → v4`: `peak_power` added (455 Wp)
- `v4 → v5`: `live_reporting` added (`True`); `write_interval` clamped to 5–300
- `v5 → v6`: `connection_type = "tcp"`
- `v6 → v7`: `auto_discovery` (`False` if missing) and `excluded_barcodes` (`[]`)

Entries newer than v7 fail migration.

#### `async_setup_entry(hass, entry) → bool`

1. Waits for a previous coordinator of this entry found among the parked coordinators (`parked_coordinators()`, i.e. `hass.data[DOMAIN]`, which only holds coordinators whose stop timed out) and releases its barcode claims; raises `ConfigEntryNotReady` (`previous_listener_stopping`) if it still hasn't stopped.
2. `_async_cleanup_legacy_entities()`.
3. Creates the coordinator; `InvalidConnectionSettings` → `ConfigEntryError` (`invalid_connection_settings`, naming the setting).
4. `async_config_entry_first_refresh()`, `async_start_listener()`.
5. Stores the coordinator in `entry.runtime_data` (`PyTapConfigEntry = ConfigEntry[PyTapDataUpdateCoordinator]`, defined in `coordinator.py`). Platforms, diagnostics, capture actions, the options flow and device removal read it from there (the latter two only while the entry is loaded).
6. Listens once for `EVENT_HOMEASSISTANT_STOP` to stop the listener and finalise captures (the listener is removed again on unload).
7. If the Store was read without error, `_async_remove_orphaned_devices()` removes (`async_remove_device`) devices whose barcodes are all in `excluded_barcodes` and no longer tracked, and string-only devices of strings without modules (unless discovery is on and no saved state was restored). Other unknown optimizer devices are kept.
8. Forwards `Platform.BINARY_SENSOR` and `Platform.SENSOR`, and registers `_async_update_options` (reload).

If steps 4–8 raise, `_async_stop_or_park()` stops the listener (or parks it) before re-raising.

#### `async_unload_entry(hass, entry) → bool`

`_async_stop_or_park()` stops the listener and releases its barcode claims, or, if it didn't stop in time, parks the coordinator (with a warning) so the next setup waits for it and config flows treat its endpoint as in use; once its worker exits it finishes its stop and removes itself. Then unloads platforms.

#### `async_remove_entry` / `async_remove_config_entry_device`

Removing the entry first gives a parked coordinator a bounded stop with `discard_state=True` (a still-stuck one stays parked, so its port stays claimed), then deletes its captures and Store. Deleting an optimizer device (any identifier not prefixed with the entry ID, so discovered optimizers too, even while not loaded) removes the module if configured and excludes its barcode; empty string devices may be deleted; the installation device and strings still in use may not.

---

### `strings.json` / Translations — UI Strings

- **Config steps:** `user` (menu), `tcp`, `serial`, `modules_menu`, `discovery`, `add_module`, `finish`.
- **Options steps:** `init`, `change_discovery`, `edit_module`, `module_details`, `change_connection` (menu), `change_tcp`, `change_serial`, `add_module`, `remove_module`, `change_reporting`, `set_interval`, `change_stale`.
- **Errors:** `cannot_connect`, `invalid_barcode`, `missing_string`, `missing_name`, `missing_barcode`, `duplicate_barcode`, `no_modules`, `invalid_host`, `invalid_port`, `invalid_serial_port`, `invalid_baud_rate`, `invalid_connection_type`, `serial_port_in_use`, `connection_in_use`, `unsupported_serial_settings`.
- **Entity names:** all sensors and binary sensors, plus `module_discovery` states.
- **Selector:** `stale_action` options (Unavailable, Zero, Hold last value).
- **Exceptions:** setup errors (`previous_listener_stopping`, `invalid_connection_settings`) and capture action errors (including `capture_failed`, `capture_invalid` and `export_insufficient_space`).

`translations/en.json` mirrors `strings.json` exactly.

---

## Config Flow UX Design

The config flow uses a **menu-driven approach**: choose a transport, enter connection details, then either finish (automatic discovery) or add modules one at a time.

### Rationale

1. **Lower error rate** — Each field is validated directly.
2. **Per-field errors** — Validation issues are shown on the relevant field.
3. **Better discoverability** — Users are guided through the expected inputs.
4. **Inline help** — Each field can include a focused description.

### Current UX Flow

```
1. Choose TCP gateway or Serial / Home Assistant proxy
2. Enter host/port, or pick the serial port and baud rate
   → Bounded probe; on failure "Could not connect" — submit again to save anyway
3. Modules menu (discovery mode: Automatic by default)
   [Add a module]  [Module discovery settings]  [Finish setup]
4. Optionally add modules (string group, name, barcode, peak power)
5. Finish setup → entry created; with discovery on, optimizers appear as they are seen
```

### Connection Validation

An unreachable endpoint (common at night) shows `cannot_connect` once. Submitting the identical settings again saves them, and the listener keeps retrying in the background.

---

## Data Flow

```
Tigo Gateway (TCP / serial / ESPHome proxy)
    │
    ▼
source.read(4096)                          [executor thread]
    │
    ├── capture.record(bytes)  (optional)
    ▼
Parser.feed(bytes) → list[Event]           [executor thread]
    │
    ▼
FOR EACH event (under _state_lock):        [executor thread]
    coordinator._process_event(event) → bool
    │   ├── configured/discovered → plausibility check → update _state["nodes"]
    │   └── unconfigured          → log, remember, maybe admit
    ▼
publish copy (live: immediately; averaged: once per write_interval)
    hass.loop.call_soon_threadsafe(async_set_updated_data, snapshot)
    │
    ▼
CoordinatorEntity._handle_coordinator_update()  [main event loop]
    ├── read data["nodes"][barcode][value_key]
    ├── apply stale action (is_reporting), convert duty cycle / status code
    └── async_write_ha_state() (status entities: only on change)
    │
    ▼
Home Assistant frontend / automations / history
```

---

## Testing

### Test Configuration

- **Framework:** pytest with `pytest-homeassistant-custom-component` (pinned in `requirements-test.txt`; currently targets HA 2026.10.4).
- **Async mode:** `asyncio_mode = auto` (in `pytest.ini`).
- **Smoke tests:** `tests/core_smoke.py` runs through `scripts/run_core_smoke.py` on HA 2026.10.4 and 2026.11.2. The runner rejects zero tests, skips, failures, errors, expected failures and unexpected successes. It includes real public API/transport checks and subprocess-isolated exact-#39 persisted-state rollback.
- **Dependencies:** CI downloads the running Core's official constraints, installs Core (and the pytest harness only on 2026.10.4), then runs `scripts/prepare_ha_tests.py`. It follows both manifest dependency kinds from ESPHome, USB, Zeroconf and Assist Satellite, checks every applicable resolved requirement, runs `pip check`, and verifies real module/API origins. The 2026.11.2 environment deliberately has no harness.

### Integration Tests (`tests/`)

| File                              | What it covers                                                                                                                                                            |
| --- | --- |
| `test_config_flow.py`             | Menu-driven config and options flows, module validation                                                                                                                   |
| `test_serial_config_flow.py`      | Serial picker, offline setup, transport changes                                                                                                                           |
| `test_connection.py`              | Transport validation, passive reads, bounded cleanup                                                                                                                      |
| `test_coordinator_persistence.py` | Store load/save, mappings, deferred reports, midnight reset                                                                                                               |
| `test_write_interval.py`          | Live vs. averaged reporting and time-weighted averaging                                                                                                                   |
| `test_panel_status.py`            | Stale-reading actions, status entities, reading faults, aggregate energy                                                                                                  |
| `test_freshness_listeners.py`     | Freshness transitions re-render only the affected panels and aggregates; listener registration, membership changes and unload                                             |
| `test_review2_data.py`            | Default stale action, domain-wide barcode claims, selective re-rendering on publish, midnight-reset ordering, save scheduling, aggregate performance and restore          |
| `test_review2_lifecycle.py`       | Orphan-device cleanup, unreadable Store, device deletion, serial path normalisation/character-device checks, parked listeners, entry removal, setup recovery, outage logs |
| `test_review2_capture.py`         | Domain-wide capture jobs, orphan archives, export budget/disk space, format 1/2, nested manifests, unknown replay verdicts, error types, symlink-safe export inputs       |
| `test_module_discovery.py`        | Passive admission, persistence, live entities, metadata edits                                                                                                             |
| `test_listener_lifecycle.py`      | Worker finishes and releases the port before unload/reload                                                                                                                |
| `test_review_fixes.py`            | TCP interrupt, slow stop, shutdown capture finalisation, invalid saved port, cross-entry discovery, device removal, `/dev/` paths, symlinks, one-time connection-loss log |
| `test_packet_capture.py`          | Raw fidelity, bounded I/O, replay, export                                                                                                                                 |
| `test_capture_integrity.py`       | Durability, manifest validation, deletion                                                                                                                                 |
| `test_capture_scanner.py`         | `FrameScanner` verdicts match `Parser.feed`                                                                                                                               |
| `test_capture_services.py`        | Capture actions, lifecycle, privacy, capture-before-parse ordering                                                                                                        |
| `test_sensor.py`                  | Sensor platform entities, values, aggregates                                                                                                                              |
| `test_diagnostics.py`             | Diagnostics payload and redaction                                                                                                                                         |
| `test_energy.py`                  | Pure energy accumulation                                                                                                                                                  |
| `test_migration.py`               | Config entry migrations and legacy entity cleanup                                                                                                                         |

### Parser Library Tests (in `custom_components/pytap/pytap/tests/`)

| Test File | Coverage |
| --- | --- |
| `test_parser.py` | Byte-level protocol parsing with captured data, including invalid slot counters |
| `test_types.py` | Protocol type construction and field validation |
| `test_crc.py` | CRC-16 calculation against known vectors |
| `test_barcode.py` | Barcode encode/decode round-trips |
| `test_api.py` | Public API function surface tests |

### Running Tests

```bash
pip install -r requirements-test.txt

# Integration tests
python3 -m pytest tests/ -vv --tb=short

# Parser library tests
PYTHONPATH=custom_components/pytap python3 -c 'import pytap, pytest; raise SystemExit(pytest.main(["custom_components/pytap/pytap/tests/", "--import-mode=importlib", "-vv"]))'

# Lint
python3 -m ruff check custom_components/pytap/ tests/ scripts/
```

---

## Design Decisions & Trade-offs

### 1. Menu-Driven Module Input

**Chosen:** Individual form-per-module with a menu loop, with automatic discovery so most users don't need to enter modules at all.

**Trade-off:** More steps for manual entry, but per-field validation and HA-native forms.

### 2. Probe Once, Save on Resubmit

**Chosen:** A failed connection probe shows `cannot_connect`; resubmitting the same settings saves them.

**Rationale:** Users often configure the integration while the gateway is offline (e.g. at night). Typos are caught, but an offline device doesn't prevent saving.

### 3. Push-Based Coordinator (Not Poll-Based)

**Chosen:** Background streaming task with `call_soon_threadsafe` dispatch. Sub-second latency, no redundant requests, matches the bus protocol.

### 4. Executor Thread Bridging With Private Worker State

**Chosen:** Run blocking `_listen()` in HA's executor; the worker owns `_state` and publishes copies.

**Rationale:** The library stays portable; copies avoid races and keep averaged values intact.

### 5. Barcode as Primary Key (Not node_id)

`node_id` is a transient 16-bit integer; barcodes are stable hardware identifiers, so all unique IDs, device identifiers and data keys use the barcode.

### 6. Passive Discovery With Exclusions

**Chosen:** New setups admit optimizers seen on the bus (no bus commands); explicit removals/deletions are excluded; barcodes owned by another entry are skipped; a manual mode remains for shared buses. Discovered metadata lives in the Store so admission doesn't reload the entry.

### 7. Configurable Stale Readings

**Chosen:** Default `hold` (the original behaviour, for new and existing entries), with opt-in `unavailable` and `zero` after a timeout (default 120 s). Energy is never affected; faults don't refresh freshness.

### 8. Integration Reload on Options Change

**Chosen:** `_async_update_options` triggers a full `async_reload`. Brief disruption, but entities always match the configuration.

---

## Known Deviations from Architecture

| Aspect                                         | Status                                                                                           |
| ---------------------------------------------- | ------------------------------------------------------------------------------------------------ |
| Gateway device registration / `via_device`     | Not implemented (optimizer, string and installation devices only)                                |
| Connectivity binary sensors                    | Not implemented (binary sensors cover status flags and reading faults)                           |
| SerialSource (pyserial) in `pytap.core.source` | Unused by the integration; serial access goes through `connection.SerialProxySource` (`serialx`) |

---

## Development History

### Phase 1 — Architecture

Created `docs/architecture.md` capturing the full design: system context, module responsibilities, data flow, threading model, entity model, and configuration schema.

### Phase 2 — Barcode-Driven Design

Updated the architecture to remove auto-discovery in favor of user-configured barcodes, inspired by the [taptap HA add-on](https://github.com/litinoveweedle/hassio-addons).

### Phase 3 — Initial Implementation

Implemented all core files:
- `const.py`, `manifest.json` — Constants and metadata.
- `config_flow.py` — Menu-driven flow (host/port → modules menu → add/remove modules).
- `coordinator.py` — Push-based streaming with barcode filtering.
- `sensor.py` — 12 per-optimizer sensor types plus aggregate sensor platform entities.
- `__init__.py` — Lifecycle management.
- `strings.json`, `translations/en.json` — UI strings.
- Test suite — 14 tests passing.

### Phase 4 — Connection Test Fix

Discovered that the TCP connection test in step 1 blocked the flow when no gateway was available (the common case during development). Changed the connection test to non-blocking: it warns but always proceeds to the modules step.

### Phase 5 — Menu-Driven Config Flow

Refined the config flow to the current menu-driven approach:
- Individual `add_module` form with string/name/barcode fields.
- Menu loop for adding multiple modules.
- Options flow with add/remove/done menu.
- Per-field error reporting (errors shown on the specific field).
- Dropdown-based module removal in options flow.
- All tests rewritten for the new flow pattern (16 tests passing).

### Phase 6 — Documentation

Created this implementation document capturing all development work to date.

### Phase 7 — Micro-Batching Fix & Timeout Removal

- Fixed micro-batching: `_listen()` was calling `async_set_updated_data` once per TCP read chunk. Changed to push per-event — each event that changes data triggers its own `async_set_updated_data` call.
- `_process_event()` and all handler methods now return `bool` indicating whether data changed.
- Removed `UNAVAILABLE_TIMEOUT` constant and all related logic. Sensors now hold their last received value indefinitely (no forced `None` after timeout).
- Updated `source.py`: `TcpSource.read()` now raises `OSError("Socket is closed")` when the socket is `None` and `ConnectionResetError` on peer close, instead of silently returning `b''`.

### Phase 8 — Entity Migration (v0.2.0)

- Split `voltage` → `voltage_in`/`voltage_out` and `current` → `current_in`/`current_out`, then expanded per-optimizer sensor count to 10 with daily/total energy.
- Added entity registry cleanup in `_async_cleanup_legacy_entities()` to remove orphaned `voltage`/`current` entities from pre-v0.2.0 installs.
- Bumped config entry version to 2 and added `async_migrate_entry()` for v1→v2 migration.
- Bumped manifest version to 0.2.0.
- Added 5 entity migration tests.

### Phase 9 — Shutdown & Threading Fixes

- Changed `_stop_event` from `asyncio.Event` to `threading.Event` — the former is not thread-safe across loops and caused HA shutdown to hang.
- Added `_source_lock` (`threading.Lock`) to protect concurrent `_source` access between the executor thread and the main loop's stop path.
- `async_stop_listener()` now uses `asyncio.timeout(5)` to prevent indefinite blocking if the listener task doesn't exit.
- Registered `coordinator.async_stop_listener` via `entry.async_on_unload()` so the listener is stopped on HA shutdown/reload, not just explicit unload.
- Added 17 coordinator persistence and lifecycle tests.

### Phase 10 — Barcode Persistence & Node Table Fix

- **Node table sentinel tolerance** — Fixed parser `_handle_node_table_command` to tolerate trailing bytes on the end-of-table sentinel page (`entries_count=0`). The gateway commonly sends padding/CRC bytes after the zero count, which was previously rejected as "corrupt", preventing the node table from completing and barcodes from being resolved.
- **Trailing-byte tolerance on data pages** — Data pages with more bytes than expected now parse the declared entries and ignore trailing bytes (changed strict equality check to minimum-length check).
- **First infrastructure event differentiation** — `_handle_infrastructure` now distinguishes between infra events with and without barcodes. The first event often arrives from gateway identity/version discovery before the node table is received. Previously this logged a misleading "0/N matched" WARNING; now it logs an INFO explaining that resolution will activate once the node table arrives.
- **Configured barcode mismatch logging** — Infrastructure events now log which specific configured barcodes are NOT found in the node table, helping users identify typos or incorrect barcodes.
- **Discovery persistence fix** — Discovered (unconfigured) barcodes from infrastructure events now properly trigger `_schedule_save()`. Previously only mapping changes triggered saves, leaving discovered barcodes unpersisted.
- **Coordinator-saved mapping preservation** — `_init_mappings_from_parser` now preserves coordinator-saved barcode↔node mappings when the parser state is empty (merge instead of replace). This prevents previously-learned mappings from being wiped on reconnect when the parser state file has no node table.
- **Instant barcode resolution on module add** — `reload_modules` now checks if newly-added barcodes already exist in the saved barcode↔node mapping and pre-populates placeholder node data so sensor entities can bind immediately without waiting for the next power report.

### Phase 11 — Node Address Bit-15 Masking

- **Node address bit-15 flag masking** — Fixed parser `_handle_node_table_command` to mask node addresses to 15 bits (`& 0x7FFF`) when parsing `NODE_TABLE_RESPONSE` entries. Bit 15 of the `NodeAddress` in node table entries is a protocol flag (indicating router/repeater status), not part of the node ID. Two nodes with barcodes `4-D39A3ES` and `4-D39CB6R` were observed with raw addresses `0x8019` (32793) and `0x801A` (32794) instead of the expected 25 and 26. Without masking, node table keys did not match the 15-bit node IDs used in power reports, causing those nodes' power data to be unresolvable to barcodes.
- **Debug logging for flagged nodes** — When bit 15 is detected on a node address, the parser now emits a `DEBUG`-level log with the raw and masked values for protocol analysis.
- **Test added** — `test_node_table_bit15_flag_masked` in `test_parser.py` verifies that addresses `0x8019`/`0x801A` resolve to node IDs 25/26 and not 32793/32794.

### Phase 12 — Storage Consolidation & CLI Removal

- **Consolidated storage** — Merged the parser's raw JSON state file and the coordinator's HA Store into a single `homeassistant.helpers.storage.Store` (version 2). The store now holds barcode↔node mappings, discovered barcodes, and parser infrastructure state (`PersistentState.to_dict()`). This eliminates raw file I/O, ensures proper HA backup inclusion, and enables automatic cleanup on config entry removal.
- **Parser decoupled from file I/O** — `Parser.__init__` now accepts an optional `PersistentState` object instead of a `state_file` path. The parser mutates the state in memory; the coordinator owns persistence.
- **`PersistentState` serialization** — Replaced `save(path)` / `load(path)` file I/O methods with `to_dict()` / `from_dict()` for JSON-compatible serialization via the HA Store.
- **`create_parser()` API updated** — Accepts `persistent_state: Optional[PersistentState]` instead of `state_file: str | Path | None`.
- **CLI removed** — Deleted `pytap/cli/` module, `pytap/setup.py`, and `pytap.egg-info/`. The library is now embedded-only, used exclusively through the HA integration coordinator.
- **`observe()` function removed** — The blocking streaming loop with callback was only used by the CLI. Callers now use `create_parser()` + `connect()` + manual `feed()` loop.
- **Store version bumped to 2** — Added migration path from v1 (barcode mappings + discovered barcodes only) to v2 (adds `parser_state`).
- **Tests updated** — Replaced `_state_file_path` assertions with `_persistent_state` checks, added `test_load_restores_parser_state` and `test_load_handles_corrupt_parser_state`. 51 tests passing.
- **Documentation updated** — All docs (README, architecture, implementation, API reference) updated to reflect the consolidated storage model and removed CLI.

### Phase 13 — Aggregate Sensors & Mandatory String Labels

- Added aggregate sensors for each string and for the full installation (`power`, `daily_energy`, `total_energy`).
- Added `PyTapAggregateSensor` and dedicated virtual devices (`Tigo String <name>`, `Tigo Installation`).
- Made module `string` mandatory in config and options flow (`missing_string` validation).
- Bumped config entry version to 3 with `v2 → v3` migration defaulting missing strings to `"Default"`.
- Added aggregate and migration tests to validate sums, IDs, availability, metadata, and migration behavior.

### Phase 14 — Performance Sensors & Peak Power Configuration

- Added `CONF_MODULE_PEAK_POWER` and `DEFAULT_PEAK_POWER` (455 Wp) constants to `const.py`.
- Extended `ADD_MODULE_SCHEMA` in `config_flow.py` with optional `peak_power` field (`vol.Range(min=1, max=1000)`).
- Updated `_modules_description` to display peak power per module.
- Bumped `PyTapConfigFlow.VERSION` and `CONFIG_ENTRY_VERSION` to 4, aligning the previously mismatched versions.
- Added v3→v4 migration in `async_migrate_entry` to backfill `DEFAULT_PEAK_POWER` on existing modules.
- Extended `coordinator._handle_power_report` to compute `performance = (power / peak_power) × 100` with defensive parsing and fallback for invalid peak_power values.
- Added `peak_power` and `performance` to coordinator node data dict and `reload_modules` placeholder.
- Added `performance` sensor description to all three description tuples (`SENSOR_DESCRIPTIONS`, `STRING_SENSOR_DESCRIPTIONS`, `INSTALLATION_SENSOR_DESCRIPTIONS`).
- Implemented capacity-weighted aggregate performance in `PyTapAggregateSensor._handle_coordinator_update` (sensor-side).
- Updated entity count formula: `M × 11 + S × 4 + 4` (was `M × 10 + S × 3 + 3`).
- Added 3 sensor translation keys (`performance`, `string_performance`, `installation_performance`) and `peak_power` config field to `strings.json` and `translations/en.json`.
- Added tests: v3→v4 migration (3 tests), config flow peak_power (2 tests), sensor performance (6 tests), coordinator performance (3 tests).
- Bumped `manifest.json` version to 0.3.0.
- 99 tests passing, ruff clean.

### Phase 15 — Diagnostics Platform & Readings Counter

- Added `custom_components/pytap/diagnostics.py` with `async_get_config_entry_diagnostics`.
- Added coordinator `get_diagnostics_data()` snapshot including node mappings, connection state, and per-barcode accumulator summary.
- Redacted host/IP in diagnostics output while preserving barcodes for troubleshooting.
- Added per-optimizer `readings_today` as a diagnostic sensor (`SensorStateClass.TOTAL`, unitless).
- Extended `EnergyAccumulator` with `readings_today`, increment-on-report, and reset-on-new-day behavior.
- Persisted `readings_today` in coordinator store load/save, including date-rollover reset on restore.
- Added tests in `tests/test_diagnostics.py` and extended sensor/coordinator/energy tests.
- Full suite status after feature: 105 tests passing, ruff clean.

### Phase 16 — Proactive Midnight Daily Reset

- **Bug fix:** Daily sensors (`daily_energy`, `readings_today`) previously reset lazily on the first power report after midnight. Since solar panels produce no data overnight, the reset occurred at a random time in the morning when the first reading arrived.
- Added `_schedule_midnight_reset()` and `_perform_midnight_reset()` to the coordinator. A `call_later` timer on the HA event loop fires at exactly midnight local time, zeroes all daily accumulators, updates node data, pushes the update to sensors, and reschedules itself for the next midnight.
- `async_start_listener()` now schedules the midnight reset timer at startup.
- `async_stop_listener()` cancels the midnight reset timer on shutdown.
- Added 4 tests in `TestMidnightReset`: zeroing daily values, skip-if-already-reset, timer rescheduling, and cancellation on stop.

### Phase 17 — Live Reporting Toggle & Time-Weighted Averaging

- Added a `live_reporting` boolean config option (default `True`) and reworked the options flow's *Change reporting settings* step to present a live-reporting checkbox plus a reporting-interval field.
- In live mode the coordinator pushes every changed reading immediately; in interval mode it buffers per-barcode `(timestamp, fields)` readings and pushes a **time-weighted average** via `_build_averaged_snapshot(flush_ts)` once per `write_interval`.
- Replaced the previous arithmetic-mean averaging with time-weighted averaging (each reading weighted by the duration it remained the latest value), giving representative values when readings arrive at uneven intervals.
- Changed `DEFAULT_WRITE_INTERVAL` from 5 to 30 seconds and the interval range from 1–300 to 5–300.
- Bumped `CONFIG_ENTRY_VERSION` and `PyTapConfigFlow.VERSION` to 5 with a v4→v5 migration adding `live_reporting=True`.
- Added `tests/test_write_interval.py` covering reporting-mode init, throttle behaviour, and time-weighted averaging; updated migration, config-flow, and sensor tests.

### Phase 18 — Reporting Mode Hardening

- **Buffer leak fix** — `_handle_power_report` now only appends to `_reading_buffers` in interval mode (`if not self._live_reporting`). Previously the buffer was populated in live mode too but never consumed, growing without bound.
- **Thread-safe live push** — extracted `_push_live_update()`, which publishes `{**self.data, "nodes": dict(self.data["nodes"])}` (an immutable copy of `nodes`) so HA-side consumers never iterate the dict while the listener thread mutates it. The reconnect/error `finally` flush uses the same detached-copy pattern.
- **Idle interval flush** — extracted `_maybe_flush_interval()` and call it from both the data and no-data branches of `_listen`, so buffered readings are flushed on schedule during production lulls instead of waiting for the next frame or a reconnect.
- **Migration clamp** — the v4→v5 migration now clamps any stale `write_interval` into `[MIN_WRITE_INTERVAL, MAX_WRITE_INTERVAL]` (5–300) so the reporting form stays submittable after upgrade. Added `MIN_WRITE_INTERVAL`/`MAX_WRITE_INTERVAL` constants shared by the migration and the config-flow schema.
- Added regression tests (`TestLivePushAndIdleFlush`, migration clamp tests) and corrected the previously misnamed live-mode buffer test. Full suite: 162 tests passing, ruff clean.

### Phase 19 — Serial and ESPHome Proxy Transports

- Added `connection.py` with `normalize_connection()`, `SerialProxySource` (`serialx`) and `open_source()`; the config and options flows gained a TCP/serial menu with the native serial-port picker.
- Config entry version 6 (`connection_type = "tcp"` for existing entries); HA 2026.11+ required.

### Phase 20 — Passive Module Discovery

- Automatic discovery (default for new setups), discovered-module metadata in the Store, module editing, and `excluded_barcodes` for explicit removals.
- Dynamic entity creation in the sensor platform; `module_discovery` status sensor.
- Config entry version 7 keeps manual mode on existing entries.

### Phase 21 — Packet Capture and Offline Replay

- `pytap/core/capture.py`, `capture_services.py` admin actions, `pytap/core/replay.py` and `scripts/replay_capture.py`.
- `Parser(clock=...)` / `create_parser(persistent_state, *, clock=None)` so replay uses recorded observation times.

### Phase 22 — Panel Status Entities and Reading Faults

- `PowerReport` / `PowerReportEvent` gained `high_output_mode` (byte 7 bit 7), `status_code` (byte 9) and `low_voltage_flag` (byte 9 bit 7) — meanings from captured-data correlation, not confirmed by Tigo.
- New `binary_sensor.py` platform (high output mode, low voltage flag, reading fault), `status_code` sensor, shared `entity.py`.
- `reading_faults()` plausibility check; impossible reports are discarded and flagged.

### Phase 23 — Stale Readings and Review Fixes

- Configurable stale-reading action/timeout (`change_stale` options step) replacing a fixed report timeout; freshness check every 5 s that re-renders only the entities of panels whose reporting state changed (per-barcode freshness listeners).
- Coordinator stored in `entry.runtime_data`; `hass.data[DOMAIN]` kept only as a registry of coordinators still stopping after a timed-out unload.
- Worker-owned `_state` with published copies; DST-safe midnight reset via `async_track_time_change`.
- Aggregate energy accumulates member increases with restored baselines.
- Status entities and reading fault write state only on change.
- Lifecycle: bounded stop with `ConfigEntryNotReady` retry, `TcpSource.interrupt()`, capture finalised on `EVENT_HOMEASSISTANT_STOP`, captures deleted with the entry.
- Devices: UI deletion of optimizer and empty string devices; orphaned devices removed at setup; discovery skips barcodes owned by another entry.
- Connection: `/dev/` paths only, symlink-aware port comparison, probe-then-resubmit, one-time connection-loss log, `ConfigEntryError` for invalid saved settings.
- Parser: an invalid header slot counter never updates the slot clock; power reports with invalid slot counters are dropped; both increment `invalid_slots` (refined in Phase 24).
- Capture: `delete_capture`, exports count toward the budget, manifests written on change with fsync ordering, `events.jsonl` checksum, recoverable exports of interrupted captures, `live_parser_would_drop` / `giant` inspection flags, translated action errors, no capture path in diagnostics.

### Phase 24 — Second Review Fixes

- Stale readings: default `hold` for all entries (`unavailable` / `zero` opt-in); in `zero` mode aggregate performance divides by every present member's rating.
- Publishing: per-barcode data listeners, so a publish re-renders only changed panels and their aggregates; snapshot sequence numbers protect the midnight reset.
- Discovery: domain-wide atomic `BarcodeClaims`; configured barcodes claimed at coordinator creation.
- Lifecycle: timed-out stops still cancel timers/saves; parked coordinators claim their endpoint in config flows and clean up when their worker exits; entry removal waits (bounded) for them and blocks their saves; setup failures stop or park the listener; corrected connection settings recover a failed setup; unreadable Store blocks saves and orphan cleanup; orphan cleanup only removes excluded optimizers and empty strings; device deletion always excludes the barcode; normalised serial paths and character-device checks; outage logged once until data flows.
- Parser: invalid-header-slot frames kept only when the strict packet walk succeeds (`invalid_slot_frames_kept` / `invalid_slot_frames_dropped`); `no_slot_clock_drops`, with the warning logged once per gateway per parser.
- Capture: domain-wide export/delete job tracking; orphan-archive deletion; export budget excludes the exported capture, defaults to its own budget and checks free disk space; manifest format 2 (format 1 readable); nested-manifest rejection; unknown replay verdicts after a mid-connection start or gap; `giant` for oversized frames with a bad escape; `HomeAssistantError` for runtime failures; symlink-safe export inputs.

---

## Future Work

Items identified but not yet implemented:

1. **Gateway device registration** — Create a device per gateway for the `via_device` hierarchy.
2. **Connectivity binary sensors** — Node connectivity and gateway online status.
