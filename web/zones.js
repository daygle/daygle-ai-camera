// zones.js - Zone drawing, editing, and object detection rules.
// Loaded after live.js on zones.html only. Accesses live.js globals:
//   isZonesPage, selectedCamera, liveEls, availableLabels,
//   clamp, normalizePoint, roundCoord, normalizeLabelList,
//   api, cameraDetection, refreshFrame, refreshDetectionStatus,
//   CLOSE_DRAFT_DISTANCE_PX.

let selectedZoneIndex = null;
let drawingMode = false;
let draftPolygon = null;
let zoneDrag = null;
let expandedZoneRules = new Set();

const DEFAULT_MOTION_GATE_FRACTION = 0.005;
const DEFAULT_MOTION_SCALE_FRACTION = 0.03;

// Coerce a per-zone motion override to a clamped number, or null ("inherit").
// Blank/empty/non-numeric all become null so clearing the field drops the
// override rather than sending 0. Mirrors the backend _optional_fraction.
function optionalFraction(value, min, max) {
  if (value == null || value === '') return null;
  const number = Number(value);
  if (!Number.isFinite(number)) return null;
  return Math.round(Math.max(min, Math.min(max, number)) * 1e6) / 1e6;
}

function effectiveZoneMotionTuning() {
  const live = window.daygleLiveConfig || {};
  const numberOr = (value, fallback) => {
    const number = Number(value);
    return Number.isFinite(number) ? number : fallback;
  };
  return {
    gateFraction: numberOr(selectedCamera?.motion_gate_fraction, numberOr(live.motion_gate_fraction, DEFAULT_MOTION_GATE_FRACTION)),
    scaleFraction: numberOr(selectedCamera?.motion_scale_fraction, numberOr(live.motion_scale_fraction, DEFAULT_MOTION_SCALE_FRACTION)),
  };
}

function formatMotionPercent(fraction) {
  const percent = Math.max(0, Number(fraction) || 0) * 100;
  if (percent >= 10) return `${percent.toFixed(1)}%`;
  if (percent >= 1) return `${percent.toFixed(2)}%`;
  return `${percent.toFixed(3)}%`;
}

function motionPixelThresholdText(rule) {
  const { gateFraction, scaleFraction } = effectiveZoneMotionTuning();
  // Per-zone gate/scale overrides win over the camera/global values when set.
  const ruleGate = Number(rule?.gate_fraction);
  const ruleScale = Number(rule?.scale_fraction);
  const gate = rule?.gate_fraction != null && Number.isFinite(ruleGate) ? ruleGate : gateFraction;
  const scale = rule?.scale_fraction != null && Number.isFinite(ruleScale) ? ruleScale : scaleFraction;
  const sensitivity = clamp(Number(rule?.min_confidence ?? 0.45), 0, 1);
  const sensitivityFraction = sensitivity * scale;
  const requiredFraction = Math.max(gate, sensitivityFraction);
  const overridden = (rule?.gate_fraction != null && Number.isFinite(ruleGate)) || (rule?.scale_fraction != null && Number.isFinite(ruleScale));
  return `Approx. ${formatMotionPercent(requiredFraction)} of this zone's pixels must change (${Math.round(sensitivity * 100)}% sensitivity × ${formatMotionPercent(scale)} scale; ${formatMotionPercent(gate)} minimum gate)${overridden ? ' - per-zone override' : ''}.`;
}

// Update only the label text on the Draw polygon button so its icon (a sibling
// <svg>) survives. Setting button.textContent would replace all child nodes,
// wiping the icon.
function setAddZoneLabel(text) {
  const label = liveEls.addZoneBtn?.querySelector('.zone-btn-label');
  if (label) label.textContent = text;
}

// A "full frame" zone is just a polygon whose points are the four corners of
// the camera frame. Detected by shape rather than a stored flag so a rectangle
// reshaped back to the frame corners reads as full frame again, and any drag of
// a corner or an added vertex naturally flips it back to a polygon.
const FULL_FRAME_POINTS = [
  { x: 0, y: 0 },
  { x: 1, y: 0 },
  { x: 1, y: 1 },
  { x: 0, y: 1 },
];

function isFullFrameZone(zone) {
  const points = zone?.points;
  if (!Array.isArray(points) || points.length !== FULL_FRAME_POINTS.length) return false;
  return points.every((point, index) => {
    const corner = FULL_FRAME_POINTS[index];
    return Math.abs(point.x - corner.x) < 0.001 && Math.abs(point.y - corner.y) < 0.001;
  });
}

// Remember the shape a zone had before it was converted to full frame so the
// conversion can be undone in-session. Stored as non-enumerable properties so
// they never leak into the saved JSON payload (the backend rebuilds zones with
// a fixed key set anyway, so they can never persist). `_convertedSeq` is a
// monotonic sequence used by Ctrl+Z to undo the most recent conversion when
// several zones have one pending.
let shapeUndoSeq = 0;

function rememberZoneShape(zone, points) {
  Object.defineProperty(zone, '_previousPoints', {
    value: points.map((point) => ({ ...point })),
    enumerable: false,
    configurable: true,
    writable: true,
  });
  Object.defineProperty(zone, '_convertedSeq', {
    value: ++shapeUndoSeq,
    enumerable: false,
    configurable: true,
    writable: true,
  });
}

function clearRememberedShape(zone) {
  delete zone._previousPoints;
  delete zone._convertedSeq;
}

function convertZoneToFullFrame(zone) {
  rememberZoneShape(zone, zone.points);
  zone.points = FULL_FRAME_POINTS.map((point) => ({ ...point }));
  normalizeZone(zone);
}

// Restore the shape a zone had before its most recent shape-replacing
// conversion. Returns true when a previous shape existed and was restored.
function undoZoneShape(zone) {
  const points = zone?._previousPoints;
  if (!Array.isArray(points) || points.length < 3) return false;
  zone.points = points.map((point) => ({ ...point }));
  clearRememberedShape(zone);
  normalizeZone(zone);
  return true;
}

// Shared by the Shape toggle's "Polygon" option and the per-zone Undo button:
// bring back the pre-conversion shape, or select the zone for reshaping when
// there was none (every zone already has editable corner points - it only
// changes shape once a corner is dragged or a vertex added).
function restorePreviousZoneShape(index) {
  const zones = cameraDetection().zones;
  const zone = zones[index];
  if (!zone || !undoZoneShape(zone)) return false;
  selectedZoneIndex = index;
  renderZones();
  refreshFrame();
  markZoneUnsaved();
  liveEls.status.textContent = 'Previous shape restored - click Save Zones to apply.';
  return true;
}

// Ctrl+Z support: undo the most recent shape-replacing conversion across all
// zones, in reverse conversion order. Returns true when something was undone.
function undoLastShapeConversion() {
  const zones = cameraDetection().zones;
  let targetIndex = -1;
  let latestSeq = 0;
  zones.forEach((zone, index) => {
    const seq = Number(zone?._convertedSeq) || 0;
    if (seq > latestSeq) {
      latestSeq = seq;
      targetIndex = index;
    }
  });
  if (targetIndex < 0) return false;
  return restorePreviousZoneShape(targetIndex);
}

function rectanglePoints(zone) {
  const x = clamp(Number(zone.x) || 0);
  const y = clamp(Number(zone.y) || 0);
  const width = clamp(Number(zone.width) || 0.01, 0.01, 1 - x);
  const height = clamp(Number(zone.height) || 0.01, 0.01, 1 - y);
  return [
    { x, y },
    { x: x + width, y },
    { x: x + width, y: y + height },
    { x, y: y + height },
  ];
}

function updateZoneBounds(zone) {
  const points = zone.points || rectanglePoints(zone);
  const xs = points.map((point) => point.x);
  const ys = points.map((point) => point.y);
  const left = Math.min(...xs);
  const top = Math.min(...ys);
  const right = Math.max(...xs);
  const bottom = Math.max(...ys);
  zone.x = roundCoord(left);
  zone.y = roundCoord(top);
  zone.width = roundCoord(Math.max(0.01, right - left));
  zone.height = roundCoord(Math.max(0.01, bottom - top));
}

function defaultObjectRule(label = '') {
  const normalized = String(label || '').trim().toLowerCase();
  // Motion and faces are non-object-class axes with a 0.45 canonical default
  // (matching zone_motion_min_confidence and the global Face Confidence
  // setting); object classes default to 0.5.
  const baseConfidence = (normalized === 'motion' || normalized === 'face') ? 0.45 : 0.5;
  return {
    label: normalized,
    enabled: true,
    record_on_detect: true,
    min_confidence: baseConfidence,
    max_confidence: 1,
    // Optional per-zone motion sensitivity overrides (motion rule only). null =
    // inherit the camera/global gate/scale.
    gate_fraction: null,
    scale_fraction: null,
    cooldown_seconds: 60,
    email_enabled: false,
    email_recipients: [],
    push_enabled: false,
    active_start: null,
    active_end: null,
    notify_start: null,
    notify_end: null,
  };
}

// Motion is stored as a plain object rule (label 'motion') on the backend so
// the runtime gating (confidence window, record, cooldown, alerts, schedule)
// stays identical, but the GUI presents it as its own card with a single
// toggle instead of a row in the object table. These helpers locate or
// materialize the underlying rule.
function motionRuleOf(zone) {
  if (!zone || !Array.isArray(zone.object_rules)) return null;
  return zone.object_rules.find((rule) => String(rule.label || '').trim().toLowerCase() === 'motion') || null;
}

function ensureMotionRule(zone) {
  const existing = motionRuleOf(zone);
  if (existing) return existing;
  const rule = defaultObjectRule('motion');
  zone.object_rules.push(rule);
  return rule;
}

// Canonical per-axis confidence defaults shared by every normalization path.
function baseConfidenceFor(label) {
  const normalized = String(label || '').trim().toLowerCase();
  return (normalized === 'motion' || normalized === 'face') ? 0.45 : 0.5;
}

// Faces follow the exact motion pattern: stored as a plain object rule
// (label 'face') so backend gating (confidence window, record, cooldown,
// alerts, schedule) stays identical, presented as its own card. A camera
// with at least one enabled Face rule scopes ALL face processing to those
// zones -- faces detected elsewhere are dropped before recognition runs.
function faceRuleOf(zone) {
  if (!zone || !Array.isArray(zone.object_rules)) return null;
  return zone.object_rules.find((rule) => String(rule.label || '').trim().toLowerCase() === 'face') || null;
}

function ensureFaceRule(zone) {
  const existing = faceRuleOf(zone);
  if (existing) return existing;
  const rule = defaultObjectRule('face');
  zone.object_rules.push(rule);
  return rule;
}

// ─── Behavioural tripwire (line crossing) ──────────────────────────────────
// A zone's optional directional line-crossing counter, stored on
// ``zone.tripwire``. The pure geometry/normalisation lives in web/utils.js
// (tripwireDefaultLine, normalizeTripwire, tripwireForwardNormal, …); these
// helpers locate/materialise the rule on the in-memory zone, mirroring the
// motion/face card pattern.
function tripwireOf(zone) {
  return zone && zone.tripwire && typeof zone.tripwire === 'object' ? zone.tripwire : null;
}

// Turn a tripwire on: reuse the existing one (re-enabling it) or seed a fresh
// one with a default line spanning the zone, which the user then drags into
// place on the footage.
function ensureTripwire(zone) {
  const existing = tripwireOf(zone);
  if (existing) {
    existing.enabled = true;
    return existing;
  }
  const line = tripwireDefaultLine(zone);
  zone.tripwire = {
    enabled: true,
    name: 'Tripwire',
    a: line.a,
    b: line.b,
    direction: 'both',
    labels: [],
    cooldown_seconds: 30,
    record_on_detect: true,
    // Delivery (email/push/recipients/quiet-hours) is configured on the Alerts
    // page, matching object/sound rules; these defaults keep the shape intact
    // until then.
    email_enabled: false,
    email_recipients: [],
    push_enabled: false,
    notify_start: null,
    notify_end: null,
  };
  return zone.tripwire;
}

// ─── Behavioural loiter rule (Tier 2: statistical long-dwell) ───────────────
// A zone's optional loitering detector, stored on ``zone.loiter``. Detection
// config (min dwell, sensitivity, which objects, record) lives on the Zones
// card; email/push/quiet-hours are configured on the Alerts page, like the
// tripwire and object/sound rules.
function loiterOf(zone) {
  return zone && zone.loiter && typeof zone.loiter === 'object' ? zone.loiter : null;
}

function ensureLoiter(zone) {
  const existing = loiterOf(zone);
  if (existing) {
    existing.enabled = true;
    return existing;
  }
  zone.loiter = {
    enabled: true,
    name: 'Loitering',
    labels: [],
    min_dwell_seconds: 30,
    sensitivity: 3,
    cooldown_seconds: 120,
    record_on_detect: true,
    email_enabled: false,
    email_recipients: [],
    push_enabled: false,
    notify_start: null,
    notify_end: null,
  };
  return zone.loiter;
}

// ─── Behavioural unusual time-of-day rule (Tier 2) ──────────────────────────
// A zone's optional unusual-time detector, stored on ``zone.time_of_day``.
// Detection config (threshold, which objects, record) lives on the Zones card;
// email/push/quiet-hours are configured on the Alerts page.
function timeOf(zone) {
  return zone && zone.time_of_day && typeof zone.time_of_day === 'object' ? zone.time_of_day : null;
}

function ensureTime(zone) {
  const existing = timeOf(zone);
  if (existing) {
    existing.enabled = true;
    return existing;
  }
  zone.time_of_day = {
    enabled: true,
    name: 'Unusual time',
    labels: [],
    threshold: 0.15,
    cooldown_seconds: 1800,
    record_on_detect: true,
    email_enabled: false,
    email_recipients: [],
    push_enabled: false,
    notify_start: null,
    notify_end: null,
  };
  return zone.time_of_day;
}

function normalizeAlertSchedules(rule) {
  const source = Array.isArray(rule.alert_schedules) && rule.alert_schedules.length
    ? rule.alert_schedules
    : [rule];
  const schedules = source.filter((schedule) => schedule && typeof schedule === 'object').map((schedule, index) => ({
    ...schedule,
    id: String(schedule.id || `schedule-${index + 1}`),
    email_enabled: schedule.email_enabled === true,
    push_enabled: schedule.push_enabled === true,
    email_recipients: normalizeEmailList(schedule.email_recipients),
    active_start: schedule.active_start || null,
    active_end: schedule.active_end || null,
    notify_start: schedule.notify_start || null,
    notify_end: schedule.notify_end || null,
  }));
  const first = schedules[0];
  rule.alert_schedules = schedules;
  rule.email_enabled = schedules.some((schedule) => schedule.email_enabled);
  rule.push_enabled = schedules.some((schedule) => schedule.push_enabled);
  rule.email_recipients = [...new Set(schedules.flatMap((schedule) => schedule.email_recipients))];
  if (first) {
    for (const key of ['active_start', 'active_end', 'notify_start', 'notify_end']) rule[key] = first[key];
  }
  return schedules;
}

function normalizeObjectRules(zone) {
  if (Array.isArray(zone.object_rules) && zone.object_rules.length) {
    return zone.object_rules.map((rule, ruleIndex) => ({ ...defaultObjectRule(rule?.label), ...rule, id: rule?.id || `${String(rule?.label || 'rule').trim().toLowerCase()}-${ruleIndex + 1}` }))
      .map((rule) => ({
        ...rule,
        label: String(rule.label || '').trim().toLowerCase(),
        enabled: rule.enabled !== false,
        record_on_detect: rule.record_on_detect !== false,
        min_confidence: clamp(Number(rule.min_confidence ?? baseConfidenceFor(rule.label)), 0, 1),
        // Upper bound of the confidence window. Defaults to 1 (no cap) and is
        // never allowed below min_confidence so the [min, max] band is valid.
        max_confidence: Math.max(
          clamp(Number(rule.min_confidence ?? baseConfidenceFor(rule.label)), 0, 1),
          clamp(Number(rule.max_confidence ?? 1), 0, 1),
        ),
        // Per-zone motion gate/scale overrides: clamp to the backend ranges when
        // set (motion rules only), else null so the zone inherits camera/global.
        gate_fraction: String(rule.label || '').trim().toLowerCase() === 'motion'
          ? optionalFraction(rule.gate_fraction, 0.0001, 0.5) : null,
        scale_fraction: String(rule.label || '').trim().toLowerCase() === 'motion'
          ? optionalFraction(rule.scale_fraction, 0.001, 1.0) : null,
        cooldown_seconds: Math.max(0, Number.parseInt(rule.cooldown_seconds ?? 60, 10) || 0),
        email_enabled: rule.email_enabled === true,
        email_recipients: normalizeEmailList(rule.email_recipients),
        push_enabled: rule.push_enabled === true,
        active_start: rule.active_start || null,
        active_end: rule.active_end || null,
        notify_start: rule.notify_start || null,
        notify_end: rule.notify_end || null,
        alert_schedules: normalizeAlertSchedules(rule),
      }))
      .filter((rule) => Boolean(rule.label));
  }
  return normalizeLabelList(zone.object_labels).map(defaultObjectRule);
}

function normalizeZone(zone) {
  const sourcePoints = Array.isArray(zone.points) && zone.points.length >= 3 ? zone.points : rectanglePoints(zone);
  zone.points = sourcePoints.map(normalizePoint);
  zone.object_rules = normalizeObjectRules(zone);
  zone.object_labels = zone.object_rules.filter((r) => r.label !== 'motion' && r.label !== 'face').map((rule) => rule.label);
  // Keep the legacy `monitor_motion` flag in sync with the actual rule list
  // so a removed or disabled Motion rule stays gone after save. The backend's
  // normalize_monitoring_zones() re-inserts Motion when it sees
  // ``monitor_motion=true`` but no Motion rule in ``object_rules`` -- without
  // this sync that legacy-migration path would resurrect the rule on every
  // save round-trip, making the delete appear to be ignored.
  zone.monitor_motion = zone.object_rules.some(
    (rule) => String(rule.label || '').trim().toLowerCase() === 'motion'
      && rule.enabled !== false
  );
  // Same legacy-flag pattern for the Faces axis: derived from the enabled
  // face rule so the backend's normalize_monitoring_zones never resurrects a
  // deleted rule on save round-trips.
  zone.monitor_faces = zone.object_rules.some(
    (rule) => String(rule.label || '').trim().toLowerCase() === 'face'
      && rule.enabled !== false
  );
  // Normalise (or drop) the optional line-crossing tripwire so the in-memory
  // shape matches what the backend stores: a valid line is clamped/rounded, an
  // absent or degenerate one is removed so the zone keeps its canonical shape.
  const tripwire = normalizeTripwire(zone.tripwire);
  if (tripwire) zone.tripwire = tripwire;
  else if ('tripwire' in zone) delete zone.tripwire;
  // Same for the optional loiter rule (Tier 2): normalise when present, drop
  // otherwise so a zone without one keeps its canonical shape.
  const loiter = normalizeLoiter(zone.loiter);
  if (loiter) zone.loiter = loiter;
  else if ('loiter' in zone) delete zone.loiter;
  // And the optional unusual time-of-day rule (Tier 2).
  const timeRule = normalizeTime(zone.time_of_day);
  if (timeRule) zone.time_of_day = timeRule;
  else if ('time_of_day' in zone) delete zone.time_of_day;
  updateZoneBounds(zone);
  return zone;
}

function visibleImageRect() {
  const frameRect = liveEls.frame.getBoundingClientRect();
  const naturalWidth = liveEls.frame.naturalWidth || selectedCamera?.width || 16;
  const naturalHeight = liveEls.frame.naturalHeight || selectedCamera?.height || 9;
  const imageRatio = naturalWidth / naturalHeight;
  const frameRatio = frameRect.width / frameRect.height;
  let width = frameRect.width;
  let height = frameRect.height;
  let left = frameRect.left;
  let top = frameRect.top;
  if (frameRatio > imageRatio) {
    width = height * imageRatio;
    left += (frameRect.width - width) / 2;
  } else {
    height = width / imageRatio;
    top += (frameRect.height - height) / 2;
  }
  return { left, top, width, height };
}

function syncZoneOverlayToImage() {
  if (!liveEls.zoneOverlay || !liveEls.frameWrap || !liveEls.frame) return;
  const wrapRect = liveEls.frameWrap.getBoundingClientRect();
  const imageRect = visibleImageRect();
  liveEls.zoneOverlay.style.left = `${imageRect.left - wrapRect.left}px`;
  liveEls.zoneOverlay.style.top = `${imageRect.top - wrapRect.top}px`;
  liveEls.zoneOverlay.style.width = `${imageRect.width}px`;
  liveEls.zoneOverlay.style.height = `${imageRect.height}px`;
}

function updateZonesStats() {
  if (!selectedCamera) return;
  const detection = cameraDetection();
  const zones = detection.zones || [];
  const ruleCount = zones.reduce((sum, zone) => sum + (zone.object_rules?.length || 0), 0);
  if (liveEls.statZoneCount) liveEls.statZoneCount.textContent = String(zones.length);
  if (liveEls.statRuleCount) liveEls.statRuleCount.textContent = String(ruleCount);
  if (liveEls.statCameraName) {
    liveEls.statCameraName.textContent = selectedCamera.name || selectedCamera.id || '-';
  }
  const zonesListCount = document.getElementById('zonesListCount');
  if (zonesListCount) {
    zonesListCount.textContent = `${zones.length} area${zones.length === 1 ? '' : 's'}`;
  }
}

// SVG arrow(s) drawn from the tripwire midpoint indicating the counting
// direction: one along the forward (right-hand) normal for 'forward', the
// reverse for 'backward', both for 'both'. Coordinates are in the 0..100
// viewBox space shared with the zone polygons.
function tripwireArrowMarkup(a, b, direction) {
  const normal = tripwireForwardNormal(a, b);
  if (!normal) return '';
  const mid = tripwireMidpoint(a, b);
  const mx = mid.x * 100;
  const my = mid.y * 100;
  const STEM = 9;
  const HEAD = 3.4;
  const round = (value) => Math.round(value * 100) / 100;
  const oneArrow = (sign) => {
    const dx = normal.x * sign;
    const dy = normal.y * sign;
    const tipX = mx + dx * STEM;
    const tipY = my + dy * STEM;
    // Splay the two head strokes along the arrow's perpendicular (the line
    // tangent), backed off from the tip along -direction.
    const px = -dy;
    const py = dx;
    const h1x = tipX - dx * HEAD + px * HEAD * 0.7;
    const h1y = tipY - dy * HEAD + py * HEAD * 0.7;
    const h2x = tipX - dx * HEAD - px * HEAD * 0.7;
    const h2y = tipY - dy * HEAD - py * HEAD * 0.7;
    return `<polyline points="${round(mx)},${round(my)} ${round(tipX)},${round(tipY)}"></polyline>`
      + `<polyline points="${round(h1x)},${round(h1y)} ${round(tipX)},${round(tipY)} ${round(h2x)},${round(h2y)}"></polyline>`;
  };
  if (direction === 'forward') return oneArrow(1);
  if (direction === 'backward') return oneArrow(-1);
  return oneArrow(1) + oneArrow(-1);
}

// The tripwire line, its direction arrow, and (when the zone is selected)
// draggable endpoint handles, layered over the zone polygon.
function renderTripwireOverlay(zone, index) {
  const wire = tripwireOf(zone);
  if (!wire || wire.enabled === false) return '';
  const a = tripwirePoint(wire.a);
  const b = tripwirePoint(wire.b);
  if (!a || !b) return '';
  const selected = index === selectedZoneIndex;
  const ax = a.x * 100;
  const ay = a.y * 100;
  const bx = b.x * 100;
  const by = b.y * 100;
  const handles = selected && !drawingMode ? (
    `<i class="zone-handle tripwire-handle" data-tripwire-index="${index}" data-tripwire-end="a" title="Drag to move the line start" style="left:${ax}%;top:${ay}%"></i>`
    + `<i class="zone-handle tripwire-handle" data-tripwire-index="${index}" data-tripwire-end="b" title="Drag to move the line end" style="left:${bx}%;top:${by}%"></i>`
  ) : '';
  return `
    <svg class="monitor-tripwire${selected ? ' selected' : ''}" viewBox="0 0 100 100" preserveAspectRatio="none" aria-hidden="true">
      <line x1="${ax}" y1="${ay}" x2="${bx}" y2="${by}"></line>
      ${tripwireArrowMarkup(a, b, wire.direction || 'both')}
    </svg>
    ${handles}
  `;
}

function renderZoneBox(zone, index) {
  const selected = index === selectedZoneIndex ? ' selected' : '';
  const points = zone.points.map((point) => `${point.x * 100},${point.y * 100}`).join(' ');
  const labelPoint = { x: zone.x, y: zone.y };
  // Compact shape badge on the canvas label (hover shows the full name).
  // The third entry is the CSS suffix (.zone-shape-badge--<suffix>).
  const shapeBadge = isFullFrameZone(zone)
    ? ['Full', 'Full frame', 'full']
    : ['Poly', 'Polygon', 'poly'];
  const handles = zone.points.map((point, pointIndex) => (
    `<i class="zone-handle zone-point-handle" data-zone-index="${index}" data-point-index="${pointIndex}" style="left:${point.x * 100}%;top:${point.y * 100}%"></i>`
  )).join('');
  // Mid-edge '+' handles let an existing zone gain extra vertices (e.g. turn a
  // full-frame rectangle into a custom polygon). Only the selected zone shows
  // them so the canvas isn't littered with handles for every area.
  const addPointHandles = index === selectedZoneIndex && !drawingMode
    ? zone.points.map((point, pointIndex) => {
        const next = zone.points[(pointIndex + 1) % zone.points.length];
        const midX = (point.x + next.x) / 2;
        const midY = (point.y + next.y) / 2;
        return `<i class="zone-handle zone-add-point-handle" data-zone-index="${index}" data-add-point="${index}:${pointIndex}" title="Add a point" style="left:${midX * 100}%;top:${midY * 100}%"></i>`;
      }).join('')
    : '';
  return `
    <svg class="monitor-zone-polygon${selected}" data-zone-index="${index}" viewBox="0 0 100 100" preserveAspectRatio="none">
      <polygon data-zone-index="${index}" points="${points}"></polygon>
    </svg>
    <span class="zone-label${selected}" data-zone-index="${index}" style="left:${labelPoint.x * 100}%;top:${labelPoint.y * 100}%">
      <span class="zone-label-name">${escapeHtml(zone.name || `Zone ${index + 1}`)}</span>
      <i class="zone-shape-badge zone-shape-badge--${shapeBadge[2]}" title="${shapeBadge[1]} shape">${shapeBadge[0]}</i>
    </span>
    ${handles}
    ${addPointHandles}
    ${renderTripwireOverlay(zone, index)}
  `;
}

function updateSelectionStyles() {
  liveEls.zoneOverlay?.querySelectorAll('.monitor-zone-polygon, .zone-label').forEach((element) => {
    element.classList.toggle('selected', Number(element.dataset.zoneIndex) === selectedZoneIndex);
  });
  liveEls.zoneList?.querySelectorAll('[data-select-zone]').forEach((row) => {
    row.classList.toggle('selected', Number(row.dataset.selectZone) === selectedZoneIndex);
  });
}

// Umbrella group labels: a single rule that matches ANY member class. Mirrors
// app/zone_schema.py::_LABEL_GROUPS on the backend. Useful when a subject is
// easily mislabeled between related classes (e.g. an IR-lit cat read as a dog).
const OBJECT_GROUP_LABELS = [
  { value: 'animal', label: 'Animal (Cat, Dog, Bird…)' },
  { value: 'pet', label: 'Pet (Cat / Dog / Bird)' },
];

function objectRuleOptions(selectedLabel) {
  const groupValues = new Set(OBJECT_GROUP_LABELS.map((group) => group.value));
  // Group names are rendered as dedicated options below, so keep them out of the
  // per-class list even when one is the currently selected value.
  const labels = [...new Set([...availableLabels, selectedLabel].filter((l) => Boolean(l) && l !== 'motion' && l !== 'face' && !groupValues.has(l)))];
  // Display labels in title case for readability; the value attribute stays
  // raw lowercase because rule.label is the canonical lookup key used by
  // defaultObjectRule, normalizeObjectRules, and backend filters.
  const coco = labels.map((label) => `<option value="${escapeHtml(label)}" ${label === selectedLabel ? 'selected' : ''}>${escapeHtml(titleCase(label))}</option>`).join('');
  const groups = OBJECT_GROUP_LABELS.map((group) => `<option value="${escapeHtml(group.value)}" ${group.value === selectedLabel ? 'selected' : ''}>${escapeHtml(group.label)}</option>`).join('');
  // Motion is not an object class: it gets its own dedicated per-zone card
  // (renderMotionCard) with a single toggle, so it stays out of this list.
  return `<option value="">Add Object...</option><optgroup label="Groups">${groups}</optgroup>${coco}`;
}

// Shared On/Off pill used in every detection-table cell (Detect + Record),
// so object, motion, face and sound rows all render identically.
function ruleToggleCell(attr, on, title, disabled) {
  return `<label class="toggle-control zone-rule-toggle" title="${escapeHtml(title)}"><input type="checkbox" ${attr}${on ? ' checked' : ''}${disabled ? ' disabled' : ''} /><span>${on ? 'On' : 'Off'}</span></label>`;
}

// Object-class rows for a zone's detection table. Motion and face get their own
// rows (renderMotionCard/renderFaceCard); this returns only object-class <tr>s.
function renderObjectRules(zone, zoneIndex) {
  zone.object_rules = normalizeObjectRules(zone);
  const rules = zone.object_rules
    .map((rule, ruleIndex) => ({ rule, ruleIndex }))
    .filter(({ rule }) => {
      const label = String(rule.label || '').trim().toLowerCase();
      return label !== 'motion' && label !== 'face';
    });
  return rules.map(({ rule, ruleIndex }) => {
    const key = `${zoneIndex}:${ruleIndex}`;
    const label = escapeHtml(titleCase(rule.label));
    const lower = label.toLowerCase();
    const enabled = rule.enabled !== false;
    return `
      <tr class="zone-rule-row${enabled ? ' is-enabled' : ''}">
        <td class="cell-label"><span class="zone-rule-icon" aria-hidden="true">🔍</span>${label}</td>
        <td>${ruleToggleCell(`data-zone-rule-enabled="${key}"`, enabled, `Enable or disable ${lower} detection in this area`, false)}</td>
        <td><input class="zone-rule-conf" type="number" data-zone-rule-confidence-value="${key}" min="0.01" max="1" step="0.01" value="${escapeHtml(rule.min_confidence)}" title="Minimum confidence (0.01-1). Overrides the global ONNX slider for this object in this zone." /></td>
        <td>${ruleToggleCell(`data-zone-rule-record="${key}"`, rule.record_on_detect !== false, `Record a clip when ${lower} is detected in this area`, false)}</td>
        <td class="cell-actions"><button class="delete-btn secondary zone-action-btn zone-rule-remove" type="button" data-delete-zone-rule="${key}" title="Remove ${label} from this area" aria-label="Remove ${label} from this area">${ICONS.remove}</button></td>
      </tr>`;
  }).join('');
}

// Motion row for the detection table, plus a hidden "Advanced" row that holds
// the per-zone gate/scale overrides and the pixel-threshold hint.
function renderMotionCard(zone, zoneIndex) {
  const rule = motionRuleOf(zone);
  const enabled = Boolean(rule && rule.enabled !== false);
  const conf = rule?.min_confidence ?? 0.45;
  const advancedRow = enabled ? `
    <tr class="zone-rule-advanced-row" data-zone-motion-advanced-for="${zoneIndex}" hidden>
      <td colspan="5">
        <div class="zone-rule-advanced">
          <label class="sound-rule-field" title="Per-zone gate: minimum fraction of THIS zone's pixels that must change before motion counts. Leave blank to use the camera/global gate. Lower = more sensitive for this zone only.">
            <span>Gate override</span>
            <input type="number" data-zone-motion-gate="${zoneIndex}" value="${rule.gate_fraction != null ? escapeHtml(rule.gate_fraction) : ''}" min="0.0001" max="0.5" step="0.0001" placeholder="Inherit" />
          </label>
          <label class="sound-rule-field" title="Per-zone scale: pixel-change fraction in THIS zone that maps to 100% motion confidence. Leave blank to use the camera/global scale. Lower = stronger confidence for small motion in this zone.">
            <span>Scale override</span>
            <input type="number" data-zone-motion-scale="${zoneIndex}" value="${rule.scale_fraction != null ? escapeHtml(rule.scale_fraction) : ''}" min="0.001" max="1.0" step="0.001" placeholder="Inherit" />
          </label>
          <small class="form-help muted zone-motion-pixel-help" data-zone-motion-pixel-help="${zoneIndex}">${escapeHtml(motionPixelThresholdText(rule))}</small>
        </div>
      </td>
    </tr>` : '';
  return `
    <tr class="zone-rule-row zone-rule-structural${enabled ? ' is-enabled' : ''}" data-zone-motion-for="${zoneIndex}">
      <td class="cell-label"><span class="zone-rule-icon" aria-hidden="true">⟳</span>Motion</td>
      <td>${ruleToggleCell(`data-zone-motion-toggle="${zoneIndex}"`, enabled, 'Enable or disable motion detection in this area', false)}</td>
      <td><input class="zone-rule-conf" type="number" data-zone-motion-confidence="${zoneIndex}" min="0" max="1" step="0.05" value="${escapeHtml(conf)}" title="Sensitivity: only motion with at least this confidence counts (0-1). Lower = more sensitive."${enabled ? '' : ' disabled'} /></td>
      <td>${ruleToggleCell(`data-zone-motion-record="${zoneIndex}"`, (rule?.record_on_detect) !== false, 'Record a clip when motion is detected in this area', !enabled)}</td>
      <td class="cell-actions">${enabled ? `<button class="secondary zone-action-btn zone-rule-advanced-toggle zone-rule-advanced-icon-btn" type="button" data-zone-motion-advanced-toggle="${zoneIndex}" title="Per-zone motion pixel overrides" aria-label="Per-zone motion pixel overrides" aria-expanded="false">${ICONS.cog}</button>` : ''}</td>
    </tr>${advancedRow}`;
}

// Face row for the detection table.
function renderFaceCard(zone, zoneIndex) {
  const rule = faceRuleOf(zone);
  const enabled = Boolean(rule && rule.enabled !== false);
  const conf = rule?.min_confidence ?? 0.45;
  return `
    <tr class="zone-rule-row zone-rule-structural${enabled ? ' is-enabled' : ''}" data-zone-face-for="${zoneIndex}">
      <td class="cell-label"><span class="zone-rule-icon" aria-hidden="true">👤</span>Face</td>
      <td>${ruleToggleCell(`data-zone-face-toggle="${zoneIndex}"`, enabled, 'Enable or disable face detection in this area', false)}</td>
      <td><input class="zone-rule-conf" type="number" data-zone-face-confidence="${zoneIndex}" min="0" max="1" step="0.05" value="${escapeHtml(conf)}" title="Only faces with at least this confidence are processed in this area (0-1). Faces detected outside Face-enabled areas are ignored entirely."${enabled ? '' : ' disabled'} /></td>
      <td>${ruleToggleCell(`data-zone-face-record="${zoneIndex}"`, (rule?.record_on_detect) !== false, 'Record a clip when a face is detected in this area', !enabled)}</td>
      <td class="cell-actions"></td>
    </tr>`;
}

// Segmented direction control for a tripwire card. Forward = an object moving
// from the LEFT of the drawn line to its RIGHT (the arrow side).
function tripwireDirectionOptions(current) {
  const options = [
    ['forward', 'Arrow way', 'Count only crossings that travel the way the arrow points (left → right of the line).'],
    ['backward', 'Against', 'Count only crossings that travel against the arrow (right → left of the line).'],
    ['both', 'Both', 'Count a crossing in either direction.'],
  ];
  return options.map(([value, label, title]) => (
    `<button type="button" class="zone-shape-option tripwire-dir-option${value === current ? ' is-active' : ''}" data-tripwire-direction-set="${value}" aria-pressed="${value === current}" title="${escapeHtml(title)}">${label}</button>`
  )).join('');
}

// Removable chips for the object labels a tripwire counts ([] = any object).
function tripwireLabelChips(wire, zoneIndex) {
  const labels = wire && Array.isArray(wire.labels) ? wire.labels : [];
  if (!labels.length) return '<span class="tripwire-any">Any object</span>';
  return labels.map((label, labelIndex) => (
    `<span class="zone-object-chip tripwire-chip">${escapeHtml(titleCase(label))}<button type="button" class="tripwire-chip-remove" data-tripwire-label-remove="${zoneIndex}:${labelIndex}" title="Stop counting ${escapeHtml(titleCase(label))}" aria-label="Stop counting ${escapeHtml(titleCase(label))}">×</button></span>`
  )).join('');
}

// "+ Limit to object" picker: available classes/groups not already chosen.
function tripwireLabelAddOptions(selected) {
  const chosen = new Set((selected || []).map((label) => String(label).toLowerCase()));
  const groupValues = new Set(OBJECT_GROUP_LABELS.map((group) => group.value));
  const labels = [...new Set(availableLabels.filter((label) => (
    label && label !== 'motion' && label !== 'face' && !groupValues.has(label) && !chosen.has(label)
  )))];
  const coco = labels.map((label) => `<option value="${escapeHtml(label)}">${escapeHtml(titleCase(label))}</option>`).join('');
  const groups = OBJECT_GROUP_LABELS.filter((group) => !chosen.has(group.value))
    .map((group) => `<option value="${escapeHtml(group.value)}">${escapeHtml(group.label)}</option>`).join('');
  return `<option value="">+ Limit to object…</option>${groups ? `<optgroup label="Groups">${groups}</optgroup>` : ''}${coco}`;
}

function tripwireToggleField(label, attr, on, title) {
  return `<div class="tripwire-toggle-field"><span>${escapeHtml(label)}</span>${ruleToggleCell(attr, on, title, false)}</div>`;
}

// The editable body of an enabled tripwire card. Detection only (line,
// direction, which objects count, record); email/push/quiet-hours are
// configured on the Alerts page, like object and sound rules.
function tripwireBody(wire, zoneIndex) {
  return `
    <div class="zone-tripwire-body">
      <label class="sound-rule-field tripwire-name-field">
        <span>Name</span>
        <input type="text" data-tripwire-name="${zoneIndex}" value="${escapeHtml(wire.name || 'Tripwire')}" maxlength="60" placeholder="Tripwire" />
      </label>
      <div class="sound-rule-field tripwire-direction-field">
        <span>Direction</span>
        <div class="zone-shape-toggle tripwire-direction" role="group" aria-label="Counting direction" data-tripwire-direction-for="${zoneIndex}">${tripwireDirectionOptions(wire.direction || 'both')}</div>
      </div>
      <div class="sound-rule-field tripwire-labels-field">
        <span>Counts</span>
        <div class="tripwire-labels" data-tripwire-labels="${zoneIndex}">${tripwireLabelChips(wire, zoneIndex)}</div>
        <select class="rule-add-select tripwire-label-add" data-tripwire-label-add="${zoneIndex}" aria-label="Limit which objects this line counts">${tripwireLabelAddOptions(wire.labels)}</select>
      </div>
      <div class="tripwire-toggles">
        ${tripwireToggleField('Record', `data-tripwire-record="${zoneIndex}"`, wire.record_on_detect !== false, 'Record a clip when the line is crossed')}
      </div>
      <p class="muted tripwire-hint">Drag the two dots on the footage to place the line. The arrow shows the “forward” direction. <a class="zone-assigned-link" href="/alerts">Set email / push alerts</a></p>
    </div>`;
}

// Per-zone line-crossing card, rendered under the detection table. Off by
// default; enabling it seeds a line across the zone to drag into position.
function renderTripwireCard(zone, zoneIndex) {
  const wire = tripwireOf(zone);
  const enabled = Boolean(wire && wire.enabled !== false);
  return `
    <div class="zone-tripwire-card${enabled ? ' is-enabled' : ''}" data-zone-tripwire-for="${zoneIndex}">
      <div class="zone-tripwire-head">
        <div class="zone-tripwire-title"><span class="zone-rule-icon" aria-hidden="true">⤢</span><strong>Line Crossing</strong><span class="muted zone-tripwire-sub">Alert when an object crosses a line you draw</span></div>
        ${ruleToggleCell(`data-tripwire-enabled="${zoneIndex}"`, enabled, 'Enable a directional line-crossing counter for this area', false)}
      </div>
      ${enabled ? tripwireBody(wire, zoneIndex) : '<p class="muted tripwire-hint tripwire-hint-off">Turn this on to draw a line across the footage and get alerted when a tracked object crosses it in the direction you choose.</p>'}
    </div>`;
}

// Removable chips for the object labels a loiter rule counts ([] = any object).
function loiterLabelChips(rule, zoneIndex) {
  const labels = rule && Array.isArray(rule.labels) ? rule.labels : [];
  if (!labels.length) return '<span class="tripwire-any">Any object</span>';
  return labels.map((label, labelIndex) => (
    `<span class="zone-object-chip tripwire-chip">${escapeHtml(titleCase(label))}<button type="button" class="tripwire-chip-remove" data-loiter-label-remove="${zoneIndex}:${labelIndex}" title="Stop counting ${escapeHtml(titleCase(label))}" aria-label="Stop counting ${escapeHtml(titleCase(label))}">×</button></span>`
  )).join('');
}

// The editable body of an enabled loiter card. Detection only (min dwell,
// sensitivity, which objects, record); email/push/quiet-hours are configured on
// the Alerts page, like the tripwire and object/sound rules.
function loiterBody(rule, zoneIndex) {
  return `
    <div class="zone-tripwire-body">
      <label class="sound-rule-field tripwire-name-field">
        <span>Name</span>
        <input type="text" data-loiter-name="${zoneIndex}" value="${escapeHtml(rule.name || 'Loitering')}" maxlength="60" placeholder="Loitering" />
      </label>
      <div class="tripwire-toggles">
        <label class="sound-rule-field">
          <span>Min dwell (s)</span>
          <input type="number" data-loiter-min-dwell="${zoneIndex}" min="1" step="1" value="${escapeHtml(rule.min_dwell_seconds ?? 30)}" title="An object must stay at least this many seconds before it can count as loitering." />
        </label>
        <label class="sound-rule-field">
          <span>Sensitivity</span>
          <input type="number" data-loiter-sensitivity="${zoneIndex}" min="0" max="10" step="0.5" value="${escapeHtml(rule.sensitivity ?? 3)}" title="How far above the zone's normal dwell before it counts (× the normal spread). Lower = more sensitive; 0 = fire at the minimum dwell." />
        </label>
      </div>
      <div class="sound-rule-field tripwire-labels-field">
        <span>Counts</span>
        <div class="tripwire-labels" data-loiter-labels="${zoneIndex}">${loiterLabelChips(rule, zoneIndex)}</div>
        <select class="rule-add-select tripwire-label-add" data-loiter-label-add="${zoneIndex}" aria-label="Limit which objects this rule counts">${tripwireLabelAddOptions(rule.labels)}</select>
      </div>
      <div class="tripwire-toggles">
        ${tripwireToggleField('Record', `data-loiter-record="${zoneIndex}"`, rule.record_on_detect !== false, 'Record a clip when loitering is detected')}
      </div>
      <p class="muted tripwire-hint">Learns this area's normal dwell over time, then alerts on an unusually long stay. <a class="zone-assigned-link" href="/alerts">Set email / push alerts</a></p>
    </div>`;
}

// Per-zone loitering card, rendered under the line-crossing card. Off by
// default; enabling it starts learning the zone's normal dwell.
function renderLoiterCard(zone, zoneIndex) {
  const rule = loiterOf(zone);
  const enabled = Boolean(rule && rule.enabled !== false);
  return `
    <div class="zone-tripwire-card zone-loiter-card${enabled ? ' is-enabled' : ''}" data-zone-loiter-for="${zoneIndex}">
      <div class="zone-tripwire-head">
        <div class="zone-tripwire-title"><span class="zone-rule-icon" aria-hidden="true">⏲</span><strong>Loitering</strong><span class="muted zone-tripwire-sub">Alert when an object lingers far longer than normal</span></div>
        ${ruleToggleCell(`data-loiter-enabled="${zoneIndex}"`, enabled, 'Enable statistical loitering detection for this area', false)}
      </div>
      ${enabled ? loiterBody(rule, zoneIndex) : '<p class="muted tripwire-hint tripwire-hint-off">Turn this on to learn this area\'s normal dwell time and get alerted when something stays unusually long.</p>'}
    </div>`;
}

// Removable chips for the object labels an unusual-time rule counts.
function timeLabelChips(rule, zoneIndex) {
  const labels = rule && Array.isArray(rule.labels) ? rule.labels : [];
  if (!labels.length) return '<span class="tripwire-any">Any object</span>';
  return labels.map((label, labelIndex) => (
    `<span class="zone-object-chip tripwire-chip">${escapeHtml(titleCase(label))}<button type="button" class="tripwire-chip-remove" data-time-label-remove="${zoneIndex}:${labelIndex}" title="Stop counting ${escapeHtml(titleCase(label))}" aria-label="Stop counting ${escapeHtml(titleCase(label))}">×</button></span>`
  )).join('');
}

// The editable body of an enabled unusual-time card. Detection only (rarity
// threshold, which objects, record); email/push/quiet-hours on the Alerts page.
// The threshold is stored as a 0..1 fraction but shown as a percentage.
function timeBody(rule, zoneIndex) {
  const percent = Math.round(Math.max(0, Math.min(1, Number(rule.threshold ?? 0.15))) * 100);
  return `
    <div class="zone-tripwire-body">
      <label class="sound-rule-field tripwire-name-field">
        <span>Name</span>
        <input type="text" data-time-name="${zoneIndex}" value="${escapeHtml(rule.name || 'Unusual time')}" maxlength="60" placeholder="Unusual time" />
      </label>
      <label class="sound-rule-field">
        <span>Flag under (%)</span>
        <input type="number" data-time-threshold="${zoneIndex}" min="0" max="100" step="1" value="${escapeHtml(percent)}" title="Consider an hour unusual when this area normally has activity on at most this percent of days. Lower = only the rarest hours fire." />
      </label>
      <div class="sound-rule-field tripwire-labels-field">
        <span>Counts</span>
        <div class="tripwire-labels" data-time-labels="${zoneIndex}">${timeLabelChips(rule, zoneIndex)}</div>
        <select class="rule-add-select tripwire-label-add" data-time-label-add="${zoneIndex}" aria-label="Limit which objects this rule counts">${tripwireLabelAddOptions(rule.labels)}</select>
      </div>
      <div class="tripwire-toggles">
        ${tripwireToggleField('Record', `data-time-record="${zoneIndex}"`, rule.record_on_detect !== false, 'Record a clip when activity happens at an unusual time')}
      </div>
      <p class="muted tripwire-hint">Learns which hours this area is normally active (needs about a week), then alerts on activity at a normally-quiet hour. <a class="zone-assigned-link" href="/alerts">Set email / push alerts</a></p>
    </div>`;
}

// Per-zone unusual time-of-day card, rendered under the loitering card. Off by
// default; enabling it starts learning the zone's normal active hours.
function renderTimeCard(zone, zoneIndex) {
  const rule = timeOf(zone);
  const enabled = Boolean(rule && rule.enabled !== false);
  return `
    <div class="zone-tripwire-card zone-time-card${enabled ? ' is-enabled' : ''}" data-zone-time-for="${zoneIndex}">
      <div class="zone-tripwire-head">
        <div class="zone-tripwire-title"><span class="zone-rule-icon" aria-hidden="true">🕒</span><strong>Unusual Time</strong><span class="muted zone-tripwire-sub">Alert on activity at a normally-quiet hour</span></div>
        ${ruleToggleCell(`data-time-enabled="${zoneIndex}"`, enabled, 'Enable unusual time-of-day detection for this area', false)}
      </div>
      ${enabled ? timeBody(rule, zoneIndex) : '<p class="muted tripwire-hint tripwire-hint-off">Turn this on to learn which hours this area is normally active and get alerted when something shows up at an odd hour.</p>'}
    </div>`;
}

function renderZones() {
  if (!selectedCamera) return;
  syncZoneOverlayToImage();
  const zones = cameraDetection().zones;
  zones.forEach(normalizeZone);
  liveEls.zoneOverlay.innerHTML = zones.map((zone, index) => (zone.enabled === false ? '' : renderZoneBox(zone, index))).join('');
  updateZonesStats();
  if (!zones.length) {
    liveEls.zoneList.innerHTML = '<div class="empty">No Zone Areas yet. Click "Draw polygon", place corner dots on the footage, then click the first dot to close the area - or add the whole frame at once.</div>';
    renderObjectDetectionRules();
    return;
  }
  liveEls.zoneList.innerHTML = zones.map((zone, index) => {
    const fullFrame = isFullFrameZone(zone);
    const zoneLabel = escapeHtml(zone.name || `Zone ${index + 1}`);
    const hasUndo = Boolean(zone._previousPoints);
    const polygonTitle = hasUndo
      ? 'Restore the shape from before the last conversion'
      : 'Custom shape - drag corner dots or click a mid-edge dot to add a point';
    const shapeOption = (mode, label, active, title) => `
      <button type="button" class="zone-shape-option${active ? ' is-active' : ''}" data-zone-shape="${index}" data-zone-shape-mode="${mode}" aria-pressed="${active}" title="${title}">${label}</button>`;
    return `
    <div class="item zone-row ${index === selectedZoneIndex ? 'selected' : ''}${zone.enabled === false ? ' disabled' : ''}" data-select-zone="${index}">
      <div class="zone-row-main">
        <div class="zone-name-field">
          ${ICONS.edit}
          <input data-zone-name="${index}" value="${zoneLabel}" placeholder="Zone name…" aria-label="Name for ${zoneLabel}" />
        </div>
        <div class="zone-shape-field">
          <div class="zone-shape-head">
            <span>Shape</span>
            ${hasUndo ? `<button type="button" class="zone-shape-undo" data-undo-zone-shape="${index}" title="Restore the shape this area had before its last conversion">${ICONS.undo}Undo</button>` : ''}
          </div>
          <div class="zone-shape-toggle" role="group" aria-label="Shape for ${zoneLabel}">
            ${shapeOption('full', 'Full frame', fullFrame, 'Cover the whole camera frame')}
            ${shapeOption('polygon', 'Polygon', !fullFrame, polygonTitle)}
          </div>
        </div>
        <div class="zone-visibility-field">
          <span>Visibility</span>
          <label class="toggle-control zone-visibility-toggle ${zone.enabled !== false ? 'is-shown' : 'is-hidden'}" title="${zone.enabled !== false ? 'Hide this area on the preview' : 'Show this area on the preview'}">
            <input type="checkbox" data-zone-enabled="${index}" ${zone.enabled !== false ? 'checked' : ''} aria-label="${zone.enabled !== false ? 'Hide' : 'Show'} ${zoneLabel} area" />
            <span>${zone.enabled !== false ? 'Shown' : 'Hidden'}</span>
          </label>
        </div>
        <div class="zone-row-actions">
          <button class="btn-danger zone-action-btn" type="button" data-delete-zone="${index}">${ICONS.remove}Remove</button>
          <button class="primary zone-action-save" type="button" data-save-zone="${index}" title="Save all zone changes">
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.25" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M19 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h11l5 5v11a2 2 0 0 1-2 2z"/><path d="M17 21v-8H7v8"/><path d="M7 3v5h8"/></svg>
            Save
          </button>
        </div>
      </div>
    </div>
  `;
  }).join('');
  bindZoneControls(zones);
  renderObjectDetectionRules();
}

function renderObjectDetectionRules() {
  const container = document.getElementById('objectDetectionRules');
  if (!container) return;
  if (!selectedCamera) { container.innerHTML = ''; return; }
  const zones = cameraDetection().zones;
  if (!zones.length) {
    container.innerHTML = '<p class="muted empty-message">No Zone Areas configured. Draw an area above first.</p>';
    return;
  }
  container.innerHTML = zones.map((zone, zoneIndex) => {
    zone.object_rules = normalizeObjectRules(zone);
    const zoneName = escapeHtml(zone.name || `Zone ${zoneIndex + 1}`);
    const addOptions = objectRuleOptions('');
    // One table per area. Object rows come first, then the Motion and Face
    // rows (which are always present so they can be toggled on).
    return `
      <div class="zone-object-rules" data-zone-rules-for="${zoneIndex}">
        <div class="zone-rules-header">
          <div class="zone-name-card"><span class="zone-name-kicker">Area</span><strong>${zoneName}</strong></div>
          <label class="zone-rule-add">
            <span class="zone-rule-add-label">Add Object</span>
            <select data-add-zone-rule="${zoneIndex}" class="rule-add-select" aria-label="Add an object to ${zoneName}">${addOptions}</select>
          </label>
        </div>
        <div class="cameras-table-wrap">
          <table class="rule-table zone-rule-table">
            <thead><tr><th scope="col">Detection</th><th scope="col">Detect</th><th scope="col">Min confidence</th><th scope="col">Record</th><th scope="col" class="cell-actions" aria-label="Actions"></th></tr></thead>
            <tbody>
              ${renderObjectRules(zone, zoneIndex)}
              ${renderMotionCard(zone, zoneIndex)}
              ${renderFaceCard(zone, zoneIndex)}
            </tbody>
          </table>
        </div>
        ${renderTripwireCard(zone, zoneIndex)}
        ${renderLoiterCard(zone, zoneIndex)}
        ${renderTimeCard(zone, zoneIndex)}
      </div>`;
  }).join('');
  bindObjectRuleControls();
}

function bindObjectRuleControls() {
  document.querySelectorAll('[data-add-zone-rule]').forEach((select) => {
    select.addEventListener('change', () => {
      const label = select.value;
      if (!label) return;
      const zones = cameraDetection().zones;
      const zone = zones[Number(select.dataset.addZoneRule)];
      zone.object_rules = normalizeObjectRules(zone);
      zone.object_rules.push({ ...defaultObjectRule(label), id: `${label}-${Date.now()}-${Math.random().toString(36).slice(2, 7)}` });
      zone.object_labels = zone.object_rules.filter((r) => r.label !== 'motion' && r.label !== 'face').map((rule) => rule.label);
      renderZones();
      markZoneUnsaved();
    });
  });
  bindMotionControls();
  bindFaceControls();
  bindTripwireControls();
  bindLoiterControls();
  bindTimeControls();
  document.querySelectorAll('[data-delete-zone-rule]').forEach((button) => {
    button.addEventListener('click', () => {
      const zones = cameraDetection().zones;
      const { zoneIndex, ruleIndex } = parseZoneRuleKey(button.dataset.deleteZoneRule);
      const removedRule = zones[zoneIndex]?.object_rules?.[ruleIndex];
      if (!removedRule) return;
      const displayLabel = titleCase(removedRule.label || '');
      const enabledFlip = [];
      if (removedRule.enabled !== false) enabledFlip.push('detection');
      if (removedRule.record_on_detect !== false) enabledFlip.push('recording');
      if (removedRule.email_enabled === true) enabledFlip.push('email alerts');
      if (removedRule.push_enabled === true) enabledFlip.push('push notifications');
      const activeHint = enabledFlip.length
        ? ` This rule currently has ${enabledFlip.join(' and ')} enabled.`
        : '';
      if (!window.confirm(`Delete the ${displayLabel} rule from this zone?${activeHint}`)) return;
      expandedZoneRules.delete(button.dataset.deleteZoneRule);
      const removedLabel = String(removedRule.label || '').trim().toLowerCase();
      zones[zoneIndex].object_rules.splice(ruleIndex, 1);
      if (removedLabel === 'motion') {
        // Belt-and-suspenders alongside the normalizeZone() sync above: clear
        // the legacy flag immediately so the in-memory model is consistent
        // even if the next save path bypasses a renderZones() re-render.
        zones[zoneIndex].monitor_motion = false;
      }
      zones[zoneIndex].object_labels = zones[zoneIndex].object_rules.filter((r) => r.label !== 'motion').map((r) => r.label);
      renderZones();
      markZoneUnsaved();
    });
  });
  bindRuleFields();
}

// Motion card controls are limited to detection sensitivity and per-zone
// pixel gate/scale overrides, plus the Record toggle. Alert delivery,
// schedules, and cooldowns are edited on Alerts. Data attributes carry the
// bare zone index; the motion rule itself is looked up by label so reordering
// object rules never breaks these bindings.
function bindMotionControls() {
  document.querySelectorAll('[data-zone-motion-toggle]').forEach((cb) => {
    cb.addEventListener('change', () => {
      const zone = cameraDetection().zones[Number(cb.dataset.zoneMotionToggle)];
      if (!zone) return;
      const rule = motionRuleOf(zone);
      if (cb.checked) ensureMotionRule(zone).enabled = true;
      else if (rule) rule.enabled = false;
      // Keep the legacy flag in sync immediately; normalizeZone() also
      // derives it from the enabled motion rule on every render/save.
      zone.monitor_motion = cb.checked;
      zone.object_labels = zone.object_rules.filter((r) => r.label !== 'motion').map((r) => r.label);
      // Re-render so the sensitivity/gate body appears or collapses with the toggle.
      renderObjectDetectionRules();
      markZoneUnsaved();
    });
  });
  document.querySelectorAll('[data-zone-motion-record]').forEach((cb) => {
    cb.addEventListener('change', () => {
      const rule = motionRuleOf(cameraDetection().zones[Number(cb.dataset.zoneMotionRecord)]);
      if (!rule) return;
      rule.record_on_detect = cb.checked;
      markZoneUnsaved();
    });
  });
  // Sensitivity number input: commit on change and refresh the pixel hint.
  document.querySelectorAll('[data-zone-motion-confidence]').forEach((inp) => {
    inp.addEventListener('change', () => {
      const zoneIndex = Number(inp.dataset.zoneMotionConfidence);
      const rule = motionRuleOf(cameraDetection().zones[zoneIndex]);
      if (!rule) return;
      rule.min_confidence = clamp(Number(inp.value || 0.45), 0, 1);
      inp.value = rule.min_confidence;
      const help = document.querySelector(`[data-zone-motion-pixel-help="${zoneIndex}"]`);
      if (help) help.textContent = motionPixelThresholdText(rule);
      markZoneUnsaved();
    });
  });
  // "Advanced" expander per motion row: toggles the hidden gate/scale row.
  document.querySelectorAll('[data-zone-motion-advanced-toggle]').forEach((btn) => {
    btn.addEventListener('click', () => {
      const row = document.querySelector(`[data-zone-motion-advanced-for="${btn.dataset.zoneMotionAdvancedToggle}"]`);
      if (!row) return;
      const show = row.hasAttribute('hidden');
      if (show) row.removeAttribute('hidden'); else row.setAttribute('hidden', '');
      btn.setAttribute('aria-expanded', String(show));
      btn.classList.toggle('is-open', show);
    });
  });
  // Per-zone motion gate/scale overrides. Blank clears the override (inherit).
  [
    ['zoneMotionGate', 'gate_fraction', 0.0001, 0.5],
    ['zoneMotionScale', 'scale_fraction', 0.001, 1.0],
  ].forEach(([datasetKey, ruleKey, min, max]) => {
    const attr = `input[data-${datasetKey.replace(/[A-Z]/g, (m) => `-${m.toLowerCase()}`)}]`;
    document.querySelectorAll(attr).forEach((inp) => {
      inp.addEventListener('change', () => {
        const zoneIndex = Number(inp.dataset[datasetKey]);
        const rule = motionRuleOf(cameraDetection().zones[zoneIndex]);
        if (!rule) return;
        rule[ruleKey] = optionalFraction(inp.value, min, max);
        // Refresh the "% of pixels must change" hint so it reflects the override.
        const help = document.querySelector(`[data-zone-motion-pixel-help="${zoneIndex}"]`);
        if (help) help.textContent = motionPixelThresholdText(rule);
        markZoneUnsaved();
      });
    });
  });
}


// Face-card bindings edit face detection scope, sensitivity, and the Record
// toggle. Alert delivery, schedules, and cooldowns are edited on Alerts.
function bindFaceControls() {
  document.querySelectorAll('[data-zone-face-toggle]').forEach((cb) => {
    cb.addEventListener('change', () => {
      const zone = cameraDetection().zones[Number(cb.dataset.zoneFaceToggle)];
      if (!zone) return;
      if (cb.checked) ensureFaceRule(zone).enabled = true;
      else {
        const rule = faceRuleOf(zone);
        if (rule) rule.enabled = false;
      }
      // Re-render so the confidence body appears or collapses with the toggle.
      renderObjectDetectionRules();
      markZoneUnsaved();
    });
  });
  document.querySelectorAll('[data-zone-face-record]').forEach((cb) => {
    cb.addEventListener('change', () => {
      const rule = faceRuleOf(cameraDetection().zones[Number(cb.dataset.zoneFaceRecord)]);
      if (!rule) return;
      rule.record_on_detect = cb.checked;
      markZoneUnsaved();
    });
  });
  document.querySelectorAll('[data-zone-face-confidence]').forEach((inp) => {
    inp.addEventListener('change', () => {
      const rule = faceRuleOf(cameraDetection().zones[Number(inp.dataset.zoneFaceConfidence)]);
      if (!rule) return;
      rule.min_confidence = clamp(Number(inp.value || 0.45), 0, 1);
      inp.value = rule.min_confidence;
      markZoneUnsaved();
    });
  });
}

// Line-crossing (tripwire) card bindings. The enable toggle, direction, and
// label edits re-render (so the canvas line/arrow and the card body update
// together); the text/number inputs mutate in place without a re-render so an
// open field keeps focus while typing. The tripwire itself is looked up by
// zone, mirroring the motion/face card pattern.
function bindTripwireControls() {
  const zoneAt = (index) => cameraDetection().zones[Number(index)];

  document.querySelectorAll('[data-tripwire-enabled]').forEach((cb) => {
    cb.addEventListener('change', () => {
      const zone = zoneAt(cb.dataset.tripwireEnabled);
      if (!zone) return;
      selectedZoneIndex = Number(cb.dataset.tripwireEnabled);
      if (cb.checked) {
        ensureTripwire(zone);
        liveEls.status.textContent = 'Line added - drag its two dots on the footage to position it, then Save Zones.';
      } else if (tripwireOf(zone)) {
        zone.tripwire.enabled = false;
      }
      renderZones();
      markZoneUnsaved();
    });
  });

  document.querySelectorAll('[data-tripwire-direction-set]').forEach((button) => {
    button.addEventListener('click', () => {
      const group = button.closest('[data-tripwire-direction-for]');
      const zone = group ? zoneAt(group.dataset.tripwireDirectionFor) : null;
      const wire = zone && tripwireOf(zone);
      if (!wire) return;
      wire.direction = button.dataset.tripwireDirectionSet;
      selectedZoneIndex = Number(group.dataset.tripwireDirectionFor);
      renderZones();
      markZoneUnsaved();
    });
  });

  document.querySelectorAll('[data-tripwire-label-add]').forEach((select) => {
    select.addEventListener('change', () => {
      const label = String(select.value || '').trim().toLowerCase();
      if (!label) return;
      const wire = tripwireOf(zoneAt(select.dataset.tripwireLabelAdd));
      if (!wire) return;
      wire.labels = Array.isArray(wire.labels) ? wire.labels : [];
      if (!wire.labels.includes(label)) wire.labels.push(label);
      renderObjectDetectionRules();
      markZoneUnsaved();
    });
  });

  document.querySelectorAll('[data-tripwire-label-remove]').forEach((button) => {
    button.addEventListener('click', () => {
      const [zoneIndex, labelIndex] = String(button.dataset.tripwireLabelRemove).split(':').map(Number);
      const wire = tripwireOf(zoneAt(zoneIndex));
      if (!wire || !Array.isArray(wire.labels)) return;
      wire.labels.splice(labelIndex, 1);
      renderObjectDetectionRules();
      markZoneUnsaved();
    });
  });

  document.querySelectorAll('[data-tripwire-name]').forEach((inp) => {
    inp.addEventListener('input', () => {
      const wire = tripwireOf(zoneAt(inp.dataset.tripwireName));
      if (!wire) return;
      wire.name = inp.value;
      markZoneUnsaved();
    });
  });

  // Record is the only delivery-adjacent toggle kept on the Zones card
  // (recording is a detection concern, like the object/sound rows); email,
  // push, recipients and quiet-hours are configured on the Alerts page.
  document.querySelectorAll('[data-tripwire-record]').forEach((cb) => {
    cb.addEventListener('change', () => {
      const wire = tripwireOf(zoneAt(cb.dataset.tripwireRecord));
      if (!wire) return;
      wire.record_on_detect = cb.checked;
      // Flip the pill's On/Off label live without a full re-render.
      const pill = cb.parentElement?.querySelector('span');
      if (pill) pill.textContent = cb.checked ? 'On' : 'Off';
      markZoneUnsaved();
    });
  });
}

// Loitering card bindings. Like the tripwire card, the enable toggle and label
// edits re-render (so the card body updates) while the text/number inputs
// mutate in place to keep focus; delivery lives on the Alerts page.
function bindLoiterControls() {
  const zoneAt = (index) => cameraDetection().zones[Number(index)];

  document.querySelectorAll('[data-loiter-enabled]').forEach((cb) => {
    cb.addEventListener('change', () => {
      const zone = zoneAt(cb.dataset.loiterEnabled);
      if (!zone) return;
      selectedZoneIndex = Number(cb.dataset.loiterEnabled);
      if (cb.checked) {
        ensureLoiter(zone);
        liveEls.status.textContent = 'Loitering enabled - it will learn this area\'s normal dwell, then Save Zones.';
      } else if (loiterOf(zone)) {
        zone.loiter.enabled = false;
      }
      renderObjectDetectionRules();
      markZoneUnsaved();
    });
  });

  document.querySelectorAll('[data-loiter-label-add]').forEach((select) => {
    select.addEventListener('change', () => {
      const label = String(select.value || '').trim().toLowerCase();
      if (!label) return;
      const rule = loiterOf(zoneAt(select.dataset.loiterLabelAdd));
      if (!rule) return;
      rule.labels = Array.isArray(rule.labels) ? rule.labels : [];
      if (!rule.labels.includes(label)) rule.labels.push(label);
      renderObjectDetectionRules();
      markZoneUnsaved();
    });
  });

  document.querySelectorAll('[data-loiter-label-remove]').forEach((button) => {
    button.addEventListener('click', () => {
      const [zoneIndex, labelIndex] = String(button.dataset.loiterLabelRemove).split(':').map(Number);
      const rule = loiterOf(zoneAt(zoneIndex));
      if (!rule || !Array.isArray(rule.labels)) return;
      rule.labels.splice(labelIndex, 1);
      renderObjectDetectionRules();
      markZoneUnsaved();
    });
  });

  document.querySelectorAll('[data-loiter-name]').forEach((inp) => {
    inp.addEventListener('input', () => {
      const rule = loiterOf(zoneAt(inp.dataset.loiterName));
      if (!rule) return;
      rule.name = inp.value;
      markZoneUnsaved();
    });
  });

  document.querySelectorAll('[data-loiter-min-dwell]').forEach((inp) => {
    inp.addEventListener('change', () => {
      const rule = loiterOf(zoneAt(inp.dataset.loiterMinDwell));
      if (!rule) return;
      const value = Math.max(1, Number.parseInt(inp.value, 10) || 1);
      rule.min_dwell_seconds = value;
      inp.value = value;
      markZoneUnsaved();
    });
  });

  document.querySelectorAll('[data-loiter-sensitivity]').forEach((inp) => {
    inp.addEventListener('change', () => {
      const rule = loiterOf(zoneAt(inp.dataset.loiterSensitivity));
      if (!rule) return;
      const raw = Number(inp.value);
      const value = Number.isFinite(raw) ? Math.max(0, Math.min(10, raw)) : 3;
      rule.sensitivity = value;
      inp.value = value;
      markZoneUnsaved();
    });
  });

  document.querySelectorAll('[data-loiter-record]').forEach((cb) => {
    cb.addEventListener('change', () => {
      const rule = loiterOf(zoneAt(cb.dataset.loiterRecord));
      if (!rule) return;
      rule.record_on_detect = cb.checked;
      const pill = cb.parentElement?.querySelector('span');
      if (pill) pill.textContent = cb.checked ? 'On' : 'Off';
      markZoneUnsaved();
    });
  });
}

// Unusual time-of-day card bindings, mirroring the loiter card: enable + label
// edits re-render; text/number inputs mutate in place; delivery on Alerts.
function bindTimeControls() {
  const zoneAt = (index) => cameraDetection().zones[Number(index)];

  document.querySelectorAll('[data-time-enabled]').forEach((cb) => {
    cb.addEventListener('change', () => {
      const zone = zoneAt(cb.dataset.timeEnabled);
      if (!zone) return;
      selectedZoneIndex = Number(cb.dataset.timeEnabled);
      if (cb.checked) {
        ensureTime(zone);
        liveEls.status.textContent = 'Unusual time enabled - it will learn this area\'s normal hours (about a week), then Save Zones.';
      } else if (timeOf(zone)) {
        zone.time_of_day.enabled = false;
      }
      renderObjectDetectionRules();
      markZoneUnsaved();
    });
  });

  document.querySelectorAll('[data-time-label-add]').forEach((select) => {
    select.addEventListener('change', () => {
      const label = String(select.value || '').trim().toLowerCase();
      if (!label) return;
      const rule = timeOf(zoneAt(select.dataset.timeLabelAdd));
      if (!rule) return;
      rule.labels = Array.isArray(rule.labels) ? rule.labels : [];
      if (!rule.labels.includes(label)) rule.labels.push(label);
      renderObjectDetectionRules();
      markZoneUnsaved();
    });
  });

  document.querySelectorAll('[data-time-label-remove]').forEach((button) => {
    button.addEventListener('click', () => {
      const [zoneIndex, labelIndex] = String(button.dataset.timeLabelRemove).split(':').map(Number);
      const rule = timeOf(zoneAt(zoneIndex));
      if (!rule || !Array.isArray(rule.labels)) return;
      rule.labels.splice(labelIndex, 1);
      renderObjectDetectionRules();
      markZoneUnsaved();
    });
  });

  document.querySelectorAll('[data-time-name]').forEach((inp) => {
    inp.addEventListener('input', () => {
      const rule = timeOf(zoneAt(inp.dataset.timeName));
      if (!rule) return;
      rule.name = inp.value;
      markZoneUnsaved();
    });
  });

  document.querySelectorAll('[data-time-threshold]').forEach((inp) => {
    inp.addEventListener('change', () => {
      const rule = timeOf(zoneAt(inp.dataset.timeThreshold));
      if (!rule) return;
      // Shown as a percentage; stored as a 0..1 fraction.
      const percent = Number(inp.value);
      const clamped = Number.isFinite(percent) ? Math.max(0, Math.min(100, percent)) : 15;
      rule.threshold = Math.round(clamped) / 100;
      inp.value = Math.round(clamped);
      markZoneUnsaved();
    });
  });

  document.querySelectorAll('[data-time-record]').forEach((cb) => {
    cb.addEventListener('change', () => {
      const rule = timeOf(zoneAt(cb.dataset.timeRecord));
      if (!rule) return;
      rule.record_on_detect = cb.checked;
      const pill = cb.parentElement?.querySelector('span');
      if (pill) pill.textContent = cb.checked ? 'On' : 'Off';
      markZoneUnsaved();
    });
  });
}

function parseZoneRuleKey(value) {
  const [zoneIndex, ruleIndex] = String(value).split(':').map((part) => Number.parseInt(part, 10));
  return { zoneIndex, ruleIndex, rule: cameraDetection().zones[zoneIndex]?.object_rules?.[ruleIndex] };
}

function bindZoneControls(zones) {
  document.querySelectorAll('[data-zone-name]').forEach((input) => {
    input.addEventListener('focus', () => { selectedZoneIndex = Number(input.dataset.zoneName); updateSelectionStyles(); });
    input.addEventListener('input', () => {
      const index = Number(input.dataset.zoneName);
      zones[index].name = input.value;
      const label = liveEls.zoneOverlay.querySelector(`.zone-label[data-zone-index="${index}"]`);
      const nameSpan = label?.querySelector('.zone-label-name');
      if (nameSpan) nameSpan.textContent = input.value || `Zone ${index + 1}`;
      markZoneUnsaved();
    });
  });
  document.querySelectorAll('[data-zone-enabled]').forEach((checkbox) => {
    checkbox.addEventListener('change', () => {
      const index = Number(checkbox.dataset.zoneEnabled);
      selectedZoneIndex = index;
      zones[index].enabled = checkbox.checked;
      renderZones();
      refreshFrame();
      markZoneUnsaved();
    });
  });
  // Per-area Save: same operation as the footer/header Save buttons - the whole
  // camera's zone config is persisted at once.
  document.querySelectorAll('[data-save-zone]').forEach((button) => {
    button.addEventListener('click', () => saveZones());
  });
  // Switch an existing zone between the full frame and a custom polygon shape.
  // Converting to full frame applies immediately with NO confirm dialog: the
  // previous shape is remembered first, so the Undo button and the Polygon
  // option can always bring it back. "Softer" selections just ready the zone
  // for reshaping, which every zone supports via its corner dots and mid-edge
  // + handles.
  document.querySelectorAll('[data-zone-shape]').forEach((button) => {
    button.addEventListener('click', () => {
      const index = Number(button.dataset.zoneShape);
      const zone = zones[index];
      if (!zone) return;
      const mode = button.dataset.zoneShapeMode;
      if (mode === 'full' && !isFullFrameZone(zone)) {
        convertZoneToFullFrame(zone);
        selectedZoneIndex = index;
        renderZones();
        refreshFrame();
        markZoneUnsaved();
        liveEls.status.textContent = 'Zone converted to full frame - click Save Zones to apply.';
      } else if (mode === 'polygon' && isFullFrameZone(zone)) {
        // Restore the pre-conversion shape when one is remembered, otherwise
        // just select the zone so its reshape handles appear.
        if (!restorePreviousZoneShape(index)) {
          selectedZoneIndex = index;
          renderZones();
          liveEls.status.textContent = 'Drag a corner dot or click a mid-edge "+" to reshape this zone.';
        }
      }
    });
  });
  document.querySelectorAll('[data-undo-zone-shape]').forEach((button) => {
    button.addEventListener('click', () => {
      restorePreviousZoneShape(Number(button.dataset.undoZoneShape));
    });
  });
  document.querySelectorAll('[data-delete-zone]').forEach((button) => {
    button.addEventListener('click', () => {
      const index = Number(button.dataset.deleteZone);
      const zone = zones[index];
      const ruleCount = Array.isArray(zone?.object_rules) ? zone.object_rules.length : 0;
      const label = escapeHtml(zone?.name || `Zone ${index + 1}`);
      const ruleHint = ruleCount
        ? ` This zone has ${ruleCount} object rule${ruleCount === 1 ? '' : 's'} that will also be deleted.`
        : '';
      if (!window.confirm(`Delete the ${label} area?${ruleHint}`)) return;
      zones.splice(index, 1);
      selectedZoneIndex = null;
      renderZones();
      refreshFrame();
      markZoneUnsaved();
    });
  });
  document.querySelectorAll('[data-select-zone]').forEach((row) => {
    row.addEventListener('click', (event) => {
      if (event.target.closest('input, select, button')) return;
      selectedZoneIndex = Number(row.dataset.selectZone);
      renderZones();
    });
  });
}

function bindRuleFields() {
  const checkboxBindings = [
    ['zoneRuleEnabled', 'enabled'],
    ['zoneRuleRecord', 'record_on_detect'],
  ];
  checkboxBindings.forEach(([datasetKey, ruleKey]) => {
    document.querySelectorAll(`input[type="checkbox"][data-${datasetKey.replace(/[A-Z]/g, (m) => `-${m.toLowerCase()}`)}]`).forEach((cb) => {
      cb.addEventListener('change', () => {
        const { zoneIndex, rule } = parseZoneRuleKey(cb.dataset[datasetKey]);
        if (!rule) return;
        rule[ruleKey] = cb.checked;
        cameraDetection().zones[zoneIndex].object_labels = normalizeObjectRules(cameraDetection().zones[zoneIndex]).filter((item) => item.label !== 'motion').map((item) => item.label);
        // Re-render so the row's On/Off pill and enabled styling update live.
        renderObjectDetectionRules();
        markZoneUnsaved();
      });
    });
  });
  // Min-confidence control: number input with clamping (0.01-1).
  const MIN_CONF = 0.01;
  document.querySelectorAll('input[data-zone-rule-confidence-value]').forEach((inp) => {
    inp.addEventListener('change', () => {
      const key = inp.dataset.zoneRuleConfidenceValue;
      const { zoneIndex, rule } = parseZoneRuleKey(key);
      if (!rule) return;
      const value = clamp(Number(inp.value) || MIN_CONF, MIN_CONF, 1);
      rule.min_confidence = value;
      inp.value = value;
      cameraDetection().zones[zoneIndex].object_labels = normalizeObjectRules(cameraDetection().zones[zoneIndex]).filter((item) => item.label !== 'motion').map((item) => item.label);
      markZoneUnsaved();
    });
  });
  const numberBindings = [];
  // Note: ``max_confidence`` is intentionally not exposed in the GUI -- the
  // frontend always writes the 1.0 (no upper limit) default so rules keep
  // their legacy behavior. The backend still normalizes and honors the field
  // (and the AlertEngine gates on it) for any config that sets it via API.
  numberBindings.forEach(([datasetKey, ruleKey, transform]) => {
    document.querySelectorAll(`input[type="number"][data-${datasetKey.replace(/[A-Z]/g, (m) => `-${m.toLowerCase()}`)}]`).forEach((inp) => {
      inp.addEventListener('change', () => {
        const { zoneIndex, rule } = parseZoneRuleKey(inp.dataset[datasetKey]);
        if (!rule) return;
        rule[ruleKey] = transform(inp.value);
        cameraDetection().zones[zoneIndex].object_labels = normalizeObjectRules(cameraDetection().zones[zoneIndex]).filter((item) => item.label !== 'motion').map((item) => item.label);
        markZoneUnsaved();
      });
    });
  });
}

function pointFromEvent(event) {
  const rect = liveEls.zoneOverlay.getBoundingClientRect();
  return { x: clamp((event.clientX - rect.left) / rect.width), y: clamp((event.clientY - rect.top) / rect.height) };
}

function pointDistancePx(first, second, rect) {
  const dx = (first.x - second.x) * rect.width;
  const dy = (first.y - second.y) * rect.height;
  return Math.sqrt((dx * dx) + (dy * dy));
}

function updateDraggedZone(event) {
  if (!zoneDrag) return;
  const point = pointFromEvent(event);
  const zone = cameraDetection().zones[zoneDrag.index];
  if (!zone) return;
  const dx = point.x - zoneDrag.startPoint.x;
  const dy = point.y - zoneDrag.startPoint.y;
  if (zoneDrag.mode === 'move') {
    const xs = zoneDrag.startPoints.map((startPoint) => startPoint.x);
    const ys = zoneDrag.startPoints.map((startPoint) => startPoint.y);
    const safeDx = clamp(dx, -Math.min(...xs), 1 - Math.max(...xs));
    const safeDy = clamp(dy, -Math.min(...ys), 1 - Math.max(...ys));
    zone.points = zoneDrag.startPoints.map((startPoint) => ({ x: roundCoord(startPoint.x + safeDx), y: roundCoord(startPoint.y + safeDy) }));
  } else if (zoneDrag.mode === 'point') {
    zone.points[zoneDrag.pointIndex] = normalizePoint(point);
  } else if (zoneDrag.mode === 'tripwire') {
    const wire = tripwireOf(zone);
    if (!wire) return;
    wire[zoneDrag.end] = normalizePoint(point);
  }
  normalizeZone(zone);
  renderZones();
  markZoneUnsaved();
}

function draftPolygonMarkup() {
  if (!draftPolygon?.points.length) return '';
  const points = [...draftPolygon.points, draftPolygon.preview].filter(Boolean);
  const pointList = points.map((point) => `${point.x * 100},${point.y * 100}`).join(' ');
  const completedPointList = draftPolygon.points.map((point) => `${point.x * 100},${point.y * 100}`).join(' ');
  const handles = draftPolygon.points.map((point, index) => {
    const closesShape = index === 0 && draftPolygon.points.length >= 3;
    return `<i class="zone-handle zone-point-handle draft-point${closesShape ? ' close-draft-point' : ''}" ${closesShape ? 'data-close-draft="true" title="Close area"' : ''} style="left:${point.x * 100}%;top:${point.y * 100}%"></i>`;
  }).join('');
  return `
    <svg class="monitor-zone-polygon draft" viewBox="0 0 100 100" preserveAspectRatio="none">
      ${draftPolygon.points.length >= 3 ? `<polygon class="draft-fill" points="${completedPointList}"></polygon>` : ''}
      <polyline points="${pointList}"></polyline>
    </svg>
    ${handles}
  `;
}

function renderDraftPolygon() {
  liveEls.zoneOverlay.querySelectorAll('.draft, .draft-point').forEach((element) => element.remove());
  liveEls.zoneOverlay.insertAdjacentHTML('beforeend', draftPolygonMarkup());
}

function finishDraftPolygon() {
  if (!draftPolygon || draftPolygon.points.length < 3) return;
  const zones = cameraDetection().zones;
  zones.push({
    id: `zone-${Date.now()}`,
    name: `Zone ${zones.length + 1}`,
    points: draftPolygon.points.map(normalizePoint),
    enabled: true,
    object_labels: [],
    object_rules: [],
  });
  selectedZoneIndex = zones.length - 1;
  normalizeZone(zones[selectedZoneIndex]);
  draftPolygon = null;
  drawingMode = false;
  setAddZoneLabel('Draw Polygon');
  renderZones();
  refreshFrame();
  markZoneUnsaved();
}

function addFullFrameZone() {
  if (!selectedCamera) return;
  const zones = cameraDetection().zones;
  zones.push({
    id: `zone-${Date.now()}`,
    name: `Zone ${zones.length + 1}`,
    points: [
      { x: 0, y: 0 },
      { x: 1, y: 0 },
      { x: 1, y: 1 },
      { x: 0, y: 1 },
    ],
    enabled: true,
    object_labels: [],
    object_rules: [],
  });
  selectedZoneIndex = zones.length - 1;
  draftPolygon = null;
  drawingMode = false;
  zoneDrag = null;
  setAddZoneLabel('Draw Polygon');
  normalizeZone(zones[selectedZoneIndex]);
  renderZones();
  refreshFrame();
  markZoneUnsaved();
}

// eslint-disable-next-line no-unused-vars -- ESLint: exported for earlier scripts (live.js hooks)
function bindZoneDrawing() {
  if (!liveEls.zoneOverlay) return;
  liveEls.zoneOverlay.addEventListener('pointerdown', (event) => {
    if (!selectedCamera) return;
    if (drawingMode) {
      event.preventDefault();
      const point = pointFromEvent(event);
      const firstPoint = draftPolygon?.points[0];
      const overlayRect = liveEls.zoneOverlay.getBoundingClientRect();
      const closeToFirstPoint = firstPoint && draftPolygon.points.length >= 3 && pointDistancePx(point, firstPoint, overlayRect) <= CLOSE_DRAFT_DISTANCE_PX;
      if (event.target.closest('[data-close-draft]') || closeToFirstPoint) {
        finishDraftPolygon();
        return;
      }
      draftPolygon ||= { points: [], preview: point };
      draftPolygon.points.push(point);
      draftPolygon.preview = point;
      setAddZoneLabel(draftPolygon.points.length >= 3 ? 'Finish Area' : 'Cancel Drawing');
      renderDraftPolygon();
      liveEls.zoneOverlay.setPointerCapture(event.pointerId);
      return;
    }
    const addPointHandle = event.target.closest('[data-add-point]');
    if (addPointHandle) {
      event.preventDefault();
      const [zoneIndex, edgeIndex] = addPointHandle.dataset.addPoint.split(':').map(Number);
      const zone = cameraDetection().zones[zoneIndex];
      const current = zone?.points?.[edgeIndex];
      const next = zone?.points?.[(edgeIndex + 1) % zone.points.length];
      if (current && next) {
        zone.points.splice(edgeIndex + 1, 0, {
          x: (current.x + next.x) / 2,
          y: (current.y + next.y) / 2,
        });
        normalizeZone(zone);
        selectedZoneIndex = zoneIndex;
        renderZones();
        markZoneUnsaved();
      }
      return;
    }
    const tripwireHandle = event.target.closest('[data-tripwire-end]');
    if (tripwireHandle) {
      event.preventDefault();
      const index = Number(tripwireHandle.dataset.tripwireIndex);
      const zone = cameraDetection().zones[index];
      if (zone && tripwireOf(zone)) {
        selectedZoneIndex = index;
        zoneDrag = {
          index,
          mode: 'tripwire',
          end: tripwireHandle.dataset.tripwireEnd,
          startPoint: pointFromEvent(event),
        };
        liveEls.zoneOverlay.setPointerCapture(event.pointerId);
      }
      return;
    }
    const pointHandle = event.target.closest('[data-point-index]');
    const zoneBox = event.target.closest('.monitor-zone-polygon[data-zone-index], .zone-label[data-zone-index], polygon[data-zone-index]');
    if (pointHandle || zoneBox) {
      event.preventDefault();
      const index = Number((pointHandle || zoneBox).dataset.zoneIndex);
      const zone = cameraDetection().zones[index];
      selectedZoneIndex = index;
      zoneDrag = {
        index,
        mode: pointHandle ? 'point' : 'move',
        pointIndex: pointHandle ? Number(pointHandle.dataset.pointIndex) : null,
        startPoint: pointFromEvent(event),
        startPoints: zone.points.map((zonePoint) => ({ ...zonePoint })),
      };
      liveEls.zoneOverlay.setPointerCapture(event.pointerId);
      renderZones();
    }
  });
  liveEls.zoneOverlay.addEventListener('pointermove', (event) => {
    if (zoneDrag) {
      updateDraggedZone(event);
      return;
    }
    if (!draftPolygon) return;
    draftPolygon.preview = pointFromEvent(event);
    renderDraftPolygon();
  });
  liveEls.zoneOverlay.addEventListener('pointerup', (event) => {
    if (zoneDrag) {
      updateDraggedZone(event);
      zoneDrag = null;
      renderZones();
    }
  });
  liveEls.zoneOverlay.addEventListener('pointercancel', () => {
    zoneDrag = null;
    renderZones();
    if (draftPolygon) renderDraftPolygon();
  });
  liveEls.zoneOverlay.addEventListener('dblclick', (event) => {
    if (drawingMode) return;
    const pointHandle = event.target.closest('[data-point-index]');
    if (!pointHandle) return;
    event.preventDefault();
    const zoneIndex = Number(pointHandle.dataset.zoneIndex);
    const pointIndex = Number(pointHandle.dataset.pointIndex);
    const zone = cameraDetection().zones[zoneIndex];
    // A polygon needs at least 3 vertices; dropping below that would collapse
    // the zone into a line and make the even-odd hit test meaningless.
    if (!zone || zone.points.length <= 3) return;
    zone.points.splice(pointIndex, 1);
    normalizeZone(zone);
    renderZones();
    markZoneUnsaved();
  });
}

function toggleDrawingMode() {
  if (drawingMode && draftPolygon?.points.length >= 3) {
    finishDraftPolygon();
    return;
  }
  drawingMode = !drawingMode;
  draftPolygon = null;
  zoneDrag = null;
  setAddZoneLabel(drawingMode ? 'Cancel Drawing' : 'Draw Polygon');
  renderZones();
}

liveEls.addZoneBtn?.addEventListener('click', toggleDrawingMode);

liveEls.fullFrameZoneBtn?.addEventListener('click', () => {
  addFullFrameZone();
  liveEls.status.textContent = 'Full-frame zone added - click Save Zones to apply.';
});

let hasUnsavedZoneChanges = false;

function markZoneUnsaved() {
  if (hasUnsavedZoneChanges) return;
  hasUnsavedZoneChanges = true;
  const btn = document.getElementById('saveZonesBtnHeader');
  if (btn) btn.style.display = '';
  liveEls.status.textContent = 'Unsaved changes - click Save Zones to apply.';
  liveEls.status.classList.add('has-unsaved');
  // Reveal the per-area Save buttons while there is something to save.
  liveEls.zoneList?.classList.add('has-unsaved');
}

function markZoneSaved() {
  hasUnsavedZoneChanges = false;
  const btn = document.getElementById('saveZonesBtnHeader');
  if (btn) btn.style.display = 'none';
  liveEls.status.classList.remove('has-unsaved');
  liveEls.zoneList?.classList.remove('has-unsaved');
}

// Disable every Save control (header button, the detection-scope Save Zones
// button, and each per-area Save button) while a save is in flight. Look the
// buttons up live from the DOM so a missing one is simply skipped instead of
// throwing.
function setZoneSaving(saving) {
  document.querySelectorAll('#saveZonesBtnHeader, #saveZonesBtn, [data-save-zone]').forEach((btn) => {
    btn.disabled = saving;
  });
}

async function saveZones() {
  try {
    setZoneSaving(true);
    cameraDetection().zones.forEach(normalizeZone);
    await api(`/api/cameras/${encodeURIComponent(selectedCamera.id)}`, { method: 'PUT', body: JSON.stringify(selectedCamera) });
    const payload = await api('/api/cameras');
    const cameraId = selectedCamera.id;
    cameras = payload.cameras || [];
    setSelectedCamera(cameraId);
    markZoneSaved();
    liveEls.status.textContent = 'Zones saved successfully.';
    window.showToast?.('Zones saved successfully.');
    await refreshDetectionStatus();
  } catch (error) {
    // Skip UI updates if api() triggered a 401 redirect
    if (window.daygleAuth?.redirecting) return;
    liveEls.status.textContent = error.message;
    window.showToast?.(error.message, true);
  } finally {
    setZoneSaving(false);
  }
}

document.getElementById('saveZonesBtnHeader')?.addEventListener('click', saveZones);
document.getElementById('saveZonesBtn')?.addEventListener('click', saveZones);

window.addEventListener('resize', syncZoneOverlayToImage);

document.addEventListener('keydown', (event) => {
  if (event.key === 'Escape' && drawingMode) {
    drawingMode = false;
    draftPolygon = null;
    setAddZoneLabel('Draw Polygon');
    renderDraftPolygon();
  }
});

window.addEventListener('beforeunload', (event) => {
  if (!hasUnsavedZoneChanges) return;
  event.preventDefault();
  event.returnValue = '';
});

document.addEventListener('keydown', (event) => {
  if ((event.ctrlKey || event.metaKey) && event.key === 's') {
    event.preventDefault();
    if (hasUnsavedZoneChanges) saveZones();
  }
});

// Ctrl/Cmd+Z undoes the most recent shape conversion (full frame / rectangle).
// Inside an editable field it is left to the browser so typing can be undone.
document.addEventListener('keydown', (event) => {
  if (!(event.ctrlKey || event.metaKey) || event.shiftKey || event.altKey) return;
  if (String(event.key).toLowerCase() !== 'z') return;
  const target = event.target;
  if (target && typeof target.closest === 'function' && target.closest('input, textarea, select, [contenteditable]')) return;
  if (undoLastShapeConversion()) event.preventDefault();
});
