const els = {
  cameraSelect: document.getElementById('timelineCameraSelect'),
  timelineDate: document.getElementById('timelineDate'),
  fromTime: document.getElementById('timelineFromTime'),
  toTime: document.getElementById('timelineToTime'),
  filterSelect: document.getElementById('timelineFilterSelect'),
  timelineLoadBtn: document.getElementById('timelineLoadBtn'),
  timelineStatus: document.getElementById('timelineStatus'),
  timelineStatusChip: document.getElementById('timelineStatusChip'),
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
  recordingDetails: document.getElementById('recordingDetails'),
  videoModal: document.getElementById('videoModal'),
  videoModalClose: document.getElementById('videoModalClose'),
  videoModalDownload: document.getElementById('videoModalDownload'),
  timelineNowBtn: document.getElementById('timelineNowBtn'),
  // Stats
  statClips: document.getElementById('statClips'),
  statClipsSub: document.getElementById('statClipsSub'),
  statCoverage: document.getElementById('statCoverage'),
  statTriggers: document.getElementById('statTriggers'),
  statCamera: document.getElementById('statCamera'),
  statCameraSub: document.getElementById('statCameraSub'),
};

const state = {
  auth: { user: null, csrfToken: null },
  payload: null,
  activeRecordingId: null,
};

let configuredLabels = null;
let activeRecording = null;

const OVERLAY_TOGGLE_KEY = 'daygle.timeline.overlay.enabled';
// On by default; users can turn it off per-browser via the toggle.
let overlayEnabled = true;
let overlayRafId = null;
let overlayVfcHandle = null;
let overlayResizeObserver = null;
// Estimated frame duration (seconds) used to project detection boxes one
// frame ahead of the VFC mediaTime or currentTime.
let _frameDuration = 1 / 30; // default 30fps, updated on each VFC frame

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

const DETECTION_EYE_ICON = '<svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="12" cy="12" r="3"/><path d="M2 12s3-7 10-7 10 7 10 7-3 7-10 7-10-7-10-7z"/></svg>';

function recordingZoneNames(recording) {
  if (isSoundRecording(recording)) return [];
  return [...new Set((recording.detections || []).map((d) => d.zone_name).filter(Boolean))];
}

function detectionPill(label, confidence, isSound) {
  const display = isSound
    ? titleCase(String(label).replace(/_/g, ' '))
    : titleCase(String(label));
  const pct = Math.round(Number(confidence) * 100);
  if (isSound) {
    return `<span class="detection detection-sound">🔊 ${escapeHtml(display)} · ${pct}%</span>`;
  }
  return `<span class="detection detection-object">${DETECTION_EYE_ICON} ${escapeHtml(display)} · ${pct}%</span>`;
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

function recordingTrack() {
  return Array.isArray(activeRecording?.track) && activeRecording.track.length ? activeRecording.track : null;
}

function overlayShouldAnimate() {
  return overlayEnabled;
}

function startOverlayRaf() {
  const video = els.clipPlayer;
  if (!video) return;
  // Uses requestVideoFrameCallback for frame-accurate sync with the video
  // decoder. The callback provides `mediaTime` — the exact PTS of the frame
  // being displayed. We project one frame ahead (mediaTime + frameDuration)
  // so the overlay paints boxes where the object will be when the next frame
  // hits the screen, compensating for the 1-frame VFC-to-composite delay.
  // Falls back to rAF + currentTime when VFC is unavailable (older browsers).
  const useVfc = typeof video.requestVideoFrameCallback === 'function';

  let prevVfcTime = 0;
  function onVfcFrame(now, metadata) {
    if (!els.clipPlayer || els.clipPlayer.paused || !overlayShouldAnimate()) {
      overlayRafId = null;
      overlayVfcHandle = null;
      return;
    }
    // Estimate frame duration from the delta between consecutive VFC frames
    // (clamped to a reasonable 10-200ms range to filter outliers).
    const mediaTime = metadata && typeof metadata.mediaTime === 'number' ? metadata.mediaTime : null;
    if (mediaTime !== null && prevVfcTime > 0) {
      const dt = mediaTime - prevVfcTime;
      if (dt >= 0.01 && dt <= 0.2) _frameDuration = dt;
    }
    if (mediaTime !== null) prevVfcTime = mediaTime;
    drawClipOverlay(mediaTime);
    overlayVfcHandle = video.requestVideoFrameCallback(onVfcFrame);
  }

  function onRafFrame() {
    if (!els.clipPlayer || els.clipPlayer.paused || !overlayShouldAnimate()) {
      overlayRafId = null;
      return;
    }
    drawClipOverlay();
    overlayRafId = requestAnimationFrame(onRafFrame);
  }

  if (useVfc) {
    if (overlayVfcHandle !== null) return; // already running
    overlayVfcHandle = video.requestVideoFrameCallback(onVfcFrame);
  } else {
    if (overlayRafId !== null) return; // already running
    overlayRafId = requestAnimationFrame(onRafFrame);
  }
}

function stopOverlayRaf() {
  if (overlayVfcHandle !== null && els.clipPlayer && typeof els.clipPlayer.cancelVideoFrameCallback === 'function') {
    els.clipPlayer.cancelVideoFrameCallback(overlayVfcHandle);
    overlayVfcHandle = null;
  }
  if (overlayRafId !== null) {
    cancelAnimationFrame(overlayRafId);
    overlayRafId = null;
  }
}

function drawClipOverlay(vfcMediaTime) {
  if (!els.clipOverlay || !els.clipPlayer) return;
  if (!overlayEnabled) {
    clearClipOverlay();
    return;
  }
  resizeOverlayCanvas(els.clipOverlay, els.clipPlayer);
  const context = els.clipOverlay.getContext('2d');
  if (!context) return;
  context.setTransform(1, 0, 0, 1, 0, 0);
  context.clearRect(0, 0, els.clipOverlay.width, els.clipOverlay.height);

  // Use the VFC-provided mediaTime (exact frame PTS) and project one frame
  // ahead. This compensates for the inherent 1-frame delay between VFC
  // firing (after the frame was sent to compositor) and the overlay paint
  // being displayed (on the next frame). Falls back to currentTime (with
  // forward projection) for the rAF path or when VFC isn't available.
  let playerTime;
  if (typeof vfcMediaTime === 'number' && Number.isFinite(vfcMediaTime)) {
    playerTime = vfcMediaTime + _frameDuration;
  } else {
    playerTime = Number(els.clipPlayer.currentTime || 0) + _frameDuration;
  }

  // The saved detection track replays the boxes the live monitor computed
  // while the clip recorded, so playback never runs inference. Clips without
  // a track fall back to the event's static boxes.
  const track = recordingTrack();
  if (track) {
    const tracked = filterByConfiguredLabels(sampleTrackAtTime(track, playerTime));
    if (tracked.length) drawDetectionBoxesOnCanvas(els.clipOverlay, tracked, els.clipPlayer);
    return;
  }

  // Static event boxes describe the trigger moment, which sits after the
  // clip's pre-roll; drawing them from time 0 puts a frozen box over footage
  // recorded before the detection existed.
  if (!shouldRenderOverlayForTime(activeRecording, playerTime)) return;
  const allEventDetections = Array.isArray(activeRecording?.detections) ? activeRecording.detections : [];
  const hasSpecificEvent = allEventDetections.some((d) => !GENERIC_TIMELINE_LABELS.has(String(d.label || '').toLowerCase()));
  const eventDetections = filterByConfiguredLabels(
    hasSpecificEvent
      ? allEventDetections.filter((d) => !GENERIC_TIMELINE_LABELS.has(String(d.label || '').toLowerCase()))
      : allEventDetections
  );
  if (!eventDetections.length) return;
  drawDetectionBoxesOnCanvas(els.clipOverlay, eventDetections, els.clipPlayer);
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

function formatClock(seconds) {
  return formatUserClock(seconds);
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

/**
 * Returns an array of { label, confidence } sorted by confidence descending,
 * filtered to non-generic labels that pass the configuredLabels threshold.
 */
function recordingDetectionSummary(recording) {
  if (isSoundRecording(recording)) {
    const meta = recording.event?.metadata || {};
    const label = (meta.class_label || meta.label || recording.trigger_label || 'sound').toLowerCase();
    const confidence = Number(meta.confidence || 0);
    return [{ label, confidence }];
  }
  // Build best-confidence map from all detections — historical data, no config filtering.
  const best = new Map();
  for (const d of (recording.detections || [])) {
    const label = String(d.label || '').trim().toLowerCase();
    if (!label) continue;
    const conf = Number(d.confidence || 0);
    if (!best.has(label) || conf > best.get(label)) best.set(label, conf);
  }
  // Use recording.labels as the authoritative label list when available.
  const authLabels = Array.isArray(recording.labels) && recording.labels.length
    ? recording.labels.map((l) => String(l || '').trim().toLowerCase()).filter((l) => l && !GENERIC_TIMELINE_LABELS.has(l))
    : Array.from(best.keys()).filter((l) => !GENERIC_TIMELINE_LABELS.has(l));
  return authLabels
    .map((label) => ({ label, confidence: best.get(label) ?? 0 }))
    .sort((a, b) => b.confidence - a.confidence);
}

function recordingTypeLabel(recording) {
  if (isSoundRecording(recording)) {
    const meta = recording.event?.metadata || {};
    return meta.class_label || titleCase((meta.label || recording.trigger_label || 'sound').replace(/_/g, ' '));
  }
  const triggerType = recordingTriggerType(recording);
  const triggerLabel = recordingTriggerLabel(recording);
  const detectionLabels = recordingDetectionLabels(recording);

  if (triggerType === 'motion' || triggerType === 'alert' || triggerType === 'human' || triggerType === 'object') {
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

/**
 * Returns a compact timeline label for multi-object recordings:
 * shows the primary label with a count of extra objects, e.g. "Person +2".
 * Falls back to recordingTypeLabel() for single-object or generic triggers.
 */
function timelineSegmentLabel(recording) {
  const primaryLabel = recordingTypeLabel(recording);
  const detectionLabels = recordingDetectionLabels(recording);
  const extraCount = detectionLabels.filter(
    (label) => !GENERIC_TIMELINE_LABELS.has(label)
  ).length - 1;
  if (extraCount > 0) {
    return `${primaryLabel} +${extraCount}`;
  }
  return primaryLabel;
}

function recordingColorKey(recording) {
  if (isSoundRecording(recording)) return '__sound__';
  return recordingTypeLabel(recording).toLowerCase();
}

function recordingTriggerSummary(recording) {
  const confidenceSummary = recordingDetectionSummary(recording)
    .filter((d) => d.confidence > 0)
    .map((d) => `${titleCase(d.label)} ${Math.round(d.confidence * 100)}%`)
    .join(' / ');
  if (confidenceSummary) return confidenceSummary;
  if (isSoundRecording(recording)) {
    return `🔊 ${recordingTypeLabel(recording)}`;
  }
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

function filterDisplayLabel(value) {
  if (value === '__sound__') return 'Sound';
  if (value === '__object__') return 'Object';
  return titleCase(value || '');
}

function matchesRecordingFilter(recording, filterValue) {
  const normalized = String(filterValue || '').trim().toLowerCase();
  if (!normalized) return true;
  if (normalized === '__sound__') return isSoundRecording(recording);
  if (normalized === '__object__') return !isSoundRecording(recording);
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

function isSoundRecording(recording) {
  return recording?.event?.metadata?.source === 'sound-detection';
}

function colorForKey(key) {
  if (key === '__sound__') return '#a855f7';
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

  const soundCount = recordings.filter(isSoundRecording).length;
  const objectCount = recordings.length - soundCount;
  const options = [{ value: '', label: `All recordings${recordings.length ? ` (${recordings.length})` : ''}` }];
  if (soundCount > 0) options.push({ value: '__sound__', label: `Sound (${soundCount})` });
  if (objectCount > 0) options.push({ value: '__object__', label: `Object (${objectCount})` });
  const seen = new Set(['', '__sound__', '__object__']);
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
  const clipLabel = totalRecordingCount > recordings.length ? `${recordings.length} of ${totalRecordingCount}` : `${recordings.length}`;

  // Separate object vs sound trigger counts
  const objectTriggers = new Set();
  const soundTriggers = new Set();
  recordings.forEach((recording) => {
    const label = recordingTypeLabel(recording).toLowerCase();
    if (isSoundRecording(recording)) {
      soundTriggers.add(label);
    } else {
      objectTriggers.add(label);
    }
  });

  els.timelineSummary.innerHTML = `
    <div><span>Camera</span><strong>${escapeHtml(payload.camera?.name || payload.camera?.id || 'Unknown')}</strong></div>
    <div><span>Day</span><strong>${escapeHtml(formatUserDate(payload.day || ''))}</strong></div>
    <div><span>Clips</span><strong>${escapeHtml(clipLabel)}</strong></div>
    <div><span>Coverage</span><strong>${escapeHtml(formatDuration(totalSeconds))}</strong></div>
    <div class="wide"><span>Triggers</span><strong>${recordings.length ? `${objectTriggers.size} objects / ${soundTriggers.size} sounds` : 'none'}</strong></div>
  `;
  // Also feed the top stats grid.
  if (els.statClips) {
    els.statClips.textContent = String(recordings.length);
    if (els.statClipsSub) {
      if (totalRecordingCount > recordings.length) {
        els.statClipsSub.textContent = `${recordings.length} of ${totalRecordingCount} clips match the filter`;
      } else {
        els.statClipsSub.textContent = els.filterSelect.value
          ? `Matching “${filterDisplayLabel(els.filterSelect.value)}”`
          : 'Matching the current filter';
      }
    }
  }
  if (els.statCoverage) {
    els.statCoverage.textContent = formatDuration(totalSeconds);
  }
  if (els.statTriggers) {
    els.statTriggers.textContent = `${objectTriggers.size}/${soundTriggers.size}`;
  }
  if (els.statCamera) {
    els.statCamera.textContent = payload.camera?.name || payload.camera?.id || 'Unknown';
  }
  if (els.statCameraSub) {
    const day = formatUserDate(payload.day || '');
    els.statCameraSub.textContent = day ? `Showing ${day}` : 'Currently displayed timeline';
  }
}

function setTimelineStatusChip(state) {
  if (!els.timelineStatusChip) return;
  els.timelineStatusChip.textContent = state.label;
  els.timelineStatusChip.className = 'chip ' + (
    state.kind === 'empty' ? 'chip-warn' :
    state.kind === 'error' ? 'chip-warn' :
    state.kind === 'filtered' ? 'chip-info' :
    'chip-dim'
  );
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
        title="${escapeHtml(`${recordingTriggerSummary(recording)} · ${formatClock(origStart)} · ${formatDuration(recording.duration_seconds)}${recordingConfidenceText(recording)}`)}"
        style="left:${left}%;width:${width}%;top:${recording.rowIndex * TIMELINE_ROW_HEIGHT + 8}px;--segment-color:${color};"
      >
        <span class="timeline-segment-label" hidden>${escapeHtml(timelineSegmentLabel(recording))}</span>
        <span class="timeline-segment-time" hidden>${escapeHtml(formatClock(origStart))}</span>
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
    const label = titleCase(timelineSegmentLabel(recording));
    const start = formatClock(recording.timeline_start_seconds || 0);
    const end = formatClock(recording.timeline_end_seconds || 0);
    const duration = formatDuration(recording.duration_seconds);
    const camera = escapeHtml(cameraLabel(recording));
    const detections = recordingDetectionSummary(recording);
    const isSound = isSoundRecording(recording);
    const confidenceBadges = detections
      .filter((d) => d.confidence > 0)
      .map((d) => detectionPill(d.label, d.confidence, isSound))
      .join('');
    const tooltip = detections
      .map((d) => `${titleCase(d.label)} · ${Math.round(d.confidence * 100)}%`)
      .join('\n');
    const typeLabel = isSound ? 'Sound' : 'Object';
    const zones = recordingZoneNames(recording);
    const zoneSuffix = zones.length ? ` · ${zones.map(escapeHtml).join(', ')}` : '';
    return `
      <button class="timeline-recording-item${activeClass}" type="button" data-recording-id="${recording.id}" data-tooltip="${escapeHtml(tooltip)}">
        <span class="timeline-recording-color" style="background:${color}"></span>
        <span class="timeline-recording-main">
          <span class="timeline-recording-title-row">
            <span class="activity-item-type">${typeLabel}</span>
            <strong>Recording #${recording.id}</strong>
          </span>
          <span>${escapeHtml(start)} – ${escapeHtml(end)} · ${camera}${zoneSuffix}</span>
          ${confidenceBadges ? `<span class="timeline-recording-confidence-row">${confidenceBadges}</span>` : ''}
        </span>
        <span class="timeline-recording-meta">
          <span>${escapeHtml(duration)}</span>
        </span>
      </button>
    `;
  }).join('');
}

function recordingConfidenceText() {
  return '';
}

function renderRecordingDetails(recording) {
  const isSound = isSoundRecording(recording);
  const detections = recordingDetectionSummary(recording);
  const detectionBadges = detections.length
    ? detections.map((d) => detectionPill(d.label, d.confidence, isSound)).join(' ')
    : 'none';
  const detectionLabel = isSound ? 'Sound' : 'Detections';
  const zones = recordingZoneNames(recording);
  const zoneRow = zones.length ? `<div><span>Zone</span><strong>${zones.map(escapeHtml).join(', ')}</strong></div>` : '';
  const triggerRow = detections.length ? '' : `<div><span>Trigger</span><strong>${escapeHtml(recordingTriggerSummary(recording))}</strong></div>`;
  els.recordingDetails.innerHTML = `
    <div><span>Recording</span><strong><a href="/recordings?recording_id=${recording.id}" class="timeline-recording-link">#${recording.id} ↗</a></strong></div>
    <div><span>Camera</span><strong>${escapeHtml(cameraLabel(recording))}</strong></div>
    ${zoneRow}
    ${triggerRow}
    <div><span>Started</span><strong>${escapeHtml(formatDateTime(recording.started_at))}</strong></div>
    <div><span>Duration</span><strong>${escapeHtml(formatDuration(recording.duration_seconds))}</strong></div>
    <div class="wide"><span>${detectionLabel}</span><strong class="recording-detail-detections">${detectionBadges}</strong></div>
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
  activeRecording = null;
}

async function playRecording(recordingId, updateHistory = true) {
  const recording = await api(`/api/recordings/${recordingId}`);
  activeRecording = recording;
  state.activeRecordingId = Number(recording.id);
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
    const formattedDay = formatUserDate(state.payload.day);
    els.timelineStatus.textContent = allRecordings.length
      ? `No recordings match the selected filter for ${state.payload.camera.name} on ${formattedDay}.`
      : `No recordings found for ${state.payload.camera.name} on ${formattedDay}.`;
    setTimelineStatusChip({
      kind: allRecordings.length ? 'empty' : 'empty',
      label: allRecordings.length ? 'No matches' : 'No recordings',
    });
    clearPlayback(false);
    replaceUrl(null);
    return;
  }

  const filterLabel = els.filterSelect.value ? ` matching ${filterDisplayLabel(els.filterSelect.value)}` : '';
  const { fromSeconds, toSeconds } = getTimeRangeConfig();
  const timeRangeLabel = (fromSeconds > 0 || toSeconds < DAY_SECONDS)
    ? ` from ${formatUserClock(fromSeconds)} to ${formatUserClock(toSeconds)}`
    : '';
  els.timelineStatus.textContent = `${recordings.length} clip${recordings.length === 1 ? '' : 's'}${filterLabel}${timeRangeLabel} for ${state.payload.camera.name} on ${formatUserDate(state.payload.day)}.`;
  setTimelineStatusChip({
    kind: els.filterSelect.value || (fromSeconds > 0 || toSeconds < DAY_SECONDS) ? 'filtered' : 'ready',
    label: els.filterSelect.value ? 'Filtered' : (fromSeconds > 0 || toSeconds < DAY_SECONDS) ? 'Windowed' : 'Ready',
  });

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
  setTimelineStatusChip({ kind: 'idle', label: 'Loading' });
  const timezoneOffsetMinutes = new Date().getTimezoneOffset();
  const payload = await api(
    `/api/recordings/timeline?camera_id=${encodeURIComponent(cameraId)}&day=${encodeURIComponent(day)}&tz_offset_minutes=${timezoneOffsetMinutes}`,
  );
  state.payload = payload;
  populateControls(payload);
  populateFilterOptions(payload.recordings || []);
  await renderFilteredTimeline({ preserveSelection });
}

async function loadAuth() {
  const authInfo = await api('/api/auth/me');
  state.auth = { user: authInfo.user, csrfToken: authInfo.csrf_token };
  // Date/time display preferences are now global (set in nav.js from the
  // same /api/auth/me response); nothing page-local to do here.
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

els.timelineLoadBtn.addEventListener('click', () => {
  loadTimeline({ preserveSelection: false }).catch((error) => {
    els.timelineStatus.textContent = error.message;
    setTimelineStatusChip({ kind: 'error', label: 'Error' });
  });
});

// "Now" shortcut: set Day=today, From=00:00, To=current local time, then reload.
els.timelineNowBtn?.addEventListener('click', () => {
  const now = new Date();
  const today = new Date(now.getTime() - now.getTimezoneOffset() * 60000).toISOString().slice(0, 10);
  const hh = String(now.getHours()).padStart(2, '0');
  const mm = String(now.getMinutes()).padStart(2, '0');
  els.timelineDate.value = today;
  els.fromTime.value = '00:00';
  els.toTime.value = `${hh}:${mm}`;
  loadTimeline({ preserveSelection: false }).catch((error) => {
    els.timelineStatus.textContent = error.message;
    setTimelineStatusChip({ kind: 'error', label: 'Error' });
  });
});

els.cameraSelect.addEventListener('change', () => {
  loadTimeline({ preserveSelection: false }).catch((error) => {
    els.timelineStatus.textContent = error.message;
    setTimelineStatusChip({ kind: 'error', label: 'Error' });
  });
});

els.timelineDate.addEventListener('change', () => {
  loadTimeline({ preserveSelection: false }).catch((error) => {
    els.timelineStatus.textContent = error.message;
    setTimelineStatusChip({ kind: 'error', label: 'Error' });
  });
});

els.filterSelect.addEventListener('change', () => {
  renderFilteredTimeline({ preserveSelection: true }).catch((error) => {
    els.timelineStatus.textContent = error.message;
    setTimelineStatusChip({ kind: 'error', label: 'Error' });
  });
});

els.fromTime.addEventListener('change', () => {
  renderFilteredTimeline({ preserveSelection: true }).catch((error) => {
    els.timelineStatus.textContent = error.message;
    setTimelineStatusChip({ kind: 'error', label: 'Error' });
  });
});

els.toTime.addEventListener('change', () => {
  renderFilteredTimeline({ preserveSelection: true }).catch((error) => {
    els.timelineStatus.textContent = error.message;
    setTimelineStatusChip({ kind: 'error', label: 'Error' });
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

// timeupdate is intentionally omitted — the requestVideoFrameCallback/rAF loop
// already draws the overlay on every frame during playback, making it redundant.
['loadedmetadata', 'loadeddata', 'pause', 'seeked'].forEach((eventName) => {
  els.clipPlayer.addEventListener(eventName, () => {
    drawClipOverlay();
  });
});

els.clipPlayer.addEventListener('play', () => {
  if (overlayShouldAnimate()) startOverlayRaf();
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
  overlayEnabled = savedValue !== '0';
  els.clipOverlayToggle.checked = overlayEnabled;
  els.clipOverlayToggle.addEventListener('change', () => {
    overlayEnabled = Boolean(els.clipOverlayToggle.checked);
    localStorage.setItem(OVERLAY_TOGGLE_KEY, overlayEnabled ? '1' : '0');
    if (els.clipPlayer && !els.clipPlayer.paused && overlayShouldAnimate()) {
      startOverlayRaf();
    } else if (!overlayEnabled) {
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
  setTimelineStatusChip({ kind: 'idle', label: 'Loading' });
  await loadConfiguredLabels();
  await loadTimeline({ preserveSelection: true });
}).catch((error) => {
  els.timelineStatus.textContent = error.message;
  els.clipPlayerStatus.textContent = error.message;
  setTimelineStatusChip({ kind: 'error', label: 'Error' });
});

// Re-render the timeline (ticks, segments, list, modal) when the user's
// date_format / time_format changes in another tab. Preserves the
// currently selected camera / day / filter / time range so the user keeps
// what they were looking at — only the rendered formatting changes.
window.daygleDatePrefsChanged = function daygleDatePrefsChanged() {
  if (typeof loadTimeline !== 'function' || !state || !state.payload) return;
  loadTimeline({ preserveSelection: true }).catch((error) => {
    els.timelineStatus.textContent = error.message;
    setTimelineStatusChip({ kind: 'error', label: 'Error' });
  });
};
