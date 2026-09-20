#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BUILD_DIR="${ROOT}/build-verify"
SDKCONFIG="${ROOT}/sdkconfig.verify"

command -v idf.py >/dev/null || {
  echo "idf.py not found; load ESP-IDF 5.5.3 first" >&2
  exit 1
}

export IDF_TARGET=esp32s3
rm -rf "${BUILD_DIR}" "${SDKCONFIG}"
idf.py -B "${BUILD_DIR}" -DSDKCONFIG="${SDKCONFIG}" build

grep -q '^CONFIG_SPIRAM=y$' "${SDKCONFIG}"
grep -q '^CONFIG_ESP_DEFAULT_CPU_FREQ_MHZ=240$' "${SDKCONFIG}"
grep -q '^CONFIG_PARTITION_TABLE_CUSTOM=y$' "${SDKCONFIG}"
grep -q '^CONFIG_AUDIO_MIC_CHANNELS=1$' "${SDKCONFIG}"
grep -q '^CONFIG_AUDIO_USB_MIC_GAIN_DB=0$' "${SDKCONFIG}"
grep -q '^CONFIG_UAC_SAMPLE_RATE=16000$' "${SDKCONFIG}"

NM="$(command -v xtensa-esp32s3-elf-nm)"
ELF="${BUILD_DIR}/esp32_s3_usb_aec.elf"

test "$("${NM}" -g "${ELF}" | grep -c ' uac_device_recover_streams$')" -eq 1
test "$("${NM}" -g "${ELF}" | grep -c ' tud_descriptor_device_cb$')" -eq 1

if grep -R -n -E 'config_manager|uart_cmd|speaker_volume' \
  "${ROOT}/main" "${ROOT}/components" --exclude-dir=managed_components; then
  echo "removed USB/UART configuration code is still referenced" >&2
  exit 1
fi

if grep -q -E 'espressif/usb_device_uac|espressif__usb_device_uac' \
  "${ROOT}/main/idf_component.yml" "${ROOT}/dependencies.lock"; then
  echo "registry usb_device_uac unexpectedly reintroduced" >&2
  exit 1
fi

echo "ESP32-S3 USB AEC build verification passed"
