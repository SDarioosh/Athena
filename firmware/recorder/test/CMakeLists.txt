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
    ${FW}/main/audio_stats.c
    ${FW}/main/wav.c
    ${FW}/main/isotime.c
    ${FW}/main/font5x7.c
    ${FW}/main/textfb.c
    ${FW}/boards/twatch_ultra/board/pcf85063a_codec.c
)
target_include_directories(recorder_pure PUBLIC ${FW}/main ${FW}/boards/twatch_ultra/board)
if(UNIX)
    target_link_libraries(recorder_pure PUBLIC m) # log10f in audio_stats.c
endif()

enable_testing()
foreach(t test_audio_stats test_wav test_isotime test_textfb test_pcf85063a_codec)
    add_executable(${t} ${t}.c)
    target_link_libraries(${t} recorder_pure)
    add_test(NAME ${t} COMMAND ${t})
endforeach()
