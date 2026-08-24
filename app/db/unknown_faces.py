"""Unknown face capture store for the Review workflow.

When a detected face does not match any enrolled person, the face crop,
embedding, and thumbnail are stored so an admin can review and optionally
assign them to a person.
"""
from __future__ import annotations

from typing import Any

from app.auth import utc_now


class UnknownFacesMixin:
    """Methods for the ``unknown_faces`` table."""

    def store_unknown_face(
        self,
        *,
        camera_id: str,
        embedding: bytes,
        dim: int,
        model: str,
        zone_id: str | None = None,
        track_id: str | None = None,
        event_id: int | None = None,
        thumbnail: bytes | None = None,
        confidence: float | None = None,
        frame_width: int | None = None,
        frame_height: int | None = None,
        box_x: float | None = None,
        box_y: float | None = None,
        box_width: float | None = None,
        box_height: float | None = None,
        created_at: str | None = None,
    ) -> int:
        """Insert a new unknown face capture. Returns the row id."""
        ts = created_at or utc_now()
        with self.connect() as db:
            cursor = db.execute(
                """
                INSERT INTO unknown_faces
                    (camera_id, zone_id, track_id, event_id,
                     embedding, dim, model, thumbnail, confidence,
                     frame_width, frame_height,
                     box_x, box_y, box_width, box_height,
                     status, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?)
                """,
                (
                    camera_id, zone_id or None, track_id or None, event_id,
                    embedding, int(dim), str(model), thumbnail,
                    confidence,
                    frame_width, frame_height,
                    box_x, box_y, box_width, box_height,
                    ts,
                ),
            )
            return int(cursor.lastrowid)

    def list_unknown_faces(
        self,
        *,
        status: str = 'pending',
        limit: int = 50,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        """List unknown face captures (without blobs)."""
        with self.connect() as db:
            rows = db.execute(
                """
                SELECT id, camera_id, zone_id, track_id, event_id,
                       model, confidence,
                       frame_width, frame_height,
                       box_x, box_y, box_width, box_height,
                       status, assigned_person_id, created_at, reviewed_at
                FROM unknown_faces
                WHERE status = ?
                ORDER BY created_at DESC
                LIMIT ? OFFSET ?
                """,
                (status, int(limit), int(offset)),
            ).fetchall()
            return [dict(row) for row in rows]

    def count_unknown_faces(self, *, status: str = 'pending') -> int:
        with self.connect() as db:
            row = db.execute(
                "SELECT COUNT(*) AS count FROM unknown_faces WHERE status = ?",
                (status,),
            ).fetchone()
            return int(row['count']) if row else 0

    def get_unknown_face(self, face_id: int) -> dict[str, Any] | None:
        """Return a single unknown face capture (without blobs)."""
        with self.connect() as db:
            row = db.execute(
                """
                SELECT id, camera_id, zone_id, track_id, event_id,
                       model, confidence,
                       frame_width, frame_height,
                       box_x, box_y, box_width, box_height,
                       status, assigned_person_id, created_at, reviewed_at
                FROM unknown_faces
                WHERE id = ?
                """,
                (face_id,),
            ).fetchone()
            return dict(row) if row else None

    def get_unknown_face_thumbnail(self, face_id: int) -> bytes | None:
        """Return JPEG thumbnail bytes or None."""
        with self.connect() as db:
            row = db.execute(
                "SELECT thumbnail FROM unknown_faces WHERE id = ?",
                (face_id,),
            ).fetchone()
        if row is None:
            return None
        blob = row['thumbnail']
        return bytes(blob) if blob is not None else None

    def get_unknown_face_embedding(self, face_id: int) -> dict[str, Any] | None:
        """Return the embedding bytes + dim + model for a capture, or None."""
        with self.connect() as db:
            row = db.execute(
                "SELECT embedding, dim, model FROM unknown_faces WHERE id = ?",
                (face_id,),
            ).fetchone()
        if row is None:
            return None
        return {'embedding': bytes(row['embedding']), 'dim': int(row['dim']), 'model': str(row['model'])}

    def assign_unknown_face(self, face_id: int, person_id: int) -> bool:
        """Mark an unknown face as assigned to a person. Returns True if a row changed."""
        ts = utc_now()
        with self.connect() as db:
            cursor = db.execute(
                "UPDATE unknown_faces SET status = 'assigned', assigned_person_id = ?, reviewed_at = ? WHERE id = ?",
                (person_id, ts, face_id),
            )
            return cursor.rowcount > 0

    def dismiss_unknown_face(self, face_id: int) -> bool:
        """Mark an unknown face as dismissed. Returns True if a row changed."""
        ts = utc_now()
        with self.connect() as db:
            cursor = db.execute(
                "UPDATE unknown_faces SET status = 'dismissed', reviewed_at = ? WHERE id = ?",
                (ts, face_id),
            )
            return cursor.rowcount > 0

    def delete_unknown_face(self, face_id: int) -> bool:
        """Permanently remove a capture. Returns True if a row was deleted."""
        with self.connect() as db:
            cursor = db.execute("DELETE FROM unknown_faces WHERE id = ?", (face_id,))
            return cursor.rowcount > 0

    def purge_old_unknown_faces(self, older_than: str) -> int:
        """Remove old captures (auto-cleanup). Returns count deleted."""
        with self.connect() as db:
            cursor = db.execute(
                "DELETE FROM unknown_faces WHERE created_at < ? AND status != 'pending'",
                (older_than,),
            )
            return cursor.rowcount
