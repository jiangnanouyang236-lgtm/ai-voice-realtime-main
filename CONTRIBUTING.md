# Contributing

这个仓库目前是私有项目，主要目标是稳定推进语音机器人链路。贡献时优先保证线上可回滚、配置可解释、日志可排查。

## 开发流程

1. 按任务查阅相关模块文档；环境预检可按需使用。
2. 从 `main` 新建分支。
3. 单一主题提交，避免把客户端、服务端、Admin UI、文档大范围混在一个 commit。
4. 根据改动选择相关测试；仓库检查可使用 `python scripts/check_repo_hygiene.py`。
5. 在 PR 里说明改动范围、配置变更和验证结果。

日常说明改动内容、实际执行的检查和未验证范围即可，无固定标签或 JSON 要求。
正式验收需要结构化报告时，可使用 `docs/evidence-gates.md` 与
`python scripts/validate_evidence_report.py <report.json>`。

推荐分支名：

```text
codex/<short-feature-name>
fix/<short-bug-name>
chore/<short-maintenance-name>
```

## 验证建议

开发依赖：

```bash
pip install -r requirements-dev.txt
```

以下命令按涉及模块选用，无需每次全部执行。

Python 编译检查：

```bash
python -m compileall gateway llm mcp_servers server_config stt tts scripts -q
```

Rust 客户端：

```bash
cd rust_client
cargo check
```

Admin UI：

```bash
cd admin-ui/frontend
npm ci
npm run build
```

运行时链路改动还应人工验证：

- Rust 客户端硬件唤醒。
- ASR -> LLM -> TTS 一轮对话。
- MCP 工具调用。
- 需要 `[EXIT]` 的长任务是否回到待机。
- Admin UI Save + Apply 后 runtime 是否切换成功。

## 配置约定

- 新部署优先使用 `LLM_API_KEY`、`LLM_MODEL_NAME`、`LLM_BASE_URL`。
- WebSearch 使用 `WEBSEARCH_API_KEY`。
- 生产 TTS 使用 `QWEN3_TTS_CUSTOM_VOICE_API_KEY` / `QWEN3_TTS_CUSTOM_VOICE_WS_URL`；Base Profile 使用对应 `QWEN3_TTS_BASE_*` 变量。
- `.env` 不提交，`.env.example` 只放示例值。
- 数据库里的 MCP Header 可以使用 `${ENV_NAME}` 占位符。

## 文档约定

如果改动影响部署、环境变量、端口、MCP 工具、Agent 触发方式或客户端行为，请同步更新：

- `README.md`
- `START.md`
- `.env.example`
- `docs/deployment-checklist.md`
- 相关子目录 README

## 不要提交

- `.env` 和任何真实密钥。
- `logs/`、`run/`、`nohup.out`。
- `target/`、`node_modules/`、`__pycache__/`。
- 模型文件、录音采集、临时调试输出。
