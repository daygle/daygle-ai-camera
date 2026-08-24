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

function normalizeObjectRules(zone) {
  if (Array.isArray(zone.object_rules) && zone.object_rules.length) {
    const seen = new Set();
    return zone.object_rules.map((rule) => ({ ...defaultObjectRule(rule?.label), ...rule }))
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
      }))
      .filter((rule) => {
        if (!rule.label || seen.has(rule.label)) return false;
        seen.add(rule.label);
        return true;
      });
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
  const alertCount = zones.reduce((sum, zone) => sum + (zone.object_rules || []).filter((r) => r.email_enabled || r.push_enabled).length, 0);
  if (liveEls.statZoneCount) liveEls.statZoneCount.textContent = String(zones.length);
  if (liveEls.statRuleCount) liveEls.statRuleCount.textContent = String(ruleCount);
  if (liveEls.statAlertRules) liveEls.statAlertRules.textContent = String(alertCount);
  if (liveEls.statCameraName) {
    liveEls.statCameraName.textContent = selectedCamera.name || selectedCamera.id || '-';
  }
  const zonesListCount = document.getElementById('zonesListCount');
  if (zonesListCount) {
    zonesListCount.textContent = `${zones.length} area${zones.length === 1 ? '' : 's'}`;
  }
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

function renderObjectRules(zone, zoneIndex) {
  zone.object_rules = normalizeObjectRules(zone);
  const rules = zone.object_rules
    .map((rule, ruleIndex) => ({ rule, ruleIndex }))
    .filter(({ rule }) => {
      const label = String(rule.label || '').trim().toLowerCase();
      return label !== 'motion' && label !== 'face';
    });
  if (!rules.length) {
    return '<div class="empty compact-empty">No object rules yet. Choose an object below to add detection settings for this area.</div>';
  }
  const cards = rules.map(({ rule, ruleIndex }) => {
    const key = `${zoneIndex}:${ruleIndex}`;
    const label = escapeHtml(titleCase(rule.label));
    const expanded = expandedZoneRules.has(key);
    const enabled = rule.enabled !== false;
    return `
      <div class="zone-motion-card${enabled ? ' is-enabled' : ''}">
        <div class="zone-motion-head">
          <div class="zone-motion-title">
            <span class="zone-motion-icon" aria-hidden="true">🔍</span>
            <div>
              <strong>${label}</strong>
              <span>Detect ${label.toLowerCase()} in this area</span>
            </div>
          </div>
          <label class="toggle-control zone-motion-toggle" title="Enable or disable ${label} detection for this area">
            <input type="checkbox" data-zone-rule-enabled="${key}" ${enabled ? 'checked' : ''} />
            <span>${enabled ? 'On' : 'Off'}</span>
          </label>
        </div>
        <div class="zone-motion-body zone-people-body">
          <div style="display:flex;align-items:center;gap:12px;flex-wrap:wrap">
            <label class="muted" style="font-size:13px;display:flex;gap:4px;align-items:center" title="Record a clip when ${label} is detected in this area">
              <input type="checkbox" data-zone-rule-record="${key}" ${rule.record_on_detect !== false ? 'checked' : ''} />📹 Record
            </label>
            <label class="muted" style="font-size:13px;display:flex;gap:4px;align-items:center" title="Email when ${label} is detected here">
              <input type="checkbox" data-zone-rule-email="${key}" ${rule.email_enabled === true ? 'checked' : ''} />📧 Email
            </label>
            <label class="muted" style="font-size:13px;display:flex;gap:4px;align-items:center" title="Push when ${label} is detected here">
              <input type="checkbox" data-zone-rule-push="${key}" ${rule.push_enabled === true ? 'checked' : ''} />🔔 Push
            </label>
            <button class="secondary rule-expand-btn" type="button" data-expand-zone-rule="${key}" title="Advanced settings for ${label}">${expanded ? ICONS.chevronUp : ICONS.email}<span>${expanded ? 'Hide' : 'Advanced'}</span></button>
          </div>
          <div style="display:flex;flex-wrap:wrap;gap:14px">
            <label class="sound-rule-field" title="Minimum confidence (0.01-1). Overrides the global ONNX slider for this object in this zone.">
              <span>Min Confidence</span>
              <input type="number" data-zone-rule-confidence-value="${key}" min="0.01" max="1" step="0.01" value="${escapeHtml(rule.min_confidence)}" style="width:90px" />
            </label>
            <label class="sound-rule-field" title="Cooldown: minimum seconds between detection events and alerts for this area.">
              <span>Cooldown (s)</span>
              <input type="number" data-zone-rule-cooldown="${key}" value="${escapeHtml(rule.cooldown_seconds)}" min="0" max="3600" step="5" style="width:90px" />
            </label>
          </div>
        </div>
        <div class="zone-motion-advanced-body" ${expanded ? '' : 'hidden'}>
          <label class="sound-rule-field" title="Comma-separated email recipients for ${label} alerts in this area.">
            <span>Recipients</span>
            <input type="email" data-zone-rule-email-recipients="${key}" value="${escapeHtml(normalizeEmailList(rule.email_recipients).join(', '))}" placeholder="alerts@example.com" multiple autocomplete="off" style="min-width:220px" />
          </label>
          <label class="sound-rule-field" title="Detection window: only detect between these times. Leave blank for all day.">
            <span>Active from</span>
            ${renderTimeSelect(rule.active_start, 'data-zone-rule-active-start', key)}
          </label>
          <label class="sound-rule-field" title="Detection window: stop detecting at this time. Leave blank for all day.">
            <span>Active to</span>
            ${renderTimeSelect(rule.active_end, 'data-zone-rule-active-end', key)}
          </label>
          <label class="sound-rule-field" title="Email/Push window: only send notifications between these times.">
            <span>Email/Push from</span>
            ${renderTimeSelect(rule.notify_start, 'data-zone-rule-notify-start', key)}
          </label>
          <label class="sound-rule-field" title="Email/Push window: stop sending notifications at this time.">
            <span>Email/Push to</span>
            ${renderTimeSelect(rule.notify_end, 'data-zone-rule-notify-end', key)}
          </label>
          <div style="width:100%;display:flex;justify-content:flex-end;padding-top:4px">
            <button class="delete-btn secondary zone-action-btn" type="button" data-delete-zone-rule="${key}" title="Delete ${label} rule from this zone">${ICONS.remove} Remove</button>
          </div>
        </div>
      </div>`;
  }).join('');
  return cards;
}

function renderMotionCard(zone, zoneIndex) {
  const rule = motionRuleOf(zone);
  const enabled = Boolean(rule && rule.enabled !== false);
  const key = `motion:${zoneIndex}`;
  const expanded = expandedZoneRules.has(key);
  const zoneLabel = escapeHtml(zone.name || `Zone ${zoneIndex + 1}`);
  return `
    <div class="zone-motion-card${enabled ? ' is-enabled' : ''}" data-zone-motion-for="${zoneIndex}">
      <div class="zone-motion-head">
        <div class="zone-motion-title">
          <span class="zone-motion-icon" aria-hidden="true">⟳</span>
          <div>
            <strong>Motion Detection</strong>
            <span>Detect any movement in this area</span>
          </div>
        </div>
        <label class="toggle-control zone-motion-toggle" title="Enable or disable motion detection for this area">
          <input type="checkbox" data-zone-motion-toggle="${zoneIndex}" ${enabled ? 'checked' : ''} aria-label="Toggle motion detection for ${zoneLabel}" />
          <span>${enabled ? 'On' : 'Off'}</span>
        </label>
      </div>
      ${enabled ? `
      <div class="zone-motion-body">
        <label class="zone-motion-field zone-motion-sensitivity" title="Sensitivity: only motion with at least this confidence counts (0-1). Drag left for more sensitive, right for less.">
          <span>Sensitivity</span>
          <span class="zone-motion-sensitivity-row">
            <input type="range" data-zone-motion-confidence="${zoneIndex}" min="0" max="1" step="0.05" value="${escapeHtml(rule.min_confidence)}" />
            <output class="zone-motion-sensitivity-value" data-zone-motion-confidence-value="${zoneIndex}">${escapeHtml(rule.min_confidence)}</output>
          </span>
          <small class="form-help muted zone-motion-pixel-help" data-zone-motion-pixel-help="${zoneIndex}">${escapeHtml(motionPixelThresholdText(rule))}</small>
        </label>
        <div class="zone-motion-secondary">
          <label class="zone-motion-field" title="Record a clip whenever motion is detected in this area.">
            <span>Record on motion</span>
            <input type="checkbox" data-zone-motion-record="${zoneIndex}" ${rule.record_on_detect !== false ? 'checked' : ''} />
          </label>
          <button class="secondary rule-expand-btn zone-motion-advanced" type="button" data-expand-zone-motion="${zoneIndex}" aria-expanded="${expanded}">
            ${expanded ? ICONS.chevronUp : ICONS.email}<span>${expanded ? 'Hide advanced' : 'Advanced'}</span>
          </button>
        </div>
      </div>
      <div class="zone-motion-advanced-body" ${expanded ? '' : 'hidden'}>
        <label class="sound-rule-field" title="Cooldown: minimum seconds between motion events and alerts for this area. Default 60.">
          <span>Cooldown (s)</span>
          <input type="number" data-zone-motion-cooldown="${zoneIndex}" value="${escapeHtml(rule.cooldown_seconds)}" min="0" max="3600" step="5" />
        </label>
        <label class="sound-rule-field" title="Per-zone gate: minimum fraction of THIS zone's pixels that must change before motion counts. Leave blank to use the camera/global gate. Lower = more sensitive for this zone only.">
          <span>Gate override</span>
          <input type="number" data-zone-motion-gate="${zoneIndex}" value="${rule.gate_fraction != null ? escapeHtml(rule.gate_fraction) : ''}" min="0.0001" max="0.5" step="0.0001" placeholder="Inherit" />
        </label>
        <label class="sound-rule-field" title="Per-zone scale: pixel-change fraction in THIS zone that maps to 100% motion confidence. Leave blank to use the camera/global scale. Lower = stronger confidence for small motion in this zone.">
          <span>Scale override</span>
          <input type="number" data-zone-motion-scale="${zoneIndex}" value="${rule.scale_fraction != null ? escapeHtml(rule.scale_fraction) : ''}" min="0.001" max="1.0" step="0.001" placeholder="Inherit" />
        </label>
        <label class="sound-rule-field" title="Send an email when motion is detected in this area. Add recipients in the Email recipients field below.">
          <span>Email alerts</span>
          <input type="checkbox" data-zone-motion-email="${zoneIndex}" ${rule.email_enabled === true ? 'checked' : ''} />
        </label>
        <label class="sound-rule-field" title="Send a push notification when motion is detected in this area.">
          <span>Push alerts</span>
          <input type="checkbox" data-zone-motion-push="${zoneIndex}" ${rule.push_enabled === true ? 'checked' : ''} />
        </label>
        ${renderRuleExpandFields('zone-motion', zoneIndex, rule)}
      </div>` : ''}
    </div>`;
}

function renderFaceCard(zone, zoneIndex) {
  const rule = faceRuleOf(zone);
  const enabled = Boolean(rule && rule.enabled !== false);
  const key = `face:${zoneIndex}`;
  const expanded = expandedZoneRules.has(key);
  const zoneLabel = escapeHtml(zone.name || `Zone ${zoneIndex + 1}`);
  return `
    <div class="zone-motion-card${enabled ? ' is-enabled' : ''}" data-zone-face-for="${zoneIndex}">
      <div class="zone-motion-head">
        <div class="zone-motion-title">
          <span class="zone-motion-icon" aria-hidden="true">👤</span>
          <div>
            <strong>Face Detection</strong>
            <span>Recognise faces inside this area only</span>
          </div>
        </div>
        <label class="toggle-control zone-motion-toggle" title="Enable or disable face detection for this area">
          <input type="checkbox" data-zone-face-toggle="${zoneIndex}" ${enabled ? 'checked' : ''} aria-label="Toggle face detection for ${zoneLabel}" />
          <span>${enabled ? 'On' : 'Off'}</span>
        </label>
      </div>
      ${enabled ? `
      <div class="zone-motion-body">
        <label class="zone-motion-field" title="Only faces with at least this confidence are processed in this area (0-1). Lower finds more faces, higher reduces false positives.">
          <span>Min confidence</span>
          <span class="zone-motion-sensitivity-row">
            <input type="range" data-zone-face-confidence="${zoneIndex}" min="0" max="1" step="0.05" value="${escapeHtml(rule.min_confidence)}" />
            <output class="zone-motion-sensitivity-value" data-zone-face-confidence-value="${zoneIndex}">${escapeHtml(rule.min_confidence)}</output>
          </span>
          <small class="form-help muted">Faces detected outside Face-enabled areas are ignored entirely.</small>
        </label>
        <div class="zone-motion-secondary">
          <label class="zone-motion-field" title="Record a clip whenever a face is detected in this area.">
            <span>Record on face</span>
            <input type="checkbox" data-zone-face-record="${zoneIndex}" ${rule.record_on_detect !== false ? 'checked' : ''} />
          </label>
          <button class="secondary rule-expand-btn zone-motion-advanced" type="button" data-expand-zone-face="${zoneIndex}" aria-expanded="${expanded}">
            ${expanded ? ICONS.chevronUp : ICONS.email}<span>${expanded ? 'Hide advanced' : 'Advanced'}</span>
          </button>
        </div>
      </div>
      <div class="zone-motion-advanced-body" ${expanded ? '' : 'hidden'}>
        <label class="sound-rule-field" title="Cooldown: minimum seconds between face events and alerts for this area. Default 60.">
          <span>Cooldown (s)</span>
          <input type="number" data-zone-face-cooldown="${zoneIndex}" value="${escapeHtml(rule.cooldown_seconds)}" min="0" max="3600" step="5" />
        </label>
        <label class="sound-rule-field" title="Send an email when a face is detected in this area. Add recipients below.">
          <span>Email alerts</span>
          <input type="checkbox" data-zone-face-email="${zoneIndex}" ${rule.email_enabled === true ? 'checked' : ''} />
        </label>
        <label class="sound-rule-field" title="Send a push notification when a face is detected in this area.">
          <span>Push alerts</span>
          <input type="checkbox" data-zone-face-push="${zoneIndex}" ${rule.push_enabled === true ? 'checked' : ''} />
        </label>
        ${renderRuleExpandFields('zone-face', zoneIndex, rule)}
      </div>` : ''}
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
    // Motion lives in its own card above; the object table only lists
    // object-class rules.
    const objectRuleCount = zone.object_rules.filter((rule) => {
      const label = String(rule.label || '').trim().toLowerCase();
      return label !== 'motion' && label !== 'face';
    }).length;
    const rulesHtml = objectRuleCount
      ? renderObjectRules(zone, zoneIndex)
      : '<p class="muted empty-message">No object rules yet. Choose an object below to add detection settings for this area.</p>';
    return `
      <div class="zone-object-rules" data-zone-rules-for="${zoneIndex}">
        <div class="zone-name-card"><span class="zone-name-kicker">Area</span><strong>${zoneName}</strong></div>
        ${renderMotionCard(zone, zoneIndex)}
        ${renderFaceCard(zone, zoneIndex)}
        ${renderPeopleCard(zone, zoneIndex)}
        <div class="zone-object-rules-header">
          <select data-add-zone-rule="${zoneIndex}" class="rule-add-select">${addOptions}</select>
        </div>
        ${rulesHtml}
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
      if (!zone.object_rules.some((rule) => rule.label === label)) zone.object_rules.push(defaultObjectRule(label));
      zone.object_labels = zone.object_rules.filter((r) => r.label !== 'motion' && r.label !== 'face').map((rule) => rule.label);
      renderZones();
      markZoneUnsaved();
    });
  });
  document.querySelectorAll('[data-expand-zone-rule]').forEach((btn) => {
    btn.addEventListener('click', () => {
      const key = btn.dataset.expandZoneRule;
      if (expandedZoneRules.has(key)) expandedZoneRules.delete(key);
      else expandedZoneRules.add(key);
      renderObjectDetectionRules();
    });
  });
  bindMotionControls();
  bindFaceControls();
  bindPeopleControls();
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

// Motion card controls: a single on/off toggle plus the essentials
// (record, sensitivity) and the advanced fields (email recipients + time
// windows) that reuse the shared renderRuleExpandFields markup. Data
// attributes carry the bare zone index; the motion rule itself is looked up
// by label so reordering object rules never breaks these bindings.
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
  // Sensitivity slider: `input` updates the live readout only, `change`
  // (released) commits the value to the rule.
  document.querySelectorAll('[data-zone-motion-confidence]').forEach((inp) => {
    inp.addEventListener('input', () => {
      const zoneIndex = Number(inp.dataset.zoneMotionConfidence);
      const readout = document.querySelector(`[data-zone-motion-confidence-value="${zoneIndex}"]`);
      if (readout) readout.textContent = inp.value;
      const help = document.querySelector(`[data-zone-motion-pixel-help="${zoneIndex}"]`);
      const rule = motionRuleOf(cameraDetection().zones[zoneIndex]);
      if (help && rule) help.textContent = motionPixelThresholdText({ ...rule, min_confidence: Number(inp.value) });
    });
    inp.addEventListener('change', () => {
      const rule = motionRuleOf(cameraDetection().zones[Number(inp.dataset.zoneMotionConfidence)]);
      if (!rule) return;
      rule.min_confidence = clamp(Number(inp.value || 0.45), 0, 1);
      markZoneUnsaved();
    });
  });
  document.querySelectorAll('[data-zone-motion-cooldown]').forEach((inp) => {
    inp.addEventListener('change', () => {
      const rule = motionRuleOf(cameraDetection().zones[Number(inp.dataset.zoneMotionCooldown)]);
      if (!rule) return;
      rule.cooldown_seconds = Math.max(0, Number.parseInt(inp.value || 0, 10) || 0);
      markZoneUnsaved();
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
  [
    ['zoneMotionEmail', 'email_enabled'],
    ['zoneMotionPush', 'push_enabled'],
  ].forEach(([datasetKey, ruleKey]) => {
    const attr = `input[type="checkbox"][data-${datasetKey.replace(/[A-Z]/g, (m) => `-${m.toLowerCase()}`)}]`;
    document.querySelectorAll(attr).forEach((cb) => {
      cb.addEventListener('change', () => {
        const rule = motionRuleOf(cameraDetection().zones[Number(cb.dataset[datasetKey])]);
        if (!rule) return;
        rule[ruleKey] = cb.checked;
        markZoneUnsaved();
      });
    });
  });
  document.querySelectorAll('[data-expand-zone-motion]').forEach((btn) => {
    btn.addEventListener('click', () => {
      const key = `motion:${btn.dataset.expandZoneMotion}`;
      if (expandedZoneRules.has(key)) expandedZoneRules.delete(key);
      else expandedZoneRules.add(key);
      renderObjectDetectionRules();
    });
  });
  document.querySelectorAll('[data-zone-motion-email-recipients]').forEach((input) => {
    input.addEventListener('change', () => {
      const rule = motionRuleOf(cameraDetection().zones[Number(input.dataset.zoneMotionEmailRecipients)]);
      if (!rule) return;
      rule.email_recipients = normalizeEmailList(input.value);
      markZoneUnsaved();
    });
  });
  [
    ['zoneMotionActiveStart', 'active_start'],
    ['zoneMotionActiveEnd', 'active_end'],
    ['zoneMotionNotifyStart', 'notify_start'],
    ['zoneMotionNotifyEnd', 'notify_end'],
  ].forEach(([datasetKey, ruleKey]) => {
    const attr = `data-${datasetKey.replace(/[A-Z]/g, (m) => `-${m.toLowerCase()}`)}`;
    document.querySelectorAll(`[${attr}]`).forEach((wrap) => {
      wrap.querySelectorAll('select').forEach((sel) => {
        sel.addEventListener('change', () => {
          const rule = motionRuleOf(cameraDetection().zones[Number(wrap.dataset[datasetKey])]);
          if (!rule) return;
          rule[ruleKey] = timeSelectValue(wrap);
          markZoneUnsaved();
        });
      });
    });
  });
}


// Face-card bindings: same structure as the motion card (data attributes carry
// the bare zone index; the face rule is looked up by label so reordering
// object rules never breaks these bindings).
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
    inp.addEventListener('input', () => {
      const zoneIndex = Number(inp.dataset.zoneFaceConfidence);
      const readout = document.querySelector(`[data-zone-face-confidence-value="${zoneIndex}"]`);
      if (readout) readout.textContent = inp.value;
    });
    inp.addEventListener('change', () => {
      const rule = faceRuleOf(cameraDetection().zones[Number(inp.dataset.zoneFaceConfidence)]);
      if (!rule) return;
      rule.min_confidence = clamp(Number(inp.value || 0.45), 0, 1);
      markZoneUnsaved();
    });
  });
  document.querySelectorAll('[data-zone-face-cooldown]').forEach((inp) => {
    inp.addEventListener('change', () => {
      const rule = faceRuleOf(cameraDetection().zones[Number(inp.dataset.zoneFaceCooldown)]);
      if (!rule) return;
      rule.cooldown_seconds = Math.max(0, Number.parseInt(inp.value || 0, 10) || 0);
      markZoneUnsaved();
    });
  });
  [
    ['zoneFaceEmail', 'email_enabled'],
    ['zoneFacePush', 'push_enabled'],
  ].forEach(([datasetKey, ruleKey]) => {
    const attr = `input[type="checkbox"][data-${datasetKey.replace(/[A-Z]/g, (m) => `-${m.toLowerCase()}`)}]`;
    document.querySelectorAll(attr).forEach((cb) => {
      cb.addEventListener('change', () => {
        const rule = faceRuleOf(cameraDetection().zones[Number(cb.dataset[datasetKey])]);
        if (!rule) return;
        rule[ruleKey] = cb.checked;
        markZoneUnsaved();
      });
    });
  });
  document.querySelectorAll('[data-expand-zone-face]').forEach((btn) => {
    btn.addEventListener('click', () => {
      const key = `face:${btn.dataset.expandZoneFace}`;
      if (expandedZoneRules.has(key)) expandedZoneRules.delete(key);
      else expandedZoneRules.add(key);
      renderObjectDetectionRules();
    });
  });
  document.querySelectorAll('[data-zone-face-email-recipients]').forEach((input) => {
    input.addEventListener('change', () => {
      const rule = faceRuleOf(cameraDetection().zones[Number(input.dataset.zoneFaceEmailRecipients)]);
      if (!rule) return;
      rule.email_recipients = normalizeEmailList(input.value);
      markZoneUnsaved();
    });
  });
  [
    ['zoneFaceActiveStart', 'active_start'],
    ['zoneFaceActiveEnd', 'active_end'],
    ['zoneFaceNotifyStart', 'notify_start'],
    ['zoneFaceNotifyEnd', 'notify_end'],
  ].forEach(([datasetKey, ruleKey]) => {
    const attr = `data-${datasetKey.replace(/[A-Z]/g, (m) => `-${m.toLowerCase()}`)}`;
    document.querySelectorAll(`[${attr}]`).forEach((wrap) => {
      wrap.querySelectorAll('select').forEach((sel) => {
        sel.addEventListener('change', () => {
          const rule = faceRuleOf(cameraDetection().zones[Number(wrap.dataset[datasetKey])]);
          if (!rule) return;
          rule[ruleKey] = timeSelectValue(wrap);
          markZoneUnsaved();
        });
      });
    });
  });
}

// ── People Detection card ────────────────────────────────────────────
// Per-zone known-person + stranger alerting. Rules live in the shared
// face-detection-rules store (the former Face Rules tab) and are stamped
// with camera_id/zone_id so they fire only inside this area. Changes save
// immediately -- they do NOT participate in the zone Save button, because
// they are stored separately from camera detection settings.

let faceRulesPayload = { rules: [] };
let enrolledPeople = [];
const PEOPLE_SAVE_DELAY_MS = 400;
let peopleSaveTimer = null;

function peopleRowKey(personId) {
  return personId ? String(personId) : '_unknown';
}

function findScopedPeopleRule(zone, personId) {
  const wanted = peopleRowKey(personId);
  return (faceRulesPayload.rules || []).find((rule) => {
    if (String(rule.camera_id || '') !== String(selectedCamera?.id || '')) return false;
    if (String(rule.zone_id || '') !== String(zone.id || '')) return false;
    return peopleRowKey(rule.person_id) === wanted;
  }) || null;
}

function ensureScopedPeopleRule(zone, personId, personName) {
  let rule = findScopedPeopleRule(zone, personId);
  if (rule) return rule;
  const isUnknown = !personId;
  rule = {
    id: isUnknown ? `_unknown:${zone.id}` : `zone:${zone.id}:person:${personId}`,
    person_id: isUnknown ? null : personId,
    name: personName || 'Unknown Person',
    enabled: true,
    email_enabled: false,
    push_enabled: false,
    email_recipients: '',
    cooldown_minutes: 5,
    min_confidence: null,
    camera_id: selectedCamera.id,
    zone_id: zone.id,
  };
  faceRulesPayload.rules = [...(faceRulesPayload.rules || []), rule];
  return rule;
}

function schedulePeopleSave() {
  clearTimeout(peopleSaveTimer);
  peopleSaveTimer = setTimeout(async () => {
    try {
      faceRulesPayload = await api('/api/settings/face-detection-rules', {
        method: 'PUT',
        body: JSON.stringify({ rules: faceRulesPayload.rules || [] }),
      });
      window.showToast?.('People rules saved.');
    } catch (error) {
      if (!window.daygleAuth?.redirecting) window.showToast?.(error.message || 'Failed to save people rules.', true);
    }
  }, PEOPLE_SAVE_DELAY_MS);
}

function renderPeopleCard(zone, zoneIndex) {
  if (!selectedCamera) return '';
  const zi = Number(zoneIndex);
  const zoneLabel = escapeHtml(zone.name || `Zone ${zi + 1}`);
  const rows = [{ key: '', name: 'Unknown Person', unknown: true }]
    .concat((enrolledPeople || []).map((person) => ({ key: String(person.id), name: person.name, unknown: false })))
    .map(({ key, name, unknown }) => {
      const rule = findScopedPeopleRule(zone, key);
      const expandKey = `people:${zi}:${key || '_unknown'}`;
      const expanded = expandedZoneRules.has(expandKey);
      // dk is a two-part composite (zone index + person row key) carried
      // through HTML data attributes. Percent-encode each part so neither can
      // inject the '|' or ':' delimiters, then decode on read -- no lossy
      // first-match replace (CodeQL: incomplete string escaping).
      const dk = `${encodeURIComponent(zi)}|${encodeURIComponent(key || '_unknown')}`;
      return `
      <div class="people-rule-row" style="display:flex;align-items:center;gap:10px;padding:8px 0;border-bottom:1px solid rgba(0,0,0,0.06)">
        <span style="flex:1;font-size:13px;font-weight:500">${escapeHtml(name)}${unknown ? ' <span class="muted" style="font-size:11px">(Stranger Alerts)</span>' : ''}</span>
        <label class="toggle-control" title="Alert when ${escapeHtml(name)} is detected in this area">
          <input type="checkbox" data-people-toggle="${dk}" ${rule && rule.enabled ? 'checked' : ''} aria-label="Toggle alerts for ${escapeHtml(name)} in ${zoneLabel}" />
          <span>${rule && rule.enabled ? 'On' : 'Off'}</span>
        </label>
        <label class="muted" style="font-size:13px;display:flex;gap:4px;align-items:center" title="Email when ${escapeHtml(name)} is detected here">
          <input type="checkbox" data-people-email="${dk}" ${rule?.email_enabled ? 'checked' : ''} />📧
        </label>
        <label class="muted" style="font-size:13px;display:flex;gap:4px;align-items:center" title="Push when ${escapeHtml(name)} is detected here">
          <input type="checkbox" data-people-push="${dk}" ${rule?.push_enabled ? 'checked' : ''} />🔔
        </label>
        <button class="secondary rule-expand-btn" type="button" data-expand-zone-people="${dk}" aria-expanded="${expanded}" title="Recipients, cooldown and confidence for ${escapeHtml(name)}">${expanded ? ICONS.chevronUp : ICONS.email}<span>${expanded ? 'Hide' : 'Advanced'}</span></button>
      </div>
      <div class="zone-motion-advanced-body" data-people-advanced="${dk}" ${expanded ? '' : 'hidden'} style="display:flex;flex-wrap:wrap;gap:12px;padding:0 0 10px">
        <label class="sound-rule-field" title="Comma-separated email recipients for ${escapeHtml(name)} alerts in this area.">
          <span>Recipients</span>
          <input type="text" data-people-recipients="${dk}" value="${escapeHtml(normalizeEmailList(rule?.email_recipients || '').join(', '))}" placeholder="a@example.com, b@example.com" style="min-width:220px" />
        </label>
        <label class="sound-rule-field" title="Minutes between repeat alerts for the same person in this area. Default 5.">
          <span>Cooldown (min)</span>
          <input type="number" data-people-cooldown="${dk}" value="${escapeHtml(String(rule ? (rule.cooldown_minutes ?? 5) : 5))}" min="0" max="1440" step="1" style="width:90px" />
        </label>
        <label class="sound-rule-field" title="Minimum recognition confidence (0-1) required. Leave blank for any.">
          <span>Min confidence</span>
          <input type="number" data-people-confidence="${dk}" value="${rule?.min_confidence != null ? escapeHtml(String(rule.min_confidence)) : ''}" min="0" max="1" step="0.01" placeholder="Any" style="width:90px" />
        </label>
      </div>`;
    }).join('');
  return `
    <div class="zone-motion-card" data-zone-people-for="${zi}">
      <div class="zone-motion-head">
        <div class="zone-motion-title">
          <span class="zone-motion-icon" aria-hidden="true">👥</span>
          <div>
            <strong>People Detection</strong>
            <span>Alert on recognised people inside this area</span>
          </div>
        </div>
      </div>
      <div class="zone-motion-body zone-people-body">${rows}</div>
    </div>`;
}

function bindPeopleControls() {
  const ruleFromDataset = (datasetValue) => {
    const sep = datasetValue.indexOf('|');
    const zone = cameraDetection().zones[Number(datasetValue.slice(0, sep))];
    if (!zone) return null;
    const rawKey = decodeURIComponent(datasetValue.slice(sep + 1));
    const personId = rawKey === '_unknown' ? '' : rawKey;
    let rule = findScopedPeopleRule(zone, personId);
    if (!rule) {
      const person = (enrolledPeople || []).find((candidate) => String(candidate.id) === String(personId));
      rule = ensureScopedPeopleRule(zone, personId, person?.name);
    }
    return rule;
  };
  [
    ['peopleToggle', (rule, checked) => { rule.enabled = checked; }],
    ['peopleEmail', (rule, checked) => { rule.email_enabled = checked; }],
    ['peoplePush', (rule, checked) => { rule.push_enabled = checked; }],
  ].forEach(([datasetKey, apply]) => {
    document.querySelectorAll(`[data-${datasetKey.replace(/[A-Z]/g, (m) => `-${m.toLowerCase()}`)}]`).forEach((cb) => {
      cb.addEventListener('change', () => {
        const rule = ruleFromDataset(cb.dataset[datasetKey]);
        if (!rule) return;
        apply(rule, cb.checked);
        schedulePeopleSave();
      });
    });
  });
  [['peopleRecipients', 'email_recipients'], ['peopleCooldown', 'cooldown_minutes'], ['peopleConfidence', 'min_confidence']]
    .forEach(([datasetKey, ruleKey]) => {
      document.querySelectorAll(`[data-${datasetKey.replace(/[A-Z]/g, (m) => `-${m.toLowerCase()}`)}]`).forEach((input) => {
        input.addEventListener('change', () => {
          const rule = ruleFromDataset(input.dataset[datasetKey]);
          if (!rule) return;
          if (ruleKey === 'email_recipients') {
            rule.email_recipients = normalizeEmailList(input.value);
          } else if (ruleKey === 'cooldown_minutes') {
            rule.cooldown_minutes = Math.max(0, Number.parseInt(input.value || '5', 10) || 0);
          } else {
            const raw = String(input.value || '').trim();
            const num = raw === '' ? null : clamp(Number(raw), 0, 1);
            rule.min_confidence = Number.isFinite(num) ? num : null;
          }
          schedulePeopleSave();
        });
      });
    });
  document.querySelectorAll('[data-expand-zone-people]').forEach((btn) => {
    btn.addEventListener('click', () => {
      const [rawZi, rawKey] = btn.dataset.expandZonePeople.split('|');
      const expandKey = `people:${decodeURIComponent(rawZi)}:${decodeURIComponent(rawKey)}`;
      if (expandedZoneRules.has(expandKey)) expandedZoneRules.delete(expandKey);
      else expandedZoneRules.add(expandKey);
      renderObjectDetectionRules();
    });
  });
}

async function loadEnrolledPeople() {
  try {
    const body = await api('/api/persons');
    enrolledPeople = body.persons || [];
  } catch {
    enrolledPeople = []; // non-fatal: the Unknown row still works
  }
}

(async function initPeopleCard() {
  try {
    const [rules] = await Promise.all([
      api('/api/settings/face-detection-rules'),
      loadEnrolledPeople(),
    ]);
    faceRulesPayload = rules;
  } catch {
    // Card still renders from empty defaults; saving recreates the store.
  }
  if (selectedCamera) renderObjectDetectionRules();
})();

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
          liveEls.status.textContent = 'Drag a corner dot or click a mid-edge \"+\" to reshape this zone.';
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
    ['zoneRuleEmail', 'email_enabled'],
    ['zoneRulePush', 'push_enabled'],
  ];
  checkboxBindings.forEach(([datasetKey, ruleKey]) => {
    document.querySelectorAll(`input[type="checkbox"][data-${datasetKey.replace(/[A-Z]/g, (m) => `-${m.toLowerCase()}`)}]`).forEach((cb) => {
      cb.addEventListener('change', () => {
        const { zoneIndex, rule } = parseZoneRuleKey(cb.dataset[datasetKey]);
        if (!rule) return;
        rule[ruleKey] = cb.checked;
        cameraDetection().zones[zoneIndex].object_labels = normalizeObjectRules(cameraDetection().zones[zoneIndex]).filter((item) => item.label !== 'motion').map((item) => item.label);
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
  const numberBindings = [
    ['zoneRuleCooldown', 'cooldown_seconds', (value) => Math.max(0, Number.parseInt(value || 0, 10) || 0)],
  ];
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
  document.querySelectorAll('input[data-zone-rule-email-recipients]').forEach((input) => {
    input.addEventListener('change', () => {
      const { zoneIndex, rule } = parseZoneRuleKey(input.dataset.zoneRuleEmailRecipients);
      if (!rule) return;
      rule.email_recipients = normalizeEmailList(input.value);
      cameraDetection().zones[zoneIndex].object_labels = normalizeObjectRules(cameraDetection().zones[zoneIndex]).filter((item) => item.label !== 'motion').map((item) => item.label);
      markZoneUnsaved();
    });
  });
  [
    ['zoneRuleActiveStart', 'active_start'],
    ['zoneRuleActiveEnd', 'active_end'],
    ['zoneRuleNotifyStart', 'notify_start'],
    ['zoneRuleNotifyEnd', 'notify_end'],
  ].forEach(([datasetKey, ruleKey]) => {
    const attr = `data-${datasetKey.replace(/[A-Z]/g, (m) => `-${m.toLowerCase()}`)}`;
    document.querySelectorAll(`[${attr}]`).forEach((wrap) => {
      wrap.querySelectorAll('select').forEach((sel) => {
        sel.addEventListener('change', () => {
          const { zoneIndex, rule } = parseZoneRuleKey(wrap.dataset[datasetKey]);
          if (!rule) return;
          rule[ruleKey] = timeSelectValue(wrap);
          cameraDetection().zones[zoneIndex].object_labels = normalizeObjectRules(cameraDetection().zones[zoneIndex]).filter((item) => item.label !== 'motion').map((item) => item.label);
          markZoneUnsaved();
        });
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
  setAddZoneLabel('Draw polygon');
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
  setAddZoneLabel('Draw polygon');
  normalizeZone(zones[selectedZoneIndex]);
  renderZones();
  refreshFrame();
  markZoneUnsaved();
}

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
      setAddZoneLabel(draftPolygon.points.length >= 3 ? 'Finish area' : 'Cancel drawing');
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
  setAddZoneLabel(drawingMode ? 'Cancel drawing' : 'Draw polygon');
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

async function saveZones() {
  try {
    liveEls.saveZonesBtn.disabled = true;
    const headerBtn = document.getElementById('saveZonesBtnHeader');
    if (headerBtn) headerBtn.disabled = true;
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
    liveEls.saveZonesBtn.disabled = false;
    const headerBtn = document.getElementById('saveZonesBtnHeader');
    if (headerBtn) headerBtn.disabled = false;
  }
}

liveEls.saveZonesBtn?.addEventListener('click', saveZones);
document.getElementById('saveZonesBtnHeader')?.addEventListener('click', saveZones);

window.addEventListener('resize', syncZoneOverlayToImage);

document.addEventListener('keydown', (event) => {
  if (event.key === 'Escape' && drawingMode) {
    drawingMode = false;
    draftPolygon = null;
    setAddZoneLabel('Draw polygon');
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
