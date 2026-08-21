// face-recognition.js - Face Recognition settings page (face-recognition.html).
// Admin-only. Uses api() / showToast from utils.js.
//
// This page is Settings only. The recognition Status readout and the
// embedding-model downloads / switching live on the ArcFace page
// (web/arcface.html) - Face Recognition here just edits the recognition
// settings that reference the active model. The active model itself is chosen
// on the ArcFace page; the hidden model_path / model_id fields below are
// populated from the loaded settings and carried back on save so changing a
// setting never clears the active model.

const frForm = document.getElementById('frForm');
const frMessage = document.getElementById('frMessage');
const frSaveBtn = document.getElementById('frSaveBtn');
const frReloadBtn = document.getElementById('frReloadBtn');

function fillForm(status) {
  frForm.enabled.value = status.enabled ? 'true' : 'false';
  frForm.alert_unknown.value = status.alert_unknown ? 'true' : 'false';
  frForm.alert_unknown_email.value = status.alert_unknown_email ?? '';
  frForm.match_threshold.value = status.match_threshold ?? 0.5;
  frForm.min_face_pixels.value = status.min_face_pixels ?? 0;
  frForm.retention_days.value = status.retention_days ?? 0;
  // The active model is chosen on the ArcFace page - carry the current values
  // through so a save never wipes it (the backend treats a missing model_path
  // as "no model selected").
  frForm.model_path.value = status.model_path ?? '';
  frForm.model_id.value = status.model_id ?? 'arcface';
}

async function loadSettings() {
  try {
    const status = await api('/api/settings/face-recognition');
    fillForm(status);
  } catch (err) {
    frMessage.textContent = err.message || 'Failed to load settings.';
  }
}

async function saveSettings(event) {
  event.preventDefault();
  const body = {
    enabled: frForm.enabled.value === 'true',
    alert_unknown: frForm.alert_unknown.value === 'true',
    alert_unknown_email: frForm.alert_unknown_email.value.trim(),
    match_threshold: parseFloat(frForm.match_threshold.value),
    min_face_pixels: parseInt(frForm.min_face_pixels.value || '0', 10),
    retention_days: parseInt(frForm.retention_days.value || '0', 10),
    model_path: frForm.model_path.value,
    model_id: frForm.model_id.value,
  };
  frSaveBtn.disabled = true;
  try {
    const status = await api('/api/settings/face-recognition', { method: 'PUT', body: JSON.stringify(body) });
    fillForm(status);
    showToast('Face recognition settings saved.');
    if (status.enabled && status.reload_error) {
      showToast(status.reload_error, true);
    }
  } catch (err) {
    showToast(err.message || 'Failed to save settings.', true);
  } finally {
    frSaveBtn.disabled = false;
  }
}

async function reloadService() {
  frReloadBtn.disabled = true;
  try {
    await api('/api/settings/face-recognition/reload', { method: 'POST' });
    showToast('Recognition service reloaded.');
  } catch (err) {
    showToast(err.message || 'Reload failed.', true);
  } finally {
    frReloadBtn.disabled = false;
  }
}

frForm.addEventListener('submit', saveSettings);
frReloadBtn.addEventListener('click', reloadService);

loadSettings();
