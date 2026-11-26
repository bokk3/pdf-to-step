# Roadmap

Each version has its own contract document; this file tracks what is built. Status legend: ✅ done · 🚧 in progress · ⏳ planned.

| Version | Contract | Theme | Status |
|---|---|---|---|
| v1.0 | [ARCHITECTURE.md](ARCHITECTURE.md) | PDF → PartSpec → native B-Rep STEP, DFM gate, self-healing, operator UI, Tailscale | ✅ |
| v1.1 | [DRAWING_SPEC.md](DRAWING_SPEC.md) | EN ISO 2D drawings, round-trip benchmark, sketch / DXF / STEP intake | ✅ |
| v1.5 | [SPEC_V1_5_CAM_SHOPFLOOR.md](SPEC_V1_5_CAM_SHOPFLOOR.md) | CAM-ready packages: tool library DFM, XCAF colours, stock + setup sheet, QR shopfloor view, batch intake, telemetry | ⏳ after v1.1 |
| v2.5 | – | self-learning loop on the operator-correction telemetry captured from v1.5 | ⏳ |

Versions are built in order: v1.1 is finished (all phases) before v1.5 starts, because v1.5 lists v1.1 as its
prerequisite. `SPEC_V1_5_CAM_SHOPFLOOR.md` refers to the v1.1 contract as `DRAWING_ENGINE_SPEC.md`; in this
repository that file is `DRAWING_SPEC.md`.

## v1.1 — EN ISO drawings & intake fast paths (DRAWING_SPEC.md §7, strict phase order)

- ✅ **Spec audit (2026-11-27)**: every DRAWING_SPEC requirement checked against the build; added the ISO
  symbols ⌴ ⌵ in callouts, ≥ 2.5 mm lettering everywhere, EN ISO 6410 threads, the hatched section A–A (ISO 128-50),
  hole / counterbore depths in the front view, GD&T + surface + extra notes, the `DrawingEngine` protocol and the OCR
  experiment. Conscious deviations are listed in ARCHITECTURE §11.8.
- ✅ **Phase 1 — EN ISO drawing generator.** `iso_template.py` + `drawing_engine.py`: first-angle front/top/left +
  isometric view from build123d HLR, baseline dimensions per view with collision avoidance, ISO callouts,
  EN ISO 5457 frame, EN ISO 7200 title block (EN or EN/NL), ISO 2768-mK / 13715 / 21920-1 notes, `DRAFT` until
  signed. Vector PDF with selectable text, SVG and layered DXF for every built revision and on demand
  (`POST /api/jobs/{id}/drawing`). PartSpec v1.1 (`input_source`, `drawing_metadata`, `missing_dimensions`) with the
  `awaiting_dimensions` gate. UI: "ISO v<n>" button with sheet / scale / language options, Origineel/ISO toggle.
- ✅ **Phase 2 — round-trip benchmark.** `roundtrip_bench.py` + `test_roundtrip.py`: procedural PartSpecs →
  ground-truth STEP + synthetic ISO PDF → full pipeline → geometric and order-independent feature diff (valid
  solid, bbox ±0.05 mm, volume < 0.5 %, nearest-neighbour feature matching ±0.10 mm, precision/recall per type),
  thinking on/off and prompt A/B, real-fixture section, `data/benchmarks/<ts>/report.{md,json}`. First real run:
  ARCHITECTURE.md §11.9 (thinking on wins on the fixture PDF; equal on clean synthetic drawings).
- ✅ **Phase 3 — intake fast paths.** `sketch_template.py` (A4, 4 ArUco markers, first-angle boxes, datum, title
  fields, checklist; `GET /api/templates/sketch-a4.pdf`), `sketch_ingestor.py` (OpenCV homography warp + crops,
  sketch prompt, confidence ≤ 0.75, missing dimensions → `awaiting_dimensions`; `POST /api/jobs/from-sketch`),
  `dxf_parser.py` (layered DXF, zero AI; `from-dxf`), `step_ingestor.py` (direct B-Rep import, DFM, colours,
  drawing; `from-step`). UI: camera capture, "Download Schetssjabloon (A4)", `MissingDimensionsModal`.
  Files API `text_recognition` / `rich_layout_recognition`: verified, wired in (`SKETCH_OCR`), benchmarked on 5
  synthetic handwritten sketches: no gain once the scan keeps faint pencil, so off by default (ARCHITECTURE §11.10).
  To do with the workshop: re-run that comparison on real pencil sketches.
- ✅ **Phase 4 (optional) — live 2D preview.** `SpecPreview2D.tsx`: first-angle front + top view of the PartSpec
  being edited, drawn client-side in datum mm (patterns expanded like the backend, hidden / bottom features dashed,
  low-confidence features amber, datum marked). Follows every keystroke; clicking a shape selects its feature.
  Toggle "2D" in the editor header (remembered per browser).

## v1.5 — CAM-ready output & shopfloor (SPEC_V1_5_CAM_SHOPFLOOR.md §6)

Starts after v1.1. Streams as defined in the spec:

| Stream | Scope | Status |
|---|---|---|
| A | `tooling.py` schemas, `tool_library.py` (tools.json + Fusion 360 / CSV import), material groups, DFM v2 tool checks + `recommended_tools`, `stock_catalog.py` raw-blank solver, `/api/config/tools` + `/api/config/stock` | ⏳ untested WIP parked on branch `feat/v1.5-cam-shopfloor` |
| B | `step_xcaf_exporter.py` with `cam_colors.json` (threads, fits, counterbore/countersink seats, datum face), STEP re-read test, identical colours in `preview.glb` | ⏳ (threads + fits are coloured since v1.0) |
| C | `qr_helper.py` (vector QR → `PUBLIC_TAILSCALE_URL/shopfloor/<job>`), QR in the drawing title block, 1-page A4 `setup_sheet.pdf`, `spec_corrections` telemetry + `GET /api/metrics/accuracy` | ⏳ |
| D | `pdf_splitter.py` + `/api/batches` (bounded concurrency, ZIP export), `/shopfloor/:jobId` read-only tablet view with clipping + colour legend, batch dashboard, tool library page | ⏳ |
