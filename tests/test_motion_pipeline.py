import unittest

import numpy as np

import app.state as state
from app.zone_detection import _zone_pixel_motion_fraction, zone_motion_detections


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

    def test_zone_motion_fraction_is_finite_for_valid_mask(self):
        zone = {'x': 0, 'y': 0, 'width': 1, 'height': 1}
        mask = np.zeros((state._MOTION_FRAME_H, state._MOTION_FRAME_W), dtype=bool)
        mask[0, 0] = True

        fraction = _zone_pixel_motion_fraction(mask, zone)

        self.assertTrue(np.isfinite(fraction))
        self.assertGreater(fraction, 0.0)


if __name__ == '__main__':
    unittest.main()
