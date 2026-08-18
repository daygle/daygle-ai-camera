// Auth (csrf token + user) lives on window.daygleAuth once loadAuth() runs
// and is read automatically by the shared api() helper.
let currentOffset = 0;
let currentTotal = 0;

// requireElements() is provided by web/utils.js (loaded before this script).
// Fail loud if a future HTML refactor removes any of these ids so we don't
// crash with a cryptic TypeError on the first innerHTML write below.
requireElements([
  'logBody', 'logEmpty', 'logTable', 'pagination',
  'pageInfo', 'prevBtn', 'nextBtn',
]);
const tbody = document.getElementById('logBody');
const logEmpty = document.getElementById('logEmpty');
const logTable = document.getElementById('logTable');
const pagination = document.getElementById('pagination');
const pageInfo = document.getElementById('pageInfo');
const prevBtn = document.getElementById('prevBtn');
const nextBtn = document.getElementById('nextBtn');

function getFilters() {
  return {
    date_from: document.getElementById('filterDateFrom').value,
    date_to: document.getElementById('filterDateTo').value,
    camera_id: document.getElementById('filterCamera').value.trim(),
    event_type: document.getElementById('filterEventType').value,
    severity: document.getElementById('filterSeverity').value,
  };
}

function buildQuery(offset) {
  const f = getFilters();
  const params = new URLSearchParams({ limit: LOG_PAGE_SIZE, offset });
  if (f.date_from) params.set('date_from', f.date_from);
  if (f.date_to) params.set('date_to', f.date_to);
  // Resolve the date filter against the viewer's local day (matches /timeline).
  if (f.date_from || f.date_to) params.set('tz_offset_minutes', String(new Date().getTimezoneOffset()));
  if (f.camera_id) params.set('camera_id', f.camera_id);
  if (f.event_type) params.set('event_type', f.event_type);
  if (f.severity) params.set('severity', f.severity);
  return params.toString();
}

function formatDetails(details) {
  if (!details || typeof details !== 'object' || Object.keys(details).length === 0) return '-';
  return Object.entries(details)
    .map(([k, v]) => `${k}: ${v === true ? 'yes' : v === false ? 'no' : v}`)
    .join(' · ');
}

function severityBadgeClass(severity) {
  if (severity === 'error') return 'status-failed';
  if (severity === 'warning') return 'status-pending';
  return 'status-success';
}

async function loadAuth() {
  // nav.js kicks off the shared /api/auth/me at script load; awaiting
  // daygleAuthReady here means this page never issues its own duplicate
  // /api/auth/me on bootstrap. The promise never throws (nav.js's IIFE
  // swallows network errors), so any auth failure here is treated as
  // anonymous - the api() helper in utils.js redirects to /login on a
  // real 401, matching the previous try/catch behaviour.
  await window.daygleAuthReady;
}

async function loadEntries(offset = 0) {
  currentOffset = offset;
  const filters = getFilters();
  if (filters.date_from && filters.date_to && filters.date_from > filters.date_to) {
    window.showToast?.('From date must not be after To date.', true);
    return;
  }
  try {
    const data = await api(`/api/camera-log?${buildQuery(offset)}`);
    currentTotal = data.total || 0;
    renderEntries(data.entries || []);
    renderPagination();
  } catch (err) {
    // Skip UI updates if api() triggered a 401 redirect
    if (window.daygleAuth?.redirecting) return;
    window.showToast?.(err.message, true);
  }
}

function makeCell(text, opts = {}) {
  const td = document.createElement('td');
  if (opts.label) td.dataset.label = opts.label;
  if (opts.noWrap) td.style.whiteSpace = 'nowrap';
  if (opts.className) td.className = opts.className;
  if (opts.code) {
    const code = document.createElement('code');
    code.textContent = text;
    td.appendChild(code);
  } else if (opts.badge) {
    const span = document.createElement('span');
    span.className = `status-badge ${opts.badge}`;
    span.textContent = text;
    td.appendChild(span);
  } else {
    td.textContent = text;
  }
  return td;
}

function renderEntries(entries) {
  tbody.innerHTML = '';
  const isEmpty = entries.length === 0;
  logEmpty.hidden = !isEmpty;
  logTable.hidden = isEmpty;
  if (isEmpty) return;

  for (const entry of entries) {
    const tr = document.createElement('tr');
    const cameraLabel = entry.camera_name || entry.camera_id || '-';
    tr.appendChild(makeCell(formatLogTime(entry.created_at), { label: 'Time', noWrap: true }));
    tr.appendChild(makeCell(cameraLabel, { label: 'Camera' }));
    tr.appendChild(makeCell(entry.event_type || '-', { label: 'Event', code: true }));
    tr.appendChild(makeCell(entry.severity || 'info', { label: 'Severity', badge: severityBadgeClass(entry.severity) }));
    tr.appendChild(makeCell(entry.message || '-', { label: 'Message', className: 'details-cell' }));
    tr.appendChild(makeCell(formatDetails(entry.details), { label: 'Details', className: 'details-cell' }));
    tbody.appendChild(tr);
  }
}

function renderPagination() {
  const totalPages = Math.max(1, Math.ceil(currentTotal / LOG_PAGE_SIZE));
  const currentPage = Math.floor(currentOffset / LOG_PAGE_SIZE) + 1;
  pagination.hidden = currentTotal <= LOG_PAGE_SIZE;
  pageInfo.textContent = `Page ${currentPage} of ${totalPages} (${currentTotal} total)`;
  prevBtn.disabled = currentOffset <= 0;
  nextBtn.disabled = currentOffset + LOG_PAGE_SIZE >= currentTotal;
}

prevBtn.addEventListener('click', () => {
  if (currentOffset > 0) loadEntries(Math.max(0, currentOffset - LOG_PAGE_SIZE));
});
nextBtn.addEventListener('click', () => {
  if (currentOffset + LOG_PAGE_SIZE < currentTotal) loadEntries(currentOffset + LOG_PAGE_SIZE);
});

document.getElementById('applyFiltersBtn').addEventListener('click', () => loadEntries(0));
document.getElementById('clearFiltersBtn').addEventListener('click', () => {
  document.getElementById('filterDateFrom').value = '';
  document.getElementById('filterDateTo').value = '';
  document.getElementById('filterCamera').value = '';
  document.getElementById('filterEventType').value = '';
  document.getElementById('filterSeverity').value = '';
  loadEntries(0);
});
document.getElementById('refreshBtn').addEventListener('click', () => loadEntries(currentOffset));

document.getElementById('clearLogBtn').addEventListener('click', async () => {
  if (!window.confirm('Clear all camera diagnostic events? This cannot be undone.')) return;
  try {
    const data = await api('/api/camera-log', { method: 'DELETE' });
    window.showToast?.(`Cleared ${data.deleted || 0} camera log event${data.deleted === 1 ? '' : 's'}.`);
    loadEntries(0);
  } catch (err) {
    // Skip UI updates if api() triggered a 401 redirect
    if (window.daygleAuth?.redirecting) return;
    window.showToast?.(err.message, true);
  }
});

document.getElementById('filterCamera').addEventListener('keydown', (e) => {
  if (e.key === 'Enter') loadEntries(0);
});

loadAuth().then(() => loadEntries(0)).catch((err) => {
  if (window.daygleAuth?.redirecting) return;
  window.showToast?.(err.message, true);
});
