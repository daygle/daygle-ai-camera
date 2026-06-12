let csrfToken = null;
let cameras = [];
let pendingDeleteIndex = null;

const messageEl = document.getElementById('cameraMessage');
const gridEl = document.getElementById('cameraGrid');
const emptyEl = document.getElementById('cameraEmpty');
const modal = document.getElementById('cameraModal');
const deleteModal = document.getElementById('deleteModal');
const editForm = document.getElementById('cameraEditForm');
let soundClasses = [];
let editingSound = null;

// Stats + filter state
const stats = {
  total: document.getElementById('statTotalCameras'),
  recording: document.getElementById('statRecordingOn'),
  zones: document.getElementById('statWithZones'),
  backends: document.getElementById('statBackends'),
  health: document.getElementById('statCameraHealth'),
};
const filter = {
  text: document.getElementById('cameraFilter'),
  backend: document.getElementById('cameraBackendFilter'),
  reset: document.getElementById('cameraFilterResetBtn'),
  form: document.getElementById('camerasFilterForm'),
};

function setMessage(text, isError = false) {
  messageEl.textContent = text;
  messageEl.className = isError ? 'error' : 'muted';
  if (text) window.showToast?.(text, isError);
}

async function api(path, options = {}) {
  const headers = { ...(options.headers || {}) };
  if (csrfToken && ['POST', 'PUT', 'PATCH', 'DELETE'].includes((options.method || 'GET').toUpperCase())) {
    headers['X-CSRF-Token'] = csrfToken;
  }
  if (options.body && !(options.body instanceof FormData) && !headers['Content-Type']) headers['Content-Type'] = 'application/json';
  const res = await fetch(path, { ...options, headers });
  if (res.status === 401) { window.location.href = '/login'; return; }
  const payload = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(payload.detail || `Request failed: ${res.status}`);
  return payload;
}

function cameraStatusBadge(camera) {
  const url = buildDisplayUrl(camera);
  if (!url && !camera.host) return '<span class="cam-badge cam-badge-warn">Not configured</span>';
  return '<span class="cam-badge cam-badge-idle"><span class="cam-badge-dot"></span>Configured</span>';
}

function cameraRecordingChips(camera) {
  const continuous = camera.recording?.continuous === true;
  const chips = [];
  if (continuous) {
    chips.push('<span class="chip chip-green">Recording On</span><span class="chip">Continuous</span>');
  } else {
    chips.push('<span class="chip chip-green">Recording On</span>');
  }
  return chips.join('');
}

function buildDisplayUrl(camera) {
  if (camera.stream_url) return camera.stream_url;
  if (!camera.host) return '';
  const auth = camera.username ? `${camera.username}:••••@` : '';
  const port = camera.port && camera.port !== 554 ? `:${camera.port}` : '';
  const path = camera.path ? `/${camera.path}` : '/stream1';
  return `rtsp://${auth}${camera.host}${port}${path}`;
}

function renderCameraCard(camera, index) {
  const displayUrl = buildDisplayUrl(camera);
  const name = escapeHtml(camera.name || camera.id || `Camera ${index + 1}`);
  const url = escapeHtml(displayUrl);
  const backend = camera.backend === 'rtsp' ? 'RTSP' : 'ONVIF';
  const res = `${camera.width || 1280}×${camera.height || 720}`;
  const fps = camera.fps || 15;
  const zoneCount = (camera.detection?.zones || []).length;

  return `
    <article class="cam-card" data-camera-index="${index}">
      <div class="cam-card-header">
        <div class="cam-card-identity">
          <div class="cam-icon">
            <svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M23 7l-7 5 7 5V7z"/><rect x="1" y="5" width="15" height="14" rx="2" ry="2"/></svg>
          </div>
          <div>
            <h3 class="cam-name">${name}</h3>
            <span class="cam-id muted">${escapeHtml(camera.id || '')}</span>
          </div>
        </div>
        <div class="cam-card-actions">
          ${cameraStatusBadge(camera)}
          <button class="secondary cam-edit-btn" data-index="${index}" type="button">Edit</button>
          <button class="secondary delete-btn cam-remove-btn" data-index="${index}" type="button">Remove</button>
        </div>
      </div>

      <div class="cam-card-body">
        <div class="cam-meta-row">
          <span class="cam-meta-label">Backend</span>
          <span class="cam-meta-value"><span class="chip">${backend}</span></span>
        </div>
        ${url ? `<div class="cam-meta-row cam-url-row">
          <span class="cam-meta-label">Stream URL</span>
          <span class="cam-meta-value cam-url">${url}</span>
        </div>` : ''}
        <div class="cam-meta-row">
          <span class="cam-meta-label">Resolution</span>
          <span class="cam-meta-value">${escapeHtml(res)} @ ${escapeHtml(fps)} fps</span>
        </div>
        <div class="cam-meta-row">
          <span class="cam-meta-label">Recording</span>
          <span class="cam-meta-value">${cameraRecordingChips(camera)}</span>
        </div>
        <div class="cam-meta-row">
          <span class="cam-meta-label">Zones</span>
          <span class="cam-meta-value">
            ${zoneCount > 0
              ? `<span class="chip chip-green">${zoneCount}</span>`
              : `<span class="chip chip-warn">No zones</span>`}
          </span>
        </div>
      </div>

      <div class="cam-card-footer">
        <a class="button-link secondary-link cam-live-link" href="/live?camera=${encodeURIComponent(camera.id || '')}">View Live</a>
        <a class="cam-footer-hint muted" href="/zones?camera=${encodeURIComponent(camera.id || '')}">Configure zones &amp; alerts</a>
      </div>
    </article>
  `;
}

function currentFilterValues() {
  return {
    text: (filter.text?.value || '').trim().toLowerCase(),
    backend: filter.backend?.value || '',
  };
}

function applyFilter(list) {
  const { text, backend } = currentFilterValues();
  return list.filter((camera) => {
    if (backend && (camera.backend || 'onvif') !== backend) return false;
    if (!text) return true;
    const haystack = `${camera.name || ''} ${camera.id || ''}`.toLowerCase();
    return haystack.includes(text);
  });
}

function updateFilterHint(filteredCount) {
  const { text, backend } = currentFilterValues();
  const parts = [];
  if (text) parts.push(`matching “${text}”`);
  if (backend === 'onvif') parts.push('using ONVIF');
  else if (backend === 'rtsp') parts.push('using RTSP');
  if (!parts.length) {
    messageEl.textContent = cameras.length
      ? `Showing all ${cameras.length} cameras.`
      : '';
    return;
  }
  messageEl.textContent = `Showing ${filteredCount} of ${cameras.length} cameras ${parts.join(' and ')}.`;
}

function renderGrid() {
  const filtered = applyFilter(cameras);
  if (cameras.length === 0) {
    gridEl.innerHTML = '';
    emptyEl.hidden = false;
    updateFilterHint(0);
    return;
  }
  emptyEl.hidden = true;
  if (filtered.length === 0) {
    gridEl.innerHTML = '<div class="camera-empty-state"><div class="camera-empty-icon" aria-hidden="true"><svg width="40" height="40" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><circle cx="11" cy="11" r="8"/><line x1="21" y1="21" x2="16.65" y2="16.65"/></svg></div><h2>No cameras match these filters</h2><p class="muted">Try clearing the search or selecting a different backend.</p></div>';
    updateFilterHint(0);
    return;
  }
  gridEl.innerHTML = filtered.map((cam) => {
    const realIndex = cameras.indexOf(cam);
    return renderCameraCard(cam, realIndex);
  }).join('');
  updateFilterHint(filtered.length);

  gridEl.querySelectorAll('.cam-edit-btn').forEach((btn) => {
    btn.addEventListener('click', () => openEditModal(Number(btn.dataset.index)));
  });
  gridEl.querySelectorAll('.cam-remove-btn').forEach((btn) => {
    btn.addEventListener('click', () => openDeleteModal(Number(btn.dataset.index)));
  });
}

function updateStats() {
  if (stats.total) stats.total.textContent = String(cameras.length);
  if (stats.recording) {
    const continuous = cameras.filter((c) => c.recording?.continuous === true).length;
    const alertBased = cameras.length - continuous;
    stats.recording.textContent = `${alertBased} / ${continuous}`;
  }
  if (stats.zones) {
    const withZones = cameras.filter((c) => (c.detection?.zones || []).length > 0).length;
    stats.zones.textContent = String(withZones);
  }
  if (stats.backends) {
    const onvif = cameras.filter((c) => (c.backend || 'onvif') === 'onvif').length;
    const rtsp = cameras.filter((c) => c.backend === 'rtsp').length;
    stats.backends.textContent = `${onvif} / ${rtsp}`;
  }
}

// ─── Modal helpers ────────────────────────────────────────────────────────────

function openModal(el) {
  el.hidden = false;
  document.body.classList.add('modal-open');
  el.focus?.();
}

function closeModal(el) {
  el.hidden = true;
  document.body.classList.remove('modal-open');
}

function switchTab(tabName) {
  modal.querySelectorAll('.modal-tab').forEach((tab) => {
    const active = tab.dataset.tab === tabName;
    tab.classList.toggle('active', active);
    tab.setAttribute('aria-selected', String(active));
  });
  modal.querySelectorAll('.modal-tab-panel').forEach((panel) => {
    panel.hidden = panel.dataset.panel !== tabName;
  });
}

modal.querySelectorAll('.modal-tab').forEach((tab) => {
  tab.addEventListener('click', () => switchTab(tab.dataset.tab));
});

// Toggle ONVIF vs manual RTSP fields
document.getElementById('editBackend').addEventListener('change', function () {
  const manual = this.value === 'rtsp';
  document.getElementById('rtspManualFields').hidden = !manual;
  document.getElementById('onvifFields').hidden = manual;
});

function fillModal(camera, index) {
  document.getElementById('modalTitle').textContent = index === null ? 'Add Camera' : 'Edit Camera';
  document.getElementById('editCameraIndex').value = index === null ? '' : String(index);
  document.getElementById('editName').value = camera.name || '';
  document.getElementById('editId').value = camera.id || '';
  document.getElementById('editBackend').value = camera.backend || 'onvif';
  document.getElementById('editStreamUrl').value = camera.stream_url || '';
  document.getElementById('editHost').value = camera.host || '';
  document.getElementById('editPort').value = camera.port || 554;
  document.getElementById('editPath').value = camera.path || 'stream1';
  document.getElementById('editUsername').value = camera.username || '';
  document.getElementById('editPassword').value = camera.password || '';
  document.getElementById('editWidth').value = camera.width || 1280;
  document.getElementById('editHeight').value = camera.height || 720;
  document.getElementById('editFps').value = camera.fps || 15;
  const staleVal = camera.stale_frame_grabs;
  document.getElementById('editStaleFrameGrabs').value = staleVal != null ? staleVal : '';
  document.getElementById('editContinuous').value = String(camera.recording?.continuous === true);

  const manual = camera.backend === 'rtsp';
  document.getElementById('rtspManualFields').hidden = !manual;
  document.getElementById('onvifFields').hidden = manual;

  // Sound detection
  editingSound = camera.detection?.sound
    ? JSON.parse(JSON.stringify(camera.detection.sound))
    : { enabled: false, rules: [] };
  renderModalSoundSettings();

  switchTab('connection');
}

function openEditModal(index) {
  const camera = index === null
    ? { id: `camera-${cameras.length + 1}`, name: `Camera ${cameras.length + 1}`, backend: 'onvif', port: 554, path: 'stream1', width: 1280, height: 720, fps: 15, recording: { continuous: false }, detection: { sound: { enabled: false, rules: [] } } }
    : cameras[index];
  fillModal(camera, index);
  openModal(modal);
}

function collectModalData() {
  const backend = document.getElementById('editBackend').value;
  return {
    id: document.getElementById('editId').value.trim() || `camera-${cameras.length + 1}`,
    name: document.getElementById('editName').value.trim(),
    backend,
    stream_url: backend === 'rtsp' ? document.getElementById('editStreamUrl').value.trim() : '',
    host: backend !== 'rtsp' ? document.getElementById('editHost').value.trim() : '',
    port: parseInt(document.getElementById('editPort').value || '554', 10),
    path: backend !== 'rtsp' ? document.getElementById('editPath').value.trim() : '',
    username: document.getElementById('editUsername').value.trim(),
    password: document.getElementById('editPassword').value,
    width: parseInt(document.getElementById('editWidth').value || '1280', 10),
    height: parseInt(document.getElementById('editHeight').value || '720', 10),
    fps: parseInt(document.getElementById('editFps').value || '15', 10),
    stale_frame_grabs: document.getElementById('editStaleFrameGrabs').value.trim() !== ''
      ? parseInt(document.getElementById('editStaleFrameGrabs').value, 10)
      : null,
    recording: {
      continuous: document.getElementById('editContinuous').value === 'true',
    },
    detection: {
      sound: editingSound || { enabled: false, rules: [] },
    },
  };
}

editForm.addEventListener('submit', async (e) => {
  e.preventDefault();
  const data = collectModalData();
  const indexEl = document.getElementById('editCameraIndex').value;
  const index = indexEl === '' ? null : Number(indexEl);

  if (index === null) {
    cameras.push(data);
  } else {
    cameras[index] = {
      ...cameras[index],
      ...data,
      detection: { ...(cameras[index].detection || {}), ...data.detection },
    };
  }

  try {
    const result = await api('/api/cameras', { method: 'PUT', body: JSON.stringify({ cameras }) });
    cameras = result.cameras || cameras;
    updateStats();
    renderGrid();
    closeModal(modal);
    setMessage(index === null ? 'Camera added.' : 'Camera updated.');
  } catch (err) {
    cameras = index === null ? cameras.slice(0, -1) : cameras;
    setMessage(err.message, true);
  }
});

// ─── Delete modal ─────────────────────────────────────────────────────────────

function openDeleteModal(index) {
  pendingDeleteIndex = index;
  const camera = cameras[index];
  const name = camera?.name || camera?.id || `Camera ${index + 1}`;
  document.getElementById('deleteModalBody').textContent =
    `Remove "${name}" from your configuration? Existing recordings are kept.`;
  openModal(deleteModal);
}

document.getElementById('deleteConfirmBtn').addEventListener('click', async () => {
  if (pendingDeleteIndex === null) return;
  cameras.splice(pendingDeleteIndex, 1);
  try {
    const result = await api('/api/cameras', { method: 'PUT', body: JSON.stringify({ cameras }) });
    cameras = result.cameras || cameras;
    updateStats();
    renderGrid();
    setMessage('Camera removed.');
  } catch (err) {
    setMessage(err.message, true);
  }
  closeModal(deleteModal);
  pendingDeleteIndex = null;
});

// ─── Close handlers ───────────────────────────────────────────────────────────

document.getElementById('addCameraBtn').addEventListener('click', () => openEditModal(null));
document.getElementById('addCameraEmptyBtn').addEventListener('click', () => openEditModal(null));
document.getElementById('modalCloseBtn').addEventListener('click', () => closeModal(modal));
document.getElementById('modalCancelBtn').addEventListener('click', () => closeModal(modal));
document.getElementById('deleteModalCloseBtn').addEventListener('click', () => closeModal(deleteModal));
document.getElementById('deleteCancelBtn').addEventListener('click', () => closeModal(deleteModal));

// Close on backdrop click
[modal, deleteModal].forEach((m) => {
  m.addEventListener('click', (e) => { if (e.target === m) closeModal(m); });
});

// Close on Escape
document.addEventListener('keydown', (e) => {
  if (e.key === 'Escape') {
    if (!modal.hidden) closeModal(modal);
    else if (!deleteModal.hidden) closeModal(deleteModal);
  }
});

// ─── Filter handlers ──────────────────────────────────────────────────────────

filter.text?.addEventListener('input', () => renderGrid());
filter.backend?.addEventListener('change', () => renderGrid());
filter.reset?.addEventListener('click', () => {
  setTimeout(() => renderGrid(), 0);
});
filter.form?.addEventListener('submit', (e) => e.preventDefault());

// Re-render when the user's date_format / time_format changes (no-op here,
// but keeps the page consistent with the rest of the app).
window.daygleDatePrefsChanged = function daygleDatePrefsChanged() { /* no-op */ };

// ─── Sound Detection (per-camera, in the modal) ───────────────────────────────

function _soundRuleOptions() {
  const activeIds = new Set((editingSound?.rules || []).map((r) => r.class));
  const available = soundClasses.filter((cls) => !activeIds.has(cls.id));
  const options = available.map((cls) => `<option value="${escapeHtml(cls.id)}">${escapeHtml(cls.label)}</option>`).join('');
  return `<option value="">Add Sound…</option>${options}`;
}

function _defaultSoundRule(cls) {
  return {
    class: cls.id,
    name: cls.label,
    enabled: false,
    record_on_detect: true,
    confidence_threshold: cls.default_threshold,
    cooldown_seconds: cls.default_cooldown,
    email_enabled: false,
    push_enabled: false,
  };
}

function _updateSoundRule(classId, field, value) {
  let rule = (editingSound?.rules || []).find((r) => r.class === classId);
  if (!rule) {
    const cls = soundClasses.find((c) => c.id === classId);
    if (!cls) return;
    rule = _defaultSoundRule(cls);
    editingSound.rules.push(rule);
  }
  rule[field] = value;
}

function renderModalSoundSettings() {
  const container = document.getElementById('cameraModalSoundSettings');
  if (!container) return;
  if (!soundClasses.length) {
    container.innerHTML = '<p class="muted">Sound classes unavailable.</p>';
    return;
  }
  const sound = editingSound || { enabled: false, rules: [] };
  const enabledSel = sound.enabled ? 'selected' : '';
  const disabledSel = sound.enabled ? '' : 'selected';

  const rows = (sound.rules || []).map((rule) => {
    const cls = soundClasses.find((c) => c.id === rule.class);
    const label = cls ? cls.label : titleCase(rule.class.replace(/_/g, ' '));
    return `
      <tr data-sound-class="${escapeHtml(rule.class)}">
        <td class="cell-label">${escapeHtml(label)}</td>
        <td class="cell-center"><input type="checkbox" data-sound-rule-enabled="${escapeHtml(rule.class)}" ${rule.enabled ? 'checked' : ''} /></td>
        <td class="cell-center"><input type="checkbox" data-sound-rule-record="${escapeHtml(rule.class)}" ${rule.record_on_detect !== false ? 'checked' : ''} /></td>
        <td><input type="number" data-sound-rule-threshold="${escapeHtml(rule.class)}" value="${rule.confidence_threshold}" min="0.1" max="1.0" step="0.05" /></td>
        <td><input type="number" data-sound-rule-cooldown="${escapeHtml(rule.class)}" value="${rule.cooldown_seconds}" min="5" max="3600" step="5" /></td>
        <td class="cell-center"><input type="checkbox" data-sound-rule-email="${escapeHtml(rule.class)}" ${rule.email_enabled ? 'checked' : ''} /></td>
        <td class="cell-center"><input type="checkbox" data-sound-rule-push="${escapeHtml(rule.class)}" ${rule.push_enabled ? 'checked' : ''} /></td>
        <td class="cell-center"><button class="secondary delete-btn" type="button" data-remove-sound-rule="${escapeHtml(rule.class)}">✕</button></td>
      </tr>`;
  }).join('');

  container.innerHTML = `
    <div class="form-grid compact-grid">
      <label><span>Sound Detection</span>
        <select id="cameraSoundEnabled">
          <option value="false" ${disabledSel}>Disabled</option>
          <option value="true" ${enabledSel}>Enabled (RTSP audio)</option>
        </select>
        <span class="field-help">Listens to this camera's RTSP audio track using YAMNet neural detection.</span>
      </label>
    </div>
    <div class="rule-select-wrapper" style="margin-top:1rem">
      <select id="addSoundRuleSelect">${_soundRuleOptions()}</select>
    </div>
    ${rows ? `<div style="overflow-x:auto; margin-top:.5rem">
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
      </table>
    </div>` : '<p class="muted empty-message" style="margin-top:.5rem">No sound rules configured. Use the dropdown above to add one.</p>'}`;

  document.getElementById('cameraSoundEnabled')?.addEventListener('change', (e) => {
    if (editingSound) editingSound.enabled = e.target.value === 'true';
  });
  document.getElementById('addSoundRuleSelect')?.addEventListener('change', (e) => {
    const classId = e.target.value;
    if (!classId || !editingSound) return;
    const cls = soundClasses.find((c) => c.id === classId);
    if (!cls) return;
    if (!editingSound.rules.some((r) => r.class === classId)) {
      editingSound.rules.push(_defaultSoundRule(cls));
    }
    renderModalSoundSettings();
  });
  container.querySelectorAll('[data-remove-sound-rule]').forEach((btn) => {
    btn.addEventListener('click', () => {
      if (!editingSound) return;
      const classId = btn.dataset.removeSoundRule;
      editingSound.rules = editingSound.rules.filter((r) => r.class !== classId);
      renderModalSoundSettings();
    });
  });
  container.querySelectorAll('[data-sound-rule-enabled]').forEach((cb) => {
    cb.addEventListener('change', () => { _updateSoundRule(cb.dataset.soundRuleEnabled, 'enabled', cb.checked); });
  });
  container.querySelectorAll('[data-sound-rule-record]').forEach((cb) => {
    cb.addEventListener('change', () => { _updateSoundRule(cb.dataset.soundRuleRecord, 'record_on_detect', cb.checked); });
  });
  container.querySelectorAll('[data-sound-rule-threshold]').forEach((inp) => {
    inp.addEventListener('change', () => { _updateSoundRule(inp.dataset.soundRuleThreshold, 'confidence_threshold', Math.max(0.1, Math.min(1.0, Number(inp.value) || 0.35))); });
  });
  container.querySelectorAll('[data-sound-rule-cooldown]').forEach((inp) => {
    inp.addEventListener('change', () => { _updateSoundRule(inp.dataset.soundRuleCooldown, 'cooldown_seconds', Math.max(5, Number.parseInt(inp.value, 10) || 30)); });
  });
  container.querySelectorAll('[data-sound-rule-email]').forEach((cb) => {
    cb.addEventListener('change', () => { _updateSoundRule(cb.dataset.soundRuleEmail, 'email_enabled', cb.checked); });
  });
  container.querySelectorAll('[data-sound-rule-push]').forEach((cb) => {
    cb.addEventListener('change', () => { _updateSoundRule(cb.dataset.soundRulePush, 'push_enabled', cb.checked); });
  });
}

// ─── Load ─────────────────────────────────────────────────────────────────────

async function loadCameras() {
  const me = await api('/api/auth/me');
  csrfToken = me.csrf_token;
  const settings = await api('/api/settings/system');
  cameras = settings.cameras || (settings.camera ? [settings.camera] : []);
  try {
    const { classes } = await api('/api/sound/classes');
    soundClasses = classes || [];
  } catch {
    soundClasses = [];
  }
  updateStats();
  renderGrid();
}

async function updateHealthStats() {
  try {
    const data = await api('/api/cameras/health');
    const s = data.summary;
    if (stats.health) {
      const online = s.online || 0;
      const offline = s.offline || 0;
      stats.health.textContent = `${online} / ${offline}`;
      // Color the stat based on health
      if (offline > 0) {
        stats.health.style.color = 'var(--danger-color, #e74c3c)';
      } else if (online > 0) {
        stats.health.style.color = 'var(--success-color, #2ecc71)';
      }
    }
  } catch {
    // silently ignore — health endpoint may not exist on older versions
  }
}

loadCameras().catch((err) => setMessage(err.message, true));
setInterval(updateHealthStats, 10000);
updateHealthStats();
