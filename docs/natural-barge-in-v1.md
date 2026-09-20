# 自然打断 V1

## 目标与边界

V1 面向单用户、相对安静环境，第一生产目标是讯飞语音硬件模组提供的系统默认麦克风和
扬声器。讯飞硬件完成 AEC、NS、AGC；Rust 只消费系统音频，不接入讯飞 SDK。

普通 TTS 与唱歌播放统一支持无需再次唤醒的自然打断。讯飞串口 KWS 保持最高优先级和
现有立即打断路径。短提示音、确认音不打开自然打断候选窗口。

V1 不新增 WebRTC PeerConnection 或音轨，复用现有上行 RTP、下行 RTP 和 control
DataChannel。Probe 只调用 ASR，不进入对话历史、LLM、MCP 或 TTS。

## 功能开关

三端开关必须同时有效，任意一端关闭都保持旧行为。服务端默认就绪，实体机由 Rust 开关控制
是否启用；Go/Python 仍可显式设置 `false` 做服务端回滚：

| 组件 | 开关 | 默认 |
|---|---|---|
| Rust | `NATURAL_BARGE_IN_ENABLED` | `false` |
| Go | `GO_VOICE_GATEWAY_BARGE_IN_ENABLED` | `true`（`python_gateway` 主路径） |
| Python | `GATEWAY_BARGE_IN_ENABLED` | `true` |

第一版默认参数：

- 前滚音频：400ms；
- 首次 Probe：候选开始后约800ms；
- 最多一次更新：候选开始后约1200ms；
- 单次 Go -> Python Probe 总超时：1000ms，超时继续播放；
- 同一播放最多3个 speech epoch；
- 同一 session 只允许一个活跃 Probe；
- 空文本、ASR失败、超时和歧义统一继续播放。

## 协议

所有消息复用 control DataChannel；WebSocket 只保留现有诊断兼容能力。

### `barge_in_start`（Rust -> Go）

开始一个只用于自然打断的临时音频段。Go 将后续上行 RTP 绑定到 `utterance_id`，但不得向
主 Python 会话发送 `input_audio.start`。

必填字段：

- `trace_id`、`utterance_id`；
- 当前 `round_id`、`playback_id`；
- `speech_epoch`；
- `sample_rate=16000`、`channels=1`、`opus_frame_ms=20`。

### `barge_in_probe`（Rust -> Go）

请求当前临时音频段的只读快照。必填字段：

- `trace_id`、`utterance_id`、`round_id`、`playback_id`；
- 严格递增的 `candidate_seq`；
- 当前 `speech_epoch`、`audio_watermark`。

首次和更新候选共用同一个 `utterance_id`。用户结束该 speech epoch 或播放结束后发送
`barge_in_cancel`，清理临时段但不得取消下行播放。

### `barge_in_decision`（Go -> Rust）

Go 将 Python ASR-only 结果返回 Rust：

- 身份字段完整回传；
- `decision=ignore|interrupt|new_intent`；
- 可选 `asr_text`、`asr_time_ms`、`reason`。

分类规则保持确定性：

- 空文本、失败、超时、纯附和表达：`ignore`；
- 明确停止当前输出且没有后续任务：`interrupt`；
- 其他有效表达，包括换歌、提问和新命令：`new_intent`。

不设置单字屏蔽。VAD 判断语音活动而不是语义长度；“停”等短表达仍属于边界能力。

### `barge_in_commit_ack`（Rust -> Go）

Rust 收到非 `ignore` 决策后，再校验当前状态仍为同一 `round_id` / `playback_id`，候选序号、
speech epoch 和 watermark 仍为最新，并确认 KWS 未抢占。

- 校验成功：先在本地停止旧播放并发送现有 `interrupt`，再发送 `accepted=true` ACK；
- 校验失败：发送 `accepted=false` 和原因，不得影响当前或下一轮播放。

Go 仅在有效 ACK 后处理决策：

- `interrupt`：只保留中断，不进入新业务轮次；
- `new_intent`：复用候选 ASR 文本进入现有文本主链，source 固定为
  `barge_in_candidate_asr`，不重复正式 ASR。
- Rust 的自然打断 `interrupt` 携带 `reason=natural_barge_in`。Go 立即停止下行 RTP，但主
  Python 会话中的旧轮次中断与新文本提升统一在 accepted ACK 后串行执行，避免异步转发让
  旧轮次中断晚于新文本到达。Go 读取后续 `barge_in_commit_ack` 前必须等待 internal voice
  interrupt ACK；Python 只有旧轮次 LLM/TTS 清理完成后才能返回该 ACK。
- Go 停止旧下行 RTP 时必须向 Rust 发送带原 `round_id` / `playback_id` 的
  `playback_cancel`。客户端只有同时观察到该取消事件和替换 `playback_start`，才能把后续
  RTP 归入新播放，不能以服务端 generation 变化代替协议闭环。

## 状态与优先级

```text
KWS interrupt
  > validated barge-in decision
  > playback complete/cancel
  > stale probe result
```

- KWS 到达时立即执行现有打断，取消 Probe，所有迟到结果按 stale 丢弃。
- TTS 在 Probe 返回前自然结束：取消 Probe；若用户仍在说话，转为现有正常录音流程。
- `ignore` 后仅允许既定的1200ms更新候选；随后必须等当前 speech epoch 结束再重新布防，
  不能在同一连续表达中继续请求。
- 唱歌与普通 TTS 使用同一状态规则；歌曲长播放需要独立限流和残余回声测试。

## 自动化测试矩阵

### Rust 状态机

| ID | 场景 | 预期 |
|---|---|---|
| BI-R-001 | 功能关闭 | TTS 状态不创建 Probe，旧 KWS 行为不变 |
| BI-R-002 | 800ms连续表达 | 发送首次候选且播放继续 |
| BI-R-003 | 1200ms仍连续表达 | 只发送一次更新 |
| BI-R-004 | `ignore` | 不停止播放，speech epoch 结束后重新布防 |
| BI-R-005 | `interrupt` | 本地先停播，再发 interrupt 和 accepted ACK |
| BI-R-006 | `new_intent` | 保留前滚音频，停止旧播放并确认提升 |
| BI-R-007 | 迟到/mismatch | rejected ACK，不影响新 playback |
| BI-R-008 | Probe期间KWS | KWS立即抢占，Probe取消 |
| BI-R-009 | 播放自然结束 | Probe取消；仍说话时进入正常录音 |
| BI-R-010 | 第4个speech epoch | 不再创建 Probe，KWS仍可打断 |

### Go 协议与并发

| ID | 场景 | 预期 |
|---|---|---|
| BI-G-001 | `barge_in_start` + RTP | 临时段收包，不提交主ASR |
| BI-G-002 | Probe snapshot | watermark之前音频只读快照正确 |
| BI-G-003 | `barge_in_cancel` | 只清理候选，不取消下行 RTP |
| BI-G-004 | stale decision/ACK | 不提升、不取消新播放 |
| BI-G-005 | accepted interrupt | 中断终态唯一，不提升文本 |
| BI-G-006 | accepted new_intent | 只提升一次，source正确 |
| BI-G-007 | 重复/乱序候选 | 旧序号丢弃，最新序号唯一有效 |
| BI-G-008 | 多session并发 | 不同session不串候选、不队头阻塞 |
| BI-G-009 | session关闭 | Probe、pending decision和连接全部释放 |
| BI-G-010 | new_intent提升 | internal interrupt ACK早于文本提升，旧新TTS不重叠 |
| BI-G-011 | 旧播放取消 | Rust收到原round/playback对应的`playback_cancel` |

### Python ASR-only

| ID | 场景 | 预期 |
|---|---|---|
| BI-P-001 | 空文本/ASR错误/超时 | `ignore` |
| BI-P-002 | 附和表达 | `ignore` |
| BI-P-003 | 明确停止表达 | `interrupt` |
| BI-P-004 | 普通新问题 | `new_intent` |
| BI-P-005 | “嗯，帮我查天气” | 按完整表达判 `new_intent` |
| BI-P-006 | “别唱了，换一首” | `new_intent` |
| BI-P-007 | Probe处理 | history、LLM、MCP、TTS调用数均为0 |
| BI-P-008 | 元数据非法/功能关闭 | 明确拒绝，不进入ASR |

### 音频、故障与端到端

| ID | 场景 | 预期 |
|---|---|---|
| BI-E-001 | 纯TTS回灌，多种衰减/延迟 | 0误打断 |
| BI-E-002 | 唱歌回灌和长播放 | 0误打断，无Probe风暴 |
| BI-E-003 | 静音、稳定噪声、脉冲噪声、咳嗽 | 0误打断 |
| BI-E-004 | TTS/唱歌与真人双讲 | 新表达完整保留并正确打断 |
| BI-E-005 | ASR超时、断连、重连 | 继续播放，资源最终释放 |
| BI-E-006 | RTP丢包、乱序、重复 | 不串轮次，失败时继续播放 |
| BI-E-007 | 连续多轮与多session soak | 无泄漏、无跨session提升 |
| BI-E-008 | 旧KWS回归 | 唤醒立即打断延迟不恶化 |
| BI-E-009 | 固定语音软件闭环 | `new_intent`、旧播放取消、替换RTP与替换`done`全部出现 |

### TTS-Base 模拟用户矩阵

- `scripts/run_natural_barge_in_matrix.py` 使用当前 Runtime Snapshot 中已启用、且不同于机器人
  当前 `wzk-base` 的 TTS-Base Profile 生成模拟用户语音。首轮仍使用固定真人录音，插话 WAV
  单独生成并绑定文本、Profile、16kHz/mono/PCM16 格式与 SHA-256。
- 首批矩阵覆盖天气换题、故事换题、换歌和继续提问。每条用例要求候选 ASR 命中人工定义的
  语义片段，并同时观察 `new_intent`、旧 `playback_cancel`、替换 `playback_start`、替换 RTP
  与替换轮次 `done`；不得把实际 ASR 输出回写为 Gold。
- 生成 WAV、清单、日志和 observation 只进入 `reports/`，不提交语音产物。该矩阵用于扩大
  软件链路覆盖，不评测硬件 AEC；生产讯飞语音硬件模组 AEC 按 owner 确认作为已满足前置条件。

## 实体机验收

软件测试完成后再在与生产同类的讯飞测试实体机生成 `HARDWARE_VERIFIED` 证据。至少覆盖：

- 普通 TTS 和唱歌各自的纯回声、双讲、距离、音量组合；
- 明确停止、新问题、换歌、附和与连续多轮；
- 每类关键打断20次，成功不少于19次；
- 固定无用户说话场景误打断为0；
- 从用户开始说话到旧播放停止目标不超过1.5秒，硬上限2秒；
- KWS立即打断和用户开头音频不得退化。

## 当前证据状态

- `IMPLEMENTED`：三端协议、Rust 默认关闭且服务端默认就绪、候选 ASR-only、两阶段确认、顺序化提升和自动化
  边界测试已实现。
- 本地自动化：仓库门禁、Rust loopback、Go/Python 测试通过后仍只作为实现证据；生成并校验
  `.ai/harness.json` 要求的结构化报告前，不声明 `LOCAL_VERIFIED`。
- `HYBRID_DIAGNOSTIC`：2026-08-21 使用固定 16kHz 录音跑通本地 Rust/Go/Python 与 LAN
  ASR/LLM/TTS；该结果验证软件自然打断闭环，但不是现场麦克风、扬声器或讯飞硬件证据。
- `HYBRID_DIAGNOSTIC`：同日使用四种 TTS-Base Profile 分别跑通天气换题、故事换题、换歌和
  继续提问；四条候选 ASR 均命中预设语义，替换 RTP 均大于 0，迟到音频均为 0。提交后的
  clean checkout 仍需复跑才能形成 `HYBRID_CANDIDATE`。
- `HARDWARE_VERIFIED`：待讯飞测试实体机按上述矩阵生成结构化证据后确认。
