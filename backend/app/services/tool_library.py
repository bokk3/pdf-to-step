"""Workshop tool library + DFM v2 tool checks (SPEC_V1_5_CAM_SHOPFLOOR.md §2.1). Deterministic, no LLM.

* ``load_tools`` / ``save_tools``: data/config/tools.json (validated, backed up on save)
* ``import_tools``: Fusion 360 tool library (.json or the zipped .tools), CSV, or our own tools.json
* ``material_group``: free-text PartSpec.material -> aluminum | steel | stainless | plastic | brass_copper | unknown
* ``tool_checks``: per feature, the in-magazine tool that can make it (pocket corner + reach, drills, taps, reamers,
  slots, counterbores, countersinks, edge breaks) or a concrete fix based on the tools that do exist.
"""

from __future__ import annotations

import csv
import io
import json
import math
import re
import zipfile
from dataclasses import dataclass, field

from app.config import Settings
from app.schemas.part_spec import (
    BossCircle,
    BossRect,
    Complex3D,
    DFMIssue,
    Hole,
    PartSpec,
    PocketCircle,
    PocketPolygon,
    PocketRect,
    Severity,
    Slot,
    StepCut,
    parse_metric_thread,
)
from app.schemas.tooling import (
    CHAMFER_TOOLS,
    ENDMILLS,
    ToolAssignment,
    ToolCheck,
    ToolLibrary,
    ToolSpec,
)
from app.services.shop_config import load_config, save_config

TOOLS_FILE = "tools.json"
DIA_TOL = 0.05  # drill / reamer diameter match (mm)


def load_tools(s: Settings) -> ToolLibrary:
    return load_config(s, TOOLS_FILE, ToolLibrary)


def save_tools(s: Settings, lib: ToolLibrary) -> None:
    save_config(s, TOOLS_FILE, lib)


# --------------------------------------------------------------------------- #
# Material groups
# --------------------------------------------------------------------------- #

_GROUP_PATTERNS: list[tuple[str, re.Pattern]] = [
    # order matters: stainless before steel ("RVS 304", "1.4301"), brass before aluminium ("CuZn...Al")
    ("stainless", re.compile(r"rvs|inox|stainless|roestvast|\b1\.4\d{3}\b|\baisi\s*3\d\d|\b30[34]l?\b|\b31[06]l?\b"
                             r"|x\d+cr", re.I)),
    ("brass_copper", re.compile(r"messing|brass|\bcuzn|\bcusn|\bcu-?etp|\bcu\b|koper|copper|brons|bronze|\bcw\d{3}"
                                r"|\bms\s?5[78]\b|\bcc\d{3}", re.I)),
    ("plastic", re.compile(r"\bpom|delrin|acetal|\bpa\s?6|\bpa\s?12|nylon|polyamid|\bpe-?(?:hd|uhmw|300|500|1000)?\b"
                           r"|\bhdpe|uhmw|\bpp\b|\bpvc|\bpeek|\bptfe|teflon|\bpmma|plexi|\bpc\b|polycarbonat"
                           r"|kunststof|plastic|\babs\b|\bpet\b|ertalyte|\bpei\b|ultem", re.I)),
    ("aluminum", re.compile(r"alu|almg|alzn|alcu|\ben\s?aw\b|\baw-?\d{4}|\b[1-8]0\d\d(?:-t\d+)?\b|\b[2567]\d{3}-t\d", re.I)),
    ("steel", re.compile(r"staal|steel|\bs\d{3}(?:jr|j0|j2|n)?\b|\bst\s?[3-7]\d\b|\bc[1-6]\d(?:e|r)?\b|42crmo|\d\dcrmo"
                         r"|\b1\.[0-3]\d{3}\b|\b11smn|\b9smn|\b1\.07\d\d|toolsteel|gereedschapsstaal|\b1\.2\d{3}\b"
                         r"|hardox|\bdc0\d|\bc45|\bck45", re.I)),
]


def material_group(material: str | None) -> str:
    """Map free text ("AlMgSi1 (6082-T6)", "Staal S355", "RVS 304", "POM-C") to a canonical group."""
    text = (material or "").strip()
    if not text:
        return "unknown"
    for group, pat in _GROUP_PATTERNS:
        if pat.search(text):
            return group
    return "unknown"


# --------------------------------------------------------------------------- #
# Importers (Fusion 360 JSON / .tools zip, CSV, native tools.json)
# --------------------------------------------------------------------------- #

FUSION_TYPES = {
    "flat end mill": "flat_endmill", "bull nose end mill": "bull_endmill", "ball end mill": "ball_endmill",
    "slot mill": "slot_mill", "face mill": "face_mill", "chamfer mill": "chamfer_mill", "radius mill": "corner_rounding",
    "spot drill": "spot_drill", "center drill": "center_drill", "drill": "drill", "tap right hand": "tap",
    "tap left hand": "tap", "thread mill": "thread_mill", "reamer": "reamer", "boring bar": "boring_bar",
    "counter bore": "counterbore", "counter sink": "countersink", "dovetail mill": "other", "lollipop mill": "other",
    "tapered mill": "other", "form mill": "other", "engrave": "other",
}
FUSION_SKIP = {"holder", "probe", "shaft"}
TEXT_TYPES = {  # CSV / free text (EN + NL)
    "endmill": "flat_endmill", "end mill": "flat_endmill", "flat": "flat_endmill", "vingerfrees": "flat_endmill",
    "schachtfrees": "flat_endmill", "frees": "flat_endmill", "torus": "bull_endmill", "bull": "bull_endmill",
    "radiusfrees": "bull_endmill", "ball": "ball_endmill", "bolkop": "ball_endmill", "kogelfrees": "ball_endmill",
    "spiebaanfrees": "slot_mill", "vlakfrees": "face_mill", "planfrees": "face_mill", "afschuinfrees": "chamfer_mill",
    "chamfer": "chamfer_mill", "kantenfrees": "chamfer_mill", "afrondfrees": "corner_rounding",
    "centerboor": "spot_drill", "nc-boor": "spot_drill", "spot": "spot_drill", "boor": "drill", "spiraalboor": "drill",
    "tap": "tap", "draadtap": "tap", "machinetap": "tap", "draadfrees": "thread_mill", "ruimer": "reamer",
    "kotterbaar": "boring_bar", "kotter": "boring_bar", "verzinkboor": "countersink", "kegelverzinker": "countersink",
    "vlakverzinker": "counterbore",
}
CSV_ALIASES = {
    "id": ("id", "tool", "tool_id", "tool number", "number", "nr", "t", "pocket", "nummer"),
    "name": ("name", "description", "omschrijving", "naam", "comment"),
    "tool_type": ("tool_type", "type", "soort", "kind"),
    "diameter_mm": ("diameter_mm", "diameter", "dia", "d", "dc", "ø", "diameter (mm)"),
    "flute_length_mm": ("flute_length_mm", "flute_length", "flute length", "lcf", "snijlengte", "flute", "reach"),
    "overall_length_mm": ("overall_length_mm", "overall_length", "overall length", "oal", "lengte", "length"),
    "corner_radius_mm": ("corner_radius_mm", "corner_radius", "corner radius", "re", "hoekradius"),
    "flutes": ("flutes", "nof", "z", "tanden", "number of flutes"),
    "materials": ("materials", "material", "materiaal", "materialen", "workpiece"),
    "thread_spec": ("thread_spec", "thread", "draad"),
    "tolerance_class": ("tolerance_class", "tolerance", "tolerantie"),
    "in_magazine": ("in_magazine", "magazine", "magazijn", "loaded"),
}


@dataclass
class ImportResult:
    tools: list[ToolSpec]
    skipped: list[str] = field(default_factory=list)
    source: str = ""


def _num(v) -> float | None:
    if v is None or v == "":
        return None
    try:
        return float(str(v).strip().replace(",", ".").replace("mm", ""))
    except ValueError:
        return None


def _tool_type(text: str) -> str | None:
    t = text.strip().lower().replace("_", " ")
    if t.replace(" ", "_") in {"flat_endmill", "bull_endmill", "ball_endmill", "slot_mill", "face_mill", "chamfer_mill",
                               "corner_rounding", "spot_drill", "center_drill", "drill", "tap", "thread_mill", "reamer",
                               "boring_bar", "counterbore", "countersink", "other"}:
        return t.replace(" ", "_")
    if t in FUSION_TYPES:
        return FUSION_TYPES[t]
    for key in sorted(TEXT_TYPES, key=len, reverse=True):
        if key in t:
            return TEXT_TYPES[key]
    return None


def _thread_label(nominal: float, pitch: float | None) -> str:
    coarse = parse_metric_thread(f"M{nominal:g}")
    if pitch is None or (coarse and abs(coarse[1] - pitch) < 1e-6):
        return f"M{nominal:g}"
    return f"M{nominal:g}x{pitch:g}"


def _materials(text) -> list[str]:
    if isinstance(text, list):
        items = text
    else:
        items = re.split(r"[;|/,+]", str(text or ""))
    out: list[str] = []
    for it in items:
        it = str(it).strip()
        if not it:
            continue
        g = it.lower() if it.lower() in ("aluminum", "steel", "stainless", "plastic", "brass_copper") else material_group(it)
        if g != "unknown" and g not in out:
            out.append(g)
    return out


def _fusion_tools(entries: list[dict]) -> ImportResult:
    res = ImportResult([], source="fusion360")
    for n, e in enumerate(entries):
        ftype = str(e.get("type", "")).lower()
        if ftype in FUSION_SKIP:
            continue
        tool_type = FUSION_TYPES.get(ftype) or _tool_type(ftype) or "other"
        g = e.get("geometry") or {}
        scale = 25.4 if str(e.get("unit", "millimeters")).lower().startswith("inch") else 1.0
        dc = _num(g.get("DC"))
        if not dc or dc <= 0:
            res.skipped.append(f"#{n} {e.get('description', ftype)}: no diameter")
            continue
        lcf = _num(g.get("LCF")) or _num(g.get("LB")) or _num(g.get("shoulder-length")) or dc * 3
        oal = _num(g.get("OAL"))
        number = (e.get("post-process") or {}).get("number")
        name = str(e.get("description") or " ".join(str(x) for x in (e.get("vendor"), e.get("product-id")) if x)
                   or ftype).strip()
        thread = None
        if tool_type in ("tap", "thread_mill"):
            tp = _num(g.get("TP"))
            thread = _thread_label(round(dc * scale, 3), round(tp * scale, 3) if tp else None) if tool_type == "tap" \
                else (f"M{dc * scale:g}x{tp * scale:g}" if tp else None)
        try:
            res.tools.append(ToolSpec(
                id=f"T{int(number):02d}" if number not in (None, "") else "",
                name=name[:120], tool_type=tool_type, diameter_mm=round(dc * scale, 4),
                flute_length_mm=round(lcf * scale, 3), overall_length_mm=round(oal * scale, 3) if oal else None,
                corner_radius_mm=round((_num(g.get("RE")) or 0.0) * scale, 4), flutes=int(_num(g.get("NOF")) or 2),
                thread_spec=thread, notes=f"Fusion 360 import ({e.get('vendor') or 'no vendor'})"[:200]))
        except Exception as exc:  # noqa: BLE001
            res.skipped.append(f"#{n} {name}: {exc}")
    return res


def _csv_tools(text: str) -> ImportResult:
    res = ImportResult([], source="csv")
    sample = text[:4096]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=";,\t")
    except csv.Error:
        dialect = csv.excel
    rows = list(csv.DictReader(io.StringIO(text), dialect=dialect))
    for n, row in enumerate(rows, start=2):
        norm = {str(k or "").strip().lower(): (v or "").strip() for k, v in row.items()}
        get = lambda key: next((norm[a] for a in CSV_ALIASES[key] if norm.get(a)), "")  # noqa: E731
        ttype = _tool_type(get("tool_type") or get("name")) or "other"
        d, lcf = _num(get("diameter_mm")), _num(get("flute_length_mm"))
        if not d or not lcf:
            res.skipped.append(f"line {n}: diameter / flute length missing")
            continue
        tid = get("id")
        if tid and re.fullmatch(r"\d+", tid):
            tid = f"T{int(tid):02d}"
        mag = get("in_magazine").lower()
        try:
            res.tools.append(ToolSpec(
                id=tid, name=get("name")[:120], tool_type=ttype, diameter_mm=d, flute_length_mm=lcf,
                overall_length_mm=_num(get("overall_length_mm")), corner_radius_mm=_num(get("corner_radius_mm")) or 0.0,
                flutes=int(_num(get("flutes")) or 2), materials=_materials(get("materials")),
                thread_spec=get("thread_spec") or None, tolerance_class=get("tolerance_class") or None,
                in_magazine=mag not in ("0", "false", "nee", "no", "n")))
        except Exception as exc:  # noqa: BLE001
            res.skipped.append(f"line {n}: {exc}")
    return res


def import_tools(data: bytes, filename: str = "") -> ImportResult:
    """Parse a Fusion 360 library (.json / zipped .tools), a CSV export or a native tools.json."""
    if data[:2] == b"PK":  # Fusion .tools = zip with one JSON document
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            member = next((n for n in zf.namelist() if n.lower().endswith(".json")), None)
            if member is None:
                raise ValueError("zip archive without a .json tool library")
            data = zf.read(member)
    text = data.decode("utf-8-sig", errors="replace")
    stripped = text.lstrip()
    if filename.lower().endswith(".csv") or not stripped.startswith(("{", "[")):
        return _csv_tools(text)
    doc = json.loads(text)
    if isinstance(doc, dict) and "tools" in doc and "data" not in doc:
        return ImportResult(ToolLibrary.model_validate(doc).tools, source="tools.json")
    entries = doc.get("data") if isinstance(doc, dict) else doc
    if not isinstance(entries, list):
        raise ValueError("unrecognised tool library (expected Fusion 360 'data' list, CSV or tools.json)")
    return _fusion_tools(entries)


def merge_tools(lib: ToolLibrary, new: list[ToolSpec], replace: bool = False) -> tuple[ToolLibrary, int, int]:
    """Add imported tools (same id replaces the existing tool). Tools without id get the next free T-number."""
    tools = [] if replace else [t.model_copy() for t in lib.tools]
    index = {t.id: i for i, t in enumerate(tools)}
    used = set(index)
    added = updated = 0

    def next_id() -> str:
        k = 1
        while f"T{k:02d}" in used:
            k += 1
        return f"T{k:02d}"

    for t in new:
        if not t.id:
            t = t.model_copy(update={"id": next_id()})
        if t.id in index:
            tools[index[t.id]] = t
            updated += 1
        else:
            index[t.id] = len(tools)
            tools.append(t)
            added += 1
        used.add(t.id)
    return ToolLibrary(tools=tools), added, updated


# --------------------------------------------------------------------------- #
# DFM v2 tool checks
# --------------------------------------------------------------------------- #

def _ceil_step(v: float, step: float = 0.5) -> float:
    return math.ceil(v / step - 1e-9) * step


def _fmt(v: float) -> str:
    return f"{v:g}"


class _Checker:
    def __init__(self, spec: PartSpec, lib: ToolLibrary, group: str):
        self.spec = spec
        self.group = group
        self.avail = [t for t in lib.tools if t.in_magazine and t.cuts(group)]
        self.shelf = [t for t in lib.tools if not t.in_magazine and t.cuts(group)]
        self.checks: list[ToolCheck] = []
        self.assign: list[ToolAssignment] = []

    # -- helpers -----------------------------------------------------------------
    def of(self, types, pool=None) -> list[ToolSpec]:
        return [t for t in (self.avail if pool is None else pool) if t.tool_type in types]

    def ok(self, fid: str, op: str, tool: ToolSpec, msg: str, status: str = "ok") -> None:
        self.checks.append(ToolCheck(feature_id=fid, operation=op, status=status, assigned_tool_id=tool.id,
                                     assigned_tool_name=tool.name, message=msg))
        self.assign.append(ToolAssignment(feature_id=fid, operation=op, tool_id=tool.id, tool_name=tool.name,
                                          tool_type=tool.tool_type, diameter_mm=tool.diameter_mm,
                                          flute_length_mm=tool.flute_length_mm))

    def warn(self, fid: str, op: str, msg: str, fix: str | None, shelf_hint: list[ToolSpec] | None = None) -> None:
        if shelf_hint:
            t = shelf_hint[0]
            msg += f" {t.label()} zou passen maar zit niet in het magazijn."
        self.checks.append(ToolCheck(feature_id=fid, operation=op, status="warning", message=msg, suggested_fix=fix))

    def depth(self, f) -> tuple[float, bool]:
        (x0, y0, z0), (x1, y1, z1) = self.spec.stock_bbox()
        thick = {"top": z1 - z0, "bottom": z1 - z0, "front": y1 - y0, "back": y1 - y0}.get(f.face, x1 - x0)
        through = bool(getattr(f, "through_all", False))
        d = thick if through else (getattr(f, "depth", None) or thick)
        return d, through

    # -- milling -------------------------------------------------------------------
    def corner_mill(self, fid: str, op: str, radius: float, depth: float, what: str) -> None:
        """Pocket-like cut: endmill with d <= 2R and flute >= depth; largest such tool."""
        mills = self.of(ENDMILLS)
        if not mills:
            self.warn(fid, op, f"{what}: geen vingerfrees in het magazijn.", "Voeg vingerfrezen toe aan de gereedschapsbibliotheek.")
            return
        sharp = radius <= 1e-9
        cands = [t for t in mills if not sharp and t.diameter_mm <= 2 * radius + 1e-9 and t.flute_length_mm >= depth - 1e-9]
        if cands:
            t = max(cands, key=lambda t: (t.diameter_mm, -t.flute_length_mm))
            self.ok(fid, op, t, f"Ø{_fmt(t.diameter_mm)} frees (snijlengte {_fmt(t.flute_length_mm)} mm ≥ diepte "
                                f"{_fmt(depth)} mm, R{_fmt(t.diameter_mm / 2)} ≤ R{_fmt(radius)}).")
            return
        reach = [t for t in mills if t.flute_length_mm >= depth - 1e-9]
        shelf = [t for t in self.of(ENDMILLS, self.shelf)
                 if not sharp and t.diameter_mm <= 2 * radius + 1e-9 and t.flute_length_mm >= depth - 1e-9]
        r_txt = "scherpe hoeken (R0)" if sharp else f"R{_fmt(radius)}"
        if reach:
            best = min(reach, key=lambda t: (t.diameter_mm, t.flute_length_mm))
            new_r = _ceil_step(best.diameter_mm / 2)
            fix = (f"Vergroot corner_radius naar {new_r:.1f} mm voor {best.label()} (Ø{_fmt(best.diameter_mm)}, "
                   f"snijlengte {_fmt(best.flute_length_mm)} mm)")
            corner = [t for t in mills if not sharp and t.diameter_mm <= 2 * radius + 1e-9]
            if corner:
                longest = max(corner, key=lambda t: t.flute_length_mm)
                fix += f", of beperk de diepte tot {_fmt(longest.flute_length_mm)} mm ({longest.label()})"
            fix += "."
            if sharp:
                msg = f"{what} (diepte {_fmt(depth)} mm, {r_txt}): niet freesbaar met een vingerfrees."
            else:
                msg = (f"{what} (diepte {_fmt(depth)} mm, {r_txt}): geen frees ≤ Ø{_fmt(2 * radius)} met snijlengte "
                       f"≥ {_fmt(depth)} mm in het magazijn.")
            self.warn(fid, op, msg, fix, shelf)
        else:
            longest = max(mills, key=lambda t: t.flute_length_mm)
            self.warn(fid, op, f"{what} (diepte {_fmt(depth)} mm): geen enkele frees heeft snijlengte ≥ {_fmt(depth)} mm "
                               f"(langste: {longest.label()}, {_fmt(longest.flute_length_mm)} mm).",
                      f"Langere frees (≥ {_fmt(depth)} mm) toevoegen, of de diepte beperken tot "
                      f"{_fmt(longest.flute_length_mm)} mm.", shelf)

    def slot(self, f: Slot) -> None:
        depth, through = self.depth(f)
        need = depth + (0.5 if through else 0.0)
        mills = self.of(ENDMILLS)
        cands = [t for t in mills if t.diameter_mm <= f.width + 1e-9 and t.flute_length_mm >= need - 1e-9]
        if cands:
            exact = [t for t in cands if abs(t.diameter_mm - f.width) <= DIA_TOL]
            t = min(exact, key=lambda t: t.flute_length_mm) if exact else max(cands, key=lambda t: t.diameter_mm)
            how = "in één baan" if exact else "met contourbanen"
            self.ok(f.id, "slot", t, f"Sleuf {_fmt(f.width)} mm {how} met Ø{_fmt(t.diameter_mm)} "
                                     f"(snijlengte {_fmt(t.flute_length_mm)} mm ≥ {_fmt(need)} mm).")
            return
        reach = [t for t in mills if t.flute_length_mm >= need - 1e-9]
        if reach:
            best = min(reach, key=lambda t: t.diameter_mm)
            self.warn(f.id, "slot", f"Sleuf {_fmt(f.width)} mm (diepte {_fmt(need)} mm): geen frees ≤ Ø{_fmt(f.width)} "
                                    f"met voldoende snijlengte.",
                      f"Verbreed de sleuf naar {_fmt(best.diameter_mm)} mm voor {best.label()}, of voeg een langere "
                      f"Ø{_fmt(f.width)} frees toe.",
                      [t for t in self.of(ENDMILLS, self.shelf) if t.diameter_mm <= f.width and t.flute_length_mm >= need])
        else:
            self.warn(f.id, "slot", f"Sleuf (diepte {_fmt(need)} mm): geen frees met voldoende snijlengte.",
                      f"Langere frees (≥ {_fmt(need)} mm) toevoegen of de diepte beperken.")

    # -- holes -----------------------------------------------------------------------
    def drill(self, fid: str, op: str, dia: float, reach: float, label: str) -> bool:
        drills = self.of({"drill"})
        match = [t for t in drills if abs(t.diameter_mm - dia) <= DIA_TOL]
        long_ok = [t for t in match if t.flute_length_mm >= reach - 1e-9]
        if long_ok:
            t = min(long_ok, key=lambda t: t.flute_length_mm)
            self.ok(fid, op, t, f"{label}: boor Ø{_fmt(t.diameter_mm)} (snijlengte {_fmt(t.flute_length_mm)} mm ≥ "
                                f"{_fmt(reach)} mm).")
            return True
        mills = [t for t in self.of(ENDMILLS) if t.diameter_mm < dia - 1e-9 and t.flute_length_mm >= reach - 1e-9]
        if mills and op == "drill":
            t = max(mills, key=lambda t: t.diameter_mm)
            self.ok(fid, "interpolate", t, f"{label}: geen boor Ø{_fmt(dia)}; circulair/helix interpoleren met "
                                           f"Ø{_fmt(t.diameter_mm)} frees.", status="info")
            return True
        if match:
            t = max(match, key=lambda t: t.flute_length_mm)
            self.warn(fid, op, f"{label}: boor Ø{_fmt(dia)} te kort ({_fmt(t.flute_length_mm)} mm < {_fmt(reach)} mm).",
                      f"Langere boor Ø{_fmt(dia)} (≥ {_fmt(reach)} mm) toevoegen of pikboren van beide zijden.")
            return False
        near = sorted(drills, key=lambda t: abs(t.diameter_mm - dia))[:2]
        near_txt = ", ".join(f"Ø{_fmt(t.diameter_mm)} ({t.id})" for t in near)
        self.warn(fid, op, f"{label}: geen boor Ø{_fmt(dia)} in het magazijn" + (f" (dichtstbij: {near_txt})." if near else "."),
                  f"Boor Ø{_fmt(dia)} toevoegen" + (f" of de maat aanpassen naar Ø{_fmt(near[0].diameter_mm)}" if near else "") + ".",
                  [t for t in self.of({"drill"}, self.shelf) if abs(t.diameter_mm - dia) <= DIA_TOL])
        return False

    def hole(self, f: Hole) -> None:
        self._hole_primary(f)
        self._hole_seats(f)

    def _hole_primary(self, f: Hole) -> None:
        depth, through = self.depth(f)
        d = f.drill_diameter()
        tip = 0.3 * d if through or f.tip_angle_deg else 0.0
        reach = depth + tip + (0.5 if through else 0.0)
        if f.thread:
            t = parse_metric_thread(f.thread)
            self.drill(f.id, "tap_drill", d, reach, f"Kernboring {f.thread}")
            thread_len = f.thread_depth or depth
            if not t:
                self.checks.append(ToolCheck(feature_id=f.id, operation="tap", status="info",
                                             message=f"Niet-metrische draad {f.thread}: gereedschap handmatig kiezen."))
                return
            nominal, pitch = t
            taps = [x for x in self.of({"tap"}) if parse_metric_thread(x.thread_spec) == (nominal, pitch)]
            ok = [x for x in taps if x.flute_length_mm >= thread_len - 1e-9]
            if ok:
                x = min(ok, key=lambda x: x.flute_length_mm)
                self.ok(f.id, "tap", x, f"Tap {f.thread} (bruikbare lengte {_fmt(x.flute_length_mm)} mm ≥ "
                                        f"{_fmt(thread_len)} mm).")
                return
            mills = [x for x in self.of({"thread_mill"}) if (p := parse_metric_thread(x.thread_spec)) and
                     abs(p[1] - pitch) < 1e-6 and x.diameter_mm < d and x.flute_length_mm >= thread_len - 1e-9]
            if mills:
                x = max(mills, key=lambda x: x.diameter_mm)
                self.ok(f.id, "thread_mill", x, f"{f.thread} draadfrezen (spoed {_fmt(pitch)}).")
                return
            if taps:
                x = max(taps, key=lambda x: x.flute_length_mm)
                self.warn(f.id, "tap", f"Tap {f.thread} te kort ({_fmt(x.flute_length_mm)} mm < draadlengte "
                                       f"{_fmt(thread_len)} mm).",
                          f"Draadlengte beperken tot {_fmt(x.flute_length_mm)} mm of een langere tap / draadfrees toevoegen.")
            else:
                self.warn(f.id, "tap", f"Geen tap {f.thread} in het magazijn.",
                          f"Tap {_thread_label(nominal, pitch)} (6H) of een draadfrees met spoed {_fmt(pitch)} toevoegen.",
                          [x for x in self.of({"tap"}, self.shelf) if parse_metric_thread(x.thread_spec) == (nominal, pitch)])
            return

        from app.services.verifier import is_tight

        if is_tight(f.tolerance):
            fit = re.search(r"[A-Za-z]{1,2}\s*\d{1,2}", f.tolerance or "")
            fit_txt = fit.group(0).replace(" ", "").upper() if fit else None
            reamers = [x for x in self.of({"reamer"}) if abs(x.diameter_mm - d) <= DIA_TOL
                       and (not x.tolerance_class or not fit_txt or x.tolerance_class.replace(" ", "").upper() == fit_txt)]
            ok = [x for x in reamers if x.flute_length_mm >= depth - 1e-9]
            if ok:
                x = min(ok, key=lambda x: x.flute_length_mm)
                pre = [t for t in self.of({"drill"}) if d - 0.45 <= t.diameter_mm <= d - 0.1 and t.flute_length_mm >= reach]
                if pre:
                    p = max(pre, key=lambda t: t.diameter_mm)
                    self.ok(f.id, "pre_drill", p, f"Voorboren Ø{_fmt(p.diameter_mm)} voor ruimer Ø{_fmt(d)}.")
                else:
                    self.warn(f.id, "pre_drill", f"Geen voorboor Ø{_fmt(d - 0.3)}…Ø{_fmt(d - 0.15)} voor de ruimer.",
                              f"Boor Ø{_fmt(round(d - 0.2, 1))} toevoegen of voorfrezen.")
                self.ok(f.id, "ream", x, f"Ruimer Ø{_fmt(x.diameter_mm)} {x.tolerance_class or ''} (snijlengte "
                                         f"{_fmt(x.flute_length_mm)} mm ≥ {_fmt(depth)} mm).".replace("  ", " "))
                return
            bars = [x for x in self.of({"boring_bar"}) if x.diameter_mm <= d and x.flute_length_mm >= depth - 1e-9]
            if bars:
                x = max(bars, key=lambda x: x.diameter_mm)
                self.ok(f.id, "bore", x, f"Pasboring {f.tolerance}: kotteren met {x.label()}.", status="info")
                return
            self.warn(f.id, "ream", f"Pasboring Ø{_fmt(d)} {f.tolerance}: geen passende ruimer of kotterbaar in het magazijn.",
                      f"Ruimer Ø{_fmt(d)} {fit_txt or ''} toevoegen, of nafrezen met een finishing-frees en meten.".replace("  ", " "),
                      [x for x in self.of({"reamer"}, self.shelf) if abs(x.diameter_mm - d) <= DIA_TOL])
        else:
            self.drill(f.id, "drill", d, reach, f"Boring Ø{_fmt(d)}")

    def _hole_seats(self, f: Hole) -> None:
        if f.counterbore:
            cb = f.counterbore
            own = [x for x in self.of({"counterbore"}) if abs(x.diameter_mm - cb.diameter) <= DIA_TOL
                   and x.flute_length_mm >= cb.depth]
            mills = [x for x in self.of(ENDMILLS) if x.diameter_mm <= cb.diameter + 1e-9 and x.flute_length_mm >= cb.depth - 1e-9]
            if own or mills:
                x = own[0] if own else max(mills, key=lambda x: x.diameter_mm)
                self.ok(f.id, "counterbore", x, f"Verzinking Ø{_fmt(cb.diameter)} ↧{_fmt(cb.depth)} met Ø{_fmt(x.diameter_mm)}.")
            else:
                self.warn(f.id, "counterbore", f"Verzinking Ø{_fmt(cb.diameter)} ↧{_fmt(cb.depth)}: geen frees ≤ "
                                               f"Ø{_fmt(cb.diameter)} met voldoende snijlengte.",
                          "Vlakverzinker of een kleinere/langere frees toevoegen.")
        if f.countersink:
            cs = f.countersink
            tools = [x for x in self.of(CHAMFER_TOOLS) if x.diameter_mm >= cs.diameter - 1e-9]
            if tools:
                x = min(tools, key=lambda x: x.diameter_mm)
                self.ok(f.id, "countersink", x, f"Kegelverzinking Ø{_fmt(cs.diameter)} x {_fmt(cs.angle_deg)}° met {x.label()}.")
            else:
                self.warn(f.id, "countersink", f"Kegelverzinking Ø{_fmt(cs.diameter)}: geen afschuinfrees / verzinkboor "
                                               f"≥ Ø{_fmt(cs.diameter)} in het magazijn.",
                          f"Afschuinfrees of kegelverzinker ≥ Ø{_fmt(cs.diameter)} ({_fmt(cs.angle_deg)}°) toevoegen.")

    # -- whole part ------------------------------------------------------------------
    def run(self) -> None:
        spec = self.spec
        (x0, y0, z0), (x1, y1, z1) = spec.stock_bbox()
        thick = z1 - z0
        faces = self.of({"face_mill"})
        if faces:
            self.ok("stock", "face", max(faces, key=lambda t: t.diameter_mm), "Bovenvlak (datum Z0) vlakfrezen.")
        mills = self.of(ENDMILLS)
        contour = [t for t in mills if t.flute_length_mm >= thick + 0.5]
        if contour:
            t = max(contour, key=lambda t: t.diameter_mm)
            self.ok("outer_profile", "contour", t, f"Buitencontour (hoogte {_fmt(thick)} mm) met Ø{_fmt(t.diameter_mm)}.")
        elif mills:
            t = max(mills, key=lambda t: t.flute_length_mm)
            self.ok("outer_profile", "contour", t, f"Buitencontour {_fmt(thick)} mm hoger dan de snijlengte "
                                                   f"({_fmt(t.flute_length_mm)} mm): in twee opspanningen.", status="info")
        for f in spec.features:
            if isinstance(f, Hole):
                self.hole(f)
            elif isinstance(f, PocketRect):
                depth, through = self.depth(f)
                self.corner_mill(f.id, "pocket", f.corner_radius, depth + (0.5 if through else 0), f"Pocket {f.id}")
            elif isinstance(f, PocketPolygon):
                depth, through = self.depth(f)
                self.corner_mill(f.id, "pocket", f.corner_radius, depth + (0.5 if through else 0), f"Pocket {f.id}")
            elif isinstance(f, PocketCircle):
                depth, through = self.depth(f)
                self.corner_mill(f.id, "pocket", f.diameter / 2, depth + (0.5 if through else 0), f"Ronde pocket {f.id}")
            elif isinstance(f, Slot):
                self.slot(f)
            elif isinstance(f, StepCut):
                if f.corner_radius > 0:
                    self.corner_mill(f.id, "step", f.corner_radius, f.depth, f"Stap {f.id}")
                else:
                    reach = [t for t in mills if t.flute_length_mm >= f.depth]
                    if reach:
                        t = max(reach, key=lambda t: t.diameter_mm)
                        self.ok(f.id, "step", t, f"Open stap (diepte {_fmt(f.depth)} mm) met Ø{_fmt(t.diameter_mm)}.")
                    else:
                        self.warn(f.id, "step", f"Stap {f.id}: geen frees met snijlengte ≥ {_fmt(f.depth)} mm.",
                                  "Langere frees toevoegen of de stap in twee opspanningen maken.")
            elif isinstance(f, (BossRect, BossCircle)):
                reach = [t for t in mills if t.flute_length_mm >= f.height]
                if reach:
                    t = max(reach, key=lambda t: t.diameter_mm)
                    self.ok(f.id, "boss", t, f"Nok (hoogte {_fmt(f.height)} mm) vrijfrezen met Ø{_fmt(t.diameter_mm)}.")
                else:
                    self.warn(f.id, "boss", f"Nok {f.id}: geen frees met snijlengte ≥ {_fmt(f.height)} mm.",
                              "Langere frees toevoegen of de nok lager maken.")
            elif isinstance(f, Complex3D):
                self.checks.append(ToolCheck(feature_id=f.id, operation="3d", status="info",
                                             message="3D-feature: gereedschap en strategie in CAM kiezen."))
        for et in spec.edge_treatments:
            if et.type == "chamfer":
                tools = [t for t in self.of(CHAMFER_TOOLS) if t.diameter_mm >= 2 * et.size + 1]
                if tools:
                    t = min(tools, key=lambda t: t.diameter_mm)
                    self.ok(et.id, "chamfer", t, f"Afschuining {_fmt(et.size)}x45° met {t.label()}.")
                else:
                    self.warn(et.id, "chamfer", f"Afschuining {_fmt(et.size)}x45°: geen afschuinfrees ≥ "
                                                f"Ø{_fmt(2 * et.size + 1)} in het magazijn.",
                              "Afschuinfrees 90° toevoegen (of de kant manueel breken).")
            else:
                tools = [t for t in self.of({"corner_rounding"}) if abs(t.corner_radius_mm - et.size) <= DIA_TOL]
                if tools:
                    self.ok(et.id, "fillet", tools[0], f"Afronding R{_fmt(et.size)} met {tools[0].label()}.")
                else:
                    self.warn(et.id, "fillet", f"Afronding R{_fmt(et.size)}: geen afrondfrees R{_fmt(et.size)} in het magazijn.",
                              f"Afrondfrees R{_fmt(et.size)} toevoegen, of een afschuining {_fmt(et.size)}x45° tekenen.")


OP_CODES = {"pocket": "TOOL_POCKET", "step": "TOOL_POCKET", "slot": "TOOL_SLOT", "drill": "TOOL_DRILL",
            "tap_drill": "TOOL_DRILL", "pre_drill": "TOOL_DRILL", "tap": "TOOL_TAP", "thread_mill": "TOOL_TAP",
            "ream": "TOOL_REAMER", "bore": "TOOL_REAMER", "counterbore": "TOOL_COUNTERBORE",
            "countersink": "TOOL_COUNTERSINK", "chamfer": "TOOL_EDGE", "fillet": "TOOL_EDGE", "boss": "TOOL_BOSS",
            "interpolate": "TOOL_DRILL", "contour": "TOOL_CONTOUR"}


def tool_checks(spec: PartSpec, lib: ToolLibrary, group: str | None = None
                ) -> tuple[list[ToolCheck], list[ToolAssignment], list[DFMIssue]]:
    """DFM v2: checks, feature->tool assignments and the DFM issues (warnings / info) they imply."""
    group = group or material_group(spec.material)
    c = _Checker(spec, lib, group)
    if not c.avail:
        msg = ("Gereedschapsbibliotheek is leeg" if not lib.tools
               else f"Geen gereedschap in het magazijn voor materiaalgroep '{group}'")
        return [], [], [DFMIssue(code="TOOL_LIBRARY_EMPTY", severity=Severity.info, message=msg + ": gereedschapscontrole overgeslagen.")]
    c.run()
    issues = []
    for ch in c.checks:
        if ch.status == "ok" or (ch.status == "info" and ch.operation in ("contour",)):
            continue
        issues.append(DFMIssue(code=OP_CODES.get(ch.operation, "TOOL_CHECK"),
                               severity=Severity.warning if ch.status == "warning" else Severity.info,
                               feature_id=ch.feature_id if ch.feature_id not in ("stock", "outer_profile") else None,
                               message=ch.message, suggestion=ch.suggested_fix))
    if group == "unknown":
        issues.append(DFMIssue(code="MATERIAL_UNKNOWN", severity=Severity.info,
                               message=f"Materiaal '{spec.material or '-'}' niet herkend: gereedschap niet op materiaal gefilterd."))
    return c.checks, c.assign, issues
