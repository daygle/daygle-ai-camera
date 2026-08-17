"""Unit tests for the nvidia-smi GPU reader in ``app.system_metrics``.

The reader shells out to ``nvidia-smi``, which is not present on every host,
so every test here drives the CSV parsing through a fake ``subprocess.run``
result instead of requiring a real GPU.
"""

from __future__ import annotations

import subprocess

import pytest

import app.system_metrics as system_metrics


@pytest.fixture(autouse=True)
def _reset_gpu_cache():
    """The polled ``system_resources`` path reads nvidia-smi through a TTL
    cache; clear it around every test so a snapshot from one test never leaks
    into the next (the parse/threshold tests each patch their own output)."""
    system_metrics.reset_gpu_snapshot_cache()
    yield
    system_metrics.reset_gpu_snapshot_cache()


class _FakeCompletedProcess:
    def __init__(self, returncode: int, stdout: str):
        self.returncode = returncode
        self.stdout = stdout


def _fake_run(returncode: int = 0, stdout: str = ''):
    def _run(*_args, **_kwargs):
        return _FakeCompletedProcess(returncode, stdout)

    return _run


P4_LINE = (
    "Tesla P4, 90, 100, 1531, 3505, 45.3, 75.0, 1575, 7680\n"
)


def test_parses_single_gpu_csv(monkeypatch):
    monkeypatch.setattr(system_metrics.subprocess, 'run', _fake_run(stdout=P4_LINE))
    devices = system_metrics.nvidia_smi_devices()
    assert devices is not None
    assert len(devices) == 1
    gpu = devices[0]
    assert gpu['name'] == 'Tesla P4'
    assert gpu['temperature_c'] == 90
    assert gpu['utilization_percent'] == 100
    assert gpu['graphics_clock_mhz'] == 1531
    assert gpu['memory_clock_mhz'] == 3505
    assert gpu['power_draw_watts'] == 45.3
    assert gpu['power_limit_watts'] == 75.0
    assert gpu['memory_used_mb'] == 1575
    assert gpu['memory_total_mb'] == 7680


def test_parses_multiple_gpus(monkeypatch):
    stdout = P4_LINE + "Tesla T4, 58, 12, 960, 2500, 18.2, 70.0, 512, 15360\n"
    monkeypatch.setattr(system_metrics.subprocess, 'run', _fake_run(stdout=stdout))
    devices = system_metrics.nvidia_smi_devices()
    assert devices is not None
    assert [d['name'] for d in devices] == ['Tesla P4', 'Tesla T4']


def test_na_cells_become_none(monkeypatch):
    # power.draw/power.limit can be [N/A] on cards without a power sensor.
    stdout = "Tesla P4, 62, 47, 1506, 3505, [N/A], [N/A], 1575, 7680\n"
    monkeypatch.setattr(system_metrics.subprocess, 'run', _fake_run(stdout=stdout))
    devices = system_metrics.nvidia_smi_devices()
    assert devices is not None
    assert devices[0]['power_draw_watts'] is None
    assert devices[0]['power_limit_watts'] is None
    assert devices[0]['temperature_c'] == 62


def test_missing_binary_returns_none(monkeypatch):
    def _raise(*_args, **_kwargs):
        raise FileNotFoundError('nvidia-smi')

    monkeypatch.setattr(system_metrics.subprocess, 'run', _raise)
    assert system_metrics.nvidia_smi_devices() is None
    assert system_metrics.gpu_status() is None


def test_nonzero_exit_returns_none(monkeypatch):
    monkeypatch.setattr(system_metrics.subprocess, 'run', _fake_run(returncode=1))
    assert system_metrics.nvidia_smi_devices() is None


def test_timeout_returns_none(monkeypatch):
    def _raise(*_args, **_kwargs):
        raise subprocess.TimeoutExpired('nvidia-smi', timeout=3)

    monkeypatch.setattr(system_metrics.subprocess, 'run', _raise)
    assert system_metrics.nvidia_smi_devices() is None


def test_gpu_status_thresholds(monkeypatch):
    # 90 C is the P4 throttle ceiling: critical.
    monkeypatch.setattr(system_metrics.subprocess, 'run', _fake_run(stdout=P4_LINE))
    status = system_metrics.gpu_status()
    assert status is not None
    assert status['count'] == 1
    assert status['primary']['thermal_status'] == 'critical'
    assert status['warn_temp_c'] == system_metrics.GPU_TEMP_WARN_C
    assert status['critical_temp_c'] == system_metrics.GPU_TEMP_CRITICAL_C

    # 86 C is above warn but below critical.
    monkeypatch.setattr(
        system_metrics.subprocess,
        'run',
        _fake_run(stdout=P4_LINE.replace('90, 100', '86, 100')),
    )
    assert system_metrics.gpu_status()['primary']['thermal_status'] == 'warn'

    # 62 C is fine.
    monkeypatch.setattr(
        system_metrics.subprocess,
        'run',
        _fake_run(stdout=P4_LINE.replace('90, 100', '62, 47')),
    )
    assert system_metrics.gpu_status()['primary']['thermal_status'] == 'ok'


def test_gpu_status_uses_custom_thresholds(monkeypatch):
    """Admin-tuned thresholds (e.g. a card throttling at 83 C) drive the
    status instead of the module defaults."""
    monkeypatch.setattr(system_metrics.subprocess, 'run', _fake_run(stdout=P4_LINE))
    # 90 C vs warn=80 / critical=85 -> critical under the custom set.
    status = system_metrics.gpu_status(warn_c=80, critical_c=85)
    assert status['primary']['thermal_status'] == 'critical'
    assert status['warn_temp_c'] == 80
    assert status['critical_temp_c'] == 85
    # 83 C vs warn=80 / critical=85 -> warn.
    monkeypatch.setattr(
        system_metrics.subprocess,
        'run',
        _fake_run(stdout=P4_LINE.replace('90, 100', '83, 100')),
    )
    assert system_metrics.gpu_status(warn_c=80, critical_c=85)['primary']['thermal_status'] == 'warn'
    # 79 C vs warn=80 / critical=85 -> ok.
    monkeypatch.setattr(
        system_metrics.subprocess,
        'run',
        _fake_run(stdout=P4_LINE.replace('90, 100', '79, 100')),
    )
    assert system_metrics.gpu_status(warn_c=80, critical_c=85)['primary']['thermal_status'] == 'ok'


def test_system_resources_passes_through_custom_thresholds(monkeypatch):
    """The resources endpoint's thresholds flow into the gpu payload."""
    monkeypatch.setattr(system_metrics.subprocess, 'run', _fake_run(stdout=P4_LINE))
    resources = system_metrics.system_resources(gpu_warn_c=80, gpu_critical_c=85)
    assert resources['gpu']['primary']['thermal_status'] == 'critical'
    assert resources['gpu']['warn_temp_c'] == 80
    assert resources['gpu']['critical_temp_c'] == 85


def test_system_resources_includes_gpu(monkeypatch):
    monkeypatch.setattr(system_metrics.subprocess, 'run', _fake_run(stdout=P4_LINE))
    resources = system_metrics.system_resources()
    assert resources['gpu'] is not None
    assert resources['gpu']['primary']['name'] == 'Tesla P4'


def test_system_resources_caches_nvidia_smi_across_polls(monkeypatch):
    """Rapid polls share one nvidia-smi read: the subprocess is spawned once
    within the TTL window, not once per /api/system/resources call."""
    calls = {'n': 0}

    def _counting_run(*_args, **_kwargs):
        calls['n'] += 1
        return _FakeCompletedProcess(0, P4_LINE)

    monkeypatch.setattr(system_metrics.subprocess, 'run', _counting_run)
    for _ in range(5):
        assert system_metrics.system_resources()['gpu']['primary']['name'] == 'Tesla P4'
    assert calls['n'] == 1  # one spawn for five polls inside the TTL


def test_system_resources_no_gpu_does_not_respawn(monkeypatch):
    """A host without a GPU caches the None result too, so repeated polls do
    not keep spawning nvidia-smi just to fail again."""
    calls = {'n': 0}

    def _raise(*_args, **_kwargs):
        calls['n'] += 1
        raise FileNotFoundError('nvidia-smi')

    monkeypatch.setattr(system_metrics.subprocess, 'run', _raise)
    for _ in range(5):
        assert system_metrics.system_resources()['gpu'] is None
    assert calls['n'] == 1
