"""Second review: lifecycle, Store safety and endpoint checks."""

import asyncio
import logging
import socket
import threading
from unittest.mock import AsyncMock, MagicMock, patch

from homeassistant import config_entries
from homeassistant.config_entries import ConfigEntryDisabler, ConfigEntryState
from homeassistant.const import EVENT_HOMEASSISTANT_STOP
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.pytap.config_flow import connection_claimed
from custom_components.pytap.const import DOMAIN
from custom_components.pytap.coordinator import PyTapDataUpdateCoordinator, parked_coordinators

BARCODE = "A-1234567B"
MODULE = {"barcode": BARCODE, "name": "East", "string": "East", "peak_power": 400}
LISTEN = "custom_components.pytap.coordinator.PyTapDataUpdateCoordinator._listen"
STOP_TIMEOUT = "custom_components.pytap.coordinator.LISTENER_STOP_TIMEOUT"
LOAD = "custom_components.pytap.coordinator._MigratingStore.async_load"
SAVE = "custom_components.pytap.coordinator._MigratingStore.async_save"


def _entry(hass, *, modules=None, **data):
    entry = MockConfigEntry(
        domain=DOMAIN,
        version=5,
        data={
            "host": "gateway",
            "port": 502,
            "modules": [MODULE] if modules is None else modules,
            **data,
        },
    )
    entry.add_to_hass(hass)
    return entry


def _idle_listen(self):
    self._stop_event.wait()


async def _wait_for(condition, timeout=2.0):
    async with asyncio.timeout(timeout):
        while not condition():
            await asyncio.sleep(0.01)


# --- Store safety ---


async def test_unreadable_store_is_never_overwritten(hass, caplog):
    entry = _entry(hass)
    save = AsyncMock()
    with patch(LISTEN, _idle_listen), patch(LOAD, AsyncMock(side_effect=OSError("io"))), patch(SAVE, save):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        coordinator = entry.runtime_data
        coordinator._unsaved_changes = True
        await coordinator._async_save_coordinator_state()
        assert await hass.config_entries.async_unload(entry.entry_id)
    save.assert_not_called()
    assert "won't be overwritten" in caplog.text


# --- Endpoint claims (#2) ---


async def test_parked_coordinators_endpoints_are_claimed(hass):
    """A deleted entry's stuck worker may still hold its gateway connection."""
    tcp = MagicMock()
    tcp.connection = {"host": "Gateway", "port": 502}
    parked_coordinators(hass).update({"deleted": tcp})
    settings = {"host": "gateway", "port": 502}
    assert connection_claimed(hass, settings)
    assert not connection_claimed(hass, settings, "deleted")  # Its own setup waits for it.
    assert not connection_claimed(hass, {"host": "gateway", "port": 503})

    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    with patch("custom_components.pytap.config_flow.validate_connection") as probe:
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"host": "gateway", "port": 502}
        )
    assert result["errors"] == {"base": "connection_in_use"}
    probe.assert_not_called()
    parked_coordinators(hass).clear()


# --- Slow stop (#2, #11) ---


def _stuck_worker(release: threading.Event, entered: threading.Event | None = None):
    def listen(self):
        if entered is not None:
            entered.set()
        release.wait(5)

    return listen


async def test_timed_out_stop_still_cancels_timers_and_pending_save(hass):
    entry = _entry(hass)
    coordinator = PyTapDataUpdateCoordinator(hass, entry)
    coordinator._async_load_coordinator_state = AsyncMock()
    release = threading.Event()
    with patch.object(PyTapDataUpdateCoordinator, "_listen", _stuck_worker(release)):
        await coordinator.async_start_listener()
        coordinator._save_task = hass.loop.call_later(60, lambda: None)
        save_task = coordinator._save_task
        with patch(STOP_TIMEOUT, 0.05):
            assert await coordinator.async_stop_listener() is False
        assert coordinator._midnight_reset_unsub is None
        assert coordinator._save_task is None and save_task.cancelled()
        assert coordinator._worker_future is not None  # Still guarding the port.
        release.set()
        assert await coordinator.async_stop_listener() is True


async def test_final_save_completes_when_stop_times_out(hass):
    entry = _entry(hass)
    coordinator = PyTapDataUpdateCoordinator(hass, entry)
    coordinator._async_load_coordinator_state = AsyncMock()
    saved = asyncio.Event()

    async def slow_save():
        await asyncio.sleep(0.2)
        saved.set()

    coordinator._async_save_coordinator_state = slow_save
    with patch.object(PyTapDataUpdateCoordinator, "_listen", _idle_listen):
        await coordinator.async_start_listener()
        coordinator._unsaved_changes = True
        with patch(STOP_TIMEOUT, 0.1):
            await coordinator.async_stop_listener()
    async with asyncio.timeout(1):
        await saved.wait()


async def test_disabled_entry_parked_coordinator_cleans_up_when_worker_exits(hass):
    entry = _entry(hass)
    release = threading.Event()
    with patch(LISTEN, _stuck_worker(release)), patch(STOP_TIMEOUT, 0.1):
        assert await hass.config_entries.async_setup(entry.entry_id)
        coordinator = entry.runtime_data
        await hass.config_entries.async_set_disabled_by(entry.entry_id, ConfigEntryDisabler.USER)
        assert entry.state is ConfigEntryState.NOT_LOADED
        assert parked_coordinators(hass)[entry.entry_id] is coordinator
        assert coordinator._midnight_reset_unsub is None
        release.set()
        await _wait_for(lambda: entry.entry_id not in parked_coordinators(hass))
        await hass.async_block_till_done()
    assert coordinator._worker_future is None


async def test_remove_waits_for_parked_listener_and_blocks_its_saves(hass):
    entry = _entry(hass)
    release = threading.Event()
    order = []
    save = AsyncMock(side_effect=lambda *_: order.append("save"))

    async def remove_store(*_):
        order.append("store")

    with (
        patch(LISTEN, _stuck_worker(release)),
        patch(SAVE, save),
        patch("custom_components.pytap.coordinator._MigratingStore.async_remove", remove_store),
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        coordinator = entry.runtime_data
        coordinator._unsaved_changes = True
        with patch(STOP_TIMEOUT, 0.1):
            assert await hass.config_entries.async_unload(entry.entry_id)
        assert parked_coordinators(hass)[entry.entry_id] is coordinator
        with patch(STOP_TIMEOUT, 5):
            removal = asyncio.create_task(hass.config_entries.async_remove(entry.entry_id))
            await asyncio.sleep(0.1)
            assert not removal.done() and order == []  # Waiting for the worker.
            hass.loop.call_soon(release.set)
            await removal
        await hass.async_block_till_done()
    assert order == ["store"]  # The stopped listener never saved.
    save.assert_not_called()
    assert entry.entry_id not in parked_coordinators(hass)


async def test_remove_with_still_stuck_listener_deletes_anyway_and_keeps_connection_claimed(hass, caplog):
    entry = _entry(hass)
    release = threading.Event()
    save = AsyncMock()
    with (
        patch(LISTEN, _stuck_worker(release)),
        patch(SAVE, save),
        patch("custom_components.pytap.coordinator._MigratingStore.async_remove") as remove_store,
        patch(STOP_TIMEOUT, 0.1),
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        coordinator = entry.runtime_data
        coordinator._unsaved_changes = True
        await hass.config_entries.async_remove(entry.entry_id)
        remove_store.assert_awaited_once()
        assert "deleting its saved state anyway" in caplog.text
        # The deleted entry's worker still holds the gateway connection.
        settings = {"host": "gateway", "port": 502}
        assert connection_claimed(hass, settings)
        release.set()
        await _wait_for(lambda: entry.entry_id not in parked_coordinators(hass))
        await hass.async_block_till_done()
    save.assert_not_called()  # Its final save can't recreate the deleted Store.
    assert not connection_claimed(hass, settings)


# --- Setup failures and recovery (#4, #7, #8) ---


async def test_setup_failure_after_start_stops_the_listener(hass):
    entry = _entry(hass)
    with (
        patch(LISTEN, _idle_listen),
        patch.object(
            hass.config_entries, "async_forward_entry_setups", AsyncMock(side_effect=RuntimeError("platform"))
        ),
    ):
        assert not await hass.config_entries.async_setup(entry.entry_id)
        coordinator = entry.runtime_data
    assert entry.state is ConfigEntryState.SETUP_ERROR
    assert coordinator._worker_future is None and coordinator._listener_task is None
    assert coordinator._midnight_reset_unsub is None


async def test_setup_failure_with_stuck_worker_parks_it(hass):
    entry = _entry(hass)
    release = threading.Event()
    with (
        patch(LISTEN, _stuck_worker(release)),
        patch(STOP_TIMEOUT, 0.1),
        patch.object(
            hass.config_entries, "async_forward_entry_setups", AsyncMock(side_effect=RuntimeError("platform"))
        ),
    ):
        assert not await hass.config_entries.async_setup(entry.entry_id)
        assert entry.entry_id in parked_coordinators(hass)
        release.set()
        await _wait_for(lambda: entry.entry_id not in parked_coordinators(hass))
        await hass.async_block_till_done()


async def test_unload_after_home_assistant_stop_logs_no_error(hass, caplog):
    entry = _entry(hass)
    with patch(LISTEN, _idle_listen):
        assert await hass.config_entries.async_setup(entry.entry_id)
        hass.bus.async_fire(EVENT_HOMEASSISTANT_STOP)
        await hass.async_block_till_done()
        assert entry.runtime_data._worker_future is None
        assert await hass.config_entries.async_unload(entry.entry_id)
    assert "Unable to remove unknown job listener" not in caplog.text


async def test_options_connection_fix_recovers_setup_error(hass):
    entry = _entry(hass, port=70000)
    assert not await hass.config_entries.async_setup(entry.entry_id)
    assert entry.state is ConfigEntryState.SETUP_ERROR
    with patch(LISTEN, _idle_listen):
        result = await hass.config_entries.options.async_init(entry.entry_id)
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {"next_step_id": "change_connection"}
        )
        with patch("custom_components.pytap.config_flow.validate_connection"):
            result = await hass.config_entries.options.async_configure(
                result["flow_id"], {"host": "gateway", "port": 502}
            )
        assert result["step_id"] == "init"
        await hass.async_block_till_done()
        assert entry.state is ConfigEntryState.LOADED
        assert await hass.config_entries.async_unload(entry.entry_id)


# --- Worker behaviour (#9, #10) ---


async def test_flapping_link_warns_once_and_logs_recovery_once(hass, caplog):
    entry = _entry(hass)
    coordinator = PyTapDataUpdateCoordinator(hass, entry)
    coordinator._schedule_save = MagicMock()

    def dropping_source():
        source = MagicMock()
        source.read.side_effect = ConnectionResetError("dropped")
        return source

    healthy = MagicMock()
    reads = iter([b"\x00", b"\x00"])

    def read(size):
        try:
            return next(reads)
        except StopIteration:
            coordinator._stop_event.set()
            return b""

    healthy.read.side_effect = read
    sources = [dropping_source() for _ in range(4)] + [healthy]
    caplog.set_level(logging.DEBUG, logger="custom_components.pytap.coordinator")
    with (
        patch("custom_components.pytap.coordinator.open_source", side_effect=sources),
        patch("custom_components.pytap.coordinator.RECONNECT_DELAY", 0),
    ):
        await hass.async_add_executor_job(coordinator._listen)
    records = [r for r in caplog.records if r.name == "custom_components.pytap.coordinator"]
    assert len([r for r in records if r.levelno == logging.WARNING]) == 1
    assert len([r for r in records if "recovered after 4 attempt" in r.message]) == 1
    assert len([r for r in records if r.message == "Connected to Tigo byte stream"]) == 1
    assert coordinator._retries == 0


async def test_clean_tcp_stop_logs_no_connection_error(hass, socket_enabled, caplog):
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    entry = _entry(hass, host="127.0.0.1", port=server.getsockname()[1])
    coordinator = PyTapDataUpdateCoordinator(hass, entry)
    coordinator._async_load_coordinator_state = AsyncMock()
    caplog.set_level(logging.DEBUG, logger="custom_components.pytap.coordinator")
    try:
        await coordinator.async_start_listener()
        peer, _ = await hass.async_add_executor_job(server.accept)
        await _wait_for(lambda: coordinator._source is not None)
        assert await coordinator.async_stop_listener() is True
        peer.close()
    finally:
        server.close()
    assert "Connected to Tigo byte stream" in caplog.text
    assert "unavailable" not in caplog.text
    assert "Connection attempt failed" not in caplog.text
