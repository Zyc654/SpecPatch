"""Unit tests for `specpatchguard.data.patch_blind` and `specpatchguard.data.tier_classifier`."""

from __future__ import annotations

import contextlib
import io
import json
from pathlib import Path
import sys

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from specpatchguard import __main__ as cli  # noqa: E402
from specpatchguard.core.config_loader import load_settings  # noqa: E402
from specpatchguard.data import (  # noqa: E402
    BoundaryContext,
    BugReport,
    DiffAnchor,
    FailingTestRecord,
    FurinaDataLoader,
    PatchBlindTask,
    PatchBlindViolationError,
    RawPatchRecord,
    RuleBasedTierClassifier,
    SubPatchSlice,
    assert_patch_blind_isolation,
    build_initial_intent_pack,
    extract_method_signature,
    load_intent_packs_for_dataset,
    parse_unified_diff,
    strip_diff_from_function_slice,
    strip_to_patch_blind,
    verify_patch_blind_isolation,
)


def _make_synthetic_record(
    patch_id: str,
    project: str,
    bug_num: int,
    raw_diff: str,
    sub_patches: list[SubPatchSlice],
) -> RawPatchRecord:
    hunks = parse_unified_diff(raw_diff)
    return RawPatchRecord(
        patch_id=patch_id,
        project=project,
        bug_num=bug_num,
        bug_id=f"{project}-{bug_num}",
        tool="synthetic",
        label=0,
        raw_diff=raw_diff,
        hunks=hunks,
        sub_patches=sub_patches,
        failing_tests=[
            FailingTestRecord(
                test_file="src/test/FooTest.java",
                test_method_name="testBug",
                start_line=10,
                source_code="@Test\npublic void testBug() { assertEquals(1, Foo.calc(0)); }",
                numbered_source="10: @Test\n11: public void testBug() { assertEquals(1, Foo.calc(0)); }",
                failed_line_start=11,
                failed_line_end=11,
                failure_message="expected <1> but was <0>",
            )
        ],
        bug_report=BugReport(bug_id=f"{project}-{bug_num}", title="calc edge case", description="Returns 0", available=True),
    )


def test_strip_diff_from_function_slice_restores_indent_and_drops_additions() -> None:
    slice_text = (
        "public static int calc(int x) {\r\n"
        "    if (x == 0) {\n"
        "+\n"
        "+       if (secretSentinel == 42) return 999;\n"
        "-       return 0;\n"
        "    }\n"
        "    return x;\n"
        "}"
    )
    cleaned = strip_diff_from_function_slice(slice_text)
    joined = "\n".join(cleaned)
    assert "secretSentinel" not in joined
    assert "+" not in joined
    assert "        return 0;" in cleaned


def test_patch_blind_isolation_blocks_leaks_and_handles_add_only_and_fallback() -> None:
    raw_diff = (
        "diff --git a/src/main/java/org/example/Foo.java b/src/main/java/org/example/Foo.java\n"
        "--- a/src/main/java/org/example/Foo.java\n"
        "+++ b/src/main/java/org/example/Foo.java\n"
        "@@ -20,5 +20,8 @@ public class Foo {\n"
        "     public static int calc(int x) {\n"
        "         if (x < 0) {\n"
        "+            if (secretSentinel == 42) {\n"
        "+                return 999;\n"
        "+            }\n"
        "             return -x;\n"
        "         }\n"
        "         return x;\n"
    )
    sub_slice = SubPatchSlice(
        file_path="src/main/java/org/example/Foo.java",
        start_line=20,
        content=(
            "public static int calc(int x) {\n"
            "    if (x < 0) {\n"
            "+       if (secretSentinel == 42) {\n"
            "+           return 999;\n"
            "+       }\n"
            "        return -x;\n"
            "    }\n"
            "    return x;\n"
            "}"
        ),
        is_fallback_diff=False,
    )
    record = _make_synthetic_record("p/Lang_1/patch1.diff", "Lang", 1, raw_diff, [sub_slice])
    task = strip_to_patch_blind(record)

    serialized = json.dumps(task.to_dict())
    assert "secretSentinel" not in serialized
    assert "999" not in serialized
    assert "@@" not in serialized
    assert task.anchors[0].method_name == "calc"
    assert task.anchors[0].kind == "add_only"
    assert task.anchors[0].is_fallback_slice is False
    assert "20: public static int calc(int x) {" in task.anchors[0].numbered_buggy_function

    # Tamper with task to verify assert_patch_blind_isolation catches added lines and diff headers.
    leaked_anchor = DiffAnchor(
        file_path=task.anchors[0].file_path,
        method_name="calc",
        anchor_start_line=21,
        anchor_end_line=21,
        hunk_old_start=20,
        hunk_old_end=24,
        kind="add_only",
        function_start_line=20,
        function_end_line=26,
        buggy_function_code=task.anchors[0].buggy_function_code + "\nif (secretSentinel == 42) {",
        numbered_buggy_function=task.anchors[0].numbered_buggy_function,
        anchor_context_lines="@@ -20,5 +20,8 @@\n+if (secretSentinel == 42) {",
        is_fallback_slice=False,
    )
    leaked_task = PatchBlindTask(
        patch_id=task.patch_id,
        bug_id=task.bug_id,
        project=task.project,
        bug_num=task.bug_num,
        anchors=[leaked_anchor],
        affected_files=task.affected_files,
        failing_tests=task.failing_tests,
        bug_report=task.bug_report,
        is_multi_hunk=False,
        is_multi_file=False,
        has_fallback_anchor=False,
    )
    violations = verify_patch_blind_isolation(leaked_task, record)
    assert len(violations) >= 2
    with pytest.raises(PatchBlindViolationError):
        assert_patch_blind_isolation(leaked_task, record)

    # Fallback slice (start_line == -1) builds anchor_context_lines strictly without '+' lines.
    fallback_record = _make_synthetic_record(
        "p/Cli_10/patch1.diff",
        "Cli",
        10,
        raw_diff,
        [SubPatchSlice(file_path="src/main/java/org/example/Foo.java", start_line=-1, content="+bad", is_fallback_diff=True)],
    )
    fb_task = strip_to_patch_blind(fallback_record)
    assert fb_task.has_fallback_anchor is True
    assert fb_task.anchors[0].buggy_function_code == ""
    assert "secretSentinel" not in json.dumps(fb_task.to_dict())
    assert "21:         if (x < 0) {" in fb_task.anchors[0].anchor_context_lines


def test_extract_method_signature_complex_java() -> None:
    code = (
        "/**\n"
        " * Formats key-value pairs.\n"
        " */\n"
        "@Override\n"
        "@SuppressWarnings({\"unchecked\", \"rawtypes\"})\n"
        "public static <K extends Comparable<K>, V> Map<K, List<V>> mergeMaps(\n"
        "        @Nullable final Map<K, V> primary,\n"
        "        String... extraKeys)\n"
        "        throws IllegalArgumentException, IOException {\n"
        "    return null;\n"
        "}"
    )
    sig = extract_method_signature(code, file_path="src/main/java/org/example/MapUtil.java")
    assert sig is not None
    assert sig.class_name == "MapUtil"
    assert sig.method_name == "mergeMaps"
    assert sig.visibility == "public"
    assert sig.is_static is True
    assert sig.is_constructor is False
    assert sig.return_type == "Map<K, List<V>>"
    assert sig.parameter_types == ["Map<K, V>", "String..."]
    assert sig.parameter_names == ["primary", "extraKeys"]
    assert sig.throws_types == ["IllegalArgumentException", "IOException"]
    assert "Formats key-value pairs." in sig.javadoc

    ctor_code = "public CSVPrinter(final Appendable out, final CSVFormat format) throws IOException {\n    this.out = out;\n}"
    ctor_sig = extract_method_signature(ctor_code, file_path="src/main/java/org/apache/commons/csv/CSVPrinter.java")
    assert ctor_sig is not None
    assert ctor_sig.is_constructor is True
    assert ctor_sig.method_name == "CSVPrinter"
    assert ctor_sig.parameter_types == ["Appendable", "CSVFormat"]


def test_tier_classifier_routing_and_filtering() -> None:
    classifier = RuleBasedTierClassifier()

    # 1. Pure static Lang method -> tier_a
    raw_lang = (
        "--- a/src/main/java/org/apache/commons/lang3/StringUtils.java\n"
        "+++ b/src/main/java/org/apache/commons/lang3/StringUtils.java\n"
        "@@ -100,4 +100,4 @@\n"
        " public static String trimSafe(String str) {\n"
        "-    return str;\n"
        "+    return str == null ? null : str.trim();\n"
        " }\n"
    )
    lang_rec = _make_synthetic_record(
        "p/Lang_33/patch1.diff",
        "Lang",
        33,
        raw_lang,
        [
            SubPatchSlice(
                file_path="src/main/java/org/apache/commons/lang3/StringUtils.java",
                start_line=100,
                content="public static String trimSafe(String str) {\n-   return str;\n+   return str == null ? null : str.trim();\n}",
                is_fallback_diff=False,
            )
        ],
    )
    lang_task = strip_to_patch_blind(lang_rec)
    lang_pack = build_initial_intent_pack(lang_task, classifier)
    assert lang_pack.tier == "tier_a"
    assert classifier.classify(lang_pack) == "tier_a"

    # 1b. Qualified standard Java types (java.lang.String, java.util.List<Integer>) stay Tier-A friendly
    qual_rec = _make_synthetic_record(
        "p/Custom_1/patch1.diff",
        "CustomProj",
        1,
        "--- a/src/Util.java\n+++ b/src/Util.java\n@@ -10,3 +10,3 @@\n public static java.util.List<Integer> parse(java.lang.String s) {\n-    return null;\n+    return java.util.Collections.emptyList();\n }\n",
        [
            SubPatchSlice(
                file_path="src/Util.java",
                start_line=10,
                content="public static java.util.List<Integer> parse(java.lang.String s) {\n-   return null;\n+   return java.util.Collections.emptyList();\n}",
                is_fallback_diff=False,
            )
        ],
    )
    qual_task = strip_to_patch_blind(qual_rec)
    assert classifier.classify_detailed(qual_task).tier == "tier_a"

    # 2. Closure project -> tier_b
    closure_rec = _make_synthetic_record(
        "p/Closure_101/patch1.diff",
        "Closure",
        101,
        raw_lang,
        [
            SubPatchSlice(
                file_path="src/main/java/org/apache/commons/lang3/StringUtils.java",
                start_line=100,
                content="public static String trimSafe(String str) {\n-   return str;\n+   return str;\n}",
                is_fallback_diff=False,
            )
        ],
    )
    closure_task = strip_to_patch_blind(closure_rec)
    closure_pack = build_initial_intent_pack(closure_task, classifier)
    assert closure_pack.tier == "tier_b"

    # 3. Private stateful method -> tier_b
    stateful_diff = (
        "--- a/src/main/java/org/apache/commons/math3/State.java\n"
        "+++ b/src/main/java/org/apache/commons/math3/State.java\n"
        "@@ -10,4 +10,4 @@\n"
        " private void update(int v) {\n"
        "-    this.count = v;\n"
        "+    this.count = v + 1;\n"
        " }\n"
    )
    stateful_rec = _make_synthetic_record(
        "p/Math_5/patch1.diff",
        "Math",
        5,
        stateful_diff,
        [
            SubPatchSlice(
                file_path="src/main/java/org/apache/commons/math3/State.java",
                start_line=10,
                content="private void update(int v) {\n-   this.count = v;\n+   this.count = v + 1;\n}",
                is_fallback_diff=False,
            )
        ],
    )
    stateful_task = strip_to_patch_blind(stateful_rec)
    assert classifier.classify_detailed(stateful_task).tier == "tier_b"

    # 4. Heavy BoundaryContext on an otherwise Tier-A IntentContextPack -> tier_b
    lang_pack.boundary_context = BoundaryContext(
        non_local_state_touches=[{"field": "A"}, {"field": "B"}, {"field": "C"}]
    )
    assert classifier.classify(lang_pack) == "tier_b"

    # 5. filter_tasks supports tier_a, tier_b, auto
    tasks = [lang_task, closure_task, stateful_task]
    assert [t.bug_id for t, _ in classifier.filter_tasks(tasks, "tier_a")] == ["Lang-33"]
    assert [t.bug_id for t, _ in classifier.filter_tasks(tasks, "tier_b")] == ["Closure-101", "Math-5"]
    assert len(classifier.filter_tasks(tasks, "auto")) == 3


def test_real_dataset_patch_blind_and_tier_samples() -> None:
    settings = load_settings(PROJECT_ROOT / "config" / "config.json")
    patch_pkl = settings.data["patch_info_pkl"]
    if patch_pkl is None or not patch_pkl.is_file():
        pytest.skip("furina_context_data not present locally")

    loader = FurinaDataLoader.from_settings(settings)
    classifier = RuleBasedTierClassifier()

    for bug_id, expected_tier in (
        ("Lang-12", "tier_a"),
        ("Codec-2", "tier_a"),
        ("Csv-10", "tier_b"),       # CSVPrinter constructor takes Appendable & CSVFormat
        ("Closure-101", "tier_b"),  # Closure project
        ("Chart-15", "tier_b"),     # multi-method + Graphics2D/PiePlot
        ("Cli-10", "tier_b"),       # fallback slice
    ):
        records = loader.load_by_bug_id(bug_id)
        assert records, f"Expected records for {bug_id}"
        task = strip_to_patch_blind(records[0], verify=True)
        pack = build_initial_intent_pack(task, classifier)
        assert pack.tier == expected_tier, f"{bug_id}: got {pack.tier}, reasons={pack.tier_decision}"


def test_selective_dataset_intent_packs_and_cli(tmp_path: Path) -> None:
    settings = load_settings(PROJECT_ROOT / "config" / "config.json")
    patch_pkl = settings.data["patch_info_pkl"]
    if patch_pkl is None or not patch_pkl.is_file():
        pytest.skip("furina_context_data not present locally")

    # Selective manifest matching only 2 bugs in Tier-A scope
    manifest = tmp_path / "subset.txt"
    manifest.write_text("Lang-12\nCodec_2\n", encoding="utf-8")

    loader = FurinaDataLoader.from_settings(settings)
    load_result, packs = load_intent_packs_for_dataset(loader, manifest, target_tier="tier_a")
    assert sorted(load_result.matched_bug_ids) == ["Codec-2", "Lang-12"]
    assert all(p.tier == "tier_a" for p in packs)

    # Real benchmark Closure directory check if present
    closure_dir = Path(r"E:\workspace\benchmark\Closure")
    if closure_dir.is_dir():
        closure_res, closure_packs = load_intent_packs_for_dataset(loader, closure_dir, limit=3)
        assert len(closure_packs) == 3
        assert all(p.project == "Closure" and p.tier == "tier_b" for p in closure_packs)
        assert closure_res.total_scanned == 844

    # CLI select-dataset end-to-end check
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = cli.main(["select-dataset", "--dataset", str(manifest), "--tier", "tier_a"])
    assert rc == 0
    cli_out = json.loads(buf.getvalue())
    assert cli_out["summary"]["matched_bug_ids"] == ["Codec-2", "Lang-12"]
    assert all(item["tier"] == "tier_a" for item in cli_out["selected_intent_packs"])


def test_review_regressions_crit05_and_warn05() -> None:
    # CRIT-05: leak inside `numbered_buggy_function` or 1-column shifted `+` line must be caught
    raw_diff = (
        "--- a/src/Foo.java\n"
        "+++ b/src/Foo.java\n"
        "@@ -10,3 +10,4 @@\n"
        " public static int foo(int x) {\n"
        "+    return 77;\n"
        "     return x;\n"
        " }\n"
    )
    record = _make_synthetic_record(
        "p/Lang_1/patch1.diff",
        "Lang",
        1,
        raw_diff,
        [
            SubPatchSlice(
                file_path="src/Foo.java",
                start_line=10,
                content="public static int foo(int x) {\n+   return 77;\n    return x;\n}",
                is_fallback_diff=False,
            )
        ],
    )
    clean_task = strip_to_patch_blind(record)
    tampered_anchor = DiffAnchor(
        file_path=clean_task.anchors[0].file_path,
        method_name="foo",
        anchor_start_line=11,
        anchor_end_line=11,
        hunk_old_start=10,
        hunk_old_end=12,
        kind="add_only",
        function_start_line=10,
        function_end_line=12,
        buggy_function_code=clean_task.anchors[0].buggy_function_code,
        numbered_buggy_function=clean_task.anchors[0].numbered_buggy_function + "\n13: +   return 77;",
        anchor_context_lines=clean_task.anchors[0].anchor_context_lines,
        is_fallback_slice=False,
    )
    tampered_task = PatchBlindTask(
        patch_id=clean_task.patch_id,
        bug_id=clean_task.bug_id,
        project=clean_task.project,
        bug_num=clean_task.bug_num,
        anchors=[tampered_anchor],
        affected_files=clean_task.affected_files,
        failing_tests=clean_task.failing_tests,
        bug_report=clean_task.bug_report,
        is_multi_hunk=False,
        is_multi_file=False,
        has_fallback_anchor=False,
    )
    with pytest.raises(PatchBlindViolationError):
        assert_patch_blind_isolation(tampered_task, record)

    # WARN-05: `this.` inside comments or string literals must NOT trigger Tier-B field mutation
    comment_diff = (
        "--- a/src/Pure.java\n"
        "+++ b/src/Pure.java\n"
        "@@ -10,4 +10,4 @@\n"
        " public int pureCalc(int x) {\n"
        "     // note: avoid this.value = x;\n"
        "     String msg = \"this.value = 0\";\n"
        "-    return x;\n"
        "+    return x + 1;\n"
        " }\n"
    )
    comment_rec = _make_synthetic_record(
        "p/Lang_2/patch1.diff",
        "Lang",
        2,
        comment_diff,
        [
            SubPatchSlice(
                file_path="src/Pure.java",
                start_line=10,
                content=(
                    "public int pureCalc(int x) {\n"
                    "    // note: avoid this.value = x;\n"
                    "    String msg = \"this.value = 0\";\n"
                    "-   return x;\n"
                    "+   return x + 1;\n"
                    "}"
                ),
                is_fallback_diff=False,
            )
        ],
    )
    comment_task = strip_to_patch_blind(comment_rec)
    decision = RuleBasedTierClassifier().classify_detailed(comment_task)
    assert decision.tier == "tier_a"
    assert decision.signals["has_this_write"] is False
