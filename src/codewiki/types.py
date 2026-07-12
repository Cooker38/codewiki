"""
CodeWiki Type Definitions — Python translation of codegraph/src/types.ts.

All field names use snake_case (matching the SQLite schema), not camelCase.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, Optional

# =============================================================================
# Node / Edge kinds (from types.ts:18-60)
# =============================================================================

NODE_KINDS = [
    'file', 'module', 'class', 'struct', 'interface', 'trait', 'protocol',
    'function', 'method', 'property', 'field', 'variable', 'constant',
    'enum', 'enum_member', 'type_alias', 'namespace', 'parameter',
    'import', 'export', 'route', 'component',
]

NodeKind = Literal[
    'file', 'module', 'class', 'struct', 'interface', 'trait', 'protocol',
    'function', 'method', 'property', 'field', 'variable', 'constant',
    'enum', 'enum_member', 'type_alias', 'namespace', 'parameter',
    'import', 'export', 'route', 'component',
]

EdgeKind = Literal[
    'contains', 'calls', 'imports', 'exports', 'extends', 'implements',
    'references', 'type_of', 'returns', 'instantiates', 'overrides', 'decorates',
]

# ReferenceKind includes all EdgeKinds plus the internal-only 'function_ref'
ReferenceKind = Literal[
    'contains', 'calls', 'imports', 'exports', 'extends', 'implements',
    'references', 'type_of', 'returns', 'instantiates', 'overrides', 'decorates',
    'function_ref',
]

Language = Literal[
    'typescript', 'javascript', 'tsx', 'jsx', 'arkts', 'python', 'go',
    'rust', 'java', 'c', 'cpp', 'csharp', 'razor', 'php', 'ruby', 'swift',
    'kotlin', 'dart', 'svelte', 'vue', 'astro', 'liquid', 'pascal', 'scala',
    'lua', 'luau', 'objc', 'r', 'solidity', 'nix', 'yaml', 'twig', 'xml',
    'properties', 'cfml', 'cfscript', 'cfquery', 'cobol', 'vbnet', 'erlang',
    'terraform', 'unknown',
]


# =============================================================================
# Core Graph Types (types.ts:120-326)
# =============================================================================

@dataclass
class Node:
    """A node in the knowledge graph representing a code symbol (types.ts:120-189)."""
    id: str
    kind: NodeKind
    name: str
    qualified_name: str
    file_path: str
    language: Language
    start_line: int
    end_line: int
    start_column: int
    end_column: int
    updated_at: int
    docstring: Optional[str] = None
    signature: Optional[str] = None
    visibility: Optional[str] = None  # 'public' | 'private' | 'protected' | 'internal'
    is_exported: bool = False
    is_async: bool = False
    is_static: bool = False
    is_abstract: bool = False
    decorators: Optional[list[str]] = None
    type_parameters: Optional[list[str]] = None
    return_type: Optional[str] = None


@dataclass
class Edge:
    """An edge representing a relationship between nodes (types.ts:194-215)."""
    source: str
    target: str
    kind: EdgeKind
    metadata: Optional[dict[str, Any]] = None
    line: Optional[int] = None
    column: Optional[int] = None
    provenance: Optional[Literal['tree-sitter', 'scip', 'heuristic']] = None


@dataclass
class FileRecord:
    """Metadata about a tracked file (types.ts:220-244)."""
    path: str
    content_hash: str
    language: Language
    size: int
    modified_at: int
    indexed_at: int
    node_count: int = 0
    errors: Optional[list[dict[str, Any]]] = None


@dataclass
class UnresolvedReference:
    """A reference that couldn't be resolved during extraction (types.ts:304-326)."""
    from_node_id: str
    reference_name: str
    reference_kind: ReferenceKind
    line: int
    column: int
    file_path: str = ''
    language: Language = 'unknown'
    candidates: Optional[list[str]] = None
    # DB auto-increment id, only set when loaded from DB (not in types.ts, added for Python ergonomics)
    id: Optional[int] = None
    status: str = 'pending'
    name_tail: str = ''


# =============================================================================
# Extraction Types (types.ts:253-291)
# =============================================================================

@dataclass
class ExtractionError:
    """Error during code extraction (types.ts:272-291)."""
    message: str
    file_path: Optional[str] = None
    line: Optional[int] = None
    column: Optional[int] = None
    severity: Literal['error', 'warning'] = 'error'
    code: Optional[str] = None


@dataclass
class ExtractionResult:
    """Result from parsing a source file (types.ts:253-268)."""
    nodes: list[Node] = field(default_factory=list)
    edges: list[Edge] = field(default_factory=list)
    unresolved_references: list[UnresolvedReference] = field(default_factory=list)
    errors: list[ExtractionError] = field(default_factory=list)
    duration_ms: int = 0


# =============================================================================
# Query Types (types.ts:358-421)
# =============================================================================

@dataclass
class TraversalOptions:
    """Options for graph traversal (types.ts:358-376)."""
    max_depth: Optional[int] = None
    edge_kinds: Optional[list[EdgeKind]] = None
    node_kinds: Optional[list[NodeKind]] = None
    direction: Literal['outgoing', 'incoming', 'both'] = 'outgoing'
    limit: Optional[int] = None
    include_start: bool = False


@dataclass
class SearchOptions:
    """Options for searching the graph (types.ts:381-402)."""
    kinds: Optional[list[NodeKind]] = None
    languages: Optional[list[Language]] = None
    include_patterns: Optional[list[str]] = None
    exclude_patterns: Optional[list[str]] = None
    limit: int = 50
    offset: int = 0
    case_sensitive: bool = False


@dataclass
class SearchResult:
    """A search result with relevance scoring (types.ts:407-421)."""
    node: Node
    score: float
    highlights: Optional[list[str]] = None
