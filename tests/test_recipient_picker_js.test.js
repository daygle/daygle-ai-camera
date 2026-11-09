// Unit tests for the email recipient picker helpers in web/utils.js, shared
// by the Alerts page and Settings > camera offline alerts. A ticked user is
// stored as ``user:<id>`` and resolved to their current address server-side
// (app/alert_recipients.py).
//
// utils.js is a classic browser script that reaches for `window` at load, so
// it is loaded into a vm context behind a lightweight window stub, like
// test_tripwire_geometry_js.test.js.

import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import vm from 'node:vm';

const here = path.dirname(fileURLToPath(import.meta.url));
const utilsSource = readFileSync(path.resolve(here, '../web/utils.js'), 'utf8');
const sandbox = {
  window: { addEventListener() {}, removeEventListener() {} },
  BroadcastChannel: undefined,
};
sandbox.window.daygleUi = null;
vm.createContext(sandbox);
vm.runInContext(`${utilsSource}
globalThis.__picker = { recipientOptions, toRecipientRefs, renderRecipientPicker };`, sandbox);
const { recipientOptions, toRecipientRefs, renderRecipientPicker } = sandbox.__picker;
const rehome = (value) => JSON.parse(JSON.stringify(value));

const pickerUsers = [
  { id: 1, username: 'zed', first_name: 'Zed', last_name: 'Smith', email: 'zed@example.com', is_active: 1 },
  { id: 2, username: 'amy', first_name: '', last_name: '', email: 'Amy@Example.com', is_active: 1 },
  { id: 3, username: 'noemail', email: '', is_active: 1 },
  { id: 4, username: 'gone', email: 'gone@example.com', is_active: 0 },
];

test('recipient picker lists active users with an email, ticking referenced ones', () => {
  const options = rehome(recipientOptions(['user:2'], pickerUsers));
  assert.deepEqual(options.map(({ value, name, detail, checked, orphan }) => ({ value, name, detail, checked, orphan })), [
    { value: 'user:2', name: 'amy', detail: 'Amy@Example.com', checked: true, orphan: false },
    { value: 'user:1', name: 'Zed Smith', detail: 'zed@example.com', checked: false, orphan: false },
  ]);
});

test('entries that send nothing via the user list stay listed and ticked', () => {
  const options = rehome(recipientOptions('typed@example.com, user:4, user:3, user:99', pickerUsers))
    .filter((option) => option.orphan);
  assert.deepEqual(options.map(({ value, name, detail, checked }) => ({ value, name, detail, checked })), [
    { value: 'typed@example.com', name: 'typed@example.com', detail: 'Not a user', checked: true },
    { value: 'user:4', name: 'gone', detail: 'Inactive, gets no email', checked: true },
    { value: 'user:3', name: 'noemail', detail: 'No email address', checked: true },
    { value: 'user:99', name: 'Deleted user #99', detail: 'Gets no email', checked: true },
  ]);
});

test('stored addresses that belong to an active user become user references', () => {
  assert.deepEqual(
    rehome(toRecipientRefs(['amy@example.com', 'typed@example.com', 'user:2', 'gone@example.com'], pickerUsers)),
    ['user:2', 'typed@example.com', 'gone@example.com'],
  );
  assert.equal(toRecipientRefs('ZED@example.com, other@example.com', pickerUsers), 'user:1, other@example.com');
});

test('the rendered picker carries user references and the empty note', () => {
  const html = renderRecipientPicker(['user:1'], pickerUsers, { emptyNote: 'Falls back to <From>' });
  assert.match(html, /value="user:1" checked/);
  assert.match(html, /value="user:2" >/);
  assert.match(html, /Falls back to &lt;From&gt;/);
  assert.match(renderRecipientPicker([], []), /No users have an email address yet/);
});
