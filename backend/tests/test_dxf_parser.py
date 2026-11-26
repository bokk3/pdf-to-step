"""Layered DXF -> PartSpec (DRAWING_SPEC.md §3 route B): deterministic, zero AI."""

from __future__ import annotations

import io
import math

import ezdxf
import pytest

from app.services.cad_compiler import compile_part
from app.services.dxf_parser import DxfError, classify, parse_dxf

B90 = math.tan(math.radians(90) / 4)  # bulge of a 90° arc


def rounded_rect(x0, y0, x1, y1, r):
    if r <= 0:
        return [(x0, y0, 0), (x1, y0, 0), (x1, y1, 0), (x0, y1, 0)]
    return [(x0 + r, y0, 0), (x1 - r, y0, B90), (x1, y0 + r, 0), (x1, y1 - r, B90),
            (x1 - r, y1, 0), (x0 + r, y1, B90), (x0, y1 - r, 0), (x0, y0 + r, B90)]


def obround(sx, sy, ex, w):
    h = w / 2
    return [(sx, sy - h, 0), (ex, sy - h, 1.0), (ex, sy + h, 0), (sx, sy + h, 1.0)]


def to_bytes(doc) -> bytes:
    s = io.StringIO()
    doc.write(s)
    return s.getvalue().encode("utf-8")


def bracket_dxf(ox=50.0, oy=30.0, contour_layer="CONTOUR_T20", units=4, k=1.0) -> bytes:
    """200 x 100 R5 plate drawn away from the origin (the datum must still be its bottom-left corner)."""
    doc = ezdxf.new("R2018")
    doc.header["$INSUNITS"] = units
    msp = doc.modelspace()
    P = lambda pts: [(ox + x * k, oy + y * k, b) for x, y, b in pts]  # noqa: E731
    msp.add_lwpolyline(P(rounded_rect(0, 0, 200, 100, 5)), format="xyb", close=True, dxfattribs={"layer": contour_layer})
    msp.add_lwpolyline(P(rounded_rect(40, 20, 160, 80, 6)), format="xyb", close=True, dxfattribs={"layer": "POCKET_D8"})
    for x, y in ((10, 10), (190, 10), (10, 90), (190, 90)):
        msp.add_circle((ox + x * k, oy + y * k), 3.3 * k, dxfattribs={"layer": "HOLE_THRU"})
    msp.add_circle((ox + 100 * k, oy + 92 * k), 4 * k, dxfattribs={"layer": "HOLE_D12"})
    for x in (20, 180):
        msp.add_circle((ox + x * k, oy + 50 * k), 2.5 * k, dxfattribs={"layer": "THREAD_M6_D12"})
    msp.add_lwpolyline(P(obround(60, 8, 140, 8)), format="xyb", close=True, dxfattribs={"layer": "SLOT_D6"})
    # a rectangular pocket drawn as 4 loose LINEs (exploded geometry)
    corners = [(170, 60), (185, 60), (185, 75), (170, 75)]
    for a, b in zip(corners, corners[1:] + corners[:1]):
        msp.add_line((ox + a[0] * k, oy + a[1] * k), (ox + b[0] * k, oy + b[1] * k), dxfattribs={"layer": "POCKET_D5"})
    msp.add_circle((ox + 25 * k, oy + 75 * k), 6 * k, dxfattribs={"layer": "POCKET_D3,5"})
    msp.add_text("NOTE", dxfattribs={"layer": "TEXT"})
    return to_bytes(doc)


def test_layers_to_partspec_with_datum_and_grouping():
    res = parse_dxf(bracket_dxf(), "plate")
    spec = res.spec
    assert spec.input_source == "dxf" and spec.part_name == "plate"
    assert (spec.stock.length_x, spec.stock.width_y, spec.stock.thickness_z) == (200, 100, 20)
    assert spec.outer_profile.type == "rounded_rectangle" and spec.outer_profile.corner_radius == pytest.approx(5)
    assert not spec.missing_dimensions
    by = {f.source_note: [g for g in spec.features if g.source_note == f.source_note] for f in spec.features}

    (thru,) = by["DXF layer HOLE_THRU"]
    assert thru.type == "hole" and thru.through_all and thru.diameter == pytest.approx(6.6)
    assert sorted(thru.positions()) == [(10, 10), (10, 90), (190, 10), (190, 90)]  # one 4x callout, datum shifted
    (blind,) = by["DXF layer HOLE_D12"]
    assert blind.depth == 12 and blind.diameter == pytest.approx(8) and (blind.center_x, blind.center_y) == (100, 92)
    (tap,) = by["DXF layer THREAD_M6_D12"]
    assert tap.thread == "M6" and tap.diameter == 6 and tap.thread_depth == 12 and tap.depth == pytest.approx(15)
    assert len(tap.positions()) == 2
    (pocket,) = by["DXF layer POCKET_D8"]
    assert pocket.type == "pocket_rect" and (pocket.length_x, pocket.width_y) == pytest.approx((120, 60))
    assert pocket.corner_radius == pytest.approx(6) and pocket.depth == 8
    assert (pocket.center_x, pocket.center_y) == pytest.approx((100, 50))
    (slot,) = by["DXF layer SLOT_D6"]
    assert slot.type == "slot" and slot.width == pytest.approx(8) and slot.depth == 6
    assert (slot.start_x, slot.start_y, slot.end_x, slot.end_y) == pytest.approx((60, 8, 140, 8))
    (lines,) = by["DXF layer POCKET_D5"]
    assert lines.type == "pocket_rect" and lines.corner_radius == 0 and (lines.length_x, lines.width_y) == pytest.approx((15, 15))
    (circ,) = by["DXF layer POCKET_D3,5"]
    assert circ.type == "pocket_circle" and circ.depth == 3.5 and circ.diameter == pytest.approx(12)
    assert any("TEXT" in w for w in res.warnings)

    result = compile_part(spec)  # the parsed spec builds a valid solid
    assert all(s.ok for s in result.feature_status)
    assert len(result.shape.solids()) == 1


def test_missing_thickness_and_depth_become_blocking_questions():
    doc = ezdxf.new("R2018")
    msp = doc.modelspace()
    msp.add_lwpolyline(rounded_rect(0, 0, 80, 50, 0), format="xyb", close=True, dxfattribs={"layer": "CONTOUR"})
    msp.add_lwpolyline(rounded_rect(20, 10, 60, 40, 4), format="xyb", close=True, dxfattribs={"layer": "POCKET"})
    spec = parse_dxf(to_bytes(doc)).spec
    paths = {m.field_path for m in spec.blocking_missing}
    assert paths == {"stock.thickness_z", "features.P1.depth"}


def test_inch_units_inner_cutout_and_errors():
    spec = parse_dxf(bracket_dxf(ox=0, oy=0, units=1, k=1 / 25.4)).spec  # same plate drawn in inches
    assert spec.stock.length_x == pytest.approx(200, abs=1e-3) and spec.stock.width_y == pytest.approx(100, abs=1e-3)

    doc = ezdxf.new("R2018")
    msp = doc.modelspace()
    msp.add_lwpolyline(rounded_rect(0, 0, 100, 60, 0), format="xyb", close=True, dxfattribs={"layer": "CONTOUR_T12"})
    msp.add_circle((50, 30), 10, dxfattribs={"layer": "CONTOUR_T12"})  # inner loop -> through cut-out
    spec = parse_dxf(to_bytes(doc)).spec
    (cut,) = spec.features
    assert cut.type == "pocket_circle" and cut.through_all and cut.diameter == pytest.approx(20)

    doc = ezdxf.new("R2018")
    doc.modelspace().add_circle((0, 0), 5, dxfattribs={"layer": "HOLE_THRU"})
    with pytest.raises(DxfError, match="CONTOUR"):
        parse_dxf(to_bytes(doc))
    with pytest.raises(DxfError):
        parse_dxf(b"not a dxf at all")


def test_classify_rotated_rect_and_polygon():
    a = math.radians(30)
    ca, sa = math.cos(a), math.sin(a)
    rect = [(x * ca - y * sa + 50, x * sa + y * ca + 40, 0) for x, y, _ in rounded_rect(-20, -10, 20, 10, 0)]
    s = classify(rect)
    assert s.kind == "rect" and s.rotation == pytest.approx(30) and (s.length, s.width) == pytest.approx((40, 20))
    assert (s.cx, s.cy) == pytest.approx((50, 40))
    tri = classify([(0, 0, 0), (30, 0, 0), (0, 20, 0)])
    assert tri.kind == "polygon" and len(tri.points) == 3
