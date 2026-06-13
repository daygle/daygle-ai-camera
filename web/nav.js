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
      admin: false,
      links: [
        { href: '/cameras', match: '/cameras', label: 'Cameras' },
        { href: '/zones', match: '/zones', label: 'Zones' },
        { href: '/sounds', match: '/sounds', label: 'Sounds' },
      ],
    },
    {
      id: 'navIntel',
      label: 'Intelligence',
      admin: false,
      links: [
        { href: '/onnx', match: '/onnx', label: 'ONNX' },
        { href: '/yamnet-tflite', match: '/yamnet-tflite', label: 'YAMNet TFLite' },
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
    // events, alerts, recordings, etc.) — not just the ones that already
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
