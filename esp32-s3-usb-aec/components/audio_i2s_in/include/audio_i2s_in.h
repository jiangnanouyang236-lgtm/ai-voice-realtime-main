#pragma once

#include <stddef.h>
#include <stdint.h>

#include "esp_err.h"

#ifdef __cplusplus
extern "C" {
#endif

typedef struct {
    uint32_t sample_rate_hz;
    uint8_t bits_per_sample;
    uint8_t channels;
} audio_i2s_in_config_t;

esp_err_t audio_i2s_in_init(const audio_i2s_in_config_t *cfg);
esp_err_t audio_i2s_in_start(void);
esp_err_t audio_i2s_in_stop(void);
esp_err_t audio_i2s_in_read(int16_t *dst, size_t samples, uint32_t timeout_ms,
                            size_t *samples_read);

#ifdef __cplusplus
}
#endif
