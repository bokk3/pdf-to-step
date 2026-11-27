"""Softjaw generator contracts (SPEC_V2_0_MULTISIDE_FIXTURING.md §3.2, §4.1)."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

Pos = float


class _M(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ViseSpec(_M):
    id: str = Field(min_length=1, max_length=48)
    name: str = ""
    jaw_width_mm: float = Field(gt=0, description="along X (jaw length)")
    jaw_height_mm: float = Field(gt=0)
    jaw_depth_mm: float = Field(gt=0, description="along Y (clamping direction)")
    max_opening_mm: float = Field(gt=0, description="max distance between the vise jaw plates")
    bolt_spacing_mm: float = Field(gt=0)
    bolt_height_from_bottom_mm: float = Field(gt=0)
    bolt_clearance_dia_mm: float = Field(gt=0)
    bolt_cbore_dia_mm: float = Field(gt=0)
    bolt_cbore_depth_mm: float = Field(gt=0)
    default_jaw_gap_mm: float = Field(6.0, ge=0)

    @model_validator(mode="after")
    def _check(self) -> "ViseSpec":
        if self.bolt_cbore_dia_mm <= self.bolt_clearance_dia_mm:
            raise ValueError(f"{self.id}: bolt counterbore must be wider than the clearance hole")
        if self.bolt_spacing_mm + self.bolt_cbore_dia_mm >= self.jaw_width_mm:
            raise ValueError(f"{self.id}: bolt pattern does not fit the jaw width")
        if self.bolt_height_from_bottom_mm + self.bolt_cbore_dia_mm / 2 >= self.jaw_height_mm:
            raise ValueError(f"{self.id}: bolt counterbore does not fit the jaw height")
        if self.bolt_cbore_depth_mm >= self.jaw_depth_mm:
            raise ValueError(f"{self.id}: counterbore deeper than the jaw")
        return self


class ViseLibrary(_M):
    schema_version: Literal["1.0"] = "1.0"
    vises: list[ViseSpec] = Field(default_factory=list)

    @model_validator(mode="after")
    def _unique(self) -> "ViseLibrary":
        ids = [v.id for v in self.vises]
        if len(set(ids)) != len(ids):
            raise ValueError("duplicate vise ids")
        return self

    def get(self, vise_id: str) -> ViseSpec:
        for v in self.vises:
            if v.id == vise_id:
                return v
        raise KeyError(vise_id)


class SoftjawRequest(_M):
    vise_id: str = "vise_125_std"
    op2_flip_axis: Literal["X", "Y"] = "X"
    z_rotation_deg: Literal[0, 90] = 0
    grip_depth_mm: float = Field(4.0, ge=2.0, le=25.0)
    xy_clearance_mm: float = Field(0.05, ge=0.0, le=1.0)
    dogbone_radius_mm: float = Field(3.2, ge=0.5, le=12.0)
    jaw_gap_mm: float | None = Field(default=None, ge=0, description="default: the vise's default_jaw_gap_mm")
    jaw_material: Literal["aluminum_6082", "steel_c45"] = "aluminum_6082"
    version: int | None = Field(default=None, description="PartSpec revision (default: current)")


class SoftjawCheck(_M):
    code: str
    passed: bool
    blocking: bool
    value_mm: float | None = None
    limit_mm: float | None = None
    message: str


class SoftjawReport(_M):
    job_id: str
    version: int
    request: SoftjawRequest
    passed: bool
    checks: list[SoftjawCheck]
    jaw_blank_mm: tuple[float, float, float] = Field(description="width X, depth Y, height Z per jaw")
    part_envelope_mm: tuple[float, float, float] = Field(description="workpiece in the Op 2 pose")
    cavity_depth_y_mm: float = Field(description="how far the pocket reaches into each jaw")
    dogbones: list[tuple[float, float]] = Field(default_factory=list, description="relief centres (jaw frame XY)")
    part_offset_mm: tuple[float, float, float] = Field(description="workpiece origin in the jaw frame (Z0 = jaw top)")
    recommended_tool: dict | None = None
    artifacts: dict[str, str] = Field(default_factory=dict)
