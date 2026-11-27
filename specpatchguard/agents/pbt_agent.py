"""Agent 2 (Spec & PBT Generator + Java Sandbox Refiner).

Combines initial Pre/Post specification extraction, Cross-Rep Strategy candidate
generation (Tier-A Python+Hypothesis or Tier-B placeholder), and real Java D4J
sandbox execution + refinement loop to build a verified `WitnessBank`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import json
from pathlib import Path
import re
from typing import Any

from ..analysis.tool_registry import ToolDispatcher
from ..core.config_loader import Settings, atomic_json
from .intent_agent import _assert_dispatcher_patch_blind
from ..data.schemas import (
    CrossRepResult,
    HarnessRunResult,
    IntentContextPack,
    PrePostSpec,
    SoftSpecCandidate,
    TierType,
    WitnessBank,
    WitnessCase,
)
from ..prompts.pbt_prompts import (
    SPEC_EXTRACTION_SYSTEM_PROMPT,
    build_junit_syntax_fix_prompt,
    build_pbt_refinement_prompt,
    build_spec_extraction_prompt,
)
from ..sandbox.d4j_runner import D4jProjectLayout
from ..sandbox.docker_backend import SandboxError
from ..sandbox.junit_injector import AnchorProbeSpec, InjectedTestSpec, JUnitInjector
from ..strategies.protocols import StrategyRegistry
from ..strategies.tier_a_cross_rep import normalize_json_safe_value
from ..strategies.tier_a_harness import HarnessBuildError, StandaloneFunctionUnitHarness
from .llm_client import LLMClient, LLMParseError, extract_code_blocks, extract_json_object

_IRRELEVANT_INPUT_HINTS = (
    "IllegalArgumentException",
    "NullPointerException",
    "UnsupportedOperationException",
)


@dataclass
class SpecRefinementResult:
    """Output of `PbtSpecAgent.generate_and_refine` ready for Step 6 Agent 3."""

    bug_id: str
    patch_id: str
    tier: TierType
    pre_post_spec: PrePostSpec
    witness_bank: WitnessBank
    soft_spec_candidates: list[SoftSpecCandidate] = field(default_factory=list)
    cross_rep_result: CrossRepResult | None = None
    junit_source: str = ""
    anchor_probes: list[AnchorProbeSpec] = field(default_factory=list)
    refinement_history: list[dict[str, Any]] = field(default_factory=list)
    status: str = "ok"

    def to_dict(self) -> dict[str, Any]:
        return {
            "bug_id": self.bug_id,
            "patch_id": self.patch_id,
            "tier": self.tier,
            "pre_post_spec": self.pre_post_spec.to_dict(),
            "witness_bank": self.witness_bank.to_dict(),
            "soft_spec_candidates": [s.to_dict() for s in self.soft_spec_candidates],
            "cross_rep_result": self.cross_rep_result.to_dict() if self.cross_rep_result else None,
            "junit_source": self.junit_source,
            "anchor_probes": [p.to_dict() for p in self.anchor_probes],
            "refinement_history": [dict(r) for r in self.refinement_history],
            "status": self.status,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> SpecRefinementResult:
        raw_cr = data.get("cross_rep_result")
        return cls(
            bug_id=str(data["bug_id"]),
            patch_id=str(data["patch_id"]),
            tier=data["tier"],
            pre_post_spec=PrePostSpec.from_dict(data["pre_post_spec"]),
            witness_bank=WitnessBank.from_dict(data["witness_bank"]),
            soft_spec_candidates=[
                SoftSpecCandidate.from_dict(item) for item in data.get("soft_spec_candidates", [])
            ],
            cross_rep_result=CrossRepResult.from_dict(raw_cr) if raw_cr else None,
            junit_source=str(data.get("junit_source", "")),
            anchor_probes=[
                AnchorProbeSpec.from_dict(item) for item in data.get("anchor_probes", [])
            ],
            refinement_history=[dict(r) for r in data.get("refinement_history", [])],
            status=str(data.get("status", "ok")),
        )


def _coerce_str_list(raw: Any) -> list[str]:
    if not isinstance(raw, list):
        return []
    return [str(item).strip() for item in raw if str(item).strip()]


def _parse_spec_and_seeds(
    payload: dict[str, Any],
) -> tuple[PrePostSpec, list[dict[str, Any]], str, list[AnchorProbeSpec], list[str]]:
    spec = PrePostSpec(
        preconditions=_coerce_str_list(payload.get("preconditions")),
        postconditions=_coerce_str_list(payload.get("postconditions")),
        invariants=_coerce_str_list(payload.get("invariants")),
        exception_contracts=_coerce_str_list(payload.get("exception_contracts")),
        anchor_infection_conditions=_coerce_str_list(payload.get("anchor_infection_conditions")),
    )
    raw_seeds = payload.get("seed_inputs", [])
    seeds: list[dict[str, Any]] = []
    if isinstance(raw_seeds, list):
        for idx, item in enumerate(raw_seeds):
            if not isinstance(item, dict):
                continue
            args = item.get("args", [])
            if not isinstance(args, list):
                args = [args]
            entry = dict(item)
            entry["case_id"] = str(item.get("case_id") or f"seed_{idx + 1}")
            entry["args"] = normalize_json_safe_value(args)
            entry.setdefault("source", "pbt_agent_seed")
            seeds.append(entry)

    receiver_setup = str(payload.get("receiver_setup_java") or "").strip()

    probes: list[AnchorProbeSpec] = []
    probe_errors: list[str] = []
    raw_probes = payload.get("anchor_probes", [])
    if isinstance(raw_probes, list):
        for item in raw_probes:
            if not isinstance(item, dict):
                continue
            try:
                probes.append(AnchorProbeSpec.from_dict(item))
            except (SandboxError, KeyError, TypeError, ValueError) as exc:
                probe_errors.append(f"Rejected invalid anchor probe {item!r}: {exc}")

    return spec, seeds, receiver_setup, probes, probe_errors


def _merge_candidate_inputs(
    primary_seeds: list[dict[str, Any]],
    cross_rep_inputs: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    merged: list[dict[str, Any]] = []
    seen_args: set[str] = set()
    seen_ids: set[str] = set()

    for source_list in (primary_seeds, cross_rep_inputs):
        for item in source_list:
            args = item.get("args", [])
            if not isinstance(args, list):
                args = [args]
            key = json.dumps(args, sort_keys=True, ensure_ascii=False)
            if key in seen_args:
                continue
            seen_args.add(key)

            base_id = str(item.get("case_id") or f"case_{len(merged) + 1}")
            cid = base_id
            suffix = 2
            while cid in seen_ids:
                cid = f"{base_id}_{suffix}"
                suffix += 1
            seen_ids.add(cid)

            entry = dict(item)
            entry["case_id"] = cid
            entry["args"] = args
            merged.append(entry)
    return merged


_EXCEPTION_CLASS_RE = re.compile(
    r"\b(?:[a-z_][A-Za-z0-9_]*\.)+[A-Z][A-Za-z0-9_$]*(?:Exception|Error|Failure)\b|"
    r"\b[A-Z][A-Za-z0-9_$]*(?:Exception|Error|Failure)\b"
)


def _expected_failure_signatures(intent_pack: IntentContextPack) -> tuple[bool, list[str]]:
    """Inspect all lines of `failing_tests` (including `d4j_fallback`) to extract failure signatures."""
    exc_names: list[str] = []
    seen: set[str] = set()
    has_assertion_failure = False
    for test in intent_pack.failing_tests:
        msg = test.failure_message or ""
        trace = test.stack_trace or ""
        combined = f"{msg}\n{trace}"
        if (
            "AssertionError" in combined
            or "AssertionFailedError" in combined
            or "ComparisonFailure" in combined
            or "expected:" in combined.lower()
        ):
            has_assertion_failure = True
        for match in _EXCEPTION_CLASS_RE.finditer(combined):
            name = match.group(0)
            if name not in seen:
                seen.add(name)
                exc_names.append(name)
    return has_assertion_failure, exc_names


def _classify_run_into_witness_bank(
    run_result: HarnessRunResult,
    cases_by_id: dict[str, dict[str, Any]],
    intent_pack: IntentContextPack,
    explicit_excluded: set[str],
) -> WitnessBank:
    """Classify Java execution outcomes on buggy `P` into confirmed/passing/excluded witnesses."""
    confirmed: list[WitnessCase] = []
    passing: list[WitnessCase] = []
    excluded: list[WitnessCase] = []
    _, trigger_exc_names = _expected_failure_signatures(intent_pack)

    for rec in run_result.passed_cases:
        cid = str(rec.get("case_id") or "")
        meta = cases_by_id.get(cid, {})
        observed = str(rec.get("observed", ""))
        case_obj = WitnessCase(
            case_id=cid or f"pass_{len(passing) + 1}",
            inputs={"args": meta.get("args", [])},
            expected_outcome=str(meta.get("expected_behavior") or "valid boundary behavior"),
            observed_buggy_outcome=observed or "passed",
            source=str(meta.get("source") or "pbt_harness"),
            metadata={k: v for k, v in meta.items() if k != "args"},
        )
        if cid in explicit_excluded:
            excluded.append(case_obj)
        else:
            passing.append(case_obj)

    for rec in run_result.failed_cases:
        cid = str(rec.get("case_id") or "")
        meta = cases_by_id.get(cid, {})
        msg = str(rec.get("message") or rec.get("observed") or "assertion failed")
        case_obj = WitnessCase(
            case_id=cid or f"fail_{len(confirmed) + 1}",
            inputs={"args": meta.get("args", [])},
            expected_outcome=str(meta.get("expected_behavior") or "postcondition satisfied"),
            observed_buggy_outcome=f"failed: {msg}",
            source=str(meta.get("source") or "pbt_harness"),
            metadata={k: v for k, v in meta.items() if k != "args"},
        )
        if cid in explicit_excluded:
            excluded.append(case_obj)
        else:
            confirmed.append(case_obj)

    for rec in run_result.error_cases:
        cid = str(rec.get("case_id") or "")
        meta = cases_by_id.get(cid, {})
        exc_type = str(rec.get("exception_type") or "")
        msg = str(rec.get("message") or rec.get("error") or "runtime error")
        category = str(meta.get("category") or "")

        matches_trigger_exc = any(
            exc_type == trig or exc_type.endswith("." + trig) or trig.endswith("." + exc_type)
            for trig in trigger_exc_names
            if exc_type and trig
        )
        is_irrelevant_noise = (
            cid in explicit_excluded
            or (
                any(exc_type.endswith(hint) for hint in _IRRELEVANT_INPUT_HINTS)
                and not matches_trigger_exc
                and category != "bug_trigger"
            )
        )
        case_obj = WitnessCase(
            case_id=cid or f"err_{len(confirmed) + len(excluded) + 1}",
            inputs={"args": meta.get("args", [])},
            expected_outcome=str(meta.get("expected_behavior") or "no unexpected exception"),
            observed_buggy_outcome=f"{exc_type}: {msg}".strip(": "),
            source=str(meta.get("source") or "pbt_harness"),
            metadata={k: v for k, v in meta.items() if k != "args"},
        )
        if is_irrelevant_noise:
            excluded.append(case_obj)
        elif matches_trigger_exc or category == "bug_trigger":
            confirmed.append(case_obj)
        else:
            # Conservative default on unverified errors: classify as excluded until refiner confirms.
            excluded.append(case_obj)

    return WitnessBank(
        confirmed_bug_witnesses=confirmed,
        boundary_passing_witnesses=passing,
        excluded_irrelevant_cases=excluded,
        infection_probe_traces=[dict(t) for t in run_result.infection_probe_traces],
    )


class PbtSpecAgent:
    """Agent 2: extracts Pre/Post specs, runs Cross-Rep & Java sandbox refinement, builds `WitnessBank`."""

    def __init__(
        self,
        llm_client: LLMClient,
        settings: Settings,
        *,
        registry: StrategyRegistry | None = None,
        junit_injector: JUnitInjector | None = None,
    ) -> None:
        self.llm_client = llm_client
        self.settings = settings
        self.registry = registry or StrategyRegistry.default(llm_client)
        self.junit_injector = junit_injector

    def _assert_sandbox_patch_blind(self, dispatcher: ToolDispatcher | None) -> None:
        _assert_dispatcher_patch_blind(dispatcher)
        if self.junit_injector is not None:
            backend_state = getattr(self.junit_injector.backend, "state", "prepatch")
            if backend_state != "prepatch":
                raise SandboxError(
                    f"Patch-Blind violation: JUnitInjector backend must be in 'prepatch' state, got {backend_state!r}"
                )

    def _extract_initial_spec(
        self,
        intent_pack: IntentContextPack,
        *,
        dispatcher: ToolDispatcher | None = None,
    ) -> tuple[PrePostSpec, list[dict[str, Any]], str, list[AnchorProbeSpec], list[str], bool, list[dict[str, Any]]]:
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": SPEC_EXTRACTION_SYSTEM_PROMPT},
            {"role": "user", "content": build_spec_extraction_prompt(intent_pack)},
        ]
        tool_records: list[dict[str, Any]] = []
        if dispatcher is not None:
            loop_res = self.llm_client.run_tool_loop(
                messages,
                dispatcher,
                role="pbt_agent",
                finalize_prompt=(
                    "Tool budget reached. Do NOT call any more tools. Return ONLY the single ```json "
                    'code block with keys "preconditions", "postconditions", "invariants", '
                    '"exception_contracts", "anchor_infection_conditions", "seed_inputs", '
                    '"receiver_setup_java", and "anchor_probes".'
                ),
            )
            raw_content = loop_res.final_response.content
            messages = loop_res.messages
            tool_records = loop_res.tool_records
        else:
            resp = self.llm_client.chat(messages, role="pbt_agent", tools=None)
            raw_content = resp.content

        try:
            parsed = extract_json_object(raw_content)
        except LLMParseError:
            repair_msgs = [
                *messages,
                {"role": "assistant", "content": raw_content},
                {
                    "role": "user",
                    "content": (
                        "Previous response was not valid JSON. Return ONLY one ```json code block "
                        'with keys "preconditions", "postconditions", "invariants", '
                        '"exception_contracts", "anchor_infection_conditions", "seed_inputs", '
                        '"receiver_setup_java", and "anchor_probes".'
                    ),
                },
            ]
            repaired = self.llm_client.chat(repair_msgs, role="pbt_agent", tools=None)
            try:
                parsed = extract_json_object(repaired.content)
            except LLMParseError:
                return (
                    PrePostSpec(),
                    [],
                    "",
                    [],
                    ["Failed to parse initial PrePostSpec JSON"],
                    True,
                    tool_records,
                )

        spec, seeds, receiver_setup, probes, probe_errors = _parse_spec_and_seeds(parsed)
        return spec, seeds, receiver_setup, probes, probe_errors, False, tool_records

    def _run_with_syntax_fix(
        self,
        spec: InjectedTestSpec,
        layout: D4jProjectLayout,
        *,
        tier: TierType,
        probes: list[AnchorProbeSpec],
        max_syntax_rounds: int,
    ) -> tuple[HarnessRunResult, InjectedTestSpec, list[dict[str, Any]]]:
        assert self.junit_injector is not None
        current_spec = spec
        syntax_history: list[dict[str, Any]] = []
        last_run_res: HarnessRunResult | None = None
        fix_messages: list[dict[str, Any]] = []

        for attempt in range(1, max_syntax_rounds + 1):
            run_res = self.junit_injector.run_injected_test(
                current_spec,
                layout,
                tier=tier,
                probes=probes or None,
                compile_first=True,
                always_cleanup=True,
                execution_mode="auto",
            )
            last_run_res = run_res
            if run_res.compiled:
                syntax_history.append({"attempt": attempt, "compiled": True})
                return run_res, current_spec, syntax_history

            err_msg = ""
            if run_res.error_cases:
                err_msg = str(run_res.error_cases[0].get("error", ""))
            if not err_msg:
                err_msg = run_res.raw_stderr or run_res.raw_stdout or "Unknown compilation failure"
            syntax_history.append({"attempt": attempt, "compiled": False, "error": err_msg[:400]})

            if attempt >= max_syntax_rounds:
                return run_res, current_spec, syntax_history

            fix_messages.append(
                {
                    "role": "user",
                    "content": build_junit_syntax_fix_prompt(current_spec.java_source, err_msg),
                }
            )

            # Retry LLM repair if output lacks a valid class declaration without re-compiling unchanged bad source.
            updated = False
            remaining_llm_tries = max(1, max_syntax_rounds - attempt)
            for _ in range(remaining_llm_tries):
                fix_resp = self.llm_client.chat(fix_messages, role="pbt_agent", tools=None)
                fix_messages.append({"role": "assistant", "content": fix_resp.content})
                java_blocks = extract_code_blocks(fix_resp.content, language="java") or extract_code_blocks(fix_resp.content)
                if not java_blocks:
                    syntax_history.append({"attempt": attempt, "syntax_fix_rejected": "No ```java block returned"})
                    fix_messages.append(
                        {
                            "role": "user",
                            "content": f"No ```java code block found. Return a valid class `{current_spec.class_name}`.",
                        }
                    )
                    continue
                fixed_src = max(java_blocks, key=len)
                try:
                    candidate_spec = InjectedTestSpec(
                        package_name=current_spec.package_name,
                        class_name=current_spec.class_name,
                        java_source=fixed_src,
                        test_methods=list(current_spec.test_methods),
                    )
                except SandboxError as exc:
                    syntax_history.append({"attempt": attempt, "syntax_fix_rejected": str(exc)})
                    fix_messages.append(
                        {
                            "role": "user",
                            "content": f"Invalid Java test class ({exc}). Keep class name `{current_spec.class_name}`.",
                        }
                    )
                    continue
                if candidate_spec.java_source == current_spec.java_source:
                    syntax_history.append({"attempt": attempt, "syntax_fix_rejected": "Source unchanged"})
                    break
                current_spec = candidate_spec
                updated = True
                break

            if not updated:
                return last_run_res, current_spec, syntax_history

        assert last_run_res is not None  # pragma: no cover
        return last_run_res, current_spec, syntax_history

    def generate_and_refine(
        self,
        intent_pack: IntentContextPack,
        *,
        layout: D4jProjectLayout | None = None,
        dispatcher: ToolDispatcher | None = None,
        persist: bool = False,
        cache_dir: Path | None = None,
    ) -> SpecRefinementResult:
        """Run Spec extraction + Cross-Rep + Java D4J sandbox refinement loop on buggy `P`."""
        self._assert_sandbox_patch_blind(dispatcher)

        spec, seed_inputs, receiver_setup, probes, probe_errors, spec_degraded, spec_tool_records = (
            self._extract_initial_spec(intent_pack, dispatcher=dispatcher)
        )
        if not self.settings.pipeline.get("enable_anchor_infection_probes", True):
            probes = []

        cross_rep_strategy = self.registry.get_cross_rep(intent_pack.tier)
        cross_rep_res = cross_rep_strategy.generate_candidates(intent_pack, self.settings.pipeline)

        all_candidates = _merge_candidate_inputs(seed_inputs, cross_rep_res.candidate_inputs)
        soft_candidates = list(cross_rep_res.soft_spec_candidates)
        harness = self.registry.get_harness(intent_pack.tier)

        history: list[dict[str, Any]] = []
        if spec_tool_records:
            history.append(
                {
                    "phase": "initial_spec_tools",
                    "tool_count": len(spec_tool_records),
                    "tools": [r.get("tool") for r in spec_tool_records],
                }
            )
        if probe_errors:
            history.append({"phase": "initial_probes", "warnings": probe_errors})

        if intent_pack.tier != "tier_a" or not isinstance(harness, StandaloneFunctionUnitHarness):
            result = SpecRefinementResult(
                bug_id=intent_pack.bug_id,
                patch_id=intent_pack.patch_id,
                tier=intent_pack.tier,
                pre_post_spec=spec,
                witness_bank=WitnessBank(),
                soft_spec_candidates=soft_candidates,
                cross_rep_result=cross_rep_res,
                anchor_probes=probes,
                refinement_history=history,
                status="tier_b_placeholder",
            )
            self._maybe_persist(result, persist=persist, cache_dir=cache_dir)
            return result

        if not all_candidates:
            result = SpecRefinementResult(
                bug_id=intent_pack.bug_id,
                patch_id=intent_pack.patch_id,
                tier=intent_pack.tier,
                pre_post_spec=spec,
                witness_bank=WitnessBank(),
                soft_spec_candidates=soft_candidates,
                cross_rep_result=cross_rep_res,
                anchor_probes=probes,
                refinement_history=history,
                status="degraded_no_candidates",
            )
            self._maybe_persist(result, persist=persist, cache_dir=cache_dir)
            return result

        max_refine_rounds = int(self.settings.pipeline.get("pbt_refine_max_rounds", 5))
        max_syntax_rounds = int(self.settings.pipeline.get("syntax_fix_max_rounds", 3))
        explicit_excluded: set[str] = set()
        accumulated_excluded: dict[str, WitnessCase] = {}
        witness_bank = WitnessBank()
        latest_junit_source = ""
        status = "degraded_parse" if spec_degraded else "partial"

        if self.junit_injector is None or layout is None:
            try:
                latest_junit_source = harness.build_junit_source(
                    spec, all_candidates, intent_pack, receiver_setup_java=receiver_setup
                )
            except HarnessBuildError as exc:
                history.append({"phase": "harness_build", "error": str(exc)})
                status = "degraded_harness_build"
            else:
                status = "degraded_no_sandbox"
            result = SpecRefinementResult(
                bug_id=intent_pack.bug_id,
                patch_id=intent_pack.patch_id,
                tier=intent_pack.tier,
                pre_post_spec=spec,
                witness_bank=witness_bank,
                soft_spec_candidates=soft_candidates,
                cross_rep_result=cross_rep_res,
                junit_source=latest_junit_source,
                anchor_probes=probes,
                refinement_history=history,
                status=status,
            )
            self._maybe_persist(result, persist=persist, cache_dir=cache_dir)
            return result

        for round_idx in range(1, max_refine_rounds + 1):
            active_candidates = [c for c in all_candidates if c["case_id"] not in explicit_excluded]
            if not active_candidates:
                active_candidates = list(all_candidates)
            try:
                injected_spec = harness.build_injected_test_spec(
                    spec,
                    active_candidates,
                    intent_pack,
                    receiver_setup_java=receiver_setup,
                )
            except HarnessBuildError as exc:
                history.append({"round": round_idx, "stage": "harness_build", "error": str(exc)})
                status = "degraded_harness_build"
                break

            run_res, compiled_spec, syntax_hist = self._run_with_syntax_fix(
                injected_spec,
                layout,
                tier=intent_pack.tier,
                probes=probes,
                max_syntax_rounds=max_syntax_rounds,
            )
            latest_junit_source = compiled_spec.java_source

            if not run_res.compiled:
                history.append(
                    {
                        "round": round_idx,
                        "compiled": False,
                        "syntax_history": syntax_hist,
                    }
                )
                status = "degraded_compile_failed"
                break

            cases_by_id = {c["case_id"]: c for c in all_candidates}
            round_bank = _classify_run_into_witness_bank(
                run_res, cases_by_id, intent_pack, explicit_excluded
            )
            for ex_case in round_bank.excluded_irrelevant_cases:
                accumulated_excluded[ex_case.case_id] = ex_case
            witness_bank = WitnessBank(
                confirmed_bug_witnesses=round_bank.confirmed_bug_witnesses,
                boundary_passing_witnesses=round_bank.boundary_passing_witnesses,
                excluded_irrelevant_cases=list(accumulated_excluded.values()),
                infection_probe_traces=round_bank.infection_probe_traces,
            )

            round_summary: dict[str, Any] = {
                "round": round_idx,
                "compiled": True,
                "syntax_history": syntax_hist,
                "confirmed_bug_witnesses": len(witness_bank.confirmed_bug_witnesses),
                "boundary_passing_witnesses": len(witness_bank.boundary_passing_witnesses),
                "excluded_irrelevant_cases": len(witness_bank.excluded_irrelevant_cases),
                "probe_traces": len(witness_bank.infection_probe_traces),
            }

            if round_idx >= max_refine_rounds:
                history.append({**round_summary, "converged": False, "reason": "max_rounds_reached"})
                if not spec_degraded:
                    status = "partial"
                break

            refine_resp = self.llm_client.chat(
                [
                    {
                        "role": "system",
                        "content": SPEC_EXTRACTION_SYSTEM_PROMPT,
                    },
                    {
                        "role": "user",
                        "content": build_pbt_refinement_prompt(
                            intent_pack,
                            spec,
                            run_res,
                            witness_bank,
                            round_idx,
                            max_refine_rounds,
                        ),
                    },
                ],
                role="pbt_agent",
                tools=None,
            )
            try:
                refined = extract_json_object(refine_resp.content)
            except LLMParseError as exc:
                history.append({**round_summary, "refine_parse_error": str(exc)})
                status = "degraded_refine_parse"
                break

            refined_spec, new_seeds, _, _, _ = _parse_spec_and_seeds(refined)
            if (
                refined_spec.preconditions
                or refined_spec.postconditions
                or refined_spec.invariants
                or refined_spec.exception_contracts
                or refined_spec.anchor_infection_conditions
            ):
                spec = PrePostSpec(
                    preconditions=refined_spec.preconditions or spec.preconditions,
                    postconditions=refined_spec.postconditions or spec.postconditions,
                    invariants=refined_spec.invariants or spec.invariants,
                    exception_contracts=refined_spec.exception_contracts or spec.exception_contracts,
                    anchor_infection_conditions=(
                        refined_spec.anchor_infection_conditions or spec.anchor_infection_conditions
                    ),
                )

            newly_excluded = _coerce_str_list(refined.get("excluded_case_ids"))
            explicit_excluded.update(newly_excluded)
            if newly_excluded:
                excluded_set = set(newly_excluded)
                kept_confirmed: list[WitnessCase] = []
                for case_obj in witness_bank.confirmed_bug_witnesses:
                    if case_obj.case_id in excluded_set:
                        accumulated_excluded[case_obj.case_id] = case_obj
                    else:
                        kept_confirmed.append(case_obj)
                kept_passing: list[WitnessCase] = []
                for case_obj in witness_bank.boundary_passing_witnesses:
                    if case_obj.case_id in excluded_set:
                        accumulated_excluded[case_obj.case_id] = case_obj
                    else:
                        kept_passing.append(case_obj)
                witness_bank = WitnessBank(
                    confirmed_bug_witnesses=kept_confirmed,
                    boundary_passing_witnesses=kept_passing,
                    excluded_irrelevant_cases=list(accumulated_excluded.values()),
                    infection_probe_traces=witness_bank.infection_probe_traces,
                )

            before_count = len(all_candidates)
            if new_seeds:
                all_candidates = _merge_candidate_inputs(all_candidates, new_seeds)
            added_count = len(all_candidates) - before_count

            converged = bool(refined.get("converged", False))
            if added_count > 0 and converged and round_idx < max_refine_rounds:
                converged = False
                round_summary["converged_deferred_for_new_seeds"] = True

            round_summary["converged"] = converged
            round_summary["newly_excluded"] = newly_excluded
            round_summary["new_seeds_added"] = added_count
            round_summary["confirmed_bug_witnesses"] = len(witness_bank.confirmed_bug_witnesses)
            round_summary["boundary_passing_witnesses"] = len(witness_bank.boundary_passing_witnesses)
            round_summary["excluded_irrelevant_cases"] = len(witness_bank.excluded_irrelevant_cases)
            round_summary["refinement_notes"] = str(refined.get("refinement_notes", ""))
            history.append(round_summary)

            if converged:
                status = "degraded_parse" if spec_degraded else "ok"
                break

        result = SpecRefinementResult(
            bug_id=intent_pack.bug_id,
            patch_id=intent_pack.patch_id,
            tier=intent_pack.tier,
            pre_post_spec=spec,
            witness_bank=witness_bank,
            soft_spec_candidates=soft_candidates,
            cross_rep_result=cross_rep_res,
            junit_source=latest_junit_source,
            anchor_probes=probes,
            refinement_history=history,
            status=status,
        )
        self._maybe_persist(result, persist=persist, cache_dir=cache_dir)
        return result

    def _maybe_persist(
        self,
        result: SpecRefinementResult,
        *,
        persist: bool,
        cache_dir: Path | None,
    ) -> None:
        if not persist and cache_dir is None:
            return
        target_dir = cache_dir or self.settings.spec_cache_dir
        safe_bug = result.bug_id.replace("\\", "_").replace("/", "_").replace("::", "_").replace("..", "_")
        safe_patch = result.patch_id.replace("\\", "_").replace("/", "_").replace("::", "_").replace("..", "_")
        out_path = target_dir / f"{safe_bug}__{safe_patch}__step5_spec.json"
        atomic_json(result.to_dict(), out_path)
