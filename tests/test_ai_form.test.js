import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import vm from 'node:vm';

test('AI settings submits an empty API key to clear stored credentials', () => {
  const fields = {
    server_url: { value: 'http://localhost:11434/v1' },
    model: { value: 'gemma3:4b' },
    api_key: { value: '' },
    timeout_seconds: { value: '20' },
    focus_crop: { value: 'true' },
    describe_events: { value: 'off' },
  };
  const form = { elements: fields, addEventListener() {}, querySelectorAll: () => [] };
  const context = vm.createContext({
    document: { getElementById: (id) => id === 'aiSettingsForm' ? form : null },
    window: { daygleAuthReady: new Promise(() => {}) },
    console,
  });
  vm.runInContext(readFileSync(new URL('../web/ai.js', import.meta.url), 'utf8'), context);
  const payload = vm.runInContext('aiPayload()', context);
  assert.equal(payload.api_key, '');
  assert.equal(payload.model, 'gemma3:4b');
  assert.equal(payload.timeout_seconds, 20);
  assert.equal(payload.focus_crop, true);
});
