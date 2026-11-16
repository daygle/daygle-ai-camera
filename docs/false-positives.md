# False positives

A false positive is a detection of something that was not really there: a
shadow read as a person, a bush read as a dog, a reflection read as a car.
False positives fill the Events, Snapshots and Recordings lists and use disk
space. This feature finds and marks them, lets you mark them yourself, and can
purge them automatically.

Everything is off until an admin turns it on under **Intelligence → AI →
False Positives**.

## Marking by hand

Admins see a **False Positive** button on each object event (Events page),
each object snapshot (Snapshots page) and each recording with detections
(Recordings page).

- **False Positive** marks it. The row shows a **🚫 False Positive · User**
  badge.
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

- **Every object rejected**: the event is marked **🚫 False Positive · AI**.
  Hover or tap the ⓘ beside the badge to see the model's reasons.
- **Any object confirmed**: the event is recorded as real.
- **Unsure** (too dark, blurry or small to tell): recorded as real, so it is
  never purged. An event with more objects than one check covers (three) is
  also treated as unsure.
- **Error** (the reply could not be read): left unmarked.

How it runs:

- **It never delays alerts.** It only starts while the AI queue is idle. It
  gives the model up to alerts, searches and tests, the same way background
  descriptions do.
- **It reuses alert verification.** When an alert was already verified, the
  answers for those objects are reused and not asked again.
- **A user decision always wins.** The AI only marks events nobody has marked.
- **What is checked:** live camera events with a snapshot and at least one
  object, from the last 24 hours, newest first. Motion, face and sound events
  have no object to check. Uploaded clips are not checked.
- **Cameras:** tick cameras under **AI Check Cameras** to check only those.
  None ticked means every camera.
- **When the model is down,** checking stops and starts again on its next
  15-second round. Events are not lost; they wait.
- The verdict is stored in the event metadata as `ai_false_positive_check`
  (status, each object's answer and reason, model, time). Every AI false
  positive is logged on the **AI Log** page.

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
| `POST` | `/api/events/{id}/false-positive` | Body `{"false_positive": true \| false \| null}` (admin). `null` clears the decision. |
| `POST` | `/api/recordings/{id}/false-positive` | Same body; applies to every event in the clip (admin). |

`/api/events`, `/api/snapshots` and `/api/recordings` take
`false_positive=only` or `false_positive=hide`. Events carry
`false_positive` (`1`, `0` or `null`), `false_positive_by` (`ai` or `user`),
`false_positive_at` and `false_positive_reason`. Recordings carry
`false_positive`: `null`, or `{"by": "ai" | "user" | "mixed", "reason": "..."}`.
