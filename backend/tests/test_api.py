"""§7 REST & SSE contract, end-to-end with the offline mock LLM (no network)."""

from __future__ import annotations

import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.services.deepseek_client import MockDeepSeekClient


def wait_terminal(client: TestClient, job_id: str, timeout: float = 240) -> dict:
    t0 = time.time()
    while time.time() - t0 < timeout:
        job = client.get(f"/api/jobs/{job_id}").json()
        if job["status"] in ("ready", "failed") and not job["running"]:
            return job
        time.sleep(0.3)
    raise TimeoutError(job)


def wait_file(path: Path, timeout: float = 60) -> bool:
    t0 = time.time()
    while time.time() - t0 < timeout:
        if path.exists():
            return True
        time.sleep(0.3)
    return False


@pytest.fixture
def mock_llm(fixtures_dir: Path) -> MockDeepSeekClient:
    return MockDeepSeekClient([fixtures_dir], fake_complex_code=True)


@pytest.fixture
def client(settings, mock_llm):
    app = create_app(settings, llm=mock_llm, use_process_pool=False)
    with TestClient(app) as c:
        yield c


def upload(client, fixtures_dir: Path, name: str = "bracket_plate.pdf") -> str:
    pdf = (fixtures_dir / "bracket_plate.pdf").read_bytes()
    r = client.post("/api/jobs", files={"file": (name, pdf, "application/pdf")})
    assert r.status_code == 202, r.text
    body = r.json()
    assert body["status"] == "queued" and body["job_id"]
    return body["job_id"]


def test_health(client):
    r = client.get("/api/health", params={"deep": True}).json()
    assert r["status"] == "ok" and r["occt"]["ok"] and r["bind"] == "127.0.0.1:8000"
    assert "api_key" not in str(r).lower()  # the key never reaches the browser


def test_rejects_non_pdf(client):
    assert client.post("/api/jobs", files={"file": ("x.pdf", b"hello", "application/pdf")}).status_code == 415


def test_upload_to_ready_with_traceability(client, fixtures_dir: Path, settings):
    job_id = upload(client, fixtures_dir)
    job = wait_terminal(client, job_id)
    assert job["status"] == "ready", job
    assert job["part_spec"]["part_name"] == "BRK-120-A Mounting plate"
    assert job["dfm_report"]["passed"] is True
    assert not [i for i in job["dfm_report"]["issues"] if i["code"] == "CALLOUT_UNMATCHED"]
    assert job["dfm_report"]["colored_faces"] == {"thread": 2, "tight_tolerance": 1}
    assert job["iteration_logs"][0]["gate_passed"] is True
    assert set(job["artifacts"]) >= {"original.pdf", "model.step", "preview.glb", "ortho_check.png", "dfm_report.json"}

    jd = settings.jobs_dir / job_id
    for rel in ["original.pdf", "extraction/text_spans.json", "extraction/vectors.json", "tiles/sheet.png",
                "tiles/title_block.png", "tiles/tile_r1c1.png", "specs/part_spec_v1.json", "versions/v1/model.step",
                "versions/v1/preview.glb", "versions/v1/ortho_check.png", "versions/v1/ortho_iso.png",
                "versions/v1/dfm_report.json", "model.step", "preview.glb", "ortho_check.png", "dfm_report.json",
                "iteration_logs.json"]:
        assert (jd / rel).exists(), rel

    lst = client.get("/api/jobs").json()
    assert lst[0]["job_id"] == job_id and lst[0]["dfm"]["passed"] is True

    step = client.get(f"/api/jobs/{job_id}/artifacts/model.step")
    assert step.status_code == 200 and step.content.startswith(b"ISO-10303-21")
    assert "bracket_plate_v1.step" in step.headers["content-disposition"]
    assert client.get(f"/api/jobs/{job_id}/artifacts/preview.glb").content[:4] == b"glTF"
    assert client.get(f"/api/jobs/{job_id}/artifacts/original.pdf").content[:5] == b"%PDF-"
    assert client.get(f"/api/jobs/{job_id}/artifacts/ortho_check.png").content[:4] == b"\x89PNG"
    assert client.get(f"/api/jobs/{job_id}/artifacts/..%2F..%2Fjobs.sqlite3").status_code == 404

    with client.stream("GET", f"/api/jobs/{job_id}/events") as s:
        body = "".join(s.iter_text())
    for stage in ("parsing_pdf", "extracting_features", "compiling_cad", "verifying", "ready"):
        assert f'"stage": "{stage}"' in body, stage
    assert "event: end" in body


def test_patch_spec_rebuilds_instantly_without_llm(client, fixtures_dir: Path, settings, mock_llm):
    job_id = upload(client, fixtures_dir)
    job = wait_terminal(client, job_id)
    calls_before = list(mock_llm.calls)

    spec = job["part_spec"]
    next(f for f in spec["features"] if f["id"] == "P1")["depth"] = 7.0
    spec["features"].append({"id": "P9", "type": "pocket_rect", "face": "top", "center_x": 100, "center_y": 40,
                             "length_x": 10, "width_y": 10, "depth": 2, "corner_radius": 0})
    r = client.patch(f"/api/jobs/{job_id}/spec", json=spec)
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["version"] == 2 and out["status"] == "ready"
    assert out["timings"]["total"] < 5, out["timings"]  # in-process here; ~1 s in a warm pool worker
    assert any(i["code"] == "SHARP_INTERNAL_CORNER" and i["feature_id"] == "P9" for i in out["dfm_report"]["issues"])
    assert out["artifacts"]["model.step"].endswith("?version=2")
    assert mock_llm.calls == calls_before  # PATCH never calls the LLM
    assert wait_file(settings.jobs_dir / job_id / "versions" / "v2" / "ortho_check.png")  # deferred render

    assert client.patch(f"/api/jobs/{job_id}/spec", json={"stock": {"length_x": -1}}).status_code == 422

    spec["features"].append({"id": "OUT", "type": "hole", "center_x": 900, "center_y": 900, "diameter": 5})
    out = client.patch(f"/api/jobs/{job_id}/spec", json=spec).json()
    assert out["version"] == 3 and out["status"] == "failed"
    assert any(i["code"] == "FEATURE_FAILED" for i in out["dfm_report"]["issues"])
    assert client.get(f"/api/jobs/{job_id}/artifacts/model.step", params={"version": 3}).status_code == 200

    v1 = client.get(f"/api/jobs/{job_id}/spec", params={"version": 1}).json()["part_spec"]
    assert next(f for f in v1["features"] if f["id"] == "P1")["depth"] == 6  # revisions are immutable
    assert [v["source"] for v in client.get(f"/api/jobs/{job_id}").json()["versions"]] == \
        ["extraction", "user_edit", "user_edit"]


def test_reextract_with_hint_and_complex_fallback(client, fixtures_dir: Path, mock_llm):
    job_id = upload(client, fixtures_dir, name="unknown_part.pdf")
    job = wait_terminal(client, job_id)
    assert job["part_spec"]["part_name"].startswith("ONGECONTROLEERD")  # mock placeholder

    spec = job["part_spec"]
    spec["features"] = [{"id": "X1", "type": "complex_3d", "description": "schuine uitsparing", "operation": "cut",
                         "bbox_min": [10, 10, -5], "bbox_max": [20, 20, 0.1]}]
    out = client.patch(f"/api/jobs/{job_id}/spec", json=spec).json()
    assert out["status"] == "failed"  # PATCH skips the LLM, so the fallback code is missing
    assert out["part_spec"]["requires_complex_3d_fallback"] is True

    r = client.post(f"/api/jobs/{job_id}/reextract", json={"hint": "Diepte van de pocket is 12mm"})
    assert r.status_code == 202
    job = wait_terminal(client, job_id)
    assert mock_llm.hints == ["Diepte van de pocket is 12mm"]
    assert job["status"] == "ready", job
    st = {s["feature_id"]: s for s in job["dfm_report"]["feature_status"]}
    assert st["X1"]["ok"] and st["X1"]["method"] == "llm_sandbox"
    assert [v["source"] for v in job["versions"]][-2:] == ["reextract", "complex_code"]
    assert job["dfm_report"]["geometry"]["volume_mm3"] == pytest.approx(100 * 50 * 10 - 10 * 10 * 5, rel=1e-6)

    assert client.delete(f"/api/jobs/{job_id}").status_code == 204
    assert client.get(f"/api/jobs/{job_id}").status_code == 404


def test_iso_drawing_per_version_and_overrides(client, fixtures_dir: Path, settings):
    """DRAWING_SPEC §6/§7: drawing.pdf/.svg/.dxf next to model.step for every revision, regenerable with overrides."""
    import pymupdf

    job_id = upload(client, fixtures_dir)
    job = wait_terminal(client, job_id)
    jd = settings.jobs_dir / job_id
    for rel in ("versions/v1/drawing.pdf", "versions/v1/drawing.svg", "versions/v1/drawing.dxf",
                "versions/v1/drawing.json", "drawing.pdf"):
        assert (jd / rel).exists(), rel
    assert job["drawing"]["sheet_size"] == "A3_LANDSCAPE" and job["drawing"]["scale"] == "1:1"
    assert not job["drawing"]["warnings"]
    assert job["artifacts"]["drawing.pdf"].endswith("?version=1")

    pdf = client.get(f"/api/jobs/{job_id}/artifacts/drawing.pdf")
    assert pdf.content[:5] == b"%PDF-" and pdf.headers["content-type"] == "application/pdf"
    assert pdf.headers["content-disposition"].startswith("inline") and "bracket_plate_v1_ISO.pdf" in \
        pdf.headers["content-disposition"]
    dxf = client.get(f"/api/jobs/{job_id}/artifacts/drawing.dxf")
    assert dxf.headers["content-disposition"].startswith("attachment") and b"SECTION" in dxf.content[:200]

    r = client.post(f"/api/jobs/{job_id}/drawing", json={"sheet_size": "A3_LANDSCAPE", "scale": "1:2",
                                                         "language": "EN_NL"})
    assert r.status_code == 200, r.text
    assert r.json()["scale"] == "1:2" and r.json()["artifacts"]["drawing.pdf"].endswith("?version=1")
    with pymupdf.open(jd / "drawing.pdf") as doc:  # promoted: v1 is the current version
        text = doc[0].get_text()
    assert "TITLE / BENAMING" in text and "1:2" in text
    assert client.post(f"/api/jobs/{job_id}/drawing", json={"scale": "half"}).status_code == 422
    assert client.post(f"/api/jobs/{job_id}/drawing", json={"engine": "freecad"}).status_code == 422

    spec = job["part_spec"]
    next(f for f in spec["features"] if f["id"] == "P1")["depth"] = 7.0
    out = client.patch(f"/api/jobs/{job_id}/spec", json=spec).json()
    assert out["version"] == 2 and out["status"] == "ready"
    assert wait_file(jd / "versions" / "v2" / "drawing.json")  # follows the instant rebuild in the background
    assert wait_file(jd / "versions" / "v2" / "drawing.pdf")
    with pymupdf.open(jd / "versions" / "v2" / "drawing.pdf") as doc:
        assert "POCKET 50x30 R5 ↧7" in doc[0].get_text()


def test_blocking_missing_dimensions_pause_the_job(client, fixtures_dir: Path, settings, mock_llm):
    """DRAWING_SPEC §5: blocking missing dimensions -> awaiting_dimensions, no CAD until PATCH resolves them."""
    job_id = upload(client, fixtures_dir)
    job = wait_terminal(client, job_id)
    jd = settings.jobs_dir / job_id
    spec = job["part_spec"]
    spec["missing_dimensions"] = [{"field_path": "features.P1.depth", "prompt_nl": "Diepte van pocket P1?",
                                   "suggested_default": 6.0, "blocking": True}]
    out = client.patch(f"/api/jobs/{job_id}/spec", json=spec).json()
    assert out["status"] == "awaiting_dimensions" and out["version"] == 2
    assert not (jd / "versions" / "v2" / "model.step").exists()
    events = client.get(f"/api/jobs/{job_id}/events/history").json()
    paused = [e for e in events if e["stage"] == "awaiting_dimensions"]
    assert paused and paused[-1]["data"]["missing"][0]["field_path"] == "features.P1.depth"
    with client.stream("GET", f"/api/jobs/{job_id}/events", params={"after": paused[-1]["id"]}) as s:
        assert "event: end" in "".join(s.iter_text())  # the SSE stream closes while paused

    spec["missing_dimensions"][0]["blocking"] = False  # operator confirmed the value
    out = client.patch(f"/api/jobs/{job_id}/spec", json=spec).json()
    assert out["status"] == "ready" and out["version"] == 3
    assert (jd / "versions" / "v3" / "model.step").exists()