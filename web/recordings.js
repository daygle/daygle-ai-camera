const els = {
  recordings: document.getElementById('recordings'),
  // Mount for the filter bar shared with /events and /snapshots
  // (web/library_filters.js).
  filterMount: document.getElementById('recordingFilters'),
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
  statTotalClips: document.getElementById('statTotalClips'),
  statTotalDuration: document.getElementById('statTotalDuration'),
  statCameraCount: document.getElementById('statCameraCount'),
};

// The shared filter bar; created during bootstrap (see the end of the file).
let filters = null;

// Errors from a list load land on the filter bar's note line.
function showListError(message) {
  if (filters) filters.setNote(message);
}

// CSRF token and current user live on window.daygleAuth set via
// setApiAuth() (loaded from web/utils.js). Date/time display preferences are
// also global (window.daygleDatePrefs) and are populated by nav.js from
// /api/auth/me - no page-local state to maintain.
let recordingRefreshTimer = null;
let activeRecording = null;
let overlayResizeObserver;
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
// Configured camera display names keyed by camera id, populated from
// /api/cameras. cameraLabel() reads the event metadata's camera_name, which
// event-less recordings (continuous chunks) don't have - it then falls back
// to the raw camera_id slug ("camera-2"). This map lets those rows show the
// friendly configured name ("Camera 2", "Driveway", ...) instead.
const cameraNamesById = new Map();

// Friendly camera name for a recording row. Prefers the name cameraLabel()
// derives (event metadata for triggered clips); when that only yields the raw
// camera_id - the continuous-chunk case - substitute the configured display
// name so always-on segments don't read as an unnamed "camera-2".
function recordingCameraName(recording) {
  const label = cameraLabel(recording);
  const cameraId = String(recording?.camera_id || '').trim();
  if (cameraId && label === cameraId && cameraNamesById.has(cameraId)) {
    return cameraNamesById.get(cameraId);
  }
  return label;
}

// api() is provided by web/utils.js (loaded before this script) - it reads
// the CSRF token from window.daygleAuth.csrfToken and handles 401 redirects
// so every page shares identical auth and error semantics.

// detectionPill(), motionPill(), isSoundLabel(), SOUND_CLASS_IDS,
// DETECTION_EYE_ICON, DETECTION_MOTION_ICON and GENERIC_TRIGGER_LABELS now
// live in web/utils.js (loaded before this script) so the same rendering is
// shared with the dashboard and the timeline page. Keeping only the local
// helpers that are specific to this page (e.g. recording-selection logic).

// A recording is "motion-only" when:
//  * it isn't a sound recording (sound already has its own visual treatment),
//  * no concrete object labels were detected during the clip (the join-table
//    labels + per-event detections are both empty once generic trigger words
//    are stripped), and
//  * the trigger type wasn't the always-on / disabled placeholders
//    ('continuous', 'none', 'off') so we don't accidentally label
//    always-on clips as motion recordings.
// isMotionOnlyRecording + motionConfidenceFor live in web/utils.js so the
// recordings list, the timeline page and the dashboard activity feed all
// share the same boundary.

// `recordingHasMotion` was added to the shared utility bundle after older
// recordings-page assets may already have been cached. Prefer the shared
// implementation, but keep this page safe when the two bundles are briefly
// out of sync during a deployment.
function hasRecordingMotion(recording) {
  if (typeof window.daygleUi?.recordingHasMotion === 'function') {
    return window.daygleUi.recordingHasMotion(recording);
  }
  return motionConfidenceFor(recording) !== null
    || (recording?.detections || []).some((d) => String(d?.label || '').trim().toLowerCase() === 'motion')
    || (recording?.track || []).some((sample) => (sample?.detections || []).some((d) => String(d?.label || '').trim().toLowerCase() === 'motion'));
}

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

// The local model's plain-English write-up for a clip is stored on the event
// that triggered it, not on the recording row -- the recording keeps only the
// derived ai_labels. Show it as an info-tip bubble so a full sentence does not
// stretch every row of the list.
function recordingDescriptionTip(recording) {
  return aiDescriptionTip(recording.event?.metadata?.ai_description?.text);
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
    // A motion-only clip reads "Motion" even when a motion alert rule fired
    // and stamped the recording's trigger_type as 'alert'.
    if (isMotionOnlyRecording(recording)) return 'Motion';
    // If detections exist and none are specific, trust the detection set and keep this as motion.
    if (!hasDetections && triggerLabel && !GENERIC_TRIGGER_LABELS.has(triggerLabel)) return `${titleCase(triggerType)} · ${titleCase(triggerLabel)}`;
    return titleCase(triggerType);
  }

  if (triggerType === 'continuous' || triggerType === 'none' || triggerType === 'off') {
    // Real always-on chunks read as-is. Event clips recorded while continuous
    // mode is enabled are also stamped 'continuous', so surface their actual
    // trigger instead: a motion clip reads "motion", an object clip reads its
    // concrete labels.
    if (!isContinuousOnlyRecording(recording)) {
      if (isMotionOnlyRecording(recording)) return 'Motion';
      if (detectionLabels.length) {
        return detectionLabels.map((label) => titleCase(label)).join(' · ');
      }
    }
    return titleCase(triggerType);
  }

  if (triggerLabel && triggerLabel !== triggerType) return `${titleCase(triggerType)} · ${titleCase(triggerLabel)}`;
  return titleCase(triggerLabel || triggerType);
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

function renderStats(recordings) {
  if (els.statTotalClips) els.statTotalClips.textContent = String(recordings.length);
  if (els.statTotalDuration) {
    const totalSeconds = recordings.reduce((sum, rec) => sum + (Number(rec.duration_seconds) || 0), 0);
    els.statTotalDuration.textContent = formatDurationShort(totalSeconds);
  }
  if (els.statCameraCount) {
    const cameras = new Set(recordings.map((rec) => recordingCameraName(rec)).filter(Boolean));
    els.statCameraCount.textContent = String(cameras.size);
  }
}

// ── Click-to-sort column headers ─────────────────────────────────────────
// Headers re-order the currently loaded list client-side. `null` means the
// server order from the filter bar's Sort applies; clicking a column cycles
// asc → desc → back to the server default. The sort survives filter changes
// and the preparing-clip auto-refresh, and clears when the user explicitly
// changes the bar's Sort (server-side newest/oldest).
let recordingsSortState = null;
let currentRecordings = [];

// ── Server-side pagination ───────────────────────────────────────────────
// The library STREAMS: the first server page paints immediately, further
// pages arrive when the user asks ("Load more") or scrolls near the bottom
// (sentinel). currentRecordings holds only the pages loaded so far, so a
// huge history is never fetched wholesale before the first paint.
const RECORDINGS_PAGE_SIZE = 200;
// A post-filtered view (motion-only / continuous) keeps drawing server pages until
// it can show a batch, but never more than this per action so one click can't
// quietly drain a sparse history.
const RECORDINGS_MAX_PAGES_PER_LOAD = 10;
let recordingsPager = null;
// Each loadRecordings() supersedes the previous one: a late page from an old
// session must not land in the new list.
let recordingsLoadSession = 0;
let recordingsMoreLoading = false;
// True while the incremental renderer has FINISHED painting the current
// table; a streamed page is only appended to a completed <tbody>.
let recordingsRowsReady = false;
// Client-side post-filters SQL can't express - the motion-only / continuous
// classification and the Object type that excludes both - applied to every
// streamed page by collectVisibleRecordings().
let recordingsViewFilter = null;

function recordingMatchesView(recording) {
  if (!recordingsViewFilter) return true;
  if (recordingsViewFilter.motionOnly && !isMotionOnlyRecording(recording)) return false;
  if (recordingsViewFilter.continuousOnly
    && (isSoundRecording(recording) || isMotionOnlyRecording(recording) || !isContinuousOnlyRecording(recording))) return false;
  if (recordingsViewFilter.objectOnly
    && (isMotionOnlyRecording(recording) || isContinuousOnlyRecording(recording))) return false;
  return true;
}

// The result count on the filter bar describes the LOADED rows and says when
// the history has more, so a partly-streamed list never reads as complete.
function updateRecordingCount() {
  if (!filters) return;
  const count = currentRecordings.length;
  const more = Boolean(recordingsPager && !recordingsPager.done);
  if (!count) {
    filters.setCount(more ? 'More recordings available' : '0 recordings');
    return;
  }
  filters.setCount(`${count} recording${count === 1 ? '' : 's'}${more ? ' loaded · more available' : ''}`);
}

// Stream server pages through the post-filters until `targetCount` visible
// rows are collected (or the history runs out, or the per-action page budget
// is spent). Unfiltered views stop after one page.
async function collectVisibleRecordings(targetCount) {
  const collected = [];
  let pages = 0;
  while (!recordingsPager.done && collected.length < targetCount && pages < RECORDINGS_MAX_PAGES_PER_LOAD) {
    const page = await recordingsPager.loadPage();
    pages += 1;
    for (const item of page.items) {
      if (recordingMatchesView(item)) collected.push(item);
    }
    if (!page.items.length) break;
  }
  return collected;
}

function recordingSortValue(recording, key) {
  switch (key) {
    case 'type': {
      if (isSoundRecording(recording)) return 2;
      return isMotionOnlyRecording(recording) ? 1 : 0;
    }
    case 'camera': return String(recordingCameraName(recording) || '').toLowerCase();
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

function renderRecordingsFooter() {
  if (!recordingsPager || recordingsPager.done) return '';
  return `
    <div class="list-load-more" id="recordings-more">
      <button type="button" class="secondary list-load-more-btn" id="recordings-more-btn">Load More</button>
      <span class="muted">Older clips load as you scroll.</span>
    </div>`;
}

function wireRecordingLoadMore() {
  const button = document.getElementById('recordings-more-btn');
  if (button) button.addEventListener('click', () => loadMoreRecordings());
  const sentinel = document.getElementById('recordings-more');
  setLoadMoreSentinel('recordings', (recordingsPager && !recordingsPager.done) ? sentinel : null, loadMoreRecordings);
}

// Preparing clips keep the 3s auto-refresh armed; anything else disarms it.
function scheduleRecordingRefresh() {
  if (currentRecordings.some((recording) => recording.media_ready === false)) {
    clearTimeout(recordingRefreshTimer);
    recordingRefreshTimer = setTimeout(() => loadRecordings(), 3000);
  } else {
    clearTimeout(recordingRefreshTimer);
    recordingRefreshTimer = null;
  }
}

// Append one streamed page's visible rows to the painted table. Falls back to
// a full repaint when appending is unsafe: a custom column sort must re-order
// the whole loaded set, the empty state has no <tbody>, and a half-painted
// render would lose its unpainted rows to a superseding append.
function appendRecordingRows(rows) {
  const tbody = document.getElementById('recordings-list-rows');
  if (!tbody || !recordingsRowsReady || recordingsSortState) {
    renderRecordings(currentRecordings);
    return;
  }
  recordingsRowsReady = false;
  renderIncrementally(tbody, rows, recordingRowHtml, {
    append: true,
    onComplete: () => {
      recordingsRowsReady = true;
      bindRecordingButtons();
    },
  });
  wireRecordingLoadMore();
  scheduleRecordingRefresh();
}

async function loadMoreRecordings() {
  if (!recordingsPager || recordingsPager.done || recordingsMoreLoading) return;
  recordingsMoreLoading = true;
  const session = recordingsLoadSession;
  try {
    const fresh = await collectVisibleRecordings(RECORDINGS_PAGE_SIZE);
    if (session !== recordingsLoadSession) return;
    currentRecordings = currentRecordings.concat(fresh);
    renderStats(currentRecordings);
    updateRecordingCount();
    appendRecordingRows(fresh);
  } catch (error) {
    // Skip UI updates if api() triggered a 401 redirect
    if (window.daygleAuth?.redirecting) return;
    window.showToast?.(`Failed to load more recordings: ${error.message}`, true);
  } finally {
    recordingsMoreLoading = false;
  }
}

function renderRecordings(recordings) {
  currentRecordings = recordings;
  renderStats(recordings);
  updateRecordingCount();
  if (!recordings.length) {
    // Concatenation, not a template literal: the H2 XSS guard (see
    // tests/test_xss_static_guards.py) rejects any `innerHTML = `…${…}…``
    // assignment in the H2-scoped files, even when the interpolated value is
    // static markup. renderRecordingsFooter() emits no server data.
    els.recordings.innerHTML =
      '<div class="recordings-empty-state">' +
        '<div class="recordings-empty-icon" aria-hidden="true">' +
          '<svg width="40" height="40" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><polygon points="23 7 16 12 23 17 23 7"/><rect x="1" y="5" width="15" height="14" rx="2" ry="2"/></svg>' +
        '</div>' +
        '<h2>No recordings match the current filters</h2>' +
        '<p class="muted">Try resetting the filters, or wait for a new event to be captured.</p>' +
      '</div>' +
      renderRecordingsFooter();
    recordingsRowsReady = false;
    wireRecordingLoadMore();
    return;
  }
  const ordered = recordingsSortState
    ? recordings.slice().sort(compareRecordings)
    : recordings;
  // Item 15: table chrome renders immediately (it carries the sortable
  // headers), rows are painted incrementally so a mature install's recording
  // list does not block the main thread in one innerHTML assignment.
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
    '<tbody id="recordings-list-rows"></tbody>' +
    '</table></div>' +
    renderRecordingsFooter();
  recordingsRowsReady = false;
  renderIncrementally(
    document.getElementById('recordings-list-rows'),
    ordered,
    recordingRowHtml,
    {
      onComplete: () => {
        recordingsRowsReady = true;
        bindRecordingButtons();
      },
    },
  );
  scheduleRecordingRefresh();
  bindSortHeaders();
  wireRecordingLoadMore();
}

// One recording table row. Extracted from renderRecordings (Item 15) so it can
// be handed to renderIncrementally() as a row renderer.
function recordingRowHtml(recording) {
  const mediaReady = recording.media_ready !== false;
  const isSound = isSoundRecording(recording);
  const isMotion = isMotionOnlyRecording(recording);
  // Always-on capture segments carry no triggering detection, so they must
  // not fall through to the "Object Recording" default (which reads as a
  // broken object clip with no detections). Classify them explicitly.
  const isContinuous = !isSound && !isMotion && isContinuousOnlyRecording(recording);
  const typeClass = isSound ? 'activity-item-sound'
    : isMotion ? 'activity-item-motion'
    : isContinuous ? 'activity-item-continuous'
    : 'activity-item-event';
  const typeLabel = isSound ? 'Sound Recording'
    : isMotion ? 'Motion Recording'
    : isContinuous ? 'Continuous Recording'
    : 'Object Recording';
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
    badges = motionPill(motionConfidenceFor(recording), motionFractionFor(recording));
  } else if (isContinuous) {
    // Always-on capture: no triggering detection. Show the neutral
    // "Continuous" chip (plus a Motion pill if the segment happened to
    // catch frame motion) instead of the "No detections" broken-looking
    // fallback.
    const motionBadge = hasRecordingMotion(recording)
      ? motionPill(motionConfidenceFor(recording), motionFractionFor(recording))
      : '';
    badges = `${continuousPill()}${motionBadge}`;
  } else {
    const summaryBadges = recordingDetectionSummary(recording)
      .map((d) => detectionPill(d.label, d.confidence, isSound, d.count)).join('');
    // A clip can contain both frame motion and a recognised object. Keep it
    // as an Object Recording, but show the motion intensity separately so
    // the list does not lose one of the event types.
    const motionBadge = !isSound && hasRecordingMotion(recording)
      ? motionPill(motionConfidenceFor(recording), motionFractionFor(recording))
      : '';
    badges = `${motionBadge}${summaryBadges}` || '<span class="muted">No detections</span>';
  }
  // recordingDetectionSummary already merges the clip's events into one pill
  // per distinct label (with a "×N" multiplier when a label fired across
  // several events), so we no longer append a second row of per-event pills
  // here -- that double-render was showing every shared label twice.
  const actions = [
    `<button class="secondary activity-item-action activity-item-action-delete" data-delete-recording="${recording.id}" type="button" aria-label="Delete recording #${recording.id}"><svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><polyline points="3 6 5 6 21 6"/><path d="M19 6l-2 14a2 2 0 0 1-2 2H9a2 2 0 0 1-2-2L5 6"/><path d="M10 11v6M14 11v6"/></svg><span class="activity-action-label">Delete</span></button>`,
    mediaReady
      ? `<button class="secondary activity-item-action activity-item-action-play" data-play-recording="${recording.id}" type="button" aria-label="Play recording #${recording.id}"><svg width="14" height="14" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true"><polygon points="6 4 20 12 6 20 6 4"/></svg><span class="activity-action-label">Play</span></button>`
      : '<button class="secondary activity-item-action" disabled aria-label="Preparing recording"><svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-linecap="round" aria-hidden="true"><circle cx="12" cy="12" r="10"/><polyline points="12 6 12 12 16 14"/></svg><span class="activity-action-label">Preparing...</span></button>',
  ];
  return `
    <tr class="activity-table-row ${typeClass}" data-recording-row="${recording.id}">
      <td class="activity-cell-type"><span class="activity-item-type">${typeLabel}</span><span class="activity-cell-ref">Recording #${recording.id}</span></td>
      <td class="activity-cell-camera">${escapeHtml(recordingCameraName(recording))}</td>
      <td class="activity-cell-detections"><div class="activity-item-badges">${badges}${faceIdentityPills(collectRecordingFaceIdentities(recording), { countUnknown: false })}${aiTagPills(recording.ai_labels)}${recordingDescriptionTip(recording)}</div></td>
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
}

function renderRecordingDetails(recording) {
  const detections = recordingDetectionSummary(recording);
  const isSound = isSoundRecording(recording);
  const isMotionOnly = isMotionOnlyRecording(recording);
  const isContinuous = !isSound && !isMotionOnly && isContinuousOnlyRecording(recording);
  // The \"Sound\" / \"Motion\" / \"Continuous\" / \"Detections\" label tracks the
  // source the row on the list uses, so opening a clip never surprises users
  // with a different category name. Motion-only clips render the teal motion
  // pill (with the strongest motion intensity confidence for the clip) rather
  // than the bare \"none\" placeholder the row used to show.
  let detectionBadges;
  let detectionLabel;
  if (isMotionOnly) {
    detectionLabel = 'Motion';
    detectionBadges = motionPill(motionConfidenceFor(recording), motionFractionFor(recording));
  } else if (isContinuous) {
    detectionLabel = 'Recording';
    const motionBadge = hasRecordingMotion(recording)
      ? motionPill(motionConfidenceFor(recording), motionFractionFor(recording))
      : '';
    detectionBadges = `${continuousPill()}${motionBadge}`;
  } else if (isSound) {
    detectionLabel = 'Sound';
    const soundDetections = recordingDetectionSummary(recording);
    detectionBadges = soundDetections.length
      ? soundDetections.map((d) => detectionPill(d.label, d.confidence, true, d.count)).join(' ')
      : 'none';
  } else {
    detectionLabel = 'Detections';
    const motionBadge = hasRecordingMotion(recording)
      ? motionPill(motionConfidenceFor(recording), motionFractionFor(recording))
      : '';
    const objectBadges = detections.map((d) => detectionPill(d.label, d.confidence, false, d.count)).join(' ');
    detectionBadges = `${motionBadge}${objectBadges}` || 'none';
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
    safeHtml`<div><span>Camera</span><strong>${recordingCameraName(recording)}</strong></div>`,
    zoneRow,
    safeHtml`<div><span>Trigger</span><strong>${recordingDisplayTrigger(recording)}</strong></div>`,
    (recording.ai_labels || []).length
      ? safeHtml`<div><span>AI Tags</span><strong>${recording.ai_labels.map((tag) => titleCase(tag)).join(' · ')}</strong></div>`
      : '',
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
  // decoder. `mediaTime` is the PTS of the frame currently presented; do not
  // add a speculative one-frame lead here. A lead can make a moving object
  // appear ahead of the footage, while the actual compositor delay is already
  // represented by the callback timing. Falls back to currentTime when VFC is
  // unavailable (older browsers).
  const useVfc = typeof video.requestVideoFrameCallback === 'function';

  function onVfcFrame(now, metadata) {
    if (!els.clipPlayer || els.clipPlayer.paused || !overlayShouldAnimate()) {
      overlayRafId = null;
      overlayVfcHandle = null;
      return;
    }
    const mediaTime = metadata && typeof metadata.mediaTime === 'number' ? metadata.mediaTime : null;
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

  // Use the VFC-provided mediaTime (exact PTS of the displayed frame). When
  // VFC is unavailable, currentTime is the closest equivalent. Avoid adding a
  // synthetic frame duration: it shifts every moving box ahead of the footage.
  const playerTime = typeof vfcMediaTime === 'number' && Number.isFinite(vfcMediaTime)
    ? vfcMediaTime
    : Number(els.clipPlayer.currentTime || 0);

  // The saved detection track replays the boxes the live monitor computed
  // while the clip recorded, so playback never runs inference. Clips without
  // a track fall back to the event's static boxes.
  const track = recordingTrack();
  if (track) {
    const tracked = filterObjectPriorityDetections(
      filterByConfiguredLabels(sampleTrackAtTime(track, playerTime)),
    );
    if (tracked.length) drawDetectionBoxesOnCanvas(els.clipOverlay, tracked, els.clipPlayer);
    return;
  }

  // Static event boxes describe the trigger moment, which sits after the
  // clip's pre-roll; drawing them from time 0 puts a frozen box over footage
  // recorded before the detection existed.
  if (!shouldRenderOverlayForTime(activeRecording, playerTime)) return;
  const allEventDetections = Array.isArray(activeRecording?.detections) ? activeRecording.detections : [];
  const hasSpecificEvent = allEventDetections.some((d) => !GENERIC_TRIGGER_LABELS.has(String(d.label || '').toLowerCase()));
  const eventDetections = filterObjectPriorityDetections(filterByConfiguredLabels(
    hasSpecificEvent
      ? allEventDetections.filter((d) => !GENERIC_TRIGGER_LABELS.has(String(d.label || '').toLowerCase()))
      : allEventDetections
  ));
  if (!eventDetections.length) return;
  drawDetectionBoxesOnCanvas(els.clipOverlay, eventDetections, els.clipPlayer);
}

// Clip segment timeline: see web/clip_timeline.js (loaded before this script).

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
    const camera = recordingCameraName(recording);
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
      window.showToast?.(`Recording #${id} loaded.`);
      return;
    }
    els.clipPlayerStatus.textContent = `Unable to play recording #${id}: ${error?.message || 'media playback failed'}.`;
  }
}

function bindRecordingButtons() {
  // Inline play buttons: open the clip player above the library card.
  // The bound marker keeps this idempotent: a streamed page appends rows and
  // re-binds the container, which must not stack a second handler on the rows
  // already on screen (they would open play/delete twice).
  document.querySelectorAll('[data-play-recording]').forEach((button) => {
    if (button.dataset.daygleBound) return;
    button.dataset.daygleBound = '1';
    button.addEventListener('click', (event) => {
      event.preventDefault();
      const id = button.dataset.playRecording;
      if (id) playRecording(id);
    });
  });
  document.querySelectorAll('[data-delete-recording]').forEach((button) => {
    if (button.dataset.daygleBound) return;
    button.dataset.daygleBound = '1';
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
    // Cache id -> friendly name for every camera so event-less recordings can
    // resolve a display name (see recordingCameraName). The filter bar's
    // Camera dropdown is filled by web/library_filters.js itself.
    for (const camera of cameras) {
      const id = String(camera.id || '').trim();
      if (id) cameraNamesById.set(id, camera.name || camera.id);
    }
  } catch (_error) {
    // Silent api() fallback (no UI mutation) - redirect guard skipped by design.
  }
}

// The /api/recordings query for the filter bar's state. Recordings are
// filtered by their start time, so the bar's since/until become
// started_after/started_before.
function recordingsQueryParams(query) {
  const params = new URLSearchParams();
  if (query.label && query.label !== 'motion') params.set('label', query.label);
  if (query.camera_id) params.set('camera_id', query.camera_id);
  if (query.since) params.set('started_after', query.since);
  if (query.until) params.set('started_before', query.until);
  if (query.sort) params.set('sort', query.sort);
  if (query.q) params.set('q', query.q);
  if (query.face) params.set('face', query.face);
  if (query.alerted_only) params.set('alerted_only', 'true');
  if (query.type === 'sound') params.set('source_type', 'sound');
  if (query.type === 'object') params.set('source_type', 'object');
  return params;
}

async function loadRecordings() {
  const query = filters ? filters.query() : libraryDefaultQuery();
  const params = recordingsQueryParams(query);
  // The backend strips generic trigger words (motion/alert/human/object/none/
  // off/continuous) from `recording.labels`, so a server-side `label=motion`
  // query returns nothing. The Motion label and the Motion / Continuous /
  // Object type pills therefore stream without that filter and keep only the
  // matching clips as pages arrive.
  recordingsViewFilter = {
    motionOnly: query.label === 'motion' || query.type === 'motion',
    continuousOnly: query.type === 'continuous',
    objectOnly: query.type === 'object',
  };
  recordingsLoadSession += 1;
  const session = recordingsLoadSession;
  const queryString = params.toString();
  recordingsPager = createCursorPager(`/api/recordings${queryString ? `?${queryString}` : ''}`, RECORDINGS_PAGE_SIZE);
  currentRecordings = [];
  // One streamed batch first (up to a screenful of VISIBLE rows, so a sparse
  // post-filter still paints something); the rest arrives on demand.
  let recordings;
  try {
    recordings = await collectVisibleRecordings(RECORDINGS_PAGE_SIZE);
  } catch (error) {
    // The failed stream must not leave the old sentinel wired to a dead pager;
    // the error itself keeps propagating to the callers' status/toast handling.
    setLoadMoreSentinel('recordings', null, loadMoreRecordings);
    throw error;
  }
  if (session !== recordingsLoadSession) return currentRecordings;
  filters?.setNote('');
  renderRecordings(recordings);
  return recordings;
}

function reloadRecordings() {
  return loadRecordings().catch((error) => {
    if (window.daygleAuth?.redirecting) return;
    showListError(error.message);
  });
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
  // Storage access can throw (privacy modes, sandboxed frames); fall back to
  // the default instead of aborting the rest of the page wiring (the codebase
  // convention: every localStorage access is guarded).
  let savedValue = null;
  try { savedValue = localStorage.getItem(RECORDINGS_OVERLAY_TOGGLE_KEY); } catch (_err) { /* storage disabled - keep default */ }
  overlayEnabled = savedValue !== '0';
  els.clipOverlayToggle.checked = overlayEnabled;
  els.clipOverlayToggle.addEventListener('change', () => {
    overlayEnabled = Boolean(els.clipOverlayToggle.checked);
    try { localStorage.setItem(RECORDINGS_OVERLAY_TOGGLE_KEY, overlayEnabled ? '1' : '0'); } catch (_err) { /* storage disabled / quota - silently no-op */ }
    if (els.clipPlayer && !els.clipPlayer.paused && overlayShouldAnimate()) {
      startOverlayRaf();
    } else if (!overlayEnabled) {
      stopOverlayRaf();
    }
    drawClipOverlay();
  });
}
} // end if (els.clipPlayer)

els.clipPlayerClose?.addEventListener('click', () => hideInlinePlayer());


document.addEventListener('keydown', (event) => {
  if (event.key === 'Escape' && !els.clipPlayerCard?.hidden) hideInlinePlayer();
});

// Re-render the recordings list (and any open modal's "Started" line) when
// the user's date_format / time_format changes in another tab. The filter
// bar's custom-range time pickers swap between 24h and 12h+AM/PM too.
window.daygleDatePrefsChanged = function daygleDatePrefsChanged() {
  filters?.refreshTimePickers();
  if (!filters) return;
  reloadRecordings();
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
  if (els.filterMount) {
    filters = createLibraryFilters({
      mount: els.filterMount,
      kind: 'recordings',
      noun: 'recordings',
      types: ['all', 'object', 'motion', 'sound', 'continuous'],
      // Motion-only clips carry no concrete label, so Motion is offered
      // explicitly (it filters client-side, see loadRecordings).
      extraLabels: [{ value: 'motion', label: 'Motion' }],
      searchPlaceholder: 'Search recordings: person, driveway, red car, a face name…',
      onChange: (_query, reason) => {
        // Picking an explicit server order drops any column sort so the
        // bar's order is what the table shows.
        if (reason === 'sort') recordingsSortState = null;
        reloadRecordings();
      },
    });
  }
  await Promise.all([loadCameras(), loadLiveSettings()]);
  await loadRecordings();
  // Auto-play the deep-linked recording inline above the library card.
  if (autoPlayId) {
    await playRecording(autoPlayId);
  }
}).catch((error) => {
  showListError(error.message);
  window.showToast?.(error.message, true);
});
