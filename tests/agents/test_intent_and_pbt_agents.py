"""Unit tests for `IntentContextAgent`, `PbtSpecAgent`, and end-to-end Patch-Blind isolation."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from specpatchguard.agents import IntentContextAgent, PbtSpecAgent
from specpatchguard.agents.llm_client import LLMResponse, ToolCallRequest, ToolLoopResult
from specpatchguard.analysis.ast_context import JavaAstContextTools
from specpatchguard.analysis.protocols import LocalRepoExecutor, ToolExecutionError
from specpatchguard.analysis.repo_tools import RepoSearchTools
from specpatchguard.analysis.tool_registry import ToolDispatcher
from specpatchguard.core.config_loader import load_settings
from specpatchguard.data.patch_blind import strip_to_patch_blind
from specpatchguard.data.schemas import (
    BugReport,
    DiffHunk,
    FailingTestRecord,
    RawPatchRecord,
    SubPatchSlice,
)
from specpatchguard.prompts.pbt_prompts import CROSS_REP_END_MARKER, CROSS_REP_START_MARKER
from specpatchguard.sandbox.d4j_runner import D4jProjectLayout
from specpatchguard.sandbox.docker_backend import SandboxError
from specpatchguard.strategies import (
    PublicEntryAnchorProbeHarness,
    StandaloneFunctionUnitHarness,
    StrategyRegistry,
    TierAPythonHypothesisStrategy,
    TierBAbstractDecisionTableStrategy,
)


def _setup_settings_and_repo(tmp_path: Path) -> tuple[Any, Path, RawPatchRecord]:
    cfg_dir = tmp_path / "config"
    cfg_dir.mkdir(parents=True)
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    for name in (
        "d4j_custom_function_level_patch.pkl",
        "labeled_d4j20_enhance_info.pkl",
        "clean_d4j20_bug_report.json",
        "manifest.json",
        "rule.md",
    ):
        (data_dir / name).write_text("{}", encoding="utf-8")

    llm_src = Path("E:/workspace/myown/SpecPatchGuard/config/llm_config.json").read_text(encoding="utf-8")
    (cfg_dir / "llm_config.json").write_text(llm_src, encoding="utf-8")

    cfg = json.loads(Path("E:/workspace/myown/SpecPatchGuard/config/config.json").read_text(encoding="utf-8"))
    cfg["data"] = {
        "patch_info_pkl": str(data_dir / "d4j_custom_function_level_patch.pkl"),
        "test_info_pkl": str(data_dir / "labeled_d4j20_enhance_info.pkl"),
        "bug_report_json": str(data_dir / "clean_d4j20_bug_report.json"),
        "manifest_json": str(data_dir / "manifest.json"),
        "rule_md_path": str(data_dir / "rule.md"),
    }
    cfg["api"]["llm_config_path"] = "./llm_config.json"
    cfg["output"] = {
        "result_dir": str(tmp_path / "out"),
        "spec_cache_dir": str(tmp_path / "out" / "frozen_specs"),
        "resume": True,
    }
    cfg_path = cfg_dir / "config.json"
    cfg_path.write_text(json.dumps(cfg), encoding="utf-8")

    env_path = tmp_path / ".env"
    env_path.write_text(
        "OPENAI_API_KEY=sk-test\nOPENAI_API_BASE=https://api.example.com/v1\nLLM_MODEL=deepseek-chat\n",
        encoding="utf-8",
    )
    settings = load_settings(cfg_path, env_path=env_path)

    repo_root = tmp_path / "repo"
    java_dir = repo_root / "src" / "java" / "org" / "apache" / "commons" / "lang"
    java_dir.mkdir(parents=True)
    (java_dir / "BooleanUtils.java").write_text(
        "package org.apache.commons.lang;\n"
        "public class BooleanUtils {\n"
        "    private static final int MAGIC_FLAG = 1;\n"
        "    public static boolean toBoolean(String str) {\n"
        "        if (str == \"true\") {\n"
        "            return true;\n"
        "        }\n"
        "        if (str == null) {\n"
        "            return false;\n"
        "        }\n"
        "        switch (str.length()) {\n"
        "            case 3: {\n"
        "                char ch0 = str.charAt(0);\n"
        "                if (ch0 == 'y') {\n"
        "                    return true;\n"
        "                }\n"
        "            }\n"
        "            case 4: {\n"
        "                char ch0 = str.charAt(0);\n"
        "                char ch3 = str.charAt(3);\n"
        "                return ch0 == 't' && ch3 == 'e';\n"
        "            }\n"
        "        }\n"
        "        return false;\n"
        "    }\n"
        "}\n",
        encoding="utf-8",
    )

    raw_patch = RawPatchRecord(
        patch_id="patch_lang51_cand",
        project="Lang",
        bug_num=51,
        bug_id="Lang-51",
        tool="candidate_tool",
        label=1,
        raw_diff="--- a/src/java/org/apache/commons/lang/BooleanUtils.java\n+++ b/src/java/org/apache/commons/lang/BooleanUtils.java\n",
        hunks=[
            DiffHunk(
                file_path="src/java/org/apache/commons/lang/BooleanUtils.java",
                old_start=15,
                old_count=2,
                new_start=15,
                new_count=3,
                header="@@ -15,2 +15,3 @@",
                lines=["                 if (ch0 == 'y') {", "+                    return SECRET_CANDIDATE_ADDITION_XYZ;", "                 }"],
                added_lines=["                    return SECRET_CANDIDATE_ADDITION_XYZ;"],
                deleted_lines=[],
                anchor_start_line=15,
                anchor_end_line=16,
                kind="add_only",
            )
        ],
        sub_patches=[
            SubPatchSlice(
                file_path="src/java/org/apache/commons/lang/BooleanUtils.java",
                start_line=4,
                content=(
                    "    public static boolean toBoolean(String str) {\n"
                    "        if (str == \"true\") {\n"
                    "            return true;\n"
                    "        }\n"
                    "        if (str == null) {\n"
                    "            return false;\n"
                    "        }\n"
                    "        switch (str.length()) {\n"
                    "            case 3: {\n"
                    "                char ch0 = str.charAt(0);\n"
                    "                if (ch0 == 'y') {\n"
                    "                    return true;\n"
                    "+                    return SECRET_CANDIDATE_ADDITION_XYZ;\n"
                    "                }\n"
                    "            }\n"
                    "            case 4: {\n"
                    "                char ch0 = str.charAt(0);\n"
                    "                char ch3 = str.charAt(3);\n"
                    "                return ch0 == 't' && ch3 == 'e';\n"
                    "            }\n"
                    "        }\n"
                    "        return false;\n"
                    "    }"
                ),
                is_fallback_diff=False,
            )
        ],
        failing_tests=[
            FailingTestRecord(
                test_file="src/test/org/apache/commons/lang/BooleanUtilsTest.java",
                test_method_name="test_toBoolean_String",
                start_line=1,
                source_code='assertFalse(BooleanUtils.toBoolean("tru"));',
                numbered_source='1: assertFalse(BooleanUtils.toBoolean("tru"));',
                failed_line_start=1,
                failed_line_end=1,
                failure_message="java.lang.StringIndexOutOfBoundsException: String index out of range: 3",
            )
        ],
        bug_report=BugReport(
            bug_id="Lang-51",
            title="BooleanUtils.toBoolean throws StringIndexOutOfBoundsException on tru",
            description="Calling toBoolean(\"tru\") falls through case 3 into case 4.",
            available=True,
        ),
    )
    return settings, repo_root, raw_patch


class _RecordingLLMClient:
    def __init__(self, chat_replies: list[str], tool_sequence: list[ToolCallRequest] | None = None) -> None:
        self.chat_replies = list(chat_replies)
        self.tool_sequence = list(tool_sequence or [])
        self.all_prompts: list[str] = []

    def _record_msgs(self, messages: list[dict[str, Any]]) -> None:
        for msg in messages:
            self.all_prompts.append(str(msg.get("content", "")))

    def chat(self, messages: list[dict[str, Any]], **kwargs: Any) -> LLMResponse:
        self._record_msgs(messages)
        return LLMResponse(content=self.chat_replies.pop(0))

    def run_tool_loop(
        self,
        messages: list[dict[str, Any]],
        dispatcher: ToolDispatcher,
        **kwargs: Any,
    ) -> ToolLoopResult:
        conv = [dict(m) for m in messages]
        self._record_msgs(conv)
        tool_records: list[dict[str, Any]] = []
        for tc in self.tool_sequence:
            res = dispatcher.dispatch(tc.name, tc.arguments)
            tool_records.append({"tool": tc.name, "arguments": tc.arguments, "result": res})
            conv.append({"role": "tool", "tool_call_id": tc.id, "content": json.dumps(res)})
        final_text = self.chat_replies.pop(0)
        return ToolLoopResult(
            final_response=LLMResponse(content=final_text),
            messages=conv,
            tool_records=tool_records,
            rounds_used=2 if tool_records else 1,
            total_prompt_tokens=100,
            total_completion_tokens=40,
            budget_exhausted=False,
        )


def test_intent_agent_tool_harvest_and_json_repair(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    for var in ("OPENAI_API_KEY", "OPENAI_API_BASE", "LLM_MODEL"):
        monkeypatch.delenv(var, raising=False)
    settings, repo_root, raw_patch = _setup_settings_and_repo(tmp_path)
    task = strip_to_patch_blind(raw_patch)

    executor = LocalRepoExecutor(repo_root, state="prepatch", readonly_prepatch=True)
    dispatcher = ToolDispatcher(
        RepoSearchTools(executor, settings.tools, readonly_prepatch=True),
        JavaAstContextTools(executor, settings.tools, readonly_prepatch=True),
        settings.tools,
        lsp_tools=None,
    )

    llm = _RecordingLLMClient(
        chat_replies=[
            "not valid json first time",
            json.dumps(
                {
                    "bug_intent_summary": "3-char strings like 'tru' must not fall through case 3.",
                    "root_cause_hypothesis": "Missing return false at end of case 3.",
                    "anchor_analysis": ["switch(str.length()) case 3 lacks default return"],
                    "boundary_notes": ["public static pure method", "null returns false"],
                    "recommended_tier": "tier_a",
                    "entry_harness_hint": "BooleanUtils.toBoolean(String)",
                }
            ),
        ],
        tool_sequence=[
            ToolCallRequest(
                id="t1",
                name="get_enclosing_branches",
                arguments=json.dumps(
                    {
                        "file_path": "src/java/org/apache/commons/lang/BooleanUtils.java",
                        "line": 15,
                    }
                ),
            ),
            ToolCallRequest(
                id="t2",
                name="get_class_members",
                arguments=json.dumps(
                    {
                        "file_path": "src/java/org/apache/commons/lang/BooleanUtils.java",
                    }
                ),
            ),
        ],
    )

    agent = IntentContextAgent(llm, settings)
    pack = agent.explore(task, dispatcher=dispatcher, include_lsp=False)

    assert pack.tier == "tier_a"
    assert "fall through case 3" in pack.bug_intent_summary
    assert len(pack.boundary_context.enclosing_branches) == 1
    assert any(f.get("name") == "MAGIC_FLAG" for f in pack.boundary_context.class_fields)
    assert "public static pure method" in pack.boundary_context.notes
    assert pack.extra_context["intent_parse_degraded"] is False
    assert all("SECRET_CANDIDATE_ADDITION_XYZ" not in p for p in llm.all_prompts)

    # Reject postpatch dispatcher immediately.
    bad_exec = LocalRepoExecutor(repo_root, state="postpatch", readonly_prepatch=False)
    bad_disp = ToolDispatcher(
        RepoSearchTools(bad_exec, settings.tools, readonly_prepatch=False),
        JavaAstContextTools(bad_exec, settings.tools, readonly_prepatch=False),
        settings.tools,
    )
    with pytest.raises(ToolExecutionError):
        agent.explore(task, dispatcher=bad_disp)


def test_pbt_agent_syntax_fix_refinement_and_patch_blind_isolation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    for var in ("OPENAI_API_KEY", "OPENAI_API_BASE", "LLM_MODEL"):
        monkeypatch.delenv(var, raising=False)
    settings, repo_root, raw_patch = _setup_settings_and_repo(tmp_path)
    task = strip_to_patch_blind(raw_patch)

    # First run IntentContextAgent cleanly
    intent_llm = _RecordingLLMClient(
        chat_replies=[
            json.dumps(
                {
                    "bug_intent_summary": "BooleanUtils.toBoolean must return false for 'tru' instead of throwing StringIndexOutOfBoundsException.",
                    "root_cause_hypothesis": "Fallthrough from case 3 into case 4.",
                    "anchor_analysis": ["case 3 falls through"],
                    "boundary_notes": ["String lengths 0..5 and null"],
                    "recommended_tier": "tier_a",
                    "entry_harness_hint": "BooleanUtils.toBoolean(String)",
                }
            )
        ]
    )
    intent_pack = IntentContextAgent(intent_llm, settings).explore(task)

    cross_rep_script = f"""```python
import json
def ref_impl(s):
    return s in ("true", "yes", "on")
if __name__ == "__main__":
    print("{CROSS_REP_START_MARKER}")
    print(json.dumps([
        {{"case_id": "cross_null", "args": [None], "category": "boundary", "rationale": "null check", "ref_hint": False}},
        {{"case_id": "cross_t00", "args": ["t00"], "category": "bug_trigger", "rationale": "3-char t*", "ref_hint": False}}
    ]))
    print("{CROSS_REP_END_MARKER}")
```"""

    initial_spec_json = json.dumps(
        {
            "preconditions": ["str may be any String or null"],
            "postconditions": ["toBoolean('tru') == false", "never throws StringIndexOutOfBoundsException"],
            "invariants": [],
            "exception_contracts": ["no StringIndexOutOfBoundsException"],
            "anchor_infection_conditions": ["str != null && str.length() == 3"],
            "receiver_setup_java": "",
            "seed_inputs": [
                {
                    "case_id": "seed_tru",
                    "args": ["tru"],
                    "category": "bug_trigger",
                    "expected_behavior": "returns false",
                    "expected_return": False,
                },
                {
                    "case_id": "seed_weird_err",
                    "args": ["bad_noise"],
                    "category": "normal",
                    "expected_behavior": "valid",
                },
            ],
            "anchor_probes": [
                {
                    "probe_id": "case3_entry",
                    "file_path": "src/java/org/apache/commons/lang/BooleanUtils.java",
                    "line_number": 15,
                    "position": "before",
                    "condition_expr": "str != null && str.length() == 3",
                    "observed_expressions": ["str"],
                }
            ],
        }
    )

    fixed_java_code = (
        "```java\n"
        "package org.apache.commons.lang;\n"
        "import org.junit.Test;\n"
        "public class SpecPatchGuardGeneratedTest {\n"
        "    @Test public void testGeneratedBoundaryWitnesses() {}\n"
        "}\n"
        "```"
    )

    refinement_round1_json = json.dumps(
        {
            "preconditions": ["str is non-empty or null; exclude synthetic bad_noise"],
            "postconditions": ["toBoolean('tru') == false", "toBoolean('t00') == false"],
            "invariants": [],
            "exception_contracts": ["never throws StringIndexOutOfBoundsException"],
            "anchor_infection_conditions": ["str != null && str.length() == 3"],
            "excluded_case_ids": ["seed_weird_err"],
            "seed_inputs": [],
            "converged": True,
            "refinement_notes": "Confirmed 3-char bug triggers and valid null passing case.",
        }
    )

    pbt_llm = _RecordingLLMClient(
        chat_replies=[
            initial_spec_json,
            cross_rep_script,
            fixed_java_code,
            refinement_round1_json,
            initial_spec_json,
        ]
    )

    class _FakeInjector:
        def __init__(self) -> None:
            self.backend = type("B", (), {"state": "prepatch"})()
            self.calls = 0

        def run_injected_test(self, spec: Any, layout: Any, **kwargs: Any) -> Any:
            self.calls += 1
            harness = StandaloneFunctionUnitHarness()
            if self.calls == 1:
                return harness.parse_execution_result(
                    {"compiled": False, "error": "cannot find symbol: bad helper"}
                )
            return harness.parse_execution_result(
                {
                    "compiled": True,
                    "stdout": (
                        '[SPECPATCHGUARD_CASE] {"case_id":"seed_tru","status":"error","exception_type":"java.lang.StringIndexOutOfBoundsException","message":"String index out of range: 3"}\n'
                        '[SPECPATCHGUARD_CASE] {"case_id":"cross_t00","status":"error","exception_type":"java.lang.StringIndexOutOfBoundsException","message":"String index out of range: 3"}\n'
                        '[SPECPATCHGUARD_CASE] {"case_id":"cross_null","status":"passed","observed":"false"}\n'
                        '[SPECPATCHGUARD_CASE] {"case_id":"seed_weird_err","status":"error","exception_type":"java.lang.IllegalArgumentException","message":"noise"}\n'
                        '[SPECPATCHGUARD_PROBE] {"probe_id":"case3_entry","file":"src/java/org/apache/commons/lang/BooleanUtils.java","line":15,"cond":true,"values":{"str":"tru"}}\n'
                    ),
                    "stderr": "",
                }
            )

    registry = StrategyRegistry()
    registry.register_cross_rep(TierAPythonHypothesisStrategy(pbt_llm, hypothesis_available=False))
    registry.register_cross_rep(TierBAbstractDecisionTableStrategy())
    registry.register_harness(StandaloneFunctionUnitHarness())
    registry.register_harness(PublicEntryAnchorProbeHarness())

    fake_injector = _FakeInjector()
    pbt_agent = PbtSpecAgent(
        pbt_llm,
        settings,
        registry=registry,
        junit_injector=fake_injector,  # type: ignore[arg-type]
    )
    layout = D4jProjectLayout(
        project="Lang",
        bug_num=51,
        bug_id="Lang-51",
        version="51b",
        work_dir="/workspace/d4j_work/Lang_51b",
        dir_src_classes="src/java",
        dir_src_tests="src/test",
        dir_bin_classes="target/classes",
        dir_bin_tests="target/tests",
    )

    res = pbt_agent.generate_and_refine(intent_pack, layout=layout, persist=True)

    assert res.status == "ok"
    assert fake_injector.calls == 2
    assert len(res.witness_bank.confirmed_bug_witnesses) == 2
    assert {w.case_id for w in res.witness_bank.confirmed_bug_witnesses} == {"seed_tru", "cross_t00"}
    assert len(res.witness_bank.boundary_passing_witnesses) == 1
    assert res.witness_bank.boundary_passing_witnesses[0].case_id == "cross_null"
    assert len(res.witness_bank.excluded_irrelevant_cases) == 1
    assert res.witness_bank.excluded_irrelevant_cases[0].case_id == "seed_weird_err"
    assert len(res.witness_bank.infection_probe_traces) == 1
    assert res.refinement_history[0]["converged"] is True

    # Persisted atomic JSON exists and round-trips cleanly.
    saved = settings.spec_cache_dir / "Lang-51__patch_lang51_cand__step5_spec.json"
    assert saved.is_file()

    # Patch-Blind isolation assertion across ALL LLM prompts in Intent + Cross-Rep + PBT.
    for prompt in intent_llm.all_prompts + pbt_llm.all_prompts:
        assert "SECRET_CANDIDATE_ADDITION_XYZ" not in prompt

    # Reject postpatch JUnitInjector backend.
    fake_injector.backend.state = "postpatch"
    with pytest.raises(SandboxError):
        pbt_agent.generate_and_refine(intent_pack, layout=layout)

    # Graceful Tier-B placeholder path without crash.
    fake_injector.backend.state = "prepatch"
    tier_b_pack = intent_pack
    tier_b_pack.tier = "tier_b"
    tier_b_res = pbt_agent.generate_and_refine(tier_b_pack, layout=layout)
    assert tier_b_res.status == "tier_b_placeholder"


def test_review_regressions_crit01_crit04_warn02_warn04_info02(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from specpatchguard.agents.pbt_agent import _expected_failure_signatures

    for var in ("OPENAI_API_KEY", "OPENAI_API_BASE", "LLM_MODEL"):
        monkeypatch.delenv(var, raising=False)
    settings, repo_root, raw_patch = _setup_settings_and_repo(tmp_path)
    task = strip_to_patch_blind(raw_patch)

    # INFO-02: multi-line d4j_fallback root cause extracts AssertionFailedError & StringIndexOutOfBoundsException
    intent_pack = IntentContextAgent(
        _RecordingLLMClient(
            [
                json.dumps(
                    {
                        "bug_intent_summary": "Handle tru",
                        "root_cause_hypothesis": "Fallthrough",
                        "anchor_analysis": [],
                        "boundary_notes": [],
                        "recommended_tier": "tier_a",
                        "entry_harness_hint": "BooleanUtils.toBoolean(String)",
                    }
                )
            ]
        ),
        settings,
    ).explore(task)
    intent_pack.failing_tests = [
        FailingTestRecord(
            test_file="src/test/org/apache/commons/lang/BooleanUtilsTest.java",
            test_method_name="test_toBoolean_String",
            start_line=1,
            source_code="BooleanUtils.toBoolean(\"tru\");",
            numbered_source="1: BooleanUtils.toBoolean(\"tru\");",
            failed_line_start=None,
            failed_line_end=None,
            failure_message=(
                " - org.apache.commons.lang.BooleanUtilsTest::test_toBoolean_String:\n"
                "   java.lang.StringIndexOutOfBoundsException: String index out of range: 3"
            ),
        )
    ]
    has_assert, exc_names = _expected_failure_signatures(intent_pack)
    assert has_assert is False
    assert "java.lang.StringIndexOutOfBoundsException" in exc_names

    # WARN-04: mixed dispatcher where ast_tools is postpatch must be rejected
    pre_exec = LocalRepoExecutor(repo_root, state="prepatch", readonly_prepatch=True)
    post_exec = LocalRepoExecutor(repo_root, state="postpatch", readonly_prepatch=False)
    mixed_disp = ToolDispatcher(
        RepoSearchTools(pre_exec, settings.tools, readonly_prepatch=True),
        JavaAstContextTools(post_exec, settings.tools, readonly_prepatch=False),
        settings.tools,
    )
    with pytest.raises(ToolExecutionError, match="JavaAstContextTools"):
        IntentContextAgent(_RecordingLLMClient([]), settings).explore(task, dispatcher=mixed_disp)

    # CRIT-01 + CRIT-04 + WARN-02:
    # - PbtSpecAgent invokes dispatcher tool loop during initial spec extraction (CRIT-04)
    # - Syntax-fix does not re-compile when LLM omits code block once, then succeeds (WARN-02)
    # - Round 1 returns excluded_case_ids=["seed_noise"] + new_seeds + converged=True -> deferred so new seed executes in Round 2 (CRIT-01)
    # - Round 2 excludes "cross_pass" alongside converged=True -> "cross_pass" is immediately moved to excluded_irrelevant_cases and Round 1's "seed_noise" is preserved (CRIT-01)
    clean_disp = ToolDispatcher(
        RepoSearchTools(pre_exec, settings.tools, readonly_prepatch=True),
        JavaAstContextTools(pre_exec, settings.tools, readonly_prepatch=True),
        settings.tools,
    )
    initial_spec = json.dumps(
        {
            "preconditions": ["str != null"],
            "postconditions": ["toBoolean('tru') == false"],
            "invariants": ["pure"],
            "exception_contracts": [],
            "anchor_infection_conditions": [],
            "seed_inputs": [
                {"case_id": "seed_tru", "args": ["tru"], "category": "bug_trigger"},
                {"case_id": "seed_noise", "args": ["noise"], "category": "normal"},
            ],
        }
    )
    cross_rep_script = f"""```python
import json
if __name__ == "__main__":
    print("{CROSS_REP_START_MARKER}")
    print(json.dumps([{{"case_id": "cross_pass", "args": ["true"], "category": "boundary"}}]))
    print("{CROSS_REP_END_MARKER}")
```"""
    fixed_java = (
        "```java\npackage org.apache.commons.lang;\nimport org.junit.Test;\n"
        "public class SpecPatchGuardGeneratedTest { @Test public void testGeneratedBoundaryWitnesses() {} }\n```"
    )
    round1_refine = json.dumps(
        {
            "preconditions": ["str length in 1..4"],
            "invariants": ["still pure"],
            "excluded_case_ids": ["seed_noise"],
            "seed_inputs": [{"case_id": "seed_t00", "args": ["t00"], "category": "bug_trigger"}],
            "converged": True,
            "refinement_notes": "Added seed_t00 and excluded seed_noise",
        }
    )
    round2_refine = json.dumps(
        {
            "preconditions": ["str length in 1..4"],
            "excluded_case_ids": ["cross_pass"],
            "seed_inputs": [],
            "converged": True,
            "refinement_notes": "Final convergence",
        }
    )
    llm = _RecordingLLMClient(
        chat_replies=[
            initial_spec,      # _extract_initial_spec via run_tool_loop
            cross_rep_script,  # TierAPythonHypothesisStrategy
            "oops no code",    # syntax-fix invalid output -> retried without recompiling
            fixed_java,        # syntax-fix valid output -> compiled on 2nd injector call
            round1_refine,     # deferred because seed_t00 added
            round2_refine,     # converges and moves cross_pass into excluded
        ],
        tool_sequence=[
            ToolCallRequest(
                id="pbt_t1",
                name="get_method_context",
                arguments=json.dumps(
                    {
                        "file_path": "src/java/org/apache/commons/lang/BooleanUtils.java",
                        "method_name": "toBoolean",
                    }
                ),
            )
        ],
    )

    class _MultiRoundInjector:
        def __init__(self) -> None:
            self.backend = type("B", (), {"state": "prepatch"})()
            self.calls = 0

        def run_injected_test(self, spec: Any, layout: Any, **kwargs: Any) -> Any:
            self.calls += 1
            h = StandaloneFunctionUnitHarness()
            if self.calls == 1:
                return h.parse_execution_result({"compiled": False, "error": "syntax error"})
            if self.calls == 2:
                return h.parse_execution_result(
                    {
                        "compiled": True,
                        "stdout": (
                            '[SPECPATCHGUARD_CASE] {"case_id":"seed_tru","status":"error","exception_type":"java.lang.StringIndexOutOfBoundsException","message":"3"}\n'
                            '[SPECPATCHGUARD_CASE] {"case_id":"seed_noise","status":"passed","observed":"false"}\n'
                            '[SPECPATCHGUARD_CASE] {"case_id":"cross_pass","status":"passed","observed":"true"}\n'
                        ),
                    }
                )
            return h.parse_execution_result(
                {
                    "compiled": True,
                    "stdout": (
                        '[SPECPATCHGUARD_CASE] {"case_id":"seed_tru","status":"error","exception_type":"java.lang.StringIndexOutOfBoundsException","message":"3"}\n'
                        '[SPECPATCHGUARD_CASE] {"case_id":"seed_t00","status":"error","exception_type":"java.lang.StringIndexOutOfBoundsException","message":"3"}\n'
                        '[SPECPATCHGUARD_CASE] {"case_id":"cross_pass","status":"passed","observed":"true"}\n'
                    ),
                }
            )

    injector = _MultiRoundInjector()
    reg = StrategyRegistry()
    reg.register_cross_rep(TierAPythonHypothesisStrategy(llm, hypothesis_available=False))
    reg.register_cross_rep(TierBAbstractDecisionTableStrategy())
    reg.register_harness(StandaloneFunctionUnitHarness())
    reg.register_harness(PublicEntryAnchorProbeHarness())

    agent = PbtSpecAgent(llm, settings, registry=reg, junit_injector=injector)  # type: ignore[arg-type]
    layout = D4jProjectLayout(
        project="Lang",
        bug_num=51,
        bug_id="Lang-51",
        version="51b",
        work_dir="/workspace/d4j_work/Lang_51b",
        dir_src_classes="src/java",
        dir_src_tests="src/test",
        dir_bin_classes="target/classes",
        dir_bin_tests="target/tests",
    )
    res = agent.generate_and_refine(intent_pack, layout=layout, dispatcher=clean_disp)

    assert res.status == "ok"
    # CRIT-04: initial spec phase executed dispatcher tool
    assert res.refinement_history[0]["phase"] == "initial_spec_tools"
    assert res.refinement_history[0]["tools"] == ["get_method_context"]
    # WARN-02: only 3 injector calls total (compile fail -> fixed compile -> round 2)
    assert injector.calls == 3
    # CRIT-01: round 1 deferred convergence for seed_t00, round 2 converged and excluded cross_pass while preserving seed_noise
    assert res.refinement_history[1]["converged"] is False
    assert res.refinement_history[1]["converged_deferred_for_new_seeds"] is True
    assert res.refinement_history[2]["converged"] is True
    assert {w.case_id for w in res.witness_bank.confirmed_bug_witnesses} == {"seed_tru", "seed_t00"}
    assert res.witness_bank.boundary_passing_witnesses == []
    assert {w.case_id for w in res.witness_bank.excluded_irrelevant_cases} == {"seed_noise", "cross_pass"}
    assert res.pre_post_spec.invariants == ["still pure"]

