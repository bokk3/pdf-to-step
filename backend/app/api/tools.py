"""Workshop configuration API (SPEC_V1_5_CAM_SHOPFLOOR.md §5): tool library, stock catalog, CAM colours."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from fastapi import APIRouter, Body, File, HTTPException, Query, Request, UploadFile
from pydantic import ValidationError

from app.schemas.tooling import StockCatalog, ToolLibrary

router = APIRouter(prefix="/api/config", tags=["config"])
MAX_LIBRARY_BYTES = 20 * 1024 * 1024


def _settings(request: Request):
    return request.app.state.pipeline.s


def _invalid(exc: ValidationError) -> HTTPException:
    return HTTPException(422, detail=json.loads(exc.json(include_url=False)))


@router.get("/tools")
async def get_tools(request: Request) -> dict:
    from app.services.tool_library import load_tools

    return load_tools(_settings(request)).model_dump(mode="json")


@router.put("/tools")
async def put_tools(request: Request, body: dict = Body(...)) -> dict:
    from app.services.tool_library import save_tools

    try:
        lib = ToolLibrary.model_validate(body)
    except ValidationError as exc:
        raise _invalid(exc) from exc
    save_tools(_settings(request), lib)
    return lib.model_dump(mode="json")


@router.post("/tools/import-fusion")
async def import_fusion(request: Request, file: UploadFile = File(...),
                        replace: bool = Query(False, description="replace the library instead of merging")) -> dict:
    """Fusion 360 tool library (.json or zipped .tools), CSV or tools.json; merged by tool id (T-number)."""
    from app.services.tool_library import import_tools, load_tools, merge_tools, save_tools

    data = await file.read(MAX_LIBRARY_BYTES + 1)
    if len(data) > MAX_LIBRARY_BYTES:
        raise HTTPException(413, "bibliotheek groter dan 20 MB")
    try:
        res = await asyncio.to_thread(import_tools, data, Path(file.filename or "").name)
    except (ValueError, json.JSONDecodeError) as exc:
        raise HTTPException(422, f"bibliotheek niet gelezen: {exc}") from exc
    if not res.tools:
        raise HTTPException(422, "geen bruikbare gereedschappen gevonden" + (f": {res.skipped[:5]}" if res.skipped else ""))
    s = _settings(request)
    lib, added, updated = merge_tools(load_tools(s), res.tools, replace=replace)
    save_tools(s, lib)
    return {"source": res.source, "added": added, "updated": updated, "skipped": res.skipped,
            "total": len(lib.tools)}


@router.get("/stock")
async def get_stock(request: Request) -> dict:
    from app.services.stock_catalog import load_stock

    return load_stock(_settings(request)).model_dump(mode="json")


@router.put("/stock")
async def put_stock(request: Request, body: dict = Body(...)) -> dict:
    from app.services.stock_catalog import save_stock

    try:
        cat = StockCatalog.model_validate(body)
    except ValidationError as exc:
        raise _invalid(exc) from exc
    save_stock(_settings(request), cat)
    return cat.model_dump(mode="json")
