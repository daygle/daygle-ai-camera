// Shared canvas overlay utilities used by live.js, recordings.js, and timeline.js.

// ── Performance caches ─────────────────────────────────────────────────────
// Caches the 2D context so getContext('2d') is called only once per canvas.
const _ctxCache = new WeakMap();
// Tracks which contexts have had their font/textBaseline/lineWidth set up.
// Invalidated when the canvas is resized (which resets context state).
const _fontSetupCache = new WeakSet();
// Cache of measureText widths keyed by the display label string.
// Labels like "Person 85%" are repeated across frames, so measuring once
// avoids the expensive text-layout pass on every single frame.
const _textWidthCache = new Map();

function _getCachedContext(canvas) {
  let ctx = _ctxCache.get(canvas);
  if (!ctx) {
    ctx = canvas.getContext('2d');
    if (ctx) _ctxCache.set(canvas, ctx);
  }
  return ctx;
}

function _ensureFontSetup(ctx) {
  if (!_fontSetupCache.has(ctx)) {
    ctx.font = '12px Inter, ui-sans-serif, system-ui, sans-serif';
    ctx.textBaseline = 'middle';
    ctx.lineWidth = 2;
    _fontSetupCache.add(ctx);
  }
}

function _measureTextWidth(ctx, text) {
  let w = _textWidthCache.get(text);
  if (w === undefined) {
    w = ctx.measureText(text).width;
    _textWidthCache.set(text, w);
  }
  return w;
}
// ── End caches ─────────────────────────────────────────────────────────────

function normalizeDetectionBox(box, frameWidth, frameHeight) {
  const rawX = Number(box?.x ?? 0);
  const rawY = Number(box?.y ?? 0);
  const rawWidth = Number(box?.width ?? 0);
  const rawHeight = Number(box?.height ?? 0);
  if (!Number.isFinite(rawX) || !Number.isFinite(rawY) || !Number.isFinite(rawWidth) || !Number.isFinite(rawHeight)) {
    return null;
  }
  if (rawWidth <= 0 || rawHeight <= 0) return null;
  // Detector returns [0,1] normalized coords; only divide if clearly pixel-space.
  if (rawX <= 1 && rawY <= 1 && rawWidth <= 1 && rawHeight <= 1) {
    return { x: rawX, y: rawY, width: rawWidth, height: rawHeight };
  }
  if (frameWidth <= 0 || frameHeight <= 0) return null;
  return {
    x: Math.max(0, Math.min(1, rawX / frameWidth)),
    y: Math.max(0, Math.min(1, rawY / frameHeight)),
    width: Math.max(0, Math.min(1, rawWidth / frameWidth)),
    height: Math.max(0, Math.min(1, rawHeight / frameHeight)),
  };
}

// Intersection-over-union of two normalized boxes. Returns 0 when they don't
// overlap. Used to correspond detections across samples by actual overlap
// rather than center proximity, which is more stable when boxes change size.
function boxIoU(a, b) {
  if (!a || !b) return 0;
  const ax2 = a.x + a.width;
  const ay2 = a.y + a.height;
  const bx2 = b.x + b.width;
  const by2 = b.y + b.height;
  const ix = Math.max(0, Math.min(ax2, bx2) - Math.max(a.x, b.x));
  const iy = Math.max(0, Math.min(ay2, by2) - Math.max(a.y, b.y));
  const inter = ix * iy;
  if (inter <= 0) return 0;
  const union = a.width * a.height + b.width * b.height - inter;
  return union > 0 ? inter / union : 0;
}

// Finds the detection in `candidates` that best corresponds to `target`:
// same label, then highest box overlap (IoU). When nothing overlaps — e.g. a
// fast-moving object whose boxes don't intersect between samples — it falls
// back to the nearest box center. This keeps the correspondence (and velocity
// estimates) stable when several objects of the same label are present, instead
// of always matching the first one in the list.
function matchDetection(candidates, target) {
  if (!Array.isArray(candidates) || !candidates.length) return null;
  const targetLabel = String(target?.label || '').toLowerCase();
  const targetBox = target?.box;
  let best = null;
  let bestIoU = 0;
  let nearest = null;
  let nearestDist = Infinity;
  for (const candidate of candidates) {
    if (String(candidate?.label || '').toLowerCase() !== targetLabel) continue;
    if (!candidate?.box || !targetBox) {
      if (!nearest) nearest = candidate;
      continue;
    }
    const iou = boxIoU(candidate.box, targetBox);
    if (iou > bestIoU) {
      bestIoU = iou;
      best = candidate;
    }
    const dx = (candidate.box.x + candidate.box.width / 2) - (targetBox.x + targetBox.width / 2);
    const dy = (candidate.box.y + candidate.box.height / 2) - (targetBox.y + targetBox.height / 2);
    const dist = dx * dx + dy * dy;
    if (dist < nearestDist) {
      nearestDist = dist;
      nearest = candidate;
    }
  }
  return best || nearest;
}

// Projects detections from where they were observed (`curTime`) to a target
// time, using the per-unit velocity measured between the two most recent
// samples. All three time arguments must share one clock: the video's
// currentTime (seconds) for recorded playback, or a monotonic wall clock
// (milliseconds) for the live snapshot stream.
//
// Unlike interpolateDetections, this is anchored to *when each sample was
// captured* rather than when it arrived, so it compensates for inference
// latency: the box is placed where the object should be on the frame that is
// actually on screen right now, not where it was when the detector last ran.
// `maxLead` caps how far past the latest sample we extrapolate so a stale box
// (object gone, video paused/seeked) does not drift away.
function projectDetections(prevDetections, curDetections, prevTime, curTime, targetTime, maxLead) {
  if (!Array.isArray(curDetections) || !curDetections.length) return curDetections;
  if (!Array.isArray(prevDetections) || !prevDetections.length) return curDetections;
  if (![prevTime, curTime, targetTime].every((value) => Number.isFinite(value))) return curDetections;
  const interval = curTime - prevTime;
  if (!(interval > 0)) return curDetections;
  let lead = targetTime - curTime;
  if (lead <= 0) return curDetections;
  const cap = Number.isFinite(maxLead) ? maxLead : interval;
  if (lead > cap) lead = cap;
  const factor = lead / interval;
  return curDetections.map((curDet) => {
    const prevDet = matchDetection(prevDetections, curDet);
    if (!prevDet?.box || !curDet?.box) return curDet;
    const project = (a, b) => Math.max(0, Math.min(1, b + (b - a) * factor));
    return {
      ...curDet,
      box: {
        x: project(prevDet.box.x, curDet.box.x),
        y: project(prevDet.box.y, curDet.box.y),
        width: project(prevDet.box.width, curDet.box.width),
        height: project(prevDet.box.height, curDet.box.height),
      },
    };
  });
}

// Returns the detections for a baked detection track at playback time `t`
// (seconds), using velocity-aware interpolation between the two surrounding
// samples so the overlay follows objects smoothly. `track` is the array
// produced server-side: [{ t, detections: [{ label, confidence, box }] }],
// assumed sorted ascending by t.
//
// Unlike pure linear interpolation, this estimates the velocity between the
// previous and next samples, then uses that velocity to project forward from
// the previous sample. When the previous-previous sample is available, it
// factors in the acceleration trend so boxes don't systematically lag behind
// accelerating motion (which happens with sparse 2-3s sample intervals).
function sampleTrackAtTime(track, t) {
  if (!Array.isArray(track) || !track.length) return [];
  const time = Number.isFinite(t) ? t : 0;
  const last = track[track.length - 1];
  // A truncated track (decode stopped before the end of the clip) must not
  // freeze its final box on screen for the rest of playback: hold the last
  // sample for ~a few sample intervals only, then stop drawing.
  const spacing = track.length > 1 ? (last.t - track[0].t) / (track.length - 1) : 0;
  const maxHold = Math.max(1, spacing * 3);
  if (time > last.t + maxHold) return [];
  // Symmetrically, a track whose first sample falls mid-clip (the monitor only
  // sampled around the event) must not back-fill that box over the whole
  // pre-roll: hold it for ~a few sample intervals before its time, then
  // nothing — those earlier frames were never analyzed.
  if (time < track[0].t - maxHold) return [];
  if (time <= track[0].t) return track[0].detections || [];
  if (time >= last.t) return last.detections || [];
  let lo = 0;
  let hi = track.length - 1;
  while (lo + 1 < hi) {
    const mid = (lo + hi) >> 1;
    if (track[mid].t <= time) lo = mid; else hi = mid;
  }
  let prev = track[lo];
  let next = track[hi];
  // Detectors routinely miss an object for a single cycle. Without bridging,
  // each empty sample blinks the box off and back on, which reads as the
  // overlay "not following" the object. Bridge short gaps by interpolating
  // straight across to the next sample that has detections again.
  const bridgeWindow = Math.max(1.2, spacing * 3);
  if (!(next.detections || []).length && (prev.detections || []).length) {
    for (let k = hi + 1; k < track.length && track[k].t - prev.t <= bridgeWindow; k++) {
      if ((track[k].detections || []).length) { next = track[k]; break; }
    }
  } else if (!(prev.detections || []).length && (next.detections || []).length) {
    for (let k = lo - 1; k >= 0 && next.t - track[k].t <= bridgeWindow; k--) {
      if ((track[k].detections || []).length) { prev = track[k]; break; }
    }
  }
  const span = next.t - prev.t;
  if (!(span > 0)) return next.detections || [];

  // Base linear interpolation factor.
  const linearFactor = (time - prev.t) / span;
  if (linearFactor <= 0) return prev.detections || [];

  // Compute an acceleration-adjusted factor using the previous-previous
  // sample. When an object is accelerating, linear interpolation undervalues
  // the forward motion (putting the box behind). When decelerating, linear
  // interpolation overvalues it (box ahead). By comparing the prev→next
  // velocity with the prev2→prev velocity, we can nudge the factor to follow
  // the acceleration trend, reducing systematic lag.
  let accelFactor = linearFactor;
  const prevIdx = track.indexOf(prev);
  if (prevIdx >= 1) {
    const prev2 = track[prevIdx - 1];
    const prev2Span = prev.t - prev2.t;
    if (prev2Span > 0) {
      // We compute a per-sample acceleration ratio by comparing the bounding
      // box area velocities of prev2→prev vs prev→next. This is coarse but
      // works across all detections in both samples without per-label matching.
      const prev2Area = _sampleTotalBoxArea(prev2.detections);
      const prevArea = _sampleTotalBoxArea(prev.detections);
      const nextArea = _sampleTotalBoxArea(next.detections);
      const v2 = (prevArea - prev2Area) / prev2Span;
      const v1 = (nextArea - prevArea) / span;
      // Only apply if both velocities are in the same direction (both positive
      // or both negative) to avoid instability from noise or occlusion.
      if (v1 * v2 > 0) {
        const accelRatio = Math.max(0.5, Math.min(2.0, v1 / v2));
        accelFactor = Math.max(0, Math.min(1, linearFactor * accelRatio));
      }
    }
  }

  // Blend 70% linear + 30% acceleration-adjusted to reduce overshoot.
  const factor = linearFactor * 0.7 + accelFactor * 0.3;

  return (next.detections || []).map((nextDet) => {
    const prevDet = matchDetection(prev.detections || [], nextDet);
    if (!prevDet?.box || !nextDet?.box) return nextDet;
    const lerp = (a, b) => Math.max(0, Math.min(1, a + (b - a) * factor));
    return {
      ...nextDet,
      box: {
        x: lerp(prevDet.box.x, nextDet.box.x),
        y: lerp(prevDet.box.y, nextDet.box.y),
        width: lerp(prevDet.box.width, nextDet.box.width),
        height: lerp(prevDet.box.height, nextDet.box.height),
      },
    };
  });
}

// Helper: sum of bounding box areas across all detections in a track sample.
// Used by sampleTrackAtTime to estimate whether objects are growing (getting
// closer) or shrinking (moving away) across sample intervals.
function _sampleTotalBoxArea(sample) {
  if (!Array.isArray(sample)) return 0;
  let total = 0;
  for (const det of sample) {
    if (det?.box) {
      const w = Math.max(0, Number(det.box.width) || 0);
      const h = Math.max(0, Number(det.box.height) || 0);
      total += w * h;
    }
  }
  return total;
}

// Dimensions are read once per frame by resizeOverlayCanvas and reused by
// drawDetectionBoxesOnCanvas to avoid triggering forced layout twice.
let _lastKnownDims = null;

function resizeOverlayCanvas(canvas, referenceEl) {
  if (!canvas || !referenceEl) return;
  const dpr = window.devicePixelRatio || 1;
  const cssWidth = Math.max(1, referenceEl.clientWidth);
  const cssHeight = Math.max(1, referenceEl.clientHeight);
  _lastKnownDims = { cssWidth, cssHeight, dpr };
  const w = Math.max(1, Math.round(cssWidth * dpr));
  const h = Math.max(1, Math.round(cssHeight * dpr));
  if (canvas.width !== w || canvas.height !== h) {
    canvas.width = w;
    canvas.height = h;
    // Canvas resize resets context state (font, transform, etc.), so mark
    // the font setup as needing re-application on the next draw.
    const ctx = _getCachedContext(canvas);
    if (ctx) _fontSetupCache.delete(ctx);
  }
}

function _readElementDims(referenceEl) {
  // Use cached dimensions from resizeOverlayCanvas (called first per frame)
  // to avoid a second forced-layout read of clientWidth/clientHeight.
  if (_lastKnownDims) {
    const dims = _lastKnownDims;
    _lastKnownDims = null; // Consume once so stale values aren't reused
    return dims;
  }
  // Fallback if drawDetectionBoxesOnCanvas is called standalone (live.js path)
  return {
    cssWidth: Math.max(1, referenceEl.clientWidth),
    cssHeight: Math.max(1, referenceEl.clientHeight),
    dpr: window.devicePixelRatio || 1,
  };
}

// Draws detection bounding boxes onto canvas. referenceEl is the <video> or <img>
// whose display size and intrinsic dimensions are used for coordinate mapping.
// Assumes the canvas has already been cleared by the caller.
function drawDetectionBoxesOnCanvas(canvas, detections, referenceEl) {
  if (!canvas || !referenceEl || !detections?.length) return;
  const ctx = _getCachedContext(canvas);
  if (!ctx) return;
  // Font is set once per context lifetime (resized canvases re-trigger it via
  // _fontSetupCache invalidation in resizeOverlayCanvas).
  _ensureFontSetup(ctx);

  // Reuse dimensions cached by resizeOverlayCanvas (called earlier in the
  // same frame) instead of reading clientWidth/clientHeight again.
  const { cssWidth, cssHeight, dpr } = _readElementDims(referenceEl);
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);

  const srcW = Math.max(1, Number(referenceEl.videoWidth || referenceEl.naturalWidth || cssWidth));
  const srcH = Math.max(1, Number(referenceEl.videoHeight || referenceEl.naturalHeight || cssHeight));
  const scale = Math.min(cssWidth / srcW, cssHeight / srcH);
  const renderWidth = srcW * scale;
  const renderHeight = srcH * scale;
  const offsetX = (cssWidth - renderWidth) / 2;
  const offsetY = (cssHeight - renderHeight) / 2;

  for (const detection of detections) {
    const box = detection?.box || detection || {};
    const x = Math.min(Math.max(Number(box.x ?? 0), 0), 1);
    const y = Math.min(Math.max(Number(box.y ?? 0), 0), 1);
    const w = Math.min(Math.max(Number(box.width ?? 0), 0), 1);
    const h = Math.min(Math.max(Number(box.height ?? 0), 0), 1);
    if (w <= 0 || h <= 0) continue;
    const dx = offsetX + x * renderWidth;
    const dy = offsetY + y * renderHeight;
    const dw = w * renderWidth;
    const dh = h * renderHeight;
    if (dw < 2 || dh < 2) continue;

    ctx.strokeStyle = '#49e6a3';
    ctx.strokeRect(dx, dy, dw, dh);

    const confidence = Math.round(Number(detection.confidence || 0) * 100);
    const label = `${String(detection.label || 'object')} ${confidence}%`;
    // measureText is cached per unique label string — it's the most expensive
    // per-frame canvas operation. Labels like "Person 85%" repeat across
    // frames, so we measure once and reuse.
    const textWidth = _measureTextWidth(ctx, label);
    const labelHeight = 20;
    const labelWidth = textWidth + 12;
    const labelY = dy > labelHeight + 4 ? dy - labelHeight - 4 : dy + 4;
    ctx.fillStyle = 'rgba(7, 11, 19, 0.86)';
    ctx.fillRect(dx, labelY, labelWidth, labelHeight);
    ctx.fillStyle = '#49e6a3';
    ctx.fillText(label, dx + 6, labelY + labelHeight / 2);
  }
}
