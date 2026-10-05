// snapshots.js - Snapshots page (the captured-frame library under Clips).
// Loaded by snapshots.html only. Lists every event that saved a frame
// (event.has_snapshot) from /api/snapshots as a visual gallery. Each row
// opens the annotated snapshot image (/api/events/{id}/snapshot) and lets an
// admin delete the stored image - deleting a snapshot never touches the
// event or its recording.
//
// Filtering goes through the bar shared with /events and /recordings
// (web/library_filters.js). /api/snapshots takes the same since/until,
// camera, label, face, keyword, alerted-only and sort parameters as
// /api/events, so the server does the narrowing and the gallery STREAMS:
// the first page paints straight away and older pages arrive on scroll or
// "Load More", instead of the whole range being fetched before first paint.
// Only the Object / Motion type pill filters client-side, because that
// classification is derived from each event's detections.
//
// escapeHtml, api, showToast, timeAgo, formatDate, cameraLabel, detectionPill,
// motionPill, isSoundLabel and GENERIC_TRIGGER_LABELS are provided by
// web/utils.js; createLibraryFilters by web/library_filters.js.

const els = {
  gallery: document.getElementById('snapshotGallery'),
  filterMount: document.getElementById('snapshotFilters'),
  statTotal: document.getElementById('statTotalSnapshots'),
  statCameras: document.getElementById('statCameraCount'),
  statAlerted: document.getElementById('statAlertedCount'),
};

let allSnapshots = [];
// The shared filter bar; created on DOMContentLoaded.
let filters = null;

// ── Snapshot classification ─────────────────────────────────────────────
// Mirrors the events page so the pill labels read identically: sound events
// come from the sound detector; motion-only frames carry no concrete object
// label; everything else is an object frame.
function concreteLabels(event) {
  return (event.detections || [])
    .map((d) => String(d && d.label || '').trim().toLowerCase())
    .filter((label) => label && !GENERIC_TRIGGER_LABELS.has(label));
}

function snapshotIsSound(event) {
  if (!event) return false;
  if (String(event.source || '').toLowerCase() === 'sound') return true;
  if (event.metadata && event.metadata.source === 'sound-detection') return true;
  if ((event.detections || []).some((d) => isSoundLabel(d && d.label))) return true;
  return isSoundLabel(event.metadata && event.metadata.label);
}

function snapshotKind(event) {
  if (snapshotIsSound(event)) return 'sound';
  const detections = event.detections || [];
  if (detections.length && concreteLabels(event).length === 0
      && detections.some((d) => String(d && d.label || '').trim().toLowerCase() === 'motion')) {
    return 'motion';
  }
  return 'object';
}

function snapshotPills(event) {
  const kind = snapshotKind(event);
  if (kind === 'sound') {
    const meta = event.metadata || {};
    const soundDetections = (event.detections || []).filter((d) => isSoundLabel(d && d.label));
    if (soundDetections.length) {
      return soundDetections.map((d) => detectionPill(d.label, d.confidence, true)).join('');
    }
    const label = meta.class_label || meta.label;
    return label ? detectionPill(label, meta.confidence, true) : '';
  }
  if (kind === 'motion') {
    const strongest = (event.detections || [])
      .filter((d) => String(d && d.label || '').toLowerCase() === 'motion')
      .reduce((best, d) => (d && d.confidence > (best ? best.confidence : -1) ? d : best), null);
    return motionPill(strongest ? strongest.confidence : null, motionFractionOf(event.detections));
  }
  const detections = event.detections || [];
  const objectDetections = detections
    .filter((d) => d && d.label && !GENERIC_TRIGGER_LABELS.has(String(d.label).trim().toLowerCase()));
  const motionDetections = detections
    .filter((d) => String(d && d.label || '').trim().toLowerCase() === 'motion');
  const strongestMotion = motionDetections.reduce(
    (best, d) => (d && Number(d.confidence) > (best ? Number(best.confidence) : -1) ? d : best),
    null,
  );
  const motionBadge = motionDetections.length ? motionPill(strongestMotion?.confidence ?? null, motionFractionOf(motionDetections)) : '';
  return `${motionBadge}${objectDetections.map((d) => detectionPill(d.label, d.confidence)).join('')}`
    || '<span class="muted">No detections</span>';
}

function snapshotCameraLabel(event) {
  const meta = event.metadata || {};
  return cameraLabel(meta.camera_name, meta.camera_id) || event.source || 'unknown';
}

function snapshotRow(event) {
  const created = event.created_at || '';
  const camera = snapshotCameraLabel(event);
  const kind = snapshotKind(event);
  const typeClass = kind === 'sound' ? 'activity-item-sound'
    : kind === 'motion' ? 'activity-item-motion'
    : 'activity-item-event';
  const typeLabel = kind === 'sound' ? 'Sound Event'
    : kind === 'motion' ? 'Motion Event'
    : 'Object Event';
  const alerted = Boolean(event.alert);
  const alertBadge = alerted
    ? '<span class="detection detection-alert" title="An alert notification was fired for this event">🔔 Alert</span>'
    : '';
  const snapshotUrl = `/api/events/${encodeURIComponent(event.id)}/snapshot`;
  // A snapshot is an event, so it carries the same AI write-up and tags the
  // events and recordings lists show (app/ai_verification.py). Render them
  // identically - tags inline after the pills, description in a bubble - so
  // the three activity pages read the same.
  const description = (event.metadata || {}).ai_description || {};
  const aiTags = aiTagPills(description.tags);
  const aiTip = aiDescriptionTip(description.text);
  // The gallery renders a 208px-wide thumb, so ask for the downscaled variant:
  // annotating and shipping a full-resolution frame per row is what made this
  // page slow to fill. The Open action still links the full-size image.
  const snapshotThumbUrl = `${snapshotUrl}?thumb=1`;
  const actions = [];
  // Deleting a snapshot is an admin action (the backend requires admin).
  if (window.daygleAuth?.user?.role === 'admin') {
    actions.push(`<button class="secondary activity-item-action activity-item-action-delete" data-delete-snapshot="${escapeHtml(String(event.id))}" type="button" aria-label="Delete snapshot for event ${escapeHtml(String(event.id))}"><svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.25" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><polyline points="3 6 5 6 21 6"/><path d="M19 6l-2 14a2 2 0 0 1-2 2H9a2 2 0 0 1-2-2L5 6"/><path d="M10 11v6M14 11v6"/><path d="M9 6V4a1 1 0 0 1 1-1h4a1 1 0 0 1 1 1v2"/></svg><span class="activity-action-label">Delete</span></button>`);
  }
  actions.push(`<a class="secondary activity-item-action activity-item-action-snapshot" href="${snapshotUrl}" target="_blank" rel="noopener" aria-label="Open snapshot for event ${escapeHtml(String(event.id))}"><svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><rect x="3" y="3" width="18" height="18" rx="2" ry="2"/><circle cx="8.5" cy="8.5" r="1.5"/><path d="M21 15l-5-5L5 21"/></svg><span class="activity-action-label">Open</span></a>`);
  return `
    <article class="snapshot-row ${typeClass}" data-snapshot-row="${escapeHtml(String(event.id))}">
      <a class="snapshot-row-thumb" href="${snapshotUrl}" target="_blank" rel="noopener" aria-label="Open snapshot for event ${escapeHtml(String(event.id))}">
        <img class="lazy-media" src="${snapshotThumbUrl}" alt="Snapshot for event ${escapeHtml(String(event.id))} on ${escapeHtml(camera)}" loading="lazy" onerror="this.remove()" />
      </a>
      <div class="snapshot-row-body">
        <div class="snapshot-row-head">
          <span class="snapshot-ref">Event #${escapeHtml(String(event.id))}</span>
          <span class="activity-item-type">${escapeHtml(typeLabel)}</span>
        </div>
        <div class="snapshot-row-meta">
          <span class="snapshot-camera">${escapeHtml(camera)}</span>
          <span class="snapshot-when" title="${escapeHtml(formatDate(created))}">
            <span class="activity-item-when">
              <span class="activity-item-when-relative">
                <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="12" cy="12" r="10"/><polyline points="12 6 12 12 16 14"/></svg>
                <span>${escapeHtml(timeAgo(created))}</span>
              </span>
              <span class="activity-item-when-absolute">${escapeHtml(formatDate(created))}</span>
            </span>
          </span>
        </div>
        <div class="activity-item-badges snapshot-row-badges">${snapshotPills(event)}${faceIdentityPills(eventFaceIdentities(event))}${alertBadge}${aiTags}${aiTip}</div>
      </div>
      <div class="snapshot-row-actions">${actions.join('')}</div>
    </article>
  `;
}

function renderStats(snapshots) {
  const cameras = new Set(snapshots.map((event) => snapshotCameraLabel(event)).filter(Boolean));
  let alerted = 0;
  for (const event of snapshots) {
    if (event.alert) alerted += 1;
  }
  if (els.statTotal) els.statTotal.textContent = String(snapshots.length);
  if (els.statCameras) els.statCameras.textContent = String(cameras.size);
  if (els.statAlerted) els.statAlerted.textContent = String(alerted);
}

function activeType() {
  return filters ? filters.query().type : 'all';
}

function visibleSnapshots() {
  const type = activeType();
  if (type === 'all') return allSnapshots;
  return allSnapshots.filter((event) => snapshotKind(event) === type);
}

// ── Server-side pagination ──────────────────────────────────────────────
// Mirrors /events: one page paints first, later pages stream on demand.
// Each loadSnapshots() supersedes the previous one so a late page from an
// old filter state never lands in the new gallery.
const SNAPSHOTS_PAGE_SIZE = 120;
let snapshotsPager = null;
let snapshotsLoadSession = 0;
let snapshotsMoreLoading = false;
// True once the incremental renderer has finished painting the gallery; a
// streamed page is only appended to a completed render.
let snapshotsRowsReady = false;

function updateSnapshotCount() {
  if (!filters) return;
  const count = visibleSnapshots().length;
  const more = Boolean(snapshotsPager && !snapshotsPager.done);
  if (!count) {
    filters.setCount(more ? 'More snapshots available' : '0 snapshots');
    return;
  }
  filters.setCount(`${count} snapshot${count === 1 ? '' : 's'}${more ? ' loaded · more available' : ''}`);
}

function renderGalleryFooter() {
  if (!snapshotsPager || snapshotsPager.done) return '';
  return `
    <div class="list-load-more" id="snapshots-more">
      <button type="button" class="secondary list-load-more-btn" id="snapshots-more-btn">Load More</button>
      <span class="muted">Older snapshots load as you scroll.</span>
    </div>`;
}

function wireLoadMore() {
  document.getElementById('snapshots-more')?.remove();
  if (snapshotsPager && !snapshotsPager.done && els.gallery) {
    els.gallery.insertAdjacentHTML('beforeend', renderGalleryFooter());
  }
  const button = document.getElementById('snapshots-more-btn');
  if (button) button.addEventListener('click', () => loadMoreSnapshots());
  const sentinel = document.getElementById('snapshots-more');
  setLoadMoreSentinel('snapshots', sentinel, loadMoreSnapshots);
}

function renderGallery() {
  const snapshots = visibleSnapshots();
  renderStats(snapshots);
  updateSnapshotCount();
  if (!els.gallery) return;
  snapshotsRowsReady = false;
  if (!snapshots.length) {
    els.gallery.innerHTML = `
      <div class="activity-empty-state snapshots-empty-state">
        <div class="activity-empty-icon" aria-hidden="true">
          <svg width="40" height="40" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="3" width="18" height="18" rx="2" ry="2"/><circle cx="8.5" cy="8.5" r="1.5"/><path d="M21 15l-5-5L5 21"/></svg>
        </div>
        <h2>No snapshots match the current filters</h2>
        <p class="muted">Try a wider time range, clearing a filter, or waiting for a new detection to be captured.</p>
      </div>`;
    wireLoadMore();
    return;
  }
  // Item 15: paint the gallery progressively rather than in one innerHTML
  // assignment, and hand the finished grid to the media observer so
  // offscreen thumbnails release their decoded bitmaps (the lazy-media class
  // lets the observer drop src without a refetch on return).
  renderIncrementally(els.gallery, snapshots, snapshotRow, {
    onComplete: () => {
      snapshotsRowsReady = true;
      bindDeleteButtons();
      observeMediaLifecycle(els.gallery);
      wireLoadMore();
    },
  });
}

// Append one streamed page; falls back to a full repaint when the previous
// render has not finished (its unpainted rows would otherwise be lost).
function appendSnapshots(rows) {
  if (snapshotsRowsReady && !rows.length) {
    // Nothing new is visible (the page held only other types): just move the
    // load-more footer along.
    wireLoadMore();
    return;
  }
  if (!els.gallery || !snapshotsRowsReady) {
    renderGallery();
    return;
  }
  document.getElementById('snapshots-more')?.remove();
  snapshotsRowsReady = false;
  renderIncrementally(els.gallery, rows, snapshotRow, {
    append: true,
    onComplete: () => {
      snapshotsRowsReady = true;
      bindDeleteButtons();
      observeMediaLifecycle(els.gallery);
      wireLoadMore();
    },
  });
}

async function loadMoreSnapshots() {
  if (!snapshotsPager || snapshotsPager.done || snapshotsMoreLoading) return;
  snapshotsMoreLoading = true;
  const session = snapshotsLoadSession;
  try {
    const page = await snapshotsPager.loadPage();
    if (session !== snapshotsLoadSession) return;
    allSnapshots = allSnapshots.concat(page.items);
    const type = activeType();
    renderStats(visibleSnapshots());
    updateSnapshotCount();
    appendSnapshots(page.items.filter((event) => type === 'all' || snapshotKind(event) === type));
  } catch (_err) {
    if (session !== snapshotsLoadSession) return;
    if (typeof showToast === 'function') showToast('Failed to load more snapshots.', true);
  } finally {
    snapshotsMoreLoading = false;
  }
}

function bindDeleteButtons() {
  document.querySelectorAll('[data-delete-snapshot]').forEach((button) => {
    // Streamed pages re-bind the gallery; never stack a second handler.
    if (button.dataset.daygleBound) return;
    button.dataset.daygleBound = '1';
    button.addEventListener('click', async () => {
      const id = button.dataset.deleteSnapshot;
      if (!confirm(`Delete snapshot for event #${id}? The event and its recording stay intact.`)) return;
      try {
        await api(`/api/snapshots/${id}`, { method: 'DELETE' });
        window.showToast?.(`Deleted snapshot for event #${id}.`);
        await loadSnapshots();
      } catch (error) {
        // Skip UI updates if api() triggered a 401 redirect.
        if (window.daygleAuth?.redirecting) return;
        window.showToast?.(`Failed to delete snapshot: ${error.message}`, true);
      }
    });
  });
}

// The /api/snapshots query for the bar's current state (the type pill is
// applied client-side by visibleSnapshots()).
function snapshotsQueryString(query) {
  const params = new URLSearchParams();
  if (query.since) params.set('since', query.since);
  if (query.until) params.set('until', query.until);
  if (query.q) params.set('q', query.q);
  if (query.camera_id) params.set('camera_id', query.camera_id);
  if (query.label) params.set('label', query.label);
  if (query.face) params.set('face', query.face);
  if (query.alerted_only) params.set('alerted_only', 'true');
  if (query.sort && query.sort !== 'newest') params.set('sort', query.sort);
  return params.toString();
}

async function loadSnapshots() {
  snapshotsLoadSession += 1;
  const session = snapshotsLoadSession;
  if (els.gallery) els.gallery.innerHTML = '<p class="muted">Loading snapshots…</p>';
  const query = snapshotsQueryString(filters ? filters.query() : libraryDefaultQuery());
  snapshotsPager = createCursorPager(`/api/snapshots${query ? `?${query}` : ''}`, SNAPSHOTS_PAGE_SIZE);
  allSnapshots = [];
  try {
    const page = await snapshotsPager.loadPage();
    if (session !== snapshotsLoadSession) return;
    allSnapshots = page.items;
  } catch (_err) {
    if (session !== snapshotsLoadSession) return;
    allSnapshots = [];
    setLoadMoreSentinel('snapshots', null, loadMoreSnapshots);
    if (els.gallery) els.gallery.innerHTML = '<p class="muted empty-state">Could not load snapshots.</p>';
    if (typeof showToast === 'function') showToast('Failed to load snapshots.', true);
    return;
  }
  renderGallery();
}

// Re-render the custom-range time pickers when Profile > Time Format changes
// in another tab (mirrors /recordings and /events).
window.daygleDatePrefsChanged = function daygleDatePrefsChanged() {
  filters?.refreshTimePickers();
};

document.addEventListener('DOMContentLoaded', async () => {
  // Await the shared /api/auth/me so the delete button only renders for
  // admins (the backend enforces this either way).
  await window.daygleAuthReady;
  if (!els.filterMount) return;
  filters = createLibraryFilters({
    mount: els.filterMount,
    kind: 'snapshots',
    noun: 'snapshots',
    // Sound events carry no frame, so snapshots are only ever object or motion.
    types: ['all', 'object', 'motion'],
    searchPlaceholder: 'Search snapshots: person, driveway, red car, a face name…',
    onChange: (_query, reason) => {
      if (reason === 'type' && snapshotsPager) renderGallery();
      else loadSnapshots();
    },
  });
  loadSnapshots();
});
