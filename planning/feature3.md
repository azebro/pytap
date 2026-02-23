# Feature 3: Performance — Implementation Plan

## Overview

Add a **performance percentage** sensor to each optimizer, string aggregate, and installation aggregate. Performance is defined as:

```
performance (%) = (current_power / peak_panel_power) × 100
```

This requires two changes:

1. **Configuration** — Extend the module configuration to capture **peak panel power** (Wp) per optimizer.
2. **Sensor entities** — Add a `performance` sensor at three levels: per-optimizer, per-string, and per-installation.

Default peak power: **455 W** (used for migrations and when the user does not provide a value).

---

## Resolved Decisions

The following design choices were evaluated and resolved before implementation.

### Decision 1: Peak Power Scope

Where should peak power be configurable?

| Option | Description | Pros | Cons |
|--------|-------------|------|------|
| **A — Per-module only** | Each module has its own `peak_power` field in config | Maximum flexibility; handles mixed panel types on one string | More fields for user to fill when all panels are identical |
| **B — Per-string with per-module override** | Default peak power set at string level, individual modules can override | Reduces repetition for uniform strings; still handles mixed panels | Adds complexity to config flow and migration; string is not currently a first-class config object |
| **C — Global default with per-module override** | Single integration-wide default peak power (e.g. in options), per-module override | Simplest UX for uniform installations | Cannot handle per-string differences without per-module overrides |

**Resolved:** **Option A** — per-module only. It aligns with the existing config model (each module is already a dict with `string`, `name`, `barcode`). Adding a `peak_power` field to the same dict is the smallest change. The config flow can pre-fill a default (455 W) so the user just confirms or edits. Strings are not first-class config objects today, so option B would require new infrastructure.

### Decision 2: Aggregate Performance Calculation

How should string and installation performance be computed?

| Option | Description | Formula | Example (2 panels: 300W/400Wp + 200W/500Wp) |
|--------|-------------|---------|----------------------------------------------|
| **A — Capacity-weighted** | Ratio of actual total power to total installed capacity | `sum(power) / sum(peak_power) × 100` | (300+200) / (400+500) × 100 = **55.6%** |
| **B — Simple average** | Mean of individual performance percentages | `mean(power_i / peak_power_i × 100)` | (75% + 40%) / 2 = **57.5%** |

**Resolved:** **Option A** — capacity-weighted. It answers the question "what fraction of my total installed capacity is currently producing?" which is the most operationally useful metric. A single underperforming small panel won't skew the aggregate the way a simple average would. It's also consistent with how PV monitoring systems (SolarEdge, Enphase) typically report system performance.

### Decision 3: Performance Clamping

Should performance be clamped to a 0–100% range?

| Option | Description | Rationale |
|--------|-------------|-----------|
| **A — No clamp** | Allow values > 100% | Real-world irradiance spikes, panel tolerances, and cold temperatures can cause momentary output above STC rating. Showing >100% is physically accurate. |
| **B — Clamp to 0–100%** | Cap at 100% | Looks cleaner in dashboards; avoids confusion for non-technical users. |
| **C — Clamp floor only** | `max(0, value)` — no negative, but allow >100% | Power is already clamped to ≥0 in energy.py. Negative performance is impossible. Values >100% are informative. |

**Resolved:** **Option C** — clamp floor only. Negative values are already impossible (power is clamped to ≥0). Values above 100% are physically meaningful and shouldn't be hidden. Users who want a capped view can use HA template sensors.

### Decision 4: Config Flow UX for Peak Power

How should peak power appear in the add-module form?

| Option | Description |
|--------|-------------|
| **A — Optional with default** | `vol.Optional(CONF_MODULE_PEAK_POWER, default=DEFAULT_PEAK_POWER)` — field is pre-filled with 455, user can change or leave it |
| **B — Required** | `vol.Required(CONF_MODULE_PEAK_POWER)` — user must explicitly enter a value (no pre-fill) |

**Resolved:** **Option A** — optional with default. Most users have uniform panels and just want to confirm or skip. Requiring an explicit value adds friction for no benefit since we have a sensible default.

### Decision 5: Performance When Power Is `None` (No Data Yet)

What should the performance sensor show when the optimizer hasn't reported yet?

| Option | Description |
|--------|-------------|
| **A — Unavailable** | Performance sensor mirrors availability of the power sensor. If power is `None`, performance is unavailable. |
| **B — Show 0%** | Treat no data as 0% performance. |

**Resolved:** **Option A** — unavailable. This is consistent with how all other PyTap sensors behave (unavailable until first `PowerReportEvent`). Showing 0% would be misleading — the panel might be producing; we just don't know yet.

### Decision 6: Aggregate Performance Availability

When should the aggregate performance sensor be available?

| Option | Description |
|--------|-------------|
| **A — ≥1 constituent reporting** | Available as soon as any constituent optimizer has data. Performance is computed from reporting members only. |
| **B — All constituents reporting** | Available only when every optimizer in the string/installation has data. |

**Resolved:** **Option A** — matches the existing aggregate sensor pattern (`PyTapAggregateSensor.available` returns `True` when any constituent has data). The denominator (total peak power) includes **only reporting members**, not all configured members. Otherwise, a string with 10×455Wp panels but only 1 reporting would show ~2% performance instead of its true ~65%.

---

## 1. Data Model Changes

### 1.1 New Constant

Add to `const.py`:

```python
CONF_MODULE_PEAK_POWER = "peak_power"
DEFAULT_PEAK_POWER = 455  # Wp (watts peak) — STC rating
```

### 1.2 Module Config Extension

Each module dict in `entry.data[CONF_MODULES]` gains a `peak_power` field:

```python
{
    "string": "A",
    "name": "Panel_01",
    "barcode": "A-1234567B",
    "peak_power": 455,       # ← new field
}
```

### 1.3 Coordinator Data Extension

`coordinator.data["nodes"][barcode]` gains a `performance` field:

```python
{
    # ... existing fields ...
    "peak_power": 455,                        # from config (static)
    "performance": 65.8,                      # (power / peak_power) × 100
}
```

Performance is computed in the coordinator at the point of the power report, alongside the existing energy accumulation.

---

## 2. Config Entry Migration (v3 → v4)

### 2.1 Version Bump

In `__init__.py`:
```python
CONFIG_ENTRY_VERSION = 4
```

### 2.2 Migration Logic

Add v3 → v4 handling in `async_migrate_entry`:

```python
if entry.version == 3:
    _LOGGER.info(
        "Migrating PyTap config entry %s from version 3 to 4",
        entry.entry_id,
    )
    modules = list(entry.data.get(CONF_MODULES, []))
    updated_modules: list[dict[str, Any]] = []
    for module in modules:
        normalized = dict(module)
        if CONF_MODULE_PEAK_POWER not in normalized:
            normalized[CONF_MODULE_PEAK_POWER] = DEFAULT_PEAK_POWER
        updated_modules.append(normalized)
    new_data = {**entry.data, CONF_MODULES: updated_modules}
    hass.config_entries.async_update_entry(entry, data=new_data, version=4)
```

This ensures all existing modules receive the default 455 Wp value. Users can subsequently adjust individual panels via the options flow.

### 2.3 Config Flow Version

Update `PyTapConfigFlow.VERSION` from `2` → `3` (to match the schema that now includes `peak_power`).

> **Note:** The config flow `VERSION` is currently `2` while `CONFIG_ENTRY_VERSION` is `3` — this existing mismatch should be investigated. For this feature, both should be aligned at `4`.

---

## 3. Config Flow Changes

### 3.1 Add Module Schema

Update `ADD_MODULE_SCHEMA` to include `peak_power`:

```python
ADD_MODULE_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_MODULE_STRING): str,
        vol.Required(CONF_MODULE_NAME): str,
        vol.Required(CONF_MODULE_BARCODE): str,
        vol.Optional(CONF_MODULE_PEAK_POWER, default=DEFAULT_PEAK_POWER): vol.All(
            vol.Coerce(int), vol.Range(min=1, max=1000)
        ),
    }
)
```

The `vol.Range(min=1, max=1000)` prevents nonsensical values (0 would cause division by zero; values above 1 kWp per panel are unrealistic for residential optimizers).

### 3.2 Validation

No special validation beyond the range check — `vol.All(vol.Coerce(int), vol.Range(...))` handles type coercion and bounds.

If peak_power is 0 or negative (shouldn't pass schema validation, but defensively): treat as `DEFAULT_PEAK_POWER` in the coordinator to avoid division by zero.

### 3.3 Both Flows

The `peak_power` field must be added to:
- `PyTapConfigFlow.async_step_add_module` (initial setup)
- `PyTapOptionsFlow.async_step_add_module` (post-setup edits)

Both use the shared `ADD_MODULE_SCHEMA`, so the schema change covers both.

### 3.4 Modules Description Update

Update `_modules_description()` to show peak power:

```python
lines.append(f"  {i}. {' / '.join(parts)} ({m.get(CONF_MODULE_PEAK_POWER, DEFAULT_PEAK_POWER)}Wp)")
```

---

## 4. Coordinator Changes

### 4.1 Module Lookup Extension

The `_module_lookup` dict already maps barcode → module dict. Since `peak_power` is now in the module dict, no new lookup structure is needed.

### 4.2 Performance Calculation in `_handle_power_report`

After the existing energy accumulation block, compute performance:

```python
peak_power = module_meta.get(CONF_MODULE_PEAK_POWER, DEFAULT_PEAK_POWER)
if peak_power and peak_power > 0 and event.power is not None:
    performance = (max(event.power, 0.0) / peak_power) * 100.0
else:
    performance = None
```

Add to the node data dict:

```python
self.data["nodes"][barcode] = {
    # ... existing fields ...
    "peak_power": peak_power,
    "performance": round(performance, 2) if performance is not None else None,
}
```

### 4.3 `reload_modules` Update

When pre-populating node data for newly added barcodes, include:

```python
"peak_power": module_meta.get(CONF_MODULE_PEAK_POWER, DEFAULT_PEAK_POWER),
"performance": None,
```

---

## 5. Sensor Changes

### 5.1 Per-Optimizer Performance Sensor

Add a new entry to `SENSOR_DESCRIPTIONS`:

```python
PyTapSensorEntityDescription(
    key="performance",
    translation_key="performance",
    native_unit_of_measurement="%",
    state_class=SensorStateClass.MEASUREMENT,
    suggested_display_precision=1,
    value_key="performance",
),
```

No `device_class` — HA does not have a `SensorDeviceClass.PERFORMANCE` or similar. Using `None` (no device class) with `%` unit is the standard pattern for percentage-based custom sensors.

Entity count per module rises from **10 → 11**.

### 5.2 Per-String Performance Sensor

Add to `STRING_SENSOR_DESCRIPTIONS`:

```python
PyTapAggregateSensorDescription(
    key="performance",
    translation_key="string_performance",
    native_unit_of_measurement="%",
    state_class=SensorStateClass.MEASUREMENT,
    suggested_display_precision=1,
    value_key="performance",  # handled specially — see §5.4
),
```

### 5.3 Per-Installation Performance Sensor

Add to `INSTALLATION_SENSOR_DESCRIPTIONS`:

```python
PyTapAggregateSensorDescription(
    key="performance",
    translation_key="installation_performance",
    native_unit_of_measurement="%",
    state_class=SensorStateClass.MEASUREMENT,
    suggested_display_precision=1,
    value_key="performance",  # handled specially — see §5.4
),
```

### 5.4 Aggregate Performance Computation

The existing `PyTapAggregateSensor._handle_coordinator_update` sums `value_key` across constituents. This works for power and energy but **not for performance**: you can't sum percentages. Aggregate performance must be computed as a capacity-weighted ratio (Decision 2).

Two approaches:

#### Option I — Compute in `_handle_coordinator_update` (sensor-side)

Override the computation for the `performance` key:

```python
@callback
def _handle_coordinator_update(self) -> None:
    nodes = self.coordinator.data.get("nodes", {})

    if self.entity_description.key == "performance":
        total_power = 0.0
        total_peak = 0.0
        has_data = False
        for barcode in self._barcodes:
            node_data = nodes.get(barcode)
            if node_data is None:
                continue
            power = node_data.get("power")
            peak = node_data.get("peak_power")
            if power is not None and peak is not None and peak > 0:
                total_power += max(power, 0.0)
                total_peak += peak
                has_data = True
        if has_data and total_peak > 0:
            self._attr_native_value = round((total_power / total_peak) * 100.0, 2)
        else:
            self._attr_native_value = None
    else:
        # existing sum logic for power/energy
        total = None
        for barcode in self._barcodes:
            node_data = nodes.get(barcode)
            if node_data is None:
                continue
            value = node_data.get(self.entity_description.value_key)
            if value is not None:
                total = (total or 0.0) + value
        self._attr_native_value = total

    self.async_write_ha_state()
```

#### Option II — Compute in coordinator (coordinator-side)

Have the coordinator maintain aggregate `performance` values in a separate data structure (e.g., `data["strings"]` and `data["installation"]`), computed on each power report.

**Recommendation:** **Option I** — sensor-side. It keeps the coordinator unchanged (no new data structures), is consistent with how aggregate power/energy is already computed in the sensor class, and is easy to test. The performance key special-case is a small addition to an existing method.

### 5.5 Entity Count Impact

| Level | Before | After | Delta |
|-------|--------|-------|-------|
| Per-module | 10 | 11 | +1 |
| Per-string | 3 | 4 | +1 |
| Installation | 3 | 4 | +1 |

**New formula:** `M × 11 + S × 4 + 4`

Example: 6 modules across 2 strings → `66 + 8 + 4 = 78` entities (was 69).

---

## 6. Translation / String Updates

### `strings.json` and `translations/en.json`

Add under `entity.sensor`:

```json
"performance": {
    "name": "Performance"
},
"string_performance": {
    "name": "Performance"
},
"installation_performance": {
    "name": "Performance"
}
```

Add to `config.step.add_module.data`:

```json
"peak_power": "Peak power (Wp)"
```

Add to `config.step.add_module.data_description`:

```json
"peak_power": "The peak power rating of this panel in watts (STC). Used to calculate performance percentage. Default: 455 Wp."
```

Same additions in `options.step.add_module.data` and `options.step.add_module.data_description`.

---

## 7. File Change Summary

| File | Change |
|------|--------|
| `const.py` | Add `CONF_MODULE_PEAK_POWER`, `DEFAULT_PEAK_POWER` |
| `config_flow.py` | Add `peak_power` to `ADD_MODULE_SCHEMA` with validation, update `_modules_description` |
| `__init__.py` | Bump `CONFIG_ENTRY_VERSION` to 4, add v3→v4 migration, import new constants |
| `coordinator.py` | Compute `performance` in `_handle_power_report`, add `peak_power`/`performance` to node data, update `reload_modules` |
| `sensor.py` | Add `performance` descriptions to all three description tuples, update aggregate `_handle_coordinator_update` for performance special case |
| `strings.json` | Add 3 sensor translation keys + `peak_power` config field strings |
| `translations/en.json` | Same as `strings.json` |
| `tests/test_sensor.py` | Update entity count assertions, add performance sensor tests (per-module, per-string, per-installation) |
| `tests/test_config_flow.py` | Add test for `peak_power` field in add_module flow |
| `tests/test_migration.py` | Add v3→v4 migration test |
| `tests/test_coordinator_persistence.py` | Add test for performance calculation in power report handling |

---

## 8. Test Plan

### 8.1 Migration Tests (`tests/test_migration.py`)

| Test | Description |
|------|-------------|
| `test_migrate_v3_to_v4_adds_peak_power` | Modules without `peak_power` get `DEFAULT_PEAK_POWER` (455) |
| `test_migrate_v3_to_v4_preserves_existing_peak_power` | Modules that already have `peak_power` (edge case — shouldn't exist in v3 but defensive) are not overwritten |
| `test_migrate_v3_to_v4_mixed` | Mix of modules with and without peak_power |

### 8.2 Config Flow Tests (`tests/test_config_flow.py`)

| Test | Description |
|------|-------------|
| `test_add_module_with_default_peak_power` | Submitting without explicit `peak_power` stores `DEFAULT_PEAK_POWER` |
| `test_add_module_with_custom_peak_power` | Submitting `peak_power=400` stores 400 |
| `test_add_module_peak_power_validation` | `peak_power=0` or `peak_power=-1` rejected by schema validation |

### 8.3 Sensor Tests (`tests/test_sensor.py`)

| Test | Description |
|------|-------------|
| `test_entity_count_with_performance` | 2 modules × 11 + 2 strings × 4 + 4 installation = 34 entities |
| `test_entity_count_single_string_with_performance` | 2 modules × 11 + 1 string × 4 + 4 = 30 entities |
| `test_performance_sensor_description` | Correct `state_class=MEASUREMENT`, unit `%`, no device_class |
| `test_performance_sensor_value` | Power=300W, peak=455Wp → performance=65.93% |
| `test_performance_sensor_zero_power` | Power=0W → performance=0.0% |
| `test_performance_sensor_above_100` | Power=500W, peak=455Wp → performance=109.89% (not clamped) |
| `test_performance_sensor_unavailable_no_data` | No node data → performance unavailable |
| `test_string_performance_weighted` | String with 300W/400Wp + 200W/500Wp → (500/900)×100 = 55.56% |
| `test_installation_performance_weighted` | Same as string but across all modules |
| `test_aggregate_performance_partial_data` | Only 1 of 2 nodes reporting: uses only reporting node's power and peak |
| `test_aggregate_performance_excludes_none_power` | Nodes with `None` power excluded from both numerator and denominator |
| `test_aggregate_performance_no_data` | No node data → aggregate performance `None` |

### 8.4 Coordinator Tests (`tests/test_coordinator_persistence.py`)

| Test | Description |
|------|-------------|
| `test_power_report_includes_performance` | After handling a power report, `data["nodes"][barcode]` has `performance` field |
| `test_power_report_performance_calculation` | Power=250W, peak=455Wp → performance=54.95% |
| `test_power_report_default_peak_power` | Module without `peak_power` in config uses `DEFAULT_PEAK_POWER` |

---

## 9. Implementation Order

| Step | Files | Description |
|------|-------|-------------|
| 1 | `const.py` | Add `CONF_MODULE_PEAK_POWER`, `DEFAULT_PEAK_POWER` |
| 2 | `config_flow.py` | Add `peak_power` to schema, update `_modules_description`, update both add_module steps |
| 3 | `__init__.py` | Bump version to 4, add v3→v4 migration |
| 4 | `coordinator.py` | Compute performance in `_handle_power_report`, update `reload_modules` |
| 5 | `sensor.py` | Add performance descriptions to all 3 tuples, update aggregate `_handle_coordinator_update` |
| 6 | `strings.json`, `translations/en.json` | Translation keys for 3 new sensors + config field |
| 7 | `tests/test_migration.py` | v3→v4 migration tests |
| 8 | `tests/test_config_flow.py` | Peak power config flow tests |
| 9 | `tests/test_sensor.py` | Performance sensor tests (entity count, values, aggregation) |
| 10 | `tests/test_coordinator_persistence.py` | Performance calculation tests |
| 11 | Lint + full test suite | `ruff check` + `pytest tests/ -vv` |
| 12 | `future_considerations.md` | Mark section 3 as implemented |

---

## 10. Edge Cases

| Case | Handling |
|------|----------|
| **Peak power = 0 in config** | Schema validation rejects (`vol.Range(min=1)`). Coordinator defensively treats ≤0 as `DEFAULT_PEAK_POWER`. |
| **Power > peak power** | Performance > 100%. Not clamped. Physically valid (irradiance spikes, cold temps). |
| **Power is `None`** | Performance is `None` (unavailable). |
| **Power is 0** | Performance is 0.0%. |
| **Negative power** | Power already clamped to ≥0 in energy accumulation. Performance uses the same clamped value. |
| **Mixed peak powers in aggregate** | Capacity-weighted formula handles correctly: each panel's power weighted by its own peak power. |
| **Module added mid-day** | Performance is `None` until first power report. |
| **All string members have `None` power** | String aggregate performance is `None` (unavailable). |
| **One string member missing** | Aggregate denominator includes only reporting members' peak power. |
| **Migration with no modules** | Migration loop is a no-op; empty list is valid. |

---

## 11. Future Extension: Options Flow for Editing Peak Power

The current options flow supports adding and removing modules but not **editing** existing module parameters. After this feature is implemented, users who want to change a panel's peak power must remove and re-add the module.

A future enhancement (not part of this feature) could add an "Edit module" option to the options flow menu that allows modifying `name`, `string`, and `peak_power` for existing modules without losing their energy history.

---

## 12. Unique ID Format

Performance sensors follow the existing unique ID conventions:

| Scope | Pattern | Example |
|-------|---------|---------|
| Per-module | `pytap_{barcode}_performance` | `pytap_A-1234567B_performance` |
| String | `pytap_{entry_id}_string_{name}_performance` | `pytap_abc123_string_A_performance` |
| Installation | `pytap_{entry_id}_installation_performance` | `pytap_abc123_installation_performance` |

---

## 13. Config Flow VERSION vs CONFIG_ENTRY_VERSION Mismatch

The current codebase has `PyTapConfigFlow.VERSION = 2` but `CONFIG_ENTRY_VERSION = 3`. The config flow `VERSION` determines what version is stamped on new entries, while `CONFIG_ENTRY_VERSION` + `async_migrate_entry` handles upgrading older entries.

For this feature, both should be bumped to `4`:
- `PyTapConfigFlow.VERSION = 4` — new entries include `peak_power` and are at version 4.
- `CONFIG_ENTRY_VERSION = 4` — migration handles v1→v2→v3→v4 chain.

This misalignment should be corrected as part of implementation.

---

## Summary of Decisions

| # | Decision | Resolved |
|---|----------|----------|
| 1 | Peak power scope | ✅ Per-module |
| 2 | Aggregate performance formula | ✅ Capacity-weighted (`Σpower / Σpeak`) |
| 3 | Performance clamping | ✅ Floor only (allow >100%) |
| 4 | Config flow UX | ✅ Optional field with 455W default |
| 5 | Performance when no data | ✅ Unavailable |
| 6 | Aggregate denominator | ✅ Reporting members only |
