# PyTap — Home Assistant Custom Component Architecture

## Overview

PyTap is a Home Assistant custom component that passively monitors Tigo TAP solar energy systems via the RS-485 bus. It connects to a Tigo gateway (over TCP or serial), parses the proprietary protocol in real time, and exposes per-optimizer sensor entities — power, voltage, current, temperature, and more — directly in Home Assistant.

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
│                          │ TCP / Serial                             │
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
│   └── architecture.md             # This file
├── config/                         # HA dev config directory
│   ├── configuration.yaml
│   └── custom_components → ../custom_components  (symlink)
├── custom_components/
│   └── pytap/                      # HA custom component
│       ├── __init__.py             # Integration setup / teardown
│       ├── config_flow.py          # UI-based configuration
│       ├── const.py                # Domain, defaults, config keys
│       ├── coordinator.py          # DataUpdateCoordinator (async bridge)
│       ├── sensor.py               # Sensor entities (power, voltage, etc.)
│       ├── manifest.json           # HA integration metadata
│       ├── strings.json            # UI strings (source)
│       ├── translations/
│       │   └── en.json             # English translations
│       └── pytap/                  # Embedded protocol parser library
│           ├── __init__.py         # Library re-exports
│           ├── api.py              # Public API (create_parser, parse_bytes, connect)
│           ├── core/
│           │   ├── parser.py       # Protocol parser (bytes → events)
│           │   ├── types.py        # Protocol types & constants
│           │   ├── events.py       # Event dataclasses
│           │   ├── state.py        # SlotClock, NodeTableBuilder, PersistentState
│           │   ├── source.py       # TcpSource, SerialSource
│           │   ├── crc.py          # CRC-16-CCITT
│           │   └── barcode.py      # Tigo barcode encode/decode
├── tests/
│   ├── conftest.py                 # HA test fixtures
│   ├── test_config_flow.py         # Config flow tests
│   ├── test_coordinator_persistence.py  # Coordinator & persistence tests
│   ├── test_migration.py           # Entity migration tests
│   └── test_sensor.py              # Sensor platform tests
├── requirements.txt                # Pinned HA + dev dependencies
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
│  │  Config Flow  │  │   Coordinator     │  │   Sensors     │  │
│  │              │  │                   │  │               │  │
│  │  • User form │  │  • Async bridge   │  │  • Per-node   │  │
│  │  • Validate  │  │  • Thread mgmt   │  │    entities   │  │
│  │    connection│  │  • Event routing  │  │  • Device     │  │
│  │  • Store     │  │  • State cache    │  │    grouping   │  │
│  │    config    │  │                   │  │  • Unit       │  │
│  │              │  │                   │  │    conversion │  │
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
│  pytap.api.connect(config) → Source                          │
│                                                              │
│  No HA dependency — pure protocol logic, stdlib only         │
└──────────────────────────────────────────────────────────────┘
```

---

## Component Modules

### 1. `__init__.py` — Integration Setup

Entry point for Home Assistant. Implements the two required lifecycle hooks:

| Function | Purpose |
| --- | --- |
| `async_setup_entry(hass, entry)` | Create coordinator, start data streaming, forward platform setup |
| `async_unload_entry(hass, entry)` | Stop coordinator, unload platforms, clean up |

**Setup flow:**
1. Instantiate `PyTapDataUpdateCoordinator` with host/port from `ConfigEntry`.
2. Call `coordinator.async_config_entry_first_refresh()` to establish the initial connection and validate it works.
3. Store coordinator in `hass.data[DOMAIN][entry.entry_id]`.
4. Forward setup to the `sensor` platform.

**Teardown flow:**
1. Unload platforms.
2. Coordinator cancels its background listener task.
3. Remove coordinator from `hass.data`.

### 2. `config_flow.py` — Configuration UI

Implements a multi-step `ConfigFlow` to collect connection parameters and module barcodes via the HA frontend:

| Step | Fields | Validation |
| --- | --- | --- |
| `user` | `host` (required), `port` (default: 502) | Attempt TCP connection to validate reachability |
| `modules_menu` | Menu (`add_module` / `finish`) | Ensures explicit module-by-module setup |
| `add_module` | `string` (required), `name` (required), `barcode` (required) | Non-empty fields, barcode format, duplicate prevention |

**Step 1 — Connection:** The user provides the gateway host and port. Validation opens a short-lived TCP connection using `pytap.api.connect()` (run in the executor). On success, proceeds to step 2. On failure, shows "cannot_connect".

**Step 2 — Modules:** The user adds modules one at a time from a menu-driven flow:

- **`STRING`** — Required string/group name (e.g., `A`, `B`, `East`, `West`).
- **`NAME`** — Required user-friendly name for the optimizer (e.g., `Panel_01`, `Roof_North_3`).
- **`BARCODE`** — Required Tigo barcode from the module sticker (e.g., `S-1234567A`).

Barcodes are validated against the `X-NNNNNNNC` format and duplicates are rejected. The module list is stored in `ConfigEntry.data["modules"]` as:

```python
[
    {"string": "A", "name": "Panel_01", "barcode": "A-1234567B"},
    {"string": "A", "name": "Panel_02", "barcode": "C-2345678D"},
    {"string": "B", "name": "Panel_03", "barcode": "E-3456789F"},
]
```

**Unique ID:** Based on `host:port` to prevent duplicate entries for the same gateway.

**Options Flow:** The module list can be edited after setup via an Options Flow without removing the integration. This allows adding/removing optimizers as the installation evolves.

### 3. `const.py` — Constants

```python
DOMAIN = "pytap"
DEFAULT_PORT = 502           # Tigo TAP default Modbus/TCP port
DEFAULT_SCAN_INTERVAL = 30   # Coordinator poll fallback (seconds)
```

### 4. `coordinator.py` — Data Update Coordinator

The coordinator is the central bridge between the blocking `pytap` parser and Home Assistant's async event loop. It uses a **push-based streaming model** rather than the typical polling pattern.

#### Architecture

```
┌─────────────────────────────────────────────────┐
│  PyTapDataUpdateCoordinator                     │
│                                                 │
│  Main thread (HA event loop):                   │
│    • Exposes self.data to sensor entities        │
│    • Calls async_set_updated_data() on events   │
│    • Manages lifecycle (start/stop)              │
│                                                 │
│  Executor thread (blocking I/O):                │
│    • pytap.api.connect() → Source               │
│    • parser.feed(source.read()) → Events        │
│    • Schedules callbacks back to event loop      │
│                                                 │
│  ┌──────────────────────────────────────────┐   │
│  │  self.data (dict[str, Any])              │   │
│  │                                          │   │
│  │  {                                       │   │
│  │    "gateways": {                         │   │
│  │      1: { "address": "...", "version": "1.2" }│
│  │    },                                    │   │
│  │    "nodes": {                            │   │
│  │      "S-1234567A": {                     │   │
│  │        "gateway_id": 1,                  │   │
│  │        "node_id": 42,                    │   │
│  │        "barcode": "S-1234567A",          │   │
│  │        "name": "Panel_01",               │   │
│  │        "string": "A",                    │   │
│  │        "power": 343.0,                   │   │
│  │        "voltage_in": 38.5,               │   │
│  │        "voltage_out": 39.2,              │   │
│  │        "current": 8.75,                  │   │
│  │        "temperature": 45.2,              │   │
│  │        "dc_dc_duty_cycle": 0.78,         │   │
│  │        "rssi": -65,                      │   │
│  │        "last_update": datetime(...)       │   │
│  │      }                                   │   │
│  │    },                                    │   │
│  │    "counters": { ... },                  │   │
│  │    "discovered_barcodes": ["S-9999999Z"] │   │
│  │  }                                       │   │
│  └──────────────────────────────────────────┘   │
└─────────────────────────────────────────────────┘
```

#### Threading Model

The `pytap` library uses blocking I/O (`socket.recv`, `serial.read`). Since Home Assistant's core runs on `asyncio`, the coordinator must bridge the two worlds:

1. **Background listener task** — An `asyncio.Task` created at setup that runs the blocking `_listen()` method in the executor via `hass.async_add_executor_job()`.
2. **Event dispatch** — When the executor thread receives parsed events, it schedules `coordinator.async_set_updated_data()` back on the event loop via `hass.loop.call_soon_threadsafe()`.
3. **Cancellation** — On unload, the task is cancelled and the source connection is closed, which unblocks the `read()` call.

```
 HA Event Loop (main thread)          Executor Thread
 ─────────────────────────            ────────────────
        │                                    │
        │  async_setup_entry()               │
        │  ─► create coordinator             │
        │  ─► start listener task ──────────►│
        │                                    │ source = connect(config)
        │                                    │ parser = create_parser()
        │                                    │
        │                                    │ loop:
        │                                    │   data = source.read(4096)
        │                                    │   events = parser.feed(data)
        │  ◄── call_soon_threadsafe ─────────│   for event in events:
        │      async_set_updated_data(...)   │     dispatch(event)
        │                                    │
        │  entities read coordinator.data    │
        │  and update their state            │
        │                                    │
        │  async_unload_entry()              │
        │  ─► cancel task ──────────────────►│ (source.close() → unblocks read)
        │                                    │ exits
```

#### Reconnection

The coordinator handles connection failures and source timeouts with automatic reconnection:

- **Initial connection failure** — Raises `UpdateFailed`, HA marks the integration as unavailable and retries using its standard backoff.
- **Mid-stream disconnection** — The listener task catches the exception, logs a warning, waits `RECONNECT_DELAY` seconds, and re-establishes the connection.
- **Silence timeout** — If no data arrives for `RECONNECT_TIMEOUT` seconds, the coordinator assumes the connection is stale and reconnects.

#### Data Merging Strategy

The coordinator maintains a cumulative state dictionary. Incoming events are merged, not replaced:

- **`PowerReportEvent`** — If the event's `barcode` matches a configured module, upserts into `data["nodes"][barcode]` with all power fields + `last_update` timestamp. Events for unconfigured barcodes are **discarded** (but logged at DEBUG level for discovery — see below).
- **`InfrastructureEvent`** — Replaces `data["gateways"]` and rebuilds the barcode→node_id mapping from scratch. The first infrastructure event in a session may arrive without barcodes (gateway identity only — node table not yet received); barcode resolution activates once the gateway sends the full node table. Subsequent events log the match count and any configured barcodes not found in the node table.
- **`TopologyEvent`** — Updates topology fields for matched nodes only.
- **`StringEvent`** — Logged for diagnostics; not stored in entity state.

#### Persistence

All persistent state is consolidated into a single **HA Store** (`<config>/.storage/pytap_<entry_id>_coordinator`), written via `homeassistant.helpers.storage.Store` (version 2). The store contains:

- **`barcode_to_node`** — Barcode↔node_id mappings learned from infrastructure events.
- **`discovered_barcodes`** — Set of unconfigured barcodes seen on the bus.
- **`parser_state`** — Serialised parser infrastructure state (gateway identities, versions, node tables) via `PersistentState.to_dict()`.

Saves are debounced (10s delay) to avoid excessive writes. On shutdown, any pending unsaved changes are flushed immediately.

On startup, the coordinator loads all state from the HA Store. The parser receives a shared `PersistentState` object (deserialized from the store) which it mutates in memory as new infrastructure events arrive. The coordinator owns persistence — the parser never performs file I/O.

This single-store approach ensures:
- No raw file I/O on the event loop (HA Store is fully async).
- Automatic cleanup when a config entry is removed.
- Proper inclusion in HA backups.
- Version migration support via the Store's built-in versioning.

#### Barcode Filtering & Discovery Logging

The coordinator builds a **barcode allowlist** from `ConfigEntry.data["modules"]` at startup. Only events whose `barcode` field matches the allowlist are forwarded to entity state.

For unconfigured barcodes, the coordinator logs a message at `INFO` level:

```
INFO: Discovered unconfigured Tigo optimizer barcode: S-9999999Z (gateway=1, node=55). Add it to your PyTap module list to start tracking.
```

This approach mirrors the [taptap add-on](https://github.com/litinoveweedle/hassio-addons) pattern: users can monitor the HA log for unconfigured barcodes and add them via the Options Flow. Since barcode discovery messages (`InfrastructureEvent`) from the Tigo gateway can be infrequent (sometimes only during overnight enumeration cycles), users should allow up to 24 hours for full discovery.

The coordinator also maintains a `data["discovered_barcodes"]` set of all seen-but-unconfigured barcodes, persisted to the HA Store for survival across restarts. Discovered barcodes are persisted alongside barcode↔node_id mappings so that when a user later adds a previously-discovered barcode via the Options Flow, it can be resolved immediately from saved state without waiting for the next infrastructure event.

#### Module Reconfiguration

When the user adds or removes modules via the Options Flow, the integration reloads. On reload, the newly-created coordinator loads saved barcode↔node_id mappings from the HA Store. If a newly-added barcode already exists in the saved mappings, a placeholder entry is created in coordinator data so that sensor entities can bind immediately — no need to wait for the next power report or infrastructure event.

### 5. `sensor.py` — Sensor Platform

Creates sensor entities **only** for optimizer modules explicitly listed in the user's configuration. No auto-discovery — the user provides the list of barcodes they want to track.

#### Entity Model

Each configured Tigo TS4 optimizer module becomes a **device** in the HA device registry, with 10 sensor entities:

```
Device: "Tigo TS4 Panel_01" (user-defined name from config)
  ├── Sensor: Power          (W)   — SensorDeviceClass.POWER
  ├── Sensor: Voltage In     (V)   — SensorDeviceClass.VOLTAGE
  ├── Sensor: Voltage Out    (V)   — SensorDeviceClass.VOLTAGE
    ├── Sensor: Current In     (A)   — SensorDeviceClass.CURRENT
    ├── Sensor: Current Out    (A)   — SensorDeviceClass.CURRENT
  ├── Sensor: Temperature    (°C)  — SensorDeviceClass.TEMPERATURE
  ├── Sensor: DC-DC Duty Cycle (%) — SensorStateClass.MEASUREMENT
    ├── Sensor: RSSI           (dBm) — SensorDeviceClass.SIGNAL_STRENGTH
    ├── Sensor: Daily Energy   (Wh)  — SensorDeviceClass.ENERGY
    └── Sensor: Total Energy   (Wh)  — SensorDeviceClass.ENERGY
```

Aggregate virtual devices are also created:

- `Tigo String <name>`: 3 sensors (`power`, `daily_energy`, `total_energy`)
- `Tigo Installation`: 3 sensors (`power`, `daily_energy`, `total_energy`)

#### Device Info

Devices are identified by barcode (stable across gateway restarts and node_id reassignments):

```python
DeviceInfo(
    identifiers={(DOMAIN, barcode)},   # e.g. ("pytap", "S-1234567A")
    name=f"Tigo TS4 {module_config['name']}",  # user-defined name
    manufacturer="Tigo Energy",
    model="TS4",
    serial_number=barcode,
)
```

#### Entity Creation — Barcode-Driven (No Auto-Discovery)

Unlike auto-discovery integrations, entities are created **deterministically** from the configured module list:

1. At `async_setup_entry`, the sensor platform reads `ConfigEntry.data["modules"]`.
2. For each configured module, it creates the full set of 10 per-optimizer sensor entities immediately.
3. It also creates aggregate sensors per distinct string and for the whole installation.
4. Entities start in an **unavailable** state until the first matching `PowerReportEvent` arrives from the bus.
5. When the coordinator receives a `PowerReportEvent` with a barcode matching a configured module, the corresponding entities become available and display live data.

This approach is inspired by the [taptap HA add-on](https://github.com/litinoveweedle/hassio-addons) which similarly requires users to define `taptap_modules` as `STRING:NAME:SERIAL` triplets.

**Rationale for explicit configuration over auto-discovery:**

- **Predictable entity IDs** — Users know exactly which entities will exist, enabling dashboards and automations to be set up before the first data arrives.
- **No phantom entities** — Auto-discovery can create entities for neighbor nodes on adjacent installations sharing the same RS-485 bus. Explicit barcodes prevent this.
- **User-friendly names** — Names are defined by the user (e.g., "Roof_East_Panel_03") rather than opaque node IDs.
- **String grouping** — The required `string` field powers deterministic per-string aggregate entities.

#### Barcode-to-Node Matching

The Tigo protocol identifies nodes by `node_id` (a transient 16-bit integer) and `barcode` (a stable hardware identifier). The mapping between them is learned from `InfrastructureEvent`s (gateway enumeration).

```
  Config:  ["S-1234567A", "S-1234568B", ...]    (user-provided barcodes)
                  │
                  ▼
  Coordinator:  barcode → node_id mapping         (learned from InfrastructureEvent)
                  │
                  ▼
  PowerReportEvent(node_id=42, barcode="S-1234567A")
                  │
                  ▼
  Coordinator:  barcode in allowlist? → YES → update data["nodes"]["S-1234567A"]
                                       NO  → log discovery, discard
```

If the parser already resolves barcodes in `PowerReportEvent.barcode` (via its internal node table), the coordinator matches directly. For events where the barcode is `None` (node table not yet populated), the coordinator uses its own `node_id → barcode` mapping built from prior `InfrastructureEvent`s.

#### Entity Updates

Sensor entities inherit from `CoordinatorEntity` and implement `_handle_coordinator_update()`:

```python
@callback
def _handle_coordinator_update(self) -> None:
    node_data = self.coordinator.data.get("nodes", {}).get(self._barcode)
    if node_data:
        self._attr_native_value = node_data.get(self._value_key)
        self._attr_available = True
    else:
        self._attr_available = False
    self.async_write_ha_state()
```

#### Entity Availability

Entities are marked unavailable only when:
- The node's barcode has **never** been seen on the bus (no `PowerReportEvent` received yet for this barcode).

Once a sensor receives its first value, it remains available and holds the last received value indefinitely. This is intentional: solar optimizers stop reporting overnight, and displaying the last known values (rather than marking entities unavailable) provides a better user experience for dashboards and history.

### 6. `manifest.json` — Integration Metadata

```json
{
  "domain": "pytap",
  "name": "PyTap",
  "codeowners": ["@azebro"],
  "config_flow": true,
  "documentation": "https://github.com/azebro/pytap",
  "integration_type": "hub",
  "iot_class": "local_push",
  "requirements": [],
  "version": "0.1.0"
}
```

Key choices:
- **`integration_type: "hub"`** — A single gateway entry manages multiple downstream devices (optimizer nodes).
- **`iot_class: "local_push"`** — Data is pushed from the device in real time (not polled). The coordinator streams events as they arrive from the bus.
- **`requirements: []`** — The `pytap` parser library is embedded, not installed from PyPI. No external dependencies beyond stdlib.

---

## Data Flow — End to End

```
 Tigo Gateway (RS-485 bus master)
     │
     │  TCP stream (port 502)
     ▼
 pytap TcpSource.read(4096)
     │
     │  raw bytes
     ▼
 pytap Parser.feed(bytes)
     │
     │  Frame accumulator state machine
     │  CRC validation
     │  Frame dispatch by type
     │  Transport correlation (request/response pairing)
     │  PV packet extraction
     │  Slot clock → timestamp mapping
     │  Barcode resolution
     │
     │  list[Event]
     ▼
 Coordinator._process_event(event)
     │
     │  Barcode in configured allowlist?
     │  ├── YES → Merge into self.data["nodes"][barcode]
     │  └── NO  → Log discovery, add to discovered_barcodes set, discard
     │
     │  async_set_updated_data()
     ▼
 Home Assistant Entity Registry
     │
     │  CoordinatorEntity._handle_coordinator_update()
     │  → updates native_value, availability (keyed by barcode)
     ▼
 HA Frontend / Automations / History
     │
     │  • Real-time dashboard cards (power, voltage, temp)
     │  • History graphs
     │  • Automations (e.g., alert on low power / high temp)
     │  • Energy dashboard integration
```

---

## Key Design Decisions

### 1. Embedded Parser Library (No PyPI Dependency)

The `pytap` parser library is bundled inside the custom component at `custom_components/pytap/pytap/`. This means:

- **Zero external dependencies** — Easier to install (just copy the folder into `custom_components/`).
- **Version lock** — The parser version always matches the integration version.
- **No PyPI publishing required** — Reduces release complexity for a niche integration.

The library maintains a clean boundary: it has **no Home Assistant imports** and can be used standalone (scripts, other platforms). Persistence is handled externally — the parser accepts a `PersistentState` object and mutates it in memory; the caller owns serialization.

### 2. Push-Based Streaming (Not Polling)

Unlike most HA integrations that poll an API on an interval, PyTap uses a continuous streaming model:

| Aspect | Polling | Streaming (PyTap) |
| --- | --- | --- |
| Latency | `scan_interval` seconds | Sub-second |
| Bandwidth | Redundant requests | Only new data |
| HA CPU | Timer fires + HTTP call | Idle until event arrives |
| Complexity | Simple `_async_update_data` | Background task + thread bridging |

Streaming is the correct choice because the Tigo bus produces a continuous flow of power reports (~1 per optimizer per 5-second slot cycle), and the bus protocol is already push-based.

### 3. Thread Bridging via Executor

The `pytap` library uses blocking `socket.recv()` / `serial.read()` calls. Rather than rewriting the library with `asyncio`, the coordinator runs the blocking listener in the HA executor thread pool:

```python
self._listener_task = entry.async_create_background_task(
    hass,
    hass.async_add_executor_job(self._listen),
    name="pytap_listener",
)
```

This keeps the parser library simple and portable while integrating cleanly with HA's async architecture.

### 4. Explicit Barcode Configuration (No Auto-Discovery)

Rather than auto-discovering nodes from the bus, users explicitly configure which optimizer barcodes to track. This is modeled after the [taptap HA add-on's](https://github.com/litinoveweedle/hassio-addons) `taptap_modules` pattern. Benefits:

- **No phantom entities** from neighbor installations sharing the RS-485 bus.
- **Predictable entity IDs** that can be referenced in automations before the first data arrives.
- **User-defined names** instead of opaque node IDs.
- **Discovery logging** surfaces unconfigured barcodes in the HA log so users can add them.

### 5. Device Hierarchy (Gateway → Nodes)

The HA device registry mirrors the physical topology:

```
Gateway 1 (via_device: None)
  ├── Node 42 (via_device: Gateway 1)
  ├── Node 43 (via_device: Gateway 1)
  └── Node 44 (via_device: Gateway 1)

Gateway 2 (via_device: None)
  ├── Node 101 (via_device: Gateway 2)
  └── Node 102 (via_device: Gateway 2)
```

This allows users to see which gateway each optimizer is connected through, matching the physical wiring.

### 6. Coordinator Data as Flat Dict

The coordinator stores node data as a flat dictionary keyed by `barcode` (not `node_id`), since barcodes are stable identifiers while node IDs can change across gateway restarts. This makes entity lookups O(1) and avoids coupling entities to the `pytap` event type hierarchy.

---

## Configuration

### User-Facing Config (via Config Flow)

| Parameter | Type | Default | Description |
| --- | --- | --- | --- |
| `host` | string | (required) | IP address or hostname of the Tigo gateway |
| `port` | int | 502 | TCP port for the gateway's RS-485 bridge |
| `modules` | list | (required) | List of optimizer modules as `STRING:NAME:BARCODE` triplets |

**Module format:** Each module is a triplet `STRING:NAME:BARCODE` where:
- `STRING` — Optional group name (e.g., `A`, `East`). Omit if not grouping.
- `NAME` — Required user-friendly label.
- `BARCODE` — Optional Tigo barcode (e.g., `S-1234567A`). If omitted, matched by discovery order.

Example: `A:Panel_01:S-1234567A, A:Panel_02:S-1234568B, B:Panel_03:S-2345678C`

### Internal Constants

| Constant | Value | Description |
| --- | --- | --- |
| `RECONNECT_TIMEOUT` | 60s | Seconds of silence before reconnecting |
| `RECONNECT_DELAY` | 5s | Delay between reconnection attempts |
| `RECONNECT_RETRIES` | 0 | Max retries (0 = infinite) |

### Options Flow

Configurable at runtime without removing the integration:

- **Module list** — Add or remove optimizer barcodes as the installation evolves.
- **Connection settings** — Change the gateway host and port without reconfiguring.

Changes trigger a full integration reload. Previously-discovered barcode mappings are persisted and survive the reload.

---

## Error Handling

| Scenario | Behavior |
| --- | --- |
| Gateway unreachable at setup | Config flow warns but proceeds (non-blocking) |
| Connection lost mid-stream | Coordinator reconnects automatically with backoff |
| CRC error in protocol data | Parser increments `counters["crc_errors"]`, skips frame |
| Malformed frame (runt/giant) | Parser increments counter, resumes at next frame boundary |
| Node table page with trailing bytes | Parser tolerates trailing bytes on sentinel (count=0) and data pages; parses the declared entries and ignores extra bytes |
| Node address with bit-15 flag set | Parser masks node addresses to 15 bits (`& 0x7FFF`) when parsing node table entries; bit 15 is a protocol flag (router/repeater) not part of the node ID |
| No data for `RECONNECT_TIMEOUT` | Coordinator reconnects (stale connection detection) |
| Node stops reporting | Entity holds last received value and remains available |
| Barcode not yet identified | Entity stays unavailable until gateway enumeration resolves the barcode |
| Unconfigured barcode seen | Logged at INFO level for discovery; event data discarded |
| First infra event without node table | Logged at INFO; barcode resolution deferred until node table arrives |

---

## Testing Strategy

### Unit Tests (`tests/`)

| Test File | Scope |
| --- | --- |
| `test_config_flow.py` | Config flow form rendering, validation, error handling (13 tests) |
| `test_coordinator_persistence.py` | Event processing, persistence, barcode mapping lifecycle (19 tests) |
| `test_migration.py` | Config entry migration and legacy entity cleanup (5 tests) |
| `test_sensor.py` | Entity creation, state updates, availability (7 tests) |

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


