// Integration tests for frontend auth edge cases.
//
// Loads web/utils.js (which supplies the API client, cross-tab auth
// broadcast, and session-loss handling) into a sandboxed vm context
// with browser-api mocks, then exercises every hardened auth edge case.
//
// Run with:
//   node --test tests/test_auth_frontend_edge_cases.test.js

import { test, describe } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import vm from 'node:vm';

const here = path.dirname(fileURLToPath(import.meta.url));
const utilsSource = readFileSync(path.resolve(here, '../web/utils.js'), 'utf8');

// ─── Helper: build a mock fetch response object ─────────────────────────

function mockResponse(status, ok, detail) {
  const payload = detail ? { detail } : {};
  return {
    status,
    ok,
    statusText: String(status),
    headers: { get: () => null },
    json: async () => payload,
  };
}

// ─── Sandbox builder ────────────────────────────────────────────────────

function createSandbox() {
  const sessionLossCalls = [];
  const storageListeners = [];

  const location = { href: '', pathname: '/recordings', search: '' };

  // Synchronous localStorage mock - storage events fire synchronously
  // on all listeners on every setItem (including the writing "tab", which
  // differs from real browser behaviour but is fine for testing).
  const localStorage = (() => {
    const store = Object.create(null);
    return {
      _store: store,
      getItem(key) { return store[key] ?? null; },
      setItem(key, value) {
        const oldValue = store[key] ?? null;
        store[key] = String(value);
        const event = { key, oldValue, newValue: String(value), storageArea: localStorage, url: 'http://localhost/' };
        for (const listener of storageListeners) {
          try { listener(event); } catch { /* ignore */ }
        }
      },
      removeItem(key) { delete store[key]; },
      clear() { for (const k of Object.keys(store)) delete store[k]; },
      get length() { return Object.keys(store).length; },
      key(i) { return Object.keys(store)[i] ?? null; },
    };
  })();

  let timerId = 1;
  function mockTimer() { const id = timerId++; return id; }

  const sandbox = {
    // Bare globals (resolvable inside the vm context without window. prefix).
    setTimeout: mockTimer,
    clearTimeout: function () {},
    setInterval: mockTimer,
    clearInterval: function () {},
    fetch: undefined,

    // CustomEvent must be resolvable as a bare global inside the vm context.
    // Code running inside setApiAuth does `new CustomEvent(...)` which looks
    // up CustomEvent on the sandbox global, not on window.CustomEvent.
    CustomEvent: class CustomEvent {
      constructor(type, detail) {
        this.type = type;
        this.detail = detail;
      }
    },


    window: {
      location,
      _events: [], // populated by dispatchEvent mock below
      addEventListener(type, listener) {
        if (type === 'storage') storageListeners.push(listener);
      },
      removeEventListener() {},
      dispatchEvent(event) {
        this._events.push({ type: event.type, detail: event.detail });
      },
      CustomEvent: class CustomEvent {
        constructor(type, detail) {
          this.type = type;
          this.detail = detail;
        }
      },
      setTimeout: mockTimer,
      clearTimeout: function () {},
      setInterval: mockTimer,
      clearInterval: function () {},
    },

    document: {
      getElementById() { return null; },
      createElement(tag) {
        return { tagName: tag.toUpperCase(), id: '', className: '', textContent: '', getAttribute() {}, setAttribute() {}, appendChild() {}, remove() {} };
      },
      body: { appendChild() {} },
      title: 'Test Page',
      URL: 'http://localhost/test',
    },

    localStorage,
    console: { log() {}, error() {}, warn() {} },

    // Tracked by the handleSessionLoss wrapper.
    _sessionLossCalls: sessionLossCalls,
  };

  sandbox.window.daygleUi = null;
  sandbox.window.daygleAuth = undefined;

  vm.createContext(sandbox);
  vm.runInContext(utilsSource, sandbox);

  // Wrap handleSessionLoss to track calls for assertions.
  // The wrapper records calls BEFORE the redirecting guard so we can
  // assert the function was invoked. The guard itself still prevents
  // double-redirects - our wrapper doesn't change that behaviour.
  const origHandleSessionLoss = sandbox.window.daygleUi.handleSessionLoss;
  sandbox.window.daygleUi.handleSessionLoss = function (reason, returnTo) {
    sessionLossCalls.push({ reason, returnTo });
    return origHandleSessionLoss.call(this, reason, returnTo);
  };
  sandbox.handleSessionLoss = sandbox.window.daygleUi.handleSessionLoss;

  // Start with clean auth state. This fires a daygle:auth-state-changed
  // event (stored in sandbox.window._events).
  sandbox.window.daygleUi.setApiAuth(null, null, null);

  return { sandbox, sessionLossCalls, storageListeners };
}

// Helper (exported from createSandbox for tests that need it): fire a
// synthetic 'storage' event on all registered listeners WITHOUT going
// through localStorage.setItem. This avoids the recursive loop:
//   handleSessionLoss → broadcastAuthStateToOtherTabs → setItem →
//   storage event → listener → handleSessionLoss (guarded by redirecting).
// The helper is defined here for reuse; wrap in your test's closure by
// calling `const { sandbox } = createSandbox(); const fireStorageEvent = makeFireStorageEvent(sandbox);`
// or inline the logic.


// ─── Tests ──────────────────────────────────────────────────────────────

describe('api() 403 CSRF handler', () => {
  test('403 with CSRF detail triggers handleSessionLoss even when user is null', async () => {
    const { sandbox, sessionLossCalls } = createSandbox();
    const ui = sandbox.window.daygleUi;

    // Simulate the race condition: user was already cleared by a prior 401.
    ui.setApiAuth(null, null, null);
    assert.equal(sandbox.window.daygleAuth.user, null);

    sandbox.fetch = async () => mockResponse(403, false, 'CSRF token missing or invalid');
    sessionLossCalls.length = 0;

    await assert.rejects(
      () => ui.api('/api/some-route', { method: 'POST' }),
      { message: 'Session expired' },
    );

    // handleSessionLoss is called at least once. The exact count can be
    // >1 because handleSessionLoss broadcasts empty-csrf to localStorage,
    // which fires a synchronous storage event, which invokes the listener
    // (subscribeDaygleAuthCrossTabs), which calls handleSessionLoss again
    // - but the redirecting guard prevents the second call from acting.
    assert.ok(sessionLossCalls.length >= 1,
      `handleSessionLoss must be called >=1 for CSRF 403 with user=null (got ${sessionLossCalls.length})`);
    assert.match(sessionLossCalls[0].reason, /session expired/i);
  });

  test('403 with "Invalid token" detail triggers handleSessionLoss', async () => {
    const { sandbox, sessionLossCalls } = createSandbox();
    const ui = sandbox.window.daygleUi;
    ui.setApiAuth({ username: 'admin' }, 'valid-token');

    sandbox.fetch = async () => mockResponse(403, false, 'Invalid token');
    sessionLossCalls.length = 0;

    await assert.rejects(
      () => ui.api('/api/some-route', { method: 'POST' }),
      { message: 'Session expired' },
    );
    assert.ok(sessionLossCalls.length >= 1,
      `handleSessionLoss must be called for 403 "Invalid token" (got ${sessionLossCalls.length})`);
  });

  test('403 with "Admin access required" does NOT trigger handleSessionLoss', async () => {
    const { sandbox, sessionLossCalls } = createSandbox();
    const ui = sandbox.window.daygleUi;
    ui.setApiAuth({ username: 'viewer' }, 'viewer-token');

    sandbox.fetch = async () => mockResponse(403, false, 'Admin access required');
    sessionLossCalls.length = 0;

    await assert.rejects(
      () => ui.api('/api/admin-route', { method: 'POST' }),
      // Falls through CSRF regex → !response.ok → throws detail text directly.
      { message: 'Admin access required' },
    );
    assert.equal(sessionLossCalls.length, 0,
      'Admin access required must NOT trigger handleSessionLoss');
  });

  test('403 with empty detail does NOT trigger handleSessionLoss', async () => {
    const { sandbox, sessionLossCalls } = createSandbox();
    const ui = sandbox.window.daygleUi;
    ui.setApiAuth({ username: 'admin' }, 'token');

    sandbox.fetch = async () => mockResponse(403, false, null);
    sessionLossCalls.length = 0;

    await assert.rejects(
      () => ui.api('/api/some-route', { method: 'POST' }),
      { message: 'Request failed: 403' },
    );
    assert.equal(sessionLossCalls.length, 0);
  });

  test('401 always triggers handleSessionLoss', async () => {
    const { sandbox, sessionLossCalls } = createSandbox();
    const ui = sandbox.window.daygleUi;
    ui.setApiAuth({ username: 'admin' }, 'token');

    sandbox.fetch = async () => mockResponse(401, false, 'Authentication required');
    sessionLossCalls.length = 0;

    await assert.rejects(
      () => ui.api('/api/status'),
      { message: 'Authentication required' },
    );
    assert.ok(sessionLossCalls.length >= 1,
      `handleSessionLoss must be called for 401 (got ${sessionLossCalls.length})`);
  });
});


describe('handleSessionLoss broadcasts to other tabs', () => {
  test('handleSessionLoss writes empty-csrf to localStorage', () => {
    const { sandbox } = createSandbox();
    const ui = sandbox.window.daygleUi;
    ui.setApiAuth({ username: 'admin' }, 'token', '3026-01-01T00:00:00Z');

    ui.handleSessionLoss('Session expired', '/');

    const stored = sandbox.localStorage.getItem('daygle.auth.v1');
    assert.ok(stored, 'localStorage must have an auth entry after handleSessionLoss');
    const parsed = JSON.parse(stored);
    assert.equal(parsed.csrf, '', 'csrf must be empty to signal session ended');
    assert.equal(parsed.u, '', 'username must be empty');
  });

  test('handleSessionLoss is idempotent when redirecting is in progress', () => {
    const { sandbox, sessionLossCalls } = createSandbox();
    const ui = sandbox.window.daygleUi;
    ui.setApiAuth({ username: 'admin' }, 'token');

    sessionLossCalls.length = 0;
    ui.handleSessionLoss('First', '/');
    assert.ok(sessionLossCalls.length >= 1,
      `handleSessionLoss must be called >=1 (got ${sessionLossCalls.length})`);

    // Second call - still tracked by wrapper, but original logic returns
    // early due to redirecting guard.
    ui.handleSessionLoss('Second', '/');
    assert.ok(sandbox.window.daygleAuth.redirecting === true,
      'Redirecting guard must be set after handleSessionLoss');
  });
});


describe('Cross-tab auth sync', () => {
  test('empty-csrf storage event triggers handleSessionLoss', () => {
    const { sandbox, sessionLossCalls } = createSandbox();
    const ui = sandbox.window.daygleUi;
    ui.setApiAuth({ username: 'admin' }, 'token', '3026-01-01T00:00:00Z');

    sessionLossCalls.length = 0;

    // Write empty-csrf to localStorage - fires storage listener synchronously.
    // The listener calls handleSessionLoss, which broadcasts empty-csrf again,
    // triggering a recursive call, but the redirecting guard suppresses it.
    sandbox.localStorage.setItem('daygle.auth.v1',
      JSON.stringify({ u: '', csrf: '', exp: '', ts: Date.now() }),
    );

    assert.ok(sessionLossCalls.length >= 1,
      'Empty-csrf storage event must trigger handleSessionLoss >=1');
    assert.ok(sandbox.window.daygleAuth.redirecting === true,
      'Redirecting guard must be set after handleSessionLoss');
  });

  test('cross-tab sync preserves local CSRF token', () => {
    const { sandbox } = createSandbox();
    const ui = sandbox.window.daygleUi;

    ui.setApiAuth({ username: 'admin' }, 'local-csrf-token', '3026-01-01T00:00:00Z');

    // Another tab broadcasts a valid session with a DIFFERENT CSRF token.
    sandbox.localStorage.setItem('daygle.auth.v1',
      JSON.stringify({ u: 'other-user', csrf: 'remote-csrf-token', exp: '3027-01-01T00:00:00Z', ts: Date.now() }),
    );

    // Local CSRF token must NOT be overwritten.
    assert.equal(
      sandbox.window.daygleAuth.csrfToken,
      'local-csrf-token',
      'Local CSRF token must NOT be overwritten by remote broadcast',
    );
    // Local user must be preserved.
    assert.equal(
      sandbox.window.daygleAuth.user?.username,
      'admin',
      'Local user must be preserved',
    );
    // Expiry should be updated from the remote payload.
    assert.equal(
      sandbox.window.daygleAuth.expiresAt,
      '3027-01-01T00:00:00Z',
      'ExpiresAt must be updated from remote broadcast',
    );
  });

  test('broadcastAuthStateToOtherTabs writes correct payload format', () => {
    const { sandbox } = createSandbox();
    sandbox.broadcastAuthStateToOtherTabs({ username: 'alice' }, 'alice-csrf', '3026-06-15T00:00:00Z');

    const stored = sandbox.localStorage.getItem('daygle.auth.v1');
    assert.ok(stored);
    const parsed = JSON.parse(stored);
    assert.equal(parsed.u, 'alice');
    assert.equal(parsed.csrf, 'alice-csrf');
    assert.equal(parsed.exp, '3026-06-15T00:00:00Z');
    assert.ok(Number.isFinite(parsed.ts), 'timestamp must be a number');
  });
});


describe('Logout with null CSRF token', () => {
  function captureHeadersSandbox() {
    const ctx = createSandbox();
    const sentHeaders = { value: null };
    ctx.sandbox.fetch = async (url, opts) => {
      sentHeaders.value = opts.headers;
      return { status: 200, ok: true, json: async () => ({ ok: true }) };
    };
    return { ...ctx, sentHeaders };
  }

  test('api() sends request even with null csrfToken', async () => {
    const { sandbox, sentHeaders } = captureHeadersSandbox();
    const ui = sandbox.window.daygleUi;
    ui.setApiAuth(null, null, null);
    assert.equal(sandbox.window.daygleAuth.csrfToken, null);

    const result = await ui.api('/logout', { method: 'POST' });
    assert.deepEqual(result, { ok: true });
    assert.equal(sentHeaders.value['X-CSRF-Token'], undefined,
      'X-CSRF-Token must NOT be sent when csrfToken is null');
  });

  test('api() sends csrf token when it exists', async () => {
    const { sandbox, sentHeaders } = captureHeadersSandbox();
    const ui = sandbox.window.daygleUi;
    ui.setApiAuth({ username: 'admin' }, 'my-csrf');

    await ui.api('/logout', { method: 'POST' });
    assert.equal(sentHeaders.value['X-CSRF-Token'], 'my-csrf',
      'X-CSRF-Token must be sent when csrfToken is set');
  });

  test('GET requests do not send csrf token', async () => {
    const { sandbox, sentHeaders } = captureHeadersSandbox();
    const ui = sandbox.window.daygleUi;
    ui.setApiAuth({ username: 'admin' }, 'my-csrf');

    await ui.api('/api/status');
    assert.equal(sentHeaders.value['X-CSRF-Token'], undefined,
      'GET requests must NOT send X-CSRF-Token');
  });
});


describe('setApiAuth dispatches auth-state-changed event', () => {
  test('setApiAuth fires daygle:auth-state-changed CustomEvent', () => {
    const { sandbox } = createSandbox();
    const ui = sandbox.window.daygleUi;

    // setApiAuth was called once during createSandbox() to reset state.
    // Count events stored on window._events (populated by dispatchEvent mock).
    const before = sandbox.window._events.filter(
      (e) => e.type === 'daygle:auth-state-changed',
    ).length;

    ui.setApiAuth({ username: 'bob' }, 'bob-csrf');

    const after = sandbox.window._events.filter(
      (e) => e.type === 'daygle:auth-state-changed',
    ).length;
    assert.ok(after > before,
      `daygle:auth-state-changed events must increase (before=${before}, after=${after})`);
  });

  test('setApiAuth(null, null, null) clears auth state and fires event', () => {
    const { sandbox } = createSandbox();
    const ui = sandbox.window.daygleUi;

    ui.setApiAuth({ username: 'admin' }, 'token', '3026-01-01T00:00:00Z');
    assert.ok(sandbox.window.daygleAuth.user);

    const before = sandbox.window._events.filter(
      (e) => e.type === 'daygle:auth-state-changed',
    ).length;

    ui.setApiAuth(null, null, null);

    assert.equal(sandbox.window.daygleAuth.user, null);
    assert.equal(sandbox.window.daygleAuth.csrfToken, null);
    assert.equal(sandbox.window.daygleAuth.expiresAt, '');

    const after = sandbox.window._events.filter(
      (e) => e.type === 'daygle:auth-state-changed',
    ).length;
    assert.ok(after > before,
      `setApiAuth(null, null, null) must dispatch event (before=${before}, after=${after})`);
  });
});
