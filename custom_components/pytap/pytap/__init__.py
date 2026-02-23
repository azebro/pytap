"""pytap — Tigo TAP protocol parser for solar monitoring systems.

Public surface re-exports from core modules and the api module.
"""

__version__ = "0.1.0"

# Core types
# API functions
from .api import (
    connect,
    create_parser,
    parse_bytes,
)

# Barcode utilities
from .core.barcode import barcode_from_address, decode_barcode, encode_barcode

# CRC
from .core.crc import crc

# Events
from .core.events import (
    Event,
    InfrastructureEvent,
    PowerReportEvent,
    StringEvent,
    TopologyEvent,
)

# Parser
from .core.parser import Parser

# State management
from .core.state import (
    NodeTableBuilder,
    PersistentState,
    SlotClock,
)
from .core.types import (
    RSSI,
    Address,
    Frame,
    FrameType,
    GatewayID,
    GatewayInfo,
    LongAddress,
    NodeAddress,
    NodeID,
    NodeInfo,
    PacketType,
    PowerReport,
    ReceivedPacketHeader,
    SlotCounter,
    U12Pair,
    iter_received_packets,
)

__all__ = [
    "__version__",
    # Types
    "GatewayID",
    "Address",
    "FrameType",
    "Frame",
    "NodeID",
    "NodeAddress",
    "LongAddress",
    "RSSI",
    "SlotCounter",
    "PacketType",
    "ReceivedPacketHeader",
    "U12Pair",
    "PowerReport",
    "GatewayInfo",
    "NodeInfo",
    "iter_received_packets",
    # Events
    "Event",
    "PowerReportEvent",
    "InfrastructureEvent",
    "TopologyEvent",
    "StringEvent",
    # State
    "SlotClock",
    "NodeTableBuilder",
    "PersistentState",
    # Parser
    "Parser",
    # Barcode
    "encode_barcode",
    "decode_barcode",
    "barcode_from_address",
    # CRC
    "crc",
    # API
    "create_parser",
    "parse_bytes",
    "connect",
]
