"""Update APIRouter.
"""

from __future__ import annotations

import json
import logging
import subprocess
import threading
import time
import urllib.error
import urllib.request

from fastapi import APIRouter, Depends, HTTPException, Request

import app.state as _state

from app.auth_gates import require_admin
from app.deps import get_logger
from app.model_management import BASE_DIR, _parse_semver
from app.utils import _current_version

GITHUB_REPO = 'daygle/daygle-ai-camera'

router = APIRouter()
logger = logging.getLogger('daygle.ai')


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
        logger.warning('Update check failed with HTTP status %s.', exc.code)
        return {
            'current_version': current_version,
            'latest_version': None,
            'update_available': False,
            'error': 'Unable to check for updates.',
        }
    except Exception as exc:
        logger.warning('Update check failed (%s).', type(exc).__name__)
        return {
            'current_version': current_version,
            'latest_version': None,
            'update_available': False,
            'error': 'Unable to check for updates.',
        }


@router.post('/api/update/apply')
def apply_update(request: Request, logger=Depends(get_logger)):
    require_admin(request)
    with _state._update_lock:
        if _state._update_in_progress:
            raise HTTPException(status_code=409, detail='An update is already in progress.')
        _state._update_in_progress = True
    update_script = BASE_DIR / 'scripts' / 'update.sh'
    if not update_script.exists():
        with _state._update_lock:
            _state._update_in_progress = False
        raise HTTPException(status_code=503, detail='Update script not found.')
    try:
        result = subprocess.run(['bash', str(update_script)], capture_output=True, text=True, timeout=300, cwd=str(BASE_DIR))
    except subprocess.TimeoutExpired as exc:
        with _state._update_lock:
            _state._update_in_progress = False
        raise HTTPException(status_code=504, detail='Update timed out after 5 minutes.') from exc
    except Exception as exc:
        with _state._update_lock:
            _state._update_in_progress = False
        logger.warning('Update apply failed (%s).', type(exc).__name__)
        raise HTTPException(status_code=500, detail=f'Update failed: {exc}') from exc
    # NOTE: the flag is intentionally NOT cleared here on the success path.
    # When a service restart is scheduled below, the flag must stay set until
    # the delayed-restart thread finishes -- otherwise the 3-second window
    # between this response and the restart would accept a second concurrent
    # apply_update() racing the imminent service restart. Every non-restart
    # exit path (timeout, failure, success-without-restart) clears it
    # explicitly; BaseException subclasses that skip the handlers above leave
    # the flag set until process restart, which is the safe direction for an
    # update-in-progress guard.
    output = ((result.stdout or '') + ('\n' + result.stderr if result.stderr else '')).strip()
    service_restart_scheduled = False
    if result.returncode == 0:
        # ``_current_version()`` caches its result for the process lifetime;
        # check_update (or any earlier caller) has already populated that cache
        # with the PRE-update tag. Reset it so the response below and any
        # subsequent version display reflect the freshly pulled code.
        import app.utils as _utils
        _utils._cached_version = None
        # ``systemctl`` may not exist (non-systemd host / containers) or may
        # hang; neither should turn an ALREADY-SUCCESSFUL update into an
        # HTTP 500. Any probe failure simply means "no automatic restart".
        try:
            check = subprocess.run(['systemctl', 'is-active', 'daygle-ai-camera'], capture_output=True, text=True, timeout=5, check=False)
            service_active = check.returncode == 0
        except FileNotFoundError:
            logger.info('Update applied; systemctl not available - skipping automatic restart.')
            service_active = False
        except subprocess.TimeoutExpired:
            logger.warning('systemctl is-active timed out - skipping automatic restart.')
            service_active = False
        if service_active:

            def _delayed_restart() -> None:
                time.sleep(3)
                try:
                    subprocess.run(['systemctl', 'restart', 'daygle-ai-camera'], timeout=30, check=False)
                except Exception as exc:
                    logger.warning('Service restart after update failed: %s', exc)
                finally:
                    with _state._update_lock:
                        _state._update_in_progress = False

            threading.Thread(target=_delayed_restart, daemon=True, name='update-restart').start()
            service_restart_scheduled = True
        else:
            with _state._update_lock:
                _state._update_in_progress = False
    else:
        with _state._update_lock:
            _state._update_in_progress = False
    return {
        'ok': result.returncode == 0,
        'output': output[-4000:],
        'returncode': result.returncode,
        'new_version': _current_version(),
        'service_restart_scheduled': service_restart_scheduled,
    }
