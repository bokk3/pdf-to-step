"""Silent operator-correction telemetry (SPEC_V1_5_CAM_SHOPFLOOR.md §2.6): the v2.5 learning loop's raw data.

The PartSpec the AI produced is snapshotted on the job (``ai_initial_spec_json``). Every operator save
(``PATCH /spec``) and every "mark verified" stores a structured diff against that snapshot in
``spec_corrections``: ``changed_feature_types``, ``field_diffs`` [{path, before, after}] and ``was_modified``.
Deterministic, no LLM. Confidence ticks, source notes, drawing metadata and missing-dimension bookkeeping are not
corrections of the reading and are ignored.
"""

from __future__ import annotations

import json
from typing import Any

AI_SOURCES = {"extraction", "reextract", "self_heal", "critic", "complex_code", "sketch"}
IGNORED = {"confidence", "source_note", "drawing_metadata", "missing_dimensions", "schema_version", "input_source",
           "ambiguities", "code", "requires_complex_3d_fallback"}
NUM_TOL = 1e-6


def _same(a: Any, b: Any) -> bool:
    if isinstance(a, (int, float)) and isinstance(b, (int, float)) and not isinstance(a, bool):
        return abs(float(a) - float(b)) <= NUM_TOL
    return a == b


def _walk(before: Any, after: Any, path: str, out: list[dict]) -> None:
    if isinstance(before, dict) and isinstance(after, dict):
        for k in sorted(set(before) | set(after)):
            if k in IGNORED:
                continue
            _walk(before.get(k), after.get(k), f"{path}.{k}" if path else k, out)
    elif isinstance(before, list) and isinstance(after, list) and all(isinstance(x, (list, tuple, int, float)) for x in before + after):
        if len(before) != len(after) or not all(_same_seq(a, b) for a, b in zip(before, after)):
            out.append({"path": path, "before": before, "after": after})
    elif not _same(before, after):
        out.append({"path": path, "before": before, "after": after})


def _same_seq(a: Any, b: Any) -> bool:
    if isinstance(a, (list, tuple)) and isinstance(b, (list, tuple)):
        return len(a) == len(b) and all(_same(x, y) for x, y in zip(a, b))
    return _same(a, b)


def spec_diff(before: dict, after: dict) -> dict[str, Any]:
    """Structured diff of two PartSpec dicts (features / edge treatments paired by id)."""
    diffs: list[dict] = []
    types: set[str] = set()
    for key, kind in (("features", None), ("edge_treatments", "edge_treatment")):
        b = {f["id"]: f for f in before.get(key, [])}
        a = {f["id"]: f for f in after.get(key, [])}
        for fid in sorted(set(b) | set(a)):
            ftype = kind or (a.get(fid) or b.get(fid) or {}).get("type", "feature")
            if fid not in a:
                diffs.append({"path": f"{key}.{fid}", "before": b[fid].get("type", kind), "after": None, "change": "removed"})
                types.add(ftype)
            elif fid not in b:
                diffs.append({"path": f"{key}.{fid}", "before": None, "after": a[fid].get("type", kind), "change": "added"})
                types.add(ftype)
            else:
                n = len(diffs)
                if kind is None and b[fid].get("type") != a[fid].get("type"):
                    types.add(b[fid].get("type", "feature"))
                _walk(b[fid], a[fid], f"{key}.{fid}", diffs)
                if len(diffs) > n:
                    types.add(ftype)
    for key, label in (("stock", "stock"), ("outer_profile", "outer_profile"), ("datum_origin", "datum"),
                       ("part_name", "metadata"), ("material", "metadata"), ("general_tolerance", "metadata")):
        n = len(diffs)
        _walk(before.get(key), after.get(key), key, diffs)
        if len(diffs) > n:
            types.add(label)
    return {"was_modified": bool(diffs), "changed_feature_types": sorted(types), "field_diffs": diffs}


async def snapshot_ai(store, job_id: str, spec_json: str) -> None:
    """Remember the AI's PartSpec (the latest AI-sourced revision is the baseline for later corrections)."""
    await store.update_job(job_id, ai_initial_spec_json=spec_json)


async def record(store, job_id: str, version: int, action: str, after: dict, fallback_before: dict | None = None) -> dict | None:
    """Diff ``after`` against the AI snapshot and store one ``spec_corrections`` row. Never raises."""
    try:
        job = await store.get_job(job_id)
        base = job.get("ai_initial_spec_json") if job else None
        before = json.loads(base) if base else fallback_before
        if before is None:
            return None  # deterministic intake (DXF / STEP) without an AI reading: nothing to learn from
        diff = spec_diff(before, after)
        await store.add_correction(job_id, version, action, diff, after.get("input_source") or before.get("input_source"))
        return diff
    except Exception:  # noqa: BLE001 - telemetry is silent by design
        return None


async def accuracy(store) -> dict[str, Any]:
    """GET /api/metrics/accuracy: verified jobs, untouched-pass rate, corrections per feature type."""
    rows = await store.list_corrections()
    last_verified: dict[str, dict] = {}
    edits_per_type: dict[str, int] = {}
    edited_jobs: set[str] = set()
    for r in rows:
        if r["action"] == "verified":
            last_verified[r["job_id"]] = r
        elif r["was_modified"]:
            edited_jobs.add(r["job_id"])
            for t in r["changed_feature_types"]:
                edits_per_type[t] = edits_per_type.get(t, 0) + 1
    verified = len(last_verified)
    untouched = sum(1 for r in last_verified.values() if not r["was_modified"])
    fields: dict[str, int] = {}
    for r in last_verified.values():
        for d in r["field_diffs"]:
            leaf = d["path"].split(".")[-1] if d.get("change") is None else d["change"]
            fields[leaf] = fields.get(leaf, 0) + 1
    return {
        "verified_jobs": verified,
        "untouched_verified_jobs": untouched,
        "untouched_pass_rate": round(untouched / verified, 4) if verified else None,
        "edited_jobs": len(edited_jobs),
        "correction_rows": len(rows),
        "corrections_per_feature_type": dict(sorted(edits_per_type.items(), key=lambda kv: -kv[1])),
        "corrected_fields_in_verified_jobs": dict(sorted(fields.items(), key=lambda kv: -kv[1])),
    }
