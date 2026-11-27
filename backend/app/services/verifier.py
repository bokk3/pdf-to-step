"""Stage 4 - geometry gatekeeper, CNC-DFM rules (§8), CAM face colours, GLB + orthographic renders.

Gate (job may only become ``ready`` when all pass):
  8.1 manifold integrity  - BRepCheck valid and exactly one solid
  8.2 dimensional check   - bounding box == stock (+ bosses) within ±0.01 mm,
                            unless a step_cut intentionally reduces the envelope
  plus: every feature compiled.
Warnings (reported, never blocking):
  8.3 internal corner radius < 1.5 mm or sharp      8.4 hole depth > 5×D, pocket depth > 4×R
  8.5 wall to stock edge < 1.5 mm (+ thin webs / floors)
8.6 CAM colour coding: threaded bores blue, tight-tolerance (H7 …) bores red, via STEP XCAF.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from app.config import Settings
from app.schemas.part_spec import (
    BossCircle,
    BossRect,
    Complex3D,
    DFMIssue,
    DFMReport,
    FeatureBuildStatus,
    GeometryCheck,
    Hole,
    PartSpec,
    PocketCircle,
    PocketPolygon,
    PocketRect,
    Severity,
    Slot,
    StepCut,
    parse_metric_thread,
)

DENSITY_G_CM3 = {
    "6082": 2.70, "6061": 2.70, "almgsi": 2.70, "7075": 2.81, "alzn": 2.81, "5083": 2.66, "almg": 2.66,
    "alu": 2.70, "aluminium": 2.70, "aluminum": 2.70,
    "1.4301": 7.90, "304": 7.90, "1.4404": 8.00, "316": 8.00, "rvs": 7.95, "stainless": 8.00,
    "s235": 7.85, "s355": 7.85, "c45": 7.85, "42crmo4": 7.85, "staal": 7.85, "steel": 7.85,
    "messing": 8.50, "brass": 8.50, "cuzn": 8.50, "brons": 8.80, "bronze": 8.80, "koper": 8.96, "copper": 8.96,
    "titanium": 4.43, "ti6al4v": 4.43, "pom": 1.41, "delrin": 1.41, "peek": 1.32, "pa6": 1.14, "nylon": 1.14,
    "pe-hd": 0.95, "hdpe": 0.95, "ptfe": 2.20, "pvc": 1.40,
}

THREAD_RGB = (0.10, 0.35, 1.00)  # blue
TIGHT_RGB = (0.90, 0.10, 0.10)  # red
FIT_RE = re.compile(r"^\s*([A-Za-z]{1,2})\s*(\d{1,2})\s*$")
NUM_RE = re.compile(r"[-+±]?\s*\d+(?:[.,]\d+)?")


# --------------------------------------------------------------------------- #
# Tolerance helpers
# --------------------------------------------------------------------------- #


def tolerance_band(tol: str | None) -> float | None:
    """Band width in mm for numeric tolerances ('±0.02', '+0.015/0', '+0.02/-0.01'); None otherwise."""
    if not tol or FIT_RE.match(tol):
        return None
    nums = [float(n.replace(" ", "").replace(",", ".").replace("±", "")) for n in NUM_RE.findall(tol)]
    if not nums:
        return None
    if "±" in tol:
        return 2 * abs(nums[0])
    return abs(max(nums + [0.0]) - min(nums + [0.0]))


FIT_ANY_RE = re.compile(r"(?<![A-Za-z])([A-HJ-NP-Za-hj-np-z]{1,2})\s*(\d{1,2})(?![\d.,])")


def is_tight(tol: str | None) -> bool:
    """True for ISO fits IT9 and finer ('H7', 'H7 (+0.015/0)', 'g6') or numeric bands <= 0.05 mm."""
    if not tol:
        return False
    m = FIT_ANY_RE.search(tol)
    if m and int(m.group(2)) <= 9:
        return True
    band = tolerance_band(re.sub(FIT_ANY_RE, "", tol))
    return band is not None and band <= 0.05


# --------------------------------------------------------------------------- #
# Deterministic callout coverage (text layer vs. PartSpec)
# --------------------------------------------------------------------------- #

CALLOUT_DIA = re.compile(r"(?:(\d+)\s*[xX×]\s*)?[Ø⌀∅]\s*(\d+(?:[.,]\d+)?)")
CALLOUT_THREAD = re.compile(r"(?:(\d+)\s*[xX×]\s*)?\bM\s*(\d+(?:[.,]\d+)?)(?:\s*[xX×]\s*\d+(?:[.,]\d+)?)?")
CALLOUT_RADIUS = re.compile(r"(?<![A-Za-z])R\s*(\d+(?:[.,]\d+)?)")


def _num(s: str) -> float:
    return float(s.replace(",", "."))


def callout_coverage(spec: PartSpec, texts: list[str]) -> list[DFMIssue]:
    """Every Ø / M / R callout on the sheet should map to something in the PartSpec (and 'Nx' to N instances)."""
    dia_counts: dict[float, int] = {}
    threads: dict[float, int] = {}
    radii: set[float] = {round(spec.outer_profile.corner_radius, 3)}
    radii |= {round(e.size, 3) for e in spec.edge_treatments if e.type == "fillet"}
    for f in spec.features:
        n = len(f.positions()) if hasattr(f, "positions") else 1
        dias: set[float] = set()
        if isinstance(f, Hole):
            dias |= {f.diameter, f.drill_diameter()}
            dias |= {f.counterbore.diameter} if f.counterbore else set()
            dias |= {f.countersink.diameter} if f.countersink else set()
            t = parse_metric_thread(f.thread)
            if t:
                threads[t[0]] = threads.get(t[0], 0) + n
        elif isinstance(f, (PocketCircle, BossCircle)):
            dias.add(f.diameter)
            radii.add(round(f.diameter / 2, 3))
        if isinstance(f, (PocketRect, PocketPolygon, StepCut, BossRect)):
            radii.add(round(f.corner_radius, 3))
        if isinstance(f, Slot):
            radii.add(round(f.width / 2, 3))
        for d in dias:
            dia_counts[round(d, 3)] = dia_counts.get(round(d, 3), 0) + n

    issues: list[DFMIssue] = []
    for text in texts:
        for m in CALLOUT_THREAD.finditer(text):
            size, count = _num(m.group(2)), int(m.group(1) or 1)
            have = threads.get(size, 0)
            if have < count:
                issues.append(DFMIssue(code="CALLOUT_UNMATCHED", severity=Severity.warning,
                                       message=f"Tekening: '{text.strip()}' -> {count}x M{size:g} verwacht, {have} gemodelleerd.",
                                       suggestion="Controleer of alle draadgaten (ook in zij-aanzichten) in de PartSpec staan."))
        for m in CALLOUT_DIA.finditer(text):
            d, count = round(_num(m.group(2)), 3), int(m.group(1) or 1)
            have = max((c for k, c in dia_counts.items() if abs(k - d) <= 0.01), default=0)
            if have < count:
                issues.append(DFMIssue(code="CALLOUT_UNMATCHED", severity=Severity.warning,
                                       message=f"Tekening: '{text.strip()}' -> {count}x Ø{d:g} verwacht, {have} gemodelleerd.",
                                       suggestion="Ontbrekende of verkeerd gemaatvoerde boring/kamer?"))
        for m in CALLOUT_RADIUS.finditer(text):
            r = round(_num(m.group(1)), 3)
            if r > 0 and not any(abs(r - k) <= 0.01 for k in radii):
                issues.append(DFMIssue(code="CALLOUT_UNMATCHED", severity=Severity.warning,
                                       message=f"Tekening: '{text.strip()}' -> R{r:g} komt in geen enkele feature voor."))
    seen, out = set(), []
    for i in issues:
        if i.message not in seen:
            seen.add(i.message)
            out.append(i)
    return out


def drawing_callout_texts(job_dir: Path) -> list[str]:
    """Text-layer spans outside the title block (from extraction/)."""
    import json

    spans_p, summ_p = job_dir / "extraction" / "text_spans.json", job_dir / "extraction" / "summary.json"
    if not spans_p.exists():
        return []
    spans = json.loads(spans_p.read_text(encoding="utf-8"))
    tb = json.loads(summ_p.read_text(encoding="utf-8")).get("title_block_mm") if summ_p.exists() else None
    out = []
    for s in spans:
        x0, y0, x1, y1 = s["bbox_mm"]
        if tb and x0 >= tb[0] - 1 and y0 >= tb[1] - 1 and x1 <= tb[2] + 1 and y1 <= tb[3] + 1:
            continue
        out.append(s["text"])
    return out


# --------------------------------------------------------------------------- #
# 8.1 / 8.2 geometry gate
# --------------------------------------------------------------------------- #

_FACE_AXIS = {"top": (2, 1), "bottom": (2, -1), "back": (1, 1), "front": (1, -1), "right": (0, 1), "left": (0, -1)}


def expected_bbox(spec: PartSpec) -> tuple[np.ndarray, np.ndarray]:
    lo, hi = (np.array(v, dtype=float) for v in spec.stock_bbox())
    for f in spec.features:
        if isinstance(f, (BossRect, BossCircle)):
            axis, sign = _FACE_AXIS[f.face]
            if sign > 0:
                hi[axis] = max(hi[axis], spec.stock_bbox()[1][axis] + f.height)
            else:
                lo[axis] = min(lo[axis], spec.stock_bbox()[0][axis] - f.height)
    return lo, hi


def _is_valid(shape) -> bool:
    v = shape.is_valid
    return bool(v() if callable(v) else v)


def check_geometry(shape, spec: PartSpec, tol_mm: float = 0.01) -> tuple[GeometryCheck, list[DFMIssue]]:
    issues: list[DFMIssue] = []
    bb = shape.bounding_box()
    got_lo = np.array([bb.min.X, bb.min.Y, bb.min.Z])
    got_hi = np.array([bb.max.X, bb.max.Y, bb.max.Z])
    exp_lo, exp_hi = expected_bbox(spec)
    dev = float(max(np.max(np.abs(got_lo - exp_lo)), np.max(np.abs(got_hi - exp_hi))))
    grew = bool(np.any(got_lo < exp_lo - tol_mm) or np.any(got_hi > exp_hi + tol_mm))
    has_step = any(isinstance(f, StepCut) for f in spec.features)
    valid = _is_valid(shape)
    n_solids = len(shape.solids())
    volume = float(shape.volume)
    geo = GeometryCheck(
        is_valid=valid,
        solid_count=n_solids,
        volume_mm3=volume,
        bbox_min_mm=tuple(float(x) for x in got_lo),  # type: ignore[arg-type]
        bbox_max_mm=tuple(float(x) for x in got_hi),  # type: ignore[arg-type]
        expected_size_mm=tuple(float(x) for x in exp_hi - exp_lo),  # type: ignore[arg-type]
        bbox_matches_spec=dev <= tol_mm,
        bbox_deviation_mm=dev,
    )
    if not valid:
        issues.append(DFMIssue(code="GEOM_INVALID", severity=Severity.error,
                               message="B-Rep faalt de OCCT-validatie (BRepCheck): geen geldige solid."))
    if n_solids != 1:
        issues.append(DFMIssue(code="GEOM_SOLID_COUNT", severity=Severity.error,
                               message=f"Verwacht precies 1 solid, gevonden: {n_solids}.",
                               suggestion="Een bewerking snijdt het werkstuk in stukken; controleer posities/dieptes."))
    if volume <= 0:
        issues.append(DFMIssue(code="GEOM_ZERO_VOLUME", severity=Severity.error, message="Solid heeft geen volume."))
    if dev > tol_mm:
        size, exp = got_hi - got_lo, exp_hi - exp_lo
        msg = (f"Omhullende {_fmt(size)} mm wijkt {dev:.3f} mm af van de stock {_fmt(exp)} mm "
               f"(tolerantie ±{tol_mm} mm).")
        if not grew and has_step:
            issues.append(DFMIssue(code="GEOM_BBOX_REDUCED_BY_STEP", severity=Severity.info, message=msg,
                                   suggestion="Omhullende verkleind door een step_cut (bedoeld)."))
        else:
            issues.append(DFMIssue(code="GEOM_BBOX_MISMATCH", severity=Severity.error, message=msg,
                                   suggestion="Controleer stockmaten en features die buiten/over de rand gaan."))
    return geo, issues


def _fmt(v) -> str:
    return " × ".join(f"{x:.3f}" for x in v)


# --------------------------------------------------------------------------- #
# 8.3 - 8.5 DFM rules
# --------------------------------------------------------------------------- #


@dataclass
class _Footprint:
    fid: str
    face: str
    kind: str  # circle | rect
    cx: float
    cy: float
    hx: float  # radius for circles
    hy: float
    depth: float | None  # None = through


def _face_extent(spec: PartSpec, face: str) -> tuple[tuple[float, float], tuple[float, float], float]:
    (x0, y0, z0), (x1, y1, z1) = spec.stock_bbox()
    if face in ("top", "bottom"):
        return (x0, x1), (y0, y1), z1 - z0
    if face in ("front", "back"):
        return (x0, x1), (z0, z1), y1 - y0
    return (y0, y1), (z0, z1), x1 - x0


def _wall(fp: _Footprint, spec: PartSpec) -> float:
    (u0, u1), (v0, v1), _ = _face_extent(spec, fp.face)
    return min(fp.cx - fp.hx - u0, u1 - fp.cx - fp.hx, fp.cy - fp.hy - v0, v1 - fp.cy - fp.hy)


def _gap(a: _Footprint, b: _Footprint) -> float:
    if a.kind == "circle" and b.kind == "circle":
        return math.hypot(a.cx - b.cx, a.cy - b.cy) - a.hx - b.hx
    du = abs(a.cx - b.cx) - a.hx - b.hx
    dv = abs(a.cy - b.cy) - a.hy - b.hy
    return math.hypot(du, dv) if du > 0 and dv > 0 else max(du, dv)


def _rot_half(length: float, width: float, rot_deg: float) -> tuple[float, float]:
    a = math.radians(rot_deg)
    c, s = abs(math.cos(a)), abs(math.sin(a))
    return (length * c + width * s) / 2, (length * s + width * c) / 2


def _inside_corners(spec: PartSpec, face: str, xa: float, xb: float, ya: float, yb: float) -> int:
    (u0, u1), (v0, v1), _ = _face_extent(spec, face)
    return sum(1 for x in (xa, xb) for y in (ya, yb) if u0 + 1e-6 < x < u1 - 1e-6 and v0 + 1e-6 < y < v1 - 1e-6)


def dfm_rules(spec: PartSpec, s: Settings) -> list[DFMIssue]:
    issues: list[DFMIssue] = []
    fps: list[_Footprint] = []
    min_r, wall_min = s.dfm_min_corner_radius_mm, s.dfm_min_wall_mm

    def corner_rule(fid: str, radius: float, depth: float, corners: int) -> None:
        if corners == 0:
            return
        if radius <= 1e-9:
            issues.append(DFMIssue(
                code="SHARP_INTERNAL_CORNER", severity=Severity.warning, feature_id=fid,
                message="Scherpe interne hoek (R0): niet freesbaar met een vingerfrees.",
                suggestion=f"Gebruik een hoekradius ≥ {min_r} mm (frees Ø{2 * min_r:g}) of dogbone-uitsparingen."))
        elif radius < min_r - 1e-9:
            issues.append(DFMIssue(
                code="SMALL_CORNER_RADIUS", severity=Severity.warning, feature_id=fid,
                message=f"Interne hoekradius R{radius:g} < {min_r} mm: vraagt een frees ≤ Ø{2 * radius:g}.",
                suggestion=f"Vergroot naar ≥ R{min_r} mm of voorzie dogbones."))
        if radius > 1e-9 and depth > s.dfm_max_pocket_depth_per_radius * radius:
            issues.append(DFMIssue(
                code="DEEP_POCKET", severity=Severity.warning, feature_id=fid,
                message=f"Diepte {depth:g} mm > {s.dfm_max_pocket_depth_per_radius:g} × hoekradius R{radius:g}: "
                        "lange, slanke frees nodig (trillingen).",
                suggestion=f"Vergroot de hoekradius tot ≥ R{depth / s.dfm_max_pocket_depth_per_radius:.1f} of verminder de diepte."))

    for f in spec.features:
        if isinstance(f, (BossRect, BossCircle)):
            continue
        if isinstance(f, Complex3D):
            issues.append(DFMIssue(code="COMPLEX_3D", severity=Severity.info, feature_id=f.id,
                                   message="Niet-prismatische feature uit AI-code: mogelijk 3+2/5-assig; extra controleren."))
            continue
        thick = _face_extent(spec, f.face)[2]
        depth = thick if getattr(f, "through_all", False) else (getattr(f, "depth", None) or thick)
        through = getattr(f, "through_all", False)

        if isinstance(f, Hole):
            d = f.drill_diameter()
            if depth > s.dfm_max_hole_depth_ratio * d:
                issues.append(DFMIssue(
                    code="DEEP_HOLE", severity=Severity.warning, feature_id=f.id,
                    message=f"Boordiepte {depth:g} mm > {s.dfm_max_hole_depth_ratio:g} × Ø{d:g}.",
                    suggestion="Pikboren / kanonboor, of de diepte verkleinen."))
            if f.thread:
                issues.append(DFMIssue(code="THREAD", severity=Severity.info, feature_id=f.id,
                                       message=f"Draad {f.thread}: gemodelleerd op kernboor Ø{d:g} (blauw in STEP)."))
                if f.thread_depth and not through and f.depth and f.thread_depth > f.depth - 0.25 * f.diameter:
                    issues.append(DFMIssue(code="THREAD_RUNOUT", severity=Severity.warning, feature_id=f.id,
                                           message="Blinde draad loopt tot (bijna) de boorbodem: geen uitloop voor de tap.",
                                           suggestion="Boor dieper dan de bruikbare draadlengte."))
            if is_tight(f.tolerance):
                issues.append(DFMIssue(code="TIGHT_TOLERANCE", severity=Severity.info, feature_id=f.id,
                                       message=f"Nauwe tolerantie {f.tolerance}: ruimen/kotteren (rood in STEP)."))
            r = max((f.counterbore.diameter if f.counterbore else 0), (f.countersink.diameter if f.countersink else 0),
                    d) / 2
            fps += [_Footprint(f.id, f.face, "circle", x, y, r, r, None if through else depth) for x, y in f.positions()]

        elif isinstance(f, PocketRect):
            hx, hy = _rot_half(f.length_x, f.width_y, f.rotation_deg)
            for x, y in f.positions():
                fp = _Footprint(f.id, f.face, "rect", x, y, hx, hy, None if through else depth)
                fps.append(fp)
            x, y = f.positions()[0]
            corners = 4 if abs(f.rotation_deg) % 90 else _inside_corners(spec, f.face, x - hx, x + hx, y - hy, y + hy)
            corner_rule(f.id, f.corner_radius, depth, corners)

        elif isinstance(f, PocketCircle):
            r = f.diameter / 2
            fps += [_Footprint(f.id, f.face, "circle", x, y, r, r, None if through else depth) for x, y in f.positions()]
            if depth > s.dfm_max_pocket_depth_per_radius * r:
                issues.append(DFMIssue(code="DEEP_POCKET", severity=Severity.warning, feature_id=f.id,
                                       message=f"Diepte {depth:g} mm > {s.dfm_max_pocket_depth_per_radius:g} × R{r:g}."))

        elif isinstance(f, PocketPolygon):
            xs, ys = [p[0] for p in f.points_2d], [p[1] for p in f.points_2d]
            fps.append(_Footprint(f.id, f.face, "rect", (min(xs) + max(xs)) / 2, (min(ys) + max(ys)) / 2,
                                  (max(xs) - min(xs)) / 2, (max(ys) - min(ys)) / 2, None if through else depth))
            corner_rule(f.id, f.corner_radius, depth, len(f.points_2d))

        elif isinstance(f, Slot):
            r = f.width / 2
            if depth > s.dfm_max_pocket_depth_per_radius * r:
                issues.append(DFMIssue(code="DEEP_SLOT", severity=Severity.warning, feature_id=f.id,
                                       message=f"Sleufdiepte {depth:g} mm > {s.dfm_max_pocket_depth_per_radius:g} × R{r:g}."))
            angle = math.degrees(math.atan2(f.end_y - f.start_y, f.end_x - f.start_x))
            hx, hy = _rot_half(f.length + f.width, f.width, angle)
            fps.append(_Footprint(f.id, f.face, "rect", (f.start_x + f.end_x) / 2, (f.start_y + f.end_y) / 2,
                                  hx, hy, None if through else depth))

        elif isinstance(f, StepCut):
            corner_rule(f.id, f.corner_radius, f.depth,
                        _inside_corners(spec, f.face, f.x_min, f.x_max, f.y_min, f.y_max))
            if thick - f.depth < wall_min:
                issues.append(DFMIssue(code="THIN_FLOOR", severity=Severity.warning, feature_id=f.id,
                                       message=f"Restdikte onder step_cut {thick - f.depth:.2f} mm < {wall_min} mm."))

    # 8.5 wall thickness to the stock edge (open cuts that break out on purpose are skipped)
    for fp in fps:
        wall = _wall(fp, spec)
        if wall < -1e-6:
            if fp.kind == "circle":
                issues.append(DFMIssue(code="BREAKOUT", severity=Severity.warning, feature_id=fp.fid,
                                       message="Boring breekt door de stockrand.",
                                       suggestion="Controleer de positie tegen de tekening."))
            continue
        if wall < wall_min:
            issues.append(DFMIssue(code="THIN_WALL", severity=Severity.warning, feature_id=fp.fid,
                                   message=f"Wand tot stockrand {wall:.2f} mm < {wall_min} mm.",
                                   suggestion="Dunne wanden buigen door en gaan trillen; maak de wand dikker."))
        if fp.depth is not None:
            floor = _face_extent(spec, fp.face)[2] - fp.depth
            if 0 < floor < wall_min:
                issues.append(DFMIssue(code="THIN_FLOOR", severity=Severity.warning, feature_id=fp.fid,
                                       message=f"Restbodem {floor:.2f} mm < {wall_min} mm."))
    for i, a in enumerate(fps):
        for b in fps[i + 1:]:
            if a.face != b.face:
                continue
            gap = _gap(a, b)
            if 0 <= gap < wall_min and not (a.fid == b.fid and a.kind == "rect"):
                issues.append(DFMIssue(code="THIN_WEB", severity=Severity.warning, feature_id=a.fid,
                                       message=f"Lijf tussen {a.fid} en {b.fid} is {gap:.2f} mm < {wall_min} mm."))

    seen, unique = set(), []
    for it in issues:
        key = (it.code, it.feature_id, it.message)
        if key not in seen:
            seen.add(key)
            unique.append(it)
    return unique


def estimate_mass_g(spec: PartSpec, volume_mm3: float) -> float | None:
    m = (spec.material or "").lower().replace(" ", "")
    for key, rho in DENSITY_G_CM3.items():
        if key in m:
            return volume_mm3 / 1000.0 * rho
    return None


def build_report(spec: PartSpec, version: int, shape, statuses: list[FeatureBuildStatus], s: Settings,
                 colored_faces: dict[str, int] | None = None, callout_texts: list[str] | None = None) -> DFMReport:
    geo, geo_issues = check_geometry(shape, spec, tol_mm=s.dfm_bbox_tolerance_mm)
    issues = geo_issues + dfm_rules(spec, s) + (callout_coverage(spec, callout_texts) if callout_texts else [])
    for st in statuses:
        if not st.ok:
            issues.insert(0, DFMIssue(code="FEATURE_FAILED", severity=Severity.error, feature_id=st.feature_id,
                                      message=f"Feature kon niet gebouwd worden: {st.error}"))
    stats = {"volume_mm3": geo.volume_mm3, "feature_count": float(len(spec.features))}
    shop = shopfloor_checks(spec, s, geo)
    issues += shop.get("issues", [])
    mass = estimate_mass_g(spec, geo.volume_mm3)
    rec = shop.get("stock")
    if mass is None and rec is not None and rec.part_mass_kg is not None:
        mass = rec.part_mass_kg * 1000.0
    if mass is not None:
        stats["mass_g"] = mass
    return DFMReport(
        spec_version=version,
        passed=not any(i.severity == Severity.error for i in issues),
        geometry=geo,
        issues=issues,
        feature_status=statuses,
        review_items=spec.review_items(),
        colored_faces=colored_faces or {},
        stats=stats,
        material_group=shop.get("group"),
        tool_checks=shop.get("checks", []),
        recommended_tools=shop.get("assign", []),
        stock_recommendation=rec,
    )


def shopfloor_checks(spec: PartSpec, s: Settings, geo: GeometryCheck | None) -> dict:
    """v1.5 DFM v2 (workshop tool library) + raw-stock advice. Never raises: a broken config only logs an info."""
    from app.services.stock_catalog import load_stock, solve_stock
    from app.services.tool_library import load_tools, material_group, tool_checks

    out: dict = {"group": material_group(spec.material)}
    try:
        if s.dfm_tool_checks:
            out["checks"], out["assign"], out["issues"] = tool_checks(spec, load_tools(s), out["group"])
        if s.stock_advisor and geo is not None:
            size = tuple(float(b - a) for a, b in zip(geo.bbox_min_mm, geo.bbox_max_mm))
            out["stock"] = solve_stock(size, geo.volume_mm3, load_stock(s), out["group"])  # type: ignore[arg-type]
    except Exception as exc:  # noqa: BLE001
        out.setdefault("issues", []).append(DFMIssue(code="SHOPFLOOR_CHECK_FAILED", severity=Severity.info,
                                                     message=f"Gereedschap-/voorraadcontrole mislukt: {exc}"))
    return out


# --------------------------------------------------------------------------- #
# 8.6 CAM face colours
# --------------------------------------------------------------------------- #


def _cylinder_faces(shape) -> list[tuple[object, float, np.ndarray, np.ndarray]]:
    from OCP.BRepAdaptor import BRepAdaptor_Surface
    from OCP.GeomAbs import GeomAbs_Cylinder

    out = []
    for face in shape.faces():
        ad = BRepAdaptor_Surface(face.wrapped)
        if ad.GetType() != GeomAbs_Cylinder:
            continue
        cyl = ad.Cylinder()
        ax = cyl.Axis()
        loc, d = ax.Location(), ax.Direction()
        out.append((face, cyl.Radius(), np.array([loc.X(), loc.Y(), loc.Z()]), np.array([d.X(), d.Y(), d.Z()])))
    return out


def cam_face_colors(shape, spec: PartSpec, palette=None) -> tuple[dict[tuple[float, float, float], list], dict[str, int]]:
    """CAM face colours ({rgb: faces}, counts per category) with the v1.5 palette (step_xcaf_exporter)."""
    from app.services.shop_config import DEFAULTS_DIR
    from app.services.step_xcaf_exporter import CamPalette, classify_faces

    pal = palette or CamPalette.model_validate_json((DEFAULTS_DIR / "cam_colors.json").read_text(encoding="utf-8"))
    col = classify_faces(shape, spec, pal)
    return col.by_rgb(), col.counts


def _legacy_cam_face_colors(shape, spec: PartSpec) -> tuple[dict[tuple[float, float, float], list], dict[str, int]]:
    """v1.0: bore faces of threaded holes (blue) and tight-tolerance holes (red). Kept for reference."""
    from app.services.cad_compiler import face_frames

    frames = face_frames(spec)
    cyls = _cylinder_faces(shape)
    colors: dict[tuple[float, float, float], list] = {}
    counts = {"thread": 0, "tight_tolerance": 0}
    for f in spec.features:
        if not isinstance(f, Hole) or not (f.thread or is_tight(f.tolerance)):
            continue
        rgb, key = (THREAD_RGB, "thread") if f.thread else (TIGHT_RGB, "tight_tolerance")
        r = f.drill_diameter() / 2
        fr = frames[f.face]
        n = np.array([fr.normal.X, fr.normal.Y, fr.normal.Z])
        for x, y in f.positions():
            p = fr.origin + fr.u_dir * x + fr.v_dir * y
            P = np.array([p.X, p.Y, p.Z])
            for face, radius, loc, d in cyls:
                if abs(radius - r) > 1e-4 or abs(abs(float(np.dot(d, n))) - 1) > 1e-6:
                    continue
                off = P - loc
                if np.linalg.norm(off - np.dot(off, d) * d) < 1e-4:
                    colors.setdefault(rgb, []).append(face)
                    counts[key] += 1
    return colors, counts


# --------------------------------------------------------------------------- #
# GLB + orthographic renders
# --------------------------------------------------------------------------- #


def export_glb(shape, path: Path, tolerance: float = 0.02, angular_tolerance: float = 0.15, coloring=None) -> Path:
    """Tessellate the B-Rep and write a Y-up glTF binary (the viewer rotates back to Z-up).

    With ``coloring`` (step_xcaf_exporter.FaceColoring) every face carries the same CAM colour as in model.step
    (vertex colours, COLOR_0); other faces get the body colour (SPEC_V1_5 §2.2 preview sync)."""
    import trimesh

    tol = max(tolerance, shape.bounding_box().diagonal / 4000.0)
    # Tessellate face by face and keep vertices un-merged between B-Rep faces: normals stay smooth on
    # cylinders but sharp on every CAD edge (merging would smear the shading across edges).
    colored = coloring.face_rgb() if coloring is not None else []
    body = coloring.body_rgb if coloring is not None else None
    parts = []
    for face in shape.faces():
        verts, tris = face.tessellate(tol, angular_tolerance)
        if not tris:
            continue
        m = trimesh.Trimesh(vertices=np.array([[p.X, p.Y, p.Z] for p in verts], dtype=np.float64),
                            faces=np.array(tris, dtype=np.int64), process=False)
        m.merge_vertices()  # within one face only
        if body is not None:
            rgb = next((c for f, c in colored if f.IsSame(face.wrapped)), body)
            m.visual.vertex_colors = np.tile(np.array([*rgb, 255], dtype=np.uint8), (len(m.vertices), 1))
        parts.append(m)
    mesh = trimesh.util.concatenate(parts)  # triangle winding already follows each face's orientation
    mesh.apply_transform(np.array([[1, 0, 0, 0], [0, 0, 1, 0], [0, -1, 0, 0], [0, 0, 0, 1]], dtype=float))
    _ = mesh.vertex_normals  # computed per face-group, exported as NORMAL
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(trimesh.exchange.gltf.export_glb(trimesh.Scene(mesh), include_normals=True))
    return path


VIEWS = {  # name: (direction from centre to camera, up)
    "top": ((0, 0, 1), (0, 1, 0)),
    "front": ((0, -1, 0), (0, 0, 1)),
    "right": ((1, 0, 0), (0, 0, 1)),
    "iso": ((1, -1, 1), (0, 0, 1)),
}


def _polyline(edge) -> np.ndarray:
    gt = edge.geom_type
    gt = gt() if callable(gt) else gt
    n = 2 if getattr(gt, "name", str(gt)) == "LINE" else 40
    pts = [edge @ t for t in np.linspace(0.0, 1.0, n)]
    return np.array([[p.X, p.Y] for p in pts])


def render_orthographic(shape, out_dir: Path, title: str = "") -> dict[str, Path]:
    """Hidden-line Top/Front/Right/Iso views at one common scale + composite ``ortho_check.png``."""
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    out_dir.mkdir(parents=True, exist_ok=True)
    bb = shape.bounding_box()
    c = bb.center()
    dist = bb.diagonal * 3 + 10
    lines: dict[str, tuple[list[np.ndarray], list[np.ndarray]]] = {}
    for name, (d, up) in VIEWS.items():
        dv = np.array(d, dtype=float) / np.linalg.norm(d)
        try:
            vis, hid = shape.project_to_viewport((c.X + dv[0] * dist, c.Y + dv[1] * dist, c.Z + dv[2] * dist),
                                                 up, (c.X, c.Y, c.Z))
            lines[name] = ([_polyline(e) for e in vis], [_polyline(e) for e in hid])
        except Exception:  # noqa: BLE001 - HLR may fail on odd geometry; leave the view empty
            lines[name] = ([], [])

    half = 1.0
    for vis, hid in lines.values():
        for pl in vis + hid:
            if len(pl):
                half = max(half, float(np.max(np.abs(pl))))
    half *= 1.08

    def draw(ax, name: str) -> None:
        vis, hid = lines[name]
        for pl in hid:
            ax.plot(pl[:, 0], pl[:, 1], color="#8a94a6", lw=0.6, ls=(0, (4, 3)))
        for pl in vis:
            ax.plot(pl[:, 0], pl[:, 1], color="#111827", lw=1.1)
        ax.set_xlim(-half, half)
        ax.set_ylim(-half, half)
        ax.set_aspect("equal")
        ax.axis("off")
        ax.set_title(name.upper(), fontsize=9, color="#374151")

    paths: dict[str, Path] = {}
    for name in VIEWS:
        fig, ax = plt.subplots(figsize=(5, 5), dpi=150)
        draw(ax, name)
        fig.tight_layout()
        p = out_dir / f"ortho_{name}.png"
        fig.savefig(p, facecolor="white")
        plt.close(fig)
        paths[name] = p

    fig, axes = plt.subplots(2, 2, figsize=(9, 9), dpi=140)
    for ax, name in zip(axes.flat, ["front", "right", "top", "iso"]):
        draw(ax, name)
    if title:
        fig.suptitle(title, fontsize=11)
    fig.tight_layout()
    sheet = out_dir / "ortho_check.png"
    fig.savefig(sheet, facecolor="white")
    plt.close(fig)
    paths["check"] = sheet
    return paths
