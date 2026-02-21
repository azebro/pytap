# pytap

Simplified TapTap protocol parser for Tigo TAP solar monitoring systems.

Passively monitors the RS-485 bus between a Tigo Cloud Connect Advanced (CCA) gateway and its TAP controller, parsing frames into structured events (power reports, infrastructure discovery, topology, diagnostics).

## Requirements

- Python 3.10+
- No dependencies for core library usage

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
| `PowerReportEvent` | `power_report` | Solar optimizer voltage, current, power, temperature |
| `InfrastructureEvent` | `infrastructure` | Gateway/node discovery (addresses, versions, barcodes) |
| `TopologyEvent` | `topology` | Mesh network topology report |
| `StringEvent` | `string` | Diagnostic string request/response |

## Running Tests

```bash
pip install -e ".[dev]"
pytest pytap/tests/ -v
```

## Project Structure

```
pytap/
├── __init__.py        # Public API re-exports
├── api.py             # Top-level functions: create_parser, parse_bytes, connect
├── core/
│   ├── barcode.py     # Tigo barcode encode/decode
│   ├── crc.py         # CRC-16-CCITT calculation
│   ├── events.py      # Event dataclasses (PowerReportEvent, etc.)
│   ├── parser.py      # Core protocol parser (frame → event pipeline)
│   ├── source.py      # TcpSource, SerialSource (byte providers)
│   ├── state.py       # SlotClock, NodeTableBuilder, PersistentState
│   └── types.py       # Protocol types (GatewayID, Frame, PowerReport, etc.)
└── tests/
    ├── test_api.py
    ├── test_barcode.py
    ├── test_crc.py
    ├── test_parser.py
    └── test_types.py
```
