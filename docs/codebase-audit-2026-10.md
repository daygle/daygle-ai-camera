# Codebase audit — October 2, 2026

## Scope and method

Repository-wide checks covered the Python/FastAPI application, vanilla-JS
browser dashboard, installer/updater scripts, dependency locks, and tests.
Targeted manual review focused on authentication/CSRF, trusted proxies,
recording/event visibility, biometric admin gates, backup/restore, media paths,
SQL construction, subprocess/download boundaries, and inference admission.
This is not a guarantee that every execution path is free of defects.

The starting worktree was clean. No commits, pushes, deployments, environment
secret changes, or service restarts were performed. Python 3.11 and audit tools
were installed in ignored, repository-local `.audit-*` directories; application
requirements and the generated CPU dependency lock were not changed. NumPy
2.2.6 and OpenCV 5.0.0.93 match the application's constraints. Optional ML
packages and real camera/GPU hardware were not installed or exercised.

## Confirmed findings and fixes

| Area | Finding | Resolution |
| --- | --- | --- |
| Request throttling | Global sliding-window cleanup deleted a bucket when its oldest hit expired, even if recent hits still exhausted its budget. | Evict only fully expired buckets; retain and locally prune active buckets. |
| Login backoff | Computing `2 ** excess` before capping could overflow after a large failure burst. NaN/infinite limiter settings were accepted by the primitive. | Bound the exponent before computation and reject non-finite configuration. Preserve zero-delay behavior. |
| Inference concurrency | A queued replacement could execute while the same camera was already running on another worker. | Select only non-running cameras; keep other cameras runnable. |
| Scheduler bookkeeping | A fast job could release its camera before submission published the busy flag. Completion also released cameras with pending replacements. | Publish claims before worker admission and release only when a camera is idle. |
| Scheduler fairness/lifecycle | Replacing a waiting frame moved its camera to the back of the queue; a stopped pool could still select work. Shutdown timeout was applied separately to each thread. | Keep the original queue position, check stop state at selection, and use one total join deadline. |
| Shared scheduler | Concurrent first-use calls from HTTP and background paths could create separate inference pools. | Serialize singleton creation/start/configuration. |
| Password handling | bcrypt 5 raises on passwords over 72 UTF-8 bytes or invalid stored hashes, producing an exception instead of a normal authentication failure. | Treat these as failed verification; existing failed-attempt/lockout logic still applies. |
| Origin validation | URL ports are validated lazily; malformed Origin or trusted forwarded-host values could escape the parser guard. | Fail closed on URL validation errors. |
| CSRF comparison | String `compare_digest` rejects non-ASCII input and could crash on attacker-supplied Unicode tokens. | Compare UTF-8 bytes in pre-auth and API checks. |
| Strict JSON | `parse_constant` rejected literal Infinity, but valid numeric syntax such as `1e999` still became infinity. | Reject overflow through a finite float parser, including nested values. |
| Form parsing | Invalid UTF-8 form bytes escaped as an application error. | Return HTTP 400. |
| Audit logging | Auth-disabled anonymous users have `id=None`; converting it to `int` failed before the logging guard. | Preserve a null audit user ID. |
| Database restore | Executable-schema rejection occurred only at final overwrite, after full-restore remapping and media copies. | Reuse one schema allowlist check during initial database validation, before row queries, remapping, or copies; recheck at overwrite. |
| Backup manifests | The archive-wide 100 GiB allowance also permitted an enormous in-memory JSON manifest. | Limit manifests to 1 MiB and return a client error for unsupported/encrypted/corrupt manifest reads. |
| HTML buffering | Repeated immutable byte concatenation copied already-buffered HTML for every chunk. | Collect chunks and join once. |
| Static cache hashes | Non-security SHA-1 cache-busting was classified as security hashing and can fail on restricted crypto builds. | Explicitly use `usedforsecurity=False`; URLs retain their existing format. |
| npm tooling | Locked `brace-expansion` 5.0.9 had published denial-of-service advisories. | Update only that transitive lock entry to 5.0.12, within the parent's existing range. |

Regression coverage is in `tests/test_codebase_audit_regressions.py` (31 cases),
in addition to the existing scheduler, middleware, backup, validation, and auth
suites. No detection thresholds, model formats, routing, roles, UI layout, or
GPU compatibility pins were changed.

## Verification

- Baseline backend: **2,011 passed, 7 skipped** (`--no-cov -n 4 --dist loadfile`).
- Final backend: **2,042 passed, 7 skipped**, **80.37% coverage**; the required
  60% floor passed (`python -m pytest -q -n 6 --dist loadfile`).
- Frontend: **272 passed**, no failures (`npm test`).
- Ruff: `ruff check app/ tests/test_codebase_audit_regressions.py` passed.
- ESLint: `npm run lint` passed. Audit directories are excluded to avoid
  linting third-party tooling source.
- Python compilation: `compileall -q app scripts tests` passed.
- Dependency advisories: `npm audit` reported **0 known vulnerabilities** after
  the lock update; `pip-audit -r requirements.cpu.lock.txt --no-deps
  --disable-pip` reported **no known vulnerabilities** in the committed CPU lock.
  This does not audit arbitrary future versions resolved from requirements
  ranges or the separately deployed GPU environment.
- Bandit scanned `app/` and `scripts/`: 128 initial heuristic findings, including
  one high-severity non-security SHA-1 use (addressed). SQL flags include dynamic
  static fragments and parameterized values; URL flags include admin-configured
  network endpoints and fixed upstream URLs. Scanner findings are review leads,
  not a count of proven vulnerabilities; not every finding was exhaustively
  adjudicated.
- Vulture at 95% confidence reported only an unused audio callback argument
  (`time_info`). It is part of the callback signature, not removable dead code.
  Existing shared-script exports and compatibility rebinds were retained.
- `git diff --check` passed.

## Remaining risks and follow-up validation

1. **Hardware/integration verification:** skipped optional-runtime tests and
   real RTSP/ONVIF/PTZ, sound input, ONNX inference/export, TensorFlow Lite,
   Pascal/CUDA, SMTP, push delivery, and browser visual behavior were not verified
   against live hardware/services. Passing mocked tests is not a camera soak
   test. A managed preview was not launched.
2. **Request resource limits:** generic JSON and login/setup forms still read
   complete request bodies; use reverse-proxy request-size/time limits pending a
   centrally enforced application limit. The image upload helper already has a
   streaming size cap. Large restored archives still require staging disk space.
3. **Restore atomicity:** media copying is additive but may overwrite existing
   files before the database swap. Schema rejection now precedes those copies,
   but I/O failure or concurrent runtime writes still warrants maintenance-mode,
   collision-safe staging, and rollback testing before a broader redesign.
4. **Auth transactions:** first-admin setup and last-active-admin protection
   perform checks separately from their write transactions. Cross-request races
   merit transactional tests and a focused change rather than a speculative
   rewrite during this audit.
5. **Privilege/deployment trade-offs:** the current systemd design intentionally
   allows the in-app updater capabilities that stronger filesystem/user
   isolation would deny. Changing that requires a privileged update broker and
   deployment migration; maintain the documented proxy/VPN/auth protection.
6. **Further load work:** benchmark scheduler shutdown/restart, aggressive
   priority traffic, limiter key cardinality, and multi-camera CPU/GPU latency.
   Fairness improvements here preserve order within a priority tier, not a hard
   starvation bound across priority tiers. No measured inference speedup is
   claimed.

These follow-ups are explicitly not represented as fixed or verified.
