const liveEls = {
  frame: document.getElementById('liveFrame'),
  frameWrap: document.getElementById('liveFrameWrap'),
  status: document.getElementById('liveStatus'),
  pulse: document.getElementById('livePulse'),
  frameTitle: document.getElementById('liveFrameTitle'),
  frameMeta: document.getElementById('liveFrameMeta'),
  cameraEmpty: document.getElementById('liveCameraEmpty'),
  detectionStatus: document.getElementById('liveDetectionStatus'),
  detectionChips: document.getElementById('liveDetectionChips'),
  detectionState: document.getElementById('liveDetectionState'),
  // Zones-page stats
  statZoneCount: document.getElementById('statZoneCount'),
  statRuleCount: document.getElementById('statRuleCount'),
  statRecording: document.getElementById('statRecording'),
  statCameraName: document.getElementById('statCameraName'),
  liveAiTrackToggle: document.getElementById('liveAiTrackToggle'),
  liveAiTrackGroup: document.getElementById('liveAiTrackGroup'),
  liveAiTrackCanvas: document.getElementById('liveAiTrackCanvas'),
  cameraSelect: document.getElementById('cameraSelect'),
  cameraControlGroup: document.getElementById('cameraControlGroup'),
  cameraGrid: document.getElementById('cameraGrid'),
  zoneOverlay: document.getElementById('zoneOverlay'),
  zoneList: document.getElementById('zoneList'),
  cameraMotionSettings: document.getElementById('cameraMotionSettings'),
  addZoneBtn: document.getElementById('addZoneBtn'),
  fullFrameZoneBtn: document.getElementById('fullFrameZoneBtn'),
  saveZonesBtn: document.getElementById('saveZonesBtn'),
};

// View mode: 'single' (one camera at a time) or 'all' (grid of every camera).
// Live-only; the zones page never enters 'all' mode.
let viewMode = 'single';

const pageMode = document.querySelector('[data-live-page]')?.dataset.livePage || 'live';
const isZonesPage = pageMode === 'zones';
const DEFAULT_SNAPSHOT_REFRESH_MS = 500;
const DEFAULT_DETECTION_STATUS_REFRESH_MS = 2000;
const CLOSE_DRAFT_DISTANCE_PX = 20;
let refreshTimer;
let detectionStatusTimer;
let snapshotRefreshMs = DEFAULT_SNAPSHOT_REFRESH_MS;
let detectionStatusRefreshMs = DEFAULT_DETECTION_STATUS_REFRESH_MS;
let csrfToken = null;
let cameras = [];
let availableLabels = [];
let selectedCamera = null;
let selectedZoneIndex = null;
let drawingMode = false;
let draftPolygon = null;
let zoneDrag = null;

let configuredLabels = null;

const LIVE_AI_TRACK_KEY = 'daygle.live.overlay.track.enabled';
// Off by default; users opt in per-browser via the toggle. The overlay only
// replays the background monitor's detections (already computed server-side
// for alerts/recording), so it never runs its own inference and adds no
// detector load — just the detection-status JSON poll and canvas drawing.
let liveAiTrackEnabled = false;
let liveAiTrackDetections = null;
let liveAiTrackPrevDetections = null;
// Wall-clock time (ms) at which each sample was received, so the overlay can
// be projected onto the frame currently on screen.
let liveAiTrackCaptureMs = 0;
let liveAiTrackPrevCaptureMs = 0;
// updated_at of the last ingested monitor sample, so polling faster than the
// monitor's detection interval does not re-ingest the same cycle (which would
// zero out the projection velocity).
let lastServerTrackUpdatedAt = null;
let liveRafId = null;
const OVERLAY_STATUS_REFRESH_MS = 600;
const LIVE_AI_TRACK_MAX_LEAD_MS = 1500;
// Stop drawing once the monitor stops reporting (camera backoff, detector
// stalled) so the last box does not linger after the object has left. The
// window is a few monitor cycles wide; an empty cycle clears boxes sooner.
const LIVE_AI_TRACK_STALE_MS = 3000;

async function api(path, options = {}) {
  const headers = { ...(options.headers || {}) };
  if (csrfToken && ['POST', 'PUT', 'PATCH', 'DELETE'].includes((options.method || 'GET').toUpperCase())) {
    headers['X-CSRF-Token'] = csrfToken;
  }
  if (options.body && !headers['Content-Type']) headers['Content-Type'] = 'application/json';
  const response = await fetch(path, { ...options, headers });
  if (response.status === 401) window.location.href = '/login';
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(payload.detail || `Request failed: ${response.status}`);
  return payload;
}

async function loadConfiguredLabels() {
  try {
    const settings = await api('/api/settings/system');
    const labels = new Map([['motion', 0.45]]);
    const setMin = (label, conf) => {
      if (!label) return;
      if (!labels.has(label) || conf < labels.get(label)) labels.set(label, conf);
    };
    for (const camera of (settings?.cameras || [])) {
      for (const zone of (camera?.detection?.zones || [])) {
        for (const rule of (zone?.object_rules || [])) {
          if (rule.enabled !== false && (rule.alert_on_detect !== false || rule.record_on_detect !== false)) {
            const label = String(rule.label || '').trim().toLowerCase();
            setMin(label, Number(rule.min_confidence ?? 0.5));
          }
        }
      }
    }
    configuredLabels = labels;
  } catch {
    // Show all labels if settings unavailable.
  }
}

function clearLiveOverlay() {
  if (!liveEls.liveAiTrackCanvas) return;
  const ctx = liveEls.liveAiTrackCanvas.getContext('2d');
  if (!ctx) return;
  ctx.setTransform(1, 0, 0, 1, 0, 0);
  ctx.clearRect(0, 0, liveEls.liveAiTrackCanvas.width, liveEls.liveAiTrackCanvas.height);
}

function drawLiveOverlay() {
  if (!liveEls.liveAiTrackCanvas || !liveEls.frame) return;
  resizeOverlayCanvas(liveEls.liveAiTrackCanvas, liveEls.frame);
  const ctx = liveEls.liveAiTrackCanvas.getContext('2d');
  if (!ctx) return;
  ctx.setTransform(1, 0, 0, 1, 0, 0);
  ctx.clearRect(0, 0, liveEls.liveAiTrackCanvas.width, liveEls.liveAiTrackCanvas.height);
  if (!liveAiTrackEnabled || !liveAiTrackDetections?.length) return;

  // Drop boxes whose source sample has gone stale (slow/stalled inference) so the
  // overlay clears instead of trailing the object after it has left the frame.
  if (liveAiTrackCaptureMs > 0 && performance.now() - liveAiTrackCaptureMs > LIVE_AI_TRACK_STALE_MS) {
    liveAiTrackDetections = null;
    liveAiTrackPrevDetections = null;
    return;
  }

  let detections = liveAiTrackDetections;
  if (liveAiTrackPrevDetections && liveAiTrackPrevCaptureMs > 0) {
    detections = projectDetections(
      liveAiTrackPrevDetections,
      liveAiTrackDetections,
      liveAiTrackPrevCaptureMs,
      liveAiTrackCaptureMs,
      performance.now(),
      LIVE_AI_TRACK_MAX_LEAD_MS,
    );
  }

  if (configuredLabels) {
    detections = detections.filter((d) => configuredLabels.has(String(d.label || '').trim().toLowerCase()));
  }
  drawDetectionBoxesOnCanvas(liveEls.liveAiTrackCanvas, detections, liveEls.frame);
}

function startLiveRaf() {
  if (liveRafId !== null) return;
  function loop() {
    if (!liveAiTrackEnabled || isAllCameraMode()) {
      liveRafId = null;
      return;
    }
    drawLiveOverlay();
    liveRafId = requestAnimationFrame(loop);
  }
  liveRafId = requestAnimationFrame(loop);
}

function stopLiveRaf() {
  if (liveRafId !== null) {
    cancelAnimationFrame(liveRafId);
    liveRafId = null;
  }
}

function cameraDetection() {
  selectedCamera.detection ||= { zones: [] };
  selectedCamera.detection.zones ||= [];
  selectedCamera.detection.motion ||= { enabled: true, record_on_detect: true, email_enabled: true, push_enabled: false };
  return selectedCamera.detection;
}

function cameraMotion() {
  const det = cameraDetection();
  det.motion ||= { enabled: true, record_on_detect: true, email_enabled: true, push_enabled: false };
  return det.motion;
}

function cameraRecording() {
  selectedCamera.recording ||= { continuous: false };
  selectedCamera.recording.continuous ??= false;
  return selectedCamera.recording;
}

function clamp(value, min = 0, max = 1) {
  return Math.max(min, Math.min(max, value));
}

function normalizePoint(point) {
  return { x: clamp(Number(point?.x) || 0), y: clamp(Number(point?.y) || 0) };
}

function roundCoord(value) {
  return Math.round(clamp(value) * 10000) / 10000;
}

function normalizeLabelList(value) {
  const source = Array.isArray(value) ? value : String(value || '').split(',');
  const seen = new Set();
  return source.map((label) => String(label).trim().toLowerCase()).filter((label) => {
    if (!label || seen.has(label)) return false;
    seen.add(label);
    return true;
  });
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
  const isMotion = String(label || '').trim().toLowerCase() === 'motion';
  return {
    label: String(label || '').trim().toLowerCase(),
    enabled: true,
    record_on_detect: true,
    alert_on_detect: true,
    min_confidence: isMotion ? 0.45 : 0.5,
    cooldown_seconds: 60,
    email_enabled: false,
    email_recipients: [],
    push_enabled: false,
    active_start: null,
    active_end: null,
  };
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
        alert_on_detect: rule.alert_on_detect !== false,
        min_confidence: clamp(Number(rule.min_confidence ?? 0.5), 0, 1),
        cooldown_seconds: Math.max(0, Number.parseInt(rule.cooldown_seconds ?? 60, 10) || 0),
        email_enabled: rule.email_enabled === true,
        email_recipients: normalizeEmailList(rule.email_recipients),
        push_enabled: rule.push_enabled === true,
        active_start: rule.active_start || null,
        active_end: rule.active_end || null,
      }))
      .filter((rule) => {
        if (!rule.label || seen.has(rule.label)) return false;
        seen.add(rule.label);
        return true;
      });
  }
  return normalizeLabelList(zone.object_labels).map(defaultObjectRule);
}

function normalizeEmailList(value) {
  const source = Array.isArray(value) ? value : String(value || '').split(',');
  return source.map((recipient) => String(recipient).trim()).filter(Boolean);
}

function normalizeZone(zone) {
  const sourcePoints = Array.isArray(zone.points) && zone.points.length >= 3 ? zone.points : rectanglePoints(zone);
  zone.points = sourcePoints.map(normalizePoint);
  zone.object_rules = normalizeObjectRules(zone);
  if (zone.monitor_motion !== false && !zone.object_rules.some((r) => r.label === 'motion')) {
    zone.object_rules.unshift(defaultObjectRule('motion'));
  }
  zone.object_labels = zone.object_rules.filter((r) => r.label !== 'motion').map((rule) => rule.label);
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
  if (!isZonesPage || !liveEls.zoneOverlay || !liveEls.frameWrap || !liveEls.frame) return;
  const wrapRect = liveEls.frameWrap.getBoundingClientRect();
  const imageRect = visibleImageRect();
  liveEls.zoneOverlay.style.left = `${imageRect.left - wrapRect.left}px`;
  liveEls.zoneOverlay.style.top = `${imageRect.top - wrapRect.top}px`;
  liveEls.zoneOverlay.style.width = `${imageRect.width}px`;
  liveEls.zoneOverlay.style.height = `${imageRect.height}px`;
}

function snapshotUrl(camera = selectedCamera) {
  const cameraId = encodeURIComponent(camera?.id || '');
  return `/api/live/snapshot?camera_id=${cameraId}&t=${Date.now()}`;
}

function isAllCameraMode() {
  return pageMode === 'live' && viewMode === 'all';
}

function refreshFrame() {
  if (!selectedCamera || document.hidden) return;
  if (isAllCameraMode()) {
    renderCameraGridFrames();
    return;
  }
  if (liveEls.frame.dataset.loading === 'true') return;
  liveEls.frame.dataset.loading = 'true';
  liveEls.frame.src = snapshotUrl();
}

function renderCameraGridFrames() {
  if (!liveEls.cameraGrid) return;
  liveEls.cameraGrid.querySelectorAll('img[data-camera-id]').forEach((image) => {
    image.src = snapshotUrl(cameras.find((camera) => camera.id === image.dataset.cameraId));
  });
}

function renderCameraGrid() {
  if (!liveEls.cameraGrid) return;
  liveEls.cameraGrid.innerHTML = cameras.map((camera) => {
    const res = `${camera.width || 1280}×${camera.height || 720}`;
    const fps = camera.fps || 15;
    return `
      <article class="live-camera-tile">
        <div class="live-camera-tile-image">
          <img data-camera-id="${escapeHtml(camera.id)}" alt="${escapeHtml(camera.name || camera.id)} live footage" />
          <div class="live-status live-status-online">LIVE</div>
        </div>
        <div class="live-camera-tile-info">
          <div class="live-camera-tile-name">${escapeHtml(camera.name || camera.id)}</div>
          <div class="live-camera-tile-meta">${escapeHtml(res)} · ${escapeHtml(fps)} fps</div>
        </div>
      </article>
    `;
  }).join('');
  renderCameraGridFrames();
}

function syncViewMode() {
  const allMode = isAllCameraMode();
  if (liveEls.frameWrap) liveEls.frameWrap.hidden = allMode;
  if (liveEls.cameraGrid) liveEls.cameraGrid.hidden = !allMode;
  if (liveEls.cameraControlGroup) liveEls.cameraControlGroup.hidden = allMode;
  if (liveEls.liveAiTrackGroup) liveEls.liveAiTrackGroup.hidden = allMode;
  if (allMode) {
    clearLiveOverlay();
    renderCameraGrid();
  }
  restartDetectionStatusTimer();
  refreshDetectionStatus();
}

function updateFrameHeader(camera) {
  if (!camera) return;
  if (liveEls.frameTitle) {
    liveEls.frameTitle.textContent = camera.name || camera.id || 'Camera';
  }
  if (liveEls.frameMeta) {
    const res = `${camera.width || 1280}×${camera.height || 720}`;
    const fps = camera.fps || 15;
    const backend = camera.backend === 'rtsp' ? 'RTSP' : 'ONVIF';
    liveEls.frameMeta.textContent = `${backend} · ${res} · ${fps} fps`;
  }
}

function updateEmptyState() {
  if (!liveEls.cameraEmpty) return;
  if (cameras.length === 0) {
    liveEls.cameraEmpty.hidden = false;
    if (liveEls.frameWrap) liveEls.frameWrap.hidden = true;
    if (liveEls.cameraGrid) liveEls.cameraGrid.hidden = true;
    if (liveEls.cameraControlGroup) liveEls.cameraControlGroup.hidden = true;
    if (liveEls.liveAiTrackGroup) liveEls.liveAiTrackGroup.hidden = true;
  } else {
    liveEls.cameraEmpty.hidden = true;
  }
}

// Recompute the zones-page stats card values from the currently selected
// camera. Safe to call on the live page (no-op when the stat elements are
// absent).
function updateZonesStats() {
  if (!isZonesPage) return;
  if (!selectedCamera) return;
  const detection = cameraDetection();
  const recording = cameraRecording();
  const zones = detection.zones || [];
  const ruleCount = zones.reduce((sum, zone) => sum + (zone.object_rules?.length || 0), 0);
  if (liveEls.statZoneCount) liveEls.statZoneCount.textContent = String(zones.length);
  if (liveEls.statRuleCount) liveEls.statRuleCount.textContent = String(ruleCount);
  if (liveEls.statRecording) {
    const isContinuous = recording.continuous === true;
    liveEls.statRecording.textContent = isContinuous ? 'Continuous' : 'On Alert';
  }
  if (liveEls.statCameraName) {
    liveEls.statCameraName.textContent = selectedCamera.name || selectedCamera.id || '—';
  }
}

// Build a structured summary of the monitor's latest cycle so the renderer
// can split the visual into a state chip, per-label chips, and a status line.
function summarizeDetectionStatus(payload, soundStatus = null, soundEnabled = false) {
  if (!payload) {
    return { state: 'idle', stateLabel: 'Idle', chips: [], message: 'Live AI status unavailable.' };
  }

  // Build a highest-confidence map of detected labels (filtered to active rules).
  const confMap = new Map();
  for (const d of (payload.detections || [])) {
    const label = String(d.label || '').trim().toLowerCase();
    const conf = Number(d.confidence || 0);
    if (!label) continue;
    if (configuredLabels && !configuredLabels.has(label)) continue;
    if (!confMap.has(label) || conf > confMap.get(label)) confMap.set(label, conf);
  }
  if (confMap.size === 0) {
    for (const label of (payload.detected_labels || [])) {
      const l = String(label || '').trim().toLowerCase();
      if (l && (!configuredLabels || configuredLabels.has(l))) confMap.set(l, 0);
    }
  }
  const chips = Array.from(confMap.entries())
    .sort((a, b) => b[1] - a[1])
    .map(([label, confidence]) => ({ label, confidence, isSound: false }));
  const labelStr = chips.length
    ? chips.map((c) => c.confidence > 0 ? `${titleCase(c.label)} (${Math.round(c.confidence * 100)}%)` : titleCase(c.label)).join(', ')
    : null;

  // Persistent sound status chip — always shown to indicate whether sound
  // detection is enabled, idle, or recently fired.
  if (soundEnabled) {
    if (soundStatus && soundStatus.last_detected_at) {
      const ageMs = Date.now() - Date.parse(soundStatus.last_detected_at);
      if (ageMs < 60000) {
        const soundLabel = soundStatus.last_class_label || soundStatus.last_class || 'sound';
        const soundConf = Number(soundStatus.last_confidence || 0);
        chips.push({ label: `🔊 ${soundLabel}`, confidence: soundConf, isSound: true });
      } else {
        chips.push({ label: '🔊 Listening', confidence: 0, isSound: true, isIdle: true });
      }
    } else {
      chips.push({ label: '🔊 Listening', confidence: 0, isSound: true, isIdle: true });
    }
  } else {
    chips.push({ label: '🔊 Sound Off', confidence: 0, isSound: true, isDisabled: true });
  }

  if (payload.state === 'alerted') {
    const alerts = (payload.triggered_alerts || []).map((a) => a.rule_name).join(', ') || 'unknown rule';
    const parts = [`Alert triggered — ${alerts}`];
    if (labelStr) parts.push(`detected ${labelStr}`);
    if (payload.recording_state) parts.push(`recording ${payload.recording_state}${payload.recording_id ? ` #${payload.recording_id}` : ''}`);
    return { state: 'alerted', stateLabel: 'Alerted', chips, message: parts.join('; ') + '.' };
  }

  if (payload.state === 'checked') {
    if (!labelStr) {
      return { state: 'idle', stateLabel: 'Monitoring', chips, message: 'Matched Objects: No detections found' };
    }
    const reason = String(payload.reason || '');
    let suffix;
    if (/debounce|suppressed/i.test(reason)) suffix = 'event suppressed (debounce active)';
    else if (/cooldown/i.test(reason)) suffix = 'alert rule in cooldown';
    else if (/no alert rule|no matching|no new alert/i.test(reason)) suffix = 'no matching alert rule';
    else if (/no detections matched/i.test(reason)) suffix = 'outside monitored zones';
    else suffix = reason || 'no alert triggered';
    return { state: 'detected', stateLabel: 'Detected', chips, message: `Detected ${labelStr} — ${suffix}.` };
  }

  const fallback = String(payload.reason || payload.ai_error || 'waiting for frames');
  return {
    state: payload.state || 'idle',
    stateLabel: payload.state ? payload.state[0].toUpperCase() + payload.state.slice(1) : 'Idle',
    chips,
    message: `Live AI: ${payload.state || 'waiting'} — ${fallback}`,
  };
}

function renderDetectionStatus(summary) {
  if (liveEls.detectionState) {
    liveEls.detectionState.textContent = summary.stateLabel;
    liveEls.detectionState.className = 'chip ' + (
      summary.state === 'alerted' ? 'chip-warn' :
      summary.state === 'detected' ? 'chip-info' :
      summary.state === 'error' ? 'chip-warn' :
      'chip-dim'
    );
  }
  if (liveEls.detectionChips) {
    const visualChips = summary.chips.filter((c) => !c.isSound);
    const soundChips = summary.chips.filter((c) => c.isSound);
    if (!visualChips.length && !soundChips.length) {
      liveEls.detectionChips.innerHTML = '<span class="detection-chip detection-chip-empty">No active detections</span>';
    } else {
      const visualHtml = visualChips.map((c) => {
        const variant = summary.state === 'alerted' ? 'detection-chip-alert' : '';
        const text = c.confidence > 0 ? `${titleCase(c.label)} · ${Math.round(c.confidence * 100)}%` : titleCase(c.label);
        return `<span class="detection-chip ${variant}">${escapeHtml(text)}</span>`;
      }).join('');
      const soundHtml = soundChips.map((c) => {
        let cls = 'detection-chip detection-chip-sound';
        if (c.isDisabled) cls = 'detection-chip detection-chip-empty';
        else if (c.isIdle) cls = 'detection-chip detection-chip-sound';
        const text = c.confidence > 0 ? `${c.label} · ${Math.round(c.confidence * 100)}%` : c.label;
        return `<span class="${cls}">${escapeHtml(text)}</span>`;
      }).join('');
      liveEls.detectionChips.innerHTML = visualHtml + soundHtml;
    }
  }
  if (liveEls.detectionStatus) {
    liveEls.detectionStatus.textContent = summary.message;
  }
}

// Feed the background monitor's object detections from a status payload into
// the overlay. Only cycles that actually ran inference ('checked'/'alerted')
// are trusted; 'error'/'skipped'/'waiting' leave the current boxes in place
// until the stale guard clears them.
function ingestServerTrackDetections(payload) {
  if (!liveAiTrackEnabled || !payload || !['checked', 'alerted'].includes(payload.state)) return;
  if (payload.updated_at && payload.updated_at === lastServerTrackUpdatedAt) return;
  lastServerTrackUpdatedAt = payload.updated_at || null;
  liveAiTrackPrevDetections = liveAiTrackDetections;
  liveAiTrackPrevCaptureMs = liveAiTrackCaptureMs;
  liveAiTrackDetections = (payload.detections || [])
    .filter((d) => d && d.box && !d.motion_event && String(d.label || '').trim().toLowerCase() !== 'motion')
    .map((d) => ({ label: d.label, confidence: d.confidence, box: d.box }));
  liveAiTrackCaptureMs = performance.now();
  drawLiveOverlay();
}

function detectionStatusInterval() {
  return liveAiTrackEnabled && !isAllCameraMode()
    ? Math.min(detectionStatusRefreshMs, OVERLAY_STATUS_REFRESH_MS)
    : detectionStatusRefreshMs;
}

function restartDetectionStatusTimer() {
  if (detectionStatusTimer) clearInterval(detectionStatusTimer);
  detectionStatusTimer = setInterval(refreshDetectionStatus, detectionStatusInterval());
}

async function refreshDetectionStatus() {
  if (!liveEls.detectionStatus) return;
  if (isAllCameraMode()) {
    renderDetectionStatus({
      state: 'idle',
      stateLabel: 'All Cameras',
      chips: [],
      message: 'Live AI: showing all cameras. Select one camera for detailed status.',
    });
    return;
  }
  if (!selectedCamera) return;
  try {
    const cameraId = encodeURIComponent(selectedCamera.id);
    // Check camera-level sound detection enabled state
    const soundEnabled = selectedCamera.detection?.sound?.enabled === true;
    const [payload, soundStatus] = await Promise.all([
      api(`/api/live/detection-status?camera_id=${cameraId}`),
      api(`/api/sound/status?camera_id=${cameraId}`).catch(() => null),
    ]);
    ingestServerTrackDetections(payload);
    renderDetectionStatus(summarizeDetectionStatus(payload, soundStatus, soundEnabled));
  } catch (error) {
    renderDetectionStatus({
      state: 'error',
      stateLabel: 'Error',
      chips: [],
      message: `Live AI status unavailable: ${error.message}`,
    });
  }
}

function setSelectedCamera(cameraId) {
  selectedCamera = cameras.find((camera) => camera.id === cameraId) || cameras[0];
  if (!selectedCamera) return;
  selectedZoneIndex = null;
  liveAiTrackDetections = null;
  liveAiTrackPrevDetections = null;
  liveAiTrackCaptureMs = 0;
  liveAiTrackPrevCaptureMs = 0;
  lastServerTrackUpdatedAt = null;
  clearLiveOverlay();
  if (liveEls.cameraSelect) liveEls.cameraSelect.value = selectedCamera.id;
  updateFrameHeader(selectedCamera);
  updateZonesStats();
  if (isZonesPage) {
    renderZones();
    renderMotionDetectionSettings();
  }
  refreshFrame();
  refreshDetectionStatus();
}

function renderCameraOptions() {
  liveEls.cameraSelect.innerHTML = cameras.map((camera) => `<option value="${escapeHtml(camera.id)}">${escapeHtml(camera.name || camera.id)}</option>`).join('');
  setSelectedCamera(liveEls.cameraSelect.value || cameras[0]?.id);
}

function renderZoneBox(zone, index) {
  const selected = index === selectedZoneIndex ? ' selected' : '';
  const points = zone.points.map((point) => `${point.x * 100},${point.y * 100}`).join(' ');
  const labelPoint = { x: zone.x, y: zone.y };
  const handles = zone.points.map((point, pointIndex) => (
    `<i class="zone-handle zone-point-handle" data-zone-index="${index}" data-point-index="${pointIndex}" style="left:${point.x * 100}%;top:${point.y * 100}%"></i>`
  )).join('');
  return `
    <svg class="monitor-zone-polygon${selected}" data-zone-index="${index}" viewBox="0 0 100 100" preserveAspectRatio="none">
      <polygon data-zone-index="${index}" points="${points}"></polygon>
    </svg>
    <span class="zone-label${selected}" data-zone-index="${index}" style="left:${labelPoint.x * 100}%;top:${labelPoint.y * 100}%">${escapeHtml(zone.name || `Zone ${index + 1}`)}</span>
    ${handles}
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

function objectRuleOptions(selectedLabel) {
  const labels = [...new Set([...availableLabels, selectedLabel].filter((l) => Boolean(l) && l !== 'motion'))];
  const coco = labels.map((label) => `<option value="${escapeHtml(label)}" ${label === selectedLabel ? 'selected' : ''}>${escapeHtml(label)}</option>`).join('');
  const motionSelected = selectedLabel === 'motion';
  return `<option value="">Add Object...</option><option value="motion" ${motionSelected ? 'selected' : ''}>motion</option>${coco}`;
}

function renderObjectRules(zone, zoneIndex) {
  zone.object_rules = normalizeObjectRules(zone);
  if (!zone.object_rules.length) {
    return '<div class="empty compact-empty">No object rules yet. Choose an object above to add detection settings for this zone.</div>';
  }
  const rows = zone.object_rules.map((rule, ruleIndex) => {
    const key = `${zoneIndex}:${ruleIndex}`;
    return `
      <tr>
        <td class="cell-label">${escapeHtml(titleCase(rule.label))}</td>
        <td class="cell-center"><input type="checkbox" data-zone-rule-enabled="${key}" ${rule.enabled !== false ? 'checked' : ''} /></td>
        <td class="cell-center"><input type="checkbox" data-zone-rule-record="${key}" ${rule.record_on_detect !== false ? 'checked' : ''} /></td>
        <td class="cell-center"><input type="checkbox" data-zone-rule-alert="${key}" ${rule.alert_on_detect !== false ? 'checked' : ''} /></td>
        <td><input type="number" data-zone-rule-confidence="${key}" value="${rule.min_confidence}" min="0" max="1" step="0.05" /></td>
        <td><input type="number" data-zone-rule-cooldown="${key}" value="${rule.cooldown_seconds}" min="0" max="3600" step="5" /></td>
        <td class="cell-center"><input type="checkbox" data-zone-rule-email="${key}" ${rule.email_enabled === true ? 'checked' : ''} /></td>
        <td class="cell-center"><input type="checkbox" data-zone-rule-push="${key}" ${rule.push_enabled === true ? 'checked' : ''} /></td>
        <td class="cell-center"><button class="secondary delete-btn" type="button" data-delete-zone-rule="${key}">✕</button></td>
      </tr>`;
  }).join('');
  return `
    <div style="overflow-x:auto">
      <table class="rule-table">
        <thead>
          <tr>
            <th>Object</th>
            <th class="cell-center">Enabled</th>
            <th class="cell-center">Record</th>
            <th class="cell-center">Alert</th>
            <th>Confidence</th>
            <th>Cooldown (s)</th>
            <th class="cell-center">Email</th>
            <th class="cell-center">Push</th>
            <th></th>
          </tr>
        </thead>
        <tbody>${rows}</tbody>
      </table>
    </div>`;
}

function renderZones() {
  if (!isZonesPage || !selectedCamera) return;
  syncZoneOverlayToImage();
  const zones = cameraDetection().zones;
  zones.forEach(normalizeZone);
  liveEls.zoneOverlay.innerHTML = zones.map((zone, index) => (zone.enabled === false ? '' : renderZoneBox(zone, index))).join('');
  updateZonesStats();
  if (!zones.length) {
    liveEls.zoneList.innerHTML = '<div class="empty">No monitoring areas yet. Click "Draw area", place corner dots on the footage, then click the first dot to close the area.</div>';
    renderObjectDetectionRules();
    return;
  }
  liveEls.zoneList.innerHTML = zones.map((zone, index) => `
    <div class="item zone-row ${index === selectedZoneIndex ? 'selected' : ''}${zone.enabled === false ? ' disabled' : ''}" data-select-zone="${index}">
      <div class="zone-row-main">
        <input data-zone-name="${index}" value="${escapeHtml(zone.name || `Zone ${index + 1}`)}" />
        <label><span>Zone</span><select data-zone-enabled="${index}"><option value="true" ${zone.enabled !== false ? 'selected' : ''}>Shown</option><option value="false" ${zone.enabled === false ? 'selected' : ''}>Hidden</option></select></label>
        <button class="secondary" type="button" data-delete-zone="${index}">Remove</button>
      </div>
    </div>
  `).join('');
  bindZoneControls(zones);
  renderObjectDetectionRules();
}

function renderObjectDetectionRules() {
  const container = document.getElementById('objectDetectionRules');
  if (!container) return;
  if (!selectedCamera) { container.innerHTML = ''; return; }
  const zones = cameraDetection().zones;
  if (!zones.length) {
    container.innerHTML = '<p class="muted empty-message">No monitoring areas configured. Draw an area above first.</p>';
    return;
  }
  container.innerHTML = zones.map((zone, zoneIndex) => {
    zone.object_rules = normalizeObjectRules(zone);
    const zoneName = escapeHtml(zone.name || `Zone ${zoneIndex + 1}`);
    const addOptions = objectRuleOptions('');
    const rulesHtml = zone.object_rules.length
      ? renderObjectRules(zone, zoneIndex)
      : '<p class="muted empty-message">No rules yet. Add an object below.</p>';
    return `
      <div class="zone-object-rules" data-zone-rules-for="${zoneIndex}">
        <div class="zone-name-card">${zoneName}</div>
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
      zone.object_labels = zone.object_rules.filter((r) => r.label !== 'motion').map((rule) => rule.label);
      renderZones();
    });
  });
  document.querySelectorAll('[data-delete-zone-rule]').forEach((button) => {
    button.addEventListener('click', () => {
      const zones = cameraDetection().zones;
      const { zoneIndex, ruleIndex, rule } = parseZoneRuleKey(button.dataset.deleteZoneRule);
      if (rule?.label === 'motion') zones[zoneIndex].monitor_motion = false;
      zones[zoneIndex].object_rules.splice(ruleIndex, 1);
      zones[zoneIndex].object_labels = zones[zoneIndex].object_rules.filter((r) => r.label !== 'motion').map((r) => r.label);
      renderZones();
    });
  });
  bindRuleFields();
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
      if (label) label.textContent = input.value || `Zone ${index + 1}`;
    });
  });
  document.querySelectorAll('[data-zone-enabled]').forEach((select) => {
    select.addEventListener('change', () => {
      selectedZoneIndex = Number(select.dataset.zoneEnabled);
      zones[selectedZoneIndex].enabled = select.value === 'true';
      renderZones();
      refreshFrame();
    });
  });
  document.querySelectorAll('[data-delete-zone]').forEach((button) => {
    button.addEventListener('click', () => {
      zones.splice(Number(button.dataset.deleteZone), 1);
      selectedZoneIndex = null;
      renderZones();
      refreshFrame();
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
  // Select-based fields (old layout compatibility)
  const selectBindings = [
    ['zoneRuleLabel', 'label', (value) => value],
    ['zoneRuleEnabled', 'enabled', (value) => value === 'true'],
    ['zoneRuleRecord', 'record_on_detect', (value) => value === 'true'],
    ['zoneRuleAlert', 'alert_on_detect', (value) => value === 'true'],
    ['zoneRuleEmail', 'email_enabled', (value) => value === 'true'],
    ['zoneRulePush', 'push_enabled', (value) => value === 'true'],
  ];
  selectBindings.forEach(([datasetKey, ruleKey, transform]) => {
    document.querySelectorAll(`[data-${datasetKey.replace(/[A-Z]/g, (m) => `-${m.toLowerCase()}`)}]`).forEach((field) => {
      field.addEventListener('change', () => {
        const { zoneIndex, rule } = parseZoneRuleKey(field.dataset[datasetKey]);
        if (!rule) return;
        rule[ruleKey] = transform(field.value);
        cameraDetection().zones[zoneIndex].object_labels = normalizeObjectRules(cameraDetection().zones[zoneIndex]).filter((item) => item.label !== 'motion').map((item) => item.label);
        if (ruleKey === 'label') renderZones();
      });
    });
  });
  // Checkbox-based fields (table layout)
  const checkboxBindings = [
    ['zoneRuleEnabled', 'enabled'],
    ['zoneRuleRecord', 'record_on_detect'],
    ['zoneRuleAlert', 'alert_on_detect'],
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
      });
    });
  });
  // Number input fields (table layout)
  const numberBindings = [
    ['zoneRuleConfidence', 'min_confidence', (value) => clamp(Number(value || 0), 0, 1)],
    ['zoneRuleCooldown', 'cooldown_seconds', (value) => Math.max(0, Number.parseInt(value || 0, 10) || 0)],
  ];
  numberBindings.forEach(([datasetKey, ruleKey, transform]) => {
    document.querySelectorAll(`input[type="number"][data-${datasetKey.replace(/[A-Z]/g, (m) => `-${m.toLowerCase()}`)}]`).forEach((inp) => {
      inp.addEventListener('change', () => {
        const { zoneIndex, rule } = parseZoneRuleKey(inp.dataset[datasetKey]);
        if (!rule) return;
        rule[ruleKey] = transform(inp.value);
        cameraDetection().zones[zoneIndex].object_labels = normalizeObjectRules(cameraDetection().zones[zoneIndex]).filter((item) => item.label !== 'motion').map((item) => item.label);
      });
    });
  });
  // Text input fields (email recipients)
  document.querySelectorAll('[data-zone-rule-recipients]').forEach((inp) => {
    inp.addEventListener('change', () => {
      const { zoneIndex, rule } = parseZoneRuleKey(inp.dataset.zoneRuleRecipients);
      if (!rule) return;
      rule.email_recipients = normalizeEmailList(inp.value);
      cameraDetection().zones[zoneIndex].object_labels = normalizeObjectRules(cameraDetection().zones[zoneIndex]).filter((item) => item.label !== 'motion').map((item) => item.label);
    });
  });
  // Time input fields (active start/end)
  ['activeStart', 'activeEnd'].forEach((key) => {
    const datasetKey = `zoneRule${key.charAt(0).toUpperCase() + key.slice(1)}`;
    const dataAttr = `zone-rule-${key.replace(/[A-Z]/g, (m) => `-${m.toLowerCase()}`)}`;
    document.querySelectorAll(`[data-${dataAttr}]`).forEach((inp) => {
      inp.addEventListener('change', () => {
        const { rule } = parseZoneRuleKey(inp.dataset[datasetKey]);
        if (!rule) return;
        rule[key === 'activeStart' ? 'active_start' : 'active_end'] = inp.value || null;
      });
    });
  });
}

function renderMotionDetectionSettings() {
  if (!selectedCamera || !liveEls.cameraMotionSettings) return;
  const motion = cameraMotion();
  liveEls.cameraMotionSettings.innerHTML = `
    <div style="overflow-x:auto">
      <table class="rule-table">
        <thead>
          <tr>
            <th>Type</th>
            <th class="cell-center">Enabled</th>
            <th class="cell-center">Record</th>
            <th class="cell-center">Email</th>
            <th class="cell-center">Push</th>
          </tr>
        </thead>
        <tbody>
          <tr>
            <td class="cell-label">Motion</td>
            <td class="cell-center"><input type="checkbox" data-motion-enabled="true" ${motion.enabled !== false ? 'checked' : ''} /></td>
            <td class="cell-center"><input type="checkbox" data-motion-record="true" ${motion.record_on_detect !== false ? 'checked' : ''} /></td>
            <td class="cell-center"><input type="checkbox" data-motion-email="true" ${motion.email_enabled ? 'checked' : ''} /></td>
            <td class="cell-center"><input type="checkbox" data-motion-push="true" ${motion.push_enabled ? 'checked' : ''} /></td>
          </tr>
        </tbody>
      </table>
    </div>
  `;
  updateZonesStats();
  liveEls.cameraMotionSettings.querySelectorAll('[data-motion-enabled]').forEach((cb) => {
    cb.addEventListener('change', () => { cameraMotion().enabled = cb.checked; });
  });
  liveEls.cameraMotionSettings.querySelectorAll('[data-motion-record]').forEach((cb) => {
    cb.addEventListener('change', () => { cameraMotion().record_on_detect = cb.checked; });
  });
  liveEls.cameraMotionSettings.querySelectorAll('[data-motion-email]').forEach((cb) => {
    cb.addEventListener('change', () => { cameraMotion().email_enabled = cb.checked; });
  });
  liveEls.cameraMotionSettings.querySelectorAll('[data-motion-push]').forEach((cb) => {
    cb.addEventListener('change', () => { cameraMotion().push_enabled = cb.checked; });
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
    object_rules: [defaultObjectRule('motion')],
  });
  selectedZoneIndex = zones.length - 1;
  normalizeZone(zones[selectedZoneIndex]);
  draftPolygon = null;
  drawingMode = false;
  liveEls.addZoneBtn.textContent = 'Draw area';
  renderZones();
  refreshFrame();
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
    object_rules: [defaultObjectRule('motion')],
  });
  selectedZoneIndex = zones.length - 1;
  draftPolygon = null;
  drawingMode = false;
  zoneDrag = null;
  if (liveEls.addZoneBtn) liveEls.addZoneBtn.textContent = 'Draw area';
  normalizeZone(zones[selectedZoneIndex]);
  renderZones();
  refreshFrame();
}

function bindZoneDrawing() {
  if (!isZonesPage || !liveEls.zoneOverlay) return;
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
      liveEls.addZoneBtn.textContent = draftPolygon.points.length >= 3 ? 'Finish area' : 'Cancel drawing';
      renderDraftPolygon();
      liveEls.zoneOverlay.setPointerCapture(event.pointerId);
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
}

liveEls.frame.addEventListener('load', () => {
  liveEls.frame.dataset.loading = 'false';
  syncZoneOverlayToImage();
  liveEls.status.textContent = selectedCamera?.name || 'Camera';
  liveEls.status.classList.add('live-status-online');
  liveEls.status.classList.remove('live-status-offline');
  if (liveEls.pulse) {
    liveEls.pulse.classList.add('online');
    liveEls.pulse.classList.remove('offline');
  }
  if (liveAiTrackEnabled && !isAllCameraMode()) {
    startLiveRaf();
  } else {
    stopLiveRaf();
  }
});

liveEls.frame.addEventListener('error', () => {
  liveEls.frame.dataset.loading = 'false';
  clearLiveOverlay();
  liveEls.status.textContent = selectedCamera?.name
    ? `${selectedCamera.name} - Unable to load live footage. Retrying...`
    : 'Unable to load live footage. Retrying...';
  liveEls.status.classList.add('live-status-offline');
  liveEls.status.classList.remove('live-status-online');
  if (liveEls.pulse) {
    liveEls.pulse.classList.add('offline');
    liveEls.pulse.classList.remove('online');
  }
});

window.addEventListener('resize', drawLiveOverlay);

if (liveEls.liveAiTrackToggle) {
  const savedTrack = localStorage.getItem(LIVE_AI_TRACK_KEY);
  liveAiTrackEnabled = savedTrack === '1';
  liveEls.liveAiTrackToggle.checked = liveAiTrackEnabled;
  liveEls.liveAiTrackToggle.addEventListener('change', () => {
    liveAiTrackEnabled = Boolean(liveEls.liveAiTrackToggle.checked);
    localStorage.setItem(LIVE_AI_TRACK_KEY, liveAiTrackEnabled ? '1' : '0');
    liveAiTrackDetections = null;
    liveAiTrackPrevDetections = null;
    liveAiTrackCaptureMs = 0;
    liveAiTrackPrevCaptureMs = 0;
    lastServerTrackUpdatedAt = null;
    clearLiveOverlay();
    restartDetectionStatusTimer();
    if (liveAiTrackEnabled && !isAllCameraMode()) {
      refreshDetectionStatus();
      startLiveRaf();
    } else {
      stopLiveRaf();
    }
  });
}

liveEls.cameraSelect.addEventListener('change', () => setSelectedCamera(liveEls.cameraSelect.value));
document.querySelectorAll('[data-view-mode]').forEach((btn) => {
  btn.addEventListener('click', () => {
    viewMode = btn.dataset.viewMode;
    document.querySelectorAll('[data-view-mode]').forEach((b) => {
      const active = b === btn;
      b.classList.toggle('active', active);
      b.setAttribute('aria-selected', String(active));
    });
    syncViewMode();
  });
});
liveEls.addZoneBtn?.addEventListener('click', () => {
  if (drawingMode && draftPolygon?.points.length >= 3) {
    finishDraftPolygon();
    return;
  }
  drawingMode = !drawingMode;
  draftPolygon = null;
  zoneDrag = null;
  liveEls.addZoneBtn.textContent = drawingMode ? 'Cancel drawing' : 'Draw area';
  renderZones();
});
liveEls.fullFrameZoneBtn?.addEventListener('click', () => {
  addFullFrameZone();
  liveEls.status.textContent = 'Full-frame monitoring area added. Save areas to keep it.';
});
liveEls.saveZonesBtn?.addEventListener('click', async () => {
  try {
    liveEls.saveZonesBtn.disabled = true;
    cameraDetection().zones.forEach(normalizeZone);
    await api(`/api/cameras/${encodeURIComponent(selectedCamera.id)}`, { method: 'PUT', body: JSON.stringify(selectedCamera) });
    const payload = await api('/api/cameras');
    const cameraId = selectedCamera.id;
    cameras = payload.cameras || [];
    setSelectedCamera(cameraId);
    liveEls.status.textContent = 'Monitoring areas saved.';
    window.showToast?.('Monitoring areas saved.');
    await refreshDetectionStatus();
  } catch (error) {
    liveEls.status.textContent = error.message;
    window.showToast?.(error.message, true);
  } finally {
    liveEls.saveZonesBtn.disabled = false;
  }
});

window.addEventListener('resize', syncZoneOverlayToImage);
document.addEventListener('keydown', (event) => {
  if (event.key === 'Escape' && drawingMode) {
    drawingMode = false;
    draftPolygon = null;
    if (liveEls.addZoneBtn) liveEls.addZoneBtn.textContent = 'Draw area';
    renderDraftPolygon();
  }
});

async function init() {
  const me = await api('/api/auth/me');
  csrfToken = me.csrf_token;
  try {
    const runtime = await api('/api/config');
    const live = runtime.live || {};
    snapshotRefreshMs = Number.parseInt(live.snapshot_refresh_ms || DEFAULT_SNAPSHOT_REFRESH_MS, 10);
    detectionStatusRefreshMs = Number.parseInt(live.detection_status_refresh_ms || DEFAULT_DETECTION_STATUS_REFRESH_MS, 10);
  } catch {
    snapshotRefreshMs = DEFAULT_SNAPSHOT_REFRESH_MS;
    detectionStatusRefreshMs = DEFAULT_DETECTION_STATUS_REFRESH_MS;
  }
  if (isZonesPage) {
    try {
      const aiSettings = await api('/api/settings/ai');
      availableLabels = aiSettings.available_labels || [];
    } catch {
      availableLabels = [];
    }
  }
  await loadConfiguredLabels();
  const payload = await api('/api/cameras');
  cameras = payload.cameras || [];
  updateEmptyState();
  renderCameraOptions();
  bindZoneDrawing();
  syncViewMode();
  refreshTimer = setInterval(refreshFrame, snapshotRefreshMs);
  restartDetectionStatusTimer();
}

init().catch((error) => { liveEls.status.textContent = error.message; });
window.addEventListener('beforeunload', () => {
  clearInterval(refreshTimer);
  clearInterval(detectionStatusTimer);
});
