"""PyMuPDF text/vector extraction + smart tiling for the vision model.

deepseek-flash downsamples each image to roughly 1300x1300 total pixels, so a
whole A3 sheet loses small tolerance text. We therefore send:

* the full sheet (downscaled to the pixel budget) for global context,
* a 2x2 grid of overlapping high-resolution tiles,
* a title-block crop (detected from vector rectangles, with a fallback),
* the exact text layer (spans + positions in sheet millimetres) so numeric
  values never depend on OCR.
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path

import pymupdf
from PIL import Image

PT_TO_MM = 25.4 / 72.0
Image.MAX_IMAGE_PIXELS = 400_000_000

DIM_PATTERNS = [
    (re.compile(r"[Ø⌀∅]\s*\d+(?:[.,]\d+)?"), "diameter"),
    (re.compile(r"\bR\s*\d+(?:[.,]\d+)?"), "radius"),
    (re.compile(r"\bM\s*\d+(?:[.,]\d+)?(?:\s*[xX×]\s*\d+(?:[.,]\d+)?)?"), "thread"),
    (re.compile(r"±\s*\d+(?:[.,]\d+)?"), "tolerance"),
    (re.compile(r"[+-]\s*0[.,]\d+"), "deviation"),
    (re.compile(r"\d\s*[A-HJ-NP-QS-WYZa-hj-np-qs-wyz]{1,2}\d{1,2}\b"), "fit"),
    (re.compile(r"\d+(?:[.,]\d+)?\s*[xX×]\s*45\s*°?"), "chamfer"),
    (re.compile(r"\b\d+\s*[xX×]\b"), "count"),
    (re.compile(r"(?<![\w.])\d+(?:[.,]\d+)?(?![\w.])"), "number"),
]


@dataclass
class TextSpan:
    text: str
    bbox_mm: tuple[float, float, float, float]
    size_pt: float
    font: str
    angle_deg: float
    tags: list[str] = field(default_factory=list)


@dataclass
class TileInfo:
    name: str
    path: str  # relative to job dir
    llm_path: str  # downscaled copy sent to the LLM
    rect_mm: tuple[float, float, float, float]
    width_px: int
    height_px: int


@dataclass
class CircleHint:
    center_mm: tuple[float, float]
    diameter_mm: float


@dataclass
class ParsedPdf:
    page_index: int
    page_count: int
    page_size_mm: tuple[float, float]
    has_text_layer: bool
    spans: list[TextSpan]
    tiles: list[TileInfo]
    circles: list[CircleHint]
    vector_stats: dict[str, int]
    title_block_mm: tuple[float, float, float, float] | None

    def text_digest(self, max_spans: int = 700) -> str:
        """Compact, position-sorted text layer for the LLM prompt."""
        lines = [
            f"PAGE {self.page_index + 1}/{self.page_count}  size={self.page_size_mm[0]:.0f}x{self.page_size_mm[1]:.0f}mm  "
            f"text_layer={'yes' if self.has_text_layer else 'NO (scanned?)'}"
        ]
        spans = sorted((s for s in self.spans if "sheet_grid" not in s.tags),
                       key=lambda s: (round(s.bbox_mm[1] / 4), s.bbox_mm[0]))
        for s in spans[:max_spans]:
            x0, y0, x1, y1 = s.bbox_mm
            tile = self._tile_for(((x0 + x1) / 2, (y0 + y1) / 2))
            rot = f" rot={s.angle_deg:.0f}" if abs(s.angle_deg) > 1 else ""
            tags = f" [{','.join(s.tags)}]" if s.tags else ""
            lines.append(f'"{s.text}" @({x0:.1f},{y0:.1f})-({x1:.1f},{y1:.1f}) h={s.size_pt:.1f}pt{rot} tile={tile}{tags}')
        if len(spans) > max_spans:
            lines.append(f"... {len(spans) - max_spans} more spans truncated")
        return "\n".join(lines)

    def vector_digest(self, max_circles: int = 80) -> str:
        lines = [f"vector stats: {json.dumps(self.vector_stats)}"]
        if self.title_block_mm:
            lines.append("title block rect (sheet mm): (%.1f,%.1f)-(%.1f,%.1f)" % self.title_block_mm)
        if self.circles:
            lines.append("circles drawn on sheet (sheet mm, NOT part mm - divide by drawing scale):")
            for c in self.circles[:max_circles]:
                lines.append(f"  center=({c.center_mm[0]:.2f},{c.center_mm[1]:.2f}) dia={c.diameter_mm:.3f}")
        return "\n".join(lines)

    def _tile_for(self, pt: tuple[float, float]) -> str:
        for t in self.tiles:
            if t.name.startswith("tile_"):
                x0, y0, x1, y1 = t.rect_mm
                if x0 <= pt[0] <= x1 and y0 <= pt[1] <= y1:
                    return t.name
        return "-"

    def to_json(self) -> dict:
        return asdict(self)


# --------------------------------------------------------------------------- #


def _tag(text: str) -> list[str]:
    tags = []
    for rx, name in DIM_PATTERNS:
        if rx.search(text):
            tags.append(name)
    if "number" in tags and len(tags) > 1:
        tags.remove("number")
    return tags


def _extract_spans(page: pymupdf.Page) -> list[TextSpan]:
    spans: list[TextSpan] = []
    d = page.get_text("dict")
    for block in d.get("blocks", []):
        for line in block.get("lines", []):
            dx, dy = line.get("dir", (1.0, 0.0))
            angle = math.degrees(math.atan2(-dy, dx))
            for sp in line.get("spans", []):
                text = sp.get("text", "").strip()
                if not text:
                    continue
                x0, y0, x1, y1 = sp["bbox"]
                spans.append(
                    TextSpan(
                        text=text,
                        bbox_mm=(x0 * PT_TO_MM, y0 * PT_TO_MM, x1 * PT_TO_MM, y1 * PT_TO_MM),
                        size_pt=float(sp.get("size", 0.0)),
                        font=sp.get("font", ""),
                        angle_deg=angle,
                        tags=_tag(text),
                    )
                )
    return spans


def _circle_from_beziers(points: list[tuple[float, float]]) -> CircleHint | None:
    """``points`` are the on-curve end points of a closed run of cubic beziers."""
    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    w, h = max(xs) - min(xs), max(ys) - min(ys)
    if w <= 0 or h <= 0 or abs(w - h) > 0.03 * max(w, h):
        return None
    cx, cy = (max(xs) + min(xs)) / 2, (max(ys) + min(ys)) / 2
    r = (w + h) / 4
    if any(abs(math.hypot(x - cx, y - cy) - r) > 0.03 * r for x, y in points):
        return None
    return CircleHint(center_mm=(cx * PT_TO_MM, cy * PT_TO_MM), diameter_mm=2 * r * PT_TO_MM)


def _circles_in_path(items: list) -> list[CircleHint]:
    """Find closed runs of consecutive bezier segments (PDF circles are 4 cubic curves)."""
    found: list[CircleHint] = []
    run: list = []

    def close(a, b) -> bool:
        return abs(a.x - b.x) < 0.05 and abs(a.y - b.y) < 0.05

    def flush() -> None:
        if len(run) >= 4 and close(run[0][1], run[-1][4]):
            pts = []
            for it in run:  # segment start + curve midpoint B(0.5) (rejects polygons drawn with curves)
                p0, c1, c2, p3 = it[1], it[2], it[3], it[4]
                pts.append((p0.x, p0.y))
                pts.append(((p0.x + 3 * c1.x + 3 * c2.x + p3.x) / 8, (p0.y + 3 * c1.y + 3 * c2.y + p3.y) / 8))
            c = _circle_from_beziers(pts)
            if c:
                found.append(c)
        run.clear()

    for it in items:
        if it[0] != "c":
            flush()
            continue
        if run and not close(run[-1][4], it[1]):
            flush()
        run.append(it)
        if len(run) >= 4 and close(run[0][1], it[4]):
            flush()
    flush()
    return found


def _extract_vectors(page: pymupdf.Page) -> tuple[list[dict], dict[str, int], list[CircleHint], list[tuple]]:
    paths = page.get_drawings()
    stats = {"paths": len(paths), "lines": 0, "curves": 0, "rects": 0, "quads": 0, "dashed_paths": 0}
    dump: list[dict] = []
    circles: list[CircleHint] = []
    rects: list[tuple[float, float, float, float]] = []
    for p in paths:
        items_out = []
        for it in p.get("items", []):
            kind = it[0]
            if kind == "l":
                stats["lines"] += 1
                items_out.append(["l", [it[1].x, it[1].y], [it[2].x, it[2].y]])
            elif kind == "c":
                stats["curves"] += 1
                pts = [it[1], it[2], it[3], it[4]]
                items_out.append(["c"] + [[q.x, q.y] for q in pts])
            elif kind == "re":
                stats["rects"] += 1
                r = it[1]
                rects.append((r.x0, r.y0, r.x1, r.y1))
                items_out.append(["re", [r.x0, r.y0, r.x1, r.y1]])
            elif kind == "qu":
                stats["quads"] += 1
                q = it[1]
                pts = [q.ul, q.ur, q.lr, q.ll]
                xs, ys = [a.x for a in pts], [a.y for a in pts]
                rects.append((min(xs), min(ys), max(xs), max(ys)))
                items_out.append(["qu"] + [[a.x, a.y] for a in pts])
        if p.get("dashes") and p["dashes"] not in ("[] 0", "[] 0.0"):
            stats["dashed_paths"] += 1
        circles.extend(_circles_in_path(p.get("items", [])))
        dump.append(
            {
                "items": items_out,
                "width": p.get("width"),
                "color": p.get("color"),
                "dashes": p.get("dashes"),
                "closePath": p.get("closePath"),
            }
        )
    stats["circles"] = len(circles)
    return dump, stats, circles, rects


def _detect_title_block(rects: list[tuple], page_w: float, page_h: float) -> tuple[float, float, float, float] | None:
    """Largest rectangle hugging the bottom-right corner, 2-35% of the sheet area."""
    best = None
    best_area = 0.0
    page_area = page_w * page_h
    for x0, y0, x1, y1 in rects:
        w, h = x1 - x0, y1 - y0
        area = w * h
        if not (0.02 * page_area <= area <= 0.35 * page_area):
            continue
        if x1 < 0.85 * page_w or y1 < 0.85 * page_h:
            continue
        if x0 < 0.3 * page_w:
            continue
        if area > best_area:
            best, best_area = (x0, y0, x1, y1), area
    return best


def _fit_pixels(img: Image.Image, max_pixels: int) -> Image.Image:
    w, h = img.size
    if w * h <= max_pixels:
        return img
    k = math.sqrt(max_pixels / (w * h))
    return img.resize((max(1, int(w * k)), max(1, int(h * k))), Image.LANCZOS)


def _save_crop(sheet: Image.Image, name: str, rect_pt: tuple[float, float, float, float], scale: float,
               out_dir: Path, job_dir: Path, max_pixels: int) -> TileInfo:
    x0, y0, x1, y1 = rect_pt
    box = (int(x0 * scale), int(y0 * scale), int(math.ceil(x1 * scale)), int(math.ceil(y1 * scale)))
    crop = sheet.crop(box)
    full_path = out_dir / f"{name}.png"
    crop.save(full_path, optimize=True)
    llm = _fit_pixels(crop, max_pixels)
    llm_path = out_dir / f"{name}.llm.png"
    llm.save(llm_path, optimize=True)
    return TileInfo(
        name=name,
        path=str(full_path.relative_to(job_dir)).replace("\\", "/"),
        llm_path=str(llm_path.relative_to(job_dir)).replace("\\", "/"),
        rect_mm=(x0 * PT_TO_MM, y0 * PT_TO_MM, x1 * PT_TO_MM, y1 * PT_TO_MM),
        width_px=crop.width,
        height_px=crop.height,
    )


GRID_LABEL = re.compile(r"^(?:[A-Z]|\d{1,2})$")


def _tag_sheet_grid(spans: list[TextSpan], w_mm: float, h_mm: float, band: float = 16.0) -> None:
    """ISO 5457 grid reference labels (1, 2, … / A, B, …) in the sheet margin are not dimensions."""
    for s in spans:
        x0, y0, x1, y1 = s.bbox_mm
        cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
        if GRID_LABEL.match(s.text) and (cx < band or cy < band or cx > w_mm - band or cy > h_mm - band):
            s.tags = ["sheet_grid"]


def choose_page(doc: pymupdf.Document) -> int:
    """Pick the page with the most vector content (usually the part sheet)."""
    if doc.page_count == 1:
        return 0
    scores = []
    for i, page in enumerate(doc):
        try:
            scores.append((len(page.get_drawings()), -i))
        except Exception:  # noqa: BLE001
            scores.append((0, -i))
    return -max(scores)[1]


def parse_pdf(pdf_path: Path, job_dir: Path, dpi: int = 300, max_pixels: int = 1300 * 1300,
              page_index: int | None = None, grid: tuple[int, int] = (2, 2), overlap: float = 0.08) -> ParsedPdf:
    extraction_dir = job_dir / "extraction"
    tiles_dir = job_dir / "tiles"
    extraction_dir.mkdir(parents=True, exist_ok=True)
    tiles_dir.mkdir(parents=True, exist_ok=True)

    doc = pymupdf.open(pdf_path)
    try:
        if doc.page_count == 0:
            raise ValueError("PDF has no pages")
        idx = choose_page(doc) if page_index is None else page_index
        page = doc[idx]
        pw, ph = page.rect.width, page.rect.height

        spans = _extract_spans(page)
        _tag_sheet_grid(spans, pw * PT_TO_MM, ph * PT_TO_MM)
        vectors, stats, circles, rects = _extract_vectors(page)
        title_pt = _detect_title_block(rects, pw, ph)

        all_pages_text = [p.get_text("text") for p in doc]

        pix = page.get_pixmap(dpi=dpi, alpha=False)
        sheet = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
        scale = pix.width / pw
    finally:
        doc.close()

    tiles: list[TileInfo] = [_save_crop(sheet, "sheet", (0, 0, pw, ph), scale, tiles_dir, job_dir, max_pixels)]
    rows, cols = grid
    tw, th = pw / cols, ph / rows
    ox, oy = tw * overlap, th * overlap
    for r in range(rows):
        for c in range(cols):
            rect = (max(0, c * tw - ox), max(0, r * th - oy), min(pw, (c + 1) * tw + ox), min(ph, (r + 1) * th + oy))
            tiles.append(_save_crop(sheet, f"tile_r{r}c{c}", rect, scale, tiles_dir, job_dir, max_pixels))
    tb_rect = title_pt or (0.55 * pw, 0.75 * ph, pw, ph)
    tiles.append(_save_crop(sheet, "title_block", tb_rect, scale, tiles_dir, job_dir, max_pixels))

    parsed = ParsedPdf(
        page_index=idx,
        page_count=len(all_pages_text),
        page_size_mm=(pw * PT_TO_MM, ph * PT_TO_MM),
        has_text_layer=len(spans) > 0,
        spans=spans,
        tiles=tiles,
        circles=circles,
        vector_stats=stats,
        title_block_mm=tuple(v * PT_TO_MM for v in title_pt) if title_pt else None,  # type: ignore[arg-type]
    )

    (extraction_dir / "text_spans.json").write_text(
        json.dumps([asdict(s) for s in spans], indent=1, ensure_ascii=False), encoding="utf-8"
    )
    (extraction_dir / "vectors.json").write_text(json.dumps(vectors), encoding="utf-8")
    (extraction_dir / "all_pages_text.json").write_text(json.dumps(all_pages_text, ensure_ascii=False), encoding="utf-8")
    (extraction_dir / "summary.json").write_text(
        json.dumps({k: v for k, v in parsed.to_json().items() if k != "spans"}, indent=1), encoding="utf-8"
    )
    (extraction_dir / "llm_text_digest.txt").write_text(
        parsed.text_digest() + "\n\n" + parsed.vector_digest(), encoding="utf-8"
    )
    return parsed
