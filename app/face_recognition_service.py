"""Runtime face-recognition service: embedder + live matcher over enrolments.

Ties the Stage 2a engine (:mod:`app.face_recognition`) to the persisted
settings and the enrolment database. One process-wide singleton holds:

- the loaded :class:`~app.face_recognition.FaceEmbedder` (or an unavailable
  placeholder when recognition is off / no model / runtime missing), and
- a cached :class:`~app.face_recognition.FaceMatcher` rebuilt from the
  ``person_faces`` rows for the active ``model_id``.

:meth:`FaceRecognitionService.recognize` turns a cropped face into an identity
(or ``None`` for unknown). The service is dormant until an admin enables
recognition and selects a model; :meth:`recognize` returns ``None`` in every
not-ready state so callers never have to special-case it.

This module wires the capability but does not itself call it on the detection
hot path -- that live-loop integration is a later slice.
"""
from __future__ import annotations

import logging
import threading
from typing import Any

from app.face_recognition import FaceEmbedder, FaceMatcher, MatchResult

logger = logging.getLogger('daygle.ai')


class FaceRecognitionService:
    """Holds the embedding model and the matcher built from enrolled faces."""

    def __init__(self, config: dict[str, Any], database: Any) -> None:
        self._database = database
        self.enabled = bool(config.get('enabled', False))
        self.model_id = str(config.get('model_id') or 'arcface')
        self.model_path = str(config.get('model_path') or '')
        try:
            self.threshold = float(config.get('match_threshold', 0.5))
        except (TypeError, ValueError):
            self.threshold = 0.5
        self.alert_unknown = bool(config.get('alert_unknown', False))
        try:
            self.min_face_pixels = max(0, int(config.get('min_face_pixels', 0) or 0))
        except (TypeError, ValueError):
            self.min_face_pixels = 0

        self._lock = threading.Lock()
        self.embedder: FaceEmbedder | None = None
        self.unavailable_reason: str | None = None

        if not self.enabled:
            self.unavailable_reason = 'Face recognition is disabled.'
        elif not self.model_path:
            self.unavailable_reason = 'No embedding model configured.'
        else:
            self.embedder = FaceEmbedder(self.model_path, model_id=self.model_id)
            if not self.embedder.available:
                self.unavailable_reason = self.embedder.unavailable_reason

        self._matcher: FaceMatcher | None = None
        self.refresh_matcher()

    # -- availability ----------------------------------------------------
    @property
    def available(self) -> bool:
        """True when recognition is enabled and the embedding model is loaded."""
        return bool(self.enabled and self.embedder is not None and self.embedder.available)

    @property
    def embedding_dim(self) -> int | None:
        return self.embedder.embedding_dim if self.embedder is not None else None

    @property
    def enrolled_count(self) -> int:
        matcher = self._matcher
        return len(matcher) if matcher is not None else 0

    # -- matcher lifecycle ----------------------------------------------
    def refresh_matcher(self) -> None:
        """Rebuild the matcher from the enrolled faces for the active model.

        Called at construction and whenever enrolments change. Cheap at
        household scale (a few hundred vectors); the rebuilt matcher is swapped
        in under the lock so an in-flight :meth:`recognize` always sees a
        consistent matrix.
        """
        rows: list[dict[str, Any]] = []
        if self._database is not None:
            try:
                rows = self._database.load_face_embeddings(self.model_id)
            except Exception as exc:  # pragma: no cover - defensive DB guard
                logger.warning('Face matcher refresh failed to load embeddings: %s', exc)
                rows = []
        try:
            matcher = FaceMatcher(rows)
        except Exception as exc:  # pragma: no cover - defensive (numpy missing)
            logger.warning('Face matcher rebuild failed: %s', exc)
            matcher = None
        with self._lock:
            self._matcher = matcher

    # -- recognition -----------------------------------------------------
    def recognize(self, face_bgr: Any) -> MatchResult | None:
        """Return the identity for a cropped BGR face, or ``None`` (unknown).

        Returns ``None`` -- never raises -- in every not-ready state: disabled,
        no model, a face below ``min_face_pixels``, an empty enrolment store, or
        a best score under the threshold. Callers treat ``None`` as "no known
        person" (which, when ``alert_unknown`` is set, is itself actionable).
        """
        if not self.available:
            return None
        if self._below_min_size(face_bgr):
            return None
        matcher = self._matcher
        if matcher is None or matcher.is_empty:
            return None
        try:
            embedding = self.embedder.embed(face_bgr)  # type: ignore[union-attr]
        except Exception as exc:
            logger.debug('Face embedding failed; treating as unknown: %s', exc)
            return None
        return matcher.match(embedding, threshold=self.threshold)

    def _below_min_size(self, face_bgr: Any) -> bool:
        if self.min_face_pixels <= 0:
            return False
        shape = getattr(face_bgr, 'shape', None)
        if not shape or len(shape) < 2:
            return False
        return min(int(shape[0]), int(shape[1])) < self.min_face_pixels


# ---------------------------------------------------------------------------
# Process-wide singleton
# ---------------------------------------------------------------------------
_service: FaceRecognitionService | None = None
_singleton_lock = threading.Lock()


def _build_service() -> FaceRecognitionService:
    from app.config_facades import effective_face_recognition_config
    import app.state as _state

    return FaceRecognitionService(effective_face_recognition_config(), _state.database)


def get_face_recognition_service() -> FaceRecognitionService:
    """Return the recognition service, building it lazily on first use."""
    global _service
    if _service is None:
        with _singleton_lock:
            if _service is None:
                _service = _build_service()
    return _service


def reload_face_recognition_service(config: dict[str, Any] | None = None) -> tuple[bool, str | None]:
    """Rebuild the service (after a settings change). Returns ``(ok, error)``.

    ``ok`` reports whether recognition ended up *available* (enabled + model
    loaded); a disabled or model-less configuration reloads successfully but
    returns ``ok=False`` with the reason, mirroring ``reload_detector``.
    """
    global _service
    import app.state as _state
    if config is None:
        from app.config_facades import effective_face_recognition_config
        config = effective_face_recognition_config()
    try:
        service = FaceRecognitionService(config, _state.database)
    except Exception as exc:  # pragma: no cover - construction is defensive already
        # Keep the returned reason generic: it flows to the settings API
        # response, and raw exception text can leak internal details.
        logger.warning('Face recognition reload failed: %s', exc)
        return False, 'Face recognition failed to load.'
    with _singleton_lock:
        _service = service
    return service.available, service.unavailable_reason


def refresh_face_recognition_matcher() -> None:
    """Rebuild the live matcher after an enrolment change (no model reload)."""
    service = _service
    if service is not None:
        service.refresh_matcher()
