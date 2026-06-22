// Streams journalctl output for daygle-ai-camera.
// REST:  GET /api/app-log?lines=200&level=...   (initial batch, server-side filtered)
// SSE:   GET /api/app-log/stream                (live follow, client-side filtered)

const MAX_ROWS = 500;

const appLogBody = document.getElementById('appLogBody');
const appLogTable = document.getElementById('appLogTable');
const appLogEmpty = document.getElementById('appLogEmpty');
const appLogOutputWrap = document.getElementById('appLogOutputWrap');
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
  error:   new Set(['EMERG', 'ALERT', 'CRIT', 'ERROR']),
  warning: new Set(['EMERG', 'ALERT', 'CRIT', 'ERROR', 'WARNING']),
  notice:  new Set(['EMERG', 'ALERT', 'CRIT', 'ERROR', 'WARNING', 'NOTICE']),
  info:    new Set(['EMERG', 'ALERT', 'CRIT', 'ERROR', 'WARNING', 'NOTICE', 'INFO']),
  debug:   new Set(LEVEL_ORDER),
};

function levelBadgeClass(level) {
  const l = (level || 'INFO').toUpperCase();
  if (['EMERG', 'ALERT', 'CRIT', 'ERROR'].includes(l)) return 'status-failed';
  if (l === 'WARNING') return 'status-pending';
  if (l === 'DEBUG') return 'status-muted';
  return 'status-success';
}

function formatEntryTime(iso) {
  if (!iso) return '—';
  try {
    const d = new Date(iso);
    const date = d.toLocaleDateString([], { month: 'short', day: '2-digit' });
    const time = d.toLocaleTimeString([], { hour12: false, hour: '2-digit', minute: '2-digit', second: '2-digit' });
    return `${date} ${time}`;
  } catch {
    return iso;
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
  const total = appLogBody.children.length;
  const visible = Array.from(appLogBody.children).filter((r) => !r.hidden).length;
  if (visible === total) {
    entryCount.textContent = `${total} entr${total === 1 ? 'y' : 'ies'}`;
  } else {
    entryCount.textContent = `${visible} of ${total} entr${total === 1 ? 'y' : 'ies'}`;
  }
  const hasVisible = visible > 0;
  appLogEmpty.hidden = hasVisible || total === 0;
  appLogTable.hidden = total === 0;
}

function applyClientFilters() {
  for (const tr of appLogBody.children) {
    tr.hidden = !isRowVisible(tr);
  }
  updateCount();
}

function trimOldRows() {
  while (appLogBody.children.length > MAX_ROWS) {
    appLogBody.removeChild(appLogBody.firstChild);
  }
}

function appendEntry(entry) {
  const tr = makeRow(entry);
  tr.hidden = !isRowVisible(tr);
  appLogBody.appendChild(tr);
  trimOldRows();
  appLogTable.hidden = false;
  if (!tr.hidden) appLogEmpty.hidden = true;
  updateCount();
  if (!tr.hidden && autoScrollCheck.checked) {
    appLogOutputWrap.scrollTop = appLogOutputWrap.scrollHeight;
  }
}

async function loadEntries() {
  const level = document.getElementById('filterLevel').value;
  const params = new URLSearchParams({ lines: 200 });
  if (level) params.set('level', level);
  try {
    const data = await api(`/api/app-log?${params}`);
    appLogBody.innerHTML = '';
    if (data.unavailable) {
      liveStatus.textContent = 'journalctl unavailable';
      appLogEmpty.hidden = false;
      appLogTable.hidden = true;
      updateCount();
      return;
    }
    for (const entry of data.entries || []) {
      const tr = makeRow(entry);
      tr.hidden = !isRowVisible(tr);
      appLogBody.appendChild(tr);
    }
    trimOldRows();
    appLogTable.hidden = appLogBody.children.length === 0;
    appLogEmpty.hidden = appLogBody.children.length > 0;
    updateCount();
    if (autoScrollCheck.checked) {
      appLogOutputWrap.scrollTop = appLogOutputWrap.scrollHeight;
    }
  } catch (err) {
    if (window.daygleAuth?.redirecting) return;
    window.showToast?.(err.message, true);
  }
}

function setLiveActive(active) {
  liveBtn.classList.toggle('app-log-live-active', active);
  liveBtn.textContent = active ? 'Live' : 'Resume';
}

function connectStream() {
  if (eventSource) {
    eventSource.close();
    eventSource = null;
  }
  liveStatus.textContent = 'Connecting…';
  setLiveActive(true);

  eventSource = new EventSource('/api/app-log/stream');

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
  appLogBody.innerHTML = '';
  appLogTable.hidden = true;
  appLogEmpty.hidden = false;
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
