# Athena Zoom meeting bot.
#
#   cmake -S meeting-bot -B build-bot && cmake --build build-bot -j && ctest --test-dir build-bot
#       builds the core library (athena_core) and its host tests: no Zoom SDK needed (CI).
#   cmake -S meeting-bot -B build-bot -DZOOM_SDK_DIR=$HOME/zoom-sdk && cmake --build build-bot -j
#       also builds build-bot/athena-meeting-bot against the owner's unpacked Zoom Meeting
#       SDK for Linux, which is never in this repo.
cmake_minimum_required(VERSION 3.22)
project(athena_meeting_bot CXX C)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_CXX_EXTENSIONS OFF)
if(NOT CMAKE_BUILD_TYPE AND NOT CMAKE_CONFIGURATION_TYPES)
  set(CMAKE_BUILD_TYPE RelWithDebInfo CACHE STRING "Build type" FORCE)
endif()

set(ZOOM_SDK_DIR "" CACHE PATH "Unpacked Zoom Meeting SDK for Linux (outside the repo)")

find_package(Threads REQUIRED)
find_package(PkgConfig REQUIRED)
pkg_check_modules(CURL REQUIRED IMPORTED_TARGET libcurl)
pkg_check_modules(OPUSENC REQUIRED IMPORTED_TARGET libopusenc opus ogg)

add_subdirectory(core)

include(CTest)
if(BUILD_TESTING)
  add_subdirectory(test)
endif()

if(ZOOM_SDK_DIR)
  if(EXISTS "${CMAKE_CURRENT_SOURCE_DIR}/bot/CMakeLists.txt")
    add_subdirectory(bot)
  else()
    message(WARNING "ZOOM_SDK_DIR is set but meeting-bot/bot/ is missing: core only")
  endif()
endif()
