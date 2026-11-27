"""v1.5 stream C: QR codes, setup sheet, correction telemetry, accuracy metrics, shopfloor payload."""

from __future__ import annotations

import copy
from pathlib import Path

import numpy as np
import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.services.deepseek_client import MockDeepSeekClient
from app.services.qr_helper import qr_matrix, qr_rects, shopfloor_url
from app.services.telemetry import spec_diff
from tests.conftest import load_fixture
from tests.test_api import upload, wait_file, wait_terminal

BASE = "https://cnc.example.test"


def decode_qr(pdf: Path, page: int = 0) -> str:
    """Render a PDF page and decode the (vector) QR code with OpenCV."""
    import cv2
    import pymupdf as fitz

    with fitz.open(pdf) as doc:
        pix = doc[page].get_pixmap(dpi=300)
    img = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width, pix.n)
    gray = cv2.cvtColor(img, cv2.COLOR_RGB2GRAY if pix.n == 3 else cv2.COLOR_RGBA2GRAY)
    text, _, _ = cv2.QRCodeDetector().detectAndDecode(gray)
    return text


def pdf_text(pdf: Path) -> str:
    import pymupdf as fitz

    with fitz.open(pdf) as doc:
        return "\n".join(p.get_text() for p in doc)


# --------------------------------------------------------------------------- telemetry (pure)


def test_spec_diff_is_structured_and_ignores_bookkeeping():
    a = load_fixture("section6_example").model_dump(mode="json")
    b = copy.deepcopy(a)
    assert spec_diff(a, b) == {"was_modified": False, "changed_feature_types": [], "field_diffs": []}

    for f in b["features"]:
        f["confidence"] = 0.99  # confidence ticks are not corrections
    b["drawing_metadata"]["drawing_number"] = "X"
    assert spec_diff(a, b)["was_modified"] is False

    hole = next(f for f in b["features"] if f["type"] == "hole")
    hole["diameter"] = hole["diameter"] + 0.5
    b["features"].append({"id": "NEW", "type": "slot", "face": "top"})
    b["stock"]["length_x"] += 1
    d = spec_diff(a, b)
    assert d["was_modified"] is True
    assert set(d["changed_feature_types"]) == {"hole", "slot", "stock"}
    paths = {x["path"]: x for x in d["field_diffs"]}
    assert paths[f"features.{hole['id']}.diameter"]["after"] == hole["diameter"]
    assert paths["features.NEW"]["change"] == "added"
    assert "stock.length_x" in paths


def test_qr_helper():
    assert shopfloor_url(type("S", (), {"public_tailscale_url": BASE + "/"})(), "abc") == f"{BASE}/shopfloor/abc"
    assert shopfloor_url(type("S", (), {"public_tailscale_url": ""})(), "abc") is None
    m = qr_matrix(f"{BASE}/shopfloor/abc")
    assert len(m) == len(m[0]) >= 25 and not any(m[0])  # quiet zone
    rects = qr_rects(f"{BASE}/shopfloor/abc", 10, 20, 30)
    assert all(10 <= x0 < x1 <= 40 + 1e-9 and 20 <= y0 < y1 <= 50 + 1e-9 for x0, y0, x1, y1 in rects)


# --------------------------------------------------------------------------- API


@pytest.fixture
def qr_client(settings, fixtures_dir: Path):
    s = settings.model_copy(update={"public_tailscale_url": BASE})
    app = create_app(s, llm=MockDeepSeekClient([fixtures_dir], fake_complex_code=True), use_process_pool=False)
    with TestClient(app) as c:
        yield c, s


def test_setup_sheet_and_drawing_carry_a_scannable_qr(qr_client, fixtures_dir: Path):
    client, s = qr_client
    job_id = upload(client, fixtures_dir)
    job = wait_terminal(client, job_id)
    assert job["status"] == "ready", job
    vdir = s.jobs_dir / job_id / "versions" / "v1"
    assert wait_file(vdir / "setup_sheet.pdf") and wait_file(vdir / "drawing.pdf")
    url = f"{BASE}/shopfloor/{job_id}"
    assert decode_qr(vdir / "setup_sheet.pdf") == url
    assert decode_qr(vdir / "drawing.pdf") == url

    text = pdf_text(vdir / "setup_sheet.pdf")
    assert "BRK-120-A" in text and "G54" in text
    rep = job["dfm_report"]
    assert rep["stock_recommendation"]["profile_label"] in text
    assert any(a["tool_id"] in text for a in rep["recommended_tools"])

    r = client.get(f"/api/jobs/{job_id}/artifacts/setup_sheet.pdf")
    assert r.status_code == 200 and r.content[:5] == b"%PDF-" and "instelblad" in r.headers["content-disposition"]
    r = client.post(f"/api/jobs/{job_id}/setup-sheet")
    assert r.status_code == 200 and "setup_sheet.pdf" in r.json()["artifacts"]


def test_no_public_url_means_no_qr(client_factory, fixtures_dir: Path, settings):
    with client_factory() as client:
        job_id = upload(client, fixtures_dir)
        assert wait_terminal(client, job_id)["status"] == "ready"
        pdf = settings.jobs_dir / job_id / "versions" / "v1" / "setup_sheet.pdf"
        assert wait_file(pdf)
        assert decode_qr(pdf) == "" and "PUBLIC_" in pdf_text(pdf)


def test_corrections_verify_and_accuracy(qr_client, fixtures_dir: Path):
    client, s = qr_client
    assert client.get("/api/metrics/accuracy").json()["verified_jobs"] == 0

    a = upload(client, fixtures_dir)
    b = upload(client, fixtures_dir)
    ja, jb = wait_terminal(client, a), wait_terminal(client, b)

    # job a: verified untouched
    r = client.post(f"/api/jobs/{a}/verify")
    assert r.status_code == 200 and r.json()["diff"]["was_modified"] is False
    assert client.get(f"/api/jobs/{a}").json()["verified_at"]

    # job b: operator corrects a pocket depth, then verifies
    spec = jb["part_spec"]
    next(f for f in spec["features"] if f["id"] == "P1")["depth"] = 7.0
    assert client.patch(f"/api/jobs/{b}/spec", json=spec).status_code == 200
    d = client.post(f"/api/jobs/{b}/verify").json()["diff"]
    assert d["was_modified"] and d["changed_feature_types"] == ["pocket_rect"]
    assert d["field_diffs"] == [{"path": "features.P1.depth", "before": 6.0, "after": 7.0}]

    m = client.get("/api/metrics/accuracy").json()
    assert m["verified_jobs"] == 2 and m["untouched_verified_jobs"] == 1 and m["untouched_pass_rate"] == 0.5
    assert m["corrections_per_feature_type"] == {"pocket_rect": 1}
    assert m["corrected_fields_in_verified_jobs"] == {"depth": 1}


def test_shopfloor_payload(qr_client, fixtures_dir: Path):
    client, _ = qr_client
    job_id = upload(client, fixtures_dir)
    wait_terminal(client, job_id)
    p = client.get(f"/api/jobs/{job_id}/shopfloor").json()
    assert p["part_name"] == "BRK-120-A Mounting plate" and p["version"] == 1
    assert p["palette"]["thread"]["hex"] == "#0066FF"
    assert p["color_counts"] == {"thread": 2, "tight_fit": 1, "seat": 9, "datum": 1}
    cats = {f["category"] for f in p["features"]}
    assert {"thread", "tight_fit", "seat"} <= cats
    threaded = next(f for f in p["features"] if f["category"] == "thread")
    assert threaded["label"].startswith(("2x M", "M", "4x M")) and threaded["anchors"]
    assert all(len(pt) == 3 for f in p["features"] for pt in f["anchors"])
    assert {"preview.glb", "drawing.pdf"} <= set(p["artifacts"])
    assert client.get("/api/jobs/nope/shopfloor").status_code == 404
