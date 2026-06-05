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
      <a href="/anpr" data-match="/anpr">ANPR</a>
      <a href="/recordings" data-match="/recordings">Recordings</a>
      <a href="/settings" data-match="/settings" data-admin="true">AI</a>
      <a href="/alert-settings" data-match="/alert-settings" data-admin="true">Alerts</a>
      <a href="/system-settings" data-match="/system-settings" data-admin="true">System</a>
      <a href="/users" data-match="/users" data-admin="true">Users</a>
    </div>
    <div class="app-nav-account">
      <a href="/profile" data-match="/profile" id="navUser">Profile</a>
      <a href="/logout">Logout</a>
    </div>
  `;

  document.body.prepend(nav);

  nav.querySelectorAll('[data-match]').forEach((link) => {
    const match = link.getAttribute('data-match');
    if ((match === '/' && currentPath === '/') || (match !== '/' && currentPath.startsWith(match))) {
      link.classList.add('active');
    }
  });

  try {
    const response = await fetch('/api/auth/me');
    if (!response.ok) return;
    const payload = await response.json();
    const user = payload.user || {};
    const navUser = document.getElementById('navUser');
    if (navUser && user.username) navUser.textContent = user.username;
    if (user.role !== 'admin') {
      nav.querySelectorAll('[data-admin="true"]').forEach((link) => {
        link.hidden = true;
      });
    }
  } catch {
    // Protected pages redirect through the server; keep the static nav harmless.
  }
}());
