requireElements(['cameraSelect', 'zoneSelect', 'alertTypeSelect', 'addAlertBtn', 'saveAlertsBtn', 'alertsList']);

let cameras = [];
let soundClasses = [];
let enrolledPeople = [];
let faceRulesPayload = { rules: [] };
let cameraIndex = 0;
let zoneIndex = 0;
let alertType = 'object';

const $ = (id) => document.getElementById(id);
const currentCamera = () => cameras[cameraIndex];
const currentZone = () => currentCamera()?.detection?.zones?.[zoneIndex];
const currentPeopleRules = () => (faceRulesPayload.rules || []).filter((rule) => {
  const cameraMatches = !rule.camera_id || String(rule.camera_id) === String(currentCamera()?.id || '');
  const zoneMatches = !rule.zone_id || String(rule.zone_id) === String(currentZone()?.id || '');
  return cameraMatches && zoneMatches;
});
const currentRules = () => {
  if (alertType === 'sound') return currentCamera()?.detection?.sound?.rules || [];
  if (alertType === 'people') return currentPeopleRules();
  if (alertType === 'tripwire') {
    const wire = currentZone()?.tripwire;
    return wire ? [wire] : [];
  }
  if (alertType === 'loiter') {
    const rule = currentZone()?.loiter;
    return rule ? [rule] : [];
  }
  if (alertType === 'time') {
    const rule = currentZone()?.time_of_day;
    return rule ? [rule] : [];
  }
  return currentZone()?.object_rules || [];
};

function defaultPeopleRule(personId, personName) {
  const cameraId = String(currentCamera()?.id || '');
  const zoneId = String(currentZone()?.id || '');
  const unknown = !personId;
  return {
    id: unknown ? `_unknown:${cameraId}:${zoneId}` : `zone:${zoneId}:person:${personId}`,
    person_id: unknown ? null : personId,
    name: personName || 'Unknown Person',
    enabled: true,
    email_enabled: false,
    push_enabled: false,
    email_recipients: '',
    cooldown_minutes: 5,
    min_confidence: null,
    camera_id: cameraId,
    zone_id: zoneId,
  };
}

function defaultAlertSchedule() {
  return {
    id: `schedule-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`,
    email_enabled: false,
    push_enabled: false,
    email_recipients: [],
    active_start: null,
    active_end: null,
    notify_start: null,
    notify_end: null,
  };
}

function ensureAlertSchedules(rule) {
  const hasSchedules = Array.isArray(rule.alert_schedules) && rule.alert_schedules.length;
  const raw = hasSchedules ? rule.alert_schedules : [rule];
  const seenIds = new Set();
  rule.alert_schedules = raw.filter((schedule) => schedule && typeof schedule === 'object').map((schedule, index) => {
    const baseId = String(schedule.id || `schedule-${index + 1}`);
    let id = baseId;
    let duplicate = 2;
    while (seenIds.has(id)) id = `${baseId}-${duplicate++}`;
    seenIds.add(id);
    return {
      ...schedule,
      id,
      email_enabled: schedule.email_enabled === true,
      push_enabled: schedule.push_enabled === true,
      email_recipients: normalizeEmailList(schedule.email_recipients),
      active_start: schedule.active_start || null,
      active_end: schedule.active_end || null,
      notify_start: schedule.notify_start || null,
      notify_end: schedule.notify_end || null,
    };
  });
  if (!rule.alert_schedules.length) rule.alert_schedules = [defaultAlertSchedule()];
  // Mirror legacy singular fields only when upgrading an older stored policy;
  // once explicit schedules exist their per-entry values are authoritative.
  if (!hasSchedules) {
    const legacy = rule.alert_schedules[0];
    legacy.email_enabled = rule.email_enabled === true;
    legacy.push_enabled = rule.push_enabled === true;
    legacy.email_recipients = normalizeEmailList(rule.email_recipients);
    for (const key of ['active_start', 'active_end', 'notify_start', 'notify_end']) {
      legacy[key] = rule[key] || null;
    }
  }
  rule.email_enabled = rule.alert_schedules.some((schedule) => schedule.email_enabled);
  rule.push_enabled = rule.alert_schedules.some((schedule) => schedule.push_enabled);
  rule.email_recipients = [...new Set(rule.alert_schedules.flatMap((schedule) => schedule.email_recipients))];
  return rule.alert_schedules;
}

function policyHasChannel(rule, channel) {
  if (Array.isArray(rule.alert_schedules) && rule.alert_schedules.length) {
    return rule.alert_schedules.some((schedule) => schedule[channel]);
  }
  return Boolean(rule[channel]);
}

function defaultRule(label = 'person', type = alertType) {
  if (type === 'sound') {
    const sound = soundClasses.find((item) => item.id === label);
    return { class: label, name: sound?.label || label, enabled: true, record_on_detect: true, confidence_threshold: sound?.default_threshold ?? 0.35, cooldown_seconds: sound?.default_cooldown ?? 30, email_enabled: false, email_recipients: [], push_enabled: false, active_start: null, active_end: null, notify_start: null, notify_end: null };
  }
  return { id: `${label}-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`, label, enabled: true, record_on_detect: true, min_confidence: label === 'motion' || label === 'face' ? 0.45 : 0.5, max_confidence: 1, cooldown_seconds: 60, email_enabled: false, email_recipients: [], push_enabled: false, active_start: null, active_end: null, notify_start: null, notify_end: null, alert_schedules: [defaultAlertSchedule()] };
}

function renderObjectSchedules(rule, ruleIndex, schedules) {
  return `<div class="alerts-schedule-list">${schedules.map((schedule, scheduleIndex) => `
    <section class="alerts-schedule-entry" data-schedule-index="${scheduleIndex}">
      <header class="alerts-schedule-head"><strong>Schedule ${scheduleIndex + 1}</strong>${schedules.length > 1 ? `<button class="secondary alerts-schedule-remove" data-remove-schedule="${ruleIndex}" data-schedule-index="${scheduleIndex}" type="button" aria-label="Remove schedule ${scheduleIndex + 1}">Remove</button>` : ''}</header>
      <div class="alerts-channel-row"><label><input data-field="email_enabled" type="checkbox" ${schedule.email_enabled ? 'checked' : ''}> Email</label><label><input data-field="push_enabled" type="checkbox" ${schedule.push_enabled ? 'checked' : ''}> Push</label></div>
      <div class="alerts-policy-grid alerts-schedule-grid">
        <label><span>Detect From</span>${timeSelect(schedule.active_start, 'data-field="active_start"')}</label>
        <label><span>Detect Until</span>${timeSelect(schedule.active_end, 'data-field="active_end"')}</label>
        <label><span>Notify From</span>${timeSelect(schedule.notify_start, 'data-field="notify_start"')}</label>
        <label><span>Notify Until</span>${timeSelect(schedule.notify_end, 'data-field="notify_end"')}</label>
      </div>
      <label class="alerts-recipient-field"><span>Email Recipients</span><input data-field="email_recipients" type="text" value="${escapeHtml(schedule.email_recipients.join(', '))}" placeholder="alerts@example.com, me@example.com"></label>
    </section>`).join('')}<button class="secondary alerts-schedule-add" data-add-schedule="${ruleIndex}" type="button">＋ Add Schedule</button></div>`;
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
  const tripwirePolicies = cameras.flatMap((camera) => (camera.detection?.zones || []).map((zone) => zone.tripwire).filter(Boolean));
  const loiterPolicies = cameras.flatMap((camera) => (camera.detection?.zones || []).map((zone) => zone.loiter).filter(Boolean));
  const timePolicies = cameras.flatMap((camera) => (camera.detection?.zones || []).map((zone) => zone.time_of_day).filter(Boolean));
  return [...objectPolicies, ...soundPolicies, ...tripwirePolicies, ...loiterPolicies, ...timePolicies, ...(faceRulesPayload.rules || [])].filter((rule) => (
    Array.isArray(rule.alert_schedules) && rule.alert_schedules.length
      ? rule.alert_schedules.some((schedule) => schedule.email_enabled || schedule.push_enabled)
      : rule.email_enabled || rule.push_enabled
  ));
}

function updateStats() {
  const policies = allPolicies();
  $('alertCount').textContent = String(policies.length);
  $('enabledCount').textContent = String(policies.filter((rule) => rule.enabled !== false).length);
  $('channelCount').textContent = String(policies.filter((rule) => policyHasChannel(rule, 'email_enabled')).length + policies.filter((rule) => policyHasChannel(rule, 'push_enabled')).length);
}

function renderSelectors() {
  $('cameraSelect').innerHTML = cameras.map((camera, index) => `<option value="${index}">${escapeHtml(camera.name || camera.id)}</option>`).join('') || '<option>No cameras</option>';
  $('cameraSelect').value = String(cameraIndex);
  const zones = currentCamera()?.detection?.zones || [];
  zoneIndex = Math.min(zoneIndex, Math.max(0, zones.length - 1));
  $('zoneSelect').innerHTML = zones.map((zone, index) => `<option value="${index}">${escapeHtml(zone.name || `Zone ${index + 1}`)}</option>`).join('') || '<option>No zones</option>';
  $('zoneSelect').value = String(zoneIndex);
  $('zoneSelect').disabled = alertType === 'sound' || !zones.length;
  // Objects and sounds are assigned on the Zones/Sounds pages and appear here
  // automatically, so only recognized-person alerts are added from this page.
  $('addAlertBtn').style.display = alertType === 'people' ? '' : 'none';
  const hint = $('alertScopeHint');
  if (hint) {
    hint.textContent = alertType === 'object'
      ? 'Objects, motion, and faces are assigned - and set to record - per area on the Zones page. Here you choose how each one notifies you.'
      : alertType === 'sound'
        ? 'Sound classes are assigned - and set to record - per camera on the Sounds page. Here you choose how each one notifies you.'
        : alertType === 'tripwire'
          ? 'Line crossings are drawn - and set to record - per area on the Zones page. Here you choose how each one notifies you.'
          : alertType === 'loiter'
            ? 'Loitering is enabled - and set to record - per area on the Zones page. Here you choose how each one notifies you.'
            : alertType === 'time'
              ? 'Unusual time-of-day is enabled - and set to record - per area on the Zones page. Here you choose how each one notifies you.'
              : 'Add recognized-person and stranger alerts here. Enrol people on the Face Recognition page.';
  }
}

function ruleLabel(rule) {
  if (alertType === 'sound') return titleCase(rule.name || soundClasses.find((item) => item.id === rule.class)?.label || rule.class);
  if (alertType === 'people') return rule.name || 'Unknown Person';
  if (alertType === 'tripwire') return titleCase(rule.name || 'Tripwire');
  if (alertType === 'loiter') return titleCase(rule.name || 'Loitering');
  if (alertType === 'time') return titleCase(rule.name || 'Unusual time');
  return String(rule.label || '').replace(/\b\w/g, (char) => char.toUpperCase());
}

// Only recognized-person policies still pick their subject here; objects and
// sound classes are fixed by their assignment on the Zones and Sounds pages.
function ruleOptions(rule) {
  const people = [{ id: '', name: 'Unknown Person' }, ...(enrolledPeople || []).map((person) => ({ id: String(person.id), name: person.name }))];
  return people.map((person) => `<option value="${escapeHtml(person.id)}" ${String(rule.person_id || '') === person.id ? 'selected' : ''}>${escapeHtml(person.name)}</option>`).join('');
}

function scopeLabel() {
  if (alertType === 'sound') return currentCamera() ? `Camera Sound · ${currentCamera().name || currentCamera().id}` : 'Camera Sound';
  return currentZone() ? `Zone · ${currentZone().name || `Zone ${zoneIndex + 1}`}` : 'Zone';
}

// Sound alerts are per-camera and never need a zone; object and
// recognized-person policies are scoped to a specific zone.
function scopeRequiresZone() {
  return alertType !== 'sound';
}

function renderPolicies() {
  const rules = currentRules();
  updateStats();
  if (!currentCamera()) {
    $('alertsList').innerHTML = '<div class="empty">Add a camera first.</div>';
    return;
  }
  if (scopeRequiresZone() && !currentZone()) {
    $('alertsList').innerHTML = '<div class="empty">Draw a zone on the Zones page first, then add object or person policies here.</div>';
    return;
  }
  if (!rules.length) {
    const emptyMessage = alertType === 'object'
      ? 'No objects are assigned to this area yet. Open the <a href="/zones">Zones</a> page to add object, motion, or face detection, then configure alerts here.'
      : alertType === 'sound'
        ? 'No sound classes are assigned to this camera yet. Open the <a href="/sounds">Sounds</a> page to enable classes, then configure alerts here.'
        : alertType === 'tripwire'
          ? 'No line crossing is configured for this area yet. Open the <a href="/zones">Zones</a> page, turn on Line crossing and draw a line, then set its alerts here.'
          : alertType === 'loiter'
            ? 'No loitering rule is configured for this area yet. Open the <a href="/zones">Zones</a> page, turn on Loitering, then set its alerts here.'
            : alertType === 'time'
              ? 'No unusual time-of-day rule is configured for this area yet. Open the <a href="/zones">Zones</a> page, turn on Unusual time, then set its alerts here.'
              : 'No recognized-person alert policies yet. Add one below to get started.';
    $('alertsList').innerHTML = `<div class="empty">${emptyMessage}</div>`;
    return;
  }
  $('alertsList').innerHTML = `<div class="cameras-table-wrap alerts-policy-table-wrap"><table class="rule-table alerts-policy-table"><thead><tr><th scope="col">Policy</th><th scope="col">Scope</th><th scope="col">Enabled</th><th scope="col">Email</th><th scope="col">Push</th><th scope="col" aria-label="Actions"></th></tr></thead><tbody>${rules.map((rule, index) => {
    const people = alertType === 'people';
    const sound = alertType === 'sound';
    const tripwire = alertType === 'tripwire';
    const loiter = alertType === 'loiter';
    const time = alertType === 'time';
    // Tripwire, loiter and unusual-time are single per-zone behaviour rules: no
    // confidence axis, and detection runs whenever enabled, so they show only a
    // Notify window (no Detect-from/until) - just how each one notifies you.
    const behaviour = tripwire || loiter || time;
    const confidence = people ? rule.min_confidence : sound ? rule.confidence_threshold : rule.min_confidence;
    const cooldown = people ? rule.cooldown_minutes : rule.cooldown_seconds;
    const confidenceLabel = people ? 'Minimum Recognition Confidence' : sound ? 'Confidence Threshold' : 'Minimum Confidence';
    const cooldownLabel = people ? 'Cooldown (Minutes)' : 'Cooldown (Seconds)';
    const schedules = alertType === 'object' ? ensureAlertSchedules(rule) : [];
    const emailEnabled = alertType === 'object' ? schedules.some((schedule) => schedule.email_enabled) : rule.email_enabled;
    const pushEnabled = alertType === 'object' ? schedules.some((schedule) => schedule.push_enabled) : rule.push_enabled;
    const policyNote = people
      ? 'Recognized-person and stranger alerts use the face recognition rule store.'
      : sound
        ? 'Assigned on the Sounds page. Removing here unassigns this sound class from the camera.'
        : tripwire
          ? 'Drawn on the Zones page. Removing here deletes the line from the area.'
          : loiter
            ? 'Enabled on the Zones page. Removing here turns loitering off for the area.'
            : time
              ? 'Enabled on the Zones page. Removing here turns unusual-time off for the area.'
              : 'Assigned on the Zones page. Removing here unassigns this item from the area.';
    return `
    <tr class="alerts-policy-row ${rule.enabled !== false ? 'is-enabled' : ''}" data-rule-index="${index}">
      <td class="alerts-policy-name"><strong>${escapeHtml(ruleLabel(rule))}</strong><span>Policy ${index + 1}</span></td>
      <td>${escapeHtml(scopeLabel())}</td>
      <td><label class="alerts-table-toggle"><input data-field="enabled" type="checkbox" ${rule.enabled !== false ? 'checked' : ''}><span>${rule.enabled !== false ? 'On' : 'Off'}</span></label></td>
      <td><label class="alerts-table-toggle"><input data-field="email_enabled" type="checkbox" ${emailEnabled ? 'checked' : ''}><span>${emailEnabled ? 'On' : 'Off'}</span></label></td>
      <td><label class="alerts-table-toggle"><input data-field="push_enabled" type="checkbox" ${pushEnabled ? 'checked' : ''}><span>${pushEnabled ? 'On' : 'Off'}</span></label></td>
      <td class="alerts-table-actions"><button class="secondary alerts-policy-expand" data-expand-policy="${index}" type="button" aria-expanded="false" aria-controls="alert-policy-settings-${index}" title="Edit alert policy" aria-label="Edit ${escapeHtml(ruleLabel(rule))}">${ICONS.edit}</button><button class="delete-btn secondary alerts-policy-remove" data-delete-rule type="button" title="Remove alert policy" aria-label="Remove ${escapeHtml(ruleLabel(rule))}">${ICONS.remove}</button></td>
    </tr>
    <tr class="alerts-policy-details-row" id="alert-policy-settings-${index}" data-policy-details-for="${index}" hidden><td colspan="6">
    <article class="alerts-policy ${rule.enabled !== false ? 'is-enabled' : ''}" data-rule-index="${index}">
      <div class="alerts-policy-head"><div><span class="zones-panel-kicker">${escapeHtml(scopeLabel())} · Policy ${index + 1}</span><h3>${escapeHtml(ruleLabel(rule))}</h3></div><button type="button" class="secondary alerts-policy-collapse" data-collapse-policy="${index}" title="Collapse policy settings" aria-label="Collapse ${escapeHtml(ruleLabel(rule))} settings">${ICONS.chevronUp}</button></div>
      <div class="alerts-policy-grid">
        ${people ? `<label><span>Person</span><select data-field="person_id">${ruleOptions(rule)}</select></label>` : ''}
        ${behaviour ? '' : `<label><span>${confidenceLabel}</span><input data-field="${people ? 'min_confidence' : sound ? 'confidence_threshold' : 'min_confidence'}" type="number" min="0" max="1" step="0.01" value="${escapeHtml(String(confidence ?? (people ? '' : 0.5)))}"></label>`}
        ${!people && !sound && !behaviour ? '<label><span>Maximum Confidence</span><input data-field="max_confidence" type="number" min="0" max="1" step="0.01" value="' + escapeHtml(String(rule.max_confidence ?? 1)) + '"></label>' : ''}
        <label><span>${cooldownLabel}</span><input data-field="${people ? 'cooldown_minutes' : 'cooldown_seconds'}" type="number" min="0" max="${people ? '1440' : '3600'}" step="${people ? '1' : '5'}" value="${escapeHtml(String(cooldown ?? (people ? 5 : 60)))}"></label>
      </div>
      ${alertType === 'object' ? renderObjectSchedules(rule, index, schedules) : people ? '' : behaviour
        ? `<div class="alerts-policy-grid alerts-schedule-grid"><label><span>Notify From</span>${timeSelect(rule.notify_start, 'data-field="notify_start"')}</label><label><span>Notify Until</span>${timeSelect(rule.notify_end, 'data-field="notify_end"')}</label></div>`
        : `<div class="alerts-policy-grid alerts-schedule-grid"><label><span>Detect From</span>${timeSelect(rule.active_start, 'data-field="active_start"')}</label><label><span>Detect Until</span>${timeSelect(rule.active_end, 'data-field="active_end"')}</label><label><span>Notify From</span>${timeSelect(rule.notify_start, 'data-field="notify_start"')}</label><label><span>Notify Until</span>${timeSelect(rule.notify_end, 'data-field="notify_end"')}</label></div>`}
      ${alertType === 'object' ? '' : `<label class="alerts-recipient-field"><span>Email Recipients</span><input data-field="email_recipients" type="text" value="${escapeHtml(Array.isArray(rule.email_recipients) ? rule.email_recipients.join(', ') : rule.email_recipients || '')}" placeholder="alerts@example.com, me@example.com"></label>`}
      <p class="muted alerts-policy-note">${policyNote}</p>
    </article></td></tr>`;
  }).join('')}</tbody></table></div>`;
  $('alertsList').querySelectorAll('[data-field]').forEach((field) => field.addEventListener('change', () => {
    const policyRow = field.closest('[data-rule-index], [data-policy-details-for]');
    const index = Number(policyRow.dataset.ruleIndex ?? policyRow.dataset.policyDetailsFor);
    const rule = rules[index];
    const key = field.dataset.field;
    const scheduleEntry = alertType === 'object' ? field.closest('[data-schedule-index]') : null;
    const schedule = scheduleEntry ? ensureAlertSchedules(rule)[Number(scheduleEntry.dataset.scheduleIndex)] : null;
    const summary = $('alertsList').querySelector(`.alerts-policy-row[data-rule-index="${index}"]`);
    const details = $('alertsList').querySelector(`[data-policy-details-for="${index}"]`);
    if (schedule) {
      if (field.type === 'checkbox') schedule[key] = field.checked;
      else if (key === 'email_recipients') schedule.email_recipients = normalizeEmailList(field.value);
      else schedule[key] = field.value || null;
      ensureAlertSchedules(rule);
      [summary, details].forEach((container) => container?.querySelectorAll(`[data-field="${key}"]`).forEach((matchingField) => {
        const isSummary = Boolean(matchingField.closest('.alerts-policy-row'));
        if (key === 'email_enabled' || key === 'push_enabled') {
          if (!isSummary && matchingField.closest('[data-schedule-index]') !== scheduleEntry) return;
          const aggregate = rule.alert_schedules.some((entry) => entry[key]);
          matchingField.checked = isSummary ? aggregate : schedule[key];
          const state = matchingField.closest('label')?.querySelector('span');
          if (state && isSummary) state.textContent = aggregate ? 'On' : 'Off';
        } else if (matchingField !== field && matchingField.closest('[data-schedule-index]') === scheduleEntry) {
          if (key === 'email_recipients') matchingField.value = schedule.email_recipients.join(', ');
          else matchingField.value = schedule[key] || '';
        }
      }));
    } else if (field.type === 'checkbox') {
      if (alertType === 'object' && (key === 'email_enabled' || key === 'push_enabled')) {
        ensureAlertSchedules(rule).forEach((entry) => { entry[key] = field.checked; });
        ensureAlertSchedules(rule);
        details?.querySelectorAll(`[data-field="${key}"]`).forEach((matchingField) => { matchingField.checked = field.checked; });
      } else rule[key] = field.checked;
      [summary, details].forEach((container) => container?.querySelectorAll(`[data-field="${key}"]`).forEach((matchingField) => {
        matchingField.checked = field.checked;
        const state = matchingField.closest('label')?.querySelector('span');
        if (state && container === summary) state.textContent = field.checked ? 'On' : 'Off';
        else if (state && key === 'enabled') state.textContent = field.checked ? 'Enabled' : 'Disabled';
      }));
      if (key === 'enabled') {
        summary.classList.toggle('is-enabled', rule.enabled !== false);
        details?.querySelector('.alerts-policy')?.classList.toggle('is-enabled', rule.enabled !== false);
      }
    } else if (key === 'email_recipients') rule[key] = alertType === 'people' ? field.value : field.value.split(',').map((item) => item.trim()).filter(Boolean);
    else if (['min_confidence', 'max_confidence', 'confidence_threshold', 'cooldown_seconds', 'cooldown_minutes'].includes(key)) rule[key] = field.value === '' ? null : Number(field.value);
    else if (key === 'person_id') {
      rule.person_id = field.value || null;
      rule.name = enrolledPeople.find((person) => String(person.id) === field.value)?.name || 'Unknown Person';
      $('alertsList').querySelector(`.alerts-policy-row[data-rule-index="${index}"] .alerts-policy-name strong`).textContent = ruleLabel(rule);
      $('alertsList').querySelector(`[data-policy-details-for="${index}"] h3`).textContent = ruleLabel(rule);
    } else rule[key] = field.value || null;
    if (key === 'class') rule.name = soundClasses.find((sound) => sound.id === field.value)?.label || field.value;
    updateStats();
  }));
  $('alertsList').querySelectorAll('[data-add-schedule]').forEach((button) => button.addEventListener('click', () => {
    const rule = rules[Number(button.dataset.addSchedule)];
    ensureAlertSchedules(rule).push(defaultAlertSchedule());
    renderPolicies();
    const details = $('alertsList').querySelector(`[data-policy-details-for="${button.dataset.addSchedule}"]`);
    if (details) details.hidden = false;
    const expand = $('alertsList').querySelector(`[data-expand-policy="${button.dataset.addSchedule}"]`);
    if (expand) { expand.setAttribute('aria-expanded', 'true'); expand.classList.add('is-open'); }
  }));
  $('alertsList').querySelectorAll('[data-remove-schedule]').forEach((button) => button.addEventListener('click', () => {
    const rule = rules[Number(button.dataset.removeSchedule)];
    const schedules = ensureAlertSchedules(rule);
    schedules.splice(Number(button.dataset.scheduleIndex), 1);
    ensureAlertSchedules(rule);
    renderPolicies();
    const details = $('alertsList').querySelector(`[data-policy-details-for="${button.dataset.removeSchedule}"]`);
    if (details) details.hidden = false;
    const expand = $('alertsList').querySelector(`[data-expand-policy="${button.dataset.removeSchedule}"]`);
    if (expand) { expand.setAttribute('aria-expanded', 'true'); expand.classList.add('is-open'); }
  }));
  $('alertsList').querySelectorAll('[data-expand-policy]').forEach((button) => button.addEventListener('click', () => {
    const index = button.dataset.expandPolicy;
    const details = $('alertsList').querySelector(`[data-policy-details-for="${index}"]`);
    const expanded = button.getAttribute('aria-expanded') === 'true';
    details.hidden = expanded;
    if (!expanded) details.scrollIntoView({ block: 'nearest', behavior: 'smooth' });
    $('alertsList').querySelectorAll(`[data-expand-policy="${index}"]`).forEach((expandButton) => {
      expandButton.setAttribute('aria-expanded', String(!expanded));
      expandButton.classList.toggle('is-open', !expanded);
      expandButton.title = expanded ? 'Edit alert policy' : 'Close alert policy settings';
      expandButton.setAttribute('aria-label', `${expanded ? 'Edit' : 'Close'} ${ruleLabel(rules[Number(index)])} settings`);
    });
  }));
  $('alertsList').querySelectorAll('[data-collapse-policy]').forEach((button) => button.addEventListener('click', () => {
    const index = button.dataset.collapsePolicy;
    const details = $('alertsList').querySelector(`[data-policy-details-for="${index}"]`);
    const expandButton = $('alertsList').querySelector(`[data-expand-policy="${index}"]`);
    if (!details || !expandButton) return;
    details.hidden = true;
    expandButton.setAttribute('aria-expanded', 'false');
    expandButton.classList.remove('is-open');
    expandButton.title = 'Edit alert policy';
    expandButton.setAttribute('aria-label', `Edit ${ruleLabel(rules[Number(index)])}`);
  }));
  $('alertsList').querySelectorAll('[data-delete-rule]').forEach((button) => {
    const card = button.closest('[data-rule-index]');
    const rule = rules[Number(card.dataset.ruleIndex)];
    button.addEventListener('click', () => {
      if (alertType === 'people') {
        const actualIndex = (faceRulesPayload.rules || []).indexOf(rule);
        if (actualIndex >= 0) faceRulesPayload.rules.splice(actualIndex, 1);
      } else if (alertType === 'tripwire') {
        // The tripwire is a single per-zone object drawn on Zones; removing its
        // policy here deletes the line.
        const zone = currentZone();
        if (zone) delete zone.tripwire;
      } else if (alertType === 'loiter') {
        const zone = currentZone();
        if (zone) delete zone.loiter;
      } else if (alertType === 'time') {
        const zone = currentZone();
        if (zone) delete zone.time_of_day;
      } else {
        const allRules = alertType === 'sound' ? currentCamera().detection.sound.rules : currentZone().object_rules;
        const actualIndex = allRules.indexOf(rule);
        if (actualIndex >= 0) allRules.splice(actualIndex, 1);
      }
      renderPolicies();
    });
  });
}

$('cameraSelect').addEventListener('change', () => { cameraIndex = Number($('cameraSelect').value); zoneIndex = 0; renderSelectors(); renderPolicies(); });
$('zoneSelect').addEventListener('change', () => { zoneIndex = Number($('zoneSelect').value); renderPolicies(); });
$('alertTypeSelect').addEventListener('change', () => { alertType = $('alertTypeSelect').value; renderSelectors(); renderPolicies(); });
// Objects and sound classes are assigned on the Zones and Sounds pages, so the
// only thing added from here is a recognized-person alert. The button is hidden
// for the other alert types (see renderSelectors).
$('addAlertBtn').addEventListener('click', () => {
  if (alertType !== 'people' || !currentCamera() || !currentZone()) return;
  const existing = currentPeopleRules();
  const candidates = [{ id: '', name: 'Unknown Person' }, ...(enrolledPeople || []).map((person) => ({ id: String(person.id), name: person.name }))];
  const available = candidates.find((person) => !existing.some((rule) => String(rule.person_id || '') === person.id));
  if (!available) {
    window.showToast?.('Every enrolled person already has an alert in this zone.', true);
    return;
  }
  faceRulesPayload.rules.push(defaultPeopleRule(available.id, available.name));
  renderPolicies();
});

$('saveAlertsBtn').addEventListener('click', async () => {
  const button = $('saveAlertsBtn'); button.disabled = true;
  try {
    cameras.forEach((camera) => {
      (camera.detection?.zones || []).forEach((zone) => {
        zone.object_rules = (zone.object_rules || []).map((rule, index) => ({ ...defaultRule(rule.label, 'object'), ...rule, id: rule.id || `${rule.label}-${index + 1}`, alert_schedules: ensureAlertSchedules(rule) }));
        zone.object_labels = zone.object_rules.filter((rule) => !['motion', 'face'].includes(rule.label)).map((rule) => rule.label);
      });
      if (camera.detection?.sound) camera.detection.sound.rules = (camera.detection.sound.rules || []).map((rule) => ({ ...defaultRule(rule.class, 'sound'), ...rule }));
    });
    if (alertType === 'people') {
      faceRulesPayload = await api('/api/settings/face-detection-rules', { method: 'PUT', body: JSON.stringify({ rules: faceRulesPayload.rules || [] }) });
    } else {
      const result = await api('/api/cameras', { method: 'PUT', body: JSON.stringify({ cameras }) });
      cameras = result.cameras || cameras;
      faceRulesPayload = await api('/api/settings/face-detection-rules', { method: 'PUT', body: JSON.stringify({ rules: faceRulesPayload.rules || [] }) });
    }
    renderSelectors(); renderPolicies(); window.showToast?.('Alert policies saved.');
  } catch (error) { if (!window.daygleAuth?.redirecting) window.showToast?.(error.message, true); }
  finally { button.disabled = false; }
});

(async function init() {
  try {
    const [cameraPayload, soundPayload, peoplePayload, rulesPayload] = await Promise.all([
      api('/api/cameras'), api('/api/sound/classes'), api('/api/persons'), api('/api/settings/face-detection-rules'),
    ]);
    cameras = cameraPayload.cameras || [];
    soundClasses = soundPayload.classes || [];
    enrolledPeople = peoplePayload.persons || [];
    faceRulesPayload = rulesPayload || { rules: [] };
    cameras.forEach((camera) => (camera.detection?.zones || []).forEach((zone) => {
      zone.object_rules ||= [];
      zone.object_rules.forEach(ensureAlertSchedules);
    }));
    renderSelectors(); renderPolicies();
  } catch (error) { $('alertsList').innerHTML = `<div class="empty">${escapeHtml(error.message)}</div>`; }
}());
