"""Batch intake (SPEC_V1_5_CAM_SHOPFLOOR.md §2.5): split a multi-page PDF into child jobs and run them with bounded
concurrency (``MAX_CONCURRENT_JOBS``) so OCCT + DeepSeek never flood the workstation."""

from __future__ import annotations

import asyncio
import io
import logging
import re
import zipfile
from pathlib import Path
from typing import Any

from app.services import pdf_splitter as ps

log = logging.getLogger(__name__)
ZIP_FILES = (("model.step", ".step"), ("drawing.pdf", "_ISO.pdf"), ("setup_sheet.pdf", "_instelblad.pdf"))


def batch_dir(pipeline, batch_id: str) -> Path:
    return pipeline.s.data_dir / "batches" / batch_id


def _part_regexes(s) -> list[str] | None:
    raw = (getattr(s, "batch_part_number_regex", "") or "").strip()
    return [r for r in raw.split("||") if r.strip()] or None


async def split_batch(pipeline, batch_id: str) -> list[dict[str, Any]]:
    """Analyse + group the pages, write one original.pdf per part, create the child jobs (status queued)."""
    from app.services.deepseek_client import ExtractionImage, LLMTrace

    s, store = pipeline.s, pipeline.store
    bdir = batch_dir(pipeline, batch_id)
    src = bdir / "original.pdf"
    infos = await asyncio.to_thread(ps.analyse_pages, src, _part_regexes(s), s.batch_sheet_regex or ps.SHEET_REGEX)
    scans = [i.page_index for i in infos if i.source == "none"]
    if scans and pipeline.llm_is_real:
        crops = await asyncio.to_thread(ps.render_title_crops, src, scans, bdir / "title_crops")
        trace = LLMTrace()
        try:
            answers = await pipeline.llm.identify_title_blocks(
                [ExtractionImage(f"page_{i}", p) for i, p in zip(scans, crops)], trace)
            ps.apply_llm_answers(infos, answers)
        except Exception as exc:  # noqa: BLE001 - fall back to page runs
            log.warning("batch %s: title-block fallback failed: %s", batch_id, exc)
    groups = ps.group_pages(infos)
    batch = await store.get_batch(batch_id)
    stem = re.sub(r"\.pdf$", "", batch["filename"], flags=re.IGNORECASE)
    out = []
    used: set[str] = set()
    for idx, g in enumerate(groups):
        name = ps.safe_name(g.part_number)
        while name.lower() in used:
            name += "_b"
        used.add(name.lower())
        job = await store.create_job(f"{name}.pdf")
        await asyncio.to_thread(ps.write_group_pdf, src, g.pages, pipeline.job_dir(job["id"]) / "original.pdf")
        await store.update_job(job["id"], batch_id=batch_id, batch_index=idx, part_number=g.part_number,
                               message=f"wacht op een vrije plek (batch {stem})")
        pages = ", ".join(str(p + 1) for p in g.pages)
        await store.add_event(job["id"], "queued", f"{g.part_number}: pagina {pages} uit {batch['filename']}",
                              progress=0.01, data={"batch_id": batch_id, "pages": [p + 1 for p in g.pages]})
        out.append({**g.as_dict(), "pages": [p + 1 for p in g.pages], "job_id": job["id"], "filename": f"{name}.pdf"})
    await store.update_batch(batch_id, groups=out, status="queued", page_count=len(infos),
                             message=f"{len(infos)} pagina's -> {len(out)} onderdelen")
    return out


async def run_children(pipeline, batch_id: str, groups: list[dict[str, Any]]) -> None:
    store = pipeline.store

    async def one(g: dict[str, Any]) -> None:
        async with pipeline.batch_slots:
            pipeline.batch_active += 1
            pipeline.batch_peak = max(pipeline.batch_peak, pipeline.batch_active)
            try:
                await store.update_batch(batch_id, status="running")
                task = pipeline.start_new_job(g["job_id"], g["filename"])
                await asyncio.shield(task)
            except Exception as exc:  # noqa: BLE001 - one bad drawing never stops the batch
                log.warning("batch %s child %s: %s", batch_id, g["job_id"], exc)
            finally:
                pipeline.batch_active -= 1

    await asyncio.gather(*(one(g) for g in groups))
    jobs = await store.batch_jobs(batch_id)
    ready = sum(1 for j in jobs if j["status"] == "ready")
    await store.update_batch(batch_id, status="done", message=f"{ready}/{len(jobs)} onderdelen gereed")


async def run_batch(pipeline, batch_id: str) -> None:
    try:
        groups = await split_batch(pipeline, batch_id)
        await run_children(pipeline, batch_id, groups)
    except Exception as exc:  # noqa: BLE001
        log.exception("batch %s failed", batch_id)
        await pipeline.store.update_batch(batch_id, status="failed", message=f"{type(exc).__name__}: {exc}"[:1000])


async def batch_status(pipeline, batch_id: str) -> dict[str, Any] | None:
    store = pipeline.store
    b = await store.get_batch(batch_id)
    if not b:
        return None
    groups = b["groups"]
    pages = {g["job_id"]: g["pages"] for g in groups}
    jobs = []
    for j in await store.batch_jobs(batch_id):
        res = j.get("result") or {}
        jobs.append({
            "job_id": j["id"], "part_number": j.get("part_number"), "filename": j["filename"],
            "pages": pages.get(j["id"], []), "status": j["status"], "progress": j["progress"], "message": j["message"],
            "running": pipeline.is_running(j["id"]), "current_version": j["current_version"],
            "dfm": ({k: res.get(k) for k in ("passed", "errors", "warnings", "needs_review")} if res else None),
        })
    done = sum(1 for j in jobs if j["status"] in ("ready", "failed", "awaiting_dimensions") and not j["running"])
    return {
        "batch_id": b["id"], "filename": b["filename"], "status": b["status"], "message": b.get("message"),
        "page_count": b["page_count"], "created_at": b["created_at"], "updated_at": b["updated_at"],
        "job_count": len(jobs), "ready_count": sum(1 for j in jobs if j["status"] == "ready"),
        "progress": round(sum(1.0 if j["status"] in ("ready", "failed") else (j["progress"] or 0) for j in jobs)
                          / len(jobs), 3) if jobs else 0.0,
        "finished_count": done, "jobs": jobs,
    }


def build_zip(pipeline, status: dict[str, Any]) -> bytes:
    """STEP + ISO drawing + setup sheet of every ready child, one folder per part number."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for j in status["jobs"]:
            if j["status"] != "ready":
                continue
            folder = ps.safe_name(j["part_number"] or j["filename"])
            jd = pipeline.job_dir(j["job_id"])
            for name, suffix in ZIP_FILES:
                p = jd / name
                if p.exists():
                    z.write(p, f"{folder}/{folder}_v{j['current_version']}{suffix}")
    return buf.getvalue()
