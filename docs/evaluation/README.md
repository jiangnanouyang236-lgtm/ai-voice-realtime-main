# LLM 评测入口

仓库只保留一套版本化评测真相源：`data/eval_gold/`。旧的
`data/eval_cases/`、自动扩写数据、模型 corrector、宽回归集和一次性
Chain/Stress 脚本已经删除；历史内容仍可从 Git 找回，不在当前工作树重复维护。

## 验收原则

- 禁止用被测模型输出创建或修改 Gold。
- Router 覆盖 Chat、Exit、Vision、WebSearch、Robot、Task、Utils、Complex。
- Tool 评测始终提供完整候选集，并精确检查工具名和完整参数。
- `any`、无效响应、请求异常、参数缺失或额外参数均失败。
- Vision 必须发送 Gold 中经过哈希绑定的真实图片，并检查必需与禁用事实。
- 报告记录 checkout、模型、endpoint、工具 Schema、Harness 和 Gold 哈希。
- `reports/` 是 ignored 的本地运行证据，不是版本化基线。
- Router/Tool/Vision 模型直连通过不等于 Gateway、MCP、MQTT 或硬件 E2E。

## 当前版本化基准

- `data/eval_gold/manifest.json`：来源、审核状态和候选/参数策略。
- `data/eval_gold/4b_router.jsonl`：94 条 Router Gold。
- `data/eval_gold/9b_tool.jsonl`：21 条 Tool Gold。
- `data/eval_gold/vision/`：1 条真实图片 Vision Gold 与哈希绑定 fixture。
- `acceptance-2026-08-09-v6` 是当前唯一验收基准，状态为 `owner_approved`。
- 基准变更必须经过人工复核并递增 revision；真实模型运行结果只能进入
  ignored 的 `reports/`，不得反向修改 Gold。

## 保留的入口

```bash
# 静态校验 provenance、Schema、覆盖和文件哈希；不访问模型
python scripts/validate_eval_gold.py \
  --require-owner-approved \
  --output reports/eval-gold-validation.json

# Router + Tool + Vision 统一真实模型验收
python scripts/eval_acceptance.py \
  --output reports/eval-acceptance.json

# 本地 LLM 服务启动后，检查所有启用 Bot 的 Utils 路由与事实忠实度
python scripts/eval_bot_utils_matrix.py \
  --port 50053 \
  --output reports/bot-utils-route-matrix.json
```

`scripts/eval_runner.py` 和 `scripts/eval_vision_e2e.py` 仅提供统一验收内部复用的
请求、Schema 和结果校验函数，不是独立命令入口。
