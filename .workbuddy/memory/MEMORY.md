# CodeWiki 项目记忆（长期）

## ★ 最高优先级：D:\codegraph 是权威实现参考（务必先读）
- 这是本地代码智能引擎（tree-sitter → SQLite 图谱 → MCP explore），CodeWiki 项目「tree-sitter 分析 AST 存 SQLite + 知识库查询」方案**直接沿用**它的设计与实现。
- 该文件夹**只做总结复用，不在此项目内对其提任何优化**（用户明确要求）。
- 已沉淀的详细实现参考文档在 `C:\Users\25692\Desktop\CodeWiki\resource\CodeGraph参考/`：
  - `00-总览与架构.md`、`01-存储层与Schema.md`、`02-建库提取管线.md`、`03-引用解析.md`、`04-图查询与遍历.md`、`05-上下文构建与MCP-Explore.md`、`06-增量同步与Watcher.md`
- 后续出方案时，先用 `CodeGraph参考/` 这套文档对齐设计，不要凭空造。

## 关键已确认事实（来自 D:\codegraph 源码查证）
- 节点 ID：`sha256("${filePath}:${kind}:${name}:${line}").slice(0,32)` 前缀 `kind:`（tree-sitter-helpers.ts:18）。行号变→ID变，增量必须用 (filePath,kind,name) 重匹配。
- 三表分离：`nodes` / `edges` / `unresolved_refs`（+ files / name_segment_vocab / FTS5 虚表）。边唯一索引 (source,target,kind,IFNULL(line,-1),IFNULL(col,-1))。
- 两阶段落库：AST 只落 `contains`+文件内 `references`，对外关系降级为 `UnresolvedReference`，由 `ReferenceResolver` 跨文件绑定；`type_of/returns/overrides` 是合成层推断。
- 节点 22 种（types.ts:18 NODE_KINDS）、边 12 种（types.ts:48 EdgeKind）。
- 跨文件边复活（#899/#1240）：re-index 删文件级联删边，须先快照跨文件入边按 (filePath,kind,name) 重绑。
- **codegraph 自身**增量基于文件系统 (size,mtime)+content_hash，不依赖 git（extraction/index.ts:2305 sync）——这是参考事实，保留。
- 图遍历「先 visited 后深度」防重复节点（#1086/#1089）；callers 计入 instantiates；impact 排除 contains 防爆炸。

## ★ CodeWiki 增量方案（用户 2026-07-12 明确纠偏，与 codegraph 故意偏离，现已对齐锁定）
- **触发 = 监听本地 git 提交，不是监听工作区文件变更（不用 fs.watch）**。codegraph 的 fs.watch 思路在 CodeWiki 不适用。
- **数据源 = 本地 git，不碰 GitHub 远端**；只索引已提交代码，未提交改动不纳入。
- **init 时锁定当前 checkout 的分支名**，之后固定轮询该分支引用（`git rev-parse <branch>`）比对 `last_indexed_commit`；切到其他分支不误触发。
- **变更集 = `git diff --name-status C_old C_new`**（A/M/D/R），M/A 重索引、D 删、R 删旧加新；定向重跑 ReferenceResolver（只改相关 unresolved_refs / 跨文件入边）。
- **非祖先提交兜底**：`git merge-base --is-ancestor C_old C_new` 为 false（强推/reset/rebase）→ 整库全量重建。
- 已写入 `项目整体方向.md` 5.1/5.3 与 `06-增量同步与Watcher.md` §5（拆分为 codegraph 复用 vs CodeWiki 偏离）。

## 技术栈（2026-07-12 确认锁定）
- **语言**：Python 3.10+（从 Java 切换，生态更轻、MCP SDK 和 tree-sitter 均为官方维护）
- **图谱**：`tree-sitter` (py-tree-sitter, 官方) + `sqlite3` (stdlib) + `GitPython`
- **MCP**：`mcp` (FastMCP, Anthropic 官方, v1.27.1, 23K star)
- **Wiki 管线**：`langgraph` (StateGraph + Send 并行 + SqliteSaver checkpoint) + `openai` SDK
- **CLI**：`typer`；**工程**：`ruff` + `mypy` + `pytest`
- **运行时依赖 10 个**（含传递），精简可控。详细见 `resource/技术架构与开发安排.md`

## 开发阶段（5 阶段，已定）
- P1 核心图谱引擎 → P2 MCP 查询服务 → P3 Git 增量同步 / P4 Wiki 生成（可并行）→ P5 产品胶水层
- P1 做 Java（`tree-sitter-java`），目标 = codegraph 完整 Python 移植。Spec 已落盘 `resource/specs/P1-核心图谱引擎.md`（15 条验收标准）。

## 重要参考源（均已深挖，后续设计对齐用）
- `D:\codegraph`：图谱侧权威参考（tree-sitter → SQLite → MCP），详细总结在 `resource/CodeGraph参考/`（7 份）。**只复用不优化**。
- `D:\openwiki`：Wiki 侧权威参考（AGENTS.md → quickstart.md → 内链导航），Wiki 放可见目录、Agent 直接读文件、init 时注 AGENTS.md。详细分析见 2026-07-12 对话。

## 原方向文档
- `C:\Users\25692\Desktop\CodeWiki\resource\项目整体方向.md` → 第六节技术选型已重写（Python 全栈）+ 新增第七节开发阶段。
- `C:\Users\25692\Desktop\CodeWiki\resource\技术架构与开发安排.md` → 新增，含系统架构图、数据流、目录结构、开发阶段、技术约定、环境搭建。

## CodeWiki 能力提供方式（2026-07-12 讨论对齐锁定）

### CLI = MCP（统一工具面）
- CLI 和 MCP 是同一套工具，不存在"只有终端能用"或"只有 Agent 能调"的区别。对标 codegraph：`codegraph index` 既是 CLI 命令也是 MCP 工具。
- 工具清单（8 个）：`codewiki init` / `sync` / `node` / `callers` / `callees` / `impact` / `search` / `explore`。其中 `init` 和 `sync` 你或 Agent 都能调；其余 6 个是图谱查询工具。

### Wiki（文件，Agent 直接读，不走 MCP）
- 对标 openwiki 设计（`D:\openwiki` 已深挖）：AGENTS.md/CLAUDE.md 是生态标准文件，Agent 进项目自动读；Wiki 放可见目录 `wiki/`（gitignore、不提交），`wiki/quickstart.md` 作为入口索引（项目概述 + "Documentation map" 链接列表），Agent 通过内链导航按需读取各 Wiki 页面。
- **Wiki 不走 MCP**——它就是 Markdown 文件，Agent 的 Read 工具直接读比包一层 MCP 更自然更快。openwiki 实证可行。

### 图谱（MCP 工具，Agent 调）
- 图谱放 `.codewiki/codewiki.db`（隐藏、gitignore），通过 MCP 工具暴露：`codewiki_node / callers / callees / impact / search / explore`。
- 图谱工具只返回图谱内容，不掺 Wiki。`codewiki_explore` 保留为图谱-only（沿用 codegraph 预算算法），和 openwiki 的 `wiki/quickstart.md` 概念不撞车。
- 传递性聚合事实（callers/impact/类型层级）grep 算不出 = 图谱工具不会被"直接 grep 源码"替代。

### 自动同步（MCP 进程内置，默认开启）
- MCP Server 进程启动时读 `.codewiki/config.json`，若 `auto_sync: true`（默认），则轮询分支引用，检测到新提交自动跑 `sync` 管线。
- 进程生命周期 = 守护生命周期：客户端启动 → 进程起来 → 开始轮询；客户端退出 → 进程死 → 轮询停。
- 关闭：`codewiki init --no-auto-sync` 或手动改 config.json。手动触发：随时调 `codewiki sync`。

### AGENTS.md / CLAUDE.md（桥）
- CodeWiki init 时**追加**引用段到已有 AGENTS.md/CLAUDE.md（若不存在则新建，openwiki 同款逻辑）。引用段内容：「Wiki 从 `wiki/quickstart.md` 开始；代码事实查 `codewiki_*` 工具」。
- 无需 prompt-hook——AGENTS.md 的自动发现已经是最短路径。
- 无需搜索引擎（FTS5 仅用于图谱的符号搜索 `codewiki_search`，不用于 Wiki 检索）。Wiki 导航靠目录 + 内链（openwiki 做法）。

## 用户偏好（保持一致）
- 中文交流；结构化输出、用表格；方案先确认再动手；不主动创文档（除非要求）；可视化要浅色背景+深字+大字号、粒度粗。
