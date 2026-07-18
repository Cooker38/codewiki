"""
Incremental Sync Reconciler — manages the full incremental sync pipeline.

M3: glue between git watcher, orchestrator, resolver, and synthesizer.
M4: adds write-lock concurrency control.
"""

from __future__ import annotations

import os
import time
import threading
from typing import TYPE_CHECKING, Optional

from codewiki.extraction.orchestrator import ExtractionOrchestrator, IndexResult
from codewiki.extraction.tree_sitter import extract_from_source
from codewiki.resolution.resolver import ReferenceResolver, ResolutionResult
from codewiki.resolution.callback_synthesizer import CallbackSynthesizer
from codewiki.sync.git_watcher import (
    get_changed_files,
    get_current_commit,
    get_last_indexed_commit,
    set_last_indexed_commit,
    is_ancestor,
    get_branch_name,
)

if TYPE_CHECKING:
    from codewiki.db.store import GraphStore

# Write lock for sync/concurrent control
_sync_lock = threading.Lock()
_syncing = False


def is_syncing() -> bool:
    """Check if a sync operation is in progress (M4)."""
    return _syncing


def sync(
    store: "GraphStore",
    repo_path: str,
    branch: Optional[str] = None,
    on_progress=None,
) -> IndexResult:
    """
    Incremental sync: only re-index changed files since last sync.

    Pipeline: detect changes → re-extract → resolve → synthesize → commit pointer.
    Non-ancestor commits trigger full rebuild.
    """
    global _syncing

    # Acquire write lock (non-blocking)
    if not _sync_lock.acquire(blocking=False):
        return IndexResult(
            success=False,
            errors=[{"message": "Sync in progress", "severity": "error", "filePath": ""}],
        )

    _syncing = True
    start_time = time.time()
    try:
        branch_name = branch or get_branch_name(repo_path)
        if not branch_name:
            return IndexResult(
                success=False,
                errors=[{"message": "Not a git repository", "severity": "error"}],
            )

        current_commit = get_current_commit(repo_path, branch_name)
        if not current_commit:
            return IndexResult(
                success=False,
                errors=[{"message": f"Cannot read branch: {branch_name}", "severity": "error"}],
            )

        last_commit = get_last_indexed_commit(store)

        # First sync: full build
        if not last_commit:
            orch = ExtractionOrchestrator(repo_path, store)
            result = orch.index_all(on_progress=on_progress)
            if result.success and result.files_indexed > 0:
                set_last_indexed_commit(store, current_commit)
                run_resolution(store, result.detected_frameworks)
            result.duration_ms = int((time.time() - start_time) * 1000)
            return result

        # No change
        if current_commit == last_commit:
            return IndexResult(success=True, duration_ms=int((time.time() - start_time) * 1000))

        # Non-ancestor → full rebuild
        if not is_ancestor(repo_path, last_commit, current_commit):
            orch = ExtractionOrchestrator(repo_path, store)
            result = orch.index_all(on_progress=on_progress)
            if result.success:
                set_last_indexed_commit(store, current_commit)
                run_resolution(store, result.detected_frameworks)
            result.duration_ms = int((time.time() - start_time) * 1000)
            return result

        # Incremental: only changed files
        changed = get_changed_files(repo_path, last_commit, current_commit)
        if not changed:
            return IndexResult(success=True, duration_ms=int((time.time() - start_time) * 1000))

        return _incremental_sync(store, repo_path, changed, current_commit, start_time, on_progress)

    finally:
        _syncing = False
        _sync_lock.release()


def _incremental_sync(
    store: "GraphStore",
    repo_path: str,
    changed_files: list[tuple[str, str]],
    current_commit: str,
    start_time: float,
    on_progress=None,
) -> IndexResult:
    """Process only changed files."""
    files_indexed = 0
    files_skipped = 0
    files_errored = 0
    total_nodes = 0
    total_edges = 0
    errors: list[dict] = []

    orch = ExtractionOrchestrator(repo_path, store)
    total = len(changed_files)
    processed = 0

    for status, file_path in changed_files:
        if on_progress:
            on_progress(processed, total, file_path)

        if status == "D":
            # Delete file and its cascade
            try:
                existing = store.get_file_by_path(file_path)
                if existing:
                    store.delete_file(file_path)
                files_indexed += 1
            except Exception as e:
                files_errored += 1
                errors.append({"message": str(e), "filePath": file_path, "severity": "error"})
        elif status in ("A", "M"):
            full_path = os.path.join(repo_path, file_path)
            if not os.path.isfile(full_path) or not file_path.endswith(".java"):
                files_skipped += 1
                processed += 1
                continue

            try:
                stat = os.stat(full_path)
                content = open(full_path, encoding="utf-8", errors="ignore").read()
                result = extract_from_source(file_path, content, "java")

                if result.nodes or not result.errors:
                    orch._store_extraction_result(file_path, content, "java", stat, result)

                if result.errors:
                    for err in result.errors:
                        errors.append({"message": err.message, "filePath": file_path, "severity": err.severity})

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
                errors.append({"message": str(e), "filePath": file_path, "severity": "error"})
        else:
            files_skipped += 1

        processed += 1

    # Run resolution + synthesis on changed files' affected refs
    frameworks = orch._detected_frameworks if hasattr(orch, '_detected_frameworks') else []
    run_resolution(store, frameworks)

    # Update commit pointer
    set_last_indexed_commit(store, current_commit)

    return IndexResult(
        success=files_errored == 0,
        files_indexed=files_indexed,
        files_skipped=files_skipped,
        files_errored=files_errored,
        nodes_created=total_nodes,
        edges_created=total_edges,
        errors=errors,
        duration_ms=int((time.time() - start_time) * 1000),
    )


def run_resolution(store: "GraphStore", frameworks: list[str]):
    """Run reference resolution + synthesis on the store."""
    resolver = ReferenceResolver(store, frameworks=frameworks)
    resolver.resolve_and_persist()
    synth = CallbackSynthesizer(store)
    synth.synthesize_all()
