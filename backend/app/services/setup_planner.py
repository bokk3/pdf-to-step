"""Automatic setup planner (SPEC_V2_0_MULTISIDE_FIXTURING.md §3.1). Pure geometry on the face frames, no CAD kernel.

* Features are grouped by their outward machining normal (within 0.1°).
* 3-axis: one setup per distinct normal; the top face (+Z, datum) is always Op 1 and the bottom (-Z) is a setup of
  its own whenever bottom features exist or the raw blank needs an Op 2 deck-off.
* 5-axis 3+2 (trunnion): one setup reaches +Z, the four sides and every tilted plane with n_z >= -0.15; a second
  setup is only needed for directions pointing further down (bottom features / deck-off).
* Op 2 is clamped in softjaws when Op 1 destroys the parallel rectangular gripping faces (non-rectangular profile,
  step cuts, angled face cuts, side bosses / pockets / slots, side edge fillets).
"""

from __future__ import annotations

import math

import numpy as np

from app.schemas.frames import frames
from app.schemas.part_spec import (
    AngledFaceCut,
    BossCircle,
    BossRect,
    Complex3D,
    PartSpec,
    PocketCircle,
    PocketPolygon,
    PocketRect,
    Slot,
    StepCut,
)
from app.schemas.setup_plan import SetupPlan, SetupStep

ANGLE_TOL_DEG = 0.1
REACH_NZ_MIN = -0.15  # 3+2 trunnion: directions with n_z >= -0.15 are reachable in the first clamping
COS_TOL = math.cos(math.radians(ANGLE_TOL_DEG))
UP, DOWN = (0.0, 0.0, 1.0), (0.0, 0.0, -1.0)
FACE_NL = {"top": "bovenzijde", "bottom": "onderzijde", "front": "voorzijde", "back": "achterzijde",
           "left": "linkerzijde", "right": "rechterzijde"}


def _r(v: np.ndarray) -> tuple[float, float, float]:
    return tuple(round(float(x), 6) + 0.0 for x in v)  # type: ignore[return-value]


class _Group:
    def __init__(self, n: np.ndarray):
        self.n = n
        self.faces: list[str] = []
        self.features: list[str] = []


def _groups(spec: PartSpec) -> list[_Group]:
    frs = frames(spec)
    groups: list[_Group] = []
    for f in spec.features:
        if isinstance(f, Complex3D):
            continue  # free-form: direction unknown (flagged by DFM COMPLEX_3D)
        n = frs[f.face].n / np.linalg.norm(frs[f.face].n)
        g = next((g for g in groups if float(g.n @ n) >= COS_TOL), None)
        if g is None:
            g = _Group(n)
            groups.append(g)
        if f.face not in g.faces:
            g.faces.append(f.face)
        g.features.append(f.id)
    return groups


def needs_softjaws(spec: PartSpec) -> bool:
    if spec.outer_profile.type == "polygon":
        return True
    for f in spec.features:
        side = f.face not in ("top", "bottom")
        if isinstance(f, (StepCut, AngledFaceCut)):
            return True
        if side and isinstance(f, (BossRect, BossCircle, PocketRect, PocketCircle, PocketPolygon, Slot)):
            return True
    return False


def _label(faces: list[str]) -> str:
    return ", ".join(FACE_NL.get(f, f.replace("plane:", "vlak ")) for f in faces)


def plan_setups(spec: PartSpec, deck_off_required: bool = True) -> SetupPlan:
    groups = _groups(spec)
    up = next((g for g in groups if float(g.n @ np.array(UP)) >= COS_TOL), None)
    if up is None:  # Op 1 always faces / contours the datum side
        up = _Group(np.array(UP))
        up.faces.append("top")
        groups.insert(0, up)
    down = next((g for g in groups if float(g.n @ np.array(DOWN)) >= COS_TOL), None)
    if down is None and deck_off_required:
        down = _Group(np.array(DOWN))
        down.faces.append("bottom")
        groups.append(down)
    soft = needs_softjaws(spec)

    # ---- 3-axis: one clamping per direction (top first, bottom second, then sides / planes)
    rest = [g for g in groups if g is not up and g is not down]
    rest.sort(key=lambda g: (-float(g.n[2]), g.faces[0]))
    ordered = [up] + ([down] if down else []) + rest
    s3: list[SetupStep] = []
    for i, g in enumerate(ordered, 1):
        if g is up:
            clamp, wh = "Ruw blok in de bankschroef, bovenzijde (datum Z0) omhoog.", "vise_hardjaws"
        elif g is down:
            clamp = ("Omdraaien om X: onderzijde omhoog" + (", vlakfrezen op dikte (deck-off)" if not g.features else "")
                     + ("; klemmen in zachte bekken (bewerkte flanken)." if soft else "; klemmen op de parallelle flanken."))
            wh = "vise_softjaws" if soft else "vise_hardjaws"
        else:
            clamp = f"Opspannen met {_label(g.faces)} omhoog (normaal {_fmt(g.n)})."
            wh = "vise_softjaws" if soft else "vise_hardjaws"
        s3.append(SetupStep(index=i, normals=[_r(g.n)], faces=g.faces, feature_ids=g.features,
                            deck_off=g is down and not g.features, clamping=clamp, workholding=wh))

    # ---- 5-axis 3+2: everything with n_z >= -0.15 in Op 1, the rest (downward) in Op 2
    reach = [g for g in ordered if float(g.n[2]) >= REACH_NZ_MIN]
    low = [g for g in ordered if float(g.n[2]) < REACH_NZ_MIN]
    s5 = [SetupStep(index=1, normals=[_r(g.n) for g in reach], faces=[f for g in reach for f in g.faces],
                    feature_ids=[f for g in reach for f in g.features],
                    clamping="5-assige bankschroef / zwaluwstaart op de trunnion, bovenzijde omhoog; zijkanten en "
                             "schuine vlakken via 3+2-indexering (PLANE SPATIAL / G68.2).", workholding="vise_5axis")]
    if low:
        s5.append(SetupStep(index=2, normals=[_r(g.n) for g in low], faces=[f for g in low for f in g.faces],
                            feature_ids=[f for g in low for f in g.features],
                            deck_off=all(not g.features for g in low),
                            clamping="Omdraaien: onderzijde omhoog" + ("; zachte bekken." if soft else "."),
                            workholding="vise_softjaws" if soft else "vise_hardjaws"))
    notes = []
    if any(isinstance(f, Complex3D) for f in spec.features):
        notes.append("complex_3d-features zitten niet in het plan (richting onbekend).")
    if any(f.face.startswith("plane:") for f in spec.features):
        notes.append("Schuine vlakken: 3-assig alleen met een hoekopspanning / sinustafel per vlak.")
    return SetupPlan(min_setups_3axis=len(s3), min_setups_5axis_3plus2=len(s5), deck_off_required=deck_off_required,
                     softjaws_recommended=soft and len(s3) > 1, setups_3axis=s3, setups_5axis_3plus2=s5, notes=notes)


def _fmt(n: np.ndarray) -> str:
    return "(" + ", ".join(f"{x:+.2f}" for x in n) + ")"


def deck_off_from_stock(spec: PartSpec, stock_recommendation) -> bool:
    """Raw blank thicker than the part -> the second side has to be faced (Op 2 deck-off)."""
    if stock_recommendation is None:
        return True
    (_, _, z0), (_, _, z1) = spec.stock_bbox()
    blank_z = stock_recommendation.recommended_blank_mm.z
    return blank_z > (z1 - z0) + 1e-6
