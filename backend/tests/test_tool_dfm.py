"""v1.5 stream A: workshop tool library, Fusion 360 / CSV import, DFM v2 tool checks (SPEC_V1_5 §2.1)."""

from __future__ import annotations

import io
import json
import zipfile

import pytest

from app.schemas.part_spec import PartSpec
from app.schemas.tooling import ToolLibrary, ToolSpec
from app.services.tool_library import import_tools, load_tools, material_group, merge_tools, save_tools, tool_checks
from tests.conftest import load_fixture, make_spec

FUSION = {"data": [
    {"type": "flat end mill", "description": "VHM 3F Alu", "unit": "millimeters", "vendor": "Acme",
     "geometry": {"DC": 6, "LCF": 18, "OAL": 57, "RE": 0, "NOF": 3}, "post-process": {"number": 4}},
    {"type": "drill", "description": "Boor 6.8", "unit": "millimeters",
     "geometry": {"DC": 6.8, "LCF": 40, "OAL": 100}, "post-process": {"number": 21}},
    {"type": "tap right hand", "description": "Tap M8", "unit": "millimeters",
     "geometry": {"DC": 8, "LCF": 22, "OAL": 90, "TP": 1.25}, "post-process": {"number": 12}},
    {"type": "bull nose end mill", "description": "inch tool", "unit": "inches",
     "geometry": {"DC": 0.5, "LCF": 1.0, "OAL": 3.0, "RE": 0.03125, "NOF": 4}, "post-process": {"number": 30}},
    {"type": "holder", "description": "BT40 holder"},
    {"type": "drill", "description": "broken entry", "geometry": {}},
]}


def test_default_library_and_backup_on_save(settings):
    lib = load_tools(settings)
    assert len(lib.tools) >= 30 and (settings.data_dir / "config" / "tools.json").exists()
    lib.tools[0] = lib.tools[0].model_copy(update={"in_magazine": False})
    save_tools(settings, lib)
    assert list((settings.data_dir / "config" / "backups").glob("tools.*.json"))
    assert load_tools(settings).tools[0].in_magazine is False


def test_fusion_json_zip_and_csv_import():
    res = import_tools(json.dumps(FUSION).encode(), "lib.json")
    by = {t.id: t for t in res.tools}
    assert set(by) == {"T04", "T21", "T12", "T30"} and res.source == "fusion360"
    assert by["T04"].tool_type == "flat_endmill" and by["T04"].flute_length_mm == 18 and by["T04"].flutes == 3
    assert by["T12"].tool_type == "tap" and by["T12"].thread_spec == "M8"
    assert by["T30"].diameter_mm == pytest.approx(12.7) and by["T30"].corner_radius_mm == pytest.approx(0.79375, abs=1e-3)
    assert any("broken" in s for s in res.skipped)  # no diameter

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("tools.json", json.dumps(FUSION))
    assert len(import_tools(buf.getvalue(), "lib.tools").tools) == 4  # Fusion .tools = zip

    csv = ("Nr;Omschrijving;Soort;Diameter;Snijlengte;Lengte;Materiaal\n"
           "7;Vingerfrees Ø10;vingerfrees;10;22;72;aluminium;POM\n"
           "8;Ruimer 8 H7;ruimer;8,0;25;117;staal\n")
    tools = import_tools(csv.encode("utf-8"), "lib.csv").tools
    assert [(t.id, t.tool_type, t.diameter_mm) for t in tools] == [("T07", "flat_endmill", 10), ("T08", "reamer", 8)]
    assert tools[0].materials == ["aluminum", "plastic"] and tools[1].materials == ["steel"]


def test_merge_replaces_same_id_and_numbers_new_tools():
    lib = ToolLibrary(tools=[ToolSpec(id="T01", tool_type="drill", diameter_mm=5, flute_length_mm=20)])
    new = [ToolSpec(id="T01", tool_type="drill", diameter_mm=5.5, flute_length_mm=20),
           ToolSpec(id="", tool_type="tap", diameter_mm=6, flute_length_mm=15, thread_spec="M6")]
    merged, added, updated = merge_tools(lib, new)
    assert (added, updated) == (1, 1) and [t.id for t in merged.tools] == ["T01", "T02"]
    assert merged.tools[0].diameter_mm == 5.5


@pytest.mark.parametrize("text,group", [
    ("AlMgSi1 (6082-T6)", "aluminum"), ("Staal S355", "steel"), ("RVS 304", "stainless"), ("POM-C", "plastic"),
    ("EN AW-7075 T651", "aluminum"), ("1.4404", "stainless"), ("CuZn39Pb3", "brass_copper"), ("42CrMo4", "steel"),
    ("", "unknown"), ("Titanium Gr5", "unknown")])
def test_material_groups(text, group):
    assert material_group(text) == group


def test_deep_sharp_pocket_names_a_real_tool(settings):
    """Spec §7 gate: a 35 mm deep pocket with R2 corners gets a warning with a concrete tool from tools.json."""
    spec = make_spec([{"id": "P1", "type": "pocket_rect", "center_x": 50, "center_y": 30, "length_x": 40,
                       "width_y": 30, "corner_radius": 2, "depth": 35}], L=100, W=60, T=40)
    spec = spec.model_copy(update={"material": "AlMgSi1"})
    checks, assign, issues = tool_checks(spec, load_tools(settings))
    (pocket,) = [c for c in checks if c.feature_id == "P1"]
    assert pocket.status == "warning"
    assert "T01" in pocket.suggested_fix and "corner_radius naar 4.0 mm" in pocket.suggested_fix
    assert any(i.code == "TOOL_POCKET" and i.feature_id == "P1" and i.severity == "warning" for i in issues)
    assert not [a for a in assign if a.feature_id == "P1"]


def test_holes_threads_reamers_and_slots(settings):
    lib = load_tools(settings)
    checks, assign, issues = tool_checks(load_fixture("bracket_plate"), lib)
    assert all(c.status == "ok" for c in checks), [c for c in checks if c.status != "ok"]
    ops = {(a.feature_id, a.operation): a.tool_id for a in assign}
    assert ops[("H2", "ream")] == "T37" and ops[("H2", "pre_drill")] == "T34"  # Ø10 H7: pre-drill + reamer
    assert ops[("T1", "tap")] == "T13" and ops[("T1", "tap_drill")] == "T23"  # M6x1: Ø5 + tap
    assert ops[("S1", "slot")] == "T01"  # 8 mm slot with the Ø8 endmill in one pass

    odd = make_spec([{"id": "A", "type": "hole", "center_x": 20, "center_y": 20, "diameter": 7.3, "through_all": True},
                     {"id": "B", "type": "hole", "center_x": 60, "center_y": 20, "diameter": 12, "thread": "M12",
                      "depth": 12},
                     {"id": "C", "type": "slot", "start_x": 20, "start_y": 40, "end_x": 70, "end_y": 40, "width": 1.5,
                      "depth": 3}], L=100, W=60, T=15).model_copy(update={"material": "AlMgSi1"})
    checks, _, issues = tool_checks(odd, lib)
    st = {(c.feature_id, c.operation): c for c in checks}
    assert st[("A", "interpolate")].status == "info"  # no Ø7.3 drill: helical interpolation with an endmill
    assert st[("B", "tap")].status == "warning" and "M12" in st[("B", "tap")].message
    assert st[("C", "slot")].status == "warning" and "Verbreed" in st[("C", "slot")].suggested_fix

    steel = odd.model_copy(update={"material": "RVS 316"})  # stainless: the alu endmills do not count
    _, assign, _ = tool_checks(steel, lib)
    assert all("Alu" not in a.tool_name for a in assign if a.tool_type == "flat_endmill")


def test_dfm_report_carries_v2_fields(settings):
    from app.services.cad_compiler import compile_part
    from app.services.verifier import build_report

    spec = load_fixture("section6_example")
    res = compile_part(spec)
    rep = build_report(spec, 1, res.shape, res.feature_status, settings)
    assert rep.passed and rep.material_group == "aluminum"
    assert {c.feature_id for c in rep.tool_checks} >= {"feat_pocket_1", "feat_hole_1"}
    assert any(a.operation == "tap" and a.tool_id == "T12" for a in rep.recommended_tools)
    s = rep.stock_recommendation
    assert s.profile_type == "flat_bar" and (s.recommended_blank_mm.x, s.recommended_blank_mm.y, s.recommended_blank_mm.z) == (205, 120, 25)
    v10 = json.loads(rep.model_dump_json(exclude={"tool_checks", "recommended_tools", "stock_recommendation", "material_group"}))
    assert type(rep).model_validate(v10).tool_checks == []  # older reports stay valid


def test_config_api(client_factory, settings):
    with client_factory() as c:
        lib = c.get("/api/config/tools").json()
        assert lib["schema_version"] == "1.0" and len(lib["tools"]) >= 30
        lib["tools"][0]["diameter_mm"] = -1
        assert c.put("/api/config/tools", json=lib).status_code == 422
        lib["tools"] = lib["tools"][:3]
        lib["tools"][0]["diameter_mm"] = 8
        assert len(c.put("/api/config/tools", json=lib).json()["tools"]) == 3
        r = c.post("/api/config/tools/import-fusion", files={"file": ("lib.json", json.dumps(FUSION), "application/json")})
        assert r.status_code == 200 and r.json()["added"] + r.json()["updated"] == 4 and r.json()["total"] >= 6
        assert c.post("/api/config/tools/import-fusion", files={"file": ("x.json", "{}", "application/json")}).status_code == 422
        stock = c.get("/api/config/stock").json()
        stock["default_allowances"]["xy_per_side_mm"] = 3
        assert c.put("/api/config/stock", json=stock).json()["default_allowances"]["xy_per_side_mm"] == 3
