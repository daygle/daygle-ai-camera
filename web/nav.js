// window.showToast() is now provided by web/utils.js, which every page loads
// after nav.js but before the page's own script. Keeping the
// DAYGLE_BUTTON_ICONS / icon decorator below here because they only make
// sense in the context of the rendered nav bar.

const DAYGLE_BUTTON_ICONS = {
  add: '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" aria-hidden="true"><line x1="12" y1="5" x2="12" y2="19"/><line x1="5" y1="12" x2="19" y2="12"/></svg>',
  apply: '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.25" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M20 6 9 17l-5-5"/></svg>',
  arrowLeft: '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.25" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="m15 18-6-6 6-6"/></svg>',
  arrowRight: '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.25" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="m9 18 6-6-6-6"/></svg>',
  bell: '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.25" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M6 8a6 6 0 0 1 12 0c0 7 3 9 3 9H3s3-2 3-9"/><path d="M10.3 21a1.94 1.94 0 0 0 3.4 0"/></svg>',
  checkCircle: '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.25" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M22 11.1V12a10 10 0 1 1-5.9-9.1"/><path d="m9 11 3 3L22 4"/></svg>',
  clock: '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.25" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="12" cy="12" r="10"/><path d="M12 6v6l4 2"/></svg>',
  close: '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" aria-hidden="true"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>',
  download: '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.25" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><path d="M7 10l5 5 5-5"/><path d="M12 15V3"/></svg>',
  edit: '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.25" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M12 20h9"/><path d="M16.5 3.5a2.1 2.1 0 0 1 3 3L7 19l-4 1 1-4 12.5-12.5z"/></svg>',
  filter: '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.25" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M22 3H2l8 9.5V20l4 2v-9.5L22 3z"/></svg>',
  key: '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.25" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="7.5" cy="15.5" r="5.5"/><path d="m12 11 8-8"/><path d="m16 7 3 3"/><path d="m18 5 3 3"/></svg>',
  logout: '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.25" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4"/><path d="m16 17 5-5-5-5"/><path d="M21 12H9"/></svg>',
  power: '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.25" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M12 2v10"/><path d="M18.4 6.6a9 9 0 1 1-12.8 0"/></svg>',
  refresh: '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.25" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M21 12a9 9 0 0 1-15.5 6.2"/><path d="M3 12A9 9 0 0 1 18.5 5.8"/><path d="M18.5 2v4h-4"/><path d="M5.5 22v-4h4"/></svg>',
  reset: '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.25" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M3 12a9 9 0 1 0 3-6.7"/><path d="M3 4v6h6"/></svg>',
  restore: '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.25" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M3 12a9 9 0 1 0 3-6.7"/><path d="M3 4v6h6"/><path d="M12 7v5l3 2"/></svg>',
  save: '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.25" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M19 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h11l5 5v11a2 2 0 0 1-2 2z"/><path d="M17 21v-8H7v8"/><path d="M7 3v5h8"/></svg>',
  search: '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.25" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="11" cy="11" r="8"/><path d="m21 21-4.3-4.3"/></svg>',
  shield: '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.25" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z"/></svg>',
  spark: '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.25" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M13 2 3 14h8l-1 8 11-14h-8l1-6z"/></svg>',
  trash: '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.25" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><polyline points="3 6 5 6 21 6"/><path d="M19 6l-2 14a2 2 0 0 1-2 2H9a2 2 0 0 1-2-2L5 6"/><path d="M10 11v6M14 11v6"/><path d="M9 6V4a1 1 0 0 1 1-1h4a1 1 0 0 1 1 1v2"/></svg>',
  upload: '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.25" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><path d="M17 8l-5-5-5 5"/><path d="M12 3v12"/></svg>',
  user: '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.25" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M20 21a8 8 0 0 0-16 0"/><circle cx="12" cy="7" r="4"/></svg>',
  link: '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.25" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M10 13a5 5 0 0 0 7.54.54l3-3a5 5 0 0 0-7.07-7.07l-1.72 1.71"/><path d="M14 11a5 5 0 0 0-7.54-.54l-3 3a5 5 0 0 0 7.07 7.07l1.71-1.71"/></svg>',
  move: '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.25" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><polyline points="5 9 2 12 5 15"/><polyline points="9 5 12 2 15 5"/><polyline points="15 19 12 22 9 19"/><polyline points="19 9 22 12 19 15"/><line x1="2" y1="12" x2="22" y2="12"/><line x1="12" y1="2" x2="12" y2="22"/></svg>',
  video: '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.25" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M23 7l-7 5 7 5V7z"/><rect x="1" y="5" width="15" height="14" rx="2" ry="2"/></svg>',
};

const DAYGLE_BUTTON_ICON_BY_LABEL = new Map([
  ['add camera', DAYGLE_BUTTON_ICONS.add],
  ['apply', DAYGLE_BUTTON_ICONS.apply],
  ['apply filters', DAYGLE_BUTTON_ICONS.filter],
  ['apply update', DAYGLE_BUTTON_ICONS.upload],
  ['advanced', DAYGLE_BUTTON_ICONS.shield],
  ['alerts', DAYGLE_BUTTON_ICONS.bell],
  ['all', DAYGLE_BUTTON_ICONS.checkCircle],
  ['cancel', DAYGLE_BUTTON_ICONS.close],
  ['change password', DAYGLE_BUTTON_ICONS.key],
  ['check for updates', DAYGLE_BUTTON_ICONS.search],
  ['check model', DAYGLE_BUTTON_ICONS.search],
  ['clear', DAYGLE_BUTTON_ICONS.reset],
  ['connection', DAYGLE_BUTTON_ICONS.link],
  ['create user', DAYGLE_BUTTON_ICONS.user],
  ['detections', DAYGLE_BUTTON_ICONS.search],
  ['disable', DAYGLE_BUTTON_ICONS.power],
  ['download', DAYGLE_BUTTON_ICONS.download],
  ['download & install', DAYGLE_BUTTON_ICONS.download],
  ['download database backup', DAYGLE_BUTTON_ICONS.download],
  ['edit', DAYGLE_BUTTON_ICONS.edit],
  ['enable', DAYGLE_BUTTON_ICONS.power],
  ['in use', DAYGLE_BUTTON_ICONS.checkCircle],
  ['logout', DAYGLE_BUTTON_ICONS.logout],
  ['next', DAYGLE_BUTTON_ICONS.arrowRight],
  ['previous', DAYGLE_BUTTON_ICONS.arrowLeft],
  ['ptz', DAYGLE_BUTTON_ICONS.move],
  ['recording', DAYGLE_BUTTON_ICONS.video],
  ['refresh', DAYGLE_BUTTON_ICONS.refresh],
  ['reload detector', DAYGLE_BUTTON_ICONS.refresh],
  ['remove', DAYGLE_BUTTON_ICONS.trash],
  ['remove camera', DAYGLE_BUTTON_ICONS.trash],
  ['reset filters', DAYGLE_BUTTON_ICONS.reset],
  ['reset password', DAYGLE_BUTTON_ICONS.key],
  ['restore database', DAYGLE_BUTTON_ICONS.restore],
  ['run purge now', DAYGLE_BUTTON_ICONS.trash],
  ['save camera', DAYGLE_BUTTON_ICONS.save],
  ['save clip settings', DAYGLE_BUTTON_ICONS.save],
  ['save live settings', DAYGLE_BUTTON_ICONS.save],
  ['save login security', DAYGLE_BUTTON_ICONS.shield],
  ['save mail server', DAYGLE_BUTTON_ICONS.save],
  ['save offline alert settings', DAYGLE_BUTTON_ICONS.bell],
  ['save onnx settings', DAYGLE_BUTTON_ICONS.save],
  ['save profile', DAYGLE_BUTTON_ICONS.save],
  ['save push settings', DAYGLE_BUTTON_ICONS.bell],
  ['save retention', DAYGLE_BUTTON_ICONS.save],
  ['save sounds', DAYGLE_BUTTON_ICONS.save],
  ['save storage', DAYGLE_BUTTON_ICONS.save],
  ['send test email', DAYGLE_BUTTON_ICONS.bell],
  ['send test notification', DAYGLE_BUTTON_ICONS.bell],
  ['start clean', DAYGLE_BUTTON_ICONS.trash],
  ['test detector', DAYGLE_BUTTON_ICONS.spark],
  ['update', DAYGLE_BUTTON_ICONS.upload],
  ['use', DAYGLE_BUTTON_ICONS.checkCircle],
]);

function normalizedButtonLabel(button) {
  return String(button.textContent || '')
    .replace(/^\+\s*/, '')
    .replace(/[×✕]/g, 'close')
    .replace(/\s+/g, ' ')
    .trim()
    .toLowerCase();
}

function decorateDaygleButton(control) {
  if (!(control instanceof HTMLElement)) return;
  if (control.dataset.iconDecorated === 'true') return;
  if (control.querySelector('svg, img')) {
    control.dataset.iconDecorated = 'true';
    return;
  }
  if (control.classList.contains('app-nav-toggle') || control.classList.contains('nav-dropdown-trigger')) return;
  const label = normalizedButtonLabel(control);
  const icon = control.classList.contains('modal-close')
    ? DAYGLE_BUTTON_ICONS.close
    : DAYGLE_BUTTON_ICON_BY_LABEL.get(label);
  if (!icon) return;
  if (control.classList.contains('modal-close')) control.textContent = '';
  control.insertAdjacentHTML('afterbegin', icon);
  control.classList.add('daygle-icon-button');
  control.dataset.iconDecorated = 'true';
}

function decorateDaygleButtons(root = document) {
  root.querySelectorAll?.('button, a.secondary, a.button-link').forEach(decorateDaygleButton);
}

function startDaygleButtonIconDecorator() {
  decorateDaygleButtons();
  const observer = new MutationObserver((mutations) => {
    for (const mutation of mutations) {
      for (const node of mutation.addedNodes) {
        if (!(node instanceof HTMLElement)) continue;
        if (node.matches?.('button, a.secondary, a.button-link')) decorateDaygleButton(node);
        decorateDaygleButtons(node);
      }
    }
  });
  observer.observe(document.body, { childList: true, subtree: true });
}

if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', startDaygleButtonIconDecorator, { once: true });
} else {
  startDaygleButtonIconDecorator();
}

// ─── Single /api/auth/me fetch shared with every page ──────────────────
// Kicked off synchronously at script-load time and exposed as a promise so
// every page's loadAuth() can simply `await window.daygleAuthReady` instead
// of issuing its own redundant /api/auth/me. By the time the inner `await`
// resumes, utils.js (loaded after nav.js) has populated setApiAuth() and
// window.daygleAuth, so subsequent api() calls see the CSRF token.
//
// The try/catch / !response.ok guards leave window.daygleAuth empty when the
// user is unauthenticated - every page bundle already handles that case.
window.daygleAuthReady = (async () => {
  try {
    const response = await fetch('/api/auth/me');
    if (!response.ok) return null;
    const payload = await response.json();
    const user = payload.user || {};
    const csrfToken = payload.csrf_token || '';
    const expiresAt = payload.expires_at || '';
    // Pipe into the shared holder. By the time we reach here the fetch has
    // resolved, so utils.js (which loads synchronously between nav.js and
    // the page's JS) has registered setApiAuth and the window.daygleUi
    // registry. setApiAuth() is the canonical writer of window.daygleAuth -
    // this single call covers the user + csrfToken + expiresAt trio, so
    // there's no redundant direct assignment below it.
    if (typeof setApiAuth === 'function') {
      setApiAuth(user, csrfToken, expiresAt);
    }
    // Propagate display preferences so utils.formatDate honours the
    // user's chosen date_format / time_format on every page (dashboard,
    // events, alerts, recordings, etc.) - not just the ones that already
    // implemented their own local formatters.
    if (typeof window.setDaygleDatePrefs === 'function') {
      window.setDaygleDatePrefs({
        date_format: user.date_format || 'locale',
        time_format: user.time_format || '24h',
      });
    }
    // scheduleNextAuthRefresh is idempotent and respects the cached token -
    // safe to call here even on the initial load. Pairs with the lazy
    // renewal in app/auth.py::get_session so the server-side row rotates
    // on the very same cadence as the client-side refresh timer.
    if (typeof window.scheduleNextAuthRefresh === 'function') {
      window.scheduleNextAuthRefresh();
    }
    // Tell other Tabs of the same user about the new CSRF token. Without
    // this, opening Tab B after Tab A logged in would leave Tab B showing
    // a stale user's avatar (or "?" if its own session expired) until its
    // scheduled refresh fires.
    if (typeof window.broadcastAuthStateToOtherTabs === 'function' && user && csrfToken) {
      window.broadcastAuthStateToOtherTabs(user, csrfToken, expiresAt);
    }
    return { user, csrfToken, expiresAt };
  } catch {
    return null;
  }
})();

(async function () {
  if (document.querySelector('.app-nav')) return;
  const currentPath = window.location.pathname;
  const nav = document.createElement('nav');
  nav.className = 'app-nav';

  /* ── Helper: detect if any link inside a dropdown matches the current path ── */
  function dropdownIsActive(links) {
    return links.some((l) => {
      const m = l.match || '';
      return (m === '/' && currentPath === '/') || (m !== '/' && currentPath.startsWith(m));
    });
  }

  /* ── Define nav structure ── */
  const primaryLinks = [
    { href: '/', match: '/', label: 'Dashboard' },
    { href: '/alerts', match: '/alerts', label: 'Alerts' },
    { href: '/live', match: '/live', label: 'Live' },
  ];

  const dropdowns = [    { id: 'navMonitor',
      label: 'Monitoring',
      admin: true,
      links: [
        { href: '/cameras', match: '/cameras', label: 'Cameras' },
        { href: '/zones', match: '/zones', label: 'Zones' },
        { href: '/sounds', match: '/sounds', label: 'Sounds' },
      ],
    },
    {
      id: 'navData',
      label: 'Data',
      admin: false,
      links: [
        { href: '/recordings', match: '/recordings', label: 'Recordings' },
        { href: '/recordings/timeline', match: '/recordings/timeline', label: 'Timeline' },
      ],
    },
    {
      id: 'navIntel',
      label: 'Intelligence',
      admin: true,
      links: [
        { href: '/onnx', match: '/onnx', label: 'ONNX' },
        { href: '/yamnet-tflite', match: '/yamnet-tflite', label: 'YAMNet TFLite' },
      ],
    },
    {
      id: 'navAdmin',
      label: 'Admin',
      admin: true,
      links: [
        { href: '/settings', match: '/settings', label: 'Settings' },
        { href: '/users', match: '/users', label: 'Users' },
        { href: '/camera-log', match: '/camera-log', label: 'Camera Log' },
        { href: '/application-log', match: '/application-log', label: 'Application Log' },
        { href: '/audit', match: '/audit', label: 'Audit Log' },
      ],
    },
  ];

  /* ── Determine active dropdown ── */
  function findActiveDropdown() {
    for (const dd of dropdowns) {
      if (dropdownIsActive(dd.links)) return dd.id;
    }
    return null;
  }
  const activeDropdownId = findActiveDropdown();

  /* ── Build HTML ── */
  let html = `
    <a class="app-brand" href="/">
      <span class="brand-mark">D</span>
      <span class="brand-text">Daygle AI Camera</span>
    </a>
    <button class="app-nav-toggle" type="button" aria-label="Toggle navigation">
      <svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round"><line x1="3" y1="6" x2="21" y2="6"/><line x1="3" y1="12" x2="21" y2="12"/><line x1="3" y1="18" x2="21" y2="18"/></svg>
    </button>
    <div class="app-nav-body">
      <div class="app-nav-links">`;

  /* Primary links */
  for (const link of primaryLinks) {
    const isActive =
      (link.match === '/' && currentPath === '/') ||
      (link.match !== '/' && currentPath.startsWith(link.match));
    html += `<a href="${link.href}" class="nav-item${isActive ? ' active' : ''}">${link.label}</a>`;
  }

  /* Dropdown groups */
  for (const dd of dropdowns) {
    const isActive = dd.id === activeDropdownId;
    const adminAttr = dd.admin ? ' data-admin="true"' : '';
    html += `
        <div class="nav-dropdown${isActive ? ' active' : ''}" data-dropdown="${dd.id}"${adminAttr}>
          <button type="button" class="nav-dropdown-trigger${isActive ? ' active' : ''}" aria-haspopup="true" aria-expanded="false">
            <span class="nav-dropdown-label">${dd.label}</span>
            <svg class="nav-dropdown-chevron" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><polyline points="6 9 12 15 18 9"/></svg>
          </button>
          <div class="nav-dropdown-menu">`;
    for (const link of dd.links) {
      const linkActive =
        (link.match === '/' && currentPath === '/') ||
        (link.match !== '/' && currentPath.startsWith(link.match));
      html += `<a href="${link.href}" class="nav-dropdown-item${linkActive ? ' active' : ''}">${link.label}</a>`;
    }
    html += `
          </div>
        </div>`;
  }

  html += `
      </div>
      <div class="app-nav-account">
        <div class="nav-dropdown" data-dropdown="account">
          <button type="button" class="nav-dropdown-trigger" aria-haspopup="true" aria-expanded="false">
            <span id="navAvatar" class="nav-avatar"></span>
            <span id="navUser" class="nav-dropdown-label">Profile</span>
            <svg class="nav-dropdown-chevron" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><polyline points="6 9 12 15 18 9"/></svg>
          </button>
          <div class="nav-dropdown-menu">
            <a href="/profile" class="nav-dropdown-item">Profile</a>
            <div style="border-top:1px solid var(--border);margin:4px 0;padding:6px 12px;font-size:11px;font-weight:800;color:var(--muted);text-transform:uppercase;letter-spacing:.06em">Theme</div>
            <button id="navThemeSystem" class="nav-dropdown-item" type="button">System</button>
            <button id="navThemeLight" class="nav-dropdown-item" type="button">Light</button>
            <button id="navThemeDark" class="nav-dropdown-item" type="button">Dark</button>
            <div id="sessionCountdown" class="nav-countdown" hidden></div>
            <button id="navLogoutBtn" class="nav-dropdown-item" type="button">Logout</button>
          </div>
        </div>
      </div>
    </div>`;

  nav.innerHTML = html;
  document.body.prepend(nav);

  /* ── Dropdown interaction ── */
  let openDropdown = null;

  function closeAllDropdowns() {
    nav.querySelectorAll('.nav-dropdown.open').forEach((el) => {
      el.classList.remove('open');
      const trigger = el.querySelector('.nav-dropdown-trigger');
      if (trigger) trigger.setAttribute('aria-expanded', 'false');
    });
    openDropdown = null;
  }

  nav.querySelectorAll('.nav-dropdown-trigger').forEach((trigger) => {
    trigger.addEventListener('click', (e) => {
      e.stopPropagation();
      const wrapper = trigger.closest('.nav-dropdown');
      const isOpen = wrapper.classList.contains('open');
      closeAllDropdowns();
      if (!isOpen) {
        wrapper.classList.add('open');
        trigger.setAttribute('aria-expanded', 'true');
        openDropdown = wrapper;
      }
    });
  });

  /* Close dropdowns on outside click */
  document.addEventListener('click', (e) => {
    if (openDropdown && !openDropdown.contains(e.target)) {
      closeAllDropdowns();
    }
  });

  /* Desktop: close dropdown on mouse-leave with small delay */
  nav.querySelectorAll('.nav-dropdown').forEach((wrapper) => {
    let leaveTimer = null;
    wrapper.addEventListener('mouseenter', () => {
      if (leaveTimer) { clearTimeout(leaveTimer); leaveTimer = null; }
    });
    wrapper.addEventListener('mouseleave', () => {
      if (!wrapper.classList.contains('open')) return;
      leaveTimer = setTimeout(() => {
        wrapper.classList.remove('open');
        const trigger = wrapper.querySelector('.nav-dropdown-trigger');
        if (trigger) trigger.setAttribute('aria-expanded', 'false');
        if (openDropdown === wrapper) openDropdown = null;
      }, 200);
    });
  });

  /* Close dropdowns on Escape */
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') closeAllDropdowns();
  });

  /* Close mobile menu when a link is clicked */
  nav.querySelectorAll('.nav-item, .nav-dropdown-item').forEach((link) => {
    link.addEventListener('click', () => {
      nav.classList.remove('nav-open');
    });
  });

  /* ── Mobile toggle ── */
  const toggle = nav.querySelector('.app-nav-toggle');
  if (toggle) {
    toggle.addEventListener('click', (e) => {
      e.stopPropagation();
      nav.classList.toggle('nav-open');
    });
  }

  /* Close mobile nav on outside click */
  document.addEventListener('click', (e) => {
    if (nav.classList.contains('nav-open') && !nav.contains(e.target)) {
      nav.classList.remove('nav-open');
    }
  });

  /* ── Auth ── */
  // The single source of truth for the account dropdown lives at the TOP
  // LEVEL of this file (``renderNavAccount`` below), exposed on
  // ``window.daygleUi`` so the cross-tab auth-state-changed listener and
  // the visibilitychange/focus refresh hooks below can all re-paint the
  // avatar without coupling to the IIFE's captured ``nav`` reference.
  // Resolving ``window.daygleUi?.renderNavAccount?.(...)`` keeps the call
  // future-proof if the listener fires before utils.js's setApiAuth has
  // had a chance to dispatch the very first event (e.g. on the login page
  // where the bootstrap IIFE in nav.js never paints an account dropdown).
  await window.daygleAuthReady;
  if (window.daygleUi?.renderNavAccount) {
    window.daygleUi.renderNavAccount(window.daygleAuth?.user || null);
  }

  // ── Apply theme from user profile ──────────────────────────────
  const userTheme = window.daygleAuth?.user?.theme || 'system';
  if (typeof window.setDaygleThemePref === 'function') {
    window.setDaygleThemePref(userTheme);
  }
  // If utils.js hasn't loaded yet (load order), poll briefly for setDaygleThemePref.
  // This handles the edge case where nav.js runs before utils.js on slow connections.
  setActiveThemeButton(userTheme);

  // ── Theme buttons ─────────────────────────────────────────────────
  function setActiveThemeButton(theme) {
    ['system', 'light', 'dark'].forEach((t) => {
      const btn = document.getElementById('navTheme' + t.charAt(0).toUpperCase() + t.slice(1));
      if (btn) btn.classList.toggle('active', t === theme);
    });
  }

  document.getElementById('navThemeSystem')?.addEventListener('click', () => {
    setActiveThemeButton('system');
    if (typeof window.setDaygleThemePref === 'function') window.setDaygleThemePref('system');
  });
  document.getElementById('navThemeLight')?.addEventListener('click', () => {
    setActiveThemeButton('light');
    if (typeof window.setDaygleThemePref === 'function') window.setDaygleThemePref('light');
  });
  document.getElementById('navThemeDark')?.addEventListener('click', () => {
    setActiveThemeButton('dark');
    if (typeof window.setDaygleThemePref === 'function') window.setDaygleThemePref('dark');
  });

  const logoutBtn = document.getElementById('navLogoutBtn');
  if (logoutBtn) {
    logoutBtn.addEventListener('click', async () => {
      try {
        const token = (window.daygleAuth && window.daygleAuth.csrfToken) || '';
        await fetch('/logout', { method: 'POST', headers: { 'X-CSRF-Token': token } });
      } catch {
        // Ignore network errors; the redirect below will clear the session server-side.
      }
      // Broadcast to other open tabs so they immediately know the session
      // is gone, rather than waiting for their next refresh cycle or API 401.
      if (typeof window.broadcastAuthStateToOtherTabs === 'function') {
        window.broadcastAuthStateToOtherTabs(null, '', '');
      }
      // Use window.defaultReturnTo defensively: by the time the user clicks
      // Logout utils.js has long loaded, but a future refactor that defers
      // the listener binding still has to work without us reading a bare
      // identifier that resolves through the IIFE's closure.
      const safeTo = window.defaultReturnTo ? window.defaultReturnTo() : '/';
      window.location.href = '/login?returnTo=' + encodeURIComponent(safeTo);
    });
  }

  /* ── Session countdown ticker ───────────────────────────────────────
   * Polls /api/auth/session-remaining every 15 s and updates the
   * countdown element in the account dropdown. Falls back to the
   * client-side expiresAt when the fetch fails (e.g. on a transitory
   * network blip). The threshold classes give the user early warning.
   *
   * Timer reference lives on window.daygleAuth._cdTimer so the guard in
   * renderNavAccount can detect whether the ticker is already running
   * when auth state changes (cross-tab sync, refresh, etc.).
   */
  const SESSION_WARN_SECONDS = 30 * 60;   // 30 min → yellow
  const SESSION_CRITICAL_SECONDS = 5 * 60;  //  5 min → red

  function formatCountdown(seconds) {
    if (seconds <= 0) return 'Expired';
    const h = Math.floor(seconds / 3600);
    const m = Math.floor((seconds % 3600) / 60);
    const s = seconds % 60;
    if (h > 0) return `${h}h ${m}m remaining`;
    if (m > 0) return `${m}m ${s}s remaining`;
    return `${s}s remaining`;
  }

  function updateCountdown() {
    const el = document.getElementById('sessionCountdown');
    if (!el) return;
    if (!window.daygleAuth?.user || !window.daygleAuth?.expiresAt) {
      el.hidden = true;
      return;
    }
    // Compute from local expiresAt as the fast path, then let the
    // next server fetch correct any clock drift.
    const ms = Date.parse(window.daygleAuth.expiresAt) - Date.now();
    if (!Number.isFinite(ms) || ms <= 0) {
      el.textContent = 'Expired';
      el.className = 'nav-countdown critical';
      el.hidden = false;
      return;
    }
    const totalSec = Math.ceil(ms / 1000);
    el.textContent = formatCountdown(totalSec);
    el.className = 'nav-countdown' +
      (totalSec <= SESSION_CRITICAL_SECONDS ? ' critical' :
       totalSec <= SESSION_WARN_SECONDS ? ' warn' :
       '');
    el.hidden = false;
  }

  async function refreshCountdownFromServer() {
    try {
      const response = await fetch('/api/auth/session-remaining');
      if (!response.ok) { updateCountdown(); return; }
      const data = await response.json();
      if (data && Number.isFinite(data.remaining_seconds)) {
        // Sync the cached expiresAt so the local ticker stays accurate.
        if (window.daygleAuth && data.expires_at) {
          window.daygleAuth.expiresAt = data.expires_at;
        }
        const el = document.getElementById('sessionCountdown');
        if (el) {
          el.textContent = formatCountdown(data.remaining_seconds);
          el.className = 'nav-countdown' +
            (data.remaining_seconds <= SESSION_CRITICAL_SECONDS ? ' critical' :
             data.remaining_seconds <= SESSION_WARN_SECONDS ? ' warn' :
             '');
          el.hidden = false;
        }
      } else {
        updateCountdown();
      }
    } catch {
      // Fall back to client-side tick on network error.
      updateCountdown();
    }
  }

  function tickCountdown() {
    updateCountdown();
    // Refresh from server every ~6 ticks (90 s) to catch sliding-window
    // renewals and clock drift.
    if ((tickCountdown._serverTick || 0) <= 0) {
      tickCountdown._serverTick = 5;
      refreshCountdownFromServer();
    } else {
      tickCountdown._serverTick--;
    }
  }
  tickCountdown._serverTick = 5;

  function startCountdownTicker() {
    stopCountdownTicker();
    // Initial fetch immediately so the display is fresh.
    refreshCountdownFromServer();
    if (window.daygleAuth) {
      window.daygleAuth._cdTimer = setInterval(tickCountdown, 15_000);
    }
  }

  function stopCountdownTicker() {
    const timer = window.daygleAuth && window.daygleAuth._cdTimer;
    if (timer) {
      clearInterval(timer);
      if (window.daygleAuth) window.daygleAuth._cdTimer = null;
    }
    tickCountdown._serverTick = 5;
  }

  // Expose the countdown helpers on window.daygleUi HERE (inside the IIFE)
  // so the bareword identifiers resolve. The bottom-of-file Object.assign
  // below runs at module top-level where startCountdownTicker /
  // stopCountdownTicker are out of scope and the shorthand-property syntax
  // would otherwise throw ``ReferenceError: startCountdownTicker is not
  // defined`` (regression observed in browser console when triggering
  // ONNX model download from the settings page).
  window.daygleUi = Object.assign(window.daygleUi || {}, {
    startCountdownTicker,
    stopCountdownTicker,
  });

  // Kick off the ticker after the initial auth render.
  if (window.daygleAuth?.user) {
    startCountdownTicker();
  }

  /* ── Idle-refresh hooks (visibilitychange + focus) ──────────────────
   * Thresholds:
   *   1. Skip the refresh if the cached expires_at still has at least
   *      ``AUTH_FOCUS_REFRESH_MARGIN_MS`` of runway. Rapidly Alt-Tabbing
   *      between windows otherwise polls /api/auth/me once per focus.
   *   2. visibilitychange -> visible is the cross-tab-aware trigger.
   *      window.addEventListener('focus', ...) is a belt-and-braces
   *      fallback for browsers that miss the visible transition.
   *   3. The refresh result flows through setApiAuth -> CustomEvent ->
   *      ``daygleUi.renderNavAccount`` via the listener at the bottom of
   *      this file, so no inline render call is needed here.
   */
  const AUTH_FOCUS_REFRESH_MARGIN_MS = 5 * 60 * 1000;
  function isFreshForRefresh() {
    const exp = window.daygleAuth?.expiresAt;
    if (!exp) return true;
    const ms = Date.parse(exp) - Date.now();
    return !Number.isFinite(ms) || ms > AUTH_FOCUS_REFRESH_MARGIN_MS;
  }
  function onReturnToForeground() {
    if (typeof window.refreshDaygleAuth !== 'function') return;
    if (!window.daygleAuth?.user) return; // handleSessionLoss already redirects on a 401 from any page's first request.
    if (isFreshForRefresh()) return;
    window.refreshDaygleAuth().catch(() => { /* keep last-known auth on transient network blips */ });
  }
  document.addEventListener('visibilitychange', () => {
    if (document.visibilityState === 'visible') onReturnToForeground();
  });
  window.addEventListener('focus', onReturnToForeground);
})();

// ─── Top-level account-area renderer ────────────────────────────────────
// Lives at the top level (NOT inside the nav-builder IIFE) so the cross-tab
// listener below - which has to register before any user interaction - can
// re-paint the account dropdown when a different tab toggles the auth
// state. The IIFE bootstrap calls this once after building the DOM, and
// every subsequent auth-state change (logout from another tab, csrf refresh,
// session-loss redirect) flows through one of three entry points:
//   1. window.daygleAuthReady resolution (initial paint)
//   2. daygle:auth-state-changed CustomEvent (utils.js cross-tab listener)
//   3. visibilitychange / window focus (idle-tab refresh)
function renderNavAccount(user) {
  const navUser = document.getElementById('navUser');
  const navAvatar = document.getElementById('navAvatar');
  const logoutBtn = document.getElementById('navLogoutBtn');
  const nav = document.querySelector('.app-nav');
  const safeReturnTo = (typeof window.defaultReturnTo === 'function')
    ? window.defaultReturnTo()
    : (window.location && (window.location.pathname + (window.location.search || ''))) || '/';
  const countdownEl = document.getElementById('sessionCountdown');
  if (!user || !user.username) {
    if (navUser) navUser.textContent = 'Sign in';
    if (navAvatar) navAvatar.textContent = '↳';
    if (countdownEl) countdownEl.hidden = true;
    // Promote the avatar into a clickable Sign in affordance rather than
    // the bare dropdown trigger. The dropdown stays for screens that err
    // on the keyboard-navigable side; the trigger now redirects when
    // clicked without a real session.
    const trigger = nav?.querySelector('.nav-dropdown[data-dropdown="account"] .nav-dropdown-trigger');
    if (trigger) {
      trigger.onclick = (e) => {
        e.preventDefault();
        e.stopPropagation();
        window.location.href = '/login?returnTo=' + encodeURIComponent(safeReturnTo);
      };
    }
    if (logoutBtn) logoutBtn.hidden = true;
    if (nav) nav.querySelectorAll('[data-admin="true"]').forEach((el) => { el.hidden = true; });
    return;
  }
  if (navUser) navUser.textContent = user.username;
  if (navAvatar) {
    // Prefer personal names for the avatar initial: first_name if available,
    // first+last initials (e.g. "JD") when both are set, fall back to the
    // first letter of the username.  Names are stripped/empty-safe so a user
    // without a configured name always shows something sensible.
    const firstInitial = (user.first_name || '').trim().charAt(0).toUpperCase();
    const lastInitial = (user.last_name || '').trim().charAt(0).toUpperCase();
    const usernameInitial = (user.username || '').charAt(0).toUpperCase();
    if (firstInitial && lastInitial) {
      navAvatar.textContent = firstInitial + lastInitial;
    } else if (firstInitial) {
      navAvatar.textContent = firstInitial;
    } else {
      navAvatar.textContent = usernameInitial;
    }
  }
  if (user.role !== 'admin' && nav) {
    nav.querySelectorAll('[data-admin="true"]').forEach((el) => { el.hidden = true; });
  }
  if (logoutBtn) logoutBtn.hidden = false;
  // The countdown element exists in the DOM; the ticker manages its
  // visibility and content. If the ticker hasn't started yet (e.g.
  // cross-tab auth change), kick it off.
  if (countdownEl && typeof window.daygleUi?.startCountdownTicker === 'function' && !window.daygleAuth?._cdTimer) {
    window.daygleUi.startCountdownTicker();
  }
}

// Expose on the daygleUi registry so re-rendering is callable from any
// per-page script that mounts after nav.js (no parallel/duplicate defs).
window.daygleUi = Object.assign(window.daygleUi || {}, {
  renderNavAccount,
});

// subscribeDaygleAuthCrossTabs (utils.js) emits CustomEvent('daygle:auth-state-changed')
// on every localStorage 'storage' event received AND on a same-tab refresh
// result. We listen here to re-paint the avatar/account dropdown so it
// reflects the latest auth state without an explicit /api/auth/me round
// trip on every cross-tab write.
window.addEventListener('daygle:auth-state-changed', () => {
  renderNavAccount(window.daygleAuth && window.daygleAuth.user || null);
});
