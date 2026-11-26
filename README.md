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

 DXF ──► layered DXF (CONTOUR_T20, POCKET_D8, HOLE_THRU, THREAD_M6_D12, SLOT_D6) → PartSpec, zero AI → 3 …
STEP ──► your own B-Rep, moved to the datum, features recognised for DFM + drawing, zero AI → 4 …
 📷  ──► photo of the printed A4 sketch template → ArUco rectification → sketch extraction (written numbers only,
          confidence ≤ 75 %, missing values asked instead of guessed) → 3 …
```

- **No mesh-to-STEP**: every solid is native B-Rep (planes, cylinders, cones), ready for CAM.
- **Schema first**: modules only exchange the versioned Pydantic `PartSpec`; raw LLM text never reaches the compiler.
- **Traceable**: every job keeps the original PDF, text/vector dump, tiles, every `PartSpec` revision, STEP, GLB,
  orthographic check renders, DFM report, iteration log and full LLM trace under `data/jobs/<job_id>/`.
- **CAM-friendly STEP**: threaded bores coloured blue, tight-tolerance bores (H7, …) red via XCAF.
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
cd backend && .venv/bin/python -m pytest        # 98 tests, offline (DeepSeek mocked), ~3-4 min
python tests/fixtures/make_fixtures.py         # regenerate the sample drawing + expected specs

# round-trip extraction benchmark (DRAWING_SPEC phase 2): synthetic ISO drawings + fixtures -> pipeline -> diff
python -m app.services.roundtrip_bench --llm oracle --n 5                       # harness only, no API calls
python -m app.services.roundtrip_bench --llm deepseek --n 10 --thinking enabled,disabled
```

Covers every feature type and face/datum mapping with ±0.01 mm assertions, STEP round-trips, XCAF colours,
all DFM rules, the PDF parser, the sandbox (allow-list, timeout), the ISO drawing layout (first-angle placement,
no overlapping text, PDF/SVG/DXF content), the round-trip diff, the DXF parser, ArUco rectification of a tilted
synthetic photo, STEP feature recognition and the full REST/SSE flow for all four intake routes.

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
round-trip extraction benchmark, plus a live 2D preview in the spec editor. Next (see [ROADMAP.md](ROADMAP.md)): the
v1.5 CAM and shopfloor package.

No license has been chosen yet; until then all rights are reserved by the author.
