"""Prompt builders for Agent 2 (Spec/PBT Extraction, Clean-Room Tier-A Cross-Rep, and Refinement).

Adapts AutoCodeSherpa's Generalization -> Symbolization -> Refinement feedback loop
(`agent_reproducer.py`) to Java Defects4J functions while keeping Cross-Rep strictly
clean-room (never showing buggy Java implementation lines to Python reference modeling).
"""

from __future__ import annotations

import json
from typing import Any

from ..data.schemas import (
    DiffAnchor,
    HarnessRunResult,
    IntentContextPack,
    MethodSignatureInfo,
    PrePostSpec,
    WitnessBank,
)

CROSS_REP_START_MARKER = "__SPG_CROSS_REP_JSON_START__"
CROSS_REP_END_MARKER = "__SPG_CROSS_REP_JSON_END__"

SPEC_EXTRACTION_SYSTEM_PROMPT = """You are a formal specification and property-based test (PBT) engineer for Java Defects4J bugs.
Your job is to derive executable Pre/Post specifications, concrete boundary seed inputs, and optional read-only anchor infection probes for the PRE-PATCH buggy program (Patch-Blind: you must NEVER assume or inspect any candidate patch).
Write ALL reasoning, analysis, summaries, notes, rationales, and JSON string values strictly in English.

Output strictly ONE ```json code block with these keys:
- "preconditions": list of human/machine-readable precondition constraints restricting inputs to valid reachable domains (no null/type noise outside contract)
- "postconditions": list of postcondition assertions expected of a correct fix
- "invariants": list of invariants that must hold across states/calls
- "exception_contracts": list of expected exception behaviors (e.g. "throws IllegalArgumentException when input < 0" or "must NOT throw StringIndexOutOfBoundsException for 3-char string")
- "anchor_infection_conditions": list of Java-style read-only boolean expressions describing the fault infection state at diff anchor lines
- "receiver_setup_java": string of Java statements constructing `_target` instance if target method is non-static (empty string "" if static)
- "seed_inputs": list of objects, each containing:
    - "case_id": short identifier string (e.g. "bug_trigger_tru", "boundary_empty")
    - "args": ordered list of JSON-compatible argument values matching the Java method's parameters (for special floats use {"__spg_float__": "NaN"|"Infinity"|"-Infinity"|"-0.0"})
    - "category": one of "bug_trigger", "boundary", "normal"
    - "expected_behavior": short description of expected result
    - "expected_return": optional expected return value (omit or null if unknown / void / exception expected)
    - "expected_exception": optional expected exception class name if an exception IS required by contract (empty string "" otherwise)
    - "java_assertion": optional single-line Java boolean expression over `_actual` (e.g. "_actual == true")
- "anchor_probes": list of optional read-only line probe objects with keys:
    - "probe_id": short identifier
    - "file_path": repository-relative Java path
    - "line_number": integer >= 1
    - "position": "before" or "after"
    - "condition_expr": optional single-line read-only Java boolean expression (no assignments, no ';', no comments)
    - "observed_expressions": list of single-line read-only Java expressions to log
"""

CROSS_REP_TIER_A_SYSTEM_PROMPT = """You are a clean-room software specification engineer.
You will receive ONLY the natural-language bug intent summary (plus structured parameter/field/local_var semantics, callee contracts, and structured defect intent when available), the Java method signature + Javadoc, failing test expectations, and boundary notes — you are intentionally NOT shown the buggy Java implementation so your Python reference model does not replicate Java implementation mistakes.
Write ALL reasoning, analysis, summaries, notes, rationales, and JSON string values strictly in English.

Write ONE self-contained Python script inside a single ```python code block that:
1. Implements a clean-room Python reference model `ref_impl(...)` matching the Java method signature semantics.
2. Implements `generate_boundary_inputs(max_examples: int)` that systematically explores normal, boundary, edge, and bug-triggering inputs (using `hypothesis` when instructed, or deterministic + randomized boundary generation in pure Python stdlib when `hypothesis` is unavailable).
3. In `if __name__ == "__main__":`, prints ONLY a JSON array of candidate input objects between the exact markers:
   __SPG_CROSS_REP_JSON_START__
   [...]
   __SPG_CROSS_REP_JSON_END__
   where each element has:
   {"case_id": str, "args": [...], "category": "bug_trigger"|"boundary"|"normal", "rationale": str, "ref_hint": <optional python output>}
4. For special floats (`NaN`, `Infinity`, `-Infinity`, `-0.0`), encode them as {"__spg_float__": "NaN"} / {"__spg_float__": "Infinity"} / {"__spg_float__": "-Infinity"} / {"__spg_float__": "-0.0"} so `json.dumps(..., allow_nan=False)` succeeds cleanly.
5. Security/Sandbox rules: ONLY use safe standard library modules (`json`, `math`, `decimal`, `fractions`, `datetime`, `re`, `string`, `itertools`, `functools`, `collections`, `typing`, `dataclasses`, `random`) and `hypothesis` (if enabled). NEVER import `os`, `sys`, `subprocess`, `pathlib`, `socket`, or call `open`, `exec`, `eval`, `compile`, `__import__`.
"""


def _format_signature_brief(sig: MethodSignatureInfo) -> str:
    params = ", ".join(
        f"{t} {n}".strip() for t, n in zip(sig.parameter_types, sig.parameter_names)
    )
    throws_part = f" throws {', '.join(sig.throws_types)}" if sig.throws_types else ""
    doc = sig.javadoc.strip() or "[No Javadoc]"
    return (
        f"Class: {sig.class_name} | File: {sig.file_path}\n"
        f"Signature: {' '.join(sig.modifiers)} {sig.return_type} {sig.method_name}({params}){throws_part}\n"
        f"Javadoc:\n{doc}"
    )


def _format_anchor_summary(anchor: DiffAnchor) -> str:
    body = anchor.numbered_buggy_function.strip() or anchor.buggy_function_code.strip()
    return (
        f"- File: {anchor.file_path} | Method: {anchor.method_name} | "
        f"Anchor Lines: {anchor.anchor_start_line}..{anchor.anchor_end_line} ({anchor.kind})\n"
        f"<PrePatchMethod>\n{body}\n</PrePatchMethod>"
    )


def _format_structured_semantics(intent_pack: IntentContextPack) -> str:
    """Format Agent 1's structured semantic tables cleanly for intent-driven modeling."""
    payload: dict[str, Any] = {}
    if intent_pack.parameter_field_semantics:
        payload["parameter_field_semantics"] = intent_pack.parameter_field_semantics
    if intent_pack.callee_contracts:
        payload["callee_contracts"] = intent_pack.callee_contracts
    if intent_pack.structured_defect_intent:
        payload["structured_defect_intent"] = intent_pack.structured_defect_intent
    if not payload:
        return "[None provided]"
    return json.dumps(payload, ensure_ascii=False, indent=2)


def build_spec_extraction_prompt(intent_pack: IntentContextPack) -> str:
    """Build initial Pre/Post specification + seed input extraction prompt for Agent 2."""
    sigs = "\n\n".join(_format_signature_brief(m) for m in intent_pack.target_methods) or "[No signature parsed]"
    anchors = "\n\n".join(_format_anchor_summary(a) for a in intent_pack.anchors) or "[No anchor]"
    tests = "\n\n".join(
        f"Test `{t.test_file}::{t.test_method_name}`:\n{t.numbered_source or t.source_code}\nFailure: {t.failure_message}"
        for t in intent_pack.failing_tests
    ) or "[No failing test snippet]"

    bc = intent_pack.boundary_context
    boundary_summary = json.dumps(
        {
            "enclosing_branches_count": len(bc.enclosing_branches),
            "class_fields": bc.class_fields[:12],
            "callers_count": len(bc.callers),
            "overrides_count": len(bc.overrides),
            "notes": bc.notes,
        },
        ensure_ascii=False,
        indent=2,
    )
    semantics_block = _format_structured_semantics(intent_pack)

    return (
        f"Bug ID: {intent_pack.bug_id} | Tier: {intent_pack.tier}\n"
        f"Write ALL reasoning, analysis, summaries, notes, rationales, and JSON string values strictly in English.\n"
        f"Bug Intent Summary:\n{intent_pack.bug_intent_summary}\n\n"
        f"# Structured Semantic Tables (Intent, Variables, Callee Contracts)\n{semantics_block}\n\n"
        f"# Target Method Signatures\n{sigs}\n\n"
        f"# Pre-Patch Anchors (Patch-Blind)\n{anchors}\n\n"
        f"# Failing Tests\n{tests}\n\n"
        f"# Boundary & Context Facts\n{boundary_summary}\n"
    )


def build_cross_rep_tier_a_prompt(
    intent_pack: IntentContextPack,
    max_examples: int,
    *,
    use_hypothesis: bool = True,
) -> str:
    """Build clean-room Python + Hypothesis prompt (strictly omits buggy Java body)."""
    sigs = "\n\n".join(_format_signature_brief(m) for m in intent_pack.target_methods) or "[No signature parsed]"
    # Deliberately include only failing test code/messages (oracle expectations), NEVER buggy Java function bodies.
    tests = "\n\n".join(
        f"Failing Test `{t.test_file}::{t.test_method_name}`:\n{t.source_code}\nExpected/Failure: {t.failure_message}"
        for t in intent_pack.failing_tests
    ) or "[No failing test snippet]"
    notes = "\n".join(f"- {note}" for note in intent_pack.boundary_context.notes) or "- None"
    semantics_block = _format_structured_semantics(intent_pack)

    sampling_instruction = (
        f"Use `@given(...)` and `@settings(max_examples={max_examples}, deadline=None, database=None)` "
        "from `hypothesis` to collect diverse boundary inputs and shrink counterexamples."
        if use_hypothesis
        else f"Note: `hypothesis` is NOT installed in this Python runtime. Use deterministic boundary enumeration + "
        f"`random.Random(42)` sampling (up to {max_examples} examples) using ONLY Python standard library modules."
    )

    report = intent_pack.bug_report
    report_text = (
        f"Title: {report.title}\nDescription: {report.description}"
        if report.available or report.title or report.description
        else "[Bug report unavailable]"
    )

    return (
        f"Clean-room Python Cross-Representation Boundary Exploration for `{intent_pack.bug_id}`.\n"
        "Model by intent using the summary, structured semantic tables, signature/Javadoc, and test expectations.\n"
        "Write ALL reasoning, analysis, summaries, notes, rationales, and JSON string values strictly in English.\n\n"
        f"# Intended Behavior Summary\n{intent_pack.bug_intent_summary}\n\n"
        f"# Structured Semantic Tables (Intent, Variables, Callee Contracts)\n{semantics_block}\n\n"
        f"# Target Method Signature & Javadoc\n{sigs}\n\n"
        f"# Bug Report\n{report_text}\n\n"
        f"# Failing Test Expectations\n{tests}\n\n"
        f"# Boundary Notes\n{notes}\n\n"
        f"# Sampling Requirement\n{sampling_instruction}\n"
        f"Target up to {max_examples} total examples and deduplicate down to the highest-signal boundary cases "
        f"(aim for 8-30 sharp cases). Print JSON strictly between `{CROSS_REP_START_MARKER}` and `{CROSS_REP_END_MARKER}`.\n"
    )


def build_cross_rep_fix_prompt(
    previous_code: str,
    error_output: str,
    *,
    use_hypothesis: bool = True,
) -> str:
    """Ask LLM to repair a failed clean-room Python boundary generator script."""
    mode_hint = (
        "Keep using `hypothesis` safely."
        if use_hypothesis
        else "Do NOT import `hypothesis`; use Python standard library only."
    )
    return (
        "Your previous Python boundary-input script failed validation or execution.\n"
        "Write ALL reasoning, analysis, summaries, notes, rationales, and JSON string values strictly in English.\n"
        f"{mode_hint}\n\n"
        f"Error:\n```\n{error_output.strip()}\n```\n\n"
        f"Previous script:\n```python\n{previous_code.strip()}\n```\n\n"
        "Return a corrected, complete script inside a single ```python code block. "
        f"Remember to print the JSON list between `{CROSS_REP_START_MARKER}` and `{CROSS_REP_END_MARKER}` "
        "and avoid disallowed imports (`os`, `sys`, `subprocess`, `open`, `eval`, `exec`)."
    )


def build_junit_syntax_fix_prompt(junit_source: str, compile_error: str) -> str:
    """Ask LLM to fix JUnit 4 / JDK 8 compilation errors while preserving `[SPECPATCHGUARD_CASE]` markers."""
    return (
        "The generated JUnit 4 test harness failed to compile under Java 8 / Defects4J.\n"
        "Write ALL reasoning, comments, and assertions strictly in English.\n"
        "Fix the compilation error while keeping:\n"
        "1. JDK 8 compatibility (no `List.of`, `Map.of`, `var`, switch expressions, or text blocks).\n"
        "2. JUnit 4 annotations (`org.junit.Test`) and `[SPECPATCHGUARD_CASE]` JSON logging format intact.\n"
        "3. Exact target class/method call signatures.\n\n"
        f"# Compilation Error\n```\n{compile_error.strip()}\n```\n\n"
        f"# Previous JUnit Source\n```java\n{junit_source.strip()}\n```\n\n"
        "Return ONLY the complete corrected Java file inside a single ```java code block."
    )


def build_pbt_refinement_prompt(
    intent_pack: IntentContextPack,
    current_spec: PrePostSpec,
    run_result: HarnessRunResult,
    witness_bank: WitnessBank,
    round_idx: int,
    max_rounds: int,
) -> str:
    """Build AutoCodeSherpa-style refinement feedback prompt from real Java sandbox execution on buggy `P`."""
    summary_payload = {
        "round": f"{round_idx}/{max_rounds}",
        "compiled": run_result.compiled,
        "passed_count": len(run_result.passed_cases),
        "failed_count": len(run_result.failed_cases),
        "error_count": len(run_result.error_cases),
        "passed_cases_sample": run_result.passed_cases[:10],
        "failed_cases_sample": run_result.failed_cases[:10],
        "error_cases_sample": run_result.error_cases[:10],
        "probe_traces_sample": run_result.infection_probe_traces[:10],
        "current_witness_counts": {
            "confirmed_bug_witnesses": len(witness_bank.confirmed_bug_witnesses),
            "boundary_passing_witnesses": len(witness_bank.boundary_passing_witnesses),
            "excluded_irrelevant_cases": len(witness_bank.excluded_irrelevant_cases),
        },
    }

    return (
        f"Refinement Round {round_idx}/{max_rounds} on PRE-PATCH buggy Java code for `{intent_pack.bug_id}`.\n"
        "Write ALL reasoning, analysis, summaries, notes, rationales, and JSON string values strictly in English.\n\n"
        "Analyze the Java sandbox execution results:\n"
        "- If error_cases contain irrelevant precondition violations (e.g. null/illegal inputs outside the method's contract), "
        "tighten `preconditions` and list their `case_id`s in `excluded_case_ids`.\n"
        "- If confirmed bug failures are not yet caught or boundary cases are missing, tighten `postconditions` / `exception_contracts` "
        "and propose additional `seed_inputs`.\n"
        "- Set `converged: true` once preconditions cleanly exclude irrelevant noise and we have sharp confirmed bug witnesses + boundary passing witnesses.\n\n"
        f"# Current PrePostSpec\n```json\n{json.dumps(current_spec.to_dict(), ensure_ascii=False, indent=2)}\n```\n\n"
        f"# Java Sandbox Execution Feedback\n```json\n{json.dumps(summary_payload, ensure_ascii=False, indent=2)}\n```\n\n"
        "Return strictly ONE ```json code block with keys: "
        '"preconditions", "postconditions", "invariants", "exception_contracts", "anchor_infection_conditions", '
        '"excluded_case_ids" (list of strings), "seed_inputs" (list of additional case dicts), '
        '"converged" (boolean), and "refinement_notes" (string).'
    )
