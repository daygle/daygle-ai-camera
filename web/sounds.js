let cameras = [];
let soundClasses = [];
let selectedCameraId = '';
let selectedStatus = null;
let soundDirty = false;

requireElements(['soundEnabled', 'soundStatusPanel']);

const soundEnabled = document.getElementById('soundEnabled');
const soundDetectionInterval = document.getElementById('soundDetectionInterval');
const statusPanel = document.getElementById('soundStatusPanel');
const saveBtn = document.getElementById('saveSoundSettingsBtn');
const reloadBtn = document.getElementById('reloadSoundsBtn');
const soundCameraStatusList = document.getElementById('soundCameraStatusList');
const soundClassSelect = document.getElementById('soundClassSelect');
const addSoundClassBtn = document.getElementById('addSoundClassBtn');
const soundClassEditor = document.getElementById('soundClassEditor');
const soundEditorTitle = document.getElementById('soundEditorTitle');

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

function detectorEnabledRules(camera) {return (detectorSoundConfig(camera).rules || []).filter((rule) => rule.enabled === true);
}

function detectorSoundConfigured(camera) {
  return detectorSoundConfig(camera).enabled === true;
}

// The classifier's sampling rate. 0.5 preserves the historical cadence (a full
// 1s window classified twice a second); 0.1 samples ten times a second. The
// window length never changes, so a bark or a doorbell is never truncated -
// only how often overlapping windows are checked.
function detectorSoundInterval(camera) {
  const value = Number(detectorSoundConfig(camera).detection_interval_seconds);
  return Number.isFinite(value) ? value : 0.5;
}

function detectorHasRtspConfig(camera) {
  return Boolean(camera?.stream_url || camera?.rtsp_url || camera?.host);
}

function detectorCameraLabel(camera) {
  const name = String(camera?.name || '').trim();
  const id = String(camera?.id || '').trim();
  const slug = name.toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-+|-+$/g, '');
  if (name && id && slug !== id) return `${name} (${id})`;
  return name || id || 'Unknown Camera';
}

// One verdict per camera, shared by its table row and the editor banner.
// "Running" alone only means the listener thread is alive: with the TFLite
// runtime missing the thread runs but nothing is classified, which used to
// show as a green "Detector Running" box. Green now needs a usable backend.
function detectorHealth(camera, status = {}) {
  const backend = String(status.backend || '').toLowerCase();
  const detail = status.backend_reason || status.status_detail || '';
  if (!detectorSoundConfigured(camera)) return { tone: 'off', label: 'Sound off', detail: '' };
  if (!detectorEnabledRules(camera).length) return { tone: 'off', label: 'No sound classes', detail: 'Add a sound class so this camera starts listening.' };
  if (!detectorHasRtspConfig(camera)) return { tone: 'error', label: 'No RTSP stream', detail: 'Sound detection needs the camera\'s RTSP stream.' };
  if (backend === 'unavailable') return { tone: 'error', label: 'Unavailable', detail: detail || 'The sound model backend is unavailable.' };
  if (backend === 'loading' || /loading/i.test(String(status.detector_status || ''))) return { tone: 'warn', label: 'Loading', detail };
  if (status.running) return { tone: 'ok', label: 'Listening', detail };
  return { tone: 'warn', label: titleCase(String(status.detector_status || status.state || 'Not running')), detail };
}

function detectorHealthPill(health) {
  return `<span class="sound-health is-${escapeHtml(health.tone)}">${escapeHtml(health.label)}</span>`;
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

function detectorLastHeard(status) {
  const label = status.last_class_label || (status.last_class ? titleCase(String(status.last_class).replace(/[_-]+/g, ' ')) : '');
  const score = Number(status.last_confidence);
  if (!label) return 'Nothing yet';
  return Number.isFinite(score) ? `${label} · ${Math.round(score * 100)}%` : label;
}

function renderDetectorStatuses(rows) {
  if (!soundCameraStatusList) return;
  if (!rows.length) {
    soundCameraStatusList.innerHTML = '<p class="muted empty-message">No cameras configured. Add one on the <a href="/cameras">Cameras</a> page.</p>';
    return;
  }
  const rowsHtml = rows.map(({ camera, status }) => {
    const health = detectorHealth(camera, status);
    const selected = camera.id === selectedCameraId;
    const classes = detectorEnabledRules(camera).map((rule) => soundClassLabel(rule)).join(', ');
    return `
    <tr data-camera-row="${escapeHtml(camera.id || '')}" class="${selected ? 'is-selected' : ''}" aria-selected="${selected ? 'true' : 'false'}">
      <td class="cell-label"><button type="button" class="sound-camera-pick" data-pick-camera="${escapeHtml(camera.id || '')}">${escapeHtml(detectorCameraLabel(camera))}</button></td>
      <td data-label="Sound">${escapeHtml(detectorSoundConfigured(camera) ? 'On' : 'Off')}</td>
      <td data-label="Listening for">${escapeHtml(classes || 'Nothing yet')}</td>
      <td data-label="Status" title="${escapeHtml(health.detail)}">${detectorHealthPill(health)}${health.detail && health.tone === 'error' ? `<span class="sound-health-detail">${escapeHtml(health.detail)}</span>` : ''}</td>
      <td data-label="Last heard" title="${escapeHtml(`Recent scores: ${detectorConfidenceMap(status.last_confidences)}`)}">${escapeHtml(detectorLastHeard(status))}</td>
    </tr>`;
  }).join('');
  soundCameraStatusList.innerHTML = '<div class="stack-table-wrap" style="overflow-x:auto">'
    + '<table class="rule-table stack-table">'
    + '<thead><tr><th>Camera</th><th>Sound</th><th>Listening for</th><th>Status</th><th>Last heard</th></tr></thead>'
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
  // Same detection table as the Zones page (.zone-rule-table) so object and
  // sound rules look identical across pages.
  const rows = rules.map((rule, index) => {
    const enabled = rule.enabled !== false;
    const record = rule.record_on_detect !== false;
    const label = escapeHtml(soundClassLabel(rule));
    return `
      <tr class="zone-rule-row${enabled ? ' is-enabled' : ''}" data-class-index="${index}">
        <td class="cell-label"><span class="zone-rule-icon" aria-hidden="true">🔊</span>${label}</td>
        <td data-label="Detect"><label class="toggle-control zone-rule-toggle" title="Enable or disable detection of this sound on this camera"><input type="checkbox" data-class-toggle="${index}" ${enabled ? 'checked' : ''} /><span>${enabled ? 'On' : 'Off'}</span></label></td>
        <td data-label="Min confidence"><input class="zone-rule-conf" type="number" data-class-confidence="${index}" aria-label="Minimum confidence for ${label}" min="0.01" max="1" step="0.01" value="${escapeHtml(String(rule.confidence_threshold ?? 0.35))}" title="Only sounds detected with at least this confidence (0.01-1) count on this camera. Overrides the detector default for this class." /></td>
        <td data-label="Record"><label class="toggle-control zone-rule-toggle" title="Record a clip when this sound is detected on this camera"><input type="checkbox" data-class-record="${index}" ${record ? 'checked' : ''} /><span>${record ? 'On' : 'Off'}</span></label></td>
        <td class="cell-actions stack-actions"><button class="delete-btn secondary zone-action-btn zone-rule-remove" type="button" data-class-remove="${index}" title="Remove ${label} from this camera" aria-label="Remove ${label} from this camera">${ICONS.remove}</button></td>
      </tr>`;
  }).join('');
  // Build the markup in a local first (rows are already escapeHtml'd); the XSS
  // static guard forbids assigning a raw `${}` template literal to .innerHTML.
  const tableHtml = `
    <div class="cameras-table-wrap">
      <table class="rule-table zone-rule-table stack-table">
        <thead><tr><th scope="col">Sound</th><th scope="col">Detect</th><th scope="col">Min confidence</th><th scope="col">Record</th><th scope="col" class="cell-actions">Actions</th></tr></thead>
        <tbody>${rows}</tbody>
      </table>
    </div>`;
  soundClassEditor.innerHTML = tableHtml;
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
  soundClassEditor.querySelectorAll('[data-class-record]').forEach((input) => {
    input.addEventListener('change', () => {
      const rule = rules[Number(input.dataset.classRecord)];
      if (!rule) return;
      rule.record_on_detect = input.checked;
      markSoundDirty();
      // Re-render so the row's Record On/Off pill updates live.
      renderClassEditor(camera);
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
  if (!camera || !selectedStatus) {
    statusPanel.hidden = true;
    statusPanel.innerHTML = '';
    return;
  }
  const health = detectorHealth(camera, selectedStatus);
  const titles = { ok: 'Listening', warn: 'Not listening yet', error: 'Not listening - no sound alerts from this camera', off: 'Not listening' };
  statusPanel.className = `sound-health-banner is-${health.tone}`;
  const heading = safeHtml`<strong>${titles[health.tone]} · ${health.label}</strong>`;
  const detail = health.detail ? safeHtml`<p>${health.detail}</p>` : '';
  statusPanel.innerHTML = heading + detail;
  statusPanel.hidden = false;
}

function renderEditor() {
  const camera = currentCamera();
  if (soundEditorTitle) {
    soundEditorTitle.textContent = camera ? `${camera.name || camera.id} - sound settings` : 'Camera sound settings';
  }
  soundEnabled.disabled = !camera;
  saveBtn.disabled = !camera;
  soundEnabled.value = String(detectorSoundConfigured(camera));
  if (soundDetectionInterval) {
    soundDetectionInterval.disabled = !camera;
    soundDetectionInterval.value = String(detectorSoundInterval(camera));
  }
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
  if (!cameraId || cameraId === selectedCameraId) return;
  if (soundDirty && !window.confirm('Discard unsaved sound settings for this camera?')) return;
  if (soundDirty) {
    // Class edits are made in place on the camera; reload to drop them.
    selectedCameraId = cameraId;
    loadSounds().catch((err) => {
      if (!window.daygleAuth?.redirecting) setMessage(err.message, true);
    });
    return;
  }
  selectedCameraId = cameraId;
  setMessage('');
  refreshStatus().then(refreshDetectorStatuses);
  document.getElementById('soundEditorCard')?.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
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
          detection_interval_seconds: Math.min(
            0.5,
            Math.max(0.1, Number(soundDetectionInterval?.value) || 0.5),
          ),
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
  // Camera names are viewer-visible; avoid the admin-gated
  // /api/settings/system (403 for non-admin roles).
  const [settings, classPayload] = await Promise.all([
    api('/api/config'),
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

soundCameraStatusList?.addEventListener('click', (event) => {
  const row = event.target instanceof Element ? event.target.closest('tr[data-camera-row]') : null;
  if (row) selectCamera(row.getAttribute('data-camera-row') || '');
});
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
