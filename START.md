# 当前主线启动说明

2026-09-14现场网络补充见[WebRTC迁移与机器人运维](docs/handover/WEBRTC_NETWORK_HANDOVER.md)：新网络服务器WS15011、NGINX/FRPS/coturn、新旧隧道和机器人systemd入口。此为用户提供的部署快照，不改变仓库通用默认或声明完整语音验收。

本主线基于 4.0，交接范围见 [交接说明](docs/v4-main-handoff.md)。运行环境参考 `docs/ai-runtime-environment.md`，需要检查环境时可运行 `scripts/ai_preflight.py`。示例配置与实际环境配置请分别管理。

本项目当前唯一主链路是：

```text
Rust Client
  -> WebRTC Opus RTP / DataChannel
  -> Go Realtime Edge Gateway :8282
  -> Python AI Orchestrator Gateway :7860
  -> Qwen3-ASR :50054
  -> LLM / Agent / MCP :50053
  -> Qwen3-TTS Profile :50052
  -> Python Gateway -> Go Gateway -> Rust Client
```

WebSocket 仍用于注册、鉴权、SDP/ICE 信令、诊断和明确的 v2 回滚，不是音频主路径。
当前 `main` 是 V4 测试候选，不等于已创建 V4 正式发布标签或完成实体机验收。

## 1. 准备配置

本地调试：

```bash
cp .env.example .env
```

生产 Compose：

```bash
cp deploy/env.v3.compose.example .env
cp deploy/vllm/.env.models.example deploy/vllm/.env.models
```

至少配置以下敏感项，且不要提交真实值：

- `CONFIG_DATABASE_URL`
- `QWEN_ASR_API_KEY`
- `LLM_API_KEY` / `LLM_ROUTER_API_KEY`
- `QWEN3_TTS_CUSTOM_VOICE_API_KEY`，按需配置 `QWEN3_TTS_BASE_API_KEY`
- `ADMIN_PASSWORD` / `ADMIN_SESSION_SECRET`
- 每台设备的 `ROBOT_ID` / `ROBOT_SECRET`
- 生产 TURN 的 `GO_VOICE_GATEWAY_ICE_SERVERS`

Go Gateway 默认只下发公共 STUN，不在源码中保存 TURN 凭据。跨 NAT 部署必须通过环境变量注入完整 STUN/TURN JSON。

## 2. 初始化 server-config

```bash
python scripts/init_config_db.py
```

已有数据库还必须按顺序应用未执行的 `scripts/migrations/*.sql`。当前代码会读取 `bots.max_response_chars`；缺少该列时先应用：

```text
scripts/migrations/20260702_bot_max_response_chars.sql
```

数据库是 Bot、Robot、MCP、Agent、TTS Profile 和 Runtime Snapshot 的运行时真相源；`server_config/bootstrap/legacy_*.yaml` 只用于初始化。

## 3. 推荐：V4 拆分 Python 服务 + Go Gateway

V4 Compose 负责 STT、LLM、TTS、Python Gateway，以及可选 Admin/MCP；Go Gateway
继续使用 host-network 兼容入口：

```bash
make compose-network
make models-config
make models-up
make compose-v4-config
make compose-v4-up
make compose-go-up
```

启用 Admin、本地 MCP 或开发源码挂载时，分别使用原始 Compose profile 或
`make compose-v4-dev-up`，详见 `docs/docker-compose-v4.md`。唱歌部署还需设置
`SINGING_AUDIO_V4_HOST_DIR` 为宿主机 WAV 目录；V4 只读挂载到
`/app/singing/audio`，不会把 WAV 打进镜像。

### 3.1 兼容一键栈

以下入口保留用于一次启动原拆分业务服务和 Go Gateway，不表示代码版本仍是 V3：

```bash
make compose-network
make models-config
make models-up
make compose-v3-validate
make compose-v3-up
make compose-v3-smoke
make compose-v3-logs
```

停止：

```bash
make compose-v3-down
make models-down
```

兼容部署分层：

- `deploy/vllm/docker-compose.models.yml`：Qwen ASR / LLM / Router / TTS 模型层。
- `deploy/compose/business.yml`：STT、LLM、TTS、Python Gateway、Admin。
- `deploy/compose/go-gateway.host.yml`：Go Gateway，host network，监听 `8282` 和 UDP `35500-35600`。
- `deploy/compose/mcp.yml`：可选 MCP 服务。

## 4. 本地裸进程调试

先启动模型端点和数据库，再依次运行：

```bash
python stt/stt_grpc_server.py
python llm/llm_grpc_server.py
python tts/tts_grpc_server.py
python gateway/gateway_server.py
```

另一个终端启动 Go Gateway：

```bash
cd go_voice_gateway
GO_VOICE_GATEWAY_ADDR=0.0.0.0:8282 \
GO_VOICE_GATEWAY_ICE_SERVERS="$GO_VOICE_GATEWAY_ICE_SERVERS" \
go run .
```

启动前可检查脱敏后的最终配置：

```bash
cd go_voice_gateway
go run . --print-config
go run . --print-env-help
```

## 5. Rust Client

通用 WebRTC 启动：

```bash
cd rust_client
GATEWAY_URL=ws://<server-ip>:8282/ws \
ROBOT_ID=<robot-id> \
ROBOT_SECRET="$ROBOT_SECRET" \
TRANSPORT_POLICY=webrtc_only \
WEBRTC_ENABLED=true \
TRANSPORT_FALLBACK_ENABLED=false \
WEBRTC_OFFER_FACTORY=native \
RTC_AUDIO_UPLINK=rtp_only \
cargo run --bin rust_client --features native-webrtc --release
```

音频前端 TCP 场景使用 `rust_client/run.audio_frontend_tcp.sh`；它的远端地址属于现场部署覆盖项，不是仓库通用默认。

## 6. Admin / Apply / Reload

Admin 裸进程默认监听 `0.0.0.0:8282`，开发前端代理也指向该端口。
现场业务容器已有宿主 `15282` → 容器 `8282` 映射，更新代码并重启 Admin 后，
可在内网访问 `http://10.10.6.121:15282`，Admin 无需 FRPC。
若 `.env` 或进程环境显式设置 `ADMIN_API_HOST` / `ADMIN_API_PORT`，仍以该设置为准。
同机开发时 Go Gateway 与 Admin 不能同时占用 `8282`，可将 Go 的
`GO_VOICE_GATEWAY_ADDR` 设为 `0.0.0.0:15010`，并让 Rust 连接对应端口。
现有 Compose 显式设置 Admin 为 `18100`，仍按其端口映射访问。

```bash
python admin-ui/backend/app.py
cd admin-ui/frontend
npm install
npm run dev
```

保存配置后执行 Apply / Reload，使 Gateway、LLM、TTS 加载同一 Snapshot 版本。当前 `service_configs` 的 Gateway/TTS 全局设置仍存在读取偏差，发布前应以各服务 `--print-config`、`/internal/status` 和真实请求日志复核生效值。

## 7. 最小验证

按改动范围选择，不做无必要的全量构建：

```bash
# Python 主链
python -m pytest test/test_stt_qwen_provider.py test/test_sensitive_defaults.py

# Go Gateway
cd go_voice_gateway && go test ./...

# Rust Client transport/config 改动时
cd rust_client && cargo test --features native-webrtc

# Compose 静态校验
python scripts/validate_compose_v3.py
python -m pytest test/test_compose_v4_contract.py -q
```

无麦克风的链路 smoke：

```bash
python scripts/smoke_go_webrtc_python_gateway.py --sample "$SAMPLE_WAV" --client rust
```

## 8. 故障定位顺序

1. Rust：确认 `GATEWAY_URL`、ICE route、RTP uplink、playback/interrupt generation。
2. Go：确认注册鉴权、PeerConnection、RTP stats、Python bridge、downlink gate。
3. Python Gateway：确认 session/round/playback、ASR、LLM、TTS 和 trace。
4. ASR：确认 Qwen3-ASR endpoint、API key、模型名和 unary `RecognizeSpeech`。
5. LLM：确认 Bot Snapshot、Router profile、MCP 绑定和首 token 指标。
6. TTS：确认 `tts_profile_id`、CustomVoice/Base provider 和增量 `input.text` 提交。

详细资料：

- `README.md`
- `docs/README.md`
- `docs/project-context-for-ai.md`
- `docs/operations-reference.md`
- `docs/deployment-checklist.md`
- `docs/go-voice-gateway.md`
