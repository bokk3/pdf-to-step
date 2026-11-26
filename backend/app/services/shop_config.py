"""Workshop configuration files in data/config/ (SPEC_V1_5_CAM_SHOPFLOOR.md §3).

The repository ships defaults in app/defaults/; the first load copies them to data/config/ so operator edits never
live in git. Every save validates the payload and keeps a timestamped backup of the previous file.
"""

from __future__ import annotations

import json
import shutil
import time
from pathlib import Path
from typing import TypeVar

from pydantic import BaseModel

from app.config import Settings

DEFAULTS_DIR = Path(__file__).resolve().parent.parent / "defaults"
MAX_BACKUPS = 30
M = TypeVar("M", bound=BaseModel)


def config_dir(s: Settings) -> Path:
    return s.data_dir / "config"


def _path(s: Settings, name: str) -> Path:
    return config_dir(s) / name


def load_config(s: Settings, name: str, model: type[M]) -> M:
    """Validated config; the shipped default is installed on first use. A corrupt file falls back to the default
    (the broken file is kept as <name>.broken-<ts> for inspection)."""
    p = _path(s, name)
    if not p.exists():
        p.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(DEFAULTS_DIR / name, p)
    try:
        return model.model_validate_json(p.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        p.replace(p.with_name(f"{p.stem}.broken-{time.strftime('%Y%m%d-%H%M%S')}{p.suffix}"))
        shutil.copyfile(DEFAULTS_DIR / name, p)
        return model.model_validate_json(p.read_text(encoding="utf-8"))


def save_config(s: Settings, name: str, obj: BaseModel) -> Path:
    p = _path(s, name)
    p.parent.mkdir(parents=True, exist_ok=True)
    if p.exists():
        bdir = p.parent / "backups"
        bdir.mkdir(exist_ok=True)
        shutil.copyfile(p, bdir / f"{p.stem}.{time.strftime('%Y%m%d-%H%M%S')}-{time.time_ns() % 1_000_000:06d}{p.suffix}")
        for old in sorted(bdir.glob(f"{p.stem}.*{p.suffix}"))[:-MAX_BACKUPS]:
            old.unlink(missing_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(obj.model_dump(mode="json"), indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    tmp.replace(p)
    return p
