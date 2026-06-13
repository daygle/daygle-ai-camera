let csrfToken = null;
let cameras = [];
let soundClasses = [];
let selectedCameraId = '';
let editingSound = { enabled: false, rules: [] };
let selectedStatus = null;

const cameraSelect = document.getElementById('soundCameraSelect');
const soundEnabled = document.getElementById('soundEnabled');
const addRuleSelect = document.getElementById('addSoundRuleSelect');
const rulesWrap = document.getElementById('soundRulesWrap');
const statusPanel = document.getElementById('soundStatusPanel');
const messageEl = document.getElementById('soundMessage');
const saveBtn = document.getElementById('saveSoundSettingsBtn');
const reloadBtn = document.getElementById('reloadSoundsBtn');

function setMessage(text, isError = false) {
  messageEl.textContent = text || '';
  messageEl.className = isError ? 'error' : 'muted cameras-list-status';
  if (text) window.showToast?.(text, isError);
}

async function api(path, options = {}) {
  const headers = { ...(options.headers || {}) };
  if (csrfToken && ['POST', 'PUT', 'PATCH', 'DELETE'].includes((options.method || 'GET').toUpperCase())) {
    headers['X-CSRF-Token'] = csrfToken;
  }
  if (options.body && !(options.body instanceof FormData) && !headers['Content-Type']) {
    headers['Content-Type'] = 'application/json';
  }
  const res = await fetch(path, { ...options, headers });
  if (res.status === 401) {
    window.location.href = '/login';
    return {};
  }
  const payload = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(payload.detail || `Request failed: ${res.status}`);
  return payload;
}

function cloneSound(sound) {
  return JSON.parse(JSON.stringify(sound || { enabled: false, rules: [] }));
}

function currentCamera() {
  return cameras.find((camera) => camera.id === selectedCameraId) || cameras[0] || null;
}

function normalisedSound(sound) {
  const next = cloneSound(sound);
  if (!Array.isArray(next.rules)) next.rules = [];
  next.enabled = next.enabled === true;
  return next;
}

function defaultSoundRule(cls) {
  return {
    class: cls.id,
    name: cls.label,
    enabled: true,
    record_on_detect: true,
    confidence_threshold: cls.default_threshold,
    cooldown_seconds: cls.default_cooldown,
    email_enabled: false,
    push_enabled: true,
  };
}

function activeRuleIds() {
  return new Set((editingSound.rules || []).map((rule) => rule.class));
}

function renderCameraSelect() {
  if (!cameras.length) {
    cameraSelect.innerHTML = '<option value="">No cameras configured</option>';
    cameraSelect.disabled = true;
    return;
  }
  cameraSelect.disabled = false;
  cameraSelect.innerHTML = cameras.map((camera) => {
    const label = camera.name || camera.id || 'Camera';
    return `<option value="${escapeHtml(camera.id || '')}" ${camera.id === selectedCameraId ? 'selected' : ''}>${escapeHtml(label)} (${escapeHtml(camera.id || '')})</option>`;
  }).join('');
}

function renderStatus() {
  if (!statusPanel) return;
  const camera = currentCamera();
  if (!camera) {
    statusPanel.innerHTML = '<p class="muted empty-message">No cameras are configured.</p>';
    return;
  }
  const backend = selectedStatus?.backend || 'none';
  const running = selectedStatus?.running ? 'Yes' : 'No';
  const detail = selectedStatus?.backend_reason || selectedStatus?.status_detail || 'No backend status available.';
  const rules = (editingSound.rules || []).filter((rule) => rule.enabled).length;
  statusPanel.innerHTML = `
    <div><span>Backend</span><strong>${escapeHtml(titleCase(backend))}</strong></div>
    <div><span>Running</span><strong>${running}</strong></div>
    <div><span>Enabled Rules</span><strong>${rules}</strong></div>
    <div class="wide"><span>Status Detail</span><strong>${escapeHtml(detail)}</strong></div>`;
}

function renderAddRuleSelect() {
  const active = activeRuleIds();
  const available = soundClasses.filter((cls) => !active.has(cls.id));
  const options = available.map((cls) => `<option value="${escapeHtml(cls.id)}">${escapeHtml(cls.label)}</option>`).join('');
  addRuleSelect.innerHTML = `<option value="">Add Sound...</option>${options}`;
  addRuleSelect.disabled = !available.length || !currentCamera();
}

function updateRule(classId, field, value) {
  const rule = (editingSound.rules || []).find((item) => item.class === classId);
  if (!rule) return;
  rule[field] = value;
  renderStatus();
}

function renderRules() {
  if (!currentCamera()) {
    rulesWrap.innerHTML = '<p class="muted empty-message">Add a camera before configuring sound detection.</p>';
    return;
  }
  const rows = (editingSound.rules || []).map((rule) => {
    const cls = soundClasses.find((item) => item.id === rule.class);
    const label = cls ? cls.label : titleCase(String(rule.class || '').replace(/_/g, ' '));
    return `
      <tr data-sound-class="${escapeHtml(rule.class)}">
        <td class="cell-label">${escapeHtml(label)}</td>
        <td class="cell-center"><input type="checkbox" data-rule-enabled="${escapeHtml(rule.class)}" ${rule.enabled ? 'checked' : ''} /></td>
        <td class="cell-center"><input type="checkbox" data-rule-record="${escapeHtml(rule.class)}" ${rule.record_on_detect !== false ? 'checked' : ''} /></td>
        <td><input type="number" data-rule-threshold="${escapeHtml(rule.class)}" value="${escapeHtml(rule.confidence_threshold ?? 0.35)}" min="0.1" max="1.0" step="0.05" /></td>
        <td><input type="number" data-rule-cooldown="${escapeHtml(rule.class)}" value="${escapeHtml(rule.cooldown_seconds ?? 30)}" min="5" max="3600" step="5" /></td>
        <td class="cell-center"><input type="checkbox" data-rule-email="${escapeHtml(rule.class)}" ${rule.email_enabled ? 'checked' : ''} /></td>
        <td class="cell-center"><input type="checkbox" data-rule-push="${escapeHtml(rule.class)}" ${rule.push_enabled ? 'checked' : ''} /></td>
        <td class="cell-center"><button class="secondary delete-btn" type="button" data-remove-rule="${escapeHtml(rule.class)}">Remove</button></td>
      </tr>`;
  }).join('');

  if (!rows) {
    rulesWrap.innerHTML = '<p class="muted empty-message">No sound rules configured. Use the dropdown above to add one.</p>';
    return;
  }

  rulesWrap.innerHTML = `
    <table class="rule-table">
      <thead>
        <tr>
          <th>Sound</th>
          <th class="cell-center">Enabled</th>
          <th class="cell-center">Record</th>
          <th>Threshold</th>
          <th>Cooldown (s)</th>
          <th class="cell-center">Email</th>
          <th class="cell-center">Push</th>
          <th></th>
        </tr>
      </thead>
      <tbody>${rows}</tbody>
    </table>`;

  rulesWrap.querySelectorAll('[data-rule-enabled]').forEach((input) => {
    input.addEventListener('change', () => updateRule(input.dataset.ruleEnabled, 'enabled', input.checked));
  });
  rulesWrap.querySelectorAll('[data-rule-record]').forEach((input) => {
    input.addEventListener('change', () => updateRule(input.dataset.ruleRecord, 'record_on_detect', input.checked));
  });
  rulesWrap.querySelectorAll('[data-rule-threshold]').forEach((input) => {
    input.addEventListener('change', () => updateRule(input.dataset.ruleThreshold, 'confidence_threshold', Math.max(0.1, Math.min(1.0, Number(input.value) || 0.35))));
  });
  rulesWrap.querySelectorAll('[data-rule-cooldown]').forEach((input) => {
    input.addEventListener('change', () => updateRule(input.dataset.ruleCooldown, 'cooldown_seconds', Math.max(5, Number.parseInt(input.value, 10) || 30)));
  });
  rulesWrap.querySelectorAll('[data-rule-email]').forEach((input) => {
    input.addEventListener('change', () => updateRule(input.dataset.ruleEmail, 'email_enabled', input.checked));
  });
  rulesWrap.querySelectorAll('[data-rule-push]').forEach((input) => {
    input.addEventListener('change', () => updateRule(input.dataset.rulePush, 'push_enabled', input.checked));
  });
  rulesWrap.querySelectorAll('[data-remove-rule]').forEach((button) => {
    button.addEventListener('click', () => {
      editingSound.rules = editingSound.rules.filter((rule) => rule.class !== button.dataset.removeRule);
      renderEditor();
    });
  });
}

function renderEditor() {
  const camera = currentCamera();
  renderCameraSelect();
  soundEnabled.disabled = !camera;
  saveBtn.disabled = !camera;
  reloadBtn.disabled = !camera;
  soundEnabled.value = String(editingSound.enabled === true);
  renderStatus();
  renderAddRuleSelect();
  renderRules();
}

async function refreshStatus() {
  const camera = currentCamera();
  if (!camera) {
    selectedStatus = null;
    renderEditor();
    return;
  }
  try {
    selectedStatus = await api(`/api/sound/status?camera_id=${encodeURIComponent(camera.id || '')}`);
  } catch (err) {
    selectedStatus = { backend_reason: err.message, running: false, backend: 'none' };
  }
  renderEditor();
}

function selectCamera(cameraId) {
  selectedCameraId = cameraId;
  const camera = currentCamera();
  editingSound = normalisedSound(camera?.detection?.sound);
  refreshStatus();
}

async function saveSounds() {
  const camera = currentCamera();
  if (!camera) return;
  const updatedCameras = cameras.map((item) => {
    if (item.id !== camera.id) return item;
    return {
      ...item,
      detection: {
        ...(item.detection || {}),
        sound: normalisedSound(editingSound),
      },
    };
  });

  saveBtn.disabled = true;
  try {
    const result = await api('/api/cameras', {
      method: 'PUT',
      body: JSON.stringify({ cameras: updatedCameras }),
    });
    cameras = result.cameras || updatedCameras;
    const saved = currentCamera();
    editingSound = normalisedSound(saved?.detection?.sound);
    setMessage('Sound settings saved.');
    await refreshStatus();
  } catch (err) {
    setMessage(err.message, true);
  } finally {
    saveBtn.disabled = false;
  }
}

async function loadSounds() {
  const me = await api('/api/auth/me');
  csrfToken = me.csrf_token;
  const [settings, classesPayload] = await Promise.all([
    api('/api/settings/system'),
    api('/api/sound/classes'),
  ]);
  cameras = settings.cameras || (settings.camera ? [settings.camera] : []);
  soundClasses = classesPayload.classes || [];
  const requested = new URLSearchParams(window.location.search).get('camera');
  selectedCameraId = requested && cameras.some((camera) => camera.id === requested)
    ? requested
    : (selectedCameraId || cameras[0]?.id || '');
  const camera = currentCamera();
  editingSound = normalisedSound(camera?.detection?.sound);
  await refreshStatus();
}

cameraSelect.addEventListener('change', () => selectCamera(cameraSelect.value));
soundEnabled.addEventListener('change', () => {
  editingSound.enabled = soundEnabled.value === 'true';
  renderStatus();
});
addRuleSelect.addEventListener('change', () => {
  const classId = addRuleSelect.value;
  if (!classId) return;
  const cls = soundClasses.find((item) => item.id === classId);
  if (!cls || activeRuleIds().has(classId)) return;
  editingSound.rules.push(defaultSoundRule(cls));
  renderEditor();
});
saveBtn.addEventListener('click', saveSounds);
reloadBtn.addEventListener('click', () => loadSounds().catch((err) => setMessage(err.message, true)));

loadSounds().catch((err) => setMessage(err.message, true));
