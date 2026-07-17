from __future__ import annotations

import json
import shutil
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class DatasetStore:
    """Persistent metadata and filesystem lifecycle for uploaded datasets."""

    def __init__(self, database: str | Path = ".symbologylink/datasets.sqlite3", root: str | Path = ".symbologylink/uploads"):
        self.database = Path(database)
        self.root = Path(root)
        self.database.parent.mkdir(parents=True, exist_ok=True)
        self.root.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.execute("""
                CREATE TABLE IF NOT EXISTS datasets (
                    id TEXT PRIMARY KEY,
                    original_filename TEXT NOT NULL,
                    stored_path TEXT NOT NULL,
                    content_type TEXT,
                    status TEXT NOT NULL,
                    file_size INTEGER NOT NULL DEFAULT 0,
                    sha256 TEXT,
                    file_type TEXT,
                    row_count INTEGER,
                    column_count INTEGER,
                    profile_json TEXT,
                    error TEXT,
                    version INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    deleted_at TEXT
                )
            """)

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.database, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        try:
            yield connection
            connection.commit()
        finally:
            connection.close()

    def file_path(self, dataset_id: str, version: int, suffix: str) -> Path:
        if not dataset_id or any(character not in "0123456789abcdef-" for character in dataset_id.lower()):
            raise ValueError("Invalid dataset ID.")
        directory = (self.root / dataset_id).resolve()
        root = self.root.resolve()
        if directory.parent != root:
            raise ValueError("Dataset path escaped the upload root.")
        directory.mkdir(parents=True, exist_ok=True)
        return directory / f"v{version}{suffix.lower()}"

    def create(self, original_filename: str, content_type: str | None, suffix: str) -> dict[str, Any]:
        dataset_id, now = str(uuid.uuid4()), _now()
        stored_path = self.file_path(dataset_id, 1, suffix)
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO datasets(id,original_filename,stored_path,content_type,status,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
                (dataset_id, original_filename, str(stored_path), content_type, "uploading", now, now),
            )
        return self.get(dataset_id)

    def get(self, dataset_id: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM datasets WHERE id=?", (dataset_id,)).fetchone()
        return self._public(row) if row else None

    def internal(self, dataset_id: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM datasets WHERE id=?", (dataset_id,)).fetchone()
        return dict(row) if row else None

    def list(self, offset: int = 0, limit: int = 100, include_deleted: bool = False) -> dict[str, Any]:
        where = "" if include_deleted else "WHERE deleted_at IS NULL"
        with self._connect() as connection:
            total = connection.execute(f"SELECT COUNT(*) FROM datasets {where}").fetchone()[0]
            rows = connection.execute(f"SELECT * FROM datasets {where} ORDER BY created_at DESC LIMIT ? OFFSET ?", (limit, offset)).fetchall()
        return {"offset": offset, "limit": limit, "total": total, "datasets": [self._public(row) for row in rows]}

    def update(self, dataset_id: str, **values: Any) -> dict[str, Any]:
        allowed = {"original_filename", "stored_path", "content_type", "status", "file_size", "sha256", "file_type", "row_count", "column_count", "profile_json", "error", "version", "deleted_at"}
        values = {key: value for key, value in values.items() if key in allowed}
        values["updated_at"] = _now()
        assignments = ",".join(f"{key}=?" for key in values)
        with self._connect() as connection:
            cursor = connection.execute(f"UPDATE datasets SET {assignments} WHERE id=?", (*values.values(), dataset_id))
            if cursor.rowcount == 0:
                raise KeyError(dataset_id)
        return self.get(dataset_id)

    def delete(self, dataset_id: str) -> dict[str, Any]:
        row = self.internal(dataset_id)
        if not row:
            raise KeyError(dataset_id)
        directory = (self.root / dataset_id).resolve()
        if directory.parent != self.root.resolve():
            raise ValueError("Dataset path escaped the upload root.")
        if directory.exists():
            shutil.rmtree(directory)
        return self.update(dataset_id, status="deleted", stored_path="", profile_json=None, deleted_at=_now())

    @staticmethod
    def _public(row: sqlite3.Row) -> dict[str, Any]:
        profile = json.loads(row["profile_json"]) if row["profile_json"] else None
        return {
            "datasetId": row["id"], "filename": row["original_filename"], "contentType": row["content_type"],
            "status": row["status"], "fileSize": row["file_size"], "sha256": row["sha256"],
            "fileType": row["file_type"], "rowCount": row["row_count"], "columnCount": row["column_count"],
            "profile": profile, "error": row["error"], "version": row["version"],
            "createdAt": row["created_at"], "updatedAt": row["updated_at"], "deletedAt": row["deleted_at"],
        }
