idf_component_register(SRCS "afe_engine.c"
                    INCLUDE_DIRS "include"
                    REQUIRES esp_common)

idf_build_get_property(build_components BUILD_COMPONENTS)
set(has_esp_sr FALSE)
if("esp-sr" IN_LIST build_components OR "espressif__esp-sr" IN_LIST build_components)
    set(has_esp_sr TRUE)
endif()

if(has_esp_sr)
    idf_component_optional_requires(PRIVATE esp-sr)
    idf_component_optional_requires(PRIVATE espressif__esp-sr)
    target_compile_definitions(${COMPONENT_LIB} PRIVATE AUDIO_HAS_ESP_SR=1)
else()
    target_compile_definitions(${COMPONENT_LIB} PRIVATE AUDIO_HAS_ESP_SR=0)
endif()
