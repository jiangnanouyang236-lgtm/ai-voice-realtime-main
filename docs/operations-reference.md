# 运维与配置参考（当前主线）

> 本文档覆盖当前 V4 测试主线的日常运维、配置、V4 拆分 Python Compose、
> Go/Python 兼容 Compose、WebRTC/STUN/TURN 探测和验证入口。
>
> 与本文档互补：
> - 架构见 [`architecture-and-design.md`](./architecture-and-design.md)
> - Docker Compose v3 细节见 [`docker-compose-v3.md`](./docker-compose-v3.md)
> - Docker Compose V4 细节见 [`docker-compose-v4.md`](./docker-compose-v4.md)
> - Go Gateway 内部细节见 [`go-voice-gateway.md`](./go-voice-gateway.md)
> - 决策背景见 [`decisions.md`](./decisions.md)

---

## 1. 环境矩阵

### 1.1 Python 版本与依赖

- Python 3.11（与 `docker/python-services.Dockerfile` 一致）
- 依赖列表：`requirements.txt`
- venv：`env/`（项目根级 venv，已 gitignore）

```bash
# 重建 venv
python3.11 -m venv env
source env/bin/activate
pip install -r requirements.txt
```

### 1.2 Go 版本

- `go_voice_gateway/go.mod` 指定最低 Go 版本
- 构建产物：`go_voice_gateway/dist/`

### 1.3 Rust 工具链（生产客户端）

- 详见 `rust_client/PACKAGING.md`
- 特性开关：`--features native-webrtc`（当前主线使用）

---

## 2. 配置矩阵（来自 `.env.example`）

> **重要**：`.env.example` 含占位符和兼容默认值。生产部署必须替换占位符，
> 并通过运行进程配置与 Runtime Snapshot 确认最终值。

### 2.1 LLM 服务

| 变量 | 默认 | 说明 |
|---|---|---|
| `LLM_API_KEY` | `your-llm-api-key-here` | 本地 vLLM 时为占位符；DashScope 兼容模式时为真实 key |
| `LLM_MODEL_NAME` | `qwen3-5-9b` | 主推理模型 |
| `LLM_BASE_URL` | `http://127.0.0.1:15101/v1` | OpenAI 兼容 endpoint；远端部署显式覆盖 |
| `LLM_ROUTER_API_KEY` | 空 | Router 分类模型 key（留空复用主 LLM） |
| `LLM_ROUTER_MODEL_NAME` | 空 | Router 模型名（留空复用主 LLM） |
| `LLM_ROUTER_BASE_URL` | 空 | Router endpoint |
| `LLM_TOOL_LATENCY_EXPERIMENT` | `true` | 实验开关：闲聊绕过 tools / 高置信工具先播过渡语；`false` 回退旧逻辑 |
| `LLM_TOOL_ROUTER_CLASSIFIER` | `true` | Router 分类器启用 |
| `LLM_TOOL_ROUTER_CLASSIFIER_MAX_TOKENS` | `1` | 分类输出单字符路由码 |
| `LLM_TOOL_ROUTER_CLASSIFIER_TIMEOUT_MS` | 代码默认 `500`；当前 example/runtime 覆盖 `3000` | 分类超时保护；调优时必须记录实际环境值 |
| `LLM_COMPLEX_WORKFLOW_ENABLED` | `false` | 注册独立 WorkflowService；必须与 Python Gateway 开关同时启用 |
| `LLM_VISION_GATEWAY_TOKEN` | 空 | 视觉取图内部令牌；未配置时不启用视觉意图取图 |
| `LLM_GRPC_SERVER_PORT` | `50053` | gRPC 监听端口 |
| `LLM_GRPC_MAX_WORKERS` | `10` | gRPC worker 池 |
| `LLM_SERVICE_URL` | `grpc://127.0.0.1:50053` | Gateway 调用 LLM gRPC 的 URL（admin DB 配置优先） |

视觉问题（如“这是什么”“前面有什么”“你看到了什么”）命中后，LLM
按 `session_id` 从 Go Gateway 读取最新 JPEG。本轮使用图片后只持久化文本，
普通聊天、Robot MCP、Agent 和运控请求不会携带图片。Docker Compose 部署时
Compose 部署需在未跟踪的环境文件中设置 `VISION_INTERNAL_TOKEN`；裸进程启动时
`LLM_VISION_GATEWAY_TOKEN` 与 `GO_VOICE_GATEWAY_VISION_INTERNAL_TOKEN` 必须解析为
同一随机值。令牌不一致时 Vision 取图会被拒绝。

### 2.2 STT 服务

| 变量 | 默认 | 说明 |
|---|---|---|
| `STT_GRPC_SERVER_PORT` | `50054` | gRPC 监听端口 |
| `STT_GRPC_MAX_WORKERS` | `10` | gRPC worker 池 |
| `STT_SERVICE_URL` | `grpc://127.0.0.1:50054` | Gateway 调用 STT gRPC 的 URL |
| `STT_AUDIO_CONTEXT_TO_LLM` | `true` | 是否把情绪/声音事件短上下文注入 LLM |

### 2.3 TTS 服务

| 变量 | 默认 | 说明 |
|---|---|---|
| `TTS_GRPC_SERVER_PORT` | `50052` | gRPC 监听端口 |
| `TTS_GRPC_MAX_WORKERS` | `5` | gRPC worker 池 |
| `TTS_SERVICE_URL` | `grpc://127.0.0.1:50052` | Gateway 调用 TTS gRPC 的 URL |
| `LOCAL_QWEN3_TTS_VOICE` | `serena` | 默认 voice |
| `LOCAL_QWEN3_TTS_SUPPORTED_VOICES` | `serena,aiden,dylan,eric,ono_anna,ryan,sohee,uncle_fu,vivian` | 允许 voice 列表 |
| `LOCAL_QWEN3_TTS_INSTRUCTIONS` | 风格 prompt | TTS 风格指令 |
| `QWEN3_TTS_CUSTOM_VOICE_WS_URL` | `ws://127.0.0.1:15120/v1/audio/speech/stream` | CustomVoice Profile 使用的 WS endpoint |
| `QWEN3_TTS_CUSTOM_VOICE_API_KEY` | 空 | CustomVoice Profile 使用的 API key |
| `QWEN3_TTS_CUSTOM_VOICE_MODEL` | `qwen3-tts` | CustomVoice Profile 使用的模型名 |
| `QWEN3_TTS_BASE_WS_URL` | `ws://127.0.0.1:15121/v1/audio/speech/stream` | Base Profile 使用的 WS endpoint |
| `QWEN3_TTS_BASE_API_KEY` | 空 | Base Profile 使用的 API key |
| `QWEN3_TTS_BASE_MODEL` | `qwen3-tts-base` | Base Profile 使用的模型名 |
| `LOCAL_QWEN3_TTS_SOURCE_SAMPLE_RATE` | `24000` | 源采样率 |
| `LOCAL_QWEN3_TTS_CONNECT_TIMEOUT_SEC` | `8` | WS 连接超时 |
| `LOCAL_QWEN3_TTS_RECV_TIMEOUT_SEC` | `60` | 接收超时 |

TTS Profile 由 Admin UI 的 `TTS Profiles` 页面维护。Bot Template 只保存 `tts_profile_id`，TTS gRPC 请求只携带该 ID；CustomVoice/Base 的 Provider 字段由 TTS 服务从配置数据库解析。

### 2.4 Gateway 服务（Python WebSocket）

| 变量 | 默认 | 说明 |
|---|---|---|
| `GATEWAY_REQUIRE_ROBOT_SECRET` | `false` | `false` 兼容旧客户端；`true` 强制 robot_secret 校验 |
| `GATEWAY_BIND_HOST` | `0.0.0.0` | 网关监听地址 |
| `GATEWAY_BIND_PORT` | `7860` | 网关监听端口 |
| `GATEWAY_COMPLEX_WORKFLOW_ENABLED` | `false` | 启用复杂任务预分类、顺序执行和播放屏障；必须与 LLM 开关同时启用 |
| `GATEWAY_WORKFLOW_RPC_TIMEOUT_SEC` | `30` | Workflow 规划和步骤 unary RPC 超时 |
| `GATEWAY_WORKFLOW_STREAM_TIMEOUT_SEC` | `120` | Workflow 综合回答流超时 |
| `GATEWAY_WORKFLOW_PLAYBACK_TIMEOUT_SEC` | `30` | 终止动作前等待客户端播放完成的超时 |
| `SINGING_CATALOG_PATH` | `singing/catalog.json` | 版本化歌曲目录，保存歌名、别名、资产路径和 SHA-256 |
| `SINGING_VOICES_PATH` | `singing/voices.json` | 唱歌音色及 `tts_profile_id` 显式映射 |
| `SINGING_DEFAULT_VOICE_ID` | `serena-v1` | 仅用于无请求上下文的本地 MCP 调试 |
| `SINGING_AUDIO_ROOT` | 空 | Python Gateway 读取的歌曲成品根目录；歌曲为 16kHz mono PCM16 WAV |
| `SINGING_AUDIO_HOST_DIR` | `../../singing/audio` | Compose 只读挂载的宿主机歌曲目录；生产建议使用绝对路径 |
| `SINGING_AUDIO_V4_HOST_DIR` | `./singing/audio` | 根 V4 Compose 使用的宿主机歌曲目录；生产必须使用绝对路径 |
| `ROBOT_ID` | 空 | 客户端 fallback robot_id |
| `ROBOT_SECRET` | 空 | 客户端 fallback robot_secret |
| `GATEWAY_URL` | `ws://127.0.0.1:8282/ws` | 客户端连接地址；设备侧显式覆盖 server IP |

AI 唱歌不经过 TTS 模型现场合成：MCP 只选择歌曲，Python Gateway 在准备语之后以同一
`round_id` / `playback_id` 分块发送预生成 PCM，Go Gateway 继续负责 Opus 封包与下行。歌曲分块
最多预送约 300ms，新一轮语音打断会取消当前播放。Compose 使用 `SINGING_AUDIO_HOST_DIR`
将宿主机成品目录只读挂载到 `/app/singing/audio`。`singing_remote` 与 Robot MCP 独立，提供
`play_song` / `list_songs`；当前版本不支持续唱。

### 2.5 Admin UI

| 变量 | 默认 | 说明 |
|---|---|---|
| `ADMIN_USERNAME` | `admin` | 登录用户名（生产必须改） |
| `ADMIN_PASSWORD` | `replace-with-a-strong-password` | 登录密码（生产必须改） |
| `ADMIN_SESSION_SECRET` | 长随机串 | session 签名密钥（生产必须改） |
| `ADMIN_SESSION_TTL_SECONDS` | `43200` | session 有效期（12 小时）|
| `ADMIN_COOKIE_SECURE` | `false` | HTTPS 部署改成 `true` |
| `ADMIN_AUTH_DISABLED` | `false` | 临时本地调试可设 `true`，生产必须 `false` |
| `GATEWAY_INTERNAL_BASE_URL` | `http://127.0.0.1:7860` | Admin 后端访问 Gateway runtime 的地址 |
| `CONFIG_DATABASE_URL` | `mysql://config_user:...@db.wzk.icu:13306/wzk_ai_voice?charset=utf8mb4` | server_config 数据库 |

### 2.6 客户端 VAD / 打断

| 变量 | 默认 | 说明 |
|---|---|---|
| `VAD_MODE` | `2` | VAD 模式 |
| `VAD_SPEECH_START_MIN_DBFS` | `-36` | WebRTC VAD 判为语音后仍需达到的最低电平；录音中也用于判断语音是否持续 |
| `VAD_SPEECH_THRESHOLD` | `0.25` | 开始录音的语音判定窗口长度（秒），不是能量阈值 |
| `VAD_SPEECH_TRIGGER_RATIO` | `0.6` | 上述窗口内至少被判为语音的帧比例 |
| `VAD_SILENCE_THRESHOLD` | `0.5` | 录音开始后连续非语音达到该时长即发送 `audio_end` |
| `VAD_MAX_RECORDING_DURATION` | `10` | 最大录音时长（秒） |
| `TURN_GATE_SHADOW_ENABLED` | `false` | Rust Client 独立开关；仅发送 provisional candidate，不改变 `audio_end` |
| `TURN_GATE_ACTIVE_ENABLED` | `true` | Rust Client 独立 Active 开关；显式设为 `false` 可回退，Go/Python 不继承该默认值 |
| `TURN_GATE_CANDIDATE_SILENCE_MS` | `300` | Shadow/Active 模式下每个静音周期产生一次 candidate 的等待时间 |
| `TURN_GATE_MIN_COMMIT_SILENCE_MS` | `600` | Active 双 True 最早提交静音点；模型可提前计算，恢复说话会取消 pending commit |
| `TURN_GATE_FAILURE_FALLBACK_MS` | `1000` | Active 模式下模型失败、超时、不可用或反馈丢失时的静音回退；Shadow/VAD-Only 不使用 |
| `TURN_GATE_AMBIGUOUS_HARD_TIMEOUT_MS` | `1600` | Active 模式下收到匹配 candidate 的 `continue` 后使用的静音硬上限 |
| `GO_VOICE_GATEWAY_TURN_GATE_SHADOW_ENABLED` | `false` | Go Gateway 独立 Shadow 开关，不继承其他进程变量 |
| `GO_VOICE_GATEWAY_TURN_GATE_ACTIVE_ENABLED` | `false` | Go Gateway 独立 Active 开关，不继承其他进程变量 |
| `GO_VOICE_GATEWAY_TURN_GATE_ACTIVE_DEADLINE_MS` | `200` | 从 Go 收到 candidate 开始计的 Active 绝对提交窗口；测试 Profile 显式设为 `300`，过期只 fallback |
| `GO_VOICE_GATEWAY_TURN_GATE_SHADOW_TIMEOUT_MS` | `5000` | Go 到 Python 的候选请求总预算；使用独立连接，不占用正式 ASR bridge |
| `GO_VOICE_GATEWAY_TURN_GATE_SHADOW_SNAPSHOT_GRACE_MS` | `30` | candidate watermark 已领先 RTP 队列时的非阻塞尾包等待；不延迟正常到齐的 candidate |
| `GATEWAY_TURN_GATE_SHADOW_ENABLED` | `false` | Python Gateway 独立 Shadow 开关，不继承其他进程变量 |
| `GATEWAY_TURN_GATE_ACTIVE_ENABLED` | `false` | Python Gateway 独立 Active 开关，不继承其他进程变量 |
| `GATEWAY_TURN_GATE_MODELS_ENABLED` | `false` | Python 启用 Smart Turn 与 LiveKit EOU；只有 Shadow 或 Active 同时开启时才预热和推理 |
| `GATEWAY_TURN_GATE_SMART_MODEL_PATH` | `turn-gate/models/smart-turn-v3.2/smart-turn-v3.2-cpu.onnx` | Python 使用的 Smart Turn V3.2 ONNX；生产环境应显式指向固定 hash 的外部模型 |
| `GATEWAY_TURN_GATE_EOU_MODEL_DIR` | `turn-gate/models/livekit-eou-v0.4.1-intl` | Python 使用的 LiveKit multilingual EOU 模型与 tokenizer 目录 |
| `GATEWAY_TURN_GATE_SMART_THRESHOLD` | `0.5` | Python Smart Turn Complete probability 阈值，判断语义为 `probability > threshold` |
| `GATEWAY_TURN_GATE_EOU_THRESHOLD` | `0.0066` | Python LiveKit 中文官方阈值，判断语义为 `probability >= threshold` |
| `TTS_PREBUFFER_SEC` | `0.10` | Rust 客户端 TTS 预缓冲 |
| `WAKE_HTTP_ENABLED` | `true` | 本地 HTTP 唤醒开关 |
| `WAKE_HTTP_HOST` | `127.0.0.1` | 唤醒 HTTP 监听（LAN 改 `0.0.0.0` 并确保可信） |
| `WAKE_HTTP_PORT` | `5202` | 唤醒 HTTP 端口 |
| `WAKE_HTTP_PATH` | `/wakeup` | 唤醒 HTTP 路径 |
| `INTERRUPT_USE_WAKE_WORD` | `true` | 必须唤醒词打断；`false` 能量阈值打断 |
| `PLAYBACK_WAKE_WORD_ARM_SEC` | `0.8` | 唤醒词打断生效延迟（秒） |

### 2.7 MCP 与机器人 MQTT

| 变量 | 默认 | 说明 |
|---|---|---|
| `MCP_ENABLED` | `false` | 全局 MCP 开关 |
| `ROBOT_MQTT_HOST` | 空 | 机器人 MQTT broker |
| `ROBOT_MQTT_PORT` | `1883` | MQTT 端口 |
| `ROBOT_MQTT_USERNAME` | 空 | MQTT 用户名 |
| `ROBOT_MQTT_PASSWORD` | 占位 | MQTT 密码（生产必须改） |
| `ROBOT_MQTT_TOPIC_PREFIX` | `windaka` | MQTT topic 前缀 |
| `ROBOT_MQTT_DEFAULT_ROBOT_ID` | `companion_01` | 工具未传 robot_id 时的默认值；不填回退 `ROBOT_ID` |
| `COGNITIVE_REPORT_MQTT_ENABLED` | `true` | 认知早筛报告上报开关 |
| `COGNITIVE_REPORT_MQTT_HOST` | 同 ROBOT_MQTT_HOST | 独立 broker 可不同 |
| `COGNITIVE_REPORT_MQTT_PORT` | `1883` | |
| `COGNITIVE_REPORT_MQTT_USERNAME` | 同上 | |
| `COGNITIVE_REPORT_MQTT_PASSWORD` | 占位 | |
| `COGNITIVE_REPORT_MQTT_TOPIC_PREFIX` | `windaka` | |
| `COGNITIVE_REPORT_TOPIC_SUFFIX` | `device/cog_ass_report` | topic 后缀 |
| `COGNITIVE_REPORT_METHOD` | `/device/cog_ass_report` | JSON-RPC method |
| `COGNITIVE_REPORT_MSG_ID` | `7` | JSON-RPC msg id |

### 2.8 v3 WebRTC / Go Gateway

完整 `.env.example` 中的 WebRTC/Go Gateway 兼容默认（生产必须复核并显式覆盖敏感项）：

```bash
# 客户端连接（Rust）
GATEWAY_URL=ws://${SERVER_IP}:8282/ws   # Go Gateway 使用 host network，直接监听宿主机 8282
TRANSPORT_POLICY=webrtc_only
WEBRTC_ENABLED=true
TRANSPORT_FALLBACK_ENABLED=false
WEBRTC_OFFER_FACTORY=native
RTC_AUDIO_UPLINK=rtp_only
WEBRTC_NATIVE_RTP_PROBE_ENABLED=false
WEBRTC_NATIVE_ICE_NETWORK_TYPES=udp4
WEBRTC_CONNECT_TIMEOUT_MS=15000
RESPONSE_TIMEOUT=45

# Go Gateway 服务端
GO_VOICE_GATEWAY_ADDR=127.0.0.1:8282
GO_VOICE_GATEWAY_RTC_ENABLED=true
GO_VOICE_GATEWAY_ICE_SERVERS=
GO_VOICE_GATEWAY_ICE_TRANSPORT_POLICY=all
GO_VOICE_GATEWAY_ICE_NETWORK_TYPES=udp4
GO_VOICE_GATEWAY_REQUIRE_ROBOT_SECRET=true
GO_VOICE_GATEWAY_DEVICE_AUTH_BACKEND=python_gateway
GO_VOICE_GATEWAY_ASR_PROCESSOR=python_gateway
GO_VOICE_GATEWAY_ASR_PYTHON_GATEWAY_WS_URL=ws://127.0.0.1:7860/ws
GO_VOICE_GATEWAY_ASR_PYTHON_GATEWAY_ROBOT_ID=companion_01
GO_VOICE_GATEWAY_ASR_PYTHON_GATEWAY_ROBOT_SECRET=
GO_VOICE_GATEWAY_ASR_PYTHON_GATEWAY_CONNECTION_MODE=session
GO_VOICE_GATEWAY_DOWNLINK_AUDIO_TRANSPORT=webrtc_rtp
GO_VOICE_GATEWAY_RTC_UDP_MIN_PORT=35500
GO_VOICE_GATEWAY_RTC_UDP_MAX_PORT=35600
```

`GO_VOICE_GATEWAY_ICE_SERVERS` 留空时只使用公共 STUN。生产跨 NAT 场景必须
通过未跟踪 `.env` 或密钥系统注入 TURN URL、用户名和 credential；源码和文档
不能保存真实 TURN 凭据。

### 2.9 其他

| 变量 | 默认 | 说明 |
|---|---|---|
| `DASHSCOPE_API_KEY` | 占位 | 旧云端 TTS 对比测试 |
| `WEBSEARCH_API_KEY` | 占位 | 联网搜索 key |

---

## 3. Makefile 目标速查

```
help                                # 列出全部目标
check                               # 当前仓库门禁；check-v3 为兼容别名
package-v3-release                  # 构建 Go linux/amd64 + Rust linux/arm64 release 包
clean-v3-dist                       # 清理 Go/Rust 打包产物
clean-local-artifacts               # 清理缓存、日志、Rust target 与 ESP-IDF build 工作目录
print-v3-config-help                # 打印 Go/Rust 端配置诊断命令
compose-network                     # 确保共享 Docker 网络存在（wzk-ai-net）
compose-v3-config                   # 渲染 v3 核心 config（business + go-gateway）
compose-v3-up                       # 一键启动 v3 核心栈
compose-v3-logs                     # tail go-gateway + python-gateway 日志
compose-v3-smoke                    # 烟测已发布端口
compose-v3-down                     # 停止 v3 核心栈
compose-business-up                 # 只启动 STT/LLM/TTS/Python Gateway/Admin
compose-go-up                       # 只启动 Go Gateway host-network 入口
compose-mcp-up                      # 只启动本地 MCP helpers
compose-v4-config                   # 渲染 V4 拆分 Python 服务
compose-v4-up                       # 启动 V4，不挂载源码
compose-v4-dev-up                   # 启动 V4，只读挂载源码
models-config                       # 渲染 vLLM 模型层 config
models-up                           # 启动 vLLM 模型层（vllm-stt/llm/router/tts）
models-logs                         # tail 模型层日志
models-down                         # 停止模型层
```

### 3.1 常用流程

```bash
# V4 测试主线冷启动（模型层 + Python 服务 + Go Gateway）
cp deploy/env.v3.compose.example .env
cp deploy/vllm/.env.models.example deploy/vllm/.env.models
# 编辑两份 .env 填入真实路径/key/IP
make models-up
make compose-v4-up
make compose-go-up

# 烟测
python -m pytest test/test_compose_v4_contract.py -q

# 查看实时日志
make compose-v4-logs

# 释放全部
make compose-go-down
make compose-v4-down
make models-down
```

---

## 4. Docker Compose 启停

V4 拆分 Python 服务是当前测试主线；`compose-v3-*` 保留为 Go + Python 兼容入口。
两套入口不能在同一主机同时占用相同端口。

### 4.1 网络准备

```bash
make compose-network
```

创建/复用 `wzk-ai-net`（变量 `AI_VOICE_DOCKER_NETWORK=wzk-ai-net`）。

### 4.2 业务层启动

```bash
make compose-v3-up
```

等价于：
```bash
docker compose -f deploy/compose/business.yml -f deploy/compose/go-gateway.host.yml up -d
```

如果只想分层启动：

```bash
make compose-business-up
make compose-go-up
```

### 4.3 启动 MCP helpers

本地 MCP 默认不启用（生产应使用 DB 驱动 MCP runtime config）。如需本地 MCP：

```bash
make compose-mcp-up
```

Bot MCP server 指向：
- `http://mcp-utils:5004/mcp`
- `http://mcp-robot:5003/mcp`
- `http://mcp-singing:5005/mcp`

专用天气 MCP 已移除；天气查询由 Runtime Snapshot 绑定的 Websearch MCP 提供。

### 4.4 端口矩阵

| 服务 | 容器端口 | 宿主机默认 | 用途 |
|---|---|---|---|
| go-gateway | 8282 | **host net**（无端口映射）| Rust 信令入口 |
| go-gateway WebRTC UDP | 35500–35600/udp | host net | Pion ICE/media 候选端口 |
| python-gateway | 7860 | `127.0.0.1:7860` | Python WebSocket Gateway |
| stt | 50054 | `127.0.0.1:50054` | STT gRPC |
| llm | 50053 | `127.0.0.1:50053` | LLM gRPC |
| llm admin | 18053 | `127.0.0.1:18053` | LLM admin HTTP |
| tts | 50052 | `127.0.0.1:50052` | TTS gRPC |
| tts admin | 18052 | `127.0.0.1:18052` | TTS admin HTTP |
| admin | 18100 | `127.0.0.1:18100` | Admin UI |

> 通过 `AI_VOICE_HOST_BIND_IP=0.0.0.0` 可把 7860/50053/50054/50052/18100 暴露到 LAN。Go Gateway 必须 host network，不要加 `ports:`。

### 4.5 配置渲染

```bash
make compose-v3-config
# 等价
docker compose -f deploy/compose/business.yml -f deploy/compose/go-gateway.host.yml config
```

输出会展开环境变量；含真实 key 时视作敏感。

---

## 5. 客户端启动（Rust）

### 5.1 当前默认（WebRTC-only）

```bash
cd rust_client
export GATEWAY_URL=ws://<server-ip>:8282/ws
export ROBOT_ID=test_01
export ROBOT_SECRET='...'
export TRANSPORT_POLICY=webrtc_only
export WEBRTC_ENABLED=true
export WEBRTC_OFFER_FACTORY=native
export RTC_AUDIO_UPLINK=rtp_only
cargo run --bin rust_client --features native-webrtc --release
```

### 5.2 配置诊断

```bash
# 打印有效配置
cargo run --bin rust_client --features native-webrtc -- --print-config

# 打印环境变量映射
cargo run --bin rust_client --features native-webrtc -- --print-env-help
```

### 5.3 Mirror 模式（排障）

```bash
export RTC_AUDIO_UPLINK=mirror_ws
```

只走 WS 音频，便于单独验证 WebRTC 信令 vs 音频路径。

---

## 6. WebRTC / STUN / TURN 探测

### 6.1 Go Gateway 自带健康检查

```bash
docker exec go-gateway /go_voice_gateway --healthcheck-url http://127.0.0.1:8282/healthz
```

### 6.2 STUN 探测工具（`tools/stun_probe_go`）

```bash
cd tools/stun_probe_go
go build -o stun_probe .
./stun_probe -server stun:stun.example.com:3478
```

输出本机公网/内网映射、UDP/TCP 路径、反射延迟。

### 6.3 WebRTC 完整路径探测（`tools/webrtc_probe_go`）

```bash
cd tools/webrtc_probe_go
go build -o webrtc_probe .
./webrtc_probe -turn turn:turn.example.com:3478 -user ... -credential ...
```

跑完整 ICE 协商（host/srflx/relay）+ RTP 收发 + DataChannel ping。

### 6.4 ICE 路由诊断

Go Gateway `/internal/status` 暴露当前 session 的 selected ICE pair 分类：

```json
{
  "selected_pair": {
    "local_type": "host",
    "remote_type": "srflx",
    "classification": "stun_udp",
    "rtt_ms": 23
  }
}
```

分类定义见 [`go-voice-gateway.md`](./go-voice-gateway.md) 与 `ice_route.go`。

### 6.5 弱网矩阵检查

默认只跑离线、可重复的弱网相关单测：RTP 乱序/丢包/重复、`audio_end`
后的尾包 grace、队列满丢弃、ICE route 分类、session close/cancel，以及 Rust
native ICE/downlink RTP 处理。

```bash
make weak-network-check
```

需要连真实 Go Gateway 做 STUN/direct 探测：

```bash
RTC_ROUTE_PROBE_WS_URL=wss://gateway.example.com/ws \
RTC_ROUTE_PROBE_ROBOT_ID=test_01 \
RTC_ROUTE_PROBE_ROBOT_SECRET="$ROBOT_SECRET" \
python scripts/webrtc_weak_network_matrix.py --live-route-probe
```

需要同时强制 TURN-only 对比：

```bash
python scripts/webrtc_weak_network_matrix.py --live-route-probe --live-turn-only
```

### 6.6 详细文档

见 [`webrtc-stun-tun-probes.md`](./webrtc-stun-tun-probes.md) 的历史探测记录。

### 6.7 M1 内部语音生产模式

Go Gateway 由一个总开关决定 Go-Python 语音协议所有权：

| 模式 | 作用 |
| --- | --- |
| `m1` | 默认生产路径。真实语音、`client_event`、打断和播放回报统一走 `/internal/voice/ws`。 |
| `shadow` | M0 `/ws` 继续处理业务，M1 只接收旁路协议流量。 |
| `m0` | 明确回滚。关闭 Go 侧 M1 session，全部恢复到兼容 `/ws` bridge。 |

部署 M1 不需要额外设置模式；仅当 Python Gateway 地址不是同机默认地址时，
设置 `GO_VOICE_GATEWAY_INTERNAL_VOICE_WS_URL`。Python Gateway 需保持
`GATEWAY_INTERNAL_VOICE_WS_ENABLED=true` 和
`GATEWAY_INTERNAL_VOICE_CLIENT_EVENT_MODE=active`。

`GO_VOICE_GATEWAY_INTERNAL_VOICE_WS_URL` 必须从 Go Gateway 进程所在的网络
命名空间可达：Go 使用 host network 且 Python Gateway 7860 映射到主机时，
可以使用 `ws://127.0.0.1:7860/internal/voice/ws`；如果 Go/Python 都在
Docker bridge 网络里，应改成 Python Gateway 的 compose service 名或容器网
络地址。若日志出现 `internal_voice_session_open_failed` 且错误是
`connect: connection refused`，说明 M1 未接管。真实语音一旦向 M1 提交，
不会自动重放到 M0，避免重复 ASR/回答；应修复连接或执行显式回滚。

`GO_VOICE_GATEWAY_ASR_PYTHON_GATEWAY_SESSION_IDLE_RECYCLE_MS=30000` 表示
空闲 30 秒后在下一轮前回收 Python Gateway session WS；需要保持到客户端断开
时可显式设为 `0`。

旁路验证：

```bash
export GO_VOICE_GATEWAY_INTERNAL_VOICE_MODE=shadow
```

生产回滚：

```bash
export GO_VOICE_GATEWAY_INTERNAL_VOICE_MODE=m0
```

回滚只需重启 Go Gateway，不需要关闭 Python 的 M1 endpoint。旧的
`*_WS_ENABLED`、`*_CLIENT_EVENT_MODE`、`*_INPUT_AUDIO_MODE`、
`*_INTERRUPT_ENABLED` 和 `*_PLAYBACK_REPORT_ENABLED` 环境变量仍可被解析，
用于旧部署迁移；生产路由以 `GO_VOICE_GATEWAY_INTERNAL_VOICE_MODE` 为准。
Go `/internal/status` 和 `--print-config` 会显示最终生效的总模式及各子路径。

需要隔离 Go Gateway、直接检查 Python Gateway 时，使用以下两个保留 probe：

```bash
# 只读状态 + M1 session.open/session.close；增加 --client-event 才会触发业务响应
python scripts/probe_python_gateway_internal_voice.py --base-url http://127.0.0.1:7860

# 使用已生成的 Opus packet JSON 验证兼容 /ws 音频路径
python scripts/probe_python_gateway_ws_audio.py \
  --robot-id test_01 \
  --packets-path tmp/live-opus-packets.json
```

二者用于 direct Python 与 Go WebRTC 对照，不是测试数据生成器；输入和输出均留在
ignored 的 `tmp/`/`reports/`，不得提交。

需要把 direct Python `client_event` 固化为可复核 Hybrid 证据时，使用
`scripts/run_dev_harness.py --scenario voice-client-event`。该场景只启动隔离的
TTS gRPC 与 Python Gateway；`client_event` 固定短语按设计不调用 LLM。
Gateway 强制 Robot Secret 时，Runner 仅从当前私有环境或未跟踪的
`rust_client/.env.local` 注入凭据；目标
`ROBOT_ID` 不一致或凭据缺失时直接拒绝，且凭据不会进入报告或日志。

需要验证 Router、主 LLM 与 TTS 的真实串联时，使用
`scripts/run_dev_harness.py --scenario voice-direct-text`。Runner 固定发送无副作用文本，
要求 Router 分类器真实调用、主 LLM 非空响应、零工具调用和同 trace VAF1 音频；
该入口绕过 ASR，不能表述为完整 Voice E2E。

`scripts/run_dev_harness.py --scenario voice-agent-continuity` 会在同一 Python M1
session 依次提交“记忆有点跟不上”和“好，开始吧”，检查认知 Agent 状态连续、两轮
真实 TTS 音频以及没有 M0 `/ws` 分叉。该诊断绕过 ASR/VAD 与 Go/Rust，不升级为
Voice E2E、部署或硬件证据。

`scripts/run_dev_harness.py --scenario voice-utils-time` 会补齐本地
`utils_remote:5004` 编排，覆盖全部启用 Bot 的时间工具路由与事实一致性，
并额外验证 `test_01` 的 Gateway/TTS 音频闭环。该场景只读，不产生外部写入。

`scripts/run_dev_harness.py --scenario voice-robot-actions` 会在固定 `test_01`
上运行 greet/cheer 两轮 Gateway 文本请求，同时订阅精确 MQTT Topic，验证
Router/LLM/Robot MCP/MQTT/TTS 的同 trace 闭环。必须显式提供 connectivity、
external-write 和授权引用；不得改用随机 Robot ID 或表述为实体硬件验证。

`scripts/run_dev_harness.py --scenario voice-audio` 使用哈希绑定的仓库固定参考录音，
启动本地 STT/LLM/TTS/Python Gateway，通过 M1 active-audio 验证 ASR 到下行音频。
运行机必须有 `ffmpeg` 和可加载的 `opuslib/libopus`；该场景无外部写入。

---

## 7. 单测与基准

### 7.1 顶层入口

```bash
make check-v3
```

等价于：
```bash
cd go_voice_gateway && make test
cd rust_client && make test
python -m pytest test/test_voice_logging.py
```

### 7.2 Python 全量单测

```bash
source env/bin/activate
python -m pytest test/ -v
```

关键测试分组：
- `test/test_llm_*` / `test_llm_tool_routing.py` / `test_tool_router_v2.py` — LLM 行为
- `test/test_gateway_*` — Gateway 行为
- `test/test_stt_*` / `test/test_tts_*` — STT/TTS 行为
- `test/test_sensitive_defaults.py` — **安全**：默认无敏感凭据
- `test/test_mcp_manager_timeouts.py` — MCP 超时
- `test/test_trace_recorder.py` — trace recorder
- `test/test_cognitive_agent_entry.py` — 认知 Agent 入口

### 7.3 基准测试

```bash
# ASR streaming
python scripts/benchmark_asr_streaming.py

# TTS providers
python scripts/benchmark_tts_providers.py

# LLM → TTS chain
python scripts/benchmark_llm_tts_chain.py

# LLM 模型对比
python scripts/benchmark_llm_model_compare.py

# 多模态 LLM：文本、图片、混合回答及图片+工具调用
python scripts/benchmark_multimodal_llm.py \
  --image scene=/path/to/pic.jpeg \
  --expect scene=机器人,桌子

# 评估 router classifier
python scripts/eval_acceptance.py --output reports/eval-acceptance.json
```

旧评测数据与历史基准文档已从工作树删除，必要时从 Git 历史追溯；ignored 的 `reports/`、`tmp/` 原始证据不能视为当前基准。新基准必须同时保留运行参数、环境和可复核结果。
多模态基准只直接请求模型接口，不经过 Gateway、Robot MCP 或会话历史；结果写入
`tmp/multimodal_llm_benchmark/`，图片本身不会复制到结果目录。

### 7.4 Voice 延迟基线

```bash
python scripts/run_voice_latency_baseline.py
python scripts/collect_gateway_trace_baseline.py
```

结果应以当前 trace 指标和同环境对照组为准，不复用已失效的 2026-06 快照。

---

## 8. Gateway 内部诊断端点

| 端点 | 用途 |
|---|---|
| `GET /` | 简单 ping |
| `GET /healthz` | 健康检查 |
| `GET /stats` | 进程统计 |
| `GET /internal/config/status` | 当前生效配置 |
| `POST /internal/config/reload` | 触发 reload（可选 `?version=N`） |
| `GET /internal/config/validate` | 校验某版本配置 |
| `GET /internal/runtime/robots` | 运行时机器人列表 |
| `GET /internal/runtime/sessions/{session_id}` | 单 session 状态 |
| `GET /internal/runtime/traces` | trace 列表 |
| `GET /internal/runtime/traces/{trace_id:path}` | 单 trace 详情 |

trace summary/detail 会返回 `diagnosis` 字段，包含 `severity`、`bottleneck_*`、
`signals` 和最多 8 个 latency breakdown 项。排查首响慢时优先看
`diagnosis.bottleneck_label` / `diagnosis.bottleneck_ms`，再展开事件时间线。

> 默认绑定 `127.0.0.1:7860`，仅本机可访问。

---

## 9. 烟测脚本

### 9.1 v3 Compose 烟测

```bash
make compose-v3-smoke
```

等价于：
```bash
python scripts/smoke_compose_v3.py
```

校验端口可达性 + 关键 endpoint 响应。

### 9.2 Go WebRTC ↔ Python Gateway 桥接烟测

```bash
# Rust native WebRTC client 验证
python scripts/smoke_go_webrtc_python_gateway.py --sample "$SAMPLE_WAV" --client rust
```

详细路径与期望输出见 [`go-voice-gateway.md`](./go-voice-gateway.md) 与 [`decisions.md`](./decisions.md) 2026-06-27 验证进展。

---

## 10. 排障指南（按症状）

### 10.1 Rust 客户端连不上 Go Gateway

1. 检查 `GATEWAY_URL`：Go Gateway 使用 host network，默认是 `ws://<host-ip>:8282/ws`
2. `docker logs go-gateway | head -50` 看信令握手
3. `curl http://<host>:8282/healthz` 验证服务在线
4. `GO_VOICE_GATEWAY_REQUIRE_ROBOT_SECRET` 设为 `false` 临时跳过鉴权

### 10.2 WebRTC ICE 协商失败

1. `tools/stun_probe_go` 与 `tools/webrtc_probe_go` 跑一遍
2. 主机防火墙与上游 NAT 开放 UDP 35500–35600
3. `GO_VOICE_GATEWAY_ICE_TRANSPORT_POLICY=relay` 强制 TURN 验证
4. Go Gateway `/internal/status` 看 selected pair 分类

### 10.3 端到端首响慢

1. 看 trace：`GET /internal/runtime/traces/{trace_id}` 的 `diagnosis`，再对比 `asr_done`、`llm_first_token`、`tts_first_audio`
2. 跑 `run_voice_latency_baseline.py` 拉基线
3. 先记录实际 `LLM_TOOL_ROUTER_CLASSIFIER_TIMEOUT_MS`；当前 example/runtime 为 3000 ms，代码缺省为 500 ms，确认无超时后再逐步下探
4. TTS 预缓冲 `TTS_PREBUFFER_SEC=0.10`，网络抖动改 0.15–0.25

### 10.4 机器人动作无响应

1. `ROBOT_MQTT_HOST/USERNAME/PASSWORD` 是否配齐
2. `docker logs mcp-robot` 看 publish 报错
3. `mosquitto_sub -h <broker> -t 'windaka/+/mcp/#' -u <user> -P <pass>` 抓包确认
4. 详见 [`robot-mqtt-integration.md`](./robot-mqtt-integration.md)

### 10.5 配置修改未生效

1. 改完 `.env` 必须 `make compose-v3-up` 或重启对应分层服务
2. 通过 admin UI 改的 `server_config` 必须等 `/internal/config/reload` 返回 200
3. 校验：`GET /internal/config/status` 与 DB snapshot version

### 10.6 日志位置

- compose：`docker compose logs -f <service>`
- nohup 启动：`logs/<service>.log` 或 `gateway/nohup.out`
- 日志格式见 [`logging.md`](./logging.md)

---

## 11. 常用诊断命令清单

```bash
# 端口扫描
ss -tlnp | grep -E '(7860|5005[234]|1805[23]|18100|8282|5202)'

# UDP 端口扫描（WebRTC）
ss -ulnp | grep -E '(35500|35600)'

# 实时跟日志
make compose-v3-logs

# 查看 gateway runtime trace
curl -s 'http://127.0.0.1:7860/internal/runtime/traces?limit=10' | jq

# 触发配置 reload
curl -X POST 'http://127.0.0.1:7860/internal/config/reload' | jq

# 抓 MQTT 包
mosquitto_sub -h "$ROBOT_MQTT_HOST" -t 'windaka/+/mcp/#' -u "$ROBOT_MQTT_USERNAME" -P "$ROBOT_MQTT_PASSWORD" -v

# 抓 WebSocket 信令
websocat ws://127.0.0.1:8282/ws

# Go Gateway 配置自检
cd go_voice_gateway && go run . --print-config
cd go_voice_gateway && go run . --print-env-help

# Python 全量单测
python -m pytest test/ -v
```

---

## 12. 安全默认

> 详见 `test/test_sensitive_defaults.py`。

- `CognitiveReportMqttPublisher.from_env()` 在无 env 时 `enabled=False`、`host=""`、`username=""`、`password=""`
- `robot_mqtt_api.MQTT_HOST` 等在无 env 时为空字符串；`_publish_payload` 直接 `RuntimeError("ROBOT_MQTT_HOST not configured")`
- `GO_VOICE_GATEWAY_REQUIRE_ROBOT_SECRET` 默认 `false`（兼容旧客户端），生产必须 `true`
- `ADMIN_AUTH_DISABLED` 默认 `false`；仅临时本地调试可改 `true`
- `WAKE_HTTP_HOST` 默认 `127.0.0.1`；LAN 部署必须显式改为可信子网

**禁止**：
- 直接 commit 真实 `.env`（用 `.env.example` 占位）
- 在 `decisions.md` 之外的场合贴真实凭据
- 关闭强鉴权在生产环境跑

---

## 13. 打包与分发

```bash
# 顶层入口：构建 Go + Rust release 包
make package-v3-release
```

等价于：
```bash
cd go_voice_gateway && make package-ubuntu22-amd64
cd rust_client && make package-ubuntu22-arm64-release
```

产物落在 `go_voice_gateway/dist/` 与 `rust_client/dist/`。

清理：
```bash
make clean-v3-dist       # 仅清 dist/
make clean-local-artifacts  # 清缓存 / 日志 / Rust target / ESP-IDF build；不删 dist 和实验素材
```

---

## 14. 变更管理

- 协议、配置或启动方式发生变化时，说明兼容影响；重要架构决定可记录在 `decisions.md`。
- 实时语音链路优先检查延迟、阻塞、同步等待、额外队列、额外序列化、资源释放、并发安全、可观测性
- 新增队列、线程池、服务层或跨语言序列化时，评估延迟和维护成本。
- 项目交接范围见 `v4-main-handoff.md`。

---

## 15. 相关文档索引

| 主题 | 文档 |
|---|---|
| 整体架构 | [`architecture-and-design.md`](./architecture-and-design.md) |
| 文档总索引 | [`README.md`](./README.md) |
| Docker Compose V4 细节 | [`docker-compose-v4.md`](./docker-compose-v4.md) |
| Docker Compose v3 细节 | [`docker-compose-v3.md`](./docker-compose-v3.md) |
| Go Gateway 内部 | [`go-voice-gateway.md`](./go-voice-gateway.md) |
| 机器人 MQTT 协议 | [`robot-mqtt-integration.md`](./robot-mqtt-integration.md) |
| 决策记录 | [`decisions.md`](./decisions.md) |
| 日志规范 | [`logging.md`](./logging.md) |
| WebRTC/STUN/TURN 探测 | [`webrtc-stun-tun-probes.md`](./webrtc-stun-tun-probes.md) |
| 版本管理 | [`version-management.md`](./version-management.md) |
| 部署清单 | [`deployment-checklist.md`](./deployment-checklist.md) |
| 当前 Gateway 任务 | [`voice_gateway_task_tracker.md`](./voice_gateway_task_tracker.md) |
