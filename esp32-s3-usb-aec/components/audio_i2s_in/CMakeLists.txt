idf_component_register(SRCS "audio_i2s_in.c"
                    INCLUDE_DIRS "include"
                    REQUIRES esp_common board_pins esp_driver_i2s freertos)
