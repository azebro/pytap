"""Offline FrameScanner verdicts must match what the live Parser.feed keeps."""

import random
from unittest.mock import patch

import pytest

from custom_components.pytap.pytap.core.capture import CapturedChunk
from custom_components.pytap.pytap.core.crc import crc
from custom_components.pytap.pytap.core.parser import MAX_FRAME_SIZE, Parser
from custom_components.pytap.pytap.core.replay import FrameScanner

_ESCAPE = {0x7E: 0, 0x24: 1, 0x23: 2, 0x25: 3, 0xA4: 4, 0xA3: 5, 0xA5: 6}


def _wire(payload=b"\x7e\x24\xff", *, frame_type=0x7777, bad_crc=False):
    body = (0x8001).to_bytes(2, "big") + frame_type.to_bytes(2, "big") + payload
    escaped = bytearray()
    for byte in body + (crc(body) ^ bad_crc).to_bytes(2, "little"):
        escaped.extend((0x7E, _ESCAPE[byte])) if byte in _ESCAPE else escaped.append(byte)
    return b"\x7e\x07" + bytes(escaped) + b"\x7e\x08"


def _parser_frames(data, chunk_sizes):
    parser, frames = Parser(), []
    with patch.object(parser, "_dispatch_frame", side_effect=lambda frame: frames.append(frame) or []):
        for chunk in _split(data, chunk_sizes):
            parser.feed(chunk.data)
    assert parser.counters["frames_received"] == len(frames)
    return [frame.payload.hex() for frame in frames]


def _split(data, chunk_sizes):
    offset, sizes = 0, iter(chunk_sizes)
    while offset < len(data):
        size = next(sizes, len(data))
        yield CapturedChunk(1, 1, offset, 1, data[offset : offset + size])
        offset += size


def _scan(data, chunk_sizes=()):
    scanner = FrameScanner()
    records = [record for chunk in _split(data, chunk_sizes) for record in scanner.feed(chunk)]
    records += list(scanner.finish())
    assert b"".join(bytes.fromhex(record["wire_hex"]) for record in records) == data
    return [record for record in records if record["kind"] == "frame"]


def _assert_parity(data, chunk_sizes=()):
    frames = _scan(data, chunk_sizes)
    kept = [frame["payload_hex"] for frame in frames if not frame["live_parser_would_drop"]]
    assert kept == _parser_frames(data, chunk_sizes)
    return frames


def test_stray_escape_before_start_marker_is_flagged_as_dropped():
    frames = _assert_parity(b"\x7e" + _wire())
    assert frames[0]["crc_valid"] is True
    assert frames[0]["live_parser_would_drop"] is True
    assert frames[0]["live_parser_drop_reason"] == "framing_state"


def test_restart_after_invalid_escape_inside_frame_is_flagged():
    frames = _assert_parity(b"\x7e\x07\xaa\x7e" + _wire())
    assert frames[-1]["crc_valid"] is True and frames[-1]["live_parser_would_drop"] is True


def test_giant_frames_are_marked_and_dropped():
    payload = b"\x11" * MAX_FRAME_SIZE
    frames = _assert_parity(_wire(payload) + _wire())
    assert frames[0]["giant"] is True and frames[0]["crc_valid"] is True
    assert frames[0]["live_parser_drop_reason"] == "giant"
    assert frames[1]["giant"] is False and frames[1]["live_parser_would_drop"] is False


def test_largest_accepted_frame_is_kept():
    frames = _assert_parity(_wire(b"\x11" * (MAX_FRAME_SIZE - 6)))
    assert frames[0]["giant"] is False and frames[0]["live_parser_would_drop"] is False


def test_valid_bad_crc_runt_and_unknown_escape_verdicts():
    data = b"\x00\xffnoise" + _wire() + _wire(bad_crc=True) + b"\x7e\x07\x01\x7e\x08"
    data += b"\x7e\x07\x01\x02\x7e\x09\x03\x7e\x08" + _wire()
    reasons = [frame["live_parser_drop_reason"] for frame in _assert_parity(data, [3, 1, 7])]
    assert reasons == [None, "crc_mismatch", "runt", "unknown_escape", None]


@pytest.mark.parametrize("seed", range(25))
def test_random_streams_match_live_parser(seed):
    rng = random.Random(seed)
    alphabet = b"\x7e\x7e\x7e\x07\x08\x00\x01\x09\xff\x41"
    parts = []
    for _ in range(40):
        choice = rng.random()
        if choice < 0.4:
            parts.append(bytes(rng.choice(alphabet) for _ in range(rng.randint(1, 6))))
        elif choice < 0.8:
            parts.append(_wire(rng.randbytes(rng.randint(0, 12)), bad_crc=rng.random() < 0.2))
        else:
            parts.append(_wire(rng.randbytes(rng.randint(MAX_FRAME_SIZE - 10, MAX_FRAME_SIZE + 4))))
    data = b"".join(parts)
    sizes = [rng.randint(1, 64) for _ in range(len(data))]
    _assert_parity(data, sizes)


def test_scanner_resets_live_framing_at_gaps_like_replay():
    scanner = FrameScanner()
    wire = _wire()
    records = list(scanner.feed(CapturedChunk(1, 1, 0, 1, b"\x7e")))
    records += list(scanner.feed(CapturedChunk(2, 2, 10, 2, wire)))
    frames = [record for record in records if record["kind"] == "frame"]
    assert frames[0]["live_parser_would_drop"] is False
