from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI
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

web_dir = Path('web')
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
        'mode': 'mock',
        'frame_number': frame['frame_number'],
        'uptime_seconds': frame['uptime_seconds'],
    }


@app.post('/api/mock/detect')
def generate_detection():
    frame = camera.get_frame()
    detections = detector.detect(frame['frame_number'])

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
def events(label: str | None = None):
    return database.search_events(label=label)


@app.get('/api/alerts')
def alert_history():
    return database.alerts()


@app.get('/api/stats')
def stats():
    return database.stats()


if __name__ == '__main__':
    import uvicorn

    server_config = config.get('server', {})
    uvicorn.run(
        'app.main:app',
        host=server_config.get('host', '0.0.0.0'),
        port=int(server_config.get('port', 8080)),
        reload=False,
    )
