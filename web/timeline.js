const els = {
  cameraSelect: document.getElementById('timelineCameraSelect'),
  timelineDate: document.getElementById('timelineDate'),
  fromTime: null, // populated by renderTimelineTimeSelects() below
  toTime: null,   // populated by renderTimelineTimeSelects() below
  filterSelect: document.getElementById('timelineFilterSelect'),
  timelineLoadBtn: document.getElementById('timelineLoadBtn'),
  // The timeline visualisation now renders into two parallel cards
  // (Objects + Sounds). Their per-card DOM refs live on TIMELINE_CARDS
  // (populated by initTimelineCards() further down) so a single render
  // call can fan out to both tracks without scattering element IDs
  // through this map. Segment clicks also stream through document-level
  // delegation so we don't keep a hand-bound listener per card.
  // timelineLegend stays in els (returns null on the page since there's
  // no #timelineLegend element any more) so tests/test_timeline_legend_js.test.js
  // - which calls renderLegend() directly against an in-vm stub - can
  // still write into the element it expects.
  timelineLegend: document.getElementById('timelineLegend'),
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

// ── Filter state & pickers ────────────────────────────────────────────────
// The From/To mounts in the Timeline Controls form render through the shared
// `renderTimeSelect` / `setTimeSelectValue` pair (web/utils.js) so the time
// pickers follow the user's Profile > Time Format choice (12h + AM/PM vs.
// 24h), matching /recordings, /sounds and /zones. Re-rendered on init, on
// the "Now" shortcut, on URL deep-link, and whenever the cross-tab prefs
// hook fires so a profile change instantly swaps the picker style without
// a manual refresh.
const TIMELINE_FILTER_TIME_FROM_DEFAULT = '00:00';
// Minute resolution is 5 minutes (shared with /recordings, /sounds and
// /zones), so 23:55 is the latest valid value that still pins to the end
// of the day.
const TIMELINE_FILTER_TIME_TO_DEFAULT = '23:55';

function renderTimelineTimeSelect(mountId, defaultValue) {
  const mount = document.getElementById(mountId);
  if (!mount) return null;
  const role = mount.dataset.timeRole || '';
  mount.innerHTML = renderTimeSelect(defaultValue, 'data-timeline-time-role', role);
  return mount.querySelector('.time-select-wrap');
}

function renderTimelineTimeSelects() {
  els.fromTime = renderTimelineTimeSelect('timelineFromTimeMount', TIMELINE_FILTER_TIME_FROM_DEFAULT);
  els.toTime = renderTimelineTimeSelect('timelineToTimeMount', TIMELINE_FILTER_TIME_TO_DEFAULT);
}

renderTimelineTimeSelects();

// ── Two-card timeline ────────────────────────────────────────────────────
// /timeline now renders two parallel cards: an Objects track (motion +
// detected objects) and a Sounds track. Both cards share the same Camera /
// Day / Filter / From / To selectors above so they always represent the
// same selected day in sync, and the page-level API call
// (/api/recordings/timeline) returns the union of both. initTimelineCards()
// binds each card's hours/grid/rows/status/statusChip to its data-*
// attributes once at script load so renderTimelineForCard() can write into
// one card without the other knowing about it.
const TIMELINE_CARDS = [
  { kind: 'object', root: null, hours: null, grid: null, rows: null, status: null, statusChip: null },
  { kind: 'sound',  root: null, hours: null, grid: null, rows: null, status: null, statusChip: null },
];

function initTimelineCards() {
  TIMELINE_CARDS.forEach((card) => {
    const root = document.querySelector(`[data-timeline-card="${card.kind}"]`);
    if (!root) return;
    card.root = root;
    card.hours = root.querySelector('[data-timeline-card-hours]');
    card.grid = root.querySelector('[data-timeline-card-grid]');
    card.rows = root.querySelector('[data-timeline-card-rows]');
    card.status = root.querySelector('[data-timeline-card-status]');
    card.statusChip = root.querySelector('[data-timeline-card-status-chip]');
  });
}

initTimelineCards();

// CSRF token and current user live on window.daygleAuth (loaded by
// web/utils.js) and are populated by each page's loadAuth() via
// setApiAuth(...). Page-local state below is timeline-specific.
const state = {
  payload: null,
  activeRecordingId: null,
};

let configuredLabels = null;
let activeRecording = null;

// TIMELINE_OVERLAY_TOGGLE_KEY (alongside RECORDINGS_OVERLAY_TOGGLE_KEY from
// recordings.js and LIVE_AI_TRACK_KEY from live.js) now lives in
// web/utils.js - exposed on window.daygleUi and visible as bare constants.
// Kept deliberately independent of RECORDINGS_OVERLAY_TOGGLE_KEY so toggling
// the overlay on /recordings doesn't flip the same preference on /timeline.
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
  // Read via timeSelectValue() so the values reflect the hour+minute(/AM/PM)
  // the user sees in the picker rather than the browser-native
  // `<input type="time">` element which rendered in the viewer's locale
  // (often 12-hour even when 24h is preferred).
  const fromSeconds = parseTimeInput(timeSelectValue(els.fromTime) || TIMELINE_FILTER_TIME_FROM_DEFAULT, 0);
  const toRaw = parseTimeInput(timeSelectValue(els.toTime) || TIMELINE_FILTER_TIME_TO_DEFAULT, DAY_SECONDS);
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

// Generic trigger labels are shared with the rest of the app via
// web/utils.js (loaded before this script). The bare name
// `GENERIC_TRIGGER_LABELS` resolves to the same set the recordings list,
// the dashboard activity feed and the playback modal use so the
// motion-vs-object boundary lives in one place.
const GENERIC_TIMELINE_LABELS = GENERIC_TRIGGER_LABELS;

// DETECTION_EYE_ICON, SOUND_CLASS_IDS, isSoundLabel and detectionPill() all
// live in web/utils.js now (loaded before this script). Existing call sites
// call detectionPill() directly with the same (label, confidence, isSound)
// signature.

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
  // decoder. The callback provides `mediaTime` - the exact PTS of the frame
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

// api() is provided by web/utils.js (loaded before this script) - it reads
// the CSRF token from window.daygleAuth.csrfToken and handles 401 redirects
// so every page shares identical auth and error semantics.

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

function recordingColorKey(recording) {
  if (isSoundRecording(recording)) return '__sound__';
  // Motion-only clips get a single reserved color key so every segment,
  // legend swatch and timeline-recording-item bar for motion uses the
  // fixed teal accent instead of being hashed against the random
  // SEGMENT_COLORS palette (which would generate one random color per
  // distinct recording and split same-category clips across hues).
  if (isMotionOnlyRecording(recording)) return '__motion__';
  // A recording whose primary label is a sound class (e.g. a Dog Bark clip
  // that isn't sound-detection sourced) shares the reserved sound colour so
  // its segment matches the purple sound chip the legend now renders for it.
  const typeLabel = recordingTypeLabel(recording).toLowerCase();
  if (isSoundLabel(typeLabel)) return '__sound__';
  return typeLabel;
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
    // Tightened to motion-only recordings now that motion is a real
    // category on its own. The previous "trigger type != placeholder"
    //    heuristic also matched object recordings with a non-continuous
    //    trigger, which made the dropdown a worse label than All.
    return !isSoundRecording(recording) && isMotionOnlyRecording(recording);
  }
  return recordingFilterTokens(recording).has(normalized);
}

// Kept page-local (not hoisted to utils.js): app.js and yamnet-tflite.js
// define their own cameraLabel() with different signatures, and a shared
// global would collide on the dashboard / yamnet pages.
function cameraLabel(recording) {
  const metadata = recording?.event?.metadata || {};
  return metadata.camera_name || recording.camera_id || recording.source || 'unknown';
}

function colorForKey(key) {
  if (key === '__sound__') return '#a855f7';
  if (key === '__motion__') return '#2dd4bf';
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
  const fromTime = timeSelectValue(els.fromTime) || '';
  const toTime = timeSelectValue(els.toTime) || '';
  if (cameraId) params.set('camera_id', cameraId);
  if (day) params.set('day', day);
  // Skip the URL params when the filter matches the picker's default so deep
  // links stay clean (and old `to_time=23:59` URLs no longer match the new
  // 23:55 end-of-day sentinel - treat them as "all day" via the constant).
  if (fromTime && fromTime !== TIMELINE_FILTER_TIME_FROM_DEFAULT) params.set('from_time', fromTime);
  if (toTime && toTime !== TIMELINE_FILTER_TIME_TO_DEFAULT) params.set('to_time', toTime);
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

  // The top stats grid (Clips / Coverage / Triggers / Camera) is the single
  // source of truth for these numbers; the inline summary panel that used to
  // repeat them here was removed to cut the duplication. Kept optional so any
  // future markup that re-adds #timelineSummary still renders.
  if (els.timelineSummary) {
    els.timelineSummary.innerHTML = `
      <div><span>Camera</span><strong>${escapeHtml(payload.camera?.name || payload.camera?.id || 'Unknown')}</strong></div>
      <div><span>Day</span><strong>${escapeHtml(formatUserDate(payload.day || ''))}</strong></div>
      <div><span>Clips</span><strong>${escapeHtml(clipLabel)}</strong></div>
      <div><span>Coverage</span><strong>${escapeHtml(formatDuration(totalSeconds))}</strong></div>
      <div class="wide"><span>Triggers</span><strong>${recordings.length ? `${objectTriggers.size} objects / ${soundTriggers.size} sounds` : 'none'}</strong></div>
    `;
  }
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

function setTimelineStatusChip(card, chipState) {
  // chips used to be a single global element keyed off #timelineStatusChip;
  // the visualisation now has two cards, each with its own chip, so the
  // helper takes the card ref and writes into card.statusChip directly.
  if (!card || !card.statusChip) return;
  card.statusChip.textContent = chipState.label;
  card.statusChip.className = 'chip ' + (
    chipState.kind === 'empty' ? 'chip-warn' :
    chipState.kind === 'error' ? 'chip-warn' :
    chipState.kind === 'filtered' ? 'chip-info' :
    'chip-dim'
  );
}

// Fan-out error helper. Every error path used to write into one global
// #timelineStatus / #timelineStatusChip element, but the page now has two
// cards. reportTimelineError() mirrors the message onto both so each card's
// chip flips to "Error" and a status blurb appears below each header
// instead of silently blowing away only the first card.
function reportTimelineError(message) {
  TIMELINE_CARDS.forEach((card) => {
    if (card.status) card.status.textContent = message;
    setTimelineStatusChip(card, { kind: 'error', label: 'Error' });
  });
}

function renderLegend(recordings) {
  // The legend is a "what happened today" key, but it now splits into two
  // side-by-side buckets that mirror the rest of the timeline surface: an
  // Objects group (Motion + every distinct detected-object label) and a
  // Sounds group (one chip per distinct sound class). Users can tell at a
  // glance which kind of trigger dominated the day without parsing every
  // chip, and the group titles line up with the same two-bucket vocabulary
  // the stats grid (statTriggers "X/Y objects/sounds") and the Filter
  // dropdown's "Sound" / "Object" options already speak. A multi-object
  // recording still contributes a chip for every object it saw (e.g.
  // Person AND Dog), and every sound class still gets its own chip
  // rather than collapsing into one.
  const objectChips = [];
  const soundChips = [];
  const seen = new Set();
  const addChip = (group, dedupKey, label, color, kind) => {
    if (seen.has(dedupKey)) return;
    seen.add(dedupKey);
    group.push({ label, color, kind });
  };
  recordings.forEach((recording) => {
    if (isMotionOnlyRecording(recording)) {
      addChip(objectChips, '__motion__', 'Motion', colorForKey('__motion__'), 'motion');
      return;
    }
    const isSound = isSoundRecording(recording);
    // recordingDetectionSummary returns one entry per unique label already
    // (the sound class for sounds; every detected object for object clips),
    // with generic trigger words filtered out. Fall back to the type label for
    // the rare label-less clip (e.g. a continuous recording).
    const summary = recordingDetectionSummary(recording);
    const labels = summary.length ? summary.map((entry) => entry.label) : [recordingTypeLabel(recording)];
    labels.forEach((rawLabel) => {
      const label = String(rawLabel || '').toLowerCase();
      if (!label) return;
      // Classify each label independently: a sound-class label (e.g. Dog Bark,
      // Car Alarm) is always a sound chip even when it rides on an object
      // recording, matching detectionPill()'s per-label icon rule. Keying the
      // whole chip off isSoundRecording() alone let a sound label leak in as a
      // second "object" chip - the recurring Dog Bark legend duplicate.
      if (isSound || isSoundLabel(label)) {
        addChip(soundChips, `__sound__:${label}`, label, colorForKey('__sound__'), 'sound');
      } else {
        addChip(objectChips, label, label, colorForKey(label), 'object');
      }
    });
  });
  if (!objectChips.length && !soundChips.length) {
    els.timelineLegend.innerHTML = '<p class="muted">No recordings match this filter for the selected day.</p>';
    return;
  }
  const renderChip = (item) => {
    // Reserve the dedicated icons so the legend reads the same as the
    // row + pill treatments on the other surfaces: speaker for sound,
    // running man for motion, eye for everything else.
    const labelText = item.kind === 'motion' ? 'Motion' : titleCase(item.label);
    const icon = item.kind === 'sound' ? '🔊' : item.kind === 'motion' ? DETECTION_MOTION_ICON : DETECTION_EYE_ICON;
    return `
      <span class="timeline-legend-chip">
        <span class="timeline-legend-swatch" style="background:${item.color}"></span>
        ${icon} <span>${escapeHtml(labelText)}</span>
      </span>
    `;
  };
  // Render each non-empty group with its own small uppercase title. The
  // Objects eyebrow uses the motion icon when a motion chip is present
  // (otherwise the eye icon) so the section header matches the most
  // recognisable child icon for that day; the Sounds eyebrow keeps the
  // speaker icon since every Sound chip already uses it. Each group gets a
  // role + aria-label so screen readers announce the two-bucket split the
  // sighted UI shows, plus a small numeric badge next to the title that
  // mirrors the "X/Y" count on the stats grid's Triggers card. Empty
  // groups are skipped wholesale (avoids dangling headers when a day is
  // all motion or all sound).
  const objectEyebrowIcon = objectChips.some((chip) => chip.kind === 'motion') ? DETECTION_MOTION_ICON : DETECTION_EYE_ICON;
  const renderGroup = (title, icon, items, ariaLabel) => items.length
    ? `<div class="timeline-legend-group" role="group" aria-label="${escapeHtml(ariaLabel)}">
        <div class="timeline-legend-group-title">
          ${icon}
          <span class="timeline-legend-group-name">${escapeHtml(title)}</span>
          <span class="timeline-legend-group-count" aria-hidden="true">${items.length}</span>
        </div>
        <div class="timeline-legend-chips">${items.map(renderChip).join('')}</div>
      </div>`
    : '';
  els.timelineLegend.innerHTML = [
    renderGroup('Objects', objectEyebrowIcon, objectChips, 'Objects legend'),
    renderGroup('Sounds', '🔊', soundChips, 'Sounds legend'),
  ].join('');
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

function renderTimelineForCard(card, viewPayload, cardRecordings, totalRecordingCount) {
  const { fromSeconds, toSeconds, totalSeconds, tickIntervalSeconds } = getTimeRangeConfig();

  // Clip this card's recordings to the visible [fromSeconds, toSeconds]
  // window and rebase positions so the existing left%/width% flow still
  // resolves against the shared hour scale. Identical math to the
  // pre-split renderTimeline() - we just slice the input set first.
  const windowRecordings = (cardRecordings || [])
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

  const recordings = buildTimelineLayout(windowRecordings, Number(viewPayload.pre_event_seconds || 0));
  const rowCount = Math.max(1, recordings.reduce((max, recording) => Math.max(max, recording.rowIndex + 1), 0));

  const ticks = [];
  for (let s = fromSeconds; s <= toSeconds; s += tickIntervalSeconds) ticks.push(s);
  if (ticks[ticks.length - 1] < toSeconds) ticks.push(toSeconds);

  const tickPos = (s) => ((s - fromSeconds) / totalSeconds) * 100;

  card.hours.innerHTML = ticks.map((s) => (
    `<span class="timeline-hour major" style="left:${tickPos(s)}%">${formatClock(Math.min(s, DAY_SECONDS - 1))}</span>`
  )).join('');
  card.grid.innerHTML = ticks.map((s) => `
    <span class="timeline-grid-line" style="left:${tickPos(s)}%"></span>
  `).join('');
  card.rows.style.height = `${Math.max(96, rowCount * TIMELINE_ROW_HEIGHT)}px`;

  const kindWord = card.kind === 'sound' ? 'sound' : 'object';
  const cameraName = viewPayload.camera?.name || viewPayload.camera?.id || 'this camera';
  const formattedDay = formatUserDate(viewPayload.day);

  // Per-card empty state. Three distinguishable reasons can leave a card
  // blank: "no recordings today at all", "filter narrowed this kind away"
  // (recordings exist elsewhere but none of this kind), and "recordings
  // exist for this kind but not in the current time window". We surface
  // each one so the user can tell which lever to adjust instead of seeing
  // a generic "no data" message regardless of cause.
  function emptyStateFor(reason) {
    // Different chip labels per kind ("No object matches" / "No sound
    // matches") so users glancing at the chip row can tell which card
    // ran out without reading the row text below it.
    const reasonPrefix = kindWord === 'sound' ? 'sound' : 'object';
    if (reason === 'no-data') {
      return {
        chipKind: 'empty',
        chipLabel: 'No recordings',
        statusText: `No ${kindWord} recordings found for ${escapeHtml(cameraName)} on ${escapeHtml(formattedDay)}.`,
        rowHtml: `<div class="empty timeline-empty">No ${kindWord} recordings for ${escapeHtml(cameraName)} on ${escapeHtml(formattedDay)}.</div>`,
      };
    }
    if (reason === 'filtered') {
      return {
        chipKind: 'filtered',
        chipLabel: `No ${reasonPrefix} matches`,
        statusText: `No ${kindWord} recordings match the selected filter for ${escapeHtml(cameraName)} on ${escapeHtml(formattedDay)}.`,
        rowHtml: `<div class="empty timeline-empty">No ${kindWord} recordings match the selected filter for ${escapeHtml(cameraName)} on ${escapeHtml(formattedDay)}.</div>`,
      };
    }
    // reason === 'windowed' - cards have recordings but none visible in
    // [fromSeconds, toSeconds] for this kind.
    return {
      chipKind: 'filtered',
      chipLabel: `${reasonPrefix} windowed`,
      statusText: `No ${kindWord} recordings in the visible time window.`,
      rowHtml: `<div class="empty timeline-empty">No ${kindWord} recordings in the selected time window.</div>`,
    };
  }

  if (!recordings.length) {
    const reason = cardRecordings.length === 0
      ? (totalRecordingCount > 0 ? 'filtered' : 'no-data')
      : 'windowed';
    const msg = emptyStateFor(reason);
    card.rows.innerHTML = msg.rowHtml;
    setTimelineStatusChip(card, { kind: msg.chipKind, label: msg.chipLabel });
    if (card.status) card.status.textContent = msg.statusText;
    return;
  }

  card.rows.innerHTML = recordings.map((recording) => {
    const visStart = Number(recording.timeline_start_seconds || 0);
    const origStart = Number(recording._orig_start_seconds ?? visStart + fromSeconds);
    const duration = Math.max(1, Number(recording.timeline_duration_seconds || 1));
    const left = (visStart / totalSeconds) * 100;
    const width = Math.max((duration / totalSeconds) * 100, 0.1);
    const color = colorForKey(recordingColorKey(recording));
    const activeClass = Number(recording.id) === Number(state.activeRecordingId) ? ' active' : '';
    const tinyClass = width < 0.25 ? ' tiny' : '';
    return `
      <button
        class="timeline-segment${activeClass}${tinyClass}"
        type="button"
        data-recording-id="${escapeHtml(String(recording.id))}"
        title="${escapeHtml(`${recordingTriggerSummary(recording)} · ${formatClock(origStart)} · ${formatDuration(recording.duration_seconds)}`)}"
        style="left:${left}%;width:${width}%;top:${recording.rowIndex * TIMELINE_ROW_HEIGHT + 8}px;--segment-color:${color};"
      ></button>
    `;
  }).join('');

  // Success path: per-card "Ready / Filtered / Windowed" chip + a status
  // blurb that mirrors the global stats grid's clip count but for this
  // kind only, so an object-heavy day and a sound-heavy day each speak
  // for themselves regardless of how the other card is populated.
  const isFiltered = !!els.filterSelect.value;
  const isWindowed = fromSeconds > 0 || toSeconds < DAY_SECONDS;
  const chipKind = isFiltered || isWindowed ? 'filtered' : 'ready';
  const chipLabel = isFiltered ? 'Filtered' : isWindowed ? 'Windowed' : 'Ready';
  setTimelineStatusChip(card, { kind: chipKind, label: chipLabel });
  if (card.status) {
    const filterPart = isFiltered ? ` matching ${filterDisplayLabel(els.filterSelect.value)}` : '';
    const windowPart = isWindowed ? ` from ${formatUserClock(fromSeconds)} to ${formatUserClock(toSeconds)}` : '';
    card.status.textContent = `${cardRecordings.length} ${kindWord} clip${cardRecordings.length === 1 ? '' : 's'}${filterPart}${windowPart} for ${escapeHtml(cameraName)} on ${escapeHtml(formattedDay)}.`;
  }
}

function renderRecordingDetails(recording) {
  const isSound = isSoundRecording(recording);
  const isMotionOnly = isMotionOnlyRecording(recording);
  const detections = recordingDetectionSummary(recording);
  // The "Sound" / "Motion" / "Detections" label tracks the source the row
  // uses on the timeline + recordings list so opening a clip never
  // surprises users with a different category name. Motion-only clips
  // render the teal motion pill instead of the bare "none" placeholder.
  let detectionBadges;
  let detectionLabel;
  if (isMotionOnly) {
    detectionLabel = 'Motion';
    detectionBadges = motionPill(motionConfidenceFor(recording));
  } else if (isSound) {
    detectionLabel = 'Sound';
    detectionBadges = detections.length
      ? detections.map((d) => detectionPill(d.label, d.confidence, true)).join(' ')
      : 'none';
  } else {
    detectionLabel = 'Detections';
    detectionBadges = detections.length
      ? detections.map((d) => detectionPill(d.label, d.confidence)).join(' ')
      : 'none';
  }
  const zones = recordingZoneNames(recording);
  const zoneRow = zones.length ? `<div><span>Zone</span><strong>${zones.map(escapeHtml).join(', ')}</strong></div>` : '';
  const triggerRow = detections.length ? '' : `<div><span>Trigger</span><strong>${escapeHtml(recordingTriggerSummary(recording))}</strong></div>`;
  els.recordingDetails.innerHTML = `
    <div><span>Recording</span><strong><a href="/recordings?recording_id=${encodeURIComponent(recording.id)}" class="timeline-recording-link">#${escapeHtml(String(recording.id))} ↗</a></strong></div>
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
  els.videoModalClose.focus();
}

function closeVideoModal() {
  els.videoModal.hidden = true;
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
    // <video>.play() media error (never an api() throw) - redirect guard skipped by design.
    if (['AbortError', 'NotAllowedError'].includes(error?.name)) {
      els.clipPlayerStatus.textContent = `Recording #${recording.id} loaded.`;
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

  // Partition the filtered set so each card sees only its own slice.
  // isSoundRecording() is the canonical predicate - any sound-detected
  // clip and any clip whose primary label is a sound class routes to
  // the Sounds card; everything else (motion + detected objects) lands
  // in the Objects card. renderLegend() is intentionally not called
  // here: the cards themselves now split the day visually, so a
  // standalone eyebrow row above them would be redundant. The function
  // stays defined so tests/test_timeline_legend_js.test.js keeps
  // passing unchanged.
  const objectRecordings = recordings.filter((r) => !isSoundRecording(r));
  const soundRecordings = recordings.filter((r) => isSoundRecording(r));

  TIMELINE_CARDS.forEach((card) => {
    const cardRecordings = card.kind === 'object' ? objectRecordings : soundRecordings;
    renderTimelineForCard(card, viewPayload, cardRecordings, allRecordings.length);
  });

  // Playback selection: still keyed off the global filtered set so an
  // existing play state survives a filter that hides one card.
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
  TIMELINE_CARDS.forEach((card) => {
    if (card.status) card.status.textContent = 'Loading timeline…';
    setTimelineStatusChip(card, { kind: 'idle', label: 'Loading' });
  });
  const timezoneOffsetMinutes = new Date().getTimezoneOffset();
  let payload;
  try {
    payload = await api(
      `/api/recordings/timeline?camera_id=${encodeURIComponent(cameraId)}&day=${encodeURIComponent(day)}&tz_offset_minutes=${timezoneOffsetMinutes}`,
    );
  } catch (err) {
    // Special-cases benign 'No cameras configured' inline; re-throws to outer guarded .catch().
    if (err.message === 'No cameras configured') {
      const msg = 'No cameras configured. Add a camera in Settings to use the timeline.';
      TIMELINE_CARDS.forEach((card) => {
        if (card.status) card.status.textContent = msg;
        setTimelineStatusChip(card, { kind: 'empty', label: 'No cameras' });
      });
      return;
    }
    throw err;
  }
  state.payload = payload;
  populateControls(payload);
  populateFilterOptions(payload.recordings || []);
  await renderFilteredTimeline({ preserveSelection });
}

async function loadAuth() {
  // nav.js kicks off the shared /api/auth/me at script load; awaiting the
  // shared daygleAuthReady promise here means this page never issues its
  // own duplicate /api/auth/me on bootstrap. Date/time display preferences
  // are also set globally by nav.js from that response.
  await window.daygleAuthReady;
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
          if (rule.enabled !== false && (rule.email_enabled === true || rule.push_enabled === true || rule.record_on_detect !== false)) {
            const label = String(rule.label || '').trim().toLowerCase();
            setMin(label, Number(rule.min_confidence ?? 0.5));
          }
        }
      }
    }
    configuredLabels = labels;
  } catch {
    // Show all labels if settings are unavailable.
  }
}

els.timelineLoadBtn.addEventListener('click', () => {
  loadTimeline({ preserveSelection: false }).catch((error) => {
    // Skip UI updates if api() triggered a 401 redirect
    if (window.daygleAuth?.redirecting) return;
    reportTimelineError(error.message);
  });
});

// "Now" shortcut: set Day=today, From=00:00, To=current local time, then reload.
// setTimeSelectValue (utils.js) snaps minutes to the nearest 5-min step
// on assignment, so passing the live "now" clock value (e.g. 14:23) still
// lands on a value the picker can represent (14:25).
els.timelineNowBtn?.addEventListener('click', () => {
  const now = new Date();
  const today = new Date(now.getTime() - now.getTimezoneOffset() * 60000).toISOString().slice(0, 10);
  els.timelineDate.value = today;
  setTimeSelectValue(els.fromTime, '00:00');
  const hh = String(now.getHours()).padStart(2, '0');
  const mm = String(now.getMinutes()).padStart(2, '0');
  setTimeSelectValue(els.toTime, `${hh}:${mm}`);
  loadTimeline({ preserveSelection: false }).catch((error) => {
    // Skip UI updates if api() triggered a 401 redirect
    if (window.daygleAuth?.redirecting) return;
    reportTimelineError(error.message);
  });
});

els.cameraSelect.addEventListener('change', () => {
  loadTimeline({ preserveSelection: false }).catch((error) => {
    // Skip UI updates if api() triggered a 401 redirect
    if (window.daygleAuth?.redirecting) return;
    reportTimelineError(error.message);
  });
});

els.timelineDate.addEventListener('change', () => {
  loadTimeline({ preserveSelection: false }).catch((error) => {
    // Skip UI updates if api() triggered a 401 redirect
    if (window.daygleAuth?.redirecting) return;
    reportTimelineError(error.message);
  });
});

els.filterSelect.addEventListener('change', () => {
  renderFilteredTimeline({ preserveSelection: true }).catch((error) => {
    // Skip UI updates if api() triggered a 401 redirect
    if (window.daygleAuth?.redirecting) return;
    reportTimelineError(error.message);
  });
});

// Debounce the From/To time-picker change handler. The picker renders as
// three separate <select> elements (hour / minute / AM/PM in 12h mode),
// each firing its own change event - so a user who adjusts both hour and
// minute triggers two roundtrips if we re-fire the timeline fetch on every
// event. Coalesce consecutive edits within a 150ms window into a single
// /api/recordings/timeline refresh; still reactive enough to feel instant
// while editing one field at a time.
//
// Attach the listeners to the outer mount spans (not the inner
// .time-select-wrap). renderTimelineTimeSelect replaces innerHTML on
// every re-render - including the daygleDatePrefsChanged 24h <-> 12h+AM/PM
// format-swap path - so a listener attached to the wrap dies with the
// old wrap. The mount spans aren't recreated, so they reliably catch
// change events from inner <select>s (via DOM bubbling) on every render.
let timelineTimeChangeTimer = null;
function scheduleTimelineTimeRefresh() {
  if (timelineTimeChangeTimer !== null) clearTimeout(timelineTimeChangeTimer);
  timelineTimeChangeTimer = setTimeout(() => {
    timelineTimeChangeTimer = null;
    renderFilteredTimeline({ preserveSelection: true }).catch((error) => {
      // Skip UI updates if api() triggered a 401 redirect
      if (window.daygleAuth?.redirecting) return;
      reportTimelineError(error.message);
    });
  }, 150);
}

document.getElementById('timelineFromTimeMount').addEventListener('change', scheduleTimelineTimeRefresh);

document.getElementById('timelineToTimeMount').addEventListener('change', scheduleTimelineTimeRefresh);

// Row-level click delegation, fanned out across both .timeline-rows
// elements. A single delegated handler per card is enough because the
// segment markup (data-recording-id) stays the same on each card - the
// only thing that changes is which recordings live on which track.
TIMELINE_CARDS.forEach((card) => {
  if (!card.rows) return;
  card.rows.addEventListener('click', (event) => {
    const button = event.target.closest('[data-recording-id]');
    if (!button) return;
    playRecording(button.dataset.recordingId).catch((error) => {
      // Skip UI updates if api() triggered a 401 redirect
      if (window.daygleAuth?.redirecting) return;
      els.clipPlayerStatus.textContent = error.message;
    });
  });
});

els.videoModalClose.addEventListener('click', () => clearPlayback());


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

// timeupdate is intentionally omitted - the requestVideoFrameCallback/rAF loop
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
  const savedValue = localStorage.getItem(TIMELINE_OVERLAY_TOGGLE_KEY);
  overlayEnabled = savedValue !== '0';
  els.clipOverlayToggle.checked = overlayEnabled;
  els.clipOverlayToggle.addEventListener('change', () => {
    overlayEnabled = Boolean(els.clipOverlayToggle.checked);
    localStorage.setItem(TIMELINE_OVERLAY_TOGGLE_KEY, overlayEnabled ? '1' : '0');
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
  if (queryFromTime) setTimeSelectValue(els.fromTime, queryFromTime);
  if (queryToTime) setTimeSelectValue(els.toTime, queryToTime);
  TIMELINE_CARDS.forEach((card) => setTimelineStatusChip(card, { kind: 'idle', label: 'Loading' }));
  await loadConfiguredLabels();
  await loadTimeline({ preserveSelection: true });
}).catch((error) => {
  // Skip UI updates if api() triggered a 401 redirect
  if (window.daygleAuth?.redirecting) return;
  els.clipPlayerStatus.textContent = error.message;
  reportTimelineError(error.message);
});

// Re-render the timeline (ticks, segments, list, modal) when the user's
// date_format / time_format changes in another tab. Preserves the
// currently selected camera / day / filter / time range so the user keeps
// what they were looking at - only the rendered formatting changes. The
// From/To time pickers also need to swap between 24h and 12h+AM/PM, so
// they're re-rendered here too (translation handled by renderTimeSelect
// reading selH/selM so a 14:30 selection in 24h mode becomes "2:30 PM"
// in 12h mode instead of snapping back to the defaults).
window.daygleDatePrefsChanged = function daygleDatePrefsChanged() {
  const preservedFrom = els.fromTime ? timeSelectValue(els.fromTime) : TIMELINE_FILTER_TIME_FROM_DEFAULT;
  const preservedTo = els.toTime ? timeSelectValue(els.toTime) : TIMELINE_FILTER_TIME_TO_DEFAULT;
  els.fromTime = renderTimelineTimeSelect('timelineFromTimeMount', preservedFrom || TIMELINE_FILTER_TIME_FROM_DEFAULT);
  els.toTime = renderTimelineTimeSelect('timelineToTimeMount', preservedTo || TIMELINE_FILTER_TIME_TO_DEFAULT);
  if (typeof loadTimeline !== 'function' || !state || !state.payload) return;
  loadTimeline({ preserveSelection: true }).catch((error) => {
    // Skip UI updates if api() triggered a 401 redirect
    if (window.daygleAuth?.redirecting) return;
    reportTimelineError(error.message);
  });
};
