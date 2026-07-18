"""
Graph Traversal — Python translation of codegraph/src/graph/traversal.ts.

Provides callers, callees, and impact (blast radius) algorithms.
Key invariants from codegraph:
- "先 visited 后深度": mark visited BEFORE depth check (#1086/#1089)
- callers includes instantiates (#774)
- impact excludes contains (防爆炸, #536)
- impact expands container nodes (class → methods) at same depth
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Optional

from codewiki.types import Edge, Node

if TYPE_CHECKING:
    from codewiki.db.store import GraphStore


@dataclass
class TraversalResult:
    """Result of a graph traversal."""
    nodes: list[Node] = field(default_factory=list)
    edges: list[Edge] = field(default_factory=list)


# Edge kinds that represent "who calls/uses this" (callers) or "what this calls/uses" (callees)
# `uses` carries Spring DI wiring (bean injection / boot-context), added by SpringResolver.
_CALL_EDGE_KINDS = ["calls", "references", "imports", "instantiates", "uses"]

# Container node kinds — impact expands these to include their children
_CONTAINER_KINDS = frozenset(["class", "interface", "struct", "trait", "protocol", "module", "enum"])


class GraphTraversal:
    """
    Graph traversal algorithms (traversal.ts).

    All methods use BFS with "先 visited 后深度" (mark visited before depth check)
    to prevent duplicate nodes at depth boundaries (#1086/#1089).
    """

    def __init__(self, store: "GraphStore"):
        self.store = store

    # =========================================================================
    # callers (traversal.ts:261-310)
    # =========================================================================

    def callers(self, node_id: str, max_depth: int = 1) -> list[Node]:
        """
        Find all nodes that call/reference/use this node (traversal.ts:261).

        Includes instantiates (construction = calling constructor, #774).
        Returns caller nodes (not the focal node itself).

        For a container node (class/interface/enum), aggregates over its member
        methods so `callers SomeClass` reflects who calls the class's methods
        (CodeGraph parity — aligns class-level callee/caller queries).
        """
        result: list[Node] = []
        visited: set[str] = set()
        for seed in self._aggregate_seeds(node_id):
            self._callers_recursive(seed, max_depth, 0, result, visited)
        return result

    def _aggregate_seeds(self, node_id: str) -> list[str]:
        """If node is a container, return it plus its member method/constructor ids."""
        node = self.store.get_node_by_id(node_id)
        if node and node.kind in _CONTAINER_KINDS:
            members = self.store.get_contained_nodes(node_id, ["method", "constructor"])
            if members:
                return [node_id] + [m.id for m in members]
        return [node_id]

    def _callers_recursive(
        self, node_id: str, max_depth: int, current_depth: int,
        result: list[Node], visited: set[str],
    ) -> None:
        # Mark visited BEFORE depth check (#1086)
        if node_id in visited:
            return
        visited.add(node_id)
        if current_depth >= max_depth:
            return

        incoming = self.store.get_incoming_edges(node_id, _CALL_EDGE_KINDS)
        if not incoming:
            return

        for edge in incoming:
            if edge.source not in visited:
                node = self.store.get_node_by_id(edge.source)
                if node:
                    result.append(node)
                    self._callers_recursive(edge.source, max_depth, current_depth + 1, result, visited)

    # =========================================================================
    # callees (traversal.ts:319-360)
    # =========================================================================

    def callees(self, node_id: str, max_depth: int = 1) -> list[Node]:
        """
        Find all nodes called/referenced/used by this node (traversal.ts:319).

        Includes instantiates (function that constructs a class has it as callee, #774).

        For a container node (class/interface/enum), aggregates over its member
        methods so `callees SomeClass` reflects what the class's methods call
        (CodeGraph parity — field.method() calls like `svc.foo()` are surfaced).
        """
        result: list[Node] = []
        visited: set[str] = set()
        for seed in self._aggregate_seeds(node_id):
            self._callees_recursive(seed, max_depth, 0, result, visited)
        return result

    def _callees_recursive(
        self, node_id: str, max_depth: int, current_depth: int,
        result: list[Node], visited: set[str],
    ) -> None:
        if node_id in visited:
            return
        visited.add(node_id)
        if current_depth >= max_depth:
            return

        outgoing = self.store.get_outgoing_edges(node_id, _CALL_EDGE_KINDS)
        if not outgoing:
            return

        for edge in outgoing:
            if edge.target not in visited:
                node = self.store.get_node_by_id(edge.target)
                if node:
                    result.append(node)
                    self._callees_recursive(edge.target, max_depth, current_depth + 1, result, visited)

    # =========================================================================
    # impact / blast radius (traversal.ts:520-600)
    # =========================================================================

    def impact(self, node_id: str, max_depth: int = 3) -> TraversalResult:
        """
        Find all nodes that depend on this node (traversal.ts:520).

        Key rules:
        - Excludes `contains` edges (a container doesn't depend on its members, #536)
        - Expands container nodes (class → methods) at same depth
        - "先 visited 后深度" (#1089)
        """
        focal = self.store.get_node_by_id(node_id)
        if not focal:
            return TraversalResult()

        nodes: dict[str, Node] = {focal.id: focal}
        edges: list[Edge] = []
        visited: set[str] = set()

        self._impact_recursive(node_id, max_depth, 0, nodes, edges, visited)

        return TraversalResult(nodes=list(nodes.values()), edges=edges)

    def _impact_recursive(
        self, node_id: str, max_depth: int, current_depth: int,
        nodes: dict[str, Node], edges: list[Edge], visited: set[str],
    ) -> None:
        # Mark visited BEFORE depth check (#1089)
        if node_id in visited:
            return
        visited.add(node_id)
        if current_depth >= max_depth:
            return

        focal = self.store.get_node_by_id(node_id)
        if not focal:
            return

        # For container nodes, expand into children at same depth
        if focal.kind in _CONTAINER_KINDS:
            contains_edges = self.store.get_outgoing_edges(node_id, ["contains"])
            for edge in contains_edges:
                if edge.target not in visited:
                    child = self.store.get_node_by_id(edge.target)
                    if child:
                        nodes[child.id] = child
                        edges.append(edge)
                        # Recurse at same depth (children are part of the same symbol)
                        self._impact_recursive(edge.target, max_depth, current_depth, nodes, edges, visited)

        # Get all incoming edges EXCLUDING contains (防爆炸, #536)
        incoming = self.store.get_incoming_edges(node_id)
        filtered = [e for e in incoming if e.kind != "contains"]

        for edge in filtered:
            source = self.store.get_node_by_id(edge.source)
            if not source:
                continue
            # Record edge unconditionally (even if node already collected, #1089)
            edges.append(edge)
            if edge.source not in visited:
                nodes[source.id] = source
                self._impact_recursive(edge.source, max_depth, current_depth + 1, nodes, edges, visited)

    # =========================================================================
    # node (get node details + source snippet)
    # =========================================================================

    def get_node(self, node_id_or_name: str) -> Optional[Node]:
        """
        Get a node by ID or by name (first match).
        If the input looks like a hash ID (contains ':'), search by ID.
        Otherwise, search by name (first match).
        """
        # Try by ID first
        if ":" in node_id_or_name and len(node_id_or_name) > 10:
            node = self.store.get_node_by_id(node_id_or_name)
            if node:
                return node

        # Definition kinds get priority over import/file/variable (codegraph-aligned)
        _DEF_PRIORITY = (
            "CASE WHEN kind IN "
            "('class','interface','enum','struct','trait','type_alias',"
            "'method','function','module','namespace') "
            "THEN 0 ELSE 1 END"
        )

        # FQN: if query contains dots, try qualified_name BEFORE bare name.
        # Import nodes store FQN as their name, so bare-name search would
        # match import nodes for FQN queries (e.g. "com.x.Service").
        # We check qualified_name first to prefer actual definitions.
        if "." in node_id_or_name:
            last_dot = node_id_or_name.rfind(".")
            fqn_last = node_id_or_name[:last_dot] + "::" + node_id_or_name[last_dot + 1:]
            # Try exactly
            rows = self.store.conn.execute(
                f"SELECT * FROM nodes WHERE qualified_name = ? ORDER BY {_DEF_PRIORITY}, start_line LIMIT 1",
                (fqn_last,)
            ).fetchall()
            if not rows:
                fqn_all = node_id_or_name.replace(".", "::")
                rows = self.store.conn.execute(
                    f"SELECT * FROM nodes WHERE qualified_name LIKE '%' || ? ORDER BY {_DEF_PRIORITY}, start_line LIMIT 1",
                    (fqn_all,)
                ).fetchall()
            if rows:
                from codewiki.db.store import _row_to_node
                return _row_to_node(rows[0])

        # Try by name (case-sensitive first, then case-insensitive)
        # Definition nodes (class/method/...) prioritized over import/etc.
        rows = self.store.conn.execute(
            f"SELECT * FROM nodes WHERE name = ? ORDER BY {_DEF_PRIORITY}, start_line LIMIT 1",
            (node_id_or_name,)
        ).fetchall()
        if not rows:
            rows = self.store.conn.execute(
                f"SELECT * FROM nodes WHERE lower(name) = lower(?) ORDER BY {_DEF_PRIORITY}, start_line LIMIT 1",
                (node_id_or_name,)
            ).fetchall()
        if rows:
            from codewiki.db.store import _row_to_node
            return _row_to_node(rows[0])

            if rows:
                from codewiki.db.store import _row_to_node
                return _row_to_node(rows[0])

        return None

    # =========================================================================
    # search (FTS5 + name match)
    # =========================================================================

    def search(self, query: str, limit: int = 50) -> list[tuple[Node, float]]:
        """
        Search nodes by FTS5 BM25 ranking (delegates to store.search_nodes_fts).
        Returns (node, score) pairs ordered by relevance.
        """
        return self.store.search_nodes_fts(query, limit=limit)
