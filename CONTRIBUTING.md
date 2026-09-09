# Contributing to Daygle AI Camera

Thanks for helping improve Daygle AI Camera! This document describes the
project's conventions: how the two test suites are organised, what CI
enforces, and the dependency policy that keeps GPU (Tesla P4 / Pascal)
deployments working.

## Development setup

```bash
git clone https://github.com/daygle/daygle-ai-camera.git
cd daygle-ai-camera
python3 -m venv .venv
source .venv/bin/activate
./scripts/install_python_deps.sh python requirements.txt
pip install --no-cache-dir pytest pytest-cov ruff pre-commit
npm install   # frontend lint/test tooling (Node.js 22+)
```

Enable the pre-commit hooks so lint failures never reach CI:

```bash
pre-commit install
```

## The two test suites

The repository carries two independent suites and CI (`.github/workflows/python-app.yml`) runs both on every push/PR:

| Suite | Command | Scope |
|---|---|---|
| Backend | `python -m compileall app && python -m pytest` | `app/` + `scripts/` behaviour, security, and API regression tests |
| Frontend | `npm run lint && npm test` | `web/` dashboard scripts via ESLint + Node's test runner |

- Backend tests use the shared harness in `tests/support.py` (`_load_app`,
  `_server`, `LocalClient`, `_setup_admin`, `_login`). New API tests follow
  the same pattern: boot the app on a uvicorn thread against a tmpdir config,
  then call through `LocalClient`.
- Frontend tests live in `tests/*.test.js` and run under
  `node --test` against the browser-vanilla `web/` scripts.
- Coverage is enforced on every pytest run via `pytest.ini`
  (`--cov-fail-under=60`). **The floor is a floor, not a target** - raise it
  deliberately as coverage grows rather than relying on the headroom.

## Lint policy

- **Python (`app/`)**: `ruff check app/` under the Pyflakes (`F`) ruleset in
  `ruff.toml`. Clean is required; new warnings are errors.
- **Python (`tests/`)**: intentionally not ruff-gated yet (the "Pool-A"
  preload pattern reads as unused imports; see the comment in `ruff.toml`).
- **JS (`web/` + `tests/`)**: ESLint recommended rules (`eslint.config.js`).
  `no-undef` is an **error** for `web/`: the cross-script globals are
  declared explicitly in `WEB_SHARED_GLOBALS` (`eslint.config.js`) - when a
  script starts using a helper from an earlier-loaded script, add the name
  there in the same PR. CI pins the remaining `no-unused-vars` baseline
  (helpers consumed only from later scripts) with `--max-warnings=51`. The
  pin decays as warnings are fixed - when a corrected file drops the count,
  tighten the pin in `.github/workflows/python-app.yml`. The long-term goal
  is module conversion, which removes the baseline entirely.

## Commit and PR conventions

- Small, focused PRs; one logical change per PR.
- Commit messages follow the Conventional Commits style already used by the
  repo (`feat:`, `fix:`, `docs:`, `chore:`, `deps(pip):`, `deps(npm):`,
  `deps(ci):` …). Dependabot opens PRs with scoped prefixes - keep them.
- Every behaviour change ships with a test. Bug fixes include a regression
  test that fails without the fix.
- CI must be green: both suites, ruff, and the coverage floor.

## Dependency policy (the two locks)

`requirements.txt` is the canonical dependency list. `requirements.cpu.lock.txt`
is a **derived** artifact - the output of
`uv pip compile requirements.txt --generate-hashes` (wrapped by
`scripts/lock_python_deps.sh`) - and it must never be hand-edited:

1. **Direct dependency change?** Edit `requirements.txt`, then regenerate the
   lock in the same PR: `./scripts/lock_python_deps.sh`. Commit both files.
2. **Dependabot PR touching only `requirements.cpu.lock.txt`?** The pip
   `ignore` rules in `.github/dependabot.yml` suppress these for the known
   lock-only transitives (torch chain: `nvidia-*`, `cuda-*`, `triton`,
   `mpmath`; FastAPI chain: `anyio`; misc: `polars*`,
   `ultralytics-platform`, `pydantic-core`). If a new lock-only PR still
   appears, add an ignore rule rather than merging a piecemeal lock edit - a
   single-pin edit outside a resolver run desyncs the lock from its
   upstream's declared constraints and can break `--require-hashes` installs.
3. **`ultralytics` bump**: it is exact-pinned
   (`ultralytics==8.4.x`) deliberately. A Dependabot PR for it is the review
   trigger, not an auto-merge: read the upstream release notes, then
   validate an A/B export byte-compare with this repo's exact export kwargs
   before merging (the comment block in `requirements.txt` documents the
   historical examples).
4. **Pascal/Tesla P4 hard stops** (`numpy`, `onnxruntime`, the
   `nvidia-*-cu12` CUDA wheels): see the mechanism notes at the top of
   `.github/dependabot.yml` and `docs/tesla-p4-gpu-setup.md`. These
   constraints exist because newer releases drop compute capability 6.1
   (sm_61) support.

## Project layout quick reference

- `app/` - FastAPI backend (routers under `app/api/`, auth middleware in
  `app/middleware.py`, shared state in `app/state.py`).
- `web/` - vanilla-JS dashboard (no bundler; classic scripts sharing
  `window` globals, loaded in a fixed per-page order; cross-script names are
  declared in `WEB_SHARED_GLOBALS` in `eslint.config.js`).
- `scripts/` - installers, updater, dependency lock generator.
- `docs/` - feature and operations runbooks (`docs/operations.md` covers
  monitoring, including the `/healthz` liveness endpoint).
- `Dockerfile` / `docker-compose.yml` - container deployment (CPU default,
  optional Pascal GPU build).
