"""Read-only shopfloor payload + accuracy metrics (SPEC_V1_5_CAM_SHOPFLOOR.md §2.4, §2.6, §3).

Pure reads of files the pipeline already wrote; no LLM, no CAD kernel. Feature anchors are global CAD coordinates
(datum frame, mm) so the phone viewer can map a tapped point on the model to the nearest feature.
"""

from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, HTTPException, Request

from app.api.jobs import _artifact_urls, _pipeline, _require_job, _store
from app.schemas.part_spec import Hole, PartSpec

router = APIRouter(prefix="/api", tags=["shopfloor"])


def _fmt(v: float | None) -> str:
    return "" if v is None else f"{v:g}"


def feature_label(f) -> str:
    """Short operator label: 'M8 x 16', 'Ø10 H7 doorgaand', 'kamer 40 x 30 x 5'."""
    d = f.model_dump()
    depth = "doorgaand" if d.get("through_all") else (f"diep {_fmt(d.get('depth'))}" if d.get("depth") else "")
    if isinstance(f, Hole):
        head = (f.thread + (f" x {_fmt(f.thread_depth)}" if getattr(f, "thread_depth", None) else "")) if f.thread \
            else f"Ø{_fmt(f.diameter)}"
        parts = [head, f.tolerance or "", depth]
        if f.counterbore:
            parts.append(f"verzinking Ø{_fmt(f.counterbore.diameter)} x {_fmt(f.counterbore.depth)}")
        if f.countersink:
            parts.append(f"kegel Ø{_fmt(f.countersink.diameter)}")
        n = len(f.positions())
        return (f"{n}x " if n > 1 else "") + " ".join(p for p in parts if p)
    dims = [d.get(k) for k in ("length_x", "width_y", "diameter", "width", "length") if d.get(k)]
    size = " x ".join(_fmt(v) for v in dims[:2])
    return " ".join(p for p in (f.type.replace("_", " "), size, depth, d.get("tolerance") or "") if p)


def feature_category(f, spec: PartSpec, palette) -> str:
    from app.services.step_xcaf_exporter import is_tight_fit

    fine = {sf.feature_id for sf in spec.drawing_metadata.surface_finishes
            if sf.ra_um <= palette.fine_finish_max_ra_um + 1e-9}
    if isinstance(f, Hole) and f.thread:
        return "thread"
    if is_tight_fit(getattr(f, "tolerance", None), palette):
        return "tight_fit"
    if f.id in fine:
        return "fine_finish"
    if isinstance(f, Hole) and (f.counterbore or f.countersink):
        return "seat"
    return "body"


def feature_anchors(f, frames) -> list[list[float]]:
    """Global points of a feature (one per pattern instance) on its entry face."""
    fr = frames.get(getattr(f, "face", None))
    if fr is None:
        return []
    try:
        pts = f.positions()
    except Exception:  # noqa: BLE001 - features without positions (step cuts, chamfers)
        d = f.model_dump()
        x, y = d.get("center_x"), d.get("center_y")
        pts = [(x, y)] if x is not None and y is not None else []
    out = []
    for x, y in pts:
        p = fr.origin + fr.u_dir * x + fr.v_dir * y
        out.append([round(float(p.X), 3), round(float(p.Y), 3), round(float(p.Z), 3)])
    return out


def shopfloor_payload(pipeline, job: dict[str, Any], version: int | None = None) -> dict[str, Any]:
    from app.services.cad_compiler import face_frames
    from app.services.step_xcaf_exporter import load_palette

    job_id = job["id"]
    v = version or job["current_version"]
    if not v:
        raise HTTPException(409, "job heeft nog geen PartSpec")
    spec = pipeline.load_spec(job_id, v)
    vdir = pipeline.job_dir(job_id) / "versions" / f"v{v}"
    rp = vdir / "dfm_report.json"
    report = json.loads(rp.read_text(encoding="utf-8")) if rp.exists() else {}
    cp = vdir / "cam_colors.json"
    cam = json.loads(cp.read_text(encoding="utf-8")) if cp.exists() else {}
    palette = load_palette(pipeline.s)
    frames = face_frames(spec)
    tools_by_feature: dict[str, list[dict]] = {}
    for a in report.get("recommended_tools") or []:
        tools_by_feature.setdefault(a["feature_id"], []).append(
            {k: a.get(k) for k in ("operation", "tool_id", "tool_name", "diameter_mm")})
    checks_by_feature: dict[str, list[dict]] = {}
    for c in report.get("tool_checks") or []:
        if c.get("status") != "ok":
            checks_by_feature.setdefault(c["feature_id"], []).append(
                {k: c.get(k) for k in ("status", "operation", "message", "suggested_fix")})
    features = [{
        "id": f.id,
        "type": f.type,
        "face": getattr(f, "face", None),
        "label": feature_label(f),
        "category": feature_category(f, spec, palette),
        "tolerance": getattr(f, "tolerance", None),
        "anchors": feature_anchors(f, frames),
        "tools": tools_by_feature.get(f.id, []),
        "issues": checks_by_feature.get(f.id, []),
    } for f in spec.features]
    finishes = {sf.feature_id: sf.ra_um for sf in spec.drawing_metadata.surface_finishes}
    for row in features:
        row["ra_um"] = finishes.get(row["id"])
    (x0, y0, z0), (x1, y1, z1) = spec.stock_bbox()
    return {
        "job_id": job_id,
        "version": v,
        "filename": job["filename"],
        "status": job["status"],
        "verified_at": job.get("verified_at"),
        "part_name": spec.part_name,
        "material": spec.material,
        "general_tolerance": spec.general_tolerance,
        "datum_origin": spec.datum_origin,
        "bbox": {"min": [x0, y0, z0], "max": [x1, y1, z1]},
        "stock_recommendation": report.get("stock_recommendation"),
        "palette": {k: {"name": c.name, "hex": c.hex, "meaning": c.meaning} for k, c in palette.colors.items()},
        "color_counts": cam.get("counts") or {},
        "features": features,
        "artifacts": _artifact_urls(pipeline, job_id, v),
    }


@router.get("/jobs/{job_id}/shopfloor")
async def get_shopfloor(request: Request, job_id: str, version: int | None = None) -> dict:
    job = await _require_job(request, job_id)
    return shopfloor_payload(_pipeline(request), job, version)


@router.get("/metrics/accuracy")
async def get_accuracy(request: Request) -> dict:
    from app.services import telemetry

    return await telemetry.accuracy(_store(request))
