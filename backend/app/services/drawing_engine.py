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
from typing import Protocol
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
    parse_metric_thread,
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
LINE_PITCH = 1.75  # multi-line callouts: baseline spacing in lettering heights
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
    alternates: list[tuple[float, float]] = field(default_factory=list)  # other instances the leader may use


def _face_frames(spec: PartSpec):
    from app.services.cad_compiler import face_frames

    return face_frames(spec)


def _global(frames, face: str, fx: float, fy: float, depth: float = 0.0) -> tuple[float, float, float]:
    fr = frames[face]
    p = fr.origin + fr.u_dir * fx + fr.v_dir * fy - fr.normal * depth
    return (p.X, p.Y, p.Z)


def _face_view(frames, face: str) -> str:
    """Orthographic view that looks at a face: the dominant axis of its outward normal (tilted planes too)."""
    n = frames[face].normal
    ax = max(range(3), key=lambda i: abs((n.X, n.Y, n.Z)[i]))
    return ("left", "front", "top")[ax]


def _in_view(frames, face: str, fx: float, fy: float) -> tuple[float, float]:
    """Face coordinates -> coordinates in the view that looks at that face (any frame convention)."""
    return model_to_view(_face_view(frames, face), _global(frames, face, fx, fy))


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


GDT_SYMBOL = {"straightness": "⏤", "flatness": "⏥", "circularity": "○", "cylindricity": "⌭", "profile_line": "⌒",
              "profile_surface": "⌓", "parallelism": "∥", "perpendicularity": "⊥", "angularity": "∠", "position": "⌖",
              "concentricity": "◎", "symmetry": "⌯", "circular_runout": "↗", "total_runout": "⌰"}


def gdt_note(g, font: DrawingFont, dec: str = ".") -> str:
    """Tolerance frame as text: ``[⌖ | Ø0.1 | A | B] H1`` (the word when the font lacks the ISO 1101 symbol)."""
    sym = GDT_SYMBOL[g.characteristic]
    sym = sym if font.has(sym) else g.characteristic.upper().replace("_", " ")
    zone = ("Ø" if g.diameter_zone else "") + fmt(g.tolerance_mm, dec)
    target = f" {g.feature_id}" if g.feature_id else ""
    return f"[{' | '.join([sym, zone, *g.datums])}]{target}" + (f" {g.note}" if g.note else "") + " (ISO 1101)"


def plan_dimensions(spec: PartSpec, font: DrawingFont, dec: str) -> tuple[list[DimSpec], list[Callout]]:
    frames = _face_frames(spec)
    (x0, y0, z0), (x1, y1, z1) = spec.stock_bbox()
    coords: dict[tuple[str, str], set[float]] = {}  # (view, axis) -> feature coordinates
    dims: list[DimSpec] = []
    callouts: list[Callout] = []
    d = lambda v: fmt(v, dec)  # noqa: E731

    depth_refs: dict[tuple[str, float], tuple[float, float, float]] = {}  # (face, level) -> (face z, depth, x extent)

    def add(view: str, axis: str, value: float) -> None:
        coords.setdefault((view, axis), set()).add(round(value, 4))

    for f in spec.features:
        if isinstance(f, Complex3D):
            continue
        view = _face_view(frames, f.face)
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
            u, v = _in_view(frames, f.face, fx, fy)
            add(view, "h", u)
            add(view, "v", v)
        lines = callout_lines(f, font, dec)
        if lines and pts:
            anchor = _in_view(frames, f.face, *pts[0]) if not isinstance(f, PocketPolygon) else _in_view(
                frames, f.face, sum(p[0] for p in pts) / len(pts), sum(p[1] for p in pts) / len(pts))
            alts = [_in_view(frames, f.face, *p) for p in pts[1:]] if not isinstance(f, PocketPolygon) else []
            if isinstance(f, (PocketRect, BossRect)) and not f.rotation_deg:
                hx = f.length_x / 2  # leader to the +u edge of the rectangle (mapped into the view)
                anchor = _in_view(frames, f.face, pts[0][0] + hx, pts[0][1])
                alts = [_in_view(frames, f.face, x + hx, y) for x, y in pts[1:]]
                radius = 0.0
            callouts.append(Callout(view, anchor, radius, lines, f.id, alts))
        # depth reference dims in the front view (spec §4.3): pockets, slots, steps, blind holes, counterbores
        if f.face in ("top", "bottom"):
            top = f.face == "top"
            umax = max(p[0] for p in pts) if pts else x1
            depths: list[tuple[float, float]] = []  # (depth, x extent of the feature)
            if isinstance(f, (PocketRect, PocketCircle, PocketPolygon, Slot, StepCut)) and not getattr(f, "through_all", False):
                ext = (f.length_x / 2 if isinstance(f, PocketRect) else f.diameter / 2 if isinstance(f, PocketCircle)
                       else f.width / 2 if isinstance(f, Slot) else 0.0)
                depths.append((f.depth, umax + ext))
            elif isinstance(f, Hole):
                if not f.through_all and f.depth:
                    depths.append((f.depth, umax + f.drill_diameter() / 2))
                if f.counterbore:
                    depths.append((f.counterbore.depth, umax + f.counterbore.diameter / 2))
            for dv, ux in depths:
                face_v = z1 if top else z0
                level = round(face_v - dv if top else face_v + dv, 4)
                prev = depth_refs.get((f.face, level))
                depth_refs[(f.face, level)] = (face_v, dv, max(ux, prev[2]) if prev else ux)

    for (_face, level), (face_v, dv, ux) in sorted(depth_refs.items(), key=lambda kv: kv[1][1]):
        dims.append(DimSpec("front", "right", face_v, level, f"({d(dv)})", ext_b=min(ux, x1)))

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
    feature_marks: dict[str, list[tuple[float, float, float]]] = field(default_factory=dict)  # view -> (u, v, r)


EDGE_CLEAR = 6.0  # clearance around a view side that carries no dimensions (towards frame / notes)
CENTER_MARK_CLEAR = 5.0  # ISO 5457 centring marks reach 5 mm into the drawing field


def _approx_text_w(text: str) -> float:
    return 0.64 * TEXT_DIM * len(text)  # DejaVu Sans digits are 0.636 em


def _gutters(dims: list[DimSpec], views: dict[str, ViewProjection] | None = None,
             scale: float = 1.0) -> dict[tuple[str, str], float]:
    """Room the baseline dimensions need on each side of each view (mm on the sheet).

    Dimension text sits above horizontal lines and left of vertical ones, so only the outermost line on the
    `above` / `left` sides carries its text outside the stack. Dimensions too short for their text put it
    past their upper (vertical) or right (horizontal) end, which may stick out of the view's side."""
    count: dict[tuple[str, str], int] = {}
    for dm in dims:
        count[(dm.view, dm.side)] = count.get((dm.view, dm.side), 0) + 1
    out: dict[tuple[str, str], float] = {}
    for view in ("front", "top", "left"):
        for side in ("below", "above", "left", "right"):
            n = count.get((view, side), 0)
            text_out = TEXT_DIM + 1.5 if side in ("above", "left") else 1.5
            out[(view, side)] = DIM_FIRST + DIM_STEP * (n - 1) + text_out + 2.0 if n else EDGE_CLEAR
    if views is not None:
        for dm in dims:
            tw = _approx_text_w(dm.text)
            if abs(dm.b - dm.a) * scale >= tw + 8:
                continue
            u0, v0, u1, v1 = views[dm.view].bbox
            if dm.side in ("left", "right"):  # vertical: text above the upper end
                key, inside = (dm.view, "above"), (v1 - max(dm.a, dm.b)) * scale
            else:  # horizontal: text right of the right end
                key, inside = (dm.view, "right"), (u1 - max(dm.a, dm.b)) * scale
            out[key] = max(out[key], 6.0 + tw + 1.5 - inside)
    return out


def solve_layout(views: dict[str, ViewProjection], dims: list[DimSpec], sheet_size: str, scale: float,
                 forbidden: Box, inner: Box) -> Layout:
    g = _gutters(dims, views, scale)
    f, t, lft = views["front"].bbox, views["top"].bbox, views["left"].bbox
    s = scale
    u_min = min(f[0], t[0])
    ox = inner[0] + max(g[("front", "left")], g[("top", "left")]) - u_min * s
    v_max = max(f[3], lft[3])
    oy_front = inner[1] + max(g[("front", "above")], g[("left", "above")]) + v_max * s
    front_bottom = oy_front - f[1] * s
    # gap between two views: both views' dimension stacks, but never closer than MIN_GUTTER
    gap_ft = max(MIN_GUTTER, g[("front", "below")] + g[("top", "above")])
    gap_fl = max(MIN_GUTTER, g[("front", "right")] + g[("left", "left")])
    oy_top = front_bottom + gap_ft + t[3] * s
    ox_left = ox + max(f[2], t[2]) * s + gap_fl - lft[0] * s

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
    """Boxes text may not overlap; the subset `texts` may not be crossed by leader lines either."""

    def __init__(self, inner: Box):
        self.boxes: list[Box] = []
        self.texts: list[Box] = []
        self.inner = inner

    def add(self, b: Box, blocks_leaders: bool = False) -> None:
        self.boxes.append(b)
        if blocks_leaders:
            self.texts.append(b)

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
        obs.add(_seg_box((x, ey + sgn * 1.0), (x, line_y + sgn * 2.0), 0.6))
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
    obs.add(box, blocks_leaders=True)
    obs.add(_seg_box((lo - 5, line_y), (hi + 5, line_y)))


def _draw_vdim(sh: Sheet, obs: _Obstacles, ya: float, yb: float, line_x: float, ext_xa: float, ext_xb: float,
               text: str, side: str) -> None:
    sgn = -1.0 if side == "left" else 1.0
    thin = sh.lines.thin
    for y, ex in ((ya, ext_xa), (yb, ext_xb)):
        sh.line((ex + sgn * 1.0, y), (line_x + sgn * 2.0, y), width=thin, layer="DIMENSIONS")
        obs.add(_seg_box((ex + sgn * 1.0, y), (line_x + sgn * 2.0, y), 0.6))
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
    obs.add(box, blocks_leaders=True)
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


VISIBLE_FACE = {"top": "top", "front": "front", "left": "left"}  # the face each view looks at (others are behind)


def _thread_symbols(sh: Sheet, spec: PartSpec, layout: Layout) -> None:
    """EN ISO 6410-1 internal threads: the model carries the tap-drill bore, the drawing adds the nominal diameter.

    Looking along the axis: thin 3/4 circle at the nominal diameter (dashed when on the far face). Axis in the view
    plane: thin hidden lines at the nominal diameter down to the usable thread length + the thread-limit line."""
    frames = _face_frames(spec)
    for f in spec.features:
        if not isinstance(f, Hole) or not f.thread:
            continue
        t = parse_metric_thread(f.thread)
        if not t:
            continue
        R = t[0] / 2
        length = f.thread_depth or (frames[f.face].thickness if f.through_all else (f.depth or frames[f.face].thickness))
        for fx, fy in f.positions():
            a3 = np.array(_global(frames, f.face, fx, fy, 0.0))
            b3 = np.array(_global(frames, f.face, fx, fy, length))
            axis = (b3 - a3) / max(np.linalg.norm(b3 - a3), 1e-9)
            for name, p in layout.placements.items():
                sa = p.to_sheet(*model_to_view(name, tuple(a3)))
                sb = p.to_sheet(*model_to_view(name, tuple(b3)))
                if math.hypot(sb[0] - sa[0], sb[1] - sa[1]) < 0.05:  # end view: 3/4 circle, gap in the 1st quadrant
                    rr = R * p.scale
                    pts = [(sa[0] + rr * math.cos(math.radians(a)), sa[1] - rr * math.sin(math.radians(a)))
                           for a in np.linspace(100, 350, 36)]
                    near = VISIBLE_FACE.get(name) == f.face
                    sh.polyline(pts, width=sh.lines.thin, style="solid" if near else "hidden", layer="THREADS")
                    continue
                # side view: offset the axis by +-R perpendicular to it, within the view plane
                for s in (-1.0, 1.0):
                    perp = np.cross(axis, np.array(VIEW_DEFS[name][0]))
                    perp = perp / max(np.linalg.norm(perp), 1e-9) * R * s
                    pa = p.to_sheet(*model_to_view(name, tuple(a3 + perp)))
                    pb = p.to_sheet(*model_to_view(name, tuple(b3 + perp)))
                    sh.line(pa, pb, width=sh.lines.thin, style="hidden", layer="THREADS")
                perp = np.cross(axis, np.array(VIEW_DEFS[name][0]))
                perp = perp / max(np.linalg.norm(perp), 1e-9) * R
                sh.line(p.to_sheet(*model_to_view(name, tuple(b3 - perp))), p.to_sheet(*model_to_view(name, tuple(b3 + perp))),
                        width=sh.lines.thick, style="hidden", layer="THREADS")  # thread limit


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


def layout_feature_boxes(view: str, layout: Layout, exclude: tuple[float, float]) -> list[Box]:
    """Sheet boxes of the other feature instances in a view (leaders should not run through them)."""
    p = layout.placements[view]
    out = []
    for u, v, r in layout.feature_marks.get(view, []):
        if abs(u - exclude[0]) < 1e-6 and abs(v - exclude[1]) < 1e-6:
            continue
        x, y = p.to_sheet(u, v)
        rr = max(r * p.scale, 1.5)
        out.append((x - rr, y - rr, x + rr, y + rr))
    return out


def _place_callout(sh: Sheet, obs: _Obstacles, c: Callout, layout: Layout) -> bool:
    best = None
    for anchor in [c.anchor] + c.alternates:  # any instance of the feature may carry the leader
        cand = _best_leader(sh, obs, c, layout, anchor)
        if cand is not None and (best is None or cand[0] < best[0]):
            best = cand
    if best is None:
        return False
    return _draw_leader(sh, obs, c, best)


def _best_leader(sh: Sheet, obs: _Obstacles, c: Callout, layout: Layout, anchor: tuple[float, float]):
    p = layout.placements[c.view]
    ax, ay = p.to_sheet(*anchor)
    r = c.radius * p.scale
    h = TEXT_DIM
    widths = [sh.measure(t, h) for t in c.lines]
    block_w, block_h = max(widths), h * LINE_PITCH * len(c.lines)
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
            if any(_segment_hits_box(start, (ex, ey), o) for o in obs.texts):
                continue
            # penalise leaders that cross other features of the same view
            crossings = sum(1 for o in layout_feature_boxes(c.view, layout, anchor) if _segment_hits_box(start, (ex, ey), o))
            score = dist + (0 if ang in (45, 135, -45, -135) else 6) + 25 * crossings
            if best is None or score < best[0]:
                best = (score, start, (ex, ey), right, tx0, box)
            break
    return best


def _draw_leader(sh: Sheet, obs: _Obstacles, c: Callout, best) -> bool:
    h = TEXT_DIM
    block_h = h * LINE_PITCH * len(c.lines)
    _, start, (ex, ey), right, tx0, box = best
    thin = sh.lines.thin
    shoulder = (ex + 3, ey) if right else (ex - 3, ey)
    sh.line(start, (ex, ey), width=thin, layer="DIMENSIONS")
    sh.line((ex, ey), shoulder, width=thin, layer="DIMENSIONS")
    sh.arrow(start, (ex, ey))
    top = ey - block_h / 2
    for i, t in enumerate(c.lines):
        sh.text(t, tx0, top + h * 1.2 + i * h * LINE_PITCH, h, "start", layer="CALLOUTS")
    obs.add(box, blocks_leaders=True)
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
                     owner: str = "", font_path: str | None = None, basename: str = "drawing",
                     section_view: str | None = None, qr_url: str | None = None) -> DrawingResult:
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
             f"3. {dm.edge_break_note}",
             f"4. DIMENSIONS IN mm. DATUM (0,0,0): {_datum_text(spec.datum_origin)}"]
    for et in spec.edge_treatments:
        notes.append(f"{len(notes) + 1}. {_edge_note(et, dec)}")
    for f in spec.complex_features():
        notes.append(f"{len(notes) + 1}. {f.id}: {f.description.upper()} - SEE 3D MODEL")
    for g in dm.gdt:  # EN ISO 1101 frames as text notes (spec §2 / §8.2)
        notes.append(f"{len(notes) + 1}. {gdt_note(g, font, dec)}")
    for sf in dm.surface_finishes:
        notes.append(f"{len(notes) + 1}. {sf.feature_id}: Ra {fmt(sf.ra_um, dec)}" + (f" {sf.note}" if sf.note else "")
                     + " (ISO 21920-1)")
    for extra in dm.notes:
        if extra.strip():
            notes.append(f"{len(notes) + 1}. {extra.strip()}")
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
        i0, i1, i2, i3 = probe.inner  # keep clear of the ISO 5457 centring marks (5 mm into the field)
        inner = (i0 + CENTER_MARK_CLEAR, i1 + CENTER_MARK_CLEAR, i2 - CENTER_MARK_CLEAR, i3 - CENTER_MARK_CLEAR)
        for sval, slabel in scales:  # largest scale first
            lay = solve_layout(views, dims, size, sval, forbidden, inner)
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
        "mass": f"{mass_g:.0f} g" if mass_g else "", "status": dm.approval_status, "sheet": "1/1", "owner": owner, "qr_url": qr_url,
    }, lang)
    obs.add(forbidden)
    _draw_views(sh, layout, views)
    for p in layout.placements.values():
        obs.add(p.rect)
    _centerlines(sh, spec, layout)
    _thread_symbols(sh, spec, layout)
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
    for c in callouts:
        for a in [c.anchor] + c.alternates:
            layout.feature_marks.setdefault(c.view, []).append((a[0], a[1], max(c.radius, 1.0)))
    balloons: list[str] = []
    for c in extra + callouts:
        if not _place_callout(sh, obs, c, layout):
            n = len(balloons) + 1
            _balloon(sh, obs, n, c, layout)
            balloons.append(f"({n}) {' / '.join(c.lines)}")
            warnings.append(f"callout for {c.feature_id} placed as balloon ({n})")

    # section A-A (hatched) through the row with the most hidden features, then the isometric view
    section_rect = None
    yc = _section_plane(spec) if (section_view or dm.section_view) == "AUTO" else None
    if yc is not None:
        try:
            section_rect = _place_section(sh, spec, shape, layout, obs, forbidden, yc)
        except Exception as exc:  # noqa: BLE001 - a failed section never costs the drawing
            warnings.append(f"section A-A skipped: {type(exc).__name__}: {exc}")
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
    if section_rect:
        rects["section"] = section_rect
    return DrawingResult(pdf, svg, dxf, layout.sheet, layout.scale_label, rects,
                         [sh.text_box(t) for t in sh.texts], balloons, warnings)


def _parse_scale(label: str) -> tuple[float, str]:
    a, b = label.split(":")
    return float(a) / float(b), label


def _notes_box(sh: Sheet, notes: list[str]) -> Box:
    probe = Sheet(sh.size, sh.font)
    return notes_block(probe, notes + ["(n) reserve line for balloons"], sh.title_block[1] - 1.5, sh.inner[2])


# --------------------------------------------------------------------------- #
# Section A-A with ISO 128-50 hatching
# --------------------------------------------------------------------------- #

HATCH_SPACING = 2.5  # mm on the sheet (EN ISO 128-50: thin lines at 45°, spacing to suit the size)


def _section_plane(spec: PartSpec) -> float | None:
    """Y of the section plane through the row with the most internal (top/bottom) features; None if pointless."""
    from app.schemas.frames import frames as pure_frames

    (x0, y0, z0), (x1, y1, z1) = spec.stock_bbox()
    frs = pure_frames(spec)
    spans: list[tuple[float, float, float]] = []  # (y_lo, y_hi, weight) in global Y
    for f in spec.features:
        if f.face not in ("top", "bottom"):
            continue
        fr, n0 = frs[f.face], len(spans)
        internal = not getattr(f, "through_all", False) or (isinstance(f, Hole) and (f.counterbore or f.countersink))
        w = 2.0 if internal else 1.0
        if isinstance(f, Hole):
            r = max(f.drill_diameter(), f.counterbore.diameter if f.counterbore else 0,
                    f.countersink.diameter if f.countersink else 0) / 2
            spans += [(y - r, y + r, w) for _, y in f.positions()]
        elif isinstance(f, (PocketRect, BossRect)):
            hy = _rot_half(f.length_x, f.width_y, f.rotation_deg)[1]
            spans += [(y - hy, y + hy, w) for _, y in f.positions()]
        elif isinstance(f, (PocketCircle, BossCircle)):
            spans += [(y - f.diameter / 2, y + f.diameter / 2, w) for _, y in f.positions()]
        elif isinstance(f, Slot):
            spans.append((min(f.start_y, f.end_y) - f.width / 2, max(f.start_y, f.end_y) + f.width / 2, w))
        elif isinstance(f, StepCut):
            spans.append((f.y_min, f.y_max, w))
        elif isinstance(f, PocketPolygon):
            ys = [p[1] for p in f.points_2d]
            spans.append((min(ys), max(ys), w))
        for i in range(n0, len(spans)):  # local v -> global Y (v2.0 bottom: v runs along -Y)
            a, b, w_ = spans[i]
            ga, gb = float(fr.point(0.0, a)[1]), float(fr.point(0.0, b)[1])
            spans[i] = (min(ga, gb), max(ga, gb), w_)
    if not any(w > 1.0 for *_, w in spans):
        return None  # nothing hidden inside: the outside views tell it all
    cands = {round((a + b) / 2, 4) for a, b, _ in spans} | {round((y0 + y1) / 2, 4)}
    cands = {c for c in cands if y0 + 0.2 < c < y1 - 0.2}
    mid = (y0 + y1) / 2
    return max(cands, key=lambda c: (sum(w for a, b, w in spans if a + 1e-6 < c < b - 1e-6), -abs(c - mid)), default=None)


def _rot_half(length: float, width: float, rot_deg: float) -> tuple[float, float]:
    a = math.radians(rot_deg)
    c, s = abs(math.cos(a)), abs(math.sin(a))
    return (length * c + width * s) / 2, (length * s + width * c) / 2


def _loops_xz(face) -> list[list[tuple[float, float]]]:
    """Closed loops of a planar face in (x, z), edges sampled and chained end to end."""
    loops = []
    for wire in [face.outer_wire(), *face.inner_wires()]:
        segs = []
        for e in wire.edges():
            n = 2 if (getattr(e.geom_type, "name", str(e.geom_type)) == "LINE") else max(12, int(e.length / 1.0) + 6)
            segs.append([(p.X, p.Z) for p in (e @ t for t in np.linspace(0.0, 1.0, n))])
        if not segs:
            continue
        loop = list(segs.pop(0))
        while segs:
            end = loop[-1]
            k = min(range(len(segs)), key=lambda i: min(math.dist(end, segs[i][0]), math.dist(end, segs[i][-1])))
            s = segs.pop(k)
            loop += (s if math.dist(end, s[0]) <= math.dist(end, s[-1]) else s[::-1])[1:]
        loops.append(loop)
    return loops


def _hatch(polys: list[list[tuple[float, float]]], spacing: float) -> list[tuple[tuple[float, float], tuple[float, float]]]:
    """45° hatch segments (sheet coords, y down) inside `polys` (even-odd: holes stay empty)."""
    edges = [(p[i], p[(i + 1) % len(p)]) for p in polys for i in range(len(p))]
    if not edges:
        return []
    vals = [x + y for p in polys for x, y in p]  # lines x + y = c run at 45° on the sheet (y down)
    out = []
    c = math.floor(min(vals) / (spacing * math.sqrt(2))) * spacing * math.sqrt(2)
    while c <= max(vals):
        hits = []
        for (ax, ay), (bx, by) in edges:
            fa, fb = ax + ay - c, bx + by - c
            if (fa > 0) != (fb > 0):
                t = fa / (fa - fb)
                hits.append((ax + (bx - ax) * t, ay + (by - ay) * t))
        hits.sort()
        out += [(hits[i], hits[i + 1]) for i in range(0, len(hits) - 1, 2)]
        c += spacing * math.sqrt(2)
    return out


def _place_section(sh: Sheet, spec: PartSpec, shape, layout: Layout, obs: _Obstacles, forbidden: Box,
                   yc: float) -> Box | None:
    from build123d import Align, Box as BBox, Compound, Location

    (x0, y0, z0), (x1, y1, z1) = spec.stock_bbox()
    bb = shape.bounding_box()
    cutter = BBox(bb.size.X + 2, bb.max.Y - yc + 1, bb.size.Z + 2, align=(Align.MIN, Align.MIN, Align.MIN)).moved(
        Location((bb.min.X - 1, yc, bb.min.Z - 1)))
    half = shape.intersect(cutter)
    if half is not None and not hasattr(half, "bounding_box"):  # build123d 0.13: ShapeList
        half = Compound(list(half)) if len(half) else None
    if half is None or not half.solids():
        return None
    vp = project_views(half)["front"]  # same direction as the front view; sections show no hidden lines
    cut_faces = [f for f in half.faces()
                 if abs(f.center().Y - yc) < 1e-4 and abs(abs(f.normal_at(f.center()).Y) - 1) < 1e-6]
    loops = [lp for f in cut_faces for lp in _loops_xz(f)]

    top, left = layout.placements["top"].rect, layout.placements["left"].rect
    fx0, fy0 = max(top[2] + 12, left[0] - 10), left[3] + 14
    fx1, fy1 = sh.inner[2] - 4, forbidden[1] - 6
    u0, v0, u1, v1 = vp.bbox
    w, h = u1 - u0, v1 - v0
    p = None
    for s in (sv for sv, _ in ISO_SCALES if sv <= layout.scale + 1e-9):
        if w * s < 25 or w * s > fx1 - fx0:
            continue
        for ay in (0.0, 0.5, 1.0):
            cx = fx0 + (fx1 - fx0) / 2
            cy = fy0 + h * s / 2 + max(0.0, fy1 - fy0 - 9 - h * s) * ay
            ox, oy = cx - (u0 + u1) / 2 * s, cy + (v0 + v1) / 2 * s
            cand = Placement("section", ox, oy, s, (ox + u0 * s, oy - v1 * s, ox + u1 * s, oy - v0 * s))
            label_box = (cand.rect[0], cand.rect[3], cand.rect[2], cand.rect[3] + 9)
            if cand.rect[3] + 9 <= fy1 and obs.free(cand.rect, pad=1.5) and obs.free(label_box, pad=0.5):
                p = cand
                break
        if p is not None:
            break
    if p is None:
        return None
    s = p.scale
    for pl in vp.visible:
        sh.polyline([p.to_sheet(u, v) for u, v in pl], width=sh.lines.thick, layer="VISIBLE")
    polys = [[p.to_sheet(x, z) for x, z in lp] for lp in loops]
    for a, b in _hatch(polys, HATCH_SPACING):
        sh.line(a, b, width=sh.lines.thin, layer="HATCH")
    scale_txt = "" if abs(s - layout.scale) < 1e-9 else f" ({next(lb for sv, lb in ISO_SCALES if sv == s)})"
    label_box = sh.text(f"SECTION A-A{scale_txt}", (p.rect[0] + p.rect[2]) / 2, p.rect[3] + 6.5, TEXT_DIM, "middle",
                        layer="NOTES")
    obs.add(p.rect)
    obs.add(label_box, blocks_leaders=True)

    # cutting plane in the top view (EN ISO 128-44): chain line, thick ends, arrows in the viewing direction (+Y)
    tp = layout.placements["top"]
    ya = tp.to_sheet(x0, yc)[1]
    xa, xb = tp.to_sheet(x0, yc)[0] - 7, tp.to_sheet(x1, yc)[0] + 7
    sh.line((xa + 6, ya), (xb - 6, ya), style="center", layer="CENTER")
    for xe, inward in ((xa, 1.0), (xb, -1.0)):
        sh.line((xe, ya), (xe + 6 * inward, ya), width=sh.lines.thick, layer="CENTER")
        sh.line((xe + 2 * inward, ya), (xe + 2 * inward, ya - 5), width=sh.lines.thin, layer="DIMENSIONS")
        sh.arrow((xe + 2 * inward, ya - 6.5), (xe + 2 * inward, ya))
        lb = sh.text("A", xe + 2 * inward - 2.5 * inward, ya - 8.5, TEXT_DIM, "middle", layer="NOTES")
        obs.add(lb, blocks_leaders=True)
    return p.rect


def _place_iso(sh: Sheet, iso: ViewProjection, layout: Layout, obs: _Obstacles, forbidden: Box) -> Box | None:
    top, left = layout.placements["top"].rect, layout.placements["left"].rect
    fx0 = max(top[2] + 12, left[0] - 10)
    fy0 = left[3] + 14
    fx1, fy1 = sh.inner[2] - 4, forbidden[1] - 6
    if fx1 - fx0 < 30 or fy1 - fy0 < 25:
        return None
    u0, v0, u1, v1 = iso.bbox
    w, h = u1 - u0, v1 - v0
    fit = min((fx1 - fx0) / max(w, 1e-6), (fy1 - fy0 - 8) / max(h, 1e-6))
    p = None
    for s in (sv for sv, _ in ISO_SCALES if sv <= min(fit, layout.scale)):  # largest free scale and position
        if w * s < 20:
            break
        for ax, ay in ((0.5, 0.5), (1.0, 0.5), (1.0, 0.0), (1.0, 1.0), (0.5, 0.0), (0.5, 1.0), (0.0, 0.5)):
            cx = fx0 + w * s / 2 + (fx1 - fx0 - w * s) * ax
            cy = fy0 + h * s / 2 + (fy1 - fy0 - 8 - h * s) * ay
            ox = cx - (u0 + u1) / 2 * s
            oy = cy + (v0 + v1) / 2 * s
            cand = Placement("iso", ox, oy, s, (ox + u0 * s, oy - v1 * s, ox + u1 * s, oy - v0 * s))
            label_box = (cand.rect[0], cand.rect[3], cand.rect[2], cand.rect[3] + 8)
            if obs.free(cand.rect, pad=1.5) and obs.free(label_box, pad=0.5):
                p = cand
                break
        if p is not None:
            break
    if p is None:
        return None
    s = p.scale
    for pl in iso.visible:
        sh.polyline([p.to_sheet(u, v) for u, v in pl], width=sh.lines.thick, layer="VISIBLE")
    label = "ISOMETRIC" + ("" if abs(s - layout.scale) < 1e-9 else f" ({next(lb for sv, lb in ISO_SCALES if sv == s)})")
    sh.text(label, (p.rect[0] + p.rect[2]) / 2, p.rect[3] + 5.5, TEXT_SMALL, "middle", layer="NOTES")
    obs.add(p.rect)
    _ = MARGIN
    return p.rect


# --------------------------------------------------------------------------- #
# Engine protocol (DRAWING_SPEC.md §4: secondary / fallback engines behind one interface)
# --------------------------------------------------------------------------- #


class DrawingEngine(Protocol):
    name: str
    description: str

    def available(self) -> bool: ...

    def render(self, spec: PartSpec, shape, out_dir: Path, **opts) -> DrawingResult: ...


class BuiltinEngine:
    name = "builtin"
    description = "Semantic build123d HLR + EN ISO sheet composer (this module)"

    def available(self) -> bool:
        return True

    def render(self, spec: PartSpec, shape, out_dir: Path, **opts) -> DrawingResult:
        return generate_drawing(spec, shape, out_dir, **opts)


class _ExternalEngine:
    """Placeholder for the optional engines named in the spec; reports itself unavailable until an adapter exists."""

    def __init__(self, name: str, description: str, probe):
        self.name, self.description, self._probe = name, description, probe

    def available(self) -> bool:
        return False  # no adapter yet, even when the tool itself is installed (see probe_installed)

    def probe_installed(self) -> bool:
        try:
            return bool(self._probe())
        except Exception:  # noqa: BLE001
            return False

    def render(self, spec: PartSpec, shape, out_dir: Path, **opts) -> DrawingResult:
        raise NotImplementedError(f"drawing engine {self.name!r} has no adapter yet")


def _has_module(mod: str) -> bool:
    import importlib.util

    return importlib.util.find_spec(mod) is not None


def _has_exe(exe: str) -> bool:
    import shutil

    return shutil.which(exe) is not None


ENGINES: dict[str, DrawingEngine] = {
    "builtin": BuiltinEngine(),
    "draftwright": _ExternalEngine("draftwright", "draftwright plug-in for complex_3d parts (optional)",
                                   lambda: _has_module("draftwright")),
    "freecad": _ExternalEngine("freecad", "FreeCAD headless TechDraw (future fallback for offset sections)",
                               lambda: _has_exe("freecadcmd")),
}


def get_engine(name: str | None) -> DrawingEngine:
    eng = ENGINES.get(name or "builtin")
    if eng is None or not eng.available():
        ok = [n for n, e in ENGINES.items() if e.available()]
        raise ValueError(f"drawing engine {name!r} is not available (available: {', '.join(ok)})")
    return eng