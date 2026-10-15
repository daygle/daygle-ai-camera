// The live motion meter (web/live.js) and the zone editor's motion trigger
// (web/zones.js).
//
// Each motion zone is reported as the share of ITS OWN pixels that changed,
// next to the trigger it was set to, so the bar and the setting use the same
// units. The editor sets one "trigger when X% of this area moves" value per
// zone, defaulting to Normal (2%) like the backend.
//
// The helpers are sliced out of the page scripts and run in a vm (both
// scripts touch the DOM at load).
//
// Run with:
//   node --test tests/test_motion_meter_js.test.js

import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import vm from 'node:vm';

const here = path.dirname(fileURLToPath(import.meta.url));
const liveSource = readFileSync(path.resolve(here, '../web/live.js'), 'utf8');
const zonesSource = readFileSync(path.resolve(here, '../web/zones.js'), 'utf8');
const liveHtml = readFileSync(path.resolve(here, '../web/live.html'), 'utf8');
const settingsHtml = readFileSync(path.resolve(here, '../web/settings.html'), 'utf8');

function slice(source, startMarker, endMarker) {
  const start = source.indexOf(startMarker);
  assert.notEqual(start, -1, `missing ${startMarker}`);
  const end = source.indexOf(endMarker, start);
  assert.notEqual(end, -1, `missing ${endMarker}`);
  return source.slice(start, end);
}

function loadHelpers() {
  const sandbox = {
    clamp: (value, min, max) => Math.min(max, Math.max(min, value)),
    escapeHtml: (value) => String(value ?? '').replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;'),
  };
  vm.createContext(sandbox);
  const meter = slice(liveSource, 'const MOTION_QUIET_FRACTION', '// True when the camera has at least one enabled motion zone');
  const fraction = slice(zonesSource, 'function optionalFraction', '// "Trigger when X% of this zone moves" presets.');
  const trigger = slice(zonesSource, '// "Trigger when X% of this zone moves" presets.', '// Update only the label text on the Draw polygon button');
  vm.runInContext(`${meter}\n${fraction}\n${trigger}\nthis.api = { formatMotionShare, overallMotionZoneState, motionZoneMeterHtml, effectiveMotionTrigger, setMotionTrigger, motionTriggerPreset, motionConfirmCycles };`, sandbox);
  return sandbox.api;
}

test('shares read as plain percentages', () => {
  const { formatMotionShare } = loadHelpers();
  assert.equal(formatMotionShare(0), '0%');
  assert.equal(formatMotionShare(0.0004), '<0.1%');
  assert.equal(formatMotionShare(0.004), '0.4%');
  assert.equal(formatMotionShare(0.02), '2%');
  assert.equal(formatMotionShare(0.0135), '1.4%');
  assert.equal(formatMotionShare(0.125), '13%');
});

test('a zone meter puts the trigger mid-bar and states the reading in the trigger\'s units', () => {
  const { motionZoneMeterHtml } = loadHelpers();
  const html = motionZoneMeterHtml({
    zone_name: 'Driveway', fraction: 0.004, trigger: 0.02, state: 'moving', peak_fraction: 0.012, peak_window_seconds: 600,
  });
  assert.match(html, /Driveway/);
  assert.match(html, /0\.4% Moving · Triggers at 2%/);
  assert.match(html, /Highest in the Last 10 Minutes: 1\.2%/);
  assert.match(html, />Moving</);
  // 0.4% of a 4% full scale (twice the trigger) is a 10% bar.
  assert.match(html, /class="motion-bar" style="width: 10\.0%"/);
  assert.match(html, /motion-trigger-tick" style="left: 50%"/);
});

test('a zone above its trigger but still confirming says which check it is on', () => {
  const { motionZoneMeterHtml } = loadHelpers();
  const html = motionZoneMeterHtml({
    zone_name: 'Path', fraction: 0.03, trigger: 0.02, state: 'moving', above_trigger: true, checks_seen: 1, checks_needed: 2,
  });
  assert.match(html, /Check 1 of 2/);
  assert.match(html, /width: 75\.0%/);
  const fired = motionZoneMeterHtml({ zone_name: 'Path', fraction: 0.05, trigger: 0.02, state: 'triggered' });
  assert.match(fired, />Triggered</);
  assert.match(fired, /width: 100\.0%/, 'the bar caps at full');
});

test('the lane state is the loudest zone', () => {
  const { overallMotionZoneState } = loadHelpers();
  assert.equal(overallMotionZoneState([{ state: 'quiet' }, { state: 'moving' }]), 'moving');
  assert.equal(overallMotionZoneState([{ state: 'moving' }, { state: 'triggered' }]), 'triggered');
  assert.equal(overallMotionZoneState([{ state: 'quiet' }]), 'quiet');
});

test('a zone uses its stored trigger, else the Normal default', () => {
  const { effectiveMotionTrigger, motionTriggerPreset } = loadHelpers();
  assert.equal(effectiveMotionTrigger({ label: 'motion', trigger_fraction: 0.04 }), 0.04);
  assert.equal(effectiveMotionTrigger({ label: 'motion', min_confidence: 0.45 }), 0.02);
  assert.equal(effectiveMotionTrigger(null), 0.02);
  assert.equal(motionTriggerPreset(0.02).label, 'Normal');
});

test('setting a trigger opens the confidence window', () => {
  const { setMotionTrigger } = loadHelpers();
  const rule = { label: 'motion', min_confidence: 0.6, max_confidence: 0.9, confirm_cycles: 3 };
  setMotionTrigger(rule, 0.04);
  assert.equal(JSON.stringify(rule), JSON.stringify({
    label: 'motion', min_confidence: 0, max_confidence: 1, confirm_cycles: 3, trigger_fraction: 0.04,
  }));
  setMotionTrigger(rule, 9);
  assert.equal(rule.trigger_fraction, 0.5, 'clamped to the backend maximum');
});

test('must-last checks clamp to 1-5 and default to 2', () => {
  const { motionConfirmCycles } = loadHelpers();
  assert.equal(motionConfirmCycles({}), 2);
  assert.equal(motionConfirmCycles({ confirm_cycles: 0 }), 1);
  assert.equal(motionConfirmCycles({ confirm_cycles: 12 }), 5);
});

test('the zones editor leaves live movement to the Live page', () => {
  assert.doesNotMatch(zonesSource, /zone-motion-meter-row/, 'no live meter row in the zone editor');
  assert.doesNotMatch(zonesSource, /motionZoneMeterHtml/, 'no per-zone meter rendering in the zone editor');
  assert.doesNotMatch(zonesSource, /updateZoneMotionMeters/, 'no status-poll meter hook in the zone editor');
  assert.match(liveSource, /motionZoneMeterHtml/, 'the Live page keeps its per-zone meters');
});

test('the live lane has a per-zone meter list beside the whole-frame bar', () => {
  assert.match(liveHtml, /id="liveMotionZones"/);
  assert.match(liveHtml, /id="liveMotionFrame"/);
  assert.doesNotMatch(liveHtml, /liveMotionTriggerTick/, 'the frame-wide trigger tick compared mismatched units');
});

test('the motion engine settings use plain names', () => {
  assert.match(settingsHtml, /<summary>Advanced Motion Engine<\/summary>/);
  assert.match(settingsHtml, /Ignore Small Light Changes/);
  assert.match(settingsHtml, /data-pixel-threshold-preset="motion_pixel_threshold"/);
  assert.doesNotMatch(settingsHtml, /Motion Gate Fraction|Motion Scale Fraction|Motion Pixel Threshold/);
});

test('Alerts points motion policies at the Zones trigger instead of a confidence window', () => {
  const alertsSource = readFileSync(path.resolve(here, '../web/alerts.js'), 'utf8');
  assert.match(alertsSource, /const motionPolicy = alertType === 'object' && String\(rule\.label \|\| ''\)\.trim\(\)\.toLowerCase\(\) === 'motion';/);
  assert.match(alertsSource, /\$\{behaviour \|\| motionPolicy \? '' : `<label><span>\$\{confidenceLabel\}/);
  assert.match(alertsSource, /!behaviour && !motionPolicy \? '<label><span>Maximum Confidence/);
});
