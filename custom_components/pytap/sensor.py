"""Sensor platform for PyTap integration.

Creates sensor entities for configured and passively discovered optimizers.
Entities keep stable barcode-based identifiers across discovery and restarts.
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
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.restore_state import ExtraStoredData
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util

from .const import (
    CONF_MODULE_BARCODE,
    CONF_MODULE_PEAK_POWER,
    CONF_MODULE_STRING,
    DEFAULT_PEAK_POWER,
    DOMAIN,
    STALE_ACTION_HOLD,
    STALE_ACTION_ZERO,
)
from .coordinator import PyTapConfigEntry, PyTapDataUpdateCoordinator
from .entity import module_device_info

_LOGGER = logging.getLogger(__name__)

# Electrical readings that report 0 when the stale action is "zero". Others
# (temperature, duty cycle, RSSI, status) have no meaningful zero and go
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
    # Skip state writes when nothing changed (avoids a recorder row per report).
    write_on_change: bool = False


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
    PyTapSensorEntityDescription(
        key="status_code",
        translation_key="status_code",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_key="status_code",
        write_on_change=True,
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
    Each configured module gets the full set of 13 sensor entities.
    """
    coordinator = entry.runtime_data
    known_barcodes: set[str] = set()
    string_sensors: dict[str, list[PyTapAggregateSensor]] = {}
    installation_sensors: list[PyTapAggregateSensor] = []
    installation_device = DeviceInfo(
        identifiers={(DOMAIN, f"{entry.entry_id}_installation")},
        name="Tigo Installation",
        manufacturer="Tigo Energy",
        model="Installation Aggregate",
    )

    @callback
    def add_discovered_entities() -> None:
        """Called on HA's loop: add new optimizer entities exactly once."""
        if len(known_barcodes) == coordinator.module_count:
            return
        entities: list[SensorEntity] = []
        string_to_barcodes: dict[str, list[str]] = {}
        all_barcodes: list[str] = []
        for module_config in coordinator.modules:
            barcode = module_config.get(CONF_MODULE_BARCODE, "")
            if not barcode:
                continue
            all_barcodes.append(barcode)
            if string_name := module_config.get(CONF_MODULE_STRING, ""):
                string_to_barcodes.setdefault(string_name, []).append(barcode)
            if barcode in known_barcodes:
                continue
            known_barcodes.add(barcode)
            for description in SENSOR_DESCRIPTIONS:
                entities.append(PyTapSensor(coordinator, description, module_config))

        for string_name, barcodes in string_to_barcodes.items():
            if string_name not in string_sensors:
                device_info = DeviceInfo(
                    identifiers={(DOMAIN, f"{entry.entry_id}_string_{string_name}")},
                    name=f"Tigo String {string_name}",
                    manufacturer="Tigo Energy",
                    model="String Aggregate",
                )
                string_sensors[string_name] = [
                    PyTapAggregateSensor(
                        coordinator,
                        description,
                        barcodes,
                        device_info,
                        f"{DOMAIN}_{entry.entry_id}_string_{string_name}_{description.key}",
                    )
                    for description in STRING_SENSOR_DESCRIPTIONS
                ]
                entities.extend(string_sensors[string_name])
            for sensor in string_sensors[string_name]:
                sensor.set_barcodes(barcodes)

        if all_barcodes and not installation_sensors:
            installation_sensors.extend(
                PyTapAggregateSensor(
                    coordinator,
                    description,
                    all_barcodes,
                    installation_device,
                    f"{DOMAIN}_{entry.entry_id}_installation_{description.key}",
                )
                for description in INSTALLATION_SENSOR_DESCRIPTIONS
            )
            entities.extend(installation_sensors)
        for sensor in installation_sensors:
            sensor.set_barcodes(all_barcodes)
        if entities:
            async_add_entities(entities)
        _sync_performance_entities(hass, entry.entry_id, coordinator, string_to_barcodes, all_barcodes)

    # Register before creating entities so aggregate membership is refreshed
    # before their own coordinator callbacks consume each discovery update.
    entry.async_on_unload(coordinator.async_add_listener(add_discovered_entities))
    add_discovered_entities()
    if coordinator.auto_discovery:
        async_add_entities([PyTapDiscoverySensor(coordinator, entry, installation_device)])


def _performance_unique_ids(
    entry_id: str,
    coordinator: PyTapDataUpdateCoordinator,
    string_to_barcodes: dict[str, list[str]],
    all_barcodes: list[str],
) -> dict[str, bool]:
    """Map each performance entity's unique ID to whether it has ratings to work from."""

    def rated(barcodes: list[str]) -> bool:
        return all(coordinator.module_peak_power(barcode) > 0 for barcode in barcodes)

    wanted = {f"{DOMAIN}_{barcode}_performance": rated([barcode]) for barcode in all_barcodes}
    for string_name, barcodes in string_to_barcodes.items():
        wanted[f"{DOMAIN}_{entry_id}_string_{string_name}_performance"] = rated(barcodes)
    if all_barcodes:
        wanted[f"{DOMAIN}_{entry_id}_installation_performance"] = rated(all_barcodes)
    return wanted


@callback
def _sync_performance_entities(
    hass: HomeAssistant,
    entry_id: str,
    coordinator: PyTapDataUpdateCoordinator,
    string_to_barcodes: dict[str, list[str]],
    all_barcodes: list[str],
) -> None:
    """Disable performance entities without a panel rating; re-enable them once rated.

    Only entities that are enabled, or that this integration disabled, are
    changed; a user's own choice to disable an entity is left alone.
    """
    registry = er.async_get(hass)
    for unique_id, has_rating in _performance_unique_ids(
        entry_id, coordinator, string_to_barcodes, all_barcodes
    ).items():
        if (entity_id := registry.async_get_entity_id("sensor", DOMAIN, unique_id)) is None:
            continue
        disabled_by = registry.entities[entity_id].disabled_by
        if not has_rating and disabled_by is None:
            registry.async_update_entity(entity_id, disabled_by=er.RegistryEntryDisabler.INTEGRATION)
        elif has_rating and disabled_by is er.RegistryEntryDisabler.INTEGRATION:
            registry.async_update_entity(entity_id, disabled_by=None)


class PyTapDiscoverySensor(CoordinatorEntity[PyTapDataUpdateCoordinator], SensorEntity):
    """Show why an empty automatic setup has no optimizer sensors yet."""

    _attr_has_entity_name = True
    _attr_translation_key = "module_discovery"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = ["waiting_for_modules", "modules_discovered"]

    def __init__(
        self, coordinator: PyTapDataUpdateCoordinator, entry: ConfigEntry, device_info: DeviceInfo
    ) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{DOMAIN}_{entry.entry_id}_module_discovery"
        self._attr_device_info = device_info

    @property
    def native_value(self) -> str:
        return "modules_discovered" if self.coordinator.module_count else "waiting_for_modules"

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {"module_count": self.coordinator.module_count}


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
        module_config: dict[str, Any],
    ) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator)
        self.entity_description = description

        self._barcode = module_config[CONF_MODULE_BARCODE]
        self._module_string = module_config.get(CONF_MODULE_STRING, "")
        self._module_peak_power = coordinator.module_peak_power(self._barcode)

        # Unique ID: domain + barcode + sensor key
        self._attr_unique_id = f"{DOMAIN}_{self._barcode}_{description.key}"
        self._attr_device_info = module_device_info(module_config)
        if description.key == "performance" and self._module_peak_power <= 0:
            # No panel rating: nothing to compute until the user sets one.
            self._attr_entity_registry_enabled_default = False
        self._restored_native_value = False
        self._last_written: tuple[Any, ...] | None = None

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
        if self.entity_description.key == "performance" and self._module_peak_power <= 0:
            # A percentage from an old rating must not masquerade as a current
            # value after the user clears the rating, even without a snapshot.
            self._attr_native_value = None
            return
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
                # Performance stays unknown when the panel rating is unknown.
                value = None if key == "performance" and self._module_peak_power <= 0 else 0.0
            elif key == "dc_dc_duty_cycle" and value is not None:
                # Convert duty cycle from 0.0-1.0 to percentage
                value = round(value * 100, 2)
            elif key == "status_code" and value is not None:
                value = f"0x{value:02X}"
            self._attr_native_value = value
            self._restored_native_value = False
        else:
            if not self._restored_native_value:
                self._attr_native_value = None
        if self.entity_description.write_on_change:
            # Only record a state when it actually changes, not every report.
            written = (self.available, self._attr_native_value, self.extra_state_attributes)
            if written == self._last_written:
                return
            self._last_written = written
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
        if last_update and not self.entity_description.write_on_change:
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
        hass = self.hass or self.coordinator.hass
        return dt_util.get_time_zone(hass.config.time_zone) or dt_util.UTC


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

    Live values sum current member readings. Energy totals accumulate each
    member's production since its saved baseline; new members start at their
    first reading. A decrease below 50% of its baseline is treated as a reset
    and contributes the post-reset value; a smaller decrease holds the old
    baseline, so recovery below it is not counted and the first reading above
    it contributes only the excess. Held baselines survive restores. Risks:
    a small genuine reset can be held, while a large restore loss or a skew
    near a tiny baseline can be treated as a reset and overcount; exact #39
    rollback from a held baseline can re-credit at most the held gap.
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
        if description.key == "performance" and any(
            coordinator.module_peak_power(barcode) <= 0 for barcode in barcodes
        ):
            self._attr_entity_registry_enabled_default = False
        self._restored_native_value = False
        self._energy_total: float | None = None
        self._baselines: dict[str, float] = {}
        self._cycle_date: str | None = None
        self._member_unsubs: list[CALLBACK_TYPE] = []

    @property
    def _is_energy(self) -> bool:
        return self.entity_description.key in ("daily_energy", "total_energy")

    @property
    def _is_daily(self) -> bool:
        return self.entity_description.key == "daily_energy"

    def set_barcodes(self, barcodes: list[str]) -> None:
        """Update aggregate membership without recreating its stable entity."""
        changed = set(barcodes) != set(self._barcodes)
        self._barcodes = list(barcodes)
        if changed:
            members = set(self._barcodes)
            self._baselines = {
                barcode: value for barcode, value in self._baselines.items() if barcode in members
            }
        if changed and self._member_unsubs:
            # Follow membership so new members' changes re-render this.
            self._subscribe_members()

    @callback
    def _subscribe_members(self) -> None:
        """Re-render on members' data and (every aggregate reports reporting_count) freshness."""
        self._unsubscribe_members()
        self._member_unsubs = [
            self.coordinator.async_add_data_listener(self._barcodes, self._handle_coordinator_update),
            self.coordinator.async_add_freshness_listener(self._barcodes, self._handle_coordinator_update),
        ]

    @callback
    def _unsubscribe_members(self) -> None:
        while self._member_unsubs:
            self._member_unsubs.pop()()

    async def async_added_to_hass(self) -> None:
        """Restore last known aggregate value from Home Assistant state cache."""
        await super().async_added_to_hass()
        self._subscribe_members()
        self.async_on_remove(self._unsubscribe_members)
        if self._is_energy:
            await self._async_restore_energy()
            if any(
                (node := self.coordinator.data.get("nodes", {}).get(barcode)) is not None
                and node.get(self.entity_description.value_key) is not None
                for barcode in self._barcodes
            ):
                self._handle_coordinator_update()
            return

        if self.entity_description.key == "performance" and any(
            self.coordinator.module_peak_power(barcode) <= 0 for barcode in self._barcodes
        ):
            self._attr_native_value = None
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
        members = set(self._barcodes)
        self._baselines = {
            barcode: value for barcode, value in self._baselines.items() if barcode in members
        }
        if self._is_daily:
            today = self._today()
            if self._cycle_date != today:
                self._cycle_date = today
                self._energy_total = 0.0 if self._energy_total is not None else None
                self._baselines = {barcode: 0.0 for barcode in self._baselines}
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
        hass = self.hass or self.coordinator.hass
        return dt_util.get_time_zone(hass.config.time_zone) or dt_util.UTC

    def _accumulate_energy(self, nodes: dict[str, Any]) -> None:
        today = self._today() if self._is_daily else None
        cycle_changed = self._is_daily and self._cycle_date != today
        if cycle_changed:
            self._cycle_date = today
            self._energy_total = 0.0 if self._energy_total is not None else None

        members = set(self._barcodes)
        self._baselines = {
            barcode: value for barcode, value in self._baselines.items() if barcode in members
        }
        if cycle_changed:
            # Reset existing members; missing baselines still identify new members.
            self._baselines = {barcode: 0.0 for barcode in self._baselines}

        stale_members: set[str] = set()
        if self._is_daily:
            stale_members = {
                barcode
                for barcode in self._barcodes
                if (node := nodes.get(barcode)) is not None
                and node.get("daily_reset_date") != today
            }
            self._baselines.update(
                {
                    barcode: 0.0
                    for barcode in stale_members
                    if self._energy_total is None or barcode in self._baselines
                }
            )

        values = {
            barcode: float(value)
            for barcode in self._barcodes
            if (node := nodes.get(barcode)) is not None
            and (not self._is_daily or node.get("daily_reset_date") == today)
            and isinstance(value := node.get(self.entity_description.value_key), (int, float))
            and value >= 0
        }
        if self._energy_total is None:
            # First ever value: start from the members' current totals.
            self._energy_total = sum(values.values()) if values else None
            if self._energy_total is not None:
                self._baselines.update(values)
        else:
            for barcode, value in values.items():
                previous = self._baselines.get(barcode)
                if previous is None:
                    self._baselines[barcode] = value
                    continue
                if value >= previous:
                    self._energy_total += value - previous
                    self._baselines[barcode] = value
                elif value >= 0.5 * previous:
                    continue
                else:
                    self._energy_total += value
                    self._baselines[barcode] = value
        if self._energy_total is None and not self._is_daily:
            self._baselines = {}

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

    def _unrated(self) -> bool:
        return any(self.coordinator.module_peak_power(barcode) <= 0 for barcode in self._barcodes)

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
                unknown = self.entity_description.key == "performance" and self._unrated()
                self._attr_native_value = None if unknown else 0.0
                self._restored_native_value = False
            elif not self._restored_native_value:
                self._attr_native_value = None
            self.async_write_ha_state()
            return

        if self.entity_description.key == "performance":
            if self._unrated():
                # Reporting a percentage for only the rated subset would
                # misrepresent the entire group/installation performance.
                self._attr_native_value = None
                self._restored_native_value = False
                self.async_write_ha_state()
                return
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
