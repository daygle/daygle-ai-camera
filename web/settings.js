let csrfToken = null;
const messageEl = document.getElementById('systemMessage');

function titleCaseWords(value) {
  return String(value || '')
    .replace(/[_-]+/g, ' ')
    .replace(/\s+/g, ' ')
    .trim()
    .split(' ')
    .filter(Boolean)
    .map((word) => {
      const normalized = word.toLowerCase();
      const acronyms = {
        ai: 'AI',
        anpr: 'ANPR',
        api: 'API',
        fps: 'FPS',
        id: 'ID',
        iou: 'IoU',
        ocr: 'OCR',
        onnx: 'ONNX',
        onvif: 'ONVIF',
        rtsp: 'RTSP',
        ssl: 'SSL',
        tls: 'TLS',
        url: 'URL',
        ip: 'IP',
      };
      if (acronyms[normalized]) return acronyms[normalized];
      return word.charAt(0).toUpperCase() + word.slice(1).toLowerCase();
    })
    .join(' ');
}

const FIELD_LABELS = {
  snapshot_refresh_ms: 'Snapshot Refresh',
  detection_status_refresh_ms: 'Detection Status Refresh',
  detection_interval_seconds: 'Detection Interval',
  event_debounce_seconds: 'Duplicate Event Debounce',
  background_detection_enabled: 'Background Alerts',
  data_dir: 'Data Directory',
  snapshots_dir: 'Snapshots Directory',
  events_dir: 'Events Directory',
  recordings_dir: 'Recordings Directory',
  plates_dir: 'Plate Images Directory',
  session_timeout_hours: 'Session Timeout Hours',
  max_login_attempts: 'Max Login Attempts',
  lockout_minutes: 'Lockout Minutes',
  min_confidence: 'Min Confidence',
  vehicle_labels: 'Vehicle Labels',
  from_address: 'From Address',
  use_tls: 'STARTTLS',
  use_ssl: 'SSL',
  host: 'SMTP Host',
  port: 'Port',
  username: 'Username',
  password: 'Password',
  backend: 'Backend',
  stream_url: 'RTSP Stream URL',
  device: 'Device',
  id: 'ID',
  name: 'Name',
  width: 'Width',
  height: 'Height',
  fps: 'FPS',
  pre_event_seconds: 'Pre-Event Seconds',
  post_event_seconds: 'Post-Event Seconds',
  extension_step_seconds: 'Extension Step Seconds',
  max_clip_seconds: 'Max Clip Seconds',
  retention_days: 'Retention Days',
  max_storage_gb: 'Max Storage GB',
  auto_purge_enabled: 'Auto Purge',
  enabled: 'Enabled',
  continuous: 'Continuous',
  record_on_alert: 'Alert Clips',
  record_on_motion: 'Record on Motion',
  record_on_human: 'Record on Human',
  record_on_objects: 'Record on Objects',
  rule_name: 'Rule Name',
  rule_type: 'Rule Type',
  plate_pattern: 'Plate Pattern',
  cooldown_seconds: 'Cooldown Seconds',
  timezone: 'Timezone',
};

function labelTextForField(field) {
  if (field.dataset.cameraField) return FIELD_LABELS[field.dataset.cameraField] || titleCaseWords(field.dataset.cameraField);
  if (field.dataset.cameraRecording) return FIELD_LABELS[field.dataset.cameraRecording] || titleCaseWords(field.dataset.cameraRecording);
  if (field.name) return FIELD_LABELS[field.name] || titleCaseWords(field.name);
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
      <button class="secondary" type="submit">Restore Database</button>
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

function createRuntimeResetSection() {
  const section = document.createElement('section');
  section.className = 'card danger-zone-card';
  section.innerHTML = `
    <div class="danger-zone-header">
      <h2>Danger Zone</h2>
      <span class="danger-zone-badge">Irreversible</span>
    </div>
    <p class="muted">Start clean removes operational data so you can begin fresh. This action deletes events, recordings, alert history, and ANPR plate history/files.</p>
    <p class="muted danger-zone-warning"><strong>Settings, users, sessions, and alert rules are preserved.</strong> This action cannot be undone.</p>
    <p class="danger-zone-confirm-hint">Confirmation required: type START CLEAN when prompted.</p>
    <div class="button-row"><button id="startCleanBtn" class="secondary delete-btn" type="button">Start Clean</button></div>
  `;

  document.querySelector('main')?.append(section);
}


function createLiveSettingsSection() {
  const section = document.createElement('section');
  section.className = 'card';
  section.innerHTML = `
    <h2>Live performance</h2>
    <form id="liveSettingsForm" class="form-grid">
      <label><span>Snapshot Refresh</span><input name="snapshot_refresh_ms" type="number" min="150" max="5000" step="10" placeholder="500" /><span class="field-help">How often the live camera image updates.</span></label>
      <label><span>Detection Status Refresh</span><input name="detection_status_refresh_ms" type="number" min="500" max="15000" step="100" placeholder="2000" /><span class="field-help">How often the live detection summary updates.</span></label>
      <label><span>Detection Interval</span><input name="detection_interval_seconds" type="number" min="0.1" max="10" step="0.05" placeholder="0.25" /><span class="field-help">How often AI checks each camera for motion and objects.</span></label>
      <label><span>Duplicate Event Debounce</span><input name="event_debounce_seconds" type="number" min="0" max="120" step="0.5" placeholder="10" /><span class="field-help">Prevents repeated recordings for the same ongoing event.</span></label>
      <label><span>AI Track Interval (ms)</span><input name="overlay_track_interval_ms" type="number" min="100" max="5000" step="50" placeholder="450" /><span class="field-help">How often the playback AI tracker refreshes detection boxes. Lower is smoother but uses more CPU.</span></label>
      <label><span>Background Alerts</span><select name="background_detection_enabled"><option value="true">Enabled</option><option value="false">Disabled</option></select></label>
      <button type="submit">Save Live Settings</button>
    </form>
    <p class="muted">Lower values can improve responsiveness but increase CPU/network usage. Background alerts keep checking cameras even when no Live Cameras page is open.</p>
  `;
  const camerasSection = document.querySelector('main > section');
  if (camerasSection) {
    camerasSection.after(section);
  } else {
    document.querySelector('main')?.append(section);
  }
}

createLiveSettingsSection();

function ensureRecordingExtensionStepField() {
  const form = document.getElementById('recordingSettingsForm');
  if (!form) return;
  if (form.querySelector('input[name="extension_step_seconds"]')) return;

  const postInput = form.querySelector('input[name="post_event_seconds"]');
  const label = document.createElement('label');
  label.innerHTML = '<span>Extension Step Seconds</span><input name="extension_step_seconds" type="number" min="0" max="300" placeholder="10" /><span class="field-help">How many seconds to extend an active clip when new detections continue.</span>';
  if (postInput?.parentElement?.tagName === 'LABEL') {
    postInput.parentElement.insertAdjacentElement('afterend', label);
  } else if (postInput) {
    postInput.insertAdjacentElement('afterend', label);
  } else {
    form.insertBefore(label, form.querySelector('button[type="submit"]'));
  }
}

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
      <button id="testEmailBtn" class="secondary" type="button">Send Test Email</button>
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
createRuntimeResetSection();
enhanceFormFieldLabels();
ensureRecordingExtensionStepField();
enhanceFormFieldLabels();

function escapeHtml(value) {
  return String(value ?? '').replace(/[&<>'"]/g, (char) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', "'": '&#39;', '"': '&quot;' }[char]));
}

const emailForm = document.getElementById('emailSettingsForm');
const testEmailRecipient = document.getElementById('testEmailRecipient');
const testEmailBtn = document.getElementById('testEmailBtn');
const startCleanBtn = document.getElementById('startCleanBtn');

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
  for (const key of ['width', 'height', 'fps', 'port', 'pre_event_seconds', 'post_event_seconds', 'extension_step_seconds', 'max_clip_seconds', 'retention_days', 'max_storage_gb', 'max_login_attempts', 'lockout_minutes']) {
    if (key in data && data[key] !== '') data[key] = Number.parseInt(data[key], 10);
  }
  for (const key of ['snapshot_refresh_ms', 'detection_status_refresh_ms', 'overlay_track_interval_ms']) {
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

loadSettings().catch((error) => setMessage(error.message));

function initSoftwareUpdateSection() {
  const checkBtn = document.getElementById('checkUpdateBtn');
  const applyBtn = document.getElementById('applyUpdateBtn');
  const statusEl = document.getElementById('updateStatus');
  const outputEl = document.getElementById('updateOutput');
  if (!checkBtn) return;

  function showUpdateStatus(html, type = '') {
    if (!statusEl) return;
    statusEl.style.display = '';
    statusEl.innerHTML = html;
    statusEl.className = 'status-panel' + (type ? ` status-${type}` : '');
  }

  function showUpdateOutput(text) {
    if (!outputEl) return;
    outputEl.style.display = text ? '' : 'none';
    outputEl.textContent = text;
  }

  checkBtn.addEventListener('click', async () => {
    checkBtn.disabled = true;
    if (applyBtn) applyBtn.style.display = 'none';
    showUpdateStatus('Checking for updates...', '');
    showUpdateOutput('');
    try {
      const result = await api('/api/update/check');
      if (result.error) {
        showUpdateStatus(`Could not reach GitHub: ${escapeHtml(result.error)}`, 'error');
        return;
      }
      const current = escapeHtml(result.current_version || 'unknown');
      const latest = escapeHtml(result.latest_version || 'unknown');
      if (result.update_available) {
        const notesHtml = result.release_notes
          ? `<p class="muted" style="margin-top:.5rem;white-space:pre-wrap">${escapeHtml(result.release_notes.slice(0, 600))}</p>`
          : '';
        showUpdateStatus(
          `<strong>Update available:</strong> v${current} &rarr; v${latest}${notesHtml}`,
          'warning',
        );
        if (applyBtn) applyBtn.style.display = '';
      } else {
        showUpdateStatus(`You are running the latest version (v${current}).`, 'ok');
      }
    } catch (err) {
      showUpdateStatus(`Check failed: ${escapeHtml(err.message)}`, 'error');
    } finally {
      checkBtn.disabled = false;
    }
  });

  applyBtn?.addEventListener('click', async () => {
    if (!confirm('Apply the update now? The service will restart automatically if running under systemd. Make sure to save any open settings first.')) return;
    applyBtn.disabled = true;
    checkBtn.disabled = true;
    showUpdateStatus('Downloading and applying update - this may take a minute...', '');
    showUpdateOutput('');
    try {
      const result = await api('/api/update/apply', { method: 'POST' });
      showUpdateOutput(result.output || '');
      if (result.ok) {
        const restartMsg = result.service_restart_scheduled
          ? ' The service is restarting - please refresh this page in a few seconds.'
          : ' Restart the service manually to apply changes.';
        showUpdateStatus(
          `Update applied successfully. New version: v${escapeHtml(result.new_version || 'unknown')}.${restartMsg}`,
          'ok',
        );
        applyBtn.style.display = 'none';
        const versionEl = document.getElementById('currentVersion');
        if (versionEl && result.new_version) versionEl.textContent = result.new_version;
      } else {
        showUpdateStatus('Update failed. See output below for details.', 'error');
      }
    } catch (err) {
      showUpdateStatus(`Update failed: ${escapeHtml(err.message)}`, 'error');
    } finally {
      applyBtn.disabled = false;
      checkBtn.disabled = false;
    }
  });
}

initSoftwareUpdateSection();

startCleanBtn?.addEventListener('click', async () => {
  const confirmed = confirm('Start clean now? This permanently deletes events, recordings, alerts, and plates, while keeping settings and users.');
  if (!confirmed) return;

  const phrase = prompt('Type START CLEAN to confirm this irreversible action.');
  if (phrase !== 'START CLEAN') {
    setMessage('Start clean cancelled. Confirmation phrase did not match.');
    return;
  }

  startCleanBtn.disabled = true;
  setMessage('Starting clean reset...');
  try {
    const result = await api('/api/system/runtime-data', { method: 'DELETE' });
    const deleted = result?.deleted || {};
    setMessage(
      `Clean start complete. Deleted ${Number(deleted.recordings || 0)} recordings, ${Number(deleted.events || 0)} events, ${Number(deleted.alerts || 0)} alerts, and ${Number(deleted.plates || 0)} plate records. Settings were preserved.`,
    );
  } catch (error) {
    setMessage(error.message);
  } finally {
    startCleanBtn.disabled = false;
  }
});
