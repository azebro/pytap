"""Native serial picker, offline setup, and transport changes."""

from unittest.mock import patch

from homeassistant import config_entries
from homeassistant.components.usb import (
    SerialDevice,
    async_register_serial_port_scanner,
    async_scan_serial_ports,
)
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers.selector import SerialPortSelector
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry
import serialx

from custom_components.pytap.const import (
    CONF_BAUD_RATE,
    CONF_CONNECTION_TYPE,
    CONF_MODULES,
    CONF_SERIAL_PORT,
    CONNECTION_SERIAL,
    CONNECTION_TCP,
    DOMAIN,
)

PROXY_URL = "esphome-hass://esphome/entry-one?port_name=Tigo%20Bus"
IDENTITY_URL = "esphome-hass://esphome/entry-one?port_udev_id=usb-Tigo_AbC-if00-port0"
MODULES = [{"string": "A", "name": "Panel", "barcode": "A-1234567B", "peak_power": 455}]


@pytest.fixture(autouse=True)
def mock_entry_setup():
    with patch("custom_components.pytap.async_setup_entry", return_value=True):
        yield


async def _serial_flow(hass):
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    return await hass.config_entries.flow.async_configure(result["flow_id"], {"next_step_id": "serial"})


async def _connection_options(hass, entry, step="change_serial"):
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "change_connection"}
    )
    assert result["menu_options"] == ["change_tcp", "change_serial"]
    return await hass.config_entries.options.async_configure(result["flow_id"], {"next_step_id": step})


def _entry(hass, serial=False):
    data = {"host": "gateway", "port": 502, CONF_CONNECTION_TYPE: CONNECTION_TCP}
    if serial:
        data = {CONF_CONNECTION_TYPE: CONNECTION_SERIAL, CONF_SERIAL_PORT: PROXY_URL, CONF_BAUD_RATE: 38400}
    entry = MockConfigEntry(
        domain=DOMAIN,
        version=6,
        unique_id="stable-pytap-id",
        data={**data, CONF_MODULES: MODULES, "live_reporting": False, "write_interval": 60},
    )
    entry.add_to_hass(hass)
    return entry


async def test_serial_form_uses_native_picker(hass):
    result = await _serial_flow(hass)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "serial"
    schema = {str(k): v for k, v in result["data_schema"].schema.items()}
    assert isinstance(schema[CONF_SERIAL_PORT], SerialPortSelector)
    assert CONF_BAUD_RATE in schema
    assert "host" not in schema


async def test_native_discovery_includes_multiple_proxy_ports(hass):
    await _serial_flow(hass)  # Also sets up the required USB websocket integration.
    ports = [
        SerialDevice(device=PROXY_URL, manufacturer="ESPHome", description="Tigo Bus", serial_number="one"),
        SerialDevice(
            device=IDENTITY_URL, manufacturer="ESPHome", description="Other Bus", serial_number="two"
        ),
    ]
    unsub = async_register_serial_port_scanner(hass, lambda hass: ports)
    try:
        found = await async_scan_serial_ports(hass)
        assert {p.device for p in found} == {PROXY_URL, IDENTITY_URL}
    finally:
        unsub()


@pytest.mark.parametrize("endpoint", [PROXY_URL, IDENTITY_URL, "/dev/serial/by-id/tigo"])
async def test_complete_offline_serial_setup(hass, endpoint):
    result = await _serial_flow(hass)
    with patch("custom_components.pytap.config_flow.validate_connection", side_effect=OSError("offline")):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_SERIAL_PORT: endpoint}
        )
        assert result["errors"] == {"base": "cannot_connect"}
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_SERIAL_PORT: endpoint}
        )
    assert result["step_id"] == "modules_menu"
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"next_step_id": "add_module"})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], MODULES[0])
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"next_step_id": "finish"})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_SERIAL_PORT] == endpoint
    assert result["data"][CONF_CONNECTION_TYPE] == CONNECTION_SERIAL
    assert result["data"][CONF_BAUD_RATE] == 38400
    assert "host" not in result["data"]
    assert result["result"].version == 7
    assert result["result"].unique_id
    assert endpoint not in result["title"]


@pytest.mark.parametrize(
    ("input_data", "errors"),
    [
        ({CONF_SERIAL_PORT: " "}, {CONF_SERIAL_PORT: "invalid_serial_port"}),
        ({CONF_SERIAL_PORT: PROXY_URL, CONF_BAUD_RATE: 0}, {CONF_BAUD_RATE: "invalid_baud_rate"}),
    ],
)
async def test_invalid_serial_settings_stay_on_form(hass, input_data, errors):
    result = await _serial_flow(hass)
    with patch("custom_components.pytap.config_flow.validate_connection") as probe:
        result = await hass.config_entries.flow.async_configure(result["flow_id"], input_data)
    assert result["errors"] == errors
    probe.assert_not_called()


async def test_unsupported_settings_block_setup(hass):
    result = await _serial_flow(hass)
    with patch(
        "custom_components.pytap.config_flow.validate_connection", side_effect=serialx.UnsupportedSetting
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_SERIAL_PORT: PROXY_URL}
        )
    assert result["errors"] == {"base": "unsupported_serial_settings"}


async def test_duplicate_serial_endpoint_is_not_probed(hass):
    _entry(hass, serial=True)
    result = await _serial_flow(hass)
    with patch("custom_components.pytap.config_flow.validate_connection") as probe:
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_SERIAL_PORT: PROXY_URL}
        )
    assert result["errors"] == {CONF_SERIAL_PORT: "serial_port_in_use"}
    probe.assert_not_called()


async def test_options_switch_transport_preserves_identity_and_data(hass):
    entry = _entry(hass)
    entry_id, unique_id = entry.entry_id, entry.unique_id
    result = await _connection_options(hass, entry)
    with patch("custom_components.pytap.config_flow.validate_connection", side_effect=OSError("offline")):
        for _ in range(2):  # Unreachable: the second identical submit saves.
            result = await hass.config_entries.options.async_configure(
                result["flow_id"], {CONF_SERIAL_PORT: PROXY_URL}
            )
    assert result["step_id"] == "init"
    assert entry.data[CONF_SERIAL_PORT] == PROXY_URL
    assert "host" not in entry.data and "port" not in entry.data
    assert entry.data[CONF_MODULES] == MODULES
    assert entry.data["live_reporting"] is False
    assert entry.data["write_interval"] == 60

    result = await _connection_options(hass, entry, "change_tcp")
    with patch("custom_components.pytap.config_flow.validate_connection"):
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {"host": "new-host", "port": 503}
        )
    assert entry.data[CONF_CONNECTION_TYPE] == CONNECTION_TCP
    assert CONF_SERIAL_PORT not in entry.data and CONF_BAUD_RATE not in entry.data
    assert entry.data[CONF_MODULES] == MODULES
    assert entry.entry_id == entry_id and entry.unique_id == unique_id


async def test_options_prefill_offline_current_port_and_skip_probe(hass):
    entry = _entry(hass, serial=True)
    result = await _connection_options(hass, entry)
    defaults = {str(k): k.default() for k in result["data_schema"].schema}
    assert defaults[CONF_SERIAL_PORT] == PROXY_URL
    with patch("custom_components.pytap.config_flow.validate_connection") as probe:
        await hass.config_entries.options.async_configure(
            result["flow_id"], {CONF_SERIAL_PORT: PROXY_URL, CONF_BAUD_RATE: 19200}
        )
    probe.assert_not_called()
    assert entry.data[CONF_BAUD_RATE] == 19200


async def test_options_change_proxy_preserves_url(hass):
    entry = _entry(hass, serial=True)
    result = await _connection_options(hass, entry)
    with patch("custom_components.pytap.config_flow.validate_connection") as probe:
        await hass.config_entries.options.async_configure(result["flow_id"], {CONF_SERIAL_PORT: IDENTITY_URL})
    probe.assert_awaited_once()
    assert entry.data[CONF_SERIAL_PORT] == IDENTITY_URL


async def test_connection_warning_does_not_log_endpoint(hass, caplog):
    result = await _serial_flow(hass)
    with patch("custom_components.pytap.config_flow.validate_connection", side_effect=OSError(PROXY_URL)):
        await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_SERIAL_PORT: PROXY_URL})
    assert PROXY_URL not in caplog.text
