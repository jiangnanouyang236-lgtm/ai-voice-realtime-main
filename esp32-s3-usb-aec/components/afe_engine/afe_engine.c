#include "afe_engine.h"

#include <stdbool.h>
#include <stdint.h>
#include <string.h>

#include "esp_log.h"
#include "freertos/FreeRTOS.h"
#include "freertos/semphr.h"
#include "sdkconfig.h"

#ifndef AUDIO_HAS_ESP_SR
#define AUDIO_HAS_ESP_SR 0
#endif

#if AUDIO_HAS_ESP_SR
#include "esp_afe_sr_iface.h"
#include "esp_afe_sr_models.h"
#endif

static const char *TAG = "afe_engine";

#define MAX_OUTPUT_SAMPLES 1024
#define MAX_FEED_INTERLEAVED_SAMPLES 4096
#define SR_FETCH_PREFILL_MULTIPLIER 2

static afe_engine_config_t s_cfg;
static afe_engine_stats_t s_stats;
static SemaphoreHandle_t s_stats_mutex;
static bool s_inited;
static bool s_use_sr_backend;
static bool s_aec_enabled;
static uint32_t s_last_seq;
static int16_t s_out_frame[MAX_OUTPUT_SAMPLES];
static size_t s_out_samples;
static bool s_has_frame;
static int32_t s_noise_estimate;

#if AUDIO_HAS_ESP_SR
static const esp_afe_sr_iface_t *s_sr_iface;
static esp_afe_sr_data_t *s_sr_data;
static srmodel_list_t *s_sr_models;
static afe_config_t *s_sr_cfg;
static char s_sr_input_format[8];
static size_t s_sr_feed_channels;
static size_t s_sr_feed_chunk;
static size_t s_sr_fetch_chunk;
static size_t s_sr_feed_samples;
static size_t s_sr_fetch_budget_step;
static size_t s_sr_fetch_min_budget;
static size_t s_sr_feed_budget;
static bool s_sr_fetch_primed;
static int16_t s_sr_feed_accum[MAX_FEED_INTERLEAVED_SAMPLES];
static size_t s_sr_feed_accum_samples;
#endif

static inline int16_t clamp_i16(int32_t v) {
    if (v > 32767) {
        return 32767;
    }
    if (v < -32768) {
        return -32768;
    }
    return (int16_t)v;
}

static void update_vad_stats(bool speech) {
    if (speech) {
        s_stats.vad_speech_count++;
        s_stats.vad_last_state = 1;
    } else {
        s_stats.vad_silence_count++;
        s_stats.vad_last_state = 0;
    }
}

static void reset_output_state(void) {
    s_last_seq = 0;
    s_out_samples = 0;
    s_has_frame = false;
    s_noise_estimate = 32;
}

static esp_err_t stub_feed(const int16_t *mic, size_t mic_samples, const int16_t *ref,
                           size_t ref_samples, uint32_t frame_seq) {
    if (!s_inited || mic == NULL || ref == NULL || mic_samples == 0 || ref_samples == 0 ||
        s_cfg.mic_channels == 0) {
        return ESP_ERR_INVALID_ARG;
    }

    const size_t out_samples = mic_samples / s_cfg.mic_channels;
    if (out_samples == 0 || out_samples > MAX_OUTPUT_SAMPLES) {
        return ESP_ERR_INVALID_SIZE;
    }

    bool aec_on = false;
    if (s_stats_mutex != NULL) {
        xSemaphoreTake(s_stats_mutex, portMAX_DELAY);
        aec_on = s_aec_enabled;
        xSemaphoreGive(s_stats_mutex);
    }
    for (size_t i = 0; i < out_samples; ++i) {
        int32_t v = mic[i * s_cfg.mic_channels];
        if (aec_on && i < ref_samples) {
            v -= ((int32_t)ref[i]) / 4;
        }

        const int32_t a = (v >= 0) ? v : -v;
        if (s_cfg.enable_ns) {
            s_noise_estimate = (15 * s_noise_estimate + a) / 16;
            const int32_t gate = s_noise_estimate + 24;
            if (a < gate) {
                v /= 4;
            }
        }
        s_out_frame[i] = clamp_i16(v);
    }

    if (s_stats_mutex != NULL) {
        xSemaphoreTake(s_stats_mutex, portMAX_DELAY);
    }
    s_out_samples = out_samples;
    s_has_frame = true;
    s_last_seq = frame_seq;
    s_stats.feed_count++;
    if (s_stats_mutex != NULL) {
        xSemaphoreGive(s_stats_mutex);
    }
    return ESP_OK;
}

#if AUDIO_HAS_ESP_SR
static bool build_input_format(char *dst, size_t cap, uint8_t mic_channels,
                               uint8_t ref_channels) {
    if (dst == NULL || cap < 3 || mic_channels == 0 || ref_channels == 0) {
        return false;
    }
    size_t pos = 0;
    for (uint8_t i = 0; i < mic_channels && pos + 1 < cap; ++i) {
        dst[pos++] = 'M';
    }
    for (uint8_t i = 0; i < ref_channels && pos + 1 < cap; ++i) {
        dst[pos++] = 'R';
    }
    if (pos + 1 > cap) {
        return false;
    }
    dst[pos] = '\0';
    return true;
}

static void sr_reset_state(void) {
    s_sr_iface = NULL;
    s_sr_data = NULL;
    s_sr_models = NULL;
    s_sr_cfg = NULL;
    s_sr_input_format[0] = '\0';
    s_sr_feed_channels = 0;
    s_sr_feed_chunk = 0;
    s_sr_fetch_chunk = 0;
    s_sr_feed_samples = 0;
    s_sr_fetch_budget_step = 0;
    s_sr_fetch_min_budget = 0;
    s_sr_feed_budget = 0;
    s_sr_fetch_primed = false;
    s_sr_feed_accum_samples = 0;
}

static void sr_deinit(void) {
    if (s_sr_iface != NULL && s_sr_data != NULL) {
        s_sr_iface->destroy(s_sr_data);
    }
    if (s_sr_cfg != NULL) {
        afe_config_free(s_sr_cfg);
    }
    if (s_sr_models != NULL) {
        esp_srmodel_deinit(s_sr_models);
    }
    sr_reset_state();
}

static esp_err_t sr_precheck(void) {
#if !defined(CONFIG_SPIRAM)
    ESP_LOGW(TAG, "esp-sr precheck failed: CONFIG_SPIRAM is disabled");
    return ESP_ERR_NOT_SUPPORTED;
#endif
#if defined(CONFIG_ESP_DEFAULT_CPU_FREQ_MHZ) && (CONFIG_ESP_DEFAULT_CPU_FREQ_MHZ < 240)
    ESP_LOGW(TAG, "esp-sr precheck failed: CPU freq is %d MHz (< 240 MHz)",
             CONFIG_ESP_DEFAULT_CPU_FREQ_MHZ);
    return ESP_ERR_NOT_SUPPORTED;
#endif
    return ESP_OK;
}

static esp_err_t sr_try_init(void) {
    sr_reset_state();

    const esp_err_t precheck_ret = sr_precheck();
    if (precheck_ret != ESP_OK) {
        return precheck_ret;
    }

    if (s_cfg.sample_rate_hz != 16000) {
        return ESP_ERR_NOT_SUPPORTED;
    }
    if (!build_input_format(s_sr_input_format, sizeof(s_sr_input_format), s_cfg.mic_channels,
                            s_cfg.ref_channels)) {
        return ESP_ERR_INVALID_ARG;
    }

    s_sr_models = esp_srmodel_init("model");
    if (s_sr_models == NULL) {
        return ESP_ERR_NOT_FOUND;
    }

    const afe_mode_t afe_mode = (s_cfg.high_perf_mode != 0) ? AFE_MODE_HIGH_PERF : AFE_MODE_LOW_COST;
    afe_config_t *afe_cfg = afe_config_init(s_sr_input_format, s_sr_models, AFE_TYPE_VC, afe_mode);
    if (afe_cfg == NULL) {
        return ESP_ERR_NO_MEM;
    }

    // Dedicated audio peripheral: maximize all processing capabilities.
    afe_cfg->wakenet_init = false;
    afe_cfg->vad_init = (s_cfg.enable_vad != 0);
    afe_cfg->aec_init = (s_cfg.enable_aec != 0);
    afe_cfg->ns_init = (s_cfg.enable_ns != 0);
    afe_cfg->agc_init = (s_cfg.enable_agc != 0);
    afe_cfg->memory_alloc_mode = AFE_MEMORY_ALLOC_MORE_INTERNAL;
    afe_cfg->fixed_first_channel = true;

    s_sr_iface = esp_afe_handle_from_config(afe_cfg);
    if (s_sr_iface == NULL) {
        afe_config_free(afe_cfg);
        return ESP_FAIL;
    }

    s_sr_data = s_sr_iface->create_from_config(afe_cfg);
    if (s_sr_data == NULL) {
        afe_config_free(afe_cfg);
        sr_deinit();
        return ESP_FAIL;
    }
    s_sr_cfg = afe_cfg;

    s_sr_feed_chunk = s_sr_iface->get_feed_chunksize(s_sr_data);
    s_sr_fetch_chunk = s_sr_iface->get_fetch_chunksize(s_sr_data);
    s_sr_feed_channels = s_sr_iface->get_feed_channel_num(s_sr_data);
    s_sr_feed_samples = s_sr_feed_chunk * s_sr_feed_channels;
    if (s_sr_feed_chunk == 0 || s_sr_feed_channels == 0 || s_sr_feed_samples == 0 ||
        s_sr_feed_samples > MAX_FEED_INTERLEAVED_SAMPLES || s_sr_feed_chunk > MAX_OUTPUT_SAMPLES ||
        s_sr_fetch_chunk == 0 || s_sr_fetch_chunk > MAX_OUTPUT_SAMPLES) {
        sr_deinit();
        return ESP_ERR_INVALID_SIZE;
    }
    s_sr_fetch_budget_step = s_sr_fetch_chunk;
    if (s_sr_fetch_budget_step == 0) {
        s_sr_fetch_budget_step = 1;
    }
    s_sr_fetch_min_budget = s_sr_fetch_budget_step * SR_FETCH_PREFILL_MULTIPLIER;
    if (s_sr_fetch_min_budget < s_sr_fetch_budget_step) {
        s_sr_fetch_min_budget = s_sr_fetch_budget_step;
    }

    // AEC/NS init state is now controlled by aec_init/ns_init fields above.
    // No runtime enable/disable calls needed here — create_from_config()
    // respects the config flags directly.

    return ESP_OK;
}

static esp_err_t sr_feed(const int16_t *mic, size_t mic_samples, const int16_t *ref,
                         size_t ref_samples, uint32_t frame_seq) {
    if (!s_inited || !s_use_sr_backend || s_sr_iface == NULL || s_sr_data == NULL || mic == NULL ||
        ref == NULL || mic_samples == 0 || s_cfg.mic_channels == 0 || s_cfg.ref_channels == 0) {
        return ESP_ERR_INVALID_ARG;
    }
    if ((mic_samples % s_cfg.mic_channels) != 0) {
        return ESP_ERR_INVALID_SIZE;
    }

    const size_t frame_samples = mic_samples / s_cfg.mic_channels;
    const size_t frame_ref_samples = frame_samples * s_cfg.ref_channels;
    const size_t frame_interleaved = frame_samples * (s_cfg.mic_channels + s_cfg.ref_channels);
    if ((frame_samples == 0) || (frame_interleaved == 0)) {
        return ESP_ERR_INVALID_SIZE;
    }
    if (ref_samples < frame_ref_samples) {
        return ESP_ERR_INVALID_SIZE;
    }
    if (s_sr_feed_accum_samples + frame_interleaved > MAX_FEED_INTERLEAVED_SAMPLES) {
        s_sr_feed_accum_samples = 0;
        return ESP_ERR_NO_MEM;
    }

    size_t wr = s_sr_feed_accum_samples;
    for (size_t i = 0; i < frame_samples; ++i) {
        for (size_t m = 0; m < s_cfg.mic_channels; ++m) {
            s_sr_feed_accum[wr++] = mic[i * s_cfg.mic_channels + m];
        }
        for (size_t r = 0; r < s_cfg.ref_channels; ++r) {
            s_sr_feed_accum[wr++] = ref[i * s_cfg.ref_channels + r];
        }
    }
    s_sr_feed_accum_samples = wr;

    while (s_sr_feed_accum_samples >= s_sr_feed_samples) {
        int feed_ret = s_sr_iface->feed(s_sr_data, s_sr_feed_accum);
        if (feed_ret < 0) {
            return ESP_FAIL;
        }
        if (s_stats_mutex != NULL) {
            xSemaphoreTake(s_stats_mutex, portMAX_DELAY);
        }
        s_stats.feed_count++;
        s_last_seq = frame_seq;
        s_sr_feed_budget += s_sr_feed_chunk;
        if (s_stats_mutex != NULL) {
            xSemaphoreGive(s_stats_mutex);
        }

        const size_t remain = s_sr_feed_accum_samples - s_sr_feed_samples;
        if (remain > 0) {
            memmove(s_sr_feed_accum, &s_sr_feed_accum[s_sr_feed_samples],
                    remain * sizeof(int16_t));
        }
        s_sr_feed_accum_samples = remain;
    }

    return ESP_OK;
}

static esp_err_t sr_fetch_frame(int16_t *out, size_t out_capacity, size_t *valid_samples_out,
                                uint32_t *frame_seq_out) {
    if (out == NULL || out_capacity == 0 || valid_samples_out == NULL || frame_seq_out == NULL) {
        return ESP_ERR_INVALID_ARG;
    }
    *valid_samples_out = 0;
    if (s_stats_mutex != NULL) {
        xSemaphoreTake(s_stats_mutex, portMAX_DELAY);
    }
    const size_t min_feed_before_fetch =
        s_sr_fetch_primed ? s_sr_fetch_budget_step : s_sr_fetch_min_budget;
    const bool can_fetch = (s_sr_feed_budget >= min_feed_before_fetch);
    if (s_stats_mutex != NULL) {
        xSemaphoreGive(s_stats_mutex);
    }
    if (!can_fetch) {
        return ESP_ERR_TIMEOUT;
    }

    afe_fetch_result_t *fetch_result =
        (s_sr_iface->fetch_with_delay != NULL)
            ? s_sr_iface->fetch_with_delay(s_sr_data, pdMS_TO_TICKS(10))
            : s_sr_iface->fetch(s_sr_data);
    if (fetch_result == NULL || fetch_result->data == NULL || fetch_result->data_size <= 0) {
        return ESP_ERR_TIMEOUT;
    }

    size_t fetch_samples = (size_t)fetch_result->data_size / sizeof(int16_t);
    if (fetch_samples > MAX_OUTPUT_SAMPLES) {
        fetch_samples = MAX_OUTPUT_SAMPLES;
    }
    const size_t n = (out_capacity < fetch_samples) ? out_capacity : fetch_samples;
    memcpy(out, fetch_result->data, n * sizeof(int16_t));

    if (s_stats_mutex != NULL) {
        xSemaphoreTake(s_stats_mutex, portMAX_DELAY);
    }
    if (s_cfg.enable_vad) {
        update_vad_stats(fetch_result->vad_state == VAD_SPEECH);
    }
    s_sr_fetch_primed = true;
    if (s_sr_feed_budget >= s_sr_fetch_budget_step) {
        s_sr_feed_budget -= s_sr_fetch_budget_step;
    } else {
        s_sr_feed_budget = 0;
    }
    *frame_seq_out = s_last_seq;
    *valid_samples_out = n;
    s_stats.fetch_count++;
    if (s_stats_mutex != NULL) {
        xSemaphoreGive(s_stats_mutex);
    }
    return ESP_OK;
}
#endif

esp_err_t afe_engine_init(const afe_engine_config_t *cfg) {
    if (cfg == NULL || cfg->sample_rate_hz == 0 || cfg->mic_channels == 0 ||
        cfg->ref_channels == 0) {
        return ESP_ERR_INVALID_ARG;
    }

    s_cfg = *cfg;
    s_aec_enabled = (s_cfg.enable_aec != 0);
    memset(&s_stats, 0, sizeof(s_stats));
    reset_output_state();
    s_use_sr_backend = false;

#if AUDIO_HAS_ESP_SR
    const esp_err_t sr_ret = sr_try_init();
    if (sr_ret == ESP_OK) {
        s_use_sr_backend = true;
        ESP_LOGI(TAG, "esp-sr backend active: fmt=%s mode=%s feed_chunk=%u fetch_chunk=%u feed_ch=%u",
                 s_sr_input_format, s_cfg.high_perf_mode ? "high-perf" : "low-cost",
                 (unsigned)s_sr_feed_chunk, (unsigned)s_sr_fetch_chunk,
                 (unsigned)s_sr_feed_channels);
    } else {
        ESP_LOGE(TAG, "esp-sr init failed; refusing to start without real AEC: %s",
                 esp_err_to_name(sr_ret));
        return sr_ret;
    }
#else
    ESP_LOGE(TAG, "esp-sr component not found; refusing to start without real AEC");
    return ESP_ERR_NOT_SUPPORTED;
#endif

    s_stats_mutex = xSemaphoreCreateMutex();
    if (s_stats_mutex == NULL) {
#if AUDIO_HAS_ESP_SR
        if (s_use_sr_backend) {
            sr_deinit();
        }
#endif
        return ESP_ERR_NO_MEM;
    }

    s_inited = true;
    ESP_LOGI(TAG, "init: %u Hz mic=%u ref=%u aec=%u ns=%u vad=%u agc=%u mode=%s mem=%s backend=%s",
             (unsigned)s_cfg.sample_rate_hz, s_cfg.mic_channels, s_cfg.ref_channels,
             s_cfg.enable_aec, s_cfg.enable_ns, s_cfg.enable_vad, s_cfg.enable_agc,
             s_cfg.high_perf_mode ? "high-perf" : "low-cost",
             s_use_sr_backend ? "more-internal" : "n/a",
             "esp-sr");
    return ESP_OK;
}

esp_err_t afe_engine_deinit(void) {
    if (!s_inited) {
        return ESP_OK;
    }
    /* Must only be called when no feed/fetch are in progress (e.g. after pipeline stopped). */

#if AUDIO_HAS_ESP_SR
    if (s_use_sr_backend) {
        sr_deinit();
    }
#endif

    s_inited = false;
    s_use_sr_backend = false;
    s_aec_enabled = false;
    if (s_stats_mutex != NULL) {
        vSemaphoreDelete(s_stats_mutex);
        s_stats_mutex = NULL;
    }
    memset(&s_cfg, 0, sizeof(s_cfg));
    memset(&s_stats, 0, sizeof(s_stats));
    reset_output_state();
    return ESP_OK;
}

esp_err_t afe_engine_feed(const int16_t *mic, size_t mic_samples, const int16_t *ref,
                          size_t ref_samples, uint32_t frame_seq) {
    if (!s_inited) {
        return ESP_ERR_INVALID_STATE;
    }

#if AUDIO_HAS_ESP_SR
    if (s_use_sr_backend) {
        return sr_feed(mic, mic_samples, ref, ref_samples, frame_seq);
    }
#endif
    return stub_feed(mic, mic_samples, ref, ref_samples, frame_seq);
}

esp_err_t afe_engine_fetch(int16_t *out, size_t out_capacity, size_t *valid_samples_out,
                           uint32_t *frame_seq_out) {
    if (!s_inited || out == NULL || out_capacity == 0 || valid_samples_out == NULL ||
        frame_seq_out == NULL) {
        return ESP_ERR_INVALID_ARG;
    }
    *valid_samples_out = 0;

#if AUDIO_HAS_ESP_SR
    if (s_use_sr_backend) {
        const esp_err_t ret =
            sr_fetch_frame(out, out_capacity, valid_samples_out, frame_seq_out);
        if (s_stats_mutex != NULL) {
            xSemaphoreTake(s_stats_mutex, portMAX_DELAY);
        }
        if (ret == ESP_ERR_TIMEOUT) {
            s_stats.timeout_count++;
        }
        if (s_stats_mutex != NULL) {
            xSemaphoreGive(s_stats_mutex);
        }
        return ret;
    }
#endif

    xSemaphoreTake(s_stats_mutex, portMAX_DELAY);
    if (!s_has_frame) {
        s_stats.timeout_count++;
        xSemaphoreGive(s_stats_mutex);
        return ESP_ERR_TIMEOUT;
    }
    const size_t n = (out_capacity < s_out_samples) ? out_capacity : s_out_samples;
    const uint32_t seq = s_last_seq;
    memcpy(out, s_out_frame, n * sizeof(int16_t));
    s_has_frame = false;
    s_stats.fetch_count++;
    xSemaphoreGive(s_stats_mutex);

    if (s_cfg.enable_vad && n > 0) {
        uint64_t sum_abs = 0;
        for (size_t i = 0; i < n; ++i) {
            const int32_t s = out[i];
            sum_abs += (uint32_t)((s >= 0) ? s : -s);
        }
        const uint32_t avg_abs = (uint32_t)(sum_abs / n);
        xSemaphoreTake(s_stats_mutex, portMAX_DELAY);
        update_vad_stats(avg_abs >= 256U);
        xSemaphoreGive(s_stats_mutex);
    }
    *frame_seq_out = seq;
    *valid_samples_out = n;
    return ESP_OK;
}

bool afe_engine_get_aec_enabled(void) {
    bool on = false;
    if (s_stats_mutex != NULL) {
        xSemaphoreTake(s_stats_mutex, portMAX_DELAY);
        on = s_aec_enabled;
        xSemaphoreGive(s_stats_mutex);
    }
    return on;
}

afe_engine_stats_t afe_engine_get_stats(void) {
    afe_engine_stats_t copy = {0};
    if (s_stats_mutex != NULL) {
        xSemaphoreTake(s_stats_mutex, portMAX_DELAY);
        copy = s_stats;
        xSemaphoreGive(s_stats_mutex);
    }
    return copy;
}
