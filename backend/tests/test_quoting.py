"""v2.0 stream C: deterministic cycle-time & cost quoting (SPEC_V2_0_MULTISIDE_FIXTURING.md §3.4)."""

from __future__ import annotations

import csv
import io
from pathlib import Path

import pytest

from app.schemas.part_spec import PartSpec
from app.schemas.quote import HoleInput, QuoteInputs, QuoteRequest, ShopRates
from app.services.cad_compiler import compile_part
from app.services.quoting_engine import compute_quote, extract_inputs, hole_seconds, load_rates, peck_factor
from tests.test_api import upload, wait_terminal

RATES = ShopRates.model_validate_json((Path(__file__).parents[1] / "app" / "defaults" / "shop_rates.json").read_text())


def inputs(**over) -> QuoteInputs:
    d = dict(version=1, material="EN AW-6082", material_group="aluminum", part_volume_mm3=300_000,
             raw_volume_mm3=500_000, raw_mass_kg=1.0, machined_area_cm2=400, fine_area_cm2=50,
             holes=[HoleInput(feature_id="T", diameter_mm=6.8, depth_mm=12, thread=True),
                    HoleInput(feature_id="T", diameter_mm=6.8, depth_mm=12, thread=True),
                    HoleInput(feature_id="R", diameter_mm=10, depth_mm=30, tight=True)],
             unique_tools=4, setups_3axis=3, setups_5axis=2)
    d.update(over)
    return QuoteInputs(**d)


def test_formula_breakdown_by_hand():
    q = compute_quote(inputs(), RATES, QuoteRequest(), "job")
    assert q.c_mat == pytest.approx(1.0 * 9.5 * 1.08)
    m3 = q.machines["3axis_vmc"]
    lines = {ln.key: ln.value for ln in m3.lines}
    t_rough = 200 / 45
    t_finish = (350 + 1.5 * 50) / 100 * 1.2
    t_holes = (2 * (12 + 10) + (12 + 18)) / 60
    t_atc = 4 * 6 / 60
    assert lines["t_rough"] == pytest.approx(t_rough, abs=1e-4)
    assert lines["t_finish"] == pytest.approx(t_finish, abs=1e-4)
    assert lines["t_holes"] == pytest.approx(t_holes, abs=1e-4)
    assert m3.t_cycle_min == pytest.approx(t_rough + t_finish + t_holes + t_atc, abs=1e-4)
    assert m3.t_setup_min == 20 + 3 * 15 and m3.t_load_unload_min == pytest.approx(45 * 3 / 60)
    cycle = t_rough + t_finish + t_holes + t_atc
    for b in m3.price_breaks:
        unit = 9.5 * 1.08 + 65 / (60 * b.quantity) * 75 + (cycle + 2.25) / 60 * 75
        assert b.unit_cost == pytest.approx(unit, abs=0.006)
        assert b.unit_price == pytest.approx(unit * 1.2, abs=0.006)
        assert b.total_price == pytest.approx(b.unit_price * b.quantity, abs=0.01 * b.quantity)
    assert [b.quantity for b in m3.price_breaks] == [1, 5, 10, 25, 50]
    assert m3.price_breaks[0].unit_cost == pytest.approx(108.29, abs=0.01)  # literal check of the example above


def test_quantity_amortisation_and_machine_comparison():
    q = compute_quote(inputs(), RATES, QuoteRequest(custom_quantity=7, margin_pct=0), "job")
    for mq in q.machines.values():
        costs = [b.unit_cost for b in mq.price_breaks]
        assert costs == sorted(costs, reverse=True)  # setup cost amortised over more parts
        shares = {b.quantity: b.setup_share for b in mq.price_breaks}
        assert shares[1] == pytest.approx(mq.t_setup_min / 60 * mq.hourly_rate, abs=0.006)
        assert shares[50] == pytest.approx(shares[1] / 50, abs=0.006)
    assert 7 in [b.quantity for b in q.machines["3axis_vmc"].price_breaks]
    five = q.machines["5axis_3plus2"]
    assert five.setups == 2 and five.t_setup_min == 20 + 2 * 20 and five.hourly_rate == 105
    assert q.cheaper in ("3axis_vmc", "5axis_3plus2")
    many = compute_quote(inputs(setups_3axis=6, setups_5axis=2), RATES, QuoteRequest(quantities=[1]), "job")
    assert many.cheaper == "5axis_3plus2"  # six 3-axis clampings cost more than the higher 5-axis rate


def test_softjaws_margin_and_reproducibility():
    base = compute_quote(inputs(), RATES, QuoteRequest(include_softjaws=False), "job")
    soft = compute_quote(inputs(), RATES, QuoteRequest(include_softjaws=True), "job")
    assert soft.machines["3axis_vmc"].t_setup_min - base.machines["3axis_vmc"].t_setup_min == 25
    assert compute_quote(inputs(), RATES, QuoteRequest(), "job") == base  # deterministic
    stainless = compute_quote(inputs(material_group="brass_copper"), RATES, QuoteRequest(), "job")
    assert stainless.material_group == "steel" and stainless.notes


def test_peck_and_hole_seconds():
    m = RATES.materials["steel"]
    assert peck_factor(30, 10) == 1.0 and peck_factor(60, 10) == pytest.approx(2.0)
    h = HoleInput(feature_id="H", diameter_mm=6.6, depth_mm=20, counterbore=True, countersink=False)
    assert hole_seconds(h, m) == pytest.approx(24 * peck_factor(20, 6.6) + 12)


def test_inputs_from_the_brep(settings):
    spec = PartSpec.model_validate({"schema_version": "2.0", "material": "EN AW-6082",
                                    "stock": {"length_x": 100, "width_y": 50, "thickness_z": 20},
                                    "features": [{"id": "H", "type": "hole", "center_x": 20, "center_y": 20,
                                                  "diameter": 10, "through_all": True, "tolerance": "H7"}]})
    res = compile_part(spec)
    from app.services.verifier import build_report

    rep = build_report(spec, 1, res.shape, res.feature_status, settings)
    inp = extract_inputs(spec, res.shape, rep, 1, settings)
    assert inp.material_group == "aluminum" and len(inp.holes) == 1 and inp.holes[0].tight
    assert inp.holes[0].depth_mm == 20
    total = (2 * (100 * 50 + 100 * 20 + 50 * 20) - 2 * 3.14159265 * 25 + 3.14159265 * 10 * 20) / 100
    assert inp.machined_area_cm2 + inp.untouched_area_cm2 == pytest.approx(total, rel=1e-4)
    assert inp.fine_area_cm2 == pytest.approx(3.14159265 * 10 * 20 / 100, rel=1e-4)  # H7 bore
    assert inp.raw_volume_mm3 > inp.part_volume_mm3 and inp.raw_mass_kg
    assert inp.setups_3axis == rep.setup_plan.min_setups_3axis


def test_quote_api_and_exports(client_factory, fixtures_dir: Path, settings):
    with client_factory() as client:
        rates = client.get("/api/config/shop-rates").json()
        assert rates["machines"]["3axis_vmc"]["hourly_rate"] == 75
        rates["machines"]["3axis_vmc"]["hourly_rate"] = 80
        assert client.put("/api/config/shop-rates", json=rates).status_code == 200
        assert load_rates(settings).machines["3axis_vmc"].hourly_rate == 80
        assert client.put("/api/config/shop-rates", json={"machines": {}}).status_code == 422

        job_id = upload(client, fixtures_dir)
        assert wait_terminal(client, job_id)["status"] == "ready"
        r = client.post(f"/api/jobs/{job_id}/quote", json={"quantities": [1, 10], "margin_pct": 15})
        assert r.status_code == 200, r.text
        q = r.json()
        assert q["margin_pct"] == 15 and q["machines"]["3axis_vmc"]["hourly_rate"] == 80
        assert [b["quantity"] for b in q["machines"]["5axis_3plus2"]["price_breaks"]] == [1, 10]
        assert q["inputs"]["holes"] and q["c_mat"] > 0
        assert client.post(f"/api/jobs/{job_id}/quote", json={"quantities": [1, 10], "margin_pct": 15}).json() == q

        csv_r = client.get(q["exports"]["csv"])
        rows = list(csv.DictReader(io.StringIO(csv_r.text), delimiter=";"))
        assert len(rows) == 4 and {r["machine"] for r in rows} == {"3axis_vmc", "5axis_3plus2"}
        pdf = client.get(q["exports"]["pdf"])
        assert pdf.status_code == 200 and pdf.content[:5] == b"%PDF-"
        assert client.get(f"/api/jobs/{job_id}/quote/export.xls").status_code == 404
