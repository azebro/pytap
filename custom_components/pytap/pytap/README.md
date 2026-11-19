# pytap

Simplified TapTap protocol parser for Tigo TAP solar monitoring systems.

Passively monitors the RS-485 bus between a Tigo Cloud Connect Advanced (CCA) gateway and its TAP controller, parsing frames into structured events (power reports, infrastructure discovery, topology, diagnostics).

## Requirements

- Python 3.10+ for the parser (3.11+ for `core.capture` / `core.replay`)
- No dependencies for core library usage (`pyserial` only for `SerialSource`)

## Library Usage

### Parse a Captured Byte Buffer

```python
import pytap

events = pytap.parse_bytes(raw_bytes)
for event in events:
    print(event.event_type, event.to_dict())
```

### Streaming with a Parser

```python
import pytap

parser = pytap.create_parser()

source = pytap.connect({"tcp": "192.168.1.100", "port": 502})
try:
    while True:
        data = source.read(1024)
        if data:
            for event in parser.feed(data):
                print(event.to_dict())
finally:
    source.close()
```

### Streaming with Persistent State

The parser accepts an optional `PersistentState` object to preserve infrastructure
state (gateway identities, versions, node tables) across sessions. The caller owns
persistence — the parser only mutates the state in memory.

```python
import pytap
from pytap import PersistentState

# Start with empty state (or restore from your storage)
state = PersistentState()

parser = pytap.create_parser(persistent_state=state)

# ... feed data, process events ...

# Save state using to_dict() / from_dict() for JSON serialization
saved = state.to_dict()

# Later, restore:
restored_state = PersistentState.from_dict(saved)
parser = pytap.create_parser(persistent_state=restored_state)
```

### Replaying Recorded Data

`create_parser()` accepts an optional keyword-only `clock` (a callable returning
`datetime`) used instead of wall time, e.g. to replay recorded traffic with its
original timestamps. `pytap.core.replay.replay_events()` does this for captures
written by `pytap.core.capture.CaptureWriter`:

```python
from pytap.core.capture import iter_capture
from pytap.core.replay import replay_events

for item in replay_events(iter_capture(session_dir)):
    print(item["observed_utc_ns"], item["event"])
```

### Barcode Utilities

```python
from pytap import encode_barcode, decode_barcode

barcode = encode_barcode(bytes([0x04, 0xC0, 0x5B, 0x30, 0x00, 0x02, 0xBE, 0x16]))
print(barcode)  # e.g. "3-2BE16Y"

address = decode_barcode(barcode)
print(address.hex(':'))  # "04:c0:5b:30:00:02:be:16"
```

## Event Types

| Type | `event_type` | Description |
|------|-------------|-------------|
| `PowerReportEvent` | `power_report` | Solar optimizer voltage, current, power, temperature, duty cycle, RSSI, and undocumented status bits (`high_output_mode`, `status_code`, `low_voltage_flag`) |
| `InfrastructureEvent` | `infrastructure` | Gateway/node discovery (addresses, versions, barcodes) |
| `TopologyEvent` | `topology` | Mesh network topology report |
| `StringEvent` | `string` | Diagnostic string request/response |

## Running Tests

From the repository root:

```bash
PYTHONPATH=custom_components/pytap python3 -c 'import pytap, pytest; raise SystemExit(pytest.main(["custom_components/pytap/pytap/tests/", "--import-mode=importlib", "-vv"]))'
```

## Project Structure

```
pytap/
├── __init__.py        # Public API re-exports
├── api.py             # Top-level functions: create_parser, parse_bytes, connect
├── core/
│   ├── barcode.py     # Tigo barcode encode/decode
│   ├── capture.py     # Raw byte recording, validation, export, deletion
│   ├── crc.py         # CRC-16-CCITT calculation
│   ├── events.py      # Event dataclasses (PowerReportEvent, etc.)
│   ├── parser.py      # Core protocol parser (frame → event pipeline)
│   ├── replay.py      # Offline frame inspection and timestamped replay
│   ├── source.py      # TcpSource, SerialSource (byte providers)
│   ├── state.py       # SlotClock, NodeTableBuilder, PersistentState
│   └── types.py       # Protocol types (GatewayID, Frame, PowerReport, etc.)
├── docs/              # Architecture, API reference, implementation plan
└── tests/
    ├── test_api.py
    ├── test_barcode.py
    ├── test_crc.py
    ├── test_parser.py
    └── test_types.py
```
