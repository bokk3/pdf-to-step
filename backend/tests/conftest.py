from __future__ import annotations

import os
from pathlib import Path

import pytest

os.environ.pop("PYTHONDONTWRITEBYTECODE", None)

from app.config import Settings  # noqa: E402
from app.schemas.part_spec import PartSpec  # noqa: E402

FIXTURES = Path(__file__).parent / "fixtures"
TOL = 0.01  # §8.2 dimensional tolerance (mm)


@pytest.fixture
def fixtures_dir() -> Path:
    return FIXTURES


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(_env_file=None, data_dir=tmp_path / "data", deepseek_api_key=None, critic_enabled=False)


def load_fixture(name: str) -> PartSpec:
    return PartSpec.model_validate_json((FIXTURES / f"{name}.part_spec.json").read_text(encoding="utf-8"))


@pytest.fixture
def bracket_spec() -> PartSpec:
    return load_fixture("bracket_plate")


@pytest.fixture
def section6_spec() -> PartSpec:
    return load_fixture("section6_example")


def make_spec(features: list[dict] | None = None, L: float = 100.0, W: float = 60.0, T: float = 20.0,
              **extra) -> PartSpec:
    """Plain block, datum on the top face (z in [-T, 0])."""
    return PartSpec.model_validate(
        {"stock": {"length_x": L, "width_y": W, "thickness_z": T}, "features": features or [], **extra}
    )


@pytest.fixture
def client_factory(settings):
    """``with client_factory() as c:`` - the app on the temp data dir with the offline mock LLM."""
    from fastapi.testclient import TestClient

    from app.main import create_app
    from app.services.deepseek_client import MockDeepSeekClient

    def make(llm=None):
        return TestClient(create_app(settings, llm=llm or MockDeepSeekClient([FIXTURES]), use_process_pool=False))

    return make