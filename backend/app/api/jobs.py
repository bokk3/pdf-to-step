"""REST & SSE API (ARCHITECTURE.md §7)."""

from __future__ import annotations

import asyncio
import json
import shutil
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Body, File, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel, ValidationError

from app.db import JobStore
from app.schemas.part_spec import PartSpec, partspec_json_schema
from app.services.pipeline import TERMINAL, Pipeline

router = APIRouter(prefix="/api", tags=["jobs"])
MAX_UPLOAD_BYTES = 50 * 1024 * 1024
MEDIA = {".pdf": "application/pdf", ".glb": "model/gltf-binary", ".png": "image/png", ".json": "application/json",
         ".step": "application/step", ".txt": "text/plain", ".svg": "image/svg+xml", ".dxf": "image/vnd.dxf"}
VERSIONED = {"model.step", "preview.glb", "ortho_check.png", "ortho_top.png", "ortho_front.png", "ortho_right.png",
             "ortho_iso.png", "dfm_report.json", "drawing.pdf", "drawing.svg", "drawing.dxf", "drawing.json",
             "setup_sheet.pdf", "cam_colors.json"}


def _store(request: Request) -> JobStore:
    return request.app.state.store


def _pipeline(request: Request) -> Pipeline:
    return request.app.state.pipeline


async def _require_job(request: Request, job_id: str) -> dict[str, Any]:
    job = await _store(request).get_job(job_id)
    if not job:
        raise HTTPException(404, "job not found")
    return job


def _artifact_urls(pipeline: Pipeline, job_id: str, version: int | None) -> dict[str, Any]:
    jd = pipeline.job_dir(job_id)
    base = f"/api/jobs/{job_id}/artifacts"
    urls: dict[str, Any] = {"original.pdf": f"{base}/original.pdf"} if (jd / "original.pdf").exists() else {}
    vdir = jd / "versions" / f"v{version}" if version else None
    if vdir and vdir.exists():
        for name in sorted(VERSIONED):
            if (vdir / name).exists():
                urls[name] = f"{base}/{name}?version={version}"
    urls["tiles"] = {p.stem: f"{base}/tiles/{p.name}" for p in sorted((jd / "tiles").glob("*.png"))
                     if ".llm" not in p.name}
    return urls


def _summary(pipeline: Pipeline, job: dict[str, Any]) -> dict[str, Any]:
    out = {k: job[k] for k in ("status", "filename", "created_at", "updated_at", "progress", "message",
                               "current_version", "error")}
    out["job_id"] = job["id"]
    out["running"] = pipeline.is_running(job["id"])
    out["verified_at"] = job.get("verified_at")
    out["batch_id"] = job.get("batch_id")
    out["part_number"] = job.get("part_number")
    out["revision"] = job.get("revision")
    res = job.get("result") or {}
    out["dfm"] = ({k: res.get(k) for k in ("passed", "errors", "warnings", "needs_review", "ambiguities")}
                  if res else None)
    return out


def _detail(pipeline: Pipeline, job: dict[str, Any], version: int | None = None) -> dict[str, Any]:
    job_id = job["id"]
    out = _summary(pipeline, job)
    hist = pipeline.history(job_id)
    v = version or job["current_version"] or (hist[-1]["version"] if hist else 0)
    out["version"] = v
    out["versions"] = hist
    out["part_spec"] = json.loads(pipeline.spec_path(job_id, v).read_text(encoding="utf-8")) if v else None
    rp = pipeline.job_dir(job_id) / "versions" / f"v{v}" / "dfm_report.json"
    out["dfm_report"] = json.loads(rp.read_text(encoding="utf-8")) if rp.exists() else None
    out["iteration_logs"] = pipeline.iteration_logs(job_id)
    out["critic"] = (job.get("result") or {}).get("critic")
    out["artifacts"] = _artifact_urls(pipeline, job_id, v)
    dp = pipeline.job_dir(job_id) / "versions" / f"v{v}" / "drawing.json"
    out["drawing"] = json.loads(dp.read_text(encoding="utf-8")) if dp.exists() else None
    sp = pipeline.job_dir(job_id) / "step_import.json"
    out["step_import"] = json.loads(sp.read_text(encoding="utf-8")) if sp.exists() else None
    return out


# --------------------------------------------------------------------------- #


@router.get("/schema/part_spec")
async def get_schema() -> dict:
    return partspec_json_schema()


@router.post("/jobs", status_code=202)
async def upload(request: Request, file: UploadFile = File(...)) -> dict:
    name = Path(file.filename or "drawing.pdf").name
    head = await file.read(5)
    if head != b"%PDF-":
        raise HTTPException(415, "file is not a PDF")
    store, pipeline = _store(request), _pipeline(request)
    job = await store.create_job(name)
    jd = pipeline.job_dir(job["id"])
    jd.mkdir(parents=True, exist_ok=True)
    size = len(head)
    with open(jd / "original.pdf", "wb") as fh:
        fh.write(head)
        while chunk := await file.read(1024 * 1024):
            size += len(chunk)
            if size > MAX_UPLOAD_BYTES:
                fh.close()
                shutil.rmtree(jd, ignore_errors=True)
                await store.delete_job(job["id"])
                raise HTTPException(413, "PDF groter dan 50 MB")
            fh.write(chunk)
    await store.add_event(job["id"], "queued", f"{name} ontvangen ({size / 1024:.0f} KB)", progress=0.01)
    pipeline.start_new_job(job["id"], name)
    return {"job_id": job["id"], "status": "queued", "filename": name}


@router.get("/jobs")
async def list_jobs(request: Request, limit: int = Query(100, le=500)) -> list[dict]:
    pipeline = _pipeline(request)
    return [_summary(pipeline, j) for j in await _store(request).list_jobs(limit)]


@router.get("/jobs/{job_id}")
async def get_job(request: Request, job_id: str, version: int | None = None) -> dict:
    job = await _require_job(request, job_id)
    pipeline = _pipeline(request)
    if version and not pipeline.spec_path(job_id, version).exists():
        raise HTTPException(404, "version not found")
    return _detail(pipeline, job, version)


@router.delete("/jobs/{job_id}", status_code=204)
async def delete_job(request: Request, job_id: str) -> None:
    await _require_job(request, job_id)
    pipeline = _pipeline(request)
    if pipeline.is_running(job_id):
        raise HTTPException(409, "job is running")
    try:
        await pipeline.llm.release_files(pipeline.job_dir(job_id))
    except Exception:  # noqa: BLE001 - remote cleanup is best effort
        pass
    await _store(request).delete_job(job_id)
    shutil.rmtree(pipeline.job_dir(job_id), ignore_errors=True)


@router.get("/jobs/{job_id}/events")
async def stream_events(request: Request, job_id: str, after: int = 0) -> StreamingResponse:
    """SSE: replays history after `after` / Last-Event-ID, then streams live stage updates."""
    await _require_job(request, job_id)
    store, pipeline = _store(request), _pipeline(request)
    last_id = int(request.headers.get("last-event-id") or after or 0)

    async def gen():
        nonlocal last_id
        idle = 0.0
        yield "retry: 2000\n\n"
        while not await request.is_disconnected():
            events = await store.get_events(job_id, after_id=last_id)
            for e in events:
                last_id = e["id"]
                yield f"id: {e['id']}\nevent: stage\ndata: {json.dumps(e)}\n\n"
            job = await store.get_job(job_id)
            if job is None:
                break
            if (not events and job["status"] in TERMINAL and not pipeline.is_running(job_id)
                    and not pipeline.has_background(job_id)):
                yield f"event: end\ndata: {json.dumps({'status': job['status']})}\n\n"
                break
            idle = 0.0 if events else idle + 0.4
            if idle >= 15:
                idle = 0.0
                yield ": keep-alive\n\n"
            await asyncio.sleep(0.4)

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@router.get("/jobs/{job_id}/events/history")
async def event_history(request: Request, job_id: str) -> list[dict]:
    await _require_job(request, job_id)
    return await _store(request).get_events(job_id, after_id=0, limit=5000)


@router.patch("/jobs/{job_id}/spec")
async def patch_spec(request: Request, job_id: str, body: dict[str, Any] = Body(...)) -> JSONResponse:
    """Operator edit: validate, then rebuild deterministically right away (no LLM call)."""
    job = await _require_job(request, job_id)
    pipeline = _pipeline(request)
    if pipeline.is_running(job_id):
        raise HTTPException(409, "AI-pipeline loopt nog voor deze job")
    try:
        spec = PartSpec.model_validate(body)
    except ValidationError as exc:
        raise HTTPException(422, detail=json.loads(exc.json(include_url=False))) from exc
    if not job["current_version"] and not pipeline.history(job_id):
        raise HTTPException(409, "job has no PartSpec yet")
    out = await pipeline.patch_spec(job_id, spec)
    job = await _store(request).get_job(job_id)
    detail = _detail(pipeline, job, out["version"])  # type: ignore[arg-type]
    detail["duration_s"] = out["duration_s"]
    detail["timings"] = out["result"].get("timings")
    return JSONResponse(detail)


@router.post("/jobs/{job_id}/verify")
async def verify(request: Request, job_id: str) -> dict:
    """"Mark verified" (v1.5 §2.6): the operator confirms the current PartSpec; logs the diff to the AI reading."""
    from app.services import telemetry

    job = await _require_job(request, job_id)
    pipeline = _pipeline(request)
    v = job["current_version"]
    if not v:
        raise HTTPException(409, "job has no PartSpec yet")
    spec = pipeline.load_spec(job_id, v).model_dump(mode="json")
    diff = await telemetry.record(_store(request), job_id, v, "verified", spec, spec)
    import time

    await _store(request).update_job(job_id, verified_at=time.time())
    return {"job_id": job_id, "version": v, "verified": True, "diff": diff}


class ReextractRequest(BaseModel):
    hint: str | None = None


@router.post("/jobs/{job_id}/reextract", status_code=202)
async def reextract(request: Request, job_id: str, body: ReextractRequest | None = None) -> dict:
    await _require_job(request, job_id)
    pipeline = _pipeline(request)
    if pipeline.is_running(job_id):
        raise HTTPException(409, "job is already running")
    if not (pipeline.job_dir(job_id) / "extraction" / "digest.json").exists():
        raise HTTPException(409, "PDF has not been parsed yet")
    hint = (body.hint or "").strip() if body else ""
    await _store(request).update_job(job_id, status="extracting_features", stage="extracting_features",
                                     progress=0.2, message="Opnieuw extraheren", error=None)
    pipeline.start_reextract(job_id, hint or None)
    return {"job_id": job_id, "status": "extracting_features", "hint": hint or None}


@router.get("/jobs/{job_id}/spec")
async def get_spec(request: Request, job_id: str, version: int | None = None) -> dict:
    job = await _require_job(request, job_id)
    pipeline = _pipeline(request)
    v = version or job["current_version"] or max((h["version"] for h in pipeline.history(job_id)), default=0)
    p = pipeline.spec_path(job_id, v)
    if not v or not p.exists():
        raise HTTPException(404, "spec version not found")
    return {"version": v, "part_spec": json.loads(p.read_text(encoding="utf-8"))}


@router.get("/jobs/{job_id}/artifacts/{filename:path}")
async def get_artifact(request: Request, job_id: str, filename: str, version: int | None = None,
                       download: bool = False) -> FileResponse:
    """model.step, preview.glb, original.pdf, ortho_check.png, dfm_report.json (current or ?version=n),
    plus ortho_<view>.png and tiles/<name>.png."""
    job = await _require_job(request, job_id)
    jd = _pipeline(request).job_dir(job_id).resolve()
    if version is not None and filename in VERSIONED:
        p = (jd / "versions" / f"v{version}" / filename).resolve()
    elif filename in VERSIONED and filename.startswith("ortho_") and filename != "ortho_check.png":
        p = (jd / "versions" / f"v{job['current_version']}" / filename).resolve()
    else:
        p = (jd / filename).resolve()
    if jd not in p.parents or not p.is_file():
        raise HTTPException(404, "artifact not found")
    kwargs: dict[str, Any] = {"media_type": MEDIA.get(p.suffix.lower(), "application/octet-stream")}
    stem, v = Path(job["filename"]).stem, version or job["current_version"]
    if filename == "model.step":
        kwargs["filename"] = f"{stem}_v{v}.step"
    elif filename == "setup_sheet.pdf":
        kwargs["filename"] = f"{stem}_v{v}_instelblad.pdf"
        if not download:
            kwargs["content_disposition_type"] = "inline"
    elif filename in ("drawing.pdf", "drawing.svg", "drawing.dxf"):
        kwargs["filename"] = f"{stem}_v{v}_ISO{p.suffix}"
        if not download and p.suffix != ".dxf":  # PDF / SVG open in the browser, with a sensible save-as name
            kwargs["content_disposition_type"] = "inline"
    elif download:
        kwargs["filename"] = p.name
    return FileResponse(p, headers={"Cache-Control": "no-cache"}, **kwargs)
