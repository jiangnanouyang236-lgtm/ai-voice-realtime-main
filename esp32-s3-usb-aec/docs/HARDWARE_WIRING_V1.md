# 硬件接线（当前基线）

目标硬件：`ESP32-S3 + INMP441 + MAX98357A`

## 1. I2S 麦克风（INMP441）

- `SCK -> GPIO5`
- `WS  -> GPIO4`
- `SD  -> GPIO6`
- `VDD -> 3.3V`
- `GND -> GND`
- `L/R -> GND` 或 `3.3V`（单麦任一都可，建议固定后不再改）

## 2. I2S 功放（MAX98357A）

- `BCLK -> GPIO15`
- `LRC  -> GPIO16`
- `DIN  -> GPIO7`
- `SD   -> 3.3V`（常开）
- `GAIN -> GND`
- `VIN  -> 3.3V`（更大音量可按模块规格改高电压）
- `GND  -> GND`
- 喇叭接 `SPK+ / SPK-`

## 3. 关键要求

- 麦克风、功放、开发板必须共地。
- 先确认单麦 + 单喇叭稳定，再增加其它外设。
- USB 线建议稳定供电，避免劣质线导致噪声和掉线。

## 4. 默认软件对应

- `CONFIG_AUDIO_MIC_CHANNELS=1`
- `CONFIG_UAC_SPEAKER_CHANNEL_NUM=1`
- `CONFIG_UAC_MIC_CHANNEL_NUM=1`
