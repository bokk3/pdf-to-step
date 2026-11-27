"""Revision diff contracts (SPEC_V2_0_MULTISIDE_FIXTURING.md §3.3)."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

ChangeCategory = Literal["BREAKING_CAM", "GEOMETRY_CHANGE", "METADATA_ONLY"]
CATEGORY_RANK = {"METADATA_ONLY": 0, "GEOMETRY_CHANGE": 1, "BREAKING_CAM": 2}


class _M(BaseModel):
    model_config = ConfigDict(extra="forbid")


class FieldChange(_M):
    path: str
    before: Any = None
    after: Any = None


class FeatureDiffItem(_M):
    category: ChangeCategory
    change: Literal["added", "removed", "modified", "moved", "changed"]
    scope: Literal["feature", "edge_treatment", "stock", "tilted_plane", "material", "general_tolerance", "metadata",
                   "outer_profile"]
    feature_type: str | None = None
    id_old: str | None = None
    id_new: str | None = None
    face: str | None = None
    moved_mm: float | None = None
    fields: list[FieldChange] = Field(default_factory=list)
    message: str


class DiffVolumes(_M):
    old_mm3: float
    new_mm3: float
    added_mm3: float
    removed_mm3: float
    unchanged_mm3: float
    added_bodies: int
    removed_bodies: int


class RevisionDiffRequest(_M):
    job_old_id: str
    job_new_id: str
    version_old: int | None = None
    version_new: int | None = None


class RevisionDiffReport(_M):
    diff_id: str
    job_old_id: str
    job_new_id: str
    version_old: int
    version_new: int
    part_number: str | None = None
    revision_old: str | None = None
    revision_new: str | None = None
    overall: ChangeCategory | Literal["IDENTICAL"]
    counts: dict[str, int] = Field(default_factory=dict)
    volumes: DiffVolumes | None = None
    items: list[FeatureDiffItem] = Field(default_factory=list)
    artifacts: dict[str, str] = Field(default_factory=dict)
