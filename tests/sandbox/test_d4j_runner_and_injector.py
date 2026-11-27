"""Unit tests for `Defects4JRunner`, `JUnitInjector`, `ReadOnlyAnchorProbeInjector`, and markers."""

from __future__ import annotations

from pathlib import Path
import sys
from typing import Any

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from specpatchguard.core.config_loader import load_settings  # noqa: E402
from specpatchguard.core.doctor import scan_server_config_markers  # noqa: E402
from specpatchguard.data.schemas import HarnessRunResult  # noqa: E402
from specpatchguard.sandbox import (  # noqa: E402
    CASE_PREFIX,
    PROBE_PREFIX,
    AnchorProbeInjector,
    AnchorProbeSpec,
    D4jProjectLayout,
    Defects4JRunner,
    DockerSandboxBackend,
    InjectedTestSpec,
    JUnitInjector,
    ReadOnlyAnchorProbeInjector,
    SandboxError,
    parse_case_records,
    parse_d4j_test_stdout,
    parse_failing_tests_file,
    parse_junit_core_stdout,
    parse_probe_traces,
)


class _FakeD4jDockerEnv:
    """Simulated Defects4J container environment for testing runner and injector flows."""

    def __init__(self) -> None:
        self.commands: list[list[str]] = []
        self.files: dict[str, bytes] = {}
        self.checkout_rc = 0
        self.compile_rc = 0
        self.compile_stdout = ""
        self.compile_stderr = ""
        self.test_rc = 0
        self.test_stdout = "Failing tests: 0\n"
        self.test_stderr = ""
        self.test_timed_out = False
        self.failing_tests_content = ""
        self.raise_on_test = False
        self.apply_rc = 0

    def run(self, argv: list[str], timeout: int) -> tuple[int, str, str]:
        self.commands.append(list(argv))
        if argv[:2] == ["docker", "run"]:
            cname = argv[argv.index("--name") + 1]
            return 0, f"{cname}-cid\n", ""
        if argv[:3] == ["docker", "rm", "-f"]:
            return 0, "", ""
        if argv[:2] == ["docker", "exec"]:
            workdir = ""
            idx = 2
            if argv[idx] == "-w":
                workdir = argv[idx + 1]
                idx += 2
            inner = argv[idx + 1 :]
            if inner[:2] == ["cat", "--"]:
                target = f"{workdir}/{inner[2]}"
                if target in self.files:
                    return 0, self.files[target].decode("utf-8", errors="replace"), ""
                return 1, "", "No such file"
            if inner[:2] == ["test", "-e"]:
                target = f"{workdir}/{inner[2]}"
                return (0, "", "") if target in self.files else (1, "", "")
            if inner[:2] == ["rm", "-f"]:
                for rel in inner[3:]:
                    target = rel if rel.startswith("/") else f"{workdir}/{rel}"
                    self.files.pop(target, None)
                return 0, "", ""
            if inner[:2] in (["sh", "-c"], ["bash", "-lc"]):
                script = inner[2]
                if "if [ -s failing_tests ]" in script:
                    return 0, self.failing_tests_content, ""
                if "defects4j checkout" in script:
                    if self.checkout_rc == 0:
                        self.files["/workspace/d4j_work/Lang_12b/.defects4j.config"] = b"pid=Lang\nvid=12b\n"
                        self.files["/workspace/d4j_work/Lang_12b/src/main/java/org/example/Foo.java"] = (
                            b"package org.example;\n"
                            b"public class Foo {\n"
                            b"    public int calc(int x) {\n"
                            b"        if (x < 0) {\n"
                            b"            return -1;\n"
                            b"        }\n"
                            b"        return x + 1;\n"
                            b"    }\n"
                            b"}\n"
                        )
                    return self.checkout_rc, "", ""
                if "__SPG_PROP_dir.src.classes_START__" in script:
                    out = "\n".join(
                        [
                            "__SPG_PROP_dir.src.classes_START__",
                            "src/main/java",
                            "__SPG_PROP_dir.src.classes_END__",
                            "__SPG_PROP_dir.src.tests_START__",
                            "src/test/java",
                            "__SPG_PROP_dir.src.tests_END__",
                            "__SPG_PROP_dir.bin.classes_START__",
                            "target/classes",
                            "__SPG_PROP_dir.bin.classes_END__",
                            "__SPG_PROP_dir.bin.tests_START__",
                            "target/test-classes",
                            "__SPG_PROP_dir.bin.tests_END__",
                            "__SPG_PROP_tests.trigger_START__",
                            "org.example.FooTest::testNegative",
                            "__SPG_PROP_tests.trigger_END__",
                            "__SPG_PROP_tests.relevant_START__",
                            "org.example.FooTest",
                            "__SPG_PROP_tests.relevant_END__",
                            "__SPG_PROP_classes.modified_START__",
                            "org.example.Foo",
                            "__SPG_PROP_classes.modified_END__",
                            "__SPG_PROP_cp.test_START__",
                            "/tmp/junit.jar:target/classes:target/test-classes",
                            "__SPG_PROP_cp.test_END__",
                        ]
                    )
                    return 0, out, ""
                if "defects4j compile" in script:
                    if self.compile_rc == 0:
                        self.files[
                            "/workspace/d4j_work/Lang_12b/target/test-classes/org/example/SpecGuardTmpTest.class"
                        ] = b"CAFEBABE"
                    return self.compile_rc, self.compile_stdout, self.compile_stderr
                if "defects4j test" in script or "org.junit.runner.JUnitCore" in script:
                    if self.raise_on_test:
                        raise RuntimeError("Simulated container crash during defects4j test")
                    if self.test_timed_out:
                        return 124, self.test_stdout, self.test_stderr
                    return self.test_rc, self.test_stdout, self.test_stderr
                if "git apply" in script or "patch --batch" in script:
                    return self.apply_rc, "", ("patch error" if self.apply_rc != 0 else "")
                if ".class" in script and "rm -f" in script:
                    for key in list(self.files):
                        if key.endswith(".class") and "SpecGuardTmpTest" in key:
                            self.files.pop(key, None)
                    return 0, "", ""
                return 0, "", ""
        return 0, "", ""

    def run_stdin(self, argv: list[str], input_bytes: bytes, timeout: int) -> tuple[int, str, str]:
        workdir = ""
        idx = 2
        if argv[idx] == "-i":
            idx += 1
        if argv[idx] == "-w":
            workdir = argv[idx + 1]
            idx += 2
        inner = argv[idx + 1 :]
        if inner[:2] == ["sh", "-c"] and "cat > " in inner[2]:
            target_raw = inner[2].split("cat > ", 1)[1].strip().strip("'\"")
            target = target_raw if target_raw.startswith("/") else f"{workdir}/{target_raw}"
            self.files[target] = input_bytes
            return 0, "", ""
        return 1, "", "Unsupported stdin write"


def _make_setup(env: _FakeD4jDockerEnv) -> tuple[DockerSandboxBackend, Defects4JRunner]:
    settings = load_settings(PROJECT_ROOT / "config" / "config.json")
    backend = DockerSandboxBackend.from_settings(settings, runner=env.run, stdin_runner=env.run_stdin)
    backend.start()
    runner = Defects4JRunner.from_settings(backend, settings)
    return backend, runner


def test_checkout_supports_official_ids_and_exports_layout():
    env = _FakeD4jDockerEnv()
    backend, runner = _make_setup(env)
    layout = runner.checkout("Lang-12")
    assert isinstance(layout, D4jProjectLayout)
    assert layout.project == "Lang"
    assert layout.bug_num == 12
    assert layout.bug_id == "Lang-12"
    assert layout.version == "12b"
    assert layout.work_dir == "/workspace/d4j_work/Lang_12b"
    assert layout.dir_src_classes == "src/main/java"
    assert layout.dir_src_tests == "src/test/java"
    assert layout.dir_bin_tests == "target/test-classes"
    assert layout.tests_trigger == ["org.example.FooTest::testNegative"]
    assert layout.classes_modified == ["org.example.Foo"]
    assert backend.active_work_root == "/workspace/d4j_work/Lang_12b"
    assert backend.state == "prepatch"

    # Reused checkout detects __SPG_CHECKOUT_REUSED__ and resets cleanly.
    reused_layout = runner.checkout("Lang", 12, session_subdir="batch_a")
    assert reused_layout.work_dir == "/workspace/d4j_work/batch_a/Lang_12b"


def test_parse_d4j_test_stdout_and_failing_tests_file():
    out = (
        "Failing tests: 2\r\n"
        "  - org.example.FooTest::testOne\r\n"
        "  - org.example.FooTest::testTwo\r\n"
    )
    count, names, compile_failed = parse_d4j_test_stdout(out)
    assert count == 2
    assert names == ["org.example.FooTest::testOne", "org.example.FooTest::testTwo"]
    assert compile_failed is False

    _, _, c_fail = parse_d4j_test_stdout(
        "Running ant (compile.tests)................ FAIL\nBUILD FAILED\nFoo.java:3: error: ';' expected"
    )
    assert c_fail is True

    failing_file = (
        "--- org.example.FooTest::testOne\n"
        "junit.framework.AssertionFailedError: expected:<1> but was:<-1>\n"
        "    line2 of message\n"
        "\tat org.example.FooTest.testOne(FooTest.java:15)\n"
        "--- org.example.FooTest::testTwo\n"
        "java.lang.NullPointerException\n"
        "\tat org.example.Foo.calc(Foo.java:8)\n"
        "--- malformed_no_colon_colon\n"
        "java.lang.IllegalStateException: boom\n"
    )
    details = parse_failing_tests_file(failing_file)
    assert len(details) == 3
    assert details[0].test_class == "org.example.FooTest"
    assert details[0].test_method == "testOne"
    assert details[0].exception_type == "junit.framework.AssertionFailedError"
    assert "line2 of message" in details[0].message
    assert details[1].exception_type == "java.lang.NullPointerException"
    assert details[2].full_name == "malformed_no_colon_colon"


def test_runner_validates_test_selectors_and_run_on_patch_always_rolls_back():
    env = _FakeD4jDockerEnv()
    backend, runner = _make_setup(env)
    runner.checkout("Lang-12")

    with pytest.raises(SandboxError):
        runner.run_tests(single_test="FooTest; rm -rf /")
    with pytest.raises(SandboxError):
        runner.run_tests(single_test="FooTest::m1", test_class="FooTest")

    patch_text = "--- a/src/main/java/org/example/Foo.java\n+++ b/src/main/java/org/example/Foo.java\n@@ -4 +4 @@\n-if (x < 0)\n+if (x <= 0)\n"
    with pytest.raises(RuntimeError, match="boom"):
        runner.run_on_patch(patch_text, lambda: (_ for _ in ()).throw(RuntimeError("boom")))
    assert backend.state == "prepatch"


def test_junit_injector_runs_parses_structured_cases_and_cleans_up_in_postpatch():
    env = _FakeD4jDockerEnv()
    backend, runner = _make_setup(env)
    layout = runner.checkout("Lang-12")

    # Simulate candidate patch applied (Judge mode).
    ok, _ = backend.apply_patch(
        "--- a/src/main/java/org/example/Foo.java\n+++ b/src/main/java/org/example/Foo.java\n@@ -4 +4 @@\n-x < 0\n+x <= 0\n"
    )
    assert ok is True
    assert backend.state == "postpatch"

    spec = InjectedTestSpec(
        package_name="org.example",
        class_name="SpecGuardTmpTest",
        java_source="package org.example;\npublic class SpecGuardTmpTest { public void testA() {} }\n",
        test_methods=["testA"],
    )
    probe = AnchorProbeSpec(
        probe_id="p1",
        file_path="src/main/java/org/example/Foo.java",
        line_number=4,
        position="before",
        observed_expressions=["x"],
        condition_expr="x == 0",
    )

    env.test_stdout = (
        f"{CASE_PREFIX} " + '{"case_id":"w1","status":"passed","observed":"1"}\n'
        f"{CASE_PREFIX} " + '{"case_id":"w2","status":"failed","expected":"2","actual":"0"}\n'
        f"{PROBE_PREFIX} " + '{"probe_id":"p1","file":"src/main/java/org/example/Foo.java","line":4,"cond":true,"values":{"x":"0"}}\n'
        "Failing tests: 1\n"
        "  - org.example.SpecGuardTmpTest::testA\n"
    )

    injector = JUnitInjector(backend, runner)
    original_foo = env.files["/workspace/d4j_work/Lang_12b/src/main/java/org/example/Foo.java"]
    result = injector.run_injected_test(spec, layout, tier="tier_a", probes=[probe])

    assert isinstance(result, HarnessRunResult)
    assert result.compiled is True
    assert len(result.passed_cases) == 1
    assert len(result.failed_cases) == 1
    assert len(result.infection_probe_traces) == 1
    assert result.infection_probe_traces[0]["cond"] is True

    # Crucial checks: injected .java & .class removed, Foo.java restored verbatim, and postpatch state preserved!
    assert "/workspace/d4j_work/Lang_12b/src/test/java/org/example/SpecGuardTmpTest.java" not in env.files
    assert (
        "/workspace/d4j_work/Lang_12b/target/test-classes/org/example/SpecGuardTmpTest.class"
        not in env.files
    )
    assert env.files["/workspace/d4j_work/Lang_12b/src/main/java/org/example/Foo.java"] == original_foo
    assert backend.state == "postpatch"


def test_junit_injector_cleanup_on_compile_failure_and_exception():
    env = _FakeD4jDockerEnv()
    backend, runner = _make_setup(env)
    layout = runner.checkout("Lang-12")
    injector = JUnitInjector(backend, runner)

    spec = InjectedTestSpec(
        package_name="org.example",
        class_name="SpecGuardTmpTest",
        java_source="package org.example;\npublic class SpecGuardTmpTest {}\n",
    )
    probe = AnchorProbeSpec(
        probe_id="p_err",
        file_path="src/main/java/org/example/Foo.java",
        line_number=4,
        observed_expressions=["x"],
    )

    # 1) Compile failure still cleans up test and reverts probe.
    env.compile_rc = 1
    env.compile_stderr = "SpecGuardTmpTest.java:1: error: bad syntax"
    original_foo = env.files["/workspace/d4j_work/Lang_12b/src/main/java/org/example/Foo.java"]
    res = injector.run_injected_test(spec, layout, probes=[probe])
    assert res.compiled is False
    assert res.error_cases[0]["stage"] == "compile"
    assert "/workspace/d4j_work/Lang_12b/src/test/java/org/example/SpecGuardTmpTest.java" not in env.files
    assert env.files["/workspace/d4j_work/Lang_12b/src/main/java/org/example/Foo.java"] == original_foo

    # 2) Exception during defects4j test still cleans up and propagates original exception.
    env.compile_rc = 0
    env.raise_on_test = True
    with pytest.raises(RuntimeError, match="Simulated container crash"):
        injector.run_injected_test(spec, layout, probes=[probe])
    assert "/workspace/d4j_work/Lang_12b/src/test/java/org/example/SpecGuardTmpTest.java" not in env.files
    assert env.files["/workspace/d4j_work/Lang_12b/src/main/java/org/example/Foo.java"] == original_foo


def test_junit_injector_refuses_clobbering_existing_test():
    env = _FakeD4jDockerEnv()
    backend, runner = _make_setup(env)
    layout = runner.checkout("Lang-12")
    env.files["/workspace/d4j_work/Lang_12b/src/test/java/org/example/ExistingTest.java"] = b"class ExistingTest {}\n"

    spec = InjectedTestSpec(
        package_name="org.example",
        class_name="ExistingTest",
        java_source="package org.example;\npublic class ExistingTest {}\n",
    )
    injector = JUnitInjector(backend, runner)
    with pytest.raises(SandboxError, match="Refusing to overwrite"):
        injector.inject_test(spec, layout)


def test_readonly_anchor_probe_validation_instrumentation_and_malformed_logs():
    injector = ReadOnlyAnchorProbeInjector()
    assert isinstance(injector, AnchorProbeInjector)

    for bad_expr in ("x = 1", "x++", "--y", "a += 1", "foo(); bar()", "if (x) {}", "x // comment"):
        with pytest.raises(SandboxError):
            AnchorProbeSpec(
                probe_id="bad",
                file_path="src/Foo.java",
                line_number=1,
                observed_expressions=[bad_expr],
            )

    p_before = AnchorProbeSpec(
        probe_id="p_before",
        file_path="src/Foo.java",
        line_number=4,
        position="before",
        observed_expressions=["x", '"a=1"'],
        condition_expr="x == 0 && x != -1 && x <= 10",
    )
    p_after = AnchorProbeSpec(
        probe_id="p_after",
        file_path="src/Foo.java",
        line_number=4,
        position="after",
        observed_expressions=["x"],
    )

    source = (
        "package org.example;\n"
        "public class Foo {\n"
        "    int calc(int x) {\n"
        "        return x + 1;\n"
        "    }\n"
        "}\n"
    )
    instrumented = injector.instrument_source(source, [p_before, p_after])
    lines = instrumented.splitlines()
    assert len(lines) == 8
    assert "p_before" in lines[3]
    assert "return x + 1;" in lines[4]
    assert "p_after" in lines[5]

    traces = parse_probe_traces(
        f"{PROBE_PREFIX} {{\"probe_id\":\"ok\"}}\n"
        f"{PROBE_PREFIX} {{broken_json\n"
    )
    assert len(traces) == 2
    assert traces[0]["probe_id"] == "ok"
    assert "parse_error" in traces[1]

    passed, failed, errors = parse_case_records(
        f"{CASE_PREFIX} {{\"status\":\"passed\",\"case_id\":\"1\"}}\n"
        f"{CASE_PREFIX} {{\"status\":\"failed\",\"case_id\":\"2\"}}\n"
        f"{CASE_PREFIX} {{bad\n"
    )
    assert len(passed) == 1
    assert len(failed) == 1
    assert len(errors) == 1


def test_server_config_markers_and_docker_backend_parity():
    markers = scan_server_config_markers(PROJECT_ROOT / "specpatchguard")
    assert len(markers) == 9
    backend_text = (PROJECT_ROOT / "specpatchguard" / "sandbox" / "docker_backend.py").read_text(encoding="utf-8")
    assert "SERVER_CONFIG_REQUIRED" in backend_text


def test_review_regressions_junit_direct_braceless_probes_and_partial_crash():
    # CRIT-01: JUnitCore parser & direct execution preserves passing stdout/stderr and runs methods.
    ok_count, ok_tests, ok_details = parse_junit_core_stdout("JUnit version 4.12\n.\nTime: 0.01\n\nOK (1 test)\n")
    assert ok_count == 0
    assert ok_tests == []
    assert ok_details == []

    fail_out = (
        "JUnit version 4.12\n.E\n"
        "1) testZero(org.example.SpecGuardTmpTest)\n"
        "java.lang.AssertionError: expected:<1> but was:<0>\n"
        "\tat org.example.SpecGuardTmpTest.testZero(SpecGuardTmpTest.java:12)\n"
        "FAILURES!!!\n"
        "Tests run: 2,  Failures: 1\n"
    )
    f_count, f_tests, f_details = parse_junit_core_stdout(fail_out)
    assert f_count == 1
    assert f_tests == ["org.example.SpecGuardTmpTest::testZero"]
    assert f_details[0].exception_type == "java.lang.AssertionError"

    env = _FakeD4jDockerEnv()
    backend, runner = _make_setup(env)
    layout = runner.checkout("Lang-12")
    # WARN-01: required property exports must chain with && before printf END.
    export_script = next(
        cmd[-1] for cmd in env.commands if cmd[-1].startswith("if command -v timeout") and "__SPG_PROP_dir.src.classes_START__" in cmd[-1]
    )
    assert "defects4j export -w /workspace/d4j_work/Lang_12b -p dir.src.classes && printf" in export_script

    injector = JUnitInjector(backend, runner)
    spec = InjectedTestSpec(
        package_name="org.example",
        class_name="SpecGuardTmpTest",
        java_source="package org.example;\npublic class SpecGuardTmpTest { public void m1() {} public void m2() {} }\n",
        test_methods=["m1", "m2"],
    )

    # CRIT-01: execution_mode="d4j" invokes defects4j test separately for each spec.test_methods entry.
    env.test_stdout = "Failing tests: 0\n"
    d4j_res = injector.run_injected_test(spec, layout, execution_mode="d4j")
    assert d4j_res.compiled is True
    assert len(d4j_res.passed_cases) == 2
    d4j_cmds = [cmd[-1] for cmd in env.commands if "defects4j test" in cmd[-1]]
    assert any("org.example.SpecGuardTmpTest::m1" in c for c in d4j_cmds)
    assert any("org.example.SpecGuardTmpTest::m2" in c for c in d4j_cmds)

    # CRIT-04: partial passing CASE record + mid-run timeout must NOT drop the timeout error!
    env.test_timed_out = True
    env.test_stdout = f'{CASE_PREFIX} {{"case_id":"w1","status":"passed"}}\n'
    partial_timeout = injector.run_injected_test(spec, layout, execution_mode="junit_direct")
    assert len(partial_timeout.passed_cases) == 1
    assert any(err.get("timed_out") is True for err in partial_timeout.error_cases)

    # WARN-02: Ant compile OK followed by runtime BUILD FAILED must stay compiled=True.
    _, _, compile_failed = parse_d4j_test_stdout(
        "Running ant (compile).......... OK\n"
        "Running ant (compile.tests).... OK\n"
        "Running ant (run.dev.tests).... FAIL\n"
        "BUILD FAILED\n"
    )
    assert compile_failed is False

    # CRIT-03 & WARN-06: braceless single-statement control body wrapped safely; multi-line rejected; \u003b rejected.
    probe_inj = ReadOnlyAnchorProbeInjector()
    with pytest.raises(SandboxError, match="Unicode escapes"):
        AnchorProbeSpec(probe_id="u", file_path="src/Foo.java", line_number=1, observed_expressions=["x \\u003b y"])

    braceless_src = (
        "class Foo {\n"
        "    int f(int x) {\n"
        "        if (x < 0)\n"
        "            return -1;\n"
        "        return x;\n"
        "    }\n"
        "}\n"
    )
    p = AnchorProbeSpec(
        probe_id="p_braceless",
        file_path="src/Foo.java",
        line_number=4,
        position="before",
        observed_expressions=["x"],
    )
    wrapped = probe_inj.instrument_source(braceless_src, [p])
    wrapped_line = wrapped.splitlines()[3]
    assert wrapped_line.lstrip().startswith("{ try {")
    assert wrapped_line.rstrip().endswith("return -1; }")
    assert "String.valueOf((Object) (x))" in wrapped_line
    assert '.replace("\\t", "\\\\t")' in wrapped_line

    multiline_braceless = (
        "class Foo {\n"
        "    void f(int x) {\n"
        "        if (x < 0)\n"
        "            foo(\n"
        "                x);\n"
        "    }\n"
        "}\n"
    )
    with pytest.raises(SandboxError, match="multi-line braceless"):
        probe_inj.instrument_source(
            multiline_braceless,
            [AnchorProbeSpec(probe_id="p_ml", file_path="src/Foo.java", line_number=4, observed_expressions=["x"])],
        )
