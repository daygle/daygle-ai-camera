from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app.alerts import AlertEngine
from app.database import EventDatabase
from app.detector import DetectorUnavailableError, MockDetector, create_detector
from app.mock_camera import MockCamera
from app.settings import load_settings
from app.storage import Storage

config = load_settings()

app = FastAPI(title='Daygle AI Camera')

BASE_DIR = Path(__file__).resolve().parent.parent
web_dir = BASE_DIR / 'web'
static_dir = web_dir
if static_dir.exists():
    app.mount('/static', StaticFiles(directory=static_dir), name='static')

camera_config = config.get('camera', {})
camera = MockCamera(
    width=int(camera_config.get('width', 1280)),
    height=int(camera_config.get('height', 720)),
    fps=int(camera_config.get('fps', 15)),
)

detector = create_detector(config.get('ai', {}))
mock_detector = MockDetector(config.get('ai', {}).get('categories', []), float(config.get('ai', {}).get('confidence', 0.45)))
alerts = AlertEngine(config['alerts']['rules'])
database = EventDatabase(config['storage']['database'])
storage = Storage(config)


def _parse_header_value(header: str, key: str) -> str | None:
    for part in header.split(';'):
        part = part.strip()
        if part.startswith(f'{key}='):
            return part.split('=', 1)[1].strip('"')
    return None


async def _read_uploaded_image(request: Request) -> tuple[bytes, str | None, str | None]:
    content_type = request.headers.get('content-type', '')
    body = await request.body()

    if content_type.startswith('image/'):
        return body, None, content_type

    boundary = _parse_header_value(content_type, 'boundary')
    if not boundary:
        raise HTTPException(status_code=400, detail='Expected multipart image upload')

    delimiter = ('--' + boundary).encode('utf-8')
    for part in body.split(delimiter):
        if b'Content-Disposition' not in part or b'name="file"' not in part:
            continue
        header_blob, separator, payload = part.partition(b'\r\n\r\n')
        if not separator:
            continue
        headers = header_blob.decode('utf-8', errors='replace')
        filename = _parse_header_value(headers, 'filename')
        uploaded_type = None
        for line in headers.splitlines():
            if line.lower().startswith('content-type:'):
                uploaded_type = line.split(':', 1)[1].strip()
                break
        return payload.rstrip(b'\r\n-'), filename, uploaded_type

    raise HTTPException(status_code=400, detail='Multipart upload must include a file field named file')


@app.get('/')
def root():
    index_path = web_dir / 'index.html'
    if index_path.exists():
        return FileResponse(index_path)
    return {'application': 'Daygle AI Camera', 'status': 'running'}


@app.get('/api/status')
def status():
    frame = camera.get_frame()
    return {
        'status': 'online',
        'mode': camera_config.get('backend', 'mock'),
        'ai_backend': config.get('ai', {}).get('backend', 'mock'),
        'ai_available': getattr(detector, 'available', True),
        'ai_error': getattr(detector, 'unavailable_reason', None),
        'frame_number': frame['frame_number'],
        'uptime_seconds': frame['uptime_seconds'],
        'resolution': {'width': frame['width'], 'height': frame['height']},
    }


@app.post('/api/mock/detect')
def generate_detection(force: bool = True):
    frame = camera.get_frame()
    active_mock_detector = detector if hasattr(detector, 'detect') else mock_detector
    detections = active_mock_detector.detect(frame['frame_number'], force=force)

    if not detections:
        return {'created': False, 'message': 'No detections generated'}

    snapshot_path = storage.save_mock_snapshot(frame, detections)
    triggered = alerts.process(detections)

    event_id = database.add_event(
        created_at=datetime.now(timezone.utc).isoformat(),
        source='mock-camera',
        snapshot_path=snapshot_path,
        detections=detections,
        alert_triggered=bool(triggered),
    )

    for alert in triggered:
        database.add_alert(
            created_at=datetime.now(timezone.utc).isoformat(),
            rule_name=alert['rule_name'],
            event_id=event_id,
            label=alert['label'],
            confidence=alert['confidence'],
            message=alert['message'],
        )

    return {
        'created': True,
        'event_id': event_id,
        'detections': detections,
        'alerts': triggered,
    }


@app.post('/api/detect/test-image')
async def detect_test_image(request: Request):
    image_bytes, filename, content_type = await _read_uploaded_image(request)
    if not image_bytes:
        raise HTTPException(status_code=400, detail='Uploaded image is empty')

    try:
        detections = detector.detect_image(image_bytes)
    except DetectorUnavailableError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    snapshot_path = storage.save_image_snapshot(image_bytes, filename)
    triggered = alerts.process(detections)
    event_id = database.add_event(
        created_at=datetime.now(timezone.utc).isoformat(),
        source='test-image',
        snapshot_path=snapshot_path,
        detections=detections,
        alert_triggered=bool(triggered),
        metadata={
            'filename': filename,
            'content_type': content_type,
            'ai_backend': config.get('ai', {}).get('backend', 'mock'),
        },
    )

    for alert in triggered:
        database.add_alert(
            created_at=datetime.now(timezone.utc).isoformat(),
            rule_name=alert['rule_name'],
            event_id=event_id,
            label=alert['label'],
            confidence=alert['confidence'],
            message=alert['message'],
        )

    return {
        'created': True,
        'event_id': event_id,
        'detections': detections,
        'alerts': triggered,
        'snapshot_path': snapshot_path,
    }


@app.get('/api/events')
def events(label: str | None = None, limit: int = Query(50, ge=1, le=200)):
    return database.search_events(label=label, limit=limit)


@app.get('/api/events/{event_id}')
def event_detail(event_id: int):
    event = database.get_event(event_id)
    if event is None:
        raise HTTPException(status_code=404, detail='Event not found')
    return event


@app.get('/api/alerts')
def alert_history(limit: int = Query(25, ge=1, le=200)):
    return database.alerts(limit=limit)


@app.get('/api/stats')
def stats():
    return database.stats()


@app.get('/api/config')
def runtime_config():
    return {
        'server': {'host': config.get('server', {}).get('host'), 'port': config.get('server', {}).get('port')},
        'camera': config.get('camera', {}),
        'ai': {
            'enabled': config.get('ai', {}).get('enabled'),
            'backend': config.get('ai', {}).get('backend'),
            'confidence': config.get('ai', {}).get('confidence'),
            'iou_threshold': config.get('ai', {}).get('iou_threshold'),
            'input_size': config.get('ai', {}).get('input_size'),
            'model_path': config.get('ai', {}).get('model_path'),
            'labels_path': config.get('ai', {}).get('labels_path'),
            'available': getattr(detector, 'available', True),
            'error': getattr(detector, 'unavailable_reason', None),
            'categories': config.get('ai', {}).get('categories', []),
        },
        'alerts': config.get('alerts', {}),
        'storage': {
            'database': config.get('storage', {}).get('database'),
            'snapshots_dir': config.get('storage', {}).get('snapshots_dir'),
        },
    }


if __name__ == '__main__':
    import uvicorn

    server_config = config.get('server', {})
    uvicorn.run(
        'app.main:app',
        host=server_config.get('host', '0.0.0.0'),
        port=int(server_config.get('port', 8080)),
        reload=False,
    )
