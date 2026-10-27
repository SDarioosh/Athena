# athena-meeting-bot: the Zoom side of the meeting bot. Added by the top-level CMakeLists
# only with -DZOOM_SDK_DIR=<unpacked Zoom Meeting SDK for Linux>, which is never in git:
#
#   cmake -S meeting-bot -B build-bot -DZOOM_SDK_DIR=$HOME/zoom-sdk && cmake --build build-bot -j
#   -> build-bot/athena-meeting-bot
#
# Sandbox / CI compile check without the SDK: -DZOOM_SDK_DIR=$PWD/meeting-bot/bot/sdkstub
# (our own stub headers and a fake libmeetingsdk.so built from stub_meetingsdk.cpp; not Zoom's
# files). The SDK-free logic (options, rules, participants) has host tests either way.

if(NOT EXISTS "${ZOOM_SDK_DIR}/h/zoom_sdk.h")
  message(FATAL_ERROR
    "ZOOM_SDK_DIR=${ZOOM_SDK_DIR} has no h/zoom_sdk.h. Point it at the unpacked Zoom Meeting "
    "SDK for Linux (never copy it into the repo), or at meeting-bot/bot/sdkstub for a "
    "compile check.")
endif()

pkg_check_modules(GLIB REQUIRED IMPORTED_TARGET glib-2.0)

# SDK-free logic: also what the host tests link.
add_library(athena_bot_logic STATIC
  src/options.cpp
  src/rules.cpp
  src/participants.cpp)
target_include_directories(athena_bot_logic PUBLIC "${CMAKE_CURRENT_SOURCE_DIR}/src")
target_compile_options(athena_bot_logic PRIVATE -Wall -Wextra)

add_executable(athena-meeting-bot src/main.cpp)
target_include_directories(athena-meeting-bot PRIVATE "${ZOOM_SDK_DIR}/h")
target_link_libraries(athena-meeting-bot PRIVATE athena_bot_logic athena_core PkgConfig::GLIB
                      Threads::Threads)
target_compile_options(athena-meeting-bot PRIVATE -Wall)
# The launcher's CONVO_MEETING_BOT_PATH points at <build dir>/athena-meeting-bot.
set_target_properties(athena-meeting-bot PROPERTIES RUNTIME_OUTPUT_DIRECTORY "${CMAKE_BINARY_DIR}")

if(EXISTS "${ZOOM_SDK_DIR}/stub_meetingsdk.cpp")
  # Sandbox stub (meeting-bot/bot/sdkstub): build the fake libmeetingsdk.so ourselves.
  message(STATUS "athena-meeting-bot: building against the SANDBOX STUB SDK (not Zoom's)")
  add_library(meetingsdk_stub SHARED "${ZOOM_SDK_DIR}/stub_meetingsdk.cpp")
  target_include_directories(meetingsdk_stub PRIVATE "${ZOOM_SDK_DIR}/h")
  target_link_libraries(meetingsdk_stub PRIVATE PkgConfig::GLIB)
  set_target_properties(meetingsdk_stub PROPERTIES OUTPUT_NAME meetingsdk
                        LIBRARY_OUTPUT_DIRECTORY "${CMAKE_BINARY_DIR}/sdkstub")
  target_link_libraries(athena-meeting-bot PRIVATE meetingsdk_stub)
else()
  # The real SDK (as the spike, meeting-bot/spike/cpp/CMakeLists.txt). Its soname is
  # libmeetingsdk.so.1 but it ships as libmeetingsdk.so: make the link in the build dir,
  # never in the SDK folder.
  set(SDK_LINK_DIR "${CMAKE_BINARY_DIR}/sdklib")
  file(MAKE_DIRECTORY "${SDK_LINK_DIR}")
  if(EXISTS "${ZOOM_SDK_DIR}/libmeetingsdk.so" AND NOT EXISTS "${ZOOM_SDK_DIR}/libmeetingsdk.so.1")
    file(CREATE_LINK "${ZOOM_SDK_DIR}/libmeetingsdk.so" "${SDK_LINK_DIR}/libmeetingsdk.so.1" SYMBOLIC)
  endif()
  target_link_directories(athena-meeting-bot PRIVATE "${ZOOM_SDK_DIR}" "${SDK_LINK_DIR}")
  target_link_libraries(athena-meeting-bot PRIVATE meetingsdk)
  # Find the SDK and its bundled Qt at run time without LD_LIBRARY_PATH.
  set_target_properties(athena-meeting-bot PROPERTIES
    BUILD_RPATH "${ZOOM_SDK_DIR};${SDK_LINK_DIR};${ZOOM_SDK_DIR}/qt_libs;${ZOOM_SDK_DIR}/qt_libs/Qt/lib")
  # An old-style RPATH, not RUNPATH: libmeetingsdk.so loads the bundled Qt libraries itself,
  # and a RUNPATH would only apply to this binary's direct dependencies.
  target_link_options(athena-meeting-bot PRIVATE "-Wl,--disable-new-dtags")
endif()

if(BUILD_TESTING)
  add_executable(bot_logic_test test/bot_logic_test.cpp)
  target_link_libraries(bot_logic_test PRIVATE athena_bot_logic)
  add_test(NAME bot_logic COMMAND bot_logic_test)
  # The whole binary against the stub SDK: status lines and exit codes per scenario.
  if(EXISTS "${ZOOM_SDK_DIR}/stub_meetingsdk.cpp")
    find_package(Python3 COMPONENTS Interpreter)
    if(Python3_FOUND)
      add_test(NAME bot_stub_scenarios
               COMMAND "${Python3_EXECUTABLE}" "${CMAKE_CURRENT_SOURCE_DIR}/test/stub_scenarios.py"
                       "$<TARGET_FILE:athena-meeting-bot>")
    endif()
  endif()
endif()
