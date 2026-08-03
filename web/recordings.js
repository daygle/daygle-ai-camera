const els = {
  recordings: document.getElementById('recordings'),
  cameraFilter: document.getElementById('cameraFilter'),
  recordingDateFrom: document.getElementById('recordingDateFrom'),
  recordingTimeFrom: null, // populated by renderFilterTimeSelects() below
  recordingDateTo: document.getElementById('recordingDateTo'),
  recordingTimeTo: null,   // populated by renderFilterTimeSelects() below
  recordingSort: document.getElementById('recordingSort'),
  recordingSearchBtn: document.getElementById('recordingSearchBtn'),
  recordingClearBtn: document.getElementById('recordingClearBtn'),
  filterForm: document.getElementById('recordingsFilterForm'),
  clipPlayer: document.getElementById('clipPlayer'),
  clipPlayerStatus: document.getElementById('clipPlayerStatus'),
  recordingDetails: document.getElementById('recordingDetails'),
  deleteAllRecordingsBtn: document.getElementById('deleteAllRecordingsBtn'),
  clipOverlay: document.getElementById('clipOverlay'),
  clipOverlayToggle: document.getElementById('clipOverlayToggle'),
  clipPlayerCard: document.getElementById('clipPlayerCard'),
  clipPlayerTitle: document.getElementById('clipPlayerTitle'),
  clipPlayerClose: document.getElementById('clipPlayerClose'),
  clipTimeline: document.getElementById('clipTimeline'),
  clipTimelineBar: document.getElementById('clipTimelineBar'),
  clipTimelineLegend: document.getElementById('clipTimelineLegend'),
  // Download button and subtitle live in the inline player card.
  videoModalDownload: document.getElementById('videoModalDownload'),
  videoModalSubtitle: document.getElementById('videoModalSubtitle'),
  listStatus: document.getElementById('listStatus'),
  statTotalClips: document.getElementById('statTotalClips'),
  statTotalDuration: document.getElementById('statTotalDuration'),
  statCameraCount: document.getElementById('statCameraCount'),
  statFilterStatus: document.getElementById('statFilterStatus'),
  statFilterHint: document.getElementById('statFilterHint'),
  // Label filter select
  labelFilter: document.getElementById('labelFilter'),
};

// CSRF token and current user live on window.daygleAuth set via
// setApiAuth() (loaded from web/utils.js). Date/time display preferences are
// also global (window.daygleDatePrefs) and are populated by nav.js from
// /api/auth/me - no page-local state to maintain.
let recordingRefreshTimer = null;
let activeRecording = null;
let overlayResizeObserver = null;
// Estimated frame duration (seconds) derived from the video element, used
// to project detection boxes one frame ahead of the VFC mediaTime.
let _frameDuration = 1 / 30; // default 30fps, updated on each VFC frame
// RECORDINGS_OVERLAY_TOGGLE_KEY (and its siblings TIMELINE_OVERLAY_TOGGLE_KEY
// and LIVE_AI_TRACK_KEY) now live in web/utils.js - exposed on window.daygleUi
// and visible as bare global constants on every page. Keeping the lookup as a
// bare name here so call sites read the same way as before.
// On by default; users can turn it off per-browser via the toggle.
let overlayEnabled = true;
// GENERIC_TRIGGER_LABELS lives in web/utils.js (loaded before this script);
// the bare name resolves via the shared realm so recordings.js, the timeline
// and the dashboard activity feed all agree on what counts as a non-concrete
// trigger word (motion / alert / human / object / none / off / continuous).

function filterByConfiguredLabels(detections) {
  if (!configuredLabels) return detections;
  return detections.filter((d) => {
    const label = String(d.label || '').trim().toLowerCase();
    return configuredLabels.has(label) || configuredLabels.has('motion') && label === 'motion';
  });
}
let overlayRafId = null;
let overlayVfcHandle = null;
let configuredLabels = null; // null = no filter loaded yet

// api() is provided by web/utils.js (loaded before this script) - it reads
// the CSRF token from window.daygleAuth.csrfToken and handles 401 redirects
// so every page shares identical auth and error semantics.

// detectionPill(), motionPill(), isSoundLabel(), SOUND_CLASS_IDS,
// DETECTION_EYE_ICON, DETECTION_MOTION_ICON, MOTION_RUNNING_ROW_ICON and
// GENERIC_TRIGGER_LABELS now live in web/utils.js (loaded before this
// script) so the same rendering is shared with the dashboard and the
// timeline page. Keeping only the local helpers that are specific to this
// page (e.g. recording-selection logic).

// A recording is "motion-only" when:
//  * it isn't a sound recording (sound already has its own visual treatment),
//  * no concrete object labels were detected during the clip (the join-table
//    labels + per-event detections are both empty once generic trigger words
//    are stripped), and
//  * the trigger type wasn't the always-on / disabled placeholders
//    ('continuous', 'none', 'off') so we don't accidentally label
//    always-on clips as motion recordings.  // isMotionOnlyRecording + motionConfidenceFor live in web/utils.js so the
  // recordings list, the timeline page and
  // the dashboard activity feed all share the same boundary.

// cameraLabel() is provided by web/utils.js (loaded before this script).
// The unified version handles both cameraLabel(recording) and
// cameraLabel(cameraName, cameraId) calling conventions.
// The yamnet-tflite page keeps its own local cameraLabel(camera) because
// it receives camera config objects (not recordings) with a different shape.

function recordingDetectionLabels(recording) {
  // Prefer the server-side `labels` array (one row per unique object detected
  // inside the recording, joined via recording_labels). Fall back to deriving
  // from the per-event detections when the join table is empty (e.g. very old
  // recordings that pre-date the multi-label upgrade).
  if (Array.isArray(recording.labels) && recording.labels.length) {
    return recording.labels
      .map((label) => String(label || '').trim().toLowerCase())
      .filter((label) => label && !GENERIC_TRIGGER_LABELS.has(label));
  }
  const all = Array.from(new Set((recording.detections || [])
    .filter((d) => {
      const label = String(d.label || '').trim().toLowerCase();
      if (!label) return false;
      if (!configuredLabels) return true;
      return configuredLabels.has(label) && Number(d.confidence || 0) >= (configuredLabels.get(label) ?? 0);
    })
    .map((d) => String(d.label || '').trim().toLowerCase())));
  const specific = all.filter((label) => !GENERIC_TRIGGER_LABELS.has(label));
  return specific.length ? specific : all;
}

function recordingDisplayTrigger(recording) {
  if (isSoundRecording(recording)) {
    const meta = recording.event?.metadata || {};
    const classLabel = meta.class_label || meta.label || recording.trigger_label || 'sound';
    return titleCase(classLabel);
  }

  const triggerType = recordingTriggerType(recording);
  const triggerLabel = recordingTriggerLabel(recording);
  const detectionLabels = recordingDetectionLabels(recording);
  const hasDetections = detectionLabels.length > 0;

  if (triggerType === 'motion' || triggerType === 'alert' || triggerType === 'human' || triggerType === 'object') {
    // Show ALL concrete object labels joined by · on the pill (e.g. "Person · Cat · Dog").
    if (detectionLabels.length) {
      return detectionLabels.map((label) => titleCase(label)).join(' · ');
    }
    // If detections exist and none are specific, trust the detection set and keep this as motion.
    if (!hasDetections && triggerLabel && !GENERIC_TRIGGER_LABELS.has(triggerLabel)) return `${triggerType} · ${triggerLabel}`;
    return triggerType;
  }

  if (triggerType === 'continuous' || triggerType === 'none' || triggerType === 'off') {
    return triggerType;
  }

  if (triggerLabel && triggerLabel !== triggerType) return `${triggerType} · ${triggerLabel}`;
  return triggerLabel || triggerType;
}

function formatDurationShort(totalSeconds) {
  const seconds = Math.max(0, Math.round(Number(totalSeconds) || 0));
  if (seconds < 60) return `${seconds}s`;
  const minutes = Math.floor(seconds / 60);
  const remSeconds = seconds % 60;
  if (minutes < 60) return `${minutes}m ${remSeconds}s`;
  const hours = Math.floor(minutes / 60);
  const remMinutes = minutes % 60;
  return remMinutes ? `${hours}h ${remMinutes}m` : `${hours}h`;
}

function updateFilterStat(label, hint) {
  if (!els.statFilterStatus || !els.statFilterHint) return;
  els.statFilterStatus.textContent = label;
  els.statFilterHint.textContent = hint;
}

function parseFilterTimeParts(timeString, fallbackHour, fallbackMinute, fallbackSecond = 0, fallbackMillisecond = 0) {
  const match = String(timeString || '').match(/^(\d{1,2}):(\d{2})$/);
  if (!match) {
    return {
      hour: fallbackHour,
      minute: fallbackMinute,
      second: fallbackSecond,
      millisecond: fallbackMillisecond,
    };
  }
  const hour = Math.min(23, Math.max(0, Number.parseInt(match[1], 10) || 0));
  const minute = Math.min(59, Math.max(0, Number.parseInt(match[2], 10) || 0));
  return { hour, minute, second: fallbackSecond, millisecond: fallbackMillisecond };
}

function formatIsoDateForFilter(dateString, endOfDay = false, timeString = '') {
  if (!dateString) return '';
  // The browser returns YYYY-MM-DD without a timezone. Anchor from/to bounds
  // in local time so the filter feels intuitive. When a time is provided, it
  // refines the selected date into an exact local datetime boundary.
  const [year, month, day] = dateString.split('-').map((part) => Number.parseInt(part, 10));
  if (!year || !month || !day) return '';
  const fallback = endOfDay
    ? parseFilterTimeParts(timeString, 23, 59, 59, 999)
    : parseFilterTimeParts(timeString, 0, 0);
  const date = new Date(year, month - 1, day, fallback.hour, fallback.minute, fallback.second, fallback.millisecond);
  return date.toISOString();
}

// ── Filter state & pickers ────────────────────────────────────────────────
// Mount spans in the filter form render through the shared `renderTimeSelect`
// helper (web/utils.js) so the From / To time pickers follow the user's
// Profile > Time Format choice (12h with AM/PM vs. 24h), matching the same
// UX on the /timeline page. Re-rendered on init, on Reset Filters, and
// whenever the cross-tab prefs hook fires so a profile change instantly
// swaps the picker style without a manual refresh.
const FILTER_TIME_FROM_DEFAULT = '00:00';
// Minute resolution is 5 minutes (shared with the timeline + /sounds and
// /zones rule editors), so 23:55 is the latest valid value that still
// pins against the end of the day.
const FILTER_TIME_TO_DEFAULT = '23:55';

function renderFilterTimeSelect(mountId, defaultValue) {
  const mount = document.getElementById(mountId);
  if (!mount) return null;
  const role = mount.dataset.timeRole || '';
  mount.innerHTML = renderTimeSelect(defaultValue, 'data-filter-time-role', role);
  return mount.querySelector('.time-select-wrap');
}

function renderFilterTimeSelects() {
  els.recordingTimeFrom = renderFilterTimeSelect('recordingTimeFromMount', FILTER_TIME_FROM_DEFAULT);
  els.recordingTimeTo = renderFilterTimeSelect('recordingTimeToMount', FILTER_TIME_TO_DEFAULT);
}

renderFilterTimeSelects();

// ── Label filter state ────────────────────────────────────────────

function currentFilterValues() {
  return {
    label: els.labelFilter?.value || '',
    cameraId: els.cameraFilter?.value || '',
    dateFrom: els.recordingDateFrom?.value || '',
    // Read from the custom hour/minute (/AM/PM) selects so the filter value
    // always matches what the user sees in the picker rather than the
    // browser-native `<input type="time">` element which rendered in the
    // viewer's locale (often 12-hour even when 24h is preferred).
    timeFrom: timeSelectValue(els.recordingTimeFrom) || FILTER_TIME_FROM_DEFAULT,
    dateTo: els.recordingDateTo?.value || '',
    timeTo: timeSelectValue(els.recordingTimeTo) || FILTER_TIME_TO_DEFAULT,
    sort: els.recordingSort?.value || 'newest',
  };
}

function describeFilters(filters) {
  const parts = [];
  if (filters.label) {
    const option = els.labelFilter?.querySelector(`option[value="${escapeHtml(filters.label)}"]`);
    parts.push(`label “${option?.textContent || filters.label}”`);
  }
  if (filters.cameraId) {
    const cameraOption = Array.from(els.cameraFilter?.options || []).find((o) => o.value === filters.cameraId);
    parts.push(`camera “${cameraOption?.textContent || filters.cameraId}”`);
  }
  if (filters.dateFrom) parts.push(`from ${formatUserDate(filters.dateFrom)} ${filters.timeFrom || FILTER_TIME_FROM_DEFAULT}`);
  if (filters.dateTo) parts.push(`through ${formatUserDate(filters.dateTo)} ${filters.timeTo || FILTER_TIME_TO_DEFAULT}`);
  return parts;
}

function renderStats(recordings) {
  if (els.statTotalClips) els.statTotalClips.textContent = String(recordings.length);
  if (els.statTotalDuration) {
    const totalSeconds = recordings.reduce((sum, rec) => sum + (Number(rec.duration_seconds) || 0), 0);
    els.statTotalDuration.textContent = formatDurationShort(totalSeconds);
  }
  if (els.statCameraCount) {
    const cameras = new Set(recordings.map((rec) => cameraLabel(rec)).filter(Boolean));
    els.statCameraCount.textContent = String(cameras.size);
  }
}

// ── Click-to-sort column headers ─────────────────────────────────────────
// Headers re-order the currently loaded list client-side. `null` means the
// server order from the Sort By select applies; clicking a column cycles
// asc → desc → back to the server default. The sort survives filter changes
// and the preparing-clip auto-refresh, and clears when the user explicitly
// changes the Sort By select (server-side newest/oldest).
let recordingsSortState = null;
let currentRecordings = [];

function recordingSortValue(recording, key) {
  switch (key) {
    case 'type': {
      if (isSoundRecording(recording)) return 2;
      return isMotionOnlyRecording(recording) ? 1 : 0;
    }
    case 'camera': return String(cameraLabel(recording) || '').toLowerCase();
    case 'detections': {
      if (isMotionOnlyRecording(recording)) return 0;
      return recordingDetectionSummary(recording).length;
    }
    case 'zone': {
      const zones = recordingZoneNames(recording);
      return String(zones[0] || '').toLowerCase();
    }
    case 'when': return Date.parse(recording.started_at) || 0;
    case 'duration': return Number(recording.duration_seconds) || 0;
    default: return 0;
  }
}

function compareRecordings(left, right) {
  if (!recordingsSortState) return 0;
  const leftValue = recordingSortValue(left, recordingsSortState.key);
  const rightValue = recordingSortValue(right, recordingsSortState.key);
  let result;
  if (typeof leftValue === 'number' && typeof rightValue === 'number') {
    result = leftValue - rightValue;
  } else {
    result = String(leftValue).localeCompare(String(rightValue), undefined, { numeric: true, sensitivity: 'base' });
  }
  return recordingsSortState.dir === 'asc' ? result : -result;
}

function renderSortHeader(label, key) {
  const active = recordingsSortState && recordingsSortState.key === key;
  const ariaSort = active ? (recordingsSortState.dir === 'asc' ? 'ascending' : 'descending') : 'none';
  const glyph = active ? (recordingsSortState.dir === 'asc' ? '▲' : '▼') : '⇅';
  const cls = active ? 'table-sort-btn is-active' : 'table-sort-btn';
  return `<th scope="col" aria-sort="${ariaSort}"><button type="button" class="${cls}" data-sort-key="${key}" aria-label="Sort by ${label}">${label}<span class="table-sort-glyph" aria-hidden="true">${glyph}</span></button></th>`;
}

function bindSortHeaders() {
  document.querySelectorAll('#recordings [data-sort-key]').forEach((button) => {
    button.addEventListener('click', () => {
      const key = button.dataset.sortKey;
      if (recordingsSortState && recordingsSortState.key === key) {
        recordingsSortState = recordingsSortState.dir === 'asc'
          ? { key, dir: 'desc' }
          : null;
      } else {
        // Date columns read newest-first by default so a single click lands on
        // the familiar order; every other column starts ascending.
        recordingsSortState = { key, dir: key === 'when' ? 'desc' : 'asc' };
      }
      renderRecordings(currentRecordings);
    });
  });
}

function renderRecordings(recordings) {
  currentRecordings = recordings;
  renderStats(recordings);
  if (!recordings.length) {
    els.recordings.innerHTML = `
      <div class="recordings-empty-state">
        <div class="recordings-empty-icon" aria-hidden="true">
          <svg width="40" height="40" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><polygon points="23 7 16 12 23 17 23 7"/><rect x="1" y="5" width="15" height="14" rx="2" ry="2"/></svg>
        </div>
        <h2>No recordings match the current filters</h2>
        <p class="muted">Try resetting the filters, or wait for a new event to be captured.</p>
      </div>`;
    return;
  }
  const ordered = recordingsSortState
    ? recordings.slice().sort(compareRecordings)
    : recordings;
  els.recordings.innerHTML = '<div class="cameras-table-wrap"><table class="rule-table activity-table">' +
    '<thead><tr>' +
      renderSortHeader('Type', 'type') +
      renderSortHeader('Camera', 'camera') +
      renderSortHeader('Detections', 'detections') +
      renderSortHeader('Zone', 'zone') +
      renderSortHeader('When', 'when') +
      renderSortHeader('Duration', 'duration') +
      '<th class="cell-center" scope="col">Actions</th>' +
    '</tr></thead>' +
    '<tbody>' + ordered.map((recording) => {
    const mediaReady = recording.media_ready !== false;
    const isSound = isSoundRecording(recording);
    const isMotion = isMotionOnlyRecording(recording);
    const typeClass = isSound ? 'activity-item-sound' : isMotion ? 'activity-item-motion' : 'activity-item-event';
    const typeLabel = isSound ? 'Sound Recording' : isMotion ? 'Motion Recording' : 'Object Recording';
    const zones = recordingZoneNames(recording);
    const zoneCell = zones.length ? zones.map(escapeHtml).join(', ') : '-';
    const durationText = `${Number(recording.duration_seconds || 0).toFixed(1)}s`;
    const durationCell = mediaReady
      ? `<span class="recording-duration">${escapeHtml(durationText)}</span>`
      : '<span class="muted">Preparing...</span>';
    let badges;
    if (isMotion) {
      // Motion-only clips have no concrete object labels - show a single
      // teal "Motion · NN%" pill so the row reads distinctly from object
      // and sound recordings without falling back to "No detections".
      badges = motionPill(motionConfidenceFor(recording));
    } else {
      badges = recordingDetectionSummary(recording).map((d) => detectionPill(d.label, d.confidence, isSound)).join('') || '<span class="muted">No detections</span>';
    }
    const actions = [
      mediaReady
        ? `<button class="secondary activity-item-action" data-play-recording="${recording.id}" type="button" aria-label="Play recording #${recording.id}"><svg width="14" height="14" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true"><polygon points="6 4 20 12 6 20 6 4"/></svg><span class="activity-action-label">Play</span></button>`
        : '<button class="secondary activity-item-action" disabled aria-label="Preparing recording"><svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-linecap="round" aria-hidden="true"><circle cx="12" cy="12" r="10"/><polyline points="12 6 12 12 16 14"/></svg><span class="activity-action-label">Preparing...</span></button>',
      `<button class="secondary delete-btn activity-item-action" data-delete-recording="${recording.id}" type="button" aria-label="Delete recording #${recording.id}"><svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><polyline points="3 6 5 6 21 6"/><path d="M19 6l-2 14a2 2 0 0 1-2 2H9a2 2 0 0 1-2-2L5 6"/><path d="M10 11v6M14 11v6"/></svg><span class="activity-action-label">Delete</span></button>`,
    ];
    return `
      <tr class="activity-table-row ${typeClass}" data-recording-row="${recording.id}">
        <td class="activity-cell-type"><span class="activity-item-type">${typeLabel}</span><span class="activity-cell-ref">Recording #${recording.id}</span></td>
        <td class="activity-cell-camera">${escapeHtml(cameraLabel(recording))}</td>
        <td class="activity-cell-detections"><div class="activity-item-badges">${badges}</div></td>
        <td class="activity-cell-zone">${zoneCell}</td>
        <td class="activity-cell-when">
          <div class="activity-item-when">
            <div class="activity-item-when-relative">
              <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="12" cy="12" r="10"/><polyline points="12 6 12 12 16 14"/></svg>
              <span>${escapeHtml(timeAgo(recording.started_at))}</span>
            </div>
            <span class="activity-item-when-absolute">${escapeHtml(formatDateTime(recording.started_at))}</span>
          </div>
        </td>
        <td class="activity-cell-duration">${durationCell}</td>
        <td class="activity-cell-actions"><div class="cell-actions">${actions.join('')}</div></td>
      </tr>
    `;
  }).join('') + '</tbody></table></div>';
  if (recordings.some((recording) => recording.media_ready === false)) {
    clearTimeout(recordingRefreshTimer);
    recordingRefreshTimer = setTimeout(() => loadRecordings(), 3000);
  } else {
    clearTimeout(recordingRefreshTimer);
    recordingRefreshTimer = null;
  }
  bindRecordingButtons();
  bindSortHeaders();
}

function renderRecordingDetails(recording) {
  const detections = recordingDetectionSummary(recording);
  const isSound = isSoundRecording(recording);
  const isMotionOnly = isMotionOnlyRecording(recording);
  // The \"Sound\" / \"Motion\" / \"Detections\" label tracks the source the row on
  // the list uses, so opening a clip never surprises users with a different
  // category name. Motion-only clips render the teal motion pill (with the
  // strongest motion intensity confidence for the clip) rather than the
  // bare \"none\" placeholder the row used to show.
  let detectionBadges;
  let detectionLabel;
  if (isMotionOnly) {
    detectionLabel = 'Motion';
    detectionBadges = motionPill(motionConfidenceFor(recording));
  } else if (isSound) {
    detectionLabel = 'Sound';
    const soundDetections = recordingDetectionSummary(recording);
    detectionBadges = soundDetections.length
      ? soundDetections.map((d) => detectionPill(d.label, d.confidence, true)).join(' ')
      : 'none';
  } else {
    detectionLabel = 'Detections';
    detectionBadges = detections.length
      ? detections.map((d) => detectionPill(d.label, d.confidence)).join(' ')
      : 'none';
  }
  const zones = recordingZoneNames(recording);
  // Build the label/value rows via the shared ``safeHtml`` tagged template
  // (web/utils.js). Each row passes the value through ``escapeHtml`` while
  // leaving the literal markup intact, so server-supplied fields (camera
  // name, trigger, started_at, etc.) can't smuggle HTML into the DOM.
  const zoneRow = zones.length
    ? safeHtml`<div><span>Zone</span><strong>${zones.join(', ')}</strong></div>`
    : '';
  const detailRows = [
    safeHtml`<div><span>Recording</span><strong>#${recording.id}</strong></div>`,
    safeHtml`<div><span>Event</span><strong>${recording.event_id || 'none'}</strong></div>`,
    safeHtml`<div><span>Camera</span><strong>${cameraLabel(recording)}</strong></div>`,
    zoneRow,
    safeHtml`<div><span>Trigger</span><strong>${recordingDisplayTrigger(recording)}</strong></div>`,
    safeHtml`<div><span>Started</span><strong>${formatDateTime(recording.started_at)}</strong></div>`,
    safeHtml`<div><span>Duration</span><strong>${Number(recording.duration_seconds || 0).toFixed(1)}s</strong></div>`,
  ].filter(Boolean);
  // ``detectionBadges`` is pre-rendered HTML markup (detectionPill / motionPill
  // / 'No detections' fallback, each already routing its user-supplied label
  // through escapeHtml internally). Route that markup through
  // ``insertAdjacentHTML`` rather than ``safeHtml`` so the badge HTML isn't
  // re-escaped; the static label uses ``escapeHtml`` on its own line so the
  // surrounding div is still built without a raw ``.innerHTML`` template.
  els.recordingDetails.innerHTML = detailRows.join('');
  els.recordingDetails.insertAdjacentHTML(
    'beforeend',
    `<div class="wide"><span>${escapeHtml(detectionLabel)}</span><strong class="recording-detail-detections">${detectionBadges}</strong></div>`,
  );
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
  const hasSpecificEvent = allEventDetections.some((d) => !GENERIC_TRIGGER_LABELS.has(String(d.label || '').toLowerCase()));
  const eventDetections = filterByConfiguredLabels(
    hasSpecificEvent
      ? allEventDetections.filter((d) => !GENERIC_TRIGGER_LABELS.has(String(d.label || '').toLowerCase()))
      : allEventDetections
  );
  if (!eventDetections.length) return;
  drawDetectionBoxesOnCanvas(els.clipOverlay, eventDetections, els.clipPlayer);
}

// ── Clip segment timeline ───────────────────────────────────────────────────
// A compact bar under the player that shows the clip's pre-roll, the event span,
// and the post-motion tail  -  the recording settings "in action" on real footage.
// Boundaries come from the detection track (real detection times), not the
// configured pre/post values, so the bar reflects what this clip actually
// captured (including a truncated pre-roll).

function clipAuthoritativeDuration() {
  const videoDuration = Number(els.clipPlayer?.duration);
  if (Number.isFinite(videoDuration) && videoDuration > 0) return videoDuration;
  const metaDuration = Number(activeRecording?.duration_seconds);
  return Number.isFinite(metaDuration) && metaDuration > 0 ? metaDuration : 0;
}

// First and last playback times (seconds) where the track localized a detection.
function clipEventBounds(track) {
  if (!Array.isArray(track) || !track.length) return null;
  let first = null;
  let last = null;
  for (const sample of track) {
    if (!sample || !Array.isArray(sample.detections) || !sample.detections.length) continue;
    const t = Number(sample.t);
    if (!Number.isFinite(t) || t < 0) continue;
    if (first === null) first = t;
    last = t;
  }
  return first === null ? null : { first, last };
}

function fmtClipSeconds(seconds) {
  const s = Math.max(0, Number(seconds) || 0);
  if (s < 60) return `${s.toFixed(s < 10 ? 1 : 0)}s`;
  return `${Math.floor(s / 60)}:${String(Math.round(s % 60)).padStart(2, '0')}`;
}

function resetClipTimeline() {
  if (!els.clipTimeline) return;
  els.clipTimeline.hidden = true;
  if (els.clipTimelineBar) els.clipTimelineBar.innerHTML = '';
  if (els.clipTimelineLegend) els.clipTimelineLegend.innerHTML = '';
}

function renderClipTimeline() {
  if (!els.clipTimeline || !els.clipTimelineBar) return;
  const duration = clipAuthoritativeDuration();
  const track = Array.isArray(activeRecording?.track) ? activeRecording.track : null;
  const bounds = clipEventBounds(track);
  // Only annotate event clips whose track actually located the event. Continuous
  // recordings and clips with no localized detections get no segmentation.
  if (!duration || !bounds) {
    resetClipTimeline();
    return;
  }
  const first = Math.max(0, Math.min(bounds.first, duration));
  const last = Math.min(Math.max(bounds.last, first), duration);
  const pct = (value) => `${Math.max(0, Math.min(100, (value / duration) * 100))}%`;

  const segments = [
    { cls: 'pre', label: 'Pre-roll', start: 0, end: first },
    { cls: 'event', label: 'Event', start: first, end: last },
    { cls: 'tail', label: 'Tail', start: last, end: duration },
  ];
  els.clipTimelineBar.innerHTML = '';
  for (const seg of segments) {
    const span = seg.end - seg.start;
    if (span <= 0.05) continue;
    const div = document.createElement('div');
    div.className = `clip-seg clip-seg-${seg.cls}`;
    div.style.left = pct(seg.start);
    div.style.width = pct(span);
    div.title = `${seg.label}: ${fmtClipSeconds(span)}`;
    els.clipTimelineBar.appendChild(div);
  }
  const marker = document.createElement('div');
  marker.className = 'clip-trigger-marker';
  marker.style.left = pct(first);
  marker.title = `Event trigger at ${fmtClipSeconds(first)}`;
  els.clipTimelineBar.appendChild(marker);

  const playhead = document.createElement('div');
  playhead.className = 'clip-playhead';
  playhead.id = 'clipPlayhead';
  els.clipTimelineBar.appendChild(playhead);

  // Legend shows the actual measured seconds of each region for this clip.
  const legendItems = [
    { cls: 'pre', label: 'Pre-roll', secs: first },
    { cls: 'event', label: 'Event', secs: last - first },
    { cls: 'tail', label: 'Tail', secs: duration - last },
  ];
  els.clipTimelineLegend.innerHTML = '';
  for (const item of legendItems) {
    const wrap = document.createElement('span');
    wrap.className = 'clip-legend-item';
    const swatch = document.createElement('i');
    swatch.className = `clip-legend-swatch clip-seg-${item.cls}`;
    wrap.appendChild(swatch);
    wrap.appendChild(document.createTextNode(`${item.label} ${fmtClipSeconds(Math.max(0, item.secs))}`));
    els.clipTimelineLegend.appendChild(wrap);
  }
  els.clipTimeline.hidden = false;
  els.clipTimelineBar.setAttribute('aria-valuemax', duration.toFixed(1));
  updateClipTimelinePlayhead();
}

function updateClipTimelinePlayhead() {
  if (!els.clipTimeline || els.clipTimeline.hidden) return;
  const duration = clipAuthoritativeDuration();
  if (!duration) return;
  const playhead = document.getElementById('clipPlayhead');
  if (!playhead) return;
  const current = Number(els.clipPlayer?.currentTime) || 0;
  playhead.style.left = `${Math.max(0, Math.min(100, (current / duration) * 100))}%`;
  els.clipTimelineBar?.setAttribute('aria-valuenow', current.toFixed(1));
}

function seekClipFromClientX(clientX) {
  if (!els.clipTimelineBar || !els.clipPlayer) return;
  const duration = clipAuthoritativeDuration();
  if (!duration) return;
  const rect = els.clipTimelineBar.getBoundingClientRect();
  if (rect.width <= 0) return;
  const fraction = Math.max(0, Math.min(1, (clientX - rect.left) / rect.width));
  try {
    els.clipPlayer.currentTime = fraction * duration;
  } catch (_error) {
    /* not seekable yet */
  }
  updateClipTimelinePlayhead();
}

function nudgeClipTime(deltaSeconds) {
  if (!els.clipPlayer) return;
  const duration = clipAuthoritativeDuration();
  if (!duration) return;
  const next = Math.max(0, Math.min(duration, (Number(els.clipPlayer.currentTime) || 0) + deltaSeconds));
  try {
    els.clipPlayer.currentTime = next;
  } catch (_error) {
    /* not seekable yet */
  }
  updateClipTimelinePlayhead();
}
// ── End clip segment timeline ───────────────────────────────────────────────

function showInlinePlayer() {
  if (els.clipPlayerCard) {
    els.clipPlayerCard.hidden = false;
    els.clipPlayerCard.scrollIntoView({ behavior: 'smooth', block: 'start' });
  }
}

function hideInlinePlayer() {
  if (els.clipPlayerCard) els.clipPlayerCard.hidden = true;
  if (els.clipPlayer) {
    els.clipPlayer.pause();
    stopOverlayRaf();
    els.clipPlayer.removeAttribute('src');
    els.clipPlayer.load();
  }
  clearClipOverlay();
  resetClipTimeline();
  activeRecording = null;
  if (els.clipPlayerStatus) els.clipPlayerStatus.textContent = '';
  if (els.recordingDetails) els.recordingDetails.innerHTML = '';
  if (els.clipPlayerTitle) els.clipPlayerTitle.textContent = 'Recording';
  if (els.videoModalSubtitle) {
    els.videoModalSubtitle.textContent = 'Watch a recording and review its detection details.';
  }
}

async function playRecording(id) {
  const recording = await api(`/api/recordings/${id}`);
  activeRecording = recording;
  renderRecordingDetails(recording);
  if (els.videoModalSubtitle) {
    const started = formatDateTime(recording.started_at);
    const camera = cameraLabel(recording);
    els.videoModalSubtitle.textContent = started
      ? `Recording from ${camera} captured ${started}.`
      : `Recording from ${camera}.`;
  }
  resetClipTimeline();
  showInlinePlayer();
  if (recording.media_ready === false) {
    clearClipOverlay();
    els.clipPlayerStatus.textContent = `Recording #${id} is still being prepared.`;
    return;
  }
  if (els.videoModalDownload) {
    els.videoModalDownload.href = `/api/recordings/${id}/download`;
    els.videoModalDownload.hidden = false;
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
    // Successful playback is self-evident from the native video controls;
    // reserve this line for preparation, loading, and error feedback.
    els.clipPlayerStatus.textContent = '';
  } catch (error) {
    // <video>.play() media error (never an api() throw) - redirect guard skipped by design.
    if (['AbortError', 'NotAllowedError'].includes(error?.name)) {
      els.clipPlayerStatus.textContent = `Recording #${id} loaded.`;
      return;
    }
    els.clipPlayerStatus.textContent = `Unable to play recording #${id}: ${error?.message || 'media playback failed'}.`;
  }
}

function bindRecordingButtons() {
  // Inline play buttons: open the clip player above the library card.
  document.querySelectorAll('[data-play-recording]').forEach((button) => {
    button.addEventListener('click', (event) => {
      event.preventDefault();
      const id = button.dataset.playRecording;
      if (id) playRecording(id);
    });
  });
  document.querySelectorAll('[data-delete-recording]').forEach((button) => {
    button.addEventListener('click', async () => {
      const id = button.dataset.deleteRecording;
      if (!confirm(`Delete recording #${id}? This cannot be undone.`)) return;
      try {
        await api(`/api/recordings/${id}`, { method: 'DELETE' });
        window.showToast?.(`Deleted recording #${id}.`);
        await loadRecordings();
      } catch (error) {
        // Skip UI updates if api() triggered a 401 redirect
        if (window.daygleAuth?.redirecting) return;
        window.showToast?.(`Failed to delete recording: ${error.message}`, true);
      }
    });
  });
}

async function loadAuth() {
  // nav.js kicks off the shared /api/auth/me at script load and exposes
  // the resolved { user, csrfToken } on window.daygleAuth. Awaiting the
  // shared daygleAuthReady promise here means this page never issues its
  // own duplicate /api/auth/me on bootstrap.
  await window.daygleAuthReady;
  // The "Delete all" button only exists on the list page, not the dedicated
  // playback page, so guard its presence before wiring it up.
  if (window.daygleAuth.user?.role === 'admin' && els.deleteAllRecordingsBtn) {
    els.deleteAllRecordingsBtn.hidden = false;
    els.deleteAllRecordingsBtn.addEventListener('click', async () => {
      if (!confirm('Delete ALL recordings and media files? Settings, users, and rules will not be changed.')) return;
      try {
        const result = await api('/api/recordings', { method: 'DELETE' });
        await loadRecordings();
        const deletedCount = Number(result?.deleted || 0);
        window.showToast?.(`Deleted ${deletedCount} recording${deletedCount === 1 ? '' : 's'}. Settings were not changed.`);
      } catch (error) {
        // Skip UI updates if api() triggered a 401 redirect
        if (window.daygleAuth?.redirecting) return;
        window.showToast?.(`Failed to delete recordings: ${error.message}`, true);
      }
    });
  }
}

async function loadLiveSettings() {
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
  } catch (_error) {
    // Silent api() fallback (no UI mutation) - redirect guard skipped by design.
  }
}

async function loadCameras() {
  try {
    const data = await api('/api/cameras');
    const cameras = data?.cameras || [];
    if (!cameras.length || !els.cameraFilter) return;
    for (const camera of cameras) {
      const option = document.createElement('option');
      option.value = camera.id;
      option.textContent = camera.name || camera.id;
      els.cameraFilter.appendChild(option);
    }
  } catch (_error) {
    // Silent api() fallback (no UI mutation) - redirect guard skipped by design.
  }
}

async function loadRecordings(filters = {}) {
  const resolved = typeof filters === 'string' || filters instanceof String
    ? { label: String(filters), cameraId: '' }
    : { ...currentFilterValues(), ...filters };
  const params = new URLSearchParams();
  if (resolved.label) params.set('label', resolved.label);
  if (resolved.cameraId) params.set('camera_id', resolved.cameraId);
  const startedAfter = formatIsoDateForFilter(resolved.dateFrom, false, resolved.timeFrom);
  if (startedAfter) params.set('started_after', startedAfter);
  const startedBefore = formatIsoDateForFilter(resolved.dateTo, true, resolved.timeTo);
  if (startedBefore) params.set('started_before', startedBefore);
  if (resolved.sort) params.set('sort', resolved.sort);
  const queryString = params.toString();
  let recordings;
  if (resolved.label === 'motion') {
    // Backend strips generic trigger words (motion/alert/human/object/none/off/
    // continuous) from `recording.labels` so a server-side `label=motion`
    // query returns nothing. Fetch without a label filter and re-filter
    // motion-only recordings on the client so the dropdown option works.
    const draftParams = new URLSearchParams(params);
    draftParams.delete('label');
    const draftQuery = draftParams.toString();
    const all = await api(`/api/recordings${draftQuery ? `?${draftQuery}` : ''}`);
    recordings = all.filter((rec) => isMotionOnlyRecording(rec));
  } else {
    recordings = await api(`/api/recordings${queryString ? `?${queryString}` : ''}`);
  }
  const activeFilters = describeFilters(resolved);
  if (activeFilters.length) {
    updateFilterStat('Filtered', `Showing clips matching ${activeFilters.join(' and ')}.`);
  } else {
    updateFilterStat('All', 'Showing every clip');
  }
  renderRecordings(recordings);
  return recordings;
}

// Player/overlay/timeline wiring only applies where the video element exists:
// the playback page and (legacy) the list modal. The list page without a modal
// skips all of it.
if (els.clipPlayer) {
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

// Build the segment bar once the duration is known; keep its playhead synced.
els.clipPlayer.addEventListener('loadedmetadata', renderClipTimeline);
['timeupdate', 'seeked', 'play'].forEach((eventName) => {
  els.clipPlayer.addEventListener(eventName, updateClipTimelinePlayhead);
});
if (els.clipTimelineBar) {
  els.clipTimelineBar.addEventListener('click', (event) => seekClipFromClientX(event.clientX));
  els.clipTimelineBar.addEventListener('keydown', (event) => {
    const step = event.shiftKey ? 5 : 1;
    if (event.key === 'ArrowRight') {
      nudgeClipTime(step);
      event.preventDefault();
    } else if (event.key === 'ArrowLeft') {
      nudgeClipTime(-step);
      event.preventDefault();
    }
  });
}

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
  const savedValue = localStorage.getItem(RECORDINGS_OVERLAY_TOGGLE_KEY);
  overlayEnabled = savedValue !== '0';
  els.clipOverlayToggle.checked = overlayEnabled;
  els.clipOverlayToggle.addEventListener('change', () => {
    overlayEnabled = Boolean(els.clipOverlayToggle.checked);
    localStorage.setItem(RECORDINGS_OVERLAY_TOGGLE_KEY, overlayEnabled ? '1' : '0');
    if (els.clipPlayer && !els.clipPlayer.paused && overlayShouldAnimate()) {
      startOverlayRaf();
    } else if (!overlayEnabled) {
      stopOverlayRaf();
    }
    drawClipOverlay();
  });
}
} // end if (els.clipPlayer)

els.cameraFilter?.addEventListener('change', () => {
  loadRecordings().catch((error) => {
    if (window.daygleAuth?.redirecting) return;
    if (els.listStatus) els.listStatus.textContent = error.message;
  });
});
els.labelFilter?.addEventListener('change', () => {
  loadRecordings().catch((error) => {
    if (window.daygleAuth?.redirecting) return;
    if (els.listStatus) els.listStatus.textContent = error.message;
  });
});
els.filterForm?.addEventListener('submit', (event) => {
  event.preventDefault();
  loadRecordings().catch((error) => {
    if (window.daygleAuth?.redirecting) return;
    if (els.listStatus) els.listStatus.textContent = error.message;
  });
});
els.recordingSort?.addEventListener('change', () => {
  // The user picked an explicit server-side order (newest/oldest) - drop any
  // active column sort so the select's order is what the table shows.
  recordingsSortState = null;
});
els.recordingClearBtn?.addEventListener('click', () => {
  if (els.labelFilter) els.labelFilter.value = '';
  if (els.cameraFilter) els.cameraFilter.value = '';
  if (els.recordingDateFrom) els.recordingDateFrom.value = '';
  if (els.recordingDateTo) els.recordingDateTo.value = '';
  if (els.recordingSort) els.recordingSort.value = 'newest';
  recordingsSortState = null;
  // Re-render the From/To time pickers back to their defaults. Going through
  // renderFilterTimeSelects (rather than poking child selects directly) means
  // Reset Filters also handles the 12h vs 24h AM/PM swap correctly.
  renderFilterTimeSelects();
  loadRecordings().catch((error) => {
    if (window.daygleAuth?.redirecting) return;
    if (els.listStatus) els.listStatus.textContent = error.message;
  });
});

// ── Label filter options ────────────────────────────────────────────────

async function populateLabelFilterOptionsFromApi() {
  if (!els.labelFilter) return;
  try {
    // Load all recordings without any filter to populate the full label list
    const allRecordings = await api('/api/recordings?limit=500');
    populateLabelFilterOptions(allRecordings);
  } catch (_error) {
    // Silent api() fallback (no UI mutation) - redirect guard skipped by design.
  }
}

function populateLabelFilterOptions(recordings) {
  if (!els.labelFilter) return;
  const currentFilter = els.labelFilter.value || new URLSearchParams(window.location.search).get('label') || '';
  const counts = {};
  recordings.forEach((recording) => {
    recordingDetectionLabels(recording).forEach((label) => { counts[label] = (counts[label] || 0) + 1; });
  });

  const options = [{ value: '', label: `All Labels${recordings.length ? ` (${recordings.length})` : ''}` }];
  const seen = new Set(['']);
  const addOption = (value) => {
    const normalized = String(value || '').trim().toLowerCase();
    if (!normalized || seen.has(normalized)) return;
    seen.add(normalized);
    const count = counts[normalized];
    options.push({ value: normalized, label: count ? `${titleCase(normalized)} (${count})` : titleCase(normalized) });
  };

  recordings.forEach((recording) => {
    recordingDetectionLabels(recording).forEach(addOption);
  });
  if (recordings.length) addOption('motion');

  const ordered = [options[0], ...options.slice(1).sort((left, right) => {
    if (left.value === 'motion') return -1;
    if (right.value === 'motion') return 1;
    return left.label.localeCompare(right.label);
  })];
  els.labelFilter.innerHTML = ordered.map((option) => (
    `<option value="${escapeHtml(option.value)}">${escapeHtml(option.label)}</option>`
  )).join('');

  const availableValues = new Set(ordered.map((option) => option.value));
  els.labelFilter.value = availableValues.has(currentFilter) ? currentFilter : '';
}

els.clipPlayerClose?.addEventListener('click', () => hideInlinePlayer());


document.addEventListener('keydown', (event) => {
  if (event.key === 'Escape' && !els.clipPlayerCard?.hidden) hideInlinePlayer();
});

// Re-render the recordings list (and any open modal's "Started" line) when
// the user's date_format / time_format changes in another tab. The From/To
// time pickers also need to swap between 24h and 12h+AM/PM, so they're
// re-rendered here too - translation between formats is handled by
// renderTimeSelect reading the current selection via selH/selM, so a
// 14:30 selection in 24h mode becomes "2:30 PM" in 12h mode rather than
// snapping back to the defaults.
window.daygleDatePrefsChanged = function daygleDatePrefsChanged() {
  const preservedFrom = els.recordingTimeFrom ? timeSelectValue(els.recordingTimeFrom) : FILTER_TIME_FROM_DEFAULT;
  const preservedTo = els.recordingTimeTo ? timeSelectValue(els.recordingTimeTo) : FILTER_TIME_TO_DEFAULT;
  els.recordingTimeFrom = renderFilterTimeSelect('recordingTimeFromMount', preservedFrom || FILTER_TIME_FROM_DEFAULT);
  els.recordingTimeTo = renderFilterTimeSelect('recordingTimeToMount', preservedTo || FILTER_TIME_TO_DEFAULT);
  if (typeof loadRecordings !== 'function' || !els || !els.listStatus) return;
  loadRecordings().catch((error) => { els.listStatus.textContent = error.message; });
};

const playbackPageMatch = window.location.pathname.match(/^\/recordings\/(\d+)$/);
const deepLinkId = playbackPageMatch?.[1]
  || new URLSearchParams(window.location.search).get('recording_id');
const autoPlayId = deepLinkId && /^\d+$/.test(String(deepLinkId)) ? deepLinkId : null;
// Clean up the URL when opened via /recordings/{id} deep link so a page
// refresh re-shows the list rather than re-playing the clip.
if (autoPlayId && playbackPageMatch) {
  history.replaceState(null, '', '/recordings');
}

loadAuth().then(async () => {
  await Promise.all([loadCameras(), loadLiveSettings()]);
  await populateLabelFilterOptionsFromApi();
  await loadRecordings();
  // Auto-play the deep-linked recording inline above the library card.
  if (autoPlayId) {
    await playRecording(autoPlayId);
  }
}).catch((error) => {
  if (els.listStatus) els.listStatus.textContent = error.message;
  window.showToast?.(error.message, true);
});
