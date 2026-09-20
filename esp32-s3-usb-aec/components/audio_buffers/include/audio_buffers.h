#pragma once

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#include "esp_err.h"

#ifdef __cplusplus
extern "C" {
#endif

typedef struct {
    size_t samples_per_frame;
    uint8_t mic_channels;
    uint8_t ref_channels;
} audio_buffers_config_t;

typedef struct {
    uint32_t mic_pushed;
    uint32_t ref_pushed;
    uint32_t aligned_popped;
    uint32_t drop_count;
} audio_buffers_stats_t;

esp_err_t audio_buffers_init(const audio_buffers_config_t *cfg);
esp_err_t audio_buffers_deinit(void);

esp_err_t audio_buffers_push_mic(const int16_t *data, size_t samples, uint32_t frame_seq);
esp_err_t audio_buffers_push_ref(const int16_t *data, size_t samples, uint32_t frame_seq);

esp_err_t audio_buffers_pop_aligned(int16_t *mic_out, size_t mic_samples,
                                    int16_t *ref_out, size_t ref_samples,
                                    uint32_t *frame_seq_out);

audio_buffers_stats_t audio_buffers_get_stats(void);

#ifdef __cplusplus
}
#endif
