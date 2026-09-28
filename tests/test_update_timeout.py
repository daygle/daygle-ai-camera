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
