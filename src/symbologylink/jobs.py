from __future__ import annotations

import json
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class JobStore:
    def __init__(self, path: str | Path = ".symbologylink/jobs.sqlite3"):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.execute("""
                CREATE TABLE IF NOT EXISTS jobs (
                    id TEXT PRIMARY KEY,
                    idempotency_key TEXT UNIQUE,
                    status TEXT NOT NULL,
                    stage TEXT NOT NULL,
                    progress REAL NOT NULL DEFAULT 0,
                    rows_processed INTEGER NOT NULL DEFAULT 0,
                    total_rows INTEGER NOT NULL,
                    request_json TEXT NOT NULL,
                    results_json TEXT,
                    metrics_json TEXT,
                    error TEXT,
                    cancel_requested INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
            """)

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        try:
            yield connection
            connection.commit()
        finally:
            connection.close()

    def create(self, records: list[dict[str, Any]], idempotency_key: str | None = None) -> tuple[dict[str, Any], bool]:
        if idempotency_key:
            with self._connect() as connection:
                existing = connection.execute("SELECT * FROM jobs WHERE idempotency_key=?", (idempotency_key,)).fetchone()
            if existing:
                return self._public(existing), False
        job_id, now = str(uuid.uuid4()), _now()
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO jobs(id,idempotency_key,status,stage,total_rows,request_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
                (job_id, idempotency_key, "queued", "queued", len(records), json.dumps(records, separators=(",", ":")), now, now),
            )
        return self.get(job_id), True

    def get(self, job_id: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        return self._public(row) if row else None

    def request(self, job_id: str) -> list[dict[str, Any]]:
        with self._connect() as connection:
            row = connection.execute("SELECT request_json FROM jobs WHERE id=?", (job_id,)).fetchone()
        if not row:
            raise KeyError(job_id)
        return json.loads(row[0])

    def results(self, job_id: str, offset: int = 0, limit: int = 100) -> dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute("SELECT results_json,status FROM jobs WHERE id=?", (job_id,)).fetchone()
        if not row:
            raise KeyError(job_id)
        values = json.loads(row[0]) if row[0] else []
        return {"jobId": job_id, "status": row[1], "offset": offset, "limit": limit, "total": len(values), "results": values[offset:offset + limit]}

    def metrics(self, job_id: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute("SELECT metrics_json FROM jobs WHERE id=?", (job_id,)).fetchone()
        if not row:
            raise KeyError(job_id)
        return json.loads(row[0]) if row[0] else None

    def update(self, job_id: str, **values: Any) -> None:
        allowed = {"status", "stage", "progress", "rows_processed", "results_json", "metrics_json", "error", "cancel_requested"}
        values = {key: value for key, value in values.items() if key in allowed}
        values["updated_at"] = _now()
        assignments = ",".join(f"{key}=?" for key in values)
        with self._connect() as connection:
            connection.execute(f"UPDATE jobs SET {assignments} WHERE id=?", (*values.values(), job_id))

    def cancel(self, job_id: str) -> bool:
        if not self.get(job_id):
            return False
        self.update(job_id, cancel_requested=1)
        return True

    def cancellation_requested(self, job_id: str) -> bool:
        with self._connect() as connection:
            row = connection.execute("SELECT cancel_requested FROM jobs WHERE id=?", (job_id,)).fetchone()
        return bool(row and row[0])

    @staticmethod
    def _public(row: sqlite3.Row) -> dict[str, Any]:
        return {"jobId": row["id"], "status": row["status"], "stage": row["stage"], "progress": row["progress"], "rowsProcessed": row["rows_processed"], "totalRows": row["total_rows"], "error": row["error"], "createdAt": row["created_at"], "updatedAt": row["updated_at"]}
