"""Shared helpers for PyTap entities."""

from __future__ import annotations

from typing import Any

from homeassistant.helpers.device_registry import DeviceInfo

from .const import CONF_MODULE_BARCODE, CONF_MODULE_NAME, DOMAIN


def module_device_info(module_config: dict[str, Any]) -> DeviceInfo:
    """Group every entity for one optimizer under the same device."""
    barcode = module_config[CONF_MODULE_BARCODE]
    return DeviceInfo(
        identifiers={(DOMAIN, barcode)},
        name=f"Tigo TS4 {module_config[CONF_MODULE_NAME]}",
        manufacturer="Tigo Energy",
        model="TS4",
        serial_number=barcode,
    )
