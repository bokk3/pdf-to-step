from __future__ import annotations

from pathlib import Path

from PIL import Image

from app.services.pdf_parser import parse_pdf


def test_parse_fixture(fixtures_dir: Path, tmp_path: Path):
    parsed = parse_pdf(fixtures_dir / "bracket_plate.pdf", tmp_path, dpi=200, max_pixels=1300 * 1300)
    assert parsed.has_text_layer
    assert parsed.page_size_mm[0] == __import__("pytest").approx(297, abs=0.5)

    texts = {s.text for s in parsed.spans}
    assert "Ø10 H7 (+0.015/0) THRU" in texts
    assert "PART NO: BRK-120-A" in texts
    tagged = {s.text: s.tags for s in parsed.spans}
    assert "diameter" in tagged["Ø10 H7 (+0.015/0) THRU"] and "fit" in tagged["Ø10 H7 (+0.015/0) THRU"]
    assert "thread" in tagged["2x M6x1 - 12 DEEP  DRILL Ø5 x 15"]

    # circles: 8 cbore/hole circles + pocket hole + csk pair + 2x2 tapped circles
    diameters = sorted(round(c.diameter_mm, 1) for c in parsed.circles)
    assert diameters.count(11.0) == 4 and diameters.count(6.6) == 4 and 10.0 in diameters

    # title block detected from vector rectangles
    x0, y0, x1, y1 = parsed.title_block_mm
    assert abs(x0 - 187) < 1 and abs(y0 - 160) < 1 and abs(x1 - 287) < 1 and abs(y1 - 200) < 1

    names = [t.name for t in parsed.tiles]
    assert names == ["sheet", "tile_r0c0", "tile_r0c1", "tile_r1c0", "tile_r1c1", "title_block"]
    for t in parsed.tiles:
        with Image.open(tmp_path / t.llm_path) as im:
            assert im.width * im.height <= 1300 * 1300
        assert (tmp_path / t.path).exists()
    # tiles overlap and cover the sheet
    r00, r11 = parsed.tiles[1].rect_mm, parsed.tiles[4].rect_mm
    assert r00[2] > r11[0] and r00[3] > r11[1]

    for f in ("text_spans.json", "vectors.json", "summary.json", "llm_text_digest.txt"):
        assert (tmp_path / "extraction" / f).exists()
    digest = parsed.text_digest()
    assert "tile=tile_r" in digest and "Ø6.6" in digest
