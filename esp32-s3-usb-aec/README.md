# esp32-s3-usb-aec

ESP32-S3 免驱 USB Speaker + USB Microphone + ESP-SR AEC 固件。

本项目只负责确定性的音频输入、输出和 AEC，不负责网络、Gateway、
ASR、LLM、TTS 或上下位机参数配置。

- 设计和验收边界：[docs/README.md](docs/README.md)
- 构建：[docs/BUILD.md](docs/BUILD.md)
- 接线：[docs/HARDWARE_WIRING_V1.md](docs/HARDWARE_WIRING_V1.md)
- 真机测试：[docs/TEST_PLAN.md](docs/TEST_PLAN.md)
- macOS 自动测试：[docs/MACOS_TESTING.md](docs/MACOS_TESTING.md)
- 本地 UAC fork：[components/usb_device_uac_fork/PATCHES.md](components/usb_device_uac_fork/PATCHES.md)
