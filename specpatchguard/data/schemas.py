"""Core data schemas, JSON round-trip helpers, and Tier-A/Tier-B extension protocols."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Literal, Protocol, runtime_checkable

TierType = Literal["tier_a", "tier_b", "tier_c"]
VerdictType = Literal["CORRECT", "OVERFITTING", "UNCERTAIN"]
AnchorKind = Literal["modify", "add_only", "delete_only", "context_only"]
ArbitrationVerdictType = Literal["pending", "patch_overfitting", "python_semantic_gap", "inconclusive"]


def _as_dict_list(items: Any) -> list[dict[str, Any]]:
    if not items:
        return []
    return [dict(item) for item in items]


def _as_str_list(items: Any) -> list[str]:
    if not items:
        return []
    return [str(item) for item in items]


@dataclass(frozen=True)
class BugReport:
    """Normalized bug report entry from `clean_d4j20_bug_report.json`."""

    bug_id: str
    title: str = ""
    description: str = ""
    available: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> BugReport:
        return cls(
            bug_id=str(data["bug_id"]),
            title=str(data.get("title", "")),
            description=str(data.get("description", "")),
            available=bool(data.get("available", False)),
        )


@dataclass(frozen=True)
class FailingTestRecord:
    """Normalized failing test entry from `labeled_d4j20_enhance_info.pkl`."""

    test_file: str
    test_method_name: str
    start_line: int
    source_code: str
    numbered_source: str
    failed_line_start: int | None
    failed_line_end: int | None
    failure_message: str
    stack_trace: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> FailingTestRecord:
        failed_start = data.get("failed_line_start")
        failed_end = data.get("failed_line_end")
        return cls(
            test_file=str(data.get("test_file", "")),
            test_method_name=str(data.get("test_method_name", "")),
            start_line=int(data.get("start_line", 1)),
            source_code=str(data.get("source_code", "")),
            numbered_source=str(data.get("numbered_source", "")),
            failed_line_start=int(failed_start) if failed_start is not None else None,
            failed_line_end=int(failed_end) if failed_end is not None else None,
            failure_message=str(data.get("failure_message", "")),
            stack_trace=str(data.get("stack_trace", "")),
        )


@dataclass(frozen=True)
class DiffHunk:
    """Parsed unified diff hunk with pre-patch anchor coordinates."""

    file_path: str
    old_start: int
    old_count: int
    new_start: int
    new_count: int
    header: str
    lines: list[str]
    added_lines: list[str]
    deleted_lines: list[str]
    anchor_start_line: int
    anchor_end_line: int
    kind: AnchorKind

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> DiffHunk:
        return cls(
            file_path=str(data["file_path"]),
            old_start=int(data["old_start"]),
            old_count=int(data["old_count"]),
            new_start=int(data["new_start"]),
            new_count=int(data["new_count"]),
            header=str(data.get("header", "")),
            lines=_as_str_list(data.get("lines")),
            added_lines=_as_str_list(data.get("added_lines")),
            deleted_lines=_as_str_list(data.get("deleted_lines")),
            anchor_start_line=int(data["anchor_start_line"]),
            anchor_end_line=int(data["anchor_end_line"]),
            kind=data["kind"],
        )


@dataclass(frozen=True)
class SubPatchSlice:
    """Function-level or fallback diff slice stored in `d4j_custom_function_level_patch.pkl`."""

    file_path: str
    start_line: int
    content: str
    is_fallback_diff: bool

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> SubPatchSlice:
        return cls(
            file_path=str(data["file_path"]),
            start_line=int(data["start_line"]),
            content=str(data.get("content", "")),
            is_fallback_diff=bool(data.get("is_fallback_diff", False)),
        )


@dataclass
class RawPatchRecord:
    """Complete raw candidate patch record (held only outside Patch-Blind agents)."""

    patch_id: str
    project: str
    bug_num: int
    bug_id: str
    tool: str
    label: int | None
    raw_diff: str
    hunks: list[DiffHunk]
    sub_patches: list[SubPatchSlice]
    failing_tests: list[FailingTestRecord]
    bug_report: BugReport
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "patch_id": self.patch_id,
            "project": self.project,
            "bug_num": self.bug_num,
            "bug_id": self.bug_id,
            "tool": self.tool,
            "label": self.label,
            "raw_diff": self.raw_diff,
            "hunks": [h.to_dict() for h in self.hunks],
            "sub_patches": [s.to_dict() for s in self.sub_patches],
            "failing_tests": [t.to_dict() for t in self.failing_tests],
            "bug_report": self.bug_report.to_dict(),
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> RawPatchRecord:
        label_val = data.get("label")
        return cls(
            patch_id=str(data["patch_id"]),
            project=str(data["project"]),
            bug_num=int(data["bug_num"]),
            bug_id=str(data["bug_id"]),
            tool=str(data.get("tool", "unknown")),
            label=int(label_val) if label_val is not None else None,
            raw_diff=str(data.get("raw_diff", "")),
            hunks=[DiffHunk.from_dict(item) for item in data.get("hunks", [])],
            sub_patches=[SubPatchSlice.from_dict(item) for item in data.get("sub_patches", [])],
            failing_tests=[FailingTestRecord.from_dict(item) for item in data.get("failing_tests", [])],
            bug_report=BugReport.from_dict(data["bug_report"]),
            metadata=dict(data.get("metadata", {})),
        )


@dataclass(frozen=True)
class PatchTargetSelector:
    """Fine-grained selector targeting an official Defects4J bug, patch, file, or method."""

    bug_id: str
    patch_name: str = ""
    file_path: str = ""
    method_name: str = ""
    local_diff_path: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> PatchTargetSelector:
        return cls(
            bug_id=str(data["bug_id"]),
            patch_name=str(data.get("patch_name", "")),
            file_path=str(data.get("file_path", "")),
            method_name=str(data.get("method_name", "")),
            local_diff_path=str(data.get("local_diff_path", "")),
        )


@dataclass
class DatasetSpec:
    """User-supplied dataset selection specification aligned with official Defects4J naming."""

    source_label: str = "explicit"
    source_path: str | None = None
    projects: list[str] = field(default_factory=list)
    bug_ids: list[str] = field(default_factory=list)
    targets: list[PatchTargetSelector] = field(default_factory=list)
    file_paths: list[str] = field(default_factory=list)
    method_names: list[str] = field(default_factory=list)
    limit: int | None = None
    require_tests: bool = False
    strict: bool = False

    def is_unfiltered(self) -> bool:
        return not (
            self.projects
            or self.bug_ids
            or self.targets
            or self.file_paths
            or self.method_names
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_label": self.source_label,
            "source_path": self.source_path,
            "projects": list(self.projects),
            "bug_ids": list(self.bug_ids),
            "targets": [t.to_dict() for t in self.targets],
            "file_paths": list(self.file_paths),
            "method_names": list(self.method_names),
            "limit": self.limit,
            "require_tests": self.require_tests,
            "strict": self.strict,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> DatasetSpec:
        limit_val = data.get("limit")
        return cls(
            source_label=str(data.get("source_label", "explicit")),
            source_path=str(data["source_path"]) if data.get("source_path") is not None else None,
            projects=_as_str_list(data.get("projects")),
            bug_ids=_as_str_list(data.get("bug_ids")),
            targets=[PatchTargetSelector.from_dict(item) for item in data.get("targets", [])],
            file_paths=_as_str_list(data.get("file_paths")),
            method_names=_as_str_list(data.get("method_names")),
            limit=int(limit_val) if limit_val is not None else None,
            require_tests=bool(data.get("require_tests", False)),
            strict=bool(data.get("strict", False)),
        )


@dataclass
class DatasetLoadResult:
    """Result of selective `.pkl` and JSON loading for a user-defined `DatasetSpec`."""

    spec: DatasetSpec
    records: list[RawPatchRecord]
    matched_bug_ids: list[str]
    missing_bug_ids: list[str]
    missing_targets: list[PatchTargetSelector]
    total_scanned: int
    truncated_by_limit: bool = False

    def summary(self) -> dict[str, Any]:
        project_counts: dict[str, int] = {}
        record_items: list[dict[str, Any]] = []
        for rec in self.records:
            project_counts[rec.project] = project_counts.get(rec.project, 0) + 1
            record_items.append(
                {
                    "patch_id": rec.patch_id,
                    "bug_id": rec.bug_id,
                    "project": rec.project,
                    "tool": rec.tool,
                    "num_hunks": len(rec.hunks),
                    "num_failing_tests": len(rec.failing_tests),
                    "has_bug_report": rec.bug_report.available,
                    "matched_methods": list(rec.metadata.get("matched_methods", [])),
                }
            )
        return {
            "source_label": self.spec.source_label,
            "source_path": self.spec.source_path,
            "total_scanned": self.total_scanned,
            "selected_records": len(self.records),
            "truncated_by_limit": self.truncated_by_limit,
            "projects": dict(sorted(project_counts.items())),
            "matched_bug_ids": list(self.matched_bug_ids),
            "missing_bug_ids": list(self.missing_bug_ids),
            "missing_targets": [t.to_dict() for t in self.missing_targets],
            "records": record_items,
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "spec": self.spec.to_dict(),
            "records": [r.to_dict() for r in self.records],
            "matched_bug_ids": list(self.matched_bug_ids),
            "missing_bug_ids": list(self.missing_bug_ids),
            "missing_targets": [t.to_dict() for t in self.missing_targets],
            "total_scanned": self.total_scanned,
            "truncated_by_limit": self.truncated_by_limit,
        }


@dataclass(frozen=True)
class DiffAnchor:
    """Patch-Blind pre-patch coordinate anchor (strictly excludes `+` lines and patch verdict)."""

    file_path: str
    method_name: str
    anchor_start_line: int
    anchor_end_line: int
    hunk_old_start: int
    hunk_old_end: int
    kind: AnchorKind
    function_start_line: int | None
    function_end_line: int | None
    buggy_function_code: str
    numbered_buggy_function: str
    anchor_context_lines: str
    is_fallback_slice: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> DiffAnchor:
        f_start = data.get("function_start_line")
        f_end = data.get("function_end_line")
        return cls(
            file_path=str(data["file_path"]),
            method_name=str(data.get("method_name", "")),
            anchor_start_line=int(data["anchor_start_line"]),
            anchor_end_line=int(data["anchor_end_line"]),
            hunk_old_start=int(data["hunk_old_start"]),
            hunk_old_end=int(data["hunk_old_end"]),
            kind=data["kind"],
            function_start_line=int(f_start) if f_start is not None else None,
            function_end_line=int(f_end) if f_end is not None else None,
            buggy_function_code=str(data.get("buggy_function_code", "")),
            numbered_buggy_function=str(data.get("numbered_buggy_function", "")),
            anchor_context_lines=str(data.get("anchor_context_lines", "")),
            is_fallback_slice=bool(data.get("is_fallback_slice", False)),
        )


@dataclass
class PatchBlindTask:
    """Sanitized Patch-Blind task package passed to Agent 1, Agent 2, and Cross-Rep."""

    patch_id: str
    bug_id: str
    project: str
    bug_num: int
    anchors: list[DiffAnchor]
    affected_files: list[str]
    failing_tests: list[FailingTestRecord]
    bug_report: BugReport
    is_multi_hunk: bool
    is_multi_file: bool
    has_fallback_anchor: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "patch_id": self.patch_id,
            "bug_id": self.bug_id,
            "project": self.project,
            "bug_num": self.bug_num,
            "anchors": [a.to_dict() for a in self.anchors],
            "affected_files": list(self.affected_files),
            "failing_tests": [t.to_dict() for t in self.failing_tests],
            "bug_report": self.bug_report.to_dict(),
            "is_multi_hunk": self.is_multi_hunk,
            "is_multi_file": self.is_multi_file,
            "has_fallback_anchor": self.has_fallback_anchor,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> PatchBlindTask:
        return cls(
            patch_id=str(data["patch_id"]),
            bug_id=str(data["bug_id"]),
            project=str(data["project"]),
            bug_num=int(data["bug_num"]),
            anchors=[DiffAnchor.from_dict(item) for item in data.get("anchors", [])],
            affected_files=_as_str_list(data.get("affected_files")),
            failing_tests=[FailingTestRecord.from_dict(item) for item in data.get("failing_tests", [])],
            bug_report=BugReport.from_dict(data["bug_report"]),
            is_multi_hunk=bool(data.get("is_multi_hunk", False)),
            is_multi_file=bool(data.get("is_multi_file", False)),
            has_fallback_anchor=bool(data.get("has_fallback_anchor", False)),
        )


@dataclass(frozen=True)
class MethodSignatureInfo:
    """Parsed Java method or constructor signature metadata."""

    file_path: str
    class_name: str
    method_name: str
    visibility: str
    is_static: bool
    is_constructor: bool
    return_type: str
    parameter_types: list[str]
    parameter_names: list[str]
    throws_types: list[str]
    modifiers: list[str]
    javadoc: str
    raw_signature: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> MethodSignatureInfo:
        return cls(
            file_path=str(data.get("file_path", "")),
            class_name=str(data.get("class_name", "")),
            method_name=str(data.get("method_name", "")),
            visibility=str(data.get("visibility", "package-private")),
            is_static=bool(data.get("is_static", False)),
            is_constructor=bool(data.get("is_constructor", False)),
            return_type=str(data.get("return_type", "")),
            parameter_types=_as_str_list(data.get("parameter_types")),
            parameter_names=_as_str_list(data.get("parameter_names")),
            throws_types=_as_str_list(data.get("throws_types")),
            modifiers=_as_str_list(data.get("modifiers")),
            javadoc=str(data.get("javadoc", "")),
            raw_signature=str(data.get("raw_signature", "")),
        )


@dataclass
class BoundaryContext:
    """Structural and LSP boundary facts reserved for Agent 1 and Tier-B verification."""

    enclosing_branches: list[dict[str, Any]] = field(default_factory=list)
    class_fields: list[dict[str, Any]] = field(default_factory=list)
    callers: list[dict[str, Any]] = field(default_factory=list)
    overrides: list[dict[str, Any]] = field(default_factory=list)
    non_local_state_touches: list[dict[str, Any]] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "enclosing_branches": _as_dict_list(self.enclosing_branches),
            "class_fields": _as_dict_list(self.class_fields),
            "callers": _as_dict_list(self.callers),
            "overrides": _as_dict_list(self.overrides),
            "non_local_state_touches": _as_dict_list(self.non_local_state_touches),
            "notes": _as_str_list(self.notes),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> BoundaryContext:
        return cls(
            enclosing_branches=_as_dict_list(data.get("enclosing_branches")),
            class_fields=_as_dict_list(data.get("class_fields")),
            callers=_as_dict_list(data.get("callers")),
            overrides=_as_dict_list(data.get("overrides")),
            non_local_state_touches=_as_dict_list(data.get("non_local_state_touches")),
            notes=_as_str_list(data.get("notes")),
        )


@dataclass(frozen=True)
class TierDecision:
    """Explainable Tier-A / Tier-B routing result."""

    tier: TierType
    confidence: float
    reasons: list[str]
    signals: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> TierDecision:
        return cls(
            tier=data["tier"],
            confidence=float(data.get("confidence", 0.0)),
            reasons=_as_str_list(data.get("reasons")),
            signals=dict(data.get("signals", {})),
        )


@dataclass
class IntentContextPack:
    """Unified Patch-Blind intent + context package shared across Agent 1, Cross-Rep, and Agent 2."""

    bug_id: str
    patch_id: str
    project: str
    tier: TierType
    bug_intent_summary: str
    anchors: list[DiffAnchor]
    target_methods: list[MethodSignatureInfo]
    failing_tests: list[FailingTestRecord]
    bug_report: BugReport
    entry_harness_skeleton: str = ""
    boundary_context: BoundaryContext = field(default_factory=BoundaryContext)
    tier_decision: TierDecision | None = None
    extra_context: dict[str, Any] = field(default_factory=dict)
    parameter_field_semantics: list[dict[str, Any]] = field(default_factory=list)
    callee_contracts: list[dict[str, Any]] = field(default_factory=list)
    structured_defect_intent: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "bug_id": self.bug_id,
            "patch_id": self.patch_id,
            "project": self.project,
            "tier": self.tier,
            "bug_intent_summary": self.bug_intent_summary,
            "anchors": [a.to_dict() for a in self.anchors],
            "target_methods": [m.to_dict() for m in self.target_methods],
            "failing_tests": [t.to_dict() for t in self.failing_tests],
            "bug_report": self.bug_report.to_dict(),
            "entry_harness_skeleton": self.entry_harness_skeleton,
            "boundary_context": self.boundary_context.to_dict(),
            "tier_decision": self.tier_decision.to_dict() if self.tier_decision else None,
            "extra_context": dict(self.extra_context),
            "parameter_field_semantics": _as_dict_list(self.parameter_field_semantics),
            "callee_contracts": _as_dict_list(self.callee_contracts),
            "structured_defect_intent": dict(self.structured_defect_intent),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> IntentContextPack:
        raw_td = data.get("tier_decision")
        raw_bc = data.get("boundary_context")
        raw_sdi = data.get("structured_defect_intent")
        return cls(
            bug_id=str(data["bug_id"]),
            patch_id=str(data["patch_id"]),
            project=str(data["project"]),
            tier=data["tier"],
            bug_intent_summary=str(data.get("bug_intent_summary", "")),
            anchors=[DiffAnchor.from_dict(item) for item in data.get("anchors", [])],
            target_methods=[MethodSignatureInfo.from_dict(item) for item in data.get("target_methods", [])],
            failing_tests=[FailingTestRecord.from_dict(item) for item in data.get("failing_tests", [])],
            bug_report=BugReport.from_dict(data["bug_report"]),
            entry_harness_skeleton=str(data.get("entry_harness_skeleton", "")),
            boundary_context=BoundaryContext.from_dict(raw_bc) if raw_bc else BoundaryContext(),
            tier_decision=TierDecision.from_dict(raw_td) if raw_td else None,
            extra_context=dict(data.get("extra_context", {})),
            parameter_field_semantics=_as_dict_list(data.get("parameter_field_semantics")),
            callee_contracts=_as_dict_list(data.get("callee_contracts")),
            structured_defect_intent=dict(raw_sdi) if isinstance(raw_sdi, dict) else {},
        )


@dataclass
class PrePostSpec:
    """Formalized preconditions, postconditions, invariants, and exception/infection contracts."""

    preconditions: list[str] = field(default_factory=list)
    postconditions: list[str] = field(default_factory=list)
    invariants: list[str] = field(default_factory=list)
    exception_contracts: list[str] = field(default_factory=list)
    anchor_infection_conditions: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> PrePostSpec:
        return cls(
            preconditions=_as_str_list(data.get("preconditions")),
            postconditions=_as_str_list(data.get("postconditions")),
            invariants=_as_str_list(data.get("invariants")),
            exception_contracts=_as_str_list(data.get("exception_contracts")),
            anchor_infection_conditions=_as_str_list(data.get("anchor_infection_conditions")),
        )


@dataclass(frozen=True)
class SoftSpecCandidate:
    """Low-weight behavioral candidate reserved for Tier-B decision-table strategies."""

    candidate_id: str
    source: str
    description: str
    condition: str
    expected_behavior: str
    weight: float = 0.3
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> SoftSpecCandidate:
        return cls(
            candidate_id=str(data["candidate_id"]),
            source=str(data.get("source", "tier_b_decision_table")),
            description=str(data.get("description", "")),
            condition=str(data.get("condition", "")),
            expected_behavior=str(data.get("expected_behavior", "")),
            weight=float(data.get("weight", 0.3)),
            metadata=dict(data.get("metadata", {})),
        )


@dataclass(frozen=True)
class WitnessCase:
    """Concrete input witness evaluated against the buggy Java program in Docker/D4J."""

    case_id: str
    inputs: dict[str, Any]
    expected_outcome: str
    observed_buggy_outcome: str
    source: str
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> WitnessCase:
        return cls(
            case_id=str(data["case_id"]),
            inputs=dict(data.get("inputs", {})),
            expected_outcome=str(data.get("expected_outcome", "")),
            observed_buggy_outcome=str(data.get("observed_buggy_outcome", "")),
            source=str(data.get("source", "")),
            metadata=dict(data.get("metadata", {})),
        )


@dataclass
class WitnessBank:
    """Persisted Java-verified witnesses and optional anchor infection probe traces."""

    confirmed_bug_witnesses: list[WitnessCase] = field(default_factory=list)
    boundary_passing_witnesses: list[WitnessCase] = field(default_factory=list)
    excluded_irrelevant_cases: list[WitnessCase] = field(default_factory=list)
    infection_probe_traces: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "confirmed_bug_witnesses": [w.to_dict() for w in self.confirmed_bug_witnesses],
            "boundary_passing_witnesses": [w.to_dict() for w in self.boundary_passing_witnesses],
            "excluded_irrelevant_cases": [w.to_dict() for w in self.excluded_irrelevant_cases],
            "infection_probe_traces": _as_dict_list(self.infection_probe_traces),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> WitnessBank:
        return cls(
            confirmed_bug_witnesses=[
                WitnessCase.from_dict(item) for item in data.get("confirmed_bug_witnesses", [])
            ],
            boundary_passing_witnesses=[
                WitnessCase.from_dict(item) for item in data.get("boundary_passing_witnesses", [])
            ],
            excluded_irrelevant_cases=[
                WitnessCase.from_dict(item) for item in data.get("excluded_irrelevant_cases", [])
            ],
            infection_probe_traces=_as_dict_list(data.get("infection_probe_traces")),
        )


@dataclass
class CrossRepResult:
    """Output of a `CrossRepStrategy` (Tier-A Hypothesis seeds or Tier-B soft candidates)."""

    tier: TierType
    candidate_inputs: list[dict[str, Any]] = field(default_factory=list)
    soft_spec_candidates: list[SoftSpecCandidate] = field(default_factory=list)
    python_model_code: str = ""
    diagnostics: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "tier": self.tier,
            "candidate_inputs": _as_dict_list(self.candidate_inputs),
            "soft_spec_candidates": [s.to_dict() for s in self.soft_spec_candidates],
            "python_model_code": self.python_model_code,
            "diagnostics": dict(self.diagnostics),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> CrossRepResult:
        return cls(
            tier=data["tier"],
            candidate_inputs=_as_dict_list(data.get("candidate_inputs")),
            soft_spec_candidates=[
                SoftSpecCandidate.from_dict(item) for item in data.get("soft_spec_candidates", [])
            ],
            python_model_code=str(data.get("python_model_code", "")),
            diagnostics=dict(data.get("diagnostics", {})),
        )


@dataclass
class HarnessRunResult:
    """Parsed execution output from a JUnit test harness inside the Docker D4J sandbox."""

    tier: TierType
    compiled: bool
    passed_cases: list[dict[str, Any]] = field(default_factory=list)
    failed_cases: list[dict[str, Any]] = field(default_factory=list)
    error_cases: list[dict[str, Any]] = field(default_factory=list)
    infection_probe_traces: list[dict[str, Any]] = field(default_factory=list)
    raw_stdout: str = ""
    raw_stderr: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "tier": self.tier,
            "compiled": self.compiled,
            "passed_cases": _as_dict_list(self.passed_cases),
            "failed_cases": _as_dict_list(self.failed_cases),
            "error_cases": _as_dict_list(self.error_cases),
            "infection_probe_traces": _as_dict_list(self.infection_probe_traces),
            "raw_stdout": self.raw_stdout,
            "raw_stderr": self.raw_stderr,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> HarnessRunResult:
        return cls(
            tier=data["tier"],
            compiled=bool(data.get("compiled", False)),
            passed_cases=_as_dict_list(data.get("passed_cases")),
            failed_cases=_as_dict_list(data.get("failed_cases")),
            error_cases=_as_dict_list(data.get("error_cases")),
            infection_probe_traces=_as_dict_list(data.get("infection_probe_traces")),
            raw_stdout=str(data.get("raw_stdout", "")),
            raw_stderr=str(data.get("raw_stderr", "")),
        )


@dataclass
class CalibrationReport:
    """Bilateral calibration gate report verifying Cross-Rep proxy on passing and failing cases."""

    calibrated: bool = False
    passing_checked: int = 0
    passing_agreed: int = 0
    passing_agreement_rate: float = 0.0
    bug_checked: int = 0
    bug_aligned: int = 0
    bug_side_aligned: bool = False
    reason: str = ""
    details: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "calibrated": self.calibrated,
            "passing_checked": self.passing_checked,
            "passing_agreed": self.passing_agreed,
            "passing_agreement_rate": self.passing_agreement_rate,
            "bug_checked": self.bug_checked,
            "bug_aligned": self.bug_aligned,
            "bug_side_aligned": self.bug_side_aligned,
            "reason": self.reason,
            "details": _as_dict_list(self.details),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> CalibrationReport:
        return cls(
            calibrated=bool(data.get("calibrated", False)),
            passing_checked=int(data.get("passing_checked", 0)),
            passing_agreed=int(data.get("passing_agreed", 0)),
            passing_agreement_rate=float(data.get("passing_agreement_rate", 0.0)),
            bug_checked=int(data.get("bug_checked", 0)),
            bug_aligned=int(data.get("bug_aligned", 0)),
            bug_side_aligned=bool(data.get("bug_side_aligned", False)),
            reason=str(data.get("reason", "")),
            details=_as_dict_list(data.get("details")),
        )


@dataclass
class DisagreementCase:
    """Single-input disagreement triple `(x*, P(x*), P'(x*), M_py(x*))` for arbitration."""

    case_id: str
    input_args: list[Any]
    target_patch_branch: str
    buggy_output: str
    patched_output: str
    python_proxy_output: str | None = None
    probe_source: str = "patch_targeted"
    arbitration_verdict: ArbitrationVerdictType = "pending"
    arbitration_rationale: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "case_id": self.case_id,
            "input_args": list(self.input_args),
            "target_patch_branch": self.target_patch_branch,
            "buggy_output": self.buggy_output,
            "patched_output": self.patched_output,
            "python_proxy_output": self.python_proxy_output,
            "probe_source": self.probe_source,
            "arbitration_verdict": self.arbitration_verdict,
            "arbitration_rationale": self.arbitration_rationale,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> DisagreementCase:
        raw_proxy = data.get("python_proxy_output")
        return cls(
            case_id=str(data["case_id"]),
            input_args=list(data.get("input_args", [])),
            target_patch_branch=str(data.get("target_patch_branch", "")),
            buggy_output=str(data.get("buggy_output", "")),
            patched_output=str(data.get("patched_output", "")),
            python_proxy_output=str(raw_proxy) if raw_proxy is not None else None,
            probe_source=str(data.get("probe_source", "patch_targeted")),
            arbitration_verdict=data.get("arbitration_verdict", "pending"),
            arbitration_rationale=str(data.get("arbitration_rationale", "")),
        )


@dataclass
class FrozenSpecBundle:
    """Immutable specification bundle frozen by Agent 3 before Judge sees the candidate patch."""

    bug_id: str
    patch_id: str
    tier: TierType
    hard_specs: PrePostSpec
    witness_bank: WitnessBank
    soft_spec_candidates: list[SoftSpecCandidate] = field(default_factory=list)
    schema_version: str = "1.0"
    metadata: dict[str, Any] = field(default_factory=dict)
    calibration_report: CalibrationReport | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "bug_id": self.bug_id,
            "patch_id": self.patch_id,
            "tier": self.tier,
            "hard_specs": self.hard_specs.to_dict(),
            "witness_bank": self.witness_bank.to_dict(),
            "soft_spec_candidates": [s.to_dict() for s in self.soft_spec_candidates],
            "schema_version": self.schema_version,
            "metadata": dict(self.metadata),
            "calibration_report": (
                self.calibration_report.to_dict() if self.calibration_report else None
            ),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> FrozenSpecBundle:
        raw_cal = data.get("calibration_report")
        return cls(
            bug_id=str(data["bug_id"]),
            patch_id=str(data["patch_id"]),
            tier=data["tier"],
            hard_specs=PrePostSpec.from_dict(data["hard_specs"]),
            witness_bank=WitnessBank.from_dict(data["witness_bank"]),
            soft_spec_candidates=[
                SoftSpecCandidate.from_dict(item) for item in data.get("soft_spec_candidates", [])
            ],
            schema_version=str(data.get("schema_version", "1.0")),
            metadata=dict(data.get("metadata", {})),
            calibration_report=CalibrationReport.from_dict(raw_cal) if raw_cal else None,
        )


@dataclass
class JudgeVerdict:
    """Final patch overfitting verdict produced by Agent 4."""

    bug_id: str
    patch_id: str
    verdict: VerdictType
    failed_gates: list[str] = field(default_factory=list)
    witness_evidence: list[dict[str, Any]] = field(default_factory=list)
    lsp_evidence: list[dict[str, Any]] = field(default_factory=list)
    soft_spec_findings: list[dict[str, Any]] = field(default_factory=list)
    rationale: str = ""
    disagreement_cases: list[DisagreementCase] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "bug_id": self.bug_id,
            "patch_id": self.patch_id,
            "verdict": self.verdict,
            "failed_gates": _as_str_list(self.failed_gates),
            "witness_evidence": _as_dict_list(self.witness_evidence),
            "lsp_evidence": _as_dict_list(self.lsp_evidence),
            "soft_spec_findings": _as_dict_list(self.soft_spec_findings),
            "rationale": self.rationale,
            "disagreement_cases": [c.to_dict() for c in self.disagreement_cases],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> JudgeVerdict:
        return cls(
            bug_id=str(data["bug_id"]),
            patch_id=str(data["patch_id"]),
            verdict=data["verdict"],
            failed_gates=_as_str_list(data.get("failed_gates")),
            witness_evidence=_as_dict_list(data.get("witness_evidence")),
            lsp_evidence=_as_dict_list(data.get("lsp_evidence")),
            soft_spec_findings=_as_dict_list(data.get("soft_spec_findings")),
            rationale=str(data.get("rationale", "")),
            disagreement_cases=[
                DisagreementCase.from_dict(item) for item in data.get("disagreement_cases", [])
            ],
        )


@runtime_checkable
class TierClassifier(Protocol):
    """Classify a bug/patch context as Tier-A (standalone/weak-state) or Tier-B (stateful/coupled)."""

    def classify(self, intent_pack: IntentContextPack) -> TierType: ...


@runtime_checkable
class CrossRepStrategy(Protocol):
    """Cross-representation modeling interface: Tier-A Hypothesis, Tier-B reserved decision table."""

    tier: TierType

    def generate_candidates(
        self, intent_pack: IntentContextPack, config: dict[str, Any]
    ) -> CrossRepResult: ...


@runtime_checkable
class TestHarnessStrategy(Protocol):
    """JUnit sandbox harness strategy: Tier-A standalone unit harness, Tier-B public-entry probe."""

    tier: TierType

    def build_junit_source(
        self,
        pre_post: PrePostSpec,
        candidate_inputs: list[dict[str, Any]],
        intent_pack: IntentContextPack,
    ) -> str: ...

    def parse_execution_result(self, raw_result: dict[str, Any]) -> HarnessRunResult: ...


TestHarnessStrategy.__test__ = False  # Prevent pytest from collecting Protocol as a test class.
