"""
Tree-sitter Shared Helpers — Python translation of codegraph/src/extraction/tree-sitter-helpers.ts.

Utility functions for node ID generation, text extraction, and docstring retrieval.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from tree_sitter import Node as SyntaxNode

    from codewiki.types import NodeKind


def generate_node_id(file_path: str, kind: str, name: str, line: int) -> str:
    """
    Generate a unique node ID (tree-sitter-helpers.ts:18-30).

    Uses a 32-character (128-bit) SHA-256 hash to avoid collisions when indexing
    large codebases with many files containing similar symbols.

    Format: "{kind}:{sha256(filePath:kind:name:line)[:32]}"
    """
    raw = f"{file_path}:{kind}:{name}:{line}"
    hash_hex = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]
    return f"{kind}:{hash_hex}"


def get_node_text(node: "SyntaxNode", source: str) -> str:
    """Extract text from a syntax node (tree-sitter-helpers.ts:35-37)."""
    return source[node.start_byte : node.end_byte]


def get_child_by_field(node: "SyntaxNode", field_name: str) -> Optional["SyntaxNode"]:
    """Find a child node by field name (tree-sitter-helpers.ts:42-44)."""
    return node.child_by_field_name(field_name)


# Node types that wrap a declaration so a leading comment is a sibling of the
# wrapper, not of the emitted (inner) declaration node (tree-sitter-helpers.ts:55-62).
_DOCSTRING_WRAPPER_TYPES = frozenset([
    "export_statement",
    "decorated_definition",
    "lexical_declaration",
    "variable_declaration",
    "variable_declarator",
    "ambient_declaration",
])


def _clean_comment_markers(comment: str) -> str:
    """
    Strip comment-syntax markers from a raw comment (tree-sitter-helpers.ts:77-90).
    Covers C-family, Rust/Swift/Kotlin, Python/Ruby/shell, Lua, Pascal.
    """
    c = comment.strip()
    if c.startswith("/*"):
        c = re.sub(r"^/\*+!?", "", c)
        c = re.sub(r"\*+/$", "", c)
    elif c.startswith("--["):
        c = re.sub(r"^--\[=*\[", "", c)
        c = re.sub(r"\]=*\]$", "", c)
    elif c.startswith("(*"):
        c = re.sub(r"^\(\*", "", c)
        c = re.sub(r"\*\)$", "", c)
    elif c.startswith("{"):
        c = re.sub(r"^\{", "", c)
        c = re.sub(r"\}$", "", c)

    c = re.sub(r"^//[/!]?\s?", "", c, flags=re.MULTILINE)
    c = re.sub(r"^--\s?", "", c, flags=re.MULTILINE)
    c = re.sub(r"^#\s?", "", c, flags=re.MULTILINE)
    c = re.sub(r"^%+\s?", "", c, flags=re.MULTILINE)
    c = re.sub(r"^\s*\*\s?", "", c, flags=re.MULTILINE)
    return c.strip()


def get_preceding_docstring(node: "SyntaxNode", source: str) -> Optional[str]:
    """
    Get the docstring/comment preceding a node (tree-sitter-helpers.ts:95-127).

    Climbs out of wrapper nodes (export, decorator, const-arrow) so a comment
    preceding the WHOLE construct is reachable as a sibling.
    """
    anchor = node
    while anchor.parent and anchor.parent.type in _DOCSTRING_WRAPPER_TYPES:
        anchor = anchor.parent

    sibling = anchor.prev_named_sibling
    comments: list[str] = []

    while sibling:
        if sibling.type in ("comment", "line_comment", "block_comment", "documentation_comment"):
            comments.insert(0, get_node_text(sibling, source))
            sibling = sibling.prev_named_sibling
        else:
            break

    if not comments:
        return None

    return "\n".join(_clean_comment_markers(c) for c in comments).strip()
