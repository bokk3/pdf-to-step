# Roadmap

Each version has its own contract document; this file tracks what is built. Status legend: ✅ done · 🚧 in progress · ⏳ planned.

| Version | Contract | Theme | Status |
|---|---|---|---|
| v1.0 | [ARCHITECTURE.md](ARCHITECTURE.md) | PDF → PartSpec → native B-Rep STEP, DFM gate, self-healing, operator UI, Tailscale | ✅ |
| v1.1 | [DRAWING_SPEC.md](DRAWING_SPEC.md) | EN ISO 2D drawings, sketch / DXF / STEP intake, round-trip benchmark | 🚧 phase 1 done |
| v1.5 | [SPEC_V1_5_CAM_SHOPFLOOR.md](SPEC_V1_5_CAM_SHOPFLOOR.md) | CAM-ready packages: tool library DFM, XCAF colours, stock + setup sheet, QR shopfloor view, batch intake, telemetry | 🚧 |
| v2.5 | – | self-learning loop on the operator-correction telemetry captured from v1.5 | ⏳ |

`SPEC_V1_5_CAM_SHOPFLOOR.md` refers to the v1.1 contract as `DRAWING_ENGINE_SPEC.md`; in this repository that file is
`DRAWING_SPEC.md`.

## v1.1 — EN ISO drawing engine (DRAWING_SPEC.md §7)

- ✅ **Phase 1 — drawing generator.** `iso_template.py` + `drawing_engine.py`: first-angle front/top/left + isometric
  view from build123d HLR, baseline dimensions per view with collision avoidance, ISO callouts, EN ISO 5457 frame,
  EN ISO 7200 title block (EN or EN/NL), ISO 2768-mK / 13715 / 21920-1 notes, `DRAFT` until signed. Vector PDF with
  selectable text, SVG and layered DXF, generated for every built revision and on demand
  (`POST /api/jobs/{id}/drawing`). PartSpec v1.1 (`input_source`, `drawing_metadata`, `missing_dimensions`) with the
  `awaiting_dimensions` gate. UI: "ISO v<n>" button with sheet/scale/language options, Origineel/ISO toggle.
- ⏳ **Phase 2 — round-trip benchmark** (`roundtrip_bench.py`): procedural PartSpecs → ground-truth STEP + synthetic
  ISO PDF → full pipeline → geometric/feature diff, thinking on/off A/B, `data/benchmarks/<ts>/report.md`.
- ⏳ **Phase 3 — intake fast paths**: ArUco A4 sketch template + OpenCV ingestor (`POST /api/jobs/from-sketch`),
  layered DXF parser (`from-dxf`, zero AI), direct STEP import (`from-step`), missing-dimensions modal.
- ⏳ **Phase 4 (optional)** — live 2D SVG preview in the spec editor.

## v1.5 — CAM-ready output & shopfloor (SPEC_V1_5_CAM_SHOPFLOOR.md §6)

Implementation order on this workstation (one agent, the spec's streams in sequence):

| Stream | Scope | Status |
|---|---|---|
| A | `tooling.py` schemas, `tool_library.py` (tools.json + Fusion 360 / CSV import), material groups, DFM v2 tool checks + `recommended_tools`, `stock_catalog.py` raw-blank solver, `/api/config/tools|stock` | 🚧 |
| C (telemetry) | `spec_corrections` table, AI-initial snapshot, diff on every `PATCH /spec`, `GET /api/metrics/accuracy` | ⏳ |
| B | `step_xcaf_exporter.py` with `cam_colors.json` (threads, fits, counterbore/countersink seats, datum face), STEP re-read test, identical colours in `preview.glb` | ⏳ (threads + fits already coloured since v1.0) |
| C | `qr_helper.py` (vector QR → `PUBLIC_TAILSCALE_URL/shopfloor/<job>`), QR in the drawing title block, 1-page A4 `setup_sheet.pdf` | ⏳ |
| D | `pdf_splitter.py` + `/api/batches` (bounded concurrency, ZIP export), `/shopfloor/:jobId` read-only tablet view with clipping + colour legend, batch dashboard, tool library page | ⏳ |

Phases 2–4 of v1.1 stay on the list and are picked up after v1.5 unless priorities change.
