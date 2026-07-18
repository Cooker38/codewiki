"""
Spring framework resolver — adds dependency-injection wiring edges.

CodeWiki's tree-sitter extraction only captures lexical references (imports,
explicit `new`, and method calls). It does NOT understand Spring's bean
graph, so `callers` / `impact` (blast radius) severely under-report how a
bean is actually used. CodeGraph (codegraph/src/resolution/frameworks/java.ts
+ the Spring-aware resolution) wires two DI relationships that we replicate
here:

1. **@Bean parameter injection**: a `@Configuration` class's `@Bean` method
   that takes a parameter of type `T` implies the configuration *uses* bean
   `T`. (Observed: `McpServerConfig.toolCallbackProvider(RunScriptTool rt)`
   → McpServerConfig uses RunScriptTool.)
2. **@SpringBootApplication boot context**: `SpringApplication.run(Boot.class)`
   boots an ApplicationContext that component-scans and instantiates every
   `@Component`/`@Service`/`@Controller`/`@Repository`/`@Configuration` bean
   under Boot's package. So Boot *uses* all those beans. (Observed:
   AgentApplication / BusinessApplication / McpServerApplication each list as
   dependents of ScriptExecutionService in codegraph's impact.)

These edges are stored as kind ``uses`` (provenance ``heuristic``) and are
picked up by ``traversal.callers`` (added to _CALL_EDGE_KINDS) and
``traversal.impact`` (all incoming except contains), closing the gap with
CodeGraph's caller / blast-radius output.

3. **@Bean return-type link (Spring bean graph)**: a ``@Bean`` method's return
   type ``T`` means the declaring ``@Configuration`` *produces* bean ``T``; a
   ``@Bean`` parameter / ``@Autowired`` field of type ``T`` means a class
   *consumes* bean ``T``; a class ``implements``/``extends`` ``T`` means it is a
   subtype of the produced contract. Wiring producers↔consumers and
   producers↔implementers by **type name** (so external contracts like
   ``ToolCallbackProvider`` from spring-ai still link) reproduces CodeGraph's
   deeper impact for a bean. Observed in data-creator:
   ``McpServerConfig.toolCallbackProvider`` returns ``ToolCallbackProvider`` →
   ``ChatClientConfig.chatClient(ToolCallbackProvider)`` consumes it and
   ``LoggingToolCallbackProvider implements ToolCallbackProvider``. Without this
   link, ``impact RunScriptTool`` (which ``McpServerConfig`` depends on) misses
   ``ChatClientConfig`` + ``LoggingToolCallbackProvider``.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Optional

from codewiki.types import Edge

if TYPE_CHECKING:
    from codewiki.db.store import GraphStore

# Annotations that mark a class as a Spring bean (component-scanned).
_BEAN_ANNOTATIONS = frozenset({
    "Component", "Service", "Controller", "RestController",
    "Repository", "Configuration", "SpringBootApplication", "SpringApplication",
})

# Annotations that mark a boot class (Application context entry point).
_BOOT_ANNOTATIONS = frozenset({"SpringBootApplication", "SpringApplication"})

# Regexes (run on comment/string-stripped source, mirroring codegraph's
# stripCommentsForRegex approach).
_PACKAGE_RE = re.compile(r'package\s+([\w.]+)\s*;')
_IMPORT_RE = re.compile(r'import\s+(?:static\s+)?([\w.]+)\s*;')
_CLASS_RE = re.compile(r'\bclass\s+(\w+)')
_ANNOT_RE = re.compile(r'@(\w+)')
_BEAN_RE = re.compile(r'@Bean\b')
_METHOD_SIG_RE = re.compile(
    r'(?:public|private|protected|@\w+(?:\([^)]*\))?\s*)*'
    r'\s*([\w.<>\[\],\s?]+?)\s+(\w+)\s*\(([^)]*)\)'
)


def _strip_comments(src: str) -> str:
    """Remove comments and string/character literals (avoid false matches)."""
    # Block comments
    src = re.sub(r'/\*.*?\*/', ' ', src, flags=re.DOTALL)
    # Line comments
    src = re.sub(r'//[^\n]*', ' ', src)
    # Strings (double + single quoted) — replace with spaces, keep length-ish
    src = re.sub(r'"(\\.|[^"\\])*"', '""', src)
    src = re.sub(r"'(\\.|[^'\\])*'", "''", src)
    return src


@dataclass
class _BeanInfo:
    node_id: str
    name: str
    package: str


@dataclass
class SpringResolutionResult:
    boot_context_edges: int = 0
    bean_param_edges: int = 0
    bean_return_edges: int = 0
    bean_implements_edges: int = 0
    by_kind: dict[str, int] = field(default_factory=dict)


class SpringResolver:
    """Scans Java sources and emits Spring DI `uses` edges."""

    def __init__(self, store: "GraphStore", root: str = "."):
        self.store = store
        self.root = root

    # ------------------------------------------------------------------ #
    # Public API
    # ------------------------------------------------------------------ #
    def resolve_and_persist(self) -> SpringResolutionResult:
        result = SpringResolutionResult()

        # 1. Load all class/interface nodes -> candidate map + bean list.
        #    Nodes carry qualified_name = "<package>::<ClassName>".
        classes = self.store.conn.execute(
            "SELECT id, name, qualified_name, file_path FROM nodes "
            "WHERE kind IN ('class','interface')"
        ).fetchall()
        # name -> list of (id, package, qualified_name)
        candidates: dict[str, list[tuple[str, str, str]]] = {}
        bean_nodes: list[_BeanInfo] = []
        for row in classes:
            qn = row["qualified_name"] or ""
            pkg = qn.split("::")[0] if "::" in qn else ""
            candidates.setdefault(row["name"], []).append(
                (row["id"], pkg, qn)
            )
            # Heuristic: a class whose package ends in one of the conventional
            # bean directories is very likely a Spring bean. We refine with
            # annotations below, but this gives a fallback so boot-context
            # wiring still works when annotation capture missed a class.
            if any(seg in pkg.split(".") for seg in
                   ("service", "controller", "repository", "config", "tools", "mcp", "agent", "business", "metadata", "shared")):
                bean_nodes.append(_BeanInfo(row["id"], row["name"], pkg))

        # 2. Scan every .java file for annotations + @Bean params + boot classes.
        boot_classes: list[_BeanInfo] = []
        # config class id -> list of injected param type simple-names (with line)
        # store the file's import map alongside so type resolution is accurate.
        bean_param_types: list[tuple[str, str, int, dict[str, str]]] = []  # (config_id, type, line, imports)
        # Spring bean graph (matched by simple type NAME, so external contracts
        # like spring-ai's ToolCallbackProvider still participate):
        #   producers[T]   = config/bean class node ids that @Bean-return T
        #   consumers[T]   = class node ids that consume T (@Bean param / @Autowired)
        #   implementers[T]= class node ids that implements/extends T
        producers: dict[str, list[str]] = {}
        consumers: dict[str, list[str]] = {}
        implementers: dict[str, list[str]] = {}

        files = self.store.get_all_files()
        for frec in files:
            path = frec.path
            if not path.endswith(".java"):
                continue
            full = os.path.join(self.root, path)
            try:
                with open(full, encoding="utf-8", errors="ignore") as fh:
                    raw = fh.read()
            except OSError:
                continue
            safe = _strip_comments(raw)

            # imports: localName -> FQN
            imports: dict[str, str] = {}
            for m in _IMPORT_RE.finditer(safe):
                fqn = m.group(1)
                local = fqn.rsplit(".", 1)[-1]
                imports[local] = fqn

            # class annotations: for each class decl, look back for @tokens
            for cm in _CLASS_RE.finditer(safe):
                cname = cm.group(1)
                window = safe[max(0, cm.start() - 400):cm.start()]
                anns = set(_ANNOT_RE.findall(window))
                node_id = self._resolve_type(cname, imports, candidates)
                if not node_id:
                    continue
                pkg = ""
                for cid, cpkg, cqn in candidates.get(cname, []):
                    if cid == node_id:
                        pkg = cpkg
                        break
                info = _BeanInfo(node_id, cname, pkg)

                # Record implements/extends supertypes so a class that realizes
                # a produced bean contract (e.g. LoggingToolCallbackProvider
                # implements ToolCallbackProvider produced by an @Bean) is wired.
                for st in self._capture_supertypes(safe, cm):
                    implementers.setdefault(st, []).append(node_id)

                if anns & _BOOT_ANNOTATIONS:
                    boot_classes.append(info)
                    if cname not in {b.name for b in bean_nodes}:
                        bean_nodes.append(info)

                # @Bean methods in this file -> param injection for the
                # enclosing class (approx: attribute to this class node).
                # (We already have the class node; @Bean methods belong to it.)
                # Find @Bean occurrences and their method signatures.
                for bm in _BEAN_RE.finditer(safe):
                    tail = safe[bm.end():bm.end() + 400]
                    sm = _METHOD_SIG_RE.search(tail)
                    if not sm:
                        continue
                    params = sm.group(3)
                    method_line = raw[:bm.start()].count("\n") + 1
                    # @Bean return type -> producer of that contract. Strip any
                    # leading modifiers/annotations the fragile signature regex
                    # may have absorbed into group(1) (e.g. "public ToolCallbackProvider").
                    ret_type = (sm.group(1) or "").strip()
                    ret_type = re.sub(
                        r'^(?:public|private|protected|static|final|abstract|'
                        r'synchronized|native|@\w+(?:\([^)]*\))?\s*)+',
                        '', ret_type).strip()
                    ret_base = re.split(r'[<\[\s]', ret_type)[0].strip()
                    if ret_base and ret_base[0].isupper():
                        producers.setdefault(ret_base, []).append(node_id)
                    for p in params.split(","):
                        p = p.strip()
                        if not p:
                            continue
                        toks = p.split()
                        # drop leading annotations / modifiers
                        toks = [t for t in toks if not t.startswith("@")]
                        if len(toks) < 1:
                            continue
                        # param type = everything except the last token (name)
                        # but generic type may be e.g. "List<Foo> foo"
                        ptype = " ".join(toks[:-1]) if len(toks) > 1 else toks[0]
                        # strip generics/array to base type
                        base = re.split(r'[<\[\s]', ptype.strip())[0].strip()
                        if base and base[0].isupper():
                            bean_param_types.append((node_id, base, method_line, dict(imports)))
                            consumers.setdefault(base, []).append(node_id)

        # 3. Emit @Bean parameter injection edges.
        seen: set[tuple[str, str, str]] = set()
        edges: list[Edge] = []
        for config_id, type_name, line, fimports in bean_param_types:
            target_id = self._resolve_type(type_name, fimports, candidates)
            if not target_id or target_id == config_id:
                continue
            key = (config_id, target_id, "uses")
            if key in seen:
                continue
            seen.add(key)
            edges.append(Edge(
                source=config_id, target=target_id, kind="uses",
                line=line, provenance="heuristic",
                metadata={"spring": "bean-param"},
            ))
            result.bean_param_edges += 1

        # 4. Emit @SpringBootApplication boot-context edges.
        for boot in boot_classes:
            prefix = boot.package + "." if boot.package else ""
            for b in bean_nodes:
                if b.node_id == boot.node_id:
                    continue
                if not b.package:
                    continue
                if b.package == boot.package or b.package.startswith(prefix):
                    key = (boot.node_id, b.node_id, "uses")
                    if key in seen:
                        continue
                    seen.add(key)
                    edges.append(Edge(
                        source=boot.node_id, target=b.node_id, kind="uses",
                        provenance="heuristic",
                        metadata={"spring": "boot-context"},
                    ))
                    result.boot_context_edges += 1

        # 5. Wire the Spring bean graph by type name: producers <-> consumers
        #    and producers <-> implementers. This links e.g. ChatClientConfig
        #    (consumes ToolCallbackProvider) and LoggingToolCallbackProvider
        #    (implements it) back to McpServerConfig (produces it via @Bean),
        #    so `impact RunScriptTool` (which McpServerConfig depends on)
        #    surfaces both — matching CodeGraph's deeper @Bean return-type link.
        for t in set(producers) | set(consumers) | set(implementers):
            for p in producers.get(t, []):
                for c in consumers.get(t, []):
                    if c != p:
                        self._emit_uses(edges, seen, result, c, p, "bean-return")
                for i in implementers.get(t, []):
                    if i != p:
                        self._emit_uses(edges, seen, result, i, p, "bean-implements")

        if edges:
            self.store.insert_edges(edges)
            result.by_kind["uses"] = len(edges)

        return result

    @staticmethod
    def _emit_uses(edges, seen, result, source, target, subkind):
        key = (source, target, "uses")
        if key in seen:
            return
        seen.add(key)
        edges.append(Edge(
            source=source, target=target, kind="uses",
            provenance="heuristic",
            metadata={"spring": subkind},
        ))
        if subkind == "bean-return":
            result.bean_return_edges += 1
        elif subkind == "bean-implements":
            result.bean_implements_edges += 1

    # ------------------------------------------------------------------ #
    # Helpers
    # ------------------------------------------------------------------ #
    def _capture_supertypes(self, safe: str, class_match) -> list[str]:
        """Return base type names from `extends`/`implements` on a class header.

        The class header runs from the `class` keyword up to the first `{`.
        Generics are collapsed to the bare type (``List<Foo>`` -> ``Foo``).
        """
        start = class_match.start()
        brace = safe.find("{", start)
        end = brace if brace != -1 else start + 400
        header = safe[start:end]
        out: list[str] = []
        for kw in ("implements", "extends"):
            idx = header.find(kw)
            if idx == -1:
                continue
            rest = header[idx + len(kw):].split("{")[0]
            for part in rest.split(","):
                base = re.split(r"[<\[\s]", part.strip())[0].strip()
                if base and base[0].isupper():
                    out.append(base)
        return out

    def _resolve_type(
        self, type_name: str, imports: dict[str, str],
        candidates: dict[str, list[tuple[str, str, str]]],
    ) -> Optional[str]:
        """Resolve a simple type name (or FQN) to a class node id."""
        if not type_name:
            return None
        # Fully-qualified in source (e.g. com.foo.Bar) — match by qualified_name.
        if "." in type_name:
            cands = candidates.get(type_name.rsplit(".", 1)[-1], [])
            for cid, pkg, qn in cands:
                if qn and (qn == type_name or qn.endswith("::" + type_name)):
                    return cid
        cands = candidates.get(type_name)
        if not cands:
            return None
        if len(cands) == 1:
            return cands[0][0]
        # Prefer the import-resolved FQN.
        fqn = imports.get(type_name)
        if fqn:
            for cid, pkg, qn in cands:
                if qn and qn.startswith(fqn + "::"):
                    return cid
        # Prefer a node in a conventional bean package.
        for cid, pkg, qn in cands:
            if any(seg in pkg.split(".") for seg in
                   ("service", "controller", "repository", "config", "tools", "mcp", "agent", "business")):
                return cid
        return cands[0][0]
