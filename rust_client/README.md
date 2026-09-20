# Rust Client

`rust_client/` 是当前的上线客户端实现。
v3.0 起端侧只支持 Rust 客户端；Python Gateway 仍是云端编排服务，不属于被移除的客户端。

统一交互状态规则见：

- [docs/decisions.md](/Users/wuzikang/Desktop/py/dify_stream_test/docs/decisions.md)

## 当前能力

- 硬件串口唤醒
- 本地 HTTP 唤醒入口
- audio_frontend KWS TCP 唤醒入口
- WebRTC VAD 自动录音
- Gateway WebSocket 对话
- 本地 TTS 播放
- TTS 播放期间的唤醒事件打断
- 实验性 AEC 说话打断开关，默认关闭
- 本地音频 ducking 控制接口

图片链路使用独立的 `vision-v1` WebRTC DataChannel。只有通道打开且发送队列
没有积压时，RustClient 才会扫描并领取图片，因此断网或 WebRTC 未完成时不会
读取、删除图片。当前约束为：

- 生产者先写同目录临时文件，再原子重命名为 `pic.jpeg`
- RustClient 成功占用后读取，处理完成或判定无效后删除该次输入
- 已是 `640×360` 且不超过 `100KB` 的 JPEG 直接透传，不做二次压缩
- 其他 JPEG/PNG 等比缩放并留边为 `640×360`，再编码为不超过 `100KB` 的 JPEG
- 每片最大 32KB，图片通道允许乱序且不重传；图片丢失时等待下一张
- 只面向“这是什么、前面是什么、你看到了什么”类场景理解，不承诺 OCR 或细节识别

当前已经移除：

- 软件唤醒
- 旧模型目录依赖

## 核心 Rust 依赖

当前主要依赖：

- `cpal`
- `hound`
- `rubato`
- `webrtc-vad`
- `serialport`
- `tokio`
- `tokio-tungstenite`
- `serde`
- `serde_json`
- `tracing`
- `anyhow`
- `bytes`
- `crossbeam`
- `image`，用于图片尺寸归一化和 JPEG/PNG 编解码
- `webrtc`，可选依赖，仅在 `native-webrtc` feature 下启用

依赖清单见 [rust_client/Cargo.toml](/Users/wuzikang/Desktop/py/dify_stream_test/rust_client/Cargo.toml)。

## 启动

v2.0 / 当前纯 WebSocket 稳定路径，最推荐的启动方式是显式指定唤醒串口：

```bash
cd /Users/wuzikang/Desktop/py/dify_stream_test/rust_client
ROBOT_ID=companion_01 WAKE_SERIAL_PORT=/dev/cu.usbserial-1110 cargo run --bin rust_client
```

如果不设置 `WAKE_SERIAL_PORT`，客户端会自动发现一个候选 USB 串口，但部署时更建议固定端口。
接入 `server-config` 后建议同时显式设置 `ROBOT_ID`；如果 Gateway 开启强认证，还需要设置后台生成的 `ROBOT_SECRET`。

v3.0 WebRTC-first 路径连接 Go Voice Gateway，音频上行走 RTP-only：

```bash
cd /Users/wuzikang/Desktop/py/dify_stream_test/rust_client
GATEWAY_URL=ws://${SERVER_IP}:8282/ws \
ROBOT_ID=companion_01 \
WAKE_SERIAL_PORT=/dev/cu.usbserial-1110 \
TRANSPORT_POLICY=webrtc_only \
WEBRTC_ENABLED=true \
TRANSPORT_FALLBACK_ENABLED=false \
WEBRTC_OFFER_FACTORY=native \
RTC_AUDIO_UPLINK=rtp_only \
cargo run --bin rust_client --features native-webrtc --release
```

不接麦克风和串口时，可用仓库根目录的 smoke 脚本验证同一条 Rust native WebRTC transport：

```bash
python scripts/smoke_go_webrtc_python_gateway.py --sample "$SAMPLE_WAV" --client rust
```

没有串口硬件时，可以用本地 HTTP 触发同一套唤醒状态机：

```bash
curl -X POST http://127.0.0.1:5202/wakeup
```

如果需要从局域网另一台机器触发，把客户端机器上的 `WAKE_HTTP_HOST` 显式设为 `0.0.0.0`，再访问 `http://<客户端局域网IP>:5202/wakeup`。

本地 C++ `audio_frontend` TCP/AEC 主线测试可以使用已跟踪的启动脚本：

```bash
cd /Users/wuzikang/Desktop/py/dify_stream_test/rust_client
./run.audio_frontend_tcp.sh
```

脚本强制使用 `AUDIO_FRONTEND_MODE=tcp`，并默认使用 `AUDIO_FRONTEND_MUTE_MIC_DURING_PLAYBACK=false`、
`TTS_BARGE_IN_WITH_AEC=false`、`WAKE_CAPTURE_PREBUFFER_MS=800` 和
`WAKE_SOURCE_MODE=kws`。敏感配置不要写进脚本；
如需 `ROBOT_SECRET`，放到未提交的 `rust_client/.env.local`，或在父 shell 中 export。
可以从示例文件开始：

```bash
cp .env.local.example .env.local
```

使用 `audio_frontend` 本地 KWS 唤醒时，先让 C++ 侧启用 `--enable-kws-event-tcp`，
RustClient 脚本默认会打开 KWS 唤醒源：

```bash
./run.audio_frontend_tcp.sh
```

迁移期也可以用 `RUN_AUDIO_FRONTEND_WAKE_SOURCE_MODE=both` 同时接受硬件/HTTP 唤醒和 KWS 唤醒。
所有唤醒源都会经过同一个 `WAKE_DEBOUNCE_SEC`，默认 `0.5` 秒，避免硬件和 KWS
在同一次唤醒中重复触发状态机。

## 常用环境变量

查看每个配置字段对应哪个环境变量：

```bash
cargo run --bin rust_client --features native-webrtc -- --print-env-help
```

查看当前实际生效配置（密钥会被隐藏为 `<set>`）：

```bash
ROBOT_ID=companion_01 ROBOT_SECRET="$ROBOT_SECRET" \
cargo run --bin rust_client --features native-webrtc -- --print-config
```

### 唤醒

- `WAKE_SERIAL_PORT`
- `WAKE_SOURCE_MODE`：代码默认 `hardware`；`run.audio_frontend_tcp.sh` 会设置为 `kws`。可选 `hardware`、`kws`、`both`。
- `RUN_AUDIO_FRONTEND_WAKE_SOURCE_MODE`：仅用于 `run.audio_frontend_tcp.sh`，默认 `kws`；设为 `both` 可同时接受硬件/HTTP 唤醒和 KWS 唤醒。
- `WAKE_SERIAL_BAUD`
- `WAKE_SERIAL_TIMEOUT`
- `WAKE_SERIAL_RECONNECT_SEC`
- `WAKE_DEBOUNCE_SEC`
- `WAKE_HTTP_ENABLED`：默认 `true`。
- `WAKE_HTTP_HOST`：默认 `127.0.0.1`；需要局域网触发时设为 `0.0.0.0`。
- `WAKE_HTTP_PORT`：默认 `5202`。
- `WAKE_HTTP_PATH`：默认 `/wakeup`。
- `KWS_WAKE_HOST`：默认 `127.0.0.1`。
- `KWS_WAKE_PORT`：默认 `39004`。
- `KWS_WAKE_RECONNECT_SEC`：默认 `1.0`。

### 本地反馈音

- `ASR_DING_SOUND_PATH`：ASR 返回有效文本后播放的确认音，默认使用包内 `assets/wake_audio/ding.wav`

### Gateway

- `GATEWAY_URL`
- `GATEWAY_SCHEME`
- `GATEWAY_HOST`
- `GATEWAY_PORT`
- `GATEWAY_PATH`
- `ROBOT_ID`
- `ROBOT_SECRET`
- `BOT_ID`
- `HEARTBEAT_INTERVAL`

### Transport / WebRTC

- `TRANSPORT_POLICY`：默认 `webrtc_only`；v3.0 默认只走 WebRTC。需要本地诊断旧链路时才显式设为 `auto_prefer_webrtc` 或 `websocket_only`，线上主线不使用 WS audio fallback。
- `WEBRTC_ENABLED`：默认 `true`；客户端会接收 Gateway 的 `rtc_config` 并尝试创建本地 offer。
- `WEBRTC_CONNECT_TIMEOUT_MS`：WebRTC signaling 连接超时，默认 `15000`。
- `TRANSPORT_FALLBACK_ENABLED`：默认 `false`；WebRTC 不可用时直接暴露错误，避免静默回到旧 WS 音频链路。该开关只作为诊断/过渡开关保留。
- `RTC_AUDIO_UPLINK`：默认 `rtp_only`；native RTP 成功即不再发送 WebSocket 音频，如果 RTP track 未就绪会直接报错。
- `WEBRTC_OFFER_FACTORY`：默认 `native`，使用 Rust 原生 WebRTC offer factory。需要用 `cargo run --bin rust_client --features native-webrtc` 或同等 feature build 启动。
- `WEBRTC_NATIVE_WEBRTC_ENABLED`：也可设为 `true` 启用 Rust 原生 WebRTC offer factory。
- `WEBRTC_NATIVE_GATHER_TIMEOUT_MS`：原生 WebRTC 等待 ICE gathering 完成的时间，默认 `5000`。
- `WEBRTC_NATIVE_ICE_NETWORK_TYPES`：原生 WebRTC candidate 网络类型，默认 `udp4`；确认目标网络 IPv6 稳定后可设为 `udp4,udp6`。ICE servers 从 Go Gateway 下发；当前 Rust native 实际保留 STUN/TURN UDP，并会在日志里标出被跳过的 TURN TCP/TLS URL。
- `WEBRTC_OFFER_HELPER`：可选外部 offer helper 命令。客户端会把 `rtc_config` JSON 写入 helper stdin，并要求 helper stdout 返回 `{"session_id":"...","sdp":"..."}`。
- `WEBRTC_OFFER_HELPER_TIMEOUT_MS`：offer helper 超时，默认 `5000`。

v3.0 默认构建启用 `native-webrtc` feature。`native-webrtc` feature 使用 `webrtc 0.17.1`；不要直接升级到 `0.20.0-beta.2`，该 beta 依赖在当前稳定 Rust 下验证过不可编译。
`WEBRTC_OFFER_HELPER` 会作为本机 shell 命令执行，只应配置可信的本地 helper 或脚本。

### 图片快照

- `VISION_SNAPSHOT_ENABLED`：默认关闭；需要上传图片时显式设为 `true`
- `VISION_SNAPSHOT_PATH`
- `VISION_SNAPSHOT_SCAN_INTERVAL_MS`

正式输入文件固定为 JPEG，文件名固定为 `pic.jpeg`。PNG 解码仅用于兼容和测试，
不作为生产输入约定。扫描频率只决定发现文件的延迟；实际上传频率由生产者生成
新 `pic.jpeg` 的频率决定。

### Runtime

- `RESPONSE_TIMEOUT`：发送录音后等待首个服务端响应/音频的超时，默认 `45` 秒。当前本地/ZeroTier 测试里 LLM+TTS 首包偶尔超过 15 秒，过短会导致客户端先回到待机再播放迟到 TTS。
- `SESSION_TIMEOUT`：无交互后回到等待唤醒的时间，默认 `15` 秒。
- `TTS_PLAYBACK_TIMEOUT`：等待单轮 TTS 播放完成的超时，默认 `60` 秒。

### 音频设备

- `AUDIO_FRONTEND_MODE`，默认 `cpal`；设为 `tcp` 时麦克风和播放都走本机 C++ `audio_frontend`
- `AUDIO_FRONTEND_HOST`，默认 `127.0.0.1`
- `AUDIO_FRONTEND_MIC_PORT`，默认 `39001`
- `AUDIO_FRONTEND_SPEAKER_PORT`，默认 `39002`
- `AUDIO_FRONTEND_CONTROL_PORT`，默认 `39003`；可选控制通道，通知 C++ 当前播放状态
- `AUDIO_FRONTEND_MUTE_MIC_DURING_PLAYBACK`，默认 `false`；显式设为 `true` 时才在 TTS 播放期间请求 C++ 暂停 mic 输出，默认保留 mic 帧给后续 AEC
- `TTS_BARGE_IN_WITH_AEC`，默认 `false`；实验性说话打断开关。显式设为 `true` 且 `AUDIO_FRONTEND_MODE=tcp`、`AUDIO_FRONTEND_MUTE_MIC_DURING_PLAYBACK=false` 时，才允许 TTS 播放期间的 AEC 后麦克风语音触发打断。默认行为仍是只由唤醒事件打断 TTS
- `MIC_DEVICE_NAME`
- `SPEAKER_DEVICE_NAME`
- `PLAYBACK_FRAMES_PER_BUFFER`
- `PAUSE_CAPTURE_WHILE_WAITING`，默认 `true`；等待唤醒时暂停 RustClient 录音上行采集，设为 `false` 可回到采集常开模式
- `WAKE_CAPTURE_PREBUFFER_MS`，默认 `800`；唤醒后录音触发前保留的前滚音频，设为 `0` 可关闭

### VAD / 播放

- `VAD_MODE`，默认 `2`（WebRTC VAD aggressive）
- `VAD_SPEECH_THRESHOLD`，默认 `0.25` 秒；开始录音的判定窗口长度
- `VAD_SPEECH_TRIGGER_RATIO`，默认 `0.6`；窗口内至少60%的帧需要判为语音
- `VAD_SPEECH_START_MIN_DBFS`，默认 `-36`；VAD语音帧仍需达到的最低电平
- `VAD_SILENCE_THRESHOLD`，默认 `0.5` 秒；录音中的连续非语音结束阈值
- `VAD_MAX_RECORDING_DURATION`，默认 `10` 秒
- `TURN_GATE_SHADOW_ENABLED`，默认 `false`；开启后只发送 provisional candidate 元数据，
  不改变 `audio_end`
- `TURN_GATE_ACTIVE_ENABLED`，默认 `true`；candidate 仅在云端请求且本地
  VAD epoch/watermark/静音状态仍匹配时才提交；未收到正常反馈时 1000ms 安全回退。
  需要回退 Rust 端 Active 时显式设为 `false`
- `TURN_GATE_CANDIDATE_SILENCE_MS`，默认 `300`；每个静音周期产生一次 Shadow/Active candidate
- `TURN_GATE_MIN_COMMIT_SILENCE_MS`，默认 `600`；首次双 True 提前返回时继续观察本地稳定静音，
  用户恢复说话立即取消待提交请求，不执行第二次模型判断
- `TURN_GATE_FAILURE_FALLBACK_MS`，默认 `1000`；模型失败、超时或反馈丢失时结束当前轮次
- `TURN_GATE_AMBIGUOUS_HARD_TIMEOUT_MS`，默认 `1600`；任一模型明确判断未说完时继续听到该上限
- `OPUS_BITRATE_BPS`，默认 `16000`；Rust 上行和 Gateway 下行建议保持同值
- `TTS_PREBUFFER_SEC`，默认 `0.10`；网络抖动明显时可临时调到 `0.15`~`0.25`
- `VOLUME_GAIN`
- `AGC_TARGET_DB`

### 本地音频 Ducking 控制

- `AUDIO_CONTROL_HOST`，默认 `127.0.0.1`
- `AUDIO_CONTROL_PORT`，默认 `19994`
- `AUDIO_DUCK_DEFAULT_TTL_MS`，默认 `15000`
- `AUDIO_DUCK_MAX_TTL_MS`，默认 `60000`

接口示例：

```bash
curl -X POST http://127.0.0.1:19994/audio/duck \
  -H 'Content-Type: application/json' \
  -d '{"volume":0.2,"ttl_ms":15000}'

curl -X POST http://127.0.0.1:19994/audio/restore

curl http://127.0.0.1:19994/audio/duck
```

`volume` 是 Rust 客户端自身播放音量系数，不修改系统音量，也不影响麦克风、VAD 或 ASR。

## 构建

调试 / 测试版本，适合线上临时排查问题：

```bash
cd /Users/wuzikang/Desktop/py/dify_stream_test/rust_client
cargo build --features native-webrtc
# 或
make build-dev
```

Release 版本偏激进优化，编译会慢一些，但运行性能更好：

```bash
cd /Users/wuzikang/Desktop/py/dify_stream_test/rust_client
cargo build --release --features native-webrtc
# 或
make build-release
```

v3.0 Rust 原生 WebRTC offer factory：

```bash
cd /Users/wuzikang/Desktop/py/dify_stream_test/rust_client
GATEWAY_URL=ws://${SERVER_IP}:8282/ws \
WEBRTC_ENABLED=true \
TRANSPORT_POLICY=webrtc_only \
WEBRTC_OFFER_FACTORY=native \
RTC_AUDIO_UPLINK=rtp_only \
cargo run --bin rust_client --features native-webrtc
```

如果是在最终目标机器本机编译，并且不需要把二进制拷到不同 CPU 的机器上运行，可以进一步尝试：

```bash
RUSTFLAGS="-C target-cpu=native" cargo build --release --features native-webrtc
```

不要把 `target-cpu=native` 默认用于 Docker buildx 跨端包，因为 buildx 的构建 CPU 不一定等于真实部署机器 CPU。

常用打包命令：

```bash
# Docker buildx 跨端 dev 包
make package-ubuntu22-arm64-dev

# Docker buildx 跨端 release 包
make package-ubuntu22-arm64-release

# 在目标板本机打 dev/release 包
make package-board-dev
make package-board-release
```

## 部署说明

打包和平台依赖说明见：

- [rust_client/PACKAGING.md](/Users/wuzikang/Desktop/py/dify_stream_test/rust_client/PACKAGING.md)

## 说明

- Rust 客户端已经不再依赖软件唤醒模型，所以不需要模型目录和额外动态库。
- 如果现场环境里存在多个串口设备，部署时建议始终显式设置 `WAKE_SERIAL_PORT`。
- v3.0 起端侧只维护 Rust 客户端；需要快速链路验证时使用仓库根目录的 Go/Rust smoke 脚本。
