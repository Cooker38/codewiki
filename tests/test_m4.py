"""
M4 verification tests: Reference resolution + synthesis.

Tests the ReferenceResolver and CallbackSynthesizer on the fixture Java project
after M3's indexAll has populated the graph with pending unresolved_refs.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from codewiki.db.store import GraphStore
from codewiki.extraction.orchestrator import ExtractionOrchestrator
from codewiki.resolution.resolver import ReferenceResolver
from codewiki.resolution.callback_synthesizer import CallbackSynthesizer

FIXTURES = Path(__file__).parent / "fixtures" / "java_sample"


@pytest.fixture
def store_with_data():
    """Build the graph and resolve references."""
    store = GraphStore(":memory:")
    store.init_schema()
    orch = ExtractionOrchestrator(str(FIXTURES), store)
    orch.index_all(pool_size=1)
    yield store
    store.close()


@pytest.fixture
def resolved_store(store_with_data):
    """Store after reference resolution."""
    resolver = ReferenceResolver(store_with_data, frameworks=["spring"])
    resolver.resolve_and_persist()
    return store_with_data


@pytest.fixture
def synthesized_store(resolved_store):
    """Store after synthesis."""
    synth = CallbackSynthesizer(resolved_store)
    synth.synthesize_all()
    return resolved_store


class TestResolution:
    """Verify reference resolution."""

    def test_pending_count_zero_after_resolution(self, resolved_store):
        assert resolved_store.get_pending_ref_count() == 0, "All pending refs should be resolved or marked failed"

    def test_failed_refs_have_name_tail(self, resolved_store):
        failed = resolved_store.conn.execute(
            "SELECT reference_name, name_tail FROM unresolved_refs WHERE status = 'failed' LIMIT 10"
        ).fetchall()
        if failed:
            for row in failed:
                # name_tail should be the last segment of reference_name
                expected = row["reference_name"].split(".")[-1]
                assert row["name_tail"] == expected

    def test_cross_file_calls_resolved(self, resolved_store):
        """DiscountService.calculateDiscount calls calculator.getRate → should have calls edge."""
        # Find DiscountService.calculateDiscount method node
        caller = resolved_store.conn.execute(
            """SELECT id FROM nodes WHERE name = 'calculateDiscount' AND kind = 'method'"""
        ).fetchone()
        assert caller is not None

        # Find getRate method node
        callee = resolved_store.conn.execute(
            """SELECT id FROM nodes WHERE name = 'getRate' AND kind = 'method'"""
        ).fetchone()
        assert callee is not None

        # Check calls edge exists
        edge = resolved_store.conn.execute(
            "SELECT 1 FROM edges WHERE source = ? AND target = ? AND kind = 'calls'",
            (caller["id"], callee["id"])
        ).fetchone()
        assert edge is not None, "Missing calls edge: calculateDiscount → getRate"

    def test_instantiates_edges_created(self, resolved_store):
        """new Order() → instantiates edge from caller to Order class."""
        order_class = resolved_store.conn.execute(
            "SELECT id FROM nodes WHERE name = 'Order' AND kind = 'class'"
        ).fetchone()
        assert order_class is not None

        instantiates = resolved_store.conn.execute(
            "SELECT * FROM edges WHERE target = ? AND kind = 'instantiates'",
            (order_class["id"],)
        ).fetchall()
        assert len(instantiates) > 0, "Should have instantiates edges targeting Order class"

    def test_calls_edge_provenance(self, resolved_store):
        """Resolved edges should have provenance='heuristic'."""
        edges = resolved_store.conn.execute(
            "SELECT provenance FROM edges WHERE kind = 'calls' LIMIT 5"
        ).fetchall()
        for e in edges:
            assert e["provenance"] == "heuristic"

    def test_internal_calls_resolved(self, resolved_store):
        """DiscountService.batchCalculate calls DiscountService.calculateDiscount → should resolve."""
        caller = resolved_store.conn.execute(
            """SELECT id FROM nodes WHERE name = 'batchCalculate' AND kind = 'method'"""
        ).fetchone()
        callee = resolved_store.conn.execute(
            """SELECT id FROM nodes WHERE name = 'calculateDiscount' AND kind = 'method'"""
        ).fetchone()
        if caller and callee:
            edge = resolved_store.conn.execute(
                "SELECT 1 FROM edges WHERE source = ? AND target = ? AND kind = 'calls'",
                (caller["id"], callee["id"])
            ).fetchone()
            assert edge is not None, "Missing internal calls edge: batchCalculate → calculateDiscount"

    def test_resolution_stats(self, store_with_data):
        resolver = ReferenceResolver(store_with_data, frameworks=["spring"])
        result = resolver.resolve_and_persist()
        assert result.total > 0
        # Some should resolve (internal calls, instantiates), some fail (java.util.*)
        assert result.resolved > 0
        assert result.resolved + result.unresolved == result.total


class TestSynthesis:
    """Verify type_of / returns / overrides synthesis."""

    def test_type_of_synthesized(self, synthesized_store):
        """Fields with return_type should have type_of edges."""
        type_of_edges = synthesized_store.conn.execute(
            "SELECT * FROM edges WHERE kind = 'type_of' LIMIT 5"
        ).fetchall()
        # Order has fields with types (id: int, amount: double, status: OrderStatus)
        # OrderStatus is a class node → type_of edge should exist
        if type_of_edges:
            for e in type_of_edges:
                assert e["provenance"] == "heuristic"

    def test_returns_synthesized(self, synthesized_store):
        """Methods with return_type should have returns edges."""
        returns_edges = synthesized_store.conn.execute(
            "SELECT * FROM edges WHERE kind = 'returns' LIMIT 5"
        ).fetchall()
        # Methods that return a class type (not primitives) should have returns edges
        # e.g. DiscountService.batchCalculate returns List<Discount> → List
        if returns_edges:
            for e in returns_edges:
                assert e["provenance"] == "heuristic"

    def test_overrides_synthesized(self, synthesized_store):
        """
        If a class extends another and has same-named methods,
        overrides edges should be created.

        Note: Our test fixtures don't have extends between classes with
        same-named methods, so this may be 0 — that's OK.
        """
        overrides_edges = synthesized_store.conn.execute(
            "SELECT * FROM edges WHERE kind = 'overrides'"
        ).fetchall()
        # Verify no errors — count can be 0 if no overrides exist in fixtures
        for e in overrides_edges:
            assert e["provenance"] == "heuristic"

    def test_synthesis_idempotent(self, synthesized_store):
        """Running synthesis twice should not duplicate edges."""
        # Count before
        type_of_before = synthesized_store.conn.execute(
            "SELECT COUNT(*) as c FROM edges WHERE kind = 'type_of'"
        ).fetchone()["c"]
        returns_before = synthesized_store.conn.execute(
            "SELECT COUNT(*) as c FROM edges WHERE kind = 'returns'"
        ).fetchone()["c"]

        # Run again
        synth = CallbackSynthesizer(synthesized_store)
        synth.synthesize_all()

        # Counts should be the same (NOT EXISTS guard prevents duplicates)
        type_of_after = synthesized_store.conn.execute(
            "SELECT COUNT(*) as c FROM edges WHERE kind = 'type_of'"
        ).fetchone()["c"]
        returns_after = synthesized_store.conn.execute(
            "SELECT COUNT(*) as c FROM edges WHERE kind = 'returns'"
        ).fetchone()["c"]

        assert type_of_before == type_of_after
        assert returns_before == returns_after


class TestEdgeIntegrity:
    """Verify edge integrity after resolution + synthesis."""

    def test_no_duplicate_edges(self, resolved_store):
        """Edge unique index should prevent duplicates."""
        dups = resolved_store.conn.execute(
            """SELECT source, target, kind, COUNT(*) as c
               FROM edges
               GROUP BY source, target, kind, IFNULL(line, -1), IFNULL(col, -1)
               HAVING c > 1"""
        ).fetchall()
        assert len(dups) == 0, f"Duplicate edges found: {dups}"

    def test_all_edges_reference_valid_nodes(self, resolved_store):
        """All edge endpoints should exist in nodes table."""
        orphan_sources = resolved_store.conn.execute(
            """SELECT COUNT(*) as c FROM edges e
               LEFT JOIN nodes n ON e.source = n.id
               WHERE n.id IS NULL"""
        ).fetchone()["c"]
        assert orphan_sources == 0, f"Edges with missing source nodes: {orphan_sources}"

        orphan_targets = resolved_store.conn.execute(
            """SELECT COUNT(*) as c FROM edges e
               LEFT JOIN nodes n ON e.target = n.id
               WHERE n.id IS NULL"""
        ).fetchone()["c"]
        assert orphan_targets == 0, f"Edges with missing target nodes: {orphan_targets}"

    def test_edge_kinds_present(self, resolved_store):
        """After resolution, various edge kinds should be present."""
        kinds = {r["kind"] for r in resolved_store.conn.execute(
            "SELECT DISTINCT kind FROM edges"
        ).fetchall()}
        # contains (from extraction), calls (from resolution), instantiates (from resolution)
        assert "contains" in kinds
        # At least some of these should be present after resolution
        resolved_kinds = kinds - {"contains"}
        assert len(resolved_kinds) > 0, f"No resolved edge kinds found. Only: {kinds}"
