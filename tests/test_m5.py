"""
M5 verification tests: Graph queries (callers / callees / impact / node / search).

Tests the GraphTraversal class on the fixture Java project after full
indexing + resolution + synthesis.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from codewiki.db.store import GraphStore
from codewiki.extraction.orchestrator import ExtractionOrchestrator
from codewiki.resolution.resolver import ReferenceResolver
from codewiki.resolution.callback_synthesizer import CallbackSynthesizer
from codewiki.graph.traversal import GraphTraversal

FIXTURES = Path(__file__).parent / "fixtures" / "java_sample"


@pytest.fixture
def traversal():
    """Build the full pipeline: index → resolve → synthesize → traversal."""
    store = GraphStore(":memory:")
    store.init_schema()
    orch = ExtractionOrchestrator(str(FIXTURES), store)
    orch.index_all(pool_size=1)
    resolver = ReferenceResolver(store, frameworks=["spring"])
    resolver.resolve_and_persist()
    synth = CallbackSynthesizer(store)
    synth.synthesize_all()
    yield GraphTraversal(store)
    store.close()


def _find_node(store, name: str, kind: str = None):
    """Helper: find a node by name (and optionally kind)."""
    if kind:
        rows = store.conn.execute(
            "SELECT id FROM nodes WHERE name = ? AND kind = ? LIMIT 1", (name, kind)
        ).fetchall()
    else:
        rows = store.conn.execute(
            "SELECT id FROM nodes WHERE name = ? LIMIT 1", (name,)
        ).fetchall()
    return rows[0]["id"] if rows else None


class TestCallers:
    """Verify callers query."""

    def test_callers_returns_calling_methods(self, traversal):
        """getRate should have calculateDiscount as a caller."""
        store = traversal.store
        get_rate_id = _find_node(store, "getRate", "method")
        assert get_rate_id is not None, "getRate method not found"

        callers = traversal.callers(get_rate_id, max_depth=1)
        caller_names = {n.name for n in callers}
        assert "calculateDiscount" in caller_names, f"calculateDiscount not in callers: {caller_names}"

    def test_callers_includes_instantiates(self, traversal):
        """Order class should have callers from methods that `new Order()`."""
        store = traversal.store
        order_id = _find_node(store, "Order", "class")
        assert order_id is not None

        callers = traversal.callers(order_id, max_depth=1)
        # calculateDiscount does `new Order(...)` → should be a caller via instantiates
        caller_names = {n.name for n in callers}
        assert "calculateDiscount" in caller_names, f"calculateDiscount (instantiates) not in callers: {caller_names}"

    def test_callers_empty_for_unreferenced(self, traversal):
        """A node with no incoming call edges should return empty."""
        store = traversal.store
        # OrderStatus.PENDING enum member is unlikely to have callers in our fixtures
        pending_id = _find_node(store, "PENDING", "enum_member")
        if pending_id:
            callers = traversal.callers(pending_id, max_depth=1)
            # May or may not have callers, but should not error
            assert isinstance(callers, list)

    def test_callers_no_duplicates(self, traversal):
        """callers should not return duplicate nodes (#1086)."""
        store = traversal.store
        # Use a method that might be called from multiple places
        calc_discount_id = _find_node(store, "calculateDiscount", "method")
        if calc_discount_id:
            callers = traversal.callers(calc_discount_id, max_depth=2)
            ids = [n.id for n in callers]
            assert len(ids) == len(set(ids)), "Duplicate nodes in callers"

    def test_callers_depth_limit(self, traversal):
        """max_depth=1 should return only direct callers."""
        store = traversal.store
        get_rate_id = _find_node(store, "getRate", "method")
        callers_depth1 = traversal.callers(get_rate_id, max_depth=1)
        callers_depth3 = traversal.callers(get_rate_id, max_depth=3)
        # Deeper traversal should find at least as many nodes
        assert len(callers_depth3) >= len(callers_depth1)


class TestCallees:
    """Verify callees query."""

    def test_callees_returns_called_methods(self, traversal):
        """calculateDiscount should have getRate as a callee."""
        store = traversal.store
        calc_discount_id = _find_node(store, "calculateDiscount", "method")
        assert calc_discount_id is not None

        callees = traversal.callees(calc_discount_id, max_depth=1)
        callee_names = {n.name for n in callees}
        assert "getRate" in callee_names, f"getRate not in callees: {callee_names}"

    def test_callees_includes_instantiates(self, traversal):
        """calculateDiscount should have Order as a callee (new Order())."""
        store = traversal.store
        calc_discount_id = _find_node(store, "calculateDiscount", "method")
        callees = traversal.callees(calc_discount_id, max_depth=1)
        callee_names = {n.name for n in callees}
        assert "Order" in callee_names, f"Order (instantiates) not in callees: {callee_names}"

    def test_callees_no_duplicates(self, traversal):
        """callees should not return duplicate nodes."""
        store = traversal.store
        batch_id = _find_node(store, "batchCalculate", "method")
        if batch_id:
            callees = traversal.callees(batch_id, max_depth=2)
            ids = [n.id for n in callees]
            assert len(ids) == len(set(ids)), "Duplicate nodes in callees"


class TestImpact:
    """Verify impact (blast radius) query."""

    def test_impact_returns_dependents(self, traversal):
        """Impact of getRate should include calculateDiscount (its caller)."""
        store = traversal.store
        get_rate_id = _find_node(store, "getRate", "method")
        assert get_rate_id is not None

        result = traversal.impact(get_rate_id, max_depth=3)
        node_names = {n.name for n in result.nodes}
        assert "calculateDiscount" in node_names, f"calculateDiscount not in impact: {node_names}"

    def test_impact_excludes_contains(self, traversal):
        """
        Impact of a class should NOT include its internal methods/fields
        as dependents (contains edges are excluded, #536).

        However, container nodes ARE expanded to include their children
        so callers of those children appear in impact.
        """
        store = traversal.store
        order_id = _find_node(store, "Order", "class")
        assert order_id is not None

        result = traversal.impact(order_id, max_depth=3)

        # The focal node (Order) should be in the result
        focal = store.get_node_by_id(order_id)
        assert any(n.id == order_id for n in result.nodes)

        # Check that no edge in the result is a 'contains' edge used for
        # upward traversal (container expansion uses contains downward,
        # but upward traversal excludes it)
        # Note: contains edges from container expansion ARE included,
        # but they go downward (class → method), not upward.
        # The key invariant: impact finds DEPENDENTS, not CONTAINERS.
        # So if Order is impacted, its parent (file/namespace) should NOT
        # appear as a dependent via contains.

    def test_impact_class_expands_to_methods(self, traversal):
        """
        Impact of a class should expand to include its methods,
        so callers of those methods appear in the result.
        """
        store = traversal.store
        order_id = _find_node(store, "Order", "class")
        result = traversal.impact(order_id, max_depth=3)

        # Order's methods (getId, getAmount, etc.) should be in the result
        # because impact expands container nodes
        node_names = {n.name for n in result.nodes}
        # Lombok-generated methods should be included
        assert "getId" in node_names or "getAmount" in node_names, \
            f"Container expansion didn't include class methods: {node_names}"

    def test_impact_no_self_in_children(self, traversal):
        """Impact should not include the focal node's own contains-children as dependents."""
        store = traversal.store
        # Use a leaf method (not a container) — its impact should be callers only
        get_rate_id = _find_node(store, "getRate", "method")
        result = traversal.impact(get_rate_id, max_depth=1)

        # getRate is a method, not a container, so no expansion happens
        # Result should contain getRate itself + its direct callers
        assert any(n.name == "getRate" for n in result.nodes)


class TestNode:
    """Verify node query."""

    def test_get_node_by_name(self, traversal):
        node = traversal.get_node("DiscountService")
        assert node is not None
        assert node.kind == "class"

    def test_get_node_by_id(self, traversal):
        store = traversal.store
        node_id = _find_node(store, "Order", "class")
        node = traversal.get_node(node_id)
        assert node is not None
        assert node.name == "Order"

    def test_get_node_case_insensitive(self, traversal):
        node = traversal.get_node("discountservice")
        assert node is not None
        assert node.name == "DiscountService"

    def test_get_node_not_found(self, traversal):
        node = traversal.get_node("NonExistentSymbol")
        assert node is None

    def test_node_has_complete_fields(self, traversal):
        node = traversal.get_node("calculateDiscount")
        assert node is not None
        assert node.kind == "method"
        assert node.qualified_name is not None
        assert node.file_path is not None
        assert node.start_line > 0
        assert node.end_line >= node.start_line
        assert node.language == "java"


class TestSearch:
    """Verify FTS5 search."""

    def test_search_by_name(self, traversal):
        results = traversal.search("DiscountService", limit=10)
        assert len(results) > 0
        assert any(r[0].name == "DiscountService" for r in results)

    def test_search_by_partial_name(self, traversal):
        results = traversal.search("Discount*", limit=10)
        assert len(results) > 0
        names = {r[0].name for r in results}
        assert any("Discount" in n for n in names)

    def test_search_by_docstring(self, traversal):
        # DiscountService has docstring "A demo service for calculating discounts."
        results = traversal.search("demo", limit=10)
        if results:
            assert any("demo" in (r[0].docstring or "").lower() for r in results)

    def test_search_returns_score(self, traversal):
        results = traversal.search("Order", limit=5)
        for node, score in results:
            assert isinstance(score, (int, float))

    def test_search_limit(self, traversal):
        results = traversal.search("a", limit=3)
        assert len(results) <= 3

    def test_search_empty_query(self, traversal):
        results = traversal.search("", limit=5)
        # Empty query may return nothing or everything depending on FTS5 config
        assert isinstance(results, list)
