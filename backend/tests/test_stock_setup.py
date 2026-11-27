"""v1.5: raw-stock advisor (SPEC_V1_5 §2.3) and the 1-page setup sheet."""

from __future__ import annotations

import pytest

from app.schemas.tooling import StockAllowances, StockCatalog, StockMaterial
from app.services.stock_catalog import load_stock, solve_stock


def test_spec_example_flat_bar(settings):
    """200 x 100 x 20 aluminium -> flat bar 120 x 25 sawn to 205 (spec §3.3 example)."""
    cat = load_stock(settings)
    part_v = 200 * 100 * 20 * 0.55
    rec = solve_stock((200, 100, 20), part_v, cat, "aluminum")
    assert rec.profile_type == "flat_bar" and rec.profile_label == "platstaf 120 x 25"
    assert (rec.required_blank_mm.x, rec.required_blank_mm.y, rec.required_blank_mm.z) == (204, 104, 25)
    assert (rec.recommended_blank_mm.x, rec.recommended_blank_mm.y, rec.recommended_blank_mm.z) == (205, 120, 25)
    assert rec.saw_cut_length_mm == 205
    v_raw = 205 * 120 * 25
    assert rec.raw_mass_kg == pytest.approx(v_raw * 1e-9 * 2700, abs=1e-3)
    assert rec.part_mass_kg == pytest.approx(part_v * 1e-9 * 2700, abs=1e-3)
    assert rec.chip_removal_pct == pytest.approx((v_raw - part_v) / v_raw * 100, abs=0.05)


def test_rotated_bar_plate_fallback_and_custom():
    cat = StockCatalog(materials={"steel": StockMaterial(density_kg_m3=7850, standard_flat_bars_mm=[(60, 20)],
                                                        standard_plate_thicknesses_mm=[30, 40])},
                       default_allowances=StockAllowances(saw_length_step_mm=0))
    rec = solve_stock((50, 100, 10), None, cat, "steel")  # 54 wide fits the 60 bar when rotated (sawn in Y)
    assert rec.profile_type == "flat_bar" and (rec.recommended_blank_mm.x, rec.recommended_blank_mm.y) == (60, 104)
    rec = solve_stock((150, 100, 20), None, cat, "steel")  # no bar wide enough -> plate 30
    assert rec.profile_type == "plate" and rec.recommended_blank_mm.z == 30 and rec.part_mass_kg is None
    rec = solve_stock((150, 100, 60), None, cat, "steel")  # nothing thick enough -> custom block
    assert rec.profile_type == "custom" and rec.note
    rec = solve_stock((50, 50, 10), 1000.0, cat, "unknown")
    assert rec.material_group == "unknown" and rec.raw_mass_kg is None and "voorraadcatalogus" in rec.note


def test_allowances_override():
    cat = StockCatalog(materials={"aluminum": StockMaterial(density_kg_m3=2700, standard_plate_thicknesses_mm=[50])})
    a = StockAllowances(xy_per_side_mm=5, z_top_face_mm=2, z_clamp_margin_mm=8, saw_length_step_mm=10)
    rec = solve_stock((100, 50, 30), None, cat, "aluminum", a)
    assert (rec.required_blank_mm.x, rec.required_blank_mm.y, rec.required_blank_mm.z) == (110, 60, 40)
    assert (rec.recommended_blank_mm.x, rec.recommended_blank_mm.y, rec.recommended_blank_mm.z) == (110, 60, 50)
