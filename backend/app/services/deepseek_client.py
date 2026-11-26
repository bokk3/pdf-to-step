"""DeepSeek integration (ARCHITECTURE.md §2 stages 2 + 5, §4).

* Stage 2 - ``extract_part_spec`` / ``correct_spec``: DEEPSEEK_VISION_MODEL with the
  full sheet, overlapping high-res tiles, the title block and the PyMuPDF text dump.
  Thinking mode + JSON output. Output is always validated into ``PartSpec``.
* Stage 5 - ``critique``: the vision model compares ``ortho_check.png`` with the
  drawing and returns a structured discrepancy report (it never edits the spec;
  corrections are routed back through ``correct_spec``).
* Layer 2 - ``generate_complex_code``: DEEPSEEK_CODE_MODEL, text only (PartSpec JSON +
  kernel error log, never images).

Drawing images are uploaded once per job through the DeepSeek Files API
(purpose ``user_data``) and re-used by ``file_id`` on every retry; base64 data
URIs are the automatic fallback. Raw model text never leaves this module.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, Protocol

from pydantic import BaseModel, Field, ValidationError

from app.config import Settings
from app.schemas.part_spec import (
    Complex3D,
    MissingDimension,
    PartSpec,
    Stock,
    parse_metric_thread,
    partspec_json_schema,
)

log = logging.getLogger(__name__)

CONVENTIONS = """\
PARTSPEC CONVENTIONS (mandatory):
- Millimetres only. If the drawing is in inches, convert every value to mm. Angles in degrees.
- datum_origin "bottom_left_top_face" (default): origin on the TOP face, at the bottom-left corner of the top
  (plan) view. X = right, Y = away from the viewer in the top view (up on the sheet in the plan view), Z = up.
  The part occupies x in [0, length_x], y in [0, width_y], z in [-thickness_z, 0].
- stock = the raw block envelope: length_x (X), width_y (Y), thickness_z (Z).
- outer_profile: "rectangle", "rounded_rectangle" (corner_radius = the 4 vertical outer corners) or "polygon"
  (points_2d in datum X/Y, corner_radius applied to every vertex).
- Every feature sits on one face: top|bottom|front|back|left|right. center_x/center_y (and start_/end_/x_min..)
  are DATUM coordinates projected on that face, never mirrored:
    top/bottom -> (x, y) = (X, Y)
    front/back -> (x, y) = (X, Z)   front = the Y-min face, Z is negative below the top face
    left/right -> (x, y) = (Y, Z)   left  = the X-min face
  Example: an M6 tapped hole in the front face, 30 mm from the left end, at mid-height of a 12 mm plate ->
  face "front", center_x 30, center_y -6.
  Convert every baseline / chain / ordinate dimension into these absolute coordinates.
- Cuts: depth = blind depth from the face (excluding drill point); through_all = true for THRU (depth null).
- Holes: diameter = NOMINAL size. Threads: thread "M8" (or "M8x1"), diameter 8, thread_depth = usable thread
  length, depth = drill depth. Counterbore {diameter, depth}; countersink {diameter, angle_deg}.
  tolerance as written on the drawing ("H7", "+0.015/0", "±0.02").
- Repeated identical features ("4x"): ONE feature with center = first instance plus
  pattern {type "linear", count_x, count_y, pitch_x, pitch_y} or {type "circular", radius, count,
  start_angle_deg} (circular: center = bolt-circle centre), or extra_positions [[x, y], ...].
- pocket_rect: center, length_x, width_y (before rotation_deg), corner_radius = internal vertical corner radius.
  pocket_circle: diameter. pocket_polygon: points_2d. slot: start_x/start_y -> end_x/end_y = the CENTRES of the two end arcs + width.
  Slot dimensions that end on centre marks / the slot centre line are already arc centres: use them as they are
  (never subtract the radius). Only when a drawing gives the OVERALL slot length, centre distance = overall - width.
  boss_rect / boss_circle: raised extrusion of height out of the face.
  step_cut: open edge cut-out / shoulder = box x_min..x_max, y_min..y_max (face coords, may extend past the
  stock edge) removed to depth.
- edge_treatments: chamfer/fillet of size on target top_outer_edges | bottom_outer_edges |
  vertical_outer_edges | all_outer_edges ("break all sharp edges 0.2" is a note, not a treatment).
- Only use a "complex_3d" feature for geometry that is NOT 2.5D prismatic (lofts, freeform, compound angles):
  describe it precisely, give bbox_min/bbox_max, never write code; then requires_complex_3d_fallback = true.
- confidence in [0,1] on stock, outer_profile, every feature and edge treatment: < 0.85 whenever a value is
  inferred, scaled, partly illegible or ambiguous. Put open questions in "ambiguities" (operator language).
- COMPLETENESS: check EVERY view (front, side, section, detail views), not only the plan view. Every
  manufacturing callout in the text layer (Ø.., M.., R.., ..x45°, POCKET, SLOT, CBORE, CSK, DEPTH ...) must end
  up in a feature, an edge treatment, the outer profile, or an "ambiguities" entry. "4x" means 4 instances.
- NEVER invent features. The PDF TEXT LAYER values are authoritative over what you read in the images.
"""

EXAMPLE_SPEC = {
    "schema_version": "1.0", "part_name": "Montageplaat_A12", "units": "mm", "material": "AlMgSi1 (6082-T6)",
    "general_tolerance": "ISO 2768-mK", "datum_origin": "bottom_left_top_face",
    "stock": {"length_x": 200.0, "width_y": 100.0, "thickness_z": 20.0, "confidence": 0.98},
    "outer_profile": {"type": "rounded_rectangle", "corner_radius": 5.0, "points_2d": None, "confidence": 0.95},
    "features": [
        {"id": "feat_pocket_1", "type": "pocket_rect", "face": "top", "center_x": 100.0, "center_y": 50.0,
         "length_x": 120.0, "width_y": 60.0, "depth": 8.0, "corner_radius": 6.0, "confidence": 0.92,
         "source_note": "Top view central pocket"},
        {"id": "feat_hole_1", "type": "hole", "face": "top", "center_x": 15.0, "center_y": 15.0, "diameter": 8.0,
         "depth": None, "through_all": True, "thread": "M8", "counterbore": None, "tolerance": None,
         "pattern": {"type": "linear", "count_x": 2, "count_y": 2, "pitch_x": 170.0, "pitch_y": 70.0},
         "confidence": 0.78, "source_note": "4x M8 callout slightly blurred"},
        {"id": "feat_hole_2", "type": "hole", "face": "front", "center_x": 100.0, "center_y": -10.0,
         "diameter": 6.6, "depth": 25.0, "through_all": False, "counterbore": {"diameter": 11.0, "depth": 6.8},
         "confidence": 0.9, "source_note": "Front view, Ø6.6 / Ø11x6.8"},
    ],
    "edge_treatments": [
        {"id": "edge_1", "type": "chamfer", "size": 1.0, "target": "top_outer_edges", "confidence": 0.90},
    ],
    "requires_complex_3d_fallback": False,
    "ambiguities": ["Verify whether the 4 corner holes are through-holes or 15mm blind."],
}


# --------------------------------------------------------------------------- #
# Typed envelopes
# --------------------------------------------------------------------------- #


class Discrepancy(BaseModel):
    feature_id: str | None = None
    issue: str
    evidence: str = Field("", description="exact text-layer callout / dimension that contradicts the model")
    severity: Literal["major", "minor"] = "major"
    suggested_fix: str | None = None


class CriticReport(BaseModel):
    verdict: Literal["match", "mismatch", "skipped"]
    discrepancies: list[Discrepancy] = Field(default_factory=list)

    @property
    def actionable(self) -> list[Discrepancy]:
        """Major discrepancies backed by evidence; minor or unsupported claims never trigger a correction."""
        return [d for d in self.discrepancies if d.severity == "major" and d.evidence.strip()]

    @property
    def summary(self) -> list[str]:
        return [f"{d.feature_id + ': ' if d.feature_id else ''}{d.issue}"
                + (f" [bewijs: {d.evidence}]" if d.evidence else "") for d in self.discrepancies]


@dataclass
class ExtractionImage:
    label: str
    path: Path


@dataclass
class LLMTrace:
    """Raw exchanges, persisted per job under llm/ for traceability."""

    calls: list[dict[str, Any]] = field(default_factory=list)

    def add(self, kind: str, model: str, request_summary: str, response_text: str, error: str | None = None,
            meta: dict[str, Any] | None = None):
        self.calls.append({"kind": kind, "model": model, "request": request_summary[:4000],
                           "response": response_text[:30000], "error": error, "meta": meta})


class EmptyResponse(RuntimeError):
    pass


class LLMClient(Protocol):
    async def extract_part_spec(self, text_digest: str, vector_digest: str, images: list[ExtractionImage],
                                trace: LLMTrace, filename: str = "", hint: str | None = None,
                                previous: PartSpec | None = None) -> PartSpec: ...

    async def extract_sketch_spec(self, images: list[ExtractionImage], trace: LLMTrace, filename: str = "",
                                  hint: str | None = None) -> PartSpec: ...

    async def correct_spec(self, spec: PartSpec, problems: list[str], text_digest: str,
                           images: list[ExtractionImage], trace: LLMTrace) -> PartSpec: ...

    async def critique(self, spec: PartSpec, drawing: list[ExtractionImage], ortho_check: Path, text_digest: str,
                       trace: LLMTrace, hints: list[str] | None = None) -> CriticReport: ...

    async def generate_complex_code(self, spec: PartSpec, feature: Complex3D, previous_code: str | None,
                                    error_log: str | None, trace: LLMTrace) -> str: ...

    async def ping(self) -> dict[str, Any]: ...

    async def release_files(self, job_dir: Path) -> int: ...


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _data_uri(path: Path) -> dict:
    b64 = base64.b64encode(path.read_bytes()).decode()
    return {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}}


def _extract_json(text: str) -> Any:
    text = text.strip()
    m = re.search(r"```(?:json)?\s*(.*?)```", text, re.S)
    if m:
        text = m.group(1).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start >= 0 and end > start:
            return json.loads(text[start : end + 1])
        raise


_NULL_DROP = {"confidence", "rotation_deg", "corner_radius", "extra_positions", "features", "edge_treatments",
              "ambiguities", "outer_profile", "through_all", "face", "datum_origin", "units", "angle_deg",
              "requires_complex_3d_fallback", "operation", "schema_version", "input_source", "drawing_metadata",
              "missing_dimensions", "sheet_size", "scale", "language", "revision", "approval_status", "projection",
              "surface_finish", "edge_break_note", "drawn_by", "checked_by", "blocking", "prompt_en"}


def _strip_nulls(obj: Any) -> Any:
    """LLMs emit explicit nulls for fields that have non-null defaults; drop those keys."""
    if isinstance(obj, dict):
        return {k: _strip_nulls(v) for k, v in obj.items() if not (v is None and k in _NULL_DROP)}
    if isinstance(obj, list):
        return [_strip_nulls(v) for v in obj]
    return obj


def post_process(spec: PartSpec) -> PartSpec:
    """Deterministic normalisation of common extraction slips."""
    data = spec.model_dump(mode="json")
    changed = False
    for f in data["features"]:
        if f["type"] == "hole" and f.get("thread"):
            t = parse_metric_thread(f["thread"])
            if t and abs(f["diameter"] - (t[0] - t[1])) < 0.11 and f["diameter"] < t[0]:  # tap drill -> nominal
                f["diameter"] = t[0]
                changed = True
    return PartSpec.model_validate(data) if changed else spec


def placeholder_spec(reason: str) -> PartSpec:
    return PartSpec(
        part_name="ONGECONTROLEERD - aanpassen",
        stock=Stock(length_x=100, width_y=50, thickness_z=10, confidence=0.0),
        ambiguities=[f"Placeholder: {reason}. Vul stock en features in en klik 'Herbereken STEP'."],
    )


def _summarise(messages: list[dict]) -> str:
    parts = []
    for m in messages:
        c = m["content"]
        if isinstance(c, str):
            parts.append(f"[{m['role']}] {c[:600]}")
        else:
            n_img = sum(1 for x in c if x.get("type") in ("image_url", "file"))
            txt = " ".join(x.get("text", "") for x in c if x.get("type") == "text")
            parts.append(f"[{m['role']}] ({n_img} images) {txt[:1500]}")
    return "\n".join(parts)


# --------------------------------------------------------------------------- #
# Real client
# --------------------------------------------------------------------------- #


class DeepSeekClient:
    def __init__(self, settings: Settings):
        from openai import AsyncOpenAI

        self.s = settings
        self.client = AsyncOpenAI(api_key=settings.deepseek_api_key, base_url=settings.deepseek_base_url,
                                  timeout=settings.deepseek_timeout_s, max_retries=2)
        self._thinking_ok = True
        self._max_tokens = settings.deepseek_max_tokens
        self._files_ok = settings.deepseek_use_files_api

    # -- images: Files API with base64 fallback ---------------------------------
    @staticmethod
    def _cache_path(img: ExtractionImage) -> Path:
        return img.path.parents[1] / "llm" / "file_ids.json"

    async def _image_parts(self, images: list[ExtractionImage]) -> list[dict]:
        parts: list[dict] = []
        if not images:
            return parts
        cache_p = self._cache_path(images[0])
        cache: dict[str, str] = json.loads(cache_p.read_text(encoding="utf-8")) if cache_p.exists() else {}
        dirty = False
        for img in images:
            parts.append({"type": "text", "text": f"--- image: {img.label} ---"})
            if self._files_ok:
                key = f"{img.path.name}:{hashlib.sha1(img.path.read_bytes()).hexdigest()[:16]}"
                fid = cache.get(key)
                if not fid:
                    try:
                        with open(img.path, "rb") as fh:
                            fid = (await self.client.files.create(file=fh, purpose="user_data")).id
                        cache[key] = fid
                        dirty = True
                    except Exception as exc:  # noqa: BLE001 - fall back to inline images
                        log.warning("Files API upload failed, using base64: %s", exc)
                        self._files_ok = False
                if fid and self._files_ok:
                    parts.append({"type": "file", "file_id": fid})
                    continue
            parts.append(_data_uri(img.path))
        if dirty:
            cache_p.parent.mkdir(parents=True, exist_ok=True)
            cache_p.write_text(json.dumps(cache, indent=1), encoding="utf-8")
        return parts

    async def release_files(self, job_dir: Path) -> int:
        """Delete this job's uploaded images from DeepSeek (called on job deletion)."""
        p = job_dir / "llm" / "file_ids.json"
        if not p.exists():
            return 0
        n = 0
        for fid in json.loads(p.read_text(encoding="utf-8")).values():
            try:
                await self.client.files.delete(fid)
                n += 1
            except Exception:  # noqa: BLE001
                pass
        p.unlink(missing_ok=True)
        return n

    # -- low level ------------------------------------------------------------
    async def _chat(self, model: str, messages: list[dict], kind: str, trace: LLMTrace,
                    thinking: str = "default") -> str:
        from openai import BadRequestError

        kwargs: dict[str, Any] = {"model": model, "messages": messages, "max_tokens": self._max_tokens,
                                  "response_format": {"type": "json_object"}}
        if thinking != "default" and self._thinking_ok:
            kwargs["extra_body"] = {"thinking": {"type": thinking}}
        summary = _summarise(messages)
        resp = None
        for _ in range(6):  # degrade gracefully on unsupported parameters
            try:
                resp = await self.client.chat.completions.create(**kwargs)
                break
            except BadRequestError as exc:
                msg = str(exc).lower()
                if "extra_body" in kwargs and "think" in msg:
                    self._thinking_ok = False
                    kwargs.pop("extra_body")
                elif "max_tokens" in msg and kwargs["max_tokens"] > 4096:
                    self._max_tokens = kwargs["max_tokens"] = kwargs["max_tokens"] // 2
                elif "response_format" in kwargs and "response_format" in msg:
                    kwargs.pop("response_format")
                elif "file" in msg and any(p.get("type") == "file" for m in messages
                                           if isinstance(m["content"], list) for p in m["content"]):
                    raise FileRefError(str(exc)) from exc
                else:
                    trace.add(kind, model, summary, "", error=str(exc))
                    raise
        if resp is None:
            raise RuntimeError(f"{kind}: request kept failing parameter negotiation")
        choice = resp.choices[0]
        text = choice.message.content or ""
        reasoning = getattr(choice.message, "reasoning_content", None) or ""
        usage = resp.usage.model_dump() if getattr(resp, "usage", None) else None
        trace.add(kind, model, summary, text,
                  meta={"finish_reason": choice.finish_reason, "usage": usage, "reasoning_chars": len(reasoning)})
        if not text.strip():
            raise EmptyResponse(f"{kind}: empty completion (finish_reason={choice.finish_reason})")
        return text

    async def _json(self, model: str, build_messages, kind: str, trace: LLMTrace, validate,
                    thinking: str = "default", rounds: int = 2, empty_retries: int = 2):
        """Call, parse, validate; on validation errors feed them back (max ``rounds``)."""
        try:
            messages = await build_messages()
            return await self._json_inner(model, messages, kind, trace, validate, thinking, rounds, empty_retries)
        except FileRefError as exc:  # stale / unsupported file ids -> rebuild with inline images
            log.warning("file_id reference rejected (%s); retrying with base64 images", exc)
            self._files_ok = False
            messages = await build_messages()
            return await self._json_inner(model, messages, kind, trace, validate, thinking, rounds, empty_retries)

    async def _json_inner(self, model, messages, kind, trace, validate, thinking, rounds, empty_retries):
        last_err: Exception | None = None
        attempt = 0
        while attempt <= rounds:
            try:
                text = await self._chat(model, messages, kind, trace, thinking=thinking)
            except EmptyResponse as exc:
                last_err = exc
                empty_retries -= 1
                if empty_retries < 0:
                    break
                continue
            attempt += 1
            try:
                return validate(_strip_nulls(_extract_json(text)))
            except (json.JSONDecodeError, ValidationError, ValueError) as exc:
                last_err = exc
                messages = messages + [
                    {"role": "assistant", "content": text[:12000]},
                    {"role": "user", "content": f"That JSON failed validation:\n{str(exc)[:4000]}\n"
                                                "Return the COMPLETE corrected JSON object only."},
                ]
        raise ValueError(f"{kind}: model output failed validation after retries: {last_err}")

    def _schema_block(self) -> str:
        return ("\nOutput: ONE json object that validates against this JSON Schema (no prose, no markdown):\n"
                + json.dumps(partspec_json_schema(), separators=(",", ":"))
                + "\n\nExample output json:\n" + json.dumps(EXAMPLE_SPEC, separators=(",", ":")))

    def _drawing_intro(self, filename: str, text_digest: str, vector_digest: str) -> str:
        return (
            f"Drawing file: {filename}\n"
            "You get (1) the exact PDF TEXT LAYER with positions in sheet millimetres (origin top-left), "
            "(2) vector hints, (3) images: the full sheet, then overlapping high-resolution tiles, then the title "
            "block. Tiles keep small callouts legible; the text layer is authoritative for numbers.\n\n"
            f"=== PDF TEXT LAYER ===\n{text_digest}\n\n=== VECTOR HINTS ===\n{vector_digest}\n"
        )

    # -- Stage 2: extraction --------------------------------------------------
    async def extract_part_spec(self, text_digest: str, vector_digest: str, images: list[ExtractionImage],
                                trace: LLMTrace, filename: str = "", hint: str | None = None,
                                previous: PartSpec | None = None) -> PartSpec:
        system = ("You are a senior CNC manufacturing engineer reading 2D technical drawings (ISO/ASME, often Dutch "
                  "or German). Convert the drawing into a PartSpec JSON object for a deterministic build123d CAD "
                  "compiler. You do NOT write CAD code.\n\n" + CONVENTIONS + self._schema_block())
        if self.s.extraction_prompt_appendix:  # prompt A/B variants (roundtrip_bench --prompt-variant)
            system += "\n\nADDITIONAL INSTRUCTIONS:\n" + self.s.extraction_prompt_appendix

        async def build():
            content = [{"type": "text", "text": self._drawing_intro(filename, text_digest, vector_digest)}]
            content += await self._image_parts(images)
            if previous is not None:
                content.append({"type": "text", "text": "Previous PartSpec (may contain operator corrections; keep "
                                                        "what is right):\n" + previous.model_dump_json(exclude_none=True)})
            if hint:
                content.append({"type": "text", "text": f"OPERATOR HINT (authoritative, may be Dutch): {hint}"})
            content.append({"type": "text", "text": "Return the PartSpec json now."})
            return [{"role": "system", "content": system}, {"role": "user", "content": content}]

        spec = await self._json(self.s.deepseek_vision_model, build, "extract", trace, PartSpec.model_validate,
                                thinking=self.s.deepseek_thinking)
        return post_process(spec)

    async def extract_sketch_spec(self, images: list[ExtractionImage], trace: LLMTrace, filename: str = "",
                                  hint: str | None = None) -> PartSpec:
        """DRAWING_SPEC §3 route A: a hand sketch on the ArUco A4 template (rectified sheet + view crops)."""
        system = (
            "You read HAND SKETCHES of CNC milling parts drawn on a printed A4 template and convert them into a PartSpec "
            "JSON object for a deterministic CAD compiler. The template has three first-angle view boxes "
            "(VOORAANZICHT = front, top-left; ZIJAANZICHT = left side view, top-right; BOVENAANZICHT = top view, "
            "below the front view), a title block (part name, material, thickness Z, general tolerance) and the "
            "datum (0,0) printed at the bottom-left corner of the top-view box (X to the right, Y up).\n\n"
            "SKETCH RULES (strict):\n"
            "1. Sketches are NOT to scale. Only WRITTEN numbers count; never measure the drawing or derive a value "
            "from proportions or the grid.\n"
            "2. Never guess. A value that is not written (thickness Z, a pocket or hole depth, a position, a thread "
            "size) goes into `missing_dimensions` with `blocking: true`, a Dutch `prompt_nl` question and the JSON "
            "field path (e.g. `stock.thickness_z`, `features.P1.depth`). Put a harmless placeholder in the field "
            "itself (1 for a missing thickness or depth) so the JSON validates.\n"
            "3. Set every `confidence` to at most 0.75: an operator must confirm each value.\n"
            "4. Coordinates are measured from the printed (0,0). If the sketch uses another origin, convert and add "
            "an `ambiguities` note.\n"
            "5. Handwriting: read digits carefully (1/7, 3/8, 5/6, decimal comma). If a number is unreadable, treat "
            "it as missing (rule 2).\n"
            "6. `input_source` is \"paper_sketch\"; units are mm.\n\n"
            + CONVENTIONS + self._schema_block())
        if self.s.extraction_prompt_appendix:
            system += "\n\nADDITIONAL INSTRUCTIONS:\n" + self.s.extraction_prompt_appendix

        async def build():
            content = [{"type": "text", "text": f"Sketch file: {filename or '-'}. Images: the rectified sheet, then "
                                                "crops of the front, side, top and title boxes (grid removed)."}]
            content += await self._image_parts(images)
            if hint:
                content.append({"type": "text", "text": f"OPERATOR HINT (authoritative, may be Dutch): {hint}"})
            content.append({"type": "text", "text": "Return the PartSpec json now."})
            return [{"role": "system", "content": system}, {"role": "user", "content": content}]

        spec = await self._json(self.s.deepseek_vision_model, build, "extract_sketch", trace, PartSpec.model_validate,
                                thinking=self.s.deepseek_thinking)
        return post_process(spec)

    async def correct_spec(self, spec: PartSpec, problems: list[str], text_digest: str,
                           images: list[ExtractionImage], trace: LLMTrace) -> PartSpec:
        """Stage 5 -> Stage 2: fix the PartSpec from kernel errors / critic discrepancies, with the drawing."""
        system = ("You correct PartSpec JSON documents. You get the drawing (text layer + images), the current "
                  "PartSpec and a list of problems: CAD kernel errors, verifier failures or visual discrepancies "
                  "reported by a reviewer who compared renders of the model with the drawing.\n"
                  "Kernel / verifier errors are facts: fix them. Reviewer discrepancies are CLAIMS: verify each one "
                  "against the PDF text layer and the drawing first. If the drawing supports the current value, keep "
                  "it (optionally add a note to ambiguities) - never move a feature away from a value that a "
                  "dimension on the drawing confirms. Change only what is needed; keep feature ids; set the "
                  "confidence of every feature you change to <= 0.8.\n\n"
                  + CONVENTIONS + self._schema_block())

        async def build():
            content = [{"type": "text", "text": self._drawing_intro("", text_digest[:20000], "")}]
            content += await self._image_parts(images)
            content.append({"type": "text", "text": "Current PartSpec:\n" + spec.model_dump_json(exclude_none=True)})
            content.append({"type": "text", "text": "Problems to fix:\n- " + "\n- ".join(problems)})
            content.append({"type": "text", "text": "Return the complete corrected PartSpec json."})
            return [{"role": "system", "content": system}, {"role": "user", "content": content}]

        out = await self._json(self.s.deepseek_vision_model, build, "correct", trace, PartSpec.model_validate,
                               thinking=self.s.deepseek_thinking)
        return post_process(out)

    # -- Stage 5: visual critic -----------------------------------------------
    async def critique(self, spec: PartSpec, drawing: list[ExtractionImage], ortho_check: Path, text_digest: str,
                       trace: LLMTrace, hints: list[str] | None = None) -> CriticReport:
        system = (
            "You are a meticulous CAD/CAM checker. Image set A is the ORIGINAL 2D drawing. The last image is "
            "ortho_check.png: hidden-line FRONT, RIGHT, TOP and ISO views (hidden edges dashed, one common scale) of "
            "the 3D model built from the PartSpec JSON.\n"
            "Report only discrepancies that matter for manufacturing: missing or extra features, wrong instance "
            "counts, wrong face, through vs blind, clearly wrong sizes or positions.\n"
            "EVIDENCE RULE: every discrepancy needs `evidence` = the exact callout or dimension text from the PDF text "
            "layer (or a clearly visible feature for a missing one) that the PartSpec contradicts. Renders are "
            "approximate: never report a position or size difference that is only based on eyeballing the images "
            "when the PartSpec value equals a dimension on the drawing. Mark cosmetic or doubtful findings as "
            "severity 'minor'. If nothing major is wrong, the verdict is 'match'.\n" + CONVENTIONS
            + '\nRespond with json: {"verdict": "match" | "mismatch", "discrepancies": [{"feature_id": "<id or null>", '
              '"issue": "...", "evidence": "<text-layer callout>", "severity": "major" | "minor", '
              '"suggested_fix": "..."}]}'
        )

        async def build():
            content = [{"type": "text", "text": "PartSpec used for the model:\n" + spec.model_dump_json(exclude_none=True)},
                       {"type": "text", "text": "PDF text layer:\n" + text_digest[:20000]}]
            if hints:
                content.append({"type": "text", "text": "Deterministic callout check (text layer vs PartSpec) flagged:\n- "
                                                        + "\n- ".join(hints)})
            content.append({"type": "text", "text": "=== A: original drawing ==="})
            content += await self._image_parts(drawing)
            content += [{"type": "text", "text": "=== B: ortho_check.png of the generated model ==="},
                        _data_uri(ortho_check)]
            return [{"role": "system", "content": system}, {"role": "user", "content": content}]

        return await self._json(self.s.deepseek_vision_model, build, "critic", trace, CriticReport.model_validate,
                                thinking=self.s.deepseek_critic_thinking)

    # -- Layer 2: complex_3d code (text only) ---------------------------------
    async def generate_complex_code(self, spec: PartSpec, feature: Complex3D, previous_code: str | None,
                                    error_log: str | None, trace: LLMTrace) -> str:
        from app.services.cad_sandbox import SandboxRejected, check_code

        system = (
            "You write build123d (Python, Open CASCADE) code for ONE non-prismatic CNC feature. Rules:\n"
            "- Only `from build123d import *` and `import math` (numpy allowed). No file, OS or network access, "
            "no exporting, no dunder attributes, no getattr/eval/exec/open.\n"
            "- build123d ALGEBRA mode (Box, Cylinder, Cone, Sphere, Torus, extrude, revolve, loft, sweep, Pos, Rot, "
            "Plane, Location, fillet, chamfer, Sketch objects).\n"
            "- Work in the PartSpec datum frame in mm (see conventions). Do NOT model the stock.\n"
            "- operation 'cut': `result` is the TOOL BODY to subtract; it must overlap the stock and extend 0.1 mm "
            "past faces it breaks through. operation 'union': `result` is the body to add, overlapping >= 0.05 mm.\n"
            "- Assign the final single solid to `result`. Under 80 lines, deterministic, runs < 15 s.\n"
            + CONVENTIONS + '\nRespond with json: {"code": "<python source>", "explanation": "<one sentence>"}'
        )
        user = (f"PartSpec (context):\n{spec.model_dump_json(exclude_none=True)}\n\n"
                f"Feature to build:\n{feature.model_dump_json(exclude={'code'})}\n")
        if previous_code:
            user += f"\nPrevious attempt:\n```python\n{previous_code}\n```\n"
        if error_log:
            user += f"\nKernel / sandbox error log:\n{error_log[-3000:]}\nFix the code.\n"

        async def build():
            return [{"role": "system", "content": system}, {"role": "user", "content": user}]

        def validate(obj):
            code = obj.get("code") if isinstance(obj, dict) else None
            if not code or not isinstance(code, str):
                raise ValueError("missing 'code' string")
            try:
                check_code(code)
            except SandboxRejected as exc:
                raise ValueError(f"code violates sandbox rules: {exc}") from exc
            return code

        return await self._json(self.s.deepseek_code_model, build, "complex_code", trace, validate)

    async def ping(self) -> dict[str, Any]:
        try:
            ids = [m.id for m in (await self.client.models.list()).data]
            return {"reachable": True, "models": ids,
                    "vision_model_listed": self.s.deepseek_vision_model in ids,
                    "code_model_listed": self.s.deepseek_code_model in ids}
        except Exception as exc:  # noqa: BLE001
            return {"reachable": False, "error": f"{type(exc).__name__}: {exc}"[:500]}


class FileRefError(RuntimeError):
    pass


# --------------------------------------------------------------------------- #
# Offline mock (tests, or no API key configured)
# --------------------------------------------------------------------------- #


class MockDeepSeekClient:
    """Deterministic stand-in: returns ``<stem>.part_spec.json`` from the fixture dirs."""

    def __init__(self, fixture_dirs: list[Path] | None = None, critic_verdict: str = "match",
                 fake_complex_code: bool = False):
        self.fixture_dirs = fixture_dirs or []
        self.critic_verdict = critic_verdict
        self.fake_complex_code = fake_complex_code
        self.calls: list[str] = []
        self.hints: list[str] = []

    async def extract_part_spec(self, text_digest, vector_digest, images, trace, filename: str = "",
                                hint: str | None = None, previous: PartSpec | None = None) -> PartSpec:
        self.calls.append("extract")
        if hint:
            self.hints.append(hint)
        if previous is not None:
            return previous
        for d in self.fixture_dirs:
            p = d / f"{Path(filename).stem}.part_spec.json"
            if p.exists():
                trace.add("extract", "mock", filename, p.read_text(encoding="utf-8"))
                return post_process(PartSpec.model_validate_json(p.read_text(encoding="utf-8")))
        return placeholder_spec("DeepSeek is niet geconfigureerd (offline mock)")

    async def extract_sketch_spec(self, images, trace, filename: str = "", hint: str | None = None) -> PartSpec:
        self.calls.append("extract_sketch")
        for d in self.fixture_dirs:
            p = d / f"{Path(filename).stem}.part_spec.json"
            if p.exists():
                trace.add("extract_sketch", "mock", filename, p.read_text(encoding="utf-8"))
                return post_process(PartSpec.model_validate_json(p.read_text(encoding="utf-8")))
        spec = placeholder_spec("DeepSeek is niet geconfigureerd: vul de schetsmaten handmatig in")
        return spec.model_copy(update={"missing_dimensions": [MissingDimension(
            field_path="stock.thickness_z", prompt_nl="Plaatdikte (Z) in mm?", blocking=True)]})

    async def correct_spec(self, spec, problems, text_digest, images, trace) -> PartSpec:
        self.calls.append("correct")
        raise RuntimeError("mock client cannot correct specs")

    async def critique(self, spec, drawing, ortho_check, text_digest, trace, hints=None) -> CriticReport:
        self.calls.append("critic")
        return CriticReport(verdict=self.critic_verdict)  # type: ignore[arg-type]

    async def generate_complex_code(self, spec, feature, previous_code, error_log, trace) -> str:
        self.calls.append("complex_code")
        if not self.fake_complex_code:
            raise RuntimeError("DeepSeek is not configured: complex_3d features need the code model")
        lo = feature.bbox_min or (0, 0, 0)
        hi = feature.bbox_max or (1, 1, 1)
        size = [max(h - l, 0.1) for l, h in zip(lo, hi)]
        return ("from build123d import *\n"
                f"result = Pos({lo[0]}, {lo[1]}, {lo[2]}) * Box({size[0]}, {size[1]}, {size[2]}, "
                "align=(Align.MIN, Align.MIN, Align.MIN))\n")

    async def ping(self) -> dict[str, Any]:
        return {"reachable": False, "mock": True}

    async def release_files(self, job_dir: Path) -> int:
        return 0


def make_client(settings: Settings) -> LLMClient:
    if settings.llm_enabled:
        return DeepSeekClient(settings)
    from app.config import BACKEND_DIR

    return MockDeepSeekClient([BACKEND_DIR / "tests" / "fixtures"])
