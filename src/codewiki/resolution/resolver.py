"""
Reference Resolver — Python translation of codegraph/src/resolution/index.ts.

Orchestrates the resolution of pending unresolved_refs into actual edges.
Strategy chain for Java: JVM import → framework → name match.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Optional

from codewiki.types import Edge, UnresolvedReference
from codewiki.resolution.import_resolver import resolve_jvm_import
from codewiki.resolution.name_matcher import match_reference

if TYPE_CHECKING:
    from codewiki.db.store import GraphStore


@dataclass
class ResolutionResult:
    """Result of a resolution pass."""
    total: int = 0
    resolved: int = 0
    unresolved: int = 0
    by_method: dict[str, int] = field(default_factory=dict)
    duration_ms: int = 0


class ReferenceResolver:
    """
    Resolves pending unresolved_refs into edges (index.ts:203 ReferenceResolver).

    Strategy chain (simplified for Java):
    1. JVM import resolution (import com.example.Foo → qualifiedName match)
    2. Name matching (receiver.method → method in class; bare name → exact match)
    3. Mark unresolvable as failed (keep name_tail for retry)
    """

    def __init__(self, store: "GraphStore", frameworks: Optional[list[str]] = None):
        self.store = store
        self.frameworks = frameworks or []

    def resolve_and_persist(self) -> ResolutionResult:
        """
        Resolve all pending refs and persist results (index.ts:1215 resolveAndPersistBatched).

        - Resolved refs → create edges + delete ref rows
        - Unresolvable refs → mark as 'failed' with name_tail
        """
        start_time = time.time()
        stats = ResolutionResult()
        pending = self.store.get_all_pending_refs()
        stats.total = len(pending)

        resolved_edges: list[Edge] = []
        resolved_ref_ids: list[int] = []
        failed_refs: list[tuple[int, str]] = []

        for ref in pending:
            result = self._resolve_one(ref)

            if result:
                # Create edge
                edge = Edge(
                    source=ref.from_node_id,
                    target=result["target_node_id"],
                    kind=ref.reference_kind if ref.reference_kind != "function_ref" else "references",
                    line=ref.line,
                    column=ref.column,
                    provenance="heuristic",
                )
                resolved_edges.append(edge)
                resolved_ref_ids.append(ref.id)
                stats.resolved += 1
                method = result.get("resolved_by", "unknown")
                stats.by_method[method] = stats.by_method.get(method, 0) + 1
            else:
                # Mark as failed with name_tail
                name_tail = ref.reference_name.split(".")[-1]
                if ref.id:
                    failed_refs.append((ref.id, name_tail))
                stats.unresolved += 1

        # Persist: insert edges
        if resolved_edges:
            self.store.insert_edges(resolved_edges)

        # Persist: delete resolved refs
        for ref_id in resolved_ref_ids:
            self.store.delete_unresolved_ref(ref_id)

        # Persist: mark failed refs
        for ref_id, name_tail in failed_refs:
            self.store.mark_unresolved_failed(ref_id, name_tail)

        stats.duration_ms = int((time.time() - start_time) * 1000)
        return stats

    def _resolve_one(self, ref: UnresolvedReference):
        """
        Resolve a single reference (index.ts:765 resolveOne).

        Strategy chain: JVM import → name match.
        """
        # Skip built-in/external (java.lang.*, java.util.* won't match any node → naturally fail)

        # Strategy 1: JVM import resolution
        if ref.reference_kind == "imports":
            result = resolve_jvm_import(ref, self.store)
            if result:
                return result

        # Strategy 2: Name matching (method call, exact name, dotted chain)
        result = match_reference(ref, self.store)
        if result:
            return result

        return None
