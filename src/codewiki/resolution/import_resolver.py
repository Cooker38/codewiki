"""
Import Resolver — Python translation of codegraph/src/resolution/import-resolver.ts.

Resolves Java import references by matching the import's FQN to node qualifiedNames.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from codewiki.db.store import GraphStore
    from codewiki.types import UnresolvedReference


def resolve_jvm_import(ref: "UnresolvedReference", store: "GraphStore"):
    """
    Resolve a JVM import reference (import-resolver.ts:1142-1174).

    `import com.example.Foo` → find node with qualifiedName `com.example::Foo`.
    Wildcard imports (`com.example.*`) return None (punt to name-matcher).
    """
    if ref.reference_kind != "imports":
        return None
    if ref.language not in ("java", "kotlin"):
        return None

    fqn = ref.reference_name
    last_dot = fqn.rfind(".")
    if last_dot <= 0:
        return None

    pkg = fqn[:last_dot]
    sym = fqn[last_dot + 1:]
    if sym == "*":
        return None  # Wildcard → punt to name-matcher

    # Look for node with qualifiedName matching `pkg::sym`
    target_qn = f"{pkg}::{sym}"
    rows = store.conn.execute(
        "SELECT id FROM nodes WHERE qualified_name = ?", (target_qn,)
    ).fetchall()

    if not rows:
        # Also try just the symbol name (for classes without package prefix in qualifiedName)
        rows = store.conn.execute(
            "SELECT id FROM nodes WHERE name = ? AND kind IN ('class', 'interface', 'enum', 'struct', 'trait')",
            (sym,)
        ).fetchall()

    if not rows:
        return None

    # Pick closest by file path proximity (simplified: first match)
    target_id = rows[0]["id"]
    return {
        "target_node_id": target_id,
        "confidence": 0.95,
        "resolved_by": "import",
    }
