# Host tests for the pure parts of the PDA firmware. No ESP-IDF needed:
#   cmake -S firmware/pda/test -B build-pda-host && cmake --build build-pda-host && ctest --test-dir build-pda-host
cmake_minimum_required(VERSION 3.16)
project(pda_host_tests C)

set(CMAKE_C_STANDARD 11)
set(CMAKE_C_STANDARD_REQUIRED ON)
if(NOT CMAKE_BUILD_TYPE)
    set(CMAKE_BUILD_TYPE Debug)
endif()
add_compile_options(-Wall -Wextra -Werror)
# fsync, fileno, strtoul etc. under -std=c11
add_compile_definitions(_DEFAULT_SOURCE _POSIX_C_SOURCE=200809L)

set(FW ${CMAKE_CURRENT_SOURCE_DIR}/..)
set(REC ${CMAKE_CURRENT_SOURCE_DIR}/../../recorder)

add_library(pda_pure STATIC
    ${FW}/main/pda_proto.c
    ${FW}/main/json.c
    ${FW}/main/feed.c
    ${FW}/main/pda_time.c
    ${FW}/main/outbox.c
    ${FW}/main/pda_actions.c
    ${FW}/main/feed_store.c
    ${FW}/main/gesture.c
    ${FW}/main/touch_idle.c
    ${FW}/main/ui_nav.c
    ${REC}/main/crc32.c
    ${REC}/main/isotime.c
)
target_include_directories(pda_pure PUBLIC ${FW}/main ${REC}/main ${REC}/test)

enable_testing()
foreach(t test_pda_proto test_json test_feed test_outbox test_feed_store test_gesture test_touch_idle test_ui_nav)
    add_executable(${t} ${t}.c)
    target_link_libraries(${t} pda_pure)
    add_test(NAME ${t} COMMAND ${t})
    set_tests_properties(${t} PROPERTIES ENVIRONMENT
        "TEST_TMPDIR=${CMAKE_CURRENT_BINARY_DIR}/tmp_${t};FIXTURES=${CMAKE_CURRENT_SOURCE_DIR}/fixtures")
endforeach()
