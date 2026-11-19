"""Binary sensor platform for PyTap: per-optimizer status flags and reading faults.

The two status flags come from undocumented power-report bits. Their names
describe what captured data shows, not a meaning confirmed by Tigo.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
    BinarySensorEntityDescription,
)
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import CONF_MODULE_BARCODE, DOMAIN, STALE_ACTION_HOLD
from .coordinator import PyTapConfigEntry, PyTapDataUpdateCoordinator
from .entity import module_device_info


@dataclass(frozen=True, kw_only=True)
class PyTapBinarySensorEntityDescription(BinarySensorEntityDescription):
    """Describes a PyTap binary sensor entity."""

    value_key: str
    # Live flags follow the stale-reading action once a panel stops reporting.
    live: bool = True


BINARY_SENSOR_DESCRIPTIONS: tuple[PyTapBinarySensorEntityDescription, ...] = (
    PyTapBinarySensorEntityDescription(
        key="high_output_mode",
        translation_key="high_output_mode",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_key="high_output_mode",
    ),
    PyTapBinarySensorEntityDescription(
        key="low_voltage_flag",
        translation_key="low_voltage_flag",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_key="low_voltage_flag",
    ),
    PyTapBinarySensorEntityDescription(
        key="reading_fault",
        translation_key="reading_fault",
        device_class=BinarySensorDeviceClass.PROBLEM,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_key="reading_fault",
        # Stays set until the next valid reading, even after reports stop.
        live=False,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: PyTapConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up PyTap binary sensors for configured and discovered optimizers."""
    coordinator = entry.runtime_data
    known_barcodes: set[str] = set()

    @callback
    def add_discovered_entities() -> None:
        if len(known_barcodes) == coordinator.module_count:
            return
        entities: list[PyTapBinarySensor] = []
        for module_config in coordinator.modules:
            barcode = module_config.get(CONF_MODULE_BARCODE, "")
            if not barcode or barcode in known_barcodes:
                continue
            known_barcodes.add(barcode)
            entities.extend(
                PyTapBinarySensor(coordinator, description, module_config)
                for description in BINARY_SENSOR_DESCRIPTIONS
            )
        if entities:
            async_add_entities(entities)

    entry.async_on_unload(coordinator.async_add_listener(add_discovered_entities))
    add_discovered_entities()


class PyTapBinarySensor(CoordinatorEntity[PyTapDataUpdateCoordinator], BinarySensorEntity):
    """A per-optimizer status flag or fault indicator.

    State is only written when it changes, so the recorder doesn't store a
    row for every power report.
    """

    _attr_has_entity_name = True
    entity_description: PyTapBinarySensorEntityDescription

    def __init__(
        self,
        coordinator: PyTapDataUpdateCoordinator,
        description: PyTapBinarySensorEntityDescription,
        module_config: dict[str, Any],
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        self._barcode = module_config[CONF_MODULE_BARCODE]
        self._attr_unique_id = f"{DOMAIN}_{self._barcode}_{description.key}"
        self._attr_device_info = module_device_info(module_config)
        self._last_written: tuple[Any, ...] | None = None

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(
            self.coordinator.async_add_data_listener([self._barcode], self._handle_coordinator_update)
        )
        if self.entity_description.live:
            self.async_on_remove(
                self.coordinator.async_add_freshness_listener(
                    [self._barcode], self._handle_coordinator_update
                )
            )

    def _node(self) -> dict[str, Any] | None:
        return (self.coordinator.data or {}).get("nodes", {}).get(self._barcode)

    @property
    def available(self) -> bool:
        if self._node() is None:
            return False
        if not self.entity_description.live or self.coordinator.is_reporting(self._barcode):
            return True
        # Flags have no meaningful "zero"; only holding keeps them visible.
        return self.coordinator.stale_action == STALE_ACTION_HOLD

    @property
    def is_on(self) -> bool | None:
        node = self._node()
        value = node.get(self.entity_description.value_key) if node else None
        return None if value is None else bool(value)

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        if self.entity_description.key != "reading_fault" or (node := self._node()) is None:
            return None
        return {
            "fault_reasons": list(node.get("fault_reasons") or []),
            "last_fault_at": node.get("last_fault_at"),
        }

    @callback
    def _handle_coordinator_update(self) -> None:
        written = (self.available, self.is_on, self.extra_state_attributes)
        if written == self._last_written:
            return
        self._last_written = written
        self.async_write_ha_state()
