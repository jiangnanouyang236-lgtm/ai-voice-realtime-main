# Harness 证据等级与验收门禁

本文用于选择 Harness 结构化验收时的报告规范；日常开发无需生成该报告或使用固定标签。
`.ai/harness.json` 定义这些工具的运行前提和证据等级。目标不是替代测试，
而是阻止静态检查、mock、localhost 集成或未经授权的外部操作被描述成生产验证。

## 证据等级

| 等级 | 最低证据 | 不代表 |
|---|---|---|
| `IMPLEMENTED` | 静态检查或单元测试至少一项通过 | 服务已启动、配置已生效、业务链路成功 |
| `LOCAL_VERIFIED` | 本机真实进程或协议集成及哈希证据 | LAN、生产服务、真实 Broker 或硬件成功 |
| `HYBRID_VERIFIED` | 当前代码在本地真实进程运行，并连接授权的 LAN/外部测试依赖 | 部署环境 E2E、生产服务归属或硬件动作成功 |
| `LIVE_VERIFIED` | 授权的非本地真实服务、Runtime Snapshot、哈希证据 | 物理设备完成动作 |
| `HARDWARE_VERIFIED` | Live 链路与授权物理设备共同通过 | 未列入报告的其他环境或设备 |

禁止使用泛化的“已验证”隐藏证据层级。报告必须保留未运行项与 `UNKNOWN`，不能用
`INFERENCE` 填补缺失组件。

## 使用方式

从 `.ai/evidence-report.example.json` 复制到 `reports/` 或 `tmp/`，填写当前 commit、
目标、能力、授权、运行态断言和逐项证据。`reports/` 与 `tmp/` 不提交 Git；需要长期
保存时，将脱敏报告和原始证据移到仓库外持久介质并记录校验和。

`endpoint_scope` 表示整份结论的最高环境范围；`observed_endpoints` 必须逐项声明本轮
实际接触的组件和范围，只记录组件名与 `localhost|lan|external|hardware` 等范围，
不得写入凭据。`LOCAL_VERIFIED` 的所有端点只能是 localhost；只要数据库、模型、
Broker 或其他依赖来自 LAN/外部，就不能申报 Local。当前 checkout 的本地服务连接
授权测试数据库、内网模型或外部 Broker 时，应使用 `HYBRID_VERIFIED`；该等级不得
被描述成部署环境或生产 E2E。

Robot MCP 的 Local/Hybrid/Live/Hardware 报告不能手写或省略端点清单。先运行脱敏采集器，
再把输出路径及 SHA-256 写入报告的 `environment_artifact`；校验器会核对 commit、
能力、等级、Harness 要求的端点集合及 `observed_endpoints`。采集物不包含地址、端口
或凭据。

采集器默认拒绝 dirty worktree，确保采集逻辑和被测代码都存在于报告绑定的 commit。
`--allow-dirty` 只用于诊断，输出会带 `dirty: true`，不能通过证据校验。

仓库提供 `scripts/local_mqtt_fixture_broker.py` 作为 Local Robot MCP 验证的最小
MQTT 3.1.1 协议夹具。它只允许用于 localhost、只实现证据链需要的 CONNECT、
PUBLISH、PING 和 DISCONNECT；不得把它描述成生产 Broker 或 Live 证据。

```bash
python scripts/capture_evidence_environment.py \
  --profile local \
  --capability robot_mcp \
  --claimed-level LOCAL_VERIFIED \
  --output reports/robot-local-environment.json
```

示例中的 `replace-with-git-commit` 必须替换为实际 Git SHA；正式报告不接受 `HEAD`、
分支名或不存在于当前仓库的提交。

```bash
python scripts/validate_evidence_report.py reports/evidence.json

# 或
make evidence-check EVIDENCE_REPORT=reports/evidence.json
```

返回码：

- `0`：报告结构和等级要求满足；不重新执行报告中的业务操作。
- `1`：报告存在缺失证据、越级结论、授权不足或哈希不匹配。
- `2`：报告或 Harness 配置无法读取。

## Live 与 Hardware 证据要求

`LOCAL_VERIFIED`、`HYBRID_VERIFIED`、`LIVE_VERIFIED` 和 `HARDWARE_VERIFIED`
的关键 PASS 项必须引用
真实证据文件并提供 SHA-256。校验器会读取文件并复算哈希。仅填写路径或复制摘要
不能通过。

需要跨组件 trace 或多 Bot 证明时，每个必需组件的 PASS evidence 还要分别填写
`trace_ids` 和 `bot_bindings`。报告级断言必须出现在所有必需组件证据中；只在顶层
写 `same_trace: true`、但组件证据无法形成交集，会被拒绝。

外部网络探测、MQTT 发布和物理动作的授权相互独立：

- `authorization.connectivity`：允许实际连接外部/LAN 服务。
- `authorization.external_write`：允许 MQTT publish、远端写入或其他副作用。
- `authorization.hardware_action`：允许真实设备动作、播放或采集。

授权只能记录已有事实；报告和校验器本身不能授予权限。

## 能力专项要求

### Robot MCP

`LOCAL_VERIFIED` 至少覆盖 Runtime Snapshot、LLM、MCP 和 localhost MQTT Broker，
并需要两个不同 Bot 绑定、至少两个不同逐轮 `trace_id` 和真正的 trace 串联。

`HYBRID_VERIFIED` 用于本地 LLM/MCP 进程连接测试数据库和非 localhost MQTT Broker
的开发闭环。至少覆盖 Runtime Snapshot、LLM、MCP、Broker 独立消费确认，保留两个
不同逐轮 `trace_id`；单 Bot smoke 可以通过，但不能替代 Live 的多 Bot 共享路由门禁。

`LIVE_VERIFIED` 至少覆盖 Gateway、Router、LLM、MCP 和非 localhost MQTT Broker，
同样需要两个不同 Bot 绑定、至少两个不同逐轮 `trace_id`、trace 串联，并具备外部写入授权。

`HARDWARE_VERIFIED` 还必须包含真实 Robot 证据和硬件动作授权。MQTT publish 成功是
业务步骤成功；除非协议另有 ACK，不把物理完成推断为已证明。

日常开发可用统一 Runner 启动当前 checkout 的隔离 LLM/Robot MCP，先订阅固定
`test_01` Topic，再发送 greet/cheer，并在结束时回收本轮进程：

```bash
python scripts/run_dev_harness.py \
  --scenario robot-actions \
  --robot-id test_01 \
  --authorize-connectivity \
  --authorize-external-write \
  --authorization-reference '<已有授权记录>'
```

Runner 只允许 `test_01`，端口被占用时拒绝复用或终止未知进程。默认允许 dirty
worktree 做诊断，只生成 `HYBRID_DIAGNOSTIC` observation；增加 `--certify` 后要求
clean worktree，并生成通过 Harness 校验的 `HYBRID_VERIFIED` 报告。

完整的无硬件 M1 语音闭环使用仓库固定录音，强制 Robot Secret 与原生
`webrtc_only + rtp_only`，覆盖 Rust Client、Go Gateway、Python Gateway、
STT、Router、LLM 和 TTS：

```bash
python scripts/run_dev_harness.py \
  --scenario voice-m1-e2e \
  --robot-id test_01 \
  --authorize-connectivity \
  --authorization-reference '<已有授权记录>' \
  --certify
```

该场景是 `HYBRID_VERIFIED`：本地运行当前 checkout 并连接授权的测试数据库和
LAN 模型服务。固定录音、RTP 收发成功不能升级为部署环境、麦克风、扬声器或
实体硬件验证。

固定 Gold 图片的程序化 Vision 闭环使用 Rust Client 的 `vision-v1` DataChannel，
经 Go Gateway 缓存后由 LLM 按同一会话拉取，并验证回答中的客观可见事实与 TTS RTP：

```bash
python scripts/run_dev_harness.py \
  --scenario voice-vision \
  --robot-id test_01 \
  --authorize-connectivity \
  --authorization-reference '<已有授权记录>' \
  --certify
```

该场景是 `vision/HYBRID_VERIFIED`，不证明实体摄像头、连续视频帧、部署环境或
硬件稳定性；Gold 图片的路径和 SHA256 会写入 observation。

无硬件的 M1 `client_event` 可使用同一 Runner 启动隔离 TTS 和 Python Gateway：

```bash
python scripts/run_dev_harness.py \
  --scenario voice-client-event \
  --robot-id test_01 \
  --authorize-connectivity \
  --authorization-reference '<已有授权记录>' \
  --certify
```

该场景按协议绕过 ASR、LLM、Router 和会话历史，只验证 Runtime Snapshot、
Python Gateway、真实 TTS、VAF1 音频帧和同一 trace，不得描述成 LLM 或 Voice E2E。
开启 Robot Secret 鉴权时仅从当前私有环境或未跟踪的
`rust_client/.env.local` 注入凭据；Runner 不关闭鉴权，也不把凭据写入观察或证据报告。

Router、主 LLM 和 TTS 的无副作用文本闭环使用：

```bash
python scripts/run_dev_harness.py \
  --scenario voice-direct-text \
  --robot-id test_01 \
  --authorize-connectivity \
  --authorization-reference '<已有授权记录>' \
  --certify
```

该场景固定使用无工具意图文本，要求真实 Router 分类器判定 `chat`、主 LLM
生成非空响应、工具调用数为零，并校验 Gateway 同一 trace 的 VAF1 音频。
它按协议绕过 ASR，因此属于 `voice_direct_text/HYBRID_VERIFIED`，不是 Voice E2E。

Turn Gate / 自然打断候选文本的 Agent 会话连续性诊断使用：

```bash
python scripts/run_dev_harness.py \
  --scenario voice-agent-continuity \
  --robot-id test_01 \
  --authorize-connectivity \
  --authorization-reference '<已有授权记录>'
```

该场景在同一 M1 session 连续发送两次 typed `input_text.commit`，要求认知 Agent
先进入、后继续接管，两轮均产生真实 LLM/TTS 响应，且 Python Gateway 不出现 M0
`/ws` 会话。它以候选文本模拟提升，绕过现场 ASR/VAD 和 Go/Rust，只生成
`HYBRID_DIAGNOSTIC`，不得称为 Voice E2E 或实体机验证。

时间工具路线使用 `--scenario voice-utils-time`。Runner 会启动本地
`utils_remote`，先对全部启用 Bot 运行完整工具候选矩阵，再用 `test_01`
经过 Gateway 和 TTS 复核同一 trace 的音频输出。验收要求固定选择
`utils_remote__get_now_context`、每轮恰好调用一次、日期/星期与独立本地事实一致；
不得只凭 Router 分类或 MCP 启动日志判定通过。全 Bot 矩阵只绑定
Runtime/Router/LLM/Utils 证据；Gateway/TTS 闭环只绑定实际执行的 `test_01`，
不得把矩阵扩大表述为每个 Bot 都完成了音频闭环。

Robot 的 Gateway 文本闭环使用 `--scenario voice-robot-actions`，固定向
`test_01` 发送 greet/cheer 两轮请求。每轮必须由 Router 收窄到
`robot_remote__move_robot`、只调用一次工具、由独立 MQTT 订阅者收到匹配 trace
和动作类型，并产生同 trace 的 TTS 音频。该场景需要既有 connectivity 与
external-write 授权；它不经过 ASR、Go Gateway、Rust Client，也不证明实体动作完成。

Python 音频输入闭环使用 `--scenario voice-audio`。Runner 只读转换并哈希校验
仓库已有 `jialan-6s.mp3`，通过 M1 active-audio 协议发送 Opus，要求同 trace 出现
真实 STT 转写、Router/主 LLM 回复和 TTS 音频。该固定录音只能证明协议与服务集成，
不能替代麦克风、真人现场、噪声、远场或播放硬件验收。

### Voice E2E

Live 至少覆盖 Rust Client、Go Gateway、Python Gateway、STT、LLM 和 TTS；Hardware
还需真实音频输入、输出。端口可连或单服务成功不能替代完整链路。

### Vision

必须使用真实或固定测试图片字节，经程序取图路径进入当前 LLM 请求；文字声称附图、
mock 图片描述或旧轮次图片不能满足 Live。

### TURN relay

必须证明 relay-only，而不是在收集 relay candidate 后实际走 host/private route。

## 维护规则

新增能力等级时，同时修改：

1. `.ai/harness.json` 的 `evidence_policy`；
2. `scripts/validate_evidence_report.py`；
3. `test/test_evidence_report.py`；
4. 本文档和 PR 模板。

修改 gRPC、MQTT、Internal Voice、DataChannel、音频格式、端口或 Runtime Snapshot 时，
仍需先登记 `docs/decisions.md` 并检查所有消费者。证据门禁不能替代协议评审。
