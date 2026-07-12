# CodeGraph 实现参考（06 · 增量同步与 Watcher）

> 自动保持图谱新鲜：文件变更 → debounce → `sync`。watcher 在 `src/sync/watcher.ts`，同步主逻辑在 `ExtractionOrchestrator.sync`（`src/extraction/index.ts:2305`）。

## 1. FileWatcher 设计原则（`watcher.ts:1-32`）

- **只用 Node 原生 `fs.watch`，无第三方 watcher、无原生 addon**。按平台选策略，使「打开描述符/内核 watch 成本」随文件数**有界**而非线性增长：
  - **macOS / Windows**：单个递归 `fs.watch(root, {recursive:true})`。libuv 映射为单个 FSEvents 流 / 单个 `ReadDirectoryChangesW` 句柄 → **O(1) 描述符**，无论树多大。这是修复 macOS 文件表耗尽（#644/#496/#555/#628）的关键：旧 watcher 在 macOS 每文件开一个 fd，数万 REG fd 撑爆 `kern.maxfiles` 拖垮全系统。
  - **Linux**：递归 `fs.watch` 不支持，故对**每个（非忽略）目录**各开一个 inotify watch → **O(目录数)** 而非 O(文件数)。新目录动态纳入；整体 watch cap 限制 inotify 用量（#579）。
- 忽略树（node_modules/dist/.git…）经 `buildScopeIgnore`（内置默认忽略 + 项目 .gitignore）过滤：Linux 不 descend（零 watch）；mac/Win 单递归流覆盖但事件在进入 sync 前丢弃。watcher 作用域 = 索引器作用域（#276/#407）。

## 2. 容错与降级（`watcher.ts:43-103`）

- `MAX_LOCK_RETRIES=5`（:48）：连续锁竞争重试上限，超则降级自动同步（短暂竞争不触发，长期外部写者触发）。
- `MAX_SYNC_FAILURE_RETRIES=5`（:58）：连续**非锁**同步失败上限（确定性失败：提取器崩某文件、DB 损坏、`SQLITE_FULL`、解析 OOM）。留无界会每 debounce 周期重试刷屏且自动更新保证悄然失效（#1127）；一次干净 sync 重置计数。
- `MAX_RETRY_BACKOFF_MS=30000`（:60）：指数退避上限。
- **EMFILE/ENFILE**（watch 资源耗尽，`isWatchResourceExhaustion`，:85-92）：**禁用自动同步**，给 `EXHAUSTION_REASON`（:63-65）→ 提示跑 `codegraph sync` 或装 git 钩子。
- **ENOSPC**（Linux inotify 计数耗尽，实际是 `fs.inotify.max_user_watches`，`isInotifyWatchExhaustion`，:101-103）：**非致命**，只 warn（已装 watch 仍工作，部分目录不自动同步），给 `INOTIFY_LIMIT_REASON`（:73-78）→ 提示 `sysctl` 调大后重启。
- `supportsRecursiveWatch()`（:110-112）：仅 `darwin`/`win32` 用递归策略，否则走 per-directory。

## 3. 触发链路

- 文件事件 → debounce（合并爆发式改动）→ 调 `ExtractionOrchestrator.sync()`（全量 reconcile，见下）。
- 也支持 **Git 钩子**（`src/sync/git-hooks.ts`）：在 commit/merge 时触发同步，作为 watcher 的补充或替代（无 watch 权限/服务器环境）。
- `watch-policy.ts`（:41 引用 `watchDisabledReason`）：决定某环境是否禁用 watch（如 CI、只读挂载）。
- `worktree.ts`：处理 git worktree（多工作树共享同一仓库时的同步边界）。

## 4. sync() 文件系统对账（非 git，`extraction/index.ts:2305`）

- **真相来源是「文件系统 vs 已索引状态」，永远不是 git**（注释 :2300-2304, :2322-2330）。
- 流程：
  1. 枚举当前源码文件。
  2. 与 DB 逐文件 reconcile：
     - **廉价 (size, mtime) stat 预筛**跳过未变文件（不读不 hash）（:2325-2327, :2405）。
     - 仅对疑似变更文件**读 + `hashContent`** 确认真变化（:2405-2419 修改/暂存；:2484-2503 新增/未跟踪 `??`；:2543-2548 删除）。
  3. 改动文件重走 `indexFileWithContent` → `storeExtractionResult`（含跨文件边复活，见 01 §5 / 02 §4），随后重跑解析（resolveAndPersist）绑定跨文件边。
- 价值：
  - 非 git 项目可用；
  - **能捕获 `git pull`/`checkout`/`merge`/`rebase` 提交后的工作区改动**——这些 `git status` 看不到（因为已是工作区内容）（:2302-2303）。

## 5. codegraph 复用要点 vs CodeWiki 增量方案（重要：两者偏离）

### 5.1 codegraph 自身（参考事实，不直接照搬）
1. 增量基于**文件系统** `(size,mtime)` 预筛 + `content_hash` 对账，**不依赖 git**（extraction/index.ts:2305）。
2. watcher 用原生 `fs.watch`，大仓库务必 macOS/Win 单递归、Linux per-directory + watch cap，避免 fd 耗尽。
3. 必须有失败退避与降级（锁竞争 / 同步失败 / 资源耗尽），否则自动更新会在确定性错误上无限刷屏。

### 5.2 CodeWiki 增量方案（2026-07-12 已对齐，故意偏离 codegraph 的 fs.watch）
CodeWiki **不监听工作区文件变更**，改为**监听本地 git 提交**：
1. **数据源 = 本地 git**（不碰 GitHub 远端）；只索引已提交代码，未提交改动不纳入。
2. **init 锁定分支**：首次建库记录当前 checkout 分支名，之后固定轮询该分支引用（`git rev-parse <branch>`），切到其他分支不误触发。
3. **触发 = 轮询分支引用**比对 `last_indexed_commit`；变更集用 `git diff --name-status C_old C_new`（A/M/D/R）。
4. **失败退避与降级仍须保留**（沿用 5.1 第 3 条）：锁竞争 / 同步失败 / 资源耗尽都要有重试上限与退避，否则自动更新在确定性错误上无限刷屏。
5. **重索引后一定重跑跨文件边复活**（删文件级联删边 → 快照跨文件入边 → 按 (filePath,kind,name) 重绑 → 失败则复活为 unresolved_ref）。
6. **非祖先提交兜底**：`git merge-base --is-ancestor C_old C_new` 为 false（强推/reset/rebase）时整库**全量重建**，保证图谱与代码一致。

---

## 系列文档索引

- `00-总览与架构.md` — 设计原则、三级主链路、文件地图、复用不变式
- `01-存储层与Schema.md` — 表结构、节点ID、边唯一性、unresolved 生命周期、跨文件边复活、name_segment_vocab、FTS5
- `02-建库提取管线.md` — indexAll、ParseWorkerPool、按序提交、storeExtractionResult、语言提取器契约、sync
- `03-引用解析.md` — resolveOne 策略链、createEdges 类型提升、import/name/callback/frameworks 子模块
- `04-图查询与遍历.md` — callers/callees/impact/findPath、visited-before-depth、批量取节点、provenance 感知
- `05-上下文构建与MCP-Explore.md` — 混合检索、explore 预算、buildFlow、动态边界
- `06-增量同步与Watcher.md` — fs.watch 平台策略、容错降级、sync 文件系统对账
