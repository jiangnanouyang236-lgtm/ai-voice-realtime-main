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
    uint8_t bits_per_sample;
    uint8_t channels;
} audio_i2s_out_config_t;

typedef struct {
    uint32_t total_write_calls;
    uint32_t total_write_timeouts;
    uint32_t total_write_failures;
    uint32_t total_zero_write_calls;
    uint32_t total_partial_write_calls;
    uint32_t total_repeated_frame_events;
    uint32_t total_short_write_samples;
    uint32_t last_frame_seq;
    uint32_t last_frame_avg_abs;
    uint32_t last_frame_peak;
    uint32_t window_write_calls;
    uint32_t window_write_timeouts;
    uint32_t window_write_failures;
    uint32_t window_zero_write_calls;
    uint32_t window_partial_write_calls;
    uint32_t window_repeated_frame_events;
    uint32_t window_repeat_run_max;
    uint32_t window_max_write_us;
    uint32_t window_max_chunk_us;
    uint32_t window_max_zero_retries;
    uint32_t window_max_frame_avg_abs;
    uint32_t window_max_frame_peak;
    uint32_t window_max_short_write_samples;
} audio_i2s_out_stats_t;

esp_err_t audio_i2s_out_init(const audio_i2s_out_config_t *cfg);
esp_err_t audio_i2s_out_start(void);
esp_err_t audio_i2s_out_stop(void);
esp_err_t audio_i2s_out_write(const int16_t *src, size_t samples, uint32_t frame_seq,
                              size_t *written_samples);
audio_i2s_out_stats_t audio_i2s_out_get_stats(bool reset_window);

#ifdef __cplusplus
}
#endif
