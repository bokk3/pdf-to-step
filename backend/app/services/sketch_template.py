"""Printable A4 sketch template with 4 ArUco markers (DRAWING_SPEC.md §3 route A).

A4 landscape, first-angle boxes (Vooraanzicht top-left, Zijaanzicht top-right, Bovenaanzicht below the front
view), the datum (0, 0) in the bottom-left corner of the top view, title fields and a checklist. The light-cyan
grid disappears in the red channel, so the ingestor keeps only pencil / pen strokes.

All coordinates are millimetres from the top-left page corner (y down); ``sketch_ingestor`` uses the same
``LAYOUT`` to rectify photos and crop the boxes.
"""

from __future__ import annotations

from functools import lru_cache

PAGE_W, PAGE_H = 297.0, 210.0
MARKER = 18.0  # marker side incl. the black border
MARKER_INSET = 8.0
ARUCO_DICT = "DICT_4X4_50"
# id -> top-left corner of the marker (clockwise from top-left)
MARKERS = {
    0: (MARKER_INSET, MARKER_INSET),
    1: (PAGE_W - MARKER_INSET - MARKER, MARKER_INSET),
    2: (PAGE_W - MARKER_INSET - MARKER, PAGE_H - MARKER_INSET - MARKER),
    3: (MARKER_INSET, PAGE_H - MARKER_INSET - MARKER),
}
LAYOUT = {  # x0, y0, x1, y1
    "front": (32.0, 14.0, 170.0, 74.0),
    "side": (176.0, 14.0, 265.0, 74.0),
    "top": (32.0, 80.0, 170.0, 198.0),
    "title": (176.0, 80.0, 265.0, 150.0),
    "checklist": (176.0, 153.0, 265.0, 198.0),
}
LABELS = {"front": "VOORAANZICHT (FRONT)", "side": "ZIJAANZICHT (LINKS)", "top": "BOVENAANZICHT (TOP)"}
TITLE_FIELDS = [("Onderdeel / part", ""), ("Materiaal / material", ""), ("Dikte Z / thickness (mm)", ""),
                ("Alg. tolerantie", "ISO 2768-mK"), ("Aantal / qty", ""), ("Naam + datum", "")]
CHECKLIST = ["Maten in mm, gemeten vanaf (0,0)", "Dikte Z ingevuld", "Diepte bij elke pocket / boring (↧)",
             "Draad als M6 ↧12, pasmaat als Ø8 H7", "Niet op schaal: de getallen tellen", "Hele blad + 4 merkers in beeld"]

GRID = (0.72, 0.88, 0.93)  # light cyan, invisible in the red channel
INK = (0.0, 0.0, 0.0)
SOFT = (0.35, 0.35, 0.35)


def marker_bits(marker_id: int) -> list[list[int]]:
    """6x6 cells (1 = black) of a DICT_4X4_50 marker including its 1-cell black border."""
    import cv2

    d = cv2.aruco.getPredefinedDictionary(getattr(cv2.aruco, ARUCO_DICT))
    img = cv2.aruco.generateImageMarker(d, marker_id, 6, borderBits=1)
    return [[1 if img[r, c] < 128 else 0 for c in range(6)] for r in range(6)]


def marker_corners_mm(marker_id: int) -> list[tuple[float, float]]:
    """Outer corners (TL, TR, BR, BL) as cv2.aruco reports them, in template mm."""
    x, y = MARKERS[marker_id]
    return [(x, y), (x + MARKER, y), (x + MARKER, y + MARKER), (x, y + MARKER)]


@lru_cache(maxsize=1)
def template_pdf() -> bytes:
    import pymupdf

    from app.services.iso_template import MM, default_font_path

    doc = pymupdf.open()
    page = doc.new_page(width=PAGE_W * MM, height=PAGE_H * MM)
    page.insert_font(fontname="dv", fontfile=default_font_path())
    R = lambda x0, y0, x1, y1: pymupdf.Rect(x0 * MM, y0 * MM, x1 * MM, y1 * MM)  # noqa: E731
    P = lambda x, y: pymupdf.Point(x * MM, y * MM)  # noqa: E731

    def text(s: str, x: float, y: float, size: float, color=INK) -> None:
        page.insert_text(P(x, y), s, fontname="dv", fontsize=size * MM, color=color)  # size = em height in mm

    # ArUco markers (white quiet zone + black cells)
    cell = MARKER / 6
    for mid, (mx, my) in MARKERS.items():
        page.draw_rect(R(mx - 2, my - 2, mx + MARKER + 2, my + MARKER + 2), color=None, fill=(1, 1, 1))
        for r, row in enumerate(marker_bits(mid)):
            for c, v in enumerate(row):
                if v:
                    page.draw_rect(R(mx + c * cell, my + r * cell, mx + (c + 1) * cell, my + (r + 1) * cell),
                                   color=None, fill=INK, width=0)
        text(str(mid), mx + MARKER + 1.2, my + 3, 2.2, SOFT)

    # view boxes with a 5 / 10 mm grid
    for key in ("front", "side", "top"):
        x0, y0, x1, y1 = LAYOUT[key]
        k = 0
        x = x0 + 5
        while x < x1 - 1e-6:
            page.draw_line(P(x, y0), P(x, y1), color=GRID, width=(0.35 if k % 2 else 0.6) * MM / 2)
            x += 5
            k += 1
        k = 0
        y = y1 - 5
        while y > y0 + 1e-6:
            page.draw_line(P(x0, y), P(x1, y), color=GRID, width=(0.35 if k % 2 else 0.6) * MM / 2)
            y -= 5
            k += 1
        page.draw_rect(R(x0, y0, x1, y1), color=SOFT, width=0.35 * MM)
        text(LABELS[key], x0 + 1.5, y0 - 1.3, 2.6, SOFT)

    # datum (0, 0): bottom-left of the top view
    x0, _, _, y1 = LAYOUT["top"]
    page.draw_circle(P(x0, y1), 2.2 * MM, color=INK, width=0.4 * MM)
    page.draw_line(P(x0, y1), P(x0 + 14, y1), color=INK, width=0.4 * MM)
    page.draw_line(P(x0, y1), P(x0, y1 - 14), color=INK, width=0.4 * MM)
    page.draw_polyline([P(x0 + 12, y1 - 1), P(x0 + 14, y1), P(x0 + 12, y1 + 1)], color=INK, width=0.4 * MM)
    page.draw_polyline([P(x0 - 1, y1 - 12), P(x0, y1 - 14), P(x0 + 1, y1 - 12)], color=INK, width=0.4 * MM)
    text("X", x0 + 15, y1 + 1, 2.6)
    text("Y", x0 - 1, y1 - 15.5, 2.6)
    text("(0,0)", x0 + 2.5, y1 + 4.0, 2.6)

    # projection hint
    fx0, fy0, fx1, _ = LAYOUT["front"]
    text("Eerste-hoekprojectie (EN ISO 5456-2): bovenaanzicht ONDER het vooraanzicht, linker zijaanzicht RECHTS ervan.",
         fx0, 8.5, 2.2, SOFT)

    # title fields
    tx0, ty0, tx1, ty1 = LAYOUT["title"]
    page.draw_rect(R(tx0, ty0, tx1, ty1), color=INK, width=0.5 * MM)
    text("TITELBLOK / TITLE", tx0 + 1.5, ty0 - 1.3, 2.6, SOFT)
    rh = (ty1 - ty0) / len(TITLE_FIELDS)
    for i, (label, value) in enumerate(TITLE_FIELDS):
        yy = ty0 + i * rh
        if i:
            page.draw_line(P(tx0, yy), P(tx1, yy), color=SOFT, width=0.25 * MM)
        text(label, tx0 + 1.5, yy + 3.2, 2.1, SOFT)
        if value:
            text(value, tx0 + 4, yy + rh - 2.2, 3.2, SOFT)

    # checklist
    cx0, cy0, cx1, cy1 = LAYOUT["checklist"]
    page.draw_rect(R(cx0, cy0, cx1, cy1), color=SOFT, width=0.3 * MM)
    for i, item in enumerate(CHECKLIST):
        yy = cy0 + 5.5 + i * 6.6
        page.draw_rect(R(cx0 + 2, yy - 2.6, cx0 + 5, yy + 0.4), color=INK, width=0.3 * MM)
        text(item, cx0 + 7, yy, 2.4)
    text(f"PDF→STEP schetssjabloon v1 · ArUco {ARUCO_DICT} id 0-3 · druk af op 100 % (A4 liggend)", 60, 204.5, 2.0, SOFT)
    doc.set_metadata({"title": "PDF-to-STEP sketch template A4", "subject": "first-angle sketch sheet with ArUco markers"})
    data = doc.tobytes(garbage=3, deflate=True)
    doc.close()
    return data
