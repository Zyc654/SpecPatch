"""Unit tests for Tier-A clean-room Cross-Rep Python strategy, AST guard, and subprocess execution."""

from __future__ import annotations

import json
from typing import Any

from specpatchguard.agents.llm_client import LLMResponse
from specpatchguard.data.schemas import (
    BugReport,
    DiffAnchor,
    FailingTestRecord,
    IntentContextPack,
    MethodSignatureInfo,
)
from specpatchguard.prompts.pbt_prompts import CROSS_REP_END_MARKER, CROSS_REP_START_MARKER
from specpatchguard.strategies import (
    TierAPythonHypothesisStrategy,
    normalize_json_safe_value,
    validate_clean_room_python,
)


class _ScriptedLLM:
    def __init__(self, responses: list[str]) -> None:
        self.responses = list(responses)
        self.recorded_messages: list[list[dict[str, Any]]] = []

    def chat(self, messages: list[dict[str, Any]], **kwargs: Any) -> LLMResponse:
        self.recorded_messages.append([dict(m) for m in messages])
        text = self.responses.pop(0)
        return LLMResponse(content=text)

    def run_tool_loop(self, *args: Any, **kwargs: Any) -> Any:  # pragma: no cover
        raise AssertionError("Cross-Rep should not invoke tool loop")


def _sample_pack_with_secret_buggy_code() -> IntentContextPack:
    return IntentContextPack(
        bug_id="Math-3",
        patch_id="patch_99",
        project="Math",
        tier="tier_a",
        bug_intent_summary="linearCombination should handle single-element arrays without ArrayIndexOutOfBoundsException",
        anchors=[
            DiffAnchor(
                file_path="src/main/java/org/apache/commons/math3/util/MathArrays.java",
                method_name="linearCombination",
                anchor_start_line=820,
                anchor_end_line=824,
                hunk_old_start=818,
                hunk_old_end=825,
                kind="modify",
                function_start_line=810,
                function_end_line=860,
                buggy_function_code="SECRET_BUGGY_BODY_MARKER_DO_NOT_LEAK_TO_PYTHON",
                numbered_buggy_function="810: SECRET_BUGGY_BODY_MARKER_DO_NOT_LEAK_TO_PYTHON",
                anchor_context_lines="820: SECRET_ANCHOR_CONTEXT_MARKER",
            )
        ],
        target_methods=[
            MethodSignatureInfo(
                file_path="src/main/java/org/apache/commons/math3/util/MathArrays.java",
                class_name="MathArrays",
                method_name="linearCombination",
                visibility="public",
                is_static=True,
                is_constructor=False,
                return_type="double",
                parameter_types=["double[]", "double[]"],
                parameter_names=["a", "b"],
                throws_types=[],
                modifiers=["public", "static"],
                javadoc="Compute a linear combination accurately.",
                raw_signature="public static double linearCombination(final double[] a, final double[] b)",
            )
        ],
        failing_tests=[
            FailingTestRecord(
                test_file="src/test/java/org/apache/commons/math3/util/MathArraysTest.java",
                test_method_name="testLinearCombinationWithSingleElementArray",
                start_line=10,
                source_code="assertEquals(1.0, MathArrays.linearCombination(new double[]{1.0}, new double[]{1.0}), 0.0);",
                numbered_source="10: assertEquals(...);",
                failed_line_start=11,
                failed_line_end=11,
                failure_message="java.lang.ArrayIndexOutOfBoundsException: 1",
            )
        ],
        bug_report=BugReport(bug_id="Math-3", title="Single element array fails", description="", available=True),
    )


def test_validate_clean_room_python_blocks_unsafe_code() -> None:
    assert validate_clean_room_python("import os\nos.system('ls')")
    assert validate_clean_room_python("from subprocess import run")
    assert validate_clean_room_python("open('/etc/passwd').read()")
    assert validate_clean_room_python("eval('1+1')")
    assert validate_clean_room_python("x = ().__class__.__bases__")
    assert validate_clean_room_python("def broken(:\n pass")

    safe = "import json\nimport math\ndef ref_impl(x):\n    return math.fabs(x)\n"
    assert validate_clean_room_python(safe) == []


def test_normalize_json_safe_value_handles_special_floats() -> None:
    normalized = normalize_json_safe_value([float("nan"), float("inf"), float("-inf"), -0.0, 1.5])
    assert normalized == [
        {"__spg_float__": "NaN"},
        {"__spg_float__": "Infinity"},
        {"__spg_float__": "-Infinity"},
        {"__spg_float__": "-0.0"},
        1.5,
    ]
    json.dumps(normalized, allow_nan=False)


def test_cross_rep_clean_room_prompt_and_syntax_fix_retry() -> None:
    pack = _sample_pack_with_secret_buggy_code()
    bad_first_script = "```python\nimport os\nprint('unsafe')\n```"
    good_second_script = f"""```python
import json

def ref_impl(a, b):
    return sum(x * y for x, y in zip(a, b))

def generate_boundary_inputs(max_examples):
    return [
        {{"case_id": "single", "args": [[1.0], [2.0]], "category": "bug_trigger", "rationale": "len 1", "ref_hint": ref_impl([1.0], [2.0])}},
        {{"case_id": "single_dup", "args": [[1.0], [2.0]], "category": "boundary", "rationale": "duplicate"}},
        {{"case_id": "nan_elem", "args": [[{{"__spg_float__": "NaN"}}], [1.0]], "category": "boundary", "rationale": "NaN"}},
    ]

if __name__ == "__main__":
    print("{CROSS_REP_START_MARKER}")
    print(json.dumps(generate_boundary_inputs(10), allow_nan=False))
    print("{CROSS_REP_END_MARKER}")
```"""

    llm = _ScriptedLLM([bad_first_script, good_second_script])
    strategy = TierAPythonHypothesisStrategy(llm, hypothesis_available=False)
    res = strategy.generate_candidates(
        pack,
        {
            "enable_cross_rep_tier_a": True,
            "hypothesis_max_examples": 25,
            "syntax_fix_max_rounds": 3,
        },
    )

    # Clean-room isolation: buggy Java body never leaked to Cross-Rep LLM.
    all_prompt_text = "\n".join(
        str(msg.get("content", "")) for conv in llm.recorded_messages for msg in conv
    )
    assert "SECRET_BUGGY_BODY_MARKER_DO_NOT_LEAK_TO_PYTHON" not in all_prompt_text
    assert "SECRET_ANCHOR_CONTEXT_MARKER" not in all_prompt_text
    assert "hypothesis` is NOT installed" in llm.recorded_messages[0][1]["content"]

    assert res.tier == "tier_a"
    assert res.diagnostics["status"] == "ok"
    assert res.diagnostics["hypothesis_available"] is False
    assert len(res.diagnostics["attempts"]) == 2
    assert res.diagnostics["attempts"][0]["ok"] is False
    assert "Disallowed import 'os'" in res.diagnostics["attempts"][0]["error"]

    # Deduplicated down to 2 distinct arg tuples, with ref_hint kept non-binding.
    assert len(res.candidate_inputs) == 2
    assert res.candidate_inputs[0]["case_id"] == "single"
    assert res.candidate_inputs[0]["ref_hint"] == 2.0
    assert res.candidate_inputs[1]["args"] == [[{"__spg_float__": "NaN"}], [1.0]]


def test_review_regressions_ast_alias_bypass_and_env_sanitization(monkeypatch: pytest.MonkeyPatch) -> None:
    from specpatchguard.strategies.tier_a_cross_rep import _build_sanitized_subprocess_env

    # CRIT-03: alias / reflection / dunder bypasses rejected
    assert validate_clean_room_python("f = open\nf('/etc/passwd')")
    assert validate_clean_room_python("g = globals()\nprint(g)")
    assert validate_clean_room_python("v = vars()")
    assert validate_clean_room_python("x = (1).__doc__")
    assert validate_clean_room_python("class C:\n    def __repr__(self):\n        return 'ok'") == []

    # CRIT-03: subprocess environment strips API secrets
    monkeypatch.setenv("OPENAI_API_KEY", "sk-do-not-leak")
    monkeypatch.setenv("CUSTOM_SECRET_TOKEN", "tok-123")
    monkeypatch.setenv("PATH", "/usr/bin")
    env = _build_sanitized_subprocess_env()
    assert "OPENAI_API_KEY" not in env
    assert "CUSTOM_SECRET_TOKEN" not in env
    assert env.get("PATH") == "/usr/bin"

