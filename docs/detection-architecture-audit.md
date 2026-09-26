# Detection architecture re-audit (Item 10)

**Audit date:** 2026-09-25  
**Scope:** live motion analysis, object inference, tracking, zone matching,
behavioural monitors, confirmation, recording/alert decisions, and the object
benchmark.

## Executive summary

The live detector is a staged pipeline rather than a single motion gate. The
current default runs motion analysis and object inference in parallel, then
applies tracking, per-camera object policy, zone filtering, temporal
confirmation, identity/behavioural checks, and finally event/recording
decisions. This is the correct high-recall shape for a camera that must detect
still subjects, but it also makes ordering and state isolation important.

This audit found and addressed three concrete architecture risks:

1. **Motion thumbnail geometry was process-global.** Cameras with different
   Day/Night profiles could continually clear or resize one another's MOG2 and
   Diff state, and zone scoring could reject a valid mask after another camera
   changed the global geometry. Motion size is now camera-local at the live
   call boundary; zone geometry is derived from the returned mask shape.
2. **Behavioural engines consumed raw tracked geometry during PTZ/ego-motion.**
   Tripwire, loiter, and time-of-day checks now pause while camera motion is
   active, matching the existing safe-mode treatment for object state and
   motion-only actions.
3. **The minimum-confidence cache was TTL-only and included disabled zones.**
   Per-camera cache entries now include a settings signature, so rule edits
   invalidate immediately; disabled zones no longer lower the detector floor.

These changes are covered by focused regression tests. The remaining items are
deliberately follow-up work rather than undocumented behavior changes.

## Current pipeline

1. **Frame and settings resolution** - `process_live_stream_alerts()` resolves
   effective global settings, then per-camera profile overrides, cadence, AI
   state, and motion policy.
2. **Motion analysis** - `app.detection_state.detect_frame_motion()` selects MOG2
   or legacy Diff. It owns per-camera background state, scene-reset handling,
   denoising, and camera-motion estimation. The returned mask is camera-local.
3. **Motion zone scoring** - `app.zone_detection.zone_motion_detections()`
   evaluates enabled motion zones using normalized zone geometry and the mask's
   own dimensions. Motion detections receive their own temporal confirmation.
4. **Inference gate** - with `always_run_object_detection=true` (the default),
   object inference runs even on a quiet frame. With it disabled, the object
   path is entered only for frame motion, a qualifying motion zone, or a
   periodic scan. The AI master switch can disable the whole path.
5. **Object inference** - `app.detector.OnnxYoloDetector` owns provider
   sessions, bounded concurrent inference, confidence floors, and NMS/NMS-free
   post-processing. Optional region boost and whole-frame tiling are merged and
   de-duplicated by `app.region_detection`.
6. **Face merge and normalization** - the secondary face pass is merged before
   zone filtering; boxes are normalized to the source frame.
7. **Tracking** - `app.object_tracking.update_object_tracks()` assigns stable
   same-label IoU-associated track IDs and displacement history before policy
   filtering.
8. **Object policy** - `app.object_settings.annotate_motion_states()` classifies
   detections as moving/still/unknown. Per-label moving/still filters and
   still-dwell candidates consume the annotated list. Camera motion makes the
   state unknown and prevents dwell accumulation.
9. **Zones and suppression** - object detections are matched to enabled object
   zones and allowed labels. Concrete objects suppress only unrelated motion
   detections; object rules and motion rules remain independent axes.
10. **Temporal confirmation** - object labels are confirmed over the configured
    N-of-M window, with optional spatial persistence, before becoming actionable.
11. **Behaviour and identity** - tripwire, loiter, and time-of-day monitors are
    isolated best-effort engines. They run only when camera motion is clear.
    Face identity and notification decisions occur after the object path.
12. **Action and persistence** - alert/record decisions, event debounce,
    recording continuity, status telemetry, and bounded history persistence are
    applied to the confirmed detections.

`app.inference_scheduler.LiveInferenceScheduler` sits around inference work:
one job per camera, newest-frame-wins replacement, fair priority, and bounded
concurrency. This prevents slow cameras from building an unbounded stale-frame
queue.

## Invariants and known-good coverage

- Motion analysis fails closed: decode or processing failure is not evidence of
  motion.
- MOG2 and Diff models self-heal when their per-camera signature or thumbnail
  shape changes.
- Motion masks are not treated as object detections; object boxes are
  authoritative only after object policy and zone matching.
- Tracking runs before moving/still filtering, so a subject that pauses can
  retain a `moving` track annotation.
- Camera motion suppresses motion-only actions and makes object movement state
  unknown rather than inventing subject movement.
- Disabled zones and disabled rules are excluded from their respective
  consumers.
- Object and motion temporal confirmation are separate.
- The scheduler is shared by background and foreground live detection paths.

Relevant tests include `tests/test_motion_shape_guard.py`,
`tests/test_motion_pipeline.py`, `tests/test_frame_motion.py`,
`tests/test_detection_confirmation.py`, `tests/test_object_tracking.py`,
`tests/test_live_track_ordering.py`, `tests/test_zone_detection.py`,
`tests/test_live_alert_audit_fixes.py`, and profile/settings validation tests.

## Prioritized follow-ups

All five follow-ups raised by this audit are now implemented. What each one
does and where it lives:

### P1 - benchmark represents the production decision path (DONE)

`scripts/evaluate_detection.py --post-pipeline` now replays the full decision
path in the same order as `process_live_stream_alerts`: per-label rule floors →
tracking → camera/zone scope → N-of-M confirmation → the post-zone action
decision. `--camera-config` supplies a real camera settings object (the
persisted `detection` block), so zone geometry and the alert/record decision are
measured rather than assumed; without it the run reports
`zone_policy_replayed: false` and says so in the human output instead of
implying coverage it did not measure. `--label-thresholds` defaults to the
camera's own per-label rule floors, so the default run measures the camera as
actually configured.

`--scenarios` replays the same clip under BOTH operating modes and reports each
separately, because their recall differs by construction: a subject that never
trips the pixel gate is invisible in motion-gated mode and visible in
always-on mode. Sizing a floor from one blended number hides exactly the
regression that matters.

The base `benchmark` block is unchanged and remains the stable detector-quality
metric, so a change to alerting policy cannot silently rewrite model-quality
history; the tuned result lives in `post_pipeline_benchmark`, and the CI
quality gates can target either.

### P1 - confidence-floor invalidation across settings writers (DONE)

`app.alert_dispatch.invalidate_min_rule_confidence_cache()` is now called by
every writer that can change a rule: `apply_cameras_settings` (which covers the
single-camera PUT, the bulk list PUT, and any caller that publishes settings)
and the profile monitor's persist. The gap was real: per-camera entries
self-invalidate via their settings signature, but the GLOBAL (no-camera) form
has no settings argument to hash, so a rule edit or a profile switch left it
serving the previous floor for up to the 5 s TTL.

Note on scope: a Day/Night profile switch does not by itself move a rule
floor. Profile mode dicts carry tuning fields (confirmation window, tiling,
always-on, motion tuning), not zones. What a switch changes is the settings
dict the hot path hashes, and the profile monitor persists straight to the
database without going through the API - which is why it has to clear the
global cache itself.

`tests/test_confidence_floor_settings_boundary.py` covers the API boundary
rather than the in-memory dict: single-camera save, bulk save, profile switch,
zone disable, and cross-camera isolation, with every read going through
persistence and reload so the hot path receives freshly constructed settings
dicts. The database double implements `_settings_cache_gen`, so
`app.config_facades` exercises its real caching path instead of the
always-rebuild path a plain double would take.

### P2 - behavioral pause/resume semantics (DONE)

`app.behaviour_monitor.sync_behaviour_pause(camera_id, active)` makes the
suppression explicit and resets per-camera TRANSITIONAL state on BOTH the onset
and the resume edge: loiter presence, the time-of-day daily tally, and
tripwire per-track cooldowns. Learned baselines and remaining cooldowns are
deliberately preserved, so a 0.4 s PTZ nudge cannot erase an hour of learning
or let the same anomaly re-fire the moment the camera settles.
`clear_behavioural_state` additionally drops the pause flag, so a
removed-and-re-added camera starts unsuppressed.

Without this, a pan moves every tracked box in image space and the first
post-motion sample could read a pre-pan visit as a long loiter, or a pan-induced
step as a line crossing.

### P2 - end-to-end architecture telemetry (DONE)

`app/detection_telemetry.py` records, per camera and per cycle: the inference
mode (`always_on` / `motion_gated` / `skipped_no_motion` /
`skipped_no_detector` / `error`), whether camera motion was active and why, how
many candidates survived each stage (`detected`, `after_motion_mode`,
`after_camera_filter`, `after_confirmation`, `alertable`), and how many were
rejected by `motion_mode`, `camera_scope`, `zone`, `confirmation`, or
`camera_motion`. Both a bounded rolling window of raw cycles and process-lifetime
totals are kept, so "what just happened" and "how does this camera normally
behave" are both answerable.

Exposed at `GET /api/live/detection-telemetry` (optionally per camera).
Recording is a dict append under one lock and never raises; telemetry is pruned
in the same pass that clears a removed camera's motion state.

### P3 - documentation examples reconciled (DONE)

`docs/motion-detection.md` now leads with the always-on timeline (the default)
and presents the motion-gated timeline as the explicit opt-in mode it is. The
Layer 2 overview and the "first frame after startup" note were corrected to stop
implying a quiet frame skips YOLO, and the periodic scan is called out as
load-bearing ONLY in motion-gated mode.

## Verification record

The following checks passed in the current environment:

- Python bytecode compilation for `app`, `scripts`, and `tests`;
- Ruff lint checks for all touched Python modules and tests;
- whitespace/diff consistency checks.

Focused pytest execution was attempted but collection is blocked by the
sandbox image's missing runtime dependencies (`numpy` and `fastapi`; the full
backend dependency set is also not installed). The added tests are intended to
run in the normal project environment alongside the existing motion, tracking,
zone, profile, confirmation, and benchmark suites.
