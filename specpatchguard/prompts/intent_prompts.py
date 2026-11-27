"""Patch-Blind prompts for Agent 1 (Intent & Context Explorer).

Adapts FURINA's `PROFILER_AGENT_SYSTEM_WITH_BUG_REPORT_PROMPT` / summary format
and Multi2Fixer's `context_retrieve_prompt` while enforcing strict Patch-Blind
isolation (only pre-patch anchors, tests, and bug report; never `+` diff lines).
"""

from __future__ import annotations

from ..data.schemas import DiffAnchor, FailingTestRecord, IntentContextPack, MethodSignatureInfo, PatchBlindTask

INTENT_SYSTEM_PROMPT = """You are an experienced software engineer responsible for investigating a Java defect from failing tests, a bug report (if available), and pre-patch diff anchor coordinates.

CRITICAL RULES (Patch-Blind Isolation & English-Only Output):
1. You do NOT have access to any candidate patch and MUST NOT guess or invent post-patch additions.
2. Write ALL reasoning, analysis, summaries, notes, rationales, and JSON string values strictly in English.
3. The failing test suite is AUTHORITATIVE: analyze why the pre-patch program fails the tests and what intended contract the tests enforce.
4. The bug report (when available) describes symptoms reported by users; verify details against the actual pre-patch repository using the provided inspection tools (`get_enclosing_branches`, `get_class_members`, `get_method_context`, `trace_symbol_usage`, `grep_code`, `read_file_lines`, `lsp_goto_definition`, `lsp_find_references`, `lsp_hover`, `lsp_type_hierarchy`).
5. Never rely solely on a truncated snippet when surrounding control flow, helper definitions, fields, callers, or overrides are needed to understand preconditions, invariants, or return-value contracts.
6. Produce COMPLETE method-level intent AND explicit symbol/variable semantics (parameters, class fields, in-method `local_var` variables/flags, return value, and callee helper contracts) so downstream specification agents can model by intent rather than literal line-by-line Java translation.
7. After gathering sufficient context, output your final analysis as a SINGLE JSON object wrapped in a ```json code block with exact keys:
   - "bug_intent_summary": string summarizing the target method's overall design responsibility, intended behavior, and pre-patch defect mechanism
   - "root_cause_hypothesis": string explaining why the anchor lines trigger the failure
   - "anchor_analysis": list of strings covering each diff anchor's preconditions/branches/side-effects
   - "boundary_notes": list of strings capturing visibility, callers, overrides, non-local state, or return nullability/mutability contracts
   - "parameter_field_semantics": list of objects `{ "name": string, "kind": "param"|"field"|"local_var"|"return", "java_type": string, "semantic_role": string, "valid_domain_or_invariants": string, "nullable_or_edge_cases": string }` (must explicitly include key in-method `local_var` variables and state flags!)
   - "callee_contracts": list of objects `{ "callee_signature": string, "resolved_location": string (repo-relative path), "precondition": string, "return_semantics": string, "side_effects": string, "exception_behavior": string }`
   - "structured_defect_intent": object `{ "method_responsibility": string, "bug_report_claims": list[string], "failing_test_assertions": list[string], "trigger_conditions": list[string], "expected_postcondition_summary": string, "bug_report_available": boolean }`
   - "recommended_tier": either "tier_a" (standalone/weak-state algorithm) or "tier_b" (heavy framework/AST/stateful coupling)
   - "entry_harness_hint": string naming the preferred JUnit entry method or standalone target method signature
"""

INTENT_FINALIZE_PROMPT = (
    "Tool call budget or round limit has been reached. Do NOT call any more tools. "
    "Write ALL reasoning, analysis, summaries, notes, rationales, and JSON string values strictly in English. "
    "Synthesize everything observed so far into the required single ```json code block "
    'with keys: "bug_intent_summary", "root_cause_hypothesis", "anchor_analysis", '
    '"boundary_notes", "parameter_field_semantics", "callee_contracts", "structured_defect_intent", '
    '"recommended_tier", and "entry_harness_hint".'
)

INTENT_JSON_REPAIR_PROMPT = (
    "Your previous response could not be parsed as a valid JSON object matching the required schema. "
    "Write ALL reasoning, analysis, summaries, notes, rationales, and JSON string values strictly in English. "
    "Return ONLY one ```json code block containing a JSON object with keys: "
    '"bug_intent_summary" (string), "root_cause_hypothesis" (string), '
    '"anchor_analysis" (array of strings), "boundary_notes" (array of strings), '
    '"parameter_field_semantics" (array of objects), "callee_contracts" (array of objects), '
    '"structured_defect_intent" (object), "recommended_tier" ("tier_a" or "tier_b"), and "entry_harness_hint" (string).'
)


def _format_bug_report(task: PatchBlindTask) -> str:
    report = task.bug_report
    if not report.available and not report.title.strip() and not report.description.strip():
        return (
            "<BugReport>\n"
            "[BUG REPORT UNAVAILABLE FOR THIS DEFECT]\n"
            "</BugReport>"
        )
    title = report.title.strip() or "[No title]"
    desc = report.description.strip() or "[No description]"
    return (
        "<BugReport>\n"
        f"Title: {title}\n"
        f"Content: {desc}\n"
        "</BugReport>"
    )


def _format_failing_test(idx: int, test: FailingTestRecord, *, max_trace_chars: int = 1600) -> str:
    pos_block = (
        f"<FailPosition>\nThis test fails around lines {test.failed_line_start} to {test.failed_line_end}.\n</FailPosition>"
        if test.failed_line_start is not None and test.failed_line_end is not None
        else "<FailPosition>\n[FAILING POSITION UNAVAILABLE CURRENTLY]\n</FailPosition>"
    )
    src = test.numbered_source.strip() or test.source_code.strip() or "[TEST SOURCE UNAVAILABLE]"
    msg = test.failure_message.strip() or "[No failure message]"
    trace = test.stack_trace.strip()
    if len(trace) > max_trace_chars:
        trace = trace[:max_trace_chars] + "\n...[TRUNCATED]"
    trace_block = f"\n<StackTrace>\n{trace}\n</StackTrace>" if trace else ""
    return (
        f"Test #{idx} ({test.test_file} :: {test.test_method_name}):\n"
        f"<TestMethod>\n{src}\n</TestMethod>\n"
        f"{pos_block}\n"
        f"<FailInfo>\n{msg}\n</FailInfo>"
        f"{trace_block}"
    )


def _format_anchor(idx: int, anchor: DiffAnchor) -> str:
    code = anchor.numbered_buggy_function.strip() or anchor.buggy_function_code.strip() or "[CODE UNAVAILABLE]"
    ctx = anchor.anchor_context_lines.strip()
    ctx_block = f"\n<AnchorContextLines>\n{ctx}\n</AnchorContextLines>" if ctx else ""
    return (
        f"Anchor #{idx}:\n"
        f"- file_path: {anchor.file_path}\n"
        f"- method_name: {anchor.method_name or '[unknown]'}\n"
        f"- anchor_lines: {anchor.anchor_start_line}..{anchor.anchor_end_line} (kind={anchor.kind}, fallback={anchor.is_fallback_slice})\n"
        f"<PrePatchMethod>\n{code}\n</PrePatchMethod>"
        f"{ctx_block}"
    )


def _format_method_signature(sig: MethodSignatureInfo) -> str:
    params = ", ".join(
        f"{t} {n}".strip() for t, n in zip(sig.parameter_types, sig.parameter_names)
    )
    throws_part = f" throws {', '.join(sig.throws_types)}" if sig.throws_types else ""
    doc_part = f"\n  Javadoc: {sig.javadoc}" if sig.javadoc.strip() else ""
    return (
        f"- {sig.file_path} :: {sig.class_name}.{sig.method_name}({params}){throws_part}\n"
        f"  visibility={sig.visibility}, static={sig.is_static}, return_type={sig.return_type or '<constructor>'}"
        f"{doc_part}"
    )


def build_intent_user_prompt(
    task: PatchBlindTask,
    initial_pack: IntentContextPack,
    *,
    has_tools: bool = True,
) -> str:
    """Build the initial Patch-Blind user prompt for `IntentContextAgent`."""
    tests_text = "\n\n".join(
        _format_failing_test(i + 1, test) for i, test in enumerate(task.failing_tests)
    ) or "[NO FAILING TESTS IN PREPROCESSED RECORD — USE TOOLS TO INSPECT]"

    anchors_text = "\n\n".join(
        _format_anchor(i + 1, anchor) for i, anchor in enumerate(task.anchors)
    ) or "[NO ANCHORS PROVIDED]"

    sigs_text = "\n".join(
        _format_method_signature(sig) for sig in initial_pack.target_methods
    ) or "[No parsed method signatures]"

    tool_guidance = (
        "Use the Docker repository & LSP tools to inspect enclosing branches, class fields/visibility, "
        "helper definitions, callers (`exclude_tests=true`), and symbol usages around the anchor lines "
        "before emitting your final JSON summary."
        if has_tools
        else "No interactive tools are attached for this run; analyze the provided pre-patch context carefully."
    )

    tier_hint = ""
    if initial_pack.tier_decision is not None:
        reasons = "; ".join(initial_pack.tier_decision.reasons)
        tier_hint = f"\nInitial heuristic tier: {initial_pack.tier_decision.tier} ({reasons})"

    return (
        f"Investigate Defects4J bug `{task.bug_id}` (project={task.project}, "
        f"multi_hunk={task.is_multi_hunk}, multi_file={task.is_multi_file}).{tier_hint}\n\n"
        f"{tool_guidance}\n\n"
        f"# Bug Report\n{_format_bug_report(task)}\n\n"
        f"# Failing Tests\n{tests_text}\n\n"
        f"# Parsed Target Method Signatures\n{sigs_text}\n\n"
        f"# Pre-Patch Diff Anchors (Patch-Blind)\n{anchors_text}\n"
    )
