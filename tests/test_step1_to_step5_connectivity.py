"""End-to-end connectivity tests across Steps 1 through 5 using real local data, tools, Python, and JDK 8 JVM."""

from __future__ import annotations

import io
import json
from pathlib import Path
import queue
import re
import shutil
import subprocess
from types import SimpleNamespace
from typing import Any

import pytest

from specpatchguard.agents import IntentContextAgent, OpenAILLMClient, PbtSpecAgent
from specpatchguard.agents.pbt_agent import SpecRefinementResult
from specpatchguard.analysis.ast_context import JavaAstContextTools
from specpatchguard.analysis.lsp_client import (
    LspNavigationTools,
    encode_lsp_message,
    read_lsp_message,
)
from specpatchguard.analysis.protocols import LocalRepoExecutor
from specpatchguard.analysis.repo_tools import RepoSearchTools
from specpatchguard.analysis.tool_registry import ToolDispatcher
from specpatchguard.core.config_loader import Settings, load_settings
from specpatchguard.core.doctor import run_doctor
from specpatchguard.data import (
    DockerD4jFallbackProvider,
    FurinaDataLoader,
    build_initial_intent_pack,
    strip_to_patch_blind,
)
from specpatchguard.data.schemas import BugReport, RawPatchRecord
from specpatchguard.prompts.pbt_prompts import CROSS_REP_END_MARKER, CROSS_REP_START_MARKER
from specpatchguard.sandbox.d4j_runner import (
    CompileResult,
    D4jProjectLayout,
    D4jTestResult,
    TestFailureDetail,
    parse_probe_traces,
)
from specpatchguard.sandbox.junit_injector import (
    JUnitInjector,
    ReadOnlyAnchorProbeInjector,
)
from specpatchguard.strategies import (
    PublicEntryAnchorProbeHarness,
    StandaloneFunctionUnitHarness,
    StrategyRegistry,
    TierAPythonHypothesisStrategy,
    TierBAbstractDecisionTableStrategy,
)

REPO_ROOT_REAL = Path("E:/workspace/myown/SpecPatchGuard")

_JUNIT_TEST_ANNOTATION_JAVA = """\
package org.junit;

import java.lang.annotation.ElementType;
import java.lang.annotation.Retention;
import java.lang.annotation.RetentionPolicy;
import java.lang.annotation.Target;

@Retention(RetentionPolicy.RUNTIME)
@Target(ElementType.METHOD)
public @interface Test {
}
"""

_JUNIT_ASSERT_JAVA = """\
package org.junit;

public class Assert {
    public static void fail(String message) {
        throw new AssertionError(message);
    }

    public static void assertTrue(boolean condition) {
        if (!condition) {
            throw new AssertionError("Expected true");
        }
    }

    public static void assertEquals(Object expected, Object actual) {
        if (!java.util.Objects.equals(expected, actual)) {
            throw new AssertionError("Expected " + expected + " but got " + actual);
        }
    }
}
"""

_MINI_RUNNER_JAVA = """\
import java.lang.reflect.InvocationTargetException;
import java.lang.reflect.Method;
import org.junit.Test;

public class SpGuardJUnitRunner {
    public static void main(String[] args) throws Exception {
        if (args.length < 1) {
            System.err.println("Missing test class FQCN");
            System.exit(2);
        }
        String fqcn = args[0];
        Class<?> cls = Class.forName(fqcn);
        java.util.List<String> targets = new java.util.ArrayList<String>();
        if (args.length > 1) {
            for (int i = 1; i < args.length; i++) {
                targets.add(args[i]);
            }
        } else {
            for (Method m : cls.getMethods()) {
                if (m.isAnnotationPresent(Test.class)) {
                    targets.add(m.getName());
                }
            }
        }
        int failures = 0;
        for (String mname : targets) {
            Method m = cls.getMethod(mname);
            Object instance = cls.getDeclaredConstructor().newInstance();
            try {
                m.invoke(instance);
            } catch (InvocationTargetException ite) {
                failures++;
                Throwable cause = ite.getCause() != null ? ite.getCause() : ite;
                System.err.println(failures + ") " + mname + "(" + fqcn + ")");
                cause.printStackTrace(System.err);
            }
        }
        System.out.println("Tests run: " + targets.size() + ", Failures: " + failures);
        if (failures > 0) {
            System.exit(1);
        }
    }
}
"""

_STRING_UTILS_JAVA = """\
package org.apache.commons.lang;

public class StringUtils {
    public static final String EMPTY = "";

    public static int indexOf(String str, String searchStr, int startPos) {
        if (str == null || searchStr == null) {
            return -1;
        }
        if (searchStr.length() == 0 && startPos >= str.length()) {
            return str.length();
        }
        return str.indexOf(searchStr, startPos);
    }

    public static String defaultString(String str) {
        return str == null ? EMPTY : str;
    }

    public static String demoCaller() {
        return WordUtils.abbreviate("hello world", 0, 5, "...");
    }
}
"""


def _build_local_settings(tmp_path: Path) -> Settings:
    """Load real `config/config.json` + `llm_config.json`, redirecting only outputs and dummy `.env`."""
    raw_cfg = json.loads((REPO_ROOT_REAL / "config" / "config.json").read_text(encoding="utf-8"))
    real_base = load_settings(REPO_ROOT_REAL / "config" / "config.json")

    cfg_dir = tmp_path / "config"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    llm_cfg_src = (REPO_ROOT_REAL / "config" / "llm_config.json").read_text(encoding="utf-8")
    (cfg_dir / "llm_config.json").write_text(llm_cfg_src, encoding="utf-8")

    raw_cfg["data"] = {
        key: str(val) if val is not None else None for key, val in real_base.data.items()
    }
    raw_cfg["api"]["llm_config_path"] = "./llm_config.json"
    raw_cfg["output"] = {
        "result_dir": str(tmp_path / "out"),
        "spec_cache_dir": str(tmp_path / "out" / "frozen_specs"),
        "resume": True,
    }
    cfg_path = cfg_dir / "config.json"
    cfg_path.write_text(json.dumps(raw_cfg), encoding="utf-8")

    env_path = tmp_path / ".env"
    env_path.write_text(
        "OPENAI_API_KEY=sk-local-connectivity-test\n"
        "OPENAI_API_BASE=https://api.example.invalid/v1\n"
        "LLM_MODEL=deepseek-chat\n",
        encoding="utf-8",
    )
    return load_settings(cfg_path, env_path=env_path)


class _ScriptedOpenAISDK:
    """Fake OpenAI SDK object exercising real `OpenAILLMClient` request-building & tool loops."""

    def __init__(self, scripted_steps: list[dict[str, Any]]) -> None:
        self._steps = list(scripted_steps)
        self.recorded_requests: list[dict[str, Any]] = []
        self.recorded_prompt_texts: list[str] = []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    def _create(self, **kwargs: Any) -> Any:
        self.recorded_requests.append(kwargs)
        for msg in kwargs.get("messages", []):
            content = msg.get("content", "")
            if content is not None:
                self.recorded_prompt_texts.append(str(content))

        if not self._steps:
            raise AssertionError("No scripted LLM response left for request")
        step = self._steps.pop(0)

        tool_calls_objs = []
        for idx, tc in enumerate(step.get("tool_calls", [])):
            tool_calls_objs.append(
                SimpleNamespace(
                    id=tc.get("id", f"call_{idx}"),
                    type="function",
                    function=SimpleNamespace(
                        name=tc["name"],
                        arguments=(
                            tc["arguments"]
                            if isinstance(tc["arguments"], str)
                            else json.dumps(tc["arguments"])
                        ),
                    ),
                )
            )
        msg_obj = SimpleNamespace(
            content=step.get("content", ""),
            reasoning_content=step.get("reasoning_content", ""),
            tool_calls=tool_calls_objs or None,
        )
        return SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=msg_obj,
                    finish_reason="tool_calls" if tool_calls_objs else "stop",
                )
            ],
            usage=SimpleNamespace(prompt_tokens=120, completion_tokens=45, total_tokens=165),
            model=kwargs.get("model", "deepseek-chat"),
        )


class _FramedMockLspTransport:
    """In-memory LSP transport exercising real `encode_lsp_message` / `read_lsp_message` framing."""

    def __init__(self, caller_uri: str) -> None:
        self.caller_uri = caller_uri
        self.sent_methods: list[str] = []
        self._inbox: queue.Queue[bytes] = queue.Queue()
        self.closed = False

    def send(self, payload: dict[str, Any]) -> None:
        framed_out = encode_lsp_message(payload)
        decoded = read_lsp_message(io.BytesIO(framed_out))
        method = str(decoded.get("method", ""))
        self.sent_methods.append(method)
        req_id = decoded.get("id")
        if req_id is None:
            return
        if method == "initialize":
            reply = {"jsonrpc": "2.0", "id": req_id, "result": {"capabilities": {"hoverProvider": True}}}
        elif method == "textDocument/references":
            reply = {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": [
                    {
                        "uri": self.caller_uri,
                        "range": {
                            "start": {"line": 19, "character": 15},
                            "end": {"line": 19, "character": 25},
                        },
                    }
                ],
            }
        elif method == "textDocument/hover":
            reply = {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "contents": {
                        "kind": "markdown",
                        "value": "```java\npublic static String abbreviate(String str, int lower, int upper, String appendToEnd)\n```",
                    }
                },
            }
        else:
            reply = {"jsonrpc": "2.0", "id": req_id, "result": None}
        self._inbox.put(encode_lsp_message(reply))

    def receive(self, timeout: float = 5.0) -> dict[str, Any]:
        raw = self._inbox.get(timeout=timeout)
        return read_lsp_message(io.BytesIO(raw))

    def close(self) -> None:
        self.closed = True


class _LocalJvmSandboxBackend:
    """Minimal pre-patch sandbox backend over a temp Java workspace for real `JUnitInjector`."""

    def __init__(self, repo_root: Path) -> None:
        self.repo_root = repo_root
        self.state = "prepatch"
        self.injected_files: list[str] = []
        self.removed_files: list[str] = []
        self.bash_commands: list[str] = []

    def _resolve(self, rel_path: str) -> Path:
        norm = rel_path.replace("\\", "/").lstrip("/")
        target = (self.repo_root / norm).resolve()
        root_resolved = self.repo_root.resolve()
        if target != root_resolved and root_resolved not in target.parents:
            raise ValueError(f"Path escapes repo root: {rel_path}")
        return target

    def file_exists(self, rel_path: str) -> bool:
        return self._resolve(rel_path).exists()

    def read_file(self, rel_path: str, *, readonly_prepatch: bool = False) -> str:
        return self._resolve(rel_path).read_text(encoding="utf-8")

    def write_file(self, rel_path: str, content: str, *, track_injected: bool = False) -> str:
        target = self._resolve(rel_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        if track_injected:
            self.injected_files.append(rel_path)
        return str(target)

    def remove_file(self, rel_path: str) -> None:
        target = self._resolve(rel_path)
        if target.exists():
            target.unlink()
        self.removed_files.append(rel_path)

    def exec_bash(
        self,
        command: str,
        *,
        workdir: str | None = None,
        with_d4j_env: bool = False,
        use_inner_timeout: bool = True,
    ) -> tuple[int, str, str]:
        self.bash_commands.append(command)
        if "SpecPatchGuardGeneratedTest" in command and ".class" in command:
            test_cls_dir = self.repo_root / "target" / "test-classes" / "org" / "apache" / "commons" / "lang"
            if test_cls_dir.exists():
                for p in test_cls_dir.glob("SpecPatchGuardGeneratedTest*.class"):
                    p.unlink()
        return 0, "", ""


class _LocalJvmD4jRunner:
    """Compile and execute JDK 8 Java sources + injected JUnit 4 harness on the real local JVM."""

    def __init__(self, repo_root: Path) -> None:
        self.repo_root = repo_root

    def compile(self, *, workdir: str, timeout: int | None = None) -> CompileResult:
        main_bin = self.repo_root / "target" / "classes"
        test_bin = self.repo_root / "target" / "test-classes"
        main_bin.mkdir(parents=True, exist_ok=True)
        test_bin.mkdir(parents=True, exist_ok=True)

        java_files = sorted(str(p) for p in (self.repo_root / "src").rglob("*.java"))
        proc_main = subprocess.run(
            ["javac", "--release", "8", "-encoding", "UTF-8", "-d", str(main_bin), *java_files],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout or 60,
        )
        if proc_main.returncode != 0:
            return CompileResult(
                success=False,
                returncode=proc_main.returncode,
                timed_out=False,
                duration_sec=0.1,
                stdout=proc_main.stdout,
                stderr=proc_main.stderr,
                error_summary=(proc_main.stderr or proc_main.stdout).strip(),
            )
        return CompileResult(
            success=True,
            returncode=0,
            timed_out=False,
            duration_sec=0.1,
            stdout=proc_main.stdout,
            stderr=proc_main.stderr,
        )

    def run_junit_direct(
        self,
        layout: D4jProjectLayout,
        fqcn: str,
        *,
        test_methods: list[str] | None = None,
        timeout: int | None = None,
    ) -> D4jTestResult:
        cp = f"{self.repo_root / 'target' / 'classes'}"
        argv = ["java", "-cp", cp, "SpGuardJUnitRunner", fqcn, *(test_methods or [])]
        proc = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout or 60,
        )
        combined = f"{proc.stdout}\n{proc.stderr}"
        probes = parse_probe_traces(combined)
        failing_tests: list[str] = []
        details: list[TestFailureDetail] = []
        header_re = re.compile(r"^\s*\d+\)\s+([A-Za-z_][A-Za-z0-9_$]*)\(([^)]+)\)\s*$")
        lines = proc.stderr.splitlines()
        for idx, line in enumerate(lines):
            m = header_re.match(line)
            if m:
                method, cls = m.group(1), m.group(2)
                full = f"{cls}::{method}"
                failing_tests.append(full)
                next_line = lines[idx + 1] if idx + 1 < len(lines) else ""
                exc_type, _, msg = next_line.partition(":")
                details.append(
                    TestFailureDetail(
                        test_class=cls,
                        test_method=method,
                        full_name=full,
                        exception_type=exc_type.strip() or "java.lang.AssertionError",
                        message=msg.strip(),
                        stack_trace="\n".join(lines[idx : idx + 6]),
                    )
                )
        return D4jTestResult(
            success=(proc.returncode == 0),
            compiled=True,
            returncode=proc.returncode,
            timed_out=False,
            duration_sec=0.1,
            failing_count=len(failing_tests),
            failing_tests=failing_tests,
            failure_details=details,
            stdout=proc.stdout,
            stderr=proc.stderr,
            probe_traces=probes,
        )


def _materialize_lang45_repo(repo_root: Path, buggy_abbreviate_code: str) -> tuple[Path, int]:
    """Create minimal Java repo with real `Lang-45` `WordUtils.abbreviate` aligned to line 605."""
    lang_dir = repo_root / "src" / "java" / "org" / "apache" / "commons" / "lang"
    junit_dir = repo_root / "src" / "java" / "org" / "junit"
    runner_dir = repo_root / "src" / "java"
    lang_dir.mkdir(parents=True, exist_ok=True)
    junit_dir.mkdir(parents=True, exist_ok=True)

    (junit_dir / "Test.java").write_text(_JUNIT_TEST_ANNOTATION_JAVA, encoding="utf-8")
    (junit_dir / "Assert.java").write_text(_JUNIT_ASSERT_JAVA, encoding="utf-8")
    (runner_dir / "SpGuardJUnitRunner.java").write_text(_MINI_RUNNER_JAVA, encoding="utf-8")
    (lang_dir / "StringUtils.java").write_text(_STRING_UTILS_JAVA, encoding="utf-8")

    header_lines = [
        "package org.apache.commons.lang;",
        "",
        "public class WordUtils {",
        "    private static final String DEFAULT_DELIM = \" \";",
    ]
    while len(header_lines) < 604:
        header_lines.append(f"    // filler line {len(header_lines) + 1}")
    indented_method = ["    " + line if line else "" for line in buggy_abbreviate_code.splitlines()]
    full_lines = [*header_lines, *indented_method, "}"]
    word_utils_path = lang_dir / "WordUtils.java"
    word_utils_path.write_text("\n".join(full_lines) + "\n", encoding="utf-8")

    # Locate exact 1-based line of `if (upper < lower) {` inside `abbreviate`.
    probe_line = next(
        idx + 1 for idx, line in enumerate(full_lines) if "if (upper < lower)" in line
    )
    return word_utils_path, probe_line


def test_full_step1_to_step5_real_lang45_connectivity_with_real_jvm(tmp_path: Path) -> None:
    """Exercise Steps 1->2->4->5+3 end-to-end with real dataset, Python subprocess, and JDK 8 JVM."""
    if not (shutil.which("javac") and shutil.which("java")):
        pytest.skip("javac/java not available on PATH")

    # --- Step 1: Config + Local Doctor ---
    settings = _build_local_settings(tmp_path)
    doctor_report = run_doctor(settings, mode="local")
    checks_by_name = {c["name"]: c for c in doctor_report["checks"]}
    assert checks_by_name["data_files"]["status"] == "pass"
    assert checks_by_name["output_safety"]["status"] == "pass"

    # --- Step 2: Real furina_context_data loading + Patch-Blind + Tier routing ---
    loader = FurinaDataLoader.from_settings(settings)
    load_res = loader.load_dataset(source=[{"bug_id": "Lang-45"}], limit=1)
    assert len(load_res.records) == 1
    raw_record: RawPatchRecord = load_res.records[0]
    assert raw_record.bug_id == "Lang-45"

    task = strip_to_patch_blind(raw_record, verify=True)
    initial_pack = build_initial_intent_pack(task)
    assert initial_pack.tier == "tier_a"
    assert initial_pack.target_methods and initial_pack.target_methods[0].method_name == "abbreviate"
    assert initial_pack.anchors[0].function_start_line == 605

    # Materialize pre-patch repo containing real buggy Lang-45 WordUtils.abbreviate
    repo_root = tmp_path / "lang45_repo"
    word_utils_path, probe_line = _materialize_lang45_repo(
        repo_root, initial_pack.anchors[0].buggy_function_code
    )
    pre_probe_bytes = word_utils_path.read_bytes()

    # --- Step 4: Real ToolDispatcher (RepoSearch + AST + Framed Mock LSP) ---
    executor = LocalRepoExecutor(repo_root, state="prepatch", readonly_prepatch=True)
    string_utils_uri = (repo_root / "src/java/org/apache/commons/lang/StringUtils.java").resolve().as_uri()
    lsp_transport = _FramedMockLspTransport(caller_uri=string_utils_uri)
    lsp_tools = LspNavigationTools(
        lsp_transport,
        executor,
        settings.lsp,
        settings.tools,
        work_root=str(repo_root),
        readonly_prepatch=True,
    )
    dispatcher = ToolDispatcher(
        RepoSearchTools(executor, settings.tools, readonly_prepatch=True),
        JavaAstContextTools(executor, settings.tools, readonly_prepatch=True),
        settings.tools,
        lsp_tools=lsp_tools,
    )

    # --- Scripted responses driving REAL OpenAILLMClient -> IntentAgent + CrossRep + PbtSpecAgent ---
    cross_rep_python = f"""```python
import json

def ref_abbreviate(s, lower, upper, append_to_end):
    if s is None:
        return None
    if len(s) == 0:
        return ""
    if lower > len(s):
        lower = len(s)
    if upper == -1 or upper > len(s):
        upper = len(s)
    if upper < lower:
        upper = lower
    idx = s.find(" ", lower)
    suffix = append_to_end if append_to_end is not None else ""
    if idx == -1:
        return s[:upper] + (suffix if upper != len(s) else "")
    if idx > upper:
        return s[:upper] + suffix
    return s[:idx] + suffix

if __name__ == "__main__":
    cases = [
        {{"case_id": "cross_null", "args": [None, 0, 5, "..."], "category": "boundary", "expected_behavior": "null returns null", "expected_return": None}},
        {{"case_id": "cross_empty", "args": ["", 0, 5, "..."], "category": "boundary", "expected_behavior": "empty returns empty", "expected_return": ""}},
        {{"case_id": "cross_lower_gt_len", "args": ["abcdef", 12, 18, None], "category": "bug_trigger", "expected_behavior": "lower > len must clamp without StringIndexOutOfBoundsException", "expected_return": ref_abbreviate("abcdef", 12, 18, None)}},
        {{"case_id": "cross_negative_lower", "args": ["0123456789", -5, 5, "..."], "category": "noise_out_of_domain", "expected_behavior": "valid", "expected_return": "01234..."}},
    ]
    print("{CROSS_REP_START_MARKER}")
    print(json.dumps(cases))
    print("{CROSS_REP_END_MARKER}")
```"""

    scripted_steps = [
        # 1) IntentContextAgent tool round 1 (calls real AST + LSP tools)
        {
            "content": "",
            "tool_calls": [
                {
                    "id": "call_branches",
                    "name": "get_enclosing_branches",
                    "arguments": {
                        "file_path": "src/java/org/apache/commons/lang/WordUtils.java",
                        "line": 622,
                    },
                },
                {
                    "id": "call_members",
                    "name": "get_class_members",
                    "arguments": {
                        "file_path": "src/java/org/apache/commons/lang/WordUtils.java",
                    },
                },
                {
                    "id": "call_refs",
                    "name": "lsp_find_references",
                    "arguments": {
                        "file_path": "src/java/org/apache/commons/lang/WordUtils.java",
                        "line": 605,
                        "character": 26,
                        "exclude_tests": True,
                    },
                },
            ],
        },
        # 2) IntentContextAgent final JSON
        {
            "content": json.dumps(
                {
                    "bug_intent_summary": "WordUtils.abbreviate must clamp lower > str.length() instead of throwing StringIndexOutOfBoundsException.",
                    "root_cause_hypothesis": "lower exceeds str.length() before StringUtils.indexOf and substring.",
                    "anchor_analysis": ["if (upper < lower) raises upper above str.length()"],
                    "boundary_notes": ["public static String abbreviate(String, int, int, String)", "lower > str.length()"],
                    "recommended_tier": "tier_a",
                    "entry_harness_hint": "WordUtils.abbreviate",
                }
            )
        },
        # 3) PbtSpecAgent initial spec extraction (no tool calls needed on follow-up)
        {
            "content": json.dumps(
                {
                    "preconditions": ["str may be null or non-null", "lower >= 0 in normal calls"],
                    "postconditions": [
                        "when lower > str.length(), abbreviate clamps lower without StringIndexOutOfBoundsException",
                        "valid strings within length return expected abbreviated prefix",
                    ],
                    "invariants": ["returns null iff str == null"],
                    "exception_contracts": ["never throws StringIndexOutOfBoundsException"],
                    "anchor_infection_conditions": ["lower > str.length()"],
                    "receiver_setup_java": "",
                    "seed_inputs": [
                        {
                            "case_id": "seed_valid_abbrev",
                            "args": ["0123456789", 0, 5, "..."],
                            "category": "normal",
                            "expected_behavior": "abbreviate first 5 chars",
                            "expected_return": "01234...",
                        },
                        {
                            "case_id": "seed_lower_gt_len",
                            "args": ["0123456789", 15, 20, None],
                            "category": "bug_trigger",
                            "expected_behavior": "return full string without exception",
                            "expected_return": "0123456789",
                        },
                    ],
                    "anchor_probes": [
                        {
                            "probe_id": "probe_upper_lt_lower",
                            "file_path": "src/java/org/apache/commons/lang/WordUtils.java",
                            "line_number": probe_line,
                            "position": "before",
                            "condition_expr": "lower > str.length()",
                            "observed_expressions": ["lower", "upper", "str.length()"],
                        }
                    ],
                }
            )
        },
        # 4) Cross-Rep clean-room Python generator (executed for REAL via python -B subprocess)
        {"content": cross_rep_python},
        # 5) PbtSpecAgent Round 1 refinement after REAL JVM execution
        {
            "content": json.dumps(
                {
                    "preconditions": ["lower >= 0 when str != null"],
                    "postconditions": [
                        "WordUtils.abbreviate('0123456789', 15, 20, null) returns '0123456789'",
                        "WordUtils.abbreviate('0123456789', 0, 5, '...') == '01234...'",
                    ],
                    "invariants": ["returns null when str is null"],
                    "exception_contracts": ["no StringIndexOutOfBoundsException when lower > str.length()"],
                    "anchor_infection_conditions": ["lower > str.length()"],
                    "excluded_case_ids": ["cross_negative_lower"],
                    "seed_inputs": [],
                    "converged": True,
                    "refinement_notes": "Confirmed lower > str.length() trigger on real JVM; excluded negative lower noise.",
                }
            )
        },
    ]

    sdk = _ScriptedOpenAISDK(scripted_steps)
    llm_client = OpenAILLMClient(settings, sdk_factory=lambda **kw: sdk)

    # --- Step 5 (Agent 1) ---
    intent_agent = IntentContextAgent(llm_client, settings)
    enriched_pack = intent_agent.explore(task, dispatcher=dispatcher, include_lsp=True)

    assert len(enriched_pack.boundary_context.enclosing_branches) == 1
    assert any(f.get("name") == "DEFAULT_DELIM" for f in enriched_pack.boundary_context.class_fields)
    assert len(enriched_pack.boundary_context.callers) == 1
    assert "StringUtils.java" in enriched_pack.boundary_context.callers[0]["file_path"]
    assert "textDocument/references" in lsp_transport.sent_methods

    # --- Step 5 + Step 3 (Agent 2 + Cross-Rep + Real JUnit 4 + Real javac --release 8 + Real JVM + Real Probes) ---
    registry = StrategyRegistry.default(llm_client, hypothesis_available=False)
    local_backend = _LocalJvmSandboxBackend(repo_root)
    local_runner = _LocalJvmD4jRunner(repo_root)
    junit_injector = JUnitInjector(
        backend=local_backend,  # type: ignore[arg-type]
        runner=local_runner,  # type: ignore[arg-type]
        probe_injector=ReadOnlyAnchorProbeInjector(),
    )
    layout = D4jProjectLayout(
        project="Lang",
        bug_num=45,
        bug_id="Lang-45",
        version="45b",
        work_dir=str(repo_root),
        dir_src_classes="src/java",
        dir_src_tests="src/java",
        dir_bin_classes="target/classes",
        dir_bin_tests="target/test-classes",
        cp_test="target/classes",
    )

    pbt_agent = PbtSpecAgent(
        llm_client,
        settings,
        registry=registry,
        junit_injector=junit_injector,
    )
    spec_result = pbt_agent.generate_and_refine(
        enriched_pack,
        layout=layout,
        dispatcher=dispatcher,
        persist=True,
    )

    assert spec_result.status == "ok"
    assert spec_result.tier == "tier_a"
    assert spec_result.cross_rep_result is not None
    assert not spec_result.cross_rep_result.diagnostics.get("degraded", False)
    assert len(spec_result.cross_rep_result.candidate_inputs) == 4

    # Real JVM confirmed bug witness cases (`StringIndexOutOfBoundsException` on buggy Lang-45)
    confirmed_ids = {c.case_id for c in spec_result.witness_bank.confirmed_bug_witnesses}
    assert "seed_lower_gt_len" in confirmed_ids
    assert "cross_lower_gt_len" in confirmed_ids
    assert any(
        "StringIndexOutOfBoundsException" in c.observed_buggy_outcome
        for c in spec_result.witness_bank.confirmed_bug_witnesses
    )

    # Real JVM passing cases + refinement exclusion
    passing_ids = {c.case_id for c in spec_result.witness_bank.boundary_passing_witnesses}
    assert {"seed_valid_abbrev", "cross_null", "cross_empty"}.issubset(passing_ids)
    excluded_ids = {c.case_id for c in spec_result.witness_bank.excluded_irrelevant_cases}
    assert "cross_negative_lower" in excluded_ids
    assert "cross_negative_lower" not in passing_ids

    # Real JVM `[SPECPATCHGUARD_PROBE]` stderr trace verification
    traces = spec_result.witness_bank.infection_probe_traces
    assert len(traces) >= 1
    assert any(
        t.get("probe_id") == "probe_upper_lt_lower"
        and t.get("cond") is True
        and str(t.get("values", {}).get("lower")) == "15"
        for t in traces
    )

    # Probe reversion & injected test cleanup verification
    assert word_utils_path.read_bytes() == pre_probe_bytes
    assert not any(
        (repo_root / p).exists() for p in local_backend.injected_files
    )

    # Atomic persistence & JSON round-trip verification
    saved_files = list(settings.spec_cache_dir.rglob("*step5_spec.json"))
    assert len(saved_files) == 1
    reloaded = SpecRefinementResult.from_dict(
        json.loads(saved_files[0].read_text(encoding="utf-8"))
    )
    assert reloaded.bug_id == "Lang-45"
    assert len(reloaded.witness_bank.confirmed_bug_witnesses) >= 2

    # Strict Patch-Blind / Zero-Leakage verification across every OpenAI request prompt
    assert len(sdk.recorded_prompt_texts) >= 5
    forbidden_fragments = [
        line.lstrip("+").strip()
        for hunk in raw_record.hunks
        for line in hunk.added_lines
        if len(line.lstrip("+").strip()) >= 8
    ]
    for prompt in sdk.recorded_prompt_texts:
        for frag in forbidden_fragments:
            assert frag not in prompt
        if raw_record.patch_id:
            assert raw_record.patch_id not in prompt
        if raw_record.tool:
            assert raw_record.tool not in prompt
    lsp_tools.close()


def test_real_tier_b_record_connectivity_routes_to_placeholder(tmp_path: Path) -> None:
    """Verify real Tier-B dataset record flows cleanly through Steps 1->2->5 reserved placeholder."""
    settings = _build_local_settings(tmp_path)
    loader = FurinaDataLoader.from_settings(settings)
    load_res = loader.load_dataset(source=[{"bug_id": "Closure-1"}], limit=1)
    if not load_res.records:
        all_recs = loader.load_all(limit=100)
        raw_record = next(
            r for r in all_recs if build_initial_intent_pack(strip_to_patch_blind(r)).tier == "tier_b"
        )
    else:
        raw_record = load_res.records[0]

    task = strip_to_patch_blind(raw_record, verify=True)
    initial_pack = build_initial_intent_pack(task)
    assert initial_pack.tier == "tier_b"

    sdk = _ScriptedOpenAISDK(
        [
            {
                "content": json.dumps(
                    {
                        "bug_intent_summary": "Stateful Tier-B defect routed to reserved strategy.",
                        "root_cause_hypothesis": "Control/state invariant violated.",
                        "anchor_analysis": [],
                        "boundary_notes": [],
                        "recommended_tier": "tier_b",
                        "entry_harness_hint": "",
                    }
                )
            },
            {
                "content": json.dumps(
                    {
                        "preconditions": ["state initialized"],
                        "postconditions": ["preserves invariant"],
                        "invariants": [],
                        "exception_contracts": [],
                        "anchor_infection_conditions": [],
                        "receiver_setup_java": "",
                        "seed_inputs": [],
                        "anchor_probes": [],
                    }
                )
            },
        ]
    )
    llm_client = OpenAILLMClient(settings, sdk_factory=lambda **kw: sdk)

    enriched = IntentContextAgent(llm_client, settings).explore(task)
    assert enriched.tier == "tier_b"

    registry = StrategyRegistry.default(llm_client)
    assert isinstance(registry.get_cross_rep("tier_b"), TierBAbstractDecisionTableStrategy)
    assert isinstance(registry.get_harness("tier_b"), PublicEntryAnchorProbeHarness)

    res = PbtSpecAgent(llm_client, settings, registry=registry).generate_and_refine(
        enriched, persist=True
    )
    assert res.status == "tier_b_placeholder"
    assert res.cross_rep_result is not None
    assert res.cross_rep_result.diagnostics["status"] == "disabled_placeholder"

    # Reserved Tier-B harness raises NotImplementedError until Step 8.
    with pytest.raises(NotImplementedError):
        registry.get_harness("tier_b").build_junit_source(res.pre_post_spec, [], enriched)


def test_patch_only_no_bug_report_connectivity_and_no_summary_leakage(tmp_path: Path) -> None:
    """Verify new patch-only records (no bug_report) hydrate safely without developer Summary leakage."""
    settings = _build_local_settings(tmp_path)
    loader = FurinaDataLoader.from_settings(settings)
    raw_lang45 = loader.load_dataset(source=[{"bug_id": "Lang-45"}], limit=1).records[0]

    sensitive_summary = "DEVELOPER_COMMIT_SUMMARY_LEAK_GUARD: Clamp lower when lower > str.length()"
    fake_d4j_info = (
        "Summary:\n"
        f"  {sensitive_summary}\n"
        "Root cause in triggering tests:\n"
        "  java.lang.StringIndexOutOfBoundsException: String index out of range: 15\n"
        "--------------------------------------------------------------------------------\n"
    )
    fallback = DockerD4jFallbackProvider.from_settings(
        settings,
        runner=lambda argv, timeout: (0, fake_d4j_info, ""),
    )
    assert fallback.include_d4j_summary is False
    hydrated_report = fallback.fetch_bug_report("Lang", 45)
    assert sensitive_summary not in hydrated_report.title
    assert sensitive_summary not in hydrated_report.description
    assert "StringIndexOutOfBoundsException" in hydrated_report.description

    patch_only_record = RawPatchRecord(
        patch_id="unlabeled_candidate_patch_001",
        project=raw_lang45.project,
        bug_num=raw_lang45.bug_num,
        bug_id=raw_lang45.bug_id,
        tool=None,
        label=None,
        raw_diff=raw_lang45.raw_diff,
        hunks=raw_lang45.hunks,
        sub_patches=raw_lang45.sub_patches,
        failing_tests=raw_lang45.failing_tests,
        bug_report=BugReport(
            bug_id=hydrated_report.bug_id,
            title=hydrated_report.title,
            description=hydrated_report.description,
            available=hydrated_report.available,
        ),
        metadata={"source": "new_candidate_patch_dir_no_bug_report"},
    )
    task = strip_to_patch_blind(patch_only_record, verify=True)

    sdk = _ScriptedOpenAISDK(
        [
            {
                "content": json.dumps(
                    {
                        "bug_intent_summary": "Avoid StringIndexOutOfBoundsException in WordUtils.abbreviate.",
                        "root_cause_hypothesis": "lower exceeds str.length()",
                        "anchor_analysis": [],
                        "boundary_notes": [],
                        "recommended_tier": "tier_a",
                        "entry_harness_hint": "WordUtils.abbreviate",
                    }
                )
            }
        ]
    )
    llm_client = OpenAILLMClient(settings, sdk_factory=lambda **kw: sdk)
    pack = IntentContextAgent(llm_client, settings).explore(task)

    assert pack.tier == "tier_a"
    assert all(sensitive_summary not in p for p in sdk.recorded_prompt_texts)
    assert any("StringIndexOutOfBoundsException" in p for p in sdk.recorded_prompt_texts)
