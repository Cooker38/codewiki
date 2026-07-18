# CodeWiki 项目记忆（长期）

## ★ 权威参考：D:\codegraph（只复用不优化）
- 本地代码智能引擎（tree-sitter → SQLite 图谱 → MCP explore）。CodeWiki 方案直接沿用其设计。
- **只总结复用，绝不在此项目内对其提优化**（用户明确要求）。
- 详细参考文档：`resource/CodeGraph参考/`（`00-总览与架构`…`06-增量同步与Watcher`，7 份）。出方案先对齐这套文档。

## CodeGraph 关键事实（速查）
- 节点 ID：`sha256("${filePath}:${kind}:${name}:${line}").slice(0,32)` 前缀 `kind:`。行号变→ID变，增量须用 (filePath,kind,name) 重匹配。
- 三表：`nodes`/`edges`/`unresolved_refs`（+files/name_segment_vocab/FTS5）。边唯一索引 (source,target,kind,IFNULL(line,-1),IFNULL(col,-1))。
- 两阶段落库：AST 只落 `contains`+文件内 `references`，对外降级 `UnresolvedReference` 由 `ReferenceResolver` 跨文件绑定；`type_of/returns/overrides` 是合成层。
- 遍历「先 visited 后深度」防重复；callers 计入 instantiates；impact 排除 contains 防爆炸。
- CG 自身增量基于文件系统 (size,mtime)+content_hash，**不依赖 git**——这是参考事实。

## ★ CodeWiki 增量方案（与 CG 故意偏离，已锁定）
- 触发=监听**本地 git 提交**（非 fs.watch）；数据源=本地 git（不碰 GitHub 远端），只索引已提交代码。
- init 锁定当前分支名，固定轮询 `git rev-parse <branch>` 比对 `last_indexed_commit`；切分支不误触发。
- 变更集=`git diff --name-status C_old C_new`（A/M/D/R）；非祖先提交→整库全量重建。
- 落盘：`项目整体方向.md` 5.1/5.3、`06-增量同步与Watcher.md` §5。

## 技术栈 / 开发阶段
- 语言 Python 3.10+；图谱 tree-sitter + sqlite3 + GitPython；MCP `mcp`(FastMCP)；Wiki `langgraph`+`openai`；CLI `typer`；工程 `ruff`+`mypy`+`pytest`。
- 5 阶段：P1 核心图谱引擎(Java, tree-sitter-java) → P2 MCP 查询 → P3 git 增量 / P4 Wiki(可并行) → P5 胶水层。P1 Spec：`resource/specs/P1-核心图谱引擎.md`。

## Spring DI 解析（对齐 CG 的 Spring 感知）
- 根因：原不解析注入 → impact/callers 只看到 import 边，严重低估。
- `src/codewiki/resolution/spring_resolver.py`（`SpringResolver.resolve_and_persist()`）建库 init 检测到 `spring` 后补 `uses` 边（provenance=heuristic）：① `@Bean` 参数注入；② `@SpringBootApplication` 启动上下文；③ `@Bean` 返回类型链（按类型名串 producer/consumer/implementer，含外部契约如 `ToolCallbackProvider`）。
- 联动：`traversal._CALL_EDGE_KINDS` 含 `"uses"` → callers/impact 能遍历注入边。data-creator 共 251 条 uses 边。
- 坑：`_METHOD_SIG_RE` 在 `@Bean` 签名 tail 以空白开头时会把 `"public ToolCallbackProvider"` 吞进返回类型 group(1)；须先剥离前导修饰符/注解再取基类型。

## explore 能力（2026-07-14 对齐）
- `codewiki_explore` 一次返回：node 详情 + 源码块 + callers + callees + impact(blast radius, depth3) + ancestor + 整簇源码(cluster) + 预算指引。
- 类级聚合：`traversal._aggregate_seeds()` 把容器 class 展开其 method/constructor 成员作遍历种子（共享 visited 去重），`store.get_contained_nodes()` 支撑。→ `callees RunScriptTool`(类)=2、`callees ScriptController`(类)=25（CG 类级 callees 仍返回空，CW 更全）。
- 整簇源码：explore 末尾 dump callers+callees 直连节点 verbatim 源码（MCP 4×100 行 / CLI 12×60 行），过滤 import/namespace。
- 注意：「callees 漏字段.方法()」是误判——方法名查双方都正确；类名查双方都空（类级粒度，非 CW 独有）。JDK/框架字段调用双方都不索引，属预期。

## 能力提供方式（已锁定 → 2026-07-14 更新）
- **CLI ≠ MCP**：MCP 只暴露 **1 工具**——`codewiki_explore`（只读查询）。`init`/`sync` 仅 CLI（长任务不进 MCP handler，避免阻塞 stdio 事件循环导致连接器崩溃）。CLI 保留 8 命令(node/callers/callees/impact/search/explore/init/sync)，窄工具不注册为 MCP。
- **Wiki 走文件**：放 `wiki/`(gitignore)，`wiki/quickstart.md` 入口，Agent 用 Read 直接读 + 内链导航；`codewiki init` 时追加引用段到 AGENTS.md/CLAUDE.md。不走 MCP。
- **P4 Wiki 单 Agent（2026-07-15 锁定）**：LangGraph 工作流 = planner(读项目地图+图谱摘要→有序 Plan) → implement 循环(每任务独立上下文, Agent 自主 explore/read_file→write_doc) → finalize(注入 AGENTS.md)。探索停止信号暂不设；增量=sync 后 Agent 拿变更文档+符号自行改写；Mermaid 暂不出。项目地图=`wiki/.project_map.md`(解析阶段生成, 目录+文件名)。Spec: `resource/specs/P4-Wiki生成.md`。
- **增量同步**：CLI `codewiki sync` 手动触发。`auto_sync`（进程内 10min 轮询）已移除——曾因大仓 init 阻塞 stdio 事件循环致 `unavailable after recovery`。
- **stdio 接入坑（2026-07-13）**：MCP `command` 绝不能是 `.bat`/`.cmd`（stdin/stdout 经 cmd 包装层无法继承，握手失败 `-32000 Connection closed`）；须是真正的 `.exe`（`pip install -e .` 生成的 `codewiki.exe`）。配置：`{ "mcpServers": { "codewiki": { "command": "codewiki", "args": ["serve"] } } }`，零绝对路径。改 PATH 后须重启客户端。

## 参考源 / 方向文档
- `D:\openwiki`：Wiki 侧权威参考（AGENTS.md→quickstart.md→内链）。
- `resource/项目整体方向.md`（Python 全栈技术选型+开发阶段）、`resource/技术架构与开发安排.md`（架构图/数据流/目录/约定）。

## 用户偏好
- 中文交流；结构化+表格输出；方案先确认再动手；**不主动创文档**（除非要求）；可视化浅色背景+深字+大字号、粒度粗。
- 实测对比时**要看双方实际输出，不要纯分析**。
