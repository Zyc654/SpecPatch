"""JSON-RPC 2.0 Eclipse `jdtls` client and 4 discrete LSP navigation tools."""

from __future__ import annotations

from collections import deque
import hashlib
import json
from pathlib import PurePosixPath
import queue
import re
import shlex
import subprocess
import threading
import time
from typing import Any, BinaryIO, Literal, Protocol, runtime_checkable
from urllib.parse import unquote, urlparse

from ..core.cache_utils import sanitize_cache_segment
from .protocols import (
    DEFAULT_COMMAND_TIMEOUT_SEC,
    RepoExecutor,
    ToolExecutionError,
    assert_patch_blind_state,
    extract_numbered_snippet,
    resolve_context_window,
    resolve_repo_rel_path,
)


class LspProtocolError(ToolExecutionError):
    """Raised on malformed LSP framing, unexpected EOF, timeouts, or JSON-RPC errors."""


def encode_lsp_message(payload: dict[str, Any]) -> bytes:
    """Encode a JSON-RPC payload using LSP `Content-Length` framing."""
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    header = f"Content-Length: {len(body)}\r\n\r\n".encode("ascii")
    return header + body


def read_lsp_message(stream: BinaryIO) -> dict[str, Any]:
    """Read one JSON-RPC message framed with `Content-Length` from a binary stream."""
    headers: dict[str, str] = {}
    while True:
        raw_line = stream.readline()
        if not raw_line:
            raise LspProtocolError("Unexpected EOF while reading LSP headers")
        if raw_line in {b"\r\n", b"\n"}:
            break
        decoded = raw_line.decode("ascii", errors="strict").strip()
        if ":" not in decoded:
            raise LspProtocolError(f"Malformed LSP header line: {decoded!r}")
        key, val = decoded.split(":", 1)
        headers[key.strip().lower()] = val.strip()

    if "content-length" not in headers:
        raise LspProtocolError("Missing Content-Length header in LSP message")
    try:
        content_length = int(headers["content-length"])
    except ValueError:
        raise LspProtocolError(f"Invalid Content-Length: {headers['content-length']!r}") from None
    if content_length < 0:
        raise LspProtocolError(f"Negative Content-Length: {content_length}")

    remaining = content_length
    chunks: list[bytes] = []
    while remaining > 0:
        chunk = stream.read(remaining)
        if not chunk:
            raise LspProtocolError("Unexpected EOF while reading LSP message body")
        chunks.append(chunk)
        remaining -= len(chunk)

    body_bytes = b"".join(chunks)
    try:
        parsed = json.loads(body_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise LspProtocolError(f"Invalid JSON body in LSP message: {exc}") from None
    if not isinstance(parsed, dict):
        raise LspProtocolError("LSP JSON-RPC message must be a JSON object")
    return parsed


@runtime_checkable
class LspTransport(Protocol):
    """Transport abstraction for sending/receiving JSON-RPC payloads to `jdtls`."""

    def send(self, payload: dict[str, Any]) -> None: ...

    def receive(self, timeout: float = float(DEFAULT_COMMAND_TIMEOUT_SEC)) -> dict[str, Any]: ...

    def close(self) -> None: ...


class DockerJdtlsTransport:
    """Launch headless Eclipse `jdtls` inside a running Docker container over stdio with non-blocking timeouts."""

    def __init__(
        self,
        container_name: str,
        lsp_cfg: dict[str, Any],
        work_root: str,
    ) -> None:
        # [SERVER_CONFIG_REQUIRED]: 确认 Docker 容器内 jdtls_java_home / jdtls_home / jdtls_config_dir / jdtls_workspace_cache 路径有效
        self.container_name = container_name
        self.lsp_cfg = lsp_cfg
        self.work_root = work_root
        jdtls_java = f"{lsp_cfg['jdtls_java_home'].rstrip('/')}/bin/java"
        jdtls_home = lsp_cfg["jdtls_home"].rstrip("/")
        jdtls_cfg_dir = lsp_cfg["jdtls_config_dir"].rstrip("/")
        jdtls_ws_base = lsp_cfg["jdtls_workspace_cache"].rstrip("/") or "/tmp/jdtls_ws_cache"
        safe_key = sanitize_cache_segment(PurePosixPath(work_root).name or "default")
        work_hash = hashlib.sha256(work_root.encode("utf-8")).hexdigest()[:8]
        unique_key = f"{safe_key}_{work_hash}"
        jdtls_ws = (
            jdtls_ws_base
            if PurePosixPath(jdtls_ws_base).name == unique_key
            else f"{jdtls_ws_base}/{unique_key}"
        )
        self.workspace_data_dir = jdtls_ws
        pid_file = f"{jdtls_ws}/.jdtls.pid"

        launcher_glob = shlex.quote(f"{jdtls_home}/plugins/org.eclipse.equinox.launcher_") + "*.jar"
        launcher_cmd = (
            f"LAUNCHER=$(ls {launcher_glob} 2>/dev/null | head -n 1); "
            '[ -n "$LAUNCHER" ] || { echo "jdtls launcher jar not found" >&2; exit 127; }; '
            f"mkdir -p {shlex.quote(jdtls_ws)} && "
            f"echo $$ > {shlex.quote(pid_file)} && "
            f"exec {shlex.quote(jdtls_java)} "
            "-Declipse.application=org.eclipse.jdt.ls.core.id1 "
            "-Dosgi.bundles.defaultStartLevel=4 "
            "-Declipse.product=org.eclipse.jdt.ls.core.product "
            "-Dlog.level=ERROR "
            '-jar "$LAUNCHER" '
            f"-configuration {shlex.quote(jdtls_cfg_dir)} "
            f"-data {shlex.quote(jdtls_ws)}"
        )
        self._proc = subprocess.Popen(
            ["docker", "exec", "-i", "-w", work_root, container_name, "sh", "-c", launcher_cmd],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        self._msg_queue: queue.Queue[tuple[str, Any]] = queue.Queue()
        self._stderr_tail: deque[str] = deque(maxlen=200)
        self._closed = False
        self._stdout_thread = threading.Thread(target=self._stdout_loop, daemon=True)
        self._stderr_thread = threading.Thread(target=self._stderr_loop, daemon=True)
        self._stdout_thread.start()
        self._stderr_thread.start()

    def _stdout_loop(self) -> None:
        assert self._proc.stdout is not None
        while not self._closed:
            try:
                msg = read_lsp_message(self._proc.stdout)
                self._msg_queue.put(("msg", msg))
            except Exception as exc:
                self._msg_queue.put(("err", exc))
                break

    def _stderr_loop(self) -> None:
        assert self._proc.stderr is not None
        try:
            for raw_line in iter(self._proc.stderr.readline, b""):
                self._stderr_tail.append(raw_line.decode("utf-8", errors="replace").rstrip("\r\n"))
        except OSError:
            pass

    def stderr_tail(self, limit: int = 10) -> str:
        tail = list(self._stderr_tail)[-limit:]
        return " | ".join(tail)

    def send(self, payload: dict[str, Any]) -> None:
        if self._closed or self._proc.stdin is None or self._proc.poll() is not None:
            raise LspProtocolError(f"jdtls container process is not running ({self.stderr_tail()})")
        try:
            self._proc.stdin.write(encode_lsp_message(payload))
            self._proc.stdin.flush()
        except OSError as exc:
            raise LspProtocolError(f"Failed writing to jdtls stdin: {exc}") from None

    def receive(self, timeout: float = float(DEFAULT_COMMAND_TIMEOUT_SEC)) -> dict[str, Any]:
        try:
            kind, value = self._msg_queue.get(timeout=max(0.01, float(timeout)))
        except queue.Empty:
            raise LspProtocolError(
                f"LSP receive timed out after {timeout}s; stderr tail: {self.stderr_tail()}"
            ) from None
        if kind == "err":
            if isinstance(value, LspProtocolError):
                raise value
            raise LspProtocolError(f"LSP reader failed: {value}")
        return value

    def close(self) -> None:
        self._closed = True
        for stream in (self._proc.stdin, self._proc.stdout, self._proc.stderr):
            if stream is not None:
                try:
                    stream.close()
                except OSError:
                    pass
        still_alive = self._proc.poll() is None
        if still_alive:
            self._proc.terminate()
            try:
                self._proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._proc.kill()
        if self.container_name and getattr(self, "workspace_data_dir", ""):
            ws_dir = self.workspace_data_dir
            pid_file = f"{ws_dir}/.jdtls.pid"
            data_pattern = re.escape(ws_dir)
            cleanup_script = (
                f"if [ -f {shlex.quote(pid_file)} ]; then "
                f"PID=$(cat {shlex.quote(pid_file)} 2>/dev/null || true); "
                f'[ -n "$PID" ] && kill "$PID" 2>/dev/null || true; '
                f"rm -f -- {shlex.quote(pid_file)}; "
                "fi; "
                f'pkill -f -- "[-][d]ata {data_pattern}( |$)" 2>/dev/null || true'
            )
            try:
                subprocess.run(
                    [
                        "docker",
                        "exec",
                        self.container_name,
                        "sh",
                        "-c",
                        cleanup_script,
                    ],
                    timeout=5,
                    capture_output=True,
                    check=False,
                )
            except Exception:
                pass


def _path_to_uri(work_root: str, rel_path: str) -> str:
    root_posix = work_root.rstrip("/")
    if not root_posix.startswith("/"):
        root_posix = "/" + root_posix
    full = PurePosixPath(root_posix) / rel_path
    return f"file://{full.as_posix()}"


def _uri_to_rel_path(uri: str, work_root: str) -> tuple[str | None, str]:
    parsed = urlparse(uri)
    if parsed.scheme != "file":
        return None, uri
    raw_path = unquote(parsed.path)
    if len(parsed.netloc) == 2 and parsed.netloc[1] == ":":
        raw_path = parsed.netloc + raw_path
    if len(raw_path) >= 3 and raw_path[0] == "/" and raw_path[2] == ":":
        raw_path = raw_path[1:]
    norm_root = work_root.rstrip("/")
    if norm_root:
        if raw_path.startswith(norm_root + "/"):
            return raw_path[len(norm_root) + 1 :], raw_path
        if len(norm_root) >= 2 and norm_root[1] == ":" and raw_path.lower().startswith(norm_root.lower() + "/"):
            return raw_path[len(norm_root) + 1 :], raw_path
    return None, raw_path


_TEST_DIR_NAMES = frozenset({"test", "tests", "test-src", "src-test", "junit"})
_TEST_PREFIX_RE = re.compile(r"^Test(?:$|[A-Z0-9_])")


def _is_test_source_path(rel_path: str) -> bool:
    """Return True if a repository-relative path points to test code rather than production code."""
    posix = PurePosixPath(rel_path.replace("\\", "/"))
    parts_lower = [part.lower() for part in posix.parts]
    dirs_lower = parts_lower[:-1]
    if dirs_lower and dirs_lower[0] in _TEST_DIR_NAMES:
        return True
    for idx in range(len(dirs_lower) - 1):
        if dirs_lower[idx] == "src" and dirs_lower[idx + 1] in {"test", "tests"}:
            return True
    for idx in range(len(dirs_lower) - 1):
        if dirs_lower[idx] == "src" and dirs_lower[idx + 1] == "main":
            return False
    if any(part in _TEST_DIR_NAMES for part in dirs_lower):
        return True
    stem = posix.stem
    return stem.endswith(("Test", "Tests", "TestCase")) or (_TEST_PREFIX_RE.match(stem) is not None)


class LspNavigationTools:
    """Four discrete LSP navigation tools aligned with `benchmark/rule.md` checks."""

    def __init__(
        self,
        transport: LspTransport,
        executor: RepoExecutor,
        lsp_cfg: dict[str, Any],
        tools_cfg: dict[str, Any],
        work_root: str | None = None,
        readonly_prepatch: bool = True,
    ) -> None:
        assert_patch_blind_state(executor, readonly_prepatch)
        self.transport = transport
        self.executor = executor
        self.lsp_cfg = lsp_cfg
        self.tools_cfg = tools_cfg
        self.work_root = (work_root or executor.work_root).rstrip("/")
        self.readonly_prepatch = readonly_prepatch
        self._next_id = 1
        self._initialized = False
        self._opened_uris: set[str] = set()

    def _handle_server_request(self, msg: dict[str, Any]) -> None:
        method = msg.get("method")
        if method == "workspace/configuration":
            items = (msg.get("params") or {}).get("items") or []
            result: Any = [{} for _ in items] if isinstance(items, list) else []
        else:
            result = None
        self.transport.send({"jsonrpc": "2.0", "id": msg["id"], "result": result})

    def _request(self, method: str, params: dict[str, Any] | None, timeout: float) -> Any:
        req_id = self._next_id
        self._next_id += 1
        self.transport.send({"jsonrpc": "2.0", "id": req_id, "method": method, "params": params})
        deadline = time.monotonic() + float(timeout)
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise LspProtocolError(f"LSP request '{method}' timed out after {timeout}s")
            msg = self.transport.receive(timeout=remaining)
            # Answer server-to-client requests (e.g., workspace/configuration, client/registerCapability)
            if "method" in msg and "id" in msg:
                self._handle_server_request(msg)
                continue
            if msg.get("id") != req_id:
                continue
            if "error" in msg and msg["error"] is not None:
                err = msg["error"]
                raise LspProtocolError(f"LSP error on {method}: {err.get('message', err)}")
            return msg.get("result")

    def _notify(self, method: str, params: dict[str, Any] | None) -> None:
        self.transport.send({"jsonrpc": "2.0", "method": method, "params": params})

    def ensure_initialized(self) -> None:
        if self._initialized:
            return
        root_uri = _path_to_uri(self.work_root, ".").removesuffix("/.")
        params = {
            "processId": None,
            "rootUri": root_uri,
            "capabilities": {
                "textDocument": {
                    "definition": {"linkSupport": True},
                    "hover": {"contentFormat": ["markdown", "plaintext"]},
                    "typeHierarchy": {"dynamicRegistration": False},
                }
            },
        }
        self._request("initialize", params, timeout=float(self.lsp_cfg["init_timeout_sec"]))
        self._notify("initialized", {})
        self._initialized = True
        self._wait_for_service_ready()

    def _wait_for_service_ready(self) -> None:
        """Wait briefly for jdtls `language/status` `ServiceReady` without failing if timeout elapses."""
        default_wait = 15.0 if isinstance(self.transport, DockerJdtlsTransport) else 0.0
        ready_wait_sec = float(self.lsp_cfg.get("ready_wait_sec", default_wait))
        if ready_wait_sec <= 0.0:
            return
        deadline = time.monotonic() + ready_wait_sec
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            try:
                msg = self.transport.receive(timeout=min(remaining, 1.0))
            except LspProtocolError as exc:
                if "timed out" not in str(exc).lower() or time.monotonic() >= deadline:
                    return
                continue
            if "method" in msg and "id" in msg:
                self._handle_server_request(msg)
                continue
            if msg.get("method") == "language/status":
                status_type = str((msg.get("params") or {}).get("type", ""))
                if status_type in {"ServiceReady", "Started"}:
                    return

    def ensure_opened(self, file_path: str) -> tuple[str, str]:
        self.ensure_initialized()
        safe_rel = resolve_repo_rel_path(file_path, readonly_prepatch=self.readonly_prepatch)
        uri = _path_to_uri(self.work_root, safe_rel)
        if uri not in self._opened_uris:
            text = self.executor.read_file(safe_rel)
            self._notify(
                "textDocument/didOpen",
                {
                    "textDocument": {
                        "uri": uri,
                        "languageId": "java",
                        "version": 1,
                        "text": text,
                    }
                },
            )
            self._opened_uris.add(uri)
        return safe_rel, uri

    def close(self) -> None:
        """Gracefully send LSP `shutdown` + `exit` and close the underlying transport."""
        try:
            if self._initialized:
                try:
                    self._request("shutdown", None, timeout=5.0)
                except LspProtocolError:
                    pass
                try:
                    self._notify("exit", None)
                except LspProtocolError:
                    pass
        finally:
            self._initialized = False
            self.transport.close()

    def _validate_position(self, line: int, character: int) -> dict[str, int]:
        if type(line) is not int or line < 1:
            raise ToolExecutionError("line must be an integer >= 1 (1-based)")
        if type(character) is not int or character < 1:
            raise ToolExecutionError("character must be an integer >= 1 (1-based)")
        return {"line": line - 1, "character": character - 1}

    def _enrich_locations(
        self,
        raw_result: Any,
        eff_before: int,
        eff_after: int,
        max_results: int | None = None,
        file_filter: str | None = None,
        exclude_tests: bool = False,
    ) -> tuple[list[dict[str, Any]], bool, int]:
        if raw_result is None:
            return [], False, 0
        items = raw_result if isinstance(raw_result, list) else [raw_result]
        enriched: list[dict[str, Any]] = []
        truncated = False
        excluded_test_count = 0

        for item in items:
            if not isinstance(item, dict):
                continue
            if "targetUri" in item:
                uri = item["targetUri"]
                rng = item.get("targetSelectionRange") or item.get("targetRange") or {}
            else:
                uri = item.get("uri", "")
                rng = item.get("range", {})
            if not isinstance(uri, str) or not uri:
                continue

            start = rng.get("start", {})
            end = rng.get("end", {})
            s_line = int(start.get("line", 0)) + 1
            s_char = int(start.get("character", 0)) + 1
            e_line = max(s_line, int(end.get("line", s_line - 1)) + 1)
            e_char = int(end.get("character", 0)) + 1

            rel_path, raw_path = _uri_to_rel_path(uri, self.work_root)
            external = rel_path is None
            if file_filter is not None and rel_path != file_filter:
                continue

            snippet_text = ""
            is_test = False
            if rel_path is not None:
                try:
                    safe_rel = resolve_repo_rel_path(rel_path, readonly_prepatch=self.readonly_prepatch)
                except ToolExecutionError:
                    continue
                is_test = _is_test_source_path(safe_rel)
                if exclude_tests and is_test:
                    excluded_test_count += 1
                    continue
                if max_results is not None and len(enriched) >= max_results:
                    truncated = True
                    break
                try:
                    content = self.executor.read_file(safe_rel)
                    snippet_info = extract_numbered_snippet(
                        content, s_line, e_line, eff_before, eff_after, self.tools_cfg["read_max_lines"]
                    )
                    snippet_text = snippet_info["content"]
                except ToolExecutionError:
                    snippet_text = ""
                display_path = safe_rel
            else:
                if max_results is not None and len(enriched) >= max_results:
                    truncated = True
                    break
                display_path = raw_path

            enriched.append(
                {
                    "file_path": display_path,
                    "external": external,
                    "is_test_file": is_test,
                    "start_line": s_line,
                    "start_character": s_char,
                    "end_line": e_line,
                    "end_character": e_char,
                    "snippet": snippet_text,
                }
            )
        return enriched, truncated, excluded_test_count

    def lsp_goto_definition(
        self,
        file_path: str,
        line: int,
        character: int,
        context_before: int | None = None,
        context_after: int | None = None,
        context_lines: int | None = None,
    ) -> dict[str, Any]:
        """Jump to a helper/field/type definition (`rule.md` 等价 3 & 7) with configurable context."""
        pos = self._validate_position(line, character)
        safe_rel, uri = self.ensure_opened(file_path)
        eff_before, eff_after, ctx_clamped = resolve_context_window(
            context_before=context_before,
            context_after=context_after,
            context_lines=context_lines,
            default_before=self.tools_cfg["read_default_context_before"],
            default_after=self.tools_cfg["read_default_context_after"],
            max_context=self.tools_cfg["read_max_lines"],
        )
        raw = self._request(
            "textDocument/definition",
            {"textDocument": {"uri": uri}, "position": pos},
            timeout=float(self.lsp_cfg["request_timeout_sec"]),
        )
        definitions, _, _ = self._enrich_locations(raw, eff_before, eff_after)
        return {
            "ok": True,
            "file_path": safe_rel,
            "line": line,
            "character": character,
            "effective_context_before": eff_before,
            "effective_context_after": eff_after,
            "context_clamped": ctx_clamped,
            "definitions": definitions,
        }

    def lsp_find_references(
        self,
        file_path: str,
        line: int,
        character: int,
        include_declaration: bool = False,
        scope: Literal["project", "file"] = "project",
        exclude_tests: bool = False,
        context_before: int | None = None,
        context_after: int | None = None,
        context_lines: int | None = None,
        max_results: int | None = None,
    ) -> dict[str, Any]:
        """Find project/file callers and state readers (`rule.md` 等价 8, 边界 1/2/3), with optional `exclude_tests`."""
        if type(include_declaration) is not bool:
            raise ToolExecutionError("include_declaration must be a boolean")
        if type(exclude_tests) is not bool:
            raise ToolExecutionError("exclude_tests must be a boolean")
        if scope not in {"project", "file"}:
            raise ToolExecutionError("scope must be 'project' or 'file'")
        cfg_max = self.tools_cfg["grep_max_matches"]
        if max_results is not None:
            if type(max_results) is not int or max_results < 1:
                raise ToolExecutionError("max_results must be an integer >= 1")
            eff_max = min(max_results, cfg_max)
        else:
            eff_max = cfg_max

        pos = self._validate_position(line, character)
        safe_rel, uri = self.ensure_opened(file_path)
        eff_before, eff_after, ctx_clamped = resolve_context_window(
            context_before=context_before,
            context_after=context_after,
            context_lines=context_lines,
            default_before=self.tools_cfg["grep_default_context_before"],
            default_after=self.tools_cfg["grep_default_context_after"],
            max_context=self.tools_cfg["grep_max_context_lines"],
        )
        raw = self._request(
            "textDocument/references",
            {
                "textDocument": {"uri": uri},
                "position": pos,
                "context": {"includeDeclaration": include_declaration},
            },
            timeout=float(self.lsp_cfg["request_timeout_sec"]),
        )
        refs, truncated, excluded_test_count = self._enrich_locations(
            raw,
            eff_before,
            eff_after,
            max_results=eff_max,
            file_filter=safe_rel if scope == "file" else None,
            exclude_tests=exclude_tests,
        )
        return {
            "ok": True,
            "file_path": safe_rel,
            "line": line,
            "character": character,
            "scope": scope,
            "exclude_tests": exclude_tests,
            "excluded_test_count": excluded_test_count,
            "effective_context_before": eff_before,
            "effective_context_after": eff_after,
            "context_clamped": ctx_clamped,
            "total_references": len(refs),
            "truncated": truncated,
            "references": refs,
        }

    def lsp_hover(self, file_path: str, line: int, character: int) -> dict[str, Any]:
        """Retrieve resolved runtime type signature and Javadoc contracts (`rule.md` 等价 7 & 边界 1)."""
        pos = self._validate_position(line, character)
        safe_rel, uri = self.ensure_opened(file_path)
        raw = self._request(
            "textDocument/hover",
            {"textDocument": {"uri": uri}, "position": pos},
            timeout=float(self.lsp_cfg["request_timeout_sec"]),
        )
        hover_text = _normalize_hover_contents(raw.get("contents") if isinstance(raw, dict) else None)
        return {
            "ok": True,
            "file_path": safe_rel,
            "line": line,
            "character": character,
            "hover_text": hover_text,
        }

    def lsp_type_hierarchy(
        self,
        file_path: str,
        line: int,
        character: int,
        direction: Literal["both", "supertypes", "subtypes"] = "both",
        context_before: int | None = None,
        context_after: int | None = None,
        context_lines: int | None = None,
    ) -> dict[str, Any]:
        """Inspect supertypes and subclass overrides (`rule.md` 过拟合 6 & 边界 1)."""
        if direction not in {"both", "supertypes", "subtypes"}:
            raise ToolExecutionError("direction must be 'both', 'supertypes', or 'subtypes'")
        pos = self._validate_position(line, character)
        safe_rel, uri = self.ensure_opened(file_path)
        eff_before, eff_after, ctx_clamped = resolve_context_window(
            context_before=context_before,
            context_after=context_after,
            context_lines=context_lines,
            default_before=self.tools_cfg["branch_context_lines"],
            default_after=self.tools_cfg["branch_context_lines"],
            max_context=self.tools_cfg["read_max_lines"],
        )
        timeout = float(self.lsp_cfg["request_timeout_sec"])
        prepared = self._request(
            "textDocument/prepareTypeHierarchy",
            {"textDocument": {"uri": uri}, "position": pos},
            timeout=timeout,
        )
        if not prepared or not isinstance(prepared, list):
            return {
                "ok": True,
                "file_path": safe_rel,
                "line": line,
                "character": character,
                "direction": direction,
                "supertypes": [],
                "subtypes": [],
            }

        item = prepared[0]
        supertypes_out: list[dict[str, Any]] = []
        subtypes_out: list[dict[str, Any]] = []

        if direction in {"both", "supertypes"}:
            raw_sup = self._request("typeHierarchy/supertypes", {"item": item}, timeout=timeout)
            supertypes_out = self._format_hierarchy_items(raw_sup, eff_before, eff_after)
        if direction in {"both", "subtypes"}:
            raw_sub = self._request("typeHierarchy/subtypes", {"item": item}, timeout=timeout)
            subtypes_out = self._format_hierarchy_items(raw_sub, eff_before, eff_after)

        return {
            "ok": True,
            "file_path": safe_rel,
            "line": line,
            "character": character,
            "direction": direction,
            "target_name": item.get("name", ""),
            "effective_context_before": eff_before,
            "effective_context_after": eff_after,
            "context_clamped": ctx_clamped,
            "supertypes": supertypes_out,
            "subtypes": subtypes_out,
        }

    def _format_hierarchy_items(
        self,
        raw_items: Any,
        eff_before: int,
        eff_after: int,
    ) -> list[dict[str, Any]]:
        if not isinstance(raw_items, list):
            return []
        formatted: list[dict[str, Any]] = []
        for entry in raw_items:
            if not isinstance(entry, dict):
                continue
            locs, _, _ = self._enrich_locations(
                [{"uri": entry.get("uri", ""), "range": entry.get("selectionRange") or entry.get("range", {})}],
                eff_before,
                eff_after,
            )
            loc = locs[0] if locs else {}
            formatted.append(
                {
                    "name": entry.get("name", ""),
                    "detail": entry.get("detail", ""),
                    **loc,
                }
            )
        return formatted


def _normalize_hover_contents(contents: Any) -> str:
    if contents is None:
        return ""
    if isinstance(contents, str):
        return contents
    if isinstance(contents, dict):
        value = contents.get("value", "")
        return value if isinstance(value, str) else str(value)
    if isinstance(contents, list):
        parts = [_normalize_hover_contents(item) for item in contents]
        return "\n\n".join(p for p in parts if p)
    return str(contents)
