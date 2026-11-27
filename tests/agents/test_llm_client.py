"""Unit tests for `OpenAILLMClient`, response parsers, and bounded tool-calling loop."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from specpatchguard.agents.llm_client import (
    LLMClientError,
    LLMParseError,
    OpenAILLMClient,
    extract_code_blocks,
    extract_json_object,
    extract_tagged_block,
)
from specpatchguard.analysis.ast_context import JavaAstContextTools
from specpatchguard.analysis.protocols import LocalRepoExecutor
from specpatchguard.analysis.repo_tools import RepoSearchTools
from specpatchguard.analysis.tool_registry import ToolDispatcher
from specpatchguard.core.config_loader import load_settings


class _TransientError(RuntimeError):
    status_code = 503


class _AuthError(RuntimeError):
    status_code = 401


def _write_test_project(tmp_path: Path) -> tuple[Path, Path]:
    cfg_dir = tmp_path / "config"
    cfg_dir.mkdir(parents=True)
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    for name in (
        "d4j_custom_function_level_patch.pkl",
        "labeled_d4j20_enhance_info.pkl",
        "clean_d4j20_bug_report.json",
        "manifest.json",
        "rule.md",
    ):
        (data_dir / name).write_text("{}", encoding="utf-8")

    llm_cfg_src = Path("E:/workspace/myown/SpecPatchGuard/config/llm_config.json").read_text(encoding="utf-8")
    llm_data = json.loads(llm_cfg_src)
    llm_data["role_overrides"]["intent_agent"] = "claude_thinking"
    (cfg_dir / "llm_config.json").write_text(json.dumps(llm_data), encoding="utf-8")

    cfg = json.loads(Path("E:/workspace/myown/SpecPatchGuard/config/config.json").read_text(encoding="utf-8"))
    cfg["data"] = {
        "patch_info_pkl": str(data_dir / "d4j_custom_function_level_patch.pkl"),
        "test_info_pkl": str(data_dir / "labeled_d4j20_enhance_info.pkl"),
        "bug_report_json": str(data_dir / "clean_d4j20_bug_report.json"),
        "manifest_json": str(data_dir / "manifest.json"),
        "rule_md_path": str(data_dir / "rule.md"),
    }
    cfg["api"]["llm_config_path"] = "./llm_config.json"
    cfg["output"] = {
        "result_dir": str(tmp_path / "out"),
        "spec_cache_dir": str(tmp_path / "out" / "specs"),
        "resume": True,
    }
    cfg_path = cfg_dir / "config.json"
    cfg_path.write_text(json.dumps(cfg), encoding="utf-8")

    env_path = tmp_path / ".env"
    env_path.write_text(
        "OPENAI_API_KEY=sk-secret-unit-test-key\n"
        "OPENAI_API_BASE=https://api.example.com/v1\n"
        "LLM_MODEL=deepseek-chat\n",
        encoding="utf-8",
    )
    return cfg_path, env_path


def _fake_sdk_resp(
    content: str,
    *,
    tool_calls: list[tuple[str, str, str]] | None = None,
    prompt_tokens: int = 10,
    completion_tokens: int = 5,
) -> Any:
    calls = []
    for cid, name, args in tool_calls or []:
        calls.append(
            SimpleNamespace(
                id=cid,
                function=SimpleNamespace(name=name, arguments=args),
            )
        )
    msg = SimpleNamespace(content=content, reasoning_content="", tool_calls=calls)
    choice = SimpleNamespace(message=msg, finish_reason="tool_calls" if calls else "stop")
    usage = SimpleNamespace(
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        total_tokens=prompt_tokens + completion_tokens,
    )
    return SimpleNamespace(choices=[choice], usage=usage, model="mock-model")


def test_response_parsers_handle_edge_cases() -> None:
    text = (
        "Note:\n"
        "```python\nprint('hello')\n```\n"
        "```json\n{\"key\": \"brace } inside\", \"n\": 2}\n```\n"
        "<SimpleBlock>\n+ return false;\n</SimpleBlock>"
    )
    assert extract_code_blocks(text, language="python") == ["print('hello')"]
    assert extract_tagged_block(text, "SimpleBlock") == "+ return false;"
    assert extract_tagged_block(text, "MissingTag") is None
    assert extract_json_object(text) == {"key": "brace } inside", "n": 2}
    assert extract_json_object('prefix {"a": {"b": [1, 2]}} suffix') == {"a": {"b": [1, 2]}}

    with pytest.raises(LLMParseError):
        extract_json_object("no json here at all")
    with pytest.raises(LLMParseError):
        extract_json_object("[1, 2, 3]")


def test_openai_client_role_override_retry_and_secret_redaction(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    for var in ("OPENAI_API_KEY", "OPENAI_API_BASE", "LLM_MODEL"):
        monkeypatch.delenv(var, raising=False)
    cfg_path, env_path = _write_test_project(tmp_path)
    settings = load_settings(cfg_path, env_path=env_path)

    captured_kwargs: list[dict[str, Any]] = []
    attempts = {"count": 0}
    sleeps: list[float] = []

    class _FakeCompletions:
        def create(self, **kwargs: Any) -> Any:
            captured_kwargs.append(kwargs)
            attempts["count"] += 1
            if attempts["count"] == 1:
                raise _TransientError("temporary failure with sk-secret-unit-test-key")
            return _fake_sdk_resp('{"ok": true}')

    client = OpenAILLMClient(
        settings,
        sdk_factory=lambda **kw: SimpleNamespace(chat=SimpleNamespace(completions=_FakeCompletions())),
        sleep_fn=sleeps.append,
    )

    resp = client.chat([{"role": "user", "content": "hi"}], role="intent_agent")
    assert resp.content == '{"ok": true}'
    assert sleeps == [2.0]
    # intent_agent was routed to claude_thinking in _write_test_project
    assert captured_kwargs[0]["extra_body"]["thinking"] == {"type": "enabled", "budget_tokens": 8192}
    assert captured_kwargs[0]["temperature"] == 1.0

    # Non-retryable 401 fails immediately without sleeping, and redacts API key
    class _FailAuthCompletions:
        def create(self, **kwargs: Any) -> Any:
            raise _AuthError("invalid token sk-secret-unit-test-key")

    auth_client = OpenAILLMClient(
        settings,
        sdk_factory=lambda **kw: SimpleNamespace(chat=SimpleNamespace(completions=_FailAuthCompletions())),
        sleep_fn=lambda _: pytest.fail("Should not sleep on 401"),
    )
    with pytest.raises(LLMClientError) as exc_info:
        auth_client.chat([{"role": "user", "content": "hi"}])
    assert "sk-secret-unit-test-key" not in str(exc_info.value)


def test_run_tool_loop_executes_tools_and_handles_budget_exhaustion(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    for var in ("OPENAI_API_KEY", "OPENAI_API_BASE", "LLM_MODEL"):
        monkeypatch.delenv(var, raising=False)
    cfg_path, env_path = _write_test_project(tmp_path)
    settings = load_settings(cfg_path, env_path=env_path)

    repo_root = tmp_path / "repo"
    (repo_root / "src").mkdir(parents=True)
    (repo_root / "src" / "Sample.java").write_text(
        "public class Sample {\n    public int add(int a, int b) {\n        return a + b;\n    }\n}\n",
        encoding="utf-8",
    )
    executor = LocalRepoExecutor(repo_root, state="prepatch", readonly_prepatch=True)
    tools_cfg = {**settings.tools, "max_tool_calls_per_agent": 1}
    dispatcher = ToolDispatcher(
        RepoSearchTools(executor, tools_cfg, readonly_prepatch=True),
        JavaAstContextTools(executor, tools_cfg, readonly_prepatch=True),
        tools_cfg,
        lsp_tools=None,
    )

    replies = [
        _fake_sdk_resp("", tool_calls=[("c1", "grep_code", json.dumps({"pattern": r"return a \+ b"}))]),
        _fake_sdk_resp('{"bug_intent_summary": "final after budget"}'),
    ]
    seen_tools_arg: list[bool] = []

    class _LoopCompletions:
        def create(self, **kwargs: Any) -> Any:
            seen_tools_arg.append("tools" in kwargs)
            return replies.pop(0)

    client = OpenAILLMClient(
        settings,
        sdk_factory=lambda **kw: SimpleNamespace(chat=SimpleNamespace(completions=_LoopCompletions())),
    )
    loop_res = client.run_tool_loop(
        [{"role": "user", "content": "inspect"}],
        dispatcher,
        role="pbt_agent",
        include_lsp=False,
        finalize_prompt="wrap up now",
    )

    assert loop_res.budget_exhausted is True
    assert len(loop_res.tool_records) == 1
    assert loop_res.tool_records[0]["result"]["ok"] is True
    assert seen_tools_arg == [True, False]
    assert "final after budget" in loop_res.final_response.content


def test_default_openai_sdk_disables_internal_retries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from specpatchguard.agents import llm_client as lc

    for var in ("OPENAI_API_KEY", "OPENAI_API_BASE", "LLM_MODEL"):
        monkeypatch.delenv(var, raising=False)
    cfg_path, env_path = _write_test_project(tmp_path)
    settings = load_settings(cfg_path, env_path=env_path)

    captured_init: list[dict[str, Any]] = []

    def _fake_default_openai(**kwargs: Any) -> Any:
        captured_init.append(kwargs)
        return SimpleNamespace(
            chat=SimpleNamespace(completions=SimpleNamespace(create=lambda **kw: _fake_sdk_resp("ok")))
        )

    monkeypatch.setattr(lc, "_DefaultOpenAIClient", _fake_default_openai)
    client = lc.OpenAILLMClient(settings)
    client.chat([{"role": "user", "content": "ping"}])
    assert captured_init[0]["max_retries"] == 0

