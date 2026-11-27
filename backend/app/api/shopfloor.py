"""Read-only shopfloor payload + accuracy metrics (SPEC_V1_5_CAM_SHOPFLOOR.md §2.4, §2.6, §3).

Pure reads of files the pipeline already wrote; no LLM, no CAD kernel. Feature anchors are global CAD coordinates
(datum frame, mm) so the phone viewer can map a tapped point on the model to the nearest feature.
"""

from __future__ import annotations

import json
import math
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
        head = f.thread or f"Ø{_fmt(f.diameter)}"
        if f.thread and getattr(f, "thread_depth", None):
            depth = f"draad {_fmt(f.thread_depth)}" + ("" if d.get("through_all") else f", boring {_fmt(d.get('depth'))}" if d.get("depth") else "")
        parts = [head, f.tolerance or "", depth]
        if f.counterbore:
            parts.append(f"verzinking Ø{_fmt(f.counterbore.diameter)} x {_fmt(f.counterbore.depth)}")
        if f.countersink:
            parts.append(f"kegel Ø{_fmt(f.countersink.diameter)}")
        n = len(f.positions())
        return (f"{n}x " if n > 1 else "") + " ".join(p for p in parts if p)
    if "start_x" in d:
        d["length"] = round(math.hypot(d["end_x"] - d["start_x"], d["end_y"] - d["start_y"]) + d["width"], 3)
    dims = [d.get(k) for k in ("length_x", "width_y", "diameter", "length", "width") if d.get(k)]
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
        if "start_x" in d:  # slot: both ends and the middle
            sx, sy, ex, ey = d["start_x"], d["start_y"], d["end_x"], d["end_y"]
            pts = [(sx, sy), ((sx + ex) / 2, (sy + ey) / 2), (ex, ey)]
        elif "x_min" in d:  # step cut: centre of the box
            pts = [((d["x_min"] + d["x_max"]) / 2, (d["y_min"] + d["y_max"]) / 2)]
        else:
            x, y = d.get("center_x"), d.get("center_y")
            pts = [(x, y)] if x is not None and y is not None else []
    out = []
    for x, y in pts:
        p = fr.origin + fr.u_dir * x + fr.v_dir * y
        out.append([round(float(p.X), 3), round(float(p.Y), 3), round(float(p.Z), 3)])
    return out


def feature_anchor_map(spec_json: str) -> dict[str, list[list[float]]]:
    """Global anchor points per feature id. Runs in a CAD worker: face frames need the OCCT kernel, which the API
    process never imports (a cold import there costs ~10 s on the first QR scan)."""
    from app.services.cad_compiler import face_frames

    spec = PartSpec.model_validate_json(spec_json)
    frames = face_frames(spec)
    return {f.id: feature_anchors(f, frames) for f in spec.features}


async def _anchors(pipeline, spec: PartSpec, vdir) -> dict[str, list[list[float]]]:
    cache = vdir / "shopfloor_anchors.json"
    if cache.exists():
        try:
            return json.loads(cache.read_text(encoding="utf-8"))
        except ValueError:
            pass
    out = await pipeline._run_pool(feature_anchor_map, spec.model_dump_json())
    if vdir.exists():
        cache.write_text(json.dumps(out), encoding="utf-8")
    return out


async def shopfloor_payload(pipeline, job: dict[str, Any], version: int | None = None) -> dict[str, Any]:
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
    anchors = await _anchors(pipeline, spec, vdir)
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
        "anchors": anchors.get(f.id, []),
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
    return await shopfloor_payload(_pipeline(request), job, version)


@router.get("/jobs/{job_id}/setup-plan")
async def get_setup_plan(request: Request, job_id: str, version: int | None = None) -> dict:
    """v2.0 §3.1 SetupPlan: 3-axis vs 5-axis 3+2 clampings (pure geometry; from the DFM report when present)."""
    from app.schemas.part_spec import DFMReport
    from app.services.setup_planner import deck_off_from_stock, plan_setups

    job = await _require_job(request, job_id)
    pipeline = _pipeline(request)
    v = version or job["current_version"]
    if not v:
        raise HTTPException(409, "job heeft nog geen PartSpec")
    rp = pipeline.job_dir(job_id) / "versions" / f"v{v}" / "dfm_report.json"
    rep = DFMReport.model_validate_json(rp.read_text(encoding="utf-8")) if rp.exists() else None
    if rep and rep.setup_plan:
        return {"version": v, **rep.setup_plan.model_dump(mode="json")}
    spec = pipeline.load_spec(job_id, v)
    plan = plan_setups(spec, deck_off_from_stock(spec, rep.stock_recommendation if rep else None))
    return {"version": v, **plan.model_dump(mode="json")}


@router.get("/metrics/accuracy")
async def get_accuracy(request: Request) -> dict:
    from app.services import telemetry

    return await telemetry.accuracy(_store(request))
