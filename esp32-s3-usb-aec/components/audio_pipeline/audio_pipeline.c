#include "audio_pipeline.h"

#include <stdbool.h>
#include <stdio.h>
#include <stdint.h>
#include <string.h>

#include "afe_engine.h"
#include "audio_buffers.h"
#include "audio_i2s_in.h"
#include "audio_i2s_out.h"
#include "esp_check.h"
#include "esp_err.h"
#include "esp_log.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "sdkconfig.h"

static const char *TAG = "audio_pipeline";
#ifndef CONFIG_UAC_SAMPLE_RATE
#define CONFIG_UAC_SAMPLE_RATE 48000
#endif

#if (CONFIG_UAC_SAMPLE_RATE == 48000)
static const size_t FRAME_SAMPLES = 480;  // 10 ms @ 48 kHz
#elif (CONFIG_UAC_SAMPLE_RATE == 16000)
static const size_t FRAME_SAMPLES = 160;  // 10 ms @ 16 kHz
#else
#error "CONFIG_UAC_SAMPLE_RATE must be 16000 or 48000"
#endif

static const size_t REF_CHANNELS = 1;
static const size_t AFE_FETCH_SAMPLES = 512;
static const uint32_t PIPELINE_SAMPLE_RATE_HZ = CONFIG_UAC_SAMPLE_RATE;
static const uint32_t AFE_SAMPLE_RATE_HZ = 16000;
#if (CONFIG_UAC_SAMPLE_RATE == 48000)
#define AFE_DECIM_FACTOR 3
#else
#endif
static const uint32_t CAPTURE_STACK_BYTES = 10240;
#if CONFIG_UAC_SPEAKER_CHANNEL_NUM > 0
static const uint32_t PLAYBACK_STACK_BYTES = 10240;
#endif
static const uint32_t AFE_FEED_STACK_BYTES = 12288;
static const uint32_t AFE_FETCH_STACK_BYTES = 14336;  /* 14KB for safety margin */
static const uint32_t MONITOR_STACK_BYTES = 5120;
static const UBaseType_t AFE_FEED_TASK_PRIO = 17;
static const UBaseType_t AFE_FETCH_TASK_PRIO = 18;

#ifndef CONFIG_AUDIO_MIC_CHANNELS
#define CONFIG_AUDIO_MIC_CHANNELS 1
#endif

#ifndef CONFIG_AUDIO_ENABLE_AFE
#define CONFIG_AUDIO_ENABLE_AFE 0
#endif

#ifndef CONFIG_AUDIO_AFE_HIGH_PERF_MODE
#define CONFIG_AUDIO_AFE_HIGH_PERF_MODE 0
#endif

#ifndef CONFIG_AUDIO_AEC_ENABLE
#define CONFIG_AUDIO_AEC_ENABLE 0
#endif

#ifndef CONFIG_AUDIO_REF_DELAY_FRAMES
#define CONFIG_AUDIO_REF_DELAY_FRAMES 0
#endif

#if (CONFIG_AUDIO_MIC_CHANNELS < 1) || (CONFIG_AUDIO_MIC_CHANNELS > 2)
#error "CONFIG_AUDIO_MIC_CHANNELS must be 1 or 2"
#endif

static inline size_t active_mic_channels(void) { return (size_t)CONFIG_AUDIO_MIC_CHANNELS; }
static inline size_t mic_samples_per_frame(void) { return FRAME_SAMPLES * active_mic_channels(); }
static inline size_t ref_samples_per_frame(void) { return FRAME_SAMPLES * REF_CHANNELS; }

typedef struct {
    TaskHandle_t capture_task;
    TaskHandle_t playback_task;
    TaskHandle_t afe_feed_task;
    TaskHandle_t afe_fetch_task;
    TaskHandle_t monitor_task;
    bool inited;
    bool running;
} audio_pipeline_ctx_t;

static audio_pipeline_ctx_t s_ctx;
static audio_pipeline_playback_provider_t s_playback_provider;
static void *s_playback_provider_ctx;
static audio_pipeline_output_tap_t s_output_tap;
static void *s_output_tap_ctx;
static audio_pipeline_input_tap_t s_input_tap;
static void *s_input_tap_ctx;
static uint32_t s_capture_seq;
static uint32_t s_playback_seq;
static volatile uint32_t s_mic_avg_abs[2];
static volatile int32_t s_mic_peak[2];
static volatile int32_t s_mic_mean[2];
static volatile uint32_t s_mic_rms[2];
static volatile uint16_t s_mic_sat_permille[2];
static volatile uint16_t s_mic_zero_permille[2];
static volatile uint32_t s_out_avg_abs;
static volatile uint32_t s_out_rms;
static volatile int32_t s_out_mean;
static volatile int32_t s_out_peak;

// Keep AFE feed working buffers off task stack to avoid overflow under esp-sr high-perf mode.
static int16_t s_afe_feed_mic_frame[480 * 2];
static int16_t s_afe_feed_ref_frame[480];
static int16_t s_afe_feed_ref_delayed[480];
static int16_t s_capture_tap_frame[480];
static int16_t s_afe_feed_mic_16k[160 * 2];
static int16_t s_afe_feed_ref_16k[160];
#if (CONFIG_AUDIO_REF_DELAY_FRAMES > 0)
static int16_t s_afe_feed_ref_hist[CONFIG_AUDIO_REF_DELAY_FRAMES + 1][480];
#endif

static uint32_t isqrt_u64(uint64_t x) {
    uint64_t op = x;
    uint64_t res = 0;
    uint64_t one = (uint64_t)1 << 62;
    while (one > op) {
        one >>= 2;
    }
    while (one != 0) {
        if (op >= res + one) {
            op -= res + one;
            res = (res >> 1) + one;
        } else {
            res >>= 1;
        }
        one >>= 2;
    }
    return (uint32_t)res;
}

static void update_frame_stats(const int16_t *buf, size_t samples, volatile uint32_t *avg_abs_out,
                               volatile uint32_t *rms_out, volatile int32_t *mean_out,
                               volatile int32_t *peak_out) {
    if (buf == NULL || samples == 0 || avg_abs_out == NULL || rms_out == NULL || mean_out == NULL ||
        peak_out == NULL) {
        return;
    }

    uint64_t sum_abs = 0;
    uint64_t sum_sq = 0;
    int64_t sum = 0;
    int32_t peak = 0;
    for (size_t i = 0; i < samples; ++i) {
        const int32_t s = (int32_t)buf[i];
        const int32_t a = (s >= 0) ? s : -s;
        if (a > peak) {
            peak = a;
        }
        sum_abs += (uint32_t)a;
        sum_sq += (uint64_t)((int64_t)s * (int64_t)s);
        sum += s;
    }

    *avg_abs_out = (uint32_t)(sum_abs / samples);
    *rms_out = isqrt_u64(sum_sq / samples);
    *mean_out = (int32_t)(sum / (int64_t)samples);
    *peak_out = peak;
}

static size_t downsample_48k_to_16k_interleaved(const int16_t *in, size_t in_frames,
                                                size_t channels, int16_t *out,
                                                size_t out_capacity_frames) {
    if (in == NULL || out == NULL || channels == 0) {
        return 0;
    }
#if (CONFIG_UAC_SAMPLE_RATE == 16000)
    const size_t frames = (in_frames < out_capacity_frames) ? in_frames : out_capacity_frames;
    memcpy(out, in, frames * channels * sizeof(int16_t));
    return frames;
#else
    const size_t out_frames = in_frames / AFE_DECIM_FACTOR;
    const size_t frames = (out_frames < out_capacity_frames) ? out_frames : out_capacity_frames;
    for (size_t i = 0; i < frames; ++i) {
        const size_t base = i * AFE_DECIM_FACTOR;
        for (size_t c = 0; c < channels; ++c) {
            const int32_t s0 = in[(base + 0U) * channels + c];
            const int32_t s1 = in[(base + 1U) * channels + c];
            const int32_t s2 = in[(base + 2U) * channels + c];
            out[i * channels + c] = (int16_t)((s0 + s1 + s2) / 3);
        }
    }
    return frames;
#endif
}

static void capture_task_fn(void *arg) {
    (void)arg;
    int16_t mic_frame[480 * 2];
    while (true) {
        const size_t expected = mic_samples_per_frame();
        size_t read_samples = 0;
        esp_err_t ret = audio_i2s_in_read(mic_frame, expected, 30, &read_samples);
        if (ret == ESP_OK && read_samples == expected) {
            const size_t ch = active_mic_channels();
            const size_t frames = read_samples / ch;
            for (size_t c = 0; c < ch; ++c) {
                uint64_t sum_abs = 0;
                uint64_t sum_sq = 0;
                int64_t sum = 0;
                int32_t peak = 0;
                uint32_t sat_count = 0;
                uint32_t zero_count = 0;
                for (size_t i = c; i < read_samples; i += ch) {
                    int32_t s = (int32_t)mic_frame[i];
                    int32_t a = (s >= 0) ? s : -s;
                    if (a > peak) {
                        peak = a;
                    }
                    sum_abs += (uint32_t)a;
                    sum_sq += (uint64_t)((int64_t)s * (int64_t)s);
                    sum += s;
                    if (a >= 32760) {
                        sat_count++;
                    }
                    if (a <= 8) {
                        zero_count++;
                    }
                }
                s_mic_peak[c] = peak;
                s_mic_avg_abs[c] = (uint32_t)(sum_abs / frames);
                s_mic_mean[c] = (int32_t)(sum / (int64_t)frames);
                s_mic_rms[c] = isqrt_u64(sum_sq / frames);
                s_mic_sat_permille[c] = (uint16_t)((sat_count * 1000U) / frames);
                s_mic_zero_permille[c] = (uint16_t)((zero_count * 1000U) / frames);
            }
            s_capture_seq++;
            if (s_input_tap != NULL) {
                if (ch == 1) {
                    s_input_tap(mic_frame, frames, s_capture_seq, s_input_tap_ctx);
                } else {
                    for (size_t i = 0; i < frames; ++i) {
                        s_capture_tap_frame[i] = mic_frame[i * ch];
                    }
                    s_input_tap(s_capture_tap_frame, frames, s_capture_seq, s_input_tap_ctx);
                }
            }
            audio_buffers_push_mic(mic_frame, read_samples, s_capture_seq);
        } else if (ret != ESP_ERR_TIMEOUT) {
            ESP_LOGW(TAG, "capture read err=%s", esp_err_to_name(ret));
            vTaskDelay(pdMS_TO_TICKS(10));
        }
    }
}

#if CONFIG_UAC_SPEAKER_CHANNEL_NUM > 0
static void playback_task_fn(void *arg) {
    (void)arg;
    int16_t tone[FRAME_SAMPLES];
    while (true) {
        size_t provided = 0;
        if (s_playback_provider != NULL) {
            provided = s_playback_provider(tone, FRAME_SAMPLES, s_playback_provider_ctx);
            if (provided > FRAME_SAMPLES) {
                provided = FRAME_SAMPLES;
            }
        }

        if (provided == 0) {
            memset(tone, 0, sizeof(tone));
        } else if (provided < FRAME_SAMPLES) {
            memset(&tone[provided], 0, (FRAME_SAMPLES - provided) * sizeof(int16_t));
        }

        const uint32_t next_seq = s_playback_seq + 1U;
        size_t written_samples = 0;
        esp_err_t ret = audio_i2s_out_write(tone, FRAME_SAMPLES, next_seq, &written_samples);
        if (written_samples < FRAME_SAMPLES) {
            memset(&tone[written_samples], 0, (FRAME_SAMPLES - written_samples) * sizeof(int16_t));
        }

        esp_err_t ref_ret = audio_buffers_push_ref(tone, FRAME_SAMPLES, next_seq);
        if (ref_ret != ESP_OK) {
            ESP_LOGW(TAG, "push_ref err=%s seq=%u", esp_err_to_name(ref_ret), next_seq);
        }
        s_playback_seq = next_seq;

        if (ret != ESP_OK && ret != ESP_ERR_TIMEOUT) {
            ESP_LOGW(TAG, "playback write err=%s", esp_err_to_name(ret));
            vTaskDelay(pdMS_TO_TICKS(10));
        }
        // No extra vTaskDelay here: i2s_write is already blocking on DMA.
        // The previous vTaskDelay(1) added ~10ms per frame, causing ref to
        // lag behind mic by ~7 frames (70ms), which degrades AEC.
    }
}
#endif

static void afe_feed_task_fn(void *arg) {
    (void)arg;
#if (CONFIG_AUDIO_REF_DELAY_FRAMES > 0)
    uint32_t ref_hist_wr = 0;
    uint32_t ref_hist_count = 0;
#endif
    uint32_t frame_seq = 0;
    while (true) {
        const size_t mic_samples = mic_samples_per_frame();
        const size_t ref_samples = ref_samples_per_frame();
        esp_err_t ret =
            audio_buffers_pop_aligned(s_afe_feed_mic_frame, mic_samples, s_afe_feed_ref_frame, ref_samples, &frame_seq);
        if (ret == ESP_OK) {
#if (CONFIG_AUDIO_REF_DELAY_FRAMES > 0)
            memcpy(s_afe_feed_ref_hist[ref_hist_wr], s_afe_feed_ref_frame, ref_samples * sizeof(int16_t));
            ref_hist_wr = (ref_hist_wr + 1U) % (uint32_t)(CONFIG_AUDIO_REF_DELAY_FRAMES + 1U);
            if (ref_hist_count < (uint32_t)CONFIG_AUDIO_REF_DELAY_FRAMES) {
                ref_hist_count++;
                memset(s_afe_feed_ref_delayed, 0, ref_samples * sizeof(int16_t));
            } else {
                if (ref_hist_count == (uint32_t)CONFIG_AUDIO_REF_DELAY_FRAMES) {
                    ref_hist_count++;
                }
                memcpy(s_afe_feed_ref_delayed, s_afe_feed_ref_hist[ref_hist_wr], ref_samples * sizeof(int16_t));
            }
#else
            memcpy(s_afe_feed_ref_delayed, s_afe_feed_ref_frame, ref_samples * sizeof(int16_t));
#endif
            const size_t ch = active_mic_channels();
            const size_t in_frames = mic_samples / ch;
            const size_t mic_afe_frames = downsample_48k_to_16k_interleaved(
                s_afe_feed_mic_frame, in_frames, ch, s_afe_feed_mic_16k,
                sizeof(s_afe_feed_mic_16k) / sizeof(int16_t) / ch);
            const size_t ref_afe_frames = downsample_48k_to_16k_interleaved(
                s_afe_feed_ref_delayed, in_frames, REF_CHANNELS, s_afe_feed_ref_16k,
                sizeof(s_afe_feed_ref_16k) / sizeof(int16_t));
            const size_t afe_frames = (mic_afe_frames < ref_afe_frames) ? mic_afe_frames : ref_afe_frames;
            if (afe_frames == 0) {
                vTaskDelay(1);
                continue;
            }
            ESP_ERROR_CHECK_WITHOUT_ABORT(afe_engine_feed(
                s_afe_feed_mic_16k, afe_frames * ch, s_afe_feed_ref_16k, afe_frames * REF_CHANNELS, frame_seq));
            taskYIELD();
        } else if (ret == ESP_ERR_NOT_FOUND) {
            // No new aligned frame yet; block one tick to avoid CPU spin/wdt.
            vTaskDelay(1);
        } else {
            ESP_LOGW(TAG, "buffer pop err=%s", esp_err_to_name(ret));
            vTaskDelay(1);
        }
    }
}

static void afe_fetch_task_fn(void *arg) {
    (void)arg;
    int16_t out_frame[AFE_FETCH_SAMPLES];
    uint32_t frame_seq = 0;
    while (true) {
        size_t valid_samples = 0;
        esp_err_t ret =
            afe_engine_fetch(out_frame, AFE_FETCH_SAMPLES, &valid_samples, &frame_seq);
        if (ret == ESP_OK) {
            update_frame_stats(out_frame, valid_samples, &s_out_avg_abs, &s_out_rms, &s_out_mean,
                               &s_out_peak);
            if (s_output_tap != NULL && valid_samples > 0) {
                s_output_tap(out_frame, valid_samples, frame_seq, s_output_tap_ctx);
            }
            // In steady state the next fetch attempt will naturally block until
            // enough feed has accumulated. Adding an extra tick delay here
            // artificially halves/triples fetch throughput on 100 Hz RTOS ticks
            // and can overflow the AFE(FEED) ringbuffer.
            taskYIELD();
        } else {
            if (ret != ESP_ERR_TIMEOUT) {
                ESP_LOGW(TAG, "afe fetch err=%s", esp_err_to_name(ret));
            }
            // No data available; wait 1 tick before retrying to avoid CPU spin.
            vTaskDelay(1);
        }
    }
}

static void monitor_task_fn(void *arg) {
    (void)arg;
    const TickType_t delay_ticks = pdMS_TO_TICKS(1000);
    while (true) {
        audio_buffers_stats_t bstats = audio_buffers_get_stats();
        afe_engine_stats_t astats = {0};
        audio_i2s_out_stats_t ostats = audio_i2s_out_get_stats(true);
        const uint32_t cap_hw =
            (s_ctx.capture_task != NULL) ? (uint32_t)uxTaskGetStackHighWaterMark(s_ctx.capture_task)
                                         : 0;
        const uint32_t pb_hw =
            (s_ctx.playback_task != NULL) ? (uint32_t)uxTaskGetStackHighWaterMark(s_ctx.playback_task)
                                          : 0;
        const uint32_t afe_feed_hw = (s_ctx.afe_feed_task != NULL)
                                         ? (uint32_t)uxTaskGetStackHighWaterMark(s_ctx.afe_feed_task)
                                         : 0;
        const uint32_t afe_fetch_hw = (s_ctx.afe_fetch_task != NULL)
                                          ? (uint32_t)uxTaskGetStackHighWaterMark(s_ctx.afe_fetch_task)
                                          : 0;
        const uint32_t mon_hw =
            (s_ctx.monitor_task != NULL) ? (uint32_t)uxTaskGetStackHighWaterMark(s_ctx.monitor_task)
                                         : 0;
        if (CONFIG_AUDIO_ENABLE_AFE) {
            astats = afe_engine_get_stats();
        }
        if (active_mic_channels() == 1) {
            ESP_LOGI(
                TAG,
                "stats mic=%u ref=%u align=%u drop=%u feed=%u fetch=%u timeout=%u | "
                "mic0(avg=%u rms=%u mean=%d peak=%d sat=%u.%u%% zero=%u.%u%%) "
                "out(avg=%u rms=%u mean=%d peak=%d) aec=%u "
                "vad(last=%u speech=%u silence=%u) "
                "stack_hw(cap=%u pb=%u afeed=%u afetch=%u mon=%u)",
                bstats.mic_pushed, bstats.ref_pushed, bstats.aligned_popped, bstats.drop_count,
                astats.feed_count, astats.fetch_count, astats.timeout_count, s_mic_avg_abs[0],
                s_mic_rms[0], s_mic_mean[0], s_mic_peak[0], s_mic_sat_permille[0] / 10,
                s_mic_sat_permille[0] % 10, s_mic_zero_permille[0] / 10, s_mic_zero_permille[0] % 10,
                s_out_avg_abs, s_out_rms, s_out_mean, s_out_peak, afe_engine_get_aec_enabled() ? 1 : 0,
                astats.vad_last_state, astats.vad_speech_count, astats.vad_silence_count,
                cap_hw, pb_hw, afe_feed_hw, afe_fetch_hw, mon_hw);
        } else {
            ESP_LOGI(TAG,
                     "stats mic=%u ref=%u align=%u drop=%u feed=%u fetch=%u timeout=%u | "
                     "mic0(avg=%u rms=%u mean=%d peak=%d sat=%u.%u%% z=%u.%u%%) "
                     "mic1(avg=%u rms=%u mean=%d peak=%d sat=%u.%u%% z=%u.%u%%) "
                     "out(avg=%u rms=%u mean=%d peak=%d) aec=%u "
                     "vad(last=%u speech=%u silence=%u) "
                     "stack_hw(cap=%u pb=%u afeed=%u afetch=%u mon=%u)",
                     bstats.mic_pushed, bstats.ref_pushed, bstats.aligned_popped,
                     bstats.drop_count, astats.feed_count, astats.fetch_count,
                     astats.timeout_count, s_mic_avg_abs[0], s_mic_rms[0], s_mic_mean[0],
                     s_mic_peak[0], s_mic_sat_permille[0] / 10, s_mic_sat_permille[0] % 10,
                     s_mic_zero_permille[0] / 10, s_mic_zero_permille[0] % 10, s_mic_avg_abs[1],
                     s_mic_rms[1], s_mic_mean[1], s_mic_peak[1], s_mic_sat_permille[1] / 10,
                     s_mic_sat_permille[1] % 10, s_mic_zero_permille[1] / 10,
                     s_mic_zero_permille[1] % 10, s_out_avg_abs, s_out_rms, s_out_mean, s_out_peak,
                     afe_engine_get_aec_enabled() ? 1 : 0, astats.vad_last_state,
                     astats.vad_speech_count, astats.vad_silence_count, cap_hw, pb_hw, afe_feed_hw,
                     afe_fetch_hw, mon_hw);
        }
        ESP_LOGI(
            TAG,
            "i2s_out seq=%u last(avg=%u peak=%u) win(calls=%u avg=%u peak=%u wr_us=%u chunk_us=%u "
            "zero=%u part=%u rep=%u run=%u to=%u fail=%u short=%u)",
            ostats.last_frame_seq, ostats.last_frame_avg_abs, ostats.last_frame_peak,
            ostats.window_write_calls, ostats.window_max_frame_avg_abs, ostats.window_max_frame_peak,
            ostats.window_max_write_us, ostats.window_max_chunk_us, ostats.window_zero_write_calls,
            ostats.window_partial_write_calls, ostats.window_repeated_frame_events,
            ostats.window_repeat_run_max, ostats.window_write_timeouts, ostats.window_write_failures,
            ostats.window_max_short_write_samples);
        vTaskDelay(delay_ticks);
    }
}

esp_err_t audio_pipeline_init(void) {
    if (s_ctx.inited) {
        return ESP_OK;
    }

    audio_i2s_in_config_t in_cfg = {
        .sample_rate_hz = PIPELINE_SAMPLE_RATE_HZ,
        .bits_per_sample = 16,
        .channels = (uint8_t)active_mic_channels(),
    };
#if CONFIG_UAC_SPEAKER_CHANNEL_NUM > 0
    audio_i2s_out_config_t out_cfg = {
        .sample_rate_hz = PIPELINE_SAMPLE_RATE_HZ,
        .bits_per_sample = 16,
        .channels = 1,
    };
#endif
    audio_buffers_config_t buf_cfg = {
        .samples_per_frame = FRAME_SAMPLES,
        .mic_channels = (uint8_t)active_mic_channels(),
        .ref_channels = (uint8_t)REF_CHANNELS,
    };
    afe_engine_config_t afe_cfg = {
        .sample_rate_hz = AFE_SAMPLE_RATE_HZ,
        .mic_channels = (uint8_t)active_mic_channels(),
        .ref_channels = (uint8_t)REF_CHANNELS,
        .high_perf_mode = CONFIG_AUDIO_AFE_HIGH_PERF_MODE ? 1 : 0,
        .enable_aec = CONFIG_AUDIO_AEC_ENABLE ? 1 : 0,
        .enable_ns = 0,
        .enable_vad = 0,
        .enable_agc = 0,
    };

    s_capture_seq = 0;
    s_playback_seq = 0;
    s_mic_avg_abs[0] = 0;
    s_mic_avg_abs[1] = 0;
    s_mic_peak[0] = 0;
    s_mic_peak[1] = 0;
    s_mic_mean[0] = 0;
    s_mic_mean[1] = 0;
    s_mic_rms[0] = 0;
    s_mic_rms[1] = 0;
    s_mic_sat_permille[0] = 0;
    s_mic_sat_permille[1] = 0;
    s_mic_zero_permille[0] = 0;
    s_mic_zero_permille[1] = 0;
    s_out_avg_abs = 0;
    s_out_rms = 0;
    s_out_mean = 0;
    s_out_peak = 0;

    ESP_ERROR_CHECK(audio_i2s_in_init(&in_cfg));
#if CONFIG_UAC_SPEAKER_CHANNEL_NUM > 0
    ESP_ERROR_CHECK(audio_i2s_out_init(&out_cfg));
#endif
    ESP_ERROR_CHECK(audio_buffers_init(&buf_cfg));
    if (CONFIG_AUDIO_ENABLE_AFE) {
        ESP_ERROR_CHECK(afe_engine_init(&afe_cfg));
    }

    s_ctx.inited = true;
    ESP_LOGI(TAG, "pipeline initialized, mic_channels=%u afe_mode=%s ref_delay=%u",
             CONFIG_AUDIO_MIC_CHANNELS,
             CONFIG_AUDIO_AFE_HIGH_PERF_MODE ? "high-perf" : "low-cost",
             CONFIG_AUDIO_REF_DELAY_FRAMES);
    return ESP_OK;
}

esp_err_t audio_pipeline_start(void) {
    if (!s_ctx.inited) {
        return ESP_ERR_INVALID_STATE;
    }
    if (s_ctx.running) {
        return ESP_OK;
    }

    ESP_ERROR_CHECK(audio_i2s_in_start());
#if CONFIG_UAC_SPEAKER_CHANNEL_NUM > 0
    ESP_ERROR_CHECK(audio_i2s_out_start());
#endif

    BaseType_t ok = pdPASS;
#if CONFIG_UAC_SPEAKER_CHANNEL_NUM > 0
    // Playback MUST be highest priority on core 0: I2S TX DMA cannot tolerate
    // starvation. Capture can run at slightly lower priority without issues.
    ok = xTaskCreatePinnedToCore(playback_task_fn, "audio_playback_task", PLAYBACK_STACK_BYTES, NULL, 20,
                                 &s_ctx.playback_task, 0);
    if (ok != pdPASS) {
        goto start_rollback;
    }
#endif
    ok = xTaskCreatePinnedToCore(capture_task_fn, "audio_capture_task", CAPTURE_STACK_BYTES, NULL, 19,
                                 &s_ctx.capture_task, 0);
    if (ok != pdPASS) {
        goto start_rollback;
    }
    if (CONFIG_AUDIO_ENABLE_AFE) {
        ok = xTaskCreatePinnedToCore(afe_feed_task_fn, "afe_feed_task", AFE_FEED_STACK_BYTES,
                                     NULL, AFE_FEED_TASK_PRIO, &s_ctx.afe_feed_task, 1);
        if (ok != pdPASS) {
            goto start_rollback;
        }
        ok = xTaskCreatePinnedToCore(afe_fetch_task_fn, "afe_fetch_task", AFE_FETCH_STACK_BYTES,
                                     NULL, AFE_FETCH_TASK_PRIO, &s_ctx.afe_fetch_task, 1);
        if (ok != pdPASS) {
            goto start_rollback;
        }
    }
    ok = xTaskCreatePinnedToCore(monitor_task_fn, "monitor_task", MONITOR_STACK_BYTES, NULL, 5,
                                 &s_ctx.monitor_task, 1);
    if (ok != pdPASS) {
        goto start_rollback;
    }

    s_ctx.running = true;
    ESP_LOGI(TAG, "pipeline started");
    return ESP_OK;

start_rollback:
    /* Partial task creation: delete any created tasks and stop I2S so start() retry is safe. */
    if (s_ctx.monitor_task != NULL) {
        vTaskDelete(s_ctx.monitor_task);
        s_ctx.monitor_task = NULL;
    }
    if (s_ctx.afe_fetch_task != NULL) {
        vTaskDelete(s_ctx.afe_fetch_task);
        s_ctx.afe_fetch_task = NULL;
    }
    if (s_ctx.afe_feed_task != NULL) {
        vTaskDelete(s_ctx.afe_feed_task);
        s_ctx.afe_feed_task = NULL;
    }
    if (s_ctx.capture_task != NULL) {
        vTaskDelete(s_ctx.capture_task);
        s_ctx.capture_task = NULL;
    }
    if (s_ctx.playback_task != NULL) {
        vTaskDelete(s_ctx.playback_task);
        s_ctx.playback_task = NULL;
    }
#if CONFIG_UAC_SPEAKER_CHANNEL_NUM > 0
    audio_i2s_out_stop();
#endif
    audio_i2s_in_stop();
    ESP_LOGW(TAG, "pipeline start failed (no memory), rolled back");
    return ESP_ERR_NO_MEM;
}

esp_err_t audio_pipeline_stop(void) {
    if (!s_ctx.running) {
        return ESP_OK;
    }
    if (s_ctx.capture_task != NULL) {
        vTaskDelete(s_ctx.capture_task);
        s_ctx.capture_task = NULL;
    }
    if (s_ctx.playback_task != NULL) {
        vTaskDelete(s_ctx.playback_task);
        s_ctx.playback_task = NULL;
    }
    if (s_ctx.afe_feed_task != NULL) {
        vTaskDelete(s_ctx.afe_feed_task);
        s_ctx.afe_feed_task = NULL;
    }
    if (s_ctx.afe_fetch_task != NULL) {
        vTaskDelete(s_ctx.afe_fetch_task);
        s_ctx.afe_fetch_task = NULL;
    }
    if (s_ctx.monitor_task != NULL) {
        vTaskDelete(s_ctx.monitor_task);
        s_ctx.monitor_task = NULL;
    }

    ESP_ERROR_CHECK(audio_i2s_in_stop());
#if CONFIG_UAC_SPEAKER_CHANNEL_NUM > 0
    ESP_ERROR_CHECK(audio_i2s_out_stop());
#endif

    s_ctx.running = false;
    ESP_LOGI(TAG, "pipeline stopped");
    return ESP_OK;
}

esp_err_t audio_pipeline_set_playback_provider(audio_pipeline_playback_provider_t provider, void *ctx) {
    if (s_ctx.running) {
        return ESP_ERR_INVALID_STATE;
    }
    s_playback_provider = provider;
    s_playback_provider_ctx = ctx;
    return ESP_OK;
}

esp_err_t audio_pipeline_set_output_tap(audio_pipeline_output_tap_t tap, void *ctx) {
    if (s_ctx.running) {
        return ESP_ERR_INVALID_STATE;
    }
    s_output_tap = tap;
    s_output_tap_ctx = ctx;
    return ESP_OK;
}

esp_err_t audio_pipeline_set_input_tap(audio_pipeline_input_tap_t tap, void *ctx) {
    if (s_ctx.running) {
        return ESP_ERR_INVALID_STATE;
    }
    s_input_tap = tap;
    s_input_tap_ctx = ctx;
    return ESP_OK;
}
