const liveEls = {
  frame: document.getElementById('liveFrame'),
  frameWrap: document.getElementById('liveFrameWrap'),
  status: document.getElementById('liveStatus'),
  pulse: document.getElementById('livePulse'),
  frameTitle: document.getElementById('liveFrameTitle'),
  cameraEmpty: document.getElementById('liveCameraEmpty'),
  streamDetailsCard: document.getElementById('streamDetailsCard'),
  streamDetailBackend: document.getElementById('streamDetailBackend'),
  streamDetailResolution: document.getElementById('streamDetailResolution'),
  streamDetailFps: document.getElementById('streamDetailFps'),
  streamDetailFpsLive: document.getElementById('streamDetailFpsLive'),
  streamDetailSource: document.getElementById('streamDetailSource'),
  detectionSubtitle: document.getElementById('liveDetectionSubtitle'),
  detectionStatus: document.getElementById('liveDetectionStatus'),
  detectionState: document.getElementById('liveDetectionState'),
  soundState: document.getElementById('liveSoundState'),
  soundStatus: document.getElementById('liveSoundStatus'),
  visionLane: document.getElementById('liveVisionLane'),
  visionBody: document.getElementById('liveVisionBody'),
  hearingBody: document.getElementById('liveHearingBody'),
  motionLane: document.getElementById('liveMotionLane'),
  motionState: document.getElementById('liveMotionState'),
  motionBar: document.getElementById('liveMotionBar'),
  motionValue: document.getElementById('liveMotionValue'),
  motionTriggerTick: document.getElementById('liveMotionTriggerTick'),
  motionCaption: document.getElementById('liveMotionCaption'),
  monitorPill: document.getElementById('liveMonitorPill'),
  monitorPillText: document.getElementById('liveMonitorPillText'),
  // Zones-page stats (null on live page - harmless)
  statZoneCount: document.getElementById('statZoneCount'),
  statRuleCount: document.getElementById('statRuleCount'),
  statAlertRules: document.getElementById('statAlertRules'),
  statCameraName: document.getElementById('statCameraName'),
  liveStreamSelect: document.getElementById('liveStreamSelect'),
  liveAiTrackToggle: document.getElementById('liveAiTrackToggle'),
  liveAiTrackGroup: document.getElementById('liveAiTrackGroup'),
  liveAiTrackCanvas: document.getElementById('liveAiTrackCanvas'),
  livePtzToggle: document.getElementById('livePtzToggle'),
  livePtzToggleGroup: document.getElementById('livePtzToggleGroup'),
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
// CSRF token is now shared via window.daygleAuth (set by loadAuth() via
// setApiAuth from web/utils.js), so there's no page-local `csrfToken`.
let cameras = [];
let availableLabels = [];
let selectedCamera = null;
// Motion-lane trigger reference: the lowest Sensitivity (%) among the
// selected camera's enabled motion zones - the "fires above" tick on the bar.
let motionTriggerSensitivityPct = 0;
// Runtime stream metadata is populated from /api/status. Camera configuration
// may intentionally leave FPS on Auto, so never render the old 15 FPS fallback
// when the backend has a better source-rate value.
const cameraRuntimeFps = {};

let configuredLabels = null;

// STREAM_SOURCE_KEY now lives in web/utils.js (exposed on window.daygleUi and
// visible as a bare global constant). This page used to redeclare it locally,
// which forced every consumer to look in three places for the same string.
const LIVE_STREAM_KEY = 'daygle.live.stream';
let liveStreamSource = 'detection';
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

// api() is provided by web/utils.js (loaded before this script) - it reads
// the CSRF token from window.daygleAuth.csrfToken, sets Content-Type
// application/json on JSON-bodied requests, and handles 401 redirects so
// every page shares identical auth and error semantics.

// Build the live overlay's label allow-list from the SELECTED camera's own
// object rules only. Scoping per camera prevents a label configured on one
// camera (e.g. a "car" rule on Front Yard) from surfacing detections on a
// different camera's live view, where the server still reports every class the
// shared detector found. Rebuilt whenever the selected camera changes.
function rebuildConfiguredLabels() {
  if (!selectedCamera) {
    configuredLabels = null;
    return;
  }
  const labels = new Map([['motion', 0.45]]);
  const setMin = (label, conf) => {
    if (!label) return;
    if (!labels.has(label) || conf < labels.get(label)) labels.set(label, conf);
  };
  for (const zone of (selectedCamera?.detection?.zones || [])) {
    for (const rule of (zone?.object_rules || [])) {
      if (rule.enabled !== false && (rule.email_enabled === true || rule.push_enabled === true || rule.record_on_detect !== false)) {
        const label = String(rule.label || '').trim().toLowerCase();
        setMin(label, Number(rule.min_confidence ?? 0.5));
      }
    }
  }
  configuredLabels = labels;
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
  const streamParam = liveStreamSource === 'recording' ? '&stream=recording' : '';
  return `/api/live/snapshot?camera_id=${cameraId}&t=${Date.now()}${streamParam}`;
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

// True when the backend reports a live measured source rate (source
// 'detected') for this camera. Configured and fallback values are static
// metadata, so they render without the live pulse indicator.
function cameraFpsIsLive(camera) {
  const runtime = cameraRuntimeFps[camera?.id];
  const detectedFps = Number(runtime?.detected);
  return runtime?.source === 'detected' && Number.isFinite(detectedFps) && detectedFps > 0;
}

function cameraDisplayFps(camera) {
  const runtime = cameraRuntimeFps[camera?.id];
  const runtimeSource = runtime?.source;
  const detectedFps = Number(runtime?.detected);
  if (cameraFpsIsLive(camera)) return detectedFps;
  if (runtimeSource === 'configured') {
    const configuredFps = Number(runtime?.configured);
    if (Number.isFinite(configuredFps) && configuredFps > 0) return configuredFps;
  }
  // A fallback effective value is an internal buffer-drain default, not the
  // camera's source rate; never present it as hardware FPS. A configured value
  // is safe to show while the runtime probe is still warming up.
  const configuredFps = Number(camera?.fps);
  return Number.isFinite(configuredFps) && configuredFps > 0 ? configuredFps : null;
}

function formatCameraFps(camera) {
  const fps = cameraDisplayFps(camera);
  if (fps == null) {
    return cameraRuntimeFps[camera?.id] ? 'Detecting FPS…' : 'FPS unavailable';
  }
  // A measured source rate is often fractional (e.g. 24.967 from ffprobe),
  // so pin it to one decimal - the trailing digit keeps a measured reading
  // visually distinct from a whole-number configured rate. Configured rates
  // are integers set by the operator, so they render as-is.
  if (cameraFpsIsLive(camera)) return `${fps.toFixed(1)} fps`;
  return `${Math.round(fps)} fps`;
}

function renderCameraGrid() {
  if (!liveEls.cameraGrid) return;
  liveEls.cameraGrid.innerHTML = cameras.map((camera) => {
    const res = `${camera.width || 1280}×${camera.height || 720}`;
    const fps = formatCameraFps(camera);
    return `
      <article class="live-camera-tile">
        <div class="live-camera-tile-image">
          <img data-camera-id="${escapeHtml(camera.id)}" alt="${escapeHtml(camera.name || camera.id)} live footage" />
          <div class="live-status live-status-online">LIVE</div>
        </div>
        <div class="live-camera-tile-info">
          <div class="live-camera-tile-name">${escapeHtml(camera.name || camera.id)}</div>
          <div class="live-camera-tile-meta">${escapeHtml(res)} · ${escapeHtml(fps)}</div>
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
  if (liveEls.streamDetailsCard) liveEls.streamDetailsCard.hidden = allMode;
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
  const backend = camera.backend === 'rtsp' ? 'RTSP' : 'ONVIF';
  const res = `${camera.width || 1280} × ${camera.height || 720}`;
  const fps = formatCameraFps(camera);
  const source = liveStreamSource === 'recording' ? 'Recording (high-res)' : 'Detection';
  if (liveEls.streamDetailBackend) liveEls.streamDetailBackend.textContent = backend;
  if (liveEls.streamDetailResolution) liveEls.streamDetailResolution.textContent = res;
  if (liveEls.streamDetailFps) liveEls.streamDetailFps.textContent = fps;
  // Toggle the live-measured pulse indicator: visible only while the backend
  // reports a detected source rate, so a static configured value stays calm.
  const fpsIsLive = cameraFpsIsLive(camera);
  if (liveEls.streamDetailFps) liveEls.streamDetailFps.classList.toggle('fps-live', fpsIsLive);
  if (liveEls.streamDetailFpsLive) liveEls.streamDetailFpsLive.hidden = !fpsIsLive;
  if (liveEls.streamDetailSource) liveEls.streamDetailSource.textContent = source;
}

function updateEmptyState() {
  if (!liveEls.cameraEmpty) return;
  if (cameras.length === 0) {
    liveEls.cameraEmpty.hidden = false;
    if (liveEls.frameWrap) liveEls.frameWrap.hidden = true;
    if (liveEls.cameraGrid) liveEls.cameraGrid.hidden = true;
    if (liveEls.cameraControlGroup) liveEls.cameraControlGroup.hidden = true;
    if (liveEls.liveAiTrackGroup) liveEls.liveAiTrackGroup.hidden = true;
    if (liveEls.livePtzToggleGroup) liveEls.livePtzToggleGroup.hidden = true;
    if (liveEls.streamDetailsCard) liveEls.streamDetailsCard.hidden = true;
  } else {
    liveEls.cameraEmpty.hidden = true;
  }
}

// Summarise the sound detector's status the same way as objects: a persistent
// state chip (Listening / Heard / Sound Off), a list of heard-sound pills, and a
// diagnostic message explaining why a heard sound did or didn't alert.
function summarizeSoundStatus(soundStatus, soundEnabled) {
  if (!soundEnabled) {
    return { soundState: { label: 'Sound Off', state: 'disabled' }, soundChips: [], soundMessage: '' };
  }
  const soundChips = [];
  let heard = false;
  // A sound that fired within the last minute shows as a "Heard" pill.
  if (soundStatus && soundStatus.last_detected_at) {
    const ageMs = Date.now() - Date.parse(soundStatus.last_detected_at);
    if (ageMs < 60000) {
      soundChips.push({ label: soundStatus.last_class_label || soundStatus.last_class || 'sound', confidence: Number(soundStatus.last_confidence || 0) });
      heard = true;
    }
  }
  // The server explains the current listening snapshot: a sound heard below its
  // alert threshold, or one suppressed because its rule is still in cooldown -
  // the sound counterpart to the object "outside zones / in cooldown" message.
  let soundMessage = '';
  const reason = soundStatus && soundStatus.reason;
  if (reason && (reason.code === 'cooldown' || reason.code === 'below_threshold')) {
    const label = reason.class_label || reason.class || 'Sound';
    const conf = Math.round(Number(reason.confidence || 0) * 100);
    if (reason.code === 'cooldown') {
      const remain = Math.round(Number(reason.cooldown_remaining || 0));
      soundMessage = `${label} heard (${conf}%) - alert rule in cooldown${remain ? ` (${remain}s left)` : ''}.`;
    } else {
      const thr = Math.round(Number(reason.threshold || 0) * 100);
      soundMessage = `${label} heard (${conf}%) - below alert threshold (${thr}%).`;
    }
    // Surface the heard-but-not-alerted sound as a pill too (faint when it never
    // crossed the threshold), unless a fired "Heard" pill already covers it.
    if (!heard) {
      soundChips.push({ label, confidence: Number(reason.confidence || 0), isBelowThreshold: reason.code === 'below_threshold' });
    }
  }
  return {
    soundState: heard ? { label: 'Heard', state: 'detected' } : { label: 'Listening', state: 'idle' },
    soundChips,
    soundMessage,
  };
}

// Build a structured summary of the monitor's latest cycle so the renderer
// can split the visual into a state chip, per-label chips, and a status line.
function summarizeDetectionStatus(payload, soundStatus = null, soundEnabled = false) {
  const sound = summarizeSoundStatus(soundStatus, soundEnabled);
  if (!payload) {
    return { state: 'idle', stateLabel: 'Idle', chips: [], ...sound, message: 'Live AI status unavailable.' };
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
    ? chips.map((c) => c.confidence > 0 ? `${sentenceCase(c.label)} (${Math.round(c.confidence * 100)}%)` : sentenceCase(c.label)).join(', ')
    : null;

  const motionData = {
    motion_confidence: payload.motion_confidence,
    motion_fraction: payload.motion_fraction,
    motion_signal: payload.motion_signal,
  };

  if (payload.state === 'alerted') {
    const alerts = (payload.triggered_alerts || []).map((a) => a.rule_name).join(', ') || 'unknown rule';
    const parts = [`Alert triggered - ${alerts}`];
    if (labelStr) parts.push(`detected ${labelStr}`);
    if (payload.recording_state) parts.push(`recording ${payload.recording_state}${payload.recording_id ? ` #${payload.recording_id}` : ''}`);
    return { state: 'alerted', stateLabel: 'Alerted', chips, ...sound, message: parts.join('; ') + '.', ...motionData };
  }

  if (payload.state === 'checked') {
    if (!labelStr) {
      return { state: 'monitoring', stateLabel: 'Monitoring', chips, ...sound, message: '', ...motionData };
    }
    const reason = String(payload.reason || '');
    let suffix;
    if (/debounce|suppressed/i.test(reason)) suffix = 'event suppressed (debounce active)';
    else if (/cooldown/i.test(reason)) suffix = 'alert rule in cooldown';
    else if (/no alert rule|no matching|no new alert/i.test(reason)) suffix = 'no matching alert rule';
    else if (/no detections matched/i.test(reason)) suffix = 'outside monitored zones';
    else suffix = reason || 'no alert triggered';
    return { state: 'detected', stateLabel: 'Detected', chips, ...sound, message: `Detected ${labelStr} - ${suffix}.`, ...motionData };
  }

  const fallback = String(payload.reason || payload.ai_error || 'waiting for frames');
  return {
    state: payload.state || 'idle',
    stateLabel: payload.state ? payload.state[0].toUpperCase() + payload.state.slice(1) : 'Idle',
    chips,
    ...sound,
    message: `Live AI: ${payload.state || 'waiting'} - ${fallback}`,
    ...motionData,
  };
}

// Sentence case for text shown in the Vision/Hearing lanes: only the first
// letter is capitalised, so multi-word detection labels read "Traffic light"
// rather than the Title Case "Traffic Light".
function sentenceCase(value) {
  const s = String(value || '').replace(/[-_]+/g, ' ').trim();
  return s ? s.charAt(0).toUpperCase() + s.slice(1) : '';
}

// Build one "detected item" row for a sense lane: label, a confidence meter,
// and the percent. Zero-confidence readings (label known but no score, e.g.
// from `detected_labels`) show the label alone with no meter.
function detectionRowHtml(label, confidence, { faint = false, alerted = false } = {}) {
  const conf = Number(confidence || 0);
  const hasPct = conf > 0;
  const pct = Math.round(conf * 100);
  const classes = 'sense-det' + (faint ? ' sense-det-faint' : '') + (alerted ? ' sense-det-alert' : '');
  const pctHtml = hasPct ? `<span class="sense-det-pct">${pct}%</span>` : '';
  const meterHtml = hasPct ? `<span class="sense-meter"><i style="width:${pct}%"></i></span>` : '';
  return `<div class="${classes}">`
    + `<span class="sense-det-label">${escapeHtml(sentenceCase(label))}</span>`
    + pctHtml + meterHtml
    + '</div>';
}

// Empty-lane placeholder. `tick` shows the affirmative green check and is used
// ONLY for a confirmed all-clear; every other empty state gets a neutral line.
function senseEmptyHtml(text, { tick = false } = {}) {
  const tickHtml = tick ? '<span class="sense-empty-tick">✓</span>' : '';
  return `<div class="sense-empty">${tickHtml}${escapeHtml(text)}</div>`;
}

function renderDetectionStatus(summary) {
  const objChips = summary.chips || [];
  const soundState = summary.soundState || { label: 'Listening', state: 'idle' };
  const soundChips = summary.soundChips || [];
  const alerted = summary.state === 'alerted';

  // Subtitle: summarise what is actively seen/heard, or fall back to the default description.
  if (liveEls.detectionSubtitle) {
    const objParts = objChips.slice(0, 3).map((c) =>
      c.confidence > 0 ? `${titleCase(c.label)} (${Math.round(c.confidence * 100)}%)` : titleCase(c.label)
    );
    const sndParts = soundChips.filter((c) => !c.isBelowThreshold).slice(0, 2).map((c) =>
      c.confidence > 0 ? `${titleCase(c.label)} (${Math.round(c.confidence * 100)}%)` : titleCase(c.label)
    );
    if (objParts.length || sndParts.length) {
      const parts = [];
      if (objParts.length) parts.push(`Seeing: ${objParts.join(', ')}`);
      if (sndParts.length) parts.push(`Hearing: ${sndParts.join(', ')}`);
      liveEls.detectionSubtitle.textContent = parts.join(' · ');
    } else {
      liveEls.detectionSubtitle.textContent = 'What the AI is currently seeing and hearing on the live feed.';
    }
  }

  // Monitor pill: a "live" affordance that dims when the status feed is down.
  if (liveEls.monitorPill) {
    const offline = summary.state === 'error';
    liveEls.monitorPill.classList.toggle('is-offline', offline);
    if (liveEls.monitorPillText) liveEls.monitorPillText.textContent = offline ? 'Paused' : 'Live';
  }

  // ── Vision lane ──────────────────────────────────────────────
  if (liveEls.visionLane) liveEls.visionLane.classList.toggle('sense-lane-alerted', alerted);
  if (liveEls.detectionState) {
    liveEls.detectionState.textContent = summary.stateLabel || 'Monitoring';
    liveEls.detectionState.className = 'sense-badge ' + (
      alerted ? 'sense-badge-alert' :
      summary.state === 'detected' ? 'sense-badge-detected' :
      summary.state === 'error' ? 'sense-badge-alert' :
      'sense-badge-idle'
    );
  }
  if (liveEls.visionBody) {
    if (objChips.length) {
      liveEls.visionBody.innerHTML = objChips.map((c) => detectionRowHtml(c.label, c.confidence, { alerted })).join('');
    } else if (summary.state === 'monitoring') {
      // Confirmed empty check - the only case that earns the affirmative "Clear".
      liveEls.visionBody.innerHTML = senseEmptyHtml('Clear', { tick: true });
    } else if (summary.state === 'waiting') {
      liveEls.visionBody.innerHTML = senseEmptyHtml('Waiting for first detection…');
    } else if (summary.state === 'skipped' || summary.state === 'error') {
      liveEls.visionBody.innerHTML = senseEmptyHtml('Detection unavailable');
    } else {
      // idle / all-cameras / no payload: don't assert anything about the frame.
      liveEls.visionBody.innerHTML = senseEmptyHtml('No detection data');
    }
  }

  // ── Hearing lane ─────────────────────────────────────────────
  // The inline all-cameras/error summaries carry no soundState, so the sound
  // status is genuinely unknown there - don't fall back to a "Listening" claim.
  const hasSound = !!summary.soundState;
  if (liveEls.soundState) {
    liveEls.soundState.textContent = hasSound ? soundState.label : '-';
    liveEls.soundState.className = 'sense-badge ' + (
      soundState.state === 'detected' ? 'sense-badge-heard' :
      soundState.state === 'disabled' ? 'sense-badge-off' :
      'sense-badge-idle'
    );
  }
  if (liveEls.hearingBody) {
    if (soundChips.length) {
      liveEls.hearingBody.innerHTML = soundChips.map((c) => detectionRowHtml(c.label, c.confidence, { faint: !!c.isBelowThreshold })).join('');
    } else if (!hasSound) {
      liveEls.hearingBody.innerHTML = senseEmptyHtml('Sound status unavailable');
    } else if (soundState.state === 'disabled') {
      liveEls.hearingBody.innerHTML = senseEmptyHtml('Sound detection disabled');
    } else {
      liveEls.hearingBody.innerHTML = senseEmptyHtml('Quiet', { tick: true });
    }
  }

  // Contextual status lines carry alert/diagnostic context only; each stays
  // hidden when it has nothing to say. Objects and sounds get their own line.
  if (liveEls.detectionStatus) {
    liveEls.detectionStatus.textContent = summary.message || '';
    liveEls.detectionStatus.hidden = !summary.message;
    liveEls.detectionStatus.classList.toggle('sense-lane-note-warn', alerted);
  }
  if (liveEls.soundStatus) {
    liveEls.soundStatus.textContent = summary.soundMessage || '';
    liveEls.soundStatus.hidden = !summary.soundMessage;
  }

  // ── Motion lane ─────────────────────────────────────────────
  // motion_confidence is the alert-gated level: it remains zero below the
  // frame gate so the bar's trigger reference still describes alertability.
  // motion_signal is the ungated changed-fraction / scale-fraction level, so
  // the diagnostic bar can show a real but sub-threshold movement. Older
  // payloads fall back to the gated value until the backend is updated.
  const motionConf = summary.motion_confidence != null ? summary.motion_confidence : null;
  const motionFraction = summary.motion_fraction != null ? summary.motion_fraction : null;
  const motionSignal = summary.motion_signal != null ? summary.motion_signal : motionConf;
  if (liveEls.motionBar) {
    const barPct = motionSignal != null ? Math.round(Math.min(1, motionSignal) * 100) : 0;
    liveEls.motionBar.style.width = barPct + '%';
    if (liveEls.motionValue) {
      liveEls.motionValue.textContent = (motionSignal != null ? Math.round(motionSignal * 100) : 0) + '%';
    }
  }
  // Trigger tick + caption: anchored to the camera's easiest motion zone
  // (lowest Sensitivity), so "fill past the tick" = a motion zone fires.
  if (liveEls.motionTriggerTick) {
    const showTrigger = !isAllCameraMode() && motionTriggerSensitivityPct > 0;
    liveEls.motionTriggerTick.hidden = !showTrigger;
    if (showTrigger) liveEls.motionTriggerTick.style.left = Math.min(99, motionTriggerSensitivityPct) + '%';
  }
  if (liveEls.motionCaption) {
    const parts = [];
    if (motionFraction != null) parts.push(`${Math.round(motionFraction * 100)}% of frame pixels`);
    if (!isAllCameraMode() && motionTriggerSensitivityPct > 0) {
      parts.push(`fires above ${motionTriggerSensitivityPct}% sensitivity`);
    } else if (!isAllCameraMode()) {
      parts.push('no motion zones configured');
    }
    liveEls.motionCaption.textContent = parts.join(' · ');
  }
  if (liveEls.motionState) {
    const motionActive = motionSignal != null && motionSignal > 0;
    liveEls.motionState.textContent = motionActive ? 'Active' : 'Waiting';
    liveEls.motionState.className = 'sense-badge ' + (
      motionActive ? 'sense-badge-detected' : 'sense-badge-idle'
    );
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
    // The grid has no selected-camera status request, but each tile still needs
    // runtime source metadata. Fetch status independently so Auto cameras do
    // not remain stuck on a configuration fallback.
    await Promise.all(cameras.map(async (camera) => {
      if (!camera?.id) return;
      try {
        const streamStatus = await api(`/api/status?camera_id=${encodeURIComponent(camera.id)}`);
        if (streamStatus?.fps) cameraRuntimeFps[camera.id] = streamStatus.fps;
      } catch {
        // A single offline camera should not prevent the other tiles updating.
      }
    }));
    renderCameraGrid();
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
    // The sound status endpoint now carries its own diagnostics/reason (heard,
    // below threshold, in cooldown), so the client no longer re-derives them.
    const [payload, soundStatus, streamStatus] = await Promise.all([
      api(`/api/live/detection-status?camera_id=${cameraId}`),
      api(`/api/sound/status?camera_id=${cameraId}`).catch(() => null),
      api(`/api/status?camera_id=${cameraId}`).catch(() => null),
    ]);
    if (streamStatus?.fps) {
      cameraRuntimeFps[selectedCamera.id] = streamStatus.fps;
      updateFrameHeader(selectedCamera);
    }
    ingestServerTrackDetections(payload);
    renderDetectionStatus(summarizeDetectionStatus(payload, soundStatus, soundEnabled));
  } catch (error) {
    // Skip UI updates if api() triggered a 401 redirect
    if (window.daygleAuth?.redirecting) return;
    renderDetectionStatus({
      state: 'error',
      stateLabel: 'Error',
      chips: [],
      message: `Live AI status unavailable: ${error.message}`,
    });
  }
}

// Lowest Sensitivity among a camera's enabled motion zones (mirrors the
// backend's zone_motion_min_confidence, which defaults to 0.45 per zone).
// The live lane is frame-wide, so this is the "easiest" trigger point.
function motionTriggerSensitivity(camera) {
  const zones = (camera && camera.detection && camera.detection.zones) || [];
  let min = null;
  for (const zone of zones) {
    if (zone.enabled === false || zone.monitor_motion === false) continue;
    const rule = (zone.object_rules || []).find((r) => (
      String(r.label || '').trim().toLowerCase() === 'motion' && r.enabled !== false
    ));
    const sens = rule != null ? Number(rule.min_confidence ?? 0.45) : 0.45;
    if (min == null || sens < min) min = sens;
  }
  return min;
}

function setSelectedCamera(cameraId) {
  selectedCamera = cameras.find((camera) => camera.id === cameraId) || cameras[0];
  if (!selectedCamera) return;
  const sens = motionTriggerSensitivity(selectedCamera);
  motionTriggerSensitivityPct = sens != null ? Math.round(sens * 100) : 0;
  rebuildConfiguredLabels();
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
  updateStreamOptions();
}

function updateStreamOptions() {
  if (!liveEls.liveStreamSelect) return;
  let recOption = liveEls.liveStreamSelect.querySelector('option[value="recording"]');
  if (recOption) {
    let hasRecPath = !!(selectedCamera?.recording_stream_path);
    recOption.hidden = !hasRecPath;
    // If recording was selected but camera has no recording stream, fall back to detection
    if (liveStreamSource === 'recording' && !hasRecPath) {
      liveStreamSource = 'detection';
      liveEls.liveStreamSelect.value = 'detection';
      refreshFrame();
    }
  }
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
  // The stream is gone, so any measured-FPS pulse is stale: hide the Live
  // indicator and drop the pulsing class until the next good status poll.
  if (liveEls.streamDetailFps) liveEls.streamDetailFps.classList.remove('fps-live');
  if (liveEls.streamDetailFpsLive) liveEls.streamDetailFpsLive.hidden = true;
  const streamLabel = liveStreamSource === 'recording' ? 'Recording stream' : '';
  liveEls.status.textContent = selectedCamera?.name
    ? `${selectedCamera.name} - ${streamLabel || 'Unable to load live footage'}. Retrying...`
    : `${streamLabel || 'Unable to load live footage'}. Retrying...`;
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
if (liveEls.liveStreamSelect) {
  const saved = localStorage.getItem(LIVE_STREAM_KEY);
  if (saved === 'recording') liveStreamSource = 'recording';
  liveEls.liveStreamSelect.value = liveStreamSource;
  liveEls.liveStreamSelect.addEventListener('change', () => {
    liveStreamSource = liveEls.liveStreamSelect.value;
    localStorage.setItem(LIVE_STREAM_KEY, liveStreamSource);
    refreshFrame();
    updateFrameHeader(selectedCamera);
  });
}
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

function updatePtzVisibility() {
  if (!ptzOverlay) return;
  const enabled = selectedCamera?.ptz?.enabled === true && !isAllCameraMode();
  if (liveEls.livePtzToggleGroup) liveEls.livePtzToggleGroup.hidden = !enabled;
  const toggled = liveEls.livePtzToggle ? liveEls.livePtzToggle.checked : true;
  ptzOverlay.hidden = !enabled || !toggled;
}

async function sendPtz(command) {
  if (!selectedCamera) return;
  try {
    await api(`/api/cameras/${encodeURIComponent(selectedCamera.id)}/ptz`, {
      method: 'POST',
      body: JSON.stringify({ command }),
    });
  } catch (err) {
    // Skip UI updates if api() triggered a 401 redirect
    if (window.daygleAuth?.redirecting) return;
    console.warn('PTZ command failed:', command, err.message);
    window.showToast?.(`PTZ error: ${err.message}`, true);
  }
}

// Visual-feedback helpers for the PTZ overlay: pressed/active CSS state on
// the pressed button plus the "Moving" status pill in the top-right corner.
function setPtzMoving(btn, isMoving) {
  if (btn) btn.setAttribute('data-moving', isMoving ? 'true' : 'false');
  const status = document.getElementById('ptzStatus');
  if (status) {
    status.setAttribute('data-visible', isMoving ? 'true' : 'false');
    status.textContent = isMoving
      ? `${clampStepDuration(selectedCamera?.ptz?.step_duration).toFixed(1)} s step`
      : '';
  }
  if (ptzOverlay) ptzOverlay.classList.toggle('ptz-overlay--active', isMoving);
}

const PTZ_STEP_DURATION_DEFAULT = 0.4;   // seconds - matches normalize_camera_ptz_settings default
const PTZ_STEP_DURATION_MIN = 0.1;
const PTZ_STEP_DURATION_MAX = 5.0;

function clampStepDuration(raw) {
  const n = Number.parseFloat(raw);
  if (!Number.isFinite(n)) return PTZ_STEP_DURATION_DEFAULT;
  return Math.min(Math.max(n, PTZ_STEP_DURATION_MIN), PTZ_STEP_DURATION_MAX);
}

let ptzHoldTimer = null;
let ptzActiveBtn = null;

function endPtzHold({ sendStop = true } = {}) {
  if (ptzHoldTimer !== null) {
    clearInterval(ptzHoldTimer);
    ptzHoldTimer = null;
  }
  const heldBtn = ptzActiveBtn;
  ptzActiveBtn = null;
  setPtzMoving(heldBtn, false);
  if (!heldBtn) return;
  const stopCmd = heldBtn.dataset.ptzStop;
  if (sendStop && stopCmd) sendPtz(stopCmd);
}

if (ptzOverlay) {
  // Global release handlers (capture phase) so we catch the release even if
  // the cursor leaves the overlay, the browser tab loses focus mid-hold,
  // or the touch is cancelled by the OS.
  const releaseHold = () => endPtzHold();
  document.addEventListener('mouseup', releaseHold, true);
  document.addEventListener('touchend', releaseHold, true);
  document.addEventListener('touchcancel', releaseHold, true);
  document.addEventListener('pointerup', releaseHold, true);
  window.addEventListener('blur', releaseHold);
  window.addEventListener('pointercancel', releaseHold);

  ptzOverlay.querySelectorAll('[data-ptz]').forEach((btn) => {
    const startCmd = btn.dataset.ptz;
    const stopCmd = btn.dataset.ptzStop;

    const startHold = (e) => {
      e.preventDefault();
      // Switching directions mid-hold: stop cleanly before starting the new
      // direction so the camera's SOAP Timeout window resets cleanly.
      if (ptzActiveBtn !== null) endPtzHold();

      // Instant one-shot: Stop/Home button has `data-ptz="stop"` and no
      // `data-ptz-stop` (and zooms without stop token still one-shot).
      if (!stopCmd || startCmd === 'stop') {
        sendPtz(startCmd);
        return;
      }

      ptzActiveBtn = btn;
      setPtzMoving(btn, true);

      // First ContinuousMove: the backend emits ONVIF <Timeout> at the
      // configured step duration, so the camera self-stops if our explicit
      // Stop call is dropped on the wire.
      sendPtz(startCmd);

      // Re-issue every ~70% of the step duration so the camera's self-stop
      // timeout never fires while the user is still holding the button.
      const stepDuration = clampStepDuration(selectedCamera?.ptz?.step_duration);
      const refreshMs = Math.max(50, Math.round(stepDuration * 700));
      ptzHoldTimer = setInterval(() => {
        if (ptzActiveBtn === btn) sendPtz(startCmd);
      }, refreshMs);
    };

    btn.addEventListener('mousedown', startHold);
    btn.addEventListener('touchstart', startHold, { passive: false });
    // Release is handled globally via the capture-phase listeners above.
  });
}

liveEls.livePtzToggle?.addEventListener('change', () => updatePtzVisibility());

async function init() {
  // nav.js kicks off the shared /api/auth/me at script load; awaiting
  // daygleAuthReady here means this page never issues its own duplicate
  // /api/auth/me on bootstrap.
  await window.daygleAuthReady;
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
  // configuredLabels is built per selected camera inside setSelectedCamera
  // (called from renderCameraOptions below) once the camera list is loaded.
  const payload = await api('/api/cameras');
  cameras = payload.cameras || [];
  updateEmptyState();
  renderCameraOptions();
  if (isZonesPage) bindZoneDrawing(); // bindZoneDrawing defined in zones.js
  syncViewMode();
  refreshTimer = setInterval(refreshFrame, snapshotRefreshMs);
  restartDetectionStatusTimer();
}

init().catch((error) => {
  // Skip UI updates if api() triggered a 401 redirect
  if (window.daygleAuth?.redirecting) return;
  liveEls.status.textContent = error.message;
});
window.addEventListener('beforeunload', () => {
  clearInterval(refreshTimer);
  clearInterval(detectionStatusTimer);
});
