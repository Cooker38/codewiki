"""
M1 verification tests: types + schema + store + helpers + identifier segments.

Run: python -m pytest tests/test_m1.py -v
"""

import pytest

from codewiki.types import Edge, FileRecord, Node, UnresolvedReference
from codewiki.db.store import GraphStore
from codewiki.extraction.tree_sitter_helpers import generate_node_id
from codewiki.search.identifier_segments import split_identifier_segments


class TestNodeID:
    """Verify node ID generation (tree-sitter-helpers.ts:18-30)."""

    def test_id_format(self):
        nid = generate_node_id("src/Main.java", "class", "Main", 10)
        assert nid.startswith("class:")
        assert len(nid) == len("class:") + 32  # kind: + 32 hex chars

    def test_id_deterministic(self):
        nid1 = generate_node_id("src/Main.java", "class", "Main", 10)
        nid2 = generate_node_id("src/Main.java", "class", "Main", 10)
        assert nid1 == nid2

    def test_id_changes_with_line(self):
        nid1 = generate_node_id("src/Main.java", "class", "Main", 10)
        nid2 = generate_node_id("src/Main.java", "class", "Main", 11)
        assert nid1 != nid2

    def test_id_changes_with_kind(self):
        nid1 = generate_node_id("src/Main.java", "class", "Main", 10)
        nid2 = generate_node_id("src/Main.java", "method", "Main", 10)
        assert nid1 != nid2


class TestIdentifierSegments:
    """Verify identifier segment splitting (identifier-segments.ts:30-47)."""

    def test_pascal_case(self):
        assert set(split_identifier_segments("OrderStateMachine")) == {"order", "state", "machine"}

    def test_acronym_run(self):
        # HTMLParser -> html, parser
        segs = set(split_identifier_segments("HTMLParser"))
        assert "html" in segs
        assert "parser" in segs

    def test_camel_case(self):
        segs = set(split_identifier_segments("base64Encode"))
        assert "base64" in segs
        assert "encode" in segs

    def test_snake_case(self):
        segs = set(split_identifier_segments("calculate_total_price"))
        assert "calculate" in segs
        assert "total" in segs
        assert "price" in segs

    def test_empty(self):
        assert split_identifier_segments("") == []

    def test_digit_only_dropped(self):
        segs = split_identifier_segments("123456")
        assert segs == []


class TestSchema:
    """Verify schema creation."""

    def test_all_tables_exist(self):
        store = GraphStore(":memory:")
        store.init_schema()
        tables = {
            r[0] for r in store.conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        for expected in ["nodes", "edges", "files", "unresolved_refs",
                         "name_segment_vocab", "project_metadata", "nodes_fts"]:
            assert expected in tables, f"Missing table: {expected}"
        store.close()

    def test_triggers_exist(self):
        store = GraphStore(":memory:")
        store.init_schema()
        triggers = {
            r[0] for r in store.conn.execute(
                "SELECT name FROM sqlite_master WHERE type='trigger'"
            ).fetchall()
        }
        for expected in ["nodes_ai", "nodes_ad", "nodes_au"]:
            assert expected in triggers, f"Missing trigger: {expected}"
        store.close()

    def test_edge_unique_index_exists(self):
        store = GraphStore(":memory:")
        store.init_schema()
        indexes = {
            r[0] for r in store.conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index'"
            ).fetchall()
        }
        assert "idx_edges_identity" in indexes
        store.close()


class TestStore:
    """Verify store CRUD operations."""

    @pytest.fixture
    def store(self):
        s = GraphStore(":memory:")
        s.init_schema()
        yield s
        s.close()

    def test_insert_and_get_node(self, store):
        nid = generate_node_id("src/Main.java", "class", "Main", 5)
        node = Node(
            id=nid, kind="class", name="Main", qualified_name="src/Main.java::Main",
            file_path="src/Main.java", language="java",
            start_line=5, end_line=20, start_column=0, end_column=0,
            updated_at=1000000,
            signature="public class Main",
            visibility="public",
            is_exported=True,
        )
        store.insert_node(node)
        retrieved = store.get_node_by_id(nid)
        assert retrieved is not None
        assert retrieved.name == "Main"
        assert retrieved.kind == "class"
        assert retrieved.visibility == "public"
        assert retrieved.is_exported is True

    def test_name_segment_vocab_populated(self, store):
        nid = generate_node_id("src/Foo.java", "class", "OrderStateMachine", 1)
        node = Node(
            id=nid, kind="class", name="OrderStateMachine",
            qualified_name="src/Foo.java::OrderStateMachine",
            file_path="src/Foo.java", language="java",
            start_line=1, end_line=10, start_column=0, end_column=0,
            updated_at=1000000,
        )
        store.insert_node(node)
        rows = store.conn.execute(
            "SELECT segment FROM name_segment_vocab WHERE name = ?", ("OrderStateMachine",)
        ).fetchall()
        segments = {r["segment"] for r in rows}
        assert "order" in segments
        assert "state" in segments
        assert "machine" in segments

    def test_file_node_not_segmented(self, store):
        nid = generate_node_id("src/Main.java", "file", "Main.java", 0)
        node = Node(
            id=nid, kind="file", name="Main.java",
            qualified_name="src/Main.java",
            file_path="src/Main.java", language="java",
            start_line=0, end_line=50, start_column=0, end_column=0,
            updated_at=1000000,
        )
        store.insert_node(node)
        count = store.conn.execute(
            "SELECT COUNT(*) as c FROM name_segment_vocab WHERE name = ?", ("Main.java",)
        ).fetchone()["c"]
        assert count == 0, "file nodes should not contribute to segment vocab"

    def test_fts_search(self, store):
        nid = generate_node_id("src/Foo.java", "class", "DiscountCalculator", 3)
        node = Node(
            id=nid, kind="class", name="DiscountCalculator",
            qualified_name="src/Foo.java::DiscountCalculator",
            file_path="src/Foo.java", language="java",
            start_line=3, end_line=30, start_column=0, end_column=0,
            updated_at=1000000,
            docstring="Calculates discounts for orders",
        )
        store.insert_node(node)
        results = store.search_nodes_fts("DiscountCalculator")
        assert len(results) > 0
        assert results[0][0].name == "DiscountCalculator"

    def test_insert_and_get_edge(self, store):
        # Create two nodes
        nid1 = generate_node_id("src/A.java", "class", "A", 1)
        nid2 = generate_node_id("src/B.java", "class", "B", 1)
        for nid, name, path in [(nid1, "A", "src/A.java"), (nid2, "B", "src/B.java")]:
            store.insert_node(Node(
                id=nid, kind="class", name=name,
                qualified_name=f"{path}::{name}",
                file_path=path, language="java",
                start_line=1, end_line=10, start_column=0, end_column=0,
                updated_at=1000000,
            ))

        edge = Edge(source=nid1, target=nid2, kind="calls", line=5, column=10,
                     provenance="tree-sitter")
        store.insert_edge(edge)

        outgoing = store.get_outgoing_edges(nid1)
        assert len(outgoing) == 1
        assert outgoing[0].kind == "calls"
        assert outgoing[0].target == nid2

        incoming = store.get_incoming_edges(nid2)
        assert len(incoming) == 1
        assert incoming[0].source == nid1

    def test_edge_dedup(self, store):
        nid1 = generate_node_id("src/A.java", "class", "A", 1)
        nid2 = generate_node_id("src/B.java", "class", "B", 1)
        for nid, name, path in [(nid1, "A", "src/A.java"), (nid2, "B", "src/B.java")]:
            store.insert_node(Node(
                id=nid, kind="class", name=name,
                qualified_name=f"{path}::{name}",
                file_path=path, language="java",
                start_line=1, end_line=10, start_column=0, end_column=0,
                updated_at=1000000,
            ))

        edge = Edge(source=nid1, target=nid2, kind="calls", line=5, column=10)
        store.insert_edge(edge)
        store.insert_edge(edge)  # duplicate
        count = store.conn.execute(
            "SELECT COUNT(*) as c FROM edges WHERE source = ? AND target = ? AND kind = ?",
            (nid1, nid2, "calls")
        ).fetchone()["c"]
        assert count == 1, "duplicate edge should be ignored by INSERT OR IGNORE"

    def test_unresolved_ref_lifecycle(self, store):
        nid = generate_node_id("src/A.java", "method", "foo", 5)
        store.insert_node(Node(
            id=nid, kind="method", name="foo",
            qualified_name="src/A.java::A.foo",
            file_path="src/A.java", language="java",
            start_line=5, end_line=10, start_column=0, end_column=0,
            updated_at=1000000,
        ))

        ref = UnresolvedReference(
            from_node_id=nid, reference_name="Bar.baz",
            reference_kind="calls", line=7, column=5,
            file_path="src/A.java", language="java",
        )
        store.insert_unresolved_ref(ref)

        pending = store.get_all_pending_refs()
        assert len(pending) == 1
        assert pending[0].reference_name == "Bar.baz"
        assert store.get_pending_ref_count() == 1

        # Mark failed
        store.mark_unresolved_failed(pending[0].id, "baz")
        assert store.get_pending_ref_count() == 0

    def test_upsert_file(self, store):
        f = FileRecord(
            path="src/Main.java", content_hash="abc123", language="java",
            size=500, modified_at=1000, indexed_at=2000, node_count=5,
        )
        store.upsert_file(f)
        retrieved = store.get_file_by_path("src/Main.java")
        assert retrieved is not None
        assert retrieved.content_hash == "abc123"
        assert retrieved.node_count == 5

        # Update
        f2 = FileRecord(
            path="src/Main.java", content_hash="def456", language="java",
            size=600, modified_at=1000, indexed_at=3000, node_count=7,
        )
        store.upsert_file(f2)
        retrieved2 = store.get_file_by_path("src/Main.java")
        assert retrieved2.content_hash == "def456"
        assert retrieved2.node_count == 7

    def test_cascade_delete(self, store):
        nid = generate_node_id("src/A.java", "class", "A", 1)
        store.insert_node(Node(
            id=nid, kind="class", name="A",
            qualified_name="src/A.java::A",
            file_path="src/A.java", language="java",
            start_line=1, end_line=10, start_column=0, end_column=0,
            updated_at=1000000,
        ))
        store.insert_unresolved_ref(UnresolvedReference(
            from_node_id=nid, reference_name="External.thing",
            reference_kind="calls", line=5, column=0,
            file_path="src/A.java", language="java",
        ))

        assert store.get_pending_ref_count() == 1
        store.delete_nodes_by_file("src/A.java")
        # Cascade should delete the unresolved_ref
        assert store.get_pending_ref_count() == 0

    def test_metadata(self, store):
        store.set_metadata("last_indexed_commit", "abc123def")
        assert store.get_metadata("last_indexed_commit") == "abc123def"

    def test_idempotent_insert(self, store):
        nid = generate_node_id("src/A.java", "class", "A", 1)
        node = Node(
            id=nid, kind="class", name="A",
            qualified_name="src/A.java::A",
            file_path="src/A.java", language="java",
            start_line=1, end_line=10, start_column=0, end_column=0,
            updated_at=1000000,
        )
        store.insert_node(node)
        store.insert_node(node)
        assert store.get_node_count() == 1
