# 仓库整理与留存规则

## 目录职责

| 内容 | 目录 | 是否提交 |
|---|---|---|
| Python 单元测试 | `test/` | 是 |
| 长期架构、运维和设计文档 | `docs/` | 是 |
| 运行脚本、benchmark、probe | `scripts/` | 是 |
| 独立验收 Gold | `data/eval_gold/` | 是，必须通过 provenance/schema/hash gate |
| 生成评测报告 | `reports/` | 否；需要发布时提交脱敏 manifest/摘要 |
| 临时输入、实验和原始输出 | `tmp/` | 否 |
| 编译缓存/工作目录 | `target/`、`build*/` | 否 |
| 发行包 | 各模块 `dist/` | 否；确认留存或外部归档后再用 `make clean-v3-dist` |
| 真实环境和机器上下文 | `.env`、`.ai/runtime.local.md` | 否 |
| 本机交付物 | `local-artifacts/deliverables/` | 否；不得作为唯一备份 |

`local-artifacts/` 只是 Git 忽略的本机停放区，不是备份。唯一的交付包、实测素材和审计证据仍需保存到仓库外的持久介质，并记录校验和。

2026-08-09 二次整理后，旧评测报告、Gold 重建前实验和
`local-artifacts/legacy/` 已删除；历史代码和已提交材料按需从 Git 追溯。
当前验收只使用 `data/eval_gold/`，新运行报告按需生成到 `reports/`，同一
验收只保留最近一份有效报告。`local-artifacts/` 当前只留明确的用户交付物。

## 清理等级

1. **可直接清理**：缓存、构建目录、`.DS_Store`、`__pycache__`。
2. **先确认再清理**：发行包、真实采集音频、硬件测试结果、评测原始报告。
3. **不得自动清理**：`.env`、密钥、用户交付物、数据库、模型、未跟踪业务素材。
4. **需要 owner 授权**：Git 历史改写、force push、远端分支或部署产物删除。

日常使用：

```bash
make clean-local-artifacts
python scripts/check_repo_hygiene.py
# 暂存后检查即将提交的精确内容
python scripts/check_repo_hygiene.py --index
```

`clean-local-artifacts` 只清理缓存和编译工作目录，不处理 `dist/`、`rust_client/temp/`、`reports/`、`tmp/`、ESP32 声学证据、模型依赖缓存或用户交付包。

## 当前 worktree 留存边界

- `tmp/seed-vc-svc-poc-worktree` 是暂停中的隔离实验，存在未提交脚本和输入，不属于缓存。
- `Desktop/temp/rust_client_rollback_tests/` 下的五个 detached worktree 是 Rust 回滚对照，
  均有未跟踪启动脚本，其中 `04_head_with_baseline_app` 还有源码修改。
- 未逐个归档、校验并取得 owner 授权前，不得运行批量 `git worktree remove`、目录删除或 prune。
