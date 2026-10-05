// library_filters.js - the one filter bar shared by the Events, Recordings and
// Snapshots pages.
//
// Each page used to grow its own controls (pills and a plain-English search on
// Events; a collapsible eight-field form with an Apply button on Recordings and
// Snapshots), so the same question - "what happened on the driveway camera
// yesterday?" - had to be asked three different ways. This component renders
// the same bar on all three:
//
//   [ search keywords ............................ ] [Search] [Ask AI]
//   [Today 24h 7d 30d All Custom]  [All Object Motion Sound]  [Filters (n)]
//   (Custom)  From [date][time]   To [date][time]
//   (Filters) Camera | Label | Face | Sort | [x] Alert Only
//   123 events · [camera: Driveway x] [label: Person x]          Clear all
//
// Every control applies as soon as it changes - there is no Apply button - and
// the state is mirrored into the URL query string so a filtered view survives
// a refresh and can be shared or bookmarked. The Label and Face dropdowns are
// filled from /api/library/facets for the selected time window, so they list
// what exists in that range rather than only what is already loaded.
//
// Pages read the state through controller.query() and map it to their own API
// (all three list endpoints take the same since/until/camera_id/label/face/q/
// alerted_only/sort parameters).
//
// escapeHtml, api, renderTimeSelect, timeSelectValue, setTimeSelectValue,
// titleCase, formatUserDate and daygleSinceParamForRange come from
// web/utils.js, which every page loads first.

const LIBRARY_FILTER_PANEL_KEY = 'daygle.library.filters.open';
const LIBRARY_TIME_FROM_DEFAULT = '00:00';
const LIBRARY_TIME_TO_DEFAULT = '23:55';
const LIBRARY_SEARCH_DEBOUNCE_MS = 450;

const LIBRARY_RANGES = [
  { value: 'today', label: 'Today' },
  { value: '24h', label: '24h' },
  { value: '7d', label: '7 days' },
  { value: '30d', label: '30 days' },
  { value: 'all', label: 'All time' },
  { value: 'custom', label: 'Custom' },
];

const LIBRARY_TYPE_ICONS = {
  all: '<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><line x1="8" y1="6" x2="21" y2="6"/><line x1="8" y1="12" x2="21" y2="12"/><line x1="8" y1="18" x2="21" y2="18"/><line x1="3" y1="6" x2="3.01" y2="6"/><line x1="3" y1="12" x2="3.01" y2="12"/><line x1="3" y1="18" x2="3.01" y2="18"/></svg>',
  object: '<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="12" cy="12" r="3"/><path d="M2 12s3-7 10-7 10 7 10 7-3 7-10 7-10-7-10-7z"/></svg>',
  motion: '<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="13" cy="4" r="2"/><path d="m4 19.5 4-4.5 1.5 4 5.5-3-2-7 4-3"/></svg>',
  sound: '<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><polygon points="11 5 6 9 2 9 2 15 6 15 11 19 11 5"/><path d="M19.07 4.93a10 10 0 0 1 0 14.14"/><path d="M15.54 8.46a5 5 0 0 1 0 7.07"/></svg>',
  continuous: '<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><polyline points="23 4 23 10 17 10"/><polyline points="1 20 1 14 7 14"/><path d="M3.51 9a9 9 0 0 1 14.85-3.36L23 10M1 14l4.64 4.36A9 9 0 0 0 20.49 15"/></svg>',
};

const LIBRARY_TYPE_LABELS = {
  all: 'All',
  object: 'Object',
  motion: 'Motion',
  sound: 'Sound',
  continuous: 'Continuous',
};

function libraryLocalDateString(date) {
  return `${date.getFullYear()}-${String(date.getMonth() + 1).padStart(2, '0')}-${String(date.getDate()).padStart(2, '0')}`;
}

// A local-time instant for a YYYY-MM-DD + HH:MM bound (the browser hands back
// dates without a timezone). The To bound covers its whole final minute.
function libraryLocalBoundary(dateString, timeString, endOfMinute) {
  const [year, month, day] = String(dateString || '').split('-').map((part) => Number.parseInt(part, 10));
  if (!year || !month || !day) return null;
  const match = String(timeString || '').match(/^(\d{1,2}):(\d{2})$/);
  const hour = match ? Math.min(23, Number.parseInt(match[1], 10) || 0) : (endOfMinute ? 23 : 0);
  const minute = match ? Math.min(59, Number.parseInt(match[2], 10) || 0) : (endOfMinute ? 59 : 0);
  return new Date(year, month - 1, day, hour, minute, endOfMinute ? 59 : 0, endOfMinute ? 999 : 0);
}

function libraryDefaultState() {
  return {
    q: '',
    range: 'today',
    dateFrom: '',
    timeFrom: LIBRARY_TIME_FROM_DEFAULT,
    dateTo: '',
    timeTo: LIBRARY_TIME_TO_DEFAULT,
    type: 'all',
    camera: '',
    label: '',
    face: '',
    alerted: false,
    sort: 'newest',
  };
}

// The since/until ISO window for a state. Pure, so tests and pages share it.
function libraryFilterWindow(state, now = new Date()) {
  const range = state.range || 'today';
  if (range === 'custom') {
    const from = libraryLocalBoundary(state.dateFrom, state.timeFrom, false);
    const to = libraryLocalBoundary(state.dateTo, state.timeTo, true);
    return { since: from ? from.toISOString() : '', until: to ? to.toISOString() : '' };
  }
  if (range === '24h') return { since: new Date(now.getTime() - 24 * 3600 * 1000).toISOString(), until: '' };
  return { since: daygleSinceParamForRange(range) || '', until: '' };
}

// The page-facing query (see controller.query()) for a state. A page that
// has no filter bar yet falls back to libraryDefaultQuery(), so it still opens
// on today instead of requesting the whole history.
function libraryQueryForState(state) {
  const { since, until } = libraryFilterWindow(state);
  return {
    since,
    until,
    q: state.q,
    type: state.type,
    camera_id: state.camera,
    label: state.label,
    face: state.face,
    alerted_only: state.alerted,
    sort: state.sort,
    range: state.range,
  };
}

// eslint-disable-next-line no-unused-vars -- ESLint: exported for later scripts (events/recordings/snapshots)
function libraryDefaultQuery() {
  return libraryQueryForState(libraryDefaultState());
}

// Which optional controls a page shows. The Timeline keeps its own camera,
// day and time pickers, so it turns the bar's time range, camera and sort off;
// a hidden control is neither read from nor written to the URL, so it never
// clashes with the page's own parameters (the Timeline's ?camera_id=).
function libraryControls(options = {}) {
  return {
    range: options.showRange !== false,
    camera: options.showCamera !== false,
    sort: options.showSort !== false,
  };
}

// Parse the URL query string into a filter state. Accepts the historical deep
// links (?label=, ?camera_id=, ?face=) that other pages already emit.
function libraryStateFromSearch(search, types, controls = libraryControls()) {
  const params = new URLSearchParams(search || '');
  const state = libraryDefaultState();
  if (!controls.range) params.delete('range');
  const ranges = new Set(LIBRARY_RANGES.map((range) => range.value));
  if (params.get('q')) state.q = params.get('q').slice(0, 300);
  if (ranges.has(params.get('range'))) state.range = params.get('range');
  const splitStamp = (value) => {
    const match = String(value || '').match(/^(\d{4}-\d{2}-\d{2})(?:T(\d{2}:\d{2}))?$/);
    return match ? { date: match[1], time: match[2] || '' } : null;
  };
  const from = controls.range ? splitStamp(params.get('from')) : null;
  const to = controls.range ? splitStamp(params.get('to')) : null;
  if (from || to) {
    state.range = 'custom';
    if (from) { state.dateFrom = from.date; state.timeFrom = from.time || LIBRARY_TIME_FROM_DEFAULT; }
    if (to) { state.dateTo = to.date; state.timeTo = to.time || LIBRARY_TIME_TO_DEFAULT; }
  }
  if (types.includes(params.get('type'))) state.type = params.get('type');
  if (controls.camera && params.get('camera_id')) state.camera = params.get('camera_id');
  if (params.get('label')) state.label = params.get('label').trim().toLowerCase();
  if (params.get('face')) state.face = params.get('face');
  if (params.get('alerted') === '1') state.alerted = true;
  if (controls.sort && params.get('sort') === 'oldest') state.sort = 'oldest';
  return state;
}

// Write the non-default parts of a state back into a query string, keeping
// any unrelated parameters (e.g. /recordings?recording_id=) intact.
function librarySearchFromState(state, currentSearch, controls = libraryControls()) {
  const params = new URLSearchParams(currentSearch || '');
  const owned = ['q', 'type', 'label', 'face', 'alerted'];
  if (controls.range) owned.push('range', 'from', 'to');
  if (controls.camera) owned.push('camera_id');
  if (controls.sort) owned.push('sort');
  owned.forEach((key) => params.delete(key));
  if (state.q) params.set('q', state.q);
  if (!controls.range) {
    // The page owns its own time window.
  } else if (state.range === 'custom') {
    if (state.dateFrom) params.set('from', `${state.dateFrom}T${state.timeFrom || LIBRARY_TIME_FROM_DEFAULT}`);
    if (state.dateTo) params.set('to', `${state.dateTo}T${state.timeTo || LIBRARY_TIME_TO_DEFAULT}`);
    if (!state.dateFrom && !state.dateTo) params.set('range', 'custom');
  } else if (state.range !== 'today') {
    params.set('range', state.range);
  }
  if (state.type && state.type !== 'all') params.set('type', state.type);
  if (controls.camera && state.camera) params.set('camera_id', state.camera);
  if (state.label) params.set('label', state.label);
  if (state.face) params.set('face', state.face);
  if (state.alerted) params.set('alerted', '1');
  if (controls.sort && state.sort === 'oldest') params.set('sort', 'oldest');
  const text = params.toString();
  return text ? `?${text}` : '';
}

// eslint-disable-next-line no-unused-vars -- ESLint: exported for later scripts (events/recordings/snapshots)
function createLibraryFilters(options) {
  const mount = options.mount;
  const kind = options.kind; // 'events' | 'recordings' | 'snapshots'
  const noun = options.noun || 'items';
  const types = options.types || ['all', 'object', 'motion', 'sound'];
  const extraLabels = options.extraLabels || [];
  const onChange = typeof options.onChange === 'function' ? options.onChange : () => {};
  const onAiSearch = typeof options.onAiSearch === 'function' ? options.onAiSearch : null;
  const controls = libraryControls(options);
  // 'api' fetches /api/library/facets for the window; 'manual' leaves the
  // page to call controller.setFacets() with options it computed itself.
  const facetsFromApi = options.facets !== 'manual';
  const cameraNames = new Map();
  let state = libraryStateFromSearch(window.location?.search || '', types, controls);
  let searchTimer = null;
  let facetsSession = 0;
  let facets = { labels: [], faces: { people: [], unknown: 0 } };
  const ids = {
    search: `${kind}-filter-search`,
    panel: `${kind}-filter-panel`,
    camera: `${kind}-filter-camera`,
    label: `${kind}-filter-label`,
    face: `${kind}-filter-face`,
    sort: `${kind}-filter-sort`,
    alerted: `${kind}-filter-alerted`,
    dateFrom: `${kind}-filter-date-from`,
    dateTo: `${kind}-filter-date-to`,
  };

  const searchIcon = '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="11" cy="11" r="8"/><line x1="21" y1="21" x2="16.65" y2="16.65"/></svg>';
  const filterIcon = '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><polygon points="22 3 2 3 10 12.46 10 19 14 21 14 12.46 22 3"/></svg>';
  const caretIcon = '<svg class="library-filter-caret" width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><polyline points="6 9 12 15 18 9"/></svg>';
  const sparkIcon = '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M12 3l1.9 5.8L20 11l-6.1 2.2L12 19l-1.9-5.8L4 11l6.1-2.2z"/></svg>';

  mount.classList.add('library-filters');
  mount.innerHTML = `
    <form class="library-search" role="search" autocomplete="off">
      <label class="library-search-field" for="${ids.search}">
        ${searchIcon}
        <input id="${ids.search}" type="search" maxlength="300" placeholder="${escapeHtml(options.searchPlaceholder || 'Search labels, cameras, zones, faces or AI descriptions')}" aria-label="Search ${escapeHtml(noun)}" />
      </label>
      <button type="submit" data-library-search>Search</button>
      ${onAiSearch ? `<button type="button" class="secondary library-ai-btn" data-library-ai title="Ask the AI model in plain English, e.g. red car in the driveway yesterday afternoon">${sparkIcon}<span>Ask AI</span></button>` : ''}
    </form>
    <div class="library-filter-toolbar">
      <div class="library-segment" role="group" aria-label="Time range"${controls.range ? '' : ' hidden'}>
        ${LIBRARY_RANGES.map((range) => `<button type="button" class="library-segment-btn" data-library-range="${range.value}" aria-pressed="false">${escapeHtml(range.label)}</button>`).join('')}
      </div>
      ${types.length > 1 ? `<div class="library-segment library-type-segment" role="group" aria-label="Type">
        ${types.map((type) => `<button type="button" class="library-segment-btn" data-library-type="${type}" aria-pressed="false">${LIBRARY_TYPE_ICONS[type] || ''}<span>${escapeHtml(LIBRARY_TYPE_LABELS[type] || titleCase(type))}</span></button>`).join('')}
      </div>` : ''}
      <button type="button" class="secondary library-more-toggle" data-library-more aria-expanded="false" aria-controls="${ids.panel}">
        ${filterIcon}<span>Filters</span><span class="library-filter-badge" data-library-badge hidden>0</span>${caretIcon}
      </button>
    </div>
    <div class="library-custom-range" data-library-custom hidden>
      <label><span>From</span><span class="library-datetime"><input id="${ids.dateFrom}" type="date" aria-label="From date" /><span data-library-time="from"></span></span></label>
      <label><span>To</span><span class="library-datetime"><input id="${ids.dateTo}" type="date" aria-label="To date" /><span data-library-time="to"></span></span></label>
    </div>
    <div class="library-filter-panel" id="${ids.panel}" hidden>
      <label${controls.camera ? '' : ' hidden'}><span>Camera</span><select id="${ids.camera}"><option value="">All cameras</option></select></label>
      <label><span>Label</span><select id="${ids.label}"><option value="">All labels</option></select></label>
      <label data-library-face-field hidden><span>Face</span><select id="${ids.face}"><option value="">All faces</option></select></label>
      <label${controls.sort ? '' : ' hidden'}><span>Sort</span><select id="${ids.sort}"><option value="newest">Newest first</option><option value="oldest">Oldest first</option></select></label>
      <label class="library-check"><input id="${ids.alerted}" type="checkbox" /><span>Alert Only</span></label>
    </div>
    <div class="library-filter-summary">
      <span class="library-result-count" data-library-count aria-live="polite"></span>
      <div class="library-chips" data-library-chips></div>
      <button type="button" class="library-clear-all" data-library-clear hidden>Clear all</button>
    </div>
    <p class="muted library-search-note" data-library-note hidden></p>`;

  const q = (selector) => mount.querySelector(selector);
  const els = {
    form: q('.library-search'),
    search: q(`#${ids.search}`),
    ai: q('[data-library-ai]'),
    ranges: Array.from(mount.querySelectorAll('[data-library-range]')),
    types: Array.from(mount.querySelectorAll('[data-library-type]')),
    more: q('[data-library-more]'),
    badge: q('[data-library-badge]'),
    custom: q('[data-library-custom]'),
    dateFrom: q(`#${ids.dateFrom}`),
    dateTo: q(`#${ids.dateTo}`),
    timeFromMount: q('[data-library-time="from"]'),
    timeToMount: q('[data-library-time="to"]'),
    timeFrom: null,
    timeTo: null,
    panel: q(`#${ids.panel}`),
    camera: q(`#${ids.camera}`),
    label: q(`#${ids.label}`),
    face: q(`#${ids.face}`),
    faceField: q('[data-library-face-field]'),
    sort: q(`#${ids.sort}`),
    alerted: q(`#${ids.alerted}`),
    count: q('[data-library-count]'),
    chips: q('[data-library-chips]'),
    clear: q('[data-library-clear]'),
    note: q('[data-library-note]'),
  };

  function renderTimePickers() {
    els.timeFromMount.innerHTML = renderTimeSelect(state.timeFrom || LIBRARY_TIME_FROM_DEFAULT, 'data-library-time-role', 'from');
    els.timeToMount.innerHTML = renderTimeSelect(state.timeTo || LIBRARY_TIME_TO_DEFAULT, 'data-library-time-role', 'to');
    els.timeFrom = els.timeFromMount.querySelector('.time-select-wrap');
    els.timeTo = els.timeToMount.querySelector('.time-select-wrap');
    els.timeFrom.querySelectorAll('select').forEach((select) => select.addEventListener('change', readCustomRange));
    els.timeTo.querySelectorAll('select').forEach((select) => select.addEventListener('change', readCustomRange));
  }

  function setPanelOpen(open, persist = true) {
    els.panel.hidden = !open;
    els.more.setAttribute('aria-expanded', String(open));
    if (!persist) return;
    try { localStorage.setItem(LIBRARY_FILTER_PANEL_KEY, open ? '1' : '0'); } catch (_err) { /* storage disabled - keep default */ }
  }

  // Filters that live inside the collapsible panel, counted on its button so a
  // collapsed panel never hides that the list is narrowed.
  function panelFilterCount() {
    return [state.camera, state.label, state.face, state.alerted, state.sort === 'oldest'].filter(Boolean).length;
  }

  function optionText(select, value) {
    const option = Array.from(select.options).find((item) => item.value === value);
    return option ? option.textContent.replace(/\s*\(\d+\)$/, '').replace(/\s*\(none in range\)$/, '') : value;
  }

  function cameraText(id) {
    return cameraNames.get(id) || id;
  }

  function faceText(value) {
    if (value === 'any') return 'Any face';
    if (value === 'unknown') return 'Unknown';
    return optionText(els.face, value).replace(/^name:/, '');
  }

  function rangeText() {
    if (state.range !== 'custom') return '';
    const from = state.dateFrom ? `${formatUserDate(state.dateFrom)} ${state.timeFrom}` : '';
    const to = state.dateTo ? `${formatUserDate(state.dateTo)} ${state.timeTo}` : '';
    if (from && to) return `${from} – ${to}`;
    if (from) return `From ${from}`;
    if (to) return `Until ${to}`;
    return '';
  }

  // One removable chip per active non-default filter.
  function activeChips() {
    const chips = [];
    if (state.q) chips.push({ key: 'q', text: `“${state.q}”` });
    const range = rangeText();
    if (range) chips.push({ key: 'range', text: range });
    if (state.type !== 'all') chips.push({ key: 'type', text: LIBRARY_TYPE_LABELS[state.type] || state.type });
    if (state.camera) chips.push({ key: 'camera', text: `Camera: ${cameraText(state.camera)}` });
    if (state.label) chips.push({ key: 'label', text: `Label: ${optionText(els.label, state.label) || titleCase(state.label)}` });
    if (state.face) chips.push({ key: 'face', text: `Face: ${faceText(state.face)}` });
    if (state.alerted) chips.push({ key: 'alerted', text: 'Alert Only' });
    if (state.sort === 'oldest') chips.push({ key: 'sort', text: 'Oldest first' });
    return chips;
  }

  function syncControls() {
    els.search.value = state.q;
    els.ranges.forEach((button) => {
      const active = button.dataset.libraryRange === state.range;
      button.classList.toggle('active', active);
      button.setAttribute('aria-pressed', String(active));
    });
    els.types.forEach((button) => {
      const active = button.dataset.libraryType === state.type;
      button.classList.toggle('active', active);
      button.setAttribute('aria-pressed', String(active));
    });
    els.custom.hidden = !controls.range || state.range !== 'custom';
    els.dateFrom.value = state.dateFrom;
    els.dateTo.value = state.dateTo;
    if (els.timeFrom) setTimeSelectValue(els.timeFrom, state.timeFrom || LIBRARY_TIME_FROM_DEFAULT);
    if (els.timeTo) setTimeSelectValue(els.timeTo, state.timeTo || LIBRARY_TIME_TO_DEFAULT);
    els.camera.value = state.camera;
    els.label.value = state.label;
    els.face.value = state.face;
    els.sort.value = state.sort;
    els.alerted.checked = state.alerted;
    const count = panelFilterCount();
    els.badge.textContent = String(count);
    els.badge.hidden = !count;
    els.more.classList.toggle('is-filtered', count > 0);
    els.more.setAttribute('aria-label', count ? `Filters, ${count} active` : 'Filters');
    const chips = activeChips();
    els.chips.innerHTML = chips.map((chip) => (
      `<button type="button" class="library-chip" data-library-chip="${chip.key}" aria-label="Remove filter ${escapeHtml(chip.text)}"><span>${escapeHtml(chip.text)}</span><span aria-hidden="true">×</span></button>`
    )).join('');
    els.clear.hidden = !chips.length && state.range === 'today';
  }

  function commit(reason) {
    syncControls();
    try {
      const search = librarySearchFromState(state, window.location.search, controls);
      if (search !== window.location.search) {
        history.replaceState(history.state, '', `${window.location.pathname}${search}${window.location.hash || ''}`);
      }
    } catch (_err) { /* history unavailable (sandboxed frame) - URL sync is a convenience */ }
    if (reason === 'range' && facetsFromApi) loadFacets();
    onChange(controller.query(), reason);
  }

  function update(patch, reason) {
    const next = { ...state, ...patch };
    if (JSON.stringify(next) === JSON.stringify(state)) return;
    state = next;
    commit(reason);
  }

  function readCustomRange() {
    update({
      dateFrom: els.dateFrom.value || '',
      dateTo: els.dateTo.value || '',
      timeFrom: (els.timeFrom && timeSelectValue(els.timeFrom)) || LIBRARY_TIME_FROM_DEFAULT,
      timeTo: (els.timeTo && timeSelectValue(els.timeTo)) || LIBRARY_TIME_TO_DEFAULT,
    }, 'range');
  }

  function fillSelect(select, items, emptyLabel, current) {
    const html = [`<option value="">${escapeHtml(emptyLabel)}</option>`];
    const values = new Set(['']);
    items.forEach((item) => {
      if (values.has(item.value)) return;
      values.add(item.value);
      html.push(`<option value="${escapeHtml(item.value)}">${escapeHtml(item.label)}</option>`);
    });
    // Keep a deep-linked or previously picked value selectable even when the
    // new window holds none of it, so the dropdown never silently drops it.
    if (current && !values.has(current)) {
      html.push(`<option value="${escapeHtml(current)}">${escapeHtml(titleCase(current.replace(/^(id|name):/, '')))} (none in range)</option>`);
    }
    select.innerHTML = html.join('');
    select.value = current || '';
  }

  function renderFacetOptions() {
    const labelItems = [];
    const seen = new Set();
    extraLabels.forEach((item) => {
      seen.add(item.value);
      labelItems.push(item);
    });
    (facets.labels || []).forEach((item) => {
      const value = String(item.value || '').trim().toLowerCase();
      if (!value || seen.has(value)) return;
      seen.add(value);
      labelItems.push({ value, label: `${titleCase(value)}${item.ai ? ' (AI tag)' : ''} (${item.count})`, sortKey: titleCase(value) });
    });
    const head = labelItems.filter((item) => !item.sortKey);
    const rest = labelItems.filter((item) => item.sortKey).sort((a, b) => a.sortKey.localeCompare(b.sortKey));
    fillSelect(els.label, [...head, ...rest], 'All labels', state.label);
    const faces = facets.faces || { people: [], unknown: 0 };
    const people = faces.people || [];
    const hasFaces = people.length > 0 || faces.unknown > 0 || Boolean(state.face);
    els.faceField.hidden = !hasFaces;
    const faceItems = [{ value: 'any', label: 'Any face' }, ...people.map((person) => ({ value: person.value, label: `${person.name} (${person.count})` }))];
    if (faces.unknown > 0) faceItems.push({ value: 'unknown', label: `Unknown (${faces.unknown})` });
    fillSelect(els.face, faceItems, 'All faces', state.face);
    syncControls();
  }

  async function loadFacets() {
    facetsSession += 1;
    const session = facetsSession;
    const { since, until } = libraryFilterWindow(state);
    const params = new URLSearchParams({ kind });
    if (since) params.set('since', since);
    if (until) params.set('until', until);
    try {
      const result = await api(`/api/library/facets?${params}`);
      if (session !== facetsSession) return;
      facets = result || facets;
    } catch (_err) {
      // Facets only feed the dropdowns; the list itself still loads.
      if (session !== facetsSession) return;
    }
    renderFacetOptions();
  }

  async function loadCameras() {
    try {
      const data = await api('/api/cameras');
      (data?.cameras || []).forEach((camera) => {
        const id = String(camera.id || '').trim();
        if (id) cameraNames.set(id, camera.name || id);
      });
    } catch (_err) { /* camera names are cosmetic; ids still filter */ }
    const items = Array.from(cameraNames.entries()).map(([value, label]) => ({ value, label }));
    fillSelect(els.camera, items, 'All cameras', state.camera);
    syncControls();
  }

  // ── Wiring ──────────────────────────────────────────────────────────────
  els.form.addEventListener('submit', (event) => {
    event.preventDefault();
    clearTimeout(searchTimer);
    update({ q: els.search.value.trim() }, 'search');
  });
  els.search.addEventListener('input', () => {
    clearTimeout(searchTimer);
    searchTimer = setTimeout(() => update({ q: els.search.value.trim() }, 'search'), LIBRARY_SEARCH_DEBOUNCE_MS);
  });
  els.ai?.addEventListener('click', () => {
    const text = els.search.value.trim();
    if (!text) {
      els.search.focus();
      return;
    }
    clearTimeout(searchTimer);
    onAiSearch(text);
  });
  els.ranges.forEach((button) => button.addEventListener('click', () => {
    const range = button.dataset.libraryRange;
    const patch = { range };
    // Opening Custom pre-fills today so the two date fields start usable.
    if (range === 'custom' && !state.dateFrom && !state.dateTo) {
      const today = libraryLocalDateString(new Date());
      patch.dateFrom = today;
      patch.dateTo = today;
    }
    update(patch, 'range');
  }));
  els.types.forEach((button) => button.addEventListener('click', () => update({ type: button.dataset.libraryType }, 'type')));
  els.dateFrom.addEventListener('change', readCustomRange);
  els.dateTo.addEventListener('change', readCustomRange);
  els.camera.addEventListener('change', () => update({ camera: els.camera.value }, 'filter'));
  els.label.addEventListener('change', () => update({ label: els.label.value }, 'filter'));
  els.face.addEventListener('change', () => update({ face: els.face.value }, 'filter'));
  els.sort.addEventListener('change', () => update({ sort: els.sort.value }, 'sort'));
  els.alerted.addEventListener('change', () => update({ alerted: els.alerted.checked }, 'filter'));
  els.more.addEventListener('click', () => {
    const opening = els.panel.hidden;
    setPanelOpen(opening);
    if (opening) els.camera.focus();
  });
  els.chips.addEventListener('click', (event) => {
    const chip = event.target.closest('[data-library-chip]');
    if (!chip) return;
    const defaults = libraryDefaultState();
    const key = chip.dataset.libraryChip;
    if (key === 'range') {
      update({ range: 'today', dateFrom: '', dateTo: '', timeFrom: defaults.timeFrom, timeTo: defaults.timeTo }, 'range');
    } else {
      update({ [key]: defaults[key] }, key === 'q' ? 'search' : 'filter');
    }
  });
  els.clear.addEventListener('click', () => {
    const rangeChanged = state.range !== 'today';
    state = libraryDefaultState();
    commit(rangeChanged ? 'range' : 'filter');
  });

  const controller = {
    // The page-facing view of the state: the time window resolved to ISO
    // bounds plus every filter, named after the API parameters.
    query() { return libraryQueryForState(state); },
    state() { return { ...state }; },
    cameraName(id) { return cameraNames.get(String(id || '')) || ''; },
    cameraNames,
    // "123 events" etc. - the page owns the wording because only it knows
    // whether more pages are still to come.
    setCount(text) { els.count.textContent = text || ''; },
    // A one-line note under the bar (the AI search interpretation, errors).
    setNote(text) {
      els.note.textContent = text || '';
      els.note.hidden = !text;
    },
    // Re-render the time pickers when Profile > Time Format changes.
    refreshTimePickers() {
      renderTimePickers();
      syncControls();
    },
    reloadFacets: loadFacets,
    // For facets: 'manual' pages - { labels: [{value, count, ai}],
    // faces: { people: [{value, name, count}], unknown } }.
    setFacets(next) {
      facets = next || { labels: [], faces: { people: [], unknown: 0 } };
      renderFacetOptions();
    },
    ready: null,
  };

  renderTimePickers();
  let savedOpen = null;
  try { savedOpen = localStorage.getItem(LIBRARY_FILTER_PANEL_KEY); } catch (_err) { /* storage disabled - keep default */ }
  setPanelOpen(savedOpen === '1' || panelFilterCount() > 0, false);
  syncControls();
  controller.ready = Promise.all([
    controls.camera ? loadCameras() : Promise.resolve(),
    facetsFromApi ? loadFacets() : Promise.resolve(renderFacetOptions()),
  ]);
  return controller;
}
