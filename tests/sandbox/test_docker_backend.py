"""Unit tests for `DockerSandboxBackend` lifecycle, file I/O, patch apply/rollback, and Step 4 bridges."""

from __future__ import annotations

from pathlib import Path
import sys
from typing import Any

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from specpatchguard.analysis.lsp_client import DockerJdtlsTransport  # noqa: E402
from specpatchguard.analysis.protocols import DockerExecExecutor  # noqa: E402
from specpatchguard.core.config_loader import load_settings  # noqa: E402
from specpatchguard.sandbox import (  # noqa: E402
    DockerSandboxBackend,
    PatchApplyError,
    SandboxBackend,
    SandboxError,
    detect_patch_strip_level,
    extract_patch_added_files,
)


class _FakeDockerEnv:
    """In-memory fake Docker CLI runner simulating container lifecycle, files, and git/patch state."""

    def __init__(self) -> None:
        self.commands: list[list[str]] = []
        self.stdin_writes: list[tuple[list[str], bytes]] = []
        self.files: dict[str, bytes] = {
            "/workspace/d4j_work/Lang_12b/.defects4j.config": b"pid=Lang\nvid=12b\n",
            "/workspace/d4j_work/Lang_12b/defects4j.build.properties": b"d4j.home=/data/public/defects4j\n",
            "/workspace/d4j_work/Lang_12b/src/main/java/Foo.java": b"class Foo { int x = 1; }\n",
        }
        self.apply_rc_by_cmd: dict[str, int] = {}
        self.started_containers: list[str] = []
        self.removed_containers: list[str] = []
        self.attached_running: bool = True

    def run(self, argv: list[str], timeout: int) -> tuple[int, str, str]:
        self.commands.append(list(argv))
        if argv[:2] == ["docker", "run"]:
            name_idx = argv.index("--name") + 1
            cname = argv[name_idx]
            self.started_containers.append(cname)
            return 0, f"{cname}-cid\n", ""
        if argv[:2] == ["docker", "inspect"]:
            return (0, "true\n", "") if self.attached_running else (0, "false\n", "")
        if argv[:3] == ["docker", "rm", "-f"]:
            self.removed_containers.append(argv[3])
            return 0, "", ""
        if argv[:2] == ["docker", "exec"]:
            workdir = ""
            idx = 2
            if argv[idx] == "-w":
                workdir = argv[idx + 1]
                idx += 2
            _container = argv[idx]
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
            if inner[:2] == ["sh", "-c"]:
                script = inner[2]
                for pattern, rc in self.apply_rc_by_cmd.items():
                    if pattern in script:
                        if rc != 0:
                            return rc, "", f"failed: {pattern}"
                        return 0, "applied", ""
                return 0, "", ""
        return 0, "", ""

    def run_stdin(self, argv: list[str], input_bytes: bytes, timeout: int) -> tuple[int, str, str]:
        self.stdin_writes.append((list(argv), input_bytes))
        workdir = ""
        idx = 2
        if argv[idx] == "-i":
            idx += 1
        if argv[idx] == "-w":
            workdir = argv[idx + 1]
            idx += 2
        _container = argv[idx]
        inner = argv[idx + 1 :]
        if inner[:2] == ["sh", "-c"]:
            cmd = inner[2]
            if "cat > " in cmd:
                target_raw = cmd.split("cat > ", 1)[1].strip().strip("'\"")
                target = target_raw if target_raw.startswith("/") else f"{workdir}/{target_raw}"
                self.files[target] = input_bytes
                return 0, "", ""
        return 1, "", "Unsupported stdin command"


def _make_backend(env: _FakeDockerEnv, **kwargs: Any) -> DockerSandboxBackend:
    return DockerSandboxBackend(
        image_name="d4j-lsp-runtime:latest",
        container_work_root="/workspace/d4j_work",
        host_mount_root="/data/patch_va/d4j_work",
        d4j_home="/data/public/defects4j",
        java8_home="/workspace/envs/defects4j",
        timeout_seconds=900,
        nano_cpus=8_000_000_000,
        runner=env.run,
        stdin_runner=env.run_stdin,
        **kwargs,
    )


def test_from_settings_and_protocol_conformance():
    settings = load_settings(PROJECT_ROOT / "config" / "config.json")
    env = _FakeDockerEnv()
    backend = DockerSandboxBackend.from_settings(settings, runner=env.run, stdin_runner=env.run_stdin)
    assert isinstance(backend, SandboxBackend)
    assert backend.image_name == "d4j-lsp-runtime:latest"
    assert backend.container_work_root == "/workspace/d4j_work"
    assert backend.d4j_home == "/data/public/defects4j"
    assert backend.java8_home == "/workspace/envs/defects4j"


def test_managed_container_lifecycle_mounts_cpus_and_cleans_up():
    env = _FakeDockerEnv()
    backend = _make_backend(env)
    with backend:
        assert backend.container_name is not None
        assert len(env.started_containers) == 1
        run_cmd = env.commands[0]
        assert "--cpus" in run_cmd
        assert run_cmd[run_cmd.index("--cpus") + 1] == "8"
        assert "-v" in run_cmd
        assert run_cmd[run_cmd.index("-v") + 1] == "/data/patch_va/d4j_work:/workspace/d4j_work"
        backend.set_active_workdir("/workspace/d4j_work/Lang_12b")
        backend.stage_patch("--- a/Foo.java\n+++ b/Foo.java\n@@ -1 +1 @@\n-a\n+b")
        assert any(k.startswith("/tmp/specpatchguard_") and k.endswith(".patch") for k in env.files)

    assert backend.container_name is None
    assert len(env.removed_containers) == 1
    assert not any(k.startswith("/tmp/specpatchguard_") for k in env.files)


def test_attach_container_never_removes_external_container():
    env = _FakeDockerEnv()
    backend = _make_backend(env, attach_container="shared-d4j-box")
    assert backend.start() == "shared-d4j-box"
    backend.destroy()
    assert env.removed_containers == []

    env.attached_running = False
    with pytest.raises(SandboxError, match="not running"):
        _make_backend(env, attach_container="shared-d4j-box").start()


def test_exec_bash_d4j_env_and_inner_timeout_handling():
    env = _FakeDockerEnv()
    backend = _make_backend(env)
    backend.start()
    backend.set_active_workdir("/workspace/d4j_work/Lang_12b")

    res = backend.exec_bash("defects4j info -p Lang -b 12", timeout=42, with_d4j_env=True)
    assert res.returncode == 0
    assert res.timed_out is False
    last_cmd = env.commands[-1]
    assert last_cmd[:5] == ["docker", "exec", "-w", "/workspace/d4j_work/Lang_12b", backend.container_name]
    shell_text = last_cmd[-1]
    assert "timeout -k 10 42" in shell_text
    assert "JAVA_HOME=/workspace/envs/defects4j" in shell_text
    assert "/data/public/defects4j/framework/bin" in shell_text

    env.apply_rc_by_cmd["hung_cmd"] = 124
    timed = backend.exec_bash("hung_cmd", timeout=5, use_inner_timeout=True)
    assert timed.timed_out is True


def test_write_file_uses_stdin_and_rejects_escape_and_git():
    env = _FakeDockerEnv()
    backend = _make_backend(env)
    backend.start()
    backend.set_active_workdir("/workspace/d4j_work/Lang_12b")

    rel = backend.write_file("src/test/java/TmpTest.java", "class TmpTest {}\n", track_injected=True)
    assert rel == "src/test/java/TmpTest.java"
    assert env.files["/workspace/d4j_work/Lang_12b/src/test/java/TmpTest.java"] == b"class TmpTest {}\n"

    for bad in ("../escape.java", "/etc/passwd", ".git/config", "a\nb.java"):
        with pytest.raises(SandboxError):
            backend.write_file(bad, "x")


def test_detect_patch_strip_level_variants():
    p1 = "diff --git a/src/Foo.java b/src/Foo.java\n--- a/src/Foo.java\n+++ b/src/Foo.java\n"
    p0 = "--- src/Foo.java\t2026-01-01\n+++ src/Foo.java\t2026-01-01\n"
    p1_add = "--- /dev/null\n+++ b/src/NewFoo.java\n"
    p0_add = "--- /dev/null\n+++ src/NewFoo.java\n"
    assert detect_patch_strip_level(p1) == 1
    assert detect_patch_strip_level(p0) == 0
    assert detect_patch_strip_level(p1_add) == 1
    assert detect_patch_strip_level(p0_add) == 0


def test_stage_patch_outside_workdir_fallback_apply_and_safe_restore_preserves_d4j_meta():
    env = _FakeDockerEnv()
    # First two apply strategies fail; patch --fuzz=3 succeeds.
    env.apply_rc_by_cmd["git apply --verbose -p1"] = 1
    env.apply_rc_by_cmd["git apply --verbose --ignore-whitespace -p1"] = 1
    env.apply_rc_by_cmd["patch --batch --no-backup-if-mismatch --fuzz=3 -p1 -i"] = 0

    backend = _make_backend(env)
    backend.start()
    backend.set_active_workdir("/workspace/d4j_work/Lang_12b", base_ref="D4J_Lang_12_BUGGY_VERSION")
    backend.write_file("src/test/java/TempHelperTest.java", "class TempHelperTest {}\n", track_injected=True)

    staged = backend.stage_patch("--- a/src/main/java/Foo.java\n+++ b/src/main/java/Foo.java\n@@ -1 +1 @@\n-1\n+2")
    assert not staged.startswith("/workspace/d4j_work/Lang_12b")
    assert staged.startswith("/tmp/specpatchguard_")

    ok, method = backend.apply_patch()
    assert ok is True
    assert "patch --batch" in method
    assert backend.state == "postpatch"

    # Attempting to apply again in postpatch must raise PatchApplyError.
    with pytest.raises(PatchApplyError):
        backend.apply_patch()

    # switch_state("prepatch") preserves injected test file, whereas restore_prepatch() removes it.
    assert backend.switch_state("prepatch") is True
    assert backend.state == "prepatch"
    assert "/workspace/d4j_work/Lang_12b/src/test/java/TempHelperTest.java" in env.files

    backend.restore_prepatch(remove_injected=True)
    assert "/workspace/d4j_work/Lang_12b/src/test/java/TempHelperTest.java" not in env.files
    # Crucial: .defects4j.config and defects4j.build.properties must NEVER be deleted by bare git clean -fd.
    assert "/workspace/d4j_work/Lang_12b/.defects4j.config" in env.files
    assert "/workspace/d4j_work/Lang_12b/defects4j.build.properties" in env.files
    assert not any("git clean" in " ".join(cmd) for cmd in env.commands)

    # All apply strategies failing guarantees state remains prepatch.
    env.apply_rc_by_cmd["patch --batch --no-backup-if-mismatch --fuzz=3 -p1 -i"] = 1
    ok2, err_detail = backend.apply_patch("--- a/src/main/java/Foo.java\n+++ b/src/main/java/Foo.java\n@@ -1 +1 @@\n-1\n+2\n")
    assert ok2 is False
    assert "patch --batch" in err_detail
    assert backend.state == "prepatch"


def test_step4_bridges_enforce_patch_blind_and_scope_jdtls_workspace(monkeypatch: pytest.MonkeyPatch):
    env = _FakeDockerEnv()
    backend = _make_backend(env)
    backend.start()
    backend.set_active_workdir("/workspace/d4j_work/Lang_12b")

    executor = backend.create_repo_executor(readonly_prepatch=True)
    assert isinstance(executor, DockerExecExecutor)
    assert executor.work_root == "/workspace/d4j_work/Lang_12b"
    assert executor.state == "prepatch"

    backend.state = "postpatch"
    with pytest.raises(SandboxError, match="Patch-Blind"):
        backend.create_repo_executor(readonly_prepatch=True)

    captured: dict[str, Any] = {}

    class _FakeTransport:
        def __init__(self, container_name: str, lsp_cfg: dict[str, Any], work_root: str) -> None:
            captured["container_name"] = container_name
            captured["lsp_cfg"] = lsp_cfg
            captured["work_root"] = work_root

    monkeypatch.setattr("specpatchguard.sandbox.docker_backend.DockerJdtlsTransport", _FakeTransport)
    lsp_cfg = load_settings(PROJECT_ROOT / "config" / "config.json").lsp
    transport = backend.create_jdtls_transport(lsp_cfg, workspace_key="Lang/12b")
    assert isinstance(transport, _FakeTransport)
    assert captured["container_name"] == backend.container_name
    assert captured["work_root"] == "/workspace/d4j_work/Lang_12b"
    assert captured["lsp_cfg"]["jdtls_workspace_cache"] == "/tmp/jdtls_ws_cache/Lang_12b"
    # Original config dict must not be mutated.
    assert lsp_cfg["jdtls_workspace_cache"] == "/tmp/jdtls_ws_cache"


def test_review_regressions_added_file_cleanup_protected_writes_and_timeout_137(monkeypatch: pytest.MonkeyPatch):
    env = _FakeDockerEnv()
    backend = _make_backend(env)
    backend.start()
    backend.set_active_workdir("/workspace/d4j_work/Lang_12b", base_ref="D4J_Lang_12_BUGGY_VERSION")

    # WARN-03: write_file must reject .defects4j.config and defects4j.build.properties.
    for protected in (".defects4j.config", "defects4j.build.properties"):
        with pytest.raises(SandboxError, match="protected"):
            backend.write_file(protected, "tampered")

    # CRIT-02: added-file patch creates tracked cleanup list and deletes newly created files on rollback/failure.
    added_patch = (
        "diff --git a/src/main/java/NewHelper.java b/src/main/java/NewHelper.java\n"
        "new file mode 100644\n"
        "--- /dev/null\n"
        "+++ b/src/main/java/NewHelper.java\n"
        "@@ -0,0 +1 @@\n"
        "+class NewHelper {}\n"
    )
    assert extract_patch_added_files(added_patch) == ["src/main/java/NewHelper.java"]

    env.files["/workspace/d4j_work/Lang_12b/src/main/java/NewHelper.java"] = b"class NewHelper {}\n"
    backend.stage_patch(added_patch)
    ok, _ = backend.apply_patch()
    assert ok is True
    backend.restore_prepatch(remove_injected=True)
    assert "/workspace/d4j_work/Lang_12b/src/main/java/NewHelper.java" not in env.files
    assert "/workspace/d4j_work/Lang_12b/.defects4j.config" in env.files

    # CRIT-02: switching active workdir cleans up previous /tmp/specpatchguard_*.patch in container.
    staged = backend.stage_patch(added_patch)
    assert staged in env.files
    backend.set_active_workdir("/workspace/d4j_work/Lang_13b")
    assert staged not in env.files

    # WARN-05: with_d4j_env wraps script in brace block to preserve operator precedence.
    backend.exec_bash("false || echo fallback_ok", with_d4j_env=True)
    last_shell = env.commands[-1][-1]
    assert "{\nfalse || echo fallback_ok\n}" in last_shell

    # WARN-04: rc==137 with partial stdout and elapsed >= timeout is flagged as timed_out=True.
    ticks = iter([100.0, 106.0])
    monkeypatch.setattr("specpatchguard.sandbox.docker_backend.time.monotonic", lambda: next(ticks))

    def _sigkill_runner(argv: list[str], timeout: int) -> tuple[int, str, str]:
        return 137, "partial stdout before SIGKILL\n", ""

    backend._runner = _sigkill_runner
    timed = backend.exec_bash("while true; do echo hi; done", timeout=5, use_inner_timeout=True)
    assert timed.timed_out is True
    assert "partial stdout" in timed.stdout
