idf_component_register(SRCS "audio_pipeline.c"
                    INCLUDE_DIRS "include"
                    REQUIRES esp_common freertos
                             driver
                             board_pins
                             audio_i2s_in
                             audio_i2s_out
                             audio_buffers
                             afe_engine)
