# CodeGraph 工作流

这个项目已经有本地 CodeGraph 索引：`.codegraph/codegraph.db`。

`.codegraph/*.db` 是本机生成数据，不提交 Git；我们只提交查询脚本和工作流说明。

## 推荐日常路线

### Codex 会话

Codex 优先用 MCP 工具：

1. `codegraph_explore`：先按自然语言、文件名、符号名找入口和源码上下文。
2. `codegraph_node`：对某个定义精查源码、调用链和位置。
3. `codegraph_callers` / `codegraph_callees` / `codegraph_impact`：改代码前看影响面。

MCP 适合我在改 Gateway、Rust Client、LLM 编排、协议消息、状态机前快速摸清结构。

### 本地命令

人手操作或 MCP 不可用时，用提交到仓库里的只读脚本：

```bash
python scripts/codegraph_query.py health
python scripts/codegraph_query.py inspect handle_text
python scripts/codegraph_query.py changed
python scripts/codegraph_query.py review ClientMessage
```

这四个命令分别回答：

- `health`：索引是否存在、覆盖多少文件、是否有未解析引用、是否有文件比索引更新。
- `inspect <symbol>`：这个符号在哪里、谁调用它、它调用了什么。
- `changed`：当前 Git 改动文件里有哪些已索引符号，适合提交前快速扫一眼。
- `review <symbol>`：围绕一个核心符号生成 CodeReview 检查面。

## Codex MCP 接入状态

本机 CodeGraph CLI 安装在：

```text
/Users/wuzikang/.nvm/versions/node/v22.15.0/bin/codegraph
```

Codex MCP 配置在 `~/.codex/config.toml`：

```toml
[mcp_servers.codegraph]
command = "/Users/wuzikang/.nvm/versions/node/v22.15.0/bin/codegraph"
args = ["serve", "--mcp"]
```

如果当前会话里搜不到 CodeGraph MCP 工具，通常不是项目索引问题，而是 Codex 工具清单没有热加载。
重启 Codex 或新开会话后再检查：

```bash
codex mcp list
codex mcp get codegraph
```

CLI 可用性可以这样验证：

```bash
/Users/wuzikang/.nvm/versions/node/v22.15.0/bin/codegraph status .
/Users/wuzikang/.nvm/versions/node/v22.15.0/bin/codegraph query handle_text --path .
```

## 适合用 CodeGraph 的场景

- 改动 Gateway、Rust Client、LLM 编排这类高耦合代码前，先看符号和调用关系。
- CodeReview 时检查新增函数是否只有预期入口调用，避免隐式链路漏看。
- 合并分支前做影响面分析，尤其是同名函数、状态机、协议消息、MCP 工具分发。
- 排查边界 bug 时，从一个函数向上查 caller，或向下查 callee。
- 找复杂函数热点，优先审查高出度函数。

## 仍然优先用 rg 的场景

- 查日志文案、环境变量、配置 key、提示词片段。
- 查 JSON/YAML/Markdown 纯文本内容。
- 确认某个字符串是否完全残留。

CodeGraph 看结构，`rg` 看文本。两者配合用，不互相替代。

## 常用命令

查看索引健康：

```bash
python scripts/codegraph_query.py health
```

查看索引覆盖明细：

```bash
python scripts/codegraph_query.py summary
```

刷新索引：

```bash
/Users/wuzikang/.nvm/versions/node/v22.15.0/bin/codegraph sync .
```

搜索符号：

```bash
python scripts/codegraph_query.py search handle_text
python scripts/codegraph_query.py search "EnvironmentTriggerQueue OR process_llm_tts_stream"
```

一次性查看符号位置、调用方、被调用方：

```bash
python scripts/codegraph_query.py inspect handle_text
```

看谁调用某个符号：

```bash
python scripts/codegraph_query.py callers handle_text
```

看某个符号调用了什么：

```bash
python scripts/codegraph_query.py callees handle_text
```

看复杂热点：

```bash
python scripts/codegraph_query.py hotspots --limit 20
```

看某个文件里的符号：

```bash
python scripts/codegraph_query.py file rust_client/src/app.rs
```

看当前 Git 改动文件里的已索引符号：

```bash
python scripts/codegraph_query.py changed
python scripts/codegraph_query.py changed gateway/gateway_server.py rust_client/src/app.rs
```

围绕一个符号生成评审检查面：

```bash
python scripts/codegraph_query.py review ClientMessage
```

## 推荐流程

### 开发前

1. 用 `python scripts/codegraph_query.py health` 确认索引可读。
2. 用 MCP 的 `codegraph_explore` 或本地 `inspect <symbol>` 找结构入口。
3. 用 `rg` 找配置 key、日志文案、环境变量、提示词片段。
4. 明确 caller/callee 影响面后再开始改代码。

### CodeReview

1. 对新增或修改的核心函数跑 `python scripts/codegraph_query.py review <symbol>`。
2. 对状态机、协议、队列、线程相关函数补跑 `inspect <symbol>`。
3. 对热点函数跑 `hotspots`，优先审查复杂路径。
4. 最后再跑编译、测试和业务验证。

### 合并分支前

1. 跑 `python scripts/codegraph_query.py changed` 看当前改动覆盖了哪些符号。
2. 对被改动的核心符号跑 `review <symbol>` 或 MCP 的 `codegraph_impact`。
3. 用 `rg` 检查环境变量、默认 URL、提示词、工具名是否残留旧版本。
4. 跑项目对应的编译/测试命令。

## 本项目优先关注的符号

这些符号通常代表跨模块链路，改动前值得先查：

```bash
python scripts/codegraph_query.py inspect handle_text
python scripts/codegraph_query.py inspect ClientMessage
python scripts/codegraph_query.py inspect EnvironmentTriggerQueue
python scripts/codegraph_query.py inspect process_llm_tts_stream
python scripts/codegraph_query.py inspect _select_robot_tool_name
```

## 注意事项

- CodeGraph 是快照索引，改代码后需要重新生成索引，查询结果才会包含最新变更。
- 如果 CodeGraph 没有覆盖某类文件，继续用 `rg` 和编译检查兜底。
- 查询脚本默认只读打开数据库，不会写入或刷新索引。
- `changed` 默认读取 Git 工作区、暂存区、未跟踪文件；也可以显式传文件路径。
