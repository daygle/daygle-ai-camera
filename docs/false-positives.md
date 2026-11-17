# False positives

A false positive is a detection with no real object there at all: a shadow
read as a person, a bush read as a dog, a reflection read as a car.

A real object under the wrong label (a cat the detector read as a dog) is
not a false positive. The AI check **reclassifies** it instead (see below),
and it is never purged.
False positives fill the Events, Snapshots and Recordings lists and use disk
space. This feature finds and marks them, lets you mark them yourself, and can
purge them automatically.

Everything is off until an admin turns it on under **Intelligence → AI →
False Positives**.

## Marking by hand

Every signed-in user (admins and viewers) sees a **False Positive** button on
each object event (Events page), each object snapshot (Snapshots page) and
each recording with detections (Recordings page). In the phone app, long-press
an Events row. Every mark is stored with who made it (their username and
whether they were an admin or a viewer) and written to the audit log. A
viewer's mark can lead to deletion, so **Purge Viewer-Marked** (below) can
leave viewers' marks alone; the purge settings and **Purge Now** stay
admin-only.

- **False Positive** marks it. The row shows a **🚫 False Positive · sam**
  badge naming who marked it (marks from before names were stored read
  **User**).
- On a marked row the button reads **Not False Positive**. It records the
  detection as real, so the AI check leaves it alone from then on.
- Marking a **recording** marks every event in that clip.
- A snapshot is an event's frame, so marking either one marks both.

Viewers see the badges but not the buttons. Every change is written to the
audit log.

## The AI check

With **AI Check All Object Events** on, the vision model from the
[AI page](ai-verification.md) checks every live object event in the
background. For each detected object it is asked the same question as alert
verification: *is a real one actually visible?*

- **Nothing real there** (every object rejected, and the model names no
  real thing instead): the event is marked **🚫 False Positive · AI**.
  Hover or tap the ⓘ beside the badge to see the model's reasons.
- **Something real, under the wrong label** (a cat the detector read as a
  dog): the event is **reclassified**. Its detection becomes `cat`, its
  recording gains the `cat` label (and loses `dog` unless another event in
  the clip saw a dog), and a recording triggered by the dog now says cat.
  The 🔄 mark after the Cat pill keeps the original visible (hover or tap it
  for "detected as Dog"); events from before per-detection verdicts show a
  **🔄 Reclassified from Dog** badge instead. It is
  recorded as real and **never purged**.
- **A real animal or vehicle the detector has no class for**: recorded as
  real, with the label left as it is.

  For each rejected object the model is asked what it is instead: a
  person, cat, dog, bird, horse, sheep, cow or bear, a car, truck, bus,
  motorcycle or bicycle (the detector's own classes, so zones and alert
  rules can match the new label), or "another animal" / "another vehicle".
  Reclassifying a past event does not send its alerts again; live alerts
  are relabelled by alert verification (see
  [ai-verification.md](ai-verification.md#relabelling-a-rejected-alert)).
- **Any object confirmed**: the event is recorded as real.
- **Unsure** (too dark, blurry or small to tell): recorded as real, so it is
  never purged. An event with more objects than one check covers (three) is
  also treated as unsure.
- **Error** (the reply could not be read): left unmarked.

How it runs:

- **It never delays alerts.** It only starts while the AI queue is idle. It
  gives the model up to alerts, searches and tests, the same way background
  descriptions do.
- **It reuses alert verification.** When an alert was already verified, a
  confirmation, or a rejection that said what the object really is, is reused
  and not asked again. A bare "no" from alert verification is asked again with
  the "what is it instead" question.
- **A user decision always wins.** The AI only marks events nobody has marked.
- **What is checked:** live camera events with a snapshot and at least one
  object, from the last 24 hours, newest first. Motion, face and sound events
  have no object to check. Uploaded clips are not checked.
- **Cameras:** tick cameras under **AI Check Cameras** to check only those.
  None ticked means every camera.
- **When the model is down,** checking stops and starts again on its next
  15-second round. Events are not lost; they wait.
- The verdict is stored in the event metadata as `ai_false_positive_check`
  (status, each object's answer and reason, `reclassified` mapping each
  corrected label to its new one, model, time). Every AI false positive and
  reclassification is logged on the **AI Log** page.
- **A user's decision always wins**: an event a user already marked is
  never reclassified.
- **Upgrading:** AI false positives recorded before the check asked what an
  object really is are returned to undecided on start-up, so they are not
  purged. Ones from the last 24 hours are checked again.

## Recordings

A recording can contain many events. It counts as a false positive when:

- at least one of its events is a false positive, and
- none of its other events is real (marked real, or an unmarked event with an
  object, a face, a sound or an alert).

A motion-only blip inside the clip does not stop it counting.

## Purging

With **Purge False Positives** on, false-positive recordings and snapshots
are deleted once they are older than **Purge After** (1 to 720 hours,
default 24). The delay gives you time to review them and un-mark any real
ones first.

- It runs with the periodic retention sweep, about every 15 minutes.
- A false-positive recording is deleted with all of its events, files and
  sidecars. A false-positive event without a recording (snapshot only) is
  deleted with its snapshot.
- **Purge AI-Marked** chooses whether AI-marked false positives are purged
  too, or only those a user marked.
- **Purge Viewer-Marked** chooses whether false positives a viewer marked are
  purged too, or only those an admin (or the AI) marked. A clip a viewer
  marked is then kept until an admin marks it too. Marks from before roles
  were stored count as an admin's.
- Uploaded clips are never purged.
- **Purge Now** runs the purge at once, even with purging off. The Purge
  After delay still applies.
- Purged recordings and snapshots cannot be recovered.

The line at the top of the section shows how many events were marked in the
last 7 days and how many recordings and snapshot-only events the purge would
delete now.

## Filtering

The Events, Snapshots and Recordings filter bar has a **False Positives**
filter: **Show all**, **Hide false positives** or **Only false positives**.
Use **Only** to review what the purge will delete.

## API

| Method | Path | Notes |
|---|---|---|
| `GET` / `PUT` | `/api/settings/false-positives` | Settings (admin). |
| `GET` | `/api/settings/false-positives/stats?days=7` | Counts (admin). |
| `POST` | `/api/settings/false-positives/purge` | Purge now (admin). |
| `POST` | `/api/events/{id}/false-positive` | Body `{"false_positive": true \| false \| null}`. `null` clears the decision. Any signed-in user. |
| `POST` | `/api/recordings/{id}/false-positive` | Same body; applies to every event in the clip. Any signed-in user. |

`/api/events`, `/api/snapshots` and `/api/recordings` take
`false_positive=only` or `false_positive=hide`. Events carry
`false_positive` (`1`, `0` or `null`), `false_positive_by` (`ai` or `user`),
`false_positive_at` and `false_positive_reason`. Recordings carry
`false_positive`: `null`, or `{"by": "ai" | "user" | "mixed", "reason": "..."}`.

See also [the detector and the AI model, together](ai-and-detector.md) for
per-detection verdicts, tag evidence and the one-request check.
