# CodeGraph 实现参考（05 · 上下文构建与 MCP Explore）

> 上下文构建 `src/context/index.ts`（`ContextBuilder`），MCP 主出口 `src/mcp/tools.ts`（`ToolHandler`）。这是 Agent 实际消费图谱的入口。

## 1. ContextBuilder.buildContext（:216）

```
buildContext(input, options):
  query = 字符串 | `${title}: ${description}`
  subgraph   = findRelevantContext(query, {...})   // 混合检索 + 图展开
  entryPoints= getEntryPoints(subgraph)
  codeBlocks = includeCode ? extractCodeBlocks(subgraph,...) : []
  relatedFiles = getRelatedFiles(subgraph)
  summary    = generateSummary(query, subgraph, entryPoints)
  stats      = {nodeCount, edgeCount, fileCount, codeBlockCount, totalCodeSize}
  return markdown | json | raw context
```

- markdown 输出 = `formatContextAsMarkdown` + `buildCallPathsSection` + 低置信度注记（:267-270）。
- 低置信度诚实交接 `buildLowConfidenceNote`（:285-306）：当检索主要靠常见词命中，明确告知 agent「这只是起点」，引导其用**确切符号名** `codegraph_explore` 或 `codegraph_search`，并给可能目录——避免 blind Read/Grep。
- `buildCallPathsSection`（:320-）：在内存里从 subgraph 的 `calls` 边直接 DFS 出调用链（最多 6 跳、budget 2000），把「X 怎么到 Y」bake 进 context 工具，免去 agent 再去发现独立 trace 工具（deferred-MCP 下 agent 不会主动 ToolSearch 陌生工具）。

## 2. findRelevantContext 混合检索（:432，HYBRID SEARCH）

顺序：

1. **抽取查询里的符号名** `extractSymbolsFromQuery(query)`（:451）。
2. **精确名匹配** `findNodesByExactName`（:459）：取 5× 限以免裁剪；
   - **共现提升**（co-location boost，:467-484）：多符号同文件 → 该文件结果 +`(count-1)*20`，更可能是用户想要的（如 `scrapeLoop`+`run` 同在 `scrape/scrape.go`）。
3. **定义前缀匹配**（:494-537）：用户写 `REST`/`bulk` 通常指 `RestController`/`BulkRequest`——
   - Title-case（`REST`→`Rest`）+ stem 变体（`caching`→`cache`）+ 限定 definition kinds（class/interface/struct/trait/protocol/enum/type_alias）；
   - brevity bonus（短名优先，核心类命名精简）。
4. **文本搜索（FTS5）**（:539-）：对自然语言 term 逐词搜，多词命中的结果提升；
   - 未设 kind 过滤时排除 import 节点（避免 `REST` 命中 44 万条 import 路径）（:550-552）。
5. **图展开**：以命中节点为入口，按 `traversalDepth` 沿边展开子图（得到 subgraph 的 nodes/edges）。

## 3. MCP ToolHandler 与默认工具集

- `DEFAULT_MCP_TOOLS = new Set(['explore'])`（`tools.ts:804`）：默认**只暴露 `codegraph_explore`**，刻意砍掉更窄的 search/node/trace（它们只是 explore 的子集；保留反而把 agent 引向窄工具、多轮往返）。
- 其他工具（search/node/callers/callees/trace/impact/files…）仍可实现，`CODEGRAPH_MCP_TOOLS=explore,node,...` 可重新启用（:797-804）。
- `codegraph_explore` 工具定义（:681），描述里含预算建议（:946-972）。

## 4. getExploreBudget（调用次数预算，:134）

按项目文件数推荐 explore 调用次数（避免小项目过度开销、大项目覆盖不足）：

```
<500      → 1
<5000     → 2
<15000    → 3
<25000    → 4
else      → 5
```

## 5. getExploreOutputBudget（输出预算，:192，自适应）

- 分级（tier 与 budget 对齐），核心不变量：**总字符上限必须 < agent 内联工具结果上限（~25K 字符）**。超限会被宿主外化到文件让 agent 再 Read，重新引入读 + 缓存写成本（#185，35K vscode explore 教训）。
- 大仓库保留慷慨默认（因为 agent 原生 grep+find+多 Read 成本远大于一次 fat explore）；小仓库收紧 `maxOutputChars`/`defaultMaxFiles`/`maxCharsPerFile`/`gapThreshold`，避免小项目一次 explore 倒一整文件源码。
- 小项目还会关掉 meta 文本（关系图/额外文件列表/完整性信号/预算注记）——一次 rich call 就是全部，多余散文是开销。
- `excludeLowValueFiles`：极小仓库直接硬删 test/spec/icon/i18n（默认只降权排序，但小仓库一个滑入就占满预算，如 cobra `command_test.go` 挤掉 `args.go`）。

## 6. handleExplore（:2492，核心出口）

- 取预算 `getExploreOutputBudget(cg.getStats().fileCount)`（:2517）。
- 自适应 sizing：默认开启 skeletonize（OFF-SPINE 流裁剪，OkHttp interceptor chain 28.5k→16.6k，省 ~28%）。
- 输出截断到预算；截断后明确写「上方源码完整逐字，已视作已 Read；未覆盖区域再跑一次 explore，别 Read 这些文件」（:3639-3679）。
- 末尾追加 explore 预算注记（:3645-3651）：「本项目 N 次调用预算，每次覆盖 ~6 文件，先花完剩余调用覆盖未覆盖区，再 fallback Read」。

## 7. buildFlowFromNamedSymbols（:1882，Flow 构建）

把 agent 的 explore 查询（一袋符号名/文件名/代码词）解析成调用流：

- **token 解析**（:1894-1898）：按空白/标点切，剥真实文件扩展名（`Create.cs`→`Create`，但**保留** `Class.method` 限定名——agent 最精确输入，按 `findAllSymbols` 精确解析）；
- **CALLABLE 集合**（method/function/component/constructor）：只把这些当调用链节点；
- **特异性判定**（:1937-1944）：限定名或 ≤3 个同名定义 → 直接保留；歧义简单名只保留「容器类也在查询里被命名」的候选（靠 `segPool` 段池消歧）；
- **动态边界**（:1929-1930, `dynNamed`）：对 `constant/variable/field/property` 这类非 callable 端点，若其 callers/callees 含 `provenance='heuristic'` 边则纳入，捕捉 RTK thunk（`const X = createAsyncThunk(...)`）等常量→常量 hop；
- **合成链表面**（:1969-1991）：收集命名符号上 incident 的 `heuristic` 边，作为「运行时在这些站点选目标」的动态派发边界提示（注册表/总线/反射），引导 agent 对候选再跑 explore。

## 8. 动态边界（dynamic-boundaries.ts）

- 识别运行时派发：注册表 / 插件 / 策略接口 / 总线。这些没有单一静态 caller→callee 边，实现即续延。
- 在 explore 输出里给出「该方法运行时分发到下列实现之一」说明，让 agent 知道要沿实现继续探索（:2201-2288 等模板）。

## 9. 给 CodeWiki 复用的要点

1. 检索是**混合**（精确名 + 前缀/stem + FTS + 共现提升 + 图展开），不是纯向量 RAG。
2. 输出必须**带预算上限且 < 内联结果上限**，并明确「已视作已 Read，勿再 Read」。
3. 跨语言自然语言兜底靠 `name_segment_vocab`（见 01 §6），不是关键词表。
4. 动态派发边界靠 `provenance` 合成边识别，输出里显式提示 agent「继续探索候选」。
