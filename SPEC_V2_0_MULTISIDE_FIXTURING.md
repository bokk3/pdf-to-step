# Version 2.0 Specification — Multi-Sided & 3+2 Machining, Softjaw Generator, 3D Revision Diff & Quoting Engine

> **Prerequisite specs:** `ARCHITECTURE.md` (v1.0), `DRAWING_ENGINE_SPEC.md` (v1.1), and `SPEC_V1_5_CAM_SHOPFLOOR.md` (v1.5)
>
> **Release Goal:** Graduate the system from single-setup 2.5D plates into a full multi-sided & 3+2 indexed CNC production platform—introducing `PartSpec v2.0` (6 faces + tilted working planes + automatic setup planning), a deterministic **Softjaw STEP generator** for Op 2 workholding, a B-Rep **3D Revision Diff viewer** (Rev A vs. Rev B), and a transparent **Cycle-Time & Cost Quoting Engine** with quantity price breaks.

## Agent Kickoff Prompt (Copy-Paste to Start v2.0 Implementation)

```text
Read ARCHITECTURE.md, DRAWING_ENGINE_SPEC.md, SPEC_V1_5_CAM_SHOPFLOOR.md, and SPEC_V2_0_MULTISIDE_FIXTURING.md in full before writing code.
Implement Version 2.0 while preserving 100% backwards-compatibility with stored PartSpec v1.0/v1.1 jobs via an automatic in-memory upgrader.
All new geometry (multi-sided cuts, 3+2 tilted planes, softjaws with dogbone corner reliefs, and revision boolean diffs) MUST be native B-Rep in build123d/OCCT. Quoting calculations MUST be 100% deterministic math driven by shop_rates.json—never LLM-guessed prices.
Work in the parallel streams defined in Section 7 and ensure full pytest coverage before marking your stream Done.
```

## 1. Non-Negotiable v2.0 Principles

1. **Seamless v1.x $\to$ v2.0 Upgrader:** Existing v1.0/v1.1 JSON files in SQLite MUST load cleanly through a Pydantic model validator (`upgrade_part_spec`) without manual DB migrations (`face="top"` remains valid; `tilted_planes` defaults to `[]`).
2. **Explicit Coordinate Frame Conventions:** Every face (`top`, `bottom`, `front`, `back`, `left`, `right`) and every `tilted_plane` MUST follow a single documented right-handed coordinate convention so Vision extraction, deterministic compilation, and 3D highlighting never mirror or invert axes.
3. **Swept-Profile Softjaw Safety:** Softjaw cavities MUST be cut using a vertically projected/swept silhouette of the workpiece in its clamping pose—never a raw 3D boolean of the part alone—so undercuts or side pockets never lock the workpiece inside the jaws.
4. **Pure B-Rep Revision Diffs:** Never compare revisions by mesh distance heuristics alone. Compute exact OCCT boolean solids (`added`, `removed`, `unchanged`) alongside an order-independent semantic `PartSpec` diff.
5. **Deterministic, Auditable Quoting:** Every Euro/minute in a quote MUST trace back to an explicit volume, surface area, hole count, setup count, and rate from `data/config/shop_rates.json`.

## 2. Schema Upgrade (`PartSpec` v2.0 — Multi-Sided & 3+2 Axis)

Extend `backend/app/schemas/part_spec.py` to support 6-face machining, arbitrary 3+2 indexed planes, and revision lineage:

```json
{
  "schema_version": "2.0",
  "part_number": "A12-400",
  "revision": "B",
  "part_name": "Lagerblok_Schuin",
  "units": "mm",
  "material": "AlMgSi1 (6082-T6)",
  "general_tolerance": "ISO 2768-mK",
  "datum_origin": "bottom_left_top_face",
  "stock": {
    "length_x": 160.0,
    "width_y": 90.0,
    "thickness_z": 40.0,
    "confidence": 0.96
  },
  "tilted_planes": [
    {
      "id": "plane_angle_30",
      "name": "30deg_Port_Face",
      "origin_x": 160.0,
      "origin_y": 45.0,
      "origin_z": 0.0,
      "rot_a_deg": 0.0,
      "rot_b_deg": 30.0,
      "rot_c_deg": 0.0,
      "rotation_convention": "SPATIAL_XYZ",
      "confidence": 0.90,
      "source_note": "Front view 30 degree angled port face"
    }
  ],
  "features": [
    {
      "id": "feat_back_pocket",
      "type": "pocket_rect",
      "face": "bottom",
      "center_x": 80.0,
      "center_y": 45.0,
      "length_x": 100.0,
      "width_y": 50.0,
      "depth": 10.0,
      "corner_radius": 6.0,
      "confidence": 0.93
    },
    {
      "id": "feat_side_hole",
      "type": "hole",
      "face": "right",
      "center_x": 45.0,
      "center_y": 20.0,
      "diameter": 10.0,
      "depth": 25.0,
      "through_all": false,
      "thread": "M10",
      "confidence": 0.88
    },
    {
      "id": "feat_tilted_bore",
      "type": "hole",
      "face": "plane:plane_angle_30",
      "center_x": 0.0,
      "center_y": 0.0,
      "diameter": 14.0,
      "depth": 18.0,
      "through_all": false,
      "tolerance": "H7",
      "confidence": 0.86
    }
  ]
}
```

### Canonical 6-Face Local Coordinate Mapping

Relative to global stock spanning $x \in [0, L_x]$, $y \in [0, W_y]$, $z \in [-T_z, 0]$ (with global datum origin at $(0, 0, 0)$ on `bottom_left_top_face`):

| `face` Value | Plane Origin $(X, Y, Z)$ | Outward Normal $\vec{n}$ | Local $+X$ Direction | Local $+Y$ Direction |
|---|---|---|---|---|
| `top` | $(0, 0, 0)$ | $(0, 0, +1)$ | $(+1, 0, 0)$ | $(0, +1, 0)$ |
| `bottom` | $(0, W_y, -T_z)$ | $(0, 0, -1)$ | $(+1, 0, 0)$ | $(0, -1, 0)$ |
| `front` | $(0, 0, -T_z)$ | $(0, -1, 0)$ | $(+1, 0, 0)$ | $(0, 0, +1)$ |
| `back` | $(L_x, W_y, -T_z)$ | $(0, +1, 0)$ | $(-1, 0, 0)$ | $(0, 0, +1)$ |
| `left` | $(0, W_y, -T_z)$ | $(-1, 0, 0)$ | $(0, -1, 0)$ | $(0, 0, +1)$ |
| `right` | $(L_x, 0, -T_z)$ | $(+1, 0, 0)$ | $(0, +1, 0)$ | $(0, 0, +1)$ |

* **Tilted Planes (`plane:<id>`):** Constructed deterministically in `build123d` via spatial Euler rotation `SPATIAL_XYZ` (intrinsic $X \to Y \to Z$ matching CNC tilted working planes such as `PLANE SPATIAL` / `G68.2`) anchored at `(origin_x, origin_y, origin_z)` with cutting depth always moving along local $-Z$.

## 3. Feature Specifications

### 3.1 Multi-Sided & 3+2 Compiler, Setup Planner & Cross-Face DFM v3

* **Compiler Upgrade (`cad_compiler.py`):**
  * Replace hard-coded `top` workplane logic with `resolve_workplane(spec, feature.face)` returning an exact `build123d.Plane` for all 6 orthogonal faces and any `plane:<id>` in `spec.tilted_planes`.
  * Support planar face-cut features (`angled_face_cut`) to mill inclined exterior faces prior to drilling/pocketing on that tilted plane.
  * Update `step_xcaf_exporter.py` face-matching to project cylindrical/planar signatures into each feature's resolved local workplane so CAM AP214 coloring works on all 6 sides and tilted bores.
* **Automatic Setup Planner (`setup_planner.py`):**
  * Group all features by unique outward machining normal $\vec{n}$ (within $0.1^\circ$ tolerance).
  * Compute and return a `SetupPlan`:
    * **`min_setups_3axis`:** Count of distinct normals (minimum 2 if `bottom` features exist or if Op 2 deck-off is required from raw stock).
    * **`min_setups_5axis_3plus2`:** Count of clamping orientations required assuming a 5-axis trunnion can reach `top` + all 4 side faces + any tilted plane with $n_z \ge -0.15$ in a single setup, requiring a second setup only for `bottom` features.
    * Per-setup feature list, recommended clamping orientation, and suggested workholding type (`vise_hardjaws` for Op 1, `vise_softjaws` for Op 2 when Op 1 removes outer rectangular gripping faces).
* **Cross-Face Breakthrough & Thin-Web Check (`verifier.py` — DFM v3):**
  * For every pair of subtractive features originating from **different** faces/planes, compute the exact minimum 3D Euclidean distance between their cutter tool volumes using OCCT `BRepExtrema_DistShapeShape`.
  * Emit `WARNING` if remaining internal wall/web thickness is in $(0.0, 1.5\text{ mm})$.
  * Emit `ERROR` (unless explicitly flagged `intentional_intersection: true`) if two blind features from opposing faces unintentionally break through into each other.

### 3.2 Automatic Softjaw Generator (`softjaw_generator.py`)

Generates ready-to-mill B-Rep Op 2 softjaws directly from any finished `Part` solid and a configured shop vise profile.

* **Vise Library (`data/config/vises.json`):** Stores exact physical jaw blank geometry and bolt hole patterns for the workshop's vises (e.g., 125 mm / 160 mm standard Kurt/Allmatic/Schunk/Lang jaws).
* **Generation Parameters (`SoftjawRequest`):**
  * `vise_id`: Target vise key from `vises.json`.
  * `op2_flip_axis`: `"X"` (flip $180^\circ$ around X, default) or `"Y"`, plus optional `z_rotation_deg` ($0^\circ$ or $90^\circ$ to align long side with jaws).
  * `grip_depth_mm`: How deep the workpiece sits inside the jaws (default `4.0 mm`, range `2.0–25.0 mm`).
  * `xy_clearance_mm`: Radial slip-fit clearance around the workpiece contour (default `0.05 mm`).
  * `dogbone_radius_mm`: Corner-relief radius automatically added at sharp convex workpiece corners so the part seats cleanly without hitting jaw pocket radii (default `3.2 mm` for a $\varnothing 6\text{ mm}$ endmill).
  * `jaw_gap_mm`: Nominal spacing between fixed and movable jaw faces when clamped (default `6.0 mm`).
  * `jaw_material`: `"aluminum_6082"` (default) or `"steel_c45"`.
* **Deterministic `build123d` Algorithm:**
  1. Load compiled `Part`, apply `op2_flip_axis` + `z_rotation_deg`, and center its XY bounding box across the jaw symmetry plane, positioned vertically so the lowest clamping zone extends `grip_depth_mm` below the top face of the jaws (`Z = 0` at jaw top).
  2. **Undercut-Safe Swept Profile:** Slice the portion of the workpiece inside $z \in [-\text{grip\_depth\_mm}, 0]$, project its outer 2D silhouette onto the XY plane (`project_to_2d` / outer hull of cross-sections), offset outward by `xy_clearance_mm`, and extrude vertically from $z = -\text{grip\_depth\_mm}$ to $z = +10\text{ mm}$.
  3. **Automatic Corner Reliefs (Dogbones):** Inspect all vertices of the 2D silhouette wire. Wherever two adjacent edges meet at an interior angle $\theta < 135^\circ$ (convex workpiece corner pressing into the jaw), place a vertical relief cylinder of radius `dogbone_radius_mm` centered on the vertex (offset slightly outward along the angle bisector by $0.25 \cdot r$) and union it with the cavity cutter.
  4. **Build & Cut Jaw Blanks:** Construct `Fixed_Jaw` and `Movable_Jaw` solids from `vises.json` (including bolt counterbores and alignment keyway/pins), spaced symmetrically by `jaw_gap_mm`. Subtract the swept cavity + dogbones from both jaws.
  5. **Safety Checks (Blocking if Failed):**
     * Verify minimum remaining wall thickness between cavity floor (`-grip_depth_mm`) and bolt counterbore ceiling is $\ge 3.0\text{ mm}$.
     * Verify each jaw contacts at least `25%` of the workpiece's side clamping length (warns if part is too narrow/wide for the selected `vise_id`).
     * Verify jaw outer wall behind the pocket is $\ge 5.0\text{ mm}$.
  6. **Artifacts Exported (`data/jobs/<job_id>/softjaws/`):**
     * `softjaws.step`: Multi-body AP214 STEP containing named solids `Fixed_Jaw` and `Movable_Jaw` (with mating pocket faces highlighted in Emerald Green for CAM).
     * `softjaws_assembly.glb`: Preview mesh showing both opaque jaws + translucent seated workpiece for visual confirmation in Three.js.
     * `softjaw_report.json`: Safety check results, jaw blank dimensions, and recommended jaw-milling endmill from `tools.json`.

### 3.3 Visual 3D Revision Diff Engine (`revision_diff.py`)

Eliminates missed "small customer tweaks" on repeat orders by comparing two job revisions (`job_old_id` vs. `job_new_id`) both geometrically and semantically.

* **Part Lineage in SQLite (`db.py`):** Index jobs by normalized `part_number` and `revision` (`A`, `B`, `C`, or `1`, `2`, `3`) so uploading a new revision automatically offers **"Compare with Previous Rev"**.
* **Deterministic B-Rep Boolean Diff:**
  1. Load `part_old` and `part_new` B-Rep solids aligned on their canonical `datum_origin`.
  2. Compute three exact solids via OCCT boolean operations:
     $$S_{\text{added}} = S_{\text{new}} \setminus S_{\text{old}}, \qquad S_{\text{removed}} = S_{\text{old}} \setminus S_{\text{new}}, \qquad S_{\text{unchanged}} = S_{\text{old}} \cap S_{\text{new}}$$
  3. Filter out numerical noise sliver fragments with volume $< 0.05\text{ mm}^3$.
  4. Export a single multi-material `revision_diff.glb`:
     * $S_{\text{added}}$: **Emerald Green** (`#1EB45A`, solid) — material added in new revision.
     * $S_{\text{removed}}$: **Signal Red** (`#E61E1E`, solid) — material removed in new revision.
     * $S_{\text{unchanged}}$: **Neutral Gray** (`#B9BEC4`, opacity `0.30`) — unchanged reference body.
* **Semantic `PartSpec` Diff:**
  * Run order-independent feature matching (reusing nearest-neighbor center/type pairing from Phase 2) across `stock`, `tilted_planes`, `features`, `edge_treatments`, `material`, and `general_tolerance`.
  * Classify changes into `BREAKING_CAM` (stock size changed, material changed, added setup face, tighter tolerance like `H7` added), `GEOMETRY_CHANGE` (hole diameter/depth/position moved), and `METADATA_ONLY` (drawing number/note update).
  * Output structured `RevisionDiffReport` + downloadable 1-page `revision_diff_report.pdf`.

### 3.4 Cycle-Time & Cost Quoting Engine (`quoting_engine.py`)

Produces an instant, transparent manufacturing time and cost estimate within seconds of `PartSpec` compilation.

* **Rate Configuration (`data/config/shop_rates.json`):** Stores workshop-specific hourly rates, setup times, material prices/kg, material removal rates (MRR), and standard per-feature cycle times.
* **Deterministic Formula Breakdown:**
  1. **Raw Material Cost ($C_{\text{mat}}$):**
     $$C_{\text{mat}} = m_{\text{raw}} \cdot p_{\text{mat}} \cdot (1 + k_{\text{saw\_waste}})$$
     using `StockRecommendation.raw_mass_kg` from v1.5.
  2. **Roughing Machining Time ($t_{\text{rough}}$, minutes):**
     $$t_{\text{rough}} = \frac{V_{\text{raw}} - V_{\text{part}}}{\text{MRR}_{\text{mat}} \cdot \eta_{\text{chip\_load}}}$$
     where volumes are in $\text{cm}^3$ and $\text{MRR}_{\text{mat}}$ is in $\text{cm}^3/\text{min}$.
  3. **Finishing Machining Time ($t_{\text{finish}}$, minutes):**
     Compute exact machined surface area $A_{\text{machined}}$ ($\text{cm}^2$) from B-Rep faces (excluding untouched raw faces if any), multiplied by material finishing speed factor (applying a $1.5\times$ multiplier for faces requiring `Ra <= 1.6` or tight fits).
  4. **Hole-Making Time ($t_{\text{holes}}$, minutes):**
     Sum deterministic seconds per hole: spot drill + peck drill (scaled by depth/diameter ratio) + rigid tap (if `thread`) + ream/bore (if tight `tolerance`) + counterbore/countersink + tool-change overhead ($t_{\text{atc}}$ per unique tool in `recommended_tools`).
  5. **Setup & Programming Amortization ($t_{\text{setup}}$):**
     $$t_{\text{setup}} = t_{\text{cam\_prog}} + N_{\text{setups}} \cdot t_{\text{fixture\_setup}} + \mathbb{I}_{\text{softjaws}} \cdot t_{\text{mill\_softjaws}}$$
     where $N_{\text{setups}}$ comes directly from `SetupPlan` (allowing toggle between 3-axis vs. 5-axis 3+2 hourly rate).
  6. **Unit Cost at Quantity $Q$:**
     $$C_{\text{unit}}(Q) = C_{\text{mat}} + \frac{t_{\text{setup}}}{60 \cdot Q} \cdot R_{\text{machine}} + \frac{t_{\text{cycle}} + t_{\text{load\_unload}}}{60} \cdot R_{\text{machine}}$$
* **Outputs:**
  * Price-break table for standard quantities (`Q = 1, 5, 10, 25, 50`, plus custom `Q`).
  * Comparison toggle: **3-Axis VMC vs. 5-Axis 3+2** total cost.
  * Exportable `quote_summary.pdf` and `.csv` for ERP/accounting handoff.

## 4. Configuration Schemas

### 4.1 `data/config/vises.json`

```json
{
  "schema_version": "1.0",
  "vises": [
    {
      "id": "vise_125_std",
      "name": "Standard 125mm CNC Vise (M10)",
      "jaw_width_mm": 125.0,
      "jaw_height_mm": 40.0,
      "jaw_depth_mm": 25.0,
      "max_opening_mm": 150.0,
      "bolt_spacing_mm": 80.0,
      "bolt_height_from_bottom_mm": 18.0,
      "bolt_clearance_dia_mm": 11.0,
      "bolt_cbore_dia_mm": 18.0,
      "bolt_cbore_depth_mm": 12.0,
      "default_jaw_gap_mm": 6.0
    },
    {
      "id": "vise_160_std",
      "name": "Standard 160mm CNC Vise (M12)",
      "jaw_width_mm": 160.0,
      "jaw_height_mm": 50.0,
      "jaw_depth_mm": 30.0,
      "max_opening_mm": 200.0,
      "bolt_spacing_mm": 100.0,
      "bolt_height_from_bottom_mm": 22.0,
      "bolt_clearance_dia_mm": 13.0,
      "bolt_cbore_dia_mm": 20.0,
      "bolt_cbore_depth_mm": 14.0,
      "default_jaw_gap_mm": 8.0
    }
  ]
}
```

### 4.2 `data/config/shop_rates.json`

```json
{
  "schema_version": "1.0",
  "currency": "EUR",
  "machines": {
    "3axis_vmc": {
      "label": "3-Axis VMC",
      "hourly_rate": 75.0,
      "tool_change_sec": 6.0,
      "load_unload_sec": 45.0
    },
    "5axis_3plus2": {
      "label": "5-Axis (3+2 Indexed)",
      "hourly_rate": 105.0,
      "tool_change_sec": 5.0,
      "load_unload_sec": 45.0
    }
  },
  "setup_minutes": {
    "base_cam_programming": 20.0,
    "per_3axis_setup": 15.0,
    "per_5axis_setup": 20.0,
    "mill_softjaws_pair": 25.0
  },
  "materials": {
    "aluminum": {
      "price_per_kg": 9.5,
      "mrr_cm3_per_min": 45.0,
      "finish_min_per_100cm2": 1.2,
      "hole_sec_base": 12.0,
      "tap_sec": 10.0,
      "ream_sec": 18.0
    },
    "steel": {
      "price_per_kg": 4.8,
      "mrr_cm3_per_min": 14.0,
      "finish_min_per_100cm2": 2.8,
      "hole_sec_base": 24.0,
      "tap_sec": 18.0,
      "ream_sec": 30.0
    },
    "stainless": {
      "price_per_kg": 8.2,
      "mrr_cm3_per_min": 9.0,
      "finish_min_per_100cm2": 3.8,
      "hole_sec_base": 32.0,
      "tap_sec": 25.0,
      "ream_sec": 40.0
    },
    "plastic": {
      "price_per_kg": 14.0,
      "mrr_cm3_per_min": 65.0,
      "finish_min_per_100cm2": 0.9,
      "hole_sec_base": 8.0,
      "tap_sec": 8.0,
      "ream_sec": 12.0
    }
  },
  "saw_waste_factor": 0.08,
  "margin_pct_default": 20.0
}
```

## 5. Repository Structure Additions

```text
pdf-to-step/
├── data/
│   └── config/
│       ├── vises.json                  # Standard workshop vise jaw profiles
│       └── shop_rates.json             # Machine hourly rates, MRR & material costs
├── backend/
│   ├── app/
│   │   ├── api/
│   │   │   ├── softjaws.py             # Softjaw generation & vise config endpoints
│   │   │   ├── revisions.py            # Part revision lineage & 3D diff endpoints
│   │   │   └── quotes.py               # Deterministic quoting & PDF/CSV export
│   │   ├── schemas/
│   │   │   ├── softjaw.py              # ViseSpec, SoftjawRequest, SoftjawReport
│   │   │   ├── revision.py             # RevisionDiffReport, FeatureDiffItem
│   │   │   └── quote.py                # QuoteRequest, QuoteBreakdown, PriceBreak
│   │   └── services/
│   │       ├── setup_planner.py        # 3-axis vs 5-axis 3+2 setup clustering
│   │       ├── softjaw_generator.py    # Swept cavity + dogbone B-Rep jaw builder
│   │       ├── revision_diff.py        # OCCT boolean diff + semantic PartSpec diff
│   │       └── quoting_engine.py       # Deterministic cycle-time & cost estimator
│   └── tests/
│       ├── test_multiside_3plus2.py    # 6-face + 30deg tilted plane + cross-face DFM tests
│       ├── test_softjaws.py            # Swept cavity undercut safety, dogbones, bolt clearance
│       ├── test_revision_diff.py       # Added/removed boolean volume & semantic diff tests
│       └── test_quoting.py             # Quantity amortization, 3-axis vs 5-axis cost math
└── frontend/
    └── src/
        ├── pages/
        │   ├── RevisionDiffPage.tsx    # Split semantic diff + Green/Red/Gray 3D viewer
        │   └── QuoteCalculatorPage.tsx # Interactive quantity breaks & machine toggle
        └── components/
            ├── SetupPlanPanel.tsx      # 3-axis vs 3+2 setup breakdown badges
            └── SoftjawModal.tsx        # Vise selector, flip axis, grip depth & 3D preview
```

## 6. New REST API Endpoints

| Method | Endpoint | Description |
|---|---|---|
| `GET` | `/api/config/vises` | Fetch configured workshop vise jaw profiles (`vises.json`). |
| `PUT` | `/api/config/vises` | Save/update `vises.json` (Pydantic validated). |
| `GET` | `/api/config/shop-rates` | Fetch `shop_rates.json`. |
| `PUT` | `/api/config/shop-rates` | Update machine hourly rates, MRR, and material prices. |
| `GET` | `/api/jobs/{job_id}/setup-plan` | Returns `SetupPlan` (`min_setups_3axis`, `min_setups_5axis_3plus2`, per-setup features, clamping advice). |
| `POST` | `/api/jobs/{job_id}/softjaws` | Accepts `SoftjawRequest`; generates `softjaws.step`, `softjaws_assembly.glb`, and `softjaw_report.json`. |
| `GET` | `/api/jobs/{job_id}/artifacts/softjaws/{filename}` | Downloads `softjaws.step` or `softjaws_assembly.glb`. |
| `GET` | `/api/parts/{part_number}/revisions` | Lists all jobs/revisions belonging to `part_number` ordered chronologically. |
| `POST` | `/api/revisions/diff` | Accepts `{"job_old_id": "...", "job_new_id": "..."}`; runs B-Rep boolean + semantic diff and returns `RevisionDiffReport` + `revision_diff.glb` URL. |
| `GET` | `/api/revisions/diff/{diff_id}/artifacts/{filename}` | Serves `revision_diff.glb` or `revision_diff_report.pdf`. |
| `POST` | `/api/jobs/{job_id}/quote` | Computes `QuoteBreakdown` with customizable `quantities`, `machine_type`, `include_softjaws`, and `margin_pct`. |
| `GET` | `/api/jobs/{job_id}/quote/export.{fmt}` | Exports `quote_summary.pdf` or `quote_summary.csv` (`fmt` in `pdf`, `csv`). |

## 7. Parallel Work Breakdown for Coding Agents (v2.0)

### Agent A — `PartSpec v2.0`, 6-Face / 3+2 Compiler & Cross-Face DFM v3
* **Scope:** `schemas/part_spec.py` (v2.0 + v1.x auto-upgrader), `services/cad_compiler.py`, `services/setup_planner.py`, `services/verifier.py` (cross-face `BRepExtrema` check), `services/step_xcaf_exporter.py` (multi-face color matching), `tests/test_multiside_3plus2.py`.
* **Definition of Done:** Compiles a 6-sided test part with an additional $30^\circ$ tilted plane bore (`Lagerblok_Schuin`) into a valid manifold STEP with accurate AP214 face colors on bottom, side, and tilted holes. Detects an intentional 0.8 mm thin web between opposing top and bottom pockets. All existing v1.0/v1.1 tests still pass unmodified.

### Agent B — Automatic Softjaw STEP Generator
* **Scope:** `data/config/vises.json`, `schemas/softjaw.py`, `services/softjaw_generator.py`, `api/softjaws.py`, `tests/test_softjaws.py`.
* **Definition of Done:** Given a rounded-corner and a sharp-corner part with side pockets, generates a valid 2-solid `softjaws.step` (`Fixed_Jaw`, `Movable_Jaw`) with swept undercut-free cavities, automatic dogbone reliefs on sharp corners, and `softjaws_assembly.glb`. Rejects configurations where grip depth breaches the 3.0 mm bolt-counterbore safety margin.

### Agent C — Visual 3D Revision Diff & Quoting Engine
* **Scope:** `schemas/revision.py`, `schemas/quote.py`, `services/revision_diff.py`, `services/quoting_engine.py`, `api/revisions.py`, `api/quotes.py`, `data/config/shop_rates.json`, `tests/test_revision_diff.py`, `tests/test_quoting.py`.
* **Definition of Done:** Comparing two revisions where 1 pocket got 4 mm deeper and 1 M6 hole became M8 H7 produces exact `added`/`removed` boolean volumes within $\pm 1\%$, exports a 3-color `revision_diff.glb`, and emits a `BREAKING_CAM` semantic report. Quoting engine computes deterministic price breaks (`Q = 1, 5, 10, 25, 50`) and exports valid PDF/CSV summaries.

### Agent D — Frontend v2.0 (Softjaw Modal, 3D Diff Viewer & Quote UI)
* **Scope:** `SetupPlanPanel.tsx`, `SoftjawModal.tsx`, `RevisionDiffPage.tsx`, `QuoteCalculatorPage.tsx`, plus Vise & Shop Rates settings tabs.
* **Definition of Done:** Operators can inspect setup counts (`3-Axis` vs `5-Axis 3+2`), generate and orbit an interactive Op 2 Softjaw assembly preview in 1 click, toggle Green/Red/Gray layers in the 3D Revision Diff view alongside semantic change badges, and adjust quantities/margins live in the Quote Calculator with PDF export.

## 8. Release Acceptance Checklist (v2.0 Gate)

* [ ] All v1.0, v1.1, and v1.5 jobs in SQLite load and re-compile with zero migration errors.
* [ ] A multi-sided part with bottom pocket, right-side threaded hole, and $30^\circ$ tilted H7 bore exports cleanly to AP214 STEP with accurate CAM face colors and correct `SetupPlan` counts.
* [ ] Generated `softjaws.step` imports into CAM as 2 clean solid bodies with dogbone corner reliefs, 0.05 mm clearance, and zero undercut interference when sliding the part vertically out of the jaws.
* [ ] Uploading Rev B of an existing `part_number` renders added material in Green and removed material in Red inside Three.js and flags tolerance/thread upgrades as `BREAKING_CAM`.
* [ ] Quoting engine produces a reproducible cost breakdown and `quote_summary.pdf` comparing 3-axis vs. 5-axis 3+2 across quantity breaks.