let csrfToken = null;
const emailForm = document.getElementById('emailSettingsForm');
const ruleForm = document.getElementById('alertRuleForm');
const messageEl = document.getElementById('settingsMessage');
const rulesEl = document.getElementById('alertRules');
const objectSelect = document.getElementById('objectSelect');
const objectOptionsHelp = document.getElementById('objectOptionsHelp');
const testEmailRecipient = document.getElementById('testEmailRecipient');
const testEmailBtn = document.getElementById('testEmailBtn');
const newAlertRuleBtn = document.getElementById('newAlertRuleBtn');
const cancelEditRuleBtn = document.getElementById('cancelEditRule');
const ruleSubmitBtn = ruleForm.querySelector('button[type="submit"]');

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

function resetRuleForm() {
  ruleForm.reset();
  ruleForm.elements.id.value = '';
  if (ruleForm.elements.min_confidence) ruleForm.elements.min_confidence.value = '0.6';
  if (ruleForm.elements.cooldown_seconds) ruleForm.elements.cooldown_seconds.value = '60';
  if (ruleForm.elements.enabled) ruleForm.elements.enabled.value = 'true';
  if (ruleForm.elements.email_enabled) ruleForm.elements.email_enabled.value = 'false';
  if (ruleSubmitBtn) ruleSubmitBtn.textContent = 'Add alert rule';
}

function setEditingRule(rule) {
  ensureObjectOption(rule.object);
  for (const [key, value] of Object.entries(rule)) {
    if (ruleForm.elements[key]) ruleForm.elements[key].value = Array.isArray(value) ? value.join(', ') : String(value ?? '');
  }
  if (ruleSubmitBtn) ruleSubmitBtn.textContent = 'Update alert rule';
}

function labelOption(label) {
  return `<option value="${escapeHtml(label)}">${escapeHtml(label)}</option>`;
}

function triggerLabel(value) {
  return String(value || '').toLowerCase() === 'motion' ? 'Motion' : String(value || '');
}

function ensureObjectOption(label) {
  if (!label) return;
  objectSelect.disabled = false;
  if (Array.from(objectSelect.options).some((option) => option.value === label)) return;
  objectSelect.insertAdjacentHTML('beforeend', labelOption(label));
  objectOptionsHelp.textContent = `Editing an existing rule for ${label}. This label is not in the current detector label list.`;
}

function renderObjectOptions(labels) {
  const cleanLabels = [...new Set((labels || []).map((label) => String(label).trim()).filter(Boolean))];
  objectSelect.disabled = false;
  objectSelect.innerHTML = '<option value="">Select a trigger...</option><option value="motion">Motion</option>' + cleanLabels.map(labelOption).join('');
  objectOptionsHelp.textContent = cleanLabels.length
    ? `${cleanLabels.length} object labels available. Choose Motion for any matching live-frame movement, or choose an object such as person, car, cat, or dog.`
    : 'Choose Motion, or check the AI labels path in AI settings to add object label choices.';
}

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
  if (testEmailRecipient && !testEmailRecipient.value) testEmailRecipient.value = settings.from_address || '';
  setMessage(settings.enabled ? 'Email alerts are enabled.' : 'Email alerts are disabled.');
}

function renderRules(rules) {
  rulesEl.innerHTML = rules.length ? rules.map((rule) => `
    <div class="item">
      <div class="item-title"><span>${escapeHtml(rule.name)}</span><span>${rule.enabled ? 'Enabled' : 'Disabled'}</span></div>
      <p>${escapeHtml(triggerLabel(rule.object))} - ${Math.round(rule.min_confidence * 100)}% - cooldown ${rule.cooldown_seconds}s</p>
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
  renderObjectOptions(alerts.available_labels);
  renderRules(alerts.rules);
  if (!ruleForm.elements.id.value) resetRuleForm();
}

emailForm.addEventListener('submit', async (event) => {
  event.preventDefault();
  try {
    renderEmail(await api('/api/settings/alert-email', { method: 'PUT', body: JSON.stringify(formPayload(emailForm)) }));
    setMessage('Mail server settings saved.');
  } catch (error) { setMessage(error.message); }
});

testEmailBtn.addEventListener('click', async () => {
  const recipient = testEmailRecipient.value.trim() || emailForm.elements.from_address.value.trim();
  if (!recipient) {
    setMessage('Enter a test recipient email address.');
    return;
  }
  testEmailBtn.disabled = true;
  setMessage('Sending test email...');
  try {
    await api('/api/settings/alert-email/test', {
      method: 'POST',
      body: JSON.stringify({ settings: formPayload(emailForm), recipient }),
    });
    setMessage(`Test email sent to ${recipient}.`);
  } catch (error) {
    setMessage(error.message);
  } finally {
    testEmailBtn.disabled = false;
  }
});

ruleForm.addEventListener('submit', async (event) => {
  event.preventDefault();
  const payload = formPayload(ruleForm);
  const id = payload.id;
  delete payload.id;
  try {
    await api(id ? `/api/settings/alerts/${id}` : '/api/settings/alerts', { method: id ? 'PUT' : 'POST', body: JSON.stringify(payload) });
    resetRuleForm();
    setMessage(id ? 'Alert rule updated.' : 'Alert rule added. Add another rule when ready.');
    await loadAll();
  } catch (error) { setMessage(error.message); }
});

newAlertRuleBtn.addEventListener('click', () => {
  resetRuleForm();
  setMessage('Ready to add a new alert rule.');
});

cancelEditRuleBtn.addEventListener('click', () => {
  resetRuleForm();
  setMessage('Edit cancelled. Ready to add a new alert rule.');
});

rulesEl.addEventListener('click', async (event) => {
  const button = event.target.closest('button');
  if (!button) return;
  if (button.dataset.action === 'edit') {
    const rule = JSON.parse(button.dataset.rule);
    setEditingRule(rule);
    setMessage(`Editing ${rule.name}.`);
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
