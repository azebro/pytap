"""HA capture actions, lifecycle, privacy, and capture-before-parser ordering."""

import asyncio
import json
from pathlib import Path
import threading
from unittest.mock import ANY, MagicMock, patch

from homeassistant.core import Context
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError, Unauthorized
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.pytap.const import DOMAIN, INTEGRATION_VERSION
from custom_components.pytap.pytap.core.capture import iter_capture, list_captures


@pytest.fixture
async def capture_entry(hass, tmp_path):
    hass.config.config_dir = str(tmp_path)
    entry = MockConfigEntry(
        domain=DOMAIN, version=7, data={"host": "private-host", "port": 502, "modules": []}
    )
    entry.add_to_hass(hass)

    def idle_listener(self):
        # Model an offline/retrying listener without a network dependency: it
        # runs until told to stop, like the real worker.
        self._stop_event.wait()

    with patch("custom_components.pytap.coordinator.PyTapDataUpdateCoordinator._listen", new=idle_listener):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    coordinator = entry.runtime_data
    coordinator._capture_root = tmp_path / "captures" / entry.entry_id
    yield entry, coordinator
    await coordinator.async_stop_capture()
    await hass.config_entries.async_unload(entry.entry_id)


async def _call(hass, service, entry, **data):
    return await hass.services.async_call(
        DOMAIN, service, {"config_entry_id": entry.entry_id, **data}, blocking=True, return_response=True
    )


async def test_actions_start_stop_list_export_and_private_metadata(hass, capture_entry):
    entry, coordinator = capture_entry
    assert coordinator.capture_status == {"state": "idle"}
    result = await _call(hass, "start_capture", entry, duration_minutes=1, max_storage_mib=16)
    assert result["state"] == "capturing"
    coordinator._capture.record(b"all raw bytes\x00\xff", 1)
    result = await _call(hass, "stop_capture", entry)
    assert result["state"] == "stopped" and result["complete"] is True
    assert result["bytes_written"] == 15
    manifest = json.loads((Path(result["directory"]) / "manifest.json").read_text())
    assert "private-host" not in json.dumps(manifest)
    assert manifest["metadata"]["integration_version"] == INTEGRATION_VERSION
    assert "parser_state" in manifest["metadata"]
    listed = await _call(hass, "list_captures", entry)
    assert listed["captures"][0]["capture_id"] == result["capture_id"]
    exported = await _call(hass, "export_capture", entry)
    assert Path(exported["archive"]).is_file()


async def test_running_export_and_duplicate_start_are_rejected(hass, capture_entry):
    entry, _ = capture_entry
    await _call(hass, "start_capture", entry, duration_minutes=1, max_storage_mib=16)
    for service in ("start_capture", "export_capture"):
        with pytest.raises(ServiceValidationError):
            await _call(hass, service, entry)


async def test_unloaded_or_wrong_entry_cannot_capture(hass):
    from custom_components.pytap import async_setup

    await async_setup(hass, {})
    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(DOMAIN, "start_capture", {"config_entry_id": "missing"}, blocking=True)


async def test_non_admin_cannot_start_capture(hass, capture_entry, hass_read_only_user):
    entry, coordinator = capture_entry
    with pytest.raises(Unauthorized):
        await hass.services.async_call(
            DOMAIN,
            "start_capture",
            {"config_entry_id": entry.entry_id},
            context=Context(user_id=hass_read_only_user.id),
            blocking=True,
        )
    assert coordinator.capture_status == {"state": "idle"}


async def test_capture_disabled_after_listener_restart_but_evidence_remains(hass, capture_entry):
    entry, coordinator = capture_entry
    started = await _call(hass, "start_capture", entry, duration_minutes=1, max_storage_mib=16)
    coordinator._capture.record(b"evidence", 1)
    assert await coordinator.async_stop_listener()
    assert coordinator.capture_status["stop_reason"] == "unload"
    with pytest.raises(ValueError, match="not running"):
        await coordinator.async_start_capture()
    assert list_captures(coordinator._capture_root)[0]["capture_id"] == started["capture_id"]


async def test_read_bytes_are_captured_before_parser_can_discard_them(hass, capture_entry):
    entry, coordinator = capture_entry
    raw = b"\x00noise-and-unknown-invalid-frames\xff"
    await _call(hass, "start_capture", entry, duration_minutes=1, max_storage_mib=16)
    source, parser = MagicMock(), MagicMock()
    parser.infrastructure = {}

    def feed(data):
        assert coordinator.capture_status["bytes_observed"] == len(raw)
        assert data == raw
        return []  # The live parser is free to reject every byte.

    parser.feed.side_effect = feed

    reads = iter([raw, b""])

    def read(size):
        data = next(reads)
        if not data:
            coordinator._stop_event.set()
        return data

    source.read.side_effect = read
    with (
        patch("custom_components.pytap.coordinator.open_source", return_value=source),
        patch("custom_components.pytap.pytap.api.create_parser", return_value=parser),
    ):
        await coordinator._async_listen()
        assert await coordinator.async_stop_listener()
    assert b"".join(c.data for c in iter_capture(coordinator._capture.directory)) == raw
    source.close.assert_called_once()


async def test_capture_shutdown_is_off_loop(hass, capture_entry):
    entry, coordinator = capture_entry
    await _call(hass, "start_capture", entry, duration_minutes=1, max_storage_mib=16)
    stopped = asyncio.create_task(coordinator.async_stop_capture())
    await asyncio.sleep(0)
    assert await stopped
    assert not coordinator._capture.busy


async def test_cancelled_export_keeps_executor_ownership_and_blocks_new_capture(hass, capture_entry):
    entry, coordinator = capture_entry
    await _call(hass, "start_capture", entry, duration_minutes=1, max_storage_mib=16)
    await _call(hass, "stop_capture", entry)
    entered, release = asyncio.Event(), threading.Event()

    def delayed_export(*args, **kwargs):
        hass.loop.call_soon_threadsafe(entered.set)
        assert release.wait(2)
        return Path("private.zip")

    with patch("custom_components.pytap.coordinator.export_archive", new=delayed_export):
        task = asyncio.create_task(coordinator.async_export_capture())
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert not coordinator._capture_export_future.done()
        try:
            with pytest.raises(ValueError, match="already running"):
                await coordinator.async_start_capture(duration_minutes=1, max_storage_mib=16)
        finally:
            release.set()
            await coordinator._capture_export_future


async def test_cancelled_capture_start_cannot_leave_a_late_writer_running(hass, capture_entry):
    _, coordinator = capture_entry
    from custom_components.pytap.pytap.core.capture import CaptureWriter

    entered, release = asyncio.Event(), threading.Event()
    original = CaptureWriter.start

    def delayed_start(writer):
        hass.loop.call_soon_threadsafe(entered.set)
        assert release.wait(2)
        original(writer)

    with patch.object(CaptureWriter, "start", new=delayed_start):
        task = asyncio.create_task(coordinator.async_start_capture(1, 16))
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        release.set()
        stopped = await coordinator.async_stop_capture()
    assert stopped["state"] == "stopped"
    assert stopped["stop_reason"] == "start_cancelled"
    assert not coordinator._capture.busy


async def test_unload_deadline_includes_capture_lock_wait(hass, capture_entry):
    _, coordinator = capture_entry
    await coordinator._capture_lock.acquire()
    try:
        with patch("custom_components.pytap.coordinator.LISTENER_STOP_TIMEOUT", 0.01):
            assert await coordinator.async_stop_listener() is False
        assert coordinator._worker_future is not None  # Retained, not discarded.
    finally:
        coordinator._capture_lock.release()
    assert await coordinator.async_stop_listener()


async def test_bootstrap_is_snapshotted_in_listener_at_first_admitted_read(hass, capture_entry):
    entry, coordinator = capture_entry
    from custom_components.pytap.pytap.core.types import LongAddress

    old = LongAddress.from_str("00:00:00:00:00:00:00:01")
    new = LongAddress.from_str("00:00:00:00:00:00:00:02")
    coordinator._persistent_state.gateway_node_tables = {1: {2: old}}
    await _call(hass, "start_capture", entry, duration_minutes=1, max_storage_mib=16)
    # A reassignment occurs after async preparation but before the first read.
    coordinator._persistent_state.gateway_node_tables[1][2] = new
    raw = b"first captured data"
    source, parser = MagicMock(), MagicMock()
    parser.infrastructure = {}
    parser.feed.return_value = []
    reads = iter([raw, b""])

    def read(size):
        data = next(reads)
        if not data:
            coordinator._stop_event.set()
        return data

    source.read.side_effect = read
    with (
        patch("custom_components.pytap.coordinator.open_source", return_value=source),
        patch("custom_components.pytap.pytap.api.create_parser", return_value=parser),
    ):
        await coordinator._async_listen()
        await coordinator.async_stop_listener()
    manifest = json.loads((coordinator._capture.directory / "manifest.json").read_text())
    assert manifest["metadata"]["parser_state"]["gateway_node_tables"]["1"]["2"] == str(new)
    assert manifest["metadata"]["parser_state_at_capture_boundary"] is True


async def test_capture_marker_failure_cannot_prevent_port_release(hass, capture_entry):
    _, coordinator = capture_entry
    source = MagicMock()

    def read(size):
        coordinator._stop_event.set()
        return b""

    source.read.side_effect = read
    recorder = MagicMock()

    def mark(event, *args, **kwargs):
        if event == "connection_closed":
            raise ValueError("marker failure")

    recorder.mark.side_effect = mark
    with (
        patch.object(coordinator, "_capture", recorder),
        patch("custom_components.pytap.coordinator.open_source", return_value=source),
    ):
        with pytest.raises(ValueError, match="marker failure"):
            await coordinator._async_listen()
    source.close.assert_called_once()
    assert coordinator._source is None


_ALL_SERVICES = (
    "start_capture",
    "stop_capture",
    "capture_status",
    "list_captures",
    "export_capture",
    "delete_capture",
)


@pytest.mark.parametrize("service", _ALL_SERVICES)
async def test_non_admin_is_rejected_for_every_capture_service(
    hass, capture_entry, hass_read_only_user, service
):
    entry, coordinator = capture_entry
    data = {"config_entry_id": entry.entry_id}
    if service == "delete_capture":
        data["capture_id"] = "20260101T000000Z_aaaaaaaaaaaa"
    with pytest.raises(Unauthorized):
        await hass.services.async_call(
            DOMAIN, service, data, context=Context(user_id=hass_read_only_user.id), blocking=True
        )
    assert coordinator.capture_status == {"state": "idle"}


async def _error_key(hass, service, entry, **data):
    with pytest.raises(ServiceValidationError) as err:
        await _call(hass, service, entry, **data)
    assert err.value.translation_domain == DOMAIN
    return err.value.translation_key, err.value.translation_placeholders


async def test_service_errors_are_translated(hass, capture_entry):
    entry, coordinator = capture_entry
    started = await _call(hass, "start_capture", entry, duration_minutes=1, max_storage_mib=16)
    assert await _error_key(hass, "start_capture", entry) == ("capture_busy", {})
    assert (await _error_key(hass, "export_capture", entry))[0] == "stop_before_export"
    key, _ = await _error_key(hass, "delete_capture", entry, capture_id=started["capture_id"])
    assert key == "capture_active"
    await _call(hass, "stop_capture", entry)
    assert (await _error_key(hass, "export_capture", entry, capture_id="../x"))[0] == "invalid_capture_id"
    missing = "20260101T000000Z_aaaaaaaaaaaa"
    assert await _error_key(hass, "capture_status", entry, capture_id=missing) == (
        "capture_not_found",
        {"capture_id": missing},
    )
    with pytest.raises(ValueError) as err:
        await coordinator.async_start_capture(duration_minutes=0)
    assert err.value.translation_key == "invalid_duration"
    coordinator._capture = None
    assert (await _error_key(hass, "export_capture", entry))[0] == "capture_id_required"
    coordinator._stop_event.set()
    assert (await _error_key(hass, "start_capture", entry))[0] == "listener_not_running"


async def test_unknown_errors_hide_details_behind_a_translated_message(hass, capture_entry):
    entry, _ = capture_entry
    with (
        patch(
            "custom_components.pytap.coordinator.list_captures", side_effect=OSError("/private/secret path")
        ),
        pytest.raises(HomeAssistantError) as err,
    ):
        await _call(hass, "list_captures", entry)
    # A runtime failure, not a problem with the request.
    assert not isinstance(err.value, ServiceValidationError)
    assert (err.value.translation_key, err.value.translation_placeholders) == (
        "capture_failed",
        {"error": "OSError"},
    )


async def test_unloaded_entry_error_is_translated(hass):
    from custom_components.pytap import async_setup

    await async_setup(hass, {})
    with pytest.raises(ServiceValidationError) as err:
        await hass.services.async_call(DOMAIN, "list_captures", {"config_entry_id": "missing"}, blocking=True)
    assert err.value.translation_key == "capture_entry_not_loaded"


async def test_export_budget_is_enforced(hass, capture_entry):
    entry, coordinator = capture_entry
    await _call(hass, "start_capture", entry, duration_minutes=1, max_storage_mib=16)
    await _call(hass, "stop_capture", entry)
    with (coordinator._capture_root / "filler").open("wb") as filler:
        filler.truncate(16 * 1024 * 1024)  # Sparse; still counted by apparent size.
    key, placeholders = await _error_key(hass, "export_capture", entry, max_storage_mib=16)
    assert key == "export_budget_exceeded" and placeholders["available_mib"] == "0"
    assert not list(coordinator._capture_root.glob("*.zip"))
    assert Path((await _call(hass, "export_capture", entry, max_storage_mib=64))["archive"]).is_file()


async def test_capture_start_applies_default_and_supported_storage_limits(hass, capture_entry):
    entry, coordinator = capture_entry
    for supplied, expected in ((None, 1024), (16, 16), (4096, 4096)):
        data = {} if supplied is None else {"max_storage_mib": supplied}
        started = await _call(hass, "start_capture", entry, duration_minutes=1, **data)
        assert started["max_storage_bytes"] == expected * 1024 * 1024
        await _call(hass, "stop_capture", entry)
    for invalid in (15, 4097):
        with pytest.raises(ValueError) as err:
            await coordinator.async_start_capture(1, invalid)
        assert err.value.translation_key == "invalid_storage"


async def test_export_does_not_hold_the_capture_lock(hass, capture_entry):
    entry, coordinator = capture_entry
    started = await _call(hass, "start_capture", entry, duration_minutes=1, max_storage_mib=16)
    await _call(hass, "stop_capture", entry)
    entered, release = asyncio.Event(), threading.Event()

    def delayed_export(*args, **kwargs):
        hass.loop.call_soon_threadsafe(entered.set)
        assert release.wait(5)
        return Path("private.zip")

    with patch("custom_components.pytap.coordinator.export_archive", new=delayed_export):
        task = asyncio.create_task(coordinator.async_export_capture())
        await entered.wait()
        try:
            assert not coordinator._capture_lock.locked()
            stopped = await asyncio.wait_for(coordinator.async_stop_capture(), 1)
            assert stopped["state"] == "stopped"
            for action in (
                coordinator.async_start_capture(1, 16),
                coordinator.async_export_capture(),
                coordinator.async_delete_capture(started["capture_id"]),
            ):
                with pytest.raises(ValueError) as err:
                    await asyncio.wait_for(action, 1)
                assert err.value.translation_key == "capture_busy"
            unload = asyncio.create_task(coordinator.async_stop_listener())
            await asyncio.sleep(0.05)
        finally:
            release.set()
        assert (await task)["archive"] == "private.zip"
        assert await unload


async def test_delete_capture_service(hass, capture_entry):
    entry, coordinator = capture_entry
    first = await _call(hass, "start_capture", entry, duration_minutes=1, max_storage_mib=16)
    await _call(hass, "stop_capture", entry)
    archive = Path((await _call(hass, "export_capture", entry))["archive"])
    second = await _call(hass, "start_capture", entry, duration_minutes=1, max_storage_mib=16)
    await _call(hass, "stop_capture", entry)
    result = await _call(hass, "delete_capture", entry, capture_id=first["capture_id"])
    assert result == {"capture_id": first["capture_id"], "deleted_archives": 1, "deleted_bytes": ANY}
    assert not Path(first["directory"]).exists() and not archive.exists()
    listed = (await _call(hass, "list_captures", entry))["captures"]
    assert [c["capture_id"] for c in listed] == [second["capture_id"]]
    await _call(hass, "delete_capture", entry, capture_id=second["capture_id"])
    assert coordinator.capture_status == {"state": "idle"}
    for capture_id in ("../../config", "..", first["capture_id"]):
        key, _ = await _error_key(hass, "delete_capture", entry, capture_id=capture_id)
        assert key in ("invalid_capture_id", "capture_not_found")


async def test_diagnostics_capture_status_has_no_directory(hass, capture_entry):
    entry, coordinator = capture_entry
    started = await _call(hass, "start_capture", entry, duration_minutes=1, max_storage_mib=16)
    assert "directory" in started
    assert "directory" not in coordinator.capture_status
    assert "directory" not in coordinator.get_diagnostics_data()["packet_capture"]
    assert "directory" in await _call(hass, "capture_status", entry)
    stopped = await _call(hass, "stop_capture", entry)
    assert str(coordinator._capture_root) not in json.dumps(
        coordinator.get_diagnostics_data()["packet_capture"]
    )
    manifest = (Path(stopped["directory"]) / "manifest.json").read_text()
    assert str(coordinator._capture_root) not in manifest
    coordinator._capture = None
    stored = await _call(hass, "capture_status", entry, capture_id=started["capture_id"])
    assert stored["directory"] == stopped["directory"] and "segments" not in stored


async def test_remove_captures_for_removed_entry(hass, tmp_path):
    from custom_components.pytap.capture_services import async_remove_captures

    hass.config.config_dir = str(tmp_path)
    root = Path(hass.config.path("pytap_captures", "entry"))
    (root / "20260101T000000Z_aaaaaaaaaaaa").mkdir(parents=True)
    await async_remove_captures(hass, "entry")
    assert not root.exists()
    await async_remove_captures(hass, "entry")  # Already gone: no error.


async def test_remove_captures_refuses_symlinked_parent_non_destructively(hass, tmp_path, caplog):
    from custom_components.pytap.capture_services import async_remove_captures, capture_root
    from custom_components.pytap.pytap.core.capture import CaptureError

    hass.config.config_dir = str(tmp_path)
    external = tmp_path / "external"
    entry_root = external / "entry"
    entry_root.mkdir(parents=True)
    (entry_root / "keep").write_text("keep")
    symlink = Path(hass.config.path("pytap_captures"))
    symlink.symlink_to(external, target_is_directory=True)
    try:
        with pytest.raises(CaptureError, match="move.*config directory"):
            capture_root(hass, "entry")
        await async_remove_captures(hass, "entry")
        assert (entry_root / "keep").read_text() == "keep"
        assert "move" in caplog.text.lower() and "config directory" in caplog.text.lower()
    finally:
        symlink.unlink()


@pytest.mark.parametrize(
    "service", ["start_capture", "list_captures", "capture_status", "export_capture", "delete_capture"]
)
async def test_capture_actions_reject_redirected_parent(hass, capture_entry, tmp_path_factory, service):
    from custom_components.pytap.pytap.core.capture import CaptureWriter

    entry, coordinator = capture_entry
    outside = tmp_path_factory.mktemp("external-capture-data")
    writer = CaptureWriter(outside / entry.entry_id, {})
    writer.start()
    writer.record(b"must remain unchanged", 1)
    writer.request_stop("test")
    assert writer.join(5)
    original = {
        path.relative_to(outside): path.read_bytes()
        for path in outside.rglob("*")
        if path.is_file()
    }
    link = coordinator._capture_root.parent
    link.symlink_to(outside, target_is_directory=True)
    try:
        args = {} if service in ("start_capture", "list_captures") else {"capture_id": writer.capture_id}
        with pytest.raises(ServiceValidationError) as err:
            await _call(hass, service, entry, **args)
        assert err.value.translation_key == "capture_path_unsafe"
        assert {
            path.relative_to(outside): path.read_bytes()
            for path in outside.rglob("*")
            if path.is_file()
        } == original
    finally:
        await coordinator.async_stop_capture()
        link.unlink()


async def test_capture_actions_allow_symlinked_ancestor_above_config(hass, capture_entry, tmp_path):
    entry, coordinator = capture_entry
    real = tmp_path / "real"
    (real / "config").mkdir(parents=True)
    alias = tmp_path / "alias"
    alias.symlink_to(real, target_is_directory=True)
    hass.config.config_dir = str(alias / "config")
    coordinator._capture_root = alias / "config" / "pytap_captures" / entry.entry_id
    started = await _call(hass, "start_capture", entry, duration_minutes=1, max_storage_mib=16)
    await _call(hass, "stop_capture", entry)
    listed = await _call(hass, "list_captures", entry)
    assert listed["captures"][0]["capture_id"] == started["capture_id"]
    exported = await _call(hass, "export_capture", entry)
    assert Path(exported["archive"]).is_file()
    await _call(hass, "delete_capture", entry, capture_id=started["capture_id"])
    assert not Path(started["directory"]).exists()
    assert not Path(exported["archive"]).exists()
