"""Agent 1 (Intent & Context Explorer): Patch-Blind intent synthesis using Docker/LSP tools."""

from __future__ import annotations

from dataclasses import replace
from typing import Any

from ..analysis.protocols import ToolExecutionError
from ..analysis.tool_registry import ToolDispatcher
from ..core.config_loader import Settings
from ..data.schemas import (
    BoundaryContext,
    IntentContextPack,
    PatchBlindTask,
    TierClassifier,
    TierDecision,
    TierType,
)
from ..data.tier_classifier import RuleBasedTierClassifier, build_initial_intent_pack
from ..prompts.intent_prompts import (
    INTENT_FINALIZE_PROMPT,
    INTENT_JSON_REPAIR_PROMPT,
    INTENT_SYSTEM_PROMPT,
    build_intent_user_prompt,
)
from .llm_client import LLMClient, LLMParseError, extract_json_object


def _assert_dispatcher_patch_blind(dispatcher: ToolDispatcher | None) -> None:
    if dispatcher is None:
        return
    checked: set[int] = set()
    for tool_name, handler in dispatcher._handlers.items():
        owner = getattr(handler, "__self__", None)
        if owner is None or id(owner) in checked:
            continue
        checked.add(id(owner))
        readonly = getattr(owner, "readonly_prepatch", False)
        state = getattr(getattr(owner, "executor", None), "state", None)
        if not readonly or state != "prepatch":
            raise ToolExecutionError(
                f"Patch-Blind violation on tool '{tool_name}' ({type(owner).__name__}): "
                f"readonly_prepatch={readonly!r}, state={state!r}"
            )


def _harvest_boundary_context(
    base: BoundaryContext,
    tool_records: list[dict[str, Any]],
    llm_notes: list[str],
) -> BoundaryContext:
    branches = list(base.enclosing_branches)
    class_fields = list(base.class_fields)
    callers = list(base.callers)
    overrides = list(base.overrides)
    non_local = list(base.non_local_state_touches)
    notes = list(base.notes)

    for rec in tool_records:
        tool_name = rec.get("tool")
        result = rec.get("result")
        if not isinstance(result, dict) or not result.get("ok"):
            continue
        if tool_name == "get_enclosing_branches":
            branches.append(
                {
                    "file_path": result.get("file_path", ""),
                    "line": result.get("line"),
                    "enclosing_class_visibility": result.get("enclosing_class_visibility", ""),
                    "enclosing_branches": result.get("enclosing_branches", []),
                    "preceding_guards": result.get("preceding_guards", []),
                }
            )
        elif tool_name == "get_class_members":
            for cls_item in result.get("classes", []):
                if isinstance(cls_item, dict):
                    for field_item in cls_item.get("fields", []):
                        if isinstance(field_item, dict):
                            class_fields.append(field_item)
        elif tool_name == "lsp_find_references":
            for ref in result.get("references", []):
                if isinstance(ref, dict) and not ref.get("is_test_file", False):
                    callers.append(ref)
        elif tool_name == "lsp_type_hierarchy":
            for sub in result.get("subtypes", []):
                if isinstance(sub, dict):
                    overrides.append(sub)
        elif tool_name == "trace_symbol_usage":
            for occ in result.get("occurrences", []):
                if isinstance(occ, dict):
                    non_local.append(occ)

    for note in llm_notes:
        cleaned = str(note).strip()
        if cleaned and cleaned not in notes:
            notes.append(cleaned)

    return BoundaryContext(
        enclosing_branches=branches,
        class_fields=class_fields,
        callers=callers,
        overrides=overrides,
        non_local_state_touches=non_local,
        notes=notes,
    )


def _coerce_str_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(x).strip() for x in value if str(x).strip()]


class IntentContextAgent:
    """Agent 1: explores pre-patch repository context and produces an enriched `IntentContextPack`."""

    def __init__(
        self,
        llm_client: LLMClient,
        settings: Settings,
        *,
        tier_classifier: TierClassifier | None = None,
    ) -> None:
        self.llm_client = llm_client
        self.settings = settings
        self.tier_classifier: TierClassifier = tier_classifier or RuleBasedTierClassifier()

    def _parse_with_repair(
        self,
        raw_content: str,
        messages: list[dict[str, Any]],
    ) -> tuple[dict[str, Any], bool]:
        try:
            return extract_json_object(raw_content), False
        except LLMParseError:
            repair_msgs = [
                *messages,
                {"role": "assistant", "content": raw_content},
                {"role": "user", "content": INTENT_JSON_REPAIR_PROMPT},
            ]
            repaired = self.llm_client.chat(repair_msgs, role="intent_agent", tools=None)
            try:
                return extract_json_object(repaired.content), False
            except LLMParseError:
                return {}, True

    def explore(
        self,
        task: PatchBlindTask,
        *,
        dispatcher: ToolDispatcher | None = None,
        initial_pack: IntentContextPack | None = None,
        include_lsp: bool = True,
        max_rounds: int | None = None,
    ) -> IntentContextPack:
        """Analyze a `PatchBlindTask` via optional Docker/LSP tools and return an enriched `IntentContextPack`."""
        _assert_dispatcher_patch_blind(dispatcher)

        base_pack = initial_pack or build_initial_intent_pack(task, classifier=self.tier_classifier)
        user_prompt = build_intent_user_prompt(task, base_pack, has_tools=(dispatcher is not None))
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": INTENT_SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ]

        tool_records: list[dict[str, Any]] = []
        budget_exhausted = False
        rounds_used = 1
        if dispatcher is not None:
            loop_res = self.llm_client.run_tool_loop(
                messages,
                dispatcher,
                role="intent_agent",
                max_rounds=max_rounds,
                include_lsp=include_lsp,
                finalize_prompt=INTENT_FINALIZE_PROMPT,
            )
            final_text = loop_res.final_response.content
            messages = loop_res.messages
            tool_records = loop_res.tool_records
            rounds_used = loop_res.rounds_used
            budget_exhausted = loop_res.budget_exhausted
        else:
            resp = self.llm_client.chat(messages, role="intent_agent", tools=None)
            final_text = resp.content
            messages.append({"role": "assistant", "content": final_text})

        parsed, parse_degraded = self._parse_with_repair(final_text, messages)
        summary = str(parsed.get("bug_intent_summary") or "").strip()
        if not summary:
            summary = final_text.strip() or base_pack.bug_intent_summary

        boundary_notes = _coerce_str_list(parsed.get("boundary_notes"))
        anchor_analysis = _coerce_str_list(parsed.get("anchor_analysis"))
        root_cause = str(parsed.get("root_cause_hypothesis") or "").strip()
        entry_hint = str(parsed.get("entry_harness_hint") or base_pack.entry_harness_skeleton).strip()
        rec_tier_raw = str(parsed.get("recommended_tier") or "").strip().lower()

        enriched_boundary = _harvest_boundary_context(
            base_pack.boundary_context,
            tool_records,
            boundary_notes,
        )

        candidate_pack = replace(
            base_pack,
            bug_intent_summary=summary,
            entry_harness_skeleton=entry_hint,
            boundary_context=enriched_boundary,
            extra_context={
                **base_pack.extra_context,
                "root_cause_hypothesis": root_cause,
                "anchor_analysis": anchor_analysis,
                "intent_parse_degraded": parse_degraded,
                "budget_exhausted": budget_exhausted,
                "rounds_used": rounds_used,
                "tool_trace": [
                    {"tool": r.get("tool"), "ok": bool((r.get("result") or {}).get("ok", False))}
                    for r in tool_records
                ],
            },
        )

        # Re-evaluate TierClassifier after BoundaryContext enrichment.
        target_tier_cfg = str(self.settings.pipeline.get("target_tier", "auto"))
        if hasattr(self.tier_classifier, "classify_detailed"):
            decision = self.tier_classifier.classify_detailed(candidate_pack)  # type: ignore[attr-defined]
        else:
            classified: TierType = self.tier_classifier.classify(candidate_pack)
            decision = TierDecision(
                tier=classified,
                confidence=0.8,
                reasons=["Classified via custom TierClassifier protocol"],
            )

        effective_tier: TierType = decision.tier
        if target_tier_cfg in {"tier_a", "tier_b"} and decision.tier != target_tier_cfg:
            candidate_pack.extra_context["configured_target_tier"] = target_tier_cfg
        if rec_tier_raw in {"tier_a", "tier_b"}:
            candidate_pack.extra_context["llm_recommended_tier"] = rec_tier_raw

        return replace(
            candidate_pack,
            tier=effective_tier,
            tier_decision=decision,
        )
