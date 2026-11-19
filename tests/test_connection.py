"""Transport validation, passive reads, and bounded cleanup."""

import asyncio
import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import serialx

from custom_components.pytap.config_flow import validate_connection
from custom_components.pytap.connection import (
    InvalidConnectionSettings,
    SerialProxySource,
    connection_title,
    normalize_connection,
    open_source,
    serial_endpoint_key,
)
from custom_components.pytap.const import (
    CONF_BAUD_RATE,
    CONF_CONNECTION_TYPE,
    CONF_SERIAL_PORT,
    CONNECTION_SERIAL,
    CONNECTION_TCP,
    CONNECTION_TIMEOUT,
)

PROXY_URL = "esphome-hass://esphome/entry-one?port_name=Tigo%20Bus&port_usb_vid=0x1234"
SERIAL_SETTINGS = {
    CONF_CONNECTION_TYPE: CONNECTION_SERIAL,
    CONF_SERIAL_PORT: PROXY_URL,
    CONF_BAUD_RATE: 38400,
}


def test_legacy_connection_defaults_to_tcp():
    assert normalize_connection({"host": " gateway "}) == {
        CONF_CONNECTION_TYPE: CONNECTION_TCP,
        "host": "gateway",
        "port": 502,
    }


def test_equivalent_endpoints_have_same_claim_key():
    assert serial_endpoint_key(PROXY_URL) == serial_endpoint_key(
        "esphome-hass://esphome/entry-one?port_usb_vid=0x1234&port_name=Tigo+Bus"
    )
    assert serial_endpoint_key("/dev/./ttyUSB0") == serial_endpoint_key("/dev/ttyUSB0")


@pytest.mark.parametrize("endpoint", [PROXY_URL, "/dev/serial/by-id/tigo"])
def test_serial_settings_preserve_endpoint(endpoint):
    settings = normalize_connection({CONF_CONNECTION_TYPE: CONNECTION_SERIAL, CONF_SERIAL_PORT: endpoint})
    assert settings[CONF_SERIAL_PORT] == endpoint
    assert settings[CONF_BAUD_RATE] == 38400
    assert endpoint not in connection_title(settings)


@pytest.mark.parametrize(
    ("data", "field"),
    [
        ({"host": " "}, "host"),
        ({"host": "gateway", "port": 0}, "port"),
        ({"host": "gateway", "port": 65536}, "port"),
        ({"host": "gateway", "port": True}, "port"),
        ({CONF_CONNECTION_TYPE: "unknown"}, "base"),
        ({**SERIAL_SETTINGS, CONF_BAUD_RATE: 0}, CONF_BAUD_RATE),
        ({**SERIAL_SETTINGS, CONF_BAUD_RATE: True}, CONF_BAUD_RATE),
        ({**SERIAL_SETTINGS, CONF_SERIAL_PORT: ""}, CONF_SERIAL_PORT),
        ({**SERIAL_SETTINGS, CONF_SERIAL_PORT: "relative/path"}, CONF_SERIAL_PORT),
        ({**SERIAL_SETTINGS, CONF_SERIAL_PORT: "socket://example:1234"}, CONF_SERIAL_PORT),
        ({**SERIAL_SETTINGS, CONF_SERIAL_PORT: "esphome-hass://esphome/?port_name=bus"}, CONF_SERIAL_PORT),
        ({**SERIAL_SETTINGS, CONF_SERIAL_PORT: "esphome-hass://esphome/entry"}, CONF_SERIAL_PORT),
        ({**SERIAL_SETTINGS, CONF_SERIAL_PORT: PROXY_URL + "&mode=protocol"}, CONF_SERIAL_PORT),
        ({**SERIAL_SETTINGS, CONF_SERIAL_PORT: PROXY_URL + "&password=secret"}, CONF_SERIAL_PORT),
        ({**SERIAL_SETTINGS, CONF_SERIAL_PORT: PROXY_URL + "&port_name=other"}, CONF_SERIAL_PORT),
        ({**SERIAL_SETTINGS, CONF_SERIAL_PORT: PROXY_URL + "&port_usb_pid=nope"}, CONF_SERIAL_PORT),
        ({**SERIAL_SETTINGS, CONF_SERIAL_PORT: PROXY_URL + "%ZZ"}, CONF_SERIAL_PORT),
    ],
)
def test_invalid_settings_never_open_a_source(data, field):
    with pytest.raises(InvalidConnectionSettings) as error:
        normalize_connection(data)
    assert error.value.field == field


@pytest.fixture
def async_serial():
    port = MagicMock()
    port.open = AsyncMock()
    port.read = AsyncMock(return_value=b"\x00\xffpayload")
    port.close = AsyncMock()
    with patch(
        "custom_components.pytap.connection.serialx.async_serial_for_url", return_value=port
    ) as factory:
        yield port, factory


async def test_passive_serial_source(hass, async_serial):
    port, factory = async_serial
    source = await hass.async_add_executor_job(open_source, hass.loop, SERIAL_SETTINGS)
    try:
        assert await hass.async_add_executor_job(source.read, 4096) == b"\x00\xffpayload"
        kwargs = factory.call_args.kwargs
        assert factory.call_args.args == (PROXY_URL,)
        assert kwargs["baudrate"] == 38400
        assert kwargs["byte_size"] == 8
        assert kwargs["parity"] is serialx.Parity.NONE
        assert kwargs["stopbits"] is serialx.StopBits.ONE
        assert kwargs["connect_timeout"] == CONNECTION_TIMEOUT
        for key in ("xonxoff", "rtscts", "dsrdtr"):
            assert kwargs[key] is False
        assert kwargs["rts_on_open"] is serialx.PinState.UNDEFINED
        port.write.assert_not_called()
    finally:
        await hass.async_add_executor_job(source.close)
    port.open.assert_awaited_once()
    port.close.assert_awaited_once()


async def test_idle_timeout_is_not_eof(hass, async_serial):
    port, _ = async_serial
    source = SerialProxySource(hass.loop, SERIAL_SETTINGS)

    async def idle(size):
        await asyncio.Future()

    port.read.side_effect = idle
    with patch("custom_components.pytap.connection.SERIAL_READ_TIMEOUT", 0.01):
        assert await hass.async_add_executor_job(source.read, 10) == b""
    port.read.side_effect = TimeoutError("transport failure")
    with pytest.raises(TimeoutError):
        await hass.async_add_executor_job(source.read, 10)
    port.read.side_effect = None
    port.read.return_value = b""
    with pytest.raises(ConnectionResetError):
        await hass.async_add_executor_job(source.read, 10)
    port.read.side_effect = OSError("disconnected")
    with pytest.raises(OSError):
        await hass.async_add_executor_job(source.read, 10)


async def test_failed_open_closes_source(hass, async_serial):
    port, _ = async_serial
    port.open.side_effect = OSError("offline")
    with pytest.raises(OSError):
        await hass.async_add_executor_job(open_source, hass.loop, SERIAL_SETTINGS)
    port.close.assert_awaited_once()


async def test_open_timeout_cleans_up(hass, async_serial):
    port, _ = async_serial

    async def never_open():
        await asyncio.Future()

    port.open.side_effect = never_open
    with patch("custom_components.pytap.connection.CONNECTION_TIMEOUT", 0.01):
        with pytest.raises(TimeoutError):
            await hass.async_add_executor_job(open_source, hass.loop, SERIAL_SETTINGS)
    port.close.assert_awaited_once()


async def test_probe_cancellation_still_closes_late_open(hass, async_serial):
    port, _ = async_serial
    opened = asyncio.Event()
    release = asyncio.Event()
    closed = asyncio.Event()
    port.close.side_effect = closed.set

    async def delayed_open():
        opened.set()
        await release.wait()

    port.open.side_effect = delayed_open
    task = asyncio.create_task(validate_connection(hass, SERIAL_SETTINGS))
    await opened.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    release.set()
    await closed.wait()
    await hass.async_block_till_done()
    port.close.assert_awaited_once()
    port.read.assert_not_called()


async def test_close_timeout_aborts_transport(hass, async_serial):
    port, _ = async_serial

    async def never_close():
        await asyncio.Future()

    port.close.side_effect = never_close
    source = SerialProxySource(hass.loop, SERIAL_SETTINGS)
    with patch("custom_components.pytap.connection.CONNECTION_TIMEOUT", 0.01):
        with pytest.raises(TimeoutError):
            await hass.async_add_executor_job(source.close)
    port.abort.assert_called_once()


async def test_sync_api_rejects_event_loop_calls(hass, async_serial):
    source = SerialProxySource(hass.loop, SERIAL_SETTINGS)
    with pytest.raises(RuntimeError, match="executor"):
        source.connect()


async def test_tcp_open_failure_releases_socket(hass):
    with patch("custom_components.pytap.connection.TcpSource") as source_cls:
        source_cls.return_value.connect.side_effect = OSError("offline")
        with pytest.raises(OSError):
            await hass.async_add_executor_job(open_source, hass.loop, {"host": "gateway"})
        source_cls.return_value.close.assert_called_once()


@pytest.mark.skipif(not hasattr(os, "openpty"), reason="Pseudo-terminals require POSIX")
async def test_real_serialx_transport_round_trip_and_release(hass):
    """Use a virtual UART to test the public transport, not just our mocks."""
    master, slave = os.openpty()
    try:
        settings = {
            CONF_CONNECTION_TYPE: CONNECTION_SERIAL,
            CONF_SERIAL_PORT: os.ttyname(slave),
        }
        source = await hass.async_add_executor_job(open_source, hass.loop, settings)
        try:
            os.write(master, b"\x00\xfftigo\x7e")
            assert await hass.async_add_executor_job(source.read, 4096) == b"\x00\xfftigo\x7e"
        finally:
            await hass.async_add_executor_job(source.close)
        await hass.async_add_executor_job(source.close)
        # Re-opening proves that the first session released its exclusive port.
        source = await hass.async_add_executor_job(open_source, hass.loop, settings)
        await hass.async_add_executor_job(source.close)
    finally:
        os.close(master)
        os.close(slave)
