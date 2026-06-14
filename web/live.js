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
  detectionMeta: document.querySelector('.live-detection-meta'),
  // Zones-page stats (null on live page - harmless)
  statZoneCount: document.getElementById('statZoneCount'),
  statRuleCount: document.getElementById('statRuleCount'),
  statAlertRules: document.getElementById('statAlertRules'),
  statCameraName: document.getElementById('statCameraName'),
  liveAiTrackToggle: document.getElementById('liveAiTrackToggle'),
  liveAiTrackGroup: document.getElementById('liveAiTrackGroup'),
  liveAiTrackCanvas: document.getElementById('liveAiTrackCanvas'),
  cameraSelect: document.getElementById('cameraSelect'),
  cameraControlGroup: document.getElementById('cameraControlGroup'),
  cameraGrid: document.getElementById('cameraGrid'),
  // Zones-page drawing elements (null on live page - harmless)
  zoneOverlay: document.getElementById('zoneOverlay'),
  zoneList: document.getElementById('zoneList'),
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

let configuredLabels = null;

const LIVE_AI_TRACK_KEY = 'daygle.live.overlay.track.enabled';
// On by default; users can opt out per-browser via the toggle. The overlay only
// replays the background monitor's detections (already computed server-side
// for alerts/recording), so it never runs its own inference and adds no
// detector load - just the detection-status JSON poll and canvas drawing.
let liveAiTrackEnabled = true;
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
  return selectedCamera.detection;
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
  updatePtzVisibility();
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

// Build a structured summary of the monitor's latest cycle so the renderer
// can split the visual into a state chip, per-label chips, and a status line.
function summarizeDetectionStatus(payload, soundStatus = null, soundEnabled = false, soundMinConf = 0, belowThresholdSound = null) {
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

  // Persistent sound status chip - always shown to indicate whether sound
  // detection is enabled, idle, or recently fired.
  if (soundEnabled) {
    let soundChipPushed = false;
    if (soundStatus && soundStatus.last_detected_at) {
      const ageMs = Date.now() - Date.parse(soundStatus.last_detected_at);
      const soundConf = Number(soundStatus.last_confidence || 0);
      if (ageMs < 60000 && soundConf >= soundMinConf) {
        const soundLabel = soundStatus.last_class_label || soundStatus.last_class || 'sound';
        chips.push({ label: `🔊 ${soundLabel}`, confidence: soundConf, isSound: true });
        soundChipPushed = true;
      }
    }
    if (!soundChipPushed && belowThresholdSound) {
      chips.push({ label: `🔊 ${belowThresholdSound.label}`, confidence: belowThresholdSound.confidence, isSound: true, isBelowThreshold: true });
    } else if (!soundChipPushed) {
      chips.push({ label: '🔊 Listening', confidence: 0, isSound: true, isIdle: true });
    }
  } else {
    chips.push({ label: '🔊 Sound Off', confidence: 0, isSound: true, isDisabled: true });
  }

  if (payload.state === 'alerted') {
    const alerts = (payload.triggered_alerts || []).map((a) => a.rule_name).join(', ') || 'unknown rule';
    const parts = [`Alert triggered - ${alerts}`];
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
    return { state: 'detected', stateLabel: 'Detected', chips, message: `Detected ${labelStr} - ${suffix}.` };
  }

  const fallback = String(payload.reason || payload.ai_error || 'waiting for frames');
  return {
    state: payload.state || 'idle',
    stateLabel: payload.state ? payload.state[0].toUpperCase() + payload.state.slice(1) : 'Idle',
    chips,
    message: `Live AI: ${payload.state || 'waiting'} - ${fallback}`,
  };
}

function renderDetectionStatus(summary) {
  if (liveEls.detectionState) {
    liveEls.detectionState.textContent = `👁️ ${summary.stateLabel}`;
    liveEls.detectionState.className = 'chip ' + (
      summary.state === 'alerted' ? 'chip-warn' :
      summary.state === 'detected' ? 'chip-info' :
      summary.state === 'error' ? 'chip-warn' :
      'chip-dim'
    );
  }
  const visualChips = summary.chips.filter((c) => !c.isSound);
  const soundChips = summary.chips.filter((c) => c.isSound);

  // Sound chips go in the header row, to the left of the state chip.
  if (liveEls.detectionMeta && liveEls.detectionState) {
    liveEls.detectionMeta.querySelectorAll('.detection-chip').forEach((el) => el.remove());
    const soundHtml = soundChips.map((c) => {
      const cls = c.isDisabled ? 'detection-chip detection-chip-empty' : 'detection-chip detection-chip-sound';
      const text = c.confidence > 0 ? `${c.label} · ${Math.round(c.confidence * 100)}%` : c.label;
      return `<span class="${cls}">${escapeHtml(text)}</span>`;
    }).join('');
    liveEls.detectionState.insertAdjacentHTML('beforebegin', soundHtml);
  }

  // Object chips go in the chips row below the header.
  if (liveEls.detectionChips) {
    liveEls.detectionChips.innerHTML = visualChips.map((c) => {
      const variant = summary.state === 'alerted' ? 'detection-chip-alert' : '';
      const text = c.confidence > 0 ? `👁️ ${titleCase(c.label)} · ${Math.round(c.confidence * 100)}%` : `👁️ ${titleCase(c.label)}`;
      return `<span class="detection-chip ${variant}">${escapeHtml(text)}</span>`;
    }).join('');
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
  return detectionStatusRefreshMs;
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
    // Resolve minimum confidence for the fired sound class (falls back to lowest enabled rule threshold).
    const soundRules = selectedCamera.detection?.sound?.rules || [];
    const lastClass = soundStatus?.last_class;
    const matchedSoundRule = lastClass ? soundRules.find((r) => r.class === lastClass && r.enabled !== false) : null;
    const soundMinConf = matchedSoundRule
      ? Number(matchedSoundRule.confidence_threshold ?? 0.35)
      : soundRules.filter((r) => r.enabled !== false).reduce((min, r) => Math.min(min, Number(r.confidence_threshold ?? 0.35)), 0.35);
    // Find the highest-scoring sound class currently below its threshold (mirrors "outside zone" for objects).
    const liveConf = soundStatus?.last_confidences || {};
    let belowThresholdSound = null;
    for (const rule of soundRules) {
      if (rule.enabled === false) continue;
      const conf = Number(liveConf[rule.class] || 0);
      const threshold = Number(rule.confidence_threshold ?? 0.35);
      if (conf > 0 && conf < threshold && (!belowThresholdSound || conf > belowThresholdSound.confidence)) {
        belowThresholdSound = { label: rule.name || rule.class, confidence: conf };
      }
    }
    ingestServerTrackDetections(payload);
    renderDetectionStatus(summarizeDetectionStatus(payload, soundStatus, soundEnabled, soundMinConf, belowThresholdSound));
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
  liveAiTrackDetections = null;
  liveAiTrackPrevDetections = null;
  liveAiTrackCaptureMs = 0;
  liveAiTrackPrevCaptureMs = 0;
  lastServerTrackUpdatedAt = null;
  clearLiveOverlay();
  if (liveEls.cameraSelect) liveEls.cameraSelect.value = selectedCamera.id;
  updateFrameHeader(selectedCamera);
  if (isZonesPage) {
    // selectedZoneIndex, updateZonesStats, renderZones are defined in zones.js
    selectedZoneIndex = null;
    updateZonesStats();
    renderZones();
  }
  refreshFrame();
  refreshDetectionStatus();
  updatePtzVisibility();
}

function renderCameraOptions() {
  liveEls.cameraSelect.innerHTML = cameras.map((camera) => `<option value="${escapeHtml(camera.id)}">${escapeHtml(camera.name || camera.id)}</option>`).join('');
  setSelectedCamera(liveEls.cameraSelect.value || cameras[0]?.id);
}

liveEls.frame.addEventListener('load', () => {
  liveEls.frame.dataset.loading = 'false';
  if (isZonesPage) syncZoneOverlayToImage(); // syncZoneOverlayToImage defined in zones.js
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
  liveAiTrackEnabled = savedTrack !== '0';
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

// ─── PTZ Controls ─────────────────────────────────────────────────────────────

const ptzOverlay = document.getElementById('ptzOverlay');
let ptzActive = false;

function updatePtzVisibility() {
  if (!ptzOverlay) return;
  const enabled = selectedCamera?.ptz?.enabled === true;
  ptzOverlay.hidden = !enabled || isAllCameraMode();
}

async function sendPtz(command) {
  if (!selectedCamera) return;
  try {
    await api(`/api/cameras/${encodeURIComponent(selectedCamera.id)}/ptz`, {
      method: 'POST',
      body: JSON.stringify({ command }),
    });
  } catch (err) {
    console.warn('PTZ command failed:', command, err.message);
    window.showToast?.(`PTZ error: ${err.message}`, true);
  }
}

if (ptzOverlay) {
  ptzOverlay.querySelectorAll('[data-ptz]').forEach((btn) => {
    const startCmd = btn.dataset.ptz;
    const stopCmd = btn.dataset.ptzStop;

    const start = (e) => {
      e.preventDefault();
      if (ptzActive && startCmd !== 'stop') return;
      ptzActive = startCmd !== 'stop';
      sendPtz(startCmd);
    };

    const stop = (e) => {
      e.preventDefault();
      if (!stopCmd || startCmd === 'stop') return;
      ptzActive = false;
      sendPtz(stopCmd);
    };

    btn.addEventListener('mousedown', start);
    btn.addEventListener('touchstart', start, { passive: false });
    btn.addEventListener('mouseup', stop);
    btn.addEventListener('mouseleave', stop);
    btn.addEventListener('touchend', stop);
    btn.addEventListener('touchcancel', stop);
  });
}

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
  if (isZonesPage) bindZoneDrawing(); // bindZoneDrawing defined in zones.js
  syncViewMode();
  refreshTimer = setInterval(refreshFrame, snapshotRefreshMs);
  restartDetectionStatusTimer();
}

init().catch((error) => { liveEls.status.textContent = error.message; });
window.addEventListener('beforeunload', () => {
  clearInterval(refreshTimer);
  clearInterval(detectionStatusTimer);
});
