"""
CodeWiki CLI — typer-based command-line interface.

Quick start:
  codewiki config set --api-key <key>     One-time LLM setup
  codewiki init                           Build graph + generate wiki
  codewiki serve                          Start MCP server for AI agents
"""

from __future__ import annotations

import os
import sys
import threading
import time
from pathlib import Path
from typing import Optional

import typer

app = typer.Typer(
    name="codewiki",
    help="Code knowledge graph + Wiki generation tool.\n\n"
         "Quick start:\n"
         "  codewiki config set --api-key <key>   Set up LLM (one-time)\n"
         "  codewiki init                         Build graph + wiki\n"
         "  codewiki serve                        Start MCP server for AI agents",
    no_args_is_help=True,
    invoke_without_command=True,
)

# Legacy wiki sub-group (wiki sync only; wiki init is now part of `codewiki init`)
wiki_app = typer.Typer(help="Wiki incremental sync (`codewiki wiki sync`) — LLM config from global ~/.codewiki/")
app.add_typer(wiki_app, name="wiki")


def _extract_source_block(node, root: str, max_lines: int = 100) -> Optional[str]:
    """Read a node's source span from disk (CodeGraph parity: return verbatim
    source so callers/agents don't need a separate Read call)."""
    if not root or not node.file_path:
        return None
    file_path = os.path.join(root, node.file_path)
    if not os.path.isfile(file_path):
        return None
    try:
        with open(file_path, encoding="utf-8", errors="ignore") as f:
            all_lines = f.readlines()
        start = max(0, node.start_line - 1)
        end = min(len(all_lines), node.end_line)
        if end - start > max_lines:
            end = start + max_lines
        return "".join(all_lines[start:end])
    except Exception:
        return None



def _find_project() -> Optional[str]:
    """Walk up from CWD to find .codewiki/."""
    p = os.path.abspath(".")
    while True:
        if os.path.isdir(os.path.join(p, ".codewiki")):
            return p
        parent = os.path.dirname(p)
        if parent == p:
            return None
        p = parent


def _get_store(project_root: Optional[str] = None):
    """Open the SQLite store for a project."""
    from codewiki.db.store import GraphStore

    root = project_root or _find_project()
    if not root:
        typer.echo("No CodeWiki project found. Run `codewiki init <path>` first.", err=True)
        raise typer.Exit(1)

    db_path = os.path.join(root, ".codewiki", "codewiki.db")
    if not os.path.isfile(db_path):
        typer.echo(f"No database found at {db_path}. Run `codewiki init <path>` first.", err=True)
        raise typer.Exit(1)

    store = GraphStore(db_path)
    store.init_schema()
    return store, root


# =========================================================================
# Build command (graph + wiki combined)
# =========================================================================

@app.command()
def init(
    project_root: str = typer.Argument(".", help="Target project root (default: cwd)."),
):
    """Build code graph and generate wiki — one command, end to end.

    Runs inside the target Java project directory:
        cd my-java-project
        codewiki init
    """
    import json

    # ── Windows: silence deepagents subprocess GBK noise ─────────────────
    # deepagents' LocalShellBackend reads subprocess output with the system
    # default encoding (GBK on Chinese Windows) which chokes on UTF-8 bytes
    # from shell commands.  These are non-fatal thread exceptions; suppress them.
    import threading as _threading
    _orig_hook = _threading.excepthook
    def _quiet_gbk(args):
        exc = args.exc_value
        if isinstance(exc, UnicodeDecodeError) and "gbk" in str(exc).lower():
            return
        _orig_hook(args)
    _threading.excepthook = _quiet_gbk

    root = os.path.abspath(project_root)
    if not os.path.isdir(root):
        typer.echo(f"Error: {root} is not a directory.", err=True)
        raise typer.Exit(1)

    # --- rich progress ---------------------------------------------------------
    from rich.progress import (
        Progress,
        SpinnerColumn,
        TextColumn,
        BarColumn,
        TimeElapsedColumn,
        MofNCompleteColumn,
    )
    from rich.console import Console

    console = Console()
    start_all = time.time()

    # 5 phases. Phase 4 (wiki generation) is the long pole; we poll the output
    # directory from a daemon thread to show "N docs written" alongside an
    # indeterminate spinner.
    with Progress(
        SpinnerColumn(),
        TextColumn("[bold]{task.description}"),
        BarColumn(),
        MofNCompleteColumn(),
        TimeElapsedColumn(),
        console=console,
        transient=False,
    ) as progress:

        # ── Phase 1/5: Parse source files ─────────────────────────────────
        task1 = progress.add_task("[1/5] 解析源码...", total=1)

        codewiki_dir = os.path.join(root, ".codewiki")
        os.makedirs(codewiki_dir, exist_ok=True)
        db_path = os.path.join(codewiki_dir, "codewiki.db")

        from codewiki.db.store import GraphStore
        from codewiki.extraction.orchestrator import ExtractionOrchestrator

        store = GraphStore(db_path)
        store.init_schema()

        orch = ExtractionOrchestrator(root, store)
        files = orch._scan_directory()
        total_files = len(files)

        # re-add the task with correct total
        progress.remove_task(task1)
        task1 = progress.add_task("[1/5] 解析源码...", total=total_files)

        def on_progress(p):
            if p.phase == "parsing":
                desc = f"[1/5] 解析源码: {p.current_file}" if p.current_file else "[1/5] 解析源码..."
                progress.update(task1, completed=p.current, description=desc)
            elif p.phase == "scanning":
                pass  # too fast to show

        result = orch.index_all(on_progress=on_progress)
        progress.update(
            task1,
            completed=total_files,
            description=f"[1/5] 解析源码 ✓ {result.files_indexed} 文件, {result.nodes_created} 节点, {result.edges_created} 边",
        )

        if result.files_indexed == 0:
            typer.echo("Warning: No Java files found. Is this a Java project?", err=True)
            # skip wiki — nothing to document
            return

        # ── Phase 2/5: Build knowledge graph ──────────────────────────────
        n_steps = 3  # resolve, synthesize, spring (inline) → done
        has_spring = "spring" in (result.detected_frameworks or [])
        task2 = progress.add_task("[2/5] 构建知识图谱...", total=n_steps)

        # 2a: cross-file references
        progress.update(task2, description="[2/5] 跨文件引用绑定...")
        from codewiki.resolution.resolver import ReferenceResolver
        resolver = ReferenceResolver(store, frameworks=result.detected_frameworks)
        res = resolver.resolve_and_persist()
        progress.update(task2, advance=1, description=f"[2/5] 引用绑定 ✓ {res.resolved} resolved")

        # 2b: synthesize edges
        progress.update(task2, description="[2/5] 合成类型/继承边...")
        from codewiki.resolution.callback_synthesizer import CallbackSynthesizer
        synth = CallbackSynthesizer(store)
        synth.synthesize_all()
        progress.update(task2, advance=1)

        # 2c: Spring DI edges
        if has_spring:
            progress.update(task2, description="[2/5] Spring DI 注入边...")
            from codewiki.resolution.spring_resolver import SpringResolver
            spring = SpringResolver(store, root)
            spring.resolve_and_persist()
        progress.update(task2, advance=1, description=f"[2/5] 知识图谱 ✓ {result.nodes_created} nodes")

        # Track git commit + write project config (branch only, no secrets)
        from codewiki.sync.git_watcher import get_current_commit, get_branch_name, set_last_indexed_commit
        branch = get_branch_name(root)
        if branch:
            commit = get_current_commit(root, branch)
            if commit:
                set_last_indexed_commit(store, commit)
        from codewiki.wiki.config import save_project_config
        save_project_config(root, branch=branch)

        # ── Phase 3/5: Project map ────────────────────────────────────────
        task3 = progress.add_task("[3/5] 生成项目地图...", total=1)
        from codewiki.wiki.project_map import ensure_project_map
        ensure_project_map(root, store)
        progress.update(task3, advance=1, description="[3/5] 项目地图 ✓ .project_map.md")

        # ── Phase 4/5: Wiki generation ────────────────────────────────────
        task4 = progress.add_task("[4/5] LLM 生成 Wiki...", total=None)

        wiki_dir = os.path.join(root, ".codewiki", "wiki")
        os.makedirs(wiki_dir, exist_ok=True)

        # Poll wiki directory from daemon thread; show doc count alongside spinner
        stop_flag = threading.Event()
        wiki_count = [0]

        def _poll_wiki_dir():
            while not stop_flag.is_set():
                try:
                    cnt = 0
                    if os.path.isdir(wiki_dir):
                        for _root, _dirs, _files in os.walk(wiki_dir):
                            cnt += sum(1 for f in _files if f.endswith(".md"))
                except Exception:
                    pass
                wiki_count[0] = cnt
                stop_flag.wait(2)

        poll_thread = threading.Thread(target=_poll_wiki_dir, daemon=True)
        poll_thread.start()

        try:
            from codewiki.wiki.build import run_wiki

            _elapsed = [0.0]
            _start = time.time()

            def _update_spinner():
                while not stop_flag.is_set():
                    _elapsed[0] = time.time() - _start
                    cnt = wiki_count[0]
                    desc = f"[4/5] LLM 生成 Wiki... ({cnt} docs, {_elapsed[0]:.0f}s)"
                    progress.update(task4, description=desc)
                    stop_flag.wait(1)

            spinner_thread = threading.Thread(target=_update_spinner, daemon=True)
            spinner_thread.start()

            run_wiki(mode="init", project_root=root)
        finally:
            stop_flag.set()
            poll_thread.join(timeout=5)

        cnt = wiki_count[0]
        progress.update(task4, completed=1, total=1, description=f"[4/5] Wiki 生成 ✓ {cnt} docs")

        # ── Phase 5/5: Lint + finalize ────────────────────────────────────
        task5 = progress.add_task("[5/5] 渲染校验+收尾...", total=1)
        from codewiki.wiki.lint import lint_wiki
        report = lint_wiki(wiki_dir)
        progress.update(
            task5, advance=1,
            description=f"[5/5] 校验 ✓ {report.files} files, {report.errors} errors" if report.errors else f"[5/5] 校验 ✓ {report.files} files, no issues"
        )

        # Bootstrap AGENTS.md ref
        from codewiki.bootstrap import inject_agents_md
        inject_agents_md(root)

    elapsed = time.time() - start_all
    console.print(f"\n[bold green]CodeWiki init complete in {elapsed:.0f}s.[/bold green]")
    console.print(f"  Entry: [bold].codewiki/wiki/index.md[/bold]")
    typer.echo("  Wrote CodeWiki guide to AGENTS.md / CLAUDE.md")


@app.command()
def sync():
    """Re-index changed files since the last build.

    Only git-committed changes are detected. Non-ancestor commits trigger
    a full rebuild automatically.
    """
    store, root = _get_store()

    from codewiki.sync.reconciler import sync as incremental_sync

    typer.echo("Syncing...")
    result = incremental_sync(store, root)

    if not result.success:
        typer.echo("Sync completed with errors:", err=True)
        for e in result.errors[:5]:
            typer.echo(f"  {e['filePath']}: {e['message']}", err=True)
    else:
        typer.echo(f"Done in {result.duration_ms}ms.")
        typer.echo(f"  Files indexed: {result.files_indexed}")
        typer.echo(f"  Nodes created: {result.nodes_created}")
        typer.echo(f"  Edges created: {result.edges_created}")


# =========================================================================
# Query commands
# =========================================================================

@app.command()
def node(symbol: str = typer.Argument(..., help="Symbol name or node ID.")):
    """Get details of a code symbol."""
    store, root = _get_store()
    from codewiki.graph.traversal import GraphTraversal

    t = GraphTraversal(store)
    node = t.get_node(symbol)
    if not node:
        typer.echo(f"Symbol '{symbol}' not found.")
        raise typer.Exit(1)

    typer.echo(f"  Name:        {node.name}")
    typer.echo(f"  Kind:        {node.kind}")
    typer.echo(f"  File:        {node.file_path}:{node.start_line}")
    typer.echo(f"  Qualified:   {node.qualified_name}")
    if node.signature:
        typer.echo(f"  Signature:   {node.signature}")
    if node.docstring:
        typer.echo(f"  Docstring:   {node.docstring}")
    if node.visibility:
        typer.echo(f"  Visibility:  {node.visibility}")
    if node.return_type:
        typer.echo(f"  Return type: {node.return_type}")

    # Source code block (CodeGraph parity — verbatim source, no extra Read needed)
    source_block = _extract_source_block(node, root)
    if source_block:
        typer.echo("")
        typer.echo("Source:")
        typer.echo("```java")
        typer.echo(source_block.rstrip("\n"))
        typer.echo("```")


@app.command()
def callers(
    symbol: str = typer.Argument(..., help="Symbol name or node ID."),
    max_depth: int = typer.Option(1, "--depth", "-d", help="Traversal depth."),
):
    """Find all callers of a symbol."""
    store, root = _get_store()
    from codewiki.graph.traversal import GraphTraversal

    t = GraphTraversal(store)
    node = t.get_node(symbol)
    if not node:
        typer.echo(f"Symbol '{symbol}' not found.")
        raise typer.Exit(1)

    callers = t.callers(node.id, max_depth=max_depth)
    if not callers:
        typer.echo(f"No callers found for '{symbol}'.")
        return

    typer.echo(f"Callers of {symbol} ({len(callers)}):")
    for c in callers:
        typer.echo(f"  {c.file_path}:{c.start_line}  {c.name} ({c.kind})")


@app.command()
def callees(
    symbol: str = typer.Argument(..., help="Symbol name or node ID."),
    max_depth: int = typer.Option(1, "--depth", "-d", help="Traversal depth."),
):
    """Find all callees of a symbol."""
    store, root = _get_store()
    from codewiki.graph.traversal import GraphTraversal

    t = GraphTraversal(store)
    node = t.get_node(symbol)
    if not node:
        typer.echo(f"Symbol '{symbol}' not found.")
        raise typer.Exit(1)

    callees = t.callees(node.id, max_depth=max_depth)
    if not callees:
        typer.echo(f"No callees found for '{symbol}'.")
        return

    typer.echo(f"Callees of {symbol} ({len(callees)}):")
    for c in callees:
        typer.echo(f"  {c.file_path}:{c.start_line}  {c.name} ({c.kind})")


@app.command()
def impact(
    symbol: str = typer.Argument(..., help="Symbol name or node ID."),
    max_depth: int = typer.Option(3, "--depth", "-d", help="Traversal depth."),
):
    """Calculate the impact radius (blast radius) of changing a symbol."""
    store, root = _get_store()
    from codewiki.graph.traversal import GraphTraversal

    t = GraphTraversal(store)
    node = t.get_node(symbol)
    if not node:
        typer.echo(f"Symbol '{symbol}' not found.")
        raise typer.Exit(1)

    result = t.impact(node.id, max_depth=max_depth)
    others = [n for n in result.nodes if n.id != node.id]
    if not others:
        typer.echo(f"No dependents found for '{symbol}'.")
        return

    typer.echo(f"Impact of {symbol} ({len(others)} dependents):")
    for n in others[:50]:
        typer.echo(f"  {n.file_path}:{n.start_line}  {n.name} ({n.kind})")
    if len(others) > 50:
        typer.echo(f"  ... and {len(others) - 50} more")


@app.command()
def search(
    query: str = typer.Argument(..., help="Search query (FTS5 syntax)."),
    limit: int = typer.Option(50, "--limit", "-n", help="Max results."),
):
    """Search for code symbols by name or docstring."""
    store, root = _get_store()
    from codewiki.graph.traversal import GraphTraversal

    t = GraphTraversal(store)
    results = t.search(query, limit=limit)
    if not results:
        typer.echo(f"No results for '{query}'.")
        return

    typer.echo(f"Search: '{query}' ({len(results)} results):")
    for node, score in results:
        typer.echo(f"  {node.file_path}:{node.start_line}  {node.name} ({node.kind})  score={score:.2f}")


@app.command()
def explore(symbol: str = typer.Argument(..., help="Symbol name or node ID.")):
    """Explore a symbol's full context (details + callers + callees)."""
    store, root = _get_store()
    from codewiki.graph.traversal import GraphTraversal

    t = GraphTraversal(store)
    node = t.get_node(symbol)
    if not node:
        typer.echo(f"Symbol '{symbol}' not found.")
        raise typer.Exit(1)

    typer.echo(f"=== {node.name} ({node.kind}) ===")
    typer.echo(f"  {node.file_path}:{node.start_line}")
    if node.docstring:
        typer.echo(f"  {node.docstring}")

    # Source code block (CodeGraph parity — verbatim source, no extra Read needed)
    source_block = _extract_source_block(node, root)
    if source_block:
        typer.echo("")
        typer.echo("Source:")
        typer.echo("```java")
        typer.echo(source_block.rstrip("\n"))
        typer.echo("```")

    callers = t.callers(node.id, max_depth=1)
    if callers:
        typer.echo(f"\nCallers ({len(callers)}):")
        for c in callers:
            typer.echo(f"  {c.file_path}:{c.start_line}  {c.name} ({c.kind})")

    callees = t.callees(node.id, max_depth=1)
    if callees:
        typer.echo(f"\nCallees ({len(callees)}):")
        for c in callees:
            typer.echo(f"  {c.file_path}:{c.start_line}  {c.name} ({c.kind})")

    # Ancestor (containing class)
    ancestors = store.get_incoming_edges(node.id, ["contains"])
    if ancestors:
        parent = store.get_node_by_id(ancestors[0].source)
        if parent:
            typer.echo(f"\nContained in: {parent.file_path}:{parent.start_line}  {parent.name} ({parent.kind})")

    # Impact / blast radius (CodeGraph parity: "what depends on this")
    result = t.impact(node.id, max_depth=3)
    others = [n for n in result.nodes if n.id != node.id]
    if others:
        take = min(len(others), 50)
        typer.echo(f"\nImpact (blast radius, {len(others)} dependents, showing {take}):")
        for n in others[:take]:
            typer.echo(f"  {n.file_path}:{n.start_line}  {n.name} ({n.kind})")

    # Related source (cluster) — CodeGraph parity: dump verbatim source for the
    # focal symbol's direct callers + callees, so a single explore shows the
    # whole dependency cluster without extra Read calls.
    related: list = []
    seen_ids: set[str] = set()
    for c in list(callers) + list(callees):
        if c.id in seen_ids:
            continue
        seen_ids.add(c.id)
        # Skip trivial nodes (imports/namespace) — their source is just one line.
        if c.kind in ("import", "namespace"):
            continue
        related.append(c)
    if related:
        shown = 0
        typer.echo(f"\nRelated source ({len(related)} symbols in cluster):")
        for c in related:
            if shown >= 12:
                typer.echo(f"  ... and {len(related) - shown} more (use `node <name>` for each)")
                break
            blk = _extract_source_block(c, root, max_lines=60)
            if blk:
                typer.echo(f"\n--- {c.name} ({c.kind}) @ {c.file_path}:{c.start_line} ---")
                typer.echo("```java")
                typer.echo(blk.rstrip("\n"))
                typer.echo("```")
                shown += 1


# =========================================================================
# MCP server
# =========================================================================

@app.command()
def serve():
    """Start the MCP server (stdio transport) for use with AI agents.

    The server auto-discovers the nearest .codewiki/ directory and serves the
    read-only `codewiki_explore` tool. Graph building/syncing is done via the
    CLI (`codewiki init` / `codewiki sync`), not in-process.

    Add to your agent's MCP config:

        "codewiki": {
            "command": "python",
            "args": ["-m", "codewiki.cli", "serve"]
        }
    """
    from codewiki.mcp_server import main
    # Redirect logs to file so stdout stays clean for MCP protocol
    import logging
    logging.basicConfig(
        filename=os.path.expanduser("~/.codewiki/mcp.log"),
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    main()


@wiki_app.command("init")
def wiki_init(
    project_root: str = typer.Argument(".", help="Project root directory (default: cwd)"),
) -> None:
    """[Deprecated] Use `codewiki init` instead — it now builds graph + wiki together."""
    typer.echo(
        "⚠ `codewiki wiki init` is deprecated. Use `codewiki init` instead (it now does both).",
        err=True,
    )
    raise typer.Exit(1)


@wiki_app.command("sync")
def wiki_sync(
    project_root: str = typer.Argument(".", help="Project root directory (default: cwd)"),
    branch: str = typer.Option(None, "--branch", "-b", help="Git branch to track (default: auto-detect)"),
) -> None:
    """Incrementally update the wiki from the latest git commits.

    Compares the last indexed commit to HEAD, builds a changeset, and lets the
    agent revise only the affected wiki documents. LLM settings are read from
    the global config (set once via `codewiki config set`).
    """
    from codewiki.wiki.build import run_wiki

    typer.echo("Starting wiki sync...")
    run_wiki(mode="sync", project_root=project_root, branch=branch)
    typer.echo("Wiki sync complete.")


# =========================================================================
# Config command
# =========================================================================

@app.command()
def config(
    action: str = typer.Argument("show", help="'show' (default) or 'set'"),
    api_key: str = typer.Option(None, "--api-key", help="LLM API key"),
    model: str = typer.Option(None, "--model", help="Model name (OpenAI-compatible, default: deepseek-v4-flash)"),
    base_url: str = typer.Option(None, "--base-url", help="OpenAI-compatible base URL (default: https://api.deepseek.com/v1)"),
) -> None:
    """View or update the global CodeWiki LLM config.

    Settings are stored in <codewiki-project>/.codewiki/config.json and apply to all projects.
    API key / model / base_url are NEVER written into target project directories.

    Examples:
        codewiki config                          Show current settings
        codewiki config set --api-key sk-xxx     Set your API key
        codewiki config set --model gpt-4o       Change the model
    """
    from codewiki.wiki.config import global_config_path, load_global_config, save_global_config

    if action == "show":
        data = load_global_config()
        if not data:
            typer.echo("No config set. Run 'codewiki config set --api-key <key>' first.")
        else:
            typer.echo(f"Global config ({global_config_path()}):")
            for k in sorted(data):
                v = data[k]
                if k == "api_key" and v:
                    v = v[:8] + "..." if len(v) > 11 else "***"
                typer.echo(f"  {k}: {v}")
        return

    if action == "set":
        p = save_global_config(api_key=api_key, model=model, base_url=base_url)
        typer.echo(f"Config saved to {p}")
        keys = [k for k, v in {"api_key": api_key, "model": model, "base_url": base_url}.items() if v is not None]
        typer.echo(f"  Updated: {', '.join(keys)}")
        return

    typer.echo(f"Unknown action: {action}. Use 'show' or 'set'.", err=True)
    raise typer.Exit(1)


def main():
    app()


if __name__ == "__main__":
    main()
