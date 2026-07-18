# CodeWiki

本地代码智能引擎：解析 Java 源码 → 知识图谱 → LLM 生成 Wiki → MCP 供 AI 检索。

## 能力一览

| 能力 | 说明 |
|---|---|
| **知识图谱** | tree-sitter 解析 Java 源码，提取类/方法/字段/接口，构建调用、继承、注入关系图 |
| **Wiki 生成** | 调用 LLM 自动写出模块文档、架构概览、快速开始、交叉索引 |
| **MCP 查询** | 启动 `codewiki serve`，AI 可通过 `codewiki_explore` 查询任意符号的 caller/callee/impact |
| **增量同步** | `codewiki wiki sync` 基于 git 变更自动更新受影响的 Wiki 文档 |

## 快速开始

### 1. 安装

```bash
pip install -e ".[wiki]"
```

### 2. 配置 LLM（只需一次）

```bash
codewiki config set --api-key <你的 key> [--model <模型名>] [--base-url <兼容地址>]
```

- 默认模型 `deepseek-v4-flash`，默认地址 `https://api.deepseek.com/v1`
- 支持任意 OpenAI 兼容接口（DeepSeek / 通义千问 / GLM / 本地模型等）
- 密钥只存 `<项目>/.codewiki/config.json`，绝不写入目标项目目录
- 查看当前配置：`codewiki config`

### 3. 初始化项目

进入你的 Java 项目目录，执行：

```bash
cd my-java-project
codewiki init
```

自动完成：
1. 解析源码 → 构建知识图谱
2. 解析交叉引用 + Spring DI 注入关系
3. 生成项目地图
4. 调用 LLM 生成 Wiki 文档
5. 渲染校验 + 写入 `agent.md` 导航入口

产出在 `.codewiki/` 下（已自动 gitignore）：

| 文件 | 说明 |
|---|---|
| `.codewiki/codewiki.db` | 知识图谱 |
| `.codewiki/wiki/index.md` | Wiki 入口索引 |
| `.codewiki/wiki/modules/*.md` | 各模块文档 |
| `.codewiki/wiki/architecture.md` | 系统架构 |
| `.codewiki/wiki/quickstart.md` | 快速开始 |
| `agent.md` | AI 导航入口（链接 wiki） |

## MCP 接入

### 启动 MCP 服务器

```bash
codewiki serve
```

### 客户端配置

在 MCP 客户端（CodeBuddy / Cursor / Claude 等）中添加：

```json
{
  "mcpServers": {
    "codewiki": {
      "command": "codewiki",
      "args": ["serve"]
    }
  }
}
```

### 可用工具

| 工具 | 说明 |
|---|---|
| `codewiki_explore(target)` | 传入符号名/文件路径，返回节点详情 + callers + callees + impact + 源码 |

## 命令清单

| 命令 | 作用 |
|---|---|
| `codewiki config [set]` | 管理全局 LLM 配置 |
| `codewiki init` | 建图谱 + 生成 Wiki（在当前目录） |
| `codewiki node <name>` | 查询单个符号详情 |
| `codewiki callers <name>` | 查看调用者 |
| `codewiki callees <name>` | 查看被调用者 |
| `codewiki impact <name>` | 查看爆炸半径 |
| `codewiki search <keyword>` | 全文搜索符号 |
| `codewiki explore <target>` | CLI 下完整探索 |
| `codewiki sync` | 增量同步图谱 |
| `codewiki wiki sync` | 增量更新 Wiki |
| `codewiki serve` | 启动 MCP 服务器 |

## 技术栈

Python 3.10+ · tree-sitter · SQLite · LangGraph · deepagents · MCP (FastMCP)
