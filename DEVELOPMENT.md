# Development Notes

## Architecture

Daygle AI Camera is a FastAPI application with static browser pages in `web/`, SQLite persistence in `app/database.py`, and modular runtime services for detection, alerts, recording, and ANPR.

The app intentionally keeps `config.yaml` small. Runtime settings live in SQLite table `app_settings` and are managed from the browser.

## ANPR Pipeline

ANPR is implemented as a separate module in `app/anpr.py` and is not tightly coupled to the object detector.

Pipeline:

```text
Camera/Image
-> YOLO Object Detection
-> Vehicle Detection
-> Plate Detection / Crop
-> Plate OCR
-> Store Plate Event
-> Search / Alerts
```

Integration points:

- `app.main.process_anpr_for_event()` is called after an event is created.
- `AnprPipeline` filters vehicle detections using `anpr.vehicle_labels`.
- `Storage.save_plate_crop()` writes a plate crop artifact for the event.
- OCR backends return normalized plate text and confidence.
- `EventDatabase.upsert_plate()` updates aggregate plate history.
- `EventDatabase.add_plate_event()` links the sighting to the source object event.

## OCR Backends

Configured in `/system-settings` or `app_settings.key = anpr`:

```json
{
  "enabled": true,
  "backend": "mock",
  "min_confidence": 0.75,
  "vehicle_labels": ["car", "truck", "bus", "motorcycle"]
}
```

Supported backends:

- `mock`: deterministic local backend for development and tests.
- `paddleocr`: optional PaddleOCR backend.
- `easyocr`: optional EasyOCR backend.

Optional installs:

```bash
pip install paddleocr
pip install easyocr
```

If PaddleOCR or EasyOCR is configured but unavailable, the pipeline falls back to the mock backend so the app remains usable.

## Plate Search And Alerts

Plate search uses `plate_events.plate_number LIKE ?`, normalized to uppercase alphanumeric text. Examples:

```text
ABC123
1ABC2D
XYZ999
```

Plate alert rules live in `plate_alert_rules`:

- `plate`: specific plate pattern.
- `unknown`: plate is not whitelisted or blacklisted.
- `blacklisted`: plate is marked blacklisted.

Cooldown state is currently in process memory in `app.main.plate_alert_last_triggered`.

## Database

Fresh installs use the current schema directly. This project is not live, so schema-breaking development changes can be handled by deleting the local SQLite file and starting fresh.

ANPR tables:

```sql
vehicle_plates(
  id,
  plate_number,
  first_seen,
  last_seen,
  sighting_count,
  notes,
  is_whitelisted,
  is_blacklisted
)

plate_events(
  id,
  event_id,
  plate_id,
  plate_number,
  confidence,
  image_path,
  created_at
)

plate_alert_rules(
  id,
  rule_name,
  rule_type,
  plate_pattern,
  enabled,
  cooldown_seconds
)
```

## API Surface

ANPR:

- `GET /api/plates`
- `GET /api/plates/{id}`
- `GET /api/plates/search?q=ABC123`
- `POST /api/plates/whitelist`
- `POST /api/plates/blacklist`
- `GET /api/plate-alerts`
- `POST /api/plate-alerts`
- `PUT /api/plate-alerts/{id}`
- `DELETE /api/plate-alerts/{id}`
- `GET /api/settings/anpr`
- `PUT /api/settings/anpr`

## Tests

Run:

```bash
python -m compileall app
python -m pytest
```

ANPR test coverage includes:

- Pipeline plate extraction.
- OCR result handling through the mock backend.
- Plate event storage and search.
- Plate alert rule CRUD and matching.
- Whitelist and blacklist updates.
- Viewer/admin API permissions.

## Known Limitations

- Mock/upload ANPR writes plate crop metadata artifacts; real crop bytes should be added with camera frame backends.
- Mock OCR does not recognize real plates.
- PaddleOCR and EasyOCR are optional and not pinned in `requirements.txt`.
- Plate alert matches are not yet written to a persistent alert-history table.
- OV5647, USB webcam, and RTSP backends can reuse the same ANPR service once they provide real frames and crops.
