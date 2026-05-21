"""Deterministic job notebook builder for the Prusa CORE One/+ pack.

The notebook is built from supported surfaces first:

- PrusaLink file metadata when available
- raw G-code text when available through the file download path
- optional external note files dropped beside the notebook cache

This module intentionally does *not* call a model.  The pack can build a
baseline notebook by itself, and external LLM-produced notes can be merged later
without turning the runtime into a second planner.
"""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import re
from typing import Any

from .types import (
    Confidence,
    Family,
    GcodeSnapshotWindow,
    JobMetadata,
    JobNotebook,
    JobSection,
    NotebookBaselines,
    NotebookBuild,
    NotebookNote,
    NoteAnchors,
    NoteProvenance,
    NoteScope,
    PrintableFile,
    ProvenanceSource,
    SectionGcodeFacts,
)

CURRENT_NOTEBOOK_SCHEMA_VERSION = "1.4"
CURRENT_NOTEBOOK_PARSER_VERSION = "job_notebook_v5_gcode_planning_facts"
_NOTEBOOK_TOP_LEVEL_KEYS = {
    "schema_version",
    "notebook_id",
    "printer_family",
    "job",
    "baselines",
    "sections",
    "global_notes",
    "local_notes",
    "build",
}
_NOTEBOOK_JOB_KEYS = {
    "job_hash",
    "file_name",
    "file_path",
    "source_type",
    "material_profile_name",
    "material_family",
    "slicer_name",
    "slicer_profile",
    "generated_at",
}
_NOTEBOOK_BASELINE_KEYS = {
    "speed_pct_default",
    "flow_pct_default",
    "nozzle_target_c_default",
    "bed_target_c_default",
    "pressure_advance_default",
    "print_accel_mm_s2_default",
    "estimated_print_time_s",
}
_NOTEBOOK_SECTION_KEYS = {
    "section_id",
    "label",
    "layer_start",
    "layer_end",
    "z_start_mm",
    "z_end_mm",
    "gcode_line_start",
    "gcode_line_end",
    "progress_start_pct",
    "progress_end_pct",
    "tags",
    "summary",
    "pressure_advance_baseline",
    "print_accel_baseline_mm_s2",
    "gcode_facts",
}
_LEGACY_NOTEBOOK_SECTION_KEYS = _NOTEBOOK_SECTION_KEYS - {"progress_start_pct", "progress_end_pct", "gcode_facts"}
_NOTEBOOK_SECTION_KEYS_WITHOUT_GCODE_FACTS = _NOTEBOOK_SECTION_KEYS - {"gcode_facts"}
_NOTEBOOK_SECTION_KEYS_WITH_GCODE_FACTS_WITHOUT_PROGRESS = _NOTEBOOK_SECTION_KEYS - {
    "progress_start_pct",
    "progress_end_pct",
}
_NOTEBOOK_GCODE_FACT_KEYS = {
    "long_travel_count",
    "long_travel_distance_mm",
    "travel_distance_mm",
    "extrusion_move_distance_mm",
    "travel_to_extrusion_ratio",
    "retraction_count",
    "restart_count",
    "active_m204_p_mm_s2",
    "active_m204_t_mm_s2",
    "active_m204_r_mm_s2",
    "extrusion_cluster_count",
    "extrusion_bbox_mm",
    "snapshot_windows",
}
_NOTEBOOK_SNAPSHOT_WINDOW_KEYS = {
    "window_id",
    "label",
    "gcode_line_start",
    "gcode_line_end",
    "reason_tags",
}
_NOTEBOOK_NOTE_KEYS = {
    "note_id",
    "scope",
    "title",
    "text",
    "confidence",
    "reason_tags",
    "suggested_families",
    "section_ids",
    "anchors",
    "provenance",
}
_NOTEBOOK_ANCHOR_KEYS = {"layer_range", "z_range_mm", "gcode_line_range"}
_NOTEBOOK_PROVENANCE_KEYS = {"source", "evidence"}
_NOTEBOOK_BUILD_KEYS = {"parser_version", "notebook_builder", "built_at"}


_LAYER_CHANGE_RE = re.compile(r"^;\s*LAYER_CHANGE", re.IGNORECASE)
_LAYER_NUMBER_RE = re.compile(r"^;\s*LAYER:\s*(-?\d+)", re.IGNORECASE)
_LAYER_MARKERS = (_LAYER_CHANGE_RE, _LAYER_NUMBER_RE)
_Z_COMMENT_RE = re.compile(r"^;\s*Z:\s*(-?\d+(?:\.\d+)?)", re.IGNORECASE)
_KEY_VALUE_RE = re.compile(r"^;\s*([A-Za-z0-9_\-\[\] ]+?)\s*=\s*(.+)$")
_TYPE_RE = re.compile(r"^;\s*TYPE:\s*(.+)$", re.IGNORECASE)
_M104_RE = re.compile(r"\bM104\b[^;\r\n]*\bS(-?\d+(?:\.\d+)?)", re.IGNORECASE)
_M109_RE = re.compile(r"\bM109\b[^;\r\n]*\bS(-?\d+(?:\.\d+)?)", re.IGNORECASE)
_M140_RE = re.compile(r"\bM140\b[^;\r\n]*\bS(-?\d+(?:\.\d+)?)", re.IGNORECASE)
_M190_RE = re.compile(r"\bM190\b[^;\r\n]*\bS(-?\d+(?:\.\d+)?)", re.IGNORECASE)
_M572_RE = re.compile(r"\bM572\b[^;\r\n]*\bS(-?\d+(?:\.\d+)?)", re.IGNORECASE)
_M204_S_RE = re.compile(r"\bM204\b[^;\r\n]*\bS(-?\d+(?:\.\d+)?)", re.IGNORECASE)
_M204_P_RE = re.compile(r"\bM204\b[^;\r\n]*\bP(-?\d+(?:\.\d+)?)", re.IGNORECASE)
_M204_T_RE = re.compile(r"\bM204\b[^;\r\n]*\bT(-?\d+(?:\.\d+)?)", re.IGNORECASE)
_M204_R_RE = re.compile(r"\bM204\b[^;\r\n]*\bR(-?\d+(?:\.\d+)?)", re.IGNORECASE)
_M204_BARE_RE = re.compile(r"\bM204\b\s+(-?\d+(?:\.\d+)?)", re.IGNORECASE)
_PRINT_TIME_RE = re.compile(r"(?:(\d+(?:\.\d+)?)\s*h)?\s*(?:(\d+(?:\.\d+)?)\s*m)?\s*(?:(\d+(?:\.\d+)?)\s*s)?", re.IGNORECASE)
_GCODE_WORD_RE = re.compile(r"([A-Za-z])\s*(-?\d+(?:\.\d+)?)")
_LONG_TRAVEL_THRESHOLD_MM = 10.0
_SNAPSHOT_WINDOW_RADIUS_LINES = 2


def build_job_notebook(
    *,
    printable: PrintableFile | None,
    file_info: dict[str, Any] | None,
    file_text: str | None,
    notes_dir: Path | None = None,
    lookahead_pct: float | None = None,
) -> JobNotebook | None:
    """Return a grounded notebook for *printable* or ``None`` if no file is known."""
    if printable is None and not file_info and not file_text:
        return None

    display_name = printable.display_name if printable else _string_or_none((file_info or {}).get("display_name")) or "unknown"
    source_path = printable.path if printable else _string_or_none((file_info or {}).get("path"))
    metadata = _extract_metadata(file_info or {})
    comment_kv = _parse_comment_key_values(file_text) if file_text else {}
    built_at = _utc_now_iso()

    material = _coalesce(
        _metadata_string(metadata, "filament_type"),
        _metadata_string(metadata, "material"),
        _comment_value(comment_kv, "filament_type"),
        _comment_value(comment_kv, "filament_settings_id"),
    )
    layer_height_mm = _coalesce_float(
        _metadata_number(metadata, "layer_height"),
        _comment_value(comment_kv, "layer_height"),
    )
    nozzle_target_c = _coalesce_int(
        _metadata_number(metadata, "extruder_temperature"),
        _metadata_number(metadata, "nozzle_temperature"),
        _first_gcode_number(file_text, _M109_RE),
        _first_gcode_number(file_text, _M104_RE),
    )
    bed_target_c = _coalesce_int(
        _metadata_number(metadata, "bed_temperature"),
        _first_gcode_number(file_text, _M190_RE),
        _first_gcode_number(file_text, _M140_RE),
    )
    pressure_advance = _coalesce_float(
        _first_gcode_number(file_text, _M572_RE),
    )
    print_accel_mm_s2 = _coalesce_float(
        _first_gcode_number(file_text, _M204_S_RE),
        _first_gcode_number(file_text, _M204_P_RE),
        _first_gcode_number(file_text, _M204_BARE_RE),
    )
    estimated_print_time = _comment_value(comment_kv, "estimated_printing_time") or _comment_value(
        comment_kv, "estimated_printing_time_(normal_mode)"
    )
    estimated_print_time_s = _parse_print_time_s(estimated_print_time)
    slicer_name = _coalesce(
        _metadata_string(metadata, "slicer"),
        _detect_slicer_name(file_text),
    )
    slicer_profile = _coalesce(
        _metadata_string(metadata, "print_settings_id"),
        _comment_value(comment_kv, "print_settings_id"),
        _comment_value(comment_kv, "printer_settings_id"),
    )
    generated_at = _generated_at_iso(printable=printable, file_info=file_info, fallback=built_at)

    sections = tuple(_build_sections(file_text)) if file_text else ()
    global_notes = _build_global_notes(
        material=material,
        layer_height_mm=layer_height_mm,
        nozzle_target_c=nozzle_target_c,
        bed_target_c=bed_target_c,
        sections=sections,
        comment_kv=comment_kv,
    )
    local_notes = _build_local_notes(sections)

    job_hash = _job_hash(printable=printable, file_info=file_info, file_text=file_text)
    notebook = JobNotebook(
        schema_version=CURRENT_NOTEBOOK_SCHEMA_VERSION,
        notebook_id=job_hash,
        printer_family="prusa_core_one_plus",
        job=JobMetadata(
            job_hash=job_hash,
            file_name=display_name,
            file_path=source_path,
            source_type=_source_type(display_name, source_path),
            material_profile_name=material,
            material_family=_material_family(material),
            slicer_name=slicer_name,
            slicer_profile=slicer_profile,
            generated_at=generated_at,
        ),
        baselines=NotebookBaselines(
            speed_pct_default=100.0,
            flow_pct_default=100.0,
            nozzle_target_c_default=float(nozzle_target_c) if nozzle_target_c is not None else None,
            bed_target_c_default=float(bed_target_c) if bed_target_c is not None else None,
            pressure_advance_default=pressure_advance,
            print_accel_mm_s2_default=print_accel_mm_s2,
            estimated_print_time_s=estimated_print_time_s,
        ),
        sections=sections,
        global_notes=tuple(global_notes),
        local_notes=tuple(local_notes),
        build=NotebookBuild(
            parser_version=CURRENT_NOTEBOOK_PARSER_VERSION,
            notebook_builder="wallee_v6.packs.prusa_core_one_plus.job_notebook",
            built_at=built_at,
        ),
        layer_height_mm_internal=layer_height_mm,
        source_text_grounded_internal=bool(file_text),
    )
    if notes_dir is not None:
        notebook = merge_external_notes(notebook, notes_dir)
    return notebook


def load_persisted_notebook(path: Path) -> JobNotebook:
    """Load and strictly validate one persisted notebook artifact."""
    payload = json.loads(path.read_text(encoding="utf-8"))
    return notebook_from_jsonable(payload)


def persist_job_notebook(path: Path, notebook: JobNotebook) -> None:
    """Persist *notebook* in the current schema."""
    path.write_text(
        json.dumps(notebook_to_jsonable(notebook), indent=2, sort_keys=True),
        encoding="utf-8",
    )


def merge_external_notes(notebook: JobNotebook, notes_dir: Path) -> JobNotebook:
    """Merge optional external note files from *notes_dir*.

    External notes are how a separate LLM notebook tool can add richer grounded
    annotations without making the runtime itself responsible for model calls.
    """
    path = notes_dir / f"{notebook.job_hash}.notes.json"
    if not path.exists():
        return notebook

    payload = json.loads(path.read_text(encoding="utf-8"))
    extra_global: list[NotebookNote] = []
    extra_local: list[NotebookNote] = []
    valid_section_ids = {section.section_id for section in notebook.sections}

    for raw_note in payload.get("global_notes", []):
        note = _note_from_json(raw_note)
        if note is not None:
            extra_global.append(note)

    for raw_note in payload.get("local_notes", []):
        note = _note_from_json(raw_note)
        if note is None:
            continue
        if not set(note.section_ids).issubset(valid_section_ids):
            continue
        extra_local.append(note)

    return JobNotebook(
        schema_version=notebook.schema_version,
        notebook_id=notebook.notebook_id,
        printer_family=notebook.printer_family,
        job=notebook.job,
        baselines=notebook.baselines,
        sections=notebook.sections,
        global_notes=tuple(list(notebook.global_notes) + extra_global),
        local_notes=tuple(list(notebook.local_notes) + extra_local),
        build=notebook.build,
        layer_height_mm_internal=notebook.layer_height_mm,
        source_text_grounded_internal=notebook.source_text_grounded_internal,
    )


def notebook_to_jsonable(notebook: JobNotebook) -> dict[str, Any]:
    """Return a JSON-serializable notebook payload."""
    return {
        "schema_version": notebook.schema_version,
        "notebook_id": notebook.notebook_id,
        "printer_family": notebook.printer_family,
        "job": {
            "job_hash": notebook.job.job_hash,
            "file_name": notebook.job.file_name,
            "file_path": notebook.job.file_path,
            "source_type": notebook.job.source_type,
            "material_profile_name": notebook.job.material_profile_name,
            "material_family": notebook.job.material_family,
            "slicer_name": notebook.job.slicer_name,
            "slicer_profile": notebook.job.slicer_profile,
            "generated_at": notebook.job.generated_at,
        },
        "baselines": {
            "speed_pct_default": notebook.baselines.speed_pct_default,
            "flow_pct_default": notebook.baselines.flow_pct_default,
            "nozzle_target_c_default": notebook.baselines.nozzle_target_c_default,
            "bed_target_c_default": notebook.baselines.bed_target_c_default,
            "pressure_advance_default": notebook.baselines.pressure_advance_default,
            "print_accel_mm_s2_default": notebook.baselines.print_accel_mm_s2_default,
            "estimated_print_time_s": notebook.baselines.estimated_print_time_s,
        },
        "sections": [_section_to_jsonable(section) for section in notebook.sections],
        "global_notes": [_note_to_jsonable(note) for note in notebook.global_notes],
        "local_notes": [_note_to_jsonable(note) for note in notebook.local_notes],
        "build": {
            "parser_version": notebook.build.parser_version,
            "notebook_builder": notebook.build.notebook_builder,
            "built_at": notebook.build.built_at,
        },
    }


def notebook_from_jsonable(payload: dict[str, Any]) -> JobNotebook:
    """Return a strictly validated notebook from persisted JSON."""
    _require_exact_keys(payload, _NOTEBOOK_TOP_LEVEL_KEYS, "notebook")
    if payload.get("schema_version") not in {"1.0", "1.1", "1.2", "1.3", CURRENT_NOTEBOOK_SCHEMA_VERSION}:
        raise ValueError(f"unsupported notebook schema_version: {payload.get('schema_version')!r}")
    if payload.get("printer_family") != "prusa_core_one_plus":
        raise ValueError(f"unsupported printer_family: {payload.get('printer_family')!r}")

    job_payload = payload.get("job")
    baselines_payload = payload.get("baselines")
    sections_payload = payload.get("sections")
    global_notes_payload = payload.get("global_notes")
    local_notes_payload = payload.get("local_notes")
    build_payload = payload.get("build")
    if not isinstance(job_payload, dict):
        raise ValueError("notebook.job must be an object")
    if not isinstance(baselines_payload, dict):
        raise ValueError("notebook.baselines must be an object")
    if not isinstance(sections_payload, list):
        raise ValueError("notebook.sections must be a list")
    if not isinstance(global_notes_payload, list):
        raise ValueError("notebook.global_notes must be a list")
    if not isinstance(local_notes_payload, list):
        raise ValueError("notebook.local_notes must be a list")
    if not isinstance(build_payload, dict):
        raise ValueError("notebook.build must be an object")

    _require_exact_keys(job_payload, _NOTEBOOK_JOB_KEYS, "notebook.job")
    allowed_baseline_keys = set(_NOTEBOOK_BASELINE_KEYS)
    legacy_baseline_keys = allowed_baseline_keys - {
        "pressure_advance_default",
        "print_accel_mm_s2_default",
        "estimated_print_time_s",
    }
    active_baseline_legacy_keys = allowed_baseline_keys - {"estimated_print_time_s"}
    baseline_keys = set(baselines_payload.keys())
    if (
        baseline_keys != allowed_baseline_keys
        and baseline_keys != active_baseline_legacy_keys
        and baseline_keys != legacy_baseline_keys
    ):
        raise ValueError(
            f"notebook.baselines keys mismatch expected one of {sorted(legacy_baseline_keys)!r}, "
            f"{sorted(active_baseline_legacy_keys)!r}, or {sorted(allowed_baseline_keys)!r} "
            f"actual={sorted(baseline_keys)!r}"
        )
    _require_exact_keys(build_payload, _NOTEBOOK_BUILD_KEYS, "notebook.build")

    notebook = JobNotebook(
        schema_version=str(payload["schema_version"]),
        notebook_id=str(payload["notebook_id"]),
        printer_family=str(payload["printer_family"]),
        job=JobMetadata(
            job_hash=str(job_payload["job_hash"]),
            file_name=str(job_payload["file_name"]),
            file_path=_string_or_none(job_payload.get("file_path")),
            source_type=str(job_payload["source_type"]),
            material_profile_name=_string_or_none(job_payload.get("material_profile_name")),
            material_family=_string_or_none(job_payload.get("material_family")),
            slicer_name=_string_or_none(job_payload.get("slicer_name")),
            slicer_profile=_string_or_none(job_payload.get("slicer_profile")),
            generated_at=str(job_payload["generated_at"]),
        ),
        baselines=NotebookBaselines(
            speed_pct_default=_float_or_none(baselines_payload.get("speed_pct_default")),
            flow_pct_default=_float_or_none(baselines_payload.get("flow_pct_default")),
            nozzle_target_c_default=_float_or_none(baselines_payload.get("nozzle_target_c_default")),
            bed_target_c_default=_float_or_none(baselines_payload.get("bed_target_c_default")),
            pressure_advance_default=_float_or_none(baselines_payload.get("pressure_advance_default")),
            print_accel_mm_s2_default=_float_or_none(baselines_payload.get("print_accel_mm_s2_default")),
            estimated_print_time_s=_float_or_none(baselines_payload.get("estimated_print_time_s")),
        ),
        sections=tuple(_section_from_jsonable(item) for item in sections_payload),
        global_notes=tuple(_note_from_jsonable_strict(item, scope=NoteScope.GLOBAL) for item in global_notes_payload),
        local_notes=tuple(_note_from_jsonable_strict(item, scope=NoteScope.LOCAL) for item in local_notes_payload),
        build=NotebookBuild(
            parser_version=str(build_payload["parser_version"]),
            notebook_builder=str(build_payload["notebook_builder"]),
            built_at=str(build_payload["built_at"]),
        ),
        source_text_grounded_internal=str(build_payload["parser_version"]) == CURRENT_NOTEBOOK_PARSER_VERSION,
    )
    if notebook.job.job_hash != notebook.notebook_id:
        raise ValueError("notebook_id must match job.job_hash")
    return notebook


def active_notes_to_jsonable(active) -> dict[str, Any]:
    return {
        "schema_version": active.schema_version,
        "job_hash": active.job_hash,
        "selected_at": active.selected_at,
        "selection_context": {
            "lifecycle": active.selection_context.lifecycle,
            "job_active": active.selection_context.job_active,
            "job_progress_pct": active.selection_context.job_progress_pct,
            "current_layer": active.selection_context.current_layer,
            "current_z_mm": active.selection_context.current_z_mm,
            "current_section_ids": list(active.selection_context.current_section_ids),
            "lookahead_section_ids": list(active.selection_context.lookahead_section_ids),
        },
        "planner_facts": _active_planner_facts_to_jsonable(active.planner_facts),
        "global_notes": [_active_note_view_to_jsonable(note) for note in active.global_notes],
        "local_notes": [_active_note_view_to_jsonable(note) for note in active.local_notes],
        "merged_reason_tags": list(active.merged_reason_tags),
        "family_hints": {key: list(value) for key, value in active.family_hints.items()},
    }


def _job_hash(*, printable: PrintableFile | None, file_info: dict[str, Any] | None, file_text: str | None) -> str:
    digest = hashlib.sha256()
    digest.update(
        json.dumps(
            _job_hash_inputs(printable=printable, file_info=file_info, file_text=file_text),
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    )
    return digest.hexdigest()[:16]


def _job_hash_inputs(*, printable: PrintableFile | None, file_info: dict[str, Any] | None, file_text: str | None) -> dict[str, Any]:
    display_name = printable.display_name if printable else _string_or_none((file_info or {}).get("display_name")) or "unknown"
    source_path = printable.path if printable else _string_or_none((file_info or {}).get("path"))
    size_bytes = None
    if printable is not None and printable.size_bytes is not None:
        size_bytes = int(printable.size_bytes)
    elif isinstance(file_info, dict) and isinstance(file_info.get("size"), (int, float)):
        size_bytes = int(file_info["size"])
    normalized_path = None
    if source_path:
        normalized_path = "/" + str(source_path).strip().lstrip("/")
    return {
        "file_name": display_name,
        "file_path": normalized_path,
        "source_type": _source_type(display_name, source_path),
        "size_bytes": size_bytes,
    }


def _parse_comment_key_values(file_text: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw_line in file_text.splitlines():
        match = _KEY_VALUE_RE.match(raw_line.strip())
        if not match:
            continue
        key = match.group(1).strip().lower().replace(" ", "_")
        values[key] = match.group(2).strip()
    return values


def _build_sections(file_text: str) -> list[JobSection]:
    lines = file_text.splitlines()
    if not lines:
        return []

    sections: list[JobSection] = []
    total_lines = max(1, len(lines))
    current_start = 0
    current_z: float | None = None
    current_layer: int | None = None
    current_tags: set[str] = set()
    current_pressure_advance: float | None = None
    current_print_accel_mm_s2: float | None = None
    section_pressure_advance: float | None = None
    section_print_accel_mm_s2: float | None = None
    seen_layer_marker = False
    inferred_layer_index = -1
    use_number_markers = any(_LAYER_NUMBER_RE.match(line.strip()) for line in lines)

    def flush(line_end: int) -> None:
        nonlocal current_start, current_tags, current_z, current_layer
        if line_end < current_start:
            return
        section_index = len(sections) + 1
        tags = tuple(sorted(current_tags))
        summary = _section_summary(tags=tags, z=current_z, layer=current_layer)
        sections.append(
            JobSection(
                section_id=f"sec_{section_index:04d}",
                label=summary,
                layer_start=current_layer,
                layer_end=current_layer,
                z_start_mm=current_z,
                z_end_mm=current_z,
                gcode_line_start=current_start,
                gcode_line_end=line_end,
                tags=tags,
                summary=summary,
                progress_start_pct=round((current_start / total_lines) * 100.0, 2),
                progress_end_pct=round(((line_end + 1) / total_lines) * 100.0, 2),
                pressure_advance_baseline=section_pressure_advance,
                print_accel_baseline_mm_s2=section_print_accel_mm_s2,
            )
        )
        current_start = line_end + 1
        current_tags = set()

    for index, raw_line in enumerate(lines):
        line = raw_line.strip()
        if not line:
            continue

        pressure_advance = _gcode_number_in_line(line, _M572_RE)
        if pressure_advance is not None:
            current_pressure_advance = pressure_advance
        print_accel_mm_s2 = _coalesce_float(
            _gcode_number_in_line(line, _M204_S_RE),
            _gcode_number_in_line(line, _M204_P_RE),
            _gcode_number_in_line(line, _M204_BARE_RE),
        )
        if print_accel_mm_s2 is not None:
            current_print_accel_mm_s2 = print_accel_mm_s2

        z_match = _Z_COMMENT_RE.match(line)
        if z_match:
            try:
                current_z = float(z_match.group(1))
            except ValueError:
                pass

        type_match = _TYPE_RE.match(line)
        if type_match:
            current_tags.update(_tags_from_type(type_match.group(1)))

        layer_number_match = _LAYER_NUMBER_RE.match(line)
        layer_change_match = _LAYER_CHANGE_RE.match(line)
        is_layer_boundary = bool(layer_number_match) if use_number_markers else bool(layer_change_match)
        if is_layer_boundary:
            if seen_layer_marker:
                flush(index - 1)
            seen_layer_marker = True
            if layer_number_match:
                try:
                    current_layer = int(layer_number_match.group(1))
                    inferred_layer_index = current_layer
                except ValueError:
                    inferred_layer_index += 1
                    current_layer = inferred_layer_index
            else:
                inferred_layer_index += 1
                current_layer = inferred_layer_index
            section_pressure_advance = current_pressure_advance
            section_print_accel_mm_s2 = current_print_accel_mm_s2
            continue

    flush(total_lines - 1)

    if not sections:
        sections.append(
            JobSection(
                section_id="sec_0001",
                label="Whole-job section",
                layer_start=current_layer,
                layer_end=current_layer,
                z_start_mm=None,
                z_end_mm=None,
                gcode_line_start=0,
                gcode_line_end=total_lines - 1,
                tags=(),
                summary="Whole-job section",
                progress_start_pct=0.0,
                progress_end_pct=100.0,
                pressure_advance_baseline=current_pressure_advance,
                print_accel_baseline_mm_s2=current_print_accel_mm_s2,
            )
        )

    # Mark very short sections after the fact.
    for section in sections:
        line_span = section.line_end - section.line_start + 1
        if line_span < 40 and "tiny_layer" not in section.tags:
            section.tags = tuple(sorted(set(section.tags).union({"tiny_layer"})))
            section.summary = _section_summary(tags=section.tags, z=section.z_min_mm, layer=section.layer_start)
    _annotate_sections_with_gcode_facts(lines, sections)
    return sections


def _annotate_sections_with_gcode_facts(lines: list[str], sections: list[JobSection]) -> None:
    if not lines or not sections:
        return

    state: dict[str, Any] = {
        "x": None,
        "y": None,
        "z": None,
        "e": 0.0,
        "absolute_xyz": True,
        "absolute_e": True,
        "in_retraction": False,
        "m204_p": None,
        "m204_t": None,
        "m204_r": None,
    }
    for section in sections:
        metrics: dict[str, Any] = {
            "long_travel_count": 0,
            "long_travel_distance_mm": 0.0,
            "travel_distance_mm": 0.0,
            "extrusion_move_distance_mm": 0.0,
            "retraction_count": 0,
            "restart_count": 0,
            "snapshot_windows": [],
            "extrusion_points": [],
        }
        start = max(0, section.gcode_line_start or 0)
        end = min(len(lines) - 1, section.gcode_line_end if section.gcode_line_end is not None else len(lines) - 1)
        for line_index in range(start, end + 1):
            _observe_gcode_line_for_facts(lines[line_index], line_index, len(lines), state, metrics)

        points = list(metrics["extrusion_points"])
        extrusion_distance = float(metrics["extrusion_move_distance_mm"])
        travel_distance = float(metrics["travel_distance_mm"])
        ratio = round(travel_distance / extrusion_distance, 3) if extrusion_distance > 0 else None
        section.gcode_facts = SectionGcodeFacts(
            long_travel_count=int(metrics["long_travel_count"]),
            long_travel_distance_mm=round(float(metrics["long_travel_distance_mm"]), 3),
            travel_distance_mm=round(travel_distance, 3),
            extrusion_move_distance_mm=round(extrusion_distance, 3),
            travel_to_extrusion_ratio=ratio,
            retraction_count=int(metrics["retraction_count"]),
            restart_count=int(metrics["restart_count"]),
            active_m204_p_mm_s2=_round_or_none(state.get("m204_p")),
            active_m204_t_mm_s2=_round_or_none(state.get("m204_t")),
            active_m204_r_mm_s2=_round_or_none(state.get("m204_r")),
            extrusion_cluster_count=_extrusion_cluster_count(points),
            extrusion_bbox_mm=_extrusion_bbox(points),
            snapshot_windows=tuple(metrics["snapshot_windows"][:6]),
        )

    _annotate_repeated_tower_geometry(sections)


def _observe_gcode_line_for_facts(
    raw_line: str,
    line_index: int,
    total_lines: int,
    state: dict[str, Any],
    metrics: dict[str, Any],
) -> None:
    code = raw_line.split(";", 1)[0].strip()
    if not code:
        return
    upper = code.upper()
    command = _gcode_command(upper)
    words = _gcode_words(upper)

    if command == "G90":
        state["absolute_xyz"] = True
        return
    if command == "G91":
        state["absolute_xyz"] = False
        return
    if command == "M82":
        state["absolute_e"] = True
        return
    if command == "M83":
        state["absolute_e"] = False
        return
    if command == "G92":
        if "E" in words:
            state["e"] = words["E"]
        if "X" in words:
            state["x"] = words["X"]
        if "Y" in words:
            state["y"] = words["Y"]
        if "Z" in words:
            state["z"] = words["Z"]
        return
    if command == "M204":
        if not {"P", "T", "R", "S"}.intersection(words):
            bare_m204 = _gcode_number_in_line(upper, _M204_BARE_RE)
            if bare_m204 is not None:
                words["S"] = bare_m204
        _update_m204_state(words, state)
        _add_snapshot_window(
            metrics,
            line_index,
            total_lines,
            label=_m204_window_label(state),
            reason_tags=("m204", "accel"),
        )
        return
    if command not in {"G0", "G00", "G1", "G01"}:
        return

    old_x = _float_or_none(state.get("x"))
    old_y = _float_or_none(state.get("y"))
    target_x = _target_axis_value(words, "X", old_x, bool(state["absolute_xyz"]))
    target_y = _target_axis_value(words, "Y", old_y, bool(state["absolute_xyz"]))
    target_z = _target_axis_value(words, "Z", _float_or_none(state.get("z")), bool(state["absolute_xyz"]))
    e_delta = _e_delta(words, _float_or_none(state.get("e")) or 0.0, bool(state["absolute_e"]))
    xy_distance = _xy_distance(old_x, old_y, target_x, target_y)

    if e_delta < -0.0001:
        metrics["retraction_count"] += 1
        state["in_retraction"] = True
        _add_snapshot_window(
            metrics,
            line_index,
            total_lines,
            label="Retraction move",
            reason_tags=("retraction",),
        )
    elif e_delta > 0.0001:
        if state.get("in_retraction"):
            metrics["restart_count"] += 1
            state["in_retraction"] = False
            _add_snapshot_window(
                metrics,
                line_index,
                total_lines,
                label="Restart after retraction",
                reason_tags=("restart",),
            )
        if xy_distance > 0:
            metrics["extrusion_move_distance_mm"] += xy_distance
            if target_x is not None and target_y is not None:
                metrics["extrusion_points"].append((target_x, target_y))
    elif xy_distance > 0:
        metrics["travel_distance_mm"] += xy_distance
        if xy_distance >= _LONG_TRAVEL_THRESHOLD_MM:
            metrics["long_travel_count"] += 1
            metrics["long_travel_distance_mm"] += xy_distance
            _add_snapshot_window(
                metrics,
                line_index,
                total_lines,
                label=f"Long travel {xy_distance:.1f} mm",
                reason_tags=("long_travel",),
            )

    if target_x is not None:
        state["x"] = target_x
    if target_y is not None:
        state["y"] = target_y
    if target_z is not None:
        state["z"] = target_z
    if "E" in words:
        state["e"] = words["E"] if state["absolute_e"] else (_float_or_none(state.get("e")) or 0.0) + words["E"]


def _annotate_repeated_tower_geometry(sections: list[JobSection]) -> None:
    candidates: list[JobSection] = []
    for section in sections:
        facts = section.gcode_facts
        bbox = facts.extrusion_bbox_mm
        if bbox is None:
            continue
        bbox_diagonal = math.hypot(bbox[2] - bbox[0], bbox[3] - bbox[1])
        if (
            facts.extrusion_cluster_count >= 2
            and facts.long_travel_count >= 1
            and facts.retraction_count >= 1
            and bbox_diagonal <= 120.0
        ):
            candidates.append(section)
    if len(candidates) < 3:
        return

    for section in candidates:
        section.tags = tuple(sorted(set(section.tags).union({"repeated_tower", "stringing_test_geometry"})))
        section.summary = _section_summary(tags=section.tags, z=section.z_min_mm, layer=section.layer_start)


def _gcode_command(line: str) -> str | None:
    match = re.match(r"^\s*([GM])\s*(\d+)", line, re.IGNORECASE)
    if not match:
        return None
    return f"{match.group(1).upper()}{int(match.group(2))}"


def _gcode_words(line: str) -> dict[str, float]:
    words: dict[str, float] = {}
    for letter, raw_value in _GCODE_WORD_RE.findall(line):
        try:
            words[letter.upper()] = float(raw_value)
        except ValueError:
            continue
    return words


def _update_m204_state(words: dict[str, float], state: dict[str, Any]) -> None:
    if "P" in words:
        state["m204_p"] = words["P"]
    if "T" in words:
        state["m204_t"] = words["T"]
    if "R" in words:
        state["m204_r"] = words["R"]
    if "S" in words and not {"P", "T", "R"}.intersection(words):
        state["m204_p"] = words["S"]
        state["m204_t"] = words["S"]


def _m204_window_label(state: dict[str, Any]) -> str:
    pieces = []
    for key, label in (("m204_p", "P"), ("m204_t", "T"), ("m204_r", "R")):
        value = state.get(key)
        if value is not None:
            pieces.append(f"{label}{float(value):.0f}")
    return "M204 active " + " ".join(pieces) if pieces else "M204 command"


def _target_axis_value(words: dict[str, float], key: str, current: float | None, absolute: bool) -> float | None:
    if key not in words:
        return current
    if absolute:
        return words[key]
    return (current or 0.0) + words[key]


def _e_delta(words: dict[str, float], current_e: float, absolute_e: bool) -> float:
    if "E" not in words:
        return 0.0
    return words["E"] - current_e if absolute_e else words["E"]


def _xy_distance(old_x: float | None, old_y: float | None, target_x: float | None, target_y: float | None) -> float:
    if old_x is None or old_y is None or target_x is None or target_y is None:
        return 0.0
    return math.hypot(target_x - old_x, target_y - old_y)


def _add_snapshot_window(
    metrics: dict[str, Any],
    line_index: int,
    total_lines: int,
    *,
    label: str,
    reason_tags: tuple[str, ...],
) -> None:
    windows: list[GcodeSnapshotWindow] = metrics["snapshot_windows"]
    if len(windows) >= 6:
        return
    start = max(0, line_index - _SNAPSHOT_WINDOW_RADIUS_LINES)
    end = min(total_lines - 1, line_index + _SNAPSHOT_WINDOW_RADIUS_LINES)
    token = "_".join(reason_tags)
    windows.append(
        GcodeSnapshotWindow(
            window_id=f"gcode_window_{line_index:06d}_{token}",
            label=label,
            gcode_line_start=start,
            gcode_line_end=end,
            reason_tags=reason_tags,
        )
    )


def _extrusion_bbox(points: list[tuple[float, float]]) -> tuple[float, float, float, float] | None:
    if not points:
        return None
    xs = [point[0] for point in points]
    ys = [point[1] for point in points]
    return (round(min(xs), 3), round(min(ys), 3), round(max(xs), 3), round(max(ys), 3))


def _extrusion_cluster_count(points: list[tuple[float, float]], *, threshold_mm: float = 12.0) -> int:
    clusters: list[tuple[float, float, int]] = []
    for x, y in points:
        selected_index = None
        selected_distance = None
        for index, (center_x, center_y, _count) in enumerate(clusters):
            distance = math.hypot(x - center_x, y - center_y)
            if distance <= threshold_mm and (selected_distance is None or distance < selected_distance):
                selected_index = index
                selected_distance = distance
        if selected_index is None:
            clusters.append((x, y, 1))
            continue
        center_x, center_y, count = clusters[selected_index]
        next_count = count + 1
        clusters[selected_index] = (
            center_x + ((x - center_x) / next_count),
            center_y + ((y - center_y) / next_count),
            next_count,
        )
    return len(clusters)


def _round_or_none(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return round(float(value), 3)
    except (TypeError, ValueError):
        return None


def _build_global_notes(
    *,
    material: str | None,
    layer_height_mm: float | None,
    nozzle_target_c: int | None,
    bed_target_c: int | None,
    sections: tuple[JobSection, ...],
    comment_kv: dict[str, str],
) -> list[NotebookNote]:
    notes: list[NotebookNote] = []

    if material:
        notes.append(
            NotebookNote(
                note_id="global_material",
                scope=NoteScope.GLOBAL,
                title="Material profile",
                text=f"Material profile: {material}.",
                confidence=Confidence.HIGH,
                reason_tags=("material",),
                provenance=NoteProvenance(source=ProvenanceSource.PARSER, evidence=("metadata_or_comments",)),
                priority=10,
            )
        )
    if layer_height_mm is not None:
        notes.append(
            NotebookNote(
                note_id="global_layer_height",
                scope=NoteScope.GLOBAL,
                title="Layer height",
                text=f"Layer height: {layer_height_mm:.3f} mm.",
                confidence=Confidence.HIGH,
                reason_tags=("layer_height",),
                provenance=NoteProvenance(source=ProvenanceSource.PARSER, evidence=("metadata_or_comments",)),
                priority=11,
            )
        )
    if nozzle_target_c is not None or bed_target_c is not None:
        pieces = []
        if nozzle_target_c is not None:
            pieces.append(f"nozzle target {nozzle_target_c}C")
        if bed_target_c is not None:
            pieces.append(f"bed target {bed_target_c}C")
        notes.append(
            NotebookNote(
                note_id="global_targets",
                scope=NoteScope.GLOBAL,
                title="Slicer targets",
                text=f"Slicer targets: {', '.join(pieces)}.",
                confidence=Confidence.HIGH,
                reason_tags=("targets",),
                suggested_families=tuple(
                    family
                    for family, present in (
                        (Family.NOZZLE, nozzle_target_c is not None),
                        (Family.BED, bed_target_c is not None),
                    )
                    if present
                ),
                provenance=NoteProvenance(source=ProvenanceSource.PARSER, evidence=("metadata_or_gcode",)),
                priority=12,
            )
        )

    tag_counts: dict[str, int] = {}
    for section in sections:
        for tag in section.tags:
            tag_counts[tag] = tag_counts.get(tag, 0) + 1

    if tag_counts.get("bridge", 0):
        notes.append(
            NotebookNote(
                note_id="global_bridge_watch",
                scope=NoteScope.GLOBAL,
                title="Bridge watch",
                text="Bridge sections exist. Prefer speed reduction before broader temperature changes if bridging starts to sag.",
                confidence=Confidence.HIGH,
                reason_tags=("bridge",),
                suggested_families=(Family.SPEED, Family.FLOW, Family.NOZZLE),
                provenance=NoteProvenance(
                    source=ProvenanceSource.PARSER,
                    evidence=(f"tag=bridge count={tag_counts['bridge']}",),
                ),
                priority=20,
            )
        )
    if tag_counts.get("high_flow", 0):
        notes.append(
            NotebookNote(
                note_id="global_flow_watch",
                scope=NoteScope.GLOBAL,
                title="Flow watch",
                text="High-flow sections exist. Watch extrusion consistency before changing multiple controls at once.",
                confidence=Confidence.MEDIUM,
                reason_tags=("high_flow",),
                suggested_families=(Family.FLOW, Family.SPEED),
                provenance=NoteProvenance(
                    source=ProvenanceSource.PARSER,
                    evidence=(f"tag=high_flow count={tag_counts['high_flow']}",),
                ),
                priority=21,
            )
        )
    if tag_counts.get("tiny_layer", 0):
        notes.append(
            NotebookNote(
                note_id="global_tiny_layers",
                scope=NoteScope.GLOBAL,
                title="Short layers",
                text="Very short sections exist. Heat accumulation can be local even when whole-job temperatures look normal.",
                confidence=Confidence.MEDIUM,
                reason_tags=("tiny_layer",),
                suggested_families=(Family.SPEED, Family.NOZZLE),
                provenance=NoteProvenance(
                    source=ProvenanceSource.PARSER,
                    evidence=(f"tag=tiny_layer count={tag_counts['tiny_layer']}",),
                ),
                priority=22,
            )
        )
    if tag_counts.get("stringing_test_geometry", 0):
        notes.append(
            NotebookNote(
                note_id="global_stringing_geometry",
                scope=NoteScope.GLOBAL,
                title="Repeated tower geometry",
                text="G-code geometry resembles a repeated-tower/stringing-test pattern with anchored long travels and retractions.",
                confidence=Confidence.HIGH,
                reason_tags=("repeated_tower", "stringing_test_geometry", "long_travel", "retraction"),
                provenance=NoteProvenance(
                    source=ProvenanceSource.PARSER,
                    evidence=(f"tag=stringing_test_geometry count={tag_counts['stringing_test_geometry']}",),
                ),
                priority=14,
            )
        )

    estimated = _comment_value(comment_kv, "estimated_printing_time") or _comment_value(
        comment_kv, "estimated_printing_time_(normal_mode)"
    )
    if estimated:
        notes.append(
            NotebookNote(
                note_id="global_estimated_time",
                scope=NoteScope.GLOBAL,
                title="Estimated print time",
                text=f"Estimated print time: {estimated}.",
                confidence=Confidence.MEDIUM,
                reason_tags=("estimated_time",),
                provenance=NoteProvenance(source=ProvenanceSource.PARSER, evidence=("gcode_comments",)),
                priority=13,
            )
        )
    return notes


def _build_local_notes(sections: tuple[JobSection, ...]) -> list[NotebookNote]:
    notes: list[NotebookNote] = []
    for section in sections:
        text_parts: list[str] = []
        priority = 80
        fact_tags: set[str] = set()
        if "bridge" in section.tags:
            text_parts.append("Bridge-heavy region")
            priority = min(priority, 40)
        if "high_flow" in section.tags:
            text_parts.append("flow-heavy region")
            priority = min(priority, 45)
        if "tiny_layer" in section.tags:
            text_parts.append("short-layer region")
            priority = min(priority, 50)
        if section.gcode_facts.long_travel_count:
            text_parts.append(
                f"{section.gcode_facts.long_travel_count} long travel"
                f"{'s' if section.gcode_facts.long_travel_count != 1 else ''}"
            )
            fact_tags.add("long_travel")
            priority = min(priority, 42)
        if section.gcode_facts.retraction_count:
            text_parts.append(
                f"{section.gcode_facts.retraction_count} retraction"
                f"{'s' if section.gcode_facts.retraction_count != 1 else ''}"
            )
            fact_tags.add("retraction")
            priority = min(priority, 43)
        if section.gcode_facts.restart_count:
            fact_tags.add("restart")
        if "stringing_test_geometry" in section.tags:
            text_parts.append("repeated-tower geometry")
            priority = min(priority, 38)
        if not text_parts:
            continue
        if section.z_min_mm is not None:
            text = f"{', '.join(text_parts).capitalize()} around Z={section.z_min_mm:.2f} mm."
        else:
            text = f"{', '.join(text_parts).capitalize()}."
        notes.append(
            NotebookNote(
                note_id=f"note_{section.section_id}",
                scope=NoteScope.LOCAL,
                title="Section watchpoint",
                text=text,
                confidence=Confidence.MEDIUM if priority <= 45 else Confidence.LOW,
                reason_tags=tuple(sorted(set(section.tags).union(fact_tags))),
                suggested_families=_families_from_tags(section.tags),
                section_ids=(section.section_id,),
                anchors=NoteAnchors(
                    layer_range=(section.layer_start, section.layer_end),
                    z_range_mm=(section.z_start_mm, section.z_end_mm),
                    gcode_line_range=(section.gcode_line_start, section.gcode_line_end),
                ),
                provenance=NoteProvenance(
                    source=ProvenanceSource.PARSER,
                    evidence=(f"section_id={section.section_id}", *[f"tag={tag}" for tag in section.tags]),
                ),
                priority=priority,
            )
        )
    return notes


def _section_summary(*, tags: tuple[str, ...], z: float | None, layer: int | None = None) -> str:
    parts: list[str] = []
    if layer is not None:
        parts.append(f"layer={layer}")
    if z is not None:
        parts.append(f"Z={z:.2f}mm")
    if tags:
        parts.append(", ".join(tags))
    return " | ".join(parts) if parts else "Layer section"


def _families_from_tags(tags: tuple[str, ...]) -> tuple[Family, ...]:
    suggested: list[Family] = []
    if "bridge" in tags:
        suggested.extend([Family.SPEED, Family.FLOW, Family.NOZZLE])
    if "high_flow" in tags:
        suggested.extend([Family.FLOW, Family.SPEED])
    if "tiny_layer" in tags:
        suggested.extend([Family.SPEED, Family.NOZZLE])
    if "stringing_test_geometry" in tags or "repeated_tower" in tags:
        suggested.extend([Family.PRESSURE_ADVANCE, Family.ACCEL, Family.NOZZLE])
    seen: set[Family] = set()
    ordered: list[Family] = []
    for family in suggested:
        if family in seen:
            continue
        seen.add(family)
        ordered.append(family)
    return tuple(ordered)


def _tags_from_type(type_text: str) -> set[str]:
    lowered = type_text.strip().lower()
    tags: set[str] = set()
    if "bridge" in lowered:
        tags.add("bridge")
    if "perimeter" in lowered:
        tags.add("perimeter")
    if "infill" in lowered:
        tags.add("high_flow")
    if "support" in lowered:
        tags.add("support")
    if "interface" in lowered:
        tags.add("interface")
    if "external" in lowered:
        tags.add("surface")
    return tags


def _extract_metadata(file_info: dict[str, Any]) -> dict[str, Any]:
    """Return the first nested metadata-like object found in *file_info*."""
    candidates = [
        file_info.get("metadata"),
        file_info.get("meta"),
        file_info.get("print_metadata"),
    ]
    for candidate in candidates:
        if isinstance(candidate, dict):
            return candidate

    # Fall back to a recursive search for a nested object that contains known
    # metadata keys.
    queue: list[Any] = [file_info]
    metadata_keys = {"bed_temperature", "extruder_temperature", "layer_height", "filament_type", "material"}
    while queue:
        node = queue.pop(0)
        if isinstance(node, dict):
            if metadata_keys.intersection({str(key) for key in node.keys()}):
                return node
            queue.extend(node.values())
        elif isinstance(node, list):
            queue.extend(node)
    return {}


def _note_from_json(raw_note: Any) -> NotebookNote | None:
    if not isinstance(raw_note, dict):
        return None
    note_id = _string_or_none(raw_note.get("note_id"))
    text = _string_or_none(raw_note.get("text"))
    if not note_id or not text:
        return None
    section_ids = tuple(
        str(item).strip()
        for item in raw_note.get("section_ids", [])
        if str(item).strip()
    )
    scope = NoteScope(str(raw_note.get("scope") or "local"))
    if scope == NoteScope.GLOBAL and section_ids:
        return None
    if scope == NoteScope.LOCAL and not section_ids:
        return None
    title = _string_or_none(raw_note.get("title")) or _string_or_none(raw_note.get("kind")) or note_id
    confidence_text = str(raw_note.get("confidence") or "medium").lower()
    confidence = Confidence(confidence_text if confidence_text in {"low", "medium", "high"} else "medium")
    reason_tags = tuple(
        str(item).strip()
        for item in raw_note.get("reason_tags", []) or []
        if str(item).strip()
    )
    suggested_families = tuple(
        family
        for family in (
            Family(str(item))
            for item in raw_note.get("suggested_families", []) or []
            if str(item) in {"speed", "flow", "nozzle", "bed", "pressure_advance", "accel"}
        )
    )
    anchors_payload = raw_note.get("anchors") if isinstance(raw_note.get("anchors"), dict) else {}
    layer_range = _pair_or_none(anchors_payload.get("layer_range"))
    z_range_mm = _float_pair_or_none(anchors_payload.get("z_range_mm"))
    gcode_line_range = _pair_or_none(anchors_payload.get("gcode_line_range"))
    provenance_payload = raw_note.get("provenance") if isinstance(raw_note.get("provenance"), dict) else {}
    provenance_source = str(provenance_payload.get("source") or ("manual" if scope == NoteScope.GLOBAL else "imported")).lower()
    if provenance_source not in {"parser", "llm", "manual", "imported"}:
        provenance_source = "imported"
    evidence = tuple(
        str(item).strip()
        for item in provenance_payload.get("evidence", []) or []
        if str(item).strip()
    )
    return NotebookNote(
        note_id=note_id,
        scope=scope,
        title=title,
        text=text,
        confidence=confidence,
        reason_tags=reason_tags,
        suggested_families=suggested_families,
        section_ids=section_ids,
        anchors=NoteAnchors(
            layer_range=layer_range,
            z_range_mm=z_range_mm,
            gcode_line_range=gcode_line_range,
        ),
        provenance=NoteProvenance(
            source=ProvenanceSource(provenance_source),
            evidence=evidence or (f"scope={scope.value}",),
        ),
        priority=int(raw_note.get("priority") or 90),
    )


def _metadata_string(metadata: dict[str, Any], key: str) -> str | None:
    value = metadata.get(key)
    return _string_or_none(value)


def _metadata_number(metadata: dict[str, Any], key: str) -> float | None:
    value = metadata.get(key)
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _comment_value(mapping: dict[str, str], key: str) -> str | None:
    value = mapping.get(key.lower())
    return value.strip() if value else None


def _first_gcode_number(file_text: str | None, pattern: re.Pattern[str]) -> float | None:
    if not file_text:
        return None
    match = pattern.search(file_text)
    if not match:
        return None
    try:
        return float(match.group(1))
    except (TypeError, ValueError):
        return None


def _coalesce(*values: str | None) -> str | None:
    for value in values:
        if value:
            return value
    return None


def _coalesce_int(*values: float | str | None) -> int | None:
    for value in values:
        if value is None:
            continue
        try:
            return int(round(float(value)))
        except (TypeError, ValueError):
            continue
    return None


def _coalesce_float(*values: float | str | None) -> float | None:
    for value in values:
        if value is None:
            continue
        try:
            return float(value)
        except (TypeError, ValueError):
            continue
    return None


def _parse_print_time_s(value: str | None) -> float | None:
    if not value:
        return None
    text = value.strip().lower()
    colon_match = re.fullmatch(r"(?:(\d+):)?(\d{1,2}):(\d{1,2})", text)
    if colon_match:
        hours = int(colon_match.group(1) or 0)
        minutes = int(colon_match.group(2))
        seconds = int(colon_match.group(3))
        return float((hours * 3600) + (minutes * 60) + seconds)
    match = _PRINT_TIME_RE.fullmatch(text)
    if not match or not any(match.groups()):
        return None
    hours = float(match.group(1) or 0.0)
    minutes = float(match.group(2) or 0.0)
    seconds = float(match.group(3) or 0.0)
    total = (hours * 3600.0) + (minutes * 60.0) + seconds
    return total if total > 0 else None


def _string_or_none(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _source_type(display_name: str | None, source_path: str | None) -> str:
    candidate = (display_name or source_path or "").lower()
    if candidate.endswith(".bgcode") or candidate.endswith(".bgc"):
        return "bgcode"
    if candidate.endswith(".gcode") or candidate.endswith(".gco"):
        return "gcode"
    if candidate.endswith(".3mf"):
        return "3mf"
    return "unknown"


def _material_family(material: str | None) -> str | None:
    if not material:
        return None
    token = material.strip().split()[0].strip().upper()
    return token or None


def _detect_slicer_name(file_text: str | None) -> str | None:
    if not file_text:
        return None
    first_lines = file_text.splitlines()[:5]
    for line in first_lines:
        lowered = line.strip().lower()
        if "prusaslicer" in lowered:
            return "PrusaSlicer"
        if "orca" in lowered:
            return "OrcaSlicer"
        if "superslicer" in lowered:
            return "SuperSlicer"
    return None


def _generated_at_iso(*, printable: PrintableFile | None, file_info: dict[str, Any] | None, fallback: str) -> str:
    modified = None
    if printable is not None and printable.modified_ts is not None:
        modified = printable.modified_ts
    elif isinstance(file_info, dict):
        candidate = file_info.get("m_timestamp") or file_info.get("modified_ts")
        if isinstance(candidate, (int, float)):
            modified = int(candidate)
    if modified is None:
        return fallback
    return datetime.fromtimestamp(int(modified), tz=timezone.utc).isoformat().replace("+00:00", "Z")


def _pair_or_none(value: Any) -> tuple[int | None, int | None] | None:
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        return None
    return (_int_or_none(value[0]), _int_or_none(value[1]))


def _float_pair_or_none(value: Any) -> tuple[float | None, float | None] | None:
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        return None
    return (_float_or_none(value[0]), _float_or_none(value[1]))


def _float_quad_or_none(value: Any) -> tuple[float, float, float, float] | None:
    if not isinstance(value, (list, tuple)) or len(value) != 4:
        return None
    parsed = tuple(_float_or_none(item) for item in value)
    if any(item is None for item in parsed):
        return None
    return (float(parsed[0]), float(parsed[1]), float(parsed[2]), float(parsed[3]))


def _int_or_none(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _float_or_none(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _section_to_jsonable(section: JobSection) -> dict[str, Any]:
    return {
        "section_id": section.section_id,
        "label": section.label,
        "layer_start": section.layer_start,
        "layer_end": section.layer_end,
        "z_start_mm": section.z_start_mm,
        "z_end_mm": section.z_end_mm,
        "gcode_line_start": section.gcode_line_start,
        "gcode_line_end": section.gcode_line_end,
        "progress_start_pct": section.progress_start_pct,
        "progress_end_pct": section.progress_end_pct,
        "tags": list(section.tags),
        "summary": section.summary,
        "pressure_advance_baseline": section.pressure_advance_baseline,
        "print_accel_baseline_mm_s2": section.print_accel_baseline_mm_s2,
        "gcode_facts": _section_gcode_facts_to_jsonable(section.gcode_facts),
    }


def _section_from_jsonable(payload: Any) -> JobSection:
    if not isinstance(payload, dict):
        raise ValueError("notebook section must be an object")
    section_keys = set(payload.keys())
    if (
        section_keys != _NOTEBOOK_SECTION_KEYS
        and section_keys != _NOTEBOOK_SECTION_KEYS_WITHOUT_GCODE_FACTS
        and section_keys != _NOTEBOOK_SECTION_KEYS_WITH_GCODE_FACTS_WITHOUT_PROGRESS
        and section_keys != _LEGACY_NOTEBOOK_SECTION_KEYS
    ):
        raise ValueError(
            "notebook.sections[] keys mismatch "
            f"expected one of {sorted(_LEGACY_NOTEBOOK_SECTION_KEYS)!r}, "
            f"{sorted(_NOTEBOOK_SECTION_KEYS_WITHOUT_GCODE_FACTS)!r}, "
            f"{sorted(_NOTEBOOK_SECTION_KEYS_WITH_GCODE_FACTS_WITHOUT_PROGRESS)!r}, or {sorted(_NOTEBOOK_SECTION_KEYS)!r} "
            f"actual={sorted(section_keys)!r}"
        )
    tags = payload.get("tags")
    if not isinstance(tags, list):
        raise ValueError("notebook section tags must be a list")
    return JobSection(
        section_id=str(payload["section_id"]),
        label=_string_or_none(payload.get("label")),
        layer_start=_int_or_none(payload.get("layer_start")),
        layer_end=_int_or_none(payload.get("layer_end")),
        z_start_mm=_float_or_none(payload.get("z_start_mm")),
        z_end_mm=_float_or_none(payload.get("z_end_mm")),
        gcode_line_start=_int_or_none(payload.get("gcode_line_start")),
        gcode_line_end=_int_or_none(payload.get("gcode_line_end")),
        progress_start_pct=float(payload.get("progress_start_pct", 0.0)),
        progress_end_pct=float(payload.get("progress_end_pct", 100.0)),
        tags=tuple(str(item) for item in tags),
        summary=_string_or_none(payload.get("summary")),
        pressure_advance_baseline=_float_or_none(payload.get("pressure_advance_baseline")),
        print_accel_baseline_mm_s2=_float_or_none(payload.get("print_accel_baseline_mm_s2")),
        gcode_facts=_section_gcode_facts_from_jsonable(payload.get("gcode_facts")),
    )


def _section_gcode_facts_to_jsonable(facts: SectionGcodeFacts) -> dict[str, Any]:
    return {
        "long_travel_count": facts.long_travel_count,
        "long_travel_distance_mm": facts.long_travel_distance_mm,
        "travel_distance_mm": facts.travel_distance_mm,
        "extrusion_move_distance_mm": facts.extrusion_move_distance_mm,
        "travel_to_extrusion_ratio": facts.travel_to_extrusion_ratio,
        "retraction_count": facts.retraction_count,
        "restart_count": facts.restart_count,
        "active_m204_p_mm_s2": facts.active_m204_p_mm_s2,
        "active_m204_t_mm_s2": facts.active_m204_t_mm_s2,
        "active_m204_r_mm_s2": facts.active_m204_r_mm_s2,
        "extrusion_cluster_count": facts.extrusion_cluster_count,
        "extrusion_bbox_mm": list(facts.extrusion_bbox_mm) if facts.extrusion_bbox_mm is not None else None,
        "snapshot_windows": [_snapshot_window_to_jsonable(window) for window in facts.snapshot_windows],
    }


def _section_gcode_facts_from_jsonable(payload: Any) -> SectionGcodeFacts:
    if payload is None:
        return SectionGcodeFacts()
    if not isinstance(payload, dict):
        raise ValueError("notebook section gcode_facts must be an object")
    _require_exact_keys(payload, _NOTEBOOK_GCODE_FACT_KEYS, "notebook section gcode_facts")
    windows_payload = payload.get("snapshot_windows")
    if not isinstance(windows_payload, list):
        raise ValueError("notebook section gcode_facts.snapshot_windows must be a list")
    bbox = _float_quad_or_none(payload.get("extrusion_bbox_mm"))
    return SectionGcodeFacts(
        long_travel_count=int(payload.get("long_travel_count") or 0),
        long_travel_distance_mm=float(payload.get("long_travel_distance_mm") or 0.0),
        travel_distance_mm=float(payload.get("travel_distance_mm") or 0.0),
        extrusion_move_distance_mm=float(payload.get("extrusion_move_distance_mm") or 0.0),
        travel_to_extrusion_ratio=_float_or_none(payload.get("travel_to_extrusion_ratio")),
        retraction_count=int(payload.get("retraction_count") or 0),
        restart_count=int(payload.get("restart_count") or 0),
        active_m204_p_mm_s2=_float_or_none(payload.get("active_m204_p_mm_s2")),
        active_m204_t_mm_s2=_float_or_none(payload.get("active_m204_t_mm_s2")),
        active_m204_r_mm_s2=_float_or_none(payload.get("active_m204_r_mm_s2")),
        extrusion_cluster_count=int(payload.get("extrusion_cluster_count") or 0),
        extrusion_bbox_mm=bbox,
        snapshot_windows=tuple(_snapshot_window_from_jsonable(item) for item in windows_payload),
    )


def _snapshot_window_to_jsonable(window: GcodeSnapshotWindow) -> dict[str, Any]:
    return {
        "window_id": window.window_id,
        "label": window.label,
        "gcode_line_start": window.gcode_line_start,
        "gcode_line_end": window.gcode_line_end,
        "reason_tags": list(window.reason_tags),
    }


def _snapshot_window_from_jsonable(payload: Any) -> GcodeSnapshotWindow:
    if not isinstance(payload, dict):
        raise ValueError("notebook section snapshot window must be an object")
    _require_exact_keys(payload, _NOTEBOOK_SNAPSHOT_WINDOW_KEYS, "notebook section snapshot window")
    reason_tags = payload.get("reason_tags")
    if not isinstance(reason_tags, list):
        raise ValueError("notebook section snapshot window reason_tags must be a list")
    return GcodeSnapshotWindow(
        window_id=str(payload["window_id"]),
        label=str(payload["label"]),
        gcode_line_start=_int_or_none(payload.get("gcode_line_start")),
        gcode_line_end=_int_or_none(payload.get("gcode_line_end")),
        reason_tags=tuple(str(item) for item in reason_tags),
    )


def _gcode_number_in_line(line: str, pattern: re.Pattern[str]) -> float | None:
    match = pattern.search(line)
    if not match:
        return None
    try:
        return float(match.group(1))
    except (TypeError, ValueError):
        return None


def _note_to_jsonable(note: NotebookNote) -> dict[str, Any]:
    return {
        "note_id": note.note_id,
        "scope": note.scope.value,
        "title": note.title,
        "text": note.text,
        "confidence": note.confidence.value,
        "reason_tags": list(note.reason_tags),
        "suggested_families": [family.value for family in note.suggested_families],
        "section_ids": list(note.section_ids),
        "anchors": {
            "layer_range": list(note.anchors.layer_range) if note.anchors.layer_range is not None else None,
            "z_range_mm": list(note.anchors.z_range_mm) if note.anchors.z_range_mm is not None else None,
            "gcode_line_range": list(note.anchors.gcode_line_range) if note.anchors.gcode_line_range is not None else None,
        },
        "provenance": {
            "source": note.provenance.source.value,
            "evidence": list(note.provenance.evidence),
        },
    }


def _active_note_view_to_jsonable(note) -> dict[str, Any]:
    return {
        "note_id": note.note_id,
        "scope": note.scope.value,
        "title": note.title,
        "text": note.text,
        "confidence": note.confidence.value,
        "reason_tags": list(note.reason_tags),
        "suggested_families": [family.value for family in note.suggested_families],
        "section_ids": list(note.section_ids),
    }


def _active_planner_facts_to_jsonable(facts) -> dict[str, Any]:
    active_section = None
    if facts.active_section is not None:
        active_section = {
            "section_id": facts.active_section.section_id,
            "progress_start_pct": facts.active_section.progress_start_pct,
            "progress_end_pct": facts.active_section.progress_end_pct,
            "remaining_progress_pct": facts.active_section.remaining_progress_pct,
            "remaining_time_s": facts.active_section.remaining_time_s,
            "gcode_facts": _section_gcode_facts_to_jsonable(facts.active_section.gcode_facts),
        }
    return {
        "active_section": active_section,
        "geometry": {
            key: list(value) if isinstance(value, tuple) else value
            for key, value in facts.geometry.items()
        },
    }


def _note_from_jsonable_strict(payload: Any, *, scope: NoteScope) -> NotebookNote:
    if not isinstance(payload, dict):
        raise ValueError("notebook note must be an object")
    _require_exact_keys(payload, _NOTEBOOK_NOTE_KEYS, "notebook note")
    if str(payload.get("scope")) != scope.value:
        raise ValueError(f"notebook note scope mismatch: expected {scope.value}")
    anchors_payload = payload.get("anchors")
    provenance_payload = payload.get("provenance")
    if not isinstance(anchors_payload, dict):
        raise ValueError("notebook note anchors must be an object")
    if not isinstance(provenance_payload, dict):
        raise ValueError("notebook note provenance must be an object")
    _require_exact_keys(anchors_payload, _NOTEBOOK_ANCHOR_KEYS, "notebook note anchors")
    _require_exact_keys(provenance_payload, _NOTEBOOK_PROVENANCE_KEYS, "notebook note provenance")
    note = _note_from_json(payload)
    if note is None:
        raise ValueError("invalid notebook note payload")
    return note


def _require_exact_keys(payload: dict[str, Any], expected_keys: set[str], label: str) -> None:
    actual_keys = set(payload.keys())
    if actual_keys != expected_keys:
        missing = sorted(expected_keys - actual_keys)
        extra = sorted(actual_keys - expected_keys)
        raise ValueError(f"{label} keys mismatch: missing={missing} extra={extra}")


def main(argv: list[str] | None = None) -> int:
    """CLI helper for building notebooks from local G-code files.

    This is mainly for operator debugging and Codex-assisted bring-up.  The pack
    itself calls :func:`build_job_notebook` directly.
    """
    import argparse

    parser = argparse.ArgumentParser(description="Build a grounded job notebook from a local G-code file.")
    parser.add_argument("gcode", help="Path to a local G-code or bgcode text export")
    parser.add_argument("--display-name", default=None, help="Optional display name override")
    parser.add_argument("--out", default=None, help="Optional JSON output path")
    args = parser.parse_args(argv)

    path = Path(args.gcode)
    text = path.read_text(encoding="utf-8", errors="replace")
    notebook = build_job_notebook(
        printable=PrintableFile(path=str(path), display_name=args.display_name or path.name, size_bytes=path.stat().st_size),
        file_info=None,
        file_text=text,
    )
    if notebook is None:
        raise SystemExit(1)

    payload = json.dumps(notebook_to_jsonable(notebook), indent=2, sort_keys=True)
    if args.out:
        Path(args.out).write_text(payload, encoding="utf-8")
    else:
        print(payload)
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI helper
    raise SystemExit(main())
