"""AI alert verification: a local vision model double-checks object alerts.

Before an object alert's email/push notification goes out, the event snapshot
is sent to a vision-language model with one question per alerted label: "is
there really a <label> here?" A "no" suppresses that alert's notification; the
event, its recording and its alert rows are kept and the verdict is stored in
the event's metadata (``ai_verification``), so a wrongly filtered alert is
still visible in the Events list.

The model is reached over the OpenAI-compatible ``/chat/completions`` API, so
Ollama, llama.cpp's server, LM Studio, vLLM or OpenLLM all work.

Everything fails open: when the model is disabled, unreachable, slow, returns
something unparseable, or the verification queue is full, notifications are
delivered exactly as they would be without this feature. Verification runs on
its own single-worker pool, never on the detection loop, and face, motion and
sound alerts are never verified.
"""

from __future__ import annotations

import base64
import json
import logging
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from typing import Any

from fastapi import HTTPException

import app.state as _state
from app.detection_status import GENERIC_TRIGGER_LABELS
from app.runtime_config import cached_snapshot

logger = logging.getLogger('daygle.ai')

SETTINGS_KEY = 'ai_verification'

DEFAULT_AI_VERIFICATION_SETTINGS: dict[str, Any] = {
    'enabled': False,
    # Ollama's OpenAI-compatible endpoint on the same host.
    'server_url': 'http://127.0.0.1:11434/v1',
    'model': 'gemma3:4b',
    'api_key': '',
    'timeout_seconds': 20,
    # Empty = every camera / every object label.
    'camera_ids': [],
    'labels': [],
    # Alerts at or above this detector confidence skip verification (1.0 =
    # verify every alert). Lets the model spend its time on borderline ones.
    'skip_above_confidence': 1.0,
    # Crop around the detected object before asking; small models judge a
    # small, distant object far better from a close-up.
    'focus_crop': True,
}

# Labels that are never sent to the model: face alerts are identity rules
# (verified by face recognition itself) and motion has no object to check.
_UNVERIFIABLE_LABELS = frozenset(GENERIC_TRIGGER_LABELS | {'face'})
MAX_LABELS_PER_EVENT = 3
_MAX_IMAGE_BYTES = 8 * 1024 * 1024
_MAX_RESPONSE_BYTES = 256 * 1024
_MODEL_NAME_RE = re.compile(r'^[A-Za-z0-9._:/@+-]{1,200}$')
_JSON_OBJECT_RE = re.compile(r'\{.*?\}', re.DOTALL)

SYSTEM_PROMPT = (
    'You check security camera alerts for false alarms. '
    'Answer only with a JSON object and nothing else.'
)


class VerificationError(RuntimeError):
    """The model could not produce a usable verdict."""


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------

def effective_ai_verification_settings() -> dict[str, Any]:
    return cached_snapshot(_state.database, SETTINGS_KEY, _build_effective_settings)


def _build_effective_settings() -> dict[str, Any]:
    settings = dict(DEFAULT_AI_VERIFICATION_SETTINGS)
    database = getattr(_state, 'database', None)
    stored = None
    if database is not None:
        try:
            stored = database.get_setting(SETTINGS_KEY)
        except Exception:  # noqa: BLE001 - a settings read must not break alerts
            stored = None
    if isinstance(stored, dict):
        settings.update(stored)
    return settings


def _bad(detail: str) -> HTTPException:
    return HTTPException(status_code=400, detail=detail)


def _string_list(value: Any, field: str) -> list[str]:
    if value in (None, ''):
        return []
    if isinstance(value, str):
        value = value.split(',')
    if not isinstance(value, list):
        raise _bad(f'{field} must be a list of strings.')
    items: list[str] = []
    for item in value:
        if not isinstance(item, str):
            raise _bad(f'{field} must be a list of strings.')
        item = item.strip()
        if item and item not in items:
            items.append(item)
    if len(items) > 200:
        raise _bad(f'{field} may contain at most 200 entries.')
    return items


def validate_ai_verification_settings(payload: dict[str, Any]) -> dict[str, Any]:
    """Validate a settings payload on top of the current effective settings."""
    settings = {**effective_ai_verification_settings(), **payload}
    settings = {key: settings[key] for key in DEFAULT_AI_VERIFICATION_SETTINGS}

    for field in ('enabled', 'focus_crop'):
        value = settings[field]
        if isinstance(value, str):
            value = value.strip().lower() in {'1', 'true', 'yes', 'on'}
        settings[field] = bool(value)

    server_url = str(settings['server_url'] or '').strip().rstrip('/')
    parsed = urllib.parse.urlparse(server_url)
    if parsed.scheme not in ('http', 'https') or not parsed.hostname:
        raise _bad('server_url must be an http:// or https:// URL, e.g. http://127.0.0.1:11434/v1.')
    if parsed.query or parsed.fragment:
        raise _bad('server_url must not contain a query string or fragment.')
    settings['server_url'] = server_url

    model = str(settings['model'] or '').strip()
    if not _MODEL_NAME_RE.match(model):
        raise _bad('model must be a model name such as gemma3:4b.')
    settings['model'] = model

    api_key = settings['api_key']
    if not isinstance(api_key, str) or len(api_key) > 500 or any(ch in api_key for ch in '\r\n'):
        raise _bad('api_key must be a single-line string.')
    settings['api_key'] = api_key.strip()

    try:
        timeout = int(settings['timeout_seconds'])
    except (TypeError, ValueError) as exc:
        raise _bad('timeout_seconds must be an integer between 3 and 120.') from exc
    if isinstance(settings['timeout_seconds'], bool) or not 3 <= timeout <= 120:
        raise _bad('timeout_seconds must be an integer between 3 and 120.')
    settings['timeout_seconds'] = timeout

    try:
        skip_above = float(settings['skip_above_confidence'])
    except (TypeError, ValueError) as exc:
        raise _bad('skip_above_confidence must be a number between 0 and 1.') from exc
    if isinstance(settings['skip_above_confidence'], bool) or not 0.0 <= skip_above <= 1.0:
        raise _bad('skip_above_confidence must be a number between 0 and 1.')
    settings['skip_above_confidence'] = skip_above

    settings['camera_ids'] = _string_list(settings['camera_ids'], 'camera_ids')
    settings['labels'] = list(dict.fromkeys(label.lower() for label in _string_list(settings['labels'], 'labels')))
    return settings


def applies_to_camera(camera_id: Any, settings: dict[str, Any] | None = None) -> bool:
    settings = settings if settings is not None else effective_ai_verification_settings()
    if not settings.get('enabled'):
        return False
    camera_ids = settings.get('camera_ids') or []
    return not camera_ids or str(camera_id or '') in camera_ids


def _is_verifiable(alert: dict[str, Any], settings: dict[str, Any]) -> bool:
    label = str(alert.get('label') or '').strip().lower()
    if not label or label in _UNVERIFIABLE_LABELS:
        return False
    if alert.get('face_rule_id') or alert.get('face_rule_ids') or alert.get('motion_event'):
        return False
    labels = settings.get('labels') or []
    if labels and label not in labels:
        return False
    try:
        confidence = float(alert.get('confidence') or 0.0)
    except (TypeError, ValueError):
        confidence = 0.0
    skip_above = float(settings.get('skip_above_confidence', 1.0))
    return not (skip_above < 1.0 and confidence >= skip_above)


def labels_to_verify(triggered: list[dict[str, Any]], settings: dict[str, Any]) -> list[str]:
    """Distinct alert labels to ask about, strongest alert first."""
    ordered = sorted(
        (alert for alert in triggered if _is_verifiable(alert, settings)),
        key=lambda alert: -float(alert.get('confidence') or 0.0),
    )
    labels: list[str] = []
    for alert in ordered:
        label = str(alert.get('label') or '').strip().lower()
        if label not in labels:
            labels.append(label)
    return labels[:MAX_LABELS_PER_EVENT]


# ---------------------------------------------------------------------------
# Model client
# ---------------------------------------------------------------------------

def build_prompt(label: str, camera_name: str = '') -> str:
    where = f' from the camera "{camera_name}"' if camera_name else ''
    return (
        f'An object detector reported a "{label}" in this security camera image{where}. '
        f'Is a real {label} actually visible? It is NOT a real {label} if it is only a shadow, '
        f'reflection, picture, poster, statue, toy, plant, rubbish bin, or another object that '
        f'looks like a {label}, or if the image shows nothing clear. '
        'Reply with JSON exactly like {"present": true, "reason": "short reason"} '
        'with "present" true or false and a reason of at most 15 words.'
    )


def parse_verdict(text: str) -> tuple[bool, str]:
    """Parse the model reply into ``(present, reason)``.

    Accepts the requested JSON (also inside code fences or surrounding prose)
    and falls back to a leading yes/no. Raises VerificationError otherwise, so
    an unusable reply fails open.
    """
    content = str(text or '').strip()
    for candidate in _JSON_OBJECT_RE.findall(content):
        try:
            data = json.loads(candidate)
        except ValueError:
            continue
        if isinstance(data, dict) and 'present' in data:
            present = data.get('present')
            if isinstance(present, str):
                lowered = present.strip().lower()
                if lowered not in {'true', 'false', 'yes', 'no'}:
                    continue
                present = lowered in {'true', 'yes'}
            if isinstance(present, bool):
                reason = ' '.join(str(data.get('reason') or '').split())[:200]
                return present, reason
    match = re.match(r'^\W*(yes|no|true|false)\b[\s.,:;-]*(.*)$', content, re.IGNORECASE | re.DOTALL)
    if match:
        present = match.group(1).lower() in {'yes', 'true'}
        return present, ' '.join(match.group(2).split())[:200]
    raise VerificationError(f'unrecognised model reply: {content[:120]!r}')


class VisionVerifier:
    """Minimal OpenAI-compatible chat client for yes/no image questions."""

    def __init__(self, settings: dict[str, Any]):
        self.server_url = str(settings.get('server_url') or '').rstrip('/')
        self.model = str(settings.get('model') or '')
        self.api_key = str(settings.get('api_key') or '')
        self.timeout = float(settings.get('timeout_seconds') or 20)

    def _request(self, path: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
        headers = {'Accept': 'application/json'}
        data = None
        if body is not None:
            headers['Content-Type'] = 'application/json'
            data = json.dumps(body).encode('utf-8')
        if self.api_key:
            headers['Authorization'] = f'Bearer {self.api_key}'
        request = urllib.request.Request(
            f'{self.server_url}{path}', data=data, headers=headers,
            method='POST' if body is not None else 'GET',
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:  # noqa: S310 - admin-configured http(s) URL
                raw = response.read(_MAX_RESPONSE_BYTES + 1)
        except urllib.error.HTTPError as exc:
            detail = ''
            try:
                detail = exc.read(500).decode('utf-8', 'replace')
            except Exception:  # noqa: BLE001
                pass
            raise VerificationError(f'HTTP {exc.code} from model server {detail[:200]}'.strip()) from exc
        except (urllib.error.URLError, OSError, TimeoutError) as exc:
            reason = getattr(exc, 'reason', exc)
            raise VerificationError(f'model server unreachable: {reason}') from exc
        if len(raw) > _MAX_RESPONSE_BYTES:
            raise VerificationError('model server response too large')
        try:
            payload = json.loads(raw.decode('utf-8'))
        except ValueError as exc:
            raise VerificationError('model server returned invalid JSON') from exc
        if not isinstance(payload, dict):
            raise VerificationError('model server returned an unexpected response')
        return payload

    def list_models(self) -> list[str]:
        payload = self._request('/models')
        data = payload.get('data')
        if not isinstance(data, list):
            return []
        return [str(item.get('id')) for item in data if isinstance(item, dict) and item.get('id')]

    def ask(self, image_bytes: bytes, label: str, camera_name: str = '') -> tuple[bool, str]:
        encoded = base64.b64encode(image_bytes).decode('ascii')
        body = {
            'model': self.model,
            'temperature': 0,
            'max_tokens': 80,
            'stream': False,
            'messages': [
                {'role': 'system', 'content': SYSTEM_PROMPT},
                {'role': 'user', 'content': [
                    {'type': 'text', 'text': build_prompt(label, camera_name)},
                    {'type': 'image_url', 'image_url': {'url': f'data:image/jpeg;base64,{encoded}'}},
                ]},
            ],
        }
        payload = self._request('/chat/completions', body)
        try:
            content = payload['choices'][0]['message']['content']
        except (KeyError, IndexError, TypeError) as exc:
            raise VerificationError('model server reply had no message content') from exc
        if isinstance(content, list):  # some servers return content parts
            content = ' '.join(str(part.get('text') or '') for part in content if isinstance(part, dict))
        return parse_verdict(str(content or ''))


# ---------------------------------------------------------------------------
# Image preparation
# ---------------------------------------------------------------------------

def _label_box(detections: list[dict[str, Any]], label: str) -> tuple[float, float, float, float] | None:
    """Union of this label's boxes, in normalized [0, 1] coordinates."""
    boxes = []
    for detection in detections or []:
        if str(detection.get('label') or '').strip().lower() != label:
            continue
        try:
            x, y = float(detection['x']), float(detection['y'])
            w, h = float(detection['width']), float(detection['height'])
        except (KeyError, TypeError, ValueError):
            continue
        if w <= 0 or h <= 0 or max(x, y, w, h) > 1.5:
            continue
        boxes.append((x, y, x + w, y + h))
    if not boxes:
        return None
    return (
        max(0.0, min(box[0] for box in boxes)), max(0.0, min(box[1] for box in boxes)),
        min(1.0, max(box[2] for box in boxes)), min(1.0, max(box[3] for box in boxes)),
    )


def focus_image(image_bytes: bytes, detections: list[dict[str, Any]], label: str) -> bytes:
    """Crop around the label's boxes with generous context; else the frame.

    The crop keeps a margin of one box size on every side (and at least a
    quarter of the frame) so the model still sees what surrounds the object,
    which is what separates a person from their reflection or a shadow.
    """
    box = _label_box(detections, label)
    if box is None:
        return image_bytes
    try:
        import cv2
        import numpy as np
    except ImportError:
        return image_bytes
    frame = cv2.imdecode(np.frombuffer(image_bytes, dtype=np.uint8), cv2.IMREAD_COLOR)
    if frame is None:
        return image_bytes
    height, width = frame.shape[:2]
    x0, y0, x1, y1 = box
    # The box plus one box size on every side, never less than 35% of the
    # frame, shifted (not shrunk) to stay inside it.
    size = min(1.0, max(3.0 * max(x1 - x0, y1 - y0), 0.35))
    if size >= 0.9:
        return image_bytes
    left = min(max(0.0, (x0 + x1) / 2 - size / 2), 1.0 - size)
    top = min(max(0.0, (y0 + y1) / 2 - size / 2), 1.0 - size)
    right, bottom = left + size, top + size
    crop = frame[int(top * height):int(bottom * height), int(left * width):int(right * width)]
    if crop.size == 0:
        return image_bytes
    ok, encoded = cv2.imencode('.jpg', crop, [int(cv2.IMWRITE_JPEG_QUALITY), 90])
    return encoded.tobytes() if ok else image_bytes


def _read_snapshot(event: dict[str, Any]) -> bytes | None:
    # Event rows can come from a restored backup: only read a snapshot that
    # really lives in the snapshots directory, as the snapshot endpoint does.
    from app.media_utils import safe_storage_path

    snapshot = safe_storage_path(event.get('snapshot_path'), roots=('snapshots_dir',))
    if snapshot is None:
        return None
    try:
        if not snapshot.is_file() or snapshot.stat().st_size > _MAX_IMAGE_BYTES:
            return None
        return snapshot.read_bytes()
    except OSError:
        return None


# ---------------------------------------------------------------------------
# Verification flow
# ---------------------------------------------------------------------------

def verify_event(
    event: dict[str, Any],
    labels: list[str],
    settings: dict[str, Any],
    *,
    camera_name: str = '',
    verifier: VisionVerifier | None = None,
) -> dict[str, Any]:
    """Ask the model about each label; return the ``ai_verification`` record."""
    record: dict[str, Any] = {
        'model': settings.get('model'),
        'checked_at': datetime.now(timezone.utc).isoformat(),
        'labels': {},
    }
    started = time.monotonic()
    image = _read_snapshot(event)
    if image is None:
        record.update(status='skipped', reason='No snapshot to check.')
        return record
    verifier = verifier or VisionVerifier(settings)
    detections = event.get('detections') or []
    for label in labels:
        try:
            focused = focus_image(image, detections, label) if settings.get('focus_crop', True) else image
            present, reason = verifier.ask(focused, label, camera_name)
            record['labels'][label] = {'present': present, 'reason': reason}
        except VerificationError as exc:
            record['labels'][label] = {'present': None, 'error': str(exc)[:200]}
        except Exception as exc:  # noqa: BLE001 - fail open on anything unexpected
            logger.exception('AI verification failed unexpectedly for label %s', label)
            record['labels'][label] = {'present': None, 'error': f'unexpected error: {exc}'[:200]}
    record['latency_ms'] = int((time.monotonic() - started) * 1000)
    results = record['labels'].values()
    if any(result.get('present') is False for result in results):
        record['status'] = 'filtered'
    elif results and all(result.get('present') is True for result in results):
        record['status'] = 'confirmed'
    else:
        record['status'] = 'error'
    return record


def _forward(triggered: list[dict[str, Any]], event_id: int, rules: list[dict[str, Any]] | None) -> None:
    from app.alert_dispatch import deliver_alert_notifications, submit_alert_notification

    if triggered:
        submit_alert_notification(deliver_alert_notifications, triggered, event_id, rules)


def _record(event_id: int, record: dict[str, Any]) -> None:
    try:
        _state.database.merge_event_metadata(event_id, {'ai_verification': record})
    except Exception as exc:  # noqa: BLE001 - recording the verdict is best effort
        logger.warning('Could not store AI verification result for event %s: %s', event_id, exc)


def verify_and_forward(
    triggered: list[dict[str, Any]],
    event_id: int,
    rules: list[dict[str, Any]] | None,
    camera_name: str = '',
    submitted_at: float | None = None,
) -> None:
    """Verification-pool job: check the alerts, then queue what survives."""
    settings = effective_ai_verification_settings()
    labels = labels_to_verify(triggered, settings)
    if not settings.get('enabled') or not labels:
        _forward(triggered, event_id, rules)
        return
    # A backlog means alerts are already late; deliver rather than add more.
    max_wait = max(60.0, 3.0 * float(settings.get('timeout_seconds') or 20))
    if submitted_at is not None and time.monotonic() - submitted_at > max_wait:
        _record(event_id, {'status': 'skipped', 'reason': 'Verification queue backlog; delivered unverified.'})
        _forward(triggered, event_id, rules)
        return
    try:
        event = _state.database.get_event(event_id) or {}
    except Exception:  # noqa: BLE001
        event = {}
    record = verify_event(event, labels, settings, camera_name=camera_name)
    _record(event_id, record)
    rejected = {label for label, result in record['labels'].items() if result.get('present') is False}
    kept = [
        alert for alert in triggered
        if not (_is_verifiable(alert, settings) and str(alert.get('label') or '').strip().lower() in rejected)
    ]
    if rejected:
        logger.info(
            'AI verification filtered event %s (%s): %s', event_id, ', '.join(sorted(rejected)),
            '; '.join(f"{label}: {record['labels'][label].get('reason') or 'not present'}" for label in sorted(rejected)),
        )
    for label, result in record['labels'].items():
        if result.get('error'):
            logger.warning('AI verification unavailable for event %s (%s): %s; alert delivered unverified.',
                           event_id, label, result['error'])
    _forward(kept, event_id, rules)


def submit_alert_notification_with_verification(
    triggered: list[dict[str, Any]],
    event_id: int,
    rules: list[dict[str, Any]] | None,
    *,
    camera_id: Any = None,
    camera_name: str = '',
) -> bool:
    """Queue an alert's notifications, via AI verification when it applies.

    Never blocks the caller. When verification is off, does not apply to this
    camera or these alerts, or its queue is full, notifications are queued
    directly, exactly as without this feature.
    """
    from app.alert_dispatch import deliver_alert_notifications, submit_alert_notification

    settings = effective_ai_verification_settings()
    if applies_to_camera(camera_id, settings) and labels_to_verify(triggered, settings):
        from app.postprocess_pool import verification_pool

        accepted = verification_pool().submit(
            verify_and_forward, list(triggered), event_id, rules, camera_name, time.monotonic(),
            block=False, label=f'verify-event-{event_id}',
        )
        if accepted:
            return True
        logger.warning('AI verification queue full; delivering event %s notification unverified', event_id)
    return submit_alert_notification(deliver_alert_notifications, triggered, event_id, rules)


def run_connection_test(settings: dict[str, Any], *, event_id: int | None = None) -> dict[str, Any]:
    """Check the server and model, and optionally verify a stored event.

    Raises VerificationError with a user-facing message on failure.
    """
    verifier = VisionVerifier(settings)
    result: dict[str, Any] = {'ok': True, 'model': settings.get('model')}
    try:
        models = verifier.list_models()
    except VerificationError:
        models = []  # /models is optional on some servers; the chat call decides
    result['model_listed'] = (settings.get('model') in models) if models else None
    event = None
    if event_id is not None:
        event = _state.database.get_event(int(event_id))
        if event is None:
            raise VerificationError(f'event {event_id} not found')
    else:
        event = _latest_object_event()
    if event is None:
        # No stored object event yet: prove the model answers at all.
        started = time.monotonic()
        body = {
            'model': verifier.model, 'temperature': 0, 'max_tokens': 5, 'stream': False,
            'messages': [{'role': 'user', 'content': 'Reply with the single word OK.'}],
        }
        verifier._request('/chat/completions', body)
        result['latency_ms'] = int((time.monotonic() - started) * 1000)
        result['message'] = 'No object event with a snapshot exists yet, so only a text reply was tested.'
        return result
    labels = sorted({
        str(detection.get('label') or '').strip().lower()
        for detection in event.get('detections') or []
        if str(detection.get('label') or '').strip().lower() not in _UNVERIFIABLE_LABELS
    })[:MAX_LABELS_PER_EVENT] or ['person']
    record = verify_event(event, labels, {**settings, 'enabled': True},
                          camera_name=str((event.get('metadata') or {}).get('camera_name') or ''),
                          verifier=verifier)
    errors = [value['error'] for value in record['labels'].values() if value.get('error')]
    if errors and record['status'] == 'error':
        raise VerificationError(errors[0])
    result.update(event_id=event.get('id'), verification=record, latency_ms=record.get('latency_ms'))
    return result


def _latest_object_event() -> dict[str, Any] | None:
    try:
        events = _state.database.search_events(limit=50)
    except Exception:  # noqa: BLE001
        return None
    for event in events or []:
        if not event.get('snapshot_path'):
            continue
        labels = {str(d.get('label') or '').strip().lower() for d in event.get('detections') or []}
        if labels - _UNVERIFIABLE_LABELS:
            return event
    return None
