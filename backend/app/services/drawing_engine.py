"""EN ISO 2D drawing engine: PartSpec + B-Rep -> drawing.pdf / .svg / .dxf (DRAWING_SPEC.md §4).

The PartSpec stays the single source of truth: geometry comes from exact HLR projections of the
compiled B-Rep, every dimension and callout comes from the PartSpec semantics (no geometric guessing).

Pipeline:
  1. HLR projections (build123d ``project_to_viewport``) with look-at = datum origin, so view coordinates
     are datum coordinates: front (u, v) = (x, z), top = (x, y), left = (-y, z).
  2. Dimension plan per view side (baseline from the datum, EN ISO 129-1; no chains).
  3. Scale + sheet solver (EN ISO 5455 scales, A4 -> A3 promotion) and first-angle layout (EN ISO 5456-2):
     front top-left, top below front, left view right of front, isometric in the free corner.
  4. Compose: frame, title block, views (ISO 128-2 lines), centre lines, dimensions, leader callouts with
     collision avoidance (fallback: numbered balloons + notes), general notes.
  5. Render the same sheet model to vector PDF (selectable text), SVG and DXF.
"""

from __future__ import annotations

import datetime as dt
import math
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

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
)
from app.services.iso_template import (
    MARGIN,
    TEXT_DIM,
    TEXT_SMALL,
    Box,
    DrawingFont,
    Sheet,
    box_inside,
    boxes_overlap,
    draw_frame,
    get_font,
    notes_block,
    title_block,
)

ISO_SCALES: list[tuple[float, str]] = [(5, "5:1"), (2, "2:1"), (1, "1:1"), (0.5, "1:2"), (0.2, "1:5"), (0.1, "1:10"),
                                       (0.05, "1:20"), (0.02, "1:50")]
MIN_GUTTER = 25.0  # mm around every view (spec §4.2)
DIM_FIRST = 10.0  # first baseline 10 mm outside the contour
DIM_STEP = 8.0  # parallel baselines staggered by 8 mm
VIEW_DEFS = {
    "front": ((0.0, -1.0, 0.0), (0.0, 0.0, 1.0)),
    "top": ((0.0, 0.0, 1.0), (0.0, 1.0, 0.0)),
    "left": ((-1.0, 0.0, 0.0), (0.0, 0.0, 1.0)),
    "iso": ((1.0, -1.0, 1.0), (0.0, 0.0, 1.0)),
}
FACE_VIEW = {"top": "top", "bottom": "top", "front": "front", "back": "front", "left": "left", "right": "left"}


def model_to_view(view: str, p: tuple[float, float, float]) -> tuple[float, float]:
    x, y, z = p
    if view == "front":
        return x, z
    if view == "top":
        return x, y
    if view == "left":
        return -y, z
    raise ValueError(view)


def fmt(v: float, decimal: str = ".") -> str:
    s = f"{v:.3f}".rstrip("0").rstrip(".")
    s = "0" if s in ("-0", "") else s
    return s.replace(".", decimal)


# --------------------------------------------------------------------------- #
# 1. Projections
# --------------------------------------------------------------------------- #


@dataclass
class ViewProjection:
    name: str
    visible: list[np.ndarray]
    hidden: list[np.ndarray]
    bbox: tuple[float, float, float, float]  # u0, v0, u1, v1 (view / model mm)


def _edge_points(edge) -> np.ndarray:
    gt = edge.geom_type
    gt = gt() if callable(gt) else gt
    n = 2 if getattr(gt, "name", str(gt)) == "LINE" else max(12, min(72, int(edge.length / 1.5) + 8))
    return np.array([[p.X, p.Y] for p in (edge @ t for t in np.linspace(0.0, 1.0, n))])


def project_views(shape) -> dict[str, ViewProjection]:
    bb = shape.bounding_box()
    dist = (bb.center().length + bb.diagonal) * 6 + 100
    out: dict[str, ViewProjection] = {}
    for name, (d, up) in VIEW_DEFS.items():
        dv = np.array(d) / np.linalg.norm(d)
        vis, hid = shape.project_to_viewport(tuple(dv * dist), up, (0.0, 0.0, 0.0))
        vis_pl = [_edge_points(e) for e in vis]
        hid_pl = [_edge_points(e) for e in hid]
        pts = np.vstack(vis_pl + hid_pl) if (vis_pl or hid_pl) else np.zeros((1, 2))
        out[name] = ViewProjection(name, vis_pl, hid_pl, (float(pts[:, 0].min()), float(pts[:, 1].min()),
                                                          float(pts[:, 0].max()), float(pts[:, 1].max())))
    return out


# --------------------------------------------------------------------------- #
# 2. Semantic dimension plan + callouts (from the PartSpec)
# --------------------------------------------------------------------------- #


@dataclass
class DimSpec:
    view: str
    side: str  # below | above | left | right
    a: float  # datum coordinate (along the measured axis, view units)
    b: float  # feature coordinate
    text: str
    ext_a: float | None = None  # perpendicular start of the extension lines (None = view edge)
    ext_b: float | None = None


@dataclass
class Callout:
    view: str
    anchor: tuple[float, float]  # view coords of the feature point
    radius: float  # feature radius (view mm) - leader starts on the outline
    lines: list[str]
    feature_id: str


def _face_frames(spec: PartSpec):
    from app.services.cad_compiler import face_frames

    return face_frames(spec)


def _global(frames, face: str, fx: float, fy: float, depth: float = 0.0) -> tuple[float, float, float]:
    fr = frames[face]
    p = fr.origin + fr.u_dir * fx + fr.v_dir * fy - fr.normal * depth
    return (p.X, p.Y, p.Z)


def _in_view(face: str, fx: float, fy: float) -> tuple[float, float]:
    """Face coordinates -> coordinates in the view that looks at that face."""
    return (-fx, fy) if face in ("left", "right") else (fx, fy)


def _thread_label(h: Hole) -> str:
    return h.thread or ""


def callout_lines(f, font: DrawingFont, dec: str) -> list[str]:
    n = len(f.positions()) if hasattr(f, "positions") else 1
    pre = f"{n}x " if n > 1 else ""
    d = lambda v: fmt(v, dec)  # noqa: E731
    cb = "⌴" if font.has("⌴") else "CBORE "
    cs = "⌵" if font.has("⌵") else "CSK "
    deep = "↧" if font.has("↧") else "DEEP "
    depth = lambda feat: "THRU" if getattr(feat, "through_all", False) else f"{deep}{d(feat.depth)}"  # noqa: E731
    if isinstance(f, Hole):
        if f.thread:
            first = f"{pre}{_thread_label(f)} " + ("THRU" if f.through_all else f"{deep}{d(f.thread_depth or f.depth)}")
            lines = [first]
            if not f.through_all and f.depth and (f.thread_depth or 0) < f.depth:
                lines.append(f"Ø{d(f.drill_diameter())} {deep}{d(f.depth)}")
        else:
            tol = f" {f.tolerance}" if f.tolerance else ""
            lines = [f"{pre}Ø{d(f.diameter)}{tol} {depth(f)}"]
        if f.counterbore:
            lines.append(f"{cb}Ø{d(f.counterbore.diameter)} {deep}{d(f.counterbore.depth)}")
        if f.countersink:
            lines.append(f"{cs}Ø{d(f.countersink.diameter)} x {d(f.countersink.angle_deg)}°")
        return lines
    if isinstance(f, PocketRect):
        r = f" R{d(f.corner_radius)}" if f.corner_radius > 0 else ""
        rot = f" ROT {d(f.rotation_deg)}°" if f.rotation_deg else ""
        return [f"{pre}POCKET {d(f.length_x)}x{d(f.width_y)}{r} {depth(f)}{rot}"]
    if isinstance(f, PocketCircle):
        return [f"{pre}POCKET Ø{d(f.diameter)} {depth(f)}"]
    if isinstance(f, PocketPolygon):
        r = f" R{d(f.corner_radius)}" if f.corner_radius > 0 else ""
        return [f"POCKET {len(f.points_2d)}-GON{r} {depth(f)}"]
    if isinstance(f, Slot):
        return [f"SLOT {d(f.width)} {depth(f)}"]
    if isinstance(f, BossRect):
        return [f"{pre}BOSS {d(f.length_x)}x{d(f.width_y)} H{d(f.height)}"]
    if isinstance(f, BossCircle):
        return [f"{pre}BOSS Ø{d(f.diameter)} H{d(f.height)}"]
    if isinstance(f, StepCut):
        r = f" R{d(f.corner_radius)}" if f.corner_radius > 0 else ""
        return [f"STEP {deep}{d(f.depth)}{r}"]
    return []


def plan_dimensions(spec: PartSpec, font: DrawingFont, dec: str) -> tuple[list[DimSpec], list[Callout]]:
    frames = _face_frames(spec)
    (x0, y0, z0), (x1, y1, z1) = spec.stock_bbox()
    coords: dict[tuple[str, str], set[float]] = {}  # (view, axis) -> feature coordinates
    dims: list[DimSpec] = []
    callouts: list[Callout] = []
    d = lambda v: fmt(v, dec)  # noqa: E731

    def add(view: str, axis: str, value: float) -> None:
        coords.setdefault((view, axis), set()).add(round(value, 4))

    for f in spec.features:
        if isinstance(f, Complex3D):
            continue
        view = FACE_VIEW[f.face]
        pts: list[tuple[float, float]] = []
        radius = 0.0
        if isinstance(f, (Hole, PocketRect, PocketCircle, BossRect, BossCircle)):
            pts = [tuple(p) for p in f.positions()]
            if isinstance(f, Hole):
                radius = (f.counterbore.diameter if f.counterbore else f.countersink.diameter if f.countersink
                          else f.drill_diameter()) / 2
            elif isinstance(f, (PocketCircle, BossCircle)):
                radius = f.diameter / 2
            elif isinstance(f, (PocketRect, BossRect)):
                radius = min(f.length_x, f.width_y) / 2
        elif isinstance(f, PocketPolygon):
            pts = [tuple(p) for p in f.points_2d]
        elif isinstance(f, Slot):
            pts = [(f.start_x, f.start_y), (f.end_x, f.end_y)]
            radius = f.width / 2
        elif isinstance(f, StepCut):
            (u0, u1), (v0, v1) = frames[f.face].u_range, frames[f.face].v_range
            pts = [(x, y) for x in (f.x_min, f.x_max) for y in (f.y_min, f.y_max)
                   if u0 + 1e-6 < x < u1 - 1e-6 or v0 + 1e-6 < y < v1 - 1e-6]
            pts = [(min(max(x, u0), u1), min(max(y, v0), v1)) for x, y in pts]
        for fx, fy in pts:
            u, v = _in_view(f.face, fx, fy)
            add(view, "h", u)
            add(view, "v", v)
        lines = callout_lines(f, font, dec)
        if lines and pts:
            anchor = _in_view(f.face, *pts[0]) if not isinstance(f, PocketPolygon) else _in_view(
                f.face, sum(p[0] for p in pts) / len(pts), sum(p[1] for p in pts) / len(pts))
            if isinstance(f, (PocketRect, BossRect)) and not f.rotation_deg:
                anchor = (anchor[0] + f.length_x / 2 * (-1 if f.face in ("left", "right") else 1), anchor[1])
                radius = 0.0
            callouts.append(Callout(view, anchor, radius, lines, f.id))
        # depth reference dims in the front view for top/bottom-face pockets, slots and steps
        if f.face in ("top", "bottom") and isinstance(f, (PocketRect, PocketCircle, PocketPolygon, Slot, StepCut)) \
                and not getattr(f, "through_all", False):
            top = f.face == "top"
            face_v = z1 if top else z0
            level = face_v - f.depth if top else face_v + f.depth
            umax = max(p[0] for p in pts) if pts else x1
            if isinstance(f, (PocketRect, PocketCircle, Slot)):
                ext = (f.length_x / 2 if isinstance(f, PocketRect) else f.diameter / 2 if isinstance(f, PocketCircle)
                       else f.width / 2)
                umax += ext
            dims.append(DimSpec("front", "right", face_v, level, f"({d(f.depth)})", ext_b=min(umax, x1)))

    for p in (spec.outer_profile.points_2d or []) if spec.outer_profile.type == "polygon" else []:
        add("top", "h", p[0])
        add("top", "v", p[1])

    # baseline dims from the datum (0) to every unique feature coordinate
    sides = {("top", "h"): "below", ("top", "v"): "left", ("front", "h"): "above", ("front", "v"): "right",
             ("left", "h"): "above", ("left", "v"): "right"}
    overall = {("top", "h"): (x0, x1), ("top", "v"): (y0, y1)}
    for key, values in coords.items():
        view, axis = key
        lo, hi = overall.get(key, (None, None))
        for value in sorted(values, key=abs):
            if abs(value) < 1e-6 or (hi is not None and (abs(value - hi) < 1e-6 or abs(value - lo) < 1e-6)):
                continue
            dims.append(DimSpec(view, sides[key], 0.0, value, d(abs(value))))
    # overall dimensions (outermost by construction: largest span)
    dims.append(DimSpec("top", "below", x0, x1, d(x1 - x0)))
    dims.append(DimSpec("top", "left", y0, y1, d(y1 - y0)))
    dims.append(DimSpec("front", "left", z0, z1, d(z1 - z0)))
    return dims, callouts


# --------------------------------------------------------------------------- #
# 3. Scale / sheet solver + first-angle layout
# --------------------------------------------------------------------------- #


@dataclass
class Placement:
    view: str
    ox: float  # sheet x of view u = 0
    oy: float  # sheet y of view v = 0
    scale: float
    rect: Box  # sheet box of the projected geometry

    def to_sheet(self, u: float, v: float) -> tuple[float, float]:
        return (self.ox + u * self.scale, self.oy - v * self.scale)


@dataclass
class Layout:
    sheet: str
    scale: float
    scale_label: str
    placements: dict[str, Placement]
    fits: bool


def _gutters(dims: list[DimSpec]) -> dict[tuple[str, str], float]:
    count: dict[tuple[str, str], int] = {}
    for dm in dims:
        count[(dm.view, dm.side)] = count.get((dm.view, dm.side), 0) + 1
    out: dict[tuple[str, str], float] = {}
    for view in ("front", "top", "left"):
        for side in ("below", "above", "left", "right"):
            n = count.get((view, side), 0)
            need = DIM_FIRST + DIM_STEP * (n - 1) + TEXT_DIM + 4 if n else 0.0
            out[(view, side)] = max(MIN_GUTTER, need)
    return out


def solve_layout(views: dict[str, ViewProjection], dims: list[DimSpec], sheet_size: str, scale: float,
                 forbidden: Box, inner: Box) -> Layout:
    g = _gutters(dims)
    f, t, lft = views["front"].bbox, views["top"].bbox, views["left"].bbox
    s = scale
    u_min = min(f[0], t[0])
    ox = inner[0] + max(g[("front", "left")], g[("top", "left")]) - u_min * s
    v_max = max(f[3], lft[3])
    oy_front = inner[1] + max(g[("front", "above")], g[("left", "above")]) + v_max * s
    front_bottom = oy_front - f[1] * s
    oy_top = front_bottom + g[("front", "below")] + g[("top", "above")] + t[3] * s
    ox_left = ox + max(f[2], t[2]) * s + g[("front", "right")] + g[("left", "left")] - lft[0] * s

    def build(dx: float, dy: float) -> tuple[dict[str, Placement], list[Box]]:
        pl = {
            "front": Placement("front", ox + dx, oy_front + dy, s, (0, 0, 0, 0)),
            "top": Placement("top", ox + dx, oy_top + dy, s, (0, 0, 0, 0)),
            "left": Placement("left", ox_left + dx, oy_front + dy, s, (0, 0, 0, 0)),
        }
        boxes = []
        for name, p in pl.items():
            u0, v0, u1, v1 = views[name].bbox
            p.rect = (p.ox + u0 * s, p.oy - v1 * s, p.ox + u1 * s, p.oy - v0 * s)
            boxes.append((p.rect[0] - g[(name, "left")], p.rect[1] - g[(name, "above")],
                          p.rect[2] + g[(name, "right")], p.rect[3] + g[(name, "below")]))
        return pl, boxes

    def ok(boxes: list[Box]) -> bool:
        return all(box_inside(b, inner) for b in boxes) and not any(boxes_overlap(b, forbidden) for b in boxes)

    pl, boxes = build(0.0, 0.0)
    slack_x = inner[2] - max(b[2] for b in boxes)
    slack_y = inner[3] - max(b[3] for b in boxes)
    for dx, dy in ((slack_x / 2, slack_y / 2), (slack_x / 2, 0.0), (0.0, slack_y / 2), (0.0, 0.0)):
        cand, cboxes = build(max(dx, 0.0), max(dy, 0.0))
        if ok(cboxes):
            return Layout(sheet_size, s, "", cand, True)
    return Layout(sheet_size, s, "", pl, ok(boxes))


# --------------------------------------------------------------------------- #
# 4. Composition
# --------------------------------------------------------------------------- #


@dataclass
class DrawingResult:
    pdf: Path
    svg: Path
    dxf: Path
    sheet_size: str
    scale: str
    view_rects: dict[str, Box]
    text_boxes: list[Box] = field(default_factory=list)
    balloons: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


class _Obstacles:
    def __init__(self, inner: Box):
        self.boxes: list[Box] = []
        self.inner = inner

    def add(self, b: Box) -> None:
        self.boxes.append(b)

    def free(self, b: Box, pad: float = 0.8) -> bool:
        if not box_inside(b, (self.inner[0] + 1, self.inner[1] + 1, self.inner[2] - 1, self.inner[3] - 1)):
            return False
        return not any(boxes_overlap(b, o, pad) for o in self.boxes)


def _seg_box(a, b, w: float = 0.4) -> Box:
    return (min(a[0], b[0]) - w, min(a[1], b[1]) - w, max(a[0], b[0]) + w, max(a[1], b[1]) + w)


def _segment_hits_box(a, b, box: Box) -> bool:
    for t in np.linspace(0.0, 1.0, 24):
        x, y = a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t
        if box[0] < x < box[2] and box[1] < y < box[3]:
            return True
    return False


def _draw_hdim(sh: Sheet, obs: _Obstacles, xa: float, xb: float, line_y: float, ext_ya: float, ext_yb: float,
               text: str, side: str) -> None:
    sgn = 1.0 if side == "below" else -1.0
    thin = sh.lines.thin
    for x, ey in ((xa, ext_ya), (xb, ext_yb)):
        sh.line((x, ey + sgn * 1.0), (x, line_y + sgn * 2.0), width=thin, layer="DIMENSIONS")
    lo, hi = min(xa, xb), max(xa, xb)
    tw = sh.measure(text)
    if hi - lo >= 7.0:
        sh.line((lo, line_y), (hi, line_y), width=thin, layer="DIMENSIONS")
        sh.arrow((lo, line_y), (hi, line_y))
        sh.arrow((hi, line_y), (lo, line_y))
    else:  # arrows outside, pointing inward
        sh.line((lo - 5, line_y), (hi + 5, line_y), width=thin, layer="DIMENSIONS")
        sh.arrow((lo, line_y), (lo - 3, line_y))
        sh.arrow((hi, line_y), (hi + 3, line_y))
    if hi - lo >= tw + 8:
        box = sh.text(text, (lo + hi) / 2, line_y - 1.0, TEXT_DIM, "middle", layer="DIMENSIONS")
    else:
        box = sh.text(text, hi + 6, line_y - 1.0, TEXT_DIM, "start", layer="DIMENSIONS")
    obs.add(box)
    obs.add(_seg_box((lo - 5, line_y), (hi + 5, line_y)))


def _draw_vdim(sh: Sheet, obs: _Obstacles, ya: float, yb: float, line_x: float, ext_xa: float, ext_xb: float,
               text: str, side: str) -> None:
    sgn = -1.0 if side == "left" else 1.0
    thin = sh.lines.thin
    for y, ex in ((ya, ext_xa), (yb, ext_xb)):
        sh.line((ex + sgn * 1.0, y), (line_x + sgn * 2.0, y), width=thin, layer="DIMENSIONS")
    lo, hi = min(ya, yb), max(ya, yb)
    tw = sh.measure(text)
    if hi - lo >= 7.0:
        sh.line((line_x, lo), (line_x, hi), width=thin, layer="DIMENSIONS")
        sh.arrow((line_x, lo), (line_x, hi))
        sh.arrow((line_x, hi), (line_x, lo))
    else:
        sh.line((line_x, lo - 5), (line_x, hi + 5), width=thin, layer="DIMENSIONS")
        sh.arrow((line_x, lo), (line_x, lo - 3))
        sh.arrow((line_x, hi), (line_x, hi + 3))
    if hi - lo >= tw + 8:
        box = sh.text(text, line_x - 1.0, (lo + hi) / 2, TEXT_DIM, "middle", rotation=90, layer="DIMENSIONS")
    else:
        box = sh.text(text, line_x - 1.0, lo - 6, TEXT_DIM, "start", rotation=90, layer="DIMENSIONS")
    obs.add(box)
    obs.add(_seg_box((line_x, lo - 5), (line_x, hi + 5)))


def _draw_views(sh: Sheet, layout: Layout, views: dict[str, ViewProjection]) -> None:
    for name, p in layout.placements.items():
        vp = views[name]
        for pl in vp.hidden:
            sh.polyline([p.to_sheet(u, v) for u, v in pl], style="hidden", layer="HIDDEN")
        for pl in vp.visible:
            sh.polyline([p.to_sheet(u, v) for u, v in pl], width=sh.lines.thick, layer="VISIBLE")


def _centerlines(sh: Sheet, spec: PartSpec, layout: Layout) -> None:
    frames = _face_frames(spec)
    (x0, y0, z0), (x1, y1, z1) = spec.stock_bbox()
    for f in spec.features:
        if not isinstance(f, (Hole, PocketCircle, BossCircle)):
            continue
        r = (f.drill_diameter() if isinstance(f, Hole) else f.diameter) / 2
        if isinstance(f, Hole):
            depth = frames[f.face].thickness if f.through_all else f.depth + r / math.tan(math.radians(59))
        elif isinstance(f, PocketCircle):
            depth = frames[f.face].thickness if f.through_all else f.depth
        else:
            depth = -f.height
        for fx, fy in f.positions():
            a3 = _global(frames, f.face, fx, fy, 0.0)
            b3 = _global(frames, f.face, fx, fy, depth)
            for name, p in layout.placements.items():
                ua, va = model_to_view(name, a3)
                ub, vb = model_to_view(name, b3)
                sa, sb = p.to_sheet(ua, va), p.to_sheet(ub, vb)
                ext = 2.0
                if math.hypot(sb[0] - sa[0], sb[1] - sa[1]) < 0.05:  # looking along the axis: centre cross
                    rr = r * p.scale + ext
                    sh.line((sa[0] - rr, sa[1]), (sa[0] + rr, sa[1]), style="center", layer="CENTER")
                    sh.line((sa[0], sa[1] - rr), (sa[0], sa[1] + rr), style="center", layer="CENTER")
                else:
                    L = math.hypot(sb[0] - sa[0], sb[1] - sa[1])
                    ux, uy = (sb[0] - sa[0]) / L, (sb[1] - sa[1]) / L
                    sh.line((sa[0] - ux * ext, sa[1] - uy * ext), (sb[0] + ux * ext, sb[1] + uy * ext), style="center",
                            layer="CENTER")


def _place_dims(sh: Sheet, obs: _Obstacles, dims: list[DimSpec], layout: Layout) -> None:
    groups: dict[tuple[str, str], list[DimSpec]] = {}
    for dm in dims:
        groups.setdefault((dm.view, dm.side), []).append(dm)
    for (view, side), items in groups.items():
        p = layout.placements[view]
        items.sort(key=lambda d_: abs(d_.b - d_.a))
        x0, y0, x1, y1 = p.rect
        for k, dm in enumerate(items):
            off = DIM_FIRST + DIM_STEP * k
            if side in ("below", "above"):
                line_y = y1 + off if side == "below" else y0 - off
                edge = y1 if side == "below" else y0
                xa, _ = p.to_sheet(dm.a, 0)
                xb, _ = p.to_sheet(dm.b, 0)
                _draw_hdim(sh, obs, xa, xb, line_y, edge, edge, dm.text, side)
            else:
                line_x = x0 - off if side == "left" else x1 + off
                edge = x0 if side == "left" else x1
                _, ya = p.to_sheet(0, dm.a)
                _, yb = p.to_sheet(0, dm.b)
                exa = p.to_sheet(dm.ext_a, 0)[0] if dm.ext_a is not None else edge
                exb = p.to_sheet(dm.ext_b, 0)[0] if dm.ext_b is not None else edge
                _draw_vdim(sh, obs, ya, yb, line_x, exa, exb, dm.text, side)


def _place_callout(sh: Sheet, obs: _Obstacles, c: Callout, layout: Layout) -> bool:
    p = layout.placements[c.view]
    ax, ay = p.to_sheet(*c.anchor)
    r = c.radius * p.scale
    h = TEXT_DIM
    widths = [sh.measure(t, h) for t in c.lines]
    block_w, block_h = max(widths), h * 1.6 * len(c.lines)
    x0, y0, x1, y1 = p.rect
    best = None
    for ang in (45, 135, -45, -135, 20, 160, -20, -160, 90, -90):
        ux, uy = math.cos(math.radians(ang)), -math.sin(math.radians(ang))
        # distance to leave the view's geometry box along this direction
        tx = ((x1 - ax) / ux if ux > 1e-9 else (x0 - ax) / ux if ux < -1e-9 else 1e9)
        ty = ((y1 - ay) / uy if uy > 1e-9 else (y0 - ay) / uy if uy < -1e-9 else 1e9)
        exit_d = max(min(tx, ty), r + 4)
        for extra in (5, 11, 17, 25, 34, 45):
            dist = exit_d + extra
            ex, ey = ax + ux * dist, ay + uy * dist
            right = ux >= 0
            tx0 = ex + 4 if right else ex - 4 - block_w
            box = (tx0 - 0.5, ey - block_h / 2 - 0.8, tx0 + block_w + 0.5, ey + block_h / 2 + 0.8)
            start = (ax + ux * r, ay + uy * r)
            if not obs.free(box):
                continue
            if any(_segment_hits_box(start, (ex, ey), o) for o in obs.boxes if o[2] - o[0] < 120 and o[3] - o[1] < 40):
                continue
            score = dist + (0 if ang in (45, 135, -45, -135) else 6)
            if best is None or score < best[0]:
                best = (score, start, (ex, ey), right, tx0, box)
            break
    if best is None:
        return False
    _, start, (ex, ey), right, tx0, box = best
    thin = sh.lines.thin
    shoulder = (ex + 3, ey) if right else (ex - 3, ey)
    sh.line(start, (ex, ey), width=thin, layer="DIMENSIONS")
    sh.line((ex, ey), shoulder, width=thin, layer="DIMENSIONS")
    sh.arrow(start, (ex, ey))
    top = ey - block_h / 2
    for i, t in enumerate(c.lines):
        sh.text(t, tx0, top + h * 1.15 + i * h * 1.6, h, "start", layer="CALLOUTS")
    obs.add(box)
    obs.add(_seg_box(start, (ex, ey)))
    return True


def _balloon(sh: Sheet, obs: _Obstacles, n: int, c: Callout, layout: Layout) -> None:
    p = layout.placements[c.view]
    ax, ay = p.to_sheet(*c.anchor)
    r = c.radius * p.scale
    for ang in (45, 135, -45, -135, 0, 180, 90, -90):
        ux, uy = math.cos(math.radians(ang)), -math.sin(math.radians(ang))
        cx, cy = ax + ux * (r + 7), ay + uy * (r + 7)
        box = (cx - 3.2, cy - 3.2, cx + 3.2, cy + 3.2)
        if obs.free(box, pad=0.2):
            sh.line((ax + ux * r, ay + uy * r), (cx - ux * 3, cy - uy * 3), layer="DIMENSIONS")
            sh.circle((cx, cy), 3.0, n=32, layer="CALLOUTS")
            sh.text(str(n), cx, cy + 1.25, TEXT_SMALL, "middle", layer="CALLOUTS")
            obs.add(box)
            return
    sh.text(f"({n})", ax + r + 1, ay - r - 1, TEXT_SMALL, layer="CALLOUTS")


def _datum_text(datum: str) -> str:
    return {
        "bottom_left_top_face": "BOTTOM LEFT OF TOP VIEW, ON TOP FACE",
        "bottom_left_bottom_face": "BOTTOM LEFT OF TOP VIEW, ON BOTTOM FACE",
        "center_top_face": "CENTRE OF TOP FACE",
        "center_bottom_face": "CENTRE OF BOTTOM FACE",
    }.get(datum, datum)


def _edge_note(et, dec: str) -> str:
    target = et.target.replace("_", " ").upper()
    return (f"CHAMFER {fmt(et.size, dec)}x45° ON {target}" if et.type == "chamfer"
            else f"FILLET R{fmt(et.size, dec)} ON {target}")


def generate_drawing(spec: PartSpec, shape, out_dir: Path, *, sheet_size: str | None = None, scale: str | None = None,
                     language: str | None = None, drawing_number: str | None = None, mass_g: float | None = None,
                     owner: str = "", font_path: str | None = None, basename: str = "drawing") -> DrawingResult:
    """Render the EN ISO detail drawing of `shape` (compiled from `spec`) to PDF, SVG and DXF."""
    font = get_font(font_path)
    dm = spec.drawing_metadata
    lang = language or dm.language
    dec = "."
    warnings: list[str] = []
    views = project_views(shape)
    dims, callouts = plan_dimensions(spec, font, dec)

    notes = [f"1. GENERAL TOLERANCES {spec.general_tolerance or 'ISO 2768-mK'}",
             f"2. SURFACE FINISH {dm.surface_finish} UNLESS OTHERWISE STATED",
             f"3. {dm.edge_break_note.upper()}",
             f"4. DIMENSIONS IN mm. DATUM (0,0,0): {_datum_text(spec.datum_origin)}"]
    for et in spec.edge_treatments:
        notes.append(f"{len(notes) + 1}. {_edge_note(et, dec)}")
    for f in spec.complex_features():
        notes.append(f"{len(notes) + 1}. {f.id}: {f.description.upper()} - SEE 3D MODEL")
    if dm.approval_status == "DRAFT":
        notes.append("DRAFT - NOT RELEASED FOR PRODUCTION UNTIL DRAWN / CHECKED ARE SIGNED")

    # -- sheet + scale selection ----------------------------------------------------
    want_sheet = sheet_size or dm.sheet_size
    want_scale = scale or dm.scale
    scales = ISO_SCALES if want_scale in (None, "AUTO") else [_parse_scale(want_scale)]

    def best_on(size: str) -> tuple[Layout, Sheet, Box] | None:
        probe = Sheet(size, font)
        nbox = _notes_box(probe, notes)
        forbidden = (min(probe.title_block[0], nbox[0]), nbox[1], probe.inner[2], probe.inner[3])
        for sval, slabel in scales:  # largest scale first
            lay = solve_layout(views, dims, size, sval, forbidden, probe.inner)
            lay.scale_label = slabel
            if lay.fits or len(scales) == 1:
                return lay, probe, forbidden
        return None

    if want_sheet not in (None, "AUTO"):
        chosen = best_on(want_sheet)
    else:  # spec §1.3: A4, promoted to A3 below 1:2 or with more than 12 features
        chosen = best_on("A4_LANDSCAPE") if len(spec.features) <= 12 else None
        if chosen is None or chosen[0].scale < 0.5:
            chosen = best_on("A3_LANDSCAPE") or chosen
    if chosen is None:  # nothing fits: smallest ISO scale on the largest sheet
        scales = ISO_SCALES[-1:]
        chosen = best_on("A3_LANDSCAPE" if want_sheet in (None, "AUTO") else want_sheet)
        warnings.append("part does not fit the sheet at any standard scale")
    assert chosen is not None
    layout, sh, forbidden = chosen
    if not layout.fits:
        warnings.append(f"views do not fit {layout.sheet} at {layout.scale_label}")

    # -- compose ---------------------------------------------------------------------
    obs = _Obstacles(sh.inner)
    draw_frame(sh)
    title_block(sh, {
        "title": spec.part_name or "PART", "doc_type": "DETAIL DRAWING",
        "drawing_no": drawing_number or dm.drawing_number or "",
        "material": spec.material or "", "gen_tol": spec.general_tolerance or "ISO 2768-mK",
        "surface": dm.surface_finish.split(" (")[0], "scale": layout.scale_label, "rev": dm.revision,
        "drawn": dm.drawn_by, "checked": dm.checked_by, "date": dt.date.today().isoformat(), "units": "mm",
        "mass": f"{mass_g:.0f} g" if mass_g else "", "status": dm.approval_status, "sheet": "1/1", "owner": owner,
    }, lang)
    obs.add(forbidden)
    _draw_views(sh, layout, views)
    for p in layout.placements.values():
        obs.add(p.rect)
    _centerlines(sh, spec, layout)
    # datum origin marker in the top view
    tp = layout.placements["top"]
    sh.circle(tp.to_sheet(0.0, 0.0), 1.2, n=24, layer="DIMENSIONS")
    _place_dims(sh, obs, dims, layout)

    # outer corner radius + edge treatment callouts
    (x0, y0, z0), (x1, y1, z1) = spec.stock_bbox()
    extra: list[Callout] = []
    prof = spec.outer_profile
    if prof.type == "rounded_rectangle" and prof.corner_radius > 0:
        rr = prof.corner_radius
        extra.append(Callout("top", (x0 + rr - rr * math.cos(math.pi / 4), y1 - rr + rr * math.sin(math.pi / 4)), 0.0,
                             [f"4x R{fmt(rr, dec)}"], "outer_profile"))
    for et in spec.edge_treatments:
        if et.target in ("top_outer_edges", "all_outer_edges"):
            label = f"{fmt(et.size, dec)}x45°" if et.type == "chamfer" else f"R{fmt(et.size, dec)}"
            extra.append(Callout("front", (x1, z1), 0.0, [label], et.id))
    balloons: list[str] = []
    for c in extra + callouts:
        if not _place_callout(sh, obs, c, layout):
            n = len(balloons) + 1
            _balloon(sh, obs, n, c, layout)
            balloons.append(f"({n}) {' / '.join(c.lines)}")
            warnings.append(f"callout for {c.feature_id} placed as balloon ({n})")

    # isometric view in the free corner (right of the top view, above the notes)
    iso_rect = _place_iso(sh, views["iso"], layout, obs, forbidden)
    if iso_rect is None:
        warnings.append("no room for the isometric view")

    final_notes = notes + [f"{len(notes) + i + 1}. {b}" for i, b in enumerate(balloons)]
    notes_block(sh, final_notes, sh.title_block[1] - 1.5, sh.inner[2])

    out_dir.mkdir(parents=True, exist_ok=True)
    meta = {"title": f"{spec.part_name or 'PART'} - EN ISO detail drawing", "subject": "EN ISO 128 / 129-1 / 5456-2 / 7200",
            "keywords": f"scale {layout.scale_label}; {layout.sheet}; {dm.approval_status}"}
    pdf = sh.to_pdf(out_dir / f"{basename}.pdf", meta)
    svg = sh.to_svg(out_dir / f"{basename}.svg", meta["title"])
    dxf = sh.to_dxf(out_dir / f"{basename}.dxf")
    rects = {k: p.rect for k, p in layout.placements.items()}
    if iso_rect:
        rects["iso"] = iso_rect
    return DrawingResult(pdf, svg, dxf, layout.sheet, layout.scale_label, rects,
                         [sh.text_box(t) for t in sh.texts], balloons, warnings)


def _parse_scale(label: str) -> tuple[float, str]:
    a, b = label.split(":")
    return float(a) / float(b), label


def _notes_box(sh: Sheet, notes: list[str]) -> Box:
    probe = Sheet(sh.size, sh.font)
    return notes_block(probe, notes + ["(n) reserve line for balloons"], sh.title_block[1] - 1.5, sh.inner[2])


def _place_iso(sh: Sheet, iso: ViewProjection, layout: Layout, obs: _Obstacles, forbidden: Box) -> Box | None:
    top, left = layout.placements["top"].rect, layout.placements["left"].rect
    fx0 = max(top[2] + 12, left[0] - 10)
    fy0 = left[3] + 14
    fx1, fy1 = sh.inner[2] - 4, forbidden[1] - 6
    if fx1 - fx0 < 30 or fy1 - fy0 < 25:
        return None
    u0, v0, u1, v1 = iso.bbox
    w, h = u1 - u0, v1 - v0
    fit = min((fx1 - fx0) / max(w, 1e-6), (fy1 - fy0) / max(h, 1e-6))
    s = next((sv for sv, _ in ISO_SCALES if sv <= min(fit, layout.scale)), None)
    if s is None or w * s < 20:
        return None
    ox = (fx0 + fx1) / 2 - (u0 + u1) / 2 * s
    oy = (fy0 + fy1) / 2 + (v0 + v1) / 2 * s
    p = Placement("iso", ox, oy, s, (ox + u0 * s, oy - v1 * s, ox + u1 * s, oy - v0 * s))
    if not obs.free(p.rect, pad=0.5):
        return None
    for pl in iso.visible:
        sh.polyline([p.to_sheet(u, v) for u, v in pl], width=sh.lines.thick, layer="VISIBLE")
    label = "ISOMETRIC" + ("" if abs(s - layout.scale) < 1e-9 else f" ({next(lb for sv, lb in ISO_SCALES if sv == s)})")
    sh.text(label, (p.rect[0] + p.rect[2]) / 2, p.rect[3] + 5.5, TEXT_SMALL, "middle", layer="NOTES")
    obs.add(p.rect)
    _ = MARGIN
    return p.rect
