"""
Parse Pool — Python translation of codegraph/src/extraction/parse-pool.ts.

Uses ProcessPoolExecutor for parallel tree-sitter parsing.
Each worker process initializes its own parser instance.
"""

from __future__ import annotations

import os
from concurrent.futures import ProcessPoolExecutor, Future
from typing import Optional

from codewiki.types import ExtractionResult


def _parse_file_worker(file_path: str, content: str, language: str) -> ExtractionResult:
    """
    Worker function executed in a subprocess.
    Imports tree-sitter locally so each process gets its own parser instance.
    """
    from codewiki.extraction.tree_sitter import extract_from_source
    return extract_from_source(file_path, content, language)


def resolve_parse_pool_size(explicit: Optional[int] = None, cpu_count: Optional[int] = None) -> int:
    """
    Resolve worker pool size (parse-pool.ts).
    Explicit override → use it. Otherwise clamp(cpu_count - 1, 1, 8).
    """
    if explicit is not None:
        return max(1, explicit)
    cpus = cpu_count or os.cpu_count() or 1
    return max(1, min(8, cpus - 1))


class ParsePool:
    """
    ProcessPoolExecutor wrapper for parallel file parsing (parse-pool.ts).

    Parses files concurrently across worker processes while maintaining
    deterministic in-order result collection (codegraph #1015).
    """

    def __init__(self, pool_size: Optional[int] = None):
        self._size = resolve_parse_pool_size(pool_size)
        self._pool: Optional[ProcessPoolExecutor] = None
        if self._size > 1:
            self._pool = ProcessPoolExecutor(max_workers=self._size)

    @property
    def size(self) -> int:
        return self._size

    def request_parse(self, file_path: str, content: str, language: str) -> ExtractionResult:
        """
        Submit a parse request. Returns ExtractionResult.
        If pool size is 1, parses in-process (no subprocess overhead).
        """
        if self._pool is None:
            return _parse_file_worker(file_path, content, language)
        future = self._pool.submit(_parse_file_worker, file_path, content, language)
        return future.result()

    def request_parse_async(self, file_path: str, content: str, language: str) -> Future:
        """Submit a parse request and return a Future (for out-of-order collection)."""
        if self._pool is None:
            # Single-threaded: return a completed Future-like object
            from concurrent.futures import Future as _F
            f: _F = _F()
            f.set_result(_parse_file_worker(file_path, content, language))
            return f
        return self._pool.submit(_parse_file_worker, file_path, content, language)

    def shutdown(self) -> None:
        if self._pool:
            self._pool.shutdown(wait=True)
            self._pool = None
