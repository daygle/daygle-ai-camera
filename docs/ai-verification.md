# AI alert verification, descriptions and search

AI alert verification reduces false alarms. Before an object alert's email or
push notification is sent, a local vision-language model looks at the event
snapshot and answers one question per alerted label: *"is there really a
person (car, dog, ...) here, or is it a shadow, reflection, bush or poster?"*

- **Rejected alerts are not notified**, but nothing is deleted. The event, its
  recording and its alert rows are kept, and the Events list marks the event
  **🤖 Filtered**. Hover the badge to see the model's reason. Confirmed alerts
  show **🤖 Verified**.
- **It fails open.** If the model is disabled, unreachable, slow, gives an
  answer that can't be read, or the verification queue is full, the alert is
  sent exactly as it would be without this feature.
- **Only object alerts are checked.** Face-recognition, motion and sound
  alerts are never sent to the model.
- **It never slows detection.** Verification runs on its own single-worker
  queue after the event is saved. Only notifications wait for the answer,
  typically 1-3 seconds on a GPU.
- **Snapshots stay local** when the model runs on your own server.

## Model server

The app talks to the model over the OpenAI-compatible `/v1/chat/completions`
API, so any of these work: Ollama, the llama.cpp server, LM Studio, vLLM and
OpenLLM. The model must be able to read images.

On a Tesla P4 or another Pascal GPU, use **Ollama** (or llama.cpp). vLLM, and
OpenLLM, which serves models through vLLM, need a Volta or newer GPU.

### Ollama on the camera server (Debian)

```bash
curl -fsSL https://ollama.com/install.sh | sh
ollama pull gemma3:4b
```

Ollama listens on `127.0.0.1:11434` and uses the NVIDIA GPU automatically.
`gemma3:4b` needs about 4-5 GB of GPU memory, which fits beside the YOLO
detectors on an 8 GB card. Smaller options are `qwen2.5vl:3b` and
`moondream`, which is fastest but least accurate.

By default Ollama unloads an idle model after 5 minutes, so the first alert
after a quiet period waits a few seconds while the model reloads. To keep it
loaded:

```bash
sudo systemctl edit ollama
# add:
# [Service]
# Environment="OLLAMA_KEEP_ALIVE=-1"
sudo systemctl restart ollama
```

Check that the model answers:

```bash
curl -s http://127.0.0.1:11434/v1/models
nvidia-smi   # after the first request, an ollama process uses the GPU
```

## Settings

Go to **Intelligence → AI** (admin only). The page has four sections: Model Server, Alert Verification, Descriptions & Search, and a pointer to AI Tag Alerts. One **Save AI Settings** button saves them all.

| Setting | Default | Notes |
|---|---|---|
| AI Verification | Disabled | Master switch. |
| Server URL | `http://127.0.0.1:11434/v1` | The OpenAI-compatible base URL. |
| Model | `gemma3:4b` | Must be a model that can read images. |
| API Key | (none) | Only if the server requires one. Ollama doesn't. |
| Timeout (seconds) | 20 | Per question for alerts. On timeout the alert is sent unverified. Background descriptions (Describe Events: All events, catch-up, Describe Past Events) wait up to 90 s, because the model keeps working on a request the app gives up on. |
| Skip Above Confidence | 1 | Alerts at or above this detector confidence are sent without checking. `1` means every alert is checked. Set it to, say, `0.85` to check only borderline alerts. |
| Object Labels | (all) | For example `person, car`. Leave empty to check every object label. |
| Focus on Object | Enabled | Sends a close-up around the detected object, with context, instead of the whole frame. Small models judge small or distant objects much better this way. |
| Cameras | (all) | Tick cameras to limit verification to them. |

**Test on Latest Event** sends the most recent object event's snapshot to the
model, using the unsaved form values, and shows the verdict and how long it
took. Use it to check the server, the model name and the speed before you
enable the feature.

## Event descriptions

With **Describe Events** set, the same model writes one factual sentence about
each event's snapshot, for example *"A courier in a hi-vis vest leaves a
parcel at the front door."*

- **Alerts only**: events that send a notification. The sentence is the first
  line of the email and the push notification, replacing "Alert triggered:
  person detected (87%)"; the confidence and other details stay below it.
- **All events**: also every event that does not alert, described in the
  background behind alert work. This makes all footage searchable, but uses
  more GPU time: one model call per event. A burst of events (a car parking
  can log five in a second) outruns a small model, so events that do not fit
  in the AI queue are described a little later, once the model is idle, newest
  first. Events from the last 3 hours are caught up; use **Describe Past
  Events** for anything older.
- **The model is told what the object detector found** (e.g. *"An object
  detector flagged: bird. It can be wrong."*). With **Focus on Object** on,
  small objects (at most a fifth of the frame) are described from a
  close-up with their surroundings instead of the whole frame. On a wide
  camera, a small distant object is only a few pixels once the frame is
  resized for the model, and without this a small model guesses (a magpie
  captioned as *"a black cat"*). It is told to say "a small animal" rather
  than guess.
- **One image per request, at most 1280 px.** Vision models resize images
  themselves, so larger frames only cost time; sending two images roughly
  doubled the time per description on a Tesla P4.
- **After a timeout, background descriptions pause for 30 s.** The model is
  still busy with the request the app gave up on; without the pause, every
  later request queued behind it and timed out too. Skipped events are
  described by the catch-up once the model is free.
- The description appears under the detections in the Events list and is
  stored in the event's metadata (`ai_description`: text, model, time).
- Verification runs first. An alert the model rejects is not described in
  *Alerts only* mode.
- If describing fails or times out, the notification is sent without the
  sentence. Descriptions are never a reason for an alert to be late or lost.
- Uses the same server, model and camera selection as verification.

### AI tags

With each description the model also lists up to 8 notable objects it can see,
such as `ladder`, `hi-vis vest`, `parcel` or `wheelie bin`. These are often
things the object detector has no class for.

- **Where they appear:** tags show as dashed **🤖** chips under the
  description in the Events list, and on the event's recordings (the list and
  the clip details).
- **Recording filter:** the recordings label filter lists them as
  "Ladder (AI tag, 3)".
- **Search:** tags are searchable, even when the sentence doesn't use the word.
- **Tags are not detections.** They have no box or confidence and never
  trigger alerts or recordings. A tag that repeats a detected label is
  dropped.
- **Detections win.** If a real detection of the same label arrives later, it
  takes over from the tag.
- **Where they are stored:** in `ai_description.tags` on the event, and as
  `recording_labels` rows with `source = 'ai'`. The recordings API reports
  them as `ai_labels`, apart from `labels`.
- **Model support:** the model is asked for JSON. A model that ignores the
  format still produces a description, just without tags.

### AI tag alerts

An **AI tag alert** notifies you when the model names something in an area,
such as a ladder, a parcel or a hi-vis vest, even when object detection has no
class for it. Set it up per area:

1. **Zones page:** turn on **AI tag alert** for the area and add what to watch
   for, e.g. `ladder` or `parcel`, pressing Enter after each.
2. **Choose where it must appear (Match in):**
   - **Tags only:** in the model's tag list;
   - **Description only:** in its sentence (whole words; plurals match);
   - **Tags or description:** either.
3. **Set a cooldown**, which is 300 seconds by default.
4. **Alerts page:** choose **Alert Type: AI Tag**, then set email, push,
   recipients and a notify window, as for loitering.

How it behaves:

- **Every event on that camera is described**, whatever the Describe Events
  setting. A rule can only see described events.
- **An event counts as in the area** when one of its detections (object or
  motion) was in the zone, or when the zone covers the whole frame.
- **What a firing does:** it adds an alert to that event, marks it as alerted,
  and sends a notification. The notification starts with
  *"AI tag alert (unconfirmed): Ladder on Front (Gate)."*, followed by the
  description.
- **Double-checked:** before alerting, the model is asked a separate yes/no
  question for each matched word (*"is a real cat actually visible?"*), on a
  close-up when the detector boxed that object. A word it then says no to
  does not alert, and does not use up the cooldown. If that check fails or
  times out, the alert is sent anyway.
- **Unconfirmed:** only the language model saw the object, never the object
  detector. Start with areas where an occasional false alert is harmless.
  These notifications show "AI Tag" as the detection type and no confidence.
- **Timing:** these alerts arrive a few seconds after the event, once the
  description is ready. If the model server is down, they don't fire.
- **Cooldown:** it is only used up by an alert that is actually sent. A rule
  with no channel on, or outside its notify window, never blocks a later alert.
- **Past events:** Describe Past Events never fires alerts on old events.

**Describe Past Events** (Intelligence → AI) describes events from the last 24 hours to
30 days that have no description yet, up to 500 at a time, so they become
searchable. It runs in the background, one event at a time, pauses whenever an
alert needs the model, and stops if descriptions are switched off.

## Plain-English search

The search box on the **Events** page searches the descriptions. Ask it
questions such as:

- `red car in the driveway yesterday afternoon`
- `anyone carrying a ladder`
- `delivery at the front door this morning`

The model turns the question into a query:

- the things that must appear, each with synonyms ("car" also matches
  "vehicle", "ute", "sedan");
- a camera, when you name one;
- a time window ("yesterday afternoon" is 12:00-18:00 yesterday in the admin
  time zone).

The line under the search box shows how the question was understood. The
search runs against a full-text index that matches word forms ("carrying"
finds "carries"). If nothing mentions every concept, it shows events that
match any of them and says so.

Without a reachable model, search still works on keywords, camera names and
simple times (today, yesterday, this morning, this afternoon, last night).
Only events that have a description are searchable. Non-admin users see the
same events in search as in the Events list.

## Behaviour details

- Each event checks at most three distinct labels, strongest alert first.
- If the model rejects one label but confirms another, only the rejected
  label's alerts are dropped. Face alerts on the same event are always sent.
- If a job waits in the queue longer than `max(60 s, 3 × timeout)`, it is sent
  unverified. Late alerts are not delayed further.
- The verdict is stored in the event's metadata under `ai_verification`, with
  a status of `filtered`, `confirmed`, `error` or `skipped`, plus each label's
  answer and reason, the model name, the latency and a timestamp.
- Filtered alerts are logged at INFO (`AI verification filtered event ...`).
  Model errors are logged at WARNING.
