"""pytap public API — protocol logic accessible via function calls."""

from __future__ import annotations

import logging
from typing import Optional

from .core.parser import Parser
from .core.events import Event
from .core.state import PersistentState
from .core.source import TcpSource, SerialSource

logger = logging.getLogger(__name__)


def create_parser(
    persistent_state: Optional[PersistentState] = None,
) -> Parser:
    """Create a new protocol parser instance.

    Args:
        persistent_state: Optional pre-loaded PersistentState object.
            If None, the parser starts with empty state.

    Returns:
        A Parser ready to accept bytes via feed().
    """
    return Parser(persistent_state=persistent_state)


def parse_bytes(data: bytes) -> list[Event]:
    """One-shot convenience: creates a parser, feeds bytes, returns events.

    Suitable for batch processing of captured data.
    For streaming use, prefer create_parser() + feed().

    Args:
        data: Raw bytes from the RS-485 bus or a capture file.

    Returns:
        List of all events found in data.
    """
    parser = Parser()
    return parser.feed(data)


def connect(source_config: dict):
    """Open a byte source for manual use with a Parser.

    Args:
        source_config: Connection parameters:
            TCP: {"tcp": "192.168.1.100", "port": 502}
            Serial: {"serial": "/dev/ttyUSB0"} or {"serial": "COM3"}

    Returns:
        A source object with read(size) and close() methods.

    Raises:
        ValueError: If source_config is missing required keys.
    """
    if "tcp" in source_config:
        src = TcpSource(source_config["tcp"], source_config.get("port", 502))
        src.connect()
        return src
    elif "serial" in source_config:
        return SerialSource(source_config["serial"])
    else:
        raise ValueError("source_config must contain 'tcp' or 'serial' key")
