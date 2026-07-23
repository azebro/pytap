"""Pure helpers for the ``last_update`` attribute (v2.3, sparse write-once).

Deliberately free of Home Assistant imports so the write-trigger logic can
be unit-tested in isolation.  Two responsibilities:

* :func:`compute_last_update` decides the per-module ``last_update`` value at
  a single push boundary and tracks which raster cell was last emitted.  In
  ``quantized`` mode the floored timestamp is therefore written **once per
  raster period** and suppressed (``None``) in between — near-``off`` storage
  while still advancing a per-module timestamp for outage detection.
* :func:`normalize_quantization` clamps/normalizes the configured interval.

``raw`` and ``off`` behaviour is unchanged from v2.2; only ``quantized``
changes from "floored value on every reading" (dense) to "written once per
raster period" (sparse).
"""

from __future__ import annotations

from datetime import datetime

from .const import (
    LAST_UPDATE_MODE_OFF,
    LAST_UPDATE_MODE_QUANTIZED,
    LAST_UPDATE_MODE_RAW,
    MAX_LAST_UPDATE_QUANTIZATION,
    MIN_LAST_UPDATE_QUANTIZATION,
)


def grid_cell(now: datetime, quant: int) -> int:
    """Return the raster-cell index for ``now`` on a ``quant``-second grid."""
    return int(now.timestamp()) // quant


def compute_last_update(
    mode: str,
    quant: int,
    now: datetime,
    last_cell: int | None,
) -> tuple[str | None, int | None]:
    """Return ``(attribute_value, updated_last_cell)`` for one push.

    ``raw``       → full-resolution ISO timestamp (previous behaviour); the
                    cell tracker is passed through unchanged.
    ``off``       → always ``None``.
    ``quantized`` → sparse write-once: on the first push of a new raster
                    cell, the floored ISO timestamp and the new cell index;
                    on any further push within the same cell, ``None`` and
                    the unchanged cell.  I.e. emitted once per raster period,
                    absent in between.

    The caller owns the per-module ``last_cell`` state and must feed back the
    returned cell.  ``compute_last_update`` must be called exactly once per
    push boundary per module (live: per reading; averaged: per flush) so the
    tracker advances in step with what is actually pushed to Home Assistant.
    """
    if mode == LAST_UPDATE_MODE_OFF:
        return None, last_cell
    if mode == LAST_UPDATE_MODE_RAW:
        return now.isoformat(), last_cell
    # quantized, sparse write-once per raster cell
    cell = int(now.timestamp()) // quant
    if cell == last_cell:
        return None, last_cell
    floored = cell * quant
    return datetime.fromtimestamp(floored, tz=now.tzinfo).isoformat(), cell


def normalize_quantization(
    seconds: int,
    write_interval: int,
    live_reporting: bool,
) -> int:
    """Clamp/normalize the quantization interval.

    Always clamped to ``[MIN_LAST_UPDATE_QUANTIZATION,
    MAX_LAST_UPDATE_QUANTIZATION]``.  In averaged (non-live) reporting the
    value is normalized to a multiple of the write interval and forced to at
    least **2×** it: at 1× every flush opens a new raster cell, so the sparse
    attribute would be re-emitted on every push and save nothing.  (If 2× the
    write interval exceeds the maximum, the maximum applies and the effective
    factor is smaller — sparse simply saves less at very long intervals.)
    """
    seconds = max(
        MIN_LAST_UPDATE_QUANTIZATION,
        min(MAX_LAST_UPDATE_QUANTIZATION, seconds),
    )
    if not live_reporting and write_interval > 0:
        multiples = max(2, round(seconds / write_interval))
        seconds = min(MAX_LAST_UPDATE_QUANTIZATION, multiples * write_interval)
    return seconds
