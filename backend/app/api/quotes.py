"""Deterministic quoting + shop rates (SPEC_V2_0_MULTISIDE_FIXTURING.md §3.4, §6)."""

from __future__ import annotations

import re

from fastapi import APIRouter, Body, HTTPException, Request
from fastapi.responses import FileResponse, Response
from pydantic import ValidationError

from app.api.jobs import _pipeline, _require_job
from app.schemas.quote import QuoteBreakdown, QuoteInputs, QuoteRequest, ShopRates

router = APIRouter(prefix="/api", tags=["quotes"])


@router.get("/config/shop-rates")
async def get_rates(request: Request) -> dict:
    from app.services.quoting_engine import load_rates

    return load_rates(_pipeline(request).s).model_dump(mode="json")


@router.put("/config/shop-rates")
async def put_rates(request: Request, body: dict = Body(...)) -> dict:
    from app.services.quoting_engine import save_rates

    try:
        rates = ShopRates.model_validate(body)
    except ValidationError as exc:
        raise HTTPException(422, detail=exc.errors(include_url=False, include_context=False)) from exc
    save_rates(_pipeline(request).s, rates)
    return rates.model_dump(mode="json")


async def _quote(request: Request, job_id: str, req: QuoteRequest, version: int | None = None) -> QuoteBreakdown:
    from app.services.quoting_engine import cad_quote_inputs, compute_quote, load_rates

    job = await _require_job(request, job_id)
    pipeline = _pipeline(request)
    v = version or job["current_version"]
    if not v or not (pipeline.job_dir(job_id) / "versions" / f"v{v}" / "model.step").exists():
        raise HTTPException(409, "deze versie heeft nog geen STEP-model")
    data = await pipeline._run_pool(cad_quote_inputs, str(pipeline.job_dir(job_id)), v,
                                    pipeline.s.model_dump(mode="json"))
    q = compute_quote(QuoteInputs.model_validate(data), load_rates(pipeline.s), req, job_id)
    vdir = pipeline.job_dir(job_id) / "versions" / f"v{v}"
    (vdir / "quote_request.json").write_text(req.model_dump_json(), encoding="utf-8")
    return q


@router.post("/jobs/{job_id}/quote")
async def make_quote(request: Request, job_id: str, body: QuoteRequest | None = None, version: int | None = None) -> dict:
    q = await _quote(request, job_id, body or QuoteRequest(), version)
    out = q.model_dump(mode="json")
    out["exports"] = {fmt: f"/api/jobs/{job_id}/quote/export.{fmt}?version={q.version}" for fmt in ("pdf", "csv")}
    return out


@router.get("/jobs/{job_id}/quote/export.{fmt}")
async def export_quote(request: Request, job_id: str, fmt: str, version: int | None = None):
    """quote_summary.pdf / .csv with the last quote request of that revision (defaults when none)."""
    from app.services.quoting_engine import quote_csv, quote_pdf

    if fmt not in ("pdf", "csv"):
        raise HTTPException(404, "fmt must be pdf or csv")
    job = await _require_job(request, job_id)
    pipeline = _pipeline(request)
    v = version or job["current_version"]
    rp = pipeline.job_dir(job_id) / "versions" / f"v{v}" / "quote_request.json"
    req = QuoteRequest.model_validate_json(rp.read_text(encoding="utf-8")) if rp.exists() else QuoteRequest()
    q = await _quote(request, job_id, req, v)
    stem = re.sub(r"[^A-Za-z0-9._-]+", "_", job.get("part_number") or job["filename"].rsplit(".", 1)[0]) or job_id
    if fmt == "csv":
        return Response(quote_csv(q), media_type="text/csv; charset=utf-8",
                        headers={"Content-Disposition": f'attachment; filename="{stem}_v{v}_quote_summary.csv"'})
    path = pipeline.job_dir(job_id) / "versions" / f"v{v}" / "quote_summary.pdf"
    spec = pipeline.load_spec(job_id, v)
    quote_pdf(q, spec.part_name or stem, path)
    return FileResponse(path, media_type="application/pdf", filename=f"{stem}_v{v}_quote_summary.pdf",
                        content_disposition_type="inline", headers={"Cache-Control": "no-cache"})
