"""Shared file-level "check-exists-then-reuse" disk cache helpers for Steps 1-5+."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path, PurePosixPath
import re
from typing import Any, Callable, Sequence

from .config_loader import ConfigError, atomic_json, path_key, read_json

_UNSAFE_SEGMENT_RE = re.compile(r"[^A-Za-z0-9._-]+")
_MULTI_UNDERSCORE_RE = re.compile(r"_+")


def sanitize_cache_segment(raw: str) -> str:
    """Sanitize a cache path segment by replacing slashes, `..`, `::`, whitespace, and unsafe chars."""
    if not isinstance(raw, str):
        raise ValueError("Cache segment must be a string")
    replaced = raw.strip().replace("..", "_").replace("::", "_").replace("/", "_").replace("\\", "_")
    replaced = _UNSAFE_SEGMENT_RE.sub("_", replaced)
    collapsed = _MULTI_UNDERSCORE_RE.sub("_", replaced).strip("._")
    return collapsed or "_"


def _extract_attr_or_key(item: Any, key: str, default: Any = "") -> Any:
    if isinstance(item, dict):
        return item.get(key, default)
    return getattr(item, key, default)


def _norm_space(value: Any) -> str:
    return " ".join(str(value or "").split())


def compute_anchor_methods_hash(anchors: Sequence[Any], target_methods: Sequence[Any]) -> str:
    """Compute a deterministic 12-char SHA-256 key over sorted unique methods or fallback anchor spans."""
    if target_methods:
        signatures: set[str] = set()
        for method in target_methods:
            file_path = path_key(_norm_space(_extract_attr_or_key(method, "file_path", "")))
            class_name = _norm_space(_extract_attr_or_key(method, "class_name", ""))
            method_name = _norm_space(_extract_attr_or_key(method, "method_name", ""))
            raw_params = _extract_attr_or_key(method, "parameter_types", []) or []
            param_types = [_norm_space(p) for p in raw_params]
            signatures.add(f"{file_path}::{class_name}.{method_name}({','.join(param_types)})")
        payload_obj: dict[str, Any] = {"mode": "methods", "items": sorted(signatures)}
    else:
        spans: set[tuple[str, str, int, int]] = set()
        for anchor in anchors:
            file_path = path_key(_norm_space(_extract_attr_or_key(anchor, "file_path", "")))
            method_name = _norm_space(_extract_attr_or_key(anchor, "method_name", ""))
            start_line = int(_extract_attr_or_key(anchor, "anchor_start_line", 0))
            end_line = int(_extract_attr_or_key(anchor, "anchor_end_line", 0))
            spans.add((file_path, method_name, start_line, end_line))
        payload_obj = {
            "mode": "anchors",
            "items": [list(entry) for entry in sorted(spans)],
        }

    serialized = json.dumps(payload_obj, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()[:12]


def resolve_cache_path(base_dir: Path | str, category: str, filename: str) -> Path:
    """Resolve `{base_dir}/{category}/{filename}` while rejecting traversal or absolute segments."""
    if not isinstance(category, str) or not category.strip():
        raise ValueError("Cache category must be a non-empty string")
    if not isinstance(filename, str) or not filename.strip():
        raise ValueError("Cache filename must be a non-empty string")
    for label, value in (("category", category), ("filename", filename)):
        if "\n" in value or "\r" in value or "\x00" in value:
            raise ValueError(f"Cache {label} cannot contain newline or NUL characters")

    clean_category = category.strip().replace("\\", "/")
    if clean_category.startswith("/") or re.match(r"^[A-Za-z]:", clean_category):
        raise ValueError(f"Cache category must be relative, got: {category!r}")
    cat_parts = [part for part in PurePosixPath(clean_category).parts if part not in {"", "."}]
    if not cat_parts or any(part == ".." for part in cat_parts):
        raise ValueError(f"Invalid cache category path: {category!r}")

    clean_filename = filename.strip()
    if "/" in clean_filename or "\\" in clean_filename or clean_filename in {".", ".."} or ".." in clean_filename:
        raise ValueError(f"Cache filename must be a single safe filename segment, got: {filename!r}")

    return Path(base_dir).joinpath(*cat_parts, clean_filename)


def load_valid_cache(
    path: Path | str,
    *,
    force_refresh: bool = False,
    validator: Callable[[dict[str, Any]], bool] | None = None,
) -> dict[str, Any] | None:
    """Return cached JSON dict when present and valid; return `None` to trigger regeneration."""
    if force_refresh:
        return None
    target = Path(path)
    if not target.is_file():
        return None
    try:
        data = read_json(target)
    except (ConfigError, OSError):
        return None
    if not isinstance(data, dict):
        return None
    if validator is not None:
        try:
            if not validator(data):
                return None
        except Exception:
            return None
    return data


def save_cache_atomic(path: Path | str, payload: dict[str, Any]) -> Path:
    """Persist a JSON dictionary payload atomically using `atomic_json`."""
    if not isinstance(payload, dict):
        raise TypeError("Cache payload must be a dictionary")
    target = Path(path)
    atomic_json(payload, target)
    return target
