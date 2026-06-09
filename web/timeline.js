const els = {
  cameraSelect: document.getElementById('timelineCameraSelect'),
  timelineDate: document.getElementById('timelineDate'),
  fromTime: document.getElementById('timelineFromTime'),
  toTime: document.getElementById('timelineToTime'),
  filterSelect: document.getElementById('timelineFilterSelect'),
  timelineLoadBtn: document.getElementById('timelineLoadBtn'),
  timelineStatus: document.getElementById('timelineStatus'),
  timelineSummary: document.getElementById('timelineSummary'),
  timelineLegend: document.getElementById('timelineLegend'),
  timelineHours: document.getElementById('timelineHours'),
  timelineGrid: document.getElementById('timelineGrid'),
  timelineRows: document.getElementById('timelineRows'),
  timelineRecordings: document.getElementById('timelineRecordings'),
  clipPlayer: document.getElementById('clipPlayer'),
  clipPlayerStatus: document.getElementById('clipPlayerStatus'),
  clipOverlay: document.getElementById('clipOverlay'),
  clipOverlayToggle: document.getElementById('clipOverlayToggle'),
  clipOverlayTrackToggle: document.getElementById('clipOverlayTrackToggle'),
  recordingDetails: document.getElementById('recordingDetails'),
  videoModal: document.getElementById('videoModal'),
  videoModalClose: document.getElementById('videoModalClose'),
  videoModalDownload: document.getElementById('videoModalDownload'),
};

const state = {
  auth: { user: null, csrfToken: null },
  payload: null,
  activeRecordingId: null,
  dateFormat: 'locale',
  timeFormat: '24h',
};

let configuredLabels = null;
let activeRecording = null;

const OVERLAY_TOGGLE_KEY = 'daygle.timeline.overlay.enabled';
const OVERLAY_TRACK_KEY = 'daygle.timeline.overlay.track.enabled';
let overlayEnabled = false;
let overlayTrackEnabled = false;
let overlayTrackIntervalMs = 300;
const OVERLAY_TRACK_MAX_WIDTH = 640;
const OVERLAY_TRACK_MAX_HEIGHT = 360;
const overlayTrackCanvas = document.createElement('canvas');
let overlayTrackLastRunMs = 0;
let overlayTrackInFlight = false;
let overlayTrackDetections = null;
let overlayTrackPrevDetections = null;
let overlayTrackPrevUpdateMs = 0;
let overlayTrackLastUpdateMs = 0;
const OVERLAY_TRACK_LERP_MS = 150;
let overlayRafId = null;
let overlayResizeObserver = null;

const DAY_SECONDS = 24 * 60 * 60;
const TIMELINE_ROW_HEIGHT = 42;

function parseTimeInput(value, fallback) {
  if (!value) return fallback;
  const parts = value.split(':');
  const h = parseInt(parts[0], 10) || 0;
  const m = parseInt(parts[1], 10) || 0;
  return h * 3600 + m * 60;
}

function getTimeRangeConfig() {
  const fromSeconds = parseTimeInput(els.fromTime.value, 0);
  const toRaw = parseTimeInput(els.toTime.value, DAY_SECONDS);
  const toSeconds = Math.min(toRaw <= fromSeconds ? fromSeconds + 3600 : toRaw, DAY_SECONDS);
  const totalSeconds = toSeconds - fromSeconds;
  const totalHours = totalSeconds / 3600;
  let tickIntervalSeconds;
  if (totalHours <= 1) tickIntervalSeconds = 900;
  else if (totalHours <= 2) tickIntervalSeconds = 1800;
  else if (totalHours <= 6) tickIntervalSeconds = 3600;
  else if (totalHours <= 12) tickIntervalSeconds = 7200;
  else tickIntervalSeconds = 10800;
  return { fromSeconds, toSeconds, totalSeconds, tickIntervalSeconds };
}

const SEGMENT_COLORS = [
  '#47d6ff',
  '#49e6a3',
  '#fbbf24',
  '#fb7185',
  '#38bdf8',
  '#f97316',
  '#a78bfa',
  '#22c55e',
  '#f43f5e',
  '#14b8a6',
];

const GENERIC_TIMELINE_LABELS = new Set(['motion', 'alert', 'human', 'object', 'none', 'off', 'continuous']);

function clamp(value, min, max) {
  return Math.min(Math.max(value, min), max);
}

function filterByConfiguredLabels(detections) {
  if (!configuredLabels) return detections;
  return detections.filter((d) => {
    const label = String(d.label || '').trim().toLowerCase();
    return configuredLabels.has(label) || (configuredLabels.has('motion') && label === 'motion');
  });
}

function detectionAnchorSeconds(recording) {
  const startedAt = Date.parse(recording?.started_at || '');
  const eventAt = Date.parse(recording?.event?.created_at || '');
  if (!Number.isFinite(startedAt) || !Number.isFinite(eventAt)) return null;
  const seconds = (eventAt - startedAt) / 1000;
  return Number.isFinite(seconds) ? Math.max(0, seconds) : null;
}

function shouldRenderOverlayForTime(recording, playerTimeSeconds) {
  const anchorSeconds = detectionAnchorSeconds(recording);
  if (anchorSeconds === null) return true;
  return playerTimeSeconds >= anchorSeconds;
}

function clearClipOverlay() {
  if (!els.clipOverlay) return;
  const context = els.clipOverlay.getContext('2d');
  if (!context) return;
  context.setTransform(1, 0, 0, 1, 0, 0);
  context.clearRect(0, 0, els.clipOverlay.width, els.clipOverlay.height);
}

function clearOverlayTrackDetections() {
  overlayTrackDetections = null;
  overlayTrackPrevDetections = null;
  overlayTrackPrevUpdateMs = 0;
  overlayTrackLastUpdateMs = 0;
  overlayTrackLastRunMs = 0;
}

function interpolateDetections(prev, cur, t) {
  if (!prev || !cur || t >= 1) return cur;
  return cur.map((curDet) => {
    const prevDet = prev.find((p) => p.label === curDet.label);
    if (!prevDet?.box) return curDet;
    const lerp = (a, b) => a + (b - a) * t;
    return {
      ...curDet,
      box: {
        x: lerp(prevDet.box.x, curDet.box.x),
        y: lerp(prevDet.box.y, curDet.box.y),
        width: lerp(prevDet.box.width, curDet.box.width),
        height: lerp(prevDet.box.height, curDet.box.height),
      },
    };
  });
}

function startOverlayRaf() {
  if (overlayRafId !== null) return;
  function loop() {
    if (!overlayTrackEnabled || !els.clipPlayer || els.clipPlayer.paused) {
      overlayRafId = null;
      return;
    }
    detectOverlayFrameDetections();
    drawClipOverlay();
    overlayRafId = requestAnimationFrame(loop);
  }
  overlayRafId = requestAnimationFrame(loop);
}

function stopOverlayRaf() {
  if (overlayRafId !== null) {
    cancelAnimationFrame(overlayRafId);
    overlayRafId = null;
  }
}

function normalizeDetectionBox(box, width, height) {
  const rawX = Number(box?.x ?? 0);
  const rawY = Number(box?.y ?? 0);
  const rawWidth = Number(box?.width ?? 0);
  const rawHeight = Number(box?.height ?? 0);
  if (!Number.isFinite(rawX) || !Number.isFinite(rawY) || !Number.isFinite(rawWidth) || !Number.isFinite(rawHeight)) return null;
  if (rawWidth <= 0 || rawHeight <= 0) return null;
  if (rawX <= 1 && rawY <= 1 && rawWidth <= 1 && rawHeight <= 1) {
    return { x: rawX, y: rawY, width: rawWidth, height: rawHeight };
  }
  if (width <= 0 || height <= 0) return null;
  return {
    x: Math.max(0, Math.min(1, rawX / width)),
    y: Math.max(0, Math.min(1, rawY / height)),
    width: Math.max(0, Math.min(1, rawWidth / width)),
    height: Math.max(0, Math.min(1, rawHeight / height)),
  };
}

async function detectOverlayFrameDetections() {
  if (!overlayTrackEnabled || !activeRecording || !els.clipPlayer) return;
  if (els.clipPlayer.readyState < 2 || els.clipPlayer.videoWidth <= 0 || els.clipPlayer.videoHeight <= 0) return;
  const now = performance.now();
  if (overlayTrackInFlight || now - overlayTrackLastRunMs < overlayTrackIntervalMs) return;
  overlayTrackLastRunMs = now;
  overlayTrackInFlight = true;
  try {
    const sourceWidth = Number(els.clipPlayer.videoWidth || 0);
    const sourceHeight = Number(els.clipPlayer.videoHeight || 0);
    if (sourceWidth <= 0 || sourceHeight <= 0) return;
    const scale = Math.min(1, OVERLAY_TRACK_MAX_WIDTH / sourceWidth, OVERLAY_TRACK_MAX_HEIGHT / sourceHeight);
    const frameWidth = Math.max(1, Math.round(sourceWidth * scale));
    const frameHeight = Math.max(1, Math.round(sourceHeight * scale));
    overlayTrackCanvas.width = frameWidth;
    overlayTrackCanvas.height = frameHeight;
    const context = overlayTrackCanvas.getContext('2d');
    if (!context) return;
    context.drawImage(els.clipPlayer, 0, 0, frameWidth, frameHeight);
    const blob = await new Promise((resolve) => {
      overlayTrackCanvas.toBlob((value) => resolve(value), 'image/jpeg', 0.8);
    });
    if (!blob) return;
    const payload = await api('/api/detect/frame', {
      method: 'POST',
      headers: { 'Content-Type': 'image/jpeg' },
      body: blob,
    });
    const detections = Array.isArray(payload?.detections) ? payload.detections : [];
    const newDetections = detections.map((detection) => {
      const normalizedBox = normalizeDetectionBox(detection?.box || {}, frameWidth, frameHeight);
      if (!normalizedBox) return null;
      return { ...detection, box: normalizedBox };
    }).filter(Boolean);
    overlayTrackPrevDetections = overlayTrackDetections;
    overlayTrackPrevUpdateMs = overlayTrackLastUpdateMs;
    overlayTrackLastUpdateMs = performance.now();
    overlayTrackDetections = newDetections;
  } catch (_error) {
    // Keep last successful overlay detections on transient failure.
  } finally {
    overlayTrackInFlight = false;
    if (!overlayRafId) drawClipOverlay();
  }
}

function resizeClipOverlay() {
  if (!els.clipOverlay || !els.clipPlayer) return;
  const width = Math.max(1, Math.round(els.clipPlayer.clientWidth));
  const height = Math.max(1, Math.round(els.clipPlayer.clientHeight));
  const dpr = window.devicePixelRatio || 1;
  const targetWidth = Math.max(1, Math.round(width * dpr));
  const targetHeight = Math.max(1, Math.round(height * dpr));
  if (els.clipOverlay.width !== targetWidth || els.clipOverlay.height !== targetHeight) {
    els.clipOverlay.width = targetWidth;
    els.clipOverlay.height = targetHeight;
  }
}

function drawClipOverlay() {
  if (!els.clipOverlay || !els.clipPlayer) return;
  resizeClipOverlay();
  const context = els.clipOverlay.getContext('2d');
  if (!context) return;
  const cssWidth = Math.max(1, els.clipPlayer.clientWidth);
  const cssHeight = Math.max(1, els.clipPlayer.clientHeight);
  const dpr = window.devicePixelRatio || 1;
  context.setTransform(1, 0, 0, 1, 0, 0);
  context.clearRect(0, 0, els.clipOverlay.width, els.clipOverlay.height);
  if (!overlayEnabled) return;
  context.setTransform(dpr, 0, 0, dpr, 0, 0);

  if (overlayTrackEnabled && !overlayRafId) {
    detectOverlayFrameDetections();
  }

  const allEventDetections = Array.isArray(activeRecording?.detections) ? activeRecording.detections : [];
  const hasSpecificEvent = allEventDetections.some((d) => !GENERIC_TIMELINE_LABELS.has(String(d.label || '').toLowerCase()));
  const eventDetections = filterByConfiguredLabels(
    hasSpecificEvent
      ? allEventDetections.filter((d) => !GENERIC_TIMELINE_LABELS.has(String(d.label || '').toLowerCase()))
      : allEventDetections
  );
  let rawTrackDetections = overlayTrackEnabled && Array.isArray(overlayTrackDetections) && overlayTrackDetections.length
    ? overlayTrackDetections : null;
  if (rawTrackDetections && overlayTrackPrevDetections && overlayTrackLastUpdateMs > 0) {
    const elapsed = performance.now() - overlayTrackLastUpdateMs;
    const t = Math.min(1, elapsed / OVERLAY_TRACK_LERP_MS);
    rawTrackDetections = interpolateDetections(overlayTrackPrevDetections, rawTrackDetections, t);
  }
  const detections = rawTrackDetections ? filterByConfiguredLabels(rawTrackDetections) : eventDetections;
  if (!detections.length) return;
  const playerTime = Number(els.clipPlayer.currentTime || 0);
  if (!overlayTrackEnabled && !shouldRenderOverlayForTime(activeRecording, playerTime)) return;

  const videoWidth = Math.max(1, Number(els.clipPlayer.videoWidth || cssWidth));
  const videoHeight = Math.max(1, Number(els.clipPlayer.videoHeight || cssHeight));
  const scale = Math.min(cssWidth / videoWidth, cssHeight / videoHeight);
  const renderWidth = videoWidth * scale;
  const renderHeight = videoHeight * scale;
  const offsetX = (cssWidth - renderWidth) / 2;
  const offsetY = (cssHeight - renderHeight) / 2;

  context.font = '12px Inter, ui-sans-serif, system-ui, sans-serif';
  context.textBaseline = 'middle';
  context.lineWidth = 2;

  detections.forEach((detection) => {
    const box = detection?.box || detection || {};
    const x = clamp(Number(box.x ?? 0), 0, 1);
    const y = clamp(Number(box.y ?? 0), 0, 1);
    const width = clamp(Number(box.width ?? 0), 0, 1);
    const height = clamp(Number(box.height ?? 0), 0, 1);
    if (width <= 0 || height <= 0) return;
    const drawX = offsetX + (x * renderWidth);
    const drawY = offsetY + (y * renderHeight);
    const drawWidth = width * renderWidth;
    const drawHeight = height * renderHeight;
    if (drawWidth < 2 || drawHeight < 2) return;

    context.strokeStyle = '#49e6a3';
    context.strokeRect(drawX, drawY, drawWidth, drawHeight);

    const confidence = Math.round(Number(detection.confidence || 0) * 100);
    const label = `${String(detection.label || 'object')} ${confidence}%`;
    const textWidth = context.measureText(label).width;
    const labelHeight = 20;
    const labelWidth = textWidth + 12;
    const labelY = drawY > labelHeight + 4 ? drawY - labelHeight - 4 : drawY + 4;
    context.fillStyle = 'rgba(7, 11, 19, 0.86)';
    context.fillRect(drawX, labelY, labelWidth, labelHeight);
    context.fillStyle = '#49e6a3';
    context.fillText(label, drawX + 6, labelY + (labelHeight / 2));
  });
}

async function api(path, options = {}) {
  const headers = { ...(options.headers || {}) };
  if (state.auth.csrfToken && ['POST', 'PUT', 'PATCH', 'DELETE'].includes((options.method || 'GET').toUpperCase())) {
    headers['X-CSRF-Token'] = state.auth.csrfToken;
  }
  const response = await fetch(path, { ...options, headers });
  if (response.status === 401) {
    window.location.href = '/login';
    throw new Error('Authentication required');
  }
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(payload.detail || `Request failed: ${response.status}`);
  return payload;
}

function titleCase(value) {
  return String(value || '')
    .split(/[-_\s]+/)
    .filter(Boolean)
    .map((token) => token.charAt(0).toUpperCase() + token.slice(1))
    .join(' ');
}

function formatDate(isoDateStr) {
  if (!isoDateStr) return '';
  const [year, month, day] = isoDateStr.split('-');
  if (!year || !month || !day) return isoDateStr;
  switch (state.dateFormat) {
    case 'iso': return `${year}-${month}-${day}`;
    case 'us': return `${month}/${day}/${year}`;
    case 'au': return `${day}/${month}/${year}`;
    default: return new Date(`${isoDateStr}T12:00:00`).toLocaleDateString();
  }
}

function formatTime(totalMinutes) {
  const h = Math.floor(totalMinutes / 60) % 24;
  const m = totalMinutes % 60;
  if (state.timeFormat === '12h') {
    const period = h < 12 ? 'am' : 'pm';
    const h12 = h % 12 || 12;
    return `${h12}:${String(m).padStart(2, '0')} ${period}`;
  }
  return `${String(h).padStart(2, '0')}:${String(m).padStart(2, '0')}`;
}

function formatDateTime(value) {
  if (!value) return 'Unknown';
  const date = new Date(value);
  const isoDate = date.toISOString().slice(0, 10);
  const datePart = formatDate(isoDate);
  const h = date.getHours();
  const m = date.getMinutes();
  const timePart = formatTime(h * 60 + m);
  return `${datePart} ${timePart}`;
}

function formatClock(seconds) {
  const totalMins = Math.floor(Math.max(0, seconds) / 60);
  return formatTime(totalMins);
}

function formatDuration(seconds) {
  const totalSeconds = Math.max(0, Math.round(Number(seconds || 0)));
  const hours = Math.floor(totalSeconds / 3600);
  const minutes = Math.floor((totalSeconds % 3600) / 60);
  const remainder = totalSeconds % 60;
  if (hours) return `${hours}h ${minutes}m ${remainder}s`;
  if (minutes) return `${minutes}m ${remainder}s`;
  return `${remainder}s`;
}

function recordingTriggerType(recording) {
  return String(recording.trigger_type || 'motion').trim().toLowerCase() || 'motion';
}

function recordingTriggerLabel(recording) {
  return String(recording.trigger_label || '').trim().toLowerCase() || null;
}

function recordingDetectionLabels(recording) {
  const labels = new Set((recording.detections || [])
    .filter((d) => {
      const label = String(d.label || '').trim().toLowerCase();
      if (!label) return false;
      if (!configuredLabels) return true;
      return configuredLabels.has(label) && Number(d.confidence || 0) >= (configuredLabels.get(label) ?? 0);
    })
    .map((d) => String(d.label || '').trim().toLowerCase()));
  const triggerLabel = recordingTriggerLabel(recording);
  if (triggerLabel && (!configuredLabels || configuredLabels.has(triggerLabel))) labels.add(triggerLabel);

  const uniqueLabels = Array.from(labels);
  const specificLabels = uniqueLabels.filter((label) => !GENERIC_TIMELINE_LABELS.has(label));
  return specificLabels.length ? specificLabels : uniqueLabels;
}

function recordingTypeLabel(recording) {
  const triggerType = recordingTriggerType(recording);
  const triggerLabel = recordingTriggerLabel(recording);
  const detectionLabels = recordingDetectionLabels(recording);

  if (triggerType === 'motion' || triggerType === 'alert' || triggerType === 'human') {
    // Prefer concrete object labels for timeline chips/segments, fall back to generic motion.
    if (triggerLabel && !GENERIC_TIMELINE_LABELS.has(triggerLabel)) return triggerLabel;
    const firstSpecificDetection = detectionLabels.find((label) => !GENERIC_TIMELINE_LABELS.has(label));
    if (firstSpecificDetection) return firstSpecificDetection;
    return 'motion';
  }
  if (triggerType === 'continuous' || triggerType === 'none' || triggerType === 'off') {
    return triggerType;
  }
  if (triggerLabel && !GENERIC_TIMELINE_LABELS.has(triggerLabel)) return triggerLabel;
  return triggerLabel || triggerType;
}

function recordingColorKey(recording) {
  return recordingTypeLabel(recording).toLowerCase();
}

function recordingTriggerSummary(recording) {
  const triggerType = recordingTriggerType(recording);
  const triggerLabel = recordingTriggerLabel(recording);
  const typeLabel = recordingTypeLabel(recording);
  if (!triggerLabel || triggerLabel === triggerType || triggerLabel === typeLabel) {
    return typeLabel;
  }
  if (triggerType === 'human' || triggerType === 'alert' || triggerType === 'motion') {
    return `${typeLabel} · motion`;
  }
  return `${typeLabel} · detected ${triggerLabel}`;
}

function recordingFilterTokens(recording) {
  const tokens = new Set([recordingTypeLabel(recording).toLowerCase()]);
  const triggerType = recordingTriggerType(recording);
  if (triggerType) tokens.add(triggerType);
  const triggerLabel = recordingTriggerLabel(recording);
  if (triggerLabel) tokens.add(triggerLabel);
  recordingDetectionLabels(recording).forEach((label) => tokens.add(label));
  return tokens;
}

function matchesRecordingFilter(recording, filterValue) {
  const normalized = String(filterValue || '').trim().toLowerCase();
  if (!normalized) return true;
  if (normalized === 'motion') {
    const triggerType = recordingTriggerType(recording);
    return !['continuous', 'off', 'none'].includes(triggerType);
  }
  return recordingFilterTokens(recording).has(normalized);
}

function cameraLabel(recording) {
  const metadata = recording?.event?.metadata || {};
  return metadata.camera_name || recording.camera_id || recording.source || 'unknown';
}

function colorForKey(key) {
  const normalized = String(key || 'motion').trim().toLowerCase() || 'motion';
  let hash = 0;
  for (let index = 0; index < normalized.length; index += 1) {
    hash = ((hash << 5) - hash) + normalized.charCodeAt(index);
    hash |= 0;
  }
  return SEGMENT_COLORS[Math.abs(hash) % SEGMENT_COLORS.length];
}

function timelineParams(overrides = {}) {
  const cameraId = overrides.cameraId || els.cameraSelect.value || '';
  const day = overrides.day || els.timelineDate.value || new Date(Date.now() - new Date().getTimezoneOffset() * 60000).toISOString().slice(0, 10);
  return { cameraId, day };
}

function replaceUrl(recordingId = state.activeRecordingId) {
  const params = new URLSearchParams();
  const { cameraId, day } = timelineParams();
  const filter = els.filterSelect.value || '';
  const fromTime = els.fromTime.value || '';
  const toTime = els.toTime.value || '';
  if (cameraId) params.set('camera_id', cameraId);
  if (day) params.set('day', day);
  if (fromTime && fromTime !== '00:00') params.set('from_time', fromTime);
  if (toTime && toTime !== '23:59') params.set('to_time', toTime);
  if (filter) params.set('filter', filter);
  if (recordingId) params.set('recording_id', String(recordingId));
  const query = params.toString();
  window.history.replaceState({}, '', `${window.location.pathname}${query ? `?${query}` : ''}`);
}

function populateControls(payload) {
  const selectedCameraId = payload.camera?.id || '';
  const selectedDay = payload.day || new Date(Date.now() - new Date().getTimezoneOffset() * 60000).toISOString().slice(0, 10);
  els.cameraSelect.innerHTML = payload.cameras.map((camera) => (
    `<option value="${escapeHtml(camera.id)}" ${camera.id === selectedCameraId ? 'selected' : ''}>${escapeHtml(camera.name)}</option>`
  )).join('');
  els.timelineDate.value = selectedDay;
}

function populateFilterOptions(recordings) {
  const currentFilter = els.filterSelect.value || new URLSearchParams(window.location.search).get('filter') || '';
  const counts = {};
  recordings.forEach((recording) => {
    const labels = new Set([recordingTypeLabel(recording).toLowerCase()]);
    recordingDetectionLabels(recording).forEach((label) => labels.add(label));
    labels.forEach((label) => { counts[label] = (counts[label] || 0) + 1; });
  });

  const options = [{ value: '', label: `All recordings${recordings.length ? ` (${recordings.length})` : ''}` }];
  const seen = new Set(['']);
  const addOption = (value) => {
    const normalized = String(value || '').trim().toLowerCase();
    if (!normalized || seen.has(normalized)) return;
    seen.add(normalized);
    const count = counts[normalized];
    options.push({ value: normalized, label: count ? `${titleCase(normalized)} (${count})` : titleCase(normalized) });
  };

  recordings.forEach((recording) => {
    addOption(recordingTypeLabel(recording));
    recordingDetectionLabels(recording).forEach(addOption);
  });
  if (recordings.length) addOption('motion');

  const ordered = [options[0], ...options.slice(1).sort((left, right) => {
    if (left.value === 'motion') return -1;
    if (right.value === 'motion') return 1;
    return left.label.localeCompare(right.label);
  })];
  els.filterSelect.innerHTML = ordered.map((option) => (
    `<option value="${escapeHtml(option.value)}">${escapeHtml(option.label)}</option>`
  )).join('');

  const availableValues = new Set(ordered.map((option) => option.value));
  els.filterSelect.value = availableValues.has(currentFilter) ? currentFilter : '';
}

function filteredRecordings() {
  const recordings = state.payload?.recordings || [];
  const { fromSeconds, toSeconds } = getTimeRangeConfig();
  const filterValue = els.filterSelect.value;
  return recordings.filter((recording) => {
    if (!matchesRecordingFilter(recording, filterValue)) return false;
    const start = Number(recording.timeline_start_seconds || 0);
    const end = Number(recording.timeline_end_seconds || start + 1);
    return start < toSeconds && end > fromSeconds;
  });
}

function renderSummary(payload, totalRecordingCount) {
  const recordings = payload.recordings || [];
  const totalSeconds = recordings.reduce((sum, recording) => sum + Number(recording.timeline_duration_seconds || recording.duration_seconds || 0), 0);
  const uniqueTriggers = new Set(recordings.map((recording) => recordingTypeLabel(recording).toLowerCase()));
  const clipLabel = totalRecordingCount > recordings.length ? `${recordings.length} of ${totalRecordingCount}` : `${recordings.length}`;
  els.timelineSummary.innerHTML = `
    <div><span>Camera</span><strong>${escapeHtml(payload.camera?.name || payload.camera?.id || 'Unknown')}</strong></div>
    <div><span>Day</span><strong>${escapeHtml(formatDate(payload.day || ''))}</strong></div>
    <div><span>Clips</span><strong>${escapeHtml(clipLabel)}</strong></div>
    <div><span>Coverage</span><strong>${escapeHtml(formatDuration(totalSeconds))}</strong></div>
    <div class="wide"><span>Triggers</span><strong>${recordings.length ? escapeHtml(Array.from(uniqueTriggers).join(', ')) : 'none'}</strong></div>
  `;
}

function renderLegend(recordings) {
  const unique = [];
  const seen = new Set();
  recordings.forEach((recording) => {
    const key = recordingColorKey(recording);
    if (seen.has(key)) return;
    seen.add(key);
    unique.push({ key, label: recordingTypeLabel(recording), color: colorForKey(key) });
  });
  if (!unique.length) {
    els.timelineLegend.innerHTML = '<p class="muted">No recordings match this filter for the selected day.</p>';
    return;
  }
  els.timelineLegend.innerHTML = unique.map((item) => `
    <span class="timeline-legend-chip">
      <span class="timeline-legend-swatch" style="background:${item.color}"></span>
      <span>${escapeHtml(item.label)}</span>
    </span>
  `).join('');
}

function buildTimelineLayout(recordings, preEventSeconds = 0) {
  const rowEnds = [];
  return recordings.map((recording) => {
    const start = Number(recording.timeline_start_seconds || 0);
    const end = Number(recording.timeline_end_seconds || start + 1);
    let rowIndex = rowEnds.findIndex((rowEnd) => rowEnd <= start + preEventSeconds);
    if (rowIndex === -1) {
      rowIndex = rowEnds.length;
      rowEnds.push(end);
    } else {
      rowEnds[rowIndex] = end;
    }
    return { ...recording, rowIndex };
  });
}

function renderTimeline(payload) {
  const { fromSeconds, toSeconds, totalSeconds, tickIntervalSeconds } = getTimeRangeConfig();

  // Clip recordings to the visible window [fromSeconds, toSeconds], rebasing positions to fromSeconds
  const windowRecordings = (payload.recordings || [])
    .filter((r) => {
      const start = Number(r.timeline_start_seconds || 0);
      const end = Number(r.timeline_end_seconds || start + 1);
      return start < toSeconds && end > fromSeconds;
    })
    .map((r) => {
      const rawStart = Number(r.timeline_start_seconds || 0);
      const rawEnd = Number(r.timeline_end_seconds || rawStart + 1);
      const visStart = Math.max(rawStart, fromSeconds) - fromSeconds;
      const visEnd = Math.min(rawEnd, toSeconds) - fromSeconds;
      return {
        ...r,
        _orig_start_seconds: rawStart,
        timeline_start_seconds: visStart,
        timeline_end_seconds: visEnd,
        timeline_duration_seconds: Math.max(1, visEnd - visStart),
      };
    });

  const recordings = buildTimelineLayout(windowRecordings, Number(payload.pre_event_seconds || 0));
  const rowCount = Math.max(1, recordings.reduce((max, recording) => Math.max(max, recording.rowIndex + 1), 0));

  const ticks = [];
  for (let s = fromSeconds; s <= toSeconds; s += tickIntervalSeconds) ticks.push(s);
  if (ticks[ticks.length - 1] < toSeconds) ticks.push(toSeconds);

  const tickPos = (s) => ((s - fromSeconds) / totalSeconds) * 100;

  els.timelineHours.innerHTML = ticks.map((s) => (
    `<span class="timeline-hour major" style="left:${tickPos(s)}%">${formatClock(Math.min(s, DAY_SECONDS - 1))}</span>`
  )).join('');
  els.timelineGrid.innerHTML = ticks.map((s) => `
    <span class="timeline-grid-line" style="left:${tickPos(s)}%"></span>
  `).join('');
  els.timelineRows.style.height = `${Math.max(96, rowCount * TIMELINE_ROW_HEIGHT)}px`;

  if (!recordings.length) {
    els.timelineRows.innerHTML = '<div class="empty timeline-empty">No recordings match the selected filter for this camera and day.</div>';
    return;
  }

  els.timelineRows.innerHTML = recordings.map((recording) => {
    const visStart = Number(recording.timeline_start_seconds || 0);
    const origStart = Number(recording._orig_start_seconds ?? visStart + fromSeconds);
    const duration = Math.max(1, Number(recording.timeline_duration_seconds || 1));
    const left = (visStart / totalSeconds) * 100;
    const width = Math.max((duration / totalSeconds) * 100, 0.1);
    const color = colorForKey(recordingColorKey(recording));
    const activeClass = Number(recording.id) === Number(state.activeRecordingId) ? ' active' : '';
    const compactClass = width < 0.7 ? ' compact' : '';
    const tinyClass = width < 0.25 ? ' tiny' : '';
    return `
      <button
        class="timeline-segment${activeClass}${compactClass}${tinyClass}"
        type="button"
        data-recording-id="${recording.id}"
        title="${escapeHtml(`${recordingTriggerSummary(recording)} · ${formatClock(origStart)} · ${formatDuration(recording.duration_seconds)}`)}"
        style="left:${left}%;width:${width}%;top:${recording.rowIndex * TIMELINE_ROW_HEIGHT + 8}px;--segment-color:${color};"
      >
        <span class="timeline-segment-label">${escapeHtml(recordingTypeLabel(recording))}</span>
        <span class="timeline-segment-time">${escapeHtml(formatClock(origStart))}</span>
      </button>
    `;
  }).join('');
}

function renderRecordingList(recordings) {
  if (!recordings.length) {
    els.timelineRecordings.innerHTML = '';
    return;
  }
  els.timelineRecordings.innerHTML = recordings.map((recording) => {
    const activeClass = Number(recording.id) === Number(state.activeRecordingId) ? ' active' : '';
    const color = colorForKey(recordingColorKey(recording));
    const label = titleCase(recordingTypeLabel(recording));
    const start = formatClock(recording.timeline_start_seconds || 0);
    const end = formatClock(recording.timeline_end_seconds || 0);
    const duration = formatDuration(recording.duration_seconds);
    const camera = escapeHtml(cameraLabel(recording));
    return `
      <button class="timeline-recording-item${activeClass}" type="button" data-recording-id="${recording.id}">
        <span class="timeline-recording-color" style="background:${color}"></span>
        <span class="timeline-recording-main">
          <strong>${escapeHtml(label)}</strong>
          <span>${escapeHtml(start)} - ${escapeHtml(end)}</span>
        </span>
        <span class="timeline-recording-meta">
          <span>${escapeHtml(duration)}</span>
          <span>#${recording.id} · ${camera}</span>
        </span>
      </button>
    `;
  }).join('');
}

function renderRecordingDetails(recording) {
  const seen = new Map();
  for (const d of (recording.detections || [])) {
    const label = String(d.label || '').trim().toLowerCase();
    if (!label) continue;
    const conf = Number(d.confidence || 0);
    if (configuredLabels && (!configuredLabels.has(label) || conf < (configuredLabels.get(label) ?? 0))) continue;
    if (!seen.has(label) || conf > seen.get(label)) seen.set(label, conf);
  }
  const detectionBadges = seen.size
    ? Array.from(seen.entries())
        .sort((a, b) => b[1] - a[1])
        .map(([label, conf]) => `<span class="detection">${escapeHtml(titleCase(label))} (${Math.round(conf * 100)}%)</span>`)
        .join(' ')
    : 'none';
  els.recordingDetails.innerHTML = `
    <div><span>Recording</span><strong><a href="/recordings?recording_id=${recording.id}" class="timeline-recording-link">#${recording.id} ↗</a></strong></div>
    <div><span>Camera</span><strong>${escapeHtml(cameraLabel(recording))}</strong></div>
    <div><span>Trigger</span><strong>${escapeHtml(titleCase(recordingTriggerSummary(recording)))}</strong></div>
    <div><span>Started</span><strong>${escapeHtml(formatDateTime(recording.started_at))}</strong></div>
    <div><span>Duration</span><strong>${escapeHtml(formatDuration(recording.duration_seconds))}</strong></div>
    <div class="wide"><span>Detections</span><strong>${detectionBadges}</strong></div>
  `;
}

function highlightActiveRecording() {
  document.querySelectorAll('[data-recording-id]').forEach((node) => {
    const isActive = Number(node.dataset.recordingId) === Number(state.activeRecordingId);
    node.classList.toggle('active', isActive);
  });
}

function openVideoModal() {
  els.videoModal.hidden = false;
  document.body.style.overflow = 'hidden';
  els.videoModalClose.focus();
}

function closeVideoModal() {
  els.videoModal.hidden = true;
  document.body.style.overflow = '';
  els.clipPlayer.pause();
  stopOverlayRaf();
  els.clipPlayer.removeAttribute('src');
  els.clipPlayer.load();
  els.videoModalDownload.hidden = true;
  els.videoModalDownload.removeAttribute('href');
  clearClipOverlay();
  clearOverlayTrackDetections();
  activeRecording = null;
}

async function playRecording(recordingId, updateHistory = true) {
  const recording = await api(`/api/recordings/${recordingId}`);
  activeRecording = recording;
  state.activeRecordingId = Number(recording.id);
  clearOverlayTrackDetections();
  renderRecordingDetails(recording);
  highlightActiveRecording();
  if (updateHistory) replaceUrl(state.activeRecordingId);

  openVideoModal();

  if (recording.media_ready === false) {
    els.clipPlayer.pause();
    els.clipPlayer.removeAttribute('src');
    els.clipPlayer.load();
    clearClipOverlay();
    els.clipPlayerStatus.textContent = `Recording #${recording.id} is still being prepared.`;
    return;
  }

  els.videoModalDownload.href = `/api/recordings/${recording.id}/download`;
  els.videoModalDownload.hidden = false;
  els.clipPlayer.pause();
  els.clipPlayer.src = `/api/recordings/${recording.id}/stream?t=${Date.now()}`;
  drawClipOverlay();
  els.clipPlayerStatus.textContent = `Loading recording #${recording.id}...`;
  try {
    els.clipPlayer.load();
    await els.clipPlayer.play();
    els.clipPlayerStatus.textContent = `Playing recording #${recording.id}.`;
  } catch (error) {
    if (['AbortError', 'NotAllowedError'].includes(error?.name)) {
      els.clipPlayerStatus.textContent = `Recording #${recording.id} loaded. Press play to start.`;
      return;
    }
    els.clipPlayerStatus.textContent = `Unable to play recording #${recording.id}: ${error?.message || 'media playback failed'}.`;
  }
}

function clearPlayback(updateHistory = true) {
  state.activeRecordingId = null;
  closeVideoModal();
  els.clipPlayerStatus.textContent = '';
  els.recordingDetails.innerHTML = '';
  highlightActiveRecording();
  if (updateHistory) replaceUrl(null);
}

async function renderFilteredTimeline({ preserveSelection = true } = {}) {
  const allRecordings = state.payload?.recordings || [];
  const recordings = filteredRecordings();
  const viewPayload = { ...(state.payload || {}), recordings };
  renderSummary(viewPayload, allRecordings.length);
  renderLegend(recordings);
  renderTimeline(viewPayload);
  renderRecordingList(recordings);

  if (!recordings.length) {
    const formattedDay = formatDate(state.payload.day);
    els.timelineStatus.textContent = allRecordings.length
      ? `No recordings match the selected filter for ${state.payload.camera.name} on ${formattedDay}.`
      : `No recordings found for ${state.payload.camera.name} on ${formattedDay}.`;
    clearPlayback(false);
    replaceUrl(null);
    return;
  }

  const filterLabel = els.filterSelect.value ? ` matching ${titleCase(els.filterSelect.value)}` : '';
  const { fromSeconds, toSeconds } = getTimeRangeConfig();
  const timeRangeLabel = (fromSeconds > 0 || toSeconds < DAY_SECONDS)
    ? ` from ${formatTime(fromSeconds / 60)} to ${formatTime(toSeconds / 60)}`
    : '';
  els.timelineStatus.textContent = `${recordings.length} clip${recordings.length === 1 ? '' : 's'}${filterLabel}${timeRangeLabel} for ${state.payload.camera.name} on ${formatDate(state.payload.day)}.`;

  const querySelection = Number(new URLSearchParams(window.location.search).get('recording_id')) || null;
  const requestedSelection = preserveSelection ? (state.activeRecordingId || querySelection) : null;
  const selectedRecording = recordings.find((recording) => Number(recording.id) === Number(requestedSelection));
  if (selectedRecording) {
    await playRecording(selectedRecording.id, false);
  } else {
    clearPlayback(false);
  }
  replaceUrl(state.activeRecordingId);
}

async function loadTimeline({ preserveSelection = true } = {}) {
  const { cameraId, day } = timelineParams();
  els.timelineStatus.textContent = 'Loading timeline…';
  const timezoneOffsetMinutes = new Date().getTimezoneOffset();
  const payload = await api(
    `/api/recordings/timeline?camera_id=${encodeURIComponent(cameraId)}&day=${encodeURIComponent(day)}&tz_offset_minutes=${encodeURIComponent(timezoneOffsetMinutes)}`,
  );
  state.payload = payload;
  populateControls(payload);
  populateFilterOptions(payload.recordings || []);
  await renderFilteredTimeline({ preserveSelection });
}

async function loadAuth() {
  const authInfo = await api('/api/auth/me');
  state.auth = { user: authInfo.user, csrfToken: authInfo.csrf_token };
  state.dateFormat = authInfo.user?.date_format || 'locale';
  state.timeFormat = authInfo.user?.time_format || '24h';
}

async function loadConfiguredLabels() {
  try {
    const [settings, alertData] = await Promise.all([api('/api/settings/system'), api('/api/settings/alerts')]);
    const labels = new Map([['motion', 0.45]]);
    const setMin = (label, conf) => {
      if (!label) return;
      if (!labels.has(label) || conf < labels.get(label)) labels.set(label, conf);
    };
    for (const rule of (alertData?.rules || [])) {
      if (rule.enabled !== false) {
        const label = String(rule.label || rule.object || '').trim().toLowerCase();
        setMin(label, Number(rule.min_confidence ?? 0.5));
      }
    }
    for (const camera of (settings?.cameras || [])) {
      for (const zone of (camera?.detection?.zones || [])) {
        for (const rule of (zone?.object_rules || [])) {
          if (rule.enabled !== false) {
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

els.timelineLoadBtn.addEventListener('click', () => {
  loadTimeline({ preserveSelection: false }).catch((error) => {
    els.timelineStatus.textContent = error.message;
  });
});

els.cameraSelect.addEventListener('change', () => {
  loadTimeline({ preserveSelection: false }).catch((error) => {
    els.timelineStatus.textContent = error.message;
  });
});

els.timelineDate.addEventListener('change', () => {
  loadTimeline({ preserveSelection: false }).catch((error) => {
    els.timelineStatus.textContent = error.message;
  });
});

els.filterSelect.addEventListener('change', () => {
  renderFilteredTimeline({ preserveSelection: true }).catch((error) => {
    els.timelineStatus.textContent = error.message;
  });
});

els.fromTime.addEventListener('change', () => {
  renderFilteredTimeline({ preserveSelection: true }).catch((error) => {
    els.timelineStatus.textContent = error.message;
  });
});

els.toTime.addEventListener('change', () => {
  renderFilteredTimeline({ preserveSelection: true }).catch((error) => {
    els.timelineStatus.textContent = error.message;
  });
});

els.timelineRows.addEventListener('click', (event) => {
  const button = event.target.closest('[data-recording-id]');
  if (!button) return;
  playRecording(button.dataset.recordingId).catch((error) => {
    els.clipPlayerStatus.textContent = error.message;
  });
});

els.timelineRecordings.addEventListener('click', (event) => {
  const button = event.target.closest('[data-recording-id]');
  if (!button) return;
  playRecording(button.dataset.recordingId).catch((error) => {
    els.clipPlayerStatus.textContent = error.message;
  });
});

els.videoModalClose.addEventListener('click', () => clearPlayback());

els.videoModal.addEventListener('click', (event) => {
  if (event.target === els.videoModal || event.target.classList.contains('video-modal-backdrop')) clearPlayback();
});

document.addEventListener('keydown', (event) => {
  if (event.key === 'Escape' && !els.videoModal.hidden) clearPlayback();
});

els.clipPlayer.addEventListener('error', () => {
  const error = els.clipPlayer.error;
  const messages = {
    1: 'Playback was aborted.',
    2: 'The recording could not be downloaded.',
    3: 'The recording could not be decoded by this browser.',
    4: 'The recording format is not supported by this browser.',
  };
  clearClipOverlay();
  els.clipPlayerStatus.textContent = messages[error?.code] || 'Unable to play this recording.';
});

['loadedmetadata', 'loadeddata', 'pause', 'seeked', 'timeupdate'].forEach((eventName) => {
  els.clipPlayer.addEventListener(eventName, drawClipOverlay);
});

els.clipPlayer.addEventListener('play', () => {
  if (overlayTrackEnabled) startOverlayRaf();
  drawClipOverlay();
});

els.clipPlayer.addEventListener('pause', () => {
  stopOverlayRaf();
  drawClipOverlay();
});

window.addEventListener('resize', drawClipOverlay);

if ('ResizeObserver' in window && els.clipPlayer) {
  overlayResizeObserver = new ResizeObserver(drawClipOverlay);
  overlayResizeObserver.observe(els.clipPlayer);
}

if (els.clipOverlayToggle) {
  const savedValue = localStorage.getItem(OVERLAY_TOGGLE_KEY);
  overlayEnabled = savedValue === '1';
  els.clipOverlayToggle.checked = overlayEnabled;
  els.clipOverlayToggle.addEventListener('change', () => {
    overlayEnabled = Boolean(els.clipOverlayToggle.checked);
    localStorage.setItem(OVERLAY_TOGGLE_KEY, overlayEnabled ? '1' : '0');
    drawClipOverlay();
  });
}

if (els.clipOverlayTrackToggle) {
  const savedTrackValue = localStorage.getItem(OVERLAY_TRACK_KEY);
  overlayTrackEnabled = savedTrackValue === '1';
  els.clipOverlayTrackToggle.checked = overlayTrackEnabled;
  els.clipOverlayTrackToggle.addEventListener('change', () => {
    overlayTrackEnabled = Boolean(els.clipOverlayTrackToggle.checked);
    localStorage.setItem(OVERLAY_TRACK_KEY, overlayTrackEnabled ? '1' : '0');
    clearOverlayTrackDetections();
    if (overlayTrackEnabled && els.clipPlayer && !els.clipPlayer.paused) {
      startOverlayRaf();
    } else {
      stopOverlayRaf();
    }
    drawClipOverlay();
  });
}

loadAuth().then(async () => {
  const params = new URLSearchParams(window.location.search);
  const queryDay = params.get('day');
  const queryCameraId = params.get('camera_id');
  const queryFilter = params.get('filter');
  const queryFromTime = params.get('from_time');
  const queryToTime = params.get('to_time');
  els.timelineDate.value = queryDay || new Date(Date.now() - new Date().getTimezoneOffset() * 60000).toISOString().slice(0, 10);
  if (queryCameraId) els.cameraSelect.innerHTML = `<option value="${escapeHtml(queryCameraId)}" selected>${escapeHtml(queryCameraId)}</option>`;
  if (queryFilter) els.filterSelect.innerHTML = `<option value="${escapeHtml(queryFilter)}" selected>${escapeHtml(titleCase(queryFilter))}</option>`;
  if (queryFromTime) els.fromTime.value = queryFromTime;
  if (queryToTime) els.toTime.value = queryToTime;
  await loadConfiguredLabels();
  await loadTimeline({ preserveSelection: true });
}).catch((error) => {
  els.timelineStatus.textContent = error.message;
  els.clipPlayerStatus.textContent = error.message;
});
