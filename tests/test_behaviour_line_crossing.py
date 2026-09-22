"""Tier-1 behavioural intelligence: directional line-crossing (tripwire).

Covers the pure geometry in ``app/behaviour.py`` and the ``tripwire`` slot in
``app/zone_schema.normalize_monitoring_zones``. Both modules are dependency-free
(no fastapi / torch), so this suite runs standalone as well as under the full
CI test run.
"""
from __future__ import annotations

import os
import sys
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from app import behaviour  # noqa: E402
from app.zone_schema import normalize_monitoring_zones, normalize_zone_tripwire  # noqa: E402


# Vertical tripwire from bottom (A) to top (B): facing A->B, "left" is west
# (smaller x), "right" is east (larger x). So west->east is FORWARD.
A = (0.5, 0.0)
B = (0.5, 1.0)


class LineCrossingGeometryTests(unittest.TestCase):
    def test_west_to_east_is_forward(self) -> None:
        self.assertEqual(behaviour.line_crossing((0.4, 0.5), (0.6, 0.5), A, B), behaviour.FORWARD)

    def test_east_to_west_is_backward(self) -> None:
        self.assertEqual(behaviour.line_crossing((0.6, 0.5), (0.4, 0.5), A, B), behaviour.BACKWARD)

    def test_no_crossing_when_staying_on_one_side(self) -> None:
        self.assertIsNone(behaviour.line_crossing((0.6, 0.5), (0.7, 0.5), A, B))
        self.assertIsNone(behaviour.line_crossing((0.2, 0.1), (0.3, 0.9), A, B))

    def test_parallel_movement_along_the_line_does_not_cross(self) -> None:
        # Moving straight up, well to one side.
        self.assertIsNone(behaviour.line_crossing((0.3, 0.1), (0.3, 0.9), A, B))

    def test_segment_bound_not_infinite_line(self) -> None:
        # Short tripwire near the middle; a crossing far above it (outside the
        # segment's y-range) must NOT count -- proves we intersect the segment,
        # not the infinite line.
        short_a, short_b = (0.5, 0.4), (0.5, 0.6)
        self.assertIsNone(behaviour.line_crossing((0.4, 0.9), (0.6, 0.9), short_a, short_b))
        self.assertEqual(
            behaviour.line_crossing((0.4, 0.5), (0.6, 0.5), short_a, short_b), behaviour.FORWARD
        )

    def test_none_endpoints_return_none(self) -> None:
        self.assertIsNone(behaviour.line_crossing(None, (0.6, 0.5), A, B))
        self.assertIsNone(behaviour.line_crossing((0.4, 0.5), None, A, B))

    def test_diagonal_tripwire_direction(self) -> None:
        # Diagonal A(0,0)->B(1,1); point below-right (1,0) is on the RIGHT,
        # point above-left (0,1) is on the LEFT.
        da, db = (0.0, 0.0), (1.0, 1.0)
        self.assertEqual(behaviour.line_crossing((0.9, 0.1), (0.1, 0.9), da, db), behaviour.BACKWARD)
        self.assertEqual(behaviour.line_crossing((0.1, 0.9), (0.9, 0.1), da, db), behaviour.FORWARD)

    def test_segments_intersect_touch_case(self) -> None:
        # Step that ends exactly on the tripwire still intersects.
        self.assertTrue(behaviour.segments_intersect((0.4, 0.5), (0.5, 0.5), A, B))


class CrossingMatchTests(unittest.TestCase):
    def test_both_accepts_either_direction(self) -> None:
        self.assertTrue(behaviour.crossing_matches(behaviour.FORWARD, 'both'))
        self.assertTrue(behaviour.crossing_matches(behaviour.BACKWARD, 'both'))

    def test_direction_filter(self) -> None:
        self.assertTrue(behaviour.crossing_matches(behaviour.FORWARD, 'forward'))
        self.assertFalse(behaviour.crossing_matches(behaviour.BACKWARD, 'forward'))

    def test_none_never_matches(self) -> None:
        self.assertFalse(behaviour.crossing_matches(None, 'both'))


class TripwireCrossingWrapperTests(unittest.TestCase):
    def _wire(self, **over):
        wire = {
            'enabled': True,
            'a': {'x': 0.5, 'y': 0.0},
            'b': {'x': 0.5, 'y': 1.0},
            'direction': 'both',
        }
        wire.update(over)
        return wire

    def test_matches_configured_direction(self) -> None:
        wire = self._wire(direction='forward')
        self.assertEqual(behaviour.tripwire_crossing((0.4, 0.5), (0.6, 0.5), wire), behaviour.FORWARD)
        self.assertIsNone(behaviour.tripwire_crossing((0.6, 0.5), (0.4, 0.5), wire))

    def test_both_direction(self) -> None:
        wire = self._wire(direction='both')
        self.assertEqual(behaviour.tripwire_crossing((0.6, 0.5), (0.4, 0.5), wire), behaviour.BACKWARD)

    def test_disabled_never_fires(self) -> None:
        wire = self._wire(enabled=False)
        self.assertIsNone(behaviour.tripwire_crossing((0.4, 0.5), (0.6, 0.5), wire))

    def test_tuple_points_accepted(self) -> None:
        wire = self._wire()
        self.assertEqual(behaviour.tripwire_crossing((0.4, 0.5), (0.6, 0.5), wire), behaviour.FORWARD)

    def test_malformed_returns_none(self) -> None:
        self.assertIsNone(behaviour.tripwire_crossing((0.4, 0.5), (0.6, 0.5), {'a': {'x': 0.5, 'y': 0.0}}))
        self.assertIsNone(behaviour.tripwire_crossing((0.4, 0.5), (0.6, 0.5), None))


class ZoneTripwireSchemaTests(unittest.TestCase):
    def test_valid_tripwire_normalizes_with_defaults(self) -> None:
        tw = normalize_zone_tripwire({'tripwire': {'a': {'x': 0.2, 'y': 0.1}, 'b': {'x': 0.8, 'y': 0.9}}})
        self.assertIsNotNone(tw)
        self.assertEqual(tw['a'], {'x': 0.2, 'y': 0.1})
        self.assertEqual(tw['b'], {'x': 0.8, 'y': 0.9})
        self.assertEqual(tw['direction'], 'both')
        self.assertTrue(tw['enabled'])
        self.assertEqual(tw['cooldown_seconds'], 30)
        self.assertEqual(tw['name'], 'Tripwire')
        self.assertEqual(tw['labels'], [])

    def test_degenerate_line_rejected(self) -> None:
        self.assertIsNone(normalize_zone_tripwire({'tripwire': {'a': {'x': 0.5, 'y': 0.5}, 'b': {'x': 0.5, 'y': 0.5}}}))

    def test_missing_or_bad_tripwire_returns_none(self) -> None:
        self.assertIsNone(normalize_zone_tripwire({}))
        self.assertIsNone(normalize_zone_tripwire({'tripwire': 'nope'}))
        self.assertIsNone(normalize_zone_tripwire({'tripwire': {'a': {'x': 0.1, 'y': 0.1}}}))

    def test_bad_direction_falls_back_to_both(self) -> None:
        tw = normalize_zone_tripwire({'tripwire': {'a': {'x': 0, 'y': 0}, 'b': {'x': 1, 'y': 1}, 'direction': 'sideways'}})
        self.assertEqual(tw['direction'], 'both')

    def test_clamps_and_coerces_fields(self) -> None:
        tw = normalize_zone_tripwire({'tripwire': {
            'a': {'x': -1, 'y': 2}, 'b': {'x': 0.9, 'y': 0.1},
            'direction': 'FORWARD', 'cooldown_seconds': -5, 'enabled': False,
            'name': '  Gate  ', 'labels': ['Person', 'car'], 'push_enabled': True,
        }})
        self.assertEqual(tw['a'], {'x': 0.0, 'y': 1.0})  # clamped to [0,1]
        self.assertEqual(tw['direction'], 'forward')
        self.assertEqual(tw['cooldown_seconds'], 0)  # negative clamped
        self.assertFalse(tw['enabled'])
        self.assertEqual(tw['name'], 'Gate')
        self.assertIn('person', tw['labels'])
        self.assertTrue(tw['push_enabled'])

    def test_monitoring_zones_attaches_tripwire_only_when_present(self) -> None:
        zones = normalize_monitoring_zones([
            {'id': 'z1', 'name': 'With', 'points': [[0, 0], [1, 0], [1, 1]],
             'tripwire': {'a': {'x': 0.5, 'y': 0}, 'b': {'x': 0.5, 'y': 1}}},
            {'id': 'z2', 'name': 'Without', 'points': [[0, 0], [1, 0], [1, 1]]},
        ])
        self.assertIn('tripwire', zones[0])
        self.assertEqual(zones[0]['tripwire']['direction'], 'both')
        self.assertNotIn('tripwire', zones[1])


if __name__ == '__main__':
    unittest.main()
