let csrfToken = null;
const messageEl = document.getElementById('systemMessage');

function createDatabaseRestoreSection() {
  const section = document.createElement('section');
  section.className = 'card';
  section.innerHTML = `
    <h2>Database backup & restore</h2>
    <p class="muted">Download a point-in-time SQLite backup or restore a previous Daygle database. Restores replace events, users, settings, alert rules, and sessions.</p>
    <div class="button-row"><a class="button-link" href="/api/settings/system/database/backup">Download database backup</a></div>
    <form id="databaseRestoreForm" class="form-grid">
      <label><span>Restore backup file</span><input name="file" type="file" accept=".sqlite,.sqlite3,.db,application/vnd.sqlite3,application/x-sqlite3" required /></label>
      <button class="secondary" type="submit">Restore database</button>
    </form>
    <p class="muted">A safety backup of the current database is created before every restore.</p>
  `;

  const authSection = document.getElementById('authSettingsForm')?.closest('section');
  if (authSection) {
    authSection.before(section);
  } else {
    document.querySelector('main')?.append(section);
  }
}

createDatabaseRestoreSection();

function createCameraManagerSection() {
  const section = document.createElement('section');
  section.className = 'card';
  section.innerHTML = `
    <div class="section-header">
      <div>
        <h2>Multiple cameras</h2>
        <p class="muted">Add additional RTSP/ONVIF cameras and configure per-camera motion, object detection, and monitoring areas from Live Cameras.</p>
      </div>
      <button id="addCameraBtn" type="button">Add camera</button>
    </div>
    <div id="cameraManager" class="camera-manager"></div>
    <div class="button-row"><button id="saveCamerasBtn" type="button">Save cameras</button></div>
  `;
  const cameraSection = document.getElementById('cameraSettingsForm')?.closest('section');
  if (cameraSection) cameraSection.before(section);
}

createCameraManagerSection();

let cameras = [];

function newCameraTemplate() {
  const number = cameras.length + 1;
  return {
    id: `camera-${number}`,
    name: `Camera ${number}`,
    backend: 'onvif',
    device: 0,
    stream_url: '',
    host: '',
    port: 554,
    path: 'stream1',
    username: '',
    password: '',
    width: 1280,
    height: 720,
    fps: 15,
    flip: 'none',
    detection: { motion_enabled: true, object_detection_enabled: true, zones: [] },
  };
}

function renderCameraManager() {
  const manager = document.getElementById('cameraManager');
  manager.innerHTML = cameras.map((camera, index) => `
    <div class="item camera-config-card" data-camera-index="${index}">
      <div class="section-header"><h3>${escapeHtml(camera.name || camera.id || `Camera ${index + 1}`)}</h3><button class="secondary" type="button" data-remove-camera="${index}" ${cameras.length <= 1 ? 'disabled' : ''}>Remove</button></div>
      <div class="form-grid compact-grid">
        <input data-camera-field="id" value="${escapeHtml(camera.id || '')}" placeholder="Camera ID" />
        <input data-camera-field="name" value="${escapeHtml(camera.name || '')}" placeholder="Display name" />
        <label><span>Backend</span><select data-camera-field="backend"><option value="onvif" ${camera.backend === 'onvif' ? 'selected' : ''}>onvif / RTSP</option><option value="rtsp" ${camera.backend === 'rtsp' ? 'selected' : ''}>rtsp</option></select></label>
        <input data-camera-field="stream_url" value="${escapeHtml(camera.stream_url || '')}" placeholder="RTSP stream URL" />
        <input data-camera-field="host" value="${escapeHtml(camera.host || '')}" placeholder="Host/IP" />
        <input data-camera-field="port" type="number" value="${escapeHtml(camera.port || 554)}" placeholder="Port" />
        <input data-camera-field="path" value="${escapeHtml(camera.path || '')}" placeholder="Stream path" />
        <input data-camera-field="username" value="${escapeHtml(camera.username || '')}" placeholder="Username" />
        <input data-camera-field="password" type="password" value="${escapeHtml(camera.password || '')}" placeholder="Password" />
        <input data-camera-field="width" type="number" value="${escapeHtml(camera.width || 1280)}" placeholder="Width" />
        <input data-camera-field="height" type="number" value="${escapeHtml(camera.height || 720)}" placeholder="Height" />
        <input data-camera-field="fps" type="number" value="${escapeHtml(camera.fps || 15)}" placeholder="FPS" />
        <label><span>Motion</span><select data-detection-field="motion_enabled"><option value="true" ${camera.detection?.motion_enabled !== false ? 'selected' : ''}>Enabled</option><option value="false" ${camera.detection?.motion_enabled === false ? 'selected' : ''}>Disabled</option></select></label>
        <label><span>Objects</span><select data-detection-field="object_detection_enabled"><option value="true" ${camera.detection?.object_detection_enabled !== false ? 'selected' : ''}>Enabled</option><option value="false" ${camera.detection?.object_detection_enabled === false ? 'selected' : ''}>Disabled</option></select></label>
      </div>
      <p class="muted">Monitoring areas: ${(camera.detection?.zones || []).length}. Use the Live Cameras page to draw or edit areas visually.</p>
    </div>
  `).join('');

  manager.querySelectorAll('[data-camera-field]').forEach((input) => {
    input.addEventListener('input', () => {
      const card = input.closest('[data-camera-index]');
      const camera = cameras[Number(card.dataset.cameraIndex)];
      const field = input.dataset.cameraField;
      camera[field] = ['port', 'width', 'height', 'fps'].includes(field) ? Number.parseInt(input.value || '0', 10) : input.value;
      if (field === 'name') renderCameraManager();
    });
  });
  manager.querySelectorAll('[data-detection-field]').forEach((select) => {
    select.addEventListener('change', () => {
      const card = select.closest('[data-camera-index]');
      const camera = cameras[Number(card.dataset.cameraIndex)];
      camera.detection ||= { zones: [] };
      camera.detection[select.dataset.detectionField] = select.value === 'true';
    });
  });
  manager.querySelectorAll('[data-remove-camera]').forEach((button) => {
    button.addEventListener('click', () => { cameras.splice(Number(button.dataset.removeCamera), 1); renderCameraManager(); });
  });
}

function escapeHtml(value) {
  return String(value ?? '').replace(/[&<>'"]/g, (char) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', "'": '&#39;', '"': '&quot;' }[char]));
}

const forms = {
  camera: document.getElementById('cameraSettingsForm'),
  anpr: document.getElementById('anprSettingsForm'),
  recording: document.getElementById('recordingSettingsForm'),
  retention: document.getElementById('retentionSettingsForm'),
  storage: document.getElementById('storageSettingsForm'),
  auth: document.getElementById('authSettingsForm'),
  databaseRestore: document.getElementById('databaseRestoreForm'),
};

async function api(path, options = {}) {
  const headers = { ...(options.headers || {}) };
  if (csrfToken && ['POST', 'PUT', 'PATCH', 'DELETE'].includes((options.method || 'GET').toUpperCase())) {
    headers['X-CSRF-Token'] = csrfToken;
  }
  if (options.body && !(options.body instanceof FormData) && !headers['Content-Type']) headers['Content-Type'] = 'application/json';
  const response = await fetch(path, { ...options, headers });
  if (response.status === 401) window.location.href = '/login';
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(payload.detail || `Request failed: ${response.status}`);
  return payload;
}

function setMessage(text) { messageEl.textContent = text; }

function fillForm(form, values) {
  for (const [key, value] of Object.entries(values || {})) {
    if (form.elements[key]) form.elements[key].value = String(value ?? '');
  }
}

function payloadFor(form) {
  const data = Object.fromEntries(new FormData(form).entries());
  for (const key of ['enabled', 'continuous', 'record_on_motion', 'record_on_human', 'auto_purge_enabled']) if (key in data) data[key] = data[key] === 'true';
  for (const key of ['width', 'height', 'fps', 'port', 'pre_event_seconds', 'post_event_seconds', 'max_clip_seconds', 'retention_days', 'max_storage_gb', 'max_login_attempts', 'lockout_minutes']) {
    if (key in data && data[key] !== '') data[key] = Number.parseInt(data[key], 10);
  }
  if ('record_on_objects' in data) data.record_on_objects = data.record_on_objects.split(',').map((label) => label.trim()).filter(Boolean);
  if ('vehicle_labels' in data) data.vehicle_labels = data.vehicle_labels.split(',').map((label) => label.trim()).filter(Boolean);
  if ('min_confidence' in data && data.min_confidence !== '') data.min_confidence = Number(data.min_confidence);
  if ('session_timeout_hours' in data && data.session_timeout_hours !== '') data.session_timeout_hours = Number(data.session_timeout_hours);
  return data;
}

async function loadSettings() {
  const me = await api('/api/auth/me');
  csrfToken = me.csrf_token;
  const settings = await api('/api/settings/system');
  cameras = settings.cameras || [settings.camera];
  renderCameraManager();
  fillForm(forms.camera, settings.camera);
  fillForm(forms.anpr, settings.anpr);
  if (forms.anpr.elements.vehicle_labels) {
    forms.anpr.elements.vehicle_labels.value = (settings.anpr.vehicle_labels || []).join(', ');
  }
  fillForm(forms.recording, settings.recording);
  fillForm(forms.retention, settings.recording);
  if (forms.recording.elements.record_on_objects) {
    forms.recording.elements.record_on_objects.value = (settings.recording.record_on_objects || []).join(', ');
  }
  fillForm(forms.storage, settings.storage);
  fillForm(forms.auth, settings.auth);
  setMessage('System settings loaded.');
}

function bindForm(name, label, endpointName = name) {
  forms[name].addEventListener('submit', async (event) => {
    event.preventDefault();
    try {
      const updated = await api(`/api/settings/system/${endpointName}`, { method: 'PUT', body: JSON.stringify(payloadFor(forms[name])) });
      fillForm(forms[name], updated);
      setMessage(`${label} settings saved.`);
    } catch (error) {
      setMessage(error.message);
    }
  });
}

bindForm('camera', 'Camera');
bindForm('anpr', 'ANPR');
bindForm('recording', 'Recording');
bindForm('retention', 'Retention', 'recording');
bindForm('storage', 'Storage');
bindForm('auth', 'Login security');
document.getElementById('purgeRecordingsBtn').addEventListener('click', async () => {
  try {
    const result = await api('/api/recordings/purge', { method: 'POST' });
    setMessage(`Purged ${result.purged} recording(s), deleted ${result.files_deleted} file(s).`);
  } catch (error) {
    setMessage(error.message);
  }
});

forms.databaseRestore.addEventListener('submit', async (event) => {
  event.preventDefault();
  if (!window.confirm('Restore this database backup? This will replace current events, users, settings, alert rules, and sessions.')) return;
  try {
    const formData = new FormData(forms.databaseRestore);
    const result = await api('/api/settings/system/database/restore', { method: 'POST', body: formData });
    forms.databaseRestore.reset();
    await loadSettings();
    setMessage(`${result.message} Safety backup: ${result.safety_backup}`);
  } catch (error) {
    setMessage(error.message);
  }
});


document.getElementById('addCameraBtn').addEventListener('click', () => {
  cameras.push(newCameraTemplate());
  renderCameraManager();
});

document.getElementById('saveCamerasBtn').addEventListener('click', async () => {
  try {
    const result = await api('/api/cameras', { method: 'PUT', body: JSON.stringify({ cameras }) });
    cameras = result.cameras || [];
    renderCameraManager();
    if (cameras[0]) fillForm(forms.camera, cameras[0]);
    setMessage('Camera list saved. The first camera remains the default for existing dashboard widgets.');
  } catch (error) {
    setMessage(error.message);
  }
});

loadSettings().catch((error) => setMessage(error.message));
