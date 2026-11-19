# PyTap — Home Assistant Custom Component Architecture

## Overview

PyTap is a Home Assistant custom component that passively monitors Tigo TAP solar energy systems via the RS-485 bus. It receives the bus byte stream over TCP, a local serial adapter, or a Home Assistant ESPHome serial proxy, parses the proprietary protocol in real time, and exposes per-optimizer sensor entities — power, voltage, current, temperature, and more — directly in Home Assistant. It never writes to the bus.

The integration embeds the `pytap` protocol parser library and bridges its event-driven output into Home Assistant's entity/device model using an async coordinator pattern.

---

## System Context

```
┌──────────────────────────────────────────────────────────────────────┐
│                       Home Assistant Instance                       │
│                                                                     │
│   ┌───────────────────────────────────────────────────────────┐     │
│   │  custom_components/pytap  (this integration)              │     │
│   │                                                           │     │
│   │   Config Flow ─► Coordinator ─► Sensor Entities           │     │
│   │                      │                                    │     │
│   │                      ▼                                    │     │
│   │               pytap library                               │     │
│   │           (embedded protocol parser)                      │     │
│   └──────────────────────┬────────────────────────────────────┘     │
│                          │ TCP / Serial / ESPHome serial proxy      │
└──────────────────────────┼──────────────────────────────────────────┘
                           │
                           ▼
               ┌───────────────────────┐
               │   Tigo TAP Gateway    │
               │   (RS-485 bus master) │
               └───────────┬───────────┘
                           │ RS-485
              ┌────────────┼────────────┐
              ▼            ▼            ▼
         ┌─────────┐ ┌─────────┐ ┌─────────┐
         │TS4 Node │ │TS4 Node │ │TS4 Node │  ... (PV optimizers)
         └─────────┘ └─────────┘ └─────────┘
```

---

## Project Structure

```
pytap/                              # Repository root
├── docs/
│   ├── architecture.md             # This file
│   └── implementation.md           # Implementation details & history
├── config/                         # HA dev config directory
│   ├── configuration.yaml
│   └── custom_components → ../custom_components  (symlink)
├── custom_components/
│   └── pytap/                      # HA custom component
│       ├── __init__.py             # Setup / teardown, migrations, device removal
│       ├── config_flow.py          # UI-based configuration
│       ├── connection.py           # Connection validation, serial/proxy source
│       ├── const.py                # Domain, defaults, config keys
│       ├── coordinator.py          # DataUpdateCoordinator (async bridge)
│       ├── diagnostics.py          # HA diagnostics platform download
│       ├── energy.py               # Pure energy accumulation helpers
│       ├── sensor.py               # Sensor entities (power, voltage, etc.)
│       ├── manifest.json           # HA integration metadata
│       ├── strings.json            # UI strings (source)
│       ├── translations/
│       │   └── en.json             # English translations
│       └── pytap/                  # Embedded protocol parser library
│           ├── __init__.py         # Library re-exports
│           ├── api.py              # Public API (create_parser, parse_bytes, connect)
│           └── core/
│               ├── parser.py       # Protocol parser (bytes → events)
│               ├── types.py        # Protocol types & constants
│               ├── events.py       # Event dataclasses
│               ├── state.py        # SlotClock, NodeTableBuilder, PersistentState
│               ├── source.py       # TcpSource, SerialSource
│               ├── crc.py          # CRC-16-CCITT
│               └── barcode.py      # Tigo barcode encode/decode
├── tests/                          # Integration tests (see Testing Strategy)
├── requirements.txt                # Home Assistant Core dependencies
├── requirements-test.txt           # Test harness dependencies
└── pytest.ini                      # Test configuration
```

---

## Component Architecture

### Layer Diagram

```
┌──────────────────────────────────────────────────────────────┐
│                    Home Assistant Core                        │
│  (event loop, entity registry, device registry, frontend)    │
└───────────┬───────────────────────────────┬──────────────────┘
            │ async_setup_entry             │ entity updates
            ▼                               ▲
┌───────────────────────────────────────────────────────────────┐
│  Integration Layer  (custom_components/pytap/)                │
│                                                               │
│  ┌──────────────┐  ┌───────────────────┐  ┌───────────────┐  │
│  │  Config Flow  │  │   Coordinator     │  │   Entities    │  │
│  │              │  │                   │  │               │  │
│  │  • Menus     │  │  • Async bridge   │  │  • Sensors    │  │
│  │  • Validate  │  │  • Thread mgmt    │  │  • Device     │  │
│  │    connection│  │  • Event routing  │  │    grouping   │  │
│  │  • Store     │  │  • Worker state + │  │               │  │
│  │    config    │  │    published copy │  │               │  │
│  └──────┬───────┘  └────────┬──────────┘  └───────┬───────┘  │
│         │                   │                     │          │
│         │ ConfigEntry       │ pytap.Parser         │ reads    │
│         │                   │ (in executor thread) │ coord    │
│         └───────────────────┘                     │ .data    │
│                             │                     │          │
└─────────────────────────────┼─────────────────────┼──────────┘
                              │                     │
                              ▼                     │
┌─────────────────────────────────────────────────────────────┐
│  Parser Library  (pytap/pytap/)                              │
│                                                              │
│  pytap.api.create_parser() → Parser                          │
│  parser.feed(bytes) → list[Event]                            │
│                                                              │
│  No HA dependency — pure protocol logic, stdlib only         │
└──────────────────────────────────────────────────────────────┘
```

---

## Component Modules

### 1. `__init__.py` — Integration Setup

Entry point for Home Assistant:

| Function | Purpose |
| --- | --- |
| `async_setup_entry(hass, entry)` | Create coordinator, start data streaming, forward platform setup |
| `async_unload_entry(hass, entry)` | Stop the listener (bounded), unload platforms |
| `async_remove_entry(hass, entry)` | Delete the entry's saved state |
| `async_remove_config_entry_device(hass, entry, device)` | Allow deleting optimizer devices and empty string devices |
| `async_migrate_entry(hass, entry)` | Migrate config entries v1 → v7 |

Platforms: `sensor`.

**Setup flow:**
1. If a coordinator from a previous load of this entry is parked (`parked_coordinators()`, i.e. `hass.data[DOMAIN]`, used only for coordinators whose stop timed out), wait for it to stop; if it still hasn't, raise `ConfigEntryNotReady` (`previous_listener_stopping`) so HA retries.
2. Remove legacy `voltage`/`current` entity registry entries.
3. Instantiate `PyTapDataUpdateCoordinator` (claims its configured barcodes). Invalid saved connection settings raise `ConfigEntryError` (`invalid_connection_settings`) instead of retrying forever; saving corrected settings in the options flow schedules a reload of an entry in setup error/retry.
4. `async_config_entry_first_refresh()` and `async_start_listener()` (loads the Store, starts the worker, midnight reset and freshness timers). No connectivity check: an offline gateway is retried in the background.
5. Store the coordinator in `entry.runtime_data` (typed `PyTapConfigEntry = ConfigEntry[PyTapDataUpdateCoordinator]`) and listen for `EVENT_HOMEASSISTANT_STOP` to stop the listener (HA does not unload entries on shutdown).
6. If the Store was read without error, remove devices of excluded optimizers and of strings without modules (empty strings are kept while discovery is on and no saved state was restored). Other unknown optimizer devices are kept; users can delete them.
7. Forward platform setup and register the options update listener (reloads the entry).

If a later step fails, the listener is stopped (releasing its barcode claims), or parked if it won't stop, so a retry can't open the port before it is released.

**Teardown flow:**
1. `async_stop_listener()` — bounded by `LISTENER_STOP_TIMEOUT` (20 s). Timers and the pending save are cancelled even if the worker can't be joined in time.
2. If it stopped, release the entry's barcode claims; otherwise park the coordinator (`hass.data[DOMAIN][entry_id]`) so the next setup waits for it and config flows treat its serial port or TCP host/port as in use. Once its worker exits, a parked coordinator finishes its own stop (final save) and releases its claims.
3. Unload platforms.

**Entry removal:** a parked coordinator gets a bounded stop with all later Store writes blocked; then its Store is deleted.

**Device removal:** deleting an optimizer device (any identifier that isn't the entry's own string/installation ID, so discovered optimizers too, even while the entry isn't loaded) removes its module from `entry.data["modules"]` if present and adds its barcode to `excluded_barcodes` (the entry update reloads PyTap). String devices can be deleted only when no module uses that string; the installation device cannot be deleted.

### 2. `config_flow.py` — Configuration UI

`PyTapConfigFlow` (`VERSION = 7`) collects connection parameters and modules:

| Step | Fields | Validation |
| --- | --- | --- |
| `user` | Menu (`tcp` / `serial`) | — |
| `tcp` | `host` (required), `port` (default: 502) | Non-empty host, port 1–65535, bounded probe |
| `serial` | `serial_port` (native `SerialPortSelector`), `baud_rate` (default: 38400) | `/dev/…` path or `esphome-hass://` proxy URL, baud 1–4000000, not used by another PyTap entry (symlinks resolved), bounded probe |
| `modules_menu` | Menu (`add_module` / `discovery` / `finish`) | — |
| `discovery` | `auto_discovery` (default on) | — |
| `add_module` | `string`, `name`, `barcode` (required), `peak_power` (default: 455 Wp) | Non-empty fields, barcode format, duplicate prevention, peak power 1–1000 |
| `finish` | — | Manual mode needs at least one module (`no_modules`); serial port re-checked |

**Connection probe:** opened and closed in the executor without consuming bytes. If it fails, the form shows `cannot_connect`; submitting identical settings again saves them anyway (the listener keeps retrying). Unsupported serial settings show `unsupported_serial_settings`; a path that doesn't resolve to a character device under `/dev/` shows `invalid_serial_port`. A serial port or TCP host/port still held by a parked coordinator shows `serial_port_in_use` / `connection_in_use`.

**Modules:** stored in `ConfigEntry.data["modules"]` as:

```python
[
    {"string": "A", "name": "Panel_01", "barcode": "A-1234567B", "peak_power": 455},
    {"string": "B", "name": "Panel_03", "barcode": "E-3456789F", "peak_power": 400},
]
```

The entry also stores `auto_discovery` and `excluded_barcodes`. With automatic discovery on, setup may finish with no modules.

**Unique ID:** a random UUID per entry; duplicate serial ports are prevented by the port check instead.

**Options Flow:** menu with _Change connection settings_ (TCP/serial; saved immediately, current serial port not probed while the listener owns it), _Change reporting settings_ (+ _Reporting Interval_ step when live reporting is off), _Change stale reading behaviour_, _Change module discovery_, _Add / Edit / Remove a module_, and _Save and close_. Connection, reporting and stale-reading changes are written immediately; module and discovery edits are written on _Save and close_, merged with modules discovered meanwhile without undoing removals. Removed barcodes are added to `excluded_barcodes`.

### 3. `const.py` — Constants

```python
DOMAIN = "pytap"
DEFAULT_PORT = 502           # Tigo TAP default TCP port
DEFAULT_BAUD_RATE = 38400    # Serial; framing fixed at 8N1
DEFAULT_PEAK_POWER = 455     # Wp (watts peak) — STC rating
DEFAULT_DISCOVERED_STRING = "Discovered"
DEFAULT_LIVE_REPORTING = True  # Push every reading as it arrives
DEFAULT_WRITE_INTERVAL = 30   # Seconds between averaged pushes (5–300)
DEFAULT_STALE_ACTION = "hold"  # or "unavailable" / "zero" (opt-in)
DEFAULT_STALE_TIMEOUT = 120  # Seconds (60–600)
FRESHNESS_CHECK_INTERVAL_SECONDS = 5
LISTENER_STOP_TIMEOUT = 20
```

### 4. `coordinator.py` — Data Update Coordinator

The coordinator is the central bridge between the blocking `pytap` parser and Home Assistant's async event loop. It uses a **push-based streaming model** rather than the typical polling pattern.

#### Architecture

```
┌─────────────────────────────────────────────────┐
│  PyTapDataUpdateCoordinator                     │
│                                                 │
│  Main thread (HA event loop):                   │
│    • Exposes self.data (published copy)         │
│    • async_set_updated_data() on each publish   │
│    • Freshness check every 5 s                  │
│    • Midnight reset, lifecycle                  │
│                                                 │
│  Executor thread (blocking I/O):                │
│    • open_source() → TCP or serial source       │
│    • parser.feed(bytes) → Events                │
│    • Updates private self._state                │
│    • Publishes a copy via call_soon_threadsafe  │
│                                                 │
│  ┌──────────────────────────────────────────┐   │
│  │  self.data (dict[str, Any])              │   │
│  │                                          │   │
│  │  {                                       │   │
│  │    "gateways": {                         │   │
│  │      1: { "address": "...", "version": "..." }│
│  │    },                                    │   │
│  │    "nodes": {                            │   │
│  │      "S-1234567A": {                     │   │
│  │        "gateway_id": 1,                  │   │
│  │        "node_id": 42,                    │   │
│  │        "barcode": "S-1234567A",          │   │
│  │        "name": "Panel_01",               │   │
│  │        "string": "A",                    │   │
│  │        "peak_power": 455,                │   │
│  │        "power": 343.0,                   │   │
│  │        "performance": 75.38,             │   │
│  │        "voltage_in": 39.2,               │   │
│  │        "voltage_out": 38.5,              │   │
│  │        "current_in": 8.75,               │   │
│  │        "current_out": 8.91,              │   │
│  │        "temperature": 45.2,              │   │
│  │        "dc_dc_duty_cycle": 0.78,         │   │
│  │        "rssi": 120,                      │   │
│  │        "daily_energy_wh": 812.4,         │   │
│  │        "total_energy_wh": 152340.0,      │   │
│  │        "readings_today": 3120,           │   │
│  │        "daily_reset_date": "2026-11-18", │   │
│  │        "last_update": "2026-11-18T…",    │   │
│  │        "report_monotonic": 12345.6       │   │
│  │      }                                   │   │
│  │    },                                    │   │
│  │    "counters": { ... },                  │   │
│  │    "discovered_barcodes": ["S-9999999Z"] │   │
│  │  }                                       │   │
│  └──────────────────────────────────────────┘   │
└─────────────────────────────────────────────────┘
```

`report_monotonic` is the monotonic time of the last valid report in this session; it is not persisted.

#### Worker State vs. Published Data

The listener thread owns `self._state` and the energy accumulators (guarded by a `threading.RLock`). Home Assistant only ever sees `self.data`, a snapshot in which every node dict is copied (`_snapshot()`), handed over with `hass.loop.call_soon_threadsafe(async_set_updated_data, snapshot, changed)`. `changed` is the set of barcodes whose data changed since the last publish: only their entities' data listeners (`async_add_data_listener(barcodes, callback)`, registered by per-optimizer sensors for their own barcode and by aggregates for their members) run. Global changes (new modules, gateway or node-table changes) pass `None` and update every coordinator listener. Each snapshot carries a sequence number, so a snapshot taken before a midnight reset can't overwrite the reset when it reaches the loop. Entities therefore never observe data changing mid-read, and in averaged mode the published averages are never overwritten by raw worker values: nodes without a new reading in the interval keep their previously published averages (energy fields still come from the worker), and the midnight reset changes only the daily fields of the published nodes.

#### Threading Model

1. **Background listener task** — An `asyncio.Task` created by `config_entry.async_create_background_task()` that awaits the blocking `_listen()` method in the executor (`hass.async_add_executor_job`), shielded so HA cancelling the task during shutdown doesn't lose track of the worker.
2. **Publishing** — The worker publishes snapshots back to the event loop with `call_soon_threadsafe` (immediately in live mode, once per interval in averaged mode).
3. **Midnight reset** — `async_track_time_change(hour=0, minute=0, second=0)` resets daily accumulators at every local midnight (DST-safe), even when no reports arrive overnight.
4. **Freshness check** — `async_track_time_interval` every 5 s recomputes which panels are reporting. Entities register per-barcode callbacks with `async_add_freshness_listener(barcodes, callback)`: live per-optimizer sensors for their own barcode, every aggregate for its current members (re-registered when membership changes). Only the callbacks of panels that started or stopped reporting run (once per registration), not every entity.
5. **Stopping** — `async_stop_listener()` sets a `threading.Event`, calls the TCP source's `interrupt()` (socket `shutdown`, waking a blocked `recv` immediately; serial reads are bounded to 1 s), cancels the midnight, freshness and save timers, then awaits the worker. The worker owns `close()`. The whole stop is bounded by `LISTENER_STOP_TIMEOUT`; on timeout it returns `False`, the coordinator is parked and reload is blocked until the worker exits. Once the worker has exited, the final Store save is shielded from cancellation.

```
 HA Event Loop (main thread)          Executor Thread
 ─────────────────────────            ────────────────
        │                                    │
        │  async_setup_entry()               │
        │  ─► create coordinator             │
        │  ─► load Store                     │
        │  ─► start listener task ──────────►│
        │  ─► midnight + freshness timers    │
        │                                    │ parser = create_parser(state)
        │                                    │ source = open_source(settings)
        │                                    │
        │                                    │ loop:
        │                                    │   data = source.read(4096)
        │                                    │   events = parser.feed(data)
        │                                    │   update self._state
        │  ◄── call_soon_threadsafe ─────────│   publish copy
        │      async_set_updated_data(copy)  │
        │                                    │
        │  [midnight] reset daily values     │
        │  [every 5 s] freshness check       │
        │                                    │
        │  entities read coordinator.data    │
        │                                    │
        │  async_unload_entry()              │
        │  ─► stop event + interrupt() ─────►│ read wakes, loop exits
        │  ─► await worker (≤ 20 s)          │ source.close()
        │  ─► cancel timers, flush Store     │ exits
```

#### Reconnection

- **Setup while offline** — No connectivity check at setup; the worker keeps retrying.
- **Connection failure / disconnection** — The worker catches the exception, closes the source, waits `RECONNECT_DELAY` seconds (waking immediately on stop) and reconnects. The first failure is logged once at WARNING with the exception type and errno (never the endpoint); further attempts log at DEBUG until data is received again (a link that connects and drops immediately doesn't warn again). A read interrupted by stop is not treated as a connection error.
- **Silence timeout** — If no data arrives for `RECONNECT_TIMEOUT` seconds, the worker reconnects (WARNING once per outage, then DEBUG).

#### Reporting Modes — Live vs. Averaged Interval

The coordinator delivers readings to Home Assistant in one of two user-selectable modes (stored as `live_reporting` / `write_interval` in `ConfigEntry.data`):

| Mode | `live_reporting` | Behavior |
| --- | --- | --- |
| **Live (default)** | `True` | Every event that changes node data triggers an immediate publish — sub-second latency. |
| **Averaged interval** | `False` | Readings are buffered per barcode with monotonic timestamps. Once per `write_interval` seconds (5–300, default 30) the coordinator publishes a **time-weighted average** via `_build_averaged_snapshot()`, also during quiet periods. |

Time-weighted averaging weights each buffered reading by the duration it remained the latest value (until the next reading, or until the flush for the final one).

Averaging applies only to power, voltage in/out, current in/out, temperature, DC-DC duty cycle and RSSI; `performance` is recomputed from the averaged power. Energy accumulation and persisted node snapshots always use the raw readings.

#### Stale Readings

`is_reporting(barcode)` is true when the published node's last valid report is younger than `stale_timeout` (plus `write_interval` in averaged mode). Entities consult it together with `stale_action` (`hold` by default, or the opt-in `unavailable` / `zero`; missing or invalid values fall back to `hold`); see [Entity Availability](#entity-availability). After a restart no node has a `report_monotonic`, so with `unavailable`/`zero` the restored live values are not presented as current until the first new report.

#### Data Merging Strategy

- **`PowerReportEvent`** — Barcode resolved from the event (parser node table) or, after this session's first node table, from the coordinator's `node_id → barcode` map. Unresolved reports are counted as pending. Unknown barcodes are logged once and, with discovery enabled, admitted (see below). Performance and energy are then computed, the node is replaced with all fields and `report_monotonic` updated; in averaged mode the reading is buffered.
- **`InfrastructureEvent`** — Replaces `gateways` and, when it contains a node table, rebuilds the barcode↔node_id mapping from scratch. Unconfigured barcodes are logged and (with discovery) admitted. Gateway-only events keep the existing mappings.
- **`TopologyEvent`** — Attaches topology data to known nodes.
- **`StringEvent`** — Logged at DEBUG; not stored.

#### Persistence

All persistent state is in a single **HA Store** (`<config>/.storage/pytap_<entry_id>_coordinator`, version 2):

- **`auto_modules`** — Metadata of automatically discovered modules.
- **`barcode_to_node`** — Barcode↔node_id mappings.
- **`discovered_barcodes`** — Barcodes seen on the bus that weren't configured.
- **`parser_state`** — `PersistentState.to_dict()` (gateway identities, versions, node tables).
- **`energy_data`** — Per-barcode accumulator state.
- **`node_snapshots`** — Last values per tracked barcode (but not `report_monotonic`).

Saves are throttled (at most one pending, written 10 s after the first change) and flushed on stop. If the Store can't be read, the coordinator logs a warning, skips orphan-device cleanup and writes nothing for the rest of its lifetime, so saved totals aren't replaced with empty state; entry removal also blocks writes. The parser receives the restored `PersistentState` and mutates it in memory; it never performs file I/O. Being under `.storage`, the Store is included in HA backups.

#### Module Discovery

With `auto_discovery` enabled, `_add_discovered_module()` admits a barcode seen in a node table or power report unless it is configured, in `excluded_barcodes`, or claimed by another PyTap entry. Claims (`BarcodeClaims`, in `hass.data`) are domain-wide and atomic across worker threads; each coordinator claims its configured barcodes when created (a barcode configured in two entries logs a warning) and releases its claims once its listener has stopped. Discovered modules get the name `Optimizer <barcode>`, string `Discovered` and rating 0 Wp (unknown), are saved in the Store (not the config entry, so no reload), and survive restarts. Enabling discovery later also admits barcodes recorded in earlier manual sessions. With discovery off, unconfigured barcodes are only logged:

```
INFO: Discovered unconfigured Tigo optimizer barcode: S-9999999Z (gateway=1, node=55).
```

#### Module Reconfiguration

Options changes reload the entry. Because the parser's node tables are restored from the Store, a newly added barcode that was seen before resolves from the next power report without waiting for another enumeration.

### 5. `sensor.py` — Sensor Platform

Creates sensor entities for configured modules at setup and for discovered modules as they are admitted (a coordinator listener adds entities for barcodes it hasn't seen yet).

#### Entity Model

Each tracked optimizer becomes a **device** with 12 sensors:

```
Device: "Tigo TS4 Panel_01"
  ├── Sensor: Performance      (%)   — SensorStateClass.MEASUREMENT
  ├── Sensor: Power            (W)   — SensorDeviceClass.POWER
  ├── Sensor: Voltage In       (V)   — SensorDeviceClass.VOLTAGE
  ├── Sensor: Voltage Out      (V)   — SensorDeviceClass.VOLTAGE
  ├── Sensor: Current In       (A)   — SensorDeviceClass.CURRENT
  ├── Sensor: Current Out      (A)   — SensorDeviceClass.CURRENT
  ├── Sensor: Temperature      (°C)  — SensorDeviceClass.TEMPERATURE
  ├── Sensor: DC-DC Duty Cycle (%)   — SensorStateClass.MEASUREMENT
  ├── Sensor: RSSI             (dBm) — SensorDeviceClass.SIGNAL_STRENGTH
  ├── Sensor: Daily Energy     (Wh)  — SensorDeviceClass.ENERGY, TOTAL
  ├── Sensor: Total Energy     (Wh)  — SensorDeviceClass.ENERGY, TOTAL_INCREASING
  └── Sensor: Readings Today   (—)   — EntityCategory.DIAGNOSTIC, TOTAL
```

Aggregate virtual devices:

- `Tigo String <name>`: `performance`, `power`, `daily_energy`, `total_energy`
- `Tigo Installation`: the same four, plus a diagnostic `module_discovery` enum sensor (`waiting_for_modules` / `modules_discovered`, attribute `module_count`) when automatic discovery is enabled.

Unique IDs: `pytap_<barcode>_<key>`, `pytap_<entry_id>_string_<name>_<key>`, `pytap_<entry_id>_installation_<key>`, `pytap_<entry_id>_module_discovery`.

#### Device Info

Optimizer devices are identified by barcode:

```python
DeviceInfo(
    identifiers={(DOMAIN, barcode)},   # e.g. ("pytap", "S-1234567A")
    name=f"Tigo TS4 {module_config['name']}",
    manufacturer="Tigo Energy",
    model="TS4",
    serial_number=barcode,
)
```

String and installation devices use `(DOMAIN, f"{entry_id}_string_{name}")` and `(DOMAIN, f"{entry_id}_installation")`.

#### Entity Updates

Sensors are `CoordinatorEntity` + `RestoreSensor`. On each update (a global coordinator update or a data listener for their barcode) they read `coordinator.data["nodes"][barcode][value_key]`, convert the duty cycle to a percentage and apply the stale-reading action. Per-optimizer sensors expose `string_group`, `last_update` and `gateway_id` attributes. On startup, values come from restored coordinator data or, failing that, HA's restore state; per-optimizer and aggregate performance stay unknown while any relevant panel rating is 0.

#### Entity Availability

| Entities                                                                       | Unavailable                                                                  | Zero                                          | Hold                           |
| ------------------------------------------------------------------------------ | ---------------------------------------------------------------------------- | --------------------------------------------- | ------------------------------ |
| Power, voltage in/out, current in/out, performance                             | Unavailable when stale                                                       | 0 when stale (performance unknown if unrated) | Last value                     |
| Temperature, duty cycle, RSSI                                                  | Unavailable when stale                                                       | Unavailable when stale                        | Last value                     |
| Daily/total energy, readings today                                             | Available once the node has data (energy sensors also with a restored value) | same                                          | same                           |
| String/installation power & performance                                        | Sum over reporting members; unavailable if none                              | Stale members count as 0 W; 0 if none         | Sum over all members with data |

In hold mode a sensor with no coordinator node but a restored HA state stays available with that value.

#### Aggregate Energy

String/installation energy sensors store their own total plus each member's last seen value (`baselines`) as extra restore data. On each update they add every member's increase since its baseline; new members and member decreases (e.g. midnight) only rebase. Membership changes therefore never add a panel's lifetime energy or look like a meter reset. Upgrading from an older version, the total aggregate continues from its restored plain sum. The daily aggregate starts a new cycle when the local date changes, and `last_reset` is the start of that cycle. Aggregate attributes: `optimizer_count`, `reporting_count`.

#### Barcode-to-Node Matching

The Tigo protocol identifies nodes by `node_id` (transient) and `barcode` (stable). The parser resolves barcodes from its node table (`PersistentState`) when it emits `PowerReportEvent`; when the barcode is `None`, the coordinator uses its own `node_id → barcode` map, but only after the current session's first node table (node IDs may have been reassigned overnight).

### 6. `connection.py` — Transports

- `normalize_connection()` validates TCP (host, port 1–65535) or serial settings (local paths are normalised with `posixpath.normpath` and must stay under `/dev/`, excluding `/dev/fd`, `/dev/std*`, `/dev/shm` and `/dev/mqueue`; proxy URLs must be `esphome-hass://esphome/<port>?…` with only known identity filters; baud 1–4000000), raising `InvalidConnectionSettings(field, error)`.
- `check_device_node()` (blocking, called by `open_source()` before any serial open, including the flow probe) requires a local path's symlink target to be a character device that passes the same `/dev/` checks.
- `resolved_endpoint_key()` resolves symlinks (e.g. `/dev/serial/by-id/…`) so the same port can't be claimed twice.
- `SerialProxySource` runs `serialx`'s async transport on HA's loop (8N1, no flow control, DTR/RTS untouched) behind a blocking `read()`/`close()` used by the worker; idle reads return `b""` after 1 s.
- `open_source()` opens a `SerialProxySource` or `pytap` `TcpSource`, closing it again if `connect()` fails.
- `connection_title()` never includes proxy URLs or serial identities.

### 7. `manifest.json` — Integration Metadata

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
  "requirements": ["serialx>=1.15.1"],
  "version": "1.2.0"
}
```

Key choices:
- **`integration_type: "hub"`** — A single entry manages multiple downstream devices (optimizers).
- **`iot_class: "local_push"`** — Data is pushed from the bus in real time.
- **`requirements`** — Only `serialx` (serial/proxy access); the `pytap` parser is embedded.

---

## Data Flow — End to End

```
 Tigo Gateway (RS-485 bus master)
     │
     │  TCP stream / local serial / ESPHome serial proxy
     ▼
 TcpSource / SerialProxySource .read(4096)   [executor thread]
     │
     │  raw bytes
     ▼
 pytap Parser.feed(bytes)
     │  Frame accumulator, CRC validation, frame dispatch,
     │  transport correlation, PV packet extraction,
     │  slot clock → timestamp, barcode resolution
     │
     │  list[Event]
     ▼
 Coordinator._process_event(event)  → updates private _state
     │  Configured / discovered barcode?  → merge
     │  Otherwise                          → log, remember barcode
     ▼
 Publish copy (live: now; averaged: once per interval)
     │  call_soon_threadsafe(async_set_updated_data)
     ▼
 Sensor entities (+ freshness check every 5 s)
     │  apply stale-reading action, write state
     ▼
 HA Frontend / Automations / History / Energy dashboard
```

---

## Key Design Decisions

### 1. Embedded Parser Library (No PyPI Dependency)

The `pytap` parser library is bundled inside the custom component at `custom_components/pytap/pytap/`:

- **Shipped together** — Each integration release bundles its parser; the parser's own `pytap.__version__` (currently 1.1.1) is not kept in step with the integration version (`manifest.json`, currently 1.2.0).
- **No PyPI publishing required** — Reduces release complexity for a niche integration.
- The only runtime requirement is `serialx`, for serial and proxy access.

The library has **no Home Assistant imports** and can be used standalone (scripts, other platforms). Persistence is handled externally — the parser accepts a `PersistentState` object and mutates it in memory; the caller owns serialization.

### 2. Push-Based Streaming (Not Polling)

| Aspect | Polling | Streaming (PyTap) |
| --- | --- | --- |
| Latency | `scan_interval` seconds | Sub-second (live mode) |
| Bandwidth | Redundant requests | Only new data |
| HA CPU | Timer fires + HTTP call | Idle until data arrives |
| Complexity | Simple `_async_update_data` | Background task + thread bridging |

Streaming fits a bus that broadcasts power reports continuously and is already push-based.

### 3. Thread Bridging via Executor

The worker uses blocking reads. Rather than rewriting the library with `asyncio`, the coordinator runs it in the HA executor:

```python
self._listener_task = self.config_entry.async_create_background_task(
    self.hass,
    self._async_listen(),   # awaits hass.async_add_executor_job(self._listen)
    name="pytap_listener",
)
```

Serial/proxy I/O stays on HA's loop (`serialx`), bridged to the worker with `run_coroutine_threadsafe` and bounded timeouts.

### 4. Worker-Owned State, Published Copies

The worker mutates only `_state`; Home Assistant receives copies. This removes data races between the worker and entities/diagnostics and keeps averaged values from being overwritten by raw ones.

### 5. Passive Discovery With a Manual Mode

New setups discover optimizers passively (no bus commands). Users can switch to manual mode to track only listed barcodes, e.g. when neighbouring installations share the bus. Removed or deleted optimizers are excluded from discovery, and a barcode already tracked by another PyTap entry is not adopted. Existing installations keep manual mode after migration.

### 6. Opt-In Stale Readings

The default, `hold`, keeps the original behaviour for new and existing entries. Users can opt in to `unavailable`, so dashboards and automations don't show night-time or dropped-out values as current after a configurable timeout, or `zero`, for dashboards that prefer 0 W at night. Energy totals are never affected.

### 7. Device Hierarchy

The device registry has one device per optimizer (barcode), one virtual device per string, and one installation device. There is no gateway device and optimizers have no `via_device`.

### 8. Coordinator Data Keyed by Barcode

Node data is keyed by `barcode` (not `node_id`), since barcodes are stable while node IDs can change across gateway restarts. Entity lookups are O(1) and entities don't depend on `pytap` event types.

---

## Configuration

### User-Facing Config (stored in `ConfigEntry.data`)

| Parameter | Type | Default | Description |
| --- | --- | --- | --- |
| `connection_type` | string | — | `tcp` or `serial` |
| `host` | string | (required for TCP) | IP address or hostname of the gateway/converter |
| `port` | int | 502 | TCP port |
| `serial_port` | string | (required for serial) | `/dev/…` path or `esphome-hass://` proxy URL |
| `baud_rate` | int | 38400 | Serial speed (8N1 fixed) |
| `modules` | list | `[]` | Configured modules (`string`, `name`, `barcode`, `peak_power`) |
| `auto_discovery` | bool | `True` for new setups, `False` after migration | Admit new optimizers automatically |
| `excluded_barcodes` | list | `[]` | Barcodes discovery must ignore |
| `live_reporting` | bool | `True` | Push every reading immediately vs. averaged interval |
| `write_interval` | int | 30 | Seconds between averaged pushes (5–300) |
| `stale_action` | string | `hold` | `hold`, or opt-in `unavailable` / `zero` |
| `stale_timeout` | int | 120 | Seconds without a valid report before a panel is stale (60–600) |

**Module fields:** `string` (required group), `name` (required label), `barcode` (required, `X-NNNNNNNC`), `peak_power` (Wp; 455 by default when added manually, 0 = unknown).

### Internal Constants

| Constant | Value | Description |
| --- | --- | --- |
| `RECONNECT_TIMEOUT` | 60 s | Seconds of silence before reconnecting |
| `RECONNECT_DELAY` | 5 s | Delay between reconnection attempts |
| `RECONNECT_RETRIES` | 0 | Max retries (0 = infinite) |
| `CONNECTION_TIMEOUT` | 10 s | Open/connect bound |
| `SERIAL_READ_TIMEOUT` | 1 s | Idle serial read bound |
| `LISTENER_STOP_TIMEOUT` | 20 s | Bound for stopping the listener |
| `FRESHNESS_CHECK_INTERVAL_SECONDS` | 5 s | Stale-panel check period |
| `ENERGY_GAP_THRESHOLD_SECONDS` | 120 s | Longer gaps are not integrated |

### Options Flow

Configurable at runtime without removing the integration: connection, reporting mode/interval, stale-reading action/timeout, discovery, and module add/edit/remove. Each saved change reloads the entry; persisted mappings, discovered modules and energy survive the reload.

---

## Error Handling

| Scenario | Behavior |
| --- | --- |
| Gateway unreachable in config/options flow | Form shows `cannot_connect`; resubmitting the same settings saves them |
| Gateway unreachable at runtime | Worker retries every `RECONNECT_DELAY`; logged once at WARNING |
| Invalid saved connection settings | `ConfigEntryError` with the offending setting; saving corrected settings retries setup |
| Previous listener still stopping | `ConfigEntryNotReady`; HA retries setup |
| Saved state can't be read | WARNING; orphan cleanup skipped, no Store writes until the next reload |
| CRC error in protocol data | Parser increments `counters["crc_errors"]`, skips frame |
| Malformed frame (runt/giant) | Parser increments counter, resumes at next frame boundary |
| Invalid slot counter in receive-response header | `invalid_slots` incremented; frame kept only if its packet walk is strict, else dropped |
| Power report with invalid slot counter | `invalid_slots` incremented; report dropped |
| Power report before the gateway has a slot clock | `no_slot_clock_drops` incremented; report dropped; WARNING once per gateway, then DEBUG |
| Node table page with trailing bytes | Declared entries parsed, extra bytes ignored |
| Node address with bit-15 flag set | Masked to 15 bits (`& 0x7FFF`) |
| No data for `RECONNECT_TIMEOUT` | Worker reconnects |
| Node stops reporting | Live readings follow `stale_action` after `stale_timeout` |
| Barcode not yet identified | Report counted as pending until a node table resolves it |
| Unconfigured barcode seen | Logged at INFO; admitted if discovery is on and it isn't excluded or owned by another entry |
| First infra event without node table | Logged at INFO; existing mappings kept |

---

## Testing Strategy

### Integration Tests (`tests/`)

| Test File | Scope |
| --- | --- |
| `test_config_flow.py` | Config/options flow forms, validation, module menu |
| `test_serial_config_flow.py` | Serial picker, offline setup, transport changes |
| `test_connection.py` | Transport validation, passive reads, bounded cleanup |
| `test_coordinator_persistence.py` | Event processing, Store save/load, barcode mapping lifecycle |
| `test_write_interval.py` | Live vs. averaged reporting, time-weighted averaging |
| `test_panel_status.py` | Stale-reading actions, aggregate energy |
| `test_freshness_listeners.py` | Freshness changes re-render only affected panels and aggregates |
| `test_review2_data.py` | Stale default, barcode claims, selective updates, aggregates |
| `test_review2_lifecycle.py` | Device cleanup, serial path checks, parked listeners, removal |
| `test_module_discovery.py` | Passive admission, persistence, metadata edits |
| `test_listener_lifecycle.py` | Worker stop/port release before unload/reload |
| `test_review_fixes.py` | Lifecycle, device registry and connection-safety regressions |
| `test_sensor.py` | Entity creation, state updates, aggregates |
| `test_diagnostics.py` | Diagnostics payload and redaction |
| `test_energy.py` | Trapezoidal energy accumulation |
| `test_migration.py` | Config entry migration and legacy entity cleanup |
| `core_smoke.py` | Smoke tests against HA 2026.11 without the test harness |

### Parser Tests (`custom_components/pytap/pytap/tests/`)

| Test File | Scope |
| --- | --- |
| `test_parser.py` | End-to-end byte → event parsing with captured data |
| `test_types.py` | Protocol type construction and validation |
| `test_crc.py` | CRC calculation against known vectors |
| `test_barcode.py` | Barcode encode/decode round-trips |
| `test_api.py` | Public API function tests |

### Integration Testing

- **Dev container** — The repository includes a dev container with HA installed. Run `python3 -m homeassistant --config config/ --debug` to test the full integration locally.
- **Mock source** — For automated tests, the coordinator can be tested with a mock `Source` that replays captured byte sequences.


