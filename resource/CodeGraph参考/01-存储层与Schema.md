# CodeGraph 实现参考（01 · 存储层与 Schema）

> 权威表结构见 `src/db/schema.sql`。所有证据来自该文件与 `src/db/queries.ts`、`src/extraction/tree-sitter-helpers.ts`、`src/extraction/index.ts`。

## 1. 表总览

| 表 | 作用 | 关键列 |
|---|---|---|
| `nodes` | 代码符号（函数/类/变量/接口…） | `id` PK, `kind`, `name`, `qualified_name`, `file_path`, `language`, 行列, `docstring`, `signature`, `visibility`, `is_exported/async/static/abstract`, `decorators`, `type_parameters`, `return_type`, `updated_at` |
| `edges` | 节点间关系 | `id` PK AUTOINC, `source`, `target`, `kind`, `metadata`(JSON), `line`, `col`, `provenance` |
| `files` | 被索引的源文件 | `path` PK, `content_hash`, `language`, `size`, `modified_at`, `indexed_at`, `node_count`, `errors` |
| `unresolved_refs` | 待解析引用（含 status 生命周期） | `from_node_id`, `reference_name`, `reference_kind`, `line`, `col`, `candidates`, `file_path`, `language`, `status`, `name_tail` |
| `name_segment_vocab` | 标识符分词表（实现检索的语义兜底） | `segment`, `name`（WITHOUT ROWID，PK=(segment,name)） |
| `nodes_fts` | FTS5 全文索引（虚表） | 覆盖 `name/qualified_name/docstring/signature` |
| `project_metadata` | 项目级元信息（版本/溯源） | `key` PK, `value`, `updated_at` |
| `schema_versions` | schema 版本追踪 | `version` PK |

## 2. 节点 ID 生成（内容寻址）

`src/extraction/tree-sitter-helpers.ts:18-30`：

```ts
export function generateNodeId(filePath, kind, name, line): string {
  const hash = crypto
    .createHash('sha256')
    .update(`${filePath}:${kind}:${name}:${line}`)
    .digest('hex')
    .substring(0, 32);
  return `${kind}:${hash}`;
}
```

- 32 字符（128-bit 截断）避免大仓库冲突；前缀 `kind:` 便于肉眼区分。
- **关键推论**：行号一变 → ID 变。所以重索引后旧边引用的旧 ID 全部失效，必须用 `(filePath, kind, name)` 重匹配（见 §5 跨文件边复活）。

## 3. edges 表要点

- **外键级联**：`FOREIGN KEY(source) REFERENCES nodes(id) ON DELETE CASCADE`、`target` 同理（`schema.sql:54-55`）。删一个文件会级联删掉所有「源或目标」在该文件的边。
- **边唯一性索引**（`schema.sql:173-174`，迁移 v6 加入）：
  ```sql
  CREATE UNIQUE INDEX idx_edges_identity
    ON edges(source, target, kind, IFNULL(line,-1), IFNULL(col,-1));
  ```
  `IFNULL(line,-1)` 让无坐标的合成边也能去重（否则 SQLite 把每个 NULL 当不同值，导致重复边灌入 callers/impact，#1034）。`insertEdge` 用 `INSERT OR IGNORE`。
- **`provenance` 列**（`schema.sql:53`，索引 `idx_edges_provenance:187`）：标记边来源。静态 AST 边为空；框架/回调/启发式合成边标 `framework:xxx` / `callback` / `heuristic`。下游遍历与 MCP 展示据此区分。
- 索引策略（#156-163）：删掉了窄的 `idx_edges_source`/`idx_edges_target`，只保留 `(source,kind)` 与 `(target,kind)` 复合索引，靠左前缀扫描覆盖 source-only / target-only 查询，减少写放大。

## 4. unresolved_refs 表生命周期（#1240）

`src/db/schema.sql:70-92` 注释明确：

- 提取阶段以 `status='pending'` 插入。
- 一次解析完成后：解析成功的行**被删除**；解析失败（attempted, no match）的行标记为 `status='failed'` 并**保留**（`name_tail` = `reference_name` 最后一段，如 `util.greet`→`greet`）。
- 保留失败行的目的：后续 `sync` 时若有文件变更引入能匹配的新符号，重试这些失败引用（`name_tail` 让重试能按点号引用匹配新节点名）。
- 行跟随其 `from_node_id` 走 `ON DELETE CASCADE`——重提取或删文件会清掉它在该文件下的所有残留行（任意状态）。
- 索引：`idx_unresolved_failed_tail`（仅在 `status='failed'` 上建 `name_tail` 索引，:186），加速失败重试查找。

## 5. 跨文件边复活（#899 / #1240，关键增量陷阱）

`src/extraction/index.ts:2177-2265`（在 `storeExtractionResult` 内）：

- **问题**：`deleteFile` 级联删掉「源或目标」在旧文件的边。其中「源在别的（未变）文件、目标在当前重索引文件」的入边（如 `pkg.mod.fn()` 的调用方）不会被提取器重发，会**静默丢失**。
- **解决**：删除前先 `getCrossFileIncomingEdgesWithTarget(filePath)` 快照这些跨文件入边（带目标节点的 `kind`/`name`）。删除 + 重插当前文件节点后，用 `newNodesByKindName`（`kind\0name` → 新 id）把每条边的目标重解析到新 id 重插。
- **行号漂移**：因为 ID 含行号，callee 文件任何 docstring 编辑都会让所有目标 id 变；按 `(filePath,kind,name)` 匹配则稳定跨越行漂移。
- **重命名/删除的符号**：若新文件里找不到同名目标，则该边**复活为原始 unresolved_ref**（由 `resurrectRefFromDroppedEdge`，边元数据里 stamped `refName`/`refKind`），交给同一次 sync 的解析阶段重绑或标记 `failed`——保证「一致的重索引结果」。

> CodeWiki 增量更新**必须**实现同样逻辑，否则跨文件调用边会在每次 re-index 后断裂。

## 6. name_segment_vocab（语义检索兜底）

- 表定义 `schema.sql:149-153`：`WITHOUT ROWID`，PK=(segment,name)。
- **写入时机**：随 `insertNode` 同事务写入（`src/db/queries.ts:331-342`），用 `splitIdentifierSegments(name)` 拆词。所以词汇永远与 nodes 同步，不会超前。
- **排除规则**：`file` 与 `import` 两种 kind 不写（`isSegmentableKind`，:348-350）——文件名重复内部符号、import 节点命名是模块路径而非符号，都会污染稀有度统计。
- **删除故意留孤儿**：行是「提案」，使用前总在 nodes 上重新校验；全量索引开头会清表。
- 分词算法 `src/search/identifier-segments.ts:30-47`：`splitIdentifierSegments` 处理 camelCase/PascalCase（`OrderStateMachine`→order/state/machine）、acronym（`HTMLParser`→html/parser）、snake/kebab/点号；下限 2 字符、上限 32、每名字最多 12 段、纯数字段丢弃。
- 用途：让自然语言 prompt 词（任何拉丁脚本语言）能与图谱符号名比对（跨语言语义兜底），FTS5 的 tokenizer 保留驼峰整体单 token 所以做不到，才在写入期物化分词。

## 7. FTS5 全文索引

`src/db/schema.sql:108-134`：

- `nodes_fts` 虚表，`content='nodes'`、`content_rowid='rowid'`，覆盖 `name/qualified_name/docstring/signature`。
- 三个触发器（`nodes_ai`/`nodes_ad`/`nodes_au`）保持 FTS 与 nodes 同步（删改时先 delete 再 insert）。
- BM25 列权重在 `src/db/queries.ts:1225` 附近：`id=0, name=20, qualified_name=5, docstring=1, signature=2`——名字权重最高。

## 8. 写入路径与事务

- `insertNodes`（`queries.ts:370-376`）：`db.transaction(() => { for node insertNode(node) })`。
- `insertNode`（:270-343）：预编译 `INSERT OR REPLACE`，缺失必填字段（id/kind/name/filePath/language）直接跳过并打 error；写入后清 `nodeCache` 该行防脏读。
- **分块 + 让出事件循环**：`storeExtractionResult` 用 `STORE_CHUNK = 2000`（`index.ts:2168`），每批插入后 `await onYield?.()`，避免超大文件（数万符号）一次长事务卡死主线程与看门狗心跳（#850），同时让 WAL 背压有机会 checkpoint（#1231）。
- 文件记录最后落库（`upsertFile`），所以「部分存储的文件没有记录」→ 崩溃恢复时该文件会被重新索引。

## 9. 内容哈希与变更检测

- `hashContent`（`index.ts:120-122`）：`crypto.createHash('sha256').update(content).digest('hex')`。
- `files.content_hash` 用于「该文件内容未变则跳过存储」（`storeExtractionResult:2172-2175`）。
- `sync()` 用「(size,mtime) 预筛跳过未变文件 → 仅对疑似变更文件读+hash 确认」（见 02 建库文档 §同步）。
