"""Unit tests for SpecPatchGuard Step 1 (config loading, .env isolation, doctor preflight, and marker parity)."""

from __future__ import annotations

import contextlib
import copy
import io
import json
import os
from pathlib import Path
import pickle
import sys
from unittest.mock import patch

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2]
PACKAGE_ROOT = PROJECT_ROOT / "specpatchguard"
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from specpatchguard import __main__ as cli  # noqa: E402
from specpatchguard.core import config_loader  # noqa: E402
from specpatchguard.core.config_loader import (  # noqa: E402
    ConfigError,
    PlainUnpickler,
    atomic_json,
    configuration_status,
    load_pickle,
    load_settings,
    parse_json,
    parse_pickle,
    resolve_local_path,
)
from specpatchguard.core.doctor import parse_java_major, run_doctor, scan_server_config_markers  # noqa: E402


def _make_fixture_workspace(tmp_path: Path, use_config_subdir: bool = False) -> tuple[Path, dict]:
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    for filename, payload in (
        ("patches.pkl", b"dummy"),
        ("tests.pkl", b"dummy"),
        ("reports.json", b"{}"),
        ("manifest.json", b"{}"),
        ("rule.md", b"# rules\n"),
    ):
        (data_dir / filename).write_bytes(payload)

    mount_dir = tmp_path / "d4j_work"
    mount_dir.mkdir(parents=True, exist_ok=True)

    prefix = "../" if use_config_subdir else ""
    cfg = {
        "data": {
            "patch_info_pkl": f"{prefix}data/patches.pkl",
            "test_info_pkl": f"{prefix}data/tests.pkl",
            "bug_report_json": f"{prefix}data/reports.json",
            "manifest_json": f"{prefix}data/manifest.json",
            "rule_md_path": f"{prefix}data/rule.md",
        },
        "api": {
            "base_url_env": "OPENAI_API_BASE",
            "api_key_env": "OPENAI_API_KEY",
            "model_env": "LLM_MODEL",
            "temperature": 0.0,
            "max_tokens": 4096,
            "timeout_seconds": 120,
            "max_retries": 2,
        },
        "docker": {
            "image_name": "d4j-lsp-runtime:latest",
            "container_work_root": "/workspace/d4j_work",
            "host_mount_root": mount_dir.as_posix(),
            "timeout_seconds": 300,
            "nano_cpus": 8000000000,
        },
        "defects4j": {
            "d4j_home": "/data/public/defects4j",
            "java8_home": "/workspace/envs/defects4j",
            "compile_timeout_sec": 120,
            "test_timeout_sec": 120,
        },
        "lsp": {
            "enabled": True,
            "mode": "docker_jdtls",
            "jdtls_home": "/opt/jdtls",
            "jdtls_java_home": "/opt/jdk-17",
            "jdtls_config_dir": "/opt/jdtls/config_linux",
            "jdtls_workspace_cache": "/tmp/jdtls_ws_cache",
            "init_timeout_sec": 45,
            "request_timeout_sec": 15,
        },
        "tools": {
            "grep_default_context_before": 5,
            "grep_default_context_after": 5,
            "grep_max_context_lines": 50,
            "grep_max_matches": 30,
            "read_default_context_before": 10,
            "read_default_context_after": 10,
            "read_max_lines": 200,
            "branch_context_lines": 3,
            "max_tool_calls_per_agent": 15,
            "max_tool_output_chars": 24000,
            "command_timeout_sec": 900,
        },
        "output": {
            "result_dir": f"{prefix}SpecPatchGuard_result",
            "spec_cache_dir": f"{prefix}SpecPatchGuard_result/frozen_specs",
            "resume": True,
        },
        "pipeline": {
            "target_tier": "tier_a",
            "enable_cross_rep_tier_a": True,
            "enable_cross_rep_tier_b": False,
            "enable_anchor_infection_probes": True,
            "pbt_refine_max_rounds": 5,
            "syntax_fix_max_rounds": 3,
            "hypothesis_max_examples": 100,
        },
    }
    cfg_path = (tmp_path / "config" / "config.json") if use_config_subdir else (tmp_path / "config.json")
    atomic_json(cfg, cfg_path)
    return cfg_path, cfg


def test_default_config_loads_and_preserves_posix_server_paths():
    settings = load_settings(PROJECT_ROOT / "config" / "config.json")
    for key in ("patch_info_pkl", "test_info_pkl", "bug_report_json", "manifest_json", "rule_md_path"):
        resolved = settings.data[key]
        assert resolved is not None
        assert resolved.is_file(), f"Missing referenced file for data.{key}: {resolved}"
    assert settings.docker["container_work_root"] == "/workspace/d4j_work"
    assert settings.docker["host_mount_root"] == "/data/patch_va/d4j_work"
    assert settings.defects4j["d4j_home"] == "/data/public/defects4j"
    assert settings.defects4j["java8_home"] == "/workspace/envs/defects4j"
    assert settings.lsp["jdtls_home"] == "/opt/jdtls"
    assert settings.lsp["jdtls_java_home"] == "/opt/jdk-17"
    assert settings.pipeline["target_tier"] == "tier_a"
    assert settings.pipeline["enable_cross_rep_tier_b"] is False
    assert settings.api["timeout_seconds"] == 900.0
    assert settings.docker["timeout_seconds"] == 900
    assert settings.defects4j["compile_timeout_sec"] == 900
    assert settings.defects4j["test_timeout_sec"] == 900
    assert settings.lsp["init_timeout_sec"] == 900
    assert settings.lsp["request_timeout_sec"] == 900
    assert settings.tools["command_timeout_sec"] == 900
    assert settings.llm_config_path is not None and settings.llm_config_path.is_file()


def test_resolve_local_path_preserves_posix_prefix_on_windows():
    base = Path("E:/workspace/myown/SpecPatchGuard/config")
    posix_path = resolve_local_path(base, "/data/public/defects4j", "test")
    assert posix_path is not None
    assert posix_path.as_posix() == "/data/public/defects4j"


def test_env_loading_precedence_and_secret_non_leakage(tmp_path: Path):
    cfg_path, _ = _make_fixture_workspace(tmp_path)
    env_file = tmp_path / ".env"
    secret_from_env_file = "sk-secret-from-dotenv-file-9999"
    secret_from_os = "sk-secret-from-os-override-8888"
    env_file.write_text(
        f"OPENAI_API_KEY={secret_from_env_file}\n"
        "OPENAI_API_BASE=https://api.example.invalid/v1\n"
        "LLM_MODEL=deepseek-chat\n",
        encoding="utf-8",
    )

    clean_env = {
        k: v for k, v in os.environ.items()
        if k not in {"OPENAI_API_KEY", "OPENAI_API_BASE", "LLM_MODEL"}
    }
    with patch.dict(os.environ, clean_env, clear=True):
        settings = load_settings(cfg_path)
        assert settings.api_key() == secret_from_env_file
        assert settings.api_base_url() == "https://api.example.invalid/v1"
        assert settings.api_model() == "deepseek-chat"
        assert secret_from_env_file not in repr(settings)
        assert secret_from_env_file not in json.dumps(configuration_status(settings))
        report = run_doctor(settings, mode="local")
        assert secret_from_env_file not in json.dumps(report)

    with patch.dict(os.environ, {**clean_env, "OPENAI_API_KEY": secret_from_os}, clear=True):
        settings = load_settings(cfg_path)
        assert settings.api_key() == secret_from_os
        assert secret_from_os not in repr(settings)
        assert secret_from_os not in json.dumps(configuration_status(settings))
        report = run_doctor(settings, mode="local")
        assert secret_from_os not in json.dumps(report)


def test_config_subdir_discovers_project_root_env(tmp_path: Path):
    cfg_path, _ = _make_fixture_workspace(tmp_path, use_config_subdir=True)
    root_env = tmp_path / ".env"
    root_env.write_text(
        "OPENAI_API_KEY=sk-root-env-777\n"
        "OPENAI_API_BASE=https://api.example.invalid/v1\n"
        "LLM_MODEL=deepseek-chat\n",
        encoding="utf-8",
    )
    clean_env = {
        k: v for k, v in os.environ.items()
        if k not in {"OPENAI_API_KEY", "OPENAI_API_BASE", "LLM_MODEL"}
    }
    with patch.dict(os.environ, clean_env, clear=True):
        settings = load_settings(cfg_path)
        assert settings.env_path == root_env.resolve()
        assert settings.api_key() == "sk-root-env-777"


def test_explicit_missing_env_file_raises(tmp_path: Path):
    cfg_path, _ = _make_fixture_workspace(tmp_path)
    with pytest.raises(ConfigError, match="Specified .env file not found"):
        load_settings(cfg_path, env_path=tmp_path / "missing.env")


@pytest.mark.parametrize(
    "mutator",
    [
        lambda c: c.pop("pipeline"),
        lambda c: c.__setitem__("extra_section", {}),
        lambda c: c["docker"].__setitem__("unknown_key", 1),
        lambda c: c["api"].__setitem__("temperature", True),
        lambda c: c["api"].__setitem__("max_tokens", True),
        lambda c: c["docker"].__setitem__("nano_cpus", False),
        lambda c: c["api"].__setitem__("temperature", -0.1),
        lambda c: c["api"].__setitem__("temperature", 2.5),
        lambda c: c["api"].__setitem__("timeout_seconds", 0),
        lambda c: c["docker"].__setitem__("timeout_seconds", 0),
        lambda c: c["api"].__setitem__("api_key_env", "123BAD"),
        lambda c: c["lsp"].__setitem__("mode", "disabled"),
        lambda c: c["pipeline"].__setitem__("target_tier", "tier_c"),
        lambda c: c["api"].__setitem__("extra_body", {"model": "override"}),
        lambda c: c["docker"].__setitem__("container_work_root", "relative/path"),
        lambda c: c["defects4j"].__setitem__("d4j_home", "/bad\npath"),
        lambda c: c["output"].__setitem__("result_dir", ""),
        lambda c: c["tools"].__setitem__("grep_default_context_before", -1),
        lambda c: c["tools"].__setitem__("grep_default_context_before", 60),
        lambda c: c["tools"].__setitem__("grep_max_matches", True),
        lambda c: c["tools"].__setitem__("max_tool_output_chars", 999),
        lambda c: c["tools"].__setitem__("command_timeout_sec", 0),
        lambda c: c["tools"].__setitem__("unknown_tool_opt", 1),
    ],
)
def test_invalid_config_boundaries_rejected(tmp_path: Path, mutator):
    cfg_path, cfg = _make_fixture_workspace(tmp_path)
    mutated = copy.deepcopy(cfg)
    mutator(mutated)
    atomic_json(mutated, cfg_path)
    with pytest.raises(ConfigError):
        load_settings(cfg_path)


def test_nan_and_infinity_rejected_by_validation(tmp_path: Path):
    cfg_path, cfg = _make_fixture_workspace(tmp_path)
    cfg_path.write_text(json.dumps(cfg).replace("0.0", "NaN"), encoding="utf-8")
    with pytest.raises(ConfigError):
        load_settings(cfg_path)


@pytest.mark.parametrize(
    "bad_url",
    [
        "ftp://example.com/v1",
        "https://user:pass@example.com/v1",
        "not-a-url",
    ],
)
def test_bad_api_base_url_flagged_by_status_and_doctor(tmp_path: Path, bad_url: str):
    cfg_path, _ = _make_fixture_workspace(tmp_path)
    with patch.dict(
        os.environ,
        {"OPENAI_API_KEY": "sk-test", "OPENAI_API_BASE": bad_url, "LLM_MODEL": "deepseek-chat"},
        clear=True,
    ):
        settings = load_settings(cfg_path)
        status = configuration_status(settings)
        assert any("OPENAI_API_BASE" in item for item in status["pending"])
        report = run_doctor(settings, mode="local")
        assert report["overall_ok"] is False
        api_check = next(c for c in report["checks"] if c["name"] == "api_credentials")
        assert api_check["status"] == "fail"


def test_parse_json_bom_and_double_encoded():
    raw_bom = b"\xef\xbb\xbf" + json.dumps({"a": 1}).encode("utf-8")
    assert parse_json(raw_bom) == {"a": 1}

    double_encoded = json.dumps(json.dumps({"nested": True})).encode("utf-8")
    assert parse_json(double_encoded) == {"nested": True}

    with pytest.raises(ConfigError):
        parse_json(b"{broken")


def test_plain_unpickler_blocks_arbitrary_classes(tmp_path: Path):
    safe_bytes = pickle.dumps([{"k": [1, "two", None]}], protocol=4)
    assert parse_pickle(safe_bytes) == [{"k": [1, "two", None]}]

    evil_bytes = pickle.dumps(Path("x"), protocol=4)
    with pytest.raises(ConfigError, match="Unsupported pickle object"):
        parse_pickle(evil_bytes)

    pkl_file = tmp_path / "safe.pkl"
    pkl_file.write_bytes(safe_bytes)
    assert load_pickle(pkl_file) == [{"k": [1, "two", None]}]
    assert PlainUnpickler is not None


def test_atomic_json_writes_and_cleans_up_on_error(tmp_path: Path):
    target = tmp_path / "out" / "result.json"
    atomic_json({"ok": True, "zh": "中文"}, target)
    assert json.loads(target.read_text(encoding="utf-8")) == {"ok": True, "zh": "中文"}

    bad_target = tmp_path / "out" / "bad.json"
    with pytest.raises(TypeError):
        atomic_json({"bad": object()}, bad_target)
    assert not bad_target.exists()
    assert list((tmp_path / "out").glob("bad.json.*")) == []


def test_doctor_local_mode_skips_docker_and_detects_output_collision(tmp_path: Path):
    cfg_path, cfg = _make_fixture_workspace(tmp_path, use_config_subdir=True)
    env = {"OPENAI_API_KEY": "sk-test", "OPENAI_API_BASE": "https://api.example.invalid/v1", "LLM_MODEL": "m"}
    with patch.dict(os.environ, env, clear=True):
        settings = load_settings(cfg_path)
        report = run_doctor(settings, mode="local")
        assert report["effective_mode"] == "local"
        assert report["overall_ok"] is True
        docker_check = next(c for c in report["checks"] if c["name"] == "docker_d4j_lsp")
        assert docker_check["status"] == "skipped"

        cfg_bad = copy.deepcopy(cfg)
        cfg_bad["output"]["result_dir"] = "../data/reports.json"
        atomic_json(cfg_bad, cfg_path)
        bad_settings = load_settings(cfg_path)
        bad_report = run_doctor(bad_settings, mode="local")
        assert bad_report["overall_ok"] is False
        out_check = next(c for c in bad_report["checks"] if c["name"] == "output_safety")
        assert out_check["status"] == "fail"


def test_doctor_server_mode_mocked_pass_and_failure_cases(tmp_path: Path):
    cfg_path, _ = _make_fixture_workspace(tmp_path)
    env = {"OPENAI_API_KEY": "sk-test", "OPENAI_API_BASE": "https://api.example.invalid/v1", "LLM_MODEL": "m"}

    def pass_runner(argv: list[str], timeout: int) -> tuple[int, str, str]:
        if argv[:2] == ["docker", "info"]:
            return 0, "Server Version: 26.0", ""
        if argv[:3] == ["docker", "image", "inspect"]:
            return 0, "[{}]", ""
        if argv[:2] == ["docker", "run"]:
            out = (
                "D4J_BIN=OK\n"
                "GREP_BIN=OK\n"
                "JAVA8_VER_START\n"
                'openjdk version "1.8.0_392"\n'
                "JAVA8_VER_END\n"
                "JDTLS_HOME=OK\n"
                "JDTLS_CFG=OK\n"
                "JDTLS_JAVA_VER_START\n"
                'openjdk version "17.0.10"\n'
                "JDTLS_JAVA_VER_END\n"
            )
            return 0, out, ""
        return 1, "", "unexpected cmd"

    with patch.dict(os.environ, env, clear=True):
        settings = load_settings(cfg_path)
        ok_report = run_doctor(settings, mode="server", runner=pass_runner, http_probe=lambda u, k, t: (200, "ok"), check_api=True)
        assert ok_report["overall_ok"] is True
        docker_check = next(c for c in ok_report["checks"] if c["name"] == "docker_d4j_lsp")
        assert docker_check["status"] == "pass"
        assert docker_check["data"]["d4j_java_major"] == 8
        assert docker_check["data"]["jdtls_java_major"] == 17

        def wrong_d4j_java(argv: list[str], timeout: int):
            rc, out, err = pass_runner(argv, timeout)
            return rc, out.replace('1.8.0_392', '11.0.22'), err

        r1 = run_doctor(settings, mode="server", runner=wrong_d4j_java)
        assert r1["overall_ok"] is False
        assert "major 8" in next(c for c in r1["checks"] if c["name"] == "docker_d4j_lsp")["detail"]

        def wrong_jdtls_java(argv: list[str], timeout: int):
            rc, out, err = pass_runner(argv, timeout)
            return rc, out.replace('17.0.10', '1.8.0_392'), err

        r2 = run_doctor(settings, mode="server", runner=wrong_jdtls_java)
        assert r2["overall_ok"] is False
        assert ">= 17" in next(c for c in r2["checks"] if c["name"] == "docker_d4j_lsp")["detail"]

        def missing_image(argv: list[str], timeout: int):
            if argv[:3] == ["docker", "image", "inspect"]:
                return 1, "", "Error: No such image"
            return pass_runner(argv, timeout)

        r3 = run_doctor(settings, mode="server", runner=missing_image)
        assert r3["overall_ok"] is False
        assert "Docker image missing" in next(c for c in r3["checks"] if c["name"] == "docker_d4j_lsp")["detail"]

        def timeout_runner(argv: list[str], timeout: int):
            if argv[:2] == ["docker", "run"]:
                return 124, "", "Command timed out after 30s"
            return pass_runner(argv, timeout)

        r4 = run_doctor(settings, mode="server", runner=timeout_runner)
        assert r4["overall_ok"] is False
        assert "timed out" in next(c for c in r4["checks"] if c["name"] == "docker_d4j_lsp")["detail"]

        def missing_grep(argv: list[str], timeout: int):
            rc, out, err = pass_runner(argv, timeout)
            return rc, out.replace("GREP_BIN=OK", "GREP_BIN=MISSING"), err

        r5 = run_doctor(settings, mode="server", runner=missing_grep)
        assert r5["overall_ok"] is False
        assert "grep" in next(c for c in r5["checks"] if c["name"] == "docker_d4j_lsp")["detail"]


def test_parse_java_major():
    assert parse_java_major('java version "1.8.0_392"') == 8
    assert parse_java_major('openjdk version "17.0.10" 2024-01-16') == 17
    assert parse_java_major('openjdk version "21.0.2"') == 21
    assert parse_java_major("unknown") is None


def test_cli_exit_codes(tmp_path: Path):
    cfg_path, _ = _make_fixture_workspace(tmp_path)
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        assert cli.main(["check-config", "--config", str(cfg_path)]) == 0
        assert cli.main(["check-config", "--config", str(tmp_path / "missing.json")]) == 2

        clean_env = {k: v for k, v in os.environ.items() if k not in {"OPENAI_API_KEY", "OPENAI_API_BASE", "LLM_MODEL"}}
        with patch.dict(os.environ, clean_env, clear=True):
            assert cli.main(["doctor", "--config", str(cfg_path), "--mode", "local"]) == 1

        with patch.dict(
            os.environ,
            {**clean_env, "OPENAI_API_KEY": "sk-test", "OPENAI_API_BASE": "https://api.example.invalid/v1", "LLM_MODEL": "m"},
            clear=True,
        ):
            assert cli.main(["doctor", "--config", str(cfg_path), "--mode", "local"]) == 0


def test_marker_parity_with_deploy_checklist():
    markers = scan_server_config_markers(PACKAGE_ROOT)
    assert len(markers) == 9
    assert any(m["file"].startswith("core/") for m in markers)
    assert any(m["file"].startswith("analysis/") for m in markers)
    assert all("reminder(s)" not in m["note"] and '"""' not in m["note"] for m in markers)
    checklist = (PROJECT_ROOT / "SERVER_DEPLOY_CHECKLIST.md").read_text(encoding="utf-8")
    for section in ("data", "api", "docker", "defects4j", "lsp", "tools", "output", "pipeline"):
        assert f"`{section}`" in checklist
    assert "llm_config.json" in checklist
    assert "SERVER_CONFIG_REQUIRED" in checklist


def test_env_fallback_parser(tmp_path: Path):
    env_file = tmp_path / ".env"
    env_file.write_text(
        "# comment\n"
        "export OPENAI_API_KEY='sk-quoted-123'\n"
        'OPENAI_API_BASE="https://example.invalid/v1"\n'
        "LLM_MODEL=deepseek-chat # inline comment\n"
        "EMPTY_KEY=\n",
        encoding="utf-8",
    )
    with patch.object(config_loader, "_dotenv_values", None):
        parsed = config_loader.load_env_file(env_file)
    assert parsed == {
        "OPENAI_API_KEY": "sk-quoted-123",
        "OPENAI_API_BASE": "https://example.invalid/v1",
        "LLM_MODEL": "deepseek-chat",
        "EMPTY_KEY": "",
    }
