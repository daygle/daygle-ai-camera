from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app.alerts import AlertEngine
from app.database import EventDatabase
from app.detector import MockDetector
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

detector = MockDetector(config['ai']['categories'], config['ai']['confidence'])
alerts = AlertEngine(config['alerts']['rules'])
database = EventDatabase(config['storage']['database'])
storage = Storage(config)


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
        'frame_number': frame['frame_number'],
        'uptime_seconds': frame['uptime_seconds'],
        'resolution': {'width': frame['width'], 'height': frame['height']},
    }


@app.post('/api/mock/detect')
def generate_detection(force: bool = True):
    frame = camera.get_frame()
    detections = detector.detect(frame['frame_number'], force=force)

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
