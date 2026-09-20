# Turn Detection 独立验证报告

状态：`NOT_RUN`

## 实验锁定

- 机器：macOS Apple Silicon M1 / 16 GB
- Runtime：ONNX Runtime CPU
- Smart Turn 模型/revision/SHA256：待填写
- LiveKit EOU 模型/revision/SHA256：待填写
- tokenizer revision：待填写
- 数据 manifest SHA256：待填写

## Smart Turn V3

- 是否推荐：`UNKNOWN`
- Synthetic TTS：样本数/False End/Missed End 待填写
- Human Recorded：样本数/False End/Missed End 待填写
- P50/P95/P99：待填写
- Model Load / CPU / Peak RAM：待填写
- 主要优势：待填写
- 主要失败模式：待填写
- Top 20 False End：待填写
- Top 20 Missed End：待填写

## LiveKit EOU

- 是否推荐：`UNKNOWN`
- With Context：样本数/False End/Missed End 待填写
- Without Context：样本数/False End/Missed End 待填写
- P50/P95/P99：待填写
- Model Load / CPU / Peak RAM：待填写
- INT8 输出是否正常：`UNKNOWN`
- probability 是否异常集中于 1.0：`UNKNOWN`
- 是否确认量化损坏：`UNKNOWN`
- 主要优势：待填写
- 主要失败模式：待填写
- Top 20 False End：待填写
- Top 20 Missed End：待填写

## 独立结果对比

| 项目 | Smart Turn V3 | LiveKit EOU |
|---|---|---|
| 输入 | Audio | Text + Context |
| False End | 待填写 | 待填写 |
| Missed End | 待填写 | 待填写 |
| P50 | 待填写 | 待填写 |
| P95 | 待填写 | 待填写 |
| RAM | 待填写 | 待填写 |
| 中文效果 | 待填写 | 待填写 |
| 思考停顿 | 待填写 | 待填写 |
| 填充词 | 待填写 | 待填写 |
| 短回答 | 待填写 | 待填写 |
| 上下文理解 | 不适用 | 待填写 |
| 推荐程度 | `UNKNOWN` | `UNKNOWN` |

## 证据边界

本报告只描述独立模型本地实验。Synthetic TTS、真人录音、mock、单元测试或本机 CPU benchmark 均不代表现有 Voice Agent、WebRTC、Gateway 或硬件链路已验证。
