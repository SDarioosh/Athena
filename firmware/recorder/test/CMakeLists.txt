# Host tests for the pure parts of the recorder firmware. No ESP-IDF needed:
#   cmake -S firmware/recorder/test -B build-host && cmake --build build-host && ctest --test-dir build-host
cmake_minimum_required(VERSION 3.16)
project(recorder_host_tests C)

set(CMAKE_C_STANDARD 11)
set(CMAKE_C_STANDARD_REQUIRED ON)
if(NOT CMAKE_BUILD_TYPE)
    set(CMAKE_BUILD_TYPE Debug)
endif()
add_compile_options(-Wall -Wextra -Werror)

set(FW ${CMAKE_CURRENT_SOURCE_DIR}/..)

add_library(recorder_pure STATIC
    ${FW}/main/audio_dsp.c
    ${FW}/main/audio_stats.c
    ${FW}/main/wav.c
    ${FW}/main/isotime.c
    ${FW}/main/font5x7.c
    ${FW}/main/textfb.c
    ${FW}/main/button_gesture.c
    ${FW}/main/click_counter.c
    ${FW}/main/key_press.c
    ${FW}/main/mic_mix.c
    ${FW}/main/battery_guard.c
    ${FW}/main/power_samples.c
    ${FW}/boards/twatch_ultra/board/pcf85063a_codec.c
    ${FW}/boards/twatch_s3/board/pcf8563_codec.c
    ${FW}/boards/t4_s3/board/sy6970_codec.c
)
# Pure modules of the BLE upload path (docs/twatch-ble-protocol.md), added when present.
foreach(src ogg_opus.c chunk_queue.c ble_proto.c crc32.c)
    if(EXISTS ${FW}/main/${src})
        target_sources(recorder_pure PRIVATE ${FW}/main/${src})
    endif()
endforeach()
target_include_directories(recorder_pure PUBLIC ${FW}/main ${FW}/boards/twatch_ultra/board
    ${FW}/boards/twatch_s3/board ${FW}/boards/t4_s3/board)
if(UNIX)
    target_link_libraries(recorder_pure PUBLIC m) # libm: audio_stats.c, audio_dsp.c
endif()

enable_testing()
set(TESTS test_audio_dsp test_audio_stats test_wav test_isotime test_textfb test_pcf85063a_codec
    test_pcf8563_codec test_button_gesture test_click_counter test_key_press test_mic_mix test_battery_guard test_power_samples
    test_sy6970_codec)
foreach(t test_ogg_opus test_chunk_queue test_ble_proto test_crc32)
    if(EXISTS ${CMAKE_CURRENT_SOURCE_DIR}/${t}.c)
        list(APPEND TESTS ${t})
    endif()
endforeach()
foreach(t ${TESTS})
    add_executable(${t} ${t}.c)
    target_link_libraries(${t} recorder_pure)
    add_test(NAME ${t} COMMAND ${t})
    # Tests that need scratch files get their own folder in the build tree.
    set_tests_properties(${t} PROPERTIES ENVIRONMENT "TEST_TMPDIR=${CMAKE_CURRENT_BINARY_DIR}/tmp_${t}")
endforeach()
