"""Temporary JUnit 4 test injector and reserved read-only line-level anchor probe injector."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
import json
from pathlib import PurePosixPath
import re
import shlex
import sys
from typing import Any, Literal, Protocol, runtime_checkable

from ..analysis.protocols import resolve_repo_rel_path
from ..data.schemas import HarnessRunResult, TierType
from .d4j_runner import (
    PROBE_PREFIX,
    D4jProjectLayout,
    D4jTestResult,
    Defects4JRunner,
    extract_compile_error_summary,
    parse_probe_traces,
)
from .docker_backend import DockerSandboxBackend, SandboxError

CASE_PREFIX = "[SPECPATCHGUARD_CASE]"

_PKG_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*$")
_CLASS_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_METHOD_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_UNICODE_ESCAPE_RE = re.compile(r"\\u[0-9a-fA-F]{4}")
_BRACELESS_HEADER_RE = re.compile(
    r"^(?:(?:}\s*)?else\s*$|if\s*\(.*\)\s*$|(?:}\s*)?else\s+if\s*\(.*\)\s*$|for\s*\(.*\)\s*$|while\s*\(.*\)\s*$)"
)
_MUTATING_ASSIGN_RE = re.compile(
    r"(?<![=!<>])=(?!=)|"
    r"\+=|-=|\*=|/=|%=|&=|\|=|\^=|<<=|>>=|>>>=|"
    r"\+\+|--"
)


def parse_case_records(
    output: str,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    """Parse `[SPECPATCHGUARD_CASE] {...}` lines into `(passed, failed, errors)`."""
    passed: list[dict[str, Any]] = []
    failed: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    if not output:
        return passed, failed, errors

    for raw_line in output.replace("\r\n", "\n").splitlines():
        idx = raw_line.find(CASE_PREFIX)
        if idx == -1:
            continue
        tail = raw_line[idx + len(CASE_PREFIX) :].strip()
        if not tail:
            continue
        try:
            record = json.loads(tail)
        except json.JSONDecodeError as exc:
            errors.append({"raw": tail, "parse_error": str(exc)})
            continue
        if not isinstance(record, dict):
            errors.append({"raw": tail, "error": "Case record JSON must be an object"})
            continue

        status = str(record.get("status", "")).lower()
        if status == "passed":
            passed.append(record)
        elif status == "failed":
            failed.append(record)
        else:
            errors.append(record)
    return passed, failed, errors


def _strip_java_string_and_char_literals(expr: str) -> str:
    """Replace string and char literal contents with spaces for safe operator inspection."""
    out: list[str] = []
    in_quote: str | None = None
    escape = False
    for ch in expr:
        if in_quote is not None:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == in_quote:
                in_quote = None
                out.append(ch)
            else:
                out.append(" ")
        else:
            if ch in {'"', "'"}:
                in_quote = ch
            out.append(ch)
    if in_quote is not None:
        raise SandboxError(f"Unterminated string/char literal in probe expression: {expr!r}")
    return "".join(out)


def _validate_readonly_java_expr(expr: str, *, field_name: str = "expression") -> str:
    """Reject mutating assignments, statements, blocks, and comments in probe Java expressions."""
    if not isinstance(expr, str) or not expr.strip():
        raise SandboxError(f"{field_name} cannot be empty")
    if "\n" in expr or "\r" in expr or "\x00" in expr:
        raise SandboxError(f"{field_name} must be a single-line expression without NUL/newlines")
    cleaned = expr.strip()
    stripped = _strip_java_string_and_char_literals(cleaned)
    if _UNICODE_ESCAPE_RE.search(stripped):
        raise SandboxError(f"{field_name} cannot contain Java Unicode escapes: {cleaned!r}")
    if "//" in stripped or "/*" in stripped or "*/" in stripped:
        raise SandboxError(f"{field_name} cannot contain comments: {cleaned!r}")
    if any(ch in stripped for ch in (";", "{", "}")):
        raise SandboxError(f"{field_name} cannot contain statement separators or blocks: {cleaned!r}")
    if _MUTATING_ASSIGN_RE.search(stripped):
        raise SandboxError(f"{field_name} must be read-only (mutating operator rejected): {cleaned!r}")
    return cleaned


def _java_string_literal(value: str) -> str:
    escaped = (
        value.replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("\n", "\\n")
        .replace("\r", "\\r")
        .replace("\t", "\\t")
    )
    return f'"{escaped}"'


def _java_json_escaped_expr(java_expr: str) -> str:
    return (
        f"String.valueOf((Object) ({java_expr}))"
        '.replace("\\\\", "\\\\\\\\")'
        '.replace("\\"", "\\\\\\"")'
        '.replace("\\n", "\\\\n")'
        '.replace("\\r", "\\\\r")'
        '.replace("\\t", "\\\\t")'
        '.replace("\\b", "\\\\b")'
        '.replace("\\f", "\\\\f")'
    )


def _strip_trailing_line_comment(line: str) -> str:
    in_quote: str | None = None
    escape = False
    for idx, ch in enumerate(line):
        if in_quote is not None:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == in_quote:
                in_quote = None
        else:
            if ch in {'"', "'"}:
                in_quote = ch
            elif ch == "/" and idx + 1 < len(line) and line[idx + 1] == "/":
                return line[:idx]
    return line


def _is_braceless_control_header(lines: list[str], target_idx: int) -> bool:
    """Return True if the preceding meaningful line opens a braceless control-flow body."""
    idx = target_idx - 1
    while idx >= 0:
        stripped = _strip_trailing_line_comment(lines[idx]).strip()
        if not stripped or (stripped.startswith("/*") and stripped.endswith("*/")):
            idx -= 1
            continue
        if stripped.endswith(("{", ";")):
            return False
        return _BRACELESS_HEADER_RE.match(stripped) is not None
    return False


@dataclass(frozen=True)
class AnchorProbeSpec:
    """Line-level read-only probe specification (shared across Tier-A optional traces and Tier-B)."""

    probe_id: str
    file_path: str
    line_number: int
    position: Literal["before", "after"] = "before"
    observed_expressions: list[str] = field(default_factory=list)
    condition_expr: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.probe_id, str) or not self.probe_id.strip():
            raise SandboxError("probe_id cannot be empty")
        if type(self.line_number) is not int or self.line_number < 1:
            raise SandboxError("line_number must be an integer >= 1")
        if self.position not in {"before", "after"}:
            raise SandboxError(f"position must be 'before' or 'after', got {self.position!r}")
        if self.condition_expr.strip():
            _validate_readonly_java_expr(self.condition_expr, field_name="condition_expr")
        for expr in self.observed_expressions:
            _validate_readonly_java_expr(expr, field_name="observed_expression")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> AnchorProbeSpec:
        return cls(
            probe_id=str(data["probe_id"]),
            file_path=str(data["file_path"]),
            line_number=int(data["line_number"]),
            position=data.get("position", "before"),
            observed_expressions=[str(x) for x in data.get("observed_expressions", [])],
            condition_expr=str(data.get("condition_expr", "")),
        )


@runtime_checkable
class AnchorProbeInjector(Protocol):
    """Reserved protocol for read-only anchor infection-condition probing (Tier-A & Tier-B)."""

    def render_probe_statement(self, probe: AnchorProbeSpec) -> str: ...

    def instrument_source(self, source: str, probes: list[AnchorProbeSpec]) -> str: ...

    def apply_probes(
        self, backend: DockerSandboxBackend, probes: list[AnchorProbeSpec]
    ) -> dict[str, str]: ...

    def revert_probes(self, backend: DockerSandboxBackend, backups: dict[str, str]) -> None: ...

    def parse_probe_traces(self, output: str) -> list[dict[str, Any]]: ...


class ReadOnlyAnchorProbeInjector:
    """Zero-dependency Java 8 read-only probe injector emitting `[SPECPATCHGUARD_PROBE]` JSON."""

    def render_probe_statement(self, probe: AnchorProbeSpec, *, indent: str = "") -> str:
        """Render a single-line try/catch probe print statement with zero side effects."""
        safe_file = resolve_repo_rel_path(probe.file_path, readonly_prepatch=False)
        prefix_json = (
            f"{PROBE_PREFIX} "
            + json.dumps(
                {
                    "probe_id": probe.probe_id,
                    "file": safe_file,
                    "line": probe.line_number,
                },
                ensure_ascii=False,
            )[:-1]
        )
        parts: list[str] = [_java_string_literal(prefix_json)]

        if probe.condition_expr.strip():
            cond = _validate_readonly_java_expr(probe.condition_expr, field_name="condition_expr")
            parts.append(_java_string_literal(',"cond":'))
            parts.append(f"(({cond}) ? \"true\" : \"false\")")

        parts.append(_java_string_literal(',"values":{'))
        for idx, raw_expr in enumerate(probe.observed_expressions):
            expr = _validate_readonly_java_expr(raw_expr, field_name="observed_expression")
            key_json = json.dumps(expr, ensure_ascii=False)
            sep = "," if idx > 0 else ""
            parts.append(_java_string_literal(f"{sep}{key_json}:\""))
            parts.append(_java_json_escaped_expr(expr))
            parts.append(_java_string_literal('"'))
        parts.append(_java_string_literal("}}"))

        expr_concat = " + ".join(parts)
        return (
            f"{indent}try {{ System.err.println({expr_concat}); }} "
            f"catch (Throwable _spg_probe_err) {{}}"
        )

    def instrument_source(self, source: str, probes: list[AnchorProbeSpec]) -> str:
        """Insert single-line probe statements into `source` in descending line order to avoid drift."""
        if not probes:
            return source
        had_trailing_nl = source.endswith("\n")
        newline = "\r\n" if "\r\n" in source else "\n"
        lines = source.replace("\r\n", "\n").replace("\r", "\n").split("\n")
        if had_trailing_nl and lines and lines[-1] == "":
            lines = lines[:-1]

        total_lines = len(lines)
        for probe in probes:
            if probe.line_number > total_lines:
                raise SandboxError(
                    f"Probe '{probe.probe_id}' line_number {probe.line_number} exceeds file line count {total_lines}"
                )

        # Group probes by target line and process bottom-to-top to prevent line-number drift.
        grouped_by_idx: dict[int, list[AnchorProbeSpec]] = {}
        for probe in probes:
            grouped_by_idx.setdefault(probe.line_number - 1, []).append(probe)

        for target_idx in sorted(grouped_by_idx, reverse=True):
            line_probes = grouped_by_idx[target_idx]
            raw_target = lines[target_idx]
            indent = raw_target[: len(raw_target) - len(raw_target.lstrip(" \t"))]
            before_stmts = [
                self.render_probe_statement(p, indent=indent)
                for p in line_probes
                if p.position == "before"
            ]
            after_stmts = [
                self.render_probe_statement(p, indent=indent)
                for p in line_probes
                if p.position == "after"
            ]

            if _is_braceless_control_header(lines, target_idx):
                target_no_comment = _strip_trailing_line_comment(raw_target).strip()
                if (
                    not target_no_comment
                    or target_no_comment.startswith("{")
                    or not target_no_comment.endswith(";")
                    or _BRACELESS_HEADER_RE.match(target_no_comment)
                ):
                    probe_ids = ", ".join(p.probe_id for p in line_probes)
                    raise SandboxError(
                        f"Probe(s) [{probe_ids}] target a multi-line braceless control-flow body at line {target_idx + 1}"
                    )
                inline_before = " ".join(s.strip() for s in before_stmts)
                inline_after = " ".join(s.strip() for s in after_stmts)
                body_parts = [part for part in (inline_before, raw_target.strip(), inline_after) if part]
                lines[target_idx] = f"{indent}{{ {' '.join(body_parts)} }}"
            else:
                for stmt in reversed(after_stmts):
                    lines.insert(target_idx + 1, stmt)
                for stmt in reversed(before_stmts):
                    lines.insert(target_idx, stmt)

        rendered = newline.join(lines)
        if had_trailing_nl:
            rendered += newline
        return rendered

    def apply_probes(
        self,
        backend: DockerSandboxBackend,
        probes: list[AnchorProbeSpec],
    ) -> dict[str, str]:
        """Back up target files, write instrumented versions, and auto-revert on partial failure."""
        if not probes:
            return {}

        grouped: dict[str, list[AnchorProbeSpec]] = {}
        for probe in probes:
            safe_rel = resolve_repo_rel_path(probe.file_path, readonly_prepatch=False)
            if ".git" in PurePosixPath(safe_rel).parts:
                raise SandboxError(f"Probing inside .git is forbidden: {safe_rel}")
            grouped.setdefault(safe_rel, []).append(probe)

        backups: dict[str, str] = {}
        written: dict[str, str] = {}
        try:
            for rel_path, file_probes in grouped.items():
                original = backend.read_file(rel_path, readonly_prepatch=False)
                backups[rel_path] = original
                instrumented = self.instrument_source(original, file_probes)
                backend.write_file(rel_path, instrumented, track_injected=False)
                written[rel_path] = original
        except Exception:
            if written:
                try:
                    self.revert_probes(backend, written)
                except Exception:
                    pass
            raise
        return backups

    def revert_probes(self, backend: DockerSandboxBackend, backups: dict[str, str]) -> None:
        """Restore exact pre-probe file contents across all backed-up files."""
        errors: list[str] = []
        for rel_path, original in backups.items():
            try:
                backend.write_file(rel_path, original, track_injected=False)
            except Exception as exc:
                errors.append(f"{rel_path}: {exc}")
        if errors:
            raise SandboxError("Failed reverting probe backups: " + "; ".join(errors))

    def parse_probe_traces(self, output: str) -> list[dict[str, Any]]:
        return parse_probe_traces(output)


@dataclass(frozen=True)
class InjectedTestSpec:
    """Descriptor for a temporary JUnit 4 test class injected into a Defects4J test tree."""

    package_name: str
    class_name: str
    java_source: str
    test_methods: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        pkg = self.package_name.strip()
        if pkg and not _PKG_RE.fullmatch(pkg):
            raise SandboxError(f"Invalid Java package_name: {self.package_name!r}")
        if not _CLASS_RE.fullmatch(self.class_name.strip()):
            raise SandboxError(f"Invalid Java class_name: {self.class_name!r}")
        if not isinstance(self.java_source, str) or not self.java_source.strip():
            raise SandboxError("java_source cannot be empty")
        class_decl_re = re.compile(rf"\bclass\s+{re.escape(self.class_name.strip())}\b")
        if not class_decl_re.search(self.java_source):
            raise SandboxError(
                f"java_source must declare class '{self.class_name.strip()}'"
            )
        for method in self.test_methods:
            if not _METHOD_RE.fullmatch(method.strip()):
                raise SandboxError(f"Invalid JUnit test method name: {method!r}")

    @property
    def fqcn(self) -> str:
        pkg = self.package_name.strip()
        cls = self.class_name.strip()
        return f"{pkg}.{cls}" if pkg else cls

    def relative_path(self, dir_src_tests: str) -> str:
        base = dir_src_tests.strip("/")
        pkg = self.package_name.strip()
        cls = self.class_name.strip()
        if pkg:
            rel = PurePosixPath(base) / PurePosixPath(*pkg.split(".")) / f"{cls}.java"
        else:
            rel = PurePosixPath(base) / f"{cls}.java"
        return resolve_repo_rel_path(rel.as_posix(), readonly_prepatch=False)

    def compiled_class_dir_and_prefix(self, dir_bin_tests: str) -> tuple[str, str]:
        base = dir_bin_tests.strip("/")
        pkg = self.package_name.strip()
        cls = self.class_name.strip()
        if pkg:
            folder = (PurePosixPath(base) / PurePosixPath(*pkg.split("."))).as_posix()
        else:
            folder = PurePosixPath(base).as_posix()
        return folder, cls


def _synthesize_cases_from_d4j_result(
    spec: InjectedTestSpec,
    test_res: D4jTestResult,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    """Synthesize `HarnessRunResult` case buckets when plain JUnit tests omit `[SPECPATCHGUARD_CASE]`."""
    failed: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    failed_methods: set[str] = set()

    for detail in test_res.failure_details:
        if detail.test_method:
            failed_methods.add(detail.test_method)
        bucket = failed if "Assertion" in detail.exception_type else errors
        bucket.append(
            {
                "case_id": detail.full_name,
                "status": "failed" if bucket is failed else "error",
                "exception_type": detail.exception_type,
                "message": detail.message,
                "stack_trace": detail.stack_trace,
            }
        )

    if not test_res.failure_details and test_res.failing_tests:
        for full_name in test_res.failing_tests:
            method = full_name.split("::", 1)[1] if "::" in full_name else full_name
            failed_methods.add(method)
            failed.append({"case_id": full_name, "status": "failed"})

    if not test_res.success and not failed and not errors:
        errors.append(
            {
                "stage": "test",
                "timed_out": test_res.timed_out,
                "returncode": test_res.returncode,
                "error": extract_compile_error_summary(test_res.stdout, test_res.stderr),
            }
        )

    passed: list[dict[str, Any]] = []
    if test_res.compiled and not test_res.timed_out:
        candidate_methods = spec.test_methods or ([spec.fqcn] if test_res.success else [])
        for method in candidate_methods:
            if method not in failed_methods and (test_res.success or spec.test_methods):
                case_id = method if "::" in method or method == spec.fqcn else f"{spec.fqcn}::{method}"
                passed.append({"case_id": case_id, "status": "passed"})
    return passed, failed, errors


class JUnitInjector:
    """Inject, compile, run, and cleanly roll back temporary JUnit 4 harnesses in Defects4J."""

    def __init__(
        self,
        backend: DockerSandboxBackend,
        runner: Defects4JRunner,
        probe_injector: AnchorProbeInjector | None = None,
    ) -> None:
        self.backend = backend
        self.runner = runner
        self.probe_injector: AnchorProbeInjector = probe_injector or ReadOnlyAnchorProbeInjector()

    def inject_test(
        self,
        spec: InjectedTestSpec,
        layout: D4jProjectLayout,
        *,
        allow_overwrite: bool = False,
    ) -> str:
        """Write temporary JUnit source into `layout.dir_src_tests` without clobbering existing tests."""
        rel_path = spec.relative_path(layout.dir_src_tests)
        if not allow_overwrite and self.backend.file_exists(rel_path):
            raise SandboxError(
                f"Refusing to overwrite existing test file '{rel_path}' (pass allow_overwrite=True to override)"
            )
        return self.backend.write_file(rel_path, spec.java_source, track_injected=True)

    def remove_injected_test(self, spec: InjectedTestSpec, layout: D4jProjectLayout) -> None:
        """Remove injected `.java` source and any stale compiled `.class` / inner-class artifacts."""
        rel_java = spec.relative_path(layout.dir_src_tests)
        self.backend.remove_file(rel_java)
        if layout.dir_bin_tests:
            cls_dir, cls_name = spec.compiled_class_dir_and_prefix(layout.dir_bin_tests)
            cls_dir_q = shlex.quote(cls_dir)
            # Remove exact and inner-class bytecode so Ant/Defects4J never reuses stale classes.
            self.backend.exec_bash(
                (
                    f"rm -f -- {cls_dir_q}/{cls_name}.class && "
                    f'for f in {cls_dir_q}/{cls_name}\\$*.class; do [ -e "$f" ] && rm -f -- "$f"; done; true'
                ),
                workdir=layout.work_dir,
                with_d4j_env=False,
                use_inner_timeout=False,
            )

    def _run_d4j_methods_or_class(
        self,
        spec: InjectedTestSpec,
        layout: D4jProjectLayout,
        *,
        timeout: int | None,
    ) -> D4jTestResult:
        if not spec.test_methods:
            return self.runner.run_tests(test_class=spec.fqcn, workdir=layout.work_dir, timeout=timeout)

        aggregated_failing: list[str] = []
        aggregated_details = []
        aggregated_probes: list[dict[str, Any]] = []
        stdout_parts: list[str] = []
        stderr_parts: list[str] = []
        raw_fail_parts: list[str] = []
        total_duration = 0.0
        last_rc = 0
        any_timed_out = False
        compiled_ok = True

        for method in spec.test_methods:
            res = self.runner.run_tests(
                single_test=f"{spec.fqcn}::{method}",
                workdir=layout.work_dir,
                timeout=timeout,
            )
            total_duration += res.duration_sec
            last_rc = res.returncode if res.returncode != 0 else last_rc
            if res.stdout:
                stdout_parts.append(res.stdout)
            if res.stderr:
                stderr_parts.append(res.stderr)
            if res.raw_failing_tests_file:
                raw_fail_parts.append(res.raw_failing_tests_file)
            aggregated_probes.extend(res.probe_traces)
            for item in res.failing_tests:
                if item not in aggregated_failing:
                    aggregated_failing.append(item)
            aggregated_details.extend(res.failure_details)
            if not res.compiled:
                compiled_ok = False
                break
            if res.timed_out:
                any_timed_out = True
                break

        failing_count = len(aggregated_failing) if compiled_ok and not any_timed_out else None
        success = compiled_ok and not any_timed_out and last_rc == 0 and failing_count == 0
        return D4jTestResult(
            success=success,
            compiled=compiled_ok,
            returncode=last_rc,
            timed_out=any_timed_out,
            duration_sec=total_duration,
            failing_count=failing_count,
            failing_tests=aggregated_failing,
            failure_details=aggregated_details,
            stdout="\n".join(stdout_parts),
            stderr="\n".join(stderr_parts),
            raw_failing_tests_file="\n".join(raw_fail_parts),
            probe_traces=aggregated_probes,
        )

    def run_injected_test(
        self,
        spec: InjectedTestSpec,
        layout: D4jProjectLayout,
        *,
        tier: TierType = "tier_a",
        probes: list[AnchorProbeSpec] | None = None,
        compile_first: bool = True,
        always_cleanup: bool = True,
        execution_mode: Literal["auto", "junit_direct", "d4j"] = "auto",
        timeout: int | None = None,
    ) -> HarnessRunResult:
        """Inject test (and optional probes), compile, execute, parse `HarnessRunResult`, and clean up."""
        if execution_mode not in {"auto", "junit_direct", "d4j"}:
            raise SandboxError(f"Unsupported execution_mode: {execution_mode!r}")

        probe_backups: dict[str, str] = {}
        test_injected = False
        try:
            self.inject_test(spec, layout, allow_overwrite=False)
            test_injected = True

            if probes:
                probe_backups = self.probe_injector.apply_probes(self.backend, probes)

            compile_stdout = ""
            compile_stderr = ""
            if compile_first:
                comp = self.runner.compile(workdir=layout.work_dir, timeout=timeout)
                compile_stdout = comp.stdout
                compile_stderr = comp.stderr
                if not comp.success:
                    return HarnessRunResult(
                        tier=tier,
                        compiled=False,
                        error_cases=[
                            {
                                "stage": "compile",
                                "timed_out": comp.timed_out,
                                "returncode": comp.returncode,
                                "error": comp.error_summary or "Compilation failed",
                            }
                        ],
                        raw_stdout=comp.stdout,
                        raw_stderr=comp.stderr,
                    )

            use_direct = execution_mode == "junit_direct" or (
                execution_mode == "auto" and bool(layout.cp_test.strip())
            )
            if use_direct:
                test_res = self.runner.run_junit_direct(
                    layout,
                    spec.fqcn,
                    test_methods=spec.test_methods or None,
                    timeout=timeout,
                )
            else:
                test_res = self._run_d4j_methods_or_class(spec, layout, timeout=timeout)
            combined_stdout = (
                f"{compile_stdout}\n{test_res.stdout}".strip() if compile_stdout else test_res.stdout
            )
            combined_stderr = (
                f"{compile_stderr}\n{test_res.stderr}".strip() if compile_stderr else test_res.stderr
            )

            if not test_res.compiled:
                return HarnessRunResult(
                    tier=tier,
                    compiled=False,
                    error_cases=[
                        {
                            "stage": "compile",
                            "timed_out": test_res.timed_out,
                            "returncode": test_res.returncode,
                            "error": extract_compile_error_summary(test_res.stdout, test_res.stderr)
                            or "Compilation failed during defects4j test",
                        }
                    ],
                    raw_stdout=combined_stdout,
                    raw_stderr=combined_stderr,
                )

            combined_logs = f"{test_res.stdout}\n{test_res.stderr}"
            passed, failed, errors = parse_case_records(combined_logs)
            if not (passed or failed or errors):
                passed, failed, errors = _synthesize_cases_from_d4j_result(spec, test_res)
            else:
                # Preserve mid-run timeouts or uncaught runner crashes even when partial CASE records exist.
                if test_res.timed_out:
                    errors.append(
                        {
                            "stage": "test",
                            "timed_out": True,
                            "returncode": test_res.returncode,
                            "error": extract_compile_error_summary(test_res.stdout, test_res.stderr)
                            or "Test execution timed out after partial case output",
                        }
                    )
                elif not test_res.success and not failed and not errors:
                    _, syn_failed, syn_errors = _synthesize_cases_from_d4j_result(spec, test_res)
                    failed.extend(syn_failed)
                    errors.extend(syn_errors)

            return HarnessRunResult(
                tier=tier,
                compiled=True,
                passed_cases=passed,
                failed_cases=failed,
                error_cases=errors,
                infection_probe_traces=list(test_res.probe_traces),
                raw_stdout=combined_stdout,
                raw_stderr=combined_stderr,
            )
        finally:
            if always_cleanup:
                cleanup_errors: list[str] = []
                if probe_backups:
                    try:
                        self.probe_injector.revert_probes(self.backend, probe_backups)
                    except Exception as exc:
                        cleanup_errors.append(f"revert_probes: {exc}")
                if test_injected:
                    try:
                        self.remove_injected_test(spec, layout)
                    except Exception as exc:
                        cleanup_errors.append(f"remove_injected_test: {exc}")
                if cleanup_errors and sys.exc_info()[0] is None:
                    raise SandboxError("Sandbox cleanup failed: " + "; ".join(cleanup_errors))
