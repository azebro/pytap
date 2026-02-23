# Feature 1: Per Sensor Cumulatives — Implementation Plan

## Overview

Add two new energy sensors per optimizer module:

| Sensor | Unit | Device Class | State Class | Reset Behaviour |
|---|---|---|---|---|
| **Daily Energy** | Wh | `ENERGY` | `TOTAL` | Resets to 0 at midnight (`last_reset` attribute) |
| **Total Energy** | Wh | `ENERGY` | `TOTAL_INCREASING` | Monotonically increasing, never resets |

Both sensors use **Wh** as the native unit. HA automatically offers unit conversion (Wh → kWh → MWh) in the UI when `SensorDeviceClass.ENERGY` is set, so the user picks their preferred display unit. No internal Wh↔kWh conversion needed.

Both use **trapezoidal integration** of the existing `power` (W) readings to compute energy.
Entity count per module rises from **8 → 10**.

---

## 1. Trapezoidal Energy Calculation

### Formula

On each new power reading for a barcode:

```
energy_increment_wh = (prev_power_w + curr_power_w) / 2 × (delta_seconds / 3600)
```

- `prev_power_w` / `curr_power_w`: the power values at consecutive readings
- `delta_seconds`: elapsed seconds between the two readings

This is the same method HA's built-in `integration` platform uses (`METHOD_TRAPEZOIDAL`). We implement it directly because the integration helper is a user-configured platform, not a reusable library.

### Daily Reset (No Midnight Splitting Needed)

PV panels produce no power after dark. The last reading of the day (sunset) and the first reading of the next day (sunrise) will both be near 0 W. Therefore **midnight boundary splitting is unnecessary** — any trapezoid spanning midnight would yield ≈ 0 Wh.

Daily reset is a simple date comparison:
1. On each reading, compare `acc.daily_reset_date` to today's date
2. If different → set `acc.daily_energy_wh = 0`, update `acc.daily_reset_date`
3. Then proceed with normal trapezoid accumulation

The first reading of the new day (sunrise, near 0 W) establishes the baseline. No energy is lost.

### Gap Handling — Solar-Specific Considerations

Gaps between readings are **normal and expected** in a solar context:

| Gap Type | Duration | Power at Boundaries | Impact |
|---|---|---|---|
| **Overnight** (sunset → sunrise) | 8–16 h | Both ≈ 0 W | Trapezoid ≈ 0 Wh — harmless but pointless to compute |
| **Morning ramp-up** | Minutes | Low, rising | Small readings, trapezoid is fine |
| **Cloud transient** | Seconds–minutes | Varies | Normal interval, trapezoid valid |
| **Connection loss mid-day** | Minutes–hours | Could be high | Trapezoid unreliable — production during gap is unknown |
| **Inverter restart** | 1–5 min | Drops to 0, then resumes | Short gap, trapezoid underestimates slightly |

**Strategy**: Use a gap threshold tuned to the expected reporting interval. The Tigo gateway typically reports every ~30 s. A gap beyond a reasonable multiple of that (e.g. 120 s) during active production likely means data loss — the trapezoid would be inaccurate.

However, when **both** the previous and current power readings are near zero (< 1 W), a large gap is benign (overnight, pre-sunrise) and the trapezoid can safely be skipped without logging a warning — it would be ≈ 0 Wh anyway.

**Rules**:
1. If `delta > ENERGY_GAP_THRESHOLD_SECONDS` **and** either reading > 1 W → **discard** trapezoid, log at DEBUG ("gap during production"), store new baseline
2. If `delta > ENERGY_GAP_THRESHOLD_SECONDS` **and** both readings ≤ 1 W → **skip** silently (overnight/no-sun gap), store new baseline
3. If `delta ≤ ENERGY_GAP_THRESHOLD_SECONDS` → **compute** trapezoid normally

### Edge Cases

| Case | Handling |
|---|---|
| **First reading** (no previous) | Store power + timestamp, emit 0 energy — no trapezoid possible yet |
| **Gap during active production** (> threshold, power > 1 W) | Discard trapezoid — energy during gap is unknown. Store new baseline. Log at DEBUG. |
| **Overnight / no-sun gap** (> threshold, both powers ≤ 1 W) | Skip silently. No energy lost. Store new baseline. |
| **Power is 0 or negative** | Clamp to 0 before integration — optimizer can't produce negative solar energy |
| **HA restart mid-day** | Restore accumulated energy from persisted store (see §3). Next reading becomes fresh baseline (no trapezoid for the first pair after restart). |
| **Date rollover** | First reading of new day triggers daily reset to 0. No midnight interpolation — panels are dark. |

---

## 2. Data Model Changes

### Coordinator: per-barcode energy accumulation state

Add to `coordinator.data["nodes"][barcode]`:

```python
{
    # ... existing fields (power, voltage_in, etc.) ...
    "daily_energy_wh": 1234.56,        # accumulated today
    "daily_reset_date": "2026-02-22",   # ISO date of last reset
    "total_energy_wh": 98765432.1,      # lifetime total in Wh
}
```

Internal tracking state (not in `coordinator.data`, in a separate dict):

```python
self._energy_state: dict[str, EnergyAccumulator] = {}

@dataclass
class EnergyAccumulator:
    daily_energy_wh: float        # running daily sum
    total_energy_wh: float        # running lifetime sum
    daily_reset_date: str         # ISO date string "YYYY-MM-DD"
    last_power_w: float           # power from previous reading
    last_reading_ts: datetime     # timestamp of previous reading
```

This state is persisted via the existing `_async_save_coordinator_state` / `_async_load_coordinator_state` mechanism.

---

## 3. Persistence

### Extend the existing HA Store schema

Currently saved fields:
- `barcode_to_node`
- `discovered_barcodes`
- `parser_state`

Add:
```python
"energy_data": {
    "A-1234567B": {
        "daily_energy_wh": 1234.56,
        "daily_reset_date": "2026-02-22",
        "total_energy_wh": 98765432.1,
        "last_power_w": 250.3,
        "last_reading_ts": "2026-02-22T14:30:00",
    },
    ...
}
```

### Store version

Keep `STORE_VERSION = 2` — the load function already handles missing keys gracefully (`.get()` with defaults). No migration needed; absent `energy_data` simply means "start fresh".

### Save triggers

Energy data changes on every power report. The existing `_schedule_save()` debounce (10 s delay) is already adequate — energy data piggybacks on the same save cycle. No additional save triggers needed.

### Load on startup

In `_async_load_coordinator_state`, restore `energy_data` into `self._energy_state`. Validate:
- If `daily_reset_date` ≠ today → reset `daily_energy_wh` to 0, update date
- `total_energy_wh` always restored as-is
- `last_power_w` / `last_reading_ts` are restored to allow trapezoid continuity after restart (though first post-restart reading will likely exceed the long-gap threshold and start a fresh baseline anyway)

---

## 4. Coordinator Changes (`coordinator.py`)

### 4.1 New module: `energy.py`

Create `custom_components/pytap/energy.py` containing:

```python
@dataclass
class EnergyAccumulator:
    daily_energy_wh: float = 0.0
    total_energy_wh: float = 0.0
    daily_reset_date: str = ""
    last_power_w: float = 0.0
    last_reading_ts: datetime | None = None


def accumulate_energy(
    acc: EnergyAccumulator,
    power: float,
    now: datetime,
    gap_threshold: int = ENERGY_GAP_THRESHOLD_SECONDS,
    low_power_threshold: float = ENERGY_LOW_POWER_THRESHOLD_W,
) -> float:
    """Integrate a power reading into the accumulator.

    Returns the energy increment in Wh (0.0 if skipped/discarded).
    Pure function on the accumulator — no HA dependencies.
    Mutates `acc` in place.
    """
```

This keeps all math and state-machine logic HA-free and directly unit-testable.

### 4.2 `__init__` additions

```python
self._energy_state: dict[str, EnergyAccumulator] = {}
```

### 4.3 New constants

Add to `const.py`:
```python
ENERGY_GAP_THRESHOLD_SECONDS = 120  # 2 minutes — discard trapezoid if gap exceeds this
ENERGY_LOW_POWER_THRESHOLD_W = 1.0  # below this, power is considered "no sun" for gap logic
```

120 s is ~4× the typical Tigo reporting interval (~30 s). Long enough to tolerate a couple of missed reports, short enough that a mid-day disconnect doesn't produce a wildly inaccurate trapezoid.

### 4.4 Energy accumulation in `_handle_power_report`

After the existing node data update block (`self.data["nodes"][barcode] = {...}`), add:

```python
from .energy import accumulate_energy

acc = self._energy_state.setdefault(barcode, EnergyAccumulator())
accumulate_energy(acc, power=event.power, now=dt_util.now())
```

Then write `acc.daily_energy_wh`, `acc.total_energy_wh`, `acc.daily_reset_date` into `self.data["nodes"][barcode]`.

1. Get or create `EnergyAccumulator` for barcode
2. Clamp power to `max(power, 0.0)`
3. `now = dt_util.now()` (timezone-aware via HA utility)
4. `today = now.date().isoformat()`
5. If `acc.daily_reset_date != today`:
   - Reset `acc.daily_energy_wh = 0.0`
   - Set `acc.daily_reset_date = today`
   - (No midnight splitting needed — panels are dark at midnight)
6. If `acc.last_reading_ts` is not None:
   - `delta = (now - acc.last_reading_ts).total_seconds()`
   - If `delta > ENERGY_GAP_THRESHOLD_SECONDS`:
     - If `acc.last_power_w > ENERGY_LOW_POWER_THRESHOLD_W` or `power > ENERGY_LOW_POWER_THRESHOLD_W`:
       - Log at DEBUG: gap during production, trapezoid discarded
     - Skip trapezoid, just update baseline
   - Else: `increment_wh = (acc.last_power_w + power) / 2 * (delta / 3600)`
     - `acc.daily_energy_wh += increment_wh`
     - `acc.total_energy_wh += increment_wh`
7. Update baseline: `acc.last_power_w = power`, `acc.last_reading_ts = now`
8. Write into `self.data["nodes"][barcode]`:
   - `daily_energy_wh = round(acc.daily_energy_wh, 2)`
   - `total_energy_wh = round(acc.total_energy_wh, 2)`
   - `daily_reset_date = acc.daily_reset_date`
9. `self._schedule_save()` (already called by existing logic path)

### 4.5 Extend persistence (load/save)

**`_async_load_coordinator_state`**: restore `energy_data` from store into `self._energy_state`

**`_async_save_coordinator_state`**: serialize `self._energy_state` into the store dict under `energy_data`

### 4.6 `reload_modules` update

When pre-populating node data for newly added barcodes, include `daily_energy_wh: 0.0`, `total_energy_wh: 0.0`, `daily_reset_date: today`.

---

## 5. Sensor Changes (`sensor.py`)

### 5.1 New sensor descriptions

Add two entries to `SENSOR_DESCRIPTIONS`:

```python
PyTapSensorEntityDescription(
    key="daily_energy",
    translation_key="daily_energy",
    native_unit_of_measurement=UnitOfEnergy.WATT_HOUR,
    device_class=SensorDeviceClass.ENERGY,
    state_class=SensorStateClass.TOTAL,
    suggested_display_precision=1,
    value_key="daily_energy_wh",
),
PyTapSensorEntityDescription(
    key="total_energy",
    translation_key="total_energy",
    native_unit_of_measurement=UnitOfEnergy.WATT_HOUR,
    device_class=SensorDeviceClass.ENERGY,
    state_class=SensorStateClass.TOTAL_INCREASING,
    suggested_display_precision=0,
    value_key="total_energy_wh",
),
```

Both sensors use Wh natively. No `value_scale` or conversion logic needed — HA's unit system handles display conversion to kWh/MWh automatically.

### 5.2 `last_reset` attribute for daily_energy

The `daily_energy` sensor needs `last_reset` as an attribute (required by `SensorStateClass.TOTAL`).

Option: Override `last_reset` property in `PyTapSensor` to read from coordinator data for the daily_energy sensor:

```python
@property
def last_reset(self) -> datetime | None:
    if self.entity_description.key != "daily_energy":
        return None
    node_data = self.coordinator.data.get("nodes", {}).get(self._barcode)
    if not node_data:
        return None
    reset_date = node_data.get("daily_reset_date")
    if reset_date:
        return datetime.fromisoformat(reset_date + "T00:00:00")
    return None
```

Alternatively, add a `has_last_reset: bool = False` flag to the description and only compute `last_reset` when the flag is set.

### 5.3 Import additions

```python
from homeassistant.const import UnitOfEnergy
```

---

## 6. Translation / String Updates

### `strings.json` and `translations/en.json`

Add under `entity.sensor`:

```json
"daily_energy": {
    "name": "Daily energy"
},
"total_energy": {
    "name": "Total energy"
}
```

---

## 7. File Change Summary

| File | Change |
|---|---|
| `const.py` | Add `ENERGY_GAP_THRESHOLD_SECONDS`, `ENERGY_LOW_POWER_THRESHOLD_W` |
| `energy.py` *(new)* | `EnergyAccumulator` dataclass, `accumulate_energy()` pure function |
| `coordinator.py` | `_energy_state` dict, call `accumulate_energy()`, extend persistence load/save, extend `reload_modules` |
| `sensor.py` | Add `daily_energy` and `total_energy` descriptions, add `last_reset` property, add `UnitOfEnergy` import |
| `strings.json` | Add `daily_energy` and `total_energy` translation keys |
| `translations/en.json` | Add `daily_energy` and `total_energy` names |
| `tests/test_sensor.py` | Update entity count assertions (16 → 20), add tests for new sensor descriptions, test `last_reset`, test kWh conversion |
| `tests/test_coordinator_persistence.py` | Add tests for energy data persistence (save/load), daily reset on load |
| `tests/test_energy.py` *(new)* | Dedicated tests for trapezoidal integration, solar gap handling, daily reset, edge cases |

---

## 8. Test Plan

### 8.1 Unit tests: Trapezoidal integration (`tests/test_energy.py`)

| Test | Description |
|---|---|
| `test_trapezoid_basic` | Two readings 60 s apart, 100 W each → 100 × 60/3600 = 1.667 Wh |
| `test_trapezoid_varying_power` | 100 W then 200 W, 120 s apart → (100+200)/2 × 120/3600 = 5.0 Wh |
| `test_trapezoid_zero_power` | 0 W readings → 0 Wh |
| `test_negative_power_clamped` | Negative power clamped to 0 before integration |
| `test_first_reading_no_energy` | First reading stores baseline, no energy added |
| `test_gap_during_production` | Gap > 120 s with power > 1 W → trapezoid discarded, baseline updated, DEBUG log emitted |
| `test_overnight_gap_silent` | Gap > 120 s with both powers ≤ 1 W → trapezoid skipped silently, no log |
| `test_daily_reset_on_new_date` | First reading of new day resets `daily_energy_wh` to 0, `total_energy_wh` unchanged |
| `test_total_never_resets` | `total_energy_wh` always grows, even across day boundary |
| `test_multi_day_gap_resets_daily` | Gap spanning >24 h (e.g. system offline): daily resets to 0, trapezoid discarded |
| `test_normal_reporting_interval` | Readings at ~30 s intervals accumulate correctly over a simulated hour |

### 8.2 Coordinator persistence tests (`tests/test_coordinator_persistence.py`)

| Test | Description |
|---|---|
| `test_save_includes_energy_data` | `_async_save_coordinator_state` includes energy_data in store |
| `test_load_restores_energy_data` | `_async_load_coordinator_state` populates `_energy_state` from store |
| `test_load_resets_daily_if_new_day` | Energy data from yesterday: daily resets to 0 on load, total preserved |
| `test_load_preserves_daily_if_same_day` | Energy data from today: daily and total both preserved |
| `test_load_handles_missing_energy_data` | No `energy_data` in store: starts with empty accumulators |

### 8.3 Sensor tests (`tests/test_sensor.py`)

| Test | Description |
|---|---|
| `test_entity_count_with_energy` | 2 modules × 10 sensors = 20 entities |
| `test_daily_energy_sensor_attributes` | Correct device_class, state_class, unit, `last_reset` present |
| `test_total_energy_sensor_attributes` | Correct device_class, state_class, unit |
| `test_total_energy_native_unit_wh` | Total energy sensor reports Wh as native unit |
| `test_daily_energy_last_reset_value` | `last_reset` returns midnight of `daily_reset_date` |
| `test_energy_sensors_unavailable_without_data` | Energy sensors unavailable when no node data exists |

---

## 9. Implementation Order

1. **`const.py`** — add `ENERGY_GAP_THRESHOLD_SECONDS`, `ENERGY_LOW_POWER_THRESHOLD_W`
2. **`energy.py`** *(new)* — `EnergyAccumulator` dataclass, `accumulate_energy()` pure function
3. **`coordinator.py`** — `_energy_state`, call into `energy.py`, persistence extension, `reload_modules` update
4. **`sensor.py`** — new descriptions, `last_reset` property, import
5. **`strings.json`** + **`translations/en.json`** — translation keys
6. **`tests/test_energy.py`** — trapezoidal integration tests (pure function, no HA mocking needed)
7. **`tests/test_coordinator_persistence.py`** — energy persistence tests
8. **`tests/test_sensor.py`** — updated entity count + energy sensor attribute tests
9. Lint and run full test suite

---

## 10. Energy Dashboard Compatibility

This implementation directly enables **Future Consideration #4** (Energy Dashboard Integration):

- `daily_energy` with `SensorStateClass.TOTAL` + `last_reset` ✓
- `total_energy` with `SensorStateClass.TOTAL_INCREASING` ✓
- `SensorDeviceClass.ENERGY` ✓

HA's energy dashboard can consume these sensors natively — no Riemann sum helper needed from the user side.

---

## 11. Open Questions — Resolved

1. **Gap threshold value**: Hardcoded at 120 s for now. Added to `future_considerations.md` §8 as a future options-flow item.
2. **Timezone**: Use `homeassistant.util.dt.now()` for timezone-aware timestamps. Daily reset "midnight" uses the HA instance's configured timezone.
3. **Precision**: Store Wh internally with full float precision, round only at display.
4. **Separate file for energy logic**: Yes — create `energy.py` module for `EnergyAccumulator` dataclass and `accumulate_energy()` pure function. Coordinator calls into it. This gives clean unit-testability without mocking coordinator internals.
