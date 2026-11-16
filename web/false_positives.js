// false_positives.js - the False Positives section of Intelligence > AI
// (ai.html). Admin-only. Its own settings (GET/PUT
// /api/settings/false-positives, app/false_positives.py) and Save button, so
// it never overwrites the model settings form above it. Uses api() /
// escapeHtml / showToast from utils.js.

const fpForm = document.getElementById('fpSettingsForm');
const fpPurgeBtn = document.getElementById('fpPurgeBtn');

const FP_FORM_DEFAULTS = {
  ai_check: false,
  purge_enabled: false,
  purge_after_hours: 24,
  purge_include_ai: true,
};
const FP_BOOLEAN_FIELDS = new Set(['ai_check', 'purge_enabled', 'purge_include_ai']);

function fpToast(text, isError = false) {
  if (text) window.showToast?.(text, isError);
}

function fpGuard(fn) {
  return async (...args) => {
    try {
      await fn(...args);
    } catch (error) {
      if (window.daygleAuth?.redirecting) return;
      fpToast(error.message, true);
    }
  };
}

function fillFalsePositiveForm(settings) {
  if (!fpForm) return;
  const values = { ...FP_FORM_DEFAULTS, ...(settings || {}) };
  Object.keys(FP_FORM_DEFAULTS).forEach((key) => {
    const field = fpForm.elements[key];
    if (field) field.value = String(values[key]);
  });
  const selected = new Set((Array.isArray(values.ai_check_camera_ids) ? values.ai_check_camera_ids : []).map(String));
  fpForm.querySelectorAll('input[name="fp_camera_id"]').forEach((box) => {
    box.checked = selected.has(box.value);
  });
}

function falsePositivePayload() {
  const data = {};
  Object.keys(FP_FORM_DEFAULTS).forEach((key) => {
    const field = fpForm.elements[key];
    if (!field) return;
    const raw = String(field.value ?? '').trim();
    if (FP_BOOLEAN_FIELDS.has(key)) data[key] = raw === 'true';
    else if (raw !== '') data[key] = Number.parseInt(raw, 10);
  });
  data.ai_check_camera_ids = [...fpForm.querySelectorAll('input[name="fp_camera_id"]:checked')].map((box) => box.value);
  return data;
}

function renderFalsePositiveCameras(cameras, selectedIds) {
  const container = document.getElementById('fpCameras');
  if (!container) return;
  if (!cameras.length) {
    container.innerHTML = '<span class="muted">No cameras configured.</span>';
    return;
  }
  const selected = new Set((selectedIds || []).map(String));
  container.innerHTML = cameras.map((camera) => {
    const id = String(camera.id || '');
    return `<label><input type="checkbox" name="fp_camera_id" value="${escapeHtml(id)}"${selected.has(id) ? ' checked' : ''} /> ${escapeHtml(camera.name || id)}</label>`;
  }).join('');
}

// One line: what was marked in the last 7 days and what the purge would
// remove now (past the grace period).
function describeFalsePositiveStats(stats) {
  if (!stats) return '';
  const days = stats.days || 7;
  const ai = Number(stats.ai || 0);
  const user = Number(stats.user || 0);
  const parts = [`Last ${days} days: ${ai} marked by the AI, ${user} by users`];
  if (stats.purgeable_recordings != null) {
    const recordings = Number(stats.purgeable_recordings || 0);
    const events = Number(stats.purgeable_events || 0);
    parts.push(`ready to purge: ${recordings} recording${recordings === 1 ? '' : 's'}, ${events} snapshot-only event${events === 1 ? '' : 's'}`);
  }
  return `${parts.join('; ')}.`;
}

function loadFalsePositiveStats() {
  return api('/api/settings/false-positives/stats?days=7').then((stats) => {
    const el = document.getElementById('fpStats');
    if (el) el.textContent = describeFalsePositiveStats(stats);
  }).catch(() => {});
}

async function loadFalsePositiveSection() {
  if (!fpForm) return;
  await window.daygleAuthReady;
  const [settings, cameraPayload] = await Promise.all([
    api('/api/settings/false-positives'),
    api('/api/cameras').catch(() => ({ cameras: [] })),
  ]);
  renderFalsePositiveCameras(cameraPayload?.cameras || [], settings?.ai_check_camera_ids);
  fillFalsePositiveForm(settings);
  loadFalsePositiveStats();
}

fpForm?.addEventListener('submit', fpGuard(async (event) => {
  event.preventDefault();
  fillFalsePositiveForm(await api('/api/settings/false-positives', {
    method: 'PUT',
    body: JSON.stringify(falsePositivePayload()),
  }));
  fpToast('False positive settings saved.');
  loadFalsePositiveStats();
}));

fpPurgeBtn?.addEventListener('click', fpGuard(async () => {
  if (!confirm('Delete every false-positive recording and snapshot older than Purge After? This cannot be undone.')) return;
  fpPurgeBtn.disabled = true;
  try {
    const result = await api('/api/settings/false-positives/purge', { method: 'POST' });
    const recordings = Number(result?.recordings_purged || 0);
    const events = Number(result?.events_purged || 0);
    fpToast(`Purged ${recordings} recording${recordings === 1 ? '' : 's'} and ${events} event${events === 1 ? '' : 's'}.`);
  } finally {
    fpPurgeBtn.disabled = false;
    loadFalsePositiveStats();
  }
}));

loadFalsePositiveSection().catch((error) => {
  if (window.daygleAuth?.redirecting) return;
  fpToast(`Could not load false positive settings: ${error.message}`, true);
});
