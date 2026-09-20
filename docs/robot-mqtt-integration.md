# 机器人 MQTT 集成

机器人控制是 LLM/MCP 的外部能力，不属于 Rust/Go/Python 语音 transport。当前入口为 `mcp_servers/robot_mqtt_api.py` 与 `mcp_servers/robot_sse_server.py`。

## 调用链

```text
LLM tool call
  -> MCP robot server
  -> robot_mqtt_api payload builder
  -> MQTT broker
  -> target robot topic
```

语音链路只负责把用户意图交给 LLM，并把工具结果转换为可播报文本；Go Gateway 不解析机器人动作。

Robot MQTT payload 的顶层 `meta` 是审计上下文，可包含跨轮 `session_id` 和逐轮 `trace_id`。它只由程序为 Robot 工具注入，不进入 LLM 可见 schema，也不得扩散到 WebSearch、Task、Utils 等非 Robot 工具。`session_id` 不能替代 `trace_id`。

## 配置

```text
ROBOT_MQTT_HOST=
ROBOT_MQTT_PORT=1883
ROBOT_MQTT_USERNAME=
ROBOT_MQTT_PASSWORD=
ROBOT_MQTT_TOPIC_PREFIX=windaka
ROBOT_MQTT_DEFAULT_ROBOT_ID=
```

- 默认 host/username/password 为空，未配置时发布必须失败关闭。
- 真实 broker、账号和密码只能来自未跟踪 `.env` 或密钥系统。
- 文档、日志、Bot prompt 和 MCP 返回值不得包含 credential。
- `robot_id` 优先来自工具参数或当前 Robot 上下文；默认值只用于明确的单设备调试。

## Topic

Topic 由 `ROBOT_MQTT_TOPIC_PREFIX`、`robot_id` 和任务/设备后缀拼接。以代码中的 builder 为准，不在 prompt 中复制完整 topic 规则，避免协议漂移。

常用能力分为：

- 移动/停止等 manual control。
- 交互动作。
- 视频呼叫：`{prefix}/{robot_id}/mcp/device/call`，`msg_id=7`，`method=/device/call`，`action=1`、`code=1` 由服务端固定填写；发布成功后语音侧发送 `[EXIT]` 回到等待唤醒，发布失败时不退出。
- 设备或任务控制。
- 任务服务查询/创建（如果绑定远端 task service）。

动作到数字类型的映射由代码维护，并可通过 `ROBOT_MQTT_TYPE_*` 环境变量适配其它机器人平台。

## 错误处理

- MQTT 未配置、连接失败或 publish 失败时，MCP 返回结构化错误。
- LLM 不应把内部 JSON、topic 或异常堆栈直接送入 TTS。
- 工具失败后应给用户简短可理解的话术，并保留 trace 中的详细诊断。
- 需要动作确定性的短语优先走确定性 Router，模糊请求再交给模型分类。

## 认知报告上报

认知报告使用独立配置，不能复用机器人控制密码：

```text
COGNITIVE_REPORT_MQTT_ENABLED=false
COGNITIVE_REPORT_MQTT_HOST=
COGNITIVE_REPORT_MQTT_PORT=1883
COGNITIVE_REPORT_MQTT_USERNAME=
COGNITIVE_REPORT_MQTT_PASSWORD=
COGNITIVE_REPORT_MQTT_TOPIC_PREFIX=
```

默认应关闭或在 host 为空时失败关闭。生产启用前确认 topic、QOS、消息 ID 和数据合规要求。

## 最小验证

```bash
python -m pytest test/test_sensitive_defaults.py test/test_robot_video_call.py test/test_utils_sse_server.py
```

接真实 broker 前先检查：

- 使用测试 Robot/topic，避免误控生产设备。
- 日志脱敏。
- 超时和重试有上限。
- 相同 trace 能关联 LLM tool call、MCP 调用和 MQTT publish 结果。
- 同一 session 连续两轮产生不同 trace，且两个启用 Bot 均遵守同一规则。
- 非 Robot MCP 工具参数中不存在 `_meta`。
