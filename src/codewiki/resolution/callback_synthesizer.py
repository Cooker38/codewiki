"""
Callback Synthesizer — Python translation of codegraph/src/resolution/callback-synthesizer.ts.

Synthesizes type_of / returns / overrides edges that aren't directly extractable
from the AST but can be inferred from existing graph data.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from codewiki.types import Edge

if TYPE_CHECKING:
    from codewiki.db.store import GraphStore


class CallbackSynthesizer:
    """
    Synthesizes inferred edges (callback-synthesizer.ts).

    1. type_of: field/variable with return_type → class node of that type
    2. returns: method with return_type → class node of that type
    3. overrides: method in subclass → same-named method in parent class (via extends)
    """

    def __init__(self, store: "GraphStore"):
        self.store = store

    def synthesize_all(self) -> dict[str, int]:
        """Run all synthesis passes. Returns counts by kind."""
        return {
            "type_of": self._synthesize_type_of(),
            "returns": self._synthesize_returns(),
            "overrides": self._synthesize_overrides(),
        }

    def _synthesize_type_of(self) -> int:
        """
        For each field/variable/parameter with a return_type,
        create a `type_of` edge to the class node of that type.
        """
        # Find nodes with return_type that don't already have a type_of edge
        rows = self.store.conn.execute(
            """SELECT n.id, n.return_type, n.file_path
               FROM nodes n
               WHERE n.return_type IS NOT NULL
               AND n.kind IN ('field', 'variable', 'parameter', 'property')
               AND NOT EXISTS (
                   SELECT 1 FROM edges e
                   WHERE e.source = n.id AND e.kind = 'type_of'
               )"""
        ).fetchall()

        edges: list[Edge] = []
        for row in rows:
            type_name = row["return_type"]
            # Find class node with this name
            target = self.store.conn.execute(
                "SELECT id FROM nodes WHERE name = ? AND kind IN ('class', 'interface', 'enum', 'struct') LIMIT 1",
                (type_name,)
            ).fetchone()
            if target:
                edges.append(Edge(
                    source=row["id"],
                    target=target["id"],
                    kind="type_of",
                    provenance="heuristic",
                ))

        if edges:
            self.store.insert_edges(edges)
        return len(edges)

    def _synthesize_returns(self) -> int:
        """
        For each method with a return_type,
        create a `returns` edge to the class node of that type.
        """
        rows = self.store.conn.execute(
            """SELECT n.id, n.return_type
               FROM nodes n
               WHERE n.return_type IS NOT NULL
               AND n.kind IN ('method', 'function')
               AND NOT EXISTS (
                   SELECT 1 FROM edges e
                   WHERE e.source = n.id AND e.kind = 'returns'
               )"""
        ).fetchall()

        edges: list[Edge] = []
        for row in rows:
            type_name = row["return_type"]
            target = self.store.conn.execute(
                "SELECT id FROM nodes WHERE name = ? AND kind IN ('class', 'interface', 'enum', 'struct') LIMIT 1",
                (type_name,)
            ).fetchone()
            if target:
                edges.append(Edge(
                    source=row["id"],
                    target=target["id"],
                    kind="returns",
                    provenance="heuristic",
                ))

        if edges:
            self.store.insert_edges(edges)
        return len(edges)

    def _synthesize_overrides(self) -> int:
        """
        For each method in a subclass that has the same name as a method
        in its parent class (connected via extends edge),
        create an `overrides` edge.
        """
        # Find extends edges: subclass → parent class
        extends_rows = self.store.conn.execute(
            "SELECT source AS subclass_id, target AS parent_id FROM edges WHERE kind = 'extends'"
        ).fetchall()

        edges: list[Edge] = []
        for ext in extends_rows:
            subclass_id = ext["subclass_id"]
            parent_id = ext["parent_id"]

            # Find methods in subclass
            subclass_methods = self.store.conn.execute(
                """SELECT n.id, n.name FROM nodes n
                   JOIN edges e ON e.target = n.id
                   WHERE n.kind = 'method' AND e.kind = 'contains' AND e.source = ?""",
                (subclass_id,)
            ).fetchall()

            for sub_method in subclass_methods:
                # Find same-named method in parent
                parent_method = self.store.conn.execute(
                    """SELECT n.id FROM nodes n
                       JOIN edges e ON e.target = n.id
                       WHERE n.kind = 'method' AND n.name = ?
                       AND e.kind = 'contains' AND e.source = ?
                       LIMIT 1""",
                    (sub_method["name"], parent_id)
                ).fetchone()

                if parent_method:
                    # Check not already exists
                    exists = self.store.conn.execute(
                        "SELECT 1 FROM edges WHERE source = ? AND target = ? AND kind = 'overrides'",
                        (sub_method["id"], parent_method["id"])
                    ).fetchone()
                    if not exists:
                        edges.append(Edge(
                            source=sub_method["id"],
                            target=parent_method["id"],
                            kind="overrides",
                            provenance="heuristic",
                        ))

        if edges:
            self.store.insert_edges(edges)
        return len(edges)
