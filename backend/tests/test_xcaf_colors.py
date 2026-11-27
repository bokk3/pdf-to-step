"""v1.5 stream B: AP214 CAM face colours via OCCT XCAF + GLB sync (SPEC_V1_5 §2.2)."""

from __future__ import annotations

import json
import re
from collections import Counter

import numpy as np
import pytest

from app.schemas.part_spec import PartSpec
from app.services.cad_compiler import compile_part
from app.services.step_xcaf_exporter import (
    classify_faces,
    export_colored_step,
    is_tight_fit,
    load_palette,
    read_step_colors,
)
from tests.conftest import load_fixture

BLUE, RED, PURPLE, GREEN, AMBER, GRAY = (0, 102, 255), (230, 30, 30), (148, 66, 224), (30, 180, 90), (245, 190, 0), (185, 190, 196)


def montageplaat_plus() -> PartSpec:
    """Montageplaat_A12 (M8 thread) + an H7 bore, a counterbored hole and a fine-finish pocket requirement."""
    d = load_fixture("section6_example").model_dump()
    d["features"] += [
        {"id": "H7", "type": "hole", "face": "top", "center_x": 185, "center_y": 85, "diameter": 10, "tolerance": "H7",
         "through_all": True},
        {"id": "CB", "type": "hole", "face": "top", "center_x": 185, "center_y": 15, "diameter": 6.6,
         "through_all": True, "counterbore": {"diameter": 11, "depth": 6.8}},
    ]
    d["drawing_metadata"]["surface_finishes"] = [{"feature_id": "feat_pocket_1", "ra_um": 0.8}]
    return PartSpec.model_validate(d)


def test_montageplaat_colours_survive_step_reimport(settings, tmp_path):
    spec = montageplaat_plus()
    shape = compile_part(spec).shape
    col = classify_faces(shape, spec, load_palette(settings))
    assert col.counts["thread"] == 1 and col.counts["tight_fit"] == 1
    assert col.counts["seat"] == 2  # counterbore cylinder + floor
    assert col.counts["datum"] == 1 and col.counts["fine_finish"] >= 5  # pocket floor + walls + corner fillets

    step = export_colored_step(shape, tmp_path / "model.step", col, name="Montageplaat_A12")
    text = step.read_text(errors="replace")
    assert "AUTOMOTIVE_DESIGN" in text and "MANIFOLD_SOLID_BREP" in text  # AP214, B-Rep (no mesh)
    assert "COLOUR_RGB('',0.,0.399999997426,1.)" in text  # exact royal blue (sRGB values in the file)
    solids, colors = read_step_colors(step)  # STEPCAFControl_Reader
    got = Counter(colors)
    assert solids == 1
    assert got[BLUE] == 1 and got[RED] == 1 and got[PURPLE] == 2 and got[GREEN] == 1 and got[AMBER] >= 5

    from build123d import import_step

    back = import_step(str(step))
    assert len(back.solids()) == 1 and back.is_valid and back.volume == pytest.approx(shape.volume, rel=1e-6)


def test_glb_carries_the_same_colours(settings, tmp_path):
    import trimesh

    from app.services.verifier import export_glb

    spec = montageplaat_plus()
    shape = compile_part(spec).shape
    col = classify_faces(shape, spec, load_palette(settings))
    glb = export_glb(shape, tmp_path / "preview.glb", coloring=col)
    mesh = trimesh.load(glb, force="mesh")
    rgb = {tuple(int(v) for v in c[:3]) for c in np.asarray(mesh.visual.vertex_colors)}
    assert {BLUE, RED, PURPLE, GREEN, AMBER, GRAY} <= rgb


@pytest.mark.parametrize("tol,tight", [("H7", True), ("H6", True), ("g6", True), ("H8", False), ("±0.02", True),
                                       ("+0.015/0", True), ("±0.05", False), (None, False)])
def test_tight_fit_rule(settings, tol, tight):
    assert is_tight_fit(tol, load_palette(settings)) is tight


def test_palette_is_configurable(settings, tmp_path):
    pal = load_palette(settings)
    cfg = settings.data_dir / "config" / "cam_colors.json"
    d = json.loads(cfg.read_text(encoding="utf-8"))
    d["colors"]["thread"]["rgb"] = [10, 20, 30]
    cfg.write_text(json.dumps(d), encoding="utf-8")
    spec = load_fixture("section6_example")
    shape = compile_part(spec).shape
    col = classify_faces(shape, spec, load_palette(settings))
    step = export_colored_step(shape, tmp_path / "m.step", col)
    assert (10, 20, 30) in read_step_colors(step)[1] and pal.colors["thread"].hex == "#0066FF"
    assert re.search(r"COLOUR_RGB", step.read_text(errors="replace"))
