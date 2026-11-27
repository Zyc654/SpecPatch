"""Unit tests for Tier-A Java literal conversion, StandaloneFunctionUnitHarness, and StrategyRegistry."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from specpatchguard.data.schemas import (
    BugReport,
    CrossRepStrategy,
    DiffAnchor,
    IntentContextPack,
    MethodSignatureInfo,
    PrePostSpec,
    TestHarnessStrategy,
)
from specpatchguard.strategies import (
    HarnessBuildError,
    PublicEntryAnchorProbeHarness,
    StandaloneFunctionUnitHarness,
    StrategyRegistry,
    TierBAbstractDecisionTableStrategy,
    infer_java_package_and_class,
    python_value_to_java_literal,
)


def _sample_intent_pack(*, is_static: bool = True, file_path: str = "src/java/org/apache/commons/lang/BooleanUtils.java") -> IntentContextPack:
    return IntentContextPack(
        bug_id="Lang-51",
        patch_id="patch_1",
        project="Lang",
        tier="tier_a",
        bug_intent_summary="toBoolean('tru') should return false without StringIndexOutOfBoundsException",
        anchors=[
            DiffAnchor(
                file_path=file_path,
                method_name="toBoolean",
                anchor_start_line=677,
                anchor_end_line=680,
                hunk_old_start=674,
                hunk_old_end=680,
                kind="modify",
                function_start_line=660,
                function_end_line=690,
                buggy_function_code="public static boolean toBoolean(String str) { return false; }",
                numbered_buggy_function="660: public static boolean toBoolean(String str) { return false; }",
                anchor_context_lines="677: break;",
            )
        ],
        target_methods=[
            MethodSignatureInfo(
                file_path=file_path,
                class_name="BooleanUtils",
                method_name="toBoolean",
                visibility="public",
                is_static=is_static,
                is_constructor=False,
                return_type="boolean",
                parameter_types=["String"],
                parameter_names=["str"],
                throws_types=[],
                modifiers=["public", "static"] if is_static else ["public"],
                javadoc="Converts a String to a boolean.",
                raw_signature="public static boolean toBoolean(String str)",
            )
        ],
        failing_tests=[],
        bug_report=BugReport(bug_id="Lang-51", title="toBoolean fails on tru", description="", available=True),
    )


def test_python_value_to_java_literal_boundaries_and_escapes() -> None:
    assert python_value_to_java_literal(None, "String") == "null"
    assert python_value_to_java_literal(None, "int[]") == "null"
    with pytest.raises(HarnessBuildError):
        python_value_to_java_literal(None, "int")
    with pytest.raises(HarnessBuildError):
        python_value_to_java_literal(True, "int")

    assert python_value_to_java_literal(True, "boolean") == "true"
    assert python_value_to_java_literal(False, "Boolean") == "Boolean.valueOf(false)"
    assert python_value_to_java_literal(-128, "byte") == "(byte) -128"
    assert python_value_to_java_literal(32767, "short") == "(short) 32767"
    assert python_value_to_java_literal(-2147483648, "int") == "Integer.MIN_VALUE"
    assert python_value_to_java_literal(-9223372036854775808, "long") == "Long.MIN_VALUE"
    assert python_value_to_java_literal(42, "long") == "42L"
    assert python_value_to_java_literal(10**30, "BigInteger") == f'new java.math.BigInteger("{10**30}")'

    with pytest.raises(HarnessBuildError):
        python_value_to_java_literal(200, "byte")
    with pytest.raises(HarnessBuildError):
        python_value_to_java_literal(2**40, "int")

    assert python_value_to_java_literal({"__spg_float__": "NaN"}, "double") == "Double.NaN"
    assert python_value_to_java_literal({"__spg_float__": "Infinity"}, "float") == "Float.POSITIVE_INFINITY"
    assert python_value_to_java_literal({"__spg_float__": "-Infinity"}, "Double") == "Double.valueOf(Double.NEGATIVE_INFINITY)"
    assert python_value_to_java_literal({"__spg_float__": "-0.0"}, "double") == "-0.0d"
    with pytest.raises(HarnessBuildError):
        python_value_to_java_literal(float("nan"), "BigDecimal")

    assert python_value_to_java_literal("\n", "char") == "'\\n'"
    assert python_value_to_java_literal("'", "char") == "'\\''"

    str_lit = python_value_to_java_literal('a\nb\rc\t"\\', "String")
    assert str_lit == '"a\\nb\\rc\\t\\"\\\\"'
    assert "\\u000a" not in str_lit and "\\u0022" not in str_lit

    arr_lit = python_value_to_java_literal([[1, 2], [3]], "int[][]")
    assert arr_lit == "new int[][] { new int[] { 1, 2 }, new int[] { 3 } }"

    list_lit = python_value_to_java_literal(["a", "b"], "List<String>")
    assert "java.util.Arrays.asList" in list_lit
    assert "List.of" not in list_lit

    map_lit = python_value_to_java_literal({"k": 1}, "Map<String, Integer>")
    assert "java.util.HashMap" in map_lit and 'put("k", Integer.valueOf(1))' in map_lit
    assert "Map.of" not in map_lit


def test_infer_package_and_standalone_junit_harness_generation() -> None:
    pkg, cls = infer_java_package_and_class("src/main/java/org/apache/commons/math3/util/FastMath.java")
    assert (pkg, cls) == ("org.apache.commons.math3.util", "FastMath")

    pack = _sample_intent_pack()
    harness = StandaloneFunctionUnitHarness()
    spec = PrePostSpec(postconditions=["toBoolean(\"tru\") == false"])
    inputs = [
        {
            "case_id": "bug_tru",
            "args": ["tru"],
            "expected_return": False,
            "java_assertion": "_actual == false",
        },
        {
            "case_id": "valid_true",
            "args": ["true"],
            "expected_return": True,
        },
        {
            "case_id": "bad_arity_skipped",
            "args": ["a", "b"],
        },
    ]

    injected = harness.build_injected_test_spec(spec, inputs, pack)
    assert injected.package_name == "org.apache.commons.lang"
    assert injected.fqcn == "org.apache.commons.lang.SpecPatchGuardGeneratedTest"
    assert "import org.junit.Test;" in injected.java_source
    assert "BooleanUtils.toBoolean(\"tru\")" in injected.java_source
    assert "[SPECPATCHGUARD_CASE]" in injected.java_source
    assert "Skipped case #3 (bad_arity_skipped)" in injected.java_source
    assert "List.of" not in injected.java_source

    parsed = harness.parse_execution_result(
        {
            "compiled": True,
            "stdout": (
                '[SPECPATCHGUARD_CASE] {"case_id":"bug_tru","status":"failed","message":"Postcondition failed"}\n'
                '[SPECPATCHGUARD_CASE] {"case_id":"valid_true","status":"passed","observed":"true"}\n'
                '[SPECPATCHGUARD_PROBE] {"probe_id":"p1","file":"BooleanUtils.java","line":677,"cond":true}\n'
            ),
            "stderr": "",
        }
    )
    assert parsed.compiled is True
    assert len(parsed.passed_cases) == 1
    assert len(parsed.failed_cases) == 1
    assert len(parsed.infection_probe_traces) == 1


def test_strategy_registry_and_tier_b_placeholders() -> None:
    dummy_llm = SimpleNamespace(chat=lambda *a, **kw: None, run_tool_loop=lambda *a, **kw: None)
    registry = StrategyRegistry.default(dummy_llm)  # type: ignore[arg-type]

    cr_a = registry.get_cross_rep("tier_a")
    cr_b = registry.get_cross_rep("tier_b")
    h_a = registry.get_harness("tier_a")
    h_b = registry.get_harness("tier_b")

    assert isinstance(cr_a, CrossRepStrategy)
    assert isinstance(cr_b, TierBAbstractDecisionTableStrategy)
    assert isinstance(h_a, TestHarnessStrategy)
    assert isinstance(h_b, PublicEntryAnchorProbeHarness)

    disabled_res = cr_b.generate_candidates(_sample_intent_pack(), {"enable_cross_rep_tier_b": False})
    assert disabled_res.tier == "tier_b"
    assert disabled_res.diagnostics["status"] == "disabled_placeholder"

    with pytest.raises(NotImplementedError):
        cr_b.generate_candidates(_sample_intent_pack(), {"enable_cross_rep_tier_b": True})

    with pytest.raises(NotImplementedError):
        h_b.build_junit_source(PrePostSpec(), [], _sample_intent_pack())

    parsed_b = h_b.parse_execution_result({"compiled": False, "error": "compile failed"})
    assert parsed_b.tier == "tier_b" and parsed_b.compiled is False


def test_review_regressions_jdk8_collections_arrays_float_overflow_and_escaping() -> None:
    # CRIT-02: float overflow rejected
    with pytest.raises(HarnessBuildError, match="float overflow"):
        python_value_to_java_literal(1e40, "float")

    # CRIT-02: JDK 8 explicit-type collections (no `HashMap<>() {{` diamond-anonymous syntax)
    map_lit = python_value_to_java_literal({"a": 1}, "Map<String, Integer>")
    assert "new java.util.HashMap<String, Integer>() {{" in map_lit
    nested_list = python_value_to_java_literal([["x"], []], "List<List<String>>")
    assert "new java.util.ArrayList<List<String>>(" in nested_list
    assert "java.util.Arrays.<List<String>>asList(" in nested_list
    assert "new java.util.ArrayList<String>()" in nested_list

    # CRIT-02 & WARN-05: array return assertions + safe Arrays.toString/deepToString + comment/quote sanitization
    pack = _sample_intent_pack()
    pack.target_methods[0] = MethodSignatureInfo(
        file_path="src/java/org/apache/commons/lang/BooleanUtils.java",
        class_name="BooleanUtils",
        method_name="flags",
        visibility="public",
        is_static=True,
        is_constructor=False,
        return_type="int[]",
        parameter_types=["String"],
        parameter_names=["s"],
        throws_types=[],
        modifiers=["public", "static"],
        javadoc="",
        raw_signature="public static int[] flags(String s)",
    )
    harness = StandaloneFunctionUnitHarness()
    src = harness.build_junit_source(
        PrePostSpec(postconditions=["line1\nline2"]),
        [
            {"case_id": 'id\nwith"quote', "args": ["ok"], "expected_return": [1, 2]},
            {"case_id": "exc_case", "args": ["ok"], "expected_exception": 'Bad"Exc\n'},
        ],
        pack,
    )
    assert "java.util.Arrays.equals(_actual, new int[] { 1, 2 })" in src
    assert "java.util.Arrays.toString(_actual)" in src
    assert "line1 line2" in src
    assert "Bad\\\"Exc" in src or 'Expected exception Bad\\"Exc' in src
    # No raw newline inside // comment
    for line in src.splitlines():
        if line.lstrip().startswith("//"):
            assert "\n" not in line

