"""Direct STEP intake (DRAWING_SPEC.md §3 route B): B-Rep in, no vision, no LLM.

The uploaded solid stays the geometry of record. It is moved to the PartSpec datum (bottom-left of the top view,
Z0 on the top face) and analysed into a *documentation* PartSpec: stock, rounded corners, top chamfer, holes
(through / blind / counterbore / countersink / drill tip, on any axis-aligned face) and pocket / slot / step floors
seen from the top or bottom. That spec drives the DFM rules, the tool checks and the dimensions on the EN ISO
drawing. Recompiling it and comparing the volume with the real solid tells whether it describes the part
completely (``complete``): only then may an operator edit rebuild the part from the spec.

Threads are not part of a B-Rep (they are modelled as the tap drill or the nominal bore), so tap-drill diameters
are listed as questions rather than guessed.
"""

from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from app.schemas.part_spec import METRIC_TAP_DRILL, PartSpec

TOL = 1e-3
VOLUME_MATCH = 1e-3  # 0.1 %: recognised spec rebuilds the same solid


@dataclass
class StepAnalysis:
    spec: PartSpec
    offset: tuple[float, float, float]  # translation applied to the upload (datum move)
    complete: bool
    volume_step: float
    volume_spec: float | None
    notes: list[str] = field(default_factory=list)

    def summary(self) -> dict:
        return {"offset_mm": [round(v, 4) for v in self.offset], "complete": self.complete,
                "volume_step_mm3": round(self.volume_step, 3),
                "volume_spec_mm3": round(self.volume_spec, 3) if self.volume_spec is not None else None,
                "notes": self.notes}


def load_step(path: Path):
    from build123d import import_step

    shape = import_step(str(path))
    if not shape.solids():
        raise ValueError("STEP bevat geen solid (alleen oppervlakken / draadmodel?)")
    return shape


def to_datum(shape):
    """Move so that x∈[0,L], y∈[0,W], z∈[-T,0] (PartSpec default datum)."""
    from build123d import Location

    bb = shape.bounding_box()
    off = (-bb.min.X, -bb.min.Y, -bb.max.Z)
    return shape.moved(Location(off)), off


# --------------------------------------------------------------------------- #
# Face analysis (OCP)
# --------------------------------------------------------------------------- #


@dataclass
class _Cyl:
    face: object
    axis: np.ndarray  # unit
    loc: np.ndarray
    r: float
    span: float  # angular span (rad)
    lo: float  # extent along the axis direction (min / max of the face along axis)
    hi: float
    concave: bool


def _surface(face):
    from OCP.BRepAdaptor import BRepAdaptor_Surface

    return BRepAdaptor_Surface(face.wrapped)


def _normal_mid(face) -> tuple[np.ndarray, np.ndarray]:
    from OCP.BRepLProp import BRepLProp_SLProps
    from OCP.TopAbs import TopAbs_REVERSED

    ad = _surface(face)
    u = (ad.FirstUParameter() + ad.LastUParameter()) / 2
    v = (ad.FirstVParameter() + ad.LastVParameter()) / 2
    props = BRepLProp_SLProps(ad, u, v, 1, 1e-6)
    p, n = props.Value(), props.Normal()
    nv = np.array([n.X(), n.Y(), n.Z()])
    if face.wrapped.Orientation() == TopAbs_REVERSED:
        nv = -nv
    return np.array([p.X(), p.Y(), p.Z()]), nv


def _axis_extent(face, axis: np.ndarray) -> tuple[float, float]:
    bb = face.bounding_box()
    corners = np.array([[x, y, z] for x in (bb.min.X, bb.max.X) for y in (bb.min.Y, bb.max.Y) for z in (bb.min.Z, bb.max.Z)])
    k = int(np.argmax(np.abs(axis)))
    return float(corners[:, k].min()), float(corners[:, k].max())


def _cylinders(shape) -> list[_Cyl]:
    from OCP.GeomAbs import GeomAbs_Cylinder

    out = []
    for f in shape.faces():
        ad = _surface(f)
        if ad.GetType() != GeomAbs_Cylinder:
            continue
        c = ad.Cylinder()
        ax = c.Axis()
        d = np.array([ax.Direction().X(), ax.Direction().Y(), ax.Direction().Z()])
        if np.max(np.abs(d)) < 1 - 1e-6:  # inclined axis: not recognised
            out.append(_Cyl(f, d, np.zeros(3), c.Radius(), 0.0, 0.0, 0.0, False))
            continue
        k = int(np.argmax(np.abs(d)))
        d = np.eye(3)[k]
        loc = np.array([ax.Location().X(), ax.Location().Y(), ax.Location().Z()])
        p, n = _normal_mid(f)
        radial = (p - loc) - np.dot(p - loc, d) * d
        lo, hi = _axis_extent(f, d)
        out.append(_Cyl(f, d, loc, c.Radius(), ad.LastUParameter() - ad.FirstUParameter(), lo, hi,
                        float(np.dot(n, radial)) < 0))
    return out


def _cones(shape) -> list[dict]:
    from OCP.GeomAbs import GeomAbs_Cone

    out = []
    for f in shape.faces():
        ad = _surface(f)
        if ad.GetType() != GeomAbs_Cone:
            continue
        c = ad.Cone()
        ax = c.Axis()
        d = np.array([ax.Direction().X(), ax.Direction().Y(), ax.Direction().Z()])
        if np.max(np.abs(d)) < 1 - 1e-6:
            continue
        k = int(np.argmax(np.abs(d)))
        loc = np.array([ax.Location().X(), ax.Location().Y(), ax.Location().Z()])
        lo, hi = _axis_extent(f, np.eye(3)[k])
        bb = f.bounding_box()
        size = sorted([bb.max.X - bb.min.X, bb.max.Y - bb.min.Y, bb.max.Z - bb.min.Z])
        out.append({"k": k, "loc": loc, "angle": math.degrees(2 * abs(c.SemiAngle())), "lo": lo, "hi": hi,
                    "dmax": size[-1] if k == 2 else max(size[1], size[2]), "face": f})
    return out


# axis index -> (face at max end, face at min end); face coordinates (u, v) per entry face
FACE_OF = {2: ("top", "bottom"), 1: ("back", "front"), 0: ("right", "left")}
UV = {"top": (0, 1), "bottom": (0, 1), "front": (0, 2), "back": (0, 2), "left": (1, 2), "right": (1, 2)}


def _key(v: float) -> float:
    return round(v, 3) + 0.0


# --------------------------------------------------------------------------- #
# Recognition
# --------------------------------------------------------------------------- #


def recognize(shape, name: str = "step_part") -> tuple[dict, list[str]]:
    """PartSpec dict (documentation) + notes. `shape` must already be at the datum."""
    from OCP.BRepAdaptor import BRepAdaptor_Curve
    from OCP.GeomAbs import GeomAbs_Line, GeomAbs_Plane

    bb = shape.bounding_box()
    lim = {0: (bb.min.X, bb.max.X), 1: (bb.min.Y, bb.max.Y), 2: (bb.min.Z, bb.max.Z)}
    L, W, T = bb.max.X - bb.min.X, bb.max.Y - bb.min.Y, bb.max.Z - bb.min.Z
    notes: list[str] = []
    cyls = _cylinders(shape)
    cones = _cones(shape)
    inclined = [c for c in cyls if c.span == 0.0 and c.lo == c.hi]
    if inclined:
        notes.append(f"{len(inclined)} cilindervlak(ken) met schuine as: niet herkend (zie 3D-model).")

    # -- outer profile: 4 equal convex vertical full-height cylinders in the corners
    profile: dict = {"type": "rectangle"}
    corner = [c for c in cyls if c.axis[2] and not c.concave and c.hi - c.lo > T * 0.5]
    if len(corner) == 4 and max(c.r for c in corner) - min(c.r for c in corner) < TOL:
        r = corner[0].r
        want = sorted([(_key(x), _key(y)) for x in (r, L - r) for y in (r, W - r)])
        got = sorted((_key(c.loc[0]), _key(c.loc[1])) for c in corner)
        if all(abs(a[0] - b[0]) < 0.01 and abs(a[1] - b[1]) < 0.01 for a, b in zip(want, got)):
            profile = {"type": "rounded_rectangle", "corner_radius": round(r, 4)}
    elif any(c.axis[2] and not c.concave for c in cyls):
        notes.append("Buitencontour is niet (afgerond) rechthoekig: als rechthoek gedocumenteerd, zie 3D-model.")

    # -- holes: coaxial concave full cylinders, grouped per axis line
    groups: dict[tuple, list[_Cyl]] = defaultdict(list)
    partial: list[_Cyl] = []
    by_axis: dict[tuple, list[_Cyl]] = defaultdict(list)
    for c in cyls:
        if not c.concave or (c.span == 0.0 and c.lo == c.hi):
            continue
        k = int(np.argmax(np.abs(c.axis)))
        other = [i for i in range(3) if i != k]
        by_axis[(k, _key(c.loc[other[0]]), _key(c.loc[other[1]]), _key(c.r))].append(c)
    for key, faces in by_axis.items():
        span = sum(f.span for f in faces)
        if span >= 2 * math.pi - 1e-3:
            k, a, b, r = key
            groups[(k, a, b)].append(faces[0] if len(faces) == 1 else _merge(faces))
        else:
            partial += faces
    holes: list[dict] = []
    for (k, a, b), stack in groups.items():
        stack.sort(key=lambda c: c.r)
        bore = stack[0]
        lo_lim, hi_lim = lim[k]
        other = [i for i in range(3) if i != k]
        csk = [cn for cn in cones if cn["k"] == k and abs(cn["loc"][other[0]] - a) < TOL and abs(cn["loc"][other[1]] - b) < TOL]
        his = [c.hi for c in stack] + [cn["hi"] for cn in csk]
        los = [c.lo for c in stack] + [cn["lo"] for cn in csk]
        # an end is open when the axis continues into air (stock face, or the floor of a pocket the hole starts in)
        top_entry = abs(max(his) - hi_lim) < TOL or _open(shape, k, other, a, b, max(his) + 0.02)
        bottom_entry = abs(min(los) - lo_lim) < TOL or _open(shape, k, other, a, b, min(los) - 0.02)
        if not (top_entry or bottom_entry):
            notes.append(f"Inwendige cilinder Ø{2 * bore.r:g} raakt geen buitenvlak: niet herkend.")
            continue
        face = FACE_OF[k][0] if top_entry else FACE_OF[k][1]
        # through: the bore itself reaches the far side (seats / countersinks sit at the entry)
        through = top_entry and bottom_entry
        entry = hi_lim if top_entry else lo_lim
        depth = (entry - bore.lo) if top_entry else (bore.hi - entry)
        h: dict = {"type": "hole", "face": face, "diameter": round(2 * bore.r, 4)}
        p = np.zeros(3)
        p[other[0]], p[other[1]] = a, b
        u_i, v_i = UV[face]
        h["center_x"], h["center_y"] = round(float(p[u_i]), 4), round(float(p[v_i]), 4)
        seats = [c for c in stack[1:] if (abs(c.hi - entry) < TOL if top_entry else abs(c.lo - entry) < TOL)]
        entry_cone = [cn for cn in csk if (abs(cn["hi"] - entry) < TOL if top_entry else abs(cn["lo"] - entry) < TOL)]
        if seats:
            cb = seats[0]
            h["counterbore"] = {"diameter": round(2 * cb.r, 4), "depth": round(cb.hi - cb.lo, 4)}
        elif entry_cone:
            h["countersink"] = {"diameter": round(entry_cone[0]["dmax"], 4), "angle_deg": round(entry_cone[0]["angle"], 3)}
        if through:
            h["through_all"] = True
        else:
            h["depth"] = round(depth, 4)
            tip = [cn for cn in csk if cn not in entry_cone]
            h["tip_angle_deg"] = round(tip[0]["angle"], 3) if tip else None
        d = h["diameter"]
        if not through and h.get("tip_angle_deg") is None and d >= 8 and not seats and not entry_cone:
            h = {"type": "pocket_circle", "face": face, "center_x": h["center_x"], "center_y": h["center_y"],
                 "diameter": d, "depth": h["depth"]}  # flat-bottomed blind bore >= Ø8: milled, not drilled
            holes.append(h)
            continue
        thread = next((f"M{m:g}" for m, td in METRIC_TAP_DRILL.items() if abs(td - d) < 0.01), None)
        if thread:
            notes.append(f"Boring Ø{d:g} op {face} ({h['center_x']:g}, {h['center_y']:g}) = kernboor {thread}: "
                         f"vul 'thread' in als het schroefdraad is (STEP bevat geen draad).")
        holes.append(h)

    # group equal holes into one callout
    feats: list[dict] = []
    grouped: dict[tuple, list[dict]] = defaultdict(list)
    for h in holes:
        sig = tuple((k, str(v)) for k, v in sorted(h.items()) if k not in ("center_x", "center_y"))
        grouped[sig].append(h)
    for i, hs in enumerate(grouped.values(), start=1):
        hs.sort(key=lambda h: (h["center_y"], h["center_x"]))
        f = dict(hs[0], id=f"{'C' if hs[0]['type'] == 'pocket_circle' else 'H'}{i}", confidence=1.0, source_note="STEP")
        f["extra_positions"] = [[h["center_x"], h["center_y"]] for h in hs[1:]]
        feats.append(f)

    # -- floors seen from top / bottom: pockets, slots, steps
    n_pocket = 0
    hole_axes = {(round(h["center_x"], 3), round(h["center_y"], 3)) for h in holes if h["face"] in ("top", "bottom")}
    for f in shape.faces():
        ad = _surface(f)
        if ad.GetType() != GeomAbs_Plane:
            continue
        p, n = _normal_mid(f)
        if abs(abs(n[2]) - 1) > 1e-6:
            if abs(n[2]) > 1e-6:
                _maybe_chamfer(f, n, lim, notes)
            continue
        fb = f.bounding_box()
        z = fb.min.Z
        if z > lim[2][1] - TOL or z < lim[2][0] + TOL:
            continue  # top / bottom stock faces
        up = n[2] > 0
        face = "top" if up else "bottom"
        depth = (lim[2][1] - z) if up else (z - lim[2][0])
        ow = f.outer_wire()
        x0, y0, x1, y1 = ow.bounding_box().min.X, ow.bounding_box().min.Y, ow.bounding_box().max.X, ow.bounding_box().max.Y
        w, h_ = x1 - x0, y1 - y0
        cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
        if (round(cx, 3), round(cy, 3)) in hole_axes and abs(w - h_) < TOL:
            continue  # counterbore seat / blind-hole bottom
        from build123d import Face

        area = Face(ow).area
        inside = [c for c in partial if c.axis[2] and x0 - TOL <= c.loc[0] <= x1 + TOL and y0 - TOL <= c.loc[1] <= y1 + TOL]
        r = min((c.r for c in inside), default=0.0)
        open_sides = sum([x0 < lim[0][0] + TOL, x1 > lim[0][1] - TOL, y0 < lim[1][0] + TOL, y1 > lim[1][1] - TOL])
        lines_only = all(BRepAdaptor_Curve(e.wrapped).GetType() == GeomAbs_Line for e in ow.edges())
        n_pocket += 1
        fid = f"P{n_pocket}"
        base = {"id": fid, "face": face, "confidence": 1.0, "source_note": "STEP"}
        if open_sides:
            feats.append({**base, "type": "step_cut", "x_min": round(x0, 4), "x_max": round(x1, 4), "y_min": round(y0, 4),
                          "y_max": round(y1, 4), "depth": round(depth, 4), "corner_radius": round(r, 4)})
        elif abs(w - h_) < TOL and abs(area - math.pi * w * w / 4) < max(0.01 * area, 0.05):
            feats.append({**base, "type": "pocket_circle", "center_x": round(cx, 4), "center_y": round(cy, 4),
                          "diameter": round(w, 4), "depth": round(depth, 4)})
        elif abs(r - min(w, h_) / 2) < TOL and abs(area - ((max(w, h_) - min(w, h_)) * min(w, h_)
                                                           + math.pi * min(w, h_) ** 2 / 4)) < max(0.002 * area, 0.05):
            sw = min(w, h_)
            half = (max(w, h_) - sw) / 2
            dx, dy = (half, 0.0) if w >= h_ else (0.0, half)
            feats.append({**base, "type": "slot", "start_x": round(cx - dx, 4), "start_y": round(cy - dy, 4),
                          "end_x": round(cx + dx, 4), "end_y": round(cy + dy, 4), "width": round(sw, 4), "depth": round(depth, 4)})
        elif abs(area - (w * h_ - (4 - math.pi) * r * r)) < max(0.002 * area, 0.05) and (r > 0 or lines_only):
            feats.append({**base, "type": "pocket_rect", "center_x": round(cx, 4), "center_y": round(cy, 4),
                          "length_x": round(w, 4), "width_y": round(h_, 4), "corner_radius": round(r, 4), "depth": round(depth, 4)})
        elif lines_only:
            pts = [[round(v.X, 4), round(v.Y, 4)] for v in ow.vertices()]
            feats.append({**base, "type": "pocket_polygon", "points_2d": pts, "corner_radius": 0.0, "depth": round(depth, 4)})
        else:
            n_pocket -= 1
            notes.append(f"Bodem op {face} (diepte {depth:g} mm, {w:g} x {h_:g}) met vrije vorm: niet herkend.")
    spec = {"schema_version": "1.1", "input_source": "step", "part_name": name,
            "stock": {"length_x": round(L, 4), "width_y": round(W, 4), "thickness_z": round(T, 4), "confidence": 1.0},
            "outer_profile": {**profile, "confidence": 1.0}, "features": feats, "edge_treatments": []}
    ch = [n_ for n_ in notes if n_.startswith("__chamfer__")]
    notes = [n_ for n_ in notes if not n_.startswith("__chamfer__")]
    sizes = sorted({float(c.split(":")[1]) for c in ch})
    if len(ch) >= 4 and len(sizes) == 1:
        spec["edge_treatments"] = [{"id": "E1", "type": "chamfer", "size": sizes[0], "target": "top_outer_edges",
                                    "confidence": 1.0}]
    elif ch:
        notes.append("Schuine vlakken gevonden die geen rondom-afschuining van de bovenkant vormen: niet herkend.")
    return spec, notes


def _open(shape, k: int, other: list[int], a: float, b: float, t: float) -> bool:
    """Is the point on the axis line (other coords a, b) at axis coordinate t outside the material?"""
    from OCP.BRepClass3d import BRepClass3d_SolidClassifier
    from OCP.gp import gp_Pnt
    from OCP.TopAbs import TopAbs_OUT

    p = [0.0, 0.0, 0.0]
    p[k], p[other[0]], p[other[1]] = t, a, b
    return all(BRepClass3d_SolidClassifier(s.wrapped, gp_Pnt(*p), 1e-6).State() == TopAbs_OUT for s in shape.solids())


def _merge(faces: list[_Cyl]) -> _Cyl:
    f0 = faces[0]
    return _Cyl(f0.face, f0.axis, f0.loc, f0.r, sum(f.span for f in faces), min(f.lo for f in faces),
                max(f.hi for f in faces), f0.concave)


def _maybe_chamfer(face, n: np.ndarray, lim: dict, notes: list[str]) -> None:
    """45° planar faces along the top outer edge -> chamfer candidates (collected via a marker note)."""
    bb = face.bounding_box()
    if abs(bb.max.Z - lim[2][1]) > TOL or n[2] <= 0:
        return
    horiz = math.hypot(n[0], n[1])
    if abs(math.degrees(math.atan2(horiz, n[2])) - 45) > 0.5:
        return
    size = round(bb.max.Z - bb.min.Z, 4)
    on_edge = (bb.min.X < lim[0][0] + TOL or bb.max.X > lim[0][1] - TOL or bb.min.Y < lim[1][0] + TOL
               or bb.max.Y > lim[1][1] - TOL)
    if on_edge:
        notes.append(f"__chamfer__:{size}")


def analyze(path: Path, name: str = "step_part") -> tuple[object, StepAnalysis]:
    """Load, move to the datum, recognise and check completeness. Returns (datum shape, analysis)."""
    from app.services.cad_compiler import compile_part

    shape, off = to_datum(load_step(path))
    spec_d, notes = recognize(shape, name)
    vol = float(shape.volume)
    if len(shape.solids()) != 1:
        notes.append(f"STEP bevat {len(shape.solids())} solids: CNC-onderdeel verwacht één solid.")
    v_spec = None
    complete = False
    try:
        spec = PartSpec.model_validate(spec_d)
        res = compile_part(spec)
        if all(s.ok for s in res.feature_status):
            v_spec = float(res.shape.volume)
            complete = abs(v_spec - vol) / vol < VOLUME_MATCH
    except Exception as exc:  # noqa: BLE001
        notes.append(f"Herkende features bouwen geen geldig model: {exc}")
        spec_d["features"] = [f for f in spec_d["features"] if f["type"] == "hole"]
        spec = PartSpec.model_validate(spec_d)
    if not complete:
        notes.append("De herkende PartSpec beschrijft het STEP-model niet volledig: het STEP blijft de geometrie; "
                     "'Herbereken STEP' is uitgeschakeld voor deze job.")
    spec = spec.model_copy(update={"ambiguities": notes})
    return shape, StepAnalysis(spec, off, complete, vol, v_spec, notes)
