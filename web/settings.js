// requireElements() is provided by web/utils.js (loaded before this script).
// Fail loud if a future HTML refactor removes any of these ids so we don't
// crash with a cryptic TypeError on the first form submit below. Element
// refs further down (cameraOfflineForm, purgeRecordingsBtn, updateStatus,
// etc.) live inside their own IIFE blocks which keep the surrounding
// code defensive; only the page-spanning refs live up here.
requireElements([
  'systemMessage',
  'emailSettingsForm', 'testEmailRecipient', 'testEmailBtn',
  'pushSettingsForm', 'testPushBtn', 'startCleanBtn',
  'cloudflareTunnelForm', 'cloudflareTunnelStatus',
]);
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
  snapshot_refresh_ms: 'Snapshot Refresh (ms)',
  detection_status_refresh_ms: 'Detection Status Refresh (ms)',
  detection_interval_seconds: 'Detection Interval (s)',
  event_debounce_seconds: 'Event Merge Window (s)',
  detection_history_minutes: 'Detection History (min)',
  background_detection_enabled: 'Background Detection',
  periodic_scan_interval_seconds: 'Periodic Scan Interval (s)',
  motion_pixel_threshold: 'Motion Pixel Threshold',
  motion_gate_fraction: 'Motion Gate Fraction',
  motion_scale_fraction: 'Motion Scale Fraction',
  motion_background_alpha: 'Motion Background Alpha',
  motion_frame_width: 'Motion Frame Width',
  motion_frame_height: 'Motion Frame Height',
  ingest_frame_fps: 'Detection Frame Rate (fps)',
  snapshot_quality: 'Snapshot Quality',
  data_dir: 'Data Directory',
  snapshots_dir: 'Snapshots Directory',
  events_dir: 'Events Directory',
  recordings_dir: 'Recordings Directory',
  session_timeout_hours: 'Session Timeout Hours',
  max_login_attempts: 'Max Login Attempts',
  lockout_minutes: 'Lockout Minutes',
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
  post_event_seconds: 'Minimum Post-Event Seconds',
  extension_step_seconds: 'Keep Recording After Motion (s)',
  max_clip_seconds: 'Max Clip Duration (s)',
  retention_days: 'Retention Days',
  max_storage_gb: 'Max Storage GB',
  auto_purge_enabled: 'Auto Purge',
  enabled: 'Enabled',
  continuous: 'Continuous Recording',
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
  cloudflareTunnel: document.getElementById('cloudflareTunnelForm'),
};

// api() is provided by web/utils.js (loaded before this script). It reads
// window.daygleAuth.csrfToken for state-changing verbs, redirects to /login
// on 401, and sets Content-Type: application/json on JSON bodies. The local
// duplicate + page-local csrfToken were removed so every page shares the
// same fetch contract.

function setMessage(text, isError = false) {
  messageEl.textContent = text;
  if (text) window.showToast?.(text, isError);
}

// Wraps an async handler so the "if api() is doing a 401 redirect, bail;
// otherwise surface the error" tail lives in one place instead of being
// repeated in every submit/click handler on the page.
function guard(fn) {
  return async (...args) => {
    try {
      await fn(...args);
    } catch (error) {
      if (window.daygleAuth?.redirecting) return;
      setMessage(error.message, true);
    }
  };
}

function fillForm(form, values) {
  for (const [key, value] of Object.entries(values || {})) {
    if (form.elements[key]) form.elements[key].value = String(value ?? '');
  }
}

// Declarative field types. A form's payload is derived by looking each field
// up here instead of maintaining a parallel list of keys per coercion kind.
// To add a setting, add its name to the matching set once.
const FIELD_TYPES = {
  boolean: new Set([
    'enabled', 'continuous', 'auto_purge_enabled', 'background_detection_enabled',
    'use_tls', 'use_ssl', 'autostart',
  ]),
  integer: new Set([
    'width', 'height', 'fps', 'port', 'pre_event_seconds', 'post_event_seconds',
    'extension_step_seconds', 'max_clip_seconds', 'retention_days', 'max_storage_gb',
    'max_login_attempts', 'lockout_minutes', 'snapshot_refresh_ms',
    'detection_status_refresh_ms', 'motion_pixel_threshold',
    'periodic_scan_interval_seconds', 'motion_frame_width', 'motion_frame_height',
    'ingest_frame_fps', 'snapshot_quality', 'offline_delay_minutes',
  ]),
  number: new Set([
    'detection_interval_seconds', 'event_debounce_seconds', 'detection_history_minutes',
    'motion_gate_fraction', 'motion_scale_fraction', 'motion_background_alpha',
    'session_timeout_hours',
  ]),
  csv: new Set(['vehicle_labels']),
};

// Coerce a raw FormData object into the typed payload the API expects.
// Booleans and CSV lists always convert; numbers are left untouched when blank
// so an empty field is not sent as 0.
function coercePayload(data) {
  for (const [key, value] of Object.entries(data)) {
    if (FIELD_TYPES.boolean.has(key)) data[key] = value === 'true';
    else if (FIELD_TYPES.csv.has(key)) data[key] = String(value).split(',').map((item) => item.trim()).filter(Boolean);
    else if (value === '') continue;
    else if (FIELD_TYPES.integer.has(key)) data[key] = Number.parseInt(value, 10);
    else if (FIELD_TYPES.number.has(key)) data[key] = Number(value);
  }
  return data;
}

function payloadFor(form) {
  return coercePayload(Object.fromEntries(new FormData(form).entries()));
}

function emailPayload(form) {
  return payloadFor(form);
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
  return payloadFor(form);
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
  // nav.js's daygleAuthReady IIFE has already fetched /api/auth/me once at
  // script-load time and populated window.daygleAuth.{user, csrfToken}; the
  // shared api() below picks up the CSRF token automatically.
  await window.daygleAuthReady;
  const [settings, emailSettings, pushSettings, cameraOfflineSettings] = await Promise.all([
    api('/api/settings/system'),
    api('/api/settings/alert-email'),
    api('/api/settings/alert-push'),
    api('/api/settings/camera-offline'),
  ]);
  const versionEl = document.getElementById('currentVersion');
  if (versionEl && settings.version) versionEl.textContent = settings.version;
  fillForm(forms.live, settings.live);
  fillForm(forms.recording, settings.recording);
  fillForm(forms.retention, settings.recording);
  fillForm(forms.storage, settings.storage);
  fillForm(forms.auth, settings.auth);
  renderEmail(emailSettings);
  renderPush(pushSettings);
  renderCameraOffline(cameraOfflineSettings);
  renderCloudflareTunnel(settings.cloudflare_tunnel);
  enhanceFormFieldLabels();
  messageEl.textContent = '';
}

function renderCloudflareTunnel(status) {
  const statusEl = document.getElementById('cloudflareTunnelStatus');
  if (!statusEl) return;
  const state = status?.running ? 'Running' : status?.configured ? 'Stopped' : 'Not configured';
  statusEl.textContent = `${state}${status?.source ? ` · ${status.source}` : ''}${status?.error ? ` · ${status.error}` : ''}`;
  const autostart = forms.cloudflareTunnel?.elements.autostart;
  if (autostart && status?.autostart != null) autostart.value = status.autostart ? 'true' : 'false';
}

async function refreshCloudflareTunnel() {
  const status = await api('/api/settings/system/cloudflare-tunnel');
  renderCloudflareTunnel(status);
  return status;
}

forms.cloudflareTunnel?.addEventListener('submit', guard(async (event) => {
  event.preventDefault();
  const data = payloadFor(forms.cloudflareTunnel);
  const saved = await api('/api/settings/system/cloudflare-tunnel', {
    method: 'PUT',
    body: JSON.stringify(data),
  });
  forms.cloudflareTunnel.elements.token.value = '';
  renderCloudflareTunnel(saved);
  setMessage('Cloudflare Tunnel settings saved.');
}));

async function tunnelAction(action) {
  const result = await api(`/api/settings/system/cloudflare-tunnel/${action}`, { method: 'POST' });
  renderCloudflareTunnel(result);
  setMessage(`Cloudflare Tunnel ${action} requested.`);
}

document.getElementById('startCloudflareTunnelBtn')?.addEventListener('click', guard(() => tunnelAction('start')));
document.getElementById('stopCloudflareTunnelBtn')?.addEventListener('click', guard(() => tunnelAction('stop')));
document.getElementById('restartCloudflareTunnelBtn')?.addEventListener('click', guard(() => tunnelAction('restart')));

function bindForm(name, label, endpointName = name) {
  forms[name].addEventListener('submit', guard(async (event) => {
    event.preventDefault();
    const btn = forms[name].querySelector('[type="submit"]');
    if (btn) btn.disabled = true;
    try {
      const updated = await api(`/api/settings/system/${endpointName}`, { method: 'PUT', body: JSON.stringify(payloadFor(forms[name])) });
      fillForm(forms[name], updated);
      setMessage(`${label} settings saved.`);
    } finally {
      if (btn) btn.disabled = false;
    }
  }));
}

bindForm('live', 'Live');
bindForm('recording', 'Recording');
bindForm('retention', 'Retention', 'recording');
bindForm('storage', 'Storage');
bindForm('auth', 'Login security');

emailForm?.addEventListener('submit', guard(async (event) => {
  event.preventDefault();
  renderEmail(await api('/api/settings/alert-email', { method: 'PUT', body: JSON.stringify(emailPayload(emailForm)) }));
  setMessage('Mail server settings saved.');
}));

pushForm?.addEventListener('submit', guard(async (event) => {
  event.preventDefault();
  renderPush(await api('/api/settings/alert-push', { method: 'PUT', body: JSON.stringify(pushPayload(pushForm)) }));
  setMessage('Push notification settings saved.');
}));

document.getElementById('cameraOfflineForm')?.addEventListener('submit', guard(async (event) => {
  event.preventDefault();
  const form = document.getElementById('cameraOfflineForm');
  const data = payloadFor(form);
  // Offline delay must be a positive integer; fall back to 1 when left blank.
  if (!Number.isFinite(data.offline_delay_minutes)) data.offline_delay_minutes = 1;
  await api('/api/settings/camera-offline', { method: 'PUT', body: JSON.stringify(data) });
  setMessage('Camera offline alert settings saved.');
}));

testPushBtn?.addEventListener('click', guard(async () => {
  testPushBtn.disabled = true;
  setMessage('Sending test notification...');
  try {
    await api('/api/settings/alert-push/test', {
      method: 'POST',
      body: JSON.stringify({ settings: pushPayload(pushForm) }),
    });
    setMessage('Test notification sent.');
  } finally {
    testPushBtn.disabled = false;
  }
}));

testEmailBtn?.addEventListener('click', guard(async () => {
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
  } finally {
    testEmailBtn.disabled = false;
  }
}));

document.getElementById('purgeRecordingsBtn').addEventListener('click', guard(async () => {
  const result = await api('/api/recordings/purge', { method: 'POST' });
  setMessage(`Purged ${result.purged} recording(s), deleted ${result.files_deleted} file(s).`);
}));


const fullBackupLink = document.getElementById('fullBackupLink');
fullBackupLink?.addEventListener('click', () => {
  // Full backups zip the database plus every recording/snapshot file, so they
  // can take a while to assemble on large libraries. Keep the user informed
  // while the browser waits for the archive to be generated.
  setMessage('Generating full backup (database + recordings + snapshots)...');
});

forms.databaseRestore.addEventListener('submit', guard(async (event) => {
  event.preventDefault();
  if (!window.confirm('Restore this database backup? This will replace current events, users, settings, alert rules, and sessions.')) return;
  const formData = new FormData(forms.databaseRestore);
  const result = await api('/api/settings/system/database/restore', { method: 'POST', body: formData });
  forms.databaseRestore.reset();
  await loadSettings();
  setMessage(`${result.message} Safety backup: ${result.safety_backup}`);
}));


loadSettings().catch((error) => {
  // Skip UI updates if api() triggered a 401 redirect
  if (window.daygleAuth?.redirecting) return;
  setMessage(error.message, true);
});

function initSoftwareUpdateSection() {
  const checkBtn = document.getElementById('checkUpdateBtn');
  const applyBtn = document.getElementById('applyUpdateBtn');
  const statusEl = document.getElementById('updateStatus');
  const outputEl = document.getElementById('updateOutput');
  if (!checkBtn) return;

  function showUpdateStatus(message, type = '') {
    if (!statusEl) return;
    statusEl.style.display = '';
    // Callers pass HTML-formatted messages (e.g. `<strong>Update
    // available:</strong>`) with every dynamic value routed through
    // escapeHtml() before interpolation, so innerHTML is safe here.
    // Using innerHTML rather than textContent lets semantic markup
    // (bold, links, arrows) render properly for end users.
    statusEl.innerHTML = String(message ?? '');
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
      const result = await api('/api/update/check', { method: 'POST' });
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
      // Skip UI updates if api() triggered a 401 redirect
      if (window.daygleAuth?.redirecting) return;
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
      // Skip UI updates if api() triggered a 401 redirect
      if (window.daygleAuth?.redirecting) return;
      showUpdateStatus(`Update failed: ${escapeHtml(err.message)}`, 'error');
    } finally {
      applyBtn.disabled = false;
      checkBtn.disabled = false;
    }
  });
}

initSoftwareUpdateSection();

startCleanBtn?.addEventListener('click', guard(async () => {
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
  } finally {
    startCleanBtn.disabled = false;
  }
}));

// ---- Tab navigation ----------------------------------------------------
// Groups the settings cards into panels switched by the tab bar. The shared
// implementation (ARIA tabs pattern + URL-hash deep-linking) lives in
// utils.js as initDaygleTabs() so the onnx page can reuse it.
initDaygleTabs();
refreshCloudflareTunnel().catch(() => {});
