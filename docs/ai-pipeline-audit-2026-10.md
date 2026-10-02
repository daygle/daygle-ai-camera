# AI pipeline audit — October 2, 2026

## Scope

Reviewed ONNX AI settings, the OpenAI-compatible vision client, description
parsing/storage, AI recording tags, zone tag rules, object-alert verification,
background catch-up/backfill, natural-language search, notification handoff,
and their dashboard controls. Existing codebase-audit changes were preserved.
No model/provider changes, external service installs, credentials, commits,
deployments, or GPU/YOLO tuning changes were made.

Focused baseline: 193 passing AI tests. Fixes add 35 backend cases in
`tests/test_ai_pipeline_audit.py` and a frontend form regression.

## Findings addressed

### AI model client and validation

- Reject malformed host/port/IPv6 URLs and credentials embedded in server URLs
  with HTTP 400 instead of accepting unusable settings or throwing parser errors.
- Empty/null/object/number chat content no longer counts as a successful reply.
  A connection test with no readable snapshot no longer claims vision success.
- Malformed structured description replies and JSON error objects are not stored
  as captions. Plain-text captions and valid fenced description JSON remain supported.
- Ignore non-finite/invalid object boxes when preparing focus crops. Malformed
  confidence values cannot crash label ordering; string `"false"` cannot enable
  alert verification.
- Serialize actual model completion calls across live AI jobs, search, tests,
  catch-up and backfill. Previously checking whether the live pool was idle did
  not prevent another thread starting a request immediately afterward. Admission
  waits are bounded by the request timeout. This prevents app-generated overlap,
  not work retained by a server after an HTTP timeout.

### AI descriptions and tags

- Deduplicate concurrent description work per database/event; refresh stale job
  rows and reuse already-persisted captions. Claims are always released.
- Do not run tag rules on an event deleted while the model was answering or after
  description persistence failed.
- Preserve raw model tags transiently for tag-rule evaluation while continuing
  to hide redundant detected-label chips in stored/displayed descriptions.
  Example: a detected cat can now match a **Tags only: cat** policy.
- Cameras with tag rules receive whole-scene descriptions: a close-up around a
  small bird must not hide a parcel in another area.
- Copy persisted AI tags when recordings are created/linked after description
  completion. This closes the timing-dependent gap between asynchronous clip
  creation and tagging, including events appended to existing clips. Real
  detection labels retain precedence over AI labels.

### AI in Zones and AI Alerts

- Polygon bounding rectangles no longer imply full-frame coverage. Full-frame
  polygon recognition is conservative; other polygons retain their mask.
- Check detection geometry for overlapping/renamed zones rather than relying
  solely on the first persisted zone name. Legacy rows without usable geometry
  retain name/ID fallback.
- Independent tag confirmation uses the configured zone image, masking everything
  outside polygons. A parcel elsewhere in the scene is no longer affirmative
  evidence for the chosen area. Cache verdicts by image region and term.
- Normalize rule string booleans correctly and cap cooldowns at the UI's 86,400 s
  maximum. Match description plurals symmetrically, including batteries/boxes.
- Isolate each rule's errors so one bad area does not prevent other policies.
  Unexpected model-confirmation exceptions fail open as documented.
- A rejected notification queue admission does not spend the AI tag cooldown.

### Verification and notification reliability

- Add a fail-open boundary around AI jobs: unexpected settings/database/client
  failures cannot silently discard object notifications in the worker pool.
- AI queue creation/submission errors fall back to the normal notification queue,
  as queue saturation already did.
- Preserve per-rule confidence skipping, the three-label cap, rejection of only
  opted-in labels, and exclusion of face/motion/sound alerts. Normal notification
  delivery remains bounded and can still reject work when its own queue is full.

### Catch-up, backfill, search, and settings

- Backfill filters camera scope and **Alerts only/All events** eligibility before
  applying its event limit, and rechecks scope while running. Backfill never
  fires historical tag alerts.
- Catch-up does not mark out-of-scope events attempted before eligibility, so a
  later scope change can caption them. Naive stored timestamps are interpreted
  as UTC; invalid/future dates cannot produce late/future tag notifications.
- Reset running flags when background thread startup fails.
- Partial ONNX settings saves preserve inference-thread/concurrency tuning;
  explicit empty values still clear it as before.
- An empty API-key field is now submitted so a previously saved key can actually
  be removed.
- Natural-language search retains parameterized FTS/LIKE queries, sanitized
  terms, owner scoping, time/camera constraints, and keyword fallback. It shares
  bounded model admission rather than starting a simultaneous completion.

## Verification

- Focused AI backend checks: **228 passed**.
- Full backend with coverage: **2,077 passed, 7 skipped**, **80.56% coverage**;
  required 60% floor passed (`pytest -q -n 6 --dist loadfile`).
- Frontend suite: **273 passed**.
- Ruff for `app/` and the new regression file; ESLint; Python compilation; and
  whitespace/diff checks pass.
- Tests include fake OpenAI-compatible HTTP servers, actual SQLite tag/link
  persistence, OpenCV crop/mask checks, deterministic concurrency boundaries,
  notification fallback, and dashboard payload behavior.

## Preserved limitations / operational follow-ups

- No live Ollama/llama.cpp model, physical camera, GPU, SMTP, push service, or
  browser preview was available for visual/quality/latency verification. Passing
  tests cannot guarantee accurate classifications or an error-free model.
- Zone confirmation is still language-model evidence, not detector localization.
  It intentionally fails open if confirmation is unavailable and remains labelled
  **unconfirmed**. The description must first name the configured term; this is
  event-driven analysis, not continuous scanning for arbitrary tag objects.
- Whole-frame captions can miss distant objects. Region masking reduces wrong-area
  confirmation but does not establish recall. Benchmark real images before using
  tag alerts for safety-critical decisions.
- A running background completion cannot be preempted by an alert. A waiting
  alert may therefore bypass AI after its admission timeout. Priority scheduling
  of all model consumers and server-side cancellation are possible future work.
- Description storage and recording-tag insertion are not one transaction; a
  storage failure can leave a caption without recording tags. Notification
  delivery is queue acceptance/best effort, not durable exactly-once delivery.
- API keys remain in admin-only settings responses as before; this audit fixed
  clearing and rejected URL-embedded credentials, but did not redesign secret
  storage or the settings API contract.
- Existing tag cooldown state is process-local. Duplicate delivery protection
  across restarts and durable outbox retries require a dedicated persistence
  design rather than an incidental change to AI verification.
