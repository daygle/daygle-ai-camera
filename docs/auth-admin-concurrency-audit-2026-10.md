# Authentication administration concurrency audit — October 16, 2026

## Scope and findings

Follow-up review of the first-admin bootstrap and administrator-management
transactions documented as unresolved in `docs/auth-audit-2026-10.md`.

- **First-admin bootstrap race (high impact):** two concurrent setup POSTs could
  both observe an empty user table and create administrators. Setup now delegates
  to `AuthService.create_initial_admin`, which takes a SQLite `BEGIN IMMEDIATE`
  write lock before checking for existing users and inserting the first admin.
  Only the request that commits the initial user succeeds; later requests are
  rejected as already complete.
- **Last active administrator race (high impact):** two concurrent admin updates
  could each observe two active admins and demote/deactivate one, leaving none.
  `update_user` now serializes the authoritative role/status read, active-admin
  count, update, and session revocation in one immediate SQLite write transaction.
  This retains the existing last-admin protection and ensures it is based on
  current state rather than a pre-transaction snapshot.

No user-management API shape, role policy, password rules, or non-concurrent
behavior was intentionally changed. The compatibility `_active_admin_count`
helper remains available.

## Regression coverage

`tests/test_auth_admin_concurrency_audit.py` uses two independent `AuthService`
instances against a shared SQLite file and synchronized threads to verify that
only one first-admin bootstrap succeeds and concurrent last-admin demotions leave
exactly one active administrator. Existing last-admin tests cover ordinary
admin demotion, deactivation, permitted demotion with another active admin,
password-only update, and inactive-admin handling.

## Verification

- Full backend suite: **2,393 passed, 11 skipped** (`pytest -q -n 6 --dist
  loadfile --no-cov`). A serial run exceeded the environment's five-minute
  command limit; the parallel run completed successfully.
- Targeted auth/zone/proxy suite: **49 passed**.
- Frontend suite: **360 passed**; ESLint passed.
- Targeted Ruff checks, Python `compileall`, and `git diff --check` passed.
- `npm audit --audit-level=low`: no known vulnerabilities.
- `pip-audit -r requirements.cpu.lock.txt --no-deps --disable-pip`: no known
  vulnerabilities in the committed CPU lock.

## Remaining audit notes

- Repository-wide Ruff still reports four existing lint findings in
  `tests/test_api_auth_settings.py`, `tests/test_live_alert_audit_fixes.py`,
  and `tests/test_zone_people_rules.py`; these unrelated test hygiene findings
  were not changed during this focused follow-up.
- Bandit completed with **0 high, 81 medium, and 69 low heuristic findings**;
  most reported network calls, argument-list subprocess usage, SQL assembled from
  fixed internal clauses, intentional best-effort exception handlers, and
  configuration defaults. These are review leads, not proven vulnerabilities;
  no untriaged report is described as fixed by this change.
- Vulture reported one unused sound callback parameter (`time_info`), retained
  because it is part of the callback interface.
- No physical cameras, GPU, live model server, production database, or multi-day
  deployment soak was available. The transaction tests exercise SQLite locking
  with local threads, not process-level chaos or production filesystem latency.
