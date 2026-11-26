"""Healthcheck + OCCT and DeepSeek connectivity."""

from __future__ import annotations

import asyncio
import time

from fastapi import APIRouter, Request

from app.services.pipeline import Pipeline

router = APIRouter(prefix="/api", tags=["health"])


def _occt_check() -> dict:
    t0 = time.monotonic()
    try:
        from importlib.metadata import PackageNotFoundError, version

        import build123d
        from build123d import Box, Cylinder

        part = Box(10, 10, 10) - Cylinder(2, 20)
        ok = part.volume > 0 and part.is_valid
        ocp = None
        for dist in ("cadquery-ocp-novtk", "cadquery-ocp"):
            try:
                ocp = version(dist)
                break
            except PackageNotFoundError:
                continue
        return {"ok": bool(ok), "build123d": build123d.__version__, "ocp": ocp,
                "ms": round((time.monotonic() - t0) * 1000)}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


@router.get("/health")
async def health(request: Request, deep: bool = False) -> dict:
    s = request.app.state.settings
    pipeline: Pipeline = request.app.state.pipeline
    out = {
        "status": "ok",
        "bind": f"{s.host}:{s.port}",
        "deepseek": {
            "configured": bool(s.deepseek_api_key),
            "mock": not pipeline.llm_is_real,
            "vision_model": s.deepseek_vision_model,
            "code_model": s.deepseek_code_model,
            "thinking": s.deepseek_thinking,
            "files_api": s.deepseek_use_files_api,
        },
        "critic_enabled": s.critic_enabled,
        "max_heal_retries": s.max_heal_retries,
        "step_schema": s.step_schema,
        "dfm": {"bbox_tolerance_mm": s.dfm_bbox_tolerance_mm, "min_corner_radius_mm": s.dfm_min_corner_radius_mm,
                "max_hole_depth_ratio": s.dfm_max_hole_depth_ratio,
                "max_pocket_depth_per_radius": s.dfm_max_pocket_depth_per_radius, "min_wall_mm": s.dfm_min_wall_mm},
    }
    if deep:
        out["occt"] = await asyncio.to_thread(_occt_check)
        if pipeline.llm_is_real:
            out["deepseek"].update(await pipeline.llm.ping())
        if not out["occt"].get("ok"):
            out["status"] = "degraded"
    return out
