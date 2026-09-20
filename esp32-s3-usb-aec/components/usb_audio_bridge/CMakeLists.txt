idf_component_register(SRCS "usb_audio_bridge.c"
                    INCLUDE_DIRS "include"
                    REQUIRES esp_common
                             freertos
                             esp_timer
                             audio_pipeline
                             audio_ringbuf
                             usb_device_uac_fork)
