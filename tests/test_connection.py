"""Connection validation and bounded cleanup."""

import asyncio
import threading
from unittest.mock import patch

import pytest

from custom_components.pytap.config_flow import validate_connection
from custom_components.pytap.connection import (
    InvalidConnectionSettings,
    normalize_connection,
    open_source,
)


def test_connection_defaults_and_strips_host():
    assert normalize_connection({"host": " gateway "}) == {
        "host": "gateway",
        "port": 502,
    }


@pytest.mark.parametrize(
    ("data", "field"),
    [
        ({"host": " "}, "host"),
        ({"host": "gateway", "port": 0}, "port"),
        ({"host": "gateway", "port": 65536}, "port"),
        ({"host": "gateway", "port": True}, "port"),
    ],
)
def test_invalid_settings_never_open_a_source(data, field):
    with pytest.raises(InvalidConnectionSettings) as error:
        normalize_connection(data)
    assert error.value.field == field


async def test_tcp_open_failure_releases_socket(hass):
    with patch("custom_components.pytap.connection.TcpSource") as source_cls:
        source_cls.return_value.connect.side_effect = OSError("offline")
        with pytest.raises(OSError):
            await hass.async_add_executor_job(open_source, {"host": "gateway"})
        source_cls.return_value.close.assert_called_once()


async def test_probe_cancellation_still_closes_late_open(hass):
    opened = asyncio.Event()
    release = threading.Event()
    closed = asyncio.Event()

    def delayed_connect():
        hass.loop.call_soon_threadsafe(opened.set)
        assert release.wait(2)

    with patch("custom_components.pytap.connection.TcpSource") as source_cls:
        source = source_cls.return_value
        source.connect.side_effect = delayed_connect
        source.close.side_effect = lambda: hass.loop.call_soon_threadsafe(closed.set)
        task = asyncio.create_task(validate_connection(hass, {"host": "gateway"}))
        await opened.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        release.set()
        await closed.wait()
        await hass.async_block_till_done()
    source.close.assert_called_once()
    source.read.assert_not_called()
