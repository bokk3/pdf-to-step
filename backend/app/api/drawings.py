"""Intake fast paths + sketch template (DRAWING_SPEC.md §6): sketch photo, layered DXF, direct STEP."""

from __future__ import annotations

import asyncio
from pathlib import Path

from fastapi import APIRouter, File, HTTPException, Request, UploadFile
from fastapi.responses import Response

from app.api.jobs import MAX_UPLOAD_BYTES, _pipeline, _store

router = APIRouter(prefix="/api", tags=["intake"])
IMAGE_TYPES = {b"\xff\xd8\xff": ".jpg", b"\x89PNG": ".png", b"RIFF": ".webp"}


@router.get("/templates/sketch-a4.pdf")
async def sketch_template() -> Response:
    from app.services.sketch_template import template_pdf

    data = await asyncio.to_thread(template_pdf)
    return Response(data, media_type="application/pdf",
                    headers={"Content-Disposition": 'inline; filename="schetssjabloon_A4.pdf"',
                             "Cache-Control": "public, max-age=3600"})


async def _read(file: UploadFile) -> bytes:
    data = await file.read(MAX_UPLOAD_BYTES + 1)
    if len(data) > MAX_UPLOAD_BYTES:
        raise HTTPException(413, "bestand groter dan 50 MB")
    if not data:
        raise HTTPException(400, "leeg bestand")
    return data


async def _new_job(request: Request, name: str, files: dict[str, bytes]) -> dict:
    store, pipeline = _store(request), _pipeline(request)
    job = await store.create_job(name)
    jd = pipeline.job_dir(job["id"])
    jd.mkdir(parents=True, exist_ok=True)
    for fname, data in files.items():
        (jd / fname).write_bytes(data)
    return job


@router.post("/jobs/from-dxf", status_code=202)
async def from_dxf(request: Request, file: UploadFile = File(...)) -> dict:
    """Layered DXF -> PartSpec -> STEP + drawing, zero AI calls."""
    from app.services.dxf_parser import DxfError, parse_dxf, render_dxf_pdf

    name = Path(file.filename or "part.dxf").name
    data = await _read(file)
    try:
        res = await asyncio.to_thread(parse_dxf, data, Path(name).stem)
    except DxfError as exc:
        raise HTTPException(422, str(exc)) from exc
    job = await _new_job(request, name, {"original.dxf": data})
    jd = _pipeline(request).job_dir(job["id"])
    try:
        await asyncio.to_thread(render_dxf_pdf, data, jd / "original.pdf")
    except Exception:  # noqa: BLE001 - the preview is optional
        pass
    layers = ", ".join(f"{k} ({v})" for k, v in res.layers.items())
    await _store(request).add_event(job["id"], "queued", f"{name}: lagen {layers}", progress=0.02,
                                    data={"warnings": res.warnings})
    _pipeline(request).start_from_spec(job["id"], res.spec, "dxf", reason="; ".join(res.warnings)[:2000])
    return {"job_id": job["id"], "status": "queued", "filename": name, "warnings": res.warnings}


@router.post("/jobs/from-step", status_code=202)
async def from_step(request: Request, file: UploadFile = File(...)) -> dict:
    """Existing 3D STEP: no vision, DFM + drawing on the uploaded B-Rep."""
    name = Path(file.filename or "part.step").name
    data = await _read(file)
    if b"ISO-10303-21" not in data[:200]:
        raise HTTPException(415, "geen STEP-bestand (ISO-10303-21)")
    job = await _new_job(request, name, {"original.step": data})
    await _store(request).add_event(job["id"], "queued", f"{name} ontvangen ({len(data) / 1024:.0f} KB)", progress=0.01)
    _pipeline(request).start_step_job(job["id"], name)
    return {"job_id": job["id"], "status": "queued", "filename": name}


@router.post("/jobs/from-sketch", status_code=202)
async def from_sketch(request: Request, file: UploadFile = File(...)) -> dict:
    """Photo / scan of the A4 sketch template: ArUco rectification + sketch extraction, confidence <= 0.75."""
    name = Path(file.filename or "schets.jpg").name
    data = await _read(file)
    ext = next((e for magic, e in IMAGE_TYPES.items() if data.startswith(magic)), None)
    if ext is None:
        raise HTTPException(415, "afbeelding verwacht (JPEG, PNG of WebP; zet HEIC op de telefoon om naar JPEG)")
    from app.services.sketch_ingestor import SketchError, decode_image, detect_markers

    try:  # fail fast (before a job exists) when the markers are not visible
        found = await asyncio.to_thread(lambda: detect_markers(decode_image(data)))
    except SketchError as exc:
        raise HTTPException(422, str(exc)) from exc
    missing = sorted({0, 1, 2, 3} - set(found))
    if missing:
        raise HTTPException(422, f"ArUco-merker(s) {missing} niet gevonden: fotografeer het hele schetssjabloon "
                                 "met de 4 hoekmerkers scherp in beeld.")
    upload = f"sketch_upload{ext}"
    job = await _new_job(request, name, {upload: data})
    await _store(request).add_event(job["id"], "queued", f"{name} ontvangen ({len(data) / 1024:.0f} KB)", progress=0.01)
    _pipeline(request).start_sketch_job(job["id"], name, upload)
    return {"job_id": job["id"], "status": "queued", "filename": name}

