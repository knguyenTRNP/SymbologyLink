from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator


class CacheError(RuntimeError):
    pass


class SQLiteCache:
    """Small provider-response cache. Keys and values must never contain secrets."""

    def __init__(self, path: str | Path = ".symbologylink/cache.sqlite3", default_ttl_seconds: int = 86400):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.default_ttl_seconds = default_ttl_seconds
        try:
            self._initialize()
        except CacheError as exc:
            if self.path.exists() and any(text in str(exc).casefold() for text in ("not a database", "malformed")):
                self._quarantine()
                self._initialize()
            else:
                raise

    def _initialize(self) -> None:
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

    def _quarantine(self) -> None:
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        for suffix in ("", "-wal", "-shm"):
            source = Path(str(self.path) + suffix)
            if source.exists():
                source.replace(source.with_name(f"{source.name}.corrupt-{timestamp}"))

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        connection: sqlite3.Connection | None = None
        try:
            connection = sqlite3.connect(self.path, timeout=30)
            connection.execute("PRAGMA journal_mode=WAL")
            yield connection
            connection.commit()
        except sqlite3.Error as exc:
            if connection is not None:
                try:
                    connection.rollback()
                except sqlite3.Error:
                    pass
            raise CacheError(
                f"Unable to use cache database at {self.path}: {exc}. "
                "Choose a local writable path with --cache or SYMBOLOGYLINK_CACHE."
            ) from exc
        finally:
            if connection is not None:
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
        try:
            serialized = json.dumps(value, separators=(",", ":"))
        except (TypeError, ValueError) as exc:
            raise CacheError("Cache values must be valid JSON; the existing entry was not changed.") from exc
        with self._connect() as connection:
            connection.execute(
                """INSERT INTO provider_cache(provider,query_type,cache_key,value_json,created_at,expires_at)
                   VALUES(?,?,?,?,?,?)
                   ON CONFLICT(provider,query_type,cache_key) DO UPDATE SET
                     value_json=excluded.value_json,
                     created_at=excluded.created_at,
                     expires_at=excluded.expires_at""",
                (provider, query_type, key, serialized, now.isoformat(), expires.isoformat() if expires else None),
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
