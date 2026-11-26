"""Generate the sample drawing PDF + expected PartSpec used by tests and demos.

Run:  python tests/fixtures/make_fixtures.py
Outputs bracket_plate.pdf, bracket_plate.part_spec.json and section6_example.part_spec.json
(the ARCHITECTURE.md §6 example, verbatim) next to this file. Replace / extend with real
workshop drawings for the Milestone 2 regression benchmark.

The drawing is a first-angle A4 sheet: FRONT view on top, TOP (plan) view below,
title block bottom-right. Every value on the sheet is in the text layer so the
extraction can be checked against PyMuPDF output.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pymupdf

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[1]))

from app.schemas.part_spec import PartSpec  # noqa: E402

MM = 72 / 25.4
L, W, H = 120.0, 80.0, 12.0

# sheet placement (sheet mm, y down)
TOP_X0, TOP_Y0 = 35.0, 160.0  # part (0,0) in top view
FRONT_X0, FRONT_Y0 = 35.0, 58.0  # part (x, z=0) in front view

EXPECTED = {
    "schema_version": "1.0",
    "part_name": "BRK-120-A Mounting plate",
    "units": "mm",
    "material": "EN AW-6082 T6",
    "general_tolerance": "ISO 2768-mK",
    "datum_origin": "bottom_left_top_face",
    "stock": {"length_x": L, "width_y": W, "thickness_z": H, "confidence": 0.98},
    "outer_profile": {"type": "rounded_rectangle", "corner_radius": 6.0, "confidence": 0.95},
    "features": [
        {"id": "H1", "type": "hole", "face": "top", "center_x": 10, "center_y": 10,
         "pattern": {"type": "linear", "count_x": 2, "count_y": 2, "pitch_x": 100, "pitch_y": 60},
         "diameter": 6.6, "through_all": True, "counterbore": {"diameter": 11.0, "depth": 6.8},
         "confidence": 0.95, "source_note": "4x Ø6.6 THRU, CBORE Ø11 DEPTH 6.8"},
        {"id": "P1", "type": "pocket_rect", "face": "top", "center_x": 60, "center_y": 40,
         "length_x": 50, "width_y": 30, "corner_radius": 5, "depth": 6, "confidence": 0.93,
         "source_note": "POCKET 50x30 R5 DEPTH 6"},
        {"id": "H2", "type": "hole", "face": "top", "center_x": 60, "center_y": 40, "diameter": 10.0,
         "through_all": True, "tolerance": "H7", "confidence": 0.92, "source_note": "Ø10 H7 (+0.015/0) THRU"},
        {"id": "K1", "type": "hole", "face": "top", "center_x": 60, "center_y": 12, "diameter": 4.5,
         "through_all": True, "countersink": {"diameter": 9.0, "angle_deg": 90}, "confidence": 0.9,
         "source_note": "Ø4.5 THRU, CSK Ø9 x 90°"},
        {"id": "S1", "type": "slot", "face": "top", "start_x": 35, "start_y": 68, "end_x": 85, "end_y": 68,
         "width": 8, "depth": 4, "confidence": 0.9, "source_note": "SLOT 8 WIDE, DEPTH 4, CENTRES 50"},
        {"id": "T1", "type": "hole", "face": "front", "center_x": 30, "center_y": -6, "extra_positions": [[90, -6]],
         "diameter": 6.0, "depth": 15, "thread": "M6x1", "thread_depth": 12,
         "confidence": 0.88, "source_note": "2x M6x1 - 12 DEEP, DRILL Ø5 x 15"},
    ],
    "edge_treatments": [
        {"id": "E1", "type": "chamfer", "size": 1.0, "target": "top_outer_edges", "confidence": 0.9},
    ],
    "requires_complex_3d_fallback": False,
    "ambiguities": ["Notitie: alle scherpe kanten breken 0.2 en clear anodiseren 15 µm (niet gemodelleerd)."],
}

# ARCHITECTURE.md §6 example, verbatim
SECTION6_EXAMPLE = {
    "schema_version": "1.0",
    "part_name": "Montageplaat_A12",
    "units": "mm",
    "material": "AlMgSi1 (6082-T6)",
    "general_tolerance": "ISO 2768-mK",
    "datum_origin": "bottom_left_top_face",
    "stock": {"length_x": 200.0, "width_y": 100.0, "thickness_z": 20.0, "confidence": 0.98},
    "outer_profile": {"type": "rounded_rectangle", "corner_radius": 5.0, "points_2d": None, "confidence": 0.95},
    "features": [
        {"id": "feat_pocket_1", "type": "pocket_rect", "face": "top", "center_x": 100.0, "center_y": 50.0,
         "length_x": 120.0, "width_y": 60.0, "depth": 8.0, "corner_radius": 6.0, "confidence": 0.92,
         "source_note": "Top view central pocket"},
        {"id": "feat_hole_1", "type": "hole", "face": "top", "center_x": 15.0, "center_y": 15.0, "diameter": 8.0,
         "depth": None, "through_all": True, "thread": "M8", "counterbore": None, "tolerance": "H7",
         "confidence": 0.78, "source_note": "4x Ø8 callout slightly blurred"},
    ],
    "edge_treatments": [
        {"id": "edge_1", "type": "chamfer", "size": 1.0, "target": "top_outer_edges", "confidence": 0.90},
    ],
    "requires_complex_3d_fallback": False,
    "ambiguities": ["Verify whether the 4 corner holes are through-holes or 15mm blind."],
}


def rr(r: float, w: float, h: float) -> tuple[float, float]:
    """PyMuPDF corner radius is a fraction of each side."""
    return (min(r / w, 0.5), min(r / h, 0.5))


def P(x_mm: float, y_mm: float) -> pymupdf.Point:
    return pymupdf.Point(x_mm * MM, y_mm * MM)


def top(x: float, y: float) -> pymupdf.Point:
    return P(TOP_X0 + x, TOP_Y0 - y)


def front(x: float, z: float) -> pymupdf.Point:
    return P(FRONT_X0 + x, FRONT_Y0 - z)


def text(page, pt: pymupdf.Point, s: str, size: float = 7, rotate: int = 0) -> None:
    page.insert_text(pt, s, fontsize=size, fontname="helv", rotate=rotate)


def hdim(page, a: pymupdf.Point, b: pymupdf.Point, offset_mm: float, label: str) -> None:
    """Horizontal dimension between a and b, offset vertically (sheet mm, negative = up)."""
    y = a.y + offset_mm * MM
    sh = page.new_shape()
    sh.draw_line(pymupdf.Point(a.x, a.y), pymupdf.Point(a.x, y))
    sh.draw_line(pymupdf.Point(b.x, b.y), pymupdf.Point(b.x, y))
    sh.draw_line(pymupdf.Point(a.x, y), pymupdf.Point(b.x, y))
    for x, d in ((a.x, 1), (b.x, -1)):
        sh.draw_polyline([pymupdf.Point(x + d * 2.5, y - 0.9), pymupdf.Point(x, y), pymupdf.Point(x + d * 2.5, y + 0.9)])
    sh.finish(color=(0, 0, 0), width=0.25)
    sh.commit()
    text(page, pymupdf.Point((a.x + b.x) / 2 - len(label) * 1.7, y - 1.5), label)


def vdim(page, a: pymupdf.Point, b: pymupdf.Point, offset_mm: float, label: str) -> None:
    x = a.x + offset_mm * MM
    sh = page.new_shape()
    sh.draw_line(pymupdf.Point(a.x, a.y), pymupdf.Point(x, a.y))
    sh.draw_line(pymupdf.Point(b.x, b.y), pymupdf.Point(x, b.y))
    sh.draw_line(pymupdf.Point(x, a.y), pymupdf.Point(x, b.y))
    for y, d in ((a.y, 1), (b.y, -1)):
        sh.draw_polyline([pymupdf.Point(x - 0.9, y + d * 2.5), pymupdf.Point(x, y), pymupdf.Point(x + 0.9, y + d * 2.5)])
    sh.finish(color=(0, 0, 0), width=0.25)
    sh.commit()
    text(page, pymupdf.Point(x - 1.5, (a.y + b.y) / 2 + len(label) * 1.7), label, rotate=90)


def leader(page, at: pymupdf.Point, to: pymupdf.Point, label: str) -> None:
    sh = page.new_shape()
    sh.draw_line(at, to)
    sh.draw_line(to, pymupdf.Point(to.x + len(label) * 3.4, to.y))
    sh.finish(color=(0, 0, 0), width=0.25, closePath=False)
    sh.draw_circle(at, 0.6)
    sh.finish(color=(0, 0, 0), width=0.25, fill=(0, 0, 0))
    sh.commit()
    text(page, pymupdf.Point(to.x + 1, to.y - 1.2), label)


def draw(path: Path) -> None:
    doc = pymupdf.open()
    page = doc.new_page(width=297 * MM, height=210 * MM)

    # frame
    sh = page.new_shape()
    sh.draw_rect(pymupdf.Rect(P(10, 10), P(287, 200)))
    sh.finish(color=(0, 0, 0), width=0.7)
    sh.commit()

    # ---- TOP view ----------------------------------------------------------
    sh = page.new_shape()
    sh.draw_rect(pymupdf.Rect(top(0, W), top(L, 0)), radius=rr(6, L, W))  # R6 corners
    sh.draw_rect(pymupdf.Rect(top(1, W - 1), top(L - 1, 1)), radius=rr(5, L - 2, W - 2))  # chamfer inner line
    for x in (10, 110):
        for y in (10, 70):
            sh.draw_circle(top(x, y), 3.3 * MM)
            sh.draw_circle(top(x, y), 5.5 * MM)
    sh.draw_rect(pymupdf.Rect(top(35, 55), top(85, 25)), radius=rr(5, 50, 30))
    sh.draw_circle(top(60, 40), 5 * MM)
    sh.draw_circle(top(60, 12), 2.25 * MM)
    sh.draw_circle(top(60, 12), 4.5 * MM)
    sh.draw_rect(pymupdf.Rect(top(31, 72), top(89, 64)), radius=rr(4, 58, 8))  # slot (full round ends)
    sh.finish(color=(0, 0, 0), width=0.5)
    sh.commit()
    # centre lines
    sh = page.new_shape()
    sh.draw_line(top(60, -4), top(60, W + 4))
    sh.draw_line(top(-4, 40), top(L + 4, 40))
    sh.finish(color=(0, 0, 0), width=0.18, dashes="[6 1.5 1 1.5] 0")
    sh.commit()

    hdim(page, top(0, 0), top(L, 0), 14, "120")
    hdim(page, top(0, 10), top(10, 10), 4, "10")
    hdim(page, top(10, 10), top(110, 10), 9, "100")
    hdim(page, top(0, 40), top(60, 40), -48, "60")
    vdim(page, top(L, 0), top(L, W), 14, "80")
    vdim(page, top(L, 0), top(L, 10), 5, "10")
    vdim(page, top(L, 10), top(L, 70), 9, "60")
    vdim(page, top(0, 0), top(0, 40), -6, "40")
    vdim(page, top(0, 0), top(0, 12), -12, "12")
    vdim(page, top(0, 0), top(0, 68), -18, "68")

    leader(page, top(112.3, 72.3), P(TOP_X0 + 128, TOP_Y0 - 92), "4x Ø6.6 THRU  CBORE Ø11 DEPTH 6.8")
    leader(page, top(85, 50), P(TOP_X0 + 100, TOP_Y0 - 50), "POCKET 50x30 R5 DEPTH 6")
    leader(page, top(63.5, 43.5), P(TOP_X0 + 100, TOP_Y0 - 42), "Ø10 H7 (+0.015/0) THRU")
    leader(page, top(63.2, 9), P(TOP_X0 + 100, TOP_Y0 + 6), "Ø4.5 THRU  CSK Ø9 x 90°")
    leader(page, top(85, 70), P(TOP_X0 + 100, TOP_Y0 - 84), "SLOT 8 WIDE  DEPTH 4  CENTRES 50")
    hdim(page, top(35, 68), top(85, 68), -18, "50")
    leader(page, top(4, W - 4), P(TOP_X0 - 22, TOP_Y0 - W - 4), "R6 (4x)")
    text(page, P(TOP_X0, TOP_Y0 + 22), "TOP VIEW", 8)

    # ---- FRONT view (looking at the Y-min face) ------------------------------
    sh = page.new_shape()
    sh.draw_rect(pymupdf.Rect(front(0, H), front(L, 0)))
    sh.draw_line(front(0, H - 1), front(L, H - 1))  # chamfer line
    sh.finish(color=(0, 0, 0), width=0.5)
    sh.commit()
    for x in (30, 90):
        sh = page.new_shape()
        sh.draw_circle(front(x, 6), 2.5 * MM)
        sh.finish(color=(0, 0, 0), width=0.5)
        sh.commit()
        sh = page.new_shape()
        sh.draw_circle(front(x, 6), 3.0 * MM)
        sh.finish(color=(0, 0, 0), width=0.25)
        sh.commit()
    sh = page.new_shape()  # hidden: pocket, slot, cbores
    sh.draw_line(front(35, H - 6), front(85, H - 6))
    sh.draw_line(front(35, H), front(35, H - 6))
    sh.draw_line(front(85, H), front(85, H - 6))
    sh.draw_line(front(31, H - 4), front(89, H - 4))
    for x in (10, 110):
        sh.draw_line(front(x - 5.5, H), front(x - 5.5, H - 6.8))
        sh.draw_line(front(x + 5.5, H), front(x + 5.5, H - 6.8))
        sh.draw_line(front(x - 3.3, 0), front(x - 3.3, H - 6.8))
        sh.draw_line(front(x + 3.3, 0), front(x + 3.3, H - 6.8))
    sh.finish(color=(0, 0, 0), width=0.25, dashes="[2 1] 0")
    sh.commit()
    hdim(page, front(0, H), front(30, H), -8, "30")
    hdim(page, front(0, H), front(90, H), -14, "90")
    vdim(page, front(0, 0), front(0, H), -8, "12")
    vdim(page, front(L, 0), front(L, 6), 6, "6")
    leader(page, front(92.2, 8), P(FRONT_X0 + 128, FRONT_Y0 - 22), "2x M6x1 - 12 DEEP  DRILL Ø5 x 15")
    leader(page, front(L - 0.5, H - 0.5), P(FRONT_X0 + 128, FRONT_Y0 - 30), "1x45° TOP EDGES")
    text(page, P(FRONT_X0, FRONT_Y0 + 8), "FRONT VIEW", 8)

    # ---- title block ----------------------------------------------------------
    tb = pymupdf.Rect(P(187, 160), P(287, 200))
    sh = page.new_shape()
    sh.draw_rect(tb)
    for y in (168, 176, 184, 192):
        sh.draw_line(P(187, y), P(287, y))
    sh.draw_line(P(237, 160), P(237, 200))
    sh.finish(color=(0, 0, 0), width=0.5)
    sh.commit()
    rows = [
        ("TITLE: MOUNTING PLATE", "PART NO: BRK-120-A"),
        ("MATERIAL: EN AW-6082 T6", "REV: A"),
        ("GENERAL TOL: ISO 2768-mK", "SCALE: 1:1"),
        ("UNITS: mm", "SHEET: A4  1/1"),
        ("PROJECTION: FIRST ANGLE", "DRAWN: QA"),
    ]
    for i, (a, b) in enumerate(rows):
        text(page, P(189, 165.5 + 8 * i), a, 6.5)
        text(page, P(239, 165.5 + 8 * i), b, 6.5)
    text(page, P(14, 196), "NOTES: 1. BREAK ALL SHARP EDGES 0.2   2. ANODISE CLEAR 15µm", 6.5)
    doc.save(path, garbage=3, deflate=True)
    doc.close()


def main() -> None:
    import json

    spec = PartSpec.model_validate(EXPECTED)
    (HERE / "bracket_plate.part_spec.json").write_text(spec.model_dump_json(indent=2, exclude_none=True),
                                                      encoding="utf-8")
    PartSpec.model_validate(SECTION6_EXAMPLE)  # the architecture example must validate as-is
    (HERE / "section6_example.part_spec.json").write_text(json.dumps(SECTION6_EXAMPLE, indent=2, ensure_ascii=False),
                                                         encoding="utf-8")
    draw(HERE / "bracket_plate.pdf")
    print("wrote bracket_plate.pdf, bracket_plate.part_spec.json, section6_example.part_spec.json in", HERE)


if __name__ == "__main__":
    main()
