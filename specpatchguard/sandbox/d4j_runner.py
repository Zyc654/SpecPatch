"""Defects4J checkout, compile, test execution, and structured failure parsing inside Docker."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
import json
from pathlib import Path
import re
import shlex
import time
from typing import Any, Callable, Literal, TypeVar
from xml.sax.saxutils import escape as _xml_escape

from ..core.cache_utils import (
    load_valid_cache,
    resolve_cache_path,
    sanitize_cache_segment,
    save_cache_atomic,
)
from ..core.config_loader import Settings
from ..data.pkl_loader import canonicalize_project_name, parse_d4j_bug_id
from .docker_backend import DockerSandboxBackend, SandboxError

T = TypeVar("T")

PROBE_PREFIX = "[SPECPATCHGUARD_PROBE]"

_PROJECT_RE = re.compile(r"^[A-Za-z][A-Za-z0-9]*$")
_PROP_NAME_RE = re.compile(r"^[a-z][a-z0-9._]*$")
_TEST_SPEC_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_$.]*(?:::[A-Za-z_][A-Za-z0-9_$]*)?$")
_CLASS_ONLY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_$.]*$")
_METHOD_ONLY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_$]*$")
_SUBDIR_SEG_RE = re.compile(r"^[A-Za-z0-9_.-]+$")
_FAILING_COUNT_RE = re.compile(r"^\s*Failing tests:\s*(\d+)\s*$", re.MULTILINE)
_FAILING_ITEM_RE = re.compile(r"^\s*-\s+([A-Za-z_][A-Za-z0-9_$.]*::[A-Za-z_][A-Za-z0-9_$]*)\s*$")
_COMPILE_ANT_LINE_RE = re.compile(r"^\s*Running ant \(compile[^\)]*\).*$", re.MULTILINE)
_COMPILE_FAIL_RE = re.compile(
    r"(?:Running ant \(compile[^\)]*\).*FAIL|Compile(?:d|ation)?\s+failed|\[\s*javac\s*\][^\n]*error:|^\s*[^:\n]+\.java:\d+:\s*error:)",
    re.IGNORECASE | re.MULTILINE,
)
_JUNIT_OK_RE = re.compile(r"^\s*OK\s*\((\d+)\s+tests?\)\s*$", re.MULTILINE)
_JUNIT_SUMMARY_RE = re.compile(r"^\s*Tests run:\s*\d+,\s*Failures:\s*(\d+)\s*$", re.MULTILINE)
_JUNIT_FAIL_HEADER_RE = re.compile(r"^\s*\d+\)\s+([A-Za-z_][A-Za-z0-9_$]*)\(([A-Za-z_][A-Za-z0-9_$.]*)\)\s*$")
_JUNIT_BARE_FAIL_HEADER_RE = re.compile(r"^\s*\d+\)\s+([A-Za-z_][A-Za-z0-9_$.]+)\s*$")


def parse_probe_traces(output: str) -> list[dict[str, Any]]:
    """Parse `[SPECPATCHGUARD_PROBE] {...}` JSON lines safely without crashing on malformed entries."""
    traces: list[dict[str, Any]] = []
    if not output:
        return traces
    for raw_line in output.replace("\r\n", "\n").splitlines():
        idx = raw_line.find(PROBE_PREFIX)
        if idx == -1:
            continue
        tail = raw_line[idx + len(PROBE_PREFIX) :].strip()
        if not tail:
            continue
        try:
            parsed = json.loads(tail)
            if isinstance(parsed, dict):
                traces.append(parsed)
            else:
                traces.append({"raw": tail, "value": parsed})
        except json.JSONDecodeError as exc:
            traces.append({"raw": tail, "parse_error": str(exc)})
    return traces


def _normalize_lf(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _validate_session_subdir(session_subdir: str | None) -> str:
    if session_subdir is None:
        return ""
    if not isinstance(session_subdir, str):
        raise SandboxError("session_subdir must be a string")
    if "\n" in session_subdir or "\r" in session_subdir or "\x00" in session_subdir:
        raise SandboxError("session_subdir cannot contain newline or NUL characters")
    cleaned = session_subdir.strip().replace("\\", "/").strip("/")
    if not cleaned:
        return ""
    parts = [part for part in cleaned.split("/") if part and part != "."]
    if not parts or any(part == ".." or not _SUBDIR_SEG_RE.fullmatch(part) for part in parts):
        raise SandboxError(f"Invalid session_subdir: {session_subdir!r}")
    return "/".join(parts)


@dataclass(frozen=True)
class D4jProjectLayout:
    """Resolved Defects4J checkout coordinates and directory/test metadata."""

    project: str
    bug_num: int
    bug_id: str
    version: str
    work_dir: str
    dir_src_classes: str
    dir_src_tests: str
    dir_bin_classes: str
    dir_bin_tests: str
    tests_trigger: list[str] = field(default_factory=list)
    tests_relevant: list[str] = field(default_factory=list)
    classes_modified: list[str] = field(default_factory=list)
    cp_test: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> D4jProjectLayout:
        return cls(
            project=str(data["project"]),
            bug_num=int(data["bug_num"]),
            bug_id=str(data["bug_id"]),
            version=str(data["version"]),
            work_dir=str(data["work_dir"]),
            dir_src_classes=str(data.get("dir_src_classes", "")),
            dir_src_tests=str(data.get("dir_src_tests", "")),
            dir_bin_classes=str(data.get("dir_bin_classes", "")),
            dir_bin_tests=str(data.get("dir_bin_tests", "")),
            tests_trigger=[str(x) for x in data.get("tests_trigger", [])],
            tests_relevant=[str(x) for x in data.get("tests_relevant", [])],
            classes_modified=[str(x) for x in data.get("classes_modified", [])],
            cp_test=str(data.get("cp_test", "")),
        )


@dataclass(frozen=True)
class CompileResult:
    """Result of running `defects4j compile` inside the sandbox."""

    success: bool
    returncode: int
    timed_out: bool
    duration_sec: float
    stdout: str
    stderr: str
    error_summary: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class TestFailureDetail:
    """Single parsed failure entry from Defects4J `failing_tests` file."""

    test_class: str
    test_method: str
    full_name: str
    exception_type: str
    message: str
    stack_trace: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


TestFailureDetail.__test__ = False  # Prevent pytest from collecting dataclass as a test class.


@dataclass
class D4jTestResult:
    """Normalized execution result from `defects4j test`."""

    success: bool
    compiled: bool
    returncode: int
    timed_out: bool
    duration_sec: float
    failing_count: int | None
    failing_tests: list[str] = field(default_factory=list)
    failure_details: list[TestFailureDetail] = field(default_factory=list)
    stdout: str = ""
    stderr: str = ""
    raw_failing_tests_file: str = ""
    probe_traces: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "success": self.success,
            "compiled": self.compiled,
            "returncode": self.returncode,
            "timed_out": self.timed_out,
            "duration_sec": self.duration_sec,
            "failing_count": self.failing_count,
            "failing_tests": list(self.failing_tests),
            "failure_details": [d.to_dict() for d in self.failure_details],
            "stdout": self.stdout,
            "stderr": self.stderr,
            "raw_failing_tests_file": self.raw_failing_tests_file,
            "probe_traces": [dict(t) for t in self.probe_traces],
        }


def extract_compile_error_summary(stdout: str, stderr: str, *, max_lines: int = 30) -> str:
    """Extract concise compiler/Ant error lines from `defects4j compile` or `test` output."""
    combined = _normalize_lf(f"{stdout}\n{stderr}").strip()
    if not combined:
        return ""
    picked: list[str] = []
    for line in combined.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        lower = stripped.lower()
        if (
            "error:" in lower
            or "build failed" in lower
            or "[javac]" in lower
            or "compile" in lower and "fail" in lower
            or stripped.startswith(("^", "symbol:", "location:"))
        ):
            picked.append(line)
            if len(picked) >= max_lines:
                break
    if picked:
        return "\n".join(picked)
    lines = combined.splitlines()
    return "\n".join(lines[-min(len(lines), max_lines) :])


def parse_d4j_test_stdout(stdout: str, stderr: str = "") -> tuple[int | None, list[str], bool]:
    """Parse `(failing_count, failing_tests, compile_failed)` from `defects4j test` output."""
    norm_out = _normalize_lf(stdout or "")
    norm_err = _normalize_lf(stderr or "")
    combined = f"{norm_out}\n{norm_err}"

    count_match = _FAILING_COUNT_RE.search(combined)
    failing_count = int(count_match.group(1)) if count_match else None

    failing_tests: list[str] = []
    seen: set[str] = set()
    in_failing_section = False
    for line in combined.splitlines():
        if _FAILING_COUNT_RE.match(line):
            in_failing_section = True
            continue
        item_match = _FAILING_ITEM_RE.match(line)
        if item_match and (in_failing_section or failing_count is not None):
            item = item_match.group(1)
            if item not in seen:
                seen.add(item)
                failing_tests.append(item)
        elif in_failing_section and line.strip() and not line.lstrip().startswith("-"):
            in_failing_section = False

    compile_failed = False
    if failing_count is None:
        ant_compile_lines = _COMPILE_ANT_LINE_RE.findall(combined)
        if ant_compile_lines and all("OK" in line and "FAIL" not in line for line in ant_compile_lines):
            # Compilation steps explicitly passed; subsequent BUILD FAILED is a test/runtime failure.
            compile_failed = False
        elif _COMPILE_FAIL_RE.search(combined):
            compile_failed = True
    return failing_count, failing_tests, compile_failed


def _build_failure_detail(
    test_class: str,
    test_method: str,
    full_name: str,
    body: list[str],
    max_trace_lines: int,
) -> TestFailureDetail:
    non_empty_body = [b for b in body if b.strip()]
    first_line = non_empty_body[0].strip() if non_empty_body else ""
    if ":" in first_line:
        exc_type, exc_msg = first_line.split(":", 1)
        exception_type = exc_type.strip()
        message = exc_msg.strip()
    else:
        exception_type = first_line
        message = ""

    extra_msg_lines: list[str] = []
    for extra in non_empty_body[1:]:
        stripped = extra.strip()
        if stripped.startswith(("at ", "...")):
            break
        extra_msg_lines.append(stripped)
    if extra_msg_lines:
        joined = "\n".join(extra_msg_lines)
        message = f"{message}\n{joined}".strip() if message else joined

    return TestFailureDetail(
        test_class=test_class,
        test_method=test_method,
        full_name=full_name,
        exception_type=exception_type,
        message=message,
        stack_trace="\n".join(body[:max_trace_lines]).strip(),
    )


def parse_junit_core_stdout(
    stdout: str,
    stderr: str = "",
    *,
    max_trace_lines: int = 40,
) -> tuple[int | None, list[str], list[TestFailureDetail]]:
    """Parse `org.junit.runner.JUnitCore` output into `(failing_count, failing_tests, details)`."""
    combined = _normalize_lf(f"{stdout or ''}\n{stderr or ''}")
    if not combined.strip():
        return None, [], []

    blocks: list[tuple[str, str, str, list[str]]] = []
    cur_cls = ""
    cur_method = ""
    cur_full = ""
    cur_body: list[str] = []
    in_block = False

    for line in combined.splitlines():
        m_fail = _JUNIT_FAIL_HEADER_RE.match(line)
        m_bare = _JUNIT_BARE_FAIL_HEADER_RE.match(line) if not m_fail else None
        if m_fail or m_bare:
            if in_block:
                blocks.append((cur_cls, cur_method, cur_full, cur_body))
            if m_fail:
                cur_method, cur_cls = m_fail.group(1), m_fail.group(2)
                cur_full = f"{cur_cls}::{cur_method}"
            else:
                assert m_bare is not None
                cur_cls = m_bare.group(1)
                cur_method = ""
                cur_full = cur_cls
            cur_body = []
            in_block = True
            continue
        if in_block:
            stripped = line.strip()
            if stripped == "FAILURES!!!" or _JUNIT_SUMMARY_RE.match(line):
                blocks.append((cur_cls, cur_method, cur_full, cur_body))
                in_block = False
                cur_body = []
                continue
            cur_body.append(line)

    if in_block:
        blocks.append((cur_cls, cur_method, cur_full, cur_body))

    details = [
        _build_failure_detail(cls, method, full_name, body, max_trace_lines)
        for cls, method, full_name, body in blocks
    ]
    failing_tests = [d.full_name for d in details]

    summary_match = _JUNIT_SUMMARY_RE.search(combined)
    if summary_match:
        failing_count: int | None = int(summary_match.group(1))
    elif _JUNIT_OK_RE.search(combined):
        failing_count = 0
    elif details:
        failing_count = len(details)
    else:
        failing_count = None

    return failing_count, failing_tests, details


def parse_failing_tests_file(content: str, *, max_trace_lines: int = 40) -> list[TestFailureDetail]:
    """Parse Defects4J `failing_tests` file entries (`--- pkg.Class::method`)."""
    if not content or not content.strip():
        return []
    lines = _normalize_lf(content).splitlines()
    blocks: list[tuple[str, list[str]]] = []
    current_header: str | None = None
    current_body: list[str] = []

    for line in lines:
        if line.startswith("---"):
            if current_header is not None:
                blocks.append((current_header, current_body))
            current_header = line[3:].strip()
            current_body = []
        elif current_header is not None:
            current_body.append(line)

    if current_header is not None:
        blocks.append((current_header, current_body))

    details: list[TestFailureDetail] = []
    for header, body in blocks:
        if "::" in header:
            test_class, test_method = [part.strip() for part in header.split("::", 1)]
            full_name = f"{test_class}::{test_method}"
        else:
            test_class = header.strip()
            test_method = ""
            full_name = test_class

        non_empty_body = [b for b in body if b.strip()]
        first_line = non_empty_body[0].strip() if non_empty_body else ""
        if ":" in first_line:
            exc_type, exc_msg = first_line.split(":", 1)
            exception_type = exc_type.strip()
            message = exc_msg.strip()
        else:
            exception_type = first_line
            message = ""

        # Append continuation message lines before stack frames (`\tat ...`).
        extra_msg_lines: list[str] = []
        for extra in non_empty_body[1:]:
            stripped = extra.strip()
            if stripped.startswith("at ") or stripped.startswith("..."):
                break
            extra_msg_lines.append(stripped)
        if extra_msg_lines:
            joined = "\n".join(extra_msg_lines)
            message = f"{message}\n{joined}".strip() if message else joined

        truncated_trace = "\n".join(body[:max_trace_lines]).strip()
        details.append(
            TestFailureDetail(
                test_class=test_class,
                test_method=test_method,
                full_name=full_name,
                exception_type=exception_type,
                message=message,
                stack_trace=truncated_trace,
            )
        )
    return details


def _parse_batched_props(stdout: str, prop_names: tuple[str, ...]) -> dict[str, str]:
    values: dict[str, str] = {}
    normalized = _normalize_lf(stdout or "")
    for prop in prop_names:
        start_marker = f"__SPG_PROP_{prop}_START__"
        end_marker = f"__SPG_PROP_{prop}_END__"
        start_idx = normalized.find(start_marker)
        if start_idx == -1:
            values[prop] = ""
            continue
        start_idx += len(start_marker)
        end_idx = normalized.find(end_marker, start_idx)
        if end_idx == -1:
            values[prop] = normalized[start_idx:].strip()
        else:
            values[prop] = normalized[start_idx:end_idx].strip()
    return values


def _split_nonempty_lines(raw: str) -> list[str]:
    return [line.strip() for line in _normalize_lf(raw).splitlines() if line.strip()]


class Defects4JRunner:
    """Defects4J lifecycle runner inside a `DockerSandboxBackend` (adapts `Multi-Hunk-APR/d4j.py`)."""

    def __init__(
        self,
        backend: DockerSandboxBackend,
        *,
        compile_timeout_sec: int = 900,
        test_timeout_sec: int = 900,
        cache_dir: Path | None = None,
        force_refresh: bool = False,
    ) -> None:
        if type(compile_timeout_sec) is not int or compile_timeout_sec < 1:
            raise SandboxError("compile_timeout_sec must be an integer >= 1")
        if type(test_timeout_sec) is not int or test_timeout_sec < 1:
            raise SandboxError("test_timeout_sec must be an integer >= 1")
        self.backend = backend
        self.compile_timeout_sec = compile_timeout_sec
        self.test_timeout_sec = test_timeout_sec
        self.cache_dir = Path(cache_dir) if cache_dir is not None else None
        self.force_refresh = bool(force_refresh)

    @classmethod
    def from_settings(
        cls,
        backend: DockerSandboxBackend,
        settings: Settings,
        *,
        cache_dir: Path | None = None,
        force_refresh: bool | None = None,
    ) -> Defects4JRunner:
        """Construct a `Defects4JRunner` from validated `Settings`."""
        return cls(
            backend=backend,
            compile_timeout_sec=settings.defects4j["compile_timeout_sec"],
            test_timeout_sec=settings.defects4j["test_timeout_sec"],
            cache_dir=cache_dir if cache_dir is not None else settings.spec_cache_dir,
            force_refresh=settings.force_refresh if force_refresh is None else bool(force_refresh),
        )

    def _layout_cache_path(
        self,
        bug_id: str,
        version: str,
        session_subdir: str | None = None,
    ) -> Path | None:
        if self.cache_dir is None:
            return None
        safe_bug = sanitize_cache_segment(bug_id)
        safe_ver = sanitize_cache_segment(version)
        if session_subdir:
            safe_sub = sanitize_cache_segment(session_subdir)
            filename = f"{safe_bug}__{safe_ver}__{safe_sub}__layout.json"
        else:
            filename = f"{safe_bug}__{safe_ver}__layout.json"
        return resolve_cache_path(self.cache_dir, "d4j_meta", filename)

    @staticmethod
    def _resolve_bug_coords(project_or_bug_id: str, bug_num: int | None) -> tuple[str, int, str]:
        if bug_num is None:
            try:
                proj, num, bug_id = parse_d4j_bug_id(project_or_bug_id)
            except Exception as exc:
                raise SandboxError(str(exc)) from None
        else:
            if type(bug_num) is not int or bug_num < 1:
                raise SandboxError(f"Invalid Defects4J bug number: {bug_num!r}")
            try:
                proj = canonicalize_project_name(project_or_bug_id)
            except Exception as exc:
                raise SandboxError(str(exc)) from None
            num = bug_num
            bug_id = f"{proj}-{num}"

        if not _PROJECT_RE.fullmatch(proj):
            raise SandboxError(f"Invalid Defects4J project name: {proj!r}")
        return proj, num, bug_id

    def checkout(
        self,
        project_or_bug_id: str,
        bug_num: int | None = None,
        *,
        version_suffix: Literal["b", "f"] = "b",
        force_fresh: bool = False,
        session_subdir: str | None = None,
        force_refresh: bool | None = None,
        ensure_eclipse_files: bool = False,
    ) -> D4jProjectLayout:
        """Checkout a Defects4J bug inside the container, reset cleanly, and export layout metadata."""
        if version_suffix not in {"b", "f"}:
            raise SandboxError(f"version_suffix must be 'b' or 'f', got {version_suffix!r}")
        proj, num, bug_id = self._resolve_bug_coords(project_or_bug_id, bug_num)
        subdir = _validate_session_subdir(session_subdir)
        base_root = self.backend.container_work_root.rstrip("/") or "/"
        parent_dir = f"{base_root}/{subdir}" if subdir else base_root
        rel_workdir = f"{subdir}/{proj}_{num}{version_suffix}" if subdir else f"{proj}_{num}{version_suffix}"
        workdir = f"{parent_dir}/{proj}_{num}{version_suffix}"
        version = f"{num}{version_suffix}"
        base_ref = f"D4J_{proj}_{num}_{'BUGGY' if version_suffix == 'b' else 'FIXED'}_VERSION"

        if force_fresh:
            checkout_script = (
                f"rm -rf -- {shlex.quote(workdir)} && "
                f"mkdir -p -- {shlex.quote(parent_dir)} && "
                f"defects4j checkout -p {shlex.quote(proj)} -v {shlex.quote(version)} -w {shlex.quote(workdir)}"
            )
        else:
            checkout_script = (
                f"if [ -f {shlex.quote(workdir + '/.defects4j.config')} ]; then "
                "echo '__SPG_CHECKOUT_REUSED__'; "
                "else "
                f"rm -rf -- {shlex.quote(workdir)} && "
                f"mkdir -p -- {shlex.quote(parent_dir)} && "
                f"defects4j checkout -p {shlex.quote(proj)} -v {shlex.quote(version)} -w {shlex.quote(workdir)}; "
                "fi"
            )

        co_res = self.backend.exec_bash(
            checkout_script,
            workdir=self.backend.container_work_root,
            with_d4j_env=True,
            use_inner_timeout=True,
        )
        if co_res.timed_out or co_res.returncode != 0:
            raise SandboxError(
                f"Failed to checkout {bug_id} ({version}) at '{workdir}': "
                f"{co_res.stderr.strip() or co_res.stdout.strip() or f'rc={co_res.returncode}'}"
            )

        self.backend.set_active_workdir(workdir, base_ref=base_ref, state="prepatch")
        reused_checkout = "__SPG_CHECKOUT_REUSED__" in co_res.stdout
        if reused_checkout:
            self.backend.restore_prepatch(remove_injected=False)

        eff_refresh = self.force_refresh if force_refresh is None else bool(force_refresh)
        cache_path = self._layout_cache_path(bug_id, version, session_subdir=subdir)
        if cache_path is not None and reused_checkout and not force_fresh and not eff_refresh:
            cached = load_valid_cache(
                cache_path,
                force_refresh=False,
                validator=lambda d: (
                    d.get("bug_id") == bug_id
                    and d.get("version") == version
                    and d.get("work_dir_rel") == rel_workdir
                    and isinstance(d.get("layout"), dict)
                    and d["layout"].get("work_dir") == workdir
                    and bool(d["layout"].get("dir_src_classes"))
                    and bool(d["layout"].get("dir_src_tests"))
                ),
            )
            if cached is not None:
                try:
                    cached_layout = D4jProjectLayout.from_dict(cached["layout"])
                    if ensure_eclipse_files:
                        self.ensure_eclipse_project_files(cached_layout)
                    return cached_layout
                except (KeyError, TypeError, ValueError):
                    pass

        prop_names = (
            "dir.src.classes",
            "dir.src.tests",
            "dir.bin.classes",
            "dir.bin.tests",
            "tests.trigger",
            "tests.relevant",
            "classes.modified",
            "cp.test",
        )
        optional_props = {"tests.relevant", "classes.modified", "cp.test"}
        batch_parts: list[str] = []
        for prop in prop_names:
            if prop in optional_props:
                batch_parts.append(
                    f"(printf '__SPG_PROP_{prop}_START__\\n'; "
                    f"defects4j export -w {shlex.quote(workdir)} -p {shlex.quote(prop)} || true; "
                    f"printf '\\n__SPG_PROP_{prop}_END__\\n')"
                )
            else:
                batch_parts.append(
                    f"printf '__SPG_PROP_{prop}_START__\\n' && "
                    f"defects4j export -w {shlex.quote(workdir)} -p {shlex.quote(prop)} && "
                    f"printf '\\n__SPG_PROP_{prop}_END__\\n'"
                )
        exp_res = self.backend.exec_bash(
            " && ".join(batch_parts),
            workdir=workdir,
            with_d4j_env=True,
            use_inner_timeout=True,
        )
        if exp_res.timed_out or exp_res.returncode != 0:
            raise SandboxError(
                f"Failed exporting Defects4J properties for {bug_id}: "
                f"{exp_res.stderr.strip() or exp_res.stdout.strip() or f'rc={exp_res.returncode}'}"
            )

        props = _parse_batched_props(exp_res.stdout, prop_names)
        if not props["dir.src.classes"] or not props["dir.src.tests"]:
            raise SandboxError(
                f"Incomplete Defects4J layout for {bug_id}: missing dir.src.classes or dir.src.tests"
            )

        layout = D4jProjectLayout(
            project=proj,
            bug_num=num,
            bug_id=bug_id,
            version=version,
            work_dir=workdir,
            dir_src_classes=props["dir.src.classes"].strip("/"),
            dir_src_tests=props["dir.src.tests"].strip("/"),
            dir_bin_classes=props["dir.bin.classes"].strip("/"),
            dir_bin_tests=props["dir.bin.tests"].strip("/"),
            tests_trigger=_split_nonempty_lines(props["tests.trigger"]),
            tests_relevant=_split_nonempty_lines(props["tests.relevant"]),
            classes_modified=_split_nonempty_lines(props["classes.modified"]),
            cp_test=props["cp.test"].strip(),
        )
        if cache_path is not None:
            save_cache_atomic(
                cache_path,
                {
                    "bug_id": bug_id,
                    "version": version,
                    "work_dir_rel": rel_workdir,
                    "layout": layout.to_dict(),
                },
            )
        if ensure_eclipse_files:
            self.ensure_eclipse_project_files(layout)
        return layout

    def ensure_eclipse_project_files(self, layout: D4jProjectLayout) -> bool:
        """Generate minimal `.project` and `.classpath` for Ant-only Defects4J repos so `jdtls` resolves symbols."""
        workdir = layout.work_dir
        probe = self.backend.exec_bash(
            (
                "if [ -f pom.xml ] || [ -f build.gradle ] || [ -f build.gradle.kts ] "
                "|| [ -f .project ] || [ -f .classpath ]; then "
                "echo '__SPG_HAS_BUILD_DESC__'; "
                "fi"
            ),
            workdir=workdir,
            with_d4j_env=False,
            use_inner_timeout=False,
        )
        if probe.returncode == 0 and "__SPG_HAS_BUILD_DESC__" in probe.stdout:
            return False

        proj_name = _xml_escape(f"{layout.project}_{layout.version}")
        project_xml = (
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            "<projectDescription>\n"
            f"  <name>{proj_name}</name>\n"
            "  <comment></comment>\n"
            "  <projects></projects>\n"
            "  <buildSpec>\n"
            "    <buildCommand>\n"
            "      <name>org.eclipse.jdt.core.javabuilder</name>\n"
            "      <arguments></arguments>\n"
            "    </buildCommand>\n"
            "  </buildSpec>\n"
            "  <natures>\n"
            "    <nature>org.eclipse.jdt.core.javanature</nature>\n"
            "  </natures>\n"
            "</projectDescription>\n"
        )

        cp_lines = ['<?xml version="1.0" encoding="UTF-8"?>', "<classpath>"]
        for src_path in (layout.dir_src_classes, layout.dir_src_tests):
            cleaned_src = src_path.strip("/")
            if cleaned_src:
                cp_lines.append(f'  <classpathentry kind="src" path="{_xml_escape(cleaned_src)}"/>')
        cp_lines.append('  <classpathentry kind="con" path="org.eclipse.jdt.launching.JRE_CONTAINER"/>')
        seen_libs: set[str] = set()
        for entry in layout.cp_test.split(":"):
            cleaned = entry.strip()
            if cleaned and cleaned.endswith(".jar") and cleaned not in seen_libs:
                seen_libs.add(cleaned)
                cp_lines.append(f'  <classpathentry kind="lib" path="{_xml_escape(cleaned)}"/>')
        output_bin = layout.dir_bin_classes.strip("/") or "bin"
        cp_lines.append(f'  <classpathentry kind="output" path="{_xml_escape(output_bin)}"/>')
        cp_lines.append("</classpath>\n")
        classpath_xml = "\n".join(cp_lines)

        prev_workdir = self.backend.active_work_root
        prev_state = self.backend.state
        prev_base_ref = self.backend.base_ref
        switched_workdir = prev_workdir != workdir
        if switched_workdir:
            self.backend.set_active_workdir(workdir, base_ref=prev_base_ref, state=prev_state)
        try:
            self.backend.write_file(".project", project_xml, track_injected=False)
            self.backend.write_file(".classpath", classpath_xml, track_injected=False)
            self.backend.exec_bash(
                (
                    "if [ -d .git ]; then "
                    "mkdir -p .git/info && touch .git/info/exclude && "
                    'grep -qxF ".project" .git/info/exclude || echo ".project" >> .git/info/exclude; '
                    'grep -qxF ".classpath" .git/info/exclude || echo ".classpath" >> .git/info/exclude; '
                    "fi"
                ),
                workdir=workdir,
                with_d4j_env=False,
                use_inner_timeout=False,
            )
        finally:
            if switched_workdir and prev_workdir is not None:
                self.backend.set_active_workdir(prev_workdir, base_ref=prev_base_ref, state=prev_state)
        return True

    def export_property(self, prop_name: str, *, workdir: str | None = None) -> str:
        """Export a single Defects4J property from `workdir` (or active workdir)."""
        if not isinstance(prop_name, str) or not _PROP_NAME_RE.fullmatch(prop_name.strip()):
            raise SandboxError(f"Invalid Defects4J property name: {prop_name!r}")
        target_dir = workdir or self.backend.active_work_root
        if not target_dir:
            raise SandboxError("No active workdir configured; call checkout() or pass workdir")
        clean_prop = prop_name.strip()
        res = self.backend.exec_bash(
            f"defects4j export -w {shlex.quote(target_dir)} -p {shlex.quote(clean_prop)}",
            workdir=target_dir,
            with_d4j_env=True,
            use_inner_timeout=True,
        )
        if res.timed_out or res.returncode != 0:
            raise SandboxError(
                f"Failed exporting property '{clean_prop}': "
                f"{res.stderr.strip() or res.stdout.strip() or f'rc={res.returncode}'}"
            )
        return _normalize_lf(res.stdout).strip()

    def compile(self, *, workdir: str | None = None, timeout: int | None = None) -> CompileResult:
        """Run `defects4j compile` in `workdir` (or active workdir)."""
        target_dir = workdir or self.backend.active_work_root
        if not target_dir:
            raise SandboxError("No active workdir configured; call checkout() or pass workdir")
        eff_timeout = self.compile_timeout_sec if timeout is None else timeout
        start = time.monotonic()
        res = self.backend.exec_bash(
            f"defects4j compile -w {shlex.quote(target_dir)}",
            workdir=target_dir,
            timeout=eff_timeout,
            with_d4j_env=True,
            use_inner_timeout=True,
        )
        duration = time.monotonic() - start
        success = res.returncode == 0 and not res.timed_out
        summary = "" if success else extract_compile_error_summary(res.stdout, res.stderr)
        return CompileResult(
            success=success,
            returncode=res.returncode,
            timed_out=res.timed_out,
            duration_sec=duration,
            stdout=res.stdout,
            stderr=res.stderr,
            error_summary=summary,
        )

    def _read_failing_tests_file(self, workdir: str) -> str:
        res = self.backend.exec_command(
            ["sh", "-c", "if [ -s failing_tests ]; then cat -- failing_tests; fi"],
            workdir=workdir,
            timeout=min(30, self.test_timeout_sec),
        )
        if res.returncode != 0 or res.timed_out:
            return ""
        return res.stdout

    def run_tests(
        self,
        *,
        single_test: str | None = None,
        test_class: str | None = None,
        relevant_only: bool = False,
        workdir: str | None = None,
        timeout: int | None = None,
    ) -> D4jTestResult:
        """Run `defects4j test` (full, `-r`, `-t class::method`, or `-t class`) and parse failures."""
        if single_test is not None and test_class is not None:
            raise SandboxError("Specify at most one of single_test or test_class")
        selector = single_test if single_test is not None else test_class
        if selector is not None:
            cleaned = selector.strip()
            if not cleaned or not _TEST_SPEC_RE.fullmatch(cleaned):
                raise SandboxError(f"Invalid Defects4J test selector: {selector!r}")
            selector = cleaned

        target_dir = workdir or self.backend.active_work_root
        if not target_dir:
            raise SandboxError("No active workdir configured; call checkout() or pass workdir")
        eff_timeout = self.test_timeout_sec if timeout is None else timeout

        cmd_parts = [f"defects4j test -w {shlex.quote(target_dir)}"]
        if selector is not None:
            cmd_parts.append(f"-t {shlex.quote(selector)}")
        elif relevant_only:
            cmd_parts.append("-r")

        # Clear stale failing_tests file first so an aborted/compile-failed run never reads old failures.
        full_cmd = f"rm -f -- {shlex.quote(target_dir + '/failing_tests')} && " + " ".join(cmd_parts)
        start = time.monotonic()
        res = self.backend.exec_bash(
            full_cmd,
            workdir=target_dir,
            timeout=eff_timeout,
            with_d4j_env=True,
            use_inner_timeout=True,
        )
        duration = time.monotonic() - start

        failing_count, failing_tests, compile_failed = parse_d4j_test_stdout(res.stdout, res.stderr)
        compiled = not compile_failed
        raw_failing_file = self._read_failing_tests_file(target_dir) if compiled and not res.timed_out else ""
        failure_details = parse_failing_tests_file(raw_failing_file)

        if not failing_tests and failure_details:
            failing_tests = [item.full_name for item in failure_details]
        if failing_count is None and failure_details:
            failing_count = len(failure_details)

        probe_traces = parse_probe_traces(f"{res.stdout}\n{res.stderr}")
        success = (
            res.returncode == 0
            and not res.timed_out
            and compiled
            and failing_count == 0
        )
        return D4jTestResult(
            success=success,
            compiled=compiled,
            returncode=res.returncode,
            timed_out=res.timed_out,
            duration_sec=duration,
            failing_count=failing_count,
            failing_tests=failing_tests,
            failure_details=failure_details,
            stdout=res.stdout,
            stderr=res.stderr,
            raw_failing_tests_file=raw_failing_file,
            probe_traces=probe_traces,
        )

    def run_junit_direct(
        self,
        layout: D4jProjectLayout,
        test_class: str,
        *,
        test_methods: list[str] | None = None,
        timeout: int | None = None,
    ) -> D4jTestResult:
        """Run a compiled JUnit 4 class via `org.junit.runner.JUnitCore` so passing stdout/stderr is preserved."""
        clean_class = test_class.strip() if isinstance(test_class, str) else ""
        if not clean_class or not _CLASS_ONLY_RE.fullmatch(clean_class):
            raise SandboxError(f"Invalid JUnit test_class: {test_class!r}")

        cleaned_methods: list[str] = []
        for method in test_methods or []:
            cleaned_m = method.strip() if isinstance(method, str) else ""
            if not cleaned_m or not _METHOD_ONLY_RE.fullmatch(cleaned_m):
                raise SandboxError(f"Invalid JUnit test method name: {method!r}")
            if cleaned_m not in cleaned_methods:
                cleaned_methods.append(cleaned_m)

        cp_raw = layout.cp_test.strip() or self.export_property("cp.test", workdir=layout.work_dir)
        cp_parts: list[str] = []
        for entry in [layout.dir_bin_tests, layout.dir_bin_classes, *cp_raw.split(":")]:
            cleaned_entry = entry.strip()
            if cleaned_entry and cleaned_entry not in cp_parts:
                cp_parts.append(cleaned_entry)
        if not cp_parts:
            raise SandboxError(f"Empty JUnit classpath for {layout.bug_id}")
        classpath = ":".join(cp_parts)

        eff_timeout = self.test_timeout_sec if timeout is None else timeout
        jvm_props = (
            f"-Dspecpatchguard.methods={shlex.quote(','.join(cleaned_methods))} "
            if cleaned_methods
            else ""
        )
        cmd = (
            f'"$JAVA_HOME/bin/java" {jvm_props}'
            f"-cp {shlex.quote(classpath)} "
            f"org.junit.runner.JUnitCore {shlex.quote(clean_class)}"
        )

        start = time.monotonic()
        res = self.backend.exec_bash(
            cmd,
            workdir=layout.work_dir,
            timeout=eff_timeout,
            with_d4j_env=True,
            use_inner_timeout=True,
        )
        duration = time.monotonic() - start

        failing_count, failing_tests, failure_details = parse_junit_core_stdout(res.stdout, res.stderr)
        probe_traces = parse_probe_traces(f"{res.stdout}\n{res.stderr}")
        success = (
            res.returncode == 0
            and not res.timed_out
            and failing_count == 0
        )
        return D4jTestResult(
            success=success,
            compiled=True,
            returncode=res.returncode,
            timed_out=res.timed_out,
            duration_sec=duration,
            failing_count=failing_count,
            failing_tests=failing_tests,
            failure_details=failure_details,
            stdout=res.stdout,
            stderr=res.stderr,
            raw_failing_tests_file="",
            probe_traces=probe_traces,
        )

    def run_trigger_tests(
        self,
        layout: D4jProjectLayout,
        *,
        timeout: int | None = None,
    ) -> list[D4jTestResult]:
        """Run each Defects4J triggering test in `layout.tests_trigger` individually."""
        results: list[D4jTestResult] = []
        for trigger in layout.tests_trigger:
            results.append(
                self.run_tests(single_test=trigger, workdir=layout.work_dir, timeout=timeout)
            )
        return results

    def run_on_patch(
        self,
        patch_content: str,
        test_fn: Callable[[], T],
        *,
        always_rollback: bool = True,
    ) -> tuple[bool, str, T | None]:
        """Apply `patch_content`, invoke `test_fn()`, and guarantee rollback in `finally`."""
        if self.backend.state != "prepatch":
            self.backend.restore_prepatch(remove_injected=True)

        applied, detail = self.backend.apply_patch(patch_content)
        if not applied:
            return False, detail, None

        try:
            outcome = test_fn()
            return True, detail, outcome
        finally:
            if always_rollback:
                self.backend.restore_prepatch(remove_injected=True)
