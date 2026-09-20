# Voice Gateway / V3-V4 追踪

> 本文只维护当前状态和未完成事项。已经完成的逐步迁移记录由 Git 历史追溯，
> 不再把历史实施清单留作“待办”。

Last updated: 2026-08-26

## 当前阶段

V3 已在提交 `b8995981` 完成发布并冻结：`release/v3.0`、`v3.0.0-rc1` 和 `v3.0.0`
均保留该提交。2026-08-26 起，完成 release gate 的 V4 测试候选可进入 `main`；
`develop/v4.0` 和当前功能分支继续保留用于测试与问题定位。该晋级不创建 `v4.0.0`
正式标签，也不改变各能力原有的 live / hardware 验证边界。

生产架构保持：

```text
Rust Client
  -> WebRTC RTP/DataChannel
  -> Go Realtime Edge Gateway
  -> Python AI Orchestrator
  -> STT / Router+LLM / MCP / TTS
```

- Go 负责实时传输、ICE/TURN、RTP、DataChannel、session 和 downlink gate。
- Python 负责鉴权、AI 编排、Runtime Snapshot、round/playback、trace 和 Bot 行为。
- M1 `/internal/voice/ws` 继续作为当前默认业务路径；M0 只作显式回滚。
- v2 Rust 直连 Python WebSocket 是版本回滚路径，不与 V3 自动混跑。

## 已完成并有证据的范围

| 范围 | 状态 | 当前证据边界 |
| --- | --- | --- |
| Rust WebRTC、Go Gateway、Python 编排主链 | DONE | 本地/混合真实服务验证通过；不等于物理设备验证 |
| M1 音频、client event、interrupt、playback report | DONE | 四轮 M1 voice 4/4，stale audio frame 为 0 |
| Router + Tool + Vision 独立 Gold | VERIFIED | `acceptance-2026-08-09-v6`：Router 94/94、Tool 21/21、Vision 1/1 |
| Direct Text、Utils、Robot Voice Route | HYBRID_VERIFIED | 当前 V3 代码基线已有结构化证据 |
| 固定图片 Vision | HYBRID_VERIFIED | 图片与 Gold 均有 SHA-256 绑定 |
| TURN UDP Relay | LIVE_VERIFIED | relay candidate 5/5；强制 Relay 语音轮次通过 |
| Complex Workflow v1 | IMPLEMENTED | 功能开关保护且生产默认关闭；尚无生产 Live 结论 |
| 仓库 Harness | DONE | release gate、证据等级、哈希绑定与仓库卫生已程序化 |

最近一次完整回归记录：Python 634 tests + 106 subtests、Go full suite、Rust 147、
Router 94/94、Tool 21/21、Vision 1/1、offline weak-network 4/4。该记录不能替代
最终发布提交上的 release gate。

## V3 发布收尾

| ID | 状态 | 收尾条件 |
| --- | --- | --- |
| V3-CLOSE-001 | DONE | V3 发布时 `main` 与 `release/v3.0` 同步到最终提交 `b8995981`；当前 `main` 已晋级 V4 候选 |
| V3-CLOSE-002 | DONE | 最终提交的 release gate 已通过 |
| V3-CLOSE-003 | DONE | Gold 与需声明的 Hybrid/Live 证据已按最终基线复核 |
| V3-CLOSE-004 | DONE | Runtime Snapshot、migration、配置和回滚入口已完成发布核对 |
| V3-CLOSE-005 | DONE | `v3.0.0-rc1` 与 `v3.0.0` 已创建并保留 |

V3 状态为 `RELEASED`。后续若发现 V3 问题，使用补丁版本或从 `v3.0.0` 建立修复分支，
不得在标签上继续堆叠提交。

## 已知限制，不阻塞 V3

| 项目 | 状态 | 处理方式 |
| --- | --- | --- |
| 实体机 serial wake、麦克风、扬声器和 CPAL 长稳 | DEFERRED | 用户已决定暂缓；不得声明 `HARDWARE_VERIFIED` |
| TURN TCP/TLS | UNKNOWN | Rust 当前验证主链使用 TURN UDP；后续独立实现和验收 |
| MQTT 动作物理结果 | UNKNOWN | `test_01` 只证明固定 Topic 消息与软件链路，不证明实体动作 |
| Complex Workflow 生产启用 | DEFERRED | V3 保持默认关闭；启用前单独做副作用与打断验收 |
| `/internal/status` 公网暴露风险 | KNOWN_LIMITATION | 只能置于 localhost、受信网络或管理代理后 |

## V4 当前队列

| ID | 状态 | 工作 | 决策 |
| --- | --- | --- | --- |
| V4-TURN-001 | IN_PROGRESS | `turn-gate/` 与 Turn Detection / EOU 评测 | 产品 V4 独立基线；不得混入 V3 release evidence |
| V4-DEPLOY-001 | DESIGN_ACCEPTED | Python 服务部署边界、根 `.env` 契约和按服务发布 | 根 `.env`、共享镜像、Compose 多容器及 `wzk-*` 命名已确认；实施前先盘点线上 |
| V4-DEPLOY-002 | IMPLEMENTED | V4 Python Compose、唱歌资产只读挂载与开发 override | 静态契约通过；尚未迁移或验证线上服务 |
| V4-MCP-001 | DONE | 移除专用 `mcp-weather` | 代码、兼容 Compose 和运维入口已移除；历史 Snapshot v161 检查记录显示活动记录/绑定为 0，Websearch 保持启用；当前运行态仍需发布时复核 |
| V4-GATEWAY-001 | QUEUED | 继续拆分 `gateway_server.py` 的 LLM/TTS streaming 路径 | 依据维护收益分批进行，不与部署迁移同时大改 |
| V4-TTS-001 | QUEUED | 新增 TTS Provider adapter | 保持 Bot/Gateway 字段稳定，只扩展 Profile/provider 配置 |
| V4-TURN-RELAY-001 | QUEUED | TURN TCP/TLS 客户端支持与独立门禁 | 不能用 URL 已下发代替客户端实际选路证据 |
| V4-CLEANUP-001 | DEFERRED | 清理 5 个历史 Rust rollback worktree | 约占 7 GiB；先保全未跟踪脚本和 `04_head_with_baseline_app` 的修改，再移除 worktree |
| V4-EXPERIMENT-001 | PAUSED | Seed-VC/SVC 隔离 worktree | 存在未提交评分脚本和 incoming 输入；继续隔离保留，不纳入日常主线清理 |
| V4-ARCH-001 | DEFERRED | Python orchestration 是否迁移到 Go | 没有压力测试和瓶颈证据时不重写 |

## 追踪规则

- 真实状态以运行服务、Runtime Snapshot、最终提交和结构化证据为准。
- `reports/` 是本地证据，不是版本化 Gold；Gold 只在 `data/eval_gold/`。
- V4 测试候选可在 release gate 通过后进入 `main`；`develop/v4.0` 保留，V3 标签保持不可变。
- rollback worktree 的清理不得用模糊批量删除：先逐个确认 dirty/untracked 内容并做保全清单。
- 每次发布候选变化都必须重新绑定 commit、working-tree diff 和相关证据哈希。
