"""Shopfloor QR codes (SPEC_V1_5_CAM_SHOPFLOOR.md §2.4): vector only, never a raster bitmap.

Target: ``{PUBLIC_TAILSCALE_URL}/shopfloor/{job_id}``. Without PUBLIC_TAILSCALE_URL no QR is drawn; the sheets show
a short warning text instead (a relative URL cannot be scanned from a phone).
"""

from __future__ import annotations

from app.config import Settings


def shopfloor_url(s: Settings, job_id: str) -> str | None:
    base = (s.public_tailscale_url or "").strip().rstrip("/")
    return f"{base}/shopfloor/{job_id}" if base else None


def qr_matrix(data: str) -> list[list[bool]]:
    """Module matrix incl. a 2-module quiet zone (error correction M)."""
    import qrcode
    from qrcode.constants import ERROR_CORRECT_M

    qr = qrcode.QRCode(error_correction=ERROR_CORRECT_M, border=2)
    qr.add_data(data)
    qr.make(fit=True)
    return [[bool(v) for v in row] for row in qr.get_matrix()]


def qr_svg(data: str) -> str:
    """Standalone SVG (one <path>) via qrcode.image.svg.SvgPathImage."""
    import qrcode
    from qrcode.image.svg import SvgPathImage

    img = qrcode.make(data, image_factory=SvgPathImage, border=2)
    out = img.to_string()
    return out.decode("utf-8") if isinstance(out, bytes) else out


def qr_rects(data: str, x: float, y: float, size: float) -> list[tuple[float, float, float, float]]:
    """Dark modules as rectangles (sheet mm, y down) in a size x size square; runs per row are merged."""
    m = qr_matrix(data)
    n = len(m)
    cell = size / n
    rects = []
    for r, row in enumerate(m):
        c = 0
        while c < n:
            if row[c]:
                start = c
                while c < n and row[c]:
                    c += 1
                rects.append((x + start * cell, y + r * cell, x + c * cell, y + (r + 1) * cell))
            else:
                c += 1
    return rects


def draw_qr(sheet, data: str | None, x: float, y: float, size: float, layer: str = "QR") -> None:
    """Vector QR on an iso_template.Sheet (filled rectangles); a warning text when there is no URL."""
    if not data:  # wrapped to the cell, lettering stays >= 2.5 mm
        lines, cur = [], ""
        for word in "geen QR: PUBLIC_ TAILSCALE_ URL niet ingesteld".split(" "):
            cand = f"{cur} {word}".strip() if not cur.endswith("_") else cur + word
            if cur and sheet.measure(cand, 2.5) > size - 1:
                lines.append(cur)
                cur = word
            else:
                cur = cand
        lines.append(cur)
        for i, ln in enumerate(lines):
            sheet.text(ln, x + 0.5, y + 4 + i * 3.6, 2.5, layer=layer)
        return
    for x0, y0, x1, y1 in qr_rects(data, x, y, size):
        sheet.fill([(x0, y0), (x1, y0), (x1, y1), (x0, y1)], layer=layer)
