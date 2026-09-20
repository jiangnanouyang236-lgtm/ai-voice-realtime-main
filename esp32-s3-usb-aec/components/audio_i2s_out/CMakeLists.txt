idf_component_register(SRCS "audio_i2s_out.c"
                    INCLUDE_DIRS "include"
                    REQUIRES esp_common board_pins esp_driver_i2s freertos esp_timer)
