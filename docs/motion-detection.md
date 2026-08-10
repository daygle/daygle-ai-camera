# Motion Detection Guide

This guide explains how Daygle AI Camera detects motion and objects, what each setting controls, and how to tune the system for your cameras and environment.

---

## How detection works - three layers

Every frame from every camera passes through three layers in order. Each layer builds on the previous one.

### Layer 1 - Background-subtraction motion gate

This runs on every single frame and is deliberately cheap. It shrinks the image to a small thumbnail (320×240 pixels) and compares it to a learned background model of what the camera normally sees.

**Motion Engine.** The default engine is **MOG2** - an adaptive Gaussian-mixture background model (OpenCV `BackgroundSubtractorMOG2`). Unlike a single averaged background, it models each pixel as a mixture of recent states, so it tolerates gradual light changes, swaying foliage, and flickering screens far better, and it can classify **cast shadows** separately so a moving shadow does not register as motion. A legacy single-frame **Diff** engine remains selectable (and is used automatically if your OpenCV build lacks MOG2).

**Denoise.** After the background comparison, the raw changed-pixel mask is morphologically cleaned (open then close) to erase isolated single-pixel sensor noise - a major false-positive source on IR/night cameras - and to consolidate real motion into solid blobs. This is on by default and can be disabled per install.

If enough pixels have changed enough, it declares motion and passes the frame on. If nothing significant changed at the whole-frame level, YOLO is not run from this gate alone - but per-zone motion rules (Layer 3) are still scored from the diff mask first, so motion confined to a small monitored zone can fire that zone's rule without ever opening the frame-wide gate.

**Why this matters:** YOLO inference is slow and CPU-intensive. Running it on every frame of a static, empty scene would waste CPU constantly. The pixel-diff gate means YOLO only runs when something is actually happening.

**What it produces:** A motion confidence value between 0 and 1. Low confidence means a small amount of subtle movement. High confidence means a large portion of the frame changed significantly.

---

### Layer 2 - YOLO object detection

This runs when Layer 1 says motion was detected, when a per-zone motion rule fires from the diff mask, or when a periodic scan is due (see below). It runs the full YOLO neural network on the frame and identifies specific objects - person, car, dog, etc. - with bounding boxes and confidence scores. The active model (a YOLOv8, YOLO11, or YOLO26 variant) is selected on the **ONNX** page; see [ai-detection.md](ai-detection.md).

These results are then matched against your zone rules. If a zone covers the area where an object was detected and you have a rule for that object label, an alert and/or recording fires.

---

### Layer 3 - Motion zone rules

This is an optional alert that fires from Layer 1's pixel-diff result, without caring what YOLO found.

You configure it on the **Zones** page: each area has its own **Motion detection** card with a single toggle. Flip it on and the motion rule is created with sensible defaults; use the **Sensitivity** field to set the minimum confidence, and the **Advanced** expander for cooldown, email/push, time windows, and the per-zone **Gate override** / **Scale override**. When a zone's pixel-diff confidence reaches the sensitivity threshold, the alert fires immediately - even before YOLO has identified any specific object. The system normally computes this confidence from the changed pixels inside the zone's own rectangle, so motion elsewhere in the camera view does not raise this zone's score.

**Per-zone sensitivity.** The **Gate override** and **Scale override** fields (under a motion card's **Advanced** expander) let one zone use a different pixel-diff sensitivity than the rest of the camera. Leave them blank to inherit the camera/global values. This is what lets a sensitive doorway (low gate) and a noisy tree-line (high gate) coexist on a single camera - the whole-camera **Motion Gate Fraction** / **Motion Scale Fraction** no longer have to be a compromise. The live hint under the Sensitivity slider shows the resulting "approx. X% of this zone's pixels must change", and marks the zone as a *per-zone override* when either field is set.

Use this when you want to be notified any time *anything* moves in an area, regardless of what it is.

---

## The background model

Layer 1 compares each frame against a learned background - a model of what the camera sees when nothing is happening. With the default **MOG2** engine each pixel is modelled as a mixture of Gaussians; with the legacy **Diff** engine it is a single exponential moving average. Either way, the adaptation speed is controlled by **Motion Background Alpha**.

**Important behaviour:** The background only updates when *no motion is detected* (the model is frozen while motion is above the gate). This means:

- A person actively moving in frame keeps the background frozen. They stay visible indefinitely.
- Once a person stands completely still, the pixel diff drops to zero, the gate closes, and the background slowly starts adapting toward the new scene (including the stationary person).
- After enough still frames, the person is absorbed into the background and becomes invisible to Layer 1.

This is where the **Periodic Scan** setting comes in.

---

## Periodic scan

When set to a non-zero value, the system forces a full YOLO scan every N seconds regardless of what Layer 1 says.

This solves the standing-still problem: even if a person has been absorbed into the background model and Layer 1 is quiet, YOLO runs on schedule and can still detect the person standing there.

**What fires on a periodic scan:**
- Object rules (person, car, etc.) - yes, as normal
- Motion zone rules - **no**. Since no pixel motion was detected, the motion confidence is zero and motion rules stay silent.

**When to use it:**
- Set to `30` - `60` seconds if you need to track whether someone remains present in an area
- Set to `120` seconds or higher if CPU is limited and you mainly care about the moment of entry
- Leave at `0` (disabled) if you only need to detect activity, not sustained presence

---

## Settings reference

All of these live under **Settings → Detection & Live → Live Performance**. The **Motion Engine**, **Denoise**, **Shadow Suppression**, **Periodic Scan Interval**, and the low-level motion tuning values (**Motion Pixel Threshold**, **Motion Gate Fraction**, **Motion Scale Fraction**, **Motion Background Alpha**, **Motion Frame Width**, and **Motion Frame Height**) are grouped under the **Advanced Motion Tuning** disclosure on that card.

These are global defaults. You can override the four motion gate tuning values - **Motion Pixel Threshold**, **Motion Gate Fraction**, **Motion Scale Fraction**, and **Motion Background Alpha** - for an individual camera from **Cameras → Edit Camera → Advanced → Motion Detection Overrides**. Blank override fields use the global Live Performance value.

### Motion Engine

Selects the background-subtraction algorithm. **MOG2** (default, recommended) is an adaptive Gaussian-mixture model that handles gradual light changes, swaying foliage, and cast shadows. **Diff** is the legacy single-frame adaptive-background model; it is also used automatically when the OpenCV build lacks MOG2.

Default: `MOG2`

### Denoise

Morphologically cleans the changed-pixel mask (open then close) to remove isolated single-pixel sensor noise and consolidate motion into solid regions. Leave enabled unless you are deliberately trying to catch sub-pixel-scale changes.

Default: `Enabled`

### Shadow Suppression

Rejects MOG2-classified cast shadows so a moving shadow does not register as motion. **MOG2 only** (has no effect with the Diff engine). Disable on very dark or IR scenes where a genuine subject can be misread as a shadow and dropped.

Default: `Enabled`

### Detection Interval (s)

How often each camera is checked. At `0.5` seconds, each camera is checked twice per second. Lower values mean faster alerts but more CPU usage.

Default: `0.5`

---

### Confirm Frames / Confirm Window

A temporal "N of the last M" gate that suppresses an object until it has been
seen in several consecutive detection cycles. It reduces single-frame false
positives (a flicker briefly misread as a `cat`, a `person`, or any other
class) without raising your confidence thresholds, so you can keep a lower
per-label confidence and still avoid noise.

- **Confirm Frames** is *N* - how many recent detection cycles must contain the
  object's label before it can alert or record. `1` disables the gate (react on
  the first frame, the historical behavior).
- **Confirm Window** is *M* - how many recent cycles the count looks back over.
  Only applies when Confirm Frames is above `1`, and is automatically clamped up
  to at least Confirm Frames.

The gate applies to every object label uniformly, and counts only cycles that
actually ran YOLO (a quiet frame with no motion is skipped and does not count).
It runs *after* zone/label filtering, so the window only tracks objects the
camera is configured to care about. Motion rules are gated separately and are
unaffected.

**Tuning tip:** If a distant or low-confidence subject flickers in and out of
detection, try Confirm Frames `2` with Confirm Window `3`. Raising Confirm
Frames adds a small latency (the object must persist for N cycles before the
first alert), so keep it low - `2` or `3` is usually enough.

Defaults: `1` (off) / `3`

---

### Periodic Scan Interval (s)

How often a full YOLO scan runs regardless of pixel-diff motion. Set to `0` to disable.

See the [Periodic scan](#periodic-scan) section above.

Default: `0` (disabled)

---

### Motion Pixel Threshold

How much a single pixel's intensity must change (on a 0-255 scale) to be counted as a changed pixel.

- **Too low:** Sensor noise, IR flicker, and minor lighting changes trigger the gate constantly
- **Too high:** Subtle or distant motion is missed

Default: `30`

**Tuning tip:** On IR or night-vision cameras, raise this to `40`-`60` to filter out sensor noise.

---

### Motion Gate Fraction

The minimum fraction of pixels that must change before motion is declared. `0.005` means 0.5% of the thumbnail pixels must exceed the pixel threshold.

- **Too low:** A flickering light in one corner of the frame triggers the gate constantly
- **Too high:** Small or distant subjects (a person at the far end of a long driveway) are missed

Default: `0.005`

**Tuning tip:** If you have a scene with a tree or flag in the corner that constantly triggers alerts, raise this to `0.008` or `0.01`.

---

### Motion Scale Fraction

The pixel change fraction that maps to 100% motion confidence. At `0.03`, if 3% of pixels changed, confidence is 1.0. At 1.5%, confidence is 0.5.

This does not affect whether motion fires - that is controlled by Gate Fraction. It only affects the confidence score that Layer 3 motion rules compare against.

- **Lower:** More sensitive confidence scoring - small movements get higher scores
- **Higher:** Only large, obvious movements score close to 1.0

Default: `0.03`

---

### Motion Background Alpha

How fast the background model adapts to scene changes when no motion is detected. `0.05` means each new frame contributes 5% to the background.

- **Higher:** Background adapts faster - a light turning on is absorbed quickly, but a stationary subject is also absorbed more quickly
- **Lower:** Background adapts slowly - more stable, but it takes longer to settle after a genuine scene change (lighting shift, camera moved)

Default: `0.05`

---

### Motion Frame Width / Motion Frame Height

The size in pixels of the thumbnail used for the Layer 1 pixel-diff. Larger dimensions give finer per-zone precision but cost more CPU on every frame.

**Important:** changing either value resets every camera's background model, so the gate re-learns the scene on the next few frames.

Defaults: `320` × `240`

---

## Tuning for common situations

### Noisy IR / night-vision camera

Symptom: YOLO runs constantly, live detection status shows "motion detected" on every quiet frame.

1. Raise **Motion Pixel Threshold** from `30` to `50`
2. If still triggering, raise **Motion Gate Fraction** from `0.005` to `0.008`
3. Save and watch the detection status - it should settle to "no motion detected" during quiet periods

---

### Missing detections of distant subjects

Symptom: A person at the far end of the driveway isn't triggering alerts.

1. Lower **Motion Gate Fraction** from `0.005` to `0.001` - fewer pixels need to change
2. Lower **Motion Pixel Threshold** from `30` to `15` - subtler pixel changes count
3. Consider also lowering the object rule's **min confidence** in zone settings

---

### Person stands still and disappears from detection

Symptom: Alerts fire when someone enters but stop after they stand still for a few minutes.

Enable **Periodic Scan Interval** - set to `30` or `60` seconds. YOLO will continue running on schedule and will detect the stationary person.

---

### Tree or flag constantly triggering motion

Symptom: A waving tree or flag in the corner of frame keeps triggering motion alerts even with nothing happening.

1. Raise **Motion Gate Fraction** - the tree may change only 0.5% of pixels; requiring 1% filters it out
2. Alternatively, adjust your detection zone in Zones settings to exclude that corner of the frame

---

### Lights turning on/off causing false motion

Symptom: Motion fires when a room light turns on, even with no person present.

This is correct behaviour - lights genuinely change a large fraction of pixels. Two options:
- Raise **Motion Pixel Threshold** so minor brightness changes below a threshold don't count
- Use a zone that only covers the area you care about (a doorway, not the whole room), and set object rules rather than motion rules so only identified people/objects trigger alerts

---

## How the layers interact - example timeline

```
00:00  Camera is quiet. Background model is stable.
       Layer 1: no motion. YOLO skipped. ✓

00:05  Person enters frame and walks toward door.
       Layer 1: motion detected (confidence 0.72)
       Layer 3: motion zone rule fires → motion alert ✓
       Layer 2: YOLO runs → "person 91%" → person rule alert + recording starts ✓
       Background model: FROZEN (motion detected)

00:20  Person reaches door and stops. Stands still.
       Layer 1: pixel diff drops below gate → no motion
       Layer 3: silent
       Layer 2: YOLO skipped
       Background model: starts slowly adapting toward stationary person

00:50  (Periodic scan = 30s - fires at 00:50)
       Layer 1: still quiet (person's zone shows near-zero pixel diff)
       Layer 2: FORCED YOLO scan → "person 88%" → detection continues ✓
       Layer 3: silent (no pixel motion in any zone)

01:20  (Periodic scan fires again)
       Layer 2: FORCED YOLO scan → "person 84%" → detection continues ✓

03:00  Person leaves frame.
       Layer 1: motion detected briefly as they exit → alert fires
       (Next periodic scan: YOLO finds nothing → detections stop)

03:30  Scene is quiet again. Background resets to empty doorway.
```

---

## Zone motion rules vs object rules - when to use each

| Use a **motion rule** when… | Use an **object rule** when… |
|---|---|
| You want to know anything moved, regardless of what it is | You only want alerts for specific things (person, car, etc.) |
| You need the fastest possible alert (fires before YOLO finishes) | You need to avoid false alerts from animals, shadows, or lights |
| CPU is very limited and you want minimal YOLO calls | Accuracy matters more than speed |
| You're monitoring a restricted area where any movement is suspicious | You're monitoring a public area with lots of expected background activity |

You can combine both on the same zone - the motion rule fires first (fast), and the object rule fires when YOLO confirms what was detected.

---

## Known limitations

### Per-zone pixel-diff and the fallback path

Motion confidence is computed independently for each zone by slicing the pixel-diff mask to that zone's bounding rectangle. Movement in Zone A does not inflate Zone B's confidence score.

When a zone fires, the green overlay box on snapshots / playback is the bounding box of the changed pixels *inside* that zone (not the whole zone), so the overlay points at where the movement happened. Without a diff mask (first frame, fail-open, forced scan) the box falls back to the zone's full rectangle.

This per-zone path requires the internal diff mask - a 240×320 boolean array produced by the background comparison. In rare error conditions (first frame after startup, numpy exception), the diff mask is unavailable and the system falls back to the frame-wide confidence score for all zones. The fallback is temporary and resolves on the next frame.

### Shadows and light reflections

With the default **MOG2** engine and **Shadow Suppression** on, cast shadows are classified separately and dropped, so a person's moving shadow no longer triggers Layer 1 by itself. This is not perfect - a hard-edged shadow or a headlight sweeping a wall can still be misread, and on very dark/IR scenes shadow suppression can occasionally drop a genuine subject (disable it there).

**Workaround:** Keep **Shadow Suppression** on for daytime/colour scenes; raise **Motion Pixel Threshold** to filter minor brightness changes; and use zone geometry to avoid placing motion zones in areas prone to shadows or reflections. Using object rules (person/car) instead of motion rules also avoids this, since YOLO has semantic understanding that pixel-diff does not.

### First frame after startup or camera reconnect

When the app starts or a camera reconnects after an outage, the first delivered frame becomes the baseline background. If a person is already standing in the scene at that moment, they will not be detected by the motion gate until they move. The **Periodic Scan** setting mitigates this: the first scheduled scan runs YOLO regardless of the gate and will detect anyone present.
