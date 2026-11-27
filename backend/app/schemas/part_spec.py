"""Canonical PartSpec v2.0 + DFMReport models (ARCHITECTURE.md §6 / §8, SPEC_V2_0_MULTISIDE_FIXTURING.md §2).

This module is the single contract between extraction (LLM), the editor (UI),
the deterministic CAD compiler and the verifier. Nothing else is allowed to pass
geometry between modules.

Coordinate conventions (must match the prompt in deepseek_client.py):

* All lengths are millimetres (inch drawings are converted on ingestion).
* ``datum_origin`` places the part in the global frame (X right, Y away from
  the viewer in the top view, Z up). The default ``bottom_left_top_face`` puts
  the origin on the top face at the bottom-left corner of the top view, so the
  part occupies x∈[0, L], y∈[0, W], z∈[-T, 0] - the usual CNC work offset.
* ``center_x`` / ``center_y`` (and other x/y fields of a feature) are local coordinates on the feature's face.
  The face frames are defined in ``app/schemas/frames.py``: schema 2.0 uses the right-handed table of the v2.0
  spec (u x v = outward normal, origin on a stock corner, local coordinates positive), stored v1.0 / v1.1 specs
  keep their original convention (datum coordinates projected on the face, never mirrored). ``face`` may also be
  ``plane:<id>`` of a ``tilted_planes`` entry (3+2 machining, v2.0 only).
  Cuts go into the material from that face (local -Z); bosses grow out of it.
* A payload without ``schema_version`` is a legacy v1 payload (``upgrade_part_spec``); new specs carry "2.0".
  ``to_v2_coordinates`` converts a v1.x spec into the v2.0 convention (same geometry).

Fields beyond the §6 example (all optional, documented in ARCHITECTURE.md §11):
``pattern`` / ``extra_positions`` for repeated features, ``countersink``,
``thread_depth``, ``tip_angle_deg``, ``rotation_deg``, the ``step_cut`` box and
the ``complex_3d`` feature that carries the fallback description + code.
"""

from __future__ import annotations

import math
import re
from enum import Enum
from typing import Annotated, Literal, Union

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.schemas.setup_plan import SetupPlan
from app.schemas.tooling import StockRecommendation, ToolAssignment, ToolCheck

SCHEMA_VERSION = "2.0"
LEGACY_VERSIONS = ("1.0", "1.1")
REVIEW_CONFIDENCE_THRESHOLD = 0.85

FACE_PATTERN = r"^(top|bottom|front|back|left|right|plane:[A-Za-z0-9_.\-]{1,48})$"
Face = Annotated[str, Field(pattern=FACE_PATTERN, description="top|bottom|front|back|left|right|plane:<id>")]
FACES: tuple[str, ...] = ("top", "bottom", "front", "back", "left", "right")
DatumOrigin = Literal["bottom_left_top_face", "bottom_left_bottom_face", "center_top_face", "center_bottom_face"]
Conf = Annotated[float, Field(ge=0.0, le=1.0)]
Pos = Annotated[float, Field(gt=0)]
XY = tuple[float, float]


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


# --------------------------------------------------------------------------- #
# Stock / profile / edges
# --------------------------------------------------------------------------- #


class Stock(_Model):
    length_x: Pos
    width_y: Pos
    thickness_z: Pos
    confidence: Conf = 1.0


class OuterProfile(_Model):
    type: Literal["rectangle", "rounded_rectangle", "polygon"] = "rectangle"
    corner_radius: float = Field(0.0, ge=0)
    points_2d: list[XY] | None = Field(default=None, description="polygon only: closed outline in datum X/Y")
    confidence: Conf = 1.0

    @model_validator(mode="after")
    def _check(self) -> "OuterProfile":
        if self.type == "polygon" and (not self.points_2d or len(self.points_2d) < 3):
            raise ValueError("outer_profile polygon requires points_2d with >= 3 points")
        if self.type == "rounded_rectangle" and self.corner_radius <= 0:
            raise ValueError("rounded_rectangle requires corner_radius > 0")
        return self


EdgeTarget = Literal["top_outer_edges", "bottom_outer_edges", "vertical_outer_edges", "all_outer_edges"]


class EdgeTreatment(_Model):
    id: str = Field(min_length=1)
    type: Literal["chamfer", "fillet"]
    size: Pos
    target: EdgeTarget = "top_outer_edges"
    confidence: Conf = 1.0


# --------------------------------------------------------------------------- #
# Features
# --------------------------------------------------------------------------- #


class LinearPattern(_Model):
    """Grid starting at the feature centre."""

    type: Literal["linear"] = "linear"
    count_x: int = Field(1, ge=1)
    count_y: int = Field(1, ge=1)
    pitch_x: float = 0.0
    pitch_y: float = 0.0


class CircularPattern(_Model):
    """Bolt circle around the feature centre."""

    type: Literal["circular"] = "circular"
    radius: Pos
    count: int = Field(ge=1)
    start_angle_deg: float = 0.0
    sweep_deg: float = 360.0


Pattern = Annotated[Union[LinearPattern, CircularPattern], Field(discriminator="type")]


class TiltedPlane(_Model):
    """3+2 indexed working plane (``face = "plane:<id>"``): SPATIAL_XYZ = intrinsic X -> Y -> Z (PLANE SPATIAL)."""

    id: str = Field(min_length=1, max_length=48, pattern=r"^[A-Za-z0-9_.\-]+$")
    name: str | None = None
    origin_x: float
    origin_y: float
    origin_z: float
    rot_a_deg: float = Field(0.0, ge=-360, le=360)
    rot_b_deg: float = Field(0.0, ge=-360, le=360)
    rot_c_deg: float = Field(0.0, ge=-360, le=360)
    rotation_convention: Literal["SPATIAL_XYZ"] = "SPATIAL_XYZ"
    confidence: Conf = 1.0
    source_note: str | None = None


class _FeatureBase(_Model):
    id: str = Field(min_length=1)
    face: Face = "top"
    confidence: Conf = 1.0
    source_note: str | None = None


class _Positioned(_FeatureBase):
    center_x: float
    center_y: float
    pattern: Pattern | None = None
    extra_positions: list[XY] = Field(default_factory=list, description="additional (x, y) instances")

    def positions(self) -> list[XY]:
        base = (self.center_x, self.center_y)
        p = self.pattern
        if isinstance(p, LinearPattern):
            pts = [(base[0] + i * p.pitch_x, base[1] + j * p.pitch_y) for j in range(p.count_y) for i in range(p.count_x)]
        elif isinstance(p, CircularPattern):
            full = math.isclose(abs(p.sweep_deg), 360.0)
            step = p.sweep_deg / (p.count if full else max(p.count - 1, 1))
            pts = []
            for k in range(p.count):
                a = math.radians(p.start_angle_deg + k * step)
                pts.append((base[0] + p.radius * math.cos(a), base[1] + p.radius * math.sin(a)))
        else:
            pts = [base]
        return pts + [tuple(e) for e in self.extra_positions]  # type: ignore[misc]


class _Cut(_Model):
    depth: float | None = Field(default=None, gt=0, description="blind depth; ignored when through_all")
    through_all: bool = False
    intentional_intersection: bool = Field(
        False, description="v2.0: this cut is meant to meet a cut from another face (no cross-face DFM error)")

    @model_validator(mode="after")
    def _depth_or_through(self):
        if self.depth is None and not self.through_all:
            self.through_all = True  # no depth given -> through
        return self


class Counterbore(_Model):
    diameter: Pos
    depth: Pos


class Countersink(_Model):
    diameter: Pos
    angle_deg: float = Field(90.0, gt=0, lt=180)


THREAD_RE = re.compile(r"^\s*M\s*(\d+(?:[.,]\d+)?)\s*(?:[xX×]\s*(\d+(?:[.,]\d+)?))?")
METRIC_COARSE_PITCH = {
    1.6: 0.35, 2.0: 0.4, 2.5: 0.45, 3.0: 0.5, 4.0: 0.7, 5.0: 0.8, 6.0: 1.0, 8.0: 1.25, 10.0: 1.5,
    12.0: 1.75, 14.0: 2.0, 16.0: 2.0, 18.0: 2.5, 20.0: 2.5, 24.0: 3.0, 30.0: 3.5,
}
METRIC_TAP_DRILL = {  # ISO 2306 coarse-thread drill sizes
    1.6: 1.25, 2.0: 1.6, 2.5: 2.05, 3.0: 2.5, 4.0: 3.3, 5.0: 4.2, 6.0: 5.0, 8.0: 6.8, 10.0: 8.5,
    12.0: 10.2, 14.0: 12.0, 16.0: 14.0, 18.0: 15.5, 20.0: 17.5, 24.0: 21.0, 30.0: 26.5,
}


def parse_metric_thread(thread: str | None) -> tuple[float, float] | None:
    """'M8' -> (8.0, 1.25); 'M8x1' -> (8.0, 1.0). None for non-metric designations."""
    if not thread:
        return None
    m = THREAD_RE.match(thread)
    if not m:
        return None
    nominal = float(m.group(1).replace(",", "."))
    pitch = float(m.group(2).replace(",", ".")) if m.group(2) else METRIC_COARSE_PITCH.get(nominal, round(nominal * 0.15, 2))
    return nominal, pitch


class Hole(_Positioned, _Cut):
    type: Literal["hole"] = "hole"
    diameter: Pos = Field(description="nominal diameter (thread size for tapped holes)")
    thread: str | None = Field(default=None, description="e.g. 'M8' or 'M8x1'")
    thread_depth: float | None = Field(default=None, gt=0)
    counterbore: Counterbore | None = None
    countersink: Countersink | None = None
    tolerance: str | None = Field(default=None, description="e.g. 'H7' or '+0.015/0'")
    tip_angle_deg: float | None = Field(default=118.0, description="drill point for blind holes; null = flat")

    @model_validator(mode="after")
    def _check(self) -> "Hole":
        if self.counterbore and self.counterbore.diameter <= self.diameter:
            raise ValueError(f"{self.id}: counterbore diameter must exceed hole diameter")
        if self.countersink and self.countersink.diameter <= self.diameter:
            raise ValueError(f"{self.id}: countersink diameter must exceed hole diameter")
        if self.counterbore and self.countersink:
            raise ValueError(f"{self.id}: a hole cannot have both counterbore and countersink")
        return self

    def drill_diameter(self) -> float:
        """Modelled diameter: ISO tap drill for metric threads (M8 -> 6.8, M8x1 -> 7.0), else nominal."""
        t = parse_metric_thread(self.thread)
        if t and abs(t[0] - self.diameter) < 1e-6:
            nominal, pitch = t
            if abs(METRIC_COARSE_PITCH.get(nominal, -1) - pitch) < 1e-9 and nominal in METRIC_TAP_DRILL:
                return METRIC_TAP_DRILL[nominal]
            return round(nominal - pitch, 3)
        return self.diameter


class PocketRect(_Positioned, _Cut):
    type: Literal["pocket_rect"] = "pocket_rect"
    length_x: Pos
    width_y: Pos
    corner_radius: float = Field(0.0, ge=0, description="internal vertical corner radius")
    rotation_deg: float = 0.0

    @model_validator(mode="after")
    def _check(self) -> "PocketRect":
        if 2 * self.corner_radius > min(self.length_x, self.width_y) + 1e-9:
            raise ValueError(f"{self.id}: corner_radius too large for pocket size")
        return self


class PocketCircle(_Positioned, _Cut):
    type: Literal["pocket_circle"] = "pocket_circle"
    diameter: Pos


class PocketPolygon(_FeatureBase, _Cut):
    type: Literal["pocket_polygon"] = "pocket_polygon"
    points_2d: list[XY] = Field(min_length=3)
    corner_radius: float = Field(0.0, ge=0)


class Slot(_FeatureBase, _Cut):
    type: Literal["slot"] = "slot"
    start_x: float
    start_y: float
    end_x: float
    end_y: float
    width: Pos

    @property
    def length(self) -> float:
        return math.hypot(self.end_x - self.start_x, self.end_y - self.start_y)


class BossRect(_Positioned):
    type: Literal["boss_rect"] = "boss_rect"
    length_x: Pos
    width_y: Pos
    height: Pos
    corner_radius: float = Field(0.0, ge=0)
    rotation_deg: float = 0.0


class BossCircle(_Positioned):
    type: Literal["boss_circle"] = "boss_circle"
    diameter: Pos
    height: Pos


class StepCut(_FeatureBase):
    """Open edge cut-out / shoulder: removes the box [x_min,x_max]x[y_min,y_max] (face coords) to `depth`.

    The box may (and usually does) extend past the stock edge.
    """

    type: Literal["step_cut"] = "step_cut"
    x_min: float
    x_max: float
    y_min: float
    y_max: float
    depth: Pos
    corner_radius: float = Field(0.0, ge=0, description="internal vertical corner radius")

    @model_validator(mode="after")
    def _check(self) -> "StepCut":
        if self.x_max <= self.x_min or self.y_max <= self.y_min:
            raise ValueError(f"{self.id}: step_cut needs x_min < x_max and y_min < y_max")
        return self


class AngledFaceCut(_FeatureBase):
    """v2.0: mill the inclined exterior face of a tilted plane: removes all stock on the outward side (local +Z) of
    ``face = "plane:<id>"`` (shifted by ``offset`` along the normal). Machined before the plane's other features."""

    type: Literal["angled_face_cut"] = "angled_face_cut"
    offset: float = Field(0.0, description="mm along the plane normal (+ leaves more material)")

    @model_validator(mode="after")
    def _plane_only(self) -> "AngledFaceCut":
        if not self.face.startswith("plane:"):
            raise ValueError(f"{self.id}: angled_face_cut needs face 'plane:<id>'")
        return self


class Complex3D(_FeatureBase):
    """Non-prismatic geometry built by sandboxed, LLM-generated build123d code (Layer 2)."""

    type: Literal["complex_3d"] = "complex_3d"
    description: str = Field(min_length=3)
    operation: Literal["cut", "union"] = "cut"
    bbox_min: tuple[float, float, float] | None = None
    bbox_max: tuple[float, float, float] | None = None
    code: str | None = Field(default=None, description="build123d code assigning `result` (filled by the code model)")


Feature = Annotated[
    Union[Hole, PocketRect, PocketCircle, PocketPolygon, Slot, BossRect, BossCircle, StepCut, AngledFaceCut, Complex3D],
    Field(discriminator="type"),
]
CUT_TYPES = (Hole, PocketRect, PocketCircle, PocketPolygon, Slot, StepCut)


# --------------------------------------------------------------------------- #
# v1.1 extensions (DRAWING_SPEC.md §5) - all optional, v1.0 payloads stay valid
# --------------------------------------------------------------------------- #

InputSource = Literal["pdf_drawing", "paper_sketch", "dxf", "step", "manual"]


GdtCharacteristic = Literal[
    "straightness", "flatness", "circularity", "cylindricity", "profile_line", "profile_surface", "parallelism",
    "perpendicularity", "angularity", "position", "concentricity", "symmetry", "circular_runout", "total_runout",
]


class GeometricTolerance(_Model):
    """EN ISO 1101 tolerance frame. Carried as metadata and printed as a drawing note; not modelled in 3D (v1)."""

    characteristic: GdtCharacteristic
    tolerance_mm: Pos
    diameter_zone: bool = Field(False, description="Ø tolerance zone (e.g. position Ø0.1)")
    datums: list[str] = Field(default_factory=list, max_length=3, description="datum letters, primary first")
    feature_id: str | None = Field(None, description="toleranced feature, None = whole part / see note")
    note: str = ""


class SurfaceFinish(_Model):
    """EN ISO 21920-1 surface requirement for one feature (the general one is drawing_metadata.surface_finish)."""

    feature_id: str
    ra_um: Pos
    note: str = ""


class DrawingMetadata(_Model):
    """Presentation data for the generated EN ISO drawing (never affects geometry)."""

    sheet_size: Literal["AUTO", "A4_LANDSCAPE", "A3_LANDSCAPE"] = "AUTO"
    projection: Literal["ISO_FIRST_ANGLE"] = "ISO_FIRST_ANGLE"
    scale: str = Field("AUTO", pattern=r"^(AUTO|\d+(\.\d+)?:\d+(\.\d+)?)$")
    language: Literal["EN", "EN_NL"] = "EN"
    surface_finish: str = "Ra 3.2 (ISO 21920-1)"
    edge_break_note: str = "Break sharp edges 0.2-0.5 mm (ISO 13715)"
    drawing_number: str | None = None
    revision: str = "A"
    drawn_by: str = ""
    checked_by: str = ""
    approval_status: Literal["DRAFT", "APPROVED"] = "DRAFT"
    section_view: Literal["AUTO", "NONE"] = Field("AUTO", description="hatched section A-A when the part has hidden features")
    gdt: list[GeometricTolerance] = Field(default_factory=list, description="EN ISO 1101 frames (text notes in v1)")
    surface_finishes: list[SurfaceFinish] = Field(default_factory=list, description="per-feature Ra requirements")
    notes: list[str] = Field(default_factory=list, description="extra general notes printed on the drawing")


class MissingDimension(_Model):
    """A value the source (e.g. a paper sketch) does not contain. Never guessed."""

    field_path: str = Field(description="dotted path into the PartSpec, e.g. 'stock.thickness_z' or 'features.P1.depth'")
    prompt_nl: str
    prompt_en: str = ""
    suggested_default: float | None = None
    blocking: bool = True


# --------------------------------------------------------------------------- #
# PartSpec
# --------------------------------------------------------------------------- #


class PartSpec(_Model):
    schema_version: Literal["1.0", "1.1", "2.0"] = SCHEMA_VERSION
    input_source: InputSource | None = None
    part_number: str | None = Field(default=None, max_length=64, description="v2.0: revision lineage key")
    revision: str | None = Field(default=None, max_length=16, description="v2.0: 'A', 'B' or '1', '2' ...")
    drawing_metadata: DrawingMetadata = Field(default_factory=DrawingMetadata)
    missing_dimensions: list[MissingDimension] = Field(default_factory=list)
    part_name: str | None = None
    units: Literal["mm"] = "mm"
    material: str | None = None
    general_tolerance: str | None = None
    datum_origin: DatumOrigin = "bottom_left_top_face"
    stock: Stock
    outer_profile: OuterProfile = Field(default_factory=OuterProfile)
    tilted_planes: list[TiltedPlane] = Field(default_factory=list, description="v2.0: 3+2 working planes")
    features: list[Feature] = Field(default_factory=list)
    edge_treatments: list[EdgeTreatment] = Field(default_factory=list)
    requires_complex_3d_fallback: bool = False
    ambiguities: list[str] = Field(default_factory=list)

    @field_validator("units", mode="before")
    @classmethod
    def _mm_only(cls, v):
        if v not in (None, "mm"):
            raise ValueError("PartSpec is millimetre-only: convert inch drawings on ingestion")
        return "mm"

    @model_validator(mode="before")
    @classmethod
    def upgrade_part_spec(cls, data):
        """v1.x -> v2.0 loader: stored v1.0 / v1.1 JSON validates unchanged (no DB migration). A payload without
        ``schema_version`` predates v2.0 and is read as v1.1 (its side / bottom coordinates use the v1 frames)."""
        if isinstance(data, dict) and "schema_version" not in data:
            data = {**data, "schema_version": "1.1"}
        return data

    @model_validator(mode="after")
    def _check(self) -> "PartSpec":
        ids = [f.id for f in self.features] + [e.id for e in self.edge_treatments]
        dupes = sorted({i for i in ids if ids.count(i) > 1})
        if dupes:
            raise ValueError(f"duplicate ids: {dupes}")
        plane_ids = [p.id for p in self.tilted_planes]
        if len(set(plane_ids)) != len(plane_ids):
            raise ValueError("duplicate tilted_planes ids")
        planes_used = {f.face[6:] for f in self.features if f.face.startswith("plane:")}
        if (self.tilted_planes or planes_used) and self.schema_version in LEGACY_VERSIONS:
            raise ValueError("tilted planes need schema_version 2.0")
        unknown = sorted(planes_used - set(plane_ids))
        if unknown:
            raise ValueError(f"features reference unknown tilted planes: {unknown}")
        if any(isinstance(f, Complex3D) for f in self.features):
            self.requires_complex_3d_fallback = True
        return self

    # -- geometry helpers -------------------------------------------------------
    def stock_bbox(self) -> tuple[tuple[float, float, float], tuple[float, float, float]]:
        """Bounding box of the stock in datum coordinates (ignores bosses)."""
        s = self.stock
        L, W, T = s.length_x, s.width_y, s.thickness_z
        x0, y0 = (-L / 2, -W / 2) if self.datum_origin.startswith("center") else (0.0, 0.0)
        z0 = -T if self.datum_origin.endswith("top_face") else 0.0
        return (x0, y0, z0), (x0 + L, y0 + W, z0 + T)

    @property
    def is_legacy(self) -> bool:
        return self.schema_version in LEGACY_VERSIONS

    def complex_features(self) -> list[Complex3D]:
        return [f for f in self.features if isinstance(f, Complex3D)]

    @property
    def blocking_missing(self) -> list[MissingDimension]:
        """Missing values that must be entered before CAD / drawing compilation may run."""
        return [m for m in self.missing_dimensions if m.blocking]

    def review_items(self) -> list[str]:
        """Ids (or 'stock' / 'outer_profile') with confidence below the review threshold."""
        out = []
        if self.stock.confidence < REVIEW_CONFIDENCE_THRESHOLD:
            out.append("stock")
        if self.outer_profile.confidence < REVIEW_CONFIDENCE_THRESHOLD:
            out.append("outer_profile")
        out += [f.id for f in self.features if f.confidence < REVIEW_CONFIDENCE_THRESHOLD]
        out += [e.id for e in self.edge_treatments if e.confidence < REVIEW_CONFIDENCE_THRESHOLD]
        return out


def to_v2_coordinates(spec: PartSpec) -> PartSpec:
    """Same geometry, v2.0 convention: maps every non-top feature's local coordinates from the v1 frame to the
    v2.0 frame (all frames are axis-parallel, so it is a reflection + offset per axis). v2.0 specs are returned as-is."""
    from app.schemas.frames import ortho_frames

    if not spec.is_legacy:
        return spec
    f1, f2 = ortho_frames(spec, legacy=True), ortho_frames(spec, legacy=False)
    d = spec.model_dump(mode="json")
    d["schema_version"] = SCHEMA_VERSION
    for feat in d["features"]:
        face = feat.get("face", "top")
        a, b = f1[face], f2[face]
        sx, sy = float(a.u @ b.u), float(a.v @ b.v)  # +-1: v1 and v2 axes are parallel per face

        def m(x: float, y: float) -> list[float]:
            u, v, _ = b.local(a.point(x, y))
            return [round(u, 9), round(v, 9)]

        if "center_x" in feat:
            feat["center_x"], feat["center_y"] = m(feat["center_x"], feat["center_y"])
            feat["extra_positions"] = [m(*xy) for xy in feat.get("extra_positions") or []]
            pat = feat.get("pattern")
            if pat and pat["type"] == "linear":
                pat["pitch_x"], pat["pitch_y"] = sx * pat["pitch_x"], sy * pat["pitch_y"]
            elif pat:
                t = math.radians(pat["start_angle_deg"])
                pat["start_angle_deg"] = math.degrees(math.atan2(sy * math.sin(t), sx * math.cos(t)))
                pat["sweep_deg"] = sx * sy * pat["sweep_deg"]
        if "rotation_deg" in feat:
            feat["rotation_deg"] = sx * sy * feat["rotation_deg"]
        if "points_2d" in feat:
            feat["points_2d"] = [m(*xy) for xy in feat["points_2d"]]
        if "start_x" in feat:
            feat["start_x"], feat["start_y"] = m(feat["start_x"], feat["start_y"])
            feat["end_x"], feat["end_y"] = m(feat["end_x"], feat["end_y"])
        if "x_min" in feat:
            (ua, va), (ub, vb) = m(feat["x_min"], feat["y_min"]), m(feat["x_max"], feat["y_max"])
            feat["x_min"], feat["x_max"] = min(ua, ub), max(ua, ub)
            feat["y_min"], feat["y_max"] = min(va, vb), max(va, vb)
    return PartSpec.model_validate(d)


# --------------------------------------------------------------------------- #
# DFM / verification report (§8)
# --------------------------------------------------------------------------- #


class Severity(str, Enum):
    info = "info"
    warning = "warning"
    error = "error"


class DFMIssue(_Model):
    code: str
    severity: Severity
    message: str
    feature_id: str | None = None
    suggestion: str | None = None


class GeometryCheck(_Model):
    is_valid: bool
    solid_count: int
    volume_mm3: float
    bbox_min_mm: tuple[float, float, float]
    bbox_max_mm: tuple[float, float, float]
    expected_size_mm: tuple[float, float, float]
    bbox_matches_spec: bool
    bbox_deviation_mm: float


class FeatureBuildStatus(_Model):
    feature_id: str
    ok: bool
    method: Literal["deterministic", "llm_sandbox", "skipped"]
    error: str | None = None


class DFMReport(_Model):
    spec_version: int
    passed: bool = Field(description="gate checks (§8.1-8.2 + feature build) passed -> job may be 'ready'")
    geometry: GeometryCheck | None = None
    issues: list[DFMIssue] = Field(default_factory=list)
    feature_status: list[FeatureBuildStatus] = Field(default_factory=list)
    review_items: list[str] = Field(default_factory=list)
    colored_faces: dict[str, int] = Field(default_factory=dict, description="STEP XCAF face colours written")
    stats: dict[str, float] = Field(default_factory=dict)
    # v1.5 DFM v2 (SPEC_V1_5_CAM_SHOPFLOOR.md §3.3) - optional, older reports stay valid
    material_group: str | None = None
    tool_checks: list[ToolCheck] = Field(default_factory=list)
    recommended_tools: list[ToolAssignment] = Field(default_factory=list)
    stock_recommendation: StockRecommendation | None = None
    # v2.0 (SPEC_V2_0_MULTISIDE_FIXTURING.md §3.1)
    setup_plan: SetupPlan | None = None

    @property
    def errors(self) -> list[DFMIssue]:
        return [i for i in self.issues if i.severity == Severity.error]


def partspec_json_schema() -> dict:
    return PartSpec.model_json_schema()
