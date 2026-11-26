"""SQLite job store (aiosqlite). One short-lived connection per operation."""

from __future__ import annotations

import json
import time
import uuid
from pathlib import Path
from typing import Any

import aiosqlite

TERMINAL_STATUSES = {"ready", "failed", "awaiting_dimensions"}  # awaiting_dimensions survives restarts

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    filename TEXT NOT NULL,
    status TEXT NOT NULL,
    stage TEXT NOT NULL,
    progress REAL NOT NULL DEFAULT 0,
    message TEXT,
    current_version INTEGER NOT NULL DEFAULT 0,
    error TEXT,
    result_json TEXT
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    ts REAL NOT NULL,
    stage TEXT NOT NULL,
    level TEXT NOT NULL,
    message TEXT NOT NULL,
    progress REAL,
    data_json TEXT
);
CREATE INDEX IF NOT EXISTS idx_events_job ON events(job_id, id);
"""


class JobStore:
    def __init__(self, path: Path):
        self.path = path

    def _connect(self):
        return aiosqlite.connect(self.path, timeout=30)

    async def init(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        async with self._connect() as db:
            await db.execute("PRAGMA journal_mode=WAL")
            await db.executescript(SCHEMA)
            await db.commit()

    async def recover_interrupted(self) -> int:
        """Jobs that were running when the server stopped can never finish: mark them failed."""
        async with self._connect() as db:
            cur = await db.execute(
                "UPDATE jobs SET status='failed', stage='failed', error='server herstart tijdens verwerking', "
                "updated_at=? WHERE status NOT IN ('ready','failed','awaiting_dimensions')",
                (time.time(),),
            )
            await db.commit()
            return cur.rowcount

    async def create_job(self, filename: str) -> dict[str, Any]:
        job_id = uuid.uuid4().hex[:12]
        now = time.time()
        async with self._connect() as db:
            await db.execute(
                "INSERT INTO jobs (id, created_at, updated_at, filename, status, stage, progress) "
                "VALUES (?, ?, ?, ?, 'queued', 'queued', 0)",
                (job_id, now, now, filename),
            )
            await db.commit()
        return await self.get_job(job_id)  # type: ignore[return-value]

    async def update_job(self, job_id: str, **fields: Any) -> None:
        if not fields:
            return
        if "result" in fields:
            fields["result_json"] = json.dumps(fields.pop("result"))
        fields["updated_at"] = time.time()
        cols = ", ".join(f"{k}=?" for k in fields)
        async with self._connect() as db:
            await db.execute(f"UPDATE jobs SET {cols} WHERE id=?", (*fields.values(), job_id))  # noqa: S608
            await db.commit()

    async def get_job(self, job_id: str) -> dict[str, Any] | None:
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute("SELECT * FROM jobs WHERE id=?", (job_id,))
            row = await cur.fetchone()
        return _row_to_job(row) if row else None

    async def list_jobs(self, limit: int = 100) -> list[dict[str, Any]]:
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute("SELECT * FROM jobs ORDER BY created_at DESC LIMIT ?", (limit,))
            rows = await cur.fetchall()
        return [_row_to_job(r) for r in rows]

    async def delete_job(self, job_id: str) -> None:
        async with self._connect() as db:
            await db.execute("DELETE FROM events WHERE job_id=?", (job_id,))
            await db.execute("DELETE FROM jobs WHERE id=?", (job_id,))
            await db.commit()

    async def add_event(self, job_id: str, stage: str, message: str, level: str = "info",
                        progress: float | None = None, data: dict | None = None) -> int:
        async with self._connect() as db:
            cur = await db.execute(
                "INSERT INTO events (job_id, ts, stage, level, message, progress, data_json) VALUES (?,?,?,?,?,?,?)",
                (job_id, time.time(), stage, level, message, progress, json.dumps(data) if data else None),
            )
            await db.commit()
            return int(cur.lastrowid or 0)

    async def get_events(self, job_id: str, after_id: int = 0, limit: int = 500) -> list[dict[str, Any]]:
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                "SELECT * FROM events WHERE job_id=? AND id>? ORDER BY id LIMIT ?", (job_id, after_id, limit)
            )
            rows = await cur.fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["data"] = json.loads(d.pop("data_json")) if d.get("data_json") else None
            out.append(d)
        return out


def _row_to_job(row) -> dict[str, Any]:
    d = dict(row)
    d["result"] = json.loads(d.pop("result_json")) if d.get("result_json") else None
    return d
