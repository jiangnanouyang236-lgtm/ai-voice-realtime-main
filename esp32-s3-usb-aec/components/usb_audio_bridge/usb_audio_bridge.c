#include "usb_audio_bridge.h"

#include <inttypes.h>
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
#include <string.h>

#include "audio_pipeline.h"
#include "audio_ringbuf.h"
#include "esp_check.h"
#include "esp_err.h"
#include "esp_log.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/semphr.h"
#include "freertos/stream_buffer.h"
#include "sdkconfig.h"
#include "usb_device_uac.h"

static const char *TAG = "usb_audio_bridge";

/* Named constants for drift/level/ASRC (tunable via Kconfig later if needed) */
#define ASRC_OVERLAP_SIZE           64
#define ASRC_INBUF_SAMPLES          1024
#define RATE_MEASURE_MIN_US         500000
#define LEVEL_DEADBAND_SAMPLES      16
#define LEVEL_CORR_WINDOW_FRAMES    320
#define STEP_Q16_ONE_TO_ONE         65536
#define STEP_Q16_MIN                64880
#define STEP_Q16_MAX                66190
#define MAX_DRIFT_Q16               (2 << 16)
#define LEVEL_LPF_SHIFT             5
#define STEP_IIR_SHIFT              4
#define MIC_PREFILL_MS              30
#define MIC_HOST_IDLE_RESET_US      250000
#define SPK_HOST_IDLE_SILENCE_US    300000
#define MIC_DC_BLOCK_A_Q15          32604
#define SPK_STALL_IDLE_MS           3000
#define SPK_STALL_WINDOWS_THRESHOLD 2
#define SPK_STALL_RECOVER_COOLDOWN_US 8000000

#ifndef CONFIG_AUDIO_USB_UAC_ENABLE
#define CONFIG_AUDIO_USB_UAC_ENABLE 0
#endif

#ifndef CONFIG_AUDIO_USB_SPK_BUFFER_MS
#define CONFIG_AUDIO_USB_SPK_BUFFER_MS 400
#endif

#ifndef CONFIG_AUDIO_USB_UAC_INPUT_ENABLE
#define CONFIG_AUDIO_USB_UAC_INPUT_ENABLE 0
#endif

#ifndef CONFIG_AUDIO_USB_MIC_BUFFER_MS
#define CONFIG_AUDIO_USB_MIC_BUFFER_MS 400
#endif

#ifndef CONFIG_AUDIO_USB_LOG_INTERVAL_SEC
#define CONFIG_AUDIO_USB_LOG_INTERVAL_SEC 2
#endif

#ifndef CONFIG_AUDIO_USB_DISABLE_DRIFT_COMP
#define CONFIG_AUDIO_USB_DISABLE_DRIFT_COMP 0
#endif

#ifndef CONFIG_AUDIO_ENABLE_AFE
#define CONFIG_AUDIO_ENABLE_AFE 0
#endif

#ifndef CONFIG_AUDIO_AEC_ENABLE
#define CONFIG_AUDIO_AEC_ENABLE 0
#endif

#ifndef CONFIG_AUDIO_USB_MIC_GAIN_DB
#define CONFIG_AUDIO_USB_MIC_GAIN_DB 8
#endif

/* Mic ring buffer: 120ms @ 48kHz mono int16 = 48*120*2 = 11520 bytes */
#define MIC_RINGBUF_CAPACITY_BYTES  11520
static audio_ringbuf_t s_mic_ringbuf;
static uint8_t s_mic_ringbuf_storage[MIC_RINGBUF_CAPACITY_BYTES];

/* Mic state: module-level statics (no mutex needed — single writer + portMUX ring buffer) */
static volatile bool s_mic_host_active;
static bool s_mic_prefill_ready;
static int64_t s_last_mic_read_us;
static size_t s_mic_prefill_bytes;
static int16_t s_mic_dc_prev_in;
static int32_t s_mic_dc_prev_out;
static int32_t s_mic_eq_low_state;
static int32_t s_mic_eq_mid_state;
static int32_t s_mic_gain_q15;

/* Precomputed Q15 gain from dB: gain_q15 = (int32_t)(pow(10, dB/20) * 32768).
 * Applied with saturation to int16_t range. */
static int32_t mic_gain_q15_from_db(int db) {
    /* Table for 0..30 dB in 1 dB steps (rounded). */
    static const int32_t table[] = {
        32768,  36766,  41253,  46286,  51933,  58268,  65381,  73362,
        82312,  92354,  103617, 116248, 130403, 146340, 164241, 184299,
        206734, 231913, 260157, 291942, 327680, 367628, 412527, 462864,
        519326, 582679, 653815, 733625, 823124, 923541, 1036178
    };
    if (db < 0) db = 0;
    if (db > 30) db = 30;
    return table[db];
}

/* ASRC state passed in/out of asrc_resample to avoid holding bridge mutex during resampling */
typedef struct {
    uint32_t phase_q16;
    int16_t overlap[ASRC_OVERLAP_SIZE];
    size_t overlap_count;
} asrc_state_t;

typedef struct {
    bool spk_enabled;
    StreamBufferHandle_t spk_stream;
    size_t spk_bytes_per_ms;
    size_t mic_bytes_per_ms;
    size_t spk_stream_capacity;
    size_t spk_prefill_bytes;
    size_t spk_target_bytes;
    size_t spk_low_wm_bytes;
    size_t spk_high_wm_bytes;
    volatile bool host_mute_runtime;
    volatile uint32_t host_volume_runtime;
    int32_t playback_gain_q15;
    int16_t last_playback_sample;
    int64_t rate_measure_start_us;
    uint32_t rate_measure_start_bytes;
    int32_t measured_drift_q16;
    uint32_t asrc_phase_q16;
    int16_t asrc_overlap[ASRC_OVERLAP_SIZE];
    size_t asrc_overlap_count;
    uint32_t asrc_last_step_q16;
    uint32_t asrc_smooth_step_q16;
    int32_t level_err_filt_q8;
    int64_t last_rx_us;
    uint32_t spk_watchdog_last_rx_bytes;
    uint8_t spk_watchdog_stall_windows;
    int64_t spk_watchdog_last_recover_us;
    uint32_t spk_watchdog_recover_rx_bytes;  /* rx_bytes at last recover; 0 = none yet */
    bool spk_watchdog_recover_ineffective;   /* true = last recover had no effect */
    uint32_t spk_session_rx_pcm_peak;        /* peak of all rx PCM since last reset */
    esp_timer_handle_t log_timer;
    uint32_t last_spk_avg_abs;
    uint32_t last_spk_peak;
    uint32_t last_rx_pcm_avg_abs;
    uint32_t last_rx_pcm_peak;
    uint32_t last_tx_pcm_avg_abs;
    uint32_t last_tx_pcm_peak;
    uint32_t diag_lvl_min;
    uint32_t diag_lvl_max;
    uint32_t diag_mic_lvl_min;
    uint32_t diag_mic_lvl_max;
    uint32_t diag_step_min;
    uint32_t diag_step_max;
    uint32_t diag_step_delta_max;
    uint32_t diag_step_update_count;
    uint32_t diag_spk_peak_max;
    uint32_t diag_rx_pcm_peak_max;
    uint32_t diag_tx_pcm_peak_max;
    uint32_t diag_tx_pcm_clip_samples;
    uint32_t diag_tx_pcm_repeat_chunks;
    uint32_t diag_conceal_frames;
    uint32_t diag_conceal_samples;
    uint32_t diag_reset_count;
    uint32_t diag_step_clamp_min_hits;
    uint32_t diag_step_clamp_max_hits;
    uint32_t tx_prev_hash;
    size_t tx_prev_samples;
    bool tx_prev_valid;
    uint32_t mic_last_frame_seq;
    bool inited;
    bool input_enabled;
    bool playback_started;
    bool rate_measured;
    usb_audio_bridge_stats_t stats;
} usb_audio_ctx_t;

static usb_audio_ctx_t s_ctx;
/* Protects s_ctx shared between uac_output_cb and usb_playback_provider.
 * If uac_output_cb is ever invoked from ISR, replace mutex with a volatile
 * "request_reset" flag and perform reset in the provider. */
static SemaphoreHandle_t s_bridge_mutex;
#define ASRC_NEED_RESET ((size_t)-1)

#if CONFIG_AUDIO_USB_UAC_ENABLE

static size_t min_size(size_t a, size_t b) {
    return (a < b) ? a : b;
}

/* Clear playback/ASRC/rate/level state. Call with s_bridge_mutex held.
 * Caller is responsible for xStreamBufferReset if the stream was corrupted. */
static void reset_playback_state(void) {
    s_ctx.playback_started = false;
    s_ctx.asrc_phase_q16 = 0;
    s_ctx.asrc_overlap_count = 0;
    s_ctx.playback_gain_q15 = 0;
    s_ctx.last_playback_sample = 0;
    s_ctx.rate_measure_start_us = 0;
    s_ctx.rate_measure_start_bytes = 0;
    s_ctx.measured_drift_q16 = 0;
    s_ctx.level_err_filt_q8 = 0;
    s_ctx.asrc_last_step_q16 = STEP_Q16_ONE_TO_ONE;
    s_ctx.asrc_smooth_step_q16 = STEP_Q16_ONE_TO_ONE;
    s_ctx.rate_measured = false;
    s_ctx.spk_session_rx_pcm_peak = 0;
}

static void trigger_speaker_stream_recover(void) {
    if (s_bridge_mutex != NULL && xSemaphoreTake(s_bridge_mutex, pdMS_TO_TICKS(20)) == pdTRUE) {
        if (s_ctx.spk_stream != NULL) {
            (void)xStreamBufferReset(s_ctx.spk_stream);
        }
        reset_playback_state();
        s_ctx.last_rx_us = 0;
        xSemaphoreGive(s_bridge_mutex);
    }
    esp_err_t ret = uac_device_recover_streams(true, false);
    if (ret != ESP_OK) {
        ESP_LOGW(TAG, "spk watchdog recover request failed: %s", esp_err_to_name(ret));
    }
}

static size_t align_even_down(size_t bytes) {
    return bytes & ~(size_t)0x1U;
}

static size_t align_even_up(size_t bytes) {
    return (bytes + 0x1U) & ~(size_t)0x1U;
}

static int16_t clamp_i16(int32_t v) {
    if (v > 32767) {
        return 32767;
    }
    if (v < -32768) {
        return -32768;
    }
    return (int16_t)v;
}

static int16_t apply_gain_q15(int16_t sample, int32_t gain_q15) {
    const int32_t scaled = (((int32_t)sample * gain_q15) + (1 << 14)) >> 15;
    return clamp_i16(scaled);
}

static void compute_pcm_frame_stats(const int16_t *buf, size_t samples, uint32_t *avg_abs, uint32_t *peak) {
    if (avg_abs == NULL || peak == NULL) {
        return;
    }
    if (buf == NULL || samples == 0) {
        *avg_abs = 0;
        *peak = 0;
        return;
    }

    uint64_t sum_abs = 0;
    uint32_t local_peak = 0;
    for (size_t i = 0; i < samples; ++i) {
        uint32_t mag = (buf[i] < 0) ? (uint32_t)(-(int32_t)buf[i]) : (uint32_t)buf[i];
        sum_abs += mag;
        if (mag > local_peak) {
            local_peak = mag;
        }
    }

    *avg_abs = (uint32_t)(sum_abs / samples);
    *peak = local_peak;
}

static void update_playback_frame_stats(const int16_t *buf, size_t samples) {
    compute_pcm_frame_stats(buf, samples, &s_ctx.last_spk_avg_abs, &s_ctx.last_spk_peak);
}

static void reset_diag_window(void) {
    s_ctx.diag_lvl_min = UINT32_MAX;
    s_ctx.diag_lvl_max = 0;
    s_ctx.diag_mic_lvl_min = UINT32_MAX;
    s_ctx.diag_mic_lvl_max = 0;
    s_ctx.diag_step_min = UINT32_MAX;
    s_ctx.diag_step_max = 0;
    s_ctx.diag_step_delta_max = 0;
    s_ctx.diag_step_update_count = 0;
    s_ctx.diag_spk_peak_max = 0;
    s_ctx.diag_rx_pcm_peak_max = 0;
    s_ctx.diag_tx_pcm_peak_max = 0;
    s_ctx.diag_tx_pcm_clip_samples = 0;
    s_ctx.diag_tx_pcm_repeat_chunks = 0;
    s_ctx.diag_conceal_frames = 0;
    s_ctx.diag_conceal_samples = 0;
    s_ctx.diag_reset_count = 0;
    s_ctx.diag_step_clamp_min_hits = 0;
    s_ctx.diag_step_clamp_max_hits = 0;
}

static void reset_mic_dsp_state(void) {
    s_mic_dc_prev_in = 0;
    s_mic_dc_prev_out = 0;
    s_mic_eq_low_state = 0;
    s_mic_eq_mid_state = 0;
}

static void diag_note_level(uint32_t level) {
    if (level < s_ctx.diag_lvl_min) {
        s_ctx.diag_lvl_min = level;
    }
    if (level > s_ctx.diag_lvl_max) {
        s_ctx.diag_lvl_max = level;
    }
}

static void diag_note_mic_level(uint32_t level) {
    if (level < s_ctx.diag_mic_lvl_min) {
        s_ctx.diag_mic_lvl_min = level;
    }
    if (level > s_ctx.diag_mic_lvl_max) {
        s_ctx.diag_mic_lvl_max = level;
    }
}

static void diag_note_step(uint32_t prev_step_q16, uint32_t step_q16) {
    s_ctx.diag_step_update_count++;
    if (step_q16 < s_ctx.diag_step_min) {
        s_ctx.diag_step_min = step_q16;
    }
    if (step_q16 > s_ctx.diag_step_max) {
        s_ctx.diag_step_max = step_q16;
    }
    const uint32_t delta = (step_q16 > prev_step_q16) ? (step_q16 - prev_step_q16) : (prev_step_q16 - step_q16);
    if (delta > s_ctx.diag_step_delta_max) {
        s_ctx.diag_step_delta_max = delta;
    }
    if (step_q16 <= STEP_Q16_MIN) {
        s_ctx.diag_step_clamp_min_hits++;
    }
    if (step_q16 >= STEP_Q16_MAX) {
        s_ctx.diag_step_clamp_max_hits++;
    }
}

static void diag_note_peak(uint32_t peak) {
    if (peak > s_ctx.diag_spk_peak_max) {
        s_ctx.diag_spk_peak_max = peak;
    }
}

static void diag_note_rx_pcm_peak(uint32_t peak) {
    if (peak > s_ctx.diag_rx_pcm_peak_max) {
        s_ctx.diag_rx_pcm_peak_max = peak;
    }
}

static uint32_t hash_pcm_frame(const int16_t *buf, size_t samples) {
    uint32_t h = 2166136261u;
    for (size_t i = 0; i < samples; ++i) {
        uint16_t v = (uint16_t)buf[i];
        h ^= (uint8_t)(v & 0xFFu);
        h *= 16777619u;
        h ^= (uint8_t)(v >> 8);
        h *= 16777619u;
    }
    return h;
}

static void diag_note_tx_pcm(const int16_t *buf, size_t samples) {
    if (buf == NULL || samples == 0) {
        s_ctx.last_tx_pcm_avg_abs = 0;
        s_ctx.last_tx_pcm_peak = 0;
        return;
    }

    compute_pcm_frame_stats(buf, samples, &s_ctx.last_tx_pcm_avg_abs, &s_ctx.last_tx_pcm_peak);
    if (s_ctx.last_tx_pcm_peak > s_ctx.diag_tx_pcm_peak_max) {
        s_ctx.diag_tx_pcm_peak_max = s_ctx.last_tx_pcm_peak;
    }

    uint32_t clip = 0;
    for (size_t i = 0; i < samples; ++i) {
        if (buf[i] == INT16_MIN || buf[i] == INT16_MAX) {
            clip++;
        }
    }
    s_ctx.diag_tx_pcm_clip_samples += clip;

    if (s_ctx.last_tx_pcm_peak > 32U) {
        const uint32_t hash = hash_pcm_frame(buf, samples);
        if (s_ctx.tx_prev_valid && s_ctx.tx_prev_samples == samples && s_ctx.tx_prev_hash == hash) {
            s_ctx.diag_tx_pcm_repeat_chunks++;
        }
        s_ctx.tx_prev_hash = hash;
        s_ctx.tx_prev_samples = samples;
        s_ctx.tx_prev_valid = true;
    }
}

static void diag_note_conceal(size_t concealed_samples) {
    if (concealed_samples == 0) {
        return;
    }
    s_ctx.diag_conceal_frames++;
    s_ctx.diag_conceal_samples += (uint32_t)concealed_samples;
}

static void diag_note_reset(void) {
    s_ctx.diag_reset_count++;
}

static void diag_note_mic_frame_seq(uint32_t frame_seq) {
    if (frame_seq == 0) {
        return;
    }
    if (s_ctx.mic_last_frame_seq != 0) {
        if (frame_seq == s_ctx.mic_last_frame_seq) {
            s_ctx.stats.mic_frame_seq_repeat++;
        } else if (frame_seq < s_ctx.mic_last_frame_seq) {
            s_ctx.stats.mic_frame_seq_backtrack++;
        }
    }
    s_ctx.mic_last_frame_seq = frame_seq;
}

static void maybe_log_u2_anomaly(const usb_audio_bridge_stats_t *prev,
                                 const usb_audio_bridge_stats_t *curr,
                                 uint32_t clip_samples,
                                 uint32_t repeat_chunks,
                                 uint32_t mic_level) {
    if (prev == NULL || curr == NULL) {
        return;
    }

    const uint32_t tx_underrun_delta =
        curr->usb_tx_underrun_calls - prev->usb_tx_underrun_calls;
    const uint32_t tx_ovf_delta =
        curr->usb_tx_overrun_events - prev->usb_tx_overrun_events;
    const uint32_t afe_drop_delta =
        curr->afe_out_drop_bytes - prev->afe_out_drop_bytes;

    if (tx_underrun_delta == 0 && tx_ovf_delta == 0 && afe_drop_delta == 0 &&
        clip_samples == 0 && repeat_chunks == 0) {
        return;
    }

    ESP_LOGW(TAG,
             "u2_anom tx_underrun=+%" PRIu32 " tx_ovf=+%" PRIu32
             " afe_drop=+%" PRIu32 " clip=%" PRIu32 " rep=%" PRIu32
             " micq=%" PRIu32,
             tx_underrun_delta, tx_ovf_delta, afe_drop_delta, clip_samples,
             repeat_chunks, mic_level);
}

// Continuous fractional resampler (ASRC): reads from StreamBuffer, writes state to
// *state so caller can hold mutex only around copy in/out. Returns produced count
// or ASRC_NEED_RESET on unaligned stream (caller must reset stream and state).
// step_q16: 65536 = 1:1, >65536 = consume faster (stretch).
static size_t asrc_resample(int16_t *dst, size_t out_samples, uint32_t step_q16,
                            StreamBufferHandle_t stream, asrc_state_t *state) {
    if (dst == NULL || out_samples == 0 || stream == NULL || state == NULL) {
        return 0;
    }

    const size_t overlap = state->overlap_count;
    const size_t max_input = (size_t)(((uint64_t)out_samples * step_q16 + 0xFFFF) >> 16) + 2;
    const size_t fresh_need = (max_input > overlap) ? (max_input - overlap) : 0;

    int16_t inbuf[ASRC_INBUF_SAMPLES];
    if (overlap > 0) {
        memcpy(inbuf, state->overlap, overlap * sizeof(int16_t));
    }

    const size_t inbuf_capacity = sizeof(inbuf) / sizeof(int16_t);
    const size_t read_max = min_size(fresh_need, inbuf_capacity - overlap);
    size_t got_bytes = 0;
    const size_t want_bytes = read_max * sizeof(int16_t);
    while (got_bytes < want_bytes) {
        size_t chunk = xStreamBufferReceive(stream,
                                            ((uint8_t *)&inbuf[overlap]) + got_bytes,
                                            want_bytes - got_bytes, 0);
        if (chunk == 0) {
            break;
        }
        if ((chunk & 0x1U) != 0U) {
            ESP_LOGW(TAG, "unaligned speaker stream chunk=%u", (unsigned)chunk);
            return ASRC_NEED_RESET;
        }
        got_bytes += chunk;
    }
    const size_t fresh_got = got_bytes / sizeof(int16_t);
    const size_t in_total = overlap + fresh_got;

    if (in_total < 2) {
        memset(dst, 0, out_samples * sizeof(int16_t));
        state->overlap_count = 0;
        state->phase_q16 = 0;
        return 0;
    }

    uint32_t phase = state->phase_q16;
    size_t produced = 0;
    for (; produced < out_samples; ++produced) {
        const size_t idx = phase >> 16;
        if (idx + 1 >= in_total) {
            break;
        }
        const uint32_t frac = phase & 0xFFFF;
        const int32_t a = inbuf[idx];
        const int32_t b = inbuf[idx + 1];
        const int64_t delta = (int64_t)(b - a) * (int64_t)frac;
        dst[produced] = (int16_t)(a + (int32_t)(delta >> 16));
        phase += step_q16;
    }

    if (produced < out_samples) {
        const int16_t last = (in_total > 0) ? inbuf[in_total - 1] : 0;
        for (size_t i = produced; i < out_samples; ++i) {
            dst[i] = last;
        }
    }

    const size_t consumed = phase >> 16;
    state->phase_q16 = phase - (consumed << 16);

    if (consumed < in_total) {
        const size_t leftover = in_total - consumed;
        const size_t keep = min_size(leftover, (size_t)ASRC_OVERLAP_SIZE);
        memmove(state->overlap, &inbuf[consumed], keep * sizeof(int16_t));
        state->overlap_count = keep;
    } else {
        state->overlap_count = 0;
    }

    return produced;
}

static void apply_playback_control(int16_t *buf, size_t samples) {
    if (buf == NULL || samples == 0) {
        return;
    }

    int32_t vol = (int32_t)s_ctx.host_volume_runtime;
    if (vol < 0) {
        vol = 0;
    } else if (vol > 100) {
        vol = 100;
    }
    /* Standard UAC volume mapping: host 100% is PCM unity gain.  Board-level
     * loudness must be set by the host and MAX98357A gain wiring, not by a
     * hidden fixed -18 dB attenuation in the digital path. */
    const int32_t target_gain_q15 = s_ctx.host_mute_runtime ? 0 : (vol * 32767) / 100;

    const int32_t start_gain_q15 = s_ctx.playback_gain_q15;
    int32_t delta_gain_q15 = target_gain_q15 - start_gain_q15;

    // Limit gain change per frame to reduce click/pop noise when host volume
    // jumps. At startup (gain=0 → unity), this also masks ~500ms of potential
    // USB init noise: 32767 Q15 / 655 per 10ms frame ≈ 50 frames = 500ms.
    const int32_t kMaxDeltaPerFrameQ15 = 655;
    if (delta_gain_q15 > kMaxDeltaPerFrameQ15) {
        delta_gain_q15 = kMaxDeltaPerFrameQ15;
    } else if (delta_gain_q15 < -kMaxDeltaPerFrameQ15) {
        delta_gain_q15 = -kMaxDeltaPerFrameQ15;
    }
    const int32_t end_gain_q15 = start_gain_q15 + delta_gain_q15;

    if (samples == 1) {
        buf[0] = apply_gain_q15(buf[0], end_gain_q15);
    } else {
        for (size_t i = 0; i < samples; ++i) {
            const int32_t gain_q15 =
                start_gain_q15 + (int32_t)(((int64_t)delta_gain_q15 * (int64_t)i) / (int64_t)(samples - 1));
            buf[i] = apply_gain_q15(buf[i], gain_q15);
        }
    }
    s_ctx.playback_gain_q15 = end_gain_q15;
}

static void log_stats_timer_cb(void *arg) {
    (void)arg;
    static usb_audio_bridge_stats_t prev_stats = { 0 };
    int64_t idle_ms = -1;
    uint32_t level = 0;
    uint32_t mic_level = 0;
    int32_t drift_sps = 0;
    usb_audio_bridge_stats_t st = { 0 };
    uint32_t asrc_step = 0;
    uint32_t mic_asrc_step = STEP_Q16_ONE_TO_ONE;
    uint32_t last_avg = 0;
    uint32_t last_peak = 0;
    uint32_t last_rx_avg = 0;
    uint32_t last_rx_peak = 0;
    uint32_t last_tx_avg = 0;
    uint32_t last_tx_peak = 0;
    uint32_t diag_lvl_min = 0;
    uint32_t diag_lvl_max = 0;
    uint32_t diag_mic_lvl_min = 0;
    uint32_t diag_mic_lvl_max = 0;
    uint32_t diag_step_min = 0;
    uint32_t diag_step_max = 0;
    uint32_t diag_step_delta_max = 0;
    uint32_t diag_step_update_count = 0;
    uint32_t diag_spk_peak_max = 0;
    uint32_t diag_rx_pcm_peak_max = 0;
    uint32_t diag_tx_pcm_peak_max = 0;
    uint32_t diag_tx_pcm_clip_samples = 0;
    uint32_t diag_tx_pcm_repeat_chunks = 0;
    uint32_t diag_conceal_frames = 0;
    uint32_t diag_conceal_samples = 0;
    uint32_t diag_reset_count = 0;
    uint32_t diag_step_clamp_min_hits = 0;
    uint32_t diag_step_clamp_max_hits = 0;
    uint32_t mic_host_active = s_mic_host_active ? 1U : 0U;
    bool trigger_spk_recover = false;
    int64_t now_us = esp_timer_get_time();

    /* Mic level from ring buffer (no mutex needed) */
    mic_level = (uint32_t)audio_ringbuf_level_bytes(&s_mic_ringbuf);

    /* Speaker stats need mutex if speaker is active */
    if (s_bridge_mutex != NULL && xSemaphoreTake(s_bridge_mutex, pdMS_TO_TICKS(20)) == pdTRUE) {
        if (s_ctx.last_rx_us > 0) {
            idle_ms = (now_us - s_ctx.last_rx_us) / 1000;
        }
        level = (s_ctx.spk_stream != NULL) ? (uint32_t)xStreamBufferBytesAvailable(s_ctx.spk_stream) : 0U;
        diag_note_level(level);
        diag_note_mic_level(mic_level);
        diag_note_peak(s_ctx.last_spk_peak);
        drift_sps = (int32_t)((s_ctx.measured_drift_q16 * (int64_t)100) >> 16);
        s_ctx.stats.asrc_step_q16 = s_ctx.asrc_last_step_q16;
        st = s_ctx.stats;
        asrc_step = s_ctx.asrc_last_step_q16;
        last_avg = s_ctx.last_spk_avg_abs;
        last_peak = s_ctx.last_spk_peak;
        last_rx_avg = s_ctx.last_rx_pcm_avg_abs;
        last_rx_peak = s_ctx.last_rx_pcm_peak;
        last_tx_avg = s_ctx.last_tx_pcm_avg_abs;
        last_tx_peak = s_ctx.last_tx_pcm_peak;
        diag_lvl_min = (s_ctx.diag_lvl_min == UINT32_MAX) ? level : s_ctx.diag_lvl_min;
        diag_lvl_max = s_ctx.diag_lvl_max;
        diag_mic_lvl_min = (s_ctx.diag_mic_lvl_min == UINT32_MAX) ? mic_level : s_ctx.diag_mic_lvl_min;
        diag_mic_lvl_max = s_ctx.diag_mic_lvl_max;
        diag_step_min = (s_ctx.diag_step_min == UINT32_MAX) ? asrc_step : s_ctx.diag_step_min;
        diag_step_max = s_ctx.diag_step_max;
        diag_step_delta_max = s_ctx.diag_step_delta_max;
        diag_step_update_count = s_ctx.diag_step_update_count;
        if (diag_step_update_count == 0) {
            diag_step_min = 0;
            diag_step_max = 0;
            diag_step_delta_max = 0;
        }
        diag_spk_peak_max = s_ctx.diag_spk_peak_max;
        diag_rx_pcm_peak_max = s_ctx.diag_rx_pcm_peak_max;
        diag_tx_pcm_peak_max = s_ctx.diag_tx_pcm_peak_max;
        diag_tx_pcm_clip_samples = s_ctx.diag_tx_pcm_clip_samples;
        diag_tx_pcm_repeat_chunks = s_ctx.diag_tx_pcm_repeat_chunks;
        diag_conceal_frames = s_ctx.diag_conceal_frames;
        diag_conceal_samples = s_ctx.diag_conceal_samples;
        diag_reset_count = s_ctx.diag_reset_count;
        diag_step_clamp_min_hits = s_ctx.diag_step_clamp_min_hits;
        diag_step_clamp_max_hits = s_ctx.diag_step_clamp_max_hits;
        maybe_log_u2_anomaly(&prev_stats, &st, diag_tx_pcm_clip_samples,
                             diag_tx_pcm_repeat_chunks, mic_level);
        prev_stats = st;

        if (s_ctx.spk_enabled) {
            if (st.usb_rx_bytes != s_ctx.spk_watchdog_last_rx_bytes) {
                s_ctx.spk_watchdog_last_rx_bytes = st.usb_rx_bytes;
                s_ctx.spk_watchdog_stall_windows = 0;
                /* New data arrived — previous recover (if any) was effective,
                 * or the host simply resumed. Clear ineffective flag. */
                s_ctx.spk_watchdog_recover_ineffective = false;
            } else {
                const bool likely_stalled =
                    (st.usb_rx_bytes > 0U) &&
                    (idle_ms >= SPK_STALL_IDLE_MS) &&
                    (level == 0U) &&
                    (!st.host_mute) &&
                    /* Only recover if we actually received non-silent audio;
                     * a host that sends all-zero PCM (enumeration probe) or
                     * has already closed the stream should not trigger recovery. */
                    (s_ctx.spk_session_rx_pcm_peak > 32U);
                if (likely_stalled) {
                    if (s_ctx.spk_watchdog_stall_windows < UINT8_MAX) {
                        s_ctx.spk_watchdog_stall_windows++;
                    }
                } else {
                    s_ctx.spk_watchdog_stall_windows = 0;
                }
            }
            if (s_ctx.spk_watchdog_stall_windows >= SPK_STALL_WINDOWS_THRESHOLD &&
                (now_us - s_ctx.spk_watchdog_last_recover_us) >= SPK_STALL_RECOVER_COOLDOWN_US &&
                !s_ctx.spk_watchdog_recover_ineffective) {
                /* If a previous recover didn't bring new rx_bytes, don't retry —
                 * the host has genuinely stopped sending. */
                if (s_ctx.spk_watchdog_recover_rx_bytes > 0U &&
                    st.usb_rx_bytes == s_ctx.spk_watchdog_recover_rx_bytes) {
                    ESP_LOGW(TAG,
                             "spk watchdog: previous recover had no effect (rx still %" PRIu32
                             "), suppressing further recoveries",
                             st.usb_rx_bytes);
                    s_ctx.spk_watchdog_recover_ineffective = true;
                    s_ctx.spk_watchdog_stall_windows = 0;
                } else {
                    ESP_LOGW(TAG,
                             "spk watchdog: stalled stream detected rx=%" PRIu32 " lvl=%" PRIu32
                             " idle_ms=%" PRId64 " peak=%" PRIu32 " windows=%u, auto-recover",
                             st.usb_rx_bytes, level, idle_ms,
                             s_ctx.spk_session_rx_pcm_peak,
                             (unsigned)s_ctx.spk_watchdog_stall_windows);
                    s_ctx.spk_watchdog_last_recover_us = now_us;
                    s_ctx.spk_watchdog_recover_rx_bytes = st.usb_rx_bytes;
                    s_ctx.spk_watchdog_stall_windows = 0;
                    trigger_spk_recover = true;
                }
            }
        }
        reset_diag_window();
        xSemaphoreGive(s_bridge_mutex);
    } else {
        /* No mutex (speaker disabled) — read stats directly (small race OK for diagnostics) */
        st = s_ctx.stats;
        last_tx_avg = s_ctx.last_tx_pcm_avg_abs;
        last_tx_peak = s_ctx.last_tx_pcm_peak;
        diag_tx_pcm_peak_max = s_ctx.diag_tx_pcm_peak_max;
        diag_tx_pcm_clip_samples = s_ctx.diag_tx_pcm_clip_samples;
        diag_tx_pcm_repeat_chunks = s_ctx.diag_tx_pcm_repeat_chunks;
        diag_mic_lvl_min = mic_level;
        diag_mic_lvl_max = mic_level;
    }
    ESP_LOGI(TAG,
             "stats rx=%" PRIu32 " rx_drop=%" PRIu32 " rx_ovf=%" PRIu32
             " play=%" PRIu32 " underrun=%" PRIu32 " step=%" PRIu32
             " tx=%" PRIu32 " tx_underrun=%" PRIu32 " tx_ovf=%" PRIu32
             " afe=%" PRIu32 " afe_drop=%" PRIu32
             " hact=%u hopen=%" PRIu32 " hreset=%" PRIu32
             " mute=%u vol=%" PRIu32 " lvl=%" PRIu32 "[%" PRIu32 ",%" PRIu32 "]"
             " micq=%" PRIu32 "[%" PRIu32 ",%" PRIu32 "] drift=%" PRId32
             " mstep=%" PRIu32
             " stepw=[%" PRIu32 ",%" PRIu32 "] stepu=%" PRIu32 " dstep=%" PRIu32 " clamp=%" PRIu32 "/%" PRIu32
             " conceal=%" PRIu32 "/%" PRIu32 " reset=%" PRIu32
             " spk(avg=%" PRIu32 " peak=%" PRIu32 " win=%" PRIu32 ")"
             " rxpcm(avg=%" PRIu32 " peak=%" PRIu32 " win=%" PRIu32 ")"
             " txpcm(avg=%" PRIu32 " peak=%" PRIu32 " win=%" PRIu32 " clip=%" PRIu32 " rep=%" PRIu32 ")"
             " seq(rep=%" PRIu32 " back=%" PRIu32 ")"
             " idle_ms=%" PRId64,
             st.usb_rx_bytes, st.usb_rx_drop_bytes,
             st.usb_rx_overrun_events, st.playback_bytes,
             st.playback_underrun_frames, asrc_step,
             st.usb_tx_bytes, st.usb_tx_underrun_calls,
             st.usb_tx_overrun_events, st.afe_out_bytes,
             st.afe_out_drop_bytes,
             mic_host_active,
             st.mic_host_open_events, st.mic_host_idle_resets,
             st.host_mute ? 1U : 0U,
             st.host_volume_percent, level, diag_lvl_min, diag_lvl_max,
             mic_level, diag_mic_lvl_min, diag_mic_lvl_max, drift_sps,
             mic_asrc_step,
             diag_step_min, diag_step_max, diag_step_update_count, diag_step_delta_max,
             diag_step_clamp_min_hits, diag_step_clamp_max_hits,
             diag_conceal_frames, diag_conceal_samples, diag_reset_count,
             last_avg, last_peak, diag_spk_peak_max,
             last_rx_avg, last_rx_peak, diag_rx_pcm_peak_max,
             last_tx_avg, last_tx_peak, diag_tx_pcm_peak_max,
             diag_tx_pcm_clip_samples, diag_tx_pcm_repeat_chunks,
             st.mic_frame_seq_repeat, st.mic_frame_seq_backtrack,
             idle_ms);

    if (trigger_spk_recover) {
        trigger_speaker_stream_recover();
    }
}

/* Returns bytes dropped. If out_did_reset is non-NULL, set to true when stream was reset. */
static size_t discard_oldest_bytes(StreamBufferHandle_t stream, size_t bytes, bool *out_did_reset) {
    uint8_t trash[128];
    size_t dropped = 0;
    if (stream == NULL) {
        return 0;
    }
    if (out_did_reset != NULL) {
        *out_did_reset = false;
    }
    bytes = align_even_up(bytes);
    while (bytes > 0) {
        const size_t want = align_even_down(min_size(bytes, sizeof(trash)));
        if (want == 0) {
            break;
        }
        const size_t got = xStreamBufferReceive(stream, trash, want, 0);
        if (got == 0) {
            break;
        }
        if ((got & 0x1U) != 0U) {
            ESP_LOGW(TAG, "unaligned stream discard=%u, resetting stream", (unsigned)got);
            diag_note_reset();
            (void)xStreamBufferReset(stream);
            if (out_did_reset != NULL) {
                *out_did_reset = true;
            }
            return dropped;
        }
        dropped += got;
        bytes -= got;
    }
    return dropped;
}

static esp_err_t uac_output_cb(uint8_t *buf, size_t len, void *cb_ctx) {
    (void)cb_ctx;
    if (buf == NULL || len == 0 || s_ctx.spk_stream == NULL || s_bridge_mutex == NULL) {
        return ESP_OK;
    }

    len = align_even_down(len);
    if (len == 0) {
        return ESP_OK;
    }

    if (xSemaphoreTake(s_bridge_mutex, pdMS_TO_TICKS(50)) != pdTRUE) {
        return ESP_OK;
    }

    s_ctx.last_rx_us = esp_timer_get_time();
    s_ctx.stats.usb_rx_bytes += (uint32_t)len;
    compute_pcm_frame_stats((const int16_t *)buf, len / sizeof(int16_t),
                            &s_ctx.last_rx_pcm_avg_abs, &s_ctx.last_rx_pcm_peak);
    diag_note_rx_pcm_peak(s_ctx.last_rx_pcm_peak);
    if (s_ctx.last_rx_pcm_peak > s_ctx.spk_session_rx_pcm_peak) {
        s_ctx.spk_session_rx_pcm_peak = s_ctx.last_rx_pcm_peak;
    }

    const size_t free_bytes = xStreamBufferSpacesAvailable(s_ctx.spk_stream);
    if (free_bytes < len) {
        /* Overrun: prefer new data over old (drop oldest to make room). */
        const size_t need_drop = align_even_up(len - free_bytes);
        bool did_reset = false;
        const size_t dropped = discard_oldest_bytes(s_ctx.spk_stream, need_drop, &did_reset);
        s_ctx.stats.usb_rx_drop_bytes += (uint32_t)dropped;
        if (dropped > 0) {
            s_ctx.stats.usb_rx_overrun_events++;
        }
        if (did_reset) {
            reset_playback_state();
        }
    }

    const size_t accepted = xStreamBufferSend(s_ctx.spk_stream, buf, len, 0);
    if (accepted != len) {
        if ((accepted & 0x1U) != 0U) {
            ESP_LOGW(TAG, "unaligned speaker stream send=%u/%u, resetting stream",
                     (unsigned)accepted, (unsigned)len);
            diag_note_reset();
            (void)xStreamBufferReset(s_ctx.spk_stream);
            reset_playback_state();
            s_ctx.stats.usb_rx_drop_bytes += (uint32_t)len;
            s_ctx.stats.usb_rx_overrun_events++;
            xSemaphoreGive(s_bridge_mutex);
            return ESP_OK;
        }
        s_ctx.stats.usb_rx_drop_bytes += (uint32_t)(len - accepted);
        s_ctx.stats.usb_rx_overrun_events++;
    }
    diag_note_level((uint32_t)xStreamBufferBytesAvailable(s_ctx.spk_stream));

    xSemaphoreGive(s_bridge_mutex);
    return ESP_OK;
}

static void uac_set_mute_cb(uint32_t mute, void *cb_ctx) {
    (void)cb_ctx;
    const bool muted = (mute != 0);
    s_ctx.stats.host_mute = muted;
    s_ctx.host_mute_runtime = muted;
}

static void uac_set_volume_cb(uint32_t volume, void *cb_ctx) {
    (void)cb_ctx;
    uint32_t vol = volume;
    if (vol > 100U) {
        vol = 100U;
    }
    s_ctx.stats.host_volume_percent = vol;
    s_ctx.host_volume_runtime = vol;
}

static size_t usb_playback_provider(int16_t *dst, size_t samples, void *ctx) {
    (void)ctx;
    if (dst == NULL || samples == 0 || s_ctx.spk_stream == NULL || s_bridge_mutex == NULL) {
        return 0;
    }

    const size_t need_bytes = samples * sizeof(int16_t);

    /* Single mutex region: take once, do all shared-state work, release once.
     * This replaces the previous 5-6 take/give cycles per frame (~500+
     * scheduler interventions per second) with exactly one. */
    if (xSemaphoreTake(s_bridge_mutex, portMAX_DELAY) != pdTRUE) {
        memset(dst, 0, need_bytes);
        return samples;
    }

    /* --- alignment sanity check --- */
    size_t avail = xStreamBufferBytesAvailable(s_ctx.spk_stream);
    const int64_t now_us = esp_timer_get_time();
    const bool host_idle = (s_ctx.last_rx_us > 0) && ((now_us - s_ctx.last_rx_us) > SPK_HOST_IDLE_SILENCE_US);
    diag_note_level((uint32_t)avail);
    if ((avail & 0x1U) != 0U) {
        ESP_LOGW(TAG, "unaligned speaker stream level=%u, resetting stream", (unsigned)avail);
        diag_note_reset();
        (void)xStreamBufferReset(s_ctx.spk_stream);
        reset_playback_state();
        xSemaphoreGive(s_bridge_mutex);
        memset(dst, 0, need_bytes);
        s_ctx.stats.playback_bytes += (uint32_t)need_bytes;
        return samples;
    }

    /* Host has stopped sending speaker data and stream is drained:
     * keep output hard-silent to avoid pause/resume crackle caused by
     * underrun conceal tails. */
    if (host_idle && avail == 0U) {
        if (s_ctx.playback_started || s_ctx.asrc_overlap_count != 0U || s_ctx.asrc_phase_q16 != 0U) {
            diag_note_reset();
            reset_playback_state();
        }
        xSemaphoreGive(s_bridge_mutex);
        memset(dst, 0, need_bytes);
        s_ctx.stats.playback_bytes += (uint32_t)need_bytes;
        return samples;
    }

    /* --- prefill gate --- */
    if (!s_ctx.playback_started) {
        if (avail < s_ctx.spk_prefill_bytes) {
            s_ctx.last_playback_sample = 0;
            xSemaphoreGive(s_bridge_mutex);
            memset(dst, 0, need_bytes);
            apply_playback_control(dst, samples);
            s_ctx.stats.playback_bytes += (uint32_t)need_bytes;
            return samples;
        }
        /* Prefill reached — begin playback */
        s_ctx.playback_gain_q15 = 0;
        s_ctx.rate_measure_start_us = esp_timer_get_time();
        s_ctx.rate_measure_start_bytes = s_ctx.stats.usb_rx_bytes;
        s_ctx.measured_drift_q16 = 0;
        s_ctx.asrc_phase_q16 = 0;
        s_ctx.asrc_overlap_count = 0;
        s_ctx.asrc_smooth_step_q16 = STEP_Q16_ONE_TO_ONE;
        s_ctx.level_err_filt_q8 = 0;
        s_ctx.rate_measured = false;
        s_ctx.last_playback_sample = 0;
        s_ctx.playback_started = true;
    }

    /* --- compute ASRC step --- */
    uint32_t step_q16 = STEP_Q16_ONE_TO_ONE;
#if !CONFIG_AUDIO_USB_DISABLE_DRIFT_COMP
    const int64_t elapsed_us = now_us - s_ctx.rate_measure_start_us;
    const uint32_t rate_start_bytes = s_ctx.rate_measure_start_bytes;
    const uint32_t rx_bytes = s_ctx.stats.usb_rx_bytes;
    if (elapsed_us > (int64_t)RATE_MEASURE_MIN_US && rx_bytes > rate_start_bytes) {
        const uint32_t delta_bytes = rx_bytes - rate_start_bytes;
        const int64_t elapsed_ms = elapsed_us / 1000;
        if (elapsed_ms > 0) {
            const int64_t usb_rate_x1000 = ((int64_t)delta_bytes * 1000000LL) / elapsed_ms;
            const int64_t bytes_per_sample =
                (int64_t)sizeof(int16_t) * (int64_t)CONFIG_UAC_SPEAKER_CHANNEL_NUM;
            const int64_t target_bps_x1000 =
                (int64_t)CONFIG_UAC_SAMPLE_RATE * bytes_per_sample * 1000LL;
            const int64_t deficit_bps_x1000 = target_bps_x1000 - usb_rate_x1000;
            const int64_t deficit_sps_x1000 =
                (bytes_per_sample > 0) ? (deficit_bps_x1000 / bytes_per_sample) : 0;
            s_ctx.measured_drift_q16 = (int32_t)((deficit_sps_x1000 * 65536LL) / 100000LL);
            s_ctx.rate_measured = true;
        }
    }
    if (s_ctx.measured_drift_q16 > (int32_t)MAX_DRIFT_Q16) {
        s_ctx.measured_drift_q16 = (int32_t)MAX_DRIFT_Q16;
    }
    if (s_ctx.measured_drift_q16 < -(int32_t)MAX_DRIFT_Q16) {
        s_ctx.measured_drift_q16 = -(int32_t)MAX_DRIFT_Q16;
    }
    avail = xStreamBufferBytesAvailable(s_ctx.spk_stream);
    diag_note_level((uint32_t)avail);
    const int32_t level_err_bytes = (int32_t)avail - (int32_t)s_ctx.spk_target_bytes;
    const int32_t bytes_per_sample =
        (int32_t)(sizeof(int16_t) * (size_t)CONFIG_UAC_SPEAKER_CHANNEL_NUM);
    const int32_t level_err_samples = (bytes_per_sample > 0) ? (level_err_bytes / bytes_per_sample) : 0;
    const int32_t level_err_q8 = level_err_samples << 8;
    s_ctx.level_err_filt_q8 += (level_err_q8 - s_ctx.level_err_filt_q8) >> LEVEL_LPF_SHIFT;
    const int32_t level_err_samples_filt = s_ctx.level_err_filt_q8 >> 8;
    int32_t level_ctrl_samples = 0;
    if (level_err_samples_filt > LEVEL_DEADBAND_SAMPLES) {
        level_ctrl_samples = level_err_samples_filt - LEVEL_DEADBAND_SAMPLES;
    } else if (level_err_samples_filt < -LEVEL_DEADBAND_SAMPLES) {
        level_ctrl_samples = level_err_samples_filt + LEVEL_DEADBAND_SAMPLES;
    }
    const int32_t level_nudge_q16 =
        -(int32_t)(((int64_t)level_ctrl_samples << 16) / (int32_t)LEVEL_CORR_WINDOW_FRAMES);
    int32_t total_comp_q16 = s_ctx.measured_drift_q16 + level_nudge_q16;
    if (total_comp_q16 > (3 << 16)) {
        total_comp_q16 = 3 << 16;
    }
    if (total_comp_q16 < -(2 << 16)) {
        total_comp_q16 = -(2 << 16);
    }
    step_q16 = (uint32_t)((int32_t)STEP_Q16_ONE_TO_ONE - total_comp_q16 / (int32_t)samples);
    if (step_q16 < STEP_Q16_MIN) {
        step_q16 = STEP_Q16_MIN;
    }
    if (step_q16 > STEP_Q16_MAX) {
        step_q16 = STEP_Q16_MAX;
    }
    const uint32_t prev_smooth = s_ctx.asrc_smooth_step_q16;
    s_ctx.asrc_smooth_step_q16 = prev_smooth - (prev_smooth >> STEP_IIR_SHIFT) + (step_q16 >> STEP_IIR_SHIFT);
    step_q16 = s_ctx.asrc_smooth_step_q16;
    diag_note_step(prev_smooth, step_q16);
    s_ctx.asrc_last_step_q16 = step_q16;
#else
    diag_note_step(s_ctx.asrc_last_step_q16, step_q16);
    s_ctx.asrc_last_step_q16 = step_q16;
#endif

    /* --- copy ASRC state out for resampling --- */
    asrc_state_t asrc_state;
    asrc_state.phase_q16 = s_ctx.asrc_phase_q16;
    asrc_state.overlap_count = s_ctx.asrc_overlap_count;
    if (asrc_state.overlap_count > 0 && asrc_state.overlap_count <= (size_t)ASRC_OVERLAP_SIZE) {
        memcpy(asrc_state.overlap, s_ctx.asrc_overlap, asrc_state.overlap_count * sizeof(int16_t));
    }
    const int16_t prev_last_sample = s_ctx.last_playback_sample;

    /* --- resample (reads from spk_stream while mutex is held) --- */
    size_t produced = asrc_resample(dst, samples, step_q16, s_ctx.spk_stream, &asrc_state);

    if (produced == ASRC_NEED_RESET) {
        diag_note_reset();
        (void)xStreamBufferReset(s_ctx.spk_stream);
        reset_playback_state();
        xSemaphoreGive(s_bridge_mutex);
        memset(dst, 0, need_bytes);
        s_ctx.stats.playback_bytes += (uint32_t)need_bytes;
        return samples;
    }

    /* --- copy ASRC state back --- */
    s_ctx.asrc_phase_q16 = asrc_state.phase_q16;
    s_ctx.asrc_overlap_count = asrc_state.overlap_count;
    if (asrc_state.overlap_count > 0 && asrc_state.overlap_count <= (size_t)ASRC_OVERLAP_SIZE) {
        memcpy(s_ctx.asrc_overlap, asrc_state.overlap, asrc_state.overlap_count * sizeof(int16_t));
    }
    if (produced < samples && !host_idle) {
        s_ctx.stats.playback_underrun_frames++;
        diag_note_conceal(samples - produced);
    }
    if (produced == 0) {
        const size_t avail_now = xStreamBufferBytesAvailable(s_ctx.spk_stream);
        if (avail_now < (s_ctx.spk_prefill_bytes / 2U)) {
            diag_note_reset();
            reset_playback_state();
        }
    }

    /* --- conceal underrun tail --- */
    if (produced < samples) {
        if (host_idle) {
            memset(&dst[produced], 0, (samples - produced) * sizeof(int16_t));
        } else {
            int32_t hold = (produced > 0U) ? (int32_t)dst[produced - 1U] : (int32_t)prev_last_sample;
            for (size_t i = produced; i < samples; ++i) {
                hold = (hold * 31) / 32;
                dst[i] = (int16_t)hold;
            }
        }
    }
    s_ctx.last_playback_sample = dst[samples - 1];

    /* --- release mutex: all shared-state access is done --- */
    xSemaphoreGive(s_bridge_mutex);

    /* --- post-processing (no shared state needed) --- */
    apply_playback_control(dst, samples);
    update_playback_frame_stats(dst, samples);
    s_ctx.stats.playback_bytes += (uint32_t)need_bytes;
    return samples;
}

static void enqueue_mic_uplink_pcm(const int16_t *src, size_t samples) {
    if (!s_ctx.input_enabled || src == NULL || samples == 0) {
        return;
    }

    if (!s_mic_host_active) {
        return;
    }

    /* Apply DC-Block → Voice EQ → digital gain → write to ring buffer.
     * No mutex needed: ring buffer uses portMUX spinlock internally.
     * IMPORTANT: process all input samples; dropping tail causes stable tx underrun. */
    int16_t gained[480];
    const int32_t gain_q15 = s_mic_gain_q15;
    size_t offset = 0;

    while (offset < samples) {
        const size_t chunk_samples = min_size(samples - offset, (size_t)480U);
        for (size_t i = 0; i < chunk_samples; ++i) {
            int32_t sample = (int32_t)src[offset + i];

            /* DC-Block high-pass: remove INMP441 DC offset */
            const int32_t dc_out =
                sample - (int32_t)s_mic_dc_prev_in +
                (int32_t)(((int64_t)s_mic_dc_prev_out * MIC_DC_BLOCK_A_Q15) >> 15);
            s_mic_dc_prev_in = (int16_t)sample;
            s_mic_dc_prev_out = dc_out;
            sample = dc_out;

            /* Voice EQ: attenuate low-frequency rumble, boost mid presence */
            s_mic_eq_low_state += ((sample - s_mic_eq_low_state) * 1024) >> 15;
            s_mic_eq_mid_state += ((sample - s_mic_eq_mid_state) * 7168) >> 15;
            const int32_t low_band = s_mic_eq_low_state;
            const int32_t presence_band = s_mic_eq_mid_state - s_mic_eq_low_state;
            sample -= (low_band * 4096) >> 15;
            sample += (presence_band * 4915) >> 15;

            /* Digital gain */
            if (gain_q15 != 32768) {
                sample = (int32_t)(((int64_t)sample * gain_q15 + (1 << 14)) >> 15);
            }
            gained[i] = clamp_i16(sample);
        }

        diag_note_tx_pcm(gained, chunk_samples);
        s_ctx.stats.afe_out_bytes += (uint32_t)(chunk_samples * sizeof(int16_t));

        /* Write to ring buffer — overflow auto-discards oldest data. */
        audio_ringbuf_stats_t before = { 0 };
        audio_ringbuf_stats_t after = { 0 };
        audio_ringbuf_get_stats(&s_mic_ringbuf, &before);
        audio_ringbuf_write(&s_mic_ringbuf, (const uint8_t *)gained, chunk_samples * sizeof(int16_t));
        audio_ringbuf_get_stats(&s_mic_ringbuf, &after);
        s_ctx.stats.usb_tx_overrun_events += after.overflow_count - before.overflow_count;
        s_ctx.stats.afe_out_drop_bytes += after.dropped_bytes - before.dropped_bytes;
        offset += chunk_samples;
    }
}

static void uac_stream_state_cb(uac_stream_t stream, bool active, void *cb_ctx) {
    (void)cb_ctx;
    if (stream != UAC_STREAM_MICROPHONE || !s_ctx.input_enabled) {
        return;
    }

    s_mic_host_active = active;
    s_mic_prefill_ready = false;
    s_last_mic_read_us = active ? esp_timer_get_time() : 0;
    audio_ringbuf_reset(&s_mic_ringbuf);
    if (active) {
        s_ctx.stats.mic_host_open_events++;
    }
}

static void afe_output_tap(const int16_t *src, size_t samples, uint32_t frame_seq, void *ctx) {
    (void)ctx;
    diag_note_mic_frame_seq(frame_seq);
    enqueue_mic_uplink_pcm(src, samples);
}

static void raw_mic_input_tap(const int16_t *src, size_t samples, uint32_t frame_seq, void *ctx) {
    (void)ctx;
    diag_note_mic_frame_seq(frame_seq);
    enqueue_mic_uplink_pcm(src, samples);
}

static esp_err_t uac_input_cb(uint8_t *buf, size_t len, size_t *bytes_read, void *cb_ctx) {
    (void)cb_ctx;
    if (buf == NULL || bytes_read == NULL) {
        return ESP_ERR_INVALID_ARG;
    }

    const int64_t now_us = esp_timer_get_time();

    /* Idle detection + reset (reader side) */
    if (s_last_mic_read_us > 0) {
        const int64_t idle_us = now_us - s_last_mic_read_us;
        if (idle_us > MIC_HOST_IDLE_RESET_US) {
            s_mic_host_active = false;
            s_mic_prefill_ready = false;
            s_last_mic_read_us = 0;
            audio_ringbuf_reset(&s_mic_ringbuf);
            reset_mic_dsp_state();
            s_ctx.stats.mic_host_idle_resets++;
        }
    }

    if (!s_mic_host_active) {
        s_mic_host_active = true;
        /* Fallback for UAC implementations without an interface-state event. */
        s_ctx.stats.mic_host_open_events++;
        s_mic_prefill_ready = false;
        s_last_mic_read_us = now_us;
        audio_ringbuf_reset(&s_mic_ringbuf);
        reset_mic_dsp_state();
    } else {
        s_last_mic_read_us = now_us;
    }

    len &= ~(size_t)0x1U;
    if (len == 0) {
        *bytes_read = 0;
        return ESP_OK;
    }

    if (!s_mic_prefill_ready) {
        const size_t avail = audio_ringbuf_level_bytes(&s_mic_ringbuf);
        if (avail < s_mic_prefill_bytes) {
            memset(buf, 0, len);
            *bytes_read = len;
            s_ctx.stats.usb_tx_bytes += (uint32_t)len;
            return ESP_OK;
        }
        s_mic_prefill_ready = true;
    }

    const size_t got = audio_ringbuf_read(&s_mic_ringbuf, buf, len);

    if (got < len) {
        memset(buf + got, 0, len - got);
        s_ctx.stats.usb_tx_underrun_calls++;
        if (audio_ringbuf_level_bytes(&s_mic_ringbuf) < (s_mic_prefill_bytes / 4U)) {
            s_mic_prefill_ready = false;
        }
    }

    *bytes_read = len;
    s_ctx.stats.usb_tx_bytes += (uint32_t)len;
    return ESP_OK;
}

#endif

esp_err_t usb_audio_bridge_init(void) {
#if !CONFIG_AUDIO_USB_UAC_ENABLE
    ESP_LOGI(TAG, "disabled by Kconfig");
    return ESP_OK;
#else
    if (s_ctx.inited) {
        return ESP_OK;
    }
    const bool afe_processing_enabled = (CONFIG_AUDIO_AEC_ENABLE != 0);
    const bool afe_uplink_rate_match = (CONFIG_UAC_SAMPLE_RATE == 16000);
    const bool use_raw_input_tap =
        !CONFIG_AUDIO_ENABLE_AFE || !afe_processing_enabled || !afe_uplink_rate_match;
    const bool input_enabled = (CONFIG_AUDIO_USB_UAC_INPUT_ENABLE != 0);
    const bool spk_enabled = (CONFIG_UAC_SPEAKER_CHANNEL_NUM > 0);

    if (CONFIG_UAC_SAMPLE_RATE != 16000 && CONFIG_UAC_SAMPLE_RATE != 48000) {
        ESP_LOGE(TAG, "UAC sample rate must be 16000 or 48000, got %d",
                 CONFIG_UAC_SAMPLE_RATE);
        return ESP_ERR_NOT_SUPPORTED;
    }
    if (CONFIG_UAC_SPEAKER_CHANNEL_NUM > 1) {
        ESP_LOGE(TAG, "UAC speaker channels must be 0 or 1, got %d",
                 CONFIG_UAC_SPEAKER_CHANNEL_NUM);
        return ESP_ERR_NOT_SUPPORTED;
    }
    if (input_enabled) {
        if (!CONFIG_AUDIO_ENABLE_AFE) {
            ESP_LOGE(TAG, "UAC mic uplink requires AUDIO_ENABLE_AFE=y");
            return ESP_ERR_NOT_SUPPORTED;
        }
        if (CONFIG_UAC_MIC_CHANNEL_NUM != 1) {
            ESP_LOGE(TAG, "UAC mic channels must be 1 for current pipeline, got %d",
                     CONFIG_UAC_MIC_CHANNEL_NUM);
            return ESP_ERR_NOT_SUPPORTED;
        }
    }
    if (!spk_enabled && !input_enabled) {
        ESP_LOGE(TAG, "both UAC speaker and microphone are disabled");
        return ESP_ERR_INVALID_STATE;
    }
    if (input_enabled && afe_processing_enabled && !afe_uplink_rate_match) {
        ESP_LOGW(TAG,
                 "UAC mic rate=%d, AFE processed tap is 16kHz; auto-fallback to raw mic tap. "
                 "Set UAC sample rate to 16000 to upload processed(AEC) audio.",
                 CONFIG_UAC_SAMPLE_RATE);
    }

    const size_t spk_bytes_per_ms = spk_enabled
                                        ? (((size_t)CONFIG_UAC_SAMPLE_RATE *
                                            (size_t)CONFIG_UAC_SPEAKER_CHANNEL_NUM *
                                            sizeof(int16_t)) /
                                           1000U)
                                        : 0U;
    const size_t spk_stream_capacity = spk_bytes_per_ms * (size_t)CONFIG_AUDIO_USB_SPK_BUFFER_MS;
    const size_t mic_bytes_per_ms =
        ((size_t)CONFIG_UAC_SAMPLE_RATE * (size_t)CONFIG_UAC_MIC_CHANNEL_NUM * sizeof(int16_t)) /
        1000U;
    if (spk_enabled && (spk_bytes_per_ms == 0 || spk_stream_capacity < 256)) {
        return ESP_ERR_INVALID_SIZE;
    }
    if (input_enabled && mic_bytes_per_ms == 0) {
        return ESP_ERR_INVALID_SIZE;
    }

    memset(&s_ctx, 0, sizeof(s_ctx));
    s_ctx.stats.host_volume_percent = 100;
    s_ctx.host_volume_runtime = 100;
    s_ctx.host_mute_runtime = false;
    s_ctx.playback_gain_q15 = 32767;
    s_ctx.last_playback_sample = 0;
    s_ctx.playback_started = false;
    s_ctx.rate_measured = false;
    s_ctx.measured_drift_q16 = 0;
    s_ctx.asrc_phase_q16 = 0;
    s_ctx.asrc_overlap_count = 0;
    s_ctx.asrc_last_step_q16 = 65536;
    s_ctx.asrc_smooth_step_q16 = 65536;
    s_ctx.level_err_filt_q8 = 0;
    s_mic_gain_q15 = mic_gain_q15_from_db(CONFIG_AUDIO_USB_MIC_GAIN_DB);
    /* Init module-level mic state */
    s_mic_host_active = false;
    s_mic_prefill_ready = false;
    s_last_mic_read_us = 0;
    reset_mic_dsp_state();
    reset_diag_window();
    s_ctx.spk_bytes_per_ms = spk_bytes_per_ms;
    s_ctx.mic_bytes_per_ms = mic_bytes_per_ms;
    s_ctx.spk_stream_capacity = spk_stream_capacity;
    s_ctx.spk_enabled = spk_enabled;
    s_ctx.input_enabled = input_enabled;
    // Prefill to half capacity so we start at target level, not below low watermark.
    size_t spk_prefill_bytes = spk_enabled ? (spk_stream_capacity / 2U) : 0U;
    const size_t max_prefill_bytes =
        (spk_stream_capacity > (spk_bytes_per_ms * 5U)) ? (spk_stream_capacity - (spk_bytes_per_ms * 5U)) : 0U;
    if (max_prefill_bytes > 0 && spk_prefill_bytes > max_prefill_bytes) {
        spk_prefill_bytes = max_prefill_bytes;
    }
    spk_prefill_bytes &= ~(size_t)0x1U;
    if (spk_enabled && spk_prefill_bytes == 0) {
        spk_prefill_bytes = spk_bytes_per_ms * 10U;
    }
    s_ctx.spk_prefill_bytes = spk_prefill_bytes;
    /* Mic prefill: 30ms @ current sample rate */
    s_mic_prefill_bytes = mic_bytes_per_ms * MIC_PREFILL_MS;
    if (s_mic_prefill_bytes > (MIC_RINGBUF_CAPACITY_BYTES / 2U)) {
        s_mic_prefill_bytes = MIC_RINGBUF_CAPACITY_BYTES / 2U;
    }
    s_mic_prefill_bytes = align_even_up(s_mic_prefill_bytes);
    if (s_mic_prefill_bytes == 0 && input_enabled) {
        s_mic_prefill_bytes = align_even_up(mic_bytes_per_ms * 10U);
    }
    size_t spk_target_bytes = spk_enabled ? (spk_stream_capacity / 2U) : 0U;
    if (spk_enabled && spk_target_bytes < s_ctx.spk_prefill_bytes) {
        spk_target_bytes = s_ctx.spk_prefill_bytes;
    }
    // Wide hysteresis: 25% of capacity on each side. Watermarks are now used
    // only for prefill/restart safety (drift is handled by continuous ASRC).
    size_t wm_hysteresis_bytes = spk_enabled ? (spk_stream_capacity / 4U) : 0U;
    if (spk_enabled && wm_hysteresis_bytes < (spk_bytes_per_ms * 20U)) {
        wm_hysteresis_bytes = spk_bytes_per_ms * 20U;
    }
    size_t spk_low_wm_bytes =
        (spk_target_bytes > wm_hysteresis_bytes) ? (spk_target_bytes - wm_hysteresis_bytes) : 0U;
    size_t spk_high_wm_bytes = spk_target_bytes + wm_hysteresis_bytes;
    if (spk_high_wm_bytes > spk_stream_capacity) {
        spk_high_wm_bytes = spk_stream_capacity;
    }
    s_ctx.spk_target_bytes = spk_target_bytes & ~(size_t)0x1U;
    s_ctx.spk_low_wm_bytes = spk_low_wm_bytes & ~(size_t)0x1U;
    s_ctx.spk_high_wm_bytes = spk_high_wm_bytes & ~(size_t)0x1U;
    /* Mutex only needed when speaker is enabled */
    if (spk_enabled) {
        s_bridge_mutex = xSemaphoreCreateMutex();
        ESP_RETURN_ON_FALSE(s_bridge_mutex != NULL, ESP_ERR_NO_MEM, TAG, "failed to create bridge mutex");
    } else {
        s_bridge_mutex = NULL;
    }
    if (spk_enabled) {
        s_ctx.spk_stream = xStreamBufferCreate(spk_stream_capacity, 1);
        if (s_ctx.spk_stream == NULL) {
            if (s_bridge_mutex != NULL) {
                vSemaphoreDelete(s_bridge_mutex);
                s_bridge_mutex = NULL;
            }
            ESP_LOGE(TAG, "failed to allocate speaker stream buffer");
            return ESP_ERR_NO_MEM;
        }
    }
    if (input_enabled) {
        esp_err_t rb_ret = audio_ringbuf_init(&s_mic_ringbuf, s_mic_ringbuf_storage, MIC_RINGBUF_CAPACITY_BYTES);
        if (rb_ret != ESP_OK) {
            if (s_ctx.spk_stream != NULL) {
                vStreamBufferDelete(s_ctx.spk_stream);
                s_ctx.spk_stream = NULL;
            }
            if (s_bridge_mutex != NULL) {
                vSemaphoreDelete(s_bridge_mutex);
                s_bridge_mutex = NULL;
            }
            return rb_ret;
        }
    }

    bool pb_registered = false;
    bool tap_registered = false;
    esp_err_t ret = ESP_OK;
    if (spk_enabled) {
        ret = audio_pipeline_set_playback_provider(usb_playback_provider, NULL);
        if (ret != ESP_OK) {
            goto fail;
        }
        pb_registered = true;
    }
    if (input_enabled) {
        ret = audio_pipeline_set_output_tap(use_raw_input_tap ? NULL : afe_output_tap, NULL);
        if (ret != ESP_OK) {
            goto fail;
        }
        ret = audio_pipeline_set_input_tap(use_raw_input_tap ? raw_mic_input_tap : NULL, NULL);
        if (ret != ESP_OK) {
            (void)audio_pipeline_set_output_tap(NULL, NULL);
            goto fail;
        }
        tap_registered = true;
    }

    uac_device_config_t cfg = {
        .skip_tinyusb_init = false,
        .output_cb = spk_enabled ? uac_output_cb : NULL,
        .input_cb = input_enabled ? uac_input_cb : NULL,
        .set_mute_cb = uac_set_mute_cb,
        .set_volume_cb = uac_set_volume_cb,
        .stream_state_cb = uac_stream_state_cb,
        .cb_ctx = NULL,
    };
    ret = uac_device_init(&cfg);
    if (ret != ESP_OK) {
        goto fail;
    }

    // Log stats from a timer callback (runs in esp_timer task), NOT from the
    // playback provider.  ESP_LOGI can block on UART for tens of ms — doing
    // that inside the playback callback starves the I2S DMA and causes audible
    // glitches whenever a log line coincides with another UART write.
    const esp_timer_create_args_t timer_args = {
        .callback = log_stats_timer_cb,
        .name = "usb_log",
    };
    ret = esp_timer_create(&timer_args, &s_ctx.log_timer);
    if (ret != ESP_OK) {
        goto fail;
    }
    const int64_t log_period_us = (int64_t)CONFIG_AUDIO_USB_LOG_INTERVAL_SEC * 1000000LL;
    ret = esp_timer_start_periodic(s_ctx.log_timer, log_period_us);
    if (ret != ESP_OK) {
        esp_timer_delete(s_ctx.log_timer);
        s_ctx.log_timer = NULL;
        goto fail;
    }

    s_ctx.inited = true;
    ESP_LOGI(TAG,
             "UAC bridge enabled: spk(enabled=%u rate=%d ch=%d buf=%dms prefill=%uB target=%uB wm=[%u,%u]B)"
             " mic(enabled=%u ch=%d ringbuf=%uB prefill=%uB raw_auto=%u proc16k=%u)",
             spk_enabled ? 1U : 0U, CONFIG_UAC_SAMPLE_RATE, CONFIG_UAC_SPEAKER_CHANNEL_NUM,
             CONFIG_AUDIO_USB_SPK_BUFFER_MS, (unsigned)s_ctx.spk_prefill_bytes,
             (unsigned)s_ctx.spk_target_bytes, (unsigned)s_ctx.spk_low_wm_bytes,
             (unsigned)s_ctx.spk_high_wm_bytes, input_enabled ? 1U : 0U,
             CONFIG_UAC_MIC_CHANNEL_NUM, (unsigned)MIC_RINGBUF_CAPACITY_BYTES,
             (unsigned)s_mic_prefill_bytes,
             use_raw_input_tap ? 1U : 0U,
             (CONFIG_AUDIO_ENABLE_AFE && afe_processing_enabled) ? 1U : 0U);
    return ESP_OK;

fail:
    if (s_ctx.log_timer != NULL) {
        esp_timer_stop(s_ctx.log_timer);
        esp_timer_delete(s_ctx.log_timer);
        s_ctx.log_timer = NULL;
    }
    if (tap_registered) {
        (void)audio_pipeline_set_output_tap(NULL, NULL);
        (void)audio_pipeline_set_input_tap(NULL, NULL);
    }
    if (pb_registered) {
        (void)audio_pipeline_set_playback_provider(NULL, NULL);
    }
    /* Ring buffer is statically allocated — no cleanup needed */
    if (s_ctx.spk_stream != NULL) {
        vStreamBufferDelete(s_ctx.spk_stream);
        s_ctx.spk_stream = NULL;
    }
    if (s_bridge_mutex != NULL) {
        vSemaphoreDelete(s_bridge_mutex);
        s_bridge_mutex = NULL;
    }
    return ret;
#endif
}

usb_audio_bridge_stats_t usb_audio_bridge_get_stats(void) {
    usb_audio_bridge_stats_t copy = { 0 };
    if (s_bridge_mutex != NULL) {
        if (xSemaphoreTake(s_bridge_mutex, pdMS_TO_TICKS(10)) == pdTRUE) {
            copy = s_ctx.stats;
            xSemaphoreGive(s_bridge_mutex);
        }
    } else {
        /* No mutex (speaker disabled) — read directly (small race OK) */
        copy = s_ctx.stats;
    }
    return copy;
}
