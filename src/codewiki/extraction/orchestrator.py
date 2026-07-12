"""
Extraction Orchestrator — Python translation of codegraph/src/extraction/index.ts.

Coordinates file scanning, framework detection, parallel parsing, and database storage.
Entry point for `codewiki init` (full build).
"""

from __future__ import annotations

import hashlib
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from codewiki.types import Edge, ExtractionResult, FileRecord, Node, UnresolvedReference
from codewiki.db.store import GraphStore
from codewiki.extraction.tree_sitter import extract_from_source
from codewiki.extraction.parse_pool import ParsePool
from codewiki.resolution.frameworks.java import detect_frameworks

# Skip files larger than this (bytes) — mirrors codegraph's MAX_FILE_SIZE
_MAX_FILE_SIZE = 2 * 1024 * 1024  # 2MB

# Store chunk size for bulk inserts (index.ts:2168)
_STORE_CHUNK = 2000


@dataclass
class IndexProgress:
    """Progress callback payload."""
    phase: str  # 'scanning' | 'parsing'
    current: int
    total: int
    current_file: Optional[str] = None


@dataclass
class IndexResult:
    """Result of indexAll (mirrors codegraph's IndexResult)."""
    success: bool
    files_indexed: int = 0
    files_skipped: int = 0
    files_errored: int = 0
    nodes_created: int = 0
    edges_created: int = 0
    errors: list[dict] = field(default_factory=list)
    duration_ms: int = 0
    detected_frameworks: list[str] = field(default_factory=list)


def hash_content(content: str) -> str:
    """SHA-256 content hash (index.ts:120-122)."""
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


class ExtractionOrchestrator:
    """
    Coordinates the full indexing pipeline (index.ts:1400+).

    scan → detect frameworks → parse (parallel) → store (in-order) → return stats
    """

    def __init__(self, root_dir: str, store: GraphStore):
        self.root_dir = os.path.abspath(root_dir)
        self.store = store
        self._detected_frameworks: list[str] = []

    def index_all(
        self,
        on_progress: Optional[Callable[[IndexProgress], None]] = None,
        pool_size: Optional[int] = None,
    ) -> IndexResult:
        """
        Index all Java files in the project (index.ts:1488 indexAll).
        """
        start_time = time.time()
        errors: list[dict] = []
        files_indexed = 0
        files_skipped = 0
        files_errored = 0
        total_nodes = 0
        total_edges = 0

        # Phase 1: Scan for files
        on_progress and on_progress(IndexProgress("scanning", 0, 0))
        files = self._scan_directory()
        total = len(files)

        # Phase 2: Detect frameworks
        self._detected_frameworks = detect_frameworks(self.root_dir, files)

        # Phase 3: Parse and store
        on_progress and on_progress(IndexProgress("parsing", 0, total))
        pool = ParsePool(pool_size)
        try:
            processed = 0
            for file_path in files:
                full_path = os.path.join(self.root_dir, file_path) if not os.path.isabs(file_path) else file_path
                rel_path = os.path.relpath(full_path, self.root_dir) if os.path.isabs(full_path) else file_path
                rel_path = rel_path.replace("\\", "/")

                try:
                    # Read file
                    stat = os.stat(full_path)
                    if stat.st_size > _MAX_FILE_SIZE:
                        files_skipped += 1
                        processed += 1
                        on_progress and on_progress(IndexProgress("parsing", processed, total, rel_path))
                        continue

                    content = Path(full_path).read_text(encoding="utf-8", errors="ignore")

                    # Parse
                    result = pool.request_parse(rel_path, content, "java")

                    # Store
                    if result.nodes or not result.errors:
                        self._store_extraction_result(
                            rel_path, content, "java", stat, result
                        )

                    if result.errors:
                        for err in result.errors:
                            errors.append({
                                "message": err.message,
                                "filePath": rel_path,
                                "severity": err.severity,
                            })

                    if result.nodes:
                        files_indexed += 1
                        total_nodes += len(result.nodes)
                        total_edges += len(result.edges)
                    elif result.errors:
                        files_errored += 1
                    else:
                        files_skipped += 1

                except Exception as e:
                    files_errored += 1
                    errors.append({
                        "message": str(e),
                        "filePath": rel_path,
                        "severity": "error",
                    })

                processed += 1
                on_progress and on_progress(IndexProgress("parsing", processed, total, rel_path))
        finally:
            pool.shutdown()

        # Store project metadata
        self.store.set_metadata("last_indexed_at", str(int(time.time() * 1000)))
        if self._detected_frameworks:
            self.store.set_metadata("detected_frameworks", ",".join(self._detected_frameworks))

        duration_ms = int((time.time() - start_time) * 1000)
        return IndexResult(
            success=files_errored == 0,
            files_indexed=files_indexed,
            files_skipped=files_skipped,
            files_errored=files_errored,
            nodes_created=total_nodes,
            edges_created=total_edges,
            errors=errors,
            duration_ms=duration_ms,
            detected_frameworks=self._detected_frameworks,
        )

    def _scan_directory(self) -> list[str]:
        """
        Scan for .java source files (index.ts:1167 scanDirectory).

        Uses git ls-files if available (respects .gitignore),
        falls back to filesystem walk.
        """
        # Try git ls-files first (respects .gitignore)
        git_files = self._get_git_visible_files()
        if git_files is not None:
            return [f for f in git_files if f.endswith(".java")]

        # Fallback: walk filesystem
        return self._walk_filesystem()

    def _get_git_visible_files(self) -> Optional[list[str]]:
        """Get visible files via `git ls-files` (respects .gitignore)."""
        import subprocess
        try:
            result = subprocess.run(
                ["git", "ls-files"],
                cwd=self.root_dir,
                capture_output=True,
                text=True,
                timeout=30,
            )
            if result.returncode == 0:
                files = [f.strip() for f in result.stdout.splitlines() if f.strip()]
                if files:
                    return files
                # git succeeded but returned nothing → fall through to walk
                return None
        except (FileNotFoundError, subprocess.TimeoutExpired):
            pass
        return None

    def _walk_filesystem(self) -> list[str]:
        """Walk filesystem for .java files (index.ts:1228 scanDirectoryWalk)."""
        files: list[str] = []
        skip_dirs = {".git", ".codewiki", "node_modules", "target", "build", ".gradle", "out", "bin"}

        for dirpath, dirnames, filenames in os.walk(self.root_dir):
            # Skip hidden and build directories
            dirnames[:] = [d for d in dirnames if d not in skip_dirs and not d.startswith(".")]

            for filename in filenames:
                if filename.endswith(".java"):
                    full_path = os.path.join(dirpath, filename)
                    rel_path = os.path.relpath(full_path, self.root_dir).replace("\\", "/")
                    files.append(rel_path)

        return sorted(files)

    def _store_extraction_result(
        self,
        file_path: str,
        content: str,
        language: str,
        stat: os.stat_result,
        result: ExtractionResult,
    ) -> None:
        """
        Store extraction result to DB (index.ts:2154 storeExtractionResult).

        Inserts nodes, edges, unresolved_refs, and the FileRecord.
        Handles content-hash change detection and cross-file edge resurrection.
        """
        content_hash = hash_content(content)

        # Check if file already exists and hasn't changed
        existing_file = self.store.get_file_by_path(file_path)
        if existing_file and existing_file.content_hash == content_hash:
            return  # No changes

        # Delete existing data for this file (cascade deletes nodes + edges + unresolved_refs)
        if existing_file:
            self.store.delete_file(file_path)

        # Filter valid nodes (index.ts:2205)
        valid_nodes = [
            n for n in result.nodes
            if n.id and n.kind and n.name and n.file_path and n.language
        ]

        # Insert nodes (chunked)
        for i in range(0, len(valid_nodes), _STORE_CHUNK):
            self.store.insert_nodes(valid_nodes[i:i + _STORE_CHUNK])

        # Filter and insert edges (only those referencing inserted nodes)
        if result.edges:
            inserted_ids = {n.id for n in valid_nodes}
            valid_edges = [
                e for e in result.edges
                if e.source in inserted_ids and e.target in inserted_ids
            ]
            self.store.insert_edges(valid_edges)

        # Insert unresolved references
        if result.unresolved_references:
            # Set file_path and language if not already set
            for ref in result.unresolved_references:
                if not ref.file_path:
                    ref.file_path = file_path
                if not ref.language or ref.language == "unknown":
                    ref.language = language
            self.store.insert_unresolved_refs_batch(result.unresolved_references)

        # Store file record
        file_record = FileRecord(
            path=file_path,
            content_hash=content_hash,
            language=language,
            size=stat.st_size,
            modified_at=int(stat.st_mtime * 1000),
            indexed_at=int(time.time() * 1000),
            node_count=len(valid_nodes),
            errors=[{"message": e.message, "severity": e.severity} for e in result.errors] if result.errors else None,
        )
        self.store.upsert_file(file_record)
