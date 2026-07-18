"""AGENTS.md / CLAUDE.md bridge for CodeWiki.

Injected by `codewiki init` (CLI) so the project's agent guidance points at the
`codewiki_explore` MCP tool and tells the agent that building/refreshing the
graph is done in a terminal — never via an MCP tool (the MCP surface exposes
only `codewiki_explore`).
"""

from __future__ import annotations

import os

_CODEWIKI_AGENTS_SECTION = """## CodeWiki

This project is indexed by **CodeWiki** — a code knowledge graph (MCP) engine.
**PREFER `codewiki_explore` OVER Grep/Read for ALL code lookup.** Only use Grep/Read
when you already know the exact file path or need a literal substring match inside a
known file.

### Rules (follow these, do not improvise with Grep)
- **To find a definition / what calls X / how X works / impact of X** → `codewiki_explore <symbol>`.
  It returns node details, verbatim source, callers, callees, and blast-radius impact in ONE call.
- **If you only have a keyword or fuzzy idea** → still call `codewiki_explore <keyword>`;
  it auto-suggests the closest matching symbols. Do NOT switch to Grep first.
- **Use Grep/Read ONLY for**: an exact literal string inside a file you already have the path to,
  or a one-line edit. Everything else → `codewiki_explore`.

### Tools available to you (MCP)
- **`codewiki_explore <symbol>`** — the ONLY code-lookup tool you have. Always try this FIRST.

### Building / refreshing the graph (NOT an MCP tool)
- The graph is built by a HUMAN in a terminal, not by you:
  `codewiki init <project_root>` (build) and `codewiki sync` (refresh after git commits).
- If `codewiki_explore` says "No code graph indexed", do NOT try to build it yourself — tell the
  user to run `codewiki init .` in the project directory.

### Examples
| Task | Do this |
|------|---------|
| Find what calls X | `codewiki_explore X` — check callers |
| Understand how X works | `codewiki_explore X` — read the source block |
| Trace a call chain | `codewiki_explore X` — check callees, then explore each |
| Impact / "what breaks" | `codewiki_explore X` — check callers for dependents |
| Only have a keyword | `codewiki_explore <keyword>` — pick a candidate it suggests |
"""


def inject_agents_md(project_root: str) -> None:
    """Append the CodeWiki guide section to the project's AGENTS.md or CLAUDE.md.

    Skipped if the section already exists (idempotent). Creates AGENTS.md if
    neither exists.
    """
    candidates = ["AGENTS.md", "AGENTS.txt", "CLAUDE.md"]
    existing = None
    for name in candidates:
        p = os.path.normpath(os.path.join(project_root, name))
        if os.path.isfile(p):
            existing = p
            break

    if existing:
        content = open(existing, encoding="utf-8", errors="ignore").read()
        if "## CodeWiki" not in content:
            with open(existing, "a", encoding="utf-8") as f:
                f.write("\n\n" + _CODEWIKI_AGENTS_SECTION)
    else:
        ag = os.path.normpath(os.path.join(project_root, "AGENTS.md"))
        # Ensure parent dir exists
        os.makedirs(os.path.dirname(ag) or project_root, exist_ok=True)
        with open(ag, "w", encoding="utf-8") as f:
            f.write(_CODEWIKI_AGENTS_SECTION)
