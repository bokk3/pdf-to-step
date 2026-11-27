# Roadmap

Each version has its own contract document; this file tracks what is built. Status legend: ✅ done · 🚧 in progress · ⏳ planned.

| Version | Contract | Theme | Status |
|---|---|---|---|
| v1.0 | [ARCHITECTURE.md](ARCHITECTURE.md) | PDF → PartSpec → native B-Rep STEP, DFM gate, self-healing, operator UI, Tailscale | ✅ |
| v1.1 | [DRAWING_SPEC.md](DRAWING_SPEC.md) | EN ISO 2D drawings, round-trip benchmark, sketch / DXF / STEP intake | ✅ |
| v1.5 | [SPEC_V1_5_CAM_SHOPFLOOR.md](SPEC_V1_5_CAM_SHOPFLOOR.md) | CAM-ready packages: tool library DFM, XCAF colours, stock + setup sheet, QR shopfloor view, batch intake, telemetry | ✅ |
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

| Stream | Scope | Status |
|---|---|---|
| A | `tooling.py` schemas, `tool_library.py` (tools.json + Fusion 360 .json/.tools / CSV import, merge by T-number), material groups, DFM v2 tool checks (pocket corners + reach, drills / taps / thread mills / reamers with pre-drill, bores, slots, seats, chamfers, fillets, face + contour) with `recommended_tools`, `stock_catalog.py` raw-blank solver (smallest standard flat bar, plate fallback, custom block; masses + chip %), `/api/config/tools` + `/api/config/stock`, UI page `/tools` | ✅ `test_tool_dfm.py`, `test_stock_setup.py` |
| B | `step_xcaf_exporter.py`: one watertight solid with `XCAFDoc_ColorSurf` face colours via `STEPCAFControl_Writer` (AP214, sRGB) from `data/config/cam_colors.json` (thread, tight fit IT ≤ 7 / band ≤ 0.04 mm, counterbore + countersink seats, fine finish Ra ≤ 1.6, datum face); the same colours as vertex colours in `preview.glb` | ✅ `test_xcaf_colors.py` (STEPCAFControl_Reader round trip) |
| C | `qr_helper.py` (vector QR → `PUBLIC_TAILSCALE_URL/shopfloor/<job>`, warning text when unset), QR cell in the EN ISO 7200 title block, A4 `setup_sheet.pdf` (iso view + G54 triad, raw stock, tooling table, critical inspection, colour legend; `POST /setup-sheet`), `spec_corrections` telemetry (AI snapshot, diff on `PATCH /spec` and "Markeer gecontroleerd" → `POST /verify`), `GET /api/metrics/accuracy` | ✅ `test_shopfloor_telemetry.py` (QR decoded from the rendered PDFs) |
| D | `pdf_splitter.py` (title-block zone text, configurable part / sheet regexes, continuation grouping, one batched flash call for scanned pages), `batch_runner.py` + `/api/batches` (202, status, SSE, ZIP) with `MAX_CONCURRENT_JOBS`, read-only `/shopfloor/:jobId` (X/Y/Z section, colour legend filter, tap-to-inspect drawer with tools + issues), `/batches` dashboard | ✅ `test_pdf_splitter.py` (synthetic 6-page / 3-part PDF → 3 jobs + ZIP) |

Open with the workshop: run a real multi-part RFQ PDF and a scanned batch through `/batches` (the scanned-page
fallback is only exercised offline), import the real Fusion 360 tool library, and check the coloured STEP in the CAM
system (face colours → machining strategies).