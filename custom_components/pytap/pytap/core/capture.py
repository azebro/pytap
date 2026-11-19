"""Bounded, opt-in byte recording. No Home Assistant or serial dependencies.

Chunk indexes use little-endian <QQQII: UTC nanoseconds, monotonic nanoseconds,
logical stream offset, byte length, connection generation. Raw data is never
decoded or filtered on the producer thread.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
import errno
import hashlib
import json
import os
from pathlib import Path
import queue
import re
import shutil
import stat
import struct
import tempfile
import threading
import time
from typing import Any, BinaryIO
import uuid
import warnings
import zipfile

INDEX = struct.Struct("<QQQII")
MAX_RECORD_BYTES = 65536
# Format 2 adds the checksummed "events" block. Format 1 (no event-log
# checksum) is still readable; its event log is reported as unverified.
FORMAT_VERSION = 2
_SUPPORTED_FORMATS = (1, FORMAT_VERSION)
MANIFEST_RESERVE = 1024 * 1024
FREE_SPACE_RESERVE = 64 * 1024 * 1024
MANIFEST_INTERVAL = 10.0
EVENTS_FILE = "events.jsonl"
CAPTURE_ID_PATTERN = re.compile(r"^[0-9]{8}T[0-9]{6}Z_[a-f0-9]{12}$")
_SEGMENT_PATTERN = re.compile(r"^bus-[0-9]{6}\.bin$")
_UNLISTED_PATTERN = re.compile(r"^(?:bus|chunks)-[0-9]{6}\.bin$")
_ARCHIVE_PATTERN = re.compile(r"^(?P<capture_id>[0-9]{8}T[0-9]{6}Z_[a-f0-9]{12})-[a-f0-9]{8}\.zip$")
_SHA256_PATTERN = re.compile(r"^[a-f0-9]{64}$")
_STATES = frozenset({"idle", "starting", "capturing", "stopping", "stopped", "error"})
_COUNTERS = (
    "bytes_observed",
    "bytes_written",
    "records_written",
    "dropped_bytes",
    "dropped_records",
    "session_budget_bytes",
    "storage_bytes_used",
    "max_storage_bytes",
    "queue_depth",
)


class CaptureError(ValueError):
    """A code-authored, user-actionable condition; safe to show and translate."""

    def __init__(self, translation_key: str, message: str, **placeholders: str) -> None:
        super().__init__(message)
        self.translation_key = translation_key
        self.translation_placeholders = placeholders


class CaptureRuntimeError(CaptureError):
    """A translatable runtime failure (e.g. disk space), not a problem with the request."""


def _mib(value: int) -> str:
    return str(max(0, -(-value // (1024 * 1024))))


def _check_root(root: Path) -> None:
    if root.is_symlink():
        raise CaptureError("capture_unsafe", "Capture root must not be a symlink")


def _private_file(path: Path) -> BinaryIO:
    return os.fdopen(os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600), "wb")


def _fsync_dir(path: Path) -> None:
    """Best-effort durability for directory entries (POSIX only)."""
    if os.name != "posix":
        return
    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    except OSError:
        return
    try:
        os.fsync(descriptor)
    except OSError:
        pass
    finally:
        os.close(descriptor)


def _atomic_write(path: Path, data: bytes) -> None:
    temp = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix=".manifest-", delete=False) as handle:
            temp = Path(handle.name)
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
        temp = None
    finally:
        if temp is not None:
            temp.unlink(missing_ok=True)
    _fsync_dir(path.parent)


def _tree_size(root: Path) -> int:
    """Count allocated evidence, without following links or removing anything."""
    return sum(p.stat().st_size for p in root.rglob("*") if not p.is_symlink() and p.is_file())


@dataclass(frozen=True)
class CapturedChunk:
    utc_ns: int
    monotonic_ns: int
    stream_offset: int
    connection_id: int
    data: bytes
    declared_length: int | None = None


class CaptureWriter:
    """One session, one writer. Producers only enqueue or record explicit loss."""

    def __init__(
        self,
        root: Path,
        metadata: Mapping[str, Any],
        *,
        duration_seconds: float = 86400,
        max_storage_bytes: int = 1024 * 1024 * 1024,
        segment_bytes: int = 16 * 1024 * 1024,
        queue_records: int = 2048,
    ) -> None:
        if (
            duration_seconds <= 0
            or max_storage_bytes <= MANIFEST_RESERVE
            or segment_bytes < 1
            or queue_records < 1
        ):
            raise ValueError("Invalid capture limits")
        self.root = Path(root)
        self.capture_id = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ_") + uuid.uuid4().hex[:12]
        self.directory = self.root / self.capture_id
        self.metadata = dict(metadata)
        self.duration_seconds = duration_seconds
        self.max_storage_bytes = max_storage_bytes
        self.segment_bytes = segment_bytes
        self._queue: queue.Queue[CapturedChunk | dict[str, Any]] = queue.Queue(queue_records)
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._accepting = False
        self._bootstrap_done = False
        self._state = "idle"
        self._reason: str | None = None
        self._error: str | None = None
        self._started_ns: int | None = None
        self._ended_ns: int | None = None
        self._deadline = 0.0
        self._observed_bytes = 0
        self._written_bytes = 0
        self._written_records = 0
        self._dropped_bytes = 0
        self._dropped_records = 0
        self._used_bytes = 0
        self._session_budget = 0
        self._segments: list[dict[str, Any]] = []
        self._raw: BinaryIO | None = None
        self._index: BinaryIO | None = None
        self._events: BinaryIO | None = None
        self._raw_hash = hashlib.sha256()
        self._index_hash = hashlib.sha256()
        self._events_hash = hashlib.sha256()
        self._events_bytes = 0
        self._manifest_data: bytes | None = None
        self._new_entries = False

    @property
    def busy(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def status(self, *, include_directory: bool = False) -> dict[str, Any]:
        """Counters only; the local path is opt-in for admin responses."""
        with self._lock:
            status = {
                "capture_id": self.capture_id,
                "state": self._state,
                "stop_reason": self._reason,
                "error": self._error,
                "started_utc_ns": self._started_ns,
                "ended_utc_ns": self._ended_ns,
                "bytes_observed": self._observed_bytes,
                "bytes_written": self._written_bytes,
                "records_written": self._written_records,
                "dropped_bytes": self._dropped_bytes,
                "dropped_records": self._dropped_records,
                "complete": self._state == "stopped"
                and self._started_ns is not None
                and not self._dropped_records
                and self._error is None,
                "duration_seconds": self.duration_seconds,
                "max_storage_bytes": self.max_storage_bytes,
                "session_budget_bytes": self._session_budget,
                "storage_bytes_used": self._used_bytes,
                "queue_depth": self._queue.qsize(),
            }
        if include_directory:
            status["directory"] = str(self.directory)
        return status

    def start(self) -> None:
        """Prepare private files and start the writer; call off an event loop."""
        with self._lock:
            if self._state != "idle":
                raise ValueError("Capture has already started")
            self._state = "starting"
        try:
            _check_root(self.root)
            self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
            self._session_budget = min(
                self.max_storage_bytes - _tree_size(self.root),
                shutil.disk_usage(self.root).free - FREE_SPACE_RESERVE,
            )
            if self._session_budget <= MANIFEST_RESERVE:
                raise CaptureError(
                    "storage_budget_exhausted",
                    "Capture storage budget exhausted",
                    available_mib=_mib(self._session_budget),
                )
            self.directory.mkdir(mode=0o700)
            _fsync_dir(self.root)
            self._events = _private_file(self.directory / EVENTS_FILE)
            self._new_entries = True
            self._open_segment()
            with self._lock:
                if not self._stop.is_set():
                    self._started_ns = time.time_ns()
                    self._deadline = time.monotonic() + self.duration_seconds
                    self._state = "capturing"
            self._write_manifest()
            # The coordinator publishes this object before start(). Only admit
            # reads after all files/metadata are ready, avoiding a startup gap.
            with self._lock:
                cancelled = self._stop.is_set()
                self._accepting = not cancelled
            if cancelled:
                self._close_files()
                with self._lock:
                    if self._state != "error":
                        self._state = "stopped"
                self._try_manifest()
                return
            self._thread = threading.Thread(target=self._run, name="pytap_capture", daemon=True)
            self._thread.start()
        except BaseException as err:
            self._fail(err, "start_failed")
            self._close_files()
            self._try_manifest()
            raise

    def _stop_locked(self, reason: str) -> None:
        self._accepting = False
        if self._reason is None:
            self._reason = reason
            self._ended_ns = time.time_ns()
        if self._state != "error":
            self._state = "stopping"
        self._stop.set()

    def request_stop(self, reason: str = "user") -> None:
        with self._lock:
            if self._state == "idle":
                # An async caller can cancel after publishing the writer but
                # before its executor starts preparation. Latch that stop.
                if self._reason is None:
                    self._reason = reason
                    self._ended_ns = time.time_ns()
                self._stop.set()
            elif self._state in ("capturing", "starting", "stopping"):
                self._stop_locked(reason)

    def join(self, timeout: float = 10) -> bool:
        if self._thread is not None:
            self._thread.join(timeout)
        return not self.busy

    def record(
        self, data: bytes, connection_id: int, bootstrap: Callable[[], Mapping[str, Any]] | None = None
    ) -> None:
        """Copy/queue bytes before parsing. Never wait on the writer or disk."""
        with self._lock:
            if not self._accepting or not data:
                return
            if time.monotonic() >= self._deadline:
                self._stop_locked("duration_limit")
                return
            snapshot_needed = not self._bootstrap_done and bootstrap is not None
        snapshot = None
        if snapshot_needed:
            # Called by the sole listener before it parses this read, so the
            # parser cannot advance meanwhile; status() never waits on this.
            try:
                snapshot = dict(bootstrap())
            except Exception as err:
                with self._lock:
                    self._error = type(err).__name__
                    self._dropped_records += 1
                    self._dropped_bytes += len(data)
                    self._observed_bytes += len(data)
                    self._stop_locked("bootstrap_error")
                    self._state = "error"
                return
        with self._lock:
            if not self._accepting:
                return
            if snapshot is not None and not self._bootstrap_done:
                self.metadata["parser_state"] = snapshot
                self.metadata["parser_state_at_capture_boundary"] = True
                self._bootstrap_done = True
            chunk = CapturedChunk(
                time.time_ns(), time.monotonic_ns(), self._observed_bytes, connection_id, bytes(data)
            )
            self._observed_bytes += len(data)
            try:
                self._queue.put_nowait(chunk)
            except queue.Full:
                self._dropped_records += 1
                self._dropped_bytes += len(data)
                self._stop_locked("queue_overflow")

    def mark(self, event: str, connection_id: int, **details: str | bool) -> None:
        """Connection boundaries, without endpoints or backend exception text."""
        with self._lock:
            if not self._accepting:
                return
            record = {
                "event": event,
                "utc_ns": time.time_ns(),
                "monotonic_ns": time.monotonic_ns(),
                "connection_id": connection_id,
                **details,
            }
            try:
                self._queue.put_nowait(record)
            except queue.Full:
                self._dropped_records += 1
                self._stop_locked("queue_overflow")

    def _fail(self, err: BaseException, reason: str) -> None:
        with self._lock:
            self._error = type(err).__name__  # Messages may contain private data.
            self._stop_locked(reason)
            self._state = "error"

    def _open_segment(self) -> None:
        number = len(self._segments)
        raw_name, index_name = f"bus-{number:06d}.bin", f"chunks-{number:06d}.bin"
        self._raw = _private_file(self.directory / raw_name)
        self._index = _private_file(self.directory / index_name)
        self._new_entries = True
        self._raw_hash, self._index_hash = hashlib.sha256(), hashlib.sha256()
        self._segments.append({"file": raw_name, "index": index_name, "bytes": 0, "records": 0})

    def _close_segment(self) -> None:
        error = None
        for handle in (self._raw, self._index):
            if handle is not None:
                try:
                    handle.flush()
                    os.fsync(handle.fileno())
                except OSError as err:
                    error = err
                finally:
                    try:
                        handle.close()
                    except OSError as err:
                        error = err
        self._raw = self._index = None
        if self._segments:
            self._segments[-1].update(
                sha256=self._raw_hash.hexdigest(), index_sha256=self._index_hash.hexdigest()
            )
        if error is not None:
            raise error

    def _close_files(self) -> None:
        error = None
        try:
            self._close_segment()
        except OSError as err:
            error = err
        if self._events is not None:
            try:
                self._events.flush()
                os.fsync(self._events.fileno())
            except OSError as err:
                error = err
            finally:
                try:
                    self._events.close()
                except OSError as err:
                    error = err
                self._events = None
        if error is not None:
            self._fail(error, "disk_error")

    def _budget(self, required: int) -> bool:
        if self._used_bytes + required + MANIFEST_RESERVE <= self._session_budget:
            return True
        self.request_stop("storage_limit")
        return False

    def _drop(self, item: CapturedChunk | dict[str, Any], byte_count: int | None = None) -> None:
        with self._lock:
            self._dropped_records += 1
            if isinstance(item, CapturedChunk):
                self._dropped_bytes += len(item.data) if byte_count is None else byte_count

    def _write_chunk(self, chunk: CapturedChunk) -> bool:
        assert self._raw is not None and self._index is not None
        remaining, space, extra_records = len(chunk.data), self.segment_bytes - self._segments[-1]["bytes"], 0
        while remaining:
            space = space or self.segment_bytes
            length = min(remaining, space, MAX_RECORD_BYTES)
            remaining -= length
            space -= length
            extra_records += 1
        if not self._budget(len(chunk.data) + INDEX.size * extra_records):
            self._drop(chunk)
            return False
        consumed = 0
        while consumed < len(chunk.data):
            segment = self._segments[-1]
            if segment["bytes"] == self.segment_bytes:
                self._close_segment()
                self._open_segment()
                self._write_manifest()
                segment = self._segments[-1]
            length = min(len(chunk.data) - consumed, self.segment_bytes - segment["bytes"], MAX_RECORD_BYTES)
            data = chunk.data[consumed : consumed + length]
            index = INDEX.pack(
                chunk.utc_ns, chunk.monotonic_ns, chunk.stream_offset + consumed, length, chunk.connection_id
            )
            self._raw.write(data)
            self._index.write(index)
            self._raw_hash.update(data)
            self._index_hash.update(index)
            segment["bytes"] += length
            segment["records"] += 1
            consumed += length
            self._used_bytes += length + INDEX.size
            with self._lock:
                self._written_bytes += length
                self._written_records += 1
        return True

    def _write_event(self, data: bytes) -> None:
        assert self._events is not None
        self._events.write(data)
        self._events_hash.update(data)
        self._events_bytes += len(data)
        self._used_bytes += len(data)

    def _sync_data(self) -> None:
        """Make referenced bytes durable before a manifest can vouch for them."""
        for handle in (self._raw, self._index, self._events):
            if handle is not None:
                handle.flush()
                os.fsync(handle.fileno())
        if self._new_entries:
            # New segment names must be durable before a manifest references them.
            _fsync_dir(self.directory)
            self._new_entries = False

    def _write_manifest(self) -> None:
        """Rewrite only on change; data files are fsynced first, then the directory."""
        with self._lock:
            metadata = dict(self.metadata)
        segments = [dict(segment) for segment in self._segments]
        if segments:
            segments[-1].update(sha256=self._raw_hash.hexdigest(), index_sha256=self._index_hash.hexdigest())
        data = json.dumps(
            {
                **self.status(),
                "format_version": FORMAT_VERSION,
                "index_format": INDEX.format,
                "metadata": metadata,
                "segments": segments,
                "events": {
                    "file": EVENTS_FILE,
                    "bytes": self._events_bytes,
                    "sha256": self._events_hash.hexdigest(),
                },
            },
            separators=(",", ":"),
            ensure_ascii=True,
        ).encode()
        if len(data) > MANIFEST_RESERVE:
            raise ValueError("Capture manifest exceeds reserved storage")
        if data == self._manifest_data:
            return
        self._sync_data()
        _atomic_write(self.directory / "manifest.json", data)
        self._manifest_data = data

    def _try_manifest(self) -> None:
        try:
            if self.directory.is_dir():
                self._write_manifest()
        except (OSError, ValueError) as err:
            self._fail(err, "disk_error")

    def _run(self) -> None:
        last_flush = last_manifest = time.monotonic()
        current = None
        written_before = self._written_bytes
        try:
            while True:
                if time.monotonic() >= self._deadline:
                    self.request_stop("duration_limit")
                if self._stop.is_set() and self._queue.empty():
                    break
                try:
                    current = self._queue.get(timeout=0.25)
                except queue.Empty:
                    current = None
                if isinstance(current, CapturedChunk):
                    written_before = self._written_bytes
                    if not self._write_chunk(current):
                        current = None
                        break
                elif current is not None:
                    data = (json.dumps(current, separators=(",", ":")) + "\n").encode()
                    if not self._budget(len(data)):
                        self._drop(current)
                        current = None
                        break
                    self._write_event(data)
                current = None
                now = time.monotonic()
                if now - last_flush >= 1:
                    for handle in (self._raw, self._index, self._events):
                        if handle is not None:
                            handle.flush()
                    last_flush = now
                    if shutil.disk_usage(self.directory).free < FREE_SPACE_RESERVE // 2:
                        self.request_stop("disk_space_low")
                        break
                if now - last_manifest >= MANIFEST_INTERVAL:
                    self._write_manifest()
                    last_manifest = now
        except BaseException as err:
            if current is not None:
                remaining = (
                    max(0, len(current.data) - (self._written_bytes - written_before))
                    if isinstance(current, CapturedChunk)
                    else None
                )
                self._drop(current, remaining)
            self._fail(err, "disk_error")
        finally:
            while True:
                try:
                    self._drop(self._queue.get_nowait())
                except queue.Empty:
                    break
            self._close_files()
            with self._lock:
                self._accepting = False
                if self._state != "error":
                    self._state = "stopped"
                self._ended_ns = self._ended_ns or time.time_ns()
            self._try_manifest()


def _check_capture_id(capture_id: Any) -> None:
    if not isinstance(capture_id, str) or not CAPTURE_ID_PATTERN.fullmatch(capture_id):
        raise CaptureError("invalid_capture_id", "Invalid capture ID")


def capture_directory(root: Path, capture_id: str) -> Path:
    """Resolve a generated session ID, never an arbitrary caller-supplied path."""
    _check_capture_id(capture_id)
    root = Path(root)
    _check_root(root)
    directory = root / capture_id
    if directory.is_symlink() or not directory.is_dir():
        raise CaptureError("capture_not_found", "Capture directory is unavailable", capture_id=capture_id)
    return directory


def _is_count(value: Any) -> bool:
    return type(value) is int and value >= 0


def _is_digest(value: Any) -> bool:
    return isinstance(value, str) and _SHA256_PATTERN.fullmatch(value) is not None


def _validate_manifest(manifest: Any, directory: Path) -> None:
    if not isinstance(manifest, dict):
        raise ValueError("Invalid capture manifest")
    version = manifest.get("format_version")
    if (
        type(version) is not int
        or version not in _SUPPORTED_FORMATS
        or manifest.get("index_format") != INDEX.format
    ):
        raise ValueError("Unsupported capture format")
    if manifest.get("capture_id") != directory.name or not CAPTURE_ID_PATTERN.fullmatch(directory.name):
        raise ValueError("Capture manifest does not match its directory")
    if manifest.get("state") not in _STATES or type(manifest.get("complete")) is not bool:
        raise ValueError("Invalid capture manifest state")
    if not all(_is_count(manifest.get(key)) for key in _COUNTERS):
        raise ValueError("Invalid capture manifest counters")
    for key in ("started_utc_ns", "ended_utc_ns", "stop_reason", "error"):
        value = manifest.get(key)
        if value is not None and not (_is_count(value) if key.endswith("_ns") else isinstance(value, str)):
            raise ValueError("Invalid capture manifest timestamps or reasons")
    duration = manifest.get("duration_seconds")
    if (
        type(duration) not in (int, float)
        or not duration > 0
        or not isinstance(manifest.get("metadata"), dict)
    ):
        raise ValueError("Invalid capture manifest metadata")
    events = manifest.get("events")
    if version >= 2 and (
        not isinstance(events, dict)
        or events.get("file") != EVENTS_FILE
        or not _is_count(events.get("bytes"))
        or not _is_digest(events.get("sha256"))
    ):
        raise ValueError("Invalid capture manifest event log")
    segments = manifest.get("segments")
    if not isinstance(segments, list):
        raise ValueError("Invalid capture manifest segments")
    for number, segment in enumerate(segments):
        if not isinstance(segment, dict):
            raise ValueError("Invalid capture manifest segment")
        name = segment.get("file")
        if not isinstance(name, str) or not _SEGMENT_PATTERN.fullmatch(name):
            raise ValueError("Unsafe segment filename")
        if name != f"bus-{number:06d}.bin" or segment.get("index") != f"chunks-{number:06d}.bin":
            raise ValueError("Invalid segment index filename")
        if not (
            _is_count(segment.get("bytes"))
            and _is_count(segment.get("records"))
            and _is_digest(segment.get("sha256"))
            and _is_digest(segment.get("index_sha256"))
        ):
            raise ValueError("Invalid capture manifest segment counters")


def read_manifest(directory: Path, *, allow_incomplete: bool = False) -> dict[str, Any]:
    path = directory / "manifest.json"
    if path.is_symlink():
        raise ValueError("Capture manifest must not be a symlink")
    if path.stat().st_size > MANIFEST_RESERVE:
        raise ValueError("Capture manifest is too large")
    try:
        manifest = json.loads(path.read_text())
    except RecursionError:
        raise ValueError("Capture manifest is too deeply nested") from None
    _validate_manifest(manifest, directory)
    if not allow_incomplete and (manifest["state"] != "stopped" or not manifest["complete"]):
        raise ValueError("Capture is incomplete; explicitly allow incomplete evidence to inspect it")
    return manifest


def _segment_paths(directory: Path, segment: Mapping[str, Any]) -> tuple[Path, Path]:
    name = segment.get("file", "")
    if not isinstance(name, str) or not _SEGMENT_PATTERN.fullmatch(name):
        raise ValueError("Unsafe segment filename")
    expected_index = name.replace("bus-", "chunks-")
    if segment.get("index") != expected_index:
        raise ValueError("Invalid segment index filename")
    paths = directory / name, directory / expected_index
    if any(path.is_symlink() or not path.is_file() for path in paths):
        raise ValueError("Capture segments must be regular files")
    return paths


def _events_path(directory: Path) -> Path:
    path = directory / EVENTS_FILE
    if path.is_symlink() or not path.is_file():
        raise ValueError("Capture event log must be a regular file")
    return path


_FORMAT_1_EVENTS_WARNING = "Format 1 capture: the event log has no checksum and is unverified"


def _verify_file(path: Path, size: int, digest: str, strict: bool, warn: Callable[[str], None]) -> None:
    if path.stat().st_size != size:
        if strict:
            raise ValueError("Capture size mismatch (possibly an interrupted write)")
        warn("Incomplete evidence has stale file sizes")
    with path.open("rb") as handle:
        if hashlib.file_digest(handle, "sha256").hexdigest() != digest:
            if strict:
                raise ValueError("Capture checksum mismatch")
            warn("Incomplete evidence has unverified checksums")


def _warn(message: str) -> None:
    warnings.warn(message, RuntimeWarning, stacklevel=2)


def _iter_capture(
    directory: Path, allow_incomplete: bool, warn: Callable[[str], None]
) -> Iterator[CapturedChunk]:
    manifest = read_manifest(directory, allow_incomplete=allow_incomplete)
    strict = not allow_incomplete or manifest["complete"]
    if manifest["format_version"] < 2:
        warn(_FORMAT_1_EVENTS_WARNING)
    else:
        events = manifest["events"]
        _verify_file(_events_path(directory), events["bytes"], events["sha256"], strict, warn)
    previous_offset = 0
    total_bytes = 0
    halted = False
    for segment in manifest["segments"]:
        raw_path, index_path = _segment_paths(directory, segment)
        _verify_file(raw_path, segment["bytes"], segment["sha256"], strict, warn)
        _verify_file(index_path, segment["records"] * INDEX.size, segment["index_sha256"], strict, warn)
        with raw_path.open("rb") as raw, index_path.open("rb") as index:
            while record := index.read(INDEX.size):
                if len(record) != INDEX.size:
                    if strict:
                        raise ValueError("Truncated chunk index")
                    warn("Incomplete evidence ends with a partial index row")
                    break
                utc_ns, mono_ns, offset, length, connection = INDEX.unpack(record)
                if not 0 < length <= MAX_RECORD_BYTES or offset < previous_offset:
                    if strict:
                        raise ValueError("Invalid chunk ordering or length")
                    # E.g. a zero-filled tail after power loss; nothing later is trustworthy.
                    warn("Incomplete evidence has an invalid index row; replay stops there")
                    halted = True
                    break
                data = raw.read(length)
                if len(data) != length:
                    if strict:
                        raise ValueError("Truncated capture data")
                    warn("Incomplete evidence ends with a partial data chunk")
                previous_offset = offset + len(data)
                total_bytes += len(data)
                if data:
                    yield CapturedChunk(utc_ns, mono_ns, offset, connection, data, length)
                if len(data) != length:
                    break
            if not halted and raw.read(1):
                if strict:
                    raise ValueError("Unindexed capture bytes")
                warn("Unindexed tail retained in raw file, not replayed")
        if halted:
            break
    if total_bytes != manifest["bytes_written"]:
        if strict:
            raise ValueError("Capture byte count mismatch")
        warn("Incomplete evidence has a stale byte count")


def iter_capture(directory: Path, *, allow_incomplete: bool = False) -> Iterator[CapturedChunk]:
    """Validate and stream a stopped capture. No packet type is filtered."""
    return _iter_capture(directory, allow_incomplete, _warn)


def list_captures(root: Path, limit: int = 20) -> list[dict[str, Any]]:
    root = Path(root)
    _check_root(root)
    if not root.exists():
        return []
    result = []
    for path in sorted(root.iterdir(), reverse=True):
        if path.is_symlink() or not path.is_dir() or not CAPTURE_ID_PATTERN.fullmatch(path.name):
            continue
        try:
            manifest = read_manifest(path, allow_incomplete=True)
            result.append(
                {
                    key: manifest.get(key)
                    for key in (
                        "capture_id",
                        "state",
                        "stop_reason",
                        "complete",
                        "bytes_written",
                        "dropped_bytes",
                        "dropped_records",
                        "started_utc_ns",
                        "ended_utc_ns",
                    )
                }
            )
        except (OSError, ValueError, RecursionError):
            result.append({"capture_id": path.name, "state": "invalid_manifest", "complete": False})
        if len(result) >= limit:
            break
    return result


# Local/central headers, zip64 extras and descriptors, plus the name twice.
_ZIP_ENTRY_OVERHEAD = 256
_ZIP_END_OVERHEAD = 1024
_COPY_BYTES = 1024 * 1024


def _zip_entry_bytes(arcname: str, size: int) -> int:
    return size + _ZIP_ENTRY_OVERHEAD + 2 * len(arcname.encode())


def _open_input(directory_fd: int | None, path: Path) -> tuple[BinaryIO, os.stat_result]:
    """Open an archive input without following a link swapped in after validation."""
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    try:
        if directory_fd is None:
            descriptor = os.open(path, flags)
        else:
            descriptor = os.open(path.name, flags, dir_fd=directory_fd)
    except OSError as err:
        if err.errno == errno.ELOOP:
            raise CaptureError("capture_unsafe", "Unsafe archive input") from None
        raise
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode):
            raise CaptureError("capture_unsafe", "Unsafe archive input")
        return os.fdopen(descriptor, "rb"), info
    except BaseException:
        os.close(descriptor)
        raise


def _zip_info(arcname: str, info: os.stat_result) -> zipfile.ZipInfo:
    entry = zipfile.ZipInfo(arcname, max(time.localtime(info.st_mtime)[:6], (1980, 1, 1, 0, 0, 0)))
    entry.external_attr = (info.st_mode & 0xFFFF) << 16
    entry.file_size = info.st_size  # Selects zip64 for large inputs up front.
    entry.compress_type = zipfile.ZIP_DEFLATED
    entry.compress_level = 1
    return entry


def export_archive(
    directory: Path, *, allow_interrupted: bool = False, max_storage_bytes: int | None = None
) -> Path:
    """Archive stopped evidence, including incomplete captures with clear flags.

    With max_storage_bytes, the archive must fit in the budget left by
    everything else under the capture root (the capture itself is excluded,
    so a capture that filled its own budget can still be exported). It must
    always fit in free disk space. Both limits are also enforced while writing.
    """
    directory = Path(directory)
    root = directory.parent
    _check_root(root)
    if directory.is_symlink():
        raise CaptureError("capture_unsafe", "Capture directory must not be a symlink")
    manifest = read_manifest(directory, allow_incomplete=True)
    if manifest["state"] not in ("stopped", "error") and not allow_interrupted:
        raise CaptureError("stop_before_export", "Stop capture before exporting")
    # The HA controller rejects a live writer. A stale 'capturing' manifest
    # after a crash is incomplete evidence and must still be recoverable.
    # Validate every byte; incomplete evidence is explicitly marked/warned and
    # preserved verbatim, never "repaired" or declared complete.
    issues: list[str] = []
    for _ in _iter_capture(directory, True, issues.append):
        pass
    issues = list(dict.fromkeys(issues))
    for message in issues:
        warnings.warn(message, RuntimeWarning, stacklevel=2)
    paths = [directory / "manifest.json"]
    unverified = []
    if manifest["format_version"] >= 2:
        paths.append(_events_path(directory))
    elif (directory / EVENTS_FILE).is_symlink() or (directory / EVENTS_FILE).exists():
        paths.append(_events_path(directory))
        unverified.append(EVENTS_FILE)  # Format 1 recorded no event-log checksum.
    for segment in manifest["segments"]:
        paths.extend(_segment_paths(directory, segment))
    if not manifest["complete"]:
        listed = {path.name for path in paths}
        for path in sorted(directory.iterdir()):
            if _UNLISTED_PATTERN.fullmatch(path.name) and path.name not in listed:
                if path.is_symlink() or not path.is_file():
                    raise CaptureError("capture_unsafe", "Unsafe unlisted segment")
                paths.append(path)
                unverified.append(path.name)
    if any(path.is_symlink() or not path.is_file() for path in paths):
        raise CaptureError("capture_unsafe", "Unsafe archive input")
    recovery = None
    if not manifest["complete"] or issues or unverified:
        recovery = json.dumps(
            {
                "complete": manifest["complete"],
                "verified": not issues and not unverified,
                "format_version": manifest["format_version"],
                "validation_warnings": issues,
                "unverified_files": unverified,
                "note": "Evidence archived verbatim; files with validation warnings, unlisted crash "
                "segments and format 1 event logs are unverified, and unlisted segments are not "
                "timestamp-replayed",
            }
        ).encode()
    recovery_name = f"{directory.name}/recovery.json"
    expected = _ZIP_END_OVERHEAD + sum(
        _zip_entry_bytes(f"{directory.name}/{path.name}", path.stat().st_size) for path in paths
    )
    if recovery is not None:
        expected += _zip_entry_bytes(recovery_name, len(recovery))
    limits: list[tuple[int, type[CaptureError], str, str]] = []
    if max_storage_bytes is not None:
        others = _tree_size(root) - _tree_size(directory)
        limits.append(
            (
                max_storage_bytes - others,
                CaptureError,
                "export_budget_exceeded",
                "Export would exceed the capture storage budget",
            )
        )
    limits.append(
        (
            shutil.disk_usage(root).free - FREE_SPACE_RESERVE,
            CaptureRuntimeError,
            "export_insufficient_space",
            "Insufficient free disk space to export",
        )
    )
    available, error_type, key, message = min(limits, key=lambda limit: limit[0])

    def limit_error(required: int) -> CaptureError:
        return error_type(key, message, required_mib=_mib(required), available_mib=_mib(available))

    if expected > available:
        raise limit_error(expected)
    output = root / f"{directory.name}-{uuid.uuid4().hex[:8]}.zip"
    directory_fd = None
    try:
        if os.open in os.supports_dir_fd:
            directory_fd = os.open(
                directory, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
            )
        with (
            _private_file(output) as handle,
            zipfile.ZipFile(handle, "w", zipfile.ZIP_DEFLATED, compresslevel=1) as archive,
        ):
            for path in paths:
                arcname = f"{directory.name}/{path.name}"
                source, info = _open_input(directory_fd, path)
                with source, archive.open(_zip_info(arcname, info), "w") as target:
                    while block := source.read(_COPY_BYTES):
                        target.write(block)
                        # Incompressible data can still outgrow the estimate.
                        if handle.tell() > available:
                            raise limit_error(handle.tell())
            if recovery is not None:
                archive.writestr(recovery_name, recovery)
        if (size := output.stat().st_size) > available:
            raise limit_error(size)
    except BaseException:
        output.unlink(missing_ok=True)
        raise
    finally:
        if directory_fd is not None:
            os.close(directory_fd)
    return output


def _capture_archives(root: Path, capture_id: str) -> list[Path]:
    return [
        path
        for path in root.iterdir()
        if (match := _ARCHIVE_PATTERN.fullmatch(path.name)) and match["capture_id"] == capture_id
    ]


def delete_capture(root: Path, capture_id: str) -> dict[str, Any]:
    """Delete one capture and its exports; the caller must exclude the active session.

    Archives left behind after their capture directory disappeared are deleted too.
    """
    root = Path(root)
    _check_root(root)
    _check_capture_id(capture_id)
    candidate = root / capture_id
    directory: Path | None = None
    entries: list[Path] = []
    if candidate.is_symlink() or candidate.exists():
        directory = capture_directory(root, capture_id)
        entries = list(directory.iterdir())
        archives = _capture_archives(root, capture_id)
    else:
        archives = _capture_archives(root, capture_id) if root.is_dir() else []
        if not archives:
            raise CaptureError("capture_not_found", "Capture directory is unavailable", capture_id=capture_id)
    # Validate everything first so a refusal never leaves a half-deleted capture.
    if any(path.is_symlink() or not path.is_file() for path in (*entries, *archives)):
        raise CaptureError("capture_unsafe", "Capture contains unexpected entries")
    deleted = 0
    for path in (*archives, *entries):
        deleted += path.lstat().st_size
        path.unlink()
    if directory is not None:
        directory.rmdir()
    _fsync_dir(root)
    return {"capture_id": capture_id, "deleted_archives": len(archives), "deleted_bytes": deleted}


def remove_capture_root(root: Path) -> bool:
    """Delete an instance's entire capture root without following a symlinked root."""
    root = Path(root)
    if root.is_symlink():
        raise CaptureError("capture_unsafe", "Capture root must not be a symlink")
    if not root.exists():
        return False
    if not root.is_dir():
        raise CaptureError("capture_unsafe", "Capture root is not a directory")
    shutil.rmtree(root)
    return True
