"""The web updater gives update.sh long enough to finish GPU dependency work.

Regression: a 5-minute cap cut update.sh off inside the Pascal CUDA repair,
after nvidia-cudnn-cu13 had been removed (taking the shared libcudnn*.so.9
with it) and before the pinned cu12 wheel was reinstalled. The next restart
then ran detection on the CPU.
"""
from __future__ import annotations

import importlib
import subprocess

import pytest
from fastapi import HTTPException


def test_update_script_gets_a_long_timeout(monkeypatch):
    router = importlib.import_module('app.api.update_router')
    state = importlib.import_module('app.state')
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(kwargs)
        raise subprocess.TimeoutExpired(cmd, kwargs['timeout'])

    monkeypatch.setattr(router, 'require_admin', lambda _request: None)
    monkeypatch.setattr(router.subprocess, 'run', fake_run)
    monkeypatch.setattr(state, '_update_in_progress', False)
    with pytest.raises(HTTPException) as exc:
        router.apply_update(object(), logger=router.logger)
    assert calls[0]['timeout'] == router.UPDATE_TIMEOUT_SECONDS >= 1800
    assert exc.value.status_code == 504 and '30 minutes' in exc.value.detail
    assert state._update_in_progress is False


def test_systemctl_probe_os_error_does_not_leave_update_locked(monkeypatch, tmp_path):
    """A successful update whose ``systemctl is-active`` probe raises an OSError
    other than FileNotFoundError (e.g. PermissionError under a hardened unit)
    must still return normally and clear the in-progress flag; otherwise every
    later update attempt is refused with 409 until the process restarts."""
    router = importlib.import_module('app.api.update_router')
    state = importlib.import_module('app.state')
    (tmp_path / 'scripts').mkdir()
    (tmp_path / 'scripts' / 'update.sh').write_text('true\n')

    def fake_run(cmd, **kwargs):
        if cmd[0] == 'systemctl':
            raise PermissionError('denied')
        return subprocess.CompletedProcess(cmd, 0, stdout='updated', stderr='')

    monkeypatch.setattr(router, 'BASE_DIR', tmp_path)
    monkeypatch.setattr(router, 'require_admin', lambda _request: None)
    monkeypatch.setattr(router, '_current_version', lambda: '9.9.9')
    monkeypatch.setattr(router.subprocess, 'run', fake_run)
    monkeypatch.setattr(state, '_update_in_progress', False)
    result = router.apply_update(object(), logger=router.logger)
    assert result['ok'] is True
    assert result['service_restart_scheduled'] is False
    assert state._update_in_progress is False
