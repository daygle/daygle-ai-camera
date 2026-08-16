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
  'cloudflareTunnelState', 'cloudflareTunnelDetail', 'cloudflareTunnelTokenSummary',
  'cloudflareTunnelTokenIndicator',
]);
const messageEl = document.getElementById('systemMessage');
let cloudflareTunnelServerStatus = null;
let cloudflareTunnelTokenDraftTouched = false;

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
  detection_confirm_frames: 'Confirm Frames',
  detection_confirm_window: 'Confirm Window',
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
  trusted_proxies: 'Trusted Proxy IPs',
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

function normalizeShadowSuppression(value) {
  if (typeof value === 'boolean') return value ? 'on' : 'off';
  const normalized = String(value ?? '').trim().toLowerCase();
  return ['on', 'off', 'auto'].includes(normalized) ? normalized : 'on';
}

// Keep the form usable with a partial/legacy API response as well as a fresh
// install. The backend owns persistence and validation; these values only
// prevent a missing key from leaving a number blank or a select on the wrong
// first option.
const FORM_DEFAULTS = {
  live: {
    snapshot_refresh_ms: 500,
    detection_status_refresh_ms: 2000,
    detection_interval_seconds: 0.5,
    event_debounce_seconds: 10,
    detection_confirm_frames: 2,
    detection_confirm_window: 3,
    background_detection_enabled: 'true',
    always_run_object_detection: 'true',
    object_detection_region_boost: 'false',
    object_detection_tiling: 'off',
    detection_history_minutes: 10,
    ingest_frame_fps: 4,
    snapshot_quality: 2,
    motion_algorithm: 'mog2',
    motion_denoise: 'true',
    motion_shadow_suppression: 'on',
    periodic_scan_interval_seconds: 0,
    motion_pixel_threshold: 30,
    motion_gate_fraction: 0.005,
    motion_scale_fraction: 0.03,
    motion_background_alpha: 0.05,
    motion_frame_width: 320,
    motion_frame_height: 240,
  },
  recording: {
    pre_event_seconds: 10,
    post_event_seconds: 15,
    extension_step_seconds: 45,
    max_clip_seconds: 300,
    retention_days: 14,
    max_storage_gb: 20,
    auto_purge_enabled: 'true',
  },
  storage: {
    data_dir: 'data',
    snapshots_dir: 'data/snapshots',
    events_dir: 'data/events',
    recordings_dir: 'data/recordings',
  },
  auth: {
    session_timeout_hours: 12,
    max_login_attempts: 5,
    lockout_minutes: 15,
    trusted_proxies: ['127.0.0.1', '::1'],
  },
  email: {
    enabled: 'false',
    host: '',
    port: 587,
    username: '',
    password: '',
    from_address: '',
    use_tls: 'true',
    use_ssl: 'false',
  },
  push: {
    enabled: 'false',
    server_url: 'https://ntfy.sh',
    topic: '',
    priority: 'default',
    username: '',
    password: '',
  },
  cameraOffline: {
    enabled: 'false',
    offline_delay_minutes: 1,
  },
};

function fillForm(form, values, defaults = {}) {
  if (!form) return;
  const source = values || {};
  const keys = new Set([...Object.keys(defaults), ...Object.keys(source)]);
  for (const key of keys) {
    const field = form.elements[key];
    if (!field) continue;
    const value = source[key] ?? defaults[key] ?? '';
    field.value = key === 'motion_shadow_suppression'
      ? normalizeShadowSuppression(value)
      : String(value ?? '');
  }
}

// Declarative field types. A form's payload is derived by looking each field
// up here instead of maintaining a parallel list of keys per coercion kind.
// To add a setting, add its name to the matching set once.
const FIELD_TYPES = {
  boolean: new Set([
    'enabled', 'continuous', 'auto_purge_enabled', 'background_detection_enabled',
    'always_run_object_detection', 'object_detection_region_boost', 'motion_denoise',
    'use_tls', 'use_ssl', 'autostart', 'tunnel_loopback_only',
  ]),
  integer: new Set([
    'width', 'height', 'fps', 'port', 'pre_event_seconds', 'post_event_seconds',
    'extension_step_seconds', 'max_clip_seconds', 'retention_days', 'max_storage_gb',
    'max_login_attempts', 'lockout_minutes', 'snapshot_refresh_ms',
    'detection_status_refresh_ms', 'motion_pixel_threshold',
    'periodic_scan_interval_seconds', 'motion_frame_width', 'motion_frame_height',
    'ingest_frame_fps', 'snapshot_quality', 'offline_delay_minutes',
    'detection_confirm_frames', 'detection_confirm_window',
  ]),
  number: new Set([
    'detection_interval_seconds', 'event_debounce_seconds', 'detection_history_minutes',
    'motion_gate_fraction', 'motion_scale_fraction', 'motion_background_alpha',
    'session_timeout_hours',
  ]),
  csv: new Set(['vehicle_labels', 'trusted_proxies']),
  triState: new Set(['motion_shadow_suppression']),
};

// Coerce a raw FormData object into the typed payload the API expects.
// Booleans and CSV lists always convert; numbers are left untouched when blank
// so an empty field is not sent as 0.
function coercePayload(data) {
  for (const [key, value] of Object.entries(data)) {
    if (FIELD_TYPES.boolean.has(key)) data[key] = value === 'true';
    else if (FIELD_TYPES.triState.has(key)) data[key] = normalizeShadowSuppression(value);
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
  fillForm(emailForm, settings, FORM_DEFAULTS.email);
  if (testEmailRecipient && !testEmailRecipient.value) testEmailRecipient.value = settings?.from_address || '';
}

function pushPayload(form) {
  return payloadFor(form);
}

function renderPush(settings) {
  if (!pushForm) return;
  fillForm(pushForm, settings, FORM_DEFAULTS.push);
}

function renderCameraOffline(settings) {
  const form = document.getElementById('cameraOfflineForm');
  fillForm(form, settings, FORM_DEFAULTS.cameraOffline);
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
  fillForm(forms.live, settings.live, FORM_DEFAULTS.live);
  fillForm(forms.recording, settings.recording, FORM_DEFAULTS.recording);
  fillForm(forms.retention, settings.recording, FORM_DEFAULTS.recording);
  fillForm(forms.storage, settings.storage, FORM_DEFAULTS.storage);
  fillForm(forms.auth, settings.auth, FORM_DEFAULTS.auth);
  renderEmail(emailSettings);
  renderPush(pushSettings);
  renderCameraOffline(cameraOfflineSettings);
  renderCloudflareTunnel(settings.cloudflare_tunnel);
  enhanceFormFieldLabels();
  messageEl.textContent = '';
}

function setCloudflareTokenIndicator(configured, hasDraft = false, source = '', willClear = false) {
  const indicator = document.getElementById('cloudflareTunnelTokenIndicator');
  if (!indicator) return;
  indicator.className = 'cloudflare-token-indicator';
  if (hasDraft) {
    indicator.textContent = 'Unsaved Token';
    indicator.classList.add('cloudflare-token-indicator-draft');
  } else if (willClear) {
    indicator.textContent = 'Will Be Removed on Save';
    indicator.classList.add('cloudflare-token-indicator-draft');
  } else if (configured && source === 'database') {
    indicator.textContent = 'Saved Securely';
    indicator.classList.add('cloudflare-token-indicator-saved');
  } else if (configured) {
    indicator.textContent = 'Configured Externally';
    indicator.classList.add('cloudflare-token-indicator-external');
  } else {
    indicator.textContent = 'Not Saved';
    indicator.classList.add('cloudflare-token-indicator-empty');
  }
}

function renderCloudflareTunnel(status) {
  const statusEl = document.getElementById('cloudflareTunnelStatus');
  const stateEl = document.getElementById('cloudflareTunnelState');
  const detailEl = document.getElementById('cloudflareTunnelDetail');
  const tokenSummaryEl = document.getElementById('cloudflareTunnelTokenSummary');
  if (!statusEl || !stateEl || !detailEl || !tokenSummaryEl) return;

  cloudflareTunnelServerStatus = status || null;
  const configured = Boolean(status?.configured);
  const running = Boolean(status?.running);
  const error = String(status?.error || '').trim();
  const source = String(status?.source || '');
  const tokenDescription = source === 'database'
    ? 'Token Saved Securely.'
    : 'Token Is Configured Outside This Page.';
  let state;
  let detail;
  let tone;
  if (running) {
    state = 'Tunnel Is Running';
    detail = `Cloudflared Is Running${status?.pid ? ` (process ${status.pid})` : ''}.`;
    tone = 'status-ok';
  } else if (!configured) {
    state = 'Tunnel Is Not Configured';
    detail = 'Add a Cloudflare Token and Save It Before Starting the Service.';
    tone = 'status-warning';
  } else if (error) {
    state = 'Tunnel Needs Attention';
    detail = `${tokenDescription} Cloudflared Is Not Running: ${error}`;
    tone = 'status-error';
  } else {
    state = 'Tunnel Is Stopped';
    detail = `${tokenDescription} Start the Tunnel When You Are Ready to Publish Daygle.`;
    tone = 'status-warning';
  }

  statusEl.className = `status-panel cloudflare-tunnel-status ${tone}`;
  stateEl.textContent = state;
  detailEl.textContent = detail;
  tokenSummaryEl.textContent = configured
    ? (source === 'database' ? 'Token Status: Saved Securely' : 'Token Status: Configured Externally')
    : 'Token Status: Not Configured';
  const draft = forms.cloudflareTunnel.elements.token.value.trim();
  setCloudflareTokenIndicator(
    configured,
    Boolean(draft),
    source,
    cloudflareTunnelTokenDraftTouched && !draft && configured,
  );

  const autostart = forms.cloudflareTunnel?.elements.autostart;
  if (autostart && status?.autostart != null) autostart.value = status.autostart ? 'true' : 'false';

  const lanToggle = forms.cloudflareTunnel?.elements.tunnel_loopback_only;
  if (lanToggle && status?.tunnel_loopback_only != null) {
    lanToggle.value = status.tunnel_loopback_only ? 'true' : 'false';
  }

  const startBtn = document.getElementById('startCloudflareTunnelBtn');
  const stopBtn = document.getElementById('stopCloudflareTunnelBtn');
  const restartBtn = document.getElementById('restartCloudflareTunnelBtn');
  if (startBtn) startBtn.disabled = !configured || running;
  if (stopBtn) stopBtn.disabled = !running;
  if (restartBtn) restartBtn.disabled = !configured;
}

async function refreshCloudflareTunnel() {
  const status = await api('/api/settings/system/cloudflare-tunnel');
  renderCloudflareTunnel(status);
  return status;
}

function renderCloudflareTunnelReadError(error) {
  const statusEl = document.getElementById('cloudflareTunnelStatus');
  const stateEl = document.getElementById('cloudflareTunnelState');
  const detailEl = document.getElementById('cloudflareTunnelDetail');
  if (!statusEl || !stateEl || !detailEl) return;
  statusEl.className = 'status-panel cloudflare-tunnel-status status-error';
  stateEl.textContent = 'Tunnel Status Unavailable';
  detailEl.textContent = `Could Not Read the Cloudflare Service Status: ${error?.message || 'Unknown Error'}`;
  const tokenSummaryEl = document.getElementById('cloudflareTunnelTokenSummary');
  if (tokenSummaryEl) tokenSummaryEl.textContent = 'Token Status: Unavailable';
  setCloudflareTokenIndicator(false);
  for (const id of ['startCloudflareTunnelBtn', 'stopCloudflareTunnelBtn', 'restartCloudflareTunnelBtn']) {
    const button = document.getElementById(id);
    if (button) button.disabled = true;
  }
}

forms.cloudflareTunnel?.elements.token.addEventListener('input', () => {
  cloudflareTunnelTokenDraftTouched = true;
  const draft = forms.cloudflareTunnel.elements.token.value.trim();
  const current = cloudflareTunnelServerStatus;
  setCloudflareTokenIndicator(
    Boolean(current?.configured),
    Boolean(draft),
    String(current?.source || ''),
    !draft && Boolean(current?.configured),
  );
});

forms.cloudflareTunnel?.addEventListener('submit', guard(async (event) => {
  event.preventDefault();
  const saveBtn = document.getElementById('saveCloudflareTunnelBtn');
  if (saveBtn) saveBtn.disabled = true;
  try {
    const data = payloadFor(forms.cloudflareTunnel);
    const saved = await api('/api/settings/system/cloudflare-tunnel', {
      method: 'PUT',
      body: JSON.stringify(data),
    });
    forms.cloudflareTunnel.elements.token.value = '';
    cloudflareTunnelTokenDraftTouched = false;
    renderCloudflareTunnel(saved);
    setMessage('Cloudflare Tunnel settings saved.');
  } finally {
    if (saveBtn) saveBtn.disabled = false;
  }
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
      const defaults = name === 'retention' ? FORM_DEFAULTS.recording : FORM_DEFAULTS[name];
      fillForm(forms[name], updated, defaults);
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
  const updated = await api('/api/settings/camera-offline', { method: 'PUT', body: JSON.stringify(data) });
  renderCameraOffline(updated);
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
  setMessage('Generating full backup (database + recordings + snapshots + models)...');
});

forms.databaseRestore.addEventListener('submit', guard(async (event) => {
  event.preventDefault();
  if (!window.confirm('Restore this backup? A database backup replaces site data. A full ZIP backup also restores recordings, snapshots, and model assets. The current state is saved to a safety backup first.')) return;
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

function refreshCloudflareTunnelSafely() {
  return refreshCloudflareTunnel().catch((error) => {
    if (!window.daygleAuth?.redirecting) renderCloudflareTunnelReadError(error);
  });
}

refreshCloudflareTunnelSafely();
setInterval(() => {
  if (!document.hidden) refreshCloudflareTunnelSafely();
}, 15000);
