const messageEl = document.getElementById('yamnetMessage');
const statusPanel = document.getElementById('soundStatusPanel');
const cameraList = document.getElementById('soundCameraStatusList');
const refreshBtn = document.getElementById('refreshSoundStatusBtn');

async function api(path, options = {}) {
  const response = await fetch(path, options);
  if (response.status === 401) {
    window.location.href = '/login';
    throw new Error('Authentication required');
  }
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(payload.detail || `Request failed: ${response.status}`);
  return payload;
}

function titleCaseWords(value) {
  return String(value || '')
    .replace(/[_-]+/g, ' ')
    .replace(/\s+/g, ' ')
    .trim()
    .split(' ')
    .filter(Boolean)
    .map((word) => {
      const normalized = word.toLowerCase();
      const acronyms = { ai: 'AI', api: 'API', onvif: 'ONVIF', rtsp: 'RTSP', url: 'URL', yamnet: 'YAMNet' };
      if (acronyms[normalized]) return acronyms[normalized];
      return word.charAt(0).toUpperCase() + word.slice(1).toLowerCase();
    })
    .join(' ');
}

function displayValue(value, fallback = 'None') {
  if (value === null || value === undefined || value === '') return fallback;
  return titleCaseWords(String(value));
}

function yesNo(value) {
  return value ? 'Yes' : 'No';
}

function percentValue(value) {
  const number = Number(value);
  return Number.isFinite(number) ? `${Math.round(number * 100)}%` : 'None';
}

function backendTone(backend) {
  const normalized = String(backend || '').toLowerCase();
  if (normalized === 'yamnet') return 'status-ok';
  if (normalized === 'spectral') return 'status-warning';
  if (normalized === 'loading') return 'status-warning';
  return 'status-error';
}

function backendNote(backend) {
  const normalized = String(backend || '').toLowerCase();
  if (normalized === 'yamnet') return 'YAMNet neural audio classification is active.';
  if (normalized === 'spectral') return 'Spectral fallback is active; expect more false positives.';
  if (normalized === 'loading') return 'YAMNet is still loading.';
  if (!normalized || normalized === 'none') return 'No sound detector backend is currently active.';
  return `Sound backend reported ${displayValue(backend)}.`;
}

function formatConfidenceMap(confidences = {}) {
  const entries = Object.entries(confidences || {})
    .filter(([, value]) => Number.isFinite(Number(value)))
    .sort((a, b) => Number(b[1]) - Number(a[1]))
    .slice(0, 4);
  if (!entries.length) return 'None';
  return entries
    .map(([label, value]) => `${titleCaseWords(label)} ${Math.round(Number(value) * 100)}%`)
    .join(', ');
}

function soundEnabled(camera) {
  const sound = camera?.detection?.sound || {};
  return sound.enabled === true && (sound.rules || []).some((rule) => rule.enabled === true);
}

function cameraLabel(camera) {
  const name = String(camera?.name || '').trim();
  const id = String(camera?.id || '').trim();
  if (name && id) return `${name} (${id})`;
  return name || id || 'Unknown camera';
}

function renderOverall(status, enabledCameras) {
  const backend = status.backend || 'none';
  statusPanel.className = `status-panel ${backendTone(backend)}`;
  statusPanel.innerHTML = `
    <div><span>Backend</span><strong>${escapeHtml(displayValue(backend, 'None'))}</strong></div>
    <div><span>Running</span><strong>${yesNo(status.running)}</strong></div>
    <div><span>Detector Status</span><strong>${escapeHtml(displayValue(status.detector_status || status.state, 'Disabled'))}</strong></div>
    <div><span>Sound Cameras</span><strong>${enabledCameras.length}</strong></div>
    <div><span>Last Sound</span><strong>${escapeHtml(status.last_class_label || displayValue(status.last_class, 'None'))}</strong></div>
    <div><span>Last Confidence</span><strong>${percentValue(status.last_confidence)}</strong></div>
    <div class="wide"><span>Status Detail</span><strong>${escapeHtml(backendNote(backend))}</strong></div>
  `;
}

function renderCameraStatuses(rows) {
  if (!rows.length) {
    cameraList.innerHTML = '<p class="muted empty-message">No sound-enabled cameras are configured.</p>';
    return;
  }
  cameraList.innerHTML = `
    <div style="overflow-x:auto">
      <table class="rule-table">
        <thead>
          <tr>
            <th>Camera</th>
            <th>Backend</th>
            <th>Running</th>
            <th>Status</th>
            <th>Last Sound</th>
            <th>Recent Scores</th>
          </tr>
        </thead>
        <tbody>
          ${rows.map(({ camera, status }) => `
            <tr>
              <td class="cell-label">${escapeHtml(cameraLabel(camera))}</td>
              <td>${escapeHtml(displayValue(status.backend, 'None'))}</td>
              <td>${yesNo(status.running)}</td>
              <td>${escapeHtml(displayValue(status.detector_status || status.state, 'Disabled'))}</td>
              <td>${escapeHtml(status.last_class_label || displayValue(status.last_class, 'None'))}</td>
              <td>${escapeHtml(formatConfidenceMap(status.last_confidences))}</td>
            </tr>
          `).join('')}
        </tbody>
      </table>
    </div>
  `;
}

async function loadSoundStatus() {
  messageEl.textContent = '';
  refreshBtn.disabled = true;
  try {
    const [settings, overall] = await Promise.all([
      api('/api/settings/system'),
      api('/api/sound/status'),
    ]);
    const cameras = settings.cameras || (settings.camera ? [settings.camera] : []);
    const enabledCameras = cameras.filter(soundEnabled);
    const rows = await Promise.all(enabledCameras.map(async (camera) => ({
      camera,
      status: await api(`/api/sound/status?camera_id=${encodeURIComponent(camera.id)}`).catch(() => ({
        state: 'unavailable',
        detector_status: 'unavailable',
        running: false,
        backend: null,
        last_confidences: {},
      })),
    })));
    renderOverall(overall, enabledCameras);
    renderCameraStatuses(rows);
  } catch (error) {
    messageEl.textContent = error.message;
    statusPanel.className = 'status-panel status-error';
    statusPanel.innerHTML = `<div><span>Status</span><strong>${escapeHtml(error.message)}</strong></div>`;
    cameraList.innerHTML = '';
  } finally {
    refreshBtn.disabled = false;
  }
}

refreshBtn.addEventListener('click', loadSoundStatus);
loadSoundStatus();
