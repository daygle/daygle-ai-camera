"""Built-in Ollama install and model management (app/ollama_manager.py,
app/api/ollama_router.py)."""
from __future__ import annotations

import io
import subprocess
import time
import urllib.error
from pathlib import Path

import pytest

import app.ollama_manager as om
from app.ollama_manager import DAYGLE_UNIT_MARKER, OllamaManager, validate_model_name
from tests.support import LocalClient, _load_app, _login, _server, _setup_admin


class FakeHttp:
    def __init__(self, reachable: bool = True) -> None:
        self.reachable = reachable
        self.models: list[dict] = []
        self.capabilities: dict[str, list[str]] = {}
        self.pull_lines: list[dict] = []
        self.deleted: list[str] = []
        self.pull_bodies: list[dict] = []

    def get_json(self, path, timeout=5.0):
        if not self.reachable:
            raise urllib.error.URLError('refused')
        if path == '/api/version':
            return {'version': '0.40.16'}
        if path == '/api/tags':
            return {'models': self.models}
        raise AssertionError(path)

    def post_json(self, path, body, method='POST', timeout=5.0):
        if path == '/api/show':
            return {'capabilities': self.capabilities.get(body['model'], ['completion'])}
        if path == '/api/delete':
            assert method == 'DELETE'
            if body['model'] not in {model['name'] for model in self.models}:
                raise urllib.error.HTTPError('u', 404, 'not found', {}, io.BytesIO(b'{}'))
            self.deleted.append(body['model'])
            self.models = [model for model in self.models if model['name'] != body['model']]
            return {}
        raise AssertionError(path)

    def stream_json(self, path, body, timeout=300.0):
        assert path == '/api/pull'
        self.pull_bodies.append(body)
        yield from self.pull_lines


def _completed(stdout='', returncode=0, stderr=''):
    return subprocess.CompletedProcess([], returncode, stdout=stdout, stderr=stderr)


def _manager(tmp_path: Path, http: FakeHttp | None = None, *, managed=False, binary=None, runner=None, **kwargs) -> OllamaManager:
    unit = tmp_path / 'ollama.service'
    if managed:
        unit.write_text(f'{DAYGLE_UNIT_MARKER}\n[Service]\n', encoding='utf-8')
    script = tmp_path / 'install_ollama.sh'
    script.write_text('#!/bin/bash\n', encoding='utf-8')
    manager = OllamaManager(
        'http://127.0.0.1:11434', http=http or FakeHttp(), runner=runner or (lambda *a, **k: _completed('active\n')),
        install_script=script, unit_path=unit, in_container=kwargs.pop('in_container', False),
        is_root=kwargs.pop('is_root', True), which=kwargs.pop('which', lambda name: f'/usr/bin/{name}'),
        **kwargs,
    )
    manager._binary = lambda: binary  # type: ignore[method-assign]
    return manager


def _wait(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError('timed out')


@pytest.mark.parametrize('name', ['gemma3:4b', 'qwen2.5vl:3b', 'moondream', 'library/llava:7b', 'hf.co/user/repo:Q4_K_M'])
def test_valid_model_names(name):
    assert validate_model_name(f' {name} ') == name


@pytest.mark.parametrize('name', ['', ' ', 'a b', 'gemma3:4b; rm -rf /', '../etc', 'http://x/y', '-flag', 'a' * 201, 5, None])
def test_invalid_model_names(name):
    with pytest.raises(ValueError):
        validate_model_name(name)


def test_status_not_installed_offers_install(tmp_path, monkeypatch):
    monkeypatch.setattr(om.Path, 'is_dir', lambda self: True)
    manager = _manager(tmp_path, FakeHttp(reachable=False))
    status = manager.status()
    assert status['installed'] is False
    assert status['reachable'] is False
    assert status['can_install'] is True
    assert status['openai_url'] == 'http://127.0.0.1:11434/v1'
    assert status['suggested_models'][0]['name'] == 'gemma3:4b'


def test_install_refused_in_container_and_when_disabled(tmp_path):
    container = _manager(tmp_path, in_container=True)
    assert 'container' in container.install_unavailable_reason()
    assert container.status()['can_install'] is False
    with pytest.raises(PermissionError):
        container.start_install()
    disabled = _manager(tmp_path, allow_install=False)
    assert 'allow_install' in disabled.install_unavailable_reason()


def test_install_refused_over_an_unmanaged_ollama(tmp_path, monkeypatch):
    monkeypatch.setattr(om.Path, 'is_dir', lambda self: True)
    manager = _manager(tmp_path, binary='/usr/bin/ollama', runner=lambda *a, **k: _completed('ollama version is 0.30.0'))
    status = manager.status()
    assert status['installed'] is True
    assert status['managed'] is False
    assert status['can_install'] is False
    assert status['update_available'] is False  # not ours to update
    with pytest.raises(PermissionError, match='not by Daygle'):
        manager.start_install()


def test_managed_old_version_offers_update(tmp_path, monkeypatch):
    monkeypatch.setattr(om.Path, 'is_dir', lambda self: True)
    manager = _manager(tmp_path, managed=True, binary='/usr/local/bin/ollama',
                       runner=lambda cmd, **k: _completed('ollama version is 0.30.1' if '--version' in cmd else 'active'))
    status = manager.status()
    assert status['version'] == '0.30.1'
    assert status['update_available'] is True
    assert status['can_install'] is True
    assert status['service'] == 'active'


class FakePopen:
    calls: list[list[str]] = []

    def __init__(self, command, **kwargs):
        FakePopen.calls.append(command)
        self.stdout = io.StringIO('>>> Downloading\nERROR: checksum mismatch for x; nothing was installed.\n')

    def wait(self, timeout=None):
        return 1

    def kill(self):
        pass


def test_install_runs_script_and_reports_failure(tmp_path, monkeypatch):
    monkeypatch.setattr(om.Path, 'is_dir', lambda self: True)
    FakePopen.calls = []
    manager = _manager(tmp_path, popen=FakePopen, is_root=False)
    manager.start_install()
    _wait(lambda: not manager.status()['install']['running'])
    install = manager.status()['install']
    assert install['ok'] is False
    assert install['error'] == 'checksum mismatch for x; nothing was installed.'
    assert install['log'][0] == '>>> Downloading'
    # Not root: the script goes through non-interactive sudo, with no arguments.
    assert FakePopen.calls == [['sudo', '-n', 'bash', str(tmp_path / 'install_ollama.sh')]]


def test_service_action_validates_and_runs_systemctl(tmp_path, monkeypatch):
    monkeypatch.setattr(om.Path, 'is_dir', lambda self: True)
    calls = []

    def runner(cmd, **kwargs):
        calls.append(cmd)
        return _completed('active')

    manager = _manager(tmp_path, binary='/usr/local/bin/ollama', runner=runner)
    with pytest.raises(ValueError):
        manager.service_action('enable; reboot')
    manager.service_action('restart')
    assert ['systemctl', 'restart', 'ollama'] in calls


def test_service_action_reports_systemctl_error(tmp_path, monkeypatch):
    monkeypatch.setattr(om.Path, 'is_dir', lambda self: True)
    manager = _manager(tmp_path, binary='/usr/local/bin/ollama',
                       runner=lambda cmd, **k: _completed(returncode=5, stderr='Unit ollama.service not found.'))
    with pytest.raises(RuntimeError, match='not found'):
        manager.service_action('start')


def test_models_listed_with_vision_flag(tmp_path):
    http = FakeHttp()
    http.models = [
        {'name': 'smollm:135m', 'size': 91_000_000, 'details': {'parameter_size': '135M', 'quantization_level': 'Q4_0'}},
        {'name': 'gemma3:4b', 'size': 3_300_000_000, 'details': {}},
    ]
    http.capabilities = {'gemma3:4b': ['completion', 'vision']}
    models = _manager(tmp_path, http).status()['models']
    assert [model['name'] for model in models] == ['gemma3:4b', 'smollm:135m']
    assert models[0]['vision'] is True
    assert models[1]['vision'] is False
    assert models[1]['parameter_size'] == '135M'


def test_pull_sums_layer_progress_and_succeeds(tmp_path):
    http = FakeHttp()
    http.pull_lines = [
        {'status': 'pulling manifest'},
        {'status': 'pulling aaa', 'digest': 'aaa', 'total': 1000, 'completed': 500},
        {'status': 'pulling bbb', 'digest': 'bbb', 'total': 10, 'completed': 10},
        {'status': 'pulling aaa', 'digest': 'aaa', 'total': 1000, 'completed': 1000},
        {'status': 'success'},
    ]
    manager = _manager(tmp_path, http)
    manager.start_pull('gemma3:4b')
    _wait(lambda: not manager.status()['pull']['running'])
    pull = manager.status()['pull']
    assert pull['ok'] is True
    assert (pull['completed'], pull['total']) == (1010, 1010)
    assert http.pull_bodies == [{'model': 'gemma3:4b', 'stream': True}]


def test_pull_error_and_validation(tmp_path):
    http = FakeHttp()
    http.pull_lines = [{'status': 'pulling manifest'}, {'error': 'pull model manifest: file does not exist'}]
    manager = _manager(tmp_path, http)
    with pytest.raises(ValueError):
        manager.start_pull('bad name')
    manager.start_pull('nope:1b')
    _wait(lambda: not manager.status()['pull']['running'])
    pull = manager.status()['pull']
    assert pull['ok'] is False
    assert 'does not exist' in pull['error']


def test_pull_needs_a_running_server(tmp_path):
    with pytest.raises(RuntimeError, match='not running'):
        _manager(tmp_path, FakeHttp(reachable=False)).start_pull('gemma3:4b')


def test_delete_model(tmp_path):
    http = FakeHttp()
    http.models = [{'name': 'gemma3:4b', 'size': 1}]
    manager = _manager(tmp_path, http)
    assert manager.delete_model('gemma3:4b')['models'] == []
    assert http.deleted == ['gemma3:4b']
    with pytest.raises(LookupError):
        manager.delete_model('gemma3:4b')


def test_install_script_pins_version_and_checksums():
    script = (Path(__file__).resolve().parents[1] / 'scripts' / 'install_ollama.sh').read_text(encoding='utf-8')
    assert f'PINNED_VERSION="{om.PINNED_VERSION}"' in script
    assert f'DAYGLE_MARKER="{DAYGLE_UNIT_MARKER}"' in script
    assert 'OLLAMA_HOST=${LISTEN_ADDRESS}' in script and 'LISTEN_ADDRESS="127.0.0.1:11434"' in script
    assert 'sha256sum --check' in script
    assert 'ollama.com/install.sh' not in script.replace('# Ollama\'s own install.sh', '')


def test_ollama_api_is_admin_only_and_validates(tmp_path, monkeypatch):
    app, _database_path = _load_app(tmp_path, monkeypatch)
    http = FakeHttp()
    http.models = [{'name': 'gemma3:4b', 'size': 1}]
    fake = _manager(tmp_path, http, in_container=True)
    # _load_app re-imports the app package; patch the module the router uses.
    import sys
    monkeypatch.setattr(sys.modules['app.ollama_manager'], '_manager', fake)
    server, thread, base_url = _server(app)
    client = LocalClient(base_url)
    try:
        status, _headers, _body = client.request('/api/settings/ollama')
        assert status in (401, 403)
        _setup_admin(client)
        csrf = _login(client)
        headers = {'X-CSRF-Token': csrf}
        status, _headers, body = client.request('/api/settings/ollama')
        assert status == 200
        assert body['reachable'] is True and body['can_install'] is False
        status, _headers, body = client.request('/api/settings/ollama/install', method='POST', json_body={}, headers=headers)
        assert status == 403
        assert 'container' in body['detail']
        status, _headers, body = client.request('/api/settings/ollama/models/pull', method='POST', json_body={'model': 'x y'}, headers=headers)
        assert status == 400
        status, _headers, body = client.request('/api/settings/ollama/models/delete', method='POST', json_body={'model': 'gemma3:4b'}, headers=headers)
        assert status == 200
        assert body['models'] == []
        status, _headers, body = client.request('/api/settings/ollama/models/delete', method='POST', json_body={'model': 'gemma3:4b'}, headers=headers)
        assert status == 404
    finally:
        server.should_exit = True
        thread.join(timeout=5)
