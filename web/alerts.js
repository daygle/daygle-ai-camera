requireElements(['cameraSelect', 'zoneSelect', 'alertTypeSelect', 'addAlertBtn', 'saveAlertsBtn', 'alertsList']);

let cameras = [];
let soundClasses = [];
let cameraIndex = 0;
let zoneIndex = 0;
let alertType = 'object';

const $ = (id) => document.getElementById(id);
const currentCamera = () => cameras[cameraIndex];
const currentZone = () => currentCamera()?.detection?.zones?.[zoneIndex];
const objectLabels = () => [...new Set(cameras.flatMap((camera) => (camera.detection?.zones || []).flatMap((zone) => (zone.object_rules || []).map((rule) => rule.label)).filter((label) => label && !['motion', 'face'].includes(label))))].sort();
const currentRules = () => alertType === 'sound' ? (currentCamera()?.detection?.sound?.rules || []) : (currentZone()?.object_rules || []).filter((rule) => !['motion', 'face'].includes(rule.label));

function defaultRule(label = 'person', type = alertType) {
  if (type === 'sound') {
    const sound = soundClasses.find((item) => item.id === label);
    return { class: label, name: sound?.label || label, enabled: true, record_on_detect: true, confidence_threshold: sound?.default_threshold ?? 0.35, cooldown_seconds: sound?.default_cooldown ?? 30, email_enabled: false, email_recipients: [], push_enabled: false, active_start: null, active_end: null, notify_start: null, notify_end: null };
  }
  return { id: `${label}-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`, label, enabled: true, record_on_detect: true, min_confidence: label === 'motion' || label === 'face' ? 0.45 : 0.5, max_confidence: 1, cooldown_seconds: 60, email_enabled: false, email_recipients: [], push_enabled: false, active_start: null, active_end: null, notify_start: null, notify_end: null };
}

function timeSelect(value, attr) {
  const options = ['<option value="">Any time</option>'];
  for (let hour = 0; hour < 24; hour += 1) for (let minute = 0; minute < 60; minute += 15) {
    const time = `${String(hour).padStart(2, '0')}:${String(minute).padStart(2, '0')}`;
    options.push(`<option value="${time}" ${time === value ? 'selected' : ''}>${time}</option>`);
  }
  return `<select ${attr}>${options.join('')}</select>`;
}

function allPolicies() {
  const objectPolicies = cameras.flatMap((camera) => (camera.detection?.zones || []).flatMap((zone) => zone.object_rules || []));
  const soundPolicies = cameras.flatMap((camera) => camera.detection?.sound?.rules || []);
  return [...objectPolicies, ...soundPolicies].filter((rule) => rule.email_enabled || rule.push_enabled);
}

function updateStats() {
  const policies = allPolicies();
  $('alertCount').textContent = String(policies.length);
  $('enabledCount').textContent = String(policies.filter((rule) => rule.enabled !== false).length);
  $('channelCount').textContent = String(policies.filter((rule) => rule.email_enabled).length + policies.filter((rule) => rule.push_enabled).length);
}

function renderSelectors() {
  $('cameraSelect').innerHTML = cameras.map((camera, index) => `<option value="${index}">${escapeHtml(camera.name || camera.id)}</option>`).join('') || '<option>No cameras</option>';
  $('cameraSelect').value = String(cameraIndex);
  const zones = currentCamera()?.detection?.zones || [];
  zoneIndex = Math.min(zoneIndex, Math.max(0, zones.length - 1));
  $('zoneSelect').innerHTML = zones.map((zone, index) => `<option value="${index}">${escapeHtml(zone.name || `Zone ${index + 1}`)}</option>`).join('') || '<option>No zones</option>';
  $('zoneSelect').value = String(zoneIndex);
  $('zoneSelect').disabled = alertType === 'sound' || !zones.length;
}

function ruleLabel(rule) {
  if (alertType === 'sound') return rule.name || soundClasses.find((item) => item.id === rule.class)?.label || rule.class;
  return String(rule.label || '').replace(/\b\w/g, (char) => char.toUpperCase());
}

function ruleOptions(rule) {
  if (alertType === 'sound') return soundClasses.map((sound) => `<option value="${escapeHtml(sound.id)}" ${sound.id === rule.class ? 'selected' : ''}>${escapeHtml(sound.label)}</option>`).join('');
  return objectLabels().map((label) => `<option value="${escapeHtml(label)}" ${label === rule.label ? 'selected' : ''}>${escapeHtml(label)}</option>`).join('');
}

function renderPolicies() {
  const rules = currentRules();
  updateStats();
  const scope = alertType === 'sound' ? (currentCamera() ? `Camera sound · ${currentCamera().name || currentCamera().id}` : 'Camera sound') : (currentZone() ? `Zone · ${currentZone().name || `Zone ${zoneIndex + 1}`}` : 'Zone');
  if (!currentCamera() || (alertType === 'object' && !currentZone())) { $('alertsList').innerHTML = '<div class="empty">Configure a camera and zone on the Zones page first.</div>'; return; }
  if (!rules.length) { $('alertsList').innerHTML = `<div class="empty">No ${alertType} alert policies in this scope. Add one below to get started.</div>`; return; }
  $('alertsList').innerHTML = rules.map((rule, index) => `
    <article class="alerts-policy ${rule.enabled !== false ? 'is-enabled' : ''}" data-rule-index="${index}">
      <div class="alerts-policy-head"><div><span class="zones-panel-kicker">${escapeHtml(scope)} · Policy ${index + 1}</span><h3>${escapeHtml(ruleLabel(rule))}</h3></div><label class="toggle-control"><input data-field="enabled" type="checkbox" ${rule.enabled !== false ? 'checked' : ''}><span>${rule.enabled !== false ? 'Enabled' : 'Disabled'}</span></label></div>
      <div class="alerts-policy-grid">
        <label><span>${alertType === 'sound' ? 'Sound' : 'Object'}</span><select data-field="${alertType === 'sound' ? 'class' : 'label'}">${ruleOptions(rule)}</select></label>
        <label><span>${alertType === 'sound' ? 'Confidence threshold' : 'Minimum confidence'}</span><input data-field="${alertType === 'sound' ? 'confidence_threshold' : 'min_confidence'}" type="number" min="0" max="1" step="0.01" value="${rule[alertType === 'sound' ? 'confidence_threshold' : 'min_confidence'] ?? 0.5}"></label>
        ${alertType === 'object' ? '<label><span>Maximum confidence</span><input data-field="max_confidence" type="number" min="0" max="1" step="0.01" value="' + (rule.max_confidence ?? 1) + '"></label>' : ''}
        <label><span>Cooldown (seconds)</span><input data-field="cooldown_seconds" type="number" min="0" max="3600" step="5" value="${rule.cooldown_seconds ?? 60}"></label>
      </div>
      <div class="alerts-channel-row"><label><input data-field="email_enabled" type="checkbox" ${rule.email_enabled ? 'checked' : ''}> Email</label><label><input data-field="push_enabled" type="checkbox" ${rule.push_enabled ? 'checked' : ''}> Push</label><label><input data-field="record_on_detect" type="checkbox" ${rule.record_on_detect !== false ? 'checked' : ''}> Record event</label></div>
      <div class="alerts-policy-grid alerts-schedule-grid">
        <label><span>Detect from</span>${timeSelect(rule.active_start, 'data-field="active_start"')}</label><label><span>Detect until</span>${timeSelect(rule.active_end, 'data-field="active_end"')}</label>
        <label><span>Notify from</span>${timeSelect(rule.notify_start, 'data-field="notify_start"')}</label><label><span>Notify until</span>${timeSelect(rule.notify_end, 'data-field="notify_end"')}</label>
      </div>
      <label class="alerts-recipient-field"><span>Email recipients</span><input data-field="email_recipients" type="text" value="${escapeHtml((rule.email_recipients || []).join(', '))}" placeholder="alerts@example.com, me@example.com"></label>
      <div class="alerts-policy-actions"><span class="muted">${alertType === 'sound' ? 'Sound rules apply to this camera and are still detected on the Sounds page.' : 'Same object, same zone? Add another policy with different timing or thresholds.'}</span><button class="btn-danger" data-delete-rule type="button">Remove policy</button></div>
    </article>`).join('');
  $('alertsList').querySelectorAll('.alerts-policy').forEach((card) => {
    const rule = rules[Number(card.dataset.ruleIndex)];
    card.querySelectorAll('[data-field]').forEach((field) => field.addEventListener('change', () => {
      const key = field.dataset.field;
      if (field.type === 'checkbox') rule[key] = field.checked;
      else if (key === 'email_recipients') rule[key] = field.value.split(',').map((item) => item.trim()).filter(Boolean);
      else if (['min_confidence', 'max_confidence', 'confidence_threshold', 'cooldown_seconds'].includes(key)) rule[key] = Number(field.value);
      else rule[key] = field.value || null;
      if (key === 'class') rule.name = soundClasses.find((sound) => sound.id === field.value)?.label || field.value;
      card.classList.toggle('is-enabled', rule.enabled !== false);
      updateStats();
    }));
    card.querySelector('[data-delete-rule]').addEventListener('click', () => {
      const allRules = alertType === 'sound' ? currentCamera().detection.sound.rules : currentZone().object_rules;
      const actualIndex = allRules.indexOf(rule);
      if (actualIndex >= 0) allRules.splice(actualIndex, 1);
      renderPolicies();
    });
  });
}

$('cameraSelect').addEventListener('change', () => { cameraIndex = Number($('cameraSelect').value); zoneIndex = 0; renderSelectors(); renderPolicies(); });
$('zoneSelect').addEventListener('change', () => { zoneIndex = Number($('zoneSelect').value); renderPolicies(); });
$('alertTypeSelect').addEventListener('change', () => { alertType = $('alertTypeSelect').value; renderSelectors(); renderPolicies(); });
$('addAlertBtn').addEventListener('click', () => {
  if (!currentCamera() || (alertType === 'object' && !currentZone())) return;
  if (alertType === 'sound') {
    const available = soundClasses.find((sound) => !(currentCamera().detection?.sound?.rules || []).some((rule) => rule.class === sound.id));
    if (!available) return;
    currentCamera().detection.sound ||= { enabled: false, rules: [] };
    currentCamera().detection.sound.rules.push(defaultRule(available.id, 'sound'));
  } else {
    currentZone().object_rules ||= [];
    currentZone().object_rules.push(defaultRule(objectLabels()[0] || 'person', 'object'));
  }
  renderPolicies();
});
$('saveAlertsBtn').addEventListener('click', async () => {
  const button = $('saveAlertsBtn'); button.disabled = true;
  try {
    cameras.forEach((camera) => {
      (camera.detection?.zones || []).forEach((zone) => {
        zone.object_rules = (zone.object_rules || []).map((rule, index) => ({ ...defaultRule(rule.label, 'object'), ...rule, id: rule.id || `${rule.label}-${index + 1}` }));
        zone.object_labels = zone.object_rules.filter((rule) => !['motion', 'face'].includes(rule.label)).map((rule) => rule.label);
      });
      if (camera.detection?.sound) camera.detection.sound.rules = (camera.detection.sound.rules || []).map((rule) => ({ ...defaultRule(rule.class, 'sound'), ...rule }));
    });
    const result = await api('/api/cameras', { method: 'PUT', body: JSON.stringify({ cameras }) });
    cameras = result.cameras || cameras; renderSelectors(); renderPolicies(); window.showToast?.('Alert policies saved.');
  } catch (error) { if (!window.daygleAuth?.redirecting) window.showToast?.(error.message, true); }
  finally { button.disabled = false; }
});

(async function init() {
  try {
    const [cameraPayload, soundPayload] = await Promise.all([api('/api/cameras'), api('/api/sound/classes')]);
    cameras = cameraPayload.cameras || [];
    soundClasses = soundPayload.classes || [];
    cameras.forEach((camera) => (camera.detection?.zones || []).forEach((zone) => { zone.object_rules ||= []; }));
    renderSelectors(); renderPolicies();
  } catch (error) { $('alertsList').innerHTML = `<div class="empty">${escapeHtml(error.message)}</div>`; }
}());
