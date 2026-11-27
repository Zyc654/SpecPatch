"""Unit tests for general LLM configuration (`config/llm_config.json` & `llm_config.py`)."""

from __future__ import annotations

import copy
import json
from pathlib import Path
import sys

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from specpatchguard.core.config_loader import ConfigError, atomic_json  # noqa: E402
from specpatchguard.core.llm_config import (  # noqa: E402
    build_chat_request_kwargs,
    load_llm_config,
)


def test_default_llm_config_loads_all_profiles():
    bundle = load_llm_config(PROJECT_ROOT / "config" / "llm_config.json")
    assert bundle.default_profile == "deepseek_default"
    assert set(bundle.profiles) == {
        "deepseek_default",
        "deepseek_thinking",
        "openai_reasoning",
        "claude_thinking",
        "qwen_coder",
    }
    assert bundle.get_profile().name == "deepseek_default"
    assert bundle.get_profile(role="intent_agent").name == "deepseek_default"
    for prof in bundle.profiles.values():
        assert prof.timeout_seconds == 900.0
        assert prof.context_window >= 128000


def test_role_overrides_and_unknown_errors(tmp_path: Path):
    raw = json.loads((PROJECT_ROOT / "config" / "llm_config.json").read_text(encoding="utf-8"))
    raw["role_overrides"]["judge_agent"] = "deepseek_thinking"
    cfg_path = tmp_path / "llm_config.json"
    atomic_json(raw, cfg_path)

    bundle = load_llm_config(cfg_path)
    assert bundle.get_profile(role="judge_agent").name == "deepseek_thinking"
    assert bundle.get_profile(role="intent_agent").name == "deepseek_default"
    assert bundle.get_profile(name="qwen_coder").name == "qwen_coder"

    with pytest.raises(ConfigError, match="Unknown LLM role"):
        bundle.get_profile(role="missing_role")
    with pytest.raises(ConfigError, match="Unknown LLM profile"):
        bundle.get_profile(name="nonexistent")


def test_thinking_dialects_and_request_builder():
    bundle = load_llm_config(PROJECT_ROOT / "config" / "llm_config.json")
    msgs = [{"role": "user", "content": "hi"}]
    tools = [{"type": "function", "function": {"name": "grep_code", "parameters": {"type": "object", "properties": {}}}}]

    ds_off = build_chat_request_kwargs(bundle.get_profile("deepseek_default"), "deepseek-chat", msgs, tools=tools)
    assert ds_off["max_tokens"] == 8192
    assert ds_off["extra_body"] == {"thinking": {"type": "disabled"}}
    assert "stop" not in ds_off  # empty stop stripped
    assert ds_off["tools"] == tools
    assert ds_off["tool_choice"] == "auto"
    assert ds_off["parallel_tool_calls"] is False

    ds_on = build_chat_request_kwargs(bundle.get_profile("deepseek_thinking"), "deepseek-reasoner", msgs)
    assert ds_on["extra_body"] == {"thinking": {"type": "enabled"}}
    assert bundle.get_profile("deepseek_thinking").extra_body == {}  # profile not mutated

    oai = build_chat_request_kwargs(bundle.get_profile("openai_reasoning"), "o3-mini", msgs)
    assert oai["max_completion_tokens"] == 16384
    assert "max_tokens" not in oai
    assert "temperature" not in oai
    assert oai["reasoning_effort"] == "medium"

    claude = build_chat_request_kwargs(bundle.get_profile("claude_thinking"), "claude-3-7-sonnet", msgs)
    assert claude["extra_body"] == {"thinking": {"type": "enabled", "budget_tokens": 8192}}

    qwen = build_chat_request_kwargs(bundle.get_profile("qwen_coder"), "qwen3-coder", msgs)
    assert qwen["extra_body"] == {"top_k": 20, "enable_thinking": False}


def test_supports_tools_false_rejects_tools(tmp_path: Path):
    raw = json.loads((PROJECT_ROOT / "config" / "llm_config.json").read_text(encoding="utf-8"))
    raw["profiles"]["deepseek_default"]["supports_tools"] = False
    cfg_path = tmp_path / "llm_config.json"
    atomic_json(raw, cfg_path)
    bundle = load_llm_config(cfg_path)
    with pytest.raises(ConfigError, match="supports_tools=False"):
        build_chat_request_kwargs(
            bundle.get_profile(),
            "deepseek-chat",
            [{"role": "user", "content": "hi"}],
            tools=[{"type": "function"}],
        )


@pytest.mark.parametrize(
    "mutator",
    [
        lambda d: d["profiles"]["deepseek_default"].__setitem__("max_tokens", None),
        lambda d: d["profiles"]["deepseek_default"].__setitem__("context_window", 4096),
        lambda d: d["profiles"]["deepseek_default"].__setitem__("max_input_tokens", 128000),
        lambda d: d["profiles"]["deepseek_default"].__setitem__("top_k", 0),
        lambda d: d["profiles"]["claude_thinking"].__setitem__("temperature", 0.5),
        lambda d: d["profiles"]["claude_thinking"].__setitem__("top_p", 0.9),
        lambda d: d["profiles"]["claude_thinking"]["thinking"].__setitem__("budget_tokens", 16384),
        lambda d: d["profiles"]["claude_thinking"]["thinking"].__setitem__("budget_tokens", 512),
        lambda d: d["profiles"]["deepseek_default"]["extra_headers"].__setitem__("Authorization", "Bearer secret"),
        lambda d: d["profiles"]["deepseek_default"]["extra_body"].__setitem__("messages", []),
        lambda d: d["profiles"]["deepseek_default"].__setitem__("max_tokens", True),
        lambda d: d["profiles"]["deepseek_default"].__setitem__("temperature", 2.5),
        lambda d: d.__setitem__("default_profile", "missing"),
        lambda d: d["role_overrides"].__setitem__("judge_agent", "missing"),
    ],
)
def test_llm_config_boundary_rejections(tmp_path: Path, mutator):
    raw = json.loads((PROJECT_ROOT / "config" / "llm_config.json").read_text(encoding="utf-8"))
    mutated = copy.deepcopy(raw)
    mutator(mutated)
    cfg_path = tmp_path / "llm_config.json"
    atomic_json(mutated, cfg_path)
    with pytest.raises(ConfigError):
        load_llm_config(cfg_path)
