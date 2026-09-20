#include "audio_ringbuf.h"

#include <stddef.h>
#include <stdint.h>
#include <string.h>

#include "esp_check.h"

static size_t min_size(size_t a, size_t b)
{
    return (a < b) ? a : b;
}

static void update_watermarks(audio_ringbuf_t *ringbuf)
{
    if (ringbuf->level_bytes > ringbuf->stats.high_watermark_bytes) {
        ringbuf->stats.high_watermark_bytes = ringbuf->level_bytes;
    }
    if (ringbuf->level_bytes < ringbuf->stats.low_watermark_bytes) {
        ringbuf->stats.low_watermark_bytes = ringbuf->level_bytes;
    }
}

esp_err_t audio_ringbuf_init(audio_ringbuf_t *ringbuf, uint8_t *storage, size_t capacity_bytes)
{
    ESP_RETURN_ON_FALSE(ringbuf != NULL, ESP_ERR_INVALID_ARG, "audio_ringbuf", "ringbuf is null");
    ESP_RETURN_ON_FALSE(storage != NULL, ESP_ERR_INVALID_ARG, "audio_ringbuf", "storage is null");
    ESP_RETURN_ON_FALSE(capacity_bytes > 0, ESP_ERR_INVALID_ARG, "audio_ringbuf",
                        "capacity must be > 0");

    memset(ringbuf, 0, sizeof(*ringbuf));
    ringbuf->storage = storage;
    ringbuf->capacity_bytes = capacity_bytes;
    ringbuf->stats.capacity_bytes = capacity_bytes;
    ringbuf->stats.low_watermark_bytes = capacity_bytes;
    ringbuf->lock = (portMUX_TYPE)portMUX_INITIALIZER_UNLOCKED;
    return ESP_OK;
}

void audio_ringbuf_reset(audio_ringbuf_t *ringbuf)
{
    if (ringbuf == NULL) {
        return;
    }

    portENTER_CRITICAL(&ringbuf->lock);
    ringbuf->head = 0;
    ringbuf->tail = 0;
    ringbuf->level_bytes = 0;
    ringbuf->stats.level_bytes = 0;
    ringbuf->stats.high_watermark_bytes = 0;
    ringbuf->stats.low_watermark_bytes = ringbuf->capacity_bytes;
    portEXIT_CRITICAL(&ringbuf->lock);
}

size_t audio_ringbuf_write(audio_ringbuf_t *ringbuf, const uint8_t *src, size_t bytes)
{
    if (ringbuf == NULL || src == NULL || bytes == 0) {
        return 0;
    }

    portENTER_CRITICAL(&ringbuf->lock);

    if (bytes >= ringbuf->capacity_bytes) {
        const size_t skip = bytes - ringbuf->capacity_bytes;
        src += skip;
        bytes = ringbuf->capacity_bytes;
        ringbuf->stats.overflow_count++;
        ringbuf->stats.dropped_bytes += (uint32_t)(skip + ringbuf->level_bytes);
        ringbuf->head = 0;
        ringbuf->tail = 0;
        ringbuf->level_bytes = 0;
    } else {
        const size_t free_bytes = ringbuf->capacity_bytes - ringbuf->level_bytes;
        if (bytes > free_bytes) {
            const size_t drop = bytes - free_bytes;
            ringbuf->tail = (ringbuf->tail + drop) % ringbuf->capacity_bytes;
            ringbuf->level_bytes -= drop;
            ringbuf->stats.overflow_count++;
            ringbuf->stats.dropped_bytes += (uint32_t)drop;
        }
    }

    const size_t first = min_size(bytes, ringbuf->capacity_bytes - ringbuf->head);
    memcpy(&ringbuf->storage[ringbuf->head], src, first);
    if (bytes > first) {
        memcpy(ringbuf->storage, src + first, bytes - first);
    }

    ringbuf->head = (ringbuf->head + bytes) % ringbuf->capacity_bytes;
    ringbuf->level_bytes += bytes;
    ringbuf->stats.level_bytes = ringbuf->level_bytes;
    ringbuf->stats.total_written_bytes += bytes;
    update_watermarks(ringbuf);

    portEXIT_CRITICAL(&ringbuf->lock);
    return bytes;
}

size_t audio_ringbuf_read(audio_ringbuf_t *ringbuf, uint8_t *dst, size_t bytes)
{
    if (ringbuf == NULL || dst == NULL || bytes == 0) {
        return 0;
    }

    portENTER_CRITICAL(&ringbuf->lock);

    const size_t available = ringbuf->level_bytes;
    const size_t to_read = min_size(bytes, available);
    if (to_read < bytes) {
        ringbuf->stats.underflow_count++;
        ringbuf->stats.short_read_bytes += (uint32_t)(bytes - to_read);
    }

    const size_t first = min_size(to_read, ringbuf->capacity_bytes - ringbuf->tail);
    memcpy(dst, &ringbuf->storage[ringbuf->tail], first);
    if (to_read > first) {
        memcpy(dst + first, ringbuf->storage, to_read - first);
    }

    ringbuf->tail = (ringbuf->tail + to_read) % ringbuf->capacity_bytes;
    ringbuf->level_bytes -= to_read;
    ringbuf->stats.level_bytes = ringbuf->level_bytes;
    ringbuf->stats.total_read_bytes += to_read;
    update_watermarks(ringbuf);

    portEXIT_CRITICAL(&ringbuf->lock);
    return to_read;
}

size_t audio_ringbuf_level_bytes(audio_ringbuf_t *ringbuf)
{
    if (ringbuf == NULL) {
        return 0;
    }

    portENTER_CRITICAL(&ringbuf->lock);
    const size_t level_bytes = ringbuf->level_bytes;
    portEXIT_CRITICAL(&ringbuf->lock);
    return level_bytes;
}

void audio_ringbuf_get_stats(audio_ringbuf_t *ringbuf, audio_ringbuf_stats_t *stats_out)
{
    if (ringbuf == NULL || stats_out == NULL) {
        return;
    }

    portENTER_CRITICAL(&ringbuf->lock);
    *stats_out = ringbuf->stats;
    stats_out->level_bytes = ringbuf->level_bytes;
    portEXIT_CRITICAL(&ringbuf->lock);
}
