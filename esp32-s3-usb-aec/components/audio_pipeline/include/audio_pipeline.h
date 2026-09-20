#pragma once

#include <stddef.h>
#include <stdint.h>

#include "esp_err.h"

#ifdef __cplusplus
extern "C" {
#endif

typedef size_t (*audio_pipeline_playback_provider_t)(int16_t *dst, size_t samples, void *ctx);
typedef void (*audio_pipeline_output_tap_t)(const int16_t *src, size_t samples, uint32_t frame_seq,
                                            void *ctx);
typedef void (*audio_pipeline_input_tap_t)(const int16_t *src, size_t samples, uint32_t frame_seq,
                                           void *ctx);

esp_err_t audio_pipeline_init(void);
esp_err_t audio_pipeline_start(void);
esp_err_t audio_pipeline_stop(void);
esp_err_t audio_pipeline_set_playback_provider(audio_pipeline_playback_provider_t provider,
                                               void *ctx);
esp_err_t audio_pipeline_set_output_tap(audio_pipeline_output_tap_t tap, void *ctx);
esp_err_t audio_pipeline_set_input_tap(audio_pipeline_input_tap_t tap, void *ctx);

#ifdef __cplusplus
}
#endif
