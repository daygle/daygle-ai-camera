// Regression tests for the Object/Face Models page's completion feedback.
//
// A download or update runs for minutes and then rebuilds the whole card
// list (loadModels() re-creates every card's DOM). Two things went wrong with
// that sequence, and both left the operator refreshing the page to find out
// whether anything had happened:
//   1. the success message was written BEFORE the rebuild, so the rebuild
//      wiped it the instant it appeared;
//   2. "Update Available" was cleared with the card's variant id
//      (yolo26n-640) while the badge is keyed on the catalog id (yolo26n),
//      so the badge stayed up until a refresh killed the in-memory map.
// These tests pin the order and the keys the handler relies on.

import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const here = path.dirname(fileURLToPath(import.meta.url));
const source = readFileSync(path.resolve(here, '../web/models.js'), 'utf8');

const actionStart = source.indexOf('function bindModelCardActions');
const actionEnd = source.indexOf('function populateFaceModelSelect');
const actionBlock = source.slice(actionStart, actionEnd);

test('the model-card action handler is located', () => {
  assert.ok(actionStart !== -1 && actionEnd > actionStart, 'bindModelCardActions block not found');
});

test('the confirmation is written after the list rebuild, not before it', () => {
  const rebuildAt = actionBlock.indexOf('await loadModels();');
  const confirmAt = actionBlock.indexOf('setModelMessage(confirmationCardKey(');
  assert.ok(rebuildAt !== -1, 'the handler no longer rebuilds the card list');
  assert.ok(confirmAt !== -1, 'the handler no longer writes a confirmation');
  assert.ok(
    confirmAt > rebuildAt,
    'the success message must be written after loadModels(), or the rebuild wipes it',
  );
});

test('the confirmation lands on the card that now holds the model', () => {
  // A first install swaps the catalog card for a "-new" download card plus an
  // installed-variant card, so the clicked button's key can be gone by then.
  assert.match(source, /function confirmationCardKey\(modelName, imgsz, fallbackKey\)/);
  assert.match(actionBlock, /matchedImgsz/);
});

test('a card message survives the list rebuilds that follow it', () => {
  // Every action ends in loadModels() and so does a later update check; the
  // message must be re-emitted from storage instead of being rebuilt empty.
  assert.match(source, /const modelCardMessages = \{\}/);
  assert.match(source, /modelCardMessages\[modelId\] = \{ text, type \}/);
  assert.match(source, /const storedMessage = modelCardMessages\[cardKey\]/);
  // A deleted card must not leave its message behind for a reinstall.
  assert.match(actionBlock, /delete modelCardMessages\[modelId\]/);
});

test('a finished update clears the badge on the catalog id too', () => {
  assert.match(actionBlock, /delete modelUpdateMap\[modelName\]/);
});

test('the confirmation is not auto-cleared after five seconds', () => {
  // It is the only in-page proof a minutes-long export finished; the toast
  // only lives a few seconds and errors already persist.
  assert.doesNotMatch(actionBlock, /setTimeout\(\(\) => setModelMessage/);
});

test('a failed detector reload is reported as an error, not success', () => {
  // reload_succeeded is false whenever the model simply was not active, so
  // only reload_error is a fault.
  assert.match(actionBlock, /result\.reload_error/);
  assert.match(actionBlock, /Detector reload failed/);
});

test('the updates banner is re-computed after a card action', () => {
  assert.match(actionBlock, /refreshUpdateSummary\(/);
  // Shared with the manual check so the two can never disagree.
  assert.match(source, /function updateCheckSummary\(family, errorMessage = null\)/);
  assert.match(source, /const \{ message, isError \} = updateCheckSummary\(family, result\.error \|\| null\)/);
});

test('a delete does not paint an empty payload over the status card', () => {
  // DELETE returns {ok, message, deleted_path} with no detector status;
  // rendering that used to show "Model: Not Set / Device: N/A" until refresh.
  assert.match(actionBlock, /else if \(result\.status\)/);
  assert.match(actionBlock, /renderAi\(await api\('\/api\/settings\/ai'\)\)/);
});
