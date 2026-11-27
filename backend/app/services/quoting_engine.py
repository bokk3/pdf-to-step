"""Deterministic cycle-time & cost quoting (SPEC_V2_0_MULTISIDE_FIXTURING.md §3.4). Never an LLM price.

Every number traces back to a geometry fact (volumes, B-Rep face areas, hole list, setup count) and a rate in
data/config/shop_rates.json; each line of the breakdown carries its formula.

    C_mat        = m_raw * p_mat * (1 + k_saw_waste)
    t_rough      = (V_raw - V_part)[cm³] / (MRR_mat * eta_chip_load)
    t_finish     = A_machined[cm²] / 100 * f_finish  (+ fine-finish / tight-fit faces * fine_finish_factor)
    t_holes      = sum per hole: base * peck(L/D) + tap + ream + 0.5 base (cbore) + 0.5 base (csk)   [s]
                   peck(L/D) = 1 + max(0, L/D - 3) / 3
    t_atc        = tool_change_sec * unique tools                                                    [s]
    t_cycle      = t_rough + t_finish + (t_holes + t_atc) / 60
    t_load       = load_unload_sec * N_setups / 60
    t_setup      = t_cam_prog + N_setups * t_fixture_setup + I_softjaws * t_mill_softjaws
    C_unit(Q)    = C_mat + t_setup / (60 Q) * R + (t_cycle + t_load) / 60 * R ;  price = C_unit * (1 + margin)
"""

from __future__ import annotations

import csv
import io
import json
from pathlib import Path

from app.schemas.quote import (
    MACHINES,
    HoleInput,
    Line,
    MachineQuote,
    PriceBreak,
    QuoteBreakdown,
    QuoteInputs,
    QuoteRequest,
    ShopRates,
)

RATES_FILE = "shop_rates.json"
FALLBACK_GROUP = "steel"


def load_rates(s) -> ShopRates:
    from app.services.shop_config import load_config

    return load_config(s, RATES_FILE, ShopRates)


def save_rates(s, rates: ShopRates) -> None:
    from app.services.shop_config import save_config

    save_config(s, RATES_FILE, rates)


def _r(x: float, n: int = 4) -> float:
    return round(float(x), n)


def peck_factor(depth: float, dia: float) -> float:
    return 1.0 + max(0.0, depth / dia - 3.0) / 3.0


def hole_seconds(h: HoleInput, m) -> float:
    t = m.hole_sec_base * peck_factor(h.depth_mm, h.diameter_mm)
    t += m.tap_sec if h.thread else 0.0
    t += m.ream_sec if h.tight else 0.0
    t += 0.5 * m.hole_sec_base * (int(h.counterbore) + int(h.countersink))
    return t


def compute_quote(inputs: QuoteInputs, rates: ShopRates, req: QuoteRequest, job_id: str) -> QuoteBreakdown:
    notes = list(inputs.notes)
    group = inputs.material_group
    if group not in rates.materials:
        notes.append(f"Geen tarieven voor materiaalgroep '{group}': gerekend met '{FALLBACK_GROUP}'.")
        group = FALLBACK_GROUP if FALLBACK_GROUP in rates.materials else next(iter(rates.materials))
    m = rates.materials[group]
    margin = rates.margin_pct_default if req.margin_pct is None else req.margin_pct
    softjaws = inputs.softjaws_recommended if req.include_softjaws is None else req.include_softjaws
    quantities = sorted(set(req.quantities) | ({req.custom_quantity} if req.custom_quantity else set()))

    # material
    if inputs.raw_mass_kg is None:
        notes.append("Ruw gewicht onbekend (materiaal / dichtheid): materiaalkosten 0.")
    mass = inputs.raw_mass_kg or 0.0
    c_mat = mass * m.price_per_kg * (1 + rates.saw_waste_factor)
    mat_lines = [Line(key="c_mat", label="Ruw materiaal", value=_r(c_mat, 2), unit=rates.currency,
                      formula=f"{mass:.4f} kg x {m.price_per_kg:g}/kg x (1 + {rates.saw_waste_factor:g})")]

    # machine-independent times
    chips_cm3 = max(inputs.raw_volume_mm3 - inputs.part_volume_mm3, 0.0) / 1000.0
    t_rough = chips_cm3 / (m.mrr_cm3_per_min * m.chip_load_efficiency)
    normal_area = max(inputs.machined_area_cm2 - inputs.fine_area_cm2, 0.0)
    t_finish = (normal_area + rates.fine_finish_factor * inputs.fine_area_cm2) / 100.0 * m.finish_min_per_100cm2
    t_holes_s = sum(hole_seconds(h, m) for h in inputs.holes)

    machines: dict[str, MachineQuote] = {}
    for mt in MACHINES:
        mr = rates.machines[mt]
        n_setups = inputs.setups_3axis if mt == "3axis_vmc" else inputs.setups_5axis
        per_setup = rates.setup_minutes.per_3axis_setup if mt == "3axis_vmc" else rates.setup_minutes.per_5axis_setup
        t_setup = (rates.setup_minutes.base_cam_programming + n_setups * per_setup
                   + (rates.setup_minutes.mill_softjaws_pair if softjaws else 0.0))
        t_atc_s = mr.tool_change_sec * inputs.unique_tools
        t_cycle = t_rough + t_finish + (t_holes_s + t_atc_s) / 60.0
        t_load = mr.load_unload_sec * n_setups / 60.0
        R = mr.hourly_rate
        lines = [
            Line(key="t_rough", label="Voorfrezen", value=_r(t_rough), unit="min",
                 formula=f"({inputs.raw_volume_mm3 / 1000:.3f} - {inputs.part_volume_mm3 / 1000:.3f}) cm³ / "
                         f"({m.mrr_cm3_per_min:g} x {m.chip_load_efficiency:g})"),
            Line(key="t_finish", label="Nafrezen", value=_r(t_finish), unit="min",
                 formula=f"({normal_area:.2f} + {rates.fine_finish_factor:g} x {inputs.fine_area_cm2:.2f}) cm² / 100 x "
                         f"{m.finish_min_per_100cm2:g}"),
            Line(key="t_holes", label="Boren / tappen / ruimen", value=_r(t_holes_s / 60), unit="min",
                 formula=f"{len(inputs.holes)} gaten: base {m.hole_sec_base:g}s x peck + tap {m.tap_sec:g}s + "
                         f"ruim {m.ream_sec:g}s + verzinking 0.5 x base"),
            Line(key="t_atc", label="Gereedschapwissels", value=_r(t_atc_s / 60), unit="min",
                 formula=f"{inputs.unique_tools} x {mr.tool_change_sec:g}s"),
            Line(key="t_cycle", label="Cyclustijd", value=_r(t_cycle), unit="min",
                 formula="t_rough + t_finish + t_holes + t_atc"),
            Line(key="t_load", label="In- / uitspannen", value=_r(t_load), unit="min",
                 formula=f"{mr.load_unload_sec:g}s x {n_setups} opspanningen"),
            Line(key="t_setup", label="Programmeren + instellen", value=_r(t_setup), unit="min",
                 formula=f"{rates.setup_minutes.base_cam_programming:g} + {n_setups} x {per_setup:g}"
                         + (f" + {rates.setup_minutes.mill_softjaws_pair:g} (zachte bekken)" if softjaws else "")),
        ]
        breaks = []
        for q in quantities:
            setup_share = t_setup / (60.0 * q) * R
            unit = c_mat + setup_share + (t_cycle + t_load) / 60.0 * R
            price = unit * (1 + margin / 100.0)
            breaks.append(PriceBreak(quantity=q, unit_cost=_r(unit, 2), unit_price=_r(price, 2),
                                     total_price=_r(price * q, 2), setup_share=_r(setup_share, 2)))
        machines[mt] = MachineQuote(machine_type=mt, label=mr.label, hourly_rate=R, setups=n_setups,
                                    t_setup_min=_r(t_setup), t_cycle_min=_r(t_cycle), t_load_unload_min=_r(t_load),
                                    lines=lines, price_breaks=breaks)
    qmax = quantities[-1]
    last = {mt: next(b for b in mq.price_breaks if b.quantity == qmax).unit_price for mt, mq in machines.items()}
    cheaper = min(last, key=last.get) if len(set(last.values())) > 1 else None
    return QuoteBreakdown(job_id=job_id, version=inputs.version, currency=rates.currency, margin_pct=margin,
                          material_group=group, include_softjaws=softjaws, c_mat=_r(c_mat, 2), material_lines=mat_lines,
                          machines=machines, selected=req.machine_type, cheaper=cheaper, inputs=inputs, notes=notes)


# --------------------------------------------------------------------------- geometry inputs (CAD worker)


def extract_inputs(spec, shape, report, version: int, settings) -> QuoteInputs:
    """Volumes, machined / fine B-Rep face areas, hole list, unique tools, setup counts (one revision)."""
    from app.schemas.frames import frames
    from app.schemas.part_spec import Hole
    from app.services.setup_planner import deck_off_from_stock, plan_setups
    from app.services.step_xcaf_exporter import classify_faces, is_tight_fit, load_palette
    from app.services.tool_library import material_group

    notes: list[str] = []
    rec = report.stock_recommendation if report else None
    geo = report.geometry if report else None
    part_v = float(geo.volume_mm3) if geo else float(shape.volume)
    bb = shape.bounding_box()
    size = (bb.size.X, bb.size.Y, bb.size.Z)
    if rec is not None:
        blank = (rec.recommended_blank_mm.x, rec.recommended_blank_mm.y, rec.recommended_blank_mm.z)
        raw_mass = rec.raw_mass_kg
    else:
        (x0, y0, z0), (x1, y1, z1) = spec.stock_bbox()
        blank = (x1 - x0, y1 - y0, z1 - z0)
        raw_mass = None
        notes.append("Geen voorraadadvies: ruw volume = stockmaten.")
    raw_v = blank[0] * blank[1] * blank[2]

    # faces on a bounding plane whose axis the raw blank already has to size are never machined
    untouched_axes = [i for i in range(3) if abs(blank[i] - size[i]) < 1e-6]
    lo, hi = (bb.min.X, bb.min.Y, bb.min.Z), (bb.max.X, bb.max.Y, bb.max.Z)
    palette = load_palette(settings)
    col = classify_faces(shape, spec, palette)
    fine_ids = {id(f.wrapped) for f, cat in col.faces if cat in ("fine_finish", "tight_fit")}
    fine_faces = [f for f, cat in col.faces if cat in ("fine_finish", "tight_fit")]
    total = untouched = 0.0
    for f in shape.faces():
        a = float(f.area)
        total += a
        if untouched_axes:
            fb = f.bounding_box()
            fl, fh = (fb.min.X, fb.min.Y, fb.min.Z), (fb.max.X, fb.max.Y, fb.max.Z)
            if any(abs(fl[i] - fh[i]) < 1e-6 and (abs(fl[i] - lo[i]) < 1e-6 or abs(fl[i] - hi[i]) < 1e-6)
                   for i in untouched_axes):
                untouched += a
    fine = sum(float(f.area) for f in fine_faces)
    del fine_ids

    frs = frames(spec)
    holes = []
    for f in spec.features:
        if not isinstance(f, Hole):
            continue
        depth = frs[f.face].thickness if f.through_all else float(f.depth or frs[f.face].thickness)
        for _ in f.positions():
            holes.append(HoleInput(feature_id=f.id, diameter_mm=f.drill_diameter(), depth_mm=depth,
                                   thread=bool(f.thread), tight=is_tight_fit(f.tolerance, palette),
                                   counterbore=f.counterbore is not None, countersink=f.countersink is not None))
    unique_tools = len({a.tool_id for a in report.recommended_tools}) if report else 0
    plan = report.setup_plan if report and report.setup_plan else plan_setups(spec, deck_off_from_stock(spec, rec))
    return QuoteInputs(version=version, material=spec.material, material_group=material_group(spec.material),
                       part_volume_mm3=_r(part_v), raw_volume_mm3=_r(raw_v),
                       raw_mass_kg=None if raw_mass is None else _r(raw_mass, 6),
                       machined_area_cm2=_r((total - untouched) / 100.0), fine_area_cm2=_r(fine / 100.0),
                       untouched_area_cm2=_r(untouched / 100.0), holes=holes, unique_tools=unique_tools,
                       setups_3axis=plan.min_setups_3axis, setups_5axis=plan.min_setups_5axis_3plus2,
                       softjaws_recommended=plan.softjaws_recommended, notes=notes)


def cad_quote_inputs(job_dir: str, version: int, settings_data: dict) -> dict:
    """Process-pool entry: inputs of one revision, cached as versions/v<n>/quote_inputs.json."""
    from build123d import import_step

    from app.config import Settings
    from app.schemas.part_spec import DFMReport, PartSpec

    jd = Path(job_dir)
    vdir = jd / "versions" / f"v{version}"
    cache = vdir / "quote_inputs.json"
    if cache.exists():
        return json.loads(cache.read_text(encoding="utf-8"))
    s = Settings(**settings_data)
    spec = PartSpec.model_validate_json((jd / "specs" / f"part_spec_v{version}.json").read_text(encoding="utf-8"))
    rp = vdir / "dfm_report.json"
    report = DFMReport.model_validate_json(rp.read_text(encoding="utf-8")) if rp.exists() else None
    shape = import_step(str(vdir / "model.step"))
    solids = shape.solids()
    shape = solids[0] if len(solids) == 1 else shape
    inputs = extract_inputs(spec, shape, report, version, s)
    cache.write_text(inputs.model_dump_json(indent=1), encoding="utf-8")
    return json.loads(inputs.model_dump_json())


# --------------------------------------------------------------------------- exports


def quote_csv(q: QuoteBreakdown) -> str:
    buf = io.StringIO()
    w = csv.writer(buf, delimiter=";")
    w.writerow(["job_id", "version", "machine", "quantity", "unit_cost", "unit_price", "total_price", "setup_share",
                "currency", "margin_pct", "t_setup_min", "t_cycle_min", "c_mat"])
    for mt, mq in q.machines.items():
        for b in mq.price_breaks:
            w.writerow([q.job_id, q.version, mt, b.quantity, f"{b.unit_cost:.2f}", f"{b.unit_price:.2f}",
                        f"{b.total_price:.2f}", f"{b.setup_share:.2f}", q.currency, f"{q.margin_pct:g}",
                        f"{mq.t_setup_min:.2f}", f"{mq.t_cycle_min:.2f}", f"{q.c_mat:.2f}"])
    return buf.getvalue()


def quote_pdf(q: QuoteBreakdown, title: str, path: Path) -> Path:
    """1-page A4 quote summary: price breaks 3-axis vs 5-axis 3+2 + the traceable breakdown."""
    import pymupdf

    doc = pymupdf.open()
    page = doc.new_page(width=595, height=842)
    y = 50

    def t(s: str, x: float = 40, size: float = 9.5, bold: bool = False, color=(0, 0, 0)) -> None:
        page.insert_text((x, y), s, fontsize=size, fontname="hebo" if bold else "helv", color=color)

    t(f"Offerte-calculatie {title}", size=16, bold=True)
    y += 20
    t(f"Job {q.job_id} v{q.version} - materiaalgroep {q.material_group} - marge {q.margin_pct:g} % - "
      f"{'met' if q.include_softjaws else 'zonder'} zachte bekken", size=9, color=(0.3, 0.3, 0.3))
    y += 26
    t("Stuks", bold=True)
    for k, mt in enumerate(q.machines):
        t(f"{q.machines[mt].label} ({q.currency}/st | totaal)", x=110 + k * 230, bold=True)
    y += 14
    qtys = [b.quantity for b in next(iter(q.machines.values())).price_breaks]
    for qty in qtys:
        t(str(qty))
        for k, mt in enumerate(q.machines):
            b = next(b for b in q.machines[mt].price_breaks if b.quantity == qty)
            best = q.cheaper == mt and qty == qtys[-1]
            t(f"{b.unit_price:10.2f}   |   {b.total_price:12.2f}", x=110 + k * 230, bold=best)
        y += 13
    y += 12
    t(f"Ruw materiaal: {q.c_mat:.2f} {q.currency}  ({q.material_lines[0].formula})", size=8.5)
    y += 20
    for mt, mq in q.machines.items():
        t(f"{mq.label}: {mq.setups} opspanning(en), {mq.hourly_rate:g} {q.currency}/h", bold=True)
        y += 13
        for ln in mq.lines:
            t(f"{ln.label}", x=50, size=8.5)
            t(f"{ln.value:9.2f} {ln.unit}", x=200, size=8.5)
            t(ln.formula[:80], x=280, size=7.5, color=(0.35, 0.35, 0.35))
            y += 11
        y += 8
    for n in q.notes[:6]:
        t(f"- {n}", size=8, color=(0.5, 0.3, 0.0))
        y += 11
    y = 815
    t("Deterministische berekening uit B-Rep-volumes, -oppervlakken, gatenlijst en data/config/shop_rates.json.",
      size=7, color=(0.45, 0.45, 0.45))
    path.parent.mkdir(parents=True, exist_ok=True)
    doc.save(path)
    doc.close()
    return path
