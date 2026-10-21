# odysseus-sound recorder firmware (ESP-IDF 5.x).
#
# One firmware, one board layer. Pick the board with -DBOARD=<name>; each board is a
# component named "board" under boards/<name>/board that implements main/board.h.
#
#   idf.py -DBOARD=twatch_ultra -DSDKCONFIG_DEFAULTS="sdkconfig.defaults;sdkconfig.twatch_ultra" build
#   idf.py -B build-s3 -DBOARD=twatch_s3 -DSDKCONFIG=build-s3/sdkconfig
#          -DSDKCONFIG_DEFAULTS="sdkconfig.defaults;sdkconfig.twatch_s3" build
#   idf.py -B build-t4 -DBOARD=t4_s3 -DSDKCONFIG=build-t4/sdkconfig
#          -DSDKCONFIG_DEFAULTS="sdkconfig.defaults;sdkconfig.t4_s3" build
cmake_minimum_required(VERSION 3.16)

if(NOT DEFINED BOARD)
    set(BOARD "twatch_ultra")
endif()
set(BOARD "${BOARD}" CACHE STRING "Board layer under boards/")
if(NOT EXISTS "${CMAKE_CURRENT_LIST_DIR}/boards/${BOARD}/board/CMakeLists.txt")
    message(FATAL_ERROR "Unknown board '${BOARD}': no boards/${BOARD}/board/CMakeLists.txt")
endif()

set(EXTRA_COMPONENT_DIRS "${CMAKE_CURRENT_LIST_DIR}/boards/${BOARD}")

# Build only what main (and the board) pull in. This keeps Wi-Fi, BT and everything
# else we never call out of the image; `idf.py size-components` shows what is linked.
set(COMPONENTS main)

include($ENV{IDF_PATH}/tools/cmake/project.cmake)
# Each board has its own component set, so its own lock file: the T-Watch Ultra keeps
# dependencies.lock, other boards get dependencies.lock.<board>.
if(NOT BOARD STREQUAL "twatch_ultra")
    idf_build_set_property(DEPENDENCIES_LOCK "${CMAKE_CURRENT_LIST_DIR}/dependencies.lock.${BOARD}")
endif()
project(recorder)
