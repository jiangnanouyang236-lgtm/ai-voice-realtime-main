#include "audio_i2s_in.h"

#include <inttypes.h>
#include <stdbool.h>
#include <stddef.h>
#include <string.h>

#include "board_pins.h"
#include "driver/i2s_std.h"
#include "esp_check.h"
#include "esp_log.h"
#include "freertos/FreeRTOS.h"

#ifndef CONFIG_AUDIO_I2S_IN_STRICT_INMP441
#define CONFIG_AUDIO_I2S_IN_STRICT_INMP441 0
#endif

#ifndef CONFIG_AUDIO_I2S_IN_FIXED_SLOT
#define CONFIG_AUDIO_I2S_IN_FIXED_SLOT 0
#endif

static const char *TAG = "audio_i2s_in";
static const uint8_t RX_SLOT_BITS = 32;
#define RX_SCRATCH_WORDS_MAX 2048
static const uint32_t RIGHT_JUST_SIGN_MASK = 0x00800000;
static const uint32_t RIGHT_JUST_VALUE_MASK = 0x00FFFFFF;

static audio_i2s_in_config_t s_cfg;
static bool s_inited;
static bool s_started;
static const i2s_port_t s_port = I2S_NUM_0;
static i2s_chan_handle_t s_rx_handle;
static int32_t s_rx_scratch[RX_SCRATCH_WORDS_MAX];
static bool s_alignment_checked;
static bool s_left_justified;
static uint8_t s_mono_slot_index;
static bool s_mono_slot_selected;
#if CONFIG_AUDIO_I2S_IN_STRICT_INMP441
static const bool s_strict_inmp441 = true;
#else
static const bool s_strict_inmp441 = false;
#endif

typedef struct {
    int64_t slot_abs[2];
    uint8_t suggested_slot;
} mono_slot_probe_t;

static void release_rx_channel(void) {
    if (s_rx_handle != NULL) {
        if (s_started) {
            (void)i2s_channel_disable(s_rx_handle);
            s_started = false;
        }
        (void)i2s_del_channel(s_rx_handle);
        s_rx_handle = NULL;
    }
    s_inited = false;
}

static inline int16_t clamp_to_i16(int32_t v) {
    if (v > 32767) {
        return 32767;
    }
    if (v < -32768) {
        return -32768;
    }
    return (int16_t)v;
}

static inline int16_t decode_left_just_to_i16(int32_t raw) {
    return clamp_to_i16(raw >> 16);
}

static inline int16_t decode_right_just_to_i16(int32_t raw) {
    int32_t sample24 = (int32_t)(raw & RIGHT_JUST_VALUE_MASK);
    if ((sample24 & RIGHT_JUST_SIGN_MASK) != 0) {
        sample24 |= (int32_t)(~RIGHT_JUST_VALUE_MASK);
    }
    return clamp_to_i16(sample24 >> 8);
}

static mono_slot_probe_t probe_mono_slots(size_t sample_pairs) {
    mono_slot_probe_t probe = {
        .slot_abs = {0, 0},
        .suggested_slot = s_mono_slot_index,
    };
    const size_t inspect_pairs = (sample_pairs < 128) ? sample_pairs : 128;
    for (size_t i = 0; i < inspect_pairs; ++i) {
        const int32_t raw0 = s_rx_scratch[i * 2];
        const int32_t raw1 = s_rx_scratch[(i * 2) + 1];
        const int16_t dec0 =
            s_left_justified ? decode_left_just_to_i16(raw0) : decode_right_just_to_i16(raw0);
        const int16_t dec1 =
            s_left_justified ? decode_left_just_to_i16(raw1) : decode_right_just_to_i16(raw1);
        probe.slot_abs[0] += (dec0 < 0) ? -(int32_t)dec0 : (int32_t)dec0;
        probe.slot_abs[1] += (dec1 < 0) ? -(int32_t)dec1 : (int32_t)dec1;
    }
    probe.suggested_slot = (probe.slot_abs[1] > probe.slot_abs[0]) ? 1 : 0;
    return probe;
}

esp_err_t audio_i2s_in_init(const audio_i2s_in_config_t *cfg) {
    if (cfg == NULL || cfg->sample_rate_hz == 0 || cfg->bits_per_sample == 0 ||
        cfg->channels == 0) {
        return ESP_ERR_INVALID_ARG;
    }
    if (s_inited) {
        return ESP_ERR_INVALID_STATE;
    }
    const board_i2s_pin_map_t *pins = board_get_rx_i2s_pins();
    if (pins == NULL) {
        return ESP_ERR_INVALID_STATE;
    }

    i2s_chan_config_t chan_cfg = I2S_CHANNEL_DEFAULT_CONFIG(s_port, I2S_ROLE_MASTER);
    chan_cfg.dma_desc_num = 6;
    chan_cfg.dma_frame_num = 256;
    ESP_RETURN_ON_ERROR(i2s_new_channel(&chan_cfg, NULL, &s_rx_handle), TAG,
                        "i2s_new_channel failed");

    i2s_std_config_t std_cfg = {
        .clk_cfg = I2S_STD_CLK_DEFAULT_CONFIG(cfg->sample_rate_hz),
        .slot_cfg =
            I2S_STD_PHILIPS_SLOT_DEFAULT_CONFIG(I2S_DATA_BIT_WIDTH_32BIT, I2S_SLOT_MODE_STEREO),
        .gpio_cfg =
            {
                .mclk = I2S_GPIO_UNUSED,
                .bclk = pins->bclk,
                .ws = pins->ws,
                .dout = I2S_GPIO_UNUSED,
                .din = pins->din,
                .invert_flags =
                    {
                        .mclk_inv = false,
                        .bclk_inv = false,
                        .ws_inv = false,
                    },
            },
    };
    std_cfg.slot_cfg.slot_mask = I2S_STD_SLOT_BOTH;

    esp_err_t ret = i2s_channel_init_std_mode(s_rx_handle, &std_cfg);
    if (ret != ESP_OK) {
        release_rx_channel();
        return ret;
    }

    s_cfg = *cfg;
    s_inited = true;
    s_started = false;
    s_alignment_checked = s_strict_inmp441;
    s_left_justified = true;
    s_mono_slot_index = CONFIG_AUDIO_I2S_IN_FIXED_SLOT;
    s_mono_slot_selected = s_strict_inmp441;
    ESP_LOGI(TAG, "init: %u Hz, hw_slot=%u-bit, out=%u-bit, %u ch",
             (unsigned)s_cfg.sample_rate_hz, RX_SLOT_BITS, s_cfg.bits_per_sample, s_cfg.channels);
    if (s_strict_inmp441) {
        ESP_LOGI(TAG, "strict INMP441 capture: slot=%u alignment=left-justified 24-in-32",
                 (unsigned)s_mono_slot_index);
    }
    return ESP_OK;
}

esp_err_t audio_i2s_in_start(void) {
    if (!s_inited) {
        return ESP_ERR_INVALID_STATE;
    }
    if (s_started) {
        return ESP_OK;
    }
    ESP_RETURN_ON_ERROR(i2s_channel_enable(s_rx_handle), TAG, "i2s_channel_enable failed");
    s_started = true;
    ESP_LOGI(TAG, "start");
    return ESP_OK;
}

esp_err_t audio_i2s_in_stop(void) {
    if (!s_inited) {
        return ESP_ERR_INVALID_STATE;
    }
    if (!s_started) {
        return ESP_OK;
    }
    ESP_RETURN_ON_ERROR(i2s_channel_disable(s_rx_handle), TAG, "i2s_channel_disable failed");
    s_started = false;
    ESP_LOGI(TAG, "stop");
    return ESP_OK;
}

esp_err_t audio_i2s_in_read(int16_t *dst, size_t samples, uint32_t timeout_ms,
                            size_t *samples_read) {
    if (!s_started || dst == NULL || samples == 0 || samples_read == NULL) {
        return ESP_ERR_INVALID_ARG;
    }
    const size_t raw_words_requested = (s_cfg.channels == 1) ? (samples * 2) : samples;
    if (raw_words_requested > RX_SCRATCH_WORDS_MAX) {
        return ESP_ERR_INVALID_SIZE;
    }

    size_t bytes_read = 0;
    esp_err_t ret = i2s_channel_read(s_rx_handle, s_rx_scratch, raw_words_requested * sizeof(int32_t),
                                     &bytes_read, timeout_ms);
    const size_t raw_words_read = bytes_read / sizeof(int32_t);
    *samples_read = (s_cfg.channels == 1) ? (raw_words_read / 2) : raw_words_read;
    if (ret != ESP_OK) {
        return ret;
    }
    if (raw_words_read == 0 || *samples_read == 0) {
        memset(dst, 0, samples * sizeof(int16_t));
        return ESP_ERR_TIMEOUT;
    }

    if (!s_alignment_checked) {
        size_t low8_zero = 0;
        size_t sat_left = 0;
        size_t sat_right = 0;
        const size_t inspect = (raw_words_read < 128) ? raw_words_read : 128;
        for (size_t i = 0; i < inspect; ++i) {
            if ((s_rx_scratch[i] & 0xFF) == 0) {
                low8_zero++;
            }
            const int32_t l = decode_left_just_to_i16(s_rx_scratch[i]);
            const int32_t r = decode_right_just_to_i16(s_rx_scratch[i]);
            if ((l >= 32000) || (l <= -32000)) {
                sat_left++;
            }
            if ((r >= 32000) || (r <= -32000)) {
                sat_right++;
            }
        }
        if (sat_left + 3 < sat_right) {
            s_left_justified = true;
        } else if (sat_right + 3 < sat_left) {
            s_left_justified = false;
        } else {
            s_left_justified = (low8_zero * 10) >= (inspect * 9);
        }
        s_alignment_checked = true;
        ESP_LOGI(TAG, "rx alignment detected: %s (low8_zero=%u/%u sat_l=%u sat_r=%u)",
                 s_left_justified ? "left-justified 24-in-32" : "right-justified 24-in-32",
                 (unsigned)low8_zero, (unsigned)inspect, (unsigned)sat_left,
                 (unsigned)sat_right);
    }

    if (s_cfg.channels == 1) {
        uint8_t current_slot = s_mono_slot_index;
        if (!s_strict_inmp441 && raw_words_read >= 2) {
            const mono_slot_probe_t probe = probe_mono_slots(*samples_read);
            const int64_t total_abs = probe.slot_abs[0] + probe.slot_abs[1];
            if (!s_mono_slot_selected) {
                current_slot = probe.suggested_slot;
                if (total_abs > 0) {
                    s_mono_slot_index = probe.suggested_slot;
                    s_mono_slot_selected = true;
                    current_slot = s_mono_slot_index;
                    ESP_LOGI(TAG,
                             "mono slot selected: slot%u (slot0_abs=%" PRId64 " slot1_abs=%" PRId64 ")",
                             (unsigned)s_mono_slot_index, probe.slot_abs[0], probe.slot_abs[1]);
                }
            } else {
                const uint8_t other_slot = (uint8_t)(1 - s_mono_slot_index);
                if (probe.slot_abs[s_mono_slot_index] == 0 && probe.slot_abs[other_slot] > 0) {
                    s_mono_slot_index = other_slot;
                    current_slot = s_mono_slot_index;
                    ESP_LOGW(TAG,
                             "mono slot reselected: slot%u (slot0_abs=%" PRId64 " slot1_abs=%" PRId64 ")",
                             (unsigned)s_mono_slot_index, probe.slot_abs[0], probe.slot_abs[1]);
                } else {
                    current_slot = s_mono_slot_index;
                }
            }
        }
        for (size_t i = 0; i < *samples_read; ++i) {
            const int32_t raw = s_rx_scratch[(i * 2) + current_slot];
            dst[i] =
                s_left_justified ? decode_left_just_to_i16(raw) : decode_right_just_to_i16(raw);
        }
    } else {
        for (size_t i = 0; i < *samples_read; ++i) {
            if (s_left_justified) {
                dst[i] = decode_left_just_to_i16(s_rx_scratch[i]);
            } else {
                dst[i] = decode_right_just_to_i16(s_rx_scratch[i]);
            }
        }
    }
    if (*samples_read < samples) {
        memset(&dst[*samples_read], 0, (samples - *samples_read) * sizeof(int16_t));
    }

    return ESP_OK;
}
