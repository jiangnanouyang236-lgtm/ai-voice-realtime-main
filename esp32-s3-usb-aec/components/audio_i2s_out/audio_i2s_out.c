#include "audio_i2s_out.h"

#include <stdbool.h>
#include <string.h>

#include "board_pins.h"
#include "driver/i2s_std.h"
#include "esp_check.h"
#include "esp_log.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"

static const char *TAG = "audio_i2s_out";
static const size_t TX_REPEAT_SAMPLES_MAX = 1024;
static const uint32_t TX_REPEAT_MIN_PEAK = 128;
static const uint32_t TX_WRITE_TIMEOUT_MS = 60;

static audio_i2s_out_config_t s_cfg;
static bool s_inited;
static bool s_started;
static const i2s_port_t s_port = I2S_NUM_1;
static i2s_chan_handle_t s_tx_handle;
static portMUX_TYPE s_stats_lock = portMUX_INITIALIZER_UNLOCKED;
static audio_i2s_out_stats_t s_stats;
static int16_t s_prev_frame[1024];
static size_t s_prev_frame_samples;
static uint32_t s_prev_frame_peak;
static uint32_t s_prev_repeat_run;

static bool to_data_width(uint8_t bits_per_sample, i2s_data_bit_width_t *out) {
    if (out == NULL) {
        return false;
    }
    switch (bits_per_sample) {
        case 8:
            *out = I2S_DATA_BIT_WIDTH_8BIT;
            return true;
        case 16:
            *out = I2S_DATA_BIT_WIDTH_16BIT;
            return true;
        case 24:
            *out = I2S_DATA_BIT_WIDTH_24BIT;
            return true;
        case 32:
            *out = I2S_DATA_BIT_WIDTH_32BIT;
            return true;
        default:
            return false;
    }
}

static void release_tx_channel(void) {
    if (s_tx_handle != NULL) {
        if (s_started) {
            (void)i2s_channel_disable(s_tx_handle);
            s_started = false;
        }
        (void)i2s_del_channel(s_tx_handle);
        s_tx_handle = NULL;
    }
    s_inited = false;
}

static void reset_window_stats_locked(void) {
    s_stats.window_write_calls = 0;
    s_stats.window_write_timeouts = 0;
    s_stats.window_write_failures = 0;
    s_stats.window_zero_write_calls = 0;
    s_stats.window_partial_write_calls = 0;
    s_stats.window_repeated_frame_events = 0;
    s_stats.window_repeat_run_max = 0;
    s_stats.window_max_write_us = 0;
    s_stats.window_max_chunk_us = 0;
    s_stats.window_max_zero_retries = 0;
    s_stats.window_max_frame_avg_abs = 0;
    s_stats.window_max_frame_peak = 0;
    s_stats.window_max_short_write_samples = 0;
}

static void compute_frame_stats(const int16_t *src, size_t samples, uint32_t *avg_abs,
                                uint32_t *peak) {
    uint64_t sum_abs = 0;
    uint32_t local_peak = 0;
    for (size_t i = 0; i < samples; ++i) {
        const int16_t s = src[i];
        const uint32_t mag = (s < 0) ? (uint32_t)(-(int32_t)s) : (uint32_t)s;
        sum_abs += mag;
        if (mag > local_peak) {
            local_peak = mag;
        }
    }
    if (avg_abs != NULL) {
        *avg_abs = (samples > 0) ? (uint32_t)(sum_abs / samples) : 0U;
    }
    if (peak != NULL) {
        *peak = local_peak;
    }
}

static void note_frame_stats_locked(const int16_t *src, size_t samples, uint32_t frame_seq,
                                    uint32_t avg_abs, uint32_t peak) {
    s_stats.total_write_calls++;
    s_stats.window_write_calls++;
    s_stats.last_frame_seq = frame_seq;
    s_stats.last_frame_avg_abs = avg_abs;
    s_stats.last_frame_peak = peak;
    if (avg_abs > s_stats.window_max_frame_avg_abs) {
        s_stats.window_max_frame_avg_abs = avg_abs;
    }
    if (peak > s_stats.window_max_frame_peak) {
        s_stats.window_max_frame_peak = peak;
    }

    if (samples == 0 || samples > TX_REPEAT_SAMPLES_MAX) {
        s_prev_frame_samples = 0;
        s_prev_frame_peak = 0;
        s_prev_repeat_run = 0;
        return;
    }

    const bool repeated = (s_prev_frame_samples == samples) && (peak >= TX_REPEAT_MIN_PEAK) &&
                          (s_prev_frame_peak >= TX_REPEAT_MIN_PEAK) &&
                          (memcmp(s_prev_frame, src, samples * sizeof(int16_t)) == 0);
    if (repeated) {
        s_prev_repeat_run++;
        s_stats.total_repeated_frame_events++;
        s_stats.window_repeated_frame_events++;
        if (s_prev_repeat_run > s_stats.window_repeat_run_max) {
            s_stats.window_repeat_run_max = s_prev_repeat_run;
        }
    } else {
        s_prev_repeat_run = 0;
    }
    memcpy(s_prev_frame, src, samples * sizeof(int16_t));
    s_prev_frame_samples = samples;
    s_prev_frame_peak = peak;
}

static void note_write_result_locked(uint32_t write_us, uint32_t chunk_us_max,
                                     uint32_t zero_writes, uint32_t max_zero_retry,
                                     uint32_t partial_chunks, uint32_t short_write_samples,
                                     bool timed_out, bool failed) {
    if (write_us > s_stats.window_max_write_us) {
        s_stats.window_max_write_us = write_us;
    }
    if (chunk_us_max > s_stats.window_max_chunk_us) {
        s_stats.window_max_chunk_us = chunk_us_max;
    }
    if (max_zero_retry > s_stats.window_max_zero_retries) {
        s_stats.window_max_zero_retries = max_zero_retry;
    }
    s_stats.total_zero_write_calls += zero_writes;
    s_stats.window_zero_write_calls += zero_writes;
    s_stats.total_partial_write_calls += partial_chunks;
    s_stats.window_partial_write_calls += partial_chunks;
    s_stats.total_short_write_samples += short_write_samples;
    if (short_write_samples > s_stats.window_max_short_write_samples) {
        s_stats.window_max_short_write_samples = short_write_samples;
    }
    if (timed_out) {
        s_stats.total_write_timeouts++;
        s_stats.window_write_timeouts++;
    }
    if (failed) {
        s_stats.total_write_failures++;
        s_stats.window_write_failures++;
    }
}

esp_err_t audio_i2s_out_init(const audio_i2s_out_config_t *cfg) {
    if (cfg == NULL || cfg->sample_rate_hz == 0 || cfg->bits_per_sample == 0 ||
        cfg->channels == 0) {
        return ESP_ERR_INVALID_ARG;
    }
    if (s_inited) {
        return ESP_ERR_INVALID_STATE;
    }
    const board_i2s_pin_map_t *pins = board_get_tx_i2s_pins();
    if (pins == NULL) {
        return ESP_ERR_INVALID_STATE;
    }
    i2s_data_bit_width_t data_width = I2S_DATA_BIT_WIDTH_16BIT;
    ESP_RETURN_ON_FALSE(to_data_width(cfg->bits_per_sample, &data_width), ESP_ERR_INVALID_ARG, TAG,
                        "unsupported bit width: %u", cfg->bits_per_sample);

    i2s_chan_config_t chan_cfg = I2S_CHANNEL_DEFAULT_CONFIG(s_port, I2S_ROLE_MASTER);
    chan_cfg.auto_clear = true;
    chan_cfg.dma_desc_num = 6;
    chan_cfg.dma_frame_num = 256;
    ESP_RETURN_ON_ERROR(i2s_new_channel(&chan_cfg, &s_tx_handle, NULL), TAG,
                        "i2s_new_channel failed");

    const i2s_slot_mode_t slot_mode =
        (cfg->channels == 2) ? I2S_SLOT_MODE_STEREO : I2S_SLOT_MODE_MONO;
    i2s_std_config_t std_cfg = {
        .clk_cfg = I2S_STD_CLK_DEFAULT_CONFIG(cfg->sample_rate_hz),
        .slot_cfg = I2S_STD_PHILIPS_SLOT_DEFAULT_CONFIG(data_width, slot_mode),
        .gpio_cfg =
            {
                .mclk = I2S_GPIO_UNUSED,
                .bclk = pins->bclk,
                .ws = pins->ws,
                .dout = pins->dout,
                .din = I2S_GPIO_UNUSED,
                .invert_flags =
                    {
                        .mclk_inv = false,
                        .bclk_inv = false,
                        .ws_inv = false,
                    },
            },
    };
    if (cfg->channels == 1) {
        // Keep single-channel TX pinned to the left slot to match the validated wiring.
        std_cfg.slot_cfg.slot_mask = I2S_STD_SLOT_LEFT;
    }

    esp_err_t ret = i2s_channel_init_std_mode(s_tx_handle, &std_cfg);
    if (ret != ESP_OK) {
        release_tx_channel();
        return ret;
    }

    s_cfg = *cfg;
    s_inited = true;
    s_started = false;
    taskENTER_CRITICAL(&s_stats_lock);
    memset(&s_stats, 0, sizeof(s_stats));
    s_prev_frame_samples = 0;
    s_prev_frame_peak = 0;
    s_prev_repeat_run = 0;
    reset_window_stats_locked();
    taskEXIT_CRITICAL(&s_stats_lock);
    ESP_LOGI(TAG, "init: %u Hz, %u-bit, %u ch", (unsigned)s_cfg.sample_rate_hz,
             s_cfg.bits_per_sample, s_cfg.channels);
    return ESP_OK;
}

esp_err_t audio_i2s_out_start(void) {
    if (!s_inited) {
        return ESP_ERR_INVALID_STATE;
    }
    if (s_started) {
        return ESP_OK;
    }
    ESP_RETURN_ON_ERROR(i2s_channel_enable(s_tx_handle), TAG, "i2s_channel_enable failed");
    s_started = true;
    ESP_LOGI(TAG, "start");
    return ESP_OK;
}

esp_err_t audio_i2s_out_stop(void) {
    if (!s_inited) {
        return ESP_ERR_INVALID_STATE;
    }
    if (!s_started) {
        return ESP_OK;
    }
    ESP_RETURN_ON_ERROR(i2s_channel_disable(s_tx_handle), TAG, "i2s_channel_disable failed");
    s_started = false;
    ESP_LOGI(TAG, "stop");
    return ESP_OK;
}

esp_err_t audio_i2s_out_write(const int16_t *src, size_t samples, uint32_t frame_seq,
                              size_t *written_samples) {
    if (!s_started || src == NULL || samples == 0) {
        return ESP_ERR_INVALID_ARG;
    }
    if (written_samples != NULL) {
        *written_samples = 0;
    }

    const uint8_t *p = (const uint8_t *)src;
    const size_t total_bytes = samples * sizeof(int16_t);
    size_t sent_bytes = 0;
    int idle_retries = 0;
    uint32_t zero_writes = 0;
    uint32_t max_zero_retry = 0;
    uint32_t partial_chunks = 0;
    uint32_t chunk_us_max = 0;
    uint32_t short_write_samples = 0;
    bool timed_out = false;
    bool failed = false;
    uint32_t frame_avg_abs = 0;
    uint32_t frame_peak = 0;
    compute_frame_stats(src, samples, &frame_avg_abs, &frame_peak);
    taskENTER_CRITICAL(&s_stats_lock);
    note_frame_stats_locked(src, samples, frame_seq, frame_avg_abs, frame_peak);
    taskEXIT_CRITICAL(&s_stats_lock);
    const int64_t write_start_us = esp_timer_get_time();
    while (sent_bytes < total_bytes) {
        size_t bytes_written = 0;
        const int64_t chunk_start_us = esp_timer_get_time();
        esp_err_t ret = i2s_channel_write(s_tx_handle, &p[sent_bytes], total_bytes - sent_bytes,
                                          &bytes_written, TX_WRITE_TIMEOUT_MS);
        const uint32_t chunk_us = (uint32_t)(esp_timer_get_time() - chunk_start_us);
        if (chunk_us > chunk_us_max) {
            chunk_us_max = chunk_us;
        }
        if (ret != ESP_OK) {
            if (written_samples != NULL) {
                *written_samples = sent_bytes / sizeof(int16_t);
            }
            failed = true;
            short_write_samples = (uint32_t)((total_bytes - sent_bytes) / sizeof(int16_t));
            taskENTER_CRITICAL(&s_stats_lock);
            note_write_result_locked((uint32_t)(esp_timer_get_time() - write_start_us), chunk_us_max,
                                     zero_writes, max_zero_retry, partial_chunks, short_write_samples,
                                     false, true);
            taskEXIT_CRITICAL(&s_stats_lock);
            ESP_LOGW(TAG, "i2s_write failed seq=%u sent=%u/%u ret=%s", frame_seq,
                     (unsigned)sent_bytes, (unsigned)total_bytes, esp_err_to_name(ret));
            return ret;
        }
        if (bytes_written == 0) {
            idle_retries++;
            zero_writes++;
            if ((uint32_t)idle_retries > max_zero_retry) {
                max_zero_retry = (uint32_t)idle_retries;
            }
            if (idle_retries >= 3) {
                if (written_samples != NULL) {
                    *written_samples = sent_bytes / sizeof(int16_t);
                }
                timed_out = true;
                short_write_samples = (uint32_t)((total_bytes - sent_bytes) / sizeof(int16_t));
                taskENTER_CRITICAL(&s_stats_lock);
                note_write_result_locked((uint32_t)(esp_timer_get_time() - write_start_us), chunk_us_max,
                                         zero_writes, max_zero_retry, partial_chunks, short_write_samples,
                                         true, false);
                taskEXIT_CRITICAL(&s_stats_lock);
                ESP_LOGW(TAG, "i2s_write timeout seq=%u sent=%u/%u", frame_seq, (unsigned)sent_bytes,
                         (unsigned)total_bytes);
                return ESP_ERR_TIMEOUT;
            }
            continue;
        }
        if (sent_bytes + bytes_written < total_bytes) {
            partial_chunks++;
        }
        idle_retries = 0;
        sent_bytes += bytes_written;
    }
    if (written_samples != NULL) {
        *written_samples = sent_bytes / sizeof(int16_t);
    }
    taskENTER_CRITICAL(&s_stats_lock);
    note_write_result_locked((uint32_t)(esp_timer_get_time() - write_start_us), chunk_us_max,
                             zero_writes, max_zero_retry, partial_chunks, short_write_samples,
                             timed_out, failed);
    taskEXIT_CRITICAL(&s_stats_lock);
    return ESP_OK;
}

audio_i2s_out_stats_t audio_i2s_out_get_stats(bool reset_window) {
    audio_i2s_out_stats_t copy = {0};
    taskENTER_CRITICAL(&s_stats_lock);
    copy = s_stats;
    if (reset_window) {
        reset_window_stats_locked();
    }
    taskEXIT_CRITICAL(&s_stats_lock);
    return copy;
}
