"""Strategy protocols, shared execution-result parser, Tier-B placeholders, and registry."""

from __future__ import annotations

from typing import Any

from ..data.schemas import (
    CrossRepResult,
    CrossRepStrategy,
    HarnessRunResult,
    IntentContextPack,
    PrePostSpec,
    TestHarnessStrategy,
    TierClassifier,
    TierType,
)
from ..sandbox.d4j_runner import parse_probe_traces
from ..sandbox.junit_injector import parse_case_records


def parse_harness_raw_result(raw_result: dict[str, Any], tier: TierType) -> HarnessRunResult:
    """Normalize either a `HarnessRunResult.to_dict()` payload or raw stdout/stderr logs."""
    if not isinstance(raw_result, dict):
        raise ValueError("raw_result must be a dictionary")

    if any(k in raw_result for k in ("passed_cases", "failed_cases", "error_cases")):
        return HarnessRunResult(
            tier=raw_result.get("tier", tier),
            compiled=bool(raw_result.get("compiled", False)),
            passed_cases=[dict(x) for x in raw_result.get("passed_cases", [])],
            failed_cases=[dict(x) for x in raw_result.get("failed_cases", [])],
            error_cases=[dict(x) for x in raw_result.get("error_cases", [])],
            infection_probe_traces=[dict(x) for x in raw_result.get("infection_probe_traces", [])],
            raw_stdout=str(raw_result.get("raw_stdout", "")),
            raw_stderr=str(raw_result.get("raw_stderr", "")),
        )

    compiled = bool(raw_result.get("compiled", False))
    stdout = str(raw_result.get("raw_stdout", raw_result.get("stdout", "")))
    stderr = str(raw_result.get("raw_stderr", raw_result.get("stderr", "")))
    combined = f"{stdout}\n{stderr}"
    if not compiled:
        return HarnessRunResult(
            tier=tier,
            compiled=False,
            error_cases=[
                {
                    "stage": "compile",
                    "error": str(raw_result.get("error", stderr.strip() or stdout.strip() or "Compilation failed")),
                }
            ],
            raw_stdout=stdout,
            raw_stderr=stderr,
        )

    passed, failed, errors = parse_case_records(combined)
    probes = parse_probe_traces(combined)
    return HarnessRunResult(
        tier=tier,
        compiled=True,
        passed_cases=passed,
        failed_cases=failed,
        error_cases=errors,
        infection_probe_traces=probes,
        raw_stdout=stdout,
        raw_stderr=stderr,
    )


class TierBAbstractDecisionTableStrategy:
    """Phase-2 reserved Tier-B `CrossRepStrategy` placeholder (abstract decision table -> `SoftSpecCandidate`)."""

    tier: TierType = "tier_b"

    def generate_candidates(
        self, intent_pack: IntentContextPack, config: dict[str, Any]
    ) -> CrossRepResult:
        if not bool(config.get("enable_cross_rep_tier_b", False)):
            return CrossRepResult(
                tier="tier_b",
                diagnostics={
                    "status": "disabled_placeholder",
                    "reason": "pipeline.enable_cross_rep_tier_b is false; Phase-2 Tier-B decision table skipped",
                },
            )
        raise NotImplementedError(
            "TierBAbstractDecisionTableStrategy is reserved for Phase-2 (Closure/stateful defects)"
        )


class PublicEntryAnchorProbeHarness:
    """Phase-2 reserved Tier-B `TestHarnessStrategy` placeholder (trigger_test entry + anchor probes)."""

    tier: TierType = "tier_b"

    def build_junit_source(
        self,
        pre_post: PrePostSpec,
        candidate_inputs: list[dict[str, Any]],
        intent_pack: IntentContextPack,
    ) -> str:
        raise NotImplementedError(
            "PublicEntryAnchorProbeHarness is reserved for Phase-2 (Tier-B public-entry probe harness)"
        )

    def parse_execution_result(self, raw_result: dict[str, Any]) -> HarnessRunResult:
        return parse_harness_raw_result(raw_result, tier="tier_b")


class StrategyRegistry:
    """Registry mapping `TierType` (`tier_a` / `tier_b`) to `CrossRepStrategy` & `TestHarnessStrategy`."""

    def __init__(self) -> None:
        self._cross_rep: dict[TierType, CrossRepStrategy] = {}
        self._harnesses: dict[TierType, TestHarnessStrategy] = {}

    def register_cross_rep(self, strategy: CrossRepStrategy) -> None:
        if not isinstance(strategy, CrossRepStrategy):
            raise TypeError(f"Expected CrossRepStrategy protocol instance, got {type(strategy).__name__}")
        self._cross_rep[strategy.tier] = strategy

    def register_harness(self, harness: TestHarnessStrategy) -> None:
        if not isinstance(harness, TestHarnessStrategy):
            raise TypeError(f"Expected TestHarnessStrategy protocol instance, got {type(harness).__name__}")
        self._harnesses[harness.tier] = harness

    def get_cross_rep(self, tier: TierType) -> CrossRepStrategy:
        if tier not in self._cross_rep:
            raise KeyError(f"No CrossRepStrategy registered for tier {tier!r}")
        return self._cross_rep[tier]

    def get_harness(self, tier: TierType) -> TestHarnessStrategy:
        if tier not in self._harnesses:
            raise KeyError(f"No TestHarnessStrategy registered for tier {tier!r}")
        return self._harnesses[tier]

    @classmethod
    def default(cls, llm_client: Any, **cross_rep_kwargs: Any) -> StrategyRegistry:
        """Create default registry with Tier-A active implementations and Tier-B reserved placeholders."""
        from .tier_a_cross_rep import TierAPythonHypothesisStrategy
        from .tier_a_harness import StandaloneFunctionUnitHarness

        registry = cls()
        registry.register_cross_rep(TierAPythonHypothesisStrategy(llm_client, **cross_rep_kwargs))
        registry.register_cross_rep(TierBAbstractDecisionTableStrategy())
        registry.register_harness(StandaloneFunctionUnitHarness())
        registry.register_harness(PublicEntryAnchorProbeHarness())
        return registry
