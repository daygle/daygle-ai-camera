"""Unknown face capture store for the Review workflow.

When a detected face does not match any enrolled person, the face crop,
embedding, and thumbnail are stored so an admin can review and optionally
assign them to a person.
"""
from __future__ import annotations

from typing import Any

from app.auth import utc_now


class UnknownFaceAssignmentError(ValueError):
    """Raised when an unknown-face assignment cannot be completed."""


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

    def assign_unknown_face_with_embedding(
        self,
        face_id: int,
        *,
        person_id: int | None = None,
        person_name: str | None = None,
    ) -> dict[str, Any]:
        """Atomically enrol an unknown face and mark it reviewed.

        The previous API implementation read the pending row, inserted the
        embedding, and updated the status through three independent database
        transactions. Two concurrent admin requests could therefore enrol the
        same capture twice, or leave an orphan person when creating a new one.
        Keeping the read, optional person creation, embedding insert, and
        compare-and-set status update in one transaction makes assignment
        exactly-once at the database boundary.
        """
        if person_id is None and not person_name:
            raise UnknownFaceAssignmentError('Provide person_id or name.')
        with self.connect() as db:
            face = db.execute(
                "SELECT embedding, dim, model, status FROM unknown_faces WHERE id = ?",
                (face_id,),
            ).fetchone()
            if face is None:
                raise LookupError('Unknown face not found.')
            if face['status'] != 'pending':
                raise UnknownFaceAssignmentError('This face has already been reviewed.')

            created_person = person_id is None
            if created_person:
                created_at = utc_now()
                cursor = db.execute(
                    "INSERT INTO persons (name, notes, created_at, updated_at) VALUES (?, NULL, ?, ?)",
                    (person_name, created_at, created_at),
                )
                person_id = int(cursor.lastrowid)
                resolved_name = str(person_name)
            else:
                person = db.execute(
                    "SELECT id, name FROM persons WHERE id = ?", (int(person_id),)
                ).fetchone()
                if person is None:
                    raise LookupError('Person not found.')
                person_id = int(person['id'])
                resolved_name = str(person['name'])

            now = utc_now()
            db.execute(
                """
                INSERT INTO person_faces
                    (person_id, embedding, dim, model, source_snapshot, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    person_id,
                    bytes(face['embedding']),
                    int(face['dim']),
                    str(face['model']),
                    f'unknown-face:{face_id}',
                    now,
                ),
            )
            updated = db.execute(
                """
                UPDATE unknown_faces
                SET status = 'assigned', assigned_person_id = ?, reviewed_at = ?
                WHERE id = ? AND status = 'pending'
                """,
                (person_id, now, face_id),
            )
            if updated.rowcount != 1:
                # The transaction is rolled back by the context manager, which
                # also rolls back a person created above and its face row.
                raise UnknownFaceAssignmentError('This face has already been reviewed.')
            return {
                'person_id': person_id,
                'person_name': resolved_name,
                'created_person': created_person,
            }

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
