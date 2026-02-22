"""Energy accumulation helpers for PyTap.

This module is intentionally Home Assistant-independent so it can be unit-tested
without coordinator/event-loop setup.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from .const import ENERGY_GAP_THRESHOLD_SECONDS, ENERGY_LOW_POWER_THRESHOLD_W


@dataclass
class EnergyAccumulator:
    """Per-barcode energy accumulation state."""

    daily_energy_wh: float = 0.0
    total_energy_wh: float = 0.0
    daily_reset_date: str = ""
    last_power_w: float = 0.0
    last_reading_ts: datetime | None = None


@dataclass(frozen=True)
class EnergyUpdateResult:
    """Result metadata for a single accumulation step."""

    increment_wh: float = 0.0
    discarded_gap_during_production: bool = False


def accumulate_energy(
    acc: EnergyAccumulator,
    power: float,
    now: datetime,
    gap_threshold: int = ENERGY_GAP_THRESHOLD_SECONDS,
    low_power_threshold: float = ENERGY_LOW_POWER_THRESHOLD_W,
) -> EnergyUpdateResult:
    """Integrate a power reading into the accumulator.

    Uses trapezoidal integration over the interval from the previous reading to
    ``now``. Mutates ``acc`` in place.
    """
    power_w = max(power, 0.0)
    today = now.date().isoformat()

    if acc.daily_reset_date != today:
        acc.daily_energy_wh = 0.0
        acc.daily_reset_date = today

    increment_wh = 0.0
    discarded_gap_during_production = False

    if acc.last_reading_ts is not None:
        delta_seconds = (now - acc.last_reading_ts).total_seconds()
        previous_power_w = max(acc.last_power_w, 0.0)

        if 0 < delta_seconds <= gap_threshold:
            increment_wh = ((previous_power_w + power_w) / 2.0) * (
                delta_seconds / 3600.0
            )
            acc.daily_energy_wh += increment_wh
            acc.total_energy_wh += increment_wh
        elif delta_seconds > gap_threshold and (
            previous_power_w > low_power_threshold or power_w > low_power_threshold
        ):
            discarded_gap_during_production = True

    acc.last_power_w = power_w
    acc.last_reading_ts = now

    return EnergyUpdateResult(
        increment_wh=increment_wh,
        discarded_gap_during_production=discarded_gap_during_production,
    )
