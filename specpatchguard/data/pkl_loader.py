"""Preprocessed `.pkl` and JSON data loader with official Defects4J selective loading.

Reuses `load_pickle` (`PlainUnpickler`) and `read_json` from `specpatchguard.core.config_loader`
and adapts diff/test/patch parsing logic from `FURINA`'s `PatchHelper.py` and `main.py`.
Supports input-driven selective loading from directories, manifest files (`.txt`/`.csv`/`.json`),
`.diff`/`.patch` files, or explicit official Defects4J project/bug/patch/method selectors, plus
optional online Docker Defects4J fallback hydration when items are absent from `.pkl`/JSON.
"""

from __future__ import annotations

import csv
from pathlib import Path
import re
from typing import Any, Iterable

from ..analysis.ast_context import _parse_method_header, _strip_comments_and_strings
from ..core.config_loader import ConfigError, Settings, load_pickle, path_key, read_json
from ..core.doctor import CommandRunner
from .d4j_fallback import (
    D4jFallbackError,
    D4jFallbackProvider,
    DockerD4jFallbackProvider,
    decode_bytes_safely,
)
from .schemas import (
    AnchorKind,
    BugReport,
    DatasetLoadResult,
    DatasetSpec,
    DiffHunk,
    FailingTestRecord,
    PatchTargetSelector,
    RawPatchRecord,
    SubPatchSlice,
)


class DataLoadError(ValueError):
    """Raised when preprocessed `.pkl`, bug report JSON, dataset input, or diff metadata is invalid."""


_OFFICIAL_D4J_PROJECTS: dict[str, str] = {
    "chart": "Chart",
    "cli": "Cli",
    "closure": "Closure",
    "codec": "Codec",
    "collections": "Collections",
    "compress": "Compress",
    "csv": "Csv",
    "gson": "Gson",
    "jacksoncore": "JacksonCore",
    "jacksondatabind": "JacksonDatabind",
    "jacksonxml": "JacksonXml",
    "jsoup": "Jsoup",
    "jxpath": "JxPath",
    "lang": "Lang",
    "math": "Math",
    "mockito": "Mockito",
    "time": "Time",
}

_IGNORE_SCAN_DIRS = frozenset(
    {
        ".git",
        ".vscode",
        "__pycache__",
        ".pytest_cache",
        "_shared_lib",
        "node_modules",
        "target",
        "build",
        "src",
        "source",
        "test",
        "tests",
        "lib",
    }
)

_HUNK_HEADER_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@(.*)$")
_D4J_SEGMENT_RE = re.compile(r"^([A-Za-z][A-Za-z0-9]*)[_-](\d+)[bf]?$")
_D4J_BUG_ID_RE = re.compile(r"^([A-Za-z][A-Za-z0-9]*)[_/-](\d+)[bf]?$")
_FURINA_FILENAME_RE = re.compile(
    r"^[^-]+-([A-Za-z][A-Za-z0-9]*)-(\d+)-([A-Za-z0-9_.+]+)$"
)
_JAVA_METHOD_RE = re.compile(
    r"\b(?:public|protected|private|static|final|abstract|synchronized|native|default)?\s*"
    r"(?:<[^>{};]+>\s+)?(?:[\w$.\[\]<>]+\s+)?([A-Za-z_$][A-Za-z0-9_$]*)\s*\("
)
_BARE_PROJECT_RE = re.compile(r"^[A-Za-z]+$")
_NON_METHOD_KEYWORDS = frozenset({"if", "for", "while", "switch", "catch", "synchronized", "else"})
_SENTINEL_EMPTY = {"", "none", "null"}


def canonicalize_project_name(project: str) -> str:
    """Normalize a Defects4J project name to official casing while allowing custom projects."""
    cleaned = str(project).strip()
    if not cleaned:
        raise DataLoadError("Project name cannot be empty")
    return _OFFICIAL_D4J_PROJECTS.get(cleaned.lower(), cleaned)


def parse_d4j_bug_id(raw: str) -> tuple[str, int, str]:
    """Parse official Defects4J bug identifiers (`Lang-12`, `Lang_12`, `Lang/12`, `Lang-12b`)."""
    if not isinstance(raw, str) or not raw.strip():
        raise DataLoadError("Bug identifier must be a non-empty string")
    cleaned = raw.strip().replace("\\", "/").strip("/")

    if "/" in cleaned:
        parts = [p for p in cleaned.split("/") if p]
        if len(parts) == 2:
            left, right = parts
            if right.isdigit() and _BARE_PROJECT_RE.fullmatch(left):
                proj = canonicalize_project_name(left)
                num = int(right)
                return proj, num, f"{proj}-{num}"
            m_right = _D4J_BUG_ID_RE.match(right)
            if m_right and m_right.group(1).lower() == left.lower():
                proj = canonicalize_project_name(m_right.group(1))
                num = int(m_right.group(2))
                return proj, num, f"{proj}-{num}"

    match = _D4J_BUG_ID_RE.match(cleaned)
    if not match:
        raise DataLoadError(f"Invalid Defects4J bug identifier: {raw!r}")
    proj = canonicalize_project_name(match.group(1))
    num = int(match.group(2))
    return proj, num, f"{proj}-{num}"


def normalize_newlines(text: str) -> str:
    """Normalize CRLF and bare CR line endings to LF."""
    return text.replace("\r\n", "\n").replace("\r", "\n")


def normalize_repo_path(raw_path: str) -> str:
    """Normalize diff/repository paths without entering infinite loops on non-src prefixes."""
    cleaned = str(raw_path).split("\t", 1)[0].strip()
    if not cleaned:
        return ""
    if cleaned == "/dev/null":
        return "/dev/null"
    cleaned = cleaned.replace("\\", "/")
    while cleaned.startswith("./"):
        cleaned = cleaned[2:]
    if cleaned.startswith(("a/", "b/")):
        cleaned = cleaned[2:]
    cleaned = cleaned.lstrip("/")
    return path_key(cleaned)


def attach_line_numbers(text: str, start_line: int = 1) -> str:
    """Prefix each line with 1-based `N: ` formatting matching `analysis.protocols`."""
    if not text:
        return ""
    lines = normalize_newlines(text).splitlines()
    if not lines:
        return ""
    return "\n".join(f"{start_line + idx}: {line}" for idx, line in enumerate(lines))


def _clean_optional_text(value: Any) -> str:
    if value is None:
        return ""
    text = normalize_newlines(str(value)).strip()
    if text.lower() in _SENTINEL_EMPTY:
        return ""
    return text


def parse_patch_metadata(patch_path: str) -> dict[str, Any]:
    """Parse canonical project, bug_num, bug_id, patch_name, tool, and optional label from a patch path."""
    if not isinstance(patch_path, str) or not patch_path.strip():
        raise DataLoadError("Patch path must be a non-empty string")

    normalized = path_key(patch_path.strip())
    parts = [part for part in normalized.split("/") if part]
    if not parts:
        raise DataLoadError(f"Invalid patch path: {patch_path!r}")

    label: int | None = None
    lower_parts = [part.lower() for part in parts]
    if "correct" in lower_parts:
        label = 1
    elif "overfitting" in lower_parts or "wrong" in lower_parts:
        label = 0

    patch_name = parts[-1]
    stem = Path(patch_name).stem
    furina_match = _FURINA_FILENAME_RE.match(stem)
    if furina_match:
        project = canonicalize_project_name(furina_match.group(1))
        bug_num = int(furina_match.group(2))
        tool = furina_match.group(3)
        return {
            "patch_id": normalized,
            "patch_name": patch_name,
            "project": project,
            "bug_num": bug_num,
            "bug_id": f"{project}-{bug_num}",
            "tool": tool,
            "label": label,
        }

    # Support official `<Project>/<Num>/<patch>.diff`.
    if len(parts) >= 3 and parts[-2].isdigit() and _BARE_PROJECT_RE.match(parts[-3]):
        project = canonicalize_project_name(parts[-3])
        bug_num = int(parts[-2])
        return {
            "patch_id": normalized,
            "patch_name": patch_name,
            "project": project,
            "bug_num": bug_num,
            "bug_id": f"{project}-{bug_num}",
            "tool": "unknown",
            "label": label,
        }

    # Prefer parent directory segments from right to left (official project names first)
    # so filenames like `patch_1.diff` inside `Lang_12/` never shadow `Lang_12`.
    dir_match: re.Match[str] | None = None
    for segment in reversed(parts[:-1]):
        m = _D4J_SEGMENT_RE.match(segment)
        if m and m.group(1).lower() in _OFFICIAL_D4J_PROJECTS:
            dir_match = m
            break
    if dir_match is None:
        for segment in reversed(parts[:-1]):
            m = _D4J_SEGMENT_RE.match(segment)
            if m:
                dir_match = m
                break
    if dir_match is None:
        dir_match = _D4J_SEGMENT_RE.match(stem)
    if dir_match is None:
        raise DataLoadError(f"Cannot extract project and bug id from patch path: {patch_path!r}")

    project = canonicalize_project_name(dir_match.group(1))
    bug_num = int(dir_match.group(2))
    tool = "Multi-Hunk-APR" if "multi-hunk-apr" in normalized.lower() else "unknown"
    return {
        "patch_id": normalized,
        "patch_name": patch_name,
        "project": project,
        "bug_num": bug_num,
        "bug_id": f"{project}-{bug_num}",
        "tool": tool,
        "label": label,
    }


def parse_target_selector(raw: str | dict[str, Any] | PatchTargetSelector) -> PatchTargetSelector:
    """Parse a flexible official Defects4J bug/patch/file/method target selector."""
    if isinstance(raw, PatchTargetSelector):
        _, _, canonical_bug = parse_d4j_bug_id(raw.bug_id)
        return PatchTargetSelector(
            bug_id=canonical_bug,
            patch_name=raw.patch_name.strip(),
            file_path=normalize_repo_path(raw.file_path) if raw.file_path else "",
            method_name=raw.method_name.strip(),
            local_diff_path=raw.local_diff_path.strip(),
        )

    if isinstance(raw, dict):
        if raw.get("bug_id"):
            _, _, canonical_bug = parse_d4j_bug_id(str(raw["bug_id"]))
        elif raw.get("project") and raw.get("bug_num") is not None:
            proj = canonicalize_project_name(str(raw["project"]))
            canonical_bug = f"{proj}-{int(raw['bug_num'])}"
        else:
            raise DataLoadError(f"Selector dict must define 'bug_id' or ('project', 'bug_num'): {raw!r}")
        return PatchTargetSelector(
            bug_id=canonical_bug,
            patch_name=str(raw.get("patch_name") or raw.get("patch") or "").strip(),
            file_path=normalize_repo_path(str(raw.get("file_path") or raw.get("file") or "")),
            method_name=str(raw.get("method_name") or raw.get("method") or "").strip(),
            local_diff_path=str(raw.get("local_diff_path") or "").strip(),
        )

    if not isinstance(raw, str) or not raw.strip():
        raise DataLoadError(f"Invalid target selector: {raw!r}")
    text = raw.strip()

    if "::" in text:
        segs = [seg.strip() for seg in text.split("::") if seg.strip()]
        if len(segs) < 2 or len(segs) > 3:
            raise DataLoadError(f"Invalid '::' target selector syntax: {raw!r}")
        base_sel = parse_target_selector(segs[0])
        if len(segs) == 2:
            second = segs[1]
            if second.endswith((".diff", ".patch")):
                return PatchTargetSelector(
                    bug_id=base_sel.bug_id,
                    patch_name=Path(second).name,
                    file_path=base_sel.file_path,
                    method_name=base_sel.method_name,
                    local_diff_path=base_sel.local_diff_path,
                )
            if second.endswith(".java") or "/" in second or "\\" in second:
                return PatchTargetSelector(
                    bug_id=base_sel.bug_id,
                    patch_name=base_sel.patch_name,
                    file_path=normalize_repo_path(second),
                    method_name=base_sel.method_name,
                    local_diff_path=base_sel.local_diff_path,
                )
            return PatchTargetSelector(
                bug_id=base_sel.bug_id,
                patch_name=base_sel.patch_name,
                file_path=base_sel.file_path,
                method_name=second,
                local_diff_path=base_sel.local_diff_path,
            )
        return PatchTargetSelector(
            bug_id=base_sel.bug_id,
            patch_name=base_sel.patch_name,
            file_path=normalize_repo_path(segs[1]),
            method_name=segs[2],
            local_diff_path=base_sel.local_diff_path,
        )

    normalized = text.replace("\\", "/")
    if normalized.endswith((".diff", ".patch")):
        meta = parse_patch_metadata(normalized)
        local_path = str(Path(text).resolve()) if Path(text).is_file() else ""
        return PatchTargetSelector(
            bug_id=meta["bug_id"],
            patch_name=meta["patch_name"],
            local_diff_path=local_path,
        )

    _, _, canonical_bug = parse_d4j_bug_id(normalized)
    return PatchTargetSelector(bug_id=canonical_bug)


def _add_unique(items: list[str], value: str) -> None:
    if value and value not in items:
        items.append(value)


def _add_unique_target(items: list[PatchTargetSelector], target: PatchTargetSelector) -> None:
    if target not in items:
        items.append(target)


def _scan_bug_dir_for_diffs(bug_dir: Path, canonical_bug: str) -> list[PatchTargetSelector]:
    targets: list[PatchTargetSelector] = []
    for child in sorted(bug_dir.iterdir(), key=lambda p: p.name):
        if child.is_file() and child.suffix.lower() in {".diff", ".patch"}:
            targets.append(
                PatchTargetSelector(
                    bug_id=canonical_bug,
                    patch_name=child.name,
                    local_diff_path=str(child.resolve()),
                )
            )
    return targets


def _has_direct_bug_children(directory: Path) -> bool:
    """Return True if `directory` immediately contains numeric or `<dir>_<num>` bug subdirs."""
    prefix = directory.name.lower()
    for child in directory.iterdir():
        if not child.is_dir() or child.name in _IGNORE_SCAN_DIRS:
            continue
        if child.name.isdigit():
            return True
        m = _D4J_SEGMENT_RE.match(child.name)
        if m and m.group(1).lower() == prefix:
            return True
    return False


def _scan_directory_tree(root: Path, bug_ids: list[str], targets: list[PatchTargetSelector]) -> None:
    """Walk a user dataset directory recognizing official Defects4J layouts."""
    def _try_bug_dir(directory: Path, parent_project: str | None = None) -> bool:
        name = directory.name
        if parent_project is not None and name.isdigit():
            proj = canonicalize_project_name(parent_project)
            canonical_bug = f"{proj}-{int(name)}"
        else:
            m = _D4J_SEGMENT_RE.match(name)
            if not m:
                return False
            proj = canonicalize_project_name(m.group(1))
            canonical_bug = f"{proj}-{int(m.group(2))}"

        diff_targets = _scan_bug_dir_for_diffs(directory, canonical_bug)
        if diff_targets:
            for t in diff_targets:
                _add_unique_target(targets, t)
        else:
            _add_unique(bug_ids, canonical_bug)
        return True

    # Check if root itself is a single bug directory (e.g. `.../Closure/101` or `.../Lang_12`).
    parent_proj = root.parent.name if root.parent.name.lower() in _OFFICIAL_D4J_PROJECTS else None
    if _try_bug_dir(root, parent_project=parent_proj):
        return

    def _walk(current: Path, current_project: str | None, depth: int) -> None:
        if depth > 4:
            return
        for entry in sorted(current.iterdir(), key=lambda p: p.name):
            if entry.is_file() and entry.suffix.lower() in {".diff", ".patch"}:
                try:
                    sel = parse_target_selector(str(entry))
                    _add_unique_target(targets, sel)
                except DataLoadError:
                    continue
                continue
            if not entry.is_dir() or entry.name in _IGNORE_SCAN_DIRS:
                continue
            if _try_bug_dir(entry, parent_project=current_project):
                continue
            if entry.name.lower() in _OFFICIAL_D4J_PROJECTS:
                _walk(entry, _OFFICIAL_D4J_PROJECTS[entry.name.lower()], depth + 1)
            elif _BARE_PROJECT_RE.match(entry.name) and _has_direct_bug_children(entry):
                _walk(entry, entry.name, depth + 1)
            else:
                # Wrapper directories (e.g. `subset/`, `d4j_patch/`, `correct/`) never act as project names.
                _walk(entry, None, depth + 1)

    root_project: str | None = None
    if root.name.lower() in _OFFICIAL_D4J_PROJECTS:
        root_project = _OFFICIAL_D4J_PROJECTS[root.name.lower()]
    elif _BARE_PROJECT_RE.match(root.name) and _has_direct_bug_children(root):
        root_project = root.name
    _walk(root, root_project, 0)


def _ingest_selector_item(
    item: str | dict[str, Any] | PatchTargetSelector,
    projects: list[str],
    bug_ids: list[str],
    targets: list[PatchTargetSelector],
) -> None:
    if isinstance(item, str):
        stripped = item.strip()
        if not stripped:
            return
        if _BARE_PROJECT_RE.match(stripped):
            _add_unique(projects, canonicalize_project_name(stripped))
            return
    sel = parse_target_selector(item)
    if sel.patch_name or sel.file_path or sel.method_name or sel.local_diff_path:
        _add_unique_target(targets, sel)
    else:
        _add_unique(bug_ids, sel.bug_id)


def _require_json_list(payload: dict[str, Any], keys: tuple[str, ...], path: Path) -> list[Any]:
    for key in keys:
        if key in payload:
            val = payload[key]
            if not isinstance(val, list):
                raise DataLoadError(f"JSON manifest field '{key}' must be a list in {path}")
            return val
    return []


def _load_spec_from_file(path: Path) -> DatasetSpec:
    suffix = path.suffix.lower()
    projects: list[str] = []
    bug_ids: list[str] = []
    targets: list[PatchTargetSelector] = []
    file_paths: list[str] = []
    method_names: list[str] = []

    if suffix in {".diff", ".patch"}:
        sel = parse_target_selector(str(path))
        _add_unique_target(targets, sel)
        return DatasetSpec(
            source_label="diff_file",
            source_path=str(path.resolve()),
            targets=targets,
        )

    if suffix == ".json":
        payload = read_json(path)
        if isinstance(payload, list):
            for entry in payload:
                _ingest_selector_item(entry, projects, bug_ids, targets)
        elif isinstance(payload, dict):
            for proj in _require_json_list(payload, ("projects",), path):
                _add_unique(projects, canonicalize_project_name(str(proj)))
            for bug in _require_json_list(payload, ("bug_ids", "bugs"), path):
                _ingest_selector_item(bug, projects, bug_ids, targets)
            for tgt in _require_json_list(payload, ("targets", "patches"), path):
                _ingest_selector_item(tgt, projects, bug_ids, targets)
            for fpath in _require_json_list(payload, ("file_paths", "files"), path):
                _add_unique(file_paths, normalize_repo_path(str(fpath)))
            for mname in _require_json_list(payload, ("method_names", "methods"), path):
                _add_unique(method_names, str(mname).strip())
        else:
            raise DataLoadError(f"Dataset JSON must be a list or object: {path}")
        spec = DatasetSpec(
            source_label="json_manifest",
            source_path=str(path.resolve()),
            projects=projects,
            bug_ids=bug_ids,
            targets=targets,
            file_paths=file_paths,
            method_names=method_names,
        )
        if spec.is_unfiltered():
            raise DataLoadError(f"JSON manifest contains no dataset selectors: {path}")
        return spec

    if suffix == ".csv":
        text = decode_bytes_safely(path.read_bytes())
        rows = [row for row in csv.reader(text.splitlines()) if row and any(c.strip() for c in row)]
        if not rows:
            raise DataLoadError(f"Empty CSV dataset file: {path}")
        header = [col.strip().lower() for col in rows[0]]
        known_cols = {
            "bug_id",
            "bug",
            "project",
            "bug_num",
            "id",
            "patch",
            "patch_name",
            "file",
            "file_path",
            "method",
            "method_name",
        }
        if any(col in known_cols for col in header):
            dict_reader = csv.DictReader(text.splitlines())
            for row in dict_reader:
                cleaned = {k.strip().lower(): (v or "").strip() for k, v in row.items() if k}
                if cleaned.get("bug_id") or cleaned.get("bug"):
                    sel_dict: dict[str, Any] = {
                        "bug_id": cleaned.get("bug_id") or cleaned.get("bug"),
                        "patch_name": cleaned.get("patch_name") or cleaned.get("patch") or "",
                        "file_path": cleaned.get("file_path") or cleaned.get("file") or "",
                        "method_name": cleaned.get("method_name") or cleaned.get("method") or "",
                    }
                    _ingest_selector_item(sel_dict, projects, bug_ids, targets)
                elif cleaned.get("project") and (cleaned.get("bug_num") or cleaned.get("id")):
                    sel_dict = {
                        "project": cleaned["project"],
                        "bug_num": cleaned.get("bug_num") or cleaned.get("id"),
                        "patch_name": cleaned.get("patch_name") or cleaned.get("patch") or "",
                        "file_path": cleaned.get("file_path") or cleaned.get("file") or "",
                        "method_name": cleaned.get("method_name") or cleaned.get("method") or "",
                    }
                    _ingest_selector_item(sel_dict, projects, bug_ids, targets)
                elif cleaned.get("project"):
                    _add_unique(projects, canonicalize_project_name(cleaned["project"]))
        else:
            for row in rows:
                _ingest_selector_item(row[0].strip(), projects, bug_ids, targets)
        spec = DatasetSpec(
            source_label="csv_manifest",
            source_path=str(path.resolve()),
            projects=projects,
            bug_ids=bug_ids,
            targets=targets,
        )
        if spec.is_unfiltered():
            raise DataLoadError(f"CSV manifest contains no dataset selectors: {path}")
        return spec

    # Plain-text manifest (.txt, .list, etc.): one selector or project per line.
    for raw_line in decode_bytes_safely(path.read_bytes()).splitlines():
        line = raw_line.split("#", 1)[0].strip()
        if line:
            _ingest_selector_item(line, projects, bug_ids, targets)
    spec = DatasetSpec(
        source_label="text_manifest",
        source_path=str(path.resolve()),
        projects=projects,
        bug_ids=bug_ids,
        targets=targets,
    )
    if spec.is_unfiltered():
        raise DataLoadError(f"Text manifest contains no dataset selectors: {path}")
    return spec


def discover_dataset_spec(
    source: str | Path | DatasetSpec | Iterable[str | dict[str, Any] | PatchTargetSelector] | None,
) -> DatasetSpec:
    """Build a `DatasetSpec` from an actual user dataset path, manifest file, or selector list."""
    if source is None:
        return DatasetSpec(source_label="all")
    if isinstance(source, DatasetSpec):
        return source

    if isinstance(source, (str, Path)):
        raw_str = str(source).strip()
        if not raw_str:
            raise DataLoadError("Dataset source string cannot be empty")
        candidate_path = Path(raw_str)
        if candidate_path.is_dir():
            bug_ids: list[str] = []
            targets: list[PatchTargetSelector] = []
            _scan_directory_tree(candidate_path, bug_ids, targets)
            if not bug_ids and not targets:
                raise DataLoadError(
                    f"No Defects4J projects, bug directories, or patch files found in: {candidate_path}"
                )
            return DatasetSpec(
                source_label="directory",
                source_path=str(candidate_path.resolve()),
                bug_ids=bug_ids,
                targets=targets,
            )
        if candidate_path.is_file():
            return _load_spec_from_file(candidate_path)

        explicit_fs_path = (
            (len(raw_str) >= 2 and raw_str[1] == ":")
            or raw_str.startswith(("/", "\\", "./", "../", ".\\", "..\\"))
        )
        if raw_str.endswith((".json", ".csv", ".txt", ".list")) or (
            explicit_fs_path and raw_str.endswith((".diff", ".patch"))
        ):
            raise DataLoadError(f"Dataset path not found: {candidate_path}")

        projects: list[str] = []
        bug_ids = []
        targets = []
        _ingest_selector_item(raw_str, projects, bug_ids, targets)
        return DatasetSpec(
            source_label="explicit",
            projects=projects,
            bug_ids=bug_ids,
            targets=targets,
        )

    if isinstance(source, Iterable):
        projects = []
        bug_ids = []
        targets = []
        for item in source:
            _ingest_selector_item(item, projects, bug_ids, targets)
        return DatasetSpec(
            source_label="explicit_list",
            projects=projects,
            bug_ids=bug_ids,
            targets=targets,
        )

    raise DataLoadError(f"Unsupported dataset source type: {type(source).__name__}")


def _build_diff_hunk(
    file_path: str,
    old_start: int,
    old_count: int,
    new_start: int,
    new_count: int,
    header: str,
    hunk_lines: list[str],
) -> DiffHunk:
    added_lines: list[str] = []
    deleted_lines: list[str] = []
    mod_old_positions: list[int] = []
    add_insert_positions: list[int] = []

    cur_old = old_start
    for line in hunk_lines:
        if line.startswith("\\"):
            continue
        if line.startswith("+"):
            added_lines.append(line[1:])
            add_insert_positions.append(max(1, cur_old))
        elif line.startswith("-"):
            deleted_lines.append(line[1:])
            mod_old_positions.append(max(1, cur_old))
            cur_old += 1
        else:
            cur_old += 1

    if added_lines and deleted_lines:
        kind: AnchorKind = "modify"
        anchor_start = min(mod_old_positions)
        anchor_end = max(mod_old_positions)
    elif deleted_lines:
        kind = "delete_only"
        anchor_start = min(mod_old_positions)
        anchor_end = max(mod_old_positions)
    elif added_lines:
        kind = "add_only"
        max_old = max(1, old_start + max(old_count, 1) - 1) if old_count > 0 else max(1, old_start)
        min_old = max(1, old_start)
        clamped = [min(max(pos, min_old), max_old) for pos in add_insert_positions]
        anchor_start = min(clamped)
        anchor_end = max(clamped)
    else:
        kind = "context_only"
        anchor_start = max(1, old_start)
        anchor_end = max(anchor_start, old_start + max(old_count, 1) - 1)

    return DiffHunk(
        file_path=file_path,
        old_start=old_start,
        old_count=old_count,
        new_start=new_start,
        new_count=new_count,
        header=header,
        lines=list(hunk_lines),
        added_lines=added_lines,
        deleted_lines=deleted_lines,
        anchor_start_line=anchor_start,
        anchor_end_line=anchor_end,
        kind=kind,
    )


def parse_unified_diff(raw_diff: str) -> list[DiffHunk]:
    """Parse a unified diff (multi-file / multi-hunk) into structured `DiffHunk` objects."""
    if not raw_diff:
        return []

    lines = normalize_newlines(raw_diff).splitlines()
    hunks: list[DiffHunk] = []
    old_file = ""
    new_file = ""
    idx = 0
    total = len(lines)

    while idx < total:
        line = lines[idx]
        if line.startswith("diff ") or line.startswith("Index:"):
            old_file = ""
            new_file = ""
            idx += 1
            continue
        if line.startswith("--- "):
            old_file = normalize_repo_path(line[4:])
            idx += 1
            continue
        if line.startswith("+++ "):
            new_file = normalize_repo_path(line[4:])
            idx += 1
            continue

        hunk_match = _HUNK_HEADER_RE.match(line)
        if not hunk_match:
            idx += 1
            continue

        old_start = int(hunk_match.group(1))
        old_count = int(hunk_match.group(2)) if hunk_match.group(2) is not None else 1
        new_start = int(hunk_match.group(3))
        new_count = int(hunk_match.group(4)) if hunk_match.group(4) is not None else 1
        header = hunk_match.group(5).strip()

        if old_file and old_file != "/dev/null":
            target_file = old_file
        elif new_file and new_file != "/dev/null":
            target_file = new_file
        else:
            target_file = ""

        idx += 1
        hunk_lines: list[str] = []
        old_seen = 0
        new_seen = 0

        while idx < total:
            body_line = lines[idx]
            hunk_complete = old_seen >= old_count and new_seen >= new_count
            if _HUNK_HEADER_RE.match(body_line) or body_line.startswith("diff ") or body_line.startswith("Index:"):
                break
            if hunk_complete and (body_line.startswith("--- ") or body_line.startswith("+++ ")):
                break

            if body_line.startswith("\\"):
                hunk_lines.append(body_line)
                idx += 1
                continue
            if body_line.startswith("+"):
                hunk_lines.append(body_line)
                new_seen += 1
                idx += 1
                continue
            if body_line.startswith("-"):
                hunk_lines.append(body_line)
                old_seen += 1
                idx += 1
                continue
            if body_line.startswith(" ") or body_line == "":
                hunk_lines.append(body_line)
                old_seen += 1
                new_seen += 1
                idx += 1
                continue
            if hunk_complete:
                break
            hunk_lines.append(body_line)
            old_seen += 1
            new_seen += 1
            idx += 1

        hunks.append(
            _build_diff_hunk(
                file_path=target_file,
                old_start=old_start,
                old_count=old_count,
                new_start=new_start,
                new_count=new_count,
                header=header,
                hunk_lines=hunk_lines,
            )
        )

    return hunks


def normalize_bug_report(bug_id: str, raw_entry: Any) -> BugReport:
    """Normalize a bug report entry, treating `None`/`'None'`/`''` safely as empty."""
    if raw_entry is None:
        return BugReport(bug_id=bug_id, title="", description="", available=False)

    title = ""
    description = ""
    if isinstance(raw_entry, dict):
        title = _clean_optional_text(raw_entry.get("title"))
        description = _clean_optional_text(raw_entry.get("description"))
    elif isinstance(raw_entry, (list, tuple)):
        if len(raw_entry) >= 1:
            title = _clean_optional_text(raw_entry[0])
        if len(raw_entry) >= 2:
            description = _clean_optional_text(raw_entry[1])
    else:
        raise DataLoadError(f"Unsupported bug_report entry type for {bug_id}: {type(raw_entry).__name__}")

    return BugReport(
        bug_id=bug_id,
        title=title,
        description=description,
        available=bool(title or description),
    )


def _extract_method_name_from_text(text: str, fallback_header: str = "") -> str:
    """Extract Java method/constructor name using comment/string masking and multiline headers."""
    if text and text.strip():
        masked, _ = _strip_comments_and_strings(normalize_newlines(text))
        paren_depth = 0
        bracket_depth = 0
        header = ""
        for idx, ch in enumerate(masked):
            if ch == "(":
                paren_depth += 1
            elif ch == ")" and paren_depth > 0:
                paren_depth -= 1
            elif ch == "[":
                bracket_depth += 1
            elif ch == "]" and bracket_depth > 0:
                bracket_depth -= 1
            elif paren_depth == 0 and bracket_depth == 0 and ch in {"{", ";"}:
                header = " ".join(masked[:idx].split())
                break
        if header:
            parsed = _parse_method_header(header)
            if parsed:
                _, name, _, _ = parsed
                if name not in _NON_METHOD_KEYWORDS:
                    return name
        collapsed = " ".join(masked.split())
        match = _JAVA_METHOD_RE.search(collapsed)
        if match and match.group(1) not in _NON_METHOD_KEYWORDS:
            return match.group(1)
    if fallback_header:
        match = _JAVA_METHOD_RE.search(fallback_header)
        if match and match.group(1) not in _NON_METHOD_KEYWORDS:
            return match.group(1)
    return ""


def _extract_failed_line_span(labeled_func: str, start_line: int) -> tuple[int | None, int | None]:
    if not labeled_func:
        return None, None
    fail_start_idx: int | None = None
    fail_end_idx: int | None = None
    for idx, raw_line in enumerate(normalize_newlines(labeled_func).splitlines()):
        stripped = raw_line.strip()
        if fail_start_idx is None and "<Failed>" in stripped:
            fail_start_idx = idx
        if "</Failed>" in stripped:
            fail_end_idx = idx
            if fail_start_idx is not None:
                break
    if fail_start_idx is None:
        return None, None
    if fail_end_idx is None:
        fail_end_idx = fail_start_idx
    return start_line + fail_start_idx, start_line + fail_end_idx


def parse_failing_tests(raw_tests: Any) -> list[FailingTestRecord]:
    """Parse failing test entries from `labeled_d4j20_enhance_info.pkl`."""
    if raw_tests is None:
        return []
    if not isinstance(raw_tests, list):
        raise DataLoadError(f"Failing tests entry must be a list, got {type(raw_tests).__name__}")

    records: list[FailingTestRecord] = []
    for idx, item in enumerate(raw_tests):
        if not isinstance(item, (list, tuple)) or len(item) < 5:
            raise DataLoadError(f"Malformed failing test entry at index {idx}")
        source_code = normalize_newlines(str(item[0] or ""))
        labeled_func = normalize_newlines(str(item[1] or ""))
        try:
            start_line = int(item[2])
        except (TypeError, ValueError) as exc:
            raise DataLoadError(f"Invalid test start_line at index {idx}: {item[2]!r}") from exc
        test_file = normalize_repo_path(str(item[3] or ""))
        failure_message = normalize_newlines(str(item[4] or "")).strip()
        stack_trace = (
            normalize_newlines(str(item[5] or "")).strip()
            if len(item) > 5 and item[5] is not None
            else ""
        )

        failed_start, failed_end = _extract_failed_line_span(labeled_func, start_line)
        records.append(
            FailingTestRecord(
                test_file=test_file,
                test_method_name=_extract_method_name_from_text(source_code),
                start_line=start_line,
                source_code=source_code,
                numbered_source=attach_line_numbers(source_code, start_line),
                failed_line_start=failed_start,
                failed_line_end=failed_end,
                failure_message=failure_message,
                stack_trace=stack_trace,
            )
        )
    return records


def _record_file_matches(record: RawPatchRecord, wanted_file: str) -> bool:
    norm_wanted = normalize_repo_path(wanted_file)
    if not norm_wanted:
        return True
    candidates = [h.file_path for h in record.hunks if h.file_path] + [
        s.file_path for s in record.sub_patches if s.file_path
    ]
    for cand in candidates:
        norm_cand = normalize_repo_path(cand)
        if norm_cand == norm_wanted or norm_cand.endswith("/" + norm_wanted):
            return True
    return False


def _record_methods(record: RawPatchRecord) -> list[str]:
    names: list[str] = []
    for sub in record.sub_patches:
        if not sub.is_fallback_diff and sub.content:
            pre_lines = [ln for ln in normalize_newlines(sub.content).splitlines() if not ln.startswith("+")]
            mname = _extract_method_name_from_text("\n".join(pre_lines))
            if mname and mname not in names:
                names.append(mname)
    for hunk in record.hunks:
        if hunk.header:
            mname = _extract_method_name_from_text("", hunk.header)
            if mname and mname not in names:
                names.append(mname)
    return names


class FurinaDataLoader:
    """Lazy, validated, input-driven selective loader with optional Docker D4J fallback."""

    def __init__(
        self,
        patch_info_pkl: str | Path | None = None,
        test_info_pkl: str | Path | None = None,
        bug_report_json: str | Path | None = None,
        manifest_json: str | Path | None = None,
        d4j_fallback: D4jFallbackProvider | None = None,
    ) -> None:
        self.patch_info_pkl = Path(patch_info_pkl) if patch_info_pkl is not None else None
        self.test_info_pkl = Path(test_info_pkl) if test_info_pkl is not None else None
        self.bug_report_json = Path(bug_report_json) if bug_report_json is not None else None
        self.manifest_json = Path(manifest_json) if manifest_json is not None else None
        self.d4j_fallback = d4j_fallback

        self._raw_patches: list[Any] | None = None
        self._raw_tests: dict[str, Any] | None = None
        self._raw_bug_reports: dict[str, Any] | None = None
        self._manifest: dict[str, Any] | None = None
        self._fallback_tests_cache: dict[str, list[FailingTestRecord]] = {}
        self._fallback_reports_cache: dict[str, BugReport] = {}

    @classmethod
    def from_settings(
        cls,
        settings: Settings,
        *,
        d4j_fallback: D4jFallbackProvider | None = None,
        enable_docker_fallback: bool = False,
        container_name: str | None = None,
        run_trigger_tests: bool = False,
        runner: CommandRunner | None = None,
    ) -> FurinaDataLoader:
        """Instantiate `FurinaDataLoader` from validated `Settings`."""
        # Server paths in `settings.data` are validated and marked with SERVER_CONFIG_REQUIRED in core/config_loader.py.
        patch_pkl = settings.data.get("patch_info_pkl")
        if patch_pkl is None and not (enable_docker_fallback or d4j_fallback is not None):
            raise DataLoadError("settings.data['patch_info_pkl'] is required")
        effective_fallback = d4j_fallback
        if effective_fallback is None and enable_docker_fallback:
            effective_fallback = DockerD4jFallbackProvider.from_settings(
                settings,
                container_name=container_name,
                run_trigger_tests=run_trigger_tests,
                runner=runner,
            )
        return cls(
            patch_info_pkl=patch_pkl,
            test_info_pkl=settings.data.get("test_info_pkl"),
            bug_report_json=settings.data.get("bug_report_json"),
            manifest_json=settings.data.get("manifest_json"),
            d4j_fallback=effective_fallback,
        )

    def _ensure_patches_loaded(self) -> list[Any]:
        if self._raw_patches is None:
            if self.patch_info_pkl is None:
                self._raw_patches = []
            else:
                if not self.patch_info_pkl.is_file():
                    raise DataLoadError(f"Patch pickle not found: {self.patch_info_pkl}")
                try:
                    loaded = load_pickle(self.patch_info_pkl)
                except ConfigError as exc:
                    raise DataLoadError(str(exc)) from None
                if not isinstance(loaded, list):
                    raise DataLoadError(
                        f"patch_info_pkl root must be a list, got {type(loaded).__name__}"
                    )
                self._raw_patches = loaded
        return self._raw_patches

    def _ensure_tests_loaded(self) -> dict[str, Any]:
        if self._raw_tests is None:
            if self.test_info_pkl is None:
                self._raw_tests = {}
            else:
                if not self.test_info_pkl.is_file():
                    raise DataLoadError(f"Test info pickle not found: {self.test_info_pkl}")
                try:
                    loaded = load_pickle(self.test_info_pkl)
                except ConfigError as exc:
                    raise DataLoadError(str(exc)) from None
                if not isinstance(loaded, dict):
                    raise DataLoadError(
                        f"test_info_pkl root must be a dict, got {type(loaded).__name__}"
                    )
                self._raw_tests = loaded
        return self._raw_tests

    def _ensure_bug_reports_loaded(self) -> dict[str, Any]:
        if self._raw_bug_reports is None:
            if self.bug_report_json is None:
                self._raw_bug_reports = {}
            else:
                if not self.bug_report_json.is_file():
                    raise DataLoadError(f"Bug report JSON not found: {self.bug_report_json}")
                try:
                    loaded = read_json(self.bug_report_json)
                except ConfigError as exc:
                    raise DataLoadError(str(exc)) from None
                if not isinstance(loaded, dict):
                    raise DataLoadError(
                        f"bug_report_json root must be a dict, got {type(loaded).__name__}"
                    )
                self._raw_bug_reports = {str(k).lower(): v for k, v in loaded.items()}
        return self._raw_bug_reports

    def get_manifest(self) -> dict[str, Any]:
        if self._manifest is None:
            if self.manifest_json is None or not self.manifest_json.is_file():
                self._manifest = {}
            else:
                try:
                    loaded = read_json(self.manifest_json)
                except ConfigError as exc:
                    raise DataLoadError(str(exc)) from None
                if not isinstance(loaded, dict):
                    raise DataLoadError("manifest_json root must be a dict")
                self._manifest = loaded
        return self._manifest

    def get_bug_report(self, bug_id: str) -> BugReport:
        reports = self._ensure_bug_reports_loaded()
        report = normalize_bug_report(bug_id, reports.get(bug_id.lower()))
        if not report.available and self.d4j_fallback is not None:
            if bug_id in self._fallback_reports_cache:
                return self._fallback_reports_cache[bug_id]
            try:
                proj, bug_num, _ = parse_d4j_bug_id(bug_id)
                fetched = self.d4j_fallback.fetch_bug_report(proj, bug_num)
                self._fallback_reports_cache[bug_id] = fetched
                return fetched
            except (DataLoadError, D4jFallbackError, OSError):
                return report
        return report

    def get_failing_tests(self, bug_id: str) -> list[FailingTestRecord]:
        tests = self._ensure_tests_loaded()
        parsed = parse_failing_tests(tests.get(bug_id))
        if not parsed and self.d4j_fallback is not None:
            if bug_id in self._fallback_tests_cache:
                return list(self._fallback_tests_cache[bug_id])
            try:
                proj, bug_num, _ = parse_d4j_bug_id(bug_id)
                fetched = self.d4j_fallback.fetch_failing_tests(proj, bug_num)
                self._fallback_tests_cache[bug_id] = fetched
                return list(fetched)
            except (DataLoadError, D4jFallbackError, OSError):
                return parsed
        return parsed

    def _hydrate_with_d4j_fallback(self, record: RawPatchRecord) -> None:
        if self.d4j_fallback is None or not record.hunks:
            return
        needs_slices = not record.sub_patches or all(sp.is_fallback_diff for sp in record.sub_patches)
        if not needs_slices:
            return
        try:
            hydrated_slices = self.d4j_fallback.build_function_slices_for_hunks(
                record.project, record.bug_num, record.hunks
            )
            if hydrated_slices:
                record.sub_patches = hydrated_slices
                record.metadata["d4j_fallback_hydrated"] = True
        except (D4jFallbackError, OSError) as exc:
            errors = record.metadata.setdefault("d4j_fallback_errors", [])
            errors.append(str(exc))

    def _parse_record(self, raw_entry: Any, index: int) -> RawPatchRecord:
        if not isinstance(raw_entry, (list, tuple)) or len(raw_entry) != 2:
            raise DataLoadError(f"Malformed patch record at index {index}")
        meta_pair, raw_subs = raw_entry
        if not isinstance(meta_pair, (list, tuple)) or len(meta_pair) != 2:
            raise DataLoadError(f"Malformed patch header at index {index}")
        if not isinstance(raw_subs, list):
            raise DataLoadError(f"Malformed sub_patches list at index {index}")

        patch_path = str(meta_pair[0] or "")
        raw_diff = normalize_newlines(str(meta_pair[1] or ""))
        meta = parse_patch_metadata(patch_path)
        bug_id = meta["bug_id"]

        sub_patches: list[SubPatchSlice] = []
        for sub_idx, sub in enumerate(raw_subs):
            if not isinstance(sub, (list, tuple)) or len(sub) < 3:
                raise DataLoadError(f"Malformed sub_patch at record {index}, slice {sub_idx}")
            file_path = normalize_repo_path(str(sub[0] or ""))
            try:
                start_line = int(sub[1])
            except (TypeError, ValueError) as exc:
                raise DataLoadError(
                    f"Invalid sub_patch start_line at record {index}, slice {sub_idx}: {sub[1]!r}"
                ) from exc
            content = normalize_newlines(str(sub[2] or ""))
            sub_patches.append(
                SubPatchSlice(
                    file_path=file_path,
                    start_line=start_line,
                    content=content,
                    is_fallback_diff=(start_line == -1),
                )
            )

        hunks = parse_unified_diff(raw_diff)
        failing_tests = self.get_failing_tests(bug_id)
        bug_report = self.get_bug_report(bug_id)
        record = RawPatchRecord(
            patch_id=meta["patch_id"],
            project=meta["project"],
            bug_num=meta["bug_num"],
            bug_id=bug_id,
            tool=meta["tool"],
            label=meta["label"],
            raw_diff=raw_diff,
            hunks=hunks,
            sub_patches=sub_patches,
            failing_tests=failing_tests,
            bug_report=bug_report,
            metadata={"source_index": index, "patch_name": meta["patch_name"]},
        )
        self._hydrate_with_d4j_fallback(record)
        return record

    def _load_unindexed_diff(self, target: PatchTargetSelector) -> RawPatchRecord:
        diff_path = Path(target.local_diff_path)
        raw_diff = normalize_newlines(decode_bytes_safely(diff_path.read_bytes()))
        meta = parse_patch_metadata(str(diff_path))
        bug_id = meta["bug_id"]
        hunks = parse_unified_diff(raw_diff)
        record = RawPatchRecord(
            patch_id=meta["patch_id"],
            project=meta["project"],
            bug_num=meta["bug_num"],
            bug_id=bug_id,
            tool=meta["tool"],
            label=meta["label"],
            raw_diff=raw_diff,
            hunks=hunks,
            sub_patches=[],
            failing_tests=self.get_failing_tests(bug_id),
            bug_report=self.get_bug_report(bug_id),
            metadata={"source_index": -1, "patch_name": meta["patch_name"], "unindexed_local_diff": True},
        )
        self._hydrate_with_d4j_fallback(record)
        return record

    def load_dataset(
        self,
        source: str | Path | DatasetSpec | Iterable[str | dict[str, Any] | PatchTargetSelector] | None = None,
        *,
        projects: Iterable[str] | None = None,
        bug_ids: Iterable[str] | None = None,
        targets: Iterable[str | dict[str, Any] | PatchTargetSelector] | None = None,
        file_paths: Iterable[str] | None = None,
        method_names: Iterable[str] | None = None,
        limit: int | None = None,
        require_tests: bool = False,
        strict: bool = False,
        include_unindexed_diffs: bool | None = None,
    ) -> DatasetLoadResult:
        """Selectively load only the `.pkl` / JSON subset matching a user-supplied dataset."""
        base_spec = discover_dataset_spec(source)
        merged_projects = list(base_spec.projects)
        if projects is not None:
            for p in projects:
                _add_unique(merged_projects, canonicalize_project_name(str(p)))

        merged_bugs = list(base_spec.bug_ids)
        if bug_ids is not None:
            for b in bug_ids:
                _, _, canonical_bug = parse_d4j_bug_id(str(b))
                _add_unique(merged_bugs, canonical_bug)

        merged_targets = list(base_spec.targets)
        if targets is not None:
            for t in targets:
                _add_unique_target(merged_targets, parse_target_selector(t))

        merged_files = list(base_spec.file_paths)
        if file_paths is not None:
            for f in file_paths:
                _add_unique(merged_files, normalize_repo_path(str(f)))

        merged_methods = list(base_spec.method_names)
        if method_names is not None:
            for m in method_names:
                _add_unique(merged_methods, str(m).strip())

        effective_limit = limit if limit is not None else base_spec.limit
        if effective_limit is not None and (type(effective_limit) is not int or effective_limit < 1):
            raise DataLoadError(f"limit must be a positive integer, got {effective_limit!r}")
        effective_require_tests = require_tests or base_spec.require_tests
        effective_strict = strict or base_spec.strict
        if include_unindexed_diffs is None:
            effective_include_unindexed = self.d4j_fallback is not None
        else:
            effective_include_unindexed = include_unindexed_diffs

        effective_spec = DatasetSpec(
            source_label=base_spec.source_label,
            source_path=base_spec.source_path,
            projects=merged_projects,
            bug_ids=merged_bugs,
            targets=merged_targets,
            file_paths=merged_files,
            method_names=merged_methods,
            limit=effective_limit,
            require_tests=effective_require_tests,
            strict=effective_strict,
        )

        raw_list = self._ensure_patches_loaded()
        self._ensure_tests_loaded()
        self._ensure_bug_reports_loaded()

        project_set = set(merged_projects)
        bug_set = set(merged_bugs)
        targets_by_bug: dict[str, list[PatchTargetSelector]] = {}
        for tgt in merged_targets:
            targets_by_bug.setdefault(tgt.bug_id, []).append(tgt)
        allowed_bugs_from_targets = set(targets_by_bug)

        has_bug_or_proj_filter = bool(project_set or bug_set or allowed_bugs_from_targets)

        records: list[RawPatchRecord] = []
        matched_bug_ids_set: set[str] = set()
        matched_targets: set[PatchTargetSelector] = set()
        truncated_by_limit = False

        for idx, raw_entry in enumerate(raw_list):
            if not isinstance(raw_entry, (list, tuple)) or len(raw_entry) != 2:
                raise DataLoadError(f"Malformed patch record at index {idx}")
            meta_pair = raw_entry[0]
            if not isinstance(meta_pair, (list, tuple)) or len(meta_pair) != 2:
                raise DataLoadError(f"Malformed patch header at index {idx}")

            meta = parse_patch_metadata(str(meta_pair[0] or ""))
            rec_proj = meta["project"]
            rec_bug = meta["bug_id"]
            rec_patch_name = meta["patch_name"]

            if has_bug_or_proj_filter:
                matched_proj = rec_proj in project_set
                matched_bug = rec_bug in bug_set
                candidate_targets = targets_by_bug.get(rec_bug, [])
                matched_target_header = any(
                    not tgt.patch_name or tgt.patch_name == rec_patch_name
                    for tgt in candidate_targets
                )
                if not (matched_proj or matched_bug or matched_target_header):
                    continue

            record = self._parse_record(raw_entry, idx)
            if effective_require_tests and not record.failing_tests:
                continue

            if merged_files and not any(_record_file_matches(record, f) for f in merged_files):
                continue

            rec_methods = _record_methods(record)
            if merged_methods:
                matched_m = [m for m in rec_methods if m in merged_methods]
                if not matched_m:
                    continue
                record.metadata["matched_methods"] = matched_m

            candidate_targets = targets_by_bug.get(rec_bug, [])
            if candidate_targets and not (rec_proj in project_set or rec_bug in bug_set):
                passing_targets: list[PatchTargetSelector] = []
                for tgt in candidate_targets:
                    if tgt.patch_name and tgt.patch_name != rec_patch_name:
                        continue
                    if tgt.file_path and not _record_file_matches(record, tgt.file_path):
                        continue
                    if tgt.method_name and tgt.method_name not in rec_methods:
                        continue
                    passing_targets.append(tgt)
                if not passing_targets:
                    continue
                matched_targets.update(passing_targets)
                target_methods = [t.method_name for t in passing_targets if t.method_name]
                if target_methods:
                    record.metadata["matched_methods"] = sorted(set(target_methods))
            else:
                for tgt in candidate_targets:
                    if (
                        (not tgt.patch_name or tgt.patch_name == rec_patch_name)
                        and (not tgt.file_path or _record_file_matches(record, tgt.file_path))
                        and (not tgt.method_name or tgt.method_name in rec_methods)
                    ):
                        matched_targets.add(tgt)

            records.append(record)
            matched_bug_ids_set.add(rec_bug)
            # In strict mode, continue full existence check before slicing to limit.
            if not effective_strict and effective_limit is not None and len(records) >= effective_limit:
                if idx < len(raw_list) - 1:
                    truncated_by_limit = True
                break

        if effective_include_unindexed:
            for tgt in merged_targets:
                if tgt in matched_targets or not tgt.local_diff_path:
                    continue
                diff_file = Path(tgt.local_diff_path)
                if not diff_file.is_file():
                    continue
                if not effective_strict and effective_limit is not None and len(records) >= effective_limit:
                    truncated_by_limit = True
                    break
                unindexed_rec = self._load_unindexed_diff(tgt)
                if effective_require_tests and not unindexed_rec.failing_tests:
                    continue
                if merged_files and not any(_record_file_matches(unindexed_rec, f) for f in merged_files):
                    continue
                rec_methods = _record_methods(unindexed_rec)
                if merged_methods and not any(m in rec_methods for m in merged_methods):
                    continue
                if tgt.file_path and not _record_file_matches(unindexed_rec, tgt.file_path):
                    continue
                if tgt.method_name and tgt.method_name not in rec_methods:
                    continue
                records.append(unindexed_rec)
                matched_bug_ids_set.add(unindexed_rec.bug_id)
                matched_targets.add(tgt)

        if truncated_by_limit:
            missing_bug_ids: list[str] = []
            missing_targets: list[PatchTargetSelector] = []
        else:
            missing_bug_ids = [b for b in merged_bugs if b not in matched_bug_ids_set]
            missing_targets = [t for t in merged_targets if t not in matched_targets]

        if effective_strict and (missing_bug_ids or missing_targets):
            raise DataLoadError(
                f"Requested dataset items missing from index: "
                f"missing_bugs={missing_bug_ids}, missing_targets={[t.to_dict() for t in missing_targets]}"
            )

        if effective_limit is not None and len(records) > effective_limit:
            records = records[:effective_limit]
            truncated_by_limit = True

        return DatasetLoadResult(
            spec=effective_spec,
            records=records,
            matched_bug_ids=sorted(matched_bug_ids_set),
            missing_bug_ids=missing_bug_ids,
            missing_targets=missing_targets,
            total_scanned=len(raw_list),
            truncated_by_limit=truncated_by_limit,
        )

    def load_all(
        self,
        projects: Iterable[str] | None = None,
        bug_ids: Iterable[str] | None = None,
        limit: int | None = None,
        require_tests: bool = False,
        *,
        source: str | Path | DatasetSpec | Iterable[str | dict[str, Any] | PatchTargetSelector] | None = None,
        targets: Iterable[str | dict[str, Any] | PatchTargetSelector] | None = None,
        file_paths: Iterable[str] | None = None,
        method_names: Iterable[str] | None = None,
        strict: bool = False,
        include_unindexed_diffs: bool | None = None,
    ) -> list[RawPatchRecord]:
        """Load and filter patch records into normalized `RawPatchRecord` objects."""
        return self.load_dataset(
            source=source,
            projects=projects,
            bug_ids=bug_ids,
            targets=targets,
            file_paths=file_paths,
            method_names=method_names,
            limit=limit,
            require_tests=require_tests,
            strict=strict,
            include_unindexed_diffs=include_unindexed_diffs,
        ).records

    def load_by_bug_id(self, bug_id: str) -> list[RawPatchRecord]:
        _, _, canonical_bug = parse_d4j_bug_id(bug_id)
        return self.load_dataset(bug_ids=[canonical_bug]).records

    def load_by_patch_id(self, patch_id: str) -> RawPatchRecord:
        cleaned = path_key(patch_id.strip())
        raw_list = self._ensure_patches_loaded()
        self._ensure_tests_loaded()
        self._ensure_bug_reports_loaded()

        for idx, raw_entry in enumerate(raw_list):
            if isinstance(raw_entry, (list, tuple)) and raw_entry and isinstance(raw_entry[0], (list, tuple)):
                candidate = path_key(str(raw_entry[0][0] or "").strip())
                if candidate == cleaned:
                    return self._parse_record(raw_entry, idx)

        try:
            selector = parse_target_selector(patch_id)
        except DataLoadError:
            selector = None
        if selector is not None:
            result = self.load_dataset(targets=[selector])
            if result.records:
                if len(result.records) == 1:
                    return result.records[0]
                raise DataLoadError(f"Ambiguous patch selector {patch_id!r} matched {len(result.records)} records")

        raise DataLoadError(f"Patch ID not found: {patch_id!r}")

    def summary(
        self,
        source: str | Path | DatasetSpec | Iterable[str | dict[str, Any] | PatchTargetSelector] | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """Return lightweight counts for either a selected dataset subset or the full index."""
        result = self.load_dataset(source=source, **kwargs)
        records = result.records
        projects: dict[str, int] = {}
        missing_tests = 0
        missing_reports = 0
        fallback_slices = 0
        for rec in records:
            projects[rec.project] = projects.get(rec.project, 0) + 1
            if not rec.failing_tests:
                missing_tests += 1
            if not rec.bug_report.available:
                missing_reports += 1
            if any(sp.is_fallback_diff for sp in rec.sub_patches):
                fallback_slices += 1
        return {
            "source_label": result.spec.source_label,
            "source_path": result.spec.source_path,
            "total_scanned": result.total_scanned,
            "total_patches": len(records),
            "unique_bugs": len({rec.bug_id for rec in records}),
            "truncated_by_limit": result.truncated_by_limit,
            "projects": dict(sorted(projects.items())),
            "missing_bug_ids": result.missing_bug_ids,
            "missing_targets": [t.to_dict() for t in result.missing_targets],
            "patches_without_tests": missing_tests,
            "patches_without_bug_report": missing_reports,
            "patches_with_fallback_slice": fallback_slices,
        }
