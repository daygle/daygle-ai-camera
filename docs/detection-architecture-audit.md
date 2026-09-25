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

1. **Frame and settings resolution** — `process_live_stream_alerts()` resolves
   effective global settings, then per-camera profile overrides, cadence, AI
   state, and motion policy.
2. **Motion analysis** — `app.detection_state.detect_frame_motion()` selects MOG2
   or legacy Diff. It owns per-camera background state, scene-reset handling,
   denoising, and camera-motion estimation. The returned mask is camera-local.
3. **Motion zone scoring** — `app.zone_detection.zone_motion_detections()`
   evaluates enabled motion zones using normalized zone geometry and the mask's
   own dimensions. Motion detections receive their own temporal confirmation.
4. **Inference gate** — with `always_run_object_detection=true` (the default),
   object inference runs even on a quiet frame. With it disabled, the object
   path is entered only for frame motion, a qualifying motion zone, or a
   periodic scan. The AI master switch can disable the whole path.
5. **Object inference** — `app.detector.OnnxYoloDetector` owns provider
   sessions, bounded concurrent inference, confidence floors, and NMS/NMS-free
   post-processing. Optional region boost and whole-frame tiling are merged and
   de-duplicated by `app.region_detection`.
6. **Face merge and normalization** — the secondary face pass is merged before
   zone filtering; boxes are normalized to the source frame.
7. **Tracking** — `app.object_tracking.update_object_tracks()` assigns stable
   same-label IoU-associated track IDs and displacement history before policy
   filtering.
8. **Object policy** — `app.object_settings.annotate_motion_states()` classifies
   detections as moving/still/unknown. Per-label moving/still filters and
   still-dwell candidates consume the annotated list. Camera motion makes the
   state unknown and prevents dwell accumulation.
9. **Zones and suppression** — object detections are matched to enabled object
   zones and allowed labels. Concrete objects suppress only unrelated motion
   detections; object rules and motion rules remain independent axes.
10. **Temporal confirmation** — object labels are confirmed over the configured
    N-of-M window, with optional spatial persistence, before becoming actionable.
11. **Behaviour and identity** — tripwire, loiter, and time-of-day monitors are
    isolated best-effort engines. They run only when camera motion is clear.
    Face identity and notification decisions occur after the object path.
12. **Action and persistence** — alert/record decisions, event debounce,
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

### P1 — make the benchmark represent the production decision path

`scripts/evaluate_detection.py` measures base detection, optional region/tile
inference, motion timing, and labeled object quality. It does not yet replay
tracking, zone matching, N-of-M confirmation, or the post-zone action decision.
Add a versioned post-pipeline prediction mode with scenario breakdowns for
motion-gated and always-on operation. Keep the current base metrics as the
stable detector benchmark so changes to alerting policy do not silently rewrite
model-quality history.

### P1 — explicitly test confidence-floor invalidation across settings writers

Per-camera rule edits now invalidate through a settings signature. Add an
integration test at the settings/profile API boundary (including a profile
switch and a zone disable) to prove the same behavior when settings arrive via
persistence and reload, not only when a test mutates the in-memory dict.

### P2 — define behavioral pause/resume semantics

Camera motion now prevents new behavioral observations. The next design step is
an explicit pause token or state reset at motion onset/resume so old track
presence cannot influence the first post-motion loiter/time-of-day sample. The
current behavior is conservative: it emits no behavior event during the motion
window.

### P2 — expand end-to-end architecture telemetry

Record, per camera and cycle, whether inference was always-on or motion-gated,
whether camera motion was active, how many candidates survived each stage, and
whether a candidate was rejected by zone, motion mode, or confirmation. This
will make future regressions measurable without relying on log-line correlation.

### P3 — reconcile remaining benchmark and documentation examples

The motion guide has been corrected to describe parallel default motion/object
signals. Remaining timeline examples that describe quiet-frame YOLO skips
should either be explicitly labeled “motion-gated mode” or updated to show the
always-on default.

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
