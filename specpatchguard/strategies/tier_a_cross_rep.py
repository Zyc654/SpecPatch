"""Tier-A clean-room Python + Hypothesis cross-representation boundary input generator."""

from __future__ import annotations

import ast
import importlib.util
import json
import math
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from typing import Any, Callable

from ..agents.llm_client import LLMClient, extract_code_blocks
from ..core.cache_utils import (
    compute_anchor_methods_hash,
    load_valid_cache,
    resolve_cache_path,
    sanitize_cache_segment,
    save_cache_atomic,
)
from ..data.schemas import CrossRepResult, IntentContextPack, TierType
from ..prompts.pbt_prompts import (
    CROSS_REP_END_MARKER,
    CROSS_REP_START_MARKER,
    CROSS_REP_TIER_A_SYSTEM_PROMPT,
    build_cross_rep_fix_prompt,
    build_cross_rep_tier_a_prompt,
)

_ALLOWED_IMPORTS = frozenset(
    {
        "collections",
        "dataclasses",
        "datetime",
        "decimal",
        "fractions",
        "functools",
        "hypothesis",
        "itertools",
        "json",
        "math",
        "random",
        "re",
        "string",
        "typing",
    }
)
_FORBIDDEN_NAMES = frozenset(
    {
        "__import__",
        "breakpoint",
        "compile",
        "delattr",
        "eval",
        "exec",
        "getattr",
        "globals",
        "input",
        "locals",
        "memoryview",
        "open",
        "setattr",
        "type",
        "vars",
    }
)
_SAFE_DUNDERS = frozenset(
    {
        "__abs__",
        "__add__",
        "__bool__",
        "__eq__",
        "__floordiv__",
        "__ge__",
        "__gt__",
        "__hash__",
        "__init__",
        "__iter__",
        "__le__",
        "__len__",
        "__lt__",
        "__mod__",
        "__mul__",
        "__name__",
        "__ne__",
        "__next__",
        "__repr__",
        "__str__",
        "__sub__",
        "__truediv__",
    }
)
_SAFE_ENV_ALLOWLIST = frozenset(
    {
        "CONDA_PREFIX",
        "HOME",
        "LANG",
        "LC_ALL",
        "PATH",
        "PYTHONPATH",
        "PYTHONUTF8",
        "SYSTEMROOT",
        "TEMP",
        "TMP",
        "TMPDIR",
        "USERPROFILE",
        "VIRTUAL_ENV",
        "WINDIR",
    }
)
_SECRET_ENV_SUBSTRINGS = ("KEY", "TOKEN", "SECRET", "PASS", "AUTH", "CREDENTIAL")


def validate_clean_room_python(code: str) -> list[str]:
    """Statically verify generated clean-room Python avoids unsafe I/O, imports, or reflection."""
    violations: list[str] = []
    if not isinstance(code, str) or not code.strip():
        return ["Generated Python script is empty"]
    try:
        tree = ast.parse(code)
    except SyntaxError as exc:
        return [f"SyntaxError at line {exc.lineno}: {exc.msg}"]

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                top = alias.name.split(".", 1)[0]
                if top not in _ALLOWED_IMPORTS:
                    violations.append(f"Disallowed import '{alias.name}'")
        elif isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            top = mod.split(".", 1)[0]
            if top not in _ALLOWED_IMPORTS:
                violations.append(f"Disallowed from-import '{mod}'")
        elif isinstance(node, ast.Name):
            if node.id in _FORBIDDEN_NAMES:
                violations.append(f"Forbidden builtin identifier '{node.id}'")
        elif isinstance(node, ast.Attribute):
            if (
                node.attr.startswith("__")
                and node.attr.endswith("__")
                and node.attr not in _SAFE_DUNDERS
            ):
                violations.append(f"Forbidden dunder attribute access '.{node.attr}'")
    return violations


def normalize_json_safe_value(value: Any) -> Any:
    """Convert non-finite or `-0.0` floats into canonical `{\"__spg_float__\": ...}` markers."""
    if isinstance(value, bool) or value is None or isinstance(value, (int, str)):
        return value
    if isinstance(value, float):
        if math.isnan(value):
            return {"__spg_float__": "NaN"}
        if math.isinf(value):
            return {"__spg_float__": "Infinity" if value > 0 else "-Infinity"}
        if value == 0.0 and math.copysign(1.0, value) < 0:
            return {"__spg_float__": "-0.0"}
        return value
    if isinstance(value, list):
        return [normalize_json_safe_value(item) for item in value]
    if isinstance(value, tuple):
        return [normalize_json_safe_value(item) for item in value]
    if isinstance(value, dict):
        return {str(k): normalize_json_safe_value(v) for k, v in value.items()}
    return str(value)


def _extract_marked_json_array(stdout: str) -> list[dict[str, Any]]:
    start = stdout.find(CROSS_REP_START_MARKER)
    end = stdout.find(CROSS_REP_END_MARKER, start + len(CROSS_REP_START_MARKER)) if start != -1 else -1
    if start == -1 or end == -1:
        raise ValueError(
            f"Missing output markers {CROSS_REP_START_MARKER} ... {CROSS_REP_END_MARKER}"
        )
    payload = stdout[start + len(CROSS_REP_START_MARKER) : end].strip()
    if not payload:
        raise ValueError("Empty JSON payload between cross-rep markers")
    decoded = json.loads(payload)
    if not isinstance(decoded, list):
        raise ValueError("Cross-rep JSON payload must be a JSON array")

    cleaned: list[dict[str, Any]] = []
    seen_args: set[str] = set()
    for idx, item in enumerate(decoded):
        if not isinstance(item, dict):
            continue
        raw_args = item.get("args", [])
        if not isinstance(raw_args, list):
            raw_args = [raw_args]
        norm_args = normalize_json_safe_value(raw_args)
        dedup_key = json.dumps(norm_args, sort_keys=True, ensure_ascii=False)
        if dedup_key in seen_args:
            continue
        seen_args.add(dedup_key)

        entry: dict[str, Any] = {
            "case_id": str(item.get("case_id") or f"cross_rep_{idx + 1}"),
            "args": norm_args,
            "category": str(item.get("category") or "boundary"),
            "rationale": str(item.get("rationale") or ""),
            "source": "cross_rep_tier_a",
        }
        if "ref_hint" in item:
            # Strictly non-authoritative hint; Java sandbox execution determines final oracle.
            entry["ref_hint"] = normalize_json_safe_value(item["ref_hint"])
        cleaned.append(entry)
    return cleaned


def _build_sanitized_subprocess_env() -> dict[str, str]:
    """Build a minimal environment for clean-room Python subprocesses without API secrets."""
    sanitized: dict[str, str] = {"PYTHONDONTWRITEBYTECODE": "1"}
    for key, value in os.environ.items():
        upper = key.upper()
        if any(tok in upper for tok in _SECRET_ENV_SUBSTRINGS):
            continue
        if upper in _SAFE_ENV_ALLOWLIST:
            sanitized[key] = value
    return sanitized


def _default_python_runner(script_path: Path, timeout_sec: int) -> tuple[int, str, str]:
    proc = subprocess.run(
        [sys.executable, "-B", str(script_path)],
        cwd=str(script_path.parent),
        env=_build_sanitized_subprocess_env(),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout_sec,
    )
    return proc.returncode, proc.stdout, proc.stderr


class TierAPythonHypothesisStrategy:
    """Clean-room Python + Hypothesis boundary-input explorer for Tier-A functions."""

    tier: TierType = "tier_a"

    def __init__(
        self,
        llm_client: LLMClient,
        *,
        script_runner: Callable[[Path, int], tuple[int, str, str]] | None = None,
        timeout_sec: int = 60,
        hypothesis_available: bool | None = None,
        cache_dir: Path | None = None,
    ) -> None:
        # Optional server hypothesis dependency — documented in SERVER_DEPLOY_CHECKLIST.md (SERVER_CONFIG_REQUIRED).
        self.llm_client = llm_client
        self._script_runner = script_runner or _default_python_runner
        self.timeout_sec = timeout_sec
        self.cache_dir = Path(cache_dir) if cache_dir is not None else None
        self._hypothesis_available = (
            bool(importlib.util.find_spec("hypothesis"))
            if hypothesis_available is None
            else bool(hypothesis_available)
        )

    def _cache_path(self, intent_pack: IntentContextPack, eff_cache_dir: Path) -> Path:
        safe_bug = sanitize_cache_segment(intent_pack.bug_id)
        anchor_hash = compute_anchor_methods_hash(intent_pack.anchors, intent_pack.target_methods)
        safe_tier = sanitize_cache_segment(self.tier)
        return resolve_cache_path(eff_cache_dir, "cross_rep", f"{safe_bug}__{anchor_hash}__{safe_tier}__cross_rep.json")

    def generate_candidates(
        self, intent_pack: IntentContextPack, config: dict[str, Any]
    ) -> CrossRepResult:
        if not bool(config.get("enable_cross_rep_tier_a", True)):
            return CrossRepResult(
                tier="tier_a",
                diagnostics={"status": "disabled", "hypothesis_available": self._hypothesis_available},
            )

        max_examples = int(config.get("hypothesis_max_examples", 100))
        max_rounds = max(1, int(config.get("syntax_fix_max_rounds", 3)))
        use_hypothesis = self._hypothesis_available

        raw_cache_dir = config.get("cache_dir")
        eff_cache_dir = Path(raw_cache_dir) if raw_cache_dir is not None else self.cache_dir
        eff_force_refresh = bool(config.get("force_refresh", False))
        cache_path = self._cache_path(intent_pack, eff_cache_dir) if eff_cache_dir is not None else None
        if cache_path is not None:
            cached = load_valid_cache(
                cache_path,
                force_refresh=eff_force_refresh,
                validator=lambda d: (
                    (d.get("diagnostics") or {}).get("status") == "ok"
                    and d.get("tier") == self.tier
                    and (d.get("diagnostics") or {}).get("bug_id") in (None, intent_pack.bug_id)
                    and bool((d.get("diagnostics") or {}).get("hypothesis_available", False)) == bool(use_hypothesis)
                    and int((d.get("diagnostics") or {}).get("hypothesis_max_examples", max_examples)) >= max_examples
                ),
            )
            if cached is not None:
                try:
                    return CrossRepResult.from_dict(cached)
                except (KeyError, TypeError, ValueError):
                    pass

        messages: list[dict[str, Any]] = [
            {"role": "system", "content": CROSS_REP_TIER_A_SYSTEM_PROMPT},
            {
                "role": "user",
                "content": build_cross_rep_tier_a_prompt(
                    intent_pack, max_examples=max_examples, use_hypothesis=use_hypothesis
                ),
            },
        ]

        last_code = ""
        last_error = ""
        attempts: list[dict[str, Any]] = []

        for attempt_idx in range(1, max_rounds + 1):
            resp = self.llm_client.chat(messages, role="cross_rep_agent")
            messages.append({"role": "assistant", "content": resp.content})

            py_blocks = extract_code_blocks(resp.content, language="python")
            if not py_blocks:
                py_blocks = extract_code_blocks(resp.content)
            if not py_blocks:
                last_error = "No ```python code block found in LLM response"
                attempts.append({"attempt": attempt_idx, "ok": False, "error": last_error})
                if attempt_idx < max_rounds:
                    messages.append(
                        {
                            "role": "user",
                            "content": build_cross_rep_fix_prompt(
                                last_code, last_error, use_hypothesis=use_hypothesis
                            ),
                        }
                    )
                continue

            candidate_code = max(py_blocks, key=len)
            last_code = candidate_code

            safety_issues = validate_clean_room_python(candidate_code)
            if safety_issues:
                last_error = "AST safety check failed: " + "; ".join(safety_issues)
                attempts.append({"attempt": attempt_idx, "ok": False, "error": last_error})
                if attempt_idx < max_rounds:
                    messages.append(
                        {
                            "role": "user",
                            "content": build_cross_rep_fix_prompt(
                                candidate_code, last_error, use_hypothesis=use_hypothesis
                            ),
                        }
                    )
                continue

            try:
                with tempfile.TemporaryDirectory(prefix="spg_cross_rep_") as tmp_dir:
                    script_file = Path(tmp_dir) / "clean_room_model.py"
                    script_file.write_text(candidate_code, encoding="utf-8")
                    rc, out, err = self._script_runner(script_file, self.timeout_sec)
            except subprocess.TimeoutExpired:
                last_error = f"Clean-room Python script timed out after {self.timeout_sec}s"
                attempts.append({"attempt": attempt_idx, "ok": False, "error": last_error})
                if attempt_idx < max_rounds:
                    messages.append(
                        {
                            "role": "user",
                            "content": build_cross_rep_fix_prompt(
                                candidate_code, last_error, use_hypothesis=use_hypothesis
                            ),
                        }
                    )
                continue

            if rc != 0:
                last_error = f"Python exit code {rc}:\n{err.strip() or out.strip()}"
                attempts.append({"attempt": attempt_idx, "ok": False, "error": last_error})
                if attempt_idx < max_rounds:
                    messages.append(
                        {
                            "role": "user",
                            "content": build_cross_rep_fix_prompt(
                                candidate_code, last_error, use_hypothesis=use_hypothesis
                            ),
                        }
                    )
                continue

            try:
                candidates = _extract_marked_json_array(out)
            except (ValueError, json.JSONDecodeError) as exc:
                last_error = f"Output parsing error: {exc}\nstdout:\n{out[:1200]}"
                attempts.append({"attempt": attempt_idx, "ok": False, "error": last_error})
                if attempt_idx < max_rounds:
                    messages.append(
                        {
                            "role": "user",
                            "content": build_cross_rep_fix_prompt(
                                candidate_code, last_error, use_hypothesis=use_hypothesis
                            ),
                        }
                    )
                continue

            attempts.append(
                {"attempt": attempt_idx, "ok": True, "candidate_count": len(candidates)}
            )
            result = CrossRepResult(
                tier="tier_a",
                candidate_inputs=candidates,
                python_model_code=candidate_code,
                diagnostics={
                    "status": "ok",
                    "bug_id": intent_pack.bug_id,
                    "hypothesis_available": self._hypothesis_available,
                    "hypothesis_max_examples": max_examples,
                    "attempts": attempts,
                },
            )
            if cache_path is not None:
                save_cache_atomic(cache_path, result.to_dict())
            return result

        return CrossRepResult(
            tier="tier_a",
            candidate_inputs=[],
            python_model_code=last_code,
            diagnostics={
                "status": "degraded_failed",
                "hypothesis_available": self._hypothesis_available,
                "last_error": last_error,
                "attempts": attempts,
            },
        )
