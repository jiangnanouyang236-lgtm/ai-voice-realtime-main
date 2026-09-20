# 新人上手指南

本指南的目标不是让新人第一天掌握所有服务，而是在 60 分钟内完成三件事：

1. 能用一句话解释系统解决什么问题。
2. 能沿一次语音请求找到对应代码和运行状态。
3. 能完成一次不污染环境、结论不过界的最小验证。

> [!IMPORTANT]
> 先建立运行上下文，再启动服务。不要用 example 覆盖已有 `.env`，不要因连接失败切换到
> mock、localhost、示例模型或关闭鉴权。

## 先选择你的入口

| 角色 / 任务 | 先看什么 | 主要目录 |
| --- | --- | --- |
| 产品、方案、技术负责人 | [`project-context-for-ai.md`](./project-context-for-ai.md) → [`architecture-and-design.md`](./architecture-and-design.md) | `docs/` |
| Python / AI 编排 | 请求生命周期、Runtime Snapshot、trace | `gateway/`、`llm/`、`stt/`、`tts/` |
| Go / 实时传输 | [`go-voice-gateway.md`](./go-voice-gateway.md) → [`go-python-internal-protocol-v1.md`](./go-python-internal-protocol-v1.md) | `go_voice_gateway/` |
| Rust / 端侧音频 | [`../rust_client/README.md`](../rust_client/README.md) → [`audio-endpoint-matrix.md`](./audio-endpoint-matrix.md) | `rust_client/` |
| MCP / 机器人能力 | [`robot-mqtt-integration.md`](./robot-mqtt-integration.md) | `mcp_servers/`、`llm/tool_router.py` |
| 配置、部署、运维 | [`../START.md`](../START.md) → [`operations-reference.md`](./operations-reference.md) | `server_config/`、`admin-ui/`、`deploy/` |
| 测试、发布、验收 | [`evidence-gates.md`](./evidence-gates.md) → [`deployment-checklist.md`](./deployment-checklist.md) | `test/`、`scripts/`、`.ai/` |

## 前 15 分钟：建立系统全貌

先记住这一条主链：

```text
Robot / Audio Device
  → Rust Client
  → WebRTC
  → Go Realtime Edge Gateway
  → Python AI Orchestrator
  → ASR → LLM / Agent / MCP → TTS
  → Python → Go → Rust → Speaker
```

各层只需要先理解一个职责：

| 层 | 负责什么 | 不负责什么 |
| --- | --- | --- |
| Rust Client | 唤醒、采音、VAD、播放、打断、端侧状态 | AI 工具路由 |
| Go Gateway | WebRTC、ICE、RTP、DataChannel、实时 session | LLM 业务编排 |
| Python Gateway | ASR → LLM / MCP → TTS、round / playback / trace | 接管 WebRTC 媒体层 |
| Model Services | 独立 ASR、LLM、TTS 推理接口 | 设备生命周期 |
| MCP / MQTT | 外部工具和机器人动作 | 隐式执行物理动作 |
| server-config | Bot / Robot / MCP / Agent / TTS Profile 运行态 | 用 YAML 冒充当前状态 |

## 前 30 分钟：沿一次请求找到代码

不需要通读大文件，按请求顺序找到这些入口即可：

| 阶段 | 主要入口 | 观察重点 |
| --- | --- | --- |
| 唤醒与录音 | `rust_client/src/main.rs`、`rust_client/src/app.rs` | 当前交互状态、VAD、interrupt |
| WebRTC 端侧 | `rust_client/src/transport/webrtc.rs` | SDP / ICE、RTP、DataChannel |
| 实时接入 | `go_voice_gateway/server.go` | session、uplink / downlink、播放门控 |
| Go-Python 会话 | `go_voice_gateway/internal_voice_client.go` | M1 typed envelope、ACK、终态 |
| AI 编排 | `gateway/gateway_server.py` | session、ASR、LLM、TTS 主流程 |
| 语音识别 | `stt/asr_providers.py`、`stt/stt_grpc_server.py` | Qwen3-ASR provider 与 unary 调用 |
| 对话与工具 | `llm/llm_grpc_server.py`、`llm/tool_router.py` | Bot Snapshot、路由、Agent / MCP |
| 语音合成 | `tts/tts_grpc_server.py` | TTS Profile、增量文本、首段音频 |
| 运行态配置 | `server_config/repository.py`、`server_config/snapshot.py` | Snapshot 版本和实际绑定 |

贯穿链路的五个标识：

- `trace_id`：跨层诊断一轮用户体验。
- `session_id`：一次连接生命周期。
- `utterance_id`：一段用户输入语音。
- `round_id`：一次 AI 编排轮次。
- `playback_id`：一次实际播放。

排查“串轮、旧音频、错误打断”时，先确认这些标识是否属于同一条链。

## 前 60 分钟：完成第一次安全验证

只安装当前任务需要的工具链，不必第一天准备全部环境：

| 工作范围 | 工具链 |
| --- | --- |
| Python / 文档 / Harness | Python 3.11 与 `requirements-dev.txt` 对应依赖 |
| Go Gateway | Go 1.24（以 `go_voice_gateway/go.mod` 为准） |
| Rust Client | Rust stable；音频与 native WebRTC 依赖见 Rust README |
| Admin UI | Node.js `>=20.19.0`（以 `admin-ui/frontend/package.json` 为准） |
| Compose 部署 | Docker 与 Compose plugin |

### 1. 确认工作区

```bash
git status --short --branch
python scripts/ai_preflight.py --profile offline
make help
```

离线预检通过只表示仓库结构、测试根、Python 与 Harness 前提可用，不代表服务已启动。
如果工作区已有改动，默认属于其他协作者；不要覆盖或清理。

### 2. 按任务选择最小验证

| 改动范围 | 推荐入口 |
| --- | --- |
| 文档、配置契约 | `python scripts/check_repo_hygiene.py`、`git diff --check` |
| Python 模块 | `python -m pytest test/test_<相关模块>.py -q` |
| Go Gateway | `cd go_voice_gateway && go test ./...` |
| Rust transport / config | `cd rust_client && cargo test --features native-webrtc` |
| V4 Compose 契约 | `python scripts/ai_preflight.py --profile compose-v4` 后运行 `python -m pytest test/test_compose_v4_contract.py -q` |
| 不确定测试范围 | `python scripts/ai_quality_gate.py --mode changed --dry-run`，确认计划后再去掉 `--dry-run` |

不要为了“看起来全绿”运行无关构建、关闭真实配置或扩大测试范围。

### 3. 第一次提交前

```bash
python scripts/check_repo_hygiene.py
git diff --check
```

暂存后再运行：

```bash
python scripts/check_repo_hygiene.py --index
```

提交说明应同时写清：改了什么、运行了什么、哪些 live / hardware 验证没有运行。

## 需要启动真实链路时

开始前向环境负责人确认以下访问前提，不要把实际值写入文档、Issue 或聊天记录：

- 当前机器应使用哪个 Profile，以及是否具备 LAN / VPN / 现场网络。
- 未跟踪 `.env` 是否已包含数据库、模型和服务凭据。
- 目标 Bot / Robot / MCP / TTS Profile 在 Runtime Snapshot 中的实际绑定。
- 是否授权网络探测、MQTT / Robot 动作和硬件操作。

### 先选 Profile

| Profile | 适合什么场景 |
| --- | --- |
| `offline` | 阅读、静态检查、Harness 自检 |
| `local` | 本机 Python / Go 服务 |
| `compose-v4` | V4 拆分 Python Compose |
| `lan` | 本机进程访问 LAN 模型和数据库 |
| `hybrid` | 本机业务进程连接测试数据库、模型和授权 MQTT |
| `hardware` | Robot、摄像头、音频前端和物理播放 |
| `model-host` | 模型服务器与 vLLM 管理 |

只有用户允许网络探测时才增加 `--connectivity`。完整变量要求见
[`ai-runtime-environment.md`](./ai-runtime-environment.md)。

### 启动顺序

1. 确认数据库、模型端点、网络边界和当前 Profile。
2. 启动或确认 ASR / LLM / TTS 与 Python Gateway。
3. 启动 Go Gateway，并确认 `/healthz`。
4. 启动 Rust Client，确认 register、ICE route 和 RTP uplink。
5. 发起一轮真实请求，用同一个 `trace_id` 检查 ASR、LLM、TTS 与播放结果。

具体命令以 [`../START.md`](../START.md) 为准。Go `/internal/status` 信息较多，只能在
localhost、可信网络或受保护的反向代理后访问。

### 怎样算“第一次成功”

| 层级 | 最小成功信号 |
| --- | --- |
| 配置 | Preflight 通过，且没有打印或覆盖密钥 |
| Go | `/healthz` 正常，selected ICE pair 与预期网络一致 |
| Python | session 注册成功，ASR / LLM / TTS 没有跨轮错误 |
| 端侧 | 有真实上行音频、下行播放和正确的 playback report |
| 观测 | 同一 `trace_id` 能关联到请求和终态 |

端口可连、上传 ACK、mock、synthetic 或文件回放都不能单独称为真实 E2E。

## 故障先从哪里看

| 现象 | 第一检查点 | 然后看 |
| --- | --- | --- |
| 无法注册 | Robot ID / Secret、Runtime Snapshot | Go / Python 注册日志 |
| WebRTC 不通 | ICE candidate、selected pair、TURN 配置 | Rust 与 Go 的同 session 日志 |
| 有音频但无识别 | utterance 是否完整提交 | Go bridge、Python ASR、Qwen3-ASR endpoint |
| 识别后无回答 | Bot Snapshot、router kind | LLM 首 token、Agent / MCP 状态 |
| 有文本但无声音 | `tts_profile_id`、TTS provider | 首 PCM、downlink gate、Rust 播放 |
| 打断后仍播旧音频 | round / playback / generation 是否一致 | interrupt ACK 与迟到音频丢弃日志 |
| 配置改了但没生效 | Snapshot 版本、Apply / Reload | 进程 `--print-config` 与真实请求 |

完整排障顺序见 [`../START.md`](../START.md#8-故障定位顺序) 和
[`operations-reference.md`](./operations-reference.md)。

## 新人最容易踩的边界

- `.env.example`、bootstrap YAML 和测试 fixture 不是运行态真相源。
- V4 Compose 负责拆分 Python 服务；Go Gateway 仍走 host network 入口。
- Go 与 Python 的 M1 协议是配套链路，协议修改必须检查两侧和回滚路径。
- LLM 文本增量提交给 TTS，不要恢复句子或段落聚合。
- 共享路由、Vision、工具和复杂工作流默认面向所有启用 Bot，不用 Bot ID / 名称硬编码能力。
- 配置、端口、协议、音频格式或启动方式变化前，先检查所有调用端并登记 `docs/decisions.md`。
- `IMPLEMENTED`、`LOCAL_VERIFIED`、`LIVE_VERIFIED`、`HARDWARE_VERIFIED` 不能混用。

## 完成上手的自检

- [ ] 能画出 Rust → Go → Python → ASR / LLM / TTS 的主链。
- [ ] 知道 Runtime Snapshot 为什么高于 example 和历史文档。
- [ ] 能用五个关联 ID 定位一轮请求。
- [ ] 知道自己的任务应改哪个目录、跑哪个最小测试。
- [ ] 知道 localhost / mock / 文件回放为什么不是生产 E2E。
- [ ] 知道真实网络、MQTT、数据库写入和硬件操作需要单独授权。

完成这些项目后，再按角色深入阅读 [文档导航](./README.md) 中的对应主题即可。
