# pytap Public API Reference

## Package: `pytap`

### Top-Level Imports

```python
from pytap import create_parser, parse_bytes, connect
from pytap import Parser, Event, PowerReportEvent, InfrastructureEvent
from pytap import PersistentState
```

All public symbols are re-exported from `pytap.__init__`.

---

## Functions

### `create_parser`

```python
pytap.create_parser(persistent_state: Optional[PersistentState] = None) -> Parser
```

Create a new protocol parser instance.

**Parameters:**
- `persistent_state` — Optional `PersistentState` object. If provided, the parser uses this for infrastructure state (gateway identities, versions, node tables). The parser mutates the state in memory; the caller owns persistence (e.g., via `to_dict()` / `from_dict()`).

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
pytap.connect(source_config: dict) -> Source
```

Open a byte source for manual use with a `Parser`.

**Parameters:**
- `source_config` — Same format as `observe()`.

**Returns:** A `Source` object with a `read(size: int) -> bytes` method.

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
class pytap.Parser(persistent_state: Optional[PersistentState] = None)
```

The core protocol parser. Maintains internal state for frame assembly, transport correlation, slot clock synchronization, and infrastructure tracking across incremental `feed()` calls.

The parser accepts an optional `PersistentState` object which it mutates in memory as infrastructure events are detected. The caller owns persistence (serialization/deserialization).

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
        1: {"address": "04:C0:5B:...", "barcode": "S-1234567A"},
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
}
```

---

## Event Types

All events are `dataclass` instances with a `to_dict() -> dict` method for JSON serialization.

### `Event` (base)

```python
@dataclass
class Event:
    event_type: str       # Discriminator: "power_report", "infrastructure", etc.
    timestamp: datetime   # When the event was generated
```

### `PowerReportEvent`

```python
@dataclass
class PowerReportEvent(Event):
    gateway_id: int          # Gateway link-layer ID (0–32767)
    node_id: int             # PV node ID (1–65535)
    barcode: str | None      # Tigo barcode e.g. "S-1234567A", if known
    voltage_in: float        # Input voltage (V), panel side
    voltage_out: float       # Output voltage (V), string side
    current: float           # Current (A)
    power: float             # Computed: voltage_out × current (W)
    temperature: float       # Module temperature (°C)
    dc_dc_duty_cycle: float  # DC-DC converter duty cycle (0.0–1.0)
    rssi: int                # Received signal strength indicator
```

**JSON example:**
```json
{
    "event_type": "power_report",
    "timestamp": "2026-02-16T12:34:56.789+01:00",
    "gateway_id": 1,
    "node_id": 42,
    "barcode": "S-1234567A",
    "voltage_in": 38.5,
    "voltage_out": 39.2,
    "current": 8.75,
    "power": 343.0,
    "temperature": 45.2,
    "dc_dc_duty_cycle": 0.78,
    "rssi": -65
}
```

### `InfrastructureEvent`

```python
@dataclass
class InfrastructureEvent(Event):
    gateways: dict[int, GatewayInfo]  # gateway_id → info
    nodes: dict[int, NodeInfo]        # node_id → info
```

Emitted when infrastructure state changes (gateway enumeration, node table updates).

### `TopologyEvent`

```python
@dataclass
class TopologyEvent(Event):
    gateway_id: int
    node_id: int
    parent_address: int
    neighbors: list[int]
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
| `NodeID` | PV network node identifier | 16-bit int (1–65535) |
| `NodeAddress` | PV network address (0=broadcast); bit 15 is a protocol flag masked to 15 bits in node table parsing | 16-bit int |
| `LongAddress` | IEEE 802.15.4 hardware address | 8 bytes |
| `SlotCounter` | Time synchronization counter | 16-bit (2-bit epoch + 14-bit slot) |
| `PacketType` | PV application packet type | `IntEnum` |
| `Frame` | Decoded gateway link-layer frame | dataclass |
| `Barcode` | Tigo device barcode (`X-NNNNNNNC`) | string |
| `GatewayInfo` | Gateway address + version | dataclass |
| `NodeInfo` | Node address + barcode | dataclass |
