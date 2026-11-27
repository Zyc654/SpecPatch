"""On-demand repository search (`grep_code`) and line-window reading (`read_file_lines`)."""

from __future__ import annotations

import fnmatch
import json
from pathlib import Path
import re
from typing import Any

from .protocols import (
    DEFAULT_COMMAND_TIMEOUT_SEC,
    LocalRepoExecutor,
    RepoExecutor,
    ToolExecutionError,
    assert_patch_blind_state,
    extract_numbered_snippet,
    resolve_context_window,
    resolve_repo_rel_path,
)


def _format_match_entry(
    file_path: str,
    match_line_no: int,
    lines_by_no: dict[int, str],
    eff_before: int,
    eff_after: int,
) -> dict[str, Any]:
    start_no = max(1, match_line_no - eff_before)
    end_no = match_line_no + eff_after
    before_list = [
        {"line": idx, "text": lines_by_no[idx]}
        for idx in range(start_no, match_line_no)
        if idx in lines_by_no
    ]
    after_list = [
        {"line": idx, "text": lines_by_no[idx]}
        for idx in range(match_line_no + 1, end_no + 1)
        if idx in lines_by_no
    ]
    snippet_lines = [
        f"{idx}{'>' if idx == match_line_no else ':'} {lines_by_no[idx]}"
        for idx in range(start_no, end_no + 1)
        if idx in lines_by_no
    ]
    return {
        "file_path": file_path,
        "line": match_line_no,
        "match_line": lines_by_no.get(match_line_no, ""),
        "context_before_lines": before_list,
        "context_after_lines": after_list,
        "snippet": "\n".join(snippet_lines),
    }


def _parse_rg_json_events(
    stdout: str,
    eff_before: int,
    eff_after: int,
    max_matches: int,
    readonly_prepatch: bool,
) -> tuple[list[dict[str, Any]], bool]:
    """Parse `rg --json` events into structured matches with context lines."""
    raw_hits: list[tuple[str, int]] = []
    file_lines: dict[str, dict[int, str]] = {}

    for raw_line in stdout.splitlines():
        if not raw_line.strip():
            continue
        try:
            event = json.loads(raw_line)
        except json.JSONDecodeError:
            continue
        etype = event.get("type")
        if etype not in {"match", "context"}:
            continue
        data = event.get("data", {})
        path_text = data.get("path", {}).get("text")
        line_no = data.get("line_number")
        text = data.get("lines", {}).get("text", "")
        if not isinstance(path_text, str) or type(line_no) is not int:
            continue
        try:
            rel_path = resolve_repo_rel_path(path_text, readonly_prepatch=readonly_prepatch)
        except ToolExecutionError:
            continue
        cleaned_text = text.rstrip("\r\n")
        file_lines.setdefault(rel_path, {})[line_no] = cleaned_text
        if etype == "match":
            raw_hits.append((rel_path, line_no))

    truncated = len(raw_hits) > max_matches
    matches = [
        _format_match_entry(rel_path, line_no, file_lines[rel_path], eff_before, eff_after)
        for rel_path, line_no in raw_hits[:max_matches]
    ]
    return matches, truncated


def _parse_gnu_grep_output(
    stdout: str,
    eff_before: int,
    eff_after: int,
    max_matches: int,
    readonly_prepatch: bool,
) -> tuple[list[dict[str, Any]], bool]:
    """Parse `grep -rn -B N -A M` output (':' for match, '-' for context)."""
    line_re = re.compile(r"^(.+?)([:-])(\d+)\2(.*)$")
    raw_hits: list[tuple[str, int]] = []
    file_lines: dict[str, dict[int, str]] = {}

    for raw_line in stdout.splitlines():
        if raw_line == "--":
            continue
        match = line_re.match(raw_line)
        if not match:
            continue
        path_text, sep, num_str, content = match.groups()
        try:
            rel_path = resolve_repo_rel_path(path_text, readonly_prepatch=readonly_prepatch)
        except ToolExecutionError:
            continue
        line_no = int(num_str)
        file_lines.setdefault(rel_path, {})[line_no] = content
        if sep == ":":
            raw_hits.append((rel_path, line_no))

    truncated = len(raw_hits) > max_matches
    matches = [
        _format_match_entry(rel_path, line_no, file_lines[rel_path], eff_before, eff_after)
        for rel_path, line_no in raw_hits[:max_matches]
    ]
    return matches, truncated


class RepoSearchTools:
    """Single-purpose repository search and line-window reader with configurable context."""

    def __init__(
        self,
        executor: RepoExecutor,
        tools_cfg: dict[str, Any],
        readonly_prepatch: bool = True,
    ) -> None:
        assert_patch_blind_state(executor, readonly_prepatch)
        self.executor = executor
        self.tools_cfg = tools_cfg
        self.readonly_prepatch = readonly_prepatch

    def grep_code(
        self,
        pattern: str,
        path_glob: str = "",
        context_before: int | None = None,
        context_after: int | None = None,
        context_lines: int | None = None,
        max_matches: int | None = None,
        case_sensitive: bool = True,
        fixed_strings: bool = False,
    ) -> dict[str, Any]:
        """Search the repo with configurable before/after context lines (`rg` in Docker or local Python)."""
        if not isinstance(pattern, str) or not pattern:
            raise ToolExecutionError("pattern must be a non-empty string")
        if "\n" in pattern or "\r" in pattern or "\x00" in pattern:
            raise ToolExecutionError("pattern cannot contain newline or NUL characters")
        if not isinstance(path_glob, str):
            raise ToolExecutionError("path_glob must be a string")
        cleaned_glob = path_glob.strip()
        if "\n" in cleaned_glob or "\r" in cleaned_glob or "\x00" in cleaned_glob or ".." in cleaned_glob:
            raise ToolExecutionError("path_glob contains forbidden characters or '..'")
        if cleaned_glob.startswith("-"):
            raise ToolExecutionError("path_glob cannot start with '-'")
        if type(case_sensitive) is not bool or type(fixed_strings) is not bool:
            raise ToolExecutionError("case_sensitive and fixed_strings must be booleans")

        cfg_max_matches = self.tools_cfg["grep_max_matches"]
        if max_matches is not None:
            if type(max_matches) is not int or max_matches < 1:
                raise ToolExecutionError("max_matches must be an integer >= 1")
            eff_max_matches = min(max_matches, cfg_max_matches)
        else:
            eff_max_matches = cfg_max_matches

        eff_before, eff_after, ctx_clamped = resolve_context_window(
            context_before=context_before,
            context_after=context_after,
            context_lines=context_lines,
            default_before=self.tools_cfg["grep_default_context_before"],
            default_after=self.tools_cfg["grep_default_context_after"],
            max_context=self.tools_cfg["grep_max_context_lines"],
        )

        if isinstance(self.executor, LocalRepoExecutor):
            matches, truncated = self._grep_local(
                pattern=pattern,
                path_glob=cleaned_glob,
                eff_before=eff_before,
                eff_after=eff_after,
                max_matches=eff_max_matches,
                case_sensitive=case_sensitive,
                fixed_strings=fixed_strings,
            )
        else:
            matches, truncated = self._grep_container(
                pattern=pattern,
                path_glob=cleaned_glob,
                eff_before=eff_before,
                eff_after=eff_after,
                max_matches=eff_max_matches,
                case_sensitive=case_sensitive,
                fixed_strings=fixed_strings,
            )

        return {
            "ok": True,
            "pattern": pattern,
            "path_glob": cleaned_glob,
            "effective_context_before": eff_before,
            "effective_context_after": eff_after,
            "context_clamped": ctx_clamped,
            "total_matches": len(matches),
            "truncated": truncated,
            "matches": matches,
        }

    def _grep_container(
        self,
        pattern: str,
        path_glob: str,
        eff_before: int,
        eff_after: int,
        max_matches: int,
        case_sensitive: bool,
        fixed_strings: bool,
    ) -> tuple[list[dict[str, Any]], bool]:
        timeout = int(self.tools_cfg.get("command_timeout_sec", DEFAULT_COMMAND_TIMEOUT_SEC))
        rg_argv = [
            "rg",
            "--json",
            "-n",
            "--no-heading",
            "-B",
            str(eff_before),
            "-A",
            str(eff_after),
        ]
        if not case_sensitive:
            rg_argv.append("-i")
        if fixed_strings:
            rg_argv.append("-F")
        if self.readonly_prepatch:
            rg_argv.extend(["--glob", "!.git/**", "--glob", "!*.diff", "--glob", "!*.patch"])
        if path_glob:
            rg_argv.extend(["--glob", path_glob])
        rg_argv.extend(["-e", pattern, "."])

        out = self.executor.exec_command(rg_argv, timeout=timeout)
        if out.timed_out:
            raise ToolExecutionError("Container grep timed out")
        if out.returncode in {0, 1}:
            return _parse_rg_json_events(
                out.stdout, eff_before, eff_after, max_matches, self.readonly_prepatch
            )
        if out.returncode != 127:
            raise ToolExecutionError(
                f"Container rg failed (rc={out.returncode}): {out.stderr.strip() or 'invalid pattern/arguments'}"
            )

        # Fallback to GNU grep only when rg binary is absent (rc == 127).
        grep_argv = [
            "grep",
            "-rn",
            "-B",
            str(eff_before),
            "-A",
            str(eff_after),
        ]
        if not case_sensitive:
            grep_argv.append("-i")
        if fixed_strings:
            grep_argv.append("-F")
        else:
            grep_argv.append("-E")
        if self.readonly_prepatch:
            grep_argv.extend(["--exclude-dir=.git", "--exclude=*.diff", "--exclude=*.patch"])
        if path_glob:
            grep_argv.append(f"--include={path_glob}")
        grep_argv.extend(["-e", pattern, "."])

        grep_out = self.executor.exec_command(grep_argv, timeout=timeout)
        if grep_out.timed_out:
            raise ToolExecutionError("Container grep fallback timed out")
        if grep_out.returncode not in {0, 1}:
            raise ToolExecutionError(
                f"Container grep failed (rc={grep_out.returncode}): {grep_out.stderr.strip()}"
            )
        return _parse_gnu_grep_output(
            grep_out.stdout, eff_before, eff_after, max_matches, self.readonly_prepatch
        )

    def _grep_local(
        self,
        pattern: str,
        path_glob: str,
        eff_before: int,
        eff_after: int,
        max_matches: int,
        case_sensitive: bool,
        fixed_strings: bool,
    ) -> tuple[list[dict[str, Any]], bool]:
        assert isinstance(self.executor, LocalRepoExecutor)
        flags = 0 if case_sensitive else re.IGNORECASE
        expr = re.escape(pattern) if fixed_strings else pattern
        try:
            compiled = re.compile(expr, flags)
        except re.error as exc:
            raise ToolExecutionError(f"Invalid regex pattern: {exc}") from None

        matches: list[dict[str, Any]] = []
        truncated = False
        root = self.executor.root_path

        for path in sorted(root.rglob("*")):
            if not path.is_file():
                continue
            rel = path.relative_to(root).as_posix()
            try:
                safe_rel = resolve_repo_rel_path(rel, readonly_prepatch=self.readonly_prepatch)
            except ToolExecutionError:
                continue
            if path_glob and not (
                fnmatch.fnmatch(safe_rel, path_glob) or fnmatch.fnmatch(Path(safe_rel).name, path_glob)
            ):
                continue
            text = self.executor.read_file(safe_rel)
            lines = text.splitlines()
            lines_by_no = {idx: line for idx, line in enumerate(lines, start=1)}
            for idx, line in enumerate(lines, start=1):
                if compiled.search(line):
                    if len(matches) >= max_matches:
                        truncated = True
                        return matches, truncated
                    matches.append(
                        _format_match_entry(safe_rel, idx, lines_by_no, eff_before, eff_after)
                    )
        return matches, truncated

    def read_file_lines(
        self,
        file_path: str,
        start_line: int,
        end_line: int | None = None,
        context_before: int | None = None,
        context_after: int | None = None,
        context_lines: int | None = None,
    ) -> dict[str, Any]:
        """Read a 1-based line range from a repository file with configurable surrounding context."""
        if type(start_line) is not int or start_line < 1:
            raise ToolExecutionError("start_line must be an integer >= 1")
        eff_end = start_line if end_line is None else end_line
        if type(eff_end) is not int or eff_end < start_line:
            raise ToolExecutionError("end_line must be an integer >= start_line")

        safe_rel = resolve_repo_rel_path(file_path, readonly_prepatch=self.readonly_prepatch)
        eff_before, eff_after, ctx_clamped = resolve_context_window(
            context_before=context_before,
            context_after=context_after,
            context_lines=context_lines,
            default_before=self.tools_cfg["read_default_context_before"],
            default_after=self.tools_cfg["read_default_context_after"],
            max_context=self.tools_cfg["read_max_lines"],
        )
        text = self.executor.read_file(safe_rel)
        snippet = extract_numbered_snippet(
            text=text,
            start_line=start_line,
            end_line=eff_end,
            before=eff_before,
            after=eff_after,
            max_lines=self.tools_cfg["read_max_lines"],
        )
        return {
            "ok": True,
            "file_path": safe_rel,
            "requested_start": start_line,
            "requested_end": eff_end,
            "effective_context_before": eff_before,
            "effective_context_after": eff_after,
            "context_clamped": ctx_clamped,
            **snippet,
        }
