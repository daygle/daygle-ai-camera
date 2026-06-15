const PAGE_SIZE = 50;
let currentOffset = 0;
let currentTotal = 0;
let csrfToken = null;

const tbody = document.getElementById('logBody');
const logEmpty = document.getElementById('logEmpty');
const logTable = document.getElementById('logTable');
const pagination = document.getElementById('pagination');
const pageInfo = document.getElementById('pageInfo');
const prevBtn = document.getElementById('prevBtn');
const nextBtn = document.getElementById('nextBtn');

function getFilters() {
  return {
    camera_id: document.getElementById('filterCamera').value.trim(),
    event_type: document.getElementById('filterEventType').value,
    severity: document.getElementById('filterSeverity').value,
  };
}

function buildQuery(offset) {
  const f = getFilters();
  const params = new URLSearchParams({ limit: PAGE_SIZE, offset });
  if (f.camera_id) params.set('camera_id', f.camera_id);
  if (f.event_type) params.set('event_type', f.event_type);
  if (f.severity) params.set('severity', f.severity);
  return params.toString();
}

// Convert date AND time together in one local-timezone call so the two never
// disagree across a UTC midnight boundary (see web/utils.js formatDate).
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

function severityBadgeClass(severity) {
  if (severity === 'error') return 'status-failed';
  if (severity === 'warning') return 'status-pending';
  return 'status-success';
}

async function loadAuth() {
  try {
    const resp = await fetch('/api/auth/me');
    if (resp.ok) {
      const data = await resp.json();
      csrfToken = data.csrf_token || null;
    }
  } catch {
    /* CSRF token only needed for the Clear button; ignore. */
  }
}

async function loadEntries(offset = 0) {
  currentOffset = offset;
  try {
    const resp = await fetch(`/api/camera-log?${buildQuery(offset)}`);
    if (resp.status === 401) {
      window.location.href = '/login';
      return;
    }
    if (!resp.ok) {
      window.showToast?.('Failed to load camera log: ' + resp.status, true);
      return;
    }
    const data = await resp.json();
    currentTotal = data.total || 0;
    renderEntries(data.entries || []);
    renderPagination();
  } catch (err) {
    window.showToast?.('Error loading camera log', true);
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
    tr.appendChild(makeCell(formatTime(entry.created_at), { label: 'Time', noWrap: true }));
    tr.appendChild(makeCell(cameraLabel, { label: 'Camera' }));
    tr.appendChild(makeCell(entry.event_type || '-', { label: 'Event', code: true }));
    tr.appendChild(makeCell(entry.severity || 'info', { label: 'Severity', badge: severityBadgeClass(entry.severity) }));
    tr.appendChild(makeCell(entry.message || '-', { label: 'Message', className: 'details-cell' }));
    tr.appendChild(makeCell(formatDetails(entry.details), { label: 'Details', className: 'details-cell' }));
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
  document.getElementById('filterCamera').value = '';
  document.getElementById('filterEventType').value = '';
  document.getElementById('filterSeverity').value = '';
  loadEntries(0);
});
document.getElementById('refreshBtn').addEventListener('click', () => loadEntries(currentOffset));

document.getElementById('clearLogBtn').addEventListener('click', async () => {
  if (!window.confirm('Clear all camera diagnostic events? This cannot be undone.')) return;
  try {
    const resp = await fetch('/api/camera-log', {
      method: 'DELETE',
      headers: csrfToken ? { 'X-CSRF-Token': csrfToken } : {},
    });
    if (!resp.ok) {
      window.showToast?.('Failed to clear camera log: ' + resp.status, true);
      return;
    }
    const data = await resp.json();
    window.showToast?.(`Cleared ${data.deleted || 0} camera log event${data.deleted === 1 ? '' : 's'}.`);
    loadEntries(0);
  } catch (err) {
    window.showToast?.('Error clearing camera log', true);
  }
});

document.getElementById('filterCamera').addEventListener('keydown', (e) => {
  if (e.key === 'Enter') loadEntries(0);
});

loadAuth().then(() => loadEntries(0));
