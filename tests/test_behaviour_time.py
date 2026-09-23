"""Tier-2 behavioural intelligence: unusual time-of-day.

Covers the pure hour-of-day statistics in ``app/behaviour_baseline.py`` and the
``time_of_day`` slot in ``app/zone_schema.normalize_monitoring_zones``. Both
modules are dependency-free (no fastapi / torch), so this suite runs standalone
as well as under the full CI test run.
"""
from __future__ import annotations

import os
import sys
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from app import behaviour_baseline as bb  # noqa: E402
from app.zone_schema import normalize_monitoring_zones, normalize_zone_time  # noqa: E402


class HourBaselineTests(unittest.TestCase):
    def test_commit_and_probability(self) -> None:
        base = bb.new_time_baseline()
        # 10 days, each active only during hour 18 (evening).
        for _ in range(10):
            base = bb.commit_time_day(base, {18})
        self.assertEqual(base['days'], 10.0)
        # Hour 18 is almost always active; hour 3 never is.
        self.assertGreater(bb.hour_activity_probability(base, 18), 0.8)
        self.assertLess(bb.hour_activity_probability(base, 3), 0.15)

    def test_commit_ignores_out_of_range_and_junk(self) -> None:
        base = bb.commit_time_day(bb.new_time_baseline(), [25, -1, 'x', 6, None])
        self.assertEqual(base['days'], 1.0)
        self.assertEqual(base['hours'][6], 1.0)
        self.assertEqual(sum(base['hours']), 1.0)  # only hour 6 credited

    def test_out_of_range_hour_probability_is_one(self) -> None:
        base = bb.commit_time_day(bb.new_time_baseline(), {1})
        self.assertEqual(bb.hour_activity_probability(base, 99), 1.0)
        self.assertEqual(bb.hour_activity_probability(base, 'x'), 1.0)

    def test_is_unusual_hour_needs_min_days(self) -> None:
        base = bb.new_time_baseline()
        for _ in range(3):  # below TIME_MIN_DAYS
            base = bb.commit_time_day(base, {18})
        self.assertFalse(bb.is_unusual_hour(base, 3))  # still learning
        for _ in range(10):
            base = bb.commit_time_day(base, {18})
        self.assertTrue(bb.is_unusual_hour(base, 3))    # 3am now clearly unusual
        self.assertFalse(bb.is_unusual_hour(base, 18))  # 18 is normal

    def test_corrupted_baseline_coerced(self) -> None:
        base = bb.commit_time_day({'days': 'bad', 'hours': 'nope'}, {5})
        self.assertEqual(base['days'], 1.0)
        self.assertEqual(base['hours'][5], 1.0)


def _obs(key='cam|z1|person', day=100, hour=3, **over):
    base = {
        'key': key, 'zone_id': 'z1', 'zone_name': 'Drive', 'label': 'person',
        'track_id': 7, 'confidence': 0.9, 'day': day, 'hour': hour,
        'threshold': 0.15, 'cooldown_seconds': 1800,
    }
    base.update(over)
    return base


class TimeOfDayStepTests(unittest.TestCase):
    def _train_evenings(self, today, baselines, cooldowns, days):
        # Each day: active at hour 18, committed when the next day arrives.
        for d in range(days):
            bb.time_of_day_step(today, baselines, cooldowns, [_obs(day=100 + d, hour=18)], 0.0)

    def test_learns_then_fires_on_unusual_hour(self) -> None:
        today, baselines, cooldowns = {}, {}, {}
        self._train_evenings(today, baselines, cooldowns, 12)  # commits days 100..110 (11 days)
        # A 3am detection on a new day: baseline has >= min_days, 3am never seen.
        out = bb.time_of_day_step(today, baselines, cooldowns, [_obs(day=200, hour=3)], 1000.0)
        self.assertEqual(len(out['fires']), 1)
        fire = out['fires'][0]
        self.assertEqual(fire['hour'], 3)
        self.assertEqual(fire['zone_id'], 'z1')
        self.assertLess(fire['probability'], 0.15)

    def test_normal_hour_does_not_fire(self) -> None:
        today, baselines, cooldowns = {}, {}, {}
        self._train_evenings(today, baselines, cooldowns, 12)
        out = bb.time_of_day_step(today, baselines, cooldowns, [_obs(day=200, hour=18)], 1000.0)
        self.assertEqual(out['fires'], [])

    def test_cooldown_suppresses_repeat(self) -> None:
        today, baselines, cooldowns = {}, {}, {}
        self._train_evenings(today, baselines, cooldowns, 12)
        first = bb.time_of_day_step(today, baselines, cooldowns, [_obs(day=200, hour=3)], 1000.0)
        self.assertEqual(len(first['fires']), 1)
        # Same day+hour a minute later: inside the 1800s cooldown -> suppressed.
        again = bb.time_of_day_step(today, baselines, cooldowns, [_obs(day=200, hour=3)], 1060.0)
        self.assertEqual(again['fires'], [])
        # Past the cooldown it may fire again.
        later = bb.time_of_day_step(today, baselines, cooldowns, [_obs(day=200, hour=3)], 3000.0)
        self.assertEqual(len(later['fires']), 1)

    def test_day_rollover_commits_previous_day(self) -> None:
        today, baselines, cooldowns = {}, {}, {}
        bb.time_of_day_step(today, baselines, cooldowns, [_obs(day=1, hour=10)], 0.0)
        self.assertNotIn('cam|z1|person', baselines)  # nothing committed until rollover
        # New day commits the previous one.
        out = bb.time_of_day_step(today, baselines, cooldowns, [_obs(day=2, hour=11)], 0.0)
        self.assertEqual(out['committed'], 1)
        self.assertEqual(baselines['cam|z1|person']['days'], 1.0)
        self.assertEqual(baselines['cam|z1|person']['hours'][10], 1.0)

    def test_ignores_malformed(self) -> None:
        today, baselines, cooldowns = {}, {}, {}
        out = bb.time_of_day_step(today, baselines, cooldowns, [None, {}, {'key': 'k'}, _obs(day='x')], 0.0)
        self.assertEqual(out['fires'], [])


class TimeSchemaTests(unittest.TestCase):
    def test_absent_keeps_zone_shape(self) -> None:
        zones = normalize_monitoring_zones([{'id': 'z1', 'name': 'Yard', 'points': [
            {'x': 0, 'y': 0}, {'x': 1, 'y': 0}, {'x': 1, 'y': 1}]}])
        self.assertNotIn('time_of_day', zones[0])

    def test_defaults(self) -> None:
        rule = normalize_zone_time({'time_of_day': {}})
        self.assertTrue(rule['enabled'])
        self.assertEqual(rule['name'], 'Unusual time')
        self.assertEqual(rule['labels'], [])
        self.assertEqual(rule['threshold'], 0.15)
        self.assertEqual(rule['cooldown_seconds'], 1800)
        self.assertTrue(rule['record_on_detect'])
        self.assertIsNone(rule['notify_start'])

    def test_clamps_and_coerces(self) -> None:
        rule = normalize_zone_time({'time_of_day': {
            'threshold': 5, 'cooldown_seconds': -1, 'labels': ['Person', 'person'],
            'name': '  ', 'notify_start': '9:30',
        }})
        self.assertEqual(rule['threshold'], 1.0)        # capped at 1
        self.assertEqual(rule['cooldown_seconds'], 0)   # floored at 0
        self.assertEqual(rule['labels'], ['person'])
        self.assertEqual(rule['name'], 'Unusual time')  # blank -> default
        self.assertEqual(rule['notify_start'], '09:30')

    def test_bad_types_fall_back(self) -> None:
        rule = normalize_zone_time({'time_of_day': {'threshold': 'nan', 'cooldown_seconds': 'x'}})
        self.assertEqual(rule['threshold'], 0.15)
        self.assertEqual(rule['cooldown_seconds'], 1800)

    def test_attached_through_normalize(self) -> None:
        zones = normalize_monitoring_zones([{
            'id': 'z1', 'name': 'Drive',
            'points': [{'x': 0, 'y': 0}, {'x': 1, 'y': 0}, {'x': 1, 'y': 1}],
            'time_of_day': {'enabled': True, 'threshold': 0.05, 'labels': ['person']},
        }])
        self.assertIn('time_of_day', zones[0])
        self.assertEqual(zones[0]['time_of_day']['threshold'], 0.05)

    def test_non_dict_dropped(self) -> None:
        self.assertIsNone(normalize_zone_time({'time_of_day': 'yes'}))
        self.assertIsNone(normalize_zone_time({}))


if __name__ == '__main__':
    unittest.main()
