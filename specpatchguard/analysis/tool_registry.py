"""OpenAI function-calling tool schemas and safe budget/truncation dispatcher for 10 discrete tools."""

from __future__ import annotations

import json
from typing import Any, Callable

from .ast_context import JavaAstContextTools
from .lsp_client import LspNavigationTools, LspProtocolError
from .protocols import ToolExecutionError
from .repo_tools import RepoSearchTools


def _schema(
    name: str,
    description: str,
    properties: dict[str, Any],
    required: list[str],
) -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {
                "type": "object",
                "properties": properties,
                "required": required,
            },
        },
    }


_CONTEXT_PROPS: dict[str, Any] = {
    "context_before": {
        "type": "integer",
        "description": "Lines of context to include before each match/target (optional; defaults to config).",
    },
    "context_after": {
        "type": "integer",
        "description": "Lines of context to include after each match/target (optional; defaults to config).",
    },
    "context_lines": {
        "type": "integer",
        "description": "Symmetric shorthand setting both before and after context lines when omitted.",
    },
}

REPO_TOOL_SCHEMAS: list[dict[str, Any]] = [
    _schema(
        name="grep_code",
        description="Search the Defects4J repository inside Docker with configurable before/after context lines.",
        properties={
            "pattern": {"type": "string", "description": "Regex or literal search pattern."},
            "path_glob": {"type": "string", "description": "Optional glob filter such as '*.java'."},
            **_CONTEXT_PROPS,
            "max_matches": {"type": "integer", "description": "Maximum number of matches to return."},
            "case_sensitive": {"type": "boolean", "description": "Whether matching is case-sensitive."},
            "fixed_strings": {"type": "boolean", "description": "Treat pattern as a literal string instead of regex."},
        },
        required=["pattern"],
    ),
    _schema(
        name="read_file_lines",
        description="Read a 1-based line interval from a repository file with configurable before/after context.",
        properties={
            "file_path": {"type": "string", "description": "Repository-relative file path."},
            "start_line": {"type": "integer", "description": "1-based start line number."},
            "end_line": {"type": "integer", "description": "1-based end line number (defaults to start_line)."},
            **_CONTEXT_PROPS,
        },
        required=["file_path", "start_line"],
    ),
    _schema(
        name="get_enclosing_branches",
        description="Extract enclosing if/else/switch/loop/try-catch conditions, preceding guards, post-line statements, and class/method visibility around a line.",
        properties={
            "file_path": {"type": "string", "description": "Repository-relative Java file path."},
            "line": {"type": "integer", "description": "1-based anchor line number."},
            **_CONTEXT_PROPS,
        },
        required=["file_path", "line"],
    ),
    _schema(
        name="get_class_members",
        description="Extract class visibility, inheritance, fields, modifiers, initializers, constructors, and method signatures.",
        properties={
            "file_path": {"type": "string", "description": "Repository-relative Java file path."},
            "class_name": {"type": "string", "description": "Optional target class name inside the file."},
            "include_fields": {"type": "boolean", "description": "Include class fields and initializers."},
            "include_constructors": {"type": "boolean", "description": "Include constructors."},
            "include_methods": {"type": "boolean", "description": "Include method signatures."},
            "include_imports": {"type": "boolean", "description": "Include import statements."},
        },
        required=["file_path"],
    ),
    _schema(
        name="get_method_context",
        description="Locate a Java method by line or name and return signature metadata, same-name overloads, and body with configurable context lines.",
        properties={
            "file_path": {"type": "string", "description": "Repository-relative Java file path."},
            "line": {"type": "integer", "description": "1-based line inside the target method."},
            "method_name": {"type": "string", "description": "Target method name when line is omitted."},
            **_CONTEXT_PROPS,
        },
        required=["file_path"],
    ),
    _schema(
        name="trace_symbol_usage",
        description="Trace read/write/call sites (with receiver qualifiers) of a field, global config, parameter, or return symbol in a Java file with configurable context.",
        properties={
            "file_path": {"type": "string", "description": "Repository-relative Java file path."},
            "symbol_name": {"type": "string", "description": "Java identifier to trace."},
            "start_line": {"type": "integer", "description": "Optional 1-based start line filter."},
            "end_line": {"type": "integer", "description": "Optional 1-based end line filter."},
            **_CONTEXT_PROPS,
            "max_matches": {"type": "integer", "description": "Maximum number of occurrences to return."},
        },
        required=["file_path", "symbol_name"],
    ),
]

LSP_TOOL_SCHEMAS: list[dict[str, Any]] = [
    _schema(
        name="lsp_goto_definition",
        description="Jump via Eclipse jdtls inside Docker to the definition of a helper method, field, or type at (file_path, line, character) for equivalence and return-contract checks.",
        properties={
            "file_path": {"type": "string", "description": "Repository-relative Java file path."},
            "line": {"type": "integer", "description": "1-based line number."},
            "character": {"type": "integer", "description": "1-based column number."},
            **_CONTEXT_PROPS,
        },
        required=["file_path", "line", "character"],
    ),
    _schema(
        name="lsp_find_references",
        description="Find project-wide or file-local callers and state readers via jdtls, with optional exclude_tests filtering to isolate reachable production callers.",
        properties={
            "file_path": {"type": "string", "description": "Repository-relative Java file path."},
            "line": {"type": "integer", "description": "1-based line number."},
            "character": {"type": "integer", "description": "1-based column number."},
            "include_declaration": {"type": "boolean", "description": "Whether to include the declaration site."},
            "scope": {"type": "string", "enum": ["project", "file"], "description": "Restrict to 'file' or search whole 'project'."},
            "exclude_tests": {
                "type": "boolean",
                "description": "Exclude test files (e.g. src/test/*, *Test.java) to inspect reachable production callers only.",
            },
            **_CONTEXT_PROPS,
            "max_results": {"type": "integer", "description": "Maximum number of references to return."},
        },
        required=["file_path", "line", "character"],
    ),
    _schema(
        name="lsp_hover",
        description="Get resolved runtime type signature and Javadoc return/nullability contract documentation for a symbol via jdtls.",
        properties={
            "file_path": {"type": "string", "description": "Repository-relative Java file path."},
            "line": {"type": "integer", "description": "1-based line number."},
            "character": {"type": "integer", "description": "1-based column number."},
        },
        required=["file_path", "line", "character"],
    ),
    _schema(
        name="lsp_type_hierarchy",
        description="Inspect supertypes and subclass overrides of a class or overridden method via jdtls.",
        properties={
            "file_path": {"type": "string", "description": "Repository-relative Java file path."},
            "line": {"type": "integer", "description": "1-based line number."},
            "character": {"type": "integer", "description": "1-based column number."},
            "direction": {"type": "string", "enum": ["both", "supertypes", "subtypes"], "description": "Hierarchy traversal direction."},
            **_CONTEXT_PROPS,
        },
        required=["file_path", "line", "character"],
    ),
]

ALL_TOOL_SCHEMAS: list[dict[str, Any]] = REPO_TOOL_SCHEMAS + LSP_TOOL_SCHEMAS
_SCHEMA_BY_NAME: dict[str, dict[str, Any]] = {
    item["function"]["name"]: item for item in ALL_TOOL_SCHEMAS
}


def _validate_tool_args(schema: dict[str, Any], args: dict[str, Any]) -> dict[str, Any]:
    params = schema["function"]["parameters"]
    props = params["properties"]
    required = set(params["required"])

    unknown = sorted(set(args) - set(props))
    if unknown:
        raise ToolExecutionError(f"Unknown parameter(s): {', '.join(unknown)}")
    missing = [req for req in params["required"] if req not in args or args[req] is None]
    if missing:
        raise ToolExecutionError(f"Missing required parameter(s): {', '.join(missing)}")

    cleaned: dict[str, Any] = {}
    for key, value in args.items():
        if value is None and key not in required:
            # Allow LLMs to pass explicit JSON null for optional parameters; omit before dispatch.
            continue
        expected_type = props[key].get("type")
        if expected_type == "integer":
            if type(value) is not int:
                raise ToolExecutionError(f"Parameter '{key}' must be an integer")
        elif expected_type == "boolean":
            if type(value) is not bool:
                raise ToolExecutionError(f"Parameter '{key}' must be a boolean")
        elif expected_type == "string":
            if not isinstance(value, str):
                raise ToolExecutionError(f"Parameter '{key}' must be a string")
            if "enum" in props[key] and value not in props[key]["enum"]:
                raise ToolExecutionError(
                    f"Parameter '{key}' must be one of {props[key]['enum']}"
                )
        cleaned[key] = value
    return cleaned


def _shrink_text_field(trimmed: dict[str, Any], field_name: str, max_chars: int) -> dict[str, Any]:
    raw_text = trimmed[field_name]
    suffix = "\n...[TRUNCATED]"
    lo = 0
    hi = len(raw_text)
    best = ""
    while lo <= hi:
        mid = (lo + hi) // 2
        candidate = dict(trimmed)
        candidate[field_name] = raw_text[:mid] + suffix
        if len(json.dumps(candidate, ensure_ascii=False)) <= max_chars:
            best = candidate[field_name]
            lo = mid + 1
        else:
            hi = mid - 1
    trimmed[field_name] = best if best else suffix[: max(0, max_chars - 64)]
    if len(json.dumps(trimmed, ensure_ascii=False)) <= max_chars:
        return trimmed
    return {
        "ok": True,
        "truncated": True,
        "summary": json.dumps(trimmed, ensure_ascii=False)[: max(0, max_chars - 64)] + "...[TRUNCATED]",
    }


def _truncate_payload(payload: dict[str, Any], max_chars: int) -> dict[str, Any]:
    encoded = json.dumps(payload, ensure_ascii=False)
    if len(encoded) <= max_chars:
        return payload

    trimmed = dict(payload)
    trimmed["truncated"] = True

    list_count_fields = {
        "matches": "total_matches",
        "references": "total_references",
        "occurrences": "total_occurrences",
        "overloads": "overload_count",
    }
    for list_key in (
        "matches",
        "references",
        "occurrences",
        "overloads",
        "definitions",
        "supertypes",
        "subtypes",
        "classes",
    ):
        if isinstance(trimmed.get(list_key), list) and trimmed[list_key]:
            items = list(trimmed[list_key])
            original_count = len(items)
            while items and len(json.dumps(trimmed, ensure_ascii=False)) > max_chars:
                items.pop()
                trimmed[list_key] = items
                trimmed["returned_count"] = len(items)
                trimmed["total_before_truncation"] = original_count
                if list_key in list_count_fields:
                    trimmed[list_count_fields[list_key]] = len(items)
            if len(json.dumps(trimmed, ensure_ascii=False)) <= max_chars:
                return trimmed

    if isinstance(trimmed.get("content"), str):
        return _shrink_text_field(trimmed, "content", max_chars)

    if isinstance(trimmed.get("snippet"), str):
        return _shrink_text_field(trimmed, "snippet", max_chars)

    return {
        "ok": True,
        "truncated": True,
        "summary": encoded[: max(0, max_chars - 64)] + "...[TRUNCATED]",
    }


class ToolDispatcher:
    """Validate, budget-guard, execute, and size-cap calls to the 10 discrete analysis tools."""

    def __init__(
        self,
        repo_tools: RepoSearchTools,
        ast_tools: JavaAstContextTools,
        tools_cfg: dict[str, Any],
        lsp_tools: LspNavigationTools | None = None,
    ) -> None:
        self.tools_cfg = tools_cfg
        self.max_calls = tools_cfg["max_tool_calls_per_agent"]
        self.max_output_chars = tools_cfg["max_tool_output_chars"]
        self.call_count = 0
        self.call_log: list[dict[str, Any]] = []

        self._handlers: dict[str, Callable[..., dict[str, Any]]] = {
            "grep_code": repo_tools.grep_code,
            "read_file_lines": repo_tools.read_file_lines,
            "get_enclosing_branches": ast_tools.get_enclosing_branches,
            "get_class_members": ast_tools.get_class_members,
            "get_method_context": ast_tools.get_method_context,
            "trace_symbol_usage": ast_tools.trace_symbol_usage,
        }
        if lsp_tools is not None:
            self._handlers.update(
                {
                    "lsp_goto_definition": lsp_tools.lsp_goto_definition,
                    "lsp_find_references": lsp_tools.lsp_find_references,
                    "lsp_hover": lsp_tools.lsp_hover,
                    "lsp_type_hierarchy": lsp_tools.lsp_type_hierarchy,
                }
            )

    def get_schemas(self, include_lsp: bool = True) -> list[dict[str, Any]]:
        if include_lsp and "lsp_goto_definition" in self._handlers:
            return list(ALL_TOOL_SCHEMAS)
        return list(REPO_TOOL_SCHEMAS)

    def dispatch(self, name: str, arguments: dict[str, Any] | str) -> dict[str, Any]:
        if name not in self._handlers:
            return {"ok": False, "tool": name, "error": f"Unknown or disabled tool: {name}"}

        if self.call_count >= self.max_calls:
            return {
                "ok": False,
                "tool": name,
                "error": f"Tool call budget exhausted ({self.call_count}/{self.max_calls})",
            }

        if isinstance(arguments, str):
            try:
                parsed_args = json.loads(arguments) if arguments.strip() else {}
            except json.JSONDecodeError as exc:
                return {"ok": False, "tool": name, "error": f"Invalid JSON tool arguments: {exc}"}
        elif isinstance(arguments, dict):
            parsed_args = arguments
        else:
            return {"ok": False, "tool": name, "error": "Tool arguments must be a JSON object or string"}

        if not isinstance(parsed_args, dict):
            return {"ok": False, "tool": name, "error": "Decoded tool arguments must be a JSON object"}

        schema = _SCHEMA_BY_NAME[name]
        try:
            cleaned_args = _validate_tool_args(schema, parsed_args)
            self.call_count += 1
            result = self._handlers[name](**cleaned_args)
            bounded = _truncate_payload(result, self.max_output_chars)
            self.call_log.append({"tool": name, "ok": True, "call_index": self.call_count})
            return bounded
        except (ToolExecutionError, LspProtocolError, ValueError, TypeError) as exc:
            self.call_log.append({"tool": name, "ok": False, "error": str(exc)})
            return {"ok": False, "tool": name, "error": str(exc)}
