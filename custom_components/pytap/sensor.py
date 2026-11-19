"""Sensor platform for PyTap integration.

Creates sensor entities for each user-configured Tigo optimizer module.
Entities are keyed by barcode (stable identifier) and created deterministically
from the configured module list — no auto-discovery.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, tzinfo
import logging
from typing import Any

from homeassistant.components.sensor import (
    RestoreSensor,
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    SIGNAL_STRENGTH_DECIBELS_MILLIWATT,
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
    EntityCategory,
    UnitOfElectricCurrent,
    UnitOfElectricPotential,
    UnitOfEnergy,
    UnitOfPower,
    UnitOfTemperature,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.restore_state import ExtraStoredData
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util

from .const import (
    CONF_MODULE_BARCODE,
    CONF_MODULE_NAME,
    CONF_MODULE_PEAK_POWER,
    CONF_MODULE_STRING,
    CONF_MODULES,
    DEFAULT_PEAK_POWER,
    DOMAIN,
    STALE_ACTION_HOLD,
    STALE_ACTION_ZERO,
)
from .coordinator import PyTapConfigEntry, PyTapDataUpdateCoordinator

_LOGGER = logging.getLogger(__name__)

# Electrical readings that report 0 when the stale action is "zero". Others
# (temperature, duty cycle, RSSI) have no meaningful zero and go
# unavailable instead.
ZERO_WHEN_STALE = frozenset(
    {"power", "voltage_in", "voltage_out", "current_in", "current_out", "performance"}
)


def _coerce_restored_state_value(raw_state: str, sensor_key: str) -> int | float | None:
    """Convert a restored state string to a native numeric sensor value."""
    if raw_state in (STATE_UNKNOWN, STATE_UNAVAILABLE, "None", "none", ""):
        return None

    try:
        numeric_value = float(raw_state)
    except (TypeError, ValueError):
        return None

    if sensor_key == "readings_today":
        return int(numeric_value)

    return numeric_value


@dataclass(frozen=True, kw_only=True)
class PyTapSensorEntityDescription(SensorEntityDescription):
    """Describes a PyTap sensor entity."""

    value_key: str
    # Live readings follow the stale-reading action once a panel stops
    # reporting; energy totals stay available so history is preserved.
    live: bool = True


@dataclass(frozen=True, kw_only=True)
class PyTapAggregateSensorDescription(SensorEntityDescription):
    """Describes a PyTap aggregate sensor entity."""

    value_key: str
    live: bool = True


SENSOR_DESCRIPTIONS: tuple[PyTapSensorEntityDescription, ...] = (
    PyTapSensorEntityDescription(
        key="performance",
        translation_key="performance",
        native_unit_of_measurement="%",
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
        value_key="performance",
    ),
    PyTapSensorEntityDescription(
        key="power",
        translation_key="power",
        native_unit_of_measurement=UnitOfPower.WATT,
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        value_key="power",
    ),
    PyTapSensorEntityDescription(
        key="voltage_in",
        translation_key="voltage_in",
        native_unit_of_measurement=UnitOfElectricPotential.VOLT,
        device_class=SensorDeviceClass.VOLTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        value_key="voltage_in",
    ),
    PyTapSensorEntityDescription(
        key="voltage_out",
        translation_key="voltage_out",
        native_unit_of_measurement=UnitOfElectricPotential.VOLT,
        device_class=SensorDeviceClass.VOLTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        value_key="voltage_out",
    ),
    PyTapSensorEntityDescription(
        key="current_in",
        translation_key="current_in",
        native_unit_of_measurement=UnitOfElectricCurrent.AMPERE,
        device_class=SensorDeviceClass.CURRENT,
        state_class=SensorStateClass.MEASUREMENT,
        value_key="current_in",
    ),
    PyTapSensorEntityDescription(
        key="current_out",
        translation_key="current_out",
        native_unit_of_measurement=UnitOfElectricCurrent.AMPERE,
        device_class=SensorDeviceClass.CURRENT,
        state_class=SensorStateClass.MEASUREMENT,
        value_key="current_out",
    ),
    PyTapSensorEntityDescription(
        key="temperature",
        translation_key="temperature",
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        device_class=SensorDeviceClass.TEMPERATURE,
        state_class=SensorStateClass.MEASUREMENT,
        value_key="temperature",
    ),
    PyTapSensorEntityDescription(
        key="dc_dc_duty_cycle",
        translation_key="dc_dc_duty_cycle",
        native_unit_of_measurement="%",
        state_class=SensorStateClass.MEASUREMENT,
        value_key="dc_dc_duty_cycle",
    ),
    PyTapSensorEntityDescription(
        key="rssi",
        translation_key="rssi",
        native_unit_of_measurement=SIGNAL_STRENGTH_DECIBELS_MILLIWATT,
        device_class=SensorDeviceClass.SIGNAL_STRENGTH,
        state_class=SensorStateClass.MEASUREMENT,
        value_key="rssi",
    ),
    PyTapSensorEntityDescription(
        key="daily_energy",
        translation_key="daily_energy",
        native_unit_of_measurement=UnitOfEnergy.WATT_HOUR,
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL,
        suggested_display_precision=1,
        value_key="daily_energy_wh",
        live=False,
    ),
    PyTapSensorEntityDescription(
        key="total_energy",
        translation_key="total_energy",
        native_unit_of_measurement=UnitOfEnergy.WATT_HOUR,
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL_INCREASING,
        suggested_display_precision=0,
        value_key="total_energy_wh",
        live=False,
    ),
    PyTapSensorEntityDescription(
        key="readings_today",
        translation_key="readings_today",
        state_class=SensorStateClass.TOTAL,
        entity_category=EntityCategory.DIAGNOSTIC,
        suggested_display_precision=0,
        value_key="readings_today",
        live=False,
    ),
)


STRING_SENSOR_DESCRIPTIONS: tuple[PyTapAggregateSensorDescription, ...] = (
    PyTapAggregateSensorDescription(
        key="performance",
        translation_key="string_performance",
        native_unit_of_measurement="%",
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
        value_key="performance",
    ),
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
        live=False,
    ),
    PyTapAggregateSensorDescription(
        key="total_energy",
        translation_key="string_total_energy",
        native_unit_of_measurement=UnitOfEnergy.WATT_HOUR,
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL_INCREASING,
        suggested_display_precision=0,
        value_key="total_energy_wh",
        live=False,
    ),
)


INSTALLATION_SENSOR_DESCRIPTIONS: tuple[PyTapAggregateSensorDescription, ...] = (
    PyTapAggregateSensorDescription(
        key="performance",
        translation_key="installation_performance",
        native_unit_of_measurement="%",
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
        value_key="performance",
    ),
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
        live=False,
    ),
    PyTapAggregateSensorDescription(
        key="total_energy",
        translation_key="installation_total_energy",
        native_unit_of_measurement=UnitOfEnergy.WATT_HOUR,
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL_INCREASING,
        suggested_display_precision=0,
        value_key="total_energy_wh",
        live=False,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: PyTapConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up PyTap sensors from a config entry.

    Creates sensor entities deterministically from the configured module list.
    Each configured module gets the full set of 12 sensor entities.
    """
    coordinator = entry.runtime_data
    modules: list[dict[str, str]] = entry.data.get(CONF_MODULES, [])

    entities: list[SensorEntity] = []
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

        for description in SENSOR_DESCRIPTIONS:
            entities.append(
                PyTapSensor(
                    coordinator=coordinator,
                    description=description,
                    module_config=module_config,
                    entry=entry,
                )
            )

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
                    unique_id=(
                        f"{DOMAIN}_{entry.entry_id}_string_{string_name}_{description.key}"
                    ),
                )
            )

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
                    unique_id=(
                        f"{DOMAIN}_{entry.entry_id}_installation_{description.key}"
                    ),
                )
            )

    async_add_entities(entities)


class PyTapSensor(CoordinatorEntity[PyTapDataUpdateCoordinator], RestoreSensor):
    """Representation of a PyTap optimizer sensor.

    Each sensor reads a specific measurement (power, voltage, etc.) for
    a single Tigo optimizer identified by its barcode.
    """

    _attr_has_entity_name = True
    entity_description: PyTapSensorEntityDescription

    def __init__(
        self,
        coordinator: PyTapDataUpdateCoordinator,
        description: PyTapSensorEntityDescription,
        module_config: dict[str, str],
        entry: ConfigEntry,
    ) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator)
        self.entity_description = description

        self._barcode = module_config[CONF_MODULE_BARCODE]
        self._module_name = module_config[CONF_MODULE_NAME]
        self._module_string = module_config.get(CONF_MODULE_STRING, "")

        # Unique ID: domain + barcode + sensor key
        self._attr_unique_id = f"{DOMAIN}_{self._barcode}_{description.key}"

        # Device info — groups all sensors for one optimizer under one device
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, self._barcode)},
            name=f"Tigo TS4 {self._module_name}",
            manufacturer="Tigo Energy",
            model="TS4",
            serial_number=self._barcode,
        )
        self._restored_native_value = False

    async def async_added_to_hass(self) -> None:
        """Restore last known native value from Home Assistant state cache."""
        await super().async_added_to_hass()
        # Coordinator listeners only see global updates; panel data changes
        # notify just this panel's entities.
        self.async_on_remove(
            self.coordinator.async_add_data_listener([self._barcode], self._handle_coordinator_update)
        )
        if self.entity_description.live:
            # Energy totals don't depend on whether the panel is reporting.
            self.async_on_remove(
                self.coordinator.async_add_freshness_listener(
                    [self._barcode], self._handle_coordinator_update
                )
            )
        nodes = self.coordinator.data.get("nodes", {})
        node = nodes.get(self._barcode)
        # Only short-circuit to coordinator data if this sensor has a non-None value.
        if node is not None:
            coordinator_value = node.get(self.entity_description.value_key)
            if coordinator_value is not None:
                self._handle_coordinator_update()
                return

        if restored := await self.async_get_last_sensor_data():
            if restored.native_value is not None:
                self._attr_native_value = restored.native_value
                self._restored_native_value = True
                return

        restored_state = await self.async_get_last_state()
        if restored_state is None:
            return

        native_value = _coerce_restored_state_value(
            restored_state.state,
            self.entity_description.key,
        )
        if native_value is not None:
            self._attr_native_value = native_value
            self._restored_native_value = True

    def _stale(self) -> bool:
        """A live reading whose panel has stopped sending valid reports."""
        return self.entity_description.live and not self.coordinator.is_reporting(self._barcode)

    @property
    def available(self) -> bool:
        """Live readings follow the stale-reading action; energy totals need any data."""
        if not self.coordinator.data:
            return False
        node_data = self.coordinator.data.get("nodes", {}).get(self._barcode)
        if not self.entity_description.live:
            return node_data is not None or self._restored_native_value
        action = self.coordinator.stale_action
        if node_data is None:
            return action == STALE_ACTION_HOLD and self._restored_native_value
        if not self._stale():
            return True
        if action == STALE_ACTION_HOLD:
            return True
        return action == STALE_ACTION_ZERO and self.entity_description.key in ZERO_WHEN_STALE

    @callback
    def _handle_coordinator_update(self) -> None:
        """Handle updated data from the coordinator."""
        node_data = self.coordinator.data.get("nodes", {}).get(self._barcode)
        key = self.entity_description.key
        if node_data:
            value = node_data.get(self.entity_description.value_key)
            if (
                key in ZERO_WHEN_STALE
                and self.coordinator.stale_action == STALE_ACTION_ZERO
                and self._stale()
            ):
                value = 0.0
            elif key == "dc_dc_duty_cycle" and value is not None:
                # Convert duty cycle from 0.0-1.0 to percentage
                value = round(value * 100, 2)
            self._attr_native_value = value
            self._restored_native_value = False
        else:
            if not self._restored_native_value:
                self._attr_native_value = None
        self.async_write_ha_state()

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Return additional state attributes."""
        node_data = self.coordinator.data.get("nodes", {}).get(self._barcode)
        if not node_data:
            return None
        attrs: dict[str, Any] = {}
        if self._module_string:
            attrs["string_group"] = self._module_string
        last_update = node_data.get("last_update")
        if last_update:
            attrs["last_update"] = last_update
        gateway_id = node_data.get("gateway_id")
        if gateway_id is not None:
            attrs["gateway_id"] = gateway_id
        return attrs if attrs else None

    @property
    def last_reset(self) -> datetime | None:
        """Return last reset for daily energy sensor cycles."""
        if self.entity_description.key not in ("daily_energy", "readings_today"):
            return None

        node_data = self.coordinator.data.get("nodes", {}).get(self._barcode)
        if not node_data:
            return None

        reset_date = node_data.get("daily_reset_date")
        if not reset_date:
            return None

        try:
            reset_day = date.fromisoformat(reset_date)
        except ValueError:
            return None

        return datetime.combine(reset_day, time.min, tzinfo=self._timezone())

    def _timezone(self) -> tzinfo:
        if self.hass is not None:
            return dt_util.get_time_zone(self.hass.config.time_zone) or dt_util.UTC
        return dt_util.UTC


@dataclass
class _AggregateEnergyData(ExtraStoredData):
    """Restorable aggregate energy: own total plus each member's last seen value."""

    native_value: float | None
    native_unit_of_measurement: str | None
    baselines: dict[str, float]
    cycle_date: str | None

    def as_dict(self) -> dict[str, Any]:
        return {
            "native_value": self.native_value,
            "native_unit_of_measurement": self.native_unit_of_measurement,
            "baselines": dict(self.baselines),
            "cycle_date": self.cycle_date,
        }


class PyTapAggregateSensor(
    CoordinatorEntity[PyTapDataUpdateCoordinator], RestoreSensor
):
    """Aggregate across optimizers.

    Live values sum current member readings. Energy totals instead add up each
    member's *increase* since it was last seen, so membership changes (a panel
    moved between strings, a newly added panel) never add a member's
    lifetime energy or look like a meter reset in long-term statistics.
    """

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
        """Initialize aggregate sensor."""
        super().__init__(coordinator)
        self.entity_description = description
        self._barcodes = barcodes
        self._attr_unique_id = unique_id
        self._attr_device_info = device_info
        self._restored_native_value = False
        self._energy_total: float | None = None
        self._baselines: dict[str, float] = {}
        self._cycle_date: str | None = None

    @property
    def _is_energy(self) -> bool:
        return self.entity_description.key in ("daily_energy", "total_energy")

    @property
    def _is_daily(self) -> bool:
        return self.entity_description.key == "daily_energy"

    async def async_added_to_hass(self) -> None:
        """Restore last known aggregate value from Home Assistant state cache."""
        await super().async_added_to_hass()
        # Re-render on members' data and (every aggregate reports reporting_count) freshness.
        self.async_on_remove(
            self.coordinator.async_add_data_listener(self._barcodes, self._handle_coordinator_update)
        )
        self.async_on_remove(
            self.coordinator.async_add_freshness_listener(self._barcodes, self._handle_coordinator_update)
        )
        if self._is_energy:
            await self._async_restore_energy()
            if any(
                (node := self.coordinator.data.get("nodes", {}).get(barcode)) is not None
                and node.get(self.entity_description.value_key) is not None
                for barcode in self._barcodes
            ):
                self._handle_coordinator_update()
            return

        nodes = self.coordinator.data.get("nodes", {})
        # Only short-circuit to live data if at least one node already has
        # a non-None value for the aggregate's underlying field. This avoids
        # overwriting a restored value with None from placeholder node dicts.
        value_key = (
            "power" if self.entity_description.key == "performance" else self.entity_description.value_key
        )
        if any(
            (node := nodes.get(barcode)) is not None and node.get(value_key) is not None
            for barcode in self._barcodes
        ):
            self._handle_coordinator_update()
            return

        if (native_value := await self._async_restored_value()) is not None:
            self._attr_native_value = native_value
            self._restored_native_value = True

    async def _async_restored_value(self) -> float | None:
        if (restored := await self.async_get_last_sensor_data()) is not None:
            if isinstance(restored.native_value, (int, float)):
                return float(restored.native_value)
        if (restored_state := await self.async_get_last_state()) is not None:
            return _coerce_restored_state_value(restored_state.state, self.entity_description.key)
        return None

    async def _async_restore_energy(self) -> None:
        """Restore the own total and member baselines; upgrade from a plain sum."""
        extra = await self.async_get_last_extra_data()
        data = extra.as_dict() if extra is not None else {}
        baselines = data.get("baselines")
        if isinstance(baselines, dict) and isinstance(data.get("native_value"), (int, float)):
            self._energy_total = float(data["native_value"])
            self._baselines = {
                str(barcode): float(value)
                for barcode, value in baselines.items()
                if isinstance(value, (int, float))
            }
            cycle_date = data.get("cycle_date")
            self._cycle_date = cycle_date if isinstance(cycle_date, str) else None
        elif not self._is_daily:
            # Earlier versions summed lifetime totals; continue from that value
            # and treat every member as newly joined (no double counting).
            self._energy_total = await self._async_restored_value()
        if self._energy_total is not None:
            self._attr_native_value = self._energy_total
            self._restored_native_value = True

    @property
    def extra_restore_state_data(self) -> ExtraStoredData:
        if not self._is_energy:
            return super().extra_restore_state_data
        return _AggregateEnergyData(
            self._energy_total, self.native_unit_of_measurement, self._baselines, self._cycle_date
        )

    def _today(self) -> str:
        return dt_util.now(self._timezone()).date().isoformat()

    def _timezone(self) -> tzinfo:
        if self.hass is not None:
            return dt_util.get_time_zone(self.hass.config.time_zone) or dt_util.UTC
        return dt_util.UTC

    def _accumulate_energy(self, nodes: dict[str, Any]) -> None:
        if self._is_daily and self._cycle_date != (today := self._today()):
            self._cycle_date = today
            self._energy_total = 0.0 if self._energy_total is not None else None
        values = {
            barcode: float(value)
            for barcode in self._barcodes
            if (node := nodes.get(barcode)) is not None
            and isinstance(value := node.get(self.entity_description.value_key), (int, float))
        }
        if self._energy_total is None:
            # First ever value: start from the members' current totals.
            self._energy_total = sum(values.values()) if values else None
        else:
            for barcode, value in values.items():
                previous = self._baselines.get(barcode)
                # New members and member resets (e.g. midnight) only rebase.
                if previous is not None and value > previous:
                    self._energy_total += value - previous
        self._baselines = values if self._energy_total is not None else {}

    def _members(self) -> list[str]:
        """Members whose data counts; live values may exclude stale panels."""
        nodes = self.coordinator.data.get("nodes", {})
        present = [b for b in self._barcodes if nodes.get(b) is not None]
        if not self.entity_description.live or self.coordinator.stale_action == STALE_ACTION_HOLD:
            return present
        return [b for b in present if self.coordinator.is_reporting(b)]

    @property
    def available(self) -> bool:
        """Return True when at least one constituent has (current) data."""
        if not self.coordinator.data:
            return False
        if not self.entity_description.live or self.coordinator.stale_action == STALE_ACTION_HOLD:
            return bool(self._members()) or self._restored_native_value
        if self.coordinator.stale_action == STALE_ACTION_ZERO:
            nodes = self.coordinator.data.get("nodes", {})
            return any(nodes.get(b) is not None for b in self._barcodes)
        return bool(self._members())

    @callback
    def _handle_coordinator_update(self) -> None:
        """Handle updated data from the coordinator."""
        nodes = self.coordinator.data.get("nodes", {})

        if self._is_energy:
            self._accumulate_energy(nodes)
            if self._energy_total is not None:
                self._attr_native_value = round(self._energy_total, 2)
                self._restored_native_value = False
            self.async_write_ha_state()
            return

        members = self._members()
        if not members:
            if self.entity_description.live and self.coordinator.stale_action == STALE_ACTION_ZERO:
                # Connected but nothing reporting (e.g. night): zero output.
                self._attr_native_value = 0.0
                self._restored_native_value = False
            elif not self._restored_native_value:
                self._attr_native_value = None
            self.async_write_ha_state()
            return

        if self.entity_description.key == "performance":
            total_power = 0.0
            total_peak_power = 0.0
            # Zero mode counts stale panels as 0 W of their full rating, so
            # performance covers the whole group, not just the reporting subset.
            zero_stale = self.coordinator.stale_action == STALE_ACTION_ZERO
            rated = [b for b in self._barcodes if nodes.get(b) is not None] if zero_stale else members
            reporting = set(members)
            for barcode in rated:
                node_data = nodes[barcode]
                power = node_data.get("power") if barcode in reporting else 0.0
                if power is None:
                    continue

                try:
                    peak_power_value = float(node_data.get(CONF_MODULE_PEAK_POWER, DEFAULT_PEAK_POWER))
                except (TypeError, ValueError):
                    peak_power_value = float(DEFAULT_PEAK_POWER)

                if peak_power_value <= 0:
                    continue

                total_power += max(power, 0.0)
                total_peak_power += peak_power_value

            if total_peak_power > 0:
                self._attr_native_value = round(
                    (total_power / total_peak_power) * 100.0, 2
                )
            else:
                self._attr_native_value = None
            self._restored_native_value = False

            self.async_write_ha_state()
            return

        total: float | None = None
        for barcode in members:
            value = nodes[barcode].get(self.entity_description.value_key)
            if value is not None:
                total = (total or 0.0) + value

        self._attr_native_value = total
        self._restored_native_value = False
        self.async_write_ha_state()

    @property
    def last_reset(self) -> datetime | None:
        """Return last reset for aggregate daily energy (start of its own cycle)."""
        if not self._is_daily:
            return None
        cycle = self._cycle_date or self._today()
        try:
            cycle_day = date.fromisoformat(cycle)
        except ValueError:
            cycle_day = date.fromisoformat(self._today())
        return datetime.combine(cycle_day, time.min, tzinfo=self._timezone())

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Return additional aggregate state attributes."""
        nodes = self.coordinator.data.get("nodes", {})
        reporting = [
            barcode
            for barcode in self._barcodes
            if nodes.get(barcode) is not None and self.coordinator.is_reporting(barcode)
        ]
        return {
            "optimizer_count": len(self._barcodes),
            "reporting_count": len(reporting),
        }
