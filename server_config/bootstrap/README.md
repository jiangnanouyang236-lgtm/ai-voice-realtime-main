# Legacy Bootstrap

这个目录只用于 `server-config` 分支的初始化与迁移：

- [legacy_bot_config.yaml](/Users/wuzikang/Desktop/py/dify_stream_test/server_config/bootstrap/legacy_bot_config.yaml)
- [legacy_mcp_servers.yaml](/Users/wuzikang/Desktop/py/dify_stream_test/server_config/bootstrap/legacy_mcp_servers.yaml)

用途：

- 新机器初始化数据库
- 从旧的 Bot / MCP 配置迁移到数据库
- 初始化时同步本地 `llm/agents/*.py` 自动发现到 `agents` 表，并按 Bot 配置写入 Agent 绑定

约束：

- 运行中的 LLM / Gateway 不应该再直接从这里加载配置
- 这两个文件是 bootstrap source of truth，不是 runtime source of truth
- Agent 的运行时可用性以数据库中的全局启用状态和 Bot 绑定为准
