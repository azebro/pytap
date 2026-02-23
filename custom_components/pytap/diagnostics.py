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
    hass: HomeAssistant,
    entry: ConfigEntry,
) -> dict[str, Any]:
    """Return diagnostics for a config entry."""
    coordinator: PyTapDataUpdateCoordinator = hass.data[DOMAIN][entry.entry_id]

    diagnostics_data = coordinator.get_diagnostics_data()

    node_summaries = {
        barcode: {
            "last_update": node_data.get("last_update"),
            "gateway_id": node_data.get("gateway_id"),
            "node_id": node_data.get("node_id"),
            "daily_energy_wh": node_data.get("daily_energy_wh"),
            "total_energy_wh": node_data.get("total_energy_wh"),
            "readings_today": node_data.get("readings_today"),
        }
        for barcode, node_data in coordinator.data.get("nodes", {}).items()
    }

    return {
        "config_entry": async_redact_data(entry.as_dict(), TO_REDACT),
        "counters": coordinator.data.get("counters", {}),
        "gateways": coordinator.data.get("gateways", {}),
        "discovered_barcodes": coordinator.data.get("discovered_barcodes", []),
        "nodes": node_summaries,
        **async_redact_data(diagnostics_data, TO_REDACT),
    }
