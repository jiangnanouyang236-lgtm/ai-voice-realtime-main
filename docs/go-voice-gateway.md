# Go Realtime Edge Gateway

`go_voice_gateway/` 是 v3 Rust Client 与 Python AI Orchestrator 之间的实时传输层，不负责 Bot prompt、Agent/MCP 决策或模型推理。

## 当前职责

- WebSocket 注册、Robot 强认证、SDP/ICE 信令。
- Pion WebRTC PeerConnection、ICE/STUN/TURN 和 UDP 端口范围。
- Opus RTP 上下行、DataChannel 控制、RTP 顺序/重复/丢包观测。
- `vision-v1` DataChannel 图片接收、校验和每会话最新帧内存缓存。
- session、utterance、round/playback 和 downlink gate。
- 默认 M1 Python `/internal/voice/ws` typed 语音编排。
- M0 Python `/ws` 作为显式回滚路径。
- `/healthz`、`/internal/status`、带鉴权的最新图片接口和结构化日志。

## 数据路径

```text
Rust
  -- WS register / rtc_offer / ICE --> Go
  -- WebRTC Opus RTP + DataChannel --> Go
Go
  -- typed audio/control ----------> Python Gateway /internal/voice/ws
Python Gateway
  -- typed status/VAF1/terminal ---> Go
Go
  -- WebRTC Opus RTP -------------> Rust
```

`GO_VOICE_GATEWAY_INTERNAL_VOICE_MODE` 统一选择路径：默认 `m1` 接管真实
用户音频、`client_event`、打断和播放回报；`shadow` 保持 M0 执行业务并旁路
验证 M1；`m0` 完全关闭 Go 侧 M1 session，作为明确回滚。M1 请求提交后
不会自动重放到 M0，避免重复 ASR 和重复回答。

## vision-v1 图片通道

图片通道与 `control` DataChannel 分离。RustClient 已能发送，GoGateway
已能接收和缓存。LLM 服务仅在命中视觉意图时，通过内部接口读取当前
session 的最新可用图片：

- DataChannel label 固定为 `vision-v1`。
- 正式上行格式固定为 JPEG、`640×360`、最大 `100KB`。
- 每个二进制消息由 32 字节大端序头部和最大 32KB payload 组成。
- 支持乱序分片，单帧最多 4 片，同时最多组装 2 帧。
- 图片采集时间超过 5 秒时拒绝；最新帧在内存保留 15 秒后清理。
- 每个 session 只保存最新一张，不落盘，日志不记录图片内容。
- 无效、过期、缺片或被新帧覆盖的图片不会影响控制通道和音频 RTP。
- `GET /internal/vision/snapshot?session_id=...` 只返回 5 秒内的 JPEG，
  要求 Bearer token，并带 `Cache-Control: no-store`；未配置 token 时接口不可用。
- 图片仅附加到当前 LLM 请求，不写入会话历史，也不进入 Robot MCP/工具 Router。

头部结构：

| 偏移 | 长度 | 字段 |
|---:|---:|---|
| 0 | 4 | Magic `VIS1` |
| 4 | 1 | 协议版本 `1` |
| 5 | 1 | Flags，当前固定 `0` |
| 6 | 2 | Header length，固定 `32` |
| 8 | 8 | Frame ID |
| 16 | 8 | Capture timestamp，Unix 毫秒 |
| 24 | 4 | JPEG 总字节数 |
| 28 | 2 | Chunk index，从 `0` 开始 |
| 30 | 2 | Chunk count |

完整帧处理后 Gateway 返回
`vision_snapshot_ack`，状态为 `accepted` 或 `dropped`，并携带
`frame_id`、`reason` 和 `received_at_ms`。

## 启动

```bash
cd go_voice_gateway
go run . --print-env-help
go run . --print-config
go run .
```

核心默认值：

| 变量 | 默认 | 说明 |
|---|---|---|
| `GO_VOICE_GATEWAY_ADDR` | `127.0.0.1:8282` | 服务器接收设备时设为 `0.0.0.0:8282` |
| `GO_VOICE_GATEWAY_RTC_ENABLED` | `true` | v3 主路径 |
| `GO_VOICE_GATEWAY_ASR_PROCESSOR` | `python_gateway` | 复用 Python 编排 |
| `GO_VOICE_GATEWAY_ASR_PYTHON_GATEWAY_WS_URL` | `ws://127.0.0.1:7860/ws` | M0 bridge |
| `GO_VOICE_GATEWAY_ASR_PYTHON_GATEWAY_CONNECTION_MODE` | `session` | 同 Rust session 复用 Python WS |
| `GO_VOICE_GATEWAY_ASR_PYTHON_GATEWAY_TOTAL_TIMEOUT_MS` | `45000` | 单轮总预算，`0` 禁用 |
| `GO_VOICE_GATEWAY_ASR_PYTHON_GATEWAY_SESSION_IDLE_RECYCLE_MS` | `30000` | 空闲 30 秒后下一轮前回收；`0` 保持到断开 |
| `GO_VOICE_GATEWAY_DOWNLINK_AUDIO_TRANSPORT` | `webrtc_rtp` | v3 TTS 下行 |
| `GO_VOICE_GATEWAY_ICE_SERVERS` | 公共 STUN | 生产 TURN 必须由环境注入 |
| `GO_VOICE_GATEWAY_ICE_TRANSPORT_POLICY` | `all` | 排障可设 `relay` |
| `GO_VOICE_GATEWAY_ICE_NETWORK_TYPES` | `udp4` | IPv6 验证后再启用 |
| `GO_VOICE_GATEWAY_RTC_UDP_MIN_PORT` | `35500` | Pion UDP 起始端口 |
| `GO_VOICE_GATEWAY_RTC_UDP_MAX_PORT` | `35600` | Pion UDP 结束端口 |

生产示例：

```bash
export GO_VOICE_GATEWAY_ADDR=0.0.0.0:8282
export GO_VOICE_GATEWAY_REQUIRE_ROBOT_SECRET=true
export GO_VOICE_GATEWAY_DEVICE_AUTH_BACKEND=python_gateway
export GO_VOICE_GATEWAY_ASR_PYTHON_GATEWAY_WS_URL=ws://127.0.0.1:7860/ws
export GO_VOICE_GATEWAY_ASR_PYTHON_GATEWAY_SESSION_IDLE_RECYCLE_MS=30000
export GO_VOICE_GATEWAY_DOWNLINK_AUDIO_TRANSPORT=webrtc_rtp
export GO_VOICE_GATEWAY_ICE_SERVERS="$TURN_ICE_JSON"
export GO_VOICE_GATEWAY_VISION_INTERNAL_TOKEN="$VISION_INTERNAL_TOKEN"
```

源码默认不含 TURN credential。`TURN_ICE_JSON` 必须来自未跟踪 `.env` 或密钥系统。

## Rust Client

```bash
cd rust_client
GATEWAY_URL=ws://${SERVER_IP}:8282/ws \
ROBOT_ID="$ROBOT_ID" \
ROBOT_SECRET="$ROBOT_SECRET" \
TRANSPORT_POLICY=webrtc_only \
WEBRTC_ENABLED=true \
TRANSPORT_FALLBACK_ENABLED=false \
WEBRTC_OFFER_FACTORY=native \
RTC_AUDIO_UPLINK=rtp_only \
cargo run --bin rust_client --features native-webrtc --release
```

v3 生产不在 Go 内自动回退到 WS 音频。网络失败先检查 ICE/TURN；需要 WS 音频时使用独立 v2 回滚路径。

### 唤醒打断下行约束

- Go 接受新的 `client_event` 后必须先封锁当前 RTP，再启动新的云端轮次。
- `playback_interrupted` 只允许取消匹配的 `round_id` / `playback_id`；旧轮次回执不能影响替代播报。
- Go 在 `playback_start.rtp_start_sequence` 中声明新轮次的首个 RTP 序号；Rust 丢弃该边界之前的无轮次元数据 RTP，避免网络中晚到的旧 TTS 恢复播放。
- `rtp_start_sequence` 是可选兼容字段，因此 Go 可先于 Rust 部署；完整保护需要两端都更新。

## 关键代码

- `main.go`：配置、启动、healthcheck CLI。
- `config.go` / `env_help.go`：配置解析与脱敏输出。
- `server.go`：HTTP/WS、PeerConnection、RTP/DataChannel、downlink。
- `session_registry.go`：注册身份与 session 生命周期。
- `asr_runtime.go` / `asr_handoff.go`：utterance 聚合与非阻塞 handoff。
- `asr_python_bridge.go`：M0 Python bridge。
- `internal_protocol.go` / `internal_voice_client.go`：M1 typed protocol。
- `downlink_rtp.go`：Python TTS 到 WebRTC RTP。
- `vision.go`：图片分片协议、JPEG 校验、最新帧缓存与 TTL。
- `canonical_event.go`：transport-neutral event 边界。

## 状态与安全

```bash
curl http://127.0.0.1:8282/healthz
curl http://127.0.0.1:8282/internal/status
```

`/internal/status` 包含连接、route、bridge 和 session 诊断信息，当前没有独立管理鉴权；只应通过 localhost、受信网络或反向代理访问，不能直接暴露到公网。

`--print-config` 会隐藏 Robot secret 和 ICE credential；日志也不应输出完整注册 payload 或环境变量。

## 验证

```bash
cd go_voice_gateway
go test ./...

cd ..
python scripts/smoke_go_webrtc_python_gateway.py \
  --sample "$SAMPLE_WAV" \
  --client rust
```

链路异常按以下指标定位：

- Rust：ICE pair、RTP uplink、TTFA、playback generation。
- Go：packet loss/reorder、bridge open/send/first binary、downlink drop。
- Python：`llm_router_kind`、`llm_first_token_ms`、`tts_first_audio_ms`、`ws_send_ms`。

部署细节见 `docs/docker-compose-v3.md`，当前 M1 契约见
`docs/go-python-internal-protocol-v1.md`。
