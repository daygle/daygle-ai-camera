"""Built-in Ollama install and model management for the AI page.

The local vision model (app/ai_verification.py) talks to any OpenAI-compatible
server. This module lets an admin set one up from the web UI instead of a
shell: install Ollama as a systemd service (scripts/install_ollama.sh), start,
stop or restart it, and download, list and delete models through Ollama's
native API.

Design notes:

- The install runs one fixed, checksummed script with no arguments taken from
  the request. Model names are the only user input, and they are validated
  against ``MODEL_NAME_PATTERN`` before they reach Ollama.
- Installs and model downloads are long (1.5 GB and 2-5 GB), so each runs on
  a background thread and the page polls ``status()``. One of each at a time.
- When the app runs as root (the Debian install) commands run directly;
  otherwise through ``sudo -n``, which fails fast instead of prompting.
- A container cannot install system services; there, Ollama runs as its own
  container (docker-compose.yml) and only model management is offered.

No FastAPI imports, so it is unit-testable.
"""
from __future__ import annotations

import collections
import json
import logging
import os
import re
import shutil
import subprocess
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

logger = logging.getLogger('daygle.ai.ollama')

BASE_DIR = Path(__file__).resolve().parent.parent
INSTALL_SCRIPT = BASE_DIR / 'scripts' / 'install_ollama.sh'

OLLAMA_URL_ENV = 'DAYGLE_OLLAMA_URL'
DEFAULT_OLLAMA_URL = 'http://127.0.0.1:11434'
SERVICE_NAME = 'ollama'
UNIT_PATH = Path('/etc/systemd/system/ollama.service')
# Must match DAYGLE_MARKER in scripts/install_ollama.sh.
DAYGLE_UNIT_MARKER = '# Managed by Daygle AI Camera (scripts/install_ollama.sh)'
# Must match PINNED_VERSION in scripts/install_ollama.sh.
PINNED_VERSION = '0.40.16'

# The download alone is ~1.5 GB; allow for a slow link.
INSTALL_TIMEOUT_SECONDS = 3600
SERVICE_TIMEOUT_SECONDS = 60
API_TIMEOUT_SECONDS = 5.0
# Ollama sends a progress line at least every few seconds while pulling; a
# connection silent for this long is treated as stalled.
PULL_READ_TIMEOUT_SECONDS = 300.0
LOG_TAIL_LINES = 200

# Ollama model references: "gemma3:4b", "qwen2.5vl:3b", "library/moondream",
# "hf.co/user/repo:Q4_K_M". No spaces, quotes, or URL schemes.
MODEL_NAME_PATTERN = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._\-]*(?:/[A-Za-z0-9][A-Za-z0-9._\-]*){0,3}(?::[A-Za-z0-9][A-Za-z0-9._\-]{0,127})?$')
MAX_MODEL_NAME_LENGTH = 200

SERVICE_ACTIONS = ('start', 'stop', 'restart')

# Update checks. The latest Ollama release comes from GitHub (the same source
# the installer downloads from); its checksum comes from that release's
# sha256sum.txt. A model's newest version is its manifest on the Ollama
# registry: Ollama's local digest for a model is the SHA-256 of that
# manifest, so a different remote hash means a newer model is published.
GITHUB_LATEST_RELEASE_URL = 'https://api.github.com/repos/ollama/ollama/releases/latest'
GITHUB_RELEASE_FILE_URL = 'https://github.com/ollama/ollama/releases/download/v{version}/{name}'
OLLAMA_REGISTRY_URL = 'https://registry.ollama.ai'
UPDATE_CHECK_TTL_SECONDS = 6 * 3600
UPDATE_CHECK_TIMEOUT_SECONDS = 15.0
MANIFEST_ACCEPT = 'application/vnd.docker.distribution.manifest.v2+json'
USER_AGENT = 'daygle-ai-camera-ollama/1.0'

# Vision models offered on the AI page, smallest-memory last. ``memory_gb`` is
# the approximate GPU memory with the default quantisation.
SUGGESTED_MODELS: tuple[dict[str, Any], ...] = (
    {'name': 'gemma3:4b', 'size_gb': 3.3, 'memory_gb': 5,
     'note': 'Recommended. Good accuracy; fits beside the detectors on an 8 GB GPU.'},
    {'name': 'qwen2.5vl:3b', 'size_gb': 3.2, 'memory_gb': 5,
     'note': 'Good at reading small details and text.'},
    {'name': 'gemma3:12b', 'size_gb': 8.1, 'memory_gb': 11,
     'note': 'More accurate; needs a 12 GB+ GPU.'},
    {'name': 'moondream', 'size_gb': 1.7, 'memory_gb': 3,
     'note': 'Fastest and smallest, least accurate.'},
)


def _now() -> float:
    return time.time()


def validate_model_name(value: Any) -> str:
    """Return a cleaned model name or raise ``ValueError``."""
    if not isinstance(value, str):
        raise ValueError('Model name must be a string.')
    name = value.strip()
    if not name:
        raise ValueError('Enter a model name, e.g. gemma3:4b.')
    if len(name) > MAX_MODEL_NAME_LENGTH or not MODEL_NAME_PATTERN.match(name):
        raise ValueError('Invalid model name. Use an Ollama model name such as gemma3:4b.')
    return name


def running_in_container(environ: Mapping[str, str] | None = None) -> bool:
    env = os.environ if environ is None else environ
    if env.get('DAYGLE_IN_CONTAINER') == '1':
        return True
    return Path('/.dockerenv').exists() or Path('/run/.containerenv').exists()


def _parse_version(text: str) -> str | None:
    match = re.search(r'(\d+\.\d+\.\d+)', text or '')
    return match.group(1) if match else None


def _version_tuple(version: str | None) -> tuple[int, ...]:
    try:
        return tuple(int(part) for part in str(version or '').split('.')[:3])
    except ValueError:
        return ()


def release_asset_name(machine: str | None = None) -> str | None:
    """The release archive install_ollama.sh installs on this CPU."""
    import platform

    arch = (machine or platform.machine()).lower()
    if arch in ('x86_64', 'amd64'):
        return 'ollama-linux-amd64.tar.zst'
    if arch in ('aarch64', 'arm64'):
        return 'ollama-linux-arm64.tar.zst'
    return None


def model_manifest_path(name: str) -> str | None:
    """Registry manifest path for an Ollama model name, or ``None`` when the
    model comes from another registry (e.g. hf.co) that is not checked."""
    base, _, tag = name.partition(':')
    tag = tag or 'latest'
    parts = base.split('/')
    if len(parts) == 3 and parts[0] == 'registry.ollama.ai':
        parts = parts[1:]
    if len(parts) == 1:
        parts = ['library', parts[0]]
    if len(parts) != 2 or '.' in parts[0]:
        return None
    return f'/v2/{parts[0]}/{parts[1]}/manifests/{tag}'


def _tagged(name: str) -> str:
    return name if ':' in name else f'{name}:latest'


class UpdateSource:
    """Internet lookups for update checks (GitHub releases, Ollama registry)."""

    def _get(self, url: str, accept: str | None = None) -> bytes:
        headers = {'User-Agent': USER_AGENT}
        if accept:
            headers['Accept'] = accept
        request = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(request, timeout=UPDATE_CHECK_TIMEOUT_SECONDS) as response:  # noqa: S310 - fixed https hosts
            return response.read()

    def latest_version(self) -> str | None:
        payload = json.loads(self._get(GITHUB_LATEST_RELEASE_URL, 'application/vnd.github+json') or b'{}')
        return _parse_version(str(payload.get('tag_name') or '')) if isinstance(payload, dict) else None

    def release_checksums(self, version: str) -> dict[str, str]:
        text = self._get(GITHUB_RELEASE_FILE_URL.format(version=version, name='sha256sum.txt')).decode('utf-8', 'replace')
        checksums = {}
        for line in text.splitlines():
            match = re.match(r'^([0-9a-fA-F]{64})\s+\*?(?:\./)?(\S+)$', line.strip())
            if match:
                checksums[match.group(2)] = match.group(1).lower()
        return checksums

    def manifest_digest(self, path: str) -> str:
        import hashlib

        return hashlib.sha256(self._get(f'{OLLAMA_REGISTRY_URL}{path}', MANIFEST_ACCEPT)).hexdigest()


class OllamaHttp:
    """The few native Ollama API calls the manager needs."""

    def __init__(self, base_url: str) -> None:
        self.base_url = base_url.rstrip('/')

    def _request(self, path: str, method: str = 'GET', body: dict[str, Any] | None = None) -> urllib.request.Request:
        data = json.dumps(body).encode('utf-8') if body is not None else None
        headers = {'Content-Type': 'application/json'} if data is not None else {}
        return urllib.request.Request(f'{self.base_url}{path}', data=data, method=method, headers=headers)

    def get_json(self, path: str, timeout: float = API_TIMEOUT_SECONDS) -> dict[str, Any]:
        with urllib.request.urlopen(self._request(path), timeout=timeout) as response:  # noqa: S310 - fixed local URL
            payload = json.loads(response.read() or b'{}')
        return payload if isinstance(payload, dict) else {}

    def post_json(self, path: str, body: dict[str, Any], method: str = 'POST', timeout: float = API_TIMEOUT_SECONDS) -> dict[str, Any]:
        with urllib.request.urlopen(self._request(path, method, body), timeout=timeout) as response:  # noqa: S310
            raw = response.read()
        if not raw:
            return {}
        try:
            payload = json.loads(raw)
        except ValueError:
            return {}
        return payload if isinstance(payload, dict) else {}

    def stream_json(self, path: str, body: dict[str, Any], timeout: float = PULL_READ_TIMEOUT_SECONDS):
        """Yield each JSON line of a streamed (NDJSON) response."""
        with urllib.request.urlopen(self._request(path, 'POST', body), timeout=timeout) as response:  # noqa: S310
            for raw_line in response:
                line = raw_line.strip()
                if not line:
                    continue
                try:
                    item = json.loads(line)
                except ValueError:
                    continue
                if isinstance(item, dict):
                    yield item


Runner = Callable[..., subprocess.CompletedProcess]


class OllamaManager:
    def __init__(
        self,
        base_url: str | None = None,
        *,
        http: OllamaHttp | None = None,
        runner: Runner | None = None,
        popen: Callable[..., Any] | None = None,
        install_script: Path = INSTALL_SCRIPT,
        unit_path: Path = UNIT_PATH,
        allow_install: bool = True,
        in_container: bool | None = None,
        is_root: bool | None = None,
        which: Callable[[str], str | None] = shutil.which,
        updates: UpdateSource | None = None,
        asset_name: str | None = None,
    ) -> None:
        self.base_url = (base_url or os.environ.get(OLLAMA_URL_ENV) or DEFAULT_OLLAMA_URL).rstrip('/')
        self.http = http or OllamaHttp(self.base_url)
        self._run = runner or subprocess.run
        self._popen = popen or subprocess.Popen
        self.install_script = install_script
        self.unit_path = unit_path
        self.allow_install = allow_install
        self.in_container = running_in_container() if in_container is None else in_container
        self.is_root = (hasattr(os, 'geteuid') and os.geteuid() == 0) if is_root is None else is_root
        self._which = which
        self._lock = threading.Lock()
        self._install: dict[str, Any] = {'running': False, 'started_at': None, 'finished_at': None, 'ok': None, 'error': None}
        self._install_log: collections.deque[str] = collections.deque(maxlen=LOG_TAIL_LINES)
        self._pull: dict[str, Any] = self._pull_state(False, None, 'download', [])
        self._updates = updates or UpdateSource()
        self._asset = asset_name if asset_name is not None else release_asset_name()
        # Latest Ollama release: version, the checksum of this CPU's archive,
        # when it was checked and any error.
        self._release: dict[str, Any] = {'latest_version': None, 'sha256': None, 'checked_at': None, 'error': None}
        # Per model: the registry's current manifest digest and when it was read.
        self._remote_digests: dict[str, dict[str, Any]] = {}
        self._models_checked_at: float | None = None

    # -- helpers ---------------------------------------------------------

    def _privileged(self, command: list[str]) -> list[str]:
        return command if self.is_root else ['sudo', '-n', *command]

    def _binary(self) -> str | None:
        for candidate in ('/usr/local/bin/ollama', '/usr/bin/ollama'):
            if Path(candidate).is_file() and os.access(candidate, os.X_OK):
                return candidate
        return self._which('ollama')

    def _managed(self) -> bool:
        try:
            return DAYGLE_UNIT_MARKER in self.unit_path.read_text(encoding='utf-8')
        except OSError:
            return False

    def _installed_version(self, binary: str | None) -> str | None:
        if not binary:
            return None
        try:
            result = self._run([binary, '--version'], capture_output=True, text=True, timeout=10, check=False)
        except (OSError, subprocess.SubprocessError):
            return None
        return _parse_version(f'{result.stdout or ""}\n{result.stderr or ""}')

    def _service_state(self) -> str | None:
        if not self._which('systemctl'):
            return None
        try:
            result = self._run(['systemctl', 'is-active', SERVICE_NAME], capture_output=True, text=True, timeout=10, check=False)
        except (OSError, subprocess.SubprocessError):
            return None
        state = (result.stdout or '').strip()
        return state or None

    def install_unavailable_reason(self) -> str | None:
        if not self.allow_install:
            return 'Installing Ollama from the web UI is turned off (ollama.allow_install in config.yaml).'
        if self.in_container:
            return 'Daygle is running in a container, which cannot install system services. Run Ollama as its own container (see docker-compose.yml).'
        if os.name != 'posix' or not Path('/run/systemd/system').is_dir():
            return 'This server does not run systemd. Install Ollama manually (see docs/ai-verification.md).'
        if not self.install_script.is_file():
            return 'The install script (scripts/install_ollama.sh) is missing.'
        if not self.is_root and not self._which('sudo'):
            return 'Daygle is not running as root and sudo is not available.'
        return None

    # -- status ------------------------------------------------------------

    def api_version(self) -> str | None:
        try:
            return str(self.http.get_json('/api/version').get('version') or '') or None
        except (OSError, ValueError, urllib.error.URLError):
            return None

    def list_models(self) -> list[dict[str, Any]]:
        payload = self.http.get_json('/api/tags')
        models = []
        for item in payload.get('models') or []:
            if not isinstance(item, dict) or not item.get('name'):
                continue
            details = item.get('details') if isinstance(item.get('details'), dict) else {}
            models.append({
                'name': str(item['name']),
                'size': int(item.get('size') or 0),
                'digest': str(item.get('digest') or ''),
                'modified_at': str(item.get('modified_at') or ''),
                'parameter_size': str(details.get('parameter_size') or ''),
                'quantization': str(details.get('quantization_level') or ''),
                'family': str(details.get('family') or ''),
                'vision': self._model_has_vision(str(item['name'])),
            })
        models.sort(key=lambda model: model['name'])
        return models

    def _model_has_vision(self, name: str) -> bool | None:
        """``True``/``False`` from the model's capabilities, ``None`` if unknown."""
        try:
            info = self.http.post_json('/api/show', {'model': name})
        except (OSError, ValueError, urllib.error.URLError):
            return None
        capabilities = info.get('capabilities')
        if isinstance(capabilities, list):
            return 'vision' in capabilities
        return None

    def status(self) -> dict[str, Any]:
        binary = self._binary()
        version = self._installed_version(binary)
        api_version = self.api_version()
        reachable = api_version is not None
        managed = self._managed()
        models: list[dict[str, Any]] = []
        models_error = None
        if reachable:
            try:
                models = self.list_models()
            except (OSError, ValueError, urllib.error.URLError) as exc:
                models_error = f'Could not list models: {exc}'
        with self._lock:
            install = dict(self._install)
            install['log'] = list(self._install_log)
            pull = dict(self._pull)
            release = dict(self._release)
            remote = {name: dict(info) for name, info in self._remote_digests.items()}
            models_checked_at = self._models_checked_at
        for model in models:
            info = remote.get(_tagged(model['name']))
            if info is None:
                model['update_available'] = None
                model['update_error'] = None
            else:
                digest = info.get('digest')
                model['update_available'] = bool(digest and model['digest'] and digest != model['digest']) if not info.get('error') else None
                model['update_error'] = info.get('error')
        install_reason = self.install_unavailable_reason()
        target = self._install_target(release)
        installed_version = version or api_version
        update_available = bool(managed and installed_version and _version_tuple(installed_version) < _version_tuple(target['version']))
        return {
            'url': self.base_url,
            'openai_url': f'{self.base_url}/v1',
            'installed': bool(binary) or reachable,
            'binary': binary,
            'version': version or api_version,
            'pinned_version': PINNED_VERSION,
            'latest_version': release['latest_version'],
            'install_version': target['version'],
            'release_checked_at': release['checked_at'],
            'release_check_error': release['error'],
            'models_checked_at': models_checked_at,
            'managed': managed,
            'update_available': update_available,
            'service': self._service_state() if binary else None,
            'reachable': reachable,
            'can_install': install_reason is None and (not binary or managed),
            'install_unavailable_reason': install_reason,
            'can_control_service': bool(binary) and install_reason is None,
            'install': install,
            'pull': pull,
            'models': models,
            'models_error': models_error,
            'suggested_models': [dict(model) for model in SUGGESTED_MODELS],
        }

    # -- updates -------------------------------------------------------------

    def _install_target(self, release: dict[str, Any] | None = None) -> dict[str, Any]:
        """The version an install or update installs: the newest release
        whose checksum is known, never older than the pinned one."""
        release = release if release is not None else self._release
        latest = release.get('latest_version')
        if latest and release.get('sha256') and _version_tuple(latest) > _version_tuple(PINNED_VERSION):
            return {'version': latest, 'sha256': release['sha256']}
        return {'version': PINNED_VERSION, 'sha256': None}

    def _check_release(self) -> None:
        if not self._asset:
            with self._lock:
                self._release.update({'checked_at': _now(), 'error': 'No Ollama build for this CPU.'})
            return
        try:
            latest = self._updates.latest_version()
            if not latest:
                raise ValueError('GitHub did not report a version.')
            sha256 = None
            error = None
            if _version_tuple(latest) > _version_tuple(PINNED_VERSION):
                sha256 = self._updates.release_checksums(latest).get(self._asset)
                if not sha256:
                    error = f'Ollama {latest} has no checksum for {self._asset}; it cannot be installed from here.'
            release = {'latest_version': latest, 'sha256': sha256, 'checked_at': _now(), 'error': error}
        except Exception as exc:  # noqa: BLE001 - network, TLS, rate limit, bad JSON: all "unknown"
            logger.warning('Ollama release check failed (%s).', exc)
            with self._lock:
                self._release.update({'checked_at': _now(), 'error': 'Could not check GitHub for a newer Ollama.'})
            return
        with self._lock:
            self._release = release

    def _check_models(self) -> None:
        try:
            models = self.list_models() if self.api_version() else []
        except (OSError, ValueError, urllib.error.URLError):
            models = []
        remote: dict[str, dict[str, Any]] = {}
        for model in models:
            name = _tagged(model['name'])
            path = model_manifest_path(name)
            if path is None:
                remote[name] = {'digest': None, 'error': 'Not on the Ollama registry; not checked.'}
                continue
            try:
                remote[name] = {'digest': self._updates.manifest_digest(path), 'error': None}
            except urllib.error.HTTPError as exc:
                remote[name] = {'digest': None, 'error': 'No longer on the Ollama registry.' if exc.code == 404 else f'Registry error (HTTP {exc.code}).'}
            except Exception as exc:  # noqa: BLE001 - reported per model
                remote[name] = {'digest': None, 'error': f'Could not reach the Ollama registry ({type(exc).__name__}).'}
        with self._lock:
            self._remote_digests = remote
            self._models_checked_at = _now()

    def check_updates(self, force: bool = False) -> dict[str, Any]:
        """Look for a newer Ollama and newer versions of the installed models.

        Results are kept for ``UPDATE_CHECK_TTL_SECONDS`` unless ``force``, so
        opening the page does not query GitHub every time."""
        now = _now()
        with self._lock:
            release_checked = self._release.get('checked_at')
            models_checked = self._models_checked_at
        if force or not release_checked or now - release_checked > UPDATE_CHECK_TTL_SECONDS:
            self._check_release()
        if force or not models_checked or now - models_checked > UPDATE_CHECK_TTL_SECONDS:
            self._check_models()
        return self.status()

    # -- install -------------------------------------------------------------

    def start_install(self) -> dict[str, Any]:
        reason = self.install_unavailable_reason()
        if reason:
            raise PermissionError(reason)
        binary = self._binary()
        if binary and not self._managed():
            raise PermissionError(f'Ollama is already installed at {binary} but not by Daygle; it is left for you to manage.')
        with self._lock:
            if self._install['running']:
                raise RuntimeError('An Ollama install is already running.')
            self._install = {'running': True, 'started_at': _now(), 'finished_at': None, 'ok': None, 'error': None}
            self._install_log.clear()
        target = self._install_target()
        threading.Thread(target=self._run_install, args=(target,), name='ollama-install', daemon=True).start()
        return self.status()

    def _run_install(self, target: dict[str, Any] | None = None) -> None:
        # The version and checksum come from this server's own release check,
        # never from the request. The pinned release needs neither.
        env_args = []
        if target and target.get('sha256') and target['version'] != PINNED_VERSION:
            env_args = ['env', f'DAYGLE_OLLAMA_VERSION={target["version"]}', f'DAYGLE_OLLAMA_SHA256={target["sha256"]}']
        command = self._privileged([*env_args, 'bash', str(self.install_script)])
        logger.info('Installing Ollama: %s', ' '.join(command))
        ok = False
        error = None
        try:
            process = self._popen(
                command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                text=True, bufsize=1, cwd=str(BASE_DIR),
            )
            deadline = time.monotonic() + INSTALL_TIMEOUT_SECONDS
            for line in process.stdout:
                line = line.rstrip()
                if line:
                    with self._lock:
                        self._install_log.append(line)
                    logger.info('ollama install: %s', line)
                if time.monotonic() > deadline:
                    process.kill()
                    error = f'Install timed out after {INSTALL_TIMEOUT_SECONDS // 60} minutes.'
                    break
            returncode = process.wait(timeout=30)
            if error is None:
                ok = returncode == 0
                if not ok:
                    error = self._install_failure_message(returncode)
        except Exception as exc:  # noqa: BLE001 - reported to the UI, never raised on the worker thread
            error = f'Install failed: {exc}'
        if ok:
            logger.info('Ollama installed.')
        else:
            logger.warning('Ollama install failed: %s', error)
        with self._lock:
            self._install.update({'running': False, 'finished_at': _now(), 'ok': ok, 'error': error})

    def _install_failure_message(self, returncode: int) -> str:
        with self._lock:
            lines = list(self._install_log)
        for line in reversed(lines):
            if 'ERROR' in line or 'sudo:' in line:
                return line.removeprefix('ERROR: ')
        return f'The install script exited with status {returncode}. See the log below.'

    # -- service -------------------------------------------------------------

    def service_action(self, action: str) -> dict[str, Any]:
        if action not in SERVICE_ACTIONS:
            raise ValueError(f'Action must be one of: {", ".join(SERVICE_ACTIONS)}.')
        reason = self.install_unavailable_reason()
        if reason:
            raise PermissionError(reason)
        if not self._binary():
            raise PermissionError('Ollama is not installed.')
        try:
            result = self._run(self._privileged(['systemctl', action, SERVICE_NAME]),
                               capture_output=True, text=True, timeout=SERVICE_TIMEOUT_SECONDS, check=False)
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError(f'systemctl {action} {SERVICE_NAME} timed out.') from exc
        except OSError as exc:
            raise RuntimeError(f'Could not run systemctl: {exc}') from exc
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or '').strip().splitlines()
            raise RuntimeError(detail[-1] if detail else f'systemctl {action} {SERVICE_NAME} failed.')
        if action != 'stop':
            # Give the API a moment, so the status the page gets back is current.
            for _ in range(20):
                if self.api_version():
                    break
                time.sleep(0.5)
        return self.status()

    # -- models --------------------------------------------------------------

    @staticmethod
    def _pull_state(running: bool, model: str | None, kind: str, queue: list[str]) -> dict[str, Any]:
        return {'running': running, 'kind': kind, 'model': model, 'queue': list(queue), 'index': 1 if running else 0,
                'status': 'starting' if running else None, 'completed': 0, 'total': 0,
                'started_at': _now() if running else None, 'finished_at': None, 'ok': None, 'error': None,
                'failed': [], 'updated': []}

    def _start_pulls(self, names: list[str], kind: str) -> dict[str, Any]:
        if not self.api_version():
            raise RuntimeError('Ollama is not running, so models cannot be downloaded.')
        with self._lock:
            if self._pull['running']:
                raise RuntimeError(f'Already downloading {self._pull["model"]}. Wait for it to finish.')
            self._pull = self._pull_state(True, names[0], kind, names)
        threading.Thread(target=self._run_pulls, args=(names,), name='ollama-pull', daemon=True).start()
        return self.status()

    def start_pull(self, model: Any) -> dict[str, Any]:
        return self._start_pulls([validate_model_name(model)], 'download')

    def start_update_models(self, models: Any = None) -> dict[str, Any]:
        """Re-download installed models that have a newer version.

        ``models`` is a list of names, or ``None`` for every model the last
        update check found out of date. Pulling an installed model fetches
        only the layers that changed."""
        installed = {_tagged(model['name']): model['name'] for model in self.list_models()} if self.api_version() else {}
        if models is None:
            names = [model['name'] for model in self.status()['models'] if model.get('update_available')]
            if not names:
                raise LookupError('No model updates found. Use Check for Updates first.')
        else:
            if not isinstance(models, list) or not models or len(models) > 50:
                raise ValueError('models must be a list of model names.')
            names = []
            for value in models:
                name = validate_model_name(value)
                if _tagged(name) not in installed:
                    raise LookupError(f'Model {name} is not installed.')
                if installed[_tagged(name)] not in names:
                    names.append(installed[_tagged(name)])
        return self._start_pulls(names, 'update')

    def _run_pulls(self, names: list[str]) -> None:
        for index, name in enumerate(names, start=1):
            with self._lock:
                self._pull.update({'model': name, 'index': index, 'status': 'starting', 'completed': 0, 'total': 0})
            ok, error = self._pull_one(name)
            with self._lock:
                if ok:
                    self._pull['updated'].append(name)
                else:
                    self._pull['failed'].append({'model': name, 'error': error})
        with self._lock:
            failed = self._pull['failed']
            error = None
            if failed:
                error = failed[0]['error'] if len(names) == 1 else '; '.join(f'{item["model"]}: {item["error"]}' for item in failed)
            self._pull.update({'running': False, 'finished_at': _now(), 'ok': not failed, 'error': error})

    def _pull_one(self, name: str) -> tuple[bool, str | None]:
        logger.info('Downloading Ollama model %s', name)
        ok = False
        error = None
        # Ollama reports progress per layer ("pulling <digest>"); sum the
        # layers so the page shows one bar for the whole model.
        layers: dict[str, tuple[int, int]] = {}
        try:
            for item in self.http.stream_json('/api/pull', {'model': name, 'stream': True}):
                if item.get('error'):
                    error = str(item['error'])
                    break
                status = str(item.get('status') or '')
                digest = str(item.get('digest') or '')
                if digest and 'total' in item:
                    layers[digest] = (int(item.get('completed') or 0), int(item.get('total') or 0))
                with self._lock:
                    self._pull['status'] = status
                    if layers:
                        self._pull['completed'] = sum(done for done, _total in layers.values())
                        self._pull['total'] = sum(total for _done, total in layers.values())
                if status == 'success':
                    ok = True
        except urllib.error.HTTPError as exc:
            error = f'Ollama refused the download (HTTP {exc.code}).'
            try:
                detail = json.loads(exc.read() or b'{}').get('error')
                if detail:
                    error = str(detail)
            except (ValueError, AttributeError, OSError):
                pass
        except Exception as exc:  # noqa: BLE001 - reported to the UI
            error = f'Download failed: {exc}'
        if not ok and error is None:
            error = 'The download ended before it finished.'
        if ok:
            logger.info('Ollama model %s downloaded.', name)
        else:
            logger.warning('Ollama model %s download failed: %s', name, error)
        return ok, error

    def delete_model(self, model: Any) -> dict[str, Any]:
        name = validate_model_name(model)
        with self._lock:
            if self._pull['running'] and name in self._pull['queue']:
                raise RuntimeError('That model is still downloading.')
        try:
            self.http.post_json('/api/delete', {'model': name}, method='DELETE')
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                raise LookupError(f'Model {name} is not installed.') from exc
            raise RuntimeError(f'Ollama could not delete {name} (HTTP {exc.code}).') from exc
        except (OSError, urllib.error.URLError) as exc:
            raise RuntimeError(f'Could not reach Ollama: {exc}') from exc
        logger.info('Deleted Ollama model %s', name)
        return self.status()


_manager: OllamaManager | None = None
_manager_lock = threading.Lock()


def get_ollama_manager(config: Mapping[str, Any] | None = None) -> OllamaManager:
    """The process-wide manager, created on first use from ``config['ollama']``."""
    global _manager
    with _manager_lock:
        if _manager is None:
            section = (config or {}).get('ollama') if isinstance(config, Mapping) else None
            section = section if isinstance(section, Mapping) else {}
            _manager = OllamaManager(
                base_url=str(section.get('url') or '') or None,
                allow_install=bool(section.get('allow_install', True)),
            )
        return _manager
