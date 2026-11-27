"""Execution protocols, safe path containment, context-window resolution, and Docker/Local executors."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path, PurePosixPath
import re
from typing import Any, Literal, Protocol, runtime_checkable

from ..core.doctor import CommandRunner, default_runner

_DRIVE_PREFIX_RE = re.compile(r"^[A-Za-z]:")
DEFAULT_COMMAND_TIMEOUT_SEC = 900


class ToolExecutionError(RuntimeError):
    """Raised when a repo/LSP tool receives invalid arguments or hits a sandbox boundary."""


@dataclass(frozen=True)
class ExecOutput:
    """Normalized subprocess/container command execution output."""

    returncode: int
    stdout: str
    stderr: str
    timed_out: bool = False


@runtime_checkable
class RepoExecutor(Protocol):
    """Abstract repository executor targeting either a Docker container or local directory."""

    work_root: str
    state: Literal["prepatch", "postpatch"]

    def exec_command(self, argv: list[str], timeout: int = DEFAULT_COMMAND_TIMEOUT_SEC) -> ExecOutput: ...

    def read_file(self, rel_path: str) -> str: ...


def assert_patch_blind_state(executor: RepoExecutor, readonly_prepatch: bool) -> None:
    """Enforce Patch-Blind isolation so spec/PBT tools never inspect a postpatch repository."""
    if readonly_prepatch and executor.state != "prepatch":
        raise ToolExecutionError(
            f"Patch-Blind violation: readonly_prepatch=True requires executor.state == 'prepatch', got {executor.state!r}"
        )


def resolve_repo_rel_path(rel_path: str, readonly_prepatch: bool = True) -> str:
    """Validate and normalize a repository-relative POSIX path with Patch-Blind safeguards."""
    if not isinstance(rel_path, str):
        raise ToolExecutionError("file_path must be a string")
    if "\n" in rel_path or "\r" in rel_path or "\x00" in rel_path:
        raise ToolExecutionError("file_path cannot contain newline or NUL characters")
    cleaned = rel_path.strip().replace("\\", "/")
    if not cleaned:
        raise ToolExecutionError("file_path cannot be empty")
    if cleaned.startswith("/") or _DRIVE_PREFIX_RE.match(cleaned):
        raise ToolExecutionError(f"file_path must be repository-relative, got: {rel_path}")

    parts = [part for part in cleaned.split("/") if part and part != "."]
    if not parts or any(part == ".." for part in parts):
        raise ToolExecutionError(f"Path traversal ('..') is forbidden in file_path: {rel_path}")

    normalized = PurePosixPath(*parts).as_posix()
    if readonly_prepatch:
        if ".git" in parts:
            raise ToolExecutionError("Access to .git internals is forbidden in readonly_prepatch mode")
        lower_name = parts[-1].lower()
        if lower_name.endswith((".diff", ".patch")) or lower_name in {"patch", "patch.diff"}:
            raise ToolExecutionError(
                f"Access to patch/diff artifact '{normalized}' is forbidden in readonly_prepatch mode"
            )
    return normalized


def resolve_context_window(
    context_before: int | None,
    context_after: int | None,
    context_lines: int | None,
    default_before: int,
    default_after: int,
    max_context: int,
) -> tuple[int, int, bool]:
    """Resolve `(before, after, clamped)` from explicit overrides, symmetric shorthand, and config bounds."""
    for name, value in (
        ("context_before", context_before),
        ("context_after", context_after),
        ("context_lines", context_lines),
    ):
        if value is not None:
            if type(value) is not int or value < 0:
                raise ToolExecutionError(f"{name} must be a non-negative integer")

    if context_lines is not None:
        raw_before = context_lines if context_before is None else context_before
        raw_after = context_lines if context_after is None else context_after
    else:
        raw_before = default_before if context_before is None else context_before
        raw_after = default_after if context_after is None else context_after

    eff_before = min(raw_before, max_context)
    eff_after = min(raw_after, max_context)
    clamped = (eff_before != raw_before) or (eff_after != raw_after)
    return eff_before, eff_after, clamped


def extract_numbered_snippet_from_lines(
    lines: list[str],
    start_line: int,
    end_line: int,
    before: int,
    after: int,
    max_lines: int,
) -> dict[str, Any]:
    """Slice 1-based `[start_line - before, end_line + after]` from pre-split lines."""
    if type(start_line) is not int or start_line < 1:
        raise ToolExecutionError("start_line must be an integer >= 1")
    if type(end_line) is not int or end_line < start_line:
        raise ToolExecutionError("end_line must be an integer >= start_line")

    total_lines = len(lines)
    if total_lines == 0:
        return {
            "actual_start": 0,
            "actual_end": 0,
            "total_lines": 0,
            "truncated": False,
            "content": "",
        }

    if start_line > total_lines:
        raise ToolExecutionError(
            f"start_line ({start_line}) exceeds file line count ({total_lines})"
        )

    actual_start = max(1, start_line - before)
    actual_end = min(total_lines, end_line + after)
    truncated = False
    if actual_end - actual_start + 1 > max_lines:
        actual_end = actual_start + max_lines - 1
        truncated = True

    numbered = "\n".join(f"{idx}: {lines[idx - 1]}" for idx in range(actual_start, actual_end + 1))
    return {
        "actual_start": actual_start,
        "actual_end": actual_end,
        "total_lines": total_lines,
        "truncated": truncated,
        "content": numbered,
    }


def extract_numbered_snippet(
    text: str,
    start_line: int,
    end_line: int,
    before: int,
    after: int,
    max_lines: int,
) -> dict[str, Any]:
    """Slice 1-based `[start_line - before, end_line + after]` with line numbers and max-line clamping."""
    return extract_numbered_snippet_from_lines(
        text.splitlines(), start_line, end_line, before, after, max_lines
    )


def _decode_bytes(raw: bytes) -> str:
    for encoding in ("utf-8-sig", "utf-8", "latin-1"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


class DockerExecExecutor:
    """Execute read-only repo inspection commands inside a running Linux Docker container."""

    def __init__(
        self,
        container_name: str,
        work_root: str,
        state: Literal["prepatch", "postpatch"] = "prepatch",
        readonly_prepatch: bool = True,
        runner: CommandRunner | None = None,
        command_timeout_sec: int = DEFAULT_COMMAND_TIMEOUT_SEC,
    ) -> None:
        if not container_name or not container_name.strip():
            raise ToolExecutionError("container_name cannot be empty")
        if not work_root or not work_root.startswith("/"):
            raise ToolExecutionError("Docker work_root must be an absolute POSIX path")
        # [SERVER_CONFIG_REQUIRED]: 确认 DockerExecExecutor 连接的容器名与 container_work_root 已正确挂载 D4J 工作区
        self.container_name = container_name.strip()
        self.work_root = work_root.rstrip("/") or "/"
        self.state = state
        self.readonly_prepatch = readonly_prepatch
        self._runner = runner or default_runner
        self.command_timeout_sec = command_timeout_sec

    def exec_command(self, argv: list[str], timeout: int | None = None) -> ExecOutput:
        if not argv:
            raise ToolExecutionError("argv cannot be empty")
        eff_timeout = self.command_timeout_sec if timeout is None else timeout
        docker_cmd = ["docker", "exec", "-w", self.work_root, self.container_name, *argv]
        rc, out, err = self._runner(docker_cmd, eff_timeout)
        return ExecOutput(
            returncode=rc,
            stdout=out,
            stderr=err,
            timed_out=(rc == 124 and "timed out" in err.lower()),
        )

    def read_file(self, rel_path: str) -> str:
        safe_rel = resolve_repo_rel_path(rel_path, readonly_prepatch=self.readonly_prepatch)
        result = self.exec_command(["cat", "--", safe_rel], timeout=self.command_timeout_sec)
        if result.timed_out:
            raise ToolExecutionError(f"Timed out reading container file: {safe_rel}")
        if result.returncode != 0:
            raise ToolExecutionError(
                f"Failed to read container file '{safe_rel}': {result.stderr.strip() or f'rc={result.returncode}'}"
            )
        return result.stdout


class LocalRepoExecutor:
    """Local-directory `RepoExecutor` used for offline unit tests and host-side inspection."""

    def __init__(
        self,
        root_dir: str | Path,
        state: Literal["prepatch", "postpatch"] = "prepatch",
        readonly_prepatch: bool = True,
        runner: CommandRunner | None = None,
        command_timeout_sec: int = DEFAULT_COMMAND_TIMEOUT_SEC,
    ) -> None:
        self.root_path = Path(root_dir).resolve()
        if not self.root_path.is_dir():
            raise ToolExecutionError(f"Local repo root does not exist: {self.root_path}")
        self.work_root = self.root_path.as_posix()
        self.state = state
        self.readonly_prepatch = readonly_prepatch
        self._runner = runner
        self.command_timeout_sec = command_timeout_sec

    def exec_command(self, argv: list[str], timeout: int | None = None) -> ExecOutput:
        if not argv:
            raise ToolExecutionError("argv cannot be empty")
        eff_timeout = self.command_timeout_sec if timeout is None else timeout
        if self._runner is None:
            rc, out, err = default_runner(argv, eff_timeout, cwd=self.root_path)
        else:
            rc, out, err = self._runner(argv, eff_timeout)
        return ExecOutput(
            returncode=rc,
            stdout=out,
            stderr=err,
            timed_out=(rc == 124 and "timed out" in err.lower()),
        )

    def read_file(self, rel_path: str) -> str:
        safe_rel = resolve_repo_rel_path(rel_path, readonly_prepatch=self.readonly_prepatch)
        target = (self.root_path / safe_rel).resolve()
        if not target.is_relative_to(self.root_path):
            raise ToolExecutionError(f"Resolved path escapes repository root: {rel_path}")
        if not target.is_file():
            raise ToolExecutionError(f"File not found in repository: {safe_rel}")
        return _decode_bytes(target.read_bytes())
