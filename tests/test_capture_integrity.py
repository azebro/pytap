"""Capture durability, integrity, manifest validation, and deletion."""

import json
import os
import threading
import time
from unittest.mock import patch
import zipfile

import pytest

from custom_components.pytap.pytap.core import capture
from custom_components.pytap.pytap.core.capture import (
    INDEX,
    CaptureError,
    CaptureWriter,
    capture_directory,
    delete_capture,
    export_archive,
    iter_capture,
    list_captures,
    read_manifest,
    remove_capture_root,
)


def _writer(tmp_path, **kwargs):
    writer = CaptureWriter(tmp_path / "captures", {"source": {"connection_type": "serial"}}, **kwargs)
    writer.start()
    return writer


def _stop(writer):
    writer.request_stop()
    assert writer.join(3)
    return read_manifest(writer.directory, allow_incomplete=True)


def _interrupt(writer, manifest):
    manifest.update(state="capturing", complete=False)
    (writer.directory / "manifest.json").write_text(json.dumps(manifest))


def _wait(predicate, timeout=3.0):
    deadline = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < deadline
        time.sleep(0.02)


def test_zero_filled_index_tail_warns_stops_and_still_exports(tmp_path):
    writer = _writer(tmp_path)
    writer.record(b"evidence", 1)
    _interrupt(writer, _stop(writer))
    with (writer.directory / "chunks-000000.bin").open("ab") as index:
        index.write(b"\x00" * INDEX.size * 2)  # Allocated but unwritten after power loss.
    with pytest.warns(RuntimeWarning) as caught:
        chunks = list(iter_capture(writer.directory, allow_incomplete=True))
    assert any("invalid index row" in str(warning.message) for warning in caught)
    assert b"".join(c.data for c in chunks) == b"evidence"
    with pytest.warns(RuntimeWarning):
        archive = export_archive(writer.directory, allow_interrupted=True)
    with zipfile.ZipFile(archive) as zipped:
        name = writer.capture_id
        assert zipped.read(f"{name}/chunks-000000.bin").endswith(b"\x00" * INDEX.size * 2)
        recovery = json.loads(zipped.read(f"{name}/recovery.json"))
    assert recovery["complete"] is False and recovery["verified"] is False
    assert any("invalid index row" in message for message in recovery["validation_warnings"])


def test_invalid_index_row_still_raises_in_strict_mode(tmp_path):
    writer = _writer(tmp_path)
    writer.record(b"evidence", 1)
    _stop(writer)
    with (writer.directory / "chunks-000000.bin").open("ab") as index:
        index.write(b"\x00" * INDEX.size)
    with pytest.raises(ValueError):
        list(iter_capture(writer.directory))
    with pytest.raises(ValueError):
        export_archive(writer.directory, allow_interrupted=True)


def test_manifest_waits_for_interval_while_data_flows(tmp_path):
    writer = _writer(tmp_path)
    writer.record(b"data", 1)
    _wait(lambda: writer.status()["bytes_written"] == 4)
    time.sleep(1.3)  # Data is flushed every second; the manifest waits for MANIFEST_INTERVAL.
    assert read_manifest(writer.directory, allow_incomplete=True)["bytes_written"] == 0
    assert _stop(writer)["bytes_written"] == 4


def test_manifest_is_not_rewritten_while_idle(tmp_path, monkeypatch):
    monkeypatch.setattr(capture, "MANIFEST_INTERVAL", 0.0)
    writer = _writer(tmp_path)
    writer.record(b"data", 1)
    _wait(lambda: read_manifest(writer.directory, allow_incomplete=True)["bytes_written"] == 4)
    with patch.object(capture, "_atomic_write", wraps=capture._atomic_write) as written:
        time.sleep(0.8)  # Several writer-loop iterations without new data.
        assert written.call_count == 0
        writer.record(b"more", 1)
        _wait(lambda: written.call_count == 1)
        _stop(writer)
    assert read_manifest(writer.directory)["bytes_written"] == 8


def test_data_is_fsynced_before_manifest_and_directory_after(tmp_path):
    writer = _writer(tmp_path, segment_bytes=4)
    calls = []
    original_sync = writer._sync_data

    def sync():
        calls.append("sync")
        original_sync()

    def atomic(path, data):
        calls.append("manifest")
        original_atomic(path, data)

    original_atomic = capture._atomic_write
    with (
        patch.object(writer, "_sync_data", side_effect=sync),
        patch.object(capture, "_atomic_write", side_effect=atomic),
        patch.object(capture, "_fsync_dir", wraps=capture._fsync_dir) as fsync_dir,
    ):
        writer.record(b"12345678", 1)  # Rotation writes a manifest immediately.
        _wait(lambda: calls.count("manifest") >= 1)
        _stop(writer)
    assert calls[0] == "sync" and calls.count("manifest") >= 2
    assert all(calls[i - 1] == "sync" for i, call in enumerate(calls) if call == "manifest")
    if os.name == "posix":
        fsync_dir.assert_any_call(writer.directory)


def test_events_log_integrity_is_verified(tmp_path):
    writer = _writer(tmp_path)
    writer.mark("connection_open", 1)
    writer.record(b"data", 1)
    manifest = _stop(writer)
    events = writer.directory / "events.jsonl"
    assert manifest["events"]["bytes"] == events.stat().st_size > 0
    list(iter_capture(writer.directory))
    events.write_bytes(events.read_bytes().replace(b"open", b"OPEN"))
    with pytest.raises(ValueError, match="checksum"):
        list(iter_capture(writer.directory))
    with pytest.raises(ValueError, match="checksum"):
        export_archive(writer.directory)
    _interrupt(writer, manifest)
    with pytest.warns(RuntimeWarning, match="checksum"):
        assert b"".join(c.data for c in iter_capture(writer.directory, allow_incomplete=True)) == b"data"


@pytest.mark.parametrize(
    "mutate",
    [
        lambda m: m.update(segments={"x": 1}),
        lambda m: m.update(segments=[1]),
        lambda m: m["segments"][0].update(records="3"),
        lambda m: m["segments"][0].update(bytes=-1),
        lambda m: m["segments"][0].update(bytes=True),
        lambda m: m["segments"][0].update(sha256=123),
        lambda m: m["segments"][0].update(index_sha256="ab"),
        lambda m: m["segments"][0].update(file="bus-000001.bin", index="chunks-000001.bin"),
        lambda m: m["segments"][0].update(index="../chunks-000000.bin"),
        lambda m: m.update(bytes_written="4"),
        lambda m: m.update(complete="yes"),
        lambda m: m.update(state="running"),
        lambda m: m.update(capture_id="20260101T000000Z_aaaaaaaaaaaa"),
        lambda m: m.update(stop_reason=1),
        lambda m: m.update(metadata=[]),
        lambda m: m.pop("events"),
        lambda m: m["events"].update(file="../x"),
        lambda m: m["events"].update(bytes="1"),
    ],
)
def test_malformed_manifests_raise_value_error(tmp_path, mutate):
    writer = _writer(tmp_path)
    writer.record(b"data", 1)
    manifest = _stop(writer)
    mutate(manifest)
    (writer.directory / "manifest.json").write_text(json.dumps(manifest))
    for allow in (False, True):
        with pytest.raises(ValueError):
            read_manifest(writer.directory, allow_incomplete=allow)
        with pytest.raises(ValueError):
            list(iter_capture(writer.directory, allow_incomplete=allow))
    with pytest.raises(ValueError):
        export_archive(writer.directory, allow_interrupted=True)
    assert list_captures(writer.root)[0]["state"] == "invalid_manifest"


def test_unicode_digit_capture_ids_are_rejected(tmp_path):
    digits = "\u0661"  # ARABIC-INDIC DIGIT ONE matches \d but is not ASCII.
    capture_id = f"{digits * 8}T{digits * 6}Z_{'a' * 12}"
    (tmp_path / capture_id).mkdir()
    with pytest.raises(CaptureError) as err:
        capture_directory(tmp_path, capture_id)
    assert err.value.translation_key == "invalid_capture_id"
    with pytest.raises(CaptureError):
        delete_capture(tmp_path, capture_id)
    assert list_captures(tmp_path) == []
    assert (tmp_path / capture_id).is_dir()


def test_bootstrap_snapshot_does_not_hold_the_status_lock(tmp_path):
    writer = _writer(tmp_path)
    finished = threading.Event()

    def bootstrap():
        reader = threading.Thread(target=lambda: (writer.status(), finished.set()))
        reader.start()
        reader.join(1)
        return {"gateway_identities": {}}

    writer.record(b"first", 1, bootstrap)
    assert finished.is_set()
    writer.record(b"second", 1, lambda: pytest.fail("bootstrap must run once"))
    manifest = _stop(writer)
    assert manifest["metadata"]["parser_state"] == {"gateway_identities": {}}
    assert manifest["metadata"]["parser_state_at_capture_boundary"] is True
    assert manifest["bytes_written"] == len(b"firstsecond")


def test_status_and_manifest_exclude_directory_unless_requested(tmp_path):
    writer = _writer(tmp_path)
    assert "directory" not in writer.status()
    assert writer.status(include_directory=True)["directory"] == str(writer.directory)
    assert "directory" not in _stop(writer)


def test_export_counts_toward_storage_budget(tmp_path):
    writer = _writer(tmp_path)
    writer.record(b"data" * 100, 1)
    _stop(writer)
    used = sum(p.stat().st_size for p in writer.directory.iterdir())
    with pytest.raises(CaptureError) as err:
        export_archive(writer.directory, max_storage_bytes=used + 10)
    assert err.value.translation_key == "export_budget_exceeded"
    assert err.value.translation_placeholders == {"required_mib": "1", "available_mib": "1"}
    assert not list(writer.root.glob("*.zip"))
    assert export_archive(writer.directory, max_storage_bytes=used * 3).is_file()


def test_delete_capture_removes_only_that_capture_and_its_exports(tmp_path):
    first, second = _writer(tmp_path), None
    _stop(first)
    second = _writer(tmp_path)
    _stop(second)
    archives = [export_archive(first.directory), export_archive(first.directory)]
    kept = export_archive(second.directory)
    result = delete_capture(first.root, first.capture_id)
    assert result["capture_id"] == first.capture_id and result["deleted_archives"] == 2
    assert not first.directory.exists() and not any(path.exists() for path in archives)
    assert second.directory.is_dir() and kept.is_file()
    with pytest.raises(CaptureError) as err:
        delete_capture(first.root, first.capture_id)
    assert err.value.translation_key == "capture_not_found"


@pytest.mark.parametrize("capture_id", ["../captures", "..", "/etc", "x/../../config", ""])
def test_delete_capture_refuses_path_traversal(tmp_path, capture_id):
    writer = _writer(tmp_path)
    _stop(writer)
    with pytest.raises(CaptureError) as err:
        delete_capture(writer.root, capture_id)
    assert err.value.translation_key == "invalid_capture_id"
    assert writer.directory.is_dir()


def test_delete_capture_refuses_symlinks(tmp_path):
    writer = _writer(tmp_path)
    _stop(writer)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep").write_text("keep")
    linked_id = "20260101T000000Z_aaaaaaaaaaaa"
    (writer.root / linked_id).symlink_to(outside, target_is_directory=True)
    with pytest.raises(CaptureError):
        delete_capture(writer.root, linked_id)
    (writer.directory / "link").symlink_to(outside / "keep")
    with pytest.raises(CaptureError) as err:
        delete_capture(writer.root, writer.capture_id)
    assert err.value.translation_key == "capture_unsafe"
    assert (writer.directory / "manifest.json").is_file() and (outside / "keep").is_file()


def test_remove_capture_root(tmp_path):
    writer = _writer(tmp_path)
    _stop(writer)
    export_archive(writer.directory)
    assert remove_capture_root(writer.root) is True
    assert not writer.root.exists()
    assert remove_capture_root(writer.root) is False
    target = tmp_path / "target"
    target.mkdir()
    (target / "keep").write_text("keep")
    link = tmp_path / "link"
    link.symlink_to(target, target_is_directory=True)
    with pytest.raises(CaptureError):
        remove_capture_root(link)
    assert (target / "keep").is_file()
