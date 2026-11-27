"""Online Docker Defects4J fallback provider for unindexed bugs, source files, and failing tests."""

from __future__ import annotations

from pathlib import Path
import re
import shlex
from typing import Any, Protocol, runtime_checkable

from ..analysis.ast_context import (
    _parse_method_header,
    _scan_java_blocks,
    _strip_comments_and_strings,
)
from ..analysis.protocols import resolve_repo_rel_path
from ..core.config_loader import Settings
from ..core.doctor import CommandRunner, default_runner
from .schemas import BugReport, DiffHunk, FailingTestRecord, SubPatchSlice

_PROJECT_RE = re.compile(r"^[A-Za-z][A-Za-z0-9]*$")
_NON_METHOD_KEYWORDS = frozenset({"if", "for", "while", "switch", "catch", "synchronized", "else"})


class D4jFallbackError(RuntimeError):
    """Raised when an invalid request is sent to the Defects4J fallback provider."""


def decode_bytes_safely(raw: bytes) -> str:
    """Decode bytes using UTF-8-SIG, UTF-8, or Latin-1 fallback matching `analysis.protocols`."""
    for encoding in ("utf-8-sig", "utf-8", "latin-1"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def _normalize_lf(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _number_lines(text: str, start_line: int) -> str:
    if not text:
        return ""
    lines = _normalize_lf(text).splitlines()
    return "\n".join(f"{start_line + idx}: {line}" for idx, line in enumerate(lines))


def _extract_method_spans(source: str) -> list[tuple[str, int, int]]:
    """Return `(method_name, start_line, end_line)` for Java methods/constructors in `source`."""
    masked, _ = _strip_comments_and_strings(source)
    spans, _ = _scan_java_blocks(masked)
    methods: list[tuple[str, int, int]] = []
    for span in spans:
        if not span.header:
            continue
        parsed = _parse_method_header(span.header)
        if not parsed:
            continue
        prefix, name, _, _ = parsed
        if name in _NON_METHOD_KEYWORDS:
            continue
        if re.search(r"\b(?:class|interface|enum|record)\b", prefix):
            continue
        methods.append((name, span.start_line, span.end_line))
    return methods


def _parse_d4j_info_output(info_text: str) -> tuple[str, str, str]:
    """Extract `(summary_title, summary_body, root_cause)` from `defects4j info` output."""
    lines = _normalize_lf(info_text).splitlines()
    summary_lines: list[str] = []
    root_cause_lines: list[str] = []
    section: str | None = None

    for line in lines:
        stripped = line.strip()
        if stripped.startswith("Summary:"):
            section = "summary"
            tail = stripped[len("Summary:") :].strip()
            if tail:
                summary_lines.append(tail)
            continue
        if stripped.startswith("Root cause in triggering tests:"):
            section = "root_cause"
            tail = stripped[len("Root cause in triggering tests:") :].strip()
            if tail:
                root_cause_lines.append(tail)
            continue
        if stripped.startswith(("--------------------------------------------------------------------------------", "Revision ", "Modified ", "List of ")):
            section = None
            continue
        if section == "summary":
            if stripped:
                summary_lines.append(stripped)
        elif section == "root_cause":
            if stripped:
                root_cause_lines.append(stripped)

    title = summary_lines[0] if summary_lines else ""
    summary_body = "\n".join(summary_lines).strip()
    root_cause = "\n".join(root_cause_lines).strip()
    return title, summary_body, root_cause


@runtime_checkable
class D4jFallbackProvider(Protocol):
    """Protocol for fetching unindexed Defects4J sources, tests, and bug metadata."""

    def fetch_source_file(self, project: str, bug_num: int, rel_path: str) -> str | None: ...

    def fetch_failing_tests(self, project: str, bug_num: int) -> list[FailingTestRecord]: ...

    def fetch_bug_report(self, project: str, bug_num: int) -> BugReport: ...

    def build_function_slices_for_hunks(
        self, project: str, bug_num: int, hunks: list[DiffHunk]
    ) -> list[SubPatchSlice]: ...


class DockerD4jFallbackProvider:
    """Online Defects4J fallback querying a Docker container/image when `.pkl` lacks an item."""

    def __init__(
        self,
        image_name: str,
        container_work_root: str,
        d4j_home: str,
        java8_home: str,
        *,
        container_name: str | None = None,
        timeout_seconds: int = 900,
        run_trigger_tests: bool = False,
        include_d4j_summary: bool = False,
        runner: CommandRunner | None = None,
    ) -> None:
        if not image_name or not image_name.strip():
            raise D4jFallbackError("image_name cannot be empty")
        if not container_work_root.startswith("/"):
            raise D4jFallbackError("container_work_root must be an absolute POSIX path")
        self.image_name = image_name.strip()
        self.container_work_root = container_work_root.rstrip("/") or "/"
        self.d4j_home = d4j_home.rstrip("/")
        self.java8_home = java8_home.rstrip("/")
        self.container_name = container_name.strip() if container_name and container_name.strip() else None
        self.timeout_seconds = timeout_seconds
        self.run_trigger_tests = run_trigger_tests
        self.include_d4j_summary = include_d4j_summary
        self._runner = runner or default_runner
        self._checked_out: dict[tuple[str, int], str] = {}
        self._info_cache: dict[tuple[str, int], str] = {}
        self._file_cache: dict[tuple[str, int, str], str | None] = {}

    @classmethod
    def from_settings(
        cls,
        settings: Settings,
        *,
        container_name: str | None = None,
        run_trigger_tests: bool = False,
        include_d4j_summary: bool = False,
        runner: CommandRunner | None = None,
    ) -> DockerD4jFallbackProvider:
        # Docker/D4J settings are validated and marked with SERVER_CONFIG_REQUIRED in core/config_loader.py.
        return cls(
            image_name=settings.docker["image_name"],
            container_work_root=settings.docker["container_work_root"],
            d4j_home=settings.defects4j["d4j_home"],
            java8_home=settings.defects4j["java8_home"],
            container_name=container_name,
            timeout_seconds=settings.docker["timeout_seconds"],
            run_trigger_tests=run_trigger_tests,
            include_d4j_summary=include_d4j_summary,
            runner=runner,
        )

    @staticmethod
    def _validate_bug_coords(project: str, bug_num: int) -> tuple[str, int]:
        proj = project.strip()
        if not _PROJECT_RE.fullmatch(proj):
            raise D4jFallbackError(f"Invalid Defects4J project name: {project!r}")
        if type(bug_num) is not int or bug_num < 1:
            raise D4jFallbackError(f"Invalid Defects4J bug number: {bug_num!r}")
        return proj, bug_num

    def _run_shell(self, script: str, timeout: int | None = None) -> tuple[int, str, str]:
        eff_timeout = self.timeout_seconds if timeout is None else timeout
        env_prefix = (
            f"export JAVA_HOME={shlex.quote(self.java8_home)} && "
            f'export PATH="$JAVA_HOME/bin":{shlex.quote(self.d4j_home + "/framework/bin")}:$PATH'
        )
        full_script = f"{env_prefix} && {script}"
        if self.container_name is not None:
            argv = ["docker", "exec", self.container_name, "bash", "-lc", full_script]
        else:
            argv = ["docker", "run", "--rm", self.image_name, "bash", "-lc", full_script]
        return self._runner(argv, eff_timeout)

    def ensure_checkout(self, project: str, bug_num: int) -> str:
        """Ensure `<Project>_<BugNum>b` is checked out inside the Docker container."""
        proj, num = self._validate_bug_coords(project, bug_num)
        key = (proj, num)
        if key in self._checked_out:
            return self._checked_out[key]

        workdir = f"{self.container_work_root}/{proj}_{num}b"
        script = (
            f"if [ ! -f {shlex.quote(workdir + '/.defects4j.config')} ]; then "
            f"mkdir -p {shlex.quote(self.container_work_root)} && "
            f"defects4j checkout -p {shlex.quote(proj)} -v {num}b -w {shlex.quote(workdir)}; "
            f"fi"
        )
        rc, _, err = self._run_shell(script)
        if rc != 0:
            raise D4jFallbackError(
                f"Failed to checkout {proj}-{num}b in Docker: {err.strip() or f'rc={rc}'}"
            )
        self._checked_out[key] = workdir
        return workdir

    def _get_d4j_info(self, project: str, bug_num: int) -> str:
        proj, num = self._validate_bug_coords(project, bug_num)
        key = (proj, num)
        if key not in self._info_cache:
            rc, out, _ = self._run_shell(f"defects4j info -p {shlex.quote(proj)} -b {num}")
            self._info_cache[key] = _normalize_lf(out) if rc == 0 else ""
        return self._info_cache[key]

    def fetch_source_file(self, project: str, bug_num: int, rel_path: str) -> str | None:
        """Read a pre-patch repository-relative file from the Docker Defects4J checkout."""
        proj, num = self._validate_bug_coords(project, bug_num)
        safe_rel = resolve_repo_rel_path(rel_path, readonly_prepatch=True)
        cache_key = (proj, num, safe_rel)
        if cache_key in self._file_cache:
            return self._file_cache[cache_key]

        workdir = self.ensure_checkout(proj, num)
        rc, out, _ = self._run_shell(f"cat -- {shlex.quote(workdir + '/' + safe_rel)}")
        content = _normalize_lf(out) if rc == 0 else None
        self._file_cache[cache_key] = content
        return content

    def fetch_bug_report(self, project: str, bug_num: int) -> BugReport:
        """Build a `BugReport` from `defects4j info` without leaking commit summaries by default."""
        proj, num = self._validate_bug_coords(project, bug_num)
        bug_id = f"{proj}-{num}"
        info_text = self._get_d4j_info(proj, num)
        if not info_text:
            return BugReport(bug_id=bug_id, title="", description="", available=False)
        title, summary_body, root_cause = _parse_d4j_info_output(info_text)
        if not self.include_d4j_summary:
            if not root_cause:
                return BugReport(bug_id=bug_id, title="", description="", available=False)
            return BugReport(
                bug_id=bug_id,
                title=f"{bug_id} triggering test failure",
                description=root_cause,
                available=True,
            )
        desc_parts = [part for part in (summary_body, root_cause) if part]
        description = "\n\n".join(desc_parts)
        return BugReport(
            bug_id=bug_id,
            title=title or f"{bug_id} triggering failure",
            description=description,
            available=bool(title or description),
        )

    def fetch_failing_tests(self, project: str, bug_num: int) -> list[FailingTestRecord]:
        """Fetch triggering tests and test method source directly from Docker Defects4J."""
        proj, num = self._validate_bug_coords(project, bug_num)
        workdir = self.ensure_checkout(proj, num)

        rc_trig, out_trig, _ = self._run_shell(
            f"defects4j export -w {shlex.quote(workdir)} -p tests.trigger"
        )
        if rc_trig != 0 or not out_trig.strip():
            return []

        rc_dir, out_dir, _ = self._run_shell(
            f"defects4j export -w {shlex.quote(workdir)} -p dir.src.tests"
        )
        test_root = out_dir.strip().strip("/") if rc_dir == 0 and out_dir.strip() else "src/test"

        _, _, root_cause = _parse_d4j_info_output(self._get_d4j_info(proj, num))
        records: list[FailingTestRecord] = []

        for raw_trigger in _normalize_lf(out_trig).splitlines():
            trigger = raw_trigger.strip()
            if not trigger or "::" not in trigger:
                continue
            class_name, method_name = trigger.split("::", 1)
            rel_test_file = f"{test_root}/{class_name.replace('.', '/')}.java"
            test_source = self.fetch_source_file(proj, num, rel_test_file) or ""

            start_line = 1
            method_body = test_source
            if test_source:
                lines = test_source.splitlines()
                for mname, m_start, m_end in _extract_method_spans(test_source):
                    if mname == method_name:
                        start_line = m_start
                        method_body = "\n".join(lines[m_start - 1 : m_end])
                        break

            failure_msg = root_cause
            stack_trace = ""
            if self.run_trigger_tests:
                rc_test, out_test, err_test = self._run_shell(
                    f"defects4j test -w {shlex.quote(workdir)} -t {shlex.quote(trigger)}"
                )
                combined = _normalize_lf(out_test + "\n" + err_test).strip()
                if rc_test == 0 and combined:
                    failure_msg = combined.splitlines()[0]
                    stack_trace = combined

            records.append(
                FailingTestRecord(
                    test_file=rel_test_file,
                    test_method_name=method_name,
                    start_line=start_line,
                    source_code=method_body,
                    numbered_source=_number_lines(method_body, start_line),
                    failed_line_start=None,
                    failed_line_end=None,
                    failure_message=failure_msg,
                    stack_trace=stack_trace,
                )
            )
        return records

    def build_function_slices_for_hunks(
        self, project: str, bug_num: int, hunks: list[DiffHunk]
    ) -> list[SubPatchSlice]:
        """Hydrate clean pre-patch `SubPatchSlice` objects for diff hunks via Docker checkout."""
        proj, num = self._validate_bug_coords(project, bug_num)
        slices: list[SubPatchSlice] = []
        seen_keys: set[tuple[str, int]] = set()

        for hunk in hunks:
            if not hunk.file_path:
                continue
            source = self.fetch_source_file(proj, num, hunk.file_path)
            if not source:
                continue
            lines = source.splitlines()
            best_span: tuple[str, int, int] | None = None
            best_len: int | None = None
            for mname, m_start, m_end in _extract_method_spans(source):
                if m_start - 3 <= hunk.anchor_start_line <= m_end + 3:
                    span_len = m_end - m_start
                    if best_len is None or span_len < best_len:
                        best_span = (mname, m_start, m_end)
                        best_len = span_len
            if best_span is None:
                continue
            _, f_start, f_end = best_span
            key = (hunk.file_path, f_start)
            if key in seen_keys:
                continue
            seen_keys.add(key)
            func_body = "\n".join(lines[f_start - 1 : f_end])
            slices.append(
                SubPatchSlice(
                    file_path=hunk.file_path,
                    start_line=f_start,
                    content=func_body,
                    is_fallback_diff=False,
                )
            )
        return slices
