# 版本与发布管理

本文只保留当前有效的分支、发布和回滚规则。历史 RC 的实施过程由 Git 和 tag 追溯。

## 分支与标签

| 分支 / 标签 | 定位 | 规则 |
| --- | --- | --- |
| `main` | 4.0 交接主线 | 基于 `develop/v4.0` 的 `b820912d`；范围见 [交接说明](v4-main-handoff.md) |
| `release/v2.0` / `v2.0.0` | V2 维护与回滚基线 | 只接必要 hotfix |
| `develop/v3.0` | V3 历史集成线 | 不再作为当前发布入口 |
| `release/v3.0-rc` | V3 历史 RC 线 | 不再移动 |
| `release/v3.0` / `v3.0.x` | V3 发布与维护线 | 正式发布后使用 |
| `codex/*` / `feature/*` | 短周期工作分支 | 验证后合入目标集成线，不长期代替集成线 |
| `develop/v4.0` | V4 保留测试线 | 保留 V4 测试和问题定位，不因 `main` 晋级而删除 |

历史回滚标签：

- `v3.0-webrtc-rc1`：2026-06-30 首个真实 WebRTC 主链基线。
- `v3.0-before-tts-profile`：2026-07-01 TTS Profile 改造前基线。

它们只用于定位和回滚，不代表当前 V3 发布候选。

## V3 冻结规则

V3 已发布并冻结。`release/v3.0` 和 `v3.0.0` 保持为维护、回滚基线，只接受：

- 发布门禁或真实链路发现的缺陷修复；
- 安全、配置、迁移、回滚和文档纠偏；
- 为最终证据增加的最小测试或观测修复。

以下工作移出 V3：

- `turn-gate/`、Turn Detection、EOU 模型或评测；
- 继续大规模拆分 Python Gateway；
- 新增 TTS Provider；
- TURN TCP/TLS 新实现；
- 没有瓶颈证据的 Python-to-Go 重写。

## V3 历史 RC 流程

以下内容只用于追溯 V3，不是当前 V4 发布命令：

1. 将已审核工作 Fast-forward 或合并到 `develop/v3.0`。
2. 确认工作树干净，V4、本地产物、`.env`、模型和日志均未进入候选提交。
3. 运行：

   ```bash
   python scripts/ai_preflight.py --profile offline
   python scripts/ai_quality_gate.py --mode release
   ```

4. 对需要声明的能力，在同一提交重新生成并校验 Gold、Hybrid/Live evidence。
5. 核对 `docs/deployment-checklist.md`、Runtime Snapshot、migration 和回滚入口。
6. 创建 annotated tag `v3.0.0-rc1`，部署 RC 并观察。
7. RC 无阻塞回归后合入 `main`，创建 annotated tag `v3.0.0`。
8. 如需长期维护，从正式标签创建 `release/v3.0`。

任何旧提交上的测试或 evidence 都只能作为回归参考，不能替代最终 tag commit 的门禁。
推送分支和标签必须由发布负责人明确授权。

## V4 历史主线规则

以下记录此前的测试候选流程；本次交接以 [4.0 交接说明](v4-main-handoff.md) 为准，
不将历史 AI 流程或固定证据标签作为日常开发要求。

- 2026-08-26 起，完成 release gate 的 V4 测试候选可以快进到 `main`；这不等于创建
  `v4.0.0` 正式发布标签，也不等于生产或实体机验收完成。
- `develop/v4.0` 和当前 V4 功能分支继续保留，用于测试、问题定位和后续修复。
- `release/v3.0`、`v3.0.0-rc1`、`v3.0.0` 保持不可变，V3 回滚能力不受 `main` 晋级影响。
- `turn-gate/` 不加入仓库级 `.gitignore`，避免未来 V4 正式纳管时被静默遗漏。
- V3 evidence 仍只绑定原 V3 提交；不得把它升级为 V4 的 release、live 或 hardware 证据。
- V4 在测能力必须继续区分 `IMPLEMENTED`、`LOCAL_VERIFIED`、`LIVE_VERIFIED` 和
  `HARDWARE_VERIFIED`，合入 `main` 本身不提升证据等级。

## 发布与回滚

正式发布使用 tag 或明确 commit，不以浮动 feature 分支作为部署版本。
当前 `main` 用作 4.0 交接主线；未经发布负责人明确确认，不创建或移动
`v4.0.0-rc*` / `v4.0.0` 标签。已有 V3 标签保持不可变。

发生故障时：

1. 保存 Runtime Snapshot、镜像 tag、Git commit 和失败 trace。
2. 单服务故障优先回滚对应 Go/Python/Rust 包或镜像。
3. V4 测试主链故障可回滚到 `v3.0.0`；V3 维护线故障再回滚上一 `v3.0.x` 或 `v2.0.0`。
4. 在对应 release 分支修复并重跑同等级门禁，禁止直接修改已发布 tag。

## 仓库整理

- 本地可再生成缓存使用 `make clean-local-artifacts`，但不得删除 `dist/`、
  `rust_client/temp/`、硬件实测结果、实验原始证据或用户交付物。
- 删除分支前用 `git branch --merged <target>` 确认已合入。
- 不提交 `.env`、凭据、模型、日志、缓存、构建目录和一次性报告。
- 每批提交先运行 `git diff --cached --check` 和
  `python scripts/check_repo_hygiene.py --index`。
