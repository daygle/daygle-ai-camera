let cameras = [];
let selectedCameraId = '';
let selectedStatus = null;

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
  soundCameraStatusList.innerHTML = `
    <div style="overflow-x:auto">
      <table class="rule-table">
        <thead><tr>
          <th>Camera</th><th>Configured</th><th>Enabled Sounds</th><th>Backend</th>
          <th>Running</th><th>Status</th><th>Last Sound</th>
          <th>Recent Scores</th><th>Detail</th>
        </tr></thead>
        <tbody>${rowsHtml}</tbody>
      </table>
    </div>
  `;
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
  const settings = await api('/api/settings/system');
  cameras = settings.cameras || (settings.camera ? [settings.camera] : []);
  const requested = new URLSearchParams(window.location.search).get('camera');
  selectedCameraId = requested && cameras.some((camera) => camera.id === requested)
    ? requested
    : (selectedCameraId || cameras[0]?.id || '');
  await refreshStatus();
  await refreshDetectorStatuses();
}

cameraSelect.addEventListener('change', () => selectCamera(cameraSelect.value));
soundEnabled.addEventListener('change', renderStatus);
saveBtn.addEventListener('click', saveSoundDetection);
reloadBtn.addEventListener('click', () => loadSounds().catch((err) => {
  if (!window.daygleAuth?.redirecting) setMessage(err.message, true);
}));

loadSounds().catch((err) => {
  if (!window.daygleAuth?.redirecting) setMessage(err.message, true);
});
