let csrfToken = null;
const emailForm = document.getElementById('emailSettingsForm');
const ruleForm = document.getElementById('alertRuleForm');
const messageEl = document.getElementById('settingsMessage');
const rulesEl = document.getElementById('alertRules');
const labelOptions = document.getElementById('labelOptions');

async function api(path, options = {}) {
  const headers = { ...(options.headers || {}) };
  if (csrfToken && ['POST', 'PUT', 'PATCH', 'DELETE'].includes((options.method || 'GET').toUpperCase())) {
    headers['X-CSRF-Token'] = csrfToken;
  }
  if (options.body && !headers['Content-Type']) headers['Content-Type'] = 'application/json';
  const response = await fetch(path, { ...options, headers });
  if (response.status === 401) window.location.href = '/login';
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(payload.detail || `Request failed: ${response.status}`);
  return payload;
}

function escapeHtml(value) {
  return String(value ?? '').replace(/[&<>'"]/g, (char) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', "'": '&#39;', '"': '&quot;' }[char]));
}

function setMessage(text) { messageEl.textContent = text; }

function formPayload(form) {
  const data = Object.fromEntries(new FormData(form).entries());
  for (const key of ['enabled', 'email_enabled', 'use_tls', 'use_ssl']) if (key in data) data[key] = data[key] === 'true';
  for (const key of ['min_confidence']) if (key in data && data[key] !== '') data[key] = Number(data[key]);
  for (const key of ['cooldown_seconds', 'port']) if (key in data && data[key] !== '') data[key] = Number.parseInt(data[key], 10);
  for (const key of ['active_start', 'active_end']) if (data[key] === '') data[key] = null;
  if ('email_recipients' in data) {
    data.email_recipients = data.email_recipients.split(',').map((value) => value.trim()).filter(Boolean);
  }
  return data;
}

function renderEmail(settings) {
  for (const [key, value] of Object.entries(settings)) {
    if (emailForm.elements[key]) emailForm.elements[key].value = String(value ?? '');
  }
  if (!emailForm.elements.port.value) emailForm.elements.port.value = '587';
  setMessage(settings.enabled ? 'Email alerts are enabled.' : 'Email alerts are disabled.');
}

function renderRules(rules) {
  rulesEl.innerHTML = rules.length ? rules.map((rule) => `
    <div class="item">
      <div class="item-title"><span>${escapeHtml(rule.name)}</span><span>${rule.enabled ? 'Enabled' : 'Disabled'}</span></div>
      <p>${escapeHtml(rule.object)} - ${Math.round(rule.min_confidence * 100)}% - cooldown ${rule.cooldown_seconds}s</p>
      <p class="muted">Window: ${rule.active_start || 'any'} - ${rule.active_end || 'any'}</p>
      <p class="muted">Email: ${rule.email_enabled ? escapeHtml((rule.email_recipients || []).join(', ') || 'no recipients') : 'disabled'}</p>
      <button class="secondary" data-action="edit" data-rule='${JSON.stringify(rule).replace(/'/g, '&#39;')}'>Edit</button>
      <button class="secondary" data-action="toggle" data-id="${rule.id}" data-enabled="${rule.enabled}">${rule.enabled ? 'Disable' : 'Enable'}</button>
      <button class="secondary" data-action="delete" data-id="${rule.id}">Delete</button>
    </div>
  `).join('') : '<div class="empty">No alert rules configured.</div>';
}

async function loadAll() {
  const me = await api('/api/auth/me');
  csrfToken = me.csrf_token;
  renderEmail(await api('/api/settings/alert-email'));
  const alerts = await api('/api/settings/alerts');
  labelOptions.innerHTML = alerts.available_labels.map((label) => `<option value="${escapeHtml(label)}"></option>`).join('');
  renderRules(alerts.rules);
}

emailForm.addEventListener('submit', async (event) => {
  event.preventDefault();
  try {
    renderEmail(await api('/api/settings/alert-email', { method: 'PUT', body: JSON.stringify(formPayload(emailForm)) }));
    setMessage('Mail server settings saved.');
  } catch (error) { setMessage(error.message); }
});

ruleForm.addEventListener('submit', async (event) => {
  event.preventDefault();
  const payload = formPayload(ruleForm);
  const id = payload.id;
  delete payload.id;
  try {
    await api(id ? `/api/settings/alerts/${id}` : '/api/settings/alerts', { method: id ? 'PUT' : 'POST', body: JSON.stringify(payload) });
    ruleForm.reset();
    setMessage('Alert rule saved.');
    await loadAll();
  } catch (error) { setMessage(error.message); }
});

document.getElementById('cancelEditRule').addEventListener('click', () => ruleForm.reset());

rulesEl.addEventListener('click', async (event) => {
  const button = event.target.closest('button');
  if (!button) return;
  if (button.dataset.action === 'edit') {
    const rule = JSON.parse(button.dataset.rule);
    for (const [key, value] of Object.entries(rule)) {
      if (ruleForm.elements[key]) ruleForm.elements[key].value = Array.isArray(value) ? value.join(', ') : String(value ?? '');
    }
  }
  if (button.dataset.action === 'toggle') {
    await api(`/api/settings/alerts/${button.dataset.id}`, { method: 'PUT', body: JSON.stringify({ enabled: button.dataset.enabled !== 'true' }) });
    setMessage('Alert rule updated.');
    await loadAll();
  }
  if (button.dataset.action === 'delete') {
    await api(`/api/settings/alerts/${button.dataset.id}`, { method: 'DELETE' });
    setMessage('Alert rule deleted.');
    await loadAll();
  }
});

loadAll().catch((error) => setMessage(error.message));
