"""Quoting contracts (SPEC_V2_0_MULTISIDE_FIXTURING.md §3.4, §4.2)."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

MachineType = Literal["3axis_vmc", "5axis_3plus2"]
MACHINES: tuple[str, ...] = ("3axis_vmc", "5axis_3plus2")


class _M(BaseModel):
    model_config = ConfigDict(extra="forbid")


class MachineRate(_M):
    label: str
    hourly_rate: float = Field(gt=0)
    tool_change_sec: float = Field(ge=0)
    load_unload_sec: float = Field(ge=0)


class SetupMinutes(_M):
    base_cam_programming: float = Field(ge=0)
    per_3axis_setup: float = Field(ge=0)
    per_5axis_setup: float = Field(ge=0)
    mill_softjaws_pair: float = Field(ge=0)


class MaterialRate(_M):
    price_per_kg: float = Field(ge=0)
    mrr_cm3_per_min: float = Field(gt=0)
    finish_min_per_100cm2: float = Field(ge=0)
    hole_sec_base: float = Field(ge=0)
    tap_sec: float = Field(ge=0)
    ream_sec: float = Field(ge=0)
    chip_load_efficiency: float = Field(1.0, gt=0, le=1.5, description="eta_chip_load in the roughing formula")


class ShopRates(_M):
    schema_version: Literal["1.0"] = "1.0"
    currency: str = "EUR"
    machines: dict[MachineType, MachineRate]
    setup_minutes: SetupMinutes
    materials: dict[str, MaterialRate]
    saw_waste_factor: float = Field(0.08, ge=0, le=1)
    margin_pct_default: float = Field(20.0, ge=0, le=500)
    fine_finish_factor: float = Field(1.5, ge=1, description="finishing multiplier for Ra <= 1.6 / tight-fit faces")


class HoleInput(_M):
    feature_id: str
    diameter_mm: float
    depth_mm: float
    thread: bool = False
    tight: bool = False
    counterbore: bool = False
    countersink: bool = False


class QuoteInputs(_M):
    """Geometry facts of one built revision (extracted from the B-Rep + DFM report, cached per version)."""

    version: int
    material: str | None
    material_group: str
    part_volume_mm3: float
    raw_volume_mm3: float
    raw_mass_kg: float | None
    machined_area_cm2: float
    fine_area_cm2: float
    untouched_area_cm2: float = 0.0
    holes: list[HoleInput] = Field(default_factory=list)
    unique_tools: int = 0
    setups_3axis: int
    setups_5axis: int
    softjaws_recommended: bool = False
    notes: list[str] = Field(default_factory=list)


class QuoteRequest(_M):
    quantities: list[int] = Field(default_factory=lambda: [1, 5, 10, 25, 50], min_length=1, max_length=20)
    custom_quantity: int | None = Field(default=None, ge=1, le=100000)
    machine_type: MachineType = "3axis_vmc"
    include_softjaws: bool | None = Field(default=None, description="default: SetupPlan.softjaws_recommended")
    margin_pct: float | None = Field(default=None, ge=0, le=500)


class Line(_M):
    key: str
    label: str
    value: float
    unit: str
    formula: str


class PriceBreak(_M):
    quantity: int
    unit_cost: float
    unit_price: float
    total_price: float
    setup_share: float = Field(description="amortised setup cost per part")


class MachineQuote(_M):
    machine_type: MachineType
    label: str
    hourly_rate: float
    setups: int
    t_setup_min: float
    t_cycle_min: float
    t_load_unload_min: float
    lines: list[Line]
    price_breaks: list[PriceBreak]


class QuoteBreakdown(_M):
    job_id: str
    version: int
    currency: str
    margin_pct: float
    material_group: str
    include_softjaws: bool
    c_mat: float
    material_lines: list[Line]
    machines: dict[MachineType, MachineQuote]
    selected: MachineType
    cheaper: MachineType | None = Field(None, description="lower unit price at the largest quantity")
    inputs: QuoteInputs
    notes: list[str] = Field(default_factory=list)
