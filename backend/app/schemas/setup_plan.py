"""SetupPlan (SPEC_V2_0_MULTISIDE_FIXTURING.md §3.1): 3-axis vs 5-axis 3+2 clamping orientations."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

Workholding = Literal["vise_hardjaws", "vise_softjaws", "vise_5axis"]


class _M(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SetupStep(_M):
    index: int = Field(ge=1, description="Op number")
    normals: list[tuple[float, float, float]] = Field(description="machining directions (outward normals) reached")
    faces: list[str] = Field(description="faces / planes machined in this setup")
    feature_ids: list[str] = Field(default_factory=list)
    deck_off: bool = Field(False, description="this setup faces the raw-stock allowance off (no features needed)")
    clamping: str = Field(description="operator advice (NL)")
    workholding: Workholding


class SetupPlan(_M):
    min_setups_3axis: int = Field(ge=1)
    min_setups_5axis_3plus2: int = Field(ge=1)
    deck_off_required: bool
    softjaws_recommended: bool = Field(description="Op 2 cannot grip on the original rectangular stock faces")
    setups_3axis: list[SetupStep]
    setups_5axis_3plus2: list[SetupStep]
    notes: list[str] = Field(default_factory=list)
