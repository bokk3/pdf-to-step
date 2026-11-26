"""Application settings, loaded from environment / .env (project root or backend/)."""

from __future__ import annotations

import ipaddress
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

BACKEND_DIR = Path(__file__).resolve().parents[1]
PROJECT_ROOT = BACKEND_DIR.parent

Thinking = Literal["default", "enabled", "disabled"]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=(str(PROJECT_ROOT / ".env"), str(BACKEND_DIR / ".env")),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- DeepSeek (server-side only; never sent to the browser) -------------
    deepseek_api_key: str | None = None
    deepseek_base_url: str = "https://api.deepseek.com"
    deepseek_vision_model: str = "deepseek-flash"
    deepseek_code_model: str = "deepseek-v4-pro"
    deepseek_thinking: Thinking = Field("enabled", description="Thinking mode for extraction / spec correction")
    deepseek_critic_thinking: Thinking = Field("enabled", description="Thinking mode for the visual critic")
    # Reasoning tokens count against max_tokens; too small a budget yields empty completions.
    deepseek_max_tokens: int = 65536
    deepseek_timeout_s: float = 600.0
    deepseek_use_files_api: bool = Field(True, description="Upload drawing images once and reuse them by file_id")
    deepseek_mock: bool = Field(False, description="Force the offline mock client (tests / no key)")

    # --- Server: loopback only (exposure via `tailscale serve`) -------------
    host: str = "127.0.0.1"
    port: int = 8000

    # --- Storage --------------------------------------------------------------
    data_dir: Path = PROJECT_ROOT / "data"

    # --- Pipeline ---------------------------------------------------------------
    max_heal_retries: int = 3
    sandbox_timeout_s: float = 15.0
    critic_enabled: bool = True
    render_dpi: int = 300
    llm_image_max_pixels: int = 1300 * 1300
    cad_workers: int = 2
    step_schema: Literal["AP214", "AP242"] = "AP214"
    step_face_colors: bool = Field(True, description="§8.6 colour threaded (blue) / H7 (red) bores via XCAF")
    drawing_enabled: bool = Field(True, description="EN ISO detail drawing (drawing.pdf/.svg/.dxf) per version")
    drawing_owner: str = Field("", description="title block 'owner' field (company name)")
    extraction_prompt_appendix: str = Field("", description="extra extraction instructions (prompt A/B benchmarks)")
    sketch_ocr: Literal["off", "text_recognition", "rich_layout_recognition"] = Field(
        "off", description="DeepSeek Files API text layer for hand sketches (DRAWING_SPEC §3 route A experiment)")
    sketch_ocr_wait_s: float = Field(3.0, ge=0, description="recognition runs asynchronously after the upload")

    # --- DFM thresholds (§8) ------------------------------------------------------
    dfm_bbox_tolerance_mm: float = 0.01
    dfm_min_corner_radius_mm: float = 1.5
    dfm_max_hole_depth_ratio: float = 5.0
    dfm_max_pocket_depth_per_radius: float = 4.0
    dfm_min_wall_mm: float = 1.5

    @field_validator("host")
    @classmethod
    def _loopback_only(cls, v: str) -> str:
        if v == "localhost":
            return "127.0.0.1"
        try:
            if ipaddress.ip_address(v).is_loopback:
                return v
        except ValueError:
            pass
        raise ValueError(f"HOST must be a loopback address (got {v!r}); expose via `tailscale serve`")

    @field_validator("data_dir")
    @classmethod
    def _abs_data_dir(cls, v: Path) -> Path:
        return v if v.is_absolute() else (PROJECT_ROOT / v).resolve()

    @property
    def jobs_dir(self) -> Path:
        return self.data_dir / "jobs"

    @property
    def db_path(self) -> Path:
        return self.data_dir / "jobs.sqlite3"

    @property
    def llm_enabled(self) -> bool:
        return bool(self.deepseek_api_key) and not self.deepseek_mock


@lru_cache
def get_settings() -> Settings:
    s = Settings()
    s.jobs_dir.mkdir(parents=True, exist_ok=True)
    return s
