"""
CodeWiki MCP Server — 8 tools via FastMCP stdio transport.

Graph tools: codewiki_node / callers / callees / impact / search / explore
Build tools: codewiki_init / codewiki_sync

Startup: discover .codewiki/ from CWD, open SQLite if exists.
"""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any, Optional

from mcp.server.fastmcp import FastMCP

from codewiki.db.store import GraphStore
from codewiki.extraction.orchestrator import ExtractionOrchestrator
from codewiki.resolution.resolver import ReferenceResolver
from codewiki.resolution.callback_synthesizer import CallbackSynthesizer
from codewiki.graph.traversal import GraphTraversal

mcp = FastMCP("codewiki")

# Global state — initialized on first use
_store: Optional[GraphStore] = None
_traversal: Optional[GraphTraversal] = None
_project_root: Optional[str] = None


def _find_codewiki_dir(start: str = ".") -> Optional[str]:
    """Walk up from start to find .codewiki/ directory."""
    p = os.path.abspath(start)
    while True:
        candidate = os.path.join(p, ".codewiki")
        if os.path.isdir(candidate):
            return p  # Return project root
        parent = os.path.dirname(p)
        if parent == p:
            return None
        p = parent


def _ensure_store(project_root: Optional[str] = None) -> Optional["GraphStore"]:
    """Ensure the store is open, creating one if needed."""
    global _store, _traversal, _project_root

    if _store:
        return _store

    root = project_root or _find_codewiki_dir() or _find_codewiki_dir(".")
    if not root:
        return None

    _project_root = root
    codewiki_dir = os.path.join(root, ".codewiki")
    os.makedirs(codewiki_dir, exist_ok=True)
    db_path = os.path.join(codewiki_dir, "codewiki.db")

    _store = GraphStore(db_path)
    _store.init_schema()
    _traversal = GraphTraversal(_store)
    return _store


def _node_to_dict(node) -> dict[str, Any]:
    return {
        "id": node.id, "kind": node.kind, "name": node.name,
        "qualified_name": node.qualified_name, "file_path": node.file_path,
        "language": node.language, "start_line": node.start_line,
        "end_line": node.end_line,
        "signature": node.signature, "docstring": node.docstring,
        "visibility": node.visibility, "is_static": node.is_static,
        "is_abstract": node.is_abstract, "return_type": node.return_type,
    }


def _format_node(node) -> str:
    """Format a single node as markdown."""
    d = _node_to_dict(node)
    loc = f"`{d['file_path']}:{d['start_line']}`"
    sig = f"\n  Signature: `{d['signature']}`" if d.get("signature") else ""
    doc = f"\n  {d['docstring']}" if d.get("docstring") else ""
    return f"- {loc} **{d['name']}** ({d['kind']}){sig}{doc}"


def _format_node_detail(node) -> str:
    """Format full node details as markdown."""
    d = _node_to_dict(node)
    lines = [f"## {d['name']} ({d['kind']})"]
    if d.get("docstring"):
        lines.append(f"\n{d['docstring']}")
    lines.append(f"\n- **File**: `{d['file_path']}:{d['start_line']}`")
    lines.append(f"- **Qualified name**: `{d['qualified_name']}`")
    if d.get("signature"):
        lines.append(f"- **Signature**: `{d['signature']}`")
    if d.get("visibility"):
        lines.append(f"- **Visibility**: {d['visibility']}")
    if d.get("return_type"):
        lines.append(f"- **Return type**: {d['return_type']}")
    if d.get("is_static"):
        lines.append("- **Static**: yes")
    if d.get("is_abstract"):
        lines.append("- **Abstract**: yes")
    return "\n".join(lines)


# =========================================================================
# Explore budget algorithm (aligned with codegraph getExploreOutputBudget)
# =========================================================================

def _get_explore_budget() -> dict:
    """Return explore output budget based on indexed file count."""
    file_count = len(_store.get_all_files()) if _store else 0
    if file_count < 500:
        return {"max_chars": 8000, "max_nodes": 20}
    elif file_count < 5000:
        return {"max_chars": 15000, "max_nodes": 40}
    elif file_count < 15000:
        return {"max_chars": 20000, "max_nodes": 60}
    else:
        return {"max_chars": 25000, "max_nodes": 80}


# =========================================================================
# Graph query tools
# =========================================================================

@mcp.tool()
def codewiki_node(symbol: str) -> str:
    """Get details of a code symbol by name or ID.

    Args:
        symbol: Symbol name (e.g., "DiscountService") or node ID.

    Returns:
        Node details as formatted text.
    """
    global _traversal
    if not _traversal:
        return "No project indexed. Use codewiki_init first."
    node = _traversal.get_node(symbol)
    if not node:
        return f"Symbol '{symbol}' not found."
    return _format_node_detail(node)


@mcp.tool()
def codewiki_callers(symbol: str, max_depth: int = 1) -> str:
    """Find all callers of a symbol (who calls/uses/instantiates it).

    Includes instantiates edges (constructing a class is calling its constructor).

    Args:
        symbol: Symbol name or ID.
        max_depth: Traversal depth (default 1 = direct callers only).

    Returns:
        List of caller nodes with file:line locations.
    """
    global _traversal
    if not _traversal:
        return "No project indexed. Use codewiki_init first."
    node = _traversal.get_node(symbol)
    if not node:
        return f"Symbol '{symbol}' not found."
    callers = _traversal.callers(node.id, max_depth=max_depth)
    if not callers:
        return f"No callers found for '{symbol}'."
    lines = [f"## Callers of {symbol} ({len(callers)})"]
    for c in callers:
        lines.append(f"- `{c.file_path}:{c.start_line}` **{c.name}** ({c.kind})")
    return "\n".join(lines)


@mcp.tool()
def codewiki_callees(symbol: str, max_depth: int = 1) -> str:
    """Find all callees of a symbol (what it calls/uses/instantiates).

    Includes instantiates edges (constructing a class makes it a callee).

    Args:
        symbol: Symbol name or ID.
        max_depth: Traversal depth (default 1 = direct callees only).

    Returns:
        List of callee nodes with file:line locations.
    """
    global _traversal
    if not _traversal:
        return "No project indexed. Use codewiki_init first."
    node = _traversal.get_node(symbol)
    if not node:
        return f"Symbol '{symbol}' not found."
    callees = _traversal.callees(node.id, max_depth=max_depth)
    if not callees:
        return f"No callees found for '{symbol}'."
    lines = [f"## Callees of {symbol} ({len(callees)})"]
    for c in callees:
        lines.append(f"- `{c.file_path}:{c.start_line}` **{c.name}** ({c.kind})")
    return "\n".join(lines)


@mcp.tool()
def codewiki_impact(symbol: str, max_depth: int = 3) -> str:
    """Calculate the impact radius (blast radius) of changing a symbol.

    Finds all nodes that depend on the given symbol. Excludes contains edges
    (a container doesn't depend on its members). Container nodes expand to
    include their members at the same depth.

    Args:
        symbol: Symbol name or ID.
        max_depth: Traversal depth (default 3).

    Returns:
        List of dependent nodes affected by a change.
    """
    global _traversal
    if not _traversal:
        return "No project indexed. Use codewiki_init first."
    node = _traversal.get_node(symbol)
    if not node:
        return f"Symbol '{symbol}' not found."
    result = _traversal.impact(node.id, max_depth=max_depth)
    others = [n for n in result.nodes if n.id != node.id]
    if not others:
        return f"No dependents found for '{symbol}'."
    lines = [f"## Impact of {symbol} ({len(others)} dependents)"]
    for n in others[:50]:
        lines.append(f"- `{n.file_path}:{n.start_line}` **{n.name}** ({n.kind})")
    if len(others) > 50:
        lines.append(f"... and {len(others) - 50} more")
    return "\n".join(lines)


@mcp.tool()
def codewiki_search(query: str, limit: int = 50) -> str:
    """Search for code symbols by name, docstring, or signature using FTS5.

    Supports FTS5 query syntax: "DiscountService", "Discount*", etc.

    Args:
        query: Search query string.
        limit: Maximum results (default 50).

    Returns:
        Ranked search results with relevance scores.
    """
    global _traversal
    if not _traversal:
        return "No project indexed. Use codewiki_init first."
    results = _traversal.search(query, limit=limit)
    if not results:
        return f"No results for '{query}'."
    lines = [f"## Search: '{query}' ({len(results)} results)"]
    for node, score in results:
        lines.append(f"- `{node.file_path}:{node.start_line}` **{node.name}** ({node.kind}) score={score:.2f}")
    return "\n".join(lines)


@mcp.tool()
def codewiki_explore(symbol: str) -> str:
    """Explore a symbol's full context with adaptive budget.

    Returns: node details, callers, callees, and related symbols within
    the budget limit (scaled by project size).

    Args:
        symbol: Symbol name or ID.

    Returns:
        Combined context view of the symbol and its graph neighborhood.
    """
    global _traversal, _store
    if not _traversal:
        return "No project indexed. Use codewiki_init first."
    node = _traversal.get_node(symbol)
    if not node:
        return f"Symbol '{symbol}' not found."

    budget = _get_explore_budget()
    max_nodes = budget["max_nodes"]
    lines: list[str] = []

    # 1. Node detail
    lines.append(_format_node_detail(node))

    # 2. Callers (budgeted)
    callers = _traversal.callers(node.id, max_depth=1)
    if callers:
        take = min(len(callers), max_nodes // 3)
        lines.append(f"\n## Callers ({len(callers)} total, showing {take})")
        for c in callers[:take]:
            lines.append(f"- `{c.file_path}:{c.start_line}` **{c.name}** ({c.kind})")

    # 3. Callees (budgeted)
    callees = _traversal.callees(node.id, max_depth=1)
    if callees:
        take = min(len(callees), max_nodes // 3)
        lines.append(f"\n## Callees ({len(callees)} total, showing {take})")
        for c in callees[:take]:
            lines.append(f"- `{c.file_path}:{c.start_line}` **{c.name}** ({c.kind})")

    # 4. Containing ancestor
    if _store:
        ancestors = _store.get_incoming_edges(node.id, ["contains"])
        if ancestors:
            lines.append("\n## Ancestor")
            for edge in ancestors[:1]:
                parent = _store.get_node_by_id(edge.source)
                if parent:
                    lines.append(f"Contained in: `{parent.file_path}:{parent.start_line}` **{parent.name}** ({parent.kind})")

    # 5. Budget annotation
    lines.append(f"\n---\n*Explore budget: project has ~{len(_store.get_all_files())} files, output capped at ~{budget['max_chars']} chars.*")

    return "\n".join(lines)


# =========================================================================
# Build tools
# =========================================================================

@mcp.tool()
def codewiki_init(project_root: str) -> str:
    """Build the code knowledge graph for a project (first time).

    Scans Java source files, extracts symbols and relationships with tree-sitter,
    resolves cross-file references, and stores the graph in .codewiki/.

    No wiki documents are generated — this builds the graph only.

    Args:
        project_root: Absolute path to the project root directory.

    Returns:
        Build statistics (files indexed, nodes/edges created, framework detected).
    """
    global _store, _traversal, _project_root

    root = os.path.abspath(project_root)
    if not os.path.isdir(root):
        return f"Project root not found: {root}"

    # Initialize store
    codewiki_dir = os.path.join(root, ".codewiki")
    os.makedirs(codewiki_dir, exist_ok=True)
    db_path = os.path.join(codewiki_dir, "codewiki.db")

    _store = GraphStore(db_path)
    _store.init_schema()
    _project_root = root

    # Build graph
    orch = ExtractionOrchestrator(root, _store)
    result = orch.index_all()

    if result.files_indexed == 0:
        return f"No Java files found in {root}. Check the project directory."

    # Resolve references
    resolver = ReferenceResolver(_store, frameworks=result.detected_frameworks)
    resolution_result = resolver.resolve_and_persist()

    # Synthesize
    synth = CallbackSynthesizer(_store)
    synth_counts = synth.synthesize_all()

    _traversal = GraphTraversal(_store)

    lines = [
        "## CodeWiki Graph Built",
        f"- **Files indexed**: {result.files_indexed}",
        f"- **Nodes created**: {result.nodes_created}",
        f"- **Edges created**: {result.edges_created}",
        f"- **References resolved**: {resolution_result.resolved}",
        f"- **References unresolved**: {resolution_result.unresolved}",
        f"- **Synthesized**: type_of={synth_counts.get('type_of',0)}, returns={synth_counts.get('returns',0)}, overrides={synth_counts.get('overrides',0)}",
        f"- **Frameworks detected**: {', '.join(result.detected_frameworks) if result.detected_frameworks else 'none'}",
        f"- **Duration**: {result.duration_ms}ms",
    ]
    if result.errors:
        lines.append(f"\n**Errors**: {len(result.errors)}")
        for e in result.errors[:5]:
            lines.append(f"  - {e['filePath']}: {e['message']}")

    return "\n".join(lines)


@mcp.tool()
def codewiki_sync() -> str:
    """Re-index changed files (incremental update placeholder).

    Currently performs a full re-index. Incremental git diff-driven sync
    will be implemented in a future phase.

    Returns:
        Build statistics for the sync run.
    """
    global _store, _traversal, _project_root

    root = _project_root or _find_codewiki_dir(".")
    if not root:
        return "No project found. Use codewiki_init first."

    if not _store:
        return "Store not open. Use codewiki_init first."

    orch = ExtractionOrchestrator(root, _store)
    result = orch.index_all()

    resolver = ReferenceResolver(_store, frameworks=result.detected_frameworks)
    resolution_result = resolver.resolve_and_persist()

    synth = CallbackSynthesizer(_store)
    synth.synthesize_all()

    _traversal = GraphTraversal(_store)

    lines = [
        "## CodeWiki Sync Complete",
        f"- **Files indexed**: {result.files_indexed}",
        f"- **Nodes**: {result.nodes_created}",
        f"- **Edges**: {result.edges_created}",
        f"- **Refs resolved**: {resolution_result.resolved}",
        f"- **Duration**: {result.duration_ms}ms",
    ]
    return "\n".join(lines)


# =========================================================================
# Entry point
# =========================================================================

def main():
    """Run the MCP server over stdio."""
    mcp.run()


if __name__ == "__main__":
    main()
