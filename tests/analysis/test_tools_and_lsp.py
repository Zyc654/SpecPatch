"""Unit tests for Docker on-demand tools (`grep_code`, `read_file_lines`, AST context, LSP, and dispatcher)."""

from __future__ import annotations

import io
import json
from pathlib import Path
import sys
from typing import Any

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from specpatchguard.analysis.ast_context import JavaAstContextTools  # noqa: E402
from specpatchguard.analysis.lsp_client import (  # noqa: E402
    LspNavigationTools,
    LspProtocolError,
    encode_lsp_message,
    read_lsp_message,
)
from specpatchguard.analysis.protocols import (  # noqa: E402
    DockerExecExecutor,
    LocalRepoExecutor,
    ToolExecutionError,
    resolve_repo_rel_path,
)
from specpatchguard.analysis.repo_tools import RepoSearchTools  # noqa: E402
from specpatchguard.analysis.tool_registry import ALL_TOOL_SCHEMAS, ToolDispatcher  # noqa: E402
from specpatchguard.core.config_loader import load_settings  # noqa: E402

SAMPLE_JAVA = """\
package org.example.calc;

import java.util.Objects;
import java.util.List;

/**
 * Calculator utility class.
 */
public class Calculator extends BaseCalculator implements Evaluable {
    /** Default scaling factor. */
    public static final int SCALE_FACTOR = 10;
    private int accumulator = 0;

    public Calculator(int initial) {
        this.accumulator = initial;
    }

    /**
     * Compute adjusted value; avoid "if (fake) {" inside comments/strings.
     */
    @Override
    public int compute(String input, int offset) throws IllegalArgumentException {
        Objects.requireNonNull(input, "input must not be null");
        if (input.isEmpty()) {
            return 0;
        }
        String note = "accumulator = 999; if (ignored) {}"; // accumulator += 1
        int result = accumulator;
        if (offset > 0) {
            for (int i = 0; i < offset; i++) {
                if (i % 2 == 0) {
                    result += input.length();
                }
            }
        }
        accumulator = result;
        helperLog(result);
        return result;
    }

    private void helperLog(int val) {
        System.out.println(val);
    }
}
"""

BASE_JAVA = """\
package org.example.calc;

public class BaseCalculator {
    public int compute(String input, int offset) {
        return 0;
    }
}
"""


def _build_sample_repo(tmp_path: Path) -> Path:
    src_dir = tmp_path / "src" / "main" / "java" / "org" / "example" / "calc"
    src_dir.mkdir(parents=True, exist_ok=True)
    (src_dir / "Calculator.java").write_text(SAMPLE_JAVA, encoding="utf-8")
    (src_dir / "BaseCalculator.java").write_text(BASE_JAVA, encoding="utf-8")
    # Patch artifact that must be blocked under readonly_prepatch
    (tmp_path / "candidate.patch").write_text("--- a/X.java\n+++ b/X.java\n+secret_patch_line\n", encoding="utf-8")
    git_dir = tmp_path / ".git"
    git_dir.mkdir(exist_ok=True)
    (git_dir / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    return tmp_path


def _default_tools_cfg() -> dict[str, Any]:
    return load_settings(PROJECT_ROOT / "config" / "config.json").tools


def _default_lsp_cfg() -> dict[str, Any]:
    return load_settings(PROJECT_ROOT / "config" / "config.json").lsp


def test_patch_blind_path_containment():
    assert resolve_repo_rel_path("./src\\main\\Calculator.java", readonly_prepatch=True) == "src/main/Calculator.java"
    for bad in ("../secret.txt", "/etc/passwd", "C:/Windows/win.ini", ".git/HEAD", "fix.patch", "a/b.diff", "bad\npath"):
        with pytest.raises(ToolExecutionError):
            resolve_repo_rel_path(bad, readonly_prepatch=True)


def test_grep_code_configurable_context_and_clamping(tmp_path: Path):
    repo = _build_sample_repo(tmp_path)
    tools_cfg = _default_tools_cfg()
    tools_cfg["grep_max_context_lines"] = 8
    executor = LocalRepoExecutor(repo, state="prepatch", readonly_prepatch=True)
    search = RepoSearchTools(executor, tools_cfg, readonly_prepatch=True)

    # Default context (5 before, 5 after)
    res_default = search.grep_code("SCALE_FACTOR")
    assert res_default["effective_context_before"] == 5
    assert res_default["effective_context_after"] == 5
    assert res_default["context_clamped"] is False
    assert res_default["total_matches"] == 1
    assert len(res_default["matches"][0]["context_before_lines"]) == 5
    assert len(res_default["matches"][0]["context_after_lines"]) == 5

    # Custom asymmetric context
    res_custom = search.grep_code("SCALE_FACTOR", context_before=1, context_after=2)
    assert res_custom["effective_context_before"] == 1
    assert res_custom["effective_context_after"] == 2
    assert len(res_custom["matches"][0]["context_before_lines"]) == 1
    assert len(res_custom["matches"][0]["context_after_lines"]) == 2

    # Clamped at grep_max_context_lines (8)
    res_clamped = search.grep_code("SCALE_FACTOR", context_lines=20)
    assert res_clamped["effective_context_before"] == 8
    assert res_clamped["effective_context_after"] == 8
    assert res_clamped["context_clamped"] is True

    # Patch-Blind blocks .patch/.diff and .git matches
    res_blocked = search.grep_code("secret_patch_line")
    assert res_blocked["total_matches"] == 0


def test_grep_code_docker_rg_and_gnu_grep_fallback():
    tools_cfg = _default_tools_cfg()
    calls: list[list[str]] = []

    def mock_rg_runner(argv: list[str], timeout: int) -> tuple[int, str, str]:
        calls.append(argv)
        ev1 = json.dumps({"type": "context", "data": {"path": {"text": "src/A.java"}, "line_number": 9, "lines": {"text": "int x = 0;\n"}}})
        ev2 = json.dumps({"type": "match", "data": {"path": {"text": "src/A.java"}, "line_number": 10, "lines": {"text": "return x;\n"}}})
        ev3 = json.dumps({"type": "context", "data": {"path": {"text": "src/A.java"}, "line_number": 11, "lines": {"text": "}\n"}}})
        return 0, f"{ev1}\n{ev2}\n{ev3}\n", ""

    docker_exec = DockerExecExecutor("c1", "/workspace/d4j_work/Lang_1", runner=mock_rg_runner)
    search = RepoSearchTools(docker_exec, tools_cfg, readonly_prepatch=True)
    res = search.grep_code("return x", context_before=2, context_after=3)
    assert res["total_matches"] == 1
    assert res["matches"][0]["line"] == 10
    assert "-B" in calls[0] and "2" in calls[0]
    assert "-A" in calls[0] and "3" in calls[0]
    assert "!*.patch" in calls[0]

    # Fallback to GNU grep when rg is missing (rc=127)
    def mock_grep_fallback(argv: list[str], timeout: int) -> tuple[int, str, str]:
        if "rg" in argv:
            return 127, "", "rg: not found"
        return 0, "src/A.java-9-int x = 0;\nsrc/A.java:10:return x;\nsrc/A.java-11-}\n", ""

    docker_exec2 = DockerExecExecutor("c1", "/workspace/d4j_work/Lang_1", runner=mock_grep_fallback)
    res2 = RepoSearchTools(docker_exec2, tools_cfg, readonly_prepatch=True).grep_code("return x", context_lines=1)
    assert res2["total_matches"] == 1
    assert res2["matches"][0]["context_before_lines"] == [{"line": 9, "text": "int x = 0;"}]

    # WARN-04: rg rc=2 (syntax error) must NOT fall back to grep; path_glob starting with '-' rejected
    def mock_rg_syntax_error(argv: list[str], timeout: int) -> tuple[int, str, str]:
        if "rg" in argv:
            return 2, "", "regex parse error"
        raise AssertionError("Should not fall back to grep when rg exits with rc=2")

    docker_exec3 = DockerExecExecutor("c1", "/workspace/d4j_work/Lang_1", runner=mock_rg_syntax_error)
    with pytest.raises(ToolExecutionError, match="Container rg failed"):
        RepoSearchTools(docker_exec3, tools_cfg, readonly_prepatch=True).grep_code("[unclosed")
    with pytest.raises(ToolExecutionError, match="cannot start with '-'"):
        RepoSearchTools(docker_exec3, tools_cfg, readonly_prepatch=True).grep_code("x", path_glob="-exec")


def test_read_file_lines_custom_context_and_bounds(tmp_path: Path):
    repo = _build_sample_repo(tmp_path)
    tools_cfg = _default_tools_cfg()
    tools_cfg["read_max_lines"] = 6
    executor = LocalRepoExecutor(repo)
    search = RepoSearchTools(executor, tools_cfg)

    rel = "src/main/java/org/example/calc/Calculator.java"
    res = search.read_file_lines(rel, start_line=24, end_line=25, context_before=1, context_after=1)
    assert res["actual_start"] == 23
    assert res["actual_end"] == 26
    assert res["truncated"] is False
    assert "24:         if (input.isEmpty()) {" in res["content"]

    # Exceeding read_max_lines clamps safely
    res_clamp = search.read_file_lines(rel, start_line=10, end_line=30, context_lines=0)
    assert res_clamp["truncated"] is True
    assert res_clamp["actual_end"] - res_clamp["actual_start"] + 1 == 6


def test_ast_context_branches_class_members_method_and_symbol_trace(tmp_path: Path):
    repo = _build_sample_repo(tmp_path)
    tools_cfg = _default_tools_cfg()
    executor = LocalRepoExecutor(repo)
    ast_tools = JavaAstContextTools(executor, tools_cfg)
    rel = "src/main/java/org/example/calc/Calculator.java"

    # Line 32 is `result += input.length();` inside `if (offset > 0)` -> `for` -> `if (i % 2 == 0)`
    branches = ast_tools.get_enclosing_branches(rel, line=32, context_lines=2)
    assert branches["enclosing_class"] == "Calculator"
    assert branches["enclosing_method"]["name"] == "compute"
    kinds_conds = [(b["kind"], b["condition"]) for b in branches["enclosing_branches"]]
    assert kinds_conds == [
        ("if", "offset > 0"),
        ("for", "int i = 0; i < offset; i++"),
        ("if", "i % 2 == 0"),
    ]
    assert any("Objects.requireNonNull" in g["text"] for g in branches["preceding_guards"])
    assert any("return 0;" in g["text"] for g in branches["preceding_guards"])
    assert any("accumulator = result;" in f["text"] for f in branches["following_statements_preview"])
    assert branches["effective_context_before"] == 2

    members = ast_tools.get_class_members(rel)
    cls = members["classes"][0]
    assert cls["name"] == "Calculator"
    assert cls["extends"] == "BaseCalculator"
    assert cls["implements"] == ["Evaluable"]
    field_names = [f["name"] for f in cls["fields"]]
    assert field_names == ["SCALE_FACTOR", "accumulator"]
    assert cls["fields"][0]["initializer"] == "10"
    assert [c["signature"] for c in cls["constructors"]] == ["Calculator(int initial)"]
    assert [m["name"] for m in cls["methods"]] == ["compute", "helperLog"]

    method_ctx = ast_tools.get_method_context(rel, method_name="compute", context_before=1, context_after=1)
    assert method_ctx["method"]["throws"] == "IllegalArgumentException"
    assert "@Override" in method_ctx["method"]["annotations"]
    assert "Compute adjusted value" in method_ctx["method"]["javadoc"]

    trace = ast_tools.trace_symbol_usage(rel, "accumulator", context_lines=1)
    # Lines: 12 (private int accumulator = 0; -> write), 15 (this.accumulator = initial; -> write),
    # 28 (int result = accumulator; -> read), 36 (accumulator = result; -> write)
    # String/comment fake occurrences on line 27 must NOT be included!
    assert all(occ["line"] != 27 for occ in trace["occurrences"])
    kinds = {occ["line"]: occ["usage_kind"] for occ in trace["occurrences"]}
    assert kinds[15] == "write"
    assert kinds[28] == "read"
    assert kinds[36] == "write"


def test_lsp_framing_and_navigation_tools(tmp_path: Path):
    repo = _build_sample_repo(tmp_path)
    tools_cfg = _default_tools_cfg()
    lsp_cfg = _default_lsp_cfg()
    executor = LocalRepoExecutor(repo)

    # 1) Test framing round-trip and malformed error
    payload = {"jsonrpc": "2.0", "id": 1, "result": {"ok": "中文"}}
    encoded = encode_lsp_message(payload)
    assert read_lsp_message(io.BytesIO(encoded)) == payload
    with pytest.raises(LspProtocolError):
        read_lsp_message(io.BytesIO(b"Missing-Header: 1\r\n\r\n{}"))

    # 2) Test all 4 LSP tools via mock transport
    rel_calc = "src/main/java/org/example/calc/Calculator.java"
    rel_base = "src/main/java/org/example/calc/BaseCalculator.java"
    calc_uri = f"file://{executor.work_root}/{rel_calc}"
    base_uri = f"file://{executor.work_root}/{rel_base}"

    class MockTransport:
        def __init__(self) -> None:
            self.sent: list[dict[str, Any]] = []
            self.responses: list[dict[str, Any]] = []

        def send(self, msg: dict[str, Any]) -> None:
            self.sent.append(msg)
            req_id = msg.get("id")
            method = msg.get("method")
            if req_id is None:
                return
            if method == "initialize":
                # Emit a server notification before actual response to verify notification skipping
                self.responses.append({"jsonrpc": "2.0", "method": "window/logMessage", "params": {"message": "ready"}})
                self.responses.append({"jsonrpc": "2.0", "id": req_id, "result": {"capabilities": {}}})
            elif method == "textDocument/definition":
                self.responses.append(
                    {
                        "jsonrpc": "2.0",
                        "id": req_id,
                        "result": [
                            {
                                "targetUri": calc_uri,
                                "targetSelectionRange": {"start": {"line": 40, "character": 17}, "end": {"line": 40, "character": 26}},
                            }
                        ],
                    }
                )
            elif method == "textDocument/references":
                self.responses.append(
                    {
                        "jsonrpc": "2.0",
                        "id": req_id,
                        "result": [
                            {"uri": calc_uri, "range": {"start": {"line": 36, "character": 8}, "end": {"line": 36, "character": 17}}},
                            {"uri": base_uri, "range": {"start": {"line": 3, "character": 15}, "end": {"line": 3, "character": 22}}},
                        ],
                    }
                )
            elif method == "textDocument/hover":
                self.responses.append(
                    {
                        "jsonrpc": "2.0",
                        "id": req_id,
                        "result": {"contents": {"kind": "markdown", "value": "```java\nprivate void helperLog(int val)\n```"}},
                    }
                )
            elif method == "textDocument/prepareTypeHierarchy":
                self.responses.append(
                    {
                        "jsonrpc": "2.0",
                        "id": req_id,
                        "result": [{"name": "Calculator", "uri": calc_uri, "selectionRange": {"start": {"line": 8, "character": 13}, "end": {"line": 8, "character": 23}}}],
                    }
                )
            elif method == "typeHierarchy/supertypes":
                self.responses.append(
                    {
                        "jsonrpc": "2.0",
                        "id": req_id,
                        "result": [{"name": "BaseCalculator", "detail": "org.example.calc", "uri": base_uri, "selectionRange": {"start": {"line": 2, "character": 13}, "end": {"line": 2, "character": 27}}}],
                    }
                )
            elif method == "typeHierarchy/subtypes":
                self.responses.append({"jsonrpc": "2.0", "id": req_id, "result": []})

        def receive(self, timeout: float = 15.0) -> dict[str, Any]:
            return self.responses.pop(0)

        def close(self) -> None:
            pass

    transport = MockTransport()
    lsp = LspNavigationTools(transport, executor, lsp_cfg, tools_cfg, readonly_prepatch=True)

    defn = lsp.lsp_goto_definition(rel_calc, line=37, character=9, context_lines=1)
    assert defn["definitions"][0]["start_line"] == 41
    assert "41:     private void helperLog(int val)" in defn["definitions"][0]["snippet"]
    # Verify 1-based -> 0-based conversion in sent request
    def_req = next(m for m in transport.sent if m.get("method") == "textDocument/definition")
    assert def_req["params"]["position"] == {"line": 36, "character": 8}

    refs_file = lsp.lsp_find_references(rel_calc, line=37, character=9, scope="file", context_lines=1)
    assert refs_file["total_references"] == 1
    assert refs_file["references"][0]["file_path"] == rel_calc

    hover = lsp.lsp_hover(rel_calc, line=37, character=9)
    assert "helperLog(int val)" in hover["hover_text"]

    hier = lsp.lsp_type_hierarchy(rel_calc, line=9, character=14, direction="both", context_lines=1)
    assert hier["supertypes"][0]["name"] == "BaseCalculator"
    assert hier["supertypes"][0]["file_path"] == rel_base
    assert hier["subtypes"] == []


def test_tool_dispatcher_schemas_budget_validation_and_truncation(tmp_path: Path):
    repo = _build_sample_repo(tmp_path)
    tools_cfg = _default_tools_cfg()
    tools_cfg["max_tool_calls_per_agent"] = 2
    tools_cfg["max_tool_output_chars"] = 350
    executor = LocalRepoExecutor(repo)

    dispatcher = ToolDispatcher(
        repo_tools=RepoSearchTools(executor, tools_cfg),
        ast_tools=JavaAstContextTools(executor, tools_cfg),
        tools_cfg=tools_cfg,
        lsp_tools=None,
    )
    assert len(ALL_TOOL_SCHEMAS) == 10
    assert len({s["function"]["name"] for s in ALL_TOOL_SCHEMAS}) == 10
    assert len(dispatcher.get_schemas(include_lsp=False)) == 6

    # Invalid arg type (bool for int) returns structured error without consuming budget
    bad_type = dispatcher.dispatch("read_file_lines", {"file_path": "src/main/java/org/example/calc/Calculator.java", "start_line": True})
    assert bad_type["ok"] is False
    assert dispatcher.call_count == 0

    # Valid call 1 (string JSON arguments + output truncation check)
    res1 = dispatcher.dispatch(
        "read_file_lines",
        json.dumps({"file_path": "src/main/java/org/example/calc/Calculator.java", "start_line": 1, "end_line": 40, "context_lines": 0}),
    )
    assert res1["ok"] is True
    assert res1["truncated"] is True
    assert len(json.dumps(res1, ensure_ascii=False)) <= 350

    # Valid call 2
    res2 = dispatcher.dispatch("get_class_members", {"file_path": "src/main/java/org/example/calc/Calculator.java"})
    assert res2["ok"] is True

    # Budget exhausted on call 3
    res3 = dispatcher.dispatch("grep_code", {"pattern": "Calculator"})
    assert res3["ok"] is False
    assert "budget exhausted" in res3["error"]


def test_review_regressions_ast_lsp_patch_blind_and_dispatcher(tmp_path: Path):
    # WARN-01: multiline signature + parameterized param annotation + braceless if + array initializer + multi-declarator + qualifier
    complex_java = """\
package org.example.edge;

public class EdgeCases {
    public static final int[] TABLE = {1, 2, 3};
    private int alpha = 1, beta = 2;

    /**
     * Multiline javadoc.
     */
    @Override
    public int process(
            @SuppressWarnings("rawtypes") java.util.List items,
            int limit) {
        if (items == null)
            return -1;
        this.alpha = limit;
        other.alpha = 5;
        return alpha;
    }
}
"""
    src_dir = tmp_path / "src"
    src_dir.mkdir(parents=True, exist_ok=True)
    rel_edge = "src/EdgeCases.java"
    (src_dir / "EdgeCases.java").write_text(complex_java, encoding="utf-8")

    tools_cfg = _default_tools_cfg()
    lsp_cfg = _default_lsp_cfg()
    executor = LocalRepoExecutor(tmp_path)
    ast_tools = JavaAstContextTools(executor, tools_cfg)

    members = ast_tools.get_class_members(rel_edge)
    field_names = [f["name"] for f in members["classes"][0]["fields"]]
    assert field_names == ["TABLE", "alpha", "beta"]
    assert members["classes"][0]["fields"][0]["initializer"] == "{1, 2, 3}"

    mctx = ast_tools.get_method_context(rel_edge, method_name="process", context_lines=0)
    assert mctx["method"]["start_line"] == 10  # Starts at @Override, includes full multiline signature
    assert "@Override" in mctx["method"]["annotations"]
    assert "Multiline javadoc" in mctx["method"]["javadoc"]
    assert "@SuppressWarnings" in mctx["method"]["parameters"]

    braceless = ast_tools.get_enclosing_branches(rel_edge, line=15, context_lines=0)
    assert any(b["kind"] == "if" and b["condition"] == "items == null" and b["braceless"] for b in braceless["enclosing_branches"])

    trace = ast_tools.trace_symbol_usage(rel_edge, "alpha", context_lines=0)
    qualifiers = {occ["line"]: occ["qualifier"] for occ in trace["occurrences"]}
    assert qualifiers[16] == "this"
    assert qualifiers[17] == "other"
    assert qualifiers[18] == ""

    # CRIT-04: Patch-Blind enforcement & LocalRepoExecutor cwd binding
    postpatch_exec = LocalRepoExecutor(tmp_path, state="postpatch", readonly_prepatch=True)
    with pytest.raises(ToolExecutionError, match="Patch-Blind violation"):
        RepoSearchTools(postpatch_exec, tools_cfg, readonly_prepatch=True)
    with pytest.raises(ToolExecutionError, match="Patch-Blind violation"):
        JavaAstContextTools(postpatch_exec, tools_cfg, readonly_prepatch=True)
    cwd_out = executor.exec_command([sys.executable, "-c", "import os; print(os.getcwd())"])
    assert Path(cwd_out.stdout.strip()).resolve() == tmp_path.resolve()

    # CRIT-02 & CRIT-03 & CRIT-01: LSP file_filter before truncation, server reverse-request reply, deadline timeout, close()
    edge_uri = f"file://{executor.work_root}/{rel_edge}"
    other_uri = f"file://{executor.work_root}/src/Other.java"

    class ReviewMockTransport:
        def __init__(self) -> None:
            self.sent: list[dict[str, Any]] = []
            self.responses: list[dict[str, Any]] = []
            self.closed = False

        def send(self, msg: dict[str, Any]) -> None:
            self.sent.append(msg)
            req_id = msg.get("id")
            method = msg.get("method")
            if req_id is None:
                return
            if method == "initialize":
                # Server reverse request before initialize result (CRIT-03)
                self.responses.append({"jsonrpc": "2.0", "id": 999, "method": "client/registerCapability", "params": {}})
                self.responses.append({"jsonrpc": "2.0", "id": req_id, "result": {"capabilities": {}}})
            elif method == "textDocument/references":
                # Same-file ref appears AFTER another file's ref (CRIT-02)
                self.responses.append(
                    {
                        "jsonrpc": "2.0",
                        "id": req_id,
                        "result": [
                            {"uri": other_uri, "range": {"start": {"line": 0, "character": 0}, "end": {"line": 0, "character": 5}}},
                            {"uri": edge_uri, "range": {"start": {"line": 15, "character": 8}, "end": {"line": 15, "character": 18}}},
                        ],
                    }
                )
            elif method == "shutdown":
                self.responses.append({"jsonrpc": "2.0", "id": req_id, "result": None})

        def receive(self, timeout: float = 900.0) -> dict[str, Any]:
            if not self.responses:
                raise LspProtocolError(f"Timed out after {timeout}s")
            return self.responses.pop(0)

        def close(self) -> None:
            self.closed = True

    transport = ReviewMockTransport()
    lsp = LspNavigationTools(transport, executor, lsp_cfg, tools_cfg, readonly_prepatch=True)
    refs = lsp.lsp_find_references(rel_edge, line=5, character=17, scope="file", max_results=1, context_lines=0)
    assert refs["total_references"] == 1
    assert refs["references"][0]["file_path"] == rel_edge
    assert {"jsonrpc": "2.0", "id": 999, "result": None} in transport.sent
    lsp.close()
    assert transport.closed is True
    assert any(m.get("method") == "shutdown" for m in transport.sent)
    assert any(m.get("method") == "exit" for m in transport.sent)

    # CRIT-01 timeout propagation on empty response
    with pytest.raises(LspProtocolError, match="Timed out"):
        lsp.lsp_hover(rel_edge, line=5, character=17)

    # WARN-02 & WARN-03: Optional null accepted; multibyte truncation respects max_output_chars
    (src_dir / "MultiByte.java").write_text("String s = \"" + ("中\n" * 80) + "\";\n", encoding="utf-8")
    tight_cfg = {**tools_cfg, "max_tool_output_chars": 320}
    disp = ToolDispatcher(RepoSearchTools(executor, tight_cfg), JavaAstContextTools(executor, tight_cfg), tight_cfg)
    null_opt = disp.dispatch("grep_code", {"pattern": "TABLE", "context_before": None, "path_glob": None})
    assert null_opt["ok"] is True
    null_req = disp.dispatch("grep_code", {"pattern": None})
    assert null_req["ok"] is False
    trunc_res = disp.dispatch("read_file_lines", {"file_path": "src/MultiByte.java", "start_line": 1, "end_line": 50, "context_lines": 0})
    assert trunc_res["truncated"] is True
    assert len(json.dumps(trunc_res, ensure_ascii=False)) <= 320


def test_simplified_rule_support_exclude_tests_overloads_and_visibility(tmp_path: Path):
    svc_java = """\
package org.example;

class PackageHelper {
    private int helper(int a) {
        return helper(a, 0);
    }

    int helper(int a, int b) {
        return a + b;
    }
}
"""
    caller_java = """\
package org.example;

public class Caller {
    int run(PackageHelper h) {
        return h.helper(1, 2);
    }
}
"""
    test1_java = """\
package org.example;

public class SvcTest {
    void testRun() {
        new PackageHelper().helper(1, 2);
    }
}
"""
    test2_java = """\
package org.example;

public class TestSvc {
    void testRun2() {
        new PackageHelper().helper(3, 4);
    }
}
"""
    (tmp_path / "src" / "main" / "java" / "org" / "example").mkdir(parents=True, exist_ok=True)
    (tmp_path / "src" / "test" / "java" / "org" / "example").mkdir(parents=True, exist_ok=True)
    (tmp_path / "test" / "org" / "example").mkdir(parents=True, exist_ok=True)

    rel_svc = "src/main/java/org/example/Svc.java"
    rel_caller = "src/main/java/org/example/Caller.java"
    rel_test1 = "src/test/java/org/example/SvcTest.java"
    rel_test2 = "test/org/example/TestSvc.java"

    (tmp_path / rel_svc).write_text(svc_java, encoding="utf-8")
    (tmp_path / rel_caller).write_text(caller_java, encoding="utf-8")
    (tmp_path / rel_test1).write_text(test1_java, encoding="utf-8")
    (tmp_path / rel_test2).write_text(test2_java, encoding="utf-8")

    tools_cfg = _default_tools_cfg()
    lsp_cfg = _default_lsp_cfg()
    executor = LocalRepoExecutor(tmp_path)
    ast_tools = JavaAstContextTools(executor, tools_cfg)
    repo_tools = RepoSearchTools(executor, tools_cfg)

    # 1) Class visibility & overloads for simplified rule.md (边界 1 & 边界 3)
    branches = ast_tools.get_enclosing_branches(rel_svc, line=5, context_lines=0)
    assert branches["enclosing_class"] == "PackageHelper"
    assert branches["enclosing_class_visibility"] == "package-private"
    assert branches["enclosing_method"]["visibility"] == "private"

    branches_pub = ast_tools.get_enclosing_branches(rel_caller, line=5, context_lines=0)
    assert branches_pub["enclosing_class_visibility"] == "public"

    mctx_by_line = ast_tools.get_method_context(rel_svc, line=5, context_lines=0)
    assert mctx_by_line["overload_count"] == 2
    assert [o["signature"] for o in mctx_by_line["overloads"]] == ["helper(int a)", "helper(int a, int b)"]

    mctx_by_name = ast_tools.get_method_context(rel_svc, method_name="helper", context_lines=0)
    assert mctx_by_name["overload_count"] == 2

    # 2) exclude_tests filters test paths BEFORE max_results truncation (边界 3)
    test1_uri = f"file://{executor.work_root}/{rel_test1}"
    test2_uri = f"file://{executor.work_root}/{rel_test2}"
    caller_uri = f"file://{executor.work_root}/{rel_caller}"

    class FilterMockTransport:
        def __init__(self) -> None:
            self.responses: list[dict[str, Any]] = []

        def send(self, msg: dict[str, Any]) -> None:
            req_id = msg.get("id")
            method = msg.get("method")
            if req_id is None:
                return
            if method == "initialize":
                self.responses.append({"jsonrpc": "2.0", "id": req_id, "result": {"capabilities": {}}})
            elif method == "textDocument/references":
                self.responses.append(
                    {
                        "jsonrpc": "2.0",
                        "id": req_id,
                        "result": [
                            {"uri": test1_uri, "range": {"start": {"line": 4, "character": 28}, "end": {"line": 4, "character": 34}}},
                            {"uri": test2_uri, "range": {"start": {"line": 4, "character": 28}, "end": {"line": 4, "character": 34}}},
                            {"uri": caller_uri, "range": {"start": {"line": 4, "character": 17}, "end": {"line": 4, "character": 23}}},
                        ],
                    }
                )

        def receive(self, timeout: float = 900.0) -> dict[str, Any]:
            return self.responses.pop(0)

        def close(self) -> None:
            pass

    lsp = LspNavigationTools(FilterMockTransport(), executor, lsp_cfg, tools_cfg, readonly_prepatch=True)
    all_refs = lsp.lsp_find_references(rel_svc, line=8, character=9, exclude_tests=False, context_lines=0)
    assert all_refs["exclude_tests"] is False
    assert [r["is_test_file"] for r in all_refs["references"]] == [True, True, False]

    prod_refs = lsp.lsp_find_references(rel_svc, line=8, character=9, exclude_tests=True, max_results=1, context_lines=0)
    assert prod_refs["exclude_tests"] is True
    assert prod_refs["total_references"] == 1
    assert prod_refs["truncated"] is False
    assert prod_refs["references"][0]["file_path"] == rel_caller
    assert prod_refs["references"][0]["is_test_file"] is False

    with pytest.raises(ToolExecutionError, match="exclude_tests must be a boolean"):
        lsp.lsp_find_references(rel_svc, line=8, character=9, exclude_tests="yes")  # type: ignore[arg-type]

    # 3) ToolDispatcher validates exclude_tests & optional None
    disp = ToolDispatcher(repo_tools=repo_tools, ast_tools=ast_tools, tools_cfg=tools_cfg, lsp_tools=lsp)
    ok_true = disp.dispatch("lsp_find_references", {"file_path": rel_svc, "line": 8, "character": 9, "exclude_tests": True})
    assert ok_true["ok"] is True
    assert [r["file_path"] for r in ok_true["references"]] == [rel_caller]

    ok_none = disp.dispatch("lsp_find_references", {"file_path": rel_svc, "line": 8, "character": 9, "exclude_tests": None})
    assert ok_none["ok"] is True
    assert ok_none["exclude_tests"] is False

    bad_bool = disp.dispatch("lsp_find_references", {"file_path": rel_svc, "line": 8, "character": 9, "exclude_tests": "yes"})
    assert bad_bool["ok"] is False


def test_gsd_review_rule_alignment_regressions(tmp_path: Path):
    # WARN-01 & WARN-04: src/main/.../junit/... must NOT be filtered by exclude_tests=True;
    # excluded_test_count reports exact count of skipped test references.
    prod_junit_java = """\
package org.example.junit;

public class ProdRunner {
    public void run() {}
}
"""
    nested_dup_java = """\
package org.example;

public class Outer {
    private static class Builder {
        public void build() {}
    }

    static class Other {
        private int fieldOnly = 0;
        public static class Builder {
            public void build() {}
            public void build(int x) {}
        }
    }

    public void work() {
        class Local {
            int marker = 1;
        }
        int afterLocal = 2;
    }
}
"""
    (tmp_path / "src" / "main" / "java" / "org" / "example" / "junit").mkdir(parents=True, exist_ok=True)
    (tmp_path / "src" / "test" / "java" / "org" / "example").mkdir(parents=True, exist_ok=True)

    rel_prod_junit = "src/main/java/org/example/junit/ProdRunner.java"
    rel_test = "src/test/java/org/example/ProdRunnerTest.java"
    rel_nested = "src/main/java/org/example/Outer.java"

    (tmp_path / rel_prod_junit).write_text(prod_junit_java, encoding="utf-8")
    (tmp_path / rel_test).write_text("package org.example;\npublic class ProdRunnerTest {}\n", encoding="utf-8")
    (tmp_path / rel_nested).write_text(nested_dup_java, encoding="utf-8")

    tools_cfg = _default_tools_cfg()
    lsp_cfg = _default_lsp_cfg()
    executor = LocalRepoExecutor(tmp_path)
    ast_tools = JavaAstContextTools(executor, tools_cfg)
    repo_tools = RepoSearchTools(executor, tools_cfg)

    # WARN-02: local class inside work() does not hijack Outer's visibility/class when enclosing_method is work();
    # and field line outside methods falls back to innermost class Other.
    local_field_branch = ast_tools.get_enclosing_branches(rel_nested, line=18, context_lines=0)
    assert local_field_branch["enclosing_method"]["name"] == "work"
    assert local_field_branch["enclosing_class"] == "Outer"
    assert local_field_branch["enclosing_class_visibility"] == "public"

    class_field_branch = ast_tools.get_enclosing_branches(rel_nested, line=9, context_lines=0)
    assert class_field_branch["enclosing_method"] is None
    assert class_field_branch["enclosing_class"] == "Other"
    assert class_field_branch["enclosing_class_visibility"] == "package-private"

    # WARN-03: duplicate nested Builder classes do not mix overloads when querying by line
    builder1_ctx = ast_tools.get_method_context(rel_nested, line=5, context_lines=0)
    assert builder1_ctx["overload_count"] == 1
    assert [o["signature"] for o in builder1_ctx["overloads"]] == ["build()"]

    builder2_ctx = ast_tools.get_method_context(rel_nested, line=12, context_lines=0)
    assert builder2_ctx["overload_count"] == 2
    assert [o["signature"] for o in builder2_ctx["overloads"]] == ["build()", "build(int x)"]

    # WARN-01 & WARN-04: ProdRunner under src/main/.../junit/ retained + excluded_test_count == 1
    prod_uri = f"file://{executor.work_root}/{rel_prod_junit}"
    test_uri = f"file://{executor.work_root}/{rel_test}"

    class WarnMockTransport:
        def __init__(self) -> None:
            self.responses: list[dict[str, Any]] = []

        def send(self, msg: dict[str, Any]) -> None:
            req_id = msg.get("id")
            method = msg.get("method")
            if req_id is None:
                return
            if method == "initialize":
                self.responses.append({"jsonrpc": "2.0", "id": req_id, "result": {"capabilities": {}}})
            elif method == "textDocument/references":
                self.responses.append(
                    {
                        "jsonrpc": "2.0",
                        "id": req_id,
                        "result": [
                            {"uri": test_uri, "range": {"start": {"line": 1, "character": 0}, "end": {"line": 1, "character": 5}}},
                            {"uri": prod_uri, "range": {"start": {"line": 3, "character": 4}, "end": {"line": 3, "character": 10}}},
                        ],
                    }
                )

        def receive(self, timeout: float = 900.0) -> dict[str, Any]:
            return self.responses.pop(0)

        def close(self) -> None:
            pass

    lsp = LspNavigationTools(WarnMockTransport(), executor, lsp_cfg, tools_cfg, readonly_prepatch=True)
    res_excl = lsp.lsp_find_references(rel_prod_junit, line=4, character=17, exclude_tests=True, context_lines=0)
    assert res_excl["excluded_test_count"] == 1
    assert res_excl["total_references"] == 1
    assert res_excl["references"][0]["file_path"] == rel_prod_junit
    assert res_excl["references"][0]["is_test_file"] is False

    # INFO-01: ToolDispatcher truncates overloads cleanly while respecting max_tool_output_chars
    many_overloads = ["package org.example;", "public class Many {"]
    for i in range(15):
        args = ", ".join(f"int a{j}" for j in range(i))
        many_overloads.append(f"    public int fn({args}) {{ return {i}; }}")
    many_overloads.append("}")
    rel_many = "src/main/java/org/example/Many.java"
    (tmp_path / rel_many).write_text("\n".join(many_overloads) + "\n", encoding="utf-8")

    tight_cfg = {**tools_cfg, "max_tool_output_chars": 1200}
    tight_disp = ToolDispatcher(repo_tools=repo_tools, ast_tools=ast_tools, tools_cfg=tight_cfg)
    trunc_overloads = tight_disp.dispatch("get_method_context", {"file_path": rel_many, "line": 3, "context_lines": 0})
    assert trunc_overloads["ok"] is True
    assert trunc_overloads["truncated"] is True
    assert trunc_overloads["overload_count"] == len(trunc_overloads["overloads"]) < 15
    assert len(json.dumps(trunc_overloads, ensure_ascii=False)) <= 1200
