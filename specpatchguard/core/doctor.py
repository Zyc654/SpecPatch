"""Read-only environment, data, Docker/D4J/jdtls, and marker preflight checks (`doctor`).

Java version parsing adapts `furina_context_collector/collect.py` (`doctor`).
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
import importlib.util
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys
from typing import Any, Callable
import urllib.request

from .config_loader import (
    ConfigError,
    Settings,
    public_api_settings,
    safe_error,
    validate_api_url,
)
from .llm_config import load_llm_config

CommandRunner = Callable[[list[str], int], tuple[int, str, str]]
HttpProbe = Callable[[str, str, float], tuple[int, str]]

_MARKER_RE = re.compile(r"^\s*#\s*\[SERVER_CONFIG_REQUIRED\]:\s*(.+)")
_JAVA_VER_RE = re.compile(r'(?:version\s+"|javac\s+|openjdk\s+|java\s+)(\d+)(?:\.(\d+))?')
_INTERRUPTED_MERGE_DIRS = (
    "before_diff_fallback",
    "before_content_alignment",
    "before_literal_additions",
    "before_escaped_newline",
)
_OPTIONAL_PACKAGES = (
    "dotenv",
    "hypothesis",
    "tree_sitter",
    "tree_sitter_java",
    "docker",
    "openai",
)


@dataclass
class CheckResult:
    """Single preflight diagnostic result."""

    name: str
    status: str  # "pass" | "warn" | "fail" | "skipped"
    detail: str
    data: dict[str, Any] = field(default_factory=dict)


def default_runner(argv: list[str], timeout: int, cwd: str | Path | None = None) -> tuple[int, str, str]:
    """Execute a read-only subprocess safely without raising on non-zero/timeout/missing binary."""
    try:
        completed = subprocess.run(
            argv,
            cwd=str(cwd) if cwd is not None else None,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
        )
        return completed.returncode, completed.stdout or "", completed.stderr or ""
    except FileNotFoundError as exc:
        return 127, "", f"Executable not found: {exc.filename or argv[0]}"
    except subprocess.TimeoutExpired:
        return 124, "", f"Command timed out after {timeout}s"
    except OSError as exc:
        return 126, "", f"OS error running {argv[0]}: {exc}"


def default_http_probe(url: str, api_key: str, timeout: float) -> tuple[int, str]:
    """Perform a lightweight GET `/models` probe without leaking the bearer token."""
    target = url.rstrip("/") + "/models"
    request = urllib.request.Request(
        target,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Accept": "application/json",
        },
        method="GET",
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        status = getattr(response, "status", response.getcode())
        return int(status), "ok"


def parse_java_major(version_text: str) -> int | None:
    """Parse Java major version (`1.8.0_392` -> 8, `17.0.10` -> 17)."""
    match = _JAVA_VER_RE.search(version_text or "")
    if not match:
        return None
    if match.group(1) == "1" and match.group(2):
        return int(match.group(2))
    return int(match.group(1))


def scan_server_config_markers(package_root: Path) -> list[dict[str, Any]]:
    """Scan non-test package source files for `# [SERVER_CONFIG_REQUIRED]` reminders."""
    markers: list[dict[str, Any]] = []
    if not package_root.is_dir():
        return markers
    for py_file in sorted(package_root.rglob("*.py")):
        rel = py_file.relative_to(package_root)
        if rel.parts and rel.parts[0] == "tests":
            continue
        text = py_file.read_text(encoding="utf-8-sig", errors="replace")
        for lineno, line in enumerate(text.splitlines(), start=1):
            match = _MARKER_RE.search(line)
            if match:
                markers.append(
                    {
                        "file": str(rel).replace("\\", "/"),
                        "line": lineno,
                        "note": match.group(1).strip(),
                    }
                )
    return markers


def _check_api_credentials(
    settings: Settings,
    check_api: bool,
    http_probe: HttpProbe,
) -> CheckResult:
    key = settings.api_key()
    base_url = settings.api_base_url()
    model = settings.api_model()
    url_error = validate_api_url(base_url)

    missing: list[str] = []
    if not key:
        missing.append(settings.api["api_key_env"])
    if url_error is not None:
        missing.append(f"{settings.api['base_url_env']} ({url_error})")
    if not model:
        missing.append(settings.api["model_env"])

    public_info = public_api_settings(settings)
    data = {
        "key_configured": bool(key),
        "env_var_names": {
            "api_key_env": settings.api["api_key_env"],
            "base_url_env": settings.api["base_url_env"],
            "model_env": settings.api["model_env"],
        },
        **public_info,
    }

    if missing:
        return CheckResult(
            name="api_credentials",
            status="fail",
            detail="Missing or invalid API configuration: " + ", ".join(missing),
            data=data,
        )

    if check_api:
        probe_timeout = min(15.0, float(settings.api["timeout_seconds"]))
        try:
            status_code, _ = http_probe(base_url, key, probe_timeout)
            data["api_probe_http_status"] = status_code
            if status_code >= 400:
                return CheckResult(
                    name="api_credentials",
                    status="fail",
                    detail=f"API endpoint probe returned HTTP {status_code}",
                    data=data,
                )
        except Exception as exc:
            return CheckResult(
                name="api_credentials",
                status="fail",
                detail=safe_error(exc, "API endpoint probe failed"),
                data=data,
            )

    return CheckResult(
        name="api_credentials",
        status="pass",
        detail="API environment variables are configured" + (" and reachable" if check_api else ""),
        data=data,
    )


def _check_data_files(settings: Settings) -> CheckResult:
    required = ("patch_info_pkl", "test_info_pkl", "bug_report_json", "rule_md_path")
    missing_or_empty: list[str] = []
    verified: dict[str, int] = {}

    for key in required:
        path = settings.data[key]
        if path is None or not path.is_file():
            missing_or_empty.append(f"data.{key} missing ({path})")
        elif path.stat().st_size == 0:
            missing_or_empty.append(f"data.{key} empty ({path})")
        else:
            verified[key] = path.stat().st_size

    manifest = settings.data["manifest_json"]
    if manifest is not None:
        if not manifest.is_file() or manifest.stat().st_size == 0:
            missing_or_empty.append(f"data.manifest_json missing/empty ({manifest})")
        else:
            verified["manifest_json"] = manifest.stat().st_size

    pkl_path = settings.data["patch_info_pkl"]
    if pkl_path is not None:
        for directory in _INTERRUPTED_MERGE_DIRS:
            if (pkl_path.parent / directory / "pending_pickle.bin").exists():
                missing_or_empty.append(f"Interrupted pickle merge marker found under {directory}")

    if missing_or_empty:
        return CheckResult(
            name="data_files",
            status="fail",
            detail="; ".join(missing_or_empty),
            data={"verified_sizes": verified, "errors": missing_or_empty},
        )
    return CheckResult(
        name="data_files",
        status="pass",
        detail=f"All {len(verified)} configured input/rule files exist and are non-empty",
        data={"verified_sizes": verified},
    )


def _check_output_safety(settings: Settings) -> CheckResult:
    protected: set[Path] = {settings.config_path, settings.config_path.parent}
    if settings.config_path.parent.name == "config":
        protected.add(settings.config_path.parent.parent)
    if settings.env_path is not None:
        protected.add(settings.env_path)
    for path in settings.data.values():
        if path is not None:
            protected.add(path)

    collisions: list[str] = []
    for label, target in (("output.result_dir", settings.result_dir), ("output.spec_cache_dir", settings.spec_cache_dir)):
        if target in protected:
            collisions.append(f"{label} collides with protected input/config path: {target}")
        elif target.exists() and target.is_file():
            collisions.append(f"{label} points to an existing regular file instead of a directory: {target}")

    if collisions:
        return CheckResult(
            name="output_safety",
            status="fail",
            detail="; ".join(collisions),
            data={"result_dir": str(settings.result_dir), "spec_cache_dir": str(settings.spec_cache_dir)},
        )
    return CheckResult(
        name="output_safety",
        status="pass",
        detail="Output directories are isolated from config and input data",
        data={"result_dir": str(settings.result_dir), "spec_cache_dir": str(settings.spec_cache_dir)},
    )


def _check_python_environment() -> CheckResult:
    py_ver = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
    if sys.version_info < (3, 10):
        return CheckResult(
            name="python_environment",
            status="fail",
            detail=f"Python >= 3.10 required, found {py_ver}",
            data={"python": py_ver},
        )
    availability = {pkg: (importlib.util.find_spec(pkg) is not None) for pkg in _OPTIONAL_PACKAGES}
    missing = [pkg for pkg, ok in availability.items() if not ok]
    if missing:
        return CheckResult(
            name="python_environment",
            status="warn",
            detail=f"Python {py_ver} OK; optional runtime packages missing for later steps: {', '.join(missing)}",
            data={"python": py_ver, "packages": availability, "missing": missing},
        )
    return CheckResult(
        name="python_environment",
        status="pass",
        detail=f"Python {py_ver} and all runtime dependencies detected",
        data={"python": py_ver, "packages": availability, "missing": []},
    )


def _extract_block(text: str, start_marker: str, end_marker: str) -> str:
    start_idx = text.find(start_marker)
    if start_idx == -1:
        return ""
    start_idx += len(start_marker)
    end_idx = text.find(end_marker, start_idx)
    if end_idx == -1:
        return text[start_idx:].strip()
    return text[start_idx:end_idx].strip()


def _check_docker_d4j_lsp(
    settings: Settings,
    effective_mode: str,
    runner: CommandRunner,
) -> CheckResult:
    if effective_mode == "local":
        return CheckResult(
            name="docker_d4j_lsp",
            status="skipped",
            detail="Skipped in local mode; run with `--mode server` on the Linux server to verify Docker, D4J (JDK 8), and jdtls (JDK 17+)",
            data={
                "effective_mode": effective_mode,
                "image_name": settings.docker["image_name"],
                "lsp_mode": settings.lsp["mode"],
            },
        )

    # [SERVER_CONFIG_REQUIRED]: 确认服务器 docker、defects4j 与 lsp 配置指向有效环境与镜像
    errors: list[str] = []
    details: dict[str, Any] = {
        "effective_mode": effective_mode,
        "image_name": settings.docker["image_name"],
        "host_mount_root": settings.docker["host_mount_root"],
        "lsp_enabled": settings.lsp["enabled"],
        "lsp_mode": settings.lsp["mode"],
    }

    host_mount = Path(settings.docker["host_mount_root"])
    if not host_mount.is_dir():
        errors.append(f"docker.host_mount_root directory does not exist: {host_mount}")

    rc, _, err = runner(["docker", "info"], 15)
    if rc != 0:
        errors.append(f"Docker daemon check failed (rc={rc}): {err.strip() or 'unavailable'}")
    else:
        rc, _, err = runner(["docker", "image", "inspect", settings.docker["image_name"]], 15)
        if rc != 0:
            errors.append(f"Docker image missing ({settings.docker['image_name']}): {err.strip() or f'rc={rc}'}")
        else:
            d4j_bin = f"{settings.defects4j['d4j_home'].rstrip('/')}/framework/bin/defects4j"
            java8_bin = f"{settings.defects4j['java8_home'].rstrip('/')}/bin/java"
            probe_parts = [
                f"if [ -x {shlex.quote(d4j_bin)} ]; then echo 'D4J_BIN=OK'; else echo 'D4J_BIN=MISSING'; fi",
                "if command -v rg >/dev/null 2>&1 || command -v grep >/dev/null 2>&1; then echo 'GREP_BIN=OK'; else echo 'GREP_BIN=MISSING'; fi",
                "echo 'JAVA8_VER_START'",
                f"{shlex.quote(java8_bin)} -version 2>&1 || true",
                "echo 'JAVA8_VER_END'",
            ]
            if settings.lsp["enabled"] and settings.lsp["mode"] == "docker_jdtls":
                jdtls_home = settings.lsp["jdtls_home"]
                jdtls_cfg = settings.lsp["jdtls_config_dir"]
                jdtls_java_bin = f"{settings.lsp['jdtls_java_home'].rstrip('/')}/bin/java"
                probe_parts.extend(
                    [
                        f"if [ -d {shlex.quote(jdtls_home)} ]; then echo 'JDTLS_HOME=OK'; else echo 'JDTLS_HOME=MISSING'; fi",
                        f"if [ -d {shlex.quote(jdtls_cfg)} ]; then echo 'JDTLS_CFG=OK'; else echo 'JDTLS_CFG=MISSING'; fi",
                        "echo 'JDTLS_JAVA_VER_START'",
                        f"{shlex.quote(jdtls_java_bin)} -version 2>&1 || true",
                        "echo 'JDTLS_JAVA_VER_END'",
                    ]
                )
            probe_cmd = "; ".join(probe_parts)
            rc, out, err = runner(
                ["docker", "run", "--rm", "--entrypoint", "sh", settings.docker["image_name"], "-c", probe_cmd],
                30,
            )
            combined = f"{out}\n{err}"
            if rc != 0:
                errors.append(f"Container probe failed (rc={rc}): {err.strip() or out.strip()}")
            else:
                if "D4J_BIN=OK" not in combined:
                    errors.append(f"Defects4J executable not found inside container: {d4j_bin}")
                if "GREP_BIN=OK" not in combined:
                    errors.append("Neither rg nor grep is available inside container for repo search tools")
                java8_block = _extract_block(combined, "JAVA8_VER_START", "JAVA8_VER_END")
                java8_major = parse_java_major(java8_block)
                details["d4j_java_major"] = java8_major
                if java8_major != 8:
                    errors.append(f"Defects4J Java inside container must be major 8, got {java8_major}")

                if settings.lsp["enabled"] and settings.lsp["mode"] == "docker_jdtls":
                    if "JDTLS_HOME=OK" not in combined:
                        errors.append(f"jdtls_home directory missing inside container: {settings.lsp['jdtls_home']}")
                    if "JDTLS_CFG=OK" not in combined:
                        errors.append(
                            f"jdtls_config_dir directory missing inside container: {settings.lsp['jdtls_config_dir']}"
                        )
                    jdtls_block = _extract_block(combined, "JDTLS_JAVA_VER_START", "JDTLS_JAVA_VER_END")
                    jdtls_major = parse_java_major(jdtls_block)
                    details["jdtls_java_major"] = jdtls_major
                    if jdtls_major is None or jdtls_major < 17:
                        errors.append(f"jdtls Java inside container must be >= 17, got {jdtls_major}")

    if settings.lsp["enabled"] and settings.lsp["mode"] == "local_jdtls":
        for key in ("jdtls_home", "jdtls_config_dir"):
            if not Path(settings.lsp[key]).is_dir():
                errors.append(f"lsp.{key} directory missing on host: {settings.lsp[key]}")
        jdtls_java_bin = str(Path(settings.lsp["jdtls_java_home"]) / "bin" / "java")
        rc, out, err = runner([jdtls_java_bin, "-version"], 15)
        if rc != 0:
            errors.append(f"Failed to run local jdtls Java ({jdtls_java_bin}): {err.strip() or out.strip()}")
        else:
            jdtls_major = parse_java_major(f"{out}\n{err}")
            details["jdtls_java_major"] = jdtls_major
            if jdtls_major is None or jdtls_major < 17:
                errors.append(f"local jdtls Java must be >= 17, got {jdtls_major}")

    if errors:
        return CheckResult(
            name="docker_d4j_lsp",
            status="fail",
            detail="; ".join(errors),
            data=details,
        )
    return CheckResult(
        name="docker_d4j_lsp",
        status="pass",
        detail="Docker image, host mount, grep, Defects4J (JDK 8), and jdtls (JDK 17+) checks passed",
        data=details,
    )


def _check_llm_config(settings: Settings) -> CheckResult:
    llm_path = settings.llm_config_path
    if llm_path is None:
        return CheckResult(
            name="llm_config",
            status="warn",
            detail="api.llm_config_path is not configured; using flat api section defaults",
            data={},
        )
    try:
        bundle = load_llm_config(llm_path)
    except ConfigError as exc:
        return CheckResult(
            name="llm_config",
            status="fail",
            detail=f"Invalid LLM config ({llm_path}): {exc}",
            data={"llm_config_path": str(llm_path)},
        )
    default_prof = bundle.get_profile()
    missing_envs: list[str] = []
    if not settings.get_env(default_prof.api_key_env):
        missing_envs.append(default_prof.api_key_env)
    prof_url = settings.get_env(default_prof.base_url_env)
    if validate_api_url(prof_url) is not None:
        missing_envs.append(default_prof.base_url_env)
    if not default_prof.model_name and not settings.get_env(default_prof.model_env):
        missing_envs.append(default_prof.model_env)

    return CheckResult(
        name="llm_config",
        status="pass",
        detail=f"Loaded LLM config with {len(bundle.profiles)} profiles (default={bundle.default_profile})",
        data={
            "llm_config_path": str(bundle.config_path),
            "default_profile": bundle.default_profile,
            "default_context_window": default_prof.context_window,
            "default_timeout_seconds": default_prof.timeout_seconds,
            "profiles": sorted(bundle.profiles),
            "role_overrides": bundle.role_overrides,
            "missing_default_profile_envs": missing_envs,
        },
    )


def _check_markers(package_root: Path) -> CheckResult:
    markers = scan_server_config_markers(package_root)
    return CheckResult(
        name="server_config_markers",
        status="pass" if markers else "warn",
        detail=f"Found {len(markers)} # [SERVER_CONFIG_REQUIRED] reminder(s)",
        data={"count": len(markers), "markers": markers},
    )


def run_doctor(
    settings: Settings,
    mode: str = "auto",
    check_api: bool = False,
    runner: CommandRunner | None = None,
    http_probe: HttpProbe | None = None,
    package_root: Path | None = None,
) -> dict[str, Any]:
    """Run read-only preflight checks and return a structured report dictionary."""
    if mode not in {"auto", "local", "server"}:
        raise ValueError(f"Unsupported doctor mode: {mode}")

    effective_mode = "local" if (mode == "local" or (mode == "auto" and os.name == "nt")) else "server"
    active_runner = runner or default_runner
    active_probe = http_probe or default_http_probe
    active_root = package_root or Path(__file__).resolve().parents[1]

    checks = [
        _check_api_credentials(settings, check_api=check_api, http_probe=active_probe),
        _check_llm_config(settings),
        _check_data_files(settings),
        _check_output_safety(settings),
        _check_python_environment(),
        _check_docker_d4j_lsp(settings, effective_mode=effective_mode, runner=active_runner),
        _check_markers(active_root),
    ]

    overall_ok = all(item.status != "fail" for item in checks)
    return {
        "overall_ok": overall_ok,
        "mode_requested": mode,
        "effective_mode": effective_mode,
        "config_path": str(settings.config_path),
        "env_file_loaded": str(settings.env_path) if settings.env_path else None,
        "target_tier": settings.pipeline["target_tier"],
        "tier_b_enabled": settings.pipeline["enable_cross_rep_tier_b"],
        "checks": [asdict(item) for item in checks],
    }
