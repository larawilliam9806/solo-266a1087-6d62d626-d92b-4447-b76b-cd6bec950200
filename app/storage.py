"""SQLite persistence for compressed furnace archives."""

from __future__ import annotations

import json
import os
import sqlite3
import threading
from datetime import datetime, timezone
from typing import Any

DEFAULT_DB_PATH = os.environ.get("FURNACE_DB_PATH", os.path.join("data", "archive.db"))

_SCHEMA = """
CREATE TABLE IF NOT EXISTS archives (
    run_id       TEXT PRIMARY KEY,
    segments     TEXT NOT NULL,
    limit_value  TEXT NOT NULL,
    tolerance    TEXT NOT NULL,
    max_points   INTEGER NOT NULL,
    point_count  INTEGER NOT NULL,
    source_point_count INTEGER NOT NULL,
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL
);
"""


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class ArchiveStore:
    """A tiny SQLite-backed archive keyed by furnace run id.

    Connections are opened per operation so the store is safe to use from
    FastAPI's worker threads.  Writes are serialised by a lock, which keeps
    the "keep the old archive on failure" replacement sequence atomic.
    """

    def __init__(self, db_path: str | None = None) -> None:
        self.db_path = db_path or DEFAULT_DB_PATH
        self._lock = threading.Lock()
        self._initialised = False

    def _ensure_schema(self) -> None:
        if self._initialised:
            return
        with self._lock:
            if self._initialised:
                return
            parent = os.path.dirname(os.path.abspath(self.db_path))
            os.makedirs(parent, exist_ok=True)
            with self._connect() as conn:
                conn.execute(_SCHEMA)
                # Lightweight in-place migration for archives created by an
                # older build that did not track the source sample count.
                columns = {
                    row["name"]
                    for row in conn.execute("PRAGMA table_info(archives)").fetchall()
                }
                if "source_point_count" not in columns:
                    conn.execute(
                        "ALTER TABLE archives ADD COLUMN source_point_count "
                        "INTEGER NOT NULL DEFAULT 0"
                    )
            self._initialised = True

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=30.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    def get(self, run_id: str) -> dict[str, Any] | None:
        self._ensure_schema()
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM archives WHERE run_id = ?", (run_id,)
            ).fetchone()
        return self._row_to_dict(row) if row is not None else None

    def list_runs(self) -> list[dict[str, Any]]:
        self._ensure_schema()
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM archives ORDER BY updated_at DESC, run_id ASC"
            ).fetchall()
        return [self._row_to_dict(row) for row in rows]

    def put(
        self,
        run_id: str,
        segments: list[list[dict[str, int]]],
        limit_value: str,
        tolerance: str,
        max_points: int,
        point_count: int,
        source_point_count: int,
    ) -> dict[str, Any]:
        self._ensure_schema()
        now = _utcnow()
        segments_json = json.dumps(segments, separators=(",", ":"), ensure_ascii=False)
        with self._lock, self._connect() as conn:
            existing = conn.execute(
                "SELECT created_at FROM archives WHERE run_id = ?", (run_id,)
            ).fetchone()
            created = existing["created_at"] if existing else now
            conn.execute(
                """
                INSERT INTO archives
                    (run_id, segments, limit_value, tolerance, max_points,
                     point_count, source_point_count, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(run_id) DO UPDATE SET
                    segments = excluded.segments,
                    limit_value = excluded.limit_value,
                    tolerance = excluded.tolerance,
                    max_points = excluded.max_points,
                    point_count = excluded.point_count,
                    source_point_count = excluded.source_point_count,
                    updated_at = excluded.updated_at
                """,
                (
                    run_id,
                    segments_json,
                    limit_value,
                    tolerance,
                    max_points,
                    point_count,
                    source_point_count,
                    created,
                    now,
                ),
            )
        return {
            "run_id": run_id,
            "segments": segments,
            "limit": limit_value,
            "tolerance": tolerance,
            "max_points": max_points,
            "point_count": point_count,
            "source_point_count": source_point_count,
            "created_at": created,
            "updated_at": now,
        }

    @staticmethod
    def _row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
        # ``limit`` and ``tolerance`` are stored as exact rational literals so
        # no precision is lost on the round trip; callers convert them.
        return {
            "run_id": row["run_id"],
            "segments": json.loads(row["segments"]),
            "limit": row["limit_value"],
            "tolerance": row["tolerance"],
            "max_points": row["max_points"],
            "point_count": row["point_count"],
            "source_point_count": row["source_point_count"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }
