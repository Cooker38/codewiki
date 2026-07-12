"""
Name Matcher — Python translation of codegraph/src/resolution/name-matcher.ts.

Matches reference names to graph nodes using a strategy chain:
1. Method call pattern (receiver.method → find method in class)
2. Dotted call chain (Foo.getInstance().bar → return type inference)
3. Exact name match (bare name → same-file first, then cross-file)
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from codewiki.db.store import GraphStore
    from codewiki.types import UnresolvedReference

# Pattern for dotted call chain: `Foo.getInstance().bar`
_CHAIN_PATTERN = re.compile(r"^(.+)\.(\w+)\(\)\.(\w+)$")


def match_reference(ref: "UnresolvedReference", store: "GraphStore"):
    """
    Match a reference to a node (name-matcher.ts:1899-2030).

    Strategy chain for Java:
    1. Dotted call chain (Foo.getInstance().bar)
    2. Method call pattern (receiver.method)
    3. Exact name match (bare name or qualified)
    """
    # 1. Dotted call chain: Foo.getInstance().bar
    result = _match_dotted_call_chain(ref, store)
    if result:
        return result

    # 2. Method call pattern: receiver.method
    result = _match_method_call(ref, store)
    if result:
        return result

    # 3. Exact name match
    result = _match_by_exact_name(ref, store)
    if result:
        return result

    return None


def _match_dotted_call_chain(ref: "UnresolvedReference", store: "GraphStore"):
    """
    Match `Foo.getInstance().bar` pattern (name-matcher.ts matchDottedCallChain).

    Resolve the method `bar` on the class that `Foo.getInstance` returns.
    Simplified: find a method named `bar` in a class named `Foo` (the receiver
    of the inner call).
    """
    if ref.reference_kind != "calls":
        return None

    m = _CHAIN_PATTERN.match(ref.reference_name)
    if not m:
        return None

    receiver_class = m.group(1)
    inner_method = m.group(2)
    outer_method = m.group(3)

    # Find the class node for the receiver
    class_rows = store.conn.execute(
        "SELECT id FROM nodes WHERE name = ? AND kind IN ('class', 'interface', 'struct')",
        (receiver_class,)
    ).fetchall()
    if not class_rows:
        return None

    # Find the method on that class
    method_rows = store.conn.execute(
        """SELECT n.id FROM nodes n
           JOIN edges e ON e.target = n.id
           WHERE n.kind = 'method' AND n.name = ?
           AND e.kind = 'contains' AND e.source = ?""",
        (outer_method, class_rows[0]["id"])
    ).fetchall()

    if method_rows:
        return {
            "target_node_id": method_rows[0]["id"],
            "confidence": 0.75,
            "resolved_by": "call-chain",
        }

    return None


def _match_method_call(ref: "UnresolvedReference", store: "GraphStore"):
    """
    Match `receiver.method` pattern (name-matcher.ts matchMethodCall).

    Find a method named `method` in a class/interface named `receiver`.
    """
    name = ref.reference_name
    if ref.reference_kind not in ("calls", "references"):
        return None

    dot_idx = name.find(".")
    if dot_idx <= 0:
        return None

    receiver = name[:dot_idx]
    method = name[dot_idx + 1:]

    # Skip if method contains dots (chained call, handled elsewhere)
    if "." in method:
        return None

    # Skip self/this/super
    if receiver in ("self", "this", "super", "parent", "static"):
        return None

    # Find class/interface named `receiver`
    class_rows = store.conn.execute(
        "SELECT id, file_path FROM nodes WHERE name = ? AND kind IN ('class', 'interface', 'struct', 'trait')",
        (receiver,)
    ).fetchall()

    if not class_rows:
        # Try variable/field with this name, then find its type
        # (simplified: just look for any node named `receiver`)
        return None

    # Find method `method` contained in that class
    for class_row in class_rows:
        method_rows = store.conn.execute(
            """SELECT n.id FROM nodes n
               JOIN edges e ON e.target = n.id
               WHERE n.kind = 'method' AND n.name = ?
               AND e.kind = 'contains' AND e.source = ?""",
            (method, class_row["id"])
        ).fetchall()
        if method_rows:
            return {
                "target_node_id": method_rows[0]["id"],
                "confidence": 0.85,
                "resolved_by": "method-call",
            }

    return None


def _match_by_exact_name(ref: "UnresolvedReference", store: "GraphStore"):
    """
    Match by exact name (name-matcher.ts matchByExactName).

    Same-file preference: if multiple nodes share the name, prefer one
    in the same file as the reference.
    """
    name = ref.reference_name

    # Strip receiver prefix for bare method name: `Foo.bar` → try `bar` as fallback
    if "." in name and ref.reference_kind == "calls":
        bare_name = name.split(".")[-1]
    else:
        bare_name = name

    # Try exact name match, same-file first
    rows = store.conn.execute(
        """SELECT id, file_path, kind FROM nodes
           WHERE name = ? OR name = ?
           ORDER BY CASE WHEN file_path = ? THEN 0 ELSE 1 END""",
        (name, bare_name, ref.file_path)
    ).fetchall()

    if not rows:
        return None

    # Filter by appropriate kind for the reference type
    appropriate_kinds = _appropriate_kinds(ref.reference_kind)
    filtered = [r for r in rows if r["kind"] in appropriate_kinds] if appropriate_kinds else rows

    if not filtered:
        filtered = rows

    # If multiple candidates, prefer same-file
    same_file = [r for r in filtered if r["file_path"] == ref.file_path]
    chosen = same_file[0] if same_file else filtered[0]

    confidence = 0.8 if same_file else 0.6
    return {
        "target_node_id": chosen["id"],
        "confidence": confidence,
        "resolved_by": "exact-match" if same_file else "cross-file-match",
    }


def _appropriate_kinds(ref_kind: str) -> set[str]:
    """Which node kinds are appropriate targets for a given reference kind."""
    if ref_kind == "calls":
        return {"method", "function"}
    if ref_kind == "instantiates":
        return {"class", "struct"}
    if ref_kind == "extends":
        return {"class", "interface", "trait"}
    if ref_kind == "implements":
        return {"interface", "trait"}
    if ref_kind == "imports":
        return {"class", "interface", "enum", "struct", "namespace", "module"}
    if ref_kind == "references":
        return {"variable", "constant", "field", "property", "enum_member", "parameter"}
    return set()
