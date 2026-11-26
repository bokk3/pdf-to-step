"""§8 CNC-DFM & validation rules."""

from __future__ import annotations

from app.schemas.part_spec import PartSpec, Severity
from app.services.cad_compiler import compile_part
from app.services.verifier import (
    build_report,
    callout_coverage,
    check_geometry,
    dfm_rules,
    drawing_callout_texts,
    is_tight,
    tolerance_band,
)
from tests.conftest import make_spec


def codes(issues, fid=None):
    return {i.code for i in issues if fid is None or i.feature_id == fid}


def pocket(**kw):
    p = {"id": "P", "type": "pocket_rect", "center_x": 50, "center_y": 30, "length_x": 40, "width_y": 20, "depth": 6}
    p.update(kw)
    return p


def report(spec, settings):
    res = compile_part(spec)
    return build_report(spec, 1, res.shape, res.feature_status, settings)


# 8.1 / 8.2 gate ------------------------------------------------------------------


def test_manifold_and_bbox_gate_pass(settings, section6_spec):
    rep = report(section6_spec, settings)
    assert rep.passed
    g = rep.geometry
    assert g.is_valid and g.solid_count == 1
    assert g.bbox_matches_spec and g.bbox_deviation_mm < 0.01
    assert rep.review_items == ["feat_hole_1"]  # confidence 0.78 < 0.85


def test_bbox_tolerance_is_0_01_mm(settings):
    shape = compile_part(make_spec(L=100.0)).shape
    geo, issues = check_geometry(shape, make_spec(L=100.005), tol_mm=settings.dfm_bbox_tolerance_mm)
    assert geo.bbox_matches_spec and not issues
    geo, issues = check_geometry(shape, make_spec(L=100.02), tol_mm=settings.dfm_bbox_tolerance_mm)
    assert not geo.bbox_matches_spec
    assert [i.code for i in issues] == ["GEOM_BBOX_MISMATCH"] and issues[0].severity == Severity.error


def test_bbox_growth_from_bosses_is_expected_but_unexpected_growth_fails(settings):
    boss = make_spec([{"id": "B", "type": "boss_circle", "center_x": 50, "center_y": 30, "diameter": 10, "height": 5}])
    shape = compile_part(boss).shape
    assert check_geometry(shape, boss)[0].bbox_matches_spec
    _, issues = check_geometry(shape, make_spec())
    assert any(i.code == "GEOM_BBOX_MISMATCH" and i.severity == Severity.error for i in issues)


def test_step_cut_may_reduce_envelope(settings):
    full_depth = make_spec([{"id": "SC", "type": "step_cut", "x_min": 0, "x_max": 10, "y_min": 0, "y_max": 60,
                             "depth": 20}])
    rep = report(full_depth, settings)
    assert rep.passed
    assert "GEOM_BBOX_REDUCED_BY_STEP" in codes(rep.issues)


def test_failed_feature_fails_gate(settings):
    spec = make_spec([{"id": "OUT", "type": "hole", "center_x": 500, "center_y": 5, "diameter": 5, "through_all": True}])
    rep = report(spec, settings)
    assert not rep.passed and "FEATURE_FAILED" in codes(rep.issues, "OUT")


# 8.3 corner radius -----------------------------------------------------------------


def test_sharp_internal_corner_warns(settings):
    issues = dfm_rules(make_spec([pocket(corner_radius=0)]), settings)
    w = [i for i in issues if i.code == "SHARP_INTERNAL_CORNER"]
    assert w and w[0].severity == Severity.warning and w[0].feature_id == "P"
    assert "dogbone" in w[0].suggestion


def test_corner_radius_below_1_5_mm_warns(settings):
    assert "SMALL_CORNER_RADIUS" in codes(dfm_rules(make_spec([pocket(corner_radius=1.4)]), settings))
    ok = codes(dfm_rules(make_spec([pocket(corner_radius=1.5)]), settings))
    assert not ok & {"SMALL_CORNER_RADIUS", "SHARP_INTERNAL_CORNER"}


def test_open_step_cut_has_no_internal_corners(settings):
    full_width = make_spec([{"id": "SC", "type": "step_cut", "x_min": 0, "x_max": 20, "y_min": 0, "y_max": 60,
                             "depth": 5}])
    assert "SHARP_INTERNAL_CORNER" not in codes(dfm_rules(full_width, settings))
    notch = make_spec([{"id": "SC", "type": "step_cut", "x_min": 0, "x_max": 20, "y_min": 0, "y_max": 20,
                        "depth": 5}])
    assert "SHARP_INTERNAL_CORNER" in codes(dfm_rules(notch, settings))


# 8.4 depth ratios -------------------------------------------------------------------


def test_hole_depth_over_5x_diameter_warns(settings):
    deep = make_spec([{"id": "H", "type": "hole", "center_x": 20, "center_y": 20, "diameter": 3, "depth": 16}])
    assert "DEEP_HOLE" in codes(dfm_rules(deep, settings))
    ok = make_spec([{"id": "H", "type": "hole", "center_x": 20, "center_y": 20, "diameter": 3, "depth": 15}])
    assert "DEEP_HOLE" not in codes(dfm_rules(ok, settings))
    thru = make_spec([{"id": "H", "type": "hole", "center_x": 20, "center_y": 20, "diameter": 3, "through_all": True}])
    assert "DEEP_HOLE" in codes(dfm_rules(thru, settings))  # 20 mm plate / Ø3


def test_pocket_depth_over_4x_corner_radius_warns(settings):
    assert "DEEP_POCKET" in codes(dfm_rules(make_spec([pocket(corner_radius=2, depth=8.5)]), settings))
    assert "DEEP_POCKET" not in codes(dfm_rules(make_spec([pocket(corner_radius=2, depth=8)]), settings))


# 8.5 thin walls ----------------------------------------------------------------------


def test_wall_to_stock_edge_under_1_5_mm_warns(settings):
    thin = make_spec([{"id": "H", "type": "hole", "center_x": 4.4, "center_y": 30, "diameter": 6, "through_all": True}])
    assert "THIN_WALL" in codes(dfm_rules(thin, settings), "H")  # 1.4 mm wall
    ok = make_spec([{"id": "H", "type": "hole", "center_x": 4.6, "center_y": 30, "diameter": 6, "through_all": True}])
    assert "THIN_WALL" not in codes(dfm_rules(ok, settings))


def test_thin_web_floor_and_breakout(settings):
    spec = make_spec([
        {"id": "A", "type": "hole", "center_x": 40, "center_y": 30, "diameter": 10, "through_all": True},
        {"id": "B", "type": "hole", "center_x": 51, "center_y": 30, "diameter": 10, "through_all": True},
        {"id": "C", "type": "hole", "center_x": 1, "center_y": 10, "diameter": 6, "through_all": True},
        pocket(id="P", center_x=75, center_y=30, length_x=20, width_y=20, corner_radius=3, depth=19),
    ])
    issues = dfm_rules(spec, settings)
    assert "THIN_WEB" in codes(issues, "A")
    assert "BREAKOUT" in codes(issues, "C")
    assert "THIN_FLOOR" in codes(issues, "P")


# tolerances / threads -------------------------------------------------------------------


def test_tolerance_parsing():
    assert is_tight("H7") and is_tight("+0.015/0") and is_tight("±0.02") and is_tight("H7 (+0.015/0)")
    assert is_tight("Ø10 g6")
    assert not is_tight("H11") and not is_tight("±0.1") and not is_tight(None)
    assert tolerance_band("+0.02/-0.01") == 0.03


def test_callout_coverage_against_the_text_layer(bracket_spec, fixtures_dir, tmp_path):
    from app.services.pdf_parser import parse_pdf

    parse_pdf(fixtures_dir / "bracket_plate.pdf", tmp_path, dpi=72)
    texts = drawing_callout_texts(tmp_path)
    assert "PART NO: BRK-120-A" not in texts  # title block excluded
    assert callout_coverage(bracket_spec, texts) == []  # complete spec: every Ø / M / R callout is modelled

    data = bracket_spec.model_dump(mode="json")
    data["features"] = [f for f in data["features"] if f["id"] != "T1"]  # drop the 2x M6 side holes
    h1 = next(f for f in data["features"] if f["id"] == "H1")
    h1["pattern"]["count_y"] = 1  # only 2 of the 4x Ø6.6 holes
    msgs = [i.message for i in callout_coverage(PartSpec.model_validate(data), texts)]
    assert any("2x M6" in m for m in msgs)
    assert any("4x Ø6.6" in m and "2 gemodelleerd" in m for m in msgs)


def test_fixture_reports(settings, bracket_spec):
    rep = report(bracket_spec, settings)
    assert rep.passed, rep.issues
    assert {"THREAD", "TIGHT_TOLERANCE"} <= codes(rep.issues)
    assert not [i for i in rep.issues if i.severity != Severity.info], rep.issues
    assert 260 < rep.stats["mass_g"] < 300
