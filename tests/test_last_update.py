"""Logic tests for the last_update sparse write-once helpers.

These cover the pure, Home-Assistant-free logic in
``custom_components.pytap.last_update``: raster flooring, the sparse
write-once emission cadence (quantized), raw/off behaviour, per-module
independence, and the quantization-interval normalization (including the
2x-write-interval floor in averaged mode). They import no Home Assistant
modules and run standalone.
"""

import unittest
from datetime import datetime, timezone

from custom_components.pytap.const import (
    LAST_UPDATE_MODE_OFF,
    LAST_UPDATE_MODE_QUANTIZED,
    LAST_UPDATE_MODE_RAW,
    MAX_LAST_UPDATE_QUANTIZATION,
    MIN_LAST_UPDATE_QUANTIZATION,
)
from custom_components.pytap.last_update import (
    compute_last_update,
    grid_cell,
    normalize_quantization,
)

# A fixed epoch that is NOT on a 60 s boundary (…000 is; …030 is mid-cell).
BASE = 1_700_000_040  # 60 s-aligned (epoch % 60 == 0) so cell math is exact


def at(epoch):
    return datetime.fromtimestamp(epoch, tz=timezone.utc)


class RawMode(unittest.TestCase):
    def test_returns_full_isoformat(self):
        now = at(BASE + 12)
        value, cell = compute_last_update(LAST_UPDATE_MODE_RAW, 60, now, None)
        # Regression: raw must reproduce the original isoformat() output byte
        # for byte (this is the non-breaking default).
        self.assertEqual(value, now.isoformat())

    def test_cell_passthrough(self):
        # raw never touches the per-module raster tracker.
        for prev in (None, 5, 999):
            _, cell = compute_last_update(
                LAST_UPDATE_MODE_RAW, 60, at(BASE), prev
            )
            self.assertEqual(cell, prev)

    def test_changes_every_reading(self):
        v1, _ = compute_last_update(LAST_UPDATE_MODE_RAW, 60, at(BASE), None)
        v2, _ = compute_last_update(LAST_UPDATE_MODE_RAW, 60, at(BASE + 1), None)
        self.assertNotEqual(v1, v2)  # this is exactly why raw triggers a write


class OffMode(unittest.TestCase):
    def test_always_none(self):
        for prev in (None, 7):
            value, cell = compute_last_update(
                LAST_UPDATE_MODE_OFF, 60, at(BASE + 33), prev
            )
            self.assertIsNone(value)
            self.assertEqual(cell, prev)


class QuantizedFlooring(unittest.TestCase):
    def test_grid_cell_matches_floor_div(self):
        self.assertEqual(grid_cell(at(BASE + 59), 60), (BASE + 59) // 60)

    def test_first_emission_is_floored_value(self):
        now = at(BASE + 37)  # mid-cell reading
        value, cell = compute_last_update(
            LAST_UPDATE_MODE_QUANTIZED, 60, now, None
        )
        floored_epoch = (int(now.timestamp()) // 60) * 60
        expected = datetime.fromtimestamp(
            floored_epoch, tz=now.tzinfo
        ).isoformat()
        self.assertEqual(value, expected)
        self.assertEqual(cell, int(now.timestamp()) // 60)

    def test_floor_value_matches_legacy_dense_formula(self):
        # The v2.2 dense formula was epoch - (epoch % quant); the sparse value
        # on first-in-cell must be identical — only the cadence changed.
        now = at(BASE + 37)
        epoch = int(now.timestamp())
        legacy = datetime.fromtimestamp(
            epoch - (epoch % 60), tz=now.tzinfo
        ).isoformat()
        value, _ = compute_last_update(
            LAST_UPDATE_MODE_QUANTIZED, 60, now, None
        )
        self.assertEqual(value, legacy)


class QuantizedSparseCadence(unittest.TestCase):
    def test_write_once_then_suppress_then_next_cell(self):
        # First reading in the cell -> emit + advance tracker.
        v0, c0 = compute_last_update(
            LAST_UPDATE_MODE_QUANTIZED, 60, at(BASE + 5), None
        )
        self.assertIsNotNone(v0)
        # Further readings in the SAME cell -> suppressed (None), same cell.
        for off in (10, 20, 59):
            v, c = compute_last_update(
                LAST_UPDATE_MODE_QUANTIZED, 60, at(BASE + off), c0
            )
            self.assertIsNone(v)
            self.assertEqual(c, c0)
        # First reading in the NEXT cell -> emit again, tracker advances.
        v_next, c_next = compute_last_update(
            LAST_UPDATE_MODE_QUANTIZED, 60, at(BASE + 60), c0
        )
        self.assertIsNotNone(v_next)
        self.assertEqual(c_next, c0 + 1)
        self.assertNotEqual(v_next, v0)

    def test_exactly_one_emission_per_cell_over_a_stream(self):
        # Simulate one module polled every 5 s across two 60 s cells.
        cell = None
        emissions = 0
        for epoch in range(BASE, BASE + 120, 5):  # 24 readings, 2 cells
            v, cell = compute_last_update(
                LAST_UPDATE_MODE_QUANTIZED, 60, at(epoch), cell
            )
            if v is not None:
                emissions += 1
        self.assertEqual(emissions, 2)  # one per cell, not one per reading


class QuantizedPerModule(unittest.TestCase):
    def test_two_modules_track_independently(self):
        # Two modules share the grid but keep separate trackers; each emits on
        # its own first-in-cell reading.
        cells = {"A": None, "B": None}
        vA, cells["A"] = compute_last_update(
            LAST_UPDATE_MODE_QUANTIZED, 60, at(BASE + 3), cells["A"]
        )
        # Module A already emitted this cell; a second A reading suppresses.
        vA2, cells["A"] = compute_last_update(
            LAST_UPDATE_MODE_QUANTIZED, 60, at(BASE + 8), cells["A"]
        )
        # Module B has not emitted yet this cell -> it still emits.
        vB, cells["B"] = compute_last_update(
            LAST_UPDATE_MODE_QUANTIZED, 60, at(BASE + 8), cells["B"]
        )
        self.assertIsNotNone(vA)
        self.assertIsNone(vA2)
        self.assertIsNotNone(vB)
        self.assertEqual(cells["A"], cells["B"])  # same cell index


class Normalization(unittest.TestCase):
    def test_live_clamps_only(self):
        # Live: clamp to [MIN, MAX], no multiple-of-interval rule.
        self.assertEqual(
            normalize_quantization(3, 10, True), MIN_LAST_UPDATE_QUANTIZATION
        )
        self.assertEqual(
            normalize_quantization(9999, 10, True), MAX_LAST_UPDATE_QUANTIZATION
        )
        self.assertEqual(normalize_quantization(60, 10, True), 60)

    def test_averaged_normalizes_to_multiple(self):
        self.assertEqual(normalize_quantization(60, 10, False), 60)   # 6x
        self.assertEqual(normalize_quantization(34, 10, False), 30)   # round 3.4 -> 3x
        self.assertEqual(normalize_quantization(36, 10, False), 40)   # round 3.6 -> 4x

    def test_averaged_enforces_min_2x(self):
        # 1x would re-emit every flush and save nothing -> forced to 2x.
        self.assertEqual(normalize_quantization(10, 10, False), 20)
        self.assertEqual(normalize_quantization(5, 10, False), 20)  # clamp then 2x

    def test_max_cap_wins_over_2x_at_long_intervals(self):
        # 2x would exceed the max; the max applies (sparse just saves less).
        self.assertEqual(
            normalize_quantization(200, 200, False), MAX_LAST_UPDATE_QUANTIZATION
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)


class PushBoundaryCadence(unittest.TestCase):
    """Guards the live-mode fix: the sparse decision must be made once per
    push (read chunk), not per reading. Within a chunk multiple readings of a
    module collapse to the last one, so deciding per reading would drop the
    once-per-cell emission. Here we only call compute_last_update at the push
    boundary (once per chunk per module) and assert exactly one emission per
    raster cell regardless of how many readings a chunk carried."""

    def test_one_emission_per_cell_across_chunks(self):
        # Chunks (push boundaries) at these wall-clock epochs. Several chunks
        # fall inside the first 60 s cell, several in the next.
        chunk_times = [
            BASE + 2, BASE + 9, BASE + 25, BASE + 48,      # cell N (4 pushes)
            BASE + 61, BASE + 70, BASE + 95, BASE + 118,   # cell N+1 (4 pushes)
        ]
        cell = None
        emissions = []
        for epoch in chunk_times:
            # Each chunk may have carried many readings; only the push decides.
            v, cell = compute_last_update(
                LAST_UPDATE_MODE_QUANTIZED, 60, at(epoch), cell
            )
            if v is not None:
                emissions.append(v)
        self.assertEqual(len(emissions), 2)  # one per cell, not per chunk
        # The two emissions are the two distinct floored cell starts.
        self.assertNotEqual(emissions[0], emissions[1])
