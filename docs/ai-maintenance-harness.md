# 可选维护检查工具

`scripts/ai_quality_gate.py` 调度现有检查并保存日志，可按需要使用。
日常开发可直接运行相关测试；没有固定阅读顺序、预检前置要求或 AI 回复格式。

## 常用命令

```bash
# 轻量检查：仓库卫生和 diff 格式
python scripts/ai_quality_gate.py --mode quick

# 根据改动文件选择语言套件，先查看计划
python scripts/ai_quality_gate.py --mode changed --dry-run
python scripts/ai_quality_gate.py --mode changed

# 全量检查；适用于确实需要扩大验证范围时
python scripts/ai_quality_gate.py --mode full

# 全量检查，并列出需要另行执行的发布验证
python scripts/ai_quality_gate.py --mode release
```

`changed` 对 Python 改动运行完整 pytest，对 Go/Rust 改动运行相应语言套件。
`--paths` 影响套件选择，不会缩小到单个测试。小改动可直接运行对应测试，
无需经过调度器。`--base <commit>` 可用于查看已提交改动对应的检查范围。

## 工具行为

- 完整日志保存到 `tmp/ai-quality-gate/<run-id>/`，终端返回紧凑摘要和有限的失败片段。
- 日志按 Harness 的密钥清单及凭据模式脱敏；需要排障时可查看对应日志。
- 调度器在第一个失败后停止；可单独重跑受影响的检查。
- LLM、Gold 或评测脚本改动会选择 Gold 校验。
- `release` 不会自动连接真实服务、发布 MQTT 或操作设备。

日常汇报说明实际检查结果与未验证范围即可。选择正式结构化验收时，
可使用 [证据工具说明](evidence-gates.md) 和 `.ai/harness.json`；这些报告格式
仅用于该工具的验收结果，不作为所有开发任务的完成条件。
