# Developer Tools

本目录放置人工诊断工具，不属于 pytest 测试树。

- `chat_llm_console.py`：连接已启动的 LLM gRPC `StreamChat` 做交互诊断。
- `stun_probe_go/`、`webrtc_probe_go/`：网络路径探测源码。

运行交互控制台前先执行对应 Profile 的 `scripts/ai_preflight.py`，并确认目标服务属于当前环境；端口可连接本身不能证明服务来自当前 checkout。
