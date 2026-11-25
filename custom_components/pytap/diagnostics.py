"""Diagnostics support for PyTap."""

from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.const import CONF_HOST
from homeassistant.core import HomeAssistant

from .const import CONF_SERIAL_PORT
from .coordinator import PyTapConfigEntry

TO_REDACT = {CONF_HOST, CONF_SERIAL_PORT, "title"}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant,
    entry: PyTapConfigEntry,
) -> dict[str, Any]:
    """Return diagnostics for a config entry."""
    coordinator = entry.runtime_data

    diagnostics_data = coordinator.get_diagnostics_data()

    node_summaries = {
        barcode: {
            "last_update": node_data.get("last_update"),
            "gateway_id": node_data.get("gateway_id"),
            "node_id": node_data.get("node_id"),
            "reporting": coordinator.is_reporting(barcode),
            "seconds_since_report": (
                round(age, 1) if (age := coordinator.seconds_since_report(barcode)) is not None else None
            ),
            "status_code": node_data.get("status_code"),
            "reading_fault": node_data.get("reading_fault"),
            "fault_reasons": node_data.get("fault_reasons"),
            "last_fault_at": node_data.get("last_fault_at"),
            "daily_energy_wh": node_data.get("daily_energy_wh"),
            "total_energy_wh": node_data.get("total_energy_wh"),
            "readings_today": node_data.get("readings_today"),
        }
        for barcode, node_data in dict(coordinator.data.get("nodes", {})).items()
    }

    return {
        "config_entry": async_redact_data(entry.as_dict(), TO_REDACT),
        "modules": coordinator.modules,
        "counters": coordinator.data.get("counters", {}),
        "gateways": coordinator.data.get("gateways", {}),
        "discovered_barcodes": coordinator.data.get("discovered_barcodes", []),
        "nodes": node_summaries,
        **async_redact_data(diagnostics_data, TO_REDACT),
    }
