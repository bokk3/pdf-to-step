"""Op 2 softjaw generator (SPEC_V2_0_MULTISIDE_FIXTURING.md §3.2). Deterministic build123d / OCCT, no LLM.

Jaw frame: X along the jaws (jaw width, symmetric about X = 0), Y = clamping direction (fixed jaw at -Y, movable
jaw at +Y, inner faces at +-gap/2), Z up with Z = 0 on the jaw tops.

1. The finished part is flipped for Op 2 (180° about X or Y, optional 90° about Z), centred in XY and lowered so
   its lowest point sits ``grip_depth`` below the jaw tops.
2. Undercut-safe cavity: the slice of the part inside z in [-grip, 0] is cross-sectioned at every vertex level
   (+ midpoints and a 1 mm grid), the sections are united into the outer XY silhouette (holes filled), offset by
   the slip-fit clearance and extruded straight up from -grip to +10 mm. Anything the part has inside the slice
   therefore always has a vertical way out: no side pocket or undercut can lock it in the jaws.
3. Dogbones: at every convex silhouette corner with an interior angle < 135° a relief cylinder (dogbone radius)
   is centred on the corner, pushed 0.25 r outward along the bisector, so the jaw pocket radius (milled with an
   end mill <= 2 r) never touches the sharp part corner.
4. Jaw blanks from vises.json incl. the bolt clearance holes and counterbores (from the clamping face); the cavity
   is cut from both jaws.
5. Safety checks (blocking): >= 3 mm between cavity floor and counterbore, >= 5 mm jaw wall behind the pocket,
   part protrudes above the jaws, part wider than the jaw gap, vise opening, both jaws stay one solid.
   Warning: each jaw grips >= 25 % of the part's side length (part not too narrow / too wide for the vise).
"""

from __future__ import annotations

import json
import math
from pathlib import Path

import numpy as np

from app.schemas.softjaw import SoftjawCheck, SoftjawReport, SoftjawRequest, ViseLibrary, ViseSpec

VISES_FILE = "vises.json"
EMERALD = (30, 180, 90)
JAW_GRAY = (150, 158, 168)
PART_BLUE = (90, 150, 235)
CAVITY_TOP = 10.0  # cavity cutter extends this far above the jaw tops
MIN_CBORE_WALL = 3.0
MIN_OUTER_WALL = 5.0
MIN_CONTACT = 0.25
DOGBONE_MAX_ANGLE = 135.0


def load_vises(s) -> ViseLibrary:
    from app.services.shop_config import load_config

    return load_config(s, VISES_FILE, ViseLibrary)


def save_vises(s, lib: ViseLibrary) -> None:
    from app.services.shop_config import save_config

    save_config(s, VISES_FILE, lib)


# --------------------------------------------------------------------------- pose


def pose_part(shape, req: SoftjawRequest):
    """Op 2 pose in the jaw frame -> (posed shape, build123d Location applied)."""
    from build123d import Location, Pos, Rot

    rot = Rot(180, 0, 0) if req.op2_flip_axis == "X" else Rot(0, 180, 0)
    loc = Rot(0, 0, req.z_rotation_deg) * rot
    moved = shape.moved(loc)
    bb = moved.bounding_box()
    shift = Pos(-(bb.min.X + bb.max.X) / 2, -(bb.min.Y + bb.max.Y) / 2, -req.grip_depth_mm - bb.min.Z)
    loc = shift * loc
    return shape.moved(loc), Location(loc)


# --------------------------------------------------------------------------- silhouette


def _faces(r) -> list:
    if r is None:
        return []
    items = r if isinstance(r, list) else [r]
    return [f for it in items for f in it.faces()]


def grip_silhouette(posed, grip: float) -> list:
    """Outer XY silhouette (faces at z = 0, holes filled) of the part inside z in [-grip, 0]."""
    from build123d import Align, Box, Face, Pos, Rectangle

    bb = posed.bounding_box()
    big = 4 * max(bb.size.X, bb.size.Y) + 20
    slab = Pos(0, 0, -grip) * Box(big, big, grip, align=(Align.CENTER, Align.CENTER, Align.MIN))
    from build123d import Compound

    parts = _solids(posed.intersect(slab))
    if not parts:
        raise ValueError("het werkstuk zit niet in de klemzone (grip_depth)")
    sl = parts[0] if len(parts) == 1 else Compound(parts)
    zs = sorted({round(v.Z, 6) for v in sl.vertices() if -grip - 1e-6 <= v.Z <= 1e-6} | {-grip, 0.0})
    zs = sorted(set(zs) | set(np.arange(-grip, 0, 1.0).round(6)))
    samples = set()
    for a, b in zip(zs, zs[1:]):
        if b - a < 2e-3:
            continue
        samples |= {a + 1e-3, (a + b) / 2, b - 1e-3}
    sections = []
    for z in sorted(samples):
        cut = _faces(sl.intersect(Pos(0, 0, z) * Rectangle(big, big)))
        sections += [Face(f.outer_wire()).moved(Pos(0, 0, -z)) for f in cut if f.area > 1e-6]
    if not sections:
        raise ValueError("het werkstuk zit niet in de klemzone (grip_depth)")
    merged = sections[0].fuse(*sections[1:]) if len(sections) > 1 else sections[0]
    outer = [Face(f.outer_wire()) for f in _faces(merged)]
    merged = outer[0].fuse(*outer[1:]) if len(outer) > 1 else outer[0]
    return [Face(f.outer_wire()) for f in _faces(merged)]


def _segments(wire, per_edge: int = 48) -> np.ndarray:
    """Wire as line segments (k, 2, 2), sampled per edge incl. the exact end points (order independent)."""
    segs = []
    for e in wire.edges():
        pts = [e.position_at(t) for t in np.linspace(0, 1, per_edge + 1)]
        segs += [((a.X, a.Y), (b.X, b.Y)) for a, b in zip(pts, pts[1:])]
    return np.array(segs, dtype=float)


def _inside(segs: np.ndarray, x: float, y: float) -> bool:
    """Even-odd ray casting against a segment soup."""
    (x1, y1), (x2, y2) = segs[:, 0].T, segs[:, 1].T
    cross = (y1 > y) != (y2 > y)
    xs = x1 + (y - y1) * (x2 - x1) / np.where(y2 - y1 == 0, 1e-300, y2 - y1)
    return bool(np.count_nonzero(cross & (x < xs)) % 2)

def convex_corners(face, max_angle_deg: float = DOGBONE_MAX_ANGLE) -> list[tuple[float, float, float, float]]:
    """(x, y, bx, by) of every convex corner sharper than ``max_angle_deg``; (bx, by) = inward unit bisector."""
    wire = face.outer_wire()
    segs = _segments(wire)
    span = max(np.ptp(segs[:, :, 0]), np.ptp(segs[:, :, 1]))
    out = []
    for v in wire.vertices():
        p = np.array([v.X, v.Y])
        dirs = []
        for e in wire.edges():
            a, b = e.position_at(0), e.position_at(1)
            if math.hypot(a.X - p[0], a.Y - p[1]) < 1e-6:
                t = e.tangent_at(0)
                dirs.append(np.array([t.X, t.Y]))
            elif math.hypot(b.X - p[0], b.Y - p[1]) < 1e-6:
                t = e.tangent_at(1)
                dirs.append(-np.array([t.X, t.Y]))
        if len(dirs) != 2:
            continue
        d1, d2 = (d / max(np.linalg.norm(d), 1e-12) for d in dirs)
        theta = math.degrees(math.acos(max(-1.0, min(1.0, float(d1 @ d2)))))
        if theta >= max_angle_deg - 1e-6:
            continue
        bis = d1 + d2
        bis /= max(np.linalg.norm(bis), 1e-12)
        probe = p + bis * min(0.05, 0.01 * span)
        if _inside(segs, *probe):  # interior angle = theta (< 180): convex part corner
            out.append((float(p[0]), float(p[1]), float(bis[0]), float(bis[1])))
    return out


def cavity_cutter(silhouette: list, req: SoftjawRequest):
    """Extruded, offset silhouette + dogbone cylinders (jaw frame) -> (cutter, dogbone centres)."""
    from build123d import Align, Cylinder, Kind, Pos, extrude, offset

    grip, r = req.grip_depth_mm, req.dogbone_radius_mm
    tools, bones = [], []
    for face in silhouette:
        prof = offset(face, amount=req.xy_clearance_mm, kind=Kind.ARC) if req.xy_clearance_mm > 0 else face
        tools.append(extrude(Pos(0, 0, -grip) * prof, amount=grip + CAVITY_TOP))
        for x, y, bx, by in convex_corners(face):
            cx, cy = x - 0.25 * r * bx, y - 0.25 * r * by  # pushed outward (away from the part)
            bones.append((round(cx, 4), round(cy, 4)))
            tools.append(Pos(cx, cy, -grip) * Cylinder(r, grip + CAVITY_TOP, align=(Align.CENTER, Align.CENTER, Align.MIN)))
    cutter = tools[0].fuse(*tools[1:]) if len(tools) > 1 else tools[0]
    return cutter, bones


# --------------------------------------------------------------------------- jaws


def jaw_blanks(v: ViseSpec, gap: float):
    """(fixed, movable) jaw solids with bolt clearance holes + counterbores from the clamping faces."""
    from build123d import Align, Box, Cylinder, Pos, Rot

    W, D, H = v.jaw_width_mm, v.jaw_depth_mm, v.jaw_height_mm
    zb = -H + v.bolt_height_from_bottom_mm
    out = []
    for side in (-1, 1):  # -1 fixed (Y-), +1 movable (Y+)
        y_in = side * gap / 2
        jaw = Pos(0, y_in + side * D / 2, -H / 2) * Box(W, D, H)
        for bx in (-v.bolt_spacing_mm / 2, v.bolt_spacing_mm / 2):
            # cylinders along Y: Rot(90,0,0) turns +Z into -Y
            axis = Rot(-90 * side, 0, 0)  # +Z -> +side*Y (into the jaw from the clamping face)
            clear = Pos(bx, y_in - side * 1.0, zb) * axis * Cylinder(v.bolt_clearance_dia_mm / 2, D + 2,
                                                                  align=(Align.CENTER, Align.CENTER, Align.MIN))
            cb = Pos(bx, y_in - side * 1.0, zb) * axis * Cylinder(v.bolt_cbore_dia_mm / 2, v.bolt_cbore_depth_mm + 1,
                                                               align=(Align.CENTER, Align.CENTER, Align.MIN))
            jaw = jaw.cut(clear, cb)
        out.append(jaw)
    return out[0], out[1]


def _solids(r) -> list:
    if r is None:
        return []
    items = r if isinstance(r, list) else [r]
    return [s for it in items for s in it.solids()]


def _pocket_faces(jaw, cutter) -> list:
    """Faces of the cut jaw that lie on the cavity cutter (the mating pocket, coloured emerald for CAM)."""
    from build123d import Vector

    out = []
    for f in jaw.faces():
        p = f.position_at(0.5, 0.5)
        try:
            on = cutter.is_inside(Vector(p.X, p.Y, p.Z), tolerance=1e-3)
        except TypeError:
            on = cutter.is_inside(Vector(p.X, p.Y, p.Z))
        if on:
            out.append(f)
    return out


def recommend_tool(s, req: SoftjawRequest) -> dict | None:
    """Largest in-magazine flat / bull end mill that fits the dogbones (Ø <= 2 r) and reaches the grip depth."""
    try:
        from app.services.tool_library import load_tools

        lib = load_tools(s)
    except Exception:  # noqa: BLE001
        return None
    group = "aluminum" if req.jaw_material == "aluminum_6082" else "steel"
    cands = [t for t in lib.tools if t.in_magazine and t.tool_type in ("flat_endmill", "bull_endmill")
             and t.diameter_mm <= 2 * req.dogbone_radius_mm + 1e-6 and t.flute_length_mm >= req.grip_depth_mm + 1
             and (not t.materials or group in t.materials)]
    if not cands:
        return None
    t = max(cands, key=lambda t: (t.diameter_mm, -t.flute_length_mm))
    return {"tool_id": t.id, "tool_name": t.name, "diameter_mm": t.diameter_mm, "flute_length_mm": t.flute_length_mm}


# --------------------------------------------------------------------------- main entry


def generate_softjaws(shape, req: SoftjawRequest, vise: ViseSpec, out_dir: Path, job_id: str, version: int,
                      settings=None) -> SoftjawReport:
    gap = vise.default_jaw_gap_mm if req.jaw_gap_mm is None else req.jaw_gap_mm
    grip = req.grip_depth_mm
    posed, loc = pose_part(shape, req)
    pb = posed.bounding_box()
    part_env = (round(pb.size.X, 4), round(pb.size.Y, 4), round(pb.size.Z, 4))
    checks: list[SoftjawCheck] = []

    def check(code, ok, blocking, msg, value=None, limit=None):
        checks.append(SoftjawCheck(code=code, passed=bool(ok), blocking=blocking, message=msg,
                                   value_mm=None if value is None else round(float(value), 4),
                                   limit_mm=None if limit is None else round(float(limit), 4)))

    check("PART_PROTRUDES", pb.size.Z - grip >= 1.0, True,
          f"Werkstuk steekt {pb.size.Z - grip:.2f} mm boven de bekken uit (min 1 mm voor Op 2).", pb.size.Z - grip, 1.0)
    check("PART_WIDER_THAN_GAP", pb.size.Y > gap + 2 * 1.0, True,
          f"Werkstukbreedte in klemrichting {pb.size.Y:.2f} mm tegen bekopening {gap:.2f} mm.", pb.size.Y, gap + 2.0)
    opening = gap + 2 * vise.jaw_depth_mm
    check("VISE_OPENING", opening <= vise.max_opening_mm, True,
          f"Benodigde bankschroefopening {opening:.1f} mm (max {vise.max_opening_mm:g}).", opening, vise.max_opening_mm)

    silhouette = grip_silhouette(posed, grip)
    cutter, bones = cavity_cutter(silhouette, req)
    cb = cutter.bounding_box()

    zb = -vise.jaw_height_mm + vise.bolt_height_from_bottom_mm
    cbore_top = zb + vise.bolt_cbore_dia_mm / 2
    cb_x = [(sx * vise.bolt_spacing_mm / 2 - vise.bolt_cbore_dia_mm / 2, sx * vise.bolt_spacing_mm / 2 + vise.bolt_cbore_dia_mm / 2)
            for sx in (-1, 1)]
    overlaps = any(a < cb.max.X and b > cb.min.X for a, b in cb_x)
    wall = -grip - cbore_top
    check("CBORE_WALL", not overlaps or wall >= MIN_CBORE_WALL - 1e-9, True,
          f"Wand tussen holtebodem (Z{-grip:g}) en boutverzinking (Z{cbore_top:g}) is {wall:.2f} mm"
          + ("" if overlaps else " (holte ligt niet boven de bouten)") + f"; minimaal {MIN_CBORE_WALL:g} mm.",
          wall, MIN_CBORE_WALL)
    reach = max(cb.max.Y - gap / 2, -gap / 2 - cb.min.Y)
    outer = vise.jaw_depth_mm - reach
    check("OUTER_WALL", outer >= MIN_OUTER_WALL - 1e-9, True,
          f"Bekwand achter de holte {outer:.2f} mm (min {MIN_OUTER_WALL:g} mm)"
          + (f"; bekopening >= {gap + 2 * (MIN_OUTER_WALL - outer):.1f} mm nodig." if outer < MIN_OUTER_WALL else "."),
          outer, MIN_OUTER_WALL)
    contact = min(pb.size.X, vise.jaw_width_mm)
    ratio = min(contact / pb.size.X, contact / vise.jaw_width_mm)
    check("JAW_CONTACT", ratio >= MIN_CONTACT, False,
          f"Klemlengte {contact:.1f} mm = {ratio * 100:.0f} % (werkstuk {pb.size.X:.1f} mm, bek {vise.jaw_width_mm:g} mm;"
          f" min {MIN_CONTACT * 100:.0f} %).", contact)

    fixed0, movable0 = jaw_blanks(vise, gap)
    fixed, movable = fixed0.cut(cutter), movable0.cut(cutter)
    fs, ms = _solids(fixed), _solids(movable)
    check("JAWS_INTACT", len(fs) == 1 and len(ms) == 1, True,
          f"Bekken na het uitfrezen: vast {len(fs)} / beweeglijk {len(ms)} solid(s) (moet 1 / 1 zijn).")

    passed = all(c.passed for c in checks if c.blocking)
    out_dir.mkdir(parents=True, exist_ok=True)
    report = SoftjawReport(
        job_id=job_id, version=version, request=req, passed=passed, checks=checks,
        jaw_blank_mm=(vise.jaw_width_mm, vise.jaw_depth_mm, vise.jaw_height_mm), part_envelope_mm=part_env,
        cavity_depth_y_mm=round(reach, 4), dogbones=bones,
        part_offset_mm=(round(loc.position.X, 4), round(loc.position.Y, 4), round(loc.position.Z, 4)),
        recommended_tool=recommend_tool(settings, req) if settings is not None else None,
    )
    if passed:
        from app.services.glb_scene import Layer, export_layers_glb
        from app.services.step_xcaf_exporter import export_bodies_step

        fixed, movable = fs[0], ms[0]
        step = export_bodies_step(
            [("Fixed_Jaw", fixed, JAW_GRAY, [(f, EMERALD) for f in _pocket_faces(fixed, cutter)]),
             ("Movable_Jaw", movable, JAW_GRAY, [(f, EMERALD) for f in _pocket_faces(movable, cutter)])],
            out_dir / "softjaws.step")
        glb = export_layers_glb([Layer("Fixed_Jaw", [fixed], JAW_GRAY), Layer("Movable_Jaw", [movable], JAW_GRAY),
                                 Layer("Workpiece", [posed], PART_BLUE, 0.35)], out_dir / "softjaws_assembly.glb")
        report.artifacts = {"softjaws.step": step.name, "softjaws_assembly.glb": glb.name}
    (out_dir / "softjaw_report.json").write_text(report.model_dump_json(indent=1), encoding="utf-8")
    report.artifacts["softjaw_report.json"] = "softjaw_report.json"
    return report


def interference_when_lifting(shape, req: SoftjawRequest, jaws: list, step_mm: float = 0.5) -> float:
    """Max overlap volume (mm³) between the posed part and the jaws while lifting it out (tests / checks)."""
    from build123d import Pos

    posed, _ = pose_part(shape, req)
    worst = 0.0
    for dz in np.arange(0.0, req.grip_depth_mm + 1.0 + 1e-9, step_mm):
        moved = Pos(0, 0, float(dz)) * posed
        for j in jaws:
            worst = max(worst, sum(float(s.volume) for s in _solids(moved.intersect(j)) if s.volume > 0))
    return worst


def report_json(report: SoftjawReport) -> dict:
    return json.loads(report.model_dump_json())


def cad_softjaws(job_dir: str, version: int, request_json: str, settings_data: dict) -> dict:
    """Process-pool entry (POST /api/jobs/{id}/softjaws): jaws for the built STEP of ``version``."""
    from build123d import import_step

    from app.config import Settings

    s = Settings(**settings_data)
    req = SoftjawRequest.model_validate_json(request_json)
    vise = load_vises(s).get(req.vise_id)
    jd = Path(job_dir)
    shape = import_step(str(jd / "versions" / f"v{version}" / "model.step"))
    solids = shape.solids()
    shape = solids[0] if len(solids) == 1 else shape
    return report_json(generate_softjaws(shape, req, vise, jd / "softjaws", jd.name, version, s))