// Streams journalctl output for daygle-ai-camera.
// REST:  GET /api/application-log?lines=200&level=...   (initial batch, server-side filtered)
// SSE:   GET /api/application-log/stream                (live follow, client-side filtered)

const MAX_ROWS = 500;

const applicationLogBody = document.getElementById('applicationLogBody');
const applicationLogTable = document.getElementById('applicationLogTable');
const applicationLogEmpty = document.getElementById('applicationLogEmpty');
const applicationLogOutputWrap = document.getElementById('applicationLogOutputWrap');
const entryCount = document.getElementById('entryCount');
const liveBtn = document.getElementById('liveBtn');
const liveStatus = document.getElementById('liveStatus');
const autoScrollCheck = document.getElementById('autoScrollCheck');

let eventSource = null;
let activeLevel = '';
let activeSearch = '';

// Priority order for level filtering (highest severity first)
const LEVEL_ORDER = ['EMERG', 'ALERT', 'CRIT', 'ERROR', 'WARNING', 'NOTICE', 'INFO', 'DEBUG'];
const LEVEL_SETS = {
  error:   ['EMERG', 'ALERT', 'CRIT', 'ERROR'],
  warning: ['EMERG', 'ALERT', 'CRIT', 'ERROR', 'WARNING'],
  notice:  ['EMERG', 'ALERT', 'CRIT', 'ERROR', 'WARNING', 'NOTICE'],
  info:    ['EMERG', 'ALERT', 'CRIT', 'ERROR', 'WARNING', 'NOTICE', 'INFO'],
  debug:   LEVEL_ORDER,
};

function levelBadgeClass(level) {
  const l = (level || 'INFO').toUpperCase();
  if (['EMERG', 'ALERT', 'CRIT', 'ERROR'].includes(l)) return 'status-failed';
  if (l === 'WARNING') return 'status-pending';
  if (l === 'DEBUG') return 'status-muted';
  return 'status-success';
}

// Honours the operator's preferred date + time format via
// ``window.daygleDatePrefs`` (set on ``profile.js`` save and propagated
// through ``daygleDatePrefsChanged``). ``formatDate`` composes the
// configured ``dateFormat`` + ``timeFormat`` + ``timezone`` exactly the
// same way the camera-log + audit pages already do, so all three log
// pages stay in step with each other and the rest of the dashboard when
// the operator changes their format under ``/profile``. The previous
// implementation called ``toLocaleDateString`` / ``toLocaleTimeString``
// with hard-coded option objects ignoring the operator preference.
function formatEntryTime(iso) {
  if (!iso) return '-';
  try {
    return formatDate(iso);
  } catch {
    return String(iso || '-');
  }
}

function isRowVisible(tr) {
  const lvl = tr.dataset.level || 'INFO';
  if (activeLevel && !(LEVEL_SETS[activeLevel] || LEVEL_ORDER).includes(lvl)) return false;
  if (activeSearch && !(tr.dataset.msg || '').includes(activeSearch)) return false;
  return true;
}

function makeRow(entry) {
  const tr = document.createElement('tr');
  tr.dataset.level = (entry.level || 'INFO').toUpperCase();
  tr.dataset.msg = (entry.message || '').toLowerCase();

  const tdTime = document.createElement('td');
  tdTime.dataset.label = 'Time';
  tdTime.style.whiteSpace = 'nowrap';
  tdTime.textContent = formatEntryTime(entry.timestamp);

  const tdLevel = document.createElement('td');
  tdLevel.dataset.label = 'Level';
  const badge = document.createElement('span');
  badge.className = `status-badge ${levelBadgeClass(entry.level)}`;
  badge.textContent = entry.level || 'INFO';
  tdLevel.appendChild(badge);

  const tdMsg = document.createElement('td');
  tdMsg.dataset.label = 'Message';
  tdMsg.textContent = entry.message || '';

  tr.appendChild(tdTime);
  tr.appendChild(tdLevel);
  tr.appendChild(tdMsg);
  return tr;
}

function updateCount() {
  const total = applicationLogBody.children.length;
  const visible = Array.from(applicationLogBody.children).filter((r) => !r.hidden).length;
  if (visible === total) {
    entryCount.textContent = `${total} entr${total === 1 ? 'y' : 'ies'}`;
  } else {
    entryCount.textContent = `${visible} of ${total} entr${total === 1 ? 'y' : 'ies'}`;
  }
  const hasVisible = visible > 0;
  applicationLogEmpty.hidden = hasVisible || total === 0;
  applicationLogTable.hidden = total === 0;
}

function applyClientFilters() {
  for (const tr of applicationLogBody.children) {
    tr.hidden = !isRowVisible(tr);
  }
  updateCount();
}

function trimOldRows() {
  while (applicationLogBody.children.length > MAX_ROWS) {
    applicationLogBody.removeChild(applicationLogBody.firstChild);
  }
}

function appendEntry(entry) {
  const tr = makeRow(entry);
  tr.hidden = !isRowVisible(tr);
  applicationLogBody.appendChild(tr);
  trimOldRows();
  applicationLogTable.hidden = false;
  if (!tr.hidden) applicationLogEmpty.hidden = true;
  updateCount();
  if (!tr.hidden && autoScrollCheck.checked) {
    applicationLogOutputWrap.scrollTop = applicationLogOutputWrap.scrollHeight;
  }
}

async function loadEntries() {
  const level = document.getElementById('filterLevel').value;
  const params = new URLSearchParams({ lines: 200 });
  if (level) params.set('level', level);
  try {
    const data = await api(`/api/application-log?${params}`);
    applicationLogBody.innerHTML = '';
    if (data.unavailable) {
      liveStatus.textContent = 'journalctl unavailable';
      applicationLogEmpty.hidden = false;
      applicationLogTable.hidden = true;
      updateCount();
      return;
    }
    for (const entry of data.entries || []) {
      const tr = makeRow(entry);
      tr.hidden = !isRowVisible(tr);
      applicationLogBody.appendChild(tr);
    }
    trimOldRows();
    applicationLogTable.hidden = applicationLogBody.children.length === 0;
    applicationLogEmpty.hidden = applicationLogBody.children.length > 0;
    updateCount();
    if (autoScrollCheck.checked) {
      applicationLogOutputWrap.scrollTop = applicationLogOutputWrap.scrollHeight;
    }
  } catch (err) {
    if (window.daygleAuth?.redirecting) return;
    window.showToast?.(err.message, true);
  }
}

function setLiveActive(active) {
  liveBtn.classList.toggle('application-log-live-active', active);
  liveBtn.textContent = active ? 'Live' : 'Resume';
}

function connectStream() {
  if (eventSource) {
    eventSource.close();
    eventSource = null;
  }
  liveStatus.textContent = 'Connecting…';
  setLiveActive(true);

  eventSource = new EventSource('/api/application-log/stream');

  eventSource.onopen = () => {
    liveStatus.textContent = 'Live';
  };

  eventSource.onmessage = (e) => {
    try {
      const entry = JSON.parse(e.data);
      if (entry.error) {
        liveStatus.textContent = `Unavailable: ${entry.error}`;
        return;
      }
      appendEntry(entry);
    } catch {
      // ignore malformed SSE frames
    }
  };

  eventSource.onerror = () => {
    liveStatus.textContent = 'Disconnected';
    setLiveActive(false);
    eventSource.close();
    eventSource = null;
  };
}

function disconnectStream() {
  if (eventSource) {
    eventSource.close();
    eventSource = null;
  }
  liveStatus.textContent = 'Paused';
  setLiveActive(false);
}

liveBtn.addEventListener('click', () => {
  if (eventSource) {
    disconnectStream();
  } else {
    connectStream();
  }
});

document.getElementById('applyFiltersBtn').addEventListener('click', async () => {
  activeLevel = document.getElementById('filterLevel').value;
  activeSearch = document.getElementById('filterSearch').value.trim().toLowerCase();
  await loadEntries();
  if (!eventSource) connectStream();
});

document.getElementById('clearFiltersBtn').addEventListener('click', async () => {
  document.getElementById('filterLevel').value = '';
  document.getElementById('filterSearch').value = '';
  activeLevel = '';
  activeSearch = '';
  await loadEntries();
  if (!eventSource) connectStream();
});

document.getElementById('filterSearch').addEventListener('input', () => {
  activeSearch = document.getElementById('filterSearch').value.trim().toLowerCase();
  applyClientFilters();
});

document.getElementById('filterSearch').addEventListener('keydown', (e) => {
  if (e.key === 'Enter') {
    activeSearch = document.getElementById('filterSearch').value.trim().toLowerCase();
    applyClientFilters();
  }
});

document.getElementById('refreshBtn').addEventListener('click', () => loadEntries());

document.getElementById('clearDisplayBtn').addEventListener('click', () => {
  applicationLogBody.innerHTML = '';
  applicationLogTable.hidden = true;
  applicationLogEmpty.hidden = false;
  updateCount();
});

async function init() {
  await window.daygleAuthReady;
  activeLevel = document.getElementById('filterLevel').value;
  activeSearch = document.getElementById('filterSearch').value.trim().toLowerCase();
  await loadEntries();
  connectStream();
}

init().catch((err) => {
  if (window.daygleAuth?.redirecting) return;
  window.showToast?.(err.message, true);
});
