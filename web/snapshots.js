// snapshots.js - Snapshots page (the captured-frame library under Clips).
// Loaded by snapshots.html only. Lists every event that saved a frame
// (event.has_snapshot) from /api/snapshots as a visual gallery. Each card
// opens the annotated snapshot image (/api/events/{id}/snapshot) and lets an
// admin delete the stored image - deleting a snapshot never touches the
// event or its recording.
//
// escapeHtml, api, showToast, timeAgo, formatDate, cameraLabel, detectionPill,
// motionPill, isSoundLabel, GENERIC_TRIGGER_LABELS and daygleSinceParamForRange
// are all provided by web/utils.js.

const els = {
  gallery: document.getElementById('snapshotGallery'),
  listStatus: document.getElementById('listStatus'),
  rangeBtns: document.querySelectorAll('[data-range]'),
  statTotal: document.getElementById('statTotalSnapshots'),
  statCameras: document.getElementById('statCameraCount'),
  statAlerted: document.getElementById('statAlertedCount'),
};

let allSnapshots = [];
let activeRange = 'today';

function getSinceParam() {
  return daygleSinceParamForRange(activeRange);
}

// ─── Snapshot classification ─────────────────────────────────────────────
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
    return motionPill(strongest ? strongest.confidence : null);
  }
  const objectDetections = (event.detections || [])
    .filter((d) => d && d.label && !GENERIC_TRIGGER_LABELS.has(String(d.label).trim().toLowerCase()));
  return objectDetections.map((d) => detectionPill(d.label, d.confidence)).join('') || '<span class="muted">No detections</span>';
}

function snapshotCameraLabel(event) {
  const meta = event.metadata || {};
  return cameraLabel(meta.camera_name, meta.camera_id) || event.source || 'unknown';
}

function snapshotCard(event) {
  const created = event.created_at || '';
  const camera = snapshotCameraLabel(event);
  const kind = snapshotKind(event);
  const typeClass = kind === 'sound' ? 'activity-item-sound'
    : kind === 'motion' ? 'activity-item-motion'
    : 'activity-item-event';
  const typeLabel = kind === 'sound' ? 'Sound Event'
    : kind === 'motion' ? 'Motion Event'
    : 'Object Event';
  const alerted = Boolean(event.alert) || Boolean(event.alert_triggered);
  const alertBadge = alerted
    ? '<span class="detection detection-alert" title="An alert notification was fired for this event">🔔 Alert</span>'
    : '';
  const snapshotUrl = `/api/events/${encodeURIComponent(event.id)}/snapshot`;
  const actions = [
    `<a class="secondary activity-item-action activity-item-action-snapshot" href="${snapshotUrl}" target="_blank" rel="noopener" aria-label="Open snapshot for event ${escapeHtml(String(event.id))}"><svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><path d="M7 10l5 5 5-5"/><path d="M12 15V3"/></svg><span class="activity-action-label">Open</span></a>`,
  ];
  // Deleting a snapshot is an admin action (the backend requires admin).
  if (window.daygleAuth?.user?.role === 'admin') {
    actions.push(`<button class="secondary delete-btn activity-item-action" data-delete-snapshot="${escapeHtml(String(event.id))}" type="button" aria-label="Delete snapshot for event ${escapeHtml(String(event.id))}"><svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.25" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><polyline points="3 6 5 6 21 6"/><path d="M19 6l-2 14a2 2 0 0 1-2 2H9a2 2 0 0 1-2-2L5 6"/><path d="M10 11v6M14 11v6"/><path d="M9 6V4a1 1 0 0 1 1-1h4a1 1 0 0 1 1 1v2"/></svg><span class="activity-action-label">Delete</span></button>`);
  }
  return `
    <figure class="snapshot-card ${typeClass}" data-snapshot-row="${escapeHtml(String(event.id))}">
      <a class="snapshot-thumb" href="${snapshotUrl}" target="_blank" rel="noopener" aria-label="Open snapshot for event ${escapeHtml(String(event.id))}">
        <img src="${snapshotUrl}" alt="Snapshot for event ${escapeHtml(String(event.id))} on ${escapeHtml(camera)}" loading="lazy" onerror="this.remove()" />
      </a>
      <figcaption class="snapshot-card-body">
        <div class="snapshot-card-head">
          <span class="snapshot-ref">Event #${escapeHtml(String(event.id))}</span>
          <span class="activity-item-type">${escapeHtml(typeLabel)}</span>
        </div>
        <div class="snapshot-card-meta">
          <span class="snapshot-camera">${escapeHtml(camera)}</span>
          <span class="snapshot-when" title="${escapeHtml(formatDate(created))}">${escapeHtml(timeAgo(created))}</span>
        </div>
        <div class="activity-item-badges">${snapshotPills(event)}${alertBadge}</div>
        <div class="snapshot-card-actions">${actions.join('')}</div>
      </figcaption>
    </figure>
  `;
}

function renderStats() {
  const cameras = new Set(allSnapshots.map((event) => snapshotCameraLabel(event)).filter(Boolean));
  let alerted = 0;
  for (const event of allSnapshots) {
    if (event.alert || event.alert_triggered) alerted += 1;
  }
  if (els.statTotal) els.statTotal.textContent = String(allSnapshots.length);
  if (els.statCameras) els.statCameras.textContent = String(cameras.size);
  if (els.statAlerted) els.statAlerted.textContent = String(alerted);
}

function renderGallery() {
  if (els.listStatus) {
    els.listStatus.textContent = allSnapshots.length
      ? `${allSnapshots.length} snapshot${allSnapshots.length === 1 ? '' : 's'}`
      : '';
  }
  if (!els.gallery) return;
  if (!allSnapshots.length) {
    els.gallery.innerHTML = `
      <div class="activity-empty-state snapshots-empty-state">
        <div class="activity-empty-icon" aria-hidden="true">
          <svg width="40" height="40" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="3" width="18" height="18" rx="2" ry="2"/><circle cx="8.5" cy="8.5" r="1.5"/><path d="M21 15l-5-5L5 21"/></svg>
        </div>
        <h2>No snapshots in this range</h2>
        <p class="muted">Try a wider time range, or wait for a new detection to be captured.</p>
      </div>`;
    return;
  }
  els.gallery.innerHTML = allSnapshots.map(snapshotCard).join('');
  bindDeleteButtons();
}

function bindDeleteButtons() {
  document.querySelectorAll('[data-delete-snapshot]').forEach((button) => {
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

async function loadSnapshots() {
  if (els.gallery) els.gallery.innerHTML = '<p class="muted">Loading snapshots…</p>';
  const params = new URLSearchParams({ limit: '500' });
  const since = getSinceParam();
  if (since) params.set('since', since);
  try {
    const data = await api(`/api/snapshots?${params.toString()}`);
    allSnapshots = Array.isArray(data) ? data : [];
  } catch (err) {
    allSnapshots = [];
    if (els.gallery) els.gallery.innerHTML = '<p class="muted empty-state">Could not load snapshots.</p>';
    if (typeof showToast === 'function') showToast('Failed to load snapshots.', true);
    return;
  }
  renderStats();
  renderGallery();
}

function wireControls() {
  els.rangeBtns.forEach((btn) => {
    btn.addEventListener('click', () => {
      activeRange = btn.dataset.range || 'today';
      els.rangeBtns.forEach((b) => {
        const selected = b === btn;
        b.classList.toggle('active', selected);
        b.setAttribute('aria-selected', selected ? 'true' : 'false');
      });
      loadSnapshots();
    });
  });
}

document.addEventListener('DOMContentLoaded', async () => {
  wireControls();
  // Await the shared /api/auth/me so the delete button only renders for
  // admins (the backend enforces this either way).
  await window.daygleAuthReady;
  loadSnapshots();
});
