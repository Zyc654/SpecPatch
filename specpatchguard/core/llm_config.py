"""General LLM configuration loader, profile resolver, and provider request builder."""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .config_loader import (
    ConfigError,
    _require_bool,
    _require_env_var_name,
    _require_finite_number,
    _require_int,
    read_json,
)

_TOP_KEYS = {"default_profile", "role_overrides", "profiles"}
_PROFILE_KEYS = {
    "provider",
    "base_url_env",
    "api_key_env",
    "model_env",
    "model_name",
    "context_window",
    "max_input_tokens",
    "max_tokens",
    "max_completion_tokens",
    "temperature",
    "top_p",
    "top_k",
    "seed",
    "stop",
    "timeout_seconds",
    "max_retries",
    "retry_backoff_seconds",
    "supports_tools",
    "parallel_tool_calls",
    "tool_choice",
    "thinking",
    "extra_headers",
    "extra_body",
}
_THINKING_KEYS = {
    "enabled",
    "effort",
    "budget_tokens",
    "provider_style",
    "return_reasoning_content",
}
_PROVIDERS = {"openai_compatible", "deepseek", "anthropic_compatible", "gemini_compatible"}
_THINKING_STYLES = {"deepseek", "openai_reasoning", "anthropic", "qwen", "none"}
_THINKING_EFFORTS = {"low", "medium", "high"}
_TOOL_CHOICES = {"auto", "required", "none"}
_FORBIDDEN_HEADER_NAMES = {"authorization", "x-api-key", "api-key"}
_FORBIDDEN_EXTRA_BODY_KEYS = {"model", "messages", "tools", "tool_choice", "stream"}


@dataclass(frozen=True)
class ThinkingConfig:
    """Provider-neutral reasoning/thinking configuration."""

    enabled: bool
    effort: str
    budget_tokens: int | None
    provider_style: str
    return_reasoning_content: bool


@dataclass(frozen=True)
class LLMProfile:
    """Single validated LLM invocation profile."""

    name: str
    provider: str
    base_url_env: str
    api_key_env: str
    model_env: str
    model_name: str
    context_window: int
    max_input_tokens: int | None
    max_tokens: int | None
    max_completion_tokens: int | None
    temperature: float | None
    top_p: float | None
    top_k: int | None
    seed: int | None
    stop: tuple[str, ...]
    timeout_seconds: float
    max_retries: int
    retry_backoff_seconds: float
    supports_tools: bool
    parallel_tool_calls: bool
    tool_choice: str
    thinking: ThinkingConfig
    extra_headers: dict[str, str] = field(default_factory=dict)
    extra_body: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class LLMConfigBundle:
    """Collection of named LLM profiles with per-agent role overrides."""

    config_path: Path
    default_profile: str
    role_overrides: dict[str, str | None]
    profiles: dict[str, LLMProfile]

    def get_profile(self, name: str | None = None, role: str | None = None) -> LLMProfile:
        """Resolve effective profile by role override -> explicit name -> default_profile."""
        target_name: str | None = None
        if role is not None:
            if role not in self.role_overrides:
                raise ConfigError(f"Unknown LLM role override key: {role}")
            target_name = self.role_overrides[role]
        if target_name is None:
            target_name = name if name is not None else self.default_profile
        if target_name not in self.profiles:
            raise ConfigError(f"Unknown LLM profile: {target_name}")
        return self.profiles[target_name]


def _parse_thinking(profile_name: str, raw: Any) -> ThinkingConfig:
    if not isinstance(raw, dict) or set(raw) != _THINKING_KEYS:
        raise ConfigError(f"profiles.{profile_name}.thinking must contain exact keys {sorted(_THINKING_KEYS)}")
    enabled = _require_bool(f"profiles.{profile_name}.thinking.enabled", raw["enabled"])
    effort = raw["effort"]
    if not isinstance(effort, str) or effort not in _THINKING_EFFORTS:
        raise ConfigError(f"profiles.{profile_name}.thinking.effort must be one of {sorted(_THINKING_EFFORTS)}")
    provider_style = raw["provider_style"]
    if not isinstance(provider_style, str) or provider_style not in _THINKING_STYLES:
        raise ConfigError(
            f"profiles.{profile_name}.thinking.provider_style must be one of {sorted(_THINKING_STYLES)}"
        )
    budget = raw["budget_tokens"]
    if budget is not None:
        budget = _require_int(f"profiles.{profile_name}.thinking.budget_tokens", budget, 1)
    if enabled and provider_style == "anthropic":
        if budget is None or budget < 1024:
            raise ConfigError(
                f"profiles.{profile_name}.thinking.budget_tokens must be >= 1024 for enabled anthropic thinking"
            )
    return_reasoning = _require_bool(
        f"profiles.{profile_name}.thinking.return_reasoning_content", raw["return_reasoning_content"]
    )
    return ThinkingConfig(
        enabled=enabled,
        effort=effort,
        budget_tokens=budget,
        provider_style=provider_style,
        return_reasoning_content=return_reasoning,
    )


def _parse_profile(name: str, raw: Any) -> LLMProfile:
    if not isinstance(raw, dict) or set(raw) != _PROFILE_KEYS:
        raise ConfigError(f"profiles.{name} must contain exact keys {sorted(_PROFILE_KEYS)}")

    provider = raw["provider"]
    if not isinstance(provider, str) or provider not in _PROVIDERS:
        raise ConfigError(f"profiles.{name}.provider must be one of {sorted(_PROVIDERS)}")

    model_name = raw["model_name"]
    if not isinstance(model_name, str) or "\n" in model_name or "\r" in model_name:
        raise ConfigError(f"profiles.{name}.model_name must be a single-line string")

    context_window = _require_int(f"profiles.{name}.context_window", raw["context_window"], 1024)
    max_input_tokens = raw["max_input_tokens"]
    if max_input_tokens is not None:
        max_input_tokens = _require_int(f"profiles.{name}.max_input_tokens", max_input_tokens, 1)
        if max_input_tokens >= context_window:
            raise ConfigError(
                f"profiles.{name}.max_input_tokens ({max_input_tokens}) must be < context_window ({context_window})"
            )

    max_tokens = raw["max_tokens"]
    if max_tokens is not None:
        max_tokens = _require_int(f"profiles.{name}.max_tokens", max_tokens, 1)
        if max_tokens > context_window:
            raise ConfigError(
                f"profiles.{name}.max_tokens ({max_tokens}) cannot exceed context_window ({context_window})"
            )
    max_completion_tokens = raw["max_completion_tokens"]
    if max_completion_tokens is not None:
        max_completion_tokens = _require_int(
            f"profiles.{name}.max_completion_tokens", max_completion_tokens, 1
        )
        if max_completion_tokens > context_window:
            raise ConfigError(
                f"profiles.{name}.max_completion_tokens ({max_completion_tokens}) cannot exceed context_window ({context_window})"
            )
    if max_tokens is None and max_completion_tokens is None:
        raise ConfigError(
            f"profiles.{name} must define at least one of max_tokens or max_completion_tokens"
        )

    temperature = raw["temperature"]
    if temperature is not None:
        temperature = _require_finite_number(f"profiles.{name}.temperature", temperature)
        if not 0.0 <= temperature <= 2.0:
            raise ConfigError(f"profiles.{name}.temperature must be in [0, 2]")

    top_p = raw["top_p"]
    if top_p is not None:
        top_p = _require_finite_number(f"profiles.{name}.top_p", top_p)
        if not 0.0 < top_p <= 1.0:
            raise ConfigError(f"profiles.{name}.top_p must be in (0, 1]")

    top_k = raw["top_k"]
    if top_k is not None:
        top_k = _require_int(f"profiles.{name}.top_k", top_k, 1)

    seed = raw["seed"]
    if seed is not None:
        seed = _require_int(f"profiles.{name}.seed", seed, 0)

    stop_raw = raw["stop"]
    if not isinstance(stop_raw, list) or not all(isinstance(s, str) for s in stop_raw):
        raise ConfigError(f"profiles.{name}.stop must be a list of strings")

    timeout_seconds = _require_finite_number(f"profiles.{name}.timeout_seconds", raw["timeout_seconds"])
    if timeout_seconds <= 0.0:
        raise ConfigError(f"profiles.{name}.timeout_seconds must be > 0")
    retry_backoff = _require_finite_number(
        f"profiles.{name}.retry_backoff_seconds", raw["retry_backoff_seconds"]
    )
    if retry_backoff < 0.0:
        raise ConfigError(f"profiles.{name}.retry_backoff_seconds must be >= 0")

    tool_choice = raw["tool_choice"]
    if not isinstance(tool_choice, str) or tool_choice not in _TOOL_CHOICES:
        raise ConfigError(f"profiles.{name}.tool_choice must be one of {sorted(_TOOL_CHOICES)}")

    thinking = _parse_thinking(name, raw["thinking"])
    if thinking.enabled and thinking.provider_style == "anthropic":
        if (
            max_tokens is not None
            and thinking.budget_tokens is not None
            and thinking.budget_tokens >= max_tokens
        ):
            raise ConfigError(
                f"profiles.{name}.thinking.budget_tokens ({thinking.budget_tokens}) must be < max_tokens ({max_tokens})"
            )
        if temperature is not None and temperature != 1.0:
            raise ConfigError(
                f"profiles.{name}.temperature must be 1.0 or null when anthropic thinking is enabled"
            )
        if top_p is not None:
            raise ConfigError(
                f"profiles.{name}.top_p must be null when anthropic thinking is enabled"
            )

    extra_headers = raw["extra_headers"]
    if not isinstance(extra_headers, dict) or not all(
        isinstance(k, str) and isinstance(v, str) for k, v in extra_headers.items()
    ):
        raise ConfigError(f"profiles.{name}.extra_headers must be a string-to-string JSON object")
    for header_key in extra_headers:
        if header_key.strip().lower() in _FORBIDDEN_HEADER_NAMES:
            raise ConfigError(
                f"profiles.{name}.extra_headers cannot embed credentials header '{header_key}'"
            )

    extra_body = raw["extra_body"]
    if not isinstance(extra_body, dict):
        raise ConfigError(f"profiles.{name}.extra_body must be a JSON object")
    if set(extra_body) & _FORBIDDEN_EXTRA_BODY_KEYS:
        raise ConfigError(
            f"profiles.{name}.extra_body cannot override model, messages, tools, tool_choice, or stream"
        )

    return LLMProfile(
        name=name,
        provider=provider,
        base_url_env=_require_env_var_name(f"profiles.{name}.base_url_env", raw["base_url_env"]),
        api_key_env=_require_env_var_name(f"profiles.{name}.api_key_env", raw["api_key_env"]),
        model_env=_require_env_var_name(f"profiles.{name}.model_env", raw["model_env"]),
        model_name=model_name.strip(),
        context_window=context_window,
        max_input_tokens=max_input_tokens,
        max_tokens=max_tokens,
        max_completion_tokens=max_completion_tokens,
        temperature=temperature,
        top_p=top_p,
        top_k=top_k,
        seed=seed,
        stop=tuple(stop_raw),
        timeout_seconds=timeout_seconds,
        max_retries=_require_int(f"profiles.{name}.max_retries", raw["max_retries"], 0),
        retry_backoff_seconds=retry_backoff,
        supports_tools=_require_bool(f"profiles.{name}.supports_tools", raw["supports_tools"]),
        parallel_tool_calls=_require_bool(
            f"profiles.{name}.parallel_tool_calls", raw["parallel_tool_calls"]
        ),
        tool_choice=tool_choice,
        thinking=thinking,
        extra_headers=dict(extra_headers),
        extra_body=copy.deepcopy(extra_body),
    )


def load_llm_config(path: str | Path) -> LLMConfigBundle:
    """Load and validate `config/llm_config.json`."""
    # [SERVER_CONFIG_REQUIRED]: 确认服务器 config/llm_config.json 中的默认 profile 与思考模式配置匹配目标模型服务
    config_file = Path(path).resolve()
    if not config_file.is_file():
        raise ConfigError(f"LLM configuration file not found: {config_file}")

    raw = read_json(config_file)
    if not isinstance(raw, dict) or set(raw) != _TOP_KEYS:
        raise ConfigError(f"LLM config must contain exact top-level keys {sorted(_TOP_KEYS)}")

    default_profile = raw["default_profile"]
    if not isinstance(default_profile, str) or not default_profile.strip():
        raise ConfigError("default_profile must be a non-empty string")

    raw_profiles = raw["profiles"]
    if not isinstance(raw_profiles, dict) or not raw_profiles:
        raise ConfigError("profiles must be a non-empty JSON object")

    profiles = {
        name: _parse_profile(name, spec)
        for name, spec in raw_profiles.items()
    }
    if default_profile not in profiles:
        raise ConfigError(f"default_profile '{default_profile}' is not defined in profiles")

    raw_overrides = raw["role_overrides"]
    if not isinstance(raw_overrides, dict):
        raise ConfigError("role_overrides must be a JSON object")
    role_overrides: dict[str, str | None] = {}
    for role, target in raw_overrides.items():
        if not isinstance(role, str) or not role.strip():
            raise ConfigError("role_overrides keys must be non-empty strings")
        if target is not None:
            if not isinstance(target, str) or target not in profiles:
                raise ConfigError(f"role_overrides.{role} points to unknown profile: {target}")
        role_overrides[role] = target

    return LLMConfigBundle(
        config_path=config_file,
        default_profile=default_profile,
        role_overrides=role_overrides,
        profiles=profiles,
    )


def build_chat_request_kwargs(
    profile: LLMProfile,
    model: str,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Build provider-aware `chat.completions.create` kwargs without mutating the profile."""
    effective_model = (model or profile.model_name).strip()
    if not effective_model:
        raise ConfigError(f"Resolved model name is empty for profile '{profile.name}'")

    kwargs: dict[str, Any] = {
        "model": effective_model,
        "messages": messages,
    }
    if profile.max_tokens is not None:
        kwargs["max_tokens"] = profile.max_tokens
    if profile.max_completion_tokens is not None:
        kwargs["max_completion_tokens"] = profile.max_completion_tokens
    if profile.temperature is not None:
        kwargs["temperature"] = profile.temperature
    if profile.top_p is not None:
        kwargs["top_p"] = profile.top_p
    if profile.seed is not None:
        kwargs["seed"] = profile.seed
    if profile.stop:
        kwargs["stop"] = list(profile.stop)

    extra_body = copy.deepcopy(profile.extra_body)
    if profile.top_k is not None:
        extra_body["top_k"] = profile.top_k
    style = profile.thinking.provider_style
    if style == "deepseek":
        extra_body["thinking"] = {"type": "enabled" if profile.thinking.enabled else "disabled"}
    elif style == "openai_reasoning":
        if profile.thinking.enabled:
            kwargs["reasoning_effort"] = profile.thinking.effort
    elif style == "anthropic":
        if profile.thinking.enabled:
            extra_body["thinking"] = {
                "type": "enabled",
                "budget_tokens": profile.thinking.budget_tokens,
            }
        else:
            extra_body["thinking"] = {"type": "disabled"}
    elif style == "qwen":
        extra_body["enable_thinking"] = profile.thinking.enabled
        if profile.thinking.enabled and profile.thinking.budget_tokens is not None:
            extra_body["thinking_budget"] = profile.thinking.budget_tokens

    if extra_body:
        kwargs["extra_body"] = extra_body
    if profile.extra_headers:
        kwargs["extra_headers"] = dict(profile.extra_headers)

    if tools:
        if not profile.supports_tools:
            raise ConfigError(f"Profile '{profile.name}' has supports_tools=False")
        kwargs["tools"] = tools
        kwargs["tool_choice"] = profile.tool_choice
        kwargs["parallel_tool_calls"] = profile.parallel_tool_calls

    return kwargs
