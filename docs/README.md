# 文档导航与真相源

2026-09-14现场网络补充见[WebRTC迁移与机器人运维](handover/WEBRTC_NETWORK_HANDOVER.md)：新网络服务器WS15011、NGINX/FRPS/coturn、新旧隧道和机器人systemd入口。此为用户提供的部署快照，不改变仓库通用默认或声明完整语音验收。

当前交接主线 `main` 基于 `develop/v4.0`（`b820912d`），范围与环境交接事项见
[4.0 交接说明](v4-main-handoff.md)。本次分支交接不包含部署或重新进行实体机验证。

## 首次阅读

1. [`getting-started.md`](./getting-started.md)：新人 15/30/60 分钟上手路径。
2. [`project-context-for-ai.md`](./project-context-for-ai.md)：当前系统边界与配置真相源。
3. [`../START.md`](../START.md)：当前主线启动方式。
4. [`ai-runtime-environment.md`](./ai-runtime-environment.md)：运行 Profile、网络和本机事实。
5. [`architecture-and-design.md`](./architecture-and-design.md)：WebRTC、Go、Python 和模型服务边界。
6. [`operations-reference.md`](./operations-reference.md)：环境变量、运维与诊断命令。

## 当前主线

| 主题 | 文档 |
| --- | --- |
| 版本、分支、发布与回滚 | [`version-management.md`](./version-management.md) |
| 发布前检查 | [`deployment-checklist.md`](./deployment-checklist.md) |
| V4 拆分 Python 服务 | [`docker-compose-v4.md`](./docker-compose-v4.md) |
| Go + Python 兼容部署入口 | [`docker-compose-v3.md`](./docker-compose-v3.md) |
| Go-Python M1 协议 | [`go-python-internal-protocol-v1.md`](./go-python-internal-protocol-v1.md) |
| 自然打断 | [`natural-barge-in-v1.md`](./natural-barge-in-v1.md) |
| 生产/替代音频端点 | [`audio-endpoint-matrix.md`](./audio-endpoint-matrix.md) |
| Turn Gate | [`turn-gate-v1.md`](./turn-gate-v1.md) |
| 当前状态与未完成项 | [`voice_gateway_task_tracker.md`](./voice_gateway_task_tracker.md) |
| 架构决策 | [`decisions.md`](./decisions.md) |

## 验证与仓库维护

| 主题 | 文档 |
| --- | --- |
| 可选维护检查工具 | [`ai-maintenance-harness.md`](./ai-maintenance-harness.md) |
| 证据等级 | [`evidence-gates.md`](./evidence-gates.md) |
| 评测 Gold | [`evaluation/README.md`](./evaluation/README.md) |
| 仓库留存和清理 | [`repository-hygiene.md`](./repository-hygiene.md) |
| 日志 | [`logging.md`](./logging.md) |

## 历史与兼容边界

- 文件名中的 `v3` 不一定表示废弃：`docker-compose.v3.yml` 仍是 Go Gateway 与原拆分业务
  服务的兼容部署入口；当前 V4 Compose 只负责拆分后的 Python 服务。
- [`barge-in-v4-design-draft.md`](./barge-in-v4-design-draft.md) 是历史设计草案；当前实现以
  [`natural-barge-in-v1.md`](./natural-barge-in-v1.md) 和代码为准。
- `reports/`、`tmp/`、模型、真实音频和发行包不属于版本化文档，不得用其中旧结果替代
  当前提交上的结构化证据。
