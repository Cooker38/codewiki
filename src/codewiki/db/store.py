"""
Database store operations — Python translation of codegraph/src/db/queries.ts insert methods.

Provides DB initialization, schema execution, and CRUD operations on nodes/edges/files/unresolved_refs.
"""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Any, Optional

from codewiki.types import Edge, FileRecord, Node, UnresolvedReference
from codewiki.search.identifier_segments import split_identifier_segments

_SCHEMA_PATH = Path(__file__).parent / "schema.sql"

# Kinds that contribute name segments to the vocab (queries.ts:348-349).
# file basenames duplicate symbols inside them; import names are module specifiers.
_SEGMENTABLE_KINDS = frozenset(k for k in [
    "module", "class", "struct", "interface", "trait", "protocol",
    "function", "method", "property", "field", "variable", "constant",
    "enum", "enum_member", "type_alias", "namespace", "parameter",
    "export", "route", "component",
])


class GraphStore:
    """
    SQLite-backed graph store. Wraps a single sqlite3.Connection and provides
    insert/query methods aligned with codegraph's QueryBuilder.
    """

    def __init__(self, db_path: str = ":memory:"):
        self._conn = sqlite3.connect(db_path, isolation_level=None)  # autocommit off, manual transactions
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._segmented_names: set[str] = set()

    @property
    def conn(self) -> sqlite3.Connection:
        return self._conn

    def init_schema(self) -> None:
        """Execute schema.sql to create all tables, indexes, triggers."""
        sql = _SCHEMA_PATH.read_text(encoding="utf-8")
        self._conn.executescript(sql)

    def close(self) -> None:
        self._conn.close()

    # =========================================================================
    # Node operations (queries.ts:270-365)
    # =========================================================================

    def insert_node(self, node: Node) -> None:
        """Insert or replace a node (queries.ts:270-343). Includes name_segment_vocab fill."""
        if not node.id or not node.kind or not node.name or not node.file_path or not node.language:
            return

        self._conn.execute(
            """INSERT OR REPLACE INTO nodes (
                id, kind, name, qualified_name, file_path, language,
                start_line, end_line, start_column, end_column,
                docstring, signature, visibility,
                is_exported, is_async, is_static, is_abstract,
                decorators, type_parameters, return_type, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                node.id, node.kind, node.name,
                node.qualified_name or node.name,
                node.file_path, node.language,
                node.start_line, node.end_line,
                node.start_column, node.end_column,
                node.docstring, node.signature, node.visibility,
                int(node.is_exported), int(node.is_async),
                int(node.is_static), int(node.is_abstract),
                json.dumps(node.decorators) if node.decorators else None,
                json.dumps(node.type_parameters) if node.type_parameters else None,
                node.return_type,
                node.updated_at or int(time.time() * 1000),
            ),
        )

        if node.kind in _SEGMENTABLE_KINDS:
            self._insert_name_segments(node.name)

    def _insert_name_segments(self, name: str) -> None:
        """Populate name_segment_vocab (queries.ts:353-365)."""
        if name in self._segmented_names:
            return
        self._segmented_names.add(name)
        for segment in split_identifier_segments(name):
            self._conn.execute(
                "INSERT OR IGNORE INTO name_segment_vocab (segment, name) VALUES (?, ?)",
                (segment, name),
            )

    def insert_nodes(self, nodes: list[Node]) -> None:
        """Insert multiple nodes in a transaction (queries.ts:367-...)."""
        self._conn.execute("BEGIN")
        try:
            for node in nodes:
                self.insert_node(node)
            self._conn.execute("COMMIT")
        except Exception:
            self._conn.execute("ROLLBACK")
            raise

    def get_node_by_id(self, node_id: str) -> Optional[Node]:
        row = self._conn.execute("SELECT * FROM nodes WHERE id = ?", (node_id,)).fetchone()
        return _row_to_node(row) if row else None

    def get_nodes_by_file(self, file_path: str) -> list[Node]:
        rows = self._conn.execute(
            "SELECT * FROM nodes WHERE file_path = ? ORDER BY start_line", (file_path,)
        ).fetchall()
        return [_row_to_node(r) for r in rows]

    def delete_nodes_by_file(self, file_path: str) -> None:
        """Delete all nodes from a file (FK cascade deletes edges + unresolved_refs)."""
        self._conn.execute("DELETE FROM nodes WHERE file_path = ?", (file_path,))

    # =========================================================================
    # Edge operations (queries.ts:1470-1510)
    # =========================================================================

    def insert_edge(self, edge: Edge) -> None:
        """Insert an edge with INSERT OR IGNORE dedup (queries.ts:1470-1487)."""
        self._conn.execute(
            """INSERT OR IGNORE INTO edges (source, target, kind, metadata, line, col, provenance)
            VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                edge.source, edge.target, edge.kind,
                json.dumps(edge.metadata) if edge.metadata else None,
                edge.line, edge.column,
                edge.provenance,
            ),
        )

    def insert_edges(self, edges: list[Edge]) -> None:
        """Insert multiple edges in a transaction, skipping edges with missing endpoints."""
        if not edges:
            return
        self._conn.execute("BEGIN")
        try:
            endpoint_ids = set()
            for e in edges:
                endpoint_ids.add(e.source)
                endpoint_ids.add(e.target)
            existing = self._get_existing_node_ids(list(endpoint_ids))
            for e in edges:
                if e.source in existing and e.target in existing:
                    self.insert_edge(e)
            self._conn.execute("COMMIT")
        except Exception:
            self._conn.execute("ROLLBACK")
            raise

    def _get_existing_node_ids(self, ids: list[str]) -> set[str]:
        if not ids:
            return set()
        placeholders = ",".join("?" * len(ids))
        rows = self._conn.execute(
            f"SELECT id FROM nodes WHERE id IN ({placeholders})", ids
        ).fetchall()
        return {r["id"] for r in rows}

    def get_outgoing_edges(self, source_id: str, kinds: Optional[list[str]] = None) -> list[Edge]:
        sql = "SELECT * FROM edges WHERE source = ?"
        params: list[Any] = [source_id]
        if kinds:
            placeholders = ",".join("?" * len(kinds))
            sql += f" AND kind IN ({placeholders})"
            params.extend(kinds)
        rows = self._conn.execute(sql, params).fetchall()
        return [_row_to_edge(r) for r in rows]

    def get_incoming_edges(self, target_id: str, kinds: Optional[list[str]] = None) -> list[Edge]:
        sql = "SELECT * FROM edges WHERE target = ?"
        params: list[Any] = [target_id]
        if kinds:
            placeholders = ",".join("?" * len(kinds))
            sql += f" AND kind IN ({placeholders})"
            params.extend(kinds)
        rows = self._conn.execute(sql, params).fetchall()
        return [_row_to_edge(r) for r in rows]

    # =========================================================================
    # File operations (queries.ts:1674-1700)
    # =========================================================================

    def upsert_file(self, file: FileRecord) -> None:
        """Insert or update a file record (queries.ts:1674-1700)."""
        self._conn.execute(
            """INSERT INTO files (path, content_hash, language, size, modified_at, indexed_at, node_count, errors)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(path) DO UPDATE SET
                content_hash = excluded.content_hash,
                language = excluded.language,
                size = excluded.size,
                modified_at = excluded.modified_at,
                indexed_at = excluded.indexed_at,
                node_count = excluded.node_count,
                errors = excluded.errors""",
            (
                file.path, file.content_hash, file.language,
                file.size, file.modified_at, file.indexed_at,
                file.node_count,
                json.dumps(file.errors) if file.errors else None,
            ),
        )

    def get_file_by_path(self, file_path: str) -> Optional[FileRecord]:
        row = self._conn.execute("SELECT * FROM files WHERE path = ?", (file_path,)).fetchone()
        return _row_to_file(row) if row else None

    def get_all_files(self) -> list[FileRecord]:
        rows = self._conn.execute("SELECT * FROM files ORDER BY path").fetchall()
        return [_row_to_file(r) for r in rows]

    def delete_file(self, file_path: str) -> None:
        """Delete a file record and its nodes (cascade)."""
        self._conn.execute("BEGIN")
        try:
            self.delete_nodes_by_file(file_path)
            self._conn.execute("DELETE FROM files WHERE path = ?", (file_path,))
            self._conn.execute("COMMIT")
        except Exception:
            self._conn.execute("ROLLBACK")
            raise

    # =========================================================================
    # Unresolved reference operations (queries.ts:1766-1810)
    # =========================================================================

    def insert_unresolved_ref(self, ref: UnresolvedReference) -> None:
        """Insert an unresolved reference (queries.ts:1766-1784)."""
        self._conn.execute(
            """INSERT INTO unresolved_refs
                (from_node_id, reference_name, reference_kind, line, col, candidates, file_path, language)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                ref.from_node_id, ref.reference_name, ref.reference_kind,
                ref.line, ref.column,
                json.dumps(ref.candidates) if ref.candidates else None,
                ref.file_path or "",
                ref.language or "unknown",
            ),
        )

    def insert_unresolved_refs_batch(self, refs: list[UnresolvedReference]) -> None:
        if not refs:
            return
        self._conn.execute("BEGIN")
        try:
            for ref in refs:
                self.insert_unresolved_ref(ref)
            self._conn.execute("COMMIT")
        except Exception:
            self._conn.execute("ROLLBACK")
            raise

    def get_unresolved_by_name(self, name: str) -> list[UnresolvedReference]:
        rows = self._conn.execute(
            "SELECT * FROM unresolved_refs WHERE reference_name = ? AND status = 'pending'",
            (name,),
        ).fetchall()
        return [_row_to_unresolved_ref(r) for r in rows]

    def get_all_pending_refs(self) -> list[UnresolvedReference]:
        rows = self._conn.execute(
            "SELECT * FROM unresolved_refs WHERE status = 'pending'"
        ).fetchall()
        return [_row_to_unresolved_ref(r) for r in rows]

    def delete_unresolved_ref(self, ref_id: int) -> None:
        self._conn.execute("DELETE FROM unresolved_refs WHERE id = ?", (ref_id,))

    def mark_unresolved_failed(self, ref_id: int, name_tail: str) -> None:
        """Mark an unresolved ref as failed, keeping it for retry (queries.ts #1240)."""
        self._conn.execute(
            "UPDATE unresolved_refs SET status = 'failed', name_tail = ? WHERE id = ?",
            (name_tail, ref_id),
        )

    # =========================================================================
    # Project metadata
    # =========================================================================

    def set_metadata(self, key: str, value: str) -> None:
        self._conn.execute(
            """INSERT INTO project_metadata (key, value, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at""",
            (key, value, int(time.time() * 1000)),
        )

    def get_metadata(self, key: str) -> Optional[str]:
        row = self._conn.execute(
            "SELECT value FROM project_metadata WHERE key = ?", (key,)
        ).fetchone()
        return row["value"] if row else None

    # =========================================================================
    # FTS5 search
    # =========================================================================

    def search_nodes_fts(self, query: str, limit: int = 50) -> list[tuple[Node, float]]:
        """FTS5 BM25 search on node names/qualified_names/docstrings/signatures."""
        if not query or not query.strip():
            return []
        rows = self._conn.execute(
            """SELECT n.*, bm25(nodes_fts) as score
            FROM nodes_fts
            JOIN nodes n ON nodes_fts.id = n.id
            WHERE nodes_fts MATCH ?
            ORDER BY score
            LIMIT ?""",
            (query, limit),
        ).fetchall()
        return [(_row_to_node(r), r["score"]) for r in rows]

    # =========================================================================
    # Stats
    # =========================================================================

    def get_node_count(self) -> int:
        return self._conn.execute("SELECT COUNT(*) as c FROM nodes").fetchone()["c"]

    def get_edge_count(self) -> int:
        return self._conn.execute("SELECT COUNT(*) as c FROM edges").fetchone()["c"]

    def get_pending_ref_count(self) -> int:
        return self._conn.execute(
            "SELECT COUNT(*) as c FROM unresolved_refs WHERE status = 'pending'"
        ).fetchone()["c"]


# =============================================================================
# Row → dataclass converters
# =============================================================================

def _row_to_node(row: sqlite3.Row) -> Node:
    return Node(
        id=row["id"],
        kind=row["kind"],
        name=row["name"],
        qualified_name=row["qualified_name"],
        file_path=row["file_path"],
        language=row["language"],
        start_line=row["start_line"],
        end_line=row["end_line"],
        start_column=row["start_column"],
        end_column=row["end_column"],
        docstring=row["docstring"],
        signature=row["signature"],
        visibility=row["visibility"],
        is_exported=bool(row["is_exported"]),
        is_async=bool(row["is_async"]),
        is_static=bool(row["is_static"]),
        is_abstract=bool(row["is_abstract"]),
        decorators=json.loads(row["decorators"]) if row["decorators"] else None,
        type_parameters=json.loads(row["type_parameters"]) if row["type_parameters"] else None,
        return_type=row["return_type"],
        updated_at=row["updated_at"],
    )


def _row_to_edge(row: sqlite3.Row) -> Edge:
    return Edge(
        source=row["source"],
        target=row["target"],
        kind=row["kind"],
        metadata=json.loads(row["metadata"]) if row["metadata"] else None,
        line=row["line"],
        column=row["col"],
        provenance=row["provenance"],
    )


def _row_to_file(row: sqlite3.Row) -> FileRecord:
    return FileRecord(
        path=row["path"],
        content_hash=row["content_hash"],
        language=row["language"],
        size=row["size"],
        modified_at=row["modified_at"],
        indexed_at=row["indexed_at"],
        node_count=row["node_count"],
        errors=json.loads(row["errors"]) if row["errors"] else None,
    )


def _row_to_unresolved_ref(row: sqlite3.Row) -> UnresolvedReference:
    return UnresolvedReference(
        id=row["id"],
        from_node_id=row["from_node_id"],
        reference_name=row["reference_name"],
        reference_kind=row["reference_kind"],
        line=row["line"],
        column=row["col"],
        file_path=row["file_path"],
        language=row["language"],
        candidates=json.loads(row["candidates"]) if row["candidates"] else None,
        status=row["status"],
        name_tail=row["name_tail"],
    )
