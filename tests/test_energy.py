"""Tests for pure energy accumulation helpers."""

from datetime import datetime, timedelta

from custom_components.pytap.const import ENERGY_GAP_THRESHOLD_SECONDS
from custom_components.pytap.energy import EnergyAccumulator, accumulate_energy


def test_trapezoid_basic() -> None:
    """100 W across 60 s should add 1.666.. Wh."""
    acc = EnergyAccumulator(daily_reset_date="2026-02-22")
    start = datetime(2026, 2, 22, 12, 0, 0)

    accumulate_energy(acc, power=100.0, now=start)
    result = accumulate_energy(acc, power=100.0, now=start + timedelta(seconds=60))

    assert round(result.increment_wh, 3) == 1.667
    assert round(acc.daily_energy_wh, 3) == 1.667
    assert round(acc.total_energy_wh, 3) == 1.667


def test_first_reading_no_increment() -> None:
    """First reading should only establish baseline."""
    acc = EnergyAccumulator(daily_reset_date="2026-02-22")
    now = datetime(2026, 2, 22, 12, 0, 0)

    result = accumulate_energy(acc, power=250.0, now=now)

    assert result.increment_wh == 0.0
    assert acc.daily_energy_wh == 0.0
    assert acc.total_energy_wh == 0.0


def test_trapezoid_varying_power() -> None:
    """100 W to 200 W across 120 s should add 5.0 Wh."""
    acc = EnergyAccumulator(daily_reset_date="2026-02-22")
    start = datetime(2026, 2, 22, 12, 0, 0)

    accumulate_energy(acc, power=100.0, now=start)
    result = accumulate_energy(acc, power=200.0, now=start + timedelta(seconds=120))

    assert round(result.increment_wh, 3) == 5.0
    assert round(acc.daily_energy_wh, 3) == 5.0
    assert round(acc.total_energy_wh, 3) == 5.0


def test_trapezoid_zero_power() -> None:
    """Zero-power readings should produce zero energy increment."""
    acc = EnergyAccumulator(daily_reset_date="2026-02-22")
    start = datetime(2026, 2, 22, 12, 0, 0)

    accumulate_energy(acc, power=0.0, now=start)
    result = accumulate_energy(acc, power=0.0, now=start + timedelta(seconds=60))

    assert result.increment_wh == 0.0
    assert acc.daily_energy_wh == 0.0
    assert acc.total_energy_wh == 0.0


def test_negative_power_clamped() -> None:
    """Negative power should be clamped to zero before integration."""
    acc = EnergyAccumulator(daily_reset_date="2026-02-22")
    start = datetime(2026, 2, 22, 12, 0, 0)

    accumulate_energy(acc, power=-50.0, now=start)
    result = accumulate_energy(acc, power=100.0, now=start + timedelta(seconds=60))

    assert round(result.increment_wh, 3) == 0.833
    assert round(acc.daily_energy_wh, 3) == 0.833
    assert round(acc.total_energy_wh, 3) == 0.833


def test_gap_during_production_discarded() -> None:
    """Long gap at non-trivial power should be discarded."""
    acc = EnergyAccumulator(daily_reset_date="2026-02-22")
    start = datetime(2026, 2, 22, 12, 0, 0)

    accumulate_energy(acc, power=120.0, now=start)
    result = accumulate_energy(acc, power=130.0, now=start + timedelta(seconds=300))

    assert result.increment_wh == 0.0
    assert result.discarded_gap_during_production is True
    assert acc.total_energy_wh == 0.0


def test_overnight_gap_skipped_without_discard_flag() -> None:
    """Long gap at near-zero power should be skipped silently."""
    acc = EnergyAccumulator(daily_reset_date="2026-02-22")
    sunset = datetime(2026, 2, 22, 18, 0, 0)
    sunrise = datetime(2026, 2, 23, 7, 0, 0)

    accumulate_energy(acc, power=0.2, now=sunset)
    result = accumulate_energy(acc, power=0.0, now=sunrise)

    assert result.increment_wh == 0.0
    assert result.discarded_gap_during_production is False


def test_daily_reset_preserves_total() -> None:
    """Daily accumulation resets on date change while total is preserved."""
    acc = EnergyAccumulator(daily_reset_date="2026-02-22")
    day1 = datetime(2026, 2, 22, 12, 0, 0)

    accumulate_energy(acc, power=100.0, now=day1)
    accumulate_energy(acc, power=100.0, now=day1 + timedelta(seconds=60))
    day1_total = acc.total_energy_wh

    day2 = datetime(2026, 2, 23, 8, 0, 0)
    accumulate_energy(acc, power=120.0, now=day2)

    assert acc.daily_reset_date == "2026-02-23"
    assert acc.daily_energy_wh == 0.0
    assert acc.total_energy_wh == day1_total


def test_multi_day_gap_resets_daily_and_discards_increment() -> None:
    """A long multi-day gap should reset daily and discard unknown production."""
    acc = EnergyAccumulator(daily_reset_date="2026-02-22")
    day1 = datetime(2026, 2, 22, 12, 0, 0)
    day3 = datetime(2026, 2, 24, 9, 0, 0)

    accumulate_energy(acc, power=250.0, now=day1)
    result = accumulate_energy(acc, power=260.0, now=day3)

    assert result.increment_wh == 0.0
    assert result.discarded_gap_during_production is True
    assert acc.daily_reset_date == "2026-02-24"
    assert acc.daily_energy_wh == 0.0
    assert acc.total_energy_wh == 0.0


def test_normal_reporting_interval_accumulates_over_hour() -> None:
    """Typical 30-second reporting intervals should accumulate correctly."""
    acc = EnergyAccumulator(daily_reset_date="2026-02-22")
    start = datetime(2026, 2, 22, 12, 0, 0)

    for step in range(0, 3601, 30):
        accumulate_energy(acc, power=100.0, now=start + timedelta(seconds=step))

    assert round(acc.daily_energy_wh, 3) == 100.0
    assert round(acc.total_energy_wh, 3) == 100.0
    assert ENERGY_GAP_THRESHOLD_SECONDS > 30


def test_readings_today_increments_per_report() -> None:
    """Each accepted report should increment the daily readings counter."""
    acc = EnergyAccumulator(daily_reset_date="2026-02-22")
    start = datetime(2026, 2, 22, 12, 0, 0)

    accumulate_energy(acc, power=100.0, now=start)
    accumulate_energy(acc, power=110.0, now=start + timedelta(seconds=30))
    accumulate_energy(acc, power=120.0, now=start + timedelta(seconds=60))

    assert acc.readings_today == 3


def test_readings_today_resets_on_new_day() -> None:
    """Daily readings counter should reset when date changes."""
    acc = EnergyAccumulator(daily_reset_date="2026-02-22")
    day1 = datetime(2026, 2, 22, 18, 0, 0)
    day2 = datetime(2026, 2, 23, 8, 0, 0)

    accumulate_energy(acc, power=90.0, now=day1)
    accumulate_energy(acc, power=0.0, now=day1 + timedelta(seconds=30))
    assert acc.readings_today == 2

    accumulate_energy(acc, power=50.0, now=day2)

    assert acc.daily_reset_date == "2026-02-23"
    assert acc.readings_today == 1
