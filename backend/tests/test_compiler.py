"""Deterministic compiler: every feature type, datum and face mapping, STEP validity and ±0.01 mm dimensions."""

from __future__ import annotations

import math
from pathlib import Path

import pytest
from build123d import Vector, import_step

from app.config import Settings
from app.schemas.part_spec import PartSpec
from app.services.cad_compiler import build_stock, compile_part, export_step_file
from app.services.verifier import cam_face_colors, export_glb, render_orthographic
from tests.conftest import TOL, make_spec

L, W, T = 100.0, 60.0, 20.0
BLOCK = L * W * T


def built(spec: PartSpec):
    res = compile_part(spec, strict=True)
    assert all(s.ok for s in res.feature_status), res.feature_status
    assert res.shape.is_valid and len(res.shape.solids()) == 1
    return res.shape


def removed(spec: PartSpec, shape) -> float:
    return float(build_stock(spec).volume) - float(shape.volume)


def bbox(shape):
    bb = shape.bounding_box()
    return (bb.min.X, bb.min.Y, bb.min.Z), (bb.max.X, bb.max.Y, bb.max.Z)


def inside(shape, *p) -> bool:
    return shape.is_inside(Vector(*p))


# --------------------------------------------------------------------------- #
# Stock, datum, outer profile
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "datum,lo,hi",
    [
        ("bottom_left_top_face", (0, 0, -T), (L, W, 0)),
        ("bottom_left_bottom_face", (0, 0, 0), (L, W, T)),
        ("center_top_face", (-L / 2, -W / 2, -T), (L / 2, W / 2, 0)),
        ("center_bottom_face", (-L / 2, -W / 2, 0), (L / 2, W / 2, T)),
    ],
)
def test_datum_origin_places_stock(datum, lo, hi):
    shape = built(make_spec(datum_origin=datum))
    blo, bhi = bbox(shape)
    assert blo == pytest.approx(lo, abs=TOL)
    assert bhi == pytest.approx(hi, abs=TOL)
    assert shape.volume == pytest.approx(BLOCK, rel=1e-9)


def test_rounded_rectangle_profile():
    spec = make_spec(outer_profile={"type": "rounded_rectangle", "corner_radius": 5})
    shape = built(spec)
    assert shape.volume == pytest.approx((L * W - (4 - math.pi) * 25) * T, rel=1e-6)
    assert not inside(shape, 0.5, 0.5, -10)
    assert bbox(shape)[1] == pytest.approx((L, W, 0), abs=TOL)


def test_polygon_profile_with_corner_radius():
    pts = [[0, 0], [60, 0], [60, 20], [20, 40], [0, 40]]
    spec = make_spec(L=60, W=40, T=5, outer_profile={"type": "polygon", "points_2d": pts, "corner_radius": 0})
    shape = built(spec)
    assert shape.volume == pytest.approx((60 * 20 + (60 + 20) / 2 * 20) * 5, rel=1e-6)
    assert bbox(shape)[0] == pytest.approx((0, 0, -5), abs=TOL)


# --------------------------------------------------------------------------- #
# Holes
# --------------------------------------------------------------------------- #


def test_through_hole():
    spec = make_spec([{"id": "H", "type": "hole", "center_x": 50, "center_y": 30, "diameter": 10, "through_all": True}])
    shape = built(spec)
    assert removed(spec, shape) == pytest.approx(math.pi * 25 * T, rel=1e-6)
    assert not inside(shape, 50, 30, -10) and inside(shape, 50, 36, -10)


def test_depth_null_means_through():
    spec = make_spec([{"id": "H", "type": "hole", "center_x": 50, "center_y": 30, "diameter": 10, "depth": None}])
    assert spec.features[0].through_all is True


def test_blind_hole_flat_and_drill_point():
    flat = make_spec([{"id": "H", "type": "hole", "center_x": 20, "center_y": 20, "diameter": 8, "depth": 10,
                       "tip_angle_deg": None}])
    assert removed(flat, built(flat)) == pytest.approx(math.pi * 16 * 10, rel=1e-6)
    tip = make_spec([{"id": "H", "type": "hole", "center_x": 20, "center_y": 20, "diameter": 8, "depth": 10}])
    h = 4 / math.tan(math.radians(59))
    assert removed(tip, built(tip)) == pytest.approx(math.pi * 16 * (10 + h / 3), rel=1e-6)


def test_counterbore_and_countersink():
    cb = make_spec([{"id": "H", "type": "hole", "center_x": 20, "center_y": 20, "diameter": 6.6, "through_all": True,
                     "counterbore": {"diameter": 11, "depth": 6.8}}])
    r, R = 3.3, 5.5
    assert removed(cb, built(cb)) == pytest.approx(math.pi * r * r * T + math.pi * (R * R - r * r) * 6.8, rel=1e-6)

    cs = make_spec([{"id": "H", "type": "hole", "center_x": 20, "center_y": 20, "diameter": 4.5, "through_all": True,
                     "countersink": {"diameter": 9, "angle_deg": 90}}])
    r, R = 2.25, 4.5
    h = R - r
    expected = math.pi * r * r * (T - h) + math.pi * h / 3 * (R * R + R * r + r * r)
    assert removed(cs, built(cs)) == pytest.approx(expected, rel=1e-6)


def test_thread_is_modelled_at_tap_drill():
    spec = make_spec([{"id": "T", "type": "hole", "center_x": 20, "center_y": 20, "diameter": 8, "thread": "M8",
                       "through_all": True}])
    assert spec.features[0].drill_diameter() == pytest.approx(6.8)  # ISO tap drill for M8
    assert removed(spec, built(spec)) == pytest.approx(math.pi * 3.4**2 * T, rel=1e-6)
    fine = make_spec([{"id": "T", "type": "hole", "center_x": 20, "center_y": 20, "diameter": 8, "thread": "M8x1",
                       "through_all": True}])
    assert fine.features[0].drill_diameter() == pytest.approx(7.0)


def test_linear_and_circular_patterns_and_extra_positions():
    spec = make_spec([
        {"id": "L", "type": "hole", "center_x": 10, "center_y": 10, "diameter": 4, "through_all": True,
         "pattern": {"type": "linear", "count_x": 3, "count_y": 2, "pitch_x": 20, "pitch_y": 30}},
        {"id": "C", "type": "hole", "center_x": 80, "center_y": 30, "diameter": 3, "through_all": True,
         "pattern": {"type": "circular", "radius": 12, "count": 6, "start_angle_deg": 0}},
        {"id": "E", "type": "hole", "center_x": 5, "center_y": 55, "extra_positions": [[95, 55]], "diameter": 3,
         "through_all": True},
    ])
    assert spec.features[0].positions()[:4] == [(10, 10), (30, 10), (50, 10), (10, 40)]
    shape = built(spec)
    for x, y in [(50, 40), (92, 30), (80 + 12 * math.cos(math.pi / 3), 30 + 12 * math.sin(math.pi / 3)), (95, 55)]:
        assert not inside(shape, x, y, -10), (x, y)
    assert removed(spec, shape) == pytest.approx(math.pi * T * (6 * 4 + 6 * 2.25 + 2 * 2.25), rel=1e-6)


@pytest.mark.parametrize(
    "face,x,y,probe",
    [
        ("top", 30, 20, (30, 20, -1)),
        ("bottom", 30, 20, (30, 20, -T + 1)),
        ("front", 30, -8, (30, 1, -8)),
        ("back", 30, -8, (30, W - 1, -8)),
        ("left", 20, -8, (1, 20, -8)),
        ("right", 20, -8, (L - 1, 20, -8)),
    ],
)
def test_face_mapping(face, x, y, probe):
    """center_x/center_y are datum coordinates projected on the face (never mirrored); cuts go inward."""
    spec = make_spec([{"id": "H", "type": "hole", "face": face, "center_x": x, "center_y": y, "diameter": 6,
                       "depth": 5, "tip_angle_deg": None}])
    shape = built(spec)
    assert removed(spec, shape) == pytest.approx(math.pi * 9 * 5, rel=1e-6)
    assert not inside(shape, *probe), f"{face}: expected hole at {probe}"


@pytest.mark.parametrize("face", ["top", "bottom", "front", "back", "left", "right"])
def test_rotation_turns_x_towards_y_on_every_face(face):
    xy = {"top": (50, 30), "bottom": (50, 30), "front": (50, -10), "back": (50, -10), "left": (30, -10),
          "right": (30, -10)}
    x, y = xy[face]
    spec = make_spec([{"id": "P", "type": "pocket_rect", "face": face, "center_x": x, "center_y": y,
                       "length_x": 14, "width_y": 2, "rotation_deg": 45, "depth": 2}])
    shape = built(spec)
    at = {"top": lambda a, b: (a, b, -0.5), "bottom": lambda a, b: (a, b, -T + 0.5),
          "front": lambda a, b: (a, 0.5, b), "back": lambda a, b: (a, W - 0.5, b),
          "left": lambda a, b: (0.5, a, b), "right": lambda a, b: (L - 0.5, a, b)}[face]
    d = 5 / math.sqrt(2)
    assert not inside(shape, *at(x + d, y + d))
    assert inside(shape, *at(x + d, y - d))


# --------------------------------------------------------------------------- #
# Pockets, slots, bosses, step cuts, edge treatments
# --------------------------------------------------------------------------- #


def test_pockets_and_slot_volumes():
    spec = make_spec([
        {"id": "P1", "type": "pocket_rect", "center_x": 30, "center_y": 30, "length_x": 40, "width_y": 20,
         "corner_radius": 4, "depth": 5},
        {"id": "P2", "type": "pocket_circle", "center_x": 75, "center_y": 30, "diameter": 16, "depth": 4},
        {"id": "P3", "type": "pocket_polygon", "points_2d": [[60, 45], [90, 45], [75, 55]], "depth": 2},
        {"id": "S1", "type": "slot", "start_x": 10, "start_y": 10, "end_x": 50, "end_y": 10, "width": 6, "depth": 3},
    ])
    shape = built(spec)
    expected = ((40 * 20 - (4 - math.pi) * 16) * 5 + math.pi * 64 * 4 + 0.5 * 30 * 10 * 2
                + (40 * 6 + math.pi * 9) * 3)
    assert removed(spec, shape) == pytest.approx(expected, rel=1e-6)
    assert not inside(shape, 30, 30, -4.9) and inside(shape, 30, 30, -5.1)  # pocket floor at z = -5


def test_bosses_grow_the_envelope():
    spec = make_spec([
        {"id": "B1", "type": "boss_circle", "center_x": 30, "center_y": 30, "diameter": 20, "height": 8},
        {"id": "B2", "type": "boss_rect", "face": "front", "center_x": 70, "center_y": -10, "length_x": 20,
         "width_y": 10, "height": 5},
    ])
    shape = built(spec)
    lo, hi = bbox(shape)
    assert hi[2] == pytest.approx(8, abs=TOL) and lo[1] == pytest.approx(-5, abs=TOL)
    assert shape.volume == pytest.approx(BLOCK + math.pi * 100 * 8 + 20 * 10 * 5, rel=1e-6)


def test_step_cut_shoulder_is_open_at_the_edges():
    spec = make_spec([{"id": "SC", "type": "step_cut", "x_min": 0, "x_max": 20, "y_min": 0, "y_max": W, "depth": 5}])
    shape = built(spec)
    assert removed(spec, shape) == pytest.approx(20 * W * 5, rel=1e-6)
    assert not inside(shape, 0.2, 0.2, -0.5) and not inside(shape, 0.2, W - 0.2, -0.5)  # no sliver walls
    assert bbox(shape)[1] == pytest.approx((L, W, 0), abs=TOL)


def test_edge_treatments():
    spec = make_spec(edge_treatments=[
        {"id": "E1", "type": "fillet", "size": 3, "target": "vertical_outer_edges"},
        {"id": "E2", "type": "chamfer", "size": 1, "target": "top_outer_edges"},
    ])
    shape = built(spec)
    assert not inside(shape, 0.3, 0.3, -10)  # filleted vertical corner
    assert not inside(shape, 50, 0.2, -0.2)  # chamfered top edge
    assert inside(shape, 50, 0.2, -T + 0.2)  # bottom edge stays sharp
    assert bbox(shape)[1] == pytest.approx((L, W, 0), abs=TOL)


def test_failed_features_are_recorded_not_raised():
    spec = make_spec([
        {"id": "OUT", "type": "hole", "center_x": 500, "center_y": 500, "diameter": 5, "through_all": True},
        {"id": "OK", "type": "hole", "center_x": 10, "center_y": 10, "diameter": 5, "through_all": True},
        {"id": "X", "type": "complex_3d", "description": "freeform scoop"},
    ])
    assert spec.requires_complex_3d_fallback is True
    res = compile_part(spec)
    st = {s.feature_id: s for s in res.feature_status}
    assert not st["OUT"].ok and "removed no material" in st["OUT"].error
    assert st["OK"].ok
    assert st["X"].method == "skipped" and "no generated code" in st["X"].error
    assert res.shape.volume == pytest.approx(BLOCK - math.pi * 6.25 * T, rel=1e-6)


def test_complex_feature_skipped_when_fallback_flag_off():
    spec = make_spec([{"id": "X", "type": "complex_3d", "description": "freeform scoop", "code": "result = 1"}])
    spec = spec.model_copy(update={"requires_complex_3d_fallback": False})
    st = compile_part(spec, complex_runner=lambda f: None).feature_status[0]
    assert st.method == "skipped" and "requires_complex_3d_fallback" in st.error


# --------------------------------------------------------------------------- #
# §6 example + fixture -> STEP / GLB / ortho_check.png
# --------------------------------------------------------------------------- #


def test_section6_example_to_step_glb_and_ortho(section6_spec, tmp_path: Path):
    shape = built(section6_spec)
    lo, hi = bbox(shape)
    assert lo == pytest.approx((0, 0, -20), abs=TOL)
    assert hi == pytest.approx((200, 100, 0), abs=TOL)
    assert (hi[0] - lo[0], hi[1] - lo[1], hi[2] - lo[2]) == pytest.approx((200, 100, 20), abs=TOL)
    assert not inside(shape, 15, 15, -10)  # M8 through hole at (15, 15)
    assert not inside(shape, 100, 50, -7.9) and inside(shape, 100, 50, -8.1)  # pocket floor at z = -8
    assert not inside(shape, 0.3, 0.3, -10)  # R5 outer corner
    assert not inside(shape, 100, 0.3, -0.3)  # 1 mm top chamfer

    colors, counts = cam_face_colors(shape, section6_spec)
    assert counts["thread"] >= 1
    step = export_step_file(shape, tmp_path / "model.step", face_colors=colors)
    text = step.read_text(encoding="utf-8", errors="replace")
    assert "AUTOMOTIVE_DESIGN" in text  # AP214
    assert "MANIFOLD_SOLID_BREP" in text and "CYLINDRICAL_SURFACE" in text  # exact analytic B-Rep, no mesh
    assert "COLOUR_RGB" in text  # §8.6 CAM colour coding
    assert "Montageplaat_A12" in text

    back = import_step(str(step))
    assert len(back.solids()) == 1
    assert back.volume == pytest.approx(shape.volume, rel=1e-6)
    blo, bhi = bbox(back)
    assert blo == pytest.approx(lo, abs=TOL) and bhi == pytest.approx(hi, abs=TOL)

    glb = export_glb(shape, tmp_path / "preview.glb")
    assert glb.read_bytes()[:4] == b"glTF"
    renders = render_orthographic(shape, tmp_path, "Montageplaat_A12")
    assert renders["check"].name == "ortho_check.png" and renders["check"].stat().st_size > 10_000
    assert {p.name for p in renders.values()} >= {"ortho_top.png", "ortho_front.png", "ortho_right.png", "ortho_iso.png"}


def test_bracket_fixture_dimensions_and_colors(bracket_spec, tmp_path: Path):
    shape = built(bracket_spec)
    lo, hi = bbox(shape)
    assert lo == pytest.approx((0, 0, -12), abs=TOL) and hi == pytest.approx((120, 80, 0), abs=TOL)
    for x, y in [(10, 10), (110, 10), (10, 70), (110, 70), (60, 40), (60, 12)]:
        assert not inside(shape, x, y, -11), (x, y)
    assert not inside(shape, 30, 5, -6) and not inside(shape, 90, 5, -6)  # M6 in the front face
    _, counts = cam_face_colors(shape, bracket_spec)
    # v1.5 palette: 2 M6 bores, the Ø10 H7 bore, 4 counterbores (cylinder + floor) + 1 countersink cone, top datum
    assert counts == {"thread": 2, "tight_fit": 1, "seat": 9, "datum": 1}


def test_ap242_schema(tmp_path: Path):
    shape = built(make_spec())
    assert "AP242" in export_step_file(shape, tmp_path / "a.step", schema="AP242").read_text(errors="replace").upper()
    export_step_file(shape, tmp_path / "b.step", schema="AP214")  # restore default


def test_settings_defaults_match_architecture():
    s = Settings(_env_file=None)
    assert s.host == "127.0.0.1" and s.port == 8000
    assert s.sandbox_timeout_s == 15 and s.max_heal_retries == 3
    assert s.deepseek_vision_model == "deepseek-flash" and s.deepseek_code_model == "deepseek-v4-pro"
    with pytest.raises(ValueError):
        Settings(_env_file=None, host="0.0.0.0")
