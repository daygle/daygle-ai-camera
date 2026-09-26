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

### Running the backend suite locally

```bash
python -m pytest -n auto --dist loadfile
```

Two things about that command are load-bearing.

**`--dist loadfile` with `pytest-xdist`** (a dev-only extra, not a runtime
dependency) cuts a full run from roughly five minutes to under two on a small
box. Use `loadfile`, not the default `load`: most of the suite boots the app on
a uvicorn thread against a tmpdir config, and the harness in `tests/support.py`
wipes and re-imports the whole `app.*` namespace per test, so interleaving two
files onto one worker produces cross-test contamination that looks like a real
failure.

**Add `--no-cov` when running a subset.** `pytest.ini` sets
`--cov-fail-under=60`, so running one file reports a coverage failure and exits
non-zero even when every test passed. Only the full-suite run should be subject
to the floor.

### A partially-installed environment fails silently

This is the trap worth knowing about. If `fastapi`, `numpy` or `cv2` is absent,
pytest does not report a clean "you are missing dependencies" - it emits ~150
collection errors and ~120 failures that are indistinguishable at a glance from
real breakage. Code paths that merely *import* those modules never execute, so
tests covering them skip or error rather than fail loudly.

Before trusting a red suite, check that it is red for the right reason:

```bash
python -m pytest --no-cov -q --collect-only 2>&1 | tail -3   # want: "N tests collected", 0 errors
```

If collection reports errors, the environment is incomplete and no failure
count is meaningful yet. The full dependency set is in `requirements.txt` and
needs Python 3.11+ (the supported floor); `scripts/install_python_deps.sh` is
the supported installer.

### Judge a change by the delta, not the absolute count

Even with a healthy environment, the useful signal when validating a change is
how the set of failures *changed*, not how many there are:

```bash
python -m pytest --no-cov -q tests/ --continue-on-collection-errors 2>&1 \
  | grep -E "^(FAILED|ERROR)" | sed 's/ - .*//' | sort > /tmp/after.txt
git stash push -u
python -m pytest --no-cov -q tests/ --continue-on-collection-errors 2>&1 \
  | grep -E "^(FAILED|ERROR)" | sed 's/ - .*//' | sort > /tmp/before.txt
git stash pop
diff /tmp/before.txt /tmp/after.txt && echo "no regressions"
```

An identical diff proves the change introduced no regressions even when the
absolute failure count is non-zero. It does **not** prove the change works - a
new test that never runs locally still has to be checked in CI, which is the
only place the full stack is guaranteed.

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
  `no-undef` and `no-unused-vars` are both **errors** for `web/` - there is
  no warning baseline and no `--max-warnings` pin in CI. Cross-script
  globals are declared explicitly in `WEB_SHARED_GLOBALS`
  (`eslint.config.js`): when a script starts using a helper from an
  earlier-loaded script, add the name there in the same PR. A helper
  defined in one file but consumed by another carries an explicit
  "ESLint: exported for later/earlier scripts" marker at its definition.
  Deliberately-ignored catch bindings follow the `catch (_err)` convention.

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
