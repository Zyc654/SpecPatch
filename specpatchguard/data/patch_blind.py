"""Patch-Blind diff anchor stripper and static leak verification.

Guarantees that downstream specification-generation components (Agent 1, Agent 2,
and Cross-Rep) receive only pre-patch buggy function context, failing tests, bug
reports, and pre-patch line coordinates — never candidate `+` lines, raw diffs,
or ground-truth patch labels.
"""

from __future__ import annotations

import json
import re
from typing import Any

from .pkl_loader import (
    _extract_method_name_from_text,
    attach_line_numbers,
    normalize_newlines,
    normalize_repo_path,
)
from .schemas import DiffAnchor, DiffHunk, PatchBlindTask, RawPatchRecord, SubPatchSlice


class PatchBlindError(ValueError):
    """Raised when a raw patch record cannot be stripped into valid Patch-Blind anchors."""


class PatchBlindViolationError(RuntimeError):
    """Raised when candidate `+` lines or diff artifacts leak into a `PatchBlindTask`."""


_HUNK_MARKER_RE = re.compile(r"@@ -\d+(?:,\d+)? \+\d+(?:,\d+)? @@")
_DIFF_HEADER_RE = re.compile(r"(?:^|\n)(?:diff --git |Index: |\+\+\+ [ab]/|--- [ab]/)")
_LINE_PREFIX_RE = re.compile(r"^\d+:\s?")
_TRIVIAL_ADDED_TOKENS = {"{", "}", "};", ");", "else", "else {", "try {", "finally {", "break;", "continue;", "return;"}


def strip_diff_from_function_slice(slice_content: str) -> list[str]:
    """Strip all `+` additions from a FURINA function-level diff slice and restore `-` lines."""
    if not slice_content:
        return []
    clean_lines: list[str] = []
    for line in normalize_newlines(slice_content).splitlines():
        if line.startswith(("+++ a/", "+++ b/", "+++ /dev/null", "--- a/", "--- b/", "--- /dev/null")):
            continue
        if line.startswith("+"):
            # Drop every added line (including bare '+' blank additions).
            continue
        if line.startswith("\\"):
            # Drop '\ No newline at end of file' markers.
            continue
        if line.startswith("-"):
            if line == "-":
                clean_lines.append("")
            elif line[1] == " ":
                # Undo FURINA ExpandFunctionPatch's 1-column '-' indent shift.
                clean_lines.append(" " + line[1:])
            else:
                clean_lines.append(line[1:])
        else:
            clean_lines.append(line)
    return clean_lines


def reconstruct_buggy_lines_from_hunk(hunk: DiffHunk) -> list[tuple[int, str]]:
    """Reconstruct 1-based pre-patch `(line_no, text)` pairs from a unified `DiffHunk`."""
    reconstructed: list[tuple[int, str]] = []
    cur_old = max(1, hunk.old_start)
    for line in hunk.lines:
        if line.startswith("+") or line.startswith("\\"):
            continue
        if line.startswith("-") or line.startswith(" "):
            reconstructed.append((cur_old, line[1:]))
            cur_old += 1
        elif line == "":
            reconstructed.append((cur_old, ""))
            cur_old += 1
        else:
            reconstructed.append((cur_old, line))
            cur_old += 1
    return reconstructed


def _prepare_slices(sub_patches: list[SubPatchSlice]) -> list[dict[str, Any]]:
    prepared: list[dict[str, Any]] = []
    for sub in sub_patches:
        if sub.is_fallback_diff or sub.start_line < 1:
            continue
        clean_lines = strip_diff_from_function_slice(sub.content)
        if not clean_lines:
            continue
        func_start = sub.start_line
        func_end = func_start + len(clean_lines) - 1
        buggy_code = "\n".join(clean_lines)
        prepared.append(
            {
                "file_path": normalize_repo_path(sub.file_path),
                "start_line": func_start,
                "end_line": func_end,
                "clean_lines": clean_lines,
                "buggy_code": buggy_code,
                "numbered_code": attach_line_numbers(buggy_code, func_start),
                "method_name": _extract_method_name_from_text(buggy_code),
            }
        )
    return prepared


def _match_slice_for_hunk(hunk: DiffHunk, prepared_slices: list[dict[str, Any]]) -> dict[str, Any] | None:
    target_file = normalize_repo_path(hunk.file_path)
    best: dict[str, Any] | None = None
    best_score: tuple[int, int] | None = None
    for candidate in prepared_slices:
        if target_file and candidate["file_path"] and candidate["file_path"] != target_file:
            continue
        f_start = candidate["start_line"]
        f_end = candidate["end_line"]
        if f_start <= hunk.anchor_start_line <= f_end:
            score = (0, abs(hunk.anchor_start_line - f_start))
        elif f_start - 3 <= hunk.anchor_start_line <= f_end + 3:
            dist = min(abs(hunk.anchor_start_line - f_start), abs(hunk.anchor_start_line - f_end))
            score = (1, dist)
        else:
            continue
        if best_score is None or score < best_score:
            best = candidate
            best_score = score
    return best


def _format_context_window(
    numbered_pairs: list[tuple[int, str]],
    anchor_start: int,
    anchor_end: int,
    context_radius: int,
) -> str:
    if not numbered_pairs:
        return ""
    win_start = max(1, anchor_start - context_radius)
    win_end = anchor_end + context_radius
    selected = [(ln, txt) for ln, txt in numbered_pairs if win_start <= ln <= win_end]
    if not selected:
        selected = numbered_pairs
    return "\n".join(f"{ln}: {txt}" for ln, txt in selected)


def _anchor_from_hunk(
    hunk: DiffHunk,
    prepared_slices: list[dict[str, Any]],
    context_radius: int,
) -> DiffAnchor:
    hunk_old_start = max(1, hunk.old_start)
    hunk_old_end = max(hunk_old_start, hunk.old_start + max(hunk.old_count, 1) - 1)
    matched = _match_slice_for_hunk(hunk, prepared_slices)

    if matched is not None:
        f_start = matched["start_line"]
        f_end = matched["end_line"]
        clean_lines = matched["clean_lines"]
        numbered_pairs = [(f_start + idx, line) for idx, line in enumerate(clean_lines)]
        ctx_text = _format_context_window(
            numbered_pairs,
            hunk.anchor_start_line,
            hunk.anchor_end_line,
            context_radius,
        )
        method_name = matched["method_name"] or _extract_method_name_from_text("", hunk.header)
        return DiffAnchor(
            file_path=normalize_repo_path(hunk.file_path) or matched["file_path"],
            method_name=method_name,
            anchor_start_line=hunk.anchor_start_line,
            anchor_end_line=hunk.anchor_end_line,
            hunk_old_start=hunk_old_start,
            hunk_old_end=hunk_old_end,
            kind=hunk.kind,
            function_start_line=f_start,
            function_end_line=f_end,
            buggy_function_code=matched["buggy_code"],
            numbered_buggy_function=matched["numbered_code"],
            anchor_context_lines=ctx_text,
            is_fallback_slice=False,
        )

    reconstructed = reconstruct_buggy_lines_from_hunk(hunk)
    ctx_text = _format_context_window(
        reconstructed,
        hunk.anchor_start_line,
        hunk.anchor_end_line,
        context_radius,
    )
    return DiffAnchor(
        file_path=normalize_repo_path(hunk.file_path),
        method_name=_extract_method_name_from_text("", hunk.header),
        anchor_start_line=hunk.anchor_start_line,
        anchor_end_line=hunk.anchor_end_line,
        hunk_old_start=hunk_old_start,
        hunk_old_end=hunk_old_end,
        kind=hunk.kind,
        function_start_line=None,
        function_end_line=None,
        buggy_function_code="",
        numbered_buggy_function="",
        anchor_context_lines=ctx_text,
        is_fallback_slice=True,
    )


def strip_to_patch_blind(
    record: RawPatchRecord,
    context_radius: int = 5,
    verify: bool = True,
) -> PatchBlindTask:
    """Convert a `RawPatchRecord` into a strictly Patch-Blind `PatchBlindTask`."""
    if context_radius < 0:
        raise PatchBlindError("context_radius must be >= 0")

    prepared_slices = _prepare_slices(record.sub_patches)
    anchors: list[DiffAnchor] = []

    if record.hunks:
        for hunk in record.hunks:
            anchors.append(_anchor_from_hunk(hunk, prepared_slices, context_radius))
    elif prepared_slices:
        for slc in prepared_slices:
            numbered_pairs = [
                (slc["start_line"] + idx, line) for idx, line in enumerate(slc["clean_lines"])
            ]
            anchors.append(
                DiffAnchor(
                    file_path=slc["file_path"],
                    method_name=slc["method_name"],
                    anchor_start_line=slc["start_line"],
                    anchor_end_line=slc["end_line"],
                    hunk_old_start=slc["start_line"],
                    hunk_old_end=slc["end_line"],
                    kind="modify",
                    function_start_line=slc["start_line"],
                    function_end_line=slc["end_line"],
                    buggy_function_code=slc["buggy_code"],
                    numbered_buggy_function=slc["numbered_code"],
                    anchor_context_lines=_format_context_window(
                        numbered_pairs, slc["start_line"], slc["start_line"], context_radius
                    ),
                    is_fallback_slice=False,
                )
            )
    else:
        raise PatchBlindError(f"Record {record.patch_id!r} has neither unified diff hunks nor function slices")

    affected_files: list[str] = []
    for anchor in anchors:
        if anchor.file_path and anchor.file_path not in affected_files:
            affected_files.append(anchor.file_path)

    task = PatchBlindTask(
        patch_id=record.patch_id,
        bug_id=record.bug_id,
        project=record.project,
        bug_num=record.bug_num,
        anchors=anchors,
        affected_files=affected_files,
        failing_tests=list(record.failing_tests),
        bug_report=record.bug_report,
        is_multi_hunk=len(anchors) > 1,
        is_multi_file=len(affected_files) > 1,
        has_fallback_anchor=any(anchor.is_fallback_slice for anchor in anchors),
    )

    if verify:
        assert_patch_blind_isolation(task, record)
    return task


def _collect_prepatch_corpus_lines(raw_patch: RawPatchRecord) -> set[str]:
    corpus: set[str] = set()

    def _add_text(text: str) -> None:
        for ln in normalize_newlines(text).splitlines():
            stripped = ln.strip()
            if stripped:
                corpus.add(stripped)

    for sub in raw_patch.sub_patches:
        for clean_line in strip_diff_from_function_slice(sub.content):
            stripped = clean_line.strip()
            if stripped:
                corpus.add(stripped)

    for hunk in raw_patch.hunks:
        for _, text in reconstruct_buggy_lines_from_hunk(hunk):
            stripped = text.strip()
            if stripped:
                corpus.add(stripped)
        for deleted in hunk.deleted_lines:
            stripped = deleted.strip()
            if stripped:
                corpus.add(stripped)

    for test in raw_patch.failing_tests:
        _add_text(test.source_code)
        _add_text(test.failure_message)
        _add_text(test.stack_trace)

    _add_text(raw_patch.bug_report.title)
    _add_text(raw_patch.bug_report.description)
    return corpus


def verify_patch_blind_isolation(task: PatchBlindTask, raw_patch: RawPatchRecord) -> list[str]:
    """Return a list of Patch-Blind isolation violations (empty list means clean)."""
    violations: list[str] = []
    anchors_serialized = json.dumps([a.to_dict() for a in task.anchors], ensure_ascii=False)

    if _HUNK_MARKER_RE.search(anchors_serialized):
        violations.append("Serialized DiffAnchors contain unified diff hunk header '@@ -.. +.. @@'")

    for idx, anchor in enumerate(task.anchors):
        for field_name, text in (
            ("buggy_function_code", anchor.buggy_function_code),
            ("numbered_buggy_function", anchor.numbered_buggy_function),
            ("anchor_context_lines", anchor.anchor_context_lines),
        ):
            if not text:
                continue
            if _DIFF_HEADER_RE.search(text):
                violations.append(f"Anchor[{idx}].{field_name} contains raw diff file header")

    prepatch_corpus = _collect_prepatch_corpus_lines(raw_patch)
    prepatch_corpus_joined = "\n".join(prepatch_corpus)
    anchor_lines_stripped: set[str] = set()
    anchor_raw_lines: set[str] = set()
    for anchor in task.anchors:
        for text in (anchor.buggy_function_code, anchor.numbered_buggy_function, anchor.anchor_context_lines):
            for ln in normalize_newlines(text).splitlines():
                unnumbered = _LINE_PREFIX_RE.sub("", ln, count=1)
                anchor_raw_lines.add(unnumbered)
                stripped = unnumbered.strip()
                if stripped:
                    anchor_lines_stripped.add(stripped)

    for hunk in raw_patch.hunks:
        for added in hunk.added_lines:
            stripped = added.strip()
            if not stripped or len(stripped) < 4 or stripped in _TRIVIAL_ADDED_TOKENS:
                continue
            if stripped.startswith(("//", "/*", "*")):
                continue
            if any(ln.startswith("+") and ln[1:].strip() == stripped for ln in anchor_raw_lines):
                violations.append(f"Raw '+' diff line leaked into anchor text: {stripped[:80]}")
                continue
            if stripped in prepatch_corpus or stripped in prepatch_corpus_joined:
                continue
            if stripped in anchor_lines_stripped:
                violations.append(
                    f"Patch-introduced '+' line leaked into anchor lines: {stripped[:80]}"
                )
            elif len(stripped) >= 12 and stripped in anchors_serialized:
                violations.append(
                    f"Patch-introduced '+' statement leaked into serialized anchors: {stripped[:80]}"
                )

    return violations


def assert_patch_blind_isolation(task: PatchBlindTask, raw_patch: RawPatchRecord) -> None:
    """Assert zero Patch-Blind isolation violations; raise `PatchBlindViolationError` otherwise."""
    violations = verify_patch_blind_isolation(task, raw_patch)
    if violations:
        raise PatchBlindViolationError("; ".join(violations))
