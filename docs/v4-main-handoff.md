# 4.0 主线交接说明

## 版本范围

本次交接以用户指定的稳定运行版本 `develop/v4.0` 为基线：

- 基线提交：`b820912d17208426893ca7d9e6cb884a9624dad0`。
- 目标仓库：`http://10.10.52.100/robot/peiban/ai-voice-realtime`，目标分支：`main`。
- 仅精简面向 AI 编程助手的仓库流程说明，并更新交接文档。
- 业务源码、模型提示词、协议、依赖、运行配置、测试、检查脚本与 CI 保持基线内容。
- 不包含 `develop/v4.1` 后续提交，也不包含开发机器上的未提交改动。

“稳定运行版本”来自交接方选定的基线。本次仅核对文件范围和文档检查，
未重新执行真实服务、语音链路或设备验收，也未发布部署。

## AI 协作方式

`AGENTS.md` 与 `CLAUDE.md` 仅保留项目导航和事实说明。
移除强制阅读顺序、固定预检流程、日常 Harness 必跑要求、固定汇报标签及
日常必须生成结构化证据报告的要求。接手者可按任务选择文档、工具和验证方法。

现有 Harness、Gold、证据校验器及其回归测试继续保留，供需要时使用。
使用这些工具时仍遵循其输入格式；现有 CI 检查保持不变。
业务鉴权、密钥保护、数据库约束和运行时 Agent 提示词不属于本次移除范围。

## 接手入口

- 启动与服务命令：[START.md](../START.md)。
- 新人上手：[getting-started.md](getting-started.md)。
- 系统边界与代码入口：[project-context-for-ai.md](project-context-for-ai.md)。
- 部署配置与故障定位：[operations-reference.md](operations-reference.md)。
- 已知问题与历史进展：[voice_gateway_task_tracker.md](voice_gateway_task_tracker.md)。
- 可选检查工具：[ai-maintenance-harness.md](ai-maintenance-harness.md)。

## 运行环境交接

Git 交付不包含真实 `.env`、数据库内容、模型权重、音频采集、构建包和设备凭据。
接手部署还需要由交接方通过安全渠道提供实际环境配置、MySQL server-config
及迁移状态、模型服务地址、Robot/MQTT/ICE-TURN 配置、部署版本和回滚材料。
文档中的历史地址和 example 只作参考，实际 Bot/Robot/MCP/TTS 绑定以运行态为准。

## 分支与同步

`develop/v4.0` 继续保留原基线，`release/v2.0`、`release/v3.0` 保留历史版本。
本次只向上述目标仓库同步交接内容，不设置后续自动提交或推送。
目标 `main` 若已有独立初始化提交，可保留其历史进行合并，但合并结果的文件树
应与本交接版本一致；不引入初始化 README 覆盖本项目文档。
正式部署可固定本次交接提交，代码回退可使用上述 4.0 基线。
