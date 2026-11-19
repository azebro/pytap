"""Regression tests for review findings: lifecycle, registry and connection safety."""

import asyncio
import logging
import os
import socket
import threading
import time
from unittest.mock import MagicMock, patch

from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import EVENT_HOMEASSISTANT_STOP
from homeassistant.helpers import device_registry as dr
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.pytap import async_remove_config_entry_device
from custom_components.pytap.connection import (
    InvalidConnectionSettings,
    normalize_connection,
    resolved_endpoint_key,
)
from custom_components.pytap.const import CONF_AUTO_DISCOVERY, CONF_EXCLUDED_BARCODES, DOMAIN
from custom_components.pytap.pytap.core.source import TcpSource

BARCODE = "A-1234567B"
OTHER = "C-2345678D"
MODULE = {"barcode": BARCODE, "name": "East", "string": "East", "peak_power": 400}


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
    """A worker that behaves like an idle connection until told to stop."""
    self._stop_event.wait()


def test_tcp_interrupt_wakes_a_blocked_read(socket_enabled):
    """Review #4: stop must not wait for the 10 s socket timeout."""
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    source = TcpSource("127.0.0.1", server.getsockname()[1])
    source.connect()
    peer, _ = server.accept()
    outcome = []

    def reader():
        try:
            source.read(10)
        except OSError as err:
            outcome.append(type(err))

    thread = threading.Thread(target=reader)
    thread.start()
    time.sleep(0.1)
    started = time.monotonic()
    source.interrupt()
    thread.join(2)
    elapsed = time.monotonic() - started
    source.close()
    peer.close()
    server.close()
    assert not thread.is_alive()
    assert elapsed < 1
    assert outcome == [ConnectionResetError]


async def test_stop_interrupts_the_active_source(hass):
    entry = _entry(hass)
    with patch("custom_components.pytap.coordinator.PyTapDataUpdateCoordinator._listen", _idle_listen):
        assert await hass.config_entries.async_setup(entry.entry_id)
        coordinator = entry.runtime_data
        source = MagicMock()
        coordinator._source = source
        assert await hass.config_entries.async_unload(entry.entry_id)
    source.interrupt.assert_called_once()


async def test_slow_stop_does_not_strand_the_entry(hass):
    """Review #5: a timed-out stop unloads; setup retries until the worker exits."""
    entry = _entry(hass)
    release = threading.Event()

    def stuck_listen(self):
        release.wait(5)

    with (
        patch("custom_components.pytap.coordinator.PyTapDataUpdateCoordinator._listen", stuck_listen),
        patch("custom_components.pytap.coordinator.LISTENER_STOP_TIMEOUT", 0.2),
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        assert await hass.config_entries.async_unload(entry.entry_id)
        assert entry.state is ConfigEntryState.NOT_LOADED
        assert entry.entry_id in hass.data[DOMAIN]  # Parked until it really stops.

        await hass.config_entries.async_setup(entry.entry_id)
        assert entry.state is ConfigEntryState.SETUP_RETRY

        release.set()
        await asyncio.sleep(0.1)
        await hass.config_entries.async_reload(entry.entry_id)
        assert entry.state is ConfigEntryState.LOADED
        assert entry.entry_id not in hass.data[DOMAIN]  # Dropped once it stopped.
        release.set()
        assert await hass.config_entries.async_unload(entry.entry_id)


async def test_home_assistant_stop_finalises_capture(hass):
    """Review #2: HA doesn't unload entries on shutdown; the capture must still close."""
    entry = _entry(hass)
    with patch("custom_components.pytap.coordinator.PyTapDataUpdateCoordinator._listen", _idle_listen):
        assert await hass.config_entries.async_setup(entry.entry_id)
        coordinator = entry.runtime_data
        await coordinator.async_start_capture(1, 16)
        hass.bus.async_fire(EVENT_HOMEASSISTANT_STOP)
        await hass.async_block_till_done()
        status = await coordinator.async_capture_info()
    assert status["state"] == "stopped"
    assert status["complete"] is True


async def test_invalid_saved_port_is_a_clear_setup_error(hass):
    """Review #13: a value older versions accepted fails with a translated reason, no retries."""
    entry = _entry(hass, port=70000)
    assert not await hass.config_entries.async_setup(entry.entry_id)
    assert entry.state is ConfigEntryState.SETUP_ERROR
    assert entry.error_reason_translation_key == "invalid_connection_settings"
    assert entry.error_reason_translation_placeholders == {"field": "port"}


async def test_second_entry_does_not_adopt_another_entrys_optimizer(hass):
    """Review #9: two entries on one bus must not create clashing entities."""
    manual = _entry(hass)
    automatic = _entry(hass, modules=[], auto=True, host="other")
    with patch("custom_components.pytap.coordinator.PyTapDataUpdateCoordinator._listen", return_value=None):
        assert await hass.config_entries.async_setup(manual.entry_id)  # Sets up both entries.
        await hass.async_block_till_done()
        assert automatic.state is ConfigEntryState.LOADED
        coordinator = automatic.runtime_data
        assert coordinator._add_discovered_module(BARCODE, 1) is False
        assert coordinator._add_discovered_module(OTHER, 2) is True
        assert await hass.config_entries.async_unload(manual.entry_id)
        assert await hass.config_entries.async_unload(automatic.entry_id)


async def test_device_removal_rules(hass):
    """Review #10: optimizer and empty string devices can be deleted; others can't."""
    entry = _entry(hass, modules=[MODULE])
    registry = dr.async_get(hass)

    def device(identifier):
        return registry.async_get_or_create(
            config_entry_id=entry.entry_id, identifiers={(DOMAIN, identifier)}
        )

    assert not await async_remove_config_entry_device(hass, entry, device(f"{entry.entry_id}_installation"))
    assert not await async_remove_config_entry_device(hass, entry, device(f"{entry.entry_id}_string_East"))
    assert await async_remove_config_entry_device(hass, entry, device(f"{entry.entry_id}_string_Old"))
    assert await async_remove_config_entry_device(hass, entry, device(BARCODE))
    assert entry.data["modules"] == []
    assert entry.data[CONF_EXCLUDED_BARCODES] == [BARCODE]


async def test_orphaned_devices_are_removed_at_setup(hass):
    entry = _entry(hass, **{CONF_EXCLUDED_BARCODES: [OTHER]})
    registry = dr.async_get(hass)
    stale = registry.async_get_or_create(config_entry_id=entry.entry_id, identifiers={(DOMAIN, OTHER)})
    # Unknown, not excluded: possibly a discovered optimizer whose state was lost.
    unknown = registry.async_get_or_create(
        config_entry_id=entry.entry_id, identifiers={(DOMAIN, "E-3456789F")}
    )
    empty_string = registry.async_get_or_create(
        config_entry_id=entry.entry_id, identifiers={(DOMAIN, f"{entry.entry_id}_string_Old")}
    )
    with patch("custom_components.pytap.coordinator.PyTapDataUpdateCoordinator._listen", return_value=None):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        assert registry.async_get(stale.id) is None
        assert registry.async_get(unknown.id) is not None
        assert registry.async_get(empty_string.id) is None
        remaining = dr.async_entries_for_config_entry(registry, entry.entry_id)
        assert any((DOMAIN, BARCODE) in device.identifiers for device in remaining)
        assert await hass.config_entries.async_unload(entry.entry_id)


@pytest.mark.parametrize("path", ["/config/secrets.yaml", "/tmp/port", "relative/tty"])
def test_serial_paths_must_be_device_nodes(path):
    """Review #14: never open arbitrary files as a serial port."""
    with pytest.raises(InvalidConnectionSettings):
        normalize_connection({"connection_type": "serial", "serial_port": path})
    assert normalize_connection({"connection_type": "serial", "serial_port": "/dev/ttyUSB0"})


def test_symlinked_ports_compare_equal(tmp_path):
    """Review #15: /dev/serial/by-id links and their targets are the same port."""
    target = tmp_path / "ttyUSB0"
    target.touch()
    link = tmp_path / "by-id-tigo"
    os.symlink(target, link)
    assert resolved_endpoint_key(str(link)) == resolved_endpoint_key(str(target))


async def test_connection_loss_logged_once(hass, caplog):
    """Review #23: a gateway that stays offline logs one warning, not one per retry."""
    entry = _entry(hass)
    from custom_components.pytap.coordinator import PyTapDataUpdateCoordinator

    coordinator = PyTapDataUpdateCoordinator(hass, entry)
    attempts = 0

    def offline(*_args):
        nonlocal attempts
        attempts += 1
        if attempts == 4:
            coordinator._stop_event.set()
        raise ConnectionRefusedError(111, "refused")

    caplog.set_level(logging.DEBUG, logger="custom_components.pytap.coordinator")
    with (
        patch("custom_components.pytap.coordinator.open_source", side_effect=offline),
        patch("custom_components.pytap.coordinator.RECONNECT_DELAY", 0),
    ):
        await hass.async_add_executor_job(coordinator._listen)
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING and "unavailable" in r.message]
    assert len(warnings) == 1
    assert "errno 111" in warnings[0].message


async def test_removing_entry_deletes_saved_state_and_captures(hass):
    entry = _entry(hass)
    with (
        patch("custom_components.pytap.coordinator.PyTapDataUpdateCoordinator._listen", return_value=None),
        patch("custom_components.pytap.coordinator._MigratingStore.async_remove") as remove_store,
        patch("custom_components.pytap.async_remove_captures") as remove_captures,
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.config_entries.async_remove(entry.entry_id)
    remove_store.assert_awaited_once()
    remove_captures.assert_awaited_once_with(hass, entry.entry_id)
