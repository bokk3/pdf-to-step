"""v2.0 stream C: exact B-Rep revision diff + semantic PartSpec diff (SPEC_V2_0_MULTISIDE_FIXTURING.md §3.3)."""

from __future__ import annotations

import math
import random
from pathlib import Path

import pytest
import trimesh

from app.schemas.part_spec import PartSpec, to_v2_coordinates
from app.services.cad_compiler import compile_part
from app.services.revision_diff import (
    norm_part_number,
    norm_revision,
    revision_key,
    run_revision_diff,
    semantic_diff,
)
from tests.test_api import upload, wait_terminal

TAN59 = math.tan(math.radians(59))


def rev(r: str, pocket_depth: float, hole: dict, features_order=None, part_number="A12-400") -> PartSpec:
    feats = [
        {"id": "P1", "type": "pocket_rect", "face": "top", "center_x": 30, "center_y": 30, "length_x": 40,
         "width_y": 20, "depth": pocket_depth, "corner_radius": 4},
        {"id": "H1", "type": "hole", "face": "top", "center_x": 75, "center_y": 30, "depth": 12, **hole},
        {"id": "S1", "type": "hole", "face": "right", "center_x": 30, "center_y": 10, "diameter": 5, "depth": 10},
    ]
    if features_order:
        feats = features_order(feats)
    return PartSpec.model_validate({"schema_version": "2.0", "part_number": part_number, "revision": r,
                                    "material": "EN AW-6082", "stock": {"length_x": 100, "width_y": 60,
                                                                        "thickness_z": 20}, "features": feats})


OLD = rev("A", 5, {"diameter": 6, "thread": "M6", "thread_depth": 10})
NEW = rev("B", 9, {"diameter": 8, "thread": "M8", "thread_depth": 10, "tolerance": "H7"})


def _drill(r: float, depth: float) -> float:
    return math.pi * r * r * depth + math.pi * r ** 3 / (3 * TAN59)  # cylinder + 118° tip


def test_deeper_pocket_and_m6_to_m8_h7(tmp_path):
    meta = {"diff_id": "t1", "job_old_id": "a", "job_new_id": "b", "version_old": 1, "version_new": 1,
            "part_number": "A12-400", "revision_old": "A", "revision_new": "B"}
    rep = run_revision_diff(OLD, NEW, compile_part(OLD, strict=True).shape, compile_part(NEW, strict=True).shape,
                            tmp_path, meta)
    pocket_extra = (40 * 20 - (4 - math.pi) * 4 ** 2) * 4  # rectangle minus the 4 R4 corner remnants
    hole_extra = _drill(6.8 / 2, 12) - _drill(5.0 / 2, 12)  # tap drills M8 6.8 / M6 5.0
    v = rep.volumes
    assert v.removed_mm3 == pytest.approx(pocket_extra + hole_extra, rel=0.01)  # spec: +-1 %
    assert v.removed_mm3 == pytest.approx(pocket_extra + hole_extra, rel=1e-4)  # actually exact
    assert v.added_mm3 == 0 and v.added_bodies == 0
    assert v.unchanged_mm3 == pytest.approx(v.new_mm3, rel=1e-6)
    assert v.old_mm3 - v.new_mm3 == pytest.approx(v.removed_mm3, rel=1e-6)

    assert rep.overall == "BREAKING_CAM"
    by_id = {i.id_new: i for i in rep.items if i.scope == "feature"}
    assert set(by_id) == {"P1", "H1"}  # S1 untouched
    assert by_id["P1"].category == "GEOMETRY_CHANGE" and [f.path for f in by_id["P1"].fields] == ["depth"]
    assert by_id["H1"].category == "BREAKING_CAM" and "H7" in by_id["H1"].message
    assert {f.path for f in by_id["H1"].fields} == {"diameter", "thread", "tolerance"}
    assert any(i.scope == "metadata" and i.category == "METADATA_ONLY" for i in rep.items)  # revision A -> B

    glb = trimesh.load(tmp_path / "revision_diff.glb")
    assert {"Removed", "Unchanged"} <= set(glb.geometry) and "Added" not in glb.geometry
    mats = {n: g.visual.material for n, g in glb.geometry.items()}
    assert [round(c) for c in mats["Removed"].baseColorFactor[:3]] == [230, 30, 30]
    assert mats["Unchanged"].baseColorFactor[3] == pytest.approx(0.30 * 255, abs=1)
    pdf = tmp_path / "revision_diff_report.pdf"
    assert pdf.read_bytes()[:5] == b"%PDF-" and (tmp_path / "report.json").exists()


def test_added_material_is_green_and_new_side_is_breaking(tmp_path):
    boss = rev("C", 5, {"diameter": 6, "thread": "M6", "thread_depth": 10})
    boss = PartSpec.model_validate({**boss.model_dump(mode="json"), "features": [
        *boss.model_dump(mode="json")["features"],
        {"id": "B1", "type": "boss_circle", "face": "top", "center_x": 80, "center_y": 50, "diameter": 10, "height": 4},
        {"id": "F1", "type": "hole", "face": "front", "center_x": 50, "center_y": 10, "diameter": 4, "depth": 6}]})
    meta = {"diff_id": "t2", "job_old_id": "a", "job_new_id": "c", "version_old": 1, "version_new": 1}
    rep = run_revision_diff(OLD, boss, compile_part(OLD).shape, compile_part(boss).shape, tmp_path, meta)
    assert rep.volumes.added_mm3 == pytest.approx(math.pi * 25 * 4, rel=1e-4)
    added = {i.id_new: i for i in rep.items if i.change == "added"}
    assert added["B1"].category == "GEOMETRY_CHANGE" and added["F1"].category == "BREAKING_CAM"  # new direction


def test_semantic_diff_is_order_and_id_independent():
    def shuffle_rename(feats):
        feats = [dict(f, id=f"x{n}") for n, f in enumerate(feats)]
        random.Random(4).shuffle(feats)
        return feats

    renamed = rev("B", 9, {"diameter": 8, "thread": "M8", "thread_depth": 10, "tolerance": "H7"},
                  features_order=shuffle_rename)
    a = [(i.category, i.change, i.feature_type) for i in semantic_diff(OLD, NEW) if i.scope == "feature"]
    b = [(i.category, i.change, i.feature_type) for i in semantic_diff(OLD, renamed) if i.scope == "feature"]
    assert sorted(a) == sorted(b) and len(a) == 2


def test_v1_and_v2_of_the_same_part_are_identical(tmp_path):
    legacy = PartSpec.model_validate({
        "schema_version": "1.1", "material": "S235", "stock": {"length_x": 80, "width_y": 40, "thickness_z": 15},
        "features": [{"id": "B", "type": "pocket_rect", "face": "bottom", "center_x": 30, "center_y": 10,
                      "length_x": 20, "width_y": 10, "depth": 4},
                     {"id": "F", "type": "hole", "face": "front", "center_x": 20, "center_y": -6, "diameter": 5,
                      "depth": 10}]})
    v2 = to_v2_coordinates(legacy)
    items = semantic_diff(legacy, v2)
    assert [i for i in items if i.scope != "metadata"] == []
    rep = run_revision_diff(legacy, v2, compile_part(legacy).shape, compile_part(v2).shape, tmp_path,
                            {"diff_id": "t3", "job_old_id": "a", "job_new_id": "b", "version_old": 1, "version_new": 2})
    assert rep.volumes.added_mm3 == 0 and rep.volumes.removed_mm3 == 0


def test_stock_and_material_changes_are_breaking():
    other = PartSpec.model_validate({**OLD.model_dump(mode="json"), "material": "1.4301",
                                     "stock": {"length_x": 110, "width_y": 60, "thickness_z": 20}})
    scopes = {i.scope: i.category for i in semantic_diff(OLD, other)}
    assert scopes["stock"] == "BREAKING_CAM" and scopes["material"] == "BREAKING_CAM"


@pytest.mark.parametrize("raw,norm", [(" a12 - 400 ", "A12-400"), ("brk 120a", "BRK120A")])
def test_part_number_normalisation(raw, norm):
    assert norm_part_number(raw) == norm


def test_revision_keys():
    assert norm_revision("Rev. b") == "B" and norm_revision("rev-3") == "3" and norm_revision("Index C") == "C"
    assert sorted(["C", "A", "AA", "B", "Z"], key=revision_key) == ["A", "B", "C", "Z", "AA"]
    assert sorted(["10", "2", "1"], key=revision_key) == ["1", "2", "10"]


def test_lineage_and_diff_api(client_factory, fixtures_dir: Path):
    with client_factory() as client:
        ids = []
        for r, depth in (("A", 6.0), ("B", 9.0)):
            job_id = upload(client, fixtures_dir)
            spec = wait_terminal(client, job_id)["part_spec"]
            spec.update(part_number="brk-120 a", revision=f"Rev {r}")
            next(f for f in spec["features"] if f["id"] == "P1")["depth"] = depth
            out = client.patch(f"/api/jobs/{job_id}/spec", json=spec)
            assert out.status_code == 200 and out.json()["status"] == "ready", out.text
            ids.append(job_id)
        lin = client.get("/api/parts/BRK-120A/revisions").json()
        assert [r["job_id"] for r in lin["revisions"]] == ids and [r["revision"] for r in lin["revisions"]] == ["A", "B"]
        assert client.get(f"/api/jobs/{ids[1]}").json()["revision"] == "B"

        r = client.post("/api/revisions/diff", json={"job_old_id": ids[0], "job_new_id": ids[1]})
        assert r.status_code == 200, r.text
        rep = r.json()
        assert rep["overall"] == "GEOMETRY_CHANGE" and rep["revision_old"] == "A" and rep["revision_new"] == "B"
        assert rep["volumes"]["removed_mm3"] > 0
        assert client.get(rep["urls"]["revision_diff.glb"]).content[:4] == b"glTF"
        assert client.get(rep["urls"]["revision_diff_report.pdf"]).content[:5] == b"%PDF-"
        again = client.post("/api/revisions/diff", json={"job_old_id": ids[0], "job_new_id": ids[1]}).json()
        assert again["diff_id"] == rep["diff_id"]  # cached
        assert client.get("/api/revisions/diff/../x/artifacts/report.json").status_code == 404
