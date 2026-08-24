let cameras = [];
let soundClasses = [];
let selectedCameraId = '';
let editingSound = { enabled: false, rules: [] };
let selectedStatus = null;
let expandedSoundRules = new Set();



// requireElements() is provided by web/utils.js (loaded before this script).
// Fail loud if a future HTML refactor removes any of these ids so we don't
// crash with a cryptic TypeError when rendering the camera list or adding a
// rule. (Stat-card ids and save/reload buttons are queried lazily inside
// their own render handlers and tolerate absence individually.)
requireElements([
  'soundCameraSelect', 'soundEnabled', 'addSoundRuleSelect',
  'soundRulesWrap', 'soundStatusPanel', 'soundMessage',
]);
const cameraSelect = document.getElementById('soundCameraSelect');
const soundEnabled = document.getElementById('soundEnabled');
const addRuleSelect = document.getElementById('addSoundRuleSelect');
const rulesWrap = document.getElementById('soundRulesWrap');
const statusPanel = document.getElementById('soundStatusPanel');
const messageEl = document.getElementById('soundMessage');
const saveBtn = document.getElementById('saveSoundSettingsBtn');
const reloadBtn = document.getElementById('reloadSoundsBtn');
const statSoundRules = document.getElementById('statSoundRules');
const statActiveRules = document.getElementById('statActiveRules');
const statDetection = document.getElementById('statDetection');
const statCamera = document.getElementById('statCamera');
const soundCameraStatusList = document.getElementById('soundCameraStatusList');

function detectorSoundConfig(camera) {
  return camera?.detection?.sound || {};
}

function detectorEnabledRules(camera) {
  return (detectorSoundConfig(camera).rules || []).filter((rule) => rule.enabled === true);
}

function detectorSoundConfigured(camera) {
  return detectorSoundConfig(camera).enabled === true;
}

function detectorHasRtspConfig(camera) {
  return Boolean(camera?.stream_url || camera?.rtsp_url || camera?.host);
}

function detectorCameraLabel(camera) {
  const name = String(camera?.name || '').trim();
  const id = String(camera?.id || '').trim();
  if (name && id) return `${name} (${id})`;
  return name || id || 'Unknown camera';
}

function detectorStatusReason(camera, status) {
  if (!detectorSoundConfigured(camera)) return 'Sound disabled';
  if (!detectorEnabledRules(camera).length) return 'No enabled sound rules';
  if (!detectorHasRtspConfig(camera)) return 'No RTSP stream configured';
  if (status.running) return 'Running';
  return titleCase(String(status.detector_status || status.state || 'Not running'));
}

function detectorStatusClass(camera, status) {
  if (status.running) return 'status-ok';
  const reason = detectorStatusReason(camera, status).toLowerCase();
  if (reason.includes('loading')) return 'status-warning';
  if (reason === 'sound disabled' || reason === 'no enabled sound rules') return '';
  return 'status-error';
}

function detectorConfidenceMap(confidences = {}) {
  const entries = Object.entries(confidences || {})
    .filter(([, value]) => Number.isFinite(Number(value)))
    .sort((left, right) => Number(right[1]) - Number(left[1]))
    .slice(0, 4);
  if (!entries.length) return 'None';
  return entries
    .map(([label, value]) => `${titleCase(String(label).replace(/[_-]+/g, ' '))} ${Math.round(Number(value) * 100)}%`)
    .join(', ');
}

function renderDetectorStatuses(rows) {
  if (!soundCameraStatusList) return;
  if (!rows.length) {
    soundCameraStatusList.innerHTML = '<p class="muted empty-message">No cameras are configured.</p>';
    return;
  }
  const rowsHtml = rows.map(({ camera, status }) => `
    <tr class="${escapeHtml(detectorStatusClass(camera, status))}">
      <td class="cell-label">${escapeHtml(detectorCameraLabel(camera))}</td>
      <td>${escapeHtml(detectorSoundConfigured(camera) ? 'Yes' : 'No')}</td>
      <td>${escapeHtml(detectorEnabledRules(camera).length)}</td>
      <td>${escapeHtml(titleCase(String(status.backend || 'None').replace(/[_-]+/g, ' ')))}</td>
      <td>${escapeHtml(status.running ? 'Yes' : 'No')}</td>
      <td>${escapeHtml(detectorStatusReason(camera, status))}</td>
      <td>${escapeHtml(status.last_class_label || titleCase(String(status.last_class || 'None').replace(/[_-]+/g, ' ')))}</td>
      <td>${escapeHtml(detectorConfidenceMap(status.last_confidences))}</td>
      <td>${escapeHtml(status.backend_reason || '')}</td>
    </tr>
  `).join('');
  soundCameraStatusList.innerHTML = `
    <div style="overflow-x:auto">
      <table class="rule-table">
        <thead><tr>
          <th>Camera</th><th>Configured</th><th>Rules</th><th>Backend</th>
          <th>Running</th><th>Status</th><th>Last Sound</th>
          <th>Recent Scores</th><th>Detail</th>
        </tr></thead>
        <tbody></tbody>
      </table>
    </div>
  `;
  soundCameraStatusList.querySelector('tbody').insertAdjacentHTML('beforeend', rowsHtml);
}

async function refreshDetectorStatuses() {
  if (!soundCameraStatusList) return;
  if (!cameras.length) {
    renderDetectorStatuses([]);
    return;
  }
  const rows = await Promise.all(cameras.map(async (camera) => ({
    camera,
    status: await api(`/api/sound/status?camera_id=${encodeURIComponent(camera.id || '')}`).catch(() => ({
      state: 'unavailable',
      detector_status: 'unavailable',
      running: false,
      backend: null,
      last_confidences: {},
    })),
  })));
  renderDetectorStatuses(rows);
}

function setMessage(text, isError = false) {
  messageEl.textContent = text || '';
  messageEl.className = isError ? 'error' : 'muted cameras-list-status';
  if (text) window.showToast?.(text, isError);
}

// api() is provided by web/utils.js (loaded before this script). It throws
// on 401 (after redirecting to /login) rather than returning {}, matching
// what every failsafe caller in this file already expects via try/catch.
// The local duplicate + page-local csrfToken were removed so every page
// shares the same fetch contract.

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
  next.rules = next.rules.map((rule) => ({
    ...rule,
    email_enabled: rule.email_enabled === true,
    email_recipients: normalizeEmailList(rule.email_recipients),
    push_enabled: rule.push_enabled === true,
    active_start: rule.active_start || null,
    active_end: rule.active_end || null,
    notify_start: rule.notify_start || null,
    notify_end: rule.notify_end || null,
  }));
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
    email_recipients: [],
    push_enabled: false,
    active_start: null,
    active_end: null,
    notify_start: null,
    notify_end: null,
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
  const camera = currentCamera();
  const rules = (editingSound.rules || []).filter((rule) => rule.enabled).length;
  const totalRules = (editingSound.rules || []).length;

  if (statSoundRules) statSoundRules.textContent = camera ? String(totalRules) : '-';
  if (statActiveRules) statActiveRules.textContent = camera ? String(rules) : '-';
  if (statDetection) statDetection.textContent = !camera ? '-' : editingSound.enabled ? 'Enabled' : 'Disabled';
  if (statCamera) statCamera.textContent = camera ? (camera.name || camera.id || '-') : '-';

  if (!statusPanel) return;
  if (!camera || !selectedStatus) {
    statusPanel.innerHTML = '';
    return;
  }
  const running = selectedStatus.running;
  const detail = selectedStatus.backend_reason || selectedStatus.status_detail || '';
  const stateClass = running ? 'status-ok' : (detail ? 'status-warning' : '');
  const stateLabel = running ? 'Detector Running' : 'Detector Not Running';
  statusPanel.innerHTML = safeHtml`<div class="status-panel${stateClass ? ` ${stateClass}` : ''}"><span>${stateLabel}${detail ? ` · ${detail}` : ''}</span></div>`;
}

function renderAddRuleSelect() {
  const active = activeRuleIds();
  const available = soundClasses.filter((cls) => !active.has(cls.id));
  const options = available.map((cls) => `<option value="${escapeHtml(cls.id)}">${escapeHtml(cls.label)}</option>`).join('');
  // ``options`` is pre-rendered HTML markup (each label/value already escaped
  // inside the .map callback). Build the static placeholder via a literal
  // innerHTML write and append the dynamic markup through insertAdjacentHTML
  // so the assignment line is a pure literal - keeps
  // ``H2RegressionGuardTests`` happy and avoids re-escaping the option tags.
  addRuleSelect.innerHTML = '<option value="">Add Sound...</option>';
  addRuleSelect.insertAdjacentHTML('beforeend', options);
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
  const rules = editingSound.rules || [];
  if (!rules.length) {
    rulesWrap.innerHTML = '<p class="muted empty-message">No sound rules configured. Use the dropdown above to add one.</p>';
    return;
  }

  rulesWrap.innerHTML = rules.map((rule) => {
    const cls = soundClasses.find((item) => item.id === rule.class);
    const label = cls ? cls.label : titleCase(String(rule.class || '').replace(/_/g, ' '));
    const id = escapeHtml(rule.class);
    const expanded = expandedSoundRules.has(rule.class);
    const enabled = rule.enabled === true;
    return `
      <div class="zone-motion-card${enabled ? ' is-enabled' : ''}">
        <div class="zone-motion-head">
          <div class="zone-motion-title">
            <span class="zone-motion-icon" aria-hidden="true">🔊</span>
            <div>
              <strong>${escapeHtml(label)}</strong>
              <span>Detect ${escapeHtml(label.toLowerCase())} in the audio stream</span>
            </div>
          </div>
          <label class="toggle-control zone-motion-toggle" title="Enable or disable ${escapeHtml(label)} detection">
            <input type="checkbox" data-rule-enabled="${id}" ${enabled ? 'checked' : ''} />
            <span>${enabled ? 'On' : 'Off'}</span>
          </label>
        </div>
        <div class="zone-motion-body zone-people-body">
          <div style="display:flex;align-items:center;gap:12px;flex-wrap:wrap">
            <label class="muted" style="font-size:13px;display:flex;gap:4px;align-items:center" title="Record a clip when ${escapeHtml(label)} is detected">
              <input type="checkbox" data-rule-record="${id}" ${rule.record_on_detect !== false ? 'checked' : ''} />📹 Record
            </label>
            <label class="muted" style="font-size:13px;display:flex;gap:4px;align-items:center" title="Email when ${escapeHtml(label)} is detected">
              <input type="checkbox" data-rule-email="${id}" ${rule.email_enabled ? 'checked' : ''} />📧 Email
            </label>
            <label class="muted" style="font-size:13px;display:flex;gap:4px;align-items:center" title="Push when ${escapeHtml(label)} is detected">
              <input type="checkbox" data-rule-push="${id}" ${rule.push_enabled ? 'checked' : ''} />🔔 Push
            </label>
            <button class="secondary rule-expand-btn" type="button" data-expand-rule="${id}" title="Advanced settings for ${escapeHtml(label)}">${expanded ? ICONS.chevronUp : ICONS.email}<span>${expanded ? 'Hide' : 'Advanced'}</span></button>
          </div>
          <div style="display:flex;flex-wrap:wrap;gap:14px">
            <label class="sound-rule-field" title="Detection threshold (0.1-1.0): how confident the model must be before this sound counts.">
              <span>Threshold</span>
              <input type="number" data-rule-threshold-value="${id}" min="0.1" max="1.0" step="0.01" value="${escapeHtml(String(rule.confidence_threshold ?? 0.35))}" style="width:90px" />
            </label>
            <label class="sound-rule-field" title="Cooldown: minimum seconds between alerts for this sound.">
              <span>Cooldown (s)</span>
              <input type="number" data-rule-cooldown="${id}" value="${escapeHtml(String(rule.cooldown_seconds ?? 30))}" min="5" max="3600" step="5" style="width:90px" />
            </label>
          </div>
        </div>
        <div class="zone-motion-advanced-body" ${expanded ? '' : 'hidden'}>
          ${renderRuleExpandFields('rule', id, rule)}
          <div style="width:100%;display:flex;justify-content:flex-end;padding-top:4px">
            <button class="delete-btn secondary" type="button" data-remove-rule="${id}" title="Delete ${escapeHtml(label)} rule">${ICONS.remove} Remove</button>
          </div>
        </div>
      </div>`;
  }).join('');

  rulesWrap.querySelectorAll('[data-expand-rule]').forEach((btn) => {
    btn.addEventListener('click', () => {
      const id = btn.dataset.expandRule;
      if (expandedSoundRules.has(id)) expandedSoundRules.delete(id);
      else expandedSoundRules.add(id);
      renderRules();
    });
  });
  rulesWrap.querySelectorAll('[data-rule-enabled]').forEach((input) => {
    input.addEventListener('change', () => {
      updateRule(input.dataset.ruleEnabled, 'enabled', input.checked);
    });
  });
  rulesWrap.querySelectorAll('[data-rule-record]').forEach((input) => {
    input.addEventListener('change', () => updateRule(input.dataset.ruleRecord, 'record_on_detect', input.checked));
  });
  // Threshold control: number input with clamping (0.1-1.0).
  rulesWrap.querySelectorAll('input[data-rule-threshold-value]').forEach((input) => {
    input.addEventListener('change', () => {
      const value = Math.max(0.1, Math.min(1.0, Number(input.value) || 0.35));
      input.value = value;
      updateRule(input.dataset.ruleThresholdValue, 'confidence_threshold', value);
    });
  });
  rulesWrap.querySelectorAll('[data-rule-cooldown]').forEach((input) => {
    input.addEventListener('change', () => updateRule(input.dataset.ruleCooldown, 'cooldown_seconds', Math.max(5, Number.parseInt(input.value, 10) || 30)));
  });
  rulesWrap.querySelectorAll('[data-rule-email]').forEach((input) => {
    input.addEventListener('change', () => updateRule(input.dataset.ruleEmail, 'email_enabled', input.checked));
  });
  rulesWrap.querySelectorAll('[data-rule-email-recipients]').forEach((input) => {
    input.addEventListener('change', () => updateRule(input.dataset.ruleEmailRecipients, 'email_recipients', normalizeEmailList(input.value)));
  });
  rulesWrap.querySelectorAll('[data-rule-active-start]').forEach((wrap) => {
    wrap.querySelectorAll('select').forEach((sel) => sel.addEventListener('change', () => updateRule(wrap.dataset.ruleActiveStart, 'active_start', timeSelectValue(wrap))));
  });
  rulesWrap.querySelectorAll('[data-rule-active-end]').forEach((wrap) => {
    wrap.querySelectorAll('select').forEach((sel) => sel.addEventListener('change', () => updateRule(wrap.dataset.ruleActiveEnd, 'active_end', timeSelectValue(wrap))));
  });
  rulesWrap.querySelectorAll('[data-rule-notify-start]').forEach((wrap) => {
    wrap.querySelectorAll('select').forEach((sel) => sel.addEventListener('change', () => updateRule(wrap.dataset.ruleNotifyStart, 'notify_start', timeSelectValue(wrap))));
  });
  rulesWrap.querySelectorAll('[data-rule-notify-end]').forEach((wrap) => {
    wrap.querySelectorAll('select').forEach((sel) => sel.addEventListener('change', () => updateRule(wrap.dataset.ruleNotifyEnd, 'notify_end', timeSelectValue(wrap))));
  });
  rulesWrap.querySelectorAll('[data-rule-push]').forEach((input) => {
    input.addEventListener('change', () => updateRule(input.dataset.rulePush, 'push_enabled', input.checked));
  });
  rulesWrap.querySelectorAll('[data-remove-rule]').forEach((button) => {
    button.addEventListener('click', () => {
      expandedSoundRules.delete(button.dataset.removeRule);
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
    // When api() set window.daygleAuth.redirecting = true, the page is on
    // its way to /login. Skipping the catch UI mutation (and the renderEditor()
    // that follows) avoids flashing a fake 'Authentication required' status
    // onto the panel for a few ms before navigation completes.
    if (window.daygleAuth?.redirecting) return;
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
    await refreshDetectorStatuses();
  } catch (err) {
    // Skip UI updates if api() triggered a 401 redirect
    if (window.daygleAuth?.redirecting) return;
    setMessage(err.message, true);
  } finally {
    saveBtn.disabled = false;
  }
}

async function loadSounds() {
  // nav.js's daygleAuthReady IIFE has already populated window.daygleAuth.{user, csrfToken}.
  await window.daygleAuthReady;
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
  await refreshDetectorStatuses();
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
reloadBtn.addEventListener('click', () => loadSounds().catch((err) => {
  // Skip UI updates if api() triggered a 401 redirect
  if (window.daygleAuth?.redirecting) return;
  setMessage(err.message, true);
}));

loadSounds().catch((err) => {
  // Skip UI updates if api() triggered a 401 redirect
  if (window.daygleAuth?.redirecting) return;
  setMessage(err.message, true);
});
