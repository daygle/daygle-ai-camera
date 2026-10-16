"""Boolean coercion regressions for user-supplied zone settings."""
from __future__ import annotations

from app.zone_schema import (
    normalize_monitoring_zones,
    normalize_zone_activity,
    normalize_zone_loiter,
    normalize_zone_time,
    normalize_zone_tripwire,
)


def test_behavior_rule_string_booleans_use_application_boolean_semantics():
    assert normalize_zone_tripwire({'tripwire': {
        'a': {'x': 0.1, 'y': 0.1}, 'b': {'x': 0.9, 'y': 0.9},
        'enabled': 'false', 'record_on_detect': 'false',
        'email_enabled': 'false', 'push_enabled': 'false',
    }}) == {
        'enabled': False, 'name': 'Tripwire',
        'a': {'x': 0.1, 'y': 0.1}, 'b': {'x': 0.9, 'y': 0.9},
        'direction': 'both', 'labels': [], 'cooldown_seconds': 30,
        'record_on_detect': False, 'email_enabled': False,
        'email_recipients': [], 'push_enabled': False,
        'notify_start': None, 'notify_end': None,
    }
    for normalize_rule, key in (
        (normalize_zone_loiter, 'loiter'),
        (normalize_zone_time, 'time_of_day'),
        (normalize_zone_activity, 'activity_spike'),
    ):
        result = normalize_rule({key: {
            'enabled': 'false', 'record_on_detect': 'false',
            'email_enabled': 'false', 'push_enabled': 'false',
        }})
        assert result['enabled'] is False
        assert result['record_on_detect'] is False
        assert result['email_enabled'] is False
        assert result['push_enabled'] is False


def test_zone_level_string_booleans_use_application_boolean_semantics():
    zone, = normalize_monitoring_zones([{
        'enabled': 'false', 'monitor_objects': 'false',
        'points': [{'x': 0, 'y': 0}, {'x': 1, 'y': 0}, {'x': 1, 'y': 1}],
    }])
    assert zone['enabled'] is False
    assert zone['monitor_objects'] is False
