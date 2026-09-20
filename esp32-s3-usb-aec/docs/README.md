# ESP32-S3 USB AEC Audio

本固件只实现一个确定性的 USB 音频协处理器：

- Host 将设备免驱识别为 USB Speaker 和 USB Microphone；
- USB Speaker PCM 经 ESP32-S3 输出到 MAX98357A；
- INMP441 麦克风经 ESP-SR AEC 后上传到 USB Microphone；
- 采样格式固定为 PCM16、16 kHz、单声道、全双工。

## 当前产品边界

当前阶段必须完成：

1. USB speaker 稳定播放；
2. USB microphone 连续录音且时基正确；
3. ESP-SR AEC 确实生效；
4. 同时录放、USB suspend/resume 和长时间运行稳定。

当前阶段不实现：

- UART/USB 上下位机参数配置；
- Wi-Fi、WebSocket、WebRTC、OTA；
- ASR、LLM、TTS 或会话状态；
- KWS、VAD；
- 双麦或麦克风阵列；
- NS、AGC 默认启用。

操作系统标准 UAC 音量和静音控制仍然保留，它们属于 USB 声卡协议，
不是上下位机参数配置。

## 固定基线

- ESP-IDF：5.5.3
- 芯片：ESP32-S3，240 MHz，Octal PSRAM
- 麦克风：单 INMP441
- 扬声器：单 MAX98357A
- USB：Speaker 1ch + Microphone 1ch
- AFE：ESP-SR high-performance mode
- AEC：开启；ESP-SR 初始化失败时拒绝启动，不静默降级
- NS / AGC / VAD / WakeNet：关闭
- 麦克风数字增益：0 dB
- AEC reference delay：当前基线 1 帧，最终值必须真机标定

## 数据流

```text
Host USB Speaker
  -> bounded speaker buffer
  -> drift compensation / ASRC
  -> host volume and mute
  -> I2S TX -> MAX98357A
  -> actual playout PCM -> AEC reference

INMP441 -> I2S RX
  -> PCM16 / 16 kHz / mono
  -> ESP-SR AEC
  -> valid samples only
  -> USB Microphone -> Host
```

## 验收顺序

当前先以 macOS 为唯一验收平台：

1. 每次候选固件先通过自动短时基线；
2. 纯播放 30 分钟，无爆音、无持续 underrun；
3. 纯录音 30 分钟，无时基膨胀、削波或持续 drop；
4. 同时录放，确认 AEC 明显优于 bypass；
5. USB pause/resume、休眠恢复和重新枚举；
6. 8 小时全双工 soak test；
7. 基线稳定后，分别评估 `AEC + NS` 和 `AEC + NS + AGC`。

macOS 全部通过后，再开始 Linux、Windows 兼容性验证。

构建方式见 [BUILD.md](./BUILD.md)，接线见
[HARDWARE_WIRING_V1.md](./HARDWARE_WIRING_V1.md)，自动测试见
[MACOS_TESTING.md](./MACOS_TESTING.md)。
