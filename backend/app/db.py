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
CREATE TABLE IF NOT EXISTS spec_corrections (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    action TEXT NOT NULL,              -- edit | verified
    created_at REAL NOT NULL,
    input_source TEXT,
    was_modified INTEGER NOT NULL,
    changed_feature_types TEXT NOT NULL,  -- JSON list
    field_diffs TEXT NOT NULL             -- JSON list of {path, before, after}
);
CREATE INDEX IF NOT EXISTS idx_corrections_job ON spec_corrections(job_id, id);
CREATE TABLE IF NOT EXISTS batches (
    id TEXT PRIMARY KEY,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    filename TEXT NOT NULL,
    page_count INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL,                 -- splitting | queued | running | done | failed
    message TEXT,
    groups_json TEXT
);
"""
JOB_COLUMNS = {  # v1.5 additions, added to existing databases on start
    "ai_initial_spec_json": "TEXT", "verified_at": "REAL", "batch_id": "TEXT", "batch_index": "INTEGER",
    "part_number": "TEXT", "revision": "TEXT",
}


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
            cur = await db.execute("PRAGMA table_info(jobs)")
            have = {r[1] for r in await cur.fetchall()}
            for col, typ in JOB_COLUMNS.items():
                if col not in have:
                    await db.execute(f"ALTER TABLE jobs ADD COLUMN {col} {typ}")
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


    # -- v1.5 telemetry (spec_corrections) ------------------------------------------------------------
    async def add_correction(self, job_id: str, version: int, action: str, diff: dict[str, Any],
                             input_source: str | None) -> int:
        async with self._connect() as db:
            cur = await db.execute(
                "INSERT INTO spec_corrections (job_id, version, action, created_at, input_source, was_modified, "
                "changed_feature_types, field_diffs) VALUES (?,?,?,?,?,?,?,?)",
                (job_id, version, action, time.time(), input_source, int(bool(diff["was_modified"])),
                 json.dumps(diff["changed_feature_types"]), json.dumps(diff["field_diffs"], default=str)))
            await db.commit()
            return int(cur.lastrowid or 0)

    async def list_corrections(self, job_id: str | None = None) -> list[dict[str, Any]]:
        q, args = "SELECT * FROM spec_corrections", ()
        if job_id:
            q, args = q + " WHERE job_id=?", (job_id,)
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            rows = await (await db.execute(q + " ORDER BY id", args)).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["was_modified"] = bool(d["was_modified"])
            d["changed_feature_types"] = json.loads(d["changed_feature_types"])
            d["field_diffs"] = json.loads(d["field_diffs"])
            out.append(d)
        return out

    # -- v1.5 batches ---------------------------------------------------------------------------------------
    async def create_batch(self, filename: str, page_count: int) -> dict[str, Any]:
        batch_id = "b" + uuid.uuid4().hex[:11]
        now = time.time()
        async with self._connect() as db:
            await db.execute("INSERT INTO batches (id, created_at, updated_at, filename, page_count, status) "
                             "VALUES (?,?,?,?,?, 'splitting')", (batch_id, now, now, filename, page_count))
            await db.commit()
        return await self.get_batch(batch_id)  # type: ignore[return-value]

    async def update_batch(self, batch_id: str, **fields: Any) -> None:
        if "groups" in fields:
            fields["groups_json"] = json.dumps(fields.pop("groups"))
        fields["updated_at"] = time.time()
        cols = ", ".join(f"{k}=?" for k in fields)
        async with self._connect() as db:
            await db.execute(f"UPDATE batches SET {cols} WHERE id=?", (*fields.values(), batch_id))  # noqa: S608
            await db.commit()

    async def get_batch(self, batch_id: str) -> dict[str, Any] | None:
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            row = await (await db.execute("SELECT * FROM batches WHERE id=?", (batch_id,))).fetchone()
        if not row:
            return None
        d = dict(row)
        d["groups"] = json.loads(d.pop("groups_json")) if d.get("groups_json") else []
        return d

    async def list_batches(self, limit: int = 50) -> list[dict[str, Any]]:
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            rows = await (await db.execute("SELECT id FROM batches ORDER BY created_at DESC LIMIT ?", (limit,))).fetchall()
        return [b for r in rows if (b := await self.get_batch(r["id"]))]

    async def jobs_by_part(self, part_number: str) -> list[dict[str, Any]]:
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            rows = await (await db.execute("SELECT * FROM jobs WHERE part_number=? ORDER BY created_at",
                                           (part_number,))).fetchall()
        return [_row_to_job(r) for r in rows]

    async def batch_jobs(self, batch_id: str) -> list[dict[str, Any]]:
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            rows = await (await db.execute("SELECT * FROM jobs WHERE batch_id=? ORDER BY batch_index", (batch_id,))).fetchall()
        return [_row_to_job(r) for r in rows]


def _row_to_job(row) -> dict[str, Any]:
    d = dict(row)
    d["result"] = json.loads(d.pop("result_json")) if d.get("result_json") else None
    return d
