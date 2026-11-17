# The detector and the AI model, together

Two models look at your cameras:

- the **object detector** (ONNX) runs on every frame and says *what* is
  there (`person`, `cat`, `car`) and *where*;
- the **vision model** (the AI page's model server) looks at event
  snapshots and answers questions in words: is this really a person, what is
  in the picture, what is it carrying.

This page describes how they share what they know. Everything here builds on
[AI verification](ai-verification.md) and [false positives](false-positives.md).

## One vocabulary

The model writes "kitten", "puppy", "sedan" or "courier"; the detector says
`cat`, `dog`, `car` and `person`. AI tags are stored under the detector's
word when one fits (a "kitten" tag is stored as `cat`), so:

- the Recordings label filter lists one **Cat**, not Cat and a separate
  "Kitten (AI tag)";
- a tag that repeats a detection is hidden as redundant;
- tags can confirm or contradict detections (below).

AI tag alert rules still see the model's own words as well, so a rule
watching for "kitten" keeps working. Words that name no detector class
("ladder", "parcel", "hi-vis vest") are kept as they are.

## One verdict per detection

Every detection now carries the AI's verdict, whichever check gave it (alert
verification or the false-positive check):

| Mark | Meaning |
|---|---|
| ✓ | The AI confirmed it. |
| 🔄 | The AI reclassified it (a cat detected as a dog); hover for the original label. |
| ✗ | The AI says it is not there (or is something else with no detector class). |
| ? | The AI could not tell. |

The mark follows the detection's pill on the Events, Snapshots and Recordings
pages; hover or focus it for the reason. A recording shows the strongest
verdict for each label across its events. On the Events page, the **AI
Check** filter has **AI Confirmed Object** and **AI Rejected Object**.

## One request per event

With the false-positive check on, an event that is being described anyway
gets its check in the same model request. The image is the slow part of a
request, so this roughly halves the model time per event and the verdict
arrives with the description instead of later.

Alert events are covered too: with the false-positive check on, alert
verification asks what a rejected object is from every detector class (after
the zone's own rule labels), so its answer settles the false-positive check
and the event is not sent to the model again. Only labels with an alert rule
in the zone ever raise a relabelled alert; the other classes only label the
detection.

## AI tags as evidence

When an event's tags name a class of the same kind as a detection but not
the detection itself (the detector said `dog`, the tags say "kitten"), the
event is flagged. The false-positive check looks at flagged events first and
offers the tagged class first when it asks what the object really is. Two
different kinds in one picture (a dog and a car) contradict nothing.

## AI Insights

**Intelligence → AI → AI Insights** turns the verdicts into suggestions:

- **Minimum Confidence.** When the AI keeps rejecting a camera's
  low-confidence detections of a label while confirming the higher ones,
  the card suggests raising that label's minimum confidence, e.g. *"Driveway:
  Person. Raise the minimum confidence from 40% to 55%. The AI rejected 18 of
  20 Person detections below 55%."* **Apply** raises every enabled rule for
  that label on the camera that is lower (it never lowers one). A suggestion
  needs at least 12 judged detections in the last 14 days, at least 5 below
  the new value, 75% of those rejected, and at most 20% of the confirmed
  ones lost; it picks the lowest value that drops the most rejections.
- **Ignore Areas.** Places in a camera's view where the AI keeps rejecting
  one label (a bush the detector reads as a dog). A suggestion needs at
  least 6 rejections clustered in one part of the frame, with at most one
  confirmation there (or 10% of the rejections), inside a zone that has a
  rule for that label. It shows the latest rejected snapshot with the area
  outlined. **Ignore Area** stops that label matching inside the area in the
  zones that contain it; other labels, and the rest of the zone, are
  unaffected. A real object of that label there is missed too, so check the
  picture first (when the snapshot is gone, the camera's current view is
  shown instead). Areas in force are listed under the suggestions with
  **Stop Ignoring**. On the Zones page the selected zone shows its ignored
  spots as red dashed boxes, listed under the zone with a × to remove one;
  they are kept when the zone is edited. A spot left outside the zone after
  it is redrawn has no effect and is marked "outside area". Admins can also
  review and apply these from the phone app (Settings → AI Insights).
- **Frequent AI Tags.** Objects the AI named on at least 3 events in the
  last 7 days that the detector has no class for and no AI tag alert on that
  camera watches yet, with a link to set one up on the Zones page.

## AI near-miss check

**Settings → Detection → AI Near-Miss Check** (off by default; needs
**Second Look** and the AI model server). A camera's **day or night
profile** can turn it on or off for that camera alone, e.g. only on the
dark cameras where the detector misses people at night. When Second Look still rejects a
near-miss (a real person at night that the detector scores at 40% even
zoomed in), the crop is shown to the AI model in the background:

- a "yes" is remembered for 20 seconds: while the object stays in view, the
  next cycles' near-miss of that label at about the same place passes, and
  the normal pipeline takes over (confirmation frames, event, recording,
  alerts). Such a detection is scored at the threshold and keeps the
  detector's own score as `detector_confidence`;
- a "no" is remembered for 60 seconds, so the same shadow is not asked about
  every cycle;
- at most one request per camera every 3 seconds, at background priority,
  so it never delays alerts, and the detection loop never waits for it.

An object that has left before the answer arrives is missed, as it would
have been without the check. Each confirmation is logged on the **AI Log**
as **AI Near-Miss Confirmed**.
