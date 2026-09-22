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


def _obs(now_key='cam|z1|7', baseline='cam|z1|person', **over):
    base = {
        'key': now_key, 'baseline_key': baseline, 'zone_id': 'z1', 'zone_name': 'Yard',
        'label': 'person', 'track_id': 7, 'confidence': 0.9,
        'min_dwell_seconds': 30, 'sensitivity': 3, 'cooldown_seconds': 120,
    }
    base.update(over)
    return base


class LoiterStepTests(unittest.TestCase):
    def test_short_visit_never_fires_and_learns_on_departure(self) -> None:
        presence, baselines, cooldowns = {}, {}, {}
        # Present for a few cycles, always under the 30s floor.
        for t in (0.0, 1.0, 2.0):
            out = bb.loiter_step(presence, baselines, cooldowns, [_obs()], t)
            self.assertEqual(out['fires'], [])
        # Track gone; after the grace window its 2s visit is folded in as a sample.
        out = bb.loiter_step(presence, baselines, cooldowns, [], 2.0 + bb.DEFAULT_DEPARTURE_GRACE_SECONDS + 0.1)
        self.assertEqual(out['learned'], 1)
        self.assertNotIn('cam|z1|7', presence)
        self.assertEqual(baselines['cam|z1|person']['count'], 1.0)

    def test_fires_once_when_dwell_crosses_floor(self) -> None:
        presence, baselines, cooldowns = {}, {}, {}
        bb.loiter_step(presence, baselines, cooldowns, [_obs()], 0.0)       # dwell 0
        out = bb.loiter_step(presence, baselines, cooldowns, [_obs()], 40.0)  # dwell 40 > 30 floor
        self.assertEqual(len(out['fires']), 1)
        fire = out['fires'][0]
        self.assertEqual(fire['zone_id'], 'z1')
        self.assertEqual(fire['label'], 'person')
        self.assertGreaterEqual(fire['dwell'], 40.0)
        # Still present and over threshold next cycle, but it must not re-fire.
        out2 = bb.loiter_step(presence, baselines, cooldowns, [_obs()], 55.0)
        self.assertEqual(out2['fires'], [])

    def test_cooldown_blocks_a_second_track_in_the_same_zone(self) -> None:
        presence, baselines, cooldowns = {}, {}, {}
        a = _obs(now_key='cam|z1|7', track_id=7)
        b = _obs(now_key='cam|z1|8', track_id=8)
        bb.loiter_step(presence, baselines, cooldowns, [a], 0.0)       # A first seen
        bb.loiter_step(presence, baselines, cooldowns, [a, b], 5.0)    # B first seen
        # At t=40 both are over the 30s floor; A (processed first) fires, B is
        # suppressed by the 120s cooldown it shares (same zone+label).
        out40 = bb.loiter_step(presence, baselines, cooldowns, [a, b], 40.0)
        self.assertEqual([f['track_id'] for f in out40['fires']], [7])
        # Once the cooldown elapses, the still-present B fires.
        out200 = bb.loiter_step(presence, baselines, cooldowns, [a, b], 200.0)
        self.assertEqual([f['track_id'] for f in out200['fires']], [8])

    def test_learned_zone_lifts_threshold_so_normal_long_visits_pass(self) -> None:
        # Teach the zone that ~60s visits are normal via completed departures.
        presence, baselines, cooldowns = {}, {}, {}
        grace = bb.DEFAULT_DEPARTURE_GRACE_SECONDS
        for i in range(40):
            key = f'cam|z1|{i}'
            obs = _obs(now_key=key, track_id=i)
            bb.loiter_step(presence, baselines, cooldowns, [obs], 0.0)   # first seen
            bb.loiter_step(presence, baselines, cooldowns, [obs], 60.0)  # last seen at 60s
            bb.loiter_step(presence, baselines, cooldowns, [], 60.0 + grace + 0.1)  # depart -> 60s sample
        self.assertGreaterEqual(baselines['cam|z1|person']['count'], 30)
        self.assertAlmostEqual(baselines['cam|z1|person']['mean'], 60.0, places=6)
        # A 50s visit is over the 30s floor but normal for this zone -> no fire.
        p2, c2 = {}, {}
        bb.loiter_step(p2, baselines, c2, [_obs(now_key='cam|z1|new', track_id=99)], 0.0)
        out = bb.loiter_step(p2, baselines, c2, [_obs(now_key='cam|z1|new', track_id=99)], 50.0)
        self.assertEqual(out['fires'], [])
        # But a genuine 600s outlier still fires.
        out2 = bb.loiter_step(p2, baselines, c2, [_obs(now_key='cam|z1|new', track_id=99)], 600.0)
        self.assertEqual(len(out2['fires']), 1)

    def test_ignores_malformed_observations(self) -> None:
        presence, baselines, cooldowns = {}, {}, {}
        out = bb.loiter_step(presence, baselines, cooldowns, [None, {}, {'key': ''}], 5.0)
        self.assertEqual(out['fires'], [])
        self.assertEqual(presence, {})


class LoiterZoneGatingTests(unittest.TestCase):
    """The monitor's enabled-zone selector (pure; no DB/heavy deps)."""

    def setUp(self) -> None:
        # behaviour_monitor imports app.state at module load, which is light.
        from app import behaviour_monitor
        self.mon = behaviour_monitor

    def _settings(self, zones):
        return {'name': 'Cam', 'detection': {'zones': zones}}

    def test_selects_only_enabled_zones_with_enabled_loiter(self) -> None:
        settings = self._settings([
            {'id': 'a', 'loiter': {'enabled': True}},
            {'id': 'b', 'loiter': {'enabled': False}},      # rule disabled
            {'id': 'c', 'enabled': False, 'loiter': {'enabled': True}},  # zone hidden
            {'id': 'd'},                                     # no loiter rule
            {'id': 'e', 'loiter': {}},                       # loiter present, enabled defaults on
        ])
        ids = [z['id'] for z in self.mon._enabled_zones_with_loiter(settings)]
        self.assertEqual(ids, ['a', 'e'])

    def test_no_zones_or_bad_settings(self) -> None:
        self.assertEqual(self.mon._enabled_zones_with_loiter({}), [])
        self.assertEqual(self.mon._enabled_zones_with_loiter(None), [])
        self.assertEqual(self.mon._enabled_zones_with_loiter({'detection': {'zones': []}}), [])


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
