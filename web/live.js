const liveEls = {
  frame: document.getElementById('liveFrame'),
  frameWrap: document.getElementById('liveFrameWrap'),
  status: document.getElementById('liveStatus'),
  detectionStatus: document.getElementById('liveDetectionStatus'),
  overlayToggle: document.getElementById('overlayToggle'),
  liveAiTrackToggle: document.getElementById('liveAiTrackToggle'),
  liveAiTrackLabel: document.getElementById('liveAiTrackLabel'),
  liveAiTrackCanvas: document.getElementById('liveAiTrackCanvas'),
  cameraSelect: document.getElementById('cameraSelect'),
  viewModeSelect: document.getElementById('viewModeSelect'),
  cameraGrid: document.getElementById('cameraGrid'),
  zoneOverlay: document.getElementById('zoneOverlay'),
  zoneList: document.getElementById('zoneList'),
  cameraRecordingControls: document.getElementById('cameraRecordingControls'),
  addZoneBtn: document.getElementById('addZoneBtn'),
  fullFrameZoneBtn: document.getElementById('fullFrameZoneBtn'),
  saveZonesBtn: document.getElementById('saveZonesBtn'),
};

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
let liveAiTrackEnabled = true;
let liveAiTrackInFlight = false;
let liveAiTrackDetections = null;
let liveAiTrackPrevDetections = null;
// Wall-clock time (ms) at which each sample's frame was captured, so the
// overlay can be projected onto the frame currently on screen regardless of
// how long detection took.
let liveAiTrackCaptureMs = 0;
let liveAiTrackPrevCaptureMs = 0;
let liveRafId = null;
const LIVE_AI_TRACK_MAX_WIDTH = 640;
const LIVE_AI_TRACK_MAX_HEIGHT = 360;
const LIVE_AI_TRACK_MAX_LEAD_MS = 1500;
const liveAiTrackOffscreenCanvas = document.createElement('canvas');

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

async function detectLiveFrameDetections() {
  if (!liveAiTrackEnabled || !liveEls.frame || isAllCameraMode()) return;
  if (!liveEls.frame.complete || liveEls.frame.naturalWidth <= 0) return;
  if (liveAiTrackInFlight) return;
  liveAiTrackInFlight = true;
  try {
    const srcW = liveEls.frame.naturalWidth;
    const srcH = liveEls.frame.naturalHeight;
    const scale = Math.min(1, LIVE_AI_TRACK_MAX_WIDTH / srcW, LIVE_AI_TRACK_MAX_HEIGHT / srcH);
    const fw = Math.max(1, Math.round(srcW * scale));
    const fh = Math.max(1, Math.round(srcH * scale));
    liveAiTrackOffscreenCanvas.width = fw;
    liveAiTrackOffscreenCanvas.height = fh;
    const ctx = liveAiTrackOffscreenCanvas.getContext('2d');
    if (!ctx) return;
    // Anchor the result to the moment the frame is captured, not to when the
    // (latency-delayed) response arrives.
    const captureMs = performance.now();
    ctx.drawImage(liveEls.frame, 0, 0, fw, fh);
    const blob = await new Promise((resolve) => liveAiTrackOffscreenCanvas.toBlob((b) => resolve(b), 'image/jpeg', 0.8));
    if (!blob) return;
    const payload = await api('/api/detect/frame', {
      method: 'POST',
      headers: { 'Content-Type': 'image/jpeg' },
      body: blob,
    });
    const newDetections = (Array.isArray(payload?.detections) ? payload.detections : []).map((det) => {
      const box = normalizeDetectionBox(det?.box || {}, fw, fh);
      if (!box) return null;
      return { ...det, box };
    }).filter(Boolean);
    liveAiTrackPrevDetections = liveAiTrackDetections;
    liveAiTrackPrevCaptureMs = liveAiTrackCaptureMs;
    liveAiTrackCaptureMs = captureMs;
    liveAiTrackDetections = newDetections;
    drawLiveOverlay();
  } catch (_err) {
    // Retain last successful detections on transient failure.
  } finally {
    liveAiTrackInFlight = false;
  }
}

function cameraDetection() {
  selectedCamera.detection ||= { zones: [] };
  selectedCamera.detection.zones ||= [];
  selectedCamera.detection.motion_email_enabled ??= true;
  return selectedCamera.detection;
}

function cameraRecording() {
  selectedCamera.recording ||= { enabled: true, record_on_alert: true, continuous: false };
  selectedCamera.recording.enabled ??= true;
  selectedCamera.recording.record_on_alert ??= true;
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
  const overlay = liveEls.overlayToggle?.checked ? '1' : '0';
  const cameraId = encodeURIComponent(camera?.id || '');
  return `/api/live/snapshot?overlay=${overlay}&camera_id=${cameraId}&t=${Date.now()}`;
}

function isAllCameraMode() {
  return pageMode === 'live' && liveEls.viewModeSelect?.value === 'all';
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
  liveEls.cameraGrid.innerHTML = cameras.map((camera) => `
    <article class="live-camera-tile">
      <img data-camera-id="${escapeHtml(camera.id)}" alt="${escapeHtml(camera.name || camera.id)} live footage" />
      <div class="live-status">${escapeHtml(camera.name || camera.id)}</div>
    </article>
  `).join('');
  renderCameraGridFrames();
}

function syncViewMode() {
  const allMode = isAllCameraMode();
  if (liveEls.frameWrap) liveEls.frameWrap.hidden = allMode;
  if (liveEls.cameraGrid) liveEls.cameraGrid.hidden = !allMode;
  if (liveEls.cameraSelect?.closest('label')) liveEls.cameraSelect.closest('label').hidden = allMode;
  if (liveEls.liveAiTrackLabel) liveEls.liveAiTrackLabel.hidden = allMode;
  if (allMode) {
    clearLiveOverlay();
    renderCameraGrid();
  }
  refreshDetectionStatus();
}

function formatDetectionStatus(payload) {
  if (!payload) return 'Live AI status unavailable.';

  // Build "label (XX%)" strings, deduplicating by highest confidence, filtering to active rules only
  const confMap = new Map();
  for (const d of (payload.detections || [])) {
    const label = String(d.label || '').trim().toLowerCase();
    const conf = Number(d.confidence || 0);
    if (!label) continue;
    if (configuredLabels && !configuredLabels.has(label)) continue;
    if (!confMap.has(label) || conf > confMap.get(label)) confMap.set(label, conf);
  }
  // Fall back to detected_labels filtered to active rules
  if (confMap.size === 0) {
    for (const label of (payload.detected_labels || [])) {
      const l = String(label || '').trim().toLowerCase();
      if (l && (!configuredLabels || configuredLabels.has(l))) confMap.set(l, 0);
    }
  }
  const labelStr = confMap.size
    ? Array.from(confMap.entries())
        .map(([label, conf]) => conf > 0 ? `${label} (${Math.round(conf * 100)}%)` : label)
        .join(', ')
    : null;

  if (payload.state === 'alerted') {
    const alerts = (payload.triggered_alerts || []).map((a) => a.rule_name).join(', ') || 'unknown rule';
    const parts = [`Live AI: alert triggered - ${alerts}`];
    if (labelStr) parts.push(`detected ${labelStr}`);
    if (payload.recording_state) parts.push(`recording ${payload.recording_state}${payload.recording_id ? ` #${payload.recording_id}` : ''}`);
    return `${parts.join('; ')}.`;
  }

  if (payload.state === 'checked') {
    if (!labelStr) return 'Live AI: scan complete - no detections.';
    const reason = String(payload.reason || '');
    let suffix;
    if (/debounce|suppressed/i.test(reason)) suffix = 'event suppressed (debounce active)';
    else if (/cooldown/i.test(reason)) suffix = 'alert rule in cooldown';
    else if (/no alert rule|no matching|no new alert/i.test(reason)) suffix = 'no matching alert rule';
    else if (/no detections matched/i.test(reason)) suffix = 'outside monitored zones';
    else suffix = reason || 'no alert triggered';
    return `Live AI: detected ${labelStr} - ${suffix}.`;
  }

  return `Live AI: ${payload.state || 'waiting'} - ${payload.reason || payload.ai_error || 'waiting for frames'}`;
}

async function refreshDetectionStatus() {
  if (!liveEls.detectionStatus) return;
  if (isAllCameraMode()) {
    liveEls.detectionStatus.textContent = 'Live AI: showing all cameras. Select one camera for detailed status.';
    return;
  }
  if (!selectedCamera) return;
  try {
    const cameraId = encodeURIComponent(selectedCamera.id);
    liveEls.detectionStatus.textContent = formatDetectionStatus(await api(`/api/live/detection-status?camera_id=${cameraId}`));
  } catch (error) {
    liveEls.detectionStatus.textContent = `Live AI status unavailable: ${error.message}`;
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
  clearLiveOverlay();
  if (liveEls.cameraSelect) liveEls.cameraSelect.value = selectedCamera.id;
  if (isZonesPage) {
    renderZones();
    renderCameraRecordingControls();
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
  return `<option value="">Object...</option><option value="motion" ${motionSelected ? 'selected' : ''}>motion</option>${coco}`;
}

function renderObjectRules(zone, zoneIndex) {
  zone.object_rules = normalizeObjectRules(zone);
  if (!zone.object_rules.length) {
    return '<div class="empty compact-empty">No object rules yet. Choose an object below to add recording and alert settings for this zone.</div>';
  }
  return zone.object_rules.map((rule, ruleIndex) => `
    <div class="zone-object-rule" data-zone-rule="${zoneIndex}:${ruleIndex}">
      <label><span>Object</span><select data-zone-rule-label="${zoneIndex}:${ruleIndex}">${objectRuleOptions(rule.label)}</select></label>
      <label><span>Rule</span><select data-zone-rule-enabled="${zoneIndex}:${ruleIndex}"><option value="true" ${rule.enabled !== false ? 'selected' : ''}>Enabled</option><option value="false" ${rule.enabled === false ? 'selected' : ''}>Disabled</option></select></label>
      <label><span>Record</span><select data-zone-rule-record="${zoneIndex}:${ruleIndex}"><option value="true" ${rule.record_on_detect !== false ? 'selected' : ''}>On detect</option><option value="false" ${rule.record_on_detect === false ? 'selected' : ''}>Off</option></select></label>
      <label><span>Alert</span><select data-zone-rule-alert="${zoneIndex}:${ruleIndex}"><option value="true" ${rule.alert_on_detect !== false ? 'selected' : ''}>On detect</option><option value="false" ${rule.alert_on_detect === false ? 'selected' : ''}>Off</option></select></label>
      <label><span>Min confidence</span><input data-zone-rule-confidence="${zoneIndex}:${ruleIndex}" type="number" min="0" max="1" step="0.01" value="${escapeHtml(rule.min_confidence)}" /></label>
      <label><span>Cooldown</span><input data-zone-rule-cooldown="${zoneIndex}:${ruleIndex}" type="number" min="0" step="1" value="${escapeHtml(rule.cooldown_seconds)}" /></label>
      <label><span>Email</span><select data-zone-rule-email="${zoneIndex}:${ruleIndex}"><option value="false" ${rule.email_enabled !== true ? 'selected' : ''}>Off</option><option value="true" ${rule.email_enabled === true ? 'selected' : ''}>On</option></select></label>
      <input data-zone-rule-recipients="${zoneIndex}:${ruleIndex}" value="${escapeHtml(rule.email_recipients.join(', '))}" placeholder="Email recipients" />
      <label><span>Push</span><select data-zone-rule-push="${zoneIndex}:${ruleIndex}"><option value="false" ${rule.push_enabled !== true ? 'selected' : ''}>Off</option><option value="true" ${rule.push_enabled === true ? 'selected' : ''}>On</option></select></label>
      <label><span>Active start</span><input data-zone-rule-active-start="${zoneIndex}:${ruleIndex}" type="time" value="${escapeHtml(rule.active_start || '')}" /></label>
      <label><span>Active end</span><input data-zone-rule-active-end="${zoneIndex}:${ruleIndex}" type="time" value="${escapeHtml(rule.active_end || '')}" /></label>
      <button class="secondary" type="button" data-delete-zone-rule="${zoneIndex}:${ruleIndex}">Remove</button>
    </div>
  `).join('');
}

function renderZones() {
  if (!isZonesPage || !selectedCamera) return;
  syncZoneOverlayToImage();
  const zones = cameraDetection().zones;
  zones.forEach(normalizeZone);
  liveEls.zoneOverlay.innerHTML = zones.map((zone, index) => (zone.enabled === false ? '' : renderZoneBox(zone, index))).join('');
  if (!zones.length) {
    liveEls.zoneList.innerHTML = '<div class="empty">No monitoring areas yet. Click "Draw area", place corner dots on the footage, then click the first dot to close the area.</div>';
    return;
  }
  liveEls.zoneList.innerHTML = zones.map((zone, index) => `
    <div class="item zone-row ${index === selectedZoneIndex ? 'selected' : ''}${zone.enabled === false ? ' disabled' : ''}" data-select-zone="${index}">
      <div class="zone-row-main">
        <input data-zone-name="${index}" value="${escapeHtml(zone.name || `Zone ${index + 1}`)}" />
        <label><span>Zone</span><select data-zone-enabled="${index}"><option value="true" ${zone.enabled !== false ? 'selected' : ''}>Displayed</option><option value="false" ${zone.enabled === false ? 'selected' : ''}>Hidden</option></select></label>
        <button class="secondary" type="button" data-delete-zone="${index}">Remove</button>
      </div>
      <div class="zone-object-rules">
        <div class="zone-object-rules-header">
          <strong>Detection rules</strong>
          <select data-add-zone-rule="${index}">${objectRuleOptions('')}</select>
        </div>
        ${renderObjectRules(zone, index)}
      </div>
    </div>
  `).join('');
  bindZoneControls(zones);
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
  document.querySelectorAll('[data-add-zone-rule]').forEach((select) => {
    select.addEventListener('change', () => {
      const label = select.value;
      if (!label) return;
      const zone = zones[Number(select.dataset.addZoneRule)];
      zone.object_rules = normalizeObjectRules(zone);
      if (!zone.object_rules.some((rule) => rule.label === label)) zone.object_rules.push(defaultObjectRule(label));
      zone.object_labels = zone.object_rules.filter((r) => r.label !== 'motion').map((rule) => rule.label);
      renderZones();
    });
  });
  document.querySelectorAll('[data-delete-zone-rule]').forEach((button) => {
    button.addEventListener('click', () => {
      const { zoneIndex, ruleIndex, rule } = parseZoneRuleKey(button.dataset.deleteZoneRule);
      if (rule?.label === 'motion') zones[zoneIndex].monitor_motion = false;
      zones[zoneIndex].object_rules.splice(ruleIndex, 1);
      zones[zoneIndex].object_labels = zones[zoneIndex].object_rules.filter((r) => r.label !== 'motion').map((r) => r.label);
      renderZones();
    });
  });
  bindRuleFields();
}

function bindRuleFields() {
  const bindings = [
    ['zoneRuleLabel', 'label', (value) => value],
    ['zoneRuleEnabled', 'enabled', (value) => value === 'true'],
    ['zoneRuleRecord', 'record_on_detect', (value) => value === 'true'],
    ['zoneRuleAlert', 'alert_on_detect', (value) => value === 'true'],
    ['zoneRuleConfidence', 'min_confidence', (value) => clamp(Number(value || 0), 0, 1)],
    ['zoneRuleCooldown', 'cooldown_seconds', (value) => Math.max(0, Number.parseInt(value || 0, 10) || 0)],
    ['zoneRuleEmail', 'email_enabled', (value) => value === 'true'],
    ['zoneRuleRecipients', 'email_recipients', normalizeEmailList],
    ['zoneRulePush', 'push_enabled', (value) => value === 'true'],
    ['zoneRuleActiveStart', 'active_start', (value) => value || null],
    ['zoneRuleActiveEnd', 'active_end', (value) => value || null],
  ];
  bindings.forEach(([datasetKey, ruleKey, transform]) => {
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
}

function renderCameraRecordingControls() {
  if (!selectedCamera || !liveEls.cameraRecordingControls) return;
  const recording = cameraRecording();
  const detection = cameraDetection();
  liveEls.cameraRecordingControls.innerHTML = `
    <label><span>Recording</span><select data-camera-recording="enabled"><option value="true" ${recording.enabled !== false ? 'selected' : ''}>Enabled</option><option value="false" ${recording.enabled === false ? 'selected' : ''}>Disabled</option></select></label>
    <label><span>Alert clips</span><select data-camera-recording="record_on_alert"><option value="true" ${recording.record_on_alert !== false ? 'selected' : ''}>Enabled</option><option value="false" ${recording.record_on_alert === false ? 'selected' : ''}>Disabled</option></select></label>
    <label><span>Continuous</span><select data-camera-recording="continuous"><option value="false" ${recording.continuous !== true ? 'selected' : ''}>Disabled</option><option value="true" ${recording.continuous === true ? 'selected' : ''}>Enabled</option></select></label>
    <label><span>Motion Email Alerts</span><select data-camera-detection="motion_email_enabled"><option value="true" ${detection.motion_email_enabled !== false ? 'selected' : ''}>Enabled</option><option value="false" ${detection.motion_email_enabled === false ? 'selected' : ''}>Disabled</option></select></label>
  `;
  liveEls.cameraRecordingControls.querySelectorAll('[data-camera-recording]').forEach((select) => {
    select.addEventListener('change', () => {
      cameraRecording()[select.dataset.cameraRecording] = select.value === 'true';
    });
  });
  liveEls.cameraRecordingControls.querySelectorAll('[data-camera-detection]').forEach((select) => {
    select.addEventListener('change', () => {
      cameraDetection()[select.dataset.cameraDetection] = select.value === 'true';
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
    monitor_anpr: true,
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
    monitor_anpr: true,
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
  liveEls.status.textContent = liveEls.overlayToggle.checked
    ? `${selectedCamera?.name || 'Camera'} - matched alert boxes on`
    : `${selectedCamera?.name || 'Camera'} - matched alert boxes off`;
  detectLiveFrameDetections();
  if (liveAiTrackEnabled && !isAllCameraMode()) {
    startLiveRaf();
  } else {
    stopLiveRaf();
  }
});

liveEls.frame.addEventListener('error', () => {
  liveEls.frame.dataset.loading = 'false';
  clearLiveOverlay();
  liveEls.status.textContent = 'Unable to load live footage. Retrying...';
});

window.addEventListener('resize', drawLiveOverlay);

if (liveEls.liveAiTrackToggle) {
  const savedTrack = localStorage.getItem(LIVE_AI_TRACK_KEY);
  liveAiTrackEnabled = savedTrack !== '0';
  liveEls.liveAiTrackToggle.checked = liveAiTrackEnabled;
  liveEls.liveAiTrackToggle.addEventListener('change', () => {
    liveAiTrackEnabled = Boolean(liveEls.liveAiTrackToggle.checked);
    localStorage.setItem(LIVE_AI_TRACK_KEY, liveAiTrackEnabled ? '1' : '0');
    liveAiTrackDetections = null;
    liveAiTrackPrevDetections = null;
    liveAiTrackCaptureMs = 0;
    liveAiTrackPrevCaptureMs = 0;
    clearLiveOverlay();
    if (liveAiTrackEnabled && !isAllCameraMode()) {
      detectLiveFrameDetections();
      startLiveRaf();
    } else {
      stopLiveRaf();
    }
  });
}

liveEls.overlayToggle.addEventListener('change', refreshFrame);
liveEls.cameraSelect.addEventListener('change', () => setSelectedCamera(liveEls.cameraSelect.value));
liveEls.viewModeSelect?.addEventListener('change', syncViewMode);
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
  renderCameraOptions();
  bindZoneDrawing();
  syncViewMode();
  refreshTimer = setInterval(refreshFrame, snapshotRefreshMs);
  detectionStatusTimer = setInterval(refreshDetectionStatus, detectionStatusRefreshMs);
}

init().catch((error) => { liveEls.status.textContent = error.message; });
window.addEventListener('beforeunload', () => {
  clearInterval(refreshTimer);
  clearInterval(detectionStatusTimer);
});
