// LOG_PAGE_SIZE (50) is provided by web/utils.js so it stays in sync with
// web/camera-log.js. Auth (csrf token + user) lives on window.daygleAuth after
// loadAuth() runs and is read automatically by the shared api() helper.
let currentOffset = 0;
let currentTotal = 0;

const tbody = document.getElementById('auditBody');
const auditEmpty = document.getElementById('auditEmpty');
const auditTable = document.getElementById('auditTable');
const pagination = document.getElementById('pagination');
const pageInfo = document.getElementById('pageInfo');
const prevBtn = document.getElementById('prevBtn');
const nextBtn = document.getElementById('nextBtn');

function getFilters() {
  return {
    username: document.getElementById('filterUsername').value.trim(),
    action: document.getElementById('filterAction').value,
    resource: document.getElementById('filterResource').value.trim(),
  };
}

function buildQuery(offset) {
  const f = getFilters();
  const params = new URLSearchParams({ limit: LOG_PAGE_SIZE, offset });
  if (f.username) params.set('username', f.username);
  if (f.action) params.set('action', f.action);
  if (f.resource) params.set('resource', f.resource);
  return params.toString();
}

function formatDetails(details) {
  if (!details || typeof details !== 'object' || Object.keys(details).length === 0) return '-';
  return Object.entries(details)
    .map(([k, v]) => `${k}: ${v === true ? 'yes' : v === false ? 'no' : v}`)
    .join(' · ');
}

async function loadAuth() {
  // nav.js kicks off the shared /api/auth/me at script load; awaiting
  // daygleAuthReady here means this page never issues its own duplicate
  // /api/auth/me on bootstrap. The api() helper attaches X-CSRF-Token to
  // any state-changing call (none on this page today, but the wiring is
  // the same as every other page).
  await window.daygleAuthReady;
}

async function loadEntries(offset = 0) {
  currentOffset = offset;
  try {
    const data = await api(`/api/audit?${buildQuery(offset)}`);
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
  auditEmpty.hidden = !isEmpty;
  auditTable.hidden = isEmpty;
  if (isEmpty) return;

  for (const entry of entries) {
    const tr = document.createElement('tr');
    const statusClass = entry.status === 'success' ? 'status-success' : 'status-failed';
    tr.appendChild(makeCell(formatLogTime(entry.created_at), { noWrap: true }));
    tr.appendChild(makeCell(entry.username || '-'));
    tr.appendChild(makeCell(entry.action || '-', { code: true }));
    tr.appendChild(makeCell(entry.resource || '-', { code: true }));
    tr.appendChild(makeCell(entry.resource_id != null ? String(entry.resource_id) : '-'));
    tr.appendChild(makeCell(entry.status || 'success', { badge: statusClass }));
    tr.appendChild(makeCell(entry.ip_address || '-', { noWrap: true }));
    tr.appendChild(makeCell(formatDetails(entry.details), { className: 'details-cell' }));
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
  document.getElementById('filterUsername').value = '';
  document.getElementById('filterAction').value = '';
  document.getElementById('filterResource').value = '';
  loadEntries(0);
});
document.getElementById('refreshBtn').addEventListener('click', () => loadEntries(currentOffset));

document.getElementById('filterUsername').addEventListener('keydown', (e) => {
  if (e.key === 'Enter') loadEntries(0);
});
document.getElementById('filterResource').addEventListener('keydown', (e) => {
  if (e.key === 'Enter') loadEntries(0);
});

// Bootstrap: prime CSRF/user via setApiAuth first so subsequent api() calls
// (none today, but consistent with every other page) are authenticated.
loadAuth().then(() => loadEntries(0));
