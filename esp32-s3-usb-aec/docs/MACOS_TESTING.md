# macOS 自动测试

`scripts/macos_audio_test.py` 用于生成可重复的短时 USB 音频基线。它不会替代
30 分钟稳定性、8 小时长稳、真实语音 AEC 和 double-talk 验收。

## 环境

建议使用独立虚拟环境，不向 ESP-IDF Python 环境安装音频依赖：

```bash
cd esp32-s3-usb-aec
python3 -m venv .venv-macos-test
. .venv-macos-test/bin/activate
python -m pip install -r scripts/requirements-macos-test.txt
```

如果 `sounddevice` 无法加载 PortAudio，再安装系统依赖：

```bash
brew install portaudio
```

## 正式固件短时基线

确认系统中存在 `wzk-speaker` 和 `wzk-mic`，然后执行：

```bash
python scripts/macos_audio_test.py run --label aec-on
```

一次默认运行约 18 秒，依次执行：

1. CoreAudio 枚举与 16 kHz mono 格式检查；
2. 3 秒静音录音；
3. 3 秒确定性宽带测试信号纯播放；
4. 10 秒确定性宽带测试信号全双工；
5. 关闭麦克风流，等待 1 秒后重新录音 1 秒。

产物写入 `test-results/<时间>-<label>/`：

- `report.json`：设备、配置、PCM 指标、PortAudio 状态和总结果；
- `idle_mic.wav`：静音底噪；
- `duplex_reference.wav`：发送到 USB Speaker 的参考信号；
- `duplex_mic.wav`：USB Microphone 返回信号；
- `reopen_mic.wav`：关闭、重开后的首段音频。

报告中的 `projected_residual_db` 和相关性只用于同一摆位、同一系统音量下的
A/B。单份报告不能独立证明 AEC 有效。

## 可选固件日志

只有确认串口后才传入，脚本不会自动猜测串口：

```bash
python scripts/macos_audio_test.py run \
  --label aec-on \
  --serial-port /dev/cu.usbmodemXXXX
```

串口内容保存为同目录下的 `firmware.log`。串口不可用会使本次报告失败，但已完成的
音频结果仍会保留。不要同时运行 `idf.py monitor`。

## AEC on/off A/B

保持设备位置、系统音量和环境不变。先保存正式固件报告，再构建并刷入诊断固件：

```bash
export IDF_TARGET=esp32s3
idf.py -B build-aec-off \
  -DSDKCONFIG=sdkconfig.aec-off \
  -DSDKCONFIG_DEFAULTS='sdkconfig.defaults;tests/profiles/aec_off.defaults' \
  build
idf.py -B build-aec-off -p /dev/cu.usbmodemXXXX app-flash

python scripts/macos_audio_test.py run --label aec-off
```

比较两个报告。默认要求 AEC-off 相关性至少为 0.02，证明测试摆位确实采到了
可测回声；在此前提下，投影残留还必须至少改善 6 dB：

```bash
python scripts/macos_audio_test.py compare \
  --aec-on test-results/<aec-on>/report.json \
  --aec-off test-results/<aec-off>/report.json \
  --output test-results/aec-comparison.json
```

测试结束后必须恢复正式固件：

```bash
idf.py -B build-clean -DSDKCONFIG=sdkconfig.clean \
  -p /dev/cu.usbmodemXXXX app-flash monitor
```

启动日志必须再次出现 `aec=1` 和 `AEC(VOIP_HIGH_PERF)`。

## 声学耦合标定

如果 A/B 返回 `observable_echo=false`，保持 AEC-off 固件，先运行多档位标定：

```bash
python scripts/macos_audio_test.py coupling --confirm-aec-off
```

默认依次测试 `0.01、0.02、0.04、0.08` 四个数字峰值，每档 5 秒，并保存各档
reference、microphone WAV 和 `coupling-report.json`。至少一个档位的绝对相关性达到
0.02 才说明当前扬声器音量和物理摆位足以开展 AEC A/B。

如果最高档仍不可测，应依次检查：

1. MAX98357A 是否实际发声，而不仅是 USB Speaker 收到了数据；
2. macOS 对 `wzk-speaker` 的设备音量和静音状态；
3. 扬声器与 INMP441 的距离、朝向和外壳遮挡；
4. I2S 功放供电、增益脚和扬声器接线。

标定结束同样必须恢复正式 AEC 固件。

播放链使用标准 UAC 音量映射：host 100% 对应 PCM unity gain。固件不会再隐藏
固定的 -18 dB 衰减；最大声压应通过 macOS 设备音量和 MAX98357A GAIN 接线控制。

## 当前短时通过条件

- 恰好找到一个指定名称的输入和输出设备；
- 两端都是 16 kHz、单声道；
- PortAudio input/output overflow/underflow 均为 0；
- 麦克风数据不是全零且没有削波；
- 麦克风关闭、重新打开后仍能正常采集。

底噪、AEC、延迟和音质指标先记录基线，不在单次测试中使用随意阈值判定。
如果比较结果为 `observable_echo=false`，应调整扬声器音量、麦克风距离或测试夹具，
不能把低相关性解释为 AEC 效果好。
