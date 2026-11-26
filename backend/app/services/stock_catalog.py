"""Raw-stock advisor (SPEC_V1_5_CAM_SHOPFLOOR.md §2.3). Deterministic, no LLM.

Required saw blank = (X + 2·a_xy) × (Y + 2·a_xy) × (Z + a_top + a_clamp) around the finished envelope. The solver
picks the standard flat bar (width × thickness, sawn to length, either orientation) or plate thickness (sawn in X
and Y) with the smallest raw volume, then reports raw / finished mass and the chip-removal ratio
η = (V_raw − V_part) / V_raw.
"""

from __future__ import annotations

import math

from app.config import Settings
from app.schemas.tooling import Dims, StockAllowances, StockCatalog, StockRecommendation
from app.services.shop_config import load_config, save_config

STOCK_FILE = "stock_catalog.json"


def load_stock(s: Settings) -> StockCatalog:
    return load_config(s, STOCK_FILE, StockCatalog)


def save_stock(s: Settings, cat: StockCatalog) -> None:
    save_config(s, STOCK_FILE, cat)


def _up(v: float, step: float) -> float:
    return math.ceil(v / step - 1e-9) * step if step > 0 else v


def solve_stock(size_mm: tuple[float, float, float], part_volume_mm3: float | None, catalog: StockCatalog,
                group: str, allowances: StockAllowances | None = None) -> StockRecommendation:
    """`size_mm` = finished envelope (X, Y, Z) with Z the machining direction (top face = datum)."""
    a = allowances or catalog.default_allowances
    X, Y, Z = size_mm
    req = Dims(x=round(X + 2 * a.xy_per_side_mm, 3), y=round(Y + 2 * a.xy_per_side_mm, 3),
               z=round(Z + a.z_top_face_mm + a.z_clamp_margin_mm, 3))
    mat = catalog.materials.get(group)  # type: ignore[call-overload]
    step = a.saw_length_step_mm
    options: list[tuple[float, int, str, str, Dims, float]] = []  # (volume, pref, type, label, blank, saw length)
    if mat:
        for w, t in mat.standard_flat_bars_mm:
            if t + 1e-9 < req.z:
                continue
            if w + 1e-9 >= req.y:  # bar width along Y, sawn to length in X
                L = _up(req.x, step)
                options.append((w * t * L, 0, "flat_bar", f"platstaf {w:g} x {t:g}", Dims(x=L, y=w, z=t), L))
            if w + 1e-9 >= req.x:  # rotated: width along X, sawn in Y
                L = _up(req.y, step)
                options.append((w * t * L, 0, "flat_bar", f"platstaf {w:g} x {t:g}", Dims(x=w, y=L, z=t), L))
        plates = [t for t in mat.standard_plate_thicknesses_mm if t + 1e-9 >= req.z]
        if plates:
            t = min(plates)
            bx, by = _up(req.x, step), _up(req.y, step)
            options.append((bx * by * t, 1, "plate", f"plaat {t:g} mm", Dims(x=bx, y=by, z=t), max(bx, by)))
    if options:
        vol, _, ptype, label, blank, saw = min(options, key=lambda o: (round(o[0], 3), o[1]))
        note = ""
    else:
        blank = Dims(x=_up(req.x, step), y=_up(req.y, step), z=math.ceil(req.z))
        vol, ptype, saw = blank.x * blank.y * blank.z, "custom", max(blank.x, blank.y)
        label = f"maatblok {blank.x:g} x {blank.y:g} x {blank.z:g}"
        note = ("geen standaardprofiel groot genoeg in de voorraadcatalogus" if mat
                else f"materiaalgroep '{group}' staat niet in de voorraadcatalogus")
    rho = mat.density_kg_m3 if mat else None
    raw_kg = vol * 1e-9 * rho if rho else None
    part_kg = part_volume_mm3 * 1e-9 * rho if (rho and part_volume_mm3) else None
    chip = (vol - part_volume_mm3) / vol * 100 if part_volume_mm3 and vol > 0 else None
    return StockRecommendation(
        material_group=group if group in ("aluminum", "steel", "stainless", "plastic", "brass_copper") else "unknown",
        profile_type=ptype, profile_label=label, required_blank_mm=req, recommended_blank_mm=blank,
        saw_cut_length_mm=saw, density_kg_m3=rho,
        raw_mass_kg=round(raw_kg, 3) if raw_kg is not None else None,
        part_mass_kg=round(part_kg, 3) if part_kg is not None else None,
        chip_removal_pct=round(chip, 1) if chip is not None else None, allowances=a, note=note)
