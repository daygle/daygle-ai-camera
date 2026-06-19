"""Update APIRouter.

Extracted from ``app/main.py`` lines 3254-3317 (Phase 12 of the
hybrid-pattern router split). Same template as ``app/api/live_router.py``
(Phase 10) and ``app/api/admin_router.py`` (Phase 11): ``import app.main
as main`` at module level, every global / helper read through
``main.<name>`` *inside* handler bodies.

Handlers moved (2):

- POST /api/update/check -- ``check_update`` (admin-only GitHub latest-release probe)
- POST /api/update/apply -- ``apply_update`` (admin-only ``scripts/update.sh`` launcher + delayed service restart)

BODY-REWRITE NOTE
Handlers in the original ``main.py`` referenced module-level state in
main.py via bare names (``require_admin``, ``_update_lock``,
``_update_in_progress``, ``_current_version``, ``GITHUB_REPO``,
``_parse_semver``, ``BASE_DIR``, ``logger``). After extraction to this
router, those bare names resolve to ZERO attributes in our namespace --
handlers would NameError at request time. Per hybrid-pattern uniformity
(rule 5 of ``app/api/__init__.py``), each bare call is rewritten as
``main.<bare>``.

MUTABLE-STATE NOTE
``apply_update`` toggles the module-level ``_update_in_progress`` flag
under ``_update_lock``. Original ``main.py`` did this via::

    global _update_in_progress
    ...
    _update_in_progress = True
    with _update_lock:
        ...
        _update_in_progress = False

In the router we keep the SAME pair of assignments but substitute direct
``main.<attr>`` mutation for the ``global`` keyword (the global declaration
itself is unnecessary because assigning to ``main._update_in_progress``
already mutates the module attribute on ``app.main`` -- this is the
hybrid-pattern idiom for module-level state mutation across extraction
boundaries; cf. Phase 9 settings_system_router's
``main.database.set_setting(...)`` writes reading through ``main.<bare>``
reads). The same substitution is applied inside the nested
``_delayed_restart`` closure.

The nested ``_delayed_restart`` function stays inside ``apply_update``
verbatim -- it's a closure over the outer handler's local scope, and
moving it to module-level would change its behavior (the closure
captures no locals today, so it could in principle be lifted, but a
verbatim mirror is the safer refactor and what Phase-N past practice
favors).

STD-LIB NOTE
``apply_update`` spawns a subprocess via ``subprocess.run`` and uses
``threading.Thread`` for the delayed restart. ``check_update`` uses
``urllib.request`` + ``urllib.error`` + ``json`` for the GitHub release
probe. These are Python stdlib modules, NOT app state -- they should be
imported directly at the top of this router file (not proxied through
``main``). The hybrid pattern explicitly keeps stdlib imports local.

Helpers KEPT on ``app.main`` (the router calls them via ``main.<name>``):

- ``main.require_admin`` -- admin gate shared across admin-only handlers.
- ``main._update_lock`` -- threading.Lock() at module level that
  serialises ``apply_update`` POSTs.
- ``main._update_in_progress`` -- module-level bool flag toggled under
  the lock to indicate an in-flight update (returned in 409 if locked,
  cleared on completion).
- ``main._current_version`` -- pure helper that reads ``VERSION`` from
  the repo root.
- ``main.GITHUB_REPO`` -- str constant naming the upstream GitHub repo
  whose ``releases/latest`` endpoint ``check_update`` probes.
- ``main._parse_semver`` -- pure helper that converts ``vX.Y.Z`` to a
  comparable tuple for the ``update_available`` computation.
- ``main.BASE_DIR`` -- module-level Path to the repo root; used to
  resolve ``scripts/update.sh``.
- ``main.logger`` -- module-level logger; ``_delayed_restart`` emits a
  warning there if the post-update ``systemctl restart`` fails.

Tests go through ``LocalClient.request`` rather than calling
``main.check_update`` / ``main.apply_update`` directly, so no
back-compat alias on ``app.main`` is needed. The Phase 7.1 invariant
``tests/test_api_router_split_invariants.py::test_app_api_imports_in_main_are_consumed``
will catch any orphan-import regression if a future refactor drops the
``from app.api.update_router import router as update_router`` rebind
line in ``app/main.py``.
"""

from __future__ import annotations

import json
import subprocess
import threading
import time
import urllib.error
import urllib.request

from fastapi import APIRouter, HTTPException, Request

import app.main as main

router = APIRouter()


@router.post('/api/update/check')
def check_update(request: Request):
    main.require_admin(request)
    current_version = main._current_version()
    try:
        req = urllib.request.Request(
            f'https://api.github.com/repos/{main.GITHUB_REPO}/releases/latest',
            headers={'User-Agent': 'daygle-ai-camera-updater/1.0', 'Accept': 'application/vnd.github.v3+json'},
        )
        with urllib.request.urlopen(req, timeout=10) as response:
            data = json.loads(response.read())
        tag_name = str(data.get('tag_name') or '')
        latest_version = tag_name.lstrip('v')
        update_available = bool(
            latest_version
            and current_version != 'unknown'
            and (main._parse_semver(latest_version) > main._parse_semver(current_version))
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
def apply_update(request: Request):
    main.require_admin(request)
    with main._update_lock:
        if main._update_in_progress:
            raise HTTPException(status_code=409, detail='An update is already in progress.')
        main._update_in_progress = True
    update_script = main.BASE_DIR / 'scripts' / 'update.sh'
    if not update_script.exists():
        with main._update_lock:
            main._update_in_progress = False
        raise HTTPException(status_code=503, detail='Update script not found.')
    try:
        result = subprocess.run(['bash', str(update_script)], capture_output=True, text=True, timeout=300, cwd=str(main.BASE_DIR))
    except subprocess.TimeoutExpired:
        with main._update_lock:
            main._update_in_progress = False
        raise HTTPException(status_code=504, detail='Update timed out after 5 minutes.')
    except Exception as exc:
        with main._update_lock:
            main._update_in_progress = False
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
                    main.logger.warning('Service restart after update failed: %s', exc)
                finally:
                    with main._update_lock:
                        main._update_in_progress = False

            threading.Thread(target=_delayed_restart, daemon=True, name='update-restart').start()
            service_restart_scheduled = True
        else:
            with main._update_lock:
                main._update_in_progress = False
    else:
        with main._update_lock:
            main._update_in_progress = False
    return {
        'ok': result.returncode == 0,
        'output': output[-4000:],
        'returncode': result.returncode,
        'new_version': main._current_version(),
        'service_restart_scheduled': service_restart_scheduled,
    }
