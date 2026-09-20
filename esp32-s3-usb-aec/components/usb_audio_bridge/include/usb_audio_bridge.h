#pragma once

#include <stdbool.h>
#include <stdint.h>

#include "esp_err.h"

#ifdef __cplusplus
extern "C" {
#endif

/* Counters are uint32_t and will wrap after long runs; use deltas for rate math. */
typedef struct {
    uint32_t usb_rx_bytes;
    uint32_t usb_rx_drop_bytes;
    uint32_t playback_bytes;
    uint32_t playback_underrun_frames;
    uint32_t asrc_step_q16;
    uint32_t usb_rx_overrun_events;
    uint32_t afe_out_bytes;
    uint32_t afe_out_drop_bytes;
    uint32_t usb_tx_bytes;
    uint32_t usb_tx_underrun_calls;
    uint32_t usb_tx_overrun_events;
    uint32_t mic_host_open_events;
    uint32_t mic_host_idle_resets;
    uint32_t mic_frame_seq_repeat;
    uint32_t mic_frame_seq_backtrack;
    uint32_t host_volume_percent;
    bool host_mute;
} usb_audio_bridge_stats_t;

esp_err_t usb_audio_bridge_init(void);
usb_audio_bridge_stats_t usb_audio_bridge_get_stats(void);

#ifdef __cplusplus
}
#endif
