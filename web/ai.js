// ai.js - Intelligence > AI page (ai.html). Admin-only.
// The local vision model (app/ai_verification.py): server + model, event
// descriptions / search, and the past-events backfill. Alert verification and
// AI tag alerts are configured per rule on the Alerts page.
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
  describe_alerts: 'off',
  describe_other_events: 'off',
};
// Alert verification's focus_crop is no longer on this form; the PUT merges
// onto the stored settings, so its saved value (default on) is kept.
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
    if (raw === '' && key !== 'api_key') return;
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
    const verdict = value.unsure ? 'unsure (alert would be sent)' : value.present ? 'confirmed' : 'NOT present';
    return `${label}: ${verdict}${value.reason ? ` - ${value.reason}` : ''}`;
  });
  const summary = String(answers.join('; ') || verification.reason || verification.status).replace(/[.\s]+$/, '');
  return `Event #${result.event_id}${seconds}: ${summary}.${listed}`;
}

// The overview cards: last 7 days of AI alert verification and whether the
// model server answered lately. Unverified alerts were sent without a
// verdict (the model was unsure, failed or skipped); a rising count there,
// or a red Model card, means the filter is not doing its job.
function aiTimeAgo(iso) {
  const when = Date.parse(iso || '');
  if (!Number.isFinite(when)) return '';
  const seconds = Math.max(0, Math.round((Date.now() - when) / 1000));
  if (seconds < 60) return 'just now';
  if (seconds < 3600) return `${Math.round(seconds / 60)} min ago`;
  if (seconds < 86400) return `${Math.round(seconds / 3600)} h ago`;
  return `${Math.round(seconds / 86400)} d ago`;
}

function aiModelHealthText(health) {
  const state = health?.state || 'unknown';
  if (state === 'ok') return { value: 'Online', className: 'is-ok', sub: `Last answer ${aiTimeAgo(health.last_success_at)}` };
  if (state === 'error') {
    const when = aiTimeAgo(health.last_error_at);
    return { value: 'Failing', className: 'is-error', sub: `${health.last_error || 'Last request failed'}${when ? ` (${when})` : ''}` };
  }
  return { value: 'No requests yet', className: 'is-unknown', sub: 'Since the app started. Use Test on Latest Event below.' };
}

function renderAiStats(stats) {
  if (!stats) return;
  const set = (id, value, sub, className) => {
    const el = document.getElementById(id);
    if (el) {
      el.textContent = value;
      el.classList.remove('is-ok', 'is-error', 'is-unknown');
      if (className) el.classList.add(className);
    }
    const subEl = document.getElementById(`${id}Sub`);
    if (subEl && sub) subEl.textContent = sub;
  };
  const days = stats.days || 7;
  set('aiStatVerified', String(stats.confirmed ?? 0), `Alerts the model confirmed, last ${days} days`);
  const stopped = Number(stats.stopped || 0);
  set('aiStatFiltered', String(stats.filtered ?? 0), `${stopped} notification${stopped === 1 ? '' : 's'} stopped, last ${days} days`);
  const unsure = Number(stats.unsure || 0);
  set('aiStatUnverified', String(stats.unverified ?? 0),
    `Sent without a verdict${unsure ? ` (${unsure} unsure)` : ''}, last ${days} days`);
  const health = aiModelHealthText(stats.health);
  const latency = typeof stats.avg_latency_ms === 'number' ? ` · avg ${(stats.avg_latency_ms / 1000).toFixed(1)}s per check` : '';
  set('aiStatModel', health.value, `${health.sub}${latency}`, health.className);
}

function loadAiStats() {
  return api('/api/settings/ai-verification/stats?days=7').then(renderAiStats).catch(() => {});
}

function renderAiTestPreview(image) {
  const figure = document.getElementById('aiTestPreview');
  const img = document.getElementById('aiTestImage');
  if (!figure || !img) return;
  const valid = typeof image === 'string' && image.startsWith('data:image/jpeg;base64,');
  if (valid) img.src = image;
  else img.removeAttribute('src');
  figure.hidden = !valid;
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
    // One poll at a time: a setInterval fired a new request every 3s even
    // while the previous one was still waiting on a busy server, so slow
    // responses stacked up and could render out of order. Each poll now
    // schedules the next only after it has finished.
    aiBackfillTimer = setTimeout(async () => {
      let next = null;
      try {
        next = await api('/api/settings/ai-verification/describe-backfill');
      } catch (_error) {
        // Stop polling; the status line keeps its last known progress.
      }
      aiBackfillTimer = null;
      if (next) renderAiBackfill(next);
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
  loadAiStats();
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
  renderAiTestPreview(null);
  try {
    const result = await api('/api/settings/ai-verification/test', {
      method: 'POST',
      body: JSON.stringify({ settings: aiPayload() }),
    });
    if (resultEl) resultEl.textContent = describeAiTest(result);
    renderAiTestPreview(result?.image);
    aiMessage('AI test finished.');
  } catch (error) {
    if (resultEl) resultEl.textContent = error.message;
    throw error;
  } finally {
    aiTestBtn.disabled = false;
    loadAiStats();
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
