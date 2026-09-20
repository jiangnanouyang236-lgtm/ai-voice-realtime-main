#include "esp_err.h"
#include "esp_log.h"
#include "sdkconfig.h"

#include "audio_pipeline.h"
#if CONFIG_AUDIO_USB_UAC_ENABLE
#include "usb_audio_bridge.h"
#endif
static const char *TAG = "main";

void app_main(void) {
    ESP_LOGI(TAG, "ESP32-S3 USB AEC audio device booting");

    ESP_ERROR_CHECK(audio_pipeline_init());
#if CONFIG_AUDIO_USB_UAC_ENABLE
    ESP_ERROR_CHECK(usb_audio_bridge_init());
#endif
    ESP_ERROR_CHECK(audio_pipeline_start());

    ESP_LOGI(TAG, "audio pipeline started");
}
