#pragma once

#include <stddef.h>
#include <stdint.h>

#include "esp_err.h"
#include "freertos/FreeRTOS.h"

#ifdef __cplusplus
extern "C" {
#endif

typedef struct {
    size_t capacity_bytes;
    size_t level_bytes;
    size_t high_watermark_bytes;
    size_t low_watermark_bytes;
    uint32_t overflow_count;
    uint32_t underflow_count;
    uint32_t dropped_bytes;
    uint32_t short_read_bytes;
    uint64_t total_written_bytes;
    uint64_t total_read_bytes;
} audio_ringbuf_stats_t;

typedef struct {
    uint8_t *storage;
    size_t capacity_bytes;
    size_t head;
    size_t tail;
    size_t level_bytes;
    audio_ringbuf_stats_t stats;
    portMUX_TYPE lock;
} audio_ringbuf_t;

esp_err_t audio_ringbuf_init(audio_ringbuf_t *ringbuf, uint8_t *storage, size_t capacity_bytes);
void audio_ringbuf_reset(audio_ringbuf_t *ringbuf);
size_t audio_ringbuf_write(audio_ringbuf_t *ringbuf, const uint8_t *src, size_t bytes);
size_t audio_ringbuf_read(audio_ringbuf_t *ringbuf, uint8_t *dst, size_t bytes);
size_t audio_ringbuf_level_bytes(audio_ringbuf_t *ringbuf);
void audio_ringbuf_get_stats(audio_ringbuf_t *ringbuf, audio_ringbuf_stats_t *stats_out);

#ifdef __cplusplus
}
#endif
