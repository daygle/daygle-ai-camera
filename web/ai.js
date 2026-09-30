// ai.js - Intelligence > AI page (ai.html). Admin-only.
// The local vision model (app/ai_verification.py): server + model, shared
// alert-verification options (verification itself is ticked per alert rule on
// the Alerts page), event descriptions / search, and the past-events backfill.
// One form, one PUT /api/settings/ai-verification. Uses api() / escapeHtml /
// showToast from utils.js.

const aiForm = document.getElementById('aiSettingsForm');
const aiTestBtn = document.getElementById('aiTestBtn');
const aiBackfillBtn = document.getElementById('aiBackfillBtn');
let aiCameraIds = [];
let aiBackfillTimer = null;

// Keep the form usable with a partial API response; the backend owns
// validation and persistence.
const AI_FORM_DEFAULTS = {
  server_url: 'http://127.0.0.1:11434/v1',
  model: 'gemma3:4b',
  api_key: '',
  timeout_seconds: 20,
  focus_crop: 'true',
  describe_events: 'off',
};
const AI_BOOLEAN_FIELDS = new Set(['focus_crop']);
const AI_INTEGER_FIELDS = new Set(['timeout_seconds']);

function aiMessage(text, isError = false) {
  if (text) window.showToast?.(text, isError);
}

function aiGuard(fn) {
  return async (...args) => {
    try {
      await fn(...args);
    } catch (error) {
      if (window.daygleAuth?.redirecting) return;
      aiMessage(error.message, true);
    }
  };
}

function fillAiForm(settings) {
  if (!aiForm) return;
  const values = { ...AI_FORM_DEFAULTS, ...(settings || {}) };
  Object.entries(values).forEach(([key, value]) => {
    const field = aiForm.elements[key];
    if (field && !(field instanceof RadioNodeList)) field.value = String(value ?? '');
  });
  aiCameraIds = Array.isArray(settings?.camera_ids) ? settings.camera_ids.map(String) : [];
  aiForm.querySelectorAll('input[name="camera_id_choice"]').forEach((box) => {
    box.checked = aiCameraIds.includes(box.value);
  });
}

function aiPayload() {
  const data = {};
  Object.keys(AI_FORM_DEFAULTS).forEach((key) => {
    const field = aiForm.elements[key];
    if (!field) return;
    const raw = String(field.value ?? '');
    if (AI_BOOLEAN_FIELDS.has(key)) data[key] = raw === 'true';
    else if (raw === '') return;
    else if (AI_INTEGER_FIELDS.has(key)) data[key] = Number.parseInt(raw, 10);
    else data[key] = raw;
  });
  data.camera_ids = [...aiForm.querySelectorAll('input[name="camera_id_choice"]:checked')].map((box) => box.value);
  return data;
}

function renderAiCameras(cameras) {
  const container = document.getElementById('aiCameras');
  if (!container) return;
  if (!cameras.length) {
    container.innerHTML = '<span class="muted">No cameras configured.</span>';
    return;
  }
  const selected = new Set(aiCameraIds);
  container.innerHTML = cameras.map((camera) => {
    const id = String(camera.id || '');
    const name = camera.name || id;
    return `<label><input type="checkbox" name="camera_id_choice" value="${escapeHtml(id)}"${selected.has(id) ? ' checked' : ''} /> ${escapeHtml(name)}</label>`;
  }).join('');
}

function describeAiTest(result) {
  const verification = result?.verification;
  const seconds = typeof result?.latency_ms === 'number' ? ` in ${(result.latency_ms / 1000).toFixed(1)}s` : '';
  const listed = result?.model_listed === false ? ' Warning: the server did not list this model; check the name.' : '';
  if (!verification) return `The model answered${seconds}. ${result?.message || ''}${listed}`.trim();
  const answers = Object.entries(verification.labels || {}).map(([label, value]) => {
    if (value.error) return `${label}: error (${value.error})`;
    return `${label}: ${value.present ? 'confirmed' : 'NOT present'}${value.reason ? ` - ${value.reason}` : ''}`;
  });
  const summary = String(answers.join('; ') || verification.reason || verification.status).replace(/[.\s]+$/, '');
  return `Event #${result.event_id}${seconds}: ${summary}.${listed}`;
}

// "Describe Past Events": starts the server-side backfill, then polls its
// progress until it finishes (the server paces it behind live alerts).
function renderAiBackfill(status) {
  const el = document.getElementById('aiBackfillStatus');
  if (!el || !status) return;
  if (status.running) {
    el.textContent = `Describing… ${status.done + status.failed} of ${status.total} done.`;
  } else if (status.finished_at && status.total) {
    el.textContent = `Finished: ${status.done} described${status.failed ? `, ${status.failed} failed` : ''}.`;
  } else if (status.finished_at) {
    el.textContent = 'Nothing to describe in that period.';
  }
  if (aiBackfillBtn) aiBackfillBtn.disabled = Boolean(status.running);
  if (status.running && !aiBackfillTimer) {
    aiBackfillTimer = setInterval(async () => {
      try {
        const next = await api('/api/settings/ai-verification/describe-backfill');
        if (!next.running) { clearInterval(aiBackfillTimer); aiBackfillTimer = null; }
        renderAiBackfill(next);
      } catch (_error) {
        clearInterval(aiBackfillTimer); aiBackfillTimer = null;
      }
    }, 3000);
  }
}

async function loadAiPage() {
  await window.daygleAuthReady;
  const [settings, cameraPayload] = await Promise.all([
    api('/api/settings/ai-verification'),
    api('/api/cameras').catch(() => ({ cameras: [] })),
  ]);
  aiCameraIds = Array.isArray(settings?.camera_ids) ? settings.camera_ids.map(String) : [];
  renderAiCameras(cameraPayload?.cameras || []);
  fillAiForm(settings);
  api('/api/settings/ai-verification/describe-backfill').then(renderAiBackfill).catch(() => {});
}

aiForm?.addEventListener('submit', aiGuard(async (event) => {
  event.preventDefault();
  fillAiForm(await api('/api/settings/ai-verification', {
    method: 'PUT',
    body: JSON.stringify(aiPayload()),
  }));
  aiMessage('AI settings saved.');
}));

aiTestBtn?.addEventListener('click', aiGuard(async () => {
  const resultEl = document.getElementById('aiTestResult');
  aiTestBtn.disabled = true;
  if (resultEl) resultEl.textContent = 'Asking the model about the latest object event…';
  try {
    const result = await api('/api/settings/ai-verification/test', {
      method: 'POST',
      body: JSON.stringify({ settings: aiPayload() }),
    });
    if (resultEl) resultEl.textContent = describeAiTest(result);
    aiMessage('AI test finished.');
  } catch (error) {
    if (resultEl) resultEl.textContent = error.message;
    throw error;
  } finally {
    aiTestBtn.disabled = false;
  }
}));

aiBackfillBtn?.addEventListener('click', aiGuard(async () => {
  const hours = Number.parseInt(document.getElementById('aiBackfillHours')?.value || '24', 10);
  aiBackfillBtn.disabled = true;
  try {
    renderAiBackfill(await api('/api/settings/ai-verification/describe-backfill', {
      method: 'POST',
      body: JSON.stringify({ hours }),
    }));
  } finally {
    if (!aiBackfillTimer) aiBackfillBtn.disabled = false;
  }
}));

loadAiPage().catch((error) => {
  if (window.daygleAuth?.redirecting) return;
  aiMessage(`Could not load AI settings: ${error.message}`, true);
});
