"""Round-trip benchmark harness (DRAWING_SPEC.md §7 phase 2), offline.

The real A/B runs need DeepSeek (``python -m app.services.roundtrip_bench --llm deepseek``); here the harness itself
is checked: procedural fixtures, the order-independent feature diff, the geometry thresholds and a full oracle run
through pipeline.py with report generation.
"""

from __future__ import annotations

import asyncio
import json
import random

import pytest

from app.schemas.part_spec import PartSpec
from app.services import roundtrip_bench as rb
from tests.conftest import load_fixture


def _kinds(spec: PartSpec) -> set[str]:
    out = set()
    for f in spec.features:
        if f.type == "hole":
            out.add("thread" if f.thread else "cbore" if f.counterbore else "csk" if f.countersink
                    else "fit" if f.tolerance else "thru" if f.through_all else "blind")
        else:
            out.add(f.type)
    if spec.edge_treatments:
        out.add("chamfer")
    if spec.outer_profile.type == "rounded_rectangle":
        out.add("rounded")
    return out


def test_generated_specs_are_valid_and_varied(settings):
    specs = rb.generate_specs(12, 11, settings)
    assert len(specs) == 12
    assert len({s.part_name for s in specs}) == 12
    kinds = set().union(*(_kinds(s) for s in specs))
    assert {"pocket_rect", "pocket_circle", "slot", "thread", "cbore", "csk", "fit", "chamfer", "rounded"} <= kinds
    for s in specs:  # deterministic: the same seed gives the same parts
        assert rb.valid_spec(s, settings)
    assert [s.model_dump() for s in rb.generate_specs(2, 11, settings)] == [s.model_dump() for s in specs[:2]]


def test_feature_diff_is_order_and_representation_independent():
    gt = load_fixture("bracket_plate")
    d = gt.model_dump()
    random.Random(4).shuffle(d["features"])
    for f in d["features"]:
        if f.get("pattern"):  # pattern -> explicit extra positions in a different order
            spec_f = next(x for x in gt.features if x.id == f["id"])
            pts = spec_f.positions()[::-1]
            f["pattern"], f["center_x"], f["center_y"] = None, pts[0][0], pts[0][1]
            f["extra_positions"] = [list(p) for p in pts[1:]]
        if f.get("through_all") and f["type"] == "hole":  # through == blind to the full thickness
            f["through_all"], f["depth"] = False, gt.stock.thickness_z
            f["tip_angle_deg"] = None
        f["id"] = "X_" + f["id"]
    pred = PartSpec.model_validate(d)
    diff = rb.feature_diff(gt, pred)
    assert diff["precision"] == 1.0 and diff["recall"] == 1.0 and not diff["near_misses"], diff


def test_feature_diff_reports_misses_per_type():
    gt = load_fixture("bracket_plate")
    d = gt.model_dump()
    holes = [f for f in d["features"] if f["type"] == "hole"]
    holes[0]["center_x"] += 0.5  # moved beyond ±0.10 mm
    pocket = next(f for f in d["features"] if f["type"] == "pocket_rect")
    pocket["depth"] += 0.2  # right place, wrong depth
    d["features"] = [f for f in d["features"] if f["type"] != "slot"]  # missed feature
    d["features"].append({"id": "EXTRA", "type": "hole", "face": "top", "center_x": 60, "center_y": 20,
                          "diameter": 3, "through_all": True})  # hallucinated feature
    diff = rb.feature_diff(gt, PartSpec.model_validate(d))
    per = diff["per_type"]
    assert per["slot"]["recall"] == 0.0 and per["slot"]["pred"] == 0
    assert per["pocket_rect"]["tp"] == 0
    n_holes = per["hole"]["gt"]
    moved = 4 if holes[0].get("pattern") else 1
    assert per["hole"]["tp"] == n_holes - moved and per["hole"]["pred"] == n_holes + 1
    assert any(m["type"] == "pocket_rect" and "depth" in m["dims"] for m in diff["near_misses"])
    assert any(m["type"] == "hole" and 0.4 < m["center_error_mm"] < 0.6 for m in diff["near_misses"])
    assert 0 < diff["precision"] < 1 and 0 < diff["recall"] < 1


def test_geometry_thresholds():
    gt = {"valid": True, "solids": 1, "volume_mm3": 1000.0, "bbox_min": [0, 0, -10], "bbox_max": [100, 50, 0]}
    ok = dict(gt, volume_mm3=1004.0, bbox_max=[100.04, 50, 0])
    bad = dict(gt, volume_mm3=1006.0, bbox_max=[100.06, 50, 0], solids=2)
    g1, g2 = rb.geometry_diff(gt, ok), rb.geometry_diff(gt, bad)
    assert g1["solid_ok"] and g1["bbox_ok"] and g1["volume_ok"]
    assert not (g2["solid_ok"] or g2["bbox_ok"] or g2["volume_ok"])
    assert rb.geometry_diff(gt, None) == {"solid_ok": False, "bbox_ok": False, "volume_ok": False,
                                          "bbox_error_mm": None, "volume_error": None}


def test_oracle_roundtrip_through_pipeline_with_ab_report(settings, tmp_path):
    """Full flow: synthetic ISO PDF + fixture PDF -> pipeline.py -> diff -> report.md/json, two prompt variants."""
    variant = tmp_path / "strict.txt"
    variant.write_text("Read every dimension from the text layer first.", encoding="utf-8")
    args = rb.parse_args(["--n", "1", "--seed", "3", "--out", str(tmp_path / "bench"),
                          "--prompt-variant", f"strict={variant}"])
    root = asyncio.run(rb.run(args, settings))
    report = json.loads((root / "report.json").read_text(encoding="utf-8"))
    assert set(report["variants"]) == {"base", "strict"}
    for name in ("base", "strict"):
        for kind in ("synthetic", "fixture"):
            s = report["variants"][name][kind]
            assert s["cases"] >= 1 and s["ready"] == s["cases"], s
            assert s["solid_ok"] == s["bbox_ok"] == s["volume_ok"] == s["cases"]
            assert s["precision"] == 1.0 and s["recall"] == 1.0
    case = next(iter((root / "cases").iterdir()))
    for f in ("ground_truth.step", "ground_truth.part_spec.json", "synthetic_iso.pdf", "ground_truth.json"):
        assert (case / f).exists(), f
    md = (root / "report.md").read_text(encoding="utf-8")
    assert "## Synthetic EN ISO drawings" in md and "## Fixture / workshop PDFs" in md and "Oracle run" in md
    assert "| strict |" in md and "per feature type" in md


def test_sketch_photo_fixtures_get_their_own_section(settings, tmp_path):
    """Drop `X.jpg` + `X.part_spec.json` in the fixture dir: the photo runs through the sketch flow."""
    from tests.sketch_photo import photo

    fx = tmp_path / "fx"
    fx.mkdir()
    (fx / "plate_sketch.jpg").write_bytes(photo())
    (fx / "plate_sketch.part_spec.json").write_text(load_fixture("section6_example").model_dump_json(), encoding="utf-8")
    args = rb.parse_args(["--n", "0", "--fixtures", str(fx), "--out", str(tmp_path / "bench")])
    root = asyncio.run(rb.run(args, settings))
    report = json.loads((root / "report.json").read_text(encoding="utf-8"))
    s = report["variants"]["base"]["sketch"]
    assert s["cases"] == 1 and s["ready"] == 1 and s["recall"] == 1.0
    assert "## Hand-sketch photos (A4 template)" in (root / "report.md").read_text(encoding="utf-8")