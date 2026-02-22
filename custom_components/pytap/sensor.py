"""Sensor platform for PyTap integration.

Creates sensor entities for each user-configured Tigo optimizer module.
Entities are keyed by barcode (stable identifier) and created deterministically
from the configured module list — no auto-discovery.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time
import logging
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    SIGNAL_STRENGTH_DECIBELS_MILLIWATT,
    UnitOfElectricCurrent,
    UnitOfElectricPotential,
    UnitOfEnergy,
    UnitOfPower,
    UnitOfTemperature,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util

from .const import (
    CONF_MODULE_BARCODE,
    CONF_MODULE_NAME,
    CONF_MODULE_STRING,
    CONF_MODULES,
    DOMAIN,
)
from .coordinator import PyTapDataUpdateCoordinator

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, kw_only=True)
class PyTapSensorEntityDescription(SensorEntityDescription):
    """Describes a PyTap sensor entity."""

    value_key: str


SENSOR_DESCRIPTIONS: tuple[PyTapSensorEntityDescription, ...] = (
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
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up PyTap sensors from a config entry.

    Creates sensor entities deterministically from the configured module list.
    Each configured module gets the full set of 10 sensor entities.
    """
    coordinator: PyTapDataUpdateCoordinator = hass.data[DOMAIN][entry.entry_id]
    modules: list[dict[str, str]] = entry.data.get(CONF_MODULES, [])

    entities: list[PyTapSensor] = []
    for module_config in modules:
        barcode = module_config.get(CONF_MODULE_BARCODE, "")
        if not barcode:
            continue
        for description in SENSOR_DESCRIPTIONS:
            entities.append(
                PyTapSensor(
                    coordinator=coordinator,
                    description=description,
                    module_config=module_config,
                    entry=entry,
                )
            )

    async_add_entities(entities)


class PyTapSensor(CoordinatorEntity[PyTapDataUpdateCoordinator], SensorEntity):
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

    @property
    def available(self) -> bool:
        """Return True if the sensor has received data."""
        if not self.coordinator.data:
            return False
        node_data = self.coordinator.data.get("nodes", {}).get(self._barcode)
        return node_data is not None

    @callback
    def _handle_coordinator_update(self) -> None:
        """Handle updated data from the coordinator."""
        node_data = self.coordinator.data.get("nodes", {}).get(self._barcode)
        if node_data:
            value = node_data.get(self.entity_description.value_key)
            # Convert duty cycle from 0.0-1.0 to percentage
            if self.entity_description.key == "dc_dc_duty_cycle" and value is not None:
                value = round(value * 100, 2)
            self._attr_native_value = value
        else:
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
        if self.entity_description.key != "daily_energy":
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

        timezone = dt_util.UTC
        if self.hass is not None:
            timezone = dt_util.get_time_zone(self.hass.config.time_zone)

        return datetime.combine(reset_day, time.min, tzinfo=timezone)
