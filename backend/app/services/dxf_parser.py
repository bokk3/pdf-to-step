"""Layered 2D DXF -> PartSpec, 100 % deterministic (DRAWING_SPEC.md §3 route B). Zero AI calls.

Layer convention (case-insensitive, decimal point or comma):

    CONTOUR_T<thickness>        outer profile (largest closed loop) + stock thickness; inner loops = through cut-outs
    POCKET_D<depth> | _THRU     closed loop -> pocket_rect (rounded / rotated) | pocket_polygon; circle -> pocket_circle
    HOLE_THRU | HOLE_D<depth>   circles -> through / blind holes (equal holes grouped into one callout)
    THREAD_<M6|M8x1>_D<depth>   circles -> threaded holes; <depth> = usable thread depth, drill 0.5·d (≥ 2.5 mm) deeper
    THREAD_<spec>_THRU          through threaded holes
    SLOT_D<depth> | _THRU       obround loop (2 half circles) -> slot; a single LINE + layer width is not supported

Geometry may be LWPOLYLINE / POLYLINE (with bulges), CIRCLE, or chains of LINE + ARC. The datum is the bottom-left
corner of the contour's bounding box (PartSpec ``bottom_left_top_face``). Everything a drawing cannot tell
(no thickness, no depth) becomes a blocking ``missing_dimensions`` entry instead of a guess.
"""

from __future__ import annotations

import io
import math
import re
from collections import defaultdict
from dataclasses import dataclass, field

from app.schemas.part_spec import PartSpec, parse_metric_thread

NUM = r"(\d+(?:[.,]\d+)?)"
LAYER_RES = {
    "contour": re.compile(rf"^CONTOUR(?:_T{NUM})?$", re.I),
    "pocket": re.compile(rf"^POCKET(?:_D{NUM}|_(THRU))?$", re.I),
    "hole": re.compile(rf"^HOLE(?:_D{NUM}|_(THRU))$", re.I),
    "thread": re.compile(rf"^THREAD_(M{NUM}(?:X{NUM})?)(?:_D{NUM}|_(THRU))?$", re.I),
    "slot": re.compile(rf"^SLOT(?:_D{NUM}|_(THRU))?$", re.I),
}
UNITS = {0: 1.0, 1: 25.4, 2: 304.8, 4: 1.0, 5: 10.0, 6: 1000.0}
EPS = 1e-3


class DxfError(ValueError):
    pass


@dataclass
class DxfResult:
    spec: PartSpec
    warnings: list[str] = field(default_factory=list)
    layers: dict[str, int] = field(default_factory=dict)  # layer -> entities used


def _num(s: str | None) -> float | None:
    return float(s.replace(",", ".")) if s else None


# --------------------------------------------------------------------------- #
# Loops: (x, y, bulge) vertex lists
# --------------------------------------------------------------------------- #

Vertex = tuple[float, float, float]


def _arc_center(p0, p1, b: float) -> tuple[tuple[float, float], float]:
    dx, dy = p1[0] - p0[0], p1[1] - p0[1]
    c = math.hypot(dx, dy)
    s = c / 2
    nx, ny = -dy / c, dx / c  # left normal of the chord
    off = s * (1 - b * b) / (2 * b)
    cx, cy = (p0[0] + p1[0]) / 2 + nx * off, (p0[1] + p1[1]) / 2 + ny * off
    return (cx, cy), math.hypot(p0[0] - cx, p0[1] - cy)


def _chain(segments: list[tuple[tuple[float, float], tuple[float, float], float]]) -> list[list[Vertex]]:
    """Join LINE / ARC segments (start, end, bulge) into closed loops."""
    loops: list[list[Vertex]] = []
    todo = list(segments)
    while todo:
        a, b, bulge = todo.pop(0)
        loop: list[Vertex] = [(a[0], a[1], bulge)]
        end, start = b, a
        progressed = True
        while progressed and math.dist(end, start) > EPS:
            progressed = False
            for i, (p, q, bb) in enumerate(todo):
                if math.dist(p, end) <= EPS:
                    loop.append((p[0], p[1], bb))
                    end = q
                elif math.dist(q, end) <= EPS:
                    loop.append((q[0], q[1], -bb))
                    end = p
                else:
                    continue
                todo.pop(i)
                progressed = True
                break
        if math.dist(end, start) <= EPS and len(loop) >= 2:
            loops.append(loop)
    return loops


def _entity_loops(entities, scale: float) -> tuple[list[list[Vertex]], list[tuple[float, float, float]], int]:
    """Closed loops and circles (cx, cy, r) from a layer's entities; returns also the count of unused entities."""
    loops: list[list[Vertex]] = []
    circles: list[tuple[float, float, float]] = []
    segs = []
    unused = 0
    for e in entities:
        t = e.dxftype()
        if t == "CIRCLE":
            c = e.dxf.center
            circles.append((c.x * scale, c.y * scale, e.dxf.radius * scale))
        elif t == "LWPOLYLINE":
            pts = [(x * scale, y * scale, b) for x, y, b in e.get_points("xyb")]
            if e.closed or (len(pts) > 2 and math.dist(pts[0][:2], pts[-1][:2]) <= EPS):
                if len(pts) > 2 and math.dist(pts[0][:2], pts[-1][:2]) <= EPS and not e.closed:
                    pts = pts[:-1]
                loops.append(pts)
            else:
                segs += [((pts[i][0], pts[i][1]), (pts[i + 1][0], pts[i + 1][1]), pts[i][2]) for i in range(len(pts) - 1)]
        elif t == "POLYLINE" and not e.is_3d_polyline:
            pts = [(v.dxf.location.x * scale, v.dxf.location.y * scale, v.dxf.bulge) for v in e.vertices]
            if e.is_closed:
                loops.append(pts)
            else:
                segs += [((pts[i][0], pts[i][1]), (pts[i + 1][0], pts[i + 1][1]), pts[i][2]) for i in range(len(pts) - 1)]
        elif t == "LINE":
            s, en = e.dxf.start, e.dxf.end
            segs.append(((s.x * scale, s.y * scale), (en.x * scale, en.y * scale), 0.0))
        elif t == "ARC":
            c, r = e.dxf.center, e.dxf.radius
            a0, a1 = math.radians(e.dxf.start_angle), math.radians(e.dxf.end_angle)
            sweep = (a1 - a0) % (2 * math.pi) or 2 * math.pi
            if abs(sweep - 2 * math.pi) < 1e-9:
                circles.append((c.x * scale, c.y * scale, r * scale))
                continue
            p0 = ((c.x + r * math.cos(a0)) * scale, (c.y + r * math.sin(a0)) * scale)
            p1 = ((c.x + r * math.cos(a1)) * scale, (c.y + r * math.sin(a1)) * scale)
            segs.append((p0, p1, math.tan(sweep / 4)))
        else:
            unused += 1
    chained = _chain(segs)
    leftover = len(segs) - sum(len(lp) for lp in chained)
    return loops + chained, circles, unused + max(0, leftover)


def _sample(loop: list[Vertex], per_arc: int = 12) -> list[tuple[float, float]]:
    pts = []
    n = len(loop)
    for i, (x, y, b) in enumerate(loop):
        p0, p1 = (x, y), loop[(i + 1) % n][:2]
        pts.append(p0)
        if abs(b) > 1e-9 and math.dist(p0, p1) > 1e-9:
            (cx, cy), r = _arc_center(p0, p1, b)
            a0 = math.atan2(p0[1] - cy, p0[0] - cx)
            sweep = 4 * math.atan(b)
            for k in range(1, per_arc):
                a = a0 + sweep * k / per_arc
                pts.append((cx + r * math.cos(a), cy + r * math.sin(a)))
    return pts


def _area(pts) -> float:
    return 0.5 * sum(pts[i][0] * pts[(i + 1) % len(pts)][1] - pts[(i + 1) % len(pts)][0] * pts[i][1] for i in range(len(pts)))


def _bbox(pts) -> tuple[float, float, float, float]:
    xs, ys = [p[0] for p in pts], [p[1] for p in pts]
    return min(xs), min(ys), max(xs), max(ys)


@dataclass
class Shape2D:
    kind: str  # rect | slot | circle | polygon
    cx: float = 0.0
    cy: float = 0.0
    length: float = 0.0
    width: float = 0.0
    radius: float = 0.0  # corner radius (rect / polygon) or circle radius
    rotation: float = 0.0
    start: tuple[float, float] = (0.0, 0.0)
    end: tuple[float, float] = (0.0, 0.0)
    points: list[tuple[float, float]] = field(default_factory=list)
    area: float = 0.0


def classify(loop: list[Vertex]) -> Shape2D:
    n = len(loop)
    segs = []
    for i, (x, y, b) in enumerate(loop):
        p0, p1 = (x, y), loop[(i + 1) % n][:2]
        if math.dist(p0, p1) <= EPS:
            continue
        segs.append((p0, p1, b))
    pts = _sample(loop)
    area = abs(_area(pts))
    arcs = [(p0, p1, b) for p0, p1, b in segs if abs(b) > 1e-6]
    lines = [(p0, p1) for p0, p1, b in segs if abs(b) <= 1e-6]
    sweeps = [abs(4 * math.atan(b)) for _, _, b in arcs]
    radii = [_arc_center(p0, p1, b)[1] for p0, p1, b in arcs]

    if not lines and arcs and abs(sum(sweeps) - 2 * math.pi) < 1e-3 and max(radii) - min(radii) < EPS:
        (cx, cy), r = _arc_center(*arcs[0])
        return Shape2D("circle", cx, cy, radius=r, area=area)
    if len(arcs) == 2 and all(abs(s - math.pi) < 1e-3 for s in sweeps) and len(lines) == 2 and abs(radii[0] - radii[1]) < EPS:
        c0, c1 = sorted((_arc_center(*arcs[0])[0], _arc_center(*arcs[1])[0]), key=lambda p: (round(p[0], 6), round(p[1], 6)))
        return Shape2D("slot", (c0[0] + c1[0]) / 2, (c0[1] + c1[1]) / 2, width=2 * radii[0], start=c0, end=c1, area=area)
    if lines and len(arcs) in (0, 4) and all(abs(s - math.pi / 2) < 1e-3 for s in sweeps) \
            and (not radii or max(radii) - min(radii) < EPS) and len(lines) == 4:
        ang = math.atan2(lines[0][1][1] - lines[0][0][1], lines[0][1][0] - lines[0][0][0])
        ok = all(abs(math.sin(2 * (math.atan2(q[1] - p[1], q[0] - p[0]) - ang))) < 1e-4 for p, q in lines)
        if ok:
            r = radii[0] if radii else 0.0
            ca, sa = math.cos(-ang), math.sin(-ang)
            rot = [(x * ca - y * sa, x * sa + y * ca) for x, y in pts]
            x0, y0, x1, y1 = _bbox(rot)
            ux, uy = (x0 + x1) / 2, (y0 + y1) / 2
            cx, cy = ux * math.cos(ang) - uy * math.sin(ang), ux * math.sin(ang) + uy * math.cos(ang)
            deg = math.degrees(ang) % 180
            L, W = x1 - x0, y1 - y0
            if abs(deg - 90) < 1e-6:  # axis-aligned, first edge vertical
                deg, L, W = 0.0, W, L
            if deg > 90:
                deg -= 180
            return Shape2D("rect", cx, cy, length=L, width=W, radius=r, rotation=round(deg, 6), area=area)
    corner = radii[0] if radii and max(radii) - min(radii) < EPS and all(s < math.pi * 0.75 for s in sweeps) else 0.0
    return Shape2D("polygon", points=[(x, y) for x, y, _ in loop] if not arcs else pts, radius=corner, area=area)


# --------------------------------------------------------------------------- #
# Layers -> PartSpec
# --------------------------------------------------------------------------- #


def _r(v: float, nd: int = 4) -> float:
    return round(v, nd) + 0.0


def parse_dxf(data: bytes, name: str = "dxf_part") -> DxfResult:
    import ezdxf
    from ezdxf import recover

    try:
        doc, auditor = recover.read(io.BytesIO(data))
    except Exception as exc:  # noqa: BLE001
        raise DxfError(f"DXF kon niet gelezen worden: {exc}") from exc
    scale = UNITS.get(int(doc.header.get("$INSUNITS", 4) or 0), 1.0)
    by_layer: dict[str, list] = defaultdict(list)
    for e in doc.modelspace():
        by_layer[e.dxf.layer].append(e)
    warnings: list[str] = []
    used: dict[str, int] = {}
    missing: list[dict] = []
    ambiguities: list[str] = []

    contour_layers = [(ly, m) for ly in by_layer if (m := LAYER_RES["contour"].match(ly))]
    if not contour_layers:
        raise DxfError("Geen laag CONTOUR_T<dikte> gevonden: teken de buitencontour op laag CONTOUR_T<dikte in mm>.")
    raw_loops = [lp for ly, _ in contour_layers for lp in _entity_loops(by_layer[ly], 1.0)[0]]
    raw_circles = [c for ly, _ in contour_layers for c in _entity_loops(by_layer[ly], 1.0)[1]]
    raw = [p for lp in raw_loops for p in _sample(lp)] + [(cx + s * r, cy) for cx, cy, r in raw_circles for s in (-1, 1)]
    if raw and scale != 1.0:
        bx0, by0, bx1, by1 = _bbox(raw)
        if max(bx1 - bx0, by1 - by0) * scale > 3000:  # a CNC plate over 3 m: the unit header is wrong
            warnings.append(f"$INSUNITS={doc.header.get('$INSUNITS')} zou het onderdeel "
                            f"{max(bx1 - bx0, by1 - by0) * scale / 1000:.1f} m groot maken: waarden als mm gelezen.")
            scale = 1.0
    contour_loops: list[list[Vertex]] = []
    thickness = None
    for ly, m in contour_layers:
        loops, circles, unused = _entity_loops(by_layer[ly], scale)
        contour_loops += loops
        contour_loops += [[(cx - r, cy, 1.0), (cx + r, cy, 1.0)] for cx, cy, r in circles]
        used[ly] = len(by_layer[ly]) - unused
        if m.group(1):
            thickness = _num(m.group(1))  # layer names are always in mm
        if unused:
            warnings.append(f"{ly}: {unused} entiteit(en) genegeerd (geen gesloten contour).")
    if not contour_loops:
        raise DxfError("Laag CONTOUR bevat geen gesloten contour.")
    classified = sorted(((classify(lp), lp) for lp in contour_loops), key=lambda t: -t[0].area)
    outer, outer_loop = classified[0]
    shapes = [s for s, _ in classified]
    pts = _sample(outer_loop)
    x0, y0, x1, y1 = _bbox(pts)
    L, W = x1 - x0, y1 - y0
    if thickness is None:
        thickness = 10.0
        missing.append({"field_path": "stock.thickness_z", "prompt_nl": "Plaatdikte (Z) ontbreekt: laagnaam CONTOUR_T<dikte>",
                        "prompt_en": "Stock thickness (Z) missing: name the layer CONTOUR_T<thickness>", "blocking": True})

    def local(x: float, y: float) -> tuple[float, float]:
        return _r(x - x0), _r(y - y0)

    if outer.kind == "rect" and abs(outer.rotation) < 1e-6:
        profile = {"type": "rounded_rectangle", "corner_radius": _r(outer.radius)} if outer.radius > EPS else {"type": "rectangle"}
    elif outer.kind == "rect":
        profile = {"type": "polygon", "points_2d": [list(local(*p)) for p in pts]}
        ambiguities.append("Gedraaide buitencontour: als polygoon overgenomen.")
    else:
        profile = {"type": "polygon", "points_2d": [list(local(*p)) for p in outer.points or pts]}
        if outer.kind == "circle":
            ambiguities.append("Ronde buitencontour: als polygoon benaderd (draaiwerk valt buiten v1).")

    features: list[dict] = []
    counters: dict[str, int] = defaultdict(int)

    def fid(prefix: str) -> str:
        counters[prefix] += 1
        return f"{prefix}{counters[prefix]}"

    def cut(depth: float | None, through: bool, prefix: str, what: str) -> dict:
        if through:
            return {"through_all": True}
        if depth is None:
            missing.append({"field_path": f"features.{prefix}.depth", "prompt_nl": f"Diepte van {what} ontbreekt (laag ..._D<diepte>)",
                            "prompt_en": f"Depth of {what} missing (layer ..._D<depth>)", "blocking": True})
            return {"depth": 1.0}
        return {"depth": depth}

    def pocket_from(shape: Shape2D, depth: float | None, through: bool, layer: str) -> None:
        f_id = fid("P")
        base = {"id": f_id, "face": "top", "source_note": f"DXF layer {layer}", "confidence": 1.0}
        if shape.kind == "circle":
            cx, cy = local(shape.cx, shape.cy)
            features.append({**base, "type": "pocket_circle", "center_x": cx, "center_y": cy,
                             "diameter": _r(2 * shape.radius), **cut(depth, through, f_id, f"pocket {f_id}")})
        elif shape.kind == "rect":
            cx, cy = local(shape.cx, shape.cy)
            features.append({**base, "type": "pocket_rect", "center_x": cx, "center_y": cy, "length_x": _r(shape.length),
                             "width_y": _r(shape.width), "corner_radius": _r(shape.radius), "rotation_deg": _r(shape.rotation),
                             **cut(depth, through, f_id, f"pocket {f_id}")})
        elif shape.kind == "slot":
            sx, sy = local(*shape.start)
            ex, ey = local(*shape.end)
            features.append({**base, "id": f_id, "type": "slot", "start_x": sx, "start_y": sy, "end_x": ex, "end_y": ey,
                             "width": _r(shape.width), **cut(depth, through, f_id, f"sleuf {f_id}")})
        else:
            features.append({**base, "type": "pocket_polygon", "points_2d": [list(local(*p)) for p in shape.points],
                             "corner_radius": _r(shape.radius), **cut(depth, through, f_id, f"pocket {f_id}")})

    for s in shapes[1:]:  # inner loops on the contour layer: through cut-outs
        pocket_from(s, None, True, "CONTOUR")

    hole_groups: dict[tuple, list[tuple[float, float]]] = defaultdict(list)
    for ly, ents in by_layer.items():
        if LAYER_RES["contour"].match(ly):
            continue
        kind = next((k for k, rx in LAYER_RES.items() if k != "contour" and rx.match(ly)), None)
        if kind is None:
            if ly not in ("0", "Defpoints", "DEFPOINTS"):
                warnings.append(f"Laag {ly!r} ({len(ents)} entiteit(en)) genegeerd: onbekende laagnaam.")
            continue
        m = LAYER_RES[kind].match(ly)
        loops, circles, unused = _entity_loops(ents, scale)
        used[ly] = len(ents) - unused
        if unused:
            warnings.append(f"{ly}: {unused} entiteit(en) genegeerd (geen cirkel of gesloten contour).")
        if kind in ("pocket", "slot"):
            depth, through = _num(m.group(1)), bool(m.group(2))  # no depth -> blocking missing dimension
            for lp in loops:
                shape = classify(lp)
                if kind == "slot" and shape.kind != "slot":
                    warnings.append(f"{ly}: gesloten contour is geen sleuf (2 halve cirkels + 2 rechten); als pocket overgenomen.")
                pocket_from(shape, depth, through, ly)
            for cx, cy, r in circles:
                pocket_from(Shape2D("circle", cx, cy, radius=r), depth, through, ly)
        elif kind == "hole":
            depth, through = _num(m.group(1)), bool(m.group(2))
            for cx, cy, r in circles:
                hole_groups[("hole", ly, round(2 * r, 3), depth, through, None)].append(local(cx, cy))
            if loops:
                warnings.append(f"{ly}: {len(loops)} gesloten contour(en) genegeerd: boringen moeten cirkels zijn.")
        elif kind == "thread":
            spec_txt = m.group(1).upper().replace("X", "x")
            depth, through = _num(m.group(4)), bool(m.group(5))
            for cx, cy, r in circles:
                hole_groups[("thread", ly, spec_txt, depth, through, round(2 * r, 3))].append(local(cx, cy))

    for (kind, ly, key, depth, through, extra), positions in hole_groups.items():
        f_id = fid("T" if kind == "thread" else "H")
        f: dict = {"id": f_id, "type": "hole", "face": "top", "center_x": positions[0][0], "center_y": positions[0][1],
                   "extra_positions": [list(p) for p in positions[1:]], "source_note": f"DXF layer {ly}", "confidence": 1.0}
        if kind == "hole":
            f["diameter"] = key
            f.update(cut(depth, through, f_id, f"boring {f_id}"))
            if not through and depth is not None:
                f["tip_angle_deg"] = 118.0
        else:
            t = parse_metric_thread(key)
            if not t:
                raise DxfError(f"{ly}: onbekende draadmaat {key!r} (verwacht bv. THREAD_M6_D12 of THREAD_M8x1_THRU)")
            nominal, pitch = t
            f["diameter"] = nominal
            f["thread"] = key
            drawn = extra or 0.0
            if drawn and not (nominal - pitch * 1.3 - 0.2 <= drawn <= nominal + 0.2):
                ambiguities.append(f"{f_id}: getekende cirkel Ø{drawn:g} past niet bij {key} (verwacht Ø{nominal:g} of kernboor).")
            if through:
                f["through_all"] = True
            elif depth is None:
                f.update(cut(None, False, f_id, f"draad {f_id}"))
            else:
                drill = depth + max(2.5, round(0.5 * nominal * 2) / 2)
                f["thread_depth"] = depth
                if drill >= thickness - 0.5:
                    f["through_all"] = True
                else:
                    f["depth"] = drill
        features.append(f)

    spec = PartSpec.model_validate({
        "schema_version": "1.1", "input_source": "dxf", "part_name": name,
        "stock": {"length_x": _r(L), "width_y": _r(W), "thickness_z": thickness, "confidence": 1.0},
        "outer_profile": {**profile, "confidence": 1.0}, "features": features, "ambiguities": ambiguities + warnings,
        "missing_dimensions": missing,
    })
    if auditor.has_errors:
        warnings.append(f"DXF hersteld: {len(auditor.errors)} fout(en) in het bestand.")
    return DxfResult(spec, warnings, used)


def render_dxf_pdf(data: bytes, out_pdf) -> None:
    """Plain vector rendering of the uploaded DXF (shown as the 'original' drawing in the UI)."""
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from ezdxf import recover
    from ezdxf.addons.drawing import Frontend, RenderContext
    from ezdxf.addons.drawing.config import BackgroundPolicy, ColorPolicy, Configuration
    from ezdxf.addons.drawing.matplotlib import MatplotlibBackend

    doc, _ = recover.read(io.BytesIO(data))
    fig = plt.figure(figsize=(11.69, 8.27))
    ax = fig.add_axes((0.02, 0.02, 0.96, 0.96))
    ax.set_axis_off()
    cfg = Configuration(background_policy=BackgroundPolicy.WHITE, color_policy=ColorPolicy.BLACK)
    Frontend(RenderContext(doc), MatplotlibBackend(ax), config=cfg).draw_layout(doc.modelspace(), finalize=True)
    fig.savefig(out_pdf, format="pdf")
    plt.close(fig)
