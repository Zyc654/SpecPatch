"""Configuration, .env loading, safe pickle/JSON I/O, and path validation for SpecPatchGuard.

Primitives (`PlainUnpickler`, `parse_json`, `read_json`, `read_snapshot`,
`atomic_json`, `digest_file`, `path_key`, `public_api_settings`, `safe_error`)
are adapted from `furina-main/furina_compat.py` so SpecPatchGuard remains
self-contained when deployed to the Linux server.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import io
import json
import math
import os
from pathlib import Path
import pickle
import re
import tempfile
from typing import Any
from urllib.parse import urlparse

try:
    from dotenv import dotenv_values as _dotenv_values
except ImportError:  # pragma: no cover - fallback parser covered in unit tests
    _dotenv_values = None


class ConfigError(ValueError):
    """Raised when configuration, .env, or serialized input fails validation."""


CompatibilityError = ConfigError

_TOP_SECTIONS = {"data", "api", "docker", "defects4j", "lsp", "tools", "output", "pipeline"}
_DATA_KEYS = {
    "patch_info_pkl",
    "test_info_pkl",
    "bug_report_json",
    "manifest_json",
    "rule_md_path",
}
_API_REQUIRED_KEYS = {
    "base_url_env",
    "api_key_env",
    "model_env",
    "temperature",
    "max_tokens",
    "timeout_seconds",
    "max_retries",
}
_API_ALLOWED_KEYS = _API_REQUIRED_KEYS | {"extra_body", "llm_config_path"}
_FORBIDDEN_EXTRA_BODY_KEYS = {"model", "messages", "tools", "tool_choice", "stream"}
_DOCKER_KEYS = {
    "image_name",
    "container_work_root",
    "host_mount_root",
    "timeout_seconds",
    "nano_cpus",
}
_DEFECTS4J_KEYS = {
    "d4j_home",
    "java8_home",
    "compile_timeout_sec",
    "test_timeout_sec",
}
_LSP_KEYS = {
    "enabled",
    "mode",
    "jdtls_home",
    "jdtls_java_home",
    "jdtls_config_dir",
    "jdtls_workspace_cache",
    "init_timeout_sec",
    "request_timeout_sec",
}
_LSP_MODES = {"docker_jdtls", "local_jdtls", "disabled"}
_TOOLS_KEYS = {
    "grep_default_context_before",
    "grep_default_context_after",
    "grep_max_context_lines",
    "grep_max_matches",
    "read_default_context_before",
    "read_default_context_after",
    "read_max_lines",
    "branch_context_lines",
    "max_tool_calls_per_agent",
    "max_tool_output_chars",
    "command_timeout_sec",
}
_OUTPUT_REQUIRED_KEYS = {"result_dir", "spec_cache_dir", "resume"}
_OUTPUT_OPTIONAL_KEYS = {"force_refresh"}
_OUTPUT_ALLOWED_KEYS = _OUTPUT_REQUIRED_KEYS | _OUTPUT_OPTIONAL_KEYS
_OUTPUT_KEYS = _OUTPUT_REQUIRED_KEYS
_PIPELINE_REQUIRED_KEYS = {
    "target_tier",
    "enable_cross_rep_tier_a",
    "enable_cross_rep_tier_b",
    "enable_anchor_infection_probes",
    "pbt_refine_max_rounds",
    "syntax_fix_max_rounds",
    "hypothesis_max_examples",
}
_PIPELINE_OPTIONAL_KEYS = {
    "max_tier_b_predicates",
    "tier_b_predicate_proposers",
    "enable_tier_c_nonexec_python",
    "calibration_min_passing_agreement",
    "calibration_min_passing_samples",
    "enable_patch_targeted_disagreement",
    "max_disagreement_probes",
}
_PIPELINE_ALLOWED_KEYS = _PIPELINE_REQUIRED_KEYS | _PIPELINE_OPTIONAL_KEYS
_PIPELINE_KEYS = _PIPELINE_REQUIRED_KEYS
_PIPELINE_TIERS = {"tier_a", "tier_b", "tier_c", "auto"}
_ENV_VAR_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class PlainUnpickler(pickle.Unpickler):
    """Restrict pickle deserialization to built-in Python primitives only."""

    def find_class(self, module: str, name: str) -> Any:
        raise ConfigError(f"Unsupported pickle object: {module}.{name}")


def parse_json(content: bytes) -> Any:
    """Decode UTF-8 (with optional BOM) JSON, including double-encoded JSON strings."""
    try:
        value = json.loads(content.decode("utf-8-sig"))
        if isinstance(value, str):
            value = json.loads(value)
        return value
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ConfigError(f"Invalid JSON content: {exc}") from None


def read_json(path: str | Path) -> Any:
    """Read and parse a JSON file safely."""
    try:
        return parse_json(Path(path).read_bytes())
    except OSError as exc:
        raise ConfigError(f"Cannot read JSON file {path}: {exc}") from None


def read_snapshot(path: str | Path, parser: Any) -> tuple[Any, str]:
    """Read bytes once, parse with `parser`, and return `(parsed, sha256)`."""
    content = Path(path).read_bytes()
    return parser(content), hashlib.sha256(content).hexdigest()


def atomic_json(value: Any, path: str | Path, *args: Any, **kwargs: Any) -> None:
    """Atomically write JSON to `path` via temp file + fsync + replace."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=target.name + ".", dir=target.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def digest_file(path: str | Path) -> str:
    """Compute SHA-256 hex digest of a file."""
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def path_key(value: str | Path) -> str:
    """Normalize slash separators for cross-platform path keys."""
    return str(value).replace("\\", "/").removeprefix("./")


def parse_pickle(content: bytes) -> Any:
    """Deserialize pickle bytes using `PlainUnpickler`."""
    try:
        return PlainUnpickler(io.BytesIO(content)).load()
    except ConfigError:
        raise
    except Exception as exc:
        raise ConfigError(f"Invalid pickle content: {exc}") from None


def load_pickle(path: str | Path) -> Any:
    """Load a pickle file safely with `PlainUnpickler`."""
    try:
        return parse_pickle(Path(path).read_bytes())
    except OSError as exc:
        raise ConfigError(f"Cannot read pickle file {path}: {exc}") from None


def safe_error(exc: Exception, action: str) -> str:
    """Format an exception without leaking URLs, request payloads, or secrets."""
    if isinstance(exc, ConfigError):
        return str(exc)
    status = getattr(exc, "status_code", None)
    details = type(exc).__name__
    if type(status) is int and 100 <= status <= 599:
        details += f", HTTP {status}"
    return f"{action} ({details}); provider/request details omitted"


def _reject_control_newlines(field_name: str, value: str) -> None:
    if "\n" in value or "\r" in value or "\x00" in value:
        raise ConfigError(f"{field_name} cannot contain newline or NUL characters")


def resolve_local_path(base: Path, value: Any, field_name: str = "path") -> Path | None:
    """Resolve a host-side path relative to `base` without corrupting POSIX server paths on Windows."""
    if not isinstance(value, str):
        raise ConfigError(f"{field_name} must be a string")
    _reject_control_newlines(field_name, value)
    stripped = value.strip()
    if not stripped:
        return None
    expanded = os.path.expandvars(os.path.expanduser(stripped))
    _reject_control_newlines(field_name, expanded)
    if os.name == "nt" and expanded.startswith("/"):
        # Preserve Linux POSIX paths (configured for server deployment) verbatim on Windows.
        return Path(expanded)
    path = Path(expanded)
    return path.resolve() if path.is_absolute() else (base / path).resolve()


def validate_server_path(
    field_name: str,
    value: Any,
    allow_blank: bool = False,
    require_posix_abs: bool = False,
) -> str:
    """Validate a Linux server/container path string without altering POSIX separators."""
    if not isinstance(value, str):
        raise ConfigError(f"{field_name} must be a string")
    _reject_control_newlines(field_name, value)
    stripped = value.strip()
    if not stripped:
        if allow_blank:
            return ""
        raise ConfigError(f"{field_name} cannot be blank")
    if require_posix_abs and not stripped.startswith("/"):
        raise ConfigError(f"{field_name} must be an absolute POSIX path starting with '/'")
    return stripped


def _parse_env_lines(text: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].strip()
        if "=" not in line:
            continue
        key, val = line.split("=", 1)
        key = key.strip()
        val = val.strip()
        if not key:
            continue
        if len(val) >= 2 and val[0] == val[-1] and val[0] in {"'", '"'}:
            val = val[1:-1]
        elif "#" in val:
            # Strip unquoted inline comments preceded by whitespace.
            val = re.split(r"\s+#", val, maxsplit=1)[0].strip()
        values[key] = val
    return values


def load_env_file(env_path: Path) -> dict[str, str]:
    """Read key/value pairs from `.env` without mutating `os.environ`."""
    try:
        if _dotenv_values is not None:
            raw = _dotenv_values(env_path, encoding="utf-8-sig")
            return {
                str(k): ("" if v is None else str(v))
                for k, v in raw.items()
                if k is not None
            }
        text = env_path.read_text(encoding="utf-8-sig")
        return _parse_env_lines(text)
    except OSError as exc:
        raise ConfigError(f"Cannot read .env file {env_path}: {exc}") from None


def validate_api_url(url: str) -> str | None:
    """Return an error reason if `url` is missing or not a clean HTTP(S) endpoint."""
    if not url or not url.strip():
        return "API base URL is empty"
    parsed = urlparse(url.strip())
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return "API base URL must be an HTTP(S) URL with a valid host"
    if parsed.username or parsed.password:
        return "API base URL must not embed credentials"
    return None


def _require_bool(field_name: str, value: Any) -> bool:
    if type(value) is not bool:
        raise ConfigError(f"{field_name} must be a JSON boolean")
    return value


def _require_int(field_name: str, value: Any, minimum: int) -> int:
    if type(value) is not int or value < minimum:
        raise ConfigError(f"{field_name} must be an integer >= {minimum}")
    return value


def _require_finite_number(field_name: str, value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigError(f"{field_name} must be numeric")
    numeric = float(value)
    if not math.isfinite(numeric):
        raise ConfigError(f"{field_name} must be a finite number")
    return numeric


def _require_env_var_name(field_name: str, value: Any) -> str:
    if not isinstance(value, str) or not _ENV_VAR_RE.fullmatch(value.strip()):
        raise ConfigError(f"{field_name} must be a valid environment variable identifier")
    return value.strip()


@dataclass
class Settings:
    """Validated SpecPatchGuard settings with secret-safe accessors."""

    config_path: Path
    env_path: Path | None
    data: dict[str, Path | None]
    api: dict[str, Any]
    docker: dict[str, Any]
    defects4j: dict[str, Any]
    lsp: dict[str, Any]
    tools: dict[str, Any]
    output: dict[str, Any]
    pipeline: dict[str, Any]
    _env_values: dict[str, str] = field(default_factory=dict, repr=False)

    def get_env(self, var_name: str) -> str:
        """Resolve an environment variable with OS environment taking precedence over `.env`.

        Note: If `var_name` is explicitly exported in OS `os.environ` (even as an empty string `""`),
        it intentionally shadows `.env`. Unset empty shell variables on the server if `.env` should apply.
        """
        if var_name in os.environ:
            return os.environ[var_name].strip()
        return self._env_values.get(var_name, "").strip()

    def api_key(self) -> str:
        return self.get_env(self.api["api_key_env"])

    def api_base_url(self) -> str:
        return self.get_env(self.api["base_url_env"])

    def api_model(self) -> str:
        return self.get_env(self.api["model_env"])

    @property
    def result_dir(self) -> Path:
        return self.output["result_dir"]

    @property
    def spec_cache_dir(self) -> Path:
        return self.output["spec_cache_dir"]

    @property
    def resume(self) -> bool:
        return self.output["resume"]

    @property
    def force_refresh(self) -> bool:
        return bool(self.output.get("force_refresh", False))

    @property
    def llm_config_path(self) -> Path | None:
        return self.api.get("llm_config_path")


def load_settings(path: str | Path, env_path: str | Path | None = None) -> Settings:
    """Load and strictly validate `config.json` and optional `.env`."""
    config_file = Path(path).resolve()
    if not config_file.is_file():
        raise ConfigError(f"Configuration file not found: {config_file}")

    raw = read_json(config_file)
    if not isinstance(raw, dict) or set(raw) != _TOP_SECTIONS:
        missing = sorted(_TOP_SECTIONS - set(raw)) if isinstance(raw, dict) else sorted(_TOP_SECTIONS)
        extra = sorted(set(raw) - _TOP_SECTIONS) if isinstance(raw, dict) else []
        raise ConfigError(
            f"Configuration must contain exact sections {sorted(_TOP_SECTIONS)}; "
            f"missing={missing}, extra={extra}"
        )

    for section in _TOP_SECTIONS:
        if not isinstance(raw[section], dict):
            raise ConfigError(f"Configuration section '{section}' must be a JSON object")

    base_dir = config_file.parent

    # [SERVER_CONFIG_REQUIRED]: 确认服务器 config/config.json -> data 指向有效预处理 pkl/json/rule.md 路径
    raw_data = raw["data"]
    if set(raw_data) != _DATA_KEYS:
        raise ConfigError(f"data section must contain exact keys {sorted(_DATA_KEYS)}")
    data = {
        key: resolve_local_path(base_dir, raw_data[key], field_name=f"data.{key}")
        for key in _DATA_KEYS
    }

    raw_api = raw["api"]
    if not (_API_REQUIRED_KEYS <= set(raw_api) <= _API_ALLOWED_KEYS):
        raise ConfigError(
            f"api section must contain {sorted(_API_REQUIRED_KEYS)} "
            f"(optional: ['extra_body', 'llm_config_path'])"
        )
    extra_body = raw_api.get("extra_body", {})
    if not isinstance(extra_body, dict):
        raise ConfigError("api.extra_body must be a JSON object")
    if set(extra_body) & _FORBIDDEN_EXTRA_BODY_KEYS:
        raise ConfigError("api.extra_body cannot override model, messages, tools, tool_choice, or stream")
    temperature = _require_finite_number("api.temperature", raw_api["temperature"])
    if not 0.0 <= temperature <= 2.0:
        raise ConfigError("api.temperature must be in [0, 2]")
    timeout_seconds = _require_finite_number("api.timeout_seconds", raw_api["timeout_seconds"])
    if timeout_seconds <= 0.0:
        raise ConfigError("api.timeout_seconds must be > 0")
    llm_config_path: Path | None = None
    if "llm_config_path" in raw_api:
        llm_config_path = resolve_local_path(
            base_dir, raw_api["llm_config_path"], field_name="api.llm_config_path"
        )
    api = {
        "base_url_env": _require_env_var_name("api.base_url_env", raw_api["base_url_env"]),
        "api_key_env": _require_env_var_name("api.api_key_env", raw_api["api_key_env"]),
        "model_env": _require_env_var_name("api.model_env", raw_api["model_env"]),
        "llm_config_path": llm_config_path,
        "temperature": temperature,
        "max_tokens": _require_int("api.max_tokens", raw_api["max_tokens"], 1),
        "timeout_seconds": timeout_seconds,
        "max_retries": _require_int("api.max_retries", raw_api["max_retries"], 0),
        "extra_body": dict(extra_body),
    }

    # [SERVER_CONFIG_REQUIRED]: 确认服务器 config/config.json -> docker 镜像名与宿主机挂载根目录
    raw_docker = raw["docker"]
    if set(raw_docker) != _DOCKER_KEYS:
        raise ConfigError(f"docker section must contain exact keys {sorted(_DOCKER_KEYS)}")
    image_name = raw_docker["image_name"]
    if not isinstance(image_name, str) or not image_name.strip() or any(ch.isspace() for ch in image_name.strip()):
        raise ConfigError("docker.image_name must be a non-empty image tag without whitespace")
    host_mount_path = resolve_local_path(
        base_dir, raw_docker["host_mount_root"], field_name="docker.host_mount_root"
    )
    if host_mount_path is None:
        raise ConfigError("docker.host_mount_root cannot be blank")
    docker = {
        "image_name": image_name.strip(),
        "container_work_root": validate_server_path(
            "docker.container_work_root", raw_docker["container_work_root"], require_posix_abs=True
        ),
        "host_mount_root": host_mount_path.as_posix(),
        "timeout_seconds": _require_int("docker.timeout_seconds", raw_docker["timeout_seconds"], 1),
        "nano_cpus": _require_int("docker.nano_cpus", raw_docker["nano_cpus"], 1),
    }

    # [SERVER_CONFIG_REQUIRED]: 确认服务器 config/config.json -> defects4j.d4j_home 与 java8_home (JDK 8)
    raw_d4j = raw["defects4j"]
    if set(raw_d4j) != _DEFECTS4J_KEYS:
        raise ConfigError(f"defects4j section must contain exact keys {sorted(_DEFECTS4J_KEYS)}")
    defects4j = {
        "d4j_home": validate_server_path("defects4j.d4j_home", raw_d4j["d4j_home"], allow_blank=False),
        "java8_home": validate_server_path("defects4j.java8_home", raw_d4j["java8_home"], allow_blank=False),
        "compile_timeout_sec": _require_int("defects4j.compile_timeout_sec", raw_d4j["compile_timeout_sec"], 1),
        "test_timeout_sec": _require_int("defects4j.test_timeout_sec", raw_d4j["test_timeout_sec"], 1),
    }

    # [SERVER_CONFIG_REQUIRED]: 确认服务器 config/config.json -> lsp.jdtls_home 与 jdtls_java_home (JDK 17+)
    raw_lsp = raw["lsp"]
    if set(raw_lsp) != _LSP_KEYS:
        raise ConfigError(f"lsp section must contain exact keys {sorted(_LSP_KEYS)}")
    lsp_enabled = _require_bool("lsp.enabled", raw_lsp["enabled"])
    lsp_mode = raw_lsp["mode"]
    if not isinstance(lsp_mode, str) or lsp_mode not in _LSP_MODES:
        raise ConfigError(f"lsp.mode must be one of {sorted(_LSP_MODES)}")
    if lsp_enabled and lsp_mode == "disabled":
        raise ConfigError("lsp.enabled cannot be true when lsp.mode is 'disabled'")
    lsp = {
        "enabled": lsp_enabled,
        "mode": lsp_mode,
        "jdtls_home": validate_server_path("lsp.jdtls_home", raw_lsp["jdtls_home"], allow_blank=not lsp_enabled),
        "jdtls_java_home": validate_server_path("lsp.jdtls_java_home", raw_lsp["jdtls_java_home"], allow_blank=not lsp_enabled),
        "jdtls_config_dir": validate_server_path("lsp.jdtls_config_dir", raw_lsp["jdtls_config_dir"], allow_blank=not lsp_enabled),
        "jdtls_workspace_cache": validate_server_path(
            "lsp.jdtls_workspace_cache", raw_lsp["jdtls_workspace_cache"], allow_blank=not lsp_enabled
        ),
        "init_timeout_sec": _require_int("lsp.init_timeout_sec", raw_lsp["init_timeout_sec"], 1),
        "request_timeout_sec": _require_int("lsp.request_timeout_sec", raw_lsp["request_timeout_sec"], 1),
    }

    raw_tools = raw["tools"]
    if set(raw_tools) != _TOOLS_KEYS:
        raise ConfigError(f"tools section must contain exact keys {sorted(_TOOLS_KEYS)}")
    grep_ctx_before = _require_int("tools.grep_default_context_before", raw_tools["grep_default_context_before"], 0)
    grep_ctx_after = _require_int("tools.grep_default_context_after", raw_tools["grep_default_context_after"], 0)
    grep_max_ctx = _require_int("tools.grep_max_context_lines", raw_tools["grep_max_context_lines"], 0)
    if grep_max_ctx < max(grep_ctx_before, grep_ctx_after):
        raise ConfigError(
            "tools.grep_max_context_lines must be >= max(grep_default_context_before, grep_default_context_after)"
        )
    tools = {
        "grep_default_context_before": grep_ctx_before,
        "grep_default_context_after": grep_ctx_after,
        "grep_max_context_lines": grep_max_ctx,
        "grep_max_matches": _require_int("tools.grep_max_matches", raw_tools["grep_max_matches"], 1),
        "read_default_context_before": _require_int(
            "tools.read_default_context_before", raw_tools["read_default_context_before"], 0
        ),
        "read_default_context_after": _require_int(
            "tools.read_default_context_after", raw_tools["read_default_context_after"], 0
        ),
        "read_max_lines": _require_int("tools.read_max_lines", raw_tools["read_max_lines"], 1),
        "branch_context_lines": _require_int("tools.branch_context_lines", raw_tools["branch_context_lines"], 0),
        "max_tool_calls_per_agent": _require_int(
            "tools.max_tool_calls_per_agent", raw_tools["max_tool_calls_per_agent"], 1
        ),
        "max_tool_output_chars": _require_int(
            "tools.max_tool_output_chars", raw_tools["max_tool_output_chars"], 1000
        ),
        "command_timeout_sec": _require_int(
            "tools.command_timeout_sec", raw_tools["command_timeout_sec"], 1
        ),
    }

    raw_output = raw["output"]
    if not (_OUTPUT_REQUIRED_KEYS <= set(raw_output) <= _OUTPUT_ALLOWED_KEYS):
        raise ConfigError(
            f"output section must contain {sorted(_OUTPUT_REQUIRED_KEYS)} "
            f"(optional: {sorted(_OUTPUT_OPTIONAL_KEYS)})"
        )
    result_dir = resolve_local_path(base_dir, raw_output["result_dir"], field_name="output.result_dir")
    spec_cache_dir = resolve_local_path(base_dir, raw_output["spec_cache_dir"], field_name="output.spec_cache_dir")
    if result_dir is None or spec_cache_dir is None:
        raise ConfigError("output.result_dir and output.spec_cache_dir cannot be blank")
    force_refresh = (
        _require_bool("output.force_refresh", raw_output["force_refresh"])
        if "force_refresh" in raw_output
        else False
    )
    output = {
        "result_dir": result_dir,
        "spec_cache_dir": spec_cache_dir,
        "resume": _require_bool("output.resume", raw_output["resume"]),
        "force_refresh": force_refresh,
    }

    raw_pipeline = raw["pipeline"]
    if not (_PIPELINE_REQUIRED_KEYS <= set(raw_pipeline) <= _PIPELINE_ALLOWED_KEYS):
        raise ConfigError(
            f"pipeline section must contain {sorted(_PIPELINE_REQUIRED_KEYS)} "
            f"(optional: {sorted(_PIPELINE_OPTIONAL_KEYS)})"
        )
    target_tier = raw_pipeline["target_tier"]
    if not isinstance(target_tier, str) or target_tier not in _PIPELINE_TIERS:
        raise ConfigError(f"pipeline.target_tier must be one of {sorted(_PIPELINE_TIERS)}")
    max_tier_b_predicates = (
        _require_int("pipeline.max_tier_b_predicates", raw_pipeline["max_tier_b_predicates"], 1)
        if "max_tier_b_predicates" in raw_pipeline
        else 6
    )
    if max_tier_b_predicates > 6:
        raise ConfigError("pipeline.max_tier_b_predicates must be <= 6")
    calibration_min_agreement = (
        _require_finite_number(
            "pipeline.calibration_min_passing_agreement",
            raw_pipeline["calibration_min_passing_agreement"],
        )
        if "calibration_min_passing_agreement" in raw_pipeline
        else 0.8
    )
    if not 0.0 <= calibration_min_agreement <= 1.0:
        raise ConfigError("pipeline.calibration_min_passing_agreement must be in [0, 1]")

    pipeline = {
        "target_tier": target_tier,
        "enable_cross_rep_tier_a": _require_bool(
            "pipeline.enable_cross_rep_tier_a", raw_pipeline["enable_cross_rep_tier_a"]
        ),
        "enable_cross_rep_tier_b": _require_bool(
            "pipeline.enable_cross_rep_tier_b", raw_pipeline["enable_cross_rep_tier_b"]
        ),
        "enable_anchor_infection_probes": _require_bool(
            "pipeline.enable_anchor_infection_probes", raw_pipeline["enable_anchor_infection_probes"]
        ),
        "pbt_refine_max_rounds": _require_int(
            "pipeline.pbt_refine_max_rounds", raw_pipeline["pbt_refine_max_rounds"], 1
        ),
        "syntax_fix_max_rounds": _require_int(
            "pipeline.syntax_fix_max_rounds", raw_pipeline["syntax_fix_max_rounds"], 1
        ),
        "hypothesis_max_examples": _require_int(
            "pipeline.hypothesis_max_examples", raw_pipeline["hypothesis_max_examples"], 1
        ),
        "max_tier_b_predicates": max_tier_b_predicates,
        "tier_b_predicate_proposers": (
            _require_int("pipeline.tier_b_predicate_proposers", raw_pipeline["tier_b_predicate_proposers"], 1)
            if "tier_b_predicate_proposers" in raw_pipeline
            else 2
        ),
        "enable_tier_c_nonexec_python": (
            _require_bool("pipeline.enable_tier_c_nonexec_python", raw_pipeline["enable_tier_c_nonexec_python"])
            if "enable_tier_c_nonexec_python" in raw_pipeline
            else False
        ),
        "calibration_min_passing_agreement": calibration_min_agreement,
        "calibration_min_passing_samples": (
            _require_int(
                "pipeline.calibration_min_passing_samples",
                raw_pipeline["calibration_min_passing_samples"],
                1,
            )
            if "calibration_min_passing_samples" in raw_pipeline
            else 2
        ),
        "enable_patch_targeted_disagreement": (
            _require_bool(
                "pipeline.enable_patch_targeted_disagreement",
                raw_pipeline["enable_patch_targeted_disagreement"],
            )
            if "enable_patch_targeted_disagreement" in raw_pipeline
            else True
        ),
        "max_disagreement_probes": (
            _require_int("pipeline.max_disagreement_probes", raw_pipeline["max_disagreement_probes"], 1)
            if "max_disagreement_probes" in raw_pipeline
            else 5
        ),
    }

    # [SERVER_CONFIG_REQUIRED]: 确认服务器存在 .env（或由环境变量注入 OPENAI_API_KEY / OPENAI_API_BASE / LLM_MODEL）
    resolved_env_path: Path | None = None
    env_values: dict[str, str] = {}
    if env_path is not None:
        explicit_env = Path(env_path).resolve()
        if not explicit_env.is_file():
            raise ConfigError(f"Specified .env file not found: {explicit_env}")
        resolved_env_path = explicit_env
        env_values = load_env_file(explicit_env)
    else:
        default_env = base_dir / ".env"
        if not default_env.is_file() and base_dir.name == "config":
            project_env = base_dir.parent / ".env"
            if project_env.is_file():
                default_env = project_env
        if default_env.is_file():
            resolved_env_path = default_env
            env_values = load_env_file(default_env)

    return Settings(
        config_path=config_file,
        env_path=resolved_env_path,
        data=data,
        api=api,
        docker=docker,
        defects4j=defects4j,
        lsp=lsp,
        tools=tools,
        output=output,
        pipeline=pipeline,
        _env_values=env_values,
    )


def public_api_settings(settings: Settings) -> dict[str, Any]:
    """Return secret-free API configuration metadata for manifests and diagnostics."""
    public = {
        key: (str(value) if isinstance(value, Path) else value)
        for key, value in settings.api.items()
        if key not in {"api_key_env", "base_url_env", "extra_body"}
    }
    base_url = settings.api_base_url()
    sanitized_url = ""
    if base_url:
        parsed = urlparse(base_url)
        if parsed.scheme in {"http", "https"} and parsed.netloc and not parsed.username and not parsed.password:
            sanitized_url = parsed._replace(query="", fragment="").geturl()
    public["base_url"] = sanitized_url
    public["model"] = settings.api_model()
    private_options = {"base_url": base_url, "extra_body": settings.api.get("extra_body", {})}
    public["connection_options_sha256"] = hashlib.sha256(
        json.dumps(private_options, sort_keys=True).encode("utf-8")
    ).hexdigest()
    return public


def configuration_status(settings: Settings) -> dict[str, Any]:
    """Summarize config readiness without exposing credentials or reading big data files."""
    pending: list[str] = []
    for name in ("patch_info_pkl", "test_info_pkl", "bug_report_json", "rule_md_path"):
        if settings.data[name] is None:
            pending.append(f"data.{name}")
    if not settings.api_key():
        pending.append(f"env:{settings.api['api_key_env']}")
    base_url = settings.api_base_url()
    url_err = validate_api_url(base_url)
    if url_err is not None:
        pending.append(f"env:{settings.api['base_url_env']} ({url_err})")
    if not settings.api_model():
        pending.append(f"env:{settings.api['model_env']}")

    return {
        "configuration_valid": True,
        "config_path": str(settings.config_path),
        "env_file_loaded": str(settings.env_path) if settings.env_path else None,
        "target_tier": settings.pipeline["target_tier"],
        "tier_b_enabled": settings.pipeline["enable_cross_rep_tier_b"],
        "lsp_enabled": settings.lsp["enabled"],
        "lsp_mode": settings.lsp["mode"],
        "pending": pending,
        "model_requests_made": 0,
    }
