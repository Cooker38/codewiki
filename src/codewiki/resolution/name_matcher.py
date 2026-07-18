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


def _get_import_mappings(store: "GraphStore", file_path: str) -> dict[str, str]:
    """
    Build a {localName: FQN} map from this file's import nodes.
    Used by type inference to resolve imported class names to their FQN.
    Aligned with codegraph's getImportMappings (import-resolver.ts).
    """
    rows = store.conn.execute(
        "SELECT name FROM nodes WHERE kind = 'import' AND file_path = ?",
        (file_path,)
    ).fetchall()
    mappings: dict[str, str] = {}
    for row in rows:
        fqn = row["name"]
        last_dot = fqn.rfind(".")
        local_name = fqn[last_dot + 1:] if last_dot >= 0 else fqn
        if local_name and local_name != "*":
            mappings[local_name] = fqn
    return mappings


def _resolve_method_on_type(
    type_name: str, method_name: str, ref: "UnresolvedReference",
    store: "GraphStore", confidence: float, resolved_by: str
):
    """
    Find a method node whose qualified_name ends with 'typeName::methodName'.
    Aligned with codegraph's resolveMethodOnType (name-matcher.ts:498-595).
    """
    rows = store.conn.execute(
        """SELECT n.id FROM nodes n
           WHERE n.kind = 'method' AND n.name = ?
           AND n.qualified_name LIKE '%' || ? || '::' || ?""",
        (method_name, type_name, method_name)
    ).fetchall()
    if rows:
        return {
            "target_node_id": rows[0]["id"],
            "confidence": confidence,
            "resolved_by": resolved_by,
        }
    return None

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


def _infer_field_type(field_name: str, ref: "UnresolvedReference", store: "GraphStore") -> Optional[str]:
    """
    Infer the type of a field by name from the containing class.
    Aligned with codegraph's inferJavaFieldReceiverType (name-matcher.ts:1005-1052).

    Strategy: find the caller's containing class → find field with same name →
    parse type name from the field's signature (format: "<TypeName> <fieldName>").
    """
    # Find the containing class of the caller
    containing_rows = store.conn.execute(
        """SELECT e.source FROM edges e
           WHERE e.target = ? AND e.kind = 'contains'""",
        (ref.from_node_id,)
    ).fetchall()
    if not containing_rows:
        return None

    class_id = containing_rows[0]["source"]

    # Find field with matching name in this class
    field_rows = store.conn.execute(
        """SELECT n.id, n.signature FROM nodes n
           JOIN edges e ON e.target = n.id
           WHERE n.kind = 'field' AND n.name = ?
           AND e.kind = 'contains' AND e.source = ? AND n.signature IS NOT NULL""",
        (field_name, class_id)
    ).fetchall()
    if not field_rows:
        return None

    sig = field_rows[0]["signature"]
    if not sig:
        return None

    # Signature format: "private TypeName fieldName" or "TypeName fieldName"
    # Extract the type name (tokens before the field name)
    sig_tokens = sig.split()
    # Remove trailing field name tokens until we get just the type
    name_parts = field_name
    type_tokens = []
    for token in sig_tokens:
        if token == name_parts:
            break
        type_tokens.append(token)

    if not type_tokens:
        return None

    # The last token before the field name is typically the simple type name
    type_name = type_tokens[-1]
    # Strip generics, array brackets
    type_name = type_name.split("<")[0].replace("[", "").replace("]", "").strip()
    if type_name and type_name[0].isupper():
        return type_name
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
        # Type inference: receiver is a variable/field, not a class name.
        # e.g. "service.executeTool()" → receiver="service" ≠ class name "ScriptExecutionService"
        # Strategy chain (aligned with codegraph name-matcher.ts: matchMethodCall):
        #   (a) infer from local variable type
        #   (b) infer from field type in the containing class

        # (a) Local variable inference: find variable with same name in this file
        var_rows = store.conn.execute(
            "SELECT return_type FROM nodes WHERE name = ? AND kind = 'variable'"
            " AND file_path = ? AND return_type IS NOT NULL",
            (receiver, ref.file_path)
        ).fetchall()

        if var_rows:
            type_name = var_rows[0]["return_type"]
        else:
            # (b) Field inference: find containing class, then field with same name
            type_name = _infer_field_type(receiver, ref, store)

        if type_name:
            # Resolve FQN from import mappings
            imports = _get_import_mappings(store, ref.file_path)
            # Find class by type name (with import FQN preference)
            result = _resolve_method_on_type(
                type_name, method, ref, store, 0.85, "inferred-method-call"
            )
            if result:
                return result

            # Fallback: try with bare type name → find class, then its method
            class_rows2 = store.conn.execute(
                "SELECT id FROM nodes WHERE name = ? AND kind IN ('class','interface','struct','trait')",
                (type_name,)
            ).fetchall()
            if class_rows2:
                for cr2 in class_rows2:
                    mr = store.conn.execute(
                        """SELECT n.id FROM nodes n
                           JOIN edges e ON e.target = n.id
                           WHERE n.kind = 'method' AND n.name = ?
                           AND e.kind = 'contains' AND e.source = ?""",
                        (method, cr2["id"])
                    ).fetchall()
                    if mr:
                        return {
                            "target_node_id": mr[0]["id"],
                            "confidence": 0.7,
                            "resolved_by": "inferred-method-call",
                        }
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
