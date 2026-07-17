from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator


class SQLiteCache:
    """Small provider-response cache. Keys and values must never contain secrets."""

    def __init__(self, path: str | Path = ".symbologylink/cache.sqlite3", default_ttl_seconds: int = 86400):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.default_ttl_seconds = default_ttl_seconds
        with self._connect() as connection:
            connection.execute("""
                CREATE TABLE IF NOT EXISTS provider_cache (
                    provider TEXT NOT NULL,
                    query_type TEXT NOT NULL,
                    cache_key TEXT NOT NULL,
                    value_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    expires_at TEXT,
                    PRIMARY KEY (provider, query_type, cache_key)
                )
            """)

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, timeout=30)
        connection.execute("PRAGMA journal_mode=WAL")
        try:
            yield connection
            connection.commit()
        finally:
            connection.close()

    def get(self, provider: str, query_type: str, key: str, allow_stale: bool = False) -> Any | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT value_json, expires_at FROM provider_cache WHERE provider=? AND query_type=? AND cache_key=?",
                (provider, query_type, key),
            ).fetchone()
        if not row:
            return None
        if row[1] and not allow_stale and datetime.fromisoformat(row[1]) < datetime.now(timezone.utc):
            return None
        return json.loads(row[0])

    def set(self, provider: str, query_type: str, key: str, value: Any, ttl_seconds: int | None = None) -> None:
        now = datetime.now(timezone.utc)
        ttl = self.default_ttl_seconds if ttl_seconds is None else ttl_seconds
        expires = now + timedelta(seconds=ttl) if ttl > 0 else None
        with self._connect() as connection:
            connection.execute(
                "INSERT OR REPLACE INTO provider_cache(provider,query_type,cache_key,value_json,created_at,expires_at) VALUES(?,?,?,?,?,?)",
                (provider, query_type, key, json.dumps(value, separators=(",", ":")), now.isoformat(), expires.isoformat() if expires else None),
            )

    def clear(self, provider: str | None = None) -> int:
        with self._connect() as connection:
            if provider:
                cursor = connection.execute("DELETE FROM provider_cache WHERE provider=?", (provider,))
            else:
                cursor = connection.execute("DELETE FROM provider_cache")
            return cursor.rowcount

    def stats(self) -> dict[str, Any]:
        with self._connect() as connection:
            rows = connection.execute("SELECT provider, COUNT(*), MIN(created_at), MAX(created_at) FROM provider_cache GROUP BY provider").fetchall()
        return {"path": str(self.path), "providers": {row[0]: {"entries": row[1], "oldest": row[2], "newest": row[3]} for row in rows}}
