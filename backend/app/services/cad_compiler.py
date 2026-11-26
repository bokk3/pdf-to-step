"""Deterministic build123d compiler (Layer 1): PartSpec -> native B-Rep solid.

Every standard 2.5D prismatic feature is built here with fixed Python code,
no LLM involved. ``complex_3d`` features are delegated to the sandbox runner
(Layer 2) and only when ``requires_complex_3d_fallback`` is true and the
feature already carries code.

Deterministic build order:
  1. stock + outer profile (rectangle / rounded rectangle / polygon)
  2. edge treatments on the stock (fillets first, then chamfers)
  3. bosses (union)
  4. cuts in spec order: step_cut, pockets, slots, holes
  5. complex_3d bodies (cut / union)
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from build123d import (
    Align,
    Axis,
    Box,
    Circle,
    Compound,
    Cone,
    Cylinder,
    Face as BFace,
    Location,
    Plane,
    Pos,
    Rectangle,
    RectangleRounded,
    Rot,
    Shape,
    SlotCenterToCenter,
    Solid,
    Vector,
    Wire,
    export_step,
    extrude,
)

from app.schemas.part_spec import (
    CUT_TYPES,
    BossCircle,
    BossRect,
    Complex3D,
    EdgeTreatment,
    FeatureBuildStatus,
    Hole,
    PartSpec,
    PocketCircle,
    PocketPolygon,
    PocketRect,
    Slot,
    StepCut,
)

EPS = 0.05  # tool overshoot (mm) so tools never share a face with the stock
C_C_MAX = (Align.CENTER, Align.CENTER, Align.MAX)


class CompileError(RuntimeError):
    pass


# --------------------------------------------------------------------------- #
# Face frames
# --------------------------------------------------------------------------- #

X, Y, Z = Vector(1, 0, 0), Vector(0, 1, 0), Vector(0, 0, 1)


@dataclass
class FaceFrame:
    origin: Vector  # point with face coords (0, 0)
    u_dir: Vector
    v_dir: Vector
    normal: Vector  # outward
    u_range: tuple[float, float]
    v_range: tuple[float, float]
    thickness: float  # material depth behind this face (stock only)

    def location(self, u: float, v: float) -> Location:
        p = self.origin + self.u_dir * u + self.v_dir * v
        return Plane(origin=p, x_dir=self.u_dir, z_dir=self.normal).location

    @property
    def v_sign(self) -> float:
        """+1 if the local plane Y equals +v on this face, -1 if mirrored (handled internally)."""
        return 1.0 if self.normal.cross(self.u_dir).dot(self.v_dir) > 0 else -1.0


def face_frames(spec: PartSpec) -> dict[str, FaceFrame]:
    (x0, y0, z0), (x1, y1, z1) = spec.stock_bbox()
    return {
        "top": FaceFrame(Vector(0, 0, z1), X, Y, Z, (x0, x1), (y0, y1), z1 - z0),
        "bottom": FaceFrame(Vector(0, 0, z0), X, Y, -Z, (x0, x1), (y0, y1), z1 - z0),
        "front": FaceFrame(Vector(0, y0, 0), X, Z, -Y, (x0, x1), (z0, z1), y1 - y0),
        "back": FaceFrame(Vector(0, y1, 0), X, Z, Y, (x0, x1), (z0, z1), y1 - y0),
        "left": FaceFrame(Vector(x0, 0, 0), Y, Z, -X, (y0, y1), (z0, z1), x1 - x0),
        "right": FaceFrame(Vector(x1, 0, 0), Y, Z, X, (y0, y1), (z0, z1), x1 - x0),
    }


# --------------------------------------------------------------------------- #
# Stock
# --------------------------------------------------------------------------- #


def _polygon_face(points: list[tuple[float, float]], corner_radius: float = 0.0) -> BFace:
    pts = [Vector(x, y, 0) for x, y in points]
    face = BFace(Wire.make_polygon(pts, close=True))
    if face.normal_at().Z < 0:
        face = BFace(Wire.make_polygon(list(reversed(pts)), close=True))
    if corner_radius > 0:
        face = face.fillet_2d(corner_radius, face.vertices())
    return face


def build_stock(spec: PartSpec) -> Shape:
    (x0, y0, z0), (x1, y1, z1) = spec.stock_bbox()
    L, W, T = x1 - x0, y1 - y0, z1 - z0
    prof = spec.outer_profile
    if prof.type == "polygon":
        face = _polygon_face(list(prof.points_2d or []), prof.corner_radius)
        return extrude(Pos(0, 0, z0) * face, amount=T)
    if prof.type == "rounded_rectangle":
        if 2 * prof.corner_radius >= min(L, W) - 1e-9:
            raise CompileError("outer_profile corner_radius too large for the stock")
        sk = Pos((x0 + x1) / 2, (y0 + y1) / 2, z0) * RectangleRounded(L, W, prof.corner_radius)
        return extrude(sk, amount=T)
    return Pos(x0, y0, z0) * Box(L, W, T, align=(Align.MIN, Align.MIN, Align.MIN))


def _select_edges(shape: Shape, target: str):
    edges = shape.edges()
    if target == "all_outer_edges":
        return edges
    if target == "vertical_outer_edges":
        return edges.filter_by(Axis.Z)
    groups = edges.group_by(Axis.Z)
    return groups[-1] if target == "top_outer_edges" else groups[0]


def apply_edge_treatment(shape: Shape, et: EdgeTreatment) -> Shape:
    edges = _select_edges(shape, et.target)
    if not edges:
        raise CompileError(f"{et.id}: no edges matched '{et.target}'")
    solid = _single_solid(shape)
    if et.type == "fillet":
        return solid.fillet(et.size, edges)
    return solid.chamfer(et.size, None, edges)


# --------------------------------------------------------------------------- #
# Tools (built in a local frame: XY = face plane, +Z = outward normal)
# --------------------------------------------------------------------------- #


def _rect_profile(length: float, width: float, corner_radius: float):
    if corner_radius <= 1e-9:
        return Rectangle(length, width)
    if abs(2 * corner_radius - min(length, width)) < 1e-6:  # full-round ends
        if length >= width:
            return SlotCenterToCenter(max(length - width, 1e-3), width)
        return SlotCenterToCenter(max(width - length, 1e-3), length, rotation=90)
    return RectangleRounded(length, width, corner_radius)


def _prism(profile, depth: float, frame: FaceFrame, u: float, v: float, rot_deg: float = 0.0) -> Shape:
    """Extrude a local profile from +EPS down to -depth, then place it at (u, v) on the face."""
    sk = Rot(0, 0, frame.v_sign * rot_deg) * profile
    tool = extrude(Pos(0, 0, EPS) * sk, amount=depth + EPS, dir=(0, 0, -1))
    return tool.moved(frame.location(u, v))


def _depth(feature, frame: FaceFrame, current: Shape) -> float:
    if getattr(feature, "through_all", False) or getattr(feature, "depth", None) is None:
        return current.bounding_box().diagonal + 2 * EPS
    return float(feature.depth)


def hole_tools(h: Hole, frame: FaceFrame, current: Shape) -> list[Shape]:
    r = h.drill_diameter() / 2
    depth = _depth(h, frame, current)
    local: list[Shape] = [Pos(0, 0, EPS) * Cylinder(r, depth + EPS, align=C_C_MAX)]
    if not h.through_all and h.tip_angle_deg:
        tip = r / math.tan(math.radians(h.tip_angle_deg) / 2)
        local.append(Pos(0, 0, -depth) * Cone(0, r, tip, align=C_C_MAX))
    if h.counterbore:
        local.append(Pos(0, 0, EPS) * Cylinder(h.counterbore.diameter / 2, h.counterbore.depth + EPS, align=C_C_MAX))
    if h.countersink:
        cr = h.countersink.diameter / 2
        cone_h = (cr - r) / math.tan(math.radians(h.countersink.angle_deg) / 2)
        local.append(Cone(r, cr, cone_h, align=C_C_MAX))
        local.append(Pos(0, 0, EPS) * Cylinder(cr, EPS, align=C_C_MAX))
    tool = local[0] if len(local) == 1 else local[0].fuse(*local[1:])
    return [tool.moved(frame.location(x, y)) for x, y in h.positions()]


def pocket_rect_tools(p: PocketRect, frame: FaceFrame, current: Shape) -> list[Shape]:
    prof = _rect_profile(p.length_x, p.width_y, p.corner_radius)
    d = _depth(p, frame, current)
    return [_prism(prof, d, frame, x, y, p.rotation_deg) for x, y in p.positions()]


def pocket_circle_tools(p: PocketCircle, frame: FaceFrame, current: Shape) -> list[Shape]:
    d = _depth(p, frame, current)
    return [_prism(Circle(p.diameter / 2), d, frame, x, y) for x, y in p.positions()]


def pocket_polygon_tools(p: PocketPolygon, frame: FaceFrame, current: Shape) -> list[Shape]:
    s = frame.v_sign
    face = _polygon_face([(x, s * y) for x, y in p.points_2d], p.corner_radius)
    return [_prism(face, _depth(p, frame, current), frame, 0.0, 0.0)]


def slot_tools(s: Slot, frame: FaceFrame, current: Shape) -> list[Shape]:
    cx, cy = (s.start_x + s.end_x) / 2, (s.start_y + s.end_y) / 2
    angle = math.degrees(math.atan2(s.end_y - s.start_y, s.end_x - s.start_x))
    prof = Circle(s.width / 2) if s.length < 1e-6 else SlotCenterToCenter(s.length, s.width)
    return [_prism(prof, _depth(s, frame, current), frame, cx, cy, angle)]


def step_cut_tools(sc: StepCut, frame: FaceFrame, current: Shape) -> list[Shape]:
    # Grow the box past every stock edge it touches so the cut is open there (no sliver walls).
    (u0, u1), (v0, v1) = frame.u_range, frame.v_range
    m = 1.0
    xa = sc.x_min - m if sc.x_min <= u0 + 1e-6 else sc.x_min
    xb = sc.x_max + m if sc.x_max >= u1 - 1e-6 else sc.x_max
    ya = sc.y_min - m if sc.y_min <= v0 + 1e-6 else sc.y_min
    yb = sc.y_max + m if sc.y_max >= v1 - 1e-6 else sc.y_max
    L, W = xb - xa, yb - ya
    r = min(sc.corner_radius, min(L, W) / 2 - 1e-3) if sc.corner_radius > 0 else 0.0
    return [_prism(_rect_profile(L, W, r), sc.depth, frame, (xa + xb) / 2, (ya + yb) / 2)]


def boss_tool(b: BossRect | BossCircle, frame: FaceFrame) -> list[Shape]:
    if isinstance(b, BossCircle):
        prof, rot = Circle(b.diameter / 2), 0.0
    else:
        prof, rot = _rect_profile(b.length_x, b.width_y, b.corner_radius), b.rotation_deg
    sk = Rot(0, 0, frame.v_sign * rot) * prof
    local = extrude(Pos(0, 0, -EPS) * sk, amount=b.height + EPS, dir=(0, 0, 1))
    return [local.moved(frame.location(x, y)) for x, y in b.positions()]


CUT_BUILDERS: dict[type, Callable] = {
    StepCut: step_cut_tools,
    PocketRect: pocket_rect_tools,
    PocketCircle: pocket_circle_tools,
    PocketPolygon: pocket_polygon_tools,
    Slot: slot_tools,
    Hole: hole_tools,
}


# --------------------------------------------------------------------------- #
# Booleans
# --------------------------------------------------------------------------- #


def _single_solid(shape: Shape) -> Solid:
    if isinstance(shape, Solid):
        return shape
    solids = shape.solids()
    if len(solids) != 1:
        raise CompileError(f"expected exactly one solid, found {len(solids)}")
    return solids[0]


def _cut(current: Shape, tools: list[Shape], fid: str) -> Shape:
    before = float(current.volume)
    result = current.cut(*tools)
    after = float(result.volume)
    if after >= before - 1e-9:
        raise CompileError(f"{fid}: cut removed no material (feature outside the part?)")
    if after <= 1e-9:
        raise CompileError(f"{fid}: cut removed the entire part")
    return result


def _union(current: Shape, tools: list[Shape], fid: str) -> Shape:
    result = current.fuse(*tools)
    if len(result.solids()) != 1:
        raise CompileError(f"{fid}: union produced a disconnected body (boss not touching the part?)")
    return result


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #

ComplexRunner = Callable[[Complex3D], Shape]


@dataclass
class CompileResult:
    shape: Shape
    feature_status: list[FeatureBuildStatus] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def failed(self) -> list[FeatureBuildStatus]:
        return [s for s in self.feature_status if not s.ok]


def compile_part(spec: PartSpec, complex_runner: ComplexRunner | None = None, strict: bool = False) -> CompileResult:
    """Compile a PartSpec into one B-Rep solid (mm, datum frame).

    Feature failures are recorded (not raised) so the pipeline can self-heal,
    unless ``strict`` is True. Stock/profile errors always raise.
    """
    current: Shape = build_stock(spec)
    frames = face_frames(spec)
    statuses: list[FeatureBuildStatus] = []
    warnings: list[str] = []

    def run(fid: str, method: str, fn: Callable[[], Shape]) -> None:
        nonlocal current
        try:
            current = fn()
            statuses.append(FeatureBuildStatus(feature_id=fid, ok=True, method=method))  # type: ignore[arg-type]
        except Exception as exc:  # noqa: BLE001 - every OCCT failure is captured for self-healing
            if strict:
                raise
            statuses.append(FeatureBuildStatus(feature_id=fid, ok=False, method=method,  # type: ignore[arg-type]
                                               error=f"{type(exc).__name__}: {exc}"))

    for et in sorted(spec.edge_treatments, key=lambda e: 0 if e.type == "fillet" else 1):
        run(et.id, "deterministic", lambda et=et: apply_edge_treatment(current, et))

    for f in spec.features:
        if isinstance(f, (BossRect, BossCircle)):
            run(f.id, "deterministic", lambda f=f: _union(current, boss_tool(f, frames[f.face]), f.id))

    order = {StepCut: 0, PocketRect: 1, PocketCircle: 1, PocketPolygon: 1, Slot: 2, Hole: 3}
    cuts = sorted((f for f in spec.features if isinstance(f, CUT_TYPES)), key=lambda f: order[type(f)])
    for f in cuts:
        builder = CUT_BUILDERS[type(f)]
        run(f.id, "deterministic", lambda f=f, b=builder: _cut(current, b(f, frames[f.face], current), f.id))

    for f in spec.complex_features():
        if not spec.requires_complex_3d_fallback or not f.code or complex_runner is None:
            reason = ("complex_3d feature has no generated code" if not f.code
                      else "no sandbox runner available" if complex_runner is None
                      else "requires_complex_3d_fallback is false")
            statuses.append(FeatureBuildStatus(feature_id=f.id, ok=False, method="skipped", error=reason))
            continue

        def _complex(f=f) -> Shape:
            body = complex_runner(f)
            return _cut(current, [body], f.id) if f.operation == "cut" else _union(current, [body], f.id)

        run(f.id, "llm_sandbox", _complex)

    solids = current.solids()
    if len(solids) == 1:
        current = solids[0]
    else:
        warnings.append(f"result has {len(solids)} solids")
        current = Compound(solids)
    current.label = spec.part_name or "part"
    return CompileResult(shape=current, feature_status=statuses, warnings=warnings)


# --------------------------------------------------------------------------- #
# STEP export (optionally with XCAF face colours, §8.6)
# --------------------------------------------------------------------------- #

STEP_SCHEMAS = {"AP214": "AP214IS", "AP242": "AP242DIS", "AP203": "AP203"}


def set_step_schema(schema: str = "AP214") -> None:
    from OCP.Interface import Interface_Static
    from OCP.STEPCAFControl import STEPCAFControl_Controller
    from OCP.STEPControl import STEPControl_Controller

    STEPCAFControl_Controller.Init_s()  # registers the static parameters (idempotent)
    STEPControl_Controller.Init_s()
    Interface_Static.SetCVal_s("write.step.schema", STEP_SCHEMAS.get(schema.upper(), "AP214IS"))


def export_step_file(shape: Shape, path: Path, schema: str = "AP214",
                     face_colors: dict[tuple[float, float, float], list] | None = None) -> Path:
    """Write a STEP file. With ``face_colors`` ({rgb: [build123d Face, ...]}) faces are coloured via XCAF."""
    path.parent.mkdir(parents=True, exist_ok=True)
    set_step_schema(schema)
    if face_colors:
        _export_step_xcaf(shape, path, face_colors)
    else:
        ok = export_step(shape, str(path))
        if ok is False:
            raise CompileError(f"STEP export failed: {path}")
    if not path.exists() or path.stat().st_size == 0:
        raise CompileError(f"STEP export failed: {path}")
    return path


def _export_step_xcaf(shape: Shape, path: Path, face_colors: dict) -> None:
    """XDE document with the solid + coloured sub-faces (mirrors build123d's own XDE setup)."""
    from OCP.IFSelect import IFSelect_ReturnStatus
    from OCP.Interface import Interface_Static
    from OCP.Quantity import Quantity_Color, Quantity_TypeOfColor
    from OCP.STEPCAFControl import STEPCAFControl_Controller, STEPCAFControl_Writer
    from OCP.STEPControl import STEPControl_Controller, STEPControl_StepModelType
    from OCP.TCollection import TCollection_ExtendedString
    from OCP.TDataStd import TDataStd_Name
    from OCP.TDocStd import TDocStd_Document
    from OCP.XCAFApp import XCAFApp_Application
    from OCP.XCAFDoc import XCAFDoc_ColorType, XCAFDoc_DocumentTool
    from OCP.XSControl import XSControl_WorkSession

    doc = TDocStd_Document(TCollection_ExtendedString("XmlOcaf"))
    app = XCAFApp_Application.GetApplication_s()
    app.NewDocument(TCollection_ExtendedString("MDTV-XCAF"), doc)
    app.InitDocument(doc)
    XCAFDoc_DocumentTool.SetLengthUnit_s(doc, 0.001)  # millimetres
    shape_tool = XCAFDoc_DocumentTool.ShapeTool_s(doc.Main())
    color_tool = XCAFDoc_DocumentTool.ColorTool_s(doc.Main())
    label = shape_tool.AddShape(shape.wrapped, False)
    TDataStd_Name.Set_s(label, TCollection_ExtendedString(shape.label or "part"))
    for (r, g, b), faces in face_colors.items():
        color = Quantity_Color(r, g, b, Quantity_TypeOfColor.Quantity_TOC_RGB)
        for face in faces:
            sub = shape_tool.AddSubShape(label, face.wrapped)
            if not sub.IsNull():
                color_tool.SetColor(sub, color, XCAFDoc_ColorType.XCAFDoc_ColorSurf)
    shape_tool.UpdateAssemblies()

    from OCP.Message import Message, Message_Gravity

    for printer in Message.DefaultMessenger_s().Printers():  # silence OCCT transfer statistics
        printer.SetTraceLevel(Message_Gravity.Message_Fail)
    STEPCAFControl_Controller.Init_s()
    STEPControl_Controller.Init_s()
    Interface_Static.SetIVal_s("write.surfacecurve.mode", 1)
    writer = STEPCAFControl_Writer(XSControl_WorkSession(), False)
    writer.SetColorMode(True)
    writer.SetNameMode(True)
    writer.Transfer(doc, STEPControl_StepModelType.STEPControl_AsIs)
    if writer.Write(str(path)) != IFSelect_ReturnStatus.IFSelect_RetDone:
        raise CompileError(f"STEP (XCAF) export failed: {path}")
