"""DataUpdateCoordinator for the PyTap integration.

Bridges the blocking pytap parser library into Home Assistant's async event loop.
Uses a background executor thread for streaming data from the Tigo gateway,
and filters events by user-configured barcodes.
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST, CONF_PORT
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import (
    CONF_MODULE_BARCODE,
    CONF_MODULE_NAME,
    CONF_MODULE_STRING,
    CONF_MODULES,
    DEFAULT_PORT,
    DOMAIN,
    RECONNECT_DELAY,
    RECONNECT_RETRIES,
    RECONNECT_TIMEOUT,
    UNAVAILABLE_TIMEOUT,
)
from .pytap.core.events import (
    Event,
    InfrastructureEvent,
    PowerReportEvent,
    StringEvent,
    TopologyEvent,
)

_LOGGER = logging.getLogger(__name__)


class PyTapDataUpdateCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Manage streaming data from the Tigo gateway via pytap parser.

    Runs a background listener thread that reads from the TCP/serial source,
    feeds bytes into the parser, and dispatches parsed events to the HA event loop.
    Only events matching user-configured barcodes are stored in coordinator data.
    """

    config_entry: ConfigEntry

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        """Initialize the coordinator."""
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            config_entry=entry,
        )
        self._host: str = entry.data[CONF_HOST]
        self._port: int = entry.data.get(CONF_PORT, DEFAULT_PORT)
        self._modules: list[dict[str, str]] = entry.data.get(CONF_MODULES, [])

        # Build barcode allowlist from configured modules
        self._configured_barcodes: set[str] = {
            m[CONF_MODULE_BARCODE] for m in self._modules if m.get(CONF_MODULE_BARCODE)
        }
        # Module lookup by barcode for name/string metadata
        self._module_lookup: dict[str, dict[str, str]] = {
            m[CONF_MODULE_BARCODE]: m
            for m in self._modules
            if m.get(CONF_MODULE_BARCODE)
        }

        # Barcode ↔ node_id mapping learned from InfrastructureEvents
        self._barcode_to_node: dict[str, int] = {}
        self._node_to_barcode: dict[int, str] = {}

        # Track barcodes seen on the bus but not in user config
        self._discovered_barcodes: set[str] = set()

        # Listener task handle
        self._listener_task: asyncio.Task | None = None
        self._stop_event = asyncio.Event()

        # Source handle for cancellation
        self._source: Any = None

        # Initialize data structure
        self.data: dict[str, Any] = {
            "gateways": {},
            "nodes": {},
            "counters": {},
            "discovered_barcodes": [],
        }

    async def _async_update_data(self) -> dict[str, Any]:
        """Return current data (push-based, no polling needed)."""
        return self.data

    async def async_start_listener(self) -> None:
        """Start the background listener task."""
        self._stop_event.clear()
        self._listener_task = self.config_entry.async_create_background_task(
            self.hass,
            self._async_listen(),
            name="pytap_listener",
        )

    async def _async_listen(self) -> None:
        """Async wrapper to run the blocking listener in an executor."""
        await self.hass.async_add_executor_job(self._listen)

    async def async_stop_listener(self) -> None:
        """Stop the background listener task."""
        self._stop_event.set()
        # Close source to unblock the read() call
        if self._source is not None:
            try:
                self._source.close()
            except Exception:
                pass
        if self._listener_task is not None:
            self._listener_task.cancel()
            try:
                await self._listener_task
            except (asyncio.CancelledError, Exception):
                pass
            self._listener_task = None

    def _listen(self) -> None:
        """Blocking listener loop (runs in executor thread).

        Connects to the Tigo gateway, reads bytes, feeds them into the
        parser, and dispatches events back to the HA event loop.
        """
        from .pytap.api import connect, create_parser

        retries = 0

        while not self._stop_event.is_set():
            parser = create_parser()
            self._source = None

            try:
                source_config = {"tcp": self._host, "port": self._port}
                self._source = connect(source_config)
                _LOGGER.info(
                    "Connected to Tigo gateway at %s:%s", self._host, self._port
                )
                retries = 0
                last_data_time = time.monotonic()

                while not self._stop_event.is_set():
                    data = self._source.read(4096)
                    if data:
                        last_data_time = time.monotonic()
                        events = parser.feed(data)
                        for event in events:
                            self._process_event(event)
                        # Update counters
                        self.data["counters"] = parser.counters
                        # Push updated data to HA event loop
                        self.hass.loop.call_soon_threadsafe(
                            self.async_set_updated_data, dict(self.data)
                        )
                    elif (
                        RECONNECT_TIMEOUT > 0
                        and (time.monotonic() - last_data_time) > RECONNECT_TIMEOUT
                    ):
                        _LOGGER.warning(
                            "No data from gateway for %ds, reconnecting",
                            RECONNECT_TIMEOUT,
                        )
                        break

            except Exception as err:
                _LOGGER.error("Connection error: %s", err)
            finally:
                if self._source is not None:
                    try:
                        self._source.close()
                    except Exception:
                        pass
                    self._source = None

            if self._stop_event.is_set():
                break

            retries += 1
            if RECONNECT_RETRIES > 0 and retries > RECONNECT_RETRIES:
                _LOGGER.error("Max retries (%d) exceeded", RECONNECT_RETRIES)
                return

            _LOGGER.info(
                "Reconnecting in %ds (attempt %d/%s)...",
                RECONNECT_DELAY,
                retries,
                str(RECONNECT_RETRIES) if RECONNECT_RETRIES else "∞",
            )
            # Sleep in small increments so we can respond to stop quickly
            for _ in range(RECONNECT_DELAY * 10):
                if self._stop_event.is_set():
                    return
                time.sleep(0.1)

    def _process_event(self, event: Event) -> None:
        """Process a parsed event, filtering by configured barcodes."""
        if isinstance(event, PowerReportEvent):
            self._handle_power_report(event)
        elif isinstance(event, InfrastructureEvent):
            self._handle_infrastructure(event)
        elif isinstance(event, TopologyEvent):
            self._handle_topology(event)
        elif isinstance(event, StringEvent):
            _LOGGER.debug(
                "String event (gw=%d, node=%d, %s): %s",
                event.gateway_id,
                event.node_id,
                event.direction,
                event.content,
            )

    def _handle_power_report(self, event: PowerReportEvent) -> None:
        """Handle a power report event."""
        barcode = event.barcode

        # Try to resolve barcode from node_id if not directly available
        if not barcode and event.node_id in self._node_to_barcode:
            barcode = self._node_to_barcode[event.node_id]

        if not barcode:
            _LOGGER.debug(
                "Power report for node %d with no barcode yet (gateway=%d)",
                event.node_id,
                event.gateway_id,
            )
            return

        # Check if this barcode is in our configured allowlist
        if barcode not in self._configured_barcodes:
            if barcode not in self._discovered_barcodes:
                self._discovered_barcodes.add(barcode)
                self.data["discovered_barcodes"] = sorted(self._discovered_barcodes)
                _LOGGER.info(
                    "Discovered unconfigured Tigo optimizer barcode: %s "
                    "(gateway=%d, node=%d). Add it to your PyTap module "
                    "list to start tracking.",
                    barcode,
                    event.gateway_id,
                    event.node_id,
                )
            return

        # Get module metadata
        module_meta = self._module_lookup.get(barcode, {})

        self.data["nodes"][barcode] = {
            "gateway_id": event.gateway_id,
            "node_id": event.node_id,
            "barcode": barcode,
            "name": module_meta.get(CONF_MODULE_NAME, barcode),
            "string": module_meta.get(CONF_MODULE_STRING, ""),
            "voltage_in": event.voltage_in,
            "voltage_out": event.voltage_out,
            "current": event.current,
            "power": event.power,
            "temperature": event.temperature,
            "dc_dc_duty_cycle": event.dc_dc_duty_cycle,
            "rssi": event.rssi,
            "last_update": datetime.now().isoformat(),
        }

    def _handle_infrastructure(self, event: InfrastructureEvent) -> None:
        """Handle an infrastructure event — update gateway/node mappings."""
        # Update gateways
        self.data["gateways"] = event.gateways

        # Update barcode ↔ node_id mapping from node table
        for node_id, node_info in event.nodes.items():
            barcode = node_info.get("barcode")
            if barcode:
                self._barcode_to_node[barcode] = node_id
                self._node_to_barcode[node_id] = barcode

                # Log discovery of unconfigured barcodes
                if barcode not in self._configured_barcodes:
                    if barcode not in self._discovered_barcodes:
                        self._discovered_barcodes.add(barcode)
                        self.data["discovered_barcodes"] = sorted(
                            self._discovered_barcodes
                        )
                        _LOGGER.info(
                            "Discovered unconfigured Tigo optimizer barcode: %s "
                            "(node=%d). Add it to your PyTap module list to "
                            "start tracking.",
                            barcode,
                            node_id,
                        )

    def _handle_topology(self, event: TopologyEvent) -> None:
        """Handle a topology event for matched nodes."""
        barcode = self._node_to_barcode.get(event.node_id)
        if barcode and barcode in self._configured_barcodes:
            node_data = self.data["nodes"].get(barcode)
            if node_data:
                node_data["topology"] = event.to_dict()

    def reload_modules(self, modules: list[dict[str, str]]) -> None:
        """Reload the module configuration (called from options flow)."""
        self._modules = modules
        self._configured_barcodes = {
            m[CONF_MODULE_BARCODE] for m in modules if m.get(CONF_MODULE_BARCODE)
        }
        self._module_lookup = {
            m[CONF_MODULE_BARCODE]: m for m in modules if m.get(CONF_MODULE_BARCODE)
        }
        _LOGGER.info(
            "Reloaded module config: tracking %d barcodes",
            len(self._configured_barcodes),
        )
