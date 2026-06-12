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
  event_debounce_seconds: 'Fallback Event Merge (s)',
  detection_history_minutes: 'Detection History (min)',
  background_detection_enabled: 'Background Alerts',
  data_dir: 'Data Directory',
  snapshots_dir: 'Snapshots Directory',
  events_dir: 'Events Directory',
  recordings_dir: 'Recordings Directory',
  session_timeout_hours: 'Session Timeout Hours',
  max_login_attempts: 'Max Login Attempts',
  lockout_minutes: 'Lockout Minutes',
  min_confidence: 'Min Confidence',
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
  extension_step_seconds: 'Extend On Motion (s)',
  max_clip_seconds: 'Max Clip Seconds',
  retention_days: 'Retention Days',
  max_storage_gb: 'Max Storage GB',
  auto_purge_enabled: 'Auto Purge',
  enabled: 'Enabled',
  continuous: 'Continuous',
  record_on_alert: 'Alert Clips',
  rule_name: 'Rule Name',
  rule_type: 'Rule Type',
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
    <div class="settings-section-header"><div class="settings-section-icon">💾</div><div><h2>Database Backup & Restore</h2><p class="settings-section-subtitle">Download a snapshot of your database or restore from a previous backup. A safety backup is automatically created before every restore.</p></div></div>
    <form id="databaseRestoreForm" class="form-grid">
      <label><span>Restore backup file</span><input name="file" type="file" accept=".sqlite,.sqlite3,.db,application/vnd.sqlite3,application/x-sqlite3" required /><span class="field-help">Select a previously downloaded .sqlite backup file to restore. Restores replace events, users, settings, alert rules, and sessions with the backup contents.</span></label>
    </form>
    <div class="button-row"><a class="secondary" href="/api/settings/system/database/backup">Download Database Backup</a><button class="secondary" type="submit" form="databaseRestoreForm">Restore Database</button></div>
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
    <div class="settings-section-header settings-section-header-danger">
      <div class="settings-section-icon settings-section-icon-danger">⚠️</div>
      <div>
        <div class="danger-zone-header">
          <h2>Danger Zone</h2>
          <span class="danger-zone-badge">Irreversible</span>
        </div>
        <p class="settings-section-subtitle">Start clean removes all operational data so you can begin fresh.</p>
      </div>
    </div>
    <p class="muted">This action deletes <strong>events, recordings, and alert history</strong>.</p>
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
    <div class="settings-section-header"><div class="settings-section-icon">⚡</div><div><h2>Live Performance</h2><p class="settings-section-subtitle">Tune refresh rates and detection frequency to balance responsiveness with resource usage.</p></div></div>
    <form id="liveSettingsForm" class="form-grid">
      <label><span>Snapshot Refresh</span><input name="snapshot_refresh_ms" type="number" min="150" max="5000" step="10" placeholder="500" /><span class="field-help">How often the live camera image updates. Lower = more responsive, higher = less bandwidth. Default: 500ms</span></label>
      <label><span>Detection Status Refresh</span><input name="detection_status_refresh_ms" type="number" min="500" max="15000" step="100" placeholder="2000" /><span class="field-help">How often the detection summary panel updates with new object counts. Default: 2000ms</span></label>
      <label><span>Detection Interval</span><input name="detection_interval_seconds" type="number" min="0.1" max="10" step="0.05" placeholder="0.25" /><span class="field-help">How often AI checks each camera for motion and objects. Lower = faster alerts, higher CPU. Default: 0.5s</span></label>
      <label><span>Fallback Event Merge (s)</span><input name="event_debounce_seconds" type="number" min="0" max="300" step="1" placeholder="10" /><span class="field-help">Merges detections within this window into one event. Per-object cooldowns on the Zones page override this. Default: 10s</span></label>
      <label><span>Background Alerts</span><select name="background_detection_enabled"><option value="true">Enabled</option><option value="false">Disabled</option></select><span class="field-help">Keep checking cameras even when no Live Cameras page is open. Default: Enabled</span></label>
      <label><span>Detection History (min)</span><input name="detection_history_minutes" type="number" min="1" max="120" step="1" placeholder="10" /><span class="field-help">How many minutes of detection data to keep per camera for recording playback overlays. Default: 10 min</span></label>
      </form>
    <div class="button-row"><button type="submit" form="liveSettingsForm">Save Live Settings</button></div>
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
  label.innerHTML = '<span>Extend On Motion (s)</span><input name="extension_step_seconds" type="number" min="0" max="300" placeholder="10" /><span class="field-help">Each time motion continues within the Event Merge Window, the recording is extended by this many seconds. Set to 30–60 to keep recording while activity is ongoing.</span>';
  if (postInput?.parentElement?.tagName === 'LABEL') {
    postInput.parentElement.insertAdjacentElement('afterend', label);
  } else if (postInput) {
    postInput.insertAdjacentElement('afterend', label);
  } else {
    form.insertBefore(label, form.querySelector('button[type="submit"]'));
  }
}

function createPushNotificationSection() {
  const section = document.createElement('section');
  section.className = 'card';
  section.innerHTML = `
    <div class="settings-section-header"><div class="settings-section-icon">🔔</div><div><h2>Push Notifications</h2><p class="settings-section-subtitle">Get instant alerts on your Android device via the ntfy app. Per-rule push toggles are on the Zones page. Install the <a href="https://ntfy.sh" target="_blank" rel="noopener">ntfy app</a> on your Android device and subscribe to your topic to receive alerts.</p></div></div>
    <form id="pushSettingsForm" class="form-grid">
      <label><span>Push Alerts</span><select name="enabled"><option value="false">Disabled</option><option value="true">Enabled</option></select><span class="field-help">Master toggle for push notifications. Default: Disabled</span></label>
      <label><span>Server URL</span><input name="server_url" placeholder="https://ntfy.sh" /><span class="field-help">NTFY server address. Use <code>https://ntfy.sh</code> for the free hosted service. Default: https://ntfy.sh</span></label>
      <label><span>Topic Name</span><input name="topic" placeholder="my-camera-alerts" /><span class="field-help">Subscribe your phone to this exact topic name to receive alerts. Default: (none)</span></label>
      <label><span>Priority</span><select name="priority"><option value="default">Default</option><option value="min">Min</option><option value="low">Low</option><option value="high">High</option><option value="urgent">Urgent</option></select><span class="field-help">Controls how the notification is presented on your device. Default: Default</span></label>
      <label><span>Username</span><input name="username" placeholder="Optional" autocomplete="off" /><span class="field-help">Required only if your NTFY server requires authentication. Default: (none)</span></label>
      <label><span>Password</span><input name="password" type="password" placeholder="Optional" autocomplete="new-password" /><span class="field-help">Required only if your NTFY server requires authentication.</span></label>
    </form>
    <div class="button-row"><button type="submit" form="pushSettingsForm">Save Push Settings</button><button id="testPushBtn" class="secondary" type="button">Send Test Notification</button></div>
  `;

  const authSection = document.getElementById('authSettingsForm')?.closest('section');
  if (authSection) {
    authSection.before(section);
  } else {
    document.querySelector('main')?.append(section);
  }
}


function createCameraOfflineSection() {
  const section = document.createElement('section');
  section.className = 'card';
  section.innerHTML = `
    <div class="settings-section-header"><div class="settings-section-icon">📡</div><div><h2>Camera Offline Notifications</h2><p class="settings-section-subtitle">Get notified when a camera goes offline or recovers. Works with both email and push notification channels.</p></div></div>
    <form id="cameraOfflineForm" class="form-grid">
      <label><span>Offline Alerts</span><select name="enabled"><option value="false">Disabled</option><option value="true">Enabled</option></select><span class="field-help">Master toggle for camera offline notifications. When enabled, you'll be notified when a camera stays offline past the delay period. Default: Disabled</span></label>
      <label><span>Offline Delay (minutes)</span><input name="offline_delay_minutes" type="number" min="1" max="60" step="1" placeholder="1" /><span class="field-help">How long a camera must be unreachable before sending an offline notification. Set higher to avoid alerts from brief connection blips. Default: 1 min</span></label>
    </form>
    <div class="button-row"><button type="submit" form="cameraOfflineForm">Save Offline Alert Settings</button></div>
  `;

  const pushSection = document.getElementById('pushSettingsForm')?.closest('section');
  if (pushSection) {
    pushSection.after(section);
  } else {
    document.querySelector('main')?.append(section);
  }
}

function createEmailDeliverySection() {
  const section = document.createElement('section');
  section.className = 'card';
  section.innerHTML = `
    <div class="settings-section-header"><div class="settings-section-icon">✉️</div><div><h2>Email Delivery</h2><p class="settings-section-subtitle">Configure your SMTP mail server to send alert emails. Per-rule email toggles are on the Zones page.</p></div></div>
    <form id="emailSettingsForm" class="form-grid">
      <label><span>Email Alerts</span><select name="enabled"><option value="true">Enabled</option><option value="false">Disabled</option></select><span class="field-help">Master toggle for email notifications. Default: Disabled</span></label>
      <label><span>SMTP Host</span><input name="host" placeholder="smtp.gmail.com" /><span class="field-help">Your mail server hostname (e.g. smtp.gmail.com, smtp-mail.outlook.com). Default: (none)</span></label>
      <label><span>SMTP Port</span><input name="port" type="number" min="1" max="65535" placeholder="587" /><span class="field-help">Common ports: 587 (STARTTLS), 465 (SSL), 25 (unencrypted). Default: 587</span></label>
      <label><span>From Address</span><input name="from_address" type="email" placeholder="alerts@example.com" /><span class="field-help">The sender address that appears in outgoing alert emails. Default: (none)</span></label>
      <label><span>SMTP Username</span><input name="username" placeholder="Your login" /><span class="field-help">Authentication username for your mail server. Default: (none)</span></label>
      <label><span>SMTP Password</span><input name="password" type="password" placeholder="Your password" autocomplete="new-password" /><span class="field-help">Authentication password or app password for your mail server. Default: (none)</span></label>
      <label><span>STARTTLS</span><select name="use_tls"><option value="true">Enabled</option><option value="false">Disabled</option></select><span class="field-help">Encrypts the connection using STARTTLS (recommended for port 587). Default: Enabled</span></label>
      <label><span>SSL</span><select name="use_ssl"><option value="false">Disabled</option><option value="true">Enabled</option></select><span class="field-help">Encrypts the connection using implicit SSL (recommended for port 465). Default: Disabled</span></label>
      <label><span>Test Recipient</span><input id="testEmailRecipient" type="email" placeholder="you@example.com" /><span class="field-help">Enter an address to send a test email to.</span></label>
    </form>
    <div class="button-row"><button id="testEmailBtn" class="secondary" type="button">Send Test Email</button><button type="submit" form="emailSettingsForm">Save Mail Server</button></div>
  `;

  const authSection = document.getElementById('authSettingsForm')?.closest('section');
  if (authSection) {
    authSection.before(section);
  } else {
    document.querySelector('main')?.append(section);
  }
}

createPushNotificationSection();
createCameraOfflineSection();
createEmailDeliverySection();
createRuntimeResetSection();
ensureRecordingExtensionStepField();
enhanceFormFieldLabels();

document.querySelectorAll('.field-help').forEach((el) => {
  if (!el.title) el.title = el.textContent;
});

const emailForm = document.getElementById('emailSettingsForm');
const testEmailRecipient = document.getElementById('testEmailRecipient');
const testEmailBtn = document.getElementById('testEmailBtn');
const pushForm = document.getElementById('pushSettingsForm');
const testPushBtn = document.getElementById('testPushBtn');
const startCleanBtn = document.getElementById('startCleanBtn');

const forms = {
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

function setMessage(text, isError = false) {
  messageEl.textContent = text;
  if (text) window.showToast?.(text, isError);
}

function fillForm(form, values) {
  for (const [key, value] of Object.entries(values || {})) {
    if (form.elements[key]) form.elements[key].value = String(value ?? '');
  }
}

function payloadFor(form) {
  const data = Object.fromEntries(new FormData(form).entries());
  for (const key of ['enabled', 'continuous', 'auto_purge_enabled', 'background_detection_enabled']) if (key in data) data[key] = data[key] === 'true';
  for (const key of ['width', 'height', 'fps', 'port', 'pre_event_seconds', 'post_event_seconds', 'extension_step_seconds', 'max_clip_seconds', 'retention_days', 'max_storage_gb', 'max_login_attempts', 'lockout_minutes']) {
    if (key in data && data[key] !== '') data[key] = Number.parseInt(data[key], 10);
  }
  for (const key of ['snapshot_refresh_ms', 'detection_status_refresh_ms']) {
    if (key in data && data[key] !== '') data[key] = Number.parseInt(data[key], 10);
  }
  if ('detection_interval_seconds' in data && data.detection_interval_seconds !== '') data.detection_interval_seconds = Number(data.detection_interval_seconds);
  if ('event_debounce_seconds' in data && data.event_debounce_seconds !== '') data.event_debounce_seconds = Number(data.event_debounce_seconds);
      if ('detection_history_minutes' in data && data.detection_history_minutes !== '') data.detection_history_minutes = Number(data.detection_history_minutes);
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

function pushPayload(form) {
  const data = Object.fromEntries(new FormData(form).entries());
  if ('enabled' in data) data.enabled = data.enabled === 'true';
  return data;
}

function renderPush(settings) {
  if (!pushForm) return;
  for (const [key, value] of Object.entries(settings || {})) {
    if (pushForm.elements[key]) pushForm.elements[key].value = String(value ?? '');
  }
  if (!pushForm.elements.server_url.value) pushForm.elements.server_url.value = 'https://ntfy.sh';
  if (!pushForm.elements.priority.value) pushForm.elements.priority.value = 'default';
}

function renderCameraOffline(settings) {
  const form = document.getElementById('cameraOfflineForm');
  if (!form) return;
  for (const [key, value] of Object.entries(settings || {})) {
    if (form.elements[key]) form.elements[key].value = String(value ?? '');
  }
}


async function loadSettings() {
  const me = await api('/api/auth/me');
  csrfToken = me.csrf_token;
  const [settings, emailSettings, pushSettings, cameraOfflineSettings] = await Promise.all([
    api('/api/settings/system'),
    api('/api/settings/alert-email'),
    api('/api/settings/alert-push'),
    api('/api/settings/camera-offline'),
  ]);
  fillForm(forms.live, settings.live);
  fillForm(forms.recording, settings.recording);
  fillForm(forms.retention, settings.recording);
  fillForm(forms.storage, settings.storage);
  fillForm(forms.auth, settings.auth);
  renderEmail(emailSettings);
  renderPush(pushSettings);
  renderCameraOffline(cameraOfflineSettings);
  enhanceFormFieldLabels();
  messageEl.textContent = '';
}

function bindForm(name, label, endpointName = name) {
  forms[name].addEventListener('submit', async (event) => {
    event.preventDefault();
    try {
      const updated = await api(`/api/settings/system/${endpointName}`, { method: 'PUT', body: JSON.stringify(payloadFor(forms[name])) });
      fillForm(forms[name], updated);
      setMessage(`${label} settings saved.`);
    } catch (error) {
      setMessage(error.message, true);
    }
  });
}

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
    setMessage(error.message, true);
  }
});

pushForm?.addEventListener('submit', async (event) => {
  event.preventDefault();
  try {
    renderPush(await api('/api/settings/alert-push', { method: 'PUT', body: JSON.stringify(pushPayload(pushForm)) }));
    setMessage('Push notification settings saved.');
  } catch (error) {
    setMessage(error.message, true);
  }
});

document.getElementById('cameraOfflineForm')?.addEventListener('submit', async (event) => {
  event.preventDefault();
  const form = document.getElementById('cameraOfflineForm');
  try {
    const data = {
      enabled: form.elements.enabled.value === 'true',
      offline_delay_minutes: Number.parseInt(form.elements.offline_delay_minutes.value, 10) || 1,
    };
    await api('/api/settings/camera-offline', { method: 'PUT', body: JSON.stringify(data) });
    setMessage('Camera offline alert settings saved.');
  } catch (error) {
    setMessage(error.message, true);
  }
});

testPushBtn?.addEventListener('click', async () => {
  testPushBtn.disabled = true;
  setMessage('Sending test notification...');
  try {
    await api('/api/settings/alert-push/test', {
      method: 'POST',
      body: JSON.stringify({ settings: pushPayload(pushForm) }),
    });
    setMessage('Test notification sent.');
  } catch (error) {
    setMessage(error.message, true);
  } finally {
    testPushBtn.disabled = false;
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
    setMessage(error.message, true);
  } finally {
    testEmailBtn.disabled = false;
  }
});

document.getElementById('purgeRecordingsBtn').addEventListener('click', async () => {
  try {
    const result = await api('/api/recordings/purge', { method: 'POST' });
    setMessage(`Purged ${result.purged} recording(s), deleted ${result.files_deleted} file(s).`);
  } catch (error) {
    setMessage(error.message, true);
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
    setMessage(error.message, true);
  }
});


loadSettings().catch((error) => setMessage(error.message, true));

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
  const confirmed = confirm('Start clean now? This permanently deletes events, recordings, and alerts, while keeping settings and users.');
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
      `Clean start complete. Deleted ${Number(deleted.recordings || 0)} recordings, ${Number(deleted.events || 0)} events, and ${Number(deleted.alerts || 0)} alerts. Settings were preserved.`,
    );
  } catch (error) {
    setMessage(error.message, true);
  } finally {
    startCleanBtn.disabled = false;
  }
});
