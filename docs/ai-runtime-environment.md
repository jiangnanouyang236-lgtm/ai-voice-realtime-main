# AI 调试与运行环境

本文是仓库可共享的环境事实入口。密钥值仍保存在未跟踪 `.env`；当前机器的额外信息保存在 `.ai/runtime.local.md`。

## 使用方式

先选择 Profile，再运行脱敏预检：

```bash
python scripts/ai_preflight.py --profile offline
python scripts/ai_preflight.py --profile local
python scripts/ai_preflight.py --profile compose-v3
python scripts/ai_preflight.py --profile compose-v4
python scripts/ai_preflight.py --profile lan
python scripts/ai_preflight.py --profile hybrid
python scripts/ai_preflight.py --profile hardware
python scripts/ai_preflight.py --profile model-host
```

需要实际 TCP 探测时显式增加 `--connectivity`。通过预检只代表具备调试前提，不代表业务 E2E 已验证。

## Profile

| Profile | 用途 | 允许的证据结论 |
|---|---|---|
| `offline` | 阅读、静态检查、Harness 自检 | 仓库结构、Python 与 Harness 前提可用；不代表全量单测通过 |
| `local` | 本机启动 Python/Go 服务 | 本机配置字段可解析；可开始本地 smoke |
| `compose-v3` | 渲染或启动 Compose v3 | Compose 输入变量可解析；仍需单独运行静态 Compose 校验 |
| `compose-v4` | 渲染或启动 V4 拆分 Python Compose | Compose 输入变量可解析；仍需运行 V4 契约测试/渲染校验 |
| `lan` | 开发机访问内网模型/数据库 | 内网配置字段可解析；可开始服务联调 |
| `hybrid` | 本地业务进程访问测试数据库、内网模型和授权 MQTT | 可开始本地进程加远程依赖的混合联调；不代表部署 E2E |
| `hardware` | Robot、音频前端、摄像头 | 相关配置字段和端点可解析；设备、协议和物理结果仍为 `UNKNOWN` |
| `model-host` | 管理 vLLM 模型服务器 | 模型环境字段和端口可解析；目录、GPU、运行时和模型状态仍为 `UNKNOWN` |

## 配置文件职责

| 文件 | 状态 | 职责 |
|---|---|---|
| `.env` | 未跟踪、当前开发机存在 | Python 服务、内网模型、数据库、MQTT 和凭据 |
| `rust_client/.env.local` | 未跟踪、当前开发机存在 | Robot 身份、Gateway、VAD 与音频前端 |
| `deploy/vllm/.env.models` | 未跟踪、当前开发机不存在 | 推荐的统一模型层 Compose 配置；只在 model host 必需 |
| `deploy/vllm/*/.env.*` | 未跟踪、当前开发机存在 | 旧的单服务模型启动配置，不是主入口 |
| `.env.example` | 已跟踪 | 业务层变量契约和安全默认值，不是运行事实 |
| `deploy/vllm/.env.models.example` | 已跟踪 | 模型层变量契约，不是运行事实 |

不要 source 未审查的环境文件来做盘点；预检工具只解析 `KEY=value`，不会执行其中内容。

### 最终值解析顺序

- Python：父进程环境优先，根 `.env` 只补缺失项。
- Rust `run.audio_frontend_tcp.sh`：会 source `rust_client/.env.local`，文件中的同名值可能覆盖父 shell。
- Compose：shell 与根 `.env` 做变量插值；Vision 使用 `VISION_INTERNAL_TOKEN` 同时注入 Go 和 LLM。
- 模型 Compose：显式读取 `deploy/vllm/.env.models`。
- Bot/MCP/Agent/TTS Profile：运行时再由 MySQL Snapshot 决定业务绑定。

因此文档值、example 和本地文件都不能单独证明最终生效配置；需要结合进程配置输出、Snapshot 版本和真实请求日志。

## 当前配置拓扑

以下为 2026-08-08 从本机未跟踪配置中脱敏整理的 `CONFIGURED` 事实；除非另有 live check，不表示服务当前可达。

音频环境必须区分：当前生产机器人使用讯飞语音硬件模组提供的系统默认麦克风/扬声器和
串口 KWS；下表的本机 `Audio frontend` TCP 配置仅表示自研软件替代路线的当前开发机配置，
不代表生产机器人。详细矩阵见 [`audio-endpoint-matrix.md`](./audio-endpoint-matrix.md)。

| 能力 | 配置端点 | 模型/说明 |
|---|---|---|
| Vision snapshot | `10.10.6.121:15010` | Go Gateway 内部图像入口 |
| Router LLM | `10.10.6.121:15100` | `qwen3-5-4b` |
| Main LLM | `10.10.6.121:15101` | `qwen3-5-9b` |
| ASR | `10.10.6.121:15110` | `Qwen3-ASR-1.7B` |
| TTS CustomVoice | `10.10.6.121:15120` | `qwen3-tts` |
| TTS Base | `10.10.6.121:15121` | `qwen3-tts-base` |
| MySQL server-config | `10.10.6.121:15501` | 凭据只在 `.env` |
| Python Gateway internal | `127.0.0.1:7860` | 本机编排入口 |
| LLM/STT/TTS gRPC | `127.0.0.1:50053/50054/50052` | 本机业务服务 |
| Rust Client Gateway | `frp.wzk.icu:15011/ws` | 当前 `.env.local` 使用 WSS |
| Robot/Cognitive MQTT | `140.249.22.147:1883` | MQTT 不使用 HTTP 代理 |
| Audio frontend | `127.0.0.1:39001` | 当前为 TCP 模式 |

模型、数据库和 Gateway 可能位于不同网络边界。连接失败时先确认 LAN、ZeroTier、FRP 或现场网络，不得直接修改业务代码。

## 下载代理

当前 shell 没有常驻代理变量。历史上最近使用的本机代理是 `http://127.0.0.1:7890`；另有 LAN 代理 `http://10.10.5.68:7890`，其当前可达性为 `UNKNOWN`。使用前必须先探测，不得默认永久有效。

临时启用本机代理：

```bash
export HTTP_PROXY=http://127.0.0.1:7890
export HTTPS_PROXY="$HTTP_PROXY"
export http_proxy="$HTTP_PROXY"
export https_proxy="$HTTPS_PROXY"
export NO_PROXY=localhost,127.0.0.1,::1,10.10.6.121,10.10.5.68
export no_proxy="$NO_PROXY"
```

关闭：

```bash
unset HTTP_PROXY HTTPS_PROXY ALL_PROXY NO_PROXY
unset http_proxy https_proxy all_proxy no_proxy
```

约束：

- 内网模型、数据库和 localhost 必须走 `NO_PROXY`。
- 优先使用命令级或当前 shell 代理，不写全局 Git/System/Docker 配置。
- 模型下载优先使用已有内网模型目录或中国大陆镜像；下载前确认目标目录、磁盘和版本。
- 代理检查失败不能自动换任意公网代理，也不能把下载失败当成代码故障。

## 测试入口

真实 pytest 根目录是 `test/`；`tests/` 不再承载文档或测试。

```bash
# Harness 自测
python -m pytest test/test_ai_preflight.py test/test_repo_hygiene.py -q

# Python 静态检查
python -m compileall gateway llm mcp_servers server_config stt tts scripts -q

# 按模块选择 pytest
python -m pytest test/test_<module>.py -q

# Compose 静态渲染，不代表当前 .env 或服务已就绪
python scripts/validate_compose_v3.py
```

Go、Rust、Admin UI、live voice 和硬件命令见 `START.md`。不需要对应能力时，不做全量构建或依赖安装。

## 证据边界

- `python scripts/validate_compose_v3.py`：只证明合成配置可渲染。
- TCP 端口可连：只证明 listener 存在，不能证明它属于当前 checkout。
- mock/单测：只能证明代码路径。
- 真实 ASR/LLM/TTS/MCP 请求：证明服务语义，但仍不等同物理 Robot/音频结果。
- Robot、摄像头、播放和打断必须保留真实设备日志与业务结果。
