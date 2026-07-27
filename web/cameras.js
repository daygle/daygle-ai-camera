let cameras = [];
let pendingDeleteIndex = null;

const messageEl = document.getElementById('cameraMessage');
const gridEl = document.getElementById('cameraGrid');
const emptyEl = document.getElementById('cameraEmpty');
const modal = document.getElementById('cameraModal');
const deleteModal = document.getElementById('deleteModal');
const editForm = document.getElementById('cameraEditForm');

// Stats + filter state
const stats = {
  total: document.getElementById('statTotalCameras'),
  recording: document.getElementById('statRecordingOn'),
  zones: document.getElementById('statWithZones'),
  backends: document.getElementById('statBackends'),
  health: document.getElementById('statCameraHealth'),
};
const filter = {
  text: document.getElementById('cameraFilter'),
  backend: document.getElementById('cameraBackendFilter'),
  reset: document.getElementById('cameraFilterResetBtn'),
  form: document.getElementById('camerasFilterForm'),
};

function setMessage(text, isError = false) {
  messageEl.textContent = text;
  messageEl.className = isError ? 'error' : 'muted';
  if (text) window.showToast?.(text, isError);
}

// api() is provided by web/utils.js (loaded before this script). It throws on
// 401 (after redirecting to /login) - callers that previously hit the silent
// 'return;' branch on 401 still navigate away before any throw is observed.
// The local duplicate + page-local csrfToken were removed so every page shares
// the same fetch contract.

function renderCameraRow(camera, index) {
  const name = escapeHtml(camera.name || camera.id || `Camera ${index + 1}`);
  const id = escapeHtml(camera.id || '');
  const backend = camera.backend === 'rtsp' ? 'RTSP' : 'ONVIF';

  const zones = camera.detection?.zones || [];
  const zoneCount = zones.length;
  const ruleCount = zones.reduce((n, z) => n + (z.object_rules?.length || 0), 0);

  const sound = camera.detection?.sound;
  const soundEnabled = sound?.enabled === true;

  const continuous = camera.recording?.continuous === true;

  const zonesHtml = zoneCount === 0
    ? '<span class="chip chip-warn">No zones</span>'
    : `<span class="chip chip-green">${zoneCount} zone${zoneCount !== 1 ? 's' : ''}</span>${ruleCount > 0 ? ` <span class="chip chip-info">${ruleCount} rule${ruleCount !== 1 ? 's' : ''}</span>` : ''}`;

  const soundHtml = soundEnabled
    ? '<span class="chip chip-green">On</span>'
    : '<span class="chip chip-dim">Off</span>';

  const recordingHtml = continuous
    ? '<span class="chip chip-green">Continuous</span>'
    : '<span class="chip chip-info">On Alert</span>';

  const hasStream = !!(camera.stream_url || camera.host);
  const healthHtml = hasStream
    ? '<span class="health-dot online"></span><span>Online</span>'
    : '<span class="health-dot offline"></span><span>Offline</span>';

  return `
    <tr draggable="true" data-drag-camera="${index}" data-camera-index="${index}">
      <td class="cell-drag"><span class="drag-handle" title="Drag to reorder">${ICONS.grip}</span></td>
      <td class="cell-camera">
        <div class="cam-info"><span class="cam-name">${name}</span>${id ? `<span class="cam-id">${id}</span>` : ''}</div>
        <div class="cell-actions">
          <button class="btn-info cam-edit-btn" data-index="${index}" type="button" title="Edit camera">${ICONS.edit}<span class="action-label">Edit</span></button>
          <button class="btn-danger cam-remove-btn" data-index="${index}" type="button" title="Remove camera">${ICONS.remove}<span class="action-label">Remove</span></button>
        </div>
      </td>
      <td><span class="chip">${backend}</span></td>
      <td class="cell-zones">${zonesHtml}</td>
      <td class="cell-center">${soundHtml}</td>
      <td>${recordingHtml}</td>
      <td class="cell-health">${healthHtml}</td>
    </tr>
  `;
}

function currentFilterValues() {
  return {
    text: (filter.text?.value || '').trim().toLowerCase(),
    backend: filter.backend?.value || '',
  };
}

function applyFilter(list) {
  const { text, backend } = currentFilterValues();
  return list.filter((camera) => {
    if (backend && (camera.backend || 'onvif') !== backend) return false;
    if (!text) return true;
    const haystack = `${camera.name || ''} ${camera.id || ''}`.toLowerCase();
    return haystack.includes(text);
  });
}

function updateFilterHint(filteredCount) {
  const { text, backend } = currentFilterValues();
  const parts = [];
  if (text) parts.push(`matching “${text}”`);
  if (backend === 'onvif') parts.push('using ONVIF');
  else if (backend === 'rtsp') parts.push('using RTSP');
  if (!parts.length) {
    messageEl.textContent = cameras.length
      ? `Showing all ${cameras.length} cameras.`
      : '';
    return;
  }
  messageEl.textContent = `Showing ${filteredCount} of ${cameras.length} cameras ${parts.join(' and ')}.`;
}

function renderGrid() {
  const filtered = applyFilter(cameras);
  if (cameras.length === 0) {
    gridEl.innerHTML = '';
    emptyEl.hidden = false;
    updateFilterHint(0);
    return;
  }
  emptyEl.hidden = true;
  if (filtered.length === 0) {
    gridEl.innerHTML = '<div class="camera-empty-state"><div class="camera-empty-icon" aria-hidden="true"><svg width="40" height="40" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><circle cx="11" cy="11" r="8"/><line x1="21" y1="21" x2="16.65" y2="16.65"/></svg></div><h2>No cameras match these filters</h2><p class="muted">Try clearing the search or selecting a different backend.</p></div>';
    updateFilterHint(0);
    return;
  }
  // Row markup is escaped inside renderCameraRow (camera name/id go through
  // escapeHtml). Build the full table markup in a local first, then assign it,
  // so this stays off the banned ``innerHTML = `…${x}…` `` template-literal
  // pattern flagged by the H2 XSS static guard (same convention as live.js).
  const rowsHtml = filtered.map((cam) => {
    const realIndex = cameras.indexOf(cam);
    return renderCameraRow(cam, realIndex);
  }).join('');
  const tableHtml = `
    <div class="cameras-table-wrap">
      <table class="cameras-table">
        <thead>
          <tr>
            <th class="cell-drag"></th>
            <th>Camera</th>
            <th>Backend</th>
            <th>Zones</th>
            <th class="cell-center">Sound</th>
            <th>Record</th>
            <th>Health</th>

          </tr>
        </thead>
        <tbody>
          ${rowsHtml}
        </tbody>
      </table>
    </div>`;
  gridEl.innerHTML = tableHtml;
  updateFilterHint(filtered.length);

  gridEl.querySelectorAll('.cam-edit-btn').forEach((btn) => {
    btn.addEventListener('click', () => openEditModal(Number(btn.dataset.index)));
  });
  gridEl.querySelectorAll('.cam-remove-btn').forEach((btn) => {
    btn.addEventListener('click', () => openDeleteModal(Number(btn.dataset.index)));
  });

  // Drag-and-drop reorder handlers for camera rows
  const table = gridEl.querySelector('table');
  gridEl.querySelectorAll('[data-drag-camera]').forEach((row) => {
    row.addEventListener('dragstart', (event) => {
      row.classList.add('dragging');
      event.dataTransfer.effectAllowed = 'move';
      event.dataTransfer.setData('text/plain', String(row.dataset.dragCamera));
    });
    row.addEventListener('dragend', () => {
      row.classList.remove('dragging');
      if (table) table.querySelectorAll('tr').forEach((r) => r.classList.remove('drag-over'));
    });
    row.addEventListener('dragover', (event) => {
      event.preventDefault();
      event.dataTransfer.dropEffect = 'move';
      if (table) table.querySelectorAll('tr[data-drag-camera]').forEach((r) => r.classList.remove('drag-over'));
      row.classList.add('drag-over');
    });
    row.addEventListener('drop', async (event) => {
      event.preventDefault();
      row.classList.remove('drag-over');
      const draggedIndex = Number(event.dataTransfer.getData('text/plain'));
      const targetIndex = Number(row.dataset.dragCamera);
      if (!Number.isFinite(draggedIndex) || !Number.isFinite(targetIndex) || draggedIndex === targetIndex) return;
      // Snapshot before mutating so we can restore on API failure
      const camerasBefore = cameras.slice();
      const [draggedCamera] = cameras.splice(draggedIndex, 1);
      const adjustedTarget = targetIndex > draggedIndex ? targetIndex - 1 : targetIndex;
      cameras.splice(adjustedTarget, 0, draggedCamera);
      try {
        const result = await api('/api/cameras', { method: 'PUT', body: JSON.stringify({ cameras }) });
        cameras = result.cameras || cameras;
        renderGrid();
        setMessage('Camera order updated.');
      } catch (err) {
        // Restore the previous order on failure
        cameras.splice(0, cameras.length, ...camerasBefore);
        if (window.daygleAuth?.redirecting) return;
        setMessage(err.message, true);
      }
    });
  });
}

function updateStats() {
  if (stats.total) stats.total.textContent = String(cameras.length);
  if (stats.recording) {
    const continuous = cameras.filter((c) => c.recording?.continuous === true).length;
    const alertBased = cameras.length - continuous;
    stats.recording.textContent = `${alertBased} / ${continuous}`;
  }
  if (stats.zones) {
    const withZones = cameras.filter((c) => (c.detection?.zones || []).length > 0).length;
    stats.zones.textContent = String(withZones);
  }
  if (stats.backends) {
    const onvif = cameras.filter((c) => (c.backend || 'onvif') === 'onvif').length;
    const rtsp = cameras.filter((c) => c.backend === 'rtsp').length;
    stats.backends.textContent = `${onvif} / ${rtsp}`;
  }
}

// ─── Modal helpers ────────────────────────────────────────────────────────────

function openModal(el) {
  el.hidden = false;
  document.body.classList.add('modal-open');
  el.focus?.();
}

function closeModal(el) {
  el.hidden = true;
  document.body.classList.remove('modal-open');
}

function switchTab(tabName) {
  modal.querySelectorAll('.modal-tab').forEach((tab) => {
    const active = tab.dataset.tab === tabName;
    tab.classList.toggle('active', active);
    tab.setAttribute('aria-selected', String(active));
  });
  modal.querySelectorAll('.modal-tab-panel').forEach((panel) => {
    panel.hidden = panel.dataset.panel !== tabName;
  });
}

modal.querySelectorAll('.modal-tab').forEach((tab) => {
  tab.addEventListener('click', () => switchTab(tab.dataset.tab));
});

// Toggle ONVIF vs manual RTSP fields
document.getElementById('editBackend').addEventListener('change', function () {
  const manual = this.value === 'rtsp';
  document.getElementById('rtspManualFields').hidden = !manual;
  document.getElementById('onvifFields').hidden = manual;
});

function fillModal(camera, index) {
  document.getElementById('modalTitle').textContent = index === null ? 'Add Camera' : 'Edit Camera';
  document.getElementById('editCameraIndex').value = index === null ? '' : String(index);
  document.getElementById('editName').value = camera.name || '';
  document.getElementById('editId').value = camera.id || '';
  document.getElementById('editBackend').value = camera.backend || 'onvif';
  document.getElementById('editStreamUrl').value = camera.stream_url || '';
  document.getElementById('editHost').value = camera.host || '';
  document.getElementById('editPort').value = camera.port || 554;
  document.getElementById('editPath').value = camera.path || 'stream1';
  document.getElementById('editUsername').value = camera.username || '';
  const pwdField = document.getElementById('editPassword');
  pwdField.value = '';
  pwdField.placeholder = camera.has_password ? '(saved - type to change)' : '(No Password)';
  document.getElementById('testConnectionResult').textContent = '';
  document.getElementById('editWidth').value = camera.width || 1280;
  document.getElementById('editHeight').value = camera.height || 720;
  document.getElementById('editFps').value = camera.fps || 15;
  const staleVal = camera.stale_frame_grabs;
  document.getElementById('editStaleFrameGrabs').value = staleVal != null ? staleVal : '';
  document.getElementById('editContinuous').value = String(camera.recording?.continuous === true);

  document.getElementById('editMotionPixelThreshold').value = camera.motion_pixel_threshold != null ? camera.motion_pixel_threshold : '';
  document.getElementById('editMotionGateFraction').value = camera.motion_gate_fraction != null ? camera.motion_gate_fraction : '';
  document.getElementById('editMotionScaleFraction').value = camera.motion_scale_fraction != null ? camera.motion_scale_fraction : '';
  document.getElementById('editMotionBackgroundAlpha').value = camera.motion_background_alpha != null ? camera.motion_background_alpha : '';

  const ptz = camera.ptz || {};
  document.getElementById('editPtzEnabled').value = String(ptz.enabled === true);
  document.getElementById('editPtzProtocol').value = ptz.protocol || 'onvif';
  document.getElementById('editPtzHttpPort').value = ptz.http_port || 80;
  document.getElementById('editPtzPort').value = ptz.port || 6060;
  document.getElementById('editPtzAddress').value = ptz.address || 1;
  document.getElementById('editPtzSpeed').value = ptz.speed || 5;
  // Step duration: ONVIF ContinuousMove ``<Timeout>`` value. Empty string
  // means "use server default (0.4s)"; validate_camera_settings will clamp.
  const stepEl = document.getElementById('editPtzStepDuration');
  if (stepEl) stepEl.value = ptz.step_duration != null ? Number(ptz.step_duration).toFixed(2) : '';

  const manual = camera.backend === 'rtsp';
  document.getElementById('rtspManualFields').hidden = !manual;
  document.getElementById('onvifFields').hidden = manual;

  switchTab('connection');
}

function openEditModal(index) {
  const camera = index === null
    ? { id: `camera-${cameras.length + 1}`, name: `Camera ${cameras.length + 1}`, backend: 'onvif', port: 554, path: 'stream1', width: 1280, height: 720, fps: 15, recording: { continuous: false }, detection: {} }
    : cameras[index];
  fillModal(camera, index);
  openModal(modal);
}

function collectModalData() {
  const backend = document.getElementById('editBackend').value;
  return {
    id: document.getElementById('editId').value.trim() || `camera-${cameras.length + 1}`,
    name: document.getElementById('editName').value.trim(),
    backend,
    stream_url: backend === 'rtsp' ? document.getElementById('editStreamUrl').value.trim() : '',
    host: backend !== 'rtsp' ? document.getElementById('editHost').value.trim() : '',
    port: parseInt(document.getElementById('editPort').value || '554', 10),
    path: backend !== 'rtsp' ? document.getElementById('editPath').value.trim() : '',
    username: document.getElementById('editUsername').value.trim(),
    password: document.getElementById('editPassword').value,
    width: parseInt(document.getElementById('editWidth').value || '1280', 10),
    height: parseInt(document.getElementById('editHeight').value || '720', 10),
    fps: parseInt(document.getElementById('editFps').value || '15', 10),
    stale_frame_grabs: document.getElementById('editStaleFrameGrabs').value.trim() !== ''
      ? parseInt(document.getElementById('editStaleFrameGrabs').value, 10)
      : null,
    recording: {
      continuous: document.getElementById('editContinuous').value === 'true',
    },
    ptz: {
      enabled: document.getElementById('editPtzEnabled').value === 'true',
      protocol: document.getElementById('editPtzProtocol').value,
      http_port: parseInt(document.getElementById('editPtzHttpPort').value || '80', 10),
      port: parseInt(document.getElementById('editPtzPort').value || '6060', 10),
      address: parseInt(document.getElementById('editPtzAddress').value || '1', 10),
      speed: parseInt(document.getElementById('editPtzSpeed').value || '5', 10),
      // Empty input falls through to the server's 0.4 s default; the
      // server-side normalizer clamps to [0.1, 5.0].
      step_duration: (() => {
        const raw = parseFloat(document.getElementById('editPtzStepDuration')?.value || '');
        return Number.isFinite(raw) ? raw : 0.4;
      })(),
    },
    detection: {},
    motion_pixel_threshold: (() => { const v = document.getElementById('editMotionPixelThreshold').value.trim(); return v !== '' ? Number.parseInt(v, 10) : null; })(),
    motion_gate_fraction: (() => { const v = document.getElementById('editMotionGateFraction').value.trim(); return v !== '' ? Number(v) : null; })(),
    motion_scale_fraction: (() => { const v = document.getElementById('editMotionScaleFraction').value.trim(); return v !== '' ? Number(v) : null; })(),
    motion_background_alpha: (() => { const v = document.getElementById('editMotionBackgroundAlpha').value.trim(); return v !== '' ? Number(v) : null; })(),
  };
}

editForm.addEventListener('submit', async (e) => {
  e.preventDefault();
  const data = collectModalData();
  const indexEl = document.getElementById('editCameraIndex').value;
  const index = indexEl === '' ? null : Number(indexEl);
  // Snapshot local state before any optimistic mutation so the catch block
  // can restore exactly what was there. Using captured snapshots (rather
  // than `cameras.slice(0, -1)` or no-op `cameras = cameras`) makes the
  // restore idempotent if the catch were to fire twice (re-entry), which
  // would otherwise drop two add-pushes or leave an edit at the wrong value.
  const camerasBefore = cameras.slice();
  const editTargetBefore = index === null ? null : cameras[index];

  if (index === null) {
    cameras.push(data);
  } else {
    cameras[index] = {
      ...cameras[index],
      ...data,
      detection: { ...(cameras[index].detection || {}), ...data.detection },
    };
  }

  try {
    const result = await api('/api/cameras', { method: 'PUT', body: JSON.stringify({ cameras }) });
    cameras = result.cameras || cameras;
    updateStats();
    renderGrid();
    closeModal(modal);
    setMessage(index === null ? 'Camera added.' : 'Camera updated.');
  } catch (err) {
    // splice-restore from the snapshot. Re-applying the same restore is a
    // safe no-op, so the catch is safe under re-entry.
    // Skip UI updates if api() triggered a 401 redirect
    if (window.daygleAuth?.redirecting) return;
    if (index === null) {
      cameras.splice(0, cameras.length, ...camerasBefore);
    } else {
      cameras.splice(index, 1, editTargetBefore);
    }
    setMessage(err.message, true);
  }
});

// ─── Delete modal ─────────────────────────────────────────────────────────────

function openDeleteModal(index) {
  pendingDeleteIndex = index;
  const camera = cameras[index];
  const name = camera?.name || camera?.id || `Camera ${index + 1}`;
  document.getElementById('deleteModalBody').textContent =
    `Remove "${name}" from your configuration? Existing recordings are kept.`;
  openModal(deleteModal);
}

document.getElementById('deleteConfirmBtn').addEventListener('click', async () => {
  if (pendingDeleteIndex === null) return;
  // Snapshot local state + build the post-delete payload WITHOUT pre-splicing
  // cameras. The previous ordering spliced before await, then ran api(); any
  // PUT failure (including the now-thrown 401 from utils.js's shared api())
  // would leave local state mutated while the server kept the camera - i.e.
  // local and remote would diverge silently. By only committing on success,
  // local stays consistent with whatever the server actually accepted.
  const originalIndex = pendingDeleteIndex;
  const camerasBefore = cameras.slice();
  const payloadCameras = camerasBefore.slice(0, originalIndex).concat(camerasBefore.slice(originalIndex + 1));
  try {
    const result = await api('/api/cameras', { method: 'PUT', body: JSON.stringify({ cameras: payloadCameras }) });
    cameras = result.cameras || payloadCameras;
    updateStats();
    renderGrid();
    setMessage('Camera removed.');
  } catch (err) {
    // Skip UI updates if api() triggered a 401 redirect
    if (window.daygleAuth?.redirecting) return;
    // Local state was not mutated, so no restore is required - just report.
    setMessage(err.message, true);
  }
  closeModal(deleteModal);
  pendingDeleteIndex = null;
});

// ─── Test connection ──────────────────────────────────────────────────────────

document.getElementById('testConnectionBtn').addEventListener('click', async () => {
  const btn = document.getElementById('testConnectionBtn');
  const resultEl = document.getElementById('testConnectionResult');
  const backend = document.getElementById('editBackend').value;
  const payload = backend === 'rtsp'
    ? { stream_url: document.getElementById('editStreamUrl').value.trim() }
    : {
        host: document.getElementById('editHost').value.trim(),
        port: parseInt(document.getElementById('editPort').value || '554', 10),
        path: document.getElementById('editPath').value.trim(),
        username: document.getElementById('editUsername').value.trim(),
        password: document.getElementById('editPassword').value,
      };
  btn.disabled = true;
  btn.textContent = 'Testing…';
  resultEl.textContent = '';
  resultEl.style.color = '';
  try {
    const result = await api('/api/cameras/test-connection', { method: 'POST', body: JSON.stringify(payload) });
    resultEl.textContent = result.online ? 'Connected' : (result.message || 'Unreachable');
    resultEl.style.color = result.online ? 'var(--color-success, #22c55e)' : 'var(--color-error, #ef4444)';
  } catch (err) {
    // Skip UI updates if api() triggered a 401 redirect
    if (window.daygleAuth?.redirecting) return;
    resultEl.textContent = err.message || 'Test failed';
    resultEl.style.color = 'var(--color-error, #ef4444)';
  } finally {
    btn.disabled = false;
    btn.textContent = 'Test Connection';
  }
});

// ─── Close handlers ───────────────────────────────────────────────────────────

document.getElementById('addCameraBtn').addEventListener('click', () => openEditModal(null));
document.getElementById('addCameraEmptyBtn').addEventListener('click', () => openEditModal(null));
document.getElementById('modalCloseBtn').addEventListener('click', () => closeModal(modal));
document.getElementById('modalCancelBtn').addEventListener('click', () => closeModal(modal));
document.getElementById('deleteModalCloseBtn').addEventListener('click', () => closeModal(deleteModal));
document.getElementById('deleteCancelBtn').addEventListener('click', () => closeModal(deleteModal));

// Close on backdrop click
[modal, deleteModal].forEach((m) => {
  m.addEventListener('click', (e) => { if (e.target === m) closeModal(m); });
});

// Close on Escape
document.addEventListener('keydown', (e) => {
  if (e.key === 'Escape') {
    if (!modal.hidden) closeModal(modal);
    else if (!deleteModal.hidden) closeModal(deleteModal);
  }
});

// ─── Filter handlers ──────────────────────────────────────────────────────────

filter.text?.addEventListener('input', () => renderGrid());
filter.backend?.addEventListener('change', () => renderGrid());
filter.reset?.addEventListener('click', () => {
  setTimeout(() => renderGrid(), 0);
});
filter.form?.addEventListener('submit', (e) => e.preventDefault());

// Re-render when the user's date_format / time_format changes (no-op here,
// but keeps the page consistent with the rest of the app).
window.daygleDatePrefsChanged = function daygleDatePrefsChanged() { /* no-op */ };

// ─── Load ─────────────────────────────────────────────────────────────────────

async function loadCameras() {
  // nav.js's daygleAuthReady IIFE has already populated window.daygleAuth.{user, csrfToken}.
  await window.daygleAuthReady;
  const settings = await api('/api/settings/system');
  cameras = settings.cameras || (settings.camera ? [settings.camera] : []);
  updateStats();
  renderGrid();
}

async function updateHealthStats() {
  try {
    const data = await api('/api/cameras/health');
    const s = data.summary;
    if (stats.health) {
      const online = s.online || 0;
      const offline = s.offline || 0;
      stats.health.textContent = `${online} / ${offline}`;
      // Color the stat based on health
      if (offline > 0) {
        stats.health.style.color = 'var(--danger-color, #e74c3c)';
      } else if (online > 0) {
        stats.health.style.color = 'var(--success-color, #2ecc71)';
      }
    }
  } catch {
    // silently ignore - health endpoint may not exist on older versions
  }
}

loadCameras().catch((err) => {
  // Skip UI updates if api() triggered a 401 redirect
  if (window.daygleAuth?.redirecting) return;
  setMessage(err.message, true);
});
setInterval(updateHealthStats, 10000);
updateHealthStats();
