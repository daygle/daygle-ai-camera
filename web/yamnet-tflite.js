const messageEl = document.getElementById('yamnetMessage');
const statusPanel = document.getElementById('soundStatusPanel');
// Per-camera detector status now lives on /sounds. This page only owns the
// backend and model diagnostics.
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

function cameraLabel(camera) {
  const name = String(camera?.name || '').trim();
  const id = String(camera?.id || '').trim();
  if (name && id) return `${name} (${id})`;
  return name || id || 'Unknown camera';
}

function renderOverall(status, enabledCameras) {
  const backend = status.backend || 'none';
  statusPanel.className = `status-panel yamnet-status-grid ${backendTone(backend)}`;
  // Five short diagnostics sit in one responsive row (auto-fit grid). The
  // full-width "Status Detail" note only renders when the backend is not in
  // a healthy active state (or carries a diagnostic reason) -- a healthy
  // panel doesn't need a whole row saying it's active.
  const activeBackend = backend === 'yamnet' || backend === 'yamnet_tflite';
  const detailNote = backendNote(backend, status.backend_reason);
  statusPanel.innerHTML = `
    <div><span>Backend</span><strong>${escapeHtml(displayValue(backend, 'None'))}</strong></div>
    <div><span>Running</span><strong>${escapeHtml(yesNo(status.running))}</strong></div>
    <div><span>Sound Cameras</span><strong>${escapeHtml(enabledCameras.length)}</strong></div>
    <div><span>Last Sound</span><strong>${escapeHtml(status.last_class_label || displayValue(status.last_class, 'None'))}</strong></div>
    <div><span>Last Confidence</span><strong>${escapeHtml(percentValue(status.last_confidence))}</strong></div>
    ${(!activeBackend || status.backend_reason) ? `<div class="wide"><span>Status Detail</span><strong>${escapeHtml(detailNote)}</strong></div>` : ''}
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
    renderOverall(overall, enabledCameras);
  } catch (error) {
    // Skip UI updates if api() triggered a 401 redirect
    if (window.daygleAuth?.redirecting) return;
    messageEl.textContent = error.message;
    statusPanel.className = 'status-panel yamnet-status-grid status-error';
    statusPanel.innerHTML = `<div><span>Status</span><strong>${escapeHtml(error.message)}</strong></div>`;
    // Camera-by-camera diagnostics are rendered on /sounds.
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

let pendingUpdate = null;

async function loadYamnetModelInfo() {
  try {
    const info = await api('/api/sound/model/info');
    if (!yamnetModelInfo) return;
    if (!info.available && !info.sha256) {
      yamnetModelInfo.innerHTML = `
        <div class="yamnet-model-empty">
          <div class="yamnet-model-empty-icon" aria-hidden="true">∿</div>
          <div class="yamnet-model-empty-copy">
            <strong>Model not installed yet</strong>
            <p class="muted">YAMNet will download automatically when sound detection is first enabled.</p>
          </div>
          <span class="yamnet-model-status-pill status-warning">Not installed</span>
        </div>`;
      return;
    }
    const installedAt = info.installed_at ? escapeHtml(new Date(info.installed_at).toLocaleDateString()) : '';
    const statusLabel = info.available ? 'Model Loaded' : 'Model File Found';
    const statusClass = info.available ? 'status-ok' : 'status-warning';
    const sizeText = info.model_size ? escapeHtml(formatBytes(info.model_size)) : 'Unknown';
    const hashText = info.sha256 ? escapeHtml(info.sha256) : 'Not available';
    const installedText = installedAt || 'Unknown';
    yamnetModelInfo.innerHTML = `
      <div class="yamnet-model-overview">
        <div class="yamnet-model-state">
          <span class="yamnet-model-orb ${statusClass}" aria-hidden="true"></span>
          <div>
            <span class="yamnet-model-status-pill ${statusClass}">${statusLabel}</span>
            <p class="yamnet-model-caption">${info.available ? 'Ready for CPU audio classification.' : 'The file is present but the detector is not currently loaded.'}</p>
          </div>
        </div>
        <div class="yamnet-model-specs">
          <div class="yamnet-model-spec">
            <span>File size</span>
            <strong>${sizeText}</strong>
          </div>
          <div class="yamnet-model-spec">
            <span>Installed</span>
            <strong>${installedText}</strong>
          </div>
          <div class="yamnet-model-spec yamnet-model-spec-wide">
            <span>SHA-256</span>
            <code title="${hashText}">${hashText}</code>
          </div>
        </div>
      </div>`;
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
        reloadYamnetModelBtn.hidden = false;
        reloadYamnetModelBtn.textContent = `Update Model (${formatBytes(result.latest_size)})`;
      }
      window.showToast?.('A newer YAMNet model is available!');
    } else {
      pendingUpdate = null;
      if (reloadYamnetModelBtn) reloadYamnetModelBtn.hidden = true;
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
      reloadYamnetModelBtn.hidden = true;
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
