# P2 MCP 查询服务 — Spec

> P1 图谱 API → FastMCP stdio Server，8 个工具。Agent 可 init/sync/查询图谱。

---

## 收敛目标

Agent 通过 MCP stdio 调用 8 个工具，可完成代码理解任务（查符号、看调用链、评估影响范围、搜索符号），且 `explore` 输出在项目规模自适应的字符预算内。

---

## 范围

| | 做 | 不做 |
|---|---|---|
| **查询** | node / callers / callees / impact / search / explore（6 个只读） | 不暴露内部 SQL / 原始数据结构 |
| **构建** | init（全量图谱，无 Wiki 生成）/ sync（P2 走全量重建，后续 P3 替为 git diff 增量） | 不做 Wiki 生成、不做 AGENTS.md 注入 |
| **传输** | stdio（FastMCP 默认） | 不做 HTTP/SSE/Streamable HTTP |
| **语言** | 复用 P1 的 Java 提取器，后续按需加 | 不做多语言自动切换 |
| **日志** | 重定向到 `.codewiki/mcp.log` 文件 | stdout 输出非 MCP 协议内容 |
| **项目发现** | 启动时从 CWD 往上找 `.codewiki/` | 不支持运行时切换项目路径 |
| **explore 预算** | 按索引文件数自适应裁剪输出（见下表） | 不返回超过预算的原始数据 |

---

## 约束

1. MCP Server 进程存活期从 CWD 发现一次项目，之后不再切换
2. stdio 模式的 stdout 只输出 JSON-RPC（MCP 协议），所有日志写 `.codewiki/mcp.log`
3. 查询工具遇到未初始化项目 → 返回文本提示，**不崩溃**
4. init 必须幂等（重复 init 等价于 sync，不翻倍节点/边）
5. explore 输出字符数不超过预算上限；超预算时展示最相关节点并标注截断

### explore 预算算法（对齐 codegraph getExploreOutputBudget）

| 索引文件数 | 最大输出字符 | 最大展示节点（callers + callees 各占 1/3） |
|---|---|---|
| <500 | 8,000 | 20 |
| <5,000 | 15,000 | 40 |
| <15,000 | 20,000 | 60 |
| ≥15,000 | 25,000 | 80 |

---

## 验收标准

| # | 验收项 | 验证方法 |
|---|---|---|
| 1 | MCP Server stdio 启动，注册 8 个工具 | MCP Inspector 连接，`tools/list` 返回 8 个 |
| 2 | `codewiki_node("DiscountService")` 返回完整节点详情 | 输出含 qualified_name/file_path/signature/docstring/visibility |
| 3 | `codewiki_callers("getRate")` 返回调用方（含 calculateDiscount） | 结果列表含 `calculateDiscount` |
| 4 | `codewiki_callees("calculateDiscount")` 返回被调用方（含 getRate + Order） | 结果列表含 `getRate` 和 `Order` |
| 5 | `codewiki_impact("Order")` 返回影响半径 | 结果含依赖方，不含内部 method/field（排除 contains） |
| 6 | `codewiki_search("Discount")` 返回 FTS5 排序结果 | 结果含 score + file:line |
| 7 | `codewiki_explore("calculateDiscount")` 返回组合上下文 | 输出含 node 详情 + callers + callees + 预算注记 |
| 8 | `codewiki_init` 构建图谱（无 Wiki 生成） | DB 含 nodes/edges/files/unresolved_refs，返回 stats |
| 9 | 未初始化项目调用查询工具不崩溃 | 返回文本提示"use codewiki_init first" |
| 10 | stdout 无非 MCP 协议输出 | stderr 或 `.codewiki/mcp.log` 可有日志，stdout 只有 JSON-RPC |

---

## Done Contract

P2 完成 = 10 条验收全部通过 + MCP Inspector 可正常调用所有工具。
