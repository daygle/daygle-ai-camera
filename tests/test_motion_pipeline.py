import unittest

import numpy as np

import app.state as state
from app.zone_detection import (
    _zone_pixel_motion_fraction,
    filter_motion_detections_by_objects,
    zone_motion_detections,
)


class MotionPipelineTests(unittest.TestCase):
    def setUp(self):
        self._dimensions = (state._MOTION_FRAME_W, state._MOTION_FRAME_H)

    def tearDown(self):
        state._MOTION_FRAME_W, state._MOTION_FRAME_H = self._dimensions

    def test_zone_motion_ignores_mask_with_stale_shape(self):
        settings = {
            'detection': {
                'zones': [
                    {
                        'id': 'porch',
                        'name': 'Porch',
                        'enabled': True,
                        'monitor_motion': True,
                        'x': 0,
                        'y': 0,
                        'width': 1,
                        'height': 1,
                        'object_rules': [
                            {'label': 'motion', 'enabled': True, 'min_confidence': 0.1}
                        ],
                    }
                ]
            }
        }
        stale_mask = np.ones((10, 10), dtype=bool)

        self.assertEqual(_zone_pixel_motion_fraction(stale_mask, settings['detection']['zones'][0]), 0.0)
        self.assertEqual(zone_motion_detections(settings, diff_mask=stale_mask), [])

    def test_zone_motion_accepts_camera_local_mask_shape(self):
        settings = {
            'detection': {'zones': [{
                'id': 'local', 'enabled': True, 'monitor_motion': True,
                'x': 0, 'y': 0, 'width': 1, 'height': 1,
                'object_rules': [{'label': 'motion', 'enabled': True, 'min_confidence': 0.1}],
            }]},
        }
        local_mask = np.ones((20, 30), dtype=bool)

        self.assertEqual(
            zone_motion_detections(settings, diff_mask=local_mask, frame_size=(30, 20)),
            [self._expected_motion_detection('local')],
        )

    @staticmethod
    def _expected_motion_detection(zone_id):
        return {
            'confidence': 1.0,
            'zone_id': zone_id, 'zone_name': zone_id,
            'box': {'x': 0.0, 'y': 0.0, 'width': 1.0, 'height': 1.0},
        }

    def test_object_detection_suppresses_overlapping_motion_box(self):
        car = {'label': 'car', 'box': {'x': 0.2, 'y': 0.2, 'width': 0.4, 'height': 0.3}}
        motion = {'label': 'motion', 'motion_event': True, 'box': {'x': 0.2, 'y': 0.2, 'width': 0.4, 'height': 0.3}}
        assert filter_motion_detections_by_objects([motion], [car]) == []

    def test_object_detection_does_not_suppress_unrelated_motion_box(self):
        car = {'label': 'car', 'box': {'x': 0.2, 'y': 0.2, 'width': 0.2, 'height': 0.2}}
        motion = {'label': 'motion', 'motion_event': True, 'box': {'x': 0.7, 'y': 0.7, 'width': 0.1, 'height': 0.1}}
        assert filter_motion_detections_by_objects([motion], [car]) == [motion]

    def test_zone_motion_fraction_is_finite_for_valid_mask(self):
        zone = {'x': 0, 'y': 0, 'width': 1, 'height': 1}
        mask = np.zeros((state._MOTION_FRAME_H, state._MOTION_FRAME_W), dtype=bool)
        mask[0, 0] = True

        fraction = _zone_pixel_motion_fraction(mask, zone)

        self.assertTrue(np.isfinite(fraction))
        self.assertGreater(fraction, 0.0)


if __name__ == '__main__':
    unittest.main()
