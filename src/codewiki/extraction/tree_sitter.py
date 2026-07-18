"""
TreeSitterExtractor — Python translation of codegraph/src/extraction/tree-sitter.ts.

Core extraction engine: parses source with tree-sitter, walks the AST, and produces
ExtractionResult (nodes + edges + unresolved_references).

Currently supports Java. Multi-language support follows the same pattern: each language
provides a LanguageExtractor config (node type mappings + hooks).
"""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any, Optional

from tree_sitter import Language, Parser
import tree_sitter_java

from codewiki.types import (
    Edge, ExtractionError, ExtractionResult, Node, NodeKind, UnresolvedReference,
)
from codewiki.extraction.tree_sitter_helpers import (
    generate_node_id, get_child_by_field, get_node_text, get_preceding_docstring,
)
from codewiki.extraction.languages.java import JavaExtractor

if TYPE_CHECKING:
    from tree_sitter import Node as SyntaxNode

# Language → grammar mapping. P1 only supports Java.
_LANGUAGES: dict[str, Language] = {
    "java": Language(tree_sitter_java.language()),
}

# Language → extractor config
_EXTRACTORS: dict[str, Any] = {
    "java": JavaExtractor(),
}

# Node types that represent class-like declarations (for isInsideClassLikeNode check)
_CLASS_LIKE_KINDS = frozenset(["class", "struct", "interface", "trait", "protocol"])

# Receivers that don't aid resolution (skip in call extraction)
_SKIP_RECEIVERS = frozenset(["self", "this", "cls", "super", "parent", "static"])

# Instantiation node types (new Foo())
_INSTANTIATION_TYPES = frozenset(["object_creation_expression", "class_literal"])

# Node types to skip when walking for calls inside function bodies.
# Descending into string/comment/annotation nodes can trigger false
# method_invocation matches when tree-sitter-java misparses Java 21
# text blocks or annotation arguments (e.g. @Bean method bodies).
# Aligned with codegraph's visitForCallsAndStructure which implicitly
# avoids these through dispatch-only recursion (tree-sitter.ts:4997-5147).
_SKIP_WALK_TYPES = frozenset([
    "string_literal", "text_block", "string_interpolation",
    "line_comment", "block_comment", "comment",
    "marker_annotation", "annotation",
])

# Valid Java identifier pattern (for call extraction garbage defense).
# Reject method_invocation names that contain newlines, annotations,
# or other non-identifier characters.  Tree-sitter-java can produce
# malformed method_invocation nodes for Java 21 text blocks / annotation
# heavy Spring methods; this is the last-resort defense at the
# unresolved-reference creation point.
import re as _re
_VALID_IDENTIFIER = _re.compile(r'^[A-Za-z_$][\w$]*$')

def _is_valid_identifier(s: str) -> bool:
    """True if s is a valid Java identifier (no newlines, @, parens, etc.)."""
    return bool(_VALID_IDENTIFIER.match(s.strip()))


# =========================================================================
# Spring MVC route extraction (mirrors CodeGraph's @GetMapping/@PostMapping
# -> `route` node creation, so impact analysis can surface HTTP endpoints).
# =========================================================================
_ROUTE_ANNOT_RE = _re.compile(r'@(\w*Mapping)\b')
_ROUTE_PATH_RE = _re.compile(r'(?:value|path)\s*=\s*"([^"]+)"')
_ROUTE_SINGLE_ARG_RE = _re.compile(r'\(\s*"([^"]+)"\s*\)')
_ROUTE_METHOD_RE = _re.compile(r'method\s*=\s*(?:RequestMethod\.)?(\w+)')
_ROUTE_METHOD_DEFAULTS = {
    "GetMapping": "GET",
    "PostMapping": "POST",
    "PutMapping": "PUT",
    "DeleteMapping": "DELETE",
    "PatchMapping": "PATCH",
    "RequestMapping": None,  # resolved from `method` attr, else "ALL"
}


def _parse_spring_route(annotation_text: str):
    """Parse a Spring MVC mapping annotation -> (http_method, path) or None."""
    m = _ROUTE_ANNOT_RE.search(annotation_text)
    if not m:
        return None
    annot_name = m.group(1)
    path = None
    pm = _ROUTE_PATH_RE.search(annotation_text)
    if pm:
        path = pm.group(1)
    else:
        sm = _ROUTE_SINGLE_ARG_RE.search(annotation_text)
        if sm:
            path = sm.group(1)
    if not path:
        return None
    if annot_name == "RequestMapping":
        mm = _ROUTE_METHOD_RE.search(annotation_text)
        http_method = mm.group(1).upper() if mm else "ALL"
    else:
        http_method = _ROUTE_METHOD_DEFAULTS.get(annot_name, "ALL")
    return http_method, path


def _join_route_path(prefix: str, path: str) -> str:
    """Join a class-level prefix with a method-level path (no double slash)."""
    if not prefix:
        return path
    if prefix.endswith("/") and path.startswith("/"):
        return prefix + path[1:]
    if not prefix.endswith("/") and not path.startswith("/"):
        return prefix + "/" + path
    return prefix + path


def _find_modifiers(node: "SyntaxNode"):
    """Return the `modifiers` child of a declaration node.

    tree-sitter-java exposes modifiers as a positional child (field name is
    None in some grammar builds), so get_child_by_field('modifiers') returns
    None. Fall back to scanning children by type.
    """
    m = get_child_by_field(node, "modifiers")
    if m:
        return m
    for i in range(node.child_count):
        c = node.child(i)
        if c and c.type == "modifiers":
            return c
    return None


class ExtractorContext:
    """
    Context object passed to language hooks (tree-sitter-types.ts:50-71).
    Provides controlled API surface for hooks to create nodes, visit children,
    and add references.
    """

    def __init__(self, extractor: "TreeSitterExtractor"):
        self._extractor = extractor

    def create_node(self, kind: str, name: str, node: "SyntaxNode",
                    extra: Optional[dict] = None) -> Optional[Node]:
        return self._extractor.create_node(kind, name, node, extra)

    def visit_node(self, node: "SyntaxNode") -> None:
        self._extractor.visit_node(node)

    def visit_function_body(self, body: "SyntaxNode", function_id: str) -> None:
        self._extractor.visit_function_body(body, function_id)

    def add_unresolved_reference(self, ref: UnresolvedReference) -> None:
        self._extractor.unresolved_references.append(ref)

    def push_scope(self, node_id: str) -> None:
        self._extractor.node_stack.append(node_id)

    def pop_scope(self) -> None:
        self._extractor.node_stack.pop()

    @property
    def file_path(self) -> str:
        return self._extractor.file_path

    @property
    def source(self) -> str:
        return self._extractor.source

    @property
    def node_stack(self) -> list[str]:
        return self._extractor.node_stack

    @property
    def nodes(self) -> list[Node]:
        return self._extractor.nodes


class TreeSitterExtractor:
    """
    Core tree-sitter extraction engine (translated from tree-sitter.ts:365+).

    Parses a source file, walks the AST, and produces an ExtractionResult
    with nodes, edges (contains), and unresolved_references.
    """

    def __init__(self, file_path: str, source: str, language: str = "java"):
        self.file_path = file_path
        self.source = source
        self.language = language
        self.extractor = _EXTRACTORS.get(language)
        self.nodes: list[Node] = []
        self.edges: list[Edge] = []
        self.unresolved_references: list[UnresolvedReference] = []
        self.errors: list[ExtractionError] = []
        self.node_stack: list[str] = []
        self._class_route_prefix: str = ""
        self._tree = None

    def extract(self) -> ExtractionResult:
        """Parse and extract from source (tree-sitter.ts:419+)."""
        start_time = time.time()

        if self.language not in _LANGUAGES:
            return ExtractionResult(errors=[ExtractionError(
                message=f"Unsupported language: {self.language}",
                file_path=self.file_path, severity="error", code="unsupported_language",
            )], duration_ms=int((time.time() - start_time) * 1000))

        parser = Parser(_LANGUAGES[self.language])
        source_bytes = self.source.encode("utf-8")

        try:
            self._tree = parser.parse(source_bytes)
            if not self._tree:
                raise ValueError("Parser returned null tree")

            # Create file node
            line_count = self.source.count("\n") + 1
            file_node = Node(
                id=f"file:{self.file_path}",
                kind="file",
                name=os.path.basename(self.file_path),
                qualified_name=self.file_path,
                file_path=self.file_path,
                language=self.language,
                start_line=1, end_line=line_count,
                start_column=0, end_column=0,
                updated_at=int(time.time() * 1000),
            )
            self.nodes.append(file_node)
            self.node_stack.append(file_node.id)

            # Extract package declaration (Java/Kotlin)
            pkg_id = self._extract_file_package(self._tree.root_node)
            if pkg_id:
                self.node_stack.append(pkg_id)

            # Walk AST
            self.visit_node(self._tree.root_node)

            if pkg_id:
                self.node_stack.pop()
            self.node_stack.pop()

        except Exception as e:
            self.errors.append(ExtractionError(
                message=f"Parse error: {e}",
                file_path=self.file_path, severity="error", code="parse_error",
            ))

        return ExtractionResult(
            nodes=self.nodes,
            edges=self.edges,
            unresolved_references=self.unresolved_references,
            errors=self.errors,
            duration_ms=int((time.time() - start_time) * 1000),
        )

    # =========================================================================
    # Node creation (tree-sitter.ts:1272-1341)
    # =========================================================================

    def create_node(self, kind: str, name: str, node: "SyntaxNode",
                    extra: Optional[dict] = None) -> Optional[Node]:
        """Create a node, build qualified name, add contains edge to parent."""
        if not name:
            return None

        line = node.start_point[0] + 1
        node_id = generate_node_id(self.file_path, kind, name, line)

        end_line = node.end_point[0] + 1
        # Extend end_line to body if it's beyond the declaration
        if kind in ("function", "method") and self.extractor:
            body = get_child_by_field(node, self.extractor.body_field)
            if body and body.end_point[0] + 1 > end_line:
                end_line = body.end_point[0] + 1

        new_node = Node(
            id=node_id,
            kind=kind,
            name=name,
            qualified_name=self._build_qualified_name(name),
            file_path=self.file_path,
            language=self.language,
            start_line=line,
            end_line=end_line,
            start_column=node.start_point[1],
            end_column=node.end_point[1],
            updated_at=int(time.time() * 1000),
        )

        # Apply extra fields
        if extra:
            for k, v in extra.items():
                if v is not None:
                    setattr(new_node, k, v)

        # Docstring from preceding comment
        docstring = get_preceding_docstring(node, self.source)
        if docstring and not new_node.docstring:
            new_node.docstring = docstring

        # Signature from extractor hook
        if self.extractor and not new_node.signature:
            sig = self.extractor.get_signature(node, self.source)
            if sig:
                new_node.signature = sig

        # Visibility
        if self.extractor and not new_node.visibility:
            vis = self.extractor.get_visibility(node)
            if vis:
                new_node.visibility = vis

        # Static
        if self.extractor:
            new_node.is_static = self.extractor.is_static(node)

        self.nodes.append(new_node)

        # Contains edge from parent
        if self.node_stack:
            parent_id = self.node_stack[-1]
            self.edges.append(Edge(source=parent_id, target=node_id, kind="contains"))

        return new_node

    def _build_qualified_name(self, name: str) -> str:
        """Build qualified name from node stack (tree-sitter.ts:1385-1398)."""
        parts: list[str] = []
        for node_id in self.node_stack:
            node = next((n for n in self.nodes if n.id == node_id), None)
            if node and node.kind != "file":
                parts.append(node.name)
        parts.append(name)
        return "::".join(parts)

    def _extract_file_package(self, root_node: "SyntaxNode") -> Optional[str]:
        """Extract package declaration → namespace node (tree-sitter.ts:1361-1380)."""
        if not self.extractor or not hasattr(self.extractor, "package_types"):
            return None
        pkg_types = self.extractor.package_types
        if not pkg_types:
            return None

        pkg_node = None
        for i in range(root_node.named_child_count):
            child = root_node.named_child(i)
            if child and child.type in pkg_types:
                pkg_node = child
                break
        if not pkg_node:
            return None

        pkg_name = self.extractor.extract_package(pkg_node, self.source)
        if not pkg_name:
            return None

        ns = self.create_node("namespace", pkg_name, pkg_node)
        return ns.id if ns else None

    # =========================================================================
    # AST visitor dispatch (tree-sitter.ts:900+)
    # =========================================================================

    def visit_node(self, node: "SyntaxNode") -> None:
        """Dispatch based on node type (tree-sitter.ts:900+)."""
        if not self.extractor:
            return

        node_type = node.type

        # Package declaration already handled in extract()
        if node_type in getattr(self.extractor, "package_types", []):
            return

        # Class declarations
        if node_type in self.extractor.class_types:
            self._extract_class(node)
            return

        # Method declarations
        if node_type in self.extractor.method_types:
            self._extract_method(node)
            return

        # Interface declarations
        if node_type in self.extractor.interface_types:
            self._extract_interface(node)
            return

        # Enum declarations
        if node_type in self.extractor.enum_types:
            self._extract_enum(node)
            return

        # Import declarations
        if node_type in self.extractor.import_types:
            self._extract_import(node)
            return

        # Field declarations (inside class body)
        if node_type in getattr(self.extractor, "field_types", []):
            self._extract_field(node)
            return

        # Variable declarations (inside method body)
        if node_type in self.extractor.variable_types:
            self._extract_variable(node)
            return

        # Recurse into children for unhandled nodes
        for i in range(node.named_child_count):
            child = node.named_child(i)
            if child:
                self.visit_node(child)

    # =========================================================================
    # Declaration extractors
    # =========================================================================

    def _extract_class(self, node: "SyntaxNode") -> None:
        """Extract a class declaration (tree-sitter.ts:1630+)."""
        name_node = get_child_by_field(node, "name")
        if not name_node:
            return
        name = get_node_text(name_node, self.source).strip()
        if not name:
            return

        # Capture class-level @RequestMapping prefix for route composition
        # (e.g. @RequestMapping("/api") on a controller + @GetMapping("/x") on a
        # method -> full path "/api/x"). Mirrors CodeGraph's route handling.
        self._class_route_prefix = ""
        modifiers = _find_modifiers(node)
        if modifiers:
            for i in range(modifiers.named_child_count):
                ann = modifiers.named_child(i)
                if ann and ann.type in ("annotation", "marker_annotation"):
                    ann_text = get_node_text(ann, self.source)
                    if "@RequestMapping" in ann_text:
                        parsed = _parse_spring_route(ann_text)
                        if parsed:
                            self._class_route_prefix = parsed[1]

        # Check extends/implements for unresolved references
        extends_node = get_child_by_field(node, "superclass")
        if extends_node:
            super_name = get_node_text(extends_node, self.source).strip()
            if super_name:
                class_id = None  # will be set after createNode
                # Push ref after node creation
                self._pending_extends = super_name

        implements_node = get_child_by_field(node, "interfaces")
        if implements_node:
            for i in range(implements_node.named_child_count):
                child = implements_node.named_child(i)
                if child and child.type == "type_list":
                    for j in range(child.named_child_count):
                        iface = child.named_child(j)
                        if iface:
                            iface_name = get_node_text(iface, self.source).strip()
                            if iface_name:
                                setattr(self, "_pending_implements", getattr(self, "_pending_implements", []) + [iface_name])

        class_node = self.create_node("class", name, node)
        if not class_node:
            return

        # Emit extends/implements unresolved refs
        if hasattr(self, "_pending_extends") and self._pending_extends:
            self.unresolved_references.append(UnresolvedReference(
                from_node_id=class_node.id,
                reference_name=self._pending_extends,
                reference_kind="extends",
                line=node.start_point[0] + 1, column=node.start_point[1],
                file_path=self.file_path, language=self.language,
            ))
            del self._pending_extends

        for iface_name in getattr(self, "_pending_implements", []):
            self.unresolved_references.append(UnresolvedReference(
                from_node_id=class_node.id,
                reference_name=iface_name,
                reference_kind="implements",
                line=node.start_point[0] + 1, column=node.start_point[1],
                file_path=self.file_path, language=self.language,
            ))
        if hasattr(self, "_pending_implements"):
            del self._pending_implements

        # Push scope and visit body
        self.node_stack.append(class_node.id)
        body = get_child_by_field(node, self.extractor.body_field)
        if body:
            for i in range(body.named_child_count):
                child = body.named_child(i)
                if child:
                    self.visit_node(child)

        # Lombok synthesis
        if self.extractor and hasattr(self.extractor, "synthesize_members"):
            ctx = ExtractorContext(self)
            self.extractor.synthesize_members(node, ctx)

        self.node_stack.pop()

    def _extract_method(self, node: "SyntaxNode") -> None:
        """Extract a method/constructor declaration (tree-sitter.ts:1730+)."""
        name_node = get_child_by_field(node, "name")
        if not name_node:
            return
        name = get_node_text(name_node, self.source).strip()
        if not name:
            return

        # Return type
        return_type = None
        if self.extractor and hasattr(self.extractor, "get_return_type"):
            return_type = self.extractor.get_return_type(node, self.source)

        method_node = self.create_node("method", name, node, {
            "return_type": return_type,
            "is_abstract": node.type == "method_declaration" and
                           _has_modifier(node, "abstract"),
        })
        if not method_node:
            return

        # Spring MVC route extraction (mirrors CodeGraph's @GetMapping/@PostMapping
        # -> `route` node creation, so impact surfaces the HTTP endpoint).
        # A `route` node is linked to the method via a `decorates` edge; impact()
        # follows it (it excludes only `contains`), so changing the method shows
        # up as affecting the endpoint — exactly like CodeGraph's blast radius.
        modifiers = _find_modifiers(node)
        if modifiers:
            for i in range(modifiers.named_child_count):
                ann = modifiers.named_child(i)
                if ann and ann.type in ("annotation", "marker_annotation"):
                    ann_text = get_node_text(ann, self.source)
                    parsed = _parse_spring_route(ann_text)
                    if parsed:
                        http_method, path = parsed
                        full_path = _join_route_path(self._class_route_prefix, path)
                        route_name = f"{http_method} {full_path}"
                        route_line = ann.start_point[0] + 1
                        route_id = generate_node_id(self.file_path, "route", route_name, route_line)
                        route_node = Node(
                            id=route_id,
                            kind="route",
                            name=route_name,
                            qualified_name=self._build_qualified_name(route_name),
                            file_path=self.file_path,
                            language=self.language,
                            start_line=route_line,
                            end_line=route_line,
                            start_column=ann.start_point[1],
                            end_column=ann.end_point[1],
                            updated_at=int(time.time() * 1000),
                        )
                        self.nodes.append(route_node)
                        self.edges.append(Edge(
                            source=route_id,
                            target=method_node.id,
                            kind="decorates",
                            provenance="heuristic",
                        ))

        # Extract parameters
        params = get_child_by_field(node, self.extractor.params_field)
        if params:
            self.node_stack.append(method_node.id)
            for i in range(params.named_child_count):
                param = params.named_child(i)
                if param and param.type == "formal_parameter":
                    self._extract_parameter(param)
            self.node_stack.pop()

        # Visit body for calls
        body = get_child_by_field(node, self.extractor.body_field)
        if body:
            self.node_stack.append(method_node.id)
            self.visit_function_body(body, method_node.id)
            self.node_stack.pop()

    def _extract_parameter(self, node: "SyntaxNode") -> None:
        """Extract a method parameter."""
        name_node = get_child_by_field(node, "name")
        if not name_node:
            return
        name = get_node_text(name_node, self.source).strip()
        if not name:
            return
        self.create_node("parameter", name, node)

    def _extract_interface(self, node: "SyntaxNode") -> None:
        """Extract an interface declaration (tree-sitter.ts:1780+)."""
        name_node = get_child_by_field(node, "name")
        if not name_node:
            return
        name = get_node_text(name_node, self.source).strip()
        if not name:
            return

        iface_node = self.create_node("interface", name, node)
        if not iface_node:
            return

        self.node_stack.append(iface_node.id)
        body = get_child_by_field(node, self.extractor.body_field)
        if body:
            for i in range(body.named_child_count):
                child = body.named_child(i)
                if child:
                    self.visit_node(child)
        self.node_stack.pop()

    def _extract_enum(self, node: "SyntaxNode") -> None:
        """Extract an enum declaration (tree-sitter.ts:1860+)."""
        name_node = get_child_by_field(node, "name")
        if not name_node:
            return
        name = get_node_text(name_node, self.source).strip()
        if not name:
            return

        enum_node = self.create_node("enum", name, node)
        if not enum_node:
            return

        self.node_stack.append(enum_node.id)
        body = get_child_by_field(node, self.extractor.body_field)
        if body:
            for i in range(body.named_child_count):
                child = body.named_child(i)
                if child:
                    if child.type in getattr(self.extractor, "enum_member_types", []):
                        member_name_node = get_child_by_field(child, "name")
                        if member_name_node:
                            member_name = get_node_text(member_name_node, self.source).strip()
                            if member_name:
                                self.create_node("enum_member", member_name, child)
                        else:
                            self.create_node("enum_member", get_node_text(child, self.source), child)
                    else:
                        self.visit_node(child)
        self.node_stack.pop()

    def _extract_field(self, node: "SyntaxNode") -> None:
        """Extract a field declaration (tree-sitter.ts:2030+)."""
        # Determine if constant (static final) or field
        is_const = False
        if self.extractor:
            is_const = self.extractor.is_const(node)

        # Get type node for return_type
        type_node = get_child_by_field(node, "type")

        # Extract each variable declarator
        for i in range(node.named_child_count):
            child = node.named_child(i)
            if not child or child.type != "variable_declarator":
                continue
            name_node = get_child_by_field(child, "name")
            if not name_node:
                continue
            name = get_node_text(name_node, self.source).strip()
            if not name:
                continue

            extra = {}
            if type_node:
                from codewiki.extraction.languages.java import normalize_java_type
                rt = normalize_java_type(type_node, self.source)
                if rt:
                    extra["return_type"] = rt

            kind = "constant" if is_const else "field"
            self.create_node(kind, name, child, extra)

    def _extract_variable(self, node: "SyntaxNode") -> None:
        """Extract a local variable declaration (inside method body).

        Stores the normalized type name in return_type so that name_matcher
        can infer receiver types for method calls (e.g. service.executeTool() →
        look up variable "service" → type "ScriptExecutionService").
        """
        # Extract the declared type from the variable declaration
        type_node = get_child_by_field(node, "type")
        var_type: Optional[str] = None
        if type_node:
            from codewiki.extraction.languages.java import normalize_java_type
            var_type = normalize_java_type(type_node, self.source)
        extra = {"return_type": var_type} if var_type else {}

        for i in range(node.named_child_count):
            child = node.named_child(i)
            if not child or child.type != "variable_declarator":
                continue
            name_node = get_child_by_field(child, "name")
            if not name_node:
                continue
            name = get_node_text(name_node, self.source).strip()
            if not name:
                continue
            self.create_node("variable", name, child, extra)

    def _extract_import(self, node: "SyntaxNode") -> None:
        """Extract an import declaration (tree-sitter.ts:3110+)."""
        if not self.extractor or not hasattr(self.extractor, "extract_import"):
            return
        info = self.extractor.extract_import(node, self.source)
        if not info:
            return

        import_node = self.create_node("import", info["module_name"], node, {
            "signature": info["signature"],
        })
        if not import_node:
            return

        # Import reference (for cross-file resolution)
        self.unresolved_references.append(UnresolvedReference(
            from_node_id=import_node.id,
            reference_name=info["module_name"],
            reference_kind="imports",
            line=node.start_point[0] + 1, column=node.start_point[1],
            file_path=self.file_path, language=self.language,
        ))

    # =========================================================================
    # Function body walker (tree-sitter.ts:4994+)
    # =========================================================================

    def visit_function_body(self, body: "SyntaxNode", _function_id: str) -> None:
        """Walk a function body to extract calls and instantiations (tree-sitter.ts:4994+)."""
        self._walk_for_calls(body)

    def _walk_for_calls(self, node: "SyntaxNode") -> None:
        """Recursively walk a subtree looking for call/instantiation nodes."""
        node_type = node.type

        if self.extractor and node_type in self.extractor.call_types:
            self._extract_call(node)
        elif node_type in _INSTANTIATION_TYPES:
            self._extract_instantiation(node)
        elif node_type == "field_access":
            self._extract_field_access(node)

        # Also handle local variable declarations inside body
        if self.extractor and node_type in self.extractor.variable_types:
            self._extract_variable(node)

        # Recurse — skip string/comment/annotation nodes (codegraph-aligned defense)
        for i in range(node.named_child_count):
            child = node.named_child(i)
            if child and child.type not in _SKIP_WALK_TYPES:
                self._walk_for_calls(child)

    def _extract_call(self, node: "SyntaxNode") -> None:
        """
        Extract a method invocation → unresolved reference (tree-sitter.ts:4070+).

        Java method_invocation has 'object' (receiver) + 'name' (method) fields.
        Handles: bare call (foo()), receiver.method (a.b()), this.field.method (this.x.foo()).
        """
        if not self.node_stack:
            return
        caller_id = self.node_stack[-1]
        if not caller_id:
            return

        name_field = get_child_by_field(node, "name")
        object_field = get_child_by_field(node, "object")

        if name_field and object_field:
            method_name = get_node_text(name_field, self.source)

            # Defend against garbage method_invocation nodes produced by
            # tree-sitter-java for Java 21 text blocks / annotation-heavy methods.
            if not _is_valid_identifier(method_name):
                return

            # Handle fluent chain: Foo.getInstance().bar() (tree-sitter.ts:4120-4138)
            if object_field.type == "method_invocation":
                inner_obj = get_child_by_field(object_field, "object")
                inner_name = get_child_by_field(object_field, "name")
                if inner_obj and inner_name:
                    inner_name_text = get_node_text(inner_name, self.source)
                    if not _is_valid_identifier(inner_name_text):
                        return
                    callee = f"{get_node_text(inner_obj, self.source)}.{inner_name_text}().{method_name}"
                    self.unresolved_references.append(UnresolvedReference(
                        from_node_id=caller_id, reference_name=callee,
                        reference_kind="calls",
                        line=node.start_point[0] + 1, column=node.start_point[1],
                        file_path=self.file_path, language=self.language,
                    ))
                    return

            # Handle this.field.method() (tree-sitter.ts:4141-4151)
            if object_field.type == "field_access":
                inner = get_child_by_field(object_field, "object")
                fld = get_child_by_field(object_field, "field")
                if inner and fld and inner.type in ("this", "this_expression"):
                    receiver_name = get_node_text(fld, self.source)
                else:
                    receiver_name = get_node_text(object_field, self.source)
            else:
                receiver_name = get_node_text(object_field, self.source)

            if method_name:
                # Skip garbage receiver names (e.g. '@Primary\npublic...')
                if receiver_name and ("\n" in receiver_name or "@" in receiver_name):
                    return
                if receiver_name in _SKIP_RECEIVERS:
                    callee = method_name
                else:
                    callee = f"{receiver_name}.{method_name}"
                self.unresolved_references.append(UnresolvedReference(
                    from_node_id=caller_id, reference_name=callee,
                    reference_kind="calls",
                    line=node.start_point[0] + 1, column=node.start_point[1],
                    file_path=self.file_path, language=self.language,
                ))
        elif name_field:
            # Bare call: foo()
            callee = get_node_text(name_field, self.source)
            if callee and _is_valid_identifier(callee):
                self.unresolved_references.append(UnresolvedReference(
                    from_node_id=caller_id, reference_name=callee,
                    reference_kind="calls",
                    line=node.start_point[0] + 1, column=node.start_point[1],
                    file_path=self.file_path, language=self.language,
                ))

    def _extract_instantiation(self, node: "SyntaxNode") -> None:
        """Extract `new Foo()` → instantiates reference (tree-sitter.ts:4475+)."""
        if not self.node_stack:
            return
        caller_id = self.node_stack[-1]
        if not caller_id:
            return

        type_node = get_child_by_field(node, "type")
        if not type_node:
            return

        # Normalize type name (strip generics, package qualifier)
        from codewiki.extraction.languages.java import normalize_java_type
        type_name = normalize_java_type(type_node, self.source)
        if not type_name:
            # Fallback: use raw text
            type_name = get_node_text(type_node, self.source).strip()
            type_name = type_name.split(".")[-1].split("<")[0].strip()
            if not type_name or not type_name[0].isupper():
                return

        self.unresolved_references.append(UnresolvedReference(
            from_node_id=caller_id, reference_name=type_name,
            reference_kind="instantiates",
            line=node.start_point[0] + 1, column=node.start_point[1],
            file_path=self.file_path, language=self.language,
        ))

    def _extract_field_access(self, node: "SyntaxNode") -> None:
        """Extract field access (this.field / obj.field) → references。

        Skips field_access nodes that are part of a method_invocation
        (already handled by _extract_call as receiver.method calls).
        """
        if not self.node_stack:
            return
        caller_id = self.node_stack[-1]
        if not caller_id:
            return

        # Skip field accesses that are the object of a method_invocation
        parent = node.parent
        if parent and parent.type == "method_invocation":
            return

        object_field = get_child_by_field(node, "object")
        field_node = get_child_by_field(node, "field")
        if not field_node:
            return

        field_name = get_node_text(field_node, self.source).strip()
        if not field_name or not _is_valid_identifier(field_name):
            return

        if object_field:
            receiver_text = get_node_text(object_field, self.source).strip()
            # Skip self/this/super receivers
            if receiver_text in _SKIP_RECEIVERS:
                ref_name = field_name
            else:
                ref_name = f"{receiver_text}.{field_name}"
        else:
            ref_name = field_name

        self.unresolved_references.append(UnresolvedReference(
            from_node_id=caller_id, reference_name=ref_name,
            reference_kind="references",
            line=node.start_point[0] + 1, column=node.start_point[1],
            file_path=self.file_path, language=self.language,
        ))


def _has_modifier(node: "SyntaxNode", keyword: str) -> bool:
    """Check if a node's modifiers child contains the given keyword."""
    for i in range(node.child_count):
        child = node.child(i)
        if child and child.type == "modifiers":
            return keyword in child.text.decode("utf-8")
    return False


def extract_from_source(file_path: str, source: str, language: str = "java") -> ExtractionResult:
    """
    Extract from source code (tree-sitter.ts:6520+).

    Entry point: creates a TreeSitterExtractor and runs it.
    """
    extractor = TreeSitterExtractor(file_path, source, language)
    return extractor.extract()
