"""Tests for the diagnostic improvements:

1. Silencing uvicorn's benign "Invalid HTTP request received." noise at the
   source (``main._DropInvalidHttpRequestNoise``) and in the log viewer
   (``app_log_router._is_noise``).
2. The clearer live-status reason when detections produce no zone-rule match
   (``live_monitor._no_object_match_reason``).
"""
from __future__ import annotations

import logging
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


# -- 1a. source-level uvicorn log filter -----------------------------------

def test_drop_invalid_http_request_noise_filter():
    import app.main as main

    filt = main._DropInvalidHttpRequestNoise()

    def _record(msg):
        return logging.LogRecord('uvicorn.error', logging.WARNING, __file__, 1, msg, None, None)

    # The noisy protocol warning is dropped...
    assert filt.filter(_record('Invalid HTTP request received.')) is False
    # ...while every other uvicorn message passes through untouched.
    assert filt.filter(_record('Application startup complete.')) is True
    assert filt.filter(_record('Uvicorn running on http://0.0.0.0:8080')) is True


def test_suppress_uvicorn_request_noise_is_idempotent():
    import app.main as main

    uvicorn_error = logging.getLogger('uvicorn.error')
    uvicorn_access = logging.getLogger('uvicorn.access')
    main._suppress_uvicorn_request_noise()
    main._suppress_uvicorn_request_noise()
    err_filters = [f for f in uvicorn_error.filters if isinstance(f, main._DropInvalidHttpRequestNoise)]
    acc_filters = [f for f in uvicorn_access.filters if isinstance(f, main._DropSuccessfulAccessLogNoise)]
    # Exactly one of each filter attached regardless of how many times called.
    assert len(err_filters) == 1
    assert len(acc_filters) == 1


def test_drop_successful_access_log_filter():
    import app.main as main

    filt = main._DropSuccessfulAccessLogNoise()

    def _access_record(status):
        rec = logging.LogRecord('uvicorn.access', logging.INFO, __file__, 1,
                                '%s - "%s %s HTTP/%s" %d', ('1.2.3.4:5', 'GET', '/api/stats', '1.1', status), None)
        return rec

    # Successful requests are dropped; client/server errors are kept.
    assert filt.filter(_access_record(200)) is False
    assert filt.filter(_access_record(304)) is False
    assert filt.filter(_access_record(404)) is True
    assert filt.filter(_access_record(500)) is True
    # A record without the expected access-log arg shape is kept (safe no-op).
    plain = logging.LogRecord('uvicorn.access', logging.INFO, __file__, 1, 'startup complete', None, None)
    assert filt.filter(plain) is True


# -- 1b. viewer-side drop --------------------------------------------------

def test_app_log_router_is_noise():
    from app.api import app_log_router as alr

    # Invalid-HTTP protocol noise.
    assert alr._is_noise({'message': 'Invalid HTTP request received.'}) is True
    assert alr._is_noise({'message': 'Invalid HTTP request received'}) is True
    # Successful access lines are noise; error access lines are kept.
    assert alr._is_noise({'message': '192.168.30.2:47614 - "GET /api/stats HTTP/1.1" 200 OK'}) is True
    assert alr._is_noise({'message': '192.168.30.2:47614 - "POST /api/events HTTP/1.1" 302 Found'}) is True
    assert alr._is_noise({'message': '192.168.30.2:47614 - "GET /api/missing HTTP/1.1" 404 Not Found'}) is False
    assert alr._is_noise({'message': '192.168.30.2:47614 - "GET /api/x HTTP/1.1" 500 Internal Server Error'}) is False
    # App events are never treated as noise.
    assert alr._is_noise({'message': 'Sound monitor started for camera front'}) is False
    assert alr._is_noise({'message': ''}) is False
    assert alr._is_noise({}) is False


# -- 2. clearer zone-match reason ------------------------------------------

def _poly_zone(points):
    return {'enabled': True, 'monitor_objects': True, 'points': points}


FULL_FRAME = _poly_zone([{'x': 0, 'y': 0}, {'x': 1, 'y': 0}, {'x': 1, 'y': 1}, {'x': 0, 'y': 1}])


def _det(label, x, y, w=0.1, h=0.1):
    return {'label': label, 'confidence': 0.71, 'box': {'x': x, 'y': y, 'width': w, 'height': h}}


def test_reason_inside_zone_no_rule_names_object():
    from app.live_monitor import _no_object_match_reason

    dets = [_det('car', 0.4, 0.4)]  # centre well inside the full-frame polygon
    reason = _no_object_match_reason(dets, ['car'], [FULL_FRAME])
    assert 'no enabled rule matched car' in reason
    assert 'Object Rules' in reason


def test_reason_outside_all_zones():
    from app.live_monitor import _no_object_match_reason

    # A small zone in the top-left; the car centre (0.8, 0.8) is outside it.
    small_zone = _poly_zone([{'x': 0, 'y': 0}, {'x': 0.2, 'y': 0}, {'x': 0.2, 'y': 0.2}, {'x': 0, 'y': 0.2}])
    dets = [_det('car', 0.75, 0.75)]
    reason = _no_object_match_reason(dets, ['car'], [small_zone])
    assert reason == 'outside your zone areas'


def test_reason_no_detections_falls_back():
    from app.live_monitor import _no_object_match_reason

    reason = _no_object_match_reason([], [], [FULL_FRAME])
    assert reason == 'No detections matched this camera and its zone areas.'


def test_reason_no_monitored_zones_is_not_inside():
    from app.live_monitor import _no_object_match_reason

    # With no monitored zones, a detection can't be "inside a zone".
    reason = _no_object_match_reason([_det('car', 0.5, 0.5)], ['car'], [])
    assert reason == 'outside your zone areas'
