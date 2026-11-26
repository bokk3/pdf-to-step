"""Photo / scan of a filled-in sketch template -> rectified sheet + view crops (DRAWING_SPEC.md §3 route A).

Deterministic OpenCV part only (the extraction prompt lives in deepseek_client.extract_sketch_spec):

1. decode (EXIF orientation applied), detect the 4 ArUco markers (DICT_4X4_50, ids 0-3)
2. homography from all 16 marker corners to their template positions, warp to a flat A4 at ``PX_PER_MM``
3. "scan" version: red channel (drops the light-cyan grid) + CLAHE + adaptive threshold
4. crop the front / side / top / title boxes by template coordinates

Hard rule from the spec (§8): nothing is inferred here; missing numbers become ``missing_dimensions`` later.
"""

from __future__ import annotations

import io
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from app.services.sketch_template import ARUCO_DICT, LAYOUT, MARKERS, PAGE_H, PAGE_W, marker_corners_mm

PX_PER_MM = 5.0
SCAN_RATIO = 0.62  # strokes darker than 62 % of the local paper white are kept
MAX_CONFIDENCE = 0.75  # every value read from a hand sketch must be confirmed by the operator


class SketchError(ValueError):
    pass


@dataclass
class SketchResult:
    sheet: Path  # rectified colour image
    scan: Path  # binarised, grid removed
    crops: dict[str, Path] = field(default_factory=dict)
    markers_found: list[int] = field(default_factory=list)
    reprojection_error_px: float = 0.0
    size_px: tuple[int, int] = (0, 0)


def decode_image(data: bytes) -> np.ndarray:
    """BGR image with EXIF orientation applied (phone photos)."""
    from PIL import Image, ImageOps

    try:
        img = ImageOps.exif_transpose(Image.open(io.BytesIO(data))).convert("RGB")
    except Exception as exc:  # noqa: BLE001
        raise SketchError(f"Afbeelding kon niet gelezen worden: {exc}") from exc
    arr = np.asarray(img)[:, :, ::-1].copy()
    if max(arr.shape[:2]) < 600:
        raise SketchError("Afbeelding is te klein (minstens 600 px aan de lange zijde).")
    return arr


def detect_markers(bgr: np.ndarray) -> dict[int, np.ndarray]:
    import cv2

    gray = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
    d = cv2.aruco.getPredefinedDictionary(getattr(cv2.aruco, ARUCO_DICT))
    params = cv2.aruco.DetectorParameters()
    params.cornerRefinementMethod = cv2.aruco.CORNER_REFINE_SUBPIX
    det = cv2.aruco.ArucoDetector(d, params)
    found: dict[int, np.ndarray] = {}
    scale = 1.0
    work = gray
    if max(gray.shape) > 2400:  # big phone photos: detect on a downscaled copy, refine positions afterwards
        scale = 2400 / max(gray.shape)
        work = cv2.resize(gray, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
    corners, ids, _ = det.detectMarkers(work)
    if ids is not None:
        for c, i in zip(corners, ids.flatten()):
            if int(i) in MARKERS and int(i) not in found:
                found[int(i)] = c.reshape(4, 2) / scale
    return found


def rectify(data: bytes, out_dir: Path) -> SketchResult:
    import cv2

    bgr = decode_image(data)
    found = detect_markers(bgr)
    missing = sorted(set(MARKERS) - set(found))
    if missing:
        raise SketchError(f"ArUco-merker(s) {missing} niet gevonden: fotografeer het hele blad met alle 4 de hoekmerkers "
                          f"scherp in beeld (gebruik het PDF→STEP schetssjabloon).")
    src = np.concatenate([found[i] for i in sorted(found)]).astype(np.float32)
    dst = np.concatenate([np.array(marker_corners_mm(i), dtype=np.float32) * PX_PER_MM for i in sorted(found)])
    H, _ = cv2.findHomography(src, dst, cv2.RANSAC, 3.0)
    if H is None:
        raise SketchError("Perspectiefcorrectie mislukt (merkers onduidelijk).")
    proj = cv2.perspectiveTransform(src.reshape(-1, 1, 2), H).reshape(-1, 2)
    err = float(np.sqrt(np.mean(np.sum((proj - dst) ** 2, axis=1))))
    W, Hh = int(round(PAGE_W * PX_PER_MM)), int(round(PAGE_H * PX_PER_MM))
    flat = cv2.warpPerspective(bgr, H, (W, Hh), flags=cv2.INTER_CUBIC, borderValue=(255, 255, 255))

    # Red channel divided by the local paper white: the light-cyan grid sits at ~0.72 of the paper, graphite and
    # ink well below 0.6, so one ratio threshold drops the grid under uneven light.
    red = flat[:, :, 2].astype(np.float32)
    paper = cv2.GaussianBlur(cv2.dilate(red, np.ones((15, 15), np.uint8)), (0, 0), 12)
    ratio = red / np.maximum(paper, 1.0)
    scan = np.where(ratio < SCAN_RATIO, 0, 255).astype(np.uint8)

    out_dir.mkdir(parents=True, exist_ok=True)
    res = SketchResult(out_dir / "sheet.png", out_dir / "sheet_scan.png", markers_found=sorted(found),
                       reprojection_error_px=round(err, 3), size_px=(W, Hh))
    cv2.imwrite(str(res.sheet), flat)
    cv2.imwrite(str(res.scan), scan)
    for key in ("front", "side", "top", "title"):
        x0, y0, x1, y1 = (int(round(v * PX_PER_MM)) for v in LAYOUT[key])
        pad = int(2 * PX_PER_MM)
        crop = scan[max(0, y0 - pad):y1 + pad, max(0, x0 - pad):x1 + pad]
        p = out_dir / f"{key}.png"
        cv2.imwrite(str(p), crop)
        res.crops[key] = p
    return res


def sheet_pdf(image: Path, out_pdf: Path) -> None:
    """Rectified sheet as an A4-landscape PDF (shown as the 'original' drawing in the UI)."""
    import pymupdf

    from app.services.iso_template import MM

    doc = pymupdf.open()
    page = doc.new_page(width=PAGE_W * MM, height=PAGE_H * MM)
    page.insert_image(page.rect, filename=str(image))
    doc.save(str(out_pdf), garbage=3, deflate=True)
    doc.close()


def cap_confidence(spec):
    """Sketch values are never trusted blindly: every confidence <= 0.75 (shown amber, must be confirmed)."""
    from app.schemas.part_spec import PartSpec

    d = spec.model_dump()

    def walk(o):
        if isinstance(o, dict):
            for k, v in o.items():
                if k == "confidence" and isinstance(v, (int, float)):
                    o[k] = min(float(v), MAX_CONFIDENCE)
                else:
                    walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)

    walk(d)
    d["input_source"] = "paper_sketch"
    return PartSpec.model_validate(d)
