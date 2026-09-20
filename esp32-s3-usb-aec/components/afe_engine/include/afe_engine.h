#pragma once

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#include "esp_err.h"

#ifdef __cplusplus
extern "C" {
#endif

typedef struct {
    uint32_t sample_rate_hz;
    uint8_t mic_channels;
    uint8_t ref_channels;
    uint8_t high_perf_mode;
    uint8_t enable_aec;
    uint8_t enable_ns;
    uint8_t enable_vad;
    uint8_t enable_agc;
} afe_engine_config_t;

typedef struct {
    uint32_t feed_count;
    uint32_t fetch_count;
    uint32_t timeout_count;
    uint32_t vad_speech_count;
    uint32_t vad_silence_count;
    uint8_t vad_last_state;
} afe_engine_stats_t;

esp_err_t afe_engine_init(const afe_engine_config_t *cfg);
esp_err_t afe_engine_deinit(void);
esp_err_t afe_engine_feed(const int16_t *mic, size_t mic_samples, const int16_t *ref,
                          size_t ref_samples, uint32_t frame_seq);
esp_err_t afe_engine_fetch(int16_t *out, size_t out_capacity, size_t *valid_samples_out,
                           uint32_t *frame_seq_out);
bool afe_engine_get_aec_enabled(void);
afe_engine_stats_t afe_engine_get_stats(void);

#ifdef __cplusplus
}
#endif
