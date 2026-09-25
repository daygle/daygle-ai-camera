// The per-camera sound "Check Interval" control on /sounds. The backend
// (app/sound_detector.py + app/sound_monitor.py) applies it as the hop
// between 1-second analysis windows; these tests pin that the page actually
// reads it back out and sends it on save, since a field that renders but is
// never submitted would silently keep the old cadence.
//
// sounds.js is DOM-coupled at import, so these are source assertions, the same
// style as tests/test_live_hidden_tab_poll.test.js.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const here = path.dirname(fileURLToPath(import.meta.url));
const soundsSource = readFileSync(path.resolve(here, '../web/sounds.js'), 'utf8');
const soundsHtml = readFileSync(path.resolve(here, '../web/sounds.html'), 'utf8');

test('the sounds page offers a Check Interval control', () => {
  assert.match(soundsHtml, /id="soundDetectionInterval"/);
  // 0.1-0.5: the constructor clamps a longer hop to half the 1s window, so the
  // input must not offer a value the backend would silently ignore.
  assert.match(soundsHtml, /min="0\.1" max="0\.5"/);
  assert.match(soundsSource, /soundDetectionInterval = document\.getElementById\('soundDetectionInterval'\)/);
});

test('the stored interval is loaded back into the form', () => {
  assert.match(
    soundsSource,
    /function detectorSoundInterval\(camera\)[\s\S]*?detection_interval_seconds[\s\S]*?return Number\.isFinite\(value\) \? value : 0\.5;/,
  );
  const start = soundsSource.indexOf('function renderEditor()');
  const body = soundsSource.slice(start, soundsSource.indexOf('async function refreshStatus', start));
  assert.match(body, /soundDetectionInterval\.value = String\(detectorSoundInterval\(camera\)\);/);
});

test('saving sends the clamped interval with the sound settings', () => {
  const start = soundsSource.indexOf('async function saveSoundDetection()');
  const body = soundsSource.slice(start, soundsSource.indexOf('saveBtn.disabled = true', start));
  assert.match(body, /detection_interval_seconds:/);
  // Clamped in the client too, so a typed-in 5 or 0 never round-trips.
  assert.match(body, /Math\.min\(\s*0\.5,\s*Math\.max\(0\.1, Number\(soundDetectionInterval\?\.value\) \|\| 0\.5\),\s*\)/);
});
