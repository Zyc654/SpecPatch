"""Java method signature parsing and deterministic Tier-A / Tier-B routing."""

from __future__ import annotations

from pathlib import Path
import re
from typing import Any, Iterable, Literal

from ..analysis.ast_context import _strip_comments_and_strings
from .patch_blind import strip_to_patch_blind
from .pkl_loader import FurinaDataLoader
from .schemas import (
    BoundaryContext,
    DatasetLoadResult,
    DatasetSpec,
    IntentContextPack,
    MethodSignatureInfo,
    PatchBlindTask,
    PatchTargetSelector,
    TierDecision,
    TierType,
)

_MODIFIERS = {
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
_VISIBILITIES = ("public", "protected", "private")

_DEFAULT_TIER_B_PROJECTS = frozenset({"Closure", "Mockito"})
_DEFAULT_TIER_A_PROJECTS = frozenset({"Lang", "Math", "Csv", "Codec"})

_TIER_B_TYPE_KEYWORDS = frozenset(
    {
        "Node",
        "Compiler",
        "CompilerOptions",
        "NodeTraversal",
        "JSType",
        "ObjectType",
        "FunctionType",
        "Scope",
        "ControlFlowGraph",
        "Graphics2D",
        "PlotRenderingInfo",
        "Rectangle2D",
        "CategoryDataset",
        "PiePlot",
        "InputStream",
        "OutputStream",
        "Reader",
        "Writer",
        "Appendable",
        "Socket",
        "Channel",
        "File",
        "Path",
        "ClassLoader",
        "Thread",
        "Executor",
        "ExecutorService",
        "JsonParser",
        "JsonGenerator",
        "DeserializationContext",
        "SerializerProvider",
        "ObjectMapper",
        "Element",
        "Document",
        "TokenQueue",
        "TreeBuilder",
        "Invocation",
        "MockSettings",
        "Answer",
        "JXPathContext",
        "NodePointer",
    }
)

_TIER_A_FRIENDLY_BASE_TYPES = frozenset(
    {
        "void",
        "boolean",
        "byte",
        "short",
        "int",
        "long",
        "float",
        "double",
        "char",
        "Boolean",
        "Byte",
        "Short",
        "Integer",
        "Long",
        "Float",
        "Double",
        "Character",
        "Number",
        "String",
        "CharSequence",
        "StringBuilder",
        "StringBuffer",
        "BigInteger",
        "BigDecimal",
        "Object",
        "Class",
        "Locale",
        "Random",
        "Date",
        "Calendar",
        "TimeZone",
        "List",
        "ArrayList",
        "LinkedList",
        "Set",
        "HashSet",
        "LinkedHashSet",
        "Map",
        "HashMap",
        "LinkedHashMap",
        "Collection",
        "Iterable",
        "Iterator",
        "Comparator",
        "Pattern",
    }
)

_THIS_WRITE_RE = re.compile(r"\bthis\.[A-Za-z_$][A-Za-z0-9_$]*\s*(?:[+\-*/%&|^]?=(?!=)|\+\+|--)")
_THIS_READ_RE = re.compile(r"\bthis\.[A-Za-z_$][A-Za-z0-9_$]*\b")
_IDENT_RE = re.compile(r"[A-Za-z_$][A-Za-z0-9_$]*")


def _split_top_level_commas(text: str) -> list[str]:
    """Split `text` by commas outside `<>`, `()`, and `[]`."""
    parts: list[str] = []
    buf: list[str] = []
    angle = 0
    paren = 0
    bracket = 0
    for ch in text:
        if ch == "<":
            angle += 1
        elif ch == ">" and angle > 0:
            angle -= 1
        elif ch == "(":
            paren += 1
        elif ch == ")" and paren > 0:
            paren -= 1
        elif ch == "[":
            bracket += 1
        elif ch == "]" and bracket > 0:
            bracket -= 1
        elif ch == "," and angle == 0 and paren == 0 and bracket == 0:
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


def _strip_leading_annotations(text: str) -> str:
    """Strip leading Java `@Annotation` and `@Annotation(...)` blocks."""
    idx = 0
    n = len(text)
    while idx < n:
        while idx < n and text[idx].isspace():
            idx += 1
        if idx >= n or text[idx] != "@":
            break
        idx += 1
        while idx < n and (text[idx].isalnum() or text[idx] in "._$"):
            idx += 1
        while idx < n and text[idx].isspace():
            idx += 1
        if idx < n and text[idx] == "(":
            depth = 1
            idx += 1
            while idx < n and depth > 0:
                if text[idx] == "(":
                    depth += 1
                elif text[idx] == ")":
                    depth -= 1
                idx += 1
    return text[idx:].strip()


def _strip_leading_generic_params(text: str) -> str:
    stripped = text.lstrip()
    if not stripped.startswith("<"):
        return stripped
    depth = 0
    for idx, ch in enumerate(stripped):
        if ch == "<":
            depth += 1
        elif ch == ">":
            depth -= 1
            if depth == 0:
                return stripped[idx + 1 :].lstrip()
    return stripped


def _extract_header_before_body(masked_source: str) -> str:
    """Extract the method/constructor header up to the opening body `{` or abstract `;`."""
    paren_depth = 0
    bracket_depth = 0
    angle_depth = 0
    for idx, ch in enumerate(masked_source):
        if ch == "(":
            paren_depth += 1
        elif ch == ")" and paren_depth > 0:
            paren_depth -= 1
        elif ch == "[":
            bracket_depth += 1
        elif ch == "]" and bracket_depth > 0:
            bracket_depth -= 1
        elif ch == "<":
            angle_depth += 1
        elif ch == ">" and angle_depth > 0:
            angle_depth -= 1
        elif paren_depth == 0 and bracket_depth == 0 and ch in {"{", ";"}:
            return masked_source[:idx].strip()
    return ""


def _parse_single_parameter(param_text: str) -> tuple[str, str]:
    cleaned = _strip_leading_annotations(param_text)
    # Remove inline `final` modifier tokens if present.
    tokens: list[str] = []
    depth = 0
    buf: list[str] = []
    for ch in cleaned:
        if ch in "<[":
            depth += 1
            buf.append(ch)
        elif ch in ">]" and depth > 0:
            depth -= 1
            buf.append(ch)
        elif ch.isspace() and depth == 0:
            if buf:
                tok = "".join(buf)
                if tok != "final":
                    tokens.append(tok)
                buf = []
        else:
            buf.append(ch)
    if buf:
        tok = "".join(buf)
        if tok != "final":
            tokens.append(tok)

    if not tokens:
        return "", ""
    if len(tokens) == 1:
        return tokens[0], ""

    raw_name = tokens[-1]
    array_suffix = ""
    while raw_name.endswith("[]"):
        array_suffix += "[]"
        raw_name = raw_name[:-2]

    if raw_name.startswith("..."):
        param_type = " ".join(tokens[:-1]).strip() + "..."
        param_name = raw_name[3:]
    else:
        param_type = (" ".join(tokens[:-1]).strip() + array_suffix).strip()
        param_name = raw_name
    return param_type, param_name


def extract_method_signature(buggy_function_code: str, file_path: str = "") -> MethodSignatureInfo | None:
    """Parse a Java method/constructor signature from a pre-patch function snippet."""
    if not buggy_function_code or not buggy_function_code.strip():
        return None

    masked, javadocs = _strip_comments_and_strings(buggy_function_code)
    header = _extract_header_before_body(masked)
    if not header:
        return None

    # Collapse whitespace to normalize multiline signatures.
    collapsed_header = " ".join(header.split())
    no_ann_header = _strip_leading_annotations(collapsed_header)
    open_paren = no_ann_header.find("(")
    if open_paren == -1:
        return None

    depth = 0
    close_paren = -1
    for idx in range(open_paren, len(no_ann_header)):
        ch = no_ann_header[idx]
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                close_paren = idx
                break
    if close_paren == -1:
        return None

    prefix = no_ann_header[:open_paren].strip()
    params_raw = no_ann_header[open_paren + 1 : close_paren].strip()
    suffix = no_ann_header[close_paren + 1 :].strip()

    # Tokenize leading modifiers.
    modifiers: list[str] = []
    rest = prefix
    while rest:
        first_space = rest.find(" ")
        head_tok = rest if first_space == -1 else rest[:first_space]
        if head_tok in _MODIFIERS:
            modifiers.append(head_tok)
            rest = "" if first_space == -1 else rest[first_space + 1 :].lstrip()
        else:
            break

    rest = _strip_leading_generic_params(rest)
    if not rest:
        return None

    name_match = re.search(r"([A-Za-z_$][A-Za-z0-9_$]*)\s*$", rest)
    if not name_match:
        return None
    method_name = name_match.group(1)
    return_type = rest[: name_match.start()].strip()
    is_constructor = return_type == ""

    parameter_types: list[str] = []
    parameter_names: list[str] = []
    if params_raw:
        for param in _split_top_level_commas(params_raw):
            ptype, pname = _parse_single_parameter(param)
            if ptype:
                parameter_types.append(ptype)
                parameter_names.append(pname)

    throws_types: list[str] = []
    throws_match = re.search(r"\bthrows\b(.*)$", suffix)
    if throws_match:
        throws_types = _split_top_level_commas(throws_match.group(1).strip())

    visibility = "package-private"
    for vis in _VISIBILITIES:
        if vis in modifiers:
            visibility = vis
            break

    class_name = Path(file_path).stem if file_path else ""
    if is_constructor and not class_name:
        class_name = method_name

    javadoc = ""
    if javadocs:
        # Attach the first Javadoc preceding the method signature if present.
        first_doc_line = min(javadocs)
        javadoc = javadocs[first_doc_line]

    return MethodSignatureInfo(
        file_path=file_path,
        class_name=class_name,
        method_name=method_name,
        visibility=visibility,
        is_static=("static" in modifiers),
        is_constructor=is_constructor,
        return_type=return_type,
        parameter_types=parameter_types,
        parameter_names=parameter_names,
        throws_types=throws_types,
        modifiers=modifiers,
        javadoc=javadoc,
        raw_signature=collapsed_header,
    )


def _simple_type_names(java_type: str) -> list[str]:
    """Extract simple Java type identifiers from potentially qualified/generic type strings."""
    cleaned = java_type.replace("[]", "").replace("...", "")
    tokens: list[str] = []
    # Split on generics/commas/whitespace while keeping dotted names intact, then take trailing simple name.
    for segment in re.split(r"[<>,\s?&]+", cleaned):
        if not segment or segment in {"extends", "super"}:
            continue
        simple = segment.rsplit(".", 1)[-1]
        if _IDENT_RE.fullmatch(simple):
            tokens.append(simple)
    return tokens


def _all_types_tier_a_friendly(sig: MethodSignatureInfo) -> bool:
    types_to_check = list(sig.parameter_types)
    if sig.return_type:
        types_to_check.append(sig.return_type)
    for t in types_to_check:
        tokens = _simple_type_names(t)
        if not tokens:
            continue
        if not all(tok in _TIER_A_FRIENDLY_BASE_TYPES for tok in tokens):
            return False
    return True


def _has_tier_b_type_keyword(sig: MethodSignatureInfo) -> str | None:
    types_to_check = list(sig.parameter_types)
    if sig.return_type:
        types_to_check.append(sig.return_type)
    for t in types_to_check:
        for tok in _simple_type_names(t):
            if tok in _TIER_B_TYPE_KEYWORDS:
                return tok
    return None


class RuleBasedTierClassifier:
    """Deterministic Tier-A / Tier-B classifier satisfying the `TierClassifier` protocol."""

    def __init__(
        self,
        tier_b_projects: Iterable[str] | None = None,
        tier_a_preferred_projects: Iterable[str] | None = None,
        max_tier_a_lines: int = 180,
    ) -> None:
        self.tier_b_projects = (
            frozenset(tier_b_projects) if tier_b_projects is not None else _DEFAULT_TIER_B_PROJECTS
        )
        self.tier_a_preferred_projects = (
            frozenset(tier_a_preferred_projects)
            if tier_a_preferred_projects is not None
            else _DEFAULT_TIER_A_PROJECTS
        )
        self.max_tier_a_lines = max_tier_a_lines

    def classify(self, intent_pack: IntentContextPack) -> TierType:
        """Protocol method returning `'tier_a'` or `'tier_b'` for an `IntentContextPack`."""
        return self.classify_detailed(intent_pack).tier

    def classify_detailed(self, source: IntentContextPack | PatchBlindTask) -> TierDecision:
        """Return a detailed `TierDecision` with confidence, reasons, and structural signals."""
        if isinstance(source, IntentContextPack):
            project = source.project
            anchors = source.anchors
            methods = list(source.target_methods)
            if not methods:
                methods = _extract_methods_from_anchors(anchors)
            boundary_context = source.boundary_context
            affected_files = sorted({a.file_path for a in anchors if a.file_path})
            has_fallback_anchor = any(a.is_fallback_slice for a in anchors)
        else:
            project = source.project
            anchors = source.anchors
            methods = _extract_methods_from_anchors(anchors)
            boundary_context = BoundaryContext()
            affected_files = list(source.affected_files)
            has_fallback_anchor = source.has_fallback_anchor

        reasons: list[str] = []
        tier_b_triggers: list[str] = []

        if project in self.tier_b_projects:
            tier_b_triggers.append(f"project '{project}' is stateful/framework-coupled (Tier-B)")

        if len(affected_files) > 1:
            tier_b_triggers.append(f"multi-file modification across {len(affected_files)} files")

        if not methods and has_fallback_anchor:
            tier_b_triggers.append("no enclosing method signature parsed (fallback hunk anchor)")

        distinct_methods = {m.method_name for m in methods if m.method_name}
        if len(distinct_methods) > 1:
            tier_b_triggers.append(f"modifies multiple distinct methods: {sorted(distinct_methods)}")

        for sig in methods:
            heavy_kw = _has_tier_b_type_keyword(sig)
            if heavy_kw is not None:
                tier_b_triggers.append(f"uses framework/IO/AST type '{heavy_kw}' in {sig.method_name}")
            if "synchronized" in sig.modifiers or "native" in sig.modifiers:
                tier_b_triggers.append(f"uses concurrent/native modifier in {sig.method_name}")
            if sig.is_constructor and not _all_types_tier_a_friendly(sig):
                tier_b_triggers.append(f"constructor '{sig.method_name}' depends on non-primitive state")

        max_func_lines = 0
        has_this_write = False
        has_this_read = False
        for anchor in anchors:
            if anchor.buggy_function_code:
                line_count = len(anchor.buggy_function_code.splitlines())
                max_func_lines = max(max_func_lines, line_count)
                masked_code, _ = _strip_comments_and_strings(anchor.buggy_function_code)
                if _THIS_WRITE_RE.search(masked_code):
                    has_this_write = True
                if _THIS_READ_RE.search(masked_code):
                    has_this_read = True

        if max_func_lines > self.max_tier_a_lines:
            tier_b_triggers.append(
                f"function length ({max_func_lines} lines) exceeds Tier-A limit ({self.max_tier_a_lines})"
            )

        if has_this_write and any(not m.is_static for m in methods):
            tier_b_triggers.append("mutates instance fields via `this.`")

        if any(m.visibility == "private" and not m.is_static for m in methods) and (
            has_this_read or len(boundary_context.class_fields) > 0
        ):
            tier_b_triggers.append("private instance method coupled to class fields")

        if len(boundary_context.non_local_state_touches) >= 3:
            tier_b_triggers.append(
                f"heavy non-local state touches ({len(boundary_context.non_local_state_touches)})"
            )
        if len(boundary_context.overrides) >= 2 and len(boundary_context.class_fields) >= 2:
            tier_b_triggers.append("polymorphic override hierarchy with stateful fields")

        all_friendly = bool(methods) and all(_all_types_tier_a_friendly(m) for m in methods)
        any_static = any(m.is_static for m in methods)

        if not tier_b_triggers and project not in self.tier_a_preferred_projects and not all_friendly:
            tier_b_triggers.append(
                f"project '{project}' requires non-primitive parameter/return types outside Tier-A allowlist"
            )

        signals = {
            "project": project,
            "num_anchors": len(anchors),
            "num_files": len(affected_files),
            "has_fallback_anchor": has_fallback_anchor,
            "distinct_methods": sorted(distinct_methods),
            "any_static": any_static,
            "all_types_tier_a_friendly": all_friendly,
            "has_this_read": has_this_read,
            "has_this_write": has_this_write,
            "max_function_lines": max_func_lines,
        }

        if tier_b_triggers:
            confidence = min(0.98, 0.70 + 0.08 * len(tier_b_triggers))
            return TierDecision(
                tier="tier_b",
                confidence=round(confidence, 2),
                reasons=tier_b_triggers,
                signals=signals,
            )

        if any_static:
            reasons.append("static target method with no mutating `this.` state")
        else:
            reasons.append("single-method weak-state function without field mutation")
        if all_friendly:
            reasons.append("all parameters and return types are Tier-A friendly primitives/Strings/collections")
        if project in self.tier_a_preferred_projects:
            reasons.append(f"project '{project}' is in Tier-A benchmark scope")

        confidence = 0.92 if (any_static and all_friendly) else (0.85 if all_friendly else 0.75)
        return TierDecision(
            tier="tier_a",
            confidence=round(confidence, 2),
            reasons=reasons,
            signals=signals,
        )

    def filter_tasks(
        self,
        tasks: list[PatchBlindTask],
        target_tier: Literal["tier_a", "tier_b", "auto"] = "tier_a",
    ) -> list[tuple[PatchBlindTask, TierDecision]]:
        """Filter `PatchBlindTask` instances by target tier (`'auto'` keeps all)."""
        selected: list[tuple[PatchBlindTask, TierDecision]] = []
        for task in tasks:
            decision = self.classify_detailed(task)
            if target_tier == "auto" or decision.tier == target_tier:
                selected.append((task, decision))
        return selected


def _extract_methods_from_anchors(anchors: list) -> list[MethodSignatureInfo]:
    methods: list[MethodSignatureInfo] = []
    seen_keys: set[tuple[str, int | None, str]] = set()
    for anchor in anchors:
        if not anchor.buggy_function_code:
            continue
        sig = extract_method_signature(anchor.buggy_function_code, file_path=anchor.file_path)
        if sig is None:
            continue
        key = (sig.file_path, anchor.function_start_line, sig.method_name)
        if key not in seen_keys:
            seen_keys.add(key)
            methods.append(sig)
    return methods


def build_initial_intent_pack(
    task: PatchBlindTask,
    classifier: RuleBasedTierClassifier | None = None,
) -> IntentContextPack:
    """Build an initial Patch-Blind `IntentContextPack` from a `PatchBlindTask`."""
    effective_classifier = classifier or RuleBasedTierClassifier()
    target_methods = _extract_methods_from_anchors(task.anchors)
    decision = effective_classifier.classify_detailed(task)

    summary_parts: list[str] = []
    if task.bug_report.title:
        summary_parts.append(task.bug_report.title)
    if task.bug_report.description:
        summary_parts.append(task.bug_report.description[:500])
    elif task.failing_tests:
        summary_parts.append(task.failing_tests[0].failure_message[:500])

    return IntentContextPack(
        bug_id=task.bug_id,
        patch_id=task.patch_id,
        project=task.project,
        tier=decision.tier,
        bug_intent_summary="\n".join(summary_parts).strip(),
        anchors=list(task.anchors),
        target_methods=target_methods,
        failing_tests=list(task.failing_tests),
        bug_report=task.bug_report,
        entry_harness_skeleton="",
        boundary_context=BoundaryContext(),
        tier_decision=decision,
        extra_context={
            "is_multi_hunk": task.is_multi_hunk,
            "is_multi_file": task.is_multi_file,
            "has_fallback_anchor": task.has_fallback_anchor,
        },
    )


def load_intent_packs_for_dataset(
    loader: FurinaDataLoader,
    source: str | Path | DatasetSpec | Iterable[str | dict[str, Any] | PatchTargetSelector] | None = None,
    *,
    target_tier: Literal["tier_a", "tier_b", "auto"] = "auto",
    classifier: RuleBasedTierClassifier | None = None,
    verify_patch_blind: bool = True,
    **load_kwargs: Any,
) -> tuple[DatasetLoadResult, list[IntentContextPack]]:
    """Selectively load a user dataset slice, strip to Patch-Blind tasks, and route Tier-A/Tier-B."""
    load_result = loader.load_dataset(source=source, **load_kwargs)
    effective_classifier = classifier or RuleBasedTierClassifier()
    packs: list[IntentContextPack] = []
    for record in load_result.records:
        task = strip_to_patch_blind(record, verify=verify_patch_blind)
        pack = build_initial_intent_pack(task, effective_classifier)
        if target_tier == "auto" or pack.tier == target_tier:
            packs.append(pack)
    return load_result, packs
