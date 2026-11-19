# pytap Public API Reference

## Package: `pytap`

### Top-Level Imports

```python
from pytap import create_parser, parse_bytes, connect
from pytap import Parser, Event, PowerReportEvent, InfrastructureEvent
from pytap import PersistentState
```

All public symbols are re-exported from `pytap.__init__`. Packet capture and offline replay live in `pytap.core.capture` and `pytap.core.replay` and are not re-exported.

---

## Functions

### `create_parser`

```python
pytap.create_parser(
    persistent_state: PersistentState | None = None,
    *,
    clock: Callable[[], datetime] | None = None,
) -> Parser
```

Create a new protocol parser instance.

**Parameters:**
- `persistent_state` — Optional `PersistentState` object. If provided, the parser uses this for infrastructure state (gateway identities, versions, node tables). The parser mutates the state in memory; the caller owns persistence (e.g., via `to_dict()` / `from_dict()`).
- `clock` — Optional source of "now" for event timestamps and slot-clock synchronisation, e.g. recorded observation times during offline replay. Defaults to `datetime.now()`.

**Returns:** A `Parser` ready to accept bytes via `feed()`.

**Example:**
```python
parser = pytap.create_parser()
events = parser.feed(some_bytes)
```

---

### `parse_bytes`

```python
pytap.parse_bytes(data: bytes) -> list[Event]
```

One-shot convenience: creates a parser, feeds the entire byte buffer, and returns all parsed events.

Suitable for batch processing of captured data. For streaming use, prefer `create_parser()` + `feed()`.

**Parameters:**
- `data` — Raw bytes from the RS-485 bus or a capture file.

**Returns:** List of all events found in `data`.

**Example:**
```python
with open("capture.bin", "rb") as f:
    events = pytap.parse_bytes(f.read())
for e in events:
    print(e)
```

---

### `connect`

```python
pytap.connect(source_config: dict) -> TcpSource | SerialSource
```

Open a byte source for manual use with a `Parser`.

**Parameters:**
- `source_config` — `{"tcp": "192.168.1.100", "port": 502}` (port defaults to 502) or `{"serial": "/dev/ttyUSB0"}`. Serial sources require `pyserial`. Raises `ValueError` if neither key is present.

**Returns:** A source with `read(size: int) -> bytes` (empty bytes on a read timeout) and `close()`. `TcpSource` also has `interrupt()`, which wakes a read blocked in another thread; the reading thread still owns `close()`.

**Example:**
```python
source = pytap.connect({"tcp": "192.168.1.100", "port": 502})
parser = pytap.create_parser()
while True:
    data = source.read(1024)
    for event in parser.feed(data):
        process(event)
```

---

## Classes

### `Parser`

```python
class pytap.Parser(
    persistent_state: PersistentState | None = None,
    *,
    clock: Callable[[], datetime] | None = None,
)
```

The core protocol parser. Maintains internal state for frame assembly, transport correlation, slot clock synchronization, and infrastructure tracking across incremental `feed()` calls.

The parser accepts an optional `PersistentState` object which it mutates in memory as infrastructure events are detected. The caller owns persistence (serialization/deserialization). `clock` works as in `create_parser()`.

#### Methods

##### `feed`

```python
parser.feed(data: bytes) -> list[Event]
```

Feed raw bytes into the parser. Returns a (possibly empty) list of events parsed from the accumulated data. Safe to call with any amount of data — the parser handles partial frames across calls.

##### `reset`

```python
parser.reset() -> None
```

Reset the parser's frame accumulation state. Does **not** clear infrastructure state or slot clocks.

#### Properties

##### `infrastructure`

```python
parser.infrastructure -> dict
```

Read-only view of the current infrastructure state:

```python
{
    "gateways": {
        1: {"address": "04:C0:5B:...", "version": "1.2.3"},
        ...
    },
    "nodes": {
        1: {"address": "04:C0:5B:...", "barcode": "S-1234567A", "gateway_id": 1},
        ...
    }
}
```

##### `counters`

```python
parser.counters -> dict
```

Parse statistics:

```python
{
    "frames_received": 1234,
    "crc_errors": 2,
    "runts": 0,
    "giants": 0,
    "noise_bytes": 15,
    "invalid_slots": 0,
    "invalid_slot_frames_kept": 0,
    "invalid_slot_frames_dropped": 0,
    "no_slot_clock_drops": 0,
}
```

`invalid_slots` counts slot counters outside the valid range (0–11999). A receive-response frame whose header slot counter is invalid never updates the slot clock; its packets are kept (`invalid_slot_frames_kept`) only if the packet walk consumes the payload exactly and every packet is well-formed (known packet type, node address 1–0x7FFF, valid power-report length), otherwise the whole frame is dropped (`invalid_slot_frames_dropped`). A power report with an invalid slot counter is dropped. `no_slot_clock_drops` counts power reports discarded because their gateway has no slot clock yet (including reports kept from such frames); the warning for this is logged once per gateway per parser, then at debug level.

---

## Event Types

All events are `dataclass` instances with a `to_dict() -> dict` method for JSON serialization (`timestamp` as ISO 8601).

### `Event` (base)

```python
@dataclass
class Event:
    event_type: str       # Discriminator: "power_report", "infrastructure", etc.
    timestamp: datetime   # From the slot clock (power reports) or the parser clock
```

### `PowerReportEvent`

```python
@dataclass
class PowerReportEvent(Event):
    gateway_id: int          # Gateway link-layer ID (0–32767)
    node_id: int             # PV node ID
    barcode: str | None      # Tigo barcode e.g. "S-1234567A", if known
    voltage_in: float        # Input voltage (V), panel side
    voltage_out: float       # Output voltage (V), string side
    current_in: float        # Input current (A)
    power: float             # current_out × voltage_out (W)
    current_out: float       # (voltage_in × current_in) / voltage_out (A); 0 if voltage_out is 0
    temperature: float       # Module temperature (°C)
    dc_dc_duty_cycle: float  # DC-DC converter duty cycle (0.0–1.0)
    rssi: int                # Raw received signal strength byte (0–255)
    high_output_mode: bool | None  # Byte 7 bit 7
    status_code: int | None  # Byte 9 raw value
    low_voltage_flag: bool | None  # Byte 9 bit 7
```

The last three fields come from undocumented report bytes; their meanings are correlations from captured data, not confirmed by Tigo. `status_code` is normally `0x64`, `0x68` at low current (around dawn/dusk) and `0xE4` at low input voltage; `high_output_mode` is set almost only above ~0.5 A input current. They default to `None` when an event is constructed without them; the parser always sets them.

**JSON example:**
```json
{
    "event_type": "power_report",
    "timestamp": "2026-02-16T12:34:56.789000",
    "gateway_id": 1,
    "node_id": 42,
    "barcode": "S-1234567A",
    "voltage_in": 40.0,
    "voltage_out": 40.0,
    "current_in": 8.0,
    "power": 320.0,
    "current_out": 8.0,
    "temperature": 45.2,
    "dc_dc_duty_cycle": 0.78,
    "rssi": 120,
    "high_output_mode": true,
    "status_code": 100,
    "low_voltage_flag": false
}
```

### `InfrastructureEvent`

```python
@dataclass
class InfrastructureEvent(Event):
    gateways: dict  # gateway_id → {"address": str | None, "version": str | None}
    nodes: dict     # node_id → {"address": str, "barcode": str | None, "gateway_id": int}
```

Emitted when infrastructure state changes (gateway enumeration/identity/version, completed node tables). Each event carries the complete current state.

### `TopologyEvent`

```python
@dataclass
class TopologyEvent(Event):
    gateway_id: int
    node_id: int
    data: bytes     # Raw topology report (hex string in to_dict())
```

Emitted when a PV optimizer reports its mesh network topology.

### `StringEvent`

```python
@dataclass
class StringEvent(Event):
    gateway_id: int
    node_id: int
    direction: str    # "request" or "response"
    content: str
```

Emitted for diagnostic string commands exchanged between controller and nodes.

---

## Protocol Types

These are available from `pytap.core.types` (and re-exported from `pytap`).

| Type | Description | Size |
|------|-------------|------|
| `GatewayID` | Gateway link-layer identifier | 15-bit int (0–32767) |
| `Address` | Link-layer address (gateway ID + direction bit) | 2 bytes |
| `NodeID` | PV network node identifier | 16-bit int |
| `NodeAddress` | PV network address (0=broadcast); bit 15 is a protocol flag masked to 15 bits in node table parsing | 16-bit int |
| `LongAddress` | IEEE 802.15.4 hardware address | 8 bytes |
| `RSSI` | Signal strength | 8-bit |
| `SlotCounter` | Time synchronization counter (slot numbers 0–11999 are valid) | 16-bit (2-bit epoch + 14-bit slot) |
| `PacketType` / `FrameType` | PV packet / gateway frame types | `IntEnum` |
| `Frame` | Decoded gateway link-layer frame | dataclass |
| `ReceivedPacketHeader` | Header of a packet inside a receive response | dataclass |
| `PowerReport` | Raw 13-byte power report with decoded properties (incl. `high_output_mode`, `status_code`, `low_voltage_flag`) | dataclass |
| `GatewayInfo` | Gateway address + version | dataclass |
| `NodeInfo` | Node address + barcode | dataclass |

Barcodes (`X-NNNNNNNC`) are plain strings; see `encode_barcode`, `decode_barcode` and `barcode_from_address`.

---

## Capture and Replay (`pytap.core.capture`, `pytap.core.replay`)

Standard library only (Python 3.11+); no Home Assistant dependency.

| Symbol                                                                                                                                  | Purpose                                                                                                                                                                                                                                                          |
| --------------------------------------------------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `CaptureWriter(root, metadata, *, duration_seconds, max_storage_bytes, segment_bytes, queue_records)`                                   | One recording session. `start()` (blocking), `record(data, connection_id, bootstrap)` (non-blocking, from the reader thread before parsing), `mark(event, connection_id, **details)`, `request_stop(reason)`, `join(timeout)`, `status(include_directory=False)` |
| `read_manifest(directory, *, allow_incomplete=False)`                                                                                   | Validate and load `manifest.json`                                                                                                                                                                                                                                |
| `iter_capture(directory, *, allow_incomplete=False)`                                                                                    | Verify checksums/sizes and stream `CapturedChunk(utc_ns, monotonic_ns, stream_offset, connection_id, data, declared_length)`                                                                                                                                     |
| `export_archive(directory, *, allow_interrupted=False, max_storage_bytes=None)`                                                         | Write a private ZIP next to the session; incomplete captures include `recovery.json`                                                                                                                                                                             |
| `list_captures(root, limit=20)`, `capture_directory(root, capture_id)`, `delete_capture(root, capture_id)`, `remove_capture_root(root)` | Session management                                                                                                                                                                                                                                               |
| `CaptureError`                                                                                                                          | User-actionable error with `translation_key` / `translation_placeholders`                                                                                                                                                                                        |
| `FrameScanner().feed(chunk)` / `.finish()`                                                                                              | Yield `frame` / `noise` / `incomplete` / `oversized_fragment` records; frames include `giant`, `live_parser_would_drop` and `live_parser_drop_reason`                                                                                                            |
| `decode_wire_frame(wire)`                                                                                                               | Decode one wire frame without discarding unknown types or bad CRCs                                                                                                                                                                                               |
| `replay_events(chunks, state=None)`                                                                                                     | Run a `Parser` with each chunk's recorded UTC time as `clock`; yields `{stream_offset, connection_id, observed_utc_ns, event}`                                                                                                                                   |

`scripts/replay_capture.py` in the repository wraps these as a command-line tool.
