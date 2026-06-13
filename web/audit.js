const PAGE_SIZE = 50;
let currentOffset = 0;
let currentTotal = 0;
let csrfToken = null;

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
  const params = new URLSearchParams({ limit: PAGE_SIZE, offset });
  if (f.username) params.set('username', f.username);
  if (f.action) params.set('action', f.action);
  if (f.resource) params.set('resource', f.resource);
  return params.toString();
}

function formatTime(iso) {
  if (!iso) return '-';
  try {
    return new Date(iso).toLocaleString(undefined, {
      year: 'numeric', month: 'short', day: 'numeric',
      hour: '2-digit', minute: '2-digit', second: '2-digit',
    });
  } catch {
    return iso;
  }
}

function formatDetails(details) {
  if (!details || typeof details !== 'object' || Object.keys(details).length === 0) return '-';
  return Object.entries(details)
    .map(([k, v]) => `${k}: ${v === true ? 'yes' : v === false ? 'no' : v}`)
    .join(' · ');
}

async function loadEntries(offset = 0) {
  currentOffset = offset;
  try {
    const resp = await fetch(`/api/audit?${buildQuery(offset)}`);
    if (resp.status === 401) {
      window.location.href = '/login';
      return;
    }
    if (!resp.ok) {
      showToast('Failed to load audit log: ' + resp.status, true);
      return;
    }
    const data = await resp.json();
    currentTotal = data.total || 0;
    renderEntries(data.entries || []);
    renderPagination();
  } catch (err) {
    showToast('Error loading audit log', true);
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
    tr.appendChild(makeCell(formatTime(entry.created_at), { noWrap: true }));
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
  const totalPages = Math.max(1, Math.ceil(currentTotal / PAGE_SIZE));
  const currentPage = Math.floor(currentOffset / PAGE_SIZE) + 1;
  pagination.hidden = currentTotal <= PAGE_SIZE;
  pageInfo.textContent = `Page ${currentPage} of ${totalPages} (${currentTotal} total)`;
  prevBtn.disabled = currentOffset <= 0;
  nextBtn.disabled = currentOffset + PAGE_SIZE >= currentTotal;
}

prevBtn.addEventListener('click', () => {
  if (currentOffset > 0) loadEntries(Math.max(0, currentOffset - PAGE_SIZE));
});
nextBtn.addEventListener('click', () => {
  if (currentOffset + PAGE_SIZE < currentTotal) loadEntries(currentOffset + PAGE_SIZE);
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

loadEntries(0);
