/*
 * SPDX-FileCopyrightText: 2023-2024 Espressif Systems (Shanghai) CO LTD
 *
 * SPDX-License-Identifier: Apache-2.0
 */

#include <string.h>
#include <inttypes.h>
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "esp_check.h"
#include "esp_err.h"
#include "esp_private/usb_phy.h"
#include "esp_timer.h"
#include "tusb.h"
#include "uac_config.h"
#include "usb_device_uac.h"
#include "uac_descriptors.h"

static const char *TAG = "usbd_uac";

const uint32_t sample_rates[] = {DEFAULT_SAMPLE_RATE};

#define N_SAMPLE_RATES  TU_ARRAY_SIZE(sample_rates)

enum {
    VOLUME_CTRL_0_DB = 0,
    VOLUME_CTRL_10_DB = 2560,
    VOLUME_CTRL_20_DB = 5120,
    VOLUME_CTRL_30_DB = 7680,
    VOLUME_CTRL_40_DB = 10240,
    VOLUME_CTRL_50_DB = 12800,
    VOLUME_CTRL_60_DB = 15360,
    VOLUME_CTRL_70_DB = 17920,
    VOLUME_CTRL_80_DB = 20480,
    VOLUME_CTRL_90_DB = 23040,
    VOLUME_CTRL_100_DB = 25600,
    VOLUME_CTRL_SILENCE = 0x8000,
};

// Resolution per format
const uint8_t spk_resolutions_per_format[CFG_TUD_AUDIO_FUNC_1_N_FORMATS] = {CFG_TUD_AUDIO_FUNC_1_FORMAT_1_RESOLUTION_RX};
const uint8_t mic_resolutions_per_format[CFG_TUD_AUDIO_FUNC_1_N_FORMATS] = {CFG_TUD_AUDIO_FUNC_1_FORMAT_1_RESOLUTION_TX};

typedef struct {
    usb_phy_handle_t phy_hdl;
    uac_device_config_t user_cfg;
    int8_t mute[CFG_TUD_AUDIO_FUNC_1_N_CHANNELS_TX + 1];         // +1 for master channel 0
    int16_t volume[CFG_TUD_AUDIO_FUNC_1_N_CHANNELS_TX + 1];      // +1 for master channel 0
    int8_t mic_mute[CFG_TUD_AUDIO_FUNC_1_N_CHANNELS_RX + 1];     // +1 for master channel 0
    int16_t mic_volume[CFG_TUD_AUDIO_FUNC_1_N_CHANNELS_RX + 1];  // +1 for master channel 0
    int16_t mic_buf1[CFG_TUD_AUDIO_FUNC_1_EP_IN_SW_BUF_SZ / 2];  // Buffer for microphone data
    int16_t mic_buf2[CFG_TUD_AUDIO_FUNC_1_EP_IN_SW_BUF_SZ / 2];  // Buffer for microphone data
    int16_t spk_buf[CFG_TUD_AUDIO_FUNC_1_EP_OUT_SW_BUF_SZ / 2];  // Buffer for speaker data
    int16_t *mic_buf_write;                                      // Pointer to the buffer to write to
    int16_t *mic_buf_read;                                       // Pointer to the buffer to read from
    int spk_data_size;                                           // Speaker data size received in the last frame
    int mic_data_size;
    int spk_itf_num;
    int mic_itf_num;
    uint8_t spk_resolution;
    uint8_t mic_resolution;
    uint32_t current_sample_rate;                                // Current resolution, update on format change
    TaskHandle_t mic_task_handle;
    TaskHandle_t spk_task_handle;
    size_t spk_bytes_per_ms;
    size_t mic_bytes_per_ms;
    bool spk_active;
    bool mic_active;
    bool spk_suspended;                                          // Was spk active before USB suspend?
    bool mic_suspended;                                          // Was mic active before USB suspend?
} uac_device_t;

static uac_device_t *s_uac_device = NULL;

static void notify_stream_state(uac_stream_t stream, bool active)
{
    if (s_uac_device != NULL && s_uac_device->user_cfg.stream_state_cb != NULL) {
        s_uac_device->user_cfg.stream_state_cb(stream, active, s_uac_device->user_cfg.cb_ctx);
    }
}
static portMUX_TYPE s_mux = portMUX_INITIALIZER_UNLOCKED;
#define UAC_ENTER_CRITICAL()    portENTER_CRITICAL(&s_mux)
#define UAC_EXIT_CRITICAL()     portEXIT_CRITICAL(&s_mux)

static size_t mic_task_period_ms(void)
{
    size_t period_ms = MIC_INTERVAL_MS;
    size_t tick_ms = pdTICKS_TO_MS(1);
    if (tick_ms == 0) {
        tick_ms = 1;
    }
    if (period_ms < tick_ms) {
        period_ms = tick_ms;
    }
    return period_ms;
}

static size_t mic_task_bytes_require(void)
{
    if (s_uac_device == NULL) {
        return 0;
    }
    size_t bytes = mic_task_period_ms() * s_uac_device->mic_bytes_per_ms;
    if (bytes > sizeof(s_uac_device->mic_buf1)) {
        bytes = sizeof(s_uac_device->mic_buf1);
    }
    return bytes;
}

static bool get_feature_unit_state(uint8_t entity_id, int8_t **mute, int16_t **volume, const char **name)
{
    if (entity_id == UAC2_ENTITY_SPK_FEATURE_UNIT) {
        *mute = s_uac_device->mute;
        *volume = s_uac_device->volume;
        *name = "speaker";
        return true;
    }
#if CFG_TUD_AUDIO_FUNC_1_N_CHANNELS_RX
    if (entity_id == UAC2_ENTITY_MIC_FEATURE_TERMINAL) {
        *mute = s_uac_device->mic_mute;
        *volume = s_uac_device->mic_volume;
        *name = "microphone";
        return true;
    }
#endif
    return false;
}

static void usb_phy_init(void)
{
    // Configure USB PHY
    usb_phy_config_t phy_conf = {
        .controller = USB_PHY_CTRL_OTG,
        .otg_mode = USB_OTG_MODE_DEVICE,
        .target = USB_PHY_TARGET_INT,
#if CONFIG_TINYUSB_RHPORT_HS
        .otg_speed = USB_PHY_SPEED_HIGH,
#endif
    };
    usb_new_phy(&phy_conf, &s_uac_device->phy_hdl);
}

static void tusb_device_task(void *arg)
{
    while (1) {
        tud_task();
    }
}

#if !CONFIG_USB_DEVICE_UAC_AS_PART
// Invoked when device is mounted
void tud_mount_cb(void)
{
    s_uac_device->spk_active = false;
    s_uac_device->mic_active = false;
    ESP_LOGI(TAG, "USB mounted");
}

// Invoked when device is unmounted
void tud_umount_cb(void)
{
    if (s_uac_device->spk_active) {
        notify_stream_state(UAC_STREAM_SPEAKER, false);
    }
    if (s_uac_device->mic_active) {
        notify_stream_state(UAC_STREAM_MICROPHONE, false);
    }
    s_uac_device->spk_active = false;
    s_uac_device->mic_active = false;
    s_uac_device->spk_data_size = 0;
    s_uac_device->mic_data_size = 0;
    ESP_LOGW(TAG, "USB unmounted – streams deactivated");
}

// Invoked when usb bus is suspended
// remote_wakeup_en : if host allow us to perform remote wakeup
// Within 7ms, device must draw an average of current less than 2.5 mA from bus
void tud_suspend_cb(bool remote_wakeup_en)
{
    (void)remote_wakeup_en;
    /* Remember which streams were active before suspend so we can restore
     * them on resume.  macOS often resumes without re-issuing SET_INTERFACE,
     * which means spk_active / mic_active would stay false forever. */
    s_uac_device->spk_suspended = s_uac_device->spk_active;
    s_uac_device->mic_suspended = s_uac_device->mic_active;
    if (s_uac_device->spk_active) {
        notify_stream_state(UAC_STREAM_SPEAKER, false);
    }
    if (s_uac_device->mic_active) {
        notify_stream_state(UAC_STREAM_MICROPHONE, false);
    }
    s_uac_device->spk_active = false;
    s_uac_device->mic_active = false;
    ESP_LOGW(TAG, "USB suspended (spk_was=%d mic_was=%d)",
             s_uac_device->spk_suspended, s_uac_device->mic_suspended);
}

// Invoked when usb bus is resumed
void tud_resume_cb(void)
{
    /* Restore streams that were active before suspend.  If the host later
     * sends SET_INTERFACE alt=0 (close) followed by alt=1 (open), those
     * callbacks will update the flags again, so this is safe. */
    if (s_uac_device->spk_suspended) {
        s_uac_device->spk_active = true;
        notify_stream_state(UAC_STREAM_SPEAKER, true);
        xTaskNotifyGive(s_uac_device->spk_task_handle);
    }
    if (s_uac_device->mic_suspended) {
        s_uac_device->mic_active = true;
        notify_stream_state(UAC_STREAM_MICROPHONE, true);
        xTaskNotifyGive(s_uac_device->mic_task_handle);
    }
    ESP_LOGW(TAG, "USB resumed – restored spk=%d mic=%d",
             s_uac_device->spk_active, s_uac_device->mic_active);
}
#endif

// Helper for clock get requests
static bool is_clock_entity(uint8_t entity_id)
{
#if CFG_TUD_AUDIO_FUNC_1_N_CHANNELS_RX && CFG_TUD_AUDIO_FUNC_1_N_CHANNELS_TX
    return entity_id == UAC2_ENTITY_SPK_CLOCK || entity_id == UAC2_ENTITY_MIC_CLOCK;
#else
    return entity_id == UAC2_ENTITY_CLOCK;
#endif
}

static bool tud_audio_clock_get_request(uint8_t rhport, audio_control_request_t const *request)
{
    TU_ASSERT(is_clock_entity(request->bEntityID));

    if (request->bControlSelector == AUDIO_CS_CTRL_SAM_FREQ) {
        if (request->bRequest == AUDIO_CS_REQ_CUR) {
            TU_LOG1("Clock get current freq %lu\r\n", s_uac_device->current_sample_rate);
            audio_control_cur_4_t curf = { (int32_t) tu_htole32(s_uac_device->current_sample_rate) };
            return tud_audio_buffer_and_schedule_control_xfer(rhport, (tusb_control_request_t const *)request, &curf, sizeof(curf));
        } else if (request->bRequest == AUDIO_CS_REQ_RANGE) {
            audio_control_range_4_n_t(N_SAMPLE_RATES) rangef = {
                .wNumSubRanges = tu_htole16(N_SAMPLE_RATES)
            };
            TU_LOG1("Clock get %d freq ranges\r\n", N_SAMPLE_RATES);
            for (uint8_t i = 0; i < N_SAMPLE_RATES; i++) {
                rangef.subrange[i].bMin = (int32_t) sample_rates[i];
                rangef.subrange[i].bMax = (int32_t) sample_rates[i];
                rangef.subrange[i].bRes = 0;
                TU_LOG1("Range %d (%d, %d, %d)\r\n", i, (int)rangef.subrange[i].bMin, (int)rangef.subrange[i].bMax, (int)rangef.subrange[i].bRes);
            }

            return tud_audio_buffer_and_schedule_control_xfer(rhport, (tusb_control_request_t const *)request, &rangef, sizeof(rangef));
        }
    } else if (request->bControlSelector == AUDIO_CS_CTRL_CLK_VALID && request->bRequest == AUDIO_CS_REQ_CUR) {
        audio_control_cur_1_t cur_valid = {
            .bCur = 1
        };
        TU_LOG1("Clock get is valid %u\r\n", cur_valid.bCur);
        return tud_audio_buffer_and_schedule_control_xfer(rhport, (tusb_control_request_t const *)request, &cur_valid, sizeof(cur_valid));
    }

    TU_LOG1("Clock get request not supported, entity = %u, selector = %u, request = %u\r\n", request->bEntityID, request->bControlSelector, request->bRequest);
    return false;
}

// Helper for clock set requests
static bool tud_audio_clock_set_request(uint8_t rhport, audio_control_request_t const *request, uint8_t const *buf)
{
    (void)rhport;

    TU_ASSERT(is_clock_entity(request->bEntityID));
    TU_VERIFY(request->bRequest == AUDIO_CS_REQ_CUR);

    if (request->bControlSelector == AUDIO_CS_CTRL_SAM_FREQ) {
        TU_VERIFY(request->wLength == sizeof(audio_control_cur_4_t));

        uint32_t target_sample_rate = (uint32_t)((audio_control_cur_4_t const *)buf)->bCur;
        TU_LOG1("Clock set current freq: %ld\r\n", target_sample_rate);

        if (target_sample_rate != s_uac_device->current_sample_rate) {
            // For now, we only support one sample rate
            return false;
        }

        return true;
    } else {
        TU_LOG1("Clock set request not supported, entity = %u, selector = %u, request = %u\r\n",
                request->bEntityID, request->bControlSelector, request->bRequest);
        return false;
    }
}

// Helper for feature unit get requests
static bool tud_audio_feature_unit_get_request(uint8_t rhport, audio_control_request_t const *request)
{
    int8_t *mute = NULL;
    int16_t *volume = NULL;
    const char *unit_name = NULL;
    TU_VERIFY(get_feature_unit_state(request->bEntityID, &mute, &volume, &unit_name));

    if (request->bControlSelector == AUDIO_FU_CTRL_MUTE && request->bRequest == AUDIO_CS_REQ_CUR) {
        audio_control_cur_1_t mute1 = {
            .bCur = mute[request->bChannelNumber]
        };
        TU_LOG1("Get %s channel %u mute %d\r\n", unit_name, request->bChannelNumber, mute1.bCur);
        return tud_audio_buffer_and_schedule_control_xfer(rhport, (tusb_control_request_t const *)request, &mute1, sizeof(mute1));
    } else if (request->bControlSelector == AUDIO_FU_CTRL_VOLUME) {
        if (request->bRequest == AUDIO_CS_REQ_RANGE) {
            audio_control_range_2_n_t(1) range_vol = {
                .wNumSubRanges = tu_htole16(1),
                .subrange[0] = { .bMin = tu_htole16(-VOLUME_CTRL_50_DB), tu_htole16(VOLUME_CTRL_0_DB), tu_htole16(256) }
            };
            TU_LOG1("Get %s channel %u volume range (%d, %d, %u) dB\r\n", unit_name, request->bChannelNumber,
                    range_vol.subrange[0].bMin / 256, range_vol.subrange[0].bMax / 256, range_vol.subrange[0].bRes / 256);
            return tud_audio_buffer_and_schedule_control_xfer(rhport, (tusb_control_request_t const *)request, &range_vol, sizeof(range_vol));
        } else if (request->bRequest == AUDIO_CS_REQ_CUR) {
            audio_control_cur_2_t cur_vol = {
                .bCur = tu_htole16(volume[request->bChannelNumber])
            };
            TU_LOG1("Get %s channel %u volume %d dB\r\n", unit_name, request->bChannelNumber, cur_vol.bCur / 256);
            return tud_audio_buffer_and_schedule_control_xfer(rhport, (tusb_control_request_t const *)request, &cur_vol, sizeof(cur_vol));
        }
    }
    TU_LOG1("Feature unit get request not supported, entity = %u, selector = %u, request = %u\r\n",
            request->bEntityID, request->bControlSelector, request->bRequest);

    return false;
}

static bool tud_audio_feature_unit_set_request(uint8_t rhport, audio_control_request_t const *request, uint8_t const *buf)
{
    (void)rhport;

    int8_t *mute = NULL;
    int16_t *volume = NULL;
    const char *unit_name = NULL;
    TU_VERIFY(get_feature_unit_state(request->bEntityID, &mute, &volume, &unit_name));
    TU_VERIFY(request->bRequest == AUDIO_CS_REQ_CUR);

    if (request->bControlSelector == AUDIO_FU_CTRL_MUTE) {
        TU_VERIFY(request->wLength == sizeof(audio_control_cur_1_t));
        mute[request->bChannelNumber] = ((audio_control_cur_1_t const *)buf)->bCur;
        TU_LOG1("Set %s channel %d mute: %d\r\n", unit_name, request->bChannelNumber, mute[request->bChannelNumber]);
        if (request->bEntityID == UAC2_ENTITY_SPK_FEATURE_UNIT && s_uac_device->user_cfg.set_mute_cb) {
            s_uac_device->user_cfg.set_mute_cb(mute[request->bChannelNumber], s_uac_device->user_cfg.cb_ctx);
        }

        return true;
    } else if (request->bControlSelector == AUDIO_FU_CTRL_VOLUME) {
        TU_VERIFY(request->wLength == sizeof(audio_control_cur_2_t));
        volume[request->bChannelNumber] = ((audio_control_cur_2_t const *)buf)->bCur;
        int volume_db = volume[request->bChannelNumber] / 256; // Convert to dB
        int volume_percent = (volume_db + 50) * 2; // Map to range 0 to 100
        TU_LOG1("Set %s channel %d volume: %d dB (%d)\r\n", unit_name, request->bChannelNumber, volume_db, volume_percent);
        if (request->bEntityID == UAC2_ENTITY_SPK_FEATURE_UNIT && s_uac_device->user_cfg.set_volume_cb) {
            s_uac_device->user_cfg.set_volume_cb(volume_percent, s_uac_device->user_cfg.cb_ctx);
        }
        return true;
    } else {
        TU_LOG1("Feature unit set request not supported, entity = %u, selector = %u, request = %u\r\n",
                request->bEntityID, request->bControlSelector, request->bRequest);
        return false;
    }
}

//--------------------------------------------------------------------+
// Application Callback API Implementations
//--------------------------------------------------------------------+

// Invoked when audio class specific get request received for an entity
bool tud_audio_get_req_entity_cb(uint8_t rhport, tusb_control_request_t const *p_request)
{
    audio_control_request_t const *request = (audio_control_request_t const *)p_request;

    if (is_clock_entity(request->bEntityID)) {
        return tud_audio_clock_get_request(rhport, request);
    }
    if (request->bEntityID == UAC2_ENTITY_SPK_FEATURE_UNIT
#if CFG_TUD_AUDIO_FUNC_1_N_CHANNELS_RX
        || request->bEntityID == UAC2_ENTITY_MIC_FEATURE_TERMINAL
#endif
    ) {
        return tud_audio_feature_unit_get_request(rhport, request);
    } else {
        TU_LOG1("Get request not handled, entity = %d, selector = %d, request = %d\r\n",
                request->bEntityID, request->bControlSelector, request->bRequest);
    }
    return false;
}

// Invoked when audio class specific set request received for an entity
bool tud_audio_set_req_entity_cb(uint8_t rhport, tusb_control_request_t const *p_request, uint8_t *buf)
{
    audio_control_request_t const *request = (audio_control_request_t const *)p_request;

    if (request->bEntityID == UAC2_ENTITY_SPK_FEATURE_UNIT
#if CFG_TUD_AUDIO_FUNC_1_N_CHANNELS_RX
        || request->bEntityID == UAC2_ENTITY_MIC_FEATURE_TERMINAL
#endif
    ) {
        return tud_audio_feature_unit_set_request(rhport, request, buf);
    }
    if (is_clock_entity(request->bEntityID)) {
        return tud_audio_clock_set_request(rhport, request, buf);
    }
    TU_LOG1("Set request not handled, entity = %d, selector = %d, request = %d\r\n",
            request->bEntityID, request->bControlSelector, request->bRequest);

    return false;
}

bool tud_audio_set_itf_close_EP_cb(uint8_t rhport, tusb_control_request_t const *p_request)
{
    (void)rhport;

    uint8_t const itf = tu_u16_low(tu_le16toh(p_request->wIndex));
    uint8_t const alt = tu_u16_low(tu_le16toh(p_request->wValue));

#if CFG_TUD_AUDIO_FUNC_1_N_CHANNELS_TX
    if (s_uac_device->spk_itf_num == itf && alt == 0) {
        TU_LOG2("Speaker interface closed");
        s_uac_device->spk_data_size = 0;
        s_uac_device->spk_active = false;
        notify_stream_state(UAC_STREAM_SPEAKER, false);
        printf("Speaker interface %d-%d closed\n", itf, alt);
    }
#endif

#if CFG_TUD_AUDIO_FUNC_1_N_CHANNELS_RX
    if (s_uac_device->mic_itf_num == itf && alt == 0) {
        TU_LOG2("Microphone interface closed");
        s_uac_device->mic_data_size = 0;
        s_uac_device->mic_active = false;
        notify_stream_state(UAC_STREAM_MICROPHONE, false);
        printf("Microphone interface %d-%d closed\n", itf, alt);
    }
#endif

    return true;
}

bool tud_audio_set_itf_cb(uint8_t rhport, tusb_control_request_t const *p_request)
{
    (void)rhport;
    uint8_t const itf = tu_u16_low(tu_le16toh(p_request->wIndex));
    uint8_t const alt = tu_u16_low(tu_le16toh(p_request->wValue));

    TU_LOG2("Set interface %d alt %d\r\n", itf, alt);

#if CFG_TUD_AUDIO_FUNC_1_N_CHANNELS_TX
    if (s_uac_device->spk_itf_num == itf && alt != 0) {
        s_uac_device->spk_data_size = 0;
        s_uac_device->spk_resolution = spk_resolutions_per_format[alt - 1];
        s_uac_device->spk_active = true;
        notify_stream_state(UAC_STREAM_SPEAKER, true);
        s_uac_device->spk_bytes_per_ms = s_uac_device->current_sample_rate / 1000 * SPEAK_CHANNEL_NUM * s_uac_device->spk_resolution / 8;
        xTaskNotifyGive(s_uac_device->spk_task_handle);
        TU_LOG1("Speaker interface %d-%d opened", itf, alt);
        printf("Speaker interface %d-%d opened\n", itf, alt);
    }
#endif

#if CFG_TUD_AUDIO_FUNC_1_N_CHANNELS_RX
    if (s_uac_device->mic_itf_num == itf && alt != 0) {
        s_uac_device->mic_data_size = 0;
        s_uac_device->mic_resolution = mic_resolutions_per_format[alt - 1];
        s_uac_device->mic_active = true;
        notify_stream_state(UAC_STREAM_MICROPHONE, true);
        s_uac_device->mic_bytes_per_ms = s_uac_device->current_sample_rate / 1000 * MIC_CHANNEL_NUM * s_uac_device->mic_resolution / 8;
        xTaskNotifyGive(s_uac_device->mic_task_handle);
        TU_LOG1("Microphone interface %d-%d opened", itf, alt);
        printf("Microphone interface %d-%d opened\n", itf, alt);
    }
#endif

    return true;
}

bool tud_audio_rx_done_post_read_cb(uint8_t rhport, uint16_t n_bytes_received, uint8_t func_id, uint8_t ep_out, uint8_t cur_alt_setting)
{
    (void)rhport;
    (void)func_id;
    (void)ep_out;
    (void)cur_alt_setting;

    static bool new_play = false;
    static int64_t last_time = 0;
    int64_t now = esp_timer_get_time();

    /**
     * @brief If no data is received for a certain period, it is considered as the initiation
     *        of a new audio transmission. At this point, the FIFO data is cleared, and a segment
     *        of data is buffered in the I2S.
     */
    /* esp_timer_get_time() is in microseconds, while CONFIG_UAC_SPK_NEW_PLAY_INTERVAL
     * is configured in milliseconds. Use 1000x conversion here; 100x would turn the
     * intended 100 ms threshold into 10 ms and spuriously re-arm "new play". */
    if (now - last_time > 1000LL * CONFIG_UAC_SPK_NEW_PLAY_INTERVAL) {
        new_play = true;
        tud_audio_clear_ep_out_ff();
    }
    last_time = now;

    int bytes_remained = tud_audio_available();

    size_t bytes_require = s_uac_device->spk_bytes_per_ms;

    if (new_play) {
        /*!< Buffer a segment of data in the I2S and control the data size to be half of the UAC FIFO size. */
        bytes_require = SPK_INTERVAL_MS * s_uac_device->spk_bytes_per_ms / 2;
        if (bytes_remained < bytes_require) {
            return true;
        }
        new_play = false;
    }

    s_uac_device->spk_data_size = tud_audio_read(s_uac_device->spk_buf, bytes_require);
    xTaskNotifyGive(s_uac_device->spk_task_handle);
    return true;
}

bool tud_audio_tx_done_pre_load_cb(uint8_t rhport, uint8_t itf, uint8_t ep_in, uint8_t cur_alt_setting)
{
    (void)rhport;
    (void)itf;
    (void)ep_in;
    (void)cur_alt_setting;

    tu_fifo_t *sw_in_fifo = tud_audio_get_ep_in_ff();
    uint16_t fifo_remained = tu_fifo_remaining(sw_in_fifo);

    // load data chunk by chunk
    UAC_ENTER_CRITICAL();
    if (s_uac_device->mic_data_size > 0) {
        if (fifo_remained >= (uint16_t)s_uac_device->mic_data_size) {
            tud_audio_write((void *)s_uac_device->mic_buf_read, s_uac_device->mic_data_size);
            s_uac_device->mic_data_size = 0;
        }
    }
    UAC_EXIT_CRITICAL();

    return true;
}

#if CFG_TUD_AUDIO_FUNC_1_N_CHANNELS_TX
static void usb_spk_task(void *pvParam)
{
    while (1) {
        if (s_uac_device->spk_active == false) {
            ulTaskNotifyTake(pdFAIL, portMAX_DELAY);
            continue;
        }
        // clear the notification
        ulTaskNotifyTake(pdTRUE, portMAX_DELAY);
        if (s_uac_device->spk_data_size == 0) {
            continue;
        }
        // playback the data from the ring buffer chunk by chunk
        if (s_uac_device->user_cfg.output_cb) {
            s_uac_device->user_cfg.output_cb((uint8_t *)s_uac_device->spk_buf, s_uac_device->spk_data_size, s_uac_device->user_cfg.cb_ctx);
        }
        s_uac_device->spk_data_size = 0;
    }
}
#endif

#if CFG_TUD_AUDIO_FUNC_1_N_CHANNELS_RX
static void usb_mic_task(void *pvParam)
{
    TickType_t xLastWakeTime = xTaskGetTickCount();
    TickType_t delay_ticks = pdMS_TO_TICKS(MIC_INTERVAL_MS);
    if (delay_ticks == 0) {
        delay_ticks = 1;
    }
    while (1) {
        if (s_uac_device->mic_active == false) {
            // clear the notification
            ulTaskNotifyTake(pdTRUE, portMAX_DELAY);
            xLastWakeTime = xTaskGetTickCount();
            continue;
        }
        // clear the notification
        // read data from the microphone chunk by chunk
        size_t bytes_require = mic_task_bytes_require();
        if (s_uac_device->user_cfg.input_cb) {
            size_t bytes_read = 0;
            esp_err_t ret = s_uac_device->user_cfg.input_cb((uint8_t *)s_uac_device->mic_buf_write, bytes_require, &bytes_read, s_uac_device->user_cfg.cb_ctx);
            if (ret != ESP_OK) {
                ESP_LOGE(TAG, "Failed to read data from mic");
                continue;
            }
            int16_t *tmp_buf = s_uac_device->mic_buf_write;
            UAC_ENTER_CRITICAL();
            s_uac_device->mic_buf_write = s_uac_device->mic_buf_read;
            s_uac_device->mic_buf_read = tmp_buf;
            s_uac_device->mic_data_size = bytes_read;
            UAC_EXIT_CRITICAL();
        }

        vTaskDelayUntil(&xLastWakeTime, delay_ticks);
    }
}
#endif

esp_err_t uac_device_init(uac_device_config_t *config)
{
    ESP_RETURN_ON_FALSE(config != NULL, ESP_ERR_INVALID_ARG, TAG, "config is NULL");
    if (s_uac_device != NULL) {
        ESP_LOGW(TAG, "uac device already initialized");
        return ESP_OK;
    }
    s_uac_device = calloc(1, sizeof(uac_device_t));
    ESP_RETURN_ON_FALSE(s_uac_device != NULL, ESP_ERR_NO_MEM, TAG, "Failed to allocate memory for uac device");
    s_uac_device->user_cfg.output_cb = config->output_cb;
    s_uac_device->user_cfg.input_cb = config->input_cb;
    s_uac_device->user_cfg.cb_ctx = config->cb_ctx;
    s_uac_device->user_cfg.set_mute_cb = config->set_mute_cb;
    s_uac_device->user_cfg.set_volume_cb = config->set_volume_cb;
    s_uac_device->user_cfg.stream_state_cb = config->stream_state_cb;
    s_uac_device->current_sample_rate = DEFAULT_SAMPLE_RATE;
    s_uac_device->mic_buf_write = s_uac_device->mic_buf1;
    s_uac_device->mic_buf_read = s_uac_device->mic_buf2;

#if CONFIG_USB_DEVICE_UAC_AS_PART
    s_uac_device->spk_itf_num = config->spk_itf_num;
    s_uac_device->mic_itf_num = config->mic_itf_num;
#else
#if SPEAK_CHANNEL_NUM
    s_uac_device->spk_itf_num = ITF_NUM_AUDIO_STREAMING_SPK;
#endif
#if MIC_CHANNEL_NUM
    s_uac_device->mic_itf_num = ITF_NUM_AUDIO_STREAMING_MIC;
#endif
#endif

    BaseType_t ret_val;
    if (!config->skip_tinyusb_init) {
        usb_phy_init();
        bool usb_init = tusb_init();
        if (!usb_init) {
            ESP_LOGE(TAG, "USB Device Stack Init Fail");
            return ESP_FAIL;
        }
        ret_val = xTaskCreatePinnedToCore(tusb_device_task, "TinyUSB", 4096, NULL, CONFIG_UAC_TINYUSB_TASK_PRIORITY,
                                          NULL, CONFIG_UAC_TINYUSB_TASK_CORE == -1 ? tskNO_AFFINITY : CONFIG_UAC_TINYUSB_TASK_CORE);
        ESP_RETURN_ON_FALSE(ret_val == pdPASS, ESP_FAIL, TAG, "Failed to create TinyUSB task");
    }

#if CFG_TUD_AUDIO_FUNC_1_N_CHANNELS_RX
    ret_val = xTaskCreatePinnedToCore(usb_mic_task, "usb_mic_task", 4096, NULL, CONFIG_UAC_MIC_TASK_PRIORITY,
                                      &s_uac_device->mic_task_handle, CONFIG_UAC_MIC_TASK_CORE == -1 ? tskNO_AFFINITY : CONFIG_UAC_MIC_TASK_CORE);
    ESP_RETURN_ON_FALSE(ret_val == pdPASS, ESP_FAIL, TAG, "Failed to create usb_mic task");
#endif

#if CFG_TUD_AUDIO_FUNC_1_N_CHANNELS_TX
    ret_val = xTaskCreatePinnedToCore(usb_spk_task, "usb_spk_task", 4096, NULL, CONFIG_UAC_SPK_TASK_PRIORITY,
                                      &s_uac_device->spk_task_handle, CONFIG_UAC_SPK_TASK_CORE == -1 ? tskNO_AFFINITY : CONFIG_UAC_SPK_TASK_CORE);
    ESP_RETURN_ON_FALSE(ret_val == pdPASS, ESP_FAIL, TAG, "Failed to create usb_spk task");
#endif

    ESP_LOGI(TAG, "UAC Device Start, Version: %d.%d.%d", USB_DEVICE_UAC_FORK_VER_MAJOR,
             USB_DEVICE_UAC_FORK_VER_MINOR, USB_DEVICE_UAC_FORK_VER_PATCH);
    return ESP_OK;
}

esp_err_t uac_device_recover_streams(bool recover_speaker, bool recover_mic)
{
    ESP_RETURN_ON_FALSE(s_uac_device != NULL, ESP_ERR_INVALID_STATE, TAG, "UAC device not initialized");

    bool notify_spk = false;
    bool notify_mic = false;

    UAC_ENTER_CRITICAL();
#if CFG_TUD_AUDIO_FUNC_1_N_CHANNELS_TX
    if (recover_speaker && s_uac_device->user_cfg.output_cb) {
        s_uac_device->spk_data_size = 0;
        s_uac_device->spk_active = true;
        s_uac_device->spk_suspended = true;
        notify_spk = true;
    }
#endif
#if CFG_TUD_AUDIO_FUNC_1_N_CHANNELS_RX
    if (recover_mic && s_uac_device->user_cfg.input_cb) {
        s_uac_device->mic_data_size = 0;
        s_uac_device->mic_active = true;
        s_uac_device->mic_suspended = true;
        notify_mic = true;
    }
#endif
    UAC_EXIT_CRITICAL();

#if CFG_TUD_AUDIO_FUNC_1_N_CHANNELS_TX
    if (notify_spk && s_uac_device->spk_task_handle) {
        xTaskNotifyGive(s_uac_device->spk_task_handle);
    }
#endif
#if CFG_TUD_AUDIO_FUNC_1_N_CHANNELS_RX
    if (notify_mic && s_uac_device->mic_task_handle) {
        xTaskNotifyGive(s_uac_device->mic_task_handle);
    }
#endif

    ESP_LOGW(TAG, "manual stream recover: req(spk=%d mic=%d) active(spk=%d mic=%d)",
             recover_speaker ? 1 : 0, recover_mic ? 1 : 0,
             s_uac_device->spk_active ? 1 : 0, s_uac_device->mic_active ? 1 : 0);
    return ESP_OK;
}
