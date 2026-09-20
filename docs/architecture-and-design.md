# 架构与设计（当前 V4 测试主线）

> V4 继承 V3 已验证的 WebRTC 双网关传输，主要演进是 Python 服务拆分、Turn Gate、
> 自然打断和唱歌能力。V3 发布基线仍由 `v3.0.0` 保留。

> 本文档给出 v3 架构的整体形态、关键设计决策、组件边界和部署拓扑。当前运行摘要见 [`project-context-for-ai.md`](./project-context-for-ai.md)，部署与运维细节见其它专题文档。

---

## 1. 系统目标与约束

### 1.1 一句话目标

> 把多种异构客户端（Rust 机器人 / ESP32-S3 硬件 / 未来浏览器）通过**统一 Transport Adapter** 接入同一条 ASR → LLM → MCP → TTS 编排管线，输出可被机器人执行的多模态响应。

> `esp32-s3-usb-aec/` 是当前 USB 麦克风/喇叭/AEC 音频协处理器，位于
> Rust 客户端之前，不是这里所说的未来 ESP32-S3 网络客户端。两者必须保持
> 独立目录和产品边界。

### 1.2 关键约束

- **实时性优先**：从用户说完话到机器人开始播报 TTS 第一帧，端到端目标 < 1.5 s
- **客户端轻量化**：嵌入式 / 机器人端不能跑大模型推理，AI 推理全在云端
- **可观测性贯穿**：每轮请求可被 trace_recorder 完整还原
- **可回滚**：v2.0 纯 WebSocket 必须始终作为稳定回滚基线
- **机器人安全边界**：所有物理动作必须经过显式工具调用，禁止"AI 直接控制硬件"

---

## 2. 回滚链路与当前链路对比

| 维度 | v2.0（回滚基线） | 当前 V4 主线（继承 V3 transport） |
|---|---|---|
| 客户端 → Gateway | WebSocket（FastAPI）| **WebRTC RTP（音频媒体）+ DataChannel（控制）+ WebSocket（信令/鉴权/诊断）** |
| Gateway 实现 | Python FastAPI 单体 | Go（Pion WebRTC 适配层）+ Python（编排层）双网关 |
| 端到端延迟 | ~2.5 s | 目标 ~1.5 s（降低弱网和跨 NAT 音频传输损耗） |
| 多客户端支持 | Rust WS | Rust WebRTC + TURN 兜底 + 预留 ESP32-S / 浏览器 |
| 控制语义通道 | WS 文本/二进制混传 | **control DataChannel**（高优先级、低延迟）+ WS 信令 |
| 音频传输 | WS 二进制 PCM | **WebRTC Opus RTP** |
| 图片快照 | 走 WS | `vision-v1` DataChannel 上传，Go 按 session 缓存，LLM 按需读取 |

> v3.0 不是"v2.0 + WebRTC 旁路"，而是"WebRTC 主链路 + TURN 兜底 + v2 独立回滚基线"。Go Gateway 不再兼容 v2 的 WS 音频数据链路；如果遇到必须使用 WS 的极端网络环境，使用 v2 客户端直连 Python Gateway 作为临时回滚。

---

## 3. 当前整体架构图

```
┌────────────────────────────────────────────────────────────────────┐
│  Clients                                                            │
│                                                                      │
│  ┌────────────────────┐                  ┌─────────────────────┐    │
│  │  Rust Client       │                  │  ESP32-S3 (future)  │    │
│  │  (production)      │                  │  independent path   │    │
│  └──────────┬─────────┘                  └──────────┬──────────┘    │
│             │                                       │                │
└─────────────┼───────────────────────────────────────┼────────────────┘
              │ WebRTC (audio RTP + control DC)       │
              │ + WebSocket (signaling/auth/diag)     │
              ▼                                       ▼
┌────────────────────────────────────────────────────────────────────┐
│  Transport Adapter Layer                                            │
│                                                                      │
│  ┌────────────────────────────────────────────────────────────────┐ │
│  │  go_voice_gateway (Go, Pion WebRTC)                            │ │
│  │  - signaling WebSocket :8282                                   │ │
│  │  - ICE/STUN/TURN negotiation                                  │ │
│  │  - audio RTP track (uplink/downlink)                           │ │
│  │  - control DataChannel (audio_start/end, interrupt, playback)  │ │
│  │  - vision-v1 snapshot DataChannel                              │ │
│  │  - canonical_event.go (transport-agnostic internal events)    │ │
│  └────────────┬───────────────────────────────────────────────────┘ │
│               │                                                     │
│               │ M1 internal voice:                                   │
│               │   ws://127.0.0.1:7860/internal/voice/ws              │
│               │   primary session + bounded candidate/probe bridges  │
│               ▼                                                     │
│  ┌────────────────────────────────────────────────────────────────┐ │
│  │  gateway (Python, FastAPI)                                     │ │
│  │  - /internal/voice/ws typed orchestration                       │ │
│  │  - /ws explicit M0 rollback                                    │ │
│  │  - /internal/* admin endpoints                                  │ │
│  │  - trace_recorder (per-turn event journal)                      │ │
│  │  - session_manager / runtime_state                             │ │
│  │  - voice orchestration (ASR → LLM → TTS)                       │ │
│  └──────┬──────────────────┬──────────────────┬──────────────────┘ │
└─────────┼──────────────────┼──────────────────┼────────────────────┘
          │ gRPC             │ gRPC             │ gRPC
          ▼                  ▼                  ▼
   ┌────────────┐    ┌──────────────┐    ┌──────────────┐
   │ STT gRPC   │    │ LLM gRPC     │    │ TTS gRPC     │
   │ :50054     │    │ :50053       │    │ :50052       │
   │ Qwen3-ASR  │    │ Qwen3-9B     │    │ Qwen3-TTS    │
   └────────────┘    └──────┬───────┘    └──────────────┘
                            │
                            │ HTTPS (tool calls)
                            ▼
                     ┌─────────────────────────────┐
                     │  MCP Servers (SSE/JSON-RPC) │
                     │  mcp-robot :5003            │
                     │  mcp-utils :5004            │
                     │  mcp-singing :5005          │
                     └──────────────┬──────────────┘
                                    │ Robot: MQTT; Singing: asset control; Weather: Websearch MCP
                                    ▼
                     ┌─────────────────────────────┐
                     │  Windaka Robots             │
                     │  subscribe windaka/robot/*  │
                     └─────────────────────────────┘
```

---

## 4. 分层命名与双网关分工

### 4.1 推荐分层命名

v3.0 建议用下面这套名字描述系统层次，避免把 Go Gateway、Python Gateway、STT/LLM/TTS 混成一个"大 Gateway"：

| Layer | 推荐名称 | 当前组件 | 职责 | 不负责 |
|---|---|---|---|---|
| L0 | Device / Client Layer | Rust Client，未来 ESP32-S3 / Browser | 唤醒、录音、播放、设备侧状态、WebRTC 发起端 | ASR/LLM/TTS 推理，业务编排 |
| L1 | Realtime Edge Gateway | `go_voice_gateway` | WebRTC、ICE/STUN/TURN、RTP/DataChannel、边缘会话状态、弱网处理、打断转发、音频包整理 | Tool routing、业务 prompt、机器人配置持久化 |
| L2 | AI Orchestrator Gateway | `gateway/` Python Gateway | 机器人注册校验、ASR -> LLM -> MCP -> TTS 编排、round/playback 语义、trace、业务 session | 公网 WebRTC 接入、UDP 端口管理、RTP jitter/reorder |
| L3 | Model Service Layer | `stt/`、`llm/`、`tts/` gRPC | 模型调用封装、provider 适配、流式输入输出 | 客户端连接状态、机器人动作协议 |
| L4 | Tool / Robot Integration Layer | `mcp_servers/`、MQTT broker、机器人控制 API | MCP 工具、机器人运控、外部系统集成 | 语音传输、LLM 主推理 |
| L5 | Runtime Config / Ops Layer | `server_config/`、Admin UI、Docker Compose、TURN | 配置、部署、观测、回滚、网络穿透 | 业务链路本身的实时处理 |

简写时可以这样说：

- **Go = Realtime Edge**：网络接入层 / WebRTC Adapter / RTP 边缘状态机。
- **Python = AI Orchestrator**：AI 编排层 / 业务 session 层。
- **STT/LLM/TTS = Model Services**：模型服务层。
- **MCP/MQTT = Tool Integration**：工具与机器人执行层。

### 4.2 为什么需要双网关

Go Gateway 与 Python Gateway 各有所长，强行融合会导致两边都复杂化：

| 维度 | Go Gateway | Python Gateway |
|---|---|---|
| 强项 | WebRTC 媒体流、信令 ICE、UDP 端口管理 | ASR/LLM/TTS 业务编排、tool routing、trace、agent |
| 弱项 | 不持有业务状态、不跑模型 | 不适合长连接媒体流 |
| 部署 | host network（直接绑定 UDP 35500–35600） | 容器化（wzk_ai_net） |

### 4.3 V3 传输边界决策

v3.0 的生产传输边界是：

```text
Rust v3 Client
  -> WebRTC RTP/DataChannel
  -> Go Realtime Edge Gateway
  -> internal WS bridge
  -> Python AI Orchestrator Gateway
```

明确不做：

- Go Gateway 不兼容 v2 Rust 客户端的 WS audio binary 数据协议。
- Go Gateway 不把 WS audio 当作 WebRTC 失败后的兜底。
- v3 客户端的媒体兜底优先依赖 ICE：STUN 直连、TURN UDP、TURN TCP/TLS。

保留：

- WebSocket 仍用于 register、鉴权、SDP/ICE signaling、配置下发、诊断和必要的控制面兼容。
- v2.0 分支/客户端作为独立回滚基线，可在极端网络环境下临时直连 Python Gateway。

### 4.4 接口边界：Canonical Voice Events

Go Gateway 内部把 WebRTC RTP 包、DataChannel JSON 控制消息、WebSocket 信令帧统一归一化为 `CanonicalVoiceEvent`：

```go
type CanonicalVoiceEvent struct {
    Type      string    // audio_start | audio_packet | audio_end | interrupt |
                        // playback_started | playback_finished | client_status
    SessionID string
    Timestamp time.Time
    // transport-agnostic payload
    Payload   []byte
    Headers   map[string]string
}
```

这是**唯一的对外契约**：未来 ASR 切片（无论 Python 直接 STT gRPC 还是 Go 直接 STT）都消费 `CanonicalVoiceEvent`，不区分客户端协议。

### 4.5 M0 Python Bridge 回滚模式

以下连接模式只约束 M0 `/ws` 回滚 bridge，不是默认 M1 业务路径：

| 模式 | 行为 | 用途 |
|---|---|---|
| `session`（v3.0 默认） | 每个 Rust session 复用一条内部 WS，注册一次后常驻 | 生产环境，减少握手开销 |
| `per_turn` | 每次 ASR handoff 重连+注册 | 排障、隔离性 |

强鉴权路径：
1. Rust → Go `register`（携带 robot_id + robot_secret）
2. Go → Python Gateway `register`（复用 Rust 凭据）
3. Python Gateway 返回 `registered`
4. Go 才发回 `rtc_config` 给 Rust
5. 之后的每轮 ASR/事件复用这条已注册的内部 WS

降级路径（强鉴权失败时）：
- 退回 `GO_VOICE_GATEWAY_ASR_PYTHON_GATEWAY_ROBOT_ID` 单机器人 fallback
- 仅用于本地排障与 smoke 测试

### 4.6 Go-Python Internal Voice Orchestrator Protocol

当前 Go Gateway 与 Python Gateway 默认使用 M1 `/internal/voice/ws` typed protocol。
同一 session 承载真实用户音频、client event、interrupt、playback report、状态、
TTS 音频和结构化终态。外部兼容 `/ws` bridge 仅作为 M0 显式回滚。

具体消息族、状态机、错误和回滚契约见
[`go-python-internal-protocol-v1.md`](./go-python-internal-protocol-v1.md)。

这个协议的定位：

- Go Gateway 负责把 WebRTC RTP/DataChannel/信令归一化为内部语音事件。
- Python Gateway 负责消费内部事件，执行现有 `ASR -> LLM -> MCP -> TTS` 编排。
- 协议只约束 Go 与 Python 的网关边界，不改变 STT/LLM/TTS/MCP 服务接口。
- 不改变 Local Qwen3 TTS 的增量文本提交策略：LLM 出一个字/小增量，Python 仍可立即推给 TTS WebSocket；协议不引入自定义分段聚合。

当前阶段：

| 阶段 | 连接形态 | 目标 | 是否当前开发 |
|---|---|---|---|
| M0 | External-compatible WS bridge | Python `/ws` 兼容路径 | 仅显式回滚 |
| M1 | Internal WS typed protocol | `/internal/voice/ws` typed events | V3 生产默认 |
| M2 | gRPC bidirectional stream | 仅在指标证明 WS 边界是瓶颈时评估 | V3 不实施 |

M1 保留 WebSocket 是为了维持 Python 现有编排、trace、round/playback、打断和
增量 TTS 语义。没有同环境性能证据时，不为追求协议形式而迁移到 M2。

架构文档不复制协议字段和消息表，避免与冻结契约产生两套真相源。这里仅保留不变式：

- `session_id/trace_id/utterance_id/round_id/playback_id` 必须跨 Go/Python 对齐。
- accepted 请求不可自动重放到 M0；重复请求不能产生第二次 ASR、回答或 TTS。
- interrupt、cancel、error 和正常完成都必须产生唯一、可关联的结构化终态。
- TTS 下行保持增量提交；Go 必须按 playback generation 丢弃迟到音频。
- TURN 是 WebRTC 媒体兜底，M0 `/ws` 不是 V3 自动网络 fallback。

---

## 5. WebRTC 通道分工

| 通道 | 用途 | 优先级 | 数据量级 |
|---|---|---|---|
| `audio` RTP track（uplink）| 用户语音上行 | 实时 | 16 kHz / 48 kHz Opus，~32 kbps |
| `audio` RTP track（downlink）| TTS 下行播放 | 实时 | 同上 |
| `control` DataChannel | `audio_start` / `audio_end` / `interrupt` / `playback_started` / `playback_finished` / `client_status` | 高 | 低频 JSON |
| `snapshot` DataChannel | `vision-v1` 图片快照上传 | 按需 | 受大小、频率和鉴权限制 |
| WebSocket signaling | `register` / `rtc_offer` / `rtc_ice_candidate` / `transport_ready` / 配置与诊断 | 控制面 | 低频 |

**关键约束**：RTP 包本身不携带完整 turn/playback 业务语义；所有业务语义通过 control DataChannel 绑定到 RTP stream / SSRC / MID。Go Gateway 需要负责把 RTP 的实时性和控制消息的业务顺序重新整理成 Python Gateway 能理解的 turn/utterance/playback 语义。

---

## 6. NAT 穿越策略

### 6.1 ICE 候选优先级

```
1. host         (本地内网直连)
2. srflx        (STUN 反射)
3. prflx        (对称 NAT 反射)
4. relay        (TURN relay)  ← 仅当上述不通时
```

### 6.2 TURN 协议回退链

```
STUN UDP 直连 → TURN UDP → TURN TCP → TURNS 443
```

- 默认 ICE 传输策略 `all`（允许 host/srflx/relay）
- 排障时可强制 `relay` 验证 TURN 路径
- 端口范围：`GO_VOICE_GATEWAY_RTC_UDP_MIN_PORT=35500` ~ `MAX_PORT=35600`
- 部署要求：防火墙与上游 NAT 同样开放此 UDP 范围
- Go Gateway 默认会下发 STUN 3478、TURN UDP/TCP 3478、TURNS 443；当前 Rust native 客户端只使用已验证的 UDP URL，并会在日志中标记跳过的 TURN TCP/TLS URL。

### 6.3 ICE 路由分类（v3 observability）

Go Gateway 通过 `ice_route.go` 把当前 selected pair 分类为：

```go
const (
    ClassHostUDP     = "direct_udp"     // host 候选直连
    ClassSTUNUDP     = "stun_udp"       // srflx
    ClassTURNUDP     = "turn_udp"       // relay over UDP
    ClassTURNTCP     = "turn_tcp"       // relay over TCP
    ClassTURNS443    = "turns_443"      // relay over TLS 443
)
```

分类结果落到 `/internal/status`，便于排障时判断当前链路是否走了 TURN。

---

## 7. 业务编排层

### 7.1 语音主链路

```
[Client audio RTP up]
   ↓ (Go Gateway: RTP → OPUSRAW1 → WS binary frame)
[Python Gateway /ws]
   ↓
process_asr() → STT gRPC (RecognizeStream)
   ↓ (final text)
process_llm_tts_stream():
   ↓
LLM gRPC ChatStream (tool routing → MCP call → final answer)
   ↓ (sentence commit)
TTS gRPC SynthesizeStream
   ↓ (PCM chunk)
opus 编码 → WebSocket binary VAF1 / WebRTC RTP
   ↓
[Client playback audio RTP down]
```

### 7.2 工具路由策略

`llm/tool_router.py` 提供两类路由：

1. **轻量 LLM 决策**：用 fast model 分类 `{category, tool_prefix}`
2. **规则回退**：正则 + 关键词（`weather_*` / `websearch_*` / `robot_*`）

意图分类（规则）：
- `_is_chat_intent`：闲聊
- `_is_websearch_query`：需联网
- `_is_low_tool_risk_chat`：低风险对话，可不带 tools
- `_is_capability_question`：用户问"你能做什么"
- `_is_narrative_chat`：故事/接龙类

工具名后缀约定：
- `weather_*`：天气类
- `robot_*`：机器人控制
- `websearch_*`：联网搜索

### 7.3 Tool Wait Message

工具调用前的等待语由 `voice_quick_replies.py` 集中管理：
- 工具调用期间不沉默：先播放不带未确认动态参数的安全过程语 → 工具执行 → 流式最终答复。
- 天气等查询首响不复述地点，实际地点以最终工具参数为准。
- 唤醒、待机、退出和工具过程语至少提供 16 条人工筛选短句，并限制可见长度；不得通过
  机械拼接凑大词池。按 session 使用 shuffle-bag，一轮用完前不重复。
- feature flag 控制，可回退到旧"等工具结果再答"逻辑

### 7.4 Trace 系统

`gateway/trace_recorder.py` 落盘每轮事件：
- `asr_final`
- `llm_first_token`
- `tool_call_start` / `tool_call_end`
- `tts_first_audio`
- `playback_end`
- `interrupt`

trace 数据通过 `/internal/runtime/traces/{trace_id}` 暴露给 admin-ui。

---

## 8. 部署拓扑

### 8.1 当前 V4 Python 服务与 Go 入口

| 服务 | 镜像基座 | 端口 | 网络模式 |
|---|---|---|---|
| `wzk-stt-grpc` | python-services | 50054（gRPC）| wzk-ai-voice-net |
| `wzk-llm-grpc` | python-services | 50053（gRPC）+ 18053（admin） | wzk-ai-voice-net |
| `wzk-tts-grpc` | python-services | 50052（gRPC）+ 18052（admin） | wzk-ai-voice-net |
| `wzk-python-gateway` | python-services | 7860（WS/HTTP） | wzk-ai-voice-net |
| `go-gateway` | go_voice_gateway | 8282（signaling）+ 35500–35600 UDP（host net） | **host network** |
| `wzk-admin-api` | python-services | 18100（HTTP）| wzk-ai-voice-net |
| `wzk-mcp-utils` | python-services | 5004（SSE）| wzk-ai-voice-net |
| `wzk-mcp-robot` | python-services | 5003（SSE）| wzk-ai-voice-net |
| `wzk-mcp-singing` | python-services | 5005（SSE）| wzk-ai-voice-net |

V4 Compose 不包含 Go Gateway；Go 继续通过 `deploy/compose/go-gateway.host.yml` 运行。
`docker-compose.v3.yml` 保留为一次启动 Go + 兼容 Python 业务栈的入口。

### 8.2 为什么 Go Gateway 用 host network

WebRTC 候选需要直接绑定 host UDP 端口（35500–35600）。如果走 Docker bridge 网络，每个候选都要 NAT，性能与连通性都受影响。host network 让 Pion 直接拿到 host IP，STUN/srflx 工作正常。

代价：Go Gateway 容器不能加 `ports:` 字段；同主机上其他服务通过 `127.0.0.1:8282` 访问。

### 8.3 模型层与业务层分离

```
deploy/vllm/docker-compose.models.yml     # 模型层（vLLM ASR / LLM / Router / TTS）
docker-compose.v4.yml                     # 当前拆分 Python 服务
docker-compose.v4.dev.yml                 # 开发只读源码挂载
docker-compose.v3.yml                     # Go + Python 兼容薄入口
deploy/compose/business.yml               # 业务层（Python Gateway + STT/LLM/TTS + Admin）
deploy/compose/go-gateway.host.yml        # WebRTC 入口层（Go Gateway host network）
deploy/compose/mcp.yml                    # 可选 MCP helpers
```

兼容 Compose 的模型/业务层可加入 `wzk-ai-net` 并使用模型容器 DNS。V4 Python 服务使用
`wzk-ai-voice-net`；模型在其他网络或主机时必须通过根 `.env` 配置可达地址，不能假设跨
Compose project 的 service name 自动可见。

### 8.4 配置真相源：server_config

`server_config/` 取代旧 .env 直接编辑：
- `ConfigRepository`：DB CRUD
- `RuntimeConfigSnapshot`：整张快照
- `build_*_runtime_snapshot`：分发到 gateway / llm / tts
- `secrets.py`：敏感字段脱敏

变更通过 admin-ui 提交 → DB → snapshot → `/internal/config/reload` → 各服务 apply_gateway_upstreams。

---

## 9. 关键设计不变式

| 不变式 | 解释 |
|---|---|
| 主业务 session 持久复用 | 每个 Rust session 复用一条 M1 主 bridge；Turn Gate/自然打断可使用独立有界辅助 bridge，关闭 session 时一并释放 |
| RTP 不携带业务语义 | 所有 turn/playback 语义走 control DataChannel |
| Canonical Voice Events 是内部契约 | WebRTC/WS/未来 client 全部归一化为同一事件流 |
| v2.0 始终可回滚 | 不在 v3 内长期维护 WS 数据双链路 |
| 机器人安全边界 | 所有物理动作必须经过显式 tool call，禁止 AI 直连硬件 |
| MCP 是 tool 调用唯一通道 | LLM 侧通过 `mcp_manager` 调 MCP server；机器人 MQTT 发布封装在 Robot MCP 内 |
| gRPC proto 与 WS 帧是跨服务契约 | 修改必须同步 `decisions.md` 与所有调用端 |

---

## 10. 当前已知限制与验证边界

| 项目 | 状态 | 影响 |
|---|---|---|
| 实体设备长稳、serial wake、麦克风和扬声器 | DEFERRED | 自动化通过不得声明 `HARDWARE_VERIFIED` |
| 自然打断实体机双讲 | UNKNOWN | Rust 默认关闭；Go/Python 默认就绪，需实体机单独验收 |
| TURN TCP/TLS | UNKNOWN | 当前验证主链只使用 TURN UDP |
| 全 Go 编排（去掉 Python） | DEFERRED | 没有瓶颈证据时不迁移 |
| ESP32-S3 网络客户端 | DEFERRED | 当前生产端侧仍是 Rust Client；USB 音频协处理器是独立路线 |
| Turn Detection / EOU | IN_PROGRESS | 由 `turn-gate/` 独立承载，证据不得与 V3 tag 混用 |

---

## 11. 与决策记录的关系

| 决策 | 文档 | 影响范围 |
|---|---|---|
| 2026-06-27: WebRTC-first v3 主线 | [`decisions.md`](./decisions.md) | 全链路，本文档主要遵循对象 |
| 2026-06-15: 工具请求首响优化实验 | [`decisions.md`](./decisions.md) | `llm/tool_router.py` 与 `llm_client.py` |
| 2026-04-17: 统一交互状态规则 | [`decisions.md`](./decisions.md) | Rust 客户端与 Python 服务端状态机 |
| 2026-03-03: 客户端目录重构 | [`decisions.md`](./decisions.md) | `rust_client/` 自包含 |
| 2026-03-04: AEC 软件补偿 | [`decisions.md`](./decisions.md) | 客户端录音链 |

---

## 12. 阅读顺序建议

新加入本项目的开发者建议按以下顺序阅读：

1. [`README.md`](./README.md) — 文档导航和当前/历史边界
2. `START.md` — 启动指引
3. [`architecture-and-design.md`](./architecture-and-design.md)（本文档）— 整体形态
4. [`project-context-for-ai.md`](./project-context-for-ai.md) — 当前运行事实
5. [`docker-compose-v4.md`](./docker-compose-v4.md) — 当前 V4 Python 服务部署
6. [`docker-compose-v3.md`](./docker-compose-v3.md) — Go + Python 兼容部署
7. [`go-voice-gateway.md`](./go-voice-gateway.md) — Go Gateway 内部
8. [`robot-mqtt-integration.md`](./robot-mqtt-integration.md) — 机器人 MQTT 协议
9. [`operations-reference.md`](./operations-reference.md) — 运维 / 配置矩阵
10. [`decisions.md`](./decisions.md) — 当前有效决策
