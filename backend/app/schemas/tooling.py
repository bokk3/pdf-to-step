"""v1.5 shopfloor contracts (SPEC_V1_5_CAM_SHOPFLOOR.md §3): tool library, tool checks, stock advice.

Independent of PartSpec (no import of part_spec) so DFMReport can embed these models.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

MaterialGroup = Literal["aluminum", "steel", "stainless", "plastic", "brass_copper", "unknown"]
MATERIAL_GROUPS: tuple[str, ...] = ("aluminum", "steel", "stainless", "plastic", "brass_copper", "unknown")

ToolType = Literal[
    "flat_endmill", "bull_endmill", "ball_endmill", "slot_mill", "face_mill", "chamfer_mill", "corner_rounding",
    "spot_drill", "center_drill", "drill", "tap", "thread_mill", "reamer", "boring_bar", "counterbore",
    "countersink", "other",
]
ENDMILLS: frozenset[str] = frozenset({"flat_endmill", "bull_endmill", "slot_mill"})
CHAMFER_TOOLS: frozenset[str] = frozenset({"chamfer_mill", "countersink", "spot_drill"})


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ToolSpec(_Model):
    id: str = Field("", max_length=32, description="magazine / pocket number, e.g. 'T04' (empty only before merging)")
    name: str = ""
    tool_type: ToolType
    diameter_mm: float = Field(gt=0, description="cutting diameter (nominal thread size for taps / thread mills)")
    flute_length_mm: float = Field(gt=0, description="usable cutting length (reach for the checks)")
    overall_length_mm: float | None = Field(default=None, gt=0)
    corner_radius_mm: float = Field(0.0, ge=0)
    flutes: int = Field(2, ge=1, le=64)
    materials: list[MaterialGroup] = Field(default_factory=list, description="empty = every material")
    thread_spec: str | None = Field(default=None, description="taps / thread mills: 'M8' or 'M8x1'")
    tolerance_class: str | None = Field(default=None, description="reamers: 'H7'; taps: '6H'")
    in_magazine: bool = True
    notes: str = ""

    @field_validator("id")
    @classmethod
    def _strip_id(cls, v: str) -> str:
        return v.strip()

    def cuts(self, group: str) -> bool:
        """Usable on this material group ('unknown' material accepts every tool)."""
        return not self.materials or group == "unknown" or group in self.materials

    def label(self) -> str:
        return f"{self.id} — {self.name}" if self.name else self.id


class ToolLibrary(_Model):
    schema_version: Literal["1.0"] = "1.0"
    tools: list[ToolSpec] = Field(default_factory=list)

    @model_validator(mode="after")
    def _unique_ids(self) -> "ToolLibrary":
        seen: set[str] = set()
        for t in self.tools:
            if not t.id:
                raise ValueError("every tool needs an id (e.g. T04)")
            if t.id in seen:
                raise ValueError(f"duplicate tool id {t.id!r}")
            seen.add(t.id)
        return self


class ToolCheck(_Model):
    """One DFM v2 result per feature (and per operation where a feature needs several tools)."""
    feature_id: str
    operation: str = Field(description="pocket | slot | drill | tap | ream | counterbore | countersink | ...")
    status: Literal["ok", "info", "warning"]
    assigned_tool_id: str | None = None
    assigned_tool_name: str | None = None
    message: str
    suggested_fix: str | None = None


class ToolAssignment(_Model):
    """Feature -> tool link, consumed by the setup sheet's tooling table."""
    feature_id: str
    operation: str
    tool_id: str
    tool_name: str = ""
    tool_type: str = ""
    diameter_mm: float = 0.0
    flute_length_mm: float = 0.0


class Dims(_Model):
    x: float
    y: float
    z: float


class StockAllowances(_Model):
    xy_per_side_mm: float = Field(2.0, ge=0)
    z_top_face_mm: float = Field(1.0, ge=0)
    z_clamp_margin_mm: float = Field(4.0, ge=0, description="extra height for the vise / op-2 deck-off")
    saw_length_step_mm: float = Field(5.0, ge=0, description="saw-cut length rounded up to this step (0 = exact)")


class StockMaterial(_Model):
    display_name: str = ""
    density_kg_m3: float = Field(gt=0)
    standard_flat_bars_mm: list[tuple[float, float]] = Field(default_factory=list, description="[width, thickness]")
    standard_plate_thicknesses_mm: list[float] = Field(default_factory=list)


class StockCatalog(_Model):
    schema_version: Literal["1.0"] = "1.0"
    default_allowances: StockAllowances = Field(default_factory=StockAllowances)
    materials: dict[MaterialGroup, StockMaterial] = Field(default_factory=dict)


class StockRecommendation(_Model):
    material_group: MaterialGroup
    profile_type: Literal["flat_bar", "plate", "custom"]
    profile_label: str = Field(description="e.g. 'flat bar 120 x 25' or 'plate 25 mm'")
    required_blank_mm: Dims = Field(description="finished envelope + allowances")
    recommended_blank_mm: Dims = Field(description="saw blank cut from the standard profile")
    saw_cut_length_mm: float
    density_kg_m3: float | None = None
    raw_mass_kg: float | None = None
    part_mass_kg: float | None = None
    chip_removal_pct: float | None = None
    allowances: StockAllowances = Field(default_factory=StockAllowances)
    note: str = ""
