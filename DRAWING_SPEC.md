# EU (EN ISO) 2D Technical Drawing Engine, Sketch Ingestion & Benchmark — Agent Specification

> **Companion document to:** `ARCHITECTURE.md`  
> **Scope:** Unified Model-Based Definition (MBD) workflow that keeps `PartSpec` as the single source of truth to deterministically generate both CNC-ready `.step` files and EN ISO 2D technical drawings (`.pdf`, `.svg`, `.dxf`), adds a printable ArUco A4 sketch workflow + DXF/STEP fast paths, and establishes an automated round-trip benchmark harness.

---

## 1. Mission & Core Architectural Principles

### 1.1 Model-Based Definition (Single Source of Truth — No "Telephone Game")
Never convert a rough sketch into a 2D PDF and feed that PDF back through Vision AI to generate a STEP file (`Sketch -> AI -> 2D PDF -> AI -> STEP`). Double translation compounds errors.

Instead, `PartSpec` (`backend/app/schemas/part_spec.py`) is the **single canonical source of truth**:
1. **Ingestion:** Customer PDFs, paper sketches on our A4 template, layered 2D DXF files, or manual edits populate and validate a `PartSpec`.
2. **Parallel Deterministic Outputs:** From one verified `PartSpec`, the backend deterministically generates:
   - **3D Manufacturing Output:** `model.step` + `preview.glb` via `cad_compiler.py` (`build123d`).
   - **2D Workshop Documentation:** EN ISO technical drawing `drawing.pdf` (plus optional `.svg` / `.dxf`) via `drawing_engine.py`, saved per revision alongside `model.step`.
   - Because both artifacts derive from the exact same `PartSpec` and B-Rep solid, the 2D drawing and 3D STEP match by definition.

### 1.2 Explicit Exception — Synthetic Round-Trip Benchmark
Feeding generated EN ISO PDFs back through the PDF-to-STEP pipeline is strictly reserved for the automated benchmark suite (`roundtrip_bench.py`) in Phase 2. Note that synthetic PDFs are "cleaner" than real customer scans; therefore, benchmarks MUST always combine synthetic round-trip runs with real workshop fixture PDFs.

### 1.3 Default Workshop Configuration Decisions (Overridable per Part)
Unless overridden in `PartSpec.drawing_metadata` or `.env`:
- **Sheet Size:** `A4_LANDSCAPE` (`297 x 210 mm`) by default; auto-upgrade to `A3_LANDSCAPE` (`420 x 297 mm`) when scale drops below `1:2` or feature count exceeds `12`.
- **Drawing Language:** English ISO standard headers & callouts (`THRU`, `CBORE`) with optional bilingual (`EN/NL`) title block labels for maximum Vision/machinist consistency.
- **General Tolerance:** `ISO 2768-mK` default (overrideable per part).
- **Dimensioning Mode:** Fully automated baseline dimensioning + standardized callouts derived semantically from `PartSpec`, cleanly separated per orthographic view.
- **Approval Status:** Every generated drawing defaults to `DRAFT` ("Getekend / Gecontroleerd" — Drawn / Checked fields left open for human sign-off).

---

## 2. Concrete EN ISO Technical Drawing Standards ("EU Spec")

There is no separate "EU CAD standard"; European CNC workshops standardize on ISO norms adopted as **EN ISO**. The drawing engine MUST enforce the following norms:

| Subject | Applicable Standard | Concrete Implementation Requirement |
|---|---|---|
| **Line types & views** | **EN ISO 128-1 / -2 / -3** | Continuous thick (`0.50` / `0.35 mm`) visible edges; dashed thin (`0.25` / `0.18 mm`) hidden edges; long-dash short-dash thin (`0.18 mm`) centerlines; 45 deg hatching (ISO 128-50). |
| **Dimensioning** | **EN ISO 129-1** | Baseline / coordinate dimensioning from one datum origin (`bottom_left_top_face`). No chain dimensioning to avoid tolerance stacking. |
| **Projection method** | **EN ISO 5456-2** | **European First-Angle Projection:** Front view top-left, **Top view below Front view**, **Left side view to the right of Front view**. Mandatory First-Angle symbol in title block. |
| **Sheet format & title block** | **EN ISO 5457 / EN ISO 7200** | A4/A3 border (`20 mm` left binding margin, `10 mm` other sides, `0.70 mm` frame) + `180 mm` bottom-right structured title block with selectable vector text. |
| **Lettering** | **EN ISO 3098** | Clean sans-serif technical vector font (e.g., `ISOCPEUR` / `osifont` / `DejaVu Sans`), minimum `2.5 mm` height on A4 (`3.5 mm` for primary dimensions). |
| **General tolerances** | **ISO 2768-1 / -2** *(successor: ISO 22081)* | Default `ISO 2768-mK` printed in title block and general notes (workshop standard). |
| **Fits & GD&T** | **EN ISO 286 / EN ISO 1101** | Fits (e.g. `Ø10 H7`) printed in callouts and tagged for CAM; GD&T frames stored & rendered as text notes only in v1 (not 3D-modeled). |
| **Surface finish, threads & edges** | **EN ISO 21920-1** *(replaces ISO 1302)*, **EN ISO 6410**, **EN ISO 13715** | Default surface note (`Ra 3.2` per ISO 21920-1), standard thread callouts (`M8 ↧12`), and edge-break note (`ISO 13715` / "Break sharp edges 0.2–0.5 mm"). |

### Standardized Callout Formatting
Use consistent ISO symbols rendered as true vector text (never rasterized bitmaps):
- Through hole: `4x Ø8 H7 THRU`
- Blind threaded hole: `2x M8 ↧12`
- Counterbored through hole: `4x Ø6.6 THRU / ⌴ Ø11 ↧6.8`
- Corner radius / chamfer: `R5`, `1x45°`

---

## 3. Ingestion Routes (4 Pathways by Input Source)

### Route A — Paper Hand Sketch via Printable ArUco A4 Template (Recommended for Own Workshop Parts)
Freehand sketches on blank paper are error-prone for Vision AI and perspective correction. Instead, provide a structured **printable A4 PDF template** (`GET /api/templates/sketch-a4.pdf`):

1. **Template Design (`sketch_template.py`):**
   - **4 Black ArUco Fiducial Corner Markers** (e.g. `DICT_4X4_50`, IDs `0–3`) in the sheet margins.
   - Light millimeter grid (`5 mm` minor / `10 mm` major) in non-reproducing light cyan/gray.
   - Pre-labeled European First-Angle boxes: **Vooraanzicht (Front)** top-left, **Bovenaanzicht (Top)** bottom-left, **Zijaanzicht (Side)** top-right.
   - Clearly marked **Datum Origin `(0, 0)`** in the bottom-left corner of the Top View box.
   - Structured title-block fields (Part Name, Material, Thickness Z, General Tolerance) + a short operator checklist.
2. **Mobile Capture & Deterministic OpenCV Preprocessing (`sketch_ingestor.py`):**
   - Frontend allows direct mobile camera capture (`<input type="file" accept="image/*" capture="environment">`).
   - OpenCV detects the 4 ArUco markers, computes an exact homography warp to flatten tilted workbench photos into a clean rectangular image, boosts contrast/adaptive thresholding, and **deterministically crops** the exact Front View, Top View, Side View, and Title Block boxes by known template coordinates.
3. **Vision Extraction (`deepseek-flash`) & Optional OCR Benchmark:**
   - Send the rectified full sheet + individual cropped view boxes to `deepseek-flash` with a dedicated sketch prompt: **written dimensions strictly override drawn proportions** (sketches are never to scale), and missing values MUST NOT be guessed.
   - *Experiment to benchmark in Phase 3:* Evaluate DeepSeek Files API `text_recognition` / `rich_layout_recognition` to extract handwritten numbers as an auxiliary text layer alongside the vision prompt.
4. **Mandatory Human Review Gate:**
   - **Hard Safety Rule:** Every dimension extracted from a paper sketch initializes with `confidence <= 0.75` (flagged `"Te controleren"` / amber in the mobile Spec tab).
   - Missing critical dimensions (e.g., plate thickness `Z` or pocket depth) populate `missing_dimensions` with `blocking: true`, pausing the job in status `awaiting_dimensions` until the operator confirms/enters all values in the Spec tab.
   - On confirmation (`PATCH /api/jobs/{job_id}/spec`), the backend immediately builds `model.step`, `preview.glb`, and the clean EN ISO `drawing.pdf`.

### Route B — Quick Sketch in a CAD / Drawing Program
- **From 3D CAD (FreeCAD, Onshape, Fusion 360):** Export `.step` directly and upload via `POST /api/jobs/from-step`. **Never** export 3D -> 2D PDF -> Vision -> STEP. The backend imports the B-Rep STEP directly, runs DFM validation (`verifier.py`), applies CAM face color-coding (AP214/XCAF), and optionally generates the EN ISO `drawing.pdf`.
- **From 2D CAD (QCAD, LibreCAD, AutoCAD LT):** Prefer uploading a **layered `.dxf`** (`POST /api/jobs/from-dxf`) parsed 100% deterministically via `ezdxf` — zero AI calls, zero hallucination risk:

| DXF Layer Name | Mapped `PartSpec` Entity | Example |
|---|---|---|
| `CONTOUR_T<thickness>` | Outer profile + stock `thickness_z` (mm) | `CONTOUR_T20` |
| `POCKET_D<depth>` | `pocket_rect` / `pocket_circle` / `pocket_polygon` | `POCKET_D8.5` |
| `HOLE_THRU` | Through-all `hole` (diameter from circle entity) | `HOLE_THRU` |
| `HOLE_D<depth>` | Blind `hole` with depth in mm | `HOLE_D12` |
| `THREAD_<spec>_D<depth>` | Threaded `hole` (or `_THRU`) | `THREAD_M6_D15` |
| `SLOT_D<depth>` | Obround `slot` (width + start/end from geometry) | `SLOT_D6` |

*(If a user exports a 2D PDF from QCAD/LibreCAD instead of DXF, it routes through Route C provided it meets the checklist below, though DXF or Route A is strictly preferred.)*

### Route C — Customer Drawings (PDF)
Processed by the existing PDF-to-STEP pipeline (`ARCHITECTURE.md`). Drawings achieve highest extraction accuracy when meeting this **Intake Quality Checklist**:
- Vector PDF with real selectable text (not scanned raster or outlined curves).
- European First-Angle projection with projection symbol, one part per sheet, stated scale.
- All dimensions in `mm` referenced from one datum origin (bottom-left of top view), no duplicate/over-constrained dimensions.
- Standard ISO callouts (`4x Ø6.6 THRU ⌴Ø11 ↧6.8`, `M8 ↧12`, `Ø10 H7`, `R5`, `1x45°`).
- Complete title block (part number, material, general tolerance, units, revision).
- Hidden edges dashed, centerlines dash-dot (EN ISO 128-2).

### Route D — Live 2D Preview inside the Spec Editor (Phase 4 Optional)
An interactive 2D SVG live-preview panel synced with the `PartSpec` form, effectively turning the Spec editor itself into a rapid 2.5D sketching tool without external CAD software.

---

## 4. Drawing Engine Architecture (Avoiding `build123d` Layout Pitfalls)

### Critical Engineering Constraint
While `build123d` includes `TechnicalDrawing`, `ExtensionLine`, and ISO line types, **DO NOT rely on `build123d.TechnicalDrawing` as the complete sheet/dimensioning engine**. Its built-in border is not EN ISO 7200 compliant, and it provides **no collision avoidance for overlapping dimension lines or text labels** on multi-feature CNC parts.

### Primary Engine Design — Semantic `build123d` HLR + Custom ISO SVG Composer
Reuse the existing B-Rep projection logic already powering `ortho_check.png`, paired with a deterministic SVG sheet composer:

1. **Exact B-Rep HLR Projections (`build123d`):**
   - Call `project_to_viewport` on the compiled `Part` for **Front**, **Top**, **Left**, and **Isometric** viewports to obtain exact 2D visible and hidden vector paths.
2. **Automatic Sheet & Scale Solver (`iso_template.py`):**
   - Compute combined First-Angle view bounding boxes including `25 mm` dimension gutters around every view.
   - Select the largest standard ISO 5455 scale (`2:1`, `1:1`, `1:2`, `1:5`, `1:10`) fitting `A4_LANDSCAPE`; automatically promote to `A3_LANDSCAPE` if scale falls below `1:2` or feature count `> 12`.
3. **View-Separated Semantic Dimensioning (`drawing_engine.py`):**
   - Read exact feature semantics directly from `PartSpec` (zero geometric guesswork):
     - **Top View:** Overall `length_x`, `width_y`, datum origin `(0, 0)`, baseline X/Y coordinates of all holes/pockets/slots, outer corner radii, and leader callouts (`4x Ø8 H7 THRU`, `R6`).
     - **Front & Left Side Views:** Overall `thickness_z`, pocket depths, blind hole depths, counterbore depths, and chamfers (`1x45°`).
   - **Anti-Overlap Staggering:** Place first baseline dimension line `10 mm` outside the contour and stagger parallel lines by `8 mm`. Run a 2D bounding-box overlap check across all text labels and push colliding callouts radially outward along leader lines.
4. **Vector PDF & DXF Export:**
   - Compose layered SVG (Border + Title Block + ISO First-Angle Symbol + HLR Views + Dimensions + General Notes).
   - Convert SVG -> print-ready **vector PDF** via `CairoSVG` or `WeasyPrint`, guaranteeing that `PyMuPDF` can extract every single dimension string as crisp text (critical for Phase 2 round-trip verification).
   - Optional `.dxf` export via `ezdxf`.

### Secondary / Fallback Engines (Behind `DrawingEngine` Protocol)
- **`draftwright` (optional plug-in):** Useful as an accelerator for `complex_3d` fallback parts where 2.5D semantic rules do not cover all geometry. Pin an exact version in `backend/pyproject.toml` because its internals are actively transitioning to Rust.
- **FreeCAD Headless `TechDraw` (future fallback):** Only if complex multi-plane offset sections are required later (`freecadcmd script.py --pass -platform offscreen`).

---

## 5. Schema Extensions (`PartSpec` v1.1 — Backwards Compatible)

Extend `backend/app/schemas/part_spec.py` with optional fields so existing v1.0 payloads continue to validate cleanly:

```json
{
  "schema_version": "1.1",
  "input_source": "paper_sketch",
  "drawing_metadata": {
    "sheet_size": "AUTO",
    "projection": "ISO_FIRST_ANGLE",
    "scale": "AUTO",
    "language": "EN",
    "surface_finish": "Ra 3.2 (ISO 21920-1)",
    "edge_break_note": "Break sharp edges 0.2-0.5 mm (ISO 13715)",
    "drawing_number": "DWG-2026-001",
    "revision": "A",
    "drawn_by": "",
    "checked_by": "",
    "approval_status": "DRAFT"
  },
  "missing_dimensions": [
    {
      "field_path": "stock.thickness_z",
      "prompt_nl": "Plaatdikte (Z) in mm ontbreekt op de schets",
      "prompt_en": "Stock thickness (Z) in mm is missing from the sketch",
      "suggested_default": null,
      "blocking": true
    }
  ]
}
```

* **Validation Gate:** If `any(item.blocking for item in spec.missing_dimensions)` is `True`, the job enters status `awaiting_dimensions` and skips CAD/drawing compilation until resolved via `PATCH /api/jobs/{job_id}/spec`.

---

## 6. Module Additions & REST API Contract

### New Repository Files
```text
backend/app/
├── api/
│   └── drawings.py               # Sketch, DXF, STEP upload & drawing endpoints
├── services/
│   ├── sketch_template.py        # Printable A4 PDF generator with 4 ArUco markers
│   ├── sketch_ingestor.py        # OpenCV ArUco warp/crop + deepseek-flash sketch extractor
│   ├── dxf_parser.py             # Deterministic ezdxf layered DXF -> PartSpec parser
│   ├── step_ingestor.py          # Direct STEP import -> DFM check + CAM AP214 coloring
│   ├── iso_template.py           # EN ISO 5457/7200/5456-2 SVG sheet & title-block builder
│   ├── drawing_engine.py         # Semantic build123d HLR + SVG composer + vector PDF export
│   └── roundtrip_bench.py        # Procedural PartSpec -> ISO PDF -> Pipeline benchmark
└── tests/
    ├── test_drawing_engine.py    # ISO layout, First-Angle placement, PyMuPDF text & overlap tests
    ├── test_dxf_parser.py        # Deterministic layer-to-PartSpec tests
    ├── test_sketch_aruco.py      # Synthetic tilted photo -> ArUco warp & crop tests
    └── test_roundtrip.py         # End-to-end geometric diff & thinking-mode A/B benchmark
```

### New REST Endpoints

| Method | Endpoint | Description |
|---|---|---|
| `GET` | `/api/templates/sketch-a4.pdf` | Downloads the printable A4 workshop sketch template (with 4 ArUco corner markers, First-Angle view boxes, datum origin, title block, and checklist). |
| `POST` | `/api/jobs/from-sketch` | Uploads mobile photo/scan of a sketch. Runs OpenCV ArUco perspective warp + `deepseek-flash` extraction, caps all confidences `<= 0.75`, and pauses at `awaiting_dimensions` if blocking fields are missing. |
| `POST` | `/api/jobs/from-dxf` | Uploads layered `.dxf`. Deterministically compiles `PartSpec`, `model.step`, `preview.glb`, and `drawing.pdf` (zero AI calls). |
| `POST` | `/api/jobs/from-step` | Uploads an existing 3D `.step` file. Skips Vision, runs DFM validation (`verifier.py`), applies CAM color metadata, and generates `preview.glb` + optional `drawing.pdf`. |
| `POST` | `/api/jobs/{job_id}/drawing` | Generates or regenerates the EN ISO 2D technical drawing from the job's current `PartSpec` (optional overrides: `sheet_size`, `scale`, `language`, `engine`). |
| `GET` | `/api/jobs/{job_id}/artifacts/drawing.pdf` | Downloads the vector EN ISO PDF drawing (`.svg` and `.dxf` served on the same artifacts route, versioned alongside `model.step`). |

---

## 7. Phased Execution Plan & Round-Trip Benchmark Specification

Execute in this strict order so Phase 1 and Phase 2 immediately provide a working workshop output **and** an objective measurement harness before building Phase 3:

### Phase 1 — EN ISO Drawing Generator (`PartSpec` + B-Rep -> Vector PDF)
- **Deliverables:** `iso_template.py`, `drawing_engine.py`, schema v1.1 fields, `POST /api/jobs/{job_id}/drawing`, UI `"ISO-tekening"` button saving `drawing.pdf` next to `model.step` for each revision.
- **Definition of Done:**
  - Compiles `Montageplaat_A12` into a vector `drawing.pdf` with A4/A3 EN ISO 5457 border, EN ISO 7200 title block, EN ISO 5456-2 First-Angle layout & symbol, EN ISO 128-2 line styles, baseline dimensions separated cleanly per view, ISO 2768-mK + ISO 13715 notes, and empty Drawn/Checked fields.
  - Automated tests verify: Top view sits below Front view, Left view sits right of Front view, all dimensions are extractable via `PyMuPDF`, zero text-to-geometry overlaps occur, and re-running the generated PDF through extraction recovers the matching `PartSpec`.

### Phase 2 — Automated Round-Trip Benchmark Suite (Milestone 2)
- **Deliverables:** `roundtrip_bench.py`, `test_roundtrip.py`, benchmark Markdown/JSON reporting under `data/benchmarks/<timestamp>/report.md`.
- **Execution Flow:**
  1. Procedurally generate $N$ realistic `PartSpec` fixtures spanning stock sizes, outer fillets, rectangular/circular pockets, through/blind/counterbored/threaded holes, slots, and chamfers.
  2. Compile each fixture directly to `ground_truth.step` (recording exact volume $V_{\text{gt}}$ and bounding box) and render `synthetic_iso.pdf` via `drawing_engine.py`.
  3. Run `synthetic_iso.pdf` through the full `pipeline.py` flow to produce `predicted_spec` and `predicted.step`.
  4. **Geometric & Order-Independent Feature Diff (DO NOT use naive JSON equality):**
     - **Solid Validity:** `predicted.step` loads as 1 valid manifold solid.
     - **Bounding Box Error:** Max deviation on $(dx, dy, dz)$ within $\pm 0.05\text{ mm}$.
     - **Volume Error:** $\frac{\vert{}V_{\text{pred}} - V_{\text{gt}}\vert{}}{V_{\text{gt}}} < 0.5\%$.
     - **Feature Matching:** Match features by `type` using nearest-neighbor center/dimension pairing within $\pm 0.10\text{ mm}$ regardless of array ordering; report precision & recall per feature type.
- **Strategic A/B Benchmarking:**
  - Support CLI flags to compare prompt versions and **DeepSeek Thinking Mode ON (~150 s) vs. OFF (~6 s)** side by side on accuracy, latency, and API cost.
  - Always include a separate section in the benchmark report for **real workshop customer PDFs** (`backend/tests/fixtures/`) so accuracy is never overfitted to clean synthetic PDFs.

### Phase 3 — Paper Sketch Ingestion + DXF & Direct STEP Fast Paths
- **Deliverables:** `sketch_template.py`, `sketch_ingestor.py`, `dxf_parser.py`, `step_ingestor.py`, mobile camera capture + `"Download Schetssjabloon (A4)"` button + `MissingDimensionsModal.tsx` in frontend.
- **Definition of Done:**
  - Tilted test photos of the printed ArUco A4 template rectify cleanly with OpenCV and crop exact view boxes.
  - Extracted sketch specs flag all dimensions `< 0.85` confidence for mandatory mobile Spec-tab confirmation and halt in `awaiting_dimensions` if thickness or depths are missing.
  - Benchmark DeepSeek Files API `text_recognition` / `rich_layout_recognition` on 5 handwritten test sketches to decide whether to enable it by default.
  - Layered DXF upload (`CONTOUR_T20`, `POCKET_D8`, `HOLE_THRU`) and direct STEP upload both succeed with zero Vision AI calls.

### Phase 4 (Optional) — Live 2D SVG Preview in Spec Editor
- Render a live reactive 2D Top/Front SVG preview next to the Spec form fields so operators can draft simple 2.5D parts directly inside the web app.

---

## 8. Explicit Limits & Safety Boundaries

1. **No Guessing Missing Sketch Dimensions:** A hand sketch is only as complete as the numbers written on it. Missing thicknesses, depths, or hole specs are surfaced in `missing_dimensions` / `ambiguities` — never fabricated.
2. **GD&T & Surface Roughness Scope (v1):** EN ISO 1101 geometric tolerances and EN ISO 21920-1 surface finish specs are carried as structured metadata and drawing text notes; they are not modeled as 3D surface deviations.
3. **Human Sign-Off Required:** Every auto-generated ISO drawing is stamped `DRAFT` until an engineer or machinist verifies the dimensions and signs off ("Getekend / Gecontroleerd") before releasing to production.