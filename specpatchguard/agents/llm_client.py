"""Unified OpenAI-compatible LLM client, tool-calling loop, and resilient response parsers."""

from __future__ import annotations

from dataclasses import dataclass, field
import json
import re
import time
from typing import Any, Callable, Protocol, runtime_checkable

from ..analysis.tool_registry import ToolDispatcher
from ..core.config_loader import ConfigError, Settings, safe_error
from ..core.llm_config import (
    LLMConfigBundle,
    LLMProfile,
    ThinkingConfig,
    build_chat_request_kwargs,
    load_llm_config,
)

try:  # pragma: no cover - optional runtime import checked cleanly at instantiation
    from openai import OpenAI as _DefaultOpenAIClient
except ImportError:  # pragma: no cover
    _DefaultOpenAIClient = None


class LLMClientError(RuntimeError):
    """Raised when an LLM request fails after configured retries or violates config."""


class LLMParseError(ValueError):
    """Raised when an LLM response cannot be parsed into expected code/tag/JSON blocks."""


@dataclass(frozen=True)
class ToolCallRequest:
    """Normalized function/tool call emitted by a chat model."""

    id: str
    name: str
    arguments: str


@dataclass
class LLMResponse:
    """Normalized completion response across OpenAI-compatible and reasoning providers."""

    content: str
    reasoning_content: str = ""
    tool_calls: list[ToolCallRequest] = field(default_factory=list)
    finish_reason: str = ""
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    model: str = ""


@dataclass
class ToolLoopResult:
    """Full trajectory and final response of a multi-round tool-calling loop."""

    final_response: LLMResponse
    messages: list[dict[str, Any]]
    tool_records: list[dict[str, Any]]
    rounds_used: int
    total_prompt_tokens: int
    total_completion_tokens: int
    budget_exhausted: bool = False


@runtime_checkable
class LLMClient(Protocol):
    """Protocol implemented by `OpenAILLMClient` and offline test mocks."""

    def chat(
        self,
        messages: list[dict[str, Any]],
        *,
        role: str | None = None,
        profile_name: str | None = None,
        tools: list[dict[str, Any]] | None = None,
    ) -> LLMResponse: ...

    def run_tool_loop(
        self,
        messages: list[dict[str, Any]],
        dispatcher: ToolDispatcher,
        *,
        role: str | None = None,
        profile_name: str | None = None,
        max_rounds: int | None = None,
        include_lsp: bool = True,
        finalize_prompt: str | None = None,
    ) -> ToolLoopResult: ...


def extract_code_blocks(text: str, language: str | None = None) -> list[str]:
    """Extract fenced markdown code blocks from `text` (preserving inner code cleanly)."""
    if not isinstance(text, str) or not text:
        return []
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    lines = normalized.splitlines()
    blocks: list[str] = []
    in_block = False
    cur_lang = ""
    buf: list[str] = []
    target_lang = language.strip().lower() if language else None

    for line in lines:
        stripped = line.strip()
        if not in_block:
            if stripped.startswith("```"):
                in_block = True
                cur_lang = stripped[3:].strip().lower()
                buf = []
        else:
            if stripped.startswith("```") and stripped == "```":
                if target_lang is None or cur_lang == target_lang or (target_lang == "python" and cur_lang in {"py", "python3"}):
                    blocks.append("\n".join(buf).strip("\n"))
                in_block = False
                cur_lang = ""
                buf = []
            else:
                buf.append(line)
    return blocks


def extract_tagged_block(text: str, tag: str) -> str | None:
    """Extract the first `<tag>...</tag>` body from `text` (case-sensitive tag match)."""
    if not isinstance(text, str) or not tag:
        return None
    pattern = re.compile(rf"<{re.escape(tag)}>\s*(.*?)\s*</{re.escape(tag)}>", re.DOTALL)
    match = pattern.search(text)
    if not match:
        return None
    return match.group(1).strip()


def _scan_balanced_json_object(text: str) -> str | None:
    """Scan for the first balanced `{...}` object while respecting JSON strings and escapes."""
    start = text.find("{")
    while start != -1:
        depth = 0
        in_str = False
        escape = False
        for idx in range(start, len(text)):
            ch = text[idx]
            if in_str:
                if escape:
                    escape = False
                elif ch == "\\":
                    escape = True
                elif ch == '"':
                    in_str = False
            else:
                if ch == '"':
                    in_str = True
                elif ch == "{":
                    depth += 1
                elif ch == "}":
                    depth -= 1
                    if depth == 0:
                        return text[start : idx + 1]
        start = text.find("{", start + 1)
    return None


def extract_json_object(text: str) -> dict[str, Any]:
    """Parse a JSON object from raw text, ```json code block, or embedded `{...}` span."""
    if not isinstance(text, str) or not text.strip():
        raise LLMParseError("LLM response is empty; expected JSON object")

    candidates: list[str] = []
    json_blocks = extract_code_blocks(text, language="json")
    candidates.extend(json_blocks)
    if not json_blocks:
        candidates.extend(extract_code_blocks(text))
    candidates.append(text.strip())

    for candidate in candidates:
        stripped = candidate.strip()
        if not stripped:
            continue
        try:
            parsed = json.loads(stripped)
            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError:
            pass
        balanced = _scan_balanced_json_object(stripped)
        if balanced is not None:
            try:
                parsed = json.loads(balanced)
                if isinstance(parsed, dict):
                    return parsed
            except json.JSONDecodeError:
                pass

    raise LLMParseError("Could not extract a valid JSON object from LLM response")


def _build_fallback_bundle(settings: Settings) -> LLMConfigBundle:
    profile = LLMProfile(
        name="settings_default",
        provider="openai_compatible",
        base_url_env=settings.api["base_url_env"],
        api_key_env=settings.api["api_key_env"],
        model_env=settings.api["model_env"],
        model_name="",
        context_window=128000,
        max_input_tokens=96000,
        max_tokens=int(settings.api["max_tokens"]),
        max_completion_tokens=None,
        temperature=float(settings.api["temperature"]),
        top_p=None,
        top_k=None,
        seed=None,
        stop=(),
        timeout_seconds=float(settings.api["timeout_seconds"]),
        max_retries=int(settings.api["max_retries"]),
        retry_backoff_seconds=2.0,
        supports_tools=True,
        parallel_tool_calls=False,
        tool_choice="auto",
        thinking=ThinkingConfig(
            enabled=False,
            effort="medium",
            budget_tokens=None,
            provider_style="none",
            return_reasoning_content=False,
        ),
        extra_headers={},
        extra_body=dict(settings.api.get("extra_body", {})),
    )
    return LLMConfigBundle(
        config_path=settings.config_path,
        default_profile=profile.name,
        role_overrides={},
        profiles={profile.name: profile},
    )


def _is_non_retryable_error(exc: Exception) -> bool:
    status_code = getattr(exc, "status_code", None)
    if isinstance(status_code, int) and status_code in {400, 401, 403, 404, 422}:
        return True
    name = exc.__class__.__name__.lower()
    if any(tok in name for tok in ("authenticationerror", "permissiondenied", "badrequesterror", "notfounderror")):
        return True
    return False


def _normalize_sdk_response(raw: Any, fallback_model: str) -> LLMResponse:
    if isinstance(raw, LLMResponse):
        return raw
    choices = getattr(raw, "choices", None)
    if not choices:
        raise LLMClientError("LLM response contained no choices")
    choice = choices[0]
    message = getattr(choice, "message", None)
    if message is None:
        raise LLMClientError("LLM response choice contained no message")

    content_raw = getattr(message, "content", "")
    if isinstance(content_raw, list):
        content = "".join(
            str(part.get("text", "")) if isinstance(part, dict) else str(part)
            for part in content_raw
        )
    else:
        content = "" if content_raw is None else str(content_raw)

    reasoning_raw = getattr(message, "reasoning_content", "") or ""
    reasoning_content = str(reasoning_raw)

    raw_calls = getattr(message, "tool_calls", None) or []
    tool_calls: list[ToolCallRequest] = []
    for idx, call in enumerate(raw_calls):
        func = getattr(call, "function", None)
        if func is None and isinstance(call, dict):
            func = call.get("function", {})
            call_id = str(call.get("id") or f"call_{idx}")
            name = str(func.get("name", ""))
            args = str(func.get("arguments", "{}"))
        else:
            call_id = str(getattr(call, "id", None) or f"call_{idx}")
            name = str(getattr(func, "name", "") or "")
            args = str(getattr(func, "arguments", "{}") or "{}")
        if name:
            tool_calls.append(ToolCallRequest(id=call_id, name=name, arguments=args))

    usage = getattr(raw, "usage", None)
    prompt_tokens = int(getattr(usage, "prompt_tokens", 0) or 0) if usage else 0
    completion_tokens = int(getattr(usage, "completion_tokens", 0) or 0) if usage else 0
    total_tokens = int(getattr(usage, "total_tokens", prompt_tokens + completion_tokens) or 0) if usage else 0

    return LLMResponse(
        content=content,
        reasoning_content=reasoning_content,
        tool_calls=tool_calls,
        finish_reason=str(getattr(choice, "finish_reason", "") or ""),
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        total_tokens=total_tokens,
        model=str(getattr(raw, "model", None) or fallback_model),
    )


class OpenAILLMClient:
    """OpenAI-compatible LLM client backed by `Settings` + `LLMConfigBundle`."""

    def __init__(
        self,
        settings: Settings,
        *,
        llm_bundle: LLMConfigBundle | None = None,
        sdk_factory: Callable[..., Any] | None = None,
        sleep_fn: Callable[[float], None] = time.sleep,
    ) -> None:
        # API/LLM settings are validated and marked with SERVER_CONFIG_REQUIRED in core/config_loader.py and core/llm_config.py.
        self.settings = settings
        if llm_bundle is not None:
            self.bundle = llm_bundle
        else:
            llm_cfg_path = settings.api.get("llm_config_path")
            if llm_cfg_path is not None:
                self.bundle = load_llm_config(llm_cfg_path)
            else:
                self.bundle = _build_fallback_bundle(settings)

        self._sdk_factory = sdk_factory or _DefaultOpenAIClient
        self._sleep_fn = sleep_fn
        self._clients: dict[tuple[str, str, float], Any] = {}

    def _resolve_profile_and_creds(
        self,
        *,
        role: str | None,
        profile_name: str | None,
    ) -> tuple[LLMProfile, str, str, str]:
        profile = self.bundle.get_profile(name=profile_name, role=role)
        api_key = self.settings.get_env(profile.api_key_env)
        base_url = self.settings.get_env(profile.base_url_env)
        model = self.settings.get_env(profile.model_env) or profile.model_name
        if not api_key:
            raise ConfigError(f"Missing API key in env var '{profile.api_key_env}' for profile '{profile.name}'")
        if not base_url:
            raise ConfigError(f"Missing API base URL in env var '{profile.base_url_env}' for profile '{profile.name}'")
        if not model:
            raise ConfigError(f"Missing model name in env var '{profile.model_env}' for profile '{profile.name}'")
        return profile, api_key, base_url, model

    def _get_sdk_client(self, api_key: str, base_url: str, timeout: float) -> Any:
        if self._sdk_factory is None:
            raise LLMClientError("openai Python package is not installed")
        cache_key = (base_url, api_key, timeout)
        if cache_key not in self._clients:
            sdk_kwargs: dict[str, Any] = {
                "api_key": api_key,
                "base_url": base_url,
                "timeout": timeout,
            }
            if _DefaultOpenAIClient is not None and self._sdk_factory is _DefaultOpenAIClient:
                # Disable OpenAI SDK's internal retry loop so outer `profile.max_retries` is exact.
                sdk_kwargs["max_retries"] = 0
            self._clients[cache_key] = self._sdk_factory(**sdk_kwargs)
        return self._clients[cache_key]

    def chat(
        self,
        messages: list[dict[str, Any]],
        *,
        role: str | None = None,
        profile_name: str | None = None,
        tools: list[dict[str, Any]] | None = None,
    ) -> LLMResponse:
        profile, api_key, base_url, model = self._resolve_profile_and_creds(
            role=role, profile_name=profile_name
        )
        kwargs = build_chat_request_kwargs(profile, model, messages, tools=tools)
        client = self._get_sdk_client(api_key, base_url, profile.timeout_seconds)

        last_exc: Exception | None = None
        for attempt in range(profile.max_retries + 1):
            try:
                raw = client.chat.completions.create(**kwargs)
                return _normalize_sdk_response(raw, model)
            except (ConfigError, LLMClientError):
                raise
            except Exception as exc:
                last_exc = exc
                if _is_non_retryable_error(exc) or attempt >= profile.max_retries:
                    break
                if profile.retry_backoff_seconds > 0:
                    self._sleep_fn(profile.retry_backoff_seconds * (2 ** attempt))

        assert last_exc is not None
        raise LLMClientError(
            f"LLM request failed for profile '{profile.name}' after {profile.max_retries + 1} attempt(s): "
            f"{safe_error(last_exc, 'chat.completions.create')}"
        ) from None

    def run_tool_loop(
        self,
        messages: list[dict[str, Any]],
        dispatcher: ToolDispatcher,
        *,
        role: str | None = None,
        profile_name: str | None = None,
        max_rounds: int | None = None,
        include_lsp: bool = True,
        finalize_prompt: str | None = None,
    ) -> ToolLoopResult:
        """Execute a bounded multi-turn tool-calling conversation via `ToolDispatcher`."""
        conv = [dict(m) for m in messages]
        schemas = dispatcher.get_schemas(include_lsp=include_lsp)
        eff_max_rounds = max_rounds if max_rounds is not None else max(1, dispatcher.max_calls)
        if type(eff_max_rounds) is not int or eff_max_rounds < 1:
            raise ValueError("max_rounds must be an integer >= 1")

        tool_records: list[dict[str, Any]] = []
        total_prompt = 0
        total_completion = 0
        rounds_used = 0
        budget_exhausted = False
        last_response = LLMResponse(content="")

        for _ in range(eff_max_rounds):
            rounds_used += 1
            active_tools = schemas if dispatcher.call_count < dispatcher.max_calls else None
            resp = self.chat(conv, role=role, profile_name=profile_name, tools=active_tools)
            total_prompt += resp.prompt_tokens
            total_completion += resp.completion_tokens
            last_response = resp

            if not resp.tool_calls:
                conv.append({"role": "assistant", "content": resp.content})
                return ToolLoopResult(
                    final_response=resp,
                    messages=conv,
                    tool_records=tool_records,
                    rounds_used=rounds_used,
                    total_prompt_tokens=total_prompt,
                    total_completion_tokens=total_completion,
                    budget_exhausted=budget_exhausted,
                )

            conv.append(
                {
                    "role": "assistant",
                    "content": resp.content,
                    "tool_calls": [
                        {
                            "id": tc.id,
                            "type": "function",
                            "function": {"name": tc.name, "arguments": tc.arguments},
                        }
                        for tc in resp.tool_calls
                    ],
                }
            )
            for tc in resp.tool_calls:
                tool_result = dispatcher.dispatch(tc.name, tc.arguments)
                tool_records.append(
                    {
                        "tool": tc.name,
                        "arguments": tc.arguments,
                        "result": tool_result,
                    }
                )
                conv.append(
                    {
                        "role": "tool",
                        "tool_call_id": tc.id,
                        "content": json.dumps(tool_result, ensure_ascii=False),
                    }
                )

            if dispatcher.call_count >= dispatcher.max_calls:
                budget_exhausted = True
                break

        # Final wrap-up without tools when tool calls exhausted the round/call budget.
        if last_response.tool_calls:
            budget_exhausted = True
            if finalize_prompt:
                conv.append({"role": "user", "content": finalize_prompt})
            final_resp = self.chat(conv, role=role, profile_name=profile_name, tools=None)
            total_prompt += final_resp.prompt_tokens
            total_completion += final_resp.completion_tokens
            conv.append({"role": "assistant", "content": final_resp.content})
            last_response = final_resp

        return ToolLoopResult(
            final_response=last_response,
            messages=conv,
            tool_records=tool_records,
            rounds_used=rounds_used,
            total_prompt_tokens=total_prompt,
            total_completion_tokens=total_completion,
            budget_exhausted=budget_exhausted,
        )
