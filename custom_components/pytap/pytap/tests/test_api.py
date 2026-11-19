"""Tests for the public API module."""

from datetime import datetime

import pytest

import pytap
from pytap.core.parser import Parser


def test_create_parser():
    """create_parser() returns a Parser instance."""
    p = pytap.create_parser()
    assert isinstance(p, Parser)


def test_create_parser_passes_clock_through():
    """Event timestamps come from the clock given to create_parser()."""
    from pytap.tests.test_parser import (
        _build_receive_request,
        _build_receive_response,
        _h_status,
        _power_packet_with,
    )

    fixed = datetime(2001, 2, 3, 4, 5, 6)
    parser = pytap.create_parser(clock=lambda: fixed)
    gw_id = 0x1203
    topology = bytes([0x09, 0x00, 0x1C, 0x00, 0x0D, 0x91, 0x02, 0xAB, 0xCD])
    parser.feed(_build_receive_request(gw_id, 0x0049))
    # Report slot equals the header slot, so its timestamp is exactly the capture time.
    frame = _build_receive_response(
        gw_id, _h_status(0x4A, 0xD457), _power_packet_with(27, slot=0xD457) + topology
    )
    events = parser.feed(frame)

    assert [e.event_type for e in events] == ["power_report", "topology"]
    assert all(e.timestamp == fixed for e in events)


def test_parse_bytes_empty():
    """parse_bytes(b'') returns empty list."""
    events = pytap.parse_bytes(b'')
    assert events == []


def test_parse_bytes_with_data():
    """parse_bytes with enumeration sequence data returns events."""
    from pytap.tests.test_parser import ENUMERATION_SEQUENCE
    events = pytap.parse_bytes(ENUMERATION_SEQUENCE)
    # Should return at least some events
    assert isinstance(events, list)


def test_connect_invalid():
    """connect({}) raises ValueError."""
    with pytest.raises(ValueError, match="'tcp' or 'serial'"):
        pytap.connect({})


def test_version():
    """Version string is available."""
    assert hasattr(pytap, '__version__')
    assert isinstance(pytap.__version__, str)


def test_all_exports_accessible():
    """All items in __all__ are importable."""
    for name in pytap.__all__:
        assert hasattr(pytap, name), f"{name} not accessible from pytap"
