const messageEl = document.getElementById('yamnetMessage');
const statusPanel = document.getElementById('soundStatusPanel');
const cameraList = document.getElementById('soundCameraStatusList');
const refreshBtn = document.getElementById('refreshSoundStatusBtn');
const yamnetModelInfo = document.getElementById('yamnetModelInfo');
const checkYamnetUpdateBtn = document.getElementById('checkYamnetUpdateBtn');
const reloadYamnetModelBtn = document.getElementById('reloadYamnetModelBtn');

// api() is provided by web/utils.js (loaded before this script). yamnet-tflite
// is a read-only status page (no POST/PUT/DELETE), so the CSRF/Content-Type
// rules in utils.js's api() simply don't fire here. The thin local wrapper
// that only handled 401 redirects was removed; utils.js's api() throws on 401
// after redirecting to /login, which is what every refresh path here already
// surfaces via the .catch(err) on loadSoundStatus().

function titleCaseWords(value) {
  return String(value || '')
    .replace(/[_-]+/g, ' ')
    .replace(/\s+/g, ' ')
    .trim()
    .split(' ')
    .filter(Boolean)
    .map((word) => {
      const normalized = word.toLowerCase();
      const acronyms = { ai: 'AI', api: 'API', onvif: 'ONVIF', rtsp: 'RTSP', tflite: 'TFLite', url: 'URL', yamnet: 'YAMNet' };
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
  if (normalized === 'yamnet' || normalized === 'yamnet_tflite') return 'status-ok';
  if (normalized === 'loading') return 'status-warning';
  if (normalized === 'unavailable') return 'status-error';
  return 'status-error';
}

function backendNote(backend, reason = '') {
  const normalized = String(backend || '').toLowerCase();
  if (normalized === 'yamnet' || normalized === 'yamnet_tflite') return 'YAMNet TFLite CPU audio classification is active.';
  if (normalized === 'loading') return 'YAMNet is still loading.';
  if (normalized === 'unavailable') return reason || 'YAMNet TFLite is unavailable; sound alerts will not be emitted.';
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

function soundConfig(camera) {
  return camera?.detection?.sound || {};
}

function enabledSoundRules(camera) {
  return (soundConfig(camera).rules || []).filter((rule) => rule.enabled === true);
}

function soundEnabled(camera) {
  const sound = soundConfig(camera);
  return sound.enabled === true && enabledSoundRules(camera).length > 0;
}

function hasRtspConfig(camera) {
  return Boolean(camera?.stream_url || camera?.rtsp_url || camera?.host);
}

function cameraSoundReason(camera, status) {
  const sound = soundConfig(camera);
  if (sound.enabled !== true) return 'Sound disabled';
  if (!enabledSoundRules(camera).length) return 'No enabled sound rules';
  if (!hasRtspConfig(camera)) return 'No RTSP stream configured';
  if (status.running) return 'Running';
  return displayValue(status.detector_status || status.state, 'Not running');
}

function cameraSoundClass(camera, status) {
  if (status.running) return 'status-ok';
  const reason = cameraSoundReason(camera, status).toLowerCase();
  if (reason.includes('loading')) return 'status-warning';
  if (reason === 'sound disabled' || reason === 'no enabled sound rules') return '';
  return 'status-error';
}

function soundConfigured(camera) {
  const sound = camera?.detection?.sound || {};
  return sound.enabled === true;
}

function cameraLabel(camera) {
  const name = String(camera?.name || '').trim();
  const id = String(camera?.id || '').trim();
  if (name && id) return `${name} (${id})`;
  return name || id || 'Unknown camera';
}

function renderOverall(status, enabledCameras) {
  const backend = status.backend || 'none';
  statusPanel.className = `status-panel yamnet-status-grid ${backendTone(backend)}`;
  statusPanel.innerHTML = `
    <div><span>Backend</span><strong>${escapeHtml(displayValue(backend, 'None'))}</strong></div>            <div><span>Running</span><strong>${escapeHtml(yesNo(status.running))}</strong></div>
    <div><span>Detector Status</span><strong>${escapeHtml(displayValue(status.detector_status || status.state, 'Disabled'))}</strong></div>            <div><span>Sound Cameras</span><strong>${escapeHtml(enabledCameras.length)}</strong></div>
    <div><span>Last Sound</span><strong>${escapeHtml(status.last_class_label || displayValue(status.last_class, 'None'))}</strong></div>            <div><span>Last Confidence</span><strong>${escapeHtml(percentValue(status.last_confidence))}</strong></div>
    <div class="wide"><span>Status Detail</span><strong>${escapeHtml(backendNote(backend, status.backend_reason))}</strong></div>
  `;
}

function renderCameraStatuses(rows) {
  if (!rows.length) {
    cameraList.innerHTML = '<p class="muted empty-message">No cameras are configured.</p>';
    return;
  }
  cameraList.innerHTML = `
    <div style="overflow-x:auto">
      <table class="rule-table">
        <thead>
          <tr>
            <th>Camera</th>
            <th>Configured</th>
            <th>Rules</th>
            <th>Backend</th>
            <th>Running</th>
            <th>Status</th>
            <th>Last Sound</th>
            <th>Recent Scores</th>
            <th>Detail</th>
          </tr>
        </thead>
        <tbody>
          ${rows.map(({ camera, status }) => `
            <tr class="${escapeHtml(cameraSoundClass(camera, status))}">
              <td class="cell-label">${escapeHtml(cameraLabel(camera))}</td>
              <td>${escapeHtml(yesNo(soundConfigured(camera)))}</td>
              <td>${escapeHtml(enabledSoundRules(camera).length)}</td>
              <td>${escapeHtml(displayValue(status.backend, 'None'))}</td>
              <td>${escapeHtml(yesNo(status.running))}</td>
              <td>${escapeHtml(cameraSoundReason(camera, status))}</td>
              <td>${escapeHtml(status.last_class_label || displayValue(status.last_class, 'None'))}</td>
              <td>${escapeHtml(formatConfidenceMap(status.last_confidences))}</td>
              <td>${escapeHtml(status.backend_reason || '')}</td>
            </tr>
          `).join('')}
        </tbody>
      </table>
    </div>
  `;
}

async function loadSoundStatus() {
  await window.daygleAuthReady;
  messageEl.textContent = '';
  refreshBtn.disabled = true;
  try {
    const [settings, overall] = await Promise.all([
      api('/api/settings/system'),
      api('/api/sound/status'),
    ]);
    const cameras = settings.cameras || (settings.camera ? [settings.camera] : []);
    const enabledCameras = cameras.filter(soundEnabled);
    const rows = await Promise.all(cameras.map(async (camera) => ({
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
    // Skip UI updates if api() triggered a 401 redirect
    if (window.daygleAuth?.redirecting) return;
    messageEl.textContent = error.message;
    statusPanel.className = 'status-panel yamnet-status-grid status-error';
    statusPanel.innerHTML = `<div><span>Status</span><strong>${escapeHtml(error.message)}</strong></div>`;
    cameraList.innerHTML = '';
  } finally {
    refreshBtn.disabled = false;
  }
}

// ─── YAMNet model management ──────────────────────────────────────────────
function formatBytes(bytes) {
  if (!bytes) return '0 B';
  const units = ['B', 'KB', 'MB', 'GB'];
  let i = 0;
  let size = bytes;
  while (size >= 1024 && i < units.length - 1) { size /= 1024; i++; }
  return `${size.toFixed(i > 0 ? 1 : 0)} ${units[i]}`;
}

function formatSha(sha) {
  if (!sha) return 'unknown';
  return sha.substring(0, 12) + '…';
}

let pendingUpdate = null;

async function loadYamnetModelInfo() {
  try {
    const info = await api('/api/sound/model/info');
    if (!yamnetModelInfo) return;
    if (!info.available && !info.sha256) {
      yamnetModelInfo.innerHTML = '<p class="muted">YAMNet model not yet downloaded. It will be downloaded automatically when sound detection is first enabled.</p>';
      return;
    }
    const sizeInfo = info.model_size ? ` · ${escapeHtml(formatBytes(info.model_size))}` : '';
    const shaInfo = info.sha256 ? `SHA-256: ${escapeHtml(formatSha(info.sha256))}` : '';
    const installedAt = info.installed_at ? escapeHtml(new Date(info.installed_at).toLocaleDateString()) : '';
    yamnetModelInfo.innerHTML = `<div class="yamnet-model-details"><span class="yamnet-model-status ${info.available ? 'status-ok' : 'status-warning'}">${info.available ? 'Model loaded' : 'Model not loaded'}</span>${sizeInfo}${shaInfo ? ` · ${shaInfo}` : ''}${installedAt ? ` · Installed ${installedAt}` : ''}</div>`;
  } catch (err) {
    if (window.daygleAuth?.redirecting) return;
    if (yamnetModelInfo) yamnetModelInfo.innerHTML = `<p class="muted">Could not load model info: ${escapeHtml(err.message)}</p>`;
  }
}

async function checkYamnetUpdate() {
  if (!checkYamnetUpdateBtn) return;
  checkYamnetUpdateBtn.disabled = true;
  checkYamnetUpdateBtn.textContent = 'Checking…';
  try {
    const result = await api('/api/sound/model/check', { method: 'POST' });
    if (result.error) {
      window.showToast?.(`Update check failed: ${result.error}`, true);
    } else if (result.update_available) {
      pendingUpdate = result;
      if (reloadYamnetModelBtn) {
        reloadYamnetModelBtn.style.display = '';
        reloadYamnetModelBtn.textContent = `Update Model (${formatBytes(result.latest_size)})`;
      }
      window.showToast?.('A newer YAMNet model is available!');
    } else {
      pendingUpdate = null;
      if (reloadYamnetModelBtn) reloadYamnetModelBtn.style.display = 'none';
      window.showToast?.('YAMNet model is up to date.');
    }
  } catch (err) {
    if (window.daygleAuth?.redirecting) return;
    window.showToast?.(`Update check failed: ${err.message}`, true);
  } finally {
    checkYamnetUpdateBtn.disabled = false;
    checkYamnetUpdateBtn.textContent = 'Check for Update';
  }
}

async function reloadYamnetModel() {
  if (!reloadYamnetModelBtn || !pendingUpdate) return;
  reloadYamnetModelBtn.disabled = true;
  reloadYamnetModelBtn.textContent = 'Updating…';
  checkYamnetUpdateBtn.disabled = true;
  try {
    const result = await api('/api/sound/model/reload', { method: 'POST' });
    if (result.ok) {
      pendingUpdate = null;
      reloadYamnetModelBtn.style.display = 'none';
      window.showToast?.('YAMNet model updated successfully!');
      await loadYamnetModelInfo();
      await loadSoundStatus();
    } else {
      window.showToast?.('Failed to update YAMNet model.', true);
    }
  } catch (err) {
    if (window.daygleAuth?.redirecting) return;
    window.showToast?.(`Update failed: ${err.message}`, true);
  } finally {
    reloadYamnetModelBtn.disabled = false;
    reloadYamnetModelBtn.textContent = 'Update Model';
    checkYamnetUpdateBtn.disabled = false;
  }
}

checkYamnetUpdateBtn?.addEventListener('click', checkYamnetUpdate);
reloadYamnetModelBtn?.addEventListener('click', reloadYamnetModel);

refreshBtn.addEventListener('click', () => {
  loadSoundStatus();
  loadYamnetModelInfo();
});
loadSoundStatus();
loadYamnetModelInfo();
