"""Prompt templates and Patch-Blind builders reused across Intent, Cross-Rep, PBT, and Judge agents."""

from .intent_prompts import (
    INTENT_FINALIZE_PROMPT,
    INTENT_JSON_REPAIR_PROMPT,
    INTENT_SYSTEM_PROMPT,
    build_intent_user_prompt,
)
from .pbt_prompts import (
    CROSS_REP_END_MARKER,
    CROSS_REP_START_MARKER,
    CROSS_REP_TIER_A_SYSTEM_PROMPT,
    SPEC_EXTRACTION_SYSTEM_PROMPT,
    build_cross_rep_fix_prompt,
    build_cross_rep_tier_a_prompt,
    build_junit_syntax_fix_prompt,
    build_pbt_refinement_prompt,
    build_spec_extraction_prompt,
)

__all__ = [
    "CROSS_REP_END_MARKER",
    "CROSS_REP_START_MARKER",
    "CROSS_REP_TIER_A_SYSTEM_PROMPT",
    "INTENT_FINALIZE_PROMPT",
    "INTENT_JSON_REPAIR_PROMPT",
    "INTENT_SYSTEM_PROMPT",
    "SPEC_EXTRACTION_SYSTEM_PROMPT",
    "build_cross_rep_fix_prompt",
    "build_cross_rep_tier_a_prompt",
    "build_intent_user_prompt",
    "build_junit_syntax_fix_prompt",
    "build_pbt_refinement_prompt",
    "build_spec_extraction_prompt",
]
