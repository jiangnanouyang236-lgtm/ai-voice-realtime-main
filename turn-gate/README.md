# Turn Gate：实时语音轮次判定

这里的“产品 V4”是仓库版本；“Smart Turn V3”是上游模型名称，两者不是同一个版本号。
这里集中维护 Turn Gate 的运行时模型、部署配置、评测数据、脚本与真机验收清单。

当前状态：`PROVISIONAL_ASR_JOINT`。已锁定两个官方 CPU INT8 ONNX，完成 50 条 Smart Turn、50 条 LiveKit EOU、14 组 context 对照、Router 4B 温度 0 双轮复测，并通过项目真实 Qwen ASR gRPC 服务完成 50 条联合链路测试；尚未录制真人语音，也未包含真实 VAD candidate、重复候选和 timeout 时序，因此不作生产结论。当前本地结果见 `results/sanity-summary.md`。

## 边界

- Smart Turn V3 只接收音频，单独统计 Synthetic TTS 与 Human Recorded。
- LiveKit EOU 只接收 transcript 与 conversation context，不读取 Smart Turn 分数。
- 联合 Demo 接真实 ASR 和 Router 4B，但不接 TTS 回复、WebRTC、Barge-in 或现有 Gateway。
- 不修改生产状态机，不做训练或自行量化。
- 合成数据、真人数据和上下文对照不得合并成一个准确率。

## 目录

```text
turn-gate/
├── data/
│   ├── smart_turn/
│   │   ├── cases.seed.jsonl
│   │   ├── synthetic/audio/       # 生成文件，不提交
│   │   └── human/
│   │       ├── README.md
│   │       └── audio/             # 真实录音，不提交
│   └── livekit_eou/cases.seed.jsonl
├── models/                         # 模型文件不提交
├── deploy/                         # Go/Python/Rust 裸进程独立配置
├── HARDWARE_TEST_GUIDE.md          # 真机测试话术与通过建议
├── results/                        # 推理结果不提交
├── scripts/
│   ├── validate_dataset.py
│   ├── generate_tts_dataset.py
│   ├── test_smart_turn.py
│   ├── test_livekit_eou.py
│   ├── build_livekit_dataset.py
│   ├── analyze_joint_results.py
│   ├── test_llm_turn_judge.py
│   ├── summarize_latency.py
│   ├── demo_turn_gate_with_asr.py
│   └── analyze_results.py
└── report.template.md
```

部署前先执行 `python turn-gate/scripts/verify_runtime_models.py`；需要单文件传输时使用
`python turn-gate/scripts/package_runtime_models.py`。裸进程部署步骤见
`deploy/README.md`。真机测试直接按 `HARDWARE_TEST_GUIDE.md` 执行。

## 数据约定

标签只能是 `END` 或 `CONTINUE`。

Smart Turn 每一行代表一个 candidate end，而不是一整段对话：

```json
{"id":"st-c-001-mid","category":"thinking_pause","source":"synthetic_tts","text":"我觉得这个事情","ground_truth":"CONTINUE","segments":[{"text":"我觉得这个事情"},{"silence_ms":800},{"text":"还是应该先做语音"}],"candidate_after_segment":1,"trailing_silence_ms":800}
```

LiveKit EOU 的 `pair_id` 用于绑定 with/without context 对照：

```json
{"id":"eou-j-001-context","pair_id":"eou-j-001","category":"short_answer","context":[{"role":"assistant","text":"你想查询哪个城市？"}],"text":"杭州","ground_truth":"END","context_mode":"with_context"}
```

## 离线检查

不需要安装任何额外依赖：

```bash
python turn-gate/scripts/validate_dataset.py \
  turn-gate/data/smart_turn/cases.seed.jsonl \
  turn-gate/data/livekit_eou/cases.seed.jsonl
```

## 结果分析

推理器后续应输出提示词约定的 CSV 字段，并始终保留原始 probability。分析脚本默认以 `END` 为正类，同时单独计算最重要的 False End Rate：

```bash
python turn-gate/scripts/analyze_results.py \
  --kind smart_turn \
  --input turn-gate/results/smart_turn.csv \
  --thresholds 0.30,0.40,0.50,0.60,0.70,0.80
```

LiveKit EOU 使用 `--kind livekit_eou`。脚本会按 threshold 输出 Accuracy、Precision、Recall、F1、False End Rate、Missed End Rate，以及 P50/P90/P95/P99 延迟。False End Rate 的分母是所有真实 `CONTINUE`；Missed End Rate 的分母是所有真实 `END`。LiveKit 默认扫描额外包含官方中文阈值 `0.0066` 附近的细粒度点。

常驻 Smart Turn / LiveKit EOU 实例的并发安全与吞吐可使用同一 WAV 重跑：

```bash
python turn-gate/scripts/benchmark_turn_gate_concurrency.py \
  --wav turn-gate/data/smart_turn/synthetic/audio/st-a-001.wav \
  --workers 4 --repeats 8
```

该结果只验证共享模型实例在指定机器上的推理一致性与排队表现，不包含 ASR、WebSocket、VAD
或硬件链路。当前 Gateway 为每个模型实例加了独立运行锁，因此 worker 数增加不代表 ONNX
推理会并行执行。

状态机时序参数可以复用已有 50 条对齐结果离线回放：

```bash
python turn-gate/scripts/simulate_turn_gate_timing.py \
  --cases turn-gate/data/smart_turn/cases.seed.jsonl \
  --smart turn-gate/results/smart_turn.50.csv \
  --eou turn-gate/results/livekit_eou.50.csv \
  --candidate-ms 300 --active-deadline-ms 300 \
  --failure-fallback-ms 1000 --hard-timeout-ms 1600 \
  --asr-ms 150 --smart-ms 50 --eou-ms 20 --control-ms 30
```

该模拟把 CONTINUE case 的 `trailing_silence_ms` 视为恢复说话时间，并单独报告确定
False End 和“恢复说话与 commit 同时发生”的 boundary risk。它只回放已有 synthetic 输出，
不重新运行模型、VAD、WebRTC 或 Gateway；非官方 EOU threshold 只能标记为同集探索结果。

当前单工真机候选参数集中记录在
`configs/single-duplex-v1-test.json`。其中时序项均已接入运行时，但状态仍是 `PROVISIONAL`；
在真人麦克风和硬件声学验证前，只能描述为可部署的测试候选，不能描述为生产配置。

模型的完整 revision、SHA256、大小、输入输出语义和本机 Runtime 见 `models/model-lock.json`。LiveKit 中文官方 calibrated threshold 为 `0.0066`，不可在正式结果中擅自替换成通用 `0.5`；阈值扫描应包含官方点和实验点。

## 下一阶段门禁

开始模型推理前必须先完成：

1. 从官方仓库核对当前推荐 CPU INT8 ONNX、官方 inference 代码和 threshold。
2. 在 `models/model-lock.template.json` 的副本中记录仓库、revision、文件 SHA256、大小及 tokenizer revision。
3. 确认 Smart Turn 的输入/输出定义，以及 LiveKit EOU 输出是否已是 probability；禁止猜 token id 或重复 sigmoid。
4. 生成 200～500 条 Smart Turn synthetic candidate，并单独录制 30～50 条真人样本。
5. 生成 200～500 条 LiveKit EOU 文本样本，其中短回答必须包含 with/without context 成对对照。

模型、音频和结果均为本地实验产物，默认不进入 Git。
