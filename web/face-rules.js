// face-rules.js - Face Detection Rules tab on the face-recognition page.
// Admin-only. Uses api() / escapeHtml / showToast from utils.js.
//
// Rules live under /api/settings/face-detection-rules and are a simple
// list of per-person alert configurations with email/push toggles.

const frRulesList = document.getElementById('faceRulesList');
const frRulesEmpty = document.getElementById('faceRulesEmpty');
const frRulesMessage = document.getElementById('faceRulesMessage');
const addRuleForm = document.getElementById('addFaceRuleForm');
const addRuleMessage = document.getElementById('addFaceRuleMessage');
const personSelect = document.getElementById('faceRulePersonSelect');

let currentRules = [];

function setRulesMsg(text, isError = false) {
  frRulesMessage.textContent = text || '';
  if (text) window.showToast(text, isError);
}

function setAddMsg(text, isError = false) {
  addRuleMessage.textContent = text || '';
  if (text) window.showToast(text, isError);
}

function ruleCard(rule) {
  const isSystem = rule.id === '_unknown';
  const enabledClass = rule.enabled ? 'model-card-active' : 'model-card-installed';
  const statusBadge = rule.enabled
    ? '<span class="model-status model-status-active">\u25CF Enabled</span>'
    : '<span class="model-status model-status-installed">\u25CB Disabled</span>';

  const emailBadge = rule.email_enabled
    ? '<span class="model-badge" style="background:rgba(73,230,163,0.1);border:1px solid rgba(73,230,163,0.25);color:#49e6a3">📧 Email</span>'
    : '';
  const pushBadge = rule.push_enabled
    ? '<span class="model-badge" style="background:rgba(124,166,255,0.1);border:1px solid rgba(124,166,255,0.25);color:#7ca6ff">🔔 Push</span>'
    : '';
  const cooldownBadge = rule.cooldown_minutes
    ? `<span class="model-badge model-badge-res">${rule.cooldown_minutes}m cooldown</span>`
    : '';
  const confidenceBadge = rule.min_confidence != null
    ? `<span class="model-badge model-badge-res">≥${escapeHtml(String(rule.min_confidence))} confidence</span>`
    : '';

  return `
    <div class="model-card ${enabledClass}" data-rule-id="${escapeHtml(rule.id)}">
      <div class="model-card-header">
        <div class="model-card-title">
          <h3>${escapeHtml(isSystem ? 'Unknown Person' : rule.name)}</h3>
          <div class="model-card-meta">
            ${statusBadge}${emailBadge}${pushBadge}${cooldownBadge}${confidenceBadge}
          </div>
        </div>
      </div>
      ${rule.email_enabled && rule.email_recipients ? `<p class="muted" style="font-size:12px;margin-top:4px">📧 ${escapeHtml(rule.email_recipients)}</p>` : ''}
      <div style="margin-top:10px;display:flex;align-items:center;gap:8px">
        <label class="muted" for="rule-confidence-${escapeHtml(rule.id)}" style="font-size:12px;white-space:nowrap">Min Confidence</label>
        <input id="rule-confidence-${escapeHtml(rule.id)}" type="number" data-rule-confidence="${escapeHtml(rule.id)}" min="0" max="1" step="0.01" value="${rule.min_confidence != null ? escapeHtml(String(rule.min_confidence)) : ''}" placeholder="Any" title="Minimum detection confidence (0-1) required for this rule's alerts. Leave blank to alert on any detected face." style="width:90px;padding:4px 6px;border:1px solid #d8dee6;border-radius:6px;background:#fff;color:#333;font-size:13px" />
      </div>
      <div class="model-card-actions" style="margin-top:10px">
        <button class="btn-info model-action-btn" data-action="toggle" data-rule-id="${escapeHtml(rule.id)}" type="button">${rule.enabled ? '\u25CB Disable' : '\u25CF Enable'}</button>
        <button class="btn-danger model-action-btn" data-action="delete" data-rule-id="${escapeHtml(rule.id)}" type="button">\u2715 Delete</button>
      </div>
    </div>`;
}

function renderRules(rules) {
  if (!rules.length) {
    frRulesList.innerHTML = '';
    frRulesEmpty.hidden = false;
    return;
  }
  frRulesEmpty.hidden = true;
  frRulesList.innerHTML = rules.map(ruleCard).join('');
  // Bind action buttons
  frRulesList.querySelectorAll('.model-action-btn').forEach(btn => {
    btn.addEventListener('click', async () => {
      const action = btn.dataset.action;
      const ruleId = btn.dataset.ruleId;
      if (action === 'toggle') {
        const rule = rules.find(r => r.id === ruleId);
        if (rule) {
          rule.enabled = !rule.enabled;
          await saveRules();
        }
      } else if (action === 'delete') {
        if (!confirm('Delete this face rule?')) return;
        const idx = rules.findIndex(r => r.id === ruleId);
        if (idx !== -1) {
          rules.splice(idx, 1);
          await saveRules();
        }
      }
    });
  });
  // Inline per-rule confidence editing: blank = no gate (any detection).
  frRulesList.querySelectorAll('[data-rule-confidence]').forEach(input => {
    input.addEventListener('change', () => {
      const rule = rules.find(r => r.id === input.dataset.ruleConfidence);
      if (!rule) return;
      const raw = input.value.trim();
      if (raw === '') {
        rule.min_confidence = null;
      } else {
        const n = Number(raw);
        rule.min_confidence = Number.isFinite(n) ? Math.min(1, Math.max(0, n)) : null;
      }
      input.value = rule.min_confidence != null ? String(rule.min_confidence) : '';
      saveRules();
    });
  });
}

async function loadRules() {
  try {
    currentRules = await api('/api/settings/face-detection-rules');
    renderRules(currentRules.rules || []);
  } catch (err) {
    setRulesMsg(err.message || 'Failed to load face rules.', true);
  }
}

async function saveRules() {
  try {
    const result = await api('/api/settings/face-detection-rules', {
      method: 'PUT',
      body: JSON.stringify({ rules: currentRules.rules || [] }),
    });
    currentRules = result;
    renderRules(currentRules.rules || []);
    setRulesMsg('Face rules saved.');
  } catch (err) {
    setRulesMsg(err.message || 'Failed to save face rules.', true);
  }
}

async function loadPeople() {
  try {
    const body = await api('/api/persons');
    const people = body.persons || [];
    const options = ['<option value=\"\">Select a person…</option>'];
    for (const p of people) {
      options.push(`<option value=\"${escapeHtml(String(p.id))}\" data-name=\"${escapeHtml(p.name)}\">${escapeHtml(p.name)}</option>`);
    }
    // Always include the Unknown Person system rule option
    options.push('<option value=\"_unknown\" data-name=\"Unknown Person\">Unknown Person</option>');
    personSelect.innerHTML = options.join('');
  } catch {
    // Non-fatal — the form still works for the system rule.
  }
}

addRuleForm.addEventListener('submit', async (event) => {
  event.preventDefault();
  const option = personSelect.options[personSelect.selectedIndex];
  const personId = personSelect.value;
  const personName = option?.dataset?.name || '';
  if (!personId) return;

  // Check for duplicate rules
  const existing = (currentRules.rules || []).find(r => r.person_id === personId || r.id === personId);
  if (existing) {
    setAddMsg(`${personName} already has a rule. Edit it above.`, true);
    return;
  }

  const rawConf = addRuleForm.min_confidence?.value.trim();
  let minConfidence = null;
  if (rawConf !== '') {
    const n = Number(rawConf);
    if (Number.isFinite(n)) minConfidence = Math.min(1, Math.max(0, n));
  }
  const rule = {
    id: personId === '_unknown' ? '_unknown' : `person_${personId}`,
    person_id: personId === '_unknown' ? null : personId,
    name: personName,
    enabled: true,
    email_enabled: addRuleForm.email_enabled.value === 'true',
    push_enabled: addRuleForm.push_enabled.value === 'true',
    email_recipients: addRuleForm.email_recipients.value.trim(),
    cooldown_minutes: parseInt(addRuleForm.cooldown_minutes.value || '5', 10),
    min_confidence: minConfidence,
  };

  currentRules.rules = [...(currentRules.rules || []), rule];
  addRuleForm.reset();
  addRuleForm.cooldown_minutes.value = '5';
  await saveRules();
  setAddMsg('');
});

async function initFaceRules() {
  await Promise.all([loadRules(), loadPeople()]);
}

initFaceRules();
