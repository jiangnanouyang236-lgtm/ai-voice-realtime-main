# 当前架构决策

> 只保留仍约束当前实现的决策；淘汰方案由 Git 历史追溯。

## 1. WebRTC-first 与双 Gateway 边界

- Rust Client 是唯一支持的端侧客户端。
- Go Gateway 是 Realtime Edge：WebRTC、ICE/STUN/TURN、RTP/DataChannel、弱网/session/downlink gate。
- Python Gateway 是 AI Orchestrator：Robot 鉴权、ASR -> LLM -> MCP -> TTS、round/playback、trace、Bot 行为。
- ASR/LLM/TTS 保持一条云端管线，客户端或 transport 差异不能演变成两套 AI 后端。
- v3 网络兜底是 ICE/TURN；Go 不承载自动 WS 音频 fallback。
- v2 Rust 直连 Python Gateway 作为明确回滚路径，不能与 v3 主路径隐式混用。

## 2. Go-Python 协议分阶段迁移

当前生产默认是 M1：

- M1：同一条 `/internal/voice/ws` 会话承载真实用户音频、client event、interrupt、audio cancel、playback report、状态、TTS 音频和结构化终态。
- M0：仅作为显式回滚模式保留，不与已提交的 M1 轮次自动混跑或重放。
- `GO_VOICE_GATEWAY_INTERNAL_VOICE_MODE=m1|shadow|m0` 是统一的生产路由开关，默认值为 `m1`。

迁移要求：

- 先统一 `session_id`、`trace_id`、`utterance_id`、`round_id`、`playback_id` 和取消语义。
- 新协议能力继续保留同输入对照和显式回滚。
- 已被 M1 接受的请求发生失败时必须明确终止，不能自动重放到 M0。
- 不因协议迁移把 LLM/MCP/TTS 业务逻辑搬进 Go。

## 3. Qwen3-ASR 是唯一 STT 实现

- `STT_PROVIDER` 当前只接受 `qwen`，未知或旧值直接失败，不静默回退。
- `funasr`、SenseVoice、Whisper 及其重型依赖不属于当前运行主线。
- Gateway 生产路径调用 unary `RecognizeSpeech`。
- Python 音频开发闭环固定使用哈希绑定的仓库参考录音，经只读转码后走 M1
  active-audio；它用于协议和服务回归，不得升级为现场麦克风或声学质量证据。
- proto 中的 `StreamRecognize` 仍是兼容/基准接口，当前实现会收齐 chunk 后一次推理，不应宣称为真正流式 ASR。

## 4. TTS Profile 与增量文本提交

- Bot 只绑定 `tts_profile_id`；provider endpoint、key、model、task type 和参考音频由 TTS 服务解析。
- CustomVoice 使用 `QWEN3_TTS_CUSTOM_VOICE_*`，Base 使用 `QWEN3_TTS_BASE_*`。
- 旧 `LOCAL_QWEN3_TTS_API_KEY` / `LOCAL_QWEN3_TTS_WS_URL` 不再是生产配置。
- LLM 增量文本直接发送到本地 Qwen3-TTS WebSocket。
- 不恢复句子、子句或段落聚合；除非有新的同环境延迟/稳定性证据并由用户明确改变方向。

## 5. Bot/Robot/MCP/Agent/TTS Profile 以数据库为准

- MySQL `server-config` 和 Runtime Snapshot 是业务配置真相源。
- `server_config/bootstrap/legacy_*.yaml` 只用于初始化。
- 保存配置后必须 Apply / Reload，并验证 Gateway、LLM、TTS 使用同一 Snapshot 版本。
- schema 变更必须提供可重复执行的 migration；当前旧库必须包含 `bots.max_response_chars`。
- Admin 保存成功不等于 runtime 已使用，发布验证必须检查真实 status/请求日志。

## 6. 安全默认

- 仓库不保存 TURN、Robot、MQTT、FRP、数据库或模型 API 凭据。
- Go Gateway 源码默认只包含公共 STUN；TURN 从未跟踪 `.env` 或密钥系统注入。
- `--print-config` 和日志必须脱敏。
- `/internal/status` 只在 localhost/受信网络/管理代理后访问。
- 发现已提交凭据时，删除明文只是第一步，还必须在外部系统轮换。

## 7. Router 与工具首响

- 确定性规则优先处理明确工具意图。
- 所有具有运行态 `bot_id` 的启用 Bot（包括 `default`）使用同一套路由策略；能力范围只由 Runtime Snapshot 的 MCP/Agent 绑定决定，不按 Bot ID、名称或角色硬编码分流。
- Router 提示词和确定性规则只能声明当前工具 schema 实际支持的能力；家电、灯光、窗帘等未绑定能力必须明确拒绝，不能替换成相近 Robot 动作。
- 每条确定性工具路线必须写入真实 `category`；Utils 单工具结果可直接返回，复杂路线中的 Utils 结果仍须交回编排模型继续执行。
- 条件结果决定后续动作、或同一句需要两个以上工具调用时，统一进入 `complex`；即使多个步骤都属于 Robot 工具，也不得按首个动作降级为单工具路线。
- 模糊请求可由独立 Router profile 分类，但超时/失败必须回到受控路径。
- 工具执行前的快速回复不得复述地点、时间、提醒内容等尚未由实际工具参数确认的动态信息；
  天气类只播报“正在查询天气”等安全过程语，避免快速回复与最终工具参数不一致。
- 高频快速回复统一由 `voice_quick_replies.py` 管理；唤醒、待机、退出和工具过程语使用
  至少 16 条人工筛选短句，并设置可见字符上限，禁止用机械拼接凑数量；按 session 使用
  shuffle-bag，同一轮语料用完前不得重复。Robot 动作语料仍可保留大词池。
- Workflow 预分类若只返回非 `complex` 工具类别，主轮必须在同类别内重新应用确定性规则；
  能命中唯一工具时收窄首轮候选并记录完整工具名，不能因复用类别结果丢失单工具选择。
- 是否慢不能只看 transport；应对同一 Bot 做 direct Python WS 与 Go WebRTC 对照，并比较 `llm_router_kind`、`llm_first_token_ms`、`tts_first_audio_ms`、`ws_send_ms` 和 Go bridge timing。
- 机器人动作工具调用失败时，不应把工具 JSON 或内部状态直接送入 TTS。
- Robot 语音侧开发闭环固定使用 `test_01` 的 greet/cheer 两轮 direct-text 场景：
  先订阅精确 MQTT Topic，再由本地 Gateway/LLM/MCP 发起动作并校验同 trace TTS；
  该启动方式不得扩展到随机 Robot ID，也不得表述为 ASR、部署环境或实体硬件验证。

## 8. 可观测与播放隔离

- `trace_id` 贯穿 Rust、Go、Python、ASR、LLM、MCP、TTS。
- `round_id` / `playback_id` 用于丢弃迟到音频和完成事件。
- interrupt/cancel 必须跨层传播，且不能让上一轮 `done` 结束当前轮。
- 日志可记录计数、耗时、route 和 redacted status，不打印原始音频或密钥。
- Gateway 结构化 trace 必须保留 `llm_selected_tool_name`、`llm_tool_choice_mode`
  和 `llm_tool_call_count`，用于区分“Router 命中”“模型选择工具”和“MCP 实际执行”；
  仅有日志或候选工具数量不能作为工具闭环证据。

## 9. 复杂任务编排边界

- `X=complex` 只负责进入复杂路径；Router 小模型不拆分任务。
- 主 9B 模型低温生成受限结构化计划，程序统一校验、重排和顺序执行。
- 图片由程序按当前 `session_id` 获取并注入本轮多模态请求，不作为 MCP 工具暴露给模型。
- 终止型任务最多一个并统一移动到最后；成功后发送 `[EXIT]`，失败或结果未知时不退出、不自动重试。
- 终止型任务开始前必须由 Python Gateway 等待匹配当前轮次的客户端 `playback_complete`；打断、断连或等待超时均不得继续启动终止任务。
- 机器人 MQTT 工具发布成功即按业务定义判定步骤成功并继续，不等待设备物理完成 ACK；发布结果未知时不自动重试。
- 现有 interrupt 必须继续传播为工作流取消；已提交的副作用不回滚，但打断后不得开始后续步骤。
- 复杂编排不得修改现有 ASR、LLM、TTS gRPC 协议和增量流式语义；如需新增 Workflow 内部接口，必须独立设计、单独讨论并可通过功能开关整体关闭。
- 简单请求保持当前快速路径。完整协议和验收矩阵见 [`complex-task-orchestration-v1.md`](./complex-task-orchestration-v1.md)。

## 10. LLM 逐轮 Trace 与 Robot 审计元数据

- `session_id` 只表示跨轮会话；`trace_id` 表示单轮请求，两者不得互相代替。
- `llm.ChatRequest` 追加可选字段 `trace_id = 6`。旧调用端不传时保持兼容，但不能把空值或 `session_id` 伪装成逐轮 trace。
- Python Gateway 必须把当前轮的 `trace_id` 传入 LLM；同一会话的不同轮次必须携带不同 `trace_id`。
- `_meta` 是程序注入的隐藏上下文，不属于 LLM 可见的工具 schema。Robot MCP 可接收审计字段；Singing MCP 仅接收 `tts_profile_id`、`session_id`、`trace_id`，不得接收 RobotID；其他工具不得出现 `_meta`。
- Robot MQTT payload 的 `meta` 可携带 `session_id` 和 `trace_id`，不改变既有业务字段语义。真实消费者兼容性仍须通过受控 live/hardware 验证，单测或本地 broker 不能替代。
- 验收至少覆盖同会话多轮、两个启用 Bot、非 Robot 工具无元数据泄漏；Gateway、Router/LLM、MCP、真实 broker 和硬件闭环按证据等级分别声明。

## 11. 独立 Gold 与固定 Robot 动作契约

- `data/eval_gold/` 是唯一版本化验收 Gold；旧自动生成、扩展、corrector 和模型输出回写数据不再作为基线。
- `acceptance-2026-08-09-v6`（Router 94、Tool 21、Vision 1）定为当前唯一验收基准；后续增删改必须人工复核并递增 revision，运行报告不得覆盖基准数据。
- Tool acceptance 的候选 schema 直接来自当前仓库 Robot/Utils MCP 定义，必须使用完整候选集并精确匹配完整参数。
- `dance` 是无舞种、无时长参数的固定动作；“爵士舞”“民族舞”等不存在的能力不能进入 schema 或 Gold。
- 打招呼、握手和欢呼是 `move_robot.action=greet|handshake|cheer`，不是独立工具。
- Vision Gold 必须绑定仓库内真实图片及 SHA-256，只标注可直接观察的事实；fixture 获批与 live Vision 通过分别记录。
- owner 可明确委托严格审核者逐条复核并登记 `owner_approved`，但不得把被测模型的预测当审核依据。

## 12. M1 语音烟测使用固定虚拟 Robot 与强鉴权

- `scripts/smoke_go_webrtc_python_gateway.py` 默认只使用虚拟 Robot `test_01`，不再默认指向实体机 `companion_01`。
- 启用 `--require-robot-secret` 时，Runner 只从进程环境、仓库根私有 `.env` 或 `rust_client/.env.local` 读取凭据；私有文件只允许导入 `ROBOT_ID`、`ROBOT_SECRET`，不得扩散其他配置。
- 私有凭据声明的 `ROBOT_ID` 与命令目标不一致时直接失败，禁止把一个 Robot 的 Secret 尝试用于另一个 Robot。
- 固定录音通过原生 WebRTC RTP 验证软件链路，只能作为无硬件 Voice E2E 证据，不能表述为麦克风、扬声器或实体 Robot 验证。
- 当前 checkout 在本机启动 Rust/Go/Python 并连接 LAN 模型服务时，证据等级固定为 `voice_e2e/HYBRID_VERIFIED`；只有非本地已部署服务链路才能申报 `LIVE_VERIFIED`。
- Vision 开发闭环复用 owner-approved Gold 图片，通过 Rust `vision-v1` DataChannel、Go 快照缓存和 Python/LLM 按 session 取图；本地当前代码连接 LAN 服务时只能申报 `vision/HYBRID_VERIFIED`。

## 13. Turn Gate 采用 provisional candidate，不复用 audio_end

- `300ms` 只表示 provisional candidate，不能直接触发现有 `audio_end`。
- Rust 保持录音和 RTP 上送；Go 对当前 utterance 构造只读 snapshot；Python 在正式 commit
  前不得进入主 LLM、MCP、历史或 TTS。
- V1 只有 Smart 与 EOU 都为 true 时才具备 early-commit 资格；任意一方明确 false 时继续
  收音至 `1600ms` 硬上限，模型失败、超时或反馈丢失时由 `1000ms` failure fallback
  收口。Turn Gate 关闭时仍使用现有 500ms VAD 路径。当前不引入 4B Judge。
- 若真实数据证明双 True 的误提前结束率仍然偏高，Turn Judge 必须作为独立职责实现，不能
  混入现有工具 Router 的分类、候选或降级流程；两者只允许共享同一个 vLLM 实例和模型资源。
- candidate 必须包含 `utterance_id`、严格递增的 `candidate_seq`、`speech_epoch` 和
  `audio_watermark`；用户恢复说话后，旧结果必须取消或按 stale 丢弃。
- Active 提交使用两阶段协议：Go 只负责云端语义决策并发送
  `turn_commit_request`；Rust 必须再校验当前 VAD epoch、watermark 和持续静音，
  只有 `turn_commit_ack.accepted=true` 才真正关闭录音。
- 双 True 只推理一次。Rust 在不足 `TURN_GATE_MIN_COMMIT_SILENCE_MS`（默认 600ms）时暂存
  `turn_commit_request`，用户恢复说话立即取消；稳定静音达标后直接 ACK，不做文本匹配、
  二次 ASR、第二次双 True 或 4B Judge。候选仍从 300ms 开始并行计算。
- accepted Active 轮次复用 candidate ASR 文本进入原 Python LLM/TTS 主链，
  不重复调用正式 ASR；Turn Gate 不与 Router 合并。
- Active candidate 的 Go-Python 连接按 session 持久化，并从 `audio_start` 开始预热；
  Shadow 仍使用 per-turn 隔离连接。会话关闭时必须同时释放主 bridge 和 candidate bridge。
- Shadow 并发测试已确认 candidate queue 当前为全局单消费者；生产放量 Active 前必须改为有界并发，
  不同 session 不得互相排队，同一 utterance 仍按 candidate 序号保持有序，并补齐 queue wait
  指标。candidate timeout 必须从入队时计算绝对 deadline，过期项出队前直接丢弃；不能只通过
  增大 queue size 或依赖服务器 CPU 掩盖队头阻塞。
- 原有正式 ASR handoff queue 同样存在跨 session 串行等待整轮 LLM/TTS `done` 的问题；该问题
  与 Turn Gate 职责分开整改，不能在引入 Turn Gate 时顺手改变单会话内的流式 TTS 语义。
- Rust、Go、Python 分别使用 `TURN_GATE_ACTIVE_ENABLED`、
  `GO_VOICE_GATEWAY_TURN_GATE_ACTIVE_ENABLED`、`GATEWAY_TURN_GATE_ACTIVE_ENABLED`，且不互相
  继承。Rust Client 按线上策略默认开启 Active，Go/Python 仍保持各自默认关闭；完整 Active
  链路要求三端开关均有效。Rust 可通过显式设置 `TURN_GATE_ACTIVE_ENABLED=false` 回退。
- Active 模式中，Go 仅在 Smart Turn / EOU 正常返回且策略为 `continue` 时发送
  `turn_candidate_decision`。Rust 再校验 utterance、candidate、speech epoch 与 watermark：
  匹配时把静音上限延至 `1600ms`；模型错误、超时、反馈丢失或结果过期均不发送该反馈，
  由 `1000ms` failure fallback 收口。双 True 仍使用既有两阶段 commit；功能关闭时继续使用
  `VAD_SILENCE_THRESHOLD`，不改变 VAD-Only。
- 详细状态机、时间语义和验证门禁见 `docs/turn-gate-v1.md`。

## 14. AI 唱歌使用独立 MCP 与预生成歌曲库

- 唱歌从 Robot MCP 完全拆分为 `singing_remote.play_song` / `singing_remote.list_songs`；不接收 RobotID，不发送 Robot MQTT，也不保留 `robot_remote.sing`。
- LLM 根据当前 Bot 的 `tts_profile_id` 隐式选择 `singing_voice_id`。当前音色没有对应资产时必须明确告知用户，禁止静默回退到 Serena 或跨音色播放。
- 歌曲匹配允许处理 ASR/LLM 轻微错字；高置信度直接命中，多候选时澄清，未知歌曲返回“这首歌我还没学会”，不得随机替代。只有泛化点歌请求才从当前音色随机选择。
- `list_songs` 只返回当前音色实际可播放的歌曲；目录与音色映射保存在 JSON 文件，MVP 不新增数据库表，也不实现续唱状态。
- Git 只保存歌曲目录、音色映射、别名、时长和 SHA-256；真实 WAV 作为部署资产从 `SINGING_AUDIO_ROOT` 只读加载。
- 歌曲成品统一为 16kHz、mono、PCM16 WAV。Python Gateway 验证格式与哈希后发送 PCM，Go Gateway 仍统一负责 Opus 封包和传输。
- 准备语 TTS 和歌曲共用当前 `round_id` / `playback_id`；歌曲按 100ms 分块并只预送约 300ms，保留原有打断与迟到音频隔离语义。
- 首批五首 Serena 版本为用户听审接受的实验成品；代码与离线测试只标记 `IMPLEMENTED`，真实 Go/WebRTC/设备播放仍需另行验证。
- 目录中的 `001`--`023` 暂时全部保留并允许播放，以扩大可点歌曲库；`011`--`023` 仍保持
  `IMPLEMENTED`，可播放不等于用户听审接受。后续听审或替换单首资产不改变稳定歌曲 ID。

## 15. 生产自然打断优先基于讯飞系统音频路径

- 当前生产机器人使用讯飞语音硬件模组。AEC、NS、AGC 和 KWS 在硬件侧完成；Rust 不接入
  讯飞 SDK，只使用系统默认麦克风/扬声器并监听串口 JSON 唤醒事件。
- 播放 TTS 时，讯飞默认麦克风仍持续提供硬件处理后的输入。自然打断第一阶段基于该输入做
  本地 VAD、候选缓存与 ASR Probe，不把 TCP 软件 AEC 或 `wzk-mic` / `wzk-speaker` 作为
  生产前置条件。
- WebRTC 已有独立上行/下行 RTP，无需新增 PeerConnection 或第二条音频通道。Probe 复用
  现有媒体连接，但必须使用独立候选语义，不得在确认打断前取消下行或进入正式业务编排。
- accepted `new_intent` 必须先等待 internal voice 旧轮次取消完成，再提升候选文本；Go 的
  `interrupt` 转发 ACK 是该顺序屏障。取消下行 RTP 时必须同时向 Rust 返回原
  `round_id` / `playback_id` 的 `playback_cancel`，不能只更新服务端 generation。
- 普通 TTS 和唱歌播放统一支持自然打断，复用同一套 round/playback、interrupt 和迟到音频
  隔离语义；短提示音、确认音不单独开启 Probe。唱歌的长播放、残余回声和换歌表达必须进入
  独立测试矩阵，不能只用普通短 TTS 用例代替。
- 讯飞串口 KWS 保持最高优先级和立即打断语义；ASR Probe 不能延迟或替代硬件唤醒路径。
- 自然打断由 Rust `NATURAL_BARGE_IN_ENABLED` 默认关闭并作为实体机启用入口；Go/Python
  ASR-only 服务端处理默认就绪，但各自仍可显式设为 `false` 回滚。旧 Rust 不发送 Probe，
  因此服务端默认就绪不会改变旧客户端行为。
- 第一阶段不叠加硬件 AEC 与软件 AEC。C++ `audio_frontend` TCP 和 ESP32-S3 USB 音频方案
  继续作为独立替代路线验证，不能用其本机状态声明生产硬件能力。
- 自动化和测试环境通过只能标记软件链路相应证据等级；真实回声、双讲和设备格式最终仍需
  测试实体机生成合规 `HARDWARE_VERIFIED` 证据。完整矩阵见
  [`audio-endpoint-matrix.md`](./audio-endpoint-matrix.md)，详细协议、状态机和验收项见
  [`natural-barge-in-v1.md`](./natural-barge-in-v1.md)。
- 软件多轮回归可使用不同于机器人当前音色的已启用 TTS-Base Profile 模拟完整用户表达；每段
  生成音频必须绑定文本、Profile、格式和 SHA-256，且不能把识别结果回写为 Gold。该矩阵不再
  重复评测已由 owner 确认有效的讯飞硬件 AEC，也不能据此声明实体机验证。

## 16. `main` 晋级到 V4 测试候选，保留 V3 回滚与 V4 测试分支

- 2026-08-26 起，V4 候选在 release gate 通过后允许快进到 `main`，作为当前产品主线继续测试。
- `develop/v4.0` 和承载候选的功能分支暂不删除，供测试、问题定位和后续修复使用。
- `release/v3.0`、`v3.0.0-rc1` 和 `v3.0.0` 均保持不可变，继续作为 V3 维护与回滚基线。
- 本次主线晋级不创建 `v4.0.0` 正式标签，不代表生产或实体机验收完成；自然打断等在测能力
  继续按各自开关和证据等级管理。

## 17. V4 Compose 补齐唱歌部署契约并彻底移除专用天气 MCP

- V4 Python Compose 纳入 `wzk-mcp-singing`；Python Gateway 使用
  `SINGING_AUDIO_V4_HOST_DIR` 将宿主机歌曲目录只读挂载到 `/app/singing/audio`。
- 真实歌曲 WAV 不进入 Git，也不得通过 Docker build context 进入 Python 镜像；部署必须以
  外部资产、只读挂载和 catalog SHA-256 完成绑定。
- 已删除的 `weather_sse_server.py` 不再保留无效 Compose、Makefile、校验器或日志入口。
  天气能力统一由 Runtime Snapshot 绑定的 Websearch MCP 提供。
- `docker-compose.v3.yml` 和 `compose-v3-*` 名称仅为现有 Go + Python 部署兼容入口，
  不再用于判断仓库当前产品版本。

## 18. 唤醒中断必须终止服务端长音频，LLM 就绪前完成 MCP 预热

- `wake_interrupt` 同时是客户端状态事件和主 Python Gateway 的显式 `interrupt`。Go 必须把
  后者转发到主连接，不能因为 M1 已消费 `client_event` 就让 M0 的 TTS/歌曲流继续占用会话。
- Python Gateway 把匹配当前 `round_id` 与 `playback_id` 的 `playback_interrupted` 作为防御性
  取消信号；缺少标识或迟到的旧播放回执只记录，不得误杀替代轮次。
- LLM 启动和 Runtime Snapshot Apply 必须等待 Bot 绑定 MCP 的并行 warm up 完成后再报告就绪/
  Apply 成功；请求路径仍保留连接兜底，但正常首轮不再承担 MCP 建连延迟。
- 单测和本机真实依赖探针只能证明 `IMPLEMENTED` / `HYBRID_DIAGNOSTIC`；上线后仍需用同一会话日志
  验证歌曲中断到服务端停止推流的耗时，以及首轮 `mcp_prepare_ms`。

## 19. Turn Gate 与自然打断候选文本必须在当前 M1 会话提交

- accepted candidate 新增 typed `input_text.commit`，复用当前 Rust/Go `rtc_*` 对应的
  `/internal/voice/ws` 会话和 Python `session_id`，直接进入既有 `handle_text`、LLM/MCP/TTS 主链。
- Turn Gate 与自然打断都复用候选 ASR 文本，不重复调用正式 ASR；候选序号、speech epoch、
  audio watermark、来源和 ASR 耗时随提交透传，`(session_id, utterance_id)` 作为幂等键。
- 自然打断仍须先等待同一 M1 会话的 `interrupt` acceptance，再提交 `input_text.commit`；
  旧轮次取消失败时不得启动新意图。
- M1 active 模式禁止为候选提升另开 M0 `/ws` 会话，也禁止复制 Agent 状态或自动回放到 M0；
  提交后失败按当前 M1 轮次结构化终止。M0 仅保留为显式 session 级回滚模式。

## 20. 唤醒播报必须产生可恢复的客户端终态

- `client_event` 仍只走直接 TTS，不进入 LLM/MCP；首次 TTS 正常结束但未产生任何音频时，允许在
  同一 round/playback 内重建 TTS channel 并重试一次，不创建第二个业务轮次。
- M1 `response.cancelled` 必须映射为带 trace/round/playback 的客户端 `playback_cancel`；
  `response.error` 保持原样转发，同时补发同标识的 `playback_cancel`，保证 Rust 不会停在
  `SendingAudio`。
- Rust 仅在 `wake_interrupt` 时启用旧响应抑制；`wake_idle` 的本轮错误必须可见。等待播放开始时
  收到匹配的 `playback_cancel`，应回到 `AwakeIdle`，不能继续等待响应超时。
- 已提交或已接受的 M1 唤醒事件仍不得自动重放到 M0；本决策只补齐同轮 TTS 重试与终态，不改变
  session 级 M1/M0 所有权。

## 21. Admin 默认通过内网访问（2026-09-11）

- 按交接方要求，Admin 裸进程默认监听 `0.0.0.0:8282`，`ADMIN_API_HOST` /
  `ADMIN_API_PORT` 仍可显式覆盖；登录鉴权不变，Vite 开发代理同步为 `8282`。
- 现场已有宿主 `15282` → 业务容器 `8282` 映射，部署并重启 Admin 后从内网
  `http://10.10.6.121:15282` 访问，无需 Admin FRPC。此次仓库修改不执行服务器部署。
- Go Gateway 同机开发时需使用其他端口（如 `15010`），Rust 连接相应端口。
  Compose 显式配置的 Admin `18100` 保留；语音链路的 FRP/STUN/TURN 不因此移除。
