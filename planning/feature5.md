# Feature 5: Diagnostics Platform — Implementation Plan

## Overview

The requirement from `future_considerations.md` has two distinct parts:

1. **Diagnostics download** — Expose parser counters (`frames_received`, `crc_errors`, `noise_bytes`) and infrastructure state as a downloadable JSON via the HA diagnostics platform, for troubleshooting.
2. **Per-sensor "readings received" daily counter** — A daily meter on each optimizer sensor that counts how many power reports were received for that module today. This helps investigate per-module connectivity issues.

These are **independent deliverables** that touch different layers of the integration. This plan covers both.

---

## Part A: Diagnostics Download

### A.1 What Is the HA Diagnostics Platform?

Home Assistant provides a built-in diagnostics integration (`homeassistant.components.diagnostics`). When an integration implements the `async_get_config_entry_diagnostics` function in a `diagnostics.py` file, the user gets a "Download diagnostics" button on the integration's config entry page. The output is a JSON file containing whatever the integration chooses to expose. Sensitive data should be redacted.

Reference: https://developers.home-assistant.io/docs/core/integration-quality-scale/rules/diagnostics/

### A.2 Data to Expose

The diagnostics JSON should include:

| Section | Source | Contents |
|---------|--------|----------|
| **Parser counters** | `coordinator.data["counters"]` (populated from `parser.counters` on every event) | `frames_received`, `crc_errors`, `runts`, `giants`, `noise_bytes` |
| **Infrastructure state** | `coordinator.data["gateways"]` | Gateway IDs, addresses, firmware versions |
| **Node table** | Derived from coordinator's `_barcode_to_node` / `_node_to_barcode` | Barcode ↔ node_id mappings |
| **Discovered barcodes** | `coordinator.data["discovered_barcodes"]` | Barcodes seen on bus but not configured |
| **Connection state** | Coordinator internal state | Host, port, whether infra has been received (`_infra_received`), pending power report count |
| **Configured modules** | `entry.data[CONF_MODULES]` | Module list (barcode, name, string, peak_power) |
| **Per-barcode last_update** | `coordinator.data["nodes"][barcode]["last_update"]` for each configured barcode | Timestamp of most recent reading per module |
| **Energy accumulation state** | `coordinator._energy_state` | Per-barcode daily/total energy, last reading timestamp |

### A.3 Sensitive Data Handling

**Decision required:** What counts as sensitive?

| Field | Sensitive? | Rationale |
|-------|-----------|-----------|
| `host` (IP address) | **Yes** — redact | Local network IP could identify the user's LAN topology |
| `port` | No | Standard port number, not identifying |
| Barcodes | **Option A: Redact** / **Option B: Keep** | Barcodes are device serial numbers. They're not secret per se (printed on the physical device), but could identify a specific installation. However, they are essential for diagnosing per-module issues. |
| Gateway addresses (LongAddress) | **Option A: Redact** / **Option B: Keep** | Same reasoning as barcodes — hardware identifiers. |
| Module names | No | User-chosen labels, useful for diagnostics |

**Recommendation:** Redact `host`. Keep barcodes and gateway addresses unredacted — they are hardware identifiers needed for meaningful diagnostics. Use HA's `async_redact_data` helper for the config entry data.

### A.4 Implementation: `diagnostics.py`

Create `custom_components/pytap/diagnostics.py`:

```python
"""Diagnostics support for PyTap."""

from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST
from homeassistant.core import HomeAssistant

from .const import DOMAIN
from .coordinator import PyTapDataUpdateCoordinator

TO_REDACT = {CONF_HOST}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: ConfigEntry
) -> dict[str, Any]:
    """Return diagnostics for a config entry."""
    coordinator: PyTapDataUpdateCoordinator = hass.data[DOMAIN][entry.entry_id]

    return {
        "config_entry": async_redact_data(entry.as_dict(), TO_REDACT),
        "counters": coordinator.data.get("counters", {}),
        "gateways": coordinator.data.get("gateways", {}),
        "discovered_barcodes": coordinator.data.get("discovered_barcodes", []),
        "node_mappings": {
            "barcode_to_node": ...,  # from coordinator
            "node_to_barcode": ...,  # from coordinator
        },
        "connection_state": {
            "infra_received": ...,
            "pending_power_reports": ...,
        },
        "nodes": ...,  # per-node last_update + energy state summary
    }
```

The exact shape is outlined above. The coordinator needs to expose the necessary internal state for the diagnostics function to read. Two options:

**Option 1 — Public properties on the coordinator:**

Add read-only properties to `PyTapDataUpdateCoordinator`:
```python
@property
def barcode_to_node(self) -> dict[str, int]:
    return dict(self._barcode_to_node)

@property
def infra_received(self) -> bool:
    return self._infra_received

@property
def pending_power_reports(self) -> int:
    return self._pending_power_reports
```

**Option 2 — Single `diagnostics_data()` method:**

Add a method that returns the full diagnostics dict, keeping the internal state encapsulated:
```python
def get_diagnostics_data(self) -> dict[str, Any]:
    return {
        "barcode_to_node": dict(self._barcode_to_node),
        "node_to_barcode": {str(k): v for k, v in self._node_to_barcode.items()},
        "infra_received": self._infra_received,
        "pending_power_reports": self._pending_power_reports,
        "energy_state": {
            barcode: {
                "daily_energy_wh": round(acc.daily_energy_wh, 2),
                "total_energy_wh": round(acc.total_energy_wh, 2),
                "daily_reset_date": acc.daily_reset_date,
                "last_reading_ts": acc.last_reading_ts.isoformat() if acc.last_reading_ts else None,
            }
            for barcode, acc in self._energy_state.items()
        },
    }
```

**Recommendation:** Option 2 — a single method is cleaner and doesn't expose multiple internal attributes.

### A.5 Manifest

No changes to `manifest.json` needed. The diagnostics platform is built into HA core and does not require a manifest declaration.

### A.6 No Changes to `__init__.py` PLATFORMS

The diagnostics platform does **not** need to be added to the `PLATFORMS` list. It is not a standard entity platform — HA discovers `diagnostics.py` automatically if the file exists and contains `async_get_config_entry_diagnostics`.

### A.7 Testing

Create `tests/test_diagnostics.py`:

- Set up a mock config entry and coordinator with known counters, node data, and barcode mappings.
- Call `async_get_config_entry_diagnostics(hass, entry)`.
- Assert the returned dict contains the expected keys and values.
- Assert `host` is redacted.
- Assert barcodes and gateway data are present (not redacted).

---

## Part B: Per-Sensor "Readings Received" Daily Counter

### B.1 Requirements Analysis

The requirement states: *"counters of 'readings received' should be on the sensor level as a daily meter."*

This means: for each configured optimizer (barcode), track how many `PowerReportEvent` readings were received today. This count resets at midnight. It helps identify modules that stop reporting (faulty optimizer, wireless interference, topology issues).

### B.2 Design Decisions

#### Decision 1: Where to Count

| Option | Description | Pros | Cons |
|--------|-------------|------|------|
| **A — Coordinator-side counter** | Increment a counter in `_handle_power_report` per barcode per day, store in `coordinator.data["nodes"][barcode]` | Consistent with existing data flow; single source of truth; persisted naturally | Counter lives in coordinator, same pattern as energy |
| **B — Sensor-side counter** | Sensor entity counts `_handle_coordinator_update` calls that have data | No coordinator changes | Loses count on HA restart; inconsistent with the push model |

**Recommendation:** Option A — coordinator-side. Matches the existing pattern for energy accumulation.

#### Decision 2: Persistence

Should the reading count survive HA restarts?

| Option | Pros | Cons |
|--------|------|------|
| **A — Persist via Store** | Count survives restart; consistent daily total | Adds to store payload; needs daily reset logic |
| **B — In-memory only** | Simpler; resets on restart | User loses count on restart; less useful for diagnosing intermittent issues |
| **C — Persist via RestoreSensor only** | Restores last known value from HA state | Good enough for display; no coordinator store changes |

**Recommendation:** Option A — persist via the existing coordinator Store. The per-barcode reading count data is small (one int per barcode). It piggybacks on the existing save/load cycle. Daily reset logic mirrors the energy daily reset pattern.

#### Decision 3: Sensor Entity Type

| Option | Description |
|--------|-------------|
| **A — New sensor per optimizer** | Add a new `SensorEntityDescription` entry `readings_today` with `SensorStateClass.TOTAL` and `last_reset` at midnight, unit = "readings" |
| **B — Extra state attribute** | Add `readings_today` as an `extra_state_attribute` on an existing sensor (e.g., power sensor) |

The requirement says "on the sensor level as a daily meter" — this implies a dedicated sensor entity, not a hidden attribute.

**Recommendation:** Option A — dedicated sensor entity. This makes it visible in history graphs, automations, and the diagnostics card.

#### Decision 4: Unit of Measurement and Device Class

- **Unit:** No standard HA unit for "readings" or "count". Use `None` (unitless) or a custom string like `"readings"`.
- **Device Class:** No matching device class. Leave as `None`.
- **State Class:** `SensorStateClass.TOTAL` with `last_reset` at midnight. This gives a staircase graph that resets daily.

| Option | Unit | Device Class | State Class |
|--------|------|-------------|-------------|
| **A — Unitless with TOTAL** | `None` | `None` | `TOTAL` + `last_reset` |
| **B — Custom unit string** | `"readings"` | `None` | `TOTAL` + `last_reset` |

**Recommendation:** Needs validation. HA's long-term statistics (LTS) requires `state_class` to be set, but non-standard units may cause warnings. Option A (unitless) is safer. Option B is more descriptive in the UI.

#### Decision 5: Entity Category

Since this is a diagnostic counter not a primary measurement, it should use `EntityCategory.DIAGNOSTIC`. This hides it from the default entity list and clearly marks it as a troubleshooting tool.

#### Decision 6: Aggregate Reading Counters

Should string/installation aggregates also have a "readings today" counter?

| Option | Pros | Cons |
|--------|------|------|
| **A — Per-optimizer only** | Simpler; the diagnostic value is per-module connectivity | No aggregate view |
| **B — Per-optimizer + aggregates** | Consistent with existing aggregate pattern | Aggregate reading count is less useful — what would it mean? Sum of all readings? |

**Recommendation:** Option A — per-optimizer only. The purpose is per-module connectivity diagnosis. An aggregate sum of readings is not meaningful for troubleshooting.

### B.3 Data Model Changes

#### Coordinator: per-barcode reading counter state

Add to the internal tracking (alongside `_energy_state`):

```python
self._reading_counts: dict[str, ReadingCounter] = {}

@dataclass
class ReadingCounter:
    count: int = 0
    reset_date: str = ""  # ISO date string
```

Or: extend `EnergyAccumulator` to include `readings_today: int`. This avoids a new dataclass.

| Option | Pros | Cons |
|--------|------|------|
| **A — Separate `ReadingCounter` dataclass** | Clean separation of concerns | New dataclass + new dict + new persistence key |
| **B — Add `readings_today` field to `EnergyAccumulator`** | Minimal change; reuses existing persistence and daily reset | Mixes diagnostic data with energy data |

**Recommendation:** Needs validation. Both are reasonable. Option B is simpler.

#### Coordinator data

Add to `self.data["nodes"][barcode]`:
```python
"readings_today": 42,
"readings_reset_date": "2026-02-23",
```

#### Persistence

Add to the store's `energy_data` dict per barcode (if Option B):
```python
"readings_today": 42,
```

The `daily_reset_date` is already there and shared with energy — the reading counter resets at the same time.

### B.4 Coordinator Changes

In `_handle_power_report`, after the existing energy accumulation block, increment the reading counter:

```python
# Increment per-module reading counter
if acc.daily_reset_date != today:
    # Already handled above by energy reset, but ensure count is zeroed
    acc.readings_today = 0
acc.readings_today += 1
```

Write `readings_today` into `self.data["nodes"][barcode]`.

### B.5 Sensor Changes

Add a new entry to `SENSOR_DESCRIPTIONS`:

```python
PyTapSensorEntityDescription(
    key="readings_today",
    translation_key="readings_today",
    native_unit_of_measurement=None,  # or "readings" — see Decision 4
    state_class=SensorStateClass.TOTAL,
    entity_category=EntityCategory.DIAGNOSTIC,
    suggested_display_precision=0,
    value_key="readings_today",
),
```

The `last_reset` property already handles the daily_energy sensor by checking `self.entity_description.key == "daily_energy"`. This needs to be extended to also return `last_reset` for `readings_today`:

```python
@property
def last_reset(self) -> datetime | None:
    if self.entity_description.key not in ("daily_energy", "readings_today"):
        return None
    # ... existing logic ...
```

### B.6 Translation Updates

Add to `strings.json` and `translations/en.json`:

```json
"readings_today": {
    "name": "Readings today"
}
```

### B.7 Entity Count Impact

Currently: 10 sensors per optimizer + aggregates.
After: **11 sensors per optimizer** + aggregates (unchanged).

### B.8 Migration

No config entry version bump needed — no config schema change. The new sensor is created deterministically from the existing module list. The new data fields (`readings_today`) are absent from the store on first load and default to 0.

### B.9 Testing

#### `test_sensor.py` additions:
- Verify the `readings_today` sensor entity is created for each configured module.
- Verify it has `entity_category` = `DIAGNOSTIC`.
- Verify it increments on each coordinator update with power data.
- Verify it resets to 0 on a new day.
- Verify `last_reset` returns midnight of the current day.

#### `test_coordinator_persistence.py` additions:
- Verify `readings_today` is persisted in the store.
- Verify it restores correctly on load.
- Verify it resets on date change during restore.

#### `test_energy.py` additions (if Option B — extending `EnergyAccumulator`):
- Verify reading count increments.
- Verify daily reset zeroes the count.

---

## Implementation Order

| Step | Description | Files Modified | Dependency |
|------|-------------|----------------|------------|
| 1 | Create `diagnostics.py` with `async_get_config_entry_diagnostics` | `diagnostics.py` (new) | None |
| 2 | Add `get_diagnostics_data()` method to coordinator | `coordinator.py` | Step 1 needs this |
| 3 | Write tests for diagnostics | `tests/test_diagnostics.py` (new) | Steps 1–2 |
| 4 | Add `readings_today` field to `EnergyAccumulator` | `energy.py` | None |
| 5 | Increment reading counter in `_handle_power_report` | `coordinator.py` | Step 4 |
| 6 | Extend persistence (save/load) for reading counter | `coordinator.py` | Step 4 |
| 7 | Add `readings_today` sensor description | `sensor.py` | Step 5 |
| 8 | Extend `last_reset` property for readings_today | `sensor.py` | Step 7 |
| 9 | Add translations | `strings.json`, `translations/en.json` | Step 7 |
| 10 | Write tests for readings_today sensor and persistence | `tests/test_sensor.py`, `tests/test_coordinator_persistence.py`, `tests/test_energy.py` | Steps 5–8 |

---

## Open Decisions Summary

| # | Decision | Options | Recommendation | Status |
|---|----------|---------|----------------|--------|
| 1 | Redact barcodes in diagnostics? | A: Redact / B: Keep | B — Keep | ✅ **Resolved: Keep** |
| 2 | Coordinator state exposure for diagnostics | A: Public properties / B: Single method | B — Single `get_diagnostics_data()` method | ✅ **Resolved: Single method** |
| 3 | Reading counter persistence | A: Store / B: Memory only / C: RestoreSensor | A — Persist via Store | ✅ **Resolved: Store** |
| 4 | Reading counter unit | A: Unitless (`None`) / B: `"readings"` | A — Unitless (`None`) | ✅ **Resolved: Unitless** |
| 5 | Reading counter data model | A: Separate `ReadingCounter` / B: Extend `EnergyAccumulator` | B — Extend `EnergyAccumulator` | ✅ **Resolved: Extend EnergyAccumulator** |
| 6 | Aggregate reading counters | A: Per-optimizer only / B: Include aggregates | A — Per-optimizer only | ✅ **Resolved: Per-optimizer only** |

---

### Decision 2: Coordinator State Exposure — Detailed Analysis

The diagnostics function in `diagnostics.py` needs to read several private attributes from the coordinator (`_barcode_to_node`, `_node_to_barcode`, `_infra_received`, `_pending_power_reports`, `_energy_state`). The question is how to expose these.

**Option A — Public properties on the coordinator**

Each piece of internal state gets its own `@property`:

```python
@property
def barcode_to_node(self) -> dict[str, int]: ...
@property
def infra_received(self) -> bool: ...
@property
def pending_power_reports(self) -> int: ...
@property
def energy_state_summary(self) -> dict[str, Any]: ...
```

| Aspect | Assessment |
|--------|------------|
| **Granularity** | Each property is independently accessible — useful if other parts of the codebase eventually need one specific field (e.g., a binary sensor checking `infra_received`) |
| **API surface** | Expands the coordinator's public API with 4+ new properties. Any future consumer can call them, which may make refactoring harder (more coupling points) |
| **Type safety** | Each property has its own return type — clear and IDE-friendly |
| **Testability** | Individual properties can be asserted on directly in unit tests |
| **Diagnostics coupling** | `diagnostics.py` still has to know which properties to call and how to assemble the dict — the assembly logic lives outside the coordinator |

**Option B — Single `get_diagnostics_data()` method**

One method returns a ready-to-use dict:

```python
def get_diagnostics_data(self) -> dict[str, Any]:
    return {
        "barcode_to_node": dict(self._barcode_to_node),
        "infra_received": self._infra_received,
        ...
    }
```

| Aspect | Assessment |
|--------|------------|
| **Encapsulation** | Internal state stays private. Only the coordinator decides what to expose and how to format it. `diagnostics.py` is a thin caller |
| **API surface** | One method only — minimal coupling. Other code paths don't accidentally depend on individual diagnostics properties |
| **Refactorability** | If internal state structure changes (e.g., `_barcode_to_node` is replaced by a different data structure), only `get_diagnostics_data()` needs updating — callers are unaffected |
| **Type safety** | Returns `dict[str, Any]` — less precise than typed properties. Consumers rely on key names |
| **Testability** | Tests assert on the returned dict. Slightly more brittle if key names change, but also tests the serialization format directly |
| **Reusability** | The returned dict is purpose-built for diagnostics. If another feature needs just `infra_received`, it can't reuse this method cleanly without pulling the whole dict |

**Recommendation: Option B** — The diagnostics platform is the only consumer of this internal state. One purpose-built method keeps encapsulation tight, minimises the public API footprint, and matches the HA pattern where diagnostics data is assembled by the component rather than the platform. If a future feature (e.g., binary sensors for Feature 6) needs `infra_received`, a targeted property can be added at that point. YAGNI applies — don't expand the API surface for consumers that don't exist yet.

---

### Decision 3: Reading Counter Persistence — Detailed Analysis

The question is whether the per-module daily reading count should survive an HA restart.

**Option A — Persist via the existing HA Store**

The reading count is saved alongside energy data in `_async_save_coordinator_state` and restored in `_async_load_coordinator_state`. It follows the same daily-reset logic as energy.

- **Accuracy:** The counter reflects the true count for the day, including readings received before a restart. If HA restarts at 2 PM, a module that received 200 readings in the morning starts at 200 after restart, not 0.
- **Complexity:** Minimal — adds one int field per barcode to the existing store payload. The save/load cycle is already debounced and running on every power report.
- **Failure mode:** If the store file is lost/corrupt, count starts at 0 — acceptable, same as energy.
- **Use case fit:** If the user checks at end of day "how many readings did Module X get?", they get the correct full-day answer.

**Option B — In-memory only (reset on restart)**

The counter is a simple int in the coordinator that starts at 0 on each startup.

- **Accuracy:** After restart, count shows only readings since boot. A module that got 200 readings before restart shows 0 until new readings arrive.
- **Complexity:** Simplest — no persistence code at all.
- **Failure mode:** Every restart loses the count. On a day with multiple restarts, the count is meaningless.
- **Use case fit:** Only useful for "are readings arriving right now?" — not for "how many total readings today?"

**Option C — Persist via RestoreSensor only**

The HA `RestoreSensor` mechanism saves the last known entity state and restores it after restart. The counter uses this instead of the coordinator store.

- **Accuracy:** Restores the last-known count from HA's state machine. However, this is the displayed value at the moment HA shut down — it may lag behind the coordinator's internal count by one update cycle.
- **Complexity:** Moderate — requires `RestoreSensor` subclass (already in use) and `async_added_to_hass` logic to restore. But the coordinator has no knowledge of the restored value, so it would need to either: (a) start its own counter from 0 and let the sensor add the restored offset (fragile), or (b) the sensor sends the restored value back to the coordinator (unusual and messy).
- **Failure mode:** RestoreSensor depends on HA's state file. If state is lost (e.g., database corruption), count starts at 0. Same as Option A, but with a more complex restore path.
- **Use case fit:** Good enough for display, but the coordinator and sensor would disagree on the count until they synchronize — a source of bugs.

**Recommendation: Option A** — It adds negligible complexity (one int per barcode in an already-running save cycle), gives the most accurate full-day count, and avoids the coordinator/sensor desync problem of Option C. Option B is only appropriate if the counter is purely a "live session" indicator, which contradicts the "daily meter" requirement.

---

### Decision 4: Reading Counter Unit — Detailed Analysis

The sensor needs a `native_unit_of_measurement`. HA uses this for display, long-term statistics (LTS), and unit conversion.

**Option A — Unitless (`None`)**

```python
native_unit_of_measurement=None,
```

- **Display:** HA shows the raw number with no unit suffix. The entity card displays "42" rather than "42 readings".
- **LTS compatibility:** HA records long-term statistics for entities with `state_class` set, even without a unit. No warnings.
- **Unit conversion:** Not applicable — HA won't try to convert a unitless value.
- **UI conventions:** Some built-in HA counters (e.g., `counter` integration) are unitless. This is an established pattern.
- **Graphing:** History graphs show the value with no Y-axis label. Less informative at a glance.

**Option B — Custom unit string (`"readings"`)**

```python
native_unit_of_measurement="readings",
```

- **Display:** Entity card shows "42 readings". More descriptive.
- **LTS compatibility:** HA records LTS for non-standard units. Prior to HA 2023.x there were warnings for unknown units; current versions handle custom strings gracefully. No known issues in 2026.x.
- **Unit conversion:** HA won't recognize "readings" as a convertible unit, so no conversion is offered. This is fine.
- **UI conventions:** Some community integrations use custom unit strings (e.g., "messages", "packets"). It's not uncommon but not an HA core pattern.
- **Graphing:** Y-axis label shows "readings" — clearer for the user.

**Recommendation: Option A (unitless)** — Safer against any future HA unit validation changes, consistent with HA's own `counter` integration, and the entity name "Readings today" already communicates what the number means. The unit suffix adds minimal information. However, Option B is perfectly functional in current HA — choose based on preference.

---

### Decision 5: Reading Counter Data Model — Detailed Analysis

The reading count needs to live somewhere in the coordinator's internal state. Two approaches:

**Option A — Separate `ReadingCounter` dataclass**

```python
@dataclass
class ReadingCounter:
    count: int = 0
    reset_date: str = ""

# In coordinator:
self._reading_counts: dict[str, ReadingCounter] = {}
```

New persistence key in the store:
```python
"reading_counts": {
    "A-1234567B": {"count": 42, "reset_date": "2026-02-23"},
    ...
}
```

| Aspect | Assessment |
|--------|------------|
| **Separation of concerns** | Reading counts are conceptually distinct from energy accumulation. A dedicated dataclass makes this explicit. Each dataclass has a single responsibility. |
| **Persistence** | Requires a new top-level key in the store dict (`reading_counts`), new save logic, new load logic. Roughly 15–20 lines of new code in the save/load methods. |
| **Daily reset** | Needs its own date comparison. Currently `EnergyAccumulator` already does this — a second dataclass means a second reset check in `_handle_power_report`. Could be factored into a shared helper, but that's extra work. |
| **Testing** | Clean: test `ReadingCounter` in isolation, test energy in isolation. |
| **Growth** | If more per-barcode diagnostic counters are added later (e.g., "topology events received", "CRC errors per module"), they'd naturally fit in `ReadingCounter`. |

**Option B — Extend `EnergyAccumulator`**

```python
@dataclass
class EnergyAccumulator:
    daily_energy_wh: float = 0.0
    total_energy_wh: float = 0.0
    daily_reset_date: str = ""
    last_power_w: float = 0.0
    last_reading_ts: datetime | None = None
    readings_today: int = 0  # ← new field
```

| Aspect | Assessment |
|--------|------------|
| **Simplicity** | One field added to an existing dataclass. No new dict, no new persistence key. `readings_today` is serialized alongside energy data in the existing `energy_data` store key. Daily reset already happens — just add `acc.readings_today = 0` next to `acc.daily_energy_wh = 0.0`. |
| **Coupling** | Mixes diagnostic data (reading count) with energy calculation data. `energy.py` is described as "Energy accumulation helpers" — a reading counter isn't energy. |
| **Persistence** | Zero new persistence code — the field serializes/deserializes with the existing `energy_data` dict. Just add `"readings_today": acc.readings_today` in save and read it back in load. |
| **Testing** | `test_energy.py` now tests something that isn't energy. Slightly muddled test scope. |
| **Growth** | If more diagnostic counters are needed, `EnergyAccumulator` becomes an increasingly misleading name. |

**Recommendation: Option B** — The practical savings are significant: no new dataclass, no new dict, no new persistence key, no new daily-reset logic. The `EnergyAccumulator` is already a per-barcode-per-day state bag updated on every power report. Adding one int field is the minimal change. The naming impurity (`EnergyAccumulator` holding a reading count) is a minor concern — if more diagnostic counters arise in the future, a refactor to separate them would be warranted at that point. For a single counter, the pragmatic choice is to extend what exists.

If cleaner naming is preferred, the dataclass could be renamed to `ModuleAccumulator` — but that's a broader refactor touching `energy.py`, `coordinator.py`, and all tests. Not necessary for this feature.

---

## Files Changed / Created Summary

| File | Action | Purpose |
|------|--------|---------|
| `custom_components/pytap/diagnostics.py` | **Create** | Diagnostics platform entry point |
| `custom_components/pytap/coordinator.py` | **Modify** | Add `get_diagnostics_data()`, reading counter logic, persist reading counter |
| `custom_components/pytap/energy.py` | **Modify** | Add `readings_today` field to `EnergyAccumulator` |
| `custom_components/pytap/sensor.py` | **Modify** | Add `readings_today` sensor description, extend `last_reset` |
| `custom_components/pytap/const.py` | No change expected | |
| `custom_components/pytap/strings.json` | **Modify** | Add `readings_today` translation key |
| `custom_components/pytap/translations/en.json` | **Modify** | Add `readings_today` translation |
| `tests/test_diagnostics.py` | **Create** | Tests for diagnostics download |
| `tests/test_sensor.py` | **Modify** | Tests for readings_today sensor |
| `tests/test_coordinator_persistence.py` | **Modify** | Tests for reading counter persistence |
| `tests/test_energy.py` | **Modify** | Tests for reading counter in accumulator |
