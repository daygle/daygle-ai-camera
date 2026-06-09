let csrfToken = null;
let cameras = [];
let pendingDeleteIndex = null;

const messageEl = document.getElementById('cameraMessage');
const gridEl = document.getElementById('cameraGrid');
const emptyEl = document.getElementById('cameraEmpty');
const modal = document.getElementById('cameraModal');
const deleteModal = document.getElementById('deleteModal');
const editForm = document.getElementById('cameraEditForm');

function setMessage(text, isError = false) {
  messageEl.textContent = text;
  messageEl.className = isError ? 'error' : 'muted';
  if (text) window.showToast?.(text, isError);
}

async function api(path, options = {}) {
  const headers = { ...(options.headers || {}) };
  if (csrfToken && ['POST', 'PUT', 'PATCH', 'DELETE'].includes((options.method || 'GET').toUpperCase())) {
    headers['X-CSRF-Token'] = csrfToken;
  }
  if (options.body && !(options.body instanceof FormData) && !headers['Content-Type']) headers['Content-Type'] = 'application/json';
  const res = await fetch(path, { ...options, headers });
  if (res.status === 401) { window.location.href = '/login'; return; }
  const payload = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(payload.detail || `Request failed: ${res.status}`);
  return payload;
}

function cameraStatusBadge(camera) {
  const url = buildDisplayUrl(camera);
  if (!url && !camera.host) return '<span class="cam-badge cam-badge-warn">Not configured</span>';
  return '<span class="cam-badge cam-badge-idle">Configured</span>';
}

function buildDisplayUrl(camera) {
  if (camera.stream_url) return camera.stream_url;
  if (!camera.host) return '';
  const auth = camera.username ? `${camera.username}:••••@` : '';
  const port = camera.port && camera.port !== 554 ? `:${camera.port}` : '';
  const path = camera.path ? `/${camera.path}` : '/stream1';
  return `rtsp://${auth}${camera.host}${port}${path}`;
}

function renderCameraCard(camera, index) {
  const displayUrl = buildDisplayUrl(camera);
  const name = escapeHtml(camera.name || camera.id || `Camera ${index + 1}`);
  const url = escapeHtml(displayUrl);
  const backend = camera.backend === 'rtsp' ? 'RTSP' : 'ONVIF';
  const recEnabled = camera.recording?.enabled !== false;
  const continuous = camera.recording?.continuous === true;
  const alertClips = camera.recording?.record_on_alert !== false;
  const anprEnabled = camera.detection?.anpr_enabled !== false;
  const res = `${camera.width || 1280}×${camera.height || 720}`;
  const fps = camera.fps || 15;

  return `
    <article class="cam-card" data-camera-index="${index}">
      <div class="cam-card-header">
        <div class="cam-card-identity">
          <div class="cam-icon">
            <svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M23 7l-7 5 7 5V7z"/><rect x="1" y="5" width="15" height="14" rx="2" ry="2"/></svg>
          </div>
          <div>
            <h3 class="cam-name">${name}</h3>
            <span class="cam-id muted">${escapeHtml(camera.id || '')}</span>
          </div>
        </div>
        <div class="cam-card-actions">
          ${cameraStatusBadge(camera)}
          <button class="secondary cam-edit-btn" data-index="${index}" type="button">Edit</button>
          <button class="secondary delete-btn cam-remove-btn" data-index="${index}" type="button">Remove</button>
        </div>
      </div>

      <div class="cam-card-body">
        <div class="cam-meta-row">
          <span class="cam-meta-label">Backend</span>
          <span class="cam-meta-value"><span class="chip">${backend}</span></span>
        </div>
        ${url ? `<div class="cam-meta-row cam-url-row">
          <span class="cam-meta-label">Stream URL</span>
          <span class="cam-meta-value cam-url">${url}</span>
        </div>` : ''}
        <div class="cam-meta-row">
          <span class="cam-meta-label">Resolution</span>
          <span class="cam-meta-value">${escapeHtml(res)} @ ${escapeHtml(fps)} fps</span>
        </div>
        <div class="cam-meta-row">
          <span class="cam-meta-label">Recording</span>
          <span class="cam-meta-value">
            ${recEnabled ? `<span class="chip chip-green">On</span>` : `<span class="chip chip-dim">Off</span>`}
            ${recEnabled && alertClips ? `<span class="chip">Alert clips</span>` : ''}
            ${recEnabled && continuous ? `<span class="chip">Continuous</span>` : ''}
          </span>
        </div>
        <div class="cam-meta-row">
          <span class="cam-meta-label">ANPR</span>
          <span class="cam-meta-value">${anprEnabled ? '<span class="chip chip-green">Enabled</span>' : '<span class="chip chip-dim">Disabled</span>'}</span>
        </div>
        ${(camera.detection?.zones || []).length > 0 ? `<div class="cam-meta-row">
          <span class="cam-meta-label">Zones</span>
          <span class="cam-meta-value">${camera.detection.zones.length} configured</span>
        </div>` : ''}
      </div>

      <div class="cam-card-footer">
        <a class="button-link secondary-link cam-live-link" href="/live?camera=${encodeURIComponent(camera.id || '')}">View Live</a>
        <a class="cam-footer-hint muted" href="/zones?camera=${encodeURIComponent(camera.id || '')}">Configure zones &amp; alerts</a>
      </div>
    </article>
  `;
}

function renderGrid() {
  if (cameras.length === 0) {
    gridEl.innerHTML = '';
    emptyEl.hidden = false;
    return;
  }
  emptyEl.hidden = true;
  gridEl.innerHTML = cameras.map((cam, i) => renderCameraCard(cam, i)).join('');

  gridEl.querySelectorAll('.cam-edit-btn').forEach((btn) => {
    btn.addEventListener('click', () => openEditModal(Number(btn.dataset.index)));
  });
  gridEl.querySelectorAll('.cam-remove-btn').forEach((btn) => {
    btn.addEventListener('click', () => openDeleteModal(Number(btn.dataset.index)));
  });
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
  document.getElementById('editPassword').value = camera.password || '';
  document.getElementById('editWidth').value = camera.width || 1280;
  document.getElementById('editHeight').value = camera.height || 720;
  document.getElementById('editFps').value = camera.fps || 15;
  document.getElementById('editRecordingEnabled').value = String(camera.recording?.enabled !== false);
  document.getElementById('editRecordOnAlert').value = String(camera.recording?.record_on_alert !== false);
  document.getElementById('editContinuous').value = String(camera.recording?.continuous === true);
  document.getElementById('editAnprEnabled').value = String(camera.detection?.anpr_enabled !== false);

  const manual = camera.backend === 'rtsp';
  document.getElementById('rtspManualFields').hidden = !manual;
  document.getElementById('onvifFields').hidden = manual;

  switchTab('connection');
}

function openEditModal(index) {
  const camera = index === null
    ? { id: `camera-${cameras.length + 1}`, name: `Camera ${cameras.length + 1}`, backend: 'onvif', port: 554, path: 'stream1', width: 1280, height: 720, fps: 15, recording: { enabled: true, record_on_alert: true, continuous: false }, detection: { anpr_enabled: true } }
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
    recording: {
      enabled: document.getElementById('editRecordingEnabled').value === 'true',
      record_on_alert: document.getElementById('editRecordOnAlert').value === 'true',
      continuous: document.getElementById('editContinuous').value === 'true',
    },
    detection: {
      anpr_enabled: document.getElementById('editAnprEnabled').value === 'true',
    },
  };
}

editForm.addEventListener('submit', async (e) => {
  e.preventDefault();
  const data = collectModalData();
  const indexEl = document.getElementById('editCameraIndex').value;
  const index = indexEl === '' ? null : Number(indexEl);

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
    renderGrid();
    closeModal(modal);
    setMessage(index === null ? 'Camera added.' : 'Camera updated.');
  } catch (err) {
    cameras = index === null ? cameras.slice(0, -1) : cameras;
    setMessage(err.message, true);
  }
});

// ─── Delete modal ─────────────────────────────────────────────────────────────

function openDeleteModal(index) {
  pendingDeleteIndex = index;
  const name = cameras[index]?.name || cameras[index]?.id || `Camera ${index + 1}`;
  document.getElementById('deleteModalBody').textContent =
    `Remove "${name}" from your configuration? Existing recordings are kept.`;
  openModal(deleteModal);
}

document.getElementById('deleteConfirmBtn').addEventListener('click', async () => {
  if (pendingDeleteIndex === null) return;
  cameras.splice(pendingDeleteIndex, 1);
  try {
    const result = await api('/api/cameras', { method: 'PUT', body: JSON.stringify({ cameras }) });
    cameras = result.cameras || cameras;
    renderGrid();
    setMessage('Camera removed.');
  } catch (err) {
    setMessage(err.message, true);
  }
  closeModal(deleteModal);
  pendingDeleteIndex = null;
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

// ─── Load ─────────────────────────────────────────────────────────────────────

async function loadCameras() {
  const me = await api('/api/auth/me');
  csrfToken = me.csrf_token;
  const settings = await api('/api/settings/system');
  cameras = settings.cameras || (settings.camera ? [settings.camera] : []);
  renderGrid();
  setMessage('');
}

loadCameras().catch((err) => setMessage(err.message, true));
