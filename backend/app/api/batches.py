"""Batch intake API (SPEC_V1_5_CAM_SHOPFLOOR.md §2.5, §4): multi-page PDF -> child jobs, status, SSE, ZIP."""

from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path

from fastapi import APIRouter, File, HTTPException, Request, UploadFile
from fastapi.responses import Response, StreamingResponse

from app.api.jobs import MAX_UPLOAD_BYTES, _pipeline, _store
from app.services import batch_runner as br

router = APIRouter(prefix="/api", tags=["batches"])
MAX_BATCH_BYTES = 4 * MAX_UPLOAD_BYTES


@router.post("/batches", status_code=202)
async def create_batch(request: Request, file: UploadFile = File(...)) -> dict:
    name = Path(file.filename or "batch.pdf").name
    data = await file.read(MAX_BATCH_BYTES + 1)
    if not data.startswith(b"%PDF-"):
        raise HTTPException(415, "file is not a PDF")
    if len(data) > MAX_BATCH_BYTES:
        raise HTTPException(413, f"PDF groter dan {MAX_BATCH_BYTES // 2**20} MB")
    import pymupdf

    try:
        with pymupdf.open(stream=data, filetype="pdf") as doc:
            pages = doc.page_count
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(422, f"PDF onleesbaar: {exc}") from exc
    store, pipeline = _store(request), _pipeline(request)
    batch = await store.create_batch(name, pages)
    bdir = br.batch_dir(pipeline, batch["id"])
    bdir.mkdir(parents=True, exist_ok=True)
    (bdir / "original.pdf").write_bytes(data)
    # Split synchronously (text layer: milliseconds per page) so the response carries the job count.
    groups = await br.split_batch(pipeline, batch["id"])
    pipeline._background(batch["id"], br.run_children(pipeline, batch["id"], groups))
    return {"batch_id": batch["id"], "job_count": len(groups), "page_count": pages,
            "jobs": [{"job_id": g["job_id"], "part_number": g["part_number"], "pages": g["pages"]} for g in groups]}


@router.get("/batches")
async def list_batches(request: Request) -> list[dict]:
    pipeline = _pipeline(request)
    out = []
    for b in await _store(request).list_batches():
        st = await br.batch_status(pipeline, b["id"])
        if st:
            st.pop("jobs")
            out.append(st)
    return out


async def _require(request: Request, batch_id: str) -> dict:
    st = await br.batch_status(_pipeline(request), batch_id)
    if st is None:
        raise HTTPException(404, "batch not found")
    return st


@router.get("/batches/{batch_id}")
async def get_batch(request: Request, batch_id: str) -> dict:
    return await _require(request, batch_id)


@router.get("/batches/{batch_id}/events")
async def batch_events(request: Request, batch_id: str) -> StreamingResponse:
    """SSE: a ``batch`` event with the aggregate + per-child status whenever something changes; ``end`` when done."""
    await _require(request, batch_id)
    pipeline = _pipeline(request)

    async def gen():
        last = None
        while True:
            if await request.is_disconnected():
                return
            st = await br.batch_status(pipeline, batch_id)
            if st is None:
                return
            snap = json.dumps(st, sort_keys=True)
            if snap != last:
                last = snap
                yield f"event: batch\ndata: {snap}\n\n"
            if st["status"] in ("done", "failed") and st["finished_count"] >= st["job_count"]:
                yield "event: end\ndata: {}\n\n"
                return
            await asyncio.sleep(1.0)

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@router.get("/batches/{batch_id}/download-zip")
async def download_zip(request: Request, batch_id: str) -> Response:
    st = await _require(request, batch_id)
    if not st["ready_count"]:
        raise HTTPException(409, "nog geen onderdelen gereed")
    data = await asyncio.to_thread(br.build_zip, _pipeline(request), st)
    stem = re.sub(r"[^A-Za-z0-9._-]+", "_", re.sub(r"\.pdf$", "", st["filename"], flags=re.IGNORECASE)) or "batch"
    return Response(data, media_type="application/zip",
                    headers={"Content-Disposition": f'attachment; filename="{stem}_STEP.zip"'})
