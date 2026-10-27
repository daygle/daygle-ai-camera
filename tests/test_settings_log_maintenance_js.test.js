// Regression tests for Settings → Maintenance → Log Maintenance.
//
// The card exposes manual clear-downs of the Camera Log (camera_diagnostics)
// and the AI Log (ai_diagnostics), mirroring the "Clear Log" buttons on
// /camera-log and /ai-log. It's pure wiring: both DELETE endpoints already
// exist server-side and are admin-gated there, so these tests pin (a) the
// buttons exist on the maintenance panel and (b) settings.js calls the right
// DELETE endpoints - the same static-analysis approach the other
// *_js.test.js guards use, since settings.js touches the DOM at load.
//
// Run with:
//   node --test tests/test_settings_log_maintenance_js.test.js
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const here = path.dirname(fileURLToPath(import.meta.url));
const settingsHtml = readFileSync(path.resolve(here, '../web/settings.html'), 'utf8');
const settingsJs = readFileSync(path.resolve(here, '../web/settings.js'), 'utf8');

test('maintenance panel has the camera-log and ai-log clear buttons', () => {
  const panelStart = settingsHtml.indexOf('id="panel-maintenance"');
  assert.notEqual(panelStart, -1, 'settings.html must have the maintenance panel');
  const panelEnd = settingsHtml.indexOf('/panel-maintenance', panelStart);
  const panel = settingsHtml.slice(panelStart, panelEnd);
  assert.ok(panel.includes('Log Maintenance'), 'the Log Maintenance card must be on the maintenance panel');
  assert.ok(panel.includes('id="purgeCameraLogBtn"'), 'camera log purge button missing');
  assert.ok(panel.includes('id="clearAiLogBtn"'), 'AI log clear button missing');
});

test('settings.js wires the buttons to the admin-gated log DELETE endpoints', () => {
  const camera = settingsJs.indexOf("getElementById('purgeCameraLogBtn')");
  const ai = settingsJs.indexOf("getElementById('clearAiLogBtn')");
  assert.notEqual(camera, -1, 'purgeCameraLogBtn must be wired in settings.js');
  assert.notEqual(ai, -1, 'clearAiLogBtn must be wired in settings.js');
  const cameraBlock = settingsJs.slice(camera, camera + 400);
  const aiBlock = settingsJs.slice(ai, ai + 400);
  assert.match(cameraBlock, /\/api\/camera-log'\s*,\s*\{\s*method:\s*'DELETE'/);
  assert.match(aiBlock, /\/api\/ai-log'\s*,\s*\{\s*method:\s*'DELETE'/);
  // Irreversible actions confirm before firing, matching the log pages.
  assert.ok(cameraBlock.includes('window.confirm'), 'camera purge must confirm first');
  assert.ok(aiBlock.includes('window.confirm'), 'AI log clear must confirm first');
  assert.ok(cameraBlock.includes('result.deleted') && aiBlock.includes('result.deleted'),
            'both handlers should echo the deleted count');
});
