"""AP214 CAM face colours via OCCT XCAF (SPEC_V1_5_CAM_SHOPFLOOR.md §2.2). Deterministic, no LLM.

Faces of the single compiled solid are matched against the PartSpec by geometric signature (0.05 mm):

    thread       bore cylinders of threaded holes (modelled at the tap drill)
    tight_fit    bore cylinders of holes with an ISO fit of IT <= 7 (H7, H6, g6 ...) or a band <= 0.04 mm (±0.02)
    seat         counterbore cylinder + floor, countersink cone
    fine_finish  faces of features listed in drawing_metadata.surface_finishes with Ra <= 1.6 µm
    datum        the primary datum face (top face for *_top_face datums, else the bottom face)
    body         everything else (set on the solid; faces above override it)

The solid stays one watertight TopoDS_Solid: colours are XCAFDoc_ColorSurf attributes on sub-shape labels written
by STEPCAFControl_Writer (never a mesh, never loose shells). ``face_rgb`` gives the same mapping to the GLB.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field

import numpy as np
from pydantic import BaseModel, ConfigDict, Field

from app.config import Settings
from app.schemas.part_spec import Hole, PartSpec, PocketCircle
from app.services.shop_config import load_config

CAM_COLORS_FILE = "cam_colors.json"
TOL = 0.05  # mm, spec §2.2
PRIORITY = ("thread", "tight_fit", "seat", "fine_finish", "datum")


class CamColor(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str
    rgb: tuple[int, int, int]
    meaning: str = ""

    @property
    def unit(self) -> tuple[float, float, float]:
        return tuple(c / 255.0 for c in self.rgb)  # type: ignore[return-value]

    @property
    def hex(self) -> str:
        return "#" + "".join(f"{c:02X}" for c in self.rgb)


class CamPalette(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_version: str = "1.0"
    tight_max_it: int = Field(7, ge=1, le=18)
    tight_max_band_mm: float = Field(0.04, gt=0)
    fine_finish_max_ra_um: float = Field(1.6, gt=0)
    colors: dict[str, CamColor]


def load_palette(s: Settings) -> CamPalette:
    return load_config(s, CAM_COLORS_FILE, CamPalette)


FIT_RE = re.compile(r"(?<![A-Za-z])([A-Za-z]{1,2})\s*(\d{1,2})(?![\d.,])")


def is_tight_fit(tol: str | None, pal: CamPalette) -> bool:
    if not tol:
        return False
    m = FIT_RE.search(tol)
    if m and int(m.group(2)) <= pal.tight_max_it:
        return True
    from app.services.verifier import tolerance_band

    band = tolerance_band(FIT_RE.sub("", tol))
    return band is not None and band <= pal.tight_max_band_mm + 1e-9


@dataclass
class FaceColoring:
    faces: list[tuple[object, str]] = field(default_factory=list)  # (build123d Face, category)
    palette: CamPalette | None = None

    @property
    def counts(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for _, cat in self.faces:
            out[cat] = out.get(cat, 0) + 1
        return out

    def by_rgb(self) -> dict[tuple[float, float, float], list]:
        out: dict[tuple[float, float, float], list] = {}
        for f, cat in self.faces:
            out.setdefault(self.palette.colors[cat].unit, []).append(f)  # type: ignore[union-attr]
        return out

    def face_rgb(self) -> list[tuple[object, tuple[int, int, int]]]:
        """(TopoDS_Face, rgb 0-255) for the GLB; faces not listed get the body colour."""
        return [(f.wrapped, self.palette.colors[cat].rgb) for f, cat in self.faces]  # type: ignore[union-attr]

    @property
    def body_rgb(self) -> tuple[int, int, int]:
        return self.palette.colors["body"].rgb  # type: ignore[union-attr]


def _surface_info(face):
    from OCP.BRepAdaptor import BRepAdaptor_Surface
    from OCP.GeomAbs import GeomAbs_Cone, GeomAbs_Cylinder, GeomAbs_Plane

    ad = BRepAdaptor_Surface(face.wrapped)
    t = ad.GetType()
    if t == GeomAbs_Cylinder:
        c = ad.Cylinder()
        ax = c.Axis()
        return "cyl", np.array([ax.Location().X(), ax.Location().Y(), ax.Location().Z()]), \
            np.array([ax.Direction().X(), ax.Direction().Y(), ax.Direction().Z()]), c.Radius()
    if t == GeomAbs_Cone:
        c = ad.Cone()
        ax = c.Axis()
        return "cone", np.array([ax.Location().X(), ax.Location().Y(), ax.Location().Z()]), \
            np.array([ax.Direction().X(), ax.Direction().Y(), ax.Direction().Z()]), c.RefRadius()
    if t == GeomAbs_Plane:
        pl = ad.Plane()
        n = pl.Axis().Direction()
        return "plane", None, np.array([n.X(), n.Y(), n.Z()]), 0.0
    return "other", None, None, 0.0


def _t(v) -> tuple[float, float, float]:
    return (float(v.X), float(v.Y), float(v.Z))


def _axis_dist(p: np.ndarray, loc: np.ndarray, d: np.ndarray) -> float:
    v = p - loc
    return float(np.linalg.norm(v - np.dot(v, d) * d))


def _inside(face, lo: np.ndarray, hi: np.ndarray) -> bool:
    bb = face.bounding_box()
    return all(_t(bb.min)[i] >= lo[i] - TOL and _t(bb.max)[i] <= hi[i] + TOL for i in range(3))


def classify_faces(shape, spec: PartSpec, palette: CamPalette) -> FaceColoring:
    from app.services.cad_compiler import face_frames

    frames = face_frames(spec)
    faces = list(shape.faces())
    info = [_surface_info(f) for f in faces]
    cats: dict[int, str] = {}

    def mark(i: int, cat: str) -> None:
        if i not in cats or PRIORITY.index(cat) < PRIORITY.index(cats[i]):
            cats[i] = cat

    fine = {sf.feature_id for sf in spec.drawing_metadata.surface_finishes
            if sf.ra_um <= palette.fine_finish_max_ra_um + 1e-9}

    for f in spec.features:
        if f.face not in frames:
            continue
        fr = frames[f.face]
        n = np.array([fr.normal.X, fr.normal.Y, fr.normal.Z])
        if isinstance(f, Hole):
            r_bore = f.drill_diameter() / 2
            bore_cat = "thread" if f.thread else "tight_fit" if is_tight_fit(f.tolerance, palette) else (
                "fine_finish" if f.id in fine else None)
            for x, y in f.positions():
                p0 = fr.origin + fr.u_dir * x + fr.v_dir * y
                P = np.array([p0.X, p0.Y, p0.Z])
                for i, (kind, loc, d, r) in enumerate(info):
                    if kind in ("cyl", "cone") and abs(abs(float(np.dot(d, n))) - 1) < 1e-6 and _axis_dist(P, loc, d) < TOL:
                        if kind == "cyl" and abs(r - r_bore) < TOL and bore_cat:
                            mark(i, bore_cat)
                        elif kind == "cyl" and f.counterbore and abs(r - f.counterbore.diameter / 2) < TOL:
                            mark(i, "seat")
                        elif kind == "cone" and f.countersink:
                            bb = faces[i].bounding_box()
                            if float(np.dot(np.array(_t(bb.center())) - P, n)) > -(f.countersink.diameter / 2 + TOL):
                                mark(i, "seat")  # the entry cone, not the drill tip
                    elif kind == "plane" and f.counterbore and abs(abs(float(np.dot(d, n))) - 1) < 1e-6:
                        c = np.array(_t(faces[i].center()))
                        depth = float(np.dot(P - c, n))
                        if abs(depth - f.counterbore.depth) < TOL and _axis_dist(c, P, n) < TOL:
                            mark(i, "seat")  # counterbore floor (annulus centred on the axis)
        elif f.id in fine:
            # any other feature with a fine finish: the faces inside its cut volume
            lo, hi = _feature_box(f, fr, frames)
            if lo is not None:
                for i, face in enumerate(faces):
                    if _inside(face, lo, hi):
                        mark(i, "fine_finish")

    # primary datum face: the stock face at Z0 of the datum (top for *_top_face datums)
    (x0, y0, z0), (x1, y1, z1) = spec.stock_bbox()
    top = spec.datum_origin.endswith("top_face")
    zd = z1 if top else z0
    for i, (kind, _, d, _) in enumerate(info):
        if kind == "plane" and abs(abs(d[2]) - 1) < 1e-6:  # horizontal plane (axis sign depends on orientation)
            bb = faces[i].bounding_box()
            if abs(bb.min.Z - zd) < TOL and abs(bb.max.Z - zd) < TOL and bb.size.X > 0.5 * (x1 - x0):
                mark(i, "datum")
    return FaceColoring([(faces[i], c) for i, c in sorted(cats.items())], palette)


def _feature_box(f, fr, frames):
    """Axis-aligned box of a pocket / slot / step cut (global coordinates) for fine-finish colouring."""
    from app.schemas.part_spec import PocketPolygon, PocketRect, Slot, StepCut

    n = np.array([fr.normal.X, fr.normal.Y, fr.normal.Z])
    depth = fr.thickness if getattr(f, "through_all", False) else float(getattr(f, "depth", 0) or 0)
    pts: list[tuple[float, float]] = []
    if isinstance(f, PocketRect):
        h = math.hypot(f.length_x, f.width_y) / 2
        pts = [(x + sx * h, y + sy * h) for x, y in f.positions() for sx in (-1, 1) for sy in (-1, 1)]
    elif isinstance(f, PocketCircle):
        pts = [(x + sx * f.diameter / 2, y + sy * f.diameter / 2) for x, y in f.positions() for sx in (-1, 1) for sy in (-1, 1)]
    elif isinstance(f, Slot):
        w = f.width / 2
        pts = [(px + sx * w, py + sy * w) for px, py in ((f.start_x, f.start_y), (f.end_x, f.end_y))
               for sx in (-1, 1) for sy in (-1, 1)]
    elif isinstance(f, PocketPolygon):
        pts = [tuple(p) for p in f.points_2d]
    elif isinstance(f, StepCut):
        pts = [(f.x_min, f.y_min), (f.x_max, f.y_max)]
    if not pts:
        return None, None
    corners = []
    for u, v in pts:
        p = fr.origin + fr.u_dir * u + fr.v_dir * v
        P = np.array([p.X, p.Y, p.Z])
        corners += [P, P - n * depth]
    arr = np.array(corners)
    return arr.min(axis=0), arr.max(axis=0)


def export_colored_step(shape, path, coloring: FaceColoring, schema: str = "AP214", name: str = "part"):
    """STEPCAFControl_Writer export: body colour on the solid, CAM colours on matched faces (XCAFDoc_ColorSurf)."""
    solid = shape.solids()[0] if len(shape.solids()) == 1 else shape
    body = coloring.body_rgb if coloring.palette is not None else None
    faces = [(f, coloring.palette.colors[cat].rgb) for f, cat in coloring.faces] if coloring.palette else []
    return export_bodies_step([(name, solid, body, faces)], path, schema)


def export_bodies_step(bodies, path, schema: str = "AP214"):
    """Several named top-level solids in one AP214 file (softjaws: Fixed_Jaw + Movable_Jaw).

    ``bodies`` = [(name, shape, body_rgb | None, [(build123d Face, rgb), ...]), ...]."""
    from OCP.IFSelect import IFSelect_ReturnStatus
    from OCP.Interface import Interface_Static
    from OCP.Message import Message, Message_Gravity
    from OCP.Quantity import Quantity_Color, Quantity_TypeOfColor
    from OCP.STEPCAFControl import STEPCAFControl_Controller, STEPCAFControl_Writer
    from OCP.STEPControl import STEPControl_Controller, STEPControl_StepModelType
    from OCP.TCollection import TCollection_ExtendedString
    from OCP.TDataStd import TDataStd_Name
    from OCP.TDocStd import TDocStd_Document
    from OCP.XCAFApp import XCAFApp_Application
    from OCP.XCAFDoc import XCAFDoc_ColorType, XCAFDoc_DocumentTool
    from OCP.XSControl import XSControl_WorkSession

    from app.services.cad_compiler import CompileError, set_step_schema

    path.parent.mkdir(parents=True, exist_ok=True)
    set_step_schema(schema)
    doc = TDocStd_Document(TCollection_ExtendedString("MDTV-XCAF"))
    app = XCAFApp_Application.GetApplication_s()
    app.NewDocument(TCollection_ExtendedString("MDTV-XCAF"), doc)
    app.InitDocument(doc)
    XCAFDoc_DocumentTool.SetLengthUnit_s(doc, 0.001)  # millimetres
    shape_tool = XCAFDoc_DocumentTool.ShapeTool_s(doc.Main())
    color_tool = XCAFDoc_DocumentTool.ColorTool_s(doc.Main())
    # sRGB: OCCT treats TOC_RGB as linear and converts on write; CAM systems read the STEP numbers as sRGB,
    # so sRGB in = the exact palette values (0, 102, 255) in the file.
    rgb = lambda c: Quantity_Color(c[0] / 255, c[1] / 255, c[2] / 255, Quantity_TypeOfColor.Quantity_TOC_sRGB)  # noqa: E731
    for name, solid, body_rgb, faces in bodies:
        label = shape_tool.AddShape(solid.wrapped, False)
        TDataStd_Name.Set_s(label, TCollection_ExtendedString(name))
        if body_rgb is not None:
            color_tool.SetColor(label, rgb(body_rgb), XCAFDoc_ColorType.XCAFDoc_ColorSurf)
        for face, frgb in faces:
            sub = shape_tool.AddSubShape(label, face.wrapped)
            if not sub.IsNull():
                color_tool.SetColor(sub, rgb(frgb), XCAFDoc_ColorType.XCAFDoc_ColorSurf)
    shape_tool.UpdateAssemblies()
    for printer in Message.DefaultMessenger_s().Printers():  # silence OCCT transfer statistics
        printer.SetTraceLevel(Message_Gravity.Message_Fail)
    STEPCAFControl_Controller.Init_s()
    STEPControl_Controller.Init_s()
    Interface_Static.SetCVal_s("write.step.schema", schema)
    Interface_Static.SetIVal_s("write.surfacecurve.mode", 1)
    writer = STEPCAFControl_Writer(XSControl_WorkSession(), False)
    writer.SetColorMode(True)
    writer.SetNameMode(True)
    writer.Transfer(doc, STEPControl_StepModelType.STEPControl_AsIs)
    if writer.Write(str(path)) != IFSelect_ReturnStatus.IFSelect_RetDone:
        raise CompileError(f"STEP (XCAF) export failed: {path}")
    return path


def read_step_colors(path) -> tuple[int, list[tuple[int, int, int]]]:
    """STEPCAFControl_Reader: (solid count, surface colours of all faces, 0-255) - for tests and checks."""
    from OCP.IFSelect import IFSelect_ReturnStatus
    from OCP.Quantity import Quantity_Color
    from OCP.STEPCAFControl import STEPCAFControl_Reader
    from OCP.TCollection import TCollection_ExtendedString
    from OCP.Quantity import Quantity_TypeOfColor
    from OCP.TDF import TDF_ChildIterator
    from OCP.TDocStd import TDocStd_Document
    from OCP.TopAbs import TopAbs_FACE, TopAbs_SOLID
    from OCP.TopExp import TopExp_Explorer
    from OCP.XCAFApp import XCAFApp_Application
    from OCP.XCAFDoc import XCAFDoc_ColorType, XCAFDoc_DocumentTool

    doc = TDocStd_Document(TCollection_ExtendedString("MDTV-XCAF"))
    XCAFApp_Application.GetApplication_s().InitDocument(doc)
    reader = STEPCAFControl_Reader()
    reader.SetColorMode(True)
    if reader.ReadFile(str(path)) != IFSelect_ReturnStatus.IFSelect_RetDone or not reader.Transfer(doc):
        raise ValueError(f"cannot read {path}")
    shape_tool = XCAFDoc_DocumentTool.ShapeTool_s(doc.Main())
    color_tool = XCAFDoc_DocumentTool.ColorTool_s(doc.Main())
    solids, colors = 0, []
    it = TDF_ChildIterator(shape_tool.Label(), False)
    tops = []
    while it.More():
        if shape_tool.IsTopLevel(it.Value()) and not shape_tool.IsAssembly_s(it.Value()):
            tops.append(it.Value())
        it.Next()
    for lab in tops:
        shp = shape_tool.GetShape_s(lab)
        ex = TopExp_Explorer(shp, TopAbs_SOLID)
        while ex.More():
            solids += 1
            ex.Next()
        ex = TopExp_Explorer(shp, TopAbs_FACE)
        while ex.More():
            c = Quantity_Color()
            if color_tool.GetColor(ex.Current(), XCAFDoc_ColorType.XCAFDoc_ColorSurf, c):
                r, g, b = c.Values(Quantity_TypeOfColor.Quantity_TOC_sRGB)
                colors.append((int(round(r * 255)), int(round(g * 255)), int(round(b * 255))))
            ex.Next()
    return solids, colors


def read_step_names(path) -> list[str]:
    """Names of the top-level shapes in a STEP file (STEPCAFControl_Reader, name mode)."""
    from OCP.IFSelect import IFSelect_ReturnStatus
    from OCP.STEPCAFControl import STEPCAFControl_Reader
    from OCP.TCollection import TCollection_ExtendedString
    from OCP.TDataStd import TDataStd_Name
    from OCP.TDF import TDF_ChildIterator
    from OCP.TDocStd import TDocStd_Document
    from OCP.XCAFApp import XCAFApp_Application
    from OCP.XCAFDoc import XCAFDoc_DocumentTool

    doc = TDocStd_Document(TCollection_ExtendedString("MDTV-XCAF"))
    XCAFApp_Application.GetApplication_s().InitDocument(doc)
    reader = STEPCAFControl_Reader()
    reader.SetNameMode(True)
    if reader.ReadFile(str(path)) != IFSelect_ReturnStatus.IFSelect_RetDone or not reader.Transfer(doc):
        raise ValueError(f"cannot read {path}")
    shape_tool = XCAFDoc_DocumentTool.ShapeTool_s(doc.Main())
    names = []
    it = TDF_ChildIterator(shape_tool.Label(), False)
    while it.More():
        lab = it.Value()
        if shape_tool.IsTopLevel(lab):
            attr = TDataStd_Name()
            if lab.FindAttribute(TDataStd_Name.GetID_s(), attr):
                names.append(attr.Get().ToExtString())
        it.Next()
    return names
