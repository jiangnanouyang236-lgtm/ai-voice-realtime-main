#include "audio_buffers.h"

#include <stdlib.h>
#include <string.h>

#include "esp_log.h"
#include "freertos/FreeRTOS.h"
#include "freertos/semphr.h"

static const char *TAG = "audio_buffers";
static const size_t FRAME_QUEUE_DEPTH = 32;

typedef struct {
    bool inited;
    audio_buffers_config_t cfg;
    audio_buffers_stats_t stats;
    uint32_t last_mic_seq;
    uint32_t last_ref_seq;
    uint32_t last_pop_seq;
    size_t mic_samples_per_frame;
    size_t ref_samples_per_frame;
    size_t queue_depth;
    int16_t *mic_frames;
    int16_t *ref_frames;
    uint32_t *mic_seq_slots;
    uint32_t *ref_seq_slots;
    SemaphoreHandle_t lock;
} audio_buffers_ctx_t;

static audio_buffers_ctx_t s_ctx;

static inline size_t slot_index(uint32_t frame_seq) {
    return (size_t)(frame_seq % (uint32_t)s_ctx.queue_depth);
}

static inline int16_t *mic_slot_ptr(size_t idx) {
    return &s_ctx.mic_frames[idx * s_ctx.mic_samples_per_frame];
}

static inline int16_t *ref_slot_ptr(size_t idx) {
    return &s_ctx.ref_frames[idx * s_ctx.ref_samples_per_frame];
}

esp_err_t audio_buffers_init(const audio_buffers_config_t *cfg) {
    if (cfg == NULL || cfg->samples_per_frame == 0 || cfg->mic_channels == 0 ||
        cfg->ref_channels == 0) {
        return ESP_ERR_INVALID_ARG;
    }
    if (s_ctx.inited) {
        return ESP_OK;
    }

    memset(&s_ctx, 0, sizeof(s_ctx));
    s_ctx.cfg = *cfg;
    s_ctx.mic_samples_per_frame = s_ctx.cfg.samples_per_frame * s_ctx.cfg.mic_channels;
    s_ctx.ref_samples_per_frame = s_ctx.cfg.samples_per_frame * s_ctx.cfg.ref_channels;
    s_ctx.queue_depth = FRAME_QUEUE_DEPTH;

    s_ctx.mic_frames = calloc(s_ctx.queue_depth * s_ctx.mic_samples_per_frame, sizeof(int16_t));
    s_ctx.ref_frames = calloc(s_ctx.queue_depth * s_ctx.ref_samples_per_frame, sizeof(int16_t));
    s_ctx.mic_seq_slots = calloc(s_ctx.queue_depth, sizeof(uint32_t));
    s_ctx.ref_seq_slots = calloc(s_ctx.queue_depth, sizeof(uint32_t));
    if (s_ctx.mic_frames == NULL || s_ctx.ref_frames == NULL || s_ctx.mic_seq_slots == NULL ||
        s_ctx.ref_seq_slots == NULL) {
        free(s_ctx.mic_frames);
        free(s_ctx.ref_frames);
        free(s_ctx.mic_seq_slots);
        free(s_ctx.ref_seq_slots);
        memset(&s_ctx, 0, sizeof(s_ctx));
        return ESP_ERR_NO_MEM;
    }
    s_ctx.lock = xSemaphoreCreateMutex();
    if (s_ctx.lock == NULL) {
        free(s_ctx.mic_frames);
        free(s_ctx.ref_frames);
        free(s_ctx.mic_seq_slots);
        free(s_ctx.ref_seq_slots);
        s_ctx.mic_frames = NULL;
        s_ctx.ref_frames = NULL;
        s_ctx.mic_seq_slots = NULL;
        s_ctx.ref_seq_slots = NULL;
        return ESP_ERR_NO_MEM;
    }

    s_ctx.inited = true;
    ESP_LOGI(TAG, "buffers init: frame=%u mic_ch=%u ref_ch=%u depth=%u",
             (unsigned)s_ctx.cfg.samples_per_frame, s_ctx.cfg.mic_channels,
             s_ctx.cfg.ref_channels, (unsigned)s_ctx.queue_depth);
    return ESP_OK;
}

esp_err_t audio_buffers_deinit(void) {
    if (!s_ctx.inited) {
        return ESP_OK;
    }
    if (s_ctx.lock != NULL) {
        vSemaphoreDelete(s_ctx.lock);
        s_ctx.lock = NULL;
    }
    free(s_ctx.mic_frames);
    free(s_ctx.ref_frames);
    free(s_ctx.mic_seq_slots);
    free(s_ctx.ref_seq_slots);
    s_ctx.mic_frames = NULL;
    s_ctx.ref_frames = NULL;
    s_ctx.mic_seq_slots = NULL;
    s_ctx.ref_seq_slots = NULL;
    memset(&s_ctx, 0, sizeof(s_ctx));
    return ESP_OK;
}

esp_err_t audio_buffers_push_mic(const int16_t *data, size_t samples, uint32_t frame_seq) {
    if (!s_ctx.inited || data == NULL || samples == 0) {
        return ESP_ERR_INVALID_ARG;
    }
    if (samples != s_ctx.mic_samples_per_frame) {
        return ESP_ERR_INVALID_SIZE;
    }
    xSemaphoreTake(s_ctx.lock, portMAX_DELAY);
    if (s_ctx.last_mic_seq != 0 && frame_seq > (s_ctx.last_mic_seq + 1)) {
        s_ctx.stats.drop_count += (frame_seq - s_ctx.last_mic_seq - 1);
    }
    const size_t idx = slot_index(frame_seq);
    memcpy(mic_slot_ptr(idx), data, samples * sizeof(int16_t));
    s_ctx.mic_seq_slots[idx] = frame_seq;
    s_ctx.stats.mic_pushed++;
    s_ctx.last_mic_seq = frame_seq;
    xSemaphoreGive(s_ctx.lock);
    return ESP_OK;
}

esp_err_t audio_buffers_push_ref(const int16_t *data, size_t samples, uint32_t frame_seq) {
    if (!s_ctx.inited || data == NULL || samples == 0) {
        return ESP_ERR_INVALID_ARG;
    }
    if (samples != s_ctx.ref_samples_per_frame) {
        return ESP_ERR_INVALID_SIZE;
    }
    xSemaphoreTake(s_ctx.lock, portMAX_DELAY);
    if (s_ctx.last_ref_seq != 0 && frame_seq > (s_ctx.last_ref_seq + 1)) {
        s_ctx.stats.drop_count += (frame_seq - s_ctx.last_ref_seq - 1);
    }
    const size_t idx = slot_index(frame_seq);
    memcpy(ref_slot_ptr(idx), data, samples * sizeof(int16_t));
    s_ctx.ref_seq_slots[idx] = frame_seq;
    s_ctx.stats.ref_pushed++;
    s_ctx.last_ref_seq = frame_seq;
    xSemaphoreGive(s_ctx.lock);
    return ESP_OK;
}

esp_err_t audio_buffers_pop_aligned(int16_t *mic_out, size_t mic_samples,
                                    int16_t *ref_out, size_t ref_samples,
                                    uint32_t *frame_seq_out) {
    if (!s_ctx.inited || mic_out == NULL || ref_out == NULL || frame_seq_out == NULL ||
        mic_samples == 0 || ref_samples == 0) {
        return ESP_ERR_INVALID_ARG;
    }
    if (mic_samples != s_ctx.mic_samples_per_frame || ref_samples != s_ctx.ref_samples_per_frame) {
        return ESP_ERR_INVALID_SIZE;
    }

    xSemaphoreTake(s_ctx.lock, portMAX_DELAY);
    const uint32_t mic_seq = s_ctx.last_mic_seq;
    const uint32_t ref_seq = s_ctx.last_ref_seq;
    if ((mic_seq == 0) || (ref_seq == 0)) {
        xSemaphoreGive(s_ctx.lock);
        return ESP_ERR_NOT_FOUND;
    }

    const uint32_t max_common_seq = (mic_seq < ref_seq) ? mic_seq : ref_seq;
    uint32_t search_start = (s_ctx.last_pop_seq == 0) ? 1U : (s_ctx.last_pop_seq + 1U);
    const uint32_t earliest_retained =
        (max_common_seq >= (uint32_t)s_ctx.queue_depth)
            ? (max_common_seq - (uint32_t)s_ctx.queue_depth + 1U)
            : 1U;
    if (search_start < earliest_retained) {
        search_start = earliest_retained;
    }

    uint32_t target_seq = 0;
    for (uint32_t seq = search_start; seq <= max_common_seq; ++seq) {
        const size_t idx = slot_index(seq);
        if (s_ctx.mic_seq_slots[idx] == seq && s_ctx.ref_seq_slots[idx] == seq) {
            target_seq = seq;
            break;
        }
    }
    if (target_seq == 0) {
        xSemaphoreGive(s_ctx.lock);
        return ESP_ERR_NOT_FOUND;
    }

    const size_t idx = slot_index(target_seq);
    memcpy(mic_out, mic_slot_ptr(idx), mic_samples * sizeof(int16_t));
    memcpy(ref_out, ref_slot_ptr(idx), ref_samples * sizeof(int16_t));
    s_ctx.stats.aligned_popped++;
    if (s_ctx.last_pop_seq != 0 && target_seq > (s_ctx.last_pop_seq + 1)) {
        s_ctx.stats.drop_count += (target_seq - s_ctx.last_pop_seq - 1);
    }
    s_ctx.last_pop_seq = target_seq;
    *frame_seq_out = target_seq;
    xSemaphoreGive(s_ctx.lock);

    return ESP_OK;
}

audio_buffers_stats_t audio_buffers_get_stats(void) {
    audio_buffers_stats_t stats = {0};
    if (!s_ctx.inited || s_ctx.lock == NULL) {
        return stats;
    }
    xSemaphoreTake(s_ctx.lock, portMAX_DELAY);
    stats = s_ctx.stats;
    xSemaphoreGive(s_ctx.lock);
    return stats;
}
