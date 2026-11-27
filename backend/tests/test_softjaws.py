"""v2.0 stream B: Op 2 softjaw generator (SPEC_V2_0_MULTISIDE_FIXTURING.md §3.2)."""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

import pytest
import trimesh

from app.schemas.part_spec import PartSpec
from app.schemas.softjaw import SoftjawRequest, ViseLibrary, ViseSpec
from app.services.cad_compiler import compile_part
from app.services.softjaw_generator import (
    EMERALD,
    generate_softjaws,
    interference_when_lifting,
    jaw_blanks,
    load_vises,
    pose_part,
)
from app.services.step_xcaf_exporter import read_step_colors, read_step_names
from tests.test_api import upload, wait_terminal

VISES = ViseLibrary.model_validate_json((Path(__file__).parents[1] / "app" / "defaults" / "vises.json").read_text())
V125 = VISES.get("vise_125_std")


def part(profile: dict | None = None, width: float = 34.0) -> PartSpec:
    """80 x width x 20 block with a groove in the front face INSIDE the Op 2 grip zone (an undercut once flipped)."""
    return PartSpec.model_validate({
        "schema_version": "2.0", "stock": {"length_x": 80, "width_y": width, "thickness_z": 20},
        **({"outer_profile": profile} if profile else {}),
        "features": [
            {"id": "GROOVE", "type": "pocket_rect", "face": "front", "center_x": 40, "center_y": 17, "length_x": 30,
             "width_y": 3, "depth": 4},
            {"id": "P", "type": "pocket_rect", "face": "top", "center_x": 40, "center_y": width / 2, "length_x": 40,
             "width_y": 12, "depth": 5, "corner_radius": 4},
        ]})


def jaws_from_step(step: Path) -> list:
    from build123d import import_step

    return list(import_step(str(step)).solids())


def test_sharp_part_gets_dogbones_and_an_undercut_free_cavity(tmp_path, settings):
    shape = compile_part(part(), strict=True).shape
    req = SoftjawRequest()
    rep = generate_softjaws(shape, req, V125, tmp_path, "job", 3, settings)
    assert rep.passed, [c.message for c in rep.checks if not c.passed]
    assert len(rep.dogbones) == 4  # four sharp 90° corners
    assert {round(abs(x), 3) for x, _ in rep.dogbones} == {round(40 + 0.25 * 3.2 / 2 ** 0.5, 3)}  # pushed outward
    assert rep.part_envelope_mm == pytest.approx((80, 34, 20))
    assert rep.recommended_tool and rep.recommended_tool["diameter_mm"] <= 6.4

    step = tmp_path / "softjaws.step"
    assert read_step_names(step) == ["Fixed_Jaw", "Movable_Jaw"]
    solids, colors = read_step_colors(step)
    assert solids == 2 and Counter(colors)[EMERALD] >= 10  # mating pocket faces highlighted for CAM
    jaws = jaws_from_step(step)
    assert len(jaws) == 2 and all(j.is_valid for j in jaws)
    assert interference_when_lifting(shape, req, jaws) < 1e-6  # slides out vertically

    # a raw boolean of the part would fill the groove with jaw material and lock the part in
    posed, _ = pose_part(shape, req)
    naive = [j.cut(posed) for j in jaw_blanks(V125, 6.0)]
    assert interference_when_lifting(shape, req, naive) > 1.0

    glb = trimesh.load(tmp_path / "softjaws_assembly.glb")
    assert set(glb.geometry) == {"Fixed_Jaw", "Movable_Jaw", "Workpiece"}
    report = json.loads((tmp_path / "softjaw_report.json").read_text(encoding="utf-8"))
    assert report["passed"] and report["version"] == 3


def test_rounded_corners_need_no_dogbones(tmp_path, settings):
    shape = compile_part(part({"type": "rounded_rectangle", "corner_radius": 5}), strict=True).shape
    rep = generate_softjaws(shape, SoftjawRequest(), V125, tmp_path, "job", 1, settings)
    assert rep.passed and rep.dogbones == []


def test_clearance_is_the_slip_fit(tmp_path, settings):
    shape = compile_part(part(), strict=True).shape
    rep = generate_softjaws(shape, SoftjawRequest(xy_clearance_mm=0.05), V125, tmp_path, "job", 1, settings)
    # deepest reach into a jaw = dogbone: centre 17 + 0.25 r / sqrt2 (bisector push) + r, minus half the gap
    assert rep.cavity_depth_y_mm == pytest.approx(17 + 0.25 * 3.2 / 2 ** 0.5 + 3.2 - 3.0, abs=1e-3)
    jaws = jaws_from_step(tmp_path / "softjaws.step")
    posed, _ = pose_part(shape, SoftjawRequest())
    from build123d import Vector

    for y in (17.04, -17.04):  # inside the 0.05 mm clearance band beside the part flanks: air
        assert not any(j.is_inside(Vector(0, y, -2)) for j in jaws)
    assert any(j.is_inside(Vector(0, 17.2, -2)) for j in jaws)  # beyond it: jaw material


def test_grip_depth_breaching_the_counterbore_margin_is_rejected(tmp_path, settings):
    shape = compile_part(part(), strict=True).shape
    rep = generate_softjaws(shape, SoftjawRequest(grip_depth_mm=11), V125, tmp_path, "job", 1, settings)
    bad = {c.code: c for c in rep.checks if not c.passed}
    assert not rep.passed and "CBORE_WALL" in bad and bad["CBORE_WALL"].value_mm == pytest.approx(2.0)
    assert not (tmp_path / "softjaws.step").exists()  # blocked: nothing to mill
    assert (tmp_path / "softjaw_report.json").exists()
    ok = generate_softjaws(shape, SoftjawRequest(grip_depth_mm=10), V125, tmp_path / "b", "job", 1, settings)
    assert ok.passed  # exactly 3.0 mm left


def test_too_wide_part_and_narrow_contact(tmp_path, settings):
    wide = compile_part(part(width=50), strict=True).shape
    rep = generate_softjaws(wide, SoftjawRequest(), V125, tmp_path, "job", 1, settings)
    wall = next(c for c in rep.checks if c.code == "OUTER_WALL")
    assert not rep.passed and not wall.passed and "bekopening >=" in wall.message
    assert generate_softjaws(wide, SoftjawRequest(jaw_gap_mm=30), V125, tmp_path / "g", "job", 1, settings).passed

    small = PartSpec.model_validate({"schema_version": "2.0", "stock": {"length_x": 20, "width_y": 20,
                                                                         "thickness_z": 15}})
    rep = generate_softjaws(compile_part(small).shape, SoftjawRequest(), V125, tmp_path / "s", "job", 1, settings)
    contact = next(c for c in rep.checks if c.code == "JAW_CONTACT")
    assert rep.passed and not contact.passed and not contact.blocking  # warning only


def test_vise_library_validation(settings):
    assert {v.id for v in load_vises(settings).vises} == {"vise_125_std", "vise_160_std"}
    with pytest.raises(ValueError, match="counterbore"):
        ViseSpec.model_validate({**V125.model_dump(), "bolt_cbore_dia_mm": 10})
    with pytest.raises(ValueError, match="duplicate"):
        ViseLibrary.model_validate({"vises": [V125.model_dump(), V125.model_dump()]})
    with pytest.raises(ValueError):
        SoftjawRequest(grip_depth_mm=30)


def test_softjaw_api(client_factory, fixtures_dir: Path):
    with client_factory() as client:
        assert client.get("/api/config/vises").json()["vises"][0]["id"] == "vise_125_std"
        assert client.put("/api/config/vises", json={"vises": [{"id": "x"}]}).status_code == 422
        job_id = upload(client, fixtures_dir)
        assert wait_terminal(client, job_id)["status"] == "ready"
        r = client.post(f"/api/jobs/{job_id}/softjaws", json={"jaw_gap_mm": 60, "vise_id": "vise_125_std"})
        assert r.status_code == 200, r.text
        rep = r.json()
        assert rep["passed"] and rep["version"] == 1
        step = client.get(rep["urls"]["softjaws.step"])
        assert step.status_code == 200 and step.content.startswith(b"ISO-10303-21")
        assert client.get(rep["urls"]["softjaws_assembly.glb"]).content[:4] == b"glTF"
        bad = client.post(f"/api/jobs/{job_id}/softjaws", json={"jaw_gap_mm": 60, "grip_depth_mm": 12})
        assert bad.status_code == 422 and bad.json()["detail"]["passed"] is False
        assert client.post(f"/api/jobs/{job_id}/softjaws", json={"vise_id": "nope"}).status_code == 422
        assert client.get(f"/api/jobs/{job_id}/artifacts/softjaws/evil.txt").status_code == 404
