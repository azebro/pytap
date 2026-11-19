# pytap — Simplified TapTap Python Port

## Purpose

`pytap` is a simplified, flat-architecture Python implementation of the TapTap protocol for passive monitoring of Tigo TAP solar energy systems. Unlike the layered `python/taptap` port (which mirrors the Rust crate's sink-chain design), `pytap` collapses the entire protocol stack into a single-pass pipeline that converts raw bytes directly into final parsed events.

The primary design goal is **ease of integration**: the full parsing functionality lives in a library module that can be called programmatically. The parser has no file I/O — persistence is handled externally by the caller (e.g., the HA coordinator). The separate, optional `core.capture` module records raw bytes to disk, and `core.replay` inspects and replays those recordings offline.

## Comparison with `python/taptap`

| Aspect | `python/taptap` | `pytap` |
|--------|-----------------|---------|
| Architecture | Layered sink-chain (5 layers, 3 Sink protocols) | Flat pipeline, single `Parser` class |
| Module count | ~25 files across 8 packages | ~10 files in 2 packages |
| Integration | Requires wiring 4 receivers + observer | Single `pytap.create_parser()` call |
| Extensibility | Add new Sink implementations | Subclass or compose `Parser` |
| Output | JSON to stdout only | Returns Python objects; caller serializes |

## Architecture Overview

```
┌─────────────────────────────────────────────────────┐
│  Public API  (pytap.api)                            │
│                                                     │
│  connect(source_config) → TcpSource | SerialSource  │
│  parse_bytes(data) → list[Event]                    │
│  create_parser(persistent_state, *, clock) → Parser │
└──────────────────────┬──────────────────────────────┘
                       │ uses
                       ▼
┌─────────────────────────────────────────────────────┐
│  Core  (pytap.core)                                 │
│                                                     │
│  ┌───────────────────────────────────────────┐      │
│  │  Parser                                   │      │
│  │  Single class implementing the full       │      │
│  │  byte → event pipeline:                   │      │
│  │                                           │      │
│  │  raw bytes                                │      │
│  │    → frame detection (preamble/terminator)│      │
│  │    → unescaping                           │      │
│  │    → CRC validation                       │      │
│  │    → frame type dispatch                  │      │
│  │    → transport interpretation             │      │
│  │    → PV packet extraction                 │      │
│  │    → application-level parsing            │      │
│  │    → Event objects                        │      │
│  └───────────────────────────────────────────┘      │
│                                                     │
│  ┌───────────────────────────────────────────┐      │
│  │  Types & Protocol Constants               │      │
│  │  Frame, GatewayID, NodeID, LongAddress,   │      │
│  │  SlotCounter, PacketType, PowerReport,    │      │
│  │  etc.                                     │      │
│  └───────────────────────────────────────────┘      │
│                                                     │
│  ┌───────────────────────────────────────────┐      │
│  │  Events                                   │      │
│  │  PowerReportEvent, InfrastructureEvent,   │      │
│  │  TopologyEvent, StringEvent               │      │
│  └───────────────────────────────────────────┘      │
│                                                     │
│  ┌───────────────────────────────────────────┐      │
│  │  Source                                   │      │
│  │  SerialSource, TcpSource                  │      │
│  │  (byte providers, no protocol knowledge)  │      │
│  └───────────────────────────────────────────┘      │
│                                                     │
│  ┌───────────────────────────────────────────┐      │
│  │  State                                    │      │
│  │  SlotClock, NodeTableBuilder (internal),  │      │
│  │  PersistentState (caller-owned)           │      │
│  └───────────────────────────────────────────┘      │
│                                                     │
│  ┌───────────────────────────────────────────┐      │
│  │  Capture & Replay (optional, offline)     │      │
│  │  CaptureWriter, iter_capture,             │      │
│  │  export_archive, FrameScanner,            │      │
│  │  replay_events                            │      │
│  └───────────────────────────────────────────┘      │
└─────────────────────────────────────────────────────┘
```

## Module Layout

```
pytap/
├── __init__.py              # Package root, re-exports api, events, types, state
├── api.py                   # Public API functions
├── core/
│   ├── __init__.py          # Package docstring only
│   ├── types.py             # All protocol types (flat, no layering)
│   ├── events.py            # Event dataclasses
│   ├── parser.py            # The single Parser class
│   ├── source.py            # SerialSource, TcpSource
│   ├── state.py             # SlotClock, NodeTableBuilder, PersistentState
│   ├── capture.py           # Raw byte recording, validation, export, deletion
│   ├── replay.py            # Offline frame inspection and timestamped replay
│   ├── crc.py               # CRC-16-CCITT calculation
│   └── barcode.py           # Tigo barcode encoding/decoding
├── docs/
│   ├── architecture.md      # This file
│   ├── api_reference.md     # Public API documentation
│   └── implementation_plan.md  # Original phased build plan
└── tests/
    ├── __init__.py
    ├── test_parser.py        # End-to-end parser tests
    ├── test_types.py         # Type construction/validation tests
    ├── test_crc.py           # CRC calculation tests
    ├── test_barcode.py       # Barcode encode/decode tests
    └── test_api.py           # Public API tests
```

## Data Flow

The entire protocol stack is collapsed into `Parser.feed(data: bytes) -> list[Event]`:

```
                    Parser.feed(bytes)
                          │
              ┌───────────┴────────────┐
              │   Frame Accumulator    │
              │   (state machine)      │
              │                        │
              │   States:              │
              │   IDLE → FRAME →       │
              │   complete frame bytes │
              └───────────┬────────────┘
                          │ raw frame bytes
              ┌───────────┴────────────┐
              │   Frame Decoder        │
              │                        │
              │   1. Unescape bytes    │
              │   2. Validate CRC      │
              │   3. Parse address,    │
              │      frame_type,       │
              │      payload           │
              └───────────┬────────────┘
                          │ Frame(gateway_id, type, payload)
              ┌───────────┴────────────┐
              │   Frame Dispatcher     │
              │                        │
              │   Routes by frame_type │
              │   to handler methods:  │
              │                        │
              │   RECEIVE_REQUEST/     │
              │   RECEIVE_RESPONSE     │
              │     → _handle_receive  │
              │                        │
              │   COMMAND_REQUEST/     │
              │   COMMAND_RESPONSE     │
              │     → _handle_command  │
              │                        │
              │   ENUMERATION_* /      │
              │   IDENTIFY / VERSION   │
              │     → infrastructure   │
              │                        │
              │   PING / unknown       │
              │     → ignored          │
              └───────────┬────────────┘
                          │ extracted packet data
              ┌───────────┴────────────┐
              │   Packet Parser        │
              │                        │
              │   By PacketType:       │
              │   POWER_REPORT →       │
              │     PowerReportEvent   │
              │   NODE_TABLE_RESPONSE →│
              │     InfrastructureEvent│
              │   TOPOLOGY_REPORT →    │
              │     TopologyEvent      │
              │   STRING_* →           │
              │     StringEvent        │
              └───────────┬────────────┘
                          │
                     list[Event]
```

## Key Design Decisions

### 1. Single `Parser` Class

Instead of 4 separate Receiver classes connected by Sink protocols, `pytap` uses one `Parser` that owns the full state machine. This eliminates:
- 3 abstract Sink protocol definitions
- forwarding boilerplate (the existing `ApplicationReceiver` forwards 8 transport methods untouched)
- the need to wire receivers together manually

Internal methods are organized by concern (`_accumulate`, `_decode_frame`, `_dispatch_frame`, `_parse_pv_packet`) but are private — callers only see `feed()`.

### 2. Events as Return Values, Not Side Effects

The existing port calls `print(json.dumps(event))` from deep inside the Observer. `pytap` instead **returns** event objects from `Parser.feed()`, giving the caller full control over output format, filtering, buffering, and routing.

This is the key enabler for non-CLI usage (library calls, MQTT bridges, databases, web UIs).

### 3. Flat Type Module

All protocol types (`GatewayID`, `NodeID`, `LongAddress`, `SlotCounter`, `PacketType`, `Frame`, `PowerReport`, etc.) live in a single `types.py` instead of being scattered across `gateway.link.address`, `pv.network.types`, `pv.link.slot_counter`, `pv.application.types`, etc.

This means users import `from pytap.core.types import GatewayID` rather than needing to know which protocol layer a type belongs to.

### 4. Caller-Owned Persistence

The parser accepts an optional `PersistentState` object and mutates it in memory as infrastructure events are detected. The caller (e.g., the HA coordinator) owns serialization and storage. This keeps the library free of file I/O dependencies and allows any storage backend:

```python
from pytap.api import create_parser
from pytap import PersistentState

# Restore state from your storage
state = PersistentState.from_dict(saved_data)
parser = create_parser(persistent_state=state)

# ... use parser ...

# Save state
data_to_save = state.to_dict()
```

### 5. State Management Inside Parser

`SlotClock`, `NodeTableBuilder`, and enumeration state are internal to `Parser`. The optional `PersistentState` object (gateway identities, versions, node tables) is passed at construction time and mutated in-place. The caller doesn't need to manage or understand the internal state machine.

### 6. Node Address Bit-15 Masking

Node addresses in `NODE_TABLE_RESPONSE` entries are 16-bit values where bit 15 is a protocol flag (indicating router/repeater status), not part of the node ID. The parser masks all node table addresses to 15 bits (`& 0x7FFF`) before storing them, so that table keys match the node IDs used in power reports. Without this masking, flagged nodes would appear with IDs like 32793 (`0x8019`) instead of the correct 25 (`0x0019`).

### 7. Invalid Slot Counters

Slot counters outside 0–11999 are counted in `counters["invalid_slots"]`. An invalid counter in a receive-response header never updates the slot clock (in earlier versions it raised `IndexError` out of `feed()`). It also suggests the variable-length header was mis-decoded, so the frame's packets are kept only if a strict walk consumes the payload exactly and every packet is well-formed (`invalid_slot_frames_kept`); otherwise the whole frame is dropped (`invalid_slot_frames_dropped`). Kept power reports still need an existing slot clock for their gateway, like any report; without one they are dropped (`no_slot_clock_drops`, warned once per gateway per parser). A power report whose own counter is invalid has no valid timestamp and is dropped.

### 8. Injectable Clock

`Parser(clock=...)` supplies "now" for slot-clock synchronisation and non-power-report event timestamps. Live use defaults to `datetime.now()`; `core.replay.replay_events()` passes each recorded chunk's UTC observation time so replayed events carry the original times.

### 9. Capture and Replay Outside the Parser

`core.capture` records raw reads before they reach the parser, so unsupported frames, bad CRCs and noise are preserved. Recording uses a bounded queue drained by a writer thread (`record()` never blocks the reader), private files, SHA-256 checksums in a manifest written only after the data is flushed, and explicit loss/stop reasons. `core.replay.FrameScanner` mirrors the parser's framing to report frames the live parser would drop (`live_parser_would_drop`, `giant`); when the parser's starting state is unknown (a capture started mid-connection, or lost bytes), the verdict is `None` until all possible states converge. Neither module changes `Parser` behaviour.

## Public API Surface

### `pytap.api`

```python
def create_parser(
    persistent_state: PersistentState | None = None,
    *,
    clock: Callable[[], datetime] | None = None,
) -> Parser:
    """Create a new protocol parser.

    Args:
        persistent_state: Optional PersistentState object for infrastructure
            state. The parser mutates it in memory; caller owns persistence.
        clock: Optional source of "now" (e.g. recorded times during replay).

    Returns:
        A Parser instance ready to receive bytes via feed().
    """

def parse_bytes(data: bytes) -> list[Event]:
    """One-shot parse: create a parser, feed bytes, return events.

    Convenience for non-streaming use (e.g., parsing captured files).
    """

def connect(source_config: dict) -> TcpSource | SerialSource:
    """Create and open a byte source ({"tcp": host, "port": 502} or {"serial": path}).

    Returns a source with read()/close() for manual feeding into a Parser.
    """
```

### `pytap.core.Parser`

```python
class Parser:
    def __init__(
        self,
        persistent_state: PersistentState | None = None,
        *,
        clock: Callable[[], datetime] | None = None,
    ):
        """Initialize parser with optional persistent infrastructure state and clock."""

    def feed(self, data: bytes) -> list[Event]:
        """Feed raw bytes and return any events parsed from them.

        Can be called incrementally — the parser maintains internal state
        across calls for partial frames.
        """

    def reset(self) -> None:
        """Reset frame accumulation (not infrastructure or slot clocks)."""

    @property
    def infrastructure(self) -> dict:
        """Current known infrastructure: gateways, nodes, barcodes."""

    @property
    def counters(self) -> dict:
        """frames_received, crc_errors, runts, giants, noise_bytes, invalid_slots,
        invalid_slot_frames_kept, invalid_slot_frames_dropped, no_slot_clock_drops."""
```

### `pytap.core.events`

```python
@dataclass
class Event:
    """Base event — all events have a type and timestamp."""
    event_type: str
    timestamp: datetime

@dataclass
class PowerReportEvent(Event):
    """Solar optimizer power measurement."""
    gateway_id: int
    node_id: int
    barcode: str | None
    voltage_in: float       # Volts
    voltage_out: float      # Volts
    current_in: float       # Amps
    power: float            # Watts (current_out * voltage_out)
    current_out: float      # Amps ((voltage_in * current_in) / voltage_out)
    temperature: float      # °C
    dc_dc_duty_cycle: float # 0.0–1.0
    rssi: int               # Raw signal strength byte

@dataclass
class InfrastructureEvent(Event):
    """Infrastructure state change (gateway/node discovery)."""
    gateways: dict  # gateway_id → {"address", "version"}
    nodes: dict     # node_id → {"address", "barcode", "gateway_id"}

@dataclass
class TopologyEvent(Event):
    """Mesh network topology report from a node."""
    gateway_id: int
    node_id: int
    data: bytes

@dataclass
class StringEvent(Event):
    """String request/response (diagnostic commands)."""
    gateway_id: int
    node_id: int
    direction: str  # "request" | "response"
    content: str
```

## Implementation Plan

The phased plan used to build the library is kept in [implementation_plan.md](implementation_plan.md). The command-line interface planned there was later removed; the library is used through the Home Assistant integration and the repository's `scripts/replay_capture.py`.

## Testing Strategy

Tests reuse captured protocol data from the Rust implementation (`src/test_data.rs`) to validate byte-level correctness:

1. **Unit**: CRC values, barcode encoding, type construction
2. **Parser**: Feed known byte sequences, assert expected events (including invalid slot counters)
3. **Integration**: Full parse pipeline with recorded data, assert correct events
4. **Regression**: Edge cases from `python/taptap` test suite

Capture and replay are tested by the integration's test suite (`tests/test_packet_capture.py`, `tests/test_capture_integrity.py`, `tests/test_capture_scanner.py`, `tests/test_review2_capture.py`).

## Dependencies

| Package | Purpose | Required |
|---------|---------|----------|
| `pyserial` | Serial port access | Optional (`SerialSource` only) |

Zero required dependencies — the parser, capture and replay modules use only the Python standard library (capture and replay need Python 3.11+).
