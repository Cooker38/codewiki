"""
Java Language Extractor — Python translation of codegraph/src/extraction/languages/java.ts.

Configures which AST node types map to which NodeKind, plus Java-specific hooks:
Lombok member synthesis, return type normalization, visibility/static/const detection,
import/package extraction.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from tree_sitter import Node as SyntaxNode

from codewiki.extraction.tree_sitter_helpers import get_child_by_field, get_node_text

# tree-sitter-java node types for a method's `type` field that can never be
# a method receiver (java.ts:10-15).
_JAVA_NON_CLASS_RETURN_NODES = frozenset([
    "void_type",
    "integral_type",   # int, long, short, byte, char
    "floating_point_type",  # float, double
    "boolean_type",
])


def normalize_java_type(type_node: Optional["SyntaxNode"], source: str) -> Optional[str]:
    """
    Normalize a Java type node to the bare class name (java.ts:24-35).
    Primitives/void/arrays → None. List<Foo> → List. java.util.List → List.
    """
    if not type_node:
        return None
    if type_node.type in _JAVA_NON_CLASS_RETURN_NODES:
        return None
    if type_node.type == "array_type":
        return None
    raw = get_node_text(type_node, source).strip()
    raw = re.sub(r"<[^>]*>", "", raw)  # Strip type arguments
    last = raw.split(".")[-1].strip()
    if not last or not re.match(r"^[A-Za-z_]\w*$", last):
        return None
    return last


def extract_java_return_type(node: "SyntaxNode", source: str) -> Optional[str]:
    """A Java method's declared return type (java.ts:41-43). Constructors → None."""
    return normalize_java_type(get_child_by_field(node, "type"), source)


# ---------------------------------------------------------------------------
# Lombok-generated member synthesis (java.ts:46-251)
# ---------------------------------------------------------------------------

_LOMBOK_LOG_ANNOTATIONS = frozenset([
    "Slf4j", "Log4j", "Log4j2", "Log", "CommonsLog", "JBossLog", "Flogger", "XSlf4j", "CustomLog",
])


def _lombok_annotation_names(node: "SyntaxNode") -> set[str]:
    """Simple names of annotations in a node's modifiers child (java.ts:59-71)."""
    names: set[str] = set()
    modifiers = None
    for i in range(node.named_child_count):
        child = node.named_child(i)
        if child and child.type == "modifiers":
            modifiers = child
            break
    if not modifiers:
        return names
    for i in range(modifiers.named_child_count):
        child = modifiers.named_child(i)
        if child and child.type in ("marker_annotation", "annotation"):
            name_node = get_child_by_field(child, "name")
            if name_node:
                simple = name_node.text.decode("utf-8").strip().split(".")[-1]
                if simple:
                    names.add(simple)
    return names


def _modifier_text(node: "SyntaxNode") -> str:
    """Text of a declaration's modifiers child (java.ts:74-77)."""
    for i in range(node.named_child_count):
        child = node.named_child(i)
        if child and child.type == "modifiers":
            return child.text.decode("utf-8")
    return ""


def _capitalize(name: str) -> str:
    return name[0].upper() + name[1:] if name else name


def _lombok_getter_name(field_name: str, is_boolean: bool) -> str:
    if is_boolean:
        return field_name if re.match(r"^is[A-Z]", field_name) else "is" + _capitalize(field_name)
    return "get" + _capitalize(field_name)


def _lombok_setter_name(field_name: str, is_boolean: bool) -> str:
    base = field_name[2:] if (is_boolean and re.match(r"^is[A-Z]", field_name)) else field_name
    return "set" + _capitalize(base)


def synthesize_lombok_members(class_node: "SyntaxNode", ctx) -> None:
    """
    Synthesize Lombok-generated members (java.ts:118-251).
    Covers @Getter/@Setter/@Data/@Value/@Builder/@ToString/@EqualsAndHashCode/@Slf4j.
    """
    class_anns = _lombok_annotation_names(class_node)
    class_getter = "Getter" in class_anns
    class_setter = "Setter" in class_anns
    is_data = "Data" in class_anns
    is_value = "Value" in class_anns
    has_builder = "Builder" in class_anns or "SuperBuilder" in class_anns
    has_to_string = is_data or is_value or "ToString" in class_anns
    has_equals = is_data or is_value or "EqualsAndHashCode" in class_anns
    log_ann = next((a for a in class_anns if a in _LOMBOK_LOG_ANNOTATIONS), None)

    body = get_child_by_field(class_node, "body")
    if not body:
        return

    fields = [body.named_child(i) for i in range(body.named_child_count)
              if body.named_child(i) and body.named_child(i).type == "field_declaration"]

    class_has_lombok = (class_getter or class_setter or is_data or is_value or
                        has_builder or has_to_string or has_equals or bool(log_ann))
    if not class_has_lombok and not any(_lombok_annotation_names(f) for f in fields):
        return

    # Track already-declared members
    class_id = ctx.node_stack[-1] if ctx.node_stack else None
    class_rec = next((n for n in ctx.nodes if n.id == class_id), None) if class_id else None
    class_qn = class_rec.qualified_name if class_rec else None
    taken_methods: set[str] = set()
    taken_fields: set[str] = set()
    if class_qn:
        for n in ctx.nodes:
            if n.file_path == ctx.file_path and n.qualified_name == f"{class_qn}::{n.name}":
                if n.kind in ("method", "function"):
                    taken_methods.add(n.name)
                elif n.kind in ("field", "variable", "constant", "property"):
                    taken_fields.add(n.name)

    class_name_node = get_child_by_field(class_node, "name") or class_node
    class_name = class_rec.name if class_rec else get_node_text(class_name_node, ctx.source).strip()

    def emit_method(name: str, anchor, signature: str, from_ann: str,
                    return_type: Optional[str] = None, is_static: bool = False):
        if not name or name in taken_methods:
            return
        taken_methods.add(name)
        ctx.create_node("method", name, anchor, {
            "visibility": "public",
            "signature": signature,
            "docstring": f"Lombok-generated ({from_ann})",
            "decorators": ["lombok"],
            "is_static": is_static,
            "return_type": return_type,
        })

    # Per-field getters/setters
    for fd in fields:
        mods = _modifier_text(fd)
        if re.search(r"\bstatic\b", mods):
            continue
        is_final = bool(re.search(r"\bfinal\b", mods))
        field_anns = _lombok_annotation_names(fd)
        field_getter = "Getter" in field_anns
        field_setter = "Setter" in field_anns

        want_getter = class_getter or is_data or is_value or field_getter
        want_setter = (class_setter or is_data or field_setter) and not is_final
        if not want_getter and not want_setter:
            continue

        type_node = get_child_by_field(fd, "type")
        type_text = get_node_text(type_node, ctx.source).strip() if type_node else "Object"
        is_boolean = type_node and type_node.type == "boolean_type"
        return_type = normalize_java_type(type_node, ctx.source)

        for i in range(fd.named_child_count):
            vd = fd.named_child(i)
            if not vd or vd.type != "variable_declarator":
                continue
            name_node = get_child_by_field(vd, "name")
            if not name_node:
                continue
            field_name = get_node_text(name_node, ctx.source).strip()
            if not field_name:
                continue

            if want_getter:
                g = _lombok_getter_name(field_name, is_boolean)
                from_ann = "@Getter" if field_getter else ("@Data" if is_data else ("@Value" if is_value else "@Getter"))
                emit_method(g, name_node, f"{type_text} {g}()", from_ann, return_type=return_type)
            if want_setter:
                s = _lombok_setter_name(field_name, is_boolean)
                from_ann = "@Setter" if field_setter else "@Data"
                emit_method(s, name_node, f"void {s}({type_text} {field_name})", from_ann)

    # Class-level synthesized methods
    if has_builder:
        ann = "@SuperBuilder" if "SuperBuilder" in class_anns else "@Builder"
        emit_method("builder", class_name_node, f"static {class_name}.{class_name}Builder builder()",
                    ann, return_type=f"{class_name}Builder", is_static=True)
    if has_to_string:
        ann = "@Data" if is_data else ("@Value" if is_value else "@ToString")
        emit_method("toString", class_name_node, "String toString()", ann)
    if has_equals:
        ann = "@Data" if is_data else ("@Value" if is_value else "@EqualsAndHashCode")
        emit_method("equals", class_name_node, "boolean equals(Object o)", ann)
        emit_method("hashCode", class_name_node, "int hashCode()", ann)

    # Logger field
    if log_ann and "log" not in taken_fields:
        taken_fields.add("log")
        ctx.create_node("field", "log", class_name_node, {
            "visibility": "private",
            "is_static": True,
            "signature": "Logger log",
            "docstring": f"Lombok-generated (@{log_ann})",
            "decorators": ["lombok"],
        })


class JavaExtractor:
    """
    Java language extractor config (translated from java.ts:253-334).

    Provides AST node type mappings and Java-specific hooks for the core
    TreeSitterExtractor.
    """

    function_types: list[str] = []
    class_types: list[str] = ["class_declaration"]
    method_types: list[str] = ["method_declaration", "constructor_declaration"]
    interface_types: list[str] = ["interface_declaration", "annotation_type_declaration"]
    struct_types: list[str] = []
    enum_types: list[str] = ["enum_declaration"]
    enum_member_types: list[str] = ["enum_constant"]
    type_alias_types: list[str] = []
    import_types: list[str] = ["import_declaration"]
    call_types: list[str] = ["method_invocation"]
    variable_types: list[str] = ["local_variable_declaration"]
    field_types: list[str] = ["field_declaration"]

    name_field: str = "name"
    body_field: str = "body"
    params_field: str = "parameters"
    return_field: str = "type"

    package_types: list[str] = ["package_declaration"]

    def get_return_type(self, node: "SyntaxNode", source: str) -> Optional[str]:
        return extract_java_return_type(node, source)

    def synthesize_members(self, class_node: "SyntaxNode", ctx) -> None:
        synthesize_lombok_members(class_node, ctx)

    def get_signature(self, node: "SyntaxNode", source: str) -> Optional[str]:
        params = get_child_by_field(node, "parameters")
        return_type = get_child_by_field(node, "type")
        if not params:
            return None
        params_text = get_node_text(params, source)
        return get_node_text(return_type, source) + " " + params_text if return_type else params_text

    def get_visibility(self, node: "SyntaxNode") -> Optional[str]:
        for i in range(node.child_count):
            child = node.child(i)
            if child and child.type == "modifiers":
                text = child.text.decode("utf-8")
                if "public" in text:
                    return "public"
                if "private" in text:
                    return "private"
                if "protected" in text:
                    return "protected"
        return None

    def is_static(self, node: "SyntaxNode") -> bool:
        for i in range(node.child_count):
            child = node.child(i)
            if child and child.type == "modifiers" and b"static" in child.text:
                return True
        return False

    def is_const(self, node: "SyntaxNode") -> bool:
        for i in range(node.child_count):
            child = node.child(i)
            if child and child.type == "modifiers":
                text = child.text.decode("utf-8")
                return bool(re.search(r"\bstatic\b", text)) and bool(re.search(r"\bfinal\b", text))
        return False

    def extract_import(self, node: "SyntaxNode", source: str):
        import_text = get_node_text(node, source).strip()
        scoped_id = None
        for i in range(node.named_child_count):
            child = node.named_child(i)
            if child and child.type == "scoped_identifier":
                scoped_id = child
                break
        if scoped_id:
            module_name = get_node_text(scoped_id, source)
            return {"module_name": module_name, "signature": import_text}
        return None

    def extract_package(self, node: "SyntaxNode", source: str) -> Optional[str]:
        for i in range(node.named_child_count):
            child = node.named_child(i)
            if child and child.type in ("scoped_identifier", "identifier"):
                return get_node_text(child, source).strip()
        return None
