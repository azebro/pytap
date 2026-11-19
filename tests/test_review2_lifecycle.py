"""Second review: lifecycle, Store safety, device registry and endpoint checks."""

import asyncio
import logging
import os
import socket
import stat
import threading
from unittest.mock import AsyncMock, MagicMock, patch

from homeassistant import config_entries
from homeassistant.config_entries import ConfigEntryDisabler, ConfigEntryState
from homeassistant.const import EVENT_HOMEASSISTANT_STOP
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import device_registry as dr
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.pytap import async_remove_config_entry_device
from custom_components.pytap.config_flow import serial_port_claimed
from custom_components.pytap.connection import (
    InvalidConnectionSettings,
    SerialProxySource,
    check_device_node,
    normalize_connection,
)
from custom_components.pytap.const import (
    CONF_AUTO_DISCOVERY,
    CONF_EXCLUDED_BARCODES,
    CONF_SERIAL_PORT,
    DEFAULT_DISCOVERED_STRING,
    DOMAIN,
)
from custom_components.pytap.coordinator import (
    PyTapDataUpdateCoordinator,
    barcode_claims,
    parked_coordinators,
)

BARCODE = "A-1234567B"
DISCOVERED = "C-2345678D"
MODULE = {"barcode": BARCODE, "name": "East", "string": "East", "peak_power": 400}
LISTEN = "custom_components.pytap.coordinator.PyTapDataUpdateCoordinator._listen"
STOP_TIMEOUT = "custom_components.pytap.coordinator.LISTENER_STOP_TIMEOUT"
LOAD = "custom_components.pytap.coordinator._MigratingStore.async_load"
SAVE = "custom_components.pytap.coordinator._MigratingStore.async_save"


def _entry(hass, *, modules=None, auto=False, **data):
    entry = MockConfigEntry(
        domain=DOMAIN,
        version=7,
        data={
            "connection_type": "tcp",
            "host": "gateway",
            "port": 502,
            "modules": [MODULE] if modules is None else modules,
            CONF_AUTO_DISCOVERY: auto,
            CONF_EXCLUDED_BARCODES: [],
            **data,
        },
    )
    entry.add_to_hass(hass)
    return entry


def _idle_listen(self):
    self._stop_event.wait()


def _device(hass, entry, identifier):
    return dr.async_get(hass).async_get_or_create(
        config_entry_id=entry.entry_id, identifiers={(DOMAIN, identifier)}
    )


async def _wait_for(condition, timeout=2.0):
    async with asyncio.timeout(timeout):
        while not condition():
            await asyncio.sleep(0.01)


# --- Device registry (#1, #3, #6) ---


async def test_orphan_cleanup_uses_async_remove_device(hass, caplog):
    entry = _entry(hass, **{CONF_EXCLUDED_BARCODES: [DISCOVERED]})
    excluded = _device(hass, entry, DISCOVERED)
    registry = dr.async_get(hass)
    with (
        patch(LISTEN, _idle_listen),
        patch.object(registry, "async_update_device", wraps=registry.async_update_device) as update,
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        assert registry.async_get(excluded.id) is None
        assert all("remove_config_entry_id" not in call.kwargs for call in update.call_args_list)
        assert await hass.config_entries.async_unload(entry.entry_id)
    assert "remove_config_entry_id" not in caplog.text


async def test_empty_store_keeps_discovered_devices(hass):
    """A corrupt Store is renamed and loads as None: discovered devices must survive."""
    entry = _entry(hass, modules=[], auto=True)
    optimizer = _device(hass, entry, DISCOVERED)
    string = _device(hass, entry, f"{entry.entry_id}_string_{DEFAULT_DISCOVERED_STRING}")
    registry = dr.async_get(hass)
    with patch(LISTEN, _idle_listen), patch(LOAD, AsyncMock(return_value=None)):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        assert entry.runtime_data.store_loaded and not entry.runtime_data.store_restored
        assert registry.async_get(optimizer.id) is not None
        assert registry.async_get(string.id) is not None
        assert await hass.config_entries.async_unload(entry.entry_id)


async def test_unreadable_store_skips_cleanup_and_is_never_overwritten(hass, caplog):
    entry = _entry(hass, **{CONF_EXCLUDED_BARCODES: [DISCOVERED]})
    excluded = _device(hass, entry, DISCOVERED)
    empty_string = _device(hass, entry, f"{entry.entry_id}_string_Old")
    registry = dr.async_get(hass)
    save = AsyncMock()
    with patch(LISTEN, _idle_listen), patch(LOAD, AsyncMock(side_effect=OSError("io"))), patch(SAVE, save):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        coordinator = entry.runtime_data
        assert not coordinator.store_loaded
        assert registry.async_get(excluded.id) is not None
        assert registry.async_get(empty_string.id) is not None
        coordinator._unsaved_changes = True
        await coordinator._async_save_coordinator_state()
        assert await hass.config_entries.async_unload(entry.entry_id)
    save.assert_not_called()
    assert "won't be overwritten" in caplog.text


async def test_deleting_discovered_device_while_not_loaded_excludes_it(hass):
    entry = _entry(hass, modules=[MODULE], auto=True)
    assert entry.state is ConfigEntryState.NOT_LOADED
    assert await async_remove_config_entry_device(hass, entry, _device(hass, entry, DISCOVERED))
    assert entry.data[CONF_EXCLUDED_BARCODES] == [DISCOVERED]
    assert entry.data["modules"] == [MODULE]
    assert await async_remove_config_entry_device(hass, entry, _device(hass, entry, BARCODE))
    assert entry.data[CONF_EXCLUDED_BARCODES] == [BARCODE, DISCOVERED]
    assert entry.data["modules"] == []


async def test_deleting_discovered_device_of_loaded_entry_excludes_and_reloads(hass):
    entry = _entry(hass, modules=[MODULE], auto=True)
    with patch(LISTEN, _idle_listen):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        first = entry.runtime_data
        with first._state_lock:
            assert first._add_discovered_module(DISCOVERED, 2)
        device = _device(hass, entry, DISCOVERED)
        assert await async_remove_config_entry_device(hass, entry, device)
        await hass.async_block_till_done()
        assert entry.state is ConfigEntryState.LOADED
        assert entry.runtime_data is not first
        assert entry.data[CONF_EXCLUDED_BARCODES] == [DISCOVERED]
        assert entry.data["modules"] == [MODULE]
        assert DISCOVERED not in {module["barcode"] for module in entry.runtime_data.modules}
        assert not entry.runtime_data._add_discovered_module(DISCOVERED, 2)
        # The installation and a string that still has modules stay protected.
        assert not await async_remove_config_entry_device(
            hass, entry, _device(hass, entry, f"{entry.entry_id}_installation")
        )
        assert not await async_remove_config_entry_device(
            hass, entry, _device(hass, entry, f"{entry.entry_id}_string_East")
        )
        assert await hass.config_entries.async_unload(entry.entry_id)


# --- Endpoint validation (#5) and claims (#2) ---


@pytest.mark.parametrize(
    "path",
    [
        "/dev/../config/secrets.yaml",
        "/dev/./../etc/passwd",
        "/dev/serial/../../etc/shadow",
        "/dev/stdin",
        "/dev/stderr",
        "/dev/fd/0",
        "/dev/./fd/0",
        "/dev/shm/x",
        "/dev/mqueue/q",
        "/dev/",
        "/dev/..",
        "//dev/ttyUSB0",
    ],
)
def test_device_paths_are_normalised_before_checking(path):
    with pytest.raises(InvalidConnectionSettings) as error:
        normalize_connection({"connection_type": "serial", "serial_port": path})
    assert error.value.field == CONF_SERIAL_PORT


def test_normalised_device_path_is_saved():
    settings = normalize_connection({"connection_type": "serial", "serial_port": "/dev/./serial//ttyUSB0"})
    assert settings[CONF_SERIAL_PORT] == "/dev/serial/ttyUSB0"


def test_resolved_path_must_be_a_character_device(tmp_path):
    outside = tmp_path / "secrets.yaml"
    outside.touch()
    targets = {"/dev/serial/by-id/outside": str(outside), "/dev/serial/by-id/fd": "/dev/fd/3"}
    with patch("custom_components.pytap.connection.os.path.realpath", side_effect=targets.get):
        for link in targets:
            with pytest.raises(InvalidConnectionSettings):
                check_device_node(link)
    regular = os.stat_result((stat.S_IFREG | 0o600, *([0] * 9)))
    with (
        patch("custom_components.pytap.connection.os.path.realpath", return_value="/dev/ttyFAKE"),
        patch("custom_components.pytap.connection.os.stat", return_value=regular),
        pytest.raises(InvalidConnectionSettings),
    ):
        check_device_node("/dev/ttyFAKE")
    check_device_node("/dev/null")  # A real character device.
    check_device_node("esphome-hass://esphome/entry?port_name=bus")  # Not a path.


async def test_probe_rejects_symlink_to_a_file_without_opening(hass, tmp_path):
    outside = tmp_path / "secrets.yaml"
    outside.touch()
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"next_step_id": "serial"})
    with (
        patch("custom_components.pytap.connection.os.path.realpath", return_value=str(outside)),
        patch("custom_components.pytap.connection.serialx.async_serial_for_url") as open_port,
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_SERIAL_PORT: "/dev/serial/by-id/evil"}
        )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {CONF_SERIAL_PORT: "invalid_serial_port"}
    open_port.assert_not_called()


def _links(mapping):
    return patch(
        "custom_components.pytap.connection.os.path.realpath",
        side_effect=lambda path: mapping.get(path, path),
    )


async def test_symlinked_port_of_another_entry_is_claimed_in_the_flow(hass):
    _entry(hass, connection_type="serial", serial_port="/dev/ttyUSB0", baud_rate=38400)
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"next_step_id": "serial"})
    with (
        _links({"/dev/serial/by-id/tigo": "/dev/ttyUSB0"}),
        patch("custom_components.pytap.config_flow.validate_connection") as probe,
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_SERIAL_PORT: "/dev/serial/by-id/tigo"}
        )
    assert result["errors"] == {CONF_SERIAL_PORT: "serial_port_in_use"}
    probe.assert_not_called()


async def test_parked_coordinators_endpoints_are_claimed(hass):
    """A deleted entry's stuck worker may still hold its port or gateway connection."""
    parked = MagicMock()
    parked.connection = {"connection_type": "serial", "serial_port": "/dev/ttyUSB0", "baud_rate": 38400}
    tcp = MagicMock()
    tcp.connection = {"connection_type": "tcp", "host": "Gateway", "port": 502}
    parked_coordinators(hass).update({"deleted": parked, "other": tcp})
    serial = {"connection_type": "serial", "serial_port": "/dev/serial/by-id/tigo", "baud_rate": 38400}
    with _links({"/dev/serial/by-id/tigo": "/dev/ttyUSB0"}):
        assert await serial_port_claimed(hass, serial)
        assert not await serial_port_claimed(hass, serial, "deleted")  # Its own setup waits for it.
    assert await serial_port_claimed(hass, {"connection_type": "tcp", "host": "gateway", "port": 502})
    assert not await serial_port_claimed(hass, {"connection_type": "tcp", "host": "gateway", "port": 503})

    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"next_step_id": "tcp"})
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
        assert coordinator._freshness_unsub is None
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
        assert barcode_claims(hass).claim(BARCODE, "another") is False
        await hass.config_entries.async_set_disabled_by(entry.entry_id, ConfigEntryDisabler.USER)
        assert entry.state is ConfigEntryState.NOT_LOADED
        assert parked_coordinators(hass)[entry.entry_id] is coordinator
        assert coordinator._midnight_reset_unsub is None and coordinator._freshness_unsub is None
        release.set()
        await _wait_for(lambda: entry.entry_id not in parked_coordinators(hass))
        await hass.async_block_till_done()
    assert coordinator._worker_future is None
    # Its optimizers are free for other entries again.
    assert barcode_claims(hass).claim(BARCODE, "another") is True


async def test_remove_waits_for_parked_listener_and_blocks_its_saves(hass):
    entry = _entry(hass)
    release = threading.Event()
    order = []
    save = AsyncMock(side_effect=lambda *_: order.append("save"))

    async def remove_captures(*_):
        order.append("captures")

    async def remove_store(*_):
        order.append("store")

    with (
        patch(LISTEN, _stuck_worker(release)),
        patch(SAVE, save),
        patch("custom_components.pytap.async_remove_captures", remove_captures),
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
    assert order == ["captures", "store"]  # The stopped listener never saved.
    save.assert_not_called()
    assert entry.entry_id not in parked_coordinators(hass)


async def test_remove_with_still_stuck_listener_deletes_anyway_and_keeps_port_claimed(hass, caplog):
    entry = _entry(hass, connection_type="serial", serial_port="/dev/ttyUSB0", baud_rate=38400)
    release = threading.Event()
    save = AsyncMock()
    with (
        patch(LISTEN, _stuck_worker(release)),
        patch(SAVE, save),
        patch("custom_components.pytap.async_remove_captures") as remove_captures,
        patch("custom_components.pytap.coordinator._MigratingStore.async_remove") as remove_store,
        patch(STOP_TIMEOUT, 0.1),
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        coordinator = entry.runtime_data
        coordinator._unsaved_changes = True
        await hass.config_entries.async_remove(entry.entry_id)
        remove_captures.assert_awaited_once()
        remove_store.assert_awaited_once()
        assert "deleting its captures and saved state anyway" in caplog.text
        # The deleted entry's worker still holds the port.
        settings = {"connection_type": "serial", "serial_port": "/dev/ttyUSB0", "baud_rate": 38400}
        assert await serial_port_claimed(hass, settings)
        release.set()
        await _wait_for(lambda: entry.entry_id not in parked_coordinators(hass))
        await hass.async_block_till_done()
    save.assert_not_called()  # Its final save can't recreate the deleted Store.
    assert not await serial_port_claimed(hass, settings)


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
    assert coordinator._midnight_reset_unsub is None and coordinator._freshness_unsub is None
    assert barcode_claims(hass).claim(BARCODE, "another") is True


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
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {"next_step_id": "change_tcp"}
        )
        with patch("custom_components.pytap.config_flow.validate_connection"):
            result = await hass.config_entries.options.async_configure(
                result["flow_id"], {"host": "gateway", "port": 502}
            )
        assert result["step_id"] == "init"
        await hass.async_block_till_done()
        assert entry.state is ConfigEntryState.LOADED
        assert await hass.config_entries.async_unload(entry.entry_id)


async def test_invalid_connection_type_names_the_setting(hass):
    entry = _entry(hass, connection_type="carrier-pigeon")
    assert not await hass.config_entries.async_setup(entry.entry_id)
    assert entry.error_reason_translation_placeholders == {"field": "connection type"}


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


async def test_clean_tcp_stop_records_no_connection_error(hass, socket_enabled):
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    entry = _entry(hass, host="127.0.0.1", port=server.getsockname()[1])
    coordinator = PyTapDataUpdateCoordinator(hass, entry)
    coordinator._async_load_coordinator_state = AsyncMock()
    calls = []
    capture = MagicMock(busy=False)
    capture.mark.side_effect = lambda kind, *args, **kwargs: calls.append(kind)
    capture.request_stop.side_effect = lambda reason: calls.append("request_stop")
    coordinator._capture = capture
    interrupt = coordinator._interrupt_source

    def tracked_interrupt():
        calls.append("interrupt")
        interrupt()

    coordinator._interrupt_source = tracked_interrupt
    try:
        await coordinator.async_start_listener()
        peer, _ = await hass.async_add_executor_job(server.accept)
        await _wait_for(lambda: coordinator._source is not None)
        assert await coordinator.async_stop_listener() is True
        peer.close()
    finally:
        server.close()
    assert "connection_open" in calls
    assert "connection_error" not in calls
    assert calls.index("request_stop") < calls.index("interrupt")


# --- Serial bridge (#5 test gap) ---


def test_serial_run_times_out_when_the_loop_is_stopped():
    """A stopped HA loop must not leave the worker waiting forever."""
    loop = asyncio.new_event_loop()
    try:
        with patch("custom_components.pytap.connection.serialx.async_serial_for_url"):
            source = SerialProxySource(
                loop, {"serial_port": "/dev/ttyUSB0", "baud_rate": 38400, "connection_type": "serial"}
            )
        outcome = []

        def worker():
            try:
                source._run(asyncio.sleep(10))
            except TimeoutError:
                outcome.append("timeout")

        with patch("custom_components.pytap.connection.CONNECTION_TIMEOUT", -4.95):
            thread = threading.Thread(target=worker)
            thread.start()
            thread.join(2)
        assert not thread.is_alive()
        assert outcome == ["timeout"]
        # The submitted coroutine is cancelled, not left to run later.
        loop.run_until_complete(asyncio.sleep(0))
        assert not asyncio.all_tasks(loop)
    finally:
        loop.close()
