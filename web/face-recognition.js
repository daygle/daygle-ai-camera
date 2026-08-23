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

// ── Face detection mode (per-object override, lives in /api/settings/objects) ─
// Faces default to "moving and still" on the backend regardless of the global
// Objects default; an explicit override here wins. Stored as labels.face in
// the objects settings, and no longer shown on the Objects page itself.
async function loadFaceMode() {
  try {
    const objectSettings = await api('/api/settings/objects');
    const override = objectSettings?.labels?.face;
    frForm.face_mode.value = override || 'inherit';
  } catch (err) {
    // Non-fatal: leave the select on its default rather than blocking the page.
    frForm.face_mode.value = 'inherit';
  }
}

// Read the select and merge it into the CURRENT objects settings so other
// per-object overrides are untouched. 'inherit' removes the explicit entry,
// restoring the backend's moving-and-still default for faces.
async function saveFaceMode() {
  const mode = frForm.face_mode.value;
  const current = await api('/api/settings/objects');
  const labels = { ...(current.labels || {}) };
  if (mode === 'inherit') delete labels.face;
  else labels.face = mode;
  await api('/api/settings/objects', {
    method: 'PUT',
    body: JSON.stringify({
      default_mode: current.default_mode || 'moving',
      labels,
      group_modes: current.group_modes || {},
      still_alerts: current.still_alerts || {},
    }),
  });
}

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
    // Face-recognition settings (from /api/settings/face-recognition) and
    // face detection confidence (from /api/settings/ai) live in separate
    // stored keys, so we fetch both.
    const [status, aiStatus] = await Promise.all([
      api('/api/settings/face-recognition'),
      api('/api/settings/ai').catch(() => ({})),
    ]);
    fillForm(status);
    // face_confidence lives in the AI settings store; populate the input
    // here so saveSettings can carry it back on the companion AI PUT.
    if (frForm.elements['face_confidence']) {
      const conf = aiStatus.face_confidence;
      frForm.face_confidence.value = (conf != null && conf !== '') ? String(conf) : '';
    }
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
    // Persist the face detection-mode override first so a failure there is
    // reported independently of the recognition-settings save.
    let faceModeError = null;
    try {
      await saveFaceMode();
    } catch (err) {
      faceModeError = err;
    }
    const status = await api('/api/settings/face-recognition', { method: 'PUT', body: JSON.stringify(body) });
    fillForm(status);
    // Face confidence lives in the AI settings store, so save it there too.
    // Blank = inherit Min Confidence from the ONNX page.
    let faceConfError = null;
    try {
      const confVal = frForm.elements['face_confidence']?.value.trim();
      const aiPayload = confVal !== '' ? { face_confidence: Number(confVal) } : { face_confidence: '' };
      await api('/api/settings/ai', { method: 'PUT', body: JSON.stringify(aiPayload) });
    } catch (err) {
      faceConfError = err;
    }
    if (faceModeError) {
      showToast(faceModeError.message || 'Failed to save the face detection mode.', true);
    } else if (faceConfError) {
      showToast(faceConfError.message || 'Failed to save face confidence.', true);
    } else {
      showToast('Face settings saved.');
    }
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
loadFaceMode();
// Tab bar (Settings / People). Shared implementation with URL-hash
// deep-linking lives in utils.js - /face-recognition#people opens People.
initDaygleTabs();
