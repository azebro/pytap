"""The worker must finish and release the port before unload/reload."""

import asyncio
from datetime import datetime
import threading
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.pytap.const import DOMAIN
from custom_components.pytap.coordinator import PyTapDataUpdateCoordinator
from custom_components.pytap.pytap.core.events import PowerReportEvent


def _coordinator(hass, serial=False):
    connection = {"connection_type": "tcp", "host": "gateway"}
    if serial:
        connection = {"connection_type": "serial", "serial_port": "/dev/fake-tigo"}
    entry = MockConfigEntry(domain=DOMAIN, version=6, data={**connection, "modules": []})
    entry.add_to_hass(hass)
    coordinator = PyTapDataUpdateCoordinator(hass, entry)
    coordinator._async_load_coordinator_state = AsyncMock()
    coordinator._async_save_coordinator_state = AsyncMock()
    return coordinator


@pytest.mark.parametrize("serial", [False, True])
async def test_stop_waits_for_read_and_close_without_blocking_loop(hass, serial):
    coordinator = _coordinator(hass, serial)
    reading = asyncio.Event()
    release = threading.Event()
    closed = asyncio.Event()
    source = MagicMock()

    def read(size):
        hass.loop.call_soon_threadsafe(reading.set)
        assert release.wait(2)
        return b""

    def close():
        # A real HA-aware close needs the HA loop to remain responsive.
        future = asyncio.run_coroutine_threadsafe(asyncio.sleep(0), hass.loop)
        future.result(timeout=2)
        hass.loop.call_soon_threadsafe(closed.set)

    source.read.side_effect = read
    source.close.side_effect = close
    with patch("custom_components.pytap.coordinator.open_source", return_value=source):
        await coordinator.async_start_listener()
        await reading.wait()
        stop = asyncio.create_task(coordinator.async_stop_listener())
        await asyncio.sleep(0)
        assert not stop.done()
        release.set()
        assert await stop is True
    assert closed.is_set()
    source.close.assert_called_once()
    assert coordinator._worker_future is None
    assert coordinator._midnight_reset_unsub is None
    assert await coordinator.async_stop_listener() is True
    source.close.assert_called_once()


async def test_snapshot_failure_cannot_skip_port_release(hass):
    coordinator = _coordinator(hass)
    source = MagicMock()
    source.read.side_effect = OSError("disconnected")
    with (
        patch("custom_components.pytap.coordinator.open_source", return_value=source),
        patch.object(coordinator, "_flush_pending_on_disconnect", side_effect=ValueError("bad snapshot")),
    ):
        with pytest.raises(ValueError, match="bad snapshot"):
            await coordinator._async_listen()
        assert await coordinator.async_stop_listener()
    source.close.assert_called_once()
    assert coordinator._worker_future is None
    assert coordinator._midnight_reset_unsub is None
    assert await coordinator.async_stop_listener() is True
    source.close.assert_called_once()


async def test_unload_during_open_closes_late_source(hass):
    coordinator = _coordinator(hass, serial=True)
    opening = asyncio.Event()
    release = threading.Event()
    source = MagicMock()

    def open_late(*args):
        hass.loop.call_soon_threadsafe(opening.set)
        assert release.wait(2)
        return source

    with patch("custom_components.pytap.coordinator.open_source", side_effect=open_late):
        await coordinator.async_start_listener()
        await opening.wait()
        stop = asyncio.create_task(coordinator.async_stop_listener())
        await asyncio.sleep(0)
        release.set()
        assert await stop is True
    source.read.assert_not_called()
    source.close.assert_called_once()


async def test_cancelled_wrapper_does_not_lose_worker(hass):
    coordinator = _coordinator(hass)
    entered = asyncio.Event()
    release = threading.Event()

    def worker():
        hass.loop.call_soon_threadsafe(entered.set)
        assert release.wait(2)

    with patch.object(coordinator, "_listen", new=worker):
        await coordinator.async_start_listener()
        await entered.wait()
        task = coordinator._listener_task
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert not coordinator._worker_future.done()
        release.set()
        assert await coordinator.async_stop_listener() is True


async def test_stop_timeout_blocks_replacement_until_worker_finishes(hass):
    coordinator = _coordinator(hass)
    entered = asyncio.Event()
    release = threading.Event()

    def worker():
        hass.loop.call_soon_threadsafe(entered.set)
        assert release.wait(2)

    with patch.object(coordinator, "_listen", new=worker):
        await coordinator.async_start_listener()
        await entered.wait()
        with patch("custom_components.pytap.coordinator.LISTENER_STOP_TIMEOUT", 0.01):
            assert await coordinator.async_stop_listener() is False
        with pytest.raises(RuntimeError, match="not stopped"):
            await coordinator.async_start_listener()
        release.set()
        assert await coordinator.async_stop_listener() is True


async def test_stop_during_retry_is_interruptible_and_logs_safely(hass, caplog):
    coordinator = _coordinator(hass, serial=True)
    attempted = asyncio.Event()

    def fail(*args):
        hass.loop.call_soon_threadsafe(attempted.set)
        raise OSError("esphome-hass://esphome/private-id?port_name=private")

    with patch("custom_components.pytap.coordinator.open_source", side_effect=fail):
        await coordinator.async_start_listener()
        await attempted.wait()
        async with asyncio.timeout(1):
            assert await coordinator.async_stop_listener() is True
    assert "private-id" not in caplog.text


async def test_proxy_reconnect_retains_endpoint(hass):
    coordinator = _coordinator(hass, serial=True)
    disconnected = MagicMock()
    disconnected.read.side_effect = OSError("proxy restarted")
    reconnected = MagicMock()

    def stop_after_reconnect(size):
        coordinator._stop_event.set()
        return b""

    reconnected.read.side_effect = stop_after_reconnect
    with (
        patch("custom_components.pytap.coordinator.RECONNECT_DELAY", 0.001),
        patch(
            "custom_components.pytap.coordinator.open_source",
            side_effect=[OSError("entry not loaded at startup"), disconnected, reconnected],
        ) as connect,
    ):
        async with asyncio.timeout(2):
            await coordinator._async_listen()
            assert await coordinator.async_stop_listener()
    assert connect.call_count == 3
    assert all(call.args[1]["serial_port"] == "/dev/fake-tigo" for call in connect.call_args_list)
    disconnected.close.assert_called_once()
    reconnected.close.assert_called_once()


@pytest.mark.parametrize("serial", [False, True])
async def test_both_transports_feed_identical_parser_pipeline(hass, serial):
    coordinator = _coordinator(hass, serial)
    coordinator._configured_barcodes = {"A-1234567B"}
    coordinator._schedule_save = MagicMock()
    parser = MagicMock()
    parser.infrastructure = {}
    event = PowerReportEvent(
        gateway_id=1,
        node_id=1,
        barcode="A-1234567B",
        voltage_in=30,
        voltage_out=30,
        current_in=4,
        temperature=25,
        dc_dc_duty_cycle=0.5,
        rssi=-60,
        timestamp=datetime.now(),
    )
    parser.feed.side_effect = [[], [event]]
    source = MagicMock()
    chunks = iter([b"first-fragment", b"second-fragment"])

    def read(size):
        try:
            return next(chunks)
        except StopIteration:
            coordinator._stop_event.set()
            return b""

    source.read.side_effect = read
    with (
        patch("custom_components.pytap.coordinator.open_source", return_value=source),
        patch("custom_components.pytap.pytap.api.create_parser", return_value=parser),
    ):
        await coordinator._async_listen()
        assert await coordinator.async_stop_listener()
    assert [call.args[0] for call in parser.feed.call_args_list] == [b"first-fragment", b"second-fragment"]
    assert coordinator._state["nodes"]["A-1234567B"]["power"] == 120
    assert coordinator._state["nodes"]["A-1234567B"]["readings_today"] == 1
    source.close.assert_called_once()
