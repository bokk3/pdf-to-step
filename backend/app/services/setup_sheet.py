"""1-page A4 setup sheet for the machine (SPEC_V1_5_CAM_SHOPFLOOR.md §2.3). Deterministic vector PDF, no LLM.

Header (part, drawing number, revision, material, job, shopfloor QR) · isometric view with the G54 / datum (0,0,0)
triad (X red, Y green, Z blue) · raw stock box · tooling table from DFM v2 ``recommended_tools`` · critical
inspection items (threads, tight fits, active warnings) · CAM colour legend matching model.step.

Rendered with the same sheet model as the EN ISO drawing (PyMuPDF, selectable text) instead of WeasyPrint /
CairoSVG, which need native libraries that are not available on the Windows workstation.
"""

from __future__ import annotations

import datetime as dt
import math
from collections import OrderedDict
from pathlib import Path

import numpy as np

from app.schemas.part_spec import DFMReport, Hole, PartSpec, Severity, parse_metric_thread
from app.services.iso_template import Sheet, get_font

W, H, M = 210.0, 297.0, 10.0
TOOL_NL = {"flat_endmill": "vingerfrees", "bull_endmill": "torusfrees", "ball_endmill": "bolkopfrees",
           "slot_mill": "spiebaanfrees", "face_mill": "vlakfrees", "chamfer_mill": "afschuinfrees",
           "corner_rounding": "afrondfrees", "spot_drill": "centerboor", "center_drill": "centerboor", "drill": "boor",
           "tap": "tap", "thread_mill": "draadfrees", "reamer": "ruimer", "boring_bar": "kotterbaar",
           "counterbore": "vlakverzinker", "countersink": "kegelverzinker", "other": "overig"}
OP_NL = {"face": "vlakfrezen", "contour": "contour", "pocket": "pocket", "slot": "sleuf", "drill": "boren",
         "tap_drill": "kernboren", "tap": "tappen", "thread_mill": "draadfrezen", "pre_drill": "voorboren",
         "ream": "ruimen", "bore": "kotteren", "counterbore": "verzinken", "countersink": "kegelverzinken",
         "chamfer": "afschuinen", "fillet": "afronden", "boss": "nok", "step": "stap", "interpolate": "interpoleren"}
RED, GREEN, BLUE = (0.88, 0.12, 0.12), (0.10, 0.62, 0.25), (0.10, 0.35, 0.95)


def _project(shape, pts3: list[tuple[float, float, float]]):
    """Points in the iso view frame used by drawing_engine.project_views (same viewport origin)."""
    from build123d import Edge

    from app.services.drawing_engine import VIEW_DEFS

    d, up = VIEW_DEFS["iso"]
    bb = shape.bounding_box()
    dist = (bb.center().length + bb.diagonal) * 6 + 100
    dv = np.array(d) / np.linalg.norm(d)
    out = []
    for p in pts3:  # a tiny edge per point (HLR projects edges, not vertices)
        e = Edge.make_line(p, (p[0] + 1e-3, p[1], p[2]))
        vis, hid = e.project_to_viewport(tuple(dv * dist), up, (0.0, 0.0, 0.0))
        ed = (list(vis) + list(hid))[0]
        q = ed @ 0.0
        out.append((q.X, q.Y))
    return out


def generate_setup_sheet(spec: PartSpec, shape, report: DFMReport | None, out_pdf: Path, *, job_id: str = "",
                         drawing_number: str = "", qr_url: str | None = None, palette=None,
                         version: int | None = None) -> Path:
    from app.services.drawing_engine import project_views
    from app.services.qr_helper import draw_qr

    sh = Sheet("A4_PORTRAIT", get_font())
    thick, thin = sh.lines.thick, sh.lines.thin
    sh.rect((M, M, W - M, H - M), width=0.5, layer="FRAME")

    # 1. header --------------------------------------------------------------------------------------------
    qr = 34.0
    draw_qr(sh, qr_url, W - M - qr - 1, M + 1, qr)
    sh.text("INSTELBLAD / SETUP SHEET", M + 3, M + 7, 3.5)
    title = spec.part_name or "PART"
    size = 6.0
    while size > 3.5 and sh.measure(title, size) > W - 2 * M - qr - 8:
        size -= 0.25
    sh.text(title, M + 3, M + 16, size)
    dm = spec.drawing_metadata
    rows = [("Tekeningnr.", drawing_number or dm.drawing_number or "-"), ("Revisie", f"{dm.revision}" + (f" (v{version})" if version else "")),
            ("Materiaal", spec.material or "-"), ("Job", job_id or "-"), ("Datum", dt.date.today().isoformat())]
    for i, (k, v) in enumerate(rows):
        sh.text(k, M + 3, M + 24 + i * 4.2, 2.5)
        sh.text(v, M + 28, M + 24 + i * 4.2, 2.5)
    y = M + 46
    sh.line((M, y), (W - M, y), width=thin, layer="FRAME")

    # 2. isometric view + G54 triad ---------------------------------------------------------------------------
    box = (M + 3, y + 6, W - M - 3, y + 78)
    sh.text("ISOMETRISCH · G54 = DATUM (0,0,0)", M + 3, y + 5, 2.5)
    iso = project_views(shape)["iso"]
    (x0, y0, z0), (x1, y1, z1) = spec.stock_bbox()
    L = max(x1 - x0, y1 - y0, z1 - z0) * 0.28
    tri = _project(shape, [(0, 0, 0), (L, 0, 0), (0, L, 0), (0, 0, L)])
    us = [p[0] for pl in iso.visible for p in pl] + [p[0] for p in tri]
    vs = [p[1] for pl in iso.visible for p in pl] + [p[1] for p in tri]
    u0, u1, v0, v1 = min(us), max(us), min(vs), max(vs)
    s = min((box[2] - box[0] - 16) / max(u1 - u0, 1e-6), (box[3] - box[1] - 10) / max(v1 - v0, 1e-6))
    cx, cy = (box[0] + box[2]) / 2, (box[1] + box[3]) / 2 + 2
    T = lambda u, v: (cx + (u - (u0 + u1) / 2) * s, cy - (v - (v0 + v1) / 2) * s)  # noqa: E731
    for pl in iso.visible:
        sh.polyline([T(u, v) for u, v in pl], width=thick, layer="VISIBLE")
    o = T(*tri[0])
    for (u, v), col, name in zip(tri[1:], (RED, GREEN, BLUE), "XYZ"):
        e = T(u, v)
        sh.line(o, e, width=0.6, layer="TRIAD", color=col)
        dx, dy = e[0] - o[0], e[1] - o[1]
        n = math.hypot(dx, dy) or 1
        ux, uy = dx / n, dy / n
        sh.fill([e, (e[0] - ux * 3 - uy * 1.1, e[1] - uy * 3 + ux * 1.1), (e[0] - ux * 3 + uy * 1.1, e[1] - uy * 3 - ux * 1.1)],
                layer="TRIAD", color=col)
        sh.text(name, e[0] + ux * 2.5 - 1, e[1] + uy * 2.5 + 1.2, 3.0, layer="TRIAD", color=col)
    sh.circle(o, 1.3, layer="TRIAD")
    sh.text("G54", o[0] - 9, o[1] + 4.5, 2.5, layer="TRIAD")
    y = box[3] + 3
    sh.line((M, y), (W - M, y), width=thin, layer="FRAME")

    # 3. raw stock -----------------------------------------------------------------------------------------
    sh.text("RUWE VOORRAAD / RAW STOCK", M + 3, y + 5.5, 3.0)
    rec = report.stock_recommendation if report else None
    if rec:
        b, r = rec.recommended_blank_mm, rec.required_blank_mm
        lines = [f"{rec.profile_label}  ({rec.material_group})",
                 f"Zaagblok: {b.x:g} x {b.y:g} x {b.z:g} mm   ·   zaaglengte {rec.saw_cut_length_mm:g} mm",
                 f"Minimaal nodig: {r.x:g} x {r.y:g} x {r.z:g} mm (+{rec.allowances.xy_per_side_mm:g}/zijde, "
                 f"+{rec.allowances.z_top_face_mm:g} vlakken, +{rec.allowances.z_clamp_margin_mm:g} klemming)"]
        if rec.raw_mass_kg is not None:
            lines.append(f"Massa ruw {rec.raw_mass_kg:.3f} kg  ·  afgewerkt {rec.part_mass_kg or 0:.3f} kg  ·  "
                         f"verspaning {rec.chip_removal_pct or 0:.1f} %")
        if rec.note:
            lines.append(rec.note)
    else:
        lines = ["Geen voorraadadvies (materiaal of catalogus ontbreekt)."]
    for i, ln in enumerate(lines):
        sh.text(ln, M + 3, y + 11 + i * 4.3, 2.6)
    y += 13 + len(lines) * 4.3
    sh.line((M, y), (W - M, y), width=thin, layer="FRAME")

    # 4. tooling table ----------------------------------------------------------------------------------------
    sh.text("GEREEDSCHAP / TOOLING", M + 3, y + 5.5, 3.0)
    cols = [("Tool", M + 3), ("Type", M + 18), ("Ø", M + 50), ("Snijl.", M + 64), ("Features / bewerking", M + 82)]
    yy = y + 11
    for name, x in cols:
        sh.text(name, x, yy, 2.5)
    sh.line((M + 2, yy + 1.2), (W - M - 2, yy + 1.2), width=thin, layer="TABLE")
    tools: OrderedDict[str, dict] = OrderedDict()
    for a in (report.recommended_tools if report else []):
        t = tools.setdefault(a.tool_id, {"a": a, "uses": []})
        use = f"{a.feature_id if a.feature_id not in ('stock', 'outer_profile') else ''} {OP_NL.get(a.operation, a.operation)}".strip()
        if use not in t["uses"]:
            t["uses"].append(use)
    order = sorted(tools.values(), key=lambda t: (int("".join(ch for ch in t["a"].tool_id if ch.isdigit()) or 0), t["a"].tool_id))
    max_rows = 10
    for i, t in enumerate(order[:max_rows]):
        a = t["a"]
        ry = yy + 5 + i * 4.2
        uses = ", ".join(t["uses"])
        while sh.measure(uses, 2.5) > W - M - 4 - cols[4][1] and len(uses) > 4:
            uses = uses[:-4] + "…"
        for text, x in ((a.tool_id, cols[0][1]), (TOOL_NL.get(a.tool_type, a.tool_type), cols[1][1]),
                        (f"{a.diameter_mm:g}", cols[2][1]), (f"{a.flute_length_mm:g}", cols[3][1]), (uses, cols[4][1])):
            sh.text(text, x, ry, 2.5)
    if len(order) > max_rows:
        sh.text(f"… + {len(order) - max_rows} gereedschap(pen): zie DFM-rapport", cols[1][1], yy + 5 + max_rows * 4.2, 2.5)
    if not order:
        sh.text("Geen gereedschapsadvies (gereedschapsbibliotheek leeg of uitgeschakeld).", cols[0][1], yy + 5, 2.5)
    y = yy + 5 + max(1, min(len(order), max_rows) + (len(order) > max_rows)) * 4.2 + 1
    sh.line((M, y), (W - M, y), width=thin, layer="FRAME")

    # 5. critical inspection ------------------------------------------------------------------------------
    from app.services.verifier import is_tight

    sh.text("KRITISCHE CONTROLE / INSPECTION", M + 3, y + 5.5, 3.0)
    items: list[str] = []
    for f in spec.features:
        if isinstance(f, Hole) and f.thread:
            t = parse_metric_thread(f.thread)
            n = len(f.positions())
            depth = f"↧{f.thread_depth:g}" if f.thread_depth else ("THRU" if f.through_all else f"↧{f.depth:g}")
            items.append(f"{f.id}: {n}x {f.thread}{'' if t else ' (niet-metrisch)'} {depth} - draadkaliber")
        if isinstance(f, Hole) and is_tight(f.tolerance):
            items.append(f"{f.id}: {len(f.positions())}x Ø{f.diameter:g} {f.tolerance} - pasdoorn / meten")
    for g in dm.gdt:
        items.append(f"{g.feature_id or 'deel'}: {g.characteristic} {g.tolerance_mm:g} mm (ISO 1101)")
    for sf in dm.surface_finishes:
        items.append(f"{sf.feature_id}: Ra {sf.ra_um:g} µm")
    for iss in (report.issues if report else []):
        if iss.severity == Severity.warning:
            items.append(f"⚠ {iss.feature_id + ': ' if iss.feature_id else ''}{iss.message}")
    if not items:
        items = ["Geen kritische maten of open waarschuwingen."]
    legend_top = H - M - 24
    max_items = int((legend_top - (y + 10)) // 4.2)
    for i, it in enumerate(items[:max_items]):
        txt = it
        while sh.measure(txt, 2.5) > W - 2 * M - 8 and len(txt) > 6:
            txt = txt[:-2] + "…"
        sh.text(txt, M + 3, y + 11 + i * 4.2, 2.5)
    if len(items) > max_items:
        sh.text(f"… + {len(items) - max_items} meer: zie DFM-rapport in de app", M + 3, y + 11 + max_items * 4.2, 2.5)

    # 6. CAM colour legend ---------------------------------------------------------------------------------
    sh.line((M, legend_top), (W - M, legend_top), width=thin, layer="FRAME")
    sh.text("KLEUREN IN MODEL.STEP (AP214)", M + 3, legend_top + 5, 2.5)
    if palette is not None:
        keys = ["thread", "tight_fit", "fine_finish", "seat", "datum", "body"]
        colw = (W - 2 * M - 6) / 2
        for i, k in enumerate(keys):
            cc = palette.colors[k]
            x = M + 3 + (i % 2) * colw
            yy = legend_top + 9 + (i // 2) * 4.6
            rgb = tuple(v / 255 for v in cc.rgb)
            sh.fill([(x, yy - 3), (x + 6, yy - 3), (x + 6, yy + 0.5), (x, yy + 0.5)], layer="LEGEND", color=rgb)
            label = f"{cc.name}: {cc.meaning}"
            while sh.measure(label, 2.5) > colw - 10 and len(label) > 6:
                label = label[:-2] + "…"
            sh.text(label, x + 8, yy, 2.5)
    out_pdf.parent.mkdir(parents=True, exist_ok=True)
    return sh.to_pdf(out_pdf, {"title": f"{title} - setup sheet", "subject": "CNC setup sheet (PDF-to-STEP v1.5)"})
