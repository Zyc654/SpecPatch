"""SpecPatchGuard package exports."""

from .core.config_loader import (
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

__version__ = "0.1.0"

__all__ = [
    "__version__",
    "CompatibilityError",
    "ConfigError",
    "PlainUnpickler",
    "Settings",
    "atomic_json",
    "configuration_status",
    "load_pickle",
    "load_settings",
    "parse_json",
    "parse_pickle",
    "public_api_settings",
    "read_json",
    "read_snapshot",
    "safe_error",
]
