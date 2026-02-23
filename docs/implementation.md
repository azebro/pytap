# PyTap — Implementation Document

> Version 0.3.0 · Last updated: February 2026

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
   - [coordinator.py — Data Coordinator](#coordinatorpy--data-coordinator)
   - [energy.py — Energy Accumulation](#energypy--energy-accumulation)
   - [sensor.py — Sensor Platform](#sensorpy--sensor-platform)
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

PyTap is a Home Assistant custom component that passively monitors Tigo TAP solar energy systems. It connects to a Tigo gateway over TCP, parses the proprietary RS-485 bus protocol in real time using an embedded Python parser library, and exposes per-optimizer sensor entities in Home Assistant.

**Key characteristics of the current implementation:**

| Aspect | Implementation |
| --- | --- |
| Integration type | Hub (`integration_type: "hub"`) |
| Data delivery | Push-based streaming (`iot_class: "local_push"`) |
| Entity creation | Deterministic from user-configured barcode list (no auto-discovery) |
| Config flow | Menu-driven: add modules one at a time via individual form fields |
| Threading model | Blocking parser in executor thread, bridged to async event loop |
| External dependencies | None — parser library embedded, stdlib only |
| Sensor types | 12 per optimizer + aggregate sensors per string and per installation (performance, power, daily energy, total energy) |
| Test coverage | Expanded integration + parser coverage, including aggregate sensor, performance, and v3→v4 migration behavior |

---

## File Inventory

```
custom_components/pytap/
├── __init__.py          # ~187 lines — Integration lifecycle (setup, teardown, migration, options listener)
├── config_flow.py       # 369 lines  — Menu-driven config & options flows
├── const.py             # ~28 lines  — Domain, config keys, defaults, energy tuning constants
├── coordinator.py       # ~826 lines — Push-based DataUpdateCoordinator
├── diagnostics.py       # ~46 lines  — Diagnostics download (config entry diagnostics)
├── energy.py            # ~80 lines  — Pure trapezoidal energy accumulation helpers
├── manifest.json        # 13 lines   — HA integration metadata
├── sensor.py            # ~572 lines — 12 sensor entity types, CoordinatorEntity pattern
├── strings.json         # ~100 lines — UI strings (source of truth)
├── translations/
│   └── en.json          # ~100 lines — English translations (mirrors strings.json)
└── pytap/               # Embedded protocol parser library (persistence decoupled)
    ├── api.py           # Public API: connect(), create_parser(), parse_bytes()
    └── core/
        ├── parser.py    # Protocol parser: bytes → events
        ├── types.py     # Protocol constants & frame types
        ├── events.py    # Event dataclasses (PowerReportEvent, etc.)
        ├── state.py     # SlotClock, NodeTableBuilder, PersistentState (to_dict/from_dict)
        ├── source.py    # TcpSource, SerialSource
        ├── crc.py       # CRC-16-CCITT
        └── barcode.py   # Tigo barcode encode/decode

tests/
├── conftest.py                    # 14 lines  — Auto-enable custom integrations fixture
├── test_config_flow.py            # 548 lines — 16 config flow tests
├── test_coordinator_persistence.py # ~729 lines — 32 coordinator & persistence tests
├── test_diagnostics.py            # ~172 lines — 4 diagnostics platform tests
├── test_energy.py                 # ~170 lines — 13 pure energy accumulation tests
├── test_migration.py              # ~300 lines — 11 entity migration tests
└── test_sensor.py                 # ~875 lines — 32 sensor platform tests

docs/
├── architecture.md      # 656 lines — Architecture & design document
└── implementation.md    # This file
```

---

## Module Details

### `const.py` — Constants

Defines all integration-wide constants in a single location:

```python
DOMAIN = "pytap"
DEFAULT_PORT = 502                # Tigo gateway default TCP port

# Config entry data keys
CONF_MODULES = "modules"          # List of module dicts in ConfigEntry.data
CONF_MODULE_STRING = "string"     # Required string/group label
CONF_MODULE_NAME = "name"         # User-friendly optimizer name
CONF_MODULE_BARCODE = "barcode"   # Tigo hardware barcode (stable ID)
CONF_MODULE_PEAK_POWER = "peak_power"  # Peak panel power in Wp

# Defaults
DEFAULT_PEAK_POWER = 455          # Wp (watts peak) — STC rating

# Reconnection tuning
RECONNECT_TIMEOUT = 60            # Seconds of silence → reconnect
RECONNECT_DELAY = 5               # Pause between reconnection attempts
RECONNECT_RETRIES = 0             # 0 = infinite retries
```

These constants are imported by every other module in the integration.

---

### `manifest.json` — Integration Metadata

```json
{
  "domain": "pytap",
  "name": "PyTap",
  "codeowners": ["@azebro"],
  "config_flow": true,
  "documentation": "https://github.com/azebro/pytap",
  "integration_type": "hub",
  "iot_class": "local_push",
  "issue_tracker": "https://github.com/azebro/pytap/issues",
  "requirements": [],
  "version": "0.3.0"
}
```

Key choices:
- **`integration_type: "hub"`** — One gateway entry manages multiple downstream optimizer devices.
- **`iot_class: "local_push"`** — Data streams from the gateway in real time; no polling interval.
- **`requirements: []`** — The pytap parser library is embedded, not installed from PyPI.

---

### `config_flow.py` — Configuration Flow

**369 lines** implementing a menu-driven config flow and a full options flow.

#### Config Flow Steps

```
┌─────────────┐     ┌──────────────────┐     ┌──────────────┐
│  Step: user │────►│ Step: modules_   │────►│ Step: add_   │
│  (host/port)│     │ menu (menu)      │◄────│ module (form)│
└─────────────┘     │                  │     └──────────────┘
                    │  ► Add module    │
                    │  ► Finish setup  │──── CREATE_ENTRY
                    └──────────────────┘
```

1. **`async_step_user`** — Collects `host` (required) and `port` (default 502). Sets a unique ID of `host:port` and aborts if already configured. Performs a non-blocking TCP connection test — warns on failure but always proceeds to the modules menu.

2. **`async_step_modules_menu`** — Shows a menu with two options: "Add a module" and "Finish setup". Displays the current module list via `_modules_description()`. If the user selects "Finish" with no modules added, the menu re-displays (guard against empty config).

3. **`async_step_add_module`** — Form with three fields:
   - **String group** (`string`) — Optional grouping label (e.g., "A", "East").
   - **Name** (`name`) — Required user-friendly label (e.g., "Panel_01").
   - **Barcode** (`barcode`) — Required Tigo hardware barcode.
   - **Peak power** (`peak_power`) — Optional peak panel power in Wp (default: 455). Used to calculate performance percentage.

   Validation:
   - Name must be non-empty → `missing_name` error on the name field.
   - Barcode must be non-empty → `missing_barcode` error on the barcode field.
   - Barcode must match pattern `^[0-9A-Fa-f]-[0-9A-Fa-f]{1,7}[A-Za-z]$` → `invalid_barcode` error.
   - Barcode must not duplicate an already-added module → `duplicate_barcode` error.
   - On success, appends the module dict and returns to the modules menu.

4. **`async_step_finish`** — Creates the config entry with `{host, port, modules: [...]}`.

#### Options Flow Steps

```
┌──────────────┐     ┌──────────────┐
│  Step: init  │────►│ add_module   │
│  (menu)      │◄────│ (form)       │
│              │     └──────────────┘
│  ► Add       │     ┌──────────────┐
│  ► Remove    │────►│ remove_module│
│  ► Save      │◄────│ (dropdown)   │
└──────────────┘     └──────────────┘
       │
       ▼ (done)
  UPDATE_ENTRY
```

- **`async_step_init`** — Menu with "Add a module", "Remove a module", "Save and close".
- **`async_step_add_module`** — Same form and validation as the config flow version.
- **`async_step_remove_module`** — Dropdown (`vol.In`) built dynamically from the current module list showing `"Name (Barcode)"` labels. Selecting one removes it and returns to the menu.
- **"Save and close"** — Updates `ConfigEntry.data` with the modified module list, triggering an integration reload.

#### Helper Functions

- **`validate_barcode(barcode)`** — Regex validation against `_BARCODE_PATTERN`.
- **`validate_connection(hass, data)`** — Runs `TcpSource.connect()` in the executor. Used for advisory connection testing only.
- **`_modules_description(modules)`** — Builds a Markdown-formatted summary of the module list for display in menu descriptions.

#### Error Classes

Four custom `HomeAssistantError` subclasses: `CannotConnect`, `InvalidAuth`, `InvalidModuleFormat`, `InvalidBarcodeFormat`.

---

### `coordinator.py` — Data Coordinator

**~680 lines** implementing `PyTapDataUpdateCoordinator`, the core runtime engine.

#### Class: `PyTapDataUpdateCoordinator`

Inherits from `DataUpdateCoordinator[dict[str, Any]]`. Despite using the coordinator pattern, this is a **push-based** integration — `_async_update_data()` simply returns the current data dict without polling.

#### Initialization

```python
def __init__(self, hass, entry):
    # Extract config
    self._host = entry.data[CONF_HOST]
    self._port = entry.data.get(CONF_PORT, DEFAULT_PORT)
    self._modules = entry.data.get(CONF_MODULES, [])

    # Build barcode allowlist and lookup table
    self._configured_barcodes = {m[CONF_MODULE_BARCODE] for m in self._modules ...}
    self._module_lookup = {m[CONF_MODULE_BARCODE]: m for m in self._modules ...}

    # Barcode ↔ node_id mapping (learned at runtime)
    self._barcode_to_node = {}
    self._node_to_barcode = {}

    # Discovery tracking for unconfigured barcodes
    self._discovered_barcodes = set()

    # Initialize data structure
    self.data = {
        "gateways": {},
        "nodes": {},        # barcode → {power, voltage_in, ...}
        "counters": {},     # parser frame counters
        "discovered_barcodes": [],
    }
```

#### Listener Lifecycle

```
async_start_listener()
    └── creates background task → _async_listen()
                                       └── executor job → _listen()

async_stop_listener()
    └── sets _stop_event (threading.Event)
    └── acquires _source_lock, closes _source (unblocks socket.read)
    └── awaits task with asyncio.timeout(5)
```

- **`_stop_event`** — A `threading.Event` (not `asyncio.Event`) so it can be set from the main thread and checked from the executor thread without cross-loop issues.

- **`_source_lock`** — A `threading.Lock` protecting `_source` access. Both `_listen()` (executor) and `async_stop_listener()` (main loop via executor) need to touch `_source`; the lock prevents races.

- **`_async_listen()`** — Async wrapper that calls `hass.async_add_executor_job(self._listen)`. This is necessary because `async_create_background_task` expects a coroutine, not a Future.

- **`_listen()`** — Blocking loop running in the executor thread:
  1. Creates a `Parser` and connects a `TcpSource` (under `_source_lock`).
  2. Checks `_stop_event` immediately after connect — if set during connect, exits cleanly.
  3. Reads 4096-byte chunks in a loop.
  4. Feeds bytes to the parser, getting back a list of `Event` objects.
  5. For **each event individually**, calls `_process_event()` — if it returns `True` (data changed), pushes an update to HA via `hass.loop.call_soon_threadsafe(self.async_set_updated_data, ...)`. This per-event push avoids micro-batching.
  6. Monitors for silence timeouts (`RECONNECT_TIMEOUT`).
  7. On error/timeout, closes the source (under `_source_lock`), waits `RECONNECT_DELAY` seconds, and retries.
  8. Sleep during reconnect delay uses 0.1s increments checking `_stop_event` for fast shutdown.

#### Event Processing

```python
def _process_event(self, event) -> bool:
    """Returns True if data was changed."""
    if isinstance(event, PowerReportEvent):
        return self._handle_power_report(event)
    elif isinstance(event, InfrastructureEvent):
        return self._handle_infrastructure(event)
    elif isinstance(event, TopologyEvent):
        return self._handle_topology(event)
    elif isinstance(event, StringEvent):
        # Logged at DEBUG, not stored
    return False
```

**`_handle_power_report(event) → bool`:**
1. Resolves `barcode` — directly from event, or via `_node_to_barcode` mapping.
2. If barcode is unknown, logs at DEBUG and returns `False`.
3. If barcode is not in `_configured_barcodes` (allowlist), logs discovery at INFO and returns `False`.
4. Upserts into `self.data["nodes"][barcode]` with all power fields plus `last_update` timestamp. Returns `True`.

The data dict stored per node:

```python
{
    "gateway_id": int,
    "node_id": int,
    "barcode": str,
    "name": str,           # from user config
    "string": str,         # from user config
    "voltage_in": float,
    "voltage_out": float,
    "current_in": float,
    "current_out": float,
    "power": float,
    "peak_power": int,
    "performance": float,     # (power / peak_power) × 100
    "temperature": float,
    "dc_dc_duty_cycle": float,  # 0.0–1.0
    "rssi": int,
    "daily_energy_wh": float,   # accumulated daily Wh (trapezoidal)
    "total_energy_wh": float,   # lifetime accumulated Wh
    "readings_today": int,      # power-report count since midnight
    "daily_reset_date": str,    # ISO date of last daily reset
    "last_update": str,         # ISO 8601
}
```

**`_handle_infrastructure(event) → bool`:**
- Replaces `self.data["gateways"]` with the event's gateway dict.
- Rebuilds the `barcode ↔ node_id` bidirectional mapping from scratch.
- Logs newly discovered unconfigured barcodes at INFO level.
- Differentiates the first infrastructure event in a session:
  - If the event has no barcodes (node table not yet received), logs an INFO explaining that resolution will activate once the gateway sends the full node table.
  - If the event has barcodes, logs a WARNING with the match count.
- On subsequent events with changed mappings, logs updated match counts and lists any configured barcodes still missing from the node table.
- Triggers a persist (via `_schedule_save`) when mappings change **or** when new unconfigured barcodes are discovered.
- Returns `True` if gateway data changed.

**`_handle_topology(event) → bool`:**
- Attaches topology data to the matching node (by resolving `node_id` → barcode).
- Returns `True` if data was attached to a configured node.

#### Discovery Logging

When an unconfigured barcode is seen for the first time, the coordinator logs:

```
INFO: Discovered unconfigured Tigo optimizer barcode: A-9999999Z
      (gateway=1, node=55). Add it to your PyTap module list to start tracking.
```

The `_discovered_barcodes` set ensures each barcode is logged only once. The sorted list is also exposed in `self.data["discovered_barcodes"]` for potential diagnostics use.

#### Live Reconfiguration

```python
def reload_modules(self, modules):
    """Rebuild allowlist and lookup from updated module config."""
    self._configured_barcodes = {m[CONF_MODULE_BARCODE] for m in modules ...}
    self._module_lookup = {m[CONF_MODULE_BARCODE]: m for m in modules ...}
```

Called when the options flow updates the module list. After updating the allowlist, checks whether any newly-configured barcodes already have a known node mapping from previous infrastructure events. If so, creates a placeholder entry in `self.data["nodes"]` with module metadata (name, string group) so sensor entities can bind immediately without waiting for the next power report. Logs which barcodes were resolved from saved state and which are still pending.

#### Persistence

All persistent state is consolidated into a single HA Store, written via `homeassistant.helpers.storage.Store` (version 2) as `<config>/.storage/pytap_<entry_id>_coordinator`. The store contains:

- **`barcode_to_node`** — Barcode↔node_id mappings learned from infrastructure events.
- **`discovered_barcodes`** — Set of unconfigured barcodes seen on the bus.
- **`parser_state`** — Serialised parser infrastructure state (gateway identities, versions, node tables) via `PersistentState.to_dict()`.
- **`energy_data`** — Per-barcode accumulator state (`daily_energy_wh`, `daily_reset_date`, `total_energy_wh`, `readings_today`, `last_power_w`, `last_reading_ts`).

On startup, coordinator state is loaded from the HA Store (via `_async_load_coordinator_state`), including the parser's `PersistentState` which is deserialized via `PersistentState.from_dict()`. The parser receives a shared `PersistentState` object and mutates it in memory — the parser never performs file I/O. The coordinator schedules debounced saves (10-second delay) when mappings or infrastructure change, and flushes immediately on shutdown.

The `_init_mappings_from_parser` method pre-populates barcode↔node mappings from the parser's infrastructure on reconnect. Parser mappings take precedence when non-empty; when the parser state has no node table (first run), the coordinator-saved mappings are preserved as fallback.

---

### `energy.py` — Energy Accumulation

Pure-logic module implementing trapezoidal energy integration. Intentionally HA-independent so it can be unit-tested without coordinator or event-loop setup.

**`EnergyAccumulator` dataclass** — Per-barcode mutable state: `daily_energy_wh`, `total_energy_wh`, `daily_reset_date`, `last_power_w`, `last_reading_ts`, `readings_today`.

**`EnergyUpdateResult` dataclass** — Immutable result metadata per accumulation step: `increment_wh`, `discarded_gap_during_production`.

**`accumulate_energy(acc, power, now, …) → EnergyUpdateResult`:**
1. Clamps power to non-negative.
2. Resets `daily_energy_wh` and `readings_today` to zero on date change.
3. If a previous reading exists and the interval is within the gap threshold, applies trapezoidal integration: `((prev_power + power) / 2) × (Δt / 3600)`.
4. Flags intervals exceeding the gap threshold during production as discarded.
5. Unconditionally increments `readings_today` and updates `last_power_w` / `last_reading_ts`.

The coordinator calls `accumulate_energy()` from `_handle_power_report` and merges the result into the node data dict.

---

### `sensor.py` — Sensor Platform

Implements per-optimizer sensors and aggregate sensors using the `CoordinatorEntity` pattern.

#### Sensor Descriptions

```python
SENSOR_DESCRIPTIONS = (
    PyTapSensorEntityDescription(key="performance",       value_key="performance",
        unit="%",                          state_class=MEASUREMENT),
    PyTapSensorEntityDescription(key="power",             value_key="power",
        unit=UnitOfPower.WATT,            device_class=POWER),
    PyTapSensorEntityDescription(key="voltage_in",        value_key="voltage_in",
        unit=UnitOfElectricPotential.VOLT, device_class=VOLTAGE),
    PyTapSensorEntityDescription(key="voltage_out",       value_key="voltage_out",
        unit=UnitOfElectricPotential.VOLT, device_class=VOLTAGE),
    PyTapSensorEntityDescription(key="current_in",        value_key="current_in",
        unit=UnitOfElectricCurrent.AMPERE, device_class=CURRENT),
    PyTapSensorEntityDescription(key="current_out",       value_key="current_out",
        unit=UnitOfElectricCurrent.AMPERE, device_class=CURRENT),
    PyTapSensorEntityDescription(key="temperature",       value_key="temperature",
        unit=UnitOfTemperature.CELSIUS,    device_class=TEMPERATURE),
    PyTapSensorEntityDescription(key="dc_dc_duty_cycle",  value_key="dc_dc_duty_cycle",
        unit="%",                          state_class=MEASUREMENT),
    PyTapSensorEntityDescription(key="rssi",              value_key="rssi",
        unit=SIGNAL_STRENGTH_DECIBELS_MILLIWATT, device_class=SIGNAL_STRENGTH),
    PyTapSensorEntityDescription(key="daily_energy",      value_key="daily_energy_wh",
        unit=UnitOfEnergy.WATT_HOUR,       device_class=ENERGY, state_class=TOTAL),
    PyTapSensorEntityDescription(key="total_energy",      value_key="total_energy_wh",
        unit=UnitOfEnergy.WATT_HOUR,       device_class=ENERGY, state_class=TOTAL_INCREASING),
    PyTapSensorEntityDescription(key="readings_today",    value_key="readings_today",
        state_class=TOTAL, entity_category=DIAGNOSTIC),
)
```

Power/electrical sensors use `SensorStateClass.MEASUREMENT`; energy sensors use `TOTAL`/`TOTAL_INCREASING` for HA long-term statistics and energy dashboard compatibility.

#### Entity Creation

`async_setup_entry()` creates entities **deterministically** from the config:

```python
for module_config in modules:
    barcode = module_config.get(CONF_MODULE_BARCODE, "")
    if not barcode:
        continue  # Skip modules without barcode
    for description in SENSOR_DESCRIPTIONS:
        entities.append(PyTapSensor(coordinator, description, module_config, entry))
```

For two modules on two strings, entity creation is:

- Per-optimizer: `2 × 12 = 24`
- Per-string aggregate: `2 × 4 = 8`
- Installation aggregate: `4`
- Total: `36`

#### `PyTapSensor` Class

Inherits `CoordinatorEntity[PyTapDataUpdateCoordinator]` and `SensorEntity`.

**Identity:**
- `unique_id`: `"{DOMAIN}_{barcode}_{sensor_key}"` (e.g., `pytap_A-1234567B_power`)
- `has_entity_name = True`

**Device grouping:**

```python
DeviceInfo(
    identifiers={(DOMAIN, barcode)},
    name=f"Tigo TS4 {module_name}",
    manufacturer="Tigo Energy",
    model="TS4",
    serial_number=barcode,
)
```

All 12 sensors for the same barcode are grouped under one device.

**Availability:**
Returns `True` only when `coordinator.data["nodes"][barcode]` exists (i.e., at least one `PowerReportEvent` has been received for this optimizer). There is no unavailable timeout — sensors hold their last received value indefinitely.

**Value updates** (`_handle_coordinator_update`):
- Reads from `coordinator.data["nodes"][barcode][value_key]`.
- Special case: `dc_dc_duty_cycle` is converted from 0.0–1.0 to percentage (`* 100`).
- Calls `self.async_write_ha_state()` to push the update.

**Extra state attributes:**
- `string_group` — from user config (if set).
- `last_update` — ISO timestamp from coordinator data.
- `gateway_id` — the gateway this optimizer communicates through.

---

### `diagnostics.py` — Diagnostics Platform

Implements the HA diagnostics download endpoint via `async_get_config_entry_diagnostics()`. HA auto-discovers this module — no `PLATFORMS` entry is needed.

**Redaction:** Uses `async_redact_data` with `TO_REDACT = {CONF_HOST}` to strip the host IP from the config entry snapshot. Port and all other data remain visible.

**Payload structure:**
- `config_entry` — Redacted config entry dict.
- `counters` — Internal event counters from coordinator data.
- `gateways` — Gateway identity data.
- `discovered_barcodes` — Unconfigured barcodes seen on the bus.
- `nodes` — Per-barcode summary (last_update, gateway_id, node_id, daily_energy_wh, total_energy_wh, readings_today).
- Plus all keys from `coordinator.get_diagnostics_data()` (node_mappings, connection_state, energy_state), also redacted.

The node summaries intentionally omit raw power/voltage fields to keep the diagnostics download focused on integration health rather than instantaneous electrical data.

---

### `__init__.py` — Integration Lifecycle

Handles integration lifecycle, config entry migration, and legacy entity cleanup.

#### Config Entry Version

`CONFIG_ENTRY_VERSION = 4`:

- `v1 → v2`: voltage/current split to `_in`/`_out`
- `v2 → v3`: module string labels became mandatory (defaulted to `"Default"` during migration)
- `v3 → v4`: peak power added to module config (defaulted to 455 Wp during migration)

#### `async_migrate_entry(hass, entry) → bool`

Handles config entry version migration:
- **v1 → v2:** Updates `entry.version` to 2.
- **v2 → v3:** Ensures each module has a non-empty string label, defaulting missing/empty values to `"Default"`.
- **v3 → v4:** Adds `peak_power` to each module, defaulting to `DEFAULT_PEAK_POWER` (455 Wp) for modules that don't have it.

#### `async_setup_entry(hass, entry) → bool`

1. Cleans up legacy entity unique IDs from pre-v0.2.0 via `_async_cleanup_legacy_entities()`.
2. Creates `PyTapDataUpdateCoordinator(hass, entry)`.
3. Calls `coordinator.async_config_entry_first_refresh()` — validates initialization (does not block on data since this is push-based).
4. Calls `coordinator.async_start_listener()` — launches the background streaming task.
5. Registers `coordinator.async_stop_listener` via `entry.async_on_unload()` — ensures the listener is stopped on HA shutdown or entry unload.
6. Stores coordinator in `hass.data[DOMAIN][entry.entry_id]`.
7. Forwards platform setup (`Platform.SENSOR`).
8. Registers `_async_update_options` as an update listener.

#### `_async_cleanup_legacy_entities(hass, entry)`

Removes orphaned entity registry entries left over from the voltage/current → voltage_in/out, current_in/out rename:
- Iterates configured modules and checks for entities with old unique IDs (`pytap_BARCODE_voltage`, `pytap_BARCODE_current`).
- Removes matching entries from the entity registry.
- Logs the count of cleaned-up entities.

#### `_async_update_options(hass, entry)`

Reloads the entire integration when options change, causing a full teardown/setup cycle that picks up the modified module list.

#### `async_unload_entry(hass, entry) → bool`

1. Unloads platforms.
2. Stops the coordinator's background listener (also covered by `async_on_unload` but called explicitly for the non-shutdown unload path).
3. Removes the coordinator from `hass.data`.

---

### `strings.json` / Translations — UI Strings

Defines all user-facing text for the config flow and options flow in structured JSON.

**Config flow steps:**
- `user` — "Connect to Tigo Gateway" with host/port fields and descriptions.
- `modules_menu` — "Configure Modules" menu with `{modules_list}` and `{error}` placeholders.
- `add_module` — "Add Module" form with string/name/barcode fields and detailed descriptions.
- `finish` — "Finish Setup" (terminal step).

**Options flow steps:**
- `init` — "PyTap Options" menu with add/remove/done options.
- `add_module` — Same fields as config flow.
- `remove_module` — Dropdown with `remove_barcode` selector.

**Error strings:** `cannot_connect`, `invalid_barcode`, `missing_string`, `missing_name`, `missing_barcode`, `duplicate_barcode`, `no_modules`, `unknown`.

**Abort reasons:** `already_configured`.

`translations/en.json` mirrors `strings.json` exactly.

---

## Config Flow UX Design

The config flow uses a **menu-driven approach** where users add optimizer modules one at a time through individual form fields.

### Rationale

The menu-driven form is used because it improves usability and validation:

1. **Lower error rate** — Each field is validated directly.
2. **Per-field errors** — Validation issues are shown on the relevant field.
3. **Better discoverability** — Users are guided through the expected inputs.
4. **Inline help** — Each field can include a focused description.

### Current UX Flow

```
1. User enters host and port
   → Non-blocking connection test (warns but proceeds if unreachable)

2. Modules menu appears:
   "Modules (0): No modules added yet."
   [Add a module]  [Finish setup]

3. User clicks "Add a module" → form appears:
    String group: [___________]  (required)
   Name:         [___________]  (required)
   Barcode:      [___________]  (required)

4. On submit, if valid:
   → Returns to menu with updated list
   "Modules (1):
     1. string=A / Panel_01 / A-1234567B"
   [Add a module]  [Finish setup]

5. User repeats step 3-4 as needed, then clicks "Finish setup"
   → Config entry created
```

### Connection Validation

The TCP connection test in step 1 is **non-blocking**: if the gateway is unreachable (common during initial setup when the gateway may not be powered on), the flow logs a warning and proceeds to the modules menu. This prevents the common frustration of being unable to complete configuration when the gateway is temporarily offline.

---

## Data Flow

```
Tigo Gateway (TCP port 502)
    │
    │  Raw bytes (RS-485 protocol frames)
    ▼
TcpSource.read(4096)                       [executor thread]
    │
    ▼
Parser.feed(bytes) → list[Event]           [executor thread]
    │
    ├── PowerReportEvent
    ├── InfrastructureEvent
    ├── TopologyEvent
    └── StringEvent
    │
    ▼
FOR EACH event:                            [executor thread]
    coordinator._process_event(event) → bool
    │
    ├── data changed (True)?
    │   ├── Barcode in allowlist? YES → merge into data["nodes"][barcode]
    │   │                         NO  → log discovery, discard
    │   │
    │   ▼
    │   hass.loop.call_soon_threadsafe(    [→ main event loop]
    │       coordinator.async_set_updated_data, data
    │   )
    │
    └── no change (False) → skip push
    │
    ▼
CoordinatorEntity._handle_coordinator_update()  [main event loop]
    │
    ├── Read from data["nodes"][barcode][value_key]
    ├── Convert duty cycle → percentage (if applicable)
    └── async_write_ha_state()
    │
    ▼
Home Assistant frontend / automations / history
```

---

## Testing

### Test Configuration

- **Framework:** pytest with `pytest-homeassistant-custom-component`
- **Async mode:** `asyncio_mode = auto` (in `pytest.ini`)
- **Fixture:** `auto_enable_custom_integrations` (in `conftest.py`) enables loading from `custom_components/`.

### Config Flow Tests

Representative tests:

| Test | What it verifies |
| --- | --- |
| `test_step_user_shows_form` | Initial step renders host/port form with no errors |
| `test_user_step_proceeds_to_menu` | Submitting host/port advances to modules_menu |
| `test_user_step_proceeds_even_without_connection` | Failed TCP test still proceeds (non-blocking) |
| `test_full_flow_add_one_module` | Complete flow: user → menu → add → menu → finish = CREATE_ENTRY |
| `test_full_flow_add_two_modules` | Two add_module cycles produce entry with 2 modules |
| `test_add_module_invalid_barcode` | Invalid barcode format shows error on barcode field |
| `test_add_module_missing_name` | Empty name shows error on name field |
| `test_add_module_duplicate_barcode` | Duplicate barcode shows error on barcode field |


All tests mock `validate_connection` to avoid real TCP connections.

### Sensor Platform Tests

| Test | What it verifies |
| --- | --- |
| `test_sensor_entities_created` | 2 modules, 2 strings create 36 entities including aggregate sensors |
| `test_sensor_unique_ids` | IDs include per-optimizer and aggregate unique ID formats |
| `test_sensor_available_with_data` | Sensor available when node data exists |
| `test_sensor_unavailable_without_data` | Sensor unavailable when data dict is empty |
| `test_sensor_skips_modules_without_barcode` | Modules with empty barcode don't create entities |
| `test_sensor_device_info` | Device identifiers, manufacturer, model, serial_number |
| `test_sensor_descriptions_count` | Exactly 12 sensor descriptions defined |
| `test_energy_sensor_descriptions` | Daily/total energy sensor metadata and state classes |
| `test_daily_energy_last_reset` | Daily energy exposes `last_reset` from `daily_reset_date` |
| `test_string_daily_energy_sums` | String daily aggregate sums constituent `daily_energy_wh` |
| `test_installation_total_energy_sums_all` | Installation total aggregate sums constituent `total_energy_wh` |
| `test_string_aggregate_device_info` | String aggregate uses virtual string device metadata |
| `test_installation_aggregate_device_info` | Installation aggregate uses installation virtual device metadata |
| `test_performance_sensor_value` | Per-optimizer performance sensor exposes stored percentage |
| `test_string_performance_weighted` | String aggregate uses capacity-weighted formula |
| `test_installation_performance_partial_data` | Aggregate performance includes only reporting nodes |
| `test_installation_performance_unavailable_without_data` | Aggregate performance unavailable when no nodes report power |
| `test_performance_sensor_zero_power` | Power=0W produces performance=0.0% |
| `test_readings_today_sensor_metadata` | `readings_today` has `TOTAL` state class and `DIAGNOSTIC` category |
| `test_readings_today_value_and_last_reset` | Value read from node data, `last_reset` from `daily_reset_date` |
| `test_performance_sensor_above_100` | Power > peak produces >100% (no clamping) |

Tests use `MagicMock(spec=PyTapDataUpdateCoordinator)` to avoid real coordinator initialization.

### Coordinator Persistence Tests (32 tests)

Coverage includes coordinator initialization, barcode mapping restoration and purging, deferred power-report handling before infrastructure, save/load behavior, parser-state restore/fallback, stop-flush behavior, and energy-data persistence (`energy_data` save/load with daily reset on new day).

### Diagnostics Platform Tests (4 tests)

`tests/test_diagnostics.py` validates the diagnostics download endpoint:

| Test | What it verifies |
| --- | --- |
| `test_config_entry_diagnostics_redacts_host` | Host is redacted, port and barcodes are visible |
| `test_config_entry_diagnostics_includes_unredacted_barcodes` | Discovered barcodes pass through unredacted |
| `test_config_entry_diagnostics_fresh_install` | Empty coordinator (no data) doesn't raise |
| `test_config_entry_diagnostics_all_keys_present` | All expected top-level keys present, `energy_state`/`discovered_barcodes` pass-through |

### Energy Accumulation Unit Tests (13 tests)

`tests/test_energy.py` validates trapezoidal integration in isolation across baseline behavior, nominal interval integration, gap handling during production, overnight gaps, daily resets with preserved total accumulation, `readings_today` incrementing and daily-reset behaviour, and related edge cases.

### Entity Migration Tests

| Test | What it verifies |
| --- | --- |
| `test_removes_old_voltage_and_current_entities` | Legacy voltage/current entity registry entries removed on setup |
| `test_does_not_touch_new_entities` | New _in/_out entities are not affected by cleanup |
| `test_no_op_when_no_legacy_entities` | No errors when no legacy entities exist |
| `test_migrates_v1_to_v2` | Config entry version migrates forward to current version |
| `test_migrate_v2_to_v3_empty_strings` | Empty/missing module strings are defaulted during migration |
| `test_migrate_v2_to_v3_existing_strings` | Existing string labels preserved during migration |
| `test_migrate_v2_to_v3_mixed` | Only missing string labels defaulted in mixed lists |
| `test_migrate_v3_to_v4_adds_peak_power` | Modules without peak_power get DEFAULT_PEAK_POWER |
| `test_migrate_v3_to_v4_preserves_peak_power` | Existing peak_power values not overwritten |
| `test_migrate_v3_to_v4_mixed` | Mixed modules: missing gets default, existing preserved |
| `test_already_current_version` | Current-version entries pass through migration unchanged |

### Parser Library Tests (in `custom_components/pytap/pytap/tests/`)

The embedded parser library has its own test suite:

| Test File | Coverage |
| --- | --- |
| `test_parser.py` | Byte-level protocol parsing with captured data |
| `test_types.py` | Protocol type construction and field validation |
| `test_crc.py` | CRC-16 calculation against known vectors |
| `test_barcode.py` | Barcode encode/decode round-trips |
| `test_api.py` | Public API function surface tests |

### Running Tests

```bash
# Run all integration tests
python3 -m pytest tests/ -vv --tb=short

# Run parser library tests
python3 -m pytest custom_components/pytap/pytap/tests/ -vv

# Lint
python3 -m ruff check custom_components/pytap/
```

---

## Design Decisions & Trade-offs

### 1. Menu-Driven Module Input

**Chosen:** Individual form-per-module with a menu loop.

**Trade-off:** More config flow steps for users with many modules, but significantly better UX:
- Per-field validation with targeted error messages.
- Required field (string group) is clearly labeled.
- Guided field-by-field entry for string, name, and barcode.
- Matches HA's native form conventions.

### 2. Non-Blocking Connection Test

**Chosen:** TCP connection test warns on failure but does not block the flow.

**Rationale:** Users often configure integrations when the target device is offline (e.g., solar gateway powered off at night). Blocking on connection would prevent saving a valid configuration. The integration will connect when the gateway becomes available.

### 3. Push-Based Coordinator (Not Poll-Based)

**Chosen:** Background streaming task with `call_soon_threadsafe` dispatch.

**Trade-off:** More complex than a simple `update_interval`-based coordinator, but:
- Sub-second latency vs. polling interval latency.
- No redundant requests — only new data is processed.
- Matches the bus protocol's inherent push nature.

### 4. Executor Thread Bridging

**Chosen:** Run blocking `_listen()` in HA's executor via `async_add_executor_job`, wrapped in an `_async_listen()` coroutine.

**Rationale:** The pytap library uses blocking `socket.recv()`. Rewriting the library for asyncio would add complexity without benefit. The executor bridge is clean: the library remains portable and testable outside HA.

### 5. Barcode as Primary Key (Not node_id)

**Chosen:** All entity IDs, device identifiers, and data dict keys use the Tigo barcode.

**Rationale:** `node_id` is a transient 16-bit integer that can change across gateway restarts. Barcodes are hardware-burned identifiers that never change, making them suitable as stable unique IDs.

### 6. Deterministic Entity Creation (No Auto-Discovery)

**Chosen:** Entities are created at setup from the configured module list. No dynamic entity creation from bus events.

**Rationale:**
- Prevents phantom entities from neighboring installations on the same RS-485 bus.
- Enables dashboard/automation setup before first data arrives.
- User-defined names instead of opaque IDs.
- Consistent with the taptap HA add-on approach.

### 7. Integration Reload on Options Change

**Chosen:** `_async_update_options` triggers a full `async_reload` rather than incremental entity updates.

**Trade-off:** Brief disruption during reload, but guarantees entities match the new config exactly. Adding/removing modules changes the entity set, which is simplest to handle via full reload.

---

## Known Deviations from Architecture

The architecture document (`architecture.md`) was written during initial design and has not been fully updated for the menu-driven config flow. Notable differences:

| Aspect | Architecture Doc | Actual Implementation |
| --- | --- | --- |
| Config flow modules step | Legacy flow variants | Menu-driven: host/port → modules_menu → add_module loop → finish |
| Module input format | Legacy text input formats | Individual form fields per module |
| Options flow | Described as text-based reconfiguration | Menu with add/remove/done actions |
| Gateway device registration | Described as separate DeviceInfo | Not yet implemented (sensors have device info per optimizer only) |
| `via_device` on nodes | Linked to gateway device | Not implemented (no gateway device yet) |
| Unavailable timeout | Described as configurable via options | Removed — sensors hold last value indefinitely |
| Sensor count | Historical counts | 12 per optimizer, plus string/installation aggregate sensors |
| Config entry version | Not mentioned | v4 with v1→v2, v2→v3, and v3→v4 migration steps |
| Threading primitives | Not specified | threading.Event + threading.Lock (not asyncio.Event) |
| Diagnostics platform | Mentioned for discovered barcodes | Implemented via `diagnostics.py` config-entry download |

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
---

## Future Work

Items identified but not yet implemented:

1. **Gateway device registration** — Create a device per gateway for the `via_device` hierarchy.
2. **Binary sensors** — Node connectivity and gateway online status.
3. **HACS distribution** — Package with `hacs.json` for one-click installation.
