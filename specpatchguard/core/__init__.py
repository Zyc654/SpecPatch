"""Core infrastructure layer: configuration, .env, safe I/O, and preflight checks."""

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
    "configuration_status",
    "load_llm_config",
    "load_pickle",
    "load_settings",
    "parse_json",
    "parse_pickle",
    "public_api_settings",
    "read_json",
    "read_snapshot",
    "run_doctor",
    "safe_error",
]
