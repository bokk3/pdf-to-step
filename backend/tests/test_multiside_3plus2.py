"""v2.0 stream A: PartSpec 2.0 frames, 6-face + 3+2 compiler, setup planner, cross-face DFM v3 (SPEC_V2_0 §2, §3.1)."""

from __future__ import annotations

import math
from collections import Counter

import numpy as np
import pytest
from build123d import Vector

from app.schemas.frames import frames, ortho_frames, rotation_spatial_xyz
from app.schemas.part_spec import PartSpec, to_v2_coordinates
from app.services.cad_compiler import compile_part
from app.services.setup_planner import plan_setups
from app.services.step_xcaf_exporter import classify_faces, export_colored_step, load_palette, read_step_colors
from app.services.verifier import build_report, cross_face_checks
from tests.conftest import load_fixture

BLUE, RED = (0, 102, 255), (230, 30, 30)
S30, C30 = math.sin(math.radians(30)), math.cos(math.radians(30))


def lagerblok(**over) -> PartSpec:
    d = {
        "schema_version": "2.0", "part_number": "A12-400", "revision": "B", "part_name": "Lagerblok_Schuin",
        "material": "AlMgSi1 (6082-T6)", "general_tolerance": "ISO 2768-mK",
        "stock": {"length_x": 160, "width_y": 90, "thickness_z": 40},
        "tilted_planes": [{"id": "plane_angle_30", "name": "30deg_Port_Face", "origin_x": 140, "origin_y": 45,
                           "origin_z": 0, "rot_b_deg": 30}],
        "features": [
            {"id": "feat_angle_face", "type": "angled_face_cut", "face": "plane:plane_angle_30"},
            {"id": "feat_back_pocket", "type": "pocket_rect", "face": "bottom", "center_x": 80, "center_y": 45,
             "length_x": 100, "width_y": 50, "depth": 10, "corner_radius": 6},
            {"id": "feat_bottom_h7", "type": "hole", "face": "bottom", "center_x": 15, "center_y": 20, "diameter": 8,
             "depth": 12, "tolerance": "H7"},
            {"id": "feat_side_hole", "type": "hole", "face": "right", "center_x": 20, "center_y": 20, "diameter": 10,
             "depth": 25, "thread": "M10"},
            {"id": "feat_tilted_bore", "type": "hole", "face": "plane:plane_angle_30", "center_x": 8, "center_y": 0,
             "diameter": 14, "depth": 18, "tolerance": "H7"},
        ],
    }
    d.update(over)
    return PartSpec.model_validate(d)


def inside(shape, x, y, z) -> bool:
    return shape.is_inside(Vector(x, y, z))


# --------------------------------------------------------------------------- frames


def test_v2_face_table_is_the_spec_table():
    spec = lagerblok()
    L, W, T = 160, 90, 40
    expect = {  # origin, normal, +u, +v  (SPEC_V2_0 §2, datum bottom_left_top_face)
        "top": ((0, 0, 0), (0, 0, 1), (1, 0, 0), (0, 1, 0)),
        "bottom": ((0, W, -T), (0, 0, -1), (1, 0, 0), (0, -1, 0)),
        "front": ((0, 0, -T), (0, -1, 0), (1, 0, 0), (0, 0, 1)),
        "back": ((L, W, -T), (0, 1, 0), (-1, 0, 0), (0, 0, 1)),
        "left": ((0, W, -T), (-1, 0, 0), (0, -1, 0), (0, 0, 1)),
        "right": ((L, 0, -T), (1, 0, 0), (0, 1, 0), (0, 0, 1)),
    }
    frs = frames(spec)
    for face, (o, n, u, v) in expect.items():
        fr = frs[face]
        assert np.allclose(fr.origin, o) and np.allclose(fr.n, n) and np.allclose(fr.u, u) and np.allclose(fr.v, v)
        assert fr.handedness == 1.0  # right-handed: u x v = outward normal
    tp = frs["plane:plane_angle_30"]
    assert np.allclose(tp.n, (S30, 0, C30)) and np.allclose(tp.u, (C30, 0, -S30)) and tp.handedness == 1.0
    assert np.allclose(rotation_spatial_xyz(90, 0, 0)[:, 2], (0, -1, 0))  # intrinsic X first
    assert np.allclose(rotation_spatial_xyz(0, 90, 90)[:, 0], (0, 1, 0))  # then Y, then the new Z


def test_v1_specs_load_unchanged_and_convert_to_v2():
    v10 = load_fixture("bracket_plate")
    assert v10.schema_version == "1.0" and v10.tilted_planes == [] and v10.is_legacy
    assert PartSpec.model_validate({"stock": {"length_x": 1, "width_y": 1, "thickness_z": 1}}).schema_version == "1.1"
    legacy = PartSpec.model_validate({
        "schema_version": "1.1", "stock": {"length_x": 100, "width_y": 60, "thickness_z": 20},
        "features": [
            {"id": "B", "type": "pocket_rect", "face": "bottom", "center_x": 30, "center_y": 15, "length_x": 20,
             "width_y": 10, "depth": 4, "rotation_deg": 20},
            {"id": "F", "type": "hole", "face": "front", "center_x": 20, "center_y": -6, "diameter": 5, "depth": 10,
             "pattern": {"type": "linear", "count_x": 3, "pitch_x": 25}},
            {"id": "K", "type": "hole", "face": "back", "center_x": 70, "center_y": -12, "diameter": 4, "depth": 8},
            {"id": "LE", "type": "slot", "face": "left", "start_x": 10, "start_y": -5, "end_x": 40, "end_y": -5,
             "width": 4, "depth": 3},
            {"id": "R", "type": "step_cut", "face": "right", "x_min": 0, "x_max": 15, "y_min": -20, "y_max": -14,
             "depth": 5},
            {"id": "P", "type": "pocket_polygon", "face": "bottom", "points_2d": [[60, 40], [90, 40], [75, 55]],
             "depth": 2},
        ]})
    v2 = to_v2_coordinates(legacy)
    assert v2.schema_version == "2.0" and not v2.is_legacy
    f = {x.id: x for x in v2.features}
    assert (f["B"].center_x, f["B"].center_y, f["B"].rotation_deg) == pytest.approx((30, 45, -20))
    assert (f["F"].center_x, f["F"].center_y) == pytest.approx((20, 14)) and f["F"].pattern.pitch_x == 25
    assert (f["K"].center_x, f["K"].center_y) == pytest.approx((30, 8))
    a, b = compile_part(legacy, strict=True).shape, compile_part(v2, strict=True).shape
    assert b.volume == pytest.approx(a.volume, rel=1e-9)
    assert a.cut(b).volume < 1e-6 and b.cut(a).volume < 1e-6  # identical geometry


def test_tilted_planes_need_v2_and_known_ids():
    with pytest.raises(ValueError, match="unknown tilted planes"):
        lagerblok(tilted_planes=[])
    with pytest.raises(ValueError, match="schema_version 2.0"):
        lagerblok(schema_version="1.1")
    with pytest.raises(ValueError, match="plane:<id>"):
        PartSpec.model_validate({"schema_version": "2.0", "stock": {"length_x": 10, "width_y": 10, "thickness_z": 5},
                                 "features": [{"id": "A", "type": "angled_face_cut", "face": "top"}]})


# --------------------------------------------------------------------------- compiler + colours


def test_lagerblok_compiles_on_all_sides_with_cam_colours(settings, tmp_path):
    spec = lagerblok()
    res = compile_part(spec, strict=True)
    shape = res.shape
    assert len(shape.solids()) == 1 and shape.is_valid
    wedge = 0.5 * 20 * (20 * math.tan(math.radians(30))) * 90
    assert not inside(shape, 159, 45, -1) and inside(shape, 139, 45, -1)  # angled face milled off
    assert inside(shape, 159, 45, -11.5) and not inside(shape, 159, 45, -10.5)  # plane at z = -19 tan 30 = -10.97
    assert not inside(shape, 80, 45, -35) and inside(shape, 80, 45, -29)  # bottom pocket 10 deep from Z-40
    assert not inside(shape, 15, 70, -35) and inside(shape, 15, 70, -24)  # bottom H7 (v 20 -> Y 70), 12 + 118° tip
    assert not inside(shape, 150, 20, -20) and inside(shape, 130, 20, -20)  # right M10, 25 deep (tap drill 8.5)
    o = np.array([140, 45, 0]) + 8 * np.array([C30, 0, -S30])
    n = np.array([S30, 0, C30])
    assert not inside(shape, *(o - 10 * n)) and inside(shape, *(o - 23 * n))  # tilted bore 18 deep along -n (+4.2 tip)
    assert shape.volume < 160 * 90 * 40 - wedge

    col = classify_faces(shape, spec, load_palette(settings))
    assert col.counts["thread"] == 1 and col.counts["tight_fit"] == 2  # side thread, bottom + tilted H7
    step = export_colored_step(shape, tmp_path / "lagerblok.step", col, name="Lagerblok_Schuin")
    solids, colors = read_step_colors(step)
    got = Counter(colors)
    assert solids == 1 and got[BLUE] == 1 and got[RED] == 2

    rep = build_report(spec, 1, shape, res.feature_status, settings)
    assert rep.passed, [i.message for i in rep.errors]
    assert rep.setup_plan and rep.setup_plan.min_setups_3axis == 4 and rep.setup_plan.min_setups_5axis_3plus2 == 2


# --------------------------------------------------------------------------- setup planner


def test_setup_plan_counts_and_order():
    plan = plan_setups(lagerblok(), deck_off_required=True)
    assert plan.min_setups_3axis == 4 and plan.min_setups_5axis_3plus2 == 2
    assert [s.faces for s in plan.setups_3axis] == [["top"], ["bottom"], ["plane:plane_angle_30"], ["right"]]
    assert plan.setups_3axis[0].deck_off is False and plan.setups_3axis[0].feature_ids == []
    five = plan.setups_5axis_3plus2
    assert set(five[0].faces) == {"top", "plane:plane_angle_30", "right"} and five[1].faces == ["bottom"]
    assert plan.softjaws_recommended  # the angled face cut destroys the rectangular gripping faces
    assert five[1].workholding == "vise_softjaws"

    plate = PartSpec.model_validate({"schema_version": "2.0", "stock": {"length_x": 100, "width_y": 50,
                                     "thickness_z": 10}, "features": [{"id": "H", "type": "hole", "center_x": 20,
                                                                       "center_y": 20, "diameter": 6}]})
    one = plan_setups(plate, deck_off_required=False)
    assert one.min_setups_3axis == 1 and one.min_setups_5axis_3plus2 == 1 and not one.softjaws_recommended
    two = plan_setups(plate, deck_off_required=True)
    assert two.min_setups_3axis == 2 and two.setups_3axis[1].deck_off and two.setups_3axis[1].workholding == "vise_hardjaws"

    angled = lagerblok(features=[{"id": "D", "type": "hole", "face": "plane:p", "center_x": 0, "center_y": 0,
                                  "diameter": 5, "depth": 5}],
                       tilted_planes=[{"id": "p", "origin_x": 80, "origin_y": 45, "origin_z": -40, "rot_a_deg": 160}])
    assert plan_setups(angled, False).min_setups_5axis_3plus2 == 2  # n_z = cos 160 < -0.15: needs the second side


# --------------------------------------------------------------------------- DFM v3


def _opposing(top_depth: float, bottom_depth: float, intentional: bool = False) -> PartSpec:
    return PartSpec.model_validate({
        "schema_version": "2.0", "stock": {"length_x": 100, "width_y": 60, "thickness_z": 40},
        "features": [
            {"id": "TOP", "type": "pocket_rect", "face": "top", "center_x": 50, "center_y": 30, "length_x": 40,
             "width_y": 20, "depth": top_depth, "corner_radius": 5},
            {"id": "BOT", "type": "pocket_rect", "face": "bottom", "center_x": 50, "center_y": 30, "length_x": 30,
             "width_y": 16, "depth": bottom_depth, "corner_radius": 5, "intentional_intersection": intentional},
        ]})


def test_cross_face_thin_web_and_breakthrough(settings):
    web = cross_face_checks(_opposing(20, 19.2), settings)
    assert [(i.code, i.severity.value) for i in web] == [("CROSS_FACE_THIN_WEB", "warning")]
    assert "0.80 mm" in web[0].message
    assert cross_face_checks(_opposing(20, 15), settings) == []  # 5 mm web: fine
    brk = cross_face_checks(_opposing(25, 20), settings)
    assert [(i.code, i.severity.value) for i in brk] == [("CROSS_FACE_BREAKTHROUGH", "error")]
    assert cross_face_checks(_opposing(25, 20, intentional=True), settings) == []

    spec = _opposing(25, 20)
    res = compile_part(spec)
    assert build_report(spec, 1, res.shape, res.feature_status, settings).passed is False


def test_cross_drilled_holes_are_info_only(settings):
    spec = PartSpec.model_validate({
        "schema_version": "2.0", "stock": {"length_x": 60, "width_y": 40, "thickness_z": 30},
        "features": [
            {"id": "V", "type": "hole", "face": "top", "center_x": 30, "center_y": 20, "diameter": 8, "depth": 25},
            {"id": "S", "type": "hole", "face": "front", "center_x": 30, "center_y": 15, "diameter": 6, "depth": 25},
        ]})
    assert [i.code for i in cross_face_checks(spec, settings)] == ["CROSS_FACE_INTERSECTION"]
