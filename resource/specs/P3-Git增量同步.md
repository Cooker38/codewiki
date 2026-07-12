# P3 Git 增量同步 — Spec

> P2 的 `sync` 替换为真正的增量：只重处理 Git 变更文件，非祖先提交降级全量重建。

---

## 收敛目标

`codewiki sync`（手动或 auto_sync 自动触发）只重处理自上次索引以来 Git 变更的文件。非祖先提交自动全量重建。sync 期间查询工具返回"维护中"提示。

---

## 范围

| | 做 | 不做 |
|---|---|---|
| **触发** | 手动 `codewiki sync`；auto_sync 每 10min 轮询 `git rev-parse <branch>` | 不做 fs.watch、不做 GitHub webhook |
| **变更检测** | `git diff --name-status C_old C_new`（A/M/D/R） | 不做文件 stat/mtime 对账 |
| **增量重提取** | A/M 文件重提取；R 删旧+加新；D 删文件+级联边；不变文件跳过 | 不重建不变文件 |
| **跨文件边复活** | 删文件前快照跨文件入边 → 按 (filePath, kind, name) 重绑 → 失败复活为 unresolved_ref | 不自创逻辑（沿用 codegraph storeExtractionResult） |
| **引用解析** | 定向重跑——只解析受变更文件影响的 unresolved_refs | 不全量重跑 |
| **合成层** | 对变更文件的节点重跑 type_of/returns/overrides | 不全量合成 |
| **非祖先兜底** | `git merge-base --is-ancestor C_old C_new` 为 false → 全量重建 | 不强行增量 |
| **commit 指针** | 存 `project_metadata`（key=`last_indexed_commit`），和图谱同一事务 | 不存 config.json |
| **并发控制** | sync 期间写锁，查询工具返回"维护中" | 不排队等待 |
| **auto_sync 开关** | `.codewiki/config.json` 的 `auto_sync` 字段（默认 true） | — |

---

## 约束

1. commit 指针必须和图谱数据原子落盘——同一事务
2. 非祖先提交 → 全量重建，不信任 diff
3. sync 写锁期间查询工具拒绝服务，不排队
4. auto_sync 轮询间隔 10min
5. 增量不改变 MCP 工具签名——P2 的 8 个工具不变
6. 跨文件边复活逻辑对齐 codegraph `storeExtractionResult`

---

## 验收标准

| # | 验收项 | 验证方法 |
|---|---|---|
| 1 | 首次 init 记录 commit | `project_metadata` 的 `last_indexed_commit` 非空 |
| 2 | 无变更 sync 零操作 | 对同一 commit 调两次 sync，files_indexed=0 |
| 3 | 新增文件入图谱 | 加 .java 文件 → commit → sync → files 表有新文件 |
| 4 | 修改文件重索引 | 改注释 → commit → sync → 节点 start_line 更新 |
| 5 | 删除文件清边 | 删 .java 文件 → commit → sync → files 表清 + edges 清（级联） |
| 6 | 跨文件边复活 | 改被调用的方法行号 → commit → sync → 调用方的 calls 边不丢 |
| 7 | 非祖先降级全量 | `git reset --hard HEAD~1` → sync → 触发全量重建 |
| 8 | sync 期间查询拒绝 | sync 锁中调 node → 返回"维护中" |
| 9 | auto_sync 周期性触发 | 提交新文件，等待 ≤10min → 自动 sync 完成 |
| 10 | idempotent | 同一 commit 多次 sync，图谱完全一致 |

---

## Done Contract

P3 完成 = 10 条验收全部通过 + sync/auto_sync 在测试靶上运行正常。
