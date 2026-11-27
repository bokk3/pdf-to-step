# PDF-to-STEP CNC Generator — Architecture & Agent Specification

## 1. Mission & Non-Negotiable Principles
Build a local-first web application behind a **Tailscale VPN** that converts 2D technical PDF drawings into CNC-ready **B-Rep STEP files** (`.step`, ISO 10303 AP214/AP242) with minimal human rework.

### Hard Rules for All Agents
1. **No Mesh-to-STEP Shortcuts:** All 3D geometry MUST be native B-Rep built with `build123d` (Open CASCADE / OCCT). Never use mesh-based libraries (OpenSCAD, JSCAD, Three.js geometry) to export STEP files; CAM software requires exact analytical surfaces (arcs, cylinders, planes).
2. **Schema-First Architecture:** Never pass raw LLM text directly between modules. All extraction, UI editing, and CAD compilation steps communicate strictly via the versioned Pydantic v2 schema `PartSpec` (`backend/app/schemas/part_spec.py`).
3. **Deterministic First, LLM Second:** Standard 2.5D prismatic milling features (~90% of CNC shop parts) MUST be compiled by deterministic Python code to eliminate code hallucinations. LLM-generated `build123d` code is strictly a fallback for `complex_3d` features and MUST execute inside an isolated subprocess sandbox with a 15-second timeout.
4. **Local-Only Binding:** Bind the Python server exclusively to `127.0.0.1:8000`. All remote access happens solely through `tailscale serve`. Never expose `DEEPSEEK_API_KEY` to the browser.
5. **Full Job Traceability:** Every job stores its original PDF, extracted vector/text dump, high-res image tiles, `PartSpec` revisions, generated `.step`, preview `.glb`, 4-view orthographic verification PNGs, and DFM report under `data/jobs/<job_id>/`.

---

## 2. The 6-Step High-Reliability Pipeline

To maximize accuracy on real CNC drawings, AI never blindly guesses CAD code directly from a full-page PDF image. Instead, every uploaded PDF moves through six controlled stages:

### Stage 1 — PDF Preprocessing & Vector Extraction (`PyMuPDF` / `fitz`)
* **Deterministic extraction:** Parse all embedded PDF vectors (lines, circles, arcs) and text blocks with exact `(x, y)` bounding-box coordinates.
* **High-resolution rendering & tiling:** Render the full drawing at 300 DPI **and** slice automatic high-res overlapping crops (4–6 quadrant/view tiles + a dedicated title-block crop). This prevents tiny callouts such as `Ø6.5 H7` from becoming unreadable when the vision model downscales large sheets.

### Stage 2 — Feature Extraction to Strict JSON (`deepseek-flash`)
* Send the full-sheet overview, high-res tiles, and PyMuPDF-extracted text spans to `deepseek-flash` with Thinking Mode and structured JSON output enabled.
* The model does **not** write CAD code here. It populates the strict `PartSpec` JSON schema (stock dimensions, datum origin, outer profile, pockets, holes, threads, tolerances) and assigns a `confidence` score (`0.0–1.0`) to every dimension and feature.

### Stage 3 — Hybrid 3D Generation to STEP & GLB (`build123d` / OCCT)
* **Layer 1 (Deterministic Compiler — ~90% of milling work):** Fixed Python code translates standard 2.5D features (outer profile, pockets, through/blind/counterbored holes, slots, bosses, shoulders, chamfers, fillets) directly into `build123d` operations. Zero hallucination risk.
* **Layer 2 (AI Code Fallback — Complex 3D):** Triggered only when `requires_complex_3d_fallback` is `true` (e.g., lofts, angled compound planes, freeform surfaces). `deepseek-v4-pro` generates a `build123d` script from the `PartSpec` JSON and executes it in an isolated subprocess sandbox (`cad_sandbox.py`).
* Exports both a CNC-ready `.step` file and a lightweight `.glb` mesh for browser inspection.

### Stage 4 — Geometry & CNC-DFM Validation (`verifier.py`)
* Runs automated gatekeeper checks before any file is marked ready: manifold solid check (`is_valid`), single-solid verification, bounding-box dimensional accuracy (`±0.01 mm`), minimum internal pocket corner radii, depth-to-diameter ratios, and thin-wall checks.
* Renders 4 orthographic verification views (Top, Front, Right, Isometric) as a composite PNG (`ortho_check.png`).

### Stage 5 — Self-Healing Visual Verification Loop (Max 3 Iterations)
* Sends the rendered orthographic PNGs alongside the original PDF reference to `deepseek-flash` acting as a visual critic.
* If OCCT raises a kernel error or the critic spots missing features / mismatched geometry, the structured error log and visual discrepancy report are routed back to Stage 2 (to correct `PartSpec`) or Stage 3 (to repair fallback code) for up to 3 automatic retries.

### Stage 6 — Operator Verification & Instant Rebuild (Browser UI)
* Displays a split-screen workstation view over Tailscale: original PDF on the left, interactive Three.js 3D `.glb` viewer on the right, and an editable `PartSpec` table underneath.
* Any dimension with `confidence < 0.85` is highlighted in amber alongside DFM warnings.
* Editing a value in the UI sends `PATCH /api/jobs/{job_id}/spec` and triggers an immediate deterministic CAD rebuild in under 1 second—without making a new AI API call.

---

## 3. Network Architecture Behind Tailscale VPN

Run both backend and frontend on the local workstation using a **single-origin reverse proxy via `tailscale serve`**:

```text
[Authorized Workshop PC / Tablet on Tailnet]
                    │
         HTTPS (Automatic Tailscale TLS)
   https://<workstation>.<tailnet>.ts.net
                    │
            tailscale serve --bg 8000
                    │
        [FastAPI Server — 127.0.0.1:8000]
         ├── /api/*        -> REST & SSE Pipeline Endpoints
         └── /*            -> Built React Frontend (/frontend/dist)
                    │
         [Local Processing + Outbound HTTPS]
         ├── build123d / OCCT (Local CPU)
         ├── SQLite + data/jobs/ (Local Disk)
         └── DeepSeek API (Key stored strictly in server .env)
```

* **Zero open router ports & zero CORS friction:** FastAPI serves both the API and the compiled static frontend on `127.0.0.1:8000`.
* **One-command HTTPS exposure:**
  ```bash
  tailscale serve --bg 8000
  ```
* **Access control:** Restrict access via Tailscale ACLs (e.g., `tag:cnc-workstation` accessible only to `group:werkplaats`).
* **Local development workflow:** Run Vite on port `5173` with a dev proxy forwarding `/api` requests to `http://127.0.0.1:8000` for hot module replacement. In production, serve `/frontend/dist` directly from FastAPI.

---

## 4. Tech Stack & DeepSeek API Policy

| Layer | Technology | Rationale |
|---|---|---|
| **Backend API** | Python 3.12, FastAPI, Uvicorn, Pydantic v2 | Native OCCT bindings, async SSE streaming, strict validation |
| **Job Queue & State** | SQLite (`aiosqlite`) + `ProcessPoolExecutor` | Zero external infra (no Redis or Docker required on workstation) |
| **PDF Parsing** | `PyMuPDF` (`fitz`) + `Pillow` | Fast vector/text coordinate extraction + 300 DPI tiling |
| **CAD Kernel** | `build123d` (OCCT/OCP) + `trimesh` | Native B-Rep STEP export, bounding-box validation, GLB preview |
| **AI Provider** | DeepSeek API (`openai` Python SDK, `base_url="https://api.deepseek.com"`) | Cost-effective multimodal vision + high-accuracy code reasoning |
| **Frontend** | React 19, TypeScript, Vite, Tailwind CSS | Fast UI iteration, strict TypeScript mirror of `PartSpec` |
| **3D & PDF Viewers** | `@react-three/fiber`, `@react-three/drei`, `pdfjs-dist` | Split-screen PDF inspection vs. orbitable 3D preview |
| **Networking** | Tailscale (`tailscale serve --bg 8000`) | Zero-port-forwarding encrypted HTTPS access |

### Critical DeepSeek API Integration Guidelines (For Agent 2)
* **Model Routing (configure via `.env`):**
  * `DEEPSEEK_VISION_MODEL=deepseek-flash` (powered by DeepSeek-V4.1-Flash): Use for **all vision tasks** (PDF extraction and visual verification). Supports native text + image inputs, thinking mode, and structured JSON output.
  * `DEEPSEEK_CODE_MODEL=deepseek-v4-pro`: Use **exclusively for text-only complex 3D code fallback** (receives `PartSpec` JSON + kernel error logs; does not accept image inputs).
  * *Do not hardcode legacy model IDs:* `deepseek-chat` and `deepseek-reasoner` are retired.
* **SDK Compatibility:** Use the official OpenAI Python SDK configured with `base_url="https://api.deepseek.com"` and `api_key=settings.deepseek_api_key`.
* **Image Resolution Limit Workaround (Mandatory Tiling):** Because the Vision API downscales large images to roughly a `1300 × 1300` pixel equivalent, **always** send:
  1. The full sheet overview,
  2. 4–6 overlapping high-res cropped tiles + a cropped title block, and
  3. The raw PyMuPDF text + coordinate dump.
  This combination is the single biggest driver of 95%+ dimensional accuracy on real CNC drawings.
* **Files API Reuse:** During the multi-iteration visual verification loop (Stage 5), upload PDF page images once via the DeepSeek Files API and reference them by `file_id` rather than re-transmitting base64 payloads on every retry.

---

## 5. Repository Structure

```text
pdf-to-step/
├── ARCHITECTURE.md                  # Source of truth for all agents
├── .env.example                     # DEEPSEEK_API_KEY, model names, data dir
├── scripts/
│   ├── dev.sh                       # Starts FastAPI + Vite concurrently
│   └── start_tailscale.sh           # Runs uvicorn on 127.0.0.1:8000 + tailscale serve
├── backend/
│   ├── pyproject.toml
│   ├── app/
│   │   ├── main.py                  # FastAPI app, static file serving, lifespan
│   │   ├── config.py                # Pydantic Settings (.env loader)
│   │   ├── db.py                    # SQLite job store (aiosqlite)
│   │   ├── api/
│   │   │   ├── jobs.py              # Upload, status, SSE stream, rebuild, download
│   │   │   └── health.py            # Healthcheck + OCCT/DeepSeek connectivity check
│   │   ├── schemas/
│   │   │   └── part_spec.py         # Canonical PartSpec v1 + DFMReport models
│   │   └── services/
│   │       ├── pdf_parser.py        # PyMuPDF text/vector extraction + smart tiling
│   │       ├── deepseek_client.py   # Vision extraction, visual critic, code fallback
│   │       ├── cad_compiler.py      # Deterministic build123d 2.5D compiler
│   │       ├── cad_sandbox.py       # Isolated subprocess executor (15s timeout)
│   │       ├── verifier.py          # Solid checks, bbox check, DFM rules, ortho renders
│   │       └── pipeline.py          # Orchestrator with max 3 self-healing retries
│   └── tests/
│       ├── fixtures/                # Real workshop PDFs + expected PartSpec JSONs
│       ├── test_compiler.py         # Unit tests verifying STEP validity & ±0.01 mm dims
│       └── test_dfm.py              # Unit tests for corner radius & DFM rules
└── frontend/
    ├── package.json
    ├── vite.config.ts               # Proxy /api -> http://127.0.0.1:8000 in dev
    └── src/
        ├── types/partSpec.ts        # Strict TS mirror of backend PartSpec schema
        ├── api/client.ts            # Fetch wrapper + EventSource SSE hook
        └── components/
            ├── UploadDropzone.tsx
            ├── PipelineProgress.tsx
            ├── PdfViewer.tsx        # Zoom/pan PDF sheet viewer
            ├── Step3DViewer.tsx     # Three.js GLB viewer with grid, axes, wireframe
            ├── FeatureEditor.tsx    # Editable table flagging confidence < 0.85
            └── DfmWarningsPanel.tsx
```

---

## 6. Canonical Data Contract (`PartSpec` v1)

All backend and frontend agents MUST conform to this exact JSON structure. All dimensions are normalized to millimeters (`mm`) on ingestion.

```json
{
  "schema_version": "1.0",
  "part_name": "Montageplaat_A12",
  "units": "mm",
  "material": "AlMgSi1 (6082-T6)",
  "general_tolerance": "ISO 2768-mK",
  "datum_origin": "bottom_left_top_face",
  "stock": {
    "length_x": 200.0,
    "width_y": 100.0,
    "thickness_z": 20.0,
    "confidence": 0.98
  },
  "outer_profile": {
    "type": "rounded_rectangle",
    "corner_radius": 5.0,
    "points_2d": null,
    "confidence": 0.95
  },
  "features": [
    {
      "id": "feat_pocket_1",
      "type": "pocket_rect",
      "face": "top",
      "center_x": 100.0,
      "center_y": 50.0,
      "length_x": 120.0,
      "width_y": 60.0,
      "depth": 8.0,
      "corner_radius": 6.0,
      "confidence": 0.92,
      "source_note": "Top view central pocket"
    },
    {
      "id": "feat_hole_1",
      "type": "hole",
      "face": "top",
      "center_x": 15.0,
      "center_y": 15.0,
      "diameter": 8.0,
      "depth": null,
      "through_all": true,
      "thread": "M8",
      "counterbore": null,
      "tolerance": "H7",
      "confidence": 0.78,
      "source_note": "4x Ø8 callout slightly blurred"
    }
  ],
  "edge_treatments": [
    {
      "id": "edge_1",
      "type": "chamfer",
      "size": 1.0,
      "target": "top_outer_edges",
      "confidence": 0.90
    }
  ],
  "requires_complex_3d_fallback": false,
  "ambiguities": [
    "Verify whether the 4 corner holes are through-holes or 15mm blind."
  ]
}
```

### Supported Deterministic `features[].type` Values (v1)
* `hole` — through, blind, counterbore, or countersink
* `pocket_rect` — rounded rectangle pocket
* `pocket_circle` — circular pocket
* `pocket_polygon` — arbitrary 2D closed wire with optional corner fillets
* `slot` — straight obround slot defined by start/end `(x, y)` and width
* `boss_rect` / `boss_circle` — raised extrusion on a target face
* `step_cut` — open edge cutout / shoulder

---

## 7. REST & SSE Streaming API Contract

| Method | Endpoint | Description |
|---|---|---|
| `POST` | `/api/jobs` | Multipart upload of PDF (`file`). Returns `{"job_id": "...", "status": "queued"}` immediately (`HTTP 202`). |
| `GET` | `/api/jobs` | List recent jobs with status, filename, timestamps, and DFM pass/fail summary. |
| `GET` | `/api/jobs/{job_id}` | Full job state: status, `part_spec`, `dfm_report`, `iteration_logs`, and artifact URLs. |
| `GET` | `/api/jobs/{job_id}/events` | Server-Sent Events (SSE) stream emitting live stage updates (`parsing_pdf`, `extracting_features`, `compiling_cad`, `verifying`, `retrying`, `ready`, `failed`). |
| `PATCH` | `/api/jobs/{job_id}/spec` | Accepts updated `PartSpec` JSON from the frontend editor. Triggers immediate deterministic CAD rebuild (skips LLM) and returns updated artifacts + DFM report. |
| `POST` | `/api/jobs/{job_id}/reextract` | Accepts optional user hint text (e.g., `"Diepte van de pocket is 12mm"`) to re-run DeepSeek extraction. |
| `GET` | `/api/jobs/{job_id}/artifacts/{filename}` | Serves `model.step`, `preview.glb`, `original.pdf`, or `ortho_check.png`. |

---

## 8. CNC DFM & Validation Rules (`verifier.py`)

Every generated `Part` MUST pass these automated checks before a job is marked `ready`:

1. **Manifold Integrity:** `part.is_valid` is `True` and `len(part.solids()) == 1` (single watertight solid, no open shells or duplicate geometry).
2. **Dimensional Integrity:** Bounding box `(dx, dy, dz)` matches `stock` dimensions within `±0.01 mm` (unless a `step_cut` intentionally reduces overall stock).
3. **Internal Corner Radius Check (CNC Milling):** Emit a `WARNING` if any internal pocket has `corner_radius < 1.5 mm` or `0 mm` (sharp non-millable corner), suggesting a minimum endmill radius or dogbone relief.
4. **Depth-to-Diameter Ratio:** Emit a `WARNING` if hole depth exceeds `5 × diameter` or pocket depth exceeds `4 × corner_radius`.
5. **Thin Wall Detection:** Emit a `WARNING` if the distance between any feature boundary and the stock edge is under `1.5 mm`.
6. **CAM Face Color Coding (Phase 2 — STEP AP214/XCAF):** Tag threaded cylindrical faces (e.g., Blue) and tight-tolerance `H7` bores (e.g., Red) in STEP XCAF metadata so CAM software (Fusion 360, Mastercam, HyperMILL, SolidCAM) and operators recognize them immediately.

---

## 9. Parallel Work Breakdown for Coding Agents

Assign one agent per workstream below. Agents coordinate strictly through this `ARCHITECTURE.md` and the `PartSpec` schema:

### Agent 1 — Core Backend, Persistence & Tailscale Deployment
* **Scope:** `backend/app/main.py`, `config.py`, `db.py`, `api/jobs.py`, `api/health.py`, `schemas/part_spec.py`, `scripts/dev.sh`, `scripts/start_tailscale.sh`, `.env.example`.
* **Definition of Done:** Uploading a PDF creates a job in SQLite, stores files under `data/jobs/<id>/`, streams mock SSE stage events, exposes all Section 7 endpoints, and serves `/frontend/dist` cleanly on `127.0.0.1:8000`.

### Agent 2 — PDF Vector/Text Parser & DeepSeek Vision Extractor
* **Scope:** `backend/app/services/pdf_parser.py`, `backend/app/services/deepseek_client.py`.
* **Definition of Done:** Given a technical PDF, extracts text + bounding boxes + vector primitives and renders the full sheet plus 4–6 overlapping high-res tiles and a title-block crop. Calls `deepseek-flash` via the OpenAI SDK (`base_url="https://api.deepseek.com"`) and returns a Pydantic-validated `PartSpec` with zero schema errors. Implements the Files API reuse and visual critic prompt comparing original PDF sheets against `ortho_check.png`.

### Agent 3 — Deterministic `build123d` Compiler, Sandbox & Verifier
* **Scope:** `backend/app/services/cad_compiler.py`, `cad_sandbox.py`, `verifier.py`, `pipeline.py`, `backend/tests/*`.
* **Definition of Done:** Compiles the Section 6 example `PartSpec` JSON into a valid `.step` and `.glb`, renders a 4-view `ortho_check.png`, passes all `pytest` dimensional assertions (`±0.01 mm`), enforces the 15s subprocess timeout for `cad_sandbox.py`, and triggers DFM warnings on sharp internal pocket corners.

### Agent 4 — React + TypeScript + Three.js Frontend
* **Scope:** `frontend/*`.
* **Definition of Done:** Responsive split-screen workstation UI featuring PDF drag-and-drop upload, live SSE pipeline progress stepper, side-by-side PDF viewer (`pdfjs-dist`) and orbitable Three.js `.glb` viewer (`@react-three/fiber`), interactive `PartSpec` editor highlighting `confidence < 0.85` in amber, instant `"Herbereken STEP"` button (`PATCH /spec`), DFM warning badges, and one-click `.step` download.

---

## 10. Recommended Build Order & Milestones

1. **Milestone 1 — Foundation (Agent 3 + Agent 1):** Finalize `part_spec.py` and build the deterministic `build123d` compiler tested against 3 real example JSONs from your workshop. Import the resulting `.step` files into your CAM software to verify clean B-Rep geometry before introducing any AI.
2. **Milestone 2 — Extraction Accuracy (Agent 2):** Collect 5–10 representative workshop PDF drawings in `backend/tests/fixtures/` as a regression benchmark. Tune PDF tiling overlap, vector/text prompt injection, and `deepseek-flash` prompts until extraction accuracy consistently clears your threshold.
3. **Milestone 3 — UI & Tailscale Rollout (Agent 4 + Ops):** Build the frontend against mock endpoints in parallel, wire up SSE + `PATCH /spec` instant rebuilds, and deploy behind `tailscale serve --bg 8000` with workshop ACLs enabled.

---

## 11. Implementation Notes (as built, 2026-11-26)

Everything above is the contract. This section records the decisions taken while implementing it, so later
agents do not have to rediscover them.

### 11.1 Coordinates & PartSpec extensions
* `datum_origin` places the part: the default `bottom_left_top_face` gives x∈[0,L], y∈[0,W], **z∈[-T,0]**
  (Z0 = top of stock). Also supported: `bottom_left_bottom_face`, `center_top_face`, `center_bottom_face`.
* `center_x/center_y` (and `start_*`, `end_*`, `x_min..y_max`, `points_2d`) are datum coordinates projected on the
  feature's face, never mirrored: top/bottom → (X, Y); front/back → (X, Z); left/right → (Y, Z).
  Example: M6 in the front face of a 12 mm plate at mid-height → `face: "front", center_y: -6`.
* `depth: null` ⇒ `through_all: true`. `units` is always `"mm"` (inch drawings are converted by the extractor).
* Optional fields beyond the §6 example (all backwards compatible): `pattern` (`linear` grid from the first
  instance, or `circular` around the centre), `extra_positions`, `countersink {diameter, angle_deg}`,
  `thread_depth`, `tip_angle_deg` (118° drill point on blind holes, `null` = flat), `rotation_deg`,
  `step_cut {x_min, x_max, y_min, y_max, depth, corner_radius}` and a `complex_3d` feature (`description`,
  `operation`, `bbox_min/max`, `code`) that carries the Layer-2 work. `requires_complex_3d_fallback` is set
  automatically when a `complex_3d` feature exists; the compiler skips such features unless the flag is true.
* Threads: `diameter` is the nominal size; the solid is modelled at the ISO tap drill (M8 → 6.8, fine threads
  nominal − pitch). Edge treatments are applied to the stock before features (fillets before chamfers).

### 11.2 Pipeline & statuses
* Job status = the §7 stage name: `queued → parsing_pdf → extracting_features → compiling_cad → verifying →
  retrying → ready | failed`. `ready` means the §8.1/8.2 gate passed and every feature compiled; DFM warnings
  never block. `GET /api/jobs` exposes `dfm.needs_review` (low confidence, warnings or ambiguities).
* Job sources besides PDF: DXF, STEP and sketch photos (§11.10); their spec revisions are tagged `dxf`, `step`,
  `sketch`.
* `awaiting_dimensions` (PartSpec v1.1, DRAWING_SPEC §5): a spec with a `missing_dimensions` entry that is
  `blocking` pauses the job before any CAD work, after extraction or on `PATCH /spec`. The SSE stream ends (the
  status is terminal until the operator answers), a server restart keeps it, and the next `PATCH /spec` without
  blocking entries builds normally. The event carries the missing fields in `data.missing`.
* Stage 5 routing: kernel/gate failures → `correct_spec` on `DEEPSEEK_VISION_MODEL` **with the drawing**
  (Stage 2); `complex_3d` sandbox failures → `DEEPSEEK_CODE_MODEL` regenerates the code (Stage 3). Max 1 + 3
  builds. The critic must quote text-layer **evidence** and mark findings `major|minor`; only evidence-backed
  major findings trigger a correction, and the correction prompt must verify every claim against the drawing.
  The job ends on the last critic-approved build (prevents a hallucinated "fix" from replacing a good model).
* Extra deterministic check (not in §8): every Ø / M / R callout in the text layer (outside the title block),
  including `Nx` counts, must map to the PartSpec, otherwise `CALLOUT_UNMATCHED` (warning). These findings
  are also handed to the critic.
* `PATCH /spec` is synchronous in a pre-warmed process-pool worker: STEP + GLB + DFM in ≈1.0–1.2 s end-to-end
  on the workstation (CAD ≈0.8–0.95 s). Ortho renders follow on a dedicated render worker and are announced
  with a `verifying` SSE event. It never calls the LLM; a `complex_3d` feature without code is reported as a
  failed feature (use `/reextract`).
* `POST /reextract {hint}` re-runs Stage 2 with the current (possibly operator-corrected) spec plus the hint.
* `POST /api/jobs/{id}/drawing {sheet_size?, scale?, language?, engine?: "builtin", version?}` regenerates the EN ISO
  drawing of a built revision from its STEP (≈2–3 s) and promotes it when that revision is current.
* Extra endpoints: `GET /api/health[?deep=true]`, `GET /api/jobs/{id}?version=n`, `GET /api/jobs/{id}/spec?version=n`,
  `GET /api/jobs/{id}/events/history`, `DELETE /api/jobs/{id}`, `GET /api/schema/part_spec`.
  `GET /artifacts/{filename}` also accepts `?version=n`, `ortho_<view>.png` and `tiles/<name>.png`.

### 11.3 DeepSeek specifics (verified against the live API, 2026-11-26)
* Files API: upload with `purpose="user_data"` (`vision` is rejected) and reference in chat content as
  `{"type": "file", "file_id": "<id>"}`. Ids are cached in `llm/file_ids.json`, reused for every retry and
  the critic, and deleted from DeepSeek when the job is deleted. Base64 data URIs are the automatic fallback.
* `deepseek-flash` reasons by default and reasoning tokens count against `max_tokens`: a 16k budget produced
  empty completions (`finish_reason=length`). Default is 65 536 (auto-halved if rejected); empty completions
  are retried. Thinking is passed as `extra_body={"thinking": {"type": "enabled"|"disabled"}}`.
* Latency on the sample drawing: extraction ≈90–150 s with thinking, ≈6 s without (same geometry).
  `DEEPSEEK_THINKING=disabled` is the fast option.

### 11.4 Verifier
* Gate: BRepCheck valid, exactly one solid, bounding box = stock (+ bosses) within ±0.01 mm; a `step_cut` may
  shrink the envelope (info). Warnings use the §8 thresholds (configurable in `.env`, `DFM_*`).
* §8.6 is implemented now: threaded bores blue, tight-tolerance bores (ISO fit IT≤9 such as H7, or a numeric
  band ≤0.05 mm) red, written as XCAF surface colours in the STEP.
* `ortho_check.png` = front / right / top / iso hidden-line views at one common scale (plus `ortho_<view>.png`).
* The GLB is tessellated per B-Rep face (sharp CAD edges, smooth cylinders) and written Y-up (glTF); the viewer
  rotates it back to Z-up.

### 11.5 Sandbox
AST allow-list (`build123d`, `math`, `numpy`; no dunders, no exec/eval/open/getattr, no exporters), separate
`python -I` interpreter with a scrubbed environment, temporary cwd and restricted builtins; output crosses back
only as a BREP file. The 15 s budget applies to the generated code itself — interpreter start + `import build123d`
(≈10 s on this workstation) has its own 90 s limit. This is defence in depth, not a hard security boundary.

### 11.6 Job folder
```text
data/jobs/<job_id>/
├── original.pdf    (DXF jobs: rendered DXF; sketch jobs: the rectified sheet; STEP jobs: none)
├── original.dxf | original.step | sketch_upload.<jpg|png|webp>, step_import.json (STEP: offset, completeness)
├── extraction/   text_spans.json, vectors.json, summary.json, all_pages_text.json, digest.json
├── tiles/        sheet.png, tile_r{0,1}c{0,1}.png, title_block.png (+ *.llm.png sent to the model)
├── specs/        part_spec_v<n>.json, history.json (source: extraction|reextract|self_heal|critic|complex_code|user_edit)
├── versions/v<n>/ model.step, preview.glb, ortho_check.png, ortho_<view>.png, dfm_report.json, complex/,
│                 drawing.pdf, drawing.svg, drawing.dxf, drawing.json (sheet, scale, warnings, overrides)
├── model.step, preview.glb, ortho_check.png, dfm_report.json, drawing.*   (= current version)
├── iteration_logs.json
└── llm/          trace_*.json (every request/response + token usage), file_ids.json
```

### 11.7 Deployment notes (reference workstation: Windows 10, Tailscale)
* **Fixed, dedicated ports** (deviation from the `8000` / `5173` / `--bg 8000` defaults in §1-§5, at the operator's
  request, to never collide with other dev servers or the services already on this machine):

  | What | Port | Notes |
  |---|---|---|
  | API + built UI (uvicorn) | `127.0.0.1:8620` | `PORT` in `.env`; still loopback-only (hard rule 4) |
  | Vite dev server | `127.0.0.1:5620` | `VITE_DEV_PORT`; proxies `/api` to `PORT` |
  | Tailnet HTTPS (`tailscale serve`) | `:8445` | `TAILSCALE_HTTPS_PORT`; 443 / 8443 / 10000 were already serving other tools on the reference workstation |

  Checked free and outside the Windows dynamic range (49152+) and excluded ranges on 2026-11-26.
  Without `.env` the code default stays `8000`.
* **Service** (`scripts/service.ps1`, Windows, no admin): `ensure` starts the API when it is not healthy, verifies the
  `tailscale serve` mapping (re-creates it when missing or when it still points to an old target of this service;
  never overwrites a mapping that belongs to something else unless `-Force`) and tests
  `https://<machine>.<tailnet>:8445/api/health` end-to-end (re-applies the mapping once on failure).
  `install` registers the task "PDF-to-STEP service" (at sign-in + every 5 min, runs `ensure`), independent of any
  other autostart on the machine. Also `status`, `stop`, `restart`, `uninstall`.
  The API is started through a hidden launcher PowerShell (`Start-Process` without redirection = ShellExecute), so
  uvicorn inherits none of the caller's handles: `ensure`/`restart` return as soon as the API is healthy instead of
  keeping the calling terminal (or remote shell) attached, and the API does not depend on that shell.
  (`Win32_Process.Create` was tried and rejected: its environment breaks the venv's python launcher.)
  Logs: `logs/service.log`, `logs/api.{out,err}.log`. `scripts/start_tailscale.ps1` delegates to `service.ps1 ensure`.
* Windows equivalents: `scripts/dev.ps1`, `scripts/start_tailscale.ps1`. If `PYTHONDONTWRITEBYTECODE` is set,
  precompile the venv once (`python -m compileall .venv/Lib/site-packages`): PyMuPDF otherwise recompiles
  ~4 MB of Python on every import.
* The UI is Dutch for the workshop operators; code, prompts and API stay English.
* Below 1024 px the UI switches to a mobile shell (`MobileShell.tsx`): compact top bar with STEP download,
  job list as its own screen, and five full-height tabs (Status · Tekening · 3D · Spec · Controle) with badges.
  Deep links: `#job=<id>&tab=<status|tekening|3d|spec|controle>`. On touch devices (`pointer: coarse`) every
  button is ≥ 44 px and form fields use 16 px text (no iOS focus zoom); the drawing supports pinch-zoom. The app
  ships a web manifest so it can be added to the home screen.

### 11.8 EN ISO drawing engine (DRAWING_SPEC.md phase 1)
* `iso_template.py` holds one primitive sheet model (strokes, fills, text in sheet mm, y down) that renders to
  **PDF via PyMuPDF** (embedded DejaVu Sans, real selectable text; no Cairo/WeasyPrint system libraries needed on
  Windows), **SVG** (one `<g id>` per layer) and **DXF** (ezdxf R2018, layers + DASHED/CENTER linetypes, y flipped).
  `DRAWING_FONT` overrides the font. EN ISO 128-2 line groups 0.35/0.18 (A4) and 0.5/0.25 (A3), lettering
  3.5 mm (dimensions) / 2.5 mm (notes), EN ISO 5457 frame with 20 mm binding margin, centring marks and grid zones,
  EN ISO 7200 title block 180 mm wide with the first-angle symbol.
* `drawing_engine.py`: build123d `project_to_viewport` HLR for front (x,z), top (x,y), left (−y,z) and an isometric
  view, placed first-angle (top below front, left view right of front). Sheet/scale solver: largest ISO 5455 scale
  that fits; A4 landscape unless the scale would drop below 1:2 or there are more than 12 features, then A3.
  Gutters are sized from the actual dimension stacks (incl. the text of short dimensions that sits outside the
  extension lines); two neighbouring views share the gap between them (min 25 mm).
* Dimensioning: EN ISO 129-1 baseline dimensions from the datum (first line 10 mm from the view, 8 mm stagger),
  one dimension per unique coordinate per view/axis; depths as reference dimensions `(d)` on the front view.
  Callouts per feature group (`4x Ø6.6 THRU` / `CBORE Ø11 ↧6.8`, `2x M6x1 ↧12` / `Ø5 ↧15`, `POCKET 50x30 R5 ↧6`,
  `SLOT 8 ↧4`, `4x R6`, `1x45°`). DejaVu Sans has no ⌴/⌵ glyphs, so counterbore/countersink are written
  `CBORE`/`CSK` unless `DRAWING_FONT` provides them. Leaders try the anchor and all pattern instances at several
  angles/lengths and may not cross text; a callout that cannot be placed becomes a numbered balloon + note.
* The generated PDF parses back through `pdf_parser.py`; the ISO 5457 grid labels in the margin are tagged
  `sheet_grid` and left out of the LLM digest so they are never read as dimensions.
* Drawings are rendered on the render worker for every full pipeline build and after every `PATCH /spec`
  (deferred, with the ortho renders); a drawing failure is logged but never fails the gate. The MASS cell is
  filled from the material density table.
* Lettering: the default font is the first installed sans-serif that also carries ⌴ ⌵ ↧ Ø ° (Segoe UI Symbol on
  Windows, embedding permitted, fsType 8), so callouts read `⌴Ø11 ↧6.8` / `⌵Ø9 x 90°` as one selectable run;
  without such a font DejaVu Sans is used and the callouts say `CBORE` / `CSK`. Every text on the sheet is
  ≥ 2.5 mm (EN ISO 3098, spec §2), including the title-block field names: the block is 40 mm high (rows
  14 / 13 / 13 mm; ISO 7200 fixes only the 180 mm width), EN/NL labels that do not fit one line are set EN over
  NL, long values wrap instead of shrinking.
* EN ISO 6410 threads: the model carries the tap-drill bore; the drawing adds a thin ¾ circle at the nominal
  diameter where the view looks along the axis (dashed on the far face) and thin hidden lines at the nominal
  diameter to the usable thread length plus the thread-limit line where the axis lies in the view.
* Section A–A (EN ISO 128-40/-44/-50), `drawing_metadata.section_view = AUTO|NONE`: when the part has hidden
  top/bottom features, the cut runs parallel to XZ through the row with the most of them (blind / seated
  features count double), the part behind the plane is projected like the front view without hidden lines, cut
  faces are hatched with thin 45° lines (2.5 mm, even-odd so holes stay open), the cutting plane is drawn on the
  top view (chain line, thick ends, arrows in the viewing direction, letters A). It goes in the free area above
  the isometric view, at the main scale or the largest smaller ISO scale that fits (then stated in its label);
  without room it is left out.
* Depths in the front view (spec §4.3): reference dimensions `(d)` for pocket, slot and step floors, blind holes
  and counterbores, one per depth level; the callouts keep `↧`.
* `drawing_metadata.gdt` (EN ISO 1101 frames), `surface_finishes` (per-feature Ra) and `notes` are printed as
  numbered notes, e.g. `[⌖ | Ø0.1 | A | B] H1 (ISO 1101)`; they never change the geometry (spec §8.2).
* `DrawingEngine` protocol + registry (`GET /api/drawing-engines`): `builtin` is this engine; `draftwright` and
  `freecad` are registered as not available (no adapter; the spec makes them optional / future). `POST /drawing`
  answers 422 for an unavailable engine.
* Deliberate deviations from the letter of DRAWING_SPEC §4: PDF straight from PyMuPDF instead of SVG → CairoSVG /
  WeasyPrint (no native Cairo on Windows; text stays selectable, which is what the spec needs it for); towards the
  frame the views keep only the room their dimensions need (+5 mm clear of the centring marks) instead of a flat
  25 mm, while two neighbouring views stay ≥ 25 mm apart; the scale solver lives in `drawing_engine.py`.

### 11.9 Round-trip benchmark (DRAWING_SPEC.md phase 2)
* `python -m app.services.roundtrip_bench --n 10 --seed 7 --llm deepseek --thinking enabled,disabled
  [--prompt-variant NAME=FILE] [--no-critic] [--concurrency 2] [--price-in X --price-out Y]` writes
  `data/benchmarks/<ts>/report.{md,json}` plus every case (ground-truth spec + STEP, `synthetic_iso.pdf`, the
  pipeline's job folder). `--llm oracle` replays the ground truth through the mock client to test the harness.
* Procedural parts: random stock / rounded corners / chamfer, optional corner hole pattern (with counterbores), then
  one feature per grid cell (pockets, circular pockets, slots, through / blind / counterbored / countersunk /
  threaded / H7 holes). A part is kept only if it compiles, passes the gate and has no thin-wall / web / breakout
  warning.
* Diff: one valid solid; bounding box ±0.05 mm; volume < 0.5 %; per feature type greedy nearest-neighbour pairing
  of placed instances in global XYZ (patterns expanded, through == blind to full depth, ids and order ignored),
  TP when the centre and every key dimension are within ±0.10 mm; near misses (≤ 3 mm) are listed with the wrong
  fields. Fixture PDFs and sketch photos (`X.pdf|jpg` + `X.part_spec.json` in `--fixtures`) get their own report
  sections.
* First real run (2026-11-26, `deepseek-flash`, critic on, seed 21, 4 synthetic parts + the bracket fixture):
  synthetic 4/4 ready with exact solids in both modes, P/R 0.95 (one slot read 4 mm short: the model treated
  dimensions to the arc centres as the slot ends; the extraction prompt now states the convention). The bracket
  fixture: thinking **on** P/R 1.0 in 260 s, thinking **off** P/R 0.42 (holes misplaced) in 54 s. Thinking stays the
  default; on clean synthetic drawings the modes tie.

### 11.10 Intake fast paths (DRAWING_SPEC.md phase 3)
* **DXF** (`POST /api/jobs/from-dxf`, `dxf_parser.py`, zero AI): layers `CONTOUR_T<t>`, `POCKET_D<d>|_THRU`,
  `HOLE_THRU|_D<d>`, `THREAD_<M6|M8x1>_D<d>|_THRU`, `SLOT_D<d>|_THRU` (case-insensitive, `.` or `,`). Geometry from
  closed (LW)polylines with bulges, circles, or loose LINE/ARC chains (joined within 1 µm). Recognised loops:
  (rounded, rotated) rectangles, obround slots, circles, other polygons; inner loops on the contour layer are
  through cut-outs. Equal holes become one callout with `extra_positions`. Thread layers give the usable thread
  depth; the drill goes max(2.5 mm, 0.5·d) deeper (through when that reaches the far side). The datum is the
  contour's bottom-left corner. A `$INSUNITS` header that would make the part larger than 3 m is ignored (read as
  mm, warning). Missing thickness / depth → blocking `missing_dimensions` → `awaiting_dimensions`.
* **STEP** (`POST /api/jobs/from-step`, `step_ingestor.py`, zero AI): the solid is moved to the datum (x,y ≥ 0,
  top face Z0) and recognised into a documentation PartSpec: rounded corners, a uniform top chamfer, holes on all
  six faces (coaxial stacks → counterbore / countersink / drill tip; a hole starting in a pocket floor counts as
  entering from that face — "open end" is tested with a point classifier), pocket / slot / circular-pocket /
  step floors seen from top or bottom, straight-edged free pockets as polygons. Flat-bottomed blind bores ≥ Ø8
  without a seat are milled circular pockets. Threads are not in a B-Rep: bores at an ISO tap-drill size are listed
  as questions. The recognised spec is recompiled; when the volume matches within 0.1 % it is **complete** and
  edits rebuild from the spec, otherwise every rebuild keeps the uploaded geometry and edits only change the
  documentation (DFM, colours, drawing). The first build always uses the upload itself.
* **Paper sketch** (`GET /api/templates/sketch-a4.pdf`, `POST /api/jobs/from-sketch`): A4 landscape template with
  ArUco `DICT_4X4_50` ids 0–3 (18 mm, 8 mm from the corners), first-angle view boxes, datum, title fields and a
  checklist. The ingestor applies EXIF orientation, finds all four markers (otherwise 422 before a job is made),
  fits a homography on the 16 marker corners (RANSAC), warps to 5 px/mm and builds a grid-free scan (see below). The model gets the colour sheet
  and the four box crops with a sketch prompt (only written numbers count, never guess, missing values as
  blocking questions); every confidence is capped at 0.75. The visual critic is off for sketches (not to scale).
  The UI asks the questions in a dialog and rebuilds via `PATCH /spec`.
* Files API OCR experiment (DRAWING_SPEC §3 / §7 phase 3), verified on the live API 2026-11-26/27: the Files API
  accepts exactly the purposes `user_data`, `text_recognition` and `rich_layout_recognition` and has no content
  endpoint; a file uploaded with a recognition purpose and referenced in a chat reaches the model as its
  recognised text (LaTeX-like, e.g. `120\times60 R6`). Recognition runs asynchronously: a reference within ~1 s of
  the upload arrives empty, hence `SKETCH_OCR_WAIT_S` (3 s). `rich_layout_recognition` returned nothing for a PDF
  page in the probe. `SKETCH_OCR` adds that text for the four crops next to the images ("the image wins").
* Benchmark on 5 synthetic handwritten sketches (`tests/fixtures/sketches/`, Windows script fonts with jitter,
  phone-photo distortion; no real pencil sketches available yet), thinking on, `deepseek-flash`:

  | Scan / sketches | OCR off | `text_recognition` | `rich_layout_recognition` |
  |---|---|---|---|
  | first run (red-channel scan, one cramped sketch) | P/R 0.92, 4/5 built | P/R 1.0, 4/5 built | – |
  | final (brightest-channel scan, cleaned sketches) | P/R 1.0, 5/5 built, 33 s | P/R 1.0, 5/5, 47 s | P/R 1.0, 5/5, 43 s |

  The one cramped sketch made both variants **ask** for the unreadable width instead of guessing (the gate works).
  Decision: OCR stays **off** by default; it helped only while the scan lost faint pencil, and costs 10–14 s per
  sketch. Turn it on (`SKETCH_OCR=text_recognition`) for poor photos, and re-run
  `roundtrip_bench --fixtures tests/fixtures/sketches --sketch-ocr off,text_recognition` once real workshop
  sketches are added.
* The scan for the crops uses the brightest colour channel divided by the local paper white (threshold 0.80):
  the light-cyan grid is bright in green/blue (~0.93) and drops out, grey pencil is dark in every channel and stays,
  even thin strokes.

* **Live 2D preview** (phase 4, `SpecPreview2D.tsx`): the editor draws the draft PartSpec as a first-angle front +
  top view in the browser (no request), with the same pattern expansion as `positions()`; shapes select features.

### 11.11 CAM-ready output & shopfloor (SPEC_V1_5_CAM_SHOPFLOOR.md, v1.5)
* **Shop configuration** lives in `data/config/{tools,stock_catalog,cam_colors}.json` (defaults in `app/defaults/`;
  a broken file falls back to the defaults, every save keeps a backup). Material groups come from the PartSpec
  material string (EN AW / 1.4301 / S235 / POM …).
* **DFM v2** adds `tool_checks`, `recommended_tools` and `stock_recommendation` to `dfm_report.json`. Tool checks
  are warnings / info, never gate failures: a part the magazine cannot make is still a valid STEP.
* **XCAF colours**: OCCT converts `Quantity_TOC_RGB` (linear) to sRGB on write, so colours are set as
  `Quantity_TOC_sRGB` and the file carries the palette's exact values. The body colour sits on the solid label,
  feature colours on face sub-shape labels; the solid is never split. Face classification uses geometric
  signatures (axis + radius within 0.05 mm) against the PartSpec, not face indices. The GLB carries the same sRGB
  bytes as vertex colours; the viewer converts them to linear once.
* **QR / setup sheet**: QR codes are vector rectangles (scannable at 300 dpi, verified with OpenCV in the tests).
  Without `PUBLIC_TAILSCALE_URL` a short warning replaces the code. The setup sheet is rendered from the built
  STEP after the drawing (deferred render on `PATCH /spec`, like the drawing).
* **Telemetry**: the latest AI-sourced revision is the baseline (`ai_initial_spec_json`); confidence, notes and
  drawing metadata are not counted as corrections. Rows are written on every `PATCH /spec` and on
  `POST /jobs/{id}/verify`; deterministic intakes (DXF, STEP) have no baseline and are not logged.
* **Batches**: the split runs synchronously in `POST /api/batches` (text layer: milliseconds per page), children
  run in the background behind an `asyncio.Semaphore(MAX_CONCURRENT_JOBS)`; single uploads are not throttled.
  Scanned pages (< 15 title-block characters) go to one `deepseek-flash` call only when a real API key is
  configured; offline they continue the previous part. A service restart does not resume queued batch children.
* **Shopfloor view** (`/shopfloor/<job>`): read-only, no LLM and no CAD kernel in the API process; feature anchor
  points are computed once per version in a CAD worker and cached (`shopfloor_anchors.json`). Tap-to-inspect maps
  the hit point (first intersection on the visible side of the section plane) to the nearest feature anchor of the
  tapped colour category.

### 11.12 Open items
* Milestone 1: import the generated STEP files into the workshop CAM package (colours, faces, datum) and add 3
  real workshop example JSONs to `backend/tests/fixtures/`.
* Milestone 2: add 5–10 real workshop PDFs + expected `*.part_spec.json` as the extraction benchmark (the current
  fixtures are a synthetic bracket and the §6 example).
* Tailscale ACLs (`tag:cnc-workstation` → `group:werkplaats`) are a tailnet admin task.
* Turned parts (cylindrical stock, lathe features) are outside the v1 contract.
* Roadmap for v1.1 phases 2–4 and v1.5: see [ROADMAP.md](ROADMAP.md).