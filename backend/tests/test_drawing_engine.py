"""EN ISO drawing engine (DRAWING_SPEC.md §7 phase 1 definition of done)."""

from __future__ import annotations

import itertools
import xml.etree.ElementTree as ET
from pathlib import Path

import pymupdf
import pytest

from app.schemas.part_spec import PartSpec
from app.services.cad_compiler import compile_part
from app.services.drawing_engine import generate_drawing
from app.services.iso_template import MM
from tests.conftest import load_fixture, make_spec


def render(spec: PartSpec, tmp_path: Path, **kw):
    shape = compile_part(spec).shape
    return generate_drawing(spec, shape, tmp_path, drawing_number="DWG-T-001", **kw)


def spans(pdf: Path) -> list[dict]:
    with pymupdf.open(pdf) as doc:
        page = doc[0]
        return [s for b in page.get_text("dict")["blocks"] for line in b.get("lines", []) for s in line["spans"]
                if s["text"].strip()]


@pytest.fixture(scope="module")
def bracket(tmp_path_factory):
    out = tmp_path_factory.mktemp("bracket")
    return render(load_fixture("bracket_plate"), out)


@pytest.fixture(scope="module")
def section6(tmp_path_factory):
    out = tmp_path_factory.mktemp("section6")
    return render(load_fixture("section6_example"), out)


def test_first_angle_layout(bracket, section6):
    for res in (bracket, section6):
        front, top, left = res.view_rects["front"], res.view_rects["top"], res.view_rects["left"]
        assert top[1] > front[3], "top view must sit below the front view"
        assert left[0] > front[2], "left view must sit right of the front view"
        assert abs(front[0] - top[0]) < 0.5, "front and top view share the X axis"
        assert abs(front[1] - left[1]) < 0.5 and abs(front[3] - left[3]) < 0.5, "front and left view share Z"
        assert "iso" in res.view_rects
        assert not res.warnings and not res.balloons


def test_vector_pdf_with_selectable_dimensions(bracket):
    texts = [s["text"] for s in spans(bracket.pdf)]
    joined = " | ".join(texts)
    for expected in ["120", "80", "12", "10", "35", "60", "85", "110", "68", "70", "40", "30", "90",
                     "4x Ø6.6 THRU", "Ø10 H7 THRU", "Ø4.5 THRU", "CSK Ø9 x 90°", "SLOT 8 ↧4", "POCKET 50x30 R5 ↧6",
                     "2x M6x1 ↧12", "Ø5 ↧15", "4x R6", "1x45°", "(6)", "(4)",
                     "BRK-120-A Mounting plate", "DWG-T-001", "EN AW-6082 T6", "ISO 2768-mK", "DRAFT", "1:1",
                     "GENERAL TOLERANCES ISO 2768-mK", "ISO 13715", "ISO 21920-1"]:
        assert any(expected in t for t in texts), f"{expected!r} not in PDF text: {joined}"
    assert any("CBORE Ø11 ↧6.8" in t or "⌴Ø11 ↧6.8" in t for t in texts)
    with pymupdf.open(bracket.pdf) as doc:
        assert doc.page_count == 1
        w, h = doc[0].rect.width / MM, doc[0].rect.height / MM
        assert (round(w), round(h)) == (420, 297)
        assert doc[0].get_images() == []  # pure vector, no raster


def test_no_text_overlaps(bracket, section6):
    for res in (bracket, section6):
        boxes = [pymupdf.Rect(s["bbox"]) for s in spans(res.pdf)]
        for a, b in itertools.combinations(boxes, 2):
            inter = a & b
            assert inter.is_empty or inter.get_area() < 1.0, f"text overlap {a} / {b}"
        # no text inside the projected part geometry of any view
        views = [pymupdf.Rect(*(v * MM for v in r)) for r in res.view_rects.values()]
        for t in boxes:
            for v in views:
                inter = t & v
                assert inter.is_empty or inter.get_area() < 2.0, f"text {t} overlaps a view {v}"


def test_svg_and_dxf_exports(bracket):
    root = ET.parse(bracket.svg).getroot()
    layers = {g.get("id") for g in root if g.tag.endswith("g")}
    assert {"FRAME", "TITLEBLOCK", "VISIBLE", "HIDDEN", "CENTER", "DIMENSIONS", "CALLOUTS", "NOTES"} <= layers
    assert root.get("width") == "420.0mm"

    import ezdxf

    doc = ezdxf.readfile(str(bracket.dxf))
    names = {layer.dxf.name for layer in doc.layers}
    assert {"VISIBLE", "HIDDEN", "CENTER", "DIMENSIONS", "TITLEBLOCK"} <= names
    texts = [e.dxf.text for e in doc.modelspace().query("TEXT")]
    assert "120" in texts and any("4x Ø6.6 THRU" in t for t in texts)
    hidden = doc.modelspace().query('LWPOLYLINE[layer=="HIDDEN"]')
    assert hidden and all(e.dxf.linetype == "DASHED" for e in hidden)


def test_small_part_stays_on_a4(tmp_path):
    spec = make_spec([{"id": "H1", "type": "hole", "center_x": 10, "center_y": 10, "diameter": 5, "through_all": True}],
                     L=60, W=40, T=8)
    res = render(spec, tmp_path)
    assert res.sheet_size == "A4_LANDSCAPE"
    assert res.scale in ("2:1", "1:1")


def test_many_features_promote_to_a3(tmp_path):
    feats = [{"id": f"H{i}", "type": "hole", "center_x": 10 + 7 * i, "center_y": 10, "diameter": 3, "through_all": True}
             for i in range(13)]
    res = render(make_spec(feats, L=120, W=40, T=10), tmp_path)
    assert res.sheet_size == "A3_LANDSCAPE"


def test_overrides_and_bilingual_title_block(tmp_path):
    spec = load_fixture("section6_example")
    res = render(spec, tmp_path, sheet_size="A3_LANDSCAPE", scale="1:5", language="EN_NL")
    texts = [s["text"] for s in spans(res.pdf)]
    assert "1:5" in texts and res.scale == "1:5"
    assert any("TITLE / BENAMING" in t for t in texts)


def test_generated_pdf_parses_back(bracket, tmp_path):
    from app.services.pdf_parser import parse_pdf

    parsed = parse_pdf(bracket.pdf, tmp_path, dpi=100)
    digest = parsed.text_digest()
    assert "4x Ø6.6 THRU" in digest and "POCKET 50x30 R5 ↧6" in digest
    assert parsed.title_block_mm is not None
    # ISO 5457 grid reference labels (1..8 / A..F in the margin) are not offered to the model as dimensions
    grid = [s for s in parsed.spans if "sheet_grid" in s.tags]
    assert len(grid) >= 12
    # (the revision letter "A" in the title block is a real span; only the margin labels are dropped)
    for s in grid:
        x0, y0 = s.bbox_mm[:2]
        assert f'"{s.text}" @({x0:.1f},{y0:.1f})' not in digest


def test_schema_v11_backwards_compatible():
    v10 = load_fixture("section6_example")
    assert v10.schema_version == "1.0" and v10.drawing_metadata.sheet_size == "AUTO"
    spec = PartSpec.model_validate({
        "schema_version": "1.1", "input_source": "paper_sketch",
        "stock": {"length_x": 100, "width_y": 50, "thickness_z": 10},
        "drawing_metadata": {"sheet_size": "A3_LANDSCAPE", "scale": "1:2", "language": "EN_NL"},
        "missing_dimensions": [{"field_path": "stock.thickness_z", "prompt_nl": "Plaatdikte ontbreekt", "blocking": True},
                               {"field_path": "features.P1.depth", "prompt_nl": "Diepte?", "blocking": False}],
    })
    assert [m.field_path for m in spec.blocking_missing] == ["stock.thickness_z"]
    with pytest.raises(ValueError):
        PartSpec.model_validate({"stock": {"length_x": 1, "width_y": 1, "thickness_z": 1},
                                 "drawing_metadata": {"scale": "half"}})
