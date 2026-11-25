"""Supported-Core smoke tests without private custom-component harness fixtures.

Run on HA 2026.10.4 and 2026.11.2 with each Core's official constraints and
the manifest closure prepared by scripts/prepare_ha_tests.py:
    python scripts/run_core_smoke.py
"""

import asyncio
from collections.abc import Callable
from contextlib import ExitStack, asynccontextmanager
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from importlib import metadata
from io import BytesIO
import json
import logging
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tarfile
from tempfile import TemporaryDirectory
import threading
from types import MappingProxyType
import unittest
from unittest.mock import AsyncMock, MagicMock, patch
from urllib.parse import parse_qsl, urlencode, urlsplit
import zoneinfo

from aioesphomeapi import (
    APIVersion,
    DeviceCapabilities,
    DeviceInfo,
    SerialProxyIdentity,
    SerialProxyIdentityFlag,
    SerialProxyIdentitySource,
    SerialProxyInfo,
    SerialProxyPortType,
    SerialProxyRequestResponse,
    SerialProxyStatus,
    UsbDeviceDescriptor,
)
from homeassistant import bootstrap, loader
from homeassistant.components.esphome import serial_proxy as esphome_serial_proxy
from homeassistant.components.usb.serial_proxy_stub import register_serialx_transport
from homeassistant.config_entries import ConfigEntries, ConfigEntry, ConfigEntryState
from homeassistant.const import MAJOR_VERSION, MINOR_VERSION
from homeassistant.core import HomeAssistant
from homeassistant.core_config import async_process_ha_core_config
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.exceptions import ConfigEntryError, ConfigEntryNotReady
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import restore_state
from homeassistant.helpers.selector import SerialPortSelector
from homeassistant.setup import async_setup_component
from homeassistant.util import dt as dt_util
from packaging.requirements import Requirement
from packaging.utils import canonicalize_name
import serialx
from serialx.platforms.serial_esphome import InvalidSettingsError, identity_port_info

from custom_components.pytap import async_setup_entry
from custom_components.pytap.config_flow import PyTapConfigFlow, PyTapOptionsFlow
from custom_components.pytap.connection import InvalidConnectionSettings, normalize_connection, open_source
from custom_components.pytap.coordinator import PyTapDataUpdateCoordinator
from custom_components.pytap.pytap.core.capture import iter_capture
from custom_components.pytap.pytap.core.events import InfrastructureEvent, PowerReportEvent

ENDPOINT = "esphome-hass://esphome/entry-one?port_name=Tigo%20Bus"
MODULE = {"string": "A", "name": "Panel", "barcode": "A-1234567B", "peak_power": 455}
REPOSITORY = Path(__file__).resolve().parents[1]
RUNTIME_ROOT = REPOSITORY / ".core-smoke-runtime"


class _PytapLogCapture(logging.Handler):
    """Capture fully formatted PyTap records without replacing logger methods."""

    def __init__(self) -> None:
        super().__init__(logging.DEBUG)
        self.records: list[logging.LogRecord] = []
        self.messages: list[str] = []
        self.setFormatter(
            logging.Formatter("%(levelname)s:%(name)s:%(message)s")
        )

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)
        self.messages.append(self.format(record))


@asynccontextmanager
async def _capture_pytap_logs():
    logger = logging.getLogger("custom_components.pytap")
    handler = _PytapLogCapture()
    prior_level = logger.level
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG)
    try:
        yield handler
    finally:
        logger.removeHandler(handler)
        logger.setLevel(prior_level)


class _FakeZeroconfManager:
    is_shared = False

    def set_instance(self, _instance):
        return None

    async def async_close(self):
        return None


class _FakeESPHomeClient:
    """API-boundary double used by the real ESPHome manager and serial handler."""

    def __init__(self, identities: dict[int, SerialProxyIdentity]) -> None:
        self.loop = asyncio.get_running_loop()
        self._loop = self.loop
        self.address = "127.0.0.1"
        self.log_name = "smoke-node @ 127.0.0.1"
        self.api_version = APIVersion(1, 18)
        self.is_connected = True
        self.outgoing_connection_target = False
        self.connected_address = self.address
        self.cached_device_has_deep_sleep = False
        self.zeroconf_manager = _FakeZeroconfManager()
        self.identities = identities
        self.device = DeviceInfo(
            name="smoke_node",
            friendly_name="Smoke Node",
            mac_address="AA:BB:CC:DD:EE:FF",
            manufacturer="ESPHome",
            model="Smoke",
            api_encryption_supported=False,
            serial_proxies=[
                SerialProxyInfo(name="CDC Port", port_type=SerialProxyPortType.USB_SERIAL),
                SerialProxyInfo(name="Vendor Port", port_type=SerialProxyPortType.USB_SERIAL),
                SerialProxyInfo(name="Fallback Port", port_type=SerialProxyPortType.TTL),
            ],
        )
        self.claimed: set[int] = set()
        self.data_deliveries = 0
        self._closed_callbacks = []
        self._identity_callbacks = []
        self._data_callbacks = []

    def set_cached_name_if_unset(self, _name):
        return None

    def register_addresses_changed_callback(self, _callback):
        return lambda: None

    async def start_resolve_host(self, **_kwargs):
        return None

    async def start_connection(self):
        return None

    async def finish_connection(self, **_kwargs):
        return None

    async def disconnect(self, **_kwargs):
        return None

    async def device_info(self):
        return self.device

    async def device_info_and_list_entities(self):
        return self.device, [], []

    async def device_capabilities_compat(self, _device_info):
        return DeviceCapabilities(serial_proxies=list(self.device.serial_proxies))

    def subscribe_home_assistant_states_and_services(self, **_kwargs):
        return None

    def subscribe_serial_proxy_identity(self, callback):
        self._identity_callbacks.append(callback)
        for identity in self.identities.values():
            callback(identity)
        return lambda: self._identity_callbacks.remove(callback)

    def subscribe_serial_proxy_data(self, callback):
        self._data_callbacks.append(callback)
        return lambda: self._data_callbacks.remove(callback)

    def add_connection_closed_callback(self, callback):
        self._closed_callbacks.append(callback)
        return lambda: self._closed_callbacks.remove(callback)

    async def serial_proxy_get_identity(self, instance, **_kwargs):
        return self.identities[instance]

    async def serial_proxy_subscribe_await_response(self, instance, **_kwargs):
        self.claimed.add(instance)
        return SerialProxyRequestResponse(instance=instance, status=SerialProxyStatus.OK)

    async def serial_proxy_set_mode_await_response(self, instance, **_kwargs):
        return SerialProxyRequestResponse(instance=instance, status=SerialProxyStatus.OK)

    async def serial_proxy_configure_await_response(self, instance, **_kwargs):
        return SerialProxyRequestResponse(instance=instance, status=SerialProxyStatus.OK)

    def serial_proxy_unsubscribe(self, instance):
        self.claimed.discard(instance)

    async def serial_proxy_write(self, **_kwargs):
        return None

    def emit_data(self, instance, data: bytes):
        from aioesphomeapi import SerialProxyDataReceived

        for callback in tuple(self._data_callbacks):
            self.data_deliveries += 1
            callback(SerialProxyDataReceived(instance=instance, data=data))

    def set_debug(self, _enabled):
        return None

    def subscription_counts(self) -> tuple[int, int, int]:
        return (
            len(self._closed_callbacks),
            len(self._identity_callbacks),
            len(self._data_callbacks),
        )


def _identities() -> dict[int, SerialProxyIdentity]:
    connected = SerialProxyIdentityFlag.CONNECTED
    cdc_usb = {
        "vendor_id": 0x10C4,
        "product_id": 0xEA60,
        "bcd_device": 0x0100,
        "interface_number": 0,
    }
    vendor_usb = {
        "vendor_id": 0x0403,
        "product_id": 0x6001,
        "bcd_device": 0x0600,
        "interface_number": 1,
    }
    if (MAJOR_VERSION, MINOR_VERSION) >= (2026, 11):
        cdc_usb.update(interface_class=0x02, port_number=0)
        vendor_usb.update(interface_class=0xFF, port_number=0)
    return {
        0: SerialProxyIdentity(
            instance=0,
            source=SerialProxyIdentitySource.USB,
            flags=connected,
            manufacturer="Tigo",
            product="CDC Adapter",
            serial_number="CDC123",
            usb=UsbDeviceDescriptor(**cdc_usb),
        ),
        1: SerialProxyIdentity(
            instance=1,
            source=SerialProxyIdentitySource.USB,
            flags=connected,
            manufacturer="Tigo",
            product="Vendor Adapter",
            serial_number="VEN123",
            usb=UsbDeviceDescriptor(**vendor_usb),
        ),
        2: SerialProxyIdentity(
            instance=2,
            source=SerialProxyIdentitySource.CONFIGURED,
            flags=connected,
            manufacturer="Tigo",
            product="Fallback Adapter",
            serial_number="FB123",
        ),
    }


def _proxy_filters(identity: SerialProxyIdentity) -> dict[str, str | int]:
    info = identity_port_info(identity, "")
    result: dict[str, str | int] = {}
    for key, value in (
        ("port_manufacturer", info.manufacturer),
        ("port_product", info.product),
        ("port_serial_number", info.serial_number),
        ("port_usb_vid", info.vid),
        ("port_usb_pid", info.pid),
        ("port_usb_bcd_device", info.bcd_device),
        ("port_usb_interface_num", info.interface_num),
    ):
        if value is not None:
            result[key] = value
    if (udev_id := serialx.udev_serial_by_id_stem(info)) is not None:
        result["port_udev_id"] = udev_id
    return result


def _proxy_url(entry_id: str, **filters) -> str:
    return f"esphome-hass://esphome/{entry_id}?{urlencode(filters)}"


def _url_fixtures(entry_id: str) -> dict[str, tuple[str, int | None]]:
    identities = _identities()
    cdc = identities[0]
    vendor = identities[1]
    fallback = identities[2]
    cdc_filters = _proxy_filters(cdc)
    vendor_filters = _proxy_filters(vendor)
    cdc_udev = cdc_filters["port_udev_id"]
    vendor_udev = str(vendor_filters["port_udev_id"])
    if (MAJOR_VERSION, MINOR_VERSION) == (2026, 10):
        assert not vendor_udev.endswith("-port0")
        vendor_udev += "-port0"
    else:
        assert vendor_udev.endswith("-port0")

    cdc_other = {
        key: value for key, value in cdc_filters.items() if key != "port_udev_id"
    }
    fallback_other = {
        key: value
        for key, value in _proxy_filters(fallback).items()
        if key != "port_udev_id"
    }
    s6_filters = {
        "port_name": "CDC Port",
        **{
            key: (hex(int(value)) if key.startswith("port_usb_") else value)
            for key, value in cdc_other.items()
        },
        "port_udev_id": cdc_udev,
    }
    s2v = _proxy_url(entry_id, port_udev_id=vendor_udev)
    s2v_encoded = s2v.replace("-port0", "%2Dport0")
    assert s2v_encoded != s2v
    assert "%2Dport0" in s2v_encoded
    return {
        "S1c": (_proxy_url(entry_id, port_name="CDC Port"), 0),
        "S1v": (_proxy_url(entry_id, port_name="Vendor Port"), 1),
        "S2c": (_proxy_url(entry_id, port_udev_id=cdc_udev), 0),
        "S2v": (s2v, 1),
        "S2v-encoded": (s2v_encoded, 1),
        "S3c": (
            _proxy_url(
                entry_id,
                port_name="CDC Port",
                port_udev_id=cdc_udev,
            ),
            0,
        ),
        "S3v": (
            _proxy_url(
                entry_id,
                port_name="Vendor Port",
                port_udev_id=vendor_udev,
            ),
            1,
        ),
        "S4": (_proxy_url(entry_id, port_name="Fallback Port", **fallback_other), 2),
        "S5": (_proxy_url(entry_id, port_name="CDC Port", **cdc_other), 0),
        "S6": (_proxy_url(entry_id, **s6_filters), 0),
        "S7": (
            _proxy_url(
                entry_id,
                port_usb_vid=cdc_other["port_usb_vid"],
                port_usb_pid=cdc_other["port_usb_pid"],
                port_serial_number=cdc_other["port_serial_number"],
            ),
            0,
        ),
    }


def _changed_query(endpoint: str, key: str, value: str) -> str:
    parsed = urlsplit(endpoint)
    query = dict(parse_qsl(parsed.query))
    query[key] = value
    return parsed._replace(query=urlencode(query)).geturl()


def _expected_matcher_error(
    endpoint: str, client: _FakeESPHomeClient
) -> str:
    query = dict(parse_qsl(urlsplit(endpoint).query))
    name = query.pop("port_name", None)
    matchers = {
        key: int(query[key], 0)
        if key.startswith("port_usb_")
        else query[key]
        for key in (
            "port_manufacturer",
            "port_product",
            "port_serial_number",
            "port_usb_vid",
            "port_usb_pid",
            "port_usb_bcd_device",
            "port_usb_interface_num",
            "port_udev_id",
        )
        if key in query
    }
    if name is None:
        return f"No serial proxy has a device attached with {matchers!r}"

    name_map = {
        proxy.name: (instance, proxy)
        for instance, proxy in enumerate(client.device.serial_proxies)
    }
    if name not in name_map:
        return f"Serial proxy with name {name!r} does not exist in {name_map!r}"

    instance = name_map[name][0]
    actual = _proxy_filters(client.identities[instance])
    for key, expected in matchers.items():
        if actual.get(key) != expected:
            return (
                f"Serial proxy {name!r} has a device attached with "
                f"{key}={actual.get(key)!r}, expected {expected!r}"
            )
    raise AssertionError(f"{endpoint} unexpectedly matches its mocked identity")


C7_WORKER = r'''
import asyncio
from contextlib import nullcontext
from datetime import datetime
import importlib.util
import json
from pathlib import Path
import sys
from types import SimpleNamespace
from unittest.mock import patch

from homeassistant import bootstrap, loader
from homeassistant.config_entries import ConfigEntries, ConfigEntry, ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.core_config import async_process_ha_core_config
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import restore_state
from homeassistant.helpers.restore_state import RestoreStateData
from homeassistant.util import dt as dt_util

scenario = sys.argv[1]
phase = sys.argv[2]
config_dir = Path(sys.argv[3]).resolve()
save_day = sys.argv[4]
expected_json = sys.argv[5] if len(sys.argv) > 5 else "{}"
tz_name = "Pacific/Auckland"
active_prior_tz = None
module_a = "A-1234567B"
module_b = "C-2345678D"
modules = [
    {"string": "A", "name": "Panel A", "barcode": module_a, "peak_power": 500},
    {"string": "B", "name": "Panel B", "barcode": module_b, "peak_power": 500},
]

assert "custom_components" not in sys.modules
assert "custom_components.pytap" not in sys.modules
assert "pytap" not in sys.modules
sys.path.insert(0, str(config_dir))

from custom_components.pytap import coordinator as coordinator_module
from custom_components.pytap.pytap.core.events import PowerReportEvent


class Clock:
    def __init__(self, instant):
        self.instant = instant

    def now(self, time_zone=None):
        return self.instant.astimezone(time_zone or dt_util.DEFAULT_TIME_ZONE)

    def utcnow(self):
        return self.instant.astimezone(dt_util.UTC)


def local_at(day, hour, minute, second=0):
    return datetime.fromisoformat(f"{day}T{hour:02d}:{minute:02d}:{second:02d}").replace(
        tzinfo=__import__("zoneinfo").ZoneInfo(tz_name)
    )


def node_event(barcode, instant):
    return PowerReportEvent(
        gateway_id=1,
        node_id=10 if barcode == module_a else 11,
        barcode=barcode,
        voltage_in=30.0,
        voltage_out=30.0,
        current_in=2.0,
        temperature=20.0,
        dc_dc_duty_cycle=0.5,
        rssi=100,
        timestamp=instant,
        high_output_mode=True,
        status_code=0x64,
        low_voltage_flag=False,
    )


async def feed(coordinator, clock, barcode, instant):
    clock.instant = instant
    assert coordinator._process_event(node_event(barcode, instant))
    coordinator._ha_update_pending = True
    coordinator._push_live_update(SimpleNamespace(counters={}))
    await coordinator.hass.async_block_till_done()


def aggregate_entities(hass, entry):
    registry = er.async_get(hass)
    component = hass.data["sensor"]
    result = {}
    for key in ("daily_energy", "total_energy"):
        unique_id = f"pytap_{entry.entry_id}_installation_{key}"
        entity_id = registry.async_get_entity_id("sensor", "pytap", unique_id)
        assert entity_id is not None
        result[key] = component.get_entity(entity_id)
        assert result[key] is not None
    return result


def capture_aggregates(hass, entities):
    result = {}
    for key, entity in entities.items():
        state = hass.states.get(entity.entity_id)
        assert state is not None
        extra = entity.extra_restore_state_data.as_dict()
        result[key] = {"state": state.state, "extra": extra}
    return result


def expected_candidate_extras(current_scenario, day):
    if current_scenario == "current":
        daily, total = 0.5, 0.5
        baselines = {module_a: 0.5}
    elif current_scenario == "removed":
        daily, total = 1.0, 1.0
        baselines = {module_a: 0.5, module_b: 0.5}
    elif current_scenario == "stale":
        daily, total = 0.5, 1.5
        baselines = None
    else:
        raise AssertionError(current_scenario)
    if baselines is None:
        return {
            "daily_energy": {
                "native_value": daily,
                "native_unit_of_measurement": "Wh",
                "baselines": {module_a: 0.0, module_b: 0.5},
                "cycle_date": day,
            },
            "total_energy": {
                "native_value": total,
                "native_unit_of_measurement": "Wh",
                "baselines": {module_a: 1.0, module_b: 0.5},
                "cycle_date": None,
            },
        }
    return {
        "daily_energy": {
            "native_value": daily,
            "native_unit_of_measurement": "Wh",
            "baselines": dict(baselines),
            "cycle_date": day,
        },
        "total_energy": {
            "native_value": total,
            "native_unit_of_measurement": "Wh",
            "baselines": dict(baselines),
            "cycle_date": None,
        },
    }


def expected_restored_extras(current_scenario, day):
    expected = expected_candidate_extras(current_scenario, day)
    if current_scenario == "removed":
        for key in expected:
            expected[key]["baselines"].pop(module_b)
    return expected


def assert_module_origins():
    import custom_components.pytap as integration
    import custom_components.pytap.coordinator as coordinator_origin
    import custom_components.pytap.pytap as parser_origin
    import custom_components.pytap.sensor as sensor_origin

    paths = {
        "integration": str(Path(integration.__file__).resolve()),
        "coordinator": str(Path(coordinator_origin.__file__).resolve()),
        "sensor": str(Path(sensor_origin.__file__).resolve()),
        "parser": str(Path(parser_origin.__file__).resolve()),
    }
    top_level = importlib.util.find_spec("pytap")
    paths["top_level_pytap"] = (
        str(Path(top_level.origin).resolve()) if top_level and top_level.origin else None
    )
    root = str(config_dir)
    assert all(not path or Path(path).is_relative_to(config_dir) for path in paths.values())
    assert paths["integration"].startswith(root)
    return paths


async def bootstrap_hass():
    global active_prior_tz
    hass = HomeAssistant(str(config_dir))
    hass.config.skip_pip = True
    loader.async_setup(hass)
    hass.config_entries = ConfigEntries(hass, {})
    assert await bootstrap.async_load_base_functionality(hass)
    assert hass.config_entries is not None
    dr.async_get(hass)
    er.async_get(hass)
    restore_state.async_get(hass)
    prior_tz = dt_util.DEFAULT_TIME_ZONE
    active_prior_tz = prior_tz
    await hass.config.async_update(time_zone=tz_name)
    assert hass.config.time_zone == tz_name
    assert dt_util.DEFAULT_TIME_ZONE == dt_util.get_time_zone(tz_name)
    hass.config.components.add("usb")
    return hass, prior_tz


async def candidate_write():
    hass, prior_tz = await bootstrap_hass()
    day = datetime.fromisoformat(save_day).date()
    clock = Clock(local_at(save_day, 0, 0, 30))
    scenario_modules = [
        dict(module)
        for module in modules
        if scenario != "current" or module["barcode"] == module_a
    ]
    entry = ConfigEntry(
        domain="pytap",
        version=7,
        minor_version=1,
        title=f"C7 {scenario}",
        unique_id=f"c7-{scenario}",
        source="user",
        data={
            "connection_type": "tcp",
            "host": "127.0.0.1",
            "port": 65431,
            "modules": scenario_modules,
            "auto_discovery": False,
            "live_reporting": True,
        },
        options={},
        discovery_keys=__import__("types").MappingProxyType({}),
        subentries_data=[],
    )
    registrations = []
    def track_reset(_hass, callback, **kwargs):
        registrations.append((callback, kwargs))
        return lambda: None

    with (
        patch("homeassistant.util.dt.now", side_effect=clock.now),
        patch("homeassistant.util.dt.utcnow", side_effect=clock.utcnow),
        patch.object(
            coordinator_module, "async_track_time_change", side_effect=track_reset
        )
        if scenario == "stale"
        else nullcontext(),
        patch.object(coordinator_module, "open_source", side_effect=OSError("offline")),
        patch.object(coordinator_module, "RECONNECT_DELAY", 3600),
    ):
        await hass.config_entries.async_add(entry)
        assert entry.state is ConfigEntryState.LOADED
        assert entry.version == 7
        coordinator = entry.runtime_data
        await hass.async_block_till_done()
        if scenario == "stale":
            previous_day = (day - __import__("datetime").timedelta(days=1)).isoformat()
            await feed(coordinator, clock, module_a, local_at(previous_day, 23, 58, 45))
            await feed(coordinator, clock, module_a, local_at(previous_day, 23, 59, 45))
            await feed(coordinator, clock, module_b, local_at(save_day, 0, 0, 0))
            await feed(coordinator, clock, module_b, local_at(save_day, 0, 0, 30))
        elif scenario in ("current", "removed"):
            active = [module_a] if scenario == "current" else [module_a, module_b]
            for barcode in active:
                await feed(coordinator, clock, barcode, local_at(save_day, 0, 0, 0))
            for barcode in active:
                await feed(coordinator, clock, barcode, local_at(save_day, 0, 0, 30))
        else:
            raise AssertionError(scenario)

        await hass.async_block_till_done()
        save_clock = clock.now().isoformat()
        save_utc_clock = clock.utcnow().isoformat()
        entities = aggregate_entities(hass, entry)
        aggregates = capture_aggregates(hass, entities)
        nodes = {barcode: dict(node) for barcode, node in coordinator.data["nodes"].items()}
        candidate_expected = expected_candidate_extras(scenario, save_day)
        assert set(aggregates) == set(candidate_expected)
        for key, expected_extra in candidate_expected.items():
            assert float(aggregates[key]["state"]) == entities[key].native_value
            assert float(aggregates[key]["state"]) == expected_extra["native_value"]
            assert aggregates[key]["extra"] == expected_extra
        assert coordinator._midnight_reset_unsub is not None or registrations
        if scenario == "stale":
            assert nodes[module_a]["daily_reset_date"] == (
                datetime.fromisoformat(save_day).date()
                - __import__("datetime").timedelta(days=1)
            ).isoformat()
            assert nodes[module_a]["daily_energy_wh"] > 0
            assert nodes[module_b]["daily_reset_date"] == save_day
            assert nodes[module_b]["daily_energy_wh"] > 0
            assert aggregates["daily_energy"]["extra"]["cycle_date"] == save_day
            assert aggregates["daily_energy"]["extra"]["baselines"][module_a] == 0.0
            assert float(aggregates["daily_energy"]["state"]) == 0.5
            assert registrations
        if scenario != "stale":
            assert not registrations
        await RestoreStateData.async_save_persistent_states(hass)
        await coordinator.async_stop_listener()
        await hass.async_stop(force=True)

    storage = config_dir / ".storage"
    required = (
        f"pytap_{entry.entry_id}_coordinator",
        "core.restore_state",
        "core.entity_registry",
        "core.config_entries",
    )
    for name in required:
        assert (storage / name).is_file(), name
    registry_text = (storage / "core.entity_registry").read_text(encoding="utf-8")
    assert f"pytap_{entry.entry_id}_installation_daily_energy" in registry_text
    return {
        "phase": "candidate",
        "scenario": scenario,
        "entry_id": entry.entry_id,
        "save_clock": save_clock,
        "save_utc_clock": save_utc_clock,
        "aggregates": aggregates,
        "nodes": nodes,
        "paths": assert_module_origins(),
    }


async def baseline_read():
    expected = json.loads(expected_json)
    hass, prior_tz = await bootstrap_hass()
    clock = Clock(local_at(save_day, 0, 0, 30))
    restore_clock = clock.now().isoformat()
    restore_utc_clock = clock.utcnow().isoformat()

    with (
        patch("homeassistant.util.dt.now", side_effect=clock.now),
        patch("homeassistant.util.dt.utcnow", side_effect=clock.utcnow),
        patch.object(coordinator_module, "open_source", side_effect=OSError("offline")),
        patch.object(coordinator_module, "RECONNECT_DELAY", 3600),
    ):
        entry = hass.config_entries.async_get_entry(expected["entry_id"])
        assert entry is not None
        assert entry.version == 7
        assert entry.state is ConfigEntryState.NOT_LOADED
        if scenario == "removed":
            updated = [
                dict(module)
                for module in entry.data["modules"]
                if module["barcode"] != module_b
            ]
            hass.config_entries.async_update_entry(
                entry, data={**entry.data, "modules": updated}
            )
            assert entry.state is ConfigEntryState.NOT_LOADED
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        assert entry.version == 7
        coordinator = entry.runtime_data
        entities = aggregate_entities(hass, entry)
        parsed_expected = expected_restored_extras(scenario, save_day)
        for key, entity in entities.items():
            raw_state = await entity.async_get_last_state()
            raw_extra = await entity.async_get_last_extra_data()
            assert raw_state is not None
            assert raw_extra is not None
            assert raw_state.state == expected["aggregates"][key]["state"]
            assert raw_extra.as_dict() == expected["aggregates"][key]["extra"]
            parsed = entity.extra_restore_state_data.as_dict()
            assert parsed == parsed_expected[key]
            assert float(hass.states.get(entity.entity_id).state) == entity.native_value
            assert float(hass.states.get(entity.entity_id).state) == parsed_expected[key]["native_value"]
        parsed_before = {
            key: entity.extra_restore_state_data.as_dict()
            for key, entity in entities.items()
        }
        if scenario == "removed":
            assert module_b not in parsed_before["daily_energy"]["baselines"]
            assert module_b not in parsed_before["total_energy"]["baselines"]
            assert float(hass.states.get(entities["daily_energy"].entity_id).state) == 1.0
            assert len(coordinator.data["nodes"]) == 1
            assert (
                parsed_before["total_energy"]["native_value"]
                > sum(node["total_energy_wh"] for node in coordinator.data["nodes"].values())
            )
        if scenario == "stale":
            assert coordinator.data["nodes"][module_a]["daily_reset_date"] == save_day
            assert coordinator.data["nodes"][module_a]["daily_energy_wh"] == 0.0
            assert float(hass.states.get(entities["daily_energy"].entity_id).state) == 0.5
        if scenario == "stale":
            post_times = (
                local_at(save_day, 0, 1, 30),
                local_at(save_day, 0, 2, 30),
            )
            expected_daily = (2.25, 3.25)
            expected_total = (3.25, 4.25)
        else:
            post_times = (
                local_at(save_day, 0, 1, 30),
                local_at(save_day, 0, 2, 30),
            )
            expected_daily = (
                parsed_expected["daily_energy"]["native_value"] + 1.0,
                parsed_expected["daily_energy"]["native_value"] + 2.0,
            )
            expected_total = (
                parsed_expected["total_energy"]["native_value"] + 1.0,
                parsed_expected["total_energy"]["native_value"] + 2.0,
            )
        for instant, wanted_daily, wanted_total in zip(
            post_times, expected_daily, expected_total, strict=True
        ):
            await feed(coordinator, clock, module_a, instant)
            daily = entities["daily_energy"]
            total = entities["total_energy"]
            assert float(hass.states.get(daily.entity_id).state) == wanted_daily
            assert float(hass.states.get(daily.entity_id).state) == daily.native_value
            assert daily.extra_restore_state_data.as_dict()["native_value"] == wanted_daily
            assert float(hass.states.get(total.entity_id).state) == wanted_total
            assert float(hass.states.get(total.entity_id).state) == total.native_value
            assert total.extra_restore_state_data.as_dict()["native_value"] == wanted_total
        parsed_after = {
            key: entity.extra_restore_state_data.as_dict()
            for key, entity in entities.items()
        }
        assert coordinator._midnight_reset_unsub is not None
        assert clock.now().isoformat() == post_times[-1].isoformat()
        await coordinator.async_stop_listener()
        await hass.async_stop(force=True)
    origins = assert_module_origins()
    return {
        "phase": "baseline",
        "scenario": scenario,
        "entry_id": entry.entry_id,
        "restore_clock": restore_clock,
        "restore_utc_clock": restore_utc_clock,
        "aggregates_before": parsed_before,
        "aggregates_after": parsed_after,
        "paths": origins,
    }


async def main():
    try:
        result = await (candidate_write() if phase == "candidate" else baseline_read())
        result["working_directory"] = str(Path.cwd().resolve())
        print("C7_JSON:" + json.dumps(result, sort_keys=True))
    finally:
        if active_prior_tz is not None:
            dt_util.set_default_time_zone(active_prior_tz)
        while str(config_dir) in sys.path:
            sys.path.remove(str(config_dir))


asyncio.run(main())
'''

C7_BASELINE = "a80698fca67bef4847a3c6ce5217f163fee94c01"


def _c7_git() -> tuple[list[str], dict[str, str]]:
    env = os.environ.copy()
    command = ["git", "-C", str(REPOSITORY)]
    dot_git = REPOSITORY / ".git"
    if env.get("GIT_DIR") or not dot_git.is_file():
        return command, env
    text = dot_git.read_text(encoding="utf-8").strip()
    prefix = "gitdir:"
    if not text.startswith(prefix):
        return command, env
    raw_git_dir = text.removeprefix(prefix).strip()
    if raw_git_dir[1:3] == ":/" or raw_git_dir[1:3] == ":\\":
        mapped = subprocess.run(
            ["wslpath", "-u", raw_git_dir],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        env["GIT_DIR"] = mapped
        env["GIT_WORK_TREE"] = str(REPOSITORY)
        command = ["git"]
    return command, env


def _c7_archive() -> bytes:
    command, env = _c7_git()
    subprocess.run(
        [*command, "cat-file", "-e", f"{C7_BASELINE}^{{commit}}"],
        check=True,
        env=env,
    )
    return subprocess.run(
        [*command, "archive", C7_BASELINE, "custom_components/pytap"],
        check=True,
        env=env,
        capture_output=True,
    ).stdout


def _run_c7_worker(
    config_dir: Path,
    scenario: str,
    phase: str,
    save_day: str,
    expected: dict | None = None,
) -> dict:
    work = config_dir / "process-working-directory"
    work.mkdir(exist_ok=True)
    env = os.environ.copy()
    for key in ("PYTHONPATH", "PYTHONHOME", "GIT_DIR", "GIT_WORK_TREE"):
        env.pop(key, None)
    env["PYTHONSAFEPATH"] = "1"
    env["PYTHONNOUSERSITE"] = "1"
    result = subprocess.run(
        [
            sys.executable,
            "-P",
            "-c",
            C7_WORKER,
            scenario,
            phase,
            str(config_dir),
            save_day,
            json.dumps(expected or {}),
        ],
        cwd=work,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    if result.returncode:
        raise AssertionError(
            f"C7 {scenario} {phase} failed\nSTDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}"
        )
    reports = [
        line.removeprefix("C7_JSON:")
        for line in result.stdout.splitlines()
        if line.startswith("C7_JSON:")
    ]
    if len(reports) != 1:
        raise AssertionError(f"C7 {scenario} {phase} did not emit one JSON report: {result.stdout}")
    report = json.loads(reports[0])
    working_directory = Path(report["working_directory"]).resolve()
    if working_directory != work.resolve() or working_directory.is_relative_to(REPOSITORY.resolve()):
        raise AssertionError(f"C7 {phase} must run in its isolated directory outside the repository")
    return report


def _c7_future_day() -> str:
    zone = zoneinfo.ZoneInfo("Pacific/Auckland")
    local_now = datetime.now(zone)
    for days in range(3, 14):
        candidate = local_now.date() + timedelta(days=days)
        start = datetime.combine(candidate, datetime.min.time(), zone)
        end = datetime.combine(candidate + timedelta(days=1), datetime.min.time(), zone)
        if (
            start.utcoffset() == end.utcoffset()
            and start.astimezone(UTC).date() != candidate
        ):
            return candidate.isoformat()
    raise AssertionError("Could not find a stable future Pacific/Auckland date")


class CoreSmoke(unittest.IsolatedAsyncioTestCase):
    """Exercise native Core config entries, schemas, and executor bridging."""

    async def asyncSetUp(self):
        self._instance_counter = 0
        self._primary = self._native_hass("primary")
        self.hass = await self._primary.__aenter__()

    async def asyncTearDown(self):
        await self._primary.__aexit__(None, None, None)

    @asynccontextmanager
    async def _native_hass(self, label: str, *, esphome_enabled: bool = False):
        self._instance_counter += 1
        directory = (
            RUNTIME_ROOT
            / f"{MAJOR_VERSION}-{MINOR_VERSION}-{self._testMethodName}-{self._instance_counter}-{label}"
        )
        shutil.rmtree(directory, ignore_errors=True)
        directory.mkdir(parents=True)
        hass = HomeAssistant(str(directory.resolve()))
        hass.config.skip_pip = True
        loader.async_setup(hass)
        hass.config_entries = ConfigEntries(hass, {})
        self.assertTrue(await bootstrap.async_load_base_functionality(hass))
        if not hasattr(hass, "auth"):
            await async_process_ha_core_config(hass, {})
        dr.async_get(hass)
        er.async_get(hass)
        restore_state.async_get(hass)
        failure: BaseException | None = None
        teardown_failure: BaseException | None = None
        try:
            yield hass
        except BaseException as err:
            failure = err
        finally:
            try:
                await hass.async_stop(force=True)
                if esphome_enabled:
                    await self._assert_esphome_handler_unregistered()
                    self._assert_port_released(hass.data.get("smoke_http_port"))
            except BaseException as err:
                teardown_failure = err
            shutil.rmtree(directory, ignore_errors=True)
        if failure is not None and teardown_failure is not None:
            raise BaseExceptionGroup("test and teardown failed", [failure, teardown_failure])
        if failure is not None:
            raise failure
        if teardown_failure is not None:
            raise teardown_failure

    @staticmethod
    def _available_port() -> int:
        while True:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
                probe.bind(("127.0.0.1", 0))
                port = probe.getsockname()[1]
            if port not in (80, 8123):
                return port

    @staticmethod
    async def _assert_esphome_handler_unregistered() -> None:
        port = serialx.async_serial_for_url(
            "esphome-hass-internal://esphome/cleanup?port_name=cleanup",
            baudrate=38400,
        )
        with unittest.TestCase().assertRaises(serialx.common.UnknownUriScheme):
            await port.open()

    async def test_graceful_stop_persists_latest_energy_before_native_restart(self):
        barcode = MODULE["barcode"]
        entered = asyncio.Event()
        stopping = threading.Event()
        release = threading.Event()
        worker_exited = threading.Event()
        save_started = threading.Event()
        saved_before_exit = False
        time_zone = zoneinfo.ZoneInfo("Pacific/Auckland")
        first_at = datetime(2035, 6, 15, 12, 0, tzinfo=time_zone)
        prior_at = first_at + timedelta(seconds=30)
        latest_at = prior_at + timedelta(seconds=30)
        clock = first_at
        prior_tz = dt_util.DEFAULT_TIME_ZONE

        def now(time_zone=None):
            return clock.astimezone(time_zone) if time_zone is not None else clock

        def utcnow():
            return clock.astimezone(UTC)

        def report(at):
            return PowerReportEvent(
                gateway_id=1,
                node_id=1,
                barcode=barcode,
                voltage_in=30.0,
                voltage_out=30.0,
                current_in=0.04,
                temperature=20.0,
                dc_dc_duty_cycle=0.5,
                rssi=100,
                timestamp=at,
                high_output_mode=True,
                status_code=0x64,
                low_voltage_flag=False,
            )

        def worker(coordinator):
            coordinator.hass.loop.call_soon_threadsafe(entered.set)
            try:
                assert coordinator._stop_event.wait(5)
                stopping.set()
                assert release.wait(5)
            finally:
                worker_exited.set()

        self.hass.config.components.add("usb")
        restore_state.async_get(self.hass).async_setup_dump()
        entry = self.entry(
            version=7,
            unique_id="shutdown-persistence",
            data={
                "connection_type": "tcp",
                "host": "127.0.0.1",
                "port": self._available_port(),
                "modules": [MODULE],
                "live_reporting": True,
            },
        )
        try:
            await self.hass.config.async_update(time_zone="Pacific/Auckland")
            with (
                patch("custom_components.pytap.coordinator.dt_util.now", side_effect=now),
                patch("custom_components.pytap.coordinator.dt_util.utcnow", side_effect=utcnow),
                patch.object(PyTapDataUpdateCoordinator, "_listen", new=worker),
            ):
                try:
                    await self.hass.config_entries.async_add(entry)
                    self.assertIs(entry.state, ConfigEntryState.LOADED)
                    async with asyncio.timeout(1):
                        await entered.wait()
                    coordinator = entry.runtime_data
                    assert coordinator._process_event(report(first_at))
                    clock = prior_at
                    assert coordinator._process_event(report(prior_at))
                    coordinator._ha_update_pending = True
                    coordinator._push_live_update(MagicMock(counters={}))
                    await self.hass.async_block_till_done()
                    coordinator._cancel_scheduled_save()
                    await coordinator._async_save_coordinator_state()
                    self.assertAlmostEqual(
                        coordinator._energy_state[barcode].total_energy_wh, 0.01
                    )
                    clock = latest_at
                    assert coordinator._process_event(report(latest_at))
                    coordinator._ha_update_pending = True
                    coordinator._push_live_update(MagicMock(counters={}))
                    await self.hass.async_block_till_done()
                    coordinator._cancel_scheduled_save()
                    original_save = coordinator._async_save_coordinator_state

                    async def observed_save():
                        nonlocal saved_before_exit
                        save_started.set()
                        saved_before_exit = not worker_exited.is_set()
                        await original_save()

                    coordinator._async_save_coordinator_state = observed_save

                    async def release_worker_after_stage_two():
                        async with asyncio.timeout(2):
                            while not stopping.is_set():
                                await asyncio.sleep(0)
                        self.assertFalse(save_started.is_set())
                        release.set()

                    release_task = asyncio.create_task(
                        release_worker_after_stage_two()
                    )
                    await asyncio.gather(
                        self.hass.async_stop(force=True),
                        release_task,
                    )
                    self.assertTrue(worker_exited.is_set())
                    self.assertTrue(save_started.is_set())
                    self.assertFalse(saved_before_exit)
                finally:
                    release.set()
                    if "coordinator" in locals():
                        coordinator._stop_event.set()
        finally:
            dt_util.set_default_time_zone(prior_tz)

        config_dir = self.hass.config.config_dir
        restarted = HomeAssistant(config_dir)
        restarted.config.skip_pip = True
        loader.async_setup(restarted)
        restarted.config_entries = ConfigEntries(restarted, {})
        try:
            self.assertTrue(await bootstrap.async_load_base_functionality(restarted))
            if not hasattr(restarted, "auth"):
                await async_process_ha_core_config(restarted, {})
            self.assertEqual(restarted.config.time_zone, "Pacific/Auckland")
            dr.async_get(restarted)
            er.async_get(restarted)
            restarted.config.components.add("usb")
            restored_entry = restarted.config_entries.async_get_entry(entry.entry_id)
            self.assertIsNotNone(restored_entry)
            with (
                patch(
                    "custom_components.pytap.coordinator.dt_util.now",
                    return_value=latest_at,
                ),
                patch(
                    "custom_components.pytap.coordinator.dt_util.utcnow",
                    return_value=latest_at.astimezone(UTC),
                ),
                patch.object(
                    PyTapDataUpdateCoordinator,
                    "_listen",
                    return_value=None,
                ),
            ):
                self.assertTrue(
                    await restarted.config_entries.async_setup(restored_entry.entry_id)
                )
                await restarted.async_block_till_done()
            restored = restored_entry.runtime_data.data["nodes"][barcode]
            self.assertAlmostEqual(restored["daily_energy_wh"], 0.02)
            self.assertAlmostEqual(restored["total_energy_wh"], 0.02)

            registry = er.async_get(restarted)
            component = restarted.data["sensor"]
            for kind in ("daily_energy", "total_energy"):
                unique_id = (
                    f"pytap_{restored_entry.entry_id}_installation_{kind}"
                )
                entity_id = registry.async_get_entity_id("sensor", "pytap", unique_id)
                self.assertIsNotNone(entity_id)
                entity = component.get_entity(entity_id)
                saved_state = await entity.async_get_last_state()
                saved_extra = await entity.async_get_last_extra_data()
                self.assertIsNotNone(saved_state)
                self.assertIsNotNone(saved_extra)
                self.assertAlmostEqual(float(saved_state.state), 0.02)
                saved_data = saved_extra.as_dict()
                self.assertAlmostEqual(saved_data["native_value"], 0.02)
                self.assertEqual(saved_data["baselines"], {barcode: 0.02})
                self.assertAlmostEqual(entity.native_value, 0.02)
                self.assertAlmostEqual(
                    float(restarted.states.get(entity_id).state), 0.02
                )
                self.assertAlmostEqual(
                    entity.extra_restore_state_data.as_dict()["native_value"], 0.02
                )
        finally:
            try:
                await restarted.async_stop(force=True)
            finally:
                dt_util.set_default_time_zone(prior_tz)

    @staticmethod
    def _assert_port_released(port: int | None) -> None:
        if port is None:
            return
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.bind(("127.0.0.1", port))

    @asynccontextmanager
    async def _esphome_hass(self, label: str, *, add_entry: bool = True):
        async with self._native_hass(label, esphome_enabled=True) as hass:
            clients: list[_FakeESPHomeClient] = []

            def client_factory(*_args, **_kwargs):
                client = _FakeESPHomeClient(_identities())
                clients.append(client)
                return client

            network_adapter = {
                "name": "loopback",
                "enabled": True,
                "default": True,
                "auto": True,
                "ipv4": [{"address": "127.0.0.1"}],
                "ipv6": [],
            }
            with ExitStack() as patches:
                patches.enter_context(
                    patch(
                        "homeassistant.components.network.async_get_adapters",
                        new=AsyncMock(return_value=[network_adapter]),
                    )
                )
                patches.enter_context(
                    patch(
                        "homeassistant.components.network.network.async_load_adapters",
                        new=AsyncMock(return_value=[network_adapter]),
                    )
                )
                patches.enter_context(
                    patch(
                        "homeassistant.components.network._check_docker_without_host_networking",
                        return_value=True,
                    )
                )
                patches.enter_context(
                    patch(
                        "homeassistant.components.network.util.async_get_source_ip",
                        return_value="127.0.0.1",
                    )
                )
                patches.enter_context(
                    patch(
                        "homeassistant.components.usb.USBDiscovery._async_start_aiousbwatcher",
                        new=AsyncMock(return_value=None),
                    )
                )
                patches.enter_context(
                    patch("homeassistant.components.usb.scan_serial_ports", return_value=[])
                )
                patches.enter_context(
                    patch(
                        "homeassistant.components.ffmpeg.FFmpegManager.async_get_version",
                        new=AsyncMock(return_value="smoke"),
                    )
                )
                patches.enter_context(
                    patch(
                        "homeassistant.components.bluetooth.HomeAssistantBluetoothManager.async_setup",
                        new=AsyncMock(return_value=None),
                    )
                )
                patches.enter_context(
                    patch(
                        "homeassistant.components.bluetooth._async_start_adapter_discovery",
                        new=AsyncMock(return_value=None),
                    )
                )
                patches.enter_context(
                    patch(
                        "homeassistant.components.esphome.async_create_api_client",
                        side_effect=client_factory,
                    )
                )

                port = self._available_port()
                self.assertTrue(await async_setup_component(hass, "network", {}))
                self.assertTrue(
                    await async_setup_component(
                        hass,
                        "http",
                        {"http": {"server_host": "127.0.0.1", "server_port": port}},
                    )
                )
                self.assertEqual(hass.http.server_host, ["127.0.0.1"])
                self.assertEqual(hass.http.server_port, port)
                hass.data["smoke_http_port"] = port

                self.assertTrue(await async_setup_component(hass, "usb", {}))
                self.assertTrue(await async_setup_component(hass, "esphome", {}))
                entry = None
                if add_entry:
                    entry = self._esphome_entry()
                    await hass.config_entries.async_add(entry)
                    self.assertIs(entry.state, ConfigEntryState.LOADED)
                    async with asyncio.timeout(5):
                        await entry.runtime_data.first_connect_done.wait()
                    if (MAJOR_VERSION, MINOR_VERSION) >= (2026, 11):
                        async with asyncio.timeout(5):
                            while len(entry.runtime_data.serial_proxy_identities) < len(
                                entry.runtime_data.device_info.serial_proxies
                            ):
                                await asyncio.sleep(0)
                try:
                    yield hass, entry, clients
                finally:
                    async with asyncio.timeout(30):
                        await hass.async_block_till_done()
                        for pytap_entry in hass.config_entries.async_entries(
                            "pytap"
                        ):
                            if pytap_entry.state is ConfigEntryState.LOADED:
                                coordinator = pytap_entry.runtime_data
                                self.assertTrue(
                                    await hass.config_entries.async_unload(
                                        pytap_entry.entry_id
                                    )
                                )
                                self.assertIsNone(coordinator._listener_task)
                                self.assertIsNone(coordinator._worker_future)

    @staticmethod
    def _esphome_entry():
        return ConfigEntry(
            domain="esphome",
            version=1,
            minor_version=1,
            title="Smoke Node",
            unique_id="aa:bb:cc:dd:ee:ff",
            source="user",
            data={"host": "127.0.0.1", "port": 6053, "password": ""},
            options={},
            discovery_keys=MappingProxyType({}),
            subentries_data=[],
        )

    async def _assert_proxy_opens(
        self, label: str, endpoint_name: str, expected_instance: int
    ) -> None:
        async with self._esphome_hass(label) as (hass, entry, clients):
            self.assertIsNotNone(entry)
            endpoint = _url_fixtures(entry.entry_id)[endpoint_name][0]
            with patch(
                "custom_components.pytap.connection.serialx.async_serial_for_url",
                wraps=serialx.async_serial_for_url,
            ) as factory:
                source = await hass.async_add_executor_job(
                    open_source,
                    hass.loop,
                    {"connection_type": "serial", "serial_port": endpoint},
                )
            try:
                factory.assert_called_once()
                self.assertEqual(factory.call_args.args[0], endpoint)
                payload = f"{endpoint_name}-payload".encode()
                clients[-1].emit_data(expected_instance, payload)
                self.assertEqual(
                    await hass.async_add_executor_job(source.read, 4096), payload
                )
            finally:
                await hass.async_add_executor_job(source.close)
            self.assertFalse(clients[-1].claimed)

    async def _backend_flow_endpoint(
        self, hass: HomeAssistant, esphome_entry: ConfigEntry | None, kind: str
    ) -> str:
        if kind == "B1":
            return _proxy_url("missing-entry", port_name="CDC Port")
        self.assertIsNotNone(esphome_entry)
        if kind == "B2":
            self.assertTrue(
                await hass.config_entries.async_unload(esphome_entry.entry_id)
            )
            return _url_fixtures(esphome_entry.entry_id)["S1c"][0]
        value = "Missing Port" if kind == "M1" else "OTHER"
        key = "port_name" if kind == "M1" else "port_serial_number"
        return _changed_query(
            _url_fixtures(esphome_entry.entry_id)["S6"][0], key, value
        )

    async def _assert_backend_flow(self, kind: str, *, options: bool) -> None:
        async with self._esphome_hass(
            f"c8-{kind}-{'options' if options else 'user'}",
            add_entry=kind != "B1",
        ) as (hass, esphome_entry, _clients):
            endpoint = await self._backend_flow_endpoint(
                hass, esphome_entry, kind
            )
            if options:
                pytap_entry = await self._loaded_tcp_entry(hass, f"{kind}-options")
                flow = await self._new_options_serial_flow(hass, pytap_entry)
                submit = flow.async_step_change_serial
            else:
                flow = await self._new_user_serial_flow(hass)
                submit = flow.async_step_serial
                pytap_entry = None

            expected = (
                "unsupported_serial_settings" if kind == "M1" else "cannot_connect"
            )
            async with _capture_pytap_logs() as captured:
                with (
                    patch(
                        "custom_components.pytap.connection.serialx.async_serial_for_url",
                        wraps=serialx.async_serial_for_url,
                    ) as factory,
                ):
                    first = await submit({"serial_port": endpoint})
                    self.assertEqual(first["errors"], {"base": expected})
                    second = await submit({"serial_port": endpoint})
                    if kind == "M1":
                        self.assertEqual(second["errors"], {"base": expected})
                        if options:
                            self.assertNotIn("serial_port", pytap_entry.data)
                        else:
                            self.assertFalse(
                                hass.config_entries.async_entries("pytap")
                            )
                    elif options:
                        self.assertEqual(second["type"], FlowResultType.MENU)
                        self.assertEqual(second["step_id"], "init")
                        self.assertEqual(pytap_entry.data["serial_port"], endpoint)
                    else:
                        self.assertEqual(second["type"], FlowResultType.MENU)
                        self.assertEqual(second["step_id"], "modules_menu")
                        await self._finish_user_flow(flow, endpoint)
            self.assertGreaterEqual(factory.call_count, 2)
            self.assertTrue(
                all(call.args[0] == endpoint for call in factory.call_args_list)
            )
            parsed = urlsplit(endpoint)
            sensitive = {
                endpoint,
                parsed.path.strip("/"),
                *(value for _key, value in parse_qsl(parsed.query)),
            }
            if esphome_entry is not None:
                sensitive.add(esphome_entry.entry_id)
            for message in captured.messages:
                self.assertTrue(
                    all(value not in message for value in sensitive if value)
                )
            if expected == "cannot_connect":
                warnings = [
                    record
                    for record in captured.records
                    if record.name == "custom_components.pytap.config_flow"
                    and record.levelno == logging.WARNING
                ]
                self.assertGreaterEqual(len(warnings), 1)
                self.assertTrue(
                    all(
                        "Could not connect using serial (" in record.getMessage()
                        for record in warnings
                    )
                )
            else:
                self.assertFalse(
                    [
                        record
                        for record in captured.records
                        if record.name == "custom_components.pytap.config_flow"
                    ]
                )

    async def _assert_prevalidation_rejected(
        self, endpoint: str, error: str
    ) -> None:
        with patch(
            "custom_components.pytap.connection.serialx.async_serial_for_url"
        ) as factory:
            with self.assertRaises(InvalidConnectionSettings) as caught:
                await self.hass.async_add_executor_job(
                    open_source,
                    self.hass.loop,
                    {"connection_type": "serial", "serial_port": endpoint},
                )
        self.assertEqual(caught.exception.field, "serial_port")
        self.assertEqual(caught.exception.error, error)
        factory.assert_not_called()

    async def _assert_proxy_backend_error(
        self,
        label: str,
        endpoint: str | Callable[[str], str],
        expected: type[Exception],
    ) -> None:
        async with self._esphome_hass(label) as (hass, entry, clients):
            if not isinstance(endpoint, str):
                endpoint = endpoint(entry.entry_id)
            await self._assert_failed_proxy_open(
                hass,
                clients[-1],
                endpoint,
                expected,
                _expected_matcher_error(endpoint, clients[-1]),
            )

    async def _assert_failed_proxy_open(
        self,
        hass: HomeAssistant,
        client: _FakeESPHomeClient,
        endpoint: str,
        expected: type[Exception],
        reason: str,
        *,
        data_instance: int = 0,
    ) -> None:
        created_serials: list[serialx.AsyncSerial] = []
        closed_transports: list[esphome_serial_proxy.HassESPHomeSerialTransport] = []
        waited_transports: list[esphome_serial_proxy.HassESPHomeSerialTransport] = []
        original_factory = serialx.async_serial_for_url
        transport_type = esphome_serial_proxy.HassESPHomeSerialTransport
        original_close = transport_type.close
        original_wait_closed = transport_type.wait_closed

        def create_serial(*args, **kwargs):
            serial = original_factory(*args, **kwargs)
            created_serials.append(serial)
            return serial

        def close_transport(transport) -> None:
            closed_transports.append(transport)
            original_close(transport)

        async def wait_transport_closed(transport) -> None:
            waited_transports.append(transport)
            await original_wait_closed(transport)

        subscriptions_before = client.subscription_counts()
        claims_before = set(client.claimed)
        deliveries_before = client.data_deliveries
        with (
            patch(
                "custom_components.pytap.connection.serialx.async_serial_for_url",
                side_effect=create_serial,
            ) as factory,
            patch.object(transport_type, "close", new=close_transport),
            patch.object(
                transport_type, "wait_closed", new=wait_transport_closed
            ),
        ):
            with self.assertRaises(expected) as caught:
                await hass.async_add_executor_job(
                    open_source,
                    hass.loop,
                    {"connection_type": "serial", "serial_port": endpoint},
                )

        self.assertIs(type(caught.exception), expected)
        self.assertEqual(str(caught.exception), reason)
        factory.assert_called_once()
        self.assertEqual(factory.call_args.args, (endpoint,))
        self.assertEqual(len(created_serials), 1)
        self.assertEqual(len(closed_transports), 1)
        self.assertEqual(len(waited_transports), 1)
        transport = closed_transports[0]
        self.assertIs(waited_transports[0], transport)
        self.assertTrue(transport.is_closing())
        serial = created_serials[0]
        self.assertIsNone(serial._transport)
        self.assertIsNone(serial._reader)
        self.assertIsNone(serial._writer)
        backend = transport.get_extra_info("serial")
        self.assertIsNotNone(backend)
        self.assertFalse(backend._instance_subscribed)
        self.assertFalse(client.claimed)
        self.assertEqual(client.claimed, claims_before)
        self.assertEqual(client.subscription_counts(), subscriptions_before)

        client.emit_data(data_instance, b"must-not-reach-failed-source")
        await asyncio.sleep(0)
        self.assertEqual(client.data_deliveries, deliveries_before)
        self.assertEqual(len(backend._read_buffer), 0)

    def entry(
        self,
        version=6,
        *,
        data=None,
        title="PyTap (gateway)",
        unique_id="stable-id",
    ):
        entry = ConfigEntry(
            domain="pytap",
            version=version,
            minor_version=1,
            title=title,
            unique_id=unique_id,
            source="user",
            data=data
            or {"host": "gateway", "port": 502, "modules": [MODULE]},
            options={},
            discovery_keys=MappingProxyType({}),
            subentries_data=[],
        )
        return entry

    async def _unloaded_pytap_entry(self, label: str):
        self.hass.config.components.add("usb")
        entry = self.entry(
            version=7,
            title=label,
            unique_id=f"{label}-id",
            data={
                "connection_type": "tcp",
                "host": "127.0.0.1",
                "port": self._available_port(),
                "modules": [MODULE],
            },
        )
        await self.hass.config_entries.async_add(entry)
        self.assertIs(entry.state, ConfigEntryState.LOADED)
        self.assertTrue(await self.hass.config_entries.async_unload(entry.entry_id))
        self.assertIs(entry.state, ConfigEntryState.NOT_LOADED)
        return entry

    async def _loaded_tcp_entry(self, hass: HomeAssistant, label: str):
        entry = self.entry(
            version=7,
            title=label,
            unique_id=f"{label}-id",
            data={
                "connection_type": "tcp",
                "host": "127.0.0.1",
                "port": self._available_port(),
                "modules": [MODULE],
            },
        )
        await hass.config_entries.async_add(entry)
        self.assertIs(entry.state, ConfigEntryState.LOADED)
        return entry

    @staticmethod
    def pytap_serial_entry(endpoint: str, *, label: str = "serial-smoke"):
        return ConfigEntry(
            domain="pytap",
            version=7,
            minor_version=1,
            title="PyTap serial",
            unique_id=f"{label}-id",
            source="user",
            data={
                "connection_type": "serial",
                "serial_port": endpoint,
                "baud_rate": 38400,
                "modules": [MODULE],
            },
            options={},
            discovery_keys=MappingProxyType({}),
            subentries_data=[],
        )

    @staticmethod
    async def _eventually(predicate, timeout: float = 5) -> None:
        async with asyncio.timeout(timeout):
            while not predicate():
                await asyncio.sleep(0.01)

    async def _new_user_serial_flow(self, hass: HomeAssistant):
        flow = PyTapConfigFlow()
        flow.hass = hass
        flow.context = {"source": "user"}
        menu = await flow.async_step_user()
        self.assertEqual(menu["type"], FlowResultType.MENU)
        self.assertEqual(menu["menu_options"], ["tcp", "serial"])
        form = await flow.async_step_serial()
        schema = {str(key): value for key, value in form["data_schema"].schema.items()}
        self.assertIsInstance(schema["serial_port"], SerialPortSelector)
        return flow

    async def _new_options_serial_flow(
        self, hass: HomeAssistant, entry: ConfigEntry
    ):
        flow = PyTapOptionsFlow(entry)
        flow.hass = hass
        first = await flow.async_step_init()
        self.assertEqual(first["type"], FlowResultType.MENU)
        second = await flow.async_step_change_connection()
        self.assertEqual(second["menu_options"], ["change_tcp", "change_serial"])
        form = await flow.async_step_change_serial()
        schema = {str(key): value for key, value in form["data_schema"].schema.items()}
        self.assertIsInstance(schema["serial_port"], SerialPortSelector)
        return flow

    async def _finish_user_flow(self, flow: PyTapConfigFlow, endpoint: str):
        module_form = await flow.async_step_add_module()
        self.assertEqual(module_form["type"], FlowResultType.FORM)
        modules = await flow.async_step_add_module(MODULE)
        self.assertEqual(modules["type"], FlowResultType.MENU)
        self.assertEqual(modules["step_id"], "modules_menu")
        result = await flow.async_step_finish()
        self.assertEqual(result["type"], FlowResultType.CREATE_ENTRY)
        self.assertEqual(result["title"], "PyTap (Home Assistant serial proxy)")
        self.assertEqual(result["data"]["serial_port"], endpoint)
        return result

    async def _assert_flow_accepts(
        self, label: str, endpoint_name: str, *, options: bool
    ) -> None:
        async with self._esphome_hass(label) as (hass, esphome_entry, _clients):
            endpoint = _url_fixtures(esphome_entry.entry_id)[endpoint_name][0]
            with patch(
                "custom_components.pytap.connection.serialx.async_serial_for_url",
                wraps=serialx.async_serial_for_url,
            ) as factory:
                if options:
                    pytap_entry = await self._loaded_tcp_entry(hass, f"{label}-pytap")
                    before = len(hass.config_entries.async_entries("pytap"))
                    flow = await self._new_options_serial_flow(hass, pytap_entry)
                    result = await flow.async_step_change_serial(
                        {"serial_port": endpoint}
                    )
                    self.assertEqual(result["type"], FlowResultType.MENU)
                    self.assertEqual(result["step_id"], "init")
                    self.assertEqual(pytap_entry.data["serial_port"], endpoint)
                    self.assertEqual(
                        pytap_entry.title, "PyTap (Home Assistant serial proxy)"
                    )
                    self.assertEqual(
                        len(hass.config_entries.async_entries("pytap")), before
                    )
                else:
                    flow = await self._new_user_serial_flow(hass)
                    self.assertFalse(hass.config_entries.async_entries("pytap"))
                    result = await flow.async_step_serial({"serial_port": endpoint})
                    self.assertEqual(result["type"], FlowResultType.MENU)
                    self.assertEqual(result["step_id"], "modules_menu")
                    await self._finish_user_flow(flow, endpoint)
                    self.assertFalse(hass.config_entries.async_entries("pytap"))
            factory.assert_called_once()
            self.assertEqual(factory.call_args.args[0], endpoint)

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

    async def test_c2_options_serial_schema_uses_public_selector(self):
        entry = self.entry(version=7)
        flow = PyTapOptionsFlow(entry)
        flow.hass = self.hass
        result = await flow.async_step_change_serial()
        self.assertEqual(result["type"], FlowResultType.FORM)
        schema = {
            str(key): value for key, value in result["data_schema"].schema.items()
        }
        self.assertIsInstance(schema["serial_port"], SerialPortSelector)
        self.assertEqual(
            result["data_schema"]({"serial_port": ENDPOINT})["serial_port"], ENDPOINT
        )

    async def test_empty_auto_setup_and_discovery_on_supported_core(self):
        flow = PyTapConfigFlow()
        flow.hass = self.hass
        flow.context = {"source": "user"}
        with patch("custom_components.pytap.config_flow.validate_connection", new=AsyncMock()):
            await flow.async_step_tcp({"host": "gateway"})
        result = await flow.async_step_finish()
        self.assertTrue(result["data"]["auto_discovery"])
        self.assertEqual(result["data"]["modules"], [])
        entry = self.entry(version=7, data=result["data"])
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
        self.hass.config.components.add("usb")
        await self.hass.config_entries.async_add(entry)
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

    async def test_real_serial_transport_releases_virtual_uart(self):
        if not hasattr(os, "openpty"):
            self.fail("This supported-core test requires POSIX pseudo-terminals")
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

    async def test_c3_manifest_accepts_the_running_serialx_pin(self):
        manifest = json.loads(
            (Path(__file__).parents[1] / "custom_components" / "pytap" / "manifest.json").read_text(
                encoding="utf-8"
            )
        )
        serialx_requirements = []
        for declaration in manifest["requirements"]:
            requirement = Requirement(declaration)
            if canonicalize_name(requirement.name) == "serialx":
                serialx_requirements.append(requirement)
        self.assertEqual(len(serialx_requirements), 1)
        requirement = serialx_requirements[0]
        self.assertIsNone(requirement.marker)
        self.assertIsNone(requirement.url)
        installed = metadata.version("serialx")
        constraints_path = Path(loader.__file__).with_name("package_constraints.txt")
        self.assertTrue(constraints_path.is_file())
        serialx_constraints = []
        for line in constraints_path.read_text(encoding="utf-8").splitlines():
            declaration = line.strip()
            if not declaration or declaration.startswith(("#", "-")):
                continue
            constraint = Requirement(declaration)
            if canonicalize_name(constraint.name) == "serialx":
                serialx_constraints.append(constraint)
        self.assertEqual(len(serialx_constraints), 1)
        pins = [
            spec.version
            for spec in serialx_constraints[0].specifier
            if spec.operator == "=="
        ]
        self.assertEqual(len(pins), 1)
        expected = pins[0]
        self.assertEqual(installed, expected)
        self.assertTrue(requirement.specifier.contains(installed, prereleases=False))

    async def test_p1_p9_and_capture_translation_keys_are_synchronized(self):
        source = json.loads(
            (REPOSITORY / "custom_components" / "pytap" / "strings.json").read_text(
                encoding="utf-8"
            )
        )
        translated = json.loads(
            (
                REPOSITORY
                / "custom_components"
                / "pytap"
                / "translations"
                / "en.json"
            ).read_text(encoding="utf-8")
        )
        for section in ("config", "options"):
            for key in (
                "serial_port_name_required",
                "serial_port_udev_suffix_unsupported",
            ):
                self.assertEqual(
                    source[section]["error"][key], translated[section]["error"][key]
                )
        for key in (
            "serial_port_name_required",
            "serial_port_udev_suffix_unsupported",
            "capture_path_unsafe",
        ):
            self.assertEqual(source["exceptions"][key], translated["exceptions"][key])

    async def test_p1_prevalidation_is_version_specific(self):
        endpoint = "esphome-hass://esphome/entry-one?port_udev_id=usb-Tigo_CDC-if00"
        settings = {
            "connection_type": "serial",
            "serial_port": endpoint,
            "baud_rate": 38400,
        }
        if (MAJOR_VERSION, MINOR_VERSION) < (2026, 11):
            await self._assert_prevalidation_rejected(
                endpoint, "serial_port_name_required"
            )
        else:
            self.assertEqual(normalize_connection(settings)["serial_port"], endpoint)

    async def test_p9_prevalidation_decodes_and_precedes_p1(self):
        for endpoint in (
            "esphome-hass://esphome/entry-one?port_udev_id=usb-Tigo_Vendor-if00-port0",
            "esphome-hass://esphome/entry-one?port_udev_id=usb-Tigo_Vendor-if00%2Dport0",
            "esphome-hass://esphome/entry-one?"
            "port_name=Tigo&port_udev_id=usb-Tigo_Vendor-if00-port0",
        ):
            with self.subTest(endpoint=endpoint):
                settings = {
                    "connection_type": "serial",
                    "serial_port": endpoint,
                    "baud_rate": 38400,
                }
                if (MAJOR_VERSION, MINOR_VERSION) < (2026, 11):
                    await self._assert_prevalidation_rejected(
                        endpoint, "serial_port_udev_suffix_unsupported"
                    )
                else:
                    self.assertEqual(normalize_connection(settings)["serial_port"], endpoint)

    async def test_c4_esphome_name_url_opens_real_handler(self):
        async with self._esphome_hass("c4-s1") as (hass, entry, clients):
            self.assertIsNotNone(entry)
            endpoint = str(
                esphome_serial_proxy.build_url(entry.entry_id, port_name="CDC Port")
            )
            with patch(
                "custom_components.pytap.connection.serialx.async_serial_for_url",
                wraps=serialx.async_serial_for_url,
            ) as factory:
                source = await hass.async_add_executor_job(
                    open_source,
                    hass.loop,
                    {"connection_type": "serial", "serial_port": endpoint},
                )
            try:
                factory.assert_called_once()
                self.assertEqual(factory.call_args.args[0], endpoint)
                clients[-1].emit_data(0, b"\x00\xfftigo")
                self.assertEqual(
                    await hass.async_add_executor_job(source.read, 4096),
                    b"\x00\xfftigo",
                )
            finally:
                await hass.async_add_executor_job(source.close)
            self.assertFalse(clients[-1].claimed)

    async def test_c4_acceptance_equivalence_classes(self):
        unsupported = {
            "S2c": "serial_port_name_required",
            "S2v": "serial_port_udev_suffix_unsupported",
            "S3v": "serial_port_udev_suffix_unsupported",
            "S7": "serial_port_name_required",
        }
        for name in ("S1c", "S1v", "S2c", "S2v", "S3c", "S3v", "S4", "S5", "S6", "S7"):
            with self.subTest(case=name):
                if (MAJOR_VERSION, MINOR_VERSION) == (2026, 10) and name in unsupported:
                    endpoint = _url_fixtures("entry-one")[name][0]
                    await self._assert_prevalidation_rejected(
                        endpoint, unsupported[name]
                    )
                    continue
                await self._assert_proxy_opens(
                    f"c4-{name}", name, _url_fixtures("entry-one")[name][1]
                )
        if (MAJOR_VERSION, MINOR_VERSION) == (2026, 10):
            await self._assert_prevalidation_rejected(
                _url_fixtures("entry-one")["S2v-encoded"][0],
                "serial_port_udev_suffix_unsupported",
            )
        else:
            await self._assert_proxy_opens(
                "c4-S2v-encoded", "S2v-encoded", 1
            )

    async def test_c4_canonical_builder_branches(self):
        identities = _identities()
        if (MAJOR_VERSION, MINOR_VERSION) < (2026, 11):
            self.assertEqual(
                str(esphome_serial_proxy.build_url("entry-one", port_name="CDC Port")),
                _url_fixtures("entry-one")["S1c"][0],
            )
            self.assertEqual(
                str(esphome_serial_proxy.build_url("entry-one", port_name="Vendor Port")),
                _url_fixtures("entry-one")["S1v"][0],
            )
            await self._assert_proxy_opens("c4-g1", "S1v", 1)
            return

        build_port_url = esphome_serial_proxy.build_port_url
        no_source = replace(identities[0], source=SerialProxyIdentitySource.NONE)
        disconnected = replace(identities[0], flags=0)
        errored = replace(identities[0], flags=SerialProxyIdentityFlag.ERROR)
        self.assertEqual(
            str(build_port_url("entry-one", "CDC Port", None)),
            _url_fixtures("entry-one")["S1c"][0],
        )
        self.assertEqual(
            str(build_port_url("entry-one", "CDC Port", no_source)),
            _url_fixtures("entry-one")["S1c"][0],
        )
        self.assertIsNone(build_port_url("entry-one", "CDC Port", disconnected))
        self.assertIsNone(build_port_url("entry-one", "CDC Port", errored))
        self.assertEqual(
            str(build_port_url("entry-one", "CDC Port", identities[0])),
            _url_fixtures("entry-one")["S2c"][0],
        )
        self.assertEqual(
            str(build_port_url("entry-one", "Vendor Port", identities[1])),
            _url_fixtures("entry-one")["S2v"][0],
        )
        duplicate = replace(identities[0], serial_number="12345678")
        self.assertTrue(serialx.is_non_unique_serial(duplicate.serial_number))
        duplicate_url = str(build_port_url("entry-one", "CDC Port", duplicate))
        self.assertIn("port_name=CDC+Port", duplicate_url)
        self.assertIn("port_udev_id=", duplicate_url)
        self.assertEqual(
            str(build_port_url("entry-one", "Fallback Port", identities[2])),
            _url_fixtures("entry-one")["S4"][0],
        )
        no_metadata = replace(
            identities[2],
            manufacturer="",
            product="",
            serial_number="",
            usb=UsbDeviceDescriptor(),
        )
        self.assertEqual(
            str(build_port_url("entry-one", "Fallback Port", no_metadata)),
            _proxy_url("entry-one", port_name="Fallback Port"),
        )

    async def test_c4_all_supplied_matchers_are_enforced(self):
        changes = {
            "M1": ("port_name", "Missing Port"),
            "M2": ("port_manufacturer", "Other Vendor"),
            "M3": ("port_product", "Other Product"),
            "M4": ("port_serial_number", "OTHER"),
            "M5": ("port_usb_vid", "0x9999"),
            "M6": ("port_usb_pid", "0x9999"),
            "M7": ("port_usb_bcd_device", "0x9999"),
            "M8": ("port_usb_interface_num", "0x9"),
            "M9": ("port_udev_id", "usb-Other_Device-if00"),
        }
        for case, (key, value) in changes.items():
            with self.subTest(case=case):
                await self._assert_proxy_backend_error(
                    f"c4-{case}",
                    lambda entry_id, key=key, value=value: _changed_query(
                        _url_fixtures(entry_id)["S6"][0], key, value
                    ),
                    ValueError if case == "M1" else serialx.SerialException,
                )

        if (MAJOR_VERSION, MINOR_VERSION) < (2026, 11):
            return
        await self._assert_proxy_backend_error(
            "c4-M10",
            lambda entry_id: _changed_query(
                _url_fixtures(entry_id)["S2c"][0],
                "port_udev_id",
                "usb-Other_Device-if00",
            ),
            serialx.SerialException,
        )
        await self._assert_proxy_backend_error(
            "c4-M11",
            lambda entry_id: _changed_query(
                _url_fixtures(entry_id)["S7"][0],
                "port_serial_number",
                "OTHER",
            ),
            serialx.SerialException,
        )
        await self._assert_proxy_backend_error(
            "c4-M12",
            lambda entry_id: _changed_query(
                _url_fixtures(entry_id)["S3v"][0],
                "port_udev_id",
                _proxy_filters(_identities()[1])["port_udev_id"][:-1] + "1",
            ),
            serialx.SerialException,
        )

    async def test_c4_backend_entry_errors_are_typed_and_retryable(self):
        async with self._esphome_hass("c4-b1") as (hass, _entry, clients):
            missing = _proxy_url("missing-entry", port_name="CDC Port")
            await self._assert_failed_proxy_open(
                hass,
                clients[-1],
                missing,
                InvalidSettingsError,
                "No ESPHome config entry with id 'missing-entry'",
            )

        async with self._esphome_hass("c4-b2") as (hass, entry, clients):
            endpoint = _url_fixtures(entry.entry_id)["S1c"][0]
            self.assertTrue(await hass.config_entries.async_unload(entry.entry_id))
            self.assertIs(entry.state, ConfigEntryState.NOT_LOADED)
            await self._assert_failed_proxy_open(
                hass,
                clients[-1],
                endpoint,
                InvalidSettingsError,
                f"ESPHome config entry {entry.entry_id!r} is not loaded",
            )

    async def test_c4_ordinary_url_grammar_is_unchanged(self):
        invalid = (
            "esphome-hass://esphome/?port_name=CDC",
            "esphome-hass://esphome/entry?port_name=",
            "esphome-hass://esphome/entry?port_name=CDC&mode=raw",
            "esphome-hass://esphome/entry?port_name=CDC&port_name=Other",
            "esphome-hass://esphome/entry",
            "esphome-hass://esphome/entry?port_usb_vid=nope",
            "esphome-hass://esphome/entry?port_usb_vid=-1",
            "http://esphome/entry?port_name=CDC",
            "esphome-hass://wrong/entry?port_name=CDC",
            "esphome-hass://user@esphome/entry?port_name=CDC",
            "esphome-hass://esphome:8123/entry?port_name=CDC",
            "esphome-hass://esphome/entry/extra?port_name=CDC",
            "esphome-hass://esphome/entry?port_name=CDC#fragment",
            "esphome-hass://esphome/entry?port_name=%ZZ",
            "esphome-hass://esphome/entry?port_name=CDC%0A",
        )
        for endpoint in invalid:
            with self.subTest(endpoint=endpoint):
                await self._assert_prevalidation_rejected(
                    endpoint, "invalid_serial_port"
                )

    async def test_c5_real_tcp_transport_opens_and_releases(self):
        payload = b"\x00\xfftcp"
        sent = asyncio.Event()

        async def on_client(_reader, writer):
            writer.write(payload)
            await writer.drain()
            sent.set()
            writer.close()
            await writer.wait_closed()

        server = await asyncio.start_server(on_client, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        try:
            source = await self.hass.async_add_executor_job(
                open_source,
                self.hass.loop,
                {"connection_type": "tcp", "host": "127.0.0.1", "port": port},
            )
            try:
                await asyncio.wait_for(sent.wait(), 2)
                self.assertEqual(
                    await self.hass.async_add_executor_job(source.read, 4096), payload
                )
            finally:
                await self.hass.async_add_executor_job(source.close)
        finally:
            server.close()
            await server.wait_closed()
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.bind(("127.0.0.1", port))

    async def test_c6_invalid_stored_settings_are_fatal_setup_errors(self):
        invalid_serial = (
            "esphome-hass://esphome/?port_name=CDC",
            "esphome-hass://esphome/entry?port_name=",
            "esphome-hass://esphome/entry?port_name=CDC&foo=bar",
            "esphome-hass://esphome/entry?port_name=CDC&port_name=Other",
            "esphome-hass://esphome/entry",
            "esphome-hass://esphome/entry?port_usb_vid=nope",
            "esphome-hass://esphome/entry?port_name=CDC%0A",
        )
        for index, endpoint in enumerate(invalid_serial):
            with self.subTest(endpoint=endpoint):
                entry = await self._unloaded_pytap_entry(f"invalid-{index}")
                self.hass.config_entries.async_update_entry(
                    entry,
                    data={
                        **entry.data,
                        "connection_type": "serial",
                        "serial_port": endpoint,
                    },
                )
                with self.assertRaises(ConfigEntryError) as caught:
                    await async_setup_entry(self.hass, entry)
                self.assertEqual(
                    caught.exception.translation_key, "invalid_connection_settings"
                )
                self.assertEqual(
                    caught.exception.translation_placeholders, {"field": "serial port"}
                )
                with patch(
                    "custom_components.pytap.connection.serialx.async_serial_for_url"
                ) as factory:
                    self.assertFalse(
                        await self.hass.config_entries.async_setup(entry.entry_id)
                    )
                self.assertIs(entry.state, ConfigEntryState.SETUP_ERROR)
                self.assertTrue(entry.reason)
                self.assertEqual(
                    entry.error_reason_translation_key,
                    "invalid_connection_settings",
                )
                factory.assert_not_called()

        entry = await self._unloaded_pytap_entry("invalid-tcp")
        self.hass.config_entries.async_update_entry(
            entry,
            data={"host": "gateway", "port": 0, "modules": [MODULE]},
        )
        with self.assertRaises(ConfigEntryError) as caught:
            await async_setup_entry(self.hass, entry)
        self.assertEqual(
            caught.exception.translation_key, "invalid_connection_settings"
        )
        self.assertEqual(caught.exception.translation_placeholders, {"field": "port"})
        self.assertFalse(await self.hass.config_entries.async_setup(entry.entry_id))
        self.assertIs(entry.state, ConfigEntryState.SETUP_ERROR)
        self.assertEqual(
            entry.error_reason_translation_key, "invalid_connection_settings"
        )

    async def test_c6_version_unsupported_stored_urls_are_actionable(self):
        cases = [
            (
                "S2c",
                "serial_port_name_required",
                (2026, 10),
            ),
            (
                "S7",
                "serial_port_name_required",
                (2026, 10),
            ),
            (
                "S2v",
                "serial_port_udev_suffix_unsupported",
                (2026, 10),
            ),
            (
                "S2v-encoded",
                "serial_port_udev_suffix_unsupported",
                (2026, 10),
            ),
            (
                "S3v",
                "serial_port_udev_suffix_unsupported",
                (2026, 10),
            ),
        ]
        if (MAJOR_VERSION, MINOR_VERSION) >= (2026, 11):
            return
        for name, expected, _versions in cases:
            with self.subTest(case=name):
                entry = await self._unloaded_pytap_entry(f"unsupported-{name}")
                endpoint = _url_fixtures(entry.entry_id)[name][0]
                self.hass.config_entries.async_update_entry(
                    entry,
                    data={
                        **entry.data,
                        "connection_type": "serial",
                        "serial_port": endpoint,
                    },
                )
                with patch(
                    "custom_components.pytap.connection.serialx.async_serial_for_url"
                ) as factory:
                    with self.assertRaises(ConfigEntryError) as caught:
                        await async_setup_entry(self.hass, entry)
                    self.assertEqual(caught.exception.translation_key, expected)
                    self.assertIsNone(caught.exception.translation_placeholders)
                    self.assertFalse(
                        await self.hass.config_entries.async_setup(entry.entry_id)
                    )
                    self.assertIs(entry.state, ConfigEntryState.SETUP_ERROR)
                    self.assertEqual(entry.error_reason_translation_key, expected)
                factory.assert_not_called()

    async def test_c6_backend_failures_stay_on_background_retry(self):
        async with self._esphome_hass("c6-b1", add_entry=False) as (hass, _entry, _clients):
            endpoint = _proxy_url("missing-entry", port_name="CDC Port")
            entry = self.pytap_serial_entry(endpoint, label="c6-b1")
            with (
                patch("custom_components.pytap.coordinator.RECONNECT_DELAY", 0.01),
                patch(
                    "custom_components.pytap.connection.serialx.async_serial_for_url",
                    wraps=serialx.async_serial_for_url,
                ) as factory,
                self.assertLogs(
                    "custom_components.pytap.coordinator", level="WARNING"
                ) as logs,
            ):
                await hass.config_entries.async_add(entry)
                self.assertIs(entry.state, ConfigEntryState.LOADED)
                await self._eventually(lambda: factory.call_count >= 2)
            self.assertFalse(any(endpoint in line for line in logs.output))
            self.assertFalse(any("missing-entry" in line for line in logs.output))
            self.assertTrue(
                await hass.config_entries.async_unload(entry.entry_id)
            )

        async with self._esphome_hass("c6-b2") as (hass, esphome_entry, clients):
            endpoint = _url_fixtures(esphome_entry.entry_id)["S1c"][0]
            self.assertTrue(
                await hass.config_entries.async_unload(esphome_entry.entry_id)
            )
            self.assertIs(esphome_entry.state, ConfigEntryState.NOT_LOADED)
            entry = self.pytap_serial_entry(endpoint, label="c6-b2")
            with (
                patch("custom_components.pytap.coordinator.RECONNECT_DELAY", 0.01),
                patch(
                    "custom_components.pytap.connection.serialx.async_serial_for_url",
                    wraps=serialx.async_serial_for_url,
                ) as factory,
            ):
                await hass.config_entries.async_add(entry)
                self.assertIs(entry.state, ConfigEntryState.LOADED)
                await self._eventually(lambda: factory.call_count >= 1)
                self.assertTrue(
                    await hass.config_entries.async_setup(esphome_entry.entry_id)
                )
                async with asyncio.timeout(5):
                    await esphome_entry.runtime_data.first_connect_done.wait()
                await self._eventually(lambda: bool(clients[-1].claimed))
            self.assertTrue(await hass.config_entries.async_unload(entry.entry_id))

    async def test_c6_matcher_failures_stay_on_background_retry(self):
        for case, key, value in (
            ("M1", "port_name", "Missing Port"),
            ("M4", "port_serial_number", "OTHER"),
        ):
            with self.subTest(case=case):
                async with self._esphome_hass(f"c6-{case}") as (
                    hass,
                    esphome_entry,
                    _clients,
                ):
                    endpoint = _changed_query(
                        _url_fixtures(esphome_entry.entry_id)["S6"][0], key, value
                    )
                    entry = self.pytap_serial_entry(endpoint, label=f"c6-{case}")
                    with (
                        patch(
                            "custom_components.pytap.coordinator.RECONNECT_DELAY",
                            0.01,
                        ),
                        patch(
                            "custom_components.pytap.connection.serialx.async_serial_for_url",
                            wraps=serialx.async_serial_for_url,
                        ) as factory,
                        self.assertLogs(
                            "custom_components.pytap.coordinator", level="WARNING"
                        ) as logs,
                    ):
                        await hass.config_entries.async_add(entry)
                        self.assertIs(entry.state, ConfigEntryState.LOADED)
                        await self._eventually(lambda: factory.call_count >= 2)
                    self.assertFalse(any(endpoint in line for line in logs.output))
                    self.assertTrue(
                        await hass.config_entries.async_unload(entry.entry_id)
                    )

    async def test_c6_tcp_failures_keep_background_retrying(self):
        entry = self.entry(
            version=7,
            title="retry-tcp",
            unique_id="retry-tcp-id",
            data={
                "connection_type": "tcp",
                "host": "127.0.0.1",
                "port": self._available_port(),
                "modules": [MODULE],
            },
        )
        self.hass.config.components.add("usb")
        with (
            patch("custom_components.pytap.coordinator.RECONNECT_DELAY", 0.01),
            patch(
                "custom_components.pytap.coordinator.open_source",
                wraps=open_source,
            ) as factory,
        ):
            await self.hass.config_entries.async_add(entry)
            self.assertIs(entry.state, ConfigEntryState.LOADED)
            await self._eventually(lambda: factory.call_count >= 2)
        self.assertIs(entry.state, ConfigEntryState.LOADED)
        self.assertTrue(await self.hass.config_entries.async_unload(entry.entry_id))

    async def test_c6_v7_and_version_supported_urls_open_without_downgrade(self):
        names = ["S1c"]
        if (MAJOR_VERSION, MINOR_VERSION) >= (2026, 11):
            names.extend(("S2c", "S7", "S2v", "S2v-encoded", "S3v"))
        for name in names:
            with self.subTest(name=name):
                async with self._esphome_hass(f"c6-open-{name}") as (
                    hass,
                    esphome_entry,
                    clients,
                ):
                    endpoint = _url_fixtures(esphome_entry.entry_id)[name][0]
                    entry = self.pytap_serial_entry(endpoint, label=f"c6-open-{name}")
                    with patch(
                        "custom_components.pytap.connection.serialx.async_serial_for_url",
                        wraps=serialx.async_serial_for_url,
                    ) as factory:
                        await hass.config_entries.async_add(entry)
                        self.assertIs(entry.state, ConfigEntryState.LOADED)
                        self.assertEqual(entry.version, 7)
                        await self._eventually(lambda: bool(clients[-1].claimed))
                    factory.assert_called()
                    self.assertTrue(
                        await hass.config_entries.async_unload(entry.entry_id)
                    )

    async def test_c7_exact_pr39_rollback_readability(self):
        archive = await asyncio.to_thread(_c7_archive)
        save_day = _c7_future_day()
        prescribed = datetime.fromisoformat(f"{save_day}T00:00:30").replace(
            tzinfo=zoneinfo.ZoneInfo("Pacific/Auckland")
        )
        prescribed_clock = prescribed.isoformat()
        prescribed_utc_clock = prescribed.astimezone(UTC).isoformat()
        self.assertNotEqual(prescribed.date(), prescribed.astimezone(UTC).date())
        for scenario in ("current", "removed", "stale"):
            with (
                self.subTest(scenario=scenario),
                TemporaryDirectory(prefix=f"pytap-c7-{scenario}-") as directory,
            ):
                config_dir = Path(directory)
                self.assertFalse(config_dir.resolve().is_relative_to(REPOSITORY.resolve()))
                target = config_dir / "custom_components" / "pytap"
                shutil.copytree(
                    REPOSITORY / "custom_components" / "pytap",
                    target,
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
                )
                candidate = await asyncio.to_thread(
                    _run_c7_worker,
                    config_dir,
                    scenario,
                    "candidate",
                    save_day,
                )
                self.assertEqual(candidate["phase"], "candidate")
                self.assertEqual(candidate["scenario"], scenario)
                self.assertEqual(candidate["save_clock"], prescribed_clock)
                self.assertEqual(candidate["save_utc_clock"], prescribed_utc_clock)
                for path in candidate["paths"].values():
                    if path is not None:
                        resolved = Path(path).resolve()
                        self.assertTrue(resolved.is_relative_to(config_dir.resolve()))
                        self.assertFalse(
                            resolved.is_relative_to(
                                (REPOSITORY / "custom_components").resolve()
                            )
                        )

                shutil.rmtree(target)
                with tarfile.open(fileobj=BytesIO(archive), mode="r:") as source:
                    source.extractall(config_dir, filter="data")

                baseline = await asyncio.to_thread(
                    _run_c7_worker,
                    config_dir,
                    scenario,
                    "baseline",
                    save_day,
                    candidate,
                )
                self.assertEqual(baseline["phase"], "baseline")
                self.assertEqual(baseline["scenario"], scenario)
                self.assertEqual(baseline["restore_clock"], prescribed_clock)
                self.assertEqual(
                    baseline["restore_utc_clock"], prescribed_utc_clock
                )
                self.assertEqual(
                    baseline["restore_clock"], candidate["save_clock"]
                )
                self.assertEqual(
                    baseline["restore_utc_clock"], candidate["save_utc_clock"]
                )
                for key, path in baseline["paths"].items():
                    if path is None:
                        self.assertEqual(key, "top_level_pytap")
                        continue
                    resolved = Path(path).resolve()
                    self.assertTrue(resolved.is_relative_to(target.resolve()))
                    self.assertFalse(
                        resolved.is_relative_to(
                            (REPOSITORY / "custom_components").resolve()
                        )
                    )

                daily = baseline["aggregates_before"]["daily_energy"]
                if scenario == "stale":
                    self.assertEqual(float(daily["native_value"]), 0.5)
                if scenario == "removed":
                    module_b = "C-2345678D"
                    self.assertNotIn(module_b, daily["baselines"])
                    self.assertNotIn(module_b, baseline["aggregates_after"]["total_energy"]["baselines"])
                if scenario == "current":
                    self.assertEqual(
                        daily["native_value"],
                        candidate["aggregates"]["daily_energy"]["extra"]["native_value"],
                    )

    async def test_c8_config_and_options_accept_supported_urls(self):
        endpoint_names = ["S1c"]
        if (MAJOR_VERSION, MINOR_VERSION) >= (2026, 11):
            endpoint_names.extend(("S2c", "S7", "S2v", "S3v"))
        for endpoint_name in endpoint_names:
            for options in (False, True):
                with self.subTest(case=endpoint_name, options=options):
                    await self._assert_flow_accepts(
                        f"c8-accept-{endpoint_name}",
                        endpoint_name,
                        options=options,
                    )

    async def test_c8_backend_error_flows_retry_and_resubmit(self):
        for kind in ("B1", "B2", "M4"):
            for options in (False, True):
                with self.subTest(kind=kind, options=options):
                    await self._assert_backend_flow(kind, options=options)

    async def test_c8_name_mismatch_never_saves_or_advances(self):
        for options in (False, True):
            with self.subTest(options=options):
                await self._assert_backend_flow("M1", options=options)

    async def test_c8_prevalidation_errors_never_probe_or_advance(self):
        cases = [
            (
                "esphome-hass://esphome/?port_name=CDC",
                "invalid_serial_port",
            ),
            (
                "esphome-hass://esphome/entry?port_name=",
                "invalid_serial_port",
            ),
            (
                "esphome-hass://esphome/entry?port_name=CDC&foo=bar",
                "invalid_serial_port",
            ),
            (
                "esphome-hass://esphome/entry?port_name=CDC&port_name=Other",
                "invalid_serial_port",
            ),
            (
                "esphome-hass://esphome/entry",
                "invalid_serial_port",
            ),
            (
                "esphome-hass://esphome/entry?port_usb_vid=nope",
                "invalid_serial_port",
            ),
            (
                "esphome-hass://esphome/entry?port_name=CDC%0A",
                "invalid_serial_port",
            ),
        ]
        if (MAJOR_VERSION, MINOR_VERSION) == (2026, 10):
            cases.extend(
                (
                    (
                        _url_fixtures("entry-one")["S2c"][0],
                        "serial_port_name_required",
                    ),
                    (
                        _url_fixtures("entry-one")["S7"][0],
                        "serial_port_name_required",
                    ),
                    (
                        _url_fixtures("entry-one")["S2v"][0],
                        "serial_port_udev_suffix_unsupported",
                    ),
                    (
                        _url_fixtures("entry-one")["S2v"][0].replace(
                            "-port0", "%2Dport0"
                        ),
                        "serial_port_udev_suffix_unsupported",
                    ),
                    (
                        _url_fixtures("entry-one")["S3v"][0],
                        "serial_port_udev_suffix_unsupported",
                    ),
                )
            )

        pytap_entry = await self._loaded_tcp_entry(self.hass, "prevalidation-options")
        for endpoint, error in cases:
            for options in (False, True):
                with self.subTest(endpoint=endpoint, options=options):
                    if options:
                        flow = await self._new_options_serial_flow(
                            self.hass, pytap_entry
                        )
                        submit = flow.async_step_change_serial
                    else:
                        flow = await self._new_user_serial_flow(self.hass)
                        submit = flow.async_step_serial
                    with patch(
                        "custom_components.pytap.connection.serialx.async_serial_for_url"
                    ) as factory:
                        first = await submit({"serial_port": endpoint})
                        second = await submit({"serial_port": endpoint})
                    self.assertEqual(first["errors"], {"serial_port": error})
                    self.assertEqual(second["errors"], {"serial_port": error})
                    factory.assert_not_called()
        self.assertNotIn("serial_port", pytap_entry.data)
