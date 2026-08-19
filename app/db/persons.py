from __future__ import annotations

import json
from typing import Any

from app.auth import utc_now


class PersonsMixin:
    """Face-recognition enrolment store (Stage 2).

    ``persons`` are named individuals; ``person_faces`` holds one or more face
    embeddings per person. An embedding row records the ``model`` id and vector
    ``dim`` it was produced with -- vectors from different embedding models are
    not comparable, so the matcher loads and compares only the rows for the
    active model (see :meth:`load_face_embeddings`).

    SQLite's ``foreign_keys`` PRAGMA is off on these connections (matching the
    rest of the schema), so ``person_faces`` rows are removed explicitly in
    :meth:`delete_person` rather than relying on the declared ON DELETE CASCADE.
    """

    def add_person(self, name: str, *, notes: str | None = None, created_at: str | None = None) -> int:
        timestamp = created_at or utc_now()
        with self.connect() as db:
            cursor = db.execute(
                "INSERT INTO persons (name, notes, created_at, updated_at) VALUES (?, ?, ?, ?)",
                (name, notes, timestamp, timestamp),
            )
            return int(cursor.lastrowid)

    def get_person(self, person_id: int) -> dict[str, Any] | None:
        with self.connect() as db:
            row = db.execute(
                """
                SELECT p.*, COUNT(f.id) AS face_count
                FROM persons p
                LEFT JOIN person_faces f ON f.person_id = p.id
                WHERE p.id = ?
                GROUP BY p.id
                """,
                (person_id,),
            ).fetchone()
            return dict(row) if row else None

    def list_persons(self) -> list[dict[str, Any]]:
        with self.connect() as db:
            rows = db.execute(
                """
                SELECT p.*, COUNT(f.id) AS face_count
                FROM persons p
                LEFT JOIN person_faces f ON f.person_id = p.id
                GROUP BY p.id
                ORDER BY p.name COLLATE NOCASE ASC
                """
            ).fetchall()
            return [dict(row) for row in rows]

    def update_person(
        self,
        person_id: int,
        *,
        name: str | None = None,
        notes: str | None = None,
        updated_at: str | None = None,
    ) -> bool:
        """Update a person's name and/or notes. Returns True if a row changed.

        ``notes`` is only written when the caller passes the keyword (``None``
        means "leave unchanged"), so a rename never wipes existing notes.
        """
        fields: list[str] = []
        params: list[Any] = []
        if name is not None:
            fields.append("name = ?")
            params.append(name)
        if notes is not None:
            fields.append("notes = ?")
            params.append(notes)
        if not fields:
            return False
        fields.append("updated_at = ?")
        params.append(updated_at or utc_now())
        params.append(person_id)
        with self.connect() as db:
            cursor = db.execute(f"UPDATE persons SET {', '.join(fields)} WHERE id = ?", params)
            return cursor.rowcount > 0

    def delete_person(self, person_id: int) -> bool:
        """Delete a person and all of their enrolled face embeddings."""
        with self.connect() as db:
            db.execute("DELETE FROM person_faces WHERE person_id = ?", (person_id,))
            cursor = db.execute("DELETE FROM persons WHERE id = ?", (person_id,))
            return cursor.rowcount > 0

    def add_person_face(
        self,
        person_id: int,
        *,
        embedding: bytes,
        dim: int,
        model: str,
        source_snapshot: str | None = None,
        created_at: str | None = None,
    ) -> int:
        with self.connect() as db:
            cursor = db.execute(
                """
                INSERT INTO person_faces (person_id, embedding, dim, model, source_snapshot, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (person_id, embedding, int(dim), model, source_snapshot, created_at or utc_now()),
            )
            return int(cursor.lastrowid)

    def list_person_faces(self, person_id: int) -> list[dict[str, Any]]:
        """List a person's enrolled faces WITHOUT the raw embedding blob.

        The vector bytes are only needed by the matcher (loaded in bulk via
        :meth:`load_face_embeddings`); listing them for the UI would ship
        kilobytes of opaque binary per row for no benefit.
        """
        with self.connect() as db:
            rows = db.execute(
                """
                SELECT id, person_id, dim, model, source_snapshot, created_at
                FROM person_faces
                WHERE person_id = ?
                ORDER BY created_at ASC
                """,
                (person_id,),
            ).fetchall()
            return [dict(row) for row in rows]

    def delete_person_face(self, face_id: int) -> bool:
        with self.connect() as db:
            cursor = db.execute("DELETE FROM person_faces WHERE id = ?", (face_id,))
            return cursor.rowcount > 0

    def load_face_embeddings(self, model: str) -> list[dict[str, Any]]:
        """Return every enrolled face embedding for ``model``, with its owner.

        This is the matcher's input: one row per enrolled face carrying the
        owning ``person_id`` / ``name`` and the raw ``embedding`` bytes + ``dim``
        so the caller can rebuild the vector matrix. Filtered to a single model
        so vectors from a different embedder are never compared.
        """
        with self.connect() as db:
            rows = db.execute(
                """
                SELECT f.id AS face_id, f.person_id, p.name AS person_name,
                       f.embedding, f.dim
                FROM person_faces f
                JOIN persons p ON p.id = f.person_id
                WHERE f.model = ?
                ORDER BY f.person_id ASC, f.id ASC
                """,
                (model,),
            ).fetchall()
            return [dict(row) for row in rows]

    def purge_face_identities(self, *, older_than: str) -> int:
        """Strip recognised-identity data from events older than ``older_than``.

        Enforces the face-recognition retention policy: the ``face_identities``
        block written onto an event's metadata (recognised people + unknown
        count) is removed once the event ages past the retention window, while
        the event itself and its detections are kept. Returns the number of
        events anonymised.
        """
        purged = 0
        with self.connect() as db:
            rows = db.execute(
                "SELECT id, metadata FROM events "
                "WHERE created_at < ? AND metadata LIKE '%face_identities%'",
                (older_than,),
            ).fetchall()
            for row in rows:
                try:
                    metadata = json.loads(row['metadata'] or '{}')
                except (TypeError, ValueError):
                    continue
                if isinstance(metadata, dict) and 'face_identities' in metadata:
                    metadata.pop('face_identities', None)
                    db.execute(
                        "UPDATE events SET metadata = ? WHERE id = ?",
                        (json.dumps(metadata), row['id']),
                    )
                    purged += 1
        return purged

    def count_person_faces(self, *, model: str | None = None) -> int:
        with self.connect() as db:
            if model is None:
                row = db.execute("SELECT COUNT(*) AS count FROM person_faces").fetchone()
            else:
                row = db.execute("SELECT COUNT(*) AS count FROM person_faces WHERE model = ?", (model,)).fetchone()
            return int(row['count'])
