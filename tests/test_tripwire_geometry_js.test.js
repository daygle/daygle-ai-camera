// Unit tests for the behavioural tripwire (directional line-crossing) geometry
// and normalisation helpers in web/utils.js. These back the Zones-canvas line
// editor (web/zones.js): the drawn line, its direction arrow, and which lines
// are "valid" must all agree with the backend (app/behaviour.py +
// app/zone_schema.py::normalize_zone_tripwire) so a line drawn in the browser
// fires the same crossings the server counts.
//
// utils.js is a classic browser script that reaches for `window` at load, so
// we load it into a vm context behind a lightweight window stub - the same
// pattern as test_motion_boundary_js.test.js / test_since_range_helpers.test.js.
//
// Run with:
//   node --test tests/test_tripwire_geometry_js.test.js

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
vm.runInContext(utilsSource, sandbox);

const ui = sandbox.window.daygleUi;
assert.ok(ui && typeof ui.normalizeTripwire === 'function',
  'utils.js should expose the tripwire helpers on window.daygleUi');

const {
  tripwireDefaultLine,
  normalizeTripwire,
  tripwireForwardNormal,
  tripwireOrientation,
  tripwireMidpoint,
  tripwirePoint,
  TRIPWIRE_DIRECTIONS,
} = ui;

// Values built inside the vm realm carry that realm's Array/Object prototypes,
// which deepStrictEqual's prototype check rejects against the test realm's.
// Round-trip through JSON to re-home them first (same pattern as
// test_motion_boundary_js.test.js).
const rehome = (value) => (value == null ? value : JSON.parse(JSON.stringify(value)));

// ─── tripwireDefaultLine ────────────────────────────────────────────────────

test('default line spans a zone horizontally through its middle', () => {
  const zone = { x: 0.2, y: 0.4, width: 0.6, height: 0.2 };
  const line = tripwireDefaultLine(zone);
  // Horizontal: both endpoints at the vertical centre of the zone.
  assert.equal(line.a.y, 0.5);
  assert.equal(line.b.y, 0.5);
  // Inside the zone's horizontal span, a before b, and not a point.
  assert.ok(line.a.x >= 0.2 && line.b.x <= 0.8, 'endpoints stay inside the zone');
  assert.ok(line.b.x - line.a.x > 0.1, 'the default line has real length');
});

test('default line derives bounds from polygon points when present', () => {
  const zone = {
    points: [{ x: 0.1, y: 0.1 }, { x: 0.5, y: 0.1 }, { x: 0.5, y: 0.9 }, { x: 0.1, y: 0.9 }],
  };
  const line = tripwireDefaultLine(zone);
  assert.equal(line.a.y, 0.5); // midY of the polygon bbox
  assert.ok(line.a.x >= 0.1 && line.b.x <= 0.5);
});

test('default line for a degenerate zone is never a point', () => {
  const line = tripwireDefaultLine({ x: 0.5, y: 0.5, width: 0, height: 0 });
  const dx = Math.abs(line.a.x - line.b.x);
  const dy = Math.abs(line.a.y - line.b.y);
  assert.ok(dx > 1e-6 || dy > 1e-6, 'coincident endpoints would be rejected by the backend');
  assert.ok(normalizeTripwire({ a: line.a, b: line.b }) !== null, 'default line normalises to a valid tripwire');
});

// ─── normalizeTripwire ──────────────────────────────────────────────────────

test('a valid tripwire round-trips with defaults filled in', () => {
  const wire = normalizeTripwire({ a: { x: 0.2, y: 0.5 }, b: { x: 0.8, y: 0.5 } });
  assert.equal(wire.enabled, true);
  assert.equal(wire.name, 'Tripwire');
  assert.equal(wire.direction, 'both');
  assert.deepEqual(rehome(wire.labels), []);
  assert.equal(wire.cooldown_seconds, 30);
  assert.equal(wire.record_on_detect, true);
  assert.equal(wire.email_enabled, false);
  assert.equal(wire.push_enabled, false);
  assert.deepEqual(rehome(wire.email_recipients), []);
  // Notify quiet-hours default to unset (any time).
  assert.equal(wire.notify_start, null);
  assert.equal(wire.notify_end, null);
});

test('notify quiet-hours are preserved and coerced to HH:MM (or null)', () => {
  // These are edited on the Alerts page; normalizeTripwire (used by the Zones
  // save path) must keep them so a Zones save never wipes delivery settings.
  const wire = normalizeTripwire({
    a: { x: 0, y: 0 }, b: { x: 1, y: 0 },
    notify_start: '9:05', notify_end: '17:30',
  });
  assert.equal(wire.notify_start, '09:05'); // zero-padded
  assert.equal(wire.notify_end, '17:30');
  // Invalid times drop to null rather than corrupt the window.
  const bad = normalizeTripwire({ a: { x: 0, y: 0 }, b: { x: 1, y: 0 }, notify_start: '25:00', notify_end: 'nope' });
  assert.equal(bad.notify_start, null);
  assert.equal(bad.notify_end, null);
});

test('a degenerate line (coincident endpoints) is rejected', () => {
  assert.equal(normalizeTripwire({ a: { x: 0.5, y: 0.5 }, b: { x: 0.5, y: 0.5 } }), null);
});

test('a missing or malformed line is rejected', () => {
  assert.equal(normalizeTripwire(null), null);
  assert.equal(normalizeTripwire({}), null);
  assert.equal(normalizeTripwire({ a: { x: 0.5, y: 0.5 } }), null);
  assert.equal(normalizeTripwire({ a: 'x', b: 'y' }), null);
});

test('an unknown direction falls back to both; known ones are kept', () => {
  assert.equal(normalizeTripwire({ a: { x: 0, y: 0 }, b: { x: 1, y: 1 }, direction: 'sideways' }).direction, 'both');
  for (const dir of TRIPWIRE_DIRECTIONS) {
    assert.equal(normalizeTripwire({ a: { x: 0, y: 0 }, b: { x: 1, y: 1 }, direction: dir.toUpperCase() }).direction, dir);
  }
});

test('cooldown clamps to >= 0 and defaults to 30 when absent or non-numeric', () => {
  assert.equal(normalizeTripwire({ a: { x: 0, y: 0 }, b: { x: 1, y: 0 }, cooldown_seconds: -5 }).cooldown_seconds, 0);
  assert.equal(normalizeTripwire({ a: { x: 0, y: 0 }, b: { x: 1, y: 0 }, cooldown_seconds: 90 }).cooldown_seconds, 90);
  assert.equal(normalizeTripwire({ a: { x: 0, y: 0 }, b: { x: 1, y: 0 } }).cooldown_seconds, 30);
  assert.equal(normalizeTripwire({ a: { x: 0, y: 0 }, b: { x: 1, y: 0 }, cooldown_seconds: 'nope' }).cooldown_seconds, 30);
});

test('labels are lower-cased and de-duplicated; recipients normalise', () => {
  const wire = normalizeTripwire({
    a: { x: 0, y: 0 }, b: { x: 1, y: 0 },
    labels: ['Car', 'car', ' Person ', ''],
    email_enabled: true,
    email_recipients: 'a@example.com, , b@example.com',
  });
  assert.deepEqual(rehome(wire.labels), ['car', 'person']);
  assert.equal(wire.email_enabled, true);
  assert.deepEqual(rehome(wire.email_recipients), ['a@example.com', 'b@example.com']);
});

test('endpoints clamp to 0..1 and round to 4 dp', () => {
  const wire = normalizeTripwire({ a: { x: -0.5, y: 1.5 }, b: { x: 0.123456, y: 0.5 } });
  assert.deepEqual(rehome(wire.a), { x: 0, y: 1 });
  assert.equal(wire.b.x, 0.1235);
});

// ─── direction geometry (arrow ↔ backend crossing side) ─────────────────────

test('forward normal is a unit vector, or null for a degenerate line', () => {
  const n = tripwireForwardNormal({ x: 0.2, y: 0.5 }, { x: 0.8, y: 0.5 });
  assert.ok(Math.abs(Math.hypot(n.x, n.y) - 1) < 1e-9);
  assert.equal(tripwireForwardNormal({ x: 0.5, y: 0.5 }, { x: 0.5, y: 0.5 }), null);
});

test('the forward arrow points to the RIGHT side of the line (backend orientation < 0)', () => {
  // "forward" = an object crossing from the LEFT of a->b to its RIGHT, which is
  // exactly what app/behaviour.py fires on. The drawn arrow must therefore land
  // on the side where tripwireOrientation < 0 for any line orientation.
  const cases = [
    [{ x: 0.2, y: 0.5 }, { x: 0.8, y: 0.5 }], // west -> east
    [{ x: 0.5, y: 0.8 }, { x: 0.5, y: 0.2 }], // south -> north
    [{ x: 0.2, y: 0.2 }, { x: 0.8, y: 0.8 }], // diagonal
  ];
  for (const [a, b] of cases) {
    const n = tripwireForwardNormal(a, b);
    const mid = tripwireMidpoint(a, b);
    const tip = { x: mid.x + n.x * 0.1, y: mid.y + n.y * 0.1 };
    assert.ok(tripwireOrientation(a, b, tip) < 0, 'arrow tip is on the forward (right) side');
    // And the opposite side is "left" (orientation > 0).
    const back = { x: mid.x - n.x * 0.1, y: mid.y - n.y * 0.1 };
    assert.ok(tripwireOrientation(a, b, back) > 0, 'reverse side is the backward (left) side');
  }
});

test('tripwirePoint accepts {x,y} and [x,y], clamps, and rejects junk', () => {
  assert.deepEqual(rehome(tripwirePoint({ x: 0.3, y: 0.7 })), { x: 0.3, y: 0.7 });
  assert.deepEqual(rehome(tripwirePoint([0.3, 0.7])), { x: 0.3, y: 0.7 });
  assert.deepEqual(rehome(tripwirePoint([2, -1])), { x: 1, y: 0 });
  assert.equal(tripwirePoint('nope'), null);
  assert.equal(tripwirePoint({ x: 'a', y: 0.5 }), null);
});
