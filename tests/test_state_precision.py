"""Logic tests for the state-precision (rounding) helpers in sensor.py.

Covers the three pure-ish helpers used by the "Change state precision"
options-flow feature: reading/validating the configured precision map from a
config entry, resolving the precision for one sensor description key, and
applying it to a value. These functions only touch plain dicts and a
``.data`` attribute, so a ``SimpleNamespace`` stands in for ``ConfigEntry`` —
no Home Assistant fixtures are needed for this part, but importing
``custom_components.pytap.sensor`` at all still requires Home Assistant to be
installed (unlike ``last_update.py``, this module is not HA-free).
"""

import types
import unittest

from custom_components.pytap.const import (
    MAX_STATE_PRECISION,
    MIN_STATE_PRECISION,
    PRECISION_GROUP_CURRENT,
    PRECISION_GROUP_ENERGY,
    PRECISION_GROUP_PERCENT,
    PRECISION_GROUP_POWER,
    PRECISION_GROUP_TEMPERATURE,
    PRECISION_GROUP_VOLTAGE,
)
from custom_components.pytap.sensor import (
    _PRECISION_GROUP_BY_KEY,
    _apply_state_precision,
    _precision_for_key,
    _state_precision_from_entry,
)


def entry(data):
    return types.SimpleNamespace(data=data)


class ApplyStatePrecision(unittest.TestCase):
    def test_none_precision_passes_float_through(self):
        self.assertEqual(_apply_state_precision(34.4999961853, None), 34.4999961853)

    def test_none_precision_passes_int_through(self):
        self.assertEqual(_apply_state_precision(5, None), 5)

    def test_none_precision_passes_none_through(self):
        self.assertIsNone(_apply_state_precision(None, None))

    def test_non_float_value_passes_through_even_with_precision_set(self):
        # int counters (e.g. readings_today) must never be rounded, even if a
        # caller mistakenly passed a precision for them.
        self.assertEqual(_apply_state_precision(7, 2), 7)

    def test_string_value_passes_through_unchanged(self):
        self.assertEqual(_apply_state_precision("unavailable", 1), "unavailable")

    def test_rounds_at_precision_0(self):
        self.assertEqual(_apply_state_precision(34.6, 0), 35.0)

    def test_rounds_at_precision_1(self):
        self.assertEqual(_apply_state_precision(48.77123, 1), 48.8)

    def test_rounds_at_precision_2(self):
        self.assertEqual(_apply_state_precision(48.77123, 2), 48.77)

    def test_rounds_at_precision_3(self):
        self.assertEqual(_apply_state_precision(48.771234, 3), 48.771)

    def test_rounds_at_negative_precision_tens(self):
        # -1 = tens, per Python round() semantics.
        self.assertEqual(_apply_state_precision(1234.0, -1), 1230.0)


class PrecisionForKey(unittest.TestCase):
    def test_every_documented_key_maps_to_its_group(self):
        expected = {
            "voltage_in": PRECISION_GROUP_VOLTAGE,
            "voltage_out": PRECISION_GROUP_VOLTAGE,
            "current_in": PRECISION_GROUP_CURRENT,
            "current_out": PRECISION_GROUP_CURRENT,
            "power": PRECISION_GROUP_POWER,
            "temperature": PRECISION_GROUP_TEMPERATURE,
            "performance": PRECISION_GROUP_PERCENT,
            "dc_dc_duty_cycle": PRECISION_GROUP_PERCENT,
            "daily_energy": PRECISION_GROUP_ENERGY,
            "total_energy": PRECISION_GROUP_ENERGY,
        }
        # Regression against the actual mapping table, not a hand copy of it.
        self.assertEqual(_PRECISION_GROUP_BY_KEY, expected)
        precision_map = {group: 2 for group in set(expected.values())}
        for key, group in expected.items():
            self.assertEqual(_precision_for_key(precision_map, key), 2)

    def test_unmapped_key_is_always_none(self):
        precision_map = {
            PRECISION_GROUP_VOLTAGE: 1,
            PRECISION_GROUP_CURRENT: 2,
            PRECISION_GROUP_POWER: 0,
            PRECISION_GROUP_TEMPERATURE: 1,
            PRECISION_GROUP_PERCENT: 1,
            PRECISION_GROUP_ENERGY: 1,
        }
        for key in ("rssi", "readings_today", "some_future_key"):
            self.assertIsNone(_precision_for_key(precision_map, key))

    def test_mapped_key_with_unconfigured_group_is_none(self):
        self.assertIsNone(_precision_for_key({}, "voltage_in"))

    def test_mapped_key_with_configured_group_returns_value(self):
        self.assertEqual(
            _precision_for_key({PRECISION_GROUP_VOLTAGE: 1}, "voltage_in"), 1
        )
        self.assertEqual(
            _precision_for_key({PRECISION_GROUP_VOLTAGE: 1}, "voltage_out"), 1
        )


class StatePrecisionFromEntry(unittest.TestCase):
    def test_missing_key_returns_empty_dict(self):
        self.assertEqual(_state_precision_from_entry(entry({})), {})

    def test_non_dict_value_returns_empty_dict(self):
        for bad in (None, "off", 5, ["voltage", 1]):
            self.assertEqual(
                _state_precision_from_entry(entry({"state_precision": bad})), {}
            )

    def test_valid_known_groups_pass_through_unchanged(self):
        raw = {
            PRECISION_GROUP_VOLTAGE: 1,
            PRECISION_GROUP_CURRENT: 2,
            PRECISION_GROUP_POWER: 0,
        }
        result = _state_precision_from_entry(entry({"state_precision": raw}))
        self.assertEqual(result, raw)

    def test_unknown_group_is_dropped_others_kept(self):
        raw = {PRECISION_GROUP_VOLTAGE: 1, "bogus_group": 3}
        result = _state_precision_from_entry(entry({"state_precision": raw}))
        self.assertEqual(result, {PRECISION_GROUP_VOLTAGE: 1})

    def test_non_int_coercible_value_is_dropped_others_kept(self):
        raw = {PRECISION_GROUP_VOLTAGE: 1, PRECISION_GROUP_CURRENT: "not-a-number"}
        result = _state_precision_from_entry(entry({"state_precision": raw}))
        self.assertEqual(result, {PRECISION_GROUP_VOLTAGE: 1})

    def test_float_and_numeric_string_values_are_coerced(self):
        # int() truncates float strings would raise, so only genuinely
        # int-coercible values (ints, floats, digit strings) survive.
        raw = {PRECISION_GROUP_VOLTAGE: 2.0, PRECISION_GROUP_CURRENT: "3"}
        result = _state_precision_from_entry(entry({"state_precision": raw}))
        self.assertEqual(result, {PRECISION_GROUP_VOLTAGE: 2, PRECISION_GROUP_CURRENT: 3})

    def test_value_above_max_is_clamped(self):
        raw = {PRECISION_GROUP_VOLTAGE: MAX_STATE_PRECISION + 5}
        result = _state_precision_from_entry(entry({"state_precision": raw}))
        self.assertEqual(result, {PRECISION_GROUP_VOLTAGE: MAX_STATE_PRECISION})

    def test_value_below_min_is_clamped(self):
        raw = {PRECISION_GROUP_VOLTAGE: MIN_STATE_PRECISION - 5}
        result = _state_precision_from_entry(entry({"state_precision": raw}))
        self.assertEqual(result, {PRECISION_GROUP_VOLTAGE: MIN_STATE_PRECISION})


class OffByDefaultRegression(unittest.TestCase):
    """Empty/missing config must be bit-identical to pre-feature behaviour."""

    def test_empty_config_means_every_key_passes_through_unrounded(self):
        precision_map = _state_precision_from_entry(entry({}))
        self.assertEqual(precision_map, {})
        for key in list(_PRECISION_GROUP_BY_KEY) + ["rssi", "readings_today"]:
            precision = _precision_for_key(precision_map, key)
            self.assertIsNone(precision)
            self.assertEqual(_apply_state_precision(34.4999961853, precision), 34.4999961853)


if __name__ == "__main__":
    unittest.main(verbosity=2)
