# Feature 2: Total Cumulatives — Implementation Plan

## Overview

Create aggregate sensor entities that sum energy and power across optimizers at two levels:

| Level | Scope | Example Device Name |
|-------|-------|---------------------|
| **String** | All optimizers sharing a `string` group label | "Tigo String A" |
| **Installation** | All configured optimizers | "Tigo Installation" |

### Resolved Decisions

| # | Decision | Choice |
|---|----------|--------|
| 1 | Aggregation location | **Sensor-side** — computed on read from `coordinator.data["nodes"]` |
| 2 | Include aggregate power | **Yes** — instantaneous sum alongside daily and total energy |
| 3 | Device hierarchy | **Dedicated virtual devices** per string and for the installation |
| 4 | Empty string handling | **String is mandatory** — config flow and migration enforce this |
| 5 | Availability | **Available when ≥1 constituent has data** |
| 6 | `last_reset` for aggregate daily energy | **Today's midnight** in HA timezone |
| 7 | Unique ID format | **Include config entry ID** — `pytap_{entry_id}_string_{name}_{key}` |
| 8 | Sensor class | **New `PyTapAggregateSensor` class** — separate from `PyTapSensor` |

---

## 1. Make String Group Mandatory

### 1.1 Config flow changes (`config_flow.py`)

The `string` field in `ADD_MODULE_SCHEMA` must change from `vol.Optional` to `vol.Required`. Both the initial config flow (`async_step_add_module`) and the options flow (`async_step_add_module`) must be updated.

**Before:**
```python
ADD_MODULE_SCHEMA = vol.Schema(
    {
        vol.Optional(CONF_MODULE_STRING, default=""): str,
        vol.Required(CONF_MODULE_NAME): str,
        vol.Required(CONF_MODULE_BARCODE): str,
    }
)
```

**After:**
```python
ADD_MODULE_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_MODULE_STRING): str,
        vol.Required(CONF_MODULE_NAME): str,
        vol.Required(CONF_MODULE_BARCODE): str,
    }
)
```

Add validation: if the user submits an empty string value, show error `missing_string`.

### 1.2 Config entry migration (`__init__.py`)

Bump `CONFIG_ENTRY_VERSION` from `2` → `3`.

**v2 → v3 migration** in `async_migrate_entry`:
- Iterate `entry.data[CONF_MODULES]`.
- Any module with an empty or missing `string` field gets assigned a **default string label**.

**Question: what default?** Use `"Default"` as the fallback string name for modules that lack one. This is a one-time migration — new installs and edits always require a string.

### 1.3 Translation updates

Add error string:
```json
"missing_string": "String group is required."
```

Update `data_description` for clarity:
```json
"string": "String group label (e.g. A, B, East, West). Required for aggregate energy tracking."
```

---

## 2. Aggregate Sensor Descriptions

Three sensors per aggregate level (string + installation):

| Key | Translation Key | Unit | Device Class | State Class | value computation |
|-----|----------------|------|--------------|-------------|-------------------|
| `power` | `string_power` / `installation_power` | W | `POWER` | `MEASUREMENT` | Sum of `power` from constituent nodes |
| `daily_energy` | `string_daily_energy` / `installation_daily_energy` | Wh | `ENERGY` | `TOTAL` | Sum of `daily_energy_wh` from constituent nodes |
| `total_energy` | `string_total_energy` / `installation_total_energy` | Wh | `ENERGY` | `TOTAL_INCREASING` | Sum of `total_energy_wh` from constituent nodes |

Each aggregate sensor uses a new `PyTapAggregateSensorDescription` dataclass:

```python
@dataclass(frozen=True, kw_only=True)
class PyTapAggregateSensorDescription(SensorEntityDescription):
    """Describes a PyTap aggregate sensor entity."""

    value_key: str  # key in node data to sum (e.g. "power", "daily_energy_wh")
```

**Sensor description tuples:**

```python
STRING_SENSOR_DESCRIPTIONS: tuple[PyTapAggregateSensorDescription, ...] = (
    PyTapAggregateSensorDescription(
        key="power",
        translation_key="string_power",
        native_unit_of_measurement=UnitOfPower.WATT,
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        value_key="power",
    ),
    PyTapAggregateSensorDescription(
        key="daily_energy",
        translation_key="string_daily_energy",
        native_unit_of_measurement=UnitOfEnergy.WATT_HOUR,
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL,
        suggested_display_precision=1,
        value_key="daily_energy_wh",
    ),
    PyTapAggregateSensorDescription(
        key="total_energy",
        translation_key="string_total_energy",
        native_unit_of_measurement=UnitOfEnergy.WATT_HOUR,
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL_INCREASING,
        suggested_display_precision=0,
        value_key="total_energy_wh",
    ),
)

INSTALLATION_SENSOR_DESCRIPTIONS: tuple[PyTapAggregateSensorDescription, ...] = (
    PyTapAggregateSensorDescription(
        key="power",
        translation_key="installation_power",
        native_unit_of_measurement=UnitOfPower.WATT,
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        value_key="power",
    ),
    PyTapAggregateSensorDescription(
        key="daily_energy",
        translation_key="installation_daily_energy",
        native_unit_of_measurement=UnitOfEnergy.WATT_HOUR,
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL,
        suggested_display_precision=1,
        value_key="daily_energy_wh",
    ),
    PyTapAggregateSensorDescription(
        key="total_energy",
        translation_key="installation_total_energy",
        native_unit_of_measurement=UnitOfEnergy.WATT_HOUR,
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL_INCREASING,
        suggested_display_precision=0,
        value_key="total_energy_wh",
    ),
)
```

---

## 3. Aggregate Sensor Class (`PyTapAggregateSensor`)

A new class in `sensor.py` that computes values by summing over constituent optimizer nodes on each coordinator update.

```python
class PyTapAggregateSensor(CoordinatorEntity[PyTapDataUpdateCoordinator], SensorEntity):
    """Aggregate sensor that sums values across multiple optimizers."""

    _attr_has_entity_name = True
    entity_description: PyTapAggregateSensorDescription

    def __init__(
        self,
        coordinator: PyTapDataUpdateCoordinator,
        description: PyTapAggregateSensorDescription,
        barcodes: list[str],
        device_info: DeviceInfo,
        unique_id: str,
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        self._barcodes = barcodes
        self._attr_unique_id = unique_id
        self._attr_device_info = device_info
```

### 3.1 Value computation (`_handle_coordinator_update`)

```python
@callback
def _handle_coordinator_update(self) -> None:
    nodes = self.coordinator.data.get("nodes", {})
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

If **no constituent has data**, `total` stays `None` → sensor is unavailable.
If **at least one** has data, `total` is the partial sum → sensor is available and shows the sum.

### 3.2 Availability

```python
@property
def available(self) -> bool:
    if not self.coordinator.data:
        return False
    nodes = self.coordinator.data.get("nodes", {})
    return any(nodes.get(bc) is not None for bc in self._barcodes)
```

### 3.3 `last_reset` for daily energy aggregates

```python
@property
def last_reset(self) -> datetime | None:
    if self.entity_description.key != "daily_energy":
        return None
    timezone = dt_util.UTC
    if self.hass is not None:
        timezone = dt_util.get_time_zone(self.hass.config.time_zone)
    return datetime.combine(dt_util.now().date(), time.min, tzinfo=timezone)
```

### 3.4 Extra state attributes

```python
@property
def extra_state_attributes(self) -> dict[str, Any] | None:
    nodes = self.coordinator.data.get("nodes", {})
    reporting = [bc for bc in self._barcodes if nodes.get(bc) is not None]
    return {
        "optimizer_count": len(self._barcodes),
        "reporting_count": len(reporting),
    }
```

---

## 4. Device Hierarchy

### 4.1 Per-string virtual device

```python
DeviceInfo(
    identifiers={(DOMAIN, f"{entry.entry_id}_string_{string_name}")},
    name=f"Tigo String {string_name}",
    manufacturer="Tigo Energy",
    model="String Aggregate",
)
```

### 4.2 Installation virtual device

```python
DeviceInfo(
    identifiers={(DOMAIN, f"{entry.entry_id}_installation")},
    name="Tigo Installation",
    manufacturer="Tigo Energy",
    model="Installation Aggregate",
)
```

---

## 5. Entity Creation (`async_setup_entry`)

Update `sensor.py`'s `async_setup_entry` to create aggregate entities after individual ones:

```python
async def async_setup_entry(hass, entry, async_add_entities):
    coordinator = hass.data[DOMAIN][entry.entry_id]
    modules = entry.data.get(CONF_MODULES, [])
    entities = []

    # --- Per-optimizer sensors (existing) ---
    for module_config in modules:
        barcode = module_config.get(CONF_MODULE_BARCODE, "")
        if not barcode:
            continue
        for description in SENSOR_DESCRIPTIONS:
            entities.append(PyTapSensor(coordinator, description, module_config, entry))

    # --- Build string → barcodes mapping ---
    string_to_barcodes: dict[str, list[str]] = {}
    all_barcodes: list[str] = []
    for module_config in modules:
        barcode = module_config.get(CONF_MODULE_BARCODE, "")
        string_name = module_config.get(CONF_MODULE_STRING, "")
        if not barcode:
            continue
        all_barcodes.append(barcode)
        if string_name:
            string_to_barcodes.setdefault(string_name, []).append(barcode)

    # --- Per-string aggregate sensors ---
    for string_name, barcodes in string_to_barcodes.items():
        device_info = DeviceInfo(
            identifiers={(DOMAIN, f"{entry.entry_id}_string_{string_name}")},
            name=f"Tigo String {string_name}",
            manufacturer="Tigo Energy",
            model="String Aggregate",
        )
        for description in STRING_SENSOR_DESCRIPTIONS:
            entities.append(
                PyTapAggregateSensor(
                    coordinator=coordinator,
                    description=description,
                    barcodes=barcodes,
                    device_info=device_info,
                    unique_id=f"{DOMAIN}_{entry.entry_id}_string_{string_name}_{description.key}",
                )
            )

    # --- Installation aggregate sensors ---
    if all_barcodes:
        device_info = DeviceInfo(
            identifiers={(DOMAIN, f"{entry.entry_id}_installation")},
            name="Tigo Installation",
            manufacturer="Tigo Energy",
            model="Installation Aggregate",
        )
        for description in INSTALLATION_SENSOR_DESCRIPTIONS:
            entities.append(
                PyTapAggregateSensor(
                    coordinator=coordinator,
                    description=description,
                    barcodes=all_barcodes,
                    device_info=device_info,
                    unique_id=f"{DOMAIN}_{entry.entry_id}_installation_{description.key}",
                )
            )

    async_add_entities(entities)
```

### Entity count formula

Per config entry with `S` distinct strings and `M` modules:
- Individual: `M × 10` (unchanged)
- String: `S × 3`
- Installation: `3`
- **Total: `M × 10 + S × 3 + 3`**

Example: 6 modules across 2 strings → `60 + 6 + 3 = 69` entities.

---

## 6. Unique ID Format

| Scope | Pattern | Example |
|-------|---------|---------|
| String power | `pytap_{entry_id}_string_{name}_power` | `pytap_abc123_string_A_power` |
| String daily energy | `pytap_{entry_id}_string_{name}_daily_energy` | `pytap_abc123_string_A_daily_energy` |
| String total energy | `pytap_{entry_id}_string_{name}_total_energy` | `pytap_abc123_string_A_total_energy` |
| Installation power | `pytap_{entry_id}_installation_power` | `pytap_abc123_installation_power` |
| Installation daily energy | `pytap_{entry_id}_installation_daily_energy` | `pytap_abc123_installation_daily_energy` |
| Installation total energy | `pytap_{entry_id}_installation_total_energy` | `pytap_abc123_installation_total_energy` |

---

## 7. Translation Strings

### `strings.json` and `translations/en.json`

Add under `entity.sensor`:

```json
"string_power": {
    "name": "Power"
},
"string_daily_energy": {
    "name": "Daily energy"
},
"string_total_energy": {
    "name": "Total energy"
},
"installation_power": {
    "name": "Power"
},
"installation_daily_energy": {
    "name": "Daily energy"
},
"installation_total_energy": {
    "name": "Total energy"
}
```

Add under `config.error`:

```json
"missing_string": "String group is required."
```

Update `data_description` for `string`:

```json
"string": "String group label (e.g. A, B, East, West). Required for aggregate energy tracking."
```

---

## 8. Config Entry Migration (v2 → v3)

### 8.1 Version bump

In `__init__.py`:
```python
CONFIG_ENTRY_VERSION = 3
```

### 8.2 Migration logic

In `async_migrate_entry`, add v2 → v3 handling:

```python
if entry.version == 2:
    _LOGGER.info("Migrating config entry from v2 to v3: making string mandatory")
    modules = list(entry.data.get(CONF_MODULES, []))
    updated = False
    for module in modules:
        if not module.get(CONF_MODULE_STRING):
            module[CONF_MODULE_STRING] = "Default"
            updated = True
    if updated:
        new_data = {**entry.data, CONF_MODULES: modules}
        hass.config_entries.async_update_entry(entry, data=new_data, version=3)
    else:
        hass.config_entries.async_update_entry(entry, version=3)
```

This ensures existing users with empty string fields get a `"Default"` label automatically. Their aggregate sensors will appear under a "Tigo String Default" device.

---

## 9. File Change Summary

| File | Change |
|------|--------|
| `__init__.py` | Bump `CONFIG_ENTRY_VERSION` to 3, add v2→v3 migration (default string) |
| `config_flow.py` | Make `CONF_MODULE_STRING` required, add `missing_string` validation |
| `sensor.py` | Add `PyTapAggregateSensorDescription`, `STRING_SENSOR_DESCRIPTIONS`, `INSTALLATION_SENSOR_DESCRIPTIONS`, `PyTapAggregateSensor` class, update `async_setup_entry` |
| `const.py` | Add `DEFAULT_STRING_NAME = "Default"` if needed |
| `strings.json` | Add 6 aggregate translation keys + `missing_string` error + updated descriptions |
| `translations/en.json` | Same as `strings.json` |
| `tests/test_sensor.py` | Update entity count, add aggregate sensor tests |
| `tests/test_migration.py` | Add v2→v3 migration test |
| `tests/test_config_flow.py` | Update for mandatory string field |

---

## 10. Test Plan

### 10.1 Aggregate sensor tests (`tests/test_sensor.py`)

| Test | Description |
|------|-------------|
| `test_aggregate_entity_count` | 2 modules, 2 strings → 20 + 6 + 3 = 29 entities |
| `test_aggregate_entity_count_single_string` | 2 modules, 1 string → 20 + 3 + 3 = 26 entities |
| `test_string_aggregate_unique_ids` | Verify unique ID format includes entry_id and string name |
| `test_installation_aggregate_unique_ids` | Verify unique ID format includes entry_id |
| `test_string_aggregate_device_info` | Verify virtual device identifiers, name, manufacturer |
| `test_installation_aggregate_device_info` | Verify virtual device identifiers, name, manufacturer |
| `test_string_power_sums_constituents` | String power = sum of node powers in that string |
| `test_installation_power_sums_all` | Installation power = sum of all node powers |
| `test_string_daily_energy_sums` | String daily energy = sum of node daily_energy_wh |
| `test_string_total_energy_sums` | String total energy = sum of node total_energy_wh |
| `test_installation_daily_energy_sums_all` | Installation daily energy = sum of all node daily_energy_wh |
| `test_installation_total_energy_sums_all` | Installation total energy = sum of all node total_energy_wh |
| `test_aggregate_available_partial_data` | Available when at least 1 constituent has data |
| `test_aggregate_unavailable_no_data` | Unavailable when no constituent has data |
| `test_aggregate_daily_energy_last_reset` | Returns today's midnight in HA timezone |
| `test_aggregate_total_energy_no_last_reset` | `last_reset` is None for total energy |
| `test_aggregate_extra_attributes` | `optimizer_count` and `reporting_count` present |
| `test_aggregate_excludes_none_values` | Nodes with `None` power are excluded from sum |
| `test_no_string_aggregates_when_no_modules` | No aggregate entities created for empty module list |

### 10.2 Migration tests (`tests/test_migration.py`)

| Test | Description |
|------|-------------|
| `test_migrate_v2_to_v3_empty_strings` | Modules with empty string get "Default" |
| `test_migrate_v2_to_v3_existing_strings` | Modules with strings are untouched |
| `test_migrate_v2_to_v3_mixed` | Mix of empty and filled strings |

### 10.3 Config flow tests (`tests/test_config_flow.py`)

| Test | Description |
|------|-------------|
| `test_add_module_missing_string` | Error shown when string is empty/missing |
| `test_add_module_with_string` | Module added successfully with string |

---

## 11. Implementation Order

| Step | Files | Description |
|------|-------|-------------|
| 1 | `const.py` | Add `DEFAULT_STRING_NAME` constant |
| 2 | `config_flow.py` | Make string mandatory, add validation |
| 3 | `__init__.py` | Bump version, add v2→v3 migration |
| 4 | `sensor.py` | Add `PyTapAggregateSensorDescription`, `PyTapAggregateSensor`, both description tuples, updated `async_setup_entry` |
| 5 | `strings.json`, `translations/en.json` | Translation keys for 6 new sensors + error + updated descriptions |
| 6 | `tests/test_config_flow.py` | Update for mandatory string |
| 7 | `tests/test_migration.py` | v2→v3 migration tests |
| 8 | `tests/test_sensor.py` | Aggregate sensor tests |
| 9 | Lint + full test suite | `ruff check` + `pytest tests/ -vv` |
| 10 | `future_considerations.md` | Mark section 2 as implemented |

---

## 12. Energy Dashboard Compatibility

All six aggregate energy sensors are energy-dashboard-ready:

| Sensor | Dashboard Use |
|--------|---------------|
| String daily energy | `TOTAL` + `last_reset` → individual energy source per string |
| String total energy | `TOTAL_INCREASING` → lifetime counter per string |
| Installation daily energy | `TOTAL` + `last_reset` → single energy source for entire system |
| Installation total energy | `TOTAL_INCREASING` → lifetime counter for entire system |

Users can choose to add either per-optimizer, per-string, or installation-level sensors to the energy dashboard — or any combination. The values at each level are independently computed sums that will track consistently.

---

## 13. Edge Cases

| Case | Handling |
|------|----------|
| **Single module, single string** | String aggregate = same as individual; installation aggregate = same as string. All three levels exist. |
| **Module added mid-day** | New module starts with `daily_energy_wh = 0`. String and installation aggregates update immediately. |
| **Module removed** | On reload, aggregate recalculates from remaining modules. Old module's data no longer in coordinator. |
| **String renamed** | Config entry version stays at 3 (no schema change). Old string aggregate device becomes orphaned; new one is created. User deletes old device manually. |
| **All modules in same string** | Only 1 string aggregate device, identical to installation aggregate (but both created). |
| **HA restart** | Aggregate sensors recompute on first coordinator update. No persistence needed for aggregates. |
| **No data yet after startup** | All aggregates show unavailable until first power report for any constituent. |
