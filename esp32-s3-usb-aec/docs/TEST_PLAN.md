# 真机测试计划

构建通过不能证明 USB、音频或 AEC 正确。每个发布候选固件至少完成以下测试。

## 1. 枚举

- 当前阶段以 macOS 为唯一验收平台，Linux、Windows 后续单独验证；
- 同时出现一个 16 kHz mono speaker 和一个 16 kHz mono microphone；
- 系统音量和静音控制能够改变实际播放；
- 连续插拔 20 次，无枚举失败或必须重启才能恢复。

## 2. 纯播放

- 连续播放语音和音乐各 30 分钟；
- `I2S write timeout/failure` 为 0；
- speaker buffer 不持续向单侧漂移；
- 无爆音、重复帧、周期性卡顿和暂停后残留尾音。

## 3. 纯录音

- 连续录音 30 分钟；
- 录制时长与墙钟误差小于 0.1%；
- 无周期性 352-sample 补零、持续 overflow 或不连续跳帧；
- 0 dB 数字增益下记录 RMS、peak、clip 和 noise floor。

## 4. 全双工和 AEC

- 固定扬声器、麦克风距离和系统音量；
- 同一段 far-end 语音分别测试 AEC on/off 固件；
- 保存原始 mic、reference 和 AEC output；
- 比较 ERLE、残余回声、近讲失真和 double-talk 表现；
- AEC 必须明显优于 bypass，不能只依据日志中的 `aec=1`。

诊断用 AEC-off 配置位于 `tests/profiles/aec_off.defaults`。它只用于同一设备、
同一摆位和同一音量下的 A/B，不得作为交付固件。测试结束后必须重新刷入默认构建，
并从启动日志确认 `aec=1` 和 `AEC(VOIP_HIGH_PERF)`。

### 2026-07-13 macOS 真机烟测基线

- 设备：ESP32-S3 rev v0.2，8 MB PSRAM；
- USB：`wzk-speaker` + `wzk-mic`，16 kHz mono；
- 自动短时基线 5/5 通过；
- 10 秒全双工：host input/output overflow/underflow 均为 0；
- 确定性宽带 A/B 中，AEC-off 相关性约 `0.0024`，当前摆位没有形成足够的
  可测回声，因此正式 AEC 效果仍标记为“未完成”；
- 工具会用 `observable_echo` 门槛拒绝把这种低相关性误判为 AEC 通过。
- 已移除播放路径中旧有的固定 `-18 dB` 隐藏衰减，恢复 host 100% 对应 PCM
  unity gain；修正后 AEC-off 多档标定最高相关性仍仅约 `0.0088`，下一步检查
  MAX98357A、扬声器接线和实际声学路径。

当前结果证明 USB 音频短时链路稳定，但不等同于正式 ERLE、double-talk 和近讲
失真验收；正式发布仍必须先建立有足够声学耦合的固定夹具，再执行原始音频保存与
真实语音 A/B。

## 5. 恢复和长稳

- host pause/resume、系统睡眠恢复、USB suspend/resume；
- 播放中拔插、录音中拔插、同时录放中拔插；
- 8 小时全双工 soak test；
- 记录 underrun、overflow、drop、recover、stack watermark 和最小空闲内存；
- 不允许出现持续增长且无法自行恢复的计数或队列水位。

## 6. NS / AGC 准入

基础 AEC 全部通过后，分别构建：

1. AEC only；
2. AEC + NS；
3. AEC + NS + AGC。

只有在 I2S/USB deadline、AEC 效果、CPU、internal heap、PSRAM 和 8 小时长稳
均不退化时才允许默认开启。AGC 还必须验证无削波、呼吸感和噪声抬升。
