"""Tier-2 behavioural intelligence: activity spike.

Covers the pure per-hour count statistics in ``app/behaviour_baseline.py`` and
the ``activity_spike`` slot in ``app/zone_schema.normalize_monitoring_zones``.
Both modules are dependency-free (no fastapi / torch).
"""
from __future__ import annotations

import os
import sys
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from app import behaviour_baseline as bb  # noqa: E402
from app.zone_schema import normalize_monitoring_zones, normalize_zone_activity  # noqa: E402


def _obs(track_id, *, day=100, hour=17, key='cam|z1|car', **over):
    base = {
        'key': key, 'baseline_key': f'{key}|{hour}', 'zone_id': 'z1', 'zone_name': 'Drive',
        'label': 'car', 'track_id': track_id, 'confidence': 0.9, 'day': day, 'hour': hour,
        'min_count': 5, 'sensitivity': 3, 'cooldown_seconds': 900,
    }
    base.update(over)
    return base


class ActivityStepTests(unittest.TestCase):
    def _train(self, today, baselines, cooldowns, per_day_count, days, hour=17):
        # Each training day: `per_day_count` distinct cars in `hour`, then roll
        # to the next day's same hour so the completed hour is committed.
        for d in range(days):
            obs = [_obs(t, day=100 + d, hour=hour) for t in range(per_day_count)]
            bb.activity_step(today, baselines, cooldowns, obs, 0.0)
        # One more day to commit the final training day.
        bb.activity_step(today, baselines, cooldowns, [_obs(0, day=100 + days, hour=hour)], 0.0)

    def test_quiet_while_learning(self) -> None:
        today, baselines, cooldowns = {}, {}, {}
        # Day one: a big burst, but no history yet -> must not fire.
        obs = [_obs(t, day=1) for t in range(20)]
        out = bb.activity_step(today, baselines, cooldowns, obs, 0.0)
        self.assertEqual(out['fires'], [])

    def test_fires_on_burst_after_learning(self) -> None:
        today, baselines, cooldowns = {}, {}, {}
        self._train(today, baselines, cooldowns, per_day_count=3, days=8)  # normal ~3 cars at 17:00
        bkey = 'cam|z1|car|17'
        self.assertGreaterEqual(baselines[bkey]['count'], 5)
        # A new day at 17:00 with 12 cars -> well over max(5, 3 + 3*std). It
        # fires in real time the moment the running count crosses the threshold,
        # so the reported count is the crossing point (>= threshold), not 12.
        p2, c2 = {}, {}
        obs = [_obs(t, day=500, hour=17) for t in range(12)]
        out = bb.activity_step(p2, baselines, c2, obs, 1000.0)
        self.assertEqual(len(out['fires']), 1)
        fire = out['fires'][0]
        self.assertEqual(fire['hour'], 17)
        self.assertGreaterEqual(fire['count'], fire['threshold'])
        self.assertGreaterEqual(fire['count'], 5)

    def test_normal_volume_does_not_fire(self) -> None:
        today, baselines, cooldowns = {}, {}, {}
        self._train(today, baselines, cooldowns, per_day_count=3, days=8)
        p2, c2 = {}, {}
        out = bb.activity_step(p2, baselines, c2, [_obs(t, day=500, hour=17) for t in range(3)], 1000.0)
        self.assertEqual(out['fires'], [])

    def test_distinct_tracks_counted_once(self) -> None:
        today, baselines, cooldowns = {}, {}, {}
        self._train(today, baselines, cooldowns, per_day_count=2, days=8)
        p2, c2 = {}, {}
        # The same 3 tracks reported across many cycles is a count of 3, not 30.
        for _ in range(10):
            out = bb.activity_step(p2, baselines, c2, [_obs(t, day=500, hour=17) for t in range(3)], 1000.0)
        self.assertEqual(p2['cam|z1|car']['tracks'], {0, 1, 2})
        self.assertEqual(out['fires'], [])  # 3 is normal-ish, not a spike

    def test_fires_once_per_bucket(self) -> None:
        today, baselines, cooldowns = {}, {}, {}
        self._train(today, baselines, cooldowns, per_day_count=3, days=8)
        p2, c2 = {}, {}
        first = bb.activity_step(p2, baselines, c2, [_obs(t, day=500, hour=17) for t in range(12)], 1000.0)
        self.assertEqual(len(first['fires']), 1)
        # More cars in the same hour bucket: already fired -> no repeat.
        again = bb.activity_step(p2, baselines, c2, [_obs(t, day=500, hour=17) for t in range(15)], 1001.0)
        self.assertEqual(again['fires'], [])

    def test_hour_rollover_commits_count(self) -> None:
        today, baselines, cooldowns = {}, {}, {}
        bb.activity_step(today, baselines, cooldowns, [_obs(t, day=1, hour=9) for t in range(4)], 0.0)
        self.assertNotIn('cam|z1|car|9', baselines)  # not committed until rollover
        bb.activity_step(today, baselines, cooldowns, [_obs(0, day=1, hour=10)], 0.0)
        self.assertEqual(baselines['cam|z1|car|9']['count'], 1.0)
        self.assertEqual(bb.baseline_mean(baselines['cam|z1|car|9']), 4.0)

    def test_ignores_malformed(self) -> None:
        today, baselines, cooldowns = {}, {}, {}
        out = bb.activity_step(today, baselines, cooldowns, [None, {}, {'key': 'k'}, _obs(1, day='x')], 0.0)
        self.assertEqual(out['fires'], [])


class ActivityZoneGatingTests(unittest.TestCase):
    def setUp(self) -> None:
        from app import behaviour_monitor
        self.mon = behaviour_monitor

    def test_selects_only_enabled(self) -> None:
        settings = {'name': 'Cam', 'detection': {'zones': [
            {'id': 'a', 'activity_spike': {'enabled': True}},
            {'id': 'b', 'activity_spike': {'enabled': False}},
            {'id': 'c', 'enabled': False, 'activity_spike': {'enabled': True}},
            {'id': 'd'},
            {'id': 'e', 'activity_spike': {}},
        ]}}
        ids = [z['id'] for z in self.mon._enabled_zones_with_activity(settings)]
        self.assertEqual(ids, ['a', 'e'])

    def test_no_zones(self) -> None:
        self.assertEqual(self.mon._enabled_zones_with_activity({}), [])
        self.assertEqual(self.mon._enabled_zones_with_activity(None), [])


class ActivitySchemaTests(unittest.TestCase):
    def test_absent_keeps_shape(self) -> None:
        zones = normalize_monitoring_zones([{'id': 'z1', 'name': 'Yard', 'points': [
            {'x': 0, 'y': 0}, {'x': 1, 'y': 0}, {'x': 1, 'y': 1}]}])
        self.assertNotIn('activity_spike', zones[0])

    def test_defaults(self) -> None:
        rule = normalize_zone_activity({'activity_spike': {}})
        self.assertTrue(rule['enabled'])
        self.assertEqual(rule['name'], 'Activity spike')
        self.assertEqual(rule['min_count'], 5)
        self.assertEqual(rule['sensitivity'], 3.0)
        self.assertEqual(rule['cooldown_seconds'], 900)
        self.assertTrue(rule['record_on_detect'])
        self.assertIsNone(rule['notify_start'])

    def test_clamps_and_coerces(self) -> None:
        rule = normalize_zone_activity({'activity_spike': {
            'min_count': 0, 'sensitivity': 50, 'cooldown_seconds': -1,
            'labels': ['Car', 'car'], 'name': '  ', 'notify_start': '8:5',
        }})
        self.assertEqual(rule['min_count'], 1)
        self.assertEqual(rule['sensitivity'], 10.0)
        self.assertEqual(rule['cooldown_seconds'], 0)
        self.assertEqual(rule['labels'], ['car'])
        self.assertEqual(rule['name'], 'Activity spike')
        self.assertEqual(rule['notify_start'], '08:05')

    def test_bad_types_fall_back(self) -> None:
        rule = normalize_zone_activity({'activity_spike': {
            'min_count': 'x', 'sensitivity': 'nan', 'cooldown_seconds': 'y'}})
        self.assertEqual(rule['min_count'], 5)
        self.assertEqual(rule['sensitivity'], 3.0)
        self.assertEqual(rule['cooldown_seconds'], 900)

    def test_attached_through_normalize(self) -> None:
        zones = normalize_monitoring_zones([{
            'id': 'z1', 'name': 'Drive',
            'points': [{'x': 0, 'y': 0}, {'x': 1, 'y': 0}, {'x': 1, 'y': 1}],
            'activity_spike': {'enabled': True, 'min_count': 8, 'labels': ['car']},
        }])
        self.assertIn('activity_spike', zones[0])
        self.assertEqual(zones[0]['activity_spike']['min_count'], 8)

    def test_non_dict_dropped(self) -> None:
        self.assertIsNone(normalize_zone_activity({'activity_spike': 'yes'}))
        self.assertIsNone(normalize_zone_activity({}))


if __name__ == '__main__':
    unittest.main()
