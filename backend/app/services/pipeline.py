"""Pipeline orchestrator (ARCHITECTURE.md §2).

  parsing_pdf -> extracting_features -> [compiling_cad -> verifying (gate + visual critic) -> retrying]x(1+3)
              -> ready | failed

* Stage 5 routing: deterministic kernel / gate failures and critic discrepancies go back to
  Stage 2 (``correct_spec`` on the vision model, with the drawing); complex_3d sandbox
  failures go back to Stage 3 (code model regenerates the build123d code).
* ``PATCH /spec`` (Stage 6) is a synchronous deterministic rebuild in a warm worker:
  STEP + GLB + DFM in ~1 s, ortho renders follow in the background. No LLM call.
* CAD work runs in a ProcessPoolExecutor so OCCT never blocks the event loop and a native
  crash cannot take the API down.

Job folder (data/jobs/<job_id>/):
  original.pdf, extraction/, tiles/, specs/part_spec_v<n>.json + history.json,
  versions/v<n>/{model.step, preview.glb, ortho_check.png, ortho_<view>.png, dfm_report.json, complex/,
                drawing.pdf/.svg/.dxf + drawing.json (EN ISO detail drawing, DRAWING_SPEC.md)},
  model.step / preview.glb / ortho_check.png / dfm_report.json / drawing.* (= current version),
  iteration_logs.json, llm/ (traces + Files API ids)
"""

from __future__ import annotations

import asyncio
import json
import logging
import shutil
import time
import traceback
from concurrent.futures import Executor
from concurrent.futures.process import BrokenProcessPool
from pathlib import Path
from typing import Any, Callable

from app.config import Settings
from app.db import JobStore
from app.schemas.part_spec import Complex3D, DFMReport, PartSpec, Severity
from app.services.deepseek_client import ExtractionImage, LLMClient, LLMTrace, MockDeepSeekClient

log = logging.getLogger(__name__)

STAGES = ("queued", "parsing_pdf", "extracting_features", "awaiting_dimensions", "compiling_cad", "verifying",
          "retrying", "ready", "failed")
TERMINAL = ("ready", "failed", "awaiting_dimensions")  # awaiting_dimensions: paused until PATCH /spec
DRAWING_FILES = ("drawing.pdf", "drawing.svg", "drawing.dxf", "drawing.json")
CURRENT_FILES = ("model.step", "preview.glb", "ortho_check.png", "dfm_report.json") + DRAWING_FILES


# --------------------------------------------------------------------------- #
# Worker functions (run in the process pool; must be top-level / picklable)
# --------------------------------------------------------------------------- #


def cad_warmup(render: bool = False) -> float:
    """Import OCCT/build123d/trimesh (and matplotlib for the render worker) so the first real job is fast."""
    import tempfile

    t0 = time.monotonic()
    from build123d import Box, Cylinder

    from app.services.verifier import export_glb, render_orthographic

    part = Box(10, 10, 10) - Cylinder(2, 20)
    with tempfile.TemporaryDirectory() as tmp:
        export_glb(part, Path(tmp) / "w.glb")
        if render:
            render_orthographic(part, Path(tmp))
    return time.monotonic() - t0


def render_drawing(shape, spec: PartSpec, vdir: Path, job_id: str, version: int, owner: str = "",
                   overrides: dict | None = None) -> dict[str, Any]:
    """EN ISO detail drawing (PDF/SVG/DXF) of one version; meta goes to drawing.json."""
    from app.services.drawing_engine import get_engine

    from app.services.verifier import estimate_mass_g

    o = {k: v for k, v in (overrides or {}).items() if v not in (None, "", "AUTO")}
    t = time.monotonic()
    try:
        mass = estimate_mass_g(spec, float(shape.volume))
    except Exception:  # noqa: BLE001
        mass = None
    res = get_engine(o.get("engine")).render(
        spec, shape, vdir, sheet_size=o.get("sheet_size"), scale=o.get("scale"), language=o.get("language"),
        drawing_number=spec.drawing_metadata.drawing_number or f"PTS-{job_id[:8].upper()}", owner=owner, mass_g=mass,
        section_view=o.get("section_view"))
    info = {"version": version, "sheet_size": res.sheet_size, "scale": res.scale, "warnings": res.warnings,
            "balloons": res.balloons, "overrides": o, "duration_s": round(time.monotonic() - t, 3)}
    (vdir / "drawing.json").write_text(json.dumps(info, indent=1), encoding="utf-8")
    return info


def cad_drawing(job_dir: str, version: int, spec_json: str, overrides: dict | None, owner: str) -> dict[str, Any]:
    """(Re)generate the drawing of a built version from its STEP (POST /drawing, deferred renders)."""
    from build123d import import_step

    jd = Path(job_dir)
    vdir = jd / "versions" / f"v{version}"
    shape = import_step(str(vdir / "model.step"))
    return render_drawing(shape, PartSpec.model_validate_json(spec_json), vdir, jd.name, version, owner, overrides)


def cad_build(job_dir: str, version: int, spec_json: str, settings_data: dict, render: bool,
              source_step: str | None = None) -> dict[str, Any]:
    """Stage 3 + 4: compile, export STEP (with CAM colours) + GLB, run the gate/DFM, optionally render.

    With ``source_step`` the geometry is the uploaded STEP (moved to the datum) instead of the compiled spec; the spec
    then only documents it (DFM, colours, drawing dimensions)."""
    from app.services.cad_compiler import compile_part, export_step_file
    from app.services.cad_sandbox import load_brep, run_build123d_code
    from app.services.verifier import (
        build_report,
        drawing_callout_texts,
        export_glb,
        render_orthographic,
    )

    timings: dict[str, float] = {}
    t0 = time.monotonic()
    jd = Path(job_dir)
    s = Settings(**settings_data)
    spec = PartSpec.model_validate_json(spec_json)
    vdir = jd / "versions" / f"v{version}"
    vdir.mkdir(parents=True, exist_ok=True)
    complex_logs: dict[str, dict] = {}

    def complex_runner(f: Complex3D):
        res = run_build123d_code(f.code or "", vdir / "complex" / f"{f.id}.brep", timeout_s=s.sandbox_timeout_s)
        complex_logs[f.id] = {"ok": res.ok, "error": res.error, "stderr": res.stderr[-2000:], "duration_s": res.duration_s}
        (vdir / "complex").mkdir(exist_ok=True)
        (vdir / "complex" / f"{f.id}.log.json").write_text(json.dumps(complex_logs[f.id], indent=1), encoding="utf-8")
        if not res.ok or res.brep_path is None:
            raise RuntimeError(f"sandbox: {res.error}")
        return load_brep(res.brep_path)

    try:
        if source_step:
            from app.services.step_ingestor import load_step, to_datum

            shape, statuses, build_warnings = to_datum(load_step(Path(source_step)))[0], [], []
        else:
            result = compile_part(spec, complex_runner=complex_runner)
            shape, statuses, build_warnings = result.shape, result.feature_status, result.warnings
    except Exception as exc:  # noqa: BLE001 - stock/profile errors
        return {"fatal": f"{type(exc).__name__}: {exc}", "traceback": traceback.format_exc(limit=8)}
    timings["compile"] = time.monotonic() - t0

    files: dict[str, str] = {}
    export_errors: list[str] = []
    color_counts: dict[str, int] = {}
    t = time.monotonic()
    coloring = None
    try:
        if s.step_face_colors and s.enable_xcaf_cam_colors:  # v1.5 §2.2: AP214 CAM colours from cam_colors.json
            from app.services.step_xcaf_exporter import classify_faces, export_colored_step, load_palette

            coloring = classify_faces(shape, spec, load_palette(s))
            color_counts = coloring.counts
            export_colored_step(shape, vdir / "model.step", coloring, schema=s.step_schema,
                                name=spec.part_name or "part")
            (vdir / "cam_colors.json").write_text(json.dumps(
                {"counts": color_counts, "palette": coloring.palette.model_dump(mode="json")}, indent=1),
                encoding="utf-8")
        else:
            export_step_file(shape, vdir / "model.step", schema=s.step_schema)
        files["model.step"] = "model.step"
    except Exception as exc:  # noqa: BLE001
        export_errors.append(f"STEP-export mislukt: {exc}")
    timings["step"] = time.monotonic() - t
    t = time.monotonic()
    try:
        export_glb(shape, vdir / "preview.glb", coloring=coloring)
        files["preview.glb"] = "preview.glb"
    except Exception as exc:  # noqa: BLE001
        export_errors.append(f"GLB-export mislukt: {exc}")
    timings["glb"] = time.monotonic() - t

    t = time.monotonic()
    report = build_report(spec, version, shape, statuses, s, color_counts,
                          callout_texts=drawing_callout_texts(jd))
    (vdir / "dfm_report.json").write_text(report.model_dump_json(indent=1), encoding="utf-8")
    files["dfm_report.json"] = "dfm_report.json"
    timings["verify"] = time.monotonic() - t

    if render:
        t = time.monotonic()
        try:
            for name, p in render_orthographic(shape, vdir, title=spec.part_name or "").items():
                files[p.name] = p.name
        except Exception as exc:  # noqa: BLE001
            export_errors.append(f"Ortho-render mislukt: {exc}")
        timings["render"] = time.monotonic() - t
    drawing: dict[str, Any] | None = None
    if render and s.drawing_enabled:  # a drawing failure never fails the gate
        t = time.monotonic()
        try:
            drawing = render_drawing(shape, spec, vdir, jd.name, version, s.drawing_owner)
            files.update({n: n for n in DRAWING_FILES})
        except Exception as exc:  # noqa: BLE001
            log.warning("drawing failed for %s v%s: %s", jd.name, version, exc)
            drawing = {"error": f"{type(exc).__name__}: {exc}"}
        timings["drawing"] = time.monotonic() - t
    timings["total"] = time.monotonic() - t0
    return {"report": report.model_dump(mode="json"), "files": files, "export_errors": export_errors,
            "warnings": build_warnings, "complex_logs": complex_logs, "timings": timings, "drawing": drawing}


def step_analyze(job_dir: str, name: str) -> dict[str, Any]:
    """STEP intake: recognise a documentation PartSpec from original.step (see step_ingestor)."""
    from app.services.step_ingestor import analyze

    _, a = analyze(Path(job_dir) / "original.step", name)
    return {"spec_json": a.spec.model_dump_json(), "summary": a.summary()}


def cad_render(job_dir: str, version: int, title: str, spec_json: str | None = None, owner: str = "") -> dict[str, Any]:
    """Deferred ortho renders (+ drawing when `spec_json` is given) for a version built without them (PATCH /spec)."""
    from build123d import import_step

    from app.services.verifier import render_orthographic

    jd = Path(job_dir)
    vdir = jd / "versions" / f"v{version}"
    shape = import_step(str(vdir / "model.step"))
    files = {p.name: p.name for p in render_orthographic(shape, vdir, title=title).values()}
    if spec_json:
        try:
            render_drawing(shape, PartSpec.model_validate_json(spec_json), vdir, jd.name, version, owner)
            files.update({n: n for n in DRAWING_FILES})
        except Exception as exc:  # noqa: BLE001
            log.warning("deferred drawing failed for %s v%s: %s", jd.name, version, exc)
    return files


# --------------------------------------------------------------------------- #
# Orchestrator
# --------------------------------------------------------------------------- #


class Pipeline:
    def __init__(self, settings: Settings, store: JobStore, llm: LLMClient,
                 executor_factory: Callable[..., Executor] | None):
        self.s = settings
        self.store = store
        self.llm = llm
        self._executor_factory = executor_factory
        self.executor = executor_factory() if executor_factory else None
        # Deferred ortho renders get their own worker so operator rebuilds never queue behind them.
        self.render_executor = executor_factory(1) if executor_factory else None
        self._tasks: dict[str, asyncio.Task] = {}
        self._bg: set[asyncio.Task] = set()

    # -- paths / versions --------------------------------------------------------
    def job_dir(self, job_id: str) -> Path:
        return self.s.jobs_dir / job_id

    def spec_path(self, job_id: str, version: int) -> Path:
        return self.job_dir(job_id) / "specs" / f"part_spec_v{version}.json"

    def history(self, job_id: str) -> list[dict]:
        p = self.job_dir(job_id) / "specs" / "history.json"
        return json.loads(p.read_text(encoding="utf-8")) if p.exists() else []

    def load_spec(self, job_id: str, version: int) -> PartSpec:
        return PartSpec.model_validate_json(self.spec_path(job_id, version).read_text(encoding="utf-8"))

    def save_spec(self, job_id: str, spec: PartSpec, source: str, reason: str = "") -> int:
        hist = self.history(job_id)
        version = max((h["version"] for h in hist), default=0) + 1
        p = self.spec_path(job_id, version)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(spec.model_dump_json(indent=1), encoding="utf-8")
        hist.append({"version": version, "source": source, "reason": reason[:2000], "created_at": time.time()})
        (p.parent / "history.json").write_text(json.dumps(hist, indent=1), encoding="utf-8")
        return version

    def iteration_logs(self, job_id: str) -> list[dict]:
        p = self.job_dir(job_id) / "iteration_logs.json"
        return json.loads(p.read_text(encoding="utf-8")) if p.exists() else []

    def _log_iteration(self, job_id: str, entry: dict) -> None:
        logs = self.iteration_logs(job_id)
        logs.append({"ts": time.time(), **entry})
        (self.job_dir(job_id) / "iteration_logs.json").write_text(json.dumps(logs, indent=1), encoding="utf-8")

    def is_running(self, job_id: str) -> bool:
        t = self._tasks.get(job_id)
        return t is not None and not t.done()

    @property
    def llm_is_real(self) -> bool:
        return not isinstance(self.llm, MockDeepSeekClient)

    def drawing_images(self, job_id: str) -> list[ExtractionImage]:
        tiles = self.job_dir(job_id) / "tiles"
        order = lambda p: (0 if p.name.startswith("sheet") else 2 if p.name.startswith("title") else 1, p.name)  # noqa: E731
        return [ExtractionImage(p.name.replace(".llm.png", ""), p) for p in sorted(tiles.glob("*.llm.png"), key=order)]

    def digest(self, job_id: str) -> dict[str, str]:
        p = self.job_dir(job_id) / "extraction" / "digest.json"
        return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {"text": "", "vector": ""}

    # -- events ----------------------------------------------------------------
    async def emit(self, job_id: str, stage: str, message: str, progress: float | None = None,
                   level: str = "info", data: dict | None = None, set_status: bool = True) -> None:
        log.info("[%s] %s: %s", job_id, stage, message)
        await self.store.add_event(job_id, stage, message, level=level, progress=progress, data=data)
        if set_status:
            fields: dict[str, Any] = {"status": stage, "stage": stage, "message": message[:1000]}
            if progress is not None:
                fields["progress"] = progress
            await self.store.update_job(job_id, **fields)

    # -- process pool --------------------------------------------------------------
    async def _run_pool(self, fn, *args, render: bool = False):
        pool = self.render_executor if render else self.executor
        if pool is None:
            return await asyncio.to_thread(fn, *args)
        loop = asyncio.get_running_loop()
        try:
            return await loop.run_in_executor(pool, fn, *args)
        except BrokenProcessPool:
            log.error("CAD worker pool broke (native crash?); recreating it")
            if self._executor_factory:
                if render:
                    self.render_executor = self._executor_factory(1)
                else:
                    self.executor = self._executor_factory()
            raise

    def warmup(self) -> None:
        if self.executor is None:
            return

        def done(f):
            if not f.exception():
                log.info("CAD worker warm (%.1fs)", f.result())

        for _ in range(max(1, self.s.cad_workers)):
            self.executor.submit(cad_warmup).add_done_callback(done)
        if self.render_executor:
            self.render_executor.submit(cad_warmup, True).add_done_callback(done)

    def step_source(self, job_id: str) -> str | None:
        """original.step when this STEP job's geometry must come from the upload (recognised spec incomplete)."""
        p = self.job_dir(job_id) / "step_import.json"
        if not p.exists():
            return None
        info = json.loads(p.read_text(encoding="utf-8"))
        return None if info.get("complete") else str(self.job_dir(job_id) / "original.step")

    async def _cad(self, job_id: str, version: int, spec: PartSpec, render: bool,
                   from_upload: bool = False) -> dict[str, Any]:
        src = str(self.job_dir(job_id) / "original.step") if from_upload else self.step_source(job_id)
        try:
            return await self._run_pool(cad_build, str(self.job_dir(job_id)), version, spec.model_dump_json(),
                                        self.s.model_dump(mode="json"), render, src)
        except Exception as exc:  # noqa: BLE001
            return {"fatal": f"CAD-worker gecrasht: {type(exc).__name__}: {exc}"}

    # -- task management -------------------------------------------------------------
    def _spawn(self, job_id: str, coro) -> asyncio.Task:
        task = asyncio.create_task(coro, name=f"job-{job_id}")
        self._tasks[job_id] = task
        task.add_done_callback(lambda t: self._tasks.pop(job_id, None) if self._tasks.get(job_id) is t else None)
        return task

    def _background(self, job_id: str, coro) -> None:
        t = asyncio.create_task(coro, name=f"bg-{job_id}")
        self._bg.add(t)
        t.add_done_callback(self._bg.discard)

    def has_background(self, job_id: str) -> bool:
        return any(t.get_name() == f"bg-{job_id}" and not t.done() for t in self._bg)

    async def shutdown(self) -> None:
        for t in list(self._tasks.values()) + list(self._bg):
            t.cancel()
        for pool in (self.executor, self.render_executor):
            if pool:
                pool.shutdown(wait=False, cancel_futures=True)

    # ================================================================================
    # Entry points
    # ================================================================================

    def start_new_job(self, job_id: str, filename: str) -> asyncio.Task:
        return self._spawn(job_id, self._guard(job_id, lambda tr: self._upload_flow(job_id, filename, tr)))

    def start_from_spec(self, job_id: str, spec: PartSpec, source: str, reason: str = "") -> asyncio.Task:
        """Deterministic intake (DXF, confirmed sketch): no LLM, straight to compile / verify / drawing."""
        return self._spawn(job_id, self._guard(job_id, lambda tr: self._spec_flow(job_id, spec, source, reason)))

    def start_sketch_job(self, job_id: str, filename: str, upload: str) -> asyncio.Task:
        return self._spawn(job_id, self._guard(job_id, lambda tr: self._sketch_flow(job_id, filename, upload, tr)))

    def start_step_job(self, job_id: str, filename: str) -> asyncio.Task:
        return self._spawn(job_id, self._guard(job_id, lambda tr: self._step_flow(job_id, filename)))

    def start_reextract(self, job_id: str, hint: str | None) -> asyncio.Task:
        return self._spawn(job_id, self._guard(job_id, lambda tr: self._reextract_flow(job_id, hint, tr)))

    async def patch_spec(self, job_id: str, spec: PartSpec) -> dict[str, Any]:
        """Stage 6: synchronous deterministic rebuild (no LLM). Returns the new version's result."""
        t0 = time.monotonic()
        version = self.save_spec(job_id, spec, source="user_edit", reason="operator edit (PATCH /spec)")
        if spec.blocking_missing:
            await self._await_dimensions(job_id, version, spec)
            return {"version": version, "status": "awaiting_dimensions", "result": {},
                    "duration_s": round(time.monotonic() - t0, 3)}
        await self.emit(job_id, "compiling_cad", f"Herberekenen v{version} (operator-wijziging, geen AI)", 0.5,
                        data={"version": version})
        result = await self._cad(job_id, version, spec, render=False)
        status = await self._finish(job_id, version, result, critic=None)
        if "model.step" in result.get("files", {}):
            self._background(job_id, self._deferred_render(job_id, version, spec))
        self._log_iteration(job_id, {"iteration": "operator", "version": version, "source": "user_edit",
                                     "gate_passed": status == "ready", "errors": _errors(result),
                                     "duration_s": round(time.monotonic() - t0, 3), "timings": result.get("timings")})
        return {"version": version, "status": status, "result": result,
                "duration_s": round(time.monotonic() - t0, 3)}

    # ================================================================================
    # Flows
    # ================================================================================

    async def _guard(self, job_id: str, make_flow: Callable[[LLMTrace], Any]) -> None:
        trace = LLMTrace()
        try:
            await make_flow(trace)
        except asyncio.CancelledError:
            await self._fail(job_id, "geannuleerd")
            raise
        except Exception as exc:  # noqa: BLE001
            log.exception("job %s failed", job_id)
            await self._fail(job_id, f"{type(exc).__name__}: {exc}", traceback.format_exc(limit=10))
        finally:
            self._save_trace(job_id, trace)

    async def _upload_flow(self, job_id: str, filename: str, trace: LLMTrace) -> None:
        from app.services.pdf_parser import parse_pdf

        jd = self.job_dir(job_id)
        await self.emit(job_id, "parsing_pdf", "Tekstlaag, vectoren en 300 DPI-tegels extraheren", 0.05)
        parsed = await asyncio.to_thread(parse_pdf, jd / "original.pdf", jd, self.s.render_dpi,
                                         self.s.llm_image_max_pixels)
        await self.emit(job_id, "parsing_pdf",
                        f"Pagina {parsed.page_index + 1}/{parsed.page_count}: {len(parsed.spans)} tekstspans, "
                        f"{parsed.vector_stats.get('lines', 0)} lijnen, {len(parsed.circles)} cirkels, "
                        f"{len(parsed.tiles)} afbeeldingen", 0.12,
                        data={"vector_stats": parsed.vector_stats, "has_text_layer": parsed.has_text_layer})
        if not parsed.has_text_layer:
            await self.emit(job_id, "parsing_pdf", "Geen tekstlaag (gescande PDF?): alleen vision", 0.12, level="warning")
        (jd / "extraction" / "digest.json").write_text(
            json.dumps({"text": parsed.text_digest(), "vector": parsed.vector_digest(), "filename": filename}),
            encoding="utf-8")
        await self._extract_and_build(job_id, filename, None, None, trace)

    async def _spec_flow(self, job_id: str, spec: PartSpec, source: str, reason: str,
                         from_upload: bool = False) -> None:
        version = self.save_spec(job_id, spec, source=source, reason=reason)
        await self.emit(job_id, "extracting_features", f"PartSpec v{version} uit {source.upper()}: {len(spec.features)} "
                        f"features (geen AI)", 0.35, data={"version": version})
        if spec.blocking_missing:
            await self._await_dimensions(job_id, version, spec)
            return
        await self.emit(job_id, "compiling_cad", f"B-Rep compileren v{version}", 0.5, data={"version": version})
        result = await self._cad(job_id, version, spec, render=True, from_upload=from_upload)
        await self.emit(job_id, "verifying", _verify_msg(version, result), 0.85, data={"version": version})
        await self._finish(job_id, version, result, critic=None)

    async def _step_flow(self, job_id: str, filename: str) -> None:
        jd = self.job_dir(job_id)
        await self.emit(job_id, "parsing_pdf", "STEP inlezen en vormelementen herkennen (geen AI)", 0.1)
        out = await self._run_pool(step_analyze, str(jd), Path(filename).stem)
        (jd / "step_import.json").write_text(json.dumps(out["summary"], indent=1), encoding="utf-8")
        spec = PartSpec.model_validate_json(out["spec_json"])
        summ = out["summary"]
        await self.emit(job_id, "extracting_features",
                        f"{len(spec.features)} features herkend; " + ("de PartSpec beschrijft het model volledig"
                                                                     if summ["complete"] else
                                                                     "geometrie blijft uit het STEP (niet alles herkend)"),
                        0.3, data=summ)
        await self._spec_flow(job_id, spec, "step", "STEP import", from_upload=True)

    async def _sketch_flow(self, job_id: str, filename: str, upload: str, trace: LLMTrace,
                           hint: str | None = None) -> None:
        from app.services.sketch_ingestor import cap_confidence, rectify, sheet_pdf

        jd = self.job_dir(job_id)
        tiles = jd / "tiles"
        if not (tiles / "sheet.llm.png").exists():
            await self.emit(job_id, "parsing_pdf", "Schets rechtzetten (ArUco-merkers, perspectiefcorrectie)", 0.05)
            res = await asyncio.to_thread(rectify, (jd / upload).read_bytes(), tiles)
            for src, dst in (("sheet.png", "sheet.llm.png"), ("front.png", "tile_front.llm.png"),
                             ("side.png", "tile_side.llm.png"), ("top.png", "tile_top.llm.png"),
                             ("title.png", "title_block.llm.png")):
                shutil.copyfile(tiles / src, tiles / dst)
            await asyncio.to_thread(sheet_pdf, res.sheet, jd / "original.pdf")
            (jd / "extraction").mkdir(exist_ok=True)
            (jd / "extraction" / "digest.json").write_text(json.dumps(
                {"text": "", "vector": "", "filename": filename, "kind": "sketch",
                 "markers": res.markers_found, "reprojection_error_px": res.reprojection_error_px}), encoding="utf-8")
            await self.emit(job_id, "parsing_pdf", f"Schets rechtgezet: 4 merkers, herprojectiefout "
                            f"{res.reprojection_error_px:.2f} px", 0.12, data={"reprojection_error_px": res.reprojection_error_px})
        images = self.drawing_images(job_id)
        model = self.s.deepseek_vision_model if self.llm_is_real else "offline mock"
        await self.emit(job_id, "extracting_features", f"Schets lezen met {model} (alleen geschreven maten tellen)", 0.2)
        spec = cap_confidence(await self.llm.extract_sketch_spec(images, trace, filename=filename, hint=hint))
        version = self.save_spec(job_id, spec, source="sketch", reason=hint or "")
        await self.emit(job_id, "extracting_features",
                        f"PartSpec v{version} uit schets: {len(spec.features)} features, alle waarden te controleren "
                        f"(≤ 75 %), {len(spec.blocking_missing)} ontbrekende maat/maten", 0.35, data={"version": version})
        if spec.blocking_missing:
            await self._await_dimensions(job_id, version, spec)
            return
        await self._heal_loop(job_id, spec, version, "", images, trace, critic=False)

    async def _reextract_flow(self, job_id: str, hint: str | None, trace: LLMTrace) -> None:
        if self.digest(job_id).get("kind") == "sketch":
            await self._sketch_flow(job_id, self.digest(job_id).get("filename", ""), "", trace, hint=hint)
            return
        job = await self.store.get_job(job_id)
        previous = self.load_spec(job_id, job["current_version"]) if job and job["current_version"] else None
        await self._extract_and_build(job_id, (job or {}).get("filename", ""), hint, previous, trace)

    async def _extract_and_build(self, job_id: str, filename: str, hint: str | None,
                                 previous: PartSpec | None, trace: LLMTrace) -> None:
        d = self.digest(job_id)
        images = self.drawing_images(job_id)
        model = self.s.deepseek_vision_model if self.llm_is_real else "offline mock"
        await self.emit(job_id, "extracting_features",
                        f"PartSpec extraheren met {model}" + (f" (hint: {hint})" if hint else ""), 0.2)
        spec = await self.llm.extract_part_spec(d["text"], d["vector"], images, trace, filename=filename,
                                                hint=hint, previous=previous)
        version = self.save_spec(job_id, spec, source="reextract" if (hint or previous) else "extraction",
                                 reason=hint or "")
        await self.emit(job_id, "extracting_features",
                        f"PartSpec v{version}: {len(spec.features)} features, {len(spec.edge_treatments)} randbewerkingen, "
                        f"{len(spec.review_items())} te controleren, {len(spec.ambiguities)} onduidelijkheden",
                        0.35, data={"version": version})
        if spec.blocking_missing:
            await self._await_dimensions(job_id, version, spec)
            return
        await self._heal_loop(job_id, spec, version, d["text"], images, trace)

    async def _heal_loop(self, job_id: str, spec: PartSpec, version: int, digest: str,
                         images: list[ExtractionImage], trace: LLMTrace, critic: bool = True) -> None:
        """Stages 3-5 with at most ``max_heal_retries`` automatic retries."""
        llm = self.llm_is_real
        attempts = 1 + max(0, self.s.max_heal_retries)
        code_errors: dict[str, tuple[str | None, str]] = {}
        best: tuple[int, dict] | None = None  # last build that passed the gate
        approved: tuple[int, dict] | None = None  # last build the visual critic approved
        last: tuple[int, dict] | None = None
        critics: dict[int, Any] = {}
        critic_report = None

        for it in range(1, attempts + 1):
            t0 = time.monotonic()
            base = 0.35 + 0.55 * (it - 1) / attempts
            entry: dict[str, Any] = {"iteration": it, "version": version}
            if spec.requires_complex_3d_fallback:
                spec, changed = await self._ensure_code(job_id, spec, code_errors, trace)
                if changed:
                    version = self.save_spec(job_id, spec, source="complex_code", reason="build123d fallback code")
                    entry["version"] = version
            code_errors = {}

            await self.emit(job_id, "compiling_cad", f"B-Rep compileren v{version} (iteratie {it}/{attempts})", base,
                            data={"version": version, "iteration": it})
            result = await self._cad(job_id, version, spec, render=True)
            last = (version, result)
            errors = _errors(result)
            await self.emit(job_id, "verifying", _verify_msg(version, result), base + 0.08,
                            level="error" if errors else "info", data={"version": version, "errors": errors[:20]})
            entry.update(gate_passed=not errors, errors=errors, timings=result.get("timings"))
            if not errors:
                best = (version, result)

            action = None
            if errors:
                if llm and it < attempts:
                    failing_code = {f.id: (f.code, next(e for e in errors if e.startswith(f"{f.id}:")))
                                    for f in spec.complex_features() if any(e.startswith(f"{f.id}:") for e in errors)}
                    if failing_code and len(failing_code) == len(errors):
                        code_errors = failing_code
                        action = "regenerate_code"
                        await self.emit(job_id, "retrying", f"Fallback-code herstellen voor {', '.join(failing_code)} "
                                        f"({self.s.deepseek_code_model})", base + 0.1)
                    else:
                        action = "correct_spec"
                        await self.emit(job_id, "retrying", f"Kernel/gate-fouten terug naar extractie "
                                        f"({self.s.deepseek_vision_model}): {len(errors)} probleem/problemen",
                                        base + 0.1, data={"errors": errors[:20]})
                        try:
                            spec = await self.llm.correct_spec(spec, errors, digest, images, trace)
                            version = self.save_spec(job_id, spec, source="self_heal", reason="; ".join(errors))
                        except Exception as exc:  # noqa: BLE001
                            await self.emit(job_id, "retrying", f"Correctie mislukt: {exc}", base + 0.1, level="warning")
                            action = None
                entry["action"] = action
                entry["duration_s"] = round(time.monotonic() - t0, 2)
                self._log_iteration(job_id, entry)
                if action:
                    continue
                break

            # gate passed -> visual critic (needs budget left for a correction)
            if llm and critic and self.s.critic_enabled and it < attempts and "ortho_check.png" in result.get("files", {}):
                await self.emit(job_id, "verifying", f"Visuele controle: ortho_check.png v{version} vs. tekening",
                                base + 0.12)
                hints = [i["message"] for i in result["report"]["issues"] if i["code"] == "CALLOUT_UNMATCHED"]
                try:
                    critic_report = await self.llm.critique(spec, images, self.job_dir(job_id) / "versions" /
                                                            f"v{version}" / "ortho_check.png", digest, trace, hints)
                except Exception as exc:  # noqa: BLE001
                    await self.emit(job_id, "verifying", f"Visuele controle niet beschikbaar: {exc}", base + 0.12,
                                    level="warning")
                    critic_report = None
                if critic_report is not None:
                    entry["critic"] = critic_report.model_dump()
                    critics[version] = critic_report
                    actionable = critic_report.actionable
                    if not actionable:
                        approved = (version, result)
                    if critic_report.verdict == "mismatch" and actionable:
                        problems = [f"visual: {d.feature_id or '-'}: {d.issue} (evidence: {d.evidence})"
                                    for d in actionable]
                        await self.emit(job_id, "retrying", "Afwijkingen gevonden: " + "; ".join(critic_report.summary)[:600],
                                        base + 0.15, level="warning", data={"discrepancies": critic_report.summary})
                        try:
                            new = await self.llm.correct_spec(spec, problems, digest, images, trace)
                            if new.model_dump() != spec.model_dump():
                                spec = new
                                version = self.save_spec(job_id, spec, source="critic", reason="; ".join(problems))
                                entry["action"] = "correct_spec"
                                entry["duration_s"] = round(time.monotonic() - t0, 2)
                                self._log_iteration(job_id, entry)
                                continue
                        except Exception as exc:  # noqa: BLE001
                            await self.emit(job_id, "retrying", f"Correctie mislukt: {exc}", base + 0.15, level="warning")
                    else:
                        minor = len(critic_report.discrepancies)
                        await self.emit(job_id, "verifying", "Visuele controle: model komt overeen met de tekening"
                                        + (f" ({minor} kleine opmerking(en))" if minor else ""), base + 0.15)
            entry["duration_s"] = round(time.monotonic() - t0, 2)
            self._log_iteration(job_id, entry)
            break

        # Final choice: the last critic-approved build, else the last build that passed the gate, else the last one.
        assert last is not None
        final = approved or (best if _errors(last[1]) and best else last)
        if final[0] != last[0]:
            await self.emit(job_id, "verifying", f"Eindresultaat: v{final[0]} (laatst goedgekeurde build) i.p.v. "
                            f"v{last[0]}", None, level="warning", set_status=False)
        critic_report = critics.get(final[0])
        assert final is not None
        await self._finish(job_id, final[0], final[1], critic=critic_report)

    async def _ensure_code(self, job_id: str, spec: PartSpec, errors: dict[str, tuple[str | None, str]],
                           trace: LLMTrace) -> tuple[PartSpec, bool]:
        todo = {f.id for f in spec.complex_features() if not f.code or f.id in errors}
        if not todo:
            return spec, False
        feats, changed = [], False
        for f in spec.features:
            if isinstance(f, Complex3D) and f.id in todo:
                prev, err = errors.get(f.id, (None, None))
                await self.emit(job_id, "compiling_cad", f"build123d-fallbackcode genereren voor {f.id} "
                                f"({self.s.deepseek_code_model})", None)
                try:
                    f = f.model_copy(update={"code": await self.llm.generate_complex_code(spec, f, prev, err, trace)})
                    changed = True
                except Exception as exc:  # noqa: BLE001
                    await self.emit(job_id, "compiling_cad", f"Codegeneratie mislukt voor {f.id}: {exc}", None,
                                    level="warning")
            feats.append(f)
        return spec.model_copy(update={"features": feats}), changed

    # ================================================================================
    # Finishing
    # ================================================================================

    async def _finish(self, job_id: str, version: int, result: dict[str, Any], critic) -> str:
        jd = self.job_dir(job_id)
        vdir = jd / "versions" / f"v{version}"
        if "fatal" in result or "model.step" not in result.get("files", {}):
            msg = result.get("fatal") or "; ".join(result.get("export_errors", [])) or "geen STEP geproduceerd"
            await self.store.update_job(job_id, current_version=version, error=msg[:4000],
                                        result=self._summary(job_id, version, result, critic))
            await self._fail(job_id, msg)
            return "failed"
        for name in CURRENT_FILES:  # promote this version to the job's current artifacts
            if (vdir / name).exists():
                shutil.copyfile(vdir / name, jd / name)
            elif (jd / name).exists():
                (jd / name).unlink()
        report = DFMReport.model_validate(result["report"])
        status = "ready" if report.passed else "failed"
        summary = self._summary(job_id, version, result, critic)
        await self.store.update_job(job_id, current_version=version, result=summary,
                                    error=None if status == "ready" else "geometriecontrole gefaald")
        n_warn = sum(1 for i in report.issues if i.severity == Severity.warning)
        if status == "ready":
            msg = f"STEP klaar (v{version}): {n_warn} DFM-waarschuwing(en), {len(report.review_items)} te controleren"
        else:
            msg = f"v{version} faalt de geometriecontrole: " + "; ".join(i.message for i in report.errors)[:500]
        await self.emit(job_id, status, msg, 1.0, level="info" if status == "ready" else "error", data=summary)
        return status

    def _summary(self, job_id: str, version: int, result: dict[str, Any], critic) -> dict[str, Any]:
        rep = result.get("report") or {}
        issues = rep.get("issues", [])
        spec = self.load_spec(job_id, version)
        return {
            "version": version,
            "passed": bool(rep.get("passed")),
            "errors": sum(1 for i in issues if i["severity"] == "error"),
            "warnings": sum(1 for i in issues if i["severity"] == "warning"),
            "review_items": rep.get("review_items", []),
            "ambiguities": len(spec.ambiguities),
            "needs_review": bool(rep.get("review_items") or spec.ambiguities
                                 or any(i["severity"] != "info" for i in issues)),
            "critic": critic.model_dump() if critic is not None else None,
            "timings": result.get("timings"),
            "drawing": result.get("drawing"),
        }

    async def _await_dimensions(self, job_id: str, version: int, spec: PartSpec) -> None:
        """DRAWING_SPEC §5: blocking missing dimensions pause the job before any CAD / drawing work."""
        missing = spec.blocking_missing
        await self.store.update_job(job_id, current_version=version, error=None)
        await self.emit(job_id, "awaiting_dimensions",
                        f"v{version}: {len(missing)} ontbrekende maat/maten invullen: "
                        + "; ".join(m.prompt_nl for m in missing)[:600], 0.4,
                        level="warning", data={"version": version, "missing": [m.model_dump() for m in missing]})

    async def _promote(self, job_id: str, version: int, names: tuple[str, ...]) -> None:
        """Copy late artifacts of `version` to the job root if it is still the current version."""
        job = await self.store.get_job(job_id)
        if not job or job["current_version"] != version:
            return
        vdir = self.job_dir(job_id) / "versions" / f"v{version}"
        for name in names:
            if (vdir / name).exists():
                shutil.copyfile(vdir / name, self.job_dir(job_id) / name)

    async def _deferred_render(self, job_id: str, version: int, spec: PartSpec) -> None:
        try:
            spec_json = spec.model_dump_json() if self.s.drawing_enabled else None
            await self._run_pool(cad_render, str(self.job_dir(job_id)), version, spec.part_name or "", spec_json,
                                 self.s.drawing_owner, render=True)
            await self._promote(job_id, version, ("ortho_check.png",) + DRAWING_FILES)
            await self.emit(job_id, "verifying", f"Ortho-aanzichten en ISO-tekening v{version} bijgewerkt", None,
                            set_status=False, data={"version": version, "renders": True, "drawing": True})
        except Exception as exc:  # noqa: BLE001
            log.warning("deferred render failed for %s v%s: %s", job_id, version, exc)

    async def regenerate_drawing(self, job_id: str, version: int, overrides: dict[str, Any]) -> dict[str, Any]:
        """POST /drawing: (re)generate the EN ISO drawing of a built version with optional overrides."""
        spec = self.load_spec(job_id, version)
        info = await self._run_pool(cad_drawing, str(self.job_dir(job_id)), version, spec.model_dump_json(),
                                    overrides, self.s.drawing_owner, render=True)
        await self._promote(job_id, version, DRAWING_FILES)
        await self.emit(job_id, "verifying", f"ISO-tekening v{version}: {info['sheet_size'].replace('_LANDSCAPE', '')} "
                        f"{info['scale']}", None, set_status=False, data={"version": version, "drawing": info})
        return info

    async def _fail(self, job_id: str, message: str, tb: str | None = None) -> None:
        await self.store.update_job(job_id, error=message[:4000])
        await self.emit(job_id, "failed", message[:1000], 1.0, level="error", data={"traceback": tb} if tb else None)

    def _save_trace(self, job_id: str, trace: LLMTrace) -> None:
        if not trace.calls:
            return
        d = self.job_dir(job_id) / "llm"
        d.mkdir(parents=True, exist_ok=True)
        (d / f"trace_{int(time.time() * 1000)}.json").write_text(json.dumps(trace.calls, indent=1), encoding="utf-8")


def _errors(result: dict[str, Any]) -> list[str]:
    """Gate failures of one build, formatted for the correction prompt (feature errors as '<id>: ...')."""
    if "fatal" in result:
        return [f"kernel: {result['fatal']}"]
    rep = DFMReport.model_validate(result["report"])
    errs = [f"{s.feature_id}: {s.error}" for s in rep.feature_status if not s.ok]
    errs += [f"{i.code}: {i.message}" for i in rep.issues if i.severity == Severity.error and i.code.startswith("GEOM_")]
    return errs + list(result.get("export_errors", []))


def _verify_msg(version: int, result: dict[str, Any]) -> str:
    if "fatal" in result:
        return f"v{version}: kernelfout: {result['fatal']}"
    rep = DFMReport.model_validate(result["report"])
    g = rep.geometry
    n = {s: sum(1 for i in rep.issues if i.severity == s) for s in Severity}
    return (f"v{version}: geldig={g.is_valid if g else False}, solids={g.solid_count if g else 0}, "
            f"bbox Δ={g.bbox_deviation_mm if g else 0:.3f} mm, {n[Severity.error]} fouten, "
            f"{n[Severity.warning]} waarschuwingen, {rep.colored_faces or {}} gekleurde vlakken")
