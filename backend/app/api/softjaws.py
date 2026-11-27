"""Softjaw generation + vise library (SPEC_V2_0_MULTISIDE_FIXTURING.md §3.2, §6)."""

from __future__ import annotations

from fastapi import APIRouter, Body, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import ValidationError

from app.api.jobs import _pipeline, _require_job
from app.schemas.softjaw import SoftjawRequest, ViseLibrary

router = APIRouter(prefix="/api", tags=["softjaws"])
SOFTJAW_FILES = {"softjaws.step": "application/step", "softjaws_assembly.glb": "model/gltf-binary",
                 "softjaw_report.json": "application/json"}


@router.get("/config/vises")
async def get_vises(request: Request) -> dict:
    from app.services.softjaw_generator import load_vises

    return load_vises(_pipeline(request).s).model_dump(mode="json")


@router.put("/config/vises")
async def put_vises(request: Request, body: dict = Body(...)) -> dict:
    from app.services.softjaw_generator import save_vises

    try:
        lib = ViseLibrary.model_validate(body)
    except ValidationError as exc:
        raise HTTPException(422, detail=exc.errors(include_url=False, include_context=False)) from exc
    save_vises(_pipeline(request).s, lib)
    return lib.model_dump(mode="json")


@router.post("/jobs/{job_id}/softjaws")
async def make_softjaws(request: Request, job_id: str, body: SoftjawRequest | None = None) -> dict:
    """Op 2 softjaws for the current (or given) built revision. 422 with the report when a safety check blocks."""
    from app.services.softjaw_generator import cad_softjaws, load_vises

    job = await _require_job(request, job_id)
    pipeline = _pipeline(request)
    req = body or SoftjawRequest()
    v = req.version or job["current_version"]
    if not v or not (pipeline.job_dir(job_id) / "versions" / f"v{v}" / "model.step").exists():
        raise HTTPException(409, "deze versie heeft nog geen STEP-model")
    try:
        load_vises(pipeline.s).get(req.vise_id)
    except KeyError as exc:
        raise HTTPException(422, f"onbekende bankschroef '{req.vise_id}' (zie /api/config/vises)") from exc
    try:
        report = await pipeline._run_pool(cad_softjaws, str(pipeline.job_dir(job_id)), v,
                                          req.model_copy(update={"version": v}).model_dump_json(),
                                          pipeline.s.model_dump(mode="json"))
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(500, f"zachte bekken mislukt: {type(exc).__name__}: {exc}") from exc
    base = f"/api/jobs/{job_id}/artifacts/softjaws"
    report["urls"] = {name: f"{base}/{name}" for name in report.get("artifacts", {})}
    if not report["passed"]:
        raise HTTPException(422, detail=report)
    return report


@router.get("/jobs/{job_id}/artifacts/softjaws/{filename}")
async def softjaw_artifact(request: Request, job_id: str, filename: str, download: bool = False):
    await _require_job(request, job_id)
    if filename not in SOFTJAW_FILES:
        raise HTTPException(404, "unknown softjaw artifact")
    p = _pipeline(request).job_dir(job_id) / "softjaws" / filename
    if not p.exists():
        raise HTTPException(404, "not generated yet")
    kwargs = {"filename": f"{job_id}_{filename}"} if download or filename.endswith(".step") else {}
    return FileResponse(p, media_type=SOFTJAW_FILES[filename], headers={"Cache-Control": "no-cache"}, **kwargs)
