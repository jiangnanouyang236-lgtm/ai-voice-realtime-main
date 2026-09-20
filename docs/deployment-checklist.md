# 当前主线发布检查清单

> 本清单用于当前 `main` 上的 V4 测试候选和未来正式发布。V3 已由
> `release/v3.0`、`v3.0.0-rc1`、`v3.0.0` 冻结保留。

## 1. Git 与产物

```bash
git status --short
git branch --show-current
git log --oneline -5
```

- 不提交 `.env`、模型、日志、`target/`、`dist/` 或编译二进制。
- Go/Rust 发布包由各自 Makefile 生成，不把产物复制回源码目录。
- 本轮若包含已有未提交改动，先确认归属并按逻辑拆分。
- 发布候选必须先完成审核和 release gate，再快进或合入 `main`；不能直接部署未记录的
  浮动 feature/codex 分支。
- 记录候选 commit；Gold 和 Hybrid/Live evidence 必须绑定该 commit 与干净 diff。

## 2. 数据库

- `CONFIG_DATABASE_URL` 指向目标 server-config。
- 首次部署执行 `python scripts/init_config_db.py`。
- 旧库按顺序执行尚未应用的 `scripts/migrations/*.sql`。
- 当前必须存在 `bots.max_response_chars`；缺少时应用 `20260702_bot_max_response_chars.sql`。
- TTS Profile 环境需先应用 `20260701_tts_profiles.sql`。
- Apply / Reload 后，Gateway、LLM、TTS 的 Snapshot 版本一致。

## 3. 模型服务

ASR：

```text
STT_PROVIDER=qwen
QWEN_ASR_BASE_URL=http://<asr-host>:<port>/v1
QWEN_ASR_API_KEY=<secret>
QWEN_ASR_MODEL=Qwen3-ASR-1.7B
```

LLM：

```text
LLM_BASE_URL=http://<llm-host>:<port>/v1
LLM_API_KEY=<secret>
LLM_MODEL_NAME=<served-model-name>
LLM_ROUTER_BASE_URL=http://<router-host>:<port>/v1
LLM_ROUTER_API_KEY=<secret>
```

TTS：

```text
QWEN3_TTS_CUSTOM_VOICE_WS_URL=ws://<tts-host>:<port>/v1/audio/speech/stream
QWEN3_TTS_CUSTOM_VOICE_API_KEY=<secret>
QWEN3_TTS_CUSTOM_VOICE_MODEL=qwen3-tts
QWEN3_TTS_BASE_WS_URL=ws://<tts-base-host>:<port>/v1/audio/speech/stream
QWEN3_TTS_BASE_API_KEY=<secret>
QWEN3_TTS_BASE_MODEL=qwen3-tts-base
```

- 生产 TTS 不再读取旧 `LOCAL_QWEN3_TTS_API_KEY` / `LOCAL_QWEN3_TTS_WS_URL`。
- 增量文本直接提交到 TTS WS，不恢复句子/分句聚合。

## 4. Gateway 与网络

```text
GATEWAY_REQUIRE_ROBOT_SECRET=true
GO_VOICE_GATEWAY_ADDR=0.0.0.0:8282
GO_VOICE_GATEWAY_REQUIRE_ROBOT_SECRET=true
GO_VOICE_GATEWAY_DEVICE_AUTH_BACKEND=python_gateway
GO_VOICE_GATEWAY_ASR_PYTHON_GATEWAY_WS_URL=ws://127.0.0.1:7860/ws
GO_VOICE_GATEWAY_ASR_PYTHON_GATEWAY_CONNECTION_MODE=session
GO_VOICE_GATEWAY_ASR_PYTHON_GATEWAY_TOTAL_TIMEOUT_MS=45000
GO_VOICE_GATEWAY_ASR_PYTHON_GATEWAY_SESSION_IDLE_RECYCLE_MS=30000
GO_VOICE_GATEWAY_DOWNLINK_AUDIO_TRANSPORT=webrtc_rtp
GO_VOICE_GATEWAY_ICE_TRANSPORT_POLICY=all
GO_VOICE_GATEWAY_ICE_NETWORK_TYPES=udp4
GO_VOICE_GATEWAY_RTC_UDP_MIN_PORT=35500
GO_VOICE_GATEWAY_RTC_UDP_MAX_PORT=35600
VISION_INTERNAL_TOKEN=<同一随机内部令牌，注入 Go Gateway 与 LLM>
GO_VOICE_GATEWAY_VISION_INTERNAL_TOKEN=<裸 Go 进程使用同一令牌>
```

- `GO_VOICE_GATEWAY_ICE_SERVERS` 为空时只有公共 STUN。
- `VISION_INTERNAL_TOKEN` 必须使用未提交到 Git 的随机值；裸进程还必须保证
  `LLM_VISION_GATEWAY_TOKEN` 与 `GO_VOICE_GATEWAY_VISION_INTERNAL_TOKEN` 同值。
  缺失或两端不一致时，
  视觉问题不会取得图片，但原有语音、Robot MCP 和 WebRTC 音频不受影响。
- 生产跨 NAT 必须从密钥系统/未跟踪 `.env` 注入 TURN JSON。
- 防火墙放行 `8282/tcp` 和配置的 WebRTC UDP 范围。
- `/internal/status` 不直接暴露到不可信公网。
- `LLM_COMPLEX_WORKFLOW_ENABLED` 默认保持 `false`；若发布负责人决定启用，必须增加
  复杂工具组合、打断、终止任务播放屏障和副作用不重放的独立验收。
- 自然打断要求 Rust `NATURAL_BARGE_IN_ENABLED` 明确启用；Go/Python 服务端默认就绪不等于
  设备已启用。实体机双讲通过前不得声明 `HARDWARE_VERIFIED`。

上线前检查脱敏后的最终配置：

```bash
cd go_voice_gateway
go run . --print-env-help
go run . --print-config

cd ../rust_client
ROBOT_ID=test_01 cargo run --bin rust_client --features native-webrtc -- --print-config
```

## 5. Compose

```bash
make compose-network
make models-config
make compose-v4-config
make compose-v4-up
make compose-go-up
python -m pytest test/test_compose_v4_contract.py -q
```

确认：

- Go Gateway 使用 host network，监听 `8282`。
- V4 Python Gateway/STT/LLM/TTS/Admin 只按预期地址暴露。
- Go 能访问 `127.0.0.1:7860/ws` 和 `/internal/voice/ws`。
- 模型 endpoint、模型名、API key 与 vLLM served name 一致。
- `SINGING_AUDIO_V4_HOST_DIR` 使用绝对宿主机路径，挂载目标恰好一次且只读；镜像中不含真实 WAV。
- Runtime Snapshot 不再绑定专用 `weather_remote`，天气走 Websearch MCP。

## 6. 代码门禁

RC 和正式发布统一运行仓库 release gate：

```bash
python scripts/ai_preflight.py --profile offline
python scripts/ai_quality_gate.py --mode release
```

release gate 不连接真实服务、不发布 MQTT、不操作硬件。按本次发布声明的能力，
另行运行 `scripts/eval_acceptance.py` 或 `scripts/run_dev_harness.py`，并用
`scripts/validate_evidence_report.py` 校验报告。硬件保持 `DEFERRED`；TURN TCP/TLS
保持 `UNKNOWN`，除非另有同一提交上的正式证据。

日常文档修改只需 changed gate；但最终 RC 无论改动类型都必须运行 release gate。

## 7. 链路烟测

```bash
python scripts/smoke_go_webrtc_python_gateway.py \
  --sample "$SAMPLE_WAV" \
  --client rust
```

检查同一 `trace_id` 下：

- Rust 注册、ICE route、RTP uplink、playback/interrupt。
- Go RTP stats、Python bridge、downlink gate。
- Python ASR、LLM 首 token、TTS 首音和 `done`。
- 没有迟到 round/playback 被错误播放。

## 8. 回滚

- 网络故障优先使用 ICE/TURN，不在 Go 内自动切换 WS 音频。
- 需要 WS 音频时，明确运行 v2 Rust Client 直连 Python Gateway。
- 回滚前保留数据库 Snapshot 版本、镜像 tag 和对应 Git commit。
- V4 主链故障可回滚到 `v3.0.0` 或 `release/v3.0`；不要移动已有 V3 tag。

## 9. 发布确认

- `docs/voice_gateway_task_tracker.md` 已记录候选状态和未完成能力。
- 正式 V4 发布需创建新的 annotated tag；仅更新 `main` 不等于正式发布。
- 已知限制明确保留：物理硬件未验证、TURN TCP/TLS 未验证、MQTT 消息不等于实体动作。
