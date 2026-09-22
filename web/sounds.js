let cameras = [];
let soundClasses = [];
let selectedCameraId = '';
let selectedStatus = null;
let soundDirty = false;

requireElements(['soundCameraSelect', 'soundEnabled', 'soundStatusPanel']);

const cameraSelect = document.getElementById('soundCameraSelect');
const soundEnabled = document.getElementById('soundEnabled');
const statusPanel = document.getElementById('soundStatusPanel');
const saveBtn = document.getElementById('saveSoundSettingsBtn');
const reloadBtn = document.getElementById('reloadSoundsBtn');
const statSoundRules = document.getElementById('statSoundRules');
const statActiveRules = document.getElementById('statActiveRules');
const statDetection = document.getElementById('statDetection');
const statCamera = document.getElementById('statCamera');
const soundCameraStatusList = document.getElementById('soundCameraStatusList');
const soundClassSelect = document.getElementById('soundClassSelect');
const addSoundClassBtn = document.getElementById('addSoundClassBtn');
const soundClassEditor = document.getElementById('soundClassEditor');

// Detection-scope defaults for a newly assigned class. Alert delivery fields
// (email/push/record/schedule) start off and are configured on the Alerts page.
function defaultSoundRule(cls) {
  const sound = soundClasses.find((item) => String(item.id) === String(cls));
  return {
    class: cls,
    name: sound?.label || cls,
    enabled: true,
    record_on_detect: true,
    confidence_threshold: sound?.default_threshold ?? 0.35,
    cooldown_seconds: sound?.default_cooldown ?? 30,
    email_enabled: false,
    email_recipients: [],
    push_enabled: false,
    active_start: null,
    active_end: null,
    notify_start: null,
    notify_end: null,
  };
}

function soundClassLabel(rule) {
  const fallback = soundClasses.find((item) => String(item.id) === String(rule.class))?.label;
  return titleCase(String(rule.name || fallback || rule.class || '').replace(/[_-]+/g, ' '));
}

function markSoundDirty() {
  soundDirty = true;
  const message = document.getElementById('soundMessage');
  if (message) {
    message.textContent = 'Unsaved changes - click Save Detection Settings to apply.';
    message.className = 'muted cameras-list-status';
  }
}

function ensureSoundConfig(camera) {
  camera.detection ||= {};
  camera.detection.sound ||= { enabled: false, rules: [] };
  camera.detection.sound.rules ||= [];
  return camera.detection.sound;
}

function detectorSoundConfig(camera) {
  return camera?.detection?.sound || {};
}

function detectorEnabledRules(camera) {
  return (detectorSoundConfig(camera).rules || []).filter((rule) => rule.enabled === true);
}

function detectorSoundClassCount(camera) {
  return (detectorSoundConfig(camera).rules || []).length;
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
  return name || id || 'Unknown Camera';
}

function detectorStatusReason(camera, status) {
  if (!detectorSoundConfigured(camera)) return 'Sound Disabled';
  if (!detectorEnabledRules(camera).length) return 'No Enabled Sound Classes';
  if (!detectorHasRtspConfig(camera)) return 'No RTSP Stream Configured';
  if (status.running) return 'Running';
  return titleCase(String(status.detector_status || status.state || 'Not Running'));
}

function detectorStatusClass(camera, status) {
  if (status.running) return 'status-ok';
  const reason = detectorStatusReason(camera, status).toLowerCase();
  if (reason.includes('loading')) return 'status-warning';
  if (reason === 'sound disabled' || reason === 'no enabled sound classes') return '';
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
    soundCameraStatusList.innerHTML = '<p class="muted empty-message">No Cameras Are Configured.</p>';
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
  soundCameraStatusList.innerHTML = '<div style="overflow-x:auto">'
    + '<table class="rule-table">'
    + '<thead><tr><th>Camera</th><th>Configured</th><th>Enabled Sounds</th><th>Backend</th>'
    + '<th>Running</th><th>Status</th><th>Last Sound</th><th>Recent Scores</th><th>Detail</th></tr></thead>'
    + '<tbody>' + rowsHtml + '</tbody></table></div>';
}

async function refreshDetectorStatuses() {
  if (!soundCameraStatusList) return;
  const rows = await Promise.all(cameras.map(async (camera) => ({
    camera,
    status: await api(`/api/sound/status?camera_id=${encodeURIComponent(camera.id || '')}`).catch(() => ({
      state: 'Unavailable',
      detector_status: 'Unavailable',
      running: false,
      backend: null,
      last_confidences: {},
    })),
  })));
  renderDetectorStatuses(rows);
}

function currentCamera() {
  return cameras.find((camera) => camera.id === selectedCameraId) || cameras[0] || null;
}

function renderCameraSelect() {
  if (!cameras.length) {
    cameraSelect.innerHTML = '<option value="">No Cameras Configured</option>';
    cameraSelect.disabled = true;
    return;
  }
  cameraSelect.disabled = false;
  cameraSelect.innerHTML = cameras.map((camera) => {
    const label = camera.name || camera.id || 'Camera';
    return `<option value="${escapeHtml(camera.id || '')}" ${camera.id === selectedCameraId ? 'selected' : ''}>${escapeHtml(label)} (${escapeHtml(camera.id || '')})</option>`;
  }).join('');
}

function renderClassPicker(camera) {
  if (!soundClassSelect) return;
  const assigned = new Set((detectorSoundConfig(camera).rules || []).map((rule) => String(rule.class)));
  const available = soundClasses.filter((sound) => !assigned.has(String(sound.id)));
  const disabled = !camera || !available.length;
  soundClassSelect.disabled = disabled;
  if (addSoundClassBtn) addSoundClassBtn.disabled = disabled;
  if (!soundClasses.length) {
    soundClassSelect.innerHTML = '<option value="">No sound classes available</option>';
    return;
  }
  if (!available.length) {
    soundClassSelect.innerHTML = '<option value="">All classes already added</option>';
    return;
  }
  soundClassSelect.innerHTML = available
    .map((sound) => `<option value="${escapeHtml(sound.id)}">${escapeHtml(sound.label || sound.id)}</option>`)
    .join('');
}

function renderClassEditor(camera) {
  renderClassPicker(camera);
  if (!soundClassEditor) return;
  if (!camera) {
    soundClassEditor.innerHTML = '';
    return;
  }
  const rules = detectorSoundConfig(camera).rules || [];
  if (!rules.length) {
    soundClassEditor.innerHTML = '<p class="muted empty-message">No sound classes assigned yet. Add one above so this camera starts listening for it.</p>';
    return;
  }
  soundClassEditor.innerHTML = rules.map((rule, index) => {
    const enabled = rule.enabled !== false;
    return `
      <div class="sound-class-row${enabled ? ' is-enabled' : ''}" data-class-index="${index}" style="display:flex;align-items:end;gap:12px;flex-wrap:wrap;padding:10px 12px;border:1px solid var(--border);border-radius:12px;margin-bottom:8px">
        <strong style="flex:1;min-width:120px;align-self:center">${escapeHtml(soundClassLabel(rule))}</strong>
        <label class="sound-rule-field" title="Only sounds detected with at least this confidence (0.01-1) count on this camera. Overrides the detector default for this class.">
          <span>Min Confidence</span>
          <input type="number" data-class-confidence="${index}" min="0.01" max="1" step="0.01" value="${escapeHtml(String(rule.confidence_threshold ?? 0.35))}" style="width:90px" />
        </label>
        <label class="toggle-control" title="Enable or disable detection of this sound on this camera" style="align-self:center">
          <input type="checkbox" data-class-toggle="${index}" ${enabled ? 'checked' : ''} />
          <span>${enabled ? 'On' : 'Off'}</span>
        </label>
        <a class="sound-class-alerts-link" href="/alerts" style="color:var(--accent);font-size:11px;font-weight:750;text-decoration:none;white-space:nowrap;align-self:center">Configure alerts</a>
        <button class="btn-danger" type="button" data-class-remove="${index}" title="Remove this sound class from the camera" style="align-self:center">Remove</button>
      </div>`;
  }).join('');
  bindClassEditor(camera);
}

function bindClassEditor(camera) {
  const rules = detectorSoundConfig(camera).rules || [];
  soundClassEditor.querySelectorAll('[data-class-toggle]').forEach((input) => {
    input.addEventListener('change', () => {
      const rule = rules[Number(input.dataset.classToggle)];
      if (!rule) return;
      rule.enabled = input.checked;
      markSoundDirty();
      renderClassEditor(camera);
      renderStatus();
    });
  });
  // Per-class detection threshold (0.01-1); mirrors the Zones object rule's
  // Min Confidence. Updates the value in place without a re-render so focus
  // is not lost while typing.
  soundClassEditor.querySelectorAll('[data-class-confidence]').forEach((input) => {
    input.addEventListener('change', () => {
      const rule = rules[Number(input.dataset.classConfidence)];
      if (!rule) return;
      const value = Math.min(1, Math.max(0.01, Number(input.value) || 0.35));
      rule.confidence_threshold = value;
      input.value = value;
      markSoundDirty();
    });
  });
  soundClassEditor.querySelectorAll('[data-class-remove]').forEach((button) => {
    button.addEventListener('click', () => {
      const index = Number(button.dataset.classRemove);
      const rule = rules[index];
      if (!rule) return;
      if (!window.confirm(`Remove ${soundClassLabel(rule)} from this camera?`)) return;
      rules.splice(index, 1);
      markSoundDirty();
      renderClassEditor(camera);
      renderStatus();
    });
  });
}

function addSoundClass() {
  const camera = currentCamera();
  if (!camera || !soundClassSelect) return;
  const cls = soundClassSelect.value;
  if (!cls) return;
  const config = ensureSoundConfig(camera);
  if (config.rules.some((rule) => String(rule.class) === String(cls))) return;
  config.rules.push(defaultSoundRule(cls));
  markSoundDirty();
  renderClassEditor(camera);
  renderStatus();
}

function renderStatus() {
  const camera = currentCamera();
  const config = detectorSoundConfig(camera);
  const totalRules = detectorSoundClassCount(camera);
  const activeRules = detectorEnabledRules(camera).length;
  if (statSoundRules) statSoundRules.textContent = camera ? String(totalRules) : '-';
  if (statActiveRules) statActiveRules.textContent = camera ? String(activeRules) : '-';
  if (statDetection) statDetection.textContent = !camera ? '-' : config.enabled === true ? 'Enabled' : 'Disabled';
  if (statCamera) statCamera.textContent = camera ? (camera.name || camera.id || '-') : '-';

  if (!camera || !selectedStatus) {
    statusPanel.innerHTML = '';
    return;
  }
  const detail = selectedStatus.backend_reason || selectedStatus.status_detail || '';
  const stateClass = selectedStatus.running ? 'status-ok' : (detail ? 'status-warning' : '');
  const stateLabel = selectedStatus.running ? 'Detector Running' : 'Detector Not Running';
  statusPanel.innerHTML = safeHtml`<div class="status-panel${stateClass ? ` ${stateClass}` : ''}"><span>${stateLabel}${detail ? ` · ${detail}` : ''}</span></div>`;
}

function renderEditor() {
  const camera = currentCamera();
  renderCameraSelect();
  soundEnabled.disabled = !camera;
  saveBtn.disabled = !camera;
  reloadBtn.disabled = !camera;
  soundEnabled.value = String(detectorSoundConfigured(camera));
  renderStatus();
  renderClassEditor(camera);
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
    if (window.daygleAuth?.redirecting) return;
    selectedStatus = { backend_reason: err.message, running: false, backend: 'None' };
  }
  renderEditor();
}

function selectCamera(cameraId) {
  selectedCameraId = cameraId;
  refreshStatus();
}

async function saveSoundDetection() {
  const camera = currentCamera();
  if (!camera) return;
  const updatedCameras = cameras.map((item) => item.id === camera.id
    ? {
      ...item,
      detection: {
        ...(item.detection || {}),
        sound: {
          ...detectorSoundConfig(item),
          enabled: soundEnabled.value === 'true',
        },
      },
    }
    : item);

  saveBtn.disabled = true;
  try {
    const result = await api('/api/cameras', {
      method: 'PUT',
      body: JSON.stringify({ cameras: updatedCameras }),
    });
    cameras = result.cameras || updatedCameras;
    selectedStatus = null;
    soundDirty = false;
    setMessage('Sound Detection Settings Saved.');
    await refreshStatus();
    await refreshDetectorStatuses();
  } catch (err) {
    if (window.daygleAuth?.redirecting) return;
    setMessage(err.message, true);
  } finally {
    saveBtn.disabled = false;
  }
}

function setMessage(text, isError = false) {
  const message = document.getElementById('soundMessage');
  if (!message) return;
  message.textContent = text || '';
  message.className = isError ? 'error' : 'muted cameras-list-status';
  if (text) window.showToast?.(text, isError);
}

async function loadSounds() {
  await window.daygleAuthReady;
  soundDirty = false;
  const [settings, classPayload] = await Promise.all([
    api('/api/settings/system'),
    api('/api/sound/classes').catch(() => ({ classes: [] })),
  ]);
  soundClasses = classPayload.classes || [];
  cameras = settings.cameras || (settings.camera ? [settings.camera] : []);
  const requested = new URLSearchParams(window.location.search).get('camera');
  selectedCameraId = requested && cameras.some((camera) => camera.id === requested)
    ? requested
    : (selectedCameraId || cameras[0]?.id || '');
  await refreshStatus();
  await refreshDetectorStatuses();
}

cameraSelect.addEventListener('change', () => selectCamera(cameraSelect.value));
soundEnabled.addEventListener('change', () => { markSoundDirty(); renderStatus(); });
addSoundClassBtn?.addEventListener('click', addSoundClass);
saveBtn.addEventListener('click', saveSoundDetection);
reloadBtn.addEventListener('click', () => loadSounds().catch((err) => {
  if (!window.daygleAuth?.redirecting) setMessage(err.message, true);
}));

// Warn before leaving with unsaved class/enablement changes.
window.addEventListener('beforeunload', (event) => {
  if (!soundDirty) return;
  event.preventDefault();
  event.returnValue = '';
});

loadSounds().catch((err) => {
  if (!window.daygleAuth?.redirecting) setMessage(err.message, true);
});
