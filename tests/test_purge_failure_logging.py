"""Retention purge failures must be visible, not buried at debug level."""

from __future__ import annotations

import logging

import pytest

import app.backup as backup
import app.state as state

PURGES = [
    (backup.purge_camera_diagnostics_by_policy, 'Camera diagnostics purge failed'),
    (backup.purge_ai_diagnostics_by_policy, 'AI diagnostics purge failed'),
    (backup.purge_face_identities_by_policy, 'Face identity purge failed'),
    (backup.purge_unknown_faces_by_policy, 'Unknown-face capture purge failed'),
]


class _BrokenDatabase:
    def __getattr__(self, name):
        def fail(*_args, **_kwargs):
            raise RuntimeError('database is locked')

        return fail


@pytest.mark.parametrize('purge, message', PURGES)
def test_a_failing_purge_is_logged_as_a_warning(purge, message, monkeypatch, caplog):
    monkeypatch.setattr(state, 'database', _BrokenDatabase())
    monkeypatch.setattr(backup, 'effective_recording_config', lambda: {'retention_days': 14})
    monkeypatch.setattr(backup, 'effective_face_recognition_config', lambda: {'retention_days': 30})
    with caplog.at_level(logging.DEBUG, logger='daygle.ai.storage'):
        assert purge() == 0
    records = [record for record in caplog.records if message in record.getMessage()]
    assert len(records) == 1
    assert records[0].levelno == logging.WARNING
    assert 'database is locked' in records[0].getMessage()


@pytest.mark.parametrize('purge, message', PURGES)
def test_no_database_yet_is_not_a_failure(purge, message, monkeypatch, caplog):
    monkeypatch.setattr(state, 'database', None)
    with caplog.at_level(logging.DEBUG, logger='daygle.ai.storage'):
        assert purge() == 0
    assert not [record for record in caplog.records if message in record.getMessage()]
