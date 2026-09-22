"""Tier-2 behavioural intelligence: statistical loitering / long-dwell.

Covers the pure baseline statistics in ``app/behaviour_baseline.py`` and the
``loiter`` slot in ``app/zone_schema.normalize_monitoring_zones``. Both modules
are dependency-free (no fastapi / torch), so this suite runs standalone as well
as under the full CI test run.
"""
from __future__ import annotations

import math
import os
import sys
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from app import behaviour_baseline as bb  # noqa: E402
from app.zone_schema import normalize_monitoring_zones, normalize_zone_loiter  # noqa: E402


def _train(samples, base=None):
    baseline = base if base is not None else bb.new_dwell_baseline()
    for sample in samples:
        baseline = bb.update_dwell_baseline(baseline, sample)
    return baseline


class WelfordStatsTests(unittest.TestCase):
    def test_empty_baseline_is_zeroed(self) -> None:
        base = bb.new_dwell_baseline()
        self.assertEqual(base['count'], 0.0)
        self.assertEqual(bb.baseline_mean(base), 0.0)
        self.assertEqual(bb.baseline_std(base), 0.0)

    def test_mean_and_std_match_numpy_free_reference(self) -> None:
        samples = [4, 5, 6, 5, 4, 6, 5, 5]
        base = _train(samples)
        self.assertEqual(base['count'], len(samples))
        self.assertAlmostEqual(bb.baseline_mean(base), sum(samples) / len(samples), places=9)
        # Sample variance (n-1) reference.
        mean = sum(samples) / len(samples)
        var = sum((s - mean) ** 2 for s in samples) / (len(samples) - 1)
        self.assertAlmostEqual(bb.baseline_std(base), math.sqrt(var), places=9)

    def test_single_sample_has_zero_std(self) -> None:
        base = _train([7])
        self.assertEqual(bb.baseline_mean(base), 7.0)
        self.assertEqual(bb.baseline_std(base), 0.0)

    def test_update_ignores_invalid_and_out_of_range(self) -> None:
        base = _train([5, 5, 5])
        before = dict(base)
        for bad in (None, 'x', float('nan'), float('inf'), -3, bb.MAX_SANE_DWELL_SECONDS + 1):
            base = bb.update_dwell_baseline(base, bad)
        self.assertEqual(base['count'], before['count'])
        self.assertAlmostEqual(base['mean'], before['mean'], places=9)

    def test_corrupted_persisted_baseline_is_coerced(self) -> None:
        base = bb.update_dwell_baseline({'count': 'bad', 'mean': None, 'm2': -5}, 10)
        self.assertEqual(base['count'], 1.0)
        self.assertEqual(bb.baseline_mean(base), 10.0)


class DwellThresholdTests(unittest.TestCase):
    def test_learning_uses_floor_only(self) -> None:
        base = _train([2, 2, 2])  # below MIN_SAMPLES
        threshold = bb.dwell_threshold(base, min_dwell_seconds=30, sensitivity=3, min_samples=30)
        self.assertEqual(threshold, 30.0)

    def test_learned_threshold_raises_bar_in_high_dwell_zone(self) -> None:
        # A patio where objects normally linger ~60s (std ~5): the learned
        # threshold should sit well above the 30s floor.
        base = _train([60, 55, 65, 58, 62, 59, 61, 57, 63, 60] * 4)
        self.assertGreaterEqual(base['count'], 30)
        threshold = bb.dwell_threshold(base, min_dwell_seconds=30, sensitivity=3, min_samples=30)
        self.assertGreater(threshold, 60.0)
        self.assertGreater(threshold, 30.0)

    def test_learned_threshold_never_below_floor(self) -> None:
        # A driveway where nobody stops (~2s dwell): the floor still governs.
        base = _train([2, 3, 2, 1, 2, 3, 2, 2, 1, 3] * 4)
        threshold = bb.dwell_threshold(base, min_dwell_seconds=30, sensitivity=3, min_samples=30)
        self.assertEqual(threshold, 30.0)


class EvaluateDwellTests(unittest.TestCase):
    def test_below_floor_is_never_anomalous_while_learning(self) -> None:
        base = _train([2, 2, 2])
        result = bb.evaluate_dwell(base, 20, min_dwell_seconds=30, min_samples=30)
        self.assertFalse(result['anomalous'])
        self.assertTrue(result['learning'])

    def test_over_floor_fires_on_day_one(self) -> None:
        base = bb.new_dwell_baseline()
        result = bb.evaluate_dwell(base, 45, min_dwell_seconds=30, min_samples=30)
        self.assertTrue(result['anomalous'])
        self.assertTrue(result['learning'])
        self.assertGreaterEqual(result['score'], 1.0)

    def test_learned_zone_does_not_fire_on_normal_long_dwell(self) -> None:
        # Patio normal ~60s; a 50s visit is normal there even though it's over
        # the 30s floor, because the learned threshold is higher.
        base = _train([60, 55, 65, 58, 62, 59, 61, 57, 63, 60] * 4)
        result = bb.evaluate_dwell(base, 50, min_dwell_seconds=30, sensitivity=3, min_samples=30)
        self.assertFalse(result['anomalous'])
        self.assertFalse(result['learning'])

    def test_learned_zone_fires_on_true_outlier(self) -> None:
        base = _train([60, 55, 65, 58, 62, 59, 61, 57, 63, 60] * 4)
        result = bb.evaluate_dwell(base, 600, min_dwell_seconds=30, sensitivity=3, min_samples=30)
        self.assertTrue(result['anomalous'])
        self.assertGreater(result['score'], 1.0)
        self.assertGreater(result['threshold'], 60.0)

    def test_invalid_dwell_is_never_anomalous(self) -> None:
        base = _train([2] * 40)
        for bad in (None, 'x', float('nan'), -5):
            result = bb.evaluate_dwell(base, bad, min_dwell_seconds=1, min_samples=30)
            self.assertFalse(result['anomalous'], bad)

    def test_zero_sensitivity_uses_mean_as_learned_term(self) -> None:
        base = _train([10] * 40)
        # sensitivity 0 -> learned term is the mean (10); floor 5 -> threshold 10.
        result = bb.evaluate_dwell(base, 10, min_dwell_seconds=5, sensitivity=0, min_samples=30)
        self.assertEqual(result['threshold'], 10.0)
        self.assertTrue(result['anomalous'])  # dwell == threshold


class DwellSecondsHelperTests(unittest.TestCase):
    def test_span_and_clamp(self) -> None:
        self.assertEqual(bb.dwell_seconds(100.0, 130.0), 30.0)
        self.assertEqual(bb.dwell_seconds(130.0, 100.0), 0.0)  # clock skew clamps
        self.assertIsNone(bb.dwell_seconds(None, 5))
        self.assertIsNone(bb.dwell_seconds('a', 'b'))
        self.assertIsNone(bb.dwell_seconds(float('inf'), 5))


class LoiterSchemaTests(unittest.TestCase):
    def test_absent_loiter_keeps_zone_shape(self) -> None:
        zones = normalize_monitoring_zones([{'id': 'z1', 'name': 'Yard', 'points': [
            {'x': 0, 'y': 0}, {'x': 1, 'y': 0}, {'x': 1, 'y': 1}]}])
        self.assertEqual(len(zones), 1)
        self.assertNotIn('loiter', zones[0])

    def test_defaults_filled_in(self) -> None:
        loiter = normalize_zone_loiter({'loiter': {}})
        self.assertIsNotNone(loiter)
        self.assertTrue(loiter['enabled'])
        self.assertEqual(loiter['name'], 'Loitering')
        self.assertEqual(loiter['labels'], [])
        self.assertEqual(loiter['min_dwell_seconds'], 30)
        self.assertEqual(loiter['sensitivity'], 3.0)
        self.assertEqual(loiter['cooldown_seconds'], 120)
        self.assertTrue(loiter['record_on_detect'])
        self.assertFalse(loiter['email_enabled'])
        self.assertFalse(loiter['push_enabled'])
        self.assertEqual(loiter['email_recipients'], [])

    def test_clamps_and_coerces(self) -> None:
        loiter = normalize_zone_loiter({'loiter': {
            'min_dwell_seconds': 0, 'sensitivity': 50, 'cooldown_seconds': -1,
            'labels': ['Person', 'person', 'car'], 'name': '  ',
        }})
        self.assertEqual(loiter['min_dwell_seconds'], 1)   # floored to >= 1
        self.assertEqual(loiter['sensitivity'], 10.0)      # capped at 10
        self.assertEqual(loiter['cooldown_seconds'], 0)    # floored to >= 0
        self.assertEqual(loiter['labels'], ['person', 'car'])
        self.assertEqual(loiter['name'], 'Loitering')      # blank -> default

    def test_bad_types_fall_back_to_defaults(self) -> None:
        loiter = normalize_zone_loiter({'loiter': {
            'min_dwell_seconds': 'nope', 'sensitivity': 'nan', 'cooldown_seconds': 'x',
        }})
        self.assertEqual(loiter['min_dwell_seconds'], 30)
        self.assertEqual(loiter['sensitivity'], 3.0)
        self.assertEqual(loiter['cooldown_seconds'], 120)

    def test_attached_through_normalize_monitoring_zones(self) -> None:
        zones = normalize_monitoring_zones([{
            'id': 'z1', 'name': 'Porch',
            'points': [{'x': 0, 'y': 0}, {'x': 1, 'y': 0}, {'x': 1, 'y': 1}],
            'loiter': {'enabled': True, 'min_dwell_seconds': 45, 'labels': ['person']},
        }])
        self.assertIn('loiter', zones[0])
        self.assertEqual(zones[0]['loiter']['min_dwell_seconds'], 45)
        self.assertEqual(zones[0]['loiter']['labels'], ['person'])

    def test_non_dict_loiter_is_dropped(self) -> None:
        self.assertIsNone(normalize_zone_loiter({'loiter': 'yes'}))
        self.assertIsNone(normalize_zone_loiter({}))


if __name__ == '__main__':
    unittest.main()
