const els = {
  recordings: document.getElementById('recordings'),
  recordingFilter: document.getElementById('recordingFilter'),
  recordingSearchBtn: document.getElementById('recordingSearchBtn'),
  recordingClearBtn: document.getElementById('recordingClearBtn'),
  clipPlayer: document.getElementById('clipPlayer'),
  clipPlayerStatus: document.getElementById('clipPlayerStatus'),
  recordingDetails: document.getElementById('recordingDetails'),
  deleteAllRecordingsBtn: document.getElementById('deleteAllRecordingsBtn'),
  clipOverlay: document.getElementById('clipOverlay'),
  clipOverlayToggle: document.getElementById('clipOverlayToggle'),
  clipOverlayOffset: document.getElementById('clipOverlayOffset'),
};

let authState = { user: null, csrfToken: null };
let recordingRefreshTimer = null;
let activeRecording = null;
let overlayResizeObserver = null;
const OVERLAY_TOGGLE_KEY = 'daygle.recordings.overlay.enabled';
const OVERLAY_OFFSET_KEY = 'daygle.recordings.overlay.offset.seconds';
let overlayEnabled = true;
let overlayOffsetSeconds = 0;
const EVENT_OVERLAY_WINDOW_SECONDS = 2.0;
const GENERIC_TRIGGER_LABELS = new Set(['motion', 'alert', 'human', 'object', 'none', 'off', 'continuous']);

async function api(path, options = {}) {
  const headers = { ...(options.headers || {}) };
  if (authState.csrfToken && ['POST', 'PUT', 'PATCH', 'DELETE'].includes((options.method || 'GET').toUpperCase())) {
    headers['X-CSRF-Token'] = authState.csrfToken;
  }
  const response = await fetch(path, { ...options, headers });
  if (response.status === 401) window.location.href = '/login';
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(payload.detail || `Request failed: ${response.status}`);
  return payload;
}

function escapeHtml(value) {
  return String(value ?? '').replace(/[&<>'"]/g, (char) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', "'": '&#39;', '"': '&quot;' }[char]));
}

function formatDate(value) {
  return value ? new Date(value).toLocaleString() : 'Unknown time';
}

function detectionBadges(detections = []) {
  if (!detections.length) return '<span class="muted">No detections</span>';
  const normalized = detections
    .map((detection) => ({
      label: String(detection.label || '').trim().toLowerCase(),
      confidence: Number(detection.confidence || 0),
    }))
    .filter((detection) => detection.label);
  if (!normalized.length) return '<span class="muted">No detections</span>';
  const hasSpecific = normalized.some((detection) => detection.label !== 'motion');
  const visible = hasSpecific
    ? normalized.filter((detection) => detection.label !== 'motion')
    : normalized;
  return visible.map((detection) => `<span class="detection">${escapeHtml(detection.label)} · ${Math.round(detection.confidence * 100)}%</span>`).join('');
}

function cameraLabel(recording) {
  const metadata = recording?.event?.metadata || {};
  return metadata.camera_name || recording.camera_id || recording.source || 'unknown';
}

function recordingTriggerType(recording) {
  return String(recording.trigger_type || 'motion').trim().toLowerCase() || 'motion';
}

function recordingTriggerLabel(recording) {
  return String(recording.trigger_label || '').trim().toLowerCase() || null;
}

function recordingDetectionLabels(recording) {
  return Array.from(new Set((recording.detections || [])
    .map((detection) => String(detection.label || '').trim().toLowerCase())
    .filter(Boolean)));
}

function recordingDisplayTrigger(recording) {
  const triggerType = recordingTriggerType(recording);
  const triggerLabel = recordingTriggerLabel(recording);
  const detectionLabels = recordingDetectionLabels(recording);
  const firstSpecificDetection = detectionLabels.find((label) => !GENERIC_TRIGGER_LABELS.has(label));

  if (triggerType === 'motion' || triggerType === 'alert' || triggerType === 'human') {
    if (firstSpecificDetection) return `motion · ${firstSpecificDetection}`;
    if (triggerLabel && !GENERIC_TRIGGER_LABELS.has(triggerLabel)) return `motion · ${triggerLabel}`;
    return 'motion';
  }

  if (triggerType === 'continuous' || triggerType === 'none' || triggerType === 'off') {
    return triggerType;
  }

  if (triggerLabel && triggerLabel !== triggerType) return `${triggerType} · ${triggerLabel}`;
  return triggerLabel || triggerType;
}

function renderRecordings(recordings) {
  if (!recordings.length) {
    els.recordings.innerHTML = '<div class="empty">No recordings yet.</div>';
    return;
  }
  els.recordings.innerHTML = recordings.map((recording) => {
    const fileName = (recording.file_path || '').split(/[\\/]/).pop();
    const mediaReady = recording.media_ready !== false;
    return `
      <div class="item recording-row" data-recording-row="${recording.id}">
        <div class="item-title">
          <span>Recording #${recording.id}</span>
          <span>${formatDate(recording.started_at)}</span>
        </div>
        <div class="recording-row-badges">${detectionBadges(recording.detections)}</div>
        <p class="muted recording-row-meta">Event #${recording.event_id || 'none'} · ${Number(recording.duration_seconds || 0).toFixed(1)}s · ${escapeHtml(cameraLabel(recording))}</p>
        <p class="muted recording-row-meta">${escapeHtml(recordingDisplayTrigger(recording))} · ${escapeHtml(fileName)}</p>
        <div class="button-row">
          <button class="secondary" data-play-recording="${recording.id}" ${mediaReady ? '' : 'disabled'}>${mediaReady ? 'Play' : 'Preparing...'}</button>
          <button class="secondary delete-btn" data-delete-recording="${recording.id}">Delete</button>
        </div>
      </div>
    `;
  }).join('');
  if (recordings.some((recording) => recording.media_ready === false)) {
    clearTimeout(recordingRefreshTimer);
    recordingRefreshTimer = setTimeout(() => loadRecordings(els.recordingFilter.value.trim()), 3000);
  } else {
    clearTimeout(recordingRefreshTimer);
    recordingRefreshTimer = null;
  }
  bindRecordingButtons();
}

function renderRecordingDetails(recording) {
  els.recordingDetails.innerHTML = `
    <div><span>Recording</span><strong>#${recording.id}</strong></div>
    <div><span>Event</span><strong>${recording.event_id || 'none'}</strong></div>
    <div><span>Camera</span><strong>${escapeHtml(cameraLabel(recording))}</strong></div>
    <div><span>Trigger</span><strong>${escapeHtml(recordingDisplayTrigger(recording))}</strong></div>
    <div><span>Started</span><strong>${formatDate(recording.started_at)}</strong></div>
    <div><span>Duration</span><strong>${Number(recording.duration_seconds || 0).toFixed(1)}s</strong></div>
    <div class="wide"><span>Detections</span><strong>${(recording.detections || []).map((d) => d.label).join(', ') || 'none'}</strong></div>
  `;
}

function clamp(value, min, max) {
  return Math.min(Math.max(value, min), max);
}

function detectionAnchorSeconds(recording) {
  const startedAt = Date.parse(recording?.started_at || '');
  const eventAt = Date.parse(recording?.event?.created_at || '');
  if (!Number.isFinite(startedAt) || !Number.isFinite(eventAt)) return null;
  const seconds = (eventAt - startedAt) / 1000;
  return Number.isFinite(seconds) ? Math.max(0, seconds) : null;
}

function shouldRenderOverlayForTime(recording, playerTimeSeconds) {
  if (els.clipPlayer?.paused) return true;
  const anchorSeconds = detectionAnchorSeconds(recording);
  if (anchorSeconds === null) return true;
  const duration = Math.max(0, Number(recording?.duration_seconds || 0));
  const shiftedAnchor = anchorSeconds + overlayOffsetSeconds;
  const clampedAnchor = duration > 0 ? clamp(shiftedAnchor, 0, duration) : shiftedAnchor;
  return Math.abs(playerTimeSeconds - clampedAnchor) <= EVENT_OVERLAY_WINDOW_SECONDS / 2;
}

function clearClipOverlay() {
  if (!els.clipOverlay) return;
  const context = els.clipOverlay.getContext('2d');
  if (!context) return;
  context.setTransform(1, 0, 0, 1, 0, 0);
  context.clearRect(0, 0, els.clipOverlay.width, els.clipOverlay.height);
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

  const detections = Array.isArray(activeRecording?.detections) ? activeRecording.detections : [];
  if (!detections.length) return;
  const playerTime = Number(els.clipPlayer.currentTime || 0);
  if (!shouldRenderOverlayForTime(activeRecording, playerTime)) return;

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

async function playRecording(id) {
  const recording = await api(`/api/recordings/${id}`);
  activeRecording = recording;
  renderRecordingDetails(recording);
  if (recording.media_ready === false) {
    clearClipOverlay();
    els.clipPlayerStatus.textContent = `Recording #${id} is still being prepared.`;
    return;
  }
  els.clipPlayer.pause();
  els.clipPlayer.removeAttribute('src');
  els.clipPlayer.load();
  els.clipPlayer.src = `/api/recordings/${id}/stream?t=${Date.now()}`;
  drawClipOverlay();
  els.clipPlayerStatus.textContent = `Loading recording #${id}...`;
  try {
    els.clipPlayer.load();
    await els.clipPlayer.play();
    els.clipPlayerStatus.textContent = `Playing recording #${id}.`;
  } catch (error) {
    if (['AbortError', 'NotAllowedError'].includes(error?.name)) {
      els.clipPlayerStatus.textContent = `Recording #${id} loaded. Press play to start.`;
      return;
    }
    els.clipPlayerStatus.textContent = `Unable to play recording #${id}: ${error?.message || 'media playback failed'}.`;
  }
}

function bindRecordingButtons() {
  document.querySelectorAll('[data-play-recording]').forEach((button) => {
    button.addEventListener('click', () => playRecording(button.dataset.playRecording));
  });
  document.querySelectorAll('[data-delete-recording]').forEach((button) => {
    button.addEventListener('click', async () => {
      if (!confirm(`Delete recording #${button.dataset.deleteRecording}? This cannot be undone.`)) return;
      try {
        await api(`/api/recordings/${button.dataset.deleteRecording}`, { method: 'DELETE' });
        await loadRecordings(els.recordingFilter.value.trim());
      } catch (error) {
        alert(`Failed to delete recording: ${error.message}`);
      }
    });
  });
}

async function loadAuth() {
  const authInfo = await api('/api/auth/me');
  authState = { user: authInfo.user, csrfToken: authInfo.csrf_token };
  if (authInfo.user.role === 'admin') {
    els.deleteAllRecordingsBtn.hidden = false;
    els.deleteAllRecordingsBtn.addEventListener('click', async () => {
      if (!confirm('Delete ALL recordings and media files? Settings, users, and rules will not be changed.')) return;
      const result = await api('/api/recordings', { method: 'DELETE' });
      await loadRecordings();
      const deletedCount = Number(result?.deleted || 0);
      els.clipPlayerStatus.textContent = `Deleted ${deletedCount} recording${deletedCount === 1 ? '' : 's'}. Settings were not changed.`;
    });
  }
}

async function loadRecordings(label = '') {
  const params = new URLSearchParams();
  if (label) params.set('label', label);
  renderRecordings(await api(`/api/recordings?${params.toString()}`));
}

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

['loadedmetadata', 'loadeddata', 'play', 'pause', 'seeked', 'timeupdate'].forEach((eventName) => {
  els.clipPlayer.addEventListener(eventName, drawClipOverlay);
});

window.addEventListener('resize', drawClipOverlay);

if ('ResizeObserver' in window && els.clipPlayer) {
  overlayResizeObserver = new ResizeObserver(drawClipOverlay);
  overlayResizeObserver.observe(els.clipPlayer);
}

if (els.clipOverlayToggle) {
  const savedValue = localStorage.getItem(OVERLAY_TOGGLE_KEY);
  overlayEnabled = savedValue !== '0';
  els.clipOverlayToggle.checked = overlayEnabled;
  els.clipOverlayToggle.addEventListener('change', () => {
    overlayEnabled = Boolean(els.clipOverlayToggle.checked);
    localStorage.setItem(OVERLAY_TOGGLE_KEY, overlayEnabled ? '1' : '0');
    drawClipOverlay();
  });
}

if (els.clipOverlayOffset) {
  const savedOffsetValue = Number(localStorage.getItem(OVERLAY_OFFSET_KEY));
  overlayOffsetSeconds = Number.isFinite(savedOffsetValue) ? clamp(savedOffsetValue, -10, 10) : 0;
  els.clipOverlayOffset.value = overlayOffsetSeconds.toFixed(1);

  const updateOffset = () => {
    const parsed = Number(els.clipOverlayOffset.value);
    overlayOffsetSeconds = Number.isFinite(parsed) ? clamp(parsed, -10, 10) : 0;
    els.clipOverlayOffset.value = overlayOffsetSeconds.toFixed(1);
    localStorage.setItem(OVERLAY_OFFSET_KEY, overlayOffsetSeconds.toFixed(1));
    drawClipOverlay();
  };

  els.clipOverlayOffset.addEventListener('change', updateOffset);
  els.clipOverlayOffset.addEventListener('blur', updateOffset);
}

els.recordingSearchBtn.addEventListener('click', () => loadRecordings(els.recordingFilter.value.trim()));
els.recordingClearBtn.addEventListener('click', () => {
  els.recordingFilter.value = '';
  loadRecordings();
});
els.recordingFilter.addEventListener('keydown', (event) => {
  if (event.key === 'Enter') loadRecordings(els.recordingFilter.value.trim());
});

loadAuth().then(async () => {
  await loadRecordings();
  const selected = new URLSearchParams(window.location.search).get('recording_id');
  if (selected) playRecording(selected).catch((error) => { els.clipPlayerStatus.textContent = error.message; });
}).catch((error) => { els.clipPlayerStatus.textContent = error.message; });
