"""EN ISO drawing sheet: primitive model, ISO 5457 frame, ISO 7200 title block, ISO 5456-2 symbol, renderers.

Everything is laid out in *sheet millimetres* (origin top-left, y down) as a list of primitives
(strokes, filled polygons, texts). One model, three deterministic renderers:

* PDF  - PyMuPDF vector drawing with an embedded font: every string is real, selectable text
         (no Cairo / GTK native dependency, works the same on Windows and Linux).
* SVG  - layered <g> groups (frame, title block, views, dimensions, notes).
* DXF  - ezdxf, one layer per ISO line type, y axis flipped to DXF convention.

Standards: EN ISO 5457 (sheet, 20 mm binding margin, 0.7 mm frame, centring marks, grid zones),
EN ISO 7200 (180 mm title block, bottom right), EN ISO 128-2 (line types, line group 0.35 / 0.5),
EN ISO 3098 (lettering heights >= 2.5 mm), EN ISO 5456-2 (first-angle projection symbol).
"""

from __future__ import annotations

import math
import os
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

MM = 72 / 25.4  # PDF points per millimetre

SHEETS: dict[str, tuple[float, float]] = {"A4_LANDSCAPE": (297.0, 210.0), "A3_LANDSCAPE": (420.0, 297.0),
                                         "A4_PORTRAIT": (210.0, 297.0)}  # portrait: setup sheet (v1.5)
MARGIN_LEFT = 20.0  # binding edge
MARGIN = 10.0
FRAME_WIDTH = 0.7
TB_W, TB_H = 180.0, 40.0  # ISO 7200 title block (width fixed by the norm; rows 14 / 13 / 13 mm)
QR_CELL = 26.0  # shopfloor QR cell, bottom-left of the title block (v1.5)

TEXT_DIM = 3.5  # primary dimensions / callouts
TEXT_SMALL = 2.5  # minimum on A4 (ISO 3098)
TEXT_LABEL = 2.5  # title-block field names (spec §2: nothing below 2.5 mm)
TEXT_TITLE = 5.0


@dataclass(frozen=True)
class LineGroup:
    """ISO 128-2 line group: thick (visible) and thin (hidden, centre, dimension) widths in mm."""

    thick: float
    thin: float


LINE_GROUPS = {"A4_LANDSCAPE": LineGroup(0.35, 0.18), "A3_LANDSCAPE": LineGroup(0.5, 0.25),
               "A4_PORTRAIT": LineGroup(0.35, 0.18)}
RGB = tuple[float, float, float]
BLACK: RGB = (0.0, 0.0, 0.0)


def dash_pattern(style: str, d: float) -> list[float]:
    """ISO 128-2 element lengths as multiples of the line width d (mm)."""
    if style == "hidden":  # type 02 dashed
        return [12 * d, 3 * d]
    if style == "center":  # type 04 long-dash dotted
        return [24 * d, 3 * d, 0.5 * d, 3 * d]
    if style == "phantom":  # type 05 long-dash double-dotted
        return [24 * d, 3 * d, 0.5 * d, 3 * d, 0.5 * d, 3 * d]
    return []


# --------------------------------------------------------------------------- #
# Font (ISO 3098-style sans serif)
# --------------------------------------------------------------------------- #


class DrawingFont:
    """Wraps a TrueType font for metrics (layout) and embedding (PDF / SVG family name)."""

    def __init__(self, path: str | None = None):
        import pymupdf
        from fontTools.ttLib import TTFont

        self.path = path or os.environ.get("DRAWING_FONT") or default_font_path()
        self._font = pymupdf.Font(fontfile=self.path)
        tt = TTFont(self.path, lazy=True)
        self._cmap = tt.getBestCmap()
        upm = tt["head"].unitsPerEm
        os2 = tt["OS/2"] if "OS/2" in tt else None
        cap = getattr(os2, "sCapHeight", 0) if os2 else 0
        self.cap_ratio = (cap / upm) if cap else 0.72
        self.family = tt["name"].getDebugName(1) or "DejaVu Sans"

    def has(self, ch: str) -> bool:
        return ord(ch) in self._cmap

    def em(self, h: float) -> float:
        """Font size (em) for an ISO lettering height h (= capital height)."""
        return h / self.cap_ratio

    def width(self, text: str, h: float) -> float:
        return float(self._font.text_length(text, fontsize=self.em(h)))


ISO_SYMBOLS = "⌴⌵↧Ø°"  # counterbore, countersink, depth, diameter, degree (DRAWING_SPEC §2 callouts)
SYMBOL_FONT_CANDIDATES = (  # sans-serif fonts that also carry the ISO drawing symbols, embeddable in PDFs
    Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts" / "seguisym.ttf",  # Segoe UI Symbol (fsType 8)
)


def _covers(path: Path, chars: str) -> bool:
    try:
        from fontTools.ttLib import TTFont

        cmap = TTFont(str(path), lazy=True).getBestCmap() or {}
        return all(ord(c) in cmap for c in chars + "AZaz09.,")
    except Exception:  # noqa: BLE001
        return False


@lru_cache(maxsize=1)
def default_font_path() -> str:
    """A sans-serif lettering font: one that also has ⌴ ⌵ ↧ when installed (so callouts use the real ISO symbols in
    one selectable text run), otherwise DejaVu Sans from matplotlib (callouts then say CBORE / CSK)."""
    import matplotlib

    for cand in SYMBOL_FONT_CANDIDATES:
        if cand.is_file() and _covers(cand, ISO_SYMBOLS):
            return str(cand)
    return str(Path(matplotlib.get_data_path()) / "fonts" / "ttf" / "DejaVuSans.ttf")


@lru_cache(maxsize=4)
def get_font(path: str | None = None) -> DrawingFont:
    return DrawingFont(path)


# --------------------------------------------------------------------------- #
# Primitive model
# --------------------------------------------------------------------------- #

Pt = tuple[float, float]
Box = tuple[float, float, float, float]  # x0, y0, x1, y1


@dataclass
class Stroke:
    pts: list[Pt]
    width: float
    style: str = "solid"  # solid | hidden | center | phantom
    layer: str = "VISIBLE"
    closed: bool = False
    color: RGB = BLACK


@dataclass
class Fill:
    pts: list[Pt]
    layer: str = "DIMENSIONS"
    color: RGB = BLACK


@dataclass
class Text:
    text: str
    x: float  # baseline anchor (sheet mm)
    y: float
    size: float  # capital height (mm)
    anchor: str = "start"  # start | middle | end  (along the text direction)
    rotation: int = 0  # 0 or 90 (counter-clockwise: reads bottom-to-top)
    layer: str = "TEXT"
    color: RGB = BLACK


def _hex(c: RGB) -> str:
    return "#" + "".join(f"{int(round(v * 255)):02x}" for v in c)


def boxes_overlap(a: Box, b: Box, pad: float = 0.0) -> bool:
    return a[0] < b[2] + pad and b[0] < a[2] + pad and a[1] < b[3] + pad and b[1] < a[3] + pad


def box_inside(a: Box, outer: Box, eps: float = 1e-6) -> bool:
    return (a[0] >= outer[0] - eps and a[1] >= outer[1] - eps
            and a[2] <= outer[2] + eps and a[3] <= outer[3] + eps)


class Sheet:
    def __init__(self, size: str, font: DrawingFont):
        self.size = size
        self.w, self.h = SHEETS[size]
        self.font = font
        self.lines = LINE_GROUPS[size]
        self.strokes: list[Stroke] = []
        self.fills: list[Fill] = []
        self.texts: list[Text] = []
        self.inner: Box = (MARGIN_LEFT, MARGIN, self.w - MARGIN, self.h - MARGIN)
        self.title_block: Box = (self.inner[2] - TB_W, self.inner[3] - TB_H, self.inner[2], self.inner[3])

    # -- primitives -------------------------------------------------------------
    def polyline(self, pts: list[Pt], width: float | None = None, style: str = "solid", layer: str = "VISIBLE",
                 closed: bool = False, color: RGB = BLACK) -> None:
        if len(pts) >= 2:
            self.strokes.append(Stroke(list(pts), width if width is not None else self.lines.thin, style, layer, closed,
                                       color))

    def fill(self, pts: list[Pt], layer: str = "DIMENSIONS", color: RGB = BLACK) -> None:
        if len(pts) >= 3:
            self.fills.append(Fill(list(pts), layer, color))

    def line(self, a: Pt, b: Pt, **kw) -> None:
        self.polyline([a, b], **kw)

    def rect(self, box: Box, **kw) -> None:
        x0, y0, x1, y1 = box
        self.polyline([(x0, y0), (x1, y0), (x1, y1), (x0, y1)], closed=True, **kw)

    def circle(self, c: Pt, r: float, n: int = 48, **kw) -> None:
        self.polyline([(c[0] + r * math.cos(2 * math.pi * i / n), c[1] + r * math.sin(2 * math.pi * i / n))
                       for i in range(n)], closed=True, **kw)

    def arrow(self, tip: Pt, toward: Pt, length: float = 3.0, half_angle_deg: float = 10.0, layer: str = "DIMENSIONS") -> None:
        """Filled closed arrowhead (ISO 129-1) at `tip`, pointing away from `toward`."""
        dx, dy = tip[0] - toward[0], tip[1] - toward[1]
        n = math.hypot(dx, dy) or 1.0
        ux, uy = dx / n, dy / n
        bx, by = tip[0] - ux * length, tip[1] - uy * length
        w = length * math.tan(math.radians(half_angle_deg))
        self.fills.append(Fill([tip, (bx - uy * w, by + ux * w), (bx + uy * w, by - ux * w)], layer))

    def text(self, s: str, x: float, y: float, size: float = TEXT_DIM, anchor: str = "start", rotation: int = 0,
             layer: str = "TEXT", color: RGB = BLACK) -> Box:
        t = Text(s, x, y, size, anchor, rotation, layer, color)
        self.texts.append(t)
        return self.text_box(t)

    def text_box(self, t: Text) -> Box:
        w = self.font.width(t.text, t.size)
        asc, desc = t.size * 1.05, t.size * 0.3  # capital height (+accents) / descenders
        off = {"start": 0.0, "middle": -w / 2, "end": -w}[t.anchor]
        if t.rotation == 90:  # text runs upward; glyph tops point to -x
            return (t.x - asc, t.y - off - w, t.x + desc, t.y - off)
        return (t.x + off, t.y - asc, t.x + off + w, t.y + desc)

    def measure(self, s: str, size: float = TEXT_DIM) -> float:
        return self.font.width(s, size)

    # -- renderers ----------------------------------------------------------------
    def to_pdf(self, path: Path, metadata: dict[str, str] | None = None) -> Path:
        import pymupdf

        doc = pymupdf.open()
        page = doc.new_page(width=self.w * MM, height=self.h * MM)
        page.insert_font(fontname="iso", fontfile=self.font.path)
        groups: dict[tuple[float, str, bool, RGB], list[Stroke]] = {}
        for s in self.strokes:
            groups.setdefault((s.width, s.style, s.closed, s.color), []).append(s)
        for (width, style, closed, color), strokes in groups.items():
            shape = page.new_shape()
            for s in strokes:
                shape.draw_polyline([pymupdf.Point(x * MM, y * MM) for x, y in s.pts])
                if closed:
                    shape.draw_line(pymupdf.Point(s.pts[-1][0] * MM, s.pts[-1][1] * MM),
                                    pymupdf.Point(s.pts[0][0] * MM, s.pts[0][1] * MM))
            pattern = dash_pattern(style, width)
            dashes = "[" + " ".join(f"{v * MM:.3f}" for v in pattern) + "] 0" if pattern else None
            shape.finish(color=color, width=width * MM, dashes=dashes, closePath=False, lineCap=0, lineJoin=1)
            shape.commit()
        if self.fills:
            shape = page.new_shape()
            for f in self.fills:
                shape.draw_polyline([pymupdf.Point(x * MM, y * MM) for x, y in f.pts])
                shape.finish(color=f.color, fill=f.color, width=0.05 * MM, closePath=True)
            shape.commit()
        for t in self.texts:
            w = self.font.width(t.text, t.size)
            off = {"start": 0.0, "middle": -w / 2, "end": -w}[t.anchor]
            x, y = (t.x, t.y - off) if t.rotation == 90 else (t.x + off, t.y)
            page.insert_text(pymupdf.Point(x * MM, y * MM), t.text, fontname="iso", fontsize=self.font.em(t.size) * MM,
                             rotate=t.rotation, color=t.color)
        doc.set_metadata({"creator": "pdf-to-step drawing_engine", "producer": "PyMuPDF", **(metadata or {})})
        try:
            doc.subset_fonts()
        except Exception:  # noqa: BLE001 - subsetting is an optimisation only
            pass
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        doc.save(str(tmp), garbage=3, deflate=True)
        doc.close()
        _atomic_replace(tmp, path)  # readers (download, QR scan, tests) never see a half-written PDF
        return path

    def to_svg(self, path: Path, title: str = "") -> Path:
        from xml.sax.saxutils import escape

        layers: dict[str, list[str]] = {}

        def add(layer: str, el: str) -> None:
            layers.setdefault(layer, []).append(el)

        for s in self.strokes:
            pts = " ".join(f"{x:.3f},{y:.3f}" for x, y in s.pts)
            tag = "polygon" if s.closed else "polyline"
            pattern = dash_pattern(s.style, s.width)
            dash = f' stroke-dasharray="{" ".join(f"{v:.3f}" for v in pattern)}"' if pattern else ""
            add(s.layer, f'<{tag} points="{pts}" fill="none" stroke="{_hex(s.color)}" stroke-width="{s.width}"{dash}/>')
        for f in self.fills:
            pts = " ".join(f"{x:.3f},{y:.3f}" for x, y in f.pts)
            add(f.layer, f'<polygon points="{pts}" fill="{_hex(f.color)}"/>')
        fam = escape(f"{self.font.family}, osifont, ISOCPEUR, DejaVu Sans, sans-serif", {'"': "&quot;"})
        for t in self.texts:
            anchor = {"start": "start", "middle": "middle", "end": "end"}[t.anchor]
            rot = f' transform="rotate(-90 {t.x:.3f} {t.y:.3f})"' if t.rotation == 90 else ""
            add(t.layer, f'<text x="{t.x:.3f}" y="{t.y:.3f}" font-size="{self.font.em(t.size):.3f}" '
                         f'text-anchor="{anchor}"{rot}>{escape(t.text)}</text>')
        body = "\n".join(f'<g id="{layer}" inkscape:groupmode="layer" inkscape:label="{layer}">\n' + "\n".join(els) + "\n</g>"
                         for layer, els in layers.items())
        svg = (f'<?xml version="1.0" encoding="UTF-8"?>\n'
               f'<svg xmlns="http://www.w3.org/2000/svg" xmlns:inkscape="http://www.inkscape.org/namespaces/inkscape" '
               f'width="{self.w}mm" height="{self.h}mm" viewBox="0 0 {self.w} {self.h}" '
               f'font-family="{fam}" stroke-linecap="butt" stroke-linejoin="round">\n'
               f'<title>{escape(title)}</title>\n<rect width="{self.w}" height="{self.h}" fill="#fff"/>\n{body}\n</svg>\n')
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(svg, encoding="utf-8")
        return path

    def to_dxf(self, path: Path) -> Path:
        import ezdxf
        from ezdxf.enums import TextEntityAlignment

        doc = ezdxf.new("R2018", setup=True, units=4)  # 4 = millimetres
        linetype = {"solid": "CONTINUOUS", "hidden": "DASHED", "center": "CENTER", "phantom": "PHANTOM"}
        for name in {s.layer for s in self.strokes} | {f.layer for f in self.fills} | {t.layer for t in self.texts}:
            if name not in doc.layers:
                doc.layers.add(name)
        doc.styles.add("ISO", font=Path(self.font.path).name)
        msp = doc.modelspace()
        fy = lambda y: self.h - y  # noqa: E731 - DXF is y-up
        for s in self.strokes:
            msp.add_lwpolyline([(x, fy(y)) for x, y in s.pts], close=s.closed, dxfattribs={
                "layer": s.layer, "linetype": linetype.get(s.style, "CONTINUOUS"),
                "lineweight": int(round(s.width * 100)), "ltscale": max(s.width * 4, 0.2)})
        for f in self.fills:
            msp.add_solid([(x, fy(y)) for x, y in f.pts[:3]], dxfattribs={"layer": f.layer})
        align = {"start": TextEntityAlignment.LEFT, "middle": TextEntityAlignment.CENTER, "end": TextEntityAlignment.RIGHT}
        for t in self.texts:
            ent = msp.add_text(t.text, height=t.size, rotation=t.rotation, dxfattribs={"layer": t.layer, "style": "ISO"})
            ent.set_placement((t.x, fy(t.y)), align=align[t.anchor])
        path.parent.mkdir(parents=True, exist_ok=True)
        doc.saveas(str(path))
        return path


# --------------------------------------------------------------------------- #
# ISO 5457 frame, ISO 7200 title block, ISO 5456-2 symbol, notes
# --------------------------------------------------------------------------- #


def draw_frame(sheet: Sheet) -> None:
    """Trimmed-sheet frame, centring marks and grid reference zones (ISO 5457)."""
    x0, y0, x1, y1 = sheet.inner
    sheet.rect(sheet.inner, width=FRAME_WIDTH, layer="FRAME")
    cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
    for a, b in (((cx, 0.0), (cx, y0 + 5)), ((cx, sheet.h), (cx, y1 - 5)), ((0.0, cy), (x0 + 5, cy)),
                 ((sheet.w, cy), (x1 - 5, cy))):
        sheet.line(a, b, width=FRAME_WIDTH, layer="FRAME")
    # grid reference zones: ~50 mm fields, numbers along x, letters along y (top / left margin only)
    nx = max(2, round((x1 - x0) / 50))
    ny = max(2, round((y1 - y0) / 50))
    for i in range(1, nx):
        x = x0 + (x1 - x0) * i / nx
        sheet.line((x, y0), (x, y0 - 3), layer="FRAME")
    for i in range(nx):
        sheet.text(str(i + 1), x0 + (x1 - x0) * (i + 0.5) / nx, y0 - 2.5, TEXT_SMALL, "middle", layer="FRAME")
    for j in range(1, ny):
        y = y0 + (y1 - y0) * j / ny
        sheet.line((x0, y), (x0 - 3, y), layer="FRAME")
    for j in range(ny):
        sheet.text(chr(ord("A") + j), x0 - 6.5, y0 + (y1 - y0) * (j + 0.5) / ny + TEXT_SMALL / 2, TEXT_SMALL, "middle",
                   layer="FRAME")


def projection_symbol(sheet: Sheet, x: float, y: float, h: float = 3.5) -> None:
    """EN ISO 5456-2 first-angle symbol: truncated cone (large end left), end view circles on the right."""
    big, small = 2 * h, h  # diameters
    length = 1.8 * h
    cy = y
    left = x
    sheet.polyline([(left, cy - big / 2), (left + length, cy - small / 2), (left + length, cy + small / 2),
                    (left, cy + big / 2)], closed=True, width=sheet.lines.thick, layer="TITLEBLOCK")
    ccx = left + length + 1.2 * h + big / 2
    sheet.circle((ccx, cy), big / 2, width=sheet.lines.thick, layer="TITLEBLOCK")
    sheet.circle((ccx, cy), small / 2, width=sheet.lines.thick, layer="TITLEBLOCK")
    ov = 0.5 * h  # centre-line overshoot
    sheet.line((left - ov, cy), (ccx + big / 2 + ov, cy), style="center", layer="TITLEBLOCK")
    sheet.line((ccx, cy - big / 2 - ov), (ccx, cy + big / 2 + ov), style="center", layer="TITLEBLOCK")


TB_LABELS = {
    "title": ("TITLE", "BENAMING"), "doc_type": ("DOCUMENT TYPE", "DOCUMENTSOORT"), "drawing_no": ("DRAWING NO.", "TEKENINGNR."),
    "material": ("MATERIAL", "MATERIAAL"), "gen_tol": ("GENERAL TOL.", "ALG. TOLERANTIE"), "surface": ("SURFACE", "OPPERVLAK"),
    "scale": ("SCALE", "SCHAAL"), "projection": ("PROJECTION", "PROJECTIE"), "rev": ("REV.", "REV."),
    "drawn": ("DRAWN", "GETEKEND"), "checked": ("CHECKED", "GECONTROLEERD"), "date": ("DATE", "DATUM"),
    "units": ("UNITS", "EENHEID"), "mass": ("MASS", "MASSA"), "status": ("STATUS", "STATUS"), "sheet": ("SHEET", "BLAD"),
    "owner": ("OWNER", "EIGENAAR"),
}


def title_block(sheet: Sheet, values: dict[str, str], language: str = "EN") -> None:
    """EN ISO 7200 title block, 180 mm wide, bottom-right inside the frame."""
    x0, y0, x1, y1 = sheet.title_block
    t = sheet.lines.thick
    sheet.rect(sheet.title_block, width=t, layer="TITLEBLOCK")
    rows = [y0, y0 + 14, y0 + 27, y1]
    sheet.line((x0, rows[1]), (x1, rows[1]), width=sheet.lines.thin, layer="TITLEBLOCK")
    sheet.line((x0 + QR_CELL, rows[2]), (x1, rows[2]), width=sheet.lines.thin, layer="TITLEBLOCK")
    # v1.5 §2.4: shopfloor QR in the bottom-left cell (rows 2-3)
    from app.services.qr_helper import draw_qr

    draw_qr(sheet, values.get("qr_url"), x0 + 1.0, rows[1] + 1.0, QR_CELL - 2.0)
    layout = [
        [("title", 0, 115, TEXT_TITLE), ("drawing_no", 115, 180, 4.5)],
        [("material", 26, 62, TEXT_SMALL + 0.5), ("gen_tol", 62, 93, TEXT_SMALL + 0.5), ("surface", 93, 115, TEXT_SMALL),
         ("scale", 115, 131, TEXT_DIM), ("projection", 131, 164, 0), ("rev", 164, 180, TEXT_DIM)],
        # widths fitted to the measured 2.5 mm labels (GECONTROLEERD, EIGENAAR ...) and typical values
        [("drawn", 26, 45.5, TEXT_SMALL), ("checked", 45.5, 76.5, TEXT_SMALL), ("date", 76.5, 97, TEXT_SMALL),
         ("units", 97, 113.5, TEXT_SMALL + 0.5), ("mass", 113.5, 127.5, TEXT_SMALL), ("status", 127.5, 147.5, TEXT_DIM),
         ("sheet", 147.5, 160, TEXT_SMALL + 0.5), ("owner", 160, 180, TEXT_SMALL)],
    ]
    for r, cells in enumerate(layout):
        ry0, ry1 = rows[r], rows[r + 1]
        for key, cx0, cx1, vsize in cells:
            if cx0 > 0:
                sheet.line((x0 + cx0, ry0), (x0 + cx0, ry1), width=sheet.lines.thin, layer="TITLEBLOCK")
            en, nl = TB_LABELS[key]
            avail = (cx1 - cx0) - 2.0
            label_lines = [en]
            if language == "EN_NL" and en != nl:  # one line if it fits, else EN over NL
                both = f"{en} / {nl}"
                label_lines = [both] if sheet.measure(both, TEXT_LABEL) <= avail else [en, nl]
            for i, ln in enumerate(label_lines):
                sheet.text(ln, x0 + cx0 + 1.0, ry0 + 3.6 + i * (TEXT_LABEL + 0.9), TEXT_LABEL, layer="TITLEBLOCK")
            value = values.get(key, "")
            if key == "projection":
                projection_symbol(sheet, x0 + cx1 - 11.5, ry1 - 3.0, 1.8)  # right of the (two-line) label
                continue
            if key == "title" and values.get("doc_type"):  # document type on the label line, right-aligned
                sheet.text(values["doc_type"], x0 + cx1 - 1.5, ry0 + 3.6, TEXT_LABEL, "end", layer="TITLEBLOCK")
            if value:
                size = vsize if key != "title" else 4.2
                room = (ry1 - 2.0) - (ry0 + 3.6 + (len(label_lines) - 1) * (TEXT_LABEL + 0.9) + 1.0)
                size = max(TEXT_SMALL, min(size, room))
                while size > TEXT_SMALL and sheet.measure(value, size) > avail - 0.5:
                    size = max(TEXT_SMALL, size - 0.25)
                lines = [value]
                if sheet.measure(value, size) > avail - 0.5 and " " in value:  # never below 2.5 mm: wrap instead
                    words = value.split(" ")
                    cut = min(range(1, len(words)), key=lambda k: abs(sheet.measure(" ".join(words[:k]), size)
                                                                     - sheet.measure(" ".join(words[k:]), size)))
                    lines = [" ".join(words[:cut]), " ".join(words[cut:])]
                base = ry0 + 11.5 if key == "title" else ry1 - (2.4 if r == 0 else 2.0)  # clear of the next row's labels
                for i, ln in enumerate(lines):
                    sheet.text(ln, x0 + cx0 + 1.5, base - (len(lines) - 1 - i) * (size + 0.7), size, layer="TITLEBLOCK")


def notes_block(sheet: Sheet, lines: list[str], bottom: float, right: float, width: float = TB_W,
                size: float = TEXT_SMALL) -> Box:
    """Left-aligned numbered general notes whose last line sits just above `bottom`; returns its box."""
    wrapped: list[str] = []
    for line in lines:
        words, cur = line.split(" "), ""
        for wd in words:
            cand = f"{cur} {wd}".strip()
            if sheet.measure(cand, size) > width - 2 and cur:
                wrapped.append(cur)
                cur = "   " + wd
            else:
                cur = cand
        wrapped.append(cur)
    step = size * 1.7
    top = bottom - step * len(wrapped) - 1.5
    x = right - width
    for i, line in enumerate(wrapped):
        sheet.text(line, x + 1.0, top + step * (i + 1), size, layer="NOTES")
    return (x, top, right, bottom)


def _atomic_replace(tmp: Path, dest: Path, attempts: int = 20) -> None:
    """os.replace with a short retry: on Windows the target may be open for reading for a moment."""
    import time

    for i in range(attempts):
        try:
            os.replace(tmp, dest)
            return
        except PermissionError:
            if i == attempts - 1:
                raise
            time.sleep(0.05)