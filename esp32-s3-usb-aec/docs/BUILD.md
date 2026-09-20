# 构建指南

## 环境

- ESP-IDF 5.5.3
- 目标：ESP32-S3
- CMake / Ninja 由 ESP-IDF 环境提供

## 首次构建

```bash
cd esp32-s3-usb-aec

export IDF_PYTHON_ENV_PATH="$HOME/.espressif/python_env/idf5.5_py3.13_env"
. "$HOME/.espressif/v5.5.3/esp-idf/export.sh"
export IDF_TARGET=esp32s3

idf.py build
```

`sdkconfig` 是生成文件，不提交。产品真值来自：

- `sdkconfig.defaults`
- `main/Kconfig.projbuild`
- `partitions.csv`
- `dependencies.lock`

## 干净构建验证

依赖、Kconfig、分区或本地 UAC fork 发生变化后，执行：

```bash
rm -rf build managed_components sdkconfig
export IDF_TARGET=esp32s3
idf.py build
```

构建必须只使用 `components/usb_device_uac_fork`，不得重新引入 registry
版本的 `espressif/usb_device_uac`。

## 烧录

```bash
idf.py -p /dev/cu.usbmodemXXXX flash monitor
```

退出 monitor：`Ctrl+]`。

## 主要产物

- `build/esp32_s3_usb_aec.bin`
- `build/bootloader/bootloader.bin`
- `build/partition_table/partition-table.bin`
- `build/srmodels/srmodels.bin`

构建成功只证明固件可生成，不能代替 USB 枚举、全双工、AEC 和长稳真机测试。
