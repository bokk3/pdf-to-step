"""v1.5 stream D: multi-page PDF batch splitter, bounded concurrency, ZIP export (SPEC_V1_5 §2.5)."""

from __future__ import annotations

import io
import json
import shutil
import time
import zipfile
from pathlib import Path

import pymupdf
import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.services.deepseek_client import MockDeepSeekClient
from app.services.pdf_splitter import (
    PageInfo,
    analyse_pages,
    apply_llm_answers,
    group_pages,
    parse_title_block,
    write_group_pdf,
)

PAGES = [  # (title-block lines, body text far away from the title block)
    (["Tekeningnr.: BRK-120-A", "Blad 1/2", "Materiaal EN AW-6082"], "Plaat"),
    (["Tekeningnr.: BRK-120-A", "Blad 2/2", "Materiaal EN AW-6082"], "Doorsnede A-A"),
    (["Drawing No. MP-A12-01", "Sheet 1 of 3", "Material S235"], "Mounting plate"),
    (["Sheet 2 of 3", "Scale 1:1  ISO 2768-mK"], "Details"),  # continuation sheet without a number
    (None, "scanned page"),  # no text layer in the title block
    (["Part no: AS-0042", "Sheet 1 of 1", "Material POM-C"], "Axle spacer"),
]
EXPECTED = [("BRK-120-A", [1, 2]), ("MP-A12-01", [3, 4, 5]), ("AS-0042", [6])]


def make_batch_pdf(path: Path, pages=PAGES) -> Path:
    doc = pymupdf.open()
    for lines, body in pages:
        page = doc.new_page(width=842, height=595)  # A4 landscape
        page.insert_text((60, 120), body, fontsize=14)
        page.draw_rect(pymupdf.Rect(480, 470, 830, 585))
        for i, ln in enumerate(lines or []):
            page.insert_text((490, 490 + i * 14), ln, fontsize=9)
        if lines is None:  # a "scan": an image only, no text
            pix = pymupdf.Pixmap(pymupdf.csGRAY, pymupdf.IRect(0, 0, 40, 20), False)
            pix.clear_with(200)
            page.insert_image(pymupdf.Rect(500, 500, 700, 560), pixmap=pix)
    doc.save(path)
    return path


@pytest.mark.parametrize("text,part,sheet", [
    ("Tekeningnr.: BRK-120-A\nBlad 2/2", "BRK-120-A", (2, 2)),
    ("DRAWING NO\nMP-A12-01\nSHEET 1 OF 3", "MP-A12-01", (1, 3)),
    ("Artikelnummer 4711-22\nblad 1 van 1", "4711-22", (1, 1)),
    ("Zeichnungsnr. ZN-1000.3 Blatt 2 von 4", "ZN-1000.3", (2, 4)),
    ("ISO 2768-mK  scale 1:2  XY-12345", "XY-12345", (None, None)),  # unlabelled fallback, ISO ignored
    ("Sheet 3 of 2 nothing", None, (None, None)),  # impossible sheet indicator
])
def test_parse_title_block(text, part, sheet):
    p, si, sn = parse_title_block(text)
    assert p == part and (si, sn) == sheet


def test_grouping_rules():
    infos = [PageInfo(0, 40, "A-100", 1, 2), PageInfo(1, 40, None, 2, 2, True), PageInfo(2, 0, source="none"),
             PageInfo(3, 40, "B-200", 1, 1), PageInfo(4, 40, "A-100", 3, 3, True), PageInfo(5, 40, None, 1, 1)]
    g = group_pages(infos)
    assert [(x.part_number, x.pages) for x in g] == [("A-100", [0, 1, 2, 4]), ("B-200", [3]), ("PAGE-6", [5])]


def test_llm_answers_only_fill_scanned_pages():
    infos = [PageInfo(0, 40, "A-100", 1, 1), PageInfo(1, 0, source="none"), PageInfo(2, 0, source="none")]
    apply_llm_answers(infos, [{"page_index": 0, "part_number": "WRONG"}, {"page_index": 1, "part_number": "c-7",
                              "sheet_index": 1}, {"page_index": 2, "part_number": None, "is_continuation": True}])
    assert infos[0].part_number == "A-100" and infos[1].part_number == "C-7" and infos[1].source == "llm"
    assert [(x.part_number, x.pages) for x in group_pages(infos)] == [("A-100", [0]), ("C-7", [1, 2])]


def test_analyse_synthetic_six_page_pdf(tmp_path):
    pdf = make_batch_pdf(tmp_path / "rfq.pdf")
    infos = analyse_pages(pdf)
    assert [i.source for i in infos] == ["text", "text", "text", "text", "none", "text"]
    assert infos[3].part_number is None and infos[3].is_continuation  # "Sheet 2 of 3" continues MP-A12-01
    groups = group_pages(infos)
    assert [(g.part_number, [p + 1 for p in g.pages]) for g in groups] == EXPECTED
    out = write_group_pdf(pdf, groups[1].pages, tmp_path / "mp.pdf")
    with pymupdf.open(out) as d:
        assert d.page_count == 3 and "Mounting plate" in d[0].get_text()


# --------------------------------------------------------------------------- API


@pytest.fixture
def batch_client(settings, fixtures_dir: Path, tmp_path):
    specs = tmp_path / "batch_fixtures"
    specs.mkdir()
    for part, _ in EXPECTED:  # the mock LLM answers <child filename stem>.part_spec.json
        shutil.copy(fixtures_dir / "bracket_plate.part_spec.json", specs / f"{part}.part_spec.json")
    s = settings.model_copy(update={"max_concurrent_jobs": 2})
    app = create_app(s, llm=MockDeepSeekClient([specs, fixtures_dir], fake_complex_code=True), use_process_pool=False)
    with TestClient(app) as c:
        yield c, s


def wait_batch(client, batch_id: str, timeout: float = 600) -> dict:
    t0 = time.time()
    while time.time() - t0 < timeout:
        st = client.get(f"/api/batches/{batch_id}").json()
        if st["status"] in ("done", "failed") and st["finished_count"] == st["job_count"]:
            return st
        time.sleep(0.5)
    raise TimeoutError(st)


def test_batch_upload_throttle_sse_and_zip(batch_client, tmp_path):
    client, s = batch_client
    pdf = make_batch_pdf(tmp_path / "RFQ 2026-114.pdf").read_bytes()
    r = client.post("/api/batches", files={"file": ("RFQ 2026-114.pdf", pdf, "application/pdf")})
    assert r.status_code == 202, r.text
    body = r.json()
    assert body["job_count"] == 3 and body["page_count"] == 6
    assert [(j["part_number"], j["pages"]) for j in body["jobs"]] == EXPECTED

    st = wait_batch(client, body["batch_id"])
    assert st["status"] == "done" and st["ready_count"] == 3, st
    assert [j["part_number"] for j in st["jobs"]] == [p for p, _ in EXPECTED]
    assert all(j["dfm"]["passed"] for j in st["jobs"])
    pipeline = client.app.state.pipeline
    assert 1 <= pipeline.batch_peak <= s.max_concurrent_jobs  # bounded concurrency

    child = client.get(f"/api/jobs/{st['jobs'][1]['job_id']}").json()
    assert child["batch_id"] == body["batch_id"] and child["part_number"] == "MP-A12-01"
    with pymupdf.open(s.jobs_dir / child["job_id"] / "original.pdf") as d:
        assert d.page_count == 3

    with client.stream("GET", f"/api/batches/{body['batch_id']}/events") as sse:
        text = "".join(sse.iter_text())
    assert "event: batch" in text and "event: end" in text
    snap = json.loads(text.split("event: batch\ndata: ")[1].split("\n\n")[0])
    assert snap["job_count"] == 3

    z = client.get(f"/api/batches/{body['batch_id']}/download-zip")
    assert z.status_code == 200 and 'RFQ_2026-114_STEP.zip' in z.headers["content-disposition"]
    names = set(zipfile.ZipFile(io.BytesIO(z.content)).namelist())
    for part, _ in EXPECTED:
        assert f"{part}/{part}_v1.step" in names and f"{part}/{part}_v1_instelblad.pdf" in names
        assert f"{part}/{part}_v1_ISO.pdf" in names
    with zipfile.ZipFile(io.BytesIO(z.content)) as zf:
        assert zf.read("AS-0042/AS-0042_v1.step").startswith(b"ISO-10303-21")

    lst = client.get("/api/batches").json()
    assert lst[0]["batch_id"] == body["batch_id"] and "jobs" not in lst[0]


def test_batch_rejects_non_pdf_and_unknown(batch_client):
    client, _ = batch_client
    assert client.post("/api/batches", files={"file": ("x.pdf", b"nope", "application/pdf")}).status_code == 415
    assert client.get("/api/batches/bnope").status_code == 404
