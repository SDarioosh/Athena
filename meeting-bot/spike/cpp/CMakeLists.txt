# Athena Zoom bot, Phase 0 spike (throwaway). Builds only against the owner's Zoom
# Meeting SDK for Linux, which is never in this repo:
#
#   cmake -S meeting-bot/spike/cpp -B meeting-bot/spike/cpp/build -DZOOM_SDK_DIR=$HOME/zoom-sdk
#   cmake --build meeting-bot/spike/cpp/build
#
# ZOOM_SDK_DIR is the unpacked SDK folder: it holds h/ (headers), libmeetingsdk.so and
# qt_libs/. Not part of CI.
cmake_minimum_required(VERSION 3.22)
project(athena_meeting_spike CXX)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)

set(ZOOM_SDK_DIR "" CACHE PATH "Unpacked Zoom Meeting SDK for Linux (outside the repo)")
if(NOT ZOOM_SDK_DIR OR NOT EXISTS "${ZOOM_SDK_DIR}/h/zoom_sdk.h")
  message(FATAL_ERROR
    "Set -DZOOM_SDK_DIR=/path/to/zoom-meeting-sdk-linux (must contain h/zoom_sdk.h). "
    "Download it from your Zoom Marketplace app; never copy it into the repo.")
endif()

# The SDK's soname is libmeetingsdk.so.1 but it ships as libmeetingsdk.so; Zoom's own
# samples copy it. We make the link in the build dir, never in the SDK folder.
set(SDK_LINK_DIR "${CMAKE_BINARY_DIR}/sdklib")
file(MAKE_DIRECTORY "${SDK_LINK_DIR}")
if(EXISTS "${ZOOM_SDK_DIR}/libmeetingsdk.so" AND NOT EXISTS "${ZOOM_SDK_DIR}/libmeetingsdk.so.1")
  file(CREATE_LINK "${ZOOM_SDK_DIR}/libmeetingsdk.so" "${SDK_LINK_DIR}/libmeetingsdk.so.1" SYMBOLIC)
endif()

find_package(PkgConfig REQUIRED)
pkg_check_modules(GLIB REQUIRED IMPORTED_TARGET glib-2.0)

add_executable(athena-meeting-spike athena_spike.cpp)
target_include_directories(athena-meeting-spike PRIVATE "${ZOOM_SDK_DIR}/h")
target_link_directories(athena-meeting-spike PRIVATE "${ZOOM_SDK_DIR}" "${SDK_LINK_DIR}")
target_link_libraries(athena-meeting-spike PRIVATE meetingsdk PkgConfig::GLIB pthread)
# Find the SDK and its bundled Qt at run time without LD_LIBRARY_PATH.
set_target_properties(athena-meeting-spike PROPERTIES
  BUILD_RPATH "${ZOOM_SDK_DIR};${SDK_LINK_DIR};${ZOOM_SDK_DIR}/qt_libs;${ZOOM_SDK_DIR}/qt_libs/Qt/lib")
