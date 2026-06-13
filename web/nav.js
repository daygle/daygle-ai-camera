window.showToast = function (message, isError) {
  if (!message) return;
  let container = document.getElementById('toastContainer');
  if (!container) {
    container = document.createElement('div');
    container.id = 'toastContainer';
    container.className = 'toast-container';
    document.body.appendChild(container);
  }
  const toast = document.createElement('div');
  toast.className = 'toast' + (isError ? ' error' : '');
  toast.textContent = String(message);
  container.appendChild(toast);
  setTimeout(() => toast.remove(), 3500);
};

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
  ['connection', DAYGLE_BUTTON_ICONS.video],
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
            <span id="navAvatar" class="nav-avatar">?</span>
            <span id="navUser" class="nav-dropdown-label">Profile</span>
            <svg class="nav-dropdown-chevron" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><polyline points="6 9 12 15 18 9"/></svg>
          </button>
          <div class="nav-dropdown-menu">
            <a href="/profile" class="nav-dropdown-item">Profile</a>
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
  try {
    const response = await fetch('/api/auth/me');
    if (!response.ok) return;
    const payload = await response.json();
    const user = payload.user || {};
    const csrfToken = payload.csrf_token || '';
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
    const navUser = document.getElementById('navUser');
    const navAvatar = document.getElementById('navAvatar');
    if (user.username) {
      if (navUser) navUser.textContent = user.username;
      if (navAvatar) navAvatar.textContent = user.username.charAt(0).toUpperCase();
    }
    if (user.role !== 'admin') {
      nav.querySelectorAll('[data-admin="true"]').forEach((el) => {
        el.hidden = true;
      });
    }
    const logoutBtn = document.getElementById('navLogoutBtn');
    if (logoutBtn && csrfToken) {
      logoutBtn.addEventListener('click', async () => {
        try {
          await fetch('/logout', { method: 'POST', headers: { 'X-CSRF-Token': csrfToken } });
        } catch {
          // Ignore network errors; the redirect below will clear the session server-side.
        }
        window.location.href = '/login';
      });
    }
  } catch {
    // Protected pages redirect through the server; keep the static nav harmless.
  }
}());
