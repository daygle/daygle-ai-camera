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

Go to **Settings → Notifications → AI Alert Verification**.

| Setting | Default | Notes |
|---|---|---|
| AI Verification | Disabled | Master switch. |
| Server URL | `http://127.0.0.1:11434/v1` | The OpenAI-compatible base URL. |
| Model | `gemma3:4b` | Must be a model that can read images. |
| API Key | (none) | Only if the server requires one. Ollama doesn't. |
| Timeout (seconds) | 20 | Per question. On timeout the alert is sent unverified. |
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
  more GPU time: one model call per event.
- The description appears under the detections in the Events list and is
  stored in the event's metadata (`ai_description`: text, model, time).
- Verification runs first. An alert the model rejects is not described in
  *Alerts only* mode.
- If describing fails or times out, the notification is sent without the
  sentence. Descriptions are never a reason for an alert to be late or lost.
- Uses the same server, model and camera selection as verification.

**Describe Past Events** (Settings) describes events from the last 24 hours to
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
