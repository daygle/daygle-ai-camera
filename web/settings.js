let csrfToken = null;
const messageEl = document.getElementById('systemMessage');

function titleCaseWords(value) {
  return String(value || '')
    .replace(/[_-]+/g, ' ')
    .replace(/\s+/g, ' ')
    .trim()
    .split(' ')
    .filter(Boolean)
    .map((word) => word.charAt(0).toUpperCase() + word.slice(1))
    .join(' ');
}

function labelTextForField(field) {
  if (field.dataset.cameraField) return titleCaseWords(field.dataset.cameraField);
  if (field.dataset.cameraRecording) return titleCaseWords(field.dataset.cameraRecording);
  if (field.name) return titleCaseWords(field.name);
  const placeholder = String(field.getAttribute('placeholder') || '').trim();
  if (placeholder) {
    return placeholder
      .replace(/\s*\(e\.g\.[^)]+\)/gi, '')
      .replace(/\s*\([^)]*\)\s*$/g, '')
      .trim();
  }
  return 'Field';
}

function enhanceFormFieldLabels(root = document) {
  root.querySelectorAll('form .form-grid, form .compact-grid').forEach((grid) => {
    Array.from(grid.children).forEach((child) => {
      if (!(child instanceof HTMLElement)) return;
      if (child.tagName === 'LABEL' || child.tagName === 'BUTTON') return;
      if (!child.matches('input, select, textarea')) return;
      if (child.matches('input[type="hidden"]')) return;
      if (child.dataset.autoLabeled === 'true') return;

      const wrapper = document.createElement('label');
      const title = document.createElement('span');
      title.textContent = labelTextForField(child);
      child.replaceWith(wrapper);
      wrapper.append(title, child);
      child.dataset.autoLabeled = 'true';
    });
  });
}

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
        <h2>Cameras</h2>
        <p class="muted">Manage RTSP/ONVIF camera connections here. Configure motion alerts, object alerts, ANPR, and monitoring areas from Live Cameras.</p>
      </div>
      <button id="addCameraBtn" type="button">Add camera</button>
    </div>
    <div id="cameraManager" class="camera-manager"></div>
    <div class="button-row"><button id="saveCamerasBtn" type="button">Save Cameras</button></div>
  `;
  const firstSection = document.querySelector('main > section');
  if (firstSection) {
    firstSection.before(section);
  } else {
    document.querySelector('main')?.append(section);
  }
}

createCameraManagerSection();

function createLiveSettingsSection() {
  const section = document.createElement('section');
  section.className = 'card';
  section.innerHTML = `
    <h2>Live performance</h2>
    <form id="liveSettingsForm" class="form-grid">
      <input name="snapshot_refresh_ms" type="number" min="150" max="5000" step="10" placeholder="Snapshot refresh ms (e.g. 500)" />
      <input name="detection_status_refresh_ms" type="number" min="500" max="15000" step="100" placeholder="Detection status refresh ms (e.g. 2000)" />
      <input name="detection_interval_seconds" type="number" min="0.1" max="10" step="0.05" placeholder="Detection interval seconds (e.g. 0.25)" />
      <input name="event_debounce_seconds" type="number" min="0" max="120" step="0.5" placeholder="Duplicate event debounce seconds (e.g. 10)" />
      <label><span>Background Alerts</span><select name="background_detection_enabled"><option value="true">Enabled</option><option value="false">Disabled</option></select></label>
      <button type="submit">Save Live Settings</button>
    </form>
    <p class="muted">Lower values can improve responsiveness but increase CPU/network usage. Background alerts keep checking cameras even when no Live Cameras page is open.</p>
  `;
  const cameraSection = document.getElementById('cameraManager')?.closest('section');
  if (cameraSection) {
    cameraSection.after(section);
  } else {
    document.querySelector('main')?.append(section);
  }
}

createLiveSettingsSection();

function createEmailDeliverySection() {
  const section = document.createElement('section');
  section.className = 'card';
  section.innerHTML = `
    <h2>Email delivery</h2>
    <form id="emailSettingsForm" class="form-grid">
      <label><span>Email Alerts</span><select name="enabled"><option value="true">Enabled</option><option value="false">Disabled</option></select></label>
      <input name="host" placeholder="SMTP host" />
      <input name="port" type="number" min="1" max="65535" placeholder="Port" />
      <input name="from_address" type="email" placeholder="From address" />
      <input name="username" placeholder="SMTP username" />
      <input name="password" type="password" placeholder="SMTP password" autocomplete="new-password" />
      <label><span>STARTTLS</span><select name="use_tls"><option value="true">Enabled</option><option value="false">Disabled</option></select></label>
      <label><span>SSL</span><select name="use_ssl"><option value="false">Disabled</option><option value="true">Enabled</option></select></label>
      <button type="submit">Save Mail Server</button>
      <input id="testEmailRecipient" type="email" placeholder="Test recipient email" />
      <button id="testEmailBtn" class="secondary" type="button">Send test email</button>
    </form>
    <p class="muted">Object-specific alert rules are configured on the Zones page.</p>
  `;

  const authSection = document.getElementById('authSettingsForm')?.closest('section');
  if (authSection) {
    authSection.before(section);
  } else {
    document.querySelector('main')?.append(section);
  }
}

createEmailDeliverySection();
enhanceFormFieldLabels();

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
    recording: { enabled: true, record_on_alert: true, continuous: false },
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
        <label><span>Backend</span><select data-camera-field="backend"><option value="onvif" ${camera.backend === 'onvif' ? 'selected' : ''}>ONVIF / RTSP</option><option value="rtsp" ${camera.backend === 'rtsp' ? 'selected' : ''}>RTSP</option></select></label>
        <input data-camera-field="stream_url" value="${escapeHtml(camera.stream_url || '')}" placeholder="RTSP stream URL" />
        <input data-camera-field="host" value="${escapeHtml(camera.host || '')}" placeholder="Host/IP" />
        <input data-camera-field="port" type="number" value="${escapeHtml(camera.port || 554)}" placeholder="Port" />
        <input data-camera-field="path" value="${escapeHtml(camera.path || '')}" placeholder="Stream path" />
        <input data-camera-field="username" value="${escapeHtml(camera.username || '')}" placeholder="Username" />
        <input data-camera-field="password" type="password" value="${escapeHtml(camera.password || '')}" placeholder="Password" />
        <input data-camera-field="width" type="number" value="${escapeHtml(camera.width || 1280)}" placeholder="Width" />
        <input data-camera-field="height" type="number" value="${escapeHtml(camera.height || 720)}" placeholder="Height" />
        <input data-camera-field="fps" type="number" value="${escapeHtml(camera.fps || 15)}" placeholder="FPS" />
      </div>
      <div class="form-grid compact-grid">
        <label><span>Recording</span><select data-camera-recording="enabled"><option value="true" ${camera.recording?.enabled !== false ? 'selected' : ''}>Enabled</option><option value="false" ${camera.recording?.enabled === false ? 'selected' : ''}>Disabled</option></select></label>
        <label><span>Alert clips</span><select data-camera-recording="record_on_alert"><option value="true" ${camera.recording?.record_on_alert !== false ? 'selected' : ''}>Enabled</option><option value="false" ${camera.recording?.record_on_alert === false ? 'selected' : ''}>Disabled</option></select></label>
        <label><span>Continuous</span><select data-camera-recording="continuous"><option value="false" ${camera.recording?.continuous !== true ? 'selected' : ''}>Disabled</option><option value="true" ${camera.recording?.continuous === true ? 'selected' : ''}>Enabled</option></select></label>
      </div>
      <p class="muted">Monitoring areas: ${(camera.detection?.zones || []).length}. Use Live Cameras to configure motion alerts, object alerts, ANPR, and areas visually.</p>
    </div>
  `).join('');

  enhanceFormFieldLabels(manager);

  manager.querySelectorAll('[data-camera-field]').forEach((input) => {
    input.addEventListener('input', () => {
      const card = input.closest('[data-camera-index]');
      const camera = cameras[Number(card.dataset.cameraIndex)];
      const field = input.dataset.cameraField;
      camera[field] = ['port', 'width', 'height', 'fps'].includes(field) ? Number.parseInt(input.value || '0', 10) : input.value;
      if (field === 'name') {
        const heading = card.querySelector('.section-header h3');
        if (heading) heading.textContent = camera.name || camera.id || `Camera ${Number(card.dataset.cameraIndex) + 1}`;
      }
    });
  });
  manager.querySelectorAll('[data-camera-recording]').forEach((select) => {
    select.addEventListener('change', () => {
      const card = select.closest('[data-camera-index]');
      const camera = cameras[Number(card.dataset.cameraIndex)];
      camera.recording ||= { enabled: true, record_on_alert: true, continuous: false };
      camera.recording[select.dataset.cameraRecording] = select.value === 'true';
    });
  });
  manager.querySelectorAll('[data-remove-camera]').forEach((button) => {
    button.addEventListener('click', () => { cameras.splice(Number(button.dataset.removeCamera), 1); renderCameraManager(); });
  });
}

function escapeHtml(value) {
  return String(value ?? '').replace(/[&<>'"]/g, (char) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', "'": '&#39;', '"': '&quot;' }[char]));
}

const emailForm = document.getElementById('emailSettingsForm');
const testEmailRecipient = document.getElementById('testEmailRecipient');
const testEmailBtn = document.getElementById('testEmailBtn');

const forms = {
  anpr: document.getElementById('anprSettingsForm'),
  live: document.getElementById('liveSettingsForm'),
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
  for (const key of ['enabled', 'continuous', 'record_on_motion', 'record_on_human', 'auto_purge_enabled', 'background_detection_enabled']) if (key in data) data[key] = data[key] === 'true';
  for (const key of ['width', 'height', 'fps', 'port', 'pre_event_seconds', 'post_event_seconds', 'max_clip_seconds', 'retention_days', 'max_storage_gb', 'max_login_attempts', 'lockout_minutes']) {
    if (key in data && data[key] !== '') data[key] = Number.parseInt(data[key], 10);
  }
  for (const key of ['snapshot_refresh_ms', 'detection_status_refresh_ms']) {
    if (key in data && data[key] !== '') data[key] = Number.parseInt(data[key], 10);
  }
  if ('detection_interval_seconds' in data && data.detection_interval_seconds !== '') data.detection_interval_seconds = Number(data.detection_interval_seconds);
  if ('event_debounce_seconds' in data && data.event_debounce_seconds !== '') data.event_debounce_seconds = Number(data.event_debounce_seconds);
  if ('record_on_objects' in data) data.record_on_objects = data.record_on_objects.split(',').map((label) => label.trim()).filter(Boolean);
  if ('vehicle_labels' in data) data.vehicle_labels = data.vehicle_labels.split(',').map((label) => label.trim()).filter(Boolean);
  if ('min_confidence' in data && data.min_confidence !== '') data.min_confidence = Number(data.min_confidence);
  if ('session_timeout_hours' in data && data.session_timeout_hours !== '') data.session_timeout_hours = Number(data.session_timeout_hours);
  return data;
}

function emailPayload(form) {
  const data = Object.fromEntries(new FormData(form).entries());
  for (const key of ['enabled', 'use_tls', 'use_ssl']) if (key in data) data[key] = data[key] === 'true';
  if (data.port !== '') data.port = Number.parseInt(data.port, 10);
  return data;
}

function renderEmail(settings) {
  if (!emailForm) return;
  for (const [key, value] of Object.entries(settings || {})) {
    if (emailForm.elements[key]) emailForm.elements[key].value = String(value ?? '');
  }
  if (!emailForm.elements.port.value) emailForm.elements.port.value = '587';
  if (testEmailRecipient && !testEmailRecipient.value) testEmailRecipient.value = settings.from_address || '';
}

async function loadSettings() {
  const me = await api('/api/auth/me');
  csrfToken = me.csrf_token;
  const [settings, emailSettings] = await Promise.all([
    api('/api/settings/system'),
    api('/api/settings/alert-email'),
  ]);
  cameras = settings.cameras || [settings.camera];
  renderCameraManager();
  fillForm(forms.anpr, settings.anpr);
  fillForm(forms.live, settings.live);
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
  renderEmail(emailSettings);
  enhanceFormFieldLabels();
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

bindForm('anpr', 'ANPR');
bindForm('live', 'Live');
bindForm('recording', 'Recording');
bindForm('retention', 'Retention', 'recording');
bindForm('storage', 'Storage');
bindForm('auth', 'Login security');

emailForm?.addEventListener('submit', async (event) => {
  event.preventDefault();
  try {
    renderEmail(await api('/api/settings/alert-email', { method: 'PUT', body: JSON.stringify(emailPayload(emailForm)) }));
    setMessage('Mail server settings saved.');
  } catch (error) {
    setMessage(error.message);
  }
});

testEmailBtn?.addEventListener('click', async () => {
  const recipient = testEmailRecipient.value.trim() || emailForm.elements.from_address.value.trim();
  if (!recipient) {
    setMessage('Enter a test recipient email address.');
    return;
  }
  testEmailBtn.disabled = true;
  setMessage('Sending test email...');
  try {
    await api('/api/settings/alert-email/test', {
      method: 'POST',
      body: JSON.stringify({ settings: emailPayload(emailForm), recipient }),
    });
    setMessage(`Test email sent to ${recipient}.`);
  } catch (error) {
    setMessage(error.message);
  } finally {
    testEmailBtn.disabled = false;
  }
});

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
    setMessage('Camera list saved.');
  } catch (error) {
    setMessage(error.message);
  }
});

loadSettings().catch((error) => setMessage(error.message));
