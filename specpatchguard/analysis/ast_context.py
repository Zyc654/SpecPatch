"""Java AST/structural context tools aligned with simplified `benchmark/rule.md` (10 等价 + 6 过拟合 + 3 边界):
- `get_enclosing_branches` (control flow, preconditions/guards, early-return preview, class & method visibility)
- `get_class_members` (fields including array initializers/multi-declarators, constructors, method signatures, inheritance)
- `get_method_context` (multiline method signature + same-name overloads + annotations + Javadoc + configurable context)
- `trace_symbol_usage` (non-local/field/config/return read/write/call footprint with receiver qualifier tracking)
"""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any

from .protocols import (
    RepoExecutor,
    ToolExecutionError,
    assert_patch_blind_state,
    extract_numbered_snippet,
    extract_numbered_snippet_from_lines,
    resolve_context_window,
    resolve_repo_rel_path,
)

_VISIBILITIES = ("public", "protected", "private")
_TYPE_HEADER_RE = re.compile(
    r"\b(?P<kind>class|interface|enum|record)\s+(?P<name>[A-Za-z_$][A-Za-z0-9_$]*)"
    r"(?:\s*<[^>{}]*>)?(?P<rest>[^{]*)$",
    re.DOTALL,
)
_EXTENDS_RE = re.compile(r"\bextends\s+([A-Za-z0-9_$.<>,\s]+?)(?=\bimplements\b|$)")
_IMPLEMENTS_RE = re.compile(r"\bimplements\s+([A-Za-z0-9_$.<>,\s]+)$")
_CONTROL_BRANCH_RE = re.compile(
    r"^(?:(?P<else_if>else\s+if)|(?P<kw>if|else|switch|for|while|do|try|catch|finally))\b(?P<rest>.*)$"
)
_GUARD_RE = re.compile(
    r"\b(?:Preconditions\.check\w+|Objects\.requireNonNull|assert\b|throw\s+new\b|return\b)"
)
_FIELD_MODIFIERS = {
    "public",
    "protected",
    "private",
    "static",
    "final",
    "transient",
    "volatile",
}
_METHOD_MODIFIERS = {
    "public",
    "protected",
    "private",
    "static",
    "final",
    "abstract",
    "synchronized",
    "native",
    "strictfp",
    "default",
}


@dataclass
class _BlockSpan:
    kind: str
    header: str
    start_line: int       # Declaration/header first line (includes multiline signature & annotations)
    body_start_line: int  # Line containing opening '{'
    end_line: int
    depth: int


def _strip_comments_and_strings(source: str) -> tuple[str, dict[int, str]]:
    """Replace Java comments and string/char literal bodies with spaces while preserving line numbers."""
    out: list[str] = []
    javadocs: dict[int, str] = {}
    i = 0
    n = len(source)
    line = 1
    while i < n:
        ch = source[i]
        nxt = source[i + 1] if i + 1 < n else ""
        if ch == "\n":
            out.append("\n")
            line += 1
            i += 1
            continue
        if ch == "/" and nxt == "/":
            while i < n and source[i] != "\n":
                out.append(" ")
                i += 1
            continue
        if ch == "/" and nxt == "*":
            is_doc = i + 2 < n and source[i + 2] == "*"
            buf = ["/*", " "]
            i += 2
            out.extend([" ", " "])
            while i < n:
                c = source[i]
                if c == "\n":
                    out.append("\n")
                    buf.append("\n")
                    line += 1
                    i += 1
                    continue
                if c == "*" and i + 1 < n and source[i + 1] == "/":
                    out.extend([" ", " "])
                    buf.append("*/")
                    i += 2
                    break
                out.append(" ")
                buf.append(c)
                i += 1
            if is_doc:
                javadocs[line] = "".join(buf).strip()
            continue
        if ch in {'"', "'"}:
            quote = ch
            out.append(quote)
            i += 1
            while i < n:
                c = source[i]
                if c == "\n":
                    out.append("\n")
                    line += 1
                    i += 1
                    break
                if c == "\\":
                    out.append(" ")
                    i += 1
                    if i < n and source[i] != "\n":
                        out.append(" ")
                        i += 1
                    continue
                if c == quote:
                    out.append(quote)
                    i += 1
                    break
                out.append(" ")
                i += 1
            continue
        out.append(ch)
        i += 1
    return "".join(out), javadocs


def _attached_javadoc(javadocs: dict[int, str], decl_start_line: int, masked_lines: list[str]) -> str:
    """Find Javadoc ending immediately above `decl_start_line` (allowing annotations/blank lines)."""
    cursor = decl_start_line - 1
    while cursor >= 1:
        if cursor in javadocs:
            return javadocs[cursor]
        stripped = masked_lines[cursor - 1].strip()
        if stripped == "" or stripped.startswith("@"):
            cursor -= 1
            continue
        break
    return ""


def _visibility_from_modifiers(tokens: list[str]) -> str:
    for vis in _VISIBILITIES:
        if vis in tokens:
            return vis
    return "package-private"


def _split_top_level_header(header: str) -> str:
    collapsed = " ".join(header.strip().split())
    while collapsed.startswith("@"):
        idx = 1
        while idx < len(collapsed) and (collapsed[idx].isalnum() or collapsed[idx] in {"_", "$", "."}):
            idx += 1
        if idx < len(collapsed) and collapsed[idx] == "(":
            depth = 1
            idx += 1
            while idx < len(collapsed) and depth > 0:
                if collapsed[idx] == "(":
                    depth += 1
                elif collapsed[idx] == ")":
                    depth -= 1
                idx += 1
        collapsed = collapsed[idx:].strip()
    return collapsed


def _extract_annotations(raw_lines: list[str], decl_start_line: int, body_start_line: int) -> list[str]:
    annotations: list[str] = []
    # Collect annotations inside [decl_start_line, body_start_line] (when stmt_buf captured @Override etc.)
    for idx in range(max(0, decl_start_line - 1), min(len(raw_lines), body_start_line)):
        line = raw_lines[idx].strip()
        if line.startswith("@"):
            annotations.append(line)
    # Also scan immediately above decl_start_line if any annotations preceded it
    idx = decl_start_line - 2
    above: list[str] = []
    while idx >= 0:
        line = raw_lines[idx].strip()
        if not line:
            idx -= 1
            continue
        if line.startswith("@"):
            above.append(line)
            idx -= 1
            continue
        break
    above.reverse()
    combined = above + annotations
    # Deduplicate preserving order
    seen: set[str] = set()
    unique: list[str] = []
    for item in combined:
        if item not in seen:
            seen.add(item)
            unique.append(item)
    return unique


def _parse_method_header(header: str) -> tuple[str, str, str, str] | None:
    """Parse `(prefix, name, params, throws)` supporting nested parens inside param annotations."""
    stripped = header.strip()
    # Optional trailing `throws ...`
    throws_match = re.search(r"\)\s*(throws\s+[^{;]+)$", stripped)
    if throws_match:
        throws_str = throws_match.group(1).replace("throws", "", 1).strip()
        close_idx = throws_match.start()
    else:
        if not stripped.endswith(")"):
            return None
        throws_str = ""
        close_idx = len(stripped) - 1

    depth = 0
    open_idx = -1
    for pos in range(close_idx, -1, -1):
        ch = stripped[pos]
        if ch == ")":
            depth += 1
        elif ch == "(":
            depth -= 1
            if depth == 0:
                open_idx = pos
                break
    if open_idx <= 0:
        return None

    before_paren = stripped[:open_idx].rstrip()
    name_match = re.search(r"([A-Za-z_$][A-Za-z0-9_$]*)$", before_paren)
    if not name_match:
        return None
    name = name_match.group(1)
    prefix = before_paren[: name_match.start()].strip()
    params = " ".join(stripped[open_idx + 1 : close_idx].split())
    return prefix, name, params, throws_str


def _scan_java_blocks(masked: str) -> tuple[list[_BlockSpan], dict[int, int]]:
    """Scan `{ ... }` spans tracking full multiline declaration start line and brace depth."""
    spans: list[_BlockSpan] = []
    line_start_depth: dict[int, int] = {}
    stack: list[tuple[int, int, int, str]] = []  # (decl_start_line, open_line, depth_before_open, header)

    line = 1
    line_start_depth[1] = 0
    stmt_buf: list[str] = []
    stmt_start_line: int | None = None
    paren_depth = 0

    for ch in masked:
        if ch == "\n":
            line += 1
            line_start_depth[line] = len(stack)
            if stmt_buf:
                stmt_buf.append(" ")
            continue
        if ch == "(":
            if stmt_start_line is None:
                stmt_start_line = line
            paren_depth += 1
            stmt_buf.append(ch)
            continue
        if ch == ")":
            paren_depth = max(0, paren_depth - 1)
            stmt_buf.append(ch)
            continue
        if ch == ";" and paren_depth == 0:
            stmt_buf.clear()
            stmt_start_line = None
            continue
        if ch == "{":
            header = " ".join("".join(stmt_buf).split())
            decl_line = stmt_start_line if stmt_start_line is not None else line
            stack.append((decl_line, line, len(stack), header))
            stmt_buf.clear()
            stmt_start_line = None
            paren_depth = 0
            continue
        if ch == "}":
            if stack:
                decl_line, open_line, open_depth, header = stack.pop()
                spans.append(
                    _BlockSpan(
                        kind="block",
                        header=header,
                        start_line=decl_line,
                        body_start_line=open_line,
                        end_line=line,
                        depth=open_depth,
                    )
                )
            stmt_buf.clear()
            stmt_start_line = None
            paren_depth = 0
            continue
        if ch.isspace():
            if stmt_buf:
                stmt_buf.append(ch)
            continue
        if stmt_start_line is None:
            stmt_start_line = line
        stmt_buf.append(ch)

    spans.sort(key=lambda s: (s.start_line, -s.end_line))
    return spans, line_start_depth


def _classify_control_kind(header: str) -> tuple[str, str] | None:
    cleaned = header.strip()
    if cleaned.startswith("}"):
        cleaned = cleaned[1:].strip()
    match = _CONTROL_BRANCH_RE.match(cleaned)
    if not match:
        return None
    kind = "else_if" if match.group("else_if") else match.group("kw")
    rest = match.group("rest").strip()
    cond = ""
    if rest.startswith("(") and ")" in rest:
        depth = 0
        for pos, ch in enumerate(rest):
            if ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
                if depth == 0:
                    cond = rest[1:pos].strip()
                    break
    return kind, cond


def _split_top_level_commas(text: str) -> list[str]:
    parts: list[str] = []
    cur: list[str] = []
    p_depth = 0
    b_depth = 0
    c_depth = 0
    a_depth = 0
    for ch in text:
        if ch == "(":
            p_depth += 1
        elif ch == ")":
            p_depth = max(0, p_depth - 1)
        elif ch == "[":
            b_depth += 1
        elif ch == "]":
            b_depth = max(0, b_depth - 1)
        elif ch == "{":
            c_depth += 1
        elif ch == "}":
            c_depth = max(0, c_depth - 1)
        elif ch == "<":
            a_depth += 1
        elif ch == ">":
            a_depth = max(0, a_depth - 1)
        elif ch == "," and p_depth == 0 and b_depth == 0 and c_depth == 0 and a_depth == 0:
            parts.append("".join(cur).strip())
            cur.clear()
            continue
        cur.append(ch)
    if cur:
        parts.append("".join(cur).strip())
    return [p for p in parts if p]


def _extract_fields_from_statement(
    stmt_clean: str,
    raw_stmt: str,
    stmt_start_line: int,
    javadocs: dict[int, str],
    masked_lines: list[str],
) -> list[dict[str, Any]]:
    head_no_ann = _split_top_level_header(stmt_clean)
    raw_no_ann = _split_top_level_header(raw_stmt)
    if not head_no_ann or head_no_ann.startswith(("package ", "import ")):
        return []
    before_eq = head_no_ann.split("=", 1)[0]
    if "(" in before_eq:
        return []

    decls = _split_top_level_commas(raw_no_ann)
    if not decls:
        return []

    first_lhs, _, _ = decls[0].partition("=")
    first_tokens = first_lhs.strip().split()
    if len(first_tokens) < 2:
        return []

    mods = [t for t in first_tokens[:-1] if t in _FIELD_MODIFIERS]
    type_tokens = [t for t in first_tokens[:-1] if t not in _FIELD_MODIFIERS]
    field_type = " ".join(type_tokens)
    visibility = _visibility_from_modifiers(mods)
    jdoc = _attached_javadoc(javadocs, stmt_start_line, masked_lines)

    fields: list[dict[str, Any]] = []
    for idx, decl in enumerate(decls):
        lhs, has_eq, rhs = decl.partition("=")
        lhs_tokens = lhs.strip().split()
        if not lhs_tokens:
            continue
        field_name = lhs_tokens[-1].rstrip("[]")
        if idx == 0 and len(lhs_tokens) < 2:
            continue
        fields.append(
            {
                "name": field_name,
                "type": field_type,
                "visibility": visibility,
                "modifiers": mods,
                "initializer": rhs.strip() if has_eq else "",
                "line": stmt_start_line,
                "javadoc": jdoc,
            }
        )
    return fields


def _parse_classes_and_members(source: str) -> dict[str, Any]:
    masked, javadocs = _strip_comments_and_strings(source)
    raw_lines = source.splitlines()
    masked_lines = masked.splitlines()
    spans, line_start_depth = _scan_java_blocks(masked)

    package_name = ""
    imports: list[str] = []
    for raw in raw_lines:
        stripped = raw.strip()
        if stripped.startswith("package ") and stripped.endswith(";"):
            package_name = stripped[8:-1].strip()
        elif stripped.startswith("import ") and stripped.endswith(";"):
            imports.append(stripped)

    classes: list[dict[str, Any]] = []
    for span in spans:
        header = _split_top_level_header(span.header)
        m = _TYPE_HEADER_RE.search(header)
        if not m:
            continue
        prefix_tokens = header[: m.start()].split()
        rest = m.group("rest").strip()
        extends_match = _EXTENDS_RE.search(rest)
        implements_match = _IMPLEMENTS_RE.search(rest)
        classes.append(
            {
                "name": m.group("name"),
                "kind": m.group("kind"),
                "visibility": _visibility_from_modifiers(prefix_tokens),
                "modifiers": prefix_tokens,
                "extends": extends_match.group(1).strip() if extends_match else "",
                "implements": [
                    item.strip()
                    for item in (implements_match.group(1).split(",") if implements_match else [])
                    if item.strip()
                ],
                "start_line": span.start_line,
                "body_start_line": span.body_start_line,
                "end_line": span.end_line,
                "depth": span.depth,
                "javadoc": _attached_javadoc(javadocs, span.start_line, masked_lines),
                "fields": [],
                "constructors": [],
                "methods": [],
            }
        )

    def _owning_class(line_no: int, member_depth: int) -> dict[str, Any] | None:
        best: dict[str, Any] | None = None
        for cls in classes:
            if cls["body_start_line"] <= line_no <= cls["end_line"] and cls["depth"] + 1 == member_depth:
                if best is None or cls["body_start_line"] > best["body_start_line"]:
                    best = cls
        return best

    # Extract methods & constructors (blocks whose depth == owning_class.depth + 1)
    for span in spans:
        cls = _owning_class(span.body_start_line, span.depth)
        if cls is None:
            continue
        header = _split_top_level_header(span.header)
        if not header or header == "static":
            continue
        if _CONTROL_BRANCH_RE.match(header):
            continue
        parsed_sig = _parse_method_header(header)
        if parsed_sig is None:
            continue
        prefix_raw, name, params, throws = parsed_sig
        prefix_tokens = [tok for tok in prefix_raw.split() if tok]
        jdoc = _attached_javadoc(javadocs, span.start_line, masked_lines)
        annotations = _extract_annotations(raw_lines, span.start_line, span.body_start_line)

        if name == cls["name"] and not any(
            t not in {"public", "protected", "private", "final", "synchronized"} for t in prefix_tokens
        ):
            cls["constructors"].append(
                {
                    "name": name,
                    "signature": f"{name}({params})",
                    "visibility": _visibility_from_modifiers(prefix_tokens),
                    "modifiers": prefix_tokens,
                    "parameters": params,
                    "throws": throws,
                    "annotations": annotations,
                    "start_line": span.start_line,
                    "body_start_line": span.body_start_line,
                    "end_line": span.end_line,
                    "javadoc": jdoc,
                }
            )
        else:
            mods = [t for t in prefix_tokens if t in _METHOD_MODIFIERS]
            ret_tokens = [t for t in prefix_tokens if t not in _METHOD_MODIFIERS]
            return_type = " ".join(ret_tokens)
            cls["methods"].append(
                {
                    "name": name,
                    "signature": f"{name}({params})",
                    "visibility": _visibility_from_modifiers(mods),
                    "modifiers": mods,
                    "return_type": return_type,
                    "parameters": params,
                    "throws": throws,
                    "annotations": annotations,
                    "start_line": span.start_line,
                    "body_start_line": span.body_start_line,
                    "end_line": span.end_line,
                    "javadoc": jdoc,
                }
            )

    # Extract class-level fields including array initializers `= {1, 2, 3};` and multi-declarators
    depth = 0
    stmt_chars: list[str] = []
    raw_stmt_chars: list[str] = []
    stmt_start_line = 1
    current_line = 1
    in_field_init = False
    init_brace_depth = 0

    for idx, ch in enumerate(masked):
        raw_ch = source[idx]
        if ch == "\n":
            current_line += 1
            stmt_chars.append(" ")
            raw_stmt_chars.append(" ")
            continue
        if ch == "{":
            if in_field_init:
                init_brace_depth += 1
                stmt_chars.append(ch)
                raw_stmt_chars.append(raw_ch)
            else:
                depth += 1
                stmt_chars.clear()
                raw_stmt_chars.clear()
                stmt_start_line = current_line
            continue
        if ch == "}":
            if in_field_init and init_brace_depth > 0:
                init_brace_depth -= 1
                stmt_chars.append(ch)
                raw_stmt_chars.append(raw_ch)
            else:
                depth = max(0, depth - 1)
                stmt_chars.clear()
                raw_stmt_chars.clear()
                stmt_start_line = current_line
                in_field_init = False
                init_brace_depth = 0
            continue
        if not stmt_chars and ch.isspace():
            stmt_start_line = current_line
            continue
        if ch == "=" and not in_field_init:
            owner = _owning_class(stmt_start_line, depth)
            if owner is not None:
                in_field_init = True
        if ch == ";" and init_brace_depth == 0:
            stmt_clean = " ".join("".join(stmt_chars).split())
            raw_stmt = " ".join("".join(raw_stmt_chars).split())
            owner = _owning_class(stmt_start_line, depth)
            if owner is not None and stmt_clean:
                owner["fields"].extend(
                    _extract_fields_from_statement(
                        stmt_clean, raw_stmt, stmt_start_line, javadocs, masked_lines
                    )
                )
            stmt_chars.clear()
            raw_stmt_chars.clear()
            stmt_start_line = current_line
            in_field_init = False
            init_brace_depth = 0
            continue
        stmt_chars.append(ch)
        raw_stmt_chars.append(raw_ch)

    return {
        "package": package_name,
        "imports": imports,
        "classes": classes,
        "spans": spans,
        "line_start_depth": line_start_depth,
        "masked_lines": masked_lines,
        "raw_lines": raw_lines,
    }


def _find_braceless_enclosing_branches(
    masked_lines: list[str],
    line_start_depth: dict[int, int],
    target_line: int,
    min_line: int,
) -> list[dict[str, Any]]:
    """Detect immediately governing braceless single-statement `if/else/for/while` headers above `target_line`."""
    target_depth = line_start_depth.get(target_line, 0)
    found: list[dict[str, Any]] = []
    cursor = target_line - 1
    while cursor >= min_line:
        text = masked_lines[cursor - 1].strip()
        if not text:
            cursor -= 1
            continue
        if line_start_depth.get(cursor, -1) != target_depth:
            break
        if text.endswith(("{", "}", ";")):
            break
        classified = _classify_control_kind(text)
        if classified is None:
            break
        kind, cond = classified
        found.append(
            {
                "kind": kind,
                "condition": cond,
                "header": text,
                "start_line": cursor,
                "end_line": target_line,
                "braceless": True,
            }
        )
        cursor -= 1
    found.reverse()
    return found


class JavaAstContextTools:
    """Four discrete Java structural tools aligned with simplified `benchmark/rule.md` rules."""

    def __init__(
        self,
        executor: RepoExecutor,
        tools_cfg: dict[str, Any],
        readonly_prepatch: bool = True,
    ) -> None:
        assert_patch_blind_state(executor, readonly_prepatch)
        self.executor = executor
        self.tools_cfg = tools_cfg
        self.readonly_prepatch = readonly_prepatch

    def get_enclosing_branches(
        self,
        file_path: str,
        line: int,
        context_before: int | None = None,
        context_after: int | None = None,
        context_lines: int | None = None,
    ) -> dict[str, Any]:
        """Extract enclosing control-flow branch chain (braced and braceless), guards, and post-line preview."""
        if type(line) is not int or line < 1:
            raise ToolExecutionError("line must be an integer >= 1")
        safe_rel = resolve_repo_rel_path(file_path, readonly_prepatch=self.readonly_prepatch)
        source = self.executor.read_file(safe_rel)
        parsed = _parse_classes_and_members(source)
        raw_lines = parsed["raw_lines"]
        if line > len(raw_lines):
            raise ToolExecutionError(f"line ({line}) exceeds file line count ({len(raw_lines)})")

        default_ctx = self.tools_cfg["branch_context_lines"]
        eff_before, eff_after, ctx_clamped = resolve_context_window(
            context_before=context_before,
            context_after=context_after,
            context_lines=context_lines,
            default_before=default_ctx,
            default_after=default_ctx,
            max_context=self.tools_cfg["read_max_lines"],
        )

        enclosing_method: dict[str, Any] | None = None
        enclosing_class_name = ""
        enclosing_class_visibility = ""
        for cls in parsed["classes"]:
            for m in cls["methods"] + cls["constructors"]:
                if m["start_line"] <= line <= m["end_line"]:
                    if enclosing_method is None or m["start_line"] > enclosing_method["start_line"]:
                        enclosing_method = m
                        enclosing_class_name = cls["name"]
                        enclosing_class_visibility = cls["visibility"]
        if enclosing_method is None:
            best_cls_start = -1
            for cls in parsed["classes"]:
                if cls["start_line"] <= line <= cls["end_line"] and cls["start_line"] > best_cls_start:
                    best_cls_start = cls["start_line"]
                    enclosing_class_name = cls["name"]
                    enclosing_class_visibility = cls["visibility"]

        branches: list[dict[str, Any]] = []
        for span in parsed["spans"]:
            if not (span.start_line <= line <= span.end_line):
                continue
            classified = _classify_control_kind(span.header)
            if classified is None:
                continue
            kind, cond = classified
            branches.append(
                {
                    "kind": kind,
                    "condition": cond,
                    "header": span.header,
                    "start_line": span.start_line,
                    "end_line": span.end_line,
                    "braceless": False,
                }
            )
        branches.sort(key=lambda b: (b["start_line"], -b["end_line"]))

        min_guard_line = (
            enclosing_method["body_start_line"] + 1 if enclosing_method is not None else 1
        )
        branches.extend(
            _find_braceless_enclosing_branches(
                parsed["masked_lines"], parsed["line_start_depth"], line, min_guard_line
            )
        )

        guards: list[dict[str, Any]] = []
        if enclosing_method is not None:
            masked_lines = parsed["masked_lines"]
            for ln in range(enclosing_method["body_start_line"] + 1, line):
                txt = masked_lines[ln - 1].strip()
                if txt and _GUARD_RE.search(txt):
                    guards.append({"line": ln, "text": raw_lines[ln - 1].strip()})

        following: list[dict[str, Any]] = []
        upper = enclosing_method["end_line"] if enclosing_method is not None else len(raw_lines)
        for ln in range(line + 1, min(upper + 1, line + 12)):
            text = raw_lines[ln - 1].strip()
            if text and text != "}":
                following.append({"line": ln, "text": text})
                if len(following) >= 5:
                    break

        snippet = extract_numbered_snippet_from_lines(
            raw_lines, line, line, eff_before, eff_after, self.tools_cfg["read_max_lines"]
        )
        return {
            "ok": True,
            "file_path": safe_rel,
            "line": line,
            "enclosing_class": enclosing_class_name,
            "enclosing_class_visibility": enclosing_class_visibility,
            "enclosing_method": (
                {
                    "name": enclosing_method["name"],
                    "signature": enclosing_method["signature"],
                    "visibility": enclosing_method["visibility"],
                    "start_line": enclosing_method["start_line"],
                    "end_line": enclosing_method["end_line"],
                }
                if enclosing_method
                else None
            ),
            "enclosing_branches": branches,
            "preceding_guards": guards,
            "following_statements_preview": following,
            "effective_context_before": eff_before,
            "effective_context_after": eff_after,
            "context_clamped": ctx_clamped,
            "snippet": snippet["content"],
        }

    def get_class_members(
        self,
        file_path: str,
        class_name: str = "",
        include_fields: bool = True,
        include_constructors: bool = True,
        include_methods: bool = True,
        include_imports: bool = True,
    ) -> dict[str, Any]:
        """Extract class fields, visibility, modifiers, initializers, constructors, and method signatures."""
        for flag_name, flag in (
            ("include_fields", include_fields),
            ("include_constructors", include_constructors),
            ("include_methods", include_methods),
            ("include_imports", include_imports),
        ):
            if type(flag) is not bool:
                raise ToolExecutionError(f"{flag_name} must be a boolean")
        if not isinstance(class_name, str):
            raise ToolExecutionError("class_name must be a string")

        safe_rel = resolve_repo_rel_path(file_path, readonly_prepatch=self.readonly_prepatch)
        source = self.executor.read_file(safe_rel)
        parsed = _parse_classes_and_members(source)

        selected = parsed["classes"]
        if class_name.strip():
            target = class_name.strip()
            selected = [c for c in selected if c["name"] == target]
            if not selected:
                raise ToolExecutionError(f"Class '{target}' not found in {safe_rel}")

        classes_out: list[dict[str, Any]] = []
        for cls in selected:
            entry: dict[str, Any] = {
                "name": cls["name"],
                "kind": cls["kind"],
                "visibility": cls["visibility"],
                "modifiers": cls["modifiers"],
                "extends": cls["extends"],
                "implements": cls["implements"],
                "start_line": cls["start_line"],
                "end_line": cls["end_line"],
                "javadoc": cls["javadoc"],
            }
            if include_fields:
                entry["fields"] = cls["fields"]
            if include_constructors:
                entry["constructors"] = cls["constructors"]
            if include_methods:
                entry["methods"] = cls["methods"]
            classes_out.append(entry)

        result: dict[str, Any] = {
            "ok": True,
            "file_path": safe_rel,
            "package": parsed["package"],
            "classes": classes_out,
        }
        if include_imports:
            result["imports"] = parsed["imports"]
        return result

    def get_method_context(
        self,
        file_path: str,
        line: int | None = None,
        method_name: str = "",
        context_before: int | None = None,
        context_after: int | None = None,
        context_lines: int | None = None,
    ) -> dict[str, Any]:
        """Locate a method by line or name and return signature metadata + configurable surrounding context."""
        if line is None and not (isinstance(method_name, str) and method_name.strip()):
            raise ToolExecutionError("Provide at least one of 'line' or 'method_name'")
        if line is not None and (type(line) is not int or line < 1):
            raise ToolExecutionError("line must be an integer >= 1")
        if not isinstance(method_name, str):
            raise ToolExecutionError("method_name must be a string")

        safe_rel = resolve_repo_rel_path(file_path, readonly_prepatch=self.readonly_prepatch)
        source = self.executor.read_file(safe_rel)
        parsed = _parse_classes_and_members(source)

        matches: list[tuple[dict[str, Any], dict[str, Any]]] = []
        for cls in parsed["classes"]:
            for m in cls["methods"] + cls["constructors"]:
                if line is not None and m["start_line"] <= line <= m["end_line"]:
                    matches.append((cls, m))
                elif line is None and m["name"] == method_name.strip():
                    matches.append((cls, m))

        if not matches:
            raise ToolExecutionError(
                f"No matching method found in {safe_rel} (line={line}, method_name={method_name!r})"
            )

        owner_cls, method_meta = (
            max(matches, key=lambda pair: pair[1]["start_line"]) if line is not None else matches[0]
        )
        owner_class = owner_cls["name"]
        overloads: list[dict[str, Any]] = []
        for cls in parsed["classes"]:
            if line is not None and cls is not owner_cls:
                continue
            for m in cls["methods"] + cls["constructors"]:
                if m["name"] == method_meta["name"]:
                    overloads.append(
                        {
                            "class_name": cls["name"],
                            "name": m["name"],
                            "signature": m["signature"],
                            "visibility": m["visibility"],
                            "return_type": m.get("return_type", ""),
                            "start_line": m["start_line"],
                            "end_line": m["end_line"],
                        }
                    )

        eff_before, eff_after, ctx_clamped = resolve_context_window(
            context_before=context_before,
            context_after=context_after,
            context_lines=context_lines,
            default_before=self.tools_cfg["read_default_context_before"],
            default_after=self.tools_cfg["read_default_context_after"],
            max_context=self.tools_cfg["read_max_lines"],
        )
        snippet = extract_numbered_snippet(
            source,
            method_meta["start_line"],
            method_meta["end_line"],
            eff_before,
            eff_after,
            self.tools_cfg["read_max_lines"],
        )
        return {
            "ok": True,
            "file_path": safe_rel,
            "class_name": owner_class,
            "method": method_meta,
            "overload_count": len(overloads),
            "overloads": overloads,
            "effective_context_before": eff_before,
            "effective_context_after": eff_after,
            "context_clamped": ctx_clamped,
            **snippet,
        }

    def trace_symbol_usage(
        self,
        file_path: str,
        symbol_name: str,
        start_line: int | None = None,
        end_line: int | None = None,
        context_before: int | None = None,
        context_after: int | None = None,
        context_lines: int | None = None,
        max_matches: int | None = None,
    ) -> dict[str, Any]:
        """Trace read/write/call occurrences of a symbol outside comments and string literals."""
        if not isinstance(symbol_name, str) or not re.fullmatch(r"[A-Za-z_$][A-Za-z0-9_$]*", symbol_name.strip()):
            raise ToolExecutionError("symbol_name must be a valid Java identifier")
        sym = symbol_name.strip()
        if start_line is not None and (type(start_line) is not int or start_line < 1):
            raise ToolExecutionError("start_line must be an integer >= 1")
        if end_line is not None and (type(end_line) is not int or end_line < 1):
            raise ToolExecutionError("end_line must be an integer >= 1")
        if start_line is not None and end_line is not None and end_line < start_line:
            raise ToolExecutionError("end_line must be >= start_line")

        cfg_max_matches = self.tools_cfg["grep_max_matches"]
        if max_matches is not None:
            if type(max_matches) is not int or max_matches < 1:
                raise ToolExecutionError("max_matches must be an integer >= 1")
            eff_max_matches = min(max_matches, cfg_max_matches)
        else:
            eff_max_matches = cfg_max_matches

        eff_before, eff_after, ctx_clamped = resolve_context_window(
            context_before=context_before,
            context_after=context_after,
            context_lines=context_lines,
            default_before=self.tools_cfg["branch_context_lines"],
            default_after=self.tools_cfg["branch_context_lines"],
            max_context=self.tools_cfg["grep_max_context_lines"],
        )

        safe_rel = resolve_repo_rel_path(file_path, readonly_prepatch=self.readonly_prepatch)
        source = self.executor.read_file(safe_rel)
        masked, _ = _strip_comments_and_strings(source)
        raw_lines = source.splitlines()
        masked_lines = masked.splitlines()

        lo = start_line or 1
        hi = min(len(raw_lines), end_line) if end_line is not None else len(raw_lines)
        sym_re = re.compile(rf"(?<![A-Za-z0-9_$])\b{re.escape(sym)}\b")
        qualifier_re = re.compile(r"([A-Za-z_$][A-Za-z0-9_$]*)\s*\.\s*$")
        write_after_re = re.compile(r"^\s*(?:\+\+|--|(?:<<|>>>|>>|[+\-*/%&|^])?=(?!=))")
        write_before_re = re.compile(r"(?:\+\+|--)\s*$")
        call_after_re = re.compile(r"^\s*(?:\(|(?:\.[A-Za-z_$][A-Za-z0-9_$]*\s*\())")

        occurrences: list[dict[str, Any]] = []
        truncated = False
        read_max = self.tools_cfg["read_max_lines"]

        for ln in range(lo, hi + 1):
            mline = masked_lines[ln - 1]
            for m in sym_re.finditer(mline):
                before_slice = mline[: m.start()]
                after_slice = mline[m.end() :]
                qual_match = qualifier_re.search(before_slice)
                qualifier = qual_match.group(1) if qual_match else ""
                if write_after_re.match(after_slice) or write_before_re.search(before_slice):
                    usage_kind = "write"
                elif call_after_re.match(after_slice):
                    usage_kind = "call"
                else:
                    usage_kind = "read"

                if len(occurrences) >= eff_max_matches:
                    truncated = True
                    return {
                        "ok": True,
                        "file_path": safe_rel,
                        "symbol_name": sym,
                        "effective_context_before": eff_before,
                        "effective_context_after": eff_after,
                        "context_clamped": ctx_clamped,
                        "total_occurrences": len(occurrences),
                        "truncated": truncated,
                        "occurrences": occurrences,
                    }

                snippet = extract_numbered_snippet_from_lines(
                    raw_lines, ln, ln, eff_before, eff_after, read_max
                )
                occurrences.append(
                    {
                        "line": ln,
                        "column": m.start() + 1,
                        "qualifier": qualifier,
                        "usage_kind": usage_kind,
                        "line_text": raw_lines[ln - 1],
                        "snippet": snippet["content"],
                    }
                )

        return {
            "ok": True,
            "file_path": safe_rel,
            "symbol_name": sym,
            "effective_context_before": eff_before,
            "effective_context_after": eff_after,
            "context_clamped": ctx_clamped,
            "total_occurrences": len(occurrences),
            "truncated": truncated,
            "occurrences": occurrences,
        }
