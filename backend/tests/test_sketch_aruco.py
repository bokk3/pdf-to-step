"""ArUco A4 sketch template: tilted photo -> rectified sheet + exact view crops (DRAWING_SPEC.md §7 phase 3)."""

from __future__ import annotations

import cv2
import numpy as np
import pymupdf
import pytest

from app.schemas.part_spec import PartSpec
from app.services.sketch_ingestor import PX_PER_MM, SketchError, cap_confidence, rectify
from app.services.sketch_template import LAYOUT, MARKERS, template_pdf
from tests.conftest import load_fixture
from tests.sketch_photo import RECT_MM, photo


def test_template_pdf_is_a4_landscape_with_markers_and_boxes():
    doc = pymupdf.open(stream=template_pdf(), filetype="pdf")
    page = doc[0]
    assert doc.page_count == 1 and (round(page.rect.width / 72 * 25.4), round(page.rect.height / 72 * 25.4)) == (297, 210)
    text = page.get_text()
    for s in ("VOORAANZICHT", "BOVENAANZICHT", "ZIJAANZICHT", "(0,0)", "Dikte Z", "ISO 2768-mK"):
        assert s in text, s
    pix = page.get_pixmap(dpi=150, alpha=False)
    img = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width, 3)
    d = cv2.aruco.ArucoDetector(cv2.aruco.getPredefinedDictionary(cv2.aruco.DICT_4X4_50))
    _, ids, _ = d.detectMarkers(cv2.cvtColor(img, cv2.COLOR_RGB2GRAY))
    assert sorted(ids.flatten().tolist()) == sorted(MARKERS)


def test_tilted_photo_rectifies_and_crops_exactly(tmp_path):
    res = rectify(photo(tilt=True), tmp_path)
    assert res.markers_found == [0, 1, 2, 3]
    assert res.reprojection_error_px < 1.5
    assert res.size_px == (int(297 * PX_PER_MM), int(210 * PX_PER_MM))
    scan = cv2.imread(str(res.scan), cv2.IMREAD_GRAYSCALE)

    # the pencil rectangle lands where it was drawn on the template (±1.5 mm)
    x0, y0, x1, y1 = RECT_MM
    win = scan[int((y0 - 10) * PX_PER_MM):int((y1 + 10) * PX_PER_MM), int((x0 - 10) * PX_PER_MM):int((x1 + 10) * PX_PER_MM)]
    ys, xs = np.nonzero(win < 128)
    bx0 = xs.min() / PX_PER_MM + x0 - 10
    by0 = ys.min() / PX_PER_MM + y0 - 10
    bx1 = xs.max() / PX_PER_MM + x0 - 10
    assert abs(bx0 - x0) < 1.5 and abs(by0 - y0) < 1.5 and abs(bx1 - x1) < 1.5

    # the light-cyan grid is gone in the scan: an empty part of the side view is (almost) white
    sx0, sy0, sx1, sy1 = LAYOUT["side"]
    empty = scan[int((sy0 + 8) * PX_PER_MM):int((sy1 - 8) * PX_PER_MM), int((sx0 + 8) * PX_PER_MM):int((sx1 - 8) * PX_PER_MM)]
    assert (empty < 128).mean() < 0.01

    for key in ("front", "side", "top", "title"):
        crop = cv2.imread(str(res.crops[key]), cv2.IMREAD_GRAYSCALE)
        bx = LAYOUT[key]
        assert abs(crop.shape[1] - ((bx[2] - bx[0]) + 4) * PX_PER_MM) <= 2  # box + 2 mm margin on both sides


def test_missing_marker_is_reported(tmp_path):
    with pytest.raises(SketchError, match=r"\[2\]"):
        rectify(photo(drop_marker=2), tmp_path)


def test_sketch_values_are_capped_for_review():
    spec = cap_confidence(load_fixture("bracket_plate"))
    assert spec.input_source == "paper_sketch"
    confs = [spec.stock.confidence, spec.outer_profile.confidence] + [f.confidence for f in spec.features]
    assert max(confs) <= 0.75
    assert len(spec.review_items()) >= len(spec.features)  # everything shows up amber in the Spec tab
    assert isinstance(spec, PartSpec)
