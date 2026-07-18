"""Shared project-root discovery + store-open helpers.

Used by both the CLI and the wiki module. Avoids circular imports by staying
at the utility level (only imports ``codewiki.db.store``).
"""
from __future__ import annotations

import os
from typing import Optional, Tuple

from codewiki.db.store import GraphStore


def find_project_root(start: Optional[str] = None) -> Optional[str]:
    """Walk upward from *start* (or cwd) looking for ``.codewiki/``.

    Returns the first parent directory that contains it, or ``None``.
    """
    p = os.path.abspath(start or ".")
    while True:
        if os.path.isdir(os.path.join(p, ".codewiki")):
            return p
        parent = os.path.dirname(p)
        if parent == p:  # filesystem root, no .codewiki/
            return None
        p = parent


def open_store(start: Optional[str] = None) -> Tuple[GraphStore, str]:
    """Open the CodeWiki graph store for the nearest ``.codewiki/`` root.

    Returns ``(store, root_dir)``. Raises ``FileNotFoundError`` if no
    ``.codewiki/`` directory or no ``codewiki.db`` is found.
    """
    root = start if start and os.path.isdir(start) else find_project_root(start)
    if not root:
        raise FileNotFoundError(
            "No CodeWiki project found (no .codewiki/ directory upward). "
            "Run `codewiki init` first."
        )
    db_path = os.path.join(root, ".codewiki", "codewiki.db")
    if not os.path.isfile(db_path):
        raise FileNotFoundError(
            f"No database at {db_path}. Run `codewiki init` first."
        )
    store = GraphStore(db_path)
    store.init_schema()
    return store, root
