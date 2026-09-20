# 项目导航

本仓库是 WebRTC 端云语音平台：`rust_client/` 负责音频与交互，
`go_voice_gateway/` 负责实时传输，`gateway/` 负责 Python 编排，
`stt/`、`llm/`、`tts/` 提供模型服务，`server_config/` 管理运行配置。

- [4.0 交接说明](docs/v4-main-handoff.md)
- [启动说明](START.md)
- [系统地图](docs/project-context-for-ai.md)
- [文档导航](docs/README.md)

以上文档可按任务需要查阅，无固定阅读顺序。预检、Harness 和结构化证据工具
可按需使用，不是日常修改的前置要求，也不规定 AI 模型的工作步骤或回复格式。
