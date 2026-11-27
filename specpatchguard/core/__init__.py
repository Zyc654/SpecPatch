"""Core infrastructure layer: configuration, .env, safe I/O, disk caching, and preflight checks."""

from .cache_utils import (
    compute_anchor_methods_hash,
    load_valid_cache,
    resolve_cache_path,
    sanitize_cache_segment,
    save_cache_atomic,
)
from .config_loader import (
    CompatibilityError,
    ConfigError,
    PlainUnpickler,
    Settings,
    atomic_json,
    configuration_status,
    load_pickle,
    load_settings,
    parse_json,
    parse_pickle,
    public_api_settings,
    read_json,
    read_snapshot,
    safe_error,
)
from .doctor import run_doctor
from .llm_config import (
    LLMConfigBundle,
    LLMProfile,
    ThinkingConfig,
    build_chat_request_kwargs,
    load_llm_config,
)

__all__ = [
    "CompatibilityError",
    "ConfigError",
    "LLMConfigBundle",
    "LLMProfile",
    "PlainUnpickler",
    "Settings",
    "ThinkingConfig",
    "atomic_json",
    "build_chat_request_kwargs",
    "compute_anchor_methods_hash",
    "configuration_status",
    "load_llm_config",
    "load_pickle",
    "load_settings",
    "load_valid_cache",
    "parse_json",
    "parse_pickle",
    "public_api_settings",
    "read_json",
    "read_snapshot",
    "resolve_cache_path",
    "run_doctor",
    "safe_error",
    "sanitize_cache_segment",
    "save_cache_atomic",
]
