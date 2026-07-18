"""
CodeWiki MCP Server — single tool via FastMCP stdio transport.

MCP tools: codewiki_explore  (read-only query — the ONLY tool exposed to agents)

Building / refreshing the graph is done OUT OF BAND via the CLI
(`codewiki init <root>` / `codewiki sync`) — never inside an MCP tool handler,
so a long-running build can never block the stdio event loop (which previously
made the connector crash with "unavailable after recovery" on large repos).

Startup: discover .codewiki/ from CWD, open the existing SQLite graph if present.
"""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any, Optional

from mcp.server.fastmcp import FastMCP

from codewiki.db.store import GraphStore
from codewiki.graph.traversal import GraphTraversal

mcp = FastMCP("codewiki")


def _check_ready() -> str | None:
    """Return error message if not ready, None if OK."""
    from codewiki.sync.reconciler import is_syncing
    if is_syncing():
        return "CodeWiki is currently syncing. Please retry in a moment."
    if not _traversal:
        _ensure_store()  # auto-open an existing .codewiki graph if present
    if not _traversal:
        return (
            "No code graph indexed for this project.\n"
            "Build it first from a terminal: `codewiki init <project_root>` "
            "(e.g. `codewiki init .`). The graph is NOT built automatically inside the agent."
        )
    return None

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


def _extract_source_block(focal, project_root: Optional[str]) -> Optional[str]:
    """Read the source code for a node from its file (start_line to end_line).

    Used by explore to include actual code so the agent doesn't need a separate
    Read call. Limited to ~100 lines max to stay within budget.
    """
    if not project_root or not focal.file_path:
        return None
    file_path = os.path.join(project_root, focal.file_path)
    if not os.path.isfile(file_path):
        return None
    try:
        with open(file_path, encoding="utf-8", errors="ignore") as f:
            all_lines = f.readlines()
        start = max(0, focal.start_line - 1)
        end = min(len(all_lines), focal.end_line)
        if end - start > 100:
            end = start + 100
        return "".join(all_lines[start:end])
    except Exception:
        return None

# NOTE: AGENTS.md / CLAUDE.md injection now lives in `codewiki.bootstrap`
# (invoked by `codewiki init`, the CLI — NOT by the MCP server, since building
# the graph is a CLI-only concern and must never run inside an MCP handler).


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

# (CLI-only, not exposed as MCP tool — codegraph-aligned)
def codewiki_node(symbol: str) -> str:
    """Get details of a code symbol by name or ID.

    Args:
        symbol: Symbol name (e.g., "DiscountService") or node ID.

    Returns:
        Node details as formatted text.
    """
    err = _check_ready()
    if err:
        return err
    node = _traversal.get_node(symbol)
    if not node:
        return f"Symbol '{symbol}' not found."
    return _format_node_detail(node)


# (CLI-only, not exposed as MCP tool)
def codewiki_callers(symbol: str, max_depth: int = 1) -> str:
    """Find all callers of a symbol (who calls/uses/instantiates it). USE THIS when asked to analyze call relationships / "who calls this method" / dependencies on a symbol.

    Includes instantiates edges (constructing a class is calling its constructor).

    Args:
        symbol: Symbol name or ID.
        max_depth: Traversal depth (default 1 = direct callers only).

    Returns:
        List of caller nodes with file:line locations.
    """
    global _traversal
    if not _traversal:
        return "No project indexed. Run `codewiki init <project_root>` first."
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


# (CLI-only, not exposed as MCP tool)
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
        return "No project indexed. Run `codewiki init <project_root>` first."
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


# (CLI-only, not exposed as MCP tool)
def codewiki_impact(symbol: str, max_depth: int = 3) -> str:
    """Calculate the impact radius (blast radius) of changing a symbol. USE THIS when asked about the impact / affected scope / "what breaks if I change this" of a symbol.

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
        return "No project indexed. Run `codewiki init <project_root>` first."
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


# (CLI-only, not exposed as MCP tool)
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
        return "No project indexed. Run `codewiki init <project_root>` first."
    results = _traversal.search(query, limit=limit)
    if not results:
        return f"No results for '{query}'."
    lines = [f"## Search: '{query}' ({len(results)} results)"]
    for node, score in results:
        lines.append(f"- `{node.file_path}:{node.start_line}` **{node.name}** ({node.kind}) score={score:.2f}")
    return "\n".join(lines)


@mcp.tool()
def codewiki_explore(symbol: str) -> str:
    """Explore a symbol's full context: node details + source code + callers + callees + blast-radius impact.

    PREFER THIS TOOL OVER Grep/Read FOR CODE LOOKUP. Use codewiki_explore (not Grep/Read)
    whenever you need to find a definition, see callers/callees, trace a call chain, or
    assess impact. Only fall back to Grep/Read when you already know the exact file path
    or need a literal substring/pattern match inside a known file.

    It returns everything you need in one call — no need to follow up with node/callers/
    callees separately, and the focal source is included verbatim so you do NOT need a
    separate Read. A blast-radius "Impact" section lists everything that depends on the
    symbol (including HTTP route nodes and Spring-wired callers).

    If the exact symbol is not found, this tool auto-searches the graph and returns the
    closest candidate symbols — try one of those instead of switching to Grep.

    Args:
        symbol: Symbol name (bare name like "DiscountService"), FQN
                ("com.example.DiscountService"), or node ID.

    Returns:
        Combined context view of the symbol and its graph neighborhood (or, on miss,
        the closest matching symbols found via fuzzy search).
    """
    global _traversal, _store
    _ensure_store()  # auto-open an existing .codewiki graph if present
    if not _traversal:
        return (
            "No code graph indexed for this project.\n"
            "Build it first from a terminal: `codewiki init <project_root>` "
            "(e.g. `codewiki init .`). The graph is NOT built automatically inside the agent."
        )
    node = _traversal.get_node(symbol)
    if not node:
        # RC-3 fix: input-contract mismatch — the agent often has a keyword, not an exact
        # symbol. Fall back to FTS and surface candidates instead of a dead-end "not found".
        try:
            candidates = _store.search_nodes_fts(symbol, limit=8) if _store else []
        except Exception:
            candidates = []
        if candidates:
            lines = [f"Symbol '{symbol}' not found exactly. Did you mean one of these? "
                     f"Call codewiki_explore on the chosen name:"]
            for cand, _score in candidates:
                lines.append(f"- `{cand.file_path}:{cand.start_line}` **{cand.name}** ({cand.kind})")
            return "\n".join(lines)
        return f"Symbol '{symbol}' not found and no close matches in the graph."

    budget = _get_explore_budget()
    max_nodes = budget["max_nodes"]
    lines: list[str] = []

    # 1. Node detail
    lines.append(_format_node_detail(node))

    # 2. Source code (focal node — agent should NOT separately Read this file)
    source_block = _extract_source_block(node, _project_root)
    if source_block:
        lines.append(f"\n## Source\n```java\n{source_block}\n```\n"
                      f"*(Source above is complete and verbatim. Treat as already Read — "
                      f"do not call Read on `{node.file_path}`.)*")

    # 3. Callers (budgeted)
    callers = _traversal.callers(node.id, max_depth=1)
    if callers:
        take = min(len(callers), max_nodes // 3)
        lines.append(f"\n## Callers ({len(callers)} total, showing {take})")
        for c in callers[:take]:
            lines.append(f"- `{c.file_path}:{c.start_line}` **{c.name}** ({c.kind})")

    # 4. Callees (budgeted)
    callees = _traversal.callees(node.id, max_depth=1)
    if callees:
        take = min(len(callees), max_nodes // 3)
        lines.append(f"\n## Callees ({len(callees)} total, showing {take})")
        for c in callees[:take]:
            lines.append(f"- `{c.file_path}:{c.start_line}` **{c.name}** ({c.kind})")

    # 4.5 Impact / blast radius (codegraph-aligned: "what depends on this")
    # CodeGraph's explore shows "Blast radius — what depends on these". Route
    # nodes (HTTP endpoints) and Spring-wired callers appear here too.
    impact_result = _traversal.impact(node.id, max_depth=3)
    impact_others = [n for n in impact_result.nodes if n.id != node.id]
    if impact_others:
        take = min(len(impact_others), max_nodes // 3)
        lines.append(f"\n## Impact (blast radius, {len(impact_others)} dependents, showing {take})")
        for n in impact_others[:take]:
            lines.append(f"- `{n.file_path}:{n.start_line}` **{n.name}** ({n.kind})")

    # 5. Containing ancestor
    if _store:
        ancestors = _store.get_incoming_edges(node.id, ["contains"])
        if ancestors:
            lines.append("\n## Ancestor")
            for edge in ancestors[:1]:
                parent = _store.get_node_by_id(edge.source)
                if parent:
                    lines.append(f"Contained in: `{parent.file_path}:{parent.start_line}` **{parent.name}** ({parent.kind})")

    # 5.5 Related source (cluster) — CodeGraph parity: dump verbatim source for
    # the focal symbol's direct callers + callees so the agent sees the whole
    # dependency cluster without extra Read calls.
    related: list = []
    seen_ids: set[str] = set()
    for c in list(callers) + list(callees):
        if c.id in seen_ids:
            continue
        seen_ids.add(c.id)
        if c.kind in ("import", "namespace"):
            continue
        related.append(c)
    if related:
        shown = 0
        lines.append("\n## Related source (cluster)")
        for c in related:
            if shown >= 4:
                lines.append(f"- ... and {len(related) - shown} more (use `codewiki_explore` on each)")
                break
            blk = _extract_source_block(c, _project_root)
            if blk:
                lines.append(f"\n### {c.name} ({c.kind}) @ `{c.file_path}:{c.start_line}`\n```java\n{blk}\n```")
                shown += 1

    # 6. Budget annotation (codegraph-aligned: tell agent how many explore calls remain)
    file_count = len(_store.get_all_files())
    max_calls = 1 if file_count < 500 else (2 if file_count < 5000 else (3 if file_count < 15000 else (4 if file_count < 25000 else 5)))
    lines.append(
        f"\n---\n"
        f"*Explore budget: {_get_explore_budget()['max_chars']} chars max output. "
        f"Project has {file_count} files → ~{max_calls} explore calls recommended. "
        f"Keep using codewiki_explore for further graph queries — do not fall back to Read.*"
    )

    return "\n".join(lines)


# =========================================================================
# Build tools are intentionally NOT exposed as MCP tools.
# `codewiki init` / `codewiki sync` live in cli.py and run in the terminal.
# Keeping long-running builds out of the MCP handler avoids blocking the
# stdio event loop (which previously crashed the connector on large repos
# with "unavailable after recovery").
# =========================================================================

# (build/sync are CLI-only — `codewiki init` / `codewiki sync`)


# =========================================================================
# Entry point
# =========================================================================

def main():
    """Run the MCP server over stdio.

    Only serves `codewiki_explore`. Graph building/syncing is done via the
    CLI (`codewiki init` / `codewiki sync`), never here — so a long build
    can never block the stdio event loop.
    """
    _ensure_store()  # auto-open an existing .codewiki graph on startup
    mcp.run()


if __name__ == "__main__":
    main()
