let csrfToken = null;
const profileForm = document.getElementById('profileForm');
const passwordForm = document.getElementById('passwordForm');
const messageEl = document.getElementById('profileMessage');
const summaryEl = document.getElementById('profileSummary');

async function api(path, options = {}) {
  const headers = { ...(options.headers || {}) };
  if (csrfToken && ['POST', 'PUT', 'PATCH', 'DELETE'].includes((options.method || 'GET').toUpperCase())) {
    headers['X-CSRF-Token'] = csrfToken;
  }
  if (options.body && !headers['Content-Type']) headers['Content-Type'] = 'application/json';
  const response = await fetch(path, { ...options, headers });
  if (response.status === 401) window.location.href = '/login';
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(payload.detail || `Request failed: ${response.status}`);
  return payload;
}

function escapeHtml(value) {
  return String(value ?? '').replace(/[&<>'"]/g, (char) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', "'": '&#39;', '"': '&quot;' }[char]));
}

function setMessage(text) { messageEl.textContent = text; }

function renderProfile(user) {
  profileForm.elements.timezone.value = user.timezone || 'Australia/Sydney';
  profileForm.elements.date_format.value = user.date_format || 'locale';
  profileForm.elements.time_format.value = user.time_format || '24h';
  summaryEl.innerHTML = `
    <div><span>Username</span><strong>${escapeHtml(user.username)}</strong></div>
    <div><span>Role</span><strong>${escapeHtml(user.role)}</strong></div>
    <div><span>Timezone</span><strong>${escapeHtml(user.timezone || 'Australia/Sydney')}</strong></div>
    <div><span>Date/time</span><strong>${escapeHtml(user.date_format || 'locale')} / ${escapeHtml(user.time_format || '24h')}</strong></div>
  `;
}

async function loadProfile() {
  const me = await api('/api/auth/me');
  csrfToken = me.csrf_token;
  renderProfile(me.user);
}

profileForm.addEventListener('submit', async (event) => {
  event.preventDefault();
  const payload = Object.fromEntries(new FormData(profileForm).entries());
  try {
    renderProfile(await api('/api/profile', { method: 'PUT', body: JSON.stringify(payload) }));
    setMessage('Profile saved.');
  } catch (error) { setMessage(error.message); }
});

passwordForm.addEventListener('submit', async (event) => {
  event.preventDefault();
  const payload = Object.fromEntries(new FormData(passwordForm).entries());
  if (payload.new_password !== payload.confirm_password) {
    setMessage('Passwords do not match.');
    return;
  }
  delete payload.confirm_password;
  try {
    await api('/api/profile/password', { method: 'POST', body: JSON.stringify(payload) });
    passwordForm.reset();
    setMessage('Password changed.');
  } catch (error) { setMessage(error.message); }
});

loadProfile().catch((error) => setMessage(error.message));
