"""Raw fidelity, bounded I/O, forensic replay, and safe evidence export."""

from datetime import UTC, datetime
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
from unittest.mock import patch
import zipfile

import pytest

from custom_components.pytap.pytap.core.capture import (
    INDEX,
    MANIFEST_RESERVE,
    CapturedChunk,
    CaptureWriter,
    capture_directory,
    export_archive,
    iter_capture,
    list_captures,
    read_manifest,
)
from custom_components.pytap.pytap.core.crc import crc
from custom_components.pytap.pytap.core.replay import FrameScanner, decode_wire_frame, replay_events
from custom_components.pytap.pytap.core.types import FrameType


def _writer(tmp_path, **kwargs):
    writer = CaptureWriter(
        tmp_path / "captures", {"source": {"connection_type": "serial", "baud_rate": 38400}}, **kwargs
    )
    writer.start()
    return writer


def _stop(writer):
    writer.request_stop()
    assert writer.join(3)
    return read_manifest(writer.directory, allow_incomplete=True)


def _wire(frame_type=0x7777, payload=b"\x7e\x24\xff", *, address=0x8001, bad_crc=False):
    body = address.to_bytes(2, "big") + int(frame_type).to_bytes(2, "big") + payload
    checksum = crc(body) ^ (1 if bad_crc else 0)
    escaped = bytearray()
    escape = {0x7E: 0, 0x24: 1, 0x23: 2, 0x25: 3, 0xA4: 4, 0xA3: 5, 0xA5: 6}
    for byte in body + checksum.to_bytes(2, "little"):
        escaped.extend((0x7E, escape[byte])) if byte in escape else escaped.append(byte)
    return b"\x7e\x07" + bytes(escaped) + b"\x7e\x08"


def test_fidelity_rotation_private_permissions_and_connections(tmp_path):
    writer = _writer(tmp_path, segment_bytes=7)
    data = b"noise\x00\xff" + _wire() + _wire(bad_crc=True)
    writer.record(data[:11], 3)
    writer.mark("connection_closed", 3)
    writer.mark("connection_open", 4)
    writer.record(data[11:], 4)
    manifest = _stop(writer)
    chunks = list(iter_capture(writer.directory))
    assert b"".join(c.data for c in chunks) == data
    assert chunks[0].connection_id == 3 and chunks[-1].connection_id == 4
    assert all(c.utc_ns > 0 and c.monotonic_ns > 0 for c in chunks)
    assert manifest["complete"] is True
    assert manifest["bytes_written"] == len(data)
    assert len(manifest["segments"]) > 1
    assert all(s["bytes"] <= 7 for s in manifest["segments"])
    if os.name == "posix":
        assert writer.directory.stat().st_mode & 0o777 == 0o700
        assert all(p.stat().st_mode & 0o077 == 0 for p in writer.directory.iterdir())
    assert [
        json.loads(line)["event"] for line in (writer.directory / "events.jsonl").read_text().splitlines()
    ] == ["connection_closed", "connection_open"]


def test_duration_limit_stops_even_without_bus_data(tmp_path):
    writer = _writer(tmp_path, duration_seconds=0.01)
    assert writer.join(2)
    manifest = read_manifest(writer.directory)
    assert manifest["stop_reason"] == "duration_limit"
    assert manifest["complete"] and manifest["bytes_written"] == 0


def test_storage_limit_marks_loss_without_deleting_prior_evidence(tmp_path):
    first = _writer(tmp_path)
    first.record(b"previous evidence", 1)
    first_manifest = _stop(first)
    first_files = {p.name: p.read_bytes() for p in first.directory.iterdir()}
    existing = sum(len(value) for value in first_files.values())
    second = _writer(tmp_path, max_storage_bytes=existing + MANIFEST_RESERVE + INDEX.size + 2)
    second.record(b"cannot fit", 1)
    assert second.join(2)
    manifest = read_manifest(second.directory, allow_incomplete=True)
    assert manifest["stop_reason"] == "storage_limit"
    assert not manifest["complete"]
    assert manifest["dropped_bytes"] == len(b"cannot fit")
    assert {p.name: p.read_bytes() for p in first.directory.iterdir()} == first_files
    assert first_manifest["complete"]


def test_queue_overflow_never_blocks_producer_and_drains_earlier_data(tmp_path):
    writer = _writer(tmp_path, queue_records=1)
    entered, release = threading.Event(), threading.Event()
    original = writer._write_chunk

    def blocked(chunk):
        entered.set()
        assert release.wait(2)
        return original(chunk)

    with patch.object(writer, "_write_chunk", side_effect=blocked):
        writer.record(b"first", 1)
        assert entered.wait(1)
        writer.record(b"second", 1)
        writer.record(b"lost", 1)
        assert writer.status()["stop_reason"] == "queue_overflow"
        release.set()
        assert writer.join(2)
    manifest = read_manifest(writer.directory, allow_incomplete=True)
    assert not manifest["complete"] and manifest["dropped_bytes"] == 4
    assert b"".join(c.data for c in iter_capture(writer.directory, allow_incomplete=True)) == b"firstsecond"
    with pytest.raises(ValueError, match="incomplete"):
        list(iter_capture(writer.directory))


def test_disk_error_is_reported_without_sensitive_exception_text(tmp_path):
    writer = _writer(tmp_path)
    with patch.object(writer, "_write_chunk", side_effect=OSError("secret endpoint and payload")):
        writer.record(b"bytes", 1)
        assert writer.join(2)
    status = writer.status()
    assert status["state"] == "error"
    assert status["error"] == "OSError"
    assert status["dropped_bytes"] == 5
    assert "secret" not in json.dumps(status)


def test_cancelled_prepare_does_not_activate_capture(tmp_path):
    writer = CaptureWriter(tmp_path / "capture", {})
    original = writer._open_segment

    def cancel_during_prepare():
        original()
        writer.request_stop("start_cancelled")

    with patch.object(writer, "_open_segment", side_effect=cancel_during_prepare):
        writer.start()
    writer.record(b"not admitted", 1)
    assert writer.join() and writer.status()["state"] == "stopped"
    assert writer.status()["bytes_observed"] == 0


def test_root_symlink_is_rejected(tmp_path):
    actual = tmp_path / "actual"
    actual.mkdir()
    link = tmp_path / "link"
    link.symlink_to(actual, target_is_directory=True)
    with pytest.raises(ValueError, match="symlink"):
        CaptureWriter(link, {}).start()


def test_archive_contains_all_raw_evidence_and_does_not_overwrite(tmp_path):
    writer = _writer(tmp_path)
    writer.record(_wire(), 1)
    _stop(writer)
    archive = export_archive(writer.directory)
    with zipfile.ZipFile(archive) as zipped:
        assert zipped.read(f"{writer.capture_id}/bus-000000.bin") == _wire()
        assert (
            zipped.read(f"{writer.capture_id}/manifest.json")
            == (writer.directory / "manifest.json").read_bytes()
        )
    assert export_archive(writer.directory) != archive
    assert list_captures(writer.root)[0]["capture_id"] == writer.capture_id
    assert capture_directory(writer.root, writer.capture_id) == writer.directory


@pytest.mark.parametrize("capture_id", ["../other", "/etc", "invalid", "x/../../config"])
def test_capture_ids_cannot_escape_root(tmp_path, capture_id):
    with pytest.raises(ValueError):
        capture_directory(tmp_path, capture_id)


def test_corrupt_segment_is_not_replayed_as_valid_evidence(tmp_path):
    writer = _writer(tmp_path)
    writer.record(b"original", 1)
    _stop(writer)
    (writer.directory / "bus-000000.bin").write_bytes(b"tampered")
    with pytest.raises(ValueError, match="checksum"):
        list(iter_capture(writer.directory))
    with pytest.raises(ValueError, match="checksum"):
        list(iter_capture(writer.directory, allow_incomplete=True))


def test_interrupted_capture_preserves_partial_evidence_with_warnings(tmp_path):
    writer = _writer(tmp_path)
    writer.record(b"evidence", 1)
    manifest = _stop(writer)
    manifest.update(state="capturing", complete=False)
    (writer.directory / "manifest.json").write_text(json.dumps(manifest))
    (writer.directory / "bus-000000.bin").write_bytes(b"evid")
    with pytest.warns(RuntimeWarning):
        chunks = list(iter_capture(writer.directory, allow_incomplete=True))
    assert chunks[0].data == b"evid" and chunks[0].declared_length == 8
    with pytest.raises(ValueError, match="Stop"):
        export_archive(writer.directory)
    with pytest.warns(RuntimeWarning):
        archive = export_archive(writer.directory, allow_interrupted=True)
    with zipfile.ZipFile(archive) as zipped:
        assert zipped.read(f"{writer.capture_id}/bus-000000.bin") == b"evid"


def test_capture_segments_cannot_be_symlinks(tmp_path):
    writer = _writer(tmp_path)
    writer.record(b"data", 1)
    _stop(writer)
    segment = writer.directory / "bus-000000.bin"
    copied = tmp_path / "copy"
    copied.write_bytes(segment.read_bytes())
    segment.unlink()
    segment.symlink_to(copied)
    with pytest.raises(ValueError, match="regular"):
        list(iter_capture(writer.directory))


def test_manifest_segment_paths_are_validated(tmp_path):
    writer = _writer(tmp_path)
    manifest = _stop(writer)
    manifest["segments"][0]["file"] = "../../config/secrets.yaml"
    (writer.directory / "manifest.json").write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match="Unsafe"):
        list(iter_capture(writer.directory))


def test_untrusted_index_cannot_request_an_unbounded_read(tmp_path):
    writer = _writer(tmp_path)
    writer.record(b"small", 1)
    manifest = _stop(writer)
    index = INDEX.pack(1, 1, 0, 0xFFFFFFFF, 1)
    (writer.directory / "chunks-000000.bin").write_bytes(index)
    manifest["segments"][0]["index_sha256"] = hashlib.sha256(index).hexdigest()
    (writer.directory / "manifest.json").write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match="ordering or length"):
        list(iter_capture(writer.directory))


def test_interrupted_archive_preserves_unlisted_rotated_segments(tmp_path):
    writer = _writer(tmp_path, segment_bytes=4)
    writer.record(b"12345678", 1)
    manifest = _stop(writer)
    manifest.update(state="capturing", complete=False)
    unlisted = manifest["segments"].pop()
    (writer.directory / "manifest.json").write_text(json.dumps(manifest))
    with pytest.warns(RuntimeWarning):
        archive = export_archive(writer.directory, allow_interrupted=True)
    with zipfile.ZipFile(archive) as zipped:
        assert zipped.read(f"{writer.capture_id}/{unlisted['file']}") == b"5678"
        recovery = json.loads(zipped.read(f"{writer.capture_id}/recovery.json"))
        assert recovery["complete"] is False
        assert set(recovery["unverified_files"]) == {unlisted["file"], unlisted["index"]}


def test_scanner_retains_unknown_bad_crc_and_noise_across_reads():
    wire = _wire() + _wire(bad_crc=True)
    scanner = FrameScanner()
    records = []
    data = b"\x00noise\xff" + wire
    for offset in range(0, len(data), 3):
        records.extend(scanner.feed(CapturedChunk(100, 200, offset, 1, data[offset : offset + 3])))
    records.extend(scanner.finish())
    assert b"".join(bytes.fromhex(row["wire_hex"]) for row in records) == data
    frames = [r for r in records if r["kind"] == "frame"]
    assert frames[0]["frame_name"] == "UNKNOWN" and frames[0]["crc_valid"] is True
    assert frames[1]["crc_valid"] is False
    assert frames[0]["payload_hex"] == "7e24ff"


def test_scanner_resets_at_reconnect_and_retains_partial_frames():
    scanner = FrameScanner()
    wire = _wire()
    records = list(scanner.feed(CapturedChunk(1, 1, 0, 1, wire[:6])))
    records += list(scanner.feed(CapturedChunk(2, 2, 6, 2, wire)))
    records += list(scanner.finish())
    assert records[0]["kind"] == "incomplete"
    assert records[-1]["kind"] == "frame" and records[-1]["connection_id"] == 2


def test_unknown_escapes_and_oversized_noise_are_not_silently_lost():
    scanner = FrameScanner(max_frame_bytes=8)
    data = b"\x7e\x07" + b"x" * 30
    records = list(scanner.feed(CapturedChunk(1, 1, 0, 1, data))) + list(scanner.finish())
    assert b"".join(bytes.fromhex(r["wire_hex"]) for r in records) == data
    invalid = decode_wire_frame(b"\x7e\x07\x01\x7e\x09\x7e\x08")
    assert invalid["framing_error"] == "unknown_escape" and invalid["crc_valid"] is None


def test_measurement_export_retains_three_unknown_and_two_extra_bytes():
    power = ((600 << 12) | 300).to_bytes(3, "big") + b"\x80" + ((400 << 12) | 250).to_bytes(3, "big")
    power += b"\xaa\xbb\xcc\x00\x00\x64\xde\xad"
    packet = b"\x31\x00\x02\x00\x02\x01\x0f" + power
    frame = decode_wire_frame(_wire(FrameType.RECEIVE_RESPONSE, b"\x00\x1f\x01\x00\x00" + packet))
    measurement = frame["receive"]["packets"][0]["measurement"]
    assert measurement["unknown_hex"] == "aabbcc" and measurement["extra_hex"] == "dead"
    assert measurement["voltage_in"] == 30 and measurement["current_in"] == 2


def test_replay_uses_recorded_time_for_slot_clock_not_wall_clock():
    power = b"\x00" * 13
    request = _wire(FrameType.RECEIVE_REQUEST, b"\x00\x00\x00\x01\x00", address=1)
    response = _wire(
        FrameType.RECEIVE_RESPONSE, b"\x00\x1f\x01\x00\x00" + b"\x31\x00\x02\x00\x02\x01\x0d" + power
    )
    chunks = iter(
        [
            CapturedChunk(1_000_000_000, 1, 0, 1, request),
            CapturedChunk(2_000_000_000, 2, len(request), 1, response),
        ]
    )
    events = list(replay_events(chunks))
    assert len(events) == 1
    assert events[0]["event"]["timestamp"] == datetime.fromtimestamp(1, UTC).isoformat()


def test_invalid_receive_slot_does_not_abort_inspection_or_replay():
    request = _wire(FrameType.RECEIVE_REQUEST, b"\x00\x00\x00\x01\x00", address=1)
    response = _wire(FrameType.RECEIVE_RESPONSE, b"\x00\x1f\x00\xff\xff")
    assert decode_wire_frame(response)["crc_valid"] is True
    chunks = [
        CapturedChunk(1_000_000_000, 1, 0, 1, request),
        CapturedChunk(2_000_000_000, 2, len(request), 1, response),
    ]
    assert list(replay_events(iter(chunks))) == []


def test_offline_cli_exports_without_touching_source(tmp_path):
    writer = _writer(tmp_path)
    writer.record(_wire() + _wire(bad_crc=True), 1)
    _stop(writer)
    original = (writer.directory / "manifest.json").read_bytes()
    script = Path(__file__).resolve().parents[1] / "scripts" / "replay_capture.py"
    output = tmp_path / "export"
    result = subprocess.run(
        [sys.executable, str(script), str(writer.directory), "--output", str(output)],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    summary = json.loads((output / "summary.json").read_text())
    assert summary["records"]["frame"] == 2
    assert summary["frames_live_parser_would_drop"] == {"crc_mismatch": 1}
    assert (writer.directory / "manifest.json").read_bytes() == original
    result = subprocess.run(
        [sys.executable, str(script), str(writer.directory), "--output", str(output)], capture_output=True
    )
    assert result.returncode == 1  # Never overwrite an earlier export.
