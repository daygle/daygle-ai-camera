# Motion Detection Guide

This guide explains how Daygle AI Camera detects motion and objects, what each setting controls, and how to tune the system for your cameras and environment.

---

## How detection works - three layers

Every frame from every camera passes through three layers in order. Each layer builds on the previous one.

### Layer 1 - Pixel-diff motion gate

This runs on every single frame and is deliberately cheap. It shrinks the image to a small thumbnail (160×120 pixels), converts it to greyscale, and compares it to a learned background model of what the camera normally sees.

If enough pixels have changed enough, it declares motion and passes the frame on. If nothing significant changed, the frame is dropped and nothing else runs.

**Why this matters:** YOLO inference is slow and CPU-intensive. Running it on every frame of a static, empty scene would waste CPU constantly. The pixel-diff gate means YOLO only runs when something is actually happening.

**What it produces:** A motion confidence value between 0 and 1. Low confidence means a small amount of subtle movement. High confidence means a large portion of the frame changed significantly.

---

### Layer 2 - YOLO object detection

This only runs when Layer 1 says motion was detected (or when a periodic scan is due - see below). It runs the full YOLOv8 neural network on the frame and identifies specific objects - person, car, dog, etc. - with bounding boxes and confidence scores.

These results are then matched against your zone rules. If a zone covers the area where an object was detected and you have a rule for that object label, an alert and/or recording fires.

---

### Layer 3 - Motion zone rules

This is an optional alert that fires from Layer 1's pixel-diff result, without caring what YOLO found.

You configure it by adding a rule with the label **motion** to a zone. When that zone's pixel-diff confidence reaches the rule's minimum threshold, the alert fires immediately - even before YOLO has identified any specific object. The system normally computes this confidence from the changed pixels inside the zone's own rectangle, so motion elsewhere in the camera view does not raise this zone's score.

Use this when you want to be notified any time *anything* moves in an area, regardless of what it is.

---

## The background model

Layer 1 compares each frame against a learned background - a model of what the camera sees when nothing is happening. The background slowly adapts over time using an exponential moving average, controlled by **Motion Background Alpha**.

**Important behaviour:** The background only updates when *no motion is detected*. This means:

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

All of these are found in **Settings → Live Detection**.

These are global defaults. You can override the four motion gate tuning values - **Motion Pixel Threshold**, **Motion Gate Fraction**, **Motion Scale Fraction**, and **Motion Background Alpha** - for an individual camera from **Cameras → Edit Camera → Advanced → Motion Detection Overrides**. Blank override fields use the global Live Detection value.

### Detection Interval (s)

How often each camera is checked. At `0.5` seconds, each camera is checked twice per second. Lower values mean faster alerts but more CPU usage.

Default: `0.5`

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

The minimum fraction of pixels that must change before motion is declared. `0.003` means 0.3% of the thumbnail pixels must exceed the pixel threshold.

- **Too low:** A flickering light in one corner of the frame triggers the gate constantly
- **Too high:** Small or distant subjects (a person at the far end of a long driveway) are missed

Default: `0.003`

**Tuning tip:** If you have a scene with a tree or flag in the corner that constantly triggers alerts, raise this to `0.008` or `0.01`.

---

### Motion Scale Fraction

The pixel change fraction that maps to 100% motion confidence. At `0.10`, if 10% of pixels changed, confidence is 1.0. At 5%, confidence is 0.5.

This does not affect whether motion fires - that is controlled by Gate Fraction. It only affects the confidence score that Layer 3 motion rules compare against.

- **Lower:** More sensitive confidence scoring - small movements get higher scores
- **Higher:** Only large, obvious movements score close to 1.0

Default: `0.10`

---

### Motion Background Alpha

How fast the background model adapts to scene changes when no motion is detected. `0.05` means each new frame contributes 5% to the background.

- **Higher:** Background adapts faster - a light turning on is absorbed quickly, but a stationary subject is also absorbed more quickly
- **Lower:** Background adapts slowly - more stable, but it takes longer to settle after a genuine scene change (lighting shift, camera moved)

Default: `0.05`

---

## Tuning for common situations

### Noisy IR / night-vision camera

Symptom: YOLO runs constantly, live detection status shows "motion detected" on every quiet frame.

1. Raise **Motion Pixel Threshold** from `30` to `50`
2. If still triggering, raise **Motion Gate Fraction** from `0.003` to `0.008`
3. Save and watch the detection status - it should settle to "no motion detected" during quiet periods

---

### Missing detections of distant subjects

Symptom: A person at the far end of the driveway isn't triggering alerts.

1. Lower **Motion Gate Fraction** from `0.003` to `0.001` - fewer pixels need to change
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

This per-zone path requires the internal diff mask - a 120×160 boolean array produced by the background comparison. In rare error conditions (first frame after startup, numpy exception), the diff mask is unavailable and the system falls back to the frame-wide confidence score for all zones. The fallback is temporary and resolves on the next frame.

### Shadows and light reflections

Pixel-diff cannot distinguish a person's shadow from the person themselves, or a car headlight sweeping across a wall from an actual intruder. These register as pixel changes and can trigger Layer 1.

**Workaround:** Raise **Motion Pixel Threshold** to filter minor brightness changes, and use zone geometry to avoid placing motion zones in areas prone to shadows or reflections. Using object rules (person/car) instead of motion rules also avoids this, since YOLO has semantic understanding that pixel-diff does not.

### First frame after startup or camera reconnect

When the app starts or a camera reconnects after an outage, the first delivered frame becomes the baseline background. If a person is already standing in the scene at that moment, they will not be detected by the motion gate until they move. The **Periodic Scan** setting mitigates this: the first scheduled scan runs YOLO regardless of the gate and will detect anyone present.
