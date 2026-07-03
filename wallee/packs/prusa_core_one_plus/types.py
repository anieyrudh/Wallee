"""Small Prusa-specific data contracts.

These types keep the runtime grounded while exposing richer advisory notebook
context to the planner:

- one current status snapshot
- one printable file descriptor
- one persisted grounded job notebook
- one compact decision-time active-notes view
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


class PrusaLifecycle(str, Enum):
    """Planner-relevant lifecycle states for the CORE One/+ pack."""

    IDLE = "IDLE"
    PRINTING = "PRINTING"
    PAUSED = "PAUSED"
    FINISHED = "FINISHED"
    STOPPED = "STOPPED"
    ATTENTION = "ATTENTION"
    ERROR = "ERROR"
    OFFLINE = "OFFLINE"
    UNKNOWN = "UNKNOWN"


class Family(str, Enum):
    SPEED = "speed"
    FLOW = "flow"
    NOZZLE = "nozzle"
    BED = "bed"
    PRESSURE_ADVANCE = "pressure_advance"
    ACCEL = "accel"


class Confidence(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class NoteScope(str, Enum):
    GLOBAL = "global"
    LOCAL = "local"


class ProvenanceSource(str, Enum):
    PARSER = "parser"
    LLM = "llm"
    MANUAL = "manual"
    IMPORTED = "imported"


@dataclass(slots=True)
class PrintableFile:
    """Normalized file record from PrusaLink storage endpoints."""

    path: str
    display_name: str
    size_bytes: int | None = None
    printable: bool = True
    modified_ts: int | None = None
    refs: dict[str, str] = field(default_factory=dict)


@dataclass(slots=True)
class PrusaStatusSnapshot:
    """Current printer snapshot normalized from HTTP status plus info."""

    lifecycle: PrusaLifecycle
    health: str
    job_active: bool
    job_id: int | None
    job_state: str | None
    job_progress_pct: float | None
    job_time_printing_s: float | None
    current_file: str | None
    speed_pct: float | None
    flow_pct: float | None
    nozzle_temp_c: float | None
    nozzle_target_c: float | None
    bed_temp_c: float | None
    bed_target_c: float | None
    pressure_advance: float | None = None
    print_accel_mm_s2: float | None = None
    min_extrusion_temp_c: float | None = None
    nozzle_diameter_mm: float | None = None
    model: str | None = None
    serial_number: str | None = None


@dataclass(slots=True)
class GcodeSnapshotWindow:
    window_id: str
    label: str
    gcode_line_start: int | None
    gcode_line_end: int | None
    reason_tags: tuple[str, ...] = ()


@dataclass(slots=True)
class SectionGcodeFacts:
    long_travel_count: int = 0
    long_travel_distance_mm: float = 0.0
    travel_distance_mm: float = 0.0
    extrusion_move_distance_mm: float = 0.0
    travel_to_extrusion_ratio: float | None = None
    retraction_count: int = 0
    restart_count: int = 0
    active_m204_p_mm_s2: float | None = None
    active_m204_t_mm_s2: float | None = None
    active_m204_r_mm_s2: float | None = None
    extrusion_cluster_count: int = 0
    extrusion_bbox_mm: tuple[float, float, float, float] | None = None
    snapshot_windows: tuple[GcodeSnapshotWindow, ...] = ()


@dataclass(slots=True)
class JobSection:
    """One grounded slice of the print job."""

    section_id: str
    label: str | None
    layer_start: int | None
    layer_end: int | None
    z_start_mm: float | None
    z_end_mm: float | None
    gcode_line_start: int | None
    gcode_line_end: int | None
    tags: tuple[str, ...] = ()
    summary: str | None = None
    progress_start_pct: float = 0.0
    progress_end_pct: float = 100.0
    pressure_advance_baseline: float | None = None
    print_accel_baseline_mm_s2: float | None = None
    gcode_facts: SectionGcodeFacts = field(default_factory=SectionGcodeFacts)

    @property
    def line_start(self) -> int | None:
        return self.gcode_line_start

    @property
    def line_end(self) -> int | None:
        return self.gcode_line_end

    @property
    def z_min_mm(self) -> float | None:
        return self.z_start_mm

    @property
    def z_max_mm(self) -> float | None:
        return self.z_end_mm


@dataclass(slots=True)
class NoteAnchors:
    layer_range: tuple[int | None, int | None] | None = None
    z_range_mm: tuple[float | None, float | None] | None = None
    gcode_line_range: tuple[int | None, int | None] | None = None


@dataclass(slots=True)
class NoteProvenance:
    source: ProvenanceSource
    evidence: tuple[str, ...] = ()


@dataclass(slots=True)
class NotebookNote:
    """One grounded note for the reasoning layer."""

    note_id: str
    scope: NoteScope
    title: str
    text: str
    confidence: Confidence
    reason_tags: tuple[str, ...] = ()
    suggested_families: tuple[Family, ...] = ()
    section_ids: tuple[str, ...] = ()
    anchors: NoteAnchors = field(default_factory=NoteAnchors)
    provenance: NoteProvenance = field(default_factory=lambda: NoteProvenance(source=ProvenanceSource.PARSER))
    priority: int = 100

    def __post_init__(self) -> None:
        if self.scope == NoteScope.GLOBAL and self.section_ids:
            raise ValueError("global notebook notes must not include section_ids")
        if self.scope == NoteScope.LOCAL and not self.section_ids:
            raise ValueError("local notebook notes must include section_ids")


@dataclass(slots=True)
class JobMetadata:
    job_hash: str
    file_name: str
    file_path: str | None
    source_type: str
    material_profile_name: str | None
    material_family: str | None
    slicer_name: str | None
    slicer_profile: str | None
    generated_at: str


@dataclass(slots=True)
class NotebookBaselines:
    speed_pct_default: float | None = None
    flow_pct_default: float | None = None
    nozzle_target_c_default: float | None = None
    bed_target_c_default: float | None = None
    pressure_advance_default: float | None = None
    print_accel_mm_s2_default: float | None = None
    estimated_print_time_s: float | None = None


@dataclass(slots=True)
class NotebookBuild:
    parser_version: str
    notebook_builder: str
    built_at: str


@dataclass(slots=True)
class ActiveSectionPlanningFacts:
    section_id: str
    progress_start_pct: float
    progress_end_pct: float
    remaining_progress_pct: float | None
    remaining_time_s: float | None
    gcode_facts: SectionGcodeFacts


@dataclass(slots=True)
class ActivePlannerFacts:
    active_section: ActiveSectionPlanningFacts | None = None
    geometry: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class ActiveNoteView:
    note_id: str
    scope: NoteScope
    title: str
    text: str
    confidence: Confidence
    reason_tags: tuple[str, ...]
    suggested_families: tuple[Family, ...]
    section_ids: tuple[str, ...]


@dataclass(slots=True)
class ActiveNotesSelectionContext:
    lifecycle: str
    job_active: bool
    job_progress_pct: float | None
    current_layer: int | None
    current_z_mm: float | None
    current_section_ids: tuple[str, ...]
    lookahead_section_ids: tuple[str, ...]


@dataclass(slots=True)
class ActiveNotes:
    schema_version: str
    job_hash: str
    selected_at: str
    selection_context: ActiveNotesSelectionContext
    planner_facts: ActivePlannerFacts
    global_notes: tuple[ActiveNoteView, ...]
    local_notes: tuple[ActiveNoteView, ...]
    merged_reason_tags: tuple[str, ...]
    family_hints: dict[str, tuple[str, ...]]


@dataclass(slots=True)
class JobNotebook:
    """Read-only understanding artifact for one print job."""

    schema_version: str
    notebook_id: str
    printer_family: str
    job: JobMetadata
    baselines: NotebookBaselines
    sections: tuple[JobSection, ...] = ()
    global_notes: tuple[NotebookNote, ...] = ()
    local_notes: tuple[NotebookNote, ...] = ()
    build: NotebookBuild = field(
        default_factory=lambda: NotebookBuild(
            parser_version="job_notebook_v2_bgcode_decode",
            notebook_builder="wallee.packs.prusa_core_one_plus.job_notebook",
            built_at=_utc_now_iso(),
        )
    )
    layer_height_mm_internal: float | None = None
    source_text_grounded_internal: bool = False

    def __post_init__(self) -> None:
        for note in self.global_notes:
            if note.scope != NoteScope.GLOBAL:
                raise ValueError("global_notes must contain only global notes")
            if note.section_ids:
                raise ValueError("global_notes entries must not include section_ids")
        for note in self.local_notes:
            if note.scope != NoteScope.LOCAL:
                raise ValueError("local_notes must contain only local notes")
            if not note.section_ids:
                raise ValueError("local_notes entries must include section_ids")

    @property
    def job_hash(self) -> str:
        return self.job.job_hash

    @property
    def display_name(self) -> str | None:
        return self.job.file_name

    @property
    def source_path(self) -> str | None:
        return self.job.file_path

    @property
    def material(self) -> str | None:
        return self.job.material_profile_name

    @property
    def nozzle_target_c(self) -> int | None:
        value = self.baselines.nozzle_target_c_default
        return int(round(value)) if value is not None else None

    @property
    def bed_target_c(self) -> int | None:
        value = self.baselines.bed_target_c_default
        return int(round(value)) if value is not None else None

    @property
    def layer_height_mm(self) -> float | None:
        return self.layer_height_mm_internal

    @property
    def parser_version(self) -> str:
        return self.build.parser_version

    def active_pressure_advance_baseline(self, progress_pct: float | None) -> float | None:
        section = self.active_section(progress_pct)
        if section is not None and section.pressure_advance_baseline is not None:
            return section.pressure_advance_baseline
        return self.baselines.pressure_advance_default

    def active_print_accel_baseline_mm_s2(self, progress_pct: float | None) -> float | None:
        section = self.active_section(progress_pct)
        if section is not None and section.print_accel_baseline_mm_s2 is not None:
            return section.print_accel_baseline_mm_s2
        return self.baselines.print_accel_mm_s2_default

    def active_planner_facts(self, progress_pct: float | None) -> ActivePlannerFacts:
        section = self.active_section(progress_pct)
        active_section = None
        if section is not None:
            remaining_progress_pct = None
            remaining_time_s = None
            if progress_pct is not None:
                remaining_progress_pct = max(0.0, round(section.progress_end_pct - progress_pct, 2))
                if self.baselines.estimated_print_time_s is not None:
                    remaining_time_s = round((remaining_progress_pct / 100.0) * self.baselines.estimated_print_time_s, 1)
            active_section = ActiveSectionPlanningFacts(
                section_id=section.section_id,
                progress_start_pct=section.progress_start_pct,
                progress_end_pct=section.progress_end_pct,
                remaining_progress_pct=remaining_progress_pct,
                remaining_time_s=remaining_time_s,
                gcode_facts=section.gcode_facts,
            )
        return ActivePlannerFacts(
            active_section=active_section,
            geometry=self._geometry_planner_facts(),
        )

    def active_section(self, progress_pct: float | None) -> JobSection | None:
        """Return the section that most likely matches *progress_pct*."""
        if progress_pct is None or not self.sections:
            return None
        for section in self.sections:
            if section.progress_start_pct <= progress_pct <= section.progress_end_pct:
                return section
        for section in self.sections:
            if progress_pct < section.progress_start_pct:
                return section
        return self.sections[-1]

    def selection_context(self, progress_pct: float | None, lookahead_pct: float = 5.0, *, lifecycle: str, job_active: bool) -> ActiveNotesSelectionContext:
        current_sections = self._sections_for_window(progress_pct, progress_pct)
        if not current_sections and progress_pct is not None:
            active = self.active_section(progress_pct)
            if active is not None:
                current_sections = [active]
        window_end = None if progress_pct is None else min(100.0, progress_pct + max(0.0, lookahead_pct))
        lookahead_sections = self._sections_for_window(progress_pct, window_end)
        current_ids = tuple(section.section_id for section in current_sections)
        lookahead_ids = tuple(section.section_id for section in lookahead_sections if section.section_id not in current_ids)
        active = current_sections[0] if current_sections else None
        return ActiveNotesSelectionContext(
            lifecycle=lifecycle,
            job_active=job_active,
            job_progress_pct=progress_pct,
            current_layer=active.layer_start if active is not None else None,
            current_z_mm=active.z_start_mm if active is not None else None,
            current_section_ids=current_ids,
            lookahead_section_ids=lookahead_ids,
        )

    def active_notes(self, progress_pct: float | None, lookahead_pct: float = 5.0) -> list[NotebookNote]:
        """Return global notes plus local notes near *progress_pct*."""
        context = self.selection_context(progress_pct, lookahead_pct, lifecycle=PrusaLifecycle.UNKNOWN.value, job_active=progress_pct is not None)
        active = self.select_active_notes(
            progress_pct,
            lookahead_pct=lookahead_pct,
            lifecycle=context.lifecycle,
            job_active=context.job_active,
        )
        note_map = {note.note_id: note for note in self.global_notes + self.local_notes}
        ordered_ids = [view.note_id for view in active.global_notes] + [view.note_id for view in active.local_notes]
        return [note_map[note_id] for note_id in ordered_ids if note_id in note_map]

    def select_active_notes(
        self,
        progress_pct: float | None,
        *,
        lookahead_pct: float = 5.0,
        lifecycle: str,
        job_active: bool,
        global_limit: int = 4,
        local_limit: int = 6,
    ) -> ActiveNotes:
        """Return the compact decision-time active-notes view."""
        context = self.selection_context(progress_pct, lookahead_pct, lifecycle=lifecycle, job_active=job_active)
        current_ids = set(context.current_section_ids)
        lookahead_ids = set(context.lookahead_section_ids)

        global_notes = sorted(self.global_notes, key=_note_sort_key)[:global_limit]
        local_candidates = [
            note
            for note in self.local_notes
            if set(note.section_ids).intersection(current_ids) or set(note.section_ids).intersection(lookahead_ids)
        ]
        local_notes = sorted(local_candidates, key=_note_sort_key)[:local_limit]

        global_views = tuple(_active_note_view(note) for note in global_notes)
        local_views = tuple(_active_note_view(note) for note in local_notes)
        merged_reason_tags = tuple(
            sorted(
                {
                    reason
                    for note in (*global_views, *local_views)
                    for reason in note.reason_tags
                }
            )
        )
        family_hints = {
            family.value: tuple(
                note.note_id
                for note in (*global_views, *local_views)
                if family in note.suggested_families
            )
            for family in Family
        }
        return ActiveNotes(
            schema_version="1.0",
            job_hash=self.job_hash,
            selected_at=_utc_now_iso(),
            selection_context=context,
            planner_facts=self.active_planner_facts(progress_pct),
            global_notes=global_views,
            local_notes=local_views,
            merged_reason_tags=merged_reason_tags,
            family_hints=family_hints,
        )

    def active_notes_summary(self, progress_pct: float | None, lookahead_pct: float = 5.0, limit: int = 3) -> str | None:
        """Return a compact summary string of the active notes."""
        active = self.select_active_notes(
            progress_pct,
            lookahead_pct=lookahead_pct,
            lifecycle=PrusaLifecycle.UNKNOWN.value,
            job_active=progress_pct is not None,
        )
        selected = list(active.local_notes[:1]) + list(active.global_notes)
        if len(selected) < limit and len(active.local_notes) > 1:
            selected.extend(list(active.local_notes[1 : 1 + (limit - len(selected))]))
        texts = [note.text.strip() for note in selected[:limit] if note.text.strip()]
        if not texts:
            return None
        summary = " | ".join(texts)
        if len(summary) > 320:
            summary = summary[:317].rstrip() + "..."
        return summary

    def _sections_for_window(self, progress_start: float | None, progress_end: float | None) -> list[JobSection]:
        if progress_start is None or progress_end is None:
            return []
        return [
            section
            for section in self.sections
            if section.progress_start_pct <= progress_end and section.progress_end_pct >= progress_start
        ]

    def _geometry_planner_facts(self) -> dict[str, Any]:
        tagged = [
            section
            for section in self.sections
            if "stringing_test_geometry" in section.tags or "repeated_tower" in section.tags
        ]
        if not tagged:
            return {
                "repeated_tower_or_stringing_test": False,
                "confidence": Confidence.LOW.value,
                "reason_tags": (),
                "section_ids": (),
            }
        confidence = Confidence.HIGH if len(tagged) >= 3 else Confidence.MEDIUM
        return {
            "repeated_tower_or_stringing_test": True,
            "confidence": confidence.value,
            "reason_tags": ("repeated_tower", "stringing_test_geometry"),
            "section_ids": tuple(section.section_id for section in tagged[:8]),
        }


def _confidence_rank(confidence: Confidence) -> int:
    if confidence == Confidence.HIGH:
        return 0
    if confidence == Confidence.MEDIUM:
        return 1
    return 2


def _note_sort_key(note: NotebookNote) -> tuple[int, int, str]:
    return (note.priority, _confidence_rank(note.confidence), note.note_id)


def _active_note_view(note: NotebookNote) -> ActiveNoteView:
    return ActiveNoteView(
        note_id=note.note_id,
        scope=note.scope,
        title=note.title,
        text=note.text,
        confidence=note.confidence,
        reason_tags=note.reason_tags,
        suggested_families=note.suggested_families,
        section_ids=note.section_ids,
    )
