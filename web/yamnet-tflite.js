// Sound Model page (web/yamnet-tflite.html, /models/sound).
//
// One card, laid out like Object/Face Models: a status strip, the single
// YAMNet model card, and ONE notice that explains whatever is wrong and what
// to do next. The status and the model used to live in two cards that could
// contradict each other (a red "runtime missing" panel above an amber "will
// download automatically" card); both now render from the same snapshot.
// Per-camera detector status lives on /detection/sounds.

const messageEl = document.getElementById('yamnetMessage');
const statusPanel = document.getElementById('soundStatusPanel');
const refreshBtn = document.getElementById('refreshSoundStatusBtn');
const yamnetModelInfo = document.getElementById('yamnetModelInfo');
const yamnetModelBadges = document.getElementById('yamnetModelBadges');
const yamnetModelSize = document.getElementById('yamnetModelSize');
const yamnetModelUpdatesMessage = document.getElementById('yamnetModelUpdatesMessage');
const checkYamnetUpdateBtn = document.getElementById('checkYamnetUpdateBtn');
const reloadYamnetModelBtn = document.getElementById('reloadYamnetModelBtn');

// api() is provided by web/utils.js (loaded before this script).

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

function percentValue(value) {
  const number = Number(value);
  return Number.isFinite(number) ? `${Math.round(number * 100)}%` : '';
}

function formatBytes(bytes) {
  if (!bytes) return '0 B';
  const units = ['B', 'KB', 'MB', 'GB'];
  let i = 0;
  let size = bytes;
  while (size >= 1024 && i < units.length - 1) { size /= 1024; i++; }
  return `${size.toFixed(i > 0 ? 1 : 0)} ${units[i]}`;
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

// Latest snapshot of everything the card renders from.
const snapshot = { status: null, enabledCameras: [], info: null, error: null };

function modelState(info) {
  if (!info) return 'unknown';
  if (info.available) return 'active';
  if (info.sha256) return 'installed';
  return 'missing';
}

// The one overall state, plus the notice (if any) that explains it. Order
// matters: the first thing that blocks sound detection is the one reported.
function overallState() {
  const { status, enabledCameras, info, error } = snapshot;
  if (error) {
    return { label: 'Unknown', tone: 'model-failed', notice: { tone: 'is-error', title: 'Could not load the sound status', bodyHtml: escapeHtml(error) } };
  }
  const backend = String(status?.backend || '').toLowerCase();
  const model = modelState(info);
  if (!enabledCameras.length) {
    return {
      label: 'Off',
      tone: 'ai-disabled',
      notice: {
        tone: 'is-info',
        title: 'No camera is listening for sounds yet',
        bodyHtml: `Add a sound rule to a camera under <a href="/detection/sounds">Detection → Sounds</a>.${model === 'missing' ? ' The model downloads automatically the first time a sound rule runs.' : ''}`,
      },
    };
  }
  if (backend === 'unavailable') {
    const reason = status?.backend_reason || 'The YAMNet backend could not start.';
    const runtimeMissing = /runtime|litert|tflite_runtime|not installed/i.test(reason);
    return {
      label: 'Unavailable',
      tone: 'model-failed',
      notice: {
        tone: 'is-error',
        title: 'Sound detection is not running - no sound alerts are sent',
        // The reason is server text, so it is escaped. The backend's own
        // message already names the package to install; add the step it
        // leaves out.
        bodyHtml: escapeHtml(reason) + (runtimeMissing ? ' After installing it, restart the server.' : ''),
      },
    };
  }
  if (backend === 'loading') {
    return {
      label: 'Loading',
      tone: 'model-missing',
      notice: { tone: '', title: 'YAMNet is loading', bodyHtml: model === 'missing' ? 'The model is downloading for the first time - this can take a minute.' : 'This usually takes a few seconds.' },
    };
  }
  if ((backend === 'yamnet' || backend === 'yamnet_tflite') && status?.running) {
    return { label: 'Running', tone: 'onnx-active', notice: null };
  }
  return {
    label: displayValue(status?.detector_status || 'stopped'),
    tone: 'ai-disabled',
    notice: { tone: '', title: 'Sound detection is not running yet', bodyHtml: 'Sound-enabled cameras start their detector with the camera stream. Check the per-camera status under Detection → Sounds.' },
  };
}

function renderNotice(notice) {
  if (!notice) {
    messageEl.hidden = true;
    messageEl.innerHTML = '';
    return;
  }
  // bodyHtml is built from fixed copy plus escaped server text.
  messageEl.className = `yamnet-notice ${notice.tone}`;
  messageEl.innerHTML = `<strong>${escapeHtml(notice.title)}</strong><p>${notice.bodyHtml}</p>`;
  messageEl.hidden = false;
}

function renderOverall() {
  const { status, enabledCameras } = snapshot;
  const state = overallState();
  const lastLabel = status?.last_class_label || (status?.last_class ? displayValue(status.last_class) : '');
  const lastConfidence = percentValue(status?.last_confidence);
  const lastHeard = lastLabel ? (lastConfidence ? `${lastLabel} · ${lastConfidence}` : lastLabel) : 'Nothing yet';
  statusPanel.innerHTML = `
    <div><span>Status</span><strong class="ai-mode ${escapeHtml(state.tone)}">${escapeHtml(state.label)}</strong></div>
    <div><span>Sound cameras</span><strong><a href="/detection/sounds">${escapeHtml(enabledCameras.length)} →</a></strong></div>
    <div><span>Last heard</span><strong>${escapeHtml(lastHeard)}</strong></div>
  `;
  renderNotice(state.notice);
}

function renderModel() {
  const info = snapshot.info;
  const model = modelState(info);
  const badges = {
    active: '<span class="model-status model-status-active">● Active</span>',
    installed: '<span class="model-status model-status-installed">○ Installed · not loaded</span>',
    missing: '<span class="model-status model-status-installed">Not downloaded</span>',
    unknown: '',
  };
  yamnetModelBadges.innerHTML = badges[model];
  yamnetModelSize.textContent = info?.model_size ? formatBytes(info.model_size) : '';
  // Nothing to compare against until a model is installed, and each check
  // downloads the whole file, so only offer it once there is one.
  if (checkYamnetUpdateBtn) checkYamnetUpdateBtn.hidden = model !== 'active' && model !== 'installed';
  if (model === 'missing') {
    yamnetModelInfo.textContent = 'Not downloaded yet - it downloads automatically the first time a camera\'s sound rule runs.';
    return;
  }
  if (model === 'unknown') {
    yamnetModelInfo.textContent = '';
    return;
  }
  const installedAt = info.installed_at ? escapeHtml(new Date(info.installed_at).toLocaleDateString(undefined, { day: 'numeric', month: 'short', year: 'numeric' })) : '';
  const hashText = info.sha256 ? escapeHtml(info.sha256) : '';
  const shortHash = info.sha256 ? escapeHtml(`${info.sha256.slice(0, 8)}…${info.sha256.slice(-4)}`) : '';
  yamnetModelInfo.innerHTML = [
    installedAt ? `<span>Installed <strong>${installedAt}</strong></span>` : '',
    hashText ? `<span class="yamnet-model-hash">SHA-256 <code title="${hashText}">${shortHash}</code><button type="button" class="secondary" data-copy-hash="${hashText}" aria-label="Copy the full SHA-256">Copy</button></span>` : '',
  ].filter(Boolean).join('');
}

function render() {
  renderOverall();
  renderModel();
}

async function loadAll() {
  await window.daygleAuthReady;
  refreshBtn.disabled = true;
  try {
    // Camera names are viewer-visible; avoid the admin-gated
    // /api/settings/system (403 for non-admin roles).
    const [settings, status, info] = await Promise.all([
      api('/api/config'),
      api('/api/sound/status'),
      api('/api/sound/model/info'),
    ]);
    const cameras = settings.cameras || (settings.camera ? [settings.camera] : []);
    snapshot.status = status;
    snapshot.enabledCameras = cameras.filter(soundEnabled);
    snapshot.info = info;
    snapshot.error = null;
  } catch (error) {
    // Skip UI updates if api() triggered a 401 redirect
    if (window.daygleAuth?.redirecting) return;
    snapshot.error = error.message;
  } finally {
    refreshBtn.disabled = false;
  }
  render();
}

// ─── Updates ──────────────────────────────────────────────────────────────
let pendingUpdate = null;

function setUpdatesMessage(text, tone) {
  yamnetModelUpdatesMessage.textContent = text;
  yamnetModelUpdatesMessage.className = `model-library-message ${tone}`;
}

async function checkYamnetUpdate() {
  if (!checkYamnetUpdateBtn) return;
  const label = checkYamnetUpdateBtn.querySelector('.model-update-check-label');
  checkYamnetUpdateBtn.disabled = true;
  checkYamnetUpdateBtn.classList.add('is-checking');
  if (label) label.textContent = 'Checking…';
  setUpdatesMessage('Checking for a newer YAMNet model…', 'is-loading');
  try {
    const result = await api('/api/sound/model/check', { method: 'POST' });
    if (result.error) {
      setUpdatesMessage(`Update check failed: ${result.error}`, 'is-error');
    } else if (result.update_available) {
      pendingUpdate = result;
      reloadYamnetModelBtn.hidden = false;
      reloadYamnetModelBtn.textContent = `↻ Update model (${formatBytes(result.latest_size)})`;
      setUpdatesMessage('A newer YAMNet model is available.', 'is-success');
    } else {
      pendingUpdate = null;
      reloadYamnetModelBtn.hidden = true;
      setUpdatesMessage('The YAMNet model is up to date.', 'is-success');
    }
  } catch (err) {
    if (window.daygleAuth?.redirecting) return;
    setUpdatesMessage(`Update check failed: ${err.message}`, 'is-error');
  } finally {
    checkYamnetUpdateBtn.disabled = false;
    checkYamnetUpdateBtn.classList.remove('is-checking');
    if (label) label.textContent = 'Check for updates';
  }
}

async function reloadYamnetModel() {
  if (!reloadYamnetModelBtn || !pendingUpdate) return;
  const original = reloadYamnetModelBtn.textContent;
  reloadYamnetModelBtn.disabled = true;
  reloadYamnetModelBtn.textContent = 'Updating…';
  checkYamnetUpdateBtn.disabled = true;
  try {
    const result = await api('/api/sound/model/reload', { method: 'POST' });
    if (result.ok) {
      pendingUpdate = null;
      reloadYamnetModelBtn.hidden = true;
      setUpdatesMessage('YAMNet model updated.', 'is-success');
      window.showToast?.('YAMNet model updated.');
      await loadAll();
    } else {
      setUpdatesMessage('Failed to update the YAMNet model.', 'is-error');
      window.showToast?.('Failed to update the YAMNet model.', true);
    }
  } catch (err) {
    if (window.daygleAuth?.redirecting) return;
    setUpdatesMessage(`Update failed: ${err.message}`, 'is-error');
    window.showToast?.(`Update failed: ${err.message}`, true);
  } finally {
    reloadYamnetModelBtn.disabled = false;
    if (!reloadYamnetModelBtn.hidden) reloadYamnetModelBtn.textContent = original;
    checkYamnetUpdateBtn.disabled = false;
  }
}

yamnetModelInfo.addEventListener('click', async (event) => {
  const button = event.target instanceof Element ? event.target.closest('button[data-copy-hash]') : null;
  if (!button) return;
  try {
    await navigator.clipboard.writeText(button.getAttribute('data-copy-hash') || '');
    window.showToast?.('SHA-256 copied.');
  } catch {
    window.showToast?.('Could not copy - select the hash instead.', true);
  }
});
checkYamnetUpdateBtn?.addEventListener('click', checkYamnetUpdate);
reloadYamnetModelBtn?.addEventListener('click', reloadYamnetModel);
refreshBtn.addEventListener('click', loadAll);
loadAll();
