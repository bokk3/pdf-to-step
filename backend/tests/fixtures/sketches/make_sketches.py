"""Five synthetic 'handwritten' sketch photos + ground-truth PartSpecs for the sketch OCR benchmark.

    cd backend && .venv/Scripts/python.exe tests/fixtures/sketches/make_sketches.py

Handwriting = the script fonts that ship with Windows (Ink Free, Segoe Print, Segoe Script) with jitter; not real
pencil. Each sketch writes every number the part needs (positions as x/y from the printed (0,0)), so a correct
reading builds the part without questions. Output: sketch_<n>.jpg + sketch_<n>.part_spec.json next to this file.
"""

from __future__ import annotations

import json
import math
import random
import sys
from pathlib import Path

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageFont

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from app.schemas.part_spec import PartSpec  # noqa: E402
from app.services.sketch_template import LAYOUT  # noqa: E402
from tests.sketch_photo import PX, template_image  # noqa: E402

OUT = Path(__file__).resolve().parent
FONTS = [r"C:\Windows\Fonts\Inkfree.ttf", r"C:\Windows\Fonts\segoepr.ttf", r"C:\Windows\Fonts\segoesc.ttf"]
PENCIL = (62, 62, 66)

PARTS = [
    {"name": "Klemplaat K1", "material": "AlMgSi1", "L": 120, "W": 80, "T": 15, "R": 0, "features": [
        {"id": "H1", "type": "hole", "face": "top", "center_x": 10, "center_y": 10, "diameter": 6.6, "through_all": True,
         "extra_positions": [[110, 10], [10, 70], [110, 70]]},
        {"id": "P1", "type": "pocket_rect", "face": "top", "center_x": 60, "center_y": 40, "length_x": 50, "width_y": 30,
         "corner_radius": 5, "depth": 6}]},
    {"name": "Lagersteun B2", "material": "S355", "L": 100, "W": 60, "T": 20, "R": 0, "features": [
        {"id": "T1", "type": "hole", "face": "top", "center_x": 25, "center_y": 20, "diameter": 8, "thread": "M8",
         "through_all": True, "extra_positions": [[75, 20]]},
        {"id": "S1", "type": "slot", "face": "top", "start_x": 30, "start_y": 45, "end_x": 70, "end_y": 45, "width": 8,
         "depth": 5}]},
    {"name": "Flens F3", "material": "AlMgSi1", "L": 150, "W": 100, "T": 12, "R": 0, "features": [
        {"id": "C1", "type": "pocket_circle", "face": "top", "center_x": 75, "center_y": 50, "diameter": 40, "depth": 8},
        {"id": "H1", "type": "hole", "face": "top", "center_x": 20, "center_y": 50, "diameter": 10, "tolerance": "H7",
         "through_all": True, "extra_positions": [[130, 50]]}]},
    {"name": "Afdekplaat D4", "material": "POM-C", "L": 80, "W": 80, "T": 10, "R": 8, "features": [
        {"id": "H1", "type": "hole", "face": "top", "center_x": 40, "center_y": 40, "diameter": 20, "through_all": True},
        {"id": "H2", "type": "hole", "face": "top", "center_x": 15, "center_y": 15, "diameter": 5.5, "through_all": True,
         "extra_positions": [[65, 15], [15, 65], [65, 65]]}]},
    {"name": "Geleiding G5", "material": "AlMgSi1", "L": 200, "W": 50, "T": 25, "R": 0, "features": [
        {"id": "P1", "type": "pocket_rect", "face": "top", "center_x": 100, "center_y": 25, "length_x": 160, "width_y": 30,
         "corner_radius": 6, "depth": 10},
        {"id": "H1", "type": "hole", "face": "top", "center_x": 10, "center_y": 25, "diameter": 8.5, "through_all": True,
         "extra_positions": [[190, 25]]}]},
]


def wobble(pts, rng, amp=0.6):
    return [(x + rng.uniform(-amp, amp), y + rng.uniform(-amp, amp)) for x, y in pts]


def draw_part(img: Image.Image, part: dict, rng: random.Random, font_path: str) -> None:
    d = ImageDraw.Draw(img)
    f = lambda mm: ImageFont.truetype(font_path, int(mm * PX))  # noqa: E731
    x0, y0, x1, y1 = LAYOUT["top"]
    # top view: not to scale, fitted into the box with the corner on the printed (0,0)
    L, W, T = part["L"], part["W"], part["T"]
    # not to scale (like a real sketch): long thin parts are drawn taller so the notes fit
    kx = min((x1 - x0 - 34) / L, 1.0) * rng.uniform(0.85, 1.0)
    ky = min((y1 - y0 - 30) / W, max(kx, 60.0 / W))
    ox, oy = x0, y1  # datum (mm on the sheet, y down)
    P = lambda x, y: ((ox + x * kx) * PX, (oy - y * ky) * PX)  # noqa: E731

    def line(pts, width=3):
        d.line(wobble([P(*p) for p in pts], rng, 1.2), fill=PENCIL, width=width, joint="curve")

    def circle(cx, cy, rs):
        """Round on the sheet: centre in part coordinates, radius `rs` in sheet mm."""
        sx, sy = P(cx, cy)
        pts = [(sx + rs * PX * math.cos(t), sy + rs * PX * math.sin(t)) for t in np.linspace(0, 2 * math.pi, 40)]
        d.line(wobble(pts, rng, 1.0), fill=PENCIL, width=3, joint="curve")

    def write(text, x, y, size=4.6, angle=None):
        fnt = f(size)
        w, h = d.textbbox((0, 0), text, font=fnt)[2:]
        tile = Image.new("L", (w + 20, h + 20), 0)
        ImageDraw.Draw(tile).text((10, 10), text, font=fnt, fill=255)
        tile = tile.rotate(angle if angle is not None else rng.uniform(-3, 3), expand=True, resample=Image.BICUBIC)
        img.paste(Image.new("RGB", tile.size, PENCIL), (int(x * PX), int(y * PX)), tile)

    R = part["R"]
    if R:
        c = []
        for (cx, cy, a0) in ((L - R, R, -90), (L - R, W - R, 0), (R, W - R, 90), (R, R, 180)):
            c += [(cx + R * math.cos(math.radians(a)), cy + R * math.sin(math.radians(a))) for a in np.linspace(a0, a0 + 90, 8)]
        line(c + [c[0]])
        write(f"4x R{R}", ox + (L - R) * kx + 2, oy - W * ky - 7)
    else:
        line([(0, 0), (L, 0), (L, W), (0, W), (0, 0)])
    # overall dimensions
    write(str(L), ox + L * kx / 2 - 4, oy + 1.5)
    line([(L + 12 / kx, 0), (L + 12 / kx, W)], 2)  # vertical dimension line right of the part (room on any sheet)
    write(str(W), ox + L * kx + 14, oy - W * ky / 2 - 3, angle=rng.uniform(-4, 4))
    for feat in part["features"]:
        pos = [(feat.get("center_x"), feat.get("center_y"))] + [tuple(p) for p in feat.get("extra_positions", [])]
        n = len(pos)
        pre = f"{n}x " if n > 1 else ""
        if feat["type"] == "hole":
            rs = max(feat["diameter"] / 2 * min(kx, ky), 1.8)
            for (hx, hy) in pos:
                circle(hx, hy, rs)
                txt = f"x{hx:g} y{hy:g}"
                tx = ox + hx * kx + rs + 0.8 if hx <= L / 2 else ox + hx * kx - rs - 1.2 - 2.0 * len(txt)
                write(txt, tx, oy - hy * ky - 2.5, 3.4)
            label = (f"{pre}{feat['thread']} door" if feat.get("thread") else
                     f"{pre}Ø{feat['diameter']:g} {feat.get('tolerance', '')} door".replace("  ", " "))
            hx, hy = max(pos, key=lambda p: (p[1], -p[0]))  # callout above the part, leader to the topmost hole
            ty = oy - W * ky - 9 - 7 * part["features"].index(feat)
            write(label, ox + hx * kx - 2, ty - 1.5, 4.0)
            d.line(wobble([((ox + hx * kx) * PX, (ty + 4.5) * PX), ((ox + hx * kx) * PX, (oy - hy * ky - rs) * PX)], rng, 0.8),
                   fill=PENCIL, width=2)
        elif feat["type"] == "pocket_rect":
            cx, cy, lx, wy, rr = feat["center_x"], feat["center_y"], feat["length_x"], feat["width_y"], feat["corner_radius"]
            line([(cx - lx / 2, cy - wy / 2), (cx + lx / 2, cy - wy / 2), (cx + lx / 2, cy + wy / 2),
                  (cx - lx / 2, cy + wy / 2), (cx - lx / 2, cy - wy / 2)])
            top = oy - (cy + wy / 2) * ky
            write(f"pocket {lx:g}x{wy:g} R{rr:g} diep {feat['depth']:g}", ox + (cx - lx / 2) * kx + 2, top + 0.5, 3.8)
            write(f"midden x{cx:g} y{cy:g}", ox + (cx - lx / 2) * kx + 2, top + 6.0, 3.6)
        elif feat["type"] == "pocket_circle":
            circle(feat["center_x"], feat["center_y"], feat["diameter"] / 2 * min(kx, ky))
            write(f"Ø{feat['diameter']:g} diep {feat['depth']:g}", ox + feat["center_x"] * kx - 9, oy - feat["center_y"] * ky - 3, 3.8)
            write(f"x{feat['center_x']:g} y{feat['center_y']:g}", ox + feat["center_x"] * kx - 7, oy - feat["center_y"] * ky + 2.5, 3.6)
        elif feat["type"] == "slot":
            sx, sy, ex, ey, w = feat["start_x"], feat["start_y"], feat["end_x"], feat["end_y"], feat["width"]
            h = w / 2
            pts = [(sx, sy - h), (ex, ey - h)] + [(ex + h * math.cos(t), ey + h * math.sin(t)) for t in np.linspace(-math.pi / 2, math.pi / 2, 10)] \
                + [(sx, sy + h)] + [(sx + h * math.cos(t), sy + h * math.sin(t)) for t in np.linspace(math.pi / 2, 1.5 * math.pi, 10)]
            line(pts + [pts[0]])
            write(f"sleuf {w:g} breed diep {feat['depth']:g}", ox + sx * kx, oy - (sy + h) * ky - 7, 3.8)
            write(f"centers x{sx:g}-x{ex:g} y{sy:g}", ox + sx * kx, oy - (sy - h) * ky + 1, 3.6)
    # front view: thickness
    fx0, fy0, fx1, fy1 = LAYOUT["front"]
    t_mm = max(6.0, min(T * ky, 30))
    fl = [(fx0 * PX, (fy1 - 8) * PX), ((fx0 + L * kx) * PX, (fy1 - 8) * PX),
          ((fx0 + L * kx) * PX, (fy1 - 8 - t_mm) * PX), (fx0 * PX, (fy1 - 8 - t_mm) * PX), (fx0 * PX, (fy1 - 8) * PX)]
    d.line(wobble(fl, rng, 1.2), fill=PENCIL, width=3)
    write(str(T), fx0 + L * kx + 3, fy1 - 8 - t_mm / 2 - 3)
    # title fields
    tx0, ty0, tx1, ty1 = LAYOUT["title"]
    rh = (ty1 - ty0) / 6
    write(part["name"], tx0 + 4, ty0 + rh * 0 + 4.2, 4.4)
    write(part["material"], tx0 + 4, ty0 + rh * 1 + 4.2, 4.4)
    write(str(T), tx0 + 4, ty0 + rh * 2 + 4.2, 4.4)


def photo_of(img_rgb: np.ndarray, seed: int) -> bytes:
    rng = np.random.default_rng(seed)
    img = img_rgb[:, :, ::-1].copy()
    h, w = img.shape[:2]
    j = lambda a: a + rng.uniform(-60, 60)  # noqa: E731
    dst = np.float32([[j(150), j(110)], [j(2060), j(200)], [j(2000), j(1580)], [j(100), j(1480)]])
    M = cv2.getPerspectiveTransform(np.float32([[0, 0], [w, 0], [w, h], [0, h]]), dst)
    out = cv2.warpPerspective(img, M, (2250, 1700), borderValue=(115, 100, 92))
    shade = np.linspace(rng.uniform(0.8, 0.9), rng.uniform(1.0, 1.08), out.shape[1])[None, :, None]
    out = np.clip(out * shade + rng.normal(0, 4, out.shape), 0, 255).astype(np.uint8)
    out = cv2.GaussianBlur(out, (3, 3), 0.9)
    ok, buf = cv2.imencode(".jpg", out, [cv2.IMWRITE_JPEG_QUALITY, 82])
    return buf.tobytes()


def main() -> None:
    base = template_image()[:, :, ::-1]  # RGB
    for i, part in enumerate(PARTS, start=1):
        rng = random.Random(100 + i)
        img = Image.fromarray(base.copy())
        draw_part(img, part, rng, FONTS[i % len(FONTS)])
        (OUT / f"sketch_{i}.jpg").write_bytes(photo_of(np.asarray(img), 100 + i))
        spec = {"schema_version": "1.1", "input_source": "paper_sketch", "part_name": part["name"],
                "material": part["material"], "general_tolerance": "ISO 2768-mK",
                "stock": {"length_x": part["L"], "width_y": part["W"], "thickness_z": part["T"]},
                "outer_profile": ({"type": "rounded_rectangle", "corner_radius": part["R"]} if part["R"]
                                  else {"type": "rectangle"}),
                "features": part["features"]}
        PartSpec.model_validate(spec)
        (OUT / f"sketch_{i}.part_spec.json").write_text(json.dumps(spec, indent=1, ensure_ascii=False), encoding="utf-8")
        print("wrote", f"sketch_{i}.jpg")


if __name__ == "__main__":
    main()
