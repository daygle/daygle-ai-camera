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
  nav.innerHTML = `
    <a class="app-brand" href="/">
      <span class="brand-mark">D</span>
      <span>Daygle AI Camera</span>
    </a>
    <div class="app-nav-links">
      <a href="/" data-match="/">Dashboard</a>
      <a href="/live" data-match="/live">Live</a>
      <a href="/zones" data-match="/zones" data-admin="true">Zones</a>
      <a href="/cameras" data-match="/cameras" data-admin="true">Cameras</a>
      <a href="/anpr" data-match="/anpr">ANPR</a>
      <a href="/recordings" data-match="/recordings">Recordings</a>
      <a href="/recordings/timeline" data-match="/recordings/timeline">Timeline</a>
      <a href="/ai" data-match="/ai" data-admin="true">AI</a>
      <a href="/settings" data-match="/settings" data-admin="true">Settings</a>
      <a href="/users" data-match="/users" data-admin="true">Users</a>
    </div>
    <div class="app-nav-account">
      <a href="/profile" data-match="/profile" id="navUser">Profile</a>
      <button class="nav-logout-btn" id="navLogoutBtn" type="button">Logout</button>
    </div>
  `;

  document.body.prepend(nav);

  const activeLink = Array.from(nav.querySelectorAll('[data-match]'))
    .filter((link) => {
      const match = String(link.getAttribute('data-match') || '');
      return (match === '/' && currentPath === '/') || (match !== '/' && currentPath.startsWith(match));
    })
    .sort((left, right) => String(right.getAttribute('data-match') || '').length - String(left.getAttribute('data-match') || '').length)[0];
  if (activeLink) activeLink.classList.add('active');

  try {
    const response = await fetch('/api/auth/me');
    if (!response.ok) return;
    const payload = await response.json();
    const user = payload.user || {};
    const csrfToken = payload.csrf_token || '';
    const navUser = document.getElementById('navUser');
    if (navUser && user.username) navUser.textContent = user.username;
    if (user.role !== 'admin') {
      nav.querySelectorAll('[data-admin="true"]').forEach((link) => {
        link.hidden = true;
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
