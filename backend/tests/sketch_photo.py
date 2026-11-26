"""Synthetic 'phone photo' of a filled-in sketch template, for the ArUco tests."""

from __future__ import annotations

import cv2
import numpy as np
import pymupdf

from app.services.sketch_template import PAGE_H, PAGE_W, template_pdf

DPI = 150
PX = DPI / 25.4
RECT_MM = (50.0, 120.0, 120.0, 170.0)  # pencil rectangle drawn inside the top-view box


def template_image() -> np.ndarray:
    doc = pymupdf.open(stream=template_pdf(), filetype="pdf")
    pix = doc[0].get_pixmap(dpi=DPI, alpha=False)
    img = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width, 3)[:, :, ::-1].copy()
    doc.close()
    return img


def photo(tilt: bool = True, drop_marker: int | None = None, seed: int = 3) -> bytes:
    img = template_image()
    x0, y0, x1, y1 = (int(round(v * PX)) for v in RECT_MM)
    cv2.rectangle(img, (x0, y0), (x1, y1), (70, 70, 70), 3)  # graphite
    cv2.putText(img, "70", ((x0 + x1) // 2 - 20, y1 + 30), cv2.FONT_HERSHEY_SIMPLEX, 1.1, (60, 60, 60), 2)
    if drop_marker is not None:  # cover one marker (e.g. a thumb over the corner)
        from app.services.sketch_template import MARKER, MARKERS

        mx, my = MARKERS[drop_marker]
        cv2.rectangle(img, (int((mx - 3) * PX), int((my - 3) * PX)), (int((mx + MARKER + 3) * PX), int((my + MARKER + 3) * PX)),
                      (210, 200, 190), -1)
    h, w = img.shape[:2]
    rng = np.random.default_rng(seed)
    canvas_w, canvas_h = 2300, 1750
    if tilt:
        dst = np.float32([[170, 120], [2080, 230], [2010, 1590], [110, 1490]])
    else:
        dst = np.float32([[100, 100], [100 + w, 100], [100 + w, 100 + h], [100, 100 + h]])
    src = np.float32([[0, 0], [w, 0], [w, h], [0, h]])
    M = cv2.getPerspectiveTransform(src, dst)
    out = cv2.warpPerspective(img, M, (canvas_w, canvas_h), borderValue=(120, 105, 95))  # workbench
    shade = np.linspace(0.85, 1.05, canvas_w)[None, :, None]  # uneven light
    out = np.clip(out * shade + rng.normal(0, 4, out.shape), 0, 255).astype(np.uint8)
    out = cv2.GaussianBlur(out, (3, 3), 0.8)
    ok, buf = cv2.imencode(".jpg", out, [cv2.IMWRITE_JPEG_QUALITY, 88])
    assert ok
    return buf.tobytes()


__all__ = ["photo", "RECT_MM", "PAGE_W", "PAGE_H"]
