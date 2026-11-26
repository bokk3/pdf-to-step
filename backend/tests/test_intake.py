"""DRAWING_SPEC.md §6 intake endpoints end to end (offline): sketch template, from-dxf, from-step, from-sketch."""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.services.cad_compiler import compile_part, export_step_file
from app.services.deepseek_client import MockDeepSeekClient
from tests.conftest import load_fixture
from tests.sketch_photo import photo
from tests.test_dxf_parser import bracket_dxf


def wait_done(client: TestClient, job_id: str, timeout: float = 240) -> dict:
    t0 = time.time()
    while time.time() - t0 < timeout:
        job = client.get(f"/api/jobs/{job_id}").json()
        if job["status"] in ("ready", "failed", "awaiting_dimensions") and not job["running"]:
            return job
        time.sleep(0.3)
    raise TimeoutError(job)


@pytest.fixture
def mock_llm() -> MockDeepSeekClient:
    return MockDeepSeekClient([])


@pytest.fixture
def client(settings, mock_llm):
    app = create_app(settings, llm=mock_llm, use_process_pool=False)
    with TestClient(app) as c:
        yield c


def test_sketch_template_download(client):
    r = client.get("/api/templates/sketch-a4.pdf")
    assert r.status_code == 200 and r.headers["content-type"] == "application/pdf"
    assert r.content[:5] == b"%PDF-" and "schetssjabloon_A4.pdf" in r.headers["content-disposition"]


def test_from_dxf_builds_without_ai(client, settings, mock_llm):
    r = client.post("/api/jobs/from-dxf", files={"file": ("plate.dxf", bracket_dxf(), "application/dxf")})
    assert r.status_code == 202, r.text
    job = wait_done(client, r.json()["job_id"])
    assert job["status"] == "ready", job
    assert mock_llm.calls == []  # zero AI calls
    spec = job["part_spec"]
    assert spec["input_source"] == "dxf" and spec["stock"]["thickness_z"] == 20
    assert job["versions"][0]["source"] == "dxf"
    for name in ("model.step", "drawing.pdf", "preview.glb", "original.pdf"):
        assert name in job["artifacts"], name
    jd = settings.jobs_dir / job["job_id"]
    assert (jd / "original.dxf").exists()
    assert client.post("/api/jobs/from-dxf", files={"file": ("x.dxf", b"0\nSECTION\n0\nEOF\n", "application/dxf")}).status_code == 422


def test_from_dxf_missing_thickness_waits_for_operator(client):
    import ezdxf

    from tests.test_dxf_parser import rounded_rect, to_bytes

    doc = ezdxf.new("R2018")
    doc.header["$INSUNITS"] = 4
    doc.modelspace().add_lwpolyline(rounded_rect(0, 0, 80, 50, 4), format="xyb", close=True, dxfattribs={"layer": "CONTOUR"})
    doc.modelspace().add_circle((40, 25), 3, dxfattribs={"layer": "HOLE_THRU"})
    job_id = client.post("/api/jobs/from-dxf", files={"file": ("p.dxf", to_bytes(doc), "application/dxf")}).json()["job_id"]
    job = wait_done(client, job_id)
    assert job["status"] == "awaiting_dimensions" and "model.step" not in job["artifacts"]
    spec = job["part_spec"]
    assert [m["field_path"] for m in spec["missing_dimensions"]] == ["stock.thickness_z"]
    spec["stock"]["thickness_z"] = 12
    spec["missing_dimensions"] = []
    out = client.patch(f"/api/jobs/{job_id}/spec", json=spec).json()
    assert out["status"] == "ready" and out["version"] == 2


def test_from_step_documents_the_upload(client, settings, mock_llm, tmp_path):
    spec = load_fixture("bracket_plate")
    step = tmp_path / "bracket.step"
    export_step_file(compile_part(spec).shape, step)
    r = client.post("/api/jobs/from-step", files={"file": ("bracket.step", step.read_bytes(), "application/step")})
    assert r.status_code == 202, r.text
    job = wait_done(client, r.json()["job_id"])
    assert job["status"] == "ready", job
    assert mock_llm.calls == []
    jd = settings.jobs_dir / job["job_id"]
    info = json.loads((jd / "step_import.json").read_text(encoding="utf-8"))
    assert info["complete"] is True
    got = job["part_spec"]
    assert got["input_source"] == "step" and got["outer_profile"]["type"] == "rounded_rectangle"
    assert any("kernboor M6" in a for a in got["ambiguities"])  # threads are a question, not a guess
    assert job["dfm_report"]["passed"] and "drawing.pdf" in job["artifacts"]
    assert client.post("/api/jobs/from-step", files={"file": ("x.step", b"hello", "application/step")}).status_code == 415


def test_incomplete_step_keeps_its_geometry_on_edit(client, settings, tmp_path):
    from build123d import Box, Cylinder, Pos, Rot

    part = Box(60, 40, 10) - Pos(30, 0, 0) * Rot(35, 0, 0) * Cylinder(3, 60)  # inclined bore: not a PartSpec feature
    step = tmp_path / "odd.step"
    export_step_file(part, step)
    job = wait_done(client, client.post("/api/jobs/from-step", files={"file": ("odd.step", step.read_bytes(), "x")}).json()["job_id"])
    assert job["status"] == "ready", job
    jd = settings.jobs_dir / job["job_id"]
    assert json.loads((jd / "step_import.json").read_text(encoding="utf-8"))["complete"] is False
    v1 = job["dfm_report"]["geometry"]["volume_mm3"]
    assert v1 == pytest.approx(part.volume, rel=1e-6)
    out = client.patch(f"/api/jobs/{job['job_id']}/spec", json=job["part_spec"]).json()
    assert out["status"] == "ready" and out["dfm_report"]["geometry"]["volume_mm3"] == pytest.approx(v1, rel=1e-9)


def test_from_sketch_rectifies_then_waits_for_dimensions(client, settings, mock_llm):
    r = client.post("/api/jobs/from-sketch", files={"file": ("schets.jpg", photo(), "image/jpeg")})
    assert r.status_code == 202, r.text
    job = wait_done(client, r.json()["job_id"])
    assert job["status"] == "awaiting_dimensions"  # the offline mock cannot read handwriting -> asks
    assert mock_llm.calls == ["extract_sketch"]
    jd = settings.jobs_dir / job["job_id"]
    for f in ("tiles/sheet.llm.png", "tiles/tile_top.llm.png", "tiles/title_block.llm.png", "original.pdf"):
        assert (jd / f).exists(), f
    assert job["part_spec"]["input_source"] == "paper_sketch"
    assert max(f["confidence"] for f in [job["part_spec"]["stock"]]) <= 0.75

    blank = client.post("/api/jobs/from-sketch", files={"file": ("x.png", _png_without_markers(), "image/png")})
    assert blank.status_code == 422 and "merker" in blank.json()["detail"]
    assert client.post("/api/jobs/from-sketch", files={"file": ("x.gif", b"GIF89a....", "image/gif")}).status_code == 415


def _png_without_markers() -> bytes:
    import cv2
    import numpy as np

    ok, buf = cv2.imencode(".png", np.full((900, 1200, 3), 240, np.uint8))
    return buf.tobytes()
