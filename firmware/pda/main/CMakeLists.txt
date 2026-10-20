# Every .c in main/ is part of the firmware; the pure modules are also built by test/ on the
# host. crc32 and isotime come unchanged from the recorder firmware (plan §2 "reused as it is").
set(RECORDER_MAIN "${CMAKE_CURRENT_LIST_DIR}/../../recorder/main")
file(GLOB PDA_SRCS "${CMAKE_CURRENT_LIST_DIR}/*.c")
idf_component_register(
    SRCS ${PDA_SRCS} "${RECORDER_MAIN}/crc32.c" "${RECORDER_MAIN}/isotime.c"
    INCLUDE_DIRS "." "${RECORDER_MAIN}"
    REQUIRES driver esp_timer console nvs_flash esp_pm bt epdiy littlefs mbedtls
)
# The generated font headers carry the glyphs as comments, including bidi control characters.
set_source_files_properties(fonts.c PROPERTIES COMPILE_OPTIONS "-Wno-bidi-chars")
