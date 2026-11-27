"""3D revision diff (SPEC_V2_0_MULTISIDE_FIXTURING.md §3.3): exact OCCT booleans + order-independent PartSpec diff.

Geometry: both revisions are placed on the canonical datum (stock min corner X/Y = 0, top face Z = 0) and compared
with exact booleans: added = new \\ old (emerald), removed = old \\ new (red), unchanged = old ∩ new (gray, 30 %).
Fragments below 0.05 mm³ are numerical noise and dropped. Never a mesh-distance heuristic.

Semantics: both specs are converted to the v2.0 face frames, features of the same type on the same face are paired
by nearest global anchor point (greedy, order independent), and every change is classified:
BREAKING_CAM (stock / material / tighter tolerance / new machining direction or tilted plane / tighter general
tolerance), GEOMETRY_CHANGE (sizes, depths, positions, added / removed features), METADATA_ONLY (title-block data).
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from pathlib import Path
from typing import Any

import numpy as np

from app.schemas.frames import frames
from app.schemas.part_spec import (
    AngledFaceCut,
    Complex3D,
    PartSpec,
    PocketPolygon,
    Slot,
    StepCut,
    to_v2_coordinates,
)
from app.schemas.revision import CATEGORY_RANK, DiffVolumes, FeatureDiffItem, FieldChange, RevisionDiffReport

NOISE_MM3 = 0.05
PAIR_MAX_MM = 25.0
POS_TOL = 0.01
EMERALD, SIGNAL_RED, NEUTRAL = (30, 180, 90), (230, 30, 30), (185, 190, 196)
IGNORE = {"id", "confidence", "source_note"}
POSITION_KEYS = {"center_x", "center_y", "start_x", "start_y", "end_x", "end_y", "x_min", "x_max", "y_min", "y_max",
                 "points_2d", "extra_positions"}


# --------------------------------------------------------------------------- lineage keys


def norm_part_number(pn: str) -> str:
    return re.sub(r"\s+", "", pn.strip().upper())


def norm_revision(rev: str) -> str:
    r = rev.strip().upper()
    r = re.sub(r"^(REV(ISION|ISIE)?|IND(EX)?)\.?\s*[:\-]?\s*", "", r)
    return r or rev.strip().upper()


def revision_key(rev: str | None) -> tuple:
    """Sort key: numbers numerically, letters A < B < ... < Z < AA; unknown last."""
    if not rev:
        return (2, 0, "")
    if rev.isdigit():
        return (0, int(rev), "")
    if rev.isalpha():
        n = 0
        for ch in rev:
            n = n * 26 + (ord(ch) - 64)
        return (1, n, "")
    return (2, 0, rev)


# --------------------------------------------------------------------------- semantic diff


def _tight(tol: str | None) -> bool:
    from app.services.step_xcaf_exporter import CamPalette, is_tight_fit

    pal = CamPalette.model_validate_json(
        (Path(__file__).resolve().parent.parent / "defaults" / "cam_colors.json").read_text(encoding="utf-8"))
    return is_tight_fit(tol, pal)


def _gen_tol_rank(g: str | None) -> int:
    m = re.search(r"2768\s*-?\s*([fmcv])", g or "", re.IGNORECASE)
    return {"f": 0, "m": 1, "c": 2, "v": 3}.get(m.group(1).lower(), 9) if m else 9


def _anchor(spec: PartSpec, f, frs) -> np.ndarray | None:
    fr = frs.get(f.face)
    if fr is None or isinstance(f, Complex3D):
        if isinstance(f, Complex3D) and f.bbox_min and f.bbox_max:
            return (np.array(f.bbox_min) + np.array(f.bbox_max)) / 2
        return None
    if isinstance(f, AngledFaceCut):
        return fr.origin
    if isinstance(f, Slot):
        return fr.point((f.start_x + f.end_x) / 2, (f.start_y + f.end_y) / 2)
    if isinstance(f, StepCut):
        return fr.point((f.x_min + f.x_max) / 2, (f.y_min + f.y_max) / 2)
    if isinstance(f, PocketPolygon):
        xs, ys = zip(*f.points_2d)
        return fr.point(sum(xs) / len(xs), sum(ys) / len(ys))
    x, y = f.positions()[0]
    return fr.point(x, y)


def _walk(a: Any, b: Any, path: str, out: list[FieldChange]) -> None:
    if isinstance(a, dict) and isinstance(b, dict):
        for k in sorted(set(a) | set(b)):
            if k not in IGNORE:
                _walk(a.get(k), b.get(k), f"{path}.{k}" if path else k, out)
    elif isinstance(a, (int, float)) and isinstance(b, (int, float)) and not isinstance(a, bool):
        if abs(float(a) - float(b)) > 1e-6:
            out.append(FieldChange(path=path, before=a, after=b))
    elif a != b:
        out.append(FieldChange(path=path, before=a, after=b))


def _pair(old: list, new: list, anchors_o: dict, anchors_n: dict) -> tuple[list[tuple], list, list]:
    cand = []
    for i, fo in enumerate(old):
        for j, fn in enumerate(new):
            ao, an = anchors_o.get(fo.id), anchors_n.get(fn.id)
            if ao is None or an is None:
                d = 0.0 if fo.id == fn.id else math.inf
            else:
                d = float(np.linalg.norm(ao - an)) + (0.0 if fo.id == fn.id else 1e-6)
            if d <= PAIR_MAX_MM:
                cand.append((d, i, j))
    used_o, used_n, pairs = set(), set(), []
    for d, i, j in sorted(cand):
        if i not in used_o and j not in used_n:
            used_o.add(i)
            used_n.add(j)
            pairs.append((old[i], new[j], d))
    return pairs, [f for i, f in enumerate(old) if i not in used_o], [f for j, f in enumerate(new) if j not in used_n]


def semantic_diff(old_spec: PartSpec, new_spec: PartSpec) -> list[FeatureDiffItem]:
    old, new = to_v2_coordinates(old_spec), to_v2_coordinates(new_spec)
    fo, fn = frames(old), frames(new)
    items: list[FeatureDiffItem] = []

    ch: list[FieldChange] = []
    _walk(old.stock.model_dump(), new.stock.model_dump(), "stock", ch)
    if ch:
        items.append(FeatureDiffItem(category="BREAKING_CAM", change="changed", scope="stock", fields=ch,
                                     message="Stockmaten gewijzigd: ander ruw materiaal en nieuwe opspanning."))
    ch = []
    _walk(old.outer_profile.model_dump(), new.outer_profile.model_dump(), "outer_profile", ch)
    if ch:
        items.append(FeatureDiffItem(category="GEOMETRY_CHANGE", change="changed", scope="outer_profile", fields=ch,
                                     message="Buitencontour gewijzigd."))
    if (old.material or "") != (new.material or ""):
        items.append(FeatureDiffItem(category="BREAKING_CAM", change="changed", scope="material",
                                     fields=[FieldChange(path="material", before=old.material, after=new.material)],
                                     message=f"Materiaal {old.material} -> {new.material}: snijdata en gereedschap."))
    if (old.general_tolerance or "") != (new.general_tolerance or ""):
        tighter = _gen_tol_rank(new.general_tolerance) < _gen_tol_rank(old.general_tolerance)
        items.append(FeatureDiffItem(
            category="BREAKING_CAM" if tighter else "METADATA_ONLY", change="changed", scope="general_tolerance",
            fields=[FieldChange(path="general_tolerance", before=old.general_tolerance, after=new.general_tolerance)],
            message=f"Algemene tolerantie {old.general_tolerance} -> {new.general_tolerance}"
                    + (" (nauwer)." if tighter else ".")))

    po, pn = {p.id: p for p in old.tilted_planes}, {p.id: p for p in new.tilted_planes}
    for pid in sorted(set(po) | set(pn)):
        if pid not in po or pid not in pn:
            items.append(FeatureDiffItem(category="BREAKING_CAM", change="added" if pid in pn else "removed",
                                         scope="tilted_plane", face=f"plane:{pid}",
                                         message=f"Schuin werkvlak {pid} {'toegevoegd' if pid in pn else 'vervallen'}."))
        else:
            ch = []
            _walk(po[pid].model_dump(), pn[pid].model_dump(), f"tilted_planes.{pid}", ch)
            if ch:
                items.append(FeatureDiffItem(category="BREAKING_CAM", change="modified", scope="tilted_plane",
                                             face=f"plane:{pid}", fields=ch, message=f"Schuin werkvlak {pid} gewijzigd."))

    anc_o = {f.id: _anchor(old, f, fo) for f in old.features}
    anc_n = {f.id: _anchor(new, f, fn) for f in new.features}
    normals_old = [fo[f.face].n for f in old.features if f.face in fo] + [np.array([0.0, 0.0, 1.0])]
    groups = sorted({(f.type, f.face) for f in old.features} | {(f.type, f.face) for f in new.features})
    for ftype, face in groups:
        a = [f for f in old.features if (f.type, f.face) == (ftype, face)]
        b = [f for f in new.features if (f.type, f.face) == (ftype, face)]
        pairs, gone, added = _pair(a, b, anc_o, anc_n)
        for f_old, f_new, d in pairs:
            ch = []
            _walk(f_old.model_dump(mode="json"), f_new.model_dump(mode="json"), "", ch)
            if not ch:
                continue
            moved = any(c.path.split(".")[0] in POSITION_KEYS for c in ch)
            told, tnew = getattr(f_old, "tolerance", None), getattr(f_new, "tolerance", None)
            tight_added = _tight(tnew) and not _tight(told)
            cat = "BREAKING_CAM" if tight_added else "GEOMETRY_CHANGE"
            what = ", ".join(f"{c.path} {c.before} -> {c.after}" for c in ch[:4]) + (" ..." if len(ch) > 4 else "")
            items.append(FeatureDiffItem(
                category=cat, change="moved" if moved and len(ch) == sum(c.path.split(".")[0] in POSITION_KEYS for c in ch)
                else "modified", scope="feature", feature_type=ftype, id_old=f_old.id, id_new=f_new.id, face=face,
                moved_mm=round(d, 4) if moved else None, fields=ch,
                message=f"{f_new.id}: {what}" + (f" (nieuwe nauwe tolerantie {tnew}: ruimen/kotteren)" if tight_added else "")))
        for f in gone:
            items.append(FeatureDiffItem(category="GEOMETRY_CHANGE", change="removed", scope="feature", feature_type=ftype,
                                         id_old=f.id, face=face, message=f"{f.id} ({ftype}) vervallen."))
        for f in added:
            n = fn[f.face].n if f.face in fn else None
            new_dir = n is not None and not any(float(n @ m) > 0.99999 for m in normals_old)
            tight = _tight(getattr(f, "tolerance", None))
            cat = "BREAKING_CAM" if new_dir or tight else "GEOMETRY_CHANGE"
            why = " (nieuwe bewerkingsrichting: extra opspanning)" if new_dir else " (nauwe tolerantie)" if tight else ""
            items.append(FeatureDiffItem(category=cat, change="added", scope="feature", feature_type=ftype, id_new=f.id,
                                         face=face, message=f"{f.id} ({ftype}) toegevoegd{why}."))

    eo, en = {e.id: e for e in old.edge_treatments}, {e.id: e for e in new.edge_treatments}
    for eid in sorted(set(eo) | set(en)):
        ch = []
        _walk(eo[eid].model_dump() if eid in eo else {}, en[eid].model_dump() if eid in en else {}, f"edge_treatments.{eid}", ch)
        if ch:
            items.append(FeatureDiffItem(category="GEOMETRY_CHANGE",
                                         change="added" if eid not in eo else "removed" if eid not in en else "modified",
                                         scope="edge_treatment", id_old=eid if eid in eo else None,
                                         id_new=eid if eid in en else None, fields=ch, message=f"Kantbewerking {eid} gewijzigd."))

    ch = []
    for key in ("part_name", "part_number", "revision"):
        _walk(getattr(old, key), getattr(new, key), key, ch)
    _walk(old.drawing_metadata.model_dump(mode="json"), new.drawing_metadata.model_dump(mode="json"), "drawing_metadata", ch)
    if ch:
        items.append(FeatureDiffItem(category="METADATA_ONLY", change="changed", scope="metadata", fields=ch,
                                     message="Titelblok / tekeningdata gewijzigd: " + ", ".join(c.path for c in ch[:5])))
    items.sort(key=lambda i: -CATEGORY_RANK[i.category])
    return items


# --------------------------------------------------------------------------- B-Rep diff


def _solids(r) -> list:
    if r is None:
        return []
    items = r if isinstance(r, list) else [r]
    return [s for it in items for s in it.solids()]


def canonical_shift(spec: PartSpec) -> tuple[float, float, float]:
    """Translation onto the canonical datum: stock min corner X/Y = 0, top face Z = 0."""
    (x0, y0, _), (_, _, z1) = spec.stock_bbox()
    return (-x0, -y0, -z1)


def boolean_diff(old_shape, new_shape) -> tuple[list, list, list]:
    added = [s for s in _solids(new_shape.cut(old_shape)) if s.volume >= NOISE_MM3]
    removed = [s for s in _solids(old_shape.cut(new_shape)) if s.volume >= NOISE_MM3]
    unchanged = [s for s in _solids(old_shape.intersect(new_shape)) if s.volume >= NOISE_MM3]
    return added, removed, unchanged


def diff_id(job_old: str, v_old: int, job_new: str, v_new: int) -> str:
    return hashlib.sha1(f"{job_old}:{v_old}:{job_new}:{v_new}".encode()).hexdigest()[:16]


def run_revision_diff(old_spec: PartSpec, new_spec: PartSpec, old_shape, new_shape, out_dir: Path,
                      meta: dict[str, Any]) -> RevisionDiffReport:
    from build123d import Pos

    from app.services.glb_scene import Layer, export_layers_glb

    old_shape = Pos(*canonical_shift(old_spec)) * old_shape
    new_shape = Pos(*canonical_shift(new_spec)) * new_shape
    added, removed, unchanged = boolean_diff(old_shape, new_shape)
    vol = lambda xs: round(sum(float(s.volume) for s in xs), 4)  # noqa: E731
    volumes = DiffVolumes(old_mm3=round(float(old_shape.volume), 4), new_mm3=round(float(new_shape.volume), 4),
                          added_mm3=vol(added), removed_mm3=vol(removed), unchanged_mm3=vol(unchanged),
                          added_bodies=len(added), removed_bodies=len(removed))
    items = semantic_diff(old_spec, new_spec)
    counts = {k: sum(1 for i in items if i.category == k) for k in CATEGORY_RANK}
    overall = max(items, key=lambda i: CATEGORY_RANK[i.category]).category if items else (
        "GEOMETRY_CHANGE" if added or removed else "IDENTICAL")
    report = RevisionDiffReport(**meta, overall=overall, counts=counts, volumes=volumes, items=items)
    out_dir.mkdir(parents=True, exist_ok=True)
    export_layers_glb([Layer("Unchanged", unchanged, NEUTRAL, 0.30), Layer("Added", added, EMERALD),
                       Layer("Removed", removed, SIGNAL_RED)], out_dir / "revision_diff.glb")
    report.artifacts = {"revision_diff.glb": "revision_diff.glb", "revision_diff_report.pdf": "revision_diff_report.pdf",
                        "report.json": "report.json"}
    write_report_pdf(report, out_dir / "revision_diff_report.pdf")
    (out_dir / "report.json").write_text(report.model_dump_json(indent=1), encoding="utf-8")
    return report


CAT_RGB = {"BREAKING_CAM": (0.9, 0.12, 0.12), "GEOMETRY_CHANGE": (0.85, 0.55, 0.0), "METADATA_ONLY": (0.35, 0.4, 0.48),
           "IDENTICAL": (0.12, 0.6, 0.3)}


def write_report_pdf(report: RevisionDiffReport, path: Path) -> Path:
    """1-page A4 summary (PyMuPDF, Helvetica: Latin-1 covers Ø, µ, ³)."""
    import pymupdf

    doc = pymupdf.open()
    page = doc.new_page(width=595, height=842)
    y = 50

    def text(s: str, x: float = 40, size: float = 10, color=(0, 0, 0), bold: bool = False) -> None:
        nonlocal y
        page.insert_text((x, y), s, fontsize=size, fontname="hebo" if bold else "helv", color=color)

    text(f"Revisievergelijking {report.part_number or ''}", size=16, bold=True)
    y += 22
    text(f"Rev {report.revision_old or '?'} (job {report.job_old_id} v{report.version_old})  ->  "
         f"Rev {report.revision_new or '?'} (job {report.job_new_id} v{report.version_new})", size=10)
    y += 24
    text(report.overall.replace("_", " "), size=14, bold=True, color=CAT_RGB[report.overall])
    y += 18
    text("  ".join(f"{k.replace('_', ' ')}: {v}" for k, v in report.counts.items()), size=9, color=(0.3, 0.3, 0.3))
    y += 24
    if report.volumes:
        v = report.volumes
        for label, val, rgb in (("Toegevoegd materiaal (groen)", v.added_mm3, EMERALD),
                                ("Verwijderd materiaal (rood)", v.removed_mm3, SIGNAL_RED),
                                ("Ongewijzigd", v.unchanged_mm3, NEUTRAL)):
            page.draw_rect(pymupdf.Rect(40, y - 8, 50, y + 2), color=None, fill=[c / 255 for c in rgb])
            text(f"{label}: {val:,.1f} mm³".replace(",", " "), x=58)
            y += 15
        text(f"Volume oud {v.old_mm3:,.1f} mm³  ->  nieuw {v.new_mm3:,.1f} mm³".replace(",", " "), size=9,
             color=(0.3, 0.3, 0.3))
        y += 26
    text("Wijzigingen", size=12, bold=True)
    y += 16
    shown = 0
    for it in report.items:
        if y > 800:
            break
        text(it.category.replace("_", " "), size=8, bold=True, color=CAT_RGB[it.category])
        msg = it.message
        lines = [msg[i:i + 95] for i in range(0, len(msg), 95)] or [""]
        for k, ln in enumerate(lines[:3]):
            text(ln, x=150, size=8.5)
            y += 12
        y += 3
        shown += 1
    if shown < len(report.items):
        text(f"+ {len(report.items) - shown} meer (zie report.json)", size=8, color=(0.4, 0.4, 0.4))
    path.parent.mkdir(parents=True, exist_ok=True)
    doc.save(path)
    doc.close()
    return path


def cad_revision_diff(old_dir: str, v_old: int, new_dir: str, v_new: int, out_dir: str, meta_json: str) -> dict:
    """Process-pool entry (POST /api/revisions/diff)."""
    from build123d import import_step

    def load(d: str, v: int):
        p = Path(d)
        spec = PartSpec.model_validate_json((p / "specs" / f"part_spec_v{v}.json").read_text(encoding="utf-8"))
        shape = import_step(str(p / "versions" / f"v{v}" / "model.step"))
        solids = shape.solids()
        return spec, (solids[0] if len(solids) == 1 else shape)

    so, ao = load(old_dir, v_old)
    sn, an = load(new_dir, v_new)
    rep = run_revision_diff(so, sn, ao, an, Path(out_dir), json.loads(meta_json))
    return json.loads(rep.model_dump_json())
