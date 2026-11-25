"""Second review: cross-reload capture jobs, export budgets, formats, verdicts, errors."""

import asyncio
import json
import os
from pathlib import Path
import shutil
import threading
import time
from unittest.mock import patch
import zipfile

from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.pytap.capture_services import async_remove_captures, async_wait_for_capture_jobs
from custom_components.pytap.const import DOMAIN
from custom_components.pytap.pytap.core import capture
from custom_components.pytap.pytap.core.capture import (
    FORMAT_VERSION,
    CapturedChunk,
    CaptureError,
    CaptureRuntimeError,
    CaptureWriter,
    capture_directory,
    delete_capture,
    export_archive,
    iter_capture,
    list_captures,
    read_manifest,
)
from custom_components.pytap.pytap.core.crc import crc
from custom_components.pytap.pytap.core.parser import MAX_FRAME_SIZE, Parser
from custom_components.pytap.pytap.core.replay import FrameScanner, decode_wire_frame

_MIB = 1024 * 1024
_ESCAPE = {0x7E: 0, 0x24: 1, 0x23: 2, 0x25: 3, 0xA4: 4, 0xA3: 5, 0xA5: 6}


def _writer(tmp_path, **kwargs):
    writer = CaptureWriter(tmp_path / "captures", {"source": {"connection_type": "serial"}}, **kwargs)
    writer.start()
    return writer


def _stop(writer):
    writer.request_stop()
    assert writer.join(3)
    return read_manifest(writer.directory, allow_incomplete=True)


def _wire(payload=b"\x7e\x24\xff"):
    body = (0x8001).to_bytes(2, "big") + (0x7777).to_bytes(2, "big") + payload
    escaped = bytearray()
    for byte in body + crc(body).to_bytes(2, "little"):
        escaped.extend((0x7E, _ESCAPE[byte])) if byte in _ESCAPE else escaped.append(byte)
    return b"\x7e\x07" + bytes(escaped) + b"\x7e\x08"


def _frames(data, **kwargs):
    scanner = FrameScanner(**kwargs)
    records = list(scanner.feed(CapturedChunk(1, 1, 0, 1, data))) + list(scanner.finish())
    return [record for record in records if record["kind"] == "frame"]


def _kept_by_parser(data, prefix=b""):
    parser, frames = Parser(), []
    with patch.object(parser, "_dispatch_frame", side_effect=lambda frame: frames.append(frame) or []):
        parser.feed(prefix)
        frames.clear()
        parser.feed(data)
    return [frame.payload.hex() for frame in frames], parser


# --- 1. Export/delete jobs are domain-wide, surviving reload ------------------


@pytest.fixture
async def loaded_entry(hass, tmp_path):
    hass.config.config_dir = str(tmp_path)
    entry = MockConfigEntry(
        domain=DOMAIN, version=7, data={"host": "private-host", "port": 502, "modules": []}
    )
    entry.add_to_hass(hass)

    def idle_listener(self):
        self._stop_event.wait()

    with patch("custom_components.pytap.coordinator.PyTapDataUpdateCoordinator._listen", new=idle_listener):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        yield entry
        await hass.config_entries.async_unload(entry.entry_id)


async def _call(hass, service, entry, **data):
    return await hass.services.async_call(
        DOMAIN, service, {"config_entry_id": entry.entry_id, **data}, blocking=True, return_response=True
    )


async def _stopped_capture(hass, entry, data=b"evidence"):
    started = await _call(hass, "start_capture", entry, duration_minutes=1, max_storage_mib=16)
    entry.runtime_data._capture.record(data, 1)
    await _call(hass, "stop_capture", entry)
    return started


def _blocking_export(hass):
    entered, release = asyncio.Event(), threading.Event()
    original = capture.export_archive

    def export(*args, **kwargs):
        hass.loop.call_soon_threadsafe(entered.set)
        assert release.wait(5)
        return original(*args, **kwargs)

    return export, entered, release


async def test_export_stays_busy_across_reload(hass, loaded_entry):
    entry = loaded_entry
    started = await _stopped_capture(hass, entry)
    old = entry.runtime_data
    export, entered, release = _blocking_export(hass)
    with patch("custom_components.pytap.coordinator.export_archive", new=export):
        task = asyncio.create_task(old.async_export_capture(started["capture_id"]))
        await entered.wait()
        try:
            assert await hass.config_entries.async_reload(entry.entry_id)  # Unload doesn't wait.
            new = entry.runtime_data
            assert new is not old and new._capture_root == old._capture_root
            for action in (
                new.async_delete_capture(started["capture_id"]),
                new.async_start_capture(1, 16),
                new.async_export_capture(started["capture_id"]),
            ):
                with pytest.raises(CaptureError) as err:
                    await action
                assert err.value.translation_key == "capture_busy"
            with pytest.raises(ServiceValidationError) as err:
                await _call(hass, "delete_capture", entry, capture_id=started["capture_id"])
            assert err.value.translation_key == "capture_busy"
            assert await async_wait_for_capture_jobs(hass, entry.entry_id, 0.01) is False
        finally:
            release.set()
        archive = Path((await task)["archive"])
    assert archive.is_file()
    assert await async_wait_for_capture_jobs(hass, entry.entry_id, 1) is True
    result = await _call(hass, "delete_capture", entry, capture_id=started["capture_id"])
    assert result["deleted_archives"] == 1 and not archive.exists()


async def test_remove_captures_waits_for_a_running_export(hass, loaded_entry):
    entry = loaded_entry
    started = await _stopped_capture(hass, entry)
    root = entry.runtime_data._capture_root
    export, entered, release = _blocking_export(hass)
    with patch("custom_components.pytap.coordinator.export_archive", new=export):
        task = asyncio.create_task(entry.runtime_data.async_export_capture(started["capture_id"]))
        await entered.wait()
        removal = asyncio.create_task(async_remove_captures(hass, entry.entry_id))
        await asyncio.sleep(0.05)
        assert not removal.done() and root.is_dir()
        release.set()
        await task  # The export completes; nothing was deleted under it.
        await removal
    assert not root.exists()


async def test_remove_captures_defers_after_timeout(hass, loaded_entry):
    entry = loaded_entry
    started = await _stopped_capture(hass, entry)
    root = entry.runtime_data._capture_root
    export, entered, release = _blocking_export(hass)
    with (
        patch("custom_components.pytap.coordinator.export_archive", new=export),
        patch("custom_components.pytap.capture_services.CAPTURE_JOB_WAIT_TIMEOUT", 0.01),
    ):
        task = asyncio.create_task(entry.runtime_data.async_export_capture(started["capture_id"]))
        await entered.wait()
        await async_remove_captures(hass, entry.entry_id)
        assert root.is_dir()  # Not removed under the running export.
        release.set()
        assert (await task)["capture_id"] == started["capture_id"]  # Completed intact.
        await hass.async_block_till_done()
    assert not root.exists()


def test_delete_removes_orphan_archives_without_directory(tmp_path):
    writer = _writer(tmp_path)
    writer.record(b"data", 1)
    _stop(writer)
    archive = export_archive(writer.directory)
    shutil.rmtree(writer.directory)
    assert list_captures(writer.root) == []
    result = delete_capture(writer.root, writer.capture_id)
    assert result["deleted_archives"] == 1 and result["deleted_bytes"] > 0
    assert not archive.exists()
    with pytest.raises(CaptureError) as err:
        delete_capture(writer.root, writer.capture_id)
    assert err.value.translation_key == "capture_not_found"


# --- 2. Export budget excludes the capture itself; disk space is checked ------


def _fill_to_storage_limit(tmp_path, budget):
    writer = _writer(tmp_path, max_storage_bytes=budget)
    deadline = time.monotonic() + 10
    while writer.status()["state"] == "capturing":
        assert time.monotonic() < deadline
        writer.record(os.urandom(64 * 1024), 1)
        time.sleep(0.002)
    assert writer.join(3)
    manifest = read_manifest(writer.directory, allow_incomplete=True)
    assert manifest["stop_reason"] == "storage_limit"
    return writer, manifest


def test_storage_limited_capture_exports_at_the_same_budget(tmp_path):
    budget = 4 * _MIB
    writer, manifest = _fill_to_storage_limit(tmp_path, budget)
    assert manifest["max_storage_bytes"] == budget
    assert manifest["bytes_written"] > budget // 2
    archive = export_archive(writer.directory, allow_interrupted=True, max_storage_bytes=budget)
    with zipfile.ZipFile(archive) as zipped:
        assert (
            zipped.read(f"{writer.capture_id}/bus-000000.bin")
            == (writer.directory / "bus-000000.bin").read_bytes()
        )


def test_other_files_still_count_against_the_export_budget(tmp_path):
    writer = _writer(tmp_path)
    writer.record(b"data" * 1000, 1)
    _stop(writer)
    with (writer.root / "filler").open("wb") as filler:
        filler.truncate(2 * _MIB)
    with pytest.raises(CaptureError) as err:
        export_archive(writer.directory, max_storage_bytes=2 * _MIB)
    assert err.value.translation_key == "export_budget_exceeded"
    assert not isinstance(err.value, CaptureRuntimeError)
    assert export_archive(writer.directory, max_storage_bytes=3 * _MIB).is_file()


def test_export_is_bounded_while_writing(tmp_path, monkeypatch):
    writer = _writer(tmp_path)
    writer.record(os.urandom(256 * 1024), 1)
    _stop(writer)
    # An estimate that is too low must not let the archive outgrow the budget.
    monkeypatch.setattr(capture, "_zip_entry_bytes", lambda arcname, size: 0)
    with pytest.raises(CaptureError) as err:
        export_archive(writer.directory, max_storage_bytes=128 * 1024)
    assert err.value.translation_key == "export_budget_exceeded"
    assert not list(writer.root.glob("*.zip"))


def test_export_reports_insufficient_disk_space_as_runtime_error(tmp_path):
    writer = _writer(tmp_path)
    writer.record(b"data", 1)
    _stop(writer)
    usage = shutil.disk_usage(writer.root)._replace(free=capture.FREE_SPACE_RESERVE + 10)
    with (
        patch.object(capture.shutil, "disk_usage", return_value=usage),
        pytest.raises(CaptureRuntimeError) as err,
    ):
        export_archive(writer.directory)
    assert err.value.translation_key == "export_insufficient_space"
    assert set(err.value.translation_placeholders) == {"required_mib", "available_mib"}
    assert not list(writer.root.glob("*.zip"))


async def test_export_defaults_to_the_captures_own_budget(hass, loaded_entry):
    entry = loaded_entry
    started = await _stopped_capture(hass, entry)
    coordinator = entry.runtime_data
    coordinator._capture = None  # No current writer to borrow a budget from.
    with (coordinator._capture_root / "filler").open("wb") as filler:
        filler.truncate(16 * _MIB)  # Fits 1 GiB, but not this capture's 16 MiB.
    with pytest.raises(ServiceValidationError) as err:
        await _call(hass, "export_capture", entry, capture_id=started["capture_id"])
    assert err.value.translation_key == "export_budget_exceeded"
    (coordinator._capture_root / "filler").unlink()
    assert Path(
        (await _call(hass, "export_capture", entry, capture_id=started["capture_id"]))["archive"]
    ).is_file()


async def test_disk_space_failure_is_a_home_assistant_error(hass, loaded_entry):
    entry = loaded_entry
    started = await _stopped_capture(hass, entry)
    usage = shutil.disk_usage(entry.runtime_data._capture_root)._replace(free=0)
    with (
        patch.object(capture.shutil, "disk_usage", return_value=usage),
        pytest.raises(HomeAssistantError) as err,
    ):
        await _call(hass, "export_capture", entry, capture_id=started["capture_id"])
    assert not isinstance(err.value, ServiceValidationError)
    assert err.value.translation_key == "export_insufficient_space"


# --- 3. Format 1 captures remain readable ------------------------------------


def _as_format_1(writer, manifest):
    """The parent commit's layout: no events block, and a local directory field."""
    manifest = {key: value for key, value in manifest.items() if key != "events"}
    manifest.update(format_version=1, directory=str(writer.directory))
    (writer.directory / "manifest.json").write_text(json.dumps(manifest))


def test_new_captures_use_format_2(tmp_path):
    writer = _writer(tmp_path)
    assert FORMAT_VERSION == 2
    assert _stop(writer)["format_version"] == 2


def test_format_1_capture_lists_replays_and_exports(tmp_path):
    writer = _writer(tmp_path)
    writer.mark("connection_open", 1)
    writer.record(b"legacy evidence", 1)
    _as_format_1(writer, _stop(writer))
    assert read_manifest(writer.directory)["format_version"] == 1
    assert list_captures(writer.root)[0]["state"] == "stopped"
    with pytest.warns(RuntimeWarning, match="Format 1"):
        assert b"".join(c.data for c in iter_capture(writer.directory)) == b"legacy evidence"
    with pytest.warns(RuntimeWarning, match="Format 1"):
        archive = export_archive(writer.directory)
    with zipfile.ZipFile(archive) as zipped:
        recovery = json.loads(zipped.read(f"{writer.capture_id}/recovery.json"))
        assert zipped.read(f"{writer.capture_id}/events.jsonl")
    assert recovery["complete"] is True and recovery["verified"] is False
    assert recovery["format_version"] == 1 and recovery["unverified_files"] == ["events.jsonl"]


def test_format_2_still_requires_the_events_block(tmp_path):
    writer = _writer(tmp_path)
    manifest = _stop(writer)
    manifest.pop("events")
    (writer.directory / "manifest.json").write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match="event log"):
        read_manifest(writer.directory)


@pytest.mark.parametrize("version", [0, 3, True, "1", 1.0])
def test_unknown_formats_are_rejected(tmp_path, version):
    writer = _writer(tmp_path)
    manifest = _stop(writer)
    manifest["format_version"] = version
    (writer.directory / "manifest.json").write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match="Unsupported"):
        read_manifest(writer.directory)


# --- 4. Deeply nested manifests ---------------------------------------------


def test_deeply_nested_manifest_is_a_value_error(tmp_path):
    writer = _writer(tmp_path)
    _stop(writer)
    (writer.directory / "manifest.json").write_text("[" * 200000)
    with pytest.raises(ValueError, match="nested"):
        read_manifest(writer.directory, allow_incomplete=True)
    assert list_captures(writer.root)[0] == {
        "capture_id": writer.capture_id,
        "state": "invalid_manifest",
        "complete": False,
    }


async def test_nested_manifest_service_error_is_translated(hass, loaded_entry):
    entry = loaded_entry
    started = await _stopped_capture(hass, entry)
    (Path(started["directory"]) / "manifest.json").write_text("[" * 200000)
    entry.runtime_data._capture = None
    for service in ("capture_status", "export_capture"):
        with pytest.raises(ServiceValidationError) as err:
            await _call(hass, service, entry, capture_id=started["capture_id"])
        assert err.value.translation_key == "capture_invalid"
    listed = (await _call(hass, "list_captures", entry))["captures"]
    assert listed[0]["state"] == "invalid_manifest"


# --- 5. Scanner verdicts ------------------------------------------------------


def test_first_frame_verdict_is_unknown_when_started_mid_connection():
    data = _wire() + _wire(b"\x01")
    prefix = b"\x7e\x07\x01\x02\x7e"  # The live parser was mid-escape at capture start.
    kept, _ = _kept_by_parser(data, prefix)
    assert kept == ["01"]  # It drops the first frame.
    naive = _frames(data)
    assert naive[0]["live_parser_would_drop"] is False  # The fresh-state guess is wrong here.
    frames = _frames(data, started_mid_connection=True)
    assert frames[0]["live_parser_would_drop"] is None
    assert frames[0]["live_parser_drop_reason"] == "unknown_at_capture_start"
    assert frames[0]["crc_valid"] is True
    assert frames[1]["live_parser_would_drop"] is False


def test_framing_converges_after_noise_when_started_mid_connection():
    frames = _frames(b"\x00" * 3 + b"\x7e\x08" + _wire(), started_mid_connection=True)
    assert frames[0]["live_parser_would_drop"] is False


def test_lost_bytes_within_a_connection_leave_the_verdict_unknown():
    scanner = FrameScanner()
    wire = _wire()
    records = list(scanner.feed(CapturedChunk(1, 1, 0, 1, wire)))
    records += list(scanner.feed(CapturedChunk(2, 2, len(wire) + 5, 1, wire)))
    frames = [record for record in records if record["kind"] == "frame"]
    assert frames[0]["live_parser_would_drop"] is False
    assert frames[1]["live_parser_would_drop"] is None
    assert frames[1]["live_parser_drop_reason"] == "unknown_after_capture_gap"


def test_giant_with_bad_escape_after_max_size_is_reported_as_giant():
    giant = b"\x7e\x07" + b"\x11" * (MAX_FRAME_SIZE + 44) + b"\x7e\x09" + b"\x11" * 4 + b"\x7e\x08"
    data = giant + _wire()
    kept, parser = _kept_by_parser(data)
    assert parser.counters["giants"] == 1 and len(kept) == 1
    decoded = decode_wire_frame(giant)
    assert decoded["framing_error"] == "unknown_escape" and decoded["giant"] is True
    frames = _frames(data)
    assert frames[0]["live_parser_drop_reason"] == "giant" and frames[0]["giant"] is True
    assert [frame["payload_hex"] for frame in frames if frame["live_parser_would_drop"] is False] == kept


def test_bad_escape_within_max_size_is_still_unknown_escape():
    frame = b"\x7e\x07" + b"\x11" * MAX_FRAME_SIZE + b"\x7e\x09" + b"\x7e\x08"
    kept, parser = _kept_by_parser(frame + _wire())
    assert parser.counters["giants"] == 0 and len(kept) == 1
    frames = _frames(frame + _wire())
    assert frames[0]["giant"] is False
    assert frames[0]["live_parser_drop_reason"] == "unknown_escape"


# --- 6. Runtime failures are HomeAssistantError ------------------------------


async def test_runtime_start_failure_is_a_home_assistant_error(hass, loaded_entry):
    entry = loaded_entry
    with (
        patch.object(CaptureWriter, "start", side_effect=OSError("No space left on /private/path")),
        pytest.raises(HomeAssistantError) as err,
    ):
        await _call(hass, "start_capture", entry, duration_minutes=1, max_storage_mib=16)
    assert not isinstance(err.value, ServiceValidationError)
    assert err.value.translation_key == "capture_failed"
    assert err.value.translation_placeholders == {"error": "OSError"}


# --- 7. Nits: directory fsync, symlinked roots, empty IDs ---------------------


def test_new_segment_entries_are_fsynced_before_the_manifest(tmp_path):
    writer = CaptureWriter(tmp_path / "captures", {}, segment_bytes=4)
    calls = []

    def atomic(path, data):
        calls.append("manifest")
        path.write_bytes(data)

    with (
        patch.object(capture, "_fsync_dir", side_effect=lambda path: calls.append(path)),
        patch.object(capture, "_atomic_write", side_effect=atomic),
    ):
        writer.start()
        writer.record(b"12345678", 1)  # Rotation opens new segment files.
        _stop(writer)
    synced = [i for i, call in enumerate(calls) if call == "manifest" and calls[i - 1] == writer.directory]
    assert len(synced) >= 2  # Start and rotation (and the final manifest).
    assert len(read_manifest(writer.directory)["segments"]) == 2


def test_symlinked_root_is_refused_everywhere(tmp_path):
    writer = _writer(tmp_path)
    _stop(writer)
    link = tmp_path / "link"
    link.symlink_to(writer.root, target_is_directory=True)
    for action in (
        lambda: list_captures(link),
        lambda: capture_directory(link, writer.capture_id),
        lambda: export_archive(link / writer.capture_id),
        lambda: delete_capture(link, writer.capture_id),
    ):
        with pytest.raises(CaptureError) as err:
            action()
        assert err.value.translation_key == "capture_unsafe"
    assert not list(writer.root.glob("*.zip")) and writer.directory.is_dir()


async def test_empty_capture_id_is_rejected_by_export(hass, loaded_entry):
    entry = loaded_entry
    await _stopped_capture(hass, entry)
    with pytest.raises(ServiceValidationError) as err:
        await _call(hass, "export_capture", entry, capture_id="")
    assert err.value.translation_key == "invalid_capture_id"
    assert not list(entry.runtime_data._capture_root.glob("*.zip"))


# --- 8. Archive inputs are opened without following links --------------------


def test_symlink_swapped_in_after_validation_is_not_archived(tmp_path):
    writer = _writer(tmp_path)
    writer.record(b"evidence", 1)
    _stop(writer)
    secret = tmp_path / "secret"
    secret.write_bytes(b"not capture data")
    original = capture._private_file

    def swap_then_open(path):
        segment = writer.directory / "bus-000000.bin"
        segment.unlink()
        segment.symlink_to(secret)
        return original(path)

    with (
        patch.object(capture, "_private_file", side_effect=swap_then_open),
        pytest.raises(CaptureError) as err,
    ):
        export_archive(writer.directory)
    assert err.value.translation_key == "capture_unsafe"
    assert not list(writer.root.glob("*.zip"))


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="POSIX only")
def test_archive_inputs_must_be_regular_files_without_blocking(tmp_path):
    fifo = tmp_path / "fifo"
    os.mkfifo(fifo)
    with pytest.raises(CaptureError):
        capture._open_input(None, fifo)
    link = tmp_path / "link"
    link.symlink_to(tmp_path / "missing")
    with pytest.raises(CaptureError):
        capture._open_input(None, link)
    regular = tmp_path / "regular"
    regular.write_bytes(b"x")
    handle, info = capture._open_input(None, regular)
    with handle:
        assert handle.read() == b"x" and info.st_size == 1
