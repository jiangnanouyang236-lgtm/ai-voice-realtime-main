# V4 Python 服务部署边界提案

状态：核心边界已确认，实施尚未开始；本文不授权直接变更线上容器。

## 结论

V4 应把 STT、LLM、TTS、Python Gateway 和 MCP 从旧的单容器迁移为独立部署单元，
但继续保留在同一仓库、同一 Compose 项目、同一个根 `.env` 和同一 Python 运行时体系中。
第一阶段不拆仓库，也不为每个小模块复制一套 Dockerfile 和依赖文件。

必须区分三种边界：

1. **进程边界**：每个服务独立进程、健康检查、日志和重启策略。
2. **部署边界**：可以只重启或回滚一个服务，不连带重启其余服务。
3. **镜像边界**：决定代码或依赖变化时要构建多少镜像；不必与进程边界一一对应。

仓库现有 `deploy/compose/business.yml` 和 `deploy/compose/mcp.yml` 已具备多个服务容器，
但全部从 `docker/python-services.Dockerfile` 构建同一个全量代码镜像，并依赖根 `.env`
完成插值。线上旧单容器尚未迁移，因此外部测试、自动化发布和局部回滚仍被绑在一起。

## 推荐边界

| 部署单元 | 职责 | 是否独立容器 | 第一阶段镜像 |
| --- | --- | --- | --- |
| `python-gateway` | session、鉴权、ASR→LLM→TTS 编排、trace | 是 | 共享 Python 应用镜像 |
| `stt` | ASR provider adapter 与 gRPC | 是 | 共享 Python 应用镜像 |
| `llm` | Router、对话、Agent 与 MCP 调用编排 | 是 | 共享 Python 应用镜像 |
| `tts` | TTS Profile、provider adapter 与 gRPC | 是 | 共享 Python 应用镜像 |
| `admin` | Runtime Snapshot 配置管理 | 是 | 共享 Python 应用镜像 |
| `mcp-robot` | 有副作用的 Robot/MQTT 工具 | 是 | 共享 Python 应用镜像 |
| `mcp-utils` | 无硬件副作用的辅助工具 | 是 | 共享 Python 应用镜像 |
| vLLM 模型服务 | GPU 模型推理 | 已独立 | 模型专用镜像，不随业务代码重建 |
| Go Gateway | WebRTC 实时边缘层 | 已独立 | Go 专用镜像 |

Robot MCP 必须与只读工具隔离，因为它拥有不同的凭据、外部写入权限和硬件风险。
LLM 与 Router/Agent 编排暂不拆成更多网络服务，避免增加一次调用跳转和版本组合。

## 服务命名约定

线上服务统一使用 `wzk-<能力>-<协议或职责>`，禁止继续出现无法从名称判断用途的临时容器名。

| Compose service | 职责 |
| --- | --- |
| `wzk-stt-grpc` | STT gRPC adapter |
| `wzk-tts-grpc` | TTS gRPC adapter |
| `wzk-llm-grpc` | Router、LLM、Agent 和 MCP 调用编排 |
| `wzk-python-gateway` | Python AI orchestration gateway |
| `wzk-admin-api` | Runtime Snapshot 管理 API |
| `wzk-mcp-robot` | Robot/MQTT 有副作用工具 |
| `wzk-mcp-utils` | 通用只读工具 |

容器网络内也使用上述 service name，例如 `grpc://wzk-stt-grpc:50054`。默认不设置
`container_name`，避免阻断扩容、滚动替换和并行测试；统一通过 Compose project、service name
和 labels 检索。若现有运维系统强制要求无后缀的固定容器名，再单独登记例外。

## 镜像和重建策略

第一阶段使用一个按 Git commit 标记的不可变 Python 应用镜像，例如
`ai-voice-python:<git-sha>`，各服务用不同 `command` 启动。一次构建后，线上只更新并重启
实际受影响的服务；不要用全栈 `down/up` 作为日常发布方式。

- 修改普通 Python 文件：依赖层应命中缓存，只重建代码层；只重启受影响服务。
- 修改 `requirements.txt`、protobuf 或共享运行时：重建共享镜像，并执行受影响面回归。
- 修改 vLLM 模型或模型镜像：走模型层发布，不重建 Python 业务镜像。
- 本地开发和服务器联调可使用只读 bind mount 加快迭代；正式生产禁止挂载源码。
- 生产镜像必须绑定 commit，禁止复用含义漂移的 `latest` 作为回滚依据。

只有在构建时长、漏洞面或依赖冲突已有数据证明共享镜像成为瓶颈后，第二阶段才拆出
`python-runtime-base` 和 STT/LLM/TTS/Gateway 薄镜像。不能仅为目录看起来整齐而复制依赖。

## 环境变量边界

V4 第一阶段继续保留一个根 `.env`，方便人工维护和部署。Compose 读取该文件，但每个服务
只通过显式 `environment` 白名单获得自身需要的变量；不把完整 `.env` 文件挂载进容器，
也不使用一个公共 `env_file` 把全部密钥注入所有服务。

业务配置按以下层次解析：

1. MySQL Runtime Snapshot：Bot、Robot、MCP、Agent、TTS Profile 等运行态绑定。
2. 服务器未跟踪的根 `.env`：数据库、模型、MQTT、TURN、Robot 等部署变量和凭据。
3. 服务级非敏感环境变量：端口、超时、日志和容器内服务发现。
4. 仓库 example：只描述变量契约和安全默认值，不代表运行事实。

仓库继续用根 `.env.example` 描述完整变量契约。容器 DNS 地址等部署内部连线在 Compose
中显式设置，不要求用户重复填写到 `.env`。不得复制 Runtime Snapshot 业务绑定，也不得
让一个服务读取其不需要的 MQTT、Robot 或模型密钥。后续只有在密钥系统或权限隔离确有
需要时，才把单个根 `.env` 升级为服务级密钥文件。

## 发布与回滚

每次部署记录以下最小清单：

- 服务名、镜像 digest、Git commit 和配置契约版本；
- 数据库 migration 版本与 Runtime Snapshot 版本；
- 变更前镜像 digest；
- readiness 结果和定向 smoke 证据；
- 回滚命令只指向明确服务和明确旧 digest。

数据库 migration 使用独立的一次性 job，先做兼容性检查，再滚动服务。应用发布不能隐式
执行不可逆 migration。回滚不得依赖仍在服务器目录中的旧源码。

## 健康与自动化测试

当前 TCP 端口探测只能证明 listener 存在。V4 需要为各服务补充分层状态：

- liveness：进程事件循环可响应；
- readiness：依赖、Runtime Snapshot 和 provider 配置可用；
- semantic smoke：执行最小真实 gRPC/MCP 请求，但不默认触发硬件副作用。

外部自动化先验证 `stt`、`llm`、`tts` 和 `python-gateway` 的独立 readiness，再验证完整
链路。Robot MCP 使用固定 `test_01` 且必须保留外部写入授权；不得把端口可连升级为 E2E。

## V4 实施顺序

| 阶段 | 内容 | 验收条件 |
| --- | --- | --- |
| 1 | 盘点线上旧容器、入口、配置和持久数据 | 有脱敏清单与明确回滚点，不改线上 |
| 2 | 建立根 `.env.example` 的服务所有权表和注入校验器 | 缺失/越权变量能在部署前失败 |
| 3 | 给共享镜像增加固定 tag/digest，按服务启动 | 单服务可独立启停，行为与 V3 等价 |
| 4 | 增加按变更路径构建、定向重启和 release manifest | 小改动不再触发全栈重启 |
| 5 | 增加 gRPC readiness、semantic smoke 和回滚演练 | 自动化能识别 listener 假健康 |
| 6 | 在测试环境迁移，再灰度线上 | 证据绑定 commit、镜像和 Runtime Snapshot |

## 设计评审需确认

1. 线上是否允许保留一段时间的旧容器作为只停不删的快速回滚入口。
2. Admin 是否与业务服务同机部署，还是仅在受信管理网络提供。

在以上边界确认前，只推进盘点、契约和本地 Compose 设计，不修改线上运行方式。

V4 Compose 的实际操作入口见 `docs/docker-compose-v4.md`。
