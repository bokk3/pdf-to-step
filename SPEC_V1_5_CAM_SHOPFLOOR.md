# Version 1.5 Specification — CAM-Ready Output, Shopfloor Integration & Batch Intake

> **Prerequisite specs:** `ARCHITECTURE.md` (v1.0) and `DRAWING_ENGINE_SPEC.md` (v1.1)
>
> **Release Goal:** Upgrade validated `PartSpec v1.1` outputs from "geometrically correct STEP files" into **shopfloor-ready CAM packages**—adding real workshop tool-library DFM checks, OCCT/XCAF STEP AP214 face color coding, automated raw-stock sizing + 1-page setup sheets, QR-linked tablet 3D inspection over Tailscale, multi-page PDF batch splitting, and silent operator-correction telemetry for v2.5.

## Agent Kickoff Prompt (Copy-Paste to Start v1.5 Implementation)

```
Read ARCHITECTURE.md, DRAWING_ENGINE_SPEC.md, and SPEC_V1_5_CAM_SHOPFLOOR.md in full before writing code.
Implement Version 1.5 strictly adhering to all existing hard rules (B-Rep only, PartSpec single source of truth, local 127.0.0.1 binding behind Tailscale, backwards compatibility with PartSpec v1.1).
Work in the parallel streams defined in Section 7. Do not break existing v1.0/v1.1 unit tests.
Every new service MUST ship with deterministic pytest coverage before marking your stream Done.

```

## 1. Non-Negotiable v1.5 Principles

1. **Zero Breaking Changes to `PartSpec v1.1`:** All v1.5 features MUST operate on existing `PartSpec v1.1` payloads without requiring schema migration of previously completed jobs.

2. **Deterministic Shopfloor Math:** Tool reach checks, XCAF face coloring, raw stock sizing, mass estimation, and QR generation are 100% deterministic Python. Zero extra LLM calls are allowed in these modules.

3. **True B-Rep Surface Coloring (OCCT XCAF):** Never convert to mesh or split a solid into loose shells to color faces. Always export a single watertight `TopoDS_Solid` with `XCAFDoc_ColorSurf` attributes via `STEPCAFControl_Writer` (ISO 10303 AP214).

4. **Tailscale-First Shopfloor Access:** QR codes resolve exclusively to `PUBLIC_TAILSCALE_URL` configured in `.env`. The shopfloor tablet route is strictly read-only to prevent accidental spec edits at the machine.

5. **Silent Data Capture for v2.5:** Every operator spec confirmation/edit logs a lightweight diff record in SQLite so the v2.5 self-learning loop starts with real historical data on day one.

## 2. Feature Specifications

### 2.1 Workshop Tool Library & DFM v2 (`tool_library.py` + `verifier.py`)

Replace generic geometric heuristics with validation against the workshop's actual cutter magazine.

* **Persistent Store:** `data/config/tools.json` (editable via REST API and UI settings panel, backed up on save).

* **Importers:**

  * Native JSON upload/edit (`tools.json`).

  * **Fusion 360 `.tools` / CSV importer:** Parse Fusion 360 tool library JSON exports (extracting diameter, flute length, overall length, corner radius, tool type, flute count) and map into the internal `ToolSpec` schema.

* **Material Group Normalization:** Map free-text `PartSpec.material` strings (e.g., `"AlMgSi1 (6082-T6)"`, `"Staal S355"`, `"RVS 304"`, `"POM-C"`) to canonical groups: `aluminum`, `steel`, `stainless`, `plastic`, `brass_copper`, `unknown`.

* **DFM v2 Checks (appended to `DFMReport`):**

  1. **Pocket Corner & Reach Match:** For each pocket with `corner_radius` $R_p$ and `depth` $D_p$, find all in-magazine endmills matching the material group where:
     

     $$
     d \le 2 \cdot R_p \quad \text{and} \quad L_{\text{flute}} \ge D_p
     $$

     * If none exist, emit `ERROR` / `WARNING` with a concrete suggestion: find the smallest available endmill with $L_{\text{flute}} \ge D_p$ and report the exact required radius increase (e.g., *"Pocket `feat_pocket_1` (depth 32 mm, R3.0): no Ø6 endmill in magazine with flute length* $\ge$ *32 mm. Suggested fix: increase `corner_radius` to 4.0 mm for available `T04 — VHM Ø8 L38`."*).

  2. **Hole & Thread Tool Match:** Verify a matching drill diameter (within $\pm 0.05\text{ mm}$), tap (`thread_spec`, e.g. `M6`, `M8`), or reamer (when `tolerance` matches tight ISO fits like `H7`) exists in `tools.json` with sufficient flute length.

  3. **Slot Width Match:** Verify an endmill with $d \le \text{slot.width}$ and $L_{\text{flute}} \ge \text{slot.depth}$ is available.

  4. **Recommended Tool List Output:** Emit `recommended_tools: list[ToolAssignment]` linking each `feature.id` to a concrete `tool.id` (consumed directly by the Setup Sheet).

### 2.2 STEP AP214 Face Color Coding via OCCT XCAF (`step_xcaf_exporter.py`)

Color-code critical manufacturing faces inside `model.step` so CAM software (Fusion 360, Mastercam, HyperMILL, Siemens NX, SolidCAM) and programmers recognize threads, tight fits, and datums immediately.

* **Critical Implementation Note (OCCT Pitfall):** Simply assigning `.color` to a `build123d` sub-face inside a `Solid` does not reliably persist per-face colors on STEP export. Implement a dedicated XCAF exporter using `OCP` directly:

  1. Create a `TDocStd_Document("MDTV-XCAF")` and obtain `ShapeTool` (`XCAFDoc_DocumentTool.ShapeTool_s`) and `ColorTool` (`XCAFDoc_DocumentTool.ColorTool_s`).

  2. Add the compiled `TopoDS_Solid` via `ShapeTool.AddShape(solid, False)`.

  3. Match faces deterministically against `PartSpec` features using geometric signatures on `TopExp_Explorer(solid, TopAbs_FACE)`:

     * **Cylindrical faces (`GeomAbs_Cylinder`):** Match axis position `(x, y)` within `0.05 mm` and radius within `0.05 mm` against `PartSpec.features` (`hole` with `thread`, `hole` with `tolerance`, `pocket_circle`).

     * **Planar faces (`GeomAbs_Plane`):** Match top datum face ($z = z_{\max}$), pocket floors, or tight-tolerance datum planes.

  4. Register each matched `TopoDS_Face` as a sub-shape and bind its `Quantity_Color` with `ColorTool.SetColor(face_label, color, XCAFDoc_ColorSurf)`.

  5. Configure `Interface_Static.SetCVal_s("write.step.schema", "AP214")` and export via `STEPCAFControl_Writer` with `SetColorMode(True)` and `SetNameMode(True)`.

* **Default Workshop Color Convention (`data/config/cam_colors.json`, configurable):**

| Feature Category | Color Name | RGB (0–255) | Hex | CAM Meaning | 
| ----- | ----- | ----- | ----- | ----- | 
| Threaded hole bore | Royal Blue | `(0, 102, 255)` | `#0066FF` | Tap / thread mill required | 
| Tight fit (`H7`, `H6`, `g6`, `±0.02` or tighter) | Signal Red | `(230, 30, 30)` | `#E61E1E` | Ream / bore / precision finish | 
| Fine surface roughness (`Ra <= 1.6`) | Amber Yellow | `(245, 190, 0)` | `#F5BE00` | Finishing pass required | 
| Counterbore / Countersink seat | Purple | `(148, 66, 224)` | `#9442E0` | Secondary seating operation | 
| Primary Datum / Face-mill reference | Emerald Green | `(30, 180, 90)` | `#1EB45A` | Datum / flatness reference | 
| Default uncolored body | Neutral Gray | `(185, 190, 196)` | `#B9BEC4` | General ISO 2768-mK | 

* **Preview Sync:** Export the exact same per-face color mapping into `preview.glb` vertex/face materials so the browser Three.js viewer and Shopfloor tablet view match the STEP file visually.

### 2.3 Raw Stock Advisor & 1-Page Setup Sheet PDF (`stock_catalog.py` + `setup_sheet.py`)

* **Stock Catalog (`data/config/stock_catalog.json`):** Stores standard workshop bar/plate dimensions per material group, density $\rho$ ($\text{kg/m}^3$), and default machining allowances:

  * Default allowances (overridable): `xy_allowance_mm = 2.0` per side (`+4.0 mm` total on X and Y), `z_top_face_mm = 1.0`, `z_clamp_margin_mm = 4.0` (extra height for vise grip / op-2 deck-off).

* **Stock Solver:**

  * Computes minimum required saw blank: $(X + 2 a_{xy}) \times (Y + 2 a_{xy}) \times (Z + a_{z,\text{top}} + a_{z,\text{clamp}})$.

  * Selects the smallest matching standard flat-bar width/thickness (or plate thickness) from `stock_catalog.json` and computes the saw-cut length.

  * Calculates **Raw Blank Mass** $m_{\text{raw}}$, **Finished Part Mass** $m_{\text{part}}$, and **Chip Removal Ratio**:
    

    $$
    \eta_{\text{chip}} = \frac{V_{\text{raw}} - V_{\text{part}}}{V_{\text{raw}}} \times 100\%
    $$

* **Setup Sheet Generator (`setup_sheet.py` -> `setup_sheet.pdf`):**

  * Deterministic 1-page A4 portrait vector PDF (HTML/SVG rendered via WeasyPrint/CairoSVG) containing:

    1. Header: Part name, drawing number, revision, material, job ID, and **Shopfloor QR Code**.

    2. Visual block: Isometric CAD render with clearly drawn `G54` / Datum Origin `(0, 0, 0)` triad (`X` red, `Y` green, `Z` blue).

    3. Raw Stock Box: Recommended standard profile, exact saw-cut dimensions, raw vs. finished weight.

    4. Required Tooling Table: Pulled from DFM v2 `recommended_tools` (`Tool #`, `Type`, `Ø`, `Flute L`, `Assigned Features`).

    5. Critical Inspection Callouts: Filtered list of all threaded holes, `H7` fits, and active DFM warnings.

    6. Color Legend: Miniature swatches matching the STEP AP214 CAM color convention.

### 2.4 Shopfloor QR Code & Read-Only Tablet 3D View

* **QR Code Generator (`qr_helper.py`):**

  * Uses Python `qrcode` with pure SVG path output (`qrcode.image.svg.SvgPathImage`)—zero blurry raster bitmaps.

  * Target URL: `{PUBLIC_TAILSCALE_URL}/shopfloor/{job_id}` (fallback to relative warning badge if `PUBLIC_TAILSCALE_URL` is unset).

  * Embedded automatically in:

    * `drawing.pdf` (bottom-left cell of the EN ISO 7200 title block via `iso_template.py`).

    * `setup_sheet.pdf` (top-right header).

* **Frontend Shopfloor Route (`/shopfloor/:jobId`):**

  * Touch-optimized, dark/high-contrast workshop tablet layout (no sidebars, no edit forms, zero risk of modifying specs on the shop floor).

  * Interactive Three.js viewer (`Step3DViewer.tsx` in `shopfloor` mode) displaying the color-coded `.glb` with:

    * **Cross-Section Clipping Plane Slider** (X, Y, or Z axis) to inspect internal pockets and blind hole depths.

    * **Interactive Color Legend Toggle** (highlight only threads, fits, or datums).

    * **Tap-to-Inspect Callout Drawer:** Tap any feature row in the bottom sheet to highlight its location on the 3D model and show exact depth/tolerance/tool assignment.

    * Quick buttons to open `drawing.pdf` and `setup_sheet.pdf`.

### 2.5 Multi-Page Customer PDF Batch Splitter (`pdf_splitter.py`)

Customer RFQ packages frequently bundle 5–30 drawings into a single PDF.

* **Upload Endpoint:** `POST /api/batches` (accepts multi-page `.pdf`).

* **Deterministic Splitting Pipeline:**

  1. Open PDF with `PyMuPDF`. For each page $p$, crop the bottom-right title-block zone (bottom 25% height, right 45% width) and extract embedded text spans.

  2. Extract candidate `part_number` / `drawing_number` via regex patterns (configurable in `.env` / `config.py`) and sheet indicators (e.g., `"Sheet 1 of 2"`, `"Blad 2/2"`).

  3. Group pages sharing the same `part_number` (or multi-sheet continuations) into distinct logical parts.

  4. **Fallback for Scanned / Raster Multi-Page PDFs:** If title-block text extraction yields fewer than 15 characters on a page, send low-res title-block crops in a single batched `deepseek-flash` call asking only for `[{page_index, part_number, sheet_index, is_continuation}]`.

  5. Write each grouped part as its own PDF under `data/jobs/<child_job_id>/original.pdf`, create child jobs linked to `batch_id` in SQLite, and enqueue them with bounded concurrency (`MAX_CONCURRENT_JOBS=2` default to protect workstation RAM/CPU during OCCT + DeepSeek calls).

* **Batch Review UI:** Show a live batch overview table (Part Number, Pages, Status, DFM Status, Quick Open) with a **"Download All Ready STEPs (.zip)"** action.

### 2.6 Silent Operator Correction Telemetry (v2.5 Foundation)

* **SQLite Table (`spec_corrections`):**

  * Whenever a job completes AI extraction (`pipeline.py`), snapshot `ai_initial_spec_json` on the job record.

  * Whenever an operator saves edits via `PATCH /api/jobs/{job_id}/spec` or clicks **"Mark Verified"**, compute a structured JSON diff (`changed_feature_types`, `field_diffs`, `was_modified`) and insert a row into `spec_corrections`.

* **Minimal Read-Only Stats Endpoint (`GET /api/metrics/accuracy`):** Returns total verified jobs, untouched-pass rate, and correction counts per `feature.type`—ready for v2.5's golden-dataset exporter.

## 3. Data Contracts & Configuration Schemas

### 3.1 `data/config/tools.json` Schema (`ToolSpec`)

```
{
  "schema_version": "1.0",
  "tools": [
    {
      "id": "T01",
      "name": "VHM 3-Flute Alu Ø8",
      "tool_type": "flat_endmill",
      "diameter_mm": 8.0,
      "flute_length_mm": 38.0,
      "overall_length_mm": 80.0,
      "corner_radius_mm": 0.0,
      "flutes": 3,
      "materials": ["aluminum", "plastic", "brass_copper"],
      "thread_spec": null,
      "tolerance_class": null,
      "in_magazine": true
    },
    {
      "id": "T12",
      "name": "Spiraaltap M8 ISO2/6H",
      "tool_type": "tap",
      "diameter_mm": 8.0,
      "flute_length_mm": 22.0,
      "overall_length_mm": 90.0,
      "corner_radius_mm": 0.0,
      "flutes": 3,
      "materials": ["aluminum", "steel"],
      "thread_spec": "M8",
      "tolerance_class": "6H",
      "in_magazine": true
    }
  ]
}

```

### 3.2 `data/config/stock_catalog.json` Schema

```
{
  "schema_version": "1.0",
  "default_allowances": {
    "xy_per_side_mm": 2.0,
    "z_top_face_mm": 1.0,
    "z_clamp_margin_mm": 4.0
  },
  "materials": {
    "aluminum": {
      "display_name": "AlMgSi1 (6082-T6)",
      "density_kg_m3": 2700,
      "standard_flat_bars_mm": [
        [20, 10], [50, 20], [100, 20], [100, 25], [120, 25], [150, 30], [200, 25], [200, 40]
      ],
      "standard_plate_thicknesses_mm": [10, 15, 20, 25, 30, 40, 50, 60]
    },
    "steel": {
      "display_name": "S355J2 / C45",
      "density_kg_m3": 7850,
      "standard_flat_bars_mm": [
        [50, 20], [100, 20], [100, 25], [120, 30], [150, 30]
      ],
      "standard_plate_thicknesses_mm": [10, 15, 20, 25, 30, 40]
    }
  }
}

```

### 3.3 `DFMReport` v2 Extension (Backwards-Compatible)

Add optional fields to existing `DFMReport`:

```
{
  "passed": true,
  "warnings": [],
  "tool_checks": [
    {
      "feature_id": "feat_pocket_1",
      "status": "ok",
      "assigned_tool_id": "T01",
      "assigned_tool_name": "VHM 3-Flute Alu Ø8",
      "message": "Matched Ø8 endmill (flute 38 mm >= pocket depth 8 mm)",
      "suggested_fix": null
    }
  ],
  "stock_recommendation": {
    "material_group": "aluminum",
    "profile_type": "flat_bar",
    "recommended_blank_mm": { "x": 205.0, "y": 120.0, "z": 25.0 },
    "raw_mass_kg": 1.66,
    "part_mass_kg": 0.89,
    "chip_removal_pct": 46.4
  }
}

```

## 4. Repository Structure Additions

```
pdf-to-step/
├── data/
│   └── config/
│       ├── tools.json                 # Editable workshop tool library
│       ├── stock_catalog.json         # Raw stock profiles + densities
│       └── cam_colors.json            # AP214 face color palette
├── backend/
│   ├── app/
│   │   ├── api/
│   │   │   ├── tools.py               # Tool library CRUD + Fusion import
│   │   │   ├── batches.py             # Multi-page PDF batch upload, status, ZIP export
│   │   │   └── metrics.py             # Accuracy telemetry summary
│   │   ├── schemas/
│   │   │   ├── tooling.py             # ToolSpec, StockRecommendation, ToolAssignment
│   │   │   └── batch.py               # BatchJob, BatchItem schemas
│   │   └── services/
│   │       ├── tool_library.py        # Load/save tools.json + Fusion 360 importer
│   │       ├── step_xcaf_exporter.py  # OCCT XCAF AP214 face color exporter + GLB sync
│   │       ├── stock_catalog.py       # Raw blank solver & mass calculator
│   │       ├── qr_helper.py           # Vector SVG QR code generator
│   │       ├── setup_sheet.py         # 1-page A4 Setup Sheet PDF builder
│   │       ├── pdf_splitter.py        # Multi-page PDF title-block splitter
│   │       └── telemetry.py           # Operator diff logger (spec_corrections)
│   └── tests/
│       ├── test_tool_dfm.py           # Tool reach, radius suggestion, Fusion import tests
│       ├── test_xcaf_colors.py        # Verify AP214 STEP contains XCAF face colors on re-import
│       ├── test_stock_setup.py        # Blank sizing, mass math, setup_sheet.pdf text tests
│       └── test_pdf_splitter.py       # Multi-page grouping + bounded concurrency tests
└── frontend/
    └── src/
        ├── pages/
        │   ├── ShopfloorView.tsx      # Read-only tablet 3D inspection route (/shopfloor/:jobId)
        │   ├── BatchDashboard.tsx     # Multi-page batch progress & bulk ZIP download
        │   └── ToolLibraryPage.tsx    # Manage tools.json + Fusion import UI
        └── components/
            ├── ClippingControls.tsx   # X/Y/Z cross-section slider for Three.js viewer
            └── CamColorLegend.tsx     # Interactive color filter overlay

```

## 5. New REST & SSE API Endpoints

| Method | Endpoint | Description | 
| ----- | ----- | ----- | 
| `GET` | `/api/config/tools` | Fetch current workshop `tools.json`. | 
| `PUT` | `/api/config/tools` | Replace/save `tools.json` (Pydantic validated). | 
| `POST` | `/api/config/tools/import-fusion` | Upload Fusion 360 `.tools` / `.json` / `.csv` library and merge into `tools.json`. | 
| `GET` | `/api/config/stock` | Fetch `stock_catalog.json` and allowances. | 
| `PUT` | `/api/config/stock` | Update standard stock profiles and allowances. | 
| `POST` | `/api/jobs/{job_id}/setup-sheet` | Generate/regenerate `setup_sheet.pdf` from current `PartSpec`, DFM v2, and stock advisor. | 
| `GET` | `/api/jobs/{job_id}/artifacts/setup_sheet.pdf` | Download 1-page Setup Sheet PDF. | 
| `GET` | `/api/jobs/{job_id}/shopfloor` | Lightweight read-only payload (`part_spec`, `dfm_report`, `color_map`, artifact URLs) for the tablet route. | 
| `POST` | `/api/batches` | Upload multi-page customer PDF; splits into child jobs and returns `{"batch_id": "...", "job_count": N}` (`HTTP 202`). | 
| `GET` | `/api/batches/{batch_id}` | Batch metadata + array of child job statuses and DFM summaries. | 
| `GET` | `/api/batches/{batch_id}/events` | SSE stream pushing aggregate batch progress and per-child status updates. | 
| `GET` | `/api/batches/{batch_id}/download-zip` | Streams a `.zip` bundle of all ready `model.step`, `drawing.pdf`, and `setup_sheet.pdf` files organized by part name. | 
| `GET` | `/api/metrics/accuracy` | Returns silent telemetry stats (verified jobs, untouched rate, corrections per feature type). | 

### New `.env` Variables

```
PUBLIC_TAILSCALE_URL=https://cnc-workstation.your-tailnet.ts.net
MAX_CONCURRENT_JOBS=2
ENABLE_XCAF_CAM_COLORS=true

```

## 6. Parallel Work Breakdown for Coding Agents (v1.5)

### Agent A — Tool Library, DFM v2 & Stock Advisor

* **Scope:** `schemas/tooling.py`, `services/tool_library.py`, `services/stock_catalog.py`, `api/tools.py`, `verifier.py` DFM v2 extension, `tests/test_tool_dfm.py`, `tests/test_stock_setup.py`.

* **Definition of Done:** Loads default `tools.json` + `stock_catalog.json`, imports a sample Fusion 360 tool JSON cleanly, flags deep sharp pockets with concrete alternative tool suggestions from `tools.json`, computes raw blank + chip removal ratio, and passes all unit tests.

### Agent B — OCCT XCAF STEP AP214 Color Exporter & GLB Sync

* **Scope:** `services/step_xcaf_exporter.py`, `data/config/cam_colors.json`, `cad_compiler.py` integration, `tests/test_xcaf_colors.py`.

* **Definition of Done:** Exports `Montageplaat_A12` with Royal Blue `M8` threaded bore faces and Signal Red `H7` faces in `model.step` (AP214) while preserving a single valid manifold `TopoDS_Solid`. Automated pytest re-reads `model.step` via `STEPCAFControl_Reader` and asserts the expected RGB face colors are present. Syncs matching colors into `preview.glb`.

### Agent C — QR Helper, Setup Sheet PDF & Silent Telemetry

* **Scope:** `services/qr_helper.py`, `services/setup_sheet.py`, `iso_template.py` QR injection, `services/telemetry.py`, `db.py` (`spec_corrections` table), `api/metrics.py`.

* **Definition of Done:** Generates a crisp 1-page A4 vector `setup_sheet.pdf` with datum triad iso render, stock blank box, tool table, critical tolerances, CAM color legend, and scannable vector QR pointing to `PUBLIC_TAILSCALE_URL/shopfloor/{job_id}`. Injects the same QR into `drawing.pdf`. Logs diffs on every `PATCH /spec`.

### Agent D — Multi-Page PDF Batch Splitter & Frontend v1.5 Views

* **Scope:** `services/pdf_splitter.py`, `api/batches.py`, `tests/test_pdf_splitter.py`, and frontend routes `ShopfloorView.tsx`, `BatchDashboard.tsx`, `ToolLibraryPage.tsx`, `ClippingControls.tsx`, `CamColorLegend.tsx`.

* **Definition of Done:** Splitting a synthetic 6-page PDF containing 3 parts produces 3 child jobs processed with `MAX_CONCURRENT_JOBS` throttling and exports a valid `.zip` package. `/shopfloor/:jobId` loads read-only on mobile/tablet viewport widths with working X/Y/Z clipping slider and CAM color highlighting.

## 7. Release Acceptance Checklist (v1.5 Gate)

* \[ \] Re-importing an exported `model.step` into **Fusion 360 / Siemens NX / SolidWorks** displays the exact AP214 face colors on threaded and `H7` bores with 1 solid body and zero geometry errors.

* \[ \] Uploading a part with a 35 mm deep pocket and R2 corners triggers a specific DFM v2 warning referencing an actual tool from `tools.json`.

* \[ \] Scanning the QR code printed on `setup_sheet.pdf` or `drawing.pdf` from a phone/tablet on Tailscale opens `/shopfloor/:jobId` in read-only mode with smooth 3D orbit and cross-section slicing.

* \[ \] Uploading a 10-page multi-part customer PDF splits cleanly into individual jobs and produces a downloadable `.zip` of STEPs + Setup Sheets.

* \[ \] Editing a dimension in the Spec tab records a structured diff row in SQLite `spec_corrections`.