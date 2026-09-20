# 裸进程部署清单

Turn Gate 分为三个独立进程，环境变量不得跨进程继承：

- Go Gateway：`go-gateway.env.example`
- Python Gateway：`python-gateway.env.example`
- Rust 真机：`rust-client.env.example`

服务器部署前先校验本地权重：

```bash
python turn-gate/scripts/verify_runtime_models.py
```

需要单文件传输时再生成部署包（产物进入已忽略的 `turn-gate/dist/`）：

```bash
python turn-gate/scripts/package_runtime_models.py
```

压缩包解压后的根目录就是两个模型目录和 `model-lock.json`，可直接放入
`/data/models/turn-gate/`。

服务器只需要同步：

```text
gateway/turn_gate_policy.py
gateway/turn_gate_shadow_models.py
gateway/audio_protocol.py
gateway/config.py
gateway/gateway_server.py
requirements-turn-gate.txt
turn-gate/models/smart-turn-v3.2/
turn-gate/models/livekit-eou-v0.4.1-intl/
turn-gate/models/model-lock.json
go_voice_gateway Linux AMD64 二进制
```

建议服务器模型目标目录：

```text
/data/models/turn-gate/
├── smart-turn-v3.2/
└── livekit-eou-v0.4.1-intl/
```

Python 虚拟环境安装新增依赖：

```bash
source env/bin/activate
python -m pip install -r requirements-turn-gate.txt
```

先重启 Python Gateway，确认启动日志中 `smart_turn=ready`、`livekit_eou=ready`，再重启
Go Gateway 和 Rust Client。回滚时将三端 Active 开关分别设为 `false`；完全回到 VAD-only
时再分别关闭三端 Shadow。
