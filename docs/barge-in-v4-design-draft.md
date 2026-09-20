# V4 可打断式双工设计草案（暂停）

> 状态：`PAUSED / DESIGN ONLY`
>
> 本文记录 2026-08-12 至 2026-08-13 对自然 Barge-in 的阶段性讨论，避免后续丢失上下文。
> 当前不授权实现，不代表最终协议或上线方案；恢复前必须重新核对真实设备日志、当前分支和运行配置。

## 目标与边界

第一阶段只解决单用户、相对干净环境下的自然打断：AI 播放 TTS 时，用户真正插话应能快速停止播放；
咳嗽、清嗓、短促噪声和 Backchannel 尽可能不触发。多人环境的声纹、DOA 和 Beamforming 延后。

硬件唤醒词继续保持最高优先级和立即打断能力，不经过普通 Barge-in 候选判断。

## 已确认的当前事实

- Rust `DialogueState` 目前包含 `TtsPlaying`，并已有默认关闭的 `TTS_BARGE_IN_WITH_AEC` 实验路径；
  该路径是 VAD 触发后直接停止 TTS，不适合作为正式自然打断策略。
- 当前 WebRTC VAD 只输出 speech 布尔值，另有 dBFS；没有可供平滑的 probability。
- 当前生产 ASR 使用 unary `RecognizeSpeech`。proto 虽保留 `StreamRecognize`，实现仍会收齐音频后一次推理，
  不能把它当作真正的 Streaming ASR partial。
- Rust 本地播放取消、Go 下行 RTP generation gate、Python active turn 取消、round/playback ID 隔离和录音
  prebuffer 已存在，应复用这些原语，不重造第二套中断链路。
- Smart Turn 与 EOU 用于判断新用户轮次何时结束，不负责判断播放期间的用户声音是否具有打断意图。

## 推荐状态设计

不要用 `BargeInCandidate` 替换顶层 `DialogueState::TtsPlaying`。候选判断期间 TTS 仍应继续播放，
因此使用与播放状态正交的子状态：

```text
BargeInState = Idle | Candidate | Suppressed | Confirming
```

基本转换：

```text
TTS Playing + VAD speech
  -> Candidate（继续播放、缓存音频）
  -> 明确打断词或有效 speech 达标：Confirming
  -> 短噪声或已确认 Backchannel：Suppressed / 丢弃
  -> Confirming：原子停止旧播放，将候选音频提升为新 Recording
```

候选必须绑定 `session_id + playback_generation + round_id + candidate_id + speech_epoch`；
任何迟到或不匹配的异步 ASR 结果都必须丢弃。

## 建议初始参数（尚未实机定稿）

| 参数 | 初始探索值 | 说明 |
| --- | ---: | --- |
| candidate start window | 150--250ms | 发现候选，不等于确认打断 |
| normal interrupt speech | 450ms | 累计 VAD 阳性帧时长 |
| VAD gap tolerance | 100ms | 容忍短暂 false，避免拆句 |
| short candidate end silence | 200--250ms | 收口短词/噪声 |
| pre-roll | 200ms | 保留首字，不与 wake prebuffer 共用配置 |
| candidate buffer cap | 2s | 防止异常无限缓存 |
| suppressed cooldown | 200ms | 只抑制噪声尾部抖动 |

持续讲话使用“累计阳性帧 + 短 gap 容忍”，不使用候选开始后的纯墙钟时间，也不复用现有
`VAD_SPEECH_THRESHOLD` 滑动窗口作为最终确认条件。

## 语义规则

- 明确打断词优先于普通持续时间门限，但必须基于可靠 ASR 结果并按完整短语/边界匹配。
- Backchannel 不能根据不稳定 partial 前缀提前判定；只有文本稳定结束且规范化后严格落入白名单，才丢弃候选。
- `好，但是……`、`好的，等一下`、`明白了，不过……` 必须继续保留为候选，不能被白名单前缀误杀。
- 当前没有真实 partial。第一版可先做 duration-only Shadow；短候选语义可复用独立 unary ASR，
  但不得冒充正式 `input_audio` 或提前取消当前轮次。

## 分阶段验证

1. Shadow：只记录 candidate、VAD/dBFS、speech/gap、规则结果、延迟和播放 generation，不实际停止 TTS。
2. 受控 Active：单人安静环境先启用 duration-only，硬件唤醒词保留为兜底。
3. 候选 ASR：只处理短候选的明确打断词和严格 Backchannel。
4. 真实 Streaming ASR 或专用分类器：只有真实数据证明 unary ASR/规则不足时再引入。

测试必须覆盖 TTS-only 回声、咳嗽/清嗓、Backchannel、明确打断、普通长插话、播放刚开始/即将结束、
候选期间唤醒、迟到 ASR、迟到 RTP、断线重连和连续两次打断。主要指标为错误打断率、漏检率、
首个真实 speech 到扬声器停止延迟、Backchannel 保留率、首字保留率和迟到音频隔离率。

## 恢复条件

完成当前 Smart Turn/EOU False End 修复并获得新的实机日志后，再恢复本设计。恢复时先写协议/状态机测试，
保持现有唤醒词立即打断路径不变，并从 Shadow 开始，不直接开启正式 Barge-in。
