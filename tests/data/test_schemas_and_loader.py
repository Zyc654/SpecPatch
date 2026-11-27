"""Unit tests for `specpatchguard.data.schemas` and `specpatchguard.data.pkl_loader`."""

from __future__ import annotations

import json
from pathlib import Path
import pickle
import sys
from typing import Any

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from specpatchguard import __main__ as cli  # noqa: E402
from specpatchguard.core.config_loader import load_settings  # noqa: E402
from specpatchguard.data import (  # noqa: E402
    BoundaryContext,
    BugReport,
    CrossRepResult,
    CrossRepStrategy,
    D4jFallbackProvider,
    DataLoadError,
    DatasetSpec,
    DiffAnchor,
    DiffHunk,
    DockerD4jFallbackProvider,
    FailingTestRecord,
    FrozenSpecBundle,
    FurinaDataLoader,
    HarnessRunResult,
    IntentContextPack,
    JudgeVerdict,
    MethodSignatureInfo,
    PatchBlindTask,
    PatchTargetSelector,
    PrePostSpec,
    RawPatchRecord,
    RuleBasedTierClassifier,
    SoftSpecCandidate,
    SubPatchSlice,
    TestHarnessStrategy,
    TierClassifier,
    TierDecision,
    WitnessBank,
    WitnessCase,
    attach_line_numbers,
    canonicalize_project_name,
    discover_dataset_spec,
    normalize_bug_report,
    parse_d4j_bug_id,
    parse_failing_tests,
    parse_patch_metadata,
    parse_target_selector,
    parse_unified_diff,
)


class _DummyCrossRep:
    tier = "tier_a"

    def generate_candidates(self, intent_pack: IntentContextPack, config: dict[str, Any]) -> CrossRepResult:
        return CrossRepResult(tier="tier_a", candidate_inputs=[{"x": 0}])


class _DummyHarness:
    tier = "tier_a"

    def build_junit_source(
        self,
        pre_post: PrePostSpec,
        candidate_inputs: list[dict[str, Any]],
        intent_pack: IntentContextPack,
    ) -> str:
        return "class DummyTest {}"

    def parse_execution_result(self, raw_result: dict[str, Any]) -> HarnessRunResult:
        return HarnessRunResult(tier="tier_a", compiled=True)


class _MaliciousPicklePayload:
    def __reduce__(self) -> tuple[Any, tuple[str]]:
        return (eval, ("1+1",))


def test_schema_json_round_trip_and_protocols() -> None:
    report = BugReport(bug_id="Lang-1", title="NumberUtils bug", description="Fails on hex", available=True)
    test_rec = FailingTestRecord(
        test_file="org/apache/commons/lang3/math/NumberUtilsTest.java",
        test_method_name="testCreateNumber",
        start_line=10,
        source_code="@Test\npublic void testCreateNumber() {\n    NumberUtils.createNumber(\"0x\");\n}",
        numbered_source="10: @Test\n11: public void testCreateNumber() {\n12:     NumberUtils.createNumber(\"0x\");\n13: }",
        failed_line_start=12,
        failed_line_end=12,
        failure_message="NumberFormatException",
        stack_trace="at NumberUtils.java:450",
    )
    anchor = DiffAnchor(
        file_path="src/main/java/org/apache/commons/lang3/math/NumberUtils.java",
        method_name="createNumber",
        anchor_start_line=450,
        anchor_end_line=452,
        hunk_old_start=447,
        hunk_old_end=455,
        kind="modify",
        function_start_line=440,
        function_end_line=470,
        buggy_function_code="public static Number createNumber(String str) { return null; }",
        numbered_buggy_function="440: public static Number createNumber(String str) { return null; }",
        anchor_context_lines="450: return null;",
        is_fallback_slice=False,
    )
    method_sig = MethodSignatureInfo(
        file_path=anchor.file_path,
        class_name="NumberUtils",
        method_name="createNumber",
        visibility="public",
        is_static=True,
        is_constructor=False,
        return_type="Number",
        parameter_types=["String"],
        parameter_names=["str"],
        throws_types=["NumberFormatException"],
        modifiers=["public", "static"],
        javadoc="/** Parses a number */",
        raw_signature="public static Number createNumber(String str) throws NumberFormatException",
    )
    boundary = BoundaryContext(
        enclosing_branches=[{"kind": "if", "line": 448}],
        class_fields=[{"name": "ZERO", "static": True}],
        callers=[{"file": "Other.java", "line": 12}],
        overrides=[],
        non_local_state_touches=[],
        notes=["static utility"],
    )
    decision = TierDecision(tier="tier_a", confidence=0.92, reasons=["static method"], signals={"any_static": True})
    intent_pack = IntentContextPack(
        bug_id="Lang-1",
        patch_id="p1",
        project="Lang",
        tier="tier_a",
        bug_intent_summary="Handle 0x hex edge cases",
        anchors=[anchor],
        target_methods=[method_sig],
        failing_tests=[test_rec],
        bug_report=report,
        entry_harness_skeleton="",
        boundary_context=boundary,
        tier_decision=decision,
        extra_context={"is_multi_hunk": False},
    )
    assert IntentContextPack.from_dict(json.loads(json.dumps(intent_pack.to_dict()))) == intent_pack

    soft = SoftSpecCandidate(
        candidate_id="soft-1",
        source="tier_b_decision_table",
        description="Guard null input",
        condition="str == null",
        expected_behavior="return null",
        weight=0.25,
    )
    witness = WitnessCase(
        case_id="w-1",
        inputs={"str": "0x80000000"},
        expected_outcome="returns Integer",
        observed_buggy_outcome="throws NumberFormatException",
        source="hypothesis",
    )
    bank = WitnessBank(
        confirmed_bug_witnesses=[witness],
        boundary_passing_witnesses=[],
        excluded_irrelevant_cases=[],
        infection_probe_traces=[{"line": 450, "hit": True}],
    )
    spec = PrePostSpec(
        preconditions=["str != null"],
        postconditions=["returns parsed Number"],
        invariants=["pure"],
        exception_contracts=["throws NumberFormatException on invalid format"],
        anchor_infection_conditions=["pfxLen == 2"],
    )
    bundle = FrozenSpecBundle(
        bug_id="Lang-1",
        patch_id="p1",
        tier="tier_a",
        hard_specs=spec,
        witness_bank=bank,
        soft_spec_candidates=[soft],
    )
    assert FrozenSpecBundle.from_dict(json.loads(json.dumps(bundle.to_dict()))) == bundle

    verdict = JudgeVerdict(
        bug_id="Lang-1",
        patch_id="p1",
        verdict="CORRECT",
        failed_gates=[],
        witness_evidence=[{"case_id": "w-1", "passed": True}],
        lsp_evidence=[{"rule": "signature_preserved", "ok": True}],
        soft_spec_findings=[],
        rationale="All confirmed witnesses pass without violating boundary rules.",
    )
    assert JudgeVerdict.from_dict(json.loads(json.dumps(verdict.to_dict()))) == verdict

    task = PatchBlindTask(
        patch_id="p1",
        bug_id="Lang-1",
        project="Lang",
        bug_num=1,
        anchors=[anchor],
        affected_files=[anchor.file_path],
        failing_tests=[test_rec],
        bug_report=report,
        is_multi_hunk=False,
        is_multi_file=False,
        has_fallback_anchor=False,
    )
    assert PatchBlindTask.from_dict(json.loads(json.dumps(task.to_dict()))) == task

    assert isinstance(RuleBasedTierClassifier(), TierClassifier)
    assert isinstance(_DummyCrossRep(), CrossRepStrategy)
    assert isinstance(_DummyHarness(), TestHarnessStrategy)


def test_parse_patch_metadata_variants_and_invalid() -> None:
    m1 = parse_patch_metadata("Multi-Hunk-APR/Multi-Hunk-APR/result/d4j_patch/Chart_15/patch2.diff")
    assert m1["project"] == "Chart"
    assert m1["bug_num"] == 15
    assert m1["bug_id"] == "Chart-15"
    assert m1["label"] is None

    m2 = parse_patch_metadata("datasets/Lin2022TOSEM/correct/patch1-Lang-33-FixMiner.patch")
    assert m2["project"] == "Lang"
    assert m2["bug_num"] == 33
    assert m2["tool"] == "FixMiner"
    assert m2["label"] == 1

    m3 = parse_patch_metadata("datasets/ChatRepairPatch/overfitting/patch2-Math-50-ChatRepair.patch")
    assert m3["label"] == 0
    assert m3["bug_id"] == "Math-50"

    with pytest.raises(DataLoadError):
        parse_patch_metadata("invalid/path/without_bug_id.diff")


def test_parse_unified_diff_edge_cases() -> None:
    diff_text = (
        "diff --git a/src/Foo.java b/src/Foo.java\r\n"
        "--- a/src/Foo.java\t2026-01-01\r\n"
        "+++ b/src/Foo.java\t2026-01-01\r\n"
        "@@ -10 +10,3 @@ public int foo(int x) {\r\n"
        "     int y = x;\r\n"
        "+    if (y < 0) {\r\n"
        "+        return 0;\r\n"
        "@@ -25,3 +27,2 @@ public int bar() {\r\n"
        "     int a = 1;\r\n"
        "---a;\r\n"
        "     return a;\r\n"
        "\\ No newline at end of file\r\n"
        "diff --git a/src/Bar.java b/src/Bar.java\r\n"
        "--- a/src/Bar.java\r\n"
        "+++ /dev/null\r\n"
        "@@ -1,2 +0,0 @@\r\n"
        "-class Bar {}\r\n"
        "-// end\r\n"
    )
    hunks = parse_unified_diff(diff_text)
    assert len(hunks) == 3

    # Hunk 0: omitted old_count defaults to 1, pure addition
    assert hunks[0].file_path == "src/Foo.java"
    assert hunks[0].old_start == 10
    assert hunks[0].old_count == 1
    assert hunks[0].kind == "add_only"
    assert hunks[0].anchor_start_line == 10
    assert hunks[0].added_lines == ["    if (y < 0) {", "        return 0;"]

    # Hunk 1: deleted line '--a;' (encoded as '---a;') did not prematurely end the hunk!
    assert hunks[1].kind == "delete_only"
    assert hunks[1].deleted_lines == ["--a;"]
    assert hunks[1].anchor_start_line == 26
    assert hunks[1].anchor_end_line == 26

    # Hunk 2: deleted file (/dev/null in +++) resolves to old file path
    assert hunks[2].file_path == "src/Bar.java"
    assert hunks[2].kind == "delete_only"
    assert hunks[2].anchor_start_line == 1
    assert hunks[2].anchor_end_line == 2


def test_normalize_bug_report_and_failing_tests_edge_cases() -> None:
    empty1 = normalize_bug_report("Mockito-2", ["Title only", "None\n"])
    assert empty1.title == "Title only"
    assert empty1.description == ""
    assert empty1.available is True

    empty2 = normalize_bug_report("Chart-3", None)
    assert empty2.available is False

    empty3 = normalize_bug_report("X-1", ["null", "  "])
    assert empty3.available is False

    tests = parse_failing_tests(
        [
            [
                "@Test\npublic void testSingle() {\n    assertEquals(1, 2);\n}",
                "@Test\npublic void testSingle() {\n    <Failed>assertEquals(1, 2);</Failed>\n}",
                50,
                "src/test/FooTest.java",
                "expected <1> but was <2>",
            ],
            [
                "@Test\npublic void testEmptyLabel() {\n    fail();\n}",
                "",
                90,
                "src/test/BarTest.java",
                "AssertionFailedError",
                "at BarTest.java:91",
            ],
        ]
    )
    assert len(tests) == 2
    assert tests[0].test_method_name == "testSingle"
    assert tests[0].failed_line_start == 52
    assert tests[0].failed_line_end == 52
    assert tests[0].stack_trace == ""
    assert tests[1].failed_line_start is None
    assert tests[1].stack_trace == "at BarTest.java:91"
    assert attach_line_numbers("a\nb", 10) == "10: a\n11: b"


def test_loader_rejects_unsafe_pickle_and_malformed_data(tmp_path: Path) -> None:
    bad_pkl = tmp_path / "bad.pkl"
    bad_pkl.write_bytes(pickle.dumps(_MaliciousPicklePayload()))
    loader = FurinaDataLoader(patch_info_pkl=bad_pkl)
    with pytest.raises(DataLoadError, match="Unsupported pickle object"):
        loader.load_all()


def test_real_furina_context_data_slice() -> None:
    config_path = PROJECT_ROOT / "config" / "config.json"
    settings = load_settings(config_path)
    patch_pkl = settings.data["patch_info_pkl"]
    if patch_pkl is None or not patch_pkl.is_file():
        pytest.skip("furina_context_data not present locally")

    loader = FurinaDataLoader.from_settings(settings)
    lang_records = loader.load_all(projects=["Lang"], limit=5)
    assert len(lang_records) == 5
    for rec in lang_records:
        assert rec.project == "Lang"
        assert rec.hunks
        assert rec.sub_patches
        assert rec.failing_tests

    # Missing test entry (Compress-7) and missing bug report (Chart-10) degrade cleanly.
    c7 = loader.load_by_bug_id("Compress-7")
    assert c7
    assert c7[0].failing_tests == []

    ch10 = loader.load_by_bug_id("Chart-10")
    assert ch10
    assert ch10[0].bug_report.available is False


def test_official_d4j_naming_and_target_selectors() -> None:
    assert canonicalize_project_name("lang") == "Lang"
    assert canonicalize_project_name("jacksondatabind") == "JacksonDatabind"
    assert canonicalize_project_name("CustomProj") == "CustomProj"

    for raw in ("Lang-12", "Lang_12", "Lang/12", "lang-12b", "Lang_12f", "Lang/Lang_12"):
        proj, num, bug_id = parse_d4j_bug_id(raw)
        assert (proj, num, bug_id) == ("Lang", 12, "Lang-12")

    s1 = parse_target_selector("Lang_12/patch1.diff")
    assert s1.bug_id == "Lang-12"
    assert s1.patch_name == "patch1.diff"

    s2 = parse_target_selector("Lang-12::random")
    assert s2.bug_id == "Lang-12"
    assert s2.method_name == "random"

    s3 = parse_target_selector("Lang-12::RandomStringUtils.java::random")
    assert s3.bug_id == "Lang-12"
    assert s3.file_path == "RandomStringUtils.java"
    assert s3.method_name == "random"

    s4 = parse_target_selector({"project": "math", "bug_num": 1, "patch": "patch1.diff"})
    assert s4.bug_id == "Math-1"
    assert s4.patch_name == "patch1.diff"


def test_discover_dataset_spec_from_directories_and_manifests(tmp_path: Path) -> None:
    # 1. Official `<Project>/<BugNum>/` layout + ignored helper dirs
    bench = tmp_path / "benchmark"
    (bench / "_shared_lib" / "Closure").mkdir(parents=True)
    (bench / "Closure" / "101" / "src").mkdir(parents=True)
    (bench / "Closure" / "101" / ".vscode").mkdir(parents=True)
    (bench / "Lang" / "12" / "src").mkdir(parents=True)
    spec_bench = discover_dataset_spec(bench)
    assert spec_bench.source_label == "directory"
    assert sorted(spec_bench.bug_ids) == ["Closure-101", "Lang-12"]

    # 2. `<Project>/<Project>_<BugNum>/` layout (`dfj` style)
    dfj = tmp_path / "dfj"
    (dfj / "Lang" / "Lang_12" / "src").mkdir(parents=True)
    (dfj / "Math" / "Math_1" / "src").mkdir(parents=True)
    spec_dfj = discover_dataset_spec(dfj)
    assert sorted(spec_dfj.bug_ids) == ["Lang-12", "Math-1"]

    # 3. `<Project>_<BugNum>/<patch>.diff` layout
    patches_dir = tmp_path / "patches"
    (patches_dir / "Lang_12").mkdir(parents=True)
    (patches_dir / "Lang_12" / "patch1.diff").write_text("--- a/X.java\n+++ b/X.java\n", encoding="utf-8")
    spec_patches = discover_dataset_spec(patches_dir)
    assert len(spec_patches.targets) == 1
    assert spec_patches.targets[0].bug_id == "Lang-12"
    assert spec_patches.targets[0].patch_name == "patch1.diff"

    # 4. Text, CSV, and JSON manifests
    txt_manifest = tmp_path / "subset.txt"
    txt_manifest.write_text("# comment\nLang-12\nMath_1/patch1.diff\nCodec\n", encoding="utf-8")
    spec_txt = discover_dataset_spec(txt_manifest)
    assert spec_txt.projects == ["Codec"]
    assert spec_txt.bug_ids == ["Lang-12"]
    assert spec_txt.targets[0].bug_id == "Math-1"

    csv_manifest = tmp_path / "subset.csv"
    csv_manifest.write_text(
        "bug_id,patch_name,method_name\nLang_12,patch1.diff,random\nMath-999,,\n",
        encoding="utf-8",
    )
    spec_csv = discover_dataset_spec(csv_manifest)
    assert len(spec_csv.targets) == 1
    assert spec_csv.targets[0].method_name == "random"
    assert spec_csv.bug_ids == ["Math-999"]

    json_manifest = tmp_path / "subset.json"
    json_manifest.write_text(
        json.dumps({"bug_ids": ["Lang-12"], "targets": ["Math-1::BigFraction.java::BigFraction"]}),
        encoding="utf-8",
    )
    spec_json = discover_dataset_spec(json_manifest)
    assert spec_json.bug_ids == ["Lang-12"]
    assert spec_json.targets[0].bug_id == "Math-1"


def test_selective_pkl_loading_subset_missing_and_unindexed_diff(tmp_path: Path) -> None:
    patch_pkl = tmp_path / "patches.pkl"
    test_pkl = tmp_path / "tests.pkl"
    report_json = tmp_path / "reports.json"

    raw_patches = [
        [
            ["custom_bench/Lang_12/patch1.diff", "--- a/src/RandomStringUtils.java\n+++ b/src/RandomStringUtils.java\n@@ -10,3 +10,3 @@\n public static String random(int c) {\n- return null;\n+ return \"\";\n }\n"],
            [["src/RandomStringUtils.java", 10, "public static String random(int c) {\n- return null;\n+ return \"\";\n}"]],
        ],
        [
            ["custom_bench/Lang_12/patch2.diff", "--- a/src/Other.java\n+++ b/src/Other.java\n@@ -5,3 +5,3 @@\n public static void helper() {\n- int x = 0;\n+ int x = 1;\n }\n"],
            [["src/Other.java", 5, "public static void helper() {\n- int x = 0;\n+ int x = 1;\n}"]],
        ],
        [
            ["custom_bench/Math_1/patch1.diff", "--- a/src/BigFraction.java\n+++ b/src/BigFraction.java\n@@ -20,3 +20,3 @@\n public BigFraction(double v) {\n- int a = 0;\n+ int a = 1;\n }\n"],
            [["src/BigFraction.java", 20, "public BigFraction(double v) {\n- int a = 0;\n+ int a = 1;\n}"]],
        ],
    ]
    patch_pkl.write_bytes(pickle.dumps(raw_patches))
    test_pkl.write_bytes(
        pickle.dumps(
            {
                "Lang-12": [
                    ["@Test\npublic void testRandom() {\n fail();\n}", "@Test\npublic void testRandom() {\n <Failed>fail();</Failed>\n}", 1, "RandomTest.java", "fail", "trace"]
                ],
                "Lang-99": [
                    ["@Test\npublic void testCustom() {\n fail();\n}", "", 1, "CustomTest.java", "custom fail", "trace"]
                ],
            }
        )
    )
    report_json.write_text(json.dumps({"lang-12": ["Random bug", "Bad output"], "lang-99": ["Unindexed bug", "Desc"]}), encoding="utf-8")

    loader = FurinaDataLoader(patch_pkl, test_pkl, report_json)

    # Select by method name inside a bug selector
    res = loader.load_dataset(["Lang-12::random"])
    assert len(res.records) == 1
    assert res.records[0].metadata["matched_methods"] == ["random"]
    assert res.total_scanned == 3

    # Shorthand patch lookup & official bug_id normalization
    p2 = loader.load_by_patch_id("Lang_12/patch2.diff")
    assert p2.patch_id.endswith("Lang_12/patch2.diff")
    assert len(loader.load_by_bug_id("lang_12b")) == 2

    # Missing bug tracking + strict error
    partial = loader.load_dataset(["Lang-12", "Math-404"])
    assert partial.matched_bug_ids == ["Lang-12"]
    assert partial.missing_bug_ids == ["Math-404"]
    with pytest.raises(DataLoadError, match="Math-404"):
        loader.load_dataset(["Lang-12", "Math-404"], strict=True)

    # Unindexed local diff enrichment using test_pkl + bug_report_json
    unindexed_dir = tmp_path / "new_diffs" / "Lang_99"
    unindexed_dir.mkdir(parents=True)
    (unindexed_dir / "candidate1.diff").write_text(
        "--- a/src/Foo.java\n+++ b/src/Foo.java\n@@ -1,2 +1,2 @@\n-int a = 1;\n+int a = 2;\n",
        encoding="utf-8",
    )
    res_unindexed = loader.load_dataset(unindexed_dir, include_unindexed_diffs=True)
    assert len(res_unindexed.records) == 1
    assert res_unindexed.records[0].bug_id == "Lang-99"
    assert res_unindexed.records[0].failing_tests[0].test_method_name == "testCustom"
    assert res_unindexed.records[0].bug_report.title == "Unindexed bug"


def test_review_regressions_crit01_to_crit04_and_warn01_to_warn07(tmp_path: Path) -> None:
    # CRIT-04: filename `patch_1.diff` must not shadow parent directory `Lang_12`
    m_patch = parse_patch_metadata("my_patches/Lang_12/patch_1.diff")
    assert m_patch["project"] == "Lang"
    assert m_patch["bug_num"] == 12
    assert m_patch["bug_id"] == "Lang-12"

    # CRIT-03: wrapper folder `subset/` must not shadow nested `Closure/101`
    wrapped = tmp_path / "my_eval" / "subset" / "Closure" / "101" / "src"
    wrapped.mkdir(parents=True)
    spec_wrapped = discover_dataset_spec(tmp_path / "my_eval")
    assert spec_wrapped.bug_ids == ["Closure-101"]

    # WARN-01 & WARN-04: empty manifest and string-typed JSON fields and alphanumeric typos raise DataLoadError
    empty_txt = tmp_path / "empty.txt"
    empty_txt.write_text("# comment only\n\n", encoding="utf-8")
    with pytest.raises(DataLoadError, match="no dataset selectors"):
        discover_dataset_spec(empty_txt)

    bad_json = tmp_path / "bad_types.json"
    bad_json.write_text(json.dumps({"projects": "Lang"}), encoding="utf-8")
    with pytest.raises(DataLoadError, match="must be a list"):
        discover_dataset_spec(bad_json)

    with pytest.raises(DataLoadError):
        discover_dataset_spec("Lang12")

    # Prepare mini pkl for CRIT-01, CRIT-02, WARN-02, WARN-03, WARN-06, WARN-07
    patch_pkl = tmp_path / "mini_patches.pkl"
    test_pkl = tmp_path / "mini_tests.pkl"
    report_json = tmp_path / "mini_reports.json"
    raw_patches = [
        [
            ["bench/Lang_12/patch1.diff", "--- a/src/MyStringUtils.java\n+++ b/src/MyStringUtils.java\n@@ -10,3 +10,3 @@\n /*\n * helper()\n */\n public static String\n random(\n int count) {\n- return null;\n+ return \"\";\n }\n"],
            [["src/MyStringUtils.java", 10, "/*\n * helper()\n */\npublic static String\nrandom(\nint count) {\n- return null;\n+ return \"\";\n}"]],
        ],
        [
            ["bench/Lang_13/patch1.diff", "--- a/src/StringUtils.java\n+++ b/src/StringUtils.java\n@@ -20,2 +20,2 @@\n-int a = 1;\n+int a = 2;\n"],
            [["src/StringUtils.java", 20, "public static void work() {\n- int a = 1;\n+ int a = 2;\n}"]],
        ],
    ]
    patch_pkl.write_bytes(pickle.dumps(raw_patches))
    test_pkl.write_bytes(pickle.dumps({}))
    report_json.write_text("{}", encoding="utf-8")
    loader = FurinaDataLoader(patch_pkl, test_pkl, report_json)

    # CRIT-01: file_paths=["StringUtils.java"] must NOT match `MyStringUtils.java`
    res_file = loader.load_dataset(file_paths=["StringUtils.java"])
    assert [r.bug_id for r in res_file.records] == ["Lang-13"]

    # CRIT-02: single-string selector `"Lang-12/patch1.diff"` behaves identically to `["Lang-12/patch1.diff"]`
    res_single = loader.load_dataset("Lang-12/patch1.diff")
    res_list = loader.load_dataset(["Lang-12/patch1.diff"])
    assert [r.patch_id for r in res_single.records] == [r.patch_id for r in res_list.records] == ["bench/Lang_12/patch1.diff"]
    with pytest.raises(DataLoadError, match="Dataset path not found"):
        loader.load_dataset("./definitely_missing.diff")

    # WARN-06: multiline signature after block comment extracts `random`, not `helper`
    res_method = loader.load_dataset(method_names=["random"])
    assert [r.bug_id for r in res_method.records] == ["Lang-12"]

    # WARN-02 & WARN-03: limit <= 0 rejected; limit=1 with non-strict clears unvisited missing_bug_ids,
    # whereas strict=True still verifies all requested bugs before slicing.
    with pytest.raises(DataLoadError, match="positive integer"):
        loader.load_dataset(limit=0)
    res_lim = loader.load_dataset(["Lang-12", "Lang-13"], limit=1)
    assert len(res_lim.records) == 1
    assert res_lim.truncated_by_limit is True
    assert res_lim.missing_bug_ids == []
    with pytest.raises(DataLoadError, match="Lang-404"):
        loader.load_dataset(["Lang-12", "Lang-404"], limit=1, strict=True)

    # WARN-07: Latin-1 local diff loads cleanly without UnicodeDecodeError
    latin_dir = tmp_path / "latin_diffs" / "Lang_77"
    latin_dir.mkdir(parents=True)
    (latin_dir / "fix.diff").write_bytes(
        "--- a/src/Latin.java\n+++ b/src/Latin.java\n@@ -1,2 +1,2 @@\n-// café\n+// café fixed\n".encode("latin-1")
    )
    res_latin = loader.load_dataset(latin_dir, include_unindexed_diffs=True)
    assert len(res_latin.records) == 1
    assert "café fixed" in res_latin.records[0].raw_diff


def test_docker_d4j_fallback_hydrates_missing_pkl_entries(tmp_path: Path) -> None:
    calls: list[list[str]] = []
    source_java = (
        "package org.apache.commons.lang3;\n"
        "public class CustomHelper {\n"
        "    public static int compute(int x) {\n"
        "        if (x < 0) {\n"
        "            return -1;\n"
        "        }\n"
        "        return x * 2;\n"
        "    }\n"
        "}\n"
    )
    test_java = (
        "package org.apache.commons.lang3;\n"
        "import org.junit.Test;\n"
        "public class CustomHelperTest {\n"
        "    @Test\n"
        "    public void testComputeNeg() {\n"
        "        org.junit.Assert.assertEquals(0, CustomHelper.compute(-5));\n"
        "    }\n"
        "}\n"
    )

    def fake_runner(argv: list[str], timeout: int) -> tuple[int, str, str]:
        calls.append(argv)
        script = argv[-1]
        if "defects4j checkout" in script:
            return 0, "", ""
        if "defects4j export" in script and "tests.trigger" in script:
            return 0, "org.apache.commons.lang3.CustomHelperTest::testComputeNeg\n", ""
        if "defects4j export" in script and "dir.src.tests" in script:
            return 0, "src/test/java\n", ""
        if "defects4j info" in script:
            return (
                0,
                "Summary:\n    Negative input returns -1 instead of 0\n"
                "Root cause in triggering tests:\n"
                " - org.apache.commons.lang3.CustomHelperTest::testComputeNeg:\n"
                "   junit.framework.AssertionFailedError: expected:<0> but was:<-1>\n",
                "",
            )
        if "CustomHelperTest.java" in script:
            return 0, test_java, ""
        if "CustomHelper.java" in script:
            return 0, source_java, ""
        return 1, "", "not found"

    provider = DockerD4jFallbackProvider(
        image_name="d4j-lsp-runtime:latest",
        container_work_root="/workspace/d4j_work",
        d4j_home="/data/public/defects4j",
        java8_home="/workspace/envs/defects4j",
        container_name="d4j_box",
        runner=fake_runner,
    )
    assert isinstance(provider, D4jFallbackProvider)

    # Empty/partial pkl + unindexed local .diff file -> hydrated completely via Docker D4J fallback!
    patch_pkl = tmp_path / "empty_patches.pkl"
    patch_pkl.write_bytes(pickle.dumps([]))
    diff_dir = tmp_path / "custom_set" / "Lang_88"
    diff_dir.mkdir(parents=True)
    (diff_dir / "patch1.diff").write_text(
        "--- a/src/main/java/org/apache/commons/lang3/CustomHelper.java\n"
        "+++ b/src/main/java/org/apache/commons/lang3/CustomHelper.java\n"
        "@@ -4,3 +4,3 @@ public class CustomHelper {\n"
        "         if (x < 0) {\n"
        "-            return -1;\n"
        "+            return 0;\n"
        "         }\n",
        encoding="utf-8",
    )

    loader = FurinaDataLoader(patch_info_pkl=patch_pkl, d4j_fallback=provider)
    result = loader.load_dataset(diff_dir)
    assert len(result.records) == 1
    rec = result.records[0]
    assert rec.bug_id == "Lang-88"
    assert rec.metadata.get("d4j_fallback_hydrated") is True
    assert rec.bug_report.available is True
    assert "Negative input" not in rec.bug_report.title
    assert "Negative input" not in rec.bug_report.description
    assert "AssertionFailedError" in rec.bug_report.description
    assert len(rec.failing_tests) == 1
    assert rec.failing_tests[0].test_method_name == "testComputeNeg"
    assert rec.failing_tests[0].start_line == 4
    assert len(rec.sub_patches) == 1
    assert rec.sub_patches[0].start_line == 3
    assert "return -1;" in rec.sub_patches[0].content
    assert "return 0;" not in rec.sub_patches[0].content
    assert any(argv[:3] == ["docker", "exec", "d4j_box"] for argv in calls)

    # Summary-only `defects4j info` without triggering-test root cause must stay empty/unavailable.
    summary_only_provider = DockerD4jFallbackProvider(
        image_name="d4j-lsp-runtime:latest",
        container_work_root="/workspace/d4j_work",
        d4j_home="/data/public/defects4j",
        java8_home="/workspace/envs/defects4j",
        runner=lambda argv, timeout: (0, "Summary:\n    Commit hint only\n", ""),
    )
    summary_only_report = summary_only_provider.fetch_bug_report("Lang", 88)
    assert summary_only_report.available is False
    assert summary_only_report.title == ""
    assert summary_only_report.description == ""
