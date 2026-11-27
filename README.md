# PDF → STEP · CNC generator

Local-first web app that turns 2D technical drawings (PDF) into **CNC-ready native B-Rep STEP files**
(ISO 10303 AP214 / AP242) with minimal rework. A vision LLM reads the drawing into a typed `PartSpec`;
a deterministic [build123d](https://github.com/gumyr/build123d) / Open CASCADE compiler turns that spec into
exact analytic geometry; a verifier gates the result on geometry and manufacturability before it is marked ready.

> **[ARCHITECTURE.md](ARCHITECTURE.md) is the source of truth** (contract, API, DFM rules). Section 11 records
> how it was implemented. Later versions have their own contracts ([DRAWING_SPEC.md](DRAWING_SPEC.md) for v1.1,
> [SPEC_V1_5_CAM_SHOPFLOOR.md](SPEC_V1_5_CAM_SHOPFLOOR.md) for v1.5); progress is tracked in [ROADMAP.md](ROADMAP.md).

![pipeline](https://img.shields.io/badge/pipeline-PDF%20%E2%86%92%20PartSpec%20%E2%86%92%20B--Rep%20%E2%86%92%20STEP-0ea5e9)
![python](https://img.shields.io/badge/python-3.12-3776ab) ![react](https://img.shields.io/badge/react-19-61dafb)

## How it works

```text
 PDF ──► 1 parse (PyMuPDF): text + coordinates, vectors, 300 DPI sheet, overlapping tiles, title block
     ──► 2 extract (DeepSeek vision, JSON mode): PartSpec v1 with a confidence per feature
     ──► 3 compile (build123d, deterministic): stock, outer profile, holes, pockets, slots, bosses, step cuts,
            chamfers / fillets; complex_3d only via LLM code in a sandboxed subprocess
     ──► 4 verify: valid single solid, bounding box ±0.01 mm, DFM rules, callout coverage, ortho renders
     ──► 5 self-heal (max 3): kernel errors / visual critic findings → corrected PartSpec → rebuild
     ──► 6 operator: PDF and 3D side by side, edit the spec, "Herbereken STEP" (instant, no AI) → download
     ──► + EN ISO detail drawing of every revision (PDF / SVG / DXF), generated from the PartSpec and the B-Rep
     ──► + CAM package: colour-coded STEP, tool-library DFM, raw-stock advice, A4 setup sheet, QR → shopfloor view

 v2  ──► 6 faces + tilted 3+2 planes, setup plan (3-axis vs 5-axis), Op 2 softjaws, Rev A vs Rev B diff, quote
 RFQ ──► multi-page customer PDF → split per part number / sheet → one job per part (throttled) → ZIP of STEPs

 DXF ──► layered DXF (CONTOUR_T20, POCKET_D8, HOLE_THRU, THREAD_M6_D12, SLOT_D6) → PartSpec, zero AI → 3 …
STEP ──► your own B-Rep, moved to the datum, features recognised for DFM + drawing, zero AI → 4 …
 📷  ──► photo of the printed A4 sketch template → ArUco rectification → sketch extraction (written numbers only,
          confidence ≤ 75 %, missing values asked instead of guessed) → 3 …
```

- **No mesh-to-STEP**: every solid is native B-Rep (planes, cylinders, cones), ready for CAM.
- **Schema first**: modules only exchange the versioned Pydantic `PartSpec`; raw LLM text never reaches the compiler.
- **Traceable**: every job keeps the original PDF, text/vector dump, tiles, every `PartSpec` revision, STEP, GLB,
  orthographic check renders, DFM report, iteration log and full LLM trace under `data/jobs/<job_id>/`.
- **CAM-coloured STEP** (AP214, XCAF face colours on one watertight solid, same colours in the 3D viewer): threads
  blue, fits IT ≤ 7 red, counterbore / countersink seats purple, fine finishes amber, the G54 datum face green.
  Palette and rules in `data/config/cam_colors.json`.
- **Shop-aware DFM**: checks against your own tool library (`/tools`, Fusion 360 / CSV import): end mill reach and
  corner radius, drills, taps, thread mills, reamers with pre-drill, counterbores, chamfers; recommended tools per
  feature and the smallest standard raw blank (flat bar / plate) with masses and chip percentage.
- **Shopfloor**: a 1-page A4 setup sheet per revision (iso view with G54, raw stock, tooling, critical
  inspection) and a QR code on it and on the drawing that opens a read-only tablet view (`/shopfloor/<job>`):
  section planes, colour filter, tap a face for tolerance, roughness and tools.
- **Batch intake** (`/batches`): one customer RFQ PDF with many drawings becomes one job per part, at most
  `MAX_CONCURRENT_JOBS` at once, with a live table and a ZIP of all ready STEPs, drawings and setup sheets.
- **Multi-sided & 3+2** (PartSpec 2.0): features on all six faces and on tilted working planes (SPATIAL_XYZ,
  like PLANE SPATIAL / G68.2), angled face cuts, a setup plan per machine type (3-axis vs 5-axis 3+2) and a
  cross-face check for thin webs and unintended breakthroughs. Stored v1 jobs load and compile unchanged.
- **Softjaws**: one click turns the finished part into two ready-to-mill Op 2 jaws (STEP with named bodies and
  green pocket faces) for a vise from `vises.json`: undercut-safe cavity, dogbones, bolt-clearance safety checks.
- **Revision diff**: Rev A vs Rev B of the same drawing number as exact B-Rep booleans (green added, red removed)
  plus a classified feature diff (`BREAKING_CAM`, `GEOMETRY_CHANGE`, `METADATA_ONLY`) and a 1-page PDF.
- **Quoting**: deterministic cycle time and cost with quantity price breaks, 3-axis vs 5-axis, every euro traced to
  a volume, area, hole, setup and rate (`/settings/rates`); PDF / CSV export.
- **Correction telemetry**: every operator edit and "Markeer gecontroleerd" is diffed against the AI reading
  (`GET /api/metrics/accuracy`), the raw data for the planned self-learning loop.
- **EN ISO 2D drawings**: first-angle views + isometric, baseline dimensions from the datum, ISO callouts,
  EN ISO 5457 frame and 7200 title block (EN or EN/NL), general tolerance / edge-break / surface notes, stamped
  `DRAFT` until signed. Vector PDF with selectable text, SVG and layered DXF, A4 or A3 at an ISO 5455 scale, next
  to `model.step` for every revision.
- **Four ways in**: customer PDFs (vision), layered DXF and STEP (deterministic), and phone photos of a printable
  A4 sketch template with ArUco markers (`GET /api/templates/sketch-a4.pdf`).
- **DFM checks**: sharp / small internal corners, hole and pocket depth ratios, thin walls / webs / floors,
  unmatched drawing callouts.
- **Operator UI** (Dutch): split-screen workstation on desktop, tabbed mobile layout on phones, installable as a PWA;
  live 2D front/top preview while editing the spec, camera capture for sketches, missing-dimension questions.

## Requirements

- Python **3.12** (build123d / OCP wheels), [uv](https://docs.astral.sh/uv/) recommended
- Node.js **20+**
- A [DeepSeek](https://platform.deepseek.com) API key (without one the app runs with an offline mock and you
  enter the spec by hand)
- Optional: [Tailscale](https://tailscale.com) for remote access (the server itself only binds to 127.0.0.1)

## Quick start

```bash
git clone https://github.com/bokk3/pdf-to-step.git && cd pdf-to-step

cd backend
uv venv --python 3.12 .venv
uv pip install --python .venv/bin/python -e ".[dev]"      # Windows: .venv\Scripts\python.exe
cd ../frontend && npm install && npm run build && cd ..

cp .env.example .env                                     # then set DEEPSEEK_API_KEY
```

Run it:

| Platform | Command | Result |
|---|---|---|
| Windows | `.\scripts\service.ps1` | API + UI on `127.0.0.1:8620`, published with `tailscale serve` on `:8445`, end-to-end checked |
| Windows | `.\scripts\service.ps1 install` | same, plus a scheduled task (sign-in + every 5 min) that restarts the API and repairs the tailnet mapping |
| Linux / macOS | `scripts/start_tailscale.sh` | API + UI on `127.0.0.1:8620`, published on the tailnet |
| Any (dev) | `scripts/dev.sh` / `.\scripts\dev.ps1` | FastAPI with reload + Vite on `127.0.0.1:5620` |

Then open `http://127.0.0.1:8620` locally or `https://<machine>.<tailnet>.ts.net:8445` from any device on your tailnet.

`.\scripts\service.ps1 status | stop | restart | uninstall` manage the Windows service; logs live in `logs/`.

## Configuration

All settings live in `.env` (see [.env.example](.env.example)). The most important ones:

| Variable | Default | Purpose |
|---|---|---|
| `DEEPSEEK_API_KEY` | – | server-side only, never sent to the browser |
| `DEEPSEEK_VISION_MODEL` / `DEEPSEEK_CODE_MODEL` | `deepseek-flash` / `deepseek-v4-pro` | extraction + critic / complex-3D code fallback |
| `DEEPSEEK_THINKING` | `enabled` | `disabled` is ~25× faster per extraction |
| `PORT` / `VITE_DEV_PORT` / `TAILSCALE_HTTPS_PORT` | `8620` / `5620` / `8445` | fixed ports |
| `MAX_HEAL_RETRIES` / `SANDBOX_TIMEOUT_S` | `3` / `15` | self-healing budget, LLM-code execution limit |
| `STEP_SCHEMA` | `AP214` | or `AP242` |
| `DFM_*` | §8 values | corner radius 1.5 mm, hole depth 5×D, pocket depth 4×R, wall 1.5 mm, bbox ±0.01 mm |
| `DRAWING_FONT` | auto | lettering font; default Segoe UI Symbol when installed (real ⌴ ⌵ ↧ symbols), else DejaVu Sans |
| `SKETCH_OCR` | `off` | `text_recognition` / `rich_layout_recognition`: DeepSeek Files API text layer for hand sketches |
| `PUBLIC_TAILSCALE_URL` | – | base URL for the shopfloor QR codes (e.g. your tailnet URL); empty = no QR |
| `MAX_CONCURRENT_JOBS` | `2` | batch children running at once (RAM / CPU guard) |
| `BATCH_PART_NUMBER_REGEX` / `BATCH_SHEET_REGEX` | built-in | extra title-block patterns for the batch splitter |

## API

| Method | Endpoint | |
|---|---|---|
| `POST` | `/api/jobs` | upload a PDF (`file`) → `202 {job_id, status}` |
| `POST` | `/api/jobs/from-dxf` · `/from-step` · `/from-sketch` | layered DXF · STEP model · photo of the sketch template |
| `GET` | `/api/templates/sketch-a4.pdf` | printable A4 sketch template (4 ArUco markers, first-angle boxes) |
| `GET` | `/api/drawing-engines` | drawing engines behind the `DrawingEngine` protocol (`builtin`; draftwright / FreeCAD not wired) |
| `GET` | `/api/jobs` · `/api/jobs/{id}` | list · full state (`part_spec`, `dfm_report`, `iteration_logs`, artifact URLs) |
| `GET` | `/api/jobs/{id}/events` | Server-Sent Events: `parsing_pdf` → … → `ready` / `failed` |
| `PATCH` | `/api/jobs/{id}/spec` | operator edit → deterministic rebuild in ~1 s, no LLM call |
| `POST` | `/api/jobs/{id}/reextract` | re-run extraction with an optional hint |
| `POST` | `/api/jobs/{id}/drawing` | (re)generate the EN ISO drawing, optional `sheet_size`, `scale`, `language` |
| `GET` | `/api/jobs/{id}/artifacts/{name}` | `model.step`, `preview.glb`, `drawing.pdf/.svg/.dxf`, `original.pdf`, `ortho_check.png`, `dfm_report.json` (`?version=n`) |
| `POST` | `/api/jobs/{id}/setup-sheet` · `/verify` | regenerate the A4 setup sheet · mark the current spec verified |
| `GET` | `/api/jobs/{id}/shopfloor` | read-only payload for the tablet view (features, colours, tools, artifacts) |
| `POST` | `/api/batches` | multi-page PDF → `202 {batch_id, job_count}`; `GET /api/batches/{id}` · `/events` (SSE) · `/download-zip` |
| `GET` · `PUT` | `/api/config/tools` · `/api/config/stock` | tool library · raw stock catalogue; `POST /api/config/tools/import-fusion` |
| `GET` | `/api/jobs/{id}/setup-plan` | 3-axis vs 5-axis 3+2 setups, clamping advice |
| `POST` | `/api/jobs/{id}/softjaws` | Op 2 softjaws (`softjaws.step`, `softjaws_assembly.glb`, report); `GET`·`PUT /api/config/vises` |
| `GET` | `/api/parts/{part_number}/revisions` | all jobs of a drawing number, oldest first |
| `POST` | `/api/revisions/diff` | `{job_old_id, job_new_id}` -> B-Rep + semantic diff, GLB and PDF URLs |
| `POST` | `/api/jobs/{id}/quote` | price breaks; `GET /api/jobs/{id}/quote/export.pdf` · `.csv`; `GET`·`PUT /api/config/shop-rates` |
| `GET` | `/api/metrics/accuracy` | verified jobs, untouched-pass rate, corrections per feature type |
| `GET` | `/api/health?deep=true` | OCCT + DeepSeek connectivity |

## Getting good results from customer PDFs

Extraction is most reliable when a drawing meets the intake checklist of [DRAWING_SPEC.md](DRAWING_SPEC.md) §3 route C:

- vector PDF with real, selectable text (not a scan or outlined text)
- first-angle projection with the projection symbol, one part per sheet, scale stated
- all dimensions in mm from one datum (bottom-left of the top view), no duplicate dimensions
- standard ISO callouts (`4x Ø6.6 THRU ⌴Ø11 ↧6.8`, `M8 ↧12`, `Ø10 H7`, `R5`, `1x45°`)
- a complete title block (part number, material, general tolerance, units, revision)
- hidden edges dashed, centre lines dash-dot

For your own parts, a layered DXF or the A4 sketch template is better than a PDF; for parts drawn in 3D CAD,
upload the STEP itself.

## Tests

```bash
cd backend && .venv/bin/python -m pytest        # ~180 tests, offline (DeepSeek mocked), ~8 min
python tests/fixtures/make_fixtures.py         # regenerate the sample drawing + expected specs

# round-trip extraction benchmark (DRAWING_SPEC phase 2): synthetic ISO drawings + fixtures -> pipeline -> diff
python -m app.services.roundtrip_bench --llm oracle --n 5                       # harness only, no API calls
python -m app.services.roundtrip_bench --llm deepseek --n 10 --thinking enabled,disabled
```

Covers every feature type and face/datum mapping with ±0.01 mm assertions, STEP round-trips, XCAF colours,
all DFM rules, the PDF parser, the sandbox (allow-list, timeout), the ISO drawing layout (first-angle placement,
no overlapping text, PDF/SVG/DXF content), the round-trip diff, the DXF parser, ArUco rectification of a tilted
synthetic photo, STEP feature recognition, the full REST/SSE flow for all four intake routes, tool-library DFM, the
stock solver, XCAF colours re-read with STEPCAFControl_Reader, QR codes decoded from the rendered PDFs, correction
telemetry and a synthetic 6-page / 3-part batch (split, throttle, ZIP).

## Project layout

```text
backend/app/
  schemas/part_spec.py      PartSpec v1.1 + DFMReport (the contract)
  services/pdf_parser.py    PyMuPDF extraction + tiling
  services/deepseek_client.py  extraction, critic, spec correction, complex-3D code, Files API reuse
  services/cad_compiler.py  deterministic build123d compiler + STEP/XCAF export
  services/cad_sandbox.py   subprocess sandbox for LLM-generated build123d code
  services/verifier.py      geometry gate, DFM rules, callout coverage, GLB + ortho renders
  services/iso_template.py  EN ISO sheet model (frame, title block, line types) → PDF / SVG / DXF
  services/drawing_engine.py  HLR views, scale/sheet solver, baseline dimensions, callouts, notes
  services/dxf_parser.py    layered DXF -> PartSpec (no AI)
  services/step_ingestor.py STEP import, datum move, feature recognition, completeness check
  services/sketch_template.py / sketch_ingestor.py  A4 ArUco template, photo rectification + crops
  services/roundtrip_bench.py  procedural parts -> ISO PDF -> pipeline -> geometric / feature diff
  services/tool_library.py / stock_catalog.py  tool library import + DFM v2 tool checks, raw-blank solver
  services/step_xcaf_exporter.py  CAM face classification + AP214 XCAF colours
  services/setup_sheet.py / qr_helper.py  A4 setup sheet, vector QR codes
  services/pdf_splitter.py / batch_runner.py  multi-page RFQ split + throttled batch runs
  services/telemetry.py     operator-correction diffs + accuracy metrics
  schemas/frames.py         face / tilted-plane frames (v2.0 and v1 conventions), pure numpy
  services/setup_planner.py / softjaw_generator.py  setup plan, Op 2 softjaws
  services/revision_diff.py / quoting_engine.py      Rev A vs B booleans + semantic diff, deterministic quotes
  services/pipeline.py      orchestrator (process pool, self-healing loop, PATCH rebuilds, intake flows)
  api/                      REST + SSE endpoints
backend/tests/              pytest suite + fixtures
frontend/src/               React 19 + TypeScript + Tailwind (PDF viewer, 3D viewer, spec editor, DFM panel)
scripts/                    service.ps1, start_tailscale.{sh,ps1}, dev.{sh,ps1}
```

## Security notes

- The server binds to loopback only; remote access is meant to go through `tailscale serve` (tailnet-only HTTPS).
  There is no built-in authentication: restrict access with Tailscale ACLs.
- LLM-generated build123d code (complex 3D features only) runs in a separate interpreter behind an AST allow-list,
  restricted builtins and a timeout. This is defence in depth, not a hard security boundary.
- Uploaded drawings are sent to the DeepSeek API for extraction. Do not upload drawings you may not share with it.

## Status

Working end to end on 2.5D prismatic milling parts: PDF, DXF, STEP and paper-sketch intake, EN ISO 2D drawings and a
round-trip extraction benchmark, a live 2D preview in the spec editor, the v1.5 CAM / shopfloor package (coloured
STEP, tool-library DFM, stock advice, setup sheets, QR tablet view, batch intake, correction telemetry) and v2.0
(multi-sided and 3+2 parts, setup plans, softjaws, revision diff, quoting). Next (see [ROADMAP.md](ROADMAP.md)):
v2.5, the learning loop on the correction telemetry.

No license has been chosen yet; until then all rights are reserved by the author.
