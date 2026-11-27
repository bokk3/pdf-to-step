"""Part revision lineage + 3D revision diff (SPEC_V2_0_MULTISIDE_FIXTURING.md §3.3, §6)."""

from __future__ import annotations

import json

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse

from app.api.jobs import _pipeline, _require_job, _store
from app.schemas.revision import RevisionDiffRequest

router = APIRouter(prefix="/api", tags=["revisions"])
DIFF_FILES = {"revision_diff.glb": "model/gltf-binary", "revision_diff_report.pdf": "application/pdf",
              "report.json": "application/json"}


def _diff_dir(request: Request, diff_id: str):
    return _pipeline(request).s.data_dir / "revisions" / diff_id


@router.get("/parts/{part_number}/revisions")
async def part_revisions(request: Request, part_number: str) -> dict:
    """All jobs of one part number (normalized), oldest first, with their revision letters / numbers."""
    from app.services.revision_diff import norm_part_number, revision_key

    key = norm_part_number(part_number)
    pipeline = _pipeline(request)
    jobs = await _store(request).jobs_by_part(key)
    rows = [{"job_id": j["id"], "filename": j["filename"], "revision": j.get("revision"), "status": j["status"],
             "current_version": j["current_version"], "created_at": j["created_at"],
             "running": pipeline.is_running(j["id"])} for j in jobs]
    by_rev = sorted(rows, key=lambda r: (revision_key(r["revision"]), r["created_at"]))
    return {"part_number": key, "revisions": rows, "by_revision": [r["job_id"] for r in by_rev]}


@router.post("/revisions/diff")
async def revision_diff(request: Request, body: RevisionDiffRequest) -> dict:
    from app.services.revision_diff import cad_revision_diff, diff_id

    pipeline = _pipeline(request)
    jo, jn = await _require_job(request, body.job_old_id), await _require_job(request, body.job_new_id)
    vo, vn = body.version_old or jo["current_version"], body.version_new or jn["current_version"]
    for j, v in ((jo, vo), (jn, vn)):
        if not v or not (pipeline.job_dir(j["id"]) / "versions" / f"v{v}" / "model.step").exists():
            raise HTTPException(409, f"job {j['id']} v{v} heeft geen STEP-model")
    did = diff_id(jo["id"], vo, jn["id"], vn)
    out = _diff_dir(request, did)
    cached = out / "report.json"
    if cached.exists():
        report = json.loads(cached.read_text(encoding="utf-8"))
    else:
        meta = {"diff_id": did, "job_old_id": jo["id"], "job_new_id": jn["id"], "version_old": vo, "version_new": vn,
                "part_number": jn.get("part_number") or jo.get("part_number"), "revision_old": jo.get("revision"),
                "revision_new": jn.get("revision")}
        try:
            report = await pipeline._run_pool(cad_revision_diff, str(pipeline.job_dir(jo["id"])), vo,
                                              str(pipeline.job_dir(jn["id"])), vn, str(out), json.dumps(meta))
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(500, f"revisievergelijking mislukt: {type(exc).__name__}: {exc}") from exc
    report["urls"] = {name: f"/api/revisions/diff/{did}/artifacts/{name}" for name in DIFF_FILES}
    return report


@router.get("/revisions/diff/{diff_id}/artifacts/{filename}")
async def diff_artifact(request: Request, diff_id: str, filename: str, download: bool = False):
    if filename not in DIFF_FILES or not diff_id.isalnum():
        raise HTTPException(404, "unknown artifact")
    p = _diff_dir(request, diff_id) / filename
    if not p.exists():
        raise HTTPException(404, "not generated")
    kwargs = {"filename": f"revision_diff_{diff_id}{p.suffix}"} if download else {}
    if filename.endswith(".pdf") and not download:
        kwargs = {"filename": p.name, "content_disposition_type": "inline"}
    return FileResponse(p, media_type=DIFF_FILES[filename], headers={"Cache-Control": "no-cache"}, **kwargs)
