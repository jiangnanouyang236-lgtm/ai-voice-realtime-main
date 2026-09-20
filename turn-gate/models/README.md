# Turn Gate 运行时模型

这里保存 Python Gateway 使用的官方运行时模型。裸进程部署时可以原样同步整个 `models/`
目录，也可以只同步下面两个模型目录：

```text
models/
├── smart-turn-v3.2/
│   └── smart-turn-v3.2-cpu.onnx
├── livekit-eou-v0.4.1-intl/
│   ├── onnx/model_q8.onnx
│   ├── tokenizer.json
│   └── tokenizer/config 等配套文件
└── model-lock.json
```

模型文件保留在项目工作区，但不提交 ONNX、tokenizer、下载缓存或部署压缩包。部署前运行：

```bash
python turn-gate/scripts/verify_runtime_models.py
```

每个模型至少锁定：官方仓库、模型 revision、ONNX 文件名与 SHA256、文件大小、tokenizer revision、官方 inference 来源、输出语义和默认 threshold。未核对完成前不得开始正式 benchmark。
