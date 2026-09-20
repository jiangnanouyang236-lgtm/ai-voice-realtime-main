# Turn Gate V1 设计与推进状态

状态：`ACTIVE_RUNTIME_IMPLEMENTED / LIVE_NOT_VERIFIED / HARDWARE_NOT_VERIFIED`。

当前已完成 Smart + EOU Shadow：Rust 可在 300ms 静音时通过 WebRTC control DataChannel 发送
candidate；Go 从仍处于 Recording 的 ASR segment 复制只读 Opus 快照，通过独立队列和独立
Python WebSocket 连接发起候选请求；Python 并行执行候选 ASR 与 Smart Turn，再将 ASR 文本和
可用的主会话历史交给 LiveKit EOU，返回内部 `turn_candidate_result`。结果包含两个 probability、
阈值、布尔意见、各阶段延迟、agreement 和 `policy_preview`，但不会加入用户请求队列。
该 Shadow 链路不会关闭 segment，也不改变当前 500ms `audio_end`。
另已实现 Active 两阶段提交：Go 仅对配置的绝对窗口内的当前双 True
candidate 发送 `turn_commit_request`；Rust 再校验本地 VAD epoch、watermark 和持续静音，
回复 `turn_commit_ack`。只有 accepted ACK 才关闭录音，Go 随后把已有 candidate ASR
文本晋升到原 Python LLM/TTS 主链，不再执行第二次正式 ASR。任一模型明确判断未说完时，
Go 发送 `turn_candidate_decision=continue`；Rust 校验同一候选后将静音上限延至 1600ms。
模型失败、超时、不可用或反馈丢失时不发送 continue，由 1000ms failure fallback 收口。

固定 WAV 已通过 Rust audio_frontend TCP 回放、真实 VAD、Go WebRTC/RTP、候选 ASR、
Smart Turn、LiveKit EOU、正式 ASR 和原有 LLM/TTS 路径运行；已覆盖一次 400ms 句中停顿
后的恢复说话。Active 协议、过期/拒绝/fallback 和 ASR 文本晋升已通过自动测试，
但尚未在真实可用的 ASR/LLM 服务、真人麦克风和硬件声学环境下运行，因此不能声称
Active Turn Gate 或完整硬件语音 E2E 已完成。
V1 不引入 4B Judge。

## 目标

在当前 VAD-Only 基线上增加可重试的轮次结束判断：

```text
300ms provisional candidate
  -> Smart Turn 与 ASR 并行
  -> ASR transcript 进入 LiveKit EOU
  -> Smart=true 且 EOU=true 时具备 early-commit 资格
  -> 任一模型明确 false 时继续收音至 1600ms 硬上限
  -> 模型或链路不可用时由 1000ms failure fallback 正式提交
```

早期方案只允许“双 True”比现有 VAD 提前，其余情况仍由 500ms VAD 收口；当前 Active
实现已将明确 false 与基础设施失败拆成 1600ms hard timeout 和 1000ms failure fallback。
当前 50 条联合集的双 True False End Rate 为 48%，且
真实 WebRTC Shadow 中“如果明天下雨的话”也出现双 True 误结束，因此 Active 提交暂不放行。
Rust 端现已按线上策略默认启用 Active；Go/Python 仍使用各自独立开关，只有三端
有效配置均开启才形成完整 Active 链路。需要回退时可单独关闭 Rust Active。

## 当前事实边界

- 当前统一 VAD 基线为 mode `2`、start min `-36dBFS`、speech window `0.25s`、trigger
  ratio `0.6`、silence `0.5s`、max recording `10s`。
- 当前 Rust 达到 VAD silence threshold 后立即发送 `audio_end`、播放确认音并进入
  `SendingAudio`，所以不能直接把该变量改成 `0.3`。
- 当前生产 STT 是完整 utterance 边界上的 unary Qwen3-ASR；provisional audio snapshot
  已经过固定 WAV + Go WebRTC/RTP + Python/内网 ASR 的真实进程验证。
- 三条 Shadow 样本均未关闭原 segment，200ms 后的 legacy `audio_end` 和正式 ASR/LLM/TTS
  均完成；candidate 总耗时分别约 423ms、483ms、513ms。该结果仍不代表真人麦克风、
  Rust VAD candidate、恢复说话或生产并发已验证。
- 一条 358 包样本在 Go 侧仅收到 357 包，candidate 与正式 ASR 均使用相同 357 包；watermark
  可暴露差异，但当前没有重传或补包，后续必须继续保留 packet-loss 指标。
- 强制将 candidate 总预算设为 `1ms` 时，隔离连接按预期超时，随后 legacy `audio_end`、正式
  ASR、LLM 和 TTS 均完成，证明 Shadow 失败不会阻塞主路径。
- “停顿 -> candidate 1 -> 恢复说话 -> candidate 2”真实 WebRTC 测试发现并修复了异步快照
  越过 watermark 读取恢复语音的问题；重跑后 candidate 1 严格为 112 包，candidate 2 使用
  speech epoch 1 并覆盖后续完整 segment。Active 已接入显式 cancel、云端 stale 检查和
  Rust 本地二次新鲜度校验；该部分已单测，尚待真实 Active WebRTC 运行验证。
- Rust 实际 VAD 固定音频回放中，首次最终静音在 `303ms` 发出 candidate，约 `202ms` 后才
  发出正式 `audio_end`；candidate 覆盖 140 包，正式请求覆盖 151 包，完整 ASR 文本正确。
- Rust 实际 VAD 的 400ms 句中停顿回放中，candidate 1 为 `seq=1 / epoch=0 / 304ms / 90包`；
  恢复说话没有结束录音；最终停顿产生 `seq=2 / epoch=1 / 302ms / 160包`，约 199ms 后正式
  `audio_end` 覆盖 171 包，完整 ASR 文本正确。
- 第二个 candidate 的模型计算约 168ms，但隔离连接端到端约 318ms，仍晚于 500ms fallback
  约 119ms；当前连接方式在本机没有提前提交收益，服务端部署需重新测量或复用连接。
- 4 个预编译 Go WebRTC 客户端并发测试中，candidate 在约 277ms 内全部到达，但当前全局
  candidate queue 只有一个消费者，因此严格串行完成。第 1 路从到达到完成约 753ms；后续
  3 路虽然各自出队后的处理仅约 210ms、196ms、284ms，包含排队后的实际等待约为 879ms、
  928ms、1168ms。更强 CPU 会缩短模型计算，但不会自动消除单消费者的队头阻塞。
- 当前 `turn_candidate_python_done duration_ms` 从出队并开始 Python bridge 调用后计时，
  不包含 queue wait；只看该字段会低估并发会话的实际 candidate 延迟。Active 前需要补充
  enqueue-to-start、queue wait 和 enqueue-to-done 指标。
- `GO_VOICE_GATEWAY_TURN_GATE_SHADOW_TIMEOUT_MS` 当前同样只在 candidate 出队后约束 Python
  bridge，并不是从 candidate 产生时开始的端到端预算；队列拥塞时请求可能在已失去提前提交
  价值后才开始计算。Active 已在提交决策处使用从 candidate 到达 Go 开始的绝对
  `200ms` deadline，过期结果不会 commit；但当前过期项仍可能浪费队列/模型计算，
  尚未做出队前的快速淘汰。
- 当前 300ms candidate 到 500ms fallback 只有约 200ms 决策窗口。50 条真实 ASR 联合结果中，
  只计算 `max(Smart, ASR) + EOU` 且不计 WebSocket/queue 的情况下有 41/50 小于等于 200ms。
  官方 EOU 阈值的 35 条双 True 中有 28 条能在 200ms 内完成，但其中 12 条 Ground Truth 为
  CONTINUE；服务器加速只能提高及时返回率，不能修复这些 False End。
- 共享常驻模型的 8 次单线程/4 线程对照中输出完全一致且无异常，但 Smart 吞吐仅从 10.30
  增至 11.02 req/s，EOU 仅从 19.12 增至 20.51 req/s；4 并发单请求最大耗时分别约 365ms、
  197ms。原因是每个模型实例都有全局 inference lock，有界 Go worker pool 不能单独解决
  模型层并发，需要在服务器评估多实例、独立推理服务或受控批处理。
- 同一轮并发测试还确认原有正式 ASR handoff queue 也是全局单消费者，而且 bridge 调用会
  等待整轮 LLM/TTS `done`。3 个成功进入主链路的会话被串行处理，后续会话分别在前一轮
  约 14--16 秒完成后才开始。这是既有主链路的跨会话队头阻塞，不是 Turn Gate 引入，但
  如果直接复用当前队列模型上线，会掩盖 Turn Gate 的低延迟收益。
- 首轮使用 4 个并发 `go run` 客户端时有 1 路在本机触发 5 秒 ICE gathering timeout；改用
  预编译客户端后 4 路均成功建立 DataChannel/ICE，说明前者更可能是并发编译和本机调度抖动。
  服务器部署仍需单独做连接建立压力测试，不能将这次重跑升级为生产并发验证。
- 测试中还观察到一次启动阶段过早发送 HTTP wake 时，内部语音会话尚未准备完成并返回
  `CLIENT_EVENT_ACTIVE_FAILED`。延后唤醒后正常；这是独立的启动时序问题，不计入 Turn Gate
  准确率，但正式启用前应增加 ready gate 或受控重试。
- 三端开关按进程独立命名：Rust 使用 `TURN_GATE_*`，Go 使用
  `GO_VOICE_GATEWAY_TURN_GATE_*`，Python 使用 `GATEWAY_TURN_GATE_*`。Rust Active 默认
  `true`，Go/Python 默认 `false`；三端之间不存在环境变量继承，避免共享 `.env` 时误开启
  其他进程。
- `TURN_GATE_SHADOW_ENABLED` 只控制 Rust candidate；只有三端 Shadow 均显式开启时才形成
  metadata-only candidate 和候选音频快照，关闭后完整保持原有 VAD-Only 行为。
- 三端 Active 开关必须分别生效；Rust 可使用默认值，Go/Python 仍需各自配置；
  Go Active 还强制使用 `python_gateway` processor。任一层未开启都不得隐式改变主路径。
- accepted commit 会直接复用 candidate ASR 文本，并以原 `trace_id + utterance_id`
  进入 Python 文本主链，不再提交正式 ASR。若 ACK 后 Python 主链连接/发送失败，
  端侧已经停止录音，无法再回到 500ms fallback；当前会明确记录 promotion failure，
  这是真实 Active 放量前必须通过故障注入和可用性门禁验证的剩余风险。
- 固定 WAV + Go WebRTC Active 实链在真实 LAN ASR/LLM/TTS 下已覆盖两条路径：
  正常 `200ms` 提交窗口中，candidate 未及时返回，250ms 后安全 fallback，
  正式 ASR/LLM/TTS 完成；仅用于协议验收的 `5000ms` 诊断窗口中，
  `turn_commit_request -> accepted ACK -> turn_commit -> candidate ASR 文本晋升`
  完整成功，未发送 legacy `audio_end`，STT 日志只有一次推理。
  该结果证明协议与 ASR 复用可行，不证明 5 秒窗口可用于产品。
- 第一轮 Active 还暴露了候选 Python WebSocket 注册/建连约 3--4 秒的开销。
  Active 现已改为按 Go session 持久化 candidate 连接，并在 `audio_start`
  异步预热；Shadow 仍保持 per-turn 隔离。重跑时建连与 2.7 秒语音重叠，
  Go candidate 总耗时从约 4.0--4.2 秒降到约 2.25 秒，但首轮候选 ASR 仍约
  1.49 秒，明显超过 200ms 窗口。同轮 fallback 正式 ASR 预热后仍约 291ms，
  所以当前环境的 `300ms candidate + 500ms fallback` 不应期待稳定 early commit。
- 候选 ASR 使用独立连接与独立队列，不占用正式 ASR 的 session bridge；失败和超时只记录，
  不阻止后续正式 `audio_end` 和主 ASR 请求。但“独立队列”目前不等于“并发处理”：候选和
  正式队列各自都只有一个全局消费者。
- Smart Turn 与候选 ASR 并行；EOU 必须等待 ASR 文本。候选连接优先引用已有主 Python
  session 的历史，首轮或主 session 尚未建立时明确使用空上下文。
- `GATEWAY_TURN_GATE_MODELS_ENABLED` 默认 `false`。显式启用后启动时预热两个模型；依赖使用独立的
  `requirements-turn-gate.txt`，避免普通 Gateway 安装被模型运行时强制放大。
- 当前默认沿用已锁定语义：Smart `> 0.5`，中文 EOU `>= 0.0066`。Shadow 结果同时记录实际
  probability 与阈值，后续可离线比较 `0.0066` 和 `0.03`，不能只保留布尔结果。

## Candidate 契约

候选必须绑定以下字段：

| 字段 | 语义 |
| --- | --- |
| `utterance_id` | 同一段用户发言的逻辑 ID |
| `candidate_seq` | 该 utterance 内严格递增的候选序号 |
| `speech_epoch` | 每次恢复说话后递增，用于淘汰旧结果 |
| `audio_watermark` | 候选覆盖到的单调音频位置 |
| `silence_ms` | 产生候选时已观察到的静音长度 |

只有仍匹配当前 `utterance_id + candidate_seq + speech_epoch + audio_watermark` 的结果可以
触发 commit。用户恢复说话后，所有在途旧 candidate 都必须 best-effort cancel，并在返回时
按 stale 丢弃。

持续静音不会重复产生相同 candidate。只有用户恢复说话并再次进入静音，才允许创建下一
candidate；否则等待当前 Active fallback；Turn Gate 关闭时仍由 legacy 500ms VAD 提交。

## 决策表

| Smart | EOU | 300ms candidate 动作 | Active 后续动作 |
| --- | --- | --- | --- |
| false | 任意 | continue | 最长继续听到 1600ms |
| true | false | continue | 最长继续听到 1600ms |
| true | true | early commit | 已提交时不再触发 |
| 失败/超时 | 任意 | 无可信反馈 | 1000ms failure fallback |

## 时间与失败语义

建议第一轮受控测试使用不同配置项，禁止复用一个 VAD threshold 表达多种语义：

| 配置 | 初值 | 作用 |
| --- | ---: | --- |
| `TURN_GATE_CANDIDATE_SILENCE_MS` | 300 | 产生 provisional candidate |
| `TURN_GATE_MIN_COMMIT_SILENCE_MS` | 600 | 双 True 最早允许提交的连续静音点 |
| `TURN_GATE_FAILURE_FALLBACK_MS` | 1000 | Active 下没有可信语义反馈时收口 |
| `TURN_GATE_AMBIGUOUS_HARD_TIMEOUT_MS` | 1600 | Active 下明确 continue 后的静音硬上限 |
| `VAD_SILENCE_THRESHOLD` | 0.5 | Turn Gate 关闭时保留原 VAD-Only 行为 |

- Smart/EOU 基础设施不可用：不产生 continue/commit 指令，按 1000ms failure fallback 提交。
- 用户恢复说话：候选失效，录音和 RTP 上送不能中断。
- Active accepted：不发送 legacy `audio_end`；Go 发送内部 `turn_commit`关闭 segment，
  并将 candidate ASR 文本晋升到主 LLM/TTS 路径。
- legacy commit：只在 Active 未提交时正式发送 `audio_end`，并继续原正式 ASR 路径。
- provisional candidate 不能播放 ASR ding，确认音只允许在正式 commit 后播放。

## 本地固定音频实测（2026-08-11）

以下结果来自真实 Rust Client、WebRTC、Go Gateway、Python Gateway、Qwen ASR、
Smart Turn 和 LiveKit EOU 本地进程，不是 mock；输入仍是固定 WAV 和 TCP audio frontend，
因此只能作为本地诊断证据，不能标记为真人硬件验证。

- 普通句尾：Rust 在约 304ms 静音时发送 candidate，约 206ms 后到达 legacy 500ms
  `audio_end`；candidate 全链路耗时约 1874ms，未赶上 200ms Active 提交窗口，系统安全走
  VAD fallback。正式 ASR 约 324ms，原 LLM/TTS 路径完成。
- 400ms 句中停顿：Rust 在 300ms 发送 `candidate_seq=1/speech_epoch=0`；恢复说话约 94ms
  后发送 `turn_candidate_cancel`，并推进到 `speech_epoch=1`；句尾重新发送
  `candidate_seq=2`。Go 同步收到 cancel，旧候选完成后返回 `no_active_candidate`，没有错误
  commit，录音最终包含完整句子。
- 该轮首次候选总耗时约 1782ms；热态第二候选约 374ms（ASR 344ms、Smart 99ms、EOU
  26ms）。两者都超过 200ms Active 窗口，因此当前 Mac 本地环境尚不能依靠双 True 在
  500ms 前稳定 early commit。
- 800ms 自然停顿样本在约 300ms 产生 candidate，随后仍被 legacy 500ms VAD 提交，只识别
  到停顿前半句。这是 500ms fallback 的边界，不是 Smart/EOU 判断错误：语义结果来不及
  返回时，当前决策表中的“任意 false 继续听”最多只能继续到 500ms。
- 当前 cancel 会阻止旧结果提交，但不会中止已经进入 Python/ASR 的旧候选推理。400ms
  用例实际产生旧 candidate ASR、正式 ASR、新 candidate ASR 三次推理；正确性成立，但在
  高并发前还需要增加云端取消/去重，或只允许一个有效 candidate 占用推理资源。

因此，`VAD_SILENCE_THRESHOLD=0.5` 可以先用于受控灰度，但不能直接宣称已兼容较长的自然
思考停顿。真人测试应重点统计 500--1000ms 停顿分布；若截断明显，应在不改变 300ms
candidate 的前提下，将 fallback 单独放宽到 800--1000ms，或先提升 candidate 推理速度。

### 线上目标延迟时序回放

使用已有 50 条 synthetic 对齐结果，将线上目标预算设为 ASR 150ms、Smart 50ms、EOU
20ms，另预留 30ms 控制开销。Smart 与 ASR 并行、EOU 位于 ASR 之后，因此 candidate 后
语义路径为 `max(150, 50) + 20 + 30 = 200ms`：300ms candidate 预计在静音约 500ms
得到首次双 True，300ms Active deadline 足够。Rust 再等待到 600ms 最小稳定静音点，因此
目标 early commit 为约 600ms；若模型结果晚于 600ms，则结果到达后立即提交，不重复等待。

离线状态机回放结果如下；“保守 False End”把 commit 与恢复说话发生在同一毫秒的样本也
计作风险：

| 策略 | 确定 False End | 保守 False End | END early commit | END 平均提交延迟 |
| --- | ---: | ---: | ---: | ---: |
| VAD-Only 500ms | 18/25 | 22/25 | 0/25 | 500ms |
| 模型不可用，800ms fallback | 8/25 | 15/25 | 0/25 | 800ms |
| 模型不可用，1000ms fallback | 3/25 | 8/25 | 0/25 | 1000ms |
| 双 True，EOU 官方阈值 0.0066，hard 1500ms | 9/25 | 13/25 | 21/25 | 660ms |
| 双 True，EOU 同集探索阈值 0.1，hard 1500ms | 1/25 | 5/25 | 18/25 | 780ms |
| 双 True，EOU 同集探索阈值 0.1，hard 1600ms | 1/25 | 2/25 | 18/25 | 808ms |
| 上一行 + 600ms 最小稳定静音 | 1/25 | 1/25 | 18/25 | 880ms |

600ms 保护在这批数据上把 EOU 0.1 组合的保守 False End 从 2/25 降到 1/25，END 平均提交
延迟从 808ms 增至 880ms（+72ms）；18 个 early-commit END 各增加 100ms，其余 hard-timeout
样本不变。这是静态 synthetic 回放，不是新的真人或硬件证据。

这批结果说明线上延迟预算足以支持约 600ms early commit，但官方 EOU 中文阈值下双 True
仍不够安全。`0.1` 与 1600ms 组合只是在同一 50 条数据上的探索点，不能直接成为生产
默认；应先用独立真人/holdout 集复验。hard timeout 从 1500ms 改到 1600ms 在本批只增加
约 28ms END 平均延迟，却避开三条正好 1500ms 恢复说话的竞态，因此下一轮测试优先使用
1600ms。

故障 fallback 从 800ms 放宽到 1000ms，会将本批保守 False End 从 15/25 降到 8/25。
这条路径只在模型不可用或超过 Active deadline 时触发，额外 200ms 不影响正常双 True
提交，因此下一轮也优先使用 1000ms failure fallback。

上述参数已固化为 `turn-gate/configs/single-duplex-v1-test.json` 测试 Profile。它不是生产
默认：candidate、Active deadline、模型阈值、1000ms failure fallback 与 1600ms ambiguous
hard timeout 均已有运行时入口；当前只达到 `IMPLEMENTED`，尚未通过真人麦克风与硬件声学验证。

真机首轮测试应显式使用：`TURN_GATE_CANDIDATE_SILENCE_MS=300`、
`TURN_GATE_MIN_COMMIT_SILENCE_MS=600`、
`GO_VOICE_GATEWAY_TURN_GATE_ACTIVE_DEADLINE_MS=300`、`TURN_GATE_FAILURE_FALLBACK_MS=1000`、
`TURN_GATE_AMBIGUOUS_HARD_TIMEOUT_MS=1600`、`GATEWAY_TURN_GATE_SMART_THRESHOLD=0.5`、
`GATEWAY_TURN_GATE_EOU_THRESHOLD=0.1`。其中 EOU `0.1` 仍是同集探索值，必须保留 probability 日志，
不能当作生产校准结论。

## 推荐落点

- Rust Client：维护 silence epoch、candidate、过期结果和最终 `audio_end`。
- Go Gateway：从仍在接收的 RTP utterance 构造只读 candidate snapshot，并保留原始 session、
  packet loss 和 watermark 证据；candidate 不得关闭 ASR segment。
- Python Gateway：并行调度 Smart 与现有 STT，随后执行 EOU；只有正式 commit 才进入主
  LLM、MCP、历史和 TTS。
- Smart/EOU：V4 中作为独立常驻 Turn Gate 推理单元，避免把大模型常驻内存复制到每个
  Python Gateway worker。
- 并发模型：候选请求使用有界 worker pool，并按 ASR/Smart/EOU 的真实承载能力设置 worker
  数和队列预算；同一 utterance 仍按 candidate 序号保持有序，不同 session 不应互相等待。
  正式 ASR/LLM/TTS 的全局串行问题应作为独立改造处理，不能通过扩大 queue size 掩盖。

## 推进门禁

1. `POLICY_IMPLEMENTED`：纯策略和 stale candidate 单测通过，不改变运行行为。
2. `SHADOW_IMPLEMENTED`：300ms candidate 能端到端到达 Python 并返回决策，但 Rust 仍按
   legacy 500ms 提交；结果只记 trace。
3. `LOCAL_VERIFIED`：真实本地进程证明 candidate 不关闭 utterance、恢复说话无丢音、旧结果
   被丢弃、ASR 可复用，并生成结构化证据。
4. `HARDWARE_VERIFIED`：真人麦克风/WebRTC 对照 VAD-Only，验证 False End、提交延迟、
   双 True 比例、fallback 比例和音频连续性。

## 可选 Turn Judge 的未来边界

V1 不实现 Turn Judge。若真实使用证明双 True 的误提前结束率仍然偏高，可以增加独立的
Turn Judge 服务接口、提示词、超时、队列和指标。它不得进入现有工具 Router 的分类流程，
也不得复用 Router 的业务候选与降级语义；两者只允许共享同一个 vLLM 实例和底层模型资源。

任何阶段都必须保留一个开关完整回到原有 500ms VAD-Only；不能按 Bot ID、名称或角色
硬编码能力范围。
