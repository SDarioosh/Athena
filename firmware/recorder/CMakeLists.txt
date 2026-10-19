# barvis-sound recorder firmware (ESP-IDF 5.x).
#
# One firmware, one board layer. Pick the board with -DBOARD=<name>; each board is a
# component named "board" under boards/<name>/board that implements main/board.h.
#
#   idf.py -DBOARD=twatch_ultra -DSDKCONFIG_DEFAULTS="sdkconfig.defaults;sdkconfig.twatch_ultra" build
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
project(recorder)
