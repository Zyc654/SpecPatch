"""Unified Docker container lifecycle, file I/O, patch staging/rollback, and Step 4 bridges."""

from __future__ import annotations

from pathlib import PurePosixPath
import re
import shlex
import subprocess
import time
from typing import Any, Callable, Literal, Protocol, runtime_checkable
import uuid

from ..analysis.lsp_client import DockerJdtlsTransport
from ..analysis.protocols import (
    DEFAULT_COMMAND_TIMEOUT_SEC,
    DockerExecExecutor,
    ExecOutput,
    resolve_repo_rel_path,
)
from ..core.config_loader import Settings
from ..core.doctor import CommandRunner, default_runner

StdinCommandRunner = Callable[[list[str], bytes, int], tuple[int, str, str]]

GIT_APPLY_CMDS: tuple[str, ...] = (
    "git apply --verbose -p{strip}",
    "git apply --verbose --ignore-whitespace -p{strip}",
    "patch --batch --no-backup-if-mismatch --fuzz=3 -p{strip} -i",
)

GIT_APPLY_REVERSE_CMDS: tuple[str, ...] = (
    "git apply --verbose --reverse -p{strip}",
    "git apply --verbose --ignore-whitespace --reverse -p{strip}",
    "patch --batch --no-backup-if-mismatch --fuzz=3 -p{strip} -R -i",
)

_CONTAINER_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
_WS_KEY_SANITIZE_RE = re.compile(r"[^A-Za-z0-9_.-]+")
_PROTECTED_D4J_META = frozenset({".defects4j.config", "defects4j.build.properties"})


class SandboxError(RuntimeError):
    """Raised on Docker container lifecycle, command, or path-isolation errors."""


class PatchApplyError(SandboxError):
    """Raised when staging or applying a candidate patch fails or violates state guards."""


def default_stdin_runner(argv: list[str], input_bytes: bytes, timeout: int) -> tuple[int, str, str]:
    """Execute a subprocess with raw stdin bytes and safe timeout/missing-binary handling."""
    try:
        completed = subprocess.run(
            argv,
            input=input_bytes,
            capture_output=True,
            timeout=timeout,
            check=False,
        )
        stdout = (completed.stdout or b"").decode("utf-8", errors="replace")
        stderr = (completed.stderr or b"").decode("utf-8", errors="replace")
        return completed.returncode, stdout, stderr
    except FileNotFoundError as exc:
        return 127, "", f"Executable not found: {exc.filename or argv[0]}"
    except subprocess.TimeoutExpired:
        return 124, "", f"Command timed out after {timeout}s"
    except OSError as exc:
        return 126, "", f"OS error running {argv[0]}: {exc}"


def detect_patch_strip_level(patch_content: str) -> int:
    """Detect unified diff `-p` strip level (`1` for `a/`/`b/`, `0` for bare repo paths)."""
    saw_relative_zero = False
    for raw_line in patch_content.replace("\r\n", "\n").splitlines():
        if not raw_line.startswith(("--- ", "+++ ")):
            continue
        path_part = raw_line[4:].split("\t", 1)[0].strip()
        if not path_part or path_part == "/dev/null":
            continue
        if path_part.startswith(("a/", "b/")):
            return 1
        if not path_part.startswith("/"):
            saw_relative_zero = True
    return 0 if saw_relative_zero else 1


def extract_patch_added_files(patch_content: str) -> list[str]:
    """Extract repo-relative paths of files newly created by a unified diff (`--- /dev/null`)."""
    added: list[str] = []
    prev_dev_null = False
    for raw_line in patch_content.replace("\r\n", "\n").replace("\r", "\n").splitlines():
        if raw_line.startswith("--- "):
            old_part = raw_line[4:].split("\t", 1)[0].strip()
            prev_dev_null = old_part == "/dev/null"
            continue
        if raw_line.startswith("+++ "):
            if prev_dev_null:
                new_part = raw_line[4:].split("\t", 1)[0].strip()
                if new_part and new_part != "/dev/null":
                    cleaned = new_part.replace("\\", "/")
                    while cleaned.startswith("./"):
                        cleaned = cleaned[2:]
                    if cleaned.startswith(("a/", "b/")):
                        cleaned = cleaned[2:]
                    try:
                        safe_rel = resolve_repo_rel_path(cleaned, readonly_prepatch=False)
                    except Exception:
                        safe_rel = ""
                    if (
                        safe_rel
                        and ".git" not in PurePosixPath(safe_rel).parts
                        and safe_rel not in _PROTECTED_D4J_META
                        and safe_rel not in added
                    ):
                        added.append(safe_rel)
            prev_dev_null = False
            continue
        if not raw_line.startswith(("diff ", "index ", "new file ", "@@ ")):
            prev_dev_null = False
    return added


def _wrap_inner_timeout(shell_cmd: str, timeout: int) -> str:
    """Wrap container bash script with GNU `timeout -k 10` so hung JVM processes are killed cleanly."""
    secs = max(1, int(timeout))
    return (
        f"if command -v timeout >/dev/null 2>&1; then "
        f"timeout -k 10 {secs} bash -lc {shlex.quote(shell_cmd)}; "
        f"else bash -lc {shlex.quote(shell_cmd)}; fi"
    )


def _normalize_patch_text(patch_content: str) -> str:
    normalized = patch_content.replace("\r\n", "\n").replace("\r", "\n")
    if not normalized.endswith("\n"):
        normalized += "\n"
    return normalized


def _validate_posix_abs(field_name: str, value: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise SandboxError(f"{field_name} cannot be empty")
    if "\n" in value or "\r" in value or "\x00" in value:
        raise SandboxError(f"{field_name} cannot contain newline or NUL characters")
    cleaned = value.strip()
    if not cleaned.startswith("/"):
        raise SandboxError(f"{field_name} must be an absolute POSIX path, got: {value!r}")
    normalized = PurePosixPath(cleaned).as_posix()
    return normalized.rstrip("/") or "/"


@runtime_checkable
class SandboxBackend(Protocol):
    """Unified container sandbox interface shared by Defects4J runner, JUnit injector, and Step 4 tools."""

    container_name: str | None
    container_work_root: str
    active_work_root: str | None
    state: Literal["prepatch", "postpatch"]

    def start(self) -> str: ...

    def destroy(self) -> None: ...

    def set_active_workdir(
        self,
        work_root: str,
        *,
        base_ref: str | None = None,
        state: Literal["prepatch", "postpatch"] = "prepatch",
    ) -> None: ...

    def exec_command(
        self,
        argv: list[str],
        *,
        workdir: str | None = None,
        timeout: int | None = None,
        use_inner_timeout: bool = False,
    ) -> ExecOutput: ...

    def exec_bash(
        self,
        script: str,
        *,
        workdir: str | None = None,
        timeout: int | None = None,
        with_d4j_env: bool = False,
        use_inner_timeout: bool = True,
    ) -> ExecOutput: ...

    def read_file(self, rel_path: str, *, readonly_prepatch: bool = False) -> str: ...

    def write_file(
        self,
        rel_path: str,
        content: str | bytes,
        *,
        track_injected: bool = False,
    ) -> str: ...

    def file_exists(self, rel_path: str) -> bool: ...

    def remove_file(self, rel_path: str) -> bool: ...

    def stage_patch(self, patch_content: str) -> str: ...

    def apply_patch(self, patch_content: str | None = None) -> tuple[bool, str]: ...

    def cleanup_injected_files(self, *, extra_paths: list[str] | None = None) -> list[str]: ...

    def restore_prepatch(self, *, remove_injected: bool = True) -> bool: ...

    def switch_state(self, target_state: Literal["prepatch", "postpatch"]) -> bool: ...


class DockerSandboxBackend:
    """Docker CLI-backed sandbox managing container lifecycle, D4J env, patch apply, and rollback.

    Lifecycle & state-switching logic adapts `AutoCodeSherpa/explanation-audit-agent/backend.py`.
    """

    def __init__(
        self,
        image_name: str,
        container_work_root: str,
        host_mount_root: str,
        d4j_home: str,
        java8_home: str,
        *,
        timeout_seconds: int = DEFAULT_COMMAND_TIMEOUT_SEC,
        nano_cpus: int = 8_000_000_000,
        attach_container: str | None = None,
        mount_host_dir: bool = True,
        container_prefix: str = "specpatchguard",
        runner: CommandRunner | None = None,
        stdin_runner: StdinCommandRunner | None = None,
    ) -> None:
        if not isinstance(image_name, str) or not image_name.strip() or any(ch.isspace() for ch in image_name.strip()):
            raise SandboxError("image_name must be a non-empty tag without whitespace")
        if not isinstance(host_mount_root, str) or not host_mount_root.strip():
            raise SandboxError("host_mount_root cannot be empty")
        if type(timeout_seconds) is not int or timeout_seconds < 1:
            raise SandboxError("timeout_seconds must be an integer >= 1")
        if type(nano_cpus) is not int or nano_cpus < 1:
            raise SandboxError("nano_cpus must be an integer >= 1")

        # Docker/D4J settings are validated and marked with SERVER_CONFIG_REQUIRED in core/config_loader.py.
        self.image_name = image_name.strip()
        self.container_work_root = _validate_posix_abs("container_work_root", container_work_root)
        self.host_mount_root = host_mount_root.strip()
        self.d4j_home = _validate_posix_abs("d4j_home", d4j_home)
        self.java8_home = _validate_posix_abs("java8_home", java8_home)
        self.timeout_seconds = timeout_seconds
        self.nano_cpus = nano_cpus
        self.mount_host_dir = mount_host_dir

        safe_prefix = re.sub(r"[^A-Za-z0-9_.-]+", "-", container_prefix.strip()) or "specpatchguard"
        self.container_prefix = safe_prefix.strip("-.") or "specpatchguard"

        if attach_container is not None:
            cleaned_attach = attach_container.strip()
            if not cleaned_attach or not _CONTAINER_NAME_RE.fullmatch(cleaned_attach):
                raise SandboxError(f"Invalid attach_container name: {attach_container!r}")
            self._attach_container: str | None = cleaned_attach
        else:
            self._attach_container = None

        self._runner: CommandRunner = runner or default_runner
        self._stdin_runner: StdinCommandRunner = stdin_runner or default_stdin_runner

        self.container_name: str | None = None
        self.active_work_root: str | None = None
        self.base_ref: str | None = None
        self.state: Literal["prepatch", "postpatch"] = "prepatch"

        self._owns_container: bool = False
        self._run_id: str = uuid.uuid4().hex[:12]
        self._staged_patch_path: str | None = None
        self._staged_patch_content: str | None = None
        self._patch_created_files: list[str] = []
        self._injected_files: list[str] = []

    @classmethod
    def from_settings(
        cls,
        settings: Settings,
        *,
        attach_container: str | None = None,
        mount_host_dir: bool = True,
        runner: CommandRunner | None = None,
        stdin_runner: StdinCommandRunner | None = None,
    ) -> DockerSandboxBackend:
        """Construct a `DockerSandboxBackend` strictly from validated `Settings`."""
        # Docker/D4J settings are validated and marked with SERVER_CONFIG_REQUIRED in core/config_loader.py.
        return cls(
            image_name=settings.docker["image_name"],
            container_work_root=settings.docker["container_work_root"],
            host_mount_root=settings.docker["host_mount_root"],
            d4j_home=settings.defects4j["d4j_home"],
            java8_home=settings.defects4j["java8_home"],
            timeout_seconds=settings.docker["timeout_seconds"],
            nano_cpus=settings.docker["nano_cpus"],
            attach_container=attach_container,
            mount_host_dir=mount_host_dir,
            runner=runner,
            stdin_runner=stdin_runner,
        )

    def __enter__(self) -> DockerSandboxBackend:
        self.start()
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.destroy()

    def _require_started(self) -> str:
        if not self.container_name:
            raise SandboxError("Docker sandbox container is not started; call start() first")
        return self.container_name

    def _require_workdir(self) -> str:
        self._require_started()
        if not self.active_work_root:
            raise SandboxError("No active workdir configured; call checkout() or set_active_workdir() first")
        return self.active_work_root

    def _effective_workdir(self, workdir: str | None) -> str:
        self._require_started()
        if workdir is not None:
            return _validate_posix_abs("workdir", workdir)
        return self.active_work_root or self.container_work_root

    def start(self) -> str:
        """Start a managed Docker container or attach to an existing running container."""
        if self.container_name is not None:
            return self.container_name

        if self._attach_container is not None:
            rc, out, err = self._runner(
                ["docker", "inspect", "-f", "{{.State.Running}}", self._attach_container],
                min(30, self.timeout_seconds),
            )
            if rc != 0 or out.strip().lower() != "true":
                raise SandboxError(
                    f"Attached container '{self._attach_container}' is not running: "
                    f"{err.strip() or out.strip() or f'rc={rc}'}"
                )
            self.container_name = self._attach_container
            self._owns_container = False
            return self.container_name

        candidate_name = f"{self.container_prefix}-{self._run_id}"
        cpus_str = f"{max(0.01, self.nano_cpus / 1_000_000_000):g}"
        cmd = ["docker", "run", "-d", "--name", candidate_name, "--cpus", cpus_str]
        if self.mount_host_dir:
            cmd.extend(["-v", f"{self.host_mount_root}:{self.container_work_root}"])
        cmd.extend([self.image_name, "tail", "-f", "/dev/null"])

        rc, _, err = self._runner(cmd, min(60, self.timeout_seconds))
        if rc != 0:
            raise SandboxError(
                f"Failed to start Docker container from '{self.image_name}': {err.strip() or f'rc={rc}'}"
            )
        self.container_name = candidate_name
        self._owns_container = True
        return self.container_name

    def clear_staged_patch(self) -> None:
        """Delete any staged `/tmp/specpatchguard_*.patch` file inside the container."""
        container = self.container_name
        staged = self._staged_patch_path
        if container and staged:
            self._runner(
                ["docker", "exec", container, "rm", "-f", "--", staged],
                min(15, self.timeout_seconds),
            )
        self._staged_patch_path = None
        self._staged_patch_content = None
        self._patch_created_files.clear()

    def destroy(self) -> None:
        """Idempotently clean up staged patch artifacts and remove owned containers."""
        container = self.container_name
        if not container:
            return

        self.clear_staged_patch()

        try:
            if self._owns_container:
                self._runner(["docker", "rm", "-f", container], min(30, self.timeout_seconds))
        finally:
            self.container_name = None
            self.active_work_root = None
            self.base_ref = None
            self.state = "prepatch"
            self._owns_container = False
            self._staged_patch_path = None
            self._staged_patch_content = None
            self._patch_created_files.clear()
            self._injected_files.clear()

    def set_active_workdir(
        self,
        work_root: str,
        *,
        base_ref: str | None = None,
        state: Literal["prepatch", "postpatch"] = "prepatch",
    ) -> None:
        """Set the active repository workdir inside the container and reset per-workdir tracking."""
        if state not in {"prepatch", "postpatch"}:
            raise SandboxError(f"Invalid sandbox state: {state!r}")
        if self.container_name is not None:
            self.clear_staged_patch()
        else:
            self._staged_patch_path = None
            self._staged_patch_content = None
            self._patch_created_files.clear()
        self.active_work_root = _validate_posix_abs("work_root", work_root)
        self.base_ref = base_ref.strip() if isinstance(base_ref, str) and base_ref.strip() else None
        self.state = state
        self._injected_files.clear()

    def exec_command(
        self,
        argv: list[str],
        *,
        workdir: str | None = None,
        timeout: int | None = None,
        use_inner_timeout: bool = False,
    ) -> ExecOutput:
        """Execute an argv vector inside the running container."""
        if not argv:
            raise SandboxError("argv cannot be empty")
        container = self._require_started()
        target_workdir = self._effective_workdir(workdir)
        eff_timeout = self.timeout_seconds if timeout is None else timeout
        if type(eff_timeout) is not int or eff_timeout < 1:
            raise SandboxError("timeout must be an integer >= 1")

        if use_inner_timeout:
            inner = " ".join(shlex.quote(arg) for arg in argv)
            wrapped = _wrap_inner_timeout(inner, eff_timeout)
            docker_cmd = ["docker", "exec", "-w", target_workdir, container, "sh", "-c", wrapped]
            host_timeout = eff_timeout + 15
        else:
            docker_cmd = ["docker", "exec", "-w", target_workdir, container, *argv]
            host_timeout = eff_timeout

        start = time.monotonic()
        rc, out, err = self._runner(docker_cmd, host_timeout)
        elapsed = time.monotonic() - start
        timed_out = (rc == 124) if use_inner_timeout else (rc == 124 and "timed out" in err.lower())
        if use_inner_timeout and rc == 137 and (elapsed >= eff_timeout or (not out and not err)):
            timed_out = True
        return ExecOutput(returncode=rc, stdout=out, stderr=err, timed_out=timed_out)

    def exec_bash(
        self,
        script: str,
        *,
        workdir: str | None = None,
        timeout: int | None = None,
        with_d4j_env: bool = False,
        use_inner_timeout: bool = True,
    ) -> ExecOutput:
        """Execute a bash script inside the container with optional Defects4J JDK 8 environment."""
        if not isinstance(script, str) or not script.strip():
            raise SandboxError("script cannot be empty")
        container = self._require_started()
        target_workdir = self._effective_workdir(workdir)
        eff_timeout = self.timeout_seconds if timeout is None else timeout
        if type(eff_timeout) is not int or eff_timeout < 1:
            raise SandboxError("timeout must be an integer >= 1")

        full_script = script
        if with_d4j_env:
            env_prefix = (
                f"export JAVA_HOME={shlex.quote(self.java8_home)} && "
                f'export PATH="$JAVA_HOME/bin":{shlex.quote(self.d4j_home + "/framework/bin")}:$PATH'
            )
            full_script = f"{env_prefix} && {{\n{script}\n}}"

        if use_inner_timeout:
            wrapped = _wrap_inner_timeout(full_script, eff_timeout)
            docker_cmd = ["docker", "exec", "-w", target_workdir, container, "sh", "-c", wrapped]
            host_timeout = eff_timeout + 15
        else:
            docker_cmd = ["docker", "exec", "-w", target_workdir, container, "bash", "-lc", full_script]
            host_timeout = eff_timeout

        start = time.monotonic()
        rc, out, err = self._runner(docker_cmd, host_timeout)
        elapsed = time.monotonic() - start
        timed_out = (rc == 124) if use_inner_timeout else (rc == 124 and "timed out" in err.lower())
        if use_inner_timeout and rc == 137 and (elapsed >= eff_timeout or (not out and not err)):
            timed_out = True
        return ExecOutput(returncode=rc, stdout=out, stderr=err, timed_out=timed_out)

    def read_file(self, rel_path: str, *, readonly_prepatch: bool = False) -> str:
        """Read a repository-relative file from `active_work_root`."""
        if readonly_prepatch and self.state != "prepatch":
            raise SandboxError(
                f"Patch-Blind violation: readonly_prepatch=True requires state == 'prepatch', got {self.state!r}"
            )
        workdir = self._require_workdir()
        try:
            safe_rel = resolve_repo_rel_path(rel_path, readonly_prepatch=readonly_prepatch)
        except Exception as exc:
            raise SandboxError(str(exc)) from None
        result = self.exec_command(["cat", "--", safe_rel], workdir=workdir)
        if result.timed_out:
            raise SandboxError(f"Timed out reading container file: {safe_rel}")
        if result.returncode != 0:
            raise SandboxError(
                f"Failed to read container file '{safe_rel}': {result.stderr.strip() or f'rc={result.returncode}'}"
            )
        return result.stdout

    def write_file(
        self,
        rel_path: str,
        content: str | bytes,
        *,
        track_injected: bool = False,
    ) -> str:
        """Safely write bytes into a repository-relative path via `docker exec -i` stdin."""
        container = self._require_started()
        workdir = self._require_workdir()
        try:
            safe_rel = resolve_repo_rel_path(rel_path, readonly_prepatch=False)
        except Exception as exc:
            raise SandboxError(str(exc)) from None

        if ".git" in PurePosixPath(safe_rel).parts or safe_rel in _PROTECTED_D4J_META:
            raise SandboxError(f"Writing protected path '{safe_rel}' is forbidden")

        raw_bytes = content.encode("utf-8") if isinstance(content, str) else bytes(content)
        parent = PurePosixPath(safe_rel).parent.as_posix()
        if parent and parent != ".":
            write_cmd = f"mkdir -p -- {shlex.quote(parent)} && cat > {shlex.quote(safe_rel)}"
        else:
            write_cmd = f"cat > {shlex.quote(safe_rel)}"

        rc, _, err = self._stdin_runner(
            ["docker", "exec", "-i", "-w", workdir, container, "sh", "-c", write_cmd],
            raw_bytes,
            min(60, self.timeout_seconds),
        )
        if rc != 0:
            raise SandboxError(f"Failed writing container file '{safe_rel}': {err.strip() or f'rc={rc}'}")
        if track_injected and safe_rel not in self._injected_files:
            self._injected_files.append(safe_rel)
        return safe_rel

    def file_exists(self, rel_path: str) -> bool:
        """Return True if `rel_path` exists as a file or directory inside `active_work_root`."""
        workdir = self._require_workdir()
        try:
            safe_rel = resolve_repo_rel_path(rel_path, readonly_prepatch=False)
        except Exception as exc:
            raise SandboxError(str(exc)) from None
        res = self.exec_command(["test", "-e", safe_rel], workdir=workdir)
        return res.returncode == 0

    def remove_file(self, rel_path: str) -> bool:
        """Remove a single repository-relative file (never `.git` or `.defects4j.config`)."""
        workdir = self._require_workdir()
        try:
            safe_rel = resolve_repo_rel_path(rel_path, readonly_prepatch=False)
        except Exception as exc:
            raise SandboxError(str(exc)) from None
        if ".git" in PurePosixPath(safe_rel).parts or safe_rel in _PROTECTED_D4J_META:
            raise SandboxError(f"Refusing to remove protected file: {safe_rel}")
        res = self.exec_command(["rm", "-f", "--", safe_rel], workdir=workdir)
        if safe_rel in self._injected_files:
            self._injected_files = [item for item in self._injected_files if item != safe_rel]
        return res.returncode == 0

    def stage_patch(self, patch_content: str) -> str:
        """Stage a candidate patch under `/tmp/` inside the container (outside `active_work_root`)."""
        container = self._require_started()
        if not isinstance(patch_content, str) or not patch_content.strip():
            raise PatchApplyError("patch_content must be a non-empty unified diff string")

        normalized = _normalize_patch_text(patch_content)
        patch_path = f"/tmp/specpatchguard_{self._run_id}.patch"
        rc, _, err = self._stdin_runner(
            ["docker", "exec", "-i", container, "sh", "-c", f"cat > {shlex.quote(patch_path)}"],
            normalized.encode("utf-8"),
            min(60, self.timeout_seconds),
        )
        if rc != 0:
            raise PatchApplyError(f"Failed to stage patch at '{patch_path}': {err.strip() or f'rc={rc}'}")

        self._staged_patch_path = patch_path
        self._staged_patch_content = normalized
        self._patch_created_files = extract_patch_added_files(normalized)
        return patch_path

    def _reset_tracked_and_rejects(self, workdir: str) -> ExecOutput:
        """Restore git-tracked files and remove `.orig`/`.rej`/patch-added files without erasing D4J metadata."""
        if self._patch_created_files:
            self.exec_command(["rm", "-f", "--", *self._patch_created_files], workdir=workdir)
        cleanup_cmd = (
            "find . -maxdepth 8 \\( -name '*.orig' -o -name '*.rej' \\) | while IFS= read -r f; do rm -f -- \"$f\"; done; "
            "if [ -n \"$SPG_BASE_REF\" ] && git rev-parse --verify -q \"$SPG_BASE_REF\" >/dev/null 2>&1; then "
            "  if [ \"$(git rev-parse HEAD)\" != \"$(git rev-parse \"$SPG_BASE_REF\")\" ]; then "
            "    git reset --hard \"$SPG_BASE_REF\"; "
            "  fi; "
            "fi; "
            "git checkout -- ."
        )
        base_env = f"SPG_BASE_REF={shlex.quote(self.base_ref or '')}; {cleanup_cmd}"
        return self.exec_bash(base_env, workdir=workdir, with_d4j_env=False, use_inner_timeout=True)

    def apply_patch(self, patch_content: str | None = None) -> tuple[bool, str]:
        """Apply the staged (or supplied) patch in `active_work_root` using ordered fallback commands."""
        workdir = self._require_workdir()
        if self.state != "prepatch":
            raise PatchApplyError(
                f"Cannot apply patch when sandbox state is {self.state!r}; restore prepatch first"
            )

        if patch_content is not None:
            self.stage_patch(patch_content)

        if not self._staged_patch_path or not self._staged_patch_content:
            raise PatchApplyError("No candidate patch staged; pass patch_content or call stage_patch()")

        strip_level = detect_patch_strip_level(self._staged_patch_content)
        errors: list[str] = []

        for template in GIT_APPLY_CMDS:
            cmd_prefix = template.format(strip=strip_level)
            full_cmd = f"{cmd_prefix} {shlex.quote(self._staged_patch_path)}"
            res = self.exec_bash(full_cmd, workdir=workdir, with_d4j_env=False, use_inner_timeout=True)
            if res.returncode == 0 and not res.timed_out:
                self.state = "postpatch"
                return True, cmd_prefix

            detail = (res.stderr or res.stdout or f"rc={res.returncode}").strip()
            errors.append(f"[{cmd_prefix}] {detail}")
            # Roll back any partial hunks / .rej files before trying next fallback command.
            self._reset_tracked_and_rejects(workdir)

        self.state = "prepatch"
        self._patch_created_files.clear()
        return False, "; ".join(errors)

    def cleanup_injected_files(self, *, extra_paths: list[str] | None = None) -> list[str]:
        """Remove tracked injected test/helper files and optional explicit relative paths."""
        workdir = self._require_workdir()
        targets: list[str] = []
        for raw in [*self._injected_files, *(extra_paths or [])]:
            try:
                safe_rel = resolve_repo_rel_path(raw, readonly_prepatch=False)
            except Exception as exc:
                raise SandboxError(str(exc)) from None
            if ".git" in PurePosixPath(safe_rel).parts or safe_rel in _PROTECTED_D4J_META:
                raise SandboxError(f"Refusing to remove protected path during cleanup: {safe_rel}")
            if safe_rel not in targets:
                targets.append(safe_rel)

        if targets:
            res = self.exec_command(["rm", "-f", "--", *targets], workdir=workdir)
            if res.returncode != 0:
                raise SandboxError(
                    f"Failed removing injected files {targets}: {res.stderr.strip() or f'rc={res.returncode}'}"
                )
        self._injected_files.clear()
        return targets

    def restore_prepatch(self, *, remove_injected: bool = True) -> bool:
        """Restore `active_work_root` back to clean pre-patch state without deleting D4J metadata."""
        workdir = self._require_workdir()
        if remove_injected:
            self.cleanup_injected_files()
        res = self._reset_tracked_and_rejects(workdir)
        if res.returncode != 0 or res.timed_out:
            raise SandboxError(
                f"Failed to restore prepatch state in '{workdir}': "
                f"{res.stderr.strip() or res.stdout.strip() or f'rc={res.returncode}'}"
            )
        self._patch_created_files.clear()
        self.state = "prepatch"
        return True

    def switch_state(self, target_state: Literal["prepatch", "postpatch"]) -> bool:
        """Switch repository state between `'prepatch'` and `'postpatch'` cleanly."""
        if target_state not in {"prepatch", "postpatch"}:
            raise SandboxError(f"Invalid target state: {target_state!r}")
        self._require_workdir()
        if self.state == target_state:
            return True
        if target_state == "postpatch":
            applied, _ = self.apply_patch()
            return applied
        # Preserve caller-managed injected test files when toggling back to prepatch during differential runs.
        return self.restore_prepatch(remove_injected=False)

    def create_repo_executor(self, *, readonly_prepatch: bool = True) -> DockerExecExecutor:
        """Expose Step 4 `DockerExecExecutor` bound to the active container and D4J workdir."""
        container = self._require_started()
        workdir = self._require_workdir()
        if readonly_prepatch and self.state != "prepatch":
            raise SandboxError(
                f"Patch-Blind violation: cannot create readonly_prepatch executor in state {self.state!r}"
            )
        return DockerExecExecutor(
            container_name=container,
            work_root=workdir,
            state=self.state,
            readonly_prepatch=readonly_prepatch,
            runner=self._runner,
            command_timeout_sec=self.timeout_seconds,
        )

    def create_jdtls_transport(
        self,
        lsp_cfg: dict[str, Any],
        *,
        workspace_key: str | None = None,
    ) -> DockerJdtlsTransport:
        """Expose Step 4 `DockerJdtlsTransport` targeting the same container and active D4J workdir."""
        container = self._require_started()
        workdir = self._require_workdir()
        effective_cfg = dict(lsp_cfg)
        base_cache = str(effective_cfg.get("jdtls_workspace_cache", "/tmp/jdtls_ws_cache")).rstrip("/") or "/tmp/jdtls_ws_cache"
        raw_key = workspace_key or PurePosixPath(workdir).name or "default"
        safe_key = _WS_KEY_SANITIZE_RE.sub("_", raw_key).strip("._") or "default"
        effective_cfg["jdtls_workspace_cache"] = f"{base_cache}/{safe_key}"
        return DockerJdtlsTransport(
            container_name=container,
            lsp_cfg=effective_cfg,
            work_root=workdir,
        )
