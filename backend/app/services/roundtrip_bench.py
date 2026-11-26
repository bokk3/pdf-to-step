"""Round-trip extraction benchmark (DRAWING_SPEC.md §7 phase 2).

    procedural PartSpec ──► ground_truth.step (V_gt, bbox) + synthetic_iso.pdf (drawing_engine)
                      ──► full pipeline.py flow (parse → extract → compile → verify → self-heal)
                      ──► predicted spec + model.step ──► geometric + order-independent feature diff

Metrics per case: predicted STEP is one valid solid, bounding-box deviation ≤ ±0.05 mm, volume error < 0.5 %,
and nearest-neighbour feature matching per type (centre and key dimensions within ±0.10 mm, any array order or
pattern representation) with precision / recall. Fixture PDFs with an expected ``*.part_spec.json`` are always
reported in a separate section, so accuracy is never judged on clean synthetic drawings alone (§1.2).

A/B: every combination of ``--thinking`` and ``--prompt-variant`` is a variant with its own data dir, reported side
by side (accuracy, latency, tokens, optional cost). ``--llm oracle`` replays the ground truth through the offline
mock client to validate the harness without API calls.

    python -m app.services.roundtrip_bench --n 10 --seed 7 --llm deepseek --thinking enabled,disabled
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import random
import shutil
import statistics
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

from app.config import BACKEND_DIR, Settings
from app.schemas.part_spec import (
    BossCircle,
    BossRect,
    Complex3D,
    Hole,
    PartSpec,
    PocketCircle,
    PocketPolygon,
    PocketRect,
    Slot,
    StepCut,
    parse_metric_thread,
)

BBOX_TOL_MM = 0.05
VOLUME_TOL = 0.005  # 0.5 %
FEATURE_TOL_MM = 0.10
NEAR_MISS_MM = 3.0  # pairs closer than this are reported as near misses (diagnostics only)
MATERIALS = ["EN AW-6082 T6", "EN AW-6082 T6", "EN AW-7075 T651", "S355J2", "1.4301", "POM-C"]
CLEARANCE = {3: (3.4, 6.5, 3.4), 4: (4.5, 8.0, 4.4), 5: (5.5, 10.0, 5.4), 6: (6.6, 11.0, 6.8), 8: (9.0, 15.0, 9.0)}


# --------------------------------------------------------------------------- #
# 1. Procedural PartSpecs
# --------------------------------------------------------------------------- #


def _r(v: float, step: float = 0.5) -> float:
    return round(round(v / step) * step, 3)


def _cell_feature(rng: random.Random, kind: str, fid: str, cx: float, cy: float, w: float, h: float, T: float) -> dict | None:
    """One feature centred in a free cell of size w x h (already shrunk by the wall margin)."""
    blind = [d for d in (2, 3, 4, 5, 6, 8, 10, 12) if d <= T - 2.5]
    if kind == "pocket_rect":
        if w < 10 or h < 10 or not blind:
            return None
        lx, wy = _r(rng.uniform(0.45, 0.9) * w, 1), _r(rng.uniform(0.45, 0.9) * h, 1)
        rmax = min(lx, wy) / 2
        radii = [r for r in (1.5, 2, 3, 4, 5, 6, 8) if r <= rmax - 0.5]
        if not radii:
            return None
        return {"id": fid, "type": "pocket_rect", "face": "top", "center_x": cx, "center_y": cy, "length_x": lx,
                "width_y": wy, "corner_radius": rng.choice(radii), "depth": float(rng.choice(blind))}
    if kind == "pocket_circle":
        d = _r(min(w, h) * rng.uniform(0.5, 0.9), 1)
        if d < 8 or not blind:
            return None
        return {"id": fid, "type": "pocket_circle", "face": "top", "center_x": cx, "center_y": cy, "diameter": d,
                "depth": float(rng.choice(blind))}
    if kind == "slot":
        horizontal = w >= h
        span = (w if horizontal else h)
        width = float(rng.choice([4, 5, 6, 8, 10]))
        length = _r(rng.uniform(0.4, 0.8) * span - width, 1)
        if length < 6 or width > (h if horizontal else w) - 2 or not blind:
            return None
        dx, dy = (length / 2, 0.0) if horizontal else (0.0, length / 2)
        return {"id": fid, "type": "slot", "face": "top", "start_x": _r(cx - dx), "start_y": _r(cy - dy),
                "end_x": _r(cx + dx), "end_y": _r(cy + dy), "width": width, "depth": float(rng.choice(blind))}
    if kind == "thread":
        sizes = [m for m in (3, 4, 5, 6, 8, 10) if m + 6 <= min(w, h)]
        if not sizes:
            return None
        m = rng.choice(sizes)
        through = T <= 2.5 * m or rng.random() < 0.4
        f = {"id": fid, "type": "hole", "face": "top", "center_x": cx, "center_y": cy, "diameter": float(m),
             "thread": f"M{m}"}
        if through:
            f["through_all"] = True
        else:
            depth = float(min(T - 2, _r(rng.uniform(1.6, 2.5) * m, 1)))
            if depth < 1.5 * m:
                f["through_all"] = True
            else:
                f["depth"] = depth
                f["thread_depth"] = float(max(m, _r(depth - 0.4 * m - 1, 1)))
        return f
    if kind in ("hole", "hole_blind", "hole_cbore", "hole_csk", "hole_fit"):
        m = rng.choice([m for m in CLEARANCE if CLEARANCE[m][1] + 6 <= min(w, h)] or [0])
        if not m:
            return None
        clear, cbore, csk = CLEARANCE[m]
        f: dict[str, Any] = {"id": fid, "type": "hole", "face": "top", "center_x": cx, "center_y": cy,
                             "diameter": clear, "through_all": True}
        if kind == "hole_blind":
            if not blind:
                return None
            f.pop("through_all")
            f["diameter"] = float(rng.choice([3, 4, 5, 6, 8, 10]))
            f["depth"] = float(rng.choice(blind))
        elif kind == "hole_cbore":
            depth = _r(min(T - 3, 0.6 * cbore + 0.5), 0.1)
            if depth < 2:
                return None
            f["counterbore"] = {"diameter": cbore, "depth": depth}
        elif kind == "hole_csk":
            f["countersink"] = {"diameter": csk * 2 if csk * 2 + 4 <= min(w, h) else csk + 3, "angle_deg": 90.0}
        elif kind == "hole_fit":
            f["diameter"] = float(rng.choice([6, 8, 10, 12]))
            f["tolerance"] = "H7"
        return f
    return None


def generate_spec(rng: random.Random, name: str) -> PartSpec:
    """A realistic 2.5D milling part: stock, optional rounded corners + corner hole pattern, 2-8 cell features."""
    L = float(rng.choice(range(60, 225, 5)))
    W = float(rng.choice([w for w in range(40, 165, 5) if w <= L]))
    T = float(rng.choice([8, 10, 12, 15, 16, 20, 25, 30]))
    spec: dict[str, Any] = {
        "schema_version": "1.1", "part_name": name, "material": rng.choice(MATERIALS),
        "general_tolerance": "ISO 2768-mK", "stock": {"length_x": L, "width_y": W, "thickness_z": T},
        "outer_profile": {"type": "rectangle"}, "features": [], "edge_treatments": [],
        "drawing_metadata": {"drawing_number": f"BENCH-{name}"},
    }
    R = 0.0
    if rng.random() < 0.5:
        R = float(rng.choice([r for r in (2, 3, 4, 5, 6, 8, 10) if r <= min(L, W) / 6] or [2]))
        spec["outer_profile"] = {"type": "rounded_rectangle", "corner_radius": R}
    x0, x1, y0, y1 = 0.0, L, 0.0, W
    feats: list[dict] = spec["features"]
    if rng.random() < 0.55 and min(L, W) >= 50:  # mounting holes in the corners
        m = rng.choice([4, 5, 6])
        clear, cbore, _ = CLEARANCE[m]
        inset = float(max(8, math.ceil(R + cbore / 2 + 2)))
        f = {"id": "H_CORNER", "type": "hole", "face": "top", "center_x": inset, "center_y": inset,
             "diameter": clear, "through_all": True,
             "pattern": {"type": "linear", "count_x": 2, "count_y": 2, "pitch_x": L - 2 * inset, "pitch_y": W - 2 * inset}}
        if rng.random() < 0.5 and T >= 8:
            f["counterbore"] = {"diameter": cbore, "depth": _r(min(T - 3, 0.6 * cbore + 0.5), 0.1)}
        feats.append(f)
        x0, x1 = inset + cbore / 2 + 3, L - inset - cbore / 2 - 3
    cols = max(1, min(4, int((x1 - x0) // 38)))
    rows = max(1, min(3, int((y1 - y0) // 38)))
    kinds = ["pocket_rect", "pocket_rect", "pocket_circle", "slot", "thread", "thread", "hole", "hole_blind",
             "hole_cbore", "hole_csk", "hole_fit"]
    n = 0
    for i in range(cols):
        for j in range(rows):
            if rng.random() < 0.2:
                continue
            cw, ch = (x1 - x0) / cols, (y1 - y0) / rows
            cx, cy = _r(x0 + (i + 0.5) * cw), _r(y0 + (j + 0.5) * ch)
            margin = 3.0
            for _ in range(4):
                f = _cell_feature(rng, rng.choice(kinds), f"F{n + 1}", cx, cy, cw - 2 * margin, ch - 2 * margin, T)
                if f:
                    feats.append(f)
                    n += 1
                    break
    if rng.random() < 0.4:
        spec["edge_treatments"].append({"id": "E1", "type": "chamfer", "size": rng.choice([0.5, 1.0]),
                                        "target": "top_outer_edges"})
    return PartSpec.model_validate(spec)


def valid_spec(spec: PartSpec, s: Settings) -> bool:
    """Compiles, passes the gate and has no DFM overlap / breakout warnings (keeps the fixtures realistic)."""
    from app.services.cad_compiler import compile_part
    from app.services.verifier import check_geometry, dfm_rules

    try:
        res = compile_part(spec)
    except Exception:  # noqa: BLE001
        return False
    if not all(st.ok for st in res.feature_status):
        return False
    geo, issues = check_geometry(res.shape, spec, s.dfm_bbox_tolerance_mm)
    if not geo.is_valid or geo.solid_count != 1 or any(i.severity == "error" for i in issues):
        return False
    bad = {"THIN_WEB", "BREAKOUT", "THIN_WALL", "THIN_FLOOR"}
    return not any(i.code in bad for i in dfm_rules(spec, s))


def generate_specs(n: int, seed: int, s: Settings | None = None) -> list[PartSpec]:
    s = s or Settings()
    rng = random.Random(seed)
    out: list[PartSpec] = []
    tries = 0
    while len(out) < n and tries < n * 30:
        tries += 1
        spec = generate_spec(rng, f"RT{seed:03d}-{len(out) + 1:02d}")
        if spec.features and valid_spec(spec, s):
            out.append(spec)
    return out


# --------------------------------------------------------------------------- #
# 2. Order-independent feature diff
# --------------------------------------------------------------------------- #


@dataclass
class Inst:
    type: str
    fid: str
    center: tuple[float, float, float]
    dims: dict[str, Any]


def _thickness(spec: PartSpec, face: str) -> float:
    (x0, y0, z0), (x1, y1, z1) = spec.stock_bbox()
    return {"top": z1 - z0, "bottom": z1 - z0, "front": y1 - y0, "back": y1 - y0}.get(face, x1 - x0)


def _depth(spec: PartSpec, f) -> float:
    return _thickness(spec, f.face) if getattr(f, "through_all", False) else float(getattr(f, "depth", 0) or 0)


def instances(spec: PartSpec) -> list[Inst]:
    """Expand patterns / extra positions into placed instances in global XYZ (face-representation independent)."""
    from app.services.cad_compiler import face_frames

    frames = face_frames(spec)
    out: list[Inst] = []

    def at(face: str, u: float, v: float) -> tuple[float, float, float]:
        fr = frames[face]
        p = fr.origin + fr.u_dir * u + fr.v_dir * v
        return (round(p.X, 4), round(p.Y, 4), round(p.Z, 4))

    for f in spec.features:
        if isinstance(f, Hole):
            t = parse_metric_thread(f.thread)
            dims = {"diameter": f.diameter, "depth": _depth(spec, f), "thread": f"M{t[0]:g}x{t[1]:g}" if t else f.thread,
                    "cbore_d": f.counterbore.diameter if f.counterbore else None,
                    "cbore_depth": f.counterbore.depth if f.counterbore else None,
                    "csk_d": f.countersink.diameter if f.countersink else None}
            out += [Inst("hole", f.id, at(f.face, x, y), dims) for x, y in f.positions()]
        elif isinstance(f, PocketRect):
            lx, wy = (f.length_x, f.width_y) if round(f.rotation_deg / 90) % 2 == 0 else (f.width_y, f.length_x)
            dims = {"length_x": lx, "width_y": wy, "corner_radius": f.corner_radius, "depth": _depth(spec, f)}
            out += [Inst("pocket_rect", f.id, at(f.face, x, y), dims) for x, y in f.positions()]
        elif isinstance(f, PocketCircle):
            dims = {"diameter": f.diameter, "depth": _depth(spec, f)}
            out += [Inst("pocket_circle", f.id, at(f.face, x, y), dims) for x, y in f.positions()]
        elif isinstance(f, PocketPolygon):
            xs, ys = [p[0] for p in f.points_2d], [p[1] for p in f.points_2d]
            dims = {"size_u": max(xs) - min(xs), "size_v": max(ys) - min(ys), "depth": _depth(spec, f)}
            out.append(Inst("pocket_polygon", f.id, at(f.face, (min(xs) + max(xs)) / 2, (min(ys) + max(ys)) / 2), dims))
        elif isinstance(f, Slot):
            ang = math.degrees(math.atan2(f.end_y - f.start_y, f.end_x - f.start_x)) % 180
            dims = {"width": f.width, "length": f.length, "angle": round(ang, 3), "depth": _depth(spec, f)}
            out.append(Inst("slot", f.id, at(f.face, (f.start_x + f.end_x) / 2, (f.start_y + f.end_y) / 2), dims))
        elif isinstance(f, StepCut):
            dims = {"size_u": f.x_max - f.x_min, "size_v": f.y_max - f.y_min, "depth": f.depth}
            out.append(Inst("step_cut", f.id, at(f.face, (f.x_min + f.x_max) / 2, (f.y_min + f.y_max) / 2), dims))
        elif isinstance(f, BossRect):
            dims = {"length_x": f.length_x, "width_y": f.width_y, "height": f.height}
            out += [Inst("boss_rect", f.id, at(f.face, x, y), dims) for x, y in f.positions()]
        elif isinstance(f, BossCircle):
            dims = {"diameter": f.diameter, "height": f.height}
            out += [Inst("boss_circle", f.id, at(f.face, x, y), dims) for x, y in f.positions()]
        elif isinstance(f, Complex3D):
            out.append(Inst("complex_3d", f.id, (0.0, 0.0, 0.0), {}))
    for et in spec.edge_treatments:
        out.append(Inst(f"edge_{et.type}", et.id, (0.0, 0.0, 0.0), {"size": et.size, "target": et.target}))
    p = spec.outer_profile
    out.append(Inst("outer_profile", "outer_profile", (0.0, 0.0, 0.0),
                    {"shape": "rounded" if p.type == "rounded_rectangle" and p.corner_radius > 0 else p.type,
                     "corner_radius": p.corner_radius if p.type == "rounded_rectangle" else 0.0}))
    return out


def _dims_match(a: dict, b: dict, tol: float) -> tuple[bool, dict[str, Any]]:
    bad: dict[str, Any] = {}
    for k in sorted(set(a) | set(b)):
        x, y = a.get(k), b.get(k)
        if isinstance(x, (int, float)) and isinstance(y, (int, float)) and not isinstance(x, bool):
            if abs(float(x) - float(y)) > tol + 1e-9:
                bad[k] = (x, y)
        elif x != y:
            bad[k] = (x, y)
    return not bad, bad


def feature_diff(gt: PartSpec, pred: PartSpec, tol: float = FEATURE_TOL_MM) -> dict[str, Any]:
    """Greedy nearest-neighbour pairing per type (any order); TP = centre and every key dimension within `tol`."""
    G, P = instances(gt), instances(pred)
    types = sorted({i.type for i in G} | {i.type for i in P})
    per: dict[str, Any] = {}
    misses: list[dict] = []
    tp_all = 0
    for t in types:
        g = [i for i in G if i.type == t]
        p = [i for i in P if i.type == t]
        pairs = sorted(((float(np.linalg.norm(np.subtract(a.center, b.center))), ia, ib)
                        for ia, a in enumerate(g) for ib, b in enumerate(p)), key=lambda x: x[0])
        used_g, used_p, tp = set(), set(), 0
        for dist, ia, ib in pairs:
            if ia in used_g or ib in used_p or dist > NEAR_MISS_MM:
                continue
            ok, bad = _dims_match(g[ia].dims, p[ib].dims, tol)
            if dist <= tol + 1e-9 and ok:
                tp += 1
                used_g.add(ia)
                used_p.add(ib)
            elif dist <= tol + 1e-9 or not bad:
                used_g.add(ia)
                used_p.add(ib)
                misses.append({"type": t, "gt": g[ia].fid, "pred": p[ib].fid, "center_error_mm": round(dist, 4),
                               "dims": {k: list(v) for k, v in bad.items()}})
        per[t] = {"gt": len(g), "pred": len(p), "tp": tp,
                  "precision": tp / len(p) if p else (1.0 if not g else 0.0),
                  "recall": tp / len(g) if g else (1.0 if not p else 0.0)}
        tp_all += tp
    n_g = sum(v["gt"] for v in per.values())
    n_p = sum(v["pred"] for v in per.values())
    return {"per_type": per, "tp": tp_all, "gt": n_g, "pred": n_p,
            "precision": tp_all / n_p if n_p else 1.0, "recall": tp_all / n_g if n_g else 1.0,
            "near_misses": misses}


def shape_metrics(shape) -> dict[str, Any]:
    from app.services.verifier import _is_valid

    bb = shape.bounding_box()
    return {"valid": bool(_is_valid(shape)), "solids": len(shape.solids()), "volume_mm3": float(shape.volume),
            "bbox_min": [bb.min.X, bb.min.Y, bb.min.Z], "bbox_max": [bb.max.X, bb.max.Y, bb.max.Z]}


def geometry_diff(gt: dict, pred: dict | None) -> dict[str, Any]:
    if pred is None:
        return {"solid_ok": False, "bbox_ok": False, "volume_ok": False, "bbox_error_mm": None, "volume_error": None}
    bbox_err = max(abs(a - b) for a, b in zip(gt["bbox_min"] + gt["bbox_max"], pred["bbox_min"] + pred["bbox_max"]))
    vol_err = abs(pred["volume_mm3"] - gt["volume_mm3"]) / gt["volume_mm3"]
    return {"solid_ok": pred["valid"] and pred["solids"] == 1, "bbox_ok": bbox_err <= BBOX_TOL_MM,
            "volume_ok": vol_err < VOLUME_TOL, "bbox_error_mm": round(bbox_err, 5), "volume_error": round(vol_err, 6)}


# --------------------------------------------------------------------------- #
# 3. Cases, pipeline runs, report
# --------------------------------------------------------------------------- #


@dataclass
class Case:
    name: str
    kind: str  # synthetic | fixture
    pdf: Path
    gt_spec: PartSpec
    gt: dict[str, Any]


@dataclass
class Variant:
    name: str
    thinking: str | None = None
    prompt_appendix: str = ""
    results: list[dict] = field(default_factory=list)


def prepare_synthetic(specs: list[PartSpec], root: Path, oracle_dir: Path) -> list[Case]:
    from app.services.cad_compiler import compile_part, export_step_file
    from app.services.drawing_engine import generate_drawing

    cases = []
    for spec in specs:
        d = root / "cases" / spec.part_name  # type: ignore[operator]
        d.mkdir(parents=True, exist_ok=True)
        shape = compile_part(spec).shape
        (d / "ground_truth.part_spec.json").write_text(spec.model_dump_json(indent=1), encoding="utf-8")
        export_step_file(shape, d / "ground_truth.step")
        res = generate_drawing(spec, shape, d, basename="synthetic_iso", drawing_number=f"BENCH-{spec.part_name}")
        (oracle_dir / f"{spec.part_name}.part_spec.json").write_text(spec.model_dump_json(), encoding="utf-8")
        gt = shape_metrics(shape)
        gt["drawing"] = {"sheet": res.sheet_size, "scale": res.scale, "warnings": res.warnings}
        (d / "ground_truth.json").write_text(json.dumps(gt, indent=1), encoding="utf-8")
        cases.append(Case(spec.part_name or d.name, "synthetic", res.pdf, spec, gt))  # type: ignore[arg-type]
    return cases


def prepare_fixtures(fixture_dir: Path, oracle_dir: Path) -> list[Case]:
    from app.services.cad_compiler import compile_part

    cases = []
    for pdf in sorted(fixture_dir.glob("*.pdf")):
        sp = pdf.parent / f"{pdf.stem}.part_spec.json"
        if not sp.exists():
            continue
        spec = PartSpec.model_validate_json(sp.read_text(encoding="utf-8"))
        if spec.requires_complex_3d_fallback:
            continue
        shutil.copyfile(sp, oracle_dir / sp.name)
        cases.append(Case(pdf.stem, "fixture", pdf, spec, shape_metrics(compile_part(spec).shape)))
    return cases


def _usage(job_dir: Path) -> dict[str, int]:
    tot = {"calls": 0, "prompt_tokens": 0, "completion_tokens": 0, "reasoning_tokens": 0, "cache_hit_tokens": 0}
    for p in sorted((job_dir / "llm").glob("trace_*.json")):
        for call in json.loads(p.read_text(encoding="utf-8")):
            u = (call.get("meta") or {}).get("usage") or {}
            if not u:
                continue
            tot["calls"] += 1
            tot["prompt_tokens"] += int(u.get("prompt_tokens") or 0)
            tot["completion_tokens"] += int(u.get("completion_tokens") or 0)
            tot["reasoning_tokens"] += int((u.get("completion_tokens_details") or {}).get("reasoning_tokens") or 0)
            tot["cache_hit_tokens"] += int(u.get("prompt_cache_hit_tokens") or 0)
    return tot


async def run_variant(variant: Variant, cases: list[Case], base: Settings, llm_kind: str, oracle_dir: Path,
                      root: Path, concurrency: int = 1, critic: bool | None = None) -> None:
    from build123d import import_step

    from app.db import JobStore
    from app.services.deepseek_client import DeepSeekClient, MockDeepSeekClient
    from app.services.pipeline import Pipeline

    data = root / "runs" / variant.name
    data.mkdir(parents=True, exist_ok=True)
    upd: dict[str, Any] = {"data_dir": data, "extraction_prompt_appendix": variant.prompt_appendix}
    if variant.thinking:
        upd["deepseek_thinking"] = variant.thinking
    if critic is not None:
        upd["critic_enabled"] = critic
    s = base.model_copy(update=upd)
    s.jobs_dir.mkdir(parents=True, exist_ok=True)
    store = JobStore(s.db_path)
    await store.init()
    llm = MockDeepSeekClient([oracle_dir]) if llm_kind == "oracle" else DeepSeekClient(s)
    pipe = Pipeline(s, store, llm, None)
    sem = asyncio.Semaphore(max(1, concurrency))

    async def one(case: Case) -> dict:
        async with sem:
            job = await store.create_job(f"{case.name}.pdf")
            jd = s.jobs_dir / job["id"]
            jd.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(case.pdf, jd / "original.pdf")
            t0 = time.monotonic()
            await pipe.start_new_job(job["id"], f"{case.name}.pdf")
            dt = time.monotonic() - t0
            job = await store.get_job(job["id"]) or {}
            out: dict[str, Any] = {"case": case.name, "kind": case.kind, "job_id": job.get("id"),
                                   "status": job.get("status"), "latency_s": round(dt, 2), "usage": _usage(jd),
                                   "heal_iterations": len(pipe.iteration_logs(job.get("id", "")))}
            pred_spec = None
            if job.get("current_version"):
                pred_spec = pipe.load_spec(job["id"], job["current_version"])
            pred_geo = None
            if (jd / "model.step").exists():
                try:
                    pred_geo = shape_metrics(import_step(str(jd / "model.step")))
                except Exception as exc:  # noqa: BLE001
                    out["step_error"] = str(exc)
            out["geometry"] = geometry_diff(case.gt, pred_geo)
            out["features"] = feature_diff(case.gt_spec, pred_spec) if pred_spec else None
            return out

    variant.results = list(await asyncio.gather(*(one(c) for c in cases)))


def summarise(results: list[dict], price_in: float | None = None, price_out: float | None = None) -> dict[str, Any]:
    if not results:
        return {"cases": 0}
    n = len(results)
    feats = [r["features"] for r in results if r.get("features")]
    tp = sum(f["tp"] for f in feats)
    n_gt = sum(f["gt"] for f in feats)
    n_pred = sum(f["pred"] for f in feats)
    missing_pred = n - len(feats)
    per_type: dict[str, dict[str, int]] = {}
    for f in feats:
        for t, v in f["per_type"].items():
            agg = per_type.setdefault(t, {"gt": 0, "pred": 0, "tp": 0})
            for k in agg:
                agg[k] += v[k]
    lat = [r["latency_s"] for r in results]
    usage = {k: sum(r["usage"][k] for r in results) for k in results[0]["usage"]}
    cost = None
    if price_in is not None and price_out is not None:
        cost = (usage["prompt_tokens"] * price_in + usage["completion_tokens"] * price_out) / 1e6
    geo = [r["geometry"] for r in results]
    vols = [g["volume_error"] for g in geo if g["volume_error"] is not None]
    return {
        "cases": n, "ready": sum(r["status"] == "ready" for r in results), "no_spec": missing_pred,
        "solid_ok": sum(g["solid_ok"] for g in geo), "bbox_ok": sum(g["bbox_ok"] for g in geo),
        "volume_ok": sum(g["volume_ok"] for g in geo),
        "volume_error_mean_pct": round(statistics.mean(vols) * 100, 4) if vols else None,
        "precision": round(tp / n_pred, 4) if n_pred else None, "recall": round(tp / n_gt, 4) if n_gt else None,
        "features_gt": n_gt, "features_pred": n_pred, "features_tp": tp,
        "per_type": {t: {**v, "precision": round(v["tp"] / v["pred"], 3) if v["pred"] else None,
                         "recall": round(v["tp"] / v["gt"], 3) if v["gt"] else None} for t, v in sorted(per_type.items())},
        "latency_mean_s": round(statistics.mean(lat), 2), "latency_median_s": round(statistics.median(lat), 2),
        "usage": usage, "cost": round(cost, 4) if cost is not None else None,
    }


def _pct(a: int, n: int) -> str:
    return f"{a}/{n} ({100 * a / n:.0f} %)" if n else "-"


def render_report(meta: dict, variants: list[Variant], price: tuple[float | None, float | None]) -> tuple[str, dict]:
    js: dict[str, Any] = {"meta": meta, "variants": {}}
    lines = [f"# Round-trip benchmark — {meta['timestamp']}", "",
             f"LLM: `{meta['llm']}` · seed {meta['seed']} · {meta['synthetic']} synthetic part(s) + "
             f"{meta['fixtures']} fixture PDF(s) · tolerances: bbox ±{BBOX_TOL_MM} mm, volume < {VOLUME_TOL * 100:g} %, "
             f"features ±{FEATURE_TOL_MM} mm", ""]
    if meta["llm"] == "oracle":
        lines += ["> **Oracle run**: the mock client replays the ground-truth PartSpec, so this validates the harness "
                  "(drawing → parse → compile → verify → diff), not the model.", ""]
    for section, kind in (("Synthetic EN ISO drawings", "synthetic"), ("Fixture / workshop PDFs", "fixture")):
        lines += [f"## {section}", ""]
        rows = []
        for v in variants:
            summ = summarise([r for r in v.results if r["kind"] == kind], *price)
            js["variants"].setdefault(v.name, {})[kind] = summ
            if summ["cases"]:
                rows.append((v.name, summ))
        if not rows:
            lines += ["_No cases._" if kind == "synthetic" else
                      "_No fixture PDFs with an expected `*.part_spec.json` were found. Add real workshop drawings "
                      "to `backend/tests/fixtures/` (PDF + expected spec) so accuracy is not judged on synthetic "
                      "drawings alone._", ""]
            continue
        lines += ["| Variant | Ready | 1 valid solid | bbox ±0.05 | volume < 0.5 % | Precision | Recall | "
                  "Latency mean / median | Tokens in / out (reasoning) | Cost |",
                  "|---|---|---|---|---|---|---|---|---|---|"]
        for name, sm in rows:
            u = sm["usage"]
            lines.append(
                f"| {name} | {_pct(sm['ready'], sm['cases'])} | {_pct(sm['solid_ok'], sm['cases'])} | "
                f"{_pct(sm['bbox_ok'], sm['cases'])} | {_pct(sm['volume_ok'], sm['cases'])} | "
                f"{sm['precision'] if sm['precision'] is not None else '-'} | {sm['recall'] if sm['recall'] is not None else '-'} | "
                f"{sm['latency_mean_s']} / {sm['latency_median_s']} s | {u['prompt_tokens']:,} / {u['completion_tokens']:,} "
                f"({u['reasoning_tokens']:,}) | {sm['cost'] if sm['cost'] is not None else '-'} |")
        lines.append("")
        for name, sm in rows:
            lines += [f"**{name} — per feature type**", "", "| Type | GT | Pred | TP | Precision | Recall |",
                      "|---|---|---|---|---|---|"]
            for t, v in sm["per_type"].items():
                lines.append(f"| {t} | {v['gt']} | {v['pred']} | {v['tp']} | {v['precision'] if v['precision'] is not None else '-'} | "
                             f"{v['recall'] if v['recall'] is not None else '-'} |")
            lines.append("")
        lines += ["<details><summary>Per case</summary>", "", "| Variant | Case | Status | Latency | bbox err | vol err | "
                  "P / R | Near misses |", "|---|---|---|---|---|---|---|---|"]
        for v in variants:
            for r in (r for r in v.results if r["kind"] == kind):
                g, f = r["geometry"], r.get("features") or {}
                nm = "; ".join(f"{m['type']} {m['gt']}→{m['pred']} Δ{m['center_error_mm']} {m['dims']}"
                               for m in f.get("near_misses", [])[:4])
                lines.append(f"| {v.name} | {r['case']} | {r['status']} | {r['latency_s']} s | "
                             f"{g['bbox_error_mm'] if g['bbox_error_mm'] is not None else '-'} | "
                             f"{g['volume_error'] if g['volume_error'] is not None else '-'} | "
                             f"{round(f['precision'], 3) if f else '-'} / {round(f['recall'], 3) if f else '-'} | {nm} |")
        lines += ["", "</details>", ""]
    js["cases"] = {v.name: v.results for v in variants}
    return "\n".join(lines), js


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(prog="roundtrip_bench", description=__doc__.split("\n\n")[0])
    ap.add_argument("--n", type=int, default=5, help="number of synthetic parts")
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--llm", choices=("oracle", "deepseek"), default="oracle")
    ap.add_argument("--thinking", default="", help="comma list, e.g. enabled,disabled (deepseek only)")
    ap.add_argument("--prompt-variant", action="append", default=[], metavar="NAME=FILE",
                    help="extra extraction instructions from FILE; repeat for A/B (baseline is always included)")
    ap.add_argument("--fixtures", type=Path, default=BACKEND_DIR / "tests" / "fixtures")
    ap.add_argument("--no-fixtures", action="store_true")
    ap.add_argument("--no-critic", action="store_true", help="skip the visual critic (faster)")
    ap.add_argument("--concurrency", type=int, default=1)
    ap.add_argument("--price-in", type=float, default=None, help="USD per 1M prompt tokens")
    ap.add_argument("--price-out", type=float, default=None, help="USD per 1M completion tokens")
    ap.add_argument("--out", type=Path, default=None, help="default data/benchmarks/<timestamp>")
    return ap.parse_args(argv)


async def run(args: argparse.Namespace, base: Settings | None = None) -> Path:
    base = base or Settings()
    ts = time.strftime("%Y%m%d-%H%M%S")
    root = args.out or base.data_dir / "benchmarks" / ts
    root.mkdir(parents=True, exist_ok=True)
    oracle = root / "oracle_specs"
    oracle.mkdir(exist_ok=True)
    specs = generate_specs(args.n, args.seed, base) if args.n > 0 else []
    cases = prepare_synthetic(specs, root, oracle)
    if not args.no_fixtures and args.fixtures.exists():
        cases += prepare_fixtures(args.fixtures, oracle)
    thinking = [t.strip() for t in args.thinking.split(",") if t.strip()] if args.llm == "deepseek" else []
    prompts: list[tuple[str, str]] = [("base", "")]
    for pv in args.prompt_variant:
        name, _, path = pv.partition("=")
        prompts.append((name.strip(), Path(path).read_text(encoding="utf-8")))
    variants = [Variant(f"{p}" + (f"-thinking-{t}" if t else ""), t or None, text)
                for p, text in prompts for t in (thinking or [""])]
    for v in variants:
        print(f"[bench] variant {v.name}: {len(cases)} case(s)", flush=True)
        await run_variant(v, cases, base, args.llm, oracle, root, args.concurrency,
                          critic=False if args.no_critic else None)
    meta = {"timestamp": ts, "llm": args.llm, "seed": args.seed, "synthetic": len(specs),
            "fixtures": sum(c.kind == "fixture" for c in cases), "args": {k: str(v) for k, v in vars(args).items()},
            "vision_model": base.deepseek_vision_model if args.llm == "deepseek" else "mock"}
    md, js = render_report(meta, variants, (args.price_in, args.price_out))
    (root / "report.md").write_text(md, encoding="utf-8")
    (root / "report.json").write_text(json.dumps(js, indent=1, default=str), encoding="utf-8")
    print(f"[bench] report: {root / 'report.md'}", flush=True)
    return root


def main(argv: list[str] | None = None) -> int:
    asyncio.run(run(parse_args(argv)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
