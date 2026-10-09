"""Unit tests for ``app.label_groups`` (user-managed object-label groups).

Covers the first-run defaults, tolerant normalization, the persisted-setting
resolution, and the cache refresh that feeds ``zone_schema.label_matches`` /
``detection_label_in_allowed``. Runs without FastAPI (the API integration test
for the router lives in tests/test_api_auth_settings.py).
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import app.label_groups as lg  # noqa: E402
import app.state as _state  # noqa: E402
from app.zone_schema import (  # noqa: E402
    detection_label_in_allowed,
    label_matches,
)


class _FakeDB:
    """Minimal stand-in for the settings row the group resolver reads."""

    def __init__(self, settings=None):
        self._settings = settings or {}

    def get_setting(self, key):
        return self._settings.get(key)


@pytest.fixture(autouse=True)
def _hermetic_groups(monkeypatch):
    """Reset the process-wide group cache to defaults around every test."""
    monkeypatch.setattr(_state, 'database', None)
    lg.refresh_label_groups()
    yield
    monkeypatch.setattr(_state, 'database', None)
    lg.refresh_label_groups()


def test_defaults_expose_animal_and_pet():
    assert 'cat' in lg.DEFAULT_LABEL_GROUPS['animal']
    assert 'dog' in lg.DEFAULT_LABEL_GROUPS['animal']
    assert lg.DEFAULT_LABEL_GROUPS['pet'] == frozenset({'cat', 'dog'})


def test_effective_returns_defaults_without_database():
    assert 'animal' in lg.cached_label_groups()
    assert lg.cached_label_groups()['pet'] == frozenset({'cat', 'dog'})


def test_normalize_label_groups_canonicalizes():
    normalized = lg.normalize_label_groups({
        '  Vehicle ': ['CAR', 'car', 'Truck', 'bus '],
        'motion': ['cat'],            # reserved name dropped
        'self': ['self', 'cat'],      # self-reference dropped, 'cat' kept
        '': ['cat'],                  # empty name dropped
    })
    assert normalized == {
        'vehicle': ['bus', 'car', 'truck'],
        'self': ['cat'],
    }


def test_normalize_label_groups_non_dict_is_empty():
    assert lg.normalize_label_groups(None) == {}
    assert lg.normalize_label_groups(['animal']) == {}
    assert lg.normalize_label_groups('animal') == {}


def test_effective_uses_persisted_setting_and_allows_removal(monkeypatch):
    monkeypatch.setattr(_state, 'database', _FakeDB({
        'label_groups': {'vehicle': ['car', 'truck'], 'pet': ['cat']},
    }))
    lg.refresh_label_groups()
    groups = lg.cached_label_groups()
    assert 'vehicle' in groups
    assert groups['vehicle'] == frozenset({'car', 'truck'})
    # A persisted map is authoritative: omitting a group removes it, including
    # the built-in defaults.
    assert 'animal' not in groups
    assert groups['pet'] == frozenset({'cat'})


def test_matching_uses_custom_groups(monkeypatch):
    monkeypatch.setattr(_state, 'database', _FakeDB({
        'label_groups': {'vehicle': ['car', 'truck', 'bus']},
    }))
    lg.refresh_label_groups()

    assert label_matches('car', 'vehicle') is True
    assert label_matches('bus', 'vehicle') is True
    assert label_matches('person', 'vehicle') is False
    assert label_matches('vehicle', 'car') is False  # one-directional expansion

    assert detection_label_in_allowed('truck', {'vehicle'}) is True
    assert detection_label_in_allowed('person', {'vehicle'}) is False
    # The removed default group no longer matches.
    assert detection_label_in_allowed('cat', {'animal'}) is False


def _camera(**zone_extra):
    zone = {
        'id': 'zone-1',
        'object_rules': [{'label': 'pet', 'enabled': True}, {'label': 'person', 'enabled': True}],
        'object_labels': ['pet', 'person'],
        **zone_extra,
    }
    return {'id': 'cam-1', 'detection': {'object_labels': ['pet', 'car'], 'zones': [zone]}}


def test_rename_follows_the_group_into_zone_rules():
    camera = _camera(tripwire={'labels': ['pet'], 'name': 'Gate'}, loiter={'labels': ['person']})
    cameras, changed = lg.rename_group_references([camera], {'Pet': 'pets'})
    assert changed
    detection = cameras[0]['detection']
    zone = detection['zones'][0]
    assert detection['object_labels'] == ['pets', 'car']
    assert [rule['label'] for rule in zone['object_rules']] == ['pets', 'person']
    assert zone['object_labels'] == ['pets', 'person']
    assert zone['tripwire'] == {'labels': ['pets'], 'name': 'Gate'}
    assert zone['loiter'] == {'labels': ['person']}
    # The input is left untouched.
    assert camera['detection']['zones'][0]['object_rules'][0]['label'] == 'pet'


def test_rename_without_references_changes_nothing():
    camera = _camera()
    cameras, changed = lg.rename_group_references([camera], {'animal': 'critter'})
    assert not changed and cameras[0] is camera
    assert lg.rename_group_references([camera], {}) == ([camera], False)
    assert lg.rename_group_references([camera], {'pet': 'pet'}) == ([camera], False)
