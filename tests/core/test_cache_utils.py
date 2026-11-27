"""Unit tests for shared Step 1 disk-cache utilities (`specpatchguard/core/cache_utils.py`)."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import sys

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from specpatchguard.core.cache_utils import (  # noqa: E402
    compute_anchor_methods_hash,
    load_valid_cache,
    resolve_cache_path,
    sanitize_cache_segment,
    save_cache_atomic,
)


@dataclass
class _DummyMethod:
    file_path: str
    class_name: str
    method_name: str
    parameter_types: list[str]


@dataclass
class _DummyAnchor:
    file_path: str
    method_name: str
    anchor_start_line: int
    anchor_end_line: int


def test_sanitize_cache_segment_normalizes_unsafe_inputs():
    assert sanitize_cache_segment("Lang-1") == "Lang-1"
    assert sanitize_cache_segment("src/main/Foo::bar") == "src_main_Foo_bar"
    assert sanitize_cache_segment("a\\b..c  d*?e") == "a_b_c_d_e"
    assert sanitize_cache_segment("...") == "_"
    assert sanitize_cache_segment("   ") == "_"
    with pytest.raises(ValueError):
        sanitize_cache_segment(123)  # type: ignore[arg-type]


def test_compute_anchor_methods_hash_order_independent_and_fallback():
    m1 = _DummyMethod(
        file_path="src/main/java/org/A.java",
        class_name="A",
        method_name="compute",
        parameter_types=["int", "String"],
    )
    m2 = _DummyMethod(
        file_path=r".\src\main\java\org\B.java",
        class_name="B",
        method_name="helper",
        parameter_types=[],
    )
    # Reversed order + duplicate + Windows vs POSIX separators + dict vs dataclass produce identical 12-char hash
    h_forward = compute_anchor_methods_hash([], [m1, m2])
    h_reversed_dup = compute_anchor_methods_hash(
        [],
        [
            m2,
            {
                "file_path": "src/main/java/org/A.java",
                "class_name": "A",
                "method_name": "compute",
                "parameter_types": ["int", "String"],
            },
            m1,
        ],
    )
    assert len(h_forward) == 12
    assert h_forward == h_reversed_dup

    # Different parameter signature must yield a different hash
    m1_diff = _DummyMethod(
        file_path="src/main/java/org/A.java",
        class_name="A",
        method_name="compute",
        parameter_types=["long", "String"],
    )
    assert compute_anchor_methods_hash([], [m1_diff, m2]) != h_forward

    # Empty target_methods falls back to sorted unique (file_path, method_name, anchor_start_line, anchor_end_line)
    a1 = _DummyAnchor("src/A.java", "", 10, 15)
    a2 = _DummyAnchor(r".\src\B.java", "run", 30, 31)
    hb1 = compute_anchor_methods_hash([a1, a2], [])
    hb2 = compute_anchor_methods_hash(
        [{"file_path": "src/B.java", "method_name": "run", "anchor_start_line": 30, "anchor_end_line": 31}, a1, a1],
        [],
    )
    assert len(hb1) == 12
    assert hb1 == hb2
    assert hb1 != compute_anchor_methods_hash([_DummyAnchor("src/A.java", "", 10, 16), a2], [])


def test_resolve_cache_path_validation(tmp_path: Path):
    target = resolve_cache_path(tmp_path, "intent", "Lang_1__abc__intent_pack.json")
    assert target == tmp_path / "intent" / "Lang_1__abc__intent_pack.json"

    nested = resolve_cache_path(tmp_path, "d4j_meta/sub", "meta.json")
    assert nested == tmp_path / "d4j_meta" / "sub" / "meta.json"

    for bad_cat, bad_file in (
        ("", "a.json"),
        ("../escape", "a.json"),
        ("/abs/cat", "a.json"),
        ("intent", ""),
        ("intent", "../a.json"),
        ("intent", "sub/a.json"),
        ("intent", "sub\\a.json"),
        ("intent\nline", "a.json"),
    ):
        with pytest.raises(ValueError):
            resolve_cache_path(tmp_path, bad_cat, bad_file)


def test_load_valid_cache_and_save_cache_atomic(tmp_path: Path):
    cache_path = resolve_cache_path(tmp_path, "pbt", "Lang_1__123__step5_spec.json")
    assert load_valid_cache(cache_path) is None

    payload = {"status": "ok", "bug_id": "Lang_1", "items": [1, 2, "中文"]}
    written = save_cache_atomic(cache_path, payload)
    assert written == cache_path
    assert cache_path.is_file()

    assert load_valid_cache(cache_path) == payload
    assert load_valid_cache(cache_path, force_refresh=True) is None

    # Validator rejecting degraded payload -> returns None
    degraded_path = resolve_cache_path(tmp_path, "intent", "Lang_1__123__intent_pack.json")
    save_cache_atomic(degraded_path, {"intent_parse_degraded": True, "summary": "fallback"})
    assert (
        load_valid_cache(
            degraded_path,
            validator=lambda d: not d.get("intent_parse_degraded", False),
        )
        is None
    )

    # Validator raising exception -> treated cleanly as invalid cache (None)
    assert load_valid_cache(cache_path, validator=lambda _d: 1 / 0 == 0) is None

    # Corrupted JSON or non-dict JSON -> returns None
    bad_json = tmp_path / "pbt" / "corrupt.json"
    bad_json.write_text("{not valid json", encoding="utf-8")
    assert load_valid_cache(bad_json) is None

    list_json = tmp_path / "pbt" / "list.json"
    list_json.write_text("[1, 2, 3]", encoding="utf-8")
    assert load_valid_cache(list_json) is None

    with pytest.raises(TypeError):
        save_cache_atomic(cache_path, ["not", "a", "dict"])  # type: ignore[arg-type]
