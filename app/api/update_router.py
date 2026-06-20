"""Update APIRouter.

Direct imports replace the ``import app.main as main`` hybrid pattern.
"""

from __future__ import annotations

import json
import subprocess
import threading
import time
import urllib.error
import urllib.request

from fastapi import APIRouter, Depends, HTTPException, Request

from app.auth_gates import require_admin
from app.deps import get_logger
from app.main import (
    BASE_DIR,
    GITHUB_REPO,
    _current_version,
    _parse_semver,
    _update_in_progress,
    _update_lock,
)

router = APIRouter()


@router.post('/api/update/check')
def check_update(request: Request):
    require_admin(request)
    current_version = _current_version()
    try:
        req = urllib.request.Request(
            f'https://api.github.com/repos/{GITHUB_REPO}/releases/latest',
            headers={'User-Agent': 'daygle-ai-camera-updater/1.0', 'Accept': 'application/vnd.github.v3+json'},
        )
        with urllib.request.urlopen(req, timeout=10) as response:
            data = json.loads(response.read())
        tag_name = str(data.get('tag_name') or '')
        latest_version = tag_name.lstrip('v')
        update_available = bool(
            latest_version
            and current_version != 'unknown'
            and (_parse_semver(latest_version) > _parse_semver(current_version))
        )
        return {
            'current_version': current_version,
            'latest_version': latest_version,
            'tag_name': tag_name,
            'html_url': str(data.get('html_url') or ''),
            'release_notes': str(data.get('body') or ''),
            'published_at': str(data.get('published_at') or ''),
            'update_available': update_available,
        }
    except urllib.error.HTTPError as exc:
        return {
            'current_version': current_version,
            'latest_version': None,
            'update_available': False,
            'error': f'GitHub API error {exc.code}: {exc.reason}',
        }
    except Exception as exc:
        return {
            'current_version': current_version,
            'latest_version': None,
            'update_available': False,
            'error': str(exc),
        }


@router.post('/api/update/apply')
def apply_update(request: Request, logger=Depends(get_logger)):
    import app.main as _main_mod
    require_admin(request)
    with _main_mod._update_lock:
        if _main_mod._update_in_progress:
            raise HTTPException(status_code=409, detail='An update is already in progress.')
        _main_mod._update_in_progress = True
    update_script = BASE_DIR / 'scripts' / 'update.sh'
    if not update_script.exists():
        with _main_mod._update_lock:
            _main_mod._update_in_progress = False
        raise HTTPException(status_code=503, detail='Update script not found.')
    try:
        result = subprocess.run(['bash', str(update_script)], capture_output=True, text=True, timeout=300, cwd=str(BASE_DIR))
    except subprocess.TimeoutExpired:
        with _main_mod._update_lock:
            _main_mod._update_in_progress = False
        raise HTTPException(status_code=504, detail='Update timed out after 5 minutes.')
    except Exception as exc:
        with _main_mod._update_lock:
            _main_mod._update_in_progress = False
        raise HTTPException(status_code=500, detail=f'Update failed: {exc}') from exc
    output = ((result.stdout or '') + ('\n' + result.stderr if result.stderr else '')).strip()
    service_restart_scheduled = False
    if result.returncode == 0:
        check = subprocess.run(['systemctl', 'is-active', 'daygle-ai-camera'], capture_output=True, text=True, timeout=5, check=False)
        if check.returncode == 0:

            def _delayed_restart() -> None:
                time.sleep(3)
                try:
                    subprocess.run(['systemctl', 'restart', 'daygle-ai-camera'], timeout=30, check=False)
                except Exception as exc:
                    logger.warning('Service restart after update failed: %s', exc)
                finally:
                    with _main_mod._update_lock:
                        _main_mod._update_in_progress = False

            threading.Thread(target=_delayed_restart, daemon=True, name='update-restart').start()
            service_restart_scheduled = True
        else:
            with _main_mod._update_lock:
                _main_mod._update_in_progress = False
    else:
        with _main_mod._update_lock:
            _main_mod._update_in_progress = False
    return {
        'ok': result.returncode == 0,
        'output': output[-4000:],
        'returncode': result.returncode,
        'new_version': _current_version(),
        'service_restart_scheduled': service_restart_scheduled,
    }
