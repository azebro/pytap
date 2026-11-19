"""Supported-Core smoke tests without private custom-component harness fixtures.

Run in a separate environment containing HA 2026.11.0, serialx 1.15.1,
aiousbwatcher 1.1.2, and aiohasupervisor 0.8.1 (see the supported-core CI job):
    python -m unittest discover -s tests -p core_smoke.py -v
"""

import asyncio
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from types import MappingProxyType
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from homeassistant.components.usb.serial_proxy_stub import register_serialx_transport
from homeassistant.config_entries import ConfigEntries, ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers.selector import SerialPortSelector

from custom_components.pytap import async_migrate_entry
from custom_components.pytap.config_flow import PyTapConfigFlow, PyTapOptionsFlow
from custom_components.pytap.connection import open_source
from custom_components.pytap.coordinator import PyTapDataUpdateCoordinator
from custom_components.pytap.pytap.core.capture import iter_capture
from custom_components.pytap.pytap.core.events import InfrastructureEvent

ENDPOINT = "esphome-hass://esphome/entry-one?port_name=Tigo%20Bus"
MODULE = {"string": "A", "name": "Panel", "barcode": "A-1234567B", "peak_power": 455}


class CoreSmoke(unittest.IsolatedAsyncioTestCase):
    """Exercise native Core config entries, schemas, and executor bridging."""

    async def asyncSetUp(self):
        self.directory = TemporaryDirectory(prefix="pytap-core-smoke-")
        self.hass = HomeAssistant(str(Path(self.directory.name).resolve()))
        self.hass.config_entries = ConfigEntries(self.hass, {})

    async def asyncTearDown(self):
        await self.hass.async_stop(force=True)
        self.directory.cleanup()

    def entry(self, version=6):
        entry = ConfigEntry(
            domain="pytap",
            version=version,
            minor_version=1,
            title="PyTap (gateway)",
            unique_id="stable-id",
            source="user",
            data={"host": "gateway", "port": 502, "modules": [MODULE]},
            options={},
            discovery_keys=MappingProxyType({}),
            subentries_data=[],
        )
        # Register without automatically starting dependency setup/network IO.
        self.hass.config_entries._entries[entry.entry_id] = entry
        return entry

    async def test_native_config_flow_serial_picker_and_entry(self):
        flow = PyTapConfigFlow()
        flow.hass = self.hass
        flow.context = {"source": "user"}
        result = await flow.async_step_user()
        self.assertEqual(result["menu_options"], ["tcp", "serial"])
        result = await flow.async_step_serial()
        schema = {str(k): v for k, v in result["data_schema"].schema.items()}
        self.assertIsInstance(schema["serial_port"], SerialPortSelector)
        result["data_schema"]({"serial_port": ENDPOINT})
        with patch("custom_components.pytap.config_flow.validate_connection", new=AsyncMock()):
            result = await flow.async_step_serial({"serial_port": ENDPOINT})
        self.assertEqual(result["step_id"], "modules_menu")
        await flow.async_step_add_module(MODULE)
        result = await flow.async_step_finish()
        self.assertEqual(result["data"]["serial_port"], ENDPOINT)
        self.assertEqual(result["data"]["modules"], [MODULE])

    async def test_empty_auto_setup_and_discovery_on_supported_core(self):
        flow = PyTapConfigFlow()
        flow.hass = self.hass
        flow.context = {"source": "user"}
        with patch("custom_components.pytap.config_flow.validate_connection", new=AsyncMock()):
            await flow.async_step_tcp({"host": "gateway"})
        result = await flow.async_step_finish()
        self.assertTrue(result["data"]["auto_discovery"])
        self.assertEqual(result["data"]["modules"], [])
        entry = self.entry(version=7)
        self.hass.config_entries.async_update_entry(entry, data=result["data"])
        coordinator = PyTapDataUpdateCoordinator(self.hass, entry)
        coordinator._schedule_save = MagicMock()
        coordinator._process_event(
            InfrastructureEvent(
                gateways={},
                nodes={1: {"barcode": "A-1234567B"}},
                timestamp=entry.created_at,
            )
        )
        self.assertEqual(coordinator.module_count, 1)
        self.assertEqual(coordinator.modules[0]["peak_power"], 0)
        self.assertEqual(coordinator.modules[0]["name"], "Optimizer A-1234567B")
        await coordinator.async_stop_listener()

    async def test_native_migration_and_options_preserve_entry(self):
        entry = self.entry(version=5)
        entry_id = entry.entry_id
        self.assertTrue(await async_migrate_entry(self.hass, entry))
        self.assertEqual(entry.version, 7)
        self.assertEqual(entry.data["connection_type"], "tcp")
        flow = PyTapOptionsFlow(entry)
        flow.hass = self.hass
        with patch("custom_components.pytap.config_flow.validate_connection", new=AsyncMock()):
            result = await flow.async_step_change_serial({"serial_port": ENDPOINT})
        self.assertEqual(result["step_id"], "init")
        self.assertEqual(entry.data["serial_port"], ENDPOINT)
        self.assertNotIn("host", entry.data)
        self.assertEqual(entry.entry_id, entry_id)
        self.assertEqual(entry.unique_id, "stable-id")
        self.assertEqual(entry.data["modules"], [MODULE])
        coordinator = PyTapDataUpdateCoordinator(self.hass, entry)
        self.assertEqual(coordinator.get_diagnostics_data()["connection_state"]["connection_type"], "serial")
        self.assertTrue(await coordinator.async_stop_listener())

    async def test_async_transport_bridge_on_supported_core(self):
        port = MagicMock()
        port.open = AsyncMock()
        port.read = AsyncMock(return_value=b"\x00\xfftigo")
        port.close = AsyncMock()
        with patch("custom_components.pytap.connection.serialx.async_serial_for_url", return_value=port):
            settings = {"connection_type": "serial", "serial_port": ENDPOINT}
            source = await self.hass.async_add_executor_job(open_source, self.hass.loop, settings)
            self.assertEqual(await self.hass.async_add_executor_job(source.read, 4096), b"\x00\xfftigo")
            await self.hass.async_add_executor_job(source.close)
            port.write.assert_not_called()
            port.close.assert_awaited_once()
        await asyncio.sleep(0)

    async def test_native_proxy_stub_is_retryable_and_cleans_up(self):
        unregister = register_serialx_transport()
        try:
            settings = {"connection_type": "serial", "serial_port": ENDPOINT}
            with self.assertRaises(ConfigEntryNotReady):
                await self.hass.async_add_executor_job(open_source, self.hass.loop, settings)
        finally:
            unregister(None)

    @unittest.skipUnless(hasattr(os, "openpty"), "Pseudo-terminals require POSIX")
    async def test_real_serial_transport_releases_virtual_uart(self):
        master, slave = os.openpty()
        try:
            settings = {"connection_type": "serial", "serial_port": os.ttyname(slave)}
            source = await self.hass.async_add_executor_job(open_source, self.hass.loop, settings)
            try:
                os.write(master, b"\x00\xfftigo")
                self.assertEqual(await self.hass.async_add_executor_job(source.read, 4096), b"\x00\xfftigo")
            finally:
                await self.hass.async_add_executor_job(source.close)
            source = await self.hass.async_add_executor_job(open_source, self.hass.loop, settings)
            await self.hass.async_add_executor_job(source.close)
        finally:
            os.close(master)
            os.close(slave)

    async def test_capture_lifecycle_on_supported_core(self):
        entry = self.entry(version=7)
        coordinator = PyTapDataUpdateCoordinator(self.hass, entry)
        started = await coordinator.async_start_capture(duration_minutes=1, max_storage_mib=16)
        self.assertEqual(started["state"], "capturing")
        coordinator._capture.record(b"unchanged\x00\xff", 1)
        stopped = await coordinator.async_stop_capture()
        self.assertTrue(stopped["complete"])
        chunks = list(iter_capture(Path(stopped["directory"])))
        self.assertEqual(b"".join(c.data for c in chunks), b"unchanged\x00\xff")
        self.assertTrue(await coordinator.async_stop_listener())
