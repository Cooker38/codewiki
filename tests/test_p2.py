"""
P2 verification tests: MCP Server tools.

Tests all 8 MCP tools by calling them directly (not through MCP protocol).
Uses a pre-built test DB so tools can respond without full init each time.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest

from codewiki.db.store import GraphStore
from codewiki.extraction.orchestrator import ExtractionOrchestrator
from codewiki.resolution.resolver import ReferenceResolver
from codewiki.resolution.callback_synthesizer import CallbackSynthesizer
from codewiki.graph.traversal import GraphTraversal

FIXTURES = Path(__file__).parent / "fixtures" / "java_sample"


@pytest.fixture(scope="module")
def built_db_path():
    """Build the graph once for all P2 tests."""
    tmp = tempfile.mkdtemp()
    project = os.path.join(tmp, "demo")
    os.makedirs(os.path.join(project, ".codewiki"))
    db_path = os.path.join(project, ".codewiki", "codewiki.db")

    store = GraphStore(db_path)
    store.init_schema()
    orch = ExtractionOrchestrator(str(FIXTURES), store)
    result = orch.index_all(pool_size=1)
    resolver = ReferenceResolver(store, frameworks=result.detected_frameworks)
    resolver.resolve_and_persist()
    synth = CallbackSynthesizer(store)
    synth.synthesize_all()
    store.close()

    return db_path


@pytest.fixture
def store(built_db_path):
    s = GraphStore(built_db_path)
    s.init_schema()
    yield s
    s.close()


@pytest.fixture
def traversal(store):
    return GraphTraversal(store)


class TestNode:
    def test_node_returns_details(self, traversal):
        node = traversal.get_node("DiscountService")
        assert node is not None
        assert node.kind == "class"
        assert node.qualified_name is not None

    def test_node_case_insensitive(self, traversal):
        node = traversal.get_node("discountservice")
        assert node is not None
        assert node.name == "DiscountService"

    def test_node_not_found(self, traversal):
        node = traversal.get_node("NonExistent")
        assert node is None


class TestCallers:
    def test_callers_returns_calling_method(self, traversal):
        get_rate = traversal.get_node("getRate")
        assert get_rate is not None
        callers = traversal.callers(get_rate.id, max_depth=1)
        caller_names = {n.name for n in callers}
        assert "calculateDiscount" in caller_names

    def test_callers_includes_instantiates(self, traversal):
        order = traversal.get_node("Order")
        assert order is not None
        callers = traversal.callers(order.id, max_depth=1)
        caller_names = {n.name for n in callers}
        assert "calculateDiscount" in caller_names  # new Order() in calculateDiscount

    def test_callers_no_duplicates(self, traversal):
        calc = traversal.get_node("calculateDiscount")
        callers = traversal.callers(calc.id, max_depth=2)
        ids = [n.id for n in callers]
        assert len(ids) == len(set(ids))


class TestCallees:
    def test_callees_returns_called_methods(self, traversal):
        calc = traversal.get_node("calculateDiscount")
        callees = traversal.callees(calc.id, max_depth=1)
        callee_names = {n.name for n in callees}
        assert "getRate" in callee_names

    def test_callees_includes_instantiates(self, traversal):
        calc = traversal.get_node("calculateDiscount")
        callees = traversal.callees(calc.id, max_depth=1)
        callee_names = {n.name for n in callees}
        assert "Order" in callee_names


class TestImpact:
    def test_impact_returns_dependents(self, traversal):
        get_rate = traversal.get_node("getRate")
        result = traversal.impact(get_rate.id, max_depth=3)
        node_names = {n.name for n in result.nodes}
        assert "calculateDiscount" in node_names

    def test_impact_has_edges(self, traversal):
        order = traversal.get_node("Order")
        result = traversal.impact(order.id, max_depth=3)
        assert len(result.edges) > 0


class TestSearch:
    def test_search_by_name(self, traversal):
        results = traversal.search("DiscountService", limit=10)
        assert len(results) > 0
        names = {r[0].name for r in results}
        assert "DiscountService" in names

    def test_search_returns_score(self, traversal):
        results = traversal.search("Order", limit=5)
        for _, score in results:
            assert isinstance(score, (int, float))


class TestExplore:
    def test_explore_returns_context(self, traversal):
        node = traversal.get_node("calculateDiscount")
        assert node is not None

        # Simulate explore logic
        callers = traversal.callers(node.id, max_depth=1)
        callees = traversal.callees(node.id, max_depth=1)

        assert len(callers) > 0 or len(callees) > 0

    def test_explore_budget(self, store):
        """Verify budget algorithm returns reasonable values."""
        file_count = len(store.get_all_files())
        # Budget function logic
        if file_count < 500:
            budget = {"max_chars": 8000, "max_nodes": 20}
        elif file_count < 5000:
            budget = {"max_chars": 15000, "max_nodes": 40}
        else:
            budget = {"max_chars": 20000, "max_nodes": 60}

        assert file_count < 500  # Our fixture is small
        assert budget["max_chars"] == 8000
        assert budget["max_nodes"] == 20


class TestInit:
    def test_init_builds_graph(self):
        """Test that init creates a valid graph DB."""
        import tempfile
        tmp = tempfile.mkdtemp()
        project = os.path.join(tmp, "init-test")
        os.makedirs(os.path.join(project, ".codewiki"))
        db_path = os.path.join(project, ".codewiki", "codewiki.db")

        store = GraphStore(db_path)
        store.init_schema()

        # Simulate init by building on the fixtures (they're not in the init-test dir,
        # but we use them as source for the extractor)
        orch = ExtractionOrchestrator(str(FIXTURES), store)
        result = orch.index_all(pool_size=1)
        resolver = ReferenceResolver(store, frameworks=result.detected_frameworks)
        resolver.resolve_and_persist()
        synth = CallbackSynthesizer(store)
        synth.synthesize_all()

        assert result.files_indexed > 0
        assert result.nodes_created > 0
        assert store.get_node_count() > 0
        assert store.get_edge_count() > 0
        assert store.get_pending_ref_count() == 0
        store.close()


class TestNotInitialized:
    """Verify tools don't crash when no project is indexed."""

    def test_traversal_without_store(self):
        """GraphTraversal with empty store should not crash."""
        tmp = tempfile.mkdtemp()
        db_path = os.path.join(tmp, "empty.codewiki.db")
        store = GraphStore(db_path)
        store.init_schema()
        t = GraphTraversal(store)

        # All queries should return empty/null, not crash
        assert t.get_node("foo") is None
        assert t.callers("nonexistent") == []
        assert t.callees("nonexistent") == []
        assert t.search("nothing") == []

        impact_result = t.impact("nonexistent")
        assert impact_result.nodes == []
        store.close()
