"""
M3 verification tests: Full project indexing (Orchestrator + parse pool + framework detection).

Tests indexAll on the test fixture Java project to verify the complete pipeline:
scan → detect framework → parse → store to DB.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from codewiki.db.store import GraphStore
from codewiki.extraction.orchestrator import ExtractionOrchestrator, IndexResult
from codewiki.resolution.frameworks.java import detect_spring, detect_frameworks

FIXTURES = Path(__file__).parent / "fixtures" / "java_sample"


@pytest.fixture
def store():
    s = GraphStore(":memory:")
    s.init_schema()
    yield s
    s.close()


@pytest.fixture
def index_result(store):
    orch = ExtractionOrchestrator(str(FIXTURES), store)
    result = orch.index_all(pool_size=1)  # Single-threaded for deterministic tests
    return result


class TestScanDirectory:
    """Verify file scanning."""

    def test_finds_java_files(self, store):
        orch = ExtractionOrchestrator(str(FIXTURES), store)
        files = orch._scan_directory()
        # Should find all .java fixture files
        assert len(files) >= 5
        assert any("DiscountService.java" in f for f in files)
        assert any("Order.java" in f for f in files)
        assert any("OrderStatus.java" in f for f in files)

    def test_skips_build_dirs(self, store):
        orch = ExtractionOrchestrator(str(FIXTURES), store)
        files = orch._scan_directory()
        # No files from target/ or build/
        assert not any("target/" in f for f in files)
        assert not any("build/" in f for f in files)


class TestFrameworkDetection:
    """Verify Spring Boot detection."""

    def test_detects_spring(self, store):
        orch = ExtractionOrchestrator(str(FIXTURES), store)
        files = orch._scan_directory()
        frameworks = detect_frameworks(str(FIXTURES), files)
        assert "spring" in frameworks

    def test_detect_spring_via_annotation(self):
        # OrderService.java has @Service annotation
        files = ["OrderService.java"]
        assert detect_spring(str(FIXTURES), files) is True

    def test_no_spring_in_plain_project(self, tmp_path):
        # Create a plain Java file without Spring
        (tmp_path / "Plain.java").write_text("public class Plain { }", encoding="utf-8")
        assert detect_spring(str(tmp_path), ["Plain.java"]) is False


class TestIndexAll:
    """Verify full indexing pipeline."""

    def test_success(self, index_result):
        assert index_result.success is True
        assert index_result.files_errored == 0

    def test_files_indexed(self, index_result):
        assert index_result.files_indexed >= 5

    def test_nodes_created(self, index_result):
        assert index_result.nodes_created > 0
        # Each file should have at least a file node + namespace + class
        assert index_result.nodes_created >= 5 * 3  # 5 files × (file + namespace + class) minimum

    def test_edges_created(self, index_result):
        assert index_result.edges_created > 0

    def test_frameworks_detected(self, index_result):
        assert "spring" in index_result.detected_frameworks

    def test_duration_recorded(self, index_result):
        assert index_result.duration_ms >= 0


class TestDatabaseContent:
    """Verify DB has correct data after indexing."""

    def test_nodes_in_db(self, store, index_result):
        assert store.get_node_count() > 0

    def test_edges_in_db(self, store, index_result):
        assert store.get_edge_count() > 0

    def test_files_in_db(self, store, index_result):
        files = store.get_all_files()
        assert len(files) >= 5
        file_paths = {f.path for f in files}
        assert any("DiscountService.java" in p for p in file_paths)
        assert any("Order.java" in p for p in file_paths)

    def test_file_records_have_hash(self, store, index_result):
        files = store.get_all_files()
        for f in files:
            assert f.content_hash  # Non-empty hash
            assert f.language == "java"
            assert f.size > 0
            assert f.node_count > 0

    def test_contains_edges(self, store, index_result):
        # Verify file → namespace → class contains chain exists
        edges = store.conn.execute(
            "SELECT * FROM edges WHERE kind = 'contains' LIMIT 10"
        ).fetchall()
        assert len(edges) > 0

    def test_unresolved_refs_in_db(self, store, index_result):
        # After M3 (before M4 resolution), pending refs should exist
        pending = store.get_pending_ref_count()
        assert pending > 0, "Should have pending unresolved references (calls/imports/instantiates)"

    def test_cross_file_refs_exist(self, store, index_result):
        # DiscountService calls calculator.getRate, new Order, new Discount, etc.
        refs = store.conn.execute(
            "SELECT reference_name, reference_kind FROM unresolved_refs WHERE file_path LIKE '%DiscountService%'"
        ).fetchall()
        ref_names = {r["reference_name"] for r in refs}
        # Should have calls and instantiates
        assert any("getRate" in n for n in ref_names)
        assert any("Order" in n for n in ref_names)

    def test_import_refs_in_db(self, store, index_result):
        refs = store.conn.execute(
            "SELECT reference_name FROM unresolved_refs WHERE reference_kind = 'imports'"
        ).fetchall()
        import_names = {r["reference_name"] for r in refs}
        assert "java.util.List" in import_names
        assert "java.util.ArrayList" in import_names

    def test_name_segment_vocab_populated(self, store, index_result):
        count = store.conn.execute(
            "SELECT COUNT(*) as c FROM name_segment_vocab"
        ).fetchone()["c"]
        assert count > 0

    def test_metadata_stored(self, store, index_result):
        assert store.get_metadata("last_indexed_at") is not None
        assert store.get_metadata("detected_frameworks") is not None

    def test_fts_search_works(self, store, index_result):
        results = store.search_nodes_fts("DiscountService", limit=5)
        assert len(results) > 0
        assert any(r[0].name == "DiscountService" for r in results)

    def test_lombok_nodes_in_db(self, store, index_result):
        # Order.java has @Data → should have synthesized methods
        lombok_methods = store.conn.execute(
            """SELECT * FROM nodes WHERE decorators LIKE '%lombok%' LIMIT 5"""
        ).fetchall()
        assert len(lombok_methods) > 0, "Should have Lombok-synthesized methods"

    def test_idempotent_index(self, store):
        """Running indexAll twice should not duplicate data."""
        orch1 = ExtractionOrchestrator(str(FIXTURES), store)
        result1 = orch1.index_all(pool_size=1)

        node_count_1 = store.get_node_count()
        edge_count_1 = store.get_edge_count()

        orch2 = ExtractionOrchestrator(str(FIXTURES), store)
        result2 = orch2.index_all(pool_size=1)

        node_count_2 = store.get_node_count()
        edge_count_2 = store.get_edge_count()

        # Content hasn't changed, so node/edge counts should be the same
        # (files are deleted and re-inserted, but counts stay stable)
        assert node_count_1 == node_count_2, "Node count changed on re-index"
        assert edge_count_1 == edge_count_2, "Edge count changed on re-index"
