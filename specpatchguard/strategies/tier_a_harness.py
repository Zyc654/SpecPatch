"""Tier-A standalone JUnit 4 harness builder and Python->Java boundary literal converter."""

from __future__ import annotations

import math
from pathlib import PurePosixPath
import re
from typing import Any

from ..data.schemas import (
    HarnessRunResult,
    IntentContextPack,
    MethodSignatureInfo,
    PrePostSpec,
    TierType,
)
from ..sandbox.junit_injector import CASE_PREFIX, InjectedTestSpec
from .protocols import parse_harness_raw_result


class HarnessBuildError(ValueError):
    """Raised when Python boundary values or target signatures cannot form safe JDK 8 JUnit source."""


_PRIMITIVE_TYPES = frozenset({"boolean", "byte", "short", "int", "long", "float", "double", "char"})
_JAVA_FLOAT_MAX = 3.4028235e38
_JAVA_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_SOURCE_ROOT_MARKERS = (
    "src/main/java/",
    "src/java/",
    "source/",
    "src/",
)


def _strip_generics(java_type: str) -> str:
    """Strip outermost generic parameters while preserving array brackets."""
    buf: list[str] = []
    depth = 0
    for ch in java_type.strip():
        if ch == "<":
            depth += 1
        elif ch == ">" and depth > 0:
            depth -= 1
        elif depth == 0:
            buf.append(ch)
    return "".join(buf).strip()


def _extract_generic_args(java_type: str) -> list[str]:
    start = java_type.find("<")
    end = java_type.rfind(">")
    if start == -1 or end <= start:
        return []
    inner = java_type[start + 1 : end]
    parts: list[str] = []
    buf: list[str] = []
    depth = 0
    for ch in inner:
        if ch == "<":
            depth += 1
        elif ch == ">" and depth > 0:
            depth -= 1
        elif ch == "," and depth == 0:
            part = "".join(buf).strip()
            if part:
                parts.append(part)
            buf = []
            continue
        buf.append(ch)
    tail = "".join(buf).strip()
    if tail:
        parts.append(tail)
    return parts


def _sanitize_java_comment(text: str) -> str:
    return text.replace("\r", " ").replace("\n", " ").replace("*/", "* /")


def _normalize_generic_type_arg(type_arg: str) -> str:
    cleaned = type_arg.strip()
    if not cleaned or cleaned == "?" or cleaned.startswith("? super "):
        return "Object"
    if cleaned.startswith("? extends "):
        return cleaned[len("? extends ") :].strip() or "Object"
    return cleaned


def _format_as_list_call(elem_type: str, elems: str) -> str:
    if "<" in elem_type or "[" in elem_type:
        return f"java.util.Arrays.<{elem_type}>asList({elems})"
    return f"java.util.Arrays.asList({elems})"


def _simple_type_name(java_type: str) -> str:
    base = _strip_generics(java_type).replace(" ", "")
    if "." in base and not base.endswith("[]") and not base.endswith("..."):
        return base.rsplit(".", 1)[-1]
    if "." in base:
        suffix = "[]" if base.endswith("[]") else "..."
        core = base[:-2] if base.endswith("[]") else base[:-3]
        return core.rsplit(".", 1)[-1] + suffix
    return base


def _encode_java_string_literal(text: str) -> str:
    """Encode a Python str into a valid JDK 8 string literal WITHOUT `\\u000a`/`\\u0022` traps."""
    out = ['"']
    for ch in text:
        code = ord(ch)
        if ch == "\\":
            out.append("\\\\")
        elif ch == '"':
            out.append('\\"')
        elif ch == "\n":
            out.append("\\n")
        elif ch == "\r":
            out.append("\\r")
        elif ch == "\t":
            out.append("\\t")
        elif ch == "\b":
            out.append("\\b")
        elif ch == "\f":
            out.append("\\f")
        elif code < 0x20 or code == 0x7F:
            out.append(f"\\{code:03o}")
        else:
            out.append(ch)
    out.append('"')
    return "".join(out)


def _encode_java_char_literal(value: Any) -> str:
    if isinstance(value, int) and not isinstance(value, bool):
        if not 0 <= value <= 65535:
            raise HarnessBuildError(f"char codepoint out of range [0, 65535]: {value}")
        ch = chr(value)
    elif isinstance(value, str) and len(value) == 1:
        ch = value
    else:
        raise HarnessBuildError(f"Cannot convert {value!r} to Java char literal")

    code = ord(ch)
    if ch == "\\":
        return "'\\\\'"
    if ch == "'":
        return "'\\''"
    if ch == "\n":
        return "'\\n'"
    if ch == "\r":
        return "'\\r'"
    if ch == "\t":
        return "'\\t'"
    if ch == "\b":
        return "'\\b'"
    if ch == "\f":
        return "'\\f'"
    if code < 0x20 or code == 0x7F:
        return f"'\\{code:03o}'"
    return f"'{ch}'"


def _decode_special_float(value: Any) -> float | None:
    if isinstance(value, dict) and set(value) == {"__spg_float__"}:
        tag = str(value["__spg_float__"])
        if tag == "NaN":
            return float("nan")
        if tag in {"Infinity", "+Infinity"}:
            return float("inf")
        if tag == "-Infinity":
            return float("-inf")
        if tag == "-0.0":
            return -0.0
        raise HarnessBuildError(f"Unsupported __spg_float__ marker: {tag!r}")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    return None


def python_value_to_java_literal(value: Any, java_type: str) -> str:
    """Safely convert a Python/JSON boundary value into a JDK 8 Java expression of `java_type`."""
    raw_type = java_type.strip()
    if not raw_type:
        raise HarnessBuildError("java_type cannot be empty")
    simple = _simple_type_name(raw_type)
    generic_args = _extract_generic_args(raw_type)

    if simple.endswith("..."):
        elem_type = raw_type.strip()[:-3].strip() + "[]"
        simple = _simple_type_name(elem_type)
        raw_type = elem_type

    if value is None:
        if simple in _PRIMITIVE_TYPES:
            raise HarnessBuildError(f"Cannot pass null to Java primitive type '{simple}'")
        return "null"

    # Check arrays first.
    if simple.endswith("[]"):
        if not isinstance(value, (list, tuple)):
            raise HarnessBuildError(f"Expected list/tuple for Java array '{raw_type}', got {type(value).__name__}")
        elem_raw = raw_type.strip()[:-2].strip()
        elems = ", ".join(python_value_to_java_literal(item, elem_raw) for item in value)
        base_no_generics = _strip_generics(elem_raw)
        return f"new {base_no_generics}[] {{ {elems} }}"

    if simple in {"boolean", "Boolean"}:
        if type(value) is not bool:
            raise HarnessBuildError(f"Expected bool for Java '{simple}', got {value!r}")
        lit = "true" if value else "false"
        return lit if simple == "boolean" else f"Boolean.valueOf({lit})"

    if simple in {"char", "Character"}:
        char_lit = _encode_java_char_literal(value)
        return char_lit if simple == "char" else f"Character.valueOf({char_lit})"

    if simple in {"byte", "Byte", "short", "Short", "int", "Integer", "long", "Long", "BigInteger"}:
        if isinstance(value, bool) or not isinstance(value, int):
            raise HarnessBuildError(f"Expected integer for Java '{simple}', got {value!r}")
        if simple in {"byte", "Byte"}:
            if not -128 <= value <= 127:
                raise HarnessBuildError(f"byte overflow: {value}")
            return f"(byte) {value}" if simple == "byte" else f"Byte.valueOf((byte) {value})"
        if simple in {"short", "Short"}:
            if not -32768 <= value <= 32767:
                raise HarnessBuildError(f"short overflow: {value}")
            return f"(short) {value}" if simple == "short" else f"Short.valueOf((short) {value})"
        if simple in {"int", "Integer"}:
            if not -2147483648 <= value <= 2147483647:
                raise HarnessBuildError(f"int overflow: {value}")
            expr = "Integer.MIN_VALUE" if value == -2147483648 else str(value)
            return expr if simple == "int" else f"Integer.valueOf({expr})"
        if simple in {"long", "Long"}:
            if not -9223372036854775808 <= value <= 9223372036854775807:
                raise HarnessBuildError(f"long overflow: {value}")
            expr = "Long.MIN_VALUE" if value == -9223372036854775808 else f"{value}L"
            return expr if simple == "long" else f"Long.valueOf({expr})"
        return f'new java.math.BigInteger("{value}")'

    if simple in {"float", "Float", "double", "Double", "Number", "BigDecimal"}:
        num = _decode_special_float(value)
        if num is None:
            raise HarnessBuildError(f"Expected numeric/float value for Java '{simple}', got {value!r}")
        if simple == "BigDecimal":
            if not math.isfinite(num):
                raise HarnessBuildError(f"BigDecimal cannot represent non-finite float {num}")
            return f'new java.math.BigDecimal("{repr(num)}")'
        if simple in {"float", "Float"}:
            if math.isnan(num):
                expr = "Float.NaN"
            elif math.isinf(num):
                expr = "Float.POSITIVE_INFINITY" if num > 0 else "Float.NEGATIVE_INFINITY"
            elif abs(num) > _JAVA_FLOAT_MAX:
                raise HarnessBuildError(f"float overflow: {num}")
            elif num == 0.0 and math.copysign(1.0, num) < 0:
                expr = "-0.0f"
            else:
                expr = f"{repr(num)}f"
            return expr if simple == "float" else f"Float.valueOf({expr})"
        if math.isnan(num):
            expr = "Double.NaN"
        elif math.isinf(num):
            expr = "Double.POSITIVE_INFINITY" if num > 0 else "Double.NEGATIVE_INFINITY"
        elif num == 0.0 and math.copysign(1.0, num) < 0:
            expr = "-0.0d"
        else:
            expr = f"{repr(num)}d"
        return expr if simple == "double" else f"Double.valueOf({expr})"

    if simple in {"String", "CharSequence"}:
        if not isinstance(value, str):
            raise HarnessBuildError(f"Expected str for Java '{simple}', got {value!r}")
        return _encode_java_string_literal(value)

    if simple in {"StringBuilder", "StringBuffer"}:
        if not isinstance(value, str):
            raise HarnessBuildError(f"Expected str for Java '{simple}', got {value!r}")
        return f"new {simple}({_encode_java_string_literal(value)})"

    if simple in {"List", "ArrayList", "LinkedList", "Collection", "Iterable"}:
        if not isinstance(value, (list, tuple)):
            raise HarnessBuildError(f"Expected list for Java '{simple}', got {type(value).__name__}")
        elem_type = _normalize_generic_type_arg(generic_args[0] if generic_args else "Object")
        impl = "java.util.LinkedList" if simple == "LinkedList" else "java.util.ArrayList"
        if not value:
            return f"new {impl}<{elem_type}>()"
        rendered_elems = []
        for item in value:
            lit = python_value_to_java_literal(item, elem_type)
            if lit == "null":
                lit = f"({elem_type}) null"
            rendered_elems.append(lit)
        elems = ", ".join(rendered_elems)
        return f"new {impl}<{elem_type}>({_format_as_list_call(elem_type, elems)})"

    if simple in {"Set", "HashSet", "LinkedHashSet"}:
        if not isinstance(value, (list, tuple, set)):
            raise HarnessBuildError(f"Expected sequence for Java '{simple}', got {type(value).__name__}")
        elem_type = _normalize_generic_type_arg(generic_args[0] if generic_args else "Object")
        impl = "java.util.LinkedHashSet" if simple == "LinkedHashSet" else "java.util.HashSet"
        seq = list(value)
        if not seq:
            return f"new {impl}<{elem_type}>()"
        rendered_elems = []
        for item in seq:
            lit = python_value_to_java_literal(item, elem_type)
            if lit == "null":
                lit = f"({elem_type}) null"
            rendered_elems.append(lit)
        elems = ", ".join(rendered_elems)
        return f"new {impl}<{elem_type}>({_format_as_list_call(elem_type, elems)})"

    if simple in {"Map", "HashMap", "LinkedHashMap"}:
        if not isinstance(value, dict):
            raise HarnessBuildError(f"Expected dict for Java '{simple}', got {type(value).__name__}")
        k_type = _normalize_generic_type_arg(generic_args[0] if len(generic_args) >= 1 else "Object")
        v_type = _normalize_generic_type_arg(generic_args[1] if len(generic_args) >= 2 else "Object")
        impl = "java.util.LinkedHashMap" if simple == "LinkedHashMap" else "java.util.HashMap"
        if not value:
            return f"new {impl}<{k_type}, {v_type}>()"
        puts = "".join(
            f"put({python_value_to_java_literal(k, k_type)}, {python_value_to_java_literal(v, v_type)}); "
            for k, v in value.items()
        )
        return f"new {impl}<{k_type}, {v_type}>() {{{{ {puts}}}}}"

    if simple == "Object":
        if type(value) is bool:
            return f"Boolean.valueOf({'true' if value else 'false'})"
        if isinstance(value, int):
            return python_value_to_java_literal(value, "Integer")
        if _decode_special_float(value) is not None:
            return python_value_to_java_literal(value, "Double")
        if isinstance(value, str):
            return _encode_java_string_literal(value)
        if isinstance(value, list):
            return python_value_to_java_literal(value, "List<Object>")
        if isinstance(value, dict):
            return python_value_to_java_literal(value, "Map<Object, Object>")

    if simple == "Locale" and isinstance(value, str):
        parts = value.replace("-", "_").split("_")
        args = ", ".join(_encode_java_string_literal(p) for p in parts[:3] if p)
        return f"new java.util.Locale({args})" if args else "java.util.Locale.ROOT"

    if simple == "Pattern" and isinstance(value, str):
        return f"java.util.regex.Pattern.compile({_encode_java_string_literal(value)})"

    raise HarnessBuildError(f"Unsupported Java type '{java_type}' for value {value!r}")


def infer_java_package_and_class(file_path: str, class_name: str = "") -> tuple[str, str]:
    """Infer `(package_name, target_class_name)` from a Defects4J repository-relative Java path."""
    norm = file_path.replace("\\", "/").strip("/")
    pkg_rel = norm
    for marker in _SOURCE_ROOT_MARKERS:
        idx = norm.find(marker)
        if idx != -1:
            pkg_rel = norm[idx + len(marker) :]
            break
    else:
        for idx, part in enumerate(PurePosixPath(norm).parts):
            if part in {"org", "com", "net", "edu", "io", "java", "javax"}:
                pkg_rel = "/".join(PurePosixPath(norm).parts[idx:])
                break

    pure = PurePosixPath(pkg_rel)
    pkg_parts = list(pure.parent.parts)
    if pkg_parts == ["."]:
        pkg_parts = []
    package_name = ".".join(pkg_parts)
    cls_name = class_name.strip() or pure.stem
    return package_name, cls_name


def _select_target_method(intent_pack: IntentContextPack) -> MethodSignatureInfo:
    if intent_pack.target_methods:
        return intent_pack.target_methods[0]
    if intent_pack.anchors:
        anchor = intent_pack.anchors[0]
        _, cls_name = infer_java_package_and_class(anchor.file_path)
        return MethodSignatureInfo(
            file_path=anchor.file_path,
            class_name=cls_name,
            method_name=anchor.method_name or "targetMethod",
            visibility="public",
            is_static=True,
            is_constructor=False,
            return_type="Object",
            parameter_types=[],
            parameter_names=[],
            throws_types=[],
            modifiers=["public", "static"],
            javadoc="",
            raw_signature="",
        )
    raise HarnessBuildError("IntentContextPack has neither target_methods nor anchors")


def _render_case_block(
    idx: int,
    case: dict[str, Any],
    sig: MethodSignatureInfo,
    target_class: str,
    receiver_setup_java: str,
) -> tuple[str, str]:
    """Render one isolated try/catch JUnit case block or raise `HarnessBuildError` if unconvertible."""
    raw_id = str(case.get("case_id") or f"case_{idx}")
    safe_id_literal = _encode_java_string_literal(raw_id)
    args = case.get("args", [])
    if not isinstance(args, list):
        args = [args]
    if len(args) != len(sig.parameter_types):
        raise HarnessBuildError(
            f"Case '{raw_id}' argument arity {len(args)} != signature arity {len(sig.parameter_types)}"
        )

    java_args: list[str] = []
    for arg_val, param_type in zip(args, sig.parameter_types):
        java_args.append(python_value_to_java_literal(arg_val, param_type))
    args_joined = ", ".join(java_args)

    if sig.is_constructor:
        invocation = f"new {target_class}({args_joined})"
    elif sig.is_static:
        invocation = f"{target_class}.{sig.method_name}({args_joined})"
    else:
        invocation = f"_target.{sig.method_name}({args_joined})"

    is_void = sig.return_type.strip() == "void" and not sig.is_constructor
    expected_exc = str(case.get("expected_exception") or "").strip()
    java_assertion = str(case.get("java_assertion") or "").strip()
    if any(tok in java_assertion for tok in ("\n", "\r", ";")):
        java_assertion = ""
    has_expected_ret = "expected_return" in case and case["expected_return"] is not None

    lines = [f"        // Case {idx}: {_sanitize_java_comment(raw_id)}"]
    lines.append("        try {")
    if not sig.is_static and not sig.is_constructor:
        setup = receiver_setup_java.strip() or f"{target_class} _target = new {target_class}();"
        for setup_line in setup.splitlines():
            lines.append(f"            {setup_line}")

    simple_ret = _simple_type_name(sig.return_type) if not sig.is_constructor else target_class
    is_array_ret = not is_void and simple_ret.endswith("[]")
    is_deep_array_ret = False
    if is_array_ret:
        elem_ret = simple_ret[:-2]
        is_deep_array_ret = elem_ret.endswith("[]") or (elem_ret not in _PRIMITIVE_TYPES)

    if is_void:
        lines.append(f"            {invocation};")
        actual_expr = '"void"'
    else:
        ret_type = target_class if sig.is_constructor else sig.return_type
        lines.append(f"            {ret_type} _actual = {invocation};")
        if is_deep_array_ret:
            actual_expr = "java.util.Arrays.deepToString(_actual)"
        elif is_array_ret:
            actual_expr = "java.util.Arrays.toString(_actual)"
        else:
            actual_expr = "String.valueOf((Object) _actual)"

    if expected_exc:
        expected_exc_msg = _encode_java_string_literal(
            f"Expected exception {expected_exc} was not thrown"
        )
        lines.append(
            f"            _spgFail({safe_id_literal}, {expected_exc_msg}, {actual_expr}, _failures);"
        )
    elif java_assertion:
        lines.append(f"            if (!({java_assertion})) {{")
        lines.append(
            f'                _spgFail({safe_id_literal}, "Postcondition failed: " + {_encode_java_string_literal(java_assertion)}, {actual_expr}, _failures);'
        )
        lines.append("            } else {")
        lines.append(f"                _spgPass({safe_id_literal}, {actual_expr});")
        lines.append("            }")
    elif has_expected_ret and not is_void:
        expected_lit = python_value_to_java_literal(case["expected_return"], sig.return_type)
        if is_deep_array_ret:
            cond = f"!java.util.Arrays.deepEquals(_actual, {expected_lit})"
            expected_str_expr = f"java.util.Arrays.deepToString({expected_lit})"
        elif is_array_ret:
            cond = f"!java.util.Arrays.equals(_actual, {expected_lit})"
            expected_str_expr = f"java.util.Arrays.toString({expected_lit})"
        elif simple_ret in {"float", "double"}:
            cond = f"Double.compare((double) _actual, (double) {expected_lit}) != 0"
            expected_str_expr = f"String.valueOf((Object) {expected_lit})"
        elif simple_ret in _PRIMITIVE_TYPES:
            cond = f"_actual != {expected_lit}"
            expected_str_expr = f"String.valueOf((Object) {expected_lit})"
        else:
            cond = f"!java.util.Objects.equals(_actual, {expected_lit})"
            expected_str_expr = f"String.valueOf((Object) {expected_lit})"
        lines.append(f"            if ({cond}) {{")
        lines.append(
            f'                _spgFail({safe_id_literal}, "Return mismatch, expected " + {expected_str_expr}, {actual_expr}, _failures);'
        )
        lines.append("            } else {")
        lines.append(f"                _spgPass({safe_id_literal}, {actual_expr});")
        lines.append("            }")
    else:
        lines.append(f"            _spgPass({safe_id_literal}, {actual_expr});")

    lines.append("        } catch (Throwable _t) {")
    if expected_exc:
        exc_lit = _encode_java_string_literal(expected_exc)
        cond_exc = (
            f"_t.getClass().getName().equals({exc_lit})"
            if "." in expected_exc
            else f"_t.getClass().getSimpleName().equals({exc_lit})"
        )
        lines.append(f"            if ({cond_exc}) {{")
        lines.append(f'                _spgPass({safe_id_literal}, _t.getClass().getName());')
        lines.append("            } else {")
        lines.append(f"                _spgError({safe_id_literal}, _t, _failures);")
        lines.append("            }")
    else:
        lines.append(f"            _spgError({safe_id_literal}, _t, _failures);")
    lines.append("        }")
    return "\n".join(lines), raw_id


class StandaloneFunctionUnitHarness:
    """Tier-A `TestHarnessStrategy` generating isolated JDK 8 + JUnit 4 unit harnesses."""

    tier: TierType = "tier_a"

    def __init__(self, default_class_name: str = "SpecPatchGuardGeneratedTest") -> None:
        if not _JAVA_IDENT_RE.fullmatch(default_class_name):
            raise HarnessBuildError(f"Invalid Java class name: {default_class_name!r}")
        self.default_class_name = default_class_name

    def build_junit_source(
        self,
        pre_post: PrePostSpec,
        candidate_inputs: list[dict[str, Any]],
        intent_pack: IntentContextPack,
        *,
        class_name: str | None = None,
        receiver_setup_java: str = "",
    ) -> str:
        sig = _select_target_method(intent_pack)
        package_name, target_class = infer_java_package_and_class(sig.file_path, sig.class_name)
        test_cls = (class_name or self.default_class_name).strip()
        if not _JAVA_IDENT_RE.fullmatch(test_cls):
            raise HarnessBuildError(f"Invalid generated test class name: {test_cls!r}")

        effective_inputs = list(candidate_inputs)
        if not effective_inputs:
            raise HarnessBuildError("candidate_inputs cannot be empty when building JUnit harness")

        case_blocks: list[str] = []
        skipped_notes: list[str] = []
        for idx, case in enumerate(effective_inputs, start=1):
            try:
                block, _ = _render_case_block(idx, case, sig, target_class, receiver_setup_java)
                case_blocks.append(block)
            except HarnessBuildError as exc:
                skipped_notes.append(
                    f"// Skipped case #{idx} ({_sanitize_java_comment(str(case.get('case_id', 'unknown')))}): "
                    f"{_sanitize_java_comment(str(exc))}"
                )

        if not case_blocks:
            raise HarnessBuildError(
                "All candidate inputs failed Java literal conversion: " + "; ".join(skipped_notes)
            )

        pkg_line = f"package {package_name};\n\n" if package_name else ""
        spec_comment = _sanitize_java_comment(
            " | ".join(pre_post.postconditions[:3]) if pre_post.postconditions else "SpecPatchGuard Tier-A Witness Run"
        )
        skipped_section = ("\n        " + "\n        ".join(skipped_notes)) if skipped_notes else ""
        prefix_literal = _encode_java_string_literal(CASE_PREFIX + " ")

        return (
            f"{pkg_line}"
            "import org.junit.Assert;\n"
            "import org.junit.Test;\n\n"
            f"// {spec_comment.replace(chr(10), ' ')}\n"
            f"public class {test_cls} {{\n\n"
            "    private static String _spgJsonEscape(String raw) {\n"
            "        if (raw == null) {\n"
            '            return "null";\n'
            "        }\n"
            "        return raw\n"
            '            .replace("\\\\", "\\\\\\\\")\n'
            '            .replace("\\"", "\\\\\\"")\n'
            '            .replace("\\n", "\\\\n")\n'
            '            .replace("\\r", "\\\\r")\n'
            '            .replace("\\t", "\\\\t")\n'
            '            .replace("\\b", "\\\\b")\n'
            '            .replace("\\f", "\\\\f");\n'
            "    }\n\n"
            "    private static void _spgPass(String caseId, String observed) {\n"
            f'        System.out.println({prefix_literal} + "{{\\"case_id\\":\\"" + _spgJsonEscape(caseId) + "\\",\\"status\\":\\"passed\\",\\"observed\\":\\"" + _spgJsonEscape(observed) + "\\"}}");\n'
            "    }\n\n"
            "    private static void _spgFail(String caseId, String msg, String observed, java.util.List<String> failures) {\n"
            f'        System.out.println({prefix_literal} + "{{\\"case_id\\":\\"" + _spgJsonEscape(caseId) + "\\",\\"status\\":\\"failed\\",\\"observed\\":\\"" + _spgJsonEscape(observed) + "\\",\\"message\\":\\"" + _spgJsonEscape(msg) + "\\"}}");\n'
            '        failures.add(caseId + ": " + msg);\n'
            "    }\n\n"
            "    private static void _spgError(String caseId, Throwable t, java.util.List<String> failures) {\n"
            "        String exc = t.getClass().getName();\n"
            "        String msg = t.getMessage() == null ? \"\" : t.getMessage();\n"
            f'        System.out.println({prefix_literal} + "{{\\"case_id\\":\\"" + _spgJsonEscape(caseId) + "\\",\\"status\\":\\"error\\",\\"exception_type\\":\\"" + _spgJsonEscape(exc) + "\\",\\"message\\":\\"" + _spgJsonEscape(msg) + "\\"}}");\n'
            '        failures.add(caseId + ": " + exc + ": " + msg);\n'
            "    }\n\n"
            "    @Test\n"
            "    public void testGeneratedBoundaryWitnesses() {\n"
            "        java.util.List<String> _failures = new java.util.ArrayList<String>();\n"
            f"{skipped_section}\n"
            + "\n\n".join(case_blocks)
            + "\n\n        if (!_failures.isEmpty()) {\n"
            '            Assert.fail("SpecPatchGuard witnesses triggered: " + _failures.size() + " -> " + _failures.get(0));\n'
            "        }\n"
            "    }\n"
            "}\n"
        )

    def build_injected_test_spec(
        self,
        pre_post: PrePostSpec,
        candidate_inputs: list[dict[str, Any]],
        intent_pack: IntentContextPack,
        *,
        class_name: str | None = None,
        receiver_setup_java: str = "",
    ) -> InjectedTestSpec:
        """Build a ready-to-inject `InjectedTestSpec` for `JUnitInjector.run_injected_test`."""
        sig = _select_target_method(intent_pack)
        package_name, _ = infer_java_package_and_class(sig.file_path, sig.class_name)
        test_cls = (class_name or self.default_class_name).strip()
        source = self.build_junit_source(
            pre_post,
            candidate_inputs,
            intent_pack,
            class_name=test_cls,
            receiver_setup_java=receiver_setup_java,
        )
        return InjectedTestSpec(
            package_name=package_name,
            class_name=test_cls,
            java_source=source,
            test_methods=["testGeneratedBoundaryWitnesses"],
        )

    def parse_execution_result(self, raw_result: dict[str, Any]) -> HarnessRunResult:
        return parse_harness_raw_result(raw_result, tier="tier_a")
