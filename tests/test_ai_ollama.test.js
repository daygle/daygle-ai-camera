import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import vm from 'node:vm';

function loadOllamaScript(current = {}) {
  const context = vm.createContext({
    document: { getElementById: () => null, addEventListener() {} },
    window: { daygleAuthReady: Promise.resolve(), daygleAiCurrentModelServer: () => current },
    escapeHtml: (value) => String(value).replace(/[&<>"']/g, (ch) => `&#${ch.charCodeAt(0)};`),
    Element: class {},
    console,
  });
  vm.runInContext(readFileSync(new URL('../web/ai-ollama.js', import.meta.url), 'utf8'), context);
  return context;
}

test('a saved localhost URL counts as the built-in Ollama', () => {
  const context = loadOllamaScript({ serverUrl: 'http://localhost:11434/v1/', model: 'gemma3:4b' });
  const state = { openai_url: 'http://127.0.0.1:11434/v1' };
  context.state = state;
  assert.equal(vm.runInContext("ollamaInUse(state, 'gemma3:4b')", context), true);
  assert.equal(vm.runInContext("ollamaInUse(state, 'moondream')", context), false);
  assert.equal(vm.runInContext("ollamaTagged('moondream')", context), 'moondream:latest');
  assert.equal(vm.runInContext("ollamaSameServer('', 'http://127.0.0.1:11434/v1')", context), false);
});

test('status text explains why Ollama cannot be installed', () => {
  const context = loadOllamaScript();
  context.state = { installed: false, install: {}, install_unavailable_reason: 'Daygle is running in a container.' };
  const text = vm.runInContext('ollamaStatusText(state)', context);
  assert.match(text, /not installed/);
  assert.match(text, /container/);
});

test('status text for a running server without models points at the download', () => {
  const context = loadOllamaScript();
  context.state = { installed: true, reachable: true, managed: true, version: '0.40.16', models: [], install: {} };
  const text = vm.runInContext('ollamaStatusText(state)', context);
  assert.match(text, /Ollama 0\.40\.16 is running\./);
  assert.match(text, /download a vision model/);
});

test('model rows escape names and disable Use for models that cannot read images', () => {
  const context = loadOllamaScript();
  context.state = { openai_url: 'http://127.0.0.1:11434/v1' };
  const row = vm.runInContext("ollamaModelRow(state, { name: 'x<y', size: 2e9, vision: false }, false)", context);
  assert.match(row, /x&#60;y/);
  assert.match(row, /disabled title="This model cannot read images\."/);
  assert.match(row, /2\.0 GB/);
});
