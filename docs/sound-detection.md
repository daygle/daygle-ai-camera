# Sound Detection Guide

Daygle AI Camera can monitor RTSP audio streams and create alerts or recordings when configured sounds are detected. Sound rules are managed per camera from **Sounds** (`/sounds`) and are independent from video zones and object rules.

## How sound detection works

1. A camera must have an RTSP stream with an audio track.
2. Sound detection is enabled for that camera from `/sounds`.
3. The per-camera ingest (the same single RTSP connection that feeds video pre-roll
   and object detection) extracts the audio track and writes 1-second 16 kHz WAV
   segments. The sound monitor consumes those segments, so it never opens a second
   RTSP connection.
4. The YAMNet TensorFlow Lite backend scores the audio against supported sound classes.
5. Enabled per-camera rules compare the class confidence to the rule threshold and cooldown.
6. Matching rules can create alert history entries, send email, send push notifications, and start recordings.

> **Audio source:** The web UI always uses the shared ingest (`source='ingest'`).
> The `SoundDetector` class also implements `'microphone'` (system audio input via
> `sounddevice`) and `'rtsp'` (its own ffmpeg pipe) sources for programmatic use,
> but they are not wired to the web UI.

## Supported sound classes

Built-in classes are:

- Cat Meow
- Dog Bark
- Glass Breaking
- Smoke Alarm
- Baby Crying
- Doorbell
- Car Alarm
- Loud Bang

Each class has a default threshold and cooldown. Treat those defaults as a starting point; noisy rooms and low-quality camera microphones often need higher thresholds.

## Requirements

- `ffmpeg` available on the server path.
- RTSP camera streams that include audio.
- A TensorFlow Lite runtime, either `ai-edge-litert` or `tflite-runtime`.
- Network access the first time assets are downloaded, unless `models/yamnet.tflite` and `models/yamnet_class_map.csv` are already present.

The sound backend stores YAMNet assets in `models/` alongside object detection models.

## Configuration workflow

1. Add or edit the camera from **Cameras** (`/cameras`) and confirm the RTSP stream works.
2. Open **Sounds** (`/sounds`).
3. Select the camera.
4. Change **Sound Detection** to **Enabled (RTSP audio)**.
5. Add one or more sound rules.
6. Tune each rule:
   - **Enabled**: whether the rule is active.
   - **Confidence threshold**: minimum score needed to fire.
   - **Cooldown**: minimum seconds between alerts for the same rule.
   - **Record**: whether the sound should create a recording clip.
   - **Email / push**: whether to send notifications.
7. Save the sound settings.
8. Open **YAMNet TFLite** (`/yamnet-tflite`) to confirm the backend is active.

## Tuning tips

- Raise thresholds when normal background noise creates false alerts.
- Lower thresholds when a sound is consistently missed, then increase cooldowns to avoid noisy alert bursts.
- Use longer cooldowns for recurring sounds such as barking or car alarms.
- Test close to the camera microphone. A camera that has clear video may still have poor audio quality.
- If a camera has multiple RTSP profiles, choose one that includes audio.
- Sound detection notifications are filtered out of the `/application-log` viewer to reduce log noise. Use `/camera-log` and the dashboard event history to investigate sound-triggered alerts instead.

## Troubleshooting

### YAMNet TFLite is unavailable

Open `/yamnet-tflite` and check the reported reason. If the runtime is missing, install `ai-edge-litert` or `tflite-runtime` into the same Python environment that runs Daygle AI Camera.

### No sound events are created

- Confirm the camera stream includes audio.
- Confirm `ffmpeg` is installed.
- Confirm sound detection and at least one rule are enabled for the camera.
- Lower the confidence threshold temporarily to validate the pipeline.
- Check application logs in `data/logs/app.log`.

### Too many alerts

- Raise the rule confidence threshold.
- Increase the rule cooldown.
- Disable classes that overlap in your environment.
- Use camera placement or microphone settings to reduce background noise.
