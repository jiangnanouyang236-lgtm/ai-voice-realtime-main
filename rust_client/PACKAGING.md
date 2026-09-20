# Rust Client Packaging Guide

这个文档说明当前 `rust_client` 的打包重点。
这版客户端的默认唤醒来自硬件 USB 串口 JSON 或本地 HTTP `/wakeup` 入口；也可以
通过 `WAKE_SOURCE_MODE=kws|both` 接入 `audio_frontend` 的 KWS TCP 唤醒事件。

## 发布包建议包含

- `rust_client` 可执行文件
- `assets/wake_audio/ding.wav`，用于 ASR 返回有效文本后的本地确认音
- 启动脚本 `run.sh`

推荐目录结构：

```text
dist/
  macos/
    rust_client
    run.sh
    assets/
      wake_audio/ding.wav

  ubuntu22/
    rust_client
    run.sh
    assets/
      wake_audio/ding.wav

  nano-arm64/
    rust_client
    run.sh
    audio/
      wake_audio/ding.wav
```

## 当前依赖特点

`rust_client` 当前核心依赖：

- `cpal`
- `webrtc-vad`
- `serialport`
- `tokio-tungstenite` with `native-tls`

这意味着它仍然依赖平台相关能力：

- 音频栈：CoreAudio / ALSA
- TLS：系统 OpenSSL 或 macOS Security Framework
- 串口设备访问：USB CDC / UART

但已经不再需要旧的软件唤醒模型目录和额外动态库。

## 构建策略

### 调试 / 测试版本

线上临时排查问题时，可以先用 dev 构建。这个版本编译快、调试信息更友好，
但运行性能明显不如 release：

```bash
cd /Users/wuzikang/Desktop/py/dify_stream_test/rust_client
cargo build
# 或
make build-dev
```

产物位置：

```text
rust_client/target/debug/rust_client
```

### Release 版本

Release profile 当前偏激进优化，目标是尽量提高运行速度，允许编译更慢：

- `opt-level = 3`
- `lto = "fat"`
- `codegen-units = 1`
- `panic = "abort"`
- `strip = "symbols"`
- `incremental = false`

如果是在最终目标机器本机编译，并且不需要把二进制拷到不同 CPU 的机器上运行，可以进一步尝试：

```bash
RUSTFLAGS="-C target-cpu=native" cargo build --release --features native-webrtc
```

这个选项不要默认用于 Docker buildx 跨端包，因为 buildx 的构建 CPU 不一定等于真实部署机器 CPU。

## macOS

### 编译

```bash
cd /Users/wuzikang/Desktop/py/dify_stream_test/rust_client
cargo build --release --features native-webrtc
```

### 启动脚本示例

```bash
#!/usr/bin/env bash
set -euo pipefail
DIR="$(cd "$(dirname "$0")" && pwd)"
: "${ROBOT_ID:?ROBOT_ID is required}"
export ASR_DING_SOUND_PATH="${ASR_DING_SOUND_PATH:-$DIR/assets/wake_audio/ding.wav}"
export OPUS_BITRATE_BPS="${OPUS_BITRATE_BPS:-16000}"
exec "$DIR/rust_client" "$@"
```

### 说明

- Apple Silicon 和 Intel Mac 最好分别打包。
- 如果机器上有多个 USB 串口，建议在启动脚本里显式指定：

```bash
export WAKE_SERIAL_PORT=/dev/cu.usbserial-1110
```

## Ubuntu 22.04 x86_64

### 编译依赖

```bash
sudo apt update
sudo apt install -y \
  build-essential \
  pkg-config \
  libasound2-dev \
  libssl-dev \
  libudev-dev \
  ca-certificates \
  alsa-utils
```

### 编译

```bash
cd /Users/wuzikang/Desktop/py/dify_stream_test/rust_client
cargo build --release --features native-webrtc
```

### 运行时建议

```bash
sudo apt install -y libasound2 libssl3 libudev1 ca-certificates alsa-utils
```

### 启动脚本示例

```bash
#!/usr/bin/env bash
set -euo pipefail
DIR="$(cd "$(dirname "$0")" && pwd)"
: "${ROBOT_ID:?ROBOT_ID is required}"
export ASR_DING_SOUND_PATH="${ASR_DING_SOUND_PATH:-$DIR/assets/wake_audio/ding.wav}"
exec "$DIR/rust_client" "$@"
```

如果系统里串口很多，建议也显式指定：

```bash
export WAKE_SERIAL_PORT=/dev/ttyUSB0
```

如果现场还有其他本地服务需要临时降低客户端播放音量，可以保留默认本地 ducking 控制端口，或在启动脚本中显式指定：

```bash
export AUDIO_CONTROL_HOST=127.0.0.1
export AUDIO_CONTROL_PORT=19994
export AUDIO_DUCK_DEFAULT_TTL_MS=15000
export AUDIO_DUCK_MAX_TTL_MS=60000
```

## Ubuntu 22.04 ARM64 / Jetson Nano

这版客户端不需要 CUDA / cuDNN / TensorRT，也不需要旧的软件唤醒动态库。

重点只剩两类：

- ARM64 的 Rust 二进制
- 本机可用的音频和串口设备

建议安装：

```bash
sudo apt update
sudo apt install -y \
  build-essential \
  pkg-config \
  libasound2-dev \
  libssl-dev \
  libudev-dev \
  libasound2 \
  libssl3 \
  libudev1 \
  ca-certificates \
  alsa-utils
```

首次部署建议先检查：

```bash
arecord -l
aplay -l
```

如果串口设备名不稳定，也建议通过环境变量固定：

```bash
WAKE_SERIAL_PORT=/dev/ttyUSB0 ./rust_client
```

### 三种打包方式

#### 1. Docker buildx dev 包

适合线上先跑调试版本，方便观察日志和定位问题：

```bash
cd /Users/wuzikang/Desktop/py/dify_stream_test/rust_client
make package-ubuntu22-arm64-dev
```

产物默认在：

```text
rust_client/dist/rust_client-ubuntu22-arm64-dev/
rust_client/dist/rust_client-ubuntu22-arm64-dev.tar.gz
```

#### 2. Docker buildx release 包

适合最终上线发布，编译更慢但运行性能更好：

```bash
cd /Users/wuzikang/Desktop/py/dify_stream_test/rust_client
make package-ubuntu22-arm64-release
```

产物默认在：

```text
rust_client/dist/rust_client-ubuntu22-arm64-release/
rust_client/dist/rust_client-ubuntu22-arm64-release.tar.gz
```

兼容旧命令：

```bash
make package-ubuntu22-arm64
```

等价于 `make package-ubuntu22-arm64-release`。

#### 3. 目标板本机打包

如果直接在 Ubuntu 22.04 ARM64 板子上打包，可以不用 Docker buildx：

```bash
cd /path/to/rust_client
make package-board-dev
# 或
make package-board-release
```

产物默认在：

```text
rust_client/dist/rust_client-board-dev.tar.gz
rust_client/dist/rust_client-board-release.tar.gz
```

这个包里会包含：

- `rust_client` 可执行文件
- `assets/wake_audio/ding.wav`
- `run.sh` 启动脚本

首次运行可能需要下载 `ubuntu:22.04` 镜像和 Rust 工具链，所以会比较慢。后续 Docker
缓存命中后会快很多。

`run.sh` 会要求显式设置 `ROBOT_ID`，并默认把 ASR 确认音路径指向包内
`assets/wake_audio/ding.wav`。唤醒、就绪、待机等提示语仍统一由 Gateway 下发事件并通过云端 TTS 播放。典型启动方式：

```bash
cd rust_client-ubuntu22-arm64
ROBOT_ID=companion_01 \
ROBOT_SECRET=your-secret \
GATEWAY_URL=ws://${SERVER_IP}:8282/ws \
WAKE_SERIAL_PORT=/dev/ttyUSB0 \
./run.sh
```

如果需要固定 Rust 工具链版本，可以指定：

```bash
make package-ubuntu22-arm64-release RUST_VERSION=1.87.0
```

## 环境变量

常用的唤醒相关环境变量：

- `WAKE_SOURCE_MODE`
- `WAKE_SERIAL_PORT`
- `WAKE_SERIAL_BAUD`
- `WAKE_SERIAL_TIMEOUT`
- `WAKE_SERIAL_RECONNECT_SEC`
- `WAKE_DEBOUNCE_SEC`
- `WAKE_HTTP_ENABLED`
- `WAKE_HTTP_HOST`
- `WAKE_HTTP_PORT`
- `WAKE_HTTP_PATH`
- `AUDIO_FRONTEND_MODE`，默认 `cpal`；设为 `tcp` 时麦克风和播放都走本机 C++ `audio_frontend`
- `AUDIO_FRONTEND_HOST`，默认 `127.0.0.1`
- `AUDIO_FRONTEND_MIC_PORT`，默认 `39001`
- `AUDIO_FRONTEND_SPEAKER_PORT`，默认 `39002`
- `AUDIO_FRONTEND_CONTROL_PORT`，默认 `39003`；可选控制通道，通知 C++ 当前播放状态
- `AUDIO_FRONTEND_MUTE_MIC_DURING_PLAYBACK`，默认 `false`；显式设为 `true` 时才在 TTS 播放期间请求 C++ 暂停 mic 输出，默认保留 mic 帧给后续 AEC
- `run.audio_frontend_tcp.sh` 强制使用 `AUDIO_FRONTEND_MODE=tcp`，默认设置 `WAKE_SOURCE_MODE=kws`，通过本机 C++ `audio_frontend` KWS event TCP 唤醒；如需同时接受硬件/HTTP 唤醒，使用 `RUN_AUDIO_FRONTEND_WAKE_SOURCE_MODE=both`
- `KWS_WAKE_HOST`
- `KWS_WAKE_PORT`
- `KWS_WAKE_RECONNECT_SEC`
- `ASR_DING_SOUND_PATH`
- `OPUS_BITRATE_BPS`，默认 `16000`
- `TTS_PREBUFFER_SEC`，默认 `0.10`
- `PAUSE_CAPTURE_WHILE_WAITING`，默认 `true`；如现场设备重开麦克风异常，可临时设为 `false`
- `WAKE_CAPTURE_PREBUFFER_MS`，默认 `800`；唤醒后录音触发前保留的前滚音频，设为 `0` 可关闭
- `ROBOT_ID`
- `ROBOT_SECRET`
- `GATEWAY_URL`

如果不设置 `WAKE_SERIAL_PORT`，客户端会自动扫描常见 USB 串口。

本地音频 ducking 控制相关环境变量：

- `AUDIO_CONTROL_HOST`
- `AUDIO_CONTROL_PORT`
- `AUDIO_DUCK_DEFAULT_TTL_MS`
- `AUDIO_DUCK_MAX_TTL_MS`

## 跨平台编译

可以做，但这版项目更适合“目标平台本机构建”。

原因是这些本地依赖仍然要和目标平台一致：

- `cpal` 对应的音频后端
- `native-tls` 对应的系统 TLS
- `serialport` 对应的串口访问能力

所以如果只是发到：

- macOS
- Ubuntu x86_64
- Ubuntu ARM64 / Nano

最稳妥的方式仍然是各平台本地编译后打包。
